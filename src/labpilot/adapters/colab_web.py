"""Google Colab in the student's own browser: for chat apps (Claude.ai, ChatGPT) whose sandbox cannot reach Colab.

1. `labpilot finish <run> --backend colab-web` writes `for_colab.ipynb`: an upload cell for the lab files, then
   the planned cells unchanged. The run waits (state EXECUTING, next action `run-in-colab`, actor user).
2. The student opens it in Colab (already signed in to Google there), clicks Runtime > Run all, uploads the
   files when asked, then File > Download > Download .ipynb, and gives that file back to the chat.
3. `labpilot finish <run> --executed <file>` matches the student's cells to the planned ones by code hash and
   reads their real outputs. Edited, missing or unrun cells are not accepted as passed.

LabPilot cannot prove the file came from Colab, so the evidence is labelled "student-run".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

from labpilot.adapters.base import BackendCapabilities, CellRequest, Check, PreflightResult
from labpilot.config import RuntimeProfile
from labpilot.errors import ErrorCode, LabPilotError
from labpilot.models import CellExecution
from labpilot.notebooks.kernel_runner import bound_outputs, summarize
from labpilot.workspace import Run, sha256_text

BACKEND_LABEL = "Google Colab (browser, student-run)"
HANDOFF = "for_colab.ipynb"

STEPS = """# Run this lab in Google Colab

1. **Runtime > Run all.** If Google asks you to sign in, sign in.
2. When the first cell asks for files, choose the lab files listed there.
3. Wait until every cell has finished (it stops early if a cell fails; that is fine).
4. **File > Download > Download .ipynb**, then give that file back to the chat.

Do not edit the cells: LabPilot only accepts its own unchanged cells."""


def _key(source: str) -> str:
    return sha256_text(source.strip())  # tolerant of the trailing-newline changes an editor may make


def _upload_cell(paths: list[str]) -> str:
    return f"""# LabPilot: upload the lab files
import os
need = {{os.path.basename(p): p for p in {paths!r}}}
missing = [p for p in need.values() if not os.path.exists(p)]
if missing:
    from google.colab import files
    print("Choose these lab files:", ", ".join(os.path.basename(p) for p in missing))
    for name, data in files.upload().items():
        path = need.get(name, name)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
missing = [p for p in need.values() if not os.path.exists(p)]
assert not missing, f"Still missing: {{missing}}. Run this cell again and choose them."
print("Lab files ready:", ", ".join(need))"""


class ColabWebBackend:
    name = "colab-web"
    label = BACKEND_LABEL

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(persistent_kernel=True, rich_output=True, native_ui=True, file_transfer=False,
                                   resume_without_replay=False, host_mediated=True, network_isolated=False)

    def preflight(self, run: Run, profile: RuntimeProfile) -> PreflightResult:
        consent = run.manifest.consent
        checks = [Check("consent_cloud_compute", consent.cloud_compute, "user consent to use Colab compute",
                        fix="Ask the user, then pass --allow-cloud.")]
        if any(d.status in ("found", "mapped") for d in run.manifest.datasets):
            checks.append(Check("consent_upload", consent.upload_datasets, "user consent to upload the lab files to Colab",
                                fix="Ask the user, then pass --allow-upload."))
        return PreflightResult(self.name, all(c.ok for c in checks), checks)

    def write_handoff(self, run: Run, cells: list[CellRequest]) -> Path:
        nb = new_notebook()
        nb.metadata["colab"] = {"name": f"{Path(run.manifest.source_name).stem} (LabPilot)", "provenance": []}
        gpu = run.manifest.backend_state.get("gpu")
        if gpu:  # Colab opens the notebook on this runtime type
            nb.metadata["accelerator"] = "GPU"
            nb.metadata["colab"]["gpuType"] = gpu
        nb.cells.append(new_markdown_cell(STEPS))
        files = [d.expected_name for d in run.manifest.datasets if d.staged_path and d.status in ("found", "mapped")]
        if files:
            nb.cells.append(new_code_cell(_upload_cell(files), metadata={"labpilot": {"control": True}}))
        for c in cells:
            nb.cells.append(new_code_cell(c.source, metadata={"labpilot": {"cell_id": c.cell_id, "task_id": c.task_id}}))
        path = run.path(HANDOFF)
        nbformat.write(nb, str(path))
        return path

    def ingest(self, run: Run, executed: Path, expected: list[CellRequest], attempt_id: str) -> list[CellExecution]:
        try:
            nb = nbformat.read(str(executed), as_version=4)
            nbformat.validate(nb)
        except Exception as exc:  # noqa: BLE001 - any unreadable file is the same problem for the user
            raise LabPilotError(ErrorCode.RECORD_INVALID, f"{executed.name} is not a valid notebook: {str(exc)[:300]}",
                                next_action="Download the notebook again from Colab (File > Download > Download .ipynb).") from exc
        got: dict[str, Any] = {}
        for c in nb.cells:
            if c.cell_type == "code":
                got.setdefault(_key(c.source), c)
        if not any(_key(c.source) in got for c in expected):
            raise LabPilotError(ErrorCode.RECORD_INVALID, f"{executed.name} has none of this run's cells.",
                                next_action=f"Give the student the current {HANDOFF} and ask for the downloaded result.")
        records: list[CellExecution] = []
        for c in expected:
            base: dict[str, Any] = {"run_id": run.id, "attempt_id": attempt_id, "cell_id": c.cell_id, "task_id": c.task_id,
                    "code_sha256": c.code_sha256, "backend": BACKEND_LABEL, "runtime": {"where": BACKEND_LABEL}}
            cell = got.get(_key(c.source))
            if cell is None:
                records.append(CellExecution(**base, status="blocked", error_code="NOT_REPORTED",
                                             error_message="Cell missing or edited in Colab; only unchanged cells count."))
                continue
            outputs = [dict(o) for o in cell.outputs]
            if cell.execution_count is None and not outputs:
                records.append(CellExecution(**base, status="blocked", error_code="NOT_RUN",
                                             error_message="Not run in Colab (Run all stops at the first failing cell)."))
                continue
            truncated = bound_outputs(outputs, 200_000)
            s = summarize(outputs)
            err = s["error"]
            records.append(CellExecution(
                **base, status="failed" if err else "passed", execution_count=cell.execution_count,
                stdout=s["stdout"][-20000:], stderr=s["stderr"][-20000:], outputs=outputs, output_truncated=truncated,
                error_type=err["ename"] if err else None, error_message=err["evalue"] if err else None,
                traceback=err["traceback"] if err else None))
        return records
