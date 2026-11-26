"""Colab CLI adapter parsing/decision logic against FAKE subprocess responses (unit tests only).

These fakes never count as Colab verification; the real opt-in smoke test is
tests/integration/test_colab_cloud_smoke.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import nbformat
import pytest
from nbformat.v4 import new_output

from labpilot.adapters import colab_cli
from labpilot.adapters.base import CellRequest, RunContext, RuntimeHandle
from labpilot.adapters.colab_cli import ColabCliBackend
from labpilot.config import load_profile
from labpilot.errors import ErrorCode, LabPilotError
from labpilot.models import RunManifest
from labpilot.procutil import CmdResult
from labpilot.workspace import create_run


class FakeColab:
    """Simulates `colab` invocations: writes <cell>_output.ipynb the way google-colab-cli 0.8.0 does."""

    def __init__(self, outputs: dict[str, list[dict[str, Any]]] | None = None, fail: dict[str, CmdResult] | None = None):
        self.calls: list[list[str]] = []
        self.outputs = outputs or {}
        self.fail = fail or {}

    def __call__(self, args: list[str], **_: Any) -> CmdResult:
        self.calls.append(args)
        sub = args[1]
        if sub in self.fail:
            return self.fail[sub]
        if sub == "exec":
            path = Path(args[args.index("-f") + 1])
            nb = nbformat.read(str(path), as_version=4)
            cell = nb.cells[0]
            outs = self.outputs.get(cell.id, [{"output_type": "stream", "name": "stdout", "text": "ok\n"}])
            cell.outputs = [new_output(**o) for o in outs]
            nbformat.write(nb, str(path.with_name(path.stem + "_output.ipynb")))
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
    assert fake.calls[0][:3] == ["colab", "new", "-s"] and "--gpu" not in fake.calls[0]  # CPU runtime only
    be.stop(handle, ctx)
    assert ["colab", "stop", "-s", session] in fake.calls


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
    # exit code 0 from `colab exec` did NOT count as success for c2
    assert not any("c3" in " ".join(c) for c in fake.calls)


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
