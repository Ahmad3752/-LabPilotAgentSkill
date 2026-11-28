"""Completion contract, export of final artifacts, validation, status and resume."""

from __future__ import annotations

import platform
import re
import shutil
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from labpilot import SCHEMA_VERSION, __version__
from labpilot.documents.validate import render_pages, validate_docx
from labpilot.documents.write import TaskContent, write_document
from labpilot.errors import ErrorCode, ExitCode, LabPilotError
from labpilot.evaluate import evaluate
from labpilot.evidence.manifest import load_evidence
from labpilot.evidence.validate import validate_evidence
from labpilot.models import AnswersFile, ItemStatus, Plan, RunState, utcnow
from labpilot.orchestration import (
    _source_copy,
    build,
    capture,
    check_inputs,
    execute,
    load_plan,
    preflight,
    reset_for_changed_inputs,
    save_answers,
    set_gpu,
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
        needs_evidence = "output" in t.required_evidence or "code" in t.required_evidence
        if needs_evidence and not any(e.task_id == t.id and e.valid for e in evidence):
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
    docx_name = f"{stem}_{'Completed' if pre_ok else 'Partial'}.docx"
    out_docx = out_dir / docx_name
    report = write_document(source=_source_copy(run), out_path=out_docx, run_dir=run.dir, plan=plan, contents=contents,
                            student=m.student,
                            student_fields=doc_info.get("student_fields", []),
                            table_fills=answers.table_cells if answers else [])
    structural = validate_docx(source=run.lab_dir / m.source_name, source_sha256=m.source_sha256, output=out_docx,
                               expected_images=report.inserted_images, removed_placeholders=report.removed_placeholders)
    checks["document_structure"] = {"ok": structural["ok"], "detail": structural}
    checks["placements_reliable"] = {"ok": not report.review_required, "detail": report.review_required}
    quality = evaluate(run, out_docx)
    checks["quality_review"] = {"ok": quality["ok"],
                                "detail": [f for f in quality["findings"] if f["severity"] == "fail"]}
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
            "pages_dir": rendered.get("pages_dir"), "render_validation": rendered}


def validate_run(run: Run) -> dict[str, Any]:
    plan = load_plan(run)
    m = run.manifest
    if m.backend_state.get("cleaned_at"):  # working files removed after completion: report the final result
        checks = m.validation.get("checks", {})
        return {"run_id": run.id, "ok": all(c["ok"] for c in checks.values()), "status": str(m.state),
                "cleaned": True, "checks": redact_obj(checks), "output_dir": m.outputs.get("dir")}
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
    if m.backend_state.get("runtime"):
        executed_on += f" — {m.backend_state['runtime']}"
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
    ev = read_json(run.path("evaluation.json")) if run.path("evaluation.json").exists() else None
    if ev and ev["findings"]:
        lines += ["", "## Quality review findings", ""]
        lines += [f"- [{f['severity'].upper()}] {f['where'] + ': ' if f['where'] else ''}{f['message']}"
                  for f in ev["findings"]]
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
    if m.backend_state.get("cleaned_at"):
        return {"actor": "none", "action": "done", "detail": f"Completed and cleaned. Open {m.outputs.get('dir')}"}
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
    if m.state == RunState.COMPLETED:
        return {"actor": "agent", "action": "clean",
                "detail": "After the review, `labpilot clean <run-id>` frees the workspace and keeps the deliverables."}
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
        "input_provenance": m.input_provenance,
        "backend": m.backend, "profile": m.profile, "attempt": m.current_attempt,
        "executed_on": m.attempts[-1]["backend"] if m.attempts else None,
        "tasks": {t.id: str(m.task_status.get(t.id, "pending")) for t in plan.tasks} if plan else {},
        "cells": counts, "failures": failures,
        "datasets": [{"name": d.expected_name, "status": d.status} for d in m.datasets],
        "blockers": [b.model_dump() for b in m.blockers],
        "outputs": m.outputs, "next_action": next_action(run), "run_dir": str(run.dir),
    }


def _recover(run: Run) -> list[dict[str, Any]]:
    """Re-validate changed inputs and drop kernel state that an interrupted attempt left behind."""
    steps: list[dict[str, Any]] = []
    m = run.manifest
    if run.path("plan.json").exists():
        changes = check_inputs(run)
        blockers_before = [b.code for b in m.blockers if b.code.startswith("DATASET_")]
        if changes or blockers_before:
            steps.append({"step": "revalidate-inputs", "result": reset_for_changed_inputs(run)})
    interrupted = m.state in (RunState.CANCELLED, RunState.PAUSED) or (
        m.state == RunState.EXECUTING and "executing_attempt" in m.backend_state)  # a completed execute clears it
    if interrupted and not m.backend_state.get("pending_host_actions"):
        for cid, s in list(m.cell_status.items()):
            if s != ItemStatus.FAILED:
                m.cell_status[cid] = ItemStatus.STALE if s == ItemStatus.PASSED else s
        run.save()
        steps.append({"step": "invalidate-kernel-state", "result": "fresh kernel: verified cells will be replayed"})
    return steps


def resume(run: Run, backend: str | None = None, allow_cloud: bool = False, allow_upload: bool = False,
           max_steps: int = 8, gpu: str | None = None, allow_gpu: bool = False) -> dict[str, Any]:
    """Advance deterministic steps from the last consistent checkpoint until agent/user input is needed."""
    steps = _recover(run)
    m = run.manifest
    for _ in range(max_steps):
        na = next_action(run)
        if na["actor"] != "labpilot":
            break
        if na["action"] == "preflight" or (na["action"] == "execute" and (backend or not m.backend
                                                                           or any(d.status in ('found', 'mapped') and not d.staged_path for d in m.datasets))):
            res = preflight(run, backend or m.backend or "auto", allow_cloud, allow_upload, gpu, allow_gpu)
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


# ------------------------------------------------------------------ finish: the mechanical chain in one command

# failures worth simply re-running; any other failed cell needs the agent to change its code first
TRANSIENT = {str(c) for c in (ErrorCode.RUNTIME_DISCONNECTED, ErrorCode.TIMEOUT, ErrorCode.NETWORK,
                              ErrorCode.QUOTA_EXHAUSTED, ErrorCode.AUTH_REQUIRED)}
USER_ERRORS = {ErrorCode.AUTH_REQUIRED, ErrorCode.BACKEND_UNAVAILABLE, ErrorCode.QUOTA_EXHAUSTED,
               ErrorCode.CONSENT_REQUIRED, ErrorCode.RETRY_BUDGET_EXHAUSTED}


# how a CPU run of a GPU lab fails: CUDA/driver errors, running out of memory, a dead kernel (timeouts count too)
GPU_FAILURE = re.compile(r"cuda|nvidia|\bgpu\b|out of memory|MemoryError|kernel (?:died|restarted)", re.IGNORECASE)


def _gpu_shaped(failed: list[dict[str, Any]]) -> bool:
    return any(f.get("error_code") == str(ErrorCode.TIMEOUT)
               or GPU_FAILURE.search(f"{f.get('error_message') or ''} {f.get('traceback_tail') or ''}") for f in failed)


CLOSING_MESSAGE = "All done, your lab is complete! Hope you liked it. With love from VellaGang <3"


def finish(run: Run, cells_file: Path | None = None, answers_file: Path | None = None, backend: str | None = None,
           allow_cloud: bool = False, allow_upload: bool = False, gpu: str | None = None,
           allow_gpu: bool = False) -> dict[str, Any]:
    """build-notebook -> preflight/execute -> capture -> answers -> export (runs evaluate); skips current steps.

    Stops at the first step that needs the agent (failed cells, answers) or the user (sign-in, consent).
    """
    plan = load_plan(run)
    m = run.manifest
    rid = run.id
    steps: list[dict[str, Any]] = []
    stop: dict[str, str] | None = None
    ran = False  # a step changed the run in this call, so later outputs are no longer current
    step = "build-notebook"

    def say(n: int, text: str) -> None:  # progress on stderr; the JSON result stays on stdout
        run.event("progress", message=f"Step {n}/6: {text}")

    try:
        if cells_file:
            say(1, "building the notebook")
            res = build(run, cells_file)
            steps.append({"step": step, "status": "done", "changed_cells": res["changed_cells"]})
            ran = bool(res["changed_cells"])
        elif run.source_notebook.exists():
            say(1, "notebook already built (skipped)")
            steps.append({"step": step, "status": "skipped", "reason": "notebook already built"})
        else:
            raise LabPilotError(ErrorCode.INVALID_STATE, "No notebook yet.",
                                next_action=f"labpilot finish {rid} --cells cells.json")
        step = "revalidate-inputs"
        say(2, "checking the input files")
        recovered = _recover(run)
        steps += [{**r, "status": "done"} for r in recovered]
        ran = ran or any(r["result"] for r in recovered)

        step = "execute"
        set_gpu(run, gpu, allow_gpu)  # a changed runtime marks every cell stale, so they all run again
        rp = run.exec_dir / "current_records.json"
        transient = {r["cell_id"] for r in (read_json(rp) if rp.exists() else []) if r.get("error_code") in TRANSIENT}
        failed = [c for c, s in m.cell_status.items() if s == ItemStatus.FAILED]
        todo = [c for c, s in m.cell_status.items() if s in (ItemStatus.PENDING, ItemStatus.STALE)]
        if m.backend_state.get("pending_host_actions"):
            stop = {"actor": "agent", "action": "record-execution",
                    "detail": f"Run the cells in host_actions.json via colab-mcp, then `labpilot record-execution {rid} "
                              "--file result.json`, then `labpilot finish` again."}
        elif [c for c in failed if c not in transient] and not todo:
            say(3, "the failing cells are unchanged, so nothing is run (skipped)")
            steps.append({"step": step, "status": "failed", "failed_cells": failed})
            stop = {"actor": "agent", "action": "repair",
                    "detail": f"Fix the failing cells {failed} (tracebacks: `labpilot status {rid} --json`), then "
                              f"`labpilot finish {rid} --cells cells.json`."}
        elif todo or failed:
            say(3, "running the cells on " + ("the local kernel" if (backend or m.backend) == "local" else "Colab"))
            step = "preflight"
            pf = preflight(run, backend or m.backend or "auto", allow_cloud, allow_upload, gpu, allow_gpu)
            bad = [c for c in pf["preflight"]["checks"] if not c["ok"] and c.get("required", True)]
            steps.append({"step": step, "status": "done" if pf["ok"] else "failed", "backend": pf["backend"],
                          "failed_checks": [c["name"] for c in bad], "input_changes": pf["input_changes"]})
            if not pf["ok"]:
                consent = any(c["name"].startswith("consent") for c in bad)
                stop = {"actor": "user" if consent else "agent", "action": "consent" if consent else "preflight",
                        "detail": (f"Ask the user whether LabPilot may run this lab in their Google Colab and upload "
                                   f"the lab files, then `labpilot finish {rid} --allow-cloud --allow-upload`."
                                   if consent else
                                   "; ".join([c["fix"] for c in bad if c.get("fix")] + pf["input_changes"]))}
            else:
                step = "execute"
                ex = execute(run)
                ran = True
                if (ex["status"] == "FAILED_CELLS" and m.consent.gpu and m.backend == "colab-cli"
                        and not m.backend_state.get("gpu") and _gpu_shaped(ex["failed"])):
                    # failed on CPU the way a GPU lab does and the user allowed a free T4: switch once (no loop,
                    # the GPU is now set) and run every cell again on the new runtime
                    say(3, "the lab needs a GPU: switching to a free Colab T4 and running every cell again")
                    steps.append({"step": "switch-to-gpu", "status": "done",
                                  "cpu_failures": [f["cell_id"] for f in ex["failed"]]})
                    set_gpu(run, "T4")
                    ex = execute(run)
                steps.append({"step": step, "status": "failed" if ex["status"] == "FAILED_CELLS" else "done",
                              "result": ex["status"], "attempt_id": ex.get("attempt_id"),
                              "passed": len(ex.get("passed", [])), "failed": ex.get("failed", []),
                              "blocked": ex.get("blocked", []), "runtime_release": ex.get("runtime_release")})
                if ex["status"] == "WAITING_FOR_HOST":
                    stop = {"actor": "agent", "action": "record-execution", "detail": ex["next_action"]}
                elif (ex["status"] == "FAILED_CELLS" and m.backend_state.get("gpu")
                      and "no GPU" in m.backend_state.get("runtime", "") and _gpu_shaped(ex["failed"])):
                    stop = {"actor": "user", "action": "gpu-unavailable",
                            "detail": "This lab needs a GPU, and Google Colab had no free GPU for your account right "
                                      "now (free GPUs run out; the limit resets within about a day). Run the same "
                                      f"`labpilot finish {rid}` command again later."}
                elif ex["status"] == "FAILED_CELLS":
                    stop = {"actor": "agent", "action": "repair",
                            "detail": "Fix the failing cells using `failed[].traceback_tail`, then "
                                      f"`labpilot finish {rid} --cells cells.json`."}
        else:
            say(3, "every cell already passed with the current code (skipped)")
            steps.append({"step": step, "status": "skipped", "reason": "every runnable cell passed with the current code"})

        step = "capture"
        if stop is None:
            ev = load_evidence(run.dir)
            em = run.dir / "evidence" / "evidence_manifest.json"
            if ev and not ran and all(e.attempt_id == m.current_attempt for e in ev):
                problems = read_json(em).get("capture_problems", []) if em.exists() else []
                say(4, "screenshots already match the current run (skipped)")
                steps.append({"step": step, "status": "skipped", "reason": "screenshots match the current attempt"})
            else:
                say(4, "taking screenshots of the outputs")
                cap = capture(run)
                ran = True
                problems = cap["problems"]
                steps.append({"step": step, "status": "failed" if problems else "done", "images": cap["images"],
                              "problems": problems})
            if problems:
                stop = {"actor": "agent", "action": "repair-output",
                        "detail": f"Some cells show no output; add print/display, then `labpilot finish {rid} "
                                  "--cells cells.json`."}

        step = "answers"
        if stop is None:
            say(5, "checking the answers")
            if answers_file:
                res = save_answers(run, answers_file)
                ran = True
                steps.append({"step": step, "status": "done", "answers": res["answers"],
                              "not_verified": res["not_verified"]})
            elif run.path("answers.json").exists() and m.backend_state.get("answers_attempt") == m.current_attempt:
                steps.append({"step": step, "status": "skipped", "reason": "answers match the current attempt"})
            elif any(t.observation_questions for t in plan.tasks) or run.path("answers.json").exists():
                steps.append({"step": step, "status": "waiting"})
                stop = {"actor": "agent", "action": "answers",
                        "detail": f"Read the outputs in {run.executed_notebook}, write answers.json from them, then "
                                  f"`labpilot finish {rid} --answers answers.json`."}
            else:
                steps.append({"step": step, "status": "skipped", "reason": "the plan has no questions"})

        step = "export"
        if stop is None:
            out = m.outputs
            if not ran and m.state == RunState.COMPLETED and out.get("docx") and Path(out["dir"], out["docx"]).exists():
                say(6, "already exported as COMPLETED (skipped)")
                steps.append({"step": step, "status": "skipped", "reason": "already exported as COMPLETED"})
            else:
                say(6, "filling the Word copy and checking it")
                exp = export(run)
                steps.append({"step": step, "status": "done", "result": exp["status"],
                              "failed_checks": exp["failed_checks"], "pages_dir": exp["pages_dir"]})
    except LabPilotError as exc:
        if exc.exit_code == ExitCode.CANCELLED:
            raise
        steps.append({"step": step, "status": "failed", "error": exc.to_dict()})
        stop = {"actor": "user" if exc.code in USER_ERRORS else "agent", "action": str(exc.code),
                "detail": exc.next_action.replace("<run-id>", rid)}

    finished = stop is None and m.state in (RunState.COMPLETED, RunState.PARTIAL)
    closing = CLOSING_MESSAGE if finished and m.state == RunState.COMPLETED else None
    if closing:
        run.event("progress", message=closing)
    exported = next((s for s in steps if s["step"] == "export" and s["status"] == "done"), None)
    return {"run_id": rid, "verdict": str(m.state) if finished else "NOT_FINISHED", "steps": steps,
            "output_dir": m.outputs.get("dir") if finished else None,
            "docx": m.outputs.get("docx") if finished else None,
            "pages_dir": m.validation.get("render", {}).get("pages_dir") if finished else None,
            "failed_checks": exported["failed_checks"] if exported else {},
            "runtime_release": m.backend_state.get("release_pending") or None,
            "next_action": stop or next_action(run), "closing_message": closing}
