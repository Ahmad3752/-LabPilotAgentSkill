"""Generate the SYNTHETIC sample Python lab (manual + datasets) into a folder.

    python examples/sample_python_lab/make_lab.py <output-folder>
"""

from __future__ import annotations

import csv
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixture_tools import code, nested_columns_table, new_manual, save, solution_box  # noqa: E402

REGIONS = [(1, "North"), (2, "South"), (3, "East"), (4, "West")]
PRODUCTS = [("Notebook", 3.5), ("Pen", 1.25), ("Backpack", 24.0), ("Calculator", 15.75), ("Ruler", 0.8)]


def write_data(folder: Path) -> None:
    rng = random.Random(42)
    with (folder / "sales.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["order_id", "region_id", "product", "quantity", "unit_price"])
        for i in range(1, 81):
            region = 9 if i in (17, 58) else rng.choice([1, 2, 3, 4])  # region 9 has no match: tests the left join
            product, price = rng.choice(PRODUCTS)
            w.writerow([1000 + i, region, product, rng.randint(1, 12), price])
    with (folder / "regions.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["region_id", "region_name"])
        w.writerows(REGIONS)
    (folder / "SYNTHETIC_DATA_README.txt").write_text(
        "sales.csv and regions.csv are synthetic, generated with a fixed seed for LabPilot demos/tests.\n",
        encoding="utf-8",
    )


def write_manual(folder: Path) -> Path:
    doc = new_manual("Lab 3: Data Analysis with Pandas", "CS-101 Introduction to Data Science (Synthetic)")
    doc.add_heading("Objectives", level=1)
    doc.add_paragraph("Load CSV files with pandas, derive new columns, join tables and summarise results.")
    doc.add_heading("Setup", level=1)
    doc.add_paragraph("Open a new Google Colab notebook. You are given two files: sales.csv and regions.csv. "
                      "Upload them when prompted.")
    code(doc, "!pip install -q pandas matplotlib\nimport pandas as pd\nimport matplotlib.pyplot as plt\n"
              "from google.colab import files\nuploaded = files.upload()")

    doc.add_heading("Task 1: Load and Inspect the Data", level=1)
    doc.add_heading("Part A: Load Both Files", level=2)
    doc.add_paragraph("Read both CSV files into DataFrames and display their shape and column types.")
    code(doc, "sales = pd.read_csv('/content/sales.csv')\nregions = # TODO: load regions.csv")
    doc.add_paragraph("How many orders does sales.csv contain?")
    doc.add_paragraph("Which column links the two files?")
    doc.add_heading("Part B: Compute Revenue", level=2)
    doc.add_paragraph("Add a revenue column equal to quantity multiplied by unit_price and print the total revenue.")
    doc.add_paragraph("What is the total revenue across all orders?")
    solution_box(doc, "Task 1 Solution")

    doc.add_page_break()
    doc.add_heading("Task 2: Merge and Summarise", level=1)
    doc.add_paragraph("The regions file has the following columns:")
    nested_columns_table(doc, "regions.csv", [("region_id", "numeric region key"), ("region_name", "display name")])
    doc.add_heading("Part A: Revenue by Region", level=2)
    doc.add_paragraph(
        "Merge sales with regions using a left join on region_id. Print every merged row, then show total revenue "
        "per region sorted from highest to lowest. Count the orders whose region has no match."
    )
    doc.add_paragraph("Which region has the highest revenue?")
    doc.add_paragraph("How many orders have no matching region?")
    doc.add_heading("Part B: Visualise", level=2)
    doc.add_paragraph("Draw a bar chart of revenue per region.")
    doc.add_paragraph("Why is a left join more appropriate than an inner join for checking data quality here?")
    solution_box(doc, "Task 2 Solution")
    return save(doc, folder / "Lab 3.docx")


def make(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    write_data(folder)
    return write_manual(folder)


if __name__ == "__main__":
    print(make(Path(sys.argv[1] if len(sys.argv) > 1 else "sample_python_lab_out")))
