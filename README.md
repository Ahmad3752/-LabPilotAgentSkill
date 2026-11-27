# LabPilot

**An agent skill and CLI that works through a Google Colab lab manual (.docx): it runs the code for real, captures real screenshots, and fills a copy of the original Word file with the evidence and grounded answers.**

Your own coding agent (Claude Code or Codex) does the reasoning. LabPilot is the deterministic layer that executes, records and writes the document, and it never invents outputs, numbers or screenshots. No separate LLM API key is needed.

> Status: working on Windows 11 with Google Colab (official `google-colab-cli`) and a local Jupyter fallback, and verified end to end on a real course lab. See [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) for exactly what is verified and what is not.

## How it works

```text
You: "Use LabPilot to do the lab in this folder."
        │
        ▼
Host agent (Claude Code / Codex) ── reads skills/labpilot/SKILL.md, plans tasks, writes cells and answers
        │  labpilot <step> --json
        ▼
labpilot CLI ── inspect .docx → validate plan → build notebook → execute → screenshots → fill Word copy → evaluate
        │
        ▼
Google Colab (google-colab-cli)  or  local Jupyter kernel (fallback)
```

Output, next to your lab files:

```text
output/<lab-name>-<run-id>/
  Lab_8_Completed.docx      # or Lab_8_Partial.docx if anything is missing
  Lab_8.ipynb               # the executed notebook
  screenshots/              # real renderings of the executed cells
  execution_report.json
  REVIEW.md                 # what was verified, what remains, how to resume
```

`Completed` is used only when every task ran, every screenshot is valid, every answer is backed by a cell that passed and the Word copy validates. Otherwise you get `Partial` with a list of what is missing, and `labpilot resume <run-id>` continues the run later.

## Quick start (Windows 11)

You need:
- **Python 3.12+ from python.org** (tick "Add python.exe to PATH"). It must be a signed build, because Windows Smart App Control blocks unsigned ones.
- **Git**, and **Codex** or **Claude Code** signed in. The ChatGPT website cannot run LabPilot, because it cannot run programs on your computer.
- A Google account for Colab.

**1. Install** (PowerShell, about 5 minutes). `-ExecutionPolicy Bypass` lets this one script run without changing your settings:

```powershell
git clone https://github.com/Ahmad3752/-LabPilotAgentSkill.git
cd -LabPilotAgentSkill
mkdir C:\Labs
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Agent both -ProjectDir C:\Labs -InstallBrowser -AddToPath -Yes
```

**2. Close PowerShell and open a new one** (so `labpilot` is on PATH), then set up Google Colab once:

```powershell
labpilot setup --agent both --backend colab-cli --install-colab-cli --project-dir C:\Labs
labpilot colab-login        # opens Google sign-in; paste the code it gives you back into the terminal
labpilot doctor             # the last line should say: ready: colab=True
```

**3. Do a lab.** Put the manual (`.docx`) and its data files in their own folder, for example `C:\Labs\Lab 1\`. Then start your agent **from `C:\Labs`**, where the skill is installed:

```powershell
cd C:\Labs
codex          # or: claude
```

and ask:

> Use LabPilot to do the lab in the "Lab 1" folder on Google Colab.

The agent first checks whether the lab can be done in Colab (it stops and says why if not, e.g. an AWS or Docker lab), then asks before using Colab and uploading your files. In Codex, approve its requests to run commands with network access: Colab needs the internet.

### Try it without an agent

```bash
python examples/run_demo.py <empty-folder>                       # local kernel
python examples/run_demo.py <empty-folder> --backend colab-cli   # real Colab, your account
```

This runs a synthetic sample lab through the real CLI with known agent files.

## What is supported

| Feature | Status |
|---|---|
| Checking first whether a lab can be done in Colab, and saying why not (e.g. AWS, Docker or terminal labs) | Verified on 11 real manuals |
| Reading `.docx` manuals (tasks, "Question N", answer boxes, fill-in tables, starter code, images) | Verified |
| Google Colab via `google-colab-cli` 0.8.0, native Windows | Verified on real Colab |
| Local Jupyter fallback | Verified |
| Crash, cancel and missing-data recovery (`resume`) | Verified |
| Screenshots (headless Chromium renderings of the executed notebook) | Verified |
| Filling a copy of the Word manual, plus quality review (`evaluate`) | Verified |
| Checking the university LMS for new labs (Nuts LMS MCP, read-only) | Verified with a real account (finding labs); downloads not yet verified |
| Windows installer (`scripts/install.ps1`) | Verified with a clean install |
| Google Colab through the browser (`colab-mcp`) | Experimental, not yet verified |
| Codex, macOS, Linux | Implemented, not yet verified |

Screenshots are renderings of the executed notebook, not captures of the Colab web page. If a manual requires native Colab screenshots, LabPilot reports that as unmet instead of faking it.

## Privacy and academic integrity

- LabPilot never submits anything, never logs in to the LMS for you and never handles your passwords.
- Files go to Colab only with your consent. Nothing is sent anywhere else.
- It stops if the manual or your course forbids AI assistance.
- The result is a draft for **your review**: read it, understand it, personalise the answers and follow your course's rules.

## Development

```bash
uv venv .venv --python 3.11 && uv pip install --python .venv -e ".[lab,dev]"
.venv/Scripts/python -m pytest tests
.venv/Scripts/ruff check src tests examples
.venv/Scripts/mypy src
```

Contributor rules are in [AGENTS.md](AGENTS.md). The full specification is [COMPLETE.md](COMPLETE.md).

## License

MIT, see [LICENSE](LICENSE).
