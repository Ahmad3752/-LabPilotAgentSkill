"""New-lab discovery through the Nuts LMS MCP (`moodle-mcp`), host-mediated.

LabPilot never talks to the LMS and never sees LMS credentials. The host agent calls the Nuts
MCP tools (`list_assignments`, `assignment_status`, `download_file`, ...), saves their JSON
results to files, and hands them to these functions:

- `check_labs`: which assignments are labs that are open, not submitted and not yet handled.
- `stage_lab`: record where a downloaded lab folder came from (`lms_source.json`) and mark it
  downloaded in the ledger.
- `mark_lab`: record that a lab was processed (run completed) or ignored by the user.

The ledger is a small JSON file in the user config directory. It holds assignment ids, names,
dates and local folder paths only: no tokens, no file URLs with credentials.

Field names follow moodle-mcp's `lms.assignments` and `lms.submission_status` output
(github.com/deeznust42/nutslms-mcp, reviewed at commit b4aa67a).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from labpilot.config import user_config_dir
from labpilot.errors import ErrorCode, ExitCode, LabPilotError
from labpilot.models import utcnow
from labpilot.workspace import atomic_write_json, sha256_file

PROVIDER = "nuts-lms-mcp"
SOURCE_FILE = "lms_source.json"
LEDGER_FILE = "lms_ledger.json"
DEFAULT_SITE = "default"
DEFAULT_ROOT = "lms-labs"
# "Lab 9", "lab-09", "Lab09", "Lab Task 3", "Laboratory 2", "Open-ended lab"
DEFAULT_LAB_PATTERN = r"\blab(?:oratory)?\b|\blab[\s_#-]*\d+"
STATES = ("downloaded", "processed", "ignored")
DONE_STATES = ("processed", "ignored")
SUBMITTED = "submitted"


# ------------------------------------------------------------------ host-tool JSON


def load_tool_json(path: Path, what: str) -> Any:
    """Read a JSON result the host agent saved from a Nuts MCP tool call."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise LabPilotError(ErrorCode.IO_ERROR, f"Cannot read the {what} file {Path(path).name}: {exc}") from exc
    if text.startswith(("Moodle error:", "Error:", "Refused:")):
        raise LabPilotError(ErrorCode.MCP_UNAVAILABLE, f"The {what} file holds a tool error, not data: {text[:300]}",
                            exit_code=ExitCode.CONFIG,
                            next_action="Fix the Nuts LMS MCP (e.g. `moodle-mcp login`), call the tool again, save "
                                        "its exact JSON and retry.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LabPilotError(ErrorCode.RECORD_INVALID, f"The {what} file is not the tool's JSON output: {exc}",
                            exit_code=ExitCode.INPUT) from exc


def _assignments(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list) or not all(isinstance(a, dict) for a in data):
        raise LabPilotError(ErrorCode.RECORD_INVALID,
                            "Expected the JSON list returned by the `list_assignments` tool.", exit_code=ExitCode.INPUT)
    bad = [i for i, a in enumerate(data) if a.get("assignid") is None or not a.get("name")]
    if bad:
        raise LabPilotError(ErrorCode.RECORD_INVALID,
                            f"Assignment entries {bad[:5]} lack 'assignid' or 'name'; pass list_assignments output "
                            "unchanged.", exit_code=ExitCode.INPUT)
    return data


def _statuses(items: list[Any]) -> dict[str, dict[str, Any]]:
    """Index `assignment_status` results (one object, or a list of them) by assignid."""
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        for s in item if isinstance(item, list) else [item]:
            if not isinstance(s, dict) or s.get("assignid") is None or "status" not in s:
                raise LabPilotError(ErrorCode.RECORD_INVALID,
                                    "Expected `assignment_status` JSON objects with 'assignid' and 'status'.",
                                    exit_code=ExitCode.INPUT)
            out[str(s["assignid"])] = s
    return out


def _moodle_time(value: Any) -> dt.datetime | None:
    """moodle-mcp formats times as local 'YYYY-MM-DD HH:MM'."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(str(value)[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def safe_name(text: str, limit: int = 60) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit].rstrip(" .") or "lab"


# ------------------------------------------------------------------ ledger


def ledger_path() -> Path:
    return user_config_dir() / LEDGER_FILE


def load_ledger() -> dict[str, Any]:
    path = ledger_path()
    if not path.exists():
        return {"schema": 1, "labs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LabPilotError(ErrorCode.IO_ERROR, f"LMS ledger {path.name} is not valid JSON: {exc}",
                            exit_code=ExitCode.CONFIG) from exc
    data.setdefault("labs", {})
    return data


def save_ledger(ledger: dict[str, Any]) -> Path:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, ledger)
    return path


def ledger_key(assignid: Any, site: str | None = None) -> str:
    return f"{site or DEFAULT_SITE}#{assignid}"


# ------------------------------------------------------------------ check


def _summary(a: dict[str, Any]) -> dict[str, Any]:
    return {k: a.get(k) for k in ("assignid", "cmid", "courseid", "course", "name", "duedate", "cutoffdate",
                                  "allowsubmissionsfrom")}


def check_labs(assignments_data: Any, status_data: list[Any] | None = None, *, site: str | None = None,
               pattern: str = DEFAULT_LAB_PATTERN, include_past: bool = False, root: str = DEFAULT_ROOT,
               now: dt.datetime | None = None) -> dict[str, Any]:
    """Classify LMS assignments. Read-only: the ledger is not changed."""
    assignments = _assignments(assignments_data)
    statuses = _statuses(status_data or [])
    labs = load_ledger()["labs"]
    now = now or dt.datetime.now()
    try:
        lab_re = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise LabPilotError(ErrorCode.RECORD_INVALID, f"Invalid --match pattern: {exc}", exit_code=ExitCode.INPUT) from exc

    new, in_progress, handled, submitted, closed, not_labs = [], [], [], [], [], []
    for a in assignments:
        if not lab_re.search(str(a["name"])):
            not_labs.append(a["name"])
            continue
        entry = labs.get(ledger_key(a["assignid"], site))
        info = _summary(a)
        if entry and entry.get("state") in DONE_STATES:
            handled.append({**info, "state": entry["state"], "run_id": entry.get("run_id")})
            continue
        if entry and entry.get("state") == "downloaded":
            in_progress.append({**info, "folder": entry.get("folder")})
            continue
        status = statuses.get(str(a["assignid"]))
        if status and status.get("status") == SUBMITTED:
            submitted.append({**info, "submitted_at": status.get("submitted_at")})
            continue
        closes = _moodle_time(a.get("cutoffdate")) or _moodle_time(a.get("duedate"))
        if closes and closes < now and not include_past:
            closed.append({**info, "closed_at": closes.strftime("%Y-%m-%d %H:%M")})
            continue
        opens = _moodle_time(a.get("allowsubmissionsfrom"))
        due = _moodle_time(a.get("duedate"))
        new.append({
            **info,
            "submission_status": status.get("status") if status else "unknown",
            "not_open_yet": bool(opens and opens > now),
            "overdue": bool(due and due < now),
            "suggested_folder": str(Path(root) / safe_name(str(a.get("course") or a.get("courseid") or "course"))
                                    / safe_name(str(a["name"]))),
        })
    new.sort(key=lambda x: x.get("duedate") or "9999")
    needs_status = [x["assignid"] for x in new if x["submission_status"] == "unknown"]
    nxt = ("Call `assignment_status` for each id in needs_status, save the results, and rerun with --status."
           if needs_status else
           "Show the new labs to the user. For each lab they choose, download its manual and data with the Nuts "
           "MCP (`download_file`) into suggested_folder, then `labpilot lms stage`." if new else
           "No new labs.")
    return {"new_labs": new, "needs_status": needs_status, "in_progress": in_progress, "handled": handled,
            "submitted": submitted, "closed": closed, "not_labs": len(not_labs),
            "counts": {"assignments": len(assignments), "new": len(new), "in_progress": len(in_progress),
                       "handled": len(handled), "submitted": len(submitted), "closed": len(closed),
                       "not_labs": len(not_labs)},
            "match": pattern, "next": nxt}


# ------------------------------------------------------------------ stage / mark


def _find(assignments_data: Any, assignid: int) -> dict[str, Any]:
    for a in _assignments(assignments_data):
        if str(a["assignid"]) == str(assignid):
            return a
    raise LabPilotError(ErrorCode.RECORD_INVALID, f"Assignment {assignid} is not in the list_assignments file.",
                        exit_code=ExitCode.INPUT)


def stage_lab(folder: Path, assignments_data: Any, assignid: int, *, site: str | None = None,
              source_urls: list[str] | None = None) -> dict[str, Any]:
    """Record the provenance of a lab folder the host filled with Nuts MCP `download_file`."""
    from labpilot.inputs import LmsStagedFolderInputProvider

    a = _find(assignments_data, assignid)
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise LabPilotError(ErrorCode.DATASET_MISSING, f"Lab folder {folder} does not exist; download the files first.",
                            exit_code=ExitCode.INPUT)
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.name != SOURCE_FILE and not p.name.startswith("~$"))
    if not files:
        raise LabPilotError(ErrorCode.DATASET_MISSING, f"Lab folder {folder.name} is empty; download the files first.",
                            exit_code=ExitCode.INPUT)
    # Moodle pluginfile URLs carry no token (moodle-mcp adds it at download time); drop any query string anyway.
    urls = [u.split("?", 1)[0] for u in source_urls or []]
    source = {
        "provider": PROVIDER, "site": site or DEFAULT_SITE, **_summary(a), "staged_at": utcnow(),
        "files": [{"name": p.name, "sha256": sha256_file(p), "bytes": p.stat().st_size} for p in files],
        "source_urls": urls,
    }
    atomic_write_json(folder / SOURCE_FILE, source)
    manuals = [p.name for p in files if p.suffix.lower() == ".docx"]
    request = None
    problem = None
    try:
        request = LmsStagedFolderInputProvider().fetch(str(folder)).model_dump()
    except LabPilotError as exc:
        others = [p.name for p in files if p.suffix.lower() in (".pdf", ".doc", ".pptx")]
        problem = exc.message + (f" Downloaded non-.docx documents: {others}. LabPilot needs the .docx manual; ask the "
                                 "user for it." if others and not manuals else "")
    ledger = load_ledger()
    ledger["labs"][ledger_key(assignid, site)] = {
        **_summary(a), "site": site or DEFAULT_SITE, "state": "downloaded", "folder": str(folder),
        "updated_at": utcnow(),
    }
    save_ledger(ledger)
    out: dict[str, Any] = {"folder": str(folder), "source_file": str(folder / SOURCE_FILE), "manuals": manuals,
                           "files": [f["name"] for f in source["files"]], "run_request": request}
    if problem:
        out["problem"] = problem
        out["next"] = "Resolve the problem above, then rerun `labpilot lms stage`."
    else:
        manual = Path(request["inputs"]["manual_path"]) if request else folder
        out["next"] = f'labpilot init --lab "{manual}" --json'
    return out


def mark_lab(assignid: int, state: str, *, site: str | None = None, run_id: str | None = None,
             name: str | None = None) -> dict[str, Any]:
    if state not in STATES:
        raise LabPilotError(ErrorCode.RECORD_INVALID, f"state must be one of {', '.join(STATES)}",
                            exit_code=ExitCode.INPUT)
    ledger = load_ledger()
    key = ledger_key(assignid, site)
    entry = ledger["labs"].get(key) or {"assignid": assignid, "site": site or DEFAULT_SITE}
    if name:
        entry["name"] = name
    entry.update({"state": state, "updated_at": utcnow()})
    if run_id:
        entry["run_id"] = run_id
    ledger["labs"][key] = entry
    path = save_ledger(ledger)
    return {"assignid": assignid, "state": state, "ledger": str(path)}


def list_ledger() -> dict[str, Any]:
    labs = load_ledger()["labs"]
    return {"ledger": str(ledger_path()), "labs": sorted(labs.values(), key=lambda e: str(e.get("duedate") or ""))}


def read_source(folder: Path) -> dict[str, Any] | None:
    path = Path(folder) / SOURCE_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("provider") == PROVIDER else None
