"""Durable Matrix-inspired rooms. PostgreSQL serializes each room's timeline."""

import hashlib
import json
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from atlas.core.user_identity import normalize_user_email

from .models import RoomArtifactRevisionRecord, RoomEventRecord, RoomParticipantTurnRecord, RoomRecord, RoomTurnRecord


class RoomNotFound(Exception):
    """Absent or inaccessible room."""


class TransactionConflict(Exception):
    """A transaction identifier was reused with different content."""


class ArtifactConflict(Exception):
    """The proposed revision does not match the current artifact head."""


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
        if transaction_id.startswith("agent-turn:") or event_type == "room.artifact.revised":
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

    def revise_artifact(self, user, room_id, transaction_id, artifact_id, base_revision, kind, title, text):
        """Commit a compare-and-set revision and its reference event atomically."""
        if transaction_id.startswith("agent-turn:"):
            raise TransactionConflict()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        content = {
            "artifact_id": artifact_id, "revision": base_revision + 1,
            "kind": kind, "title": title, "sha256": digest,
        }
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
        with self._session_factory.begin() as session:
            room = self._find(session, user, room_id, lock=True)
            existing = session.scalar(select(RoomEventRecord).where(
                RoomEventRecord.room_id == room_id,
                RoomEventRecord.transaction_id == transaction_id,
            ))
            if existing is not None:
                if existing.event_type != "room.artifact.revised" or existing.content_json != encoded:
                    raise TransactionConflict()
                return self._event(existing)
            head = session.scalar(select(RoomArtifactRevisionRecord).where(
                RoomArtifactRevisionRecord.room_id == room_id,
                RoomArtifactRevisionRecord.artifact_id == artifact_id,
            ).order_by(RoomArtifactRevisionRecord.revision.desc()).limit(1))
            if (head.revision if head else 0) != base_revision or (head and head.kind != kind):
                raise ArtifactConflict()
            room.last_sequence += 1
            session.add(RoomArtifactRevisionRecord(
                room_id=room_id, artifact_id=artifact_id, revision=base_revision + 1,
                kind=kind, title=title, text=text, sequence=room.last_sequence, digest=digest,
            ))
            event = RoomEventRecord(
                room_id=room_id, sequence=room.last_sequence, transaction_id=transaction_id,
                sender=self._owner(user), event_type="room.artifact.revised", content_json=encoded,
            )
            session.add(event)
            session.flush()
            return self._event(event)

    def artifact_revision(self, user, room_id, artifact_id, revision):
        """Read one exact immutable revision, only within the owner's room."""
        with self._session_factory() as session:
            self._find(session, user, room_id)
            record = session.get(RoomArtifactRevisionRecord, (room_id, artifact_id, revision))
            if record is None:
                raise RoomNotFound()
            return {
                "artifact_id": record.artifact_id, "revision": record.revision,
                "kind": record.kind, "title": record.title, "text": record.text,
                "sha256": record.digest, "sequence": record.sequence,
            }

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
            membership = self._membership(session, room_id, encoded)
            if membership is None:
                raise AgentNotFound()
            history = self._participant_history(session, room_id, membership)
            rows = session.scalars(select(RoomEventRecord).where(
                RoomEventRecord.room_id == room_id,
                RoomEventRecord.event_type.in_(["room.message", "room.agent.reply"]),
            ).order_by(RoomEventRecord.sequence.desc()).limit(20)).all()
            context = self._bounded_context(
                [self._event(row) for row in reversed(rows)], 10000)
            record = RoomTurnRecord(room_id=room_id, transaction_id=transaction_id,
                                    agent_id=agent_id, model=model, prompt=prompt)
            session.add(record)
            session.add(RoomParticipantTurnRecord(
                room_id=room_id, transaction_id=transaction_id,
                member_sequence=membership,
            ))
            session.flush()
            return self._turn(record), {
                "room": context, "participant": history,
                "participant_id": self._participant_id(room_id, membership),
            }

    @staticmethod
    def _participant_id(room_id, membership):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"atlas-room:{room_id}:{membership}"))

    @staticmethod
    def _membership(session, room_id, encoded):
        """Repeated adds are idempotent membership; a removal starts a new identity."""
        rows = session.execute(select(RoomEventRecord.sequence, RoomEventRecord.event_type).where(
            RoomEventRecord.room_id == room_id,
            RoomEventRecord.event_type.in_(["room.agent.added", "room.agent.removed"]),
            RoomEventRecord.content_json == encoded,
        ).order_by(RoomEventRecord.sequence.desc()))
        membership = None
        for sequence, event_type in rows:
            if event_type == "room.agent.removed":
                break
            membership = sequence
        return membership

    @staticmethod
    def _bounded_context(items, budget):
        """Keep complete JSON records; prefer the newest, without broken JSON."""
        result = []
        for item in reversed(items):
            candidate = [item, *result]
            if len(json.dumps(candidate, ensure_ascii=False)) > budget:
                # Skip oversized individual records instead of losing all history.
                continue
            result = candidate
        return result

    def _participant_history(self, session, room_id, membership):
        rows = session.scalars(select(RoomTurnRecord).join(
            RoomParticipantTurnRecord,
            (RoomParticipantTurnRecord.room_id == RoomTurnRecord.room_id)
            & (RoomParticipantTurnRecord.transaction_id == RoomTurnRecord.transaction_id),
        ).join(
            RoomEventRecord,
            (RoomEventRecord.room_id == RoomTurnRecord.room_id)
            & (RoomEventRecord.transaction_id == "agent-turn:" + RoomTurnRecord.conversation_id),
        ).where(
            RoomTurnRecord.room_id == room_id,
            RoomParticipantTurnRecord.member_sequence == membership,
            RoomTurnRecord.state == "completed",
        ).order_by(RoomEventRecord.sequence.desc()).limit(12)).all()
        return self._bounded_context([
            {"prompt": row.prompt[-2000:], "reply": (row.reply or "")[-4000:], "model": row.model}
            for row in reversed(rows)
        ], 8000)

    def participants(self, user, room_id):
        """Current identities and saved-turn counts; no private prompt contents."""
        with self._session_factory() as session:
            self._find(session, user, room_id, lock=True)
            rows = session.scalars(select(RoomEventRecord).where(
                RoomEventRecord.room_id == room_id,
                RoomEventRecord.event_type.in_(["room.agent.added", "room.agent.removed"]),
            ).order_by(RoomEventRecord.sequence)).all()
            active = {}
            for row in rows:
                name = json.loads(row.content_json)["agent_id"]
                if row.event_type == "room.agent.added":
                    active.setdefault(name, row.sequence)
                else:
                    active.pop(name, None)
            result = []
            for name, membership in sorted(active.items()):
                history = self._participant_history(session, room_id, membership)
                result.append({"agent_id": name, "participant_id": self._participant_id(room_id, membership),
                               "context_turns": len(history),
                               "last_model": history[-1]["model"] if history else None})
            return result

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
                binding = session.get(RoomParticipantTurnRecord, (room_id, transaction_id))
                identity = self._participant_id(room_id, binding.member_sequence) if binding else None
                event = RoomEventRecord(
                    room_id=room_id, sequence=room.last_sequence,
                    transaction_id="agent-turn:" + record.conversation_id,
                    sender=self._owner(user), event_type="room.agent.reply",
                    content_json=json.dumps({"agent_id": record.agent_id, "model": record.model,
                                             "text": reply, "conversation_id": record.conversation_id,
                                             "participant_id": identity}),
                )
                session.add(event)
            session.flush()
            return self._turn(record)
