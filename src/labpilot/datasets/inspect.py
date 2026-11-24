"""Bounded, dependency-free inspection of dataset files (format, encoding, schema, sample problems)."""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

MAX_SAMPLE_ROWS = 2000
MAX_SAMPLE_BYTES = 4 * 1024 * 1024
ENCODINGS = ["utf-8", "utf-8-sig", "cp1252", "latin-1"]
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$|^\d{1,2}/\d{1,2}/\d{2,4}$")


def _decode(raw: bytes) -> tuple[str, str]:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "unknown"


def _infer(values: list[str]) -> str:
    vals = [v for v in values if v not in ("", "NA", "N/A", "null", "NULL", "None")]
    if not vals:
        return "empty"
    if all(_INT.match(v) for v in vals):
        return "integer"
    if all(_FLOAT.match(v) for v in vals):
        return "double"
    if all(_DATE.match(v) for v in vals):
        return "date"
    if all(v.lower() in ("true", "false") for v in vals):
        return "boolean"
    return "string"


def inspect_csv(path: Path, delimiter: str | None = None) -> dict[str, Any]:
    with path.open("rb") as fh:
        raw = fh.read(MAX_SAMPLE_BYTES)
    truncated = path.stat().st_size > len(raw)
    text, encoding = _decode(raw)
    if truncated and "\n" in text:
        text = text[: text.rfind("\n")]  # drop partial last line of the sample
    summary: dict[str, Any] = {"format": "csv", "encoding": encoding, "sample_truncated": truncated}
    if not text.strip():
        summary["problems"] = ["file is empty"]
        return summary
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = []
    for i, row in enumerate(reader):
        rows.append(row)
        if i > MAX_SAMPLE_ROWS:
            break
    header = [h.strip() for h in rows[0]]
    body = rows[1:MAX_SAMPLE_ROWS + 1]
    problems: list[str] = []
    malformed = [i + 2 for i, r in enumerate(body) if len(r) != len(header) and any(x.strip() for x in r)]
    if malformed:
        problems.append(f"{len(malformed)} malformed row(s) in sample (line numbers {malformed[:10]})")
    if len(set(header)) != len(header):
        problems.append("duplicate column names in header")
    if any(not h for h in header):
        problems.append("empty column name in header")
    seen: set[tuple[str, ...]] = set()
    dup_rows = 0
    for r in body:
        t = tuple(r)
        if t in seen:
            dup_rows += 1
        seen.add(t)
    columns = []
    for j, name in enumerate(header):
        col = [r[j].strip() if j < len(r) else "" for r in body]
        columns.append(
            {
                "name": name,
                "type": _infer(col),
                "nulls_in_sample": sum(1 for v in col if v == ""),
                "distinct_in_sample": len(set(col)),
            }
        )
    summary.update(
        {
            "delimiter": delimiter,
            "columns": columns,
            "column_names": header,
            "rows_in_sample": len(body),
            "duplicate_rows_in_sample": dup_rows,
            "problems": problems,
        }
    )
    if not truncated:
        summary["row_count"] = len(body)
    return summary


def inspect_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()[:MAX_SAMPLE_BYTES]
    text, encoding = _decode(raw)
    summary: dict[str, Any] = {"format": "json", "encoding": encoding}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # JSON lines?
        lines = [ln for ln in text.splitlines() if ln.strip()][:MAX_SAMPLE_ROWS]
        try:
            objs = [json.loads(ln) for ln in lines]
        except json.JSONDecodeError as exc:
            summary["problems"] = [f"invalid JSON: {exc}"]
            return summary
        keys = sorted({k for o in objs if isinstance(o, dict) for k in o})
        summary.update({"format": "jsonl", "records_in_sample": len(objs), "keys": keys, "problems": []})
        return summary
    if isinstance(data, list):
        keys = sorted({k for o in data[:MAX_SAMPLE_ROWS] if isinstance(o, dict) for k in o})
        summary.update({"top_level": "array", "length": len(data), "keys": keys})
    elif isinstance(data, dict):
        summary.update({"top_level": "object", "keys": sorted(data)[:200]})
    summary["problems"] = []
    return summary


def inspect_image(path: Path) -> dict[str, Any]:
    from PIL import Image

    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            return {"format": "image", "width": im.width, "height": im.height, "mode": im.mode, "problems": []}
    except Exception as exc:  # noqa: BLE001
        return {"format": "image", "problems": [f"cannot decode image: {exc}"]}


def inspect_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".txt"):
        return inspect_csv(path)
    if suffix == ".tsv":
        return inspect_csv(path, delimiter="\t")
    if suffix in (".json", ".jsonl"):
        return inspect_json(path)
    if suffix in (".png", ".jpg", ".jpeg"):
        return inspect_image(path)
    if suffix == ".parquet":
        with path.open("rb") as fh:
            ok = fh.read(4) == b"PAR1"
        return {"format": "parquet", "problems": [] if ok else ["missing PAR1 magic header"]}
    if suffix in (".xlsx", ".zip"):
        import zipfile

        return {"format": suffix[1:], "problems": [] if zipfile.is_zipfile(path) else ["not a valid zip container"]}
    return {"format": suffix.lstrip(".") or "unknown", "problems": []}
