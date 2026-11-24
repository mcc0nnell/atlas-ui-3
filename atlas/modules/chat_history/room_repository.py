"""Durable Matrix-inspired rooms. PostgreSQL serializes each room's timeline."""

import json
from datetime import timedelta

from sqlalchemy import func, select, update

from atlas.core.user_identity import normalize_user_email

from .models import RoomEventRecord, RoomRecord, RoomTurnRecord


class RoomNotFound(Exception):
    """Absent or inaccessible room."""


class TransactionConflict(Exception):
    """A transaction identifier was reused with different content."""


class RoomBusy(Exception):
    """A room turn is already reserved."""


class AgentNotFound(Exception):
    """Agent is not on the room's current roster."""


class RoomRepository:
    """Owner-scoped rooms; roster entries do not confer execution authority."""

    def __init__(self, session_factory):
        if session_factory.kw["bind"].dialect.name != "postgresql":
            raise ValueError("Collaboration rooms require PostgreSQL")
        self._session_factory = session_factory

    @staticmethod
    def _owner(user):
        owner = normalize_user_email(user)
        if not owner or len(owner) > 255:
            raise RoomNotFound()
        return owner

    @staticmethod
    def _room(record):
        return {"id": record.id, "name": record.name,
                "last_sequence": record.last_sequence,
                "created_at": record.created_at.isoformat()}

    @staticmethod
    def _event(record):
        return {"sequence": record.sequence, "sender": record.sender,
                "type": record.event_type, "content": json.loads(record.content_json),
                "transaction_id": record.transaction_id,
                "created_at": record.created_at.isoformat()}

    def create(self, user, name):
        with self._session_factory.begin() as session:
            record = RoomRecord(owner=self._owner(user), name=name)
            session.add(record)
            session.flush()
            return self._room(record)

    def list(self, user):
        with self._session_factory() as session:
            rows = session.scalars(select(RoomRecord).where(
                RoomRecord.owner == self._owner(user)
            ).order_by(RoomRecord.created_at.desc(), RoomRecord.id)).all()
            return [self._room(row) for row in rows]

    def _find(self, session, user, room_id, lock=False):
        query = select(RoomRecord).where(
            RoomRecord.id == room_id, RoomRecord.owner == self._owner(user)
        )
        if lock:
            query = query.with_for_update()
        room = session.scalar(query)
        if room is None:
            raise RoomNotFound()
        return room

    def append(self, user, room_id, transaction_id, event_type, content):
        # Internal reply identifiers and event types are never supplied by the client.
        if transaction_id.startswith("agent-turn:"):
            raise TransactionConflict()
        # The owner and the sender are derived from authentication, not the payload.
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
        with self._session_factory.begin() as session:
            room = self._find(session, user, room_id, lock=True)
            existing = session.scalar(select(RoomEventRecord).where(
                RoomEventRecord.room_id == room_id,
                RoomEventRecord.transaction_id == transaction_id,
            ))
            if existing is not None:
                if existing.event_type != event_type or existing.content_json != encoded:
                    raise TransactionConflict()
                return self._event(existing)
            room.last_sequence += 1
            event = RoomEventRecord(
                room_id=room_id, sequence=room.last_sequence,
                transaction_id=transaction_id, sender=self._owner(user),
                event_type=event_type, content_json=encoded,
            )
            session.add(event)
            session.flush()
            return self._event(event)

    def timeline(self, user, room_id, after=0, limit=100):
        with self._session_factory() as session:
            self._find(session, user, room_id)
            rows = session.scalars(select(RoomEventRecord).where(
                RoomEventRecord.room_id == room_id, RoomEventRecord.sequence > after,
            ).order_by(RoomEventRecord.sequence).limit(limit + 1)).all()
            events = [self._event(row) for row in rows[:limit]]
            return {"events": events, "next_after": events[-1]["sequence"] if events else after,
                    "has_more": len(rows) > limit}


    @staticmethod
    def _turn(record):
        return {"transaction_id": record.transaction_id, "agent_id": record.agent_id,
                "model": record.model, "state": record.state, "reply": record.reply,
                "conversation_id": record.conversation_id}

    def reserve_turn(self, user, room_id, transaction_id, agent_id, model, prompt):
        """Reserve before model invocation; retries do not launch a second turn."""
        with self._session_factory.begin() as session:
            self._find(session, user, room_id, lock=True)
            # A process killed mid-turn cannot mark its reservation failed.
            # Expire only on a later human request, after the server timeout
            # plus grace. Never automatically replay the abandoned invocation.
            session.execute(update(RoomTurnRecord).where(
                RoomTurnRecord.room_id == room_id,
                RoomTurnRecord.state == "pending",
                RoomTurnRecord.created_at < func.now() - timedelta(seconds=180),
            ).values(state="failed"))
            existing = session.get(RoomTurnRecord, (room_id, transaction_id))
            if existing is not None:
                if (existing.agent_id, existing.model, existing.prompt) != (agent_id, model, prompt):
                    raise TransactionConflict()
                return self._turn(existing), None
            pending = session.scalar(select(RoomTurnRecord).where(
                RoomTurnRecord.room_id == room_id, RoomTurnRecord.state == "pending"
            ).limit(1))
            if pending is not None:
                raise RoomBusy()
            encoded = json.dumps({"agent_id": agent_id}, sort_keys=True, separators=(",", ":"))
            latest = session.scalar(select(RoomEventRecord).where(
                RoomEventRecord.room_id == room_id,
                RoomEventRecord.event_type.in_(["room.agent.added", "room.agent.removed"]),
                RoomEventRecord.content_json == encoded,
            ).order_by(RoomEventRecord.sequence.desc()).limit(1))
            if latest is None or latest.event_type != "room.agent.added":
                raise AgentNotFound()
            rows = session.scalars(select(RoomEventRecord).where(
                RoomEventRecord.room_id == room_id,
                RoomEventRecord.event_type.in_(["room.message", "room.agent.reply"]),
            ).order_by(RoomEventRecord.sequence.desc()).limit(20)).all()
            # Bound input and treat previous room messages as quoted context.
            context = json.dumps([self._event(row) for row in reversed(rows)], ensure_ascii=False)
            record = RoomTurnRecord(room_id=room_id, transaction_id=transaction_id,
                                    agent_id=agent_id, model=model, prompt=prompt)
            session.add(record)
            session.flush()
            return self._turn(record), context[-12000:]

    def finish_turn(self, user, room_id, transaction_id, reply=None):
        """Commit status and the server-authored reply atomically."""
        with self._session_factory.begin() as session:
            room = self._find(session, user, room_id, lock=True)
            record = session.get(RoomTurnRecord, (room_id, transaction_id))
            if record is None:
                raise RoomNotFound()
            if record.state != "pending":
                return self._turn(record)
            record.state = "completed" if reply else "failed"
            record.reply = reply
            if reply:
                room.last_sequence += 1
                event = RoomEventRecord(
                    room_id=room_id, sequence=room.last_sequence,
                    transaction_id="agent-turn:" + record.conversation_id,
                    sender=self._owner(user), event_type="room.agent.reply",
                    content_json=json.dumps({"agent_id": record.agent_id, "model": record.model,
                                             "text": reply, "conversation_id": record.conversation_id}),
                )
                session.add(event)
            session.flush()
            return self._turn(record)
