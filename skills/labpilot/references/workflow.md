# LabPilot workflow reference

Run files live in `<lab>/.labpilot/runs/<run-id>/`. You write three JSON files (plan, cells, answers), and LabPilot
validates each one. Everything between them is one command, `labpilot finish` (below).

## plan.json

```json
{
  "schema_version": 1,
  "lab_title": "Lab 3: Data Cleaning with Pandas",
  "source_sha256": "<copy from init/inspect output>",
  "profile": "python",
  "requires_native_colab_evidence": false,
  "required_files": [
    {"name": "sales.csv", "role": "dataset", "required": true,
     "expected_columns": ["order_id", "amount"], "used_by_tasks": ["T1.A", "T1.B"]}
  ],
  "tasks": [
    {"id": "T1.A", "title": "Load the data", "parent": "T1",
     "source_anchor": "b12#3fa2c1d09e8b", "solution_anchor": "b31/r1/c0#e3b0c44298fc",
     "placement": "anchor", "depends_on": [],
     "instructions": ["Show the schema and row count"],
     "observation_questions": [{"id": "T1.A.Q1", "text": "How many rows are there?", "kind": "observed"}],
     "required_evidence": ["code", "output"]}
  ],
  "notes": ["Heading mentions retail_sales.csv but the code only uses sales.csv; not treated as required."]
}
```

- `profile`: always `python` (Colab labs install anything else in their own `!pip` setup cell).
- A `SETUP` task exists only when the manual has its own setup section (the draft plan then adds it, placed after
  that section). Otherwise imports and installs go in the first task's first cell.
- `placement`:
  - `anchor`: insert inside `solution_anchor`, which is usually a solution-box cell from `document.json` `solution_boxes[].fill_anchor`.
  - `after_source`: insert after the task when the manual has no solution box.
  - `append`: add to an appendix.
- Anchors must be copied exactly, including the `#fingerprint`. If the manual changes, its fingerprint no longer matches and LabPilot rejects the anchor rather than writing to the wrong place.
- Question ids and task ids must be unique. `depends_on` may only point to earlier tasks.
- `placement: "end"` adds a closing section after the last task, for example a cluster teardown that the introduction promises. `appendix_title` names the appendix that holds unplaced work.
- Every inspected task part must appear in the plan. If a part really doesn't apply, pass `--allow-uncovered` and explain why in `notes`.

## cells.json

```json
{
  "schema_version": 1,
  "cells": [
    {"id": "t1a-imports", "task_id": "T1.A", "setup": true, "expect_output": false,
     "source": "import warnings\nwarnings.filterwarnings('ignore', category=DeprecationWarning)\nwarnings.filterwarnings('ignore', category=FutureWarning)\nimport pandas as pd"},
    {"id": "t1a-load", "task_id": "T1.A",
     "source": "df = pd.read_csv('sales.csv')\nprint(df.dtypes)\nprint('rows:', len(df))"}
  ],
  "approved_security_rules": []
}
```

Rules for cell code:
- GPU code must also run on CPU: `device = "cuda" if torch.cuda.is_available() else "cpu"` and `.to(device)`, never a bare `.cuda()` or `"cuda"`. LabPilot may fall back to CPU when Colab has no free GPU.
- Files are staged in the kernel's working directory. Use relative names (`'sales.csv'`), never `/content/...` or absolute paths. Write outputs (for example Parquet) to relative paths too.
- Do not use `files.upload()`, `input()`, Drive mounts, or credential or environment dumps. These are denied.
- Shell (`!pip ...`, `subprocess`) and network use trigger warnings (allowed; recorded in the report).
- Make every required result visible with `print`, `display`, `df.show()`, `plt.show()` or `explain()`. A cell that is expected to show output but prints nothing counts as missing evidence. Set `"expect_output": false` only for cells that truly display nothing.
- Prove numeric claims with `assert` (use a tolerance for floats). Example: `assert abs(matched + unmatched - total) < 1e-6`.
- Keep the manual's `!pip install` / `!apt-get` lines in the first cell of the first task; they run inside Colab.
- In that first cell, silence only library `DeprecationWarning` and `FutureWarning` (as in the example) unless the
  manual asks about warnings, so long warning lines stay out of the screenshots. Real errors stay visible.
- Print rounded values (`f"{x:.3f}"`) unless the manual needs full precision.
- Keep the starter code from the manual. Fill in only what is missing. Write at the course's level.
- Check cells should print readable sentences ("Both runs gave the same totals.") rather than `KEY = value` lines. End a cell with `_ = helper(...)` when the last call returns a status code, so that no stray `Out[ ]: 0` appears in the screenshot.
- Cells execute in order in one kernel. Every `execute` starts a fresh kernel and replays from the first cell, so do not rely on state from an earlier attempt.

## labpilot finish

After `labpilot plan` has accepted the plan, `finish` runs the remaining mechanical steps in order:
build-notebook → preflight → execute on Google Colab → capture → answers → export (which runs `evaluate`).
It skips every step whose output is already current (unchanged code that passed in the current attempt,
screenshots from the current attempt, answers for the current attempt, a COMPLETED export), and stops at the
first step that needs you or the user.

```bash
labpilot finish <id> --cells cells.json --allow-cloud --allow-upload --json   # first call: stops at "answers"
labpilot finish <id> --answers answers.json --json                            # writes and checks the Word copy
```

The JSON summary:

```json
{"ok": true, "run_id": "...", "verdict": "COMPLETED | PARTIAL | NOT_FINISHED",
 "steps": [{"step": "execute", "status": "done | skipped | failed | waiting", "...": "..."}],
 "output_dir": "<lab>/output/<name>-<run-id>", "docx": "..._Completed.docx", "failed_checks": {},
 "runtime_release": null, "next_action": {"actor": "agent | user | none", "action": "...", "detail": "..."}}
```

Do what `next_action` says, then run `finish` again with whatever you changed (`--cells` or `--answers`).
Exit code 0 means COMPLETED; 6 means it stopped and `next_action` tells you why.

Keep your context small: read the summary only. Do not print whole notebooks, logs or `status` dumps; when you
need the outputs, read the text outputs of `notebook/executed.ipynb` (skip embedded images), and send any long
tool output to a file and read just the part you need.

## Repair loop

When `finish` stops with `action: "repair"`, the execute step lists `failed[]`. For each item, read `error_code`,
`error_type`, `error_message` and `traceback_tail`, then fix that cell:

| error_code | What to do |
|---|---|
| CODE_SYNTAX / CODE_RUNTIME | Fix the code. |
| MISSING_PACKAGE | Add the manual's `!pip install` to the setup cell, or use a library Colab has. |
| MISSING_DATA | Check the file name. Never fabricate the file. |
| TIMEOUT | Simplify the code or pass `--timeout` to `labpilot execute`. |
| RUNTIME_DISCONNECTED | Run `finish` again; it reruns transient failures. |

Run `finish --cells cells.json` again. Changed cells become `pending` and every later cell becomes `stale`; a failed
cell whose code did not change is not sent to Colab again. Each change to a failed cell uses one repair. When the
budget (default 3) runs out, stop and report to the user. Every execution starts a fresh Colab runtime and replays
the cells in order, because later cells need the earlier cells' state.

## colab-mcp (host-mediated, experimental)

1. `labpilot finish <id> --backend colab-mcp --allow-cloud --allow-upload --json` stops with
   `action: "record-execution"` and writes `host_actions.json`.
2. Make sure the colab-mcp tools are listed in your session. If they are not, report `MCP_UNAVAILABLE`.
3. Upload the files and check their sha256. Then run the cells in order and collect each cell's real nbformat outputs.
4. Write `result.json` (`HostExecutionRecord` in `host_actions.json` `result_schema`), run
   `labpilot record-execution <id> --file result.json`, then `labpilot finish <id>` again.

## answers.json

```json
{
  "schema_version": 1,
  "answers": [
    {"question_id": "T1.A.Q1", "text": "The file has 1,204 rows.",
     "source_cell_ids": ["t1a-load"], "verification": "verified"}
  ],
  "task_summaries": [
    {"task_id": "T1.A", "explanation": "I loaded the CSV with pandas and checked the column types and row count."}
  ]
}
```

- Write every answer and summary as described in *How to write* below.
- `task_summaries` are optional: add one only where the manual asks for an explanation or it genuinely helps.
- `table_cells`: values the manual asks you to copy into its tables, as `{"anchor": "<from document.json fill_in_tables>", "text": "28", "source_cell_ids": ["..."]}`.
- `verified`: every cited cell passed in the current attempt.
- `conceptual`: only for questions of `kind: conceptual`.
- `not_verified`: the data or runtime was unavailable. The text should say what is missing.

## How to write

`answers.json`, `task_summaries` and the text cells print read as the student's own lab report.

- Write as the student: plain, direct, at the course's level. Answer the question in the first sentence. Length
  follows the question: one or two sentences for a simple one. Vary the shape; no template repeated per answer.
- Stick to the lab's subject. The tool, the agent, "the student", identity fields, submission, Colab or runtime
  details and how the answers were produced belong in chat, not in the document.
- Sections only where the manual has them: no setup/environment or submission/deliverables section of your own.
- Numbers the way a student writes them: 2-3 significant figures or the manual's precision (0.18 s, speedup 1.01).
  Exact values only where the manual asks for them.
- At most one short limitation, and only when the question asks for interpretation.
- Formulas in plain text: "Speedup = 1.60 / 0.40 = 4". Word shows LaTeX (`\frac`, `$...$`) raw.
- Accuracy wins: every number comes from the executed outputs, and style never changes a result.
- Clean writing is the goal, not disguise: no deliberate typos or invented mistakes.

## Resume and inputs

- `labpilot finish <id> --json` continues from the last consistent checkpoint: it rechecks input hashes, re-stages
  new or changed datasets, and replays, captures and exports only what is no longer current.
- `labpilot status <id> --json` shows the state, failures, blockers and `next_action`.
- If the manual itself changed, start a new run.
