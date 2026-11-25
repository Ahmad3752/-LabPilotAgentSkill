"""Nuts LMS MCP integration (host-mediated new-lab discovery).

The assignment / status payloads below are SYNTHETIC fixtures written in the exact shape that
moodle-mcp's `list_assignments` and `assignment_status` tools return (lms.assignments /
lms.submission_status at commit b4aa67a). No LMS is contacted in these tests.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from labpilot import lms
from labpilot.cli import app
from labpilot.datasets.discover import list_candidate_files
from labpilot.errors import ErrorCode, LabPilotError
from labpilot.inputs import LmsStagedFolderInputProvider

NOW = dt.datetime(2026, 11, 26, 12, 0)


def _a(assignid: int, name: str, due: str | None, cutoff: str | None = None, course: str = "CS-401 Big Data",
       opens: str | None = None) -> dict:
    return {"course": course, "courseid": 77, "assignid": assignid, "cmid": 9000 + assignid, "name": name,
            "duedate": due, "cutoffdate": cutoff, "allowsubmissionsfrom": opens, "maxattempts": -1, "intro": None}


ASSIGNMENTS = [
    _a(1, "Lab 9: Spark DataFrames", "2026-12-03 23:59"),
    _a(2, "Lab 8 - Hadoop MapReduce", "2026-11-28 23:59"),
    _a(3, "Assignment 2", "2026-12-10 23:59"),
    _a(4, "Lab 7", "2026-11-10 23:59"),                                   # closed
    _a(5, "Lab10 Kafka Streams", "2026-11-20 23:59", "2026-12-01 23:59"),  # overdue, cutoff still open
    _a(6, "Open-ended Lab", None, opens="2026-12-01 00:00"),               # no deadline, opens later
    _a(7, "Quiz on labour economics", "2026-12-20 23:59"),                # 'labour' is not a lab
]


def status(assignid: int, state: str) -> dict:
    return {"assignid": assignid, "status": state, "gradingstatus": "notgraded", "submitted_at": None, "files": [],
            "grade": None, "can_edit": True, "can_submit": True}


def test_check_classifies_labs() -> None:
    r = lms.check_labs(ASSIGNMENTS, now=NOW)
    ids = [x["assignid"] for x in r["new_labs"]]
    assert ids == [5, 2, 1, 6]  # by due date (overdue-but-open first), undated last
    assert [x["assignid"] for x in r["closed"]] == [4]
    assert r["not_labs"] == 2
    assert r["needs_status"] == [5, 2, 1, 6]
    by_id = {x["assignid"]: x for x in r["new_labs"]}
    assert by_id[5]["overdue"] is True and by_id[6]["not_open_yet"] is True
    assert by_id[1]["suggested_folder"] == str(Path("lms-labs") / "CS-401 Big Data" / "Lab 9 Spark DataFrames")


def test_check_uses_statuses_and_ledger() -> None:
    lms.mark_lab(2, "processed", run_id="r-1")
    statuses = [[status(1, "submitted"), status(5, "new")], status(6, "draft")]
    r = lms.check_labs(ASSIGNMENTS, statuses, now=NOW)
    assert [x["assignid"] for x in r["new_labs"]] == [5, 6]
    assert r["needs_status"] == []
    assert [x["assignid"] for x in r["submitted"]] == [1]
    assert r["handled"][0]["assignid"] == 2 and r["handled"][0]["run_id"] == "r-1"
    assert r["new_labs"][1]["submission_status"] == "draft"


def test_check_include_past_and_custom_pattern() -> None:
    assert 4 in [x["assignid"] for x in lms.check_labs(ASSIGNMENTS, include_past=True, now=NOW)["new_labs"]]
    r = lms.check_labs(ASSIGNMENTS, pattern=r"assignment", now=NOW)
    assert [x["assignid"] for x in r["new_labs"]] == [3]


def test_sites_are_kept_apart() -> None:
    lms.mark_lab(1, "ignored", site="https://lms.a.edu")
    assert 1 in [x["assignid"] for x in lms.check_labs(ASSIGNMENTS, site="https://lms.b.edu", now=NOW)["new_labs"]]
    assert 1 not in [x["assignid"] for x in lms.check_labs(ASSIGNMENTS, site="https://lms.a.edu", now=NOW)["new_labs"]]


def test_tool_errors_and_bad_shapes(tmp_path: Path) -> None:
    err = tmp_path / "a.json"
    err.write_text("Moodle error: invalidtoken: Invalid token (token may have been revoked; re-run login)")
    with pytest.raises(LabPilotError) as e:
        lms.load_tool_json(err, "list_assignments")
    assert e.value.code == ErrorCode.MCP_UNAVAILABLE
    with pytest.raises(LabPilotError):
        lms.check_labs({"courses": []})
    with pytest.raises(LabPilotError):
        lms.check_labs([{"name": "Lab 1"}])
    with pytest.raises(LabPilotError):
        lms.check_labs(ASSIGNMENTS, [{"assignid": 1}])
    with pytest.raises(LabPilotError):
        lms.mark_lab(1, "done")


def test_stage_records_provenance_and_progress(python_lab: Path, tmp_path: Path) -> None:
    folder = tmp_path / "lms-labs" / "CS-401" / "Lab 9"
    shutil.copytree(python_lab, folder)
    out = lms.stage_lab(folder, ASSIGNMENTS, 1, source_urls=["https://lms.x.edu/webservice/pluginfile.php/1/a.docx?forcedownload=1"])
    assert "problem" not in out and out["next"].startswith("labpilot init --lab")
    source = json.loads((folder / lms.SOURCE_FILE).read_text(encoding="utf-8"))
    assert source["assignid"] == 1 and source["provider"] == "nuts-lms-mcp"
    assert source["source_urls"] == ["https://lms.x.edu/webservice/pluginfile.php/1/a.docx"]
    assert all(len(f["sha256"]) == 64 for f in source["files"])
    prov = out["run_request"]["inputs"]["provenance"]
    assert prov["provider"] == "nuts-lms-mcp" and prov["assignid"] == "1"
    # the provenance file is never mistaken for a dataset
    assert lms.SOURCE_FILE not in [p.name for p in list_candidate_files(folder)]
    r = lms.check_labs(ASSIGNMENTS, now=NOW)
    assert [x["assignid"] for x in r["in_progress"]] == [1]
    assert 1 not in [x["assignid"] for x in r["new_labs"]]


def test_stage_without_docx_manual_reports_problem(tmp_path: Path) -> None:
    folder = tmp_path / "lab"
    folder.mkdir()
    (folder / "Lab 9 manual.pdf").write_bytes(b"%PDF-1.4 synthetic")
    out = lms.stage_lab(folder, ASSIGNMENTS, 1)
    assert "needs the .docx manual" in out["problem"] and out["run_request"] is None
    with pytest.raises(LabPilotError):
        lms.stage_lab(tmp_path / "missing", ASSIGNMENTS, 1)
    with pytest.raises(LabPilotError):
        lms.stage_lab(folder, ASSIGNMENTS, 999)


def test_provider_requires_staging(python_lab: Path) -> None:
    with pytest.raises(LabPilotError):
        LmsStagedFolderInputProvider().fetch(str(python_lab))


def test_init_run_records_lms_provenance(python_lab: Path) -> None:
    from labpilot.orchestration import init_run

    lms.stage_lab(python_lab, ASSIGNMENTS, 2, site="https://lms.x.edu")
    manual = next(python_lab.glob("*.docx"))
    run = init_run(manual)
    prov = run.manifest.input_provenance
    assert prov["provider"] == "nuts-lms-mcp" and prov["assignid"] == "2"
    assert prov["manual_matches_download"] == "true" and prov["site"] == "https://lms.x.edu"


def test_init_run_without_lms_is_local(python_lab: Path) -> None:
    from labpilot.orchestration import init_run

    run = init_run(next(python_lab.glob("*.docx")))
    assert run.manifest.input_provenance == {"provider": "local-folder"}


def test_cli_check_stage_mark_list(tmp_path: Path) -> None:
    runner = CliRunner()
    f = tmp_path / "assignments.json"
    f.write_text(json.dumps(ASSIGNMENTS), encoding="utf-8")
    res = runner.invoke(app, ["lms", "check", "--assignments", str(f), "--include-past", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["ok"] and data["counts"]["new"] == 5
    res = runner.invoke(app, ["lms", "mark", "4", "--state", "ignored", "--name", "Lab 7", "--json"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["lms", "list", "--json"])
    assert json.loads(res.stdout)["labs"][0]["name"] == "Lab 7"
    res = runner.invoke(app, ["lms", "check", "--assignments", str(f), "--include-past"])
    assert res.exit_code == 0 and "4 new lab(s)" in res.stdout
    bad = tmp_path / "bad.json"
    bad.write_text("Refused: something")
    res = runner.invoke(app, ["lms", "check", "--assignments", str(bad), "--json"])
    assert res.exit_code == 3 and json.loads(res.stdout)["error"]["code"] == "MCP_UNAVAILABLE"


def test_doctor_nuts_lms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from labpilot import doctor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.nuts_lms()["fix"].startswith("uv tool install")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/bin/moodle-mcp")
    assert re.search(r"moodle[-_]mcp login", doctor.nuts_lms()["fix"])  # `-m moodle_mcp` form on Windows
    (tmp_path / "cache" / "moodle-mcp").mkdir(parents=True)
    (tmp_path / "cache" / "moodle-mcp" / "lms.x.edu.token.json").write_text("{}")
    assert "claude mcp add" in doctor.nuts_lms()["fix"]
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"moodle": {"command": "moodle-mcp", "args": ["serve"]}}}))
    assert doctor.nuts_lms()["ready"] is True
