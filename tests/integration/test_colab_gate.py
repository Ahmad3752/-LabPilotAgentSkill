"""The kernel-side gate of the batched Colab adapter, run in a REAL local IPython kernel.

nbclient with allow_errors=True keeps going after a cell raises, like `colab exec` does, so this
checks the real gate code (not a fake): later cells never run, outputs are unchanged.
Only `colab exec` itself (transport, files) is replaced, by a local kernel; this is not Colab verification.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import nbformat
import pytest
from nbclient import NotebookClient
from nbformat.v4 import new_code_cell, new_notebook

from labpilot.adapters import colab_cli
from labpilot.adapters.base import CellRequest, RunContext, RuntimeHandle
from labpilot.adapters.colab_cli import CONTROL, ColabCliBackend, control_cell
from labpilot.config import load_profile
from labpilot.models import RunManifest
from labpilot.procutil import CmdResult
from labpilot.workspace import create_run

pytestmark = pytest.mark.integration


def _execute(sources: list[str]) -> list[Any]:
    nb = new_notebook(cells=[new_code_cell(s) for s in sources])
    NotebookClient(nb, kernel_name="python3", allow_errors=True, timeout=120).execute()
    return nb.cells


def _batch(lab: list[str]) -> list[str]:
    out = [control_cell("t", None, False)]
    for i, src in enumerate(lab):
        out += [src, control_cell("t", f"c{i + 1}", i == len(lab) - 1)]
    return out


def _gates(cells: list[Any]) -> list[str]:
    return [o.text.split()[1] for c in cells[::2] for o in c.outputs if o.get("name") == "stdout"]


@pytest.fixture(autouse=True)
def _workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(colab_cli, "REMOTE_DIR", str(tmp_path))  # the control cells chdir here, not /content


@pytest.mark.parametrize("failing", [
    "open('ran2.txt', 'w').write('x')\nraise ValueError('boom')",
    "try:\n    1 / 0\nexcept ZeroDivisionError:\n    get_ipython().showtraceback()",  # traceback without raising
    "x = (",  # syntax error
])
def test_gate_stops_after_a_failing_cell(tmp_path: Path, failing: str) -> None:
    lab = ["x = 1\nprint('one')", failing, "x = 99\nopen('ran3.txt', 'w').write('x')\nprint('three')"]
    cells = _execute([*_batch(lab), f"{CONTROL}\nprint('CHECK', x, _labpilot_marker)"])
    assert _gates(cells[:7]) == ["open", "passed", "failed", "skipped"]
    assert cells[5].outputs == []  # the cell after the failure never ran ...
    assert not (tmp_path / "ran3.txt").exists()  # ... so it changed nothing
    assert cells[7].outputs[0].text == "CHECK 1 c1\n"  # kernel state is what the passed cell left


def test_gated_cells_give_the_same_outputs_as_plain_cells() -> None:
    lab = ["a = 2\na * 21",
           "import sys\nprint('hi')\nprint('warn', file=sys.stderr)",
           "from IPython.display import HTML, display\ndisplay(HTML('<b>x</b>'))",
           "def f():\n    return 1 / 0\nf()"]

    def strip(c: Any) -> list[dict[str, Any]]:
        outs = []
        for o in c.outputs:
            o = dict(o)
            o.pop("execution_count", None)
            if "traceback" in o:  # only the In[n] counter may differ
                o["traceback"] = [re.sub(r"In\[\d+\]", "In[N]", line) for line in o["traceback"]]
            outs.append(o)
        return outs

    plain = _execute(lab)
    gated = _execute(_batch(lab))[1::2]
    assert [strip(c) for c in gated] == [strip(c) for c in plain]
    assert any(o["output_type"] == "error" for o in strip(plain[3]))


def test_adapter_batch_on_a_real_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """execute_cells end to end; FAKE `colab exec` = nbclient on a local kernel (one batch, one kernel)."""

    def fake_colab(args: list[str], **_: Any) -> CmdResult:
        path = Path(args[args.index("-f") + 1])
        nb = nbformat.read(str(path), as_version=4)
        NotebookClient(nb, kernel_name="python3", allow_errors=True, timeout=120).execute()
        nbformat.write(nb, str(path.with_name(path.stem + "_output.ipynb")))
        return CmdResult(0, "", "")

    monkeypatch.setattr(colab_cli, "run_cmd", fake_colab)
    m = RunManifest(run_id="20260101-000000-gggggg", lab_dir=str(tmp_path), source_name="m.docx", source_sha256="x")
    run = create_run(tmp_path, m)
    ctx = RunContext(run=run, profile=load_profile("python"), attempt_id="a001", timeout_s=60, datasets=[])
    work = tmp_path / "w"
    work.mkdir()
    handle = RuntimeHandle("colab-cli", "a001", state={"session": "labpilot-x", "work": str(work)})
    sources = ["n = 5\nprint(n)", "n * 2", "raise KeyError('missing')", "open('late.txt', 'w')", "print('never')"]
    cells = [CellRequest(f"c{i}", "T1", s, "h") for i, s in enumerate(sources, 1)]
    recs = ColabCliBackend(command=["colab"]).execute_cells(handle, ctx, cells, lambda e: None)
    assert [r.status for r in recs] == ["passed", "passed", "failed", "blocked", "blocked"]
    assert recs[0].stdout == "5\n" and recs[1].outputs[0]["data"]["text/plain"] == "10"
    assert recs[2].error_type == "KeyError" and recs[3].outputs == []
    assert not (tmp_path / "late.txt").exists()
    assert run.manifest.backend_state["last_marker"] == "c2"
    assert all(r.started_at and r.ended_at and r.started_at <= r.ended_at for r in recs[:3])
