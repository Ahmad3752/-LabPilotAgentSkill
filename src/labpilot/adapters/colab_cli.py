"""Official Google Colab CLI adapter (google-colab-cli, verified against 0.8.0 `--help` and source).

Verified upstream behaviour this adapter relies on:
  * `colab new -s NAME` allocates a CPU runtime when --gpu/--tpu are omitted.
  * `colab upload -s NAME LOCAL REMOTE` / `colab download -s NAME REMOTE LOCAL`.
  * `colab exec -s NAME -f cell.ipynb --timeout SECONDS` runs each code cell in the
    session's persistent kernel (cwd /content) and writes `cell_output.ipynb` next
    to the input with real nbformat outputs (stream / display_data /
    execute_result / error). It exits 0 even when a cell raises, so LabPilot
    decides success from the output objects, never from the exit code.
  * `colab stop -s NAME` releases the runtime.
  * Upstream lists Linux and macOS; 0.8.0 was also verified natively on Windows 11.
  * Authentication is the CLI's own OAuth (paste-code) or ADC flow; LabPilot never
    sees credentials. Non-interactive calls get stdin=/dev/null, so a missing
    login surfaces as WAITING_FOR_AUTH instead of a hang.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Literal

import nbformat
from nbformat.v4 import new_code_cell, new_notebook

from labpilot.adapters.base import (
    BackendCapabilities,
    CellRequest,
    Check,
    OnEvent,
    PreflightResult,
    RunContext,
    RuntimeHandle,
    RuntimeHealth,
    StageResult,
)
from labpilot.config import RuntimeProfile, load_config
from labpilot.errors import ErrorCode, ExitCode, LabPilotError, strip_ansi
from labpilot.models import CellExecution, utcnow
from labpilot.notebooks.kernel_runner import PROBE, bound_outputs, summarize
from labpilot.procutil import CmdResult, run_cmd
from labpilot.workspace import Run

SESSION_PREFIX = "labpilot-"
REMOTE_DIR = "/content"
AUTH_HINTS = re.compile(r"(authorization code|Enter the authorization|accounts\.google\.com|oauth|invalid_grant|"
                        r"Reauthentication|credentials were not found|DefaultCredentialsError|401)", re.I)
QUOTA_HINTS = re.compile(r"(quota|resource exhausted|429|usage limit|no backend|not available|compute units)", re.I)
LOST_HINTS = re.compile(r"(appears to be lost|session .* not found|connection (refused|closed|reset)|websocket)", re.I)


class ColabCliBackend:
    name = "colab-cli"

    def __init__(self, command: list[str] | None = None) -> None:
        configured = command or load_config().get("colab_cli", {}).get("command")
        exe = shutil.which("colab")
        self.base = list(configured) if configured else ([exe] if exe else [])

    # ------------------------------------------------------------ helpers
    def _path(self, local: Path) -> str:
        return str(local.resolve())

    def _colab(self, *args: str, timeout: float = 600) -> CmdResult:
        if not self.base:
            return CmdResult(127, "", "colab CLI not configured")
        return run_cmd([*self.base, *args], timeout=timeout)

    def _classify_transport(self, res: CmdResult) -> ErrorCode:
        text = strip_ansi(res.stdout + "\n" + res.stderr)
        if res.timed_out:
            return ErrorCode.TIMEOUT
        if LOST_HINTS.search(text):  # upstream prints "appears to be lost (404/401)" for dead sessions
            return ErrorCode.RUNTIME_DISCONNECTED
        if AUTH_HINTS.search(text):
            return ErrorCode.AUTH_REQUIRED
        if QUOTA_HINTS.search(text):
            return ErrorCode.QUOTA_EXHAUSTED
        return ErrorCode.RUNTIME_DISCONNECTED

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            persistent_kernel=True,
            rich_output=True,
            native_ui=False,  # CLI execution is not visible in the Colab UI
            file_transfer=True,
            resume_without_replay=True,  # session kernels persist between exec calls (verified by marker probe)
            host_mediated=False,
            network_isolated=False,
            platform_supported=True,  # Linux/macOS upstream; native Windows verified with 0.8.0
        )

    # ------------------------------------------------------------ preflight
    def preflight(self, run: Run, profile: RuntimeProfile) -> PreflightResult:
        checks: list[Check] = []
        ver = self._colab("version", timeout=120)
        checks.append(Check("colab_cli", ver.ok, strip_ansi(ver.stdout.strip() or ver.stderr.strip())[-200:],
                            fix="Install: labpilot setup --backend colab-cli --install-colab-cli (needs Python >= 3.12)."))
        if not ver.ok:
            return PreflightResult(self.name, False, checks)
        sessions = self._colab("sessions", timeout=120)
        text = strip_ansi(sessions.stdout + sessions.stderr)
        authed = sessions.ok and not AUTH_HINTS.search(text)
        checks.append(Check("colab_auth", authed, "authenticated" if authed else "sign-in required",
                            fix="Run `colab sessions` once in an interactive terminal and complete Google's sign-in."))
        consent = run.manifest.consent
        needs_upload = any(d.status in ("found", "mapped") for d in run.manifest.datasets)
        checks.append(Check("consent_cloud_compute", consent.cloud_compute, "user consent to allocate a Colab runtime",
                            fix="Ask the user, then pass --allow-cloud to `labpilot preflight`."))
        if needs_upload:
            checks.append(Check("consent_upload", consent.upload_datasets, "user consent to upload datasets to Colab",
                                fix="Ask the user, then pass --allow-upload to `labpilot preflight`."))
        hint = None if authed else "WAITING_FOR_AUTH"
        return PreflightResult(self.name, all(c.ok for c in checks if c.required), checks, state_hint=hint)

    # ------------------------------------------------------------ lifecycle
    def start(self, ctx: RunContext) -> RuntimeHandle:
        m = ctx.run.manifest
        if not m.consent.cloud_compute:
            raise LabPilotError(ErrorCode.CONSENT_REQUIRED, "Colab compute needs explicit user consent.", exit_code=ExitCode.CONFIG)
        session = f"{SESSION_PREFIX}{ctx.run.id}-{uuid.uuid4().hex[:4]}".lower()
        res = self._colab("new", "-s", session, timeout=900)
        if not res.ok:
            code = self._classify_transport(res)
            raise LabPilotError(code, f"colab new failed: {strip_ansi(res.stderr or res.stdout)[-400:]}",
                                exit_code=ExitCode.RUNTIME)
        owned = m.backend_state.setdefault("owned_sessions", [])
        owned.append(session)
        m.backend_state["session"] = session
        ctx.run.save()
        work = ctx.run.exec_dir / ctx.attempt_id / "colab"
        work.mkdir(parents=True, exist_ok=True)
        handle = RuntimeHandle(backend=self.name, attempt_id=ctx.attempt_id, state={"session": session, "work": str(work)})
        out = self._exec_code(handle, PROBE, "probe")
        for o in out.get("outputs", []):
            text = o.get("text", "") if o.get("output_type") == "stream" else ""
            for line in (text if isinstance(text, str) else "".join(text)).splitlines():
                if line.startswith("LABPILOT_PROBE "):
                    handle.identity = json.loads(line[len("LABPILOT_PROBE "):])
        handle.identity["colab_session"] = session
        return handle

    def stage_inputs(self, handle: RuntimeHandle, ctx: RunContext) -> StageResult:
        staged: dict[str, str] = {}
        problems: list[str] = []
        expected: dict[str, str] = {}
        for d in ctx.datasets:
            if d.status not in ("found", "mapped") or not d.staged_path:
                continue
            if not d.cloud_upload_consent:
                problems.append(f"{d.expected_name}: no consent to upload")
                continue
            remote = f"{REMOTE_DIR}/{d.expected_name}"
            res = self._colab("upload", "-s", handle.state["session"], self._path(ctx.run.dir / d.staged_path), remote,
                              timeout=1800)
            if not res.ok:
                problems.append(f"{d.expected_name}: upload failed ({strip_ansi(res.stderr)[-200:]})")
                continue
            staged[d.expected_name] = remote
            expected[remote] = d.sha256 or ""
        if expected:
            verify = (
                "import hashlib, json\n"
                f"_paths = {json.dumps(list(expected))}\n"
                "print('LABPILOT_HASHES ' + json.dumps({p: hashlib.sha256(open(p,'rb').read()).hexdigest() for p in _paths}))\n"
            )
            out = self._exec_code(handle, verify, "verify-upload")
            got: dict[str, str] = {}
            for o in out.get("outputs", []):
                text = o.get("text", "")
                for line in (text if isinstance(text, str) else "".join(text)).splitlines():
                    if line.startswith("LABPILOT_HASHES "):
                        got = json.loads(line[len("LABPILOT_HASHES "):])
            for remote, digest in expected.items():
                if got.get(remote) != digest:
                    problems.append(f"{remote}: integrity check failed after upload")
        return StageResult(ok=not problems, staged=staged, problems=problems)

    def _exec_code(self, handle: RuntimeHandle, source: str, cell_id: str, timeout: float = 600) -> dict[str, Any]:
        """Run one code cell via `colab exec -f <cell>.ipynb` and return the executed cell dict."""
        work = Path(handle.state["work"])
        nb = new_notebook()
        cell = new_code_cell(source)
        cell.id = re.sub(r"[^A-Za-z0-9_-]", "-", cell_id)[:64]
        nb.cells.append(cell)
        src = work / f"{cell.id}.ipynb"
        out = work / f"{cell.id}_output.ipynb"
        out.unlink(missing_ok=True)
        nbformat.write(nb, str(src))
        res = self._colab("exec", "-s", handle.state["session"], "-f", self._path(src), "--timeout", str(int(timeout)),
                          timeout=timeout + 120)
        (work / f"{cell.id}.log").write_text(strip_ansi(res.stdout[-20000:] + "\n--- stderr ---\n" + res.stderr[-20000:]),
                                             encoding="utf-8")
        if not out.exists():
            code = self._classify_transport(res)
            raise LabPilotError(code, f"colab exec produced no output notebook ({code}).", exit_code=ExitCode.RUNTIME,
                                details={"stderr": strip_ansi(res.stderr)[-500:]})
        executed = nbformat.read(str(out), as_version=4)
        c = next((x for x in executed.cells if x.cell_type == "code"), None)
        if c is None:
            raise LabPilotError(ErrorCode.RUNTIME_DISCONNECTED, "colab exec output notebook has no code cell.",
                                exit_code=ExitCode.RUNTIME)
        return {"outputs": [dict(o) for o in c.get("outputs", [])], "execution_count": c.get("execution_count"),
                "returncode": res.returncode}

    def execute_cells(
        self, handle: RuntimeHandle, ctx: RunContext, cells: list[CellRequest], on_event: OnEvent
    ) -> list[CellExecution]:
        results: list[CellExecution] = []
        failed = False
        for c in cells:
            base: dict[str, Any] = dict(run_id=ctx.run.id, attempt_id=ctx.attempt_id, cell_id=c.cell_id, task_id=c.task_id,
                        code_sha256=c.code_sha256, backend=self.name, runtime=handle.identity)
            if failed:
                results.append(CellExecution(**base, status="blocked", error_code="UPSTREAM_FAILED",
                                             error_message="Not executed because an earlier cell failed."))
                continue
            on_event({"event": "cell_start", "cell_id": c.cell_id})
            started = utcnow()
            try:
                cell = self._exec_code(handle, c.source, c.cell_id, timeout=ctx.timeout_s)
            except LabPilotError as exc:
                results.append(CellExecution(**base, status="failed", started_at=started, ended_at=utcnow(),
                                             error_code=str(exc.code), error_type="TransportError", error_message=exc.message))
                on_event({"event": "cell", "cell_id": c.cell_id, "status": "failed", "error_type": str(exc.code)})
                failed = True
                continue
            outputs = cell["outputs"]
            truncated = bound_outputs(outputs, 200_000)
            s = summarize(outputs)
            status: Literal["passed", "failed"] = "failed" if s["error"] else "passed"
            results.append(CellExecution(
                **base, status=status, execution_count=cell.get("execution_count"), started_at=started, ended_at=utcnow(),
                stdout=s["stdout"][-20000:], stderr=s["stderr"][-20000:], outputs=outputs, output_truncated=truncated,
                error_type=s["error"]["ename"] if s["error"] else None,
                error_message=s["error"]["evalue"] if s["error"] else None,
                traceback=s["error"]["traceback"] if s["error"] else None,
            ))
            on_event({"event": "cell", "cell_id": c.cell_id, "status": status})
            if status == "failed":
                failed = True
            else:
                # marker lets resume prove the remote kernel still holds this state
                self._exec_code(handle, f"_labpilot_marker = {c.cell_id!r}", f"marker-{c.cell_id}", timeout=60)
                ctx.run.manifest.backend_state["last_marker"] = c.cell_id
        return results

    def health(self, handle: RuntimeHandle) -> RuntimeHealth:
        res = self._colab("status", "-s", handle.state["session"], timeout=120)
        if not res.ok:
            return RuntimeHealth(False, False, strip_ansi(res.stderr)[-200:])
        try:
            out = self._exec_code(handle, "print('LABPILOT_MARKER', globals().get('_labpilot_marker'))", "health", timeout=60)
        except LabPilotError as exc:
            return RuntimeHealth(False, False, exc.message)
        text = "".join(o.get("text", "") for o in out["outputs"] if o.get("output_type") == "stream")
        marker = text.split("LABPILOT_MARKER", 1)[-1].strip()
        return RuntimeHealth(True, marker not in ("", "None"), f"marker={marker}")

    def cancel(self, run: Run) -> bool:
        session = run.manifest.backend_state.get("session")
        if session and session in run.manifest.backend_state.get("owned_sessions", []):
            self._colab("stop", "-s", session, timeout=300)
            return True
        return False

    def stop(self, handle: RuntimeHandle, ctx: RunContext) -> None:
        session = handle.state.get("session", "")
        owned = ctx.run.manifest.backend_state.get("owned_sessions", [])
        if session.startswith(SESSION_PREFIX) and session in owned:  # never stop sessions LabPilot did not create
            self._colab("stop", "-s", session, timeout=300)
            owned.remove(session)
            ctx.run.manifest.backend_state.pop("session", None)
            ctx.run.save()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
