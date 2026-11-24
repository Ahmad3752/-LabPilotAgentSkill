# Implementation status

This file is the source of truth for what has been **verified**, what is **implemented but not yet verified**, and what is **out of scope**. README claims must match it.

Last updated: 2026-11-25.

## Phase scope

This phase targets **Google Colab labs only**. Docker, local Spark/Java runtimes and other heavy local profiles were removed on purpose. A lab's own Colab setup cells (`!pip`, `apt-get`, Hadoop or PySpark installs) run inside Colab. The plain local Jupyter kernel remains only as a clearly labelled fallback.

## Verified (real runs on Windows 11 + Python 3.11/3.12, VS Code)

| Capability | How it was verified |
|---|---|
| Reading `.docx` manuals | Covered by unit tests on synthetic manuals and the real course Lab 8 manual. Verified behaviours: reading order, nested tables, stable anchors with fingerprints, Task / Part / "Question N" units, one "Your answer:" box per question, fill-in tables, starter code, questions, student fields, embedded images, Colab-specific starter code. |
| Dataset handling | Unit tests cover discovery, matching, hashing and schema checks: exact, case-insensitive and alias matches, ambiguous and missing files, corrupt files, schema mismatch, encodings, path traversal. |
| Plan, cells and answers validation | Unit and integration tests cover duplicate IDs, task order, dependency order, anchor fingerprints, grounded answers (cited cells must have passed) and fill-in table values. |
| Local fallback execution | Integration tests with a real kernel cover a persistent kernel per attempt, per-cell checkpoints, stop at the first error, repair → stale invalidation → replay, the retry budget, a hard wall-clock timeout (a Windows kernel interrupt alone did not stop `time.sleep`; found and fixed), output size limits and credential-scrubbed environments. |
| Recovery | Integration tests cover: cancel while running → `CANCELLED` → resume; abrupt kill of the whole process tree → stale lock recovered → resume; changed inputs detected → resume re-stages; missing dataset → `PARTIAL` → user adds the file → resume → `COMPLETED`. |
| Screenshots | Real headless Chromium element screenshots of the actual executed notebook. Scripts are stripped, JavaScript is disabled, the network is blocked and images are cropped to their content. Long outputs are split at line, row or image boundaries. Blank or undecodable images are detected. Each image has provenance and a sha256. Checked visually. |
| Word output | The original is never modified, and output is regenerated from it each time, so there is no duplication. Placeholders are removed but labels are kept. Exact-height and can't-split rows are relaxed. Images are inline and sized to the cell. Fill-in table cells are written. Structural validation checks text order, screenshots embedded byte for byte, original images, headers/footers and page setup. The `Completed` / `Partial` naming follows the completion contract. |
| Optional page rendering | Microsoft Word, run locally through COM (read-only, `-EncodedCommand`, no execution-policy change), converts to PDF and each page is rendered with `pypdfium2`. Pages were checked visually. LibreOffice is used when installed (not tested here). |
| **Google Colab via the official `google-colab-cli` 0.8.0, natively on Windows** | Steps verified on real Colab: sign-in with `labpilot colab-login`, CPU runtime allocation, dataset upload with remote sha256 check, per-cell execution with rich outputs (charts included), success decided from the output objects (the CLI exits 0 even on errors), screenshots, Word export, and release of the owned session ("No active sessions" afterwards). The synthetic sample lab completed with identical answers on Colab and on the local fallback. |
| Colab CLI installer | `labpilot setup --backend colab-cli --install-colab-cli` installs into a LabPilot-owned venv. It needs a **signed** Python 3.12+, because Windows Smart App Control blocks unsigned builds such as uv-managed Python; this is detected and explained. |
| Skill packaging | One canonical `skills/labpilot/SKILL.md`, which follows the Agent Skills spec. Copies are installed to `.agents/skills` (Codex) and `.claude/skills` (Claude Code) with an ownership marker and a hash. Reruns are idempotent, and user-created skills are never overwritten. |
| Security screening | Unit tests: deny rules (credentials, environment dumps, `files.upload()`, `input()`, Drive mounts, deleting root, home or system folders) and warn rules (shell, network, absolute paths). Secret redaction is also tested. |
| Lint, types, CI | `ruff check` and `mypy src` pass. The GitHub Actions workflow covers Windows and Linux on Python 3.11 and 3.12. **The CI workflow has not run yet**; check the Actions tab after the first push. |

## Implemented, not yet verified

| Capability | What is missing |
|---|---|
| Colab MCP (browser) bridge | The protocol is tested: host actions, then strict validation and ingest of host-reported outputs. It has not been tried with the real `colab-mcp` server, which needs registration in Claude Code and a connected Colab tab. Codex is not a documented colab-mcp client. |
| Colab CLI through WSL | Code paths exist (`--wsl-distro`). Untested, because native Windows works. |
| Codex skill discovery and the one-prompt flow | The skill is installed in `.agents/skills/labpilot`. A real Codex session in VS Code has not yet been run on a lab. |
| Claude Code skill discovery | Installed to `.claude/skills/labpilot`. Not exercised in a separate session. |
| macOS / Linux install scripts | Written (`scripts/install.sh`). Not run on a real Mac or Linux machine. |
| `scripts/install.ps1` full clean-install run | Written. The smoke test was interrupted and has not been rerun. |

## Known limitations (by design or upstream)

- Screenshots are renderings of the executed notebook, not captures of the Colab web UI. A manual that demands Colab UI screenshots is reported as unmet.
- The Colab CLI does not report Jupyter execution counters, so code screenshots show `In [ ]:`. LabPilot leaves them blank rather than inventing numbers.
- The local fallback isolates packages but not filesystem privileges.
- Labs that continue an earlier lab (for example Lab 8 needs Lab 7's setup) need that earlier manual, or its setup code, in the run.

## Out of scope in this phase

LMS integration, WhatsApp or other messaging, automatic submission, scheduled jobs, Docker and local Spark/Java profiles, native Colab UI capture.
