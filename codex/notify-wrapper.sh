#!/bin/bash
# notify-wrapper.sh — fires on every Codex turn-end.
#
# Setup:
#   1. Copy to ~/.codex/notify-wrapper.sh
#   2. chmod +x ~/.codex/notify-wrapper.sh
#   3. In ~/.codex/config.toml set:
#        notify = ["/path/to/.codex/notify-wrapper.sh", "turn-ended"]
#   4. Export LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL
#      in your shell profile (or set them below).

# ── Optional: path to Codex Computer Use notify binary (macOS only) ──────────
# Set CODEX_NOTIFY_BINARY to the path of the original SkyComputerUseClient
# binary if you want desktop notifications to continue working alongside the
# Langfuse hook. Leave unset if you don't use Codex Computer Use.
#
# Example (adjust version/path as installed on your machine):
#   export CODEX_NOTIFY_BINARY="$HOME/.codex/computer-use/Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient"

if [ -n "${CODEX_NOTIFY_BINARY:-}" ] && [ -x "$CODEX_NOTIFY_BINARY" ]; then
    "$CODEX_NOTIFY_BINARY" "$@" &
fi

# ── Langfuse hook ─────────────────────────────────────────────────────────────
# Runs non-blocking so Codex is never slowed down waiting for Langfuse.
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$HOOK_DIR/langfuse_hook.py" &
