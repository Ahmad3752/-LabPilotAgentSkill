#!/usr/bin/env bash
# Install LabPilot for the current user on macOS/Linux (no sudo, no global changes).
#   bash scripts/install.sh [--agent codex|claude|both|none] [--scope project|user]
#                           [--project-dir DIR] [--with-browser] [--yes]
set -euo pipefail

AGENT="claude"; SCOPE="project"; PROJECT_DIR="$(pwd)"; WITH_BROWSER=0; YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --agent) AGENT="$2"; shift 2 ;;
    --scope) SCOPE="$2"; shift 2 ;;
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --with-browser) WITH_BROWSER=1; shift ;;
    --yes) YES=1; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${LABPILOT_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/labpilot}"
BIN="$HOME/.local/bin"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11+ not found. Install it (e.g. brew install python@3.12 / apt install python3.12 python3.12-venv) and rerun." >&2
  exit 3
fi
echo "Using Python: $PY"

if [ ! -x "$ROOT/venv/bin/python" ]; then
  "$PY" -m venv "$ROOT/venv" || { echo "venv creation failed (Debian/Ubuntu: apt install python3-venv)" >&2; exit 3; }
fi
"$ROOT/venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip >/dev/null
"$ROOT/venv/bin/python" -m pip install --disable-pip-version-check --upgrade "$REPO[lab]"

mkdir -p "$BIN"
cat > "$BIN/labpilot" <<EOF
#!/usr/bin/env sh
exec "$ROOT/venv/bin/python" -m labpilot "\$@"
EOF
chmod +x "$BIN/labpilot"
echo "Command: $BIN/labpilot"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: $BIN is not on PATH; add it in your shell profile or call $BIN/labpilot." ;; esac

if [ "$WITH_BROWSER" -eq 0 ] && [ "$YES" -eq 0 ]; then
  printf "Download Playwright's Chromium (~150 MB, needed for screenshots)? [Y/n] "
  read -r ans || ans=""
  case "$ans" in ""|y|Y|yes) WITH_BROWSER=1 ;; esac
fi
if [ "$WITH_BROWSER" -eq 1 ]; then "$ROOT/venv/bin/python" -m playwright install chromium; fi

if [ "$AGENT" != "none" ]; then "$BIN/labpilot" setup --agent "$AGENT" --scope "$SCOPE" --project-dir "$PROJECT_DIR"; fi
"$BIN/labpilot" doctor || true
echo
echo "Done. Open your lab folder in your agent and ask:"
echo "  Use LabPilot to work through the lab in this folder, run the code, collect screenshots, and prepare the original Word document with answers for my review."
