# Where the lab code runs

LabPilot targets **Google Colab labs**. Choose the backend in this order:

1. Use what the user explicitly asked for, and what the manual requires.
2. Use Google Colab if it is set up and the user consents.
3. Otherwise use the local fallback, and tell the user you did.

| Backend | Command | When | Notes |
|---|---|---|---|
| colab-cli | `preflight --backend colab-cli --allow-cloud --allow-upload` | The official `google-colab-cli` is installed and signed in. | Runs every cell in a real Colab CPU runtime, with the user's account. Never allocate compute or upload files without consent. Never request a GPU unless the lab needs one and the user agrees. |
| colab-mcp | `execute --backend colab-mcp` (after preflight) | Claude Code with the official colab-mcp server and a Colab tab connected. | Host-mediated: you run the cells through the colab-mcp tools, then `record-execution`. Codex is not a documented colab-mcp client. |
| local | `preflight --backend local` | Colab is unavailable. | Runs on the user's machine in a plain Jupyter kernel. The report labels it `local-jupyter`. Packages the lab installs with `!pip` must already exist locally. |

`--backend auto` picks `colab-cli` only when it is configured and cloud consent was given. Otherwise it picks `local`.

## Colab-specific starter code (see `colab_features` in document.json)

| Starter code | What to do |
|---|---|
| `files.upload()` | Do not use it. LabPilot stages the files (it uploads them to `/content` in Colab), so load them by name, e.g. `pd.read_csv('sales.csv')`. |
| `/content/...` paths | Use plain relative names. Colab's working directory is `/content`, so relative names work there and locally. |
| `!pip install ...` / `!apt-get ...` | Put them in the first setup cell exactly as the manual says. They run inside Colab. |
| `drive.mount()` | Never mount Drive. Ask the user to place the files in the lab folder. |

## When to stop and ask the user

| Situation | What to tell the user |
|---|---|
| Sign-in needed (`WAITING_FOR_AUTH`) | Run the official sign-in once in a terminal. See the error's `next_action`. Never ask for passwords or tokens. |
| Quota or paid entitlement needed | Explain, and offer the local fallback. Never buy anything. |
| The manual demands screenshots from the Colab web UI | LabPilot produces labelled renderings of the executed notebook, not Colab UI screenshots. Report this as unmet. |
