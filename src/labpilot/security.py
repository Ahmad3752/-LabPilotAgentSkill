"""Path safety, secret redaction, bounded archive checks and generated-code screening.

Textual screening is a speed bump, not a sandbox. Code runs either in a Google
Colab VM (isolated from this machine) or in the local fallback kernel, which runs
with a credential-scrubbed environment but the user's filesystem privileges.
"""

from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from labpilot.errors import ErrorCode, ExitCode, LabPilotError

# ---------------------------------------------------------------- paths


def safe_join(root: Path, relative: str | os.PathLike[str]) -> Path:
    """Join `relative` under `root`, rejecting traversal, absolute paths and symlink escapes."""
    rel = Path(relative)
    if rel.is_absolute() or rel.drive or any(part == ".." for part in rel.parts):
        raise LabPilotError(ErrorCode.PATH_ESCAPE, f"Path '{relative}' escapes the workspace.")
    root_resolved = root.resolve()
    candidate = (root_resolved / rel).resolve()
    if not is_within(candidate, root_resolved):
        raise LabPilotError(ErrorCode.PATH_ESCAPE, f"Path '{relative}' resolves outside the workspace.")
    return candidate


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_regular_file_within(path: Path, root: Path) -> Path:
    """Reject symlinks that point outside the approved root."""
    resolved = path.resolve()
    if not is_within(resolved, root):
        raise LabPilotError(ErrorCode.PATH_ESCAPE, f"'{path.name}' points outside the approved folder.")
    if not resolved.is_file():
        raise LabPilotError(ErrorCode.DATASET_MISSING, f"'{path.name}' is not a regular file.")
    return resolved


# ---------------------------------------------------------------- redaction

_SECRET_PATTERNS = [
    (re.compile(r"ya29\.[0-9A-Za-z_\-]{10,}"), "[REDACTED_GOOGLE_TOKEN]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)([^\s'\"]{4,})"), r"\1\2[REDACTED]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[REDACTED_EMAIL]"),
]


def _home_variants() -> list[str]:
    home = str(Path.home())
    variants = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
    return sorted((v for v in variants if len(v) > 3), key=len, reverse=True)


def redact(text: str, home: bool = True) -> str:
    """Remove obvious secrets and e-mail addresses; optionally replace the home path with '~'.

    Shareable artifacts (reports, logs) use home=True. Local machine-readable CLI results
    keep real paths (home=False) because the agent must open them.
    """
    if not text:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if home:
        for h in _home_variants():
            text = text.replace(h, "~")
    return text


def redact_obj(obj: Any, home: bool = True) -> Any:
    if isinstance(obj, str):
        return redact(obj, home)
    if isinstance(obj, dict):
        return {k: redact_obj(v, home) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, home) for v in obj]
    return obj


# ---------------------------------------------------------------- archives

MAX_DOCX_ENTRIES = 5_000
MAX_DOCX_UNCOMPRESSED = 400 * 1024 * 1024  # 400 MB
MAX_COMPRESSION_RATIO = 200
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")


def check_docx_container(path: Path) -> None:
    """Reject legacy/encrypted Word files and zip bombs before python-docx parses anything."""
    if path.suffix.lower() != ".docx":
        raise LabPilotError(
            ErrorCode.DOCX_UNSUPPORTED,
            f"'{path.name}' is not a .docx file. v1 accepts .docx only (save legacy .doc/.pdf as .docx).",
        )
    with path.open("rb") as fh:
        head = fh.read(8)
    if head == OLE_MAGIC:
        raise LabPilotError(
            ErrorCode.DOCX_ENCRYPTED,
            f"'{path.name}' is an OLE container: either a password-protected .docx or a legacy .doc. "
            "Remove the password / re-save as .docx.",
        )
    if not zipfile.is_zipfile(path):
        raise LabPilotError(ErrorCode.DOCX_CORRUPT, f"'{path.name}' is not a valid .docx (zip) package.")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_DOCX_ENTRIES:
                raise LabPilotError(ErrorCode.DOCX_CORRUPT, f"'{path.name}' has too many package entries.")
            total = 0
            for info in infos:
                total += info.file_size
                if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
                    raise LabPilotError(ErrorCode.DOCX_CORRUPT, f"'{path.name}' contains a suspicious entry.")
            if total > MAX_DOCX_UNCOMPRESSED:
                raise LabPilotError(ErrorCode.DOCX_CORRUPT, f"'{path.name}' expands beyond the size limit.")
            names = {i.filename for i in infos}
            if "word/document.xml" not in names:
                raise LabPilotError(ErrorCode.DOCX_CORRUPT, f"'{path.name}' has no word/document.xml part.")
            bad = zf.testzip()
            if bad:
                raise LabPilotError(ErrorCode.DOCX_CORRUPT, f"'{path.name}' has a corrupt entry: {bad}")
    except zipfile.BadZipFile as exc:
        raise LabPilotError(ErrorCode.DOCX_CORRUPT, f"'{path.name}' is corrupt: {exc}") from exc


# ---------------------------------------------------------------- code screening


@dataclass(frozen=True)
class CodeRule:
    id: str
    severity: str  # "deny" or "warn"
    pattern: re.Pattern[str]
    message: str


CODE_RULES: list[CodeRule] = [
    CodeRule(
        "credential-access",
        "deny",
        re.compile(
            r"(\.ssh[/\\]|\.aws[/\\]|\.codex[/\\]|\.claude[/\\]|\.config[/\\]gcloud|credentials\.json|"
            r"\.git-credentials|\.netrc|Login Data|Cookies\b|keyring\.get_password)"
        ),
        "Accesses credential or browser-profile locations.",
    ),
    CodeRule(
        "env-secrets",
        "deny",
        re.compile(r"os\.environ\s*(\.items\(\)|\.copy\(\)|\))|print\(\s*os\.environ\s*\)|dict\(os\.environ\)"),
        "Dumps the full environment (may contain secrets).",
    ),
    CodeRule(
        "interactive-upload",
        "deny",
        re.compile(r"files\.upload\s*\("),
        "Interactive google.colab files.upload(); LabPilot stages datasets non-interactively instead.",
    ),
    CodeRule(
        "interactive-input",
        "deny",
        re.compile(r"(^|[^\w.])(input|getpass(\.getpass)?)\s*\("),
        "Waits for interactive input, which would hang unattended execution.",
    ),
    CodeRule(
        "destructive-fs",
        "deny",
        # deletes of the filesystem root, home directories, system folders or a drive root; lab workspace
        # paths such as /content/<dir> (the Colab VM) or /tmp/<dir> are allowed
        re.compile(
            r"(rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/\*?(\s|$|;|&)|~|\$HOME|/(home|Users|root|etc|usr|bin|"
            r"boot|var|opt|lib|sbin)(/|\s|$)|[A-Za-z]:[\\/]?(\s|$))"
            r"|shutil\.rmtree\(\s*['\"]?(/['\"]|~|[A-Za-z]:\\\\?['\"]|/(home|Users|root|etc|usr)\b|os\.path\.expanduser)"
            r"|format\s+[A-Za-z]:)"
        ),
        "Destructive filesystem operation outside the lab workspace (root, home, system folders or a drive).",
    ),
    CodeRule(
        "drive-mount",
        "deny",
        re.compile(r"drive\.mount\s*\("),
        "Mounts Google Drive; LabPilot never mounts Drive by default.",
    ),
    CodeRule(
        "shell",
        "warn",
        re.compile(r"(^\s*!|subprocess\.|os\.system\(|os\.popen\(|%%bash|%%sh)", re.MULTILINE),
        "Runs shell commands.",
    ),
    CodeRule(
        "network",
        "warn",
        re.compile(r"(requests\.|urllib\.request|urlopen\(|socket\.|httpx\.|wget\s|curl\s|pip\s+install)"),
        "Performs network access.",
    ),
    CodeRule(
        "absolute-path",
        "warn",
        re.compile(r"['\"](/content/|/home/|/Users/|[A-Za-z]:\\\\)"),
        "Uses an absolute path; prefer paths relative to the staged data directory.",
    ),
]


@dataclass
class CodeFinding:
    cell_id: str
    rule: str
    severity: str
    message: str
    excerpt: str

    def to_dict(self) -> dict[str, str]:
        return {
            "cell_id": self.cell_id,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "excerpt": redact(self.excerpt),
        }


def scan_code(cell_id: str, source: str) -> list[CodeFinding]:
    findings: list[CodeFinding] = []
    for rule in CODE_RULES:
        match = rule.pattern.search(source)
        if match:
            line_start = source.rfind("\n", 0, match.start()) + 1
            line_end = source.find("\n", match.end())
            excerpt = source[line_start : line_end if line_end != -1 else len(source)].strip()[:160]
            findings.append(CodeFinding(cell_id, rule.id, rule.severity, rule.message, excerpt))
    return findings


def enforce_code_policy(
    findings: list[CodeFinding], approved_rules: set[str]
) -> tuple[list[CodeFinding], list[CodeFinding]]:
    """Return (denied, warnings). Denied findings not explicitly approved block the build."""
    denied = [f for f in findings if f.severity == "deny" and f.rule not in approved_rules]
    warnings = [f for f in findings if f not in denied]
    return denied, warnings


def raise_if_denied(denied: list[CodeFinding]) -> None:
    if denied:
        raise LabPilotError(
            ErrorCode.SECURITY_DENIED,
            "Generated code uses operations that LabPilot denies by default.",
            exit_code=ExitCode.INPUT,
            details={"findings": [f.to_dict() for f in denied]},
        )
