"""Opt-in operator-side prototype. Never import or expose as an Atlas server route.

Use a preapproved local image and a dedicated read-only dependency directory.
No arbitrary host-side notebook execution, credentials, or host socket mounts.
"""

import hashlib
import json
import re
import selectors
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import nbformat

from atlas.modules.chat_history.room_notebooks import notebook_cells


class NotebookRunError(RuntimeError):
    """An execution failed or could not be durably recorded."""


def run_isolated(text, image_id, dependency_dir, wall_seconds=75):
    notebook_cells(text)
    if len(text) > 64000 or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", image_id):
        raise NotebookRunError("Require a bounded checkpoint and an exact local image ID")
    if not 1 <= wall_seconds <= 120:
        raise NotebookRunError("Wall-clock limit outside supported range")
    deps = Path(dependency_dir).resolve(strict=True)
    if not deps.is_dir() or not (deps / "nbclient").is_dir() or not (deps / "talkpipe").is_dir():
        raise NotebookRunError("Require a dedicated notebook dependency directory")
    worker = Path(__file__).with_name("execute.py").resolve(strict=True)
    name = "atlas-notebook-" + uuid.uuid4().hex
    command = ["podman", "run", "--rm", "--pull=never", "--log-driver=none", "--name", name,
               "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
               "--pids-limit=64", "--memory=768m", "--memory-swap=768m", "--cpus=1", "--user=65534:65534",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m,mode=1777", "--workdir=/tmp", "-i",
               "-e", "HOME=/tmp", "-e", "PYTHONPATH=/opt/notebook-deps", "-e", "PYTHONDONTWRITEBYTECODE=1",
               "-e", "OPENBLAS_NUM_THREADS=1", "-e", "OMP_NUM_THREADS=1",
               "-v", f"{deps}:/opt/notebook-deps:ro", "-v", f"{worker}:/opt/execute.py:ro",
               image_id, "python", "/opt/execute.py"]
    # Refuse to run as root or on a host without cgroup v2 resource controls.
    info = subprocess.run(["podman", "info", "--format", "{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}}"],
                          capture_output=True, text=True, timeout=10, check=True)
    if info.stdout.strip() != "true v2":
        raise NotebookRunError("Rootless Podman with cgroup v2 is required")
    deadline = time.monotonic() + wall_seconds
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    proc = None
    try:
        with tempfile.TemporaryFile() as input_file, selectors.DefaultSelector() as selector:
            input_file.write(text.encode("utf-8"))
            input_file.seek(0)
            proc = subprocess.Popen(command, stdin=input_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
            selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NotebookRunError("Notebook execution timed out")
                for key, _ in selector.select(remaining):
                    chunk = key.fileobj.read1(8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = buffers[key.data]
                    buffer.extend(chunk)
                    if len(buffer) > 65536:
                        raise NotebookRunError("Notebook runner exceeded output budget")
            if proc.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
                raise NotebookRunError("Notebook execution failed; no result published")
        return json.loads(buffers["stdout"])
    except (subprocess.TimeoutExpired, ValueError) as exc:
        raise NotebookRunError("Notebook runner timed out or returned invalid data") from exc
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
            # --rm handles normal exit; explicitly clean up timed-out runs too.
            subprocess.run(["podman", "rm", "--force", "--ignore", name], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


def execute_checkpoint(client, room_id, artifact_id, revision, image_id, dependency_dir, *, wall_seconds=75):
    """Use the caller's already-authorized Atlas API client; return an unpublished result.

    A separate explicit publish operation prevents automatically replaying execution on retry.
    """
    room_id, artifact_id = str(uuid.UUID(room_id)), str(uuid.UUID(artifact_id))
    if type(revision) is not int or not 1 <= revision <= 2147483647:
        raise NotebookRunError("Invalid checkpoint revision")
    response = client.get(f"/api/rooms/{room_id}/artifacts/{artifact_id}/revisions/{revision}")
    response.raise_for_status()
    source = response.json()
    if source["kind"] != "notebook" or source["artifact_id"] != artifact_id or source["revision"] != revision:
        raise NotebookRunError("Not the requested notebook checkpoint")
    return prepare_result(source, image_id, dependency_dir, wall_seconds=wall_seconds)


def prepare_result(source, image_id, dependency_dir, *, wall_seconds=75, run_id=None):
    """Execute an already authorized, immutable checkpoint in the worker."""
    artifact_id, revision = source["artifact_id"], source["revision"]
    if hashlib.sha256(source["text"].encode()).hexdigest() != source["sha256"]:
        raise NotebookRunError("Checkpoint digest mismatch")
    original = nbformat.reads(source["text"], as_version=4)
    if not any(c.cell_type == "code" and c.source.strip() for c in original.cells):
        raise NotebookRunError("No executable cells")
    result = run_isolated(source["text"], image_id, dependency_dir, wall_seconds)
    executed = nbformat.from_dict(result["notebook"])
    nbformat.validate(executed)
    # Keep the original inputs, IDs and metadata. Copy only standard execution outputs.
    if len(original.cells) != len(executed.cells):
        raise NotebookRunError("Notebook structure changed during execution")
    for cell, output in zip(original.cells, executed.cells):
        if (cell.id, cell.cell_type, cell.source) != (output.id, output.cell_type, output.source):
            raise NotebookRunError("Notebook inputs changed during execution")
        if cell.cell_type == "code":
            if cell.source.strip() and not isinstance(output.execution_count, int):
                raise NotebookRunError("Missing execution count")
            if any(o.output_type == "error" for o in output.outputs):
                raise NotebookRunError("Notebook returned an error")
            cell.outputs, cell.execution_count = output.outputs, output.execution_count
    run_id = run_id or str(uuid.uuid4())
    original.metadata.kernelspec = {"display_name": "Python 3", "language": "python", "name": "python3"}
    original.metadata.atlas_execution = {"run_id": run_id, "source_artifact_id": artifact_id,
        "source_revision": revision, "source_sha256": source["sha256"], "runtime_image_id": image_id,
        "versions": result["versions"], "mode": "operator-reported-isolated-notebook"}
    output_text = json.dumps(original, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(output_text) > 64000:
        raise NotebookRunError("Result exceeds Atlas artifact size limit")
    notebook_cells(output_text)
    return {"transaction_id": run_id, "event": {"type": "room.artifact.revised", "artifact_id": run_id,
            "base_revision": 0, "kind": "notebook", "title": (source["title"] + " — executed")[:200], "text": output_text}}


def publish_result(client, room_id, result):
    """Publish/retry the exact prepared result without executing the notebook again."""
    response = client.post(f"/api/rooms/{uuid.UUID(room_id)}/events", json=result)
    response.raise_for_status()
    return response.json()
