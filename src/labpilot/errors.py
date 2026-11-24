"""Stable error codes, CLI exit codes and execution-error classification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    INPUT = 2  # bad arguments, missing/corrupt input files, invalid agent JSON
    CONFIG = 3  # environment / backend / prerequisite problem
    RUNTIME = 4  # execution failed (code error, runtime lost, timeout)
    VALIDATION = 5  # artifacts failed validation
    INCOMPLETE = 6  # run is partial / waiting for input or auth (not an error, not complete)
    LOCKED = 7  # another process holds the run lock
    CANCELLED = 130


class ErrorCode(StrEnum):
    # inputs
    DOCX_UNSUPPORTED = "DOCX_UNSUPPORTED"
    DOCX_ENCRYPTED = "DOCX_ENCRYPTED"
    DOCX_CORRUPT = "DOCX_CORRUPT"
    DATASET_MISSING = "DATASET_MISSING"
    DATASET_AMBIGUOUS = "DATASET_AMBIGUOUS"
    DATASET_SCHEMA_MISMATCH = "DATASET_SCHEMA_MISMATCH"
    DATASET_CORRUPT = "DATASET_CORRUPT"
    INPUT_CHANGED = "INPUT_CHANGED"
    PATH_ESCAPE = "PATH_ESCAPE"
    # agent-supplied JSON
    PLAN_INVALID = "PLAN_INVALID"
    CELLS_INVALID = "CELLS_INVALID"
    ANSWERS_INVALID = "ANSWERS_INVALID"
    RECORD_INVALID = "RECORD_INVALID"
    # run state
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    RUN_LOCKED = "RUN_LOCKED"
    INVALID_STATE = "INVALID_STATE"
    RETRY_BUDGET_EXHAUSTED = "RETRY_BUDGET_EXHAUSTED"
    # environment / backends
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    PLATFORM_UNSUPPORTED = "PLATFORM_UNSUPPORTED"
    PROFILE_PREREQ_MISSING = "PROFILE_PREREQ_MISSING"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    MCP_UNAVAILABLE = "MCP_UNAVAILABLE"
    RUNTIME_DISCONNECTED = "RUNTIME_DISCONNECTED"
    BROWSER_UNAVAILABLE = "BROWSER_UNAVAILABLE"
    # execution classification
    CODE_SYNTAX = "CODE_SYNTAX"
    CODE_RUNTIME = "CODE_RUNTIME"
    MISSING_PACKAGE = "MISSING_PACKAGE"
    MISSING_DATA = "MISSING_DATA"
    NETWORK = "NETWORK"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    SECURITY_DENIED = "SECURITY_DENIED"
    # evidence / document
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    ANCHOR_AMBIGUOUS = "ANCHOR_AMBIGUOUS"
    DOCX_WRITE_FAILED = "DOCX_WRITE_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    IO_ERROR = "IO_ERROR"


# Which error codes are worth retrying automatically / by an agent repair.
RETRYABLE = {
    ErrorCode.CODE_SYNTAX,
    ErrorCode.CODE_RUNTIME,
    ErrorCode.MISSING_PACKAGE,
    ErrorCode.NETWORK,
    ErrorCode.RUNTIME_DISCONNECTED,
    ErrorCode.TIMEOUT,
    ErrorCode.BROWSER_UNAVAILABLE,
}

NEXT_ACTIONS: dict[ErrorCode, str] = {
    ErrorCode.CODE_SYNTAX: "Fix the syntax error in the cell source, rebuild the notebook and execute again.",
    ErrorCode.CODE_RUNTIME: "Read the traceback, repair the cell code, rebuild the notebook and execute again.",
    ErrorCode.MISSING_PACKAGE: "Install the package into the runtime profile (see `labpilot preflight`) or "
    "use a library the profile provides.",
    ErrorCode.MISSING_DATA: "Place the missing dataset in the lab folder, run `labpilot inspect` and resume.",
    ErrorCode.NETWORK: "Check the internet connection of the runtime (Colab or local), then retry.",
    ErrorCode.RESOURCE_EXHAUSTED: "Reduce data size or raise memory limits in the profile.",
    ErrorCode.TIMEOUT: "The cell exceeded its time limit; simplify it or raise --timeout.",
    ErrorCode.SECURITY_DENIED: "Rewrite the cell without the denied operation, or have the user approve it.",
    ErrorCode.RUNTIME_DISCONNECTED: "Resume the run; LabPilot recreates the runtime and replays verified cells.",
    ErrorCode.AUTH_REQUIRED: "Sign in using the official flow for the backend, then resume.",
    ErrorCode.QUOTA_EXHAUSTED: "Stop using this cloud backend; resume with --backend local if allowed.",
}


class LabPilotError(Exception):
    """A structured, user-facing failure."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        exit_code: ExitCode = ExitCode.INPUT,
        next_action: str | None = None,
        affected_tasks: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.next_action = next_action or NEXT_ACTIONS.get(code, "")
        self.affected_tasks = affected_tasks or []
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def to_dict(self) -> dict[str, Any]:
        from labpilot.security import redact_obj

        return redact_obj(
            {
                "code": str(self.code),
                "message": self.message,
                "retryable": self.retryable,
                "affected_tasks": self.affected_tasks,
                "next_action": self.next_action,
                "details": self.details,
            }
        )


@dataclass
class Classified:
    code: ErrorCode
    retryable: bool
    next_action: str
    details: dict[str, Any] = field(default_factory=dict)


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def classify_execution_error(ename: str | None, evalue: str | None, traceback_text: str = "") -> Classified:
    """Map a kernel error (ename/evalue/traceback) to a stable error code."""
    ename = ename or ""
    evalue = evalue or ""
    blob = f"{ename}: {evalue}\n{strip_ansi(traceback_text)}"
    code: ErrorCode
    if ename in {"SyntaxError", "IndentationError", "TabError"}:
        code = ErrorCode.CODE_SYNTAX
    elif ename in {"ModuleNotFoundError", "ImportError"} or "No module named" in blob:
        code = ErrorCode.MISSING_PACKAGE
    elif ename == "FileNotFoundError" or "PATH_NOT_FOUND" in blob or "Path does not exist" in blob:
        code = ErrorCode.MISSING_DATA
    elif ename in {"MemoryError"} or "OutOfMemoryError" in blob or "Java heap space" in blob:
        code = ErrorCode.RESOURCE_EXHAUSTED
    elif ename in {"ConnectionError", "URLError", "gaierror", "TimeoutError"} or (
        "Temporary failure in name resolution" in blob or "Network is unreachable" in blob
    ):
        code = ErrorCode.NETWORK
    elif ename in {"CellTimeoutError", "DeadKernelError"}:
        code = ErrorCode.TIMEOUT if ename == "CellTimeoutError" else ErrorCode.RUNTIME_DISCONNECTED
    elif ename == "PermissionError":
        code = ErrorCode.SECURITY_DENIED
    else:
        code = ErrorCode.CODE_RUNTIME
    return Classified(
        code=code,
        retryable=code in RETRYABLE,
        next_action=NEXT_ACTIONS.get(code, ""),
        details={"ename": ename, "evalue": evalue[:500]},
    )
