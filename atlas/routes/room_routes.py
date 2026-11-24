"""Opt-in room API: authenticated owner, immutable events, cursor pagination."""

import asyncio
import logging
from typing import Annotated, Literal, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field, StringConstraints, field_validator
from starlette.concurrency import run_in_threadpool

from atlas.core.log_sanitizer import get_current_user
from atlas.modules.chat_history.room_notebooks import MAX_STATE_TEXT, InvalidNotebook
from atlas.modules.chat_history.room_repository import (
    AgentNotFound,
    ArtifactConflict,
    RoomBusy,
    RoomNotFound,
    TransactionConflict,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rooms", tags=["rooms"])
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
TransactionId = Annotated[str, StringConstraints(min_length=1, max_length=100)]
RoomId = Annotated[str, StringConstraints(min_length=1, max_length=36)]


class ClosedModel(BaseModel):
    model_config = {"extra": "forbid"}


class CreateRoom(ClosedModel):
    name: Name


class MessageEvent(ClosedModel):
    type: Literal["room.message"]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]


class NotebookCommentEvent(ClosedModel):
    type: Literal["room.notebook.comment"]
    artifact_id: UUID
    revision: Annotated[int, Field(strict=True, ge=1, le=2147483647)]
    cell_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]


class AgentEvent(ClosedModel):
    type: Literal["room.agent.added", "room.agent.removed"]
    agent_id: Name


class ArtifactEvent(ClosedModel):
    type: Literal["room.artifact.revised"]
    artifact_id: UUID
    base_revision: Annotated[int, Field(strict=True, ge=0, lt=2147483647)]
    kind: Literal["document", "workflow", "result", "notebook"]
    title: Name
    text: Annotated[str, StringConstraints(max_length=64000)]

    @field_validator("title", "text")
    @classmethod
    def valid_storage_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Null characters are not supported")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Invalid Unicode text") from exc
        return value


class AppendEvent(ClosedModel):
    transaction_id: TransactionId
    event: Annotated[Union[MessageEvent, AgentEvent, ArtifactEvent, NotebookCommentEvent], Field(discriminator="type")]


def _execution_enabled():
    from atlas.infrastructure.app_factory import app_factory

    return bool(app_factory.get_config_manager().app_settings.feature_room_notebook_runs_enabled)


def _require_execution():
    if not _execution_enabled():
        raise HTTPException(404, "Notebook execution not enabled")


def _repo():
    from atlas.infrastructure.app_factory import app_factory

    settings = app_factory.get_config_manager().app_settings
    if not (settings.feature_rooms_enabled and settings.feature_chat_history_enabled) or settings.feature_compliance_levels_enabled:
        raise HTTPException(404, "Rooms not enabled")
    repo = getattr(app_factory, "room_repository", None)
    if repo is None:
        raise HTTPException(503, "Rooms require available PostgreSQL chat history")
    return repo


@router.get("")
def list_rooms(user: str = Depends(get_current_user), repo=Depends(_repo)):
    return {"rooms": repo.list(user)}


@router.post("", status_code=201)
def create_room(body: CreateRoom, user: str = Depends(get_current_user), repo=Depends(_repo)):
    return repo.create(user, body.name)


@router.get("/{room_id}/events")
def timeline(room_id: RoomId, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
             user: str = Depends(get_current_user), repo=Depends(_repo)):
    try:
        return repo.timeline(user, room_id, after, limit)
    except RoomNotFound as exc:
        raise HTTPException(404, "Room not found") from exc


@router.post("/{room_id}/events")
def append_event(room_id: RoomId, body: AppendEvent, user: str = Depends(get_current_user),
                 repo=Depends(_repo)):
    try:
        if isinstance(body.event, ArtifactEvent):
            event = body.event
            return repo.revise_artifact(user, room_id, body.transaction_id, str(event.artifact_id),
                                        event.base_revision, event.kind, event.title, event.text)
        return repo.append(user, room_id, body.transaction_id, body.event.type,
                           body.event.model_dump(mode="json", exclude={"type"}))
    except RoomNotFound as exc:
        raise HTTPException(404, "Room not found") from exc
    except TransactionConflict as exc:
        raise HTTPException(409, "Transaction identifier already used") from exc
    except InvalidNotebook as exc:
        raise HTTPException(422, str(exc)) from exc
    except ArtifactConflict as exc:
        raise HTTPException(409, "Artifact revision conflict; refresh before editing") from exc


@router.get("/{room_id}/artifacts/{artifact_id}/revisions/{revision}")
def artifact_revision(room_id: RoomId, artifact_id: UUID, revision: int = Path(ge=1, le=2147483647),
                      user: str = Depends(get_current_user), repo=Depends(_repo)):
    try:
        result = repo.artifact_revision(user, room_id, str(artifact_id), revision)
        if result["kind"] == "notebook" and _execution_enabled():
            result["execution_enabled"] = True
        return result
    except RoomNotFound as exc:
        raise HTTPException(404, "Room or artifact revision not found") from exc


class NotebookRunRequest(ClosedModel):
    run_id: UUID
    artifact_id: UUID
    revision: Annotated[int, Field(strict=True, ge=1, le=2147483647)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    approved: Literal[True]


@router.post("/{room_id}/notebook-runs", dependencies=[Depends(_require_execution)])
def request_notebook_run(room_id: RoomId, body: NotebookRunRequest, user: str = Depends(get_current_user), repo=Depends(_repo)):
    try:
        return repo.request_notebook_run(user, room_id, str(body.run_id), str(body.artifact_id), body.revision, body.sha256)
    except RoomNotFound as exc:
        raise HTTPException(404, "Room or notebook not found") from exc
    except (RoomBusy, TransactionConflict, ArtifactConflict) as exc:
        raise HTTPException(409, "Notebook run conflict or another run active") from exc
    except InvalidNotebook as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{room_id}/notebook-runs/{run_id}")
def notebook_run(room_id: RoomId, run_id: UUID, user: str = Depends(get_current_user), repo=Depends(_repo)):
    try:
        return repo.notebook_run(user, room_id, str(run_id))
    except RoomNotFound as exc:
        raise HTTPException(404, "Room or run not found") from exc


@router.post("/{room_id}/notebook-runs/{run_id}/cancel")
def cancel_notebook_run(room_id: RoomId, run_id: UUID, user: str = Depends(get_current_user), repo=Depends(_repo)):
    try:
        return repo.cancel_notebook_run(user, room_id, str(run_id))
    except RoomNotFound as exc:
        raise HTTPException(404, "Room or run not found") from exc
    except RoomBusy as exc:
        raise HTTPException(409, "Run already started; worker timeout applies") from exc


class NotebookSync(ClosedModel):
    update: Annotated[str, StringConstraints(min_length=1, max_length=MAX_STATE_TEXT)] | None = None


@router.post("/{room_id}/artifacts/{artifact_id}/collaboration")
def sync_notebook(room_id: RoomId, artifact_id: UUID, body: NotebookSync,
                  user: str = Depends(get_current_user), repo=Depends(_repo)):
    try:
        return repo.sync_notebook(user, room_id, str(artifact_id), body.update)
    except RoomNotFound as exc:
        raise HTTPException(404, "Room or notebook not found") from exc
    except InvalidNotebook as exc:
        raise HTTPException(422, str(exc)) from exc
    except ArtifactConflict as exc:
        raise HTTPException(409, "Notebook collaboration conflict") from exc


@router.get("/{room_id}/participants")
def participants(room_id: RoomId, user: str = Depends(get_current_user), repo=Depends(_repo)):
    try:
        return {"participants": repo.participants(user, room_id)}
    except RoomNotFound as exc:
        raise HTTPException(404, "Room not found") from exc


class AgentTurn(ClosedModel):
    transaction_id: TransactionId
    agent_id: Name
    model: Name
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


@router.post("/{room_id}/turns")
async def agent_turn(room_id: RoomId, body: AgentTurn, user: str = Depends(get_current_user),
                     repo=Depends(_repo)):
    from atlas.application.chat.runs.launcher import LaunchRefused, resolve_model
    from atlas.application.chat.runs.room_turns import run_room_turn
    from atlas.infrastructure.app_factory import app_factory

    try:
        await resolve_model(app_factory.get_config_manager(), body.model, user)
        turn, context = await run_in_threadpool(
            repo.reserve_turn, user, room_id, body.transaction_id,
            body.agent_id, body.model, body.prompt,
        )
    except (RoomNotFound, AgentNotFound) as exc:
        raise HTTPException(404, "Room or roster entry not found") from exc
    except (RoomBusy, TransactionConflict) as exc:
        raise HTTPException(409, "Room busy or transaction conflict") from exc
    except LaunchRefused as exc:
        raise HTTPException(403, "Model unavailable") from exc
    if context is None:
        return turn
    reply = None
    try:
        reply = await asyncio.wait_for(run_room_turn(app_factory, user, turn, context, body.prompt), timeout=100)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Room turn failed", exc_info=False)
    finally:
        finished = await run_in_threadpool(repo.finish_turn, user, room_id, body.transaction_id, reply)
    return finished
