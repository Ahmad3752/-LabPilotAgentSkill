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
| standing | Commit and push to `Ahmad3752/-LabPilotAgentSkill` after each tested step. | User preference. |

## Waiting on something

- **Upstream PR** `deeznust42/nutslms-mcp` from `Ahmad3752/nutslms-mcp:fix/legacy-renegotiation` (sets OpenSSL `0x40000`, plus a test). The branch is pushed; the user opens the PR. **Once it is merged**, delete the workaround: `src/labpilot/lms_openssl.cnf`, the `OPENSSL_CONF` parts of `nuts_lms()` in `doctor.py` and their test in `tests/unit/test_lms.py`, the note in `skills/labpilot/references/lms.md` step 3, and the TLS sentence in `IMPLEMENTATION_STATUS.md`. Then re-register `moodle` without `-e OPENSSL_CONF`.
- **First real `labpilot lms check`.** `moodle` is re-registered (local scope) with `OPENSSL_CONF`, and a session started after 2026-11-27 picks it up. Run the LMS flow in `skills/labpilot/references/lms.md` read-only, then record the result in `IMPLEMENTATION_STATUS.md`.
- **CI** has never been confirmed green: `gh` is not installed here, so the user checks the Actions tab.

## Next steps (in order; proposed unless marked agreed)

1. Real `lms check` with the user's account (agreed).
2. `doctor` redacts the home folder to `~` inside the commands it prints, and a quoted `"~\AppData\..."` path fails when pasted into PowerShell. Print real paths in the `fix` commands.
3. Grilling questions still open: is v1 done when a classmate on Windows gets a Completed lab from one prompt in Claude Code? Is the audience the user plus classmates via GitHub?
4. A second real lab end to end, to prove the tool works beyond the Hadoop lab.
5. Check that the skill is discovered in a fresh Claude Code session (still unverified; any new session can check it).

## Machine gotchas (this Windows 11 PC)

- Tests: the default temp dir is blocked. Pass `--basetemp <scratch dir>` to pytest. Expect 103 passed and 2 skipped (opt-in Colab, symlinks).
- Smart App Control blocks unsigned `.exe` launchers, so `moodle-mcp` runs as `"%APPDATA%\uv\tools\nutslms-mcp\Scripts\python.exe" -m moodle_mcp`.
- The Colab CLI 0.8.0 is installed in a LabPilot-owned venv and signed in. `labpilot doctor --json` shows it.
- `gh` is not installed, so pull requests are opened in the browser.
- `docs/INTERVIEW_PREP.md` is the user's personal interview prep. It is deliberately left uncommitted.

## Session log (newest first; keep the last five)

- **2026-11-27:** Diagnosed the LMS TLS failure. Added the `lms_openssl.cnf` workaround with a `doctor` check, re-registered `moodle`, and verified login and the course list with the real account. Prepared the upstream PR branch. Added this file.
- **2026-11-26:** Wrote the README from the status file. Removed the WSL path. Wrote the interview prep doc and ran grilling rounds on what to do next.
- **2026-11-26:** Added `labpilot clean` and `labpilot lms` (Nuts LMS MCP).
- **2026-11-25:** Initial implementation through a real Hadoop lab on Colab: `caf0669`..`fa9d053`.
