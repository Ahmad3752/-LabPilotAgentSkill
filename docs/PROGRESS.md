# Session handoff

Where the work stands, what was decided and why, what is waiting, and what comes next.
What is verified lives in [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md); history lives in `git log`. This file holds only what neither records.

Last updated: 2026-11-28.

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
| 2026-11-27 | Ask once, at discovery: one message with the lab, the Colab verdict and sign-in state, asking for "yes + name + CMS ID"; the agent ends its turn there. The yes is the Colab consent for that run (`--allow-cloud --allow-upload`); after it, no more questions until COMPLETED or a blocker only the user can fix. One status line with a percentage per phase. | Codex Lab 3 run: the agent asked mid-work, kept going, hit CONSENT_REQUIRED after 4 minutes and waited while the user was away. Still no standing consent and no fetching without asking. |
| 2026-11-27 | The student name and CMS ID are saved once in the local user config (`labpilot init --student-name/--student-id`; new values overwrite) and reported by `doctor --json`. Never sent anywhere. | The Lab 3 report had blank name/ID fields; the user wants to give them once ever. |
| 2026-11-27 | Writing rules in the skill (*How to write* in `references/workflow.md`): the student's own voice, answer first, short answers, rounded numbers, plain-text formulas, no meta text or own setup/submission sections, deprecation warnings silenced. No detector-evasion tricks. | The Lab 3 report read like a bot wrote it. |
| 2026-11-27 | Terminal or Docker labs (like Lab 11, Kafka) are out of scope for now. | The user will discuss them later. Feasible with tmux `send-keys`/`capture-pane`, but it needs a Linux and Docker host, which reopens "Colab only". |
| 2026-11-27 | One-command setup: `install.ps1` installs everything, runs the Google and LMS sign-ins (each tool's own prompt in the user's terminal), connects the LMS to Codex and Claude Code, and opens the agent asking for new labs. | User: "as soon as a person downloads it, it configures moodle-mcp, logins, Colab, and starts". LabPilot still never reads or stores a password. |
| 2026-11-27 | Lab code runs only on Google Colab. Not signed in means the user signs in first; no local option. The local kernel stays only for tests and the offline demo (`LABPILOT_ALLOW_LOCAL=1`). | First real Codex run: local pandas was blocked by Windows Application Control. User: "lab code should be in colab". |
| 2026-11-27 | Codex runs `labpilot` outside its sandbox via a LabPilot-owned rules file plus the "Approve for me" mode, not by copying Python or Docker. | Codex's Windows sandbox cannot read per-user installs or the Colab sign-in; the user rejected copying Python as a hack and Docker does not fix the sandbox. Verified by hand in real Codex. |
| 2026-11-28 | Chat app mode (branch `feat/cloud-mode`): Claude.ai/ChatGPT does the reasoning in its sandbox, the student runs LabPilot's notebook in their own browser Colab (one click, Run all) and uploads the downloaded .ipynb. No LMS there; the student uploads the lab files. Never ask for a password in chat. | A client demo of the local install failed (heavy downloads, Colab sign-in failure). The user and the client want no local install. The sandbox cannot reach the student's browser, and browser-driving agents were judged too fragile for a demo. |
| standing | Commit and push to `Ahmad3752/-LabPilotAgentSkill` after each tested step. | User preference. |

## Waiting on something

- **Upstream PR** `deeznust42/nutslms-mcp` from `Ahmad3752/nutslms-mcp:fix/legacy-renegotiation` (sets OpenSSL `0x40000`, plus a test). The user opened the PR; it is not merged yet. **Once it is merged**, delete the workaround: `src/labpilot/lms_openssl.cnf`, the `OPENSSL_CONF` parts of `nuts_lms()` in `doctor.py` and their test in `tests/unit/test_lms.py`, the note in `skills/labpilot/references/lms.md` step 3, and the TLS sentence in `IMPLEMENTATION_STATUS.md`. Then re-register `moodle` without `-e OPENSSL_CONF`.
- **The next Colab lab on the LMS.** Lab 11 (the only new lab) is Kafka with Docker and tmux, so it is not a Colab lab. LMS downloads and `lms stage` stay unverified until a Colab lab appears.
- **CI** has never been confirmed green: `gh` is not installed here, so the user checks the Actions tab.

## Next steps (in order; proposed unless marked agreed)

0. **Test chat app mode in a real Claude.ai chat** (agreed, on `feat/cloud-mode`; the user tests too). Check in this order: `pip install` from GitHub works in the sandbox; `colab-check`/`init` run; the handoff notebook opens in Colab and Run all + upload cell work; the downloaded .ipynb is accepted by `finish --executed` (cell sources unchanged by Colab); screenshots (Chromium in the sandbox, or `playwright install chromium`); the Completed .docx downloads. If Chromium cannot be installed there, screenshots need a no-browser fallback.
1. **Re-test Lab 2 in Codex with one prompt and time it** (agreed). Note the Colab per-call time in `events.jsonl` (batching landed 2026-11-27). Rerun `install.ps1` (or `labpilot setup --agent both --project-dir C:\Labs`) so the rules file and `C:\Labs\.codex\config.toml` are written, trust `C:\Labs` when Codex asks, then one prompt. Success = no user commands, `Lab_2_Completed.docx`, well under the first run's 35 minutes. This also verifies `labpilot finish` and the release check on real Colab.
2. Grilling questions still open: is v1 done when a classmate on Windows gets a Completed lab from one prompt? Is the audience the user plus classmates via GitHub?

## Machine gotchas (this Windows 11 PC)

- Tests: the default temp dir is blocked. Pass `--basetemp <scratch dir>` to pytest. Expect about 177 passed and 2 skipped (opt-in Colab, symlinks).
- Smart App Control blocks unsigned `.exe` launchers, so `moodle-mcp` runs as `"%APPDATA%\uv\tools\nutslms-mcp\Scripts\python.exe" -m moodle_mcp`.
- The Colab CLI 0.8.0 is installed in a LabPilot-owned venv and signed in. `labpilot doctor --json` shows it.
- `gh` is not installed, so pull requests are opened in the browser.
- `docs/INTERVIEW_PREP.md` is the user's personal interview prep. It is deliberately left uncommitted.

## Session log (newest first; keep the last five)

- **2026-11-28:** Chat app mode on branch `feat/cloud-mode`: new `colab-web` backend (`adapters/colab_web.py`), `finish --backend colab-web` hands out `for_colab.ipynb`, `finish --executed <file>` reads the student's Colab run; skill `references/chat-apps.md`. Unit-tested only.

- **2026-11-28:** GPU labs, agreed with the user: the one question asks for a free T4 "if needed" on ML labs (`--allow-gpu`); with no GPU free, fall back to CPU (cells must be device-agnostic). Detection is strong/weak/negated, the runtime is checked with `nvidia-smi`, a CPU run that fails like a GPU lab switches to T4 once. Unit-tested only; next: one real T4 run (e.g. Lab 10, In-Context Learning).

- **2026-11-28:** `finish` returns a `closing_message` ("All done, your lab is complete! Hope you liked it. With love from VellaGang <3") only when the lab is COMPLETED; the skill ends its final message with it.

- **2026-11-27:** Faster Colab runs and visible progress. Measured on real Colab: each `colab exec` call costs ~4-14 s of fixed overhead (it varied between runs), while quick cells run in ~1 s, and every cell used two calls (the cell, then the `_labpilot_marker` cell). `colab exec` keeps going after a cell raises (read from the 0.8.0 source and confirmed), so batching needed a kernel-side guard: chosen design is up to 5 unchanged lab cells per call with a LabPilot control cell between them that reports each outcome, sets the marker only after a pass, and installs an IPython input transformer that blanks every cell after a failure (so state is untouched before the repair). Each control cell also `chdir`s to /content, as the one-call-per-cell flow did. Rejected: wrapping cells in `run_cell` from one control cell (changes outputs and tracebacks) and one call for all cells (no progress for minutes). Timings, same benchmark minutes apart: 19 quick cells 139.4 s / 38 calls before, 33.0 s / 4 calls after; a cell failing mid-batch gave identical records and kernel state. Sample lab on Colab: execute phase 43.9 s before, 28 s after (in a slower Colab period: ~13 s per call). Progress lines: `Colab: 7/19 cells done (36%) - <cell> passed` and `Step n/6: ...` from `finish`.

- **2026-11-27:** After the Codex Lab 3 run (asked mid-work, waited at CONSENT_REQUIRED, blank name/ID, bot-like text): the skill now asks once at discovery (yes + name + CMS ID), ends its turn, then runs to the end with percentage status lines; `init` saves the name/ID locally and `doctor --json` reports them; the skill has *How to write* rules and drops the draft plan's SETUP appendix. Not yet tried in a real run.
