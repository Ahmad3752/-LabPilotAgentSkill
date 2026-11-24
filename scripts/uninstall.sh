#!/usr/bin/env bash
# Remove LabPilot-owned files only (skill copies it installed, MCP entries it registered, its venv and shim).
set -euo pipefail
SCOPE="${1:-project}"
ROOT="${LABPILOT_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/labpilot}"
BIN="$HOME/.local/bin/labpilot"
if [ -x "$BIN" ]; then "$BIN" uninstall --agent both --scope "$SCOPE" --project-dir "$(pwd)" || true; fi
if [ -f "$BIN" ] && grep -q "labpilot" "$BIN"; then rm -f "$BIN"; echo "Removed $BIN"; fi
if [ -d "$ROOT/venv" ]; then rm -rf "$ROOT/venv"; echo "Removed $ROOT/venv"; fi
echo "LabPilot removed. Lab folders and outputs were left untouched."
