"""Real PNG evidence of executed cells via headless Chromium element screenshots.

Mode "rendered-local-notebook": the actual executed .ipynb is rendered to
script-free HTML (nbconvert), opened from a local file with JavaScript disabled
and all network requests blocked, and each cell is captured with Playwright.
The evidence manifest records which backend actually executed the code.

Mode "native-colab-ui" requires an authorized, user-visible Colab browser
session showing the real executed notebook; v1 does not automate it (see
docs/BACKENDS.md) and reports it as unavailable instead of imitating it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import nbformat

from labpilot.errors import ErrorCode, ExitCode, LabPilotError
from labpilot.evidence.validate import image_problems
from labpilot.models import EvidenceRecord, utcnow
from labpilot.notebooks.render import render_html
from labpilot.workspace import rmtree_owned, sha256_file

VIEWPORT_W = 1280
MAX_VIEWPORT_W = 2200
SCALE = 2
MAX_CHUNK_CSS_H = 1100  # ~ one Word page at readable size; taller content is split
CAPTURE_RETRIES = 2


@dataclass
class CaptureResult:
    records: list[EvidenceRecord] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    html_path: str | None = None


def _safe_dir(task_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in task_id)


def capture_notebook(
    *,
    run_dir: Path,
    run_id: str,
    attempt_id: str,
    executed_nb: Path,
    backend: str,
    cells_to_capture: list[dict[str, Any]],
    provenance: str,
) -> CaptureResult:
    """cells_to_capture: [{cell_id, task_id, code_sha256, expect_output}] in notebook order."""
    try:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise LabPilotError(
            ErrorCode.BROWSER_UNAVAILABLE, "Playwright is not installed.", exit_code=ExitCode.CONFIG
        ) from exc

    out_root = run_dir / "evidence"
    shots_root = out_root / "screenshots"
    rmtree_owned(shots_root)  # owned, regenerated per capture from the current attempt only
    html = render_html(executed_nb, out_root / "render" / "executed.html", provenance)
    result = CaptureResult(html_path=html.relative_to(run_dir).as_posix())

    nb = nbformat.read(str(executed_nb), as_version=4)
    outputs_by_id = {c.get("id"): c.get("outputs", []) for c in nb.cells if c.cell_type == "code"}
    counters: dict[str, int] = {}

    for attempt in range(CAPTURE_RETRIES + 1):
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        viewport={"width": VIEWPORT_W, "height": 900},
                        device_scale_factor=SCALE,
                        java_script_enabled=False,
                        offline=True,
                    )
                    context.route(
                        "**/*",
                        lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort(),
                    )
                    page = context.new_page()
                    page.goto(html.resolve().as_uri(), wait_until="load")
                    page.wait_for_load_state("networkidle")
                    for spec in cells_to_capture:
                        if any(r.cell_ids == [spec["cell_id"]] for r in result.records):
                            continue
                        _capture_cell(
                            page, spec, outputs_by_id.get(spec["cell_id"], []), shots_root, run_dir, counters,
                            result, run_id=run_id, attempt_id=attempt_id, backend=backend,
                        )
                finally:
                    browser.close()
            break
        except PWError as exc:
            if attempt == CAPTURE_RETRIES:
                raise LabPilotError(
                    ErrorCode.BROWSER_UNAVAILABLE,
                    f"Headless Chromium failed after {CAPTURE_RETRIES + 1} attempts: {exc}",
                    exit_code=ExitCode.CONFIG,
                    next_action="Run `python -m playwright install chromium`, then `labpilot capture <run-id>`.",
                ) from exc
    return result


def _capture_cell(
    page: Any,
    spec: dict[str, Any],
    outputs: list[Any],
    shots_root: Path,
    run_dir: Path,
    counters: dict[str, int],
    result: CaptureResult,
    *,
    run_id: str,
    attempt_id: str,
    backend: str,
) -> None:
    cid, task_id = spec["cell_id"], spec["task_id"]
    if spec.get("expect_output", True) and not outputs:
        result.problems.append(
            {
                "cell_id": cid,
                "task_id": task_id,
                "code": str(ErrorCode.EVIDENCE_INVALID),
                "message": "Cell produced no output although the task needs visible output; "
                "add an explicit display (print/show/display) and re-execute.",
            }
        )
    has_figure = any("image/png" in o.get("data", {}) or "image/svg+xml" in o.get("data", {}) for o in outputs)
    if re.search(r"plt\.show\(|\.plot\(|\.hist\(|\.bar\(|sns\.", spec.get("source", "")) and not has_figure:
        result.problems.append(
            {
                "cell_id": cid,
                "task_id": task_id,
                "code": str(ErrorCode.EVIDENCE_INVALID),
                "message": "Cell draws a chart but no figure was produced in the output; "
                "check the matplotlib backend / display call and re-execute.",
            }
        )
    cell = page.locator(f'[id="cell-id={cid}"]')
    if cell.count() != 1:
        result.problems.append(
            {"cell_id": cid, "task_id": task_id, "code": "EVIDENCE_INVALID", "message": "cell not found in rendering"}
        )
        return
    # measure at the standard width first (an earlier wide cell may have widened the page), then widen only if this
    # cell's own content (e.g. a wide table) needs it, instead of clipping it
    page.set_viewport_size({"width": VIEWPORT_W, "height": 900})
    width = cell.evaluate("el => Math.ceil(Math.max(el.scrollWidth, el.getBoundingClientRect().width))")
    needed = min(MAX_VIEWPORT_W, max(VIEWPORT_W, int(width) + 48))
    page.set_viewport_size({"width": needed, "height": 900})
    box = cell.bounding_box()
    if box is None:
        result.problems.append({"cell_id": cid, "task_id": task_id, "code": "EVIDENCE_INVALID", "message": "cell not visible"})
        return

    task_dir = shots_root / _safe_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    n = counters.get(task_id, 0) + 1
    counters[task_id] = n
    base = f"{n:02d}"

    targets: list[tuple[str, dict[str, float], int, int]] = []  # kind, clip, part, parts
    if box["height"] <= MAX_CHUNK_CSS_H or not outputs:
        clip = dict(box)
        clip["width"] = _content_width(cell, box["x"], box["width"])
        targets.append(("code-output", clip, 1, 1))
    else:
        inp_loc = cell.locator(".jp-Cell-inputWrapper").first
        inp = inp_loc.bounding_box()
        if inp:
            inp = dict(inp)
            inp["width"] = _content_width(inp_loc, inp["x"], inp["width"])
            targets.append(("code", inp, 1, 1))
        chunks = _output_chunks(cell, page)
        for i, clip in enumerate(chunks, start=1):
            targets.append(("output", clip, i, len(chunks)))

    for kind, clip, part, parts in targets:
        suffix = f"-part{part}" if parts > 1 else ""
        dest = task_dir / f"{base}-{kind}{suffix}.png"
        page.screenshot(
            path=str(dest),
            clip={"x": clip["x"], "y": clip["y"], "width": clip["width"], "height": clip["height"]},
            full_page=True,
            animations="disabled",
        )
        probs, w, h = image_problems(dest)
        rec = EvidenceRecord(
            run_id=run_id,
            attempt_id=attempt_id,
            task_id=task_id,
            cell_ids=[cid],
            code_sha256={cid: spec["code_sha256"]},
            backend=backend,
            capture_mode="rendered-local-notebook",
            kind=kind,  # type: ignore[arg-type]
            part=part,
            parts=parts,
            captured_at=utcnow(),
            sha256=sha256_file(dest),
            width=w,
            height=h,
            path=dest.relative_to(run_dir).as_posix(),
            valid=not probs,
            problems=probs,
        )
        result.records.append(rec)
        if probs:
            result.problems.append(
                {"cell_id": cid, "task_id": task_id, "code": "EVIDENCE_INVALID", "message": "; ".join(probs)}
            )


# Right edge of the real content (text line boxes, images, tables), used to crop empty space so text
# stays legible when the image is fitted to the Word page width.
CONTENT_RIGHT_JS = """el => {
  let right = 0;
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (!node.textContent.trim()) continue;
    const r = document.createRange(); r.selectNodeContents(node);
    for (const rect of r.getClientRects()) right = Math.max(right, rect.right);
  }
  el.querySelectorAll('img, svg, table, canvas').forEach(n => right = Math.max(right, n.getBoundingClientRect().right));
  return right + window.scrollX;
}"""

# Safe cut positions inside an output area: text line boundaries of <pre> blocks, table row and
# image edges, and output-item edges. Never inside a line, row or image.
CUT_POINTS_JS = """el => {
  const ys = []; const add = y => ys.push(y + window.scrollY);
  el.querySelectorAll('pre').forEach(pre => {
    const cs = getComputedStyle(pre); const r = pre.getBoundingClientRect();
    let lh = parseFloat(cs.lineHeight); if (isNaN(lh)) lh = parseFloat(cs.fontSize) * 1.2;
    const top = r.top + parseFloat(cs.paddingTop || '0');
    for (let y = top; y <= r.bottom + 0.5; y += lh) add(y);
  });
  el.querySelectorAll('tr, img, svg, .jp-OutputArea-child').forEach(n => {
    const r = n.getBoundingClientRect(); add(r.top); add(r.bottom);
  });
  return ys;
}"""


def _content_width(locator: Any, x: float, max_width: float) -> float:
    try:
        right = float(locator.evaluate(CONTENT_RIGHT_JS))
    except Exception:  # noqa: BLE001 - fall back to the full element width
        return max_width
    return max(120.0, min(max_width, right - x + 24))


def _output_chunks(cell: Any, page: Any) -> list[dict[str, float]]:
    """Split the output area into chunks no taller than MAX_CHUNK_CSS_H, cutting only at safe boundaries."""
    wrapper = cell.locator(".jp-Cell-outputWrapper").first
    wbox = wrapper.bounding_box()
    if wbox is None:
        return []
    x, top, end = wbox["x"], wbox["y"], wbox["y"] + wbox["height"]
    width = _content_width(wrapper, x, wbox["width"])
    cuts = sorted(set(round(float(y), 2) for y in wrapper.evaluate(CUT_POINTS_JS)))
    chunks: list[dict[str, float]] = []
    start = top
    while start < end - 1:
        limit = start + MAX_CHUNK_CSS_H
        if limit >= end:
            stop = end
        else:
            candidates = [c for c in cuts if start + 40 < c <= limit]
            stop = max(candidates) if candidates else limit  # no safe boundary: fixed band (rare: huge image)
        chunks.append({"x": x, "y": start, "width": width, "height": stop - start})
        start = stop
    return chunks
