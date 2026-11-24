"""Release-gate end-to-end run of the synthetic Colab-style sample lab (no LLM, no credentials)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import zipfile
from pathlib import Path

import docx
import pytest

from conftest import ROOT
from labpilot.workspace import sha256_file

pytestmark = pytest.mark.e2e


def _demo():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("run_demo", ROOT / "examples" / "run_demo.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def _check_artifacts(result: dict) -> dict:
    exp = result["export"]
    assert exp["status"] == "COMPLETED", exp["failed_checks"]
    out = Path(exp["output_dir"])
    names = {p.name for p in out.iterdir()}
    assert {"Lab_3_Completed.docx", "Lab_3.ipynb", "REVIEW.md", "execution_report.json",
            "evidence_manifest.json", "screenshots"} <= names
    report = json.loads((out / "execution_report.json").read_text(encoding="utf-8"))
    assert report["source"]["unchanged"] is True
    assert all(t["status"] == "passed" for t in report["tasks"]) and len(report["tasks"]) == 5
    assert all(c["ok"] for c in report["validation"]["checks"].values())
    ev = json.loads((out / "evidence_manifest.json").read_text(encoding="utf-8"))["records"]
    # every embedded screenshot is byte-identical to an evidence record from the current attempt
    with zipfile.ZipFile(out / "Lab_3_Completed.docx") as zf:
        media = {hashlib.sha256(zf.read(n)).hexdigest() for n in zf.namelist() if n.startswith("word/media/")}
    assert {r["sha256"] for r in ev} <= media
    assert all(sha256_file(out / r["path"]) == r["sha256"] for r in ev)
    nb = json.loads((out / "Lab_3.ipynb").read_text(encoding="utf-8"))
    assert all(c.get("outputs") for c in nb["cells"] if c["cell_type"] == "code")
    d = docx.Document(str(out / "Lab_3_Completed.docx"))
    assert len(d.inline_shapes) == len(ev) + 1  # + the original logo
    return report


def test_sample_lab_end_to_end_local_fallback(tmp_path: Path) -> None:
    report = _check_artifacts(_demo().run(tmp_path / "lab", "local"))
    assert report["backend"]["executed_on"] == "local-jupyter"


@pytest.mark.cloud
@pytest.mark.skipif(os.environ.get("LABPILOT_COLAB_SMOKE") != "1",
                    reason="opt-in: set LABPILOT_COLAB_SMOKE=1 (uses your Google Colab account)")
def test_sample_lab_end_to_end_on_google_colab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("LABPILOT_REAL_CONFIG_DIR"):
        monkeypatch.setenv("LABPILOT_CONFIG_DIR", os.environ["LABPILOT_REAL_CONFIG_DIR"])
    report = _check_artifacts(_demo().run(tmp_path / "lab", "colab-cli"))
    assert report["backend"]["executed_on"] == "colab-cli"
    assert report["backend"]["runtime"].get("google_colab") is True
