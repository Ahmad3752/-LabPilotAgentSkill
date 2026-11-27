# AGENTS.md — working on the LabPilot repository

This file is for coding agents that change LabPilot's own code. To *use* LabPilot on a lab, follow `skills/labpilot/SKILL.md`.

- **Spec:** `COMPLETE.md` is the implementation contract.
- **Status:** `docs/IMPLEMENTATION_STATUS.md` is the source of truth for what is verified and what is not. Update it whenever you verify or change a capability. README claims must match it.
- **Handoff:** read `docs/PROGRESS.md` at session start: it holds settled decisions, items waiting on something, and the next steps. Update it in the same commit as each completed step.

## Layout
| Area | Where |
|---|---|
| Package | `src/labpilot` (CLI in `cli.py`) |
| Workflow steps | `orchestration.py` (steps), `report.py` (export/validate/status/resume) |
| Backends | `adapters/` |
| LMS new-lab checks (Nuts LMS MCP, host-mediated) | `lms.py`, `inputs.py` |
| Word documents | `documents/` |
| Datasets | `datasets/` |
| Notebook build and kernel runner | `notebooks/` |
| Screenshots | `evidence/` |
| Runtime profile | `profiles/python.toml` (local fallback kernel requirements) |
| The skill | `skills/labpilot/` is the one canonical copy. `labpilot setup` installs copies, and the wheel bundles it. |
| Examples | `examples/` holds synthetic sample labs and `run_demo.py` (the reproducible demo without an LLM) |

## Commands
```bash
uv venv .venv --python 3.11 && uv pip install --python .venv -e ".[lab,dev]"
.venv/Scripts/python -m pytest tests            # add --basetemp <dir> if your sandbox blocks the default temp dir
.venv/Scripts/ruff check src tests examples
.venv/Scripts/mypy src
python examples/run_demo.py <empty-dir>                       # full pipeline, local fallback kernel
python examples/run_demo.py <empty-dir> --backend colab-cli   # real Google Colab (your account)
```

## Rules
- Never fake execution, outputs, screenshots or test results.
  - Fake subprocess layers are allowed only in unit tests, and must be labelled as fakes.
- Do not add separate LLM API dependencies. The host agent does the reasoning.
- Subprocesses use argument lists only, never shell strings.
- Never collect credentials. Only stop or clean up resources that LabPilot owns.
- Do not commit private lab manuals, datasets, `.labpilot/` or `output/`.
- LMS: only read-only new-lab discovery through the Nuts LMS MCP (`moodle-mcp`), host-mediated: the agent calls the MCP tools and passes their JSON to `labpilot lms` (`lms.py`). LabPilot never logs in to the LMS, never handles LMS credentials and never submits. WhatsApp and other messaging are out of scope.
