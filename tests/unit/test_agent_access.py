"""LabPilot's agent permission entries: Codex rules + approval mode, Claude Code allow rules (tmp dirs only)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from labpilot import agents
from labpilot.agents import agent_access_status, codex_rules_path, grant_agent_access, revoke_agent_access

CLI = ["C:\\Users\\Zoë x\\labpilot\\venv\\Scripts\\python.exe", "-m", "labpilot"]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setattr(agents, "labpilot_cli", lambda: CLI)
    return h


def _trust(home: Path, project: Path) -> None:
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "config.toml").write_text(f'model = "x"\n\n[projects.{json.dumps(str(project))}]\n'
                                                 'trust_level = "trusted"\n', encoding="utf-8")


def test_rules_file_escapes_windows_paths(home: Path, tmp_path: Path) -> None:
    grant_agent_access("codex", tmp_path / "labs", {})
    rules = codex_rules_path()
    assert rules == home / ".codex" / "rules" / "labpilot.rules"
    text = rules.read_text(encoding="utf-8")
    assert '["labpilot"]' in text and '"C:\\\\Users\\\\Zoë x\\\\labpilot' in text
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("codex not on PATH; Starlark parse not checked")

    def decision(*argv: str) -> str | None:
        out = subprocess.run([codex, "execpolicy", "check", "--rules", str(rules), "--", *argv],
                             capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
        return json.loads(out.stdout).get("decision")

    assert decision(*CLI, "doctor", "--json") == "allow"
    assert decision("labpilot", "init", "--lab", "Lab 1.docx") == "allow"
    assert decision("python", "-m", "labpilot") is None


def test_setup_is_idempotent_and_keeps_user_settings(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "labs"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex" / "config.toml").write_text('approvals_reviewer = "user"\n\n[features]\nx = true\n',
                                                    encoding="utf-8")
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(git *)"]},
                                                                   "model": "opus"}), encoding="utf-8")
    cfg: dict = {}
    first = grant_agent_access("both", project, cfg)
    snapshot = {p: p.read_text(encoding="utf-8") for p in project.rglob("*") if p.is_file()}
    grant_agent_access("both", project, cfg)  # rerun: nothing duplicated
    assert snapshot == {p: p.read_text(encoding="utf-8") for p in project.rglob("*") if p.is_file()}
    # the user's own reviewer choice is not overwritten; only the missing key is added, before the [table]
    assert first["codex"]["config"]["added"] == ['approval_policy = "on-request"']
    toml = (project / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert toml.startswith('approval_policy = "on-request"\napprovals_reviewer = "user"\n')
    import tomllib

    assert tomllib.loads(toml)["features"] == {"x": True}
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["model"] == "opus" and settings["permissions"]["allow"][0] == "Bash(git *)"
    assert "Bash(labpilot *)" in settings["permissions"]["allow"]
    assert f"Bash({' '.join(CLI)} *)" in settings["permissions"]["allow"]
    assert {e["agent"] for e in cfg["agent_access"]} == {"codex", "claude"}


def test_uninstall_removes_only_what_labpilot_added(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "labs"
    other_rules = home / ".codex" / "rules" / "default.rules"
    other_rules.parent.mkdir(parents=True)
    other_rules.write_text('prefix_rule(pattern = ["git"], decision = "allow")\n', encoding="utf-8")
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(labpilot *)"]}}),
                                                       encoding="utf-8")
    cfg: dict = {}
    grant_agent_access("both", project, cfg)
    out = revoke_agent_access("both", project, cfg)
    assert out["codex_rules"]["removed"] and not codex_rules_path().exists()
    assert other_rules.read_text(encoding="utf-8").startswith("prefix_rule")
    assert not (project / ".codex").exists()  # created by setup, nothing else in it
    # the rule the user had before setup stays
    assert json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8")) == {
        "permissions": {"allow": ["Bash(labpilot *)"]}}
    assert cfg["agent_access"] == []


def test_foreign_rules_file_is_never_overwritten_or_removed(home: Path, tmp_path: Path) -> None:
    rules = codex_rules_path()
    rules.parent.mkdir(parents=True)
    rules.write_text("# mine\n", encoding="utf-8")
    assert grant_agent_access("codex", tmp_path, {})["codex"]["rules"]["written"] is False
    assert revoke_agent_access("codex", tmp_path, {})["codex_rules"]["removed"] is False
    assert rules.read_text(encoding="utf-8") == "# mine\n"


def test_doctor_check_states(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "labs"
    project.mkdir()
    st = agent_access_status(project)["codex"]
    assert not st["ok"] and "labpilot setup" in st["fix"]
    grant_agent_access("codex", project, {})
    st = agent_access_status(project)["codex"]  # rules ok, but Codex ignores config of an untrusted folder
    assert st["rules_ok"] and not st["project_trusted"] and not st["ok"]
    assert "trust the folder" in st["fix"] and "Approve for me" in st["fix"]
    _trust(home, project)
    st = agent_access_status(project)["codex"]
    assert st["ok"] and st["fix"] is None
    assert (st["approval_policy"], st["approvals_reviewer"]) == ("on-request", "auto_review")
    # rules written for another installation no longer match the current CLI
    codex_rules_path().write_text(agents.codex_rules_text(["C:\\old\\python.exe", "-m", "labpilot"]), encoding="utf-8")
    st = agent_access_status(project)["codex"]
    assert not st["rules_ok"] and "another installation" in st["fix"]
    assert agent_access_status(project)["claude"]["ok"] is False
    grant_agent_access("claude", project, {})
    assert agent_access_status(project)["claude"]["ok"] is True
