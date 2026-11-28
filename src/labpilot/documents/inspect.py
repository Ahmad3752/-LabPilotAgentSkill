"""Read a .docx manual in true document order and extract what the host agent needs to plan.

Everything here is deterministic and conservative: it proposes candidates
(tasks, parts, solution boxes, questions) and a draft plan, but the host agent
reviews them against the original text. Content found only inside images is
reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import docx
from docx.oxml.ns import qn

from labpilot.documents.anchors import (
    W_P,
    W_TBL,
    element_fingerprint,
    element_text,
    iter_blocks,
    iter_cells,
    iter_rows,
    normalize,
)
from labpilot.errors import ErrorCode, LabPilotError
from labpilot.security import check_docx_container

VML_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"
MONO_FONTS = {
    "consolas",
    "courier new",
    "courier",
    "lucida console",
    "menlo",
    "monaco",
    "source code pro",
    "cascadia code",
    "cascadia mono",
    "dejavu sans mono",
    "liberation mono",
    "roboto mono",
    "fira code",
    "jetbrains mono",
}
# "Task 2 – ..." is a task; "Activity 2.1 – ..." (a dotted number) is a child unit of Task 2
TASK_RE = re.compile(
    r"^\s*(?:Lab\s+)?(Task|Exercise|Activity|Problem)\s*[#:.\-]?\s*(\d+)(?:\.(\d+))?\b\s*[:.\-–—]?\s*(.*)$", re.I
)
PART_RE = re.compile(r"^\s*\(?Part\s+([A-Z])\)?\s*[:.\-–—]?\s*(.*)$", re.I)
PROMISED_SECTIONS = {
    "teardown": r"\bteardown\b|\btear(ing)? down\b|\bshut ?down\b|\bstop(ping)? (the )?(cluster|hadoop|daemons)\b",
    "cleanup": r"\bclean ?up\b",
    "deliverables": r"\bdeliverables?\b",
    "submission": r"\bsubmission\b",
}
SOFT_PROMISES = {"deliverables", "submission"}  # usually the filled manual itself; teardown/cleanup are real lab steps
# an explicit "Q1." / "Q2:" / "Q3)" label makes a question even without a "?" ("Q1. Modify the ..."); "Question 1:"
# is left out because manuals use it for section titles ("Question 1: Working with HDFS")
QUESTION_LABEL_RE = re.compile(r"^\s*Q\s*\d+\s*[:.)\-–—]", re.I)
QUESTION_PART_RE = re.compile(r"^\s*Question\s+(\d+)\s*[:.\-–—]\s*(.*)$", re.I)
SOLUTION_RE = re.compile(
    r"\b(solution|answer|your code|paste (your )?(code|screenshot|output)|screenshot(s)? here|write your)\b", re.I
)
SOLUTION_LABEL_RE = re.compile(r"^\s*(?:Task\s*(\d+)\s*)?(Solution|Answer)s?\b", re.I)
QUESTION_START_RE = re.compile(
    r"^\s*(Q\d*[:.)]|Question\b|Observation|Observe|Explain|Why\b|What\b|Which\b|How\b|Does\b|Do\b|Is\b|Are\b|Can\b|Compare\b|Describe\b|Identify\b)",
    re.I,
)
_EXT = r"(?:csv|tsv|json|jsonl|xlsx|xls|parquet|txt|png|jpe?g|xml|zip|db|sqlite3?)"
FILE_RE = re.compile(
    r"[\"'“‘`]([^\"'”’`\n]{1,80}?\." + _EXT + r")[\"'”’`]"  # quoted names may contain spaces
    r"|(?<![\w\\-])([\w][\w\-.]{0,80}\." + _EXT + r")\b(?!\()",  # not a call: spark.read.parquet(...)
    re.I,
)
ENV_PATTERNS = {
    "java": re.compile(r"\bJava\s*(?:JDK\s*)?(\d{1,2})\b", re.I),
    "pyspark": re.compile(r"\bpyspark\s*(?:==|version)?\s*(\d+\.\d+(?:\.\d+)?)", re.I),
    "spark": re.compile(r"\bApache\s+Spark\s*(\d+\.\d+(?:\.\d+)?)?", re.I),
    "python": re.compile(r"\bPython\s*(3(?:\.\d+)?)\b", re.I),
    "spark_master": re.compile(r"local\[(\d+|\*)\]"),
    "colab": re.compile(r"\b(?:Google\s+)?Colab(?:oratory)?\b", re.I),
}
STUDENT_RE = re.compile(
    r"^\s*(Student\s+Name|Name|CMS\s*(ID|No\.?)?|Reg(istration)?\.?\s*(No\.?|Number|#)?|Roll\s*(No\.?|Number)|"
    r"Class|Section|Date)\s*[:：]",
    re.I,
)
NATIVE_COLAB_RE = re.compile(r"screenshot[s]?\s+(of|from)\s+(google\s+)?colab|colab\s+screenshot", re.I)
SETUP_RE = re.compile(r"\b(set\s*-?\s*up|install(ation|ing)?|environment|getting started)\b", re.I)  # setup heading


@dataclass
class BlockInfo:
    anchor: str
    fp: str
    kind: str  # paragraph | table | cell
    depth: int
    text: str
    style: str | None = None
    heading_level: int | None = None
    bold: bool = False
    is_code: bool = False
    images: int = 0
    table_shape: list[int] | None = None


@dataclass
class Inspection:
    source_name: str
    source_sha256: str
    title_candidates: list[str] = field(default_factory=list)
    blocks: list[BlockInfo] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    solution_boxes: list[dict[str, Any]] = field(default_factory=list)
    code_blocks: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    file_references: list[dict[str, Any]] = field(default_factory=list)  # external inputs the lab needs
    generated_files: list[dict[str, Any]] = field(default_factory=list)  # files the manual's own code creates
    environment: dict[str, Any] = field(default_factory=dict)
    student_fields: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    fill_in_tables: list[dict[str, Any]] = field(default_factory=list)
    uncovered_mentions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_native_colab_evidence: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["block_count"] = len(self.blocks)
        return d


def _style_name(p_el: Any, doc: Any) -> str | None:
    ppr = p_el.find(qn("w:pPr"))
    if ppr is None:
        return None
    ps = ppr.find(qn("w:pStyle"))
    if ps is None:
        return None
    sid = ps.get(qn("w:val"))
    try:
        return doc.styles.get_by_id(sid, 1).name  # WD_STYLE_TYPE.PARAGRAPH == 1
    except Exception:  # noqa: BLE001 - unknown style ids are common in converted docs
        return sid


def _heading_level(style: str | None) -> int | None:
    if not style:
        return None
    s = style.lower()
    if s == "title":
        return 0
    m = re.match(r"heading\s*(\d)", s)
    return int(m.group(1)) if m else None


def _runs_info(p_el: Any) -> tuple[bool, bool]:
    """(all_bold, monospace) for a paragraph's text runs."""
    runs = [r for r in p_el.iter(qn("w:r")) if "".join(t.text or "" for t in r.iter(qn("w:t"))).strip()]
    if not runs:
        return False, False
    bold = all(r.find(qn("w:rPr")) is not None and r.find(qn("w:rPr")).find(qn("w:b")) is not None for r in runs)
    mono = 0
    for r in runs:
        rpr = r.find(qn("w:rPr"))
        fonts = rpr.find(qn("w:rFonts")) if rpr is not None else None
        name = ""
        if fonts is not None:
            name = (fonts.get(qn("w:ascii")) or fonts.get(qn("w:hAnsi")) or "").lower()
        if name in MONO_FONTS:
            mono += 1
    return bold, mono == len(runs)


def _count_images(el: Any) -> int:
    return len(list(el.iter(qn("a:blip")))) + len(list(el.iter(VML_IMAGEDATA)))


def _walk(container: Any, prefix: str, depth: int, doc: Any, out: list[BlockInfo]) -> None:
    for idx, block in enumerate(iter_blocks(container)):
        anchor = f"{prefix}b{idx}"
        if block.tag == W_P:
            text = element_text(block)
            style = _style_name(block, doc)
            bold, mono = _runs_info(block)
            code_style = bool(style and re.search(r"code|preformatted|source|macro", style, re.I))
            out.append(
                BlockInfo(
                    anchor=anchor,
                    fp=element_fingerprint(block),
                    kind="paragraph",
                    depth=depth,
                    text=text,
                    style=style,
                    heading_level=_heading_level(style),
                    bold=bold,
                    is_code=mono or code_style,
                    images=_count_images(block),
                )
            )
        elif block.tag == W_TBL:
            rows = iter_rows(block)
            shape = [len(iter_cells(r)) for r in rows]
            out.append(
                BlockInfo(
                    anchor=anchor,
                    fp=element_fingerprint(block),
                    kind="table",
                    depth=depth,
                    text=normalize(element_text(block))[:200],
                    images=_count_images(block),
                    table_shape=shape,
                )
            )
            for r, tr in enumerate(rows):
                for c, tc in enumerate(iter_cells(tr)):
                    cell_anchor = f"{anchor}/r{r}/c{c}"
                    ctext = element_text(tc)
                    out.append(
                        BlockInfo(
                            anchor=cell_anchor,
                            fp=element_fingerprint(tc),
                            kind="cell",
                            depth=depth + 1,
                            text=ctext,
                            images=_count_images(tc),
                        )
                    )
                    _walk(tc, cell_anchor + "/", depth + 2, doc, out)


def _extract_images(doc: Any, body: Any, out_dir: Path | None, blocks: list[BlockInfo]) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    part = doc.part
    # map blip rId -> nearest block anchor (paragraph containing it)
    anchor_by_rid: dict[str, str] = {}
    para_blocks = {b.anchor: b for b in blocks if b.kind == "paragraph" and b.images}
    for anchor in para_blocks:
        from labpilot.documents.anchors import resolve

        el = resolve(body, anchor, check_fingerprint=False)
        for blip in el.iter(qn("a:blip")):
            rid = blip.get(qn("r:embed"))
            if rid:
                anchor_by_rid.setdefault(rid, anchor)
    for n, (rid, anchor) in enumerate(anchor_by_rid.items(), start=1):
        rel = part.rels.get(rid)
        if rel is None or rel.is_external:
            continue
        target = rel.target_part
        ext = Path(str(target.partname)).suffix or ".bin"
        record: dict[str, Any] = {"anchor": anchor, "rid": rid, "content_type": target.content_type}
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"image_{n:02d}{ext}"
            dest.write_bytes(target.blob)
            record["path"] = f"{out_dir.name}/{dest.name}"
        images.append(record)
    return images


def inspect_docx(path: Path, sha256: str, image_dir: Path | None = None) -> Inspection:
    check_docx_container(path)
    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001 - python-docx raises many exception types for corrupt XML
        raise LabPilotError(ErrorCode.DOCX_CORRUPT, f"Cannot open '{path.name}': {exc}") from exc
    body = document.element.body
    blocks: list[BlockInfo] = []
    _walk(body, "", 0, document, blocks)
    insp = Inspection(source_name=path.name, source_sha256=sha256, blocks=blocks)
    _analyse(insp)
    try:
        insp.images = _extract_images(document, body, image_dir, blocks)
    except LabPilotError as exc:  # pragma: no cover - defensive
        insp.warnings.append(f"image extraction failed: {exc.message}")
    for img in insp.images:
        b = next((x for x in blocks if x.anchor == img["anchor"]), None)
        if b is not None and not b.text:
            insp.warnings.append(
                f"Image at {img['anchor']} has no accompanying text; inspect it in case it contains instructions."
            )
    return insp


def _analyse(insp: Inspection) -> None:
    paras = [b for b in insp.blocks if b.kind == "paragraph"]
    # ---- title
    for b in paras:
        if b.heading_level == 0 and b.text:
            insp.title_candidates.append(b.text)
    for b in paras[:40]:
        if re.search(r"\bLab\s*(No\.?\s*)?\d+\b", b.text, re.I) and len(b.text) < 120:
            insp.title_candidates.append(b.text)
    for b in paras:
        if b.heading_level == 1 and b.text:
            insp.title_candidates.append(b.text)
            break
    insp.title_candidates = list(dict.fromkeys(insp.title_candidates))[:5]

    # ---- environment / files / students / native colab
    full_text = "\n".join(b.text for b in paras)
    for key, pattern in ENV_PATTERNS.items():
        found = sorted({(m.group(1) or "yes") if m.groups() else "yes" for m in pattern.finditer(full_text)})
        if found:
            insp.environment[key] = found
    insp.requires_native_colab_evidence = bool(NATIVE_COLAB_RE.search(full_text))
    seen_files: dict[str, dict[str, Any]] = {}
    for b in paras:
        for m in FILE_RE.finditer(b.text):
            name = (m.group(1) or m.group(2)).strip()
            name = name.replace("\\", "/").split("/")[-1]
            entry = seen_files.setdefault(name, {"name": name, "anchors": [], "in_code": False})
            entry["anchors"].append(b.anchor)
            entry["in_code"] = entry["in_code"] or b.is_code
    for entry in seen_files.values():
        entry["role"] = _file_role(entry["name"], full_text)
    insp.file_references = [f for f in seen_files.values() if f["role"] == "input"]
    insp.generated_files = [f for f in seen_files.values() if f["role"] == "output"]
    for b in insp.blocks:
        if b.kind in ("paragraph", "cell") and STUDENT_RE.match(b.text) and len(b.text) < 120:
            label = b.text.split(":")[0].strip()
            value = b.text.split(":", 1)[1].strip() if ":" in b.text else ""
            if not any(s["anchor"] == b.anchor for s in insp.student_fields):
                insp.student_fields.append(
                    {"anchor": f"{b.anchor}#{b.fp}", "label": label, "has_value": bool(value.strip("_ .-"))}
                )

    # ---- code blocks (consecutive monospace / code-style paragraphs)
    current: list[BlockInfo] = []

    def flush() -> None:
        if current:
            insp.code_blocks.append(
                {
                    "anchors": [c.anchor for c in current],
                    "start": f"{current[0].anchor}#{current[0].fp}",
                    "code": "\n".join(c.text for c in current),
                }
            )
            current.clear()

    for b in paras:
        if b.is_code and b.text and not QUESTION_LABEL_RE.match(b.text):
            if current and not _adjacent(current[-1].anchor, b.anchor):
                flush()
            current.append(b)
        else:
            flush()
    flush()

    # ---- tasks and parts in reading order
    task: dict[str, Any] | None = None
    used: set[str] = set()
    for b in paras:
        if b.depth > 1:  # skip text nested inside solution tables
            continue
        text = b.text
        is_headingish = b.heading_level is not None or b.bold or len(text) < 90
        tm = TASK_RE.match(text)
        if tm and is_headingish:
            if re.search(r"\b(solution|answer)s?\b", text, re.I):
                continue
            word, number, minor, rest = tm.groups()
            unit: dict[str, Any] = {"title": rest.strip() or text.strip(), "source_anchor": f"{b.anchor}#{b.fp}",
                                    "questions": [], "instructions": []}
            if minor is None:
                task = {"id": _unique_id(f"T{number}", used, text, insp.warnings), "number": int(number), **unit,
                        "parts": []}
                insp.tasks.append(task)
                continue
            uid = _unique_id(f"T{number}-{word[0].upper()}{minor}", used, text, insp.warnings)
            if task is None:
                insp.warnings.append(f"'{normalize(text)[:60]}' comes before any task heading; it was made task {uid}.")
                task = {"id": uid, "number": int(number), **unit, "parts": []}
                insp.tasks.append(task)
                continue
            if int(number) != task["number"]:
                insp.warnings.append(f"'{normalize(text)[:60]}' sits under {task['id']} but is numbered for task "
                                     f"{number}; check the manual's numbering.")
            task["parts"].append({"id": uid, "kind": word.lower(), "number": f"{number}.{minor}", **unit})
            continue
        pm = PART_RE.match(text)
        qm: re.Match[str] | None = (
            QUESTION_PART_RE.match(text) if b.heading_level is not None else None  # "Question 2: ..." headings
        )
        unit_match = pm if (pm and is_headingish) else qm
        if unit_match is not None and task is not None:
            label = unit_match.group(1).upper()
            rest = unit_match.group(2).strip()
            task["parts"].append(
                {
                    "id": _unique_id(f"{task['id']}.{label}", used, text, insp.warnings),
                    "kind": "question" if unit_match is qm else "part",
                    "title": rest or (f"Question {label}" if unit_match is qm else f"Part {label}"),
                    "source_anchor": f"{b.anchor}#{b.fp}",
                    "questions": [],
                    "instructions": [],
                }
            )
            continue
        labelled = bool(QUESTION_LABEL_RE.match(text))
        if task is None or not text or (b.is_code and not labelled):  # a "Q1." label wins over a code font
            continue
        target = task["parts"][-1] if task["parts"] else task
        if labelled or _is_question(text):
            qid = f"{target['id']}.Q{len(target['questions']) + 1}"
            q = {"id": qid, "text": text, "anchor": f"{b.anchor}#{b.fp}", "task_id": target["id"]}
            target["questions"].append(q)
            insp.questions.append(q)
        elif len(target["instructions"]) < 25:
            target["instructions"].append(text)

    # ---- solution boxes: tables whose label/placeholder mentions a solution
    tables = [b for b in insp.blocks if b.kind == "table"]
    for t in tables:
        cells = [b for b in insp.blocks if b.kind == "cell" and _is_direct_cell(t.anchor, b.anchor)]
        if not cells or not SOLUTION_RE.search(t.text):
            continue
        label_cell = cells[0]
        lm = SOLUTION_LABEL_RE.match(label_cell.text)
        fill = cells[1] if (lm and len(cells) > 1) else label_cell
        task_hint = None
        preceding = [x for x in insp.tasks if _anchor_before(x["source_anchor"].split("#")[0], t.anchor)]
        # "Task 2 Solution", or a "Solution" label over a "Task 2 – Code, Output ..." cell, names its task
        named = (lm.group(1) or _leading_task_number(fill.text)) if lm else None
        if named:
            task_hint = f"T{named}"
        elif preceding:
            task_hint = preceding[-1]["id"]
        # boxes naming a task cover the whole task; generic boxes ("Your answer:") belong to the unit above
        scope = "task" if named else "unit"
        unit_hint = task_hint
        if preceding:
            parts_before = [p for p in preceding[-1]["parts"]
                            if _anchor_before(p["source_anchor"].split("#")[0], t.anchor)]
            if parts_before and preceding[-1]["id"] == task_hint:
                unit_hint = parts_before[-1]["id"]
        insp.solution_boxes.append(
            {
                "table_anchor": t.anchor,
                "label": normalize(label_cell.text)[:80],
                "fill_anchor": f"{fill.anchor}#{fill.fp}",
                "placeholder": normalize(fill.text)[:200],
                "task_hint": task_hint,
                "unit_hint": unit_hint,
                "scope": scope,
                "shape": t.table_shape,
            }
        )
    for hint in {s["unit_hint"] if s["scope"] == "unit" else s["task_hint"] for s in insp.solution_boxes}:
        same = [s for s in insp.solution_boxes if (s["unit_hint"] if s["scope"] == "unit" else s["task_hint"]) == hint]
        if hint and len(same) > 1:
            insp.warnings.append(f"More than one solution box maps to {hint}; review the plan anchors.")
    # fill-in tables (e.g. counters to copy): empty cells next to filled header cells
    for t in tables:
        if any(t.anchor == s["table_anchor"] for s in insp.solution_boxes):
            continue
        cells = [b for b in insp.blocks if b.kind == "cell" and _is_direct_cell(t.anchor, b.anchor)]
        empty = [c for c in cells if not c.text.strip()]
        if empty and len(empty) < len(cells):
            insp.fill_in_tables.append({
                "table_anchor": t.anchor,
                "text": t.text[:200],
                "empty_cells": [f"{c.anchor}#{c.fp}" for c in empty],
                "cells": [{"anchor": f"{c.anchor}#{c.fp}", "text": c.text[:80]} for c in cells],
            })
    if insp.tasks and not insp.solution_boxes:
        insp.warnings.append(
            "No solution boxes detected; answers will be inserted after each task (placement=after_source)."
        )
    for task_ in insp.tasks:
        if not task_["parts"] and not task_["instructions"]:
            insp.warnings.append(f"{task_['id']} has no readable instructions; check images or layout.")

    # things the introduction promises (teardown, deliverables, ...) that no task covers
    first_task = insp.tasks[0]["source_anchor"].split("#")[0] if insp.tasks else None
    intro = " ".join(b.text for b in paras if first_task is None or _anchor_before(b.anchor, first_task)).lower()
    covered = " ".join(
        " ".join([t["title"], *t["instructions"], *(p["title"] for p in t["parts"]),
                  *(i for p in t["parts"] for i in p["instructions"])])
        for t in insp.tasks
    ).lower()
    for label, promise_re in PROMISED_SECTIONS.items():
        if re.search(promise_re, intro) and not re.search(promise_re, covered):
            insp.uncovered_mentions.append(label)
            insp.warnings.append(
                f"The introduction mentions '{label}' but no task covers it. The filled manual itself is normally "
                "the deliverable: add a section only if the manual names extra content to hand in, written as "
                "the student would write it (no notes about the tool or blank fields)."
                if label in SOFT_PROMISES else
                f"The introduction mentions '{label}' but no task covers it. If the lab expects it, add a closing "
                "task with placement 'end' (it may live in an earlier lab's manual)."
            )


def _unique_id(base: str, used: set[str], text: str, warnings: list[str]) -> str:
    """Never hand out the same id twice: a repeated heading gets a '-dupN' id and a warning."""
    uid, n = base, 1
    while uid in used:
        n += 1
        uid = f"{base}-dup{n}"
    if uid != base:
        warnings.append(f"'{normalize(text)[:60]}' repeats {base}; it was given id {uid}. Check the manual's "
                        "numbering and merge or rename it in the plan.")
    used.add(uid)
    return uid


def _leading_task_number(text: str) -> str | None:
    m = re.match(r"\s*Task\s*(\d+)\b(?!\.\d)", text, re.I)
    return m.group(1) if m else None


_DB_CREATES = re.compile(r"\bCREATE\s+TABLE\b|\.to_sql\s*\(", re.I)
_WRITE_MODE = r"\s*,\s*(?:mode\s*=\s*)?[\"'][wax]"


def _file_role(name: str, text: str) -> str:
    """'output' when the manual's code creates the file before anything reads it, else 'input' (must be supplied).

    shortcut: only literal names inside the call count (not `path = "x.csv"; df.to_csv(path)`); the agent
    reviews required_files against the manual anyway.
    """
    lit = r"\s*\(\s*[rRfFbB]?[\"'](?:[^\"'\n]*[/\\])?" + re.escape(name) + r"[\"']"
    writes = [m.start() for m in re.finditer(r"(?:\.to_[a-z]+|savefig|\.save|\.write\.\w+)" + lit, text)]
    writes += [m.start() for m in re.finditer(r"\bopen" + lit + _WRITE_MODE, text)]
    reads = [m.start() for m in re.finditer(r"(?:read_[a-z]+|\bload\w*|imread|\.read\.\w+)" + lit, text)]
    reads += [m.start() for m in re.finditer(r"\bopen" + lit + f"(?!{_WRITE_MODE})", text)]
    # sqlite3.connect() creates a missing database; a lab that creates its own tables owns the file
    connects = [m.start() for m in re.finditer(r"sqlite3\.connect" + lit, text)]
    (writes if _DB_CREATES.search(text) else reads).extend(connects)
    return "output" if writes and (not reads or min(writes) < min(reads)) else "input"


def _is_question(text: str) -> bool:
    t = text.strip()
    if t.endswith("?"):
        return True
    if len(t) >= 400 or "?" not in t:
        return False
    # "3. Are the files sorted together? Explain." / "(b) Why ...? Discuss."
    numbered = re.match(r"^\s*(\d+[.)]|\(?[a-z]\))\s+", t)
    return bool(QUESTION_START_RE.match(t) or numbered)


def _anchor_key(anchor: str) -> list[int]:
    return [int(x[1:]) for x in anchor.split("/")]


def _anchor_before(a: str, b: str) -> bool:
    return _anchor_key(a) < _anchor_key(b)


def _adjacent(a: str, b: str) -> bool:
    pa, pb = a.rsplit("/", 1) if "/" in a else ("", a), b.rsplit("/", 1) if "/" in b else ("", b)
    return pa[0] == pb[0] and int(pb[1][1:]) == int(pa[1][1:]) + 1


def _is_direct_cell(table_anchor: str, cell_anchor: str) -> bool:
    return bool(re.fullmatch(re.escape(table_anchor) + r"/r\d+/c\d+", cell_anchor))


# ---------------------------------------------------------------- draft plan


def draft_plan(insp: Inspection, required_files: list[dict[str, Any]]) -> dict[str, Any]:
    """A starting point for the agent's plan. The agent must review it against the manual."""
    tasks: list[dict[str, Any]] = []
    box_by_task: dict[str, str] = {}
    box_by_unit: dict[str, str] = {}
    for box in insp.solution_boxes:
        if box.get("scope") == "unit" and box.get("unit_hint"):
            box_by_unit.setdefault(box["unit_hint"], box["fill_anchor"])
        elif box["task_hint"]:
            box_by_task.setdefault(box["task_hint"], box["fill_anchor"])
    # a SETUP task only when the manual has its own setup section (its solution goes right after that section);
    # otherwise imports and installs belong in the first task's first cell, not in an invented appendix
    first_task = insp.tasks[0]["source_anchor"].split("#")[0] if insp.tasks else None
    setup = next((b for b in insp.blocks if b.kind == "paragraph" and b.depth == 0
                  and (b.heading_level or b.bold) and len(b.text) <= 60 and SETUP_RE.search(b.text)
                  and (first_task is None or _anchor_before(b.anchor, first_task))), None)
    prev: str | None = None
    if setup is not None:
        prev = "SETUP"
        tasks.append(
            {
                "id": "SETUP",
                "title": normalize(setup.text).rstrip(":"),
                "source_anchor": f"{setup.anchor}#{setup.fp}",
                "placement": "after_source",
                "depends_on": [],
                "instructions": ["Do what the manual's setup section asks (installs, imports, loading the files)."],
                "observation_questions": [],
                "required_evidence": ["code", "output"],
                "cell_ids": [],
            }
        )
    for t in insp.tasks:
        units = t["parts"] or [t]
        for u in units:
            anchor = box_by_unit.get(u["id"]) or box_by_task.get(t["id"])
            tasks.append(
                {
                    "id": u["id"],
                    "title": u["title"],
                    "parent": t["id"] if t["parts"] else None,
                    "source_anchor": u["source_anchor"],
                    "solution_anchor": anchor,
                    "placement": "anchor" if anchor else "after_source",
                    "depends_on": [prev] if prev else [],
                    "instructions": u["instructions"],
                    "observation_questions": [
                        {"id": q["id"], "text": q["text"], "kind": "observed"} for q in u["questions"]
                    ],
                    "required_evidence": ["code", "output"],
                    "cell_ids": [],
                }
            )
            prev = u["id"]
    title = insp.title_candidates[0] if insp.title_candidates else Path(insp.source_name).stem
    return {
        "schema_version": 1,
        "lab_title": title,
        "source_sha256": insp.source_sha256,
        "profile": "python",
        "requires_native_colab_evidence": insp.requires_native_colab_evidence,
        "required_files": required_files,
        "tasks": tasks,
        "notes": ["DRAFT generated by `labpilot inspect`: review every task, question and anchor."],
    }


COLAB_FEATURES = [
    (re.compile(r"files\.upload\s*\("), "interactive-upload",
     "files.upload(): LabPilot stages the files non-interactively; load them by name (e.g. 'data.csv') instead."),
    (re.compile(r"/content/"), "content-paths",
     "/content/ paths: Colab's working directory; use plain relative file names so the code runs anywhere."),
    (re.compile(r"^\s*!\s*(pip|apt|apt-get)\b", re.M), "setup-commands",
     "!pip / !apt setup lines: keep them as the first setup cell when running in Colab."),
    (re.compile(r"drive\.mount\s*\("), "drive-mount",
     "drive.mount(): LabPilot never mounts Google Drive; the files are staged instead."),
]


COLAB_SIGNAL = re.compile(r"\bcolab\b|google\.colab|/content/", re.I)
PYTHON_SIGNAL = re.compile(r"\bpython\b|^\s*(?:import|from)\s+\w+|\bpandas\b|pyspark|\.ipynb\b|jupyter", re.I | re.M)
COLAB_BLOCKERS = [
    (re.compile(r"\bdocker\b", re.I), "Docker containers"),
    (re.compile(r"\btmux\b|\bterminals?\s+[A-D]\b", re.I), "several live terminal sessions (tmux)"),
    (re.compile(r"killercoda|katacoda|play-with-docker", re.I), "a browser Linux playground such as Killercoda"),
    (re.compile(r"\baws\b|\bec2\b|amazon web services|\bazure\b|google cloud console", re.I),
     "a cloud provider console (AWS, Azure or GCP)"),
    (re.compile(r"public\s+static\s+void\s+main|\bjavac\b", re.I), "Java programs"),
    (re.compile(r"\beclipse\b|intellij|netbeans|android studio", re.I), "a desktop IDE"),
    (re.compile(r"virtualbox|vmware|\bssh\b", re.I), "a virtual machine or SSH access"),
]
# local software a lab needs even when it also names Colab (Lab 1: Colab for Python, SQL Server for the SQL task);
# sqlite3 and pandas SQL run in Colab and are not matched
COLAB_HARD_BLOCKERS = [
    (re.compile(r"\bSSMS\b|SQL\s+Server\s+(?:Management\s+Studio|Express|Developer)|Microsoft\s+SQL\s+Server|"
                r"\bMSSQL\b|\bsqlcmd\b", re.I), "Microsoft SQL Server and SSMS"),
]
# interactive Colab widgets the CLI cannot click; they do not block the lab
COLAB_ATTENTION = [
    (re.compile(r"files\.upload\s*\("),
     "files.upload() opens a browser file picker the CLI cannot click: upload the lab's files to the runtime "
     "directly instead and load them by name."),
    (re.compile(r"drive\.mount\s*\("),
     "drive.mount() needs an interactive Google sign-in the CLI cannot click: upload the files to the runtime "
     "directly instead of reading them from Drive."),
    (re.compile(r"\b(?:GPU|CUDA)\b|\.cuda\(|['\"]cuda['\"]|change runtime type", re.IGNORECASE),
     "The lab uses a GPU: ask the user once (it uses their Colab GPU quota), then add --gpu T4 to `labpilot finish`."),
]


def colab_fit(text: str) -> dict[str, Any]:
    """Can this lab be done in Google Colab? Deterministic first answer; the agent confirms it from the manual.

    A manual written for Colab wins over incidental mentions (a Hadoop lab that skips an "AWS bundle").
    """
    hard = [label for pattern, label in COLAB_HARD_BLOCKERS if pattern.search(text)]
    blockers = hard + [label for pattern, label in COLAB_BLOCKERS if pattern.search(text)]
    needs_attention = [note for pattern, note in COLAB_ATTENTION if pattern.search(text)]
    if hard:
        ok, reason = False, f"It needs {' and '.join(hard)}, which Google Colab cannot provide."
    elif COLAB_SIGNAL.search(text):
        ok, reason = True, "The manual is written for Google Colab."
    elif blockers:
        needs = blockers[0] if len(blockers) == 1 else ", ".join(blockers[:-1]) + " and " + blockers[-1]
        ok, reason = False, f"It needs {needs}, which Google Colab cannot provide."
    elif len(PYTHON_SIGNAL.findall(text)) >= 3:
        ok, reason = True, "A Python lab: it runs in Google Colab even though the manual does not name it."
    else:
        ok, reason = False, "It is not a Python notebook lab, so it cannot be done in Google Colab."
    return {"colab_ok": ok, "reason": reason, "blockers": blockers, "needs_attention": needs_attention}


def colab_features(insp: Inspection) -> list[dict[str, Any]]:
    """Colab-specific constructs in the manual's starter code that the agent must adapt."""
    found = []
    for pattern, key, advice in COLAB_FEATURES:
        anchors = [c["start"] for c in insp.code_blocks if pattern.search(c["code"])]
        if anchors:
            found.append({"feature": key, "anchors": anchors, "advice": advice})
    return found
