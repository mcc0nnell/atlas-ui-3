"""Durable Matrix-inspired rooms. PostgreSQL serializes each room's timeline."""

import json

from sqlalchemy import select

from atlas.core.user_identity import normalize_user_email

from .models import RoomEventRecord, RoomRecord


class RoomNotFound(Exception):
    """Absent or inaccessible room."""


class TransactionConflict(Exception):
    """A transaction identifier was reused with different content."""


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
