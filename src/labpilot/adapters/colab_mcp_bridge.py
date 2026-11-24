"""Host-mediated Google Colab MCP workflow (https://github.com/googlecolab/colab-mcp).

The official colab-mcp server is connected to the HOST agent (e.g. Claude Code),
not to this CLI, and its tool names are discovered dynamically
(`notifications/tools/list_changed`); they are not documented upstream, so
LabPilot never hardcodes them. Instead:

1. `labpilot execute <run> --backend colab-mcp` writes `host_actions.json`: the
   ordered cells (id, source, sha256), files to upload, and the exact result
   schema. The run waits (exit code 6, state EXECUTING, pending host action).
2. The host agent uses whatever colab-mcp tools it discovered to run those
   cells in the user's browser-connected Colab notebook, collecting the real
   nbformat output objects of each cell.
3. The agent submits them with `labpilot record-execution <run> --file result.json`.
   LabPilot validates them strictly (order, code hashes, nbformat output schema,
   status/output consistency) before accepting anything.

LabPilot cannot cryptographically prove the outputs came from Colab; the
evidence manifest therefore labels this path "host-reported (colab-mcp)".
"""

from __future__ import annotations

from typing import Any

import nbformat
from nbformat.v4 import new_code_cell, new_notebook

from labpilot.adapters.base import BackendCapabilities, CellRequest, Check, PreflightResult
from labpilot.config import RuntimeProfile
from labpilot.errors import ErrorCode, LabPilotError
from labpilot.models import CellExecution, HostExecutionRecord
from labpilot.notebooks.kernel_runner import bound_outputs, summarize
from labpilot.workspace import Run, atomic_write_json

BACKEND_LABEL = "colab-mcp (host-reported)"


class ColabMcpBridge:
    name = "colab-mcp"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            persistent_kernel=True,
            rich_output=True,
            native_ui=True,
            file_transfer=False,  # not documented upstream; agent must verify per server version
            resume_without_replay=False,
            host_mediated=True,
            network_isolated=False,
        )

    def preflight(self, run: Run, profile: RuntimeProfile) -> PreflightResult:
        from labpilot.doctor import mcp_registration

        reg = mcp_registration()
        checks = [
            Check("mcp_registered", reg["registered"], reg["detail"],
                  fix="Register the official server in your agent (see docs/BACKENDS.md#colab-mcp), restart it, "
                      "and open/connect a Colab notebook in the browser."),
            Check("host_tools_discoverable", True,
                  "Only the host agent can confirm colab-mcp tools are listed; if they are missing, report MCP_UNAVAILABLE.",
                  required=False),
        ]
        consent = run.manifest.consent
        checks.append(Check("consent_cloud_compute", consent.cloud_compute, "user consent to use Colab compute",
                            fix="Ask the user, then pass --allow-cloud."))
        if any(d.status in ("found", "mapped") for d in run.manifest.datasets):
            checks.append(Check("consent_upload", consent.upload_datasets, "user consent to upload datasets to Colab",
                                fix="Ask the user, then pass --allow-upload."))
        return PreflightResult(self.name, all(c.ok for c in checks if c.required), checks)

    def write_host_actions(self, run: Run, cells: list[CellRequest]) -> dict[str, Any]:
        uploads = [
            {"name": d.expected_name, "local_path": str((run.dir / d.staged_path).resolve()), "sha256": d.sha256,
             "remote_path": f"/content/{d.expected_name}"}
            for d in run.manifest.datasets
            if d.staged_path and d.status in ("found", "mapped")
        ]
        actions = {
            "schema_version": 1,
            "run_id": run.id,
            "backend": "colab-mcp",
            "instructions": [
                "Confirm the colab-mcp tools are listed in this session; if not, stop and report MCP_UNAVAILABLE.",
                "Use the browser-connected Colab notebook the user authorised. Never handle Google credentials.",
                "Upload each file in `uploads` to its remote_path using a supported tool and verify its sha256 in a cell "
                "(hashlib) before running dependent cells. If no supported upload mechanism exists, stop and report it.",
                "Run `cells` strictly in order in ONE runtime. For each, record the cell's real nbformat output objects "
                "exactly as returned (stream/display_data/execute_result/error). Stop at the first error.",
                "Write result.json matching `result_schema` and call: labpilot record-execution <run-id> --file result.json",
                "Never invent, edit or summarise outputs. If outputs cannot be retrieved, record nothing and report it.",
            ],
            "uploads": uploads,
            "cells": [{"cell_id": c.cell_id, "task_id": c.task_id, "code_sha256": c.code_sha256, "source": c.source}
                      for c in cells],
            "result_schema": HostExecutionRecord.model_json_schema(),
        }
        atomic_write_json(run.path("host_actions.json"), actions)
        run.manifest.backend_state["pending_host_actions"] = True
        run.save()
        return actions

    def ingest(self, run: Run, record: HostExecutionRecord, expected: list[CellRequest], attempt_id: str) -> list[CellExecution]:
        problems: list[str] = []
        if not record.tool_names_used:
            problems.append("tool_names_used is empty; name the colab-mcp tools actually invoked")
        order = [c.cell_id for c in expected]
        got = [c.cell_id for c in record.cells]
        if got != order[: len(got)]:
            problems.append(f"cells must be the first {len(got)} planned cells in order; expected {order[:len(got)]}, got {got}")
        by_id = {c.cell_id: c for c in expected}
        for i, c in enumerate(record.cells):
            exp = by_id.get(c.cell_id)
            if exp and exp.code_sha256 != c.code_sha256:
                problems.append(f"{c.cell_id}: code_sha256 does not match the current cell source")
            nb = new_notebook()
            cell = new_code_cell("")
            cell.outputs = [nbformat.from_dict(o) for o in c.outputs]
            nb.cells.append(cell)
            try:
                nbformat.validate(nb)
            except nbformat.ValidationError as exc:
                problems.append(f"{c.cell_id}: outputs are not valid nbformat output objects ({exc.message[:200]})")
                continue
            has_error = any(o.get("output_type") == "error" for o in c.outputs)
            if has_error != (c.status == "failed"):
                problems.append(f"{c.cell_id}: status '{c.status}' is inconsistent with its outputs")
            if c.status == "failed" and i != len(record.cells) - 1:
                problems.append(f"{c.cell_id}: cells after a failed cell must not be reported")
        if problems:
            raise LabPilotError(ErrorCode.RECORD_INVALID, "Host execution record rejected.", details={"problems": problems})
        results: list[CellExecution] = []
        for c in record.cells:
            outputs = [dict(o) for o in c.outputs]
            truncated = bound_outputs(outputs, 200_000)
            s = summarize(outputs)
            results.append(CellExecution(
                run_id=run.id, attempt_id=attempt_id, cell_id=c.cell_id, task_id=by_id[c.cell_id].task_id,
                code_sha256=c.code_sha256, backend=BACKEND_LABEL, runtime=record.runtime, status=c.status,
                execution_count=c.execution_count, started_at=c.started_at, ended_at=c.ended_at,
                stdout=s["stdout"][-20000:], stderr=s["stderr"][-20000:], outputs=outputs, output_truncated=truncated,
                error_type=s["error"]["ename"] if s["error"] else None,
                error_message=s["error"]["evalue"] if s["error"] else None,
                traceback=s["error"]["traceback"] if s["error"] else None,
            ))
        run.manifest.backend_state["pending_host_actions"] = False
        run.manifest.backend_state["mcp_tools_used"] = record.tool_names_used
        if record.notebook_url:
            run.manifest.backend_state["notebook_url"] = record.notebook_url
        return results
