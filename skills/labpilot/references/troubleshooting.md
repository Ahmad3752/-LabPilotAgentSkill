# Troubleshooting (error codes)

| Code | Meaning | Action |
|---|---|---|
| DOCX_UNSUPPORTED / DOCX_ENCRYPTED / DOCX_CORRUPT | The manual is not a readable .docx file. | Ask the user to save it as .docx and remove any password. |
| DATASET_MISSING / DATASET_AMBIGUOUS | A required file is absent, or more than one file matches. | Ask the user to add the file, or map it with `inspect --alias EXPECTED=ACTUAL`. Then `resume`. |
| DATASET_SCHEMA_MISMATCH / DATASET_CORRUPT | The file's columns or contents are not what the task expects. | Report expected vs actual. Never edit the source data. |
| PLAN_INVALID / CELLS_INVALID / ANSWERS_INVALID / RECORD_INVALID | The JSON you submitted failed validation. | Fix each problem listed in `error.details.problems`. |
| SECURITY_DENIED | The code uses a denied operation (credentials, interactive input, `files.upload`, Drive mount, destructive filesystem commands). | Rewrite the code. Use `--approve <rule>` only if the user explicitly approves it. |
| INPUT_CHANGED | The manual or a dataset changed after staging. | Run `resume` to re-stage. If the manual changed, start a new run with `init`. |
| RETRY_BUDGET_EXHAUSTED | A cell has used all its repair attempts. | Report this to the user. Export the result as partial. |
| RUNTIME_DISCONNECTED | The kernel or Colab runtime was lost. | Run `resume`. It replays verified cells in a fresh runtime. |
| AUTH_REQUIRED / WAITING_FOR_AUTH | The user is not signed in to Google Colab. | The user runs `labpilot colab-login` in their own terminal, then you run the same command again. Never collect credentials. |
| BACKEND_UNAVAILABLE | The Colab CLI is missing, or `--backend local` was asked for (tests and the offline demo only). | `labpilot setup --backend colab-cli --install-colab-cli`, then `labpilot colab-login`. |
| QUOTA_EXHAUSTED | The Colab quota has run out. | Tell the user; run `finish` again later. There is no local fallback. |
| CONSENT_REQUIRED | Cloud compute or file upload needs the user's permission. | Pass `--allow-cloud` / `--allow-upload` (the user's up-front yes). Ask only if they never said yes. |
| MCP_UNAVAILABLE | The colab-mcp tools are not listed in the host agent. | Reconnect or restart the agent per colab-mcp's docs, or use the default colab-cli backend. |
| BROWSER_UNAVAILABLE | Chromium for Playwright is missing. | Run `python -m playwright install chromium`. |
| gpu-unavailable | The lab needs a GPU and Colab had none free for this account (free GPUs run out; the limit resets within about a day). | Tell the user to run the same `labpilot finish` command later. Do not change the cells. |
| EVIDENCE_INVALID / EVIDENCE_STALE | A screenshot is blank, a cell has no output, or the code changed after capture. | Fix the cell's output display, then re-execute and capture again. |
| ANCHOR_AMBIGUOUS | An anchor's text changed or doesn't match. | Re-inspect the document and copy the anchors again. |
| RUN_LOCKED | Another LabPilot process is using this run. | Wait for it to finish, or run `labpilot cancel <id>`. |

