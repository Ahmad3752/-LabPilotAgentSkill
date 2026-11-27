# Session handoff

Where the work stands, what was decided and why, what is waiting, and what comes next.
What is verified lives in [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md); history lives in `git log`. This file holds only what neither records.

Last updated: 2026-11-27.

## Decisions (settled with the user; reopen only if the user does)

| Date | Decision | Why |
|---|---|---|
| 2026-11-25 | Colab labs only. No Docker or local Spark/Java profiles. | The lab's own setup cells run inside Colab. |
| 2026-11-26 | LMS: read-only new-lab discovery through the Nuts LMS MCP (`moodle-mcp`), host-mediated. | The user wants the skill to find new labs itself. LabPilot never logs in, handles passwords or submits. |
| 2026-11-26 | WSL path for the Colab CLI removed. | The Colab CLI 0.8.0 runs natively on Windows (verified on real Colab), so the WSL code was untested and unused. |
| 2026-11-26 | colab-mcp (browser Colab) skipped for now and labelled experimental. | It gives the same rendered screenshots as the CLI plus a browser tab to keep open. Revisit only if instructors require screenshots of the Colab web UI, which would be a new feature (native UI capture). |
| 2026-11-27 | LMS TLS error: fix upstream by PR, and meanwhile use a LabPilot-side workaround. | `lms.nust.edu.pk` needs legacy TLS renegotiation. A LabPilot LMS client was rejected because it would have to handle NUST credentials. |
| 2026-11-27 | Goal: a student uses it and the lab gets done, automated end to end, Colab only. The user's CS-412 course has no AI-assistance rule. | User statement. The final acceptance test is the user doing a real lab with the skill from Codex (and Claude Code). |
| 2026-11-27 | Colab check first: LabPilot decides from the Word manual whether a lab can be done in Colab. Not Python notebook code (Java programs, AWS or cloud consoles, Docker or terminal labs) means it tells the user and stops. | The user wants every lab to start with "can Colab do this?". The check was calibrated on Labs 1–11. |
| 2026-11-27 | The agent always asks "I found <lab>. Should I do it for you?" before fetching, and asks for Colab permission on every run. | User decision: no standing consent, and no fetching without asking. |
| 2026-11-27 | Terminal or Docker labs (like Lab 11, Kafka) are out of scope for now. | The user will discuss them later. Feasible with tmux `send-keys`/`capture-pane`, but it needs a Linux and Docker host, which reopens "Colab only". |
| 2026-11-27 | One-command setup: `install.ps1` installs everything, runs the Google and LMS sign-ins (each tool's own prompt in the user's terminal), connects the LMS to Codex and Claude Code, and opens the agent asking for new labs. | User: "as soon as a person downloads it, it configures moodle-mcp, logins, Colab, and starts". LabPilot still never reads or stores a password. |
| 2026-11-27 | Lab code runs only on Google Colab. Not signed in means the user signs in first; no local option. The local kernel stays only for tests and the offline demo (`LABPILOT_ALLOW_LOCAL=1`). | First real Codex run: local pandas was blocked by Windows Application Control. User: "lab code should be in colab". |
| 2026-11-27 | Codex runs `labpilot` outside its sandbox via a LabPilot-owned rules file plus the "Approve for me" mode, not by copying Python or Docker. | Codex's Windows sandbox cannot read per-user installs or the Colab sign-in; the user rejected copying Python as a hack and Docker does not fix the sandbox. Verified by hand in real Codex. |
| standing | Commit and push to `Ahmad3752/-LabPilotAgentSkill` after each tested step. | User preference. |

## Waiting on something

- **Upstream PR** `deeznust42/nutslms-mcp` from `Ahmad3752/nutslms-mcp:fix/legacy-renegotiation` (sets OpenSSL `0x40000`, plus a test). The user opened the PR; it is not merged yet. **Once it is merged**, delete the workaround: `src/labpilot/lms_openssl.cnf`, the `OPENSSL_CONF` parts of `nuts_lms()` in `doctor.py` and their test in `tests/unit/test_lms.py`, the note in `skills/labpilot/references/lms.md` step 3, and the TLS sentence in `IMPLEMENTATION_STATUS.md`. Then re-register `moodle` without `-e OPENSSL_CONF`.
- **The next Colab lab on the LMS.** Lab 11 (the only new lab) is Kafka with Docker and tmux, so it is not a Colab lab. LMS downloads and `lms stage` stay unverified until a Colab lab appears.
- **CI** has never been confirmed green: `gh` is not installed here, so the user checks the Actions tab.

## Next steps (in order; proposed unless marked agreed)

1. **Re-test Lab 2 in Codex with one prompt and time it** (agreed). Rerun `install.ps1` (or `labpilot setup --agent both --project-dir C:\Labs`) so the rules file and `C:\Labs\.codex\config.toml` are written, trust `C:\Labs` when Codex asks, then one prompt. Success = no user commands, `Lab_2_Completed.docx`, well under the first run's 35 minutes. This also verifies `labpilot finish` and the release check on real Colab.
2. Speed: measure Colab per-cell overhead (each cell is a separate CLI call plus a marker call) on that run, then decide on batching.
3. Grilling questions still open: is v1 done when a classmate on Windows gets a Completed lab from one prompt? Is the audience the user plus classmates via GitHub?

## Machine gotchas (this Windows 11 PC)

- Tests: the default temp dir is blocked. Pass `--basetemp <scratch dir>` to pytest. Expect about 140 passed and 2 skipped (opt-in Colab, symlinks).
- Smart App Control blocks unsigned `.exe` launchers, so `moodle-mcp` runs as `"%APPDATA%\uv\tools\nutslms-mcp\Scripts\python.exe" -m moodle_mcp`.
- The Colab CLI 0.8.0 is installed in a LabPilot-owned venv and signed in. `labpilot doctor --json` shows it.
- `gh` is not installed, so pull requests are opened in the browser.
- `docs/INTERVIEW_PREP.md` is the user's personal interview prep. It is deliberately left uncommitted.

## Session log (newest first; keep the last five)

- **2026-11-27:** The Word report read like a bot made it. Removed the "What the code does:" label and the run location from captions; 'deliverables'/'submission' mentions only warn now (they forced an invented "Submission preparation" section); new `writing` warnings for LaTeX, 5+ decimals and tool wording; Word equations are read (fingerprints unchanged); no invented SETUP appendix (only when the manual has a setup section). Screenshots: long lines wrapped at the 1280px viewport, so the image was fitted at 0.48 scale; they now wrap at 880px. Export renders review pages itself (`pages_dir`), with `pypdfium2` a required dependency.

- **2026-11-27:** First real Codex run (Lab 2) worked but took 35 minutes and 5 user commands, because Codex's sandbox could not start LabPilot and the agent improvised runners. Fixed in four parallel branches: Codex/Claude agent access (rules file, approval mode, doctor check, "never improvise" in the skill); Colab-only execution and `labpilot finish`; manual parsing (activities, Q-labelled questions, generated files, headings, SQL Server blocker, `needs_attention`); long code split into screenshot parts.

- **2026-11-27:** One-command install: `labpilot lms connect` (install, moodle-mcp's own login prompt, registration with Codex and Claude Code at user scope) and `install.ps1` now runs `colab-login`, `lms connect` and starts the agent with the first prompt. Doctor's LMS line says which agents are connected. The full one-command run on a clean PC is not verified yet.

- **2026-11-27:** The LMS is now the main way to start a lab in the README ("any new labs?" → pick one → fetch → Colab check → solve). `doctor` prints the LMS registration for Codex and Claude with real paths (the `~` redaction is fixed), a valid PowerShell login command (`& "…python.exe"`), and finds `moodle-mcp` even when uv's bin is not on PATH. Naming a lab ("do Lab 1") uses `--include-past`.

- **2026-11-27:** First full clean run of `install.ps1`, which works. Rewrote the README quick start so a friend can follow it from a terminal: `-ExecutionPolicy Bypass`, a new terminal after the PATH change, starting the agent in `C:\Labs`, both Codex and Claude, and a note that ChatGPT cannot run it.
