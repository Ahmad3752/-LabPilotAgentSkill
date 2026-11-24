"""Resolve, hash, inspect and validate every required file; report affected tasks for anything missing."""

from __future__ import annotations

import shutil
from pathlib import Path

from labpilot.datasets.discover import list_candidate_files, match_required
from labpilot.datasets.inspect import inspect_file
from labpilot.models import Blocker, DatasetEntry, RequiredFile
from labpilot.security import ensure_regular_file_within
from labpilot.workspace import sha256_file


def resolve_datasets(
    lab_dir: Path,
    required: list[RequiredFile],
    aliases: dict[str, str] | None = None,
) -> list[DatasetEntry]:
    files = list_candidate_files(lab_dir)
    entries: list[DatasetEntry] = []
    for req in required:
        alias_list = list(req.aliases)
        if aliases and req.name in aliases:
            alias_list.insert(0, aliases[req.name])
        entry = match_required(lab_dir, req.name, alias_list, files)
        entry.role = req.role
        entry.used_by_tasks = list(req.used_by_tasks)
        if entry.local_path:
            path = ensure_regular_file_within(lab_dir / entry.local_path, lab_dir.resolve())
            entry.sha256 = sha256_file(path)
            entry.size_bytes = path.stat().st_size
            summary = inspect_file(path)
            entry.schema_summary = summary
            if summary.get("problems"):
                entry.problems.extend(summary["problems"])
                if any("empty" in p or "invalid" in p or "cannot decode" in p or "magic" in p for p in summary["problems"]):
                    entry.status = "corrupt"
            if req.expected_columns and "column_names" in summary:
                actual = summary["column_names"]
                missing = [c for c in req.expected_columns if c not in actual]
                if missing:
                    entry.status = "schema_mismatch"
                    entry.problems.append(f"expected columns {missing} not in actual header {actual}")
        if not req.required and entry.status == "missing":
            entry.problems.append("optional file not present")
        entries.append(entry)
    return entries


def dataset_blockers(entries: list[DatasetEntry], required: list[RequiredFile]) -> list[Blocker]:
    req_by_name = {r.name: r for r in required}
    blockers: list[Blocker] = []
    for e in entries:
        req = req_by_name.get(e.expected_name)
        if req is not None and not req.required and e.status == "missing":
            continue
        if e.status in ("found", "mapped"):
            continue
        if e.status == "missing":
            msg = f"Required file '{e.expected_name}' is missing from the lab folder."
            if e.candidates:
                msg += f" Similar files: {', '.join(e.candidates)}."
            action = f"Place '{e.expected_name}' in the lab folder (or map an alias), then `labpilot resume`."
        elif e.status == "ambiguous":
            msg = f"Several candidates for '{e.expected_name}': {', '.join(e.candidates)}."
            action = "Choose one with `labpilot inspect <run-id> --alias EXPECTED=ACTUAL`."
        elif e.status == "schema_mismatch":
            msg = f"'{e.expected_name}' has an unexpected schema: {'; '.join(e.problems)}"
            action = "Supply the correct file; LabPilot never edits source data."
        else:
            msg = f"'{e.expected_name}' is unreadable: {'; '.join(e.problems)}"
            action = "Replace the corrupt file with a valid copy."
        blockers.append(
            Blocker(
                code=f"DATASET_{e.status.upper()}",
                message=msg,
                affected_tasks=e.used_by_tasks,
                next_action=action,
                requires_user=True,
            )
        )
    return blockers


def stage_local(lab_dir: Path, data_dir: Path, entries: list[DatasetEntry]) -> list[DatasetEntry]:
    """Copy resolved datasets into the run's data directory under their expected names and verify hashes."""
    data_dir.mkdir(parents=True, exist_ok=True)
    for e in entries:
        if e.status not in ("found", "mapped") or not e.local_path:
            continue
        src = ensure_regular_file_within(lab_dir / e.local_path, lab_dir.resolve())
        dest = data_dir / e.expected_name
        shutil.copy2(src, dest)
        digest = sha256_file(dest)
        if digest != e.sha256:
            raise OSError(f"staged copy of {e.expected_name} does not match source hash")
        e.staged_path = f"data/{e.expected_name}"
    return entries
