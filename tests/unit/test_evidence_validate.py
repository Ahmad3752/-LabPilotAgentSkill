from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from labpilot.evidence.validate import image_problems, validate_evidence
from labpilot.models import EvidenceRecord, ItemStatus, RunManifest
from labpilot.workspace import sha256_file


def _img(path: Path, blank: bool = False) -> Path:
    im = Image.new("RGB", (400, 200), "white")
    if not blank:
        ImageDraw.Draw(im).text((10, 10), "output 42", fill="black")
        ImageDraw.Draw(im).rectangle((5, 50, 390, 190), outline="black", width=4)
    im.save(path)
    return path


def test_image_checks(tmp_path: Path) -> None:
    assert image_problems(_img(tmp_path / "ok.png"))[0] == []
    assert "image appears blank" in image_problems(_img(tmp_path / "b.png", blank=True))[0]
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG not really")
    assert "does not decode" in image_problems(bad)[0][0]


def _rec(path: Path, run_dir: Path, **kw: object) -> EvidenceRecord:
    base = dict(run_id="r", attempt_id="a002", task_id="T1", cell_ids=["c1"], code_sha256={"c1": "h1"}, backend="b",
                capture_mode="rendered-local-notebook", kind="code-output", captured_at="t",
                sha256=sha256_file(path), width=400, height=200, path=path.relative_to(run_dir).as_posix())
    base.update(kw)
    return EvidenceRecord.model_validate(base)


def test_stale_and_tampered_evidence(tmp_path: Path) -> None:
    p = _img(tmp_path / "e.png")
    m = RunManifest(run_id="r", lab_dir=str(tmp_path), source_name="m.docx", source_sha256="x", current_attempt="a002",
                    cell_code_sha256={"c1": "h1"}, cell_status={"c1": ItemStatus.PASSED})
    assert validate_evidence(tmp_path, [_rec(p, tmp_path)], m) == []
    assert any("not the current attempt" in x for x in validate_evidence(tmp_path, [_rec(p, tmp_path, attempt_id="a001")], m))
    m.cell_code_sha256["c1"] = "changed"
    assert any("stale" in x for x in validate_evidence(tmp_path, [_rec(p, tmp_path)], m))
    m.cell_code_sha256["c1"] = "h1"
    m.cell_status["c1"] = ItemStatus.FAILED
    assert any("not in passed state" in x for x in validate_evidence(tmp_path, [_rec(p, tmp_path)], m))
    m.cell_status["c1"] = ItemStatus.PASSED
    rec = _rec(p, tmp_path)
    _img(p, blank=True)  # file replaced after capture
    probs = validate_evidence(tmp_path, [rec], m)
    assert any("hash mismatch" in x for x in probs) and any("blank" in x for x in probs)
