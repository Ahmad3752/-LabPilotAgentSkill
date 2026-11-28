"""Deterministic workflow steps. The host agent supplies plan/cells/answers; everything else happens here."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any

import nbformat
from pydantic import ValidationError

from labpilot.adapters.base import CellRequest, RunContext
from labpilot.adapters.colab_cli import ColabCliBackend
from labpilot.adapters.colab_mcp_bridge import ColabMcpBridge
from labpilot.adapters.colab_web import ColabWebBackend
from labpilot.adapters.local import LocalBackend
from labpilot.config import RuntimeProfile, load_config, load_profile
from labpilot.datasets.validate import dataset_blockers, resolve_datasets, stage_local
from labpilot.documents.anchors import resolve
from labpilot.documents.inspect import colab_features, colab_fit, draft_plan, inspect_docx
from labpilot.errors import RETRYABLE, ErrorCode, ExitCode, LabPilotError, classify_execution_error
from labpilot.evidence.capture import capture_notebook
from labpilot.evidence.manifest import load_evidence, save_evidence
from labpilot.models import (
    AnswersFile,
    Blocker,
    CellExecution,
    CellsFile,
    DatasetEntry,
    HostExecutionRecord,
    ItemStatus,
    Plan,
    RequiredFile,
    RunManifest,
    RunState,
    utcnow,
)
from labpilot.notebooks.build import build_notebook, diff_code_hashes
from labpilot.security import check_docx_container, redact
from labpilot.workspace import (
    Run,
    atomic_write_json,
    create_run,
    new_run_id,
    pid_alive,
    read_json,
    sha256_file,
    sha256_text,
)

DATA_SUFFIXES_FOR_PLAN = {".csv", ".tsv", ".json", ".jsonl", ".xlsx", ".xls", ".parquet"}


# ====================================================================== helpers


def _load_json_file(path: Path, what: str, code: ErrorCode) -> Any:
    if not path.exists():
        raise LabPilotError(code, f"{what} file '{path}' not found.")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise LabPilotError(code, f"{what} is not valid JSON: {exc}") from exc


def _pydantic_problems(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


def load_plan(run: Run) -> Plan:
    p = run.path("plan.json")
    if not p.exists():
        raise LabPilotError(ErrorCode.INVALID_STATE, "No plan yet. Submit one with `labpilot plan <run-id> --file plan.json`.")
    return Plan.model_validate(read_json(p))


def load_cells(run: Run) -> CellsFile:
    p = run.path("cells.json")
    if not p.exists():
        raise LabPilotError(ErrorCode.INVALID_STATE, "No cells yet. Submit them with `labpilot build-notebook`.")
    return CellsFile.model_validate(read_json(p))


def _source_copy(run: Run) -> Path:
    return run.inputs_dir / Path(run.manifest.source_name).name


def _original(run: Run) -> Path:
    return run.lab_dir / run.manifest.source_name


def check_inputs(run: Run) -> list[str]:
    """Detect changes to the original manual or datasets since they were registered."""
    m = run.manifest
    changes: list[str] = []
    orig = _original(run)
    if orig.exists() and sha256_file(orig) != m.source_sha256:
        changes.append(f"manual '{m.source_name}' changed")
    if sha256_file(_source_copy(run)) != m.source_sha256:
        changes.append("internal copy of the manual changed")
    for d in m.datasets:
        if d.local_path and d.sha256:
            p = run.lab_dir / d.local_path
            if not p.exists():
                changes.append(f"dataset '{d.expected_name}' disappeared")
            elif sha256_file(p) != d.sha256:
                changes.append(f"dataset '{d.expected_name}' changed")
    return changes


def _profile(run: Run) -> RuntimeProfile:
    return load_profile(run.manifest.profile or "python")


def _code_cells(run: Run) -> list[Any]:
    nb = nbformat.read(str(run.source_notebook), as_version=4)
    return [c for c in nb.cells if c.cell_type == "code"]


def _blocked_tasks(plan: Plan, datasets: list[DatasetEntry]) -> set[str]:
    bad = set()
    for d in datasets:
        req = next((r for r in plan.required_files if r.name == d.expected_name), None)
        if d.status not in ("found", "mapped") and (req is None or req.required):
            bad.update(d.used_by_tasks or [t.id for t in plan.tasks])  # unknown usage: assume everything
    changed = True
    while changed:
        changed = False
        for t in plan.tasks:
            if t.id not in bad and any(dep in bad for dep in t.depends_on):
                bad.add(t.id)
                changed = True
    return bad


def _recompute_tasks(run: Run, plan: Plan) -> None:
    m = run.manifest
    blocked = _blocked_tasks(plan, m.datasets)
    for t in plan.tasks:
        statuses = [m.cell_status.get(c, ItemStatus.PENDING) for c in t.cell_ids]
        if t.id in blocked:
            m.task_status[t.id] = ItemStatus.BLOCKED
        elif not statuses:
            m.task_status[t.id] = ItemStatus.PENDING
        elif all(s == ItemStatus.PASSED for s in statuses):
            m.task_status[t.id] = ItemStatus.PASSED
        elif any(s == ItemStatus.FAILED for s in statuses):
            m.task_status[t.id] = ItemStatus.FAILED
        elif any(s == ItemStatus.BLOCKED for s in statuses):
            m.task_status[t.id] = ItemStatus.BLOCKED
        elif any(s == ItemStatus.STALE for s in statuses):
            m.task_status[t.id] = ItemStatus.STALE
        else:
            m.task_status[t.id] = ItemStatus.PENDING


def _set_blockers(run: Run, kind_prefix: str, blockers: list[Blocker]) -> None:
    m = run.manifest
    m.blockers = [b for b in m.blockers if not b.code.startswith(kind_prefix)] + blockers


# ====================================================================== init / inspect


def _lms_provenance(lab_dir: Path, manual_name: str, digest: str) -> dict[str, str]:
    """LMS origin of the manual, if `labpilot lms stage` recorded it and the file is unchanged since."""
    from labpilot.lms import read_source

    source = read_source(lab_dir)
    if not source:
        return {"provider": "local-folder"}
    staged = {f.get("name"): f.get("sha256") for f in source.get("files", [])}
    out = {"provider": str(source["provider"]),
           **{k: str(source[k]) for k in ("site", "course", "courseid", "assignid", "cmid", "name", "duedate", "staged_at")
              if source.get(k) is not None}}
    out["manual_matches_download"] = str(staged.get(manual_name) == digest).lower()
    return out


def init_run(lab_doc: Path, data_dir: Path | None = None, student: dict[str, str] | None = None,
             synthetic: bool = False) -> Run:
    lab_doc = lab_doc.expanduser()
    if not lab_doc.exists():
        raise LabPilotError(ErrorCode.DOCX_UNSUPPORTED, f"Lab manual '{lab_doc}' not found.")
    check_docx_container(lab_doc)
    lab_dir = (data_dir or lab_doc.parent).resolve()
    try:
        source_name = lab_doc.resolve().relative_to(lab_dir).as_posix()
    except ValueError as exc:
        raise LabPilotError(ErrorCode.PATH_ESCAPE, "The manual must be inside the lab folder (--data-dir).") from exc
    digest = sha256_file(lab_doc)
    manifest = RunManifest(
        run_id=new_run_id(), lab_dir=str(lab_dir), source_name=source_name, source_sha256=digest,
        student={k: v for k, v in (student or {}).items() if v}, synthetic=synthetic,
        input_provenance=_lms_provenance(lab_dir, lab_doc.name, digest),
    )
    run = create_run(lab_dir, manifest)
    run.inputs_dir.mkdir(parents=True, exist_ok=True)
    copy = run.inputs_dir / lab_doc.name
    shutil.copy2(lab_doc, copy)
    os.chmod(copy, stat.S_IREAD)  # internal copy is read-only too
    if sha256_file(copy) != digest:
        raise LabPilotError(ErrorCode.IO_ERROR, "Copy of the manual does not match the original hash.")
    return run


def inspect_run(run: Run, aliases: dict[str, str] | None = None) -> dict[str, Any]:
    m = run.manifest
    if aliases:
        stored = m.backend_state.setdefault("aliases", {})
        stored.update(aliases)
    insp = inspect_docx(_source_copy(run), m.source_sha256, run.doc_images_dir)
    colab = colab_features(insp)
    fit = colab_fit("\n".join(b.text for b in insp.blocks))
    plan_path = run.path("plan.json")
    if plan_path.exists():
        required = Plan.model_validate(read_json(plan_path)).required_files
    else:
        required = [
            RequiredFile(name=f["name"], status="unchecked")
            for f in insp.file_references
            if Path(f["name"]).suffix.lower() in DATA_SUFFIXES_FOR_PLAN
        ]
    all_aliases = {**load_config().get("aliases", {}), **m.backend_state.get("aliases", {})}
    m.datasets = resolve_datasets(run.lab_dir, required, all_aliases)
    for r in required:
        d = next(x for x in m.datasets if x.expected_name == r.name)
        r.status = "found" if d.status == "found" else ("mapped" if d.status == "mapped" else
                                                         ("ambiguous" if d.status == "ambiguous" else "missing"))
    m.lab_title = m.lab_title or (insp.title_candidates[0] if insp.title_candidates else None)
    m.profile = "python"
    doc = insp.to_dict()
    doc["colab_features"] = colab
    doc["colab_fit"] = fit
    atomic_write_json(run.path("document.json"), doc)
    draft = draft_plan(insp, [r.model_dump() for r in required])
    atomic_write_json(run.path("draft_plan.json"), draft)
    _set_blockers(run, "DATASET_", dataset_blockers(m.datasets, required))
    if m.state in (RunState.CREATED,):
        run.set_state(RunState.INSPECTED)
    run.save()
    run.event("progress", message=f"inspected {len(insp.blocks)} blocks, {len(insp.tasks)} tasks")
    return {
        "run_id": run.id,
        "lab_title_candidates": insp.title_candidates,
        "environment": insp.environment,
        "colab_fit": fit,
        "colab_features": colab,
        "requires_native_colab_evidence": insp.requires_native_colab_evidence,
        "tasks": [{"id": t["id"], "title": t["title"], "parts": [p["id"] for p in t["parts"]],
                   "questions": len(t["questions"]) + sum(len(p["questions"]) for p in t["parts"])} for t in insp.tasks],
        "solution_boxes": insp.solution_boxes,
        "student_fields": insp.student_fields,
        "file_references": insp.file_references,
        "datasets": [d.model_dump(include={"expected_name", "status", "local_path", "candidates", "problems",
                                           "schema_summary"}) for d in m.datasets],
        "images": insp.images,
        "warnings": insp.warnings,
        "artifacts": {"document": "document.json", "draft_plan": "draft_plan.json",
                      "images_dir": run.doc_images_dir.name, "run_dir": str(run.dir)},
    }


# ====================================================================== plan


def save_plan(run: Run, plan_file: Path, allow_uncovered: bool = False) -> dict[str, Any]:
    data = _load_json_file(plan_file, "Plan", ErrorCode.PLAN_INVALID)
    try:
        plan = Plan.model_validate(data)
    except ValidationError as exc:
        raise LabPilotError(ErrorCode.PLAN_INVALID, "plan.json failed schema validation.",
                            details={"problems": _pydantic_problems(exc)}) from exc
    m = run.manifest
    problems: list[str] = []
    warnings: list[str] = []
    if plan.source_sha256 != m.source_sha256:
        problems.append("source_sha256 does not match the registered manual (inputs changed or wrong file)")
    ids = [t.id for t in plan.tasks]
    for i, t in enumerate(plan.tasks):
        for dep in t.depends_on:
            if dep not in ids:
                problems.append(f"{t.id}: depends_on unknown task '{dep}'")
            elif ids.index(dep) >= i:
                problems.append(f"{t.id}: depends on '{dep}' which does not come earlier (order must follow the manual)")
        if t.placement == "anchor" and not t.solution_anchor:
            problems.append(f"{t.id}: placement 'anchor' needs a solution_anchor")
        if t.placement == "after_source" and not t.source_anchor:
            problems.append(f"{t.id}: placement 'after_source' needs a source_anchor")
    import docx

    body = docx.Document(str(_source_copy(run))).element.body
    for t in plan.tasks:
        for label, anchor in (("source_anchor", t.source_anchor), ("solution_anchor", t.solution_anchor)):
            if anchor:
                try:
                    resolve(body, anchor)
                except LabPilotError as exc:
                    problems.append(f"{t.id}.{label}: {exc.message}")
    doc_path = run.path("document.json")
    if doc_path.exists():
        doc = read_json(doc_path)
        planned = set(ids)
        for task in doc.get("tasks", []):
            units = [p["id"] for p in task["parts"]] or [task["id"]]
            missing = [u for u in units if u not in planned]
            if missing:
                (warnings if allow_uncovered else problems).append(
                    f"inspected task units not covered by the plan: {missing}")
        planned_q = {q.text.strip() for t in plan.tasks for q in t.observation_questions} | {
            q.id for t in plan.tasks for q in t.observation_questions}
        for q in doc.get("questions", []):
            if q["id"] not in planned_q and q["text"].strip() not in planned_q:
                warnings.append(f"detected question not in plan (check it is covered): {q['text'][:100]}")
    if problems:
        raise LabPilotError(ErrorCode.PLAN_INVALID, "Plan failed validation.", details={"problems": problems,
                                                                                         "warnings": warnings})
    all_aliases = {**load_config().get("aliases", {}), **m.backend_state.get("aliases", {})}
    m.datasets = resolve_datasets(run.lab_dir, plan.required_files, all_aliases)
    for r in plan.required_files:
        d = next(x for x in m.datasets if x.expected_name == r.name)
        r.status = d.status if d.status in ("found", "mapped", "missing", "ambiguous") else "missing"
    blockers = dataset_blockers(m.datasets, plan.required_files)
    _set_blockers(run, "DATASET_", blockers)
    atomic_write_json(run.path("plan.json"), plan.model_dump(mode="json"))
    m.lab_title = plan.lab_title
    m.profile = plan.profile
    for t in plan.tasks:
        m.task_status.setdefault(t.id, ItemStatus.PENDING)
    _recompute_tasks(run, plan)
    run.save()
    run.set_state(RunState.WAITING_FOR_INPUT if blockers else RunState.PLANNED,
                  "missing inputs; independent tasks can still run" if blockers else "plan accepted")
    return {
        "run_id": run.id, "tasks": len(plan.tasks),
        "questions": sum(len(t.observation_questions) for t in plan.tasks),
        "datasets": [{"name": d.expected_name, "status": d.status, "problems": d.problems} for d in m.datasets],
        "blocked_tasks": sorted(t for t, s in m.task_status.items() if s == ItemStatus.BLOCKED),
        "blockers": [b.model_dump() for b in blockers], "warnings": warnings,
    }


# ====================================================================== notebook


def build(run: Run, cells_file: Path, approve: set[str] | None = None) -> dict[str, Any]:
    plan = load_plan(run)
    data = _load_json_file(cells_file, "Cells", ErrorCode.CELLS_INVALID)
    try:
        cells = CellsFile.model_validate(data)
    except ValidationError as exc:
        raise LabPilotError(ErrorCode.CELLS_INVALID, "cells.json failed schema validation.",
                            details={"problems": _pydantic_problems(exc)}) from exc
    try:
        nb, warnings = build_notebook(plan, cells, run.id, approve)
    except LabPilotError as exc:
        if exc.code == ErrorCode.SECURITY_DENIED:
            run.manifest.security_events.append({"ts": utcnow(), "event": "denied", **exc.details})
            run.save()
            run.event("security", decision="denied", findings=exc.details.get("findings"))
        raise
    m = run.manifest
    order = [c.id for c in cells.cells if c.kind == "code"]
    current = {c.id: sha256_text(c.source) for c in cells.cells if c.kind == "code"}
    changes, changed = diff_code_hashes(m.cell_code_sha256, current, order)
    for cid in changed:
        if m.cell_status.get(cid) == ItemStatus.FAILED:
            m.cell_repairs[cid] = m.cell_repairs.get(cid, 0) + 1
    for cid, st in changes.items():
        m.cell_status[cid] = st
    for cid in order:
        m.cell_status.setdefault(cid, ItemStatus.PENDING)
    for cid in list(m.cell_status):
        if cid not in current:
            del m.cell_status[cid]
    m.cell_code_sha256 = current
    # fill the plan's cell mapping from the submitted cells
    by_task: dict[str, list[str]] = {}
    for c in cells.cells:
        if c.kind == "code":
            by_task.setdefault(c.task_id, []).append(c.id)
    for t in plan.tasks:
        t.cell_ids = by_task.get(t.id, [])
    atomic_write_json(run.path("plan.json"), plan.model_dump(mode="json"))
    atomic_write_json(run.path("cells.json"), cells.model_dump(mode="json"))
    run.nb_dir.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, str(run.source_notebook))
    if warnings:
        m.security_events.append({"ts": utcnow(), "event": "warnings", "findings": [w.to_dict() for w in warnings]})
    _recompute_tasks(run, plan)
    run.save()
    run.event("progress", message=f"notebook built: {len(order)} code cells, {len(changed)} changed")
    return {"run_id": run.id, "code_cells": len(order), "changed_cells": changed,
            "stale_cells": [c for c, s in changes.items() if s == ItemStatus.STALE],
            "security_warnings": [w.to_dict() for w in warnings], "notebook": str(run.source_notebook)}


# ====================================================================== preflight / backends


def make_backend(name: str) -> Any:
    if name == "local":
        return LocalBackend()
    if name == "colab-cli":
        return ColabCliBackend()
    if name == "colab-mcp":
        return ColabMcpBridge()
    if name == "colab-web":
        return ColabWebBackend()
    raise LabPilotError(ErrorCode.BACKEND_UNAVAILABLE, f"Unknown backend '{name}'.", exit_code=ExitCode.CONFIG)


COLAB_LOGIN = "labpilot colab-login"
COLAB_INSTALL = "labpilot setup --backend colab-cli --install-colab-cli"


def _check_backend_allowed(backend: str) -> None:
    """Lab code runs on Google Colab only; the local kernel exists for LabPilot's own tests and offline demo."""
    if backend == "local" and os.environ.get("LABPILOT_ALLOW_LOCAL") != "1":
        raise LabPilotError(ErrorCode.BACKEND_UNAVAILABLE,
                            "Lab code runs only on Google Colab; the local kernel is reserved for LabPilot's tests "
                            "and offline demo (LABPILOT_ALLOW_LOCAL=1).", exit_code=ExitCode.CONFIG,
                            next_action="labpilot preflight <run-id> --backend colab-cli --allow-cloud --allow-upload")


def _choose(run: Run, backend: str) -> tuple[str, list[str]]:
    """`auto` means Google Colab (the CLI, or colab-mcp when the user configured it)."""
    notes: list[str] = []
    if backend == "auto":
        configured = load_config().get("backend", "auto")
        backend = configured if configured in ("colab-cli", "colab-mcp") else "colab-cli"
        notes.append(f"auto: lab code runs on Google Colab ({backend})")
    _check_backend_allowed(backend)
    return backend, notes


def gpu_choice(gpu: str) -> str | None:
    """`--gpu` value as stored on the run: the variant in upper case, or None (CPU) for "none"/"cpu"."""
    return None if gpu.lower() in ("none", "cpu") else gpu.upper()


def set_gpu(run: Run, gpu: str | None, allow_gpu: bool = False) -> bool:
    """Record the `--gpu`/`--allow-gpu` choice. A runtime change re-runs every cell, so one notebook = one runtime."""
    m = run.manifest
    m.consent.gpu = m.consent.gpu or allow_gpu
    if not gpu or gpu_choice(gpu) == m.backend_state.get("gpu"):
        return False
    m.backend_state["gpu"] = gpu_choice(gpu)
    m.consent.gpu = m.consent.gpu or m.backend_state["gpu"] is not None  # an explicit --gpu is the user's yes
    for cid, status in m.cell_status.items():
        if status != ItemStatus.PENDING:
            m.cell_status[cid] = ItemStatus.STALE
    run.save()
    return True


def preflight(run: Run, backend: str = "auto", allow_cloud: bool = False, allow_upload: bool = False,
              gpu: str | None = None, allow_gpu: bool = False) -> dict[str, Any]:
    plan = load_plan(run)
    m = run.manifest
    profile = load_profile(plan.profile)
    m.profile = plan.profile
    set_gpu(run, gpu, allow_gpu)
    if allow_cloud or allow_upload:
        m.consent.cloud_compute = m.consent.cloud_compute or allow_cloud
        m.consent.upload_datasets = m.consent.upload_datasets or allow_upload
        m.consent.scope = "run"
        m.consent.granted_at = utcnow()
        for d in m.datasets:
            d.cloud_upload_consent = m.consent.upload_datasets
    chosen, notes = _choose(run, backend)
    be = make_backend(chosen)
    result = be.preflight(run, profile)
    changes = check_inputs(run)
    m.backend = chosen
    if chosen == "colab-cli":
        failed = {c.name for c in result.checks if not c.ok}
        if "colab_cli" in failed:
            run.save()
            raise LabPilotError(ErrorCode.BACKEND_UNAVAILABLE, "The Google Colab CLI is not installed or configured.",
                                exit_code=ExitCode.CONFIG, next_action=COLAB_INSTALL)
        if "colab_auth" in failed:
            run.save()
            run.set_state(RunState.WAITING_FOR_AUTH, "Colab sign-in required")
            raise LabPilotError(ErrorCode.AUTH_REQUIRED, "You are not signed in to Google Colab.",
                                exit_code=ExitCode.CONFIG,
                                next_action=f"Run `{COLAB_LOGIN}` in your own terminal, then run the command again.")
    staged: list[str] = []
    if result.ok and not changes:
        stage_local(run.lab_dir, run.data_dir, m.datasets)
        staged = [d.expected_name for d in m.datasets if d.staged_path]
        run.save()
        run.set_state(RunState.PREFLIGHT_OK)
        run.set_state(RunState.STAGED, f"{len(staged)} file(s) staged")
    elif result.state_hint == "WAITING_FOR_AUTH":
        run.save()
        run.set_state(RunState.WAITING_FOR_AUTH)
    else:
        run.save()
    return {"run_id": run.id, "backend": chosen, "notes": notes,
            "capabilities": be.capabilities().to_dict(), "preflight": result.to_dict(),
            "input_changes": changes, "staged": staged, "ok": result.ok and not changes}


# ====================================================================== execution


def _target_cells(run: Run, plan: Plan, upto: str | None) -> tuple[list[CellRequest], list[str]]:
    blocked_tasks = {t for t, s in run.manifest.task_status.items() if s == ItemStatus.BLOCKED} | _blocked_tasks(
        plan, run.manifest.datasets)
    reqs: list[CellRequest] = []
    skipped: list[str] = []
    found = upto is None
    for c in _code_cells(run):
        meta = c.metadata.get("labpilot", {})
        if meta.get("task_id") in blocked_tasks:
            skipped.append(c.id)
            continue
        reqs.append(CellRequest(c.id, meta.get("task_id", ""), c.source, sha256_text(c.source)))
        if upto and c.id == upto:
            found = True
            break
    if upto and not found:
        raise LabPilotError(ErrorCode.CELLS_INVALID, f"Cell '{upto}' is not an executable cell of this run.")
    return reqs, skipped


def assemble_executed(run: Run, records: list[CellExecution], target: Path) -> Path:
    nb = nbformat.read(str(run.source_notebook), as_version=4)
    by_id = {r.cell_id: r for r in records}
    for c in nb.cells:
        if c.cell_type != "code":
            continue
        r = by_id.get(c.id)
        c.outputs = [nbformat.from_dict(o) for o in r.outputs] if r else []
        c.execution_count = r.execution_count if r else None
    nbformat.validate(nb)
    target.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, str(target))
    return target


def apply_records(run: Run, plan: Plan, attempt_id: str, records: list[CellExecution], executed_nb: Path | None,
                  backend_label: str, requested: list[str], skipped: list[str]) -> dict[str, Any]:
    m = run.manifest
    m.backend_state.pop("executing_attempt", None)
    attempt_dir = run.exec_dir / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(attempt_dir / "records.json", [r.model_dump(mode="json") for r in records])
    for r in records:
        if r.status == "failed" and not r.error_code:
            r.error_code = str(classify_execution_error(r.error_type, r.error_message, r.traceback or "").code)
    for r in records:
        m.cell_status[r.cell_id] = {"passed": ItemStatus.PASSED, "failed": ItemStatus.FAILED}.get(r.status, ItemStatus.BLOCKED)
    executed = {r.cell_id for r in records}
    for cid in m.cell_code_sha256:
        if cid not in executed and m.cell_status.get(cid) == ItemStatus.PASSED:
            m.cell_status[cid] = ItemStatus.STALE  # passed in an older attempt; not part of this evidence set
    for cid in skipped:
        m.cell_status[cid] = ItemStatus.BLOCKED
    if executed_nb is None or not executed_nb.exists():
        executed_nb = assemble_executed(run, records, attempt_dir / "executed.ipynb")
    shutil.copy2(executed_nb, run.executed_notebook)
    atomic_write_json(run.exec_dir / "current_records.json", [r.model_dump(mode="json") for r in records])
    m.current_attempt = attempt_id
    if records and records[0].runtime:
        m.runtime_identity = records[0].runtime
    failed = [r for r in records if r.status == "failed"]
    m.attempts.append({"attempt_id": attempt_id, "backend": backend_label, "ended_at": utcnow(),
                       "requested": len(requested), "passed": sum(r.status == "passed" for r in records),
                       "failed": [r.cell_id for r in failed]})
    _recompute_tasks(run, plan)
    run.save()
    return {
        "attempt_id": attempt_id,
        "backend": backend_label,
        "passed": [r.cell_id for r in records if r.status == "passed"],
        "failed": [
            {"cell_id": r.cell_id, "task_id": r.task_id, "error_code": r.error_code, "error_type": r.error_type,
             "error_message": redact((r.error_message or "")[:1500]),
             "traceback_tail": redact((r.traceback or "")[-3000:]),
             "repairs_used": m.cell_repairs.get(r.cell_id, 0), "repair_budget": m.retry_budget_per_cell,
             "retryable": r.error_code in {str(c) for c in RETRYABLE}}
            for r in failed
        ],
        "blocked": [r.cell_id for r in records if r.status == "blocked"] + skipped,
        "executed_notebook": str(run.executed_notebook),
    }


def execute(run: Run, cell: str | None = None, timeout: int | None = None, retry_budget: int | None = None,
            backend_override: str | None = None) -> dict[str, Any]:
    plan = load_plan(run)
    m = run.manifest
    if not run.source_notebook.exists():
        raise LabPilotError(ErrorCode.INVALID_STATE, "Build the notebook first (`labpilot build-notebook`).")
    if backend_override:
        m.backend = _choose(run, backend_override)[0]
    _check_backend_allowed(m.backend or "")
    if m.backend in ("colab-cli", "colab-mcp", "colab-web"):
        needs_upload = any(d.status in ("found", "mapped") for d in m.datasets)
        if not m.consent.cloud_compute or (needs_upload and not m.consent.upload_datasets):
            raise LabPilotError(ErrorCode.CONSENT_REQUIRED,
                                "Cloud execution needs the user's consent for compute and for uploading their files.",
                                exit_code=ExitCode.CONFIG,
                                next_action="Ask the user, then `labpilot preflight <run-id> --backend "
                                            f"{m.backend} --allow-cloud --allow-upload`.")
    unstaged = [d.expected_name for d in m.datasets if d.status in ("found", "mapped") and not d.staged_path]
    if not m.backend or unstaged:
        raise LabPilotError(ErrorCode.INVALID_STATE, "Run `labpilot preflight <run-id>` before executing"
                            + (f" (unstaged inputs: {unstaged})." if unstaged else "."))
    changes = check_inputs(run)
    if changes:
        raise LabPilotError(ErrorCode.INPUT_CHANGED, "Inputs changed since staging: " + "; ".join(changes),
                            next_action="Run `labpilot resume <run-id>` to re-validate and re-stage inputs, "
                                        "or start a new run if the manual itself changed.")
    if retry_budget is not None:
        m.retry_budget_per_cell = retry_budget
    exhausted = [c for c, n in m.cell_repairs.items() if n > m.retry_budget_per_cell and m.cell_status.get(c) != ItemStatus.PASSED]
    if exhausted:
        tasks = sorted({t.id for t in plan.tasks if set(t.cell_ids) & set(exhausted)})
        _set_blockers(run, "RETRY_", [Blocker(code="RETRY_BUDGET_EXHAUSTED",
                                              message=f"Cells {exhausted} exceeded the repair budget ({m.retry_budget_per_cell}).",
                                              affected_tasks=tasks, requires_user=True,
                                              next_action="Export a partial result, or raise --retry-budget after review.")])
        run.save()
        raise LabPilotError(ErrorCode.RETRY_BUDGET_EXHAUSTED, f"Repair budget exhausted for {exhausted}.",
                            exit_code=ExitCode.RUNTIME, affected_tasks=tasks)
    profile = _profile(run)
    targets, skipped = _target_cells(run, plan, cell)
    if not targets:
        raise LabPilotError(ErrorCode.DATASET_MISSING, "Every task is blocked by missing inputs.", exit_code=ExitCode.INCOMPLETE,
                            next_action="Supply the missing files, then `labpilot resume`.")
    attempt_id = f"a{len(m.attempts) + 1:03d}"
    be = make_backend(m.backend)

    if m.backend == "colab-mcp":
        actions = be.write_host_actions(run, targets)
        m.backend_state["pending_attempt"] = attempt_id
        m.backend_state["pending_targets"] = [c.cell_id for c in targets]
        m.backend_state["pending_skipped"] = skipped
        run.save()
        run.set_state(RunState.EXECUTING, "waiting for host agent to run cells via colab-mcp")
        return {"status": "WAITING_FOR_HOST", "host_actions": str(run.path("host_actions.json")),
                "cells": len(actions["cells"]),
                "next_action": "Host agent: follow host_actions.json, then `labpilot record-execution <run-id> --file result.json`."}

    if m.backend == "colab-web":
        handoff = be.write_handoff(run, targets)
        m.backend_state.update(pending_colab_web=True, pending_attempt=attempt_id,
                               pending_targets=[c.cell_id for c in targets], pending_skipped=skipped)
        run.save()
        run.set_state(RunState.EXECUTING, "waiting for the student to run the notebook in Colab")
        return {"status": "WAITING_FOR_USER", "handoff": str(handoff), "cells": len(targets)}

    run.path("cancel.requested").unlink(missing_ok=True)  # a new explicit execute supersedes an old cancel
    ctx = RunContext(run=run, profile=profile, attempt_id=attempt_id, timeout_s=timeout or profile.cell_timeout_s,
                     datasets=m.datasets)
    m.backend_state["executing_attempt"] = attempt_id  # cleared by apply_records; left behind only by a crash
    run.set_state(RunState.EXECUTING, f"attempt {attempt_id} on {getattr(be, 'label', be.name)}")

    where = "Colab" if (m.backend or "").startswith("colab") else "Local kernel"
    finished: set[str] = set()  # a set, so a replay after a lost runtime does not count cells twice

    def on_event(ev: dict[str, Any]) -> None:
        if ev.get("event") in ("cell", "cell_start"):
            ids = ev.get("cell_ids") or [ev.get("cell_id")]
            if ev.get("event") == "cell":
                finished.add(str(ev.get("cell_id")))
            run.event("cell", cell_id=ev.get("cell_id") if len(ids) == 1 else f"{ids[0]} .. {ids[-1]}",
                      status=ev.get("status", "running"), message=ev.get("error_type") or "",
                      done=len(finished), total=len(targets), where=where)
        elif ev.get("event") == "runtime":
            run.event("progress", message=f"runtime {ev.get('runtime')}")

    records: list[CellExecution] = []
    handle = None
    try:
        while True:
            handle = be.start(ctx)
            stage = be.stage_inputs(handle, ctx)
            if not stage.ok:
                raise LabPilotError(ErrorCode.DATASET_MISSING, "Staging failed: " + "; ".join(stage.problems),
                                    exit_code=ExitCode.RUNTIME)
            records = be.execute_cells(handle, ctx, targets, on_event)
            lost = next((r for r in records if r.error_code == str(ErrorCode.RUNTIME_DISCONNECTED)), None)
            if lost and m.connection_recoveries < m.connection_recovery_budget and m.backend == "colab-cli":
                m.connection_recoveries += 1
                run.event("progress", message=f"runtime lost; recreating and replaying ({m.connection_recoveries}/"
                                              f"{m.connection_recovery_budget})")
                be.stop(handle, ctx)
                continue
            break
    except KeyboardInterrupt:
        be.cancel(run)
        if records:
            apply_records(run, plan, attempt_id, records, None, getattr(be, "label", be.name), [c.cell_id for c in targets], skipped)
        run.set_state(RunState.PAUSED, "interrupted by user; resume with `labpilot resume`")
        raise LabPilotError(ErrorCode.INVALID_STATE, "Interrupted; state saved.", exit_code=ExitCode.CANCELLED) from None
    except LabPilotError as exc:
        if exc.code == ErrorCode.AUTH_REQUIRED:
            run.set_state(RunState.WAITING_FOR_AUTH, exc.message)
        raise
    finally:
        if handle is not None and m.backend == "colab-cli" and not os.environ.get("LABPILOT_KEEP_SESSION"):
            be.stop(handle, ctx)
    nb_path = be.executed_notebook(handle) if hasattr(be, "executed_notebook") and handle else None
    summary = apply_records(run, plan, attempt_id, records, nb_path, getattr(be, "label", be.name),
                            [c.cell_id for c in targets], skipped)
    if run.path("cancel.requested").exists():
        run.path("cancel.requested").unlink(missing_ok=True)
        run.set_state(RunState.CANCELLED, "cancelled by user; checkpointed records kept")
        raise LabPilotError(ErrorCode.INVALID_STATE, "Execution cancelled; resume with `labpilot resume`.",
                            exit_code=ExitCode.CANCELLED)
    quota = [r for r in records if r.error_code == str(ErrorCode.QUOTA_EXHAUSTED)]
    if quota:
        _set_blockers(run, "QUOTA", [Blocker(code="QUOTA_EXHAUSTED", message="Colab quota/resources unavailable.",
                                             next_action="Wait until Colab has resources again, then `labpilot finish <run-id>`.",
                                             requires_user=True)])
        run.save()
    summary["status"] = "FAILED_CELLS" if summary["failed"] else ("PARTIAL" if summary["blocked"] else "OK")
    if m.backend_state.get("release_pending"):
        summary["runtime_release"] = {
            "confirmed": False, "sessions": m.backend_state["release_pending"],
            "detail": "Release of the Colab runtime was attempted, not confirmed; retry with `labpilot cancel <run-id>`."}
    return summary


def record_execution(run: Run, result_file: Path) -> dict[str, Any]:
    plan = load_plan(run)
    m = run.manifest
    if not m.backend_state.get("pending_host_actions"):
        raise LabPilotError(ErrorCode.INVALID_STATE, "No pending host actions; run `labpilot execute --backend colab-mcp` first.")
    data = _load_json_file(result_file, "Execution record", ErrorCode.RECORD_INVALID)
    try:
        record = HostExecutionRecord.model_validate(data)
    except ValidationError as exc:
        raise LabPilotError(ErrorCode.RECORD_INVALID, "result.json failed schema validation.",
                            details={"problems": _pydantic_problems(exc)}) from exc
    pending = m.backend_state.get("pending_targets", [])
    current = {c.id: c for c in _code_cells(run)}
    expected = [CellRequest(cid, current[cid].metadata.get("labpilot", {}).get("task_id", ""), current[cid].source,
                            sha256_text(current[cid].source)) for cid in pending if cid in current]
    attempt_id = m.backend_state.get("pending_attempt") or f"a{len(m.attempts) + 1:03d}"
    records = ColabMcpBridge().ingest(run, record, expected, attempt_id)
    reported = {r.cell_id for r in records}
    for c in expected:
        if c.cell_id not in reported:
            records.append(CellExecution(run_id=run.id, attempt_id=attempt_id, cell_id=c.cell_id, task_id=c.task_id,
                                         code_sha256=c.code_sha256, backend="colab-mcp (host-reported)", status="blocked",
                                         error_code="NOT_REPORTED", error_message="Host did not report this cell."))
    summary = apply_records(run, plan, attempt_id, records, None, "colab-mcp (host-reported)", pending,
                            m.backend_state.get("pending_skipped", []))
    summary["status"] = "FAILED_CELLS" if summary["failed"] else ("PARTIAL" if summary["blocked"] else "OK")
    return summary


def record_colab_web(run: Run, executed: Path) -> dict[str, Any]:
    """Read the notebook the student ran in Colab and downloaded (see adapters/colab_web.py)."""
    plan = load_plan(run)
    m = run.manifest
    if not m.backend_state.get("pending_colab_web"):
        raise LabPilotError(ErrorCode.INVALID_STATE, "No notebook is waiting for a Colab run.",
                            next_action=f"labpilot finish {run.id} --backend colab-web --allow-cloud --allow-upload")
    pending = m.backend_state.get("pending_targets", [])
    current = {c.id: c for c in _code_cells(run)}
    expected = [CellRequest(cid, current[cid].metadata.get("labpilot", {}).get("task_id", ""), current[cid].source,
                            sha256_text(current[cid].source)) for cid in pending if cid in current]
    attempt_id = m.backend_state.get("pending_attempt") or f"a{len(m.attempts) + 1:03d}"
    records = ColabWebBackend().ingest(run, executed, expected, attempt_id)
    m.backend_state["pending_colab_web"] = False
    summary = apply_records(run, plan, attempt_id, records, None, ColabWebBackend.label, pending,
                            m.backend_state.get("pending_skipped", []))
    summary["status"] = "FAILED_CELLS" if summary["failed"] else ("PARTIAL" if summary["blocked"] else "OK")
    summary["not_run"] = [r.cell_id for r in records if r.error_code in ("NOT_RUN", "NOT_REPORTED")]
    return summary


# ====================================================================== evidence


def _provenance(run: Run) -> str:
    m = run.manifest
    backend = m.attempts[-1]["backend"] if m.attempts else (m.backend or "?")
    rt = m.runtime_identity
    bits = [f"local rendering of the notebook executed on {backend}"]
    if rt.get("python"):
        bits.append(f"Python {rt['python']}")
    # mention PySpark only when the lab actually uses it (Colab images ship it preinstalled)
    uses_spark = run.source_notebook.exists() and "pyspark" in run.source_notebook.read_text(encoding="utf-8").lower()
    if rt.get("pyspark") and uses_spark:
        bits.append(f"PySpark {rt['pyspark']}")
    bits.append(f"run {run.id}/{m.current_attempt}")
    if m.synthetic:
        bits.append("SYNTHETIC DEMO DATA")
    return " · ".join(bits)


def capture(run: Run, mode: str = "auto") -> dict[str, Any]:
    plan = load_plan(run)
    m = run.manifest
    if not run.executed_notebook.exists() or not m.current_attempt:
        raise LabPilotError(ErrorCode.INVALID_STATE, "Nothing executed yet; run `labpilot execute` first.")
    if mode == "native":
        raise LabPilotError(
            ErrorCode.BACKEND_UNAVAILABLE,
            "Native Colab UI capture needs a user-authorised, visible Colab browser session showing the executed "
            "notebook; LabPilot v1 does not automate it. Use --mode rendered (labelled local rendering).",
            exit_code=ExitCode.CONFIG,
        )
    nb = nbformat.read(str(run.source_notebook), as_version=4)
    specs = []
    for c in nb.cells:
        meta = c.metadata.get("labpilot", {})
        if c.cell_type == "code" and meta.get("capture", True) and m.cell_status.get(c.id) == ItemStatus.PASSED:
            specs.append({"cell_id": c.id, "task_id": meta.get("task_id"), "code_sha256": sha256_text(c.source),
                          "expect_output": meta.get("expect_output", True), "source": c.source})
    backend = m.attempts[-1]["backend"] if m.attempts else (m.backend or "unknown")
    result = capture_notebook(run_dir=run.dir, run_id=run.id, attempt_id=m.current_attempt,
                              executed_nb=run.executed_notebook, backend=backend, cells_to_capture=specs,
                              provenance=_provenance(run))
    save_evidence(run.dir, result.records, {"run_id": run.id, "attempt_id": m.current_attempt,
                                            "capture_problems": result.problems, "html": result.html_path})
    all_passed = all(m.task_status.get(t.id) == ItemStatus.PASSED for t in plan.tasks)
    if not result.problems and all_passed:
        run.set_state(RunState.EVIDENCE_READY)
    run.event("progress", message=f"captured {len(result.records)} screenshot(s), {len(result.problems)} problem(s)")
    return {"run_id": run.id, "images": len(result.records), "capture_mode": "rendered-local-notebook",
            "screenshots": [r.path for r in result.records], "problems": result.problems,
            "evidence_manifest": "evidence/evidence_manifest.json"}


# ====================================================================== answers


def validate_answers(run: Run, plan: Plan, answers: AnswersFile) -> list[str]:
    m = run.manifest
    problems: list[str] = []
    questions = {q.id: (t, q) for t in plan.tasks for q in t.observation_questions}
    seen = set()
    evidence_paths = {e.path for e in load_evidence(run.dir)}
    for a in answers.answers:
        if a.question_id not in questions:
            problems.append(f"answer for unknown question '{a.question_id}'")
            continue
        if a.question_id in seen:
            problems.append(f"duplicate answer for '{a.question_id}'")
        seen.add(a.question_id)
        _, q = questions[a.question_id]
        if not a.text.strip():
            problems.append(f"{a.question_id}: empty answer")
        for cid in a.source_cell_ids:
            if cid not in m.cell_code_sha256:
                problems.append(f"{a.question_id}: unknown source cell '{cid}'")
        for ev in a.evidence:
            if ev not in evidence_paths:
                problems.append(f"{a.question_id}: evidence '{ev}' not in the evidence manifest")
        if a.verification == "verified":
            if not a.source_cell_ids:
                problems.append(f"{a.question_id}: verified answers must cite the cells that produced the result")
            bad = [c for c in a.source_cell_ids if m.cell_status.get(c) != ItemStatus.PASSED]
            if bad:
                problems.append(f"{a.question_id}: cited cells {bad} did not pass in the current attempt; "
                                "mark the answer not_verified or re-execute")
        elif a.verification == "conceptual" and q.kind == "observed":
            problems.append(f"{a.question_id}: asks about observed results; it must be 'verified' (cite cells) "
                            "or 'not_verified'")
    missing = [qid for qid in questions if qid not in seen]
    if missing:
        problems.append(f"missing answers for questions: {missing}")
    for s in answers.task_summaries:
        if s.task_id not in {t.id for t in plan.tasks}:
            problems.append(f"task summary for unknown task '{s.task_id}'")
    if answers.table_cells:
        import docx

        body = docx.Document(str(_source_copy(run))).element.body
        seen_cells: set[str] = set()
        for f in answers.table_cells:
            path = f.anchor.split("#")[0]
            if not re.search(r"/r\d+/c\d+$", path):
                problems.append(f"table cell '{f.anchor}' is not a table-cell anchor")
                continue
            if path in seen_cells:
                problems.append(f"table cell '{f.anchor}' is filled twice")
            seen_cells.add(path)
            try:
                resolve(body, f.anchor)
            except LabPilotError as exc:
                problems.append(f"table cell '{f.anchor}': {exc.message}")
            if not f.text.strip():
                problems.append(f"table cell '{f.anchor}': empty value")
            if f.verification == "verified":
                if not f.source_cell_ids:
                    problems.append(f"table cell '{f.anchor}': cite the cells whose output contains this value")
                bad = [c for c in f.source_cell_ids if m.cell_status.get(c) != ItemStatus.PASSED]
                if bad:
                    problems.append(f"table cell '{f.anchor}': cited cells {bad} did not pass")
    return problems


def save_answers(run: Run, answers_file: Path) -> dict[str, Any]:
    plan = load_plan(run)
    data = _load_json_file(answers_file, "Answers", ErrorCode.ANSWERS_INVALID)
    try:
        answers = AnswersFile.model_validate(data)
    except ValidationError as exc:
        raise LabPilotError(ErrorCode.ANSWERS_INVALID, "answers.json failed schema validation.",
                            details={"problems": _pydantic_problems(exc)}) from exc
    problems = validate_answers(run, plan, answers)
    if problems:
        raise LabPilotError(ErrorCode.ANSWERS_INVALID, "Answers failed validation.", details={"problems": problems})
    atomic_write_json(run.path("answers.json"), answers.model_dump(mode="json"))
    run.manifest.backend_state["answers_attempt"] = run.manifest.current_attempt
    run.save()
    not_verified = [a.question_id for a in answers.answers if a.verification == "not_verified"]
    if not not_verified and run.manifest.state == RunState.EVIDENCE_READY:
        run.set_state(RunState.ANSWERS_READY)
    return {"run_id": run.id, "answers": len(answers.answers), "not_verified": not_verified}


# ====================================================================== export / validate (see report.py)


def cancel(run: Run) -> dict[str, Any]:
    m = run.manifest
    # Tell a concurrently running `execute` (which owns the manifest while it holds the lock) to persist
    # CANCELLED itself once its runtime stops; this avoids it overwriting our state change.
    run.path("cancel.requested").write_text(utcnow(), encoding="utf-8")
    stopped = False
    for name in {"local", m.backend or "local"}:
        try:
            be = make_backend(name)
            stopped = be.cancel(run) or stopped
        except LabPilotError:
            continue
    lock = run.dir / "run.lock"
    holder_alive = False
    if lock.exists():
        try:
            holder_alive = pid_alive(int(json.loads(lock.read_text(encoding="utf-8")).get("pid", -1)))
        except (OSError, ValueError, json.JSONDecodeError):
            holder_alive = False
    if not holder_alive:  # nothing else owns the run: record the state directly
        lock.unlink(missing_ok=True)
        run.set_state(RunState.CANCELLED, "cancelled by user")
        run.path("cancel.requested").unlink(missing_ok=True)
    return {"run_id": run.id, "stopped_processes": stopped, "state": str(RunState.CANCELLED),
            "resume": f"labpilot resume {run.id}"}


def reset_for_changed_inputs(run: Run) -> list[str]:
    """Re-resolve and re-stage datasets after they were added/changed; invalidate execution state."""
    plan = load_plan(run)
    m = run.manifest
    changes = check_inputs(run)
    if any("manual" in c for c in changes):
        raise LabPilotError(ErrorCode.INPUT_CHANGED, "The lab manual changed; start a new run with `labpilot init`.",
                            next_action="labpilot init --lab <manual> (a new run keeps evidence provenance clean)")
    all_aliases = {**load_config().get("aliases", {}), **m.backend_state.get("aliases", {})}
    before = {d.expected_name: (d.status, d.sha256) for d in m.datasets}
    m.datasets = resolve_datasets(run.lab_dir, plan.required_files, all_aliases)
    after = {d.expected_name: (d.status, d.sha256) for d in m.datasets}
    if before != after or changes:
        for cid in m.cell_status:
            if m.cell_status[cid] in (ItemStatus.PASSED, ItemStatus.BLOCKED):
                m.cell_status[cid] = ItemStatus.STALE
        stage_local(run.lab_dir, run.data_dir, m.datasets)
        changes.append("datasets re-resolved and re-staged; all cells will be replayed")
    _set_blockers(run, "DATASET_", dataset_blockers(m.datasets, plan.required_files))
    _recompute_tasks(run, plan)
    run.save()
    return changes
