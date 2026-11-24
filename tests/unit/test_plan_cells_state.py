from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from labpilot.errors import ErrorCode, LabPilotError, classify_execution_error
from labpilot.models import CellsFile, ItemStatus, Plan, RunManifest, RunState
from labpilot.notebooks.build import build_notebook, diff_code_hashes, validate_cells_against_plan
from labpilot.workspace import Run, atomic_write_json, create_run, open_run


def _plan(**kw: object) -> Plan:
    base = {"lab_title": "L", "source_sha256": "x", "tasks": [
        {"id": "SETUP", "title": "s", "placement": "append"},
        {"id": "T1.A", "title": "a", "placement": "append", "depends_on": ["SETUP"],
         "observation_questions": [{"id": "T1.A.Q1", "text": "q?"}]},
    ]}
    base.update(kw)
    return Plan.model_validate(base)


def _cells(*specs: tuple[str, str, str]) -> CellsFile:
    return CellsFile.model_validate({"cells": [{"id": i, "task_id": t, "source": s} for i, t, s in specs]})


def test_plan_rejects_duplicates_and_bad_ids() -> None:
    with pytest.raises(ValidationError):
        _plan(tasks=[{"id": "T1", "title": "a"}, {"id": "T1", "title": "b"}])
    with pytest.raises(ValidationError):
        _plan(tasks=[{"id": "bad id!", "title": "a"}])
    with pytest.raises(ValidationError):
        _plan(tasks=[{"id": "T1", "title": "a", "observation_questions": [{"id": "Q", "text": "?"}]},
                     {"id": "T2", "title": "b", "observation_questions": [{"id": "Q", "text": "?"}]}])
    with pytest.raises(ValidationError):
        CellsFile.model_validate({"cells": [{"id": "has.dot", "task_id": "T1", "source": ""}]})
    with pytest.raises(ValidationError):
        _cells(("a", "T1", "x"), ("a", "T1", "y"))


def test_cells_must_follow_task_order_and_cover_tasks() -> None:
    plan = _plan()
    ok = _cells(("c0", "SETUP", "x=1"), ("c1", "T1.A", "print(x)"))
    assert validate_cells_against_plan(plan, ok) == []
    bad = _cells(("c1", "T1.A", "print(x)"), ("c0", "SETUP", "x=1"))
    assert any("out of the manual's task order" in p for p in validate_cells_against_plan(plan, bad))
    missing = _cells(("c0", "SETUP", "x=1"))
    assert any("T1.A" in p for p in validate_cells_against_plan(plan, missing))
    unknown = _cells(("c0", "SETUP", "x=1"), ("c1", "T9", "y"), ("c2", "T1.A", "z"))
    assert any("unknown task" in p for p in validate_cells_against_plan(plan, unknown))


def test_build_notebook_stable_ids_and_security() -> None:
    nb, warnings = build_notebook(_plan(), _cells(("c0", "SETUP", "x=1"), ("c1", "T1.A", "!pip list\nprint(x)")), "r1")
    code = [c for c in nb.cells if c.cell_type == "code"]
    assert [c.id for c in code] == ["c0", "c1"]
    assert code[1].metadata["labpilot"]["task_id"] == "T1.A"
    assert "labpilot-cell-c1" in code[1].metadata["tags"]
    assert [w.rule for w in warnings] == ["shell"]
    with pytest.raises(LabPilotError) as exc:
        build_notebook(_plan(), _cells(("c0", "SETUP", "files.upload()"), ("c1", "T1.A", "1")), "r1")
    assert exc.value.code == ErrorCode.SECURITY_DENIED
    nb2, _ = build_notebook(_plan(), _cells(("c0", "SETUP", "files.upload()"), ("c1", "T1.A", "1")), "r1",
                            approved={"interactive-upload"})
    assert nb2.cells


def test_changed_cell_marks_downstream_stale() -> None:
    prev = {"a": "1", "b": "2", "c": "3"}
    cur = {"a": "1", "b": "X", "c": "3"}
    status, changed = diff_code_hashes(prev, cur, ["a", "b", "c"])
    assert changed == ["b"]
    assert status == {"b": ItemStatus.PENDING, "c": ItemStatus.STALE}


@pytest.mark.parametrize(
    ("ename", "evalue", "code"),
    [
        ("SyntaxError", "invalid syntax", ErrorCode.CODE_SYNTAX),
        ("ModuleNotFoundError", "No module named 'x'", ErrorCode.MISSING_PACKAGE),
        ("FileNotFoundError", "sales.csv", ErrorCode.MISSING_DATA),
        ("AnalysisException", "[PATH_NOT_FOUND] Path does not exist", ErrorCode.MISSING_DATA),
        ("Py4JJavaError", "java.lang.OutOfMemoryError: Java heap space", ErrorCode.RESOURCE_EXHAUSTED),
        ("CellTimeoutError", "", ErrorCode.TIMEOUT),
        ("DeadKernelError", "", ErrorCode.RUNTIME_DISCONNECTED),
        ("ZeroDivisionError", "division by zero", ErrorCode.CODE_RUNTIME),
    ],
)
def test_error_classification(ename: str, evalue: str, code: ErrorCode) -> None:
    c = classify_execution_error(ename, evalue)
    assert c.code == code
    assert c.next_action


def _manifest(tmp_path: Path) -> RunManifest:
    return RunManifest(run_id="20260101-000000-abcdef", lab_dir=str(tmp_path), source_name="m.docx", source_sha256="x")


def test_run_lock_contention_and_stale_lock(tmp_path: Path) -> None:
    run = create_run(tmp_path, _manifest(tmp_path))
    with run.lock():
        with pytest.raises(LabPilotError) as exc, run.lock():
            pass
        assert exc.value.code == ErrorCode.RUN_LOCKED
    # stale lock from a dead process is recovered automatically
    (run.dir / "run.lock").write_text(json.dumps({"pid": 999999, "since": "x"}), encoding="utf-8")
    with run.lock():
        assert (run.dir / "run.lock").exists()
    assert not (run.dir / "run.lock").exists()


def test_state_transitions_are_persisted_and_logged(tmp_path: Path) -> None:
    run = create_run(tmp_path, _manifest(tmp_path))
    run.set_state(RunState.INSPECTED)
    run.set_state(RunState.PLANNED)
    reopened = open_run(run.id, tmp_path)
    assert reopened.manifest.state == RunState.PLANNED
    assert reopened.manifest.previous_state == RunState.INSPECTED
    events = [json.loads(x) for x in (run.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["state"] for e in events if e["event"] == "state"] == ["CREATED", "INSPECTED", "PLANNED"]
    assert all(e["ts"].endswith("Z") for e in events)


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "m.json"
    for i in range(20):
        atomic_write_json(target, {"i": i})
    assert json.loads(target.read_text(encoding="utf-8")) == {"i": 19}
    assert [p.name for p in tmp_path.iterdir()] == ["m.json"]


def test_open_run_rejects_path_like_ids(tmp_path: Path) -> None:
    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(LabPilotError):
            open_run(bad, tmp_path)


def test_run_index_finds_run_from_elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = create_run(tmp_path, _manifest(tmp_path))
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert isinstance(open_run(run.id), Run)
    assert os.getcwd() == str(other)
