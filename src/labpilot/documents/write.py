"""Populate a COPY of the original manual with evidence and answers, preserving original content."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docx
from docx.enum.text import WD_BREAK
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Pt, RGBColor
from docx.table import _Cell
from docx.text.paragraph import Paragraph

from labpilot.documents.anchors import W_P, W_TC, element_text, iter_blocks, normalize, resolve
from labpilot.errors import LabPilotError
from labpilot.models import Answer, EvidenceRecord, Plan, TableCellFill, Task

EMU_PER_INCH = 914400
DXA_PER_INCH = 1440
DEFAULT_CELL_MARGIN_DXA = 108
CAPTURE_DPI = 96 * 2  # screenshots are captured at device scale 2
MAX_IMAGE_HEIGHT_IN = 8.3
PLACEHOLDER_RE = re.compile(
    r"^\s*(\[.*\]|_{3,}|\.{3,}|"
    r"(paste|insert|add|write|put|attach)\b.*\b(here|code|screenshot|screenshots|output|outputs|answer|answers|solution)\b.*|"
    r"(your\s+)?(solution|answer|code|screenshot)s?\s*(here)?\s*[:.]?)\s*$",
    re.I,
)
UNVERIFIED = "Not verified — required dataset/runtime unavailable"


@dataclass
class WriteReport:
    output: str
    placements: list[dict[str, Any]] = field(default_factory=list)
    inserted_images: list[dict[str, Any]] = field(default_factory=list)
    removed_placeholders: list[dict[str, str]] = field(default_factory=list)
    layout_changes: list[str] = field(default_factory=list)
    student_fields_filled: list[str] = field(default_factory=list)
    table_fills: list[dict[str, str]] = field(default_factory=list)
    figure_count: int = 0
    warnings: list[str] = field(default_factory=list)
    review_required: list[str] = field(default_factory=list)


@dataclass
class TaskContent:
    task: Task
    explanation: str | None
    evidence: list[EvidenceRecord]
    answers: list[tuple[str, Answer | None]]  # (question text, answer)
    status: str  # passed / failed / blocked / pending


def _available_width_in(document: Any, tc: Any | None) -> float:
    section = document.sections[0]
    page_w = (section.page_width - section.left_margin - section.right_margin) / EMU_PER_INCH
    if tc is None:
        return max(2.0, page_w)
    width_dxa = None
    tcpr = tc.find(qn("w:tcPr"))
    if tcpr is not None:
        tcw = tcpr.find(qn("w:tcW"))
        if tcw is not None and tcw.get(qn("w:type")) in (None, "dxa"):
            try:
                width_dxa = int(tcw.get(qn("w:w")))
            except (TypeError, ValueError):
                width_dxa = None
    if not width_dxa:
        tbl = tc.getparent().getparent()
        grid = tbl.find(qn("w:tblGrid"))
        if grid is not None:
            cols = [int(g.get(qn("w:w"), "0")) for g in grid.findall(qn("w:gridCol"))]
            idx = list(c for c in tc.getparent() if c.tag == W_TC).index(tc)
            span = 1
            if tcpr is not None and tcpr.find(qn("w:gridSpan")) is not None:
                span = int(tcpr.find(qn("w:gridSpan")).get(qn("w:val"), "1"))
            if idx < len(cols):
                width_dxa = sum(cols[idx : idx + span])
    if not width_dxa:
        return max(2.0, page_w - 0.2)
    margins = 2 * DEFAULT_CELL_MARGIN_DXA
    width_in = (width_dxa - margins) / DXA_PER_INCH
    return max(1.5, min(width_in, page_w))


def _relax_row(tc: Any, report: WriteReport, anchor: str) -> None:
    tr = tc.getparent()
    trpr = tr.find(qn("w:trPr"))
    if trpr is None:
        return
    h = trpr.find(qn("w:trHeight"))
    if h is not None and h.get(qn("w:hRule")) == "exact":
        h.set(qn("w:hRule"), "atLeast")
        report.layout_changes.append(f"{anchor}: row height exact -> atLeast so content can expand")
    cs = trpr.find(qn("w:cantSplit"))
    if cs is not None:
        trpr.remove(cs)
        report.layout_changes.append(f"{anchor}: removed cantSplit so the row can flow across pages")


def _remove_placeholders(tc: Any, report: WriteReport, anchor: str) -> None:
    paras = [b for b in iter_blocks(tc) if b.tag == W_P]
    for p in paras:
        text = element_text(p)
        has_drawing = p.find(".//" + qn("w:drawing")) is not None
        is_label = text.rstrip().endswith(":")  # e.g. "Your answer:" stays; the answer goes below it
        if text and PLACEHOLDER_RE.match(text) and not has_drawing and not is_label:
            report.removed_placeholders.append({"anchor": anchor, "text": normalize(text)})
            if len([b for b in iter_blocks(tc)]) > 1:
                tc.remove(p)
            else:  # a cell must keep one paragraph: blank it instead
                for r in list(p):
                    if r.tag != qn("w:pPr"):
                        p.remove(r)


class _Sink:
    """Appends paragraphs to a cell, or inserts them before a body element / at the body end."""

    def __init__(self, document: Any, *, tc: Any | None = None, before: Any | None = None) -> None:
        self.document = document
        self.tc = tc
        self.before = before
        self.cell = _Cell(tc, document._body) if tc is not None else None

    def paragraph(self) -> Paragraph:
        if self.cell is not None:
            return self.cell.add_paragraph()
        p = self.document.add_paragraph()
        if self.before is not None:
            self.before.addprevious(p._p)
        return p


def _heading(sink: _Sink, text: str, size: int = 12) -> None:
    p = sink.paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.keep_with_next = True
    r = p.add_run(text)
    r.bold = True
    r.font.size = Pt(size)


def _label(sink: _Sink, text: str, italic: bool = True) -> None:
    p = sink.paragraph()
    p.paragraph_format.keep_with_next = True
    r = p.add_run(text)
    r.italic = italic
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(0x44, 0x44, 0x44)


def _text(sink: _Sink, label: str | None, text: str, color: RGBColor | None = None) -> None:
    p = sink.paragraph()
    if label:
        lr = p.add_run(label + " ")
        lr.bold = True
    r = p.add_run(text)
    if color is not None:
        r.font.color.rgb = color


def _picture(sink: _Sink, path: Path, rec: EvidenceRecord, avail_in: float) -> dict[str, Any]:
    natural_w = rec.width / CAPTURE_DPI
    natural_h = rec.height / CAPTURE_DPI
    width = min(natural_w, avail_in)
    if natural_h * (width / natural_w) > MAX_IMAGE_HEIGHT_IN:
        width = MAX_IMAGE_HEIGHT_IN * natural_w / natural_h
    p = sink.paragraph()
    run = p.add_run()
    run.add_picture(str(path), width=Emu(int(width * EMU_PER_INCH)))
    return {"path": rec.path, "sha256": rec.sha256, "width_in": round(width, 3), "task_id": rec.task_id}


def display_heading(t: Task) -> str:
    """Heading in the manual's own terms: 'Question 3.2: Add a combiner', 'Part A: ...', or the plain title."""
    m = re.fullmatch(r"T(\d+)\.(\d+)", t.id)
    if m:
        return f"Question {m.group(1)}.{m.group(2)}: {t.title}"
    m = re.fullmatch(r"T(\d+)\.([A-Z])", t.id)
    if m:
        return f"Task {m.group(1)}, Part {m.group(2)}: {t.title}"
    m = re.fullmatch(r"T(\d+)", t.id)
    if m:
        return f"Task {m.group(1)}: {t.title}"
    return t.title


def _write_task(sink: _Sink, run_dir: Path, content: TaskContent, avail: float, run_label: str, report: WriteReport) -> None:
    t = content.task
    _heading(sink, display_heading(t))
    if content.status != "passed":
        _text(
            sink,
            "Status:",
            f"{content.status.upper()} — this part is incomplete. {UNVERIFIED if content.status != 'failed' else 'Execution failed; see REVIEW.md.'}",
            RGBColor(0xB0, 0x00, 0x00),
        )
    if content.explanation:
        _text(sink, "What the code does:", content.explanation)
    for rec in content.evidence:
        what = {"code-output": "Code and output", "code": "Code", "output": "Output"}[rec.kind]
        part = f", part {rec.part} of {rec.parts}" if rec.parts > 1 else ""
        report.figure_count += 1
        _label(sink, f"Figure {report.figure_count}: {what}{part} ({run_label})")
        report.inserted_images.append(_picture(sink, run_dir / rec.path, rec, avail))
    if content.answers:
        _label(sink, "Observations", italic=False)
        for qtext, ans in content.answers:
            q = sink.paragraph()
            qr = q.add_run(qtext)
            qr.italic = True
            if ans is None:
                _text(sink, "Answer:", UNVERIFIED, RGBColor(0xB0, 0x00, 0x00))
            elif ans.verification == "not_verified":
                _text(sink, "Answer:", f"{ans.text} ({UNVERIFIED})", RGBColor(0xB0, 0x00, 0x00))
            else:
                _text(sink, "Answer:", ans.text)


def _insertion_point_after_task(body: Any, plan: Plan, task: Task) -> Any | None:
    """Body-level element before which a task's solution goes: the next task's heading, else None (end)."""
    tasks = plan.tasks
    idx = tasks.index(task)
    parent = task.parent or task.id
    for nxt in tasks[idx + 1 :]:
        if (nxt.parent or nxt.id) != parent and nxt.source_anchor:
            el = resolve(body, nxt.source_anchor)
            while el.getparent() is not None and el.getparent() is not body and el.getparent().tag != qn("w:body"):
                el = el.getparent()
            return el
    return None


def write_document(
    *,
    source: Path,
    out_path: Path,
    run_dir: Path,
    plan: Plan,
    contents: list[TaskContent],
    run_label: str,
    student: dict[str, str] | None = None,
    student_fields: list[dict[str, Any]] | None = None,
    table_fills: list[TableCellFill] | None = None,
) -> WriteReport:
    shutil.copyfile(source, out_path)  # edit a fresh writable copy; the original stays read-only and untouched
    document = docx.Document(str(out_path))
    body = document.element.body
    report = WriteReport(output=out_path.name)

    # resolve every anchor BEFORE mutating, so indices refer to the pristine document
    groups: dict[str, list[TaskContent]] = {}
    resolved: dict[str, Any] = {}
    after_source: list[tuple[TaskContent, Any | None]] = []
    at_end: list[TaskContent] = []
    appended: list[TaskContent] = []
    for content in contents:
        t = content.task
        if t.placement == "anchor" and t.solution_anchor:
            try:
                el = resolve(body, t.solution_anchor)
            except LabPilotError as exc:
                report.review_required.append(f"{t.id}: {exc.message} Solution appended at the end instead.")
                appended.append(content)
                continue
            key = t.solution_anchor.split("#")[0]
            resolved[key] = el
            groups.setdefault(key, []).append(content)
        elif t.placement == "after_source" and t.source_anchor:
            try:
                after_source.append((content, _insertion_point_after_task(body, plan, t)))
            except LabPilotError as exc:
                report.review_required.append(f"{t.id}: {exc.message} Solution appended at the end instead.")
                appended.append(content)
        elif t.placement == "end":
            at_end.append(content)
        else:
            appended.append(content)

    student_targets: list[tuple[Any, str, str]] = []
    if student and student_fields:
        for sf in student_fields:
            skey = _student_key(sf["label"])
            if skey and skey in student and not sf.get("has_value"):
                try:
                    student_targets.append((resolve(body, sf["anchor"]), sf["label"], student[skey]))
                except LabPilotError:
                    report.warnings.append(f"student field '{sf['label']}' moved; not filled")

    fill_targets: list[tuple[Any, TableCellFill]] = []
    for f in table_fills or []:
        try:
            fill_targets.append((resolve(body, f.anchor), f))
        except LabPilotError as exc:
            report.review_required.append(f"table cell {f.anchor}: {exc.message} Value not written.")

    page_w = _available_width_in(document, None)
    for key, items in groups.items():
        el = resolved[key]
        if el.tag == W_TC:
            _relax_row(el, report, key)
            _remove_placeholders(el, report, key)
            sink = _Sink(document, tc=el)
            avail = _available_width_in(document, el)
        else:  # paragraph anchor: insert right after it
            nxt = el.getnext()
            sink = _Sink(document, before=nxt) if nxt is not None and nxt.tag != qn("w:sectPr") else _Sink(document, before=body.find(qn("w:sectPr")))
            avail = page_w
        for content in items:
            _write_task(sink, run_dir, content, avail, run_label, report)
            report.placements.append({"task_id": content.task.id, "anchor": key, "mode": "anchor"})

    for content, before in after_source:
        if before is None:
            before = body.find(qn("w:sectPr"))
        sink = _Sink(document, before=before)
        _write_task(sink, run_dir, content, page_w, run_label, report)
        report.placements.append({"task_id": content.task.id, "anchor": None, "mode": "after_source"})

    # closing sections the manual expects after the last task (e.g. cluster teardown), in plan order
    for content in at_end:
        sink = _Sink(document, before=body.find(qn("w:sectPr")))
        _write_task(sink, run_dir, content, page_w, run_label, report)
        report.placements.append({"task_id": content.task.id, "anchor": None, "mode": "end"})

    if appended:
        sect = body.find(qn("w:sectPr"))
        sink = _Sink(document, before=sect)
        p = sink.paragraph()
        p.add_run().add_break(WD_BREAK.PAGE)
        _heading(sink, plan.appendix_title, 13)
        for content in appended:
            _write_task(sink, run_dir, content, page_w, run_label, report)
            report.placements.append({"task_id": content.task.id, "anchor": None, "mode": "append"})

    for el, label, value in student_targets:
        if el.tag == W_P:  # noqa: SIM108 - clearer than a ternary with a type: ignore
            target = Paragraph(el, document._body)
        else:
            target = _Cell(el, document._body).paragraphs[-1]  # type: ignore[arg-type]
        target.add_run(" " + value)
        report.student_fields_filled.append(label)

    for el, f in fill_targets:
        cell = _Cell(el, document._body)  # type: ignore[arg-type]
        value = f.text if f.verification == "verified" else f"{f.text} ({UNVERIFIED})"
        para = cell.paragraphs[-1]
        if para.text.strip():
            para = cell.add_paragraph()
        run = para.add_run(value)
        if f.verification != "verified":
            run.font.color.rgb = RGBColor(0xB0, 0x00, 0x00)
        report.table_fills.append({"anchor": f.anchor.split("#")[0], "text": f.text})

    document.save(str(out_path))
    return report


def _student_key(label: str) -> str | None:
    s = label.lower()
    if "name" in s:
        return "name"
    if "cms" in s or "reg" in s or "roll" in s:
        return "id"
    if "class" in s:
        return "class"
    if "section" in s:
        return "section"
    return None


__all__ = ["write_document", "WriteReport", "TaskContent", "UNVERIFIED", "Inches"]
