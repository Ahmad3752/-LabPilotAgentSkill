"""Build the source notebook from the validated plan and the agent's cells."""

from __future__ import annotations

from typing import Any

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

from labpilot.errors import ErrorCode, LabPilotError
from labpilot.models import CellsFile, ItemStatus, Plan
from labpilot.security import CodeFinding, enforce_code_policy, raise_if_denied, scan_code
from labpilot.workspace import sha256_text


def validate_cells_against_plan(plan: Plan, cells: CellsFile) -> list[str]:
    """Return a list of problems (empty == valid). Also enforces original task order."""
    problems: list[str] = []
    order = {t.id: i for i, t in enumerate(plan.tasks)}
    last = -1
    for c in cells.cells:
        if c.task_id not in order:
            problems.append(f"cell '{c.id}' references unknown task '{c.task_id}'")
            continue
        if order[c.task_id] < last:
            problems.append(f"cell '{c.id}' ({c.task_id}) is out of the manual's task order")
        last = max(last, order[c.task_id])
    by_task: dict[str, list[str]] = {}
    for c in cells.cells:
        if c.kind == "code":
            by_task.setdefault(c.task_id, []).append(c.id)
    for t in plan.tasks:
        if t.required_evidence and not by_task.get(t.id):
            problems.append(f"task '{t.id}' requires evidence but has no code cells")
        if t.cell_ids and set(t.cell_ids) != set(by_task.get(t.id, [])):
            problems.append(
                f"task '{t.id}' plan cell_ids {t.cell_ids} differ from cells.json {by_task.get(t.id, [])}"
            )
    return problems


def build_notebook(
    plan: Plan, cells: CellsFile, run_id: str, approved: set[str] | None = None
) -> tuple[nbformat.NotebookNode, list[CodeFinding]]:
    approved = set(approved or set()) | set(cells.approved_security_rules)
    problems = validate_cells_against_plan(plan, cells)
    if problems:
        raise LabPilotError(ErrorCode.CELLS_INVALID, "cells.json does not match the plan.", details={"problems": problems})

    findings: list[CodeFinding] = []
    for c in cells.cells:
        if c.kind == "code":
            findings.extend(scan_code(c.id, c.source))
    denied, warnings = enforce_code_policy(findings, approved)
    raise_if_denied(denied)

    nb = new_notebook()
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    nb.metadata["language_info"] = {"name": "python"}
    nb.metadata["labpilot"] = {
        "run_id": run_id,
        "lab_title": plan.lab_title,
        "source_sha256": plan.source_sha256,
        "profile": plan.profile,
    }
    intro = new_markdown_cell(
        f"# {plan.lab_title}\n\n"
        "Notebook assembled by LabPilot from the lab manual. Every output below comes from real execution; "
        "see `execution_report.json` for the backend and runtime that produced it.",
    )
    intro.id = "labpilot-intro"
    nb.cells.append(intro)

    tasks = {t.id: t for t in plan.tasks}
    seen_task: set[str] = set()
    for c in cells.cells:
        if c.task_id not in seen_task:
            seen_task.add(c.task_id)
            t = tasks[c.task_id]
            lines = [f"## {t.id} — {t.title}"]
            if t.instructions:
                lines.append("")
                lines.extend(f"- {i}" for i in t.instructions[:12])
            if t.observation_questions:
                lines.append("\n**Questions to answer from the output:**")
                lines.extend(f"- *{q.id}*: {q.text}" for q in t.observation_questions)
            md = new_markdown_cell("\n".join(lines))
            md.id = "md-" + "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in t.id)[:60]
            nb.cells.append(md)
        if c.kind == "markdown":
            cell = new_markdown_cell(c.source)
        else:
            cell = new_code_cell(c.source)
        cell.id = c.id
        cell.metadata["labpilot"] = {
            "task_id": c.task_id,
            "cell_id": c.id,
            "code_sha256": sha256_text(c.source),
            "capture": c.capture,
            "expect_output": c.expect_output,
            "setup": c.setup,
            "title": c.title,
        }
        cell.metadata["tags"] = [f"labpilot-cell-{c.id}", f"labpilot-task-{_tag(c.task_id)}"]
        nb.cells.append(cell)
    nbformat.validate(nb)
    return nb, warnings


def _tag(task_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in task_id)


def code_cells(nb: nbformat.NotebookNode) -> list[Any]:
    return [c for c in nb.cells if c.cell_type == "code"]


def diff_code_hashes(
    previous: dict[str, str], current: dict[str, str], order: list[str]
) -> tuple[dict[str, ItemStatus], list[str]]:
    """Mark changed cells pending and every later cell stale (its kernel state depended on earlier cells)."""
    status: dict[str, ItemStatus] = {}
    changed = [cid for cid in order if previous.get(cid) != current.get(cid)]
    first_change = min((order.index(c) for c in changed), default=None)
    for i, cid in enumerate(order):
        if cid in changed:
            status[cid] = ItemStatus.PENDING
        elif first_change is not None and i > first_change:
            status[cid] = ItemStatus.STALE
    return status, changed
