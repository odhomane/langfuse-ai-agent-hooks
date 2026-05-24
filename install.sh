#!/usr/bin/env bash
# install.sh — set up Langfuse observability hooks for Claude Code, Codex, and OpenCode
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/odhomane/langfuse-ai-agent-hooks/main/install.sh | bash
#   # or clone the repo and run locally:
#   bash install.sh
#
# What it does:
#   1. Detects OS (macOS / Linux) and shell profile (~/.zshrc or ~/.bashrc)
#   2. Installs Python deps (langfuse>=4.0,<5 and opentelemetry-api)
#   3. Copies Claude Code hook to ~/.claude/hooks/
#   4. Copies Codex hook + wrapper to ~/.codex/
#   5. Builds the OpenCode TypeScript plugin (requires bun)
#   6. Appends placeholder env var blocks to your shell profile (skips if already present)
#   7. Prints a checklist of manual steps that need real API keys
#
# All operations are idempotent — safe to re-run.

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[info]${NC}  $*"; }
ok()      { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()     { echo -e "${RED}[error]${NC} $*" >&2; }

# ── Repo root (works whether sourced, piped, or run directly) ─────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
# If piped from curl, SCRIPT_DIR will be "/" — detect and abort gracefully
if [[ "$SCRIPT_DIR" == "/" ]] || [[ ! -d "$SCRIPT_DIR/claude-code" ]]; then
    err "Cannot locate repo root. Please clone the repo and run: bash install.sh"
    exit 1
fi

REPO_ROOT="$SCRIPT_DIR"

# ── OS detection ──────────────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM="macos" ;;
    Linux)  PLATFORM="linux" ;;
    *) err "Unsupported OS: $OS"; exit 1 ;;
esac
info "Platform: $PLATFORM"

# ── Shell profile detection ───────────────────────────────────────────────────
detect_shell_profile() {
    local shell_name
    shell_name="$(basename "${SHELL:-/bin/sh}")"
    case "$shell_name" in
        zsh)  echo "$HOME/.zshrc" ;;
        bash) echo "$HOME/.bashrc" ;;
        *)    echo "$HOME/.profile" ;;
    esac
}
SHELL_PROFILE="$(detect_shell_profile)"
info "Shell profile: $SHELL_PROFILE"

# ── Python detection ──────────────────────────────────────────────────────────
detect_python() {
    if command -v python3 &>/dev/null; then
        echo "python3"
    else
        echo ""
    fi
}
PYTHON="$(detect_python)"
if [[ -z "$PYTHON" ]]; then
    err "python3 not found. Install Python 3.10+ before running this script."
    exit 1
fi
PYTHON_VERSION="$("$PYTHON" --version 2>&1)"
info "Python: $PYTHON_VERSION at $(command -v "$PYTHON")"

# ── Helper: append a block to shell profile only if sentinel is absent ─────────
append_if_absent() {
    local sentinel="$1"
    local block="$2"
    local profile="$3"
    if grep -qF "$sentinel" "$profile" 2>/dev/null; then
        ok "Already in $profile: $sentinel"
    else
        printf '\n%s\n' "$block" >> "$profile"
        ok "Appended to $profile: $sentinel"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. Python dependencies
# ─────────────────────────────────────────────────────────────────────────────
info "Installing Python SDK dependencies..."
"$PYTHON" -m pip install --quiet --upgrade "langfuse>=4.0,<5" opentelemetry-api \
    && ok "langfuse + opentelemetry-api installed" \
    || warn "pip install failed — you may need to install manually: $PYTHON -m pip install 'langfuse>=4.0,<5' opentelemetry-api"

# ─────────────────────────────────────────────────────────────────────────────
# 2. Claude Code hook
# ─────────────────────────────────────────────────────────────────────────────
info "Setting up Claude Code hook..."
CLAUDE_HOOKS_DIR="$HOME/.claude/hooks"
CLAUDE_STATE_DIR="$HOME/.claude/state"
mkdir -p "$CLAUDE_HOOKS_DIR" "$CLAUDE_STATE_DIR"
cp "$REPO_ROOT/claude-code/hooks/langfuse_hook.py" "$CLAUDE_HOOKS_DIR/langfuse_hook.py"
ok "Copied claude-code hook → $CLAUDE_HOOKS_DIR/langfuse_hook.py"

CLAUDE_SETTINGS="$HOME/.claude/settings.json"
if [[ ! -f "$CLAUDE_SETTINGS" ]]; then
    cat > "$CLAUDE_SETTINGS" << 'SETTINGS_EOF'
{
  "env": {
    "TRACE_TO_LANGFUSE": "true",
    "CC_LANGFUSE_PUBLIC_KEY": "pk-lf-REPLACE_ME",
    "CC_LANGFUSE_SECRET_KEY": "sk-lf-REPLACE_ME",
    "CC_LANGFUSE_BASE_URL": "https://cloud.langfuse.com",
    "CC_LANGFUSE_DEBUG": "false",
    "CC_LANGFUSE_MAX_CHARS": "1000000"
  },
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/langfuse_hook.py"
          }
        ]
      }
    ]
  }
}
SETTINGS_EOF
    ok "Created $CLAUDE_SETTINGS (fill in CC_LANGFUSE_* keys)"
else
    warn "$CLAUDE_SETTINGS already exists — skipping creation."
    warn "Make sure it contains the Stop hook and CC_LANGFUSE_* env vars. See README for the snippet."
fi

# ─────────────────────────────────────────────────────────────────────────────
# 3. Codex hook
# ─────────────────────────────────────────────────────────────────────────────
info "Setting up Codex hook..."
CODEX_HOME="$HOME/.codex"
CODEX_STATE_DIR="$CODEX_HOME/state"
mkdir -p "$CODEX_HOME" "$CODEX_STATE_DIR"
cp "$REPO_ROOT/codex/langfuse_hook.py" "$CODEX_HOME/langfuse_hook.py"
cp "$REPO_ROOT/codex/notify-wrapper.sh" "$CODEX_HOME/notify-wrapper.sh"
chmod +x "$CODEX_HOME/notify-wrapper.sh"
ok "Copied codex hook + wrapper → $CODEX_HOME/"

CODEX_CONFIG="$CODEX_HOME/config.toml"
if [[ ! -f "$CODEX_CONFIG" ]]; then
    cat > "$CODEX_CONFIG" << TOML_EOF
notify = ["$CODEX_HOME/notify-wrapper.sh", "turn-ended"]
TOML_EOF
    ok "Created $CODEX_CONFIG with notify hook"
elif ! grep -q "notify-wrapper.sh" "$CODEX_CONFIG"; then
    echo "" >> "$CODEX_CONFIG"
    echo "notify = [\"$CODEX_HOME/notify-wrapper.sh\", \"turn-ended\"]" >> "$CODEX_CONFIG"
    ok "Appended notify hook to $CODEX_CONFIG"
else
    ok "Codex notify hook already present in $CODEX_CONFIG"
fi

# Append Codex env vars to shell profile
CODEX_ENV_BLOCK="# Codex → Langfuse tracing (langfuse-ai-agent-hooks)
# Uses CODEX_LANGFUSE_* prefix to isolate from Claude Code and OpenCode
export CODEX_LANGFUSE_PUBLIC_KEY=\"pk-lf-REPLACE_ME\"
export CODEX_LANGFUSE_SECRET_KEY=\"sk-lf-REPLACE_ME\"
export CODEX_LANGFUSE_BASE_URL=\"https://cloud.langfuse.com\"
# export CODEX_LANGFUSE_DEBUG=\"true\"  # uncomment for verbose logging"

append_if_absent "CODEX_LANGFUSE_PUBLIC_KEY" "$CODEX_ENV_BLOCK" "$SHELL_PROFILE"

# macOS-only: desktop notification binary hint
if [[ "$PLATFORM" == "macos" ]]; then
    NOTIFY_BINARY="$HOME/.codex/computer-use/Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient"
    if [[ -x "$NOTIFY_BINARY" ]]; then
        NOTIFY_BLOCK="# Codex Computer Use desktop notifications (macOS)
export CODEX_NOTIFY_BINARY=\"$NOTIFY_BINARY\""
        append_if_absent "CODEX_NOTIFY_BINARY" "$NOTIFY_BLOCK" "$SHELL_PROFILE"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4. OpenCode plugin (requires bun)
# ─────────────────────────────────────────────────────────────────────────────
info "Setting up OpenCode plugin..."
if command -v bun &>/dev/null; then
    info "Building OpenCode plugin with bun..."
    (cd "$REPO_ROOT/opencode" && bun install --frozen-lockfile 2>&1 | tail -5 && bun run build 2>&1 | tail -5)
    ok "OpenCode plugin built → $REPO_ROOT/opencode/dist/index.js"

    OC_CONFIG="$HOME/.config/opencode/opencode.json"
    PLUGIN_PATH="$REPO_ROOT/opencode"
    if [[ -f "$OC_CONFIG" ]]; then
        if grep -qF "$PLUGIN_PATH" "$OC_CONFIG"; then
            ok "OpenCode plugin already registered in $OC_CONFIG"
        else
            warn "opencode.json exists but plugin is not registered."
            warn "Add this to the 'plugin' array in $OC_CONFIG:"
            warn "  \"$PLUGIN_PATH\""
            warn "Also ensure: \"experimental\": { \"openTelemetry\": true }"
        fi
    else
        mkdir -p "$(dirname "$OC_CONFIG")"
        cat > "$OC_CONFIG" << JSON_EOF
{
  "plugin": [
    "$PLUGIN_PATH"
  ],
  "experimental": {
    "openTelemetry": true
  }
}
JSON_EOF
        ok "Created $OC_CONFIG with plugin registered"
    fi

    # Append OpenCode env vars to shell profile
    OC_ENV_BLOCK="# OpenCode → Langfuse tracing (opencode-plugin-langfuse-rich)
# Uses OC_LANGFUSE_* prefix so keys don't bleed into Claude Code or Codex hooks
export OC_LANGFUSE_PUBLIC_KEY=\"pk-lf-REPLACE_ME\"
export OC_LANGFUSE_SECRET_KEY=\"sk-lf-REPLACE_ME\"
export OC_LANGFUSE_BASE_URL=\"https://cloud.langfuse.com\"
export OC_LANGFUSE_ENVIRONMENT=\"opencode\""

    append_if_absent "OC_LANGFUSE_PUBLIC_KEY" "$OC_ENV_BLOCK" "$SHELL_PROFILE"
else
    warn "bun not found — skipping OpenCode plugin build."
    warn "Install bun (https://bun.sh) then run: cd $REPO_ROOT/opencode && bun install && bun run build"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Done — print checklist
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation complete — manual steps required below  ${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo ""
echo "1. Edit $SHELL_PROFILE and replace REPLACE_ME placeholders with real keys:"
echo ""
echo "   ┌─ Claude Code (Settings → API Keys in your Claude Code Langfuse project)"
echo "   │  CC_LANGFUSE_PUBLIC_KEY, CC_LANGFUSE_SECRET_KEY, CC_LANGFUSE_BASE_URL"
echo "   │  (these live in ~/.claude/settings.json env block)"
echo "   │"
echo "   ├─ Codex (Settings → API Keys in your Codex Langfuse project)"
echo "   │  CODEX_LANGFUSE_PUBLIC_KEY, CODEX_LANGFUSE_SECRET_KEY, CODEX_LANGFUSE_BASE_URL"
echo "   │"
echo "   └─ OpenCode (Settings → API Keys in your OpenCode Langfuse project)"
echo "      OC_LANGFUSE_PUBLIC_KEY, OC_LANGFUSE_SECRET_KEY, OC_LANGFUSE_BASE_URL"
echo ""
echo "2. Reload your shell profile:"
echo "   source $SHELL_PROFILE"
echo ""
echo "3. Verify Claude Code hook is wired (check ~/.claude/settings.json has the Stop hook)."
echo ""
echo "4. Start a new Codex or OpenCode session and check your Langfuse dashboards."
echo ""
echo "   Logs:"
echo "   - Claude Code: tail -f ~/.claude/state/langfuse_hook.log"
echo "   - Codex:       tail -f ~/.codex/state/langfuse_codex_hook.log"
echo "   - OpenCode:    check Help → View Logs in the app"
echo ""
