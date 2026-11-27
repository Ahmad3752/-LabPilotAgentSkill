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
| standing | Commit and push to `Ahmad3752/-LabPilotAgentSkill` after each tested step. | User preference. |

## Waiting on something

- **Upstream PR** `deeznust42/nutslms-mcp` from `Ahmad3752/nutslms-mcp:fix/legacy-renegotiation` (sets OpenSSL `0x40000`, plus a test). The user opened the PR; it is not merged yet. **Once it is merged**, delete the workaround: `src/labpilot/lms_openssl.cnf`, the `OPENSSL_CONF` parts of `nuts_lms()` in `doctor.py` and their test in `tests/unit/test_lms.py`, the note in `skills/labpilot/references/lms.md` step 3, and the TLS sentence in `IMPLEMENTATION_STATUS.md`. Then re-register `moodle` without `-e OPENSSL_CONF`.
- **The next Colab lab on the LMS.** Lab 11 (the only new lab) is Kafka with Docker and tmux, so it is not a Colab lab. LMS downloads and `lms stage` stay unverified until a Colab lab appears.
- **CI** has never been confirmed green: `gh` is not installed here, so the user checks the Actions tab.

## Next steps (in order; proposed unless marked agreed)

1. The user's real-world test (agreed): a fresh Codex or Claude Code session started in `C:\Labs` runs the LMS flow from the README ("any new labs?" → "do Lab 1"); Lab 1 is a closed Colab lab, fine for testing. That verifies the one-prompt flow, Codex, LMS downloads and `lms stage`. Codex needs `moodle` registered in its own config with `OPENSSL_CONF` to see the LMS.
2. Grilling questions still open: is v1 done when a classmate on Windows gets a Completed lab from one prompt in Claude Code? Is the audience the user plus classmates via GitHub?
3. Check that the skill is discovered in a fresh Claude Code session (still unverified; any new session can check it).

## Machine gotchas (this Windows 11 PC)

- Tests: the default temp dir is blocked. Pass `--basetemp <scratch dir>` to pytest. Expect 112 passed and 2 skipped (opt-in Colab, symlinks).
- Smart App Control blocks unsigned `.exe` launchers, so `moodle-mcp` runs as `"%APPDATA%\uv\tools\nutslms-mcp\Scripts\python.exe" -m moodle_mcp`.
- The Colab CLI 0.8.0 is installed in a LabPilot-owned venv and signed in. `labpilot doctor --json` shows it.
- `gh` is not installed, so pull requests are opened in the browser.
- `docs/INTERVIEW_PREP.md` is the user's personal interview prep. It is deliberately left uncommitted.

## Session log (newest first; keep the last five)

- **2026-11-27:** The LMS is now the main way to start a lab in the README ("any new labs?" → pick one → fetch → Colab check → solve). `doctor` prints the LMS registration for Codex and Claude with real paths (the `~` redaction is fixed), a valid PowerShell login command (`& "…python.exe"`), and finds `moodle-mcp` even when uv's bin is not on PATH. Naming a lab ("do Lab 1") uses `--include-past`.

- **2026-11-27:** First full clean run of `install.ps1`, which works. Rewrote the README quick start so a friend can follow it from a terminal: `-ExecutionPolicy Bypass`, a new terminal after the PATH change, starting the agent in `C:\Labs`, both Codex and Claude, and a note that ChatGPT cannot run it.

- **2026-11-27:** Added the Colab check (`colab_fit` in `init`/`inspect`, plus `labpilot colab-check`), calibrated on the real CS-412 Labs 1–11, which all came out correct. The skill now checks Colab first and asks "should I do it?". Fixed the LMS download instruction: `dest` must be a file path.

- **2026-11-27 (after restart):** The `moodle` MCP works with the TLS workaround. Ran the first real read-only `lms check`: 19 assignments, 1 new lab (Lab 11), all classified correctly. Nothing downloaded or submitted.

- **2026-11-27:** Diagnosed the LMS TLS failure. Added the `lms_openssl.cnf` workaround with a `doctor` check, re-registered `moodle`, and verified login and the course list with the real account. Prepared the upstream PR branch. Added this file.
- **2026-11-26:** Wrote the README from the status file. Removed the WSL path. Wrote the interview prep doc and ran grilling rounds on what to do next.
- **2026-11-26:** Added `labpilot clean` and `labpilot lms` (Nuts LMS MCP).
