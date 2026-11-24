"""Explicitly started, single-room, one-shot notebook worker. No HTTP execution service."""

import argparse
import os
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from atlas.modules.chat_history.room_repository import RoomRepository
from scripts.notebook_runner.bridge import prepare_result


def work_once(repo, owner, room_id, image, dependencies, run_id=None, wall_seconds=75):
    claim = repo.claim_notebook_run(owner, room_id, run_id)
    if claim is None:
        return None
    try:
        result = prepare_result(claim["source"], image, dependencies, wall_seconds=wall_seconds, run_id=claim["run_id"])
        return repo.finish_notebook_run(owner, room_id, claim["run_id"], result["event"])
    except Exception:
        # No raw notebook text or credentials are exposed through error events.
        return repo.finish_notebook_run(owner, room_id, claim["run_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--room", type=uuid.UUID, required=True)
    parser.add_argument("--run", type=uuid.UUID, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--dependencies", required=True)
    args = parser.parse_args()
    engine = create_engine(os.environ["ATLAS_NOTEBOOK_WORKER_DATABASE_URL"])
    try:
        result = work_once(RoomRepository(sessionmaker(bind=engine)), args.owner, str(args.room), args.image,
                           args.dependencies, str(args.run))
        print(result["state"] if result else "No queued request")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
