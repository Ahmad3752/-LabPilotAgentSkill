"""`labpilot doctor`: read-only environment diagnostics. Never allocates cloud compute by default."""

from __future__ import annotations

import json
import platform
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from labpilot import __version__
from labpilot.adapters.colab_cli import AUTH_HINTS, ColabCliBackend
from labpilot.agents import skill_status, skill_targets
from labpilot.config import load_config
from labpilot.documents.validate import find_renderer, find_word
from labpilot.errors import ErrorCode, ExitCode, LabPilotError, strip_ansi
from labpilot.procutil import run_cmd
from labpilot.security import redact_obj


def _agent(name: str) -> dict[str, Any]:
    exe = shutil.which(name)
    if not exe:
        return {"available": False, "fix": f"Install and sign in to {name} (the host agent provides all AI reasoning)."}
    res = run_cmd([exe, "--version"], timeout=60)
    return {"available": res.ok, "version": (res.stdout or res.stderr).strip().splitlines()[0] if (res.stdout or res.stderr).strip() else None}


def mcp_registration() -> dict[str, Any]:
    found = []
    for path in (Path.home() / ".claude.json", Path.cwd() / ".mcp.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        servers = dict(data.get("mcpServers", {}))
        for proj in (data.get("projects") or {}).values():
            servers.update(proj.get("mcpServers", {}) or {})
        if "colab-mcp" in servers:
            found.append(f"claude:{path.name}")
    codex_cfg = Path.home() / ".codex" / "config.toml"
    if codex_cfg.exists() and "colab-mcp" in codex_cfg.read_text(encoding="utf-8", errors="ignore"):
        found.append("codex:config.toml (Codex is not a documented colab-mcp client)")
    return {"registered": bool(found), "where": found,
            "detail": ("colab-mcp entry found in " + ", ".join(found)) if found else "no colab-mcp registration found",
            "note": "Tool availability can only be confirmed by the host agent (dynamic tools)."}


NUTS_LMS_SOURCE = "git+https://github.com/deeznust42/nutslms-mcp"


def _show(argv: list[str]) -> str:
    """An argv list as a command to paste (PowerShell or POSIX shell); a quoted first word needs `&` in PowerShell."""
    words = [f'"{a}"' if any(c in a for c in " /\\") else a for a in argv]
    return ("& " if words[0].startswith('"') and sys.platform == "win32" else "") + " ".join(words)


def _lms_state() -> dict[str, Any]:
    """What is missing for the Nuts LMS MCP (moodle-mcp), with each missing step as an argument list."""
    import os

    # On Windows, Smart App Control can block uv's unsigned moodle-mcp.exe launcher; the tool's own
    # (signed python.org) interpreter running `-m moodle_mcp` works, and it exists even when uv's bin is not on PATH.
    tool_py = Path(os.environ.get("APPDATA", "")) / "uv" / "tools" / "nutslms-mcp" / "Scripts" / "python.exe"
    use_py = sys.platform == "win32" and tool_py.exists()
    exe = shutil.which("moodle-mcp") or (str(tool_py) if use_py else None)
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "moodle-mcp"
    signed_in = any(cache.glob("*.token.json")) if cache.is_dir() else False
    where = []
    tls_workaround = False
    for path in (Path.home() / ".claude.json", Path.cwd() / ".mcp.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        servers = dict(data.get("mcpServers", {}))
        for proj in (data.get("projects") or {}).values():
            servers.update(proj.get("mcpServers", {}) or {})
        # `moodle-mcp serve`, or `<python> -m moodle_mcp serve` where Smart App Control blocks the uv .exe launcher
        entries = [s for s in servers.values() if isinstance(s, dict) and re.search(
            r"moodle[-_]mcp", f"{s.get('command', '')} {' '.join(map(str, s.get('args') or []))}")]
        if entries:
            where.append(f"claude:{path.name}")
            tls_workaround = tls_workaround or any("OPENSSL_CONF" in (s.get("env") or {}) for s in entries)
    claude_reg, claude_tls = bool(where), tls_workaround
    codex_cfg = Path.home() / ".codex" / "config.toml"
    codex_text = codex_cfg.read_text(encoding="utf-8", errors="ignore") if codex_cfg.exists() else ""
    codex_reg = bool(re.search(r"moodle[-_]mcp", codex_text))
    codex_tls = "OPENSSL_CONF" in codex_text  # shortcut: any OPENSSL_CONF in the file counts; parse TOML if it misleads
    if codex_reg:
        where.append("codex:config.toml")
    run = [str(tool_py), "-m", "moodle_mcp"] if use_py else ["moodle-mcp"]
    # lms.nust.edu.pk needs legacy TLS renegotiation, which moodle-mcp does not enable yet (see lms_openssl.cnf)
    cnf = Path(__file__).with_name("lms_openssl.cnf")
    register: list[list[str]] = []
    for agent, reg, tls, flag, scope in (("claude", claude_reg, claude_tls, "-e", ["-s", "user"]),
                                         ("codex", codex_reg, codex_tls, "--env", [])):
        if shutil.which(agent) and not (reg and tls):
            if reg:
                register.append([agent, "mcp", "remove", "moodle", *scope])
            # user scope: the agent sees the LMS from any folder (C:\Labs), not only the one it was registered in
            register.append([agent, "mcp", "add", *scope, "moodle", flag, f"OPENSSL_CONF={cnf}", "--", *run, "serve"])
    return {"exe": exe, "signed_in": signed_in, "where": where, "tls_workaround": claude_tls or codex_tls,
            "run": run, "cnf": cnf, "register": register}


def nuts_lms() -> dict[str, Any]:
    """Nuts LMS MCP (moodle-mcp): installed, signed in (token file exists; never read), registered with an agent."""
    s = _lms_state()
    env = f'$env:OPENSSL_CONF="{s["cnf"]}";' if sys.platform == "win32" else f'OPENSSL_CONF="{s["cnf"]}"'
    login = f"{env} {_show([*s['run'], 'login'])}"
    fix = None
    if not s["exe"]:
        fix = f"labpilot lms connect   (or: uv tool install {NUTS_LMS_SOURCE})"
    elif not s["signed_in"]:
        fix = f"labpilot lms connect   (or run `{login}` in your own terminal; LabPilot never sees your LMS password)"
    elif s["register"]:
        fix = "labpilot lms connect   (or: " + "; ".join(map(_show, s["register"])) + ")   then restart the agent"
    return {"installed": bool(s["exe"]), "signed_in": s["signed_in"], "registered": bool(s["where"]),
            "where": s["where"], "tls_workaround": s["tls_workaround"],
            "ready": bool(s["exe"] and s["signed_in"] and s["where"]), "fix": fix}


def connect_lms() -> dict[str, Any]:
    """Install, sign in to and register the Nuts LMS MCP, doing only the steps still missing (rerunnable).

    The sign-in is moodle-mcp's own prompt in this terminal: LabPilot neither reads nor stores the password."""
    import os
    import subprocess

    s = _lms_state()
    if not s["exe"]:
        uv = shutil.which("uv")
        if uv is None:  # uv's PyPI wheel brings the binary; `python -m uv` runs it without touching PATH
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "uv"], check=True)
        # --python: reuse this (signed python.org) interpreter instead of letting uv download an unsigned one
        subprocess.run([*([uv] if uv else [sys.executable, "-m", "uv"]), "tool", "install", "--python", sys.executable,
                        NUTS_LMS_SOURCE], check=True)
        s = _lms_state()
        if not s["exe"]:
            raise LabPilotError(ErrorCode.MCP_UNAVAILABLE, "moodle-mcp was installed but is not on PATH yet.",
                                exit_code=ExitCode.CONFIG, next_action="Open a new terminal and run: labpilot lms connect")
    if not s["signed_in"]:
        if not sys.stdin.isatty():
            raise LabPilotError(ErrorCode.AUTH_REQUIRED, "Signing in to the LMS needs your own terminal.",
                                exit_code=ExitCode.CONFIG, next_action="Run in your own terminal: labpilot lms connect")
        print("Sign in to your LMS. You type your password into moodle-mcp's own prompt; LabPilot does not see it.",
              file=sys.stderr, flush=True)
        subprocess.call([*s["run"], "login"], env={**os.environ, "OPENSSL_CONF": str(s["cnf"])})
        s = _lms_state()
        if not s["signed_in"]:
            raise LabPilotError(ErrorCode.AUTH_REQUIRED, "LMS sign-in did not finish.", exit_code=ExitCode.CONFIG,
                                next_action="Run again: labpilot lms connect")
    for argv in s["register"]:
        exe = shutil.which(argv[0]) or argv[0]  # codex/claude may be .cmd shims on Windows
        # check=False: `mcp remove` fails harmlessly when the entry lives in another scope
        subprocess.run([exe, *argv[1:]], check=False)
    return nuts_lms()


def _browser() -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            v = b.version
            b.close()
        return {"available": True, "chromium": v}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc).splitlines()[0][:200],
                "fix": f"{sys.executable} -m playwright install chromium"}


def _kernel() -> dict[str, Any]:
    try:
        from jupyter_client.manager import start_new_kernel

        km, kc = start_new_kernel(kernel_name="python3")
        try:
            ok = km.is_alive()
        finally:
            kc.stop_channels()
            km.shutdown_kernel(now=True)
        return {"available": ok, "python": platform.python_version()}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)[:200], "fix": f"{sys.executable} -m pip install ipykernel"}


def _colab_cli() -> dict[str, Any]:
    be = ColabCliBackend()
    caps = be.capabilities()
    info: dict[str, Any] = {"platform_supported": caps.platform_supported, "configured": bool(be.base)}
    if not be.base:
        info["detail"] = "colab CLI not found (labpilot setup --backend colab-cli --install-colab-cli)"
        return info
    ver = be._colab("version", timeout=120)
    info["version"] = strip_ansi(ver.stdout or ver.stderr).strip()[-120:] if ver.returncode != 127 else None
    if ver.ok:
        s = be._colab("sessions", timeout=120)
        info["authenticated"] = s.ok and not AUTH_HINTS.search(s.stdout + s.stderr)
    return info


def _writable(path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, delete=True):
            pass
        return {"writable": True}
    except OSError as exc:
        return {"writable": False, "error": str(exc)}


def doctor(project_dir: Path | None = None) -> dict[str, Any]:
    project_dir = project_dir or Path.cwd()
    cfg = load_config()
    skills = {}
    for agent in ("codex", "claude"):
        for scope in ("project", "user"):
            skills[f"{agent}:{scope}"] = skill_status(skill_targets(agent, scope, project_dir)[0])
    report: dict[str, Any] = {
        "labpilot": __version__,
        "os": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "python": {"version": platform.python_version(), "ok": sys.version_info >= (3, 11)},
        "agents": {"codex": _agent("codex"), "claude": _agent("claude")},
        "skills": skills,
        "browser": _browser(),
        "local_kernel": _kernel(),
        "colab_cli": _colab_cli(),
        "colab_mcp": mcp_registration(),
        "nuts_lms": nuts_lms(),
        "page_renderer": {"available": bool(find_renderer() or find_word()),
                          "kind": "libreoffice" if find_renderer() else ("microsoft-word" if find_word() else None)},
        "config": {"backend": cfg.get("backend"), "telemetry": cfg.get("telemetry")},
        "output_dir": _writable(project_dir / ".labpilot"),
    }
    problems = []
    if not report["python"]["ok"]:
        problems.append("Python 3.11+ required")
    if not report["browser"]["available"]:
        problems.append("Chromium for Playwright missing: " + report["browser"].get("fix", ""))
    if not report["local_kernel"]["available"]:
        problems.append("Local Jupyter kernel unavailable")
    if not (report["agents"]["codex"]["available"] or report["agents"]["claude"]["available"]):
        problems.append("No supported coding agent found (Codex or Claude Code)")
    if not any(s.get("installed") for s in skills.values()):
        problems.append("LabPilot skill not installed: labpilot setup --agent codex (or claude)")
    report["ready_for_local_fallback"] = report["browser"]["available"] and report["local_kernel"]["available"]
    report["ready_for_colab"] = bool(report["colab_cli"].get("authenticated")) or report["colab_mcp"]["registered"]
    if not report["ready_for_colab"]:
        problems.append("Google Colab not ready: configure the Colab CLI (labpilot setup --backend colab-cli) "
                        "or register colab-mcp in Claude Code; until then labs run on the local fallback")
    report["problems"] = problems
    lms = report.pop("nuts_lms")
    # the LMS fix holds commands the user pastes, so its paths must stay real (redaction would print "~\...")
    return {**redact_obj(report), "nuts_lms": lms}
