"""Colab CLI adapter parsing/decision logic against FAKE subprocess responses (unit tests only).

These fakes never count as Colab verification; the real opt-in smoke test is
tests/integration/test_colab_cloud_smoke.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import nbformat
import pytest
from nbformat.v4 import new_output

from conftest import fake_colab_exec
from labpilot.adapters import colab_cli
from labpilot.adapters.base import CellRequest, RunContext, RuntimeHandle
from labpilot.adapters.colab_cli import ColabCliBackend
from labpilot.config import load_profile
from labpilot.errors import ErrorCode, LabPilotError
from labpilot.models import RunManifest
from labpilot.procutil import CmdResult
from labpilot.workspace import create_run


class FakeColab:
    """FAKE `colab` invocations: writes <name>_output.ipynb the way google-colab-cli 0.8.0 does."""

    def __init__(self, outputs: dict[str, list[dict[str, Any]]] | None = None, fail: dict[str, CmdResult] | None = None):
        self.calls: list[list[str]] = []
        self.outputs = outputs or {}
        self.fail = fail or {}
        self.ran: list[str] = []  # lab cells whose code the fake "ran"

    def __call__(self, args: list[str], **_: Any) -> CmdResult:
        self.calls.append(args)
        sub = args[1]
        if sub in self.fail:
            return self.fail[sub]
        if sub == "exec":
            self.ran += fake_colab_exec(Path(args[args.index("-f") + 1]), lambda cell: self.outputs.get(
                cell.id, [{"output_type": "stream", "name": "stdout", "text": "ok\n"}]))
        return CmdResult(0, "", "")


def _run(tmp_path: Path, consent: bool = True) -> Any:
    m = RunManifest(run_id="20260101-000000-cccccc", lab_dir=str(tmp_path), source_name="m.docx", source_sha256="x")
    m.consent.cloud_compute = consent
    return create_run(tmp_path, m)


def _backend(monkeypatch: pytest.MonkeyPatch, fake: FakeColab) -> ColabCliBackend:
    monkeypatch.setattr(colab_cli, "run_cmd", fake)
    return ColabCliBackend(command=["colab"])


def test_missing_cli_fails_preflight_with_install_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(colab_cli, "load_config", lambda: {})
    monkeypatch.setattr(colab_cli.shutil, "which", lambda _name: None)
    be = ColabCliBackend()
    assert be.base == []
    res = be.preflight(_run(tmp_path), load_profile("python"))
    assert not res.ok and res.checks[0].name == "colab_cli" and "--install-colab-cli" in res.checks[0].fix


def test_preflight_detects_missing_auth_and_consent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeColab(fail={"sessions": CmdResult(1, "Please visit https://accounts.google.com/o/oauth2/auth ...",
                                                 "Enter the authorization code:")})
    be = _backend(monkeypatch, fake)
    res = be.preflight(_run(tmp_path, consent=False), load_profile("python"))
    checks = {c.name: c.ok for c in res.checks}
    assert checks["colab_auth"] is False and checks["consent_cloud_compute"] is False
    assert res.state_hint == "WAITING_FOR_AUTH" and not res.ok


def test_start_requires_consent_and_records_owned_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeColab(outputs={"probe": [{"output_type": "stream", "name": "stdout",
                                         "text": 'LABPILOT_PROBE {"python": "3.12.0", "google_colab": true}\n'}]})
    be = _backend(monkeypatch, fake)
    run = _run(tmp_path, consent=False)
    ctx = RunContext(run=run, profile=load_profile("python"), attempt_id="a001", timeout_s=60, datasets=[])
    with pytest.raises(LabPilotError) as exc:
        be.start(ctx)
    assert exc.value.code == ErrorCode.CONSENT_REQUIRED
    run.manifest.consent.cloud_compute = True
    handle = be.start(ctx)
    assert handle.identity["google_colab"] is True
    session = handle.state["session"]
    assert session.startswith("labpilot-") and session in run.manifest.backend_state["owned_sessions"]
    assert fake.calls[0][:3] == ["colab", "new", "-s"] and "--gpu" not in fake.calls[0]  # CPU by default
    be.stop(handle, ctx)
    assert ["colab", "stop", "-s", session] in fake.calls
    assert run.manifest.backend_state["runtime"] == "Colab CPU"


class GpuFakeColab(FakeColab):
    """FAKE `colab`: `new --gpu X` is refused for X in `refuse`; the probe reports a GPU only when one was granted."""

    def __init__(self, refuse: tuple[str, ...] = (), empty: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.refuse, self.empty = refuse, empty

    def __call__(self, args: list[str], **kw: Any) -> CmdResult:
        if args[1] == "new":
            want = args[args.index("--gpu") + 1] if "--gpu" in args else None
            if want in self.refuse:
                self.calls.append(args)
                return CmdResult(1, "", "No backend with GPU available")
            gpu = f"Tesla {want}" if want and want not in self.empty else None
            self.outputs["probe"] = [{"output_type": "stream", "name": "stdout",
                                      "text": "LABPILOT_PROBE " + json.dumps({"google_colab": True, "gpu": gpu}) + "\n"}]
        return super().__call__(args, **kw)


@pytest.mark.parametrize(("refuse", "empty", "tried", "runtime"), [
    ((), (), ["L4"], "Colab GPU (Tesla L4)"),
    (("L4",), (), ["L4", "T4"], "Colab GPU (Tesla T4)"),  # Pro GPU refused: a free T4
    (("L4", "T4"), (), ["L4", "T4", None], "Colab CPU (no GPU was available)"),  # none free: CPU
    ((), ("L4", "T4"), ["L4", "T4", None], "Colab CPU (no GPU was available)"),  # granted but no GPU inside
])
def test_gpu_fallback_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refuse: tuple[str, ...],
                            empty: tuple[str, ...], tried: list[str | None], runtime: str) -> None:
    fake = GpuFakeColab(refuse, empty)
    be = _backend(monkeypatch, fake)
    run = _run(tmp_path)
    run.manifest.backend_state["gpu"] = "L4"
    handle = be.start(RunContext(run=run, profile=load_profile("python"), attempt_id="a001", timeout_s=60, datasets=[]))
    news = [c for c in fake.calls if c[1] == "new"]
    assert [c[c.index("--gpu") + 1] if "--gpu" in c else None for c in news] == tried
    assert run.manifest.backend_state["runtime"] == runtime
    # sessions without the GPU were stopped; only the one in use is still owned
    assert run.manifest.backend_state["owned_sessions"] == [handle.state["session"]]
    assert len([c for c in fake.calls if c[1] == "stop"]) == len(empty)


def test_gpu_request_does_not_hide_a_sign_in_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeColab(fail={"new": CmdResult(1, "", "google.auth: invalid_grant, Reauthentication is needed")})
    be = _backend(monkeypatch, fake)
    run = _run(tmp_path)
    run.manifest.backend_state["gpu"] = "T4"
    with pytest.raises(LabPilotError) as exc:
        be.start(RunContext(run=run, profile=load_profile("python"), attempt_id="a001", timeout_s=60, datasets=[]))
    assert exc.value.code == ErrorCode.AUTH_REQUIRED and len(fake.calls) == 1


def test_exec_success_failure_and_stop_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeColab(outputs={
        "c1": [{"output_type": "stream", "name": "stdout", "text": "42\n"}],
        "c2": [{"output_type": "error", "ename": "ZeroDivisionError", "evalue": "division by zero",
                "traceback": ["\x1b[31mTraceback\x1b[0m", "ZeroDivisionError: division by zero"]}],
    })
    be = _backend(monkeypatch, fake)
    run = _run(tmp_path)
    ctx = RunContext(run=run, profile=load_profile("python"), attempt_id="a001", timeout_s=60, datasets=[])
    work = tmp_path / "w"
    work.mkdir()
    handle = RuntimeHandle("colab-cli", "a001", state={"session": "labpilot-x", "work": str(work)})
    cells = [CellRequest(c, "T1", "print(1)", "h") for c in ("c1", "c2", "c3")]
    recs = be.execute_cells(handle, ctx, cells, lambda e: None)
    assert [r.status for r in recs] == ["passed", "failed", "blocked"]
    assert recs[0].stdout == "42\n"
    assert recs[1].error_type == "ZeroDivisionError" and "\x1b" not in (recs[1].traceback or "")
    # exit code 0 from `colab exec` did NOT count as success for c2, and c3 never ran
    assert fake.ran == ["c1", "c2"] and recs[2].error_code == "UPSTREAM_FAILED"
    assert sum(1 for c in fake.calls if c[1] == "exec") == 1  # one call for the batch, marker included
    assert run.manifest.backend_state["last_marker"] == "c1"


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("[colab] Session 'x' appears to be lost (404/401). Cleaning up.", ErrorCode.RUNTIME_DISCONNECTED),
        ("429 Resource exhausted: quota", ErrorCode.QUOTA_EXHAUSTED),
        ("google.auth.exceptions.DefaultCredentialsError", ErrorCode.AUTH_REQUIRED),
    ],
)
def test_transport_failure_classification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str,
                                          code: ErrorCode) -> None:
    be = _backend(monkeypatch, FakeColab(fail={"exec": CmdResult(1, "", stderr)}))
    run = _run(tmp_path)
    ctx = RunContext(run=run, profile=load_profile("python"), attempt_id="a001", timeout_s=60, datasets=[])
    work = tmp_path / "w"
    work.mkdir()
    handle = RuntimeHandle("colab-cli", "a001", state={"session": "labpilot-x", "work": str(work)})
    recs = be.execute_cells(handle, ctx, [CellRequest("c1", "T1", "1", "h")], lambda e: None)
    assert recs[0].status == "failed" and recs[0].error_code == str(code)


def test_never_stops_sessions_it_does_not_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeColab()
    be = _backend(monkeypatch, fake)
    run = _run(tmp_path)
    ctx = RunContext(run=run, profile=load_profile("python"), attempt_id="a001", timeout_s=60, datasets=[])
    be.stop(RuntimeHandle("colab-cli", "a001", state={"session": "my-own-notebook"}), ctx)
    run.manifest.backend_state["session"] = "other"
    assert be.cancel(run) is False
    assert not any(c[1] == "stop" for c in fake.calls)


def _handle(tmp_path: Path) -> RuntimeHandle:
    work = tmp_path / "w"
    work.mkdir()
    return RuntimeHandle("colab-cli", "a001", state={"session": "labpilot-x", "work": str(work)})


def test_cells_run_in_batches_with_per_cell_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeColab(outputs={f"c{i}": [{"output_type": "stream", "name": "stdout", "text": f"out {i}\n"}]
                              for i in range(1, 8)})
    be = _backend(monkeypatch, fake)
    run = _run(tmp_path)
    ctx = RunContext(run=run, profile=load_profile("python"), attempt_id="a001", timeout_s=60, datasets=[])
    events: list[dict[str, Any]] = []
    recs = be.execute_cells(_handle(tmp_path), ctx, [CellRequest(f"c{i}", "T1", "1", "h") for i in range(1, 8)],
                            events.append)
    execs = [c for c in fake.calls if c[1] == "exec"]
    assert len(execs) == 2  # 7 cells, BATCH_CELLS = 5
    assert execs[0][execs[0].index("--timeout") + 1] == "60"  # the idle limit stays per cell
    assert [r.status for r in recs] == ["passed"] * 7
    assert [r.stdout for r in recs] == [f"out {i}\n" for i in range(1, 8)]
    assert run.manifest.backend_state["last_marker"] == "c7"
    assert [e["cell_id"] for e in events if e["event"] == "cell"] == [f"c{i}" for i in range(1, 8)]
    assert [e["cell_ids"] for e in events if e["event"] == "cell_start"] == [
        [f"c{i}" for i in range(1, 6)], ["c6", "c7"]]


def test_call_stopped_mid_batch_fails_that_cell_and_blocks_the_rest(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """FAKE `colab exec` that hits its idle --timeout in the second lab cell: the notebook holds only what ran."""

    def fake(args: list[str], **_: Any) -> CmdResult:
        path = Path(args[args.index("-f") + 1])
        nb = nbformat.read(str(path), as_version=4)
        gate = [{"output_type": "stream", "name": "stdout", "text": "LABPILOT_GATE {} 2026-01-01T00:00:0{}.000000Z\n"}]
        nb.cells[0].outputs = [new_output(**{**gate[0], "text": gate[0]["text"].format("open", 0)})]
        nb.cells[1].outputs = [new_output("stream", name="stdout", text="first\n")]
        nb.cells[2].outputs = [new_output(**{**gate[0], "text": gate[0]["text"].format("passed", 3)})]
        nb.cells[3].outputs = [new_output("stream", name="stdout", text="partial\n")]
        nbformat.write(nb, str(path.with_name(path.stem + "_output.ipynb")))
        return CmdResult(1, "", "TimeoutError: Timeout waiting for output: no message from the kernel for 60s.")

    monkeypatch.setattr(colab_cli, "run_cmd", fake)
    be = ColabCliBackend(command=["colab"])
    run = _run(tmp_path)
    ctx = RunContext(run=run, profile=load_profile("python"), attempt_id="a001", timeout_s=60, datasets=[])
    recs = be.execute_cells(_handle(tmp_path), ctx, [CellRequest(f"c{i}", "T1", "1", "h") for i in range(1, 4)],
                            lambda e: None)
    assert [r.status for r in recs] == ["passed", "failed", "blocked"]
    assert recs[0].started_at == "2026-01-01T00:00:00.000000Z" and recs[0].ended_at == "2026-01-01T00:00:03.000000Z"
    assert recs[1].error_code == str(ErrorCode.TIMEOUT)  # partial output of an unfinished cell is not a pass
    assert run.manifest.backend_state["last_marker"] == "c1"
