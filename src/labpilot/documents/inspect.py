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
    element_text,
    fingerprint,
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
TASK_RE = re.compile(r"^\s*(?:Lab\s+)?(Task|Exercise|Activity|Problem)\s*[#:.\-]?\s*(\d+)\b\s*[:.\-–—]?\s*(.*)$", re.I)
PART_RE = re.compile(r"^\s*\(?Part\s+([A-Z])\)?\s*[:.\-–—]?\s*(.*)$", re.I)
SOLUTION_RE = re.compile(
    r"\b(solution|answer|your code|paste (your )?(code|screenshot|output)|screenshot(s)? here|write your)\b", re.I
)
SOLUTION_LABEL_RE = re.compile(r"^\s*(?:Task\s*(\d+)\s*)?(Solution|Answer)s?\b", re.I)
QUESTION_START_RE = re.compile(
    r"^\s*(Q\d*[:.)]|Question\b|Observation|Observe|Explain|Why\b|What\b|Which\b|How\b|Does\b|Do\b|Is\b|Are\b|Can\b|Compare\b|Describe\b|Identify\b)",
    re.I,
)
_EXT = r"(?:csv|tsv|json|jsonl|xlsx|xls|parquet|txt|png|jpe?g|xml|zip)"
FILE_RE = re.compile(
    r"[\"'“‘`]([^\"'”’`\n]{1,80}?\." + _EXT + r")[\"'”’`]"  # quoted names may contain spaces
    r"|(?<![\w\\-])([\w][\w\-.]{0,80}\." + _EXT + r")\b",
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
    file_references: list[dict[str, Any]] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    student_fields: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
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
                    fp=fingerprint(text),
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
                    fp=fingerprint(element_text(block)),
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
                            fp=fingerprint(ctext),
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
    insp.file_references = list(seen_files.values())
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
        if b.is_code and b.text:
            if current and not _adjacent(current[-1].anchor, b.anchor):
                flush()
            current.append(b)
        else:
            flush()
    flush()

    # ---- tasks and parts in reading order
    task: dict[str, Any] | None = None
    for b in paras:
        if b.depth > 1:  # skip text nested inside solution tables
            continue
        text = b.text
        is_headingish = b.heading_level is not None or b.bold or len(text) < 90
        m = TASK_RE.match(text)
        if m and is_headingish:
            if re.search(r"\b(solution|answer)s?\b", text, re.I):
                continue
            number = m.group(2)
            title = m.group(3).strip() or text.strip()
            task = {
                "id": f"T{number}",
                "number": int(number),
                "title": title,
                "source_anchor": f"{b.anchor}#{b.fp}",
                "parts": [],
                "questions": [],
                "instructions": [],
            }
            insp.tasks.append(task)
            continue
        pm = PART_RE.match(text)
        if pm and task is not None and is_headingish:
            letter = pm.group(1).upper()
            task["parts"].append(
                {
                    "id": f"{task['id']}.{letter}",
                    "title": pm.group(2).strip() or f"Part {letter}",
                    "source_anchor": f"{b.anchor}#{b.fp}",
                    "questions": [],
                    "instructions": [],
                }
            )
            continue
        if task is None or not text or b.is_code:
            continue
        target = task["parts"][-1] if task["parts"] else task
        if _is_question(text):
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
        if lm and lm.group(1):
            task_hint = f"T{lm.group(1)}"
        else:
            preceding = [x for x in insp.tasks if _anchor_before(x["source_anchor"].split("#")[0], t.anchor)]
            if preceding:
                task_hint = preceding[-1]["id"]
        insp.solution_boxes.append(
            {
                "table_anchor": t.anchor,
                "label": normalize(label_cell.text)[:80],
                "fill_anchor": f"{fill.anchor}#{fill.fp}",
                "placeholder": normalize(fill.text)[:200],
                "task_hint": task_hint,
                "shape": t.table_shape,
            }
        )
        if task_hint and sum(1 for s in insp.solution_boxes if s["task_hint"] == task_hint) > 1:
            insp.warnings.append(f"More than one solution box maps to {task_hint}; review the plan anchors.")
    if insp.tasks and not insp.solution_boxes:
        insp.warnings.append(
            "No solution boxes detected; answers will be inserted after each task (placement=after_source)."
        )
    for task_ in insp.tasks:
        if not task_["parts"] and not task_["instructions"]:
            insp.warnings.append(f"{task_['id']} has no readable instructions; check images or layout.")


def _is_question(text: str) -> bool:
    t = text.strip()
    return t.endswith("?") or (bool(QUESTION_START_RE.match(t)) and len(t) < 400 and "?" in t)


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
    box_by_task = {}
    for box in insp.solution_boxes:
        if box["task_hint"] and box["task_hint"] not in box_by_task:
            box_by_task[box["task_hint"]] = box["fill_anchor"]
    prev: str | None = "SETUP"
    tasks.append(
        {
            "id": "SETUP",
            "title": "Environment setup and data loading",
            "placement": "append",
            "depends_on": [],
            "instructions": ["Initialise the runtime, verify versions and confirm the staged input files."],
            "observation_questions": [],
            "required_evidence": ["code", "output"],
            "cell_ids": [],
        }
    )
    for t in insp.tasks:
        units = t["parts"] or [t]
        for u in units:
            anchor = box_by_task.get(t["id"])
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


def colab_features(insp: Inspection) -> list[dict[str, Any]]:
    """Colab-specific constructs in the manual's starter code that the agent must adapt."""
    found = []
    for pattern, key, advice in COLAB_FEATURES:
        anchors = [c["start"] for c in insp.code_blocks if pattern.search(c["code"])]
        if anchors:
            found.append({"feature": key, "anchors": anchors, "advice": advice})
    return found
