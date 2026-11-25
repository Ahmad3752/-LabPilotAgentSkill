"""Input providers: where a lab manual and its files come from.

LocalFolderInputProvider reads a folder the user prepared. LmsStagedFolderInputProvider reads a
folder the host agent filled through the Nuts LMS MCP (see `labpilot.lms`): same RunRequest, plus
LMS provenance. Both return locally staged files, so the notebook, evidence and Word pipeline
need no changes. Nothing here talks to an LMS or messaging service.
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


class LmsStagedFolderInputProvider:
    """A lab folder downloaded with the Nuts LMS MCP and recorded by `labpilot lms stage`."""

    name = "nuts-lms-mcp"

    def fetch(self, reference: str, staging_dir: Path | None = None) -> RunRequest:
        from labpilot.lms import read_source

        request = LocalFolderInputProvider().fetch(reference, staging_dir)
        folder = Path(request.inputs.manual_path).parent
        source = read_source(folder)
        if source is None:
            raise LabPilotError(
                ErrorCode.DATASET_MISSING,
                f"{folder.name} has no lms_source.json; stage it with `labpilot lms stage` first.",
            )
        request.inputs.provenance = {
            "provider": self.name,
            "folder": folder.name,
            **{k: str(source[k]) for k in ("site", "course", "courseid", "assignid", "name", "duedate")
               if source.get(k) is not None},
        }
        return request
