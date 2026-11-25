"""Find required input files inside the approved lab folder only (bounded, no machine-wide search)."""

from __future__ import annotations

import difflib
from pathlib import Path

from labpilot.models import DatasetEntry
from labpilot.security import is_within

DATA_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl", ".xlsx", ".xls", ".parquet", ".txt", ".png", ".jpg", ".jpeg", ".xml", ".zip"}
SKIP_DIRS = {".labpilot", "output", ".git", ".venv", "venv", "__pycache__", "node_modules", ".ipynb_checkpoints"}
SKIP_FILES = {"lms_source.json"}  # LabPilot's own LMS provenance record, never a dataset
MAX_DEPTH = 3
MAX_FILES = 2000


def list_candidate_files(lab_dir: Path) -> list[Path]:
    root = lab_dir.resolve()
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if depth > MAX_DEPTH or len(found) >= MAX_FILES:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for p in entries:
            if len(found) >= MAX_FILES:
                return
            if p.is_symlink() and not is_within(p, root):
                continue  # never follow links out of the approved folder
            if p.is_dir():
                if p.name in SKIP_DIRS or p.name.startswith("."):
                    continue
                walk(p, depth + 1)
            elif p.suffix.lower() in DATA_SUFFIXES and p.is_file() and p.name not in SKIP_FILES:
                found.append(p)

    walk(root, 0)
    return found


def match_required(
    lab_dir: Path,
    expected_name: str,
    aliases: list[str] | None = None,
    candidates: list[Path] | None = None,
) -> DatasetEntry:
    """Resolve one expected file name to an actual file, never guessing among conflicts."""
    root = lab_dir.resolve()
    files = candidates if candidates is not None else list_candidate_files(root)
    rel = {f: f.resolve().relative_to(root).as_posix() for f in files}
    entry = DatasetEntry(expected_name=expected_name)

    exact = [f for f in files if f.name == expected_name]
    if len(exact) == 1:
        entry.status, entry.local_path = "found", rel[exact[0]]
        return entry
    if len(exact) > 1:
        entry.status, entry.candidates = "ambiguous", sorted(rel[f] for f in exact)
        entry.problems.append("Several files share this exact name in different folders.")
        return entry

    for alias in aliases or []:
        hits = [f for f in files if f.name == alias or rel[f] == alias]
        if len(hits) == 1:
            entry.status, entry.local_path = "mapped", rel[hits[0]]
            entry.problems.append(f"Mapped by explicit alias '{alias}'.")
            return entry

    ci = [f for f in files if f.name.lower() == expected_name.lower()]
    if len(ci) == 1:
        entry.status, entry.local_path = "mapped", rel[ci[0]]
        entry.problems.append(f"Case-insensitive match '{ci[0].name}'.")
        return entry
    if len(ci) > 1:
        entry.status, entry.candidates = "ambiguous", sorted(rel[f] for f in ci)
        return entry

    suffix = Path(expected_name).suffix.lower()
    same_type = [f for f in files if f.suffix.lower() == suffix]
    close = difflib.get_close_matches(expected_name.lower(), [f.name.lower() for f in same_type], n=3, cutoff=0.75)
    if close:
        entry.candidates = sorted(rel[f] for f in same_type if f.name.lower() in close)
        entry.problems.append(
            "Not found by name; similar files exist. Confirm with an explicit alias "
            "(labpilot inspect --alias EXPECTED=ACTUAL) after checking the schema."
        )
    entry.status = "missing"
    return entry
