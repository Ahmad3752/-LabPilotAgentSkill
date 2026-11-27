"""Render an executed notebook to self-contained, script-free HTML for evidence capture.

Safety model: active content is removed from outputs here (scripts, iframes,
objects, event-handler attributes, javascript: URLs, external resources), and
the page is additionally opened with JavaScript disabled and all network
requests blocked. Table/markup structure is kept so rendered DataFrames look
exactly as they do in Jupyter (nbconvert's own sanitizer escapes <style> and
breaks pandas tables, so it is not used).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import nbformat
from nbconvert import HTMLExporter

ACTIVE_MIME = ("application/javascript", "application/vnd.jupyter.widget-view+json", "text/javascript")
_DANGEROUS_BLOCKS = re.compile(r"<(script|iframe|object|embed|frame|frameset|applet|noscript)\b.*?</\1\s*>", re.I | re.S)
_DANGEROUS_TAGS = re.compile(r"<\s*/?\s*(script|iframe|object|embed|frame|frameset|applet|link|meta|base|form)\b[^>]*>", re.I)
_EVENT_ATTRS = re.compile(r"""\s+on[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_JS_URLS = re.compile(r"""(href|src|xlink:href|action)\s*=\s*(["']?)\s*(javascript|vbscript|data:text/html)[^"'>\s]*\2""", re.I)
_EXTERNAL = re.compile(r"""(src|href)\s*=\s*(["'])(https?:)?//[^"']*\2""", re.I)
_CSS_IMPORT = re.compile(r"@import[^;]+;|url\(\s*['\"]?(https?:)?//[^)]*\)", re.I)

# Readability tweaks only; no branding imitation. Code/output are not altered.
EVIDENCE_CSS = """
<style id="labpilot-evidence">
  body { background: #ffffff !important; margin: 0; }
  .jp-Notebook { padding: 12px 16px !important; }
  .jp-Cell { margin-bottom: 14px !important; }
  .jp-InputArea-editor, .jp-OutputArea-output pre, .jp-RenderedText pre {
    font-family: Consolas, "Cascadia Mono", "DejaVu Sans Mono", Menlo, monospace !important;
    font-size: 13.5px !important; line-height: 1.4 !important;
  }
  .jp-RenderedHTMLCommon table { font-size: 12.5px !important; }
  /* wrap long log lines (e.g. YARN application lists) at page width instead of widening the image until the
     text becomes unreadably small in the document; the text itself is unchanged */
  .jp-OutputArea-output pre, .jp-RenderedText pre { white-space: pre-wrap !important; overflow-wrap: anywhere; }
  /* the same for very long code lines, which otherwise widen the whole cell image */
  .jp-InputArea-editor pre, .jp-InputArea-editor .highlight pre { white-space: pre-wrap !important; overflow-wrap: anywhere; }
  /* wrap at ~118 characters, not at the 1280px viewport: a full-width crop is ~13in at capture DPI and Word fits it
     at ~0.49 scale; 880px keeps the crop under ~1000px, i.e. >= MIN_READABLE_SCALE (0.6) on an A4 or Letter page */
  .jp-OutputArea-output pre, .jp-RenderedText pre, .jp-InputArea-editor pre { max-width: 880px; }
  .jp-OutputArea-output pre { word-break: normal !important; }  /* JupyterLab's break-all splits words mid-word */
  .labpilot-provenance { font: 11px sans-serif; color: #555; padding: 4px 16px; border-bottom: 1px solid #ddd; }
</style>
"""


def clean_html(html: str) -> str:
    html = _DANGEROUS_BLOCKS.sub("", html)
    html = _DANGEROUS_TAGS.sub("", html)
    html = _EVENT_ATTRS.sub("", html)
    html = _JS_URLS.sub(r'\1=""', html)
    html = _EXTERNAL.sub(r'\1=""', html)
    return _CSS_IMPORT.sub("", html)


def sanitized_copy(nb: nbformat.NotebookNode) -> nbformat.NotebookNode:
    nb = copy.deepcopy(nb)
    for cell in nb.cells:
        if cell.cell_type == "markdown":
            cell.source = clean_html(cell.source)
            continue
        if cell.cell_type != "code":
            continue
        for out in cell.get("outputs", []):
            data = out.get("data")
            if not data:
                continue
            for mime in ACTIVE_MIME:
                data.pop(mime, None)
            for mime in ("text/html", "image/svg+xml"):
                if mime in data:
                    val = data[mime]
                    data[mime] = clean_html("".join(val) if isinstance(val, list) else val)
    return nb


def render_html(executed_nb: Path, out_html: Path, provenance: str) -> Path:
    nb = nbformat.read(str(executed_nb), as_version=4)
    nb = sanitized_copy(nb)
    exporter = HTMLExporter(template_name="lab")
    exporter.sanitize_html = False  # done above, preserving table structure
    exporter.embed_images = True
    exporter.mathjax_url = ""
    body, _ = exporter.from_notebook_node(nb)
    banner = f'<div class="labpilot-provenance">{_escape(provenance)}</div>'
    body = body.replace("</head>", EVIDENCE_CSS + "</head>", 1)
    idx = body.find(">", body.find("<body")) + 1
    body = body[:idx] + banner + body[idx:]
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(body, encoding="utf-8")
    return out_html


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
