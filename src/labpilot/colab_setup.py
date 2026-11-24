"""Opt-in installation and sign-in helpers for the official Google Colab CLI (google-colab-cli).

* Installs the CLI into a LabPilot-owned virtual environment (never globally), pinned to the
  version this adapter was verified against.
* Upstream lists Linux and macOS only. On Windows LabPilot calls the CLI's Python entry point
  directly (no .exe launcher). Verified 2026-11-25 on Windows 11 with google-colab-cli 0.8.0 and a
  signed Python 3.12: sign-in, CPU runtime allocation, upload + hash check, per-cell execution with
  rich outputs, and session release. Other Windows setups are untested.
* Sign-in is the CLI's own Google OAuth flow, run interactively in the user's terminal.
  LabPilot never reads, stores or forwards credentials.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from labpilot.config import load_config, save_config
from labpilot.errors import ErrorCode, ExitCode, LabPilotError
from labpilot.procutil import run_cmd

COLAB_CLI_PACKAGE = "google-colab-cli==0.8.0"  # version whose --help and source the adapter was verified against
LAUNCHER = "import sys; from colab_cli.cli import main; sys.argv[0] = 'colab'; sys.exit(main())"


def install_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else os.environ.get(
        "XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(base or Path.home()) / "labpilot" / "colab-cli"


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def install_colab_cli() -> dict[str, Any]:
    """Create the owned venv (Python >= 3.12, required upstream) and install the pinned CLI."""
    root = install_root()
    py = _venv_python(root)
    uv = shutil.which("uv")
    steps: list[str] = []
    if py.exists() and not run_cmd([str(py), "-c", "print(1)"], timeout=60).ok:
        shutil.rmtree(root, ignore_errors=True)  # our own broken/blocked environment from an earlier attempt
    if not py.exists():
        base = _find_python312()
        if not base:
            raise LabPilotError(
                ErrorCode.PROFILE_PREREQ_MISSING,
                "google-colab-cli needs Python 3.12 or newer, and no trusted Python 3.12+ was found. On Windows, "
                "Smart App Control blocks unsigned Python builds (such as the ones uv downloads).",
                exit_code=ExitCode.CONFIG,
                next_action="Install Python 3.12+ from python.org (signed installer; per-user, no admin) or "
                            "`winget install Python.Python.3.12 --scope user`, then rerun "
                            "`labpilot setup --backend colab-cli --install-colab-cli`.",
            )
        res = run_cmd([uv, "venv", str(root), "--python", base], timeout=900) if uv else run_cmd(
            [base, "-m", "venv", str(root)], timeout=600)
        if not res.ok:
            raise LabPilotError(ErrorCode.PROFILE_PREREQ_MISSING, f"could not create the Colab CLI environment: "
                                f"{(res.stderr or res.stdout)[-400:]}", exit_code=ExitCode.CONFIG)
        steps.append(f"created {root}")
    if uv:
        res = run_cmd([uv, "pip", "install", "--python", str(py), COLAB_CLI_PACKAGE], timeout=900)
    else:
        res = run_cmd([str(py), "-m", "pip", "install", COLAB_CLI_PACKAGE], timeout=900)
    if not res.ok:
        raise LabPilotError(ErrorCode.PROFILE_PREREQ_MISSING, f"pip install {COLAB_CLI_PACKAGE} failed: "
                            f"{(res.stderr or res.stdout)[-400:]}", exit_code=ExitCode.CONFIG)
    steps.append(f"installed {COLAB_CLI_PACKAGE}")
    command = [str(py), "-c", LAUNCHER]
    ver = run_cmd([*command, "version"], timeout=120)
    if not ver.ok:
        raise LabPilotError(ErrorCode.BACKEND_UNAVAILABLE, f"the Colab CLI does not start: {(ver.stderr or ver.stdout)[-400:]}",
                            exit_code=ExitCode.CONFIG)
    cfg = load_config()
    cfg["colab_cli"]["command"] = command
    cfg["colab_cli"]["owned_install"] = str(root)
    cfg["colab_cli"]["native_windows"] = sys.platform == "win32"
    save_config(cfg)
    return {"installed": True, "version": ver.stdout.strip()[-120:], "command": command, "steps": steps,
            "native_windows": sys.platform == "win32",
            "next": "Run `labpilot colab-login` once in a terminal to sign in with Google."}


def _find_python312() -> str | None:
    """A Python >= 3.12 that can actually run here (on Windows: Authenticode-signed, so Smart App Control allows it)."""
    for cand in (["py", "-3.14"], ["py", "-3.13"], ["py", "-3.12"], ["python3.13"], ["python3.12"], ["python3"],
                 ["python"]):
        if not shutil.which(cand[0]):
            continue
        res = run_cmd([*cand, "-c", "import sys; print(sys.executable if sys.version_info >= (3, 12) else '')"], timeout=30)
        exe = res.stdout.strip() if res.ok else ""
        if exe and (sys.platform != "win32" or _signed(exe)):
            return exe
    return None


def _signed(exe: str) -> bool:
    res = run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   "(Get-AuthenticodeSignature -LiteralPath $env:LP_EXE).Status"], timeout=60,
                  env={**os.environ, "LP_EXE": exe})
    return res.ok and res.stdout.strip() == "Valid"


def colab_command() -> list[str]:
    from labpilot.adapters.colab_cli import ColabCliBackend

    base = ColabCliBackend().base
    if not base:
        raise LabPilotError(ErrorCode.BACKEND_UNAVAILABLE, "The Colab CLI is not configured.",
                            exit_code=ExitCode.CONFIG,
                            next_action="labpilot setup --backend colab-cli --install-colab-cli")
    return base


def interactive_login() -> int:
    """Run the official sign-in interactively (inherits the terminal; LabPilot sees nothing)."""
    cmd = colab_command()
    print("Opening Google's sign-in for the Colab CLI. Follow the link, approve access, and paste the code "
          "back into THIS terminal when asked. LabPilot does not see or store it.", file=sys.stderr, flush=True)
    return subprocess.call([*cmd, "sessions"])  # noqa: S603 - official CLI, argument list, user's own terminal
