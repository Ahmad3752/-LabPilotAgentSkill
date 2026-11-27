"""Word copy population and structural validation (unit level, with locally generated test PNGs).

The PNGs here are synthetic test images used only to exercise the DOCX writer; they are never
presented as execution evidence.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import docx
from docx.oxml.ns import qn
from PIL import Image, ImageDraw

from labpilot.documents.inspect import draft_plan, inspect_docx
from labpilot.documents.validate import validate_docx
from labpilot.documents.write import UNVERIFIED, TaskContent, write_document
from labpilot.models import Answer, EvidenceRecord, Plan
from labpilot.workspace import sha256_file


def _png(path: Path, w: int = 1200, h: int = 500) -> EvidenceRecord:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (w, h), "white")
    ImageDraw.Draw(img).text((20, 20), f"test image {path.name}", fill="black")
    ImageDraw.Draw(img).rectangle((10, 60, w - 10, h - 10), outline="black", width=3)
    img.save(path)
    return EvidenceRecord(run_id="r", attempt_id="a001", task_id="", cell_ids=["c"], code_sha256={"c": "x"},
                          backend="test", capture_mode="rendered-local-notebook", kind="code-output",
                          captured_at="2026-01-01T00:00:00Z", sha256=sha256_file(path), width=w, height=h,
                          path=path.name)


def _setup(python_lab: Path, tmp_path: Path) -> tuple[Path, Plan, list[TaskContent]]:
    src = python_lab / "Lab 3.docx"
    insp = inspect_docx(src, sha256_file(src))
    plan = Plan.model_validate(draft_plan(insp, []))
    contents = []
    for i, t in enumerate(plan.tasks):
        rec = _png(tmp_path / f"img{i}.png").model_copy(update={"task_id": t.id})
        answers = [(q.text, Answer(question_id=q.id, text=f"answer {q.id}", verification="conceptual"))
                   for q in t.observation_questions]
        contents.append(TaskContent(task=t, explanation=f"explains {t.id}", evidence=[rec], answers=answers,
                                    status="passed"))
    return src, plan, contents


def test_write_into_solution_boxes_and_validate(python_lab: Path, tmp_path: Path) -> None:
    src, plan, contents = _setup(python_lab, tmp_path)
    before = sha256_file(src)
    out = tmp_path / "out.docx"
    rep = write_document(source=src, out_path=out, run_dir=tmp_path, plan=plan, contents=contents)
    assert sha256_file(src) == before  # original untouched
    assert {p["mode"] for p in rep.placements} == {"anchor", "after_source"}  # SETUP follows the manual's Setup section
    assert len(rep.inserted_images) == len(contents)
    assert len(rep.removed_placeholders) == 2
    assert len(rep.layout_changes) == 4  # exact height + cantSplit on both boxes
    v = validate_docx(source=src, source_sha256=before, output=out, expected_images=rep.inserted_images,
                      removed_placeholders=rep.removed_placeholders)
    assert v["ok"], v["problems"]

    d = docx.Document(str(out))
    tables = d.tables
    box1 = tables[0].rows[1].cells[0]
    text = box1.text
    assert "Task 1, Part A: Load Both Files" in text and "Task 1, Part B: Compute Revenue" in text
    assert text.index("Part A: Load") < text.index("Part B: Compute")
    assert "Figure 1: Code and output\n" in text and "Colab" not in text  # where it ran lives in REVIEW.md
    assert "explains T1" in text and "What the code does" not in text  # plain paragraph, no label
    assert "Paste your code" not in text
    assert len(box1._tc.findall(".//" + qn("a:blip"))) == 2  # inline images inside the cell
    trpr = tables[0].rows[1]._tr.trPr
    assert trpr.find(qn("w:cantSplit")) is None
    # images are inline (not floating) and fit the cell width
    assert not list(d.element.body.iter(qn("wp:anchor")))


def test_regeneration_does_not_duplicate(python_lab: Path, tmp_path: Path) -> None:
    src, plan, contents = _setup(python_lab, tmp_path)
    out = tmp_path / "out.docx"
    for _ in range(2):
        rep = write_document(source=src, out_path=out, run_dir=tmp_path, plan=plan, contents=contents)
    with zipfile.ZipFile(out) as zf:
        media = [n for n in zf.namelist() if n.startswith("word/media/")]
    assert len(media) == len(contents) + 1  # +1 original logo; second write started from the pristine source
    assert len(rep.inserted_images) == len(contents)


def test_incomplete_tasks_are_labelled_not_verified(python_lab: Path, tmp_path: Path) -> None:
    src, plan, contents = _setup(python_lab, tmp_path)
    contents[1] = TaskContent(task=contents[1].task, explanation=None, evidence=[],
                              answers=[(q.text, None) for q in contents[1].task.observation_questions], status="blocked")
    out = tmp_path / "out.docx"
    write_document(source=src, out_path=out, run_dir=tmp_path, plan=plan, contents=contents)
    text = "\n".join(p.text for p in docx.Document(str(out)).tables[0].rows[1].cells[0].paragraphs)
    assert "BLOCKED" in text and UNVERIFIED in text


def test_validation_detects_lost_original_text_and_missing_images(python_lab: Path, tmp_path: Path) -> None:
    src, plan, contents = _setup(python_lab, tmp_path)
    out = tmp_path / "out.docx"
    rep = write_document(source=src, out_path=out, run_dir=tmp_path, plan=plan, contents=contents)
    d = docx.Document(str(out))
    victim = next(p for p in d.paragraphs if p.text.startswith("Read both CSV files"))
    victim._p.getparent().remove(victim._p)
    d.save(str(out))
    fake_expected = rep.inserted_images + [{"path": "ghost.png", "sha256": "0" * 64}]
    v = validate_docx(source=src, source_sha256=sha256_file(src), output=out, expected_images=fake_expected,
                      removed_placeholders=rep.removed_placeholders)
    assert not v["ok"]
    assert not v["checks"]["original_text_preserved"]
    assert not v["checks"]["all_screenshots_embedded"]


def test_unresolvable_anchor_goes_to_flagged_appendix(python_lab: Path, tmp_path: Path) -> None:
    src, plan, contents = _setup(python_lab, tmp_path)
    t = contents[1].task.model_copy(update={"solution_anchor": "b21/r1/c0#ffffffffffff"})
    contents[1] = TaskContent(task=t, explanation=None, evidence=contents[1].evidence, answers=[], status="passed")
    rep = write_document(source=src, out_path=tmp_path / "o.docx", run_dir=tmp_path, plan=plan, contents=contents)
    assert rep.review_required and "T1.A" in rep.review_required[0]
    assert {"task_id": "T1.A", "anchor": None, "mode": "append"} in rep.placements


def test_student_fields_only_when_supplied(python_lab: Path, tmp_path: Path) -> None:
    src, plan, contents = _setup(python_lab, tmp_path)
    insp = inspect_docx(src, sha256_file(src))
    out = tmp_path / "s.docx"
    write_document(source=src, out_path=out, run_dir=tmp_path, plan=plan, contents=contents,
                   student={"name": "Test Student"}, student_fields=insp.student_fields)
    texts = [p.text for p in docx.Document(str(out)).paragraphs]
    assert any(t.startswith("Name:") and "Test Student" in t for t in texts)
    assert any(t.startswith("CMS ID:") and t.strip().endswith("_") for t in texts)  # not supplied -> untouched


def test_multi_part_evidence_is_inserted_in_order_with_captions(python_lab: Path, tmp_path: Path) -> None:
    import hashlib

    src, plan, contents = _setup(python_lab, tmp_path)
    t = contents[1].task
    parts = [_png(tmp_path / f"code-part{i}.png", 1500, 2100).model_copy(update={"task_id": t.id, "kind": "code",
                                                                                "part": i, "parts": 2})
             for i in (1, 2)]
    out_rec = _png(tmp_path / "output.png", 900, 300).model_copy(update={"task_id": t.id, "kind": "output"})
    contents[1] = TaskContent(task=t, explanation=None, evidence=[*parts, out_rec], answers=[], status="passed")
    out = tmp_path / "parts.docx"
    rep = write_document(source=src, out_path=out, run_dir=tmp_path, plan=plan, contents=contents)
    assert rep.review_required == []  # each part fits a page at a readable scale

    d = docx.Document(str(out))
    seq: list[str] = []
    for p in d.tables[0].rows[1].cells[0].paragraphs:
        blips = p._p.findall(".//" + qn("a:blip"))
        if blips:
            blob = d.part.related_parts[blips[0].get(qn("r:embed"))].blob
            seq.append(hashlib.sha256(blob).hexdigest())
            assert p.paragraph_format.keep_with_next  # an image stays on the page of its caption
        elif p.text.startswith("Figure"):
            seq.append(p.text)
    i = seq.index(parts[0].sha256)
    n = int(seq[i + 1].split(":")[0].split()[1])
    assert seq[i : i + 6] == [
        parts[0].sha256, f"Figure {n}: Code (part 1 of 2)",
        parts[1].sha256, f"Figure {n + 1}: Code (continued, part 2 of 2)",
        out_rec.sha256, f"Figure {n + 2}: Output",
    ]


def test_image_too_tall_to_read_is_flagged_not_silently_shrunk(python_lab: Path, tmp_path: Path) -> None:
    src, plan, contents = _setup(python_lab, tmp_path)
    t = contents[1].task
    tall = _png(tmp_path / "tall.png", 1500, 4000).model_copy(update={"task_id": t.id})  # one old unsplit capture
    contents[1] = TaskContent(task=t, explanation=None, evidence=[tall], answers=[], status="passed")
    rep = write_document(source=src, out_path=tmp_path / "t.docx", run_dir=tmp_path, plan=plan, contents=contents)
    assert any("tall.png" in x and "labpilot capture" in x for x in rep.review_required)
