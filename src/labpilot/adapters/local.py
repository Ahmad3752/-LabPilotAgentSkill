"""Local Jupyter fallback backend: one persistent kernel per attempt, in a credential-scrubbed subprocess.

This is the always-available fallback when Google Colab is not configured. It
isolates packages, NOT filesystem privileges, so it is meant for the student's
own lab code on their own machine; reports always label it "local-jupyter".
"""

from __future__ import annotations

import json
import queue
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

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
from labpilot.errors import ErrorCode
from labpilot.models import CellExecution
from labpilot.procutil import kill_tree, popen_group, run_cmd, scrubbed_env
from labpilot.workspace import Run, atomic_write_json, read_json

RUNNER = Path(__file__).resolve().parent.parent / "notebooks" / "kernel_runner.py"
KERNEL_NAME = "labpilot-runtime"
HARD_TIMEOUT_GRACE_S = 15


def runtime_python(profile: RuntimeProfile) -> str:
    override = (load_config().get("runtime_python") or {}).get(profile.name)
    return override or sys.executable


class LocalBackend:
    name = "local"
    label = "local-jupyter"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            persistent_kernel=True,
            rich_output=True,
            native_ui=False,
            file_transfer=True,
            resume_without_replay=False,
            host_mediated=False,
            network_isolated=False,
        )

    # ------------------------------------------------------------ preflight
    def preflight(self, run: Run, profile: RuntimeProfile) -> PreflightResult:
        py = runtime_python(profile)
        imports = ["nbclient", "nbformat", "ipykernel", *profile.import_checks]
        res = run_cmd([py, "-c", "; ".join(f"import {m}" for m in imports) + "; import sys; print(sys.version.split()[0])"],
                      timeout=120)
        check = Check(
            "kernel_python",
            res.ok,
            f"Python {res.stdout.strip()} with {', '.join(imports)}" if res.ok else res.stderr.strip()[-400:],
            fix=f"{py} -m pip install " + " ".join(profile.python_packages or profile.import_checks),
        )
        return PreflightResult(self.name, check.ok, [check])

    # ------------------------------------------------------------ lifecycle
    def start(self, ctx: RunContext) -> RuntimeHandle:
        attempt_dir = ctx.run.exec_dir / ctx.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        return RuntimeHandle(backend=self.name, attempt_id=ctx.attempt_id, state={"attempt_dir": str(attempt_dir)})

    def stage_inputs(self, handle: RuntimeHandle, ctx: RunContext) -> StageResult:
        staged: dict[str, str] = {}
        problems = []
        for d in ctx.datasets:
            if d.staged_path and (ctx.run.dir / d.staged_path).exists():
                staged[d.expected_name] = d.expected_name  # relative to the kernel cwd (the run's data dir)
            elif d.status in ("found", "mapped"):
                problems.append(f"{d.expected_name} was not staged")
        return StageResult(ok=not problems, staged=staged, problems=problems)

    def _write_kernelspec(self, attempt_dir: Path, python_cmd: str) -> Path:
        share = attempt_dir / "jupyter"
        kdir = share / "kernels" / KERNEL_NAME
        kdir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            kdir / "kernel.json",
            {"argv": [python_cmd, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
             "display_name": "LabPilot runtime", "language": "python"},
        )
        return share

    def execute_cells(
        self, handle: RuntimeHandle, ctx: RunContext, cells: list[CellRequest], on_event: OnEvent
    ) -> list[CellExecution]:
        run = ctx.run
        attempt_dir = Path(handle.state["attempt_dir"])
        shutil.copy2(run.source_notebook, attempt_dir / "source.ipynb")
        records = attempt_dir / "records.jsonl"
        records.unlink(missing_ok=True)
        python_cmd = runtime_python(ctx.profile)
        share = self._write_kernelspec(attempt_dir, python_cmd)
        tmp_dir = run.dir / "tmp"
        tmp_dir.mkdir(exist_ok=True)
        run.data_dir.mkdir(parents=True, exist_ok=True)
        request = {
            "run_id": run.id,
            "attempt_id": ctx.attempt_id,
            "backend": self.label,
            "notebook": str(attempt_dir / "source.ipynb"),
            "out_notebook": str(attempt_dir / "executed.ipynb"),
            "records": str(records),
            "cell_ids": [c.cell_id for c in cells],
            "cwd": str(run.data_dir),
            "timeout": ctx.timeout_s,
            "kernel_name": KERNEL_NAME,
            "max_output_bytes": 200_000,
            "stop_on_error": True,
            # inline backend so figures become real image/png outputs; kernel temp files stay inside the
            # run workspace (keeps host home paths out of tracebacks/warnings where possible)
            "env": {"LABPILOT_DATA_DIR": str(run.data_dir), "MPLBACKEND": "module://matplotlib_inline.backend_inline",
                    "PYTHONHASHSEED": "0", "TMP": str(tmp_dir), "TEMP": str(tmp_dir), "TMPDIR": str(tmp_dir)},
        }
        req_path = attempt_dir / "request.json"
        atomic_write_json(req_path, request)
        env = scrubbed_env({"JUPYTER_PATH": str(share), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        proc = popen_group([python_cmd, str(RUNNER), "--request", str(req_path)], stdout=-1, stderr=-1, text=True,
                           encoding="utf-8", errors="replace", env=env)
        atomic_write_json(attempt_dir / "runner.json", {"pid": proc.pid, "started": time.time()})

        stderr_chunks: list[str] = []
        events: queue.Queue[str | None] = queue.Queue()

        def pump_stdout() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                events.put(raw)
            events.put(None)

        def pump_stderr() -> None:
            assert proc.stderr is not None
            for raw in proc.stderr:
                stderr_chunks.append(raw)

        threading.Thread(target=pump_stdout, daemon=True).start()
        threading.Thread(target=pump_stderr, daemon=True).start()
        # Hard wall-clock limit enforced by the host: kernel interrupts do not stop every blocking call
        # (notably on Windows), so a cell exceeding timeout + grace gets its owned runtime killed.
        hard_limit = ctx.timeout_s + HARD_TIMEOUT_GRACE_S
        current: str | None = None
        cell_started = time.monotonic()
        startup_deadline = time.monotonic() + 300
        timed_out: str | None = None
        try:
            while True:
                try:
                    raw = events.get(timeout=1.0)
                except queue.Empty:
                    now = time.monotonic()
                    if (current and now - cell_started > hard_limit) or (current is None and now > startup_deadline):
                        timed_out = current or "<kernel startup>"
                        kill_tree(proc.pid)
                        break
                    continue
                if raw is None:
                    break
                line = raw.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") == "cell_start":
                    current, cell_started = ev.get("cell_id"), time.monotonic()
                elif ev.get("event") == "cell":
                    current = None
                    startup_deadline = float("inf")
                elif ev.get("event") == "runtime":
                    startup_deadline = float("inf")
                on_event(ev)
            proc.wait(timeout=60)
        except KeyboardInterrupt:
            kill_tree(proc.pid)
            raise
        stderr = "".join(stderr_chunks)
        (attempt_dir / "runner.stderr.txt").write_text(stderr[-50_000:], encoding="utf-8")
        (attempt_dir / "runner.json").unlink(missing_ok=True)

        results = _read_records(records)
        done = {r.cell_id for r in results}
        lost = [c for c in cells if c.cell_id not in done]
        if lost:
            reason = "runtime process exited unexpectedly"
            if stderr.strip():
                reason += f": {stderr.strip().splitlines()[-1][:300]}"
            for i, c in enumerate(lost):
                first = i == 0
                is_timeout = first and timed_out == c.cell_id
                results.append(
                    CellExecution(
                        run_id=run.id, attempt_id=ctx.attempt_id, cell_id=c.cell_id, task_id=c.task_id,
                        code_sha256=c.code_sha256, backend=self.label,
                        status="failed" if first else "blocked",
                        error_code=(str(ErrorCode.TIMEOUT) if is_timeout else str(ErrorCode.RUNTIME_DISCONNECTED))
                        if first else "UPSTREAM_FAILED",
                        error_type=("CellTimeoutError" if is_timeout else "RuntimeLost") if first else None,
                        error_message=(f"Cell exceeded {ctx.timeout_s}s; the runtime was stopped." if is_timeout
                                       else reason) if first else "Not executed because the runtime was stopped.",
                    )
                )
        handle.identity = results[0].runtime if results and results[0].runtime else {}
        return results

    def executed_notebook(self, handle: RuntimeHandle) -> Path:
        return Path(handle.state["attempt_dir"]) / "executed.ipynb"

    def cancel(self, run: Run) -> bool:
        stopped = False
        for info in run.exec_dir.glob("*/runner.json"):
            kill_tree(int(read_json(info)["pid"]))
            info.unlink(missing_ok=True)
            stopped = True
        return stopped

    def health(self, handle: RuntimeHandle) -> RuntimeHealth:
        # The kernel lives only for the duration of one runner invocation.
        return RuntimeHealth(alive=False, kernel_state_intact=False, detail="local kernels are per-attempt; replay required")

    def stop(self, handle: RuntimeHandle, ctx: RunContext) -> None:
        return None


def _read_records(path: Path) -> list[CellExecution]:
    out: list[CellExecution] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn final line after a crash: the cell is treated as not executed
        out.append(CellExecution.model_validate(data))
    return out
