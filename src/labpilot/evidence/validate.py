"""Image and evidence-record validation (decode, non-blank, hash, staleness)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from PIL import Image, ImageStat

from labpilot.models import EvidenceRecord, ItemStatus, RunManifest
from labpilot.workspace import sha256_file

MIN_DIM = 16
BLANK_RANGE = 12  # grayscale max-min below this == visually blank
BLANK_STDDEV = 2.0


def image_problems(path: Path) -> tuple[list[str], int, int]:
    problems: list[str] = []
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            w, h = im.size
            gray = im.convert("L")
            lo, hi = cast(tuple[int, int], gray.getextrema())
            stddev = ImageStat.Stat(gray).stddev[0]
    except Exception as exc:  # noqa: BLE001
        return [f"image does not decode: {exc}"], 0, 0
    if w < MIN_DIM or h < MIN_DIM:
        problems.append(f"image too small ({w}x{h})")
    if (hi - lo) < BLANK_RANGE or stddev < BLANK_STDDEV:
        problems.append("image appears blank")
    return problems, w, h


def validate_evidence(run_dir: Path, records: list[EvidenceRecord], manifest: RunManifest) -> list[str]:
    """Problems that make evidence unusable for the current run state."""
    problems: list[str] = []
    for rec in records:
        path = run_dir / rec.path
        if not path.exists():
            problems.append(f"{rec.path}: file missing")
            continue
        if sha256_file(path) != rec.sha256:
            problems.append(f"{rec.path}: hash mismatch (file changed after capture)")
        img_problems, _, _ = image_problems(path)
        problems.extend(f"{rec.path}: {p}" for p in img_problems)
        if rec.attempt_id != manifest.current_attempt:
            problems.append(f"{rec.path}: captured from attempt {rec.attempt_id}, not the current attempt")
        for cid, digest in rec.code_sha256.items():
            if manifest.cell_code_sha256.get(cid) != digest:
                problems.append(f"{rec.path}: cell {cid} code changed since capture (stale)")
            if manifest.cell_status.get(cid) != ItemStatus.PASSED:
                problems.append(f"{rec.path}: cell {cid} is not in passed state")
    return problems
