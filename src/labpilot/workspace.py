"""Durable per-run workspace: layout, atomic writes, run lock, events and manifest persistence."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from labpilot.errors import ErrorCode, ExitCode, LabPilotError
from labpilot.models import RunManifest, RunState, utcnow

WORKSPACE_DIRNAME = ".labpilot"


# ------------------------------------------------------------------ io helpers


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # os.replace is atomic on the same volume (POSIX and Windows).
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: a reader (antivirus/indexer) may briefly hold the target.
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rmtree_owned(path: Path) -> None:
    """Remove a LabPilot-owned directory, clearing Windows read-only bits if needed."""
    import shutil
    import stat as _stat

    def onexc(func: Any, p: str, _exc: Any) -> None:
        os.chmod(p, _stat.S_IWRITE)
        func(p)

    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=onexc)  # type: ignore[call-arg]
        else:
            shutil.rmtree(path, onerror=onexc)


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def safe_stem(name: str) -> str:
    stem = Path(name).stem
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "lab"


# ------------------------------------------------------------------ run index (user-level)


def _index_path() -> Path:
    from labpilot.config import user_config_dir

    return user_config_dir() / "runs.json"


def register_run(run_id: str, lab_dir: Path) -> None:
    path = _index_path()
    try:
        data = read_json(path) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    data[run_id] = str(lab_dir.resolve())
    atomic_write_json(path, data)


def lookup_run(run_id: str) -> Path | None:
    path = _index_path()
    if not path.exists():
        return None
    try:
        lab = read_json(path).get(run_id)
    except (json.JSONDecodeError, OSError):
        return None
    return Path(lab) if lab else None


# ------------------------------------------------------------------ run workspace


class Run:
    """A run directory: <lab-dir>/.labpilot/runs/<run-id>/."""

    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        self.id = run_dir.name
        self._manifest: RunManifest | None = None

    # layout -------------------------------------------------------------
    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    @property
    def inputs_dir(self) -> Path:
        return self.dir / "inputs"

    @property
    def data_dir(self) -> Path:
        return self.dir / "data"

    @property
    def nb_dir(self) -> Path:
        return self.dir / "notebook"

    @property
    def source_notebook(self) -> Path:
        return self.nb_dir / "source.ipynb"

    @property
    def executed_notebook(self) -> Path:
        return self.nb_dir / "executed.ipynb"

    @property
    def exec_dir(self) -> Path:
        return self.dir / "executions"

    @property
    def evidence_dir(self) -> Path:
        return self.dir / "evidence"

    @property
    def doc_images_dir(self) -> Path:
        return self.dir / "document_images"

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    def path(self, name: str) -> Path:
        return self.dir / name

    @property
    def lab_dir(self) -> Path:
        return Path(self.manifest.lab_dir)

    # manifest -----------------------------------------------------------
    @property
    def manifest(self) -> RunManifest:
        if self._manifest is None:
            self._manifest = RunManifest.model_validate(read_json(self.manifest_path))
        return self._manifest

    def reload(self) -> RunManifest:
        self._manifest = None
        return self.manifest

    def save(self) -> None:
        m = self.manifest
        m.updated_at = utcnow()
        atomic_write_json(self.manifest_path, m.model_dump(mode="json"))

    def set_state(self, state: RunState, note: str = "") -> None:
        m = self.manifest
        if m.state != state:
            m.previous_state = m.state
            m.state = state
            self.save()
            self.event("state", state=str(state), note=note)

    # events -------------------------------------------------------------
    def event(self, kind: str, **fields: Any) -> None:
        from labpilot.security import redact_obj

        record = {"ts": utcnow(), "run_id": self.id, "event": kind, **redact_obj(fields)}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            fh.flush()
        if os.environ.get("LABPILOT_PROGRESS", "1") != "0" and kind in {"state", "progress", "cell"}:
            print(progress_line(record["ts"], kind, fields), file=sys.stderr, flush=True)

    # lock ---------------------------------------------------------------
    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        lock_path = self.dir / "run.lock"
        payload = json.dumps({"pid": os.getpid(), "since": utcnow()})
        for _ in range(2):
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as fh:
                    fh.write(payload)
                break
            except FileExistsError:
                holder = _read_lock(lock_path)
                if holder and not pid_alive(holder.get("pid", -1)):
                    with contextlib.suppress(OSError):
                        lock_path.unlink()
                    continue
                raise LabPilotError(
                    ErrorCode.RUN_LOCKED,
                    f"Run {self.id} is in use by another LabPilot process (pid {holder.get('pid') if holder else '?'}).",
                    exit_code=ExitCode.LOCKED,
                    next_action="Wait for it to finish, or `labpilot cancel <run-id>`.",
                ) from None
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink()


def progress_line(ts: str, kind: str, fields: dict[str, Any]) -> str:
    """One stderr progress line, e.g. `[labpilot 14:41:47] Colab: 7/19 cells done (37%) - load-data passed`."""
    head = f"[labpilot {ts[11:19]}]"
    msg = fields.get("message") or fields.get("state") or ""
    if kind == "cell" and fields.get("total"):
        done, total = fields.get("done", 0), fields["total"]
        line = (f"{head} {fields.get('where', 'Run')}: {done}/{total} cells done ({done * 100 // total}%) - "
                f"{fields.get('cell_id')} {fields.get('status', '')}")
        return f"{line} {msg}".rstrip()
    if kind == "progress":
        return f"{head} {msg}"
    parts = [head, kind, *(str(fields[k]) for k in ("cell_id", "status") if fields.get(k))]
    return " ".join([*parts, str(msg)] if msg else parts)


def _read_lock(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def runs_root(lab_dir: Path) -> Path:
    return lab_dir / WORKSPACE_DIRNAME / "runs"


def create_run(lab_dir: Path, manifest: RunManifest) -> Run:
    run_dir = runs_root(lab_dir) / manifest.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    gitignore = lab_dir / WORKSPACE_DIRNAME / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    run = Run(run_dir)
    run._manifest = manifest
    run.save()
    register_run(manifest.run_id, lab_dir)
    run.event("state", state=str(manifest.state), note="run created")
    return run


def open_run(run_id: str, lab_dir: Path | None = None) -> Run:
    if not run_id or any(c in run_id for c in "/\\") or run_id.startswith("."):
        raise LabPilotError(ErrorCode.RUN_NOT_FOUND, f"Invalid run id '{run_id}'.")
    candidates: list[Path] = []
    if lab_dir:
        candidates.append(runs_root(lab_dir) / run_id)
    candidates.append(runs_root(Path.cwd()) / run_id)
    indexed = lookup_run(run_id)
    if indexed:
        candidates.append(runs_root(indexed) / run_id)
    for c in candidates:
        if (c / "manifest.json").exists():
            return Run(c)
    raise LabPilotError(
        ErrorCode.RUN_NOT_FOUND,
        f"Run '{run_id}' not found. Pass --lab-dir or run from the lab folder.",
    )


def latest_run(lab_dir: Path) -> Run | None:
    root = runs_root(lab_dir)
    if not root.exists():
        return None
    runs = sorted((p for p in root.iterdir() if (p / "manifest.json").exists()), key=lambda p: p.name)
    return Run(runs[-1]) if runs else None
