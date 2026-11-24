"""Subprocess helpers: argument arrays only (never shell strings), bounded output, scrubbed environments."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass

SECRET_ENV = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|ACCESS_KEY|COOKIE|SESSION_KEY|PRIVATE_KEY)", re.I
)
SECRET_PREFIXES = ("OPENAI_", "ANTHROPIC_", "CLAUDE_", "CODEX_", "GH_", "GITHUB_", "AWS_", "AZURE_", "GOOGLE_", "GCLOUD_", "CLOUDSDK_")


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Copy of os.environ without credential-looking variables (agent/cloud tokens never reach lab code)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not SECRET_ENV.search(k) and not k.upper().startswith(SECRET_PREFIXES)
    }
    env.update(extra or {})
    return env


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_cmd(
    args: list[str],
    *,
    timeout: float = 60,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    max_output: int = 2_000_000,
) -> CmdResult:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=env,
            input=input_text,
            stdin=None if input_text is not None else subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return CmdResult(127, "", str(exc))
    except OSError as exc:  # e.g. Windows Application Control blocking an executable
        return CmdResult(126, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CmdResult(124, out[-max_output:], err[-max_output:], timed_out=True)
    return CmdResult(proc.returncode, (proc.stdout or "")[-max_output:], (proc.stderr or "")[-max_output:])


def popen_group(args: list[str], **kwargs: object) -> subprocess.Popen[str]:
    """Start a process in its own group so cancel can stop it and its children (the kernel)."""
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)  # type: ignore[call-overload]


def kill_tree(pid: int) -> None:
    """Terminate a process we own and its children."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
