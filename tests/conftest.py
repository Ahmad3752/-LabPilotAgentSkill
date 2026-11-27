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
    monkeypatch.setenv("LABPILOT_ALLOW_LOCAL", "1")  # lab code runs on Colab; the local kernel is for tests only
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


def fake_colab_exec(path: Path, outputs_for: Any) -> list[str]:
    """FAKE `colab exec -f path` for unit tests: writes <stem>_output.ipynb like google-colab-cli 0.8.0.

    It also imitates LabPilot's kernel-side gate (the real gate runs in IPython, see
    tests/integration/test_colab_gate.py): after a lab cell with an error output, later lab cells get
    no outputs. `outputs_for(cell)` gives a cell's outputs; returns the ids of the lab cells it "ran".
    """
    import nbformat
    from nbformat.v4 import new_output

    nb = nbformat.read(str(path), as_version=4)
    outcome, ran = "open", []
    for cell in nb.cells:
        if "_labpilot_control(" in cell.source:
            outs = [{"output_type": "stream", "name": "stdout", "text": f"LABPILOT_GATE {outcome} 2026-01-01T00:00:00.000000Z\n"}]
        elif outcome in ("failed", "skipped"):
            outs, outcome = [], "skipped"
        else:
            outs = outputs_for(cell)
            ran.append(cell.id)
            outcome = "failed" if any(o["output_type"] == "error" for o in outs) else "passed"
        cell.outputs = [new_output(**o) for o in outs]
    nbformat.write(nb, str(path.with_name(path.stem + "_output.ipynb")))
    return ran
