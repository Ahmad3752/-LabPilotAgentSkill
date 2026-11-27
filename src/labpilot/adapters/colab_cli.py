"""Official Google Colab CLI adapter (google-colab-cli, verified against 0.8.0 `--help` and source).

Verified upstream behaviour this adapter relies on:
  * `colab new -s NAME` allocates a CPU runtime when --gpu/--tpu are omitted.
  * `colab upload -s NAME LOCAL REMOTE` / `colab download -s NAME REMOTE LOCAL`.
  * `colab exec -s NAME -f cells.ipynb --timeout SECONDS` first runs `os.chdir('/content')`,
    then every code cell in order in the session's persistent kernel, and writes
    `cells_output.ipynb` next to the input with real nbformat outputs (stream /
    display_data / execute_result / error). A cell that raises does NOT stop it: the
    error is only an output and the exit code stays 0. Only a transport error stops
    it (no kernel message for --timeout seconds, which is an idle limit, a kernel
    restart, a lost connection); the output notebook is then still written with the
    cells run so far. So LabPilot decides success from the output objects, never
    from the exit code, and runs several cells per call behind a kernel-side gate
    (`CONTROL_SRC`) that blanks every cell after a failure.
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
GONE_HINTS = re.compile(r"(appears to be lost|session .* not found)", re.I)  # nothing left to release
LOST_HINTS = re.compile(r"(appears to be lost|session .* not found|connection (refused|closed|reset)|websocket)", re.I)
BATCH_CELLS = 5  # lab cells per `colab exec` call: each call costs seconds of fixed overhead
BATCH_WALL_CAP_S = 3600
CONTROL = "# labpilot-control"  # first line of LabPilot's own cells; the gate never blanks them
GATE = "LABPILOT_GATE "
# A control cell runs before, between and after the lab cells of one `colab exec` call. It reports
# how the previous lab cell ended (passed / failed / skipped / lost) with a kernel timestamp, sets
# `_labpilot_marker` only after a passed cell, and installs an IPython input transformer (the gate)
# that turns every later lab cell into an empty cell once one failed, so their code never runs.
# A lab cell counts as failed when it raised or showed a traceback (an `error` output), the same
# rule LabPilot applies to the outputs. The lab cells themselves are sent unchanged.
CONTROL_SRC = '''# labpilot-control
def _labpilot_control(token, prev, last, workdir):
    import datetime, os
    ip = get_ipython()
    st = getattr(ip, "_labpilot_state", None)
    if prev is None:
        outcome = "open"
        st = ip._labpilot_state = {"token": token, "closed": False, "tb": False}
    elif st is None or st["token"] != token:  # the kernel restarted: its state is gone
        outcome = "lost"
        st = ip._labpilot_state = {"token": token, "closed": True, "tb": False}
    elif st["closed"]:
        outcome = "skipped"
    elif st["tb"] or not ip.last_execution_succeeded:
        outcome = "failed"
        st["closed"] = True
    else:
        outcome = "passed"
        ip.user_ns["_labpilot_marker"] = prev
    st["tb"] = False

    def _labpilot_gate(lines):
        s = getattr(ip, "_labpilot_state", None)
        if s and s["closed"] and not (lines and lines[0].startswith("# labpilot-control")):
            return []
        return lines

    t = ip.input_transformers_cleanup
    t[:] = [f for f in t if getattr(f, "__name__", "") != "_labpilot_gate"] + ([] if last else [_labpilot_gate])
    show = getattr(ip, "_showtraceback", None)
    if show is not None and not getattr(show, "_labpilot", False):
        def _labpilot_tb(*args, **kwargs):
            s = getattr(ip, "_labpilot_state", None)
            if s is not None:
                s["tb"] = True
            return show(*args, **kwargs)
        _labpilot_tb._labpilot = True
        ip._showtraceback = _labpilot_tb
    os.chdir(workdir)  # each lab cell starts in /content, as it did with one `colab exec` per cell
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    print("LABPILOT_GATE", outcome, now)
'''


def control_cell(token: str, prev: str | None, last: bool) -> str:
    return f"{CONTROL_SRC}_labpilot_control({token!r}, {prev!r}, {last!r}, {REMOTE_DIR!r})\ndel _labpilot_control\n"


def _gate(outputs: list[dict[str, Any]]) -> tuple[str, str] | None:
    """(outcome, kernel timestamp) printed by a control cell, or None if it never ran."""
    for o in outputs:
        text = o.get("text", "") if o.get("output_type") == "stream" else ""
        for line in (text if isinstance(text, str) else "".join(text)).splitlines():
            if line.startswith(GATE):
                outcome, _, ts = line[len(GATE):].partition(" ")
                return outcome, ts.strip()
    return None


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
        if res.timed_out or "Timeout waiting for" in text:  # killed by us, or the CLI's idle --timeout
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

    def _exec_nb(self, handle: RuntimeHandle, cells: list[tuple[str, str]], name: str, idle_s: float,
                 wall_s: float) -> tuple[list[dict[str, Any]], CmdResult]:
        """Run (cell id, source) cells in one `colab exec -f <name>.ipynb`; return each code cell's outputs."""
        work = Path(handle.state["work"])
        nb = new_notebook()
        for cid, source in cells:
            cell = new_code_cell(source)
            cell.id = re.sub(r"[^A-Za-z0-9_-]", "-", cid)[:64]
            nb.cells.append(cell)
        stem = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64]
        src = work / f"{stem}.ipynb"
        out = work / f"{stem}_output.ipynb"
        out.unlink(missing_ok=True)
        nbformat.write(nb, str(src))
        res = self._colab("exec", "-s", handle.state["session"], "-f", self._path(src), "--timeout", str(int(idle_s)),
                          timeout=wall_s)
        (work / f"{stem}.log").write_text(strip_ansi(res.stdout[-20000:] + "\n--- stderr ---\n" + res.stderr[-20000:]),
                                          encoding="utf-8")
        if not out.exists():
            code = self._classify_transport(res)
            raise LabPilotError(code, f"colab exec produced no output notebook ({code}).", exit_code=ExitCode.RUNTIME,
                                details={"stderr": strip_ansi(res.stderr)[-500:]})
        executed = [x for x in nbformat.read(str(out), as_version=4).cells if x.cell_type == "code"]
        if len(executed) != len(cells):
            raise LabPilotError(ErrorCode.RUNTIME_DISCONNECTED, "colab exec output notebook does not match its input.",
                                exit_code=ExitCode.RUNTIME)
        return [{"outputs": [dict(o) for o in c.get("outputs", [])], "execution_count": c.get("execution_count")}
                for c in executed], res

    def _exec_code(self, handle: RuntimeHandle, source: str, cell_id: str, timeout: float = 600) -> dict[str, Any]:
        """Run one LabPilot cell (probe, upload check, health) in its own `colab exec` call."""
        cells, res = self._exec_nb(handle, [(cell_id, f"{CONTROL}\n{source}")], cell_id, timeout, timeout + 120)
        return {**cells[0], "returncode": res.returncode}

    def execute_cells(
        self, handle: RuntimeHandle, ctx: RunContext, cells: list[CellRequest], on_event: OnEvent
    ) -> list[CellExecution]:
        """Run the lab cells BATCH_CELLS per `colab exec` call, with the result one call per cell gave.

        Each cell keeps its own outputs and status, `_labpilot_marker` is set only after a passed cell,
        and no cell runs after one failed: the control cells' gate blanks them in the kernel and
        LabPilot records them as blocked.
        """
        results: list[CellExecution] = []
        failed = False
        for first in range(0, len(cells), BATCH_CELLS):
            batch = cells[first:first + BATCH_CELLS]
            if not failed:
                token = uuid.uuid4().hex[:12]
                nb: list[tuple[str, str]] = [("lp-ctl-0", control_cell(token, None, False))]
                for i, c in enumerate(batch):
                    nb += [(c.cell_id, c.source), (f"lp-ctl-{i + 1}", control_cell(token, c.cell_id, i == len(batch) - 1))]
                on_event({"event": "cell_start", "cell_id": batch[0].cell_id, "cell_ids": [c.cell_id for c in batch]})
                started = utcnow()
                transport: LabPilotError | None = None
                executed: list[dict[str, Any]] = []
                res = CmdResult(0, "", "")
                try:
                    executed, res = self._exec_nb(handle, nb, f"batch-{first:04d}", ctx.timeout_s,
                                                  min(len(batch) * ctx.timeout_s, BATCH_WALL_CAP_S) + 120)
                except LabPilotError as exc:
                    transport = exc
                ended = utcnow()
                opened = _gate(executed[0]["outputs"]) if executed else None
                prev_ts = opened[1] if opened else started
            for i, c in enumerate(batch):
                base = self._base(ctx, handle, c)
                if failed:
                    results.append(CellExecution(**base, status="blocked", error_code="UPSTREAM_FAILED",
                                                 error_message="Not executed because an earlier cell failed."))
                    continue
                gate = _gate(executed[2 * i + 2]["outputs"]) if executed else None
                if gate is None or gate[0] == "lost":  # the call stopped, or the kernel restarted, during this cell
                    if transport is None:
                        code = ErrorCode.RUNTIME_DISCONNECTED if gate else self._classify_transport(res)
                        transport = LabPilotError(code, "The Colab kernel restarted while this cell ran; its state is gone."
                                                  if gate else f"colab exec stopped while this cell ran ({code}).")
                    results.append(CellExecution(**base, status="failed", started_at=prev_ts, ended_at=ended,
                                                 error_code=str(transport.code), error_type="TransportError",
                                                 error_message=transport.message))
                    on_event({"event": "cell", "cell_id": c.cell_id, "status": "failed", "error_type": str(transport.code)})
                    failed = True
                    continue
                outputs = executed[2 * i + 1]["outputs"]
                truncated = bound_outputs(outputs, 200_000)
                s = summarize(outputs)
                status: Literal["passed", "failed"] = "failed" if s["error"] or gate[0] != "passed" else "passed"
                results.append(CellExecution(
                    **base, status=status, execution_count=executed[2 * i + 1].get("execution_count"),
                    started_at=prev_ts, ended_at=gate[1] or ended,
                    stdout=s["stdout"][-20000:], stderr=s["stderr"][-20000:], outputs=outputs, output_truncated=truncated,
                    error_type=s["error"]["ename"] if s["error"] else None,
                    error_message=s["error"]["evalue"] if s["error"] else (
                        None if status == "passed" else "The kernel reported this cell as not successful."),
                    traceback=s["error"]["traceback"] if s["error"] else None,
                ))
                prev_ts = gate[1] or ended
                on_event({"event": "cell", "cell_id": c.cell_id, "status": status})
                if status == "failed":
                    failed = True
                else:
                    ctx.run.manifest.backend_state["last_marker"] = c.cell_id  # the control cell set it in the kernel
        return results

    def _base(self, ctx: RunContext, handle: RuntimeHandle, c: CellRequest) -> dict[str, Any]:
        return dict(run_id=ctx.run.id, attempt_id=ctx.attempt_id, cell_id=c.cell_id, task_id=c.task_id,
                    code_sha256=c.code_sha256, backend=self.name, runtime=handle.identity)

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
        """Stop the current session and retry any release that was not confirmed earlier."""
        state = run.manifest.backend_state
        sessions = [*state.get("release_pending", []), state.get("session")]
        attempted = False
        for session in dict.fromkeys(s for s in sessions if s):
            if session in state.get("owned_sessions", []):
                self._release(run, session)
                attempted = True
        return attempted

    def stop(self, handle: RuntimeHandle, ctx: RunContext) -> None:
        session = handle.state.get("session", "")
        owned = ctx.run.manifest.backend_state.get("owned_sessions", [])
        if session.startswith(SESSION_PREFIX) and session in owned:  # never stop sessions LabPilot did not create
            self._release(ctx.run, session)

    def _release(self, run: Run, session: str) -> bool:
        """Clear ownership only after `colab stop` succeeded; otherwise keep the session as release-pending."""
        if not session.startswith(SESSION_PREFIX):
            return False
        res = self._colab("stop", "-s", session, timeout=300)
        state = run.manifest.backend_state
        pending = state.setdefault("release_pending", [])
        released = res.ok or bool(GONE_HINTS.search(strip_ansi(res.stdout + res.stderr)))
        if released:
            if session in state.get("owned_sessions", []):
                state["owned_sessions"].remove(session)
            if session in pending:
                pending.remove(session)
            if state.get("session") == session:
                state.pop("session")
        else:
            if session not in pending:
                pending.append(session)
            run.event("progress", message=f"release of Colab session {session} attempted, not confirmed")
        if not pending:
            state.pop("release_pending")
        run.save()
        return released


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
