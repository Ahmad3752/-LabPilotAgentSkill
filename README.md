# LabPilot

**An agent skill and CLI that works through a Google Colab lab manual (.docx): it runs the code for real, captures real screenshots, and fills a copy of the original Word file with the evidence and grounded answers.**

Your own coding agent (Claude Code or Codex) does the reasoning. LabPilot is the deterministic layer that executes, records and writes the document, and it never invents outputs, numbers or screenshots. No separate LLM API key is needed.

> Status: working on Windows 11 with Google Colab (official `google-colab-cli`), and verified end to end on a real course lab. See [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) for exactly what is verified and what is not.

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
Google Colab (google-colab-cli): all lab code runs here
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

## Quick start in a chat app (Claude.ai or ChatGPT, nothing to install)

1. Download the lab manual (`.docx`) and its data files from the LMS yourself.
2. Open Claude.ai (code execution on, with network access for github.com and pypi.org) and upload the skill once:
   zip the `skills/labpilot` folder and add it under Settings > Capabilities > Skills. Then, in a new chat,
   upload the lab files and say: *"Use LabPilot to do this lab."*
3. Reply **yes** with your name and CMS ID when it asks.
4. It gives you a notebook. Open [colab.research.google.com](https://colab.research.google.com), **File > Upload
   notebook**, **Runtime > Run all**, choose your lab files when asked, then **File > Download > Download .ipynb**
   and upload that file back to the chat.
5. It writes the answers and gives you the completed Word file.

Your code runs in your own Google Colab, in your browser. The chat never asks for your LMS or Google password.
LMS discovery is not available in this mode. ChatGPT is not tested yet (its sandbox usually cannot download
LabPilot).

## Quick start (Windows 11, Codex or Claude Code)

You need:
- **Python 3.12+ from python.org** (tick "Add python.exe to PATH"). It must be a signed build, because Windows Smart App Control blocks unsigned ones.
- **Git**, and **Codex** or **Claude Code** signed in. The ChatGPT website cannot run LabPilot, because it cannot run programs on your computer.
- A Google account for Colab.

**Install and start** (PowerShell, about 10 minutes, once). `-ExecutionPolicy Bypass` lets this one script run without changing your settings:

```powershell
git clone https://github.com/Ahmad3752/-LabPilotAgentSkill.git LabPilot
cd LabPilot
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Agent both -ProjectDir C:\Labs -InstallBrowser -AddToPath -Yes
```

The script does everything in order and stops only where you have to type something yourself:

1. Installs LabPilot, the screenshot browser, the Google Colab CLI and the skill for Codex and Claude Code.
2. **Google sign-in:** it opens a link. Approve it and paste the code back into the terminal.
3. **NUST LMS sign-in:** it installs the [Nuts LMS MCP](https://github.com/deeznust42/nutslms-mcp) and asks for your LMS username and password. That prompt belongs to the LMS tool: LabPilot never sees or stores your password. It then connects the LMS to Codex and Claude Code.
4. It opens Codex in `C:\Labs` already asking: *"Use LabPilot: are there any new labs on the LMS?"*

The agent lists your labs and says which ones can be done in Google Colab. Answer with the one you want, for example "do Lab 1" (older labs work too). It then asks one question: **yes** plus your name and CMS ID. After that it downloads the lab, runs it on Colab and fills the Word copy without asking again, posting a one-line status per phase. Your name and CMS ID are saved on your PC (`labpilot init --student-name ... --student-id ...` changes them), so later labs only ask for yes. Rerunning the script skips whatever is already done. Add `-NoLms` if you are not at NUST.

**Next time**, open a new PowerShell and run:

```powershell
cd C:\Labs
codex          # or: claude
```

`labpilot doctor` checks the setup at any time and prints the next command for anything missing.

Without the LMS, put the manual (`.docx`) and its data files in a folder such as `C:\Labs\Lab 1\` and ask:

> Use LabPilot to do the lab in the "Lab 1" folder on Google Colab.

Either way, the agent first checks whether the lab can be done in Colab, and stops with the reason if not (e.g. an AWS or Docker lab). **In Codex:** the first time Codex opens `C:\Labs` it asks whether to trust the folder. Answer yes: that is where LabPilot's setup tells Codex to let `labpilot` run outside its sandbox (the "Approve for me" mode). If Codex still says LabPilot cannot run, type `/permissions` and pick **Approve for me**. LabPilot never submits anything: you review the Word file and submit it yourself.

### Try it without an agent

```bash
python examples/run_demo.py <empty-folder>                       # local kernel (demo and tests only)
python examples/run_demo.py <empty-folder> --backend colab-cli   # real Colab, your account
```

This runs a synthetic sample lab through the real CLI with known agent files.

## What is supported

| Feature | Status |
|---|---|
| Checking first whether a lab can be done in Colab, and saying why not (e.g. AWS, Docker or terminal labs) | Verified on 11 real manuals |
| Reading `.docx` manuals (tasks, "Question N", answer boxes, fill-in tables, starter code, images) | Verified |
| Google Colab via `google-colab-cli` 0.8.0, native Windows | Verified on real Colab |
| Lab code on Colab only (no local fallback; sign-in required) | Verified with unit tests |
| `labpilot finish`: one command after the agent writes the plan, code and answers | Verified offline (demo, fake Colab CLI); not yet on real Colab |
| Running inside Codex without the sandbox blocking it (rule file + "Approve for me") | Verified by hand in real Codex; the automatic setup is unit-tested |
| Crash, cancel and missing-data recovery (`resume`) | Verified |
| Screenshots (headless Chromium renderings of the executed notebook) | Verified |
| Filling a copy of the Word manual, plus quality review (`evaluate`) | Verified |
| Checking the university LMS for new labs (Nuts LMS MCP, read-only) | Verified with a real account (finding labs); downloads not yet verified |
| One-command Windows installer (`scripts/install.ps1`): install, sign-ins, LMS connection, start the agent | Install part verified on a clean PC; the sign-in, LMS and start steps not yet |
| Google Colab through the browser (`colab-mcp`) | Experimental, not yet verified |
| Chat apps (Claude.ai): the student runs LabPilot's notebook in their own browser Colab (`--backend colab-web`) | Unit-tested; not yet tried in a real Claude.ai chat |
| One question up front (yes + name/CMS ID), then no more questions; name/ID saved on your PC | Skill rule; saving unit-tested; not yet tried in a real run |
| A full lab from one prompt in Codex | Next test |
| macOS, Linux | Implemented, not yet verified |

Screenshots are renderings of the executed notebook, not captures of the Colab web page. If a manual requires native Colab screenshots, LabPilot reports that as unmet instead of faking it.

## Privacy and academic integrity

- LabPilot never submits anything, never logs in to the LMS for you and never handles your passwords.
- Files go to Colab only with your consent. Nothing is sent anywhere else. Your saved name and CMS ID stay in LabPilot's config file on your PC.
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
