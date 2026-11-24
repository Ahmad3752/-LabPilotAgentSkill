from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
sys.path.insert(0, str(EXAMPLES))


def load_example(kind: str, module: str) -> Any:
    path = EXAMPLES / f"sample_{kind}_lab" / f"{module}.py"
    spec = importlib.util.spec_from_file_location(f"{kind}_{module}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path_factory.mktemp("labpilot-config")
    monkeypatch.setenv("LABPILOT_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("LABPILOT_PROGRESS", "0")
    return cfg


@pytest.fixture
def python_lab(tmp_path: Path) -> Path:
    folder = tmp_path / "lab"
    load_example("python", "make_lab").make(folder)
    return folder


def cli(folder: Path, *args: str) -> dict[str, Any]:
    """Run the real CLI as a subprocess (as an agent would) and parse its JSON result."""
    proc = subprocess.run([sys.executable, "-m", "labpilot", *args, "--json"], cwd=folder, capture_output=True,
                          text=True, encoding="utf-8")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"non-JSON stdout for {args}: {proc.stdout}\n{proc.stderr}") from exc
    data["_exit"] = proc.returncode
    data["_stderr"] = proc.stderr
    return data
