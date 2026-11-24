"""What a host agent would submit for the synthetic Python lab: plan edits, cells, and grounded answers.

Used by the reproducible demo (examples/run_demo.py) and the end-to-end tests. The answers
are produced by READING the executed notebook's real outputs, never hardcoded.
"""

from __future__ import annotations

import re
from typing import Any

import nbformat

QUESTION_KINDS = {"T2.B.Q1": "conceptual"}


def make_plan(draft: dict[str, Any]) -> dict[str, Any]:
    plan = dict(draft)
    all_tasks = [t["id"] for t in draft["tasks"] if t["id"] != "SETUP"]
    plan["required_files"] = [
        {"name": "sales.csv", "role": "dataset", "required": True, "used_by_tasks": all_tasks,
         "expected_columns": ["order_id", "region_id", "product", "quantity", "unit_price"]},
        {"name": "regions.csv", "role": "dataset", "required": True, "used_by_tasks": all_tasks,
         "expected_columns": ["region_id", "region_name"]},
    ]
    for t in plan["tasks"]:
        for q in t["observation_questions"]:
            q["kind"] = QUESTION_KINDS.get(q["id"], "observed")
    plan["notes"] = ["Reviewed against the manual: 2 tasks x 2 parts, 6 questions; datasets are synthetic."]
    return plan


CELLS: dict[str, Any] = {
    "schema_version": 1,
    "cells": [
        {"id": "setup-imports", "task_id": "SETUP", "setup": True, "source":
            "import os\nimport pandas as pd\nimport matplotlib\nimport matplotlib.pyplot as plt\n"
            "print('pandas', pd.__version__, '| matplotlib', matplotlib.__version__)\n"
            "print('staged files:', sorted(f for f in os.listdir('.') if f.endswith('.csv')))"},
        {"id": "t1a-load", "task_id": "T1.A", "source":
            "sales = pd.read_csv('sales.csv')\nregions = pd.read_csv('regions.csv')\n"
            "print('sales shape:', sales.shape)\nprint('regions shape:', regions.shape)\n"
            "print(sales.dtypes)\n"
            "print('ORDERS =', len(sales))\n"
            "print('LINK_COLUMNS =', sorted(set(sales.columns) & set(regions.columns)))"},
        {"id": "t1b-revenue", "task_id": "T1.B", "source":
            "sales['revenue'] = sales['quantity'] * sales['unit_price']\n"
            "display(sales.head())\n"
            "total_revenue = sales['revenue'].sum()\n"
            "assert abs(total_revenue - (sales['quantity'] * sales['unit_price']).sum()) < 1e-9\n"
            "print(f'TOTAL_REVENUE = {total_revenue:.2f}')"},
        {"id": "t2a-merge", "task_id": "T2.A", "source":
            "merged = sales.merge(regions, on='region_id', how='left')\n"
            "print(merged.to_string())"},
        {"id": "t2a-summary", "task_id": "T2.A", "source":
            "by_region = (merged.fillna({'region_name': '(no match)'})\n"
            "             .groupby('region_name')['revenue'].sum().sort_values(ascending=False))\n"
            "display(by_region.to_frame('revenue'))\n"
            "unmatched = int(merged['region_name'].isna().sum())\n"
            "assert abs(by_region.sum() - total_revenue) < 1e-6  # nothing lost in the join\n"
            "matched_only = by_region.drop('(no match)', errors='ignore')\n"
            "print('TOP_REGION =', matched_only.index[0])\n"
            "print('UNMATCHED =', unmatched)"},
        {"id": "t2b-plot", "task_id": "T2.B", "source":
            "ax = by_region.plot(kind='bar', color='#3b6ea8', title='Revenue by region')\n"
            "ax.set_ylabel('revenue')\nplt.tight_layout()\nplt.show()"},
    ],
}


def _stdout(nb: nbformat.NotebookNode) -> dict[str, str]:
    out = {}
    for c in nb.cells:
        if c.cell_type == "code":
            out[c.id] = "".join(o.get("text", "") for o in c.outputs if o.get("output_type") == "stream")
    return out


def _value(text: str, key: str) -> str:
    m = re.search(rf"{key} = (.+)", text)
    if not m:
        raise ValueError(f"{key} not found in the real output; cannot answer from evidence")
    return m.group(1).strip()


def make_answers(executed_nb_path: str) -> dict[str, Any]:
    nb = nbformat.read(executed_nb_path, as_version=4)
    s = _stdout(nb)
    orders = _value(s["t1a-load"], "ORDERS")
    link = _value(s["t1a-load"], "LINK_COLUMNS")
    total = _value(s["t1b-revenue"], "TOTAL_REVENUE")
    top = _value(s["t2a-summary"], "TOP_REGION")
    unmatched = _value(s["t2a-summary"], "UNMATCHED")
    return {
        "schema_version": 1,
        "answers": [
            {"question_id": "T1.A.Q1", "text": f"sales.csv contains {orders} orders.",
             "source_cell_ids": ["t1a-load"], "verification": "verified"},
            {"question_id": "T1.A.Q2", "text": f"The shared column is {link}; it is the key used to join the files.",
             "source_cell_ids": ["t1a-load"], "verification": "verified"},
            {"question_id": "T1.B.Q1", "text": f"Total revenue across all orders is {total}.",
             "source_cell_ids": ["t1b-revenue"], "verification": "verified"},
            {"question_id": "T2.A.Q1", "text": f"{top} has the highest revenue among matched regions.",
             "source_cell_ids": ["t2a-summary"], "verification": "verified"},
            {"question_id": "T2.A.Q2", "text": f"{unmatched} order(s) have a region_id with no match in regions.csv.",
             "source_cell_ids": ["t2a-summary"], "verification": "verified"},
            {"question_id": "T2.B.Q1", "text": "A left join keeps every order, so rows whose region_id has no match "
             "remain visible (with a missing region name) instead of silently disappearing as with an inner join.",
             "source_cell_ids": [], "verification": "conceptual"},
        ],
        "task_summaries": [
            {"task_id": "SETUP", "explanation": "Imports pandas and matplotlib and lists the staged CSV files."},
            {"task_id": "T1.A", "explanation": "Reads both CSV files, prints their shapes, column types, the "
             "order count and the column they share."},
            {"task_id": "T1.B", "explanation": "Adds revenue = quantity x unit_price and prints the total."},
            {"task_id": "T2.A", "explanation": "Left-joins orders with regions, prints all rows, sums revenue per "
             "region (sorted) and checks that no revenue is lost in the join."},
            {"task_id": "T2.B", "explanation": "Plots the per-region revenue as a bar chart."},
        ],
    }
