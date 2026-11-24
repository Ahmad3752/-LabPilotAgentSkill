"""Input providers: where a lab manual and its files come from.

v1 implements only LocalFolderInputProvider. A future LMS provider (through an
authorised MCP) or messaging front-end would return the same RunRequest of
locally staged files with provenance, so the notebook, evidence and Word
pipeline need no changes. Nothing here talks to an LMS or messaging service.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from labpilot.datasets.discover import list_candidate_files
from labpilot.errors import ErrorCode, LabPilotError
from labpilot.models import InputManifest, RunRequest


class InputProvider(Protocol):
    """Turns a user-authorised assignment reference into locally staged files plus provenance."""

    name: str

    def fetch(self, reference: str, staging_dir: Path) -> RunRequest: ...


class LocalFolderInputProvider:
    name = "local-folder"

    def fetch(self, reference: str, staging_dir: Path | None = None) -> RunRequest:
        path = Path(reference).expanduser().resolve()
        folder = path.parent if path.is_file() else path
        if path.is_file():
            manual = path
        else:
            docs = sorted(p for p in folder.glob("*.docx") if not p.name.startswith("~$"))
            if len(docs) != 1:
                raise LabPilotError(
                    ErrorCode.DATASET_AMBIGUOUS if docs else ErrorCode.DOCX_UNSUPPORTED,
                    f"Expected exactly one .docx manual in {folder.name}, found {len(docs)}; pass the file explicitly.",
                )
            manual = docs[0]
        files = [p for p in list_candidate_files(folder) if p.suffix.lower() != ".docx"]
        return RunRequest(
            inputs=InputManifest(
                manual_path=str(manual),
                dataset_paths=[str(p) for p in files],
                provenance={"provider": self.name, "folder": folder.name},
            )
        )
