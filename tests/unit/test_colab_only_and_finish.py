"""Colab-only backend selection, safe Colab release and `labpilot finish`, against a FAKE `colab` CLI.

FakeColab below is a fake subprocess layer for unit tests only: it never counts as Colab verification
(the real opt-in check is the cloud-marked e2e test). Screenshots here are real renderings of the fake outputs.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import nbformat
import pytest
from PIL import Image, ImageDraw

from conftest import fake_colab_exec, load_example
from labpilot.adapters import colab_cli
from labpilot.errors import ErrorCode, LabPilotError
from labpilot.evidence.manifest import load_evidence
from labpilot.evidence.validate import validate_evidence
from labpilot.models import ItemStatus, RunState
from labpilot.orchestration import _choose, init_run, inspect_run, load_plan, preflight, save_plan
from labpilot.procutil import CmdResult
from labpilot.report import completion_checks, finish
from labpilot.workspace import Run, read_json


class FakeColab:
    """FAKE `colab` CLI: answers like google-colab-cli 0.8.0, and like the real CLI returns null execution counts."""

    def __init__(self, errors: dict[str, str] | None = None, fail: dict[str, CmdResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.errors = errors or {}  # cell id -> exception message
        self.fail = fail or {}
        self.uploaded: dict[str, str] = {}

    def __call__(self, args: list[str], **_: Any) -> CmdResult:
        self.calls.append(args)
        sub = args[1]
        if sub in self.fail:
            return self.fail[sub]
        if sub == "upload":
            self.uploaded[args[5]] = hashlib.sha256(Path(args[4]).read_bytes()).hexdigest()
        if sub == "exec":
            path = Path(args[args.index("-f") + 1])
            fake_colab_exec(path, self._outputs)
            out = nbformat.read(str(path.with_name(path.stem + "_output.ipynb")), as_version=4)
            assert all(c.execution_count is None for c in out.cells)  # what the real CLI reports
        return CmdResult(0, "", "")

    def _outputs(self, cell: Any) -> list[dict[str, Any]]:
        if cell.id == "verify-upload":
            return [{"output_type": "stream", "name": "stdout", "text": "LABPILOT_HASHES " + json.dumps(self.uploaded) + "\n"}]
        if cell.id in self.errors:
            return [{"output_type": "error", "ename": "ValueError", "evalue": self.errors[cell.id],
                     "traceback": ["ValueError: " + self.errors[cell.id]]}]
        outs = [{"output_type": "stream", "name": "stdout", "text": f"output of {cell.id}\n"}]
        if "plt." in cell.source:  # a chart cell shows a (fake) figure
            outs.append({"output_type": "display_data", "data": {"image/png": _png()}})
        return outs

    def execs(self) -> int:
        return sum(1 for c in self.calls if c[1] == "exec")


def _png() -> str:
    im = Image.new("RGB", (200, 120), "white")
    ImageDraw.Draw(im).rectangle((20, 20, 180, 100), fill="steelblue")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def colab_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LABPILOT_ALLOW_LOCAL", raising=False)


def _fake(monkeypatch: pytest.MonkeyPatch, fake: FakeColab | None) -> None:
    monkeypatch.setattr(colab_cli.shutil, "which", lambda _name: "colab" if fake else None)
    if fake:
        monkeypatch.setattr(colab_cli, "run_cmd", fake)


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


def test_auto_means_colab_and_local_is_only_for_tests(tmp_path: Path, colab_only: None,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = _lab_run(tmp_path)
    assert _choose(run, "auto")[0] == "colab-cli"  # even without consent: no silent local fallback
    with pytest.raises(LabPilotError) as exc:
        _choose(run, "local")
    assert exc.value.code == ErrorCode.BACKEND_UNAVAILABLE and "--backend colab-cli" in exc.value.next_action
    monkeypatch.setenv("LABPILOT_ALLOW_LOCAL", "1")
    assert _choose(run, "local")[0] == "local"


def test_missing_cli_or_sign_in_stops_with_one_command(tmp_path: Path, colab_only: None,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = _lab_run(tmp_path)
    _fake(monkeypatch, None)
    with pytest.raises(LabPilotError) as exc:
        preflight(run, "auto", True, True)
    assert exc.value.code == ErrorCode.BACKEND_UNAVAILABLE and "--install-colab-cli" in exc.value.next_action

    _fake(monkeypatch, FakeColab(fail={"sessions": CmdResult(1, "", "Enter the authorization code:")}))
    with pytest.raises(LabPilotError) as exc:
        preflight(run, "auto", True, True)
    assert exc.value.code == ErrorCode.AUTH_REQUIRED and "labpilot colab-login" in exc.value.next_action
    assert run.manifest.state == RunState.WAITING_FOR_AUTH
    res = finish(run, cells_file=run.lab_dir / "cells.json", allow_cloud=True, allow_upload=True)
    assert res["verdict"] == "NOT_FINISHED" and res["next_action"]["actor"] == "user"
    assert "labpilot colab-login" in res["next_action"]["detail"]


def test_finish_needs_consent_before_using_colab(tmp_path: Path, colab_only: None,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    run, folder = _lab_run(tmp_path)
    fake = FakeColab()
    _fake(monkeypatch, fake)
    res = finish(run, cells_file=folder / "cells.json")
    assert res["next_action"]["action"] == "consent" and "--allow-cloud --allow-upload" in res["next_action"]["detail"]
    assert not any(c[1] in ("new", "upload", "exec") for c in fake.calls)


def test_finish_null_counts_pass_and_verified_cells_are_not_rerun(tmp_path: Path, colab_only: None,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    run, folder = _lab_run(tmp_path)
    fake = FakeColab()
    _fake(monkeypatch, fake)
    res = finish(run, cells_file=folder / "cells.json", allow_cloud=True, allow_upload=True)
    steps = {s["step"]: s for s in res["steps"]}
    assert steps["preflight"]["backend"] == "colab-cli" and steps["execute"]["result"] == "OK"
    assert steps["capture"]["status"] == "done", steps["capture"]
    assert res["next_action"]["action"] == "answers"
    m = run.manifest
    assert all(s == ItemStatus.PASSED for s in m.cell_status.values())
    records = read_json(run.exec_dir / "current_records.json")
    assert records and all(r["execution_count"] is None and r["outputs"] for r in records)
    nb = nbformat.read(str(run.executed_notebook), as_version=4)
    assert all(c.execution_count is None and c.outputs for c in nb.cells if c.cell_type == "code")
    checks = completion_checks(run, load_plan(run))
    assert checks["all_tasks_executed"]["ok"] and checks["evidence_valid"]["ok"], checks
    assert m.backend_state["owned_sessions"] == [] and "release_pending" not in m.backend_state

    # second call: nothing changed, so nothing is executed or captured again
    execs = fake.execs()
    again = finish(run)
    assert {s["step"]: s["status"] for s in again["steps"]} == {
        "build-notebook": "skipped", "execute": "skipped", "capture": "skipped", "answers": "waiting"}
    assert fake.execs() == execs

    # stale attempt / changed code invalidate the evidence
    ev = load_evidence(run.dir)
    m.cell_code_sha256["t1a-load"] = "changed"
    assert any("stale" in p for p in validate_evidence(run.dir, ev, m))
    m.current_attempt = "a999"
    assert any("not the current attempt" in p for p in validate_evidence(run.dir, ev, m))


def test_finish_stops_on_error_output(tmp_path: Path, colab_only: None, monkeypatch: pytest.MonkeyPatch) -> None:
    run, folder = _lab_run(tmp_path)
    _fake(monkeypatch, FakeColab(errors={"t1b-revenue": "bad value"}))
    res = finish(run, cells_file=folder / "cells.json", allow_cloud=True, allow_upload=True)
    ex = next(s for s in res["steps"] if s["step"] == "execute")
    assert ex["result"] == "FAILED_CELLS" and ex["failed"][0]["cell_id"] == "t1b-revenue"
    assert res["verdict"] == "NOT_FINISHED" and res["next_action"]["action"] == "repair"
    assert "capture" not in {s["step"] for s in res["steps"]}
    assert not completion_checks(run, load_plan(run))["all_tasks_executed"]["ok"]
    # unchanged failing code is not sent to Colab again
    again = finish(run)
    assert next(s for s in again["steps"] if s["step"] == "execute")["status"] == "failed"
    assert "preflight" not in {s["step"] for s in again["steps"]}
    # the same --gpu on every call does not re-send them either; a GPU change does
    assert "preflight" not in {s["step"] for s in finish(run, gpu="none")["steps"]}
    assert "preflight" in {s["step"] for s in finish(run, gpu="t4")["steps"]}
    assert run.manifest.backend_state["gpu"] == "T4"
    assert "preflight" not in {s["step"] for s in finish(run, gpu="T4")["steps"]}


CUDA_ERROR = "RuntimeError: Found no NVIDIA driver on your system"


def test_cpu_failure_like_a_gpu_lab_switches_to_t4_once(tmp_path: Path, colab_only: None,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    run, folder = _lab_run(tmp_path)
    fake = FakeColab(errors={"t1b-revenue": CUDA_ERROR})  # this fake's probe reports no GPU, so T4 falls back to CPU
    _fake(monkeypatch, fake)
    res = finish(run, cells_file=folder / "cells.json", allow_cloud=True, allow_upload=True, allow_gpu=True)
    assert "switch-to-gpu" in {s["step"] for s in res["steps"]}
    news = [c for c in fake.calls if c[1] == "new"]
    assert "--gpu" not in news[0] and news[1][-2:] == ["--gpu", "T4"]  # CPU first, then T4, then CPU fallback
    assert run.manifest.backend_state["runtime"] == "Colab CPU (no GPU was available)"
    assert res["next_action"]["action"] == "gpu-unavailable" and res["next_action"]["actor"] == "user"
    # calling again does not loop through GPU attempts: the failing cells are unchanged
    calls = len(fake.calls)
    assert "switch-to-gpu" not in {s["step"] for s in finish(run, allow_gpu=True)["steps"]}
    assert not [c for c in fake.calls[calls:] if c[1] == "new"]


def test_no_gpu_switch_without_permission(tmp_path: Path, colab_only: None, monkeypatch: pytest.MonkeyPatch) -> None:
    run, folder = _lab_run(tmp_path)
    fake = FakeColab(errors={"t1b-revenue": CUDA_ERROR})
    _fake(monkeypatch, fake)
    res = finish(run, cells_file=folder / "cells.json", allow_cloud=True, allow_upload=True)
    assert "switch-to-gpu" not in {s["step"] for s in res["steps"]}
    assert not any("--gpu" in c for c in fake.calls) and res["next_action"]["action"] == "repair"


def test_stop_keeps_ownership_until_release_is_confirmed(tmp_path: Path, colab_only: None,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from labpilot.adapters.base import RunContext, RuntimeHandle
    from labpilot.config import load_profile

    run, _ = _lab_run(tmp_path)
    fake = FakeColab(fail={"stop": CmdResult(1, "", "connection reset by peer")})
    _fake(monkeypatch, fake)
    be = colab_cli.ColabCliBackend()
    session = "labpilot-x-1234"
    state = run.manifest.backend_state
    state.update(owned_sessions=[session], session=session)
    ctx = RunContext(run=run, profile=load_profile("python"), attempt_id="a001", timeout_s=60, datasets=[])
    be.stop(RuntimeHandle("colab-cli", "a001", state={"session": session}), ctx)
    assert state["owned_sessions"] == [session] and state["release_pending"] == [session]
    assert state["session"] == session

    fake.fail = {}
    assert be.cancel(run) is True  # retry
    assert state["owned_sessions"] == [] and "release_pending" not in state and "session" not in state
    assert [c for c in fake.calls if c[1] == "stop"] == [["colab", "stop", "-s", session]] * 2


def test_finish_prints_steps_and_cell_progress_on_stderr_only(tmp_path: Path, colab_only: None,
                                                               monkeypatch: pytest.MonkeyPatch,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    run, folder = _lab_run(tmp_path)
    _fake(monkeypatch, FakeColab())
    monkeypatch.setenv("LABPILOT_PROGRESS", "1")
    capsys.readouterr()
    finish(run, cells_file=folder / "cells.json", allow_cloud=True, allow_upload=True)
    out, err = capsys.readouterr()
    assert out == ""  # stdout is reserved for the JSON result
    steps = [line.split("] ", 1)[1] for line in err.splitlines() if "] Step " in line]
    assert steps == ["Step 1/6: building the notebook", "Step 2/6: checking the input files",
                     "Step 3/6: running the cells on Colab", "Step 4/6: taking screenshots of the outputs",
                     "Step 5/6: checking the answers"]
    total = len(run.manifest.cell_status)
    done = [line for line in err.splitlines() if "cells done" in line and line.endswith("passed")]
    assert len(done) == total
    assert done[-1].split("] ", 1)[1].startswith(f"Colab: {total}/{total} cells done (100%) - ")
    finish(run)  # nothing changed: every step says it was skipped instead of claiming to run
    assert [line.split("] ", 1)[1] for line in capsys.readouterr().err.splitlines() if "] Step " in line][:4] == [
        "Step 1/6: notebook already built (skipped)", "Step 2/6: checking the input files",
        "Step 3/6: every cell already passed with the current code (skipped)",
        "Step 4/6: screenshots already match the current run (skipped)"]


def test_progress_line_formats() -> None:
    from labpilot.workspace import progress_line

    ts = "2026-11-27T14:41:47.123456Z"
    assert progress_line(ts, "cell", {"cell_id": "benchmark-million", "status": "passed", "done": 7, "total": 19,
                                      "where": "Colab", "message": ""}) == \
        "[labpilot 14:41:47] Colab: 7/19 cells done (36%) - benchmark-million passed"
    assert progress_line(ts, "progress", {"message": "Step 3/6: running the cells on Colab"}) == \
        "[labpilot 14:41:47] Step 3/6: running the cells on Colab"
    assert progress_line(ts, "state", {"state": "EXECUTING", "note": "x"}) == "[labpilot 14:41:47] state EXECUTING"
