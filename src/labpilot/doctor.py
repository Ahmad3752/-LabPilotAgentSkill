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
from labpilot.errors import strip_ansi
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


def nuts_lms() -> dict[str, Any]:
    """Nuts LMS MCP (moodle-mcp): installed, signed in (token file exists; never read), registered with an agent."""
    import os

    exe = shutil.which("moodle-mcp")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "moodle-mcp"
    signed_in = any(cache.glob("*.token.json")) if cache.is_dir() else False
    where = []
    for path in (Path.home() / ".claude.json", Path.cwd() / ".mcp.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        servers = dict(data.get("mcpServers", {}))
        for proj in (data.get("projects") or {}).values():
            servers.update(proj.get("mcpServers", {}) or {})
        # `moodle-mcp serve`, or `<python> -m moodle_mcp serve` where Smart App Control blocks the uv .exe launcher
        if any(re.search(r"moodle[-_]mcp", f"{s.get('command', '')} {' '.join(map(str, s.get('args') or []))}")
               for s in servers.values() if isinstance(s, dict)):
            where.append(f"claude:{path.name}")
    codex_cfg = Path.home() / ".codex" / "config.toml"
    if codex_cfg.exists() and re.search(r"moodle[-_]mcp", codex_cfg.read_text(encoding="utf-8", errors="ignore")):
        where.append("codex:config.toml")
    # On Windows, Smart App Control can block uv's unsigned moodle-mcp.exe launcher; the tool's own
    # (signed python.org) interpreter running `-m moodle_mcp` works.
    run = "moodle-mcp"
    tool_py = Path(os.environ.get("APPDATA", "")) / "uv" / "tools" / "nutslms-mcp" / "Scripts" / "python.exe"
    if exe and sys.platform == "win32" and tool_py.exists():
        run = f'"{tool_py}" -m moodle_mcp'
    fix = None
    if not exe:
        fix = "uv tool install git+https://github.com/deeznust42/nutslms-mcp"
    elif not signed_in:
        fix = f"Run `{run} login` in your own terminal (LabPilot never asks for your LMS password)."
    elif not where:
        fix = f"claude mcp add moodle -- {run} serve   (then restart the agent)"
    return {"installed": bool(exe), "signed_in": signed_in, "registered": bool(where), "where": where,
            "ready": bool(exe and signed_in and where), "fix": fix}


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


def _wsl() -> dict[str, Any]:
    if sys.platform != "win32":
        return {"applicable": False}
    res = run_cmd(["wsl", "-l", "-q"], timeout=30)
    distros = [d.replace("\x00", "").strip() for d in res.stdout.splitlines()]
    return {"available": res.ok, "distros": [d for d in distros if d]}


def _colab_cli() -> dict[str, Any]:
    be = ColabCliBackend()
    caps = be.capabilities()
    info: dict[str, Any] = {"platform_supported": caps.platform_supported, "configured": bool(be.base)}
    if sys.platform == "win32" and not be.base:
        info["detail"] = "google-colab-cli supports Linux/macOS only; configure a WSL distro to use it on Windows"
        return info
    if not be.base:
        info["detail"] = "colab CLI not found (uv tool install google-colab-cli)"
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
        "wsl": _wsl(),
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
    return redact_obj(report)
