from __future__ import annotations

from pathlib import Path

from labpilot.datasets.inspect import inspect_csv
from labpilot.datasets.validate import dataset_blockers, resolve_datasets
from labpilot.models import RequiredFile


def _req(name: str, cols: list[str] | None = None, used: list[str] | None = None) -> RequiredFile:
    return RequiredFile(name=name, expected_columns=cols or [], used_by_tasks=used or ["T1"])


def test_exact_case_alias_ambiguous_missing(tmp_path: Path) -> None:
    (tmp_path / "orders.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "STORES.CSV").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    (tmp_path / "x" / "dup.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "y" / "dup.csv").write_text("a\n2\n", encoding="utf-8")
    (tmp_path / "my_sales_2024.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "sales_2024.csv").write_text("a\n1\n", encoding="utf-8")
    entries = resolve_datasets(
        tmp_path,
        [_req("orders.csv"), _req("stores.csv"), _req("dup.csv"), _req("sales.csv"), _req("sales2024.csv")],
        aliases={"sales.csv": "my_sales_2024.csv"},
    )
    by = {e.expected_name: e for e in entries}
    assert by["orders.csv"].status == "found" and by["orders.csv"].sha256
    assert by["stores.csv"].status == "mapped" and by["stores.csv"].local_path == "STORES.CSV"
    assert by["dup.csv"].status == "ambiguous" and len(by["dup.csv"].candidates) == 2
    assert by["sales.csv"].status == "mapped" and by["sales.csv"].local_path == "my_sales_2024.csv"
    assert by["sales2024.csv"].status == "missing" and by["sales2024.csv"].candidates  # similar names offered, not chosen
    blockers = dataset_blockers(entries, [_req(n) for n in by])
    codes = sorted(b.code for b in blockers)
    assert codes == ["DATASET_AMBIGUOUS", "DATASET_MISSING"]
    assert all(b.affected_tasks == ["T1"] and b.requires_user for b in blockers)


def test_schema_mismatch_and_corrupt(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (tmp_path / "empty.csv").write_text("", encoding="utf-8")
    entries = resolve_datasets(tmp_path, [_req("a.csv", ["x", "z"]), _req("empty.csv")])
    assert entries[0].status == "schema_mismatch" and "z" in entries[0].problems[-1]
    assert entries[1].status == "corrupt"


def test_csv_inspection_types_malformed_duplicates_encoding(tmp_path: Path) -> None:
    p = tmp_path / "d.csv"
    p.write_bytes("id,price,day,name\n1,2.5,2025-01-01,Caf\xe9\n1,2.5,2025-01-01,Caf\xe9\n2,3,2025-01-02\n".encode("cp1252"))
    s = inspect_csv(p)
    assert s["encoding"] == "cp1252"
    types = {c["name"]: c["type"] for c in s["columns"]}
    assert types == {"id": "integer", "price": "double", "day": "date", "name": "string"}
    assert s["duplicate_rows_in_sample"] == 1
    assert any("malformed" in x for x in s["problems"])
    assert s["row_count"] == 3


def test_discovery_skips_workspace_and_output(tmp_path: Path) -> None:
    for d in (".labpilot", "output"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "orders.csv").write_text("a\n1\n", encoding="utf-8")
    entries = resolve_datasets(tmp_path, [_req("orders.csv")])
    assert entries[0].status == "missing"
