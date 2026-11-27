"""Tall code cells are captured in parts: REAL local kernel execution, REAL headless Chromium rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import nbformat
import pytest
from nbclient import NotebookClient

from labpilot.evidence.capture import VIEWPORT_W, _input_chunks, capture_notebook
from labpilot.evidence.validate import validate_evidence
from labpilot.models import ItemStatus, RunManifest
from labpilot.workspace import sha256_text

pytestmark = pytest.mark.integration

LONG = "\n".join([f"value_{i:02d} = {i} * 3  # benchmark line {i:02d}" for i in range(1, 80)] + ["print(value_79)"])
SHORT = "print('short cell')"

# Text of every character of the cell's code, assigned to the clip that fully contains it (or flagged as clipped).
TEXT_PER_CLIP_JS = r"""(el, clips) => {
  const pre = el.querySelector('pre');
  const texts = clips.map(() => ''); const clipped = []; let cur = 0;
  const walker = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT); let node;
  while ((node = walker.nextNode())) {
    const s = node.textContent;
    for (let i = 0; i < s.length; i++) {
      if (s[i] !== '\n') {
        const range = document.createRange(); range.setStart(node, i); range.setEnd(node, i + 1);
        const r = range.getBoundingClientRect();
        const top = r.top + window.scrollY, bottom = r.bottom + window.scrollY, right = r.right + window.scrollX;
        const k = clips.findIndex(c => top >= c.y - 0.5 && bottom <= c.y + c.height + 0.5 && right <= c.x + c.width + 0.5);
        if (k < 0) { clipped.push(s[i]); continue; }
        cur = k;
      }
      texts[cur] += s[i];
    }
  }
  return {texts, clipped, full: pre.textContent};
}"""


def _executed(tmp_path: Path) -> Path:
    nb = nbformat.v4.new_notebook()
    nb.cells = [nbformat.v4.new_code_cell(LONG), nbformat.v4.new_code_cell(SHORT)]
    nb.cells[0]["id"], nb.cells[1]["id"] = "long", "short"
    NotebookClient(nb, timeout=120, kernel_name="python3").execute()
    path = tmp_path / "executed.ipynb"
    nbformat.write(nb, str(path))
    return path


def _spec(cid: str, source: str) -> dict[str, Any]:
    return {"cell_id": cid, "task_id": "T1", "code_sha256": sha256_text(source), "expect_output": True, "source": source}


def test_tall_code_cell_is_captured_in_ordered_parts(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    run_dir = tmp_path / "run"
    executed = _executed(tmp_path)
    res = capture_notebook(run_dir=run_dir, run_id="r", attempt_id="a001", executed_nb=executed, backend="local-jupyter",
                           cells_to_capture=[_spec("long", LONG), _spec("short", SHORT)], provenance="test rendering")
    assert res.problems == []
    long_code = [r for r in res.records if r.cell_ids == ["long"] and r.kind == "code"]
    assert len(long_code) >= 2
    assert [(r.part, r.parts) for r in long_code] == [(i, len(long_code)) for i in range(1, len(long_code) + 1)]
    assert [r.kind for r in res.records if r.cell_ids == ["long"]][-1] == "output"  # the real printed value
    short = [r for r in res.records if r.cell_ids == ["short"]]
    assert [(r.kind, r.part, r.parts) for r in short] == [("code-output", 1, 1)]

    m = RunManifest(run_id="r", lab_dir=str(tmp_path), source_name="m.docx", source_sha256="x", current_attempt="a001",
                    cell_code_sha256={"long": sha256_text(LONG), "short": sha256_text(SHORT)},
                    cell_status={"long": ItemStatus.PASSED, "short": ItemStatus.PASSED})
    assert validate_evidence(run_dir, res.records, m) == []

    # every source line is in exactly one part, in order, never cut: checked on the rendered text of each clip
    assert res.html_path
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": VIEWPORT_W, "height": 900}, device_scale_factor=2,
                                    java_script_enabled=False)
            page.goto((run_dir / res.html_path).resolve().as_uri(), wait_until="load")
            cell = page.locator('[id="cell-id=long"]')
            clips = _input_chunks(cell)
            assert [round(c["height"] * 2) for c in clips] == [pytest.approx(r.height, abs=2) for r in long_code]
            got = cell.locator(".jp-Cell-inputWrapper").first.evaluate(TEXT_PER_CLIP_JS, clips)
        finally:
            browser.close()
    assert got["clipped"] == []
    assert "".join(got["texts"]) == got["full"]
    assert all(t.endswith("\n") for t in got["texts"][:-1])  # parts end on a line boundary
    lines = [ln for t in got["texts"] for ln in t.splitlines()]
    assert [ln.rstrip() for ln in lines] == LONG.splitlines()
