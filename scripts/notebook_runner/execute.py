"""Container entry point. Jupyter owns kernel execution and output capture."""

import importlib.metadata
import json
import sys

import nbformat
from nbclient import NotebookClient


def main():
    notebook = nbformat.reads(sys.stdin.read(64001), as_version=4)
    nbformat.validate(notebook)
    for cell in notebook.cells:
        if cell.cell_type == "code":
            cell.outputs = []
            cell.execution_count = None
    # Ignore notebook-supplied kernel name; use only the installed local Python kernel.
    notebook.metadata.kernelspec = {"display_name": "Python 3", "language": "python", "name": "python3"}
    executed = NotebookClient(notebook, timeout=20, startup_timeout=20, kernel_name="python3",
                              allow_errors=False, resources={"metadata": {"path": "/tmp"}}).execute()
    nbformat.validate(executed)
    versions = {name: importlib.metadata.version(name) for name in ("nbclient", "nbformat", "ipykernel", "talkpipe")}
    payload = json.dumps({"notebook": executed, "versions": versions}, ensure_ascii=True, allow_nan=False)
    if len(payload) > 64000:
        raise ValueError("Executed notebook exceeds output budget")
    print(payload)


if __name__ == "__main__":
    main()
