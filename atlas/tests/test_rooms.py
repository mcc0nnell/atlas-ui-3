"""Room persistence, cross-instance ordering and authenticated API contracts."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from atlas.core.log_sanitizer import get_current_user
from atlas.modules.chat_history.models import RoomEventRecord, RoomParticipantTurnRecord, RoomRecord, RoomTurnRecord
from atlas.modules.chat_history.room_repository import RoomRepository
from atlas.routes import room_routes


@pytest.fixture
def repositories():
    url = os.environ.get("ATLAS_ROOM_TEST_DB_URL")
    if not url:
        pytest.skip("Set ATLAS_ROOM_TEST_DB_URL to a disposable PostgreSQL database")
    schema = "room_test_" + uuid.uuid4().hex
    engine = create_engine(url)
    second = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(CreateSchema(schema))
        a = engine.execution_options(schema_translate_map={None: schema})
        b = second.execution_options(schema_translate_map={None: schema})
        RoomRecord.__table__.create(a)
        RoomEventRecord.__table__.create(a)
        RoomTurnRecord.__table__.create(a)
        RoomParticipantTurnRecord.__table__.create(a)
        yield RoomRepository(sessionmaker(bind=a)), RoomRepository(sessionmaker(bind=b))
    finally:
        second.dispose()
        with engine.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


def client(repo, user):
    app = FastAPI()
    app.include_router(room_routes.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[room_routes._repo] = lambda: repo
    return TestClient(app)


def message(txn, text="hello"):
    return {"transaction_id": txn, "event": {"type": "room.message", "text": text}}


def test_room_isolation_and_cursor(repositories):
    a, b = repositories
    owner = client(a, "Alice@example.com")
    same = client(b, "alice@example.com")
    other = client(b, "bob@example.com")
    created = owner.post("/api/rooms", json={"name": "Atlas"})
    assert created.status_code == 201
    room_id = created.json()["id"]
    path = f"/api/rooms/{room_id}/events"
    response = same.get("/api/rooms")
    assert response.json()["rooms"][0]["id"] == room_id
    response = other.get("/api/rooms")
    assert response.json() == {"rooms": []}
    response = other.get(path)
    assert response.status_code == 404
    response = other.post(path, json=message("x"))
    assert response.status_code == 404
    first = owner.post(path, json=message("1"))
    assert first.status_code == 200
    assert first.json()["sender"] == "alice@example.com"
    second = same.post(path, json={"transaction_id": "2", "event": {
        "type": "room.agent.added", "agent_id": "reviewer"}})
    assert second.status_code == 200
    response = owner.post(path, json=message("1"))
    assert response.json() == first.json()
    response = same.post(path, json=message("1", "different"))
    assert response.status_code == 409
    page = same.get(path, params={"limit": 1}).json()
    assert page["has_more"] is True
    assert page["next_after"] == 1
    rest = same.get(path, params={"after": 1}).json()
    assert [item["type"] for item in rest["events"]] == ["room.agent.added"]
    assert rest["has_more"] is False
    response = owner.post(path, json={"transaction_id": "3", "event": {
        "type": "room.message", "text": "spoof", "sender": "reviewer"}})
    assert response.status_code == 422


def test_independent_repositories_serialize_and_deduplicate(repositories):
    a, b = repositories
    room_id = a.create("owner@example.com", "Concurrent")["id"]

    def append(i):
        repo = a if i % 2 else b
        return repo.append("owner@example.com", room_id, str(i // 2),
                           "room.message", {"text": str(i // 2)})["sequence"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(append, range(40)))
    assert sorted(sequences) == sorted(list(range(1, 21)) * 2)
    events = b.timeline("owner@example.com", room_id)["events"]
    assert [e["sequence"] for e in events] == list(range(1, 21))
    assert a.list("owner@example.com")[0]["last_sequence"] == 20


def test_request_contract_without_database():
    c = client(object(), "owner@example.com")
    response = c.post("/api/rooms", json={"name": " "})
    assert response.status_code == 422
    response = c.post("/api/rooms/r/events", json={"transaction_id": "1", "event": {
        "type": "agent.execute", "agent_id": "x"}})
    assert response.status_code == 422
    response = c.get("/api/rooms/r/events", params={"limit": 201})
    assert response.status_code == 422


@pytest.mark.parametrize("enabled,history,compliance,expected", [
    (False, True, False, 404),
    (True, False, False, 404),
    (True, True, True, 404),
    (True, True, False, 200),
])
def test_room_feature_gate(monkeypatch, enabled, history, compliance, expected):
    from atlas.infrastructure.app_factory import app_factory

    settings = app_factory.get_config_manager().app_settings
    monkeypatch.setattr(settings, "feature_rooms_enabled", enabled)
    monkeypatch.setattr(settings, "feature_chat_history_enabled", history)
    monkeypatch.setattr(settings, "feature_compliance_levels_enabled", compliance)
    monkeypatch.setattr(app_factory, "room_repository", object())
    if expected == 200:
        assert room_routes._repo() is app_factory.room_repository
    else:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            room_routes._repo()
        assert exc.value.status_code == expected


def test_room_agent_turn_reservation_and_reply(repositories):
    from atlas.modules.chat_history.room_repository import AgentNotFound, RoomBusy, RoomNotFound, TransactionConflict

    a, b = repositories
    owner = "owner@example.com"
    room = a.create(owner, "Agents")["id"]
    with pytest.raises(AgentNotFound):
        a.reserve_turn(owner, room, "t", "Reviewer", "model", "Discuss")
    a.append(owner, room, "join", "room.agent.added", {"agent_id": "Reviewer"})
    a.append(owner, room, "msg", "room.message", {"text": "Earlier context"})
    turn, context = a.reserve_turn(owner, room, "t", "Reviewer", "model", "Discuss")
    assert turn["state"] == "pending"
    assert any(e["content"].get("text") == "Earlier context" for e in context["room"])
    retry, context = b.reserve_turn(owner, room, "t", "Reviewer", "model", "Discuss")
    assert retry == turn
    assert context is None
    with pytest.raises(RoomBusy):
        b.reserve_turn(owner, room, "other", "Reviewer", "model", "Discuss")
    with pytest.raises(TransactionConflict):
        b.reserve_turn(owner, room, "t", "Reviewer", "model", "Different")
    with pytest.raises(RoomNotFound):
        b.finish_turn("other@example.com", room, "t", "forged")
    done = b.finish_turn(owner, room, "t", "Native response")
    assert done["state"] == "completed"
    assert a.finish_turn(owner, room, "t", "duplicate") == done
    reply = a.timeline(owner, room)["events"][-1]
    assert reply["type"] == "room.agent.reply"
    assert reply["content"]["agent_id"] == "Reviewer"
    assert reply["content"]["text"] == "Native response"
    a.append(owner, room, "leave", "room.agent.removed", {"agent_id": "Reviewer"})
    with pytest.raises(AgentNotFound):
        a.reserve_turn(owner, room, "new", "Reviewer", "model", "Discuss")


def test_expired_turn_is_not_replayed_and_late_reply_is_ignored(repositories):
    from datetime import datetime, timedelta, timezone

    a, b = repositories
    owner = "owner@example.com"
    room = a.create(owner, "Recovery")["id"]
    a.append(owner, room, "join", "room.agent.added", {"agent_id": "Reviewer"})
    a.reserve_turn(owner, room, "old", "Reviewer", "model", "Discuss")
    with a._session_factory.begin() as session:
        record = session.get(RoomTurnRecord, (room, "old"))
        record.created_at = datetime.now(timezone.utc) - timedelta(minutes=4)
    retry, context = b.reserve_turn(owner, room, "old", "Reviewer", "model", "Discuss")
    assert retry["state"] == "failed"
    assert context is None
    new, context = b.reserve_turn(owner, room, "new", "Reviewer", "model", "Continue")
    assert new["state"] == "pending"
    assert context is not None
    result = a.finish_turn(owner, room, "old", "Late reply")
    assert result["state"] == "failed"
    assert [event["type"] for event in a.timeline(owner, room)["events"]] == ["room.agent.added"]


def test_persistent_participant_history_is_scoped_and_generation_safe(repositories):
    a, b = repositories
    owner = "owner@example.com"
    room = a.create(owner, "Design")["id"]
    for name in ("Designer", "Reviewer"):
        a.append(owner, room, name, "room.agent.added", {"agent_id": name})
    first, context = a.reserve_turn(owner, room, "one", "Designer", "m1", "private-design-brief")
    identity = context["participant_id"]
    assert context["participant"] == []
    b.finish_turn(owner, room, "one", "shared-design-reply")
    a.append(owner, room, "duplicate-add", "room.agent.added", {"agent_id": "Designer"})
    second, context = b.reserve_turn(owner, room, "two", "Designer", "m1", "follow-up")
    assert context["participant_id"] == identity
    assert context["participant"] == [
        {"prompt": "private-design-brief", "reply": "shared-design-reply", "model": "m1"}
    ]
    assert second["conversation_id"] != first["conversation_id"]
    a.finish_turn(owner, room, "two", "follow-up-reply")
    _, context = b.reserve_turn(owner, room, "three", "Reviewer", "m2", "review")
    assert context["participant_id"] != identity
    assert context["participant"] == []
    assert any(e["content"].get("text") == "shared-design-reply" for e in context["room"])
    assert "private-design-brief" not in str(context)
    a.finish_turn(owner, room, "three", "review-reply")
    roster = b.participants(owner, room)
    assert roster[0]["participant_id"] == identity
    assert roster[0]["context_turns"] == 2
    assert roster[0]["last_model"] == "m1"
    a.append(owner, room, "leave", "room.agent.removed", {"agent_id": "Designer"})
    a.append(owner, room, "rejoin", "room.agent.added", {"agent_id": "Designer"})
    _, context = b.reserve_turn(owner, room, "four", "Designer", "m1", "new membership")
    assert context["participant_id"] != identity
    assert context["participant"] == []
    other_room = a.create(owner, "Other")["id"]
    a.append(owner, other_room, "join", "room.agent.added", {"agent_id": "Designer"})
    _, other = b.reserve_turn(owner, other_room, "one", "Designer", "m1", "other room")
    assert other["participant"] == []
    assert other["room"] == []
    assert other["participant_id"] not in {identity, context["participant_id"]}


def test_context_budget_preserves_complete_records():
    items = [{"text": "old"}, {"text": "x" * 500}, {"text": "latest"}]
    assert RoomRepository._bounded_context(items, 70) == [items[0], items[2]]
