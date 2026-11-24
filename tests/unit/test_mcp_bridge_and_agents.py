from __future__ import annotations

import json
from pathlib import Path

import pytest

from labpilot.adapters.base import CellRequest
from labpilot.adapters.colab_mcp_bridge import ColabMcpBridge
from labpilot.agents import MARKER, install_skill, skill_status, skill_targets, uninstall_skill
from labpilot.errors import ErrorCode, LabPilotError
from labpilot.models import HostExecutionRecord, RunManifest
from labpilot.workspace import create_run, sha256_text


def _run(tmp_path: Path):  # noqa: ANN202
    return create_run(tmp_path, RunManifest(run_id="20260101-000000-dddddd", lab_dir=str(tmp_path),
                                            source_name="m.docx", source_sha256="x"))


CELLS = [CellRequest("c1", "T1", "print(1)", sha256_text("print(1)")),
         CellRequest("c2", "T1", "1/0", sha256_text("1/0"))]


def _record(**over: object) -> HostExecutionRecord:
    data = {
        "tool_names_used": ["<discovered colab tool>"],
        "runtime": {"python": "3.12"},
        "cells": [
            {"cell_id": "c1", "code_sha256": CELLS[0].code_sha256, "status": "passed", "execution_count": 1,
             "outputs": [{"output_type": "stream", "name": "stdout", "text": "1\n"}]},
            {"cell_id": "c2", "code_sha256": CELLS[1].code_sha256, "status": "failed", "execution_count": 2,
             "outputs": [{"output_type": "error", "ename": "ZeroDivisionError", "evalue": "x", "traceback": []}]},
        ],
    }
    data.update(over)
    return HostExecutionRecord.model_validate(data)


def test_host_actions_and_valid_ingest(tmp_path: Path) -> None:
    run = _run(tmp_path)
    bridge = ColabMcpBridge()
    actions = bridge.write_host_actions(run, CELLS)
    assert [c["cell_id"] for c in actions["cells"]] == ["c1", "c2"]
    assert json.loads((run.dir / "host_actions.json").read_text(encoding="utf-8"))["result_schema"]
    recs = bridge.ingest(run, _record(), CELLS, "a001")
    assert [r.status for r in recs] == ["passed", "failed"]
    assert recs[0].backend == "colab-mcp (host-reported)" and recs[0].stdout == "1\n"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(tool_names_used=[]),
        lambda d: d["cells"][0].update(code_sha256="0" * 64),  # stale code
        lambda d: d["cells"][0].update(outputs=[{"output_type": "bogus"}]),  # not nbformat
        lambda d: d["cells"][0].update(status="failed"),  # inconsistent with outputs
        lambda d: d["cells"].reverse(),  # wrong order
    ],
)
def test_ingest_rejects_unverifiable_records(tmp_path: Path, mutate) -> None:  # noqa: ANN001
    run = _run(tmp_path)
    data = _record().model_dump()
    mutate(data)
    with pytest.raises(LabPilotError) as exc:
        ColabMcpBridge().ingest(run, HostExecutionRecord.model_validate(data), CELLS, "a001")
    assert exc.value.code == ErrorCode.RECORD_INVALID


def test_skill_install_is_idempotent_owned_and_reversible(tmp_path: Path) -> None:
    targets = skill_targets("both", "project", tmp_path)
    assert [t.relative_to(tmp_path).as_posix() for t in targets] == [".agents/skills/labpilot", ".claude/skills/labpilot"]
    for t in targets:
        install_skill(t)
        install_skill(t)  # rerun: no duplicates, no error
        st = skill_status(t)
        assert st["installed"] and st["owned"] and st["in_sync"] and (t / "SKILL.md").exists()
        assert (t / "references" / "workflow.md").exists()
    (targets[0] / "SKILL.md").write_text("edited", encoding="utf-8")
    assert skill_status(targets[0])["modified_locally"] is True
    # a skill the user created themselves is never overwritten or removed
    foreign = tmp_path / ".claude" / "skills" / "labpilot"
    (foreign / MARKER).unlink()
    with pytest.raises(LabPilotError):
        install_skill(foreign)
    assert uninstall_skill(foreign)["removed"] is False and (foreign / "SKILL.md").exists()
    assert uninstall_skill(targets[0])["removed"] is True and not targets[0].exists()


def test_skill_frontmatter_follows_agent_skills_spec() -> None:
    import re

    from labpilot.agents import skill_source

    text = (skill_source() / "SKILL.md").read_text(encoding="utf-8")
    fm = text.split("---")[1]
    name = re.search(r"^name:\s*(\S+)", fm, re.M).group(1)
    desc = re.search(r"^description:\s*(.+)$", fm, re.M).group(1)
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64 and name == skill_source().name
    assert 0 < len(desc) <= 1024
    assert len(text.splitlines()) < 500
