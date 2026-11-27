from __future__ import annotations

from pathlib import Path

import docx
import pytest
from docx.shared import Pt

from labpilot.documents.anchors import element_text, fingerprint, resolve
from labpilot.documents.inspect import colab_features, colab_fit, inspect_docx
from labpilot.errors import LabPilotError
from labpilot.workspace import sha256_file


def _nested_doc(path: Path) -> Path:
    d = docx.Document()
    d.add_paragraph("Intro paragraph")
    t = d.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text = "outer r0c0"
    inner = t.rows[1].cells[1].add_table(rows=1, cols=1)
    inner.rows[0].cells[0].text = "deep text"
    d.add_paragraph("After table")
    r = d.add_paragraph().add_run("x = 1")
    r.font.name = "Consolas"
    r.font.size = Pt(9)
    d.save(str(path))
    return path


def test_reading_order_nested_tables_and_anchor_resolution(tmp_path: Path) -> None:
    p = _nested_doc(tmp_path / "n.docx")
    insp = inspect_docx(p, sha256_file(p))
    texts = [(b.anchor, b.text) for b in insp.blocks if b.text]
    order = [t for _, t in texts]
    assert order.index("Intro paragraph") < order.index("outer r0c0") < order.index("deep text") < order.index("After table")
    deep = next(b for b in insp.blocks if b.text == "deep text" and b.kind == "paragraph")
    assert deep.anchor == "b1/r1/c1/b1/r0/c0/b0"  # nested table inside cell r1c1 (cell keeps an empty first paragraph)
    body = docx.Document(str(p)).element.body
    el = resolve(body, f"{deep.anchor}#{fingerprint('deep text')}")
    assert element_text(el) == "deep text"
    with pytest.raises(LabPilotError):
        resolve(body, f"{deep.anchor}#000000000000")
    with pytest.raises(LabPilotError):
        resolve(body, "b99")
    assert any("x = 1" in c["code"] for c in insp.code_blocks)


def test_sample_python_lab_structure(python_lab: Path) -> None:
    p = python_lab / "Lab 3.docx"
    insp = inspect_docx(p, sha256_file(p), tmp_path_images := python_lab / "imgs")
    assert insp.title_candidates[0] == "Lab 3: Data Analysis with Pandas"
    assert [t["id"] for t in insp.tasks] == ["T1", "T2"]
    assert [p_["id"] for t in insp.tasks for p_ in t["parts"]] == ["T1.A", "T1.B", "T2.A", "T2.B"]
    assert len(insp.questions) == 6
    assert [b["task_hint"] for b in insp.solution_boxes] == ["T1", "T2"]
    assert all("Paste your code" in b["placeholder"] for b in insp.solution_boxes)
    assert {f["name"] for f in insp.file_references} == {"sales.csv", "regions.csv"}
    assert [s["label"] for s in insp.student_fields] == ["Name", "CMS ID"]
    assert len(insp.images) == 1 and (tmp_path_images / "image_01.png").exists()
    assert insp.environment.get("colab") == ["yes"]


def test_colab_specific_starter_code_is_flagged(python_lab: Path, tmp_path: Path) -> None:
    p = python_lab / "Lab 3.docx"
    features = {f["feature"]: f for f in colab_features(inspect_docx(p, sha256_file(p)))}
    assert set(features) == {"interactive-upload", "content-paths", "setup-commands"}
    assert "stages the files" in features["interactive-upload"]["advice"]

    d = docx.Document()
    r = d.add_paragraph().add_run("from google.colab import drive\ndrive.mount('/content/drive')")
    r.font.name = "Consolas"
    q = tmp_path / "drive.docx"
    d.save(str(q))
    assert "drive-mount" in {f["feature"] for f in colab_features(inspect_docx(q, sha256_file(q)))}


@pytest.mark.parametrize(("text", "ok", "blocker"), [
    # written for Colab: an incidental mention (a Hadoop lab skipping the "AWS bundle") does not block it
    ("Open Google Colab. !wget hadoop.tar.gz; skip the unused AWS bundle; cd /content/hadoop", True, None),
    ("Start Kafka with docker run, open tmux terminals A to D on Killercoda", False, "Docker containers"),
    ("Lab 5: VPC Networking, EC2, and S3 on AWS. Connect with ssh.", False, "a cloud provider console (AWS, Azure or GCP)"),
    ("Write public static void main in Eclipse and compile with javac", False, "Java programs"),
    ("Use Python.\nimport pandas as pd\nfrom math import sqrt", True, None),  # Python lab, Colab not named
    ("Draw an ER diagram for the library database.", False, None),  # not a Python notebook lab
])
def test_colab_fit(text: str, ok: bool, blocker: str | None) -> None:
    fit = colab_fit(text)
    assert fit["colab_ok"] is ok and (blocker is None or blocker in fit["blockers"])


def test_sample_lab_is_colab_ok(python_lab: Path) -> None:
    manual = next(python_lab.glob("*.docx"))
    text = "\n".join(b.text for b in inspect_docx(manual, sha256_file(manual)).blocks)
    assert colab_fit(text)["colab_ok"] is True


def test_word_equations_are_read_as_plain_text_and_anchors_stay_stable(tmp_path: Path) -> None:
    from lxml import etree

    m = "http://schemas.openxmlformats.org/officeDocument/2006/math"
    d = docx.Document()
    p = d.add_paragraph("Compute the speedup ")
    omath = etree.SubElement(p._p, f"{{{m}}}oMath")
    for chunk in ("S", "=", "T1/T4"):
        etree.SubElement(etree.SubElement(omath, f"{{{m}}}r"), f"{{{m}}}t").text = chunk
    d.add_paragraph("Plain paragraph")
    path = tmp_path / "math.docx"
    d.save(str(path))
    insp = inspect_docx(path, sha256_file(path))
    eq = next(b for b in insp.blocks if b.text.startswith("Compute"))
    assert eq.text == "Compute the speedup S=T1/T4"  # the formula is visible to the agent, no LaTeX
    assert eq.fp == fingerprint("Compute the speedup")  # same anchor as before equations were read
    plain = next(b for b in insp.blocks if b.text == "Plain paragraph")
    assert plain.fp == fingerprint("Plain paragraph")
    body = docx.Document(str(path)).element.body
    assert element_text(resolve(body, f"{eq.anchor}#{eq.fp}")) == eq.text
