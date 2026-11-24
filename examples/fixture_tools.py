"""Helpers to generate SYNTHETIC lab manuals that look like real university lab handouts.

Everything produced here is synthetic and safe to publish. Used by tests and demos.
"""

from __future__ import annotations

import io
from pathlib import Path

from docx import Document
from docx.enum.table import WD_ROW_HEIGHT_RULE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from PIL import Image, ImageDraw


def logo_png() -> bytes:
    img = Image.new("RGB", (360, 120), "white")
    d = ImageDraw.Draw(img)
    d.ellipse((10, 10, 110, 110), outline=(20, 60, 140), width=8)
    d.rectangle((40, 40, 80, 80), fill=(20, 60, 140))
    d.text((130, 45), "SYNTHETIC UNIVERSITY", fill=(20, 60, 140))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def new_manual(title: str, course: str) -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Inches(1)
    sec.header.paragraphs[0].text = f"{course} — Synthetic Department of Computing"
    sec.footer.paragraphs[0].text = "Synthetic fixture for LabPilot tests. Not a real course document."
    doc.add_picture(io.BytesIO(logo_png()), width=Inches(2.5))
    doc.add_paragraph(title, style="Title")
    doc.add_paragraph(course)
    doc.add_paragraph("Name: ____________________")
    doc.add_paragraph("CMS ID: ____________________")
    return doc


def code(doc: Document, text: str) -> None:
    for line in text.strip("\n").splitlines():
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        r = p.add_run(line if line else " ")
        r.font.name = "Consolas"
        r.font.size = Pt(9.5)
        rpr = r._element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        fonts.set(qn("w:ascii"), "Consolas")
        fonts.set(qn("w:hAnsi"), "Consolas")


def solution_box(doc: Document, label: str, placeholder: str = "Paste your code and output screenshots here.") -> None:
    table = doc.add_table(rows=2, cols=1)
    table.style = "Table Grid"
    head = table.rows[0].cells[0].paragraphs[0].add_run(label)
    head.bold = True
    head.font.color.rgb = RGBColor(0x1F, 0x3B, 0x73)
    table.rows[1].cells[0].paragraphs[0].add_run(placeholder).italic = True
    row = table.rows[1]
    row.height = Inches(1.6)
    row.height_rule = WD_ROW_HEIGHT_RULE.EXACTLY  # typical template: fixed box that would clip content
    trpr = row._tr.get_or_add_trPr()
    trpr.append(OxmlElement("w:cantSplit"))


def nested_columns_table(doc: Document, title: str, rows: list[tuple[str, str]]) -> None:
    outer = doc.add_table(rows=1, cols=2)
    outer.style = "Table Grid"
    outer.rows[0].cells[0].text = title
    inner_cell = outer.rows[0].cells[1]
    inner = inner_cell.add_table(rows=len(rows) + 1, cols=2)
    inner.style = "Table Grid"
    inner.rows[0].cells[0].text = "Column"
    inner.rows[0].cells[1].text = "Meaning"
    for i, (a, b) in enumerate(rows, start=1):
        inner.rows[i].cells[0].text = a
        inner.rows[i].cells[1].text = b


def save(doc: Document, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path
