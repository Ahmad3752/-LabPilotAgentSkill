"""Reproducible end-to-end demo WITHOUT an LLM: drives the real CLI with known agent files.

    python examples/run_demo.py <empty-folder>                       # local fallback kernel
    python examples/run_demo.py <empty-folder> --backend colab-cli   # real Google Colab (your account)

It generates the SYNTHETIC Colab-style sample lab, then performs exactly the CLI calls
the skill tells an agent to make. Every output, screenshot and answer comes from real execution.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LAB = HERE / "sample_python_lab"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cli(folder: Path, *args: str, check: bool = True) -> dict[str, Any]:
    proc = subprocess.run([sys.executable, "-m", "labpilot", *args, "--json"], cwd=folder, capture_output=True,
                          text=True, encoding="utf-8")
    sys.stderr.write(proc.stderr)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"labpilot {args[0]} printed non-JSON output:\n{proc.stdout}") from None
    result["_exit"] = proc.returncode
    if check and not result.get("ok"):
        raise SystemExit(f"labpilot {args[0]} failed (exit {proc.returncode}): {json.dumps(result, indent=2)}")
    return result


def run(folder: Path, backend: str = "local") -> dict[str, Any]:
    make = _load(LAB / "make_lab.py", "make_lab")
    agent = _load(LAB / "agent_files.py", "agent_files")
    manual = make.make(folder)

    init = cli(folder, "init", "--lab", manual.name, "--synthetic")
    rid = init["run_id"]
    run_dir = Path(init["run_dir"])
    draft = json.loads((run_dir / "draft_plan.json").read_text(encoding="utf-8"))
    (folder / "plan.json").write_text(json.dumps(agent.make_plan(draft), indent=2), encoding="utf-8")
    cli(folder, "plan", rid, "--file", "plan.json")
    (folder / "cells.json").write_text(json.dumps(agent.CELLS, indent=2), encoding="utf-8")
    cli(folder, "build-notebook", rid, "--file", "cells.json")
    pf_args = ["preflight", rid, "--backend", backend]
    if backend != "local":
        pf_args += ["--allow-cloud", "--allow-upload"]  # the person running the demo consents explicitly
    cli(folder, *pf_args)
    ex = cli(folder, "execute", rid, check=False)
    if ex.get("status") != "OK":
        raise SystemExit(f"execution did not pass: {json.dumps(ex, indent=2)[:4000]}")
    cap = cli(folder, "capture", rid)
    answers = agent.make_answers(str(run_dir / "notebook" / "executed.ipynb"))
    (folder / "answers.json").write_text(json.dumps(answers, indent=2), encoding="utf-8")
    cli(folder, "answers", rid, "--file", "answers.json")
    exp = cli(folder, "export", rid, check=False)
    return {"run_id": rid, "capture": cap, "export": exp}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--backend", default="local", choices=["local", "colab-cli"])
    a = ap.parse_args()
    res = run(Path(a.folder).resolve(), a.backend)
    print(json.dumps(res["export"], indent=2))
