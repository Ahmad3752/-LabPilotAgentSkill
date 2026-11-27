from __future__ import annotations

from pathlib import Path

import pytest

from labpilot.evaluate import evaluate
from labpilot.models import ItemStatus, RunManifest
from labpilot.workspace import atomic_write_json, create_run


def _run(tmp_path: Path, answer_text: str, stdout: str, filled: bool, sections: list[str]):  # noqa: ANN202
    m = RunManifest(run_id="20260101-000000-eeeeee", lab_dir=str(tmp_path), source_name="m.docx", source_sha256="x",
                    current_attempt="a001", task_status={"T1.1": ItemStatus.PASSED},
                    cell_status={"c1": ItemStatus.PASSED})
    run = create_run(tmp_path, m)
    atomic_write_json(run.path("plan.json"), {
        "lab_title": "L", "source_sha256": "x",
        "tasks": [{"id": "T1.1", "title": "Totals", "placement": "append", "required_evidence": [],
                   "observation_questions": [{"id": "T1.1.Q1", "text": "Same result?", "kind": "observed"}]}]})
    atomic_write_json(run.path("answers.json"), {
        "answers": [{"question_id": "T1.1.Q1", "text": answer_text, "source_cell_ids": ["c1"]}],
        "table_cells": [{"anchor": "b5/r1/c1#e3b0c44298fc", "text": "28", "source_cell_ids": ["c1"]}] if filled else []})
    atomic_write_json(run.exec_dir / "current_records.json",
                      [{"cell_id": "c1", "status": "passed", "stdout": stdout, "stderr": "", "outputs": []}])
    atomic_write_json(run.path("document.json"), {
        "uncovered_mentions": sections,
        "fill_in_tables": [{"table_anchor": "b5", "empty_cells": ["b5/r1/c1#e3b0c44298fc"],
                            "cells": [{"anchor": "b5/r0/c0#a", "text": "Counter"}, {"anchor": "b5/r1/c1#e3b0c44298fc", "text": ""}]}]})
    return run


def test_clean_lab_passes(tmp_path: Path) -> None:
    run = _run(tmp_path, "Yes. Both runs printed exactly the same four regional totals, so the combiner did not "
                         "change the result.", "East 1.0\nJOB OK\n", filled=True, sections=[])
    res = evaluate(run)
    assert res["ok"], res["findings"]
    assert run.path("evaluation.json").exists()


def test_log_like_answers_hidden_errors_empty_tables_and_missing_sections_fail(tmp_path: Path) -> None:
    run = _run(tmp_path, "IDENTICAL_RESULTS = True", "cat: `/out/part-r-00000': No such file or directory\n",
               filled=False, sections=["teardown"])
    res = evaluate(run)
    checks = {(f["check"], f["severity"]) for f in res["findings"]}
    assert not res["ok"]
    assert {("answers_prose", "fail"), ("outputs_clean", "fail"), ("tables_filled", "fail"),
            ("sections_covered", "fail")} <= checks
    assert ("answers_prose", "warn") in checks  # also too short


def test_deliverables_and_submission_mentions_only_warn(tmp_path: Path) -> None:
    run = _run(tmp_path, "Yes. Both runs printed exactly the same four regional totals, so the combiner did not "
                         "change the result.", "ok", filled=True, sections=["deliverables", "submission"])
    res = evaluate(run)
    assert res["ok"], res["findings"]
    assert [(f["check"], f["severity"]) for f in res["findings"]] == [("sections_covered", "warn")] * 2


@pytest.mark.parametrize("text", [
    r"Speedup=1.600.40=4\text{Speedup}", r"The ratio is \frac{1.6}{0.4}.", "$$S = T_1 / T_4$$", r"so \(S = 4\)",
    "The serial run took 0.182433 s in total.", "This answer was written by LabPilot.",
    "The student should enter the name here.", "The identity fields remain blank.",
    "This section was generated automatically.", "Timings come from the Google Colab runtime.",
])
def test_writing_flags_text_a_student_would_not_write(tmp_path: Path, text: str) -> None:
    run = _run(tmp_path, f"Yes, both runs matched exactly in every region. {text}", "ok", filled=True, sections=[])
    res = evaluate(run)
    assert res["ok"]  # a warning to fix, never a blocker
    assert [(f["check"], f["where"]) for f in res["findings"] if f["check"] == "writing"] == [("writing", "T1.1.Q1")]


@pytest.mark.parametrize("text", [
    "We used Python 3.11.2 and PySpark 3.5.1 on the cluster.", "The serial run took 0.18 s, the parallel one 0.05 s.",
    "NumPy generated 1000 random values for the test.", "It cost $5 per hour and $6 with a GPU.",
    "The scale factor 1.23456e-05 is tiny.", r"The file C:\Users\lab\data.csv was read twice.",
    "The students compared the timings in class.",
])
def test_writing_check_ignores_normal_lab_text(tmp_path: Path, text: str) -> None:
    run = _run(tmp_path, f"Yes, both runs matched exactly in every region. {text}", "ok", filled=True, sections=[])
    assert [f for f in evaluate(run)["findings"] if f["check"] == "writing"] == []


def test_exact_value_the_manual_asks_for_is_not_flagged(tmp_path: Path) -> None:
    import json

    run = _run(tmp_path, "The estimate of pi printed by the program was 3.141592 after one million samples.", "ok",
               filled=True, sections=[])
    plan = json.loads(run.path("plan.json").read_text(encoding="utf-8"))
    plan["tasks"][0]["observation_questions"][0]["text"] = "Report pi to 6 decimal places."
    atomic_write_json(run.path("plan.json"), plan)
    assert [f for f in evaluate(run)["findings"] if f["check"] == "writing"] == []


def test_clean_keeps_deliverables_and_refuses_unfinished_runs(tmp_path: Path) -> None:
    import pytest

    from labpilot.cleanup import clean_run, ensure_not_cleaned
    from labpilot.errors import LabPilotError
    from labpilot.models import RunState

    run = _run(tmp_path, "Both runs printed the same totals, so the combiner did not change anything.", "ok",
               filled=True, sections=[])
    out = tmp_path / "output" / "Lab-x"
    out.mkdir(parents=True)
    for name in ("Lab_Completed.docx", "Lab.ipynb", "REVIEW.md"):
        (out / name).write_text("x", encoding="utf-8")
    (out / "screenshots").mkdir()
    for d in ("executions/a001", "evidence/screenshots", "render/pages", "inputs", "data", "notebook"):
        (run.dir / d).mkdir(parents=True, exist_ok=True)
        (run.dir / d / "f.bin").write_bytes(b"0" * 1000)
    run.manifest.outputs = {"dir": str(out), "docx": "Lab_Completed.docx", "notebook": "Lab.ipynb"}
    run.manifest.state = RunState.PARTIAL
    run.save()
    with pytest.raises(LabPilotError):
        clean_run(run)  # partial runs keep their workspace so they can resume
    run.manifest.state = RunState.COMPLETED
    run.save()
    res = clean_run(run)
    assert res["cleaned"] and res["freed_bytes"] >= 6000
    assert sorted(res["removed"]) == ["data", "evidence", "executions", "inputs", "notebook", "render"]
    assert sorted(p.name for p in out.iterdir()) == ["Lab.ipynb", "Lab_Completed.docx", "REVIEW.md", "screenshots"]
    assert (run.dir / "manifest.json").exists() and (run.dir / "plan.json").exists()
    with pytest.raises(LabPilotError):
        ensure_not_cleaned(run)
    assert clean_run(run)["already_clean"]
