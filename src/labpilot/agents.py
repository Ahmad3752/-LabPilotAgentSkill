"""Install/uninstall the one canonical LabPilot skill for Codex and Claude Code, and optional MCP registration.

Discovery paths (verified against official docs, 2026-11):
  Codex:       <repo>/.agents/skills/<name>/SKILL.md   and  ~/.agents/skills/<name>/SKILL.md
  Claude Code: <project>/.claude/skills/<name>/SKILL.md and  ~/.claude/skills/<name>/SKILL.md

Every installed copy is generated from the same packaged source and carries an
ownership marker with a content hash, so `doctor` can detect drift and
`uninstall` removes only directories LabPilot created.
"""

from __future__ import annotations

import contextlib
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


# ---------------------------------------------------------------- letting the agent run `labpilot`
# Codex on Windows runs commands as a sandbox user that cannot read per-user installs (Python, LabPilot's venv,
# the Colab sign-in). A Codex rule with decision "allow" runs matching commands outside the sandbox without a
# prompt, but only for escalation requests, which Codex raises only under approval_policy "on-request".
RULES_HEADER = "# Owned by LabPilot"
CODEX_APPROVAL = {"approval_policy": "on-request", "approvals_reviewer": "auto_review"}
PERMISSIONS_HINT = 'in Codex type /permissions and pick "Approve for me"'


def labpilot_cli() -> list[str]:
    return [sys.executable, "-m", "labpilot"]


def codex_rules_path() -> Path:
    return Path.home() / ".codex" / "rules" / "labpilot.rules"


def codex_rules_text(cli: list[str]) -> str:
    # json.dumps writes valid Starlark string literals (a bare Windows backslash breaks the rules parser)
    rules = "".join(f'prefix_rule(pattern = {json.dumps(p, ensure_ascii=False)}, decision = "allow")\n'
                    for p in (["labpilot"], cli))
    return (f"{RULES_HEADER}: lets the agent run the `labpilot` CLI as you (outside the Codex sandbox), because it\n"
            "# needs your own Python, Colab sign-in and screenshot browser. Remove with `labpilot uninstall`.\n" + rules)


def _owned_rules(path: Path) -> bool:
    return path.exists() and path.read_text(encoding="utf-8").startswith(RULES_HEADER)


def _claude_allow(cli: list[str]) -> list[str]:
    return [f"{tool}({cmd} *)" for cmd in ("labpilot", " ".join(cli)) for tool in ("Bash", "PowerShell")]


def _codex_config_path(project_dir: Path) -> Path:
    return project_dir.resolve() / ".codex" / "config.toml"


def _claude_settings_path(project_dir: Path) -> Path:
    return project_dir.resolve() / ".claude" / "settings.json"


def _read_toml(path: Path) -> dict[str, Any]:
    import tomllib

    try:
        return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except tomllib.TOMLDecodeError as exc:
        raise LabPilotError(ErrorCode.IO_ERROR, f"{path} is not valid TOML: {exc}", exit_code=ExitCode.CONFIG,
                            next_action="Fix or remove that file, then rerun labpilot setup.") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError as exc:
        raise LabPilotError(ErrorCode.IO_ERROR, f"{path} is not valid JSON: {exc}", exit_code=ExitCode.CONFIG,
                            next_action="Fix or remove that file, then rerun labpilot setup.") from exc
    if not isinstance(data, dict):
        raise LabPilotError(ErrorCode.IO_ERROR, f"{path} is not a JSON object.", exit_code=ExitCode.CONFIG)
    return data


def _add_toml_keys(path: Path, keys: dict[str, str]) -> list[str]:
    """Add top-level keys the file lacks (never changes a value already there); returns the exact lines added."""
    have = _read_toml(path)
    lines = [f"{k} = {json.dumps(v)}" for k, v in keys.items() if k not in have]
    if lines:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n" + text, encoding="utf-8")  # top-level keys must precede any [table]
    return lines


def _add_allow(path: Path, rules: list[str]) -> list[str]:
    data = _read_json(path)
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    added = [r for r in rules if r not in allow]
    if added:
        allow.extend(added)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return added


def _remove_added(entry: dict[str, Any]) -> None:
    """Undo exactly what setup added to one file; delete the file (and its folder) if nothing else is left."""
    path, added = Path(entry["path"]), set(entry["added"])
    if not path.exists():
        return
    if path.suffix == ".json":
        data = _read_json(path)
        perms = data.get("permissions", {})
        perms["allow"] = [r for r in perms.get("allow", []) if r not in added]
        for key, holder in (("allow", perms), ("permissions", data)):
            if not holder.get(key):
                holder.pop(key, None)
        left = json.dumps(data, indent=2) + "\n" if data else ""
    else:
        kept = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln not in added]
        left = "\n".join(kept) + "\n" if any(ln.strip() for ln in kept) else ""
    if left:
        path.write_text(left, encoding="utf-8")
        return
    path.unlink()
    with contextlib.suppress(OSError):
        path.parent.rmdir()  # only succeeds when the folder is empty


def grant_agent_access(agent: str, project_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """Let Codex / Claude Code run this installation's CLI without prompts; records what it adds in `cfg`."""
    cli, out = labpilot_cli(), {}
    entries: list[dict[str, Any]] = []
    if agent in ("codex", "both"):
        rules = codex_rules_path()
        if rules.exists() and not _owned_rules(rules):
            rules_res: dict[str, Any] = {"path": str(rules), "written": False,
                                         "reason": "exists and was not created by LabPilot; left untouched"}
        else:
            rules.parent.mkdir(parents=True, exist_ok=True)
            rules.write_text(codex_rules_text(cli), encoding="utf-8")
            rules_res = {"path": str(rules), "written": True}
        conf = _codex_config_path(project_dir)
        entries.append({"agent": "codex", "path": str(conf), "added": _add_toml_keys(conf, CODEX_APPROVAL)})
        out["codex"] = {"rules": rules_res, "config": entries[-1]}
    if agent in ("claude", "both"):
        settings = _claude_settings_path(project_dir)
        entries.append({"agent": "claude", "path": str(settings), "added": _add_allow(settings, _claude_allow(cli))})
        out["claude"] = {"settings": entries[-1]}
    owned = cfg.setdefault("agent_access", [])
    for e in entries:
        prev = next((o for o in owned if o["path"] == e["path"]), None)
        if prev is None:
            owned.append({**e, "added": list(e["added"])})
        else:  # a rerun adds nothing new; keep what earlier runs added
            prev["added"] += [a for a in e["added"] if a not in prev["added"]]
    cfg["agent_access"] = [o for o in owned if o["added"]]
    return out


def revoke_agent_access(agent: str, project_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """Remove the LabPilot-owned Codex rules file and exactly the settings setup added under `project_dir`."""
    agents = {"codex", "claude"} if agent == "both" else {agent}
    out: dict[str, Any] = {"removed": []}
    if "codex" in agents:
        rules = codex_rules_path()
        out["codex_rules"] = {"path": str(rules), "removed": _owned_rules(rules)}
        if out["codex_rules"]["removed"]:
            rules.unlink()
    root, keep = project_dir.resolve(), []
    for e in cfg.get("agent_access", []):
        if e["agent"] in agents and Path(e["path"]).is_relative_to(root):
            _remove_added(e)
            out["removed"].append(e)
        else:
            keep.append(e)
    cfg["agent_access"] = keep
    return out


def _trusted(project_dir: Path, user_cfg: dict[str, Any]) -> bool:
    import os

    trusted = {os.path.normcase(os.path.normpath(k)) for k, v in (user_cfg.get("projects") or {}).items()
               if isinstance(v, dict) and v.get("trust_level") == "trusted"}
    here = project_dir.resolve()
    return any(os.path.normcase(str(p)) in trusted for p in (here, *here.parents))


def agent_access_status(project_dir: Path) -> dict[str, Any]:
    """Read-only: can Codex / Claude Code run this installation's `labpilot` without a sandbox failure or prompt?"""
    cli = labpilot_cli()
    rules = codex_rules_path()
    rules_ok = rules.exists() and rules.read_text(encoding="utf-8") == codex_rules_text(cli)
    error = None
    try:
        user_cfg = _read_toml(Path.home() / ".codex" / "config.toml")
        proj_cfg = _read_toml(_codex_config_path(project_dir))
    except LabPilotError as exc:
        user_cfg, proj_cfg, error = {}, {}, exc.message
    trusted = _trusted(project_dir, user_cfg)
    layers = [proj_cfg, user_cfg] if trusted else [user_cfg]  # Codex reads project .codex/ only for trusted folders
    policy = next((c["approval_policy"] for c in layers if "approval_policy" in c), None)
    reviewer = next((c["approvals_reviewer"] for c in layers if "approvals_reviewer" in c), None)
    setup = f'run `labpilot setup --agent codex --project-dir "{project_dir}"`'
    fix = None
    if not rules_ok:
        fix = f"{setup} (LabPilot's Codex rules file is missing or was written for another installation)"
    elif policy != "on-request":
        fix = (f"open Codex in {project_dir} and trust the folder (Codex reads its .codex/config.toml only then)"
               if "approval_policy" in proj_cfg and not trusted else setup) + f", or {PERMISSIONS_HINT}"
    codex = {"rules_file": str(rules), "rules_ok": rules_ok, "project_trusted": trusted, "approval_policy": policy,
             "approvals_reviewer": reviewer, "ok": rules_ok and policy == "on-request", "fix": fix, "error": error,
             "note": f"A mode saved in Codex can override config: if labpilot is still refused, {PERMISSIONS_HINT}."}
    try:
        allow = _read_json(_claude_settings_path(project_dir)).get("permissions", {}).get("allow", [])
    except LabPilotError:
        allow = []
    claude_ok = all(r in allow for r in _claude_allow(cli))
    claude = {"settings": str(_claude_settings_path(project_dir)), "ok": claude_ok,
              "fix": None if claude_ok else f'run `labpilot setup --agent claude --project-dir "{project_dir}"`'}
    return {"cli": cli, "codex": codex, "claude": claude}
