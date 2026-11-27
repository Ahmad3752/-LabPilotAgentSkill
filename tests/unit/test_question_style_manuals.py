"""Manuals whose tasks are split into "Question N:" sections, each with its own "Your answer:" box,
plus fill-in tables (a common university lab format). Synthetic content only."""

from __future__ import annotations

from pathlib import Path

import docx
from PIL import Image, ImageDraw

from labpilot.documents.inspect import draft_plan, inspect_docx
from labpilot.documents.validate import validate_docx
from labpilot.documents.write import TaskContent, write_document
from labpilot.models import Answer, EvidenceRecord, Plan, TableCellFill
from labpilot.workspace import sha256_file


def _manual(path: Path) -> Path:
    d = docx.Document()
    d.add_paragraph("Lab 9: Synthetic Practice")
    d.add_heading("Task 3: Aggregation", level=1)
    d.add_heading("Question 1: Total per group", level=2)
    d.add_paragraph("Compute the totals.")
    d.add_paragraph("1. What is the key?")
    d.add_table(rows=1, cols=1).rows[0].cells[0].text = "Your answer:"
    d.add_heading("Question 2: Add a combiner", level=2)
    counters = d.add_table(rows=3, cols=3)
    for c, txt in enumerate(["Counter", "Without", "With"]):
        counters.rows[0].cells[c].text = txt
    counters.rows[1].cells[0].text = "Map output records"
    counters.rows[2].cells[0].text = "Reduce input records"
    d.add_paragraph("1. Did the result change?")
    d.add_paragraph("2. Are the outputs sorted together as one list? Explain.")
    d.add_table(rows=1, cols=1).rows[0].cells[0].text = "Your answer:"
    hdr = d.sections[0].header.paragraphs[0]
    hdr.text = "Synthetic header"
    d.save(str(path))
    return path


def test_question_sections_become_units_with_their_own_boxes(tmp_path: Path) -> None:
    p = _manual(tmp_path / "m.docx")
    insp = inspect_docx(p, sha256_file(p))
    assert [u["id"] for t in insp.tasks for u in t["parts"]] == ["T3.1", "T3.2"]
    boxes = insp.solution_boxes
    assert [(b["scope"], b["unit_hint"]) for b in boxes] == [("unit", "T3.1"), ("unit", "T3.2")]
    assert not [w for w in insp.warnings if "More than one solution box" in w]
    plan = draft_plan(insp, [])
    by_id = {t["id"]: t for t in plan["tasks"]}
    assert by_id["T3.1"]["solution_anchor"] == boxes[0]["fill_anchor"]
    assert by_id["T3.2"]["solution_anchor"] == boxes[1]["fill_anchor"]
    assert len(insp.fill_in_tables) == 1 and len(insp.fill_in_tables[0]["empty_cells"]) == 4
    t32 = next(p for t in insp.tasks for p in t["parts"] if p["id"] == "T3.2")
    assert [q["text"][:5] for q in t32["questions"]] == ["1. Di", "2. Ar"]  # "...? Explain." is a question too


def test_answers_keep_label_and_table_cells_are_filled(tmp_path: Path) -> None:
    p = _manual(tmp_path / "m.docx")
    insp = inspect_docx(p, sha256_file(p))
    plan = Plan.model_validate(draft_plan(insp, []))
    img = tmp_path / "e.png"
    im = Image.new("RGB", (800, 300), "white")
    ImageDraw.Draw(im).rectangle((10, 10, 790, 290), outline="black", width=4)
    im.save(img)
    contents = []
    for t in plan.tasks:
        rec = EvidenceRecord(run_id="r", attempt_id="a", task_id=t.id, cell_ids=["c"], code_sha256={"c": "x"},
                             backend="test", capture_mode="rendered-local-notebook", kind="code-output",
                             captured_at="t", sha256=sha256_file(img), width=800, height=300, path="e.png")
        contents.append(TaskContent(task=t, explanation=None, evidence=[rec] if t.id == "T3.1" else [],
                                    answers=[(q.text, Answer(question_id=q.id, text="ans", verification="conceptual"))
                                             for q in t.observation_questions], status="passed"))
    empty = insp.fill_in_tables[0]["empty_cells"]
    fills = [TableCellFill(anchor=empty[0], text="200000", source_cell_ids=["c"]),
             TableCellFill(anchor=empty[1], text="200000", source_cell_ids=["c"])]
    out = tmp_path / "o.docx"
    rep = write_document(source=p, out_path=out, run_dir=tmp_path, plan=plan, contents=contents,
                         table_fills=fills)
    assert rep.removed_placeholders == []  # "Your answer:" labels are kept
    assert len(rep.table_fills) == 2
    d = docx.Document(str(out))
    assert d.tables[0].rows[0].cells[0].text.startswith("Your answer:")
    assert d.tables[1].rows[1].cells[1].text == "200000" and d.tables[1].rows[1].cells[2].text == "200000"
    v = validate_docx(source=p, source_sha256=sha256_file(p), output=out, expected_images=rep.inserted_images,
                      removed_placeholders=rep.removed_placeholders)
    assert v["ok"], v["problems"]


def test_intro_promises_without_a_task_are_flagged(tmp_path: Path) -> None:
    d = docx.Document()
    d.add_paragraph("Lab 9: Synthetic Practice")
    d.add_paragraph("This lab covers joins, YARN and proper cluster teardown.")
    d.add_heading("Task 1: Joins", level=1)
    d.add_paragraph("Join the two files.")
    p = d.save(str(tmp_path / "m.docx")) or tmp_path / "m.docx"
    insp = inspect_docx(p, sha256_file(p))
    assert insp.uncovered_mentions == ["teardown"]
    assert any("mentions 'teardown'" in w for w in insp.warnings)


def test_end_section_and_appendix_title(tmp_path: Path) -> None:
    p = _manual(tmp_path / "m.docx")
    insp = inspect_docx(p, sha256_file(p))
    data = draft_plan(insp, [])
    assert "SETUP" not in [t["id"] for t in data["tasks"]]  # the manual has no setup section: no invented one
    data["appendix_title"] = "Appendix: Environment setup (re-run)"
    data["tasks"].append({"id": "TEARDOWN", "title": "Cluster teardown and deliverables", "placement": "end",
                          "depends_on": ["T3.2"], "required_evidence": []})
    data["tasks"].append({"id": "SETUP", "title": "Setup", "placement": "append", "required_evidence": []})
    plan = Plan.model_validate(data)
    contents = [TaskContent(task=t, explanation="x", evidence=[], answers=[], status="passed") for t in plan.tasks]
    out = tmp_path / "o.docx"
    rep = write_document(source=p, out_path=out, run_dir=tmp_path, plan=plan, contents=contents)
    modes = {pl["task_id"]: pl["mode"] for pl in rep.placements}
    assert modes["TEARDOWN"] == "end" and modes["SETUP"] == "append"
    texts = [para.text for para in docx.Document(str(out)).paragraphs]
    i_end, i_app = texts.index("Cluster teardown and deliverables"), texts.index("Appendix: Environment setup (re-run)")
    assert i_end < i_app  # the closing section comes before the appendix
    assert "Question 3.1: Total per group" in " ".join(
        c.text for t in docx.Document(str(out)).tables for c in t._cells)
