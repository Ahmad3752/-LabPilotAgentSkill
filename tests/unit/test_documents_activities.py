"""Manuals split into "Task N" sections with "Activity N.M" children and "Q1." questions without a "?".
Synthetic content only."""

from __future__ import annotations

from pathlib import Path

import docx
import pytest

from labpilot.documents.inspect import _file_role, colab_fit, draft_plan, inspect_docx
from labpilot.documents.write import display_heading
from labpilot.models import Plan, Task
from labpilot.workspace import sha256_file


def _bold(d: docx.document.Document, text: str) -> None:
    d.add_paragraph().add_run(text).bold = True


def _code(d: docx.document.Document, text: str) -> None:
    d.add_paragraph().add_run(text).font.name = "Consolas"


def _solution_box(d: docx.document.Document, task: int) -> None:
    t = d.add_table(rows=2, cols=1)
    t.rows[0].cells[0].text = "Solution"
    t.rows[1].cells[0].text = f"Task {task} – Code, Output Screenshots & Answers"


def _manual(path: Path) -> Path:
    d = docx.Document()
    _bold(d, "Lab 4: Synthetic Pipelines")
    _bold(d, "Task 1 – Build a pipeline")
    d.add_paragraph("This task joins the pieces together.")
    _bold(d, "Activity 1.1 – Run the pipeline")
    _code(d, 'df = pd.read_csv("raw_orders.csv")\ndf.to_csv("clean_orders.csv", index=False)')
    d.add_paragraph("Q1. Modify the validation so bad rows are dropped.")
    d.add_paragraph("Q2. Add a discount column.")
    d.add_paragraph("Checkpoint: clean_orders.csv is created.")
    _solution_box(d, 1)
    _bold(d, "Task 2 – Storage")
    _bold(d, "Activity 2.1 – Part A: Create the database")
    _code(d, 'con = sqlite3.connect("shop.db")\ncon.execute("CREATE TABLE items (id INTEGER)")')
    _bold(d, "Activity 2.2 – Part B: Plot")
    _code(d, 'plt.savefig("chart.png")')
    _code(d, "Q1: Explain the query plan.")  # a question typed in a code font is still a question
    d.add_paragraph("Q2) Compare the two runs and justify the choice.")
    _solution_box(d, 2)
    d.save(str(path))
    return path


def test_activities_are_children_with_unique_ids(tmp_path: Path) -> None:
    p = _manual(tmp_path / "m.docx")
    insp = inspect_docx(p, sha256_file(p))
    assert [t["id"] for t in insp.tasks] == ["T1", "T2"]
    assert [(u["id"], u["kind"], u["number"]) for t in insp.tasks for u in t["parts"]] == [
        ("T1-A1", "activity", "1.1"), ("T2-A1", "activity", "2.1"), ("T2-A2", "activity", "2.2")]
    assert insp.tasks[1]["parts"][0]["title"] == "Part A: Create the database"
    assert [q["id"] for q in insp.questions] == ["T1-A1.Q1", "T1-A1.Q2", "T2-A2.Q1", "T2-A2.Q2"]
    assert not any("Q1: Explain" in c["code"] for c in insp.code_blocks)
    assert [(b["task_hint"], b["scope"]) for b in insp.solution_boxes] == [("T1", "task"), ("T2", "task")]
    assert not [w for w in insp.warnings if "repeats" in w or "numbered for" in w]

    plan = Plan.model_validate(draft_plan(insp, []))
    by_id = {t.id: t for t in plan.tasks}
    assert by_id["T2-A1"].parent == "T2"
    assert by_id["T2-A1"].solution_anchor == by_id["T2-A2"].solution_anchor == insp.solution_boxes[1]["fill_anchor"]


def test_generated_files_are_not_required_inputs(tmp_path: Path) -> None:
    p = _manual(tmp_path / "m.docx")
    insp = inspect_docx(p, sha256_file(p))
    assert [f["name"] for f in insp.file_references] == ["raw_orders.csv"]
    assert {f["name"] for f in insp.generated_files} == {"clean_orders.csv", "shop.db", "chart.png"}


@pytest.mark.parametrize(("code", "role"), [
    ('df.to_parquet("out/x.parquet")', "output"),
    ('pd.read_csv("x.csv")\ndf.to_csv("x.csv")', "input"),  # read before it is written: must be supplied
    ('with open("x.txt", "w") as f: f.write("a")', "output"),
    ('with open("x.txt") as f: print(f.read())', "input"),
    ('df.to_excel("x.xlsx")', "output"),
    ('sqlite3.connect("x.db")', "input"),  # an existing database: nothing in the lab creates its tables
])
def test_file_role(code: str, role: str) -> None:
    name = code.split('"')[1].split("/")[-1]
    assert _file_role(name, code) == role


def test_repeated_and_misnumbered_headings_warn(tmp_path: Path) -> None:
    d = docx.Document()
    _bold(d, "Task 1: First")
    _bold(d, "Activity 3.1: Wrong task number")
    _bold(d, "Task 1: First again")
    q = tmp_path / "dup.docx"
    d.save(str(q))
    insp = inspect_docx(q, sha256_file(q))
    assert [t["id"] for t in insp.tasks] == ["T1", "T1-dup2"]
    assert [u["id"] for u in insp.tasks[0]["parts"]] == ["T3-A1"]
    assert any("numbered for task 3" in w for w in insp.warnings)
    assert any("repeats T1" in w for w in insp.warnings)


@pytest.mark.parametrize(("tid", "title", "heading"), [
    ("T2", "Task 2 – Scalability and Performance", "Task 2: Scalability and Performance"),
    ("T2", "task 2: Scalability", "Task 2: Scalability"),
    ("T2", "Scalability", "Task 2: Scalability"),
    ("T2", "Task 20 notes", "Task 2: Task 20 notes"),
    ("T1.A", "Part A - Load", "Task 1, Part A: Load"),
    ("T5-A1", "Activity 5.1 — Hashing", "Task 5, Activity 5.1: Hashing"),
    ("T5-A1", "Part A: Hashing", "Task 5, Activity 5.1: Part A: Hashing"),
    ("T3.2", "Question 3.2: Add a combiner", "Question 3.2: Add a combiner"),
])
def test_display_heading_does_not_repeat_the_label(tid: str, title: str, heading: str) -> None:
    assert display_heading(Task(id=tid, title=title)) == heading


def test_colab_fit_sql_server_and_interactive_calls() -> None:
    lab1 = "Python 3 with Google Colab\nSQL Server Express (local installation)\nSQL Server Management Studio (SSMS)"
    fit = colab_fit(lab1)
    assert fit["colab_ok"] is False and "Microsoft SQL Server and SSMS" in fit["blockers"]
    lab2 = "Google Colab\nimport sqlite3\nconnection = sqlite3.connect('lab.db')\npd.read_sql_query(q, connection)"
    assert colab_fit(lab2) == {"colab_ok": True, "reason": "The manual is written for Google Colab.",
                               "blockers": [], "needs_attention": [], "gpu": None, "gpu_evidence": []}
    fit = colab_fit("Google Colab\nfrom google.colab import files, drive\nuploaded = files.upload()\ndrive.mount('/content/d')")
    assert fit["colab_ok"] is True and len(fit["needs_attention"]) == 2
    assert all("upload" in n and "directly" in n for n in fit["needs_attention"])


@pytest.mark.parametrize(("text", "level"), [
    ("Google Colab\nRuntime > Change runtime type > T4 GPU", "needed"),  # Colab setup step
    ("model = model.to('cuda')", "needed"),
    ("device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')", "needed"),
    ("Scaling laws: larger models need more GPUs to train.", "maybe"),  # theory only: never starts on a GPU
    ("from transformers import pipeline", "maybe"),  # ML library: permission to switch if needed
    ("This lab does not need a GPU; the CPU runtime is enough.", None),
    ("You can run this without a GPU.\nimport pandas as pd", None),
    ("import pandas as pd\ndf.describe()", None),
])
def test_gpu_need_strong_weak_and_negated(text: str, level: str | None) -> None:
    fit = colab_fit(text)
    assert fit["gpu"] == level
    assert bool(fit["gpu_evidence"]) == (level == "needed" or "GPU" in text and level is not None)
