"""Opt-in room API: authenticated owner, immutable events, cursor pagination."""

from typing import Annotated, Literal, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints

from atlas.core.log_sanitizer import get_current_user
from atlas.modules.chat_history.room_repository import RoomNotFound, TransactionConflict

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


class AgentEvent(ClosedModel):
    type: Literal["room.agent.added", "room.agent.removed"]
    agent_id: Name


class AppendEvent(ClosedModel):
    transaction_id: TransactionId
    event: Annotated[Union[MessageEvent, AgentEvent], Field(discriminator="type")]


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
        return repo.append(user, room_id, body.transaction_id, body.event.type,
                           body.event.model_dump(exclude={"type"}))
    except RoomNotFound as exc:
        raise HTTPException(404, "Room not found") from exc
    except TransactionConflict as exc:
        raise HTTPException(409, "Transaction identifier already used") from exc
