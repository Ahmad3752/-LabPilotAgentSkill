"""`labpilot evaluate`: deterministic quality review of a finished lab before it may be called Completed.

These checks catch what a lab engineer would mark down and what "all cells passed" hides:
answers that read like log output, error text inside "passed" outputs, unfilled tables,
answer boxes without evidence, promised sections that were skipped, and internal IDs left in
the document. The host agent's independent review (skill step 11) complements this; it is
not a substitute for the student reading and understanding the work.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import docx

from labpilot.documents.anchors import W_P, element_text
from labpilot.evidence.manifest import load_evidence
from labpilot.models import AnswersFile, ItemStatus, Plan
from labpilot.workspace import Run, atomic_write_json, read_json

LOG_LIKE = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\s*=\s*\S")  # e.g. "IDENTICAL_RESULTS = True"
ERROR_MARKERS = [
    "No such file or directory",
    "Traceback (most recent call last)",
    "JOB FAILED",
    "command not found",
    "cannot access",
    "Permission denied",
    "Exception in thread",
]
MIN_ANSWER_WORDS = 8


def _finding(check: str, severity: str, message: str, where: str = "") -> dict[str, str]:
    return {"check": check, "severity": severity, "message": message, "where": where}


def evaluate(run: Run, docx_path: Path | None = None) -> dict[str, Any]:
    plan = Plan.model_validate(read_json(run.path("plan.json")))
    m = run.manifest
    findings: list[dict[str, str]] = []
    answers_path = run.path("answers.json")
    answers = AnswersFile.model_validate(read_json(answers_path)) if answers_path.exists() else None
    questions = {q.id: q for t in plan.tasks for q in t.observation_questions}

    # 1. every question answered, in sentences, not log output
    by_q = {a.question_id: a for a in (answers.answers if answers else [])}
    for qid in questions:
        a = by_q.get(qid)
        if a is None:
            findings.append(_finding("answers_complete", "fail", "question has no answer", qid))
            continue
        hits = LOG_LIKE.findall(a.text)
        if hits:
            findings.append(_finding("answers_prose", "fail",
                                     f"answer reads like program output ({', '.join(sorted(set(hits)))[:80]}); "
                                     "explain it in plain sentences", qid))
        if len(a.text.split()) < MIN_ANSWER_WORDS:
            findings.append(_finding("answers_prose", "warn", "answer is very short; add a sentence of explanation", qid))
        if a.verification == "not_verified":
            findings.append(_finding("answers_verified", "fail", "answer is not verified by a real run", qid))

    # 2. "passed" cells whose output still contains error text
    rec_path = run.exec_dir / "current_records.json"
    for r in read_json(rec_path) if rec_path.exists() else []:
        if r.get("status") != "passed":
            continue
        text = (r.get("stdout") or "") + (r.get("stderr") or "")
        for out in r.get("outputs", []):
            data = out.get("data") or {}
            val = data.get("text/plain", "")
            text += "".join(val) if isinstance(val, list) else str(val)
        for marker in ERROR_MARKERS:
            if marker in text:
                findings.append(_finding("outputs_clean", "fail",
                                         f"output contains '{marker}' although the cell counted as passed", r["cell_id"]))
                break

    # 3. evidence for every answer box / task that needs it
    evidence = [e for e in load_evidence(run.dir) if e.attempt_id == m.current_attempt and e.valid]
    for t in plan.tasks:
        if t.required_evidence and not any(e.task_id == t.id for e in evidence):
            findings.append(_finding("evidence_present", "fail", "no screenshot evidence for this part", t.id))
        if m.task_status.get(t.id) != ItemStatus.PASSED:
            findings.append(_finding("tasks_passed", "fail", f"status is {m.task_status.get(t.id)}", t.id))

    # 4. fill-in tables (student identity tables are the student's own job)
    doc = read_json(run.path("document.json")) if run.path("document.json").exists() else {}
    filled = {f.anchor.split("#")[0] for f in (answers.table_cells if answers else [])}
    for table in doc.get("fill_in_tables", []):
        labels = " ".join(c["text"] for c in table["cells"]).lower()
        if re.search(r"student|name|cms|roll|registration", labels):
            if any(not c["text"].strip() for c in table["cells"]):
                findings.append(_finding("student_fields", "warn", "student name / ID cells are empty; fill them in "
                                         "yourself", table["table_anchor"]))
            continue
        missing = [c for c in table["empty_cells"] if c.split("#")[0] not in filled]
        if missing:
            findings.append(_finding("tables_filled", "fail", f"{len(missing)} table cell(s) left empty",
                                     table["table_anchor"]))

    # 5. sections the introduction promises
    covered = " ".join(t.title.lower() for t in plan.tasks)
    for label in doc.get("uncovered_mentions", []):
        if label not in covered and not (label == "cleanup" and "teardown" in covered):
            findings.append(_finding("sections_covered", "fail",
                                     f"the manual's introduction mentions '{label}' but no section covers it"))

    # 6. presentation: internal identifiers left in the document text
    if docx_path is not None and docx_path.exists():
        body = docx.Document(str(docx_path)).element.body
        text = "\n".join(element_text(p) for p in body.iter(W_P))
        if run.id in text or re.search(r"\b\d{8}-\d{6}-[0-9a-f]{6}\b", text):
            findings.append(_finding("presentation", "warn", "internal run identifiers appear in the document"))

    fails = [f for f in findings if f["severity"] == "fail"]
    result = {"run_id": run.id, "ok": not fails, "fails": len(fails),
              "warnings": len(findings) - len(fails), "findings": findings}
    atomic_write_json(run.path("evaluation.json"), result)
    return result
