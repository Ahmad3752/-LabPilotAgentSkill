"""Typed, validated schemas for every file LabPilot reads from or writes for the host agent."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from labpilot import SCHEMA_VERSION

CELL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # nbformat 4.5 cell id rule
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------ states


class RunState(StrEnum):
    CREATED = "CREATED"
    INSPECTED = "INSPECTED"
    PLANNED = "PLANNED"
    PREFLIGHT_OK = "PREFLIGHT_OK"
    STAGED = "STAGED"
    EXECUTING = "EXECUTING"
    EVIDENCE_READY = "EVIDENCE_READY"
    ANSWERS_READY = "ANSWERS_READY"
    DOCUMENT_WRITTEN = "DOCUMENT_WRITTEN"
    VALIDATED = "VALIDATED"
    COMPLETED = "COMPLETED"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    WAITING_FOR_AUTH = "WAITING_FOR_AUTH"
    PAUSED = "PAUSED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


MAIN_SEQUENCE = [
    RunState.CREATED,
    RunState.INSPECTED,
    RunState.PLANNED,
    RunState.PREFLIGHT_OK,
    RunState.STAGED,
    RunState.EXECUTING,
    RunState.EVIDENCE_READY,
    RunState.ANSWERS_READY,
    RunState.DOCUMENT_WRITTEN,
    RunState.VALIDATED,
    RunState.COMPLETED,
]


class ItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    STALE = "stale"


# ------------------------------------------------------------------ plan (agent -> LabPilot)


class RequiredFile(Strict):
    name: str
    role: str = "dataset"
    required: bool = True
    expected_columns: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    status: Literal["found", "mapped", "missing", "ambiguous", "unchecked"] = "unchecked"
    used_by_tasks: list[str] = Field(default_factory=list)


class Question(Strict):
    id: str
    text: str
    kind: Literal["observed", "conceptual"] = "observed"

    @field_validator("id")
    @classmethod
    def _qid(cls, v: str) -> str:
        if not TASK_ID_RE.match(v):
            raise ValueError(f"invalid question id '{v}'")
        return v


def _both_evidence() -> list[Literal["code", "output"]]:
    return ["code", "output"]


class Task(Strict):
    id: str
    title: str
    parent: str | None = None  # e.g. "T1" for "T1.A"; used to group parts in one solution box
    source_anchor: str | None = None
    solution_anchor: str | None = None
    placement: Literal["anchor", "after_source", "append"] = "anchor"
    depends_on: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    observation_questions: list[Question] = Field(default_factory=list)
    required_evidence: list[Literal["code", "output"]] = Field(default_factory=lambda: _both_evidence())
    cell_ids: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _tid(cls, v: str) -> str:
        if not TASK_ID_RE.match(v):
            raise ValueError(f"invalid task id '{v}'")
        return v


class Plan(Strict):
    schema_version: int = SCHEMA_VERSION
    lab_title: str
    source_sha256: str
    profile: Literal["python"] = "python"  # Colab labs install anything else in their own setup cells
    requires_native_colab_evidence: bool = False
    required_files: list[RequiredFile] = Field(default_factory=list)
    tasks: list[Task]
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Plan:
        ids = [t.id for t in self.tasks]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate task ids: {sorted(dupes)}")
        qids = [q.id for t in self.tasks for q in t.observation_questions]
        qd = {i for i in qids if qids.count(i) > 1}
        if qd:
            raise ValueError(f"duplicate question ids: {sorted(qd)}")
        cids = [c for t in self.tasks for c in t.cell_ids]
        cd = {i for i in cids if cids.count(i) > 1}
        if cd:
            raise ValueError(f"cell ids assigned to more than one task: {sorted(cd)}")
        return self


# ------------------------------------------------------------------ cells (agent -> LabPilot)


class CellSpec(Strict):
    id: str
    task_id: str
    source: str
    kind: Literal["code", "markdown"] = "code"
    title: str | None = None
    capture: bool = True  # include in evidence screenshots
    expect_output: bool = True  # an empty output is treated as missing evidence
    setup: bool = False  # deterministic environment setup cell (replayed first)

    @field_validator("id")
    @classmethod
    def _cid(cls, v: str) -> str:
        if not CELL_ID_RE.match(v):
            raise ValueError(f"cell id '{v}' must match [A-Za-z0-9_-]{{1,64}}")
        return v


class CellsFile(Strict):
    schema_version: int = SCHEMA_VERSION
    cells: list[CellSpec]
    approved_security_rules: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> CellsFile:
        ids = [c.id for c in self.cells]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate cell ids: {sorted(dupes)}")
        return self


# ------------------------------------------------------------------ answers (agent -> LabPilot)


class Answer(Strict):
    question_id: str
    text: str
    source_cell_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)  # evidence relative paths (optional)
    verification: Literal["verified", "conceptual", "not_verified"] = "verified"


class TaskSummary(Strict):
    task_id: str
    explanation: str  # what the code does, for the student


class TableCellFill(Strict):
    """A value the manual asks to copy into an existing table cell (e.g. job counters)."""

    anchor: str  # cell anchor with fingerprint, from document.json `fill_in_tables`
    text: str
    source_cell_ids: list[str] = Field(default_factory=list)
    verification: Literal["verified", "not_verified"] = "verified"


class AnswersFile(Strict):
    schema_version: int = SCHEMA_VERSION
    answers: list[Answer]
    task_summaries: list[TaskSummary] = Field(default_factory=list)
    table_cells: list[TableCellFill] = Field(default_factory=list)


# ------------------------------------------------------------------ execution records


class CellExecution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    attempt_id: str
    cell_id: str
    task_id: str | None = None
    code_sha256: str
    backend: str
    runtime: dict[str, Any] = Field(default_factory=dict)
    status: Literal["passed", "failed", "blocked", "skipped"]
    execution_count: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    stdout: str = ""
    stderr: str = ""
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    output_truncated: bool = False
    error_code: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    traceback: str | None = None
    retry_count: int = 0


class HostCellResult(Strict):
    """One cell executed by the host agent through Colab MCP tools (record-execution)."""

    cell_id: str
    code_sha256: str
    status: Literal["passed", "failed"]
    execution_count: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    outputs: list[dict[str, Any]]


class HostExecutionRecord(Strict):
    schema_version: int = SCHEMA_VERSION
    backend: Literal["colab-mcp"] = "colab-mcp"
    mcp_server: str = "colab-mcp"
    tool_names_used: list[str]
    runtime: dict[str, Any] = Field(default_factory=dict)
    notebook_url: str | None = None
    cells: list[HostCellResult]


# ------------------------------------------------------------------ datasets


class DatasetEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expected_name: str
    role: str = "dataset"
    local_path: str | None = None  # relative to lab dir
    staged_path: str | None = None  # relative to run dir
    runtime_path: str | None = None  # path inside the execution runtime (set at staging)
    status: Literal["found", "mapped", "missing", "ambiguous", "corrupt", "schema_mismatch"] = "missing"
    candidates: list[str] = Field(default_factory=list)
    sha256: str | None = None
    size_bytes: int | None = None
    origin: str = "user-local-folder"
    schema_summary: dict[str, Any] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)
    cloud_upload_consent: bool = False
    used_by_tasks: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ evidence


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    attempt_id: str
    task_id: str
    cell_ids: list[str]
    code_sha256: dict[str, str]
    backend: str
    capture_mode: Literal["rendered-local-notebook", "native-colab-ui"]
    kind: Literal["code", "output", "code-output"]
    part: int = 1
    parts: int = 1
    captured_at: str
    sha256: str
    width: int
    height: int
    path: str  # relative to run dir
    valid: bool = True
    problems: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ run manifest


class Blocker(BaseModel):
    code: str
    message: str
    affected_tasks: list[str] = Field(default_factory=list)
    next_action: str = ""
    requires_user: bool = False


class Consent(BaseModel):
    cloud_compute: bool = False
    upload_datasets: bool = False
    scope: Literal["run", "none"] = "none"
    granted_at: str | None = None


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    run_id: str
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    state: RunState = RunState.CREATED
    previous_state: RunState | None = None
    lab_dir: str  # absolute path of the lab folder on this machine (not exported)
    source_name: str  # original manual file name, relative to lab dir
    source_sha256: str
    lab_title: str | None = None
    synthetic: bool = False  # explicitly labelled synthetic/demo data run; never the real assignment
    student: dict[str, str] = Field(default_factory=dict)  # only explicit user-supplied fields
    profile: str | None = None
    backend: str | None = None
    datasets: list[DatasetEntry] = Field(default_factory=list)
    cell_status: dict[str, ItemStatus] = Field(default_factory=dict)
    task_status: dict[str, ItemStatus] = Field(default_factory=dict)
    cell_code_sha256: dict[str, str] = Field(default_factory=dict)
    cell_repairs: dict[str, int] = Field(default_factory=dict)
    current_attempt: str | None = None
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    connection_recoveries: int = 0
    retry_budget_per_cell: int = 3
    connection_recovery_budget: int = 2
    consent: Consent = Field(default_factory=Consent)
    runtime_identity: dict[str, Any] = Field(default_factory=dict)
    backend_state: dict[str, Any] = Field(default_factory=dict)  # e.g. owned colab session name
    blockers: list[Blocker] = Field(default_factory=list)
    security_events: list[dict[str, Any]] = Field(default_factory=list)
    outputs: dict[str, str] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ future integration boundary


class InputManifest(BaseModel):
    manual_path: str
    dataset_paths: list[str] = Field(default_factory=list)
    provenance: dict[str, str] = Field(default_factory=dict)


class RunRequest(BaseModel):
    """A request to process one lab; produced by an InputProvider (v1: local folder only)."""

    inputs: InputManifest
    backend_preference: str = "auto"
    output_dir: str | None = None
    consent: Consent = Field(default_factory=Consent)


class RunResult(BaseModel):
    run_id: str
    status: RunState
    artifacts: dict[str, str] = Field(default_factory=dict)
    blockers: list[Blocker] = Field(default_factory=list)
