"""Evidence manifest persistence."""

from __future__ import annotations

from pathlib import Path

from labpilot.models import EvidenceRecord
from labpilot.workspace import atomic_write_json, read_json


def manifest_path(run_dir: Path) -> Path:
    return run_dir / "evidence" / "evidence_manifest.json"


def load_evidence(run_dir: Path) -> list[EvidenceRecord]:
    path = manifest_path(run_dir)
    if not path.exists():
        return []
    data = read_json(path)
    return [EvidenceRecord.model_validate(r) for r in data.get("records", [])]


def save_evidence(run_dir: Path, records: list[EvidenceRecord], extra: dict | None = None) -> Path:
    path = manifest_path(run_dir)
    payload = {"schema_version": 1, **(extra or {}), "records": [r.model_dump(mode="json") for r in records]}
    atomic_write_json(path, payload)
    return path
