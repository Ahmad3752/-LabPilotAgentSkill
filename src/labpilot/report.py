"""Completion contract, export of final artifacts, validation, status and resume."""

from __future__ import annotations

import platform
import shutil
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from labpilot import SCHEMA_VERSION, __version__
from labpilot.documents.validate import render_pages, validate_docx
from labpilot.documents.write import TaskContent, write_document
from labpilot.errors import ErrorCode, LabPilotError
from labpilot.evidence.manifest import load_evidence
from labpilot.evidence.validate import validate_evidence
from labpilot.models import AnswersFile, ItemStatus, Plan, RunState, utcnow
from labpilot.orchestration import (
    _source_copy,
    capture,
    check_inputs,
    execute,
    load_plan,
    preflight,
    reset_for_changed_inputs,
    validate_answers,
)
from labpilot.security import redact, redact_obj
from labpilot.workspace import (
    Run,
    atomic_write_json,
    atomic_write_text,
    read_json,
    rmtree_owned,
    safe_stem,
    sha256_file,
)


def _pkg(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _answers(run: Run) -> AnswersFile | None:
    p = run.path("answers.json")
    return AnswersFile.model_validate(read_json(p)) if p.exists() else None


# ------------------------------------------------------------------ completion contract


def completion_checks(run: Run, plan: Plan) -> dict[str, dict[str, Any]]:
    m = run.manifest
    checks: dict[str, dict[str, Any]] = {}
    changes = check_inputs(run)
    checks["inputs_unchanged"] = {"ok": not changes, "detail": changes}
    missing = [d.expected_name for d in m.datasets
               if d.status not in ("found", "mapped")
               and next((r.required for r in plan.required_files if r.name == d.expected_name), True)]
    checks["datasets_present"] = {"ok": not missing, "detail": missing}
    not_passed = {t.id: str(m.task_status.get(t.id, ItemStatus.PENDING)) for t in plan.tasks
                  if m.task_status.get(t.id) != ItemStatus.PASSED}
    checks["all_tasks_executed"] = {"ok": not not_passed, "detail": not_passed}
    evidence = load_evidence(run.dir)
    ev_problems = validate_evidence(run.dir, evidence, m) if evidence else ["no evidence captured"]
    em = run.dir / "evidence" / "evidence_manifest.json"
    capture_problems = read_json(em).get("capture_problems", []) if em.exists() else []
    need = []
    for t in plan.tasks:
        if "output" in t.required_evidence or "code" in t.required_evidence:
            if not any(e.task_id == t.id and e.valid for e in evidence):
                need.append(t.id)
    checks["evidence_valid"] = {"ok": not ev_problems and not capture_problems and not need,
                                "detail": {"problems": ev_problems, "capture_problems": capture_problems,
                                           "tasks_without_evidence": need}}
    if plan.requires_native_colab_evidence:
        native = any(e.capture_mode == "native-colab-ui" for e in evidence)
        checks["native_colab_evidence"] = {"ok": native, "detail": "the manual requires screenshots from Google Colab; "
                                           "only labelled local renderings are available" if not native else "ok"}
    answers = _answers(run)
    if answers is None:
        checks["answers_valid"] = {"ok": not any(t.observation_questions for t in plan.tasks), "detail": "no answers.json"}
    else:
        probs = validate_answers(run, plan, answers)
        nv = [a.question_id for a in answers.answers if a.verification == "not_verified"]
        nv += [f"table:{f.anchor.split('#')[0]}" for f in answers.table_cells if f.verification == "not_verified"]
        checks["answers_valid"] = {"ok": not probs and not nv, "detail": {"problems": probs, "not_verified": nv}}
    return checks


# ------------------------------------------------------------------ export


def export(run: Run, render: bool = True) -> dict[str, Any]:
    plan = load_plan(run)
    m = run.manifest
    if not run.executed_notebook.exists():
        raise LabPilotError(ErrorCode.INVALID_STATE, "Nothing executed yet; nothing to export.")
    checks = completion_checks(run, plan)
    pre_ok = all(c["ok"] for c in checks.values())
    stem = safe_stem(m.source_name)
    out_dir = run.lab_dir / "output" / f"{stem}-{run.id}"
    if out_dir.exists():  # owned by this run; regenerated from the pristine original every time
        rmtree_owned(out_dir)
    out_dir.mkdir(parents=True)
    evidence = load_evidence(run.dir)
    current_ev = [e for e in evidence if e.attempt_id == m.current_attempt and e.valid]
    answers = _answers(run)
    ans_by_q = {a.question_id: a for a in (answers.answers if answers else [])}
    summaries = {s.task_id: s.explanation for s in (answers.task_summaries if answers else [])}
    contents = []
    for t in plan.tasks:
        status = str(m.task_status.get(t.id, ItemStatus.PENDING))
        evs = [e for e in current_ev if e.task_id == t.id] if status == "passed" else []
        contents.append(TaskContent(task=t, explanation=summaries.get(t.id), evidence=evs,
                                    answers=[(q.text, ans_by_q.get(q.id)) for q in t.observation_questions],
                                    status=status))
    doc_info = read_json(run.path("document.json")) if run.path("document.json").exists() else {}
    from labpilot.orchestration import _provenance

    docx_name = f"{stem}_{'Completed' if pre_ok else 'Partial'}.docx"
    out_docx = out_dir / docx_name
    report = write_document(source=_source_copy(run), out_path=out_docx, run_dir=run.dir, plan=plan, contents=contents,
                            provenance=_provenance(run), student=m.student,
                            student_fields=doc_info.get("student_fields", []),
                            table_fills=answers.table_cells if answers else [])
    structural = validate_docx(source=run.lab_dir / m.source_name, source_sha256=m.source_sha256, output=out_docx,
                               expected_images=report.inserted_images, removed_placeholders=report.removed_placeholders)
    checks["document_structure"] = {"ok": structural["ok"], "detail": structural}
    checks["placements_reliable"] = {"ok": not report.review_required, "detail": report.review_required}
    complete = all(c["ok"] for c in checks.values())
    if not complete and "Completed" in out_docx.name:
        partial = out_docx.with_name(f"{stem}_Partial.docx")
        out_docx.rename(partial)
        out_docx = partial
    rendered = render_pages(out_docx, run.dir / "render") if render else {"available": False, "reason": "skipped"}

    nb_out = out_dir / f"{stem}.ipynb"
    shutil.copy2(run.executed_notebook, nb_out)
    shots = out_dir / "screenshots"
    for e in current_ev:
        dest = shots / Path(e.path).relative_to("evidence/screenshots")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run.dir / e.path, dest)
    ev_export = [e.model_copy(update={"path": "screenshots/" + Path(e.path).relative_to("evidence/screenshots").as_posix()})
                 for e in current_ev]
    atomic_write_json(out_dir / "evidence_manifest.json",
                      {"schema_version": SCHEMA_VERSION, "run_id": run.id, "attempt_id": m.current_attempt,
                       "capture_mode": "rendered-local-notebook",
                       "note": "Screenshots are local renderings of the executed notebook; `backend` names where the "
                               "code actually ran.", "records": [e.model_dump(mode="json") for e in ev_export]})
    status = RunState.COMPLETED if complete else RunState.PARTIAL
    m.outputs = {"dir": str(out_dir), "docx": out_docx.name, "notebook": nb_out.name,
                 "report": "execution_report.json", "review": "REVIEW.md", "evidence": "evidence_manifest.json"}
    m.validation = {"checks": checks, "render": rendered, "write": report.__dict__, "status": str(status),
                    "validated_at": utcnow()}
    run.save()
    run.set_state(RunState.DOCUMENT_WRITTEN)
    run.set_state(RunState.VALIDATED)
    run.set_state(status)
    exec_report = build_execution_report(run, plan, checks, rendered, report.__dict__, status)
    atomic_write_json(out_dir / "execution_report.json", exec_report)
    atomic_write_text(out_dir / "REVIEW.md", build_review(run, plan, checks, rendered, report.__dict__, status, answers))
    return {"run_id": run.id, "status": str(status), "output_dir": str(out_dir), "docx": out_docx.name,
            "notebook": nb_out.name, "screenshots": len(current_ev),
            "failed_checks": {k: v["detail"] for k, v in checks.items() if not v["ok"]},
            "render_validation": rendered}


def validate_run(run: Run) -> dict[str, Any]:
    plan = load_plan(run)
    m = run.manifest
    checks = completion_checks(run, plan)
    out = m.outputs
    if out.get("docx") and Path(out["dir"], out["docx"]).exists():
        w = m.validation.get("write", {})
        structural = validate_docx(source=run.lab_dir / m.source_name, source_sha256=m.source_sha256,
                                   output=Path(out["dir"], out["docx"]), expected_images=w.get("inserted_images", []),
                                   removed_placeholders=w.get("removed_placeholders", []))
        checks["document_structure"] = {"ok": structural["ok"], "detail": structural}
        if "Completed" in out["docx"] and not all(c["ok"] for c in checks.values()):
            checks["output_name"] = {"ok": False, "detail": "output is named Completed but checks now fail; re-export"}
    else:
        checks["document_structure"] = {"ok": False, "detail": "not exported yet"}
    ok = all(c["ok"] for c in checks.values())
    return {"run_id": run.id, "ok": ok, "status": "COMPLETED" if ok else "PARTIAL", "checks": redact_obj(checks)}


# ------------------------------------------------------------------ reports


def build_execution_report(run: Run, plan: Plan, checks: dict[str, Any], rendered: dict[str, Any],
                           write: dict[str, Any], status: RunState) -> dict[str, Any]:
    m = run.manifest
    records_path = run.exec_dir / "current_records.json"
    records = read_json(records_path) if records_path.exists() else []
    cells = []
    for r in records:
        cells.append({k: r.get(k) for k in ("cell_id", "task_id", "status", "execution_count", "started_at", "ended_at",
                                            "code_sha256", "error_code", "error_type", "error_message", "output_truncated")}
                     | {"repairs": m.cell_repairs.get(r["cell_id"], 0)})
    return redact_obj({
        "schema_version": SCHEMA_VERSION,
        "run_id": run.id,
        "generated_at": utcnow(),
        "status": str(status),
        "synthetic_demo": m.synthetic,
        "lab_title": plan.lab_title,
        "source": {"name": Path(m.source_name).name, "sha256": m.source_sha256,
                   "unchanged": sha256_file(run.lab_dir / m.source_name) == m.source_sha256},
        "datasets": [d.model_dump(include={"expected_name", "role", "local_path", "status", "sha256", "size_bytes",
                                           "origin", "schema_summary", "problems", "cloud_upload_consent",
                                           "staged_path", "used_by_tasks"}) for d in m.datasets],
        "backend": {"name": m.backend,
                    "executed_on": m.attempts[-1]["backend"] if m.attempts else None,
                    "capture_mode": "rendered-local-notebook", "runtime": m.runtime_identity},
        "versions": {"labpilot": __version__, "host_python": platform.python_version(), "os": platform.platform(),
                     "nbclient": _pkg("nbclient"), "nbconvert": _pkg("nbconvert"), "playwright": _pkg("playwright"),
                     "python-docx": _pkg("python-docx")},
        "tasks": [{"id": t.id, "title": t.title, "status": str(m.task_status.get(t.id, "pending")), "cells": t.cell_ids,
                   "questions": [q.id for q in t.observation_questions]} for t in plan.tasks],
        "cells": cells,
        "attempts": m.attempts,
        "retries": {"cell_repairs": m.cell_repairs, "repair_budget_per_cell": m.retry_budget_per_cell,
                    "connection_recoveries": m.connection_recoveries},
        "validation": {"checks": checks, "render": rendered, "layout_changes": write.get("layout_changes", []),
                       "placements": write.get("placements", []), "review_required": write.get("review_required", [])},
        "blockers": [b.model_dump() for b in m.blockers],
        "security_events": m.security_events,
        "outputs": m.outputs,
    })


def build_review(run: Run, plan: Plan, checks: dict[str, Any], rendered: dict[str, Any], write: dict[str, Any],
                 status: RunState, answers: AnswersFile | None) -> str:
    m = run.manifest
    executed_on = m.attempts[-1]["backend"] if m.attempts else "n/a"
    summaries = {s.task_id: s.explanation for s in (answers.task_summaries if answers else [])}
    lines = [f"# Review — {plan.lab_title}", ""]
    if m.synthetic:
        lines += ["> **SYNTHETIC DEMO RUN.** The data is explicitly synthetic. This does not complete the real assignment.", ""]
    lines += [
        f"- **Status:** {status}",
        f"- **Run:** `{run.id}` (attempt `{m.current_attempt}`)",
        f"- **Executed on:** {executed_on}",
        "- **Screenshots:** local renderings of the executed notebook (headless Chromium). "
        "They are not screenshots of the Google Colab web UI.",
        f"- **Word file:** `{m.outputs.get('docx')}` — a copy of the original manual; the original is unchanged.",
        "",
        "This document is a review-ready learning artifact. Read every solution, run the notebook yourself, and make sure "
        "you can explain it. Follow your course's rules on AI assistance; LabPilot never submits anything.",
        "",
        "## Tasks",
        "",
        "| Task | Status | What the solution does |",
        "|---|---|---|",
    ]
    for t in plan.tasks:
        lines.append(f"| {t.id} — {t.title} | {m.task_status.get(t.id, 'pending')} | "
                     f"{(summaries.get(t.id) or '').replace('|', '/')[:220]} |")
    lines += ["", "## Verification checks", ""]
    for name, c in checks.items():
        mark = "PASS" if c["ok"] else "FAIL"
        detail = "" if c["ok"] else f" — {redact(str(c['detail']))[:400]}"
        lines.append(f"- [{mark}] {name}{detail}")
    lines.append(f"- [{'PASS' if rendered.get('ok') else 'N/A' if not rendered.get('available') else 'FAIL'}] "
                 f"visual page render (optional): {rendered.get('reason') or rendered.get('pages', '')}")
    if write.get("layout_changes"):
        lines += ["", "## Layout adjustments made in the copy", ""] + [f"- {x}" for x in write["layout_changes"]]
    if write.get("review_required"):
        lines += ["", "## Needs your review", ""] + [f"- {x}" for x in write["review_required"]]
    if m.blockers:
        lines += ["", "## Outstanding blockers", ""]
        lines += [f"- **{b.code}** {b.message} → {b.next_action}" for b in m.blockers]
    if status != RunState.COMPLETED:
        lines += ["", "## How to continue", "",
                  f"Fix the items above (add missing files to the lab folder, etc.), then run `labpilot resume {run.id}` "
                  "from the lab folder, or ask your agent to continue the LabPilot run."]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ status / next action / resume


def next_action(run: Run) -> dict[str, str]:
    m = run.manifest
    if not run.path("plan.json").exists():
        return {"actor": "agent", "action": "plan",
                "detail": "Review document.json + draft_plan.json, write plan.json, run `labpilot plan`."}
    if not run.path("cells.json").exists():
        return {"actor": "agent", "action": "build-notebook", "detail": "Write cells.json and run `labpilot build-notebook`."}
    user_blockers = [b for b in m.blockers if b.requires_user]
    if m.state == RunState.WAITING_FOR_AUTH:
        return {"actor": "user", "action": "authenticate", "detail": "Complete the backend's official sign-in, then resume."}
    if not m.backend or m.state in (RunState.PLANNED, RunState.WAITING_FOR_INPUT, RunState.INSPECTED):
        if m.state == RunState.WAITING_FOR_INPUT and user_blockers and not m.backend:
            return {"actor": "labpilot", "action": "preflight",
                    "detail": "Run preflight; independent tasks can execute while inputs are missing."}
        if not m.backend:
            return {"actor": "labpilot", "action": "preflight", "detail": "labpilot preflight <run-id>"}
    if m.backend_state.get("pending_host_actions"):
        return {"actor": "agent", "action": "record-execution", "detail": "Follow host_actions.json via colab-mcp."}
    exhausted = [c for c, n in m.cell_repairs.items()
                 if n > m.retry_budget_per_cell and m.cell_status.get(c) != ItemStatus.PASSED]
    if exhausted:
        return {"actor": "user", "action": "review-failures",
                "detail": f"Repair budget used up for {exhausted}; export a partial result or raise --retry-budget."}
    failed = [c for c, s in m.cell_status.items() if s == ItemStatus.FAILED]
    if failed:
        return {"actor": "agent", "action": "repair",
                "detail": f"Fix failing cells {failed} using the tracebacks (labpilot status --json), rebuild, execute."}
    pending = [c for c, s in m.cell_status.items() if s in (ItemStatus.PENDING, ItemStatus.STALE)]
    if pending:
        return {"actor": "labpilot", "action": "execute", "detail": f"{len(pending)} cell(s) need (re)execution."}
    ev = load_evidence(run.dir)
    if not ev or any(e.attempt_id != m.current_attempt for e in ev):
        return {"actor": "labpilot", "action": "capture", "detail": "labpilot capture <run-id>"}
    em = run.dir / "evidence" / "evidence_manifest.json"
    if em.exists() and read_json(em).get("capture_problems"):
        return {"actor": "agent", "action": "repair-output",
                "detail": "Some cells produced no visible output; add displays, rebuild, execute, capture."}
    answers = _answers(run)
    if answers is None or m.backend_state.get("answers_attempt") != m.current_attempt:
        return {"actor": "agent", "action": "answers",
                "detail": "Write grounded answers.json from the real outputs and run `labpilot answers`."}
    if m.state not in (RunState.COMPLETED, RunState.PARTIAL) or not m.outputs:
        return {"actor": "labpilot", "action": "export", "detail": "labpilot export <run-id>"}
    if user_blockers:
        return {"actor": "user", "action": "resolve-blockers", "detail": "; ".join(b.next_action for b in user_blockers)}
    return {"actor": "none", "action": "done" if m.state == RunState.COMPLETED else "review",
            "detail": f"Open {m.outputs.get('dir')}"}


def status(run: Run) -> dict[str, Any]:
    m = run.manifest
    plan = load_plan(run) if run.path("plan.json").exists() else None
    counts: dict[str, int] = {}
    for s in m.cell_status.values():
        counts[str(s)] = counts.get(str(s), 0) + 1
    failures = []
    rp = run.exec_dir / "current_records.json"
    if rp.exists():
        for r in read_json(rp):
            if r["status"] == "failed":
                failures.append({"cell_id": r["cell_id"], "error_code": r.get("error_code"), "error_type": r.get("error_type"),
                                 "error_message": redact((r.get("error_message") or "")[:800]),
                                 "traceback_tail": redact((r.get("traceback") or "")[-2500:])})
    return {
        "run_id": run.id, "state": str(m.state), "lab_title": m.lab_title, "synthetic_demo": m.synthetic,
        "backend": m.backend, "profile": m.profile, "attempt": m.current_attempt,
        "executed_on": m.attempts[-1]["backend"] if m.attempts else None,
        "tasks": {t.id: str(m.task_status.get(t.id, "pending")) for t in plan.tasks} if plan else {},
        "cells": counts, "failures": failures,
        "datasets": [{"name": d.expected_name, "status": d.status} for d in m.datasets],
        "blockers": [b.model_dump() for b in m.blockers],
        "outputs": m.outputs, "next_action": next_action(run), "run_dir": str(run.dir),
    }


def resume(run: Run, backend: str | None = None, allow_cloud: bool = False, allow_upload: bool = False,
           max_steps: int = 8) -> dict[str, Any]:
    """Advance deterministic steps from the last consistent checkpoint until agent/user input is needed."""
    steps: list[dict[str, Any]] = []
    m = run.manifest
    if run.path("plan.json").exists():
        changes = check_inputs(run)
        blockers_before = [b.code for b in m.blockers if b.code.startswith("DATASET_")]
        if changes or blockers_before:
            steps.append({"step": "revalidate-inputs", "result": reset_for_changed_inputs(run)})
    if m.state in (RunState.CANCELLED, RunState.PAUSED, RunState.EXECUTING) and not m.backend_state.get("pending_host_actions"):
        for cid, s in list(m.cell_status.items()):
            if s != ItemStatus.FAILED:
                m.cell_status[cid] = ItemStatus.STALE if s == ItemStatus.PASSED else s
        run.save()
        steps.append({"step": "invalidate-kernel-state", "result": "fresh kernel: verified cells will be replayed"})
    for _ in range(max_steps):
        na = next_action(run)
        if na["actor"] != "labpilot":
            break
        if na["action"] == "preflight" or (na["action"] == "execute" and (backend or not m.backend
                                                                           or any(d.status in ('found', 'mapped') and not d.staged_path for d in m.datasets))):
            res = preflight(run, backend or m.backend or "auto", allow_cloud, allow_upload)
            steps.append({"step": "preflight", "ok": res["ok"], "backend": res["backend"]})
            if not res["ok"]:
                break
            backend = None
            continue
        if na["action"] == "execute":
            res = execute(run)
            steps.append({"step": "execute", "status": res.get("status"), "failed": res.get("failed")})
            if res.get("status") in ("FAILED_CELLS", "WAITING_FOR_HOST"):
                break
            continue
        if na["action"] == "capture":
            res = capture(run)
            steps.append({"step": "capture", "images": res["images"], "problems": res["problems"]})
            if res["problems"]:
                break
            continue
        if na["action"] == "export":
            res = export(run)
            steps.append({"step": "export", "status": res["status"], "output_dir": res["output_dir"]})
            break
    return {"run_id": run.id, "steps": steps, "state": str(run.manifest.state), "next_action": next_action(run)}
