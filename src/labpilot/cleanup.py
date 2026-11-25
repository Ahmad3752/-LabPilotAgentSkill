"""`labpilot clean`: free the temporary workspace of a COMPLETED lab, keeping the deliverables.

Kept (in the lab's output folder): the Completed .docx, the executed .ipynb, the screenshots,
REVIEW.md, execution_report.json and evidence_manifest.json.
Kept (in .labpilot/runs/<id>, a few KB): the manifest, plan, cells, answers, document map,
evaluation and event log, so `labpilot status` still explains what was done.
Removed: execution logs and checkpoints, the evidence working copies and HTML render, the page
previews, the internal copy of the manual, staged dataset copies and temporary files. The
original manual and datasets in the lab folder are never touched.

Partial or failed runs are refused: they need the workspace to resume.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from labpilot.errors import ErrorCode, ExitCode, LabPilotError
from labpilot.models import RunState, utcnow
from labpilot.workspace import Run, rmtree_owned

REMOVABLE_DIRS = ("executions", "evidence", "render", "notebook", "inputs", "data", "tmp", "document_images")
REQUIRED_OUTPUTS = (".docx", ".ipynb")


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def is_cleaned(run: Run) -> bool:
    return bool(run.manifest.backend_state.get("cleaned_at"))


def ensure_not_cleaned(run: Run) -> None:
    if is_cleaned(run):
        raise LabPilotError(
            ErrorCode.INVALID_STATE,
            f"Run {run.id} was cleaned after completion; its working files were removed. The deliverables are in "
            f"{run.manifest.outputs.get('dir')}.",
            exit_code=ExitCode.INPUT,
            next_action="Start a new run with `labpilot init` if you need to redo this lab.",
        )


def clean_run(run: Run, force: bool = False) -> dict[str, Any]:
    m = run.manifest
    if is_cleaned(run):
        return {"run_id": run.id, "cleaned": True, "already_clean": True, "freed_bytes": 0,
                "kept_output": m.outputs.get("dir")}
    if m.state != RunState.COMPLETED and not force:
        raise LabPilotError(
            ErrorCode.INVALID_STATE,
            f"Run {run.id} is {m.state}, not COMPLETED; its workspace is needed to resume, so it was not cleaned.",
            exit_code=ExitCode.INCOMPLETE,
            next_action="Finish the lab (labpilot resume) or pass --force to discard the workspace anyway.",
        )
    out_dir = Path(m.outputs["dir"]) if m.outputs.get("dir") else None
    if not force:
        present = {p.suffix for p in out_dir.iterdir()} if out_dir and out_dir.exists() else set()
        missing = [s for s in REQUIRED_OUTPUTS if s not in present]
        if missing:
            raise LabPilotError(
                ErrorCode.INVALID_STATE,
                f"The output folder is missing {missing}; nothing was deleted.",
                exit_code=ExitCode.VALIDATION,
                next_action="Run `labpilot export <run-id>` again before cleaning.",
            )
    removed: list[str] = []
    freed = 0
    for name in REMOVABLE_DIRS:
        target = run.dir / name
        if target.exists():
            freed += _size(target)
            rmtree_owned(target)
            removed.append(name)
    m.backend_state["cleaned_at"] = utcnow()
    m.backend_state["cleaned_dirs"] = removed
    run.save()
    run.event("progress", message=f"workspace cleaned: {len(removed)} folders, {freed / 1e6:.1f} MB freed")
    kept = sorted(p.name for p in out_dir.iterdir()) if out_dir and out_dir.exists() else []
    return {"run_id": run.id, "cleaned": True, "removed": removed, "freed_bytes": freed,
            "freed_mb": round(freed / 1e6, 1), "kept_output": str(out_dir) if out_dir else None, "kept_files": kept,
            "workspace_left_bytes": _size(run.dir)}
