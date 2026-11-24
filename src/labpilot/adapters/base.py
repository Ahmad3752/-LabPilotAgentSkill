"""Runtime backend contract shared by the local, Colab CLI and host-mediated Colab MCP backends."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from labpilot.config import RuntimeProfile
from labpilot.models import CellExecution, DatasetEntry
from labpilot.workspace import Run


@dataclass
class BackendCapabilities:
    persistent_kernel: bool
    rich_output: bool
    native_ui: bool
    file_transfer: bool
    resume_without_replay: bool
    host_mediated: bool
    network_isolated: bool
    platform_supported: bool = True

    def to_dict(self) -> dict[str, bool]:
        return dict(self.__dict__)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class PreflightResult:
    backend: str
    ok: bool
    checks: list[Check] = field(default_factory=list)
    state_hint: str | None = None  # e.g. WAITING_FOR_AUTH

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "ok": self.ok,
            "state_hint": self.state_hint,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class CellRequest:
    cell_id: str
    task_id: str
    source: str
    code_sha256: str


@dataclass
class RunContext:
    run: Run
    profile: RuntimeProfile
    attempt_id: str
    timeout_s: int
    datasets: list[DatasetEntry]


@dataclass
class RuntimeHandle:
    backend: str
    attempt_id: str
    identity: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageResult:
    ok: bool
    staged: dict[str, str] = field(default_factory=dict)  # expected name -> runtime path
    problems: list[str] = field(default_factory=list)


@dataclass
class RuntimeHealth:
    alive: bool
    kernel_state_intact: bool
    detail: str = ""


OnEvent = Callable[[dict[str, Any]], None]


class RuntimeBackend(Protocol):
    name: str

    def capabilities(self) -> BackendCapabilities: ...

    def preflight(self, run: Run, profile: RuntimeProfile) -> PreflightResult: ...

    def start(self, ctx: RunContext) -> RuntimeHandle: ...

    def stage_inputs(self, handle: RuntimeHandle, ctx: RunContext) -> StageResult: ...

    def execute_cells(
        self, handle: RuntimeHandle, ctx: RunContext, cells: list[CellRequest], on_event: OnEvent
    ) -> list[CellExecution]: ...

    def health(self, handle: RuntimeHandle) -> RuntimeHealth: ...

    def cancel(self, run: Run) -> bool: ...

    def stop(self, handle: RuntimeHandle, ctx: RunContext) -> None: ...
