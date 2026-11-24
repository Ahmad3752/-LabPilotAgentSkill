"""Install/uninstall the one canonical LabPilot skill for Codex and Claude Code, and optional MCP registration.

Discovery paths (verified against official docs, 2026-11):
  Codex:       <repo>/.agents/skills/<name>/SKILL.md   and  ~/.agents/skills/<name>/SKILL.md
  Claude Code: <project>/.claude/skills/<name>/SKILL.md and  ~/.claude/skills/<name>/SKILL.md

Every installed copy is generated from the same packaged source and carries an
ownership marker with a content hash, so `doctor` can detect drift and
`uninstall` removes only directories LabPilot created.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from labpilot import __version__
from labpilot.errors import ErrorCode, ExitCode, LabPilotError
from labpilot.procutil import run_cmd

SKILL_NAME = "labpilot"
MARKER = ".labpilot-owned.json"
COLAB_MCP_REPO = "https://github.com/googlecolab/colab-mcp"


def skill_source() -> Path:
    packaged = Path(__file__).resolve().parent / "_skill" / SKILL_NAME
    if (packaged / "SKILL.md").exists():
        return packaged
    repo = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME  # editable / source checkout
    if (repo / "SKILL.md").exists():
        return repo
    raise LabPilotError(ErrorCode.BACKEND_UNAVAILABLE, "Bundled LabPilot skill not found; reinstall LabPilot.",
                        exit_code=ExitCode.CONFIG)


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != MARKER:
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def skill_targets(agent: str, scope: str, project_dir: Path) -> list[Path]:
    agents = ["codex", "claude"] if agent == "both" else [agent]
    out = []
    for a in agents:
        base = {"codex": ".agents", "claude": ".claude"}.get(a)
        if base is None:
            raise LabPilotError(ErrorCode.BACKEND_UNAVAILABLE, f"Unknown agent '{a}' (use codex, claude or both).")
        root = Path.home() if scope == "user" else project_dir.resolve()
        out.append(root / base / "skills" / SKILL_NAME)
    return out


def skill_status(target: Path) -> dict[str, Any]:
    marker = target / MARKER
    if not (target / "SKILL.md").exists():
        return {"path": str(target), "installed": False}
    if not marker.exists():
        return {"path": str(target), "installed": True, "owned": False, "in_sync": None}
    data = json.loads(marker.read_text(encoding="utf-8"))
    current = tree_hash(target)
    try:
        expected = tree_hash(skill_source())
    except LabPilotError:
        expected = data.get("sha256")
    return {"path": str(target), "installed": True, "owned": True, "version": data.get("version"),
            "modified_locally": current != data.get("sha256"), "in_sync": current == expected}


def install_skill(target: Path) -> dict[str, Any]:
    src = skill_source()
    if target.exists():
        if not (target / MARKER).exists():
            raise LabPilotError(
                ErrorCode.IO_ERROR,
                f"{target} exists and was not created by LabPilot; refusing to overwrite it.",
                exit_code=ExitCode.CONFIG, next_action="Rename/remove that skill yourself, then rerun setup.")
        shutil.rmtree(target)  # our own previous copy: replace (idempotent rerun)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (target / MARKER).write_text(json.dumps({"version": __version__, "sha256": tree_hash(target),
                                             "installed_at": datetime.now(UTC).isoformat(),
                                             # exact command for agents whose PATH lacks `labpilot`
                                             "cli": [sys.executable, "-m", "labpilot"]}, indent=2), encoding="utf-8")
    return {"path": str(target), "installed": True}


def uninstall_skill(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"path": str(target), "removed": False, "reason": "not installed"}
    if not (target / MARKER).exists():
        return {"path": str(target), "removed": False, "reason": "not owned by LabPilot; left untouched"}
    shutil.rmtree(target)
    return {"path": str(target), "removed": True}


def colab_mcp_revision() -> str | None:
    res = run_cmd(["git", "ls-remote", COLAB_MCP_REPO, "HEAD"], timeout=60)
    if res.ok and res.stdout.strip():
        return res.stdout.split()[0]
    return None


def register_colab_mcp(agent: str, scope: str = "user") -> dict[str, Any]:
    """Register the official colab-mcp server via the agent's own CLI (never by editing its config files)."""
    if agent != "claude":
        return {"agent": agent, "registered": False,
                "reason": "colab-mcp lists Claude Code (not Codex) among clients with the required "
                          "notifications/tools/list_changed support; not registering it for Codex. "
                          "Use the local backend or Colab CLI with Codex."}
    if not shutil.which("claude"):
        return {"agent": agent, "registered": False, "reason": "claude CLI not found"}
    existing = run_cmd(["claude", "mcp", "get", "colab-mcp"], timeout=60)
    if existing.ok:
        return {"agent": agent, "registered": True, "already_present": True, "owned": False,
                "reason": "an entry named colab-mcp already exists; left unchanged"}
    rev = colab_mcp_revision()
    spec = f"git+{COLAB_MCP_REPO}" + (f"@{rev}" if rev else "")
    res = run_cmd(["claude", "mcp", "add", "-s", scope, "colab-mcp", "--", "uvx", spec], timeout=120)
    return {"agent": agent, "registered": res.ok, "owned": res.ok, "pinned_revision": rev, "scope": scope,
            "detail": (res.stdout + res.stderr).strip()[-400:]}


def unregister_colab_mcp(entry: dict[str, Any]) -> dict[str, Any]:
    if not entry.get("owned"):
        return {"removed": False, "reason": "not registered by LabPilot"}
    res = run_cmd(["claude", "mcp", "remove", "-s", entry.get("scope", "user"), "colab-mcp"], timeout=60)
    return {"removed": res.ok, "detail": (res.stdout + res.stderr).strip()[-300:]}
