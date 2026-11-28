"""colab-web: the student runs LabPilot's notebook in their own Colab and gives the downloaded .ipynb back.

The "student run" here is a FAKE: the test fills outputs into the handoff notebook itself, the way a Colab
download looks. Real Colab is checked by hand (docs/IMPLEMENTATION_STATUS.md).
"""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import nbformat
import pytest
from PIL import Image

from conftest import load_example
from labpilot.models import ItemStatus, RunState
from labpilot.orchestration import init_run, inspect_run, save_plan
from labpilot.report import finish
from labpilot.workspace import Run, read_json


@pytest.fixture(autouse=True)
def colab_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LABPILOT_ALLOW_LOCAL", raising=False)


def _lab_run(tmp_path: Path) -> tuple[Run, Path]:
    folder = tmp_path / "lab"
    manual = load_example("python", "make_lab").make(folder)
    agent = load_example("python", "agent_files")
    run = init_run(manual)
    inspect_run(run)
    (folder / "plan.json").write_text(json.dumps(agent.make_plan(read_json(run.path("draft_plan.json")))), encoding="utf-8")
    save_plan(run, folder / "plan.json")
    (folder / "cells.json").write_text(json.dumps(agent.CELLS), encoding="utf-8")
    return run, folder


def _fake_student_run(handoff: Path, dest: Path, outputs: Callable[[Any], list[dict[str, Any]] | None]) -> Path:
    """FAKE Colab "Run all" + download: `outputs(cell)` gives a cell's outputs, None = not run."""
    nb = nbformat.read(str(handoff), as_version=4)
    count = 0
    for c in nb.cells:
        if c.cell_type != "code" or c.metadata.get("labpilot", {}).get("control"):
            continue
        outs = outputs(c)
        if outs is None:
            continue
        count += 1
        c.execution_count = count
        c.outputs = [nbformat.from_dict(o) for o in outs]
    nbformat.write(nb, str(dest))
    return dest


def _ok(cell: Any) -> list[dict[str, Any]]:
    outs: list[dict[str, Any]] = [
        {"output_type": "stream", "name": "stdout", "text": f"output of {cell.metadata['labpilot']['cell_id']}\n"}]
    if re.search(r"plt\.|\.plot\(|\.hist\(|\.bar\(|sns\.", cell.source):  # a chart cell shows a (fake) figure
        buf = io.BytesIO()
        Image.new("RGB", (200, 120), "steelblue").save(buf, format="PNG")
        outs.append({"output_type": "display_data", "metadata": {},
                     "data": {"image/png": base64.b64encode(buf.getvalue()).decode()}})
    return outs


def test_handoff_waits_for_the_student_then_reads_their_run(tmp_path: Path) -> None:
    run, folder = _lab_run(tmp_path)
    res = finish(run, cells_file=folder / "cells.json", backend="colab-web")
    assert res["next_action"]["action"] == "consent"  # never without the user's yes

    res = finish(run, backend="colab-web", allow_cloud=True, allow_upload=True, gpu="T4")
    assert res["next_action"] == {"actor": "user", "action": "run-in-colab", "detail": res["next_action"]["detail"]}
    handoff = run.path("for_colab.ipynb")
    nb = nbformat.read(str(handoff), as_version=4)
    assert nb.metadata["accelerator"] == "GPU" and nb.metadata["colab"]["gpuType"] == "T4"
    upload = nb.cells[1]
    assert upload.metadata["labpilot"]["control"] and "files.upload()" in upload.source
    lab_ids = [c.metadata["labpilot"]["cell_id"] for c in nb.cells[2:]]
    assert lab_ids == list(run.manifest.cell_code_sha256)

    # asking again before the run hands out the same notebook, it does not rebuild anything
    again = finish(run)
    assert again["next_action"]["action"] == "run-in-colab" and run.manifest.state == RunState.EXECUTING

    done = _fake_student_run(handoff, tmp_path / "downloaded.ipynb", _ok)
    res = finish(run, executed_file=done)
    steps = {s["step"]: s for s in res["steps"]}
    assert steps["record-colab-run"]["result"] == "OK" and steps["capture"]["status"] == "done", steps
    assert res["next_action"]["action"] == "answers"
    assert all(s == ItemStatus.PASSED for s in run.manifest.cell_status.values())
    assert run.manifest.attempts[-1]["backend"] == "Google Colab (browser, student-run)"


def test_failed_cell_needs_repair_and_later_cells_are_not_passed(tmp_path: Path) -> None:
    run, folder = _lab_run(tmp_path)
    finish(run, cells_file=folder / "cells.json", backend="colab-web", allow_cloud=True, allow_upload=True)
    ids = list(run.manifest.cell_code_sha256)
    bad = ids[1]

    def outs(cell: Any) -> list[dict[str, Any]] | None:
        cid = cell.metadata["labpilot"]["cell_id"]
        if cid == bad:
            return [{"output_type": "error", "ename": "KeyError", "evalue": "'price'", "traceback": ["KeyError: 'price'"]}]
        return None if ids.index(cid) > ids.index(bad) else _ok(cell)

    res = finish(run, executed_file=_fake_student_run(run.path("for_colab.ipynb"), tmp_path / "d.ipynb", outs))
    rec = next(s for s in res["steps"] if s["step"] == "record-colab-run")
    assert rec["failed"][0]["cell_id"] == bad and rec["not_run"] == ids[2:]
    assert res["next_action"]["action"] == "repair"
    assert run.manifest.cell_status[ids[0]] == ItemStatus.PASSED
    assert all(run.manifest.cell_status[c] == ItemStatus.BLOCKED for c in ids[2:])


def test_edited_cells_do_not_count_and_a_fresh_notebook_is_handed_out(tmp_path: Path) -> None:
    run, folder = _lab_run(tmp_path)
    finish(run, cells_file=folder / "cells.json", backend="colab-web", allow_cloud=True, allow_upload=True)
    handoff = run.path("for_colab.ipynb")
    nb = nbformat.read(str(handoff), as_version=4)
    nb.cells[-1].source += "\nprint('my own change')"
    nbformat.write(nb, str(tmp_path / "edited.ipynb"))
    res = finish(run, executed_file=_fake_student_run(tmp_path / "edited.ipynb", tmp_path / "d.ipynb", _ok))
    rec = next(s for s in res["steps"] if s["step"] == "record-colab-run")
    last = list(run.manifest.cell_code_sha256)[-1]
    assert rec["not_run"] == [last] and res["next_action"]["action"] == "run-in-colab"
    assert run.manifest.backend_state["pending_colab_web"] is True


def test_a_file_that_is_not_the_run_notebook_is_rejected(tmp_path: Path) -> None:
    run, folder = _lab_run(tmp_path)
    finish(run, cells_file=folder / "cells.json", backend="colab-web", allow_cloud=True, allow_upload=True)
    junk = tmp_path / "junk.ipynb"
    junk.write_text("not json", encoding="utf-8")
    res = finish(run, executed_file=junk)
    assert res["next_action"]["action"] == "RECORD_INVALID" and run.manifest.backend_state["pending_colab_web"]
    other = tmp_path / "other.ipynb"
    nbformat.write(nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("print(42)")]), str(other))
    assert finish(run, executed_file=other)["next_action"]["action"] == "RECORD_INVALID"
