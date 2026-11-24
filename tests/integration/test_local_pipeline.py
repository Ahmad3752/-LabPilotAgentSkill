"""Integration tests with a REAL local Jupyter kernel, REAL headless Chromium and the REAL CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import docx
import nbformat
import pytest
from conftest import cli, load_example
from PIL import Image

pytestmark = pytest.mark.integration


def _start(folder: Path, cells: dict[str, Any] | None = None) -> tuple[str, Path]:
    agent = load_example("python", "agent_files")
    init = cli(folder, "init", "--lab", "Lab 3.docx", "--synthetic")
    assert init["ok"], init
    rid, run_dir = init["run_id"], Path(init["run_dir"])
    draft = json.loads((run_dir / "draft_plan.json").read_text(encoding="utf-8"))
    (folder / "plan.json").write_text(json.dumps(agent.make_plan(draft)), encoding="utf-8")
    res = cli(folder, "plan", rid, "--file", "plan.json")
    assert res["_exit"] in (0,), res
    (folder / "cells.json").write_text(json.dumps(cells or agent.CELLS), encoding="utf-8")
    assert cli(folder, "build-notebook", rid, "--file", "cells.json")["ok"]
    return rid, run_dir


def _with_cell(cell_id: str, source: str) -> dict[str, Any]:
    cells = json.loads(json.dumps(load_example("python", "agent_files").CELLS))
    for c in cells["cells"]:
        if c["id"] == cell_id:
            c["source"] = source
    return cells


def test_failure_repair_replay_and_stale_invalidation(python_lab: Path) -> None:
    rid, run_dir = _start(python_lab, _with_cell("t1b-revenue", "sales['revenue'] = sales['qty'] * sales['unit_price']"))
    assert cli(python_lab, "preflight", rid, "--backend", "local")["ok"]
    ex = cli(python_lab, "execute", rid)
    assert ex["_exit"] == 6 and ex["status"] == "FAILED_CELLS"
    assert ex["passed"] == ["setup-imports", "t1a-load"]
    f = ex["failed"][0]
    assert f["cell_id"] == "t1b-revenue" and f["error_type"] == "KeyError" and f["error_code"] == "CODE_RUNTIME"
    assert "qty" in f["traceback_tail"]
    assert set(ex["blocked"]) == {"t2a-merge", "t2a-summary", "t2b-plot"}  # stop on first error, never skipped silently

    # agent repairs only that cell; downstream cells become stale and the repair is counted
    (python_lab / "cells.json").write_text(json.dumps(load_example("python", "agent_files").CELLS), encoding="utf-8")
    b = cli(python_lab, "build-notebook", rid, "--file", "cells.json")
    assert b["changed_cells"] == ["t1b-revenue"] and set(b["stale_cells"]) == {"t2a-merge", "t2a-summary", "t2b-plot"}
    ex2 = cli(python_lab, "execute", rid)
    assert ex2["status"] == "OK" and len(ex2["passed"]) == 6 and ex2["attempt_id"] == "a002"
    st = cli(python_lab, "status", rid)
    assert st["cells"] == {"passed": 6} and st["attempt"] == "a002"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["cell_repairs"] == {"t1b-revenue": 1}
    nb = nbformat.read(str(run_dir / "notebook" / "executed.ipynb"), as_version=4)
    counts = [c.execution_count for c in nb.cells if c.cell_type == "code"]
    assert counts == [1, 2, 3, 4, 5, 6]  # replayed from a fresh kernel, in order


def test_retry_budget_exhaustion_blocks(python_lab: Path) -> None:
    rid, _ = _start(python_lab, _with_cell("t1a-load", "raise ValueError('v0')"))
    assert cli(python_lab, "preflight", rid, "--backend", "local")["ok"]
    for i in range(1, 3):
        cli(python_lab, "execute", rid, "--retry-budget", "1")
        (python_lab / "cells.json").write_text(json.dumps(_with_cell("t1a-load", f"raise ValueError('v{i}')")),
                                               encoding="utf-8")
        cli(python_lab, "build-notebook", rid, "--file", "cells.json")
    res = cli(python_lab, "execute", rid)
    assert res["_exit"] == 4 and res["error"]["code"] == "RETRY_BUDGET_EXHAUSTED"
    assert cli(python_lab, "status", rid)["next_action"]["actor"] == "user"


def test_timeout_and_output_bounds(python_lab: Path) -> None:
    cells = _with_cell("setup-imports", "import os, time\nimport pandas as pd\nprint('x' * 600000)")
    cells["cells"][1]["source"] = "time.sleep(30)"
    rid, run_dir = _start(python_lab, cells)
    assert cli(python_lab, "preflight", rid, "--backend", "local")["ok"]
    started = time.time()
    ex = cli(python_lab, "execute", rid, "--timeout", "4")
    assert time.time() - started < 60
    assert ex["failed"][0]["cell_id"] == "t1a-load" and ex["failed"][0]["error_code"] == "TIMEOUT"
    records = json.loads((run_dir / "executions" / "current_records.json").read_text(encoding="utf-8"))
    big = records[0]
    assert big["output_truncated"] is True and "bytes of output omitted" in big["outputs"][0]["text"]
    assert len(big["outputs"][0]["text"]) < 260_000


def test_missing_dataset_partial_then_resume_completes(python_lab: Path) -> None:
    hidden = python_lab.parent / "regions.csv.hidden"
    (python_lab / "regions.csv").rename(hidden)
    agent = load_example("python", "agent_files")
    rid, run_dir = _start(python_lab)
    st = cli(python_lab, "status", rid)
    assert st["state"] == "WAITING_FOR_INPUT"
    assert any(b["code"] == "DATASET_MISSING" and "regions.csv" in b["message"] for b in st["blockers"])
    assert cli(python_lab, "preflight", rid, "--backend", "local")["ok"]
    ex = cli(python_lab, "execute", rid)
    assert ex["status"] == "PARTIAL" and ex["passed"] == ["setup-imports"]  # independent work still done
    assert cli(python_lab, "capture", rid)["ok"]
    exp = cli(python_lab, "export", rid, "--no-render")
    assert exp["status"] == "PARTIAL" and exp["docx"] == "Lab_3_Partial.docx" and exp["_exit"] == 6
    out = Path(exp["output_dir"])
    review = (out / "REVIEW.md").read_text(encoding="utf-8")
    assert "DATASET_MISSING" in review and "labpilot resume" in review
    text = "\n".join(p.text for t in docx.Document(str(out / "Lab_3_Partial.docx")).tables for c in t._cells for p in c.paragraphs)
    assert "Not verified — required dataset/runtime unavailable" in text
    report = json.loads((out / "execution_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "PARTIAL" and report["synthetic_demo"] is True

    # user supplies the file; resume re-stages, replays, captures and stops for the agent's answers
    hidden.rename(python_lab / "regions.csv")
    res = cli(python_lab, "resume", rid)
    steps = [s["step"] for s in res["steps"]]
    assert steps[:1] == ["revalidate-inputs"] and "execute" in steps and "capture" in steps
    assert res["next_action"]["action"] == "answers"
    answers = agent.make_answers(str(run_dir / "notebook" / "executed.ipynb"))
    (python_lab / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    assert cli(python_lab, "answers", rid, "--file", "answers.json")["ok"]
    final = cli(python_lab, "resume", rid)
    assert final["state"] == "COMPLETED", final
    assert cli(python_lab, "validate", rid)["ok"]


def test_answers_must_be_grounded(python_lab: Path) -> None:
    agent = load_example("python", "agent_files")
    rid, run_dir = _start(python_lab)
    assert cli(python_lab, "preflight", rid, "--backend", "local")["ok"]
    assert cli(python_lab, "execute", rid)["status"] == "OK"
    good = agent.make_answers(str(run_dir / "notebook" / "executed.ipynb"))
    bad = json.loads(json.dumps(good))
    bad["answers"][0]["source_cell_ids"] = []  # verified claim without evidence
    bad["answers"][1]["verification"] = "conceptual"  # observed question answered without data
    bad["answers"].pop()  # missing answer
    (python_lab / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    res = cli(python_lab, "answers", rid, "--file", "bad.json")
    probs = " ".join(res["error"]["details"]["problems"])
    assert res["error"]["code"] == "ANSWERS_INVALID"
    assert "must cite" in probs and "observed results" in probs and "missing answers" in probs


def test_crash_mid_execution_then_resume(python_lab: Path) -> None:
    cells = _with_cell("t2a-merge", "import time\ntime.sleep(120)\nmerged = sales.merge(regions, on='region_id', how='left')\n"
                                    "print(merged.to_string())")
    rid, run_dir = _start(python_lab, cells)
    assert cli(python_lab, "preflight", rid, "--backend", "local")["ok"]
    proc = subprocess.Popen([sys.executable, "-m", "labpilot", "execute", rid, "--json"], cwd=python_lab,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    records = None
    for _ in range(120):
        found = list((run_dir / "executions").glob("a001/records.jsonl"))
        if found and found[0].read_text(encoding="utf-8").count("\n") >= 3:
            records = found[0]
            break
        time.sleep(0.5)
    assert records is not None, "runner never checkpointed"
    # user cancels while the kernel is busy: owned runtime stopped, the executing CLI persists CANCELLED
    cancel = cli(python_lab, "cancel", rid)
    assert cancel["stopped_processes"] is True
    assert proc.wait(timeout=90) == 130
    lines = [json.loads(x) for x in records.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [r["cell_id"] for r in lines] == ["setup-imports", "t1a-load", "t1b-revenue"]  # durable checkpoint
    assert cli(python_lab, "status", rid)["state"] == "CANCELLED"

    (python_lab / "cells.json").write_text(json.dumps(load_example("python", "agent_files").CELLS), encoding="utf-8")
    cli(python_lab, "build-notebook", rid, "--file", "cells.json")
    res = cli(python_lab, "resume", rid)
    assert "invalidate-kernel-state" in [s["step"] for s in res["steps"]]
    assert any(s["step"] == "execute" and s["status"] == "OK" for s in res["steps"])
    assert res["next_action"]["action"] == "answers"


def test_abrupt_crash_leaves_recoverable_state(python_lab: Path) -> None:
    from labpilot.procutil import kill_tree

    cells = _with_cell("t1b-revenue", "import time\ntime.sleep(120)\n" +
                       load_example("python", "agent_files").CELLS["cells"][2]["source"])
    rid, run_dir = _start(python_lab, cells)
    assert cli(python_lab, "preflight", rid, "--backend", "local")["ok"]
    proc = subprocess.Popen([sys.executable, "-m", "labpilot", "execute", rid, "--json"], cwd=python_lab,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rec = run_dir / "executions" / "a001" / "records.jsonl"
    for _ in range(120):
        if rec.exists() and rec.read_text(encoding="utf-8").count("\n") >= 2:
            break
        time.sleep(0.5)
    kill_tree(proc.pid)  # power loss / killed terminal: no cleanup code runs at all
    proc.wait(timeout=30)
    assert (run_dir / "run.lock").exists()  # stale lock from a dead process
    assert cli(python_lab, "status", rid)["state"] == "EXECUTING"
    (python_lab / "cells.json").write_text(json.dumps(load_example("python", "agent_files").CELLS), encoding="utf-8")
    assert cli(python_lab, "build-notebook", rid, "--file", "cells.json")["ok"]  # stale lock recovered
    res = cli(python_lab, "resume", rid)
    assert any(s["step"] == "execute" and s["status"] == "OK" for s in res["steps"]), res


def test_changed_input_is_detected(python_lab: Path) -> None:
    rid, _ = _start(python_lab)
    assert cli(python_lab, "preflight", rid, "--backend", "local")["ok"]
    with (python_lab / "sales.csv").open("a", encoding="utf-8") as fh:
        fh.write("9999,1,Pen,1,1.25\n")
    res = cli(python_lab, "execute", rid)
    assert res["error"]["code"] == "INPUT_CHANGED"
    res2 = cli(python_lab, "resume", rid)
    assert res2["steps"][0]["step"] == "revalidate-inputs"
    assert any(s["step"] == "execute" and s["status"] == "OK" for s in res2["steps"])


def test_capture_produces_real_valid_pngs_with_provenance(python_lab: Path) -> None:
    rid, run_dir = _start(python_lab)
    assert cli(python_lab, "preflight", rid, "--backend", "local")["ok"]
    assert cli(python_lab, "execute", rid)["status"] == "OK"
    cap = cli(python_lab, "capture", rid)
    assert cap["ok"] and cap["problems"] == []
    manifest = json.loads((run_dir / "evidence" / "evidence_manifest.json").read_text(encoding="utf-8"))
    recs = manifest["records"]
    assert {r["task_id"] for r in recs} == {"SETUP", "T1.A", "T1.B", "T2.A", "T2.B"}
    long_parts = [r for r in recs if r["cell_ids"] == ["t2a-merge"]]
    assert [r["kind"] for r in long_parts][0] == "code" and len(long_parts) >= 3  # long output was split
    for r in recs:
        p = run_dir / r["path"]
        with Image.open(p) as im:
            assert im.size == (r["width"], r["height"]) and im.width >= 400
        assert r["capture_mode"] == "rendered-local-notebook" and r["backend"].startswith("local-jupyter")
    html = (run_dir / "evidence" / "render" / "executed.html").read_text(encoding="utf-8")
    assert "SYNTHETIC DEMO DATA" in html
    assert "<th>order_id</th>" in html  # pandas table structure preserved (not escaped by sanitising)
    assert "<script" not in html.split("<body", 1)[1].split("jp-Notebook", 1)[1]  # no active content in cells


def test_colab_mcp_host_mediated_flow(python_lab: Path) -> None:
    """Exercise the bridge protocol by replaying the same notebook's real local outputs as the host result."""
    rid, run_dir = _start(python_lab)
    assert cli(python_lab, "preflight", rid, "--backend", "local")["ok"]
    assert cli(python_lab, "execute", rid)["status"] == "OK"
    records = json.loads((run_dir / "executions" / "current_records.json").read_text(encoding="utf-8"))
    denied = cli(python_lab, "execute", rid, "--backend", "colab-mcp")
    assert denied["error"]["code"] == "CONSENT_REQUIRED"  # cloud use always needs explicit consent
    cli(python_lab, "preflight", rid, "--backend", "colab-mcp", "--allow-cloud", "--allow-upload")
    wait = cli(python_lab, "execute", rid, "--backend", "colab-mcp")
    assert wait["status"] == "WAITING_FOR_HOST" and wait["_exit"] == 6
    actions = json.loads((run_dir / "host_actions.json").read_text(encoding="utf-8"))
    assert [c["cell_id"] for c in actions["cells"]] == [r["cell_id"] for r in records]
    result = {"tool_names_used": ["test-harness (not colab)"], "runtime": {"note": "integration test"},
              "cells": [{"cell_id": r["cell_id"], "code_sha256": r["code_sha256"], "status": r["status"],
                         "execution_count": r["execution_count"], "outputs": r["outputs"]} for r in records]}
    (python_lab / "result.json").write_text(json.dumps(result), encoding="utf-8")
    rec = cli(python_lab, "record-execution", rid, "--file", "result.json")
    assert rec["status"] == "OK" and rec["backend"] == "colab-mcp (host-reported)"
    assert cli(python_lab, "status", rid)["executed_on"] == "colab-mcp (host-reported)"
