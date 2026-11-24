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
from atlas.modules.chat_history.models import RoomEventRecord, RoomRecord
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
