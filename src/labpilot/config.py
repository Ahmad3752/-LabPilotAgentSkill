"""User-level configuration (non-sensitive choices only) and runtime profiles."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from labpilot.errors import ErrorCode, ExitCode, LabPilotError

APP = "labpilot"


def user_config_dir() -> Path:
    override = os.environ.get("LABPILOT_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP


def config_path() -> Path:
    return user_config_dir() / "config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "backend": "auto",  # auto | local | colab-cli | colab-mcp
    "agents": [],
    "colab_cli": {"wsl_distro": None, "command": None},
    "aliases": {},  # expected dataset name -> actual file name
    "telemetry": False,
    "registered_mcp": [],  # MCP registrations LabPilot made (for clean uninstall)
    "installed_skills": [],  # skill directories LabPilot installed
}


def load_config() -> dict[str, Any]:
    path = config_path()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LabPilotError(
                ErrorCode.IO_ERROR,
                f"User config {path.name} is not valid JSON: {exc}",
                exit_code=ExitCode.CONFIG,
            ) from exc
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    from labpilot.workspace import atomic_write_json

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, cfg)
    return path


# ------------------------------------------------------------------ profiles


@dataclass
class RuntimeProfile:
    name: str
    description: str
    import_checks: list[str]
    python_packages: list[str]
    cell_timeout_s: int = 600
    raw: dict[str, Any] = field(default_factory=dict)


def profiles_dir() -> Path:
    return Path(str(resources.files("labpilot") / "profiles"))


def load_profile(name: str) -> RuntimeProfile:
    path = profiles_dir() / f"{name}.toml"
    if not path.exists():
        available = sorted(p.stem for p in profiles_dir().glob("*.toml"))
        raise LabPilotError(
            ErrorCode.PROFILE_PREREQ_MISSING,
            f"Unknown runtime profile '{name}'. Tested profiles: {', '.join(available)}",
            exit_code=ExitCode.CONFIG,
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return RuntimeProfile(
        name=data["name"],
        description=data.get("description", ""),
        import_checks=data.get("preflight", {}).get("imports", []),
        python_packages=data.get("python", {}).get("packages", []),
        cell_timeout_s=data.get("runtime", {}).get("cell_timeout_s", 600),
        raw=data,
    )
