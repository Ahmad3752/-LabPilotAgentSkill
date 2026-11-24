"""Stable anchors for Word content.

Anchor grammar (document order, physical XML structure, content controls flattened):

    b<N>                    N-th block (paragraph or table) in the body
    b<N>/r<I>/c<J>          physical cell J of row I of table b<N>
    b<N>/r<I>/c<J>/b<K>     K-th block inside that cell (recursive for nested tables)

An anchor may carry a text fingerprint: "b5/r1/c0#3fa2c1d09e8b". Resolution
verifies the fingerprint so edited/shifted documents are detected instead of
silently writing into the wrong place.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from docx.oxml.ns import qn

from labpilot.errors import ErrorCode, LabPilotError

W_P = qn("w:p")
W_TBL = qn("w:tbl")
W_TR = qn("w:tr")
W_TC = qn("w:tc")
W_SDT = qn("w:sdt")
W_SDT_CONTENT = qn("w:sdtContent")
W_T = qn("w:t")
W_TAB = qn("w:tab")
W_BR = qn("w:br")

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text).strip()


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).lower().encode("utf-8")).hexdigest()[:12]


def element_text(el: Any) -> str:
    """Visible text of a paragraph/cell/table element (tabs and breaks become spaces/newlines)."""
    parts: list[str] = []
    for node in el.iter():
        if node.tag == W_T and node.text:
            parts.append(node.text)
        elif node.tag == W_TAB:
            parts.append("\t")
        elif node.tag == W_BR or node.tag == W_P and parts and not parts[-1].endswith("\n"):
            parts.append("\n")
    return "".join(parts).strip()


def iter_blocks(container: Any) -> Iterator[Any]:
    """Yield w:p / w:tbl children of a body or cell in order, descending into content controls."""
    for child in container.iterchildren():
        if child.tag in (W_P, W_TBL):
            yield child
        elif child.tag == W_SDT:
            content = child.find(W_SDT_CONTENT)
            if content is not None:
                yield from iter_blocks(content)


def iter_rows(tbl: Any) -> list[Any]:
    return [c for c in tbl.iterchildren() if c.tag == W_TR]


def iter_cells(tr: Any) -> list[Any]:
    cells: list[Any] = []
    for c in tr.iterchildren():
        if c.tag == W_TC:
            cells.append(c)
        elif c.tag == W_SDT:
            content = c.find(W_SDT_CONTENT)
            if content is not None:
                cells.extend(x for x in content.iterchildren() if x.tag == W_TC)
    return cells


@dataclass
class ParsedAnchor:
    path: str
    fingerprint: str | None


def parse_anchor(anchor: str) -> ParsedAnchor:
    path, _, fp = anchor.partition("#")
    if not re.fullmatch(r"b\d+(/r\d+/c\d+(/b\d+)?)*", path):
        raise LabPilotError(ErrorCode.ANCHOR_AMBIGUOUS, f"Malformed anchor '{anchor}'.")
    return ParsedAnchor(path=path, fingerprint=fp or None)


def resolve(body: Any, anchor: str, *, check_fingerprint: bool = True) -> Any:
    """Return the XML element (w:p, w:tbl or w:tc) addressed by an anchor."""
    parsed = parse_anchor(anchor)
    segments = parsed.path.split("/")
    container = body
    element: Any = None
    i = 0
    while i < len(segments):
        seg = segments[i]
        if seg.startswith("b"):
            blocks = list(iter_blocks(container))
            idx = int(seg[1:])
            if idx >= len(blocks):
                raise LabPilotError(ErrorCode.ANCHOR_AMBIGUOUS, f"Anchor '{anchor}' no longer exists.")
            element = blocks[idx]
            i += 1
        elif seg.startswith("r"):
            if element is None or element.tag != W_TBL:
                raise LabPilotError(ErrorCode.ANCHOR_AMBIGUOUS, f"Anchor '{anchor}' does not address a table.")
            rows = iter_rows(element)
            r = int(seg[1:])
            c = int(segments[i + 1][1:])
            if r >= len(rows) or c >= len(iter_cells(rows[r])):
                raise LabPilotError(ErrorCode.ANCHOR_AMBIGUOUS, f"Anchor '{anchor}' cell is out of range.")
            element = iter_cells(rows[r])[c]
            container = element
            i += 2
        else:  # pragma: no cover - guarded by parse_anchor
            raise LabPilotError(ErrorCode.ANCHOR_AMBIGUOUS, f"Malformed anchor '{anchor}'.")
    if check_fingerprint and parsed.fingerprint and fingerprint(element_text(element)) != parsed.fingerprint:
        raise LabPilotError(
            ErrorCode.ANCHOR_AMBIGUOUS,
            f"Anchor '{anchor}' text changed since inspection; re-run `labpilot inspect` and review the plan.",
        )
    return element
