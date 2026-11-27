"""Structural validation of the written Word copy, plus optional rendered-page validation."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import docx
from docx.oxml.ns import qn

from labpilot.documents.anchors import W_P, element_text, normalize
from labpilot.security import check_docx_container
from labpilot.workspace import sha256_file


def _paragraph_texts(path: Path) -> list[str]:
    d = docx.Document(str(path))
    return [normalize(element_text(p)) for p in d.element.body.iter(W_P)]


def validate_docx(
    *,
    source: Path,
    source_sha256: str,
    output: Path,
    expected_images: list[dict[str, Any]],
    removed_placeholders: list[dict[str, str]],
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    problems: list[str] = []

    checks["source_unchanged"] = sha256_file(source) == source_sha256
    if not checks["source_unchanged"]:
        problems.append("original manual changed on disk during the run")

    try:
        check_docx_container(output)
        out_doc = docx.Document(str(output))
        checks["opens"] = True
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "checks": {"opens": False}, "problems": [f"output does not open: {exc}"]}

    # every image reference in the body resolves to an embedded part
    part = out_doc.part
    blip_rids = [b.get(qn("r:embed")) for b in out_doc.element.body.iter(qn("a:blip"))]
    unresolved = [rid for rid in blip_rids if rid not in part.rels or part.rels[rid].is_external]
    checks["image_relationships_resolve"] = not unresolved
    if unresolved:
        problems.append(f"unresolved image relationships: {unresolved}")

    # every inserted screenshot is embedded byte-for-byte, exactly once
    embedded: dict[str, int] = {}
    for rid in blip_rids:
        if rid in part.rels and not part.rels[rid].is_external:
            digest = hashlib.sha256(part.rels[rid].target_part.blob).hexdigest()
            embedded[digest] = embedded.get(digest, 0) + 1
    missing = [i["path"] for i in expected_images if embedded.get(i["sha256"], 0) < 1]
    checks["all_screenshots_embedded"] = not missing
    if missing:
        problems.append(f"screenshots not embedded: {missing}")
    src_doc = docx.Document(str(source))
    src_counts: dict[str, int] = {}
    for b in src_doc.element.body.iter(qn("a:blip")):
        rid = b.get(qn("r:embed"))
        if rid in src_doc.part.rels and not src_doc.part.rels[rid].is_external:
            dg = hashlib.sha256(src_doc.part.rels[rid].target_part.blob).hexdigest()
            src_counts[dg] = src_counts.get(dg, 0) + 1
    dupes = [i["path"] for i in expected_images if embedded.get(i["sha256"], 0) - src_counts.get(i["sha256"], 0) > 1]
    checks["no_duplicate_insertions"] = not dupes
    if dupes:
        problems.append(f"screenshots inserted more than once: {dupes}")
    lost_src_images = [d for d, n in src_counts.items() if embedded.get(d, 0) < n]
    checks["original_images_preserved"] = not lost_src_images
    if lost_src_images:
        problems.append(f"{len(lost_src_images)} original image(s) missing from the output")

    # original text preserved in order (except placeholders deliberately removed)
    removed = [normalize(r["text"]) for r in removed_placeholders]
    src_texts = [t for t in _paragraph_texts(source) if t]
    out_texts = [t for t in _paragraph_texts(output) if t]
    j = 0
    lost: list[str] = []
    for t in src_texts:
        if t in removed:
            removed.remove(t)
            continue
        k = j
        while k < len(out_texts) and out_texts[k] != t and not out_texts[k].startswith(t):
            k += 1
        if k == len(out_texts):
            lost.append(t[:80])
        else:
            j = k + 1
    checks["original_text_preserved"] = not lost
    if lost:
        problems.append(f"{len(lost)} original paragraph(s) missing or reordered, e.g. {lost[:3]}")

    # headers/footers and section settings survive
    src_z, out_z = zipfile.ZipFile(source), zipfile.ZipFile(output)
    src_hf = sorted(n for n in src_z.namelist() if n.startswith(("word/header", "word/footer")))
    out_hf = sorted(n for n in out_z.namelist() if n.startswith(("word/header", "word/footer")))
    # compare the parsed XML (canonical form), not raw bytes: python-docx re-serialises these parts on save,
    # which changes only the XML declaration quoting and line endings
    checks["headers_footers_preserved"] = set(src_hf) <= set(out_hf) and all(
        _canonical(out_z.read(n)) == _canonical(src_z.read(n)) for n in src_hf
    )
    src_z.close()
    out_z.close()
    if not checks["headers_footers_preserved"]:
        problems.append("headers/footers changed")
    sec_src = [(s.page_width, s.page_height, s.left_margin, s.right_margin) for s in src_doc.sections]
    sec_out = [(s.page_width, s.page_height, s.left_margin, s.right_margin) for s in out_doc.sections]
    checks["page_setup_preserved"] = sec_src == sec_out
    if sec_src != sec_out:
        problems.append("page setup changed")

    return {"ok": not problems, "checks": checks, "problems": problems}


def _canonical(blob: bytes) -> bytes:
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    return etree.tostring(etree.fromstring(blob, parser), method="c14n")


def find_renderer() -> str | None:
    for name in ("soffice", "libreoffice"):
        exe = shutil.which(name)
        if exe:
            return exe
    for candidate in (
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


WORD_EXPORT_PS1 = r"""
$ErrorActionPreference = 'Stop'
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
try {
  $doc = $word.Documents.Open($In, $false, $true, $false)  # ConfirmConversions, ReadOnly, AddToRecentFiles
  $doc.ExportAsFixedFormat($Out, 17)                         # wdExportFormatPDF
  $doc.Close(0)                                              # wdDoNotSaveChanges
} finally { $word.Quit() }
"""


def find_word() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application")
        return True
    except OSError:
        return False


def _to_pdf(docx_path: Path, out_dir: Path, timeout: int) -> tuple[Path | None, str, str]:
    pdf = out_dir / (docx_path.stem + ".pdf")
    pdf.unlink(missing_ok=True)
    exe = find_renderer()
    if exe:
        proc = subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
                              capture_output=True, text=True, timeout=timeout)
        return (pdf if pdf.exists() else None), "libreoffice", (proc.stderr or proc.stdout)[-400:]
    if find_word():
        import base64

        def lit(p: Path) -> str:  # PowerShell single-quoted literal (no interpolation)
            return "'" + str(p.resolve()).replace("'", "''") + "'"

        script = f"$In = {lit(docx_path)}\n$Out = {lit(pdf)}\n" + WORD_EXPORT_PS1
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                              capture_output=True, text=True, timeout=timeout)
        return (pdf if pdf.exists() else None), "microsoft-word", (proc.stderr or proc.stdout)[-400:]
    return None, "", ""


def render_pages(docx_path: Path, out_dir: Path, timeout: int = 240) -> dict[str, Any]:
    """Optional visual check: render to PDF (LibreOffice or installed Word) and rasterize pages to PNG.

    Never mandatory; when no renderer exists the limitation is reported separately from structural checks.
    """
    if not find_renderer() and not find_word():
        return {"available": False, "reason": "no local renderer (LibreOffice or Microsoft Word); page check skipped"}
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        pdf, renderer, log = _to_pdf(docx_path, out_dir, timeout)
    except subprocess.TimeoutExpired:
        return {"available": True, "ok": False, "reason": "renderer timed out"}
    if pdf is None:
        return {"available": True, "ok": False, "renderer": renderer, "reason": log}
    import pypdfium2 as pdfium

    from labpilot.evidence.validate import image_problems

    pages_dir = out_dir / "pages"
    pages_dir.mkdir(exist_ok=True)
    for old in pages_dir.glob("page-*.png"):  # a shorter re-export must not leave stale pages behind
        old.unlink()
    doc = pdfium.PdfDocument(str(pdf))
    pages = []
    try:
        for i in range(len(doc)):
            dest = pages_dir / f"page-{i + 1:02d}.png"
            doc[i].render(scale=1.3).to_pil().save(dest)
            probs, _, _ = image_problems(dest)
            pages.append({"page": i + 1, "png": f"pages/{dest.name}", "blank": bool(probs)})
    finally:
        doc.close()
    return {"available": True, "ok": True, "renderer": renderer, "pdf": pdf.name, "pages": len(pages),
            "pages_dir": str(pages_dir.resolve()), "page_images": pages,
            "blank_pages": [p["page"] for p in pages if p["blank"]]}
