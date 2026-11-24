"""Validate standard notebooks without executing or rendering their contents."""

import base64
import binascii
import json
import re

import nbformat
from jupyter_ydoc import YNotebook


class InvalidNotebook(ValueError):
    """The artifact is not a supported notebook or cell reference."""


def _reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def notebook_cells(text):
    """Require nbformat 4.5+ and stable unique cell identifiers."""
    try:
        data = json.loads(text, parse_constant=_reject_constant)
        if not isinstance(data, dict) or data.get("nbformat") != 4 or not isinstance(data.get("nbformat_minor"), int) or data["nbformat_minor"] < 5:
            raise ValueError("Notebook must use nbformat 4.5 or later")
        cells = data.get("cells")
        if not isinstance(cells, list):
            raise ValueError("Notebook cells must be a list")
        ids = set()
        for cell in cells:
            cell_id = cell.get("id") if isinstance(cell, dict) else None
            if not isinstance(cell_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cell_id) or cell_id in ids:
                raise ValueError("Notebook cells require unique valid IDs")
            ids.add(cell_id)
        nbformat.validate(data)
        return ids
    except (ValueError, TypeError, KeyError, RecursionError, nbformat.ValidationError) as exc:
        raise InvalidNotebook("Invalid notebook; expected nbformat 4.5+ with unique cell IDs") from exc


MAX_STATE_BYTES = 262144
MAX_STATE_TEXT = ((MAX_STATE_BYTES + 2) // 3) * 4


def _decode_update(value):
    try:
        if len(value) > MAX_STATE_TEXT:
            raise ValueError("Update too large")
        result = base64.b64decode(value, validate=True)
        if not result or len(result) > MAX_STATE_BYTES:
            raise ValueError("Invalid update size")
        return result
    except (ValueError, binascii.Error) as exc:
        raise InvalidNotebook("Invalid or oversized collaboration update") from exc


def merge_notebook_state(text, state=None, update=None):
    """Merge native Yjs updates, returning a bounded snapshot and validated notebook."""
    doc = YNotebook()
    try:
        if state is None:
            doc.set(json.loads(text))
        else:
            doc.ydoc.apply_update(_decode_update(state))
        before = doc.get(deduplicate=False)
        if update is not None:
            doc.ydoc.apply_update(_decode_update(update))
        if set(doc.ydoc.keys()) != {"cells", "meta", "state"}:
            raise InvalidNotebook("Unexpected collaboration document structure")
        after = doc.get(deduplicate=False)
        merged = json.dumps(after, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)
        if len(merged) > 64000:
            raise InvalidNotebook("Notebook exceeds artifact size limit")
        notebook_cells(merged)
        raw = doc.ydoc.get_update()
        if len(raw) > MAX_STATE_BYTES:
            raise InvalidNotebook("Collaboration state exceeds size limit")
        return base64.b64encode(raw).decode("ascii"), merged if before != after else None
    except InvalidNotebook as exc:
        error = str(exc)
    except (ValueError, TypeError, KeyError, RuntimeError, OverflowError, RecursionError, AssertionError):
        error = "Invalid collaboration update"
    finally:
        # PyCRDT's native undo manager is thread-affine. Do not retain the
        # document in an exception traceback crossing the request worker.
        doc.unobserve()
        del doc
    raise InvalidNotebook(error)
