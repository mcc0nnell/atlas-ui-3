"""Durable Matrix-inspired rooms. PostgreSQL serializes each room's timeline."""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from atlas.core.user_identity import normalize_user_email

from .models import (
    RoomArtifactRevisionRecord,
    RoomEventRecord,
    RoomNotebookRunRecord,
    RoomNotebookStateRecord,
    RoomParticipantTurnRecord,
    RoomRecord,
    RoomTurnRecord,
)
from .room_notebooks import InvalidNotebook, merge_notebook_state, notebook_cells


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
        if transaction_id.startswith(("agent-turn:", "notebook-sync:", "notebook-run:")) or event_type == "room.artifact.revised":
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
            if event_type == "room.notebook.comment":
                record = session.get(RoomArtifactRevisionRecord, (room_id, content["artifact_id"], content["revision"]))
                if record is None or record.kind != "notebook":
                    raise RoomNotFound()
                if content["cell_id"] not in notebook_cells(record.text):
                    raise InvalidNotebook("Cell does not exist in this notebook revision")
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
        if transaction_id.startswith(("agent-turn:", "notebook-sync:", "notebook-run:")):
            raise TransactionConflict()
        if kind == "notebook":
            notebook_cells(text)
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
            if head and kind == "notebook" and session.get(RoomNotebookStateRecord, (room_id, artifact_id)) is not None:
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

    def sync_notebook(self, user, room_id, artifact_id, update=None):
        """Serialize YDoc merges across replicas, atomically checkpoint visible edits."""
        with self._session_factory.begin() as session:
            room = self._find(session, user, room_id, lock=True)
            head = session.scalar(select(RoomArtifactRevisionRecord).where(
                RoomArtifactRevisionRecord.room_id == room_id,
                RoomArtifactRevisionRecord.artifact_id == artifact_id,
            ).order_by(RoomArtifactRevisionRecord.revision.desc()).limit(1))
            if head is None or head.kind != "notebook":
                raise RoomNotFound()
            record = session.get(RoomNotebookStateRecord, (room_id, artifact_id))
            if record is not None and record.revision != head.revision:
                raise ArtifactConflict()
            if record is not None and update is None:
                # Polls must return the persisted snapshot, not re-encode an
                # equivalent YDoc whose binary serialization can vary.
                return {"state": record.state, "revision": record.revision,
                        "title": head.title, "text": head.text}
            state, changed = merge_notebook_state(head.text, record.state if record else None, update)
            if record is None:
                record = RoomNotebookStateRecord(room_id=room_id, artifact_id=artifact_id, revision=head.revision, state=state)
                session.add(record)
            elif record.state != state:
                record.state = state
            if changed is not None:
                revision = head.revision + 1
                if revision > 2147483647:
                    raise ArtifactConflict()
                digest = hashlib.sha256(changed.encode("utf-8")).hexdigest()
                room.last_sequence += 1
                content = {"artifact_id": artifact_id, "revision": revision, "kind": "notebook", "title": head.title, "sha256": digest}
                session.add(RoomArtifactRevisionRecord(room_id=room_id, artifact_id=artifact_id, revision=revision,
                    kind="notebook", title=head.title, text=changed, sequence=room.last_sequence, digest=digest))
                session.add(RoomEventRecord(room_id=room_id, sequence=room.last_sequence,
                    transaction_id=f"notebook-sync:{uuid.uuid4()}", sender=self._owner(user),
                    event_type="room.artifact.revised", content_json=json.dumps(content, sort_keys=True, separators=(",", ":"))))
                record.revision = revision
            session.flush()
            return {"state": state, "revision": record.revision, "title": head.title, "text": changed if changed is not None else head.text}

    @staticmethod
    def _notebook_run(record):
        return {"run_id": record.run_id, "artifact_id": record.artifact_id,
                "revision": record.revision, "source_sha256": record.source_sha256,
                "state": record.state, "result_artifact_id": record.result_artifact_id}

    def _run_event(self, session, room, record):
        room.last_sequence += 1
        session.add(RoomEventRecord(room_id=room.id, sequence=room.last_sequence,
            transaction_id=f"notebook-run:{record.run_id}:{record.state}", sender=room.owner,
            event_type="room.notebook.run", content_json=json.dumps(self._notebook_run(record), sort_keys=True)))

    def request_notebook_run(self, user, room_id, run_id, artifact_id, revision, digest):
        with self._session_factory.begin() as session:
            room = self._find(session, user, room_id, lock=True)
            previous = session.get(RoomNotebookRunRecord, (room_id, run_id))
            if previous is not None:
                if (previous.artifact_id, previous.revision, previous.source_sha256) != (artifact_id, revision, digest):
                    raise TransactionConflict()
                return self._notebook_run(previous)
            source = session.get(RoomArtifactRevisionRecord, (room_id, artifact_id, revision))
            if source is None or source.kind != "notebook":
                raise RoomNotFound()
            if source.digest != digest:
                raise ArtifactConflict()
            data = json.loads(source.text)
            if not any(c["cell_type"] == "code" and ("".join(c["source"]) if isinstance(c["source"], list) else c["source"]).strip() for c in data["cells"]):
                raise InvalidNotebook("Notebook has no executable cells")
            active = session.scalar(select(RoomNotebookRunRecord.run_id).where(
                RoomNotebookRunRecord.room_id == room_id, RoomNotebookRunRecord.state.in_(["queued", "running"])).limit(1))
            if active:
                raise RoomBusy()
            record = RoomNotebookRunRecord(room_id=room_id, run_id=run_id, artifact_id=artifact_id,
                revision=revision, source_sha256=digest, state="queued")
            session.add(record)
            self._run_event(session, room, record)
            session.flush()
            return self._notebook_run(record)

    def notebook_run(self, user, room_id, run_id):
        with self._session_factory() as session:
            self._find(session, user, room_id)
            record = session.get(RoomNotebookRunRecord, (room_id, run_id))
            if record is None:
                raise RoomNotFound()
            return self._notebook_run(record)

    def cancel_notebook_run(self, user, room_id, run_id):
        with self._session_factory.begin() as session:
            room = self._find(session, user, room_id, lock=True)
            record = session.get(RoomNotebookRunRecord, (room_id, run_id))
            if record is None:
                raise RoomNotFound()
            if record.state == "queued":
                record.state = "cancelled"
                record.finished_at = datetime.now(timezone.utc)
                self._run_event(session, room, record)
            elif record.state == "running":
                raise RoomBusy()
            return self._notebook_run(record)

    def claim_notebook_run(self, user, room_id, run_id=None):
        """Trusted worker only: claim once; never automatically replay running jobs."""
        with self._session_factory.begin() as session:
            room = self._find(session, user, room_id, lock=True)
            query = select(RoomNotebookRunRecord).where(RoomNotebookRunRecord.room_id == room_id,
                RoomNotebookRunRecord.state == "queued")
            if run_id is not None:
                query = query.where(RoomNotebookRunRecord.run_id == run_id)
            record = session.scalar(query.order_by(RoomNotebookRunRecord.created_at).limit(1))
            if record is None:
                return None
            source = session.get(RoomArtifactRevisionRecord, (room_id, record.artifact_id, record.revision))
            if source is None or source.kind != "notebook" or source.digest != record.source_sha256:
                raise ArtifactConflict()
            record.state = "running"
            record.started_at = datetime.now(timezone.utc)
            self._run_event(session, room, record)
            return {**self._notebook_run(record), "source": {"artifact_id": source.artifact_id,
                "revision": source.revision, "kind": source.kind, "title": source.title,
                "text": source.text, "sha256": source.digest}}

    def finish_notebook_run(self, user, room_id, run_id, result=None):
        """Trusted worker only: commit result, run status and timeline atomically."""
        with self._session_factory.begin() as session:
            room = self._find(session, user, room_id, lock=True)
            record = session.get(RoomNotebookRunRecord, (room_id, run_id))
            if record is None:
                raise RoomNotFound()
            if record.state != "running":
                return self._notebook_run(record)
            if result is not None:
                text = result["text"]
                notebook_cells(text)
                if len(text) > 64000:
                    raise InvalidNotebook("Result too large")
                meta = json.loads(text)["metadata"].get("atlas_execution", {})
                if (meta.get("run_id"), meta.get("source_artifact_id"), meta.get("source_revision"), meta.get("source_sha256")) != (run_id, record.artifact_id, record.revision, record.source_sha256):
                    raise ArtifactConflict()
                result_id = str(uuid.uuid4())
                digest = hashlib.sha256(text.encode()).hexdigest()
                title = result["title"][:200]
                room.last_sequence += 1
                session.add(RoomArtifactRevisionRecord(room_id=room_id, artifact_id=result_id, revision=1,
                    kind="notebook", title=title, text=text, sequence=room.last_sequence, digest=digest))
                session.add(RoomEventRecord(room_id=room_id, sequence=room.last_sequence,
                    transaction_id=f"notebook-run:{run_id}:result", sender=room.owner, event_type="room.artifact.revised",
                    content_json=json.dumps({"artifact_id": result_id, "revision": 1, "kind": "notebook", "title": title, "sha256": digest}, sort_keys=True)))
                record.result_artifact_id = result_id
                record.state = "completed"
            else:
                record.state = "failed"
            record.finished_at = datetime.now(timezone.utc)
            self._run_event(session, room, record)
            session.flush()
            return self._notebook_run(record)

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
