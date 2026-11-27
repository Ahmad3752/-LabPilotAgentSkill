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

Requires Python 3.11+. To use Google Colab you need a **signed** Python 3.12+ (python.org or Microsoft Store), because Windows Smart App Control blocks unsigned builds.

```powershell
git clone https://github.com/Ahmad3752/-LabPilotAgentSkill.git
cd -LabPilotAgentSkill
.\scripts\install.ps1 -Agent claude -ProjectDir C:\Labs -InstallBrowser -AddToPath
labpilot setup --agent claude --backend colab-cli --install-colab-cli --project-dir C:\Labs
labpilot colab-login        # one-time Google sign-in, in the official Colab CLI
labpilot doctor             # check everything is ready
```

Then put the manual and its datasets in a folder under `C:\Labs`, open that folder in Claude Code and ask:

> Use LabPilot to work through the lab in this folder, run the code in Colab, collect screenshots, and prepare the Word document for my review.

The agent asks before using Colab compute or uploading your files.

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
| Checking the university LMS for new labs (Nuts LMS MCP, read-only) | Implemented, not yet verified with a real account |
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
