# Where the lab code runs

LabPilot runs lab code **only on Google Colab**, with the user's own account. There is no local fallback.

| Backend | How | When | Notes |
|---|---|---|---|
| colab-cli (default) | `labpilot finish <id> --cells cells.json --allow-cloud --allow-upload` | Always, unless the user set up colab-mcp. `--backend auto` means colab-cli. | Runs every cell in a real Colab CPU runtime. Never allocate compute or upload files without the user's consent. Never request a GPU unless the lab needs one and the user agrees. LabPilot stops only the Colab sessions it created. |
| colab-mcp (experimental) | `labpilot finish <id> --backend colab-mcp ...` | Claude Code with the official colab-mcp server and a Colab tab connected. | Host-mediated: `finish` stops with `record-execution`; you run the cells through the colab-mcp tools, then `labpilot record-execution`, then `finish` again. Codex is not a documented colab-mcp client. |

The local Jupyter kernel exists only for LabPilot's own tests and offline demo. `--backend local` is refused.

## Colab-specific starter code (see `colab_features` in document.json)

| Starter code | What to do |
|---|---|
| `files.upload()` | Do not use it. LabPilot stages the files (it uploads them to `/content` in Colab), so load them by name, e.g. `pd.read_csv('sales.csv')`. |
| `/content/...` paths | Use plain relative names. Colab's working directory is `/content`, so relative names work there. |
| `!pip install ...` / `!apt-get ...` | Put them in the first cell (SETUP's if the manual has a setup section) exactly as the manual says. They run inside Colab. |
| `drive.mount()` | Never mount Drive. Ask the user to place the files in the lab folder. |

## When to stop and ask the user

`finish` puts the one action needed in `next_action`. When `actor` is `user`, tell the user exactly that.

| Situation | What to tell the user |
|---|---|
| Not signed in (`AUTH_REQUIRED`) | Run `labpilot colab-login` in your own terminal (Google's sign-in), then run the same command again. Never ask for passwords or tokens. |
| Colab CLI missing (`BACKEND_UNAVAILABLE`) | Run `labpilot setup --backend colab-cli --install-colab-cli`, then `labpilot colab-login`. |
| No consent yet (`next_action.action == "consent"`) | The user's yes from SKILL.md *Ask once* is the consent: rerun with `--allow-cloud --allow-upload`. Ask only if they never said yes. |
| Quota or paid entitlement needed (`QUOTA_EXHAUSTED`) | Explain and wait; run `finish` again later. Never buy anything. |
| `runtime_release` in the summary | LabPilot tried to release the Colab runtime but could not confirm it. `labpilot cancel <id>` retries. |
| The manual demands screenshots from the Colab web UI | LabPilot produces labelled renderings of the executed notebook, not Colab UI screenshots. Report this as unmet. |
