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
from nbformat.v4 import new_output
from PIL import Image, ImageDraw

from conftest import load_example
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
            nb = nbformat.read(str(path), as_version=4)
            cell = nb.cells[0]
            if cell.id == "verify-upload":
                outs = [{"output_type": "stream", "name": "stdout",
                         "text": "LABPILOT_HASHES " + json.dumps(self.uploaded) + "\n"}]
            elif cell.id in self.errors:
                outs = [{"output_type": "error", "ename": "ValueError", "evalue": self.errors[cell.id],
                         "traceback": ["ValueError: " + self.errors[cell.id]]}]
            else:
                outs = [{"output_type": "stream", "name": "stdout", "text": f"output of {cell.id}\n"}]
                if "plt." in cell.source:  # a chart cell shows a (fake) figure
                    outs.append({"output_type": "display_data", "data": {"image/png": _png()}})
            cell.outputs = [new_output(**o) for o in outs]
            assert cell.execution_count is None  # what the real CLI reports
            nbformat.write(nb, str(path.with_name(path.stem + "_output.ipynb")))
        return CmdResult(0, "", "")

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
