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
from atlas.modules.chat_history.models import (
    RoomArtifactRevisionRecord,
    RoomEventRecord,
    RoomNotebookRunRecord,
    RoomNotebookStateRecord,
    RoomParticipantTurnRecord,
    RoomRecord,
    RoomTurnRecord,
)
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
        RoomArtifactRevisionRecord.__table__.create(a)
        RoomNotebookStateRecord.__table__.create(a)
        RoomNotebookRunRecord.__table__.create(a)
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

def test_artifact_revisions_are_immutable_and_owner_scoped(repositories):
    a, b = repositories
    owner = client(a, "owner@example.com")
    same = client(b, "owner@example.com")
    other = client(b, "other@example.com")
    room = owner.post("/api/rooms", json={"name": "Research"}).json()["id"]
    other_room = owner.post("/api/rooms", json={"name": "Separate"}).json()["id"]
    artifact = str(uuid.uuid4())
    path = f"/api/rooms/{room}/events"
    base = f"/api/rooms/{room}/artifacts/{artifact}/revisions"
    body = {"transaction_id": "a", "event": {"type": "room.artifact.revised",
            "artifact_id": artifact, "base_revision": 0, "kind": "workflow",
            "title": "Pipeline", "text": "inert v1"}}
    first = owner.post(path, json=body)
    assert first.status_code == 200
    assert first.json()["content"]["revision"] == 1
    assert "text" not in first.json()["content"]
    assert same.post(path, json=body).json() == first.json()
    assert other.post(path, json=body).status_code == 404
    assert other.get(base + "/1").status_code == 404
    assert owner.get(f"/api/rooms/{other_room}/artifacts/{artifact}/revisions/1").status_code == 404
    body["event"]["text"] = "inert v2"
    assert same.post(path, json=body).status_code == 409
    body["transaction_id"] = "b"
    assert same.post(path, json=body).status_code == 409
    body["event"]["base_revision"] = 1
    second = same.post(path, json=body)
    assert second.status_code == 200
    assert second.json()["sequence"] == 2
    assert owner.get(base + "/1").json()["text"] == "inert v1"
    assert owner.get(base + "/2").json()["text"] == "inert v2"
    assert owner.get(base + "/3").status_code == 404
    assert owner.get(path).json()["next_after"] == 2
    body["transaction_id"] = "c"
    body["event"]["base_revision"] = 2
    body["event"]["kind"] = "document"
    assert same.post(path, json=body).status_code == 409


def test_artifact_compare_and_set_across_repositories(repositories):
    from atlas.modules.chat_history.room_repository import ArtifactConflict

    a, b = repositories
    user = "owner@example.com"
    room = a.create(user, "Race")["id"]
    artifact = str(uuid.uuid4())
    def write(i):
        try:
            return (a if i == 0 else b).revise_artifact(
                user, room, str(i), artifact, 0, "document", "Note", str(i))
        except ArtifactConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(a.timeline(user, room)["events"]) == 1
    assert b.artifact_revision(user, room, artifact, 1)["text"] in ("0", "1")


def test_artifact_payload_validation_without_database():
    c = client(object(), "owner@example.com")
    event = {"type": "room.artifact.revised", "artifact_id": str(uuid.uuid4()),
             "base_revision": 0, "kind": "workflow", "title": "Example", "text": "inert"}
    for change in ({"base_revision": -1}, {"base_revision": True}, {"base_revision": 2147483647},
                   {"artifact_id": "bad"}, {"kind": "unknown"}, {"text": "x" * 64001},
                   {"text": "a\x00b"}, {"title": "a\x00b"}, {"revision": 1},
                   {"sender": "other@example.com"}):
        assert c.post("/api/rooms/r/events", json={"transaction_id": "t", "event": {**event, **change}}).status_code == 422


def test_notebook_revision_and_cell_discussion_across_repositories(repositories):
    import json

    import nbformat

    a, b = repositories
    owner = client(a, "owner@example.com")
    same = client(b, "owner@example.com")
    other = client(b, "other@example.com")
    room = owner.post("/api/rooms", json={"name": "Notebook"}).json()["id"]
    artifact = str(uuid.uuid4())
    path = f"/api/rooms/{room}/events"
    base = f"/api/rooms/{room}/artifacts/{artifact}/revisions"
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("10 + 20", id="sum-cell")])
    body = {"transaction_id": "nb1", "event": {"type": "room.artifact.revised", "artifact_id": artifact,
            "base_revision": 0, "kind": "notebook", "title": "Analysis", "text": json.dumps(notebook)}}
    first = owner.post(path, json=body)
    assert first.status_code == 200, first.text
    assert same.get(path).json()["events"] == [first.json()]
    saved = same.get(base + "/1").json()
    nbformat.validate(nbformat.reads(saved["text"], as_version=4))
    comment = {"transaction_id": "comment1", "event": {"type": "room.notebook.comment", "artifact_id": artifact,
               "revision": 1, "cell_id": "sum-cell", "text": "Review this cell"}}
    result = same.post(path, json=comment)
    assert result.status_code == 200, result.text
    assert owner.post(path, json=comment).json() == result.json()
    assert other.post(path, json=comment).status_code == 404
    assert other.get(base + "/1").status_code == 404
    assert owner.post(path, json={"transaction_id": "bad", "event": {**comment["event"], "cell_id": "missing"}}).status_code == 422
    notebook.cells[0].source = "10 + 20 + 1"
    body["transaction_id"] = "nb2"
    body["event"]["base_revision"] = 1
    body["event"]["text"] = json.dumps(notebook)
    assert same.post(path, json=body).status_code == 200
    assert owner.get(path, params={"after": 2}).json()["events"][0]["content"]["revision"] == 2
    assert nbformat.reads(owner.get(base + "/1").json()["text"], as_version=4).cells[0].source == "10 + 20"
    assert nbformat.reads(owner.get(base + "/2").json()["text"], as_version=4).cells[0].source == "10 + 20 + 1"
    body["transaction_id"] = "stale"
    assert owner.post(path, json=body).status_code == 409


def test_notebook_validation():
    import json

    import nbformat

    from atlas.modules.chat_history.room_notebooks import InvalidNotebook, notebook_cells

    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_markdown_cell("hello", id="stable")])
    assert notebook_cells(json.dumps(nb)) == {"stable"}
    for invalid in ("{}", "null", "{", json.dumps({**nb, "nbformat_minor": 4}),
                    json.dumps({**nb, "cells": [nb.cells[0], nb.cells[0]]}),
                    json.dumps({**nb, "cells": [{**nb.cells[0], "id": "bad id"}]})):
        with pytest.raises(InvalidNotebook):
            notebook_cells(invalid)


def test_live_notebook_concurrent_updates_and_checkpoint_replay(repositories):
    import base64
    import json

    import nbformat
    from jupyter_ydoc import YNotebook

    a, b = repositories
    owner = client(a, "owner@example.com")
    same = client(b, "owner@example.com")
    other = client(b, "other@example.com")
    room = owner.post("/api/rooms", json={"name": "Live"}).json()["id"]
    artifact = str(uuid.uuid4())
    path = f"/api/rooms/{room}/events"
    base = f"/api/rooms/{room}/artifacts/{artifact}"
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("middle", id="cell")])
    body = {"transaction_id": "nb1", "event": {"type": "room.artifact.revised", "artifact_id": artifact,
            "base_revision": 0, "kind": "notebook", "title": "Live", "text": json.dumps(notebook)}}
    assert owner.post(path, json=body).status_code == 200
    sync = base + "/collaboration"
    initial = owner.post(sync, json={}).json()
    assert initial["revision"] == 1
    assert same.post(sync, json={}).json() == initial
    assert other.post(sync, json={}).status_code == 404
    one, two = YNotebook(), YNotebook()
    for doc in (one, two):
        doc.ydoc.apply_update(base64.b64decode(initial["state"]))
    one.ycells[0]["source"].insert(0, "left ")
    two.ycells[0]["source"].insert(len(two.ycells[0]["source"]), " right")
    updates = [base64.b64encode(doc.ydoc.get_update()).decode() for doc in (one, two)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda i: (owner if i == 0 else same).post(sync, json={"update": updates[i]}), range(2)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    final = same.post(sync, json={}).json()
    assert final["revision"] == 3
    assert nbformat.reads(final["text"], as_version=4).cells[0].source == "left middle right"
    for doc in (one, two):
        doc.ydoc.apply_update(base64.b64decode(final["state"]))
        assert doc.get()["cells"][0]["source"] == "left middle right"
    assert same.post(sync, json={"update": updates[0]}).json()["revision"] == 3
    assert owner.post(sync, json={"update": "not base64"}).status_code == 422
    assert owner.post(sync, json={"update": "YWJj"}).status_code == 422
    assert other.post(sync, json={"update": updates[0]}).status_code == 404
    assert owner.get(path).json()["next_after"] == 3
    assert owner.get(base + "/revisions/1").json()["text"] == body["event"]["text"]
    assert owner.get(base + "/revisions/3").json()["text"] == final["text"]
    body["event"]["base_revision"] = 3
    body["transaction_id"] = "direct"
    assert owner.post(path, json=body).status_code == 409
    comment = {"transaction_id": "comment", "event": {"type": "room.notebook.comment", "artifact_id": artifact,
               "revision": 3, "cell_id": "cell", "text": "Merged result"}}
    assert owner.post(path, json=comment).status_code == 200
    assert same.post(sync, json={}).json()["revision"] == 3


@pytest.mark.skipif(os.environ.get("ATLAS_ROOM_JS_TESTS") != "1", reason="Enable with installed frontend dependencies")
def test_live_notebook_jupyter_javascript_interop(repositories):
    import json
    import subprocess
    from pathlib import Path

    import nbformat

    a, b = repositories
    owner = client(a, "owner@example.com")
    same = client(b, "owner@example.com")
    room = owner.post("/api/rooms", json={"name": "Interop"}).json()["id"]
    artifact = str(uuid.uuid4())
    base = f"/api/rooms/{room}/artifacts/{artifact}"
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("middle", id="shared-cell")])
    body = {"transaction_id": "nb", "event": {"type": "room.artifact.revised", "artifact_id": artifact,
            "base_revision": 0, "kind": "notebook", "title": "Interop", "text": json.dumps(nb)}}
    assert owner.post(f"/api/rooms/{room}/events", json=body).status_code == 200
    sync = base + "/collaboration"
    initial = owner.post(sync, json={}).json()
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    script = '''import { YNotebook } from '@jupyter/ydoc';
import * as Y from 'yjs';
import { decodeState, encodeState, editSource } from './src/components/notebookCollaboration.js';
import { readFileSync } from 'node:fs';
const {state, source} = JSON.parse(readFileSync(0, 'utf8'));
const notebook = new YNotebook();
Y.applyUpdate(notebook.ydoc, decodeState(state));
if (source !== undefined) notebook.transact(() => editSource(notebook.cells[0], source));
console.log(JSON.stringify({source: notebook.cells[0].getSource(), id: notebook.cells[0].id,
  update: encodeState(Y.encodeStateAsUpdate(notebook.ydoc))}));
notebook.dispose();'''

    def js(state, source=None):
        payload = {"state": state}
        if source is not None:
            payload["source"] = source
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=frontend,
                                input=json.dumps(payload), text=True, capture_output=True, timeout=15, check=True)
        return json.loads(result.stdout)

    left = js(initial["state"], "left middle")
    right = js(initial["state"], "middle right")
    assert left["id"] == right["id"] == "shared-cell"
    assert owner.post(sync, json={"update": left["update"]}).status_code == 200
    assert same.post(sync, json={"update": right["update"]}).status_code == 200
    final = owner.post(sync, json={}).json()
    assert final["revision"] == 3
    assert js(final["state"])["source"] == "left middle right"
    assert nbformat.reads(final["text"], as_version=4).cells[0].source == "left middle right"
    assert same.post(sync, json={"update": left["update"]}).json()["revision"] == 3


@pytest.mark.skipif(not (os.environ.get("ATLAS_NOTEBOOK_TEST_IMAGE") and os.environ.get("ATLAS_NOTEBOOK_TEST_DEPS")),
                    reason="Requires preapproved rootless Podman image and dedicated Jupyter/TalkPipe dependency directory")
def test_isolated_notebook_checkpoint_execution(repositories):
    import json
    import subprocess
    from pathlib import Path

    import nbformat
    from httpx import HTTPStatusError

    from scripts.notebook_runner.bridge import NotebookRunError, execute_checkpoint, publish_result, run_isolated

    a, b = repositories
    owner = client(a, "owner@example.com")
    same = client(b, "owner@example.com")
    other = client(b, "other@example.com")
    room = owner.post("/api/rooms", json={"name": "Computational room"}).json()["id"]
    artifact = str(uuid.uuid4())
    path = f"/api/rooms/{room}/events"
    image = os.environ["ATLAS_NOTEBOOK_TEST_IMAGE"]
    deps = os.environ["ATLAS_NOTEBOOK_TEST_DEPS"]
    notebook = nbformat.v4.new_notebook(cells=[
        nbformat.v4.new_markdown_cell("# Review fixture", id="intro"),
        nbformat.v4.new_code_cell('from talkpipe.chatterlang import compiler\nnormalize = compiler.compile(\'| toDict[field_list="_:text"]\').as_function(single_in=True, single_out=True)', id="setup"),
        nbformat.v4.new_code_cell('records = [normalize(x) for x in ["Human: Review the draft.", "Reviewer: Verify the figures."]]\nrecords', id="records"),
        nbformat.v4.new_code_cell('import json\nprint(json.dumps(records))', id="evidence"),
    ])
    body = {"transaction_id": "input", "event": {"type": "room.artifact.revised", "artifact_id": artifact,
            "base_revision": 0, "kind": "notebook", "title": "Review", "text": json.dumps(notebook)}}
    first = owner.post(path, json=body)
    assert first.status_code == 200
    source_url = f"/api/rooms/{room}/artifacts/{artifact}/revisions/1"
    source = same.get(source_url).json()
    assert owner.post(f"/api/rooms/{room}/artifacts/{artifact}/collaboration", json={}).status_code == 200
    with pytest.raises(HTTPStatusError) as denied:
        execute_checkpoint(other, room, artifact, 1, image, deps)
    assert denied.value.response.status_code == 404
    result = execute_checkpoint(owner, room, artifact, 1, image, deps)
    # Explicit publication is separate from execution and uses an idempotent transaction.
    assert same.get(path).json()["next_after"] == 1
    result_event = publish_result(owner, room, result)
    assert publish_result(same, room, result) == result_event
    output_id = result_event["content"]["artifact_id"]
    output_url = f"/api/rooms/{room}/artifacts/{output_id}/revisions/1"
    output = same.get(output_url).json()
    assert other.get(output_url).status_code == 404
    executed = nbformat.reads(output["text"], as_version=4)
    expected = [{"text": "Human: Review the draft."}, {"text": "Reviewer: Verify the figures."}]
    assert json.loads(executed.cells[-1].outputs[0].text) == expected
    assert [c.execution_count for c in executed.cells if c.cell_type == "code"] == [1, 2, 3]
    assert executed.metadata.atlas_execution.source_sha256 == source["sha256"]
    assert executed.metadata.atlas_execution.source_artifact_id == artifact
    assert executed.metadata.atlas_execution.source_revision == 1
    assert owner.get(source_url).json() == source
    assert same.get(path).json()["next_after"] == 2
    comment = {"transaction_id": "discuss", "event": {"type": "room.notebook.comment", "artifact_id": output_id,
               "revision": 1, "cell_id": "records", "text": "Review the computed records"}}
    assert same.post(path, json=comment).status_code == 200
    # A failed or timed-out kernel is not a successful result; force cleanup is verified below.
    bad = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell('raise ValueError("fixture failure")')])
    with pytest.raises(NotebookRunError):
        run_isolated(json.dumps(bad), image, deps)
    slow = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell('import time; time.sleep(30)')])
    with pytest.raises(NotebookRunError):
        run_isolated(json.dumps(slow), image, deps, wall_seconds=2)
    assert same.get(path).json()["next_after"] == 3
    containers = subprocess.run(["podman", "ps", "-a", "--filter", "name=^atlas-notebook-[a-f0-9]{32}$", "--format", "{{.Names}}"],
                                check=True, capture_output=True, text=True).stdout.strip()
    assert not containers
    evidence = os.environ.get("ATLAS_NOTEBOOK_TEST_EVIDENCE")
    if evidence:
        root = Path(evidence)
        root.mkdir(parents=True, exist_ok=True)
        nbformat.write(executed, root / "executed.ipynb")
        (root / "results.json").write_text(json.dumps({"records": expected, "input_sha256": source["sha256"],
            "output_sha256": output["sha256"], "input_unchanged": True, "idempotent_publish": True,
            "owner_scope": True, "cell_discussion": True, "kernel_failure": True, "timeout_cleanup": True,
            "versions": executed.metadata.atlas_execution.versions, "runtime_image": image}, indent=2))


def test_notebook_run_queue_approval_and_claim(repositories, monkeypatch):
    import json

    import nbformat

    a, b = repositories
    owner, same, other = client(a, "owner@example.com"), client(b, "owner@example.com"), client(b, "other@example.com")
    room = owner.post("/api/rooms", json={"name": "Run queue"}).json()["id"]
    artifact, run = str(uuid.uuid4()), str(uuid.uuid4())
    events = f"/api/rooms/{room}/events"
    url = f"/api/rooms/{room}/notebook-runs"
    source = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("10+20", id="sum")])
    saved = owner.post(events, json={"transaction_id": "save", "event": {"type": "room.artifact.revised", "artifact_id": artifact,
        "base_revision": 0, "kind": "notebook", "title": "Sum", "text": json.dumps(source)}}).json()
    body = {"run_id": run, "artifact_id": artifact, "revision": 1, "sha256": saved["content"]["sha256"], "approved": True}
    assert owner.post(url, json=body).status_code == 404
    monkeypatch.setattr(room_routes, "_execution_enabled", lambda: True)
    assert owner.post(url, json={**body, "approved": False}).status_code == 422
    assert other.post(url, json=body).status_code == 404
    assert owner.post(url, json={**body, "sha256": "0" * 64}).status_code == 409
    queued = owner.post(url, json=body).json()
    assert queued["state"] == "queued"
    assert same.post(url, json=body).json() == queued
    assert other.get(f"{url}/{run}").status_code == 404
    assert same.post(url, json={**body, "run_id": str(uuid.uuid4())}).status_code == 409
    assert owner.post(f"{url}/{run}/cancel").json()["state"] == "cancelled"
    assert b.claim_notebook_run("owner@example.com", room, run) is None
    run = str(uuid.uuid4())
    body["run_id"] = run
    assert owner.post(url, json=body).status_code == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda repo: repo.claim_notebook_run("owner@example.com", room, run), [a, b]))
    assert sum(claim is not None for claim in claims) == 1
    assert same.get(f"{url}/{run}").json()["state"] == "running"
    assert owner.post(f"{url}/{run}/cancel").status_code == 409
    assert a.finish_notebook_run("owner@example.com", room, run)["state"] == "failed"
    assert b.claim_notebook_run("owner@example.com", room, run) is None
    assert owner.post(url, json=body).json()["state"] == "failed"


@pytest.mark.skipif(not (os.environ.get("ATLAS_NOTEBOOK_TEST_IMAGE") and os.environ.get("ATLAS_NOTEBOOK_TEST_DEPS")), reason="Requires isolated runner")
def test_notebook_run_worker_real_kernel(repositories, monkeypatch):
    import json

    import nbformat

    from scripts.notebook_runner.worker import work_once

    a, b = repositories
    owner, same = client(a, "owner@example.com"), client(b, "owner@example.com")
    monkeypatch.setattr(room_routes, "_execution_enabled", lambda: True)
    room = owner.post("/api/rooms", json={"name": "Worker proof"}).json()["id"]
    artifact, run = str(uuid.uuid4()), str(uuid.uuid4())
    events = f"/api/rooms/{room}/events"
    url = f"/api/rooms/{room}/notebook-runs"
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell('from talkpipe.chatterlang import compiler\nprint(compiler.compile(\'| toDict[field_list="_:text"]\').as_function(single_in=True,single_out=True)("hello"))', id="calculation")])
    saved = owner.post(events, json={"transaction_id": "save", "event": {"type": "room.artifact.revised", "artifact_id": artifact,
        "base_revision": 0, "kind": "notebook", "title": "Calculation", "text": json.dumps(nb)}}).json()
    body = {"run_id": run, "artifact_id": artifact, "revision": 1, "sha256": saved["content"]["sha256"], "approved": True}
    assert owner.post(url, json=body).json()["state"] == "queued"
    args = ("owner@example.com", room, os.environ["ATLAS_NOTEBOOK_TEST_IMAGE"], os.environ["ATLAS_NOTEBOOK_TEST_DEPS"], run)
    result = work_once(b, *args)
    assert result["state"] == "completed", result
    assert work_once(a, *args) is None
    assert same.get(f"{url}/{run}").json() == result
    assert owner.post(url, json=body).json() == result
    output = same.get(f"/api/rooms/{room}/artifacts/{result['result_artifact_id']}/revisions/1").json()
    executed = nbformat.reads(output["text"], as_version=4)
    assert executed.cells[0].outputs[0].text == "{'text': 'hello'}\n"
    assert executed.metadata.atlas_execution.run_id == run
    assert owner.get(f"/api/rooms/{room}/artifacts/{artifact}/revisions/1").json()["sha256"] == body["sha256"]
    timeline = same.get(events).json()["events"]
    assert [e["content"]["state"] for e in timeline if e["type"] == "room.notebook.run"] == ["queued", "running", "completed"]
    assert len(timeline) == 5
