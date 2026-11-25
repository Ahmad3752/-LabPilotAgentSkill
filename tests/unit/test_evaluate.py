from __future__ import annotations

from pathlib import Path

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
