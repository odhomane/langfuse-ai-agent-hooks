# langfuse-ai-agent-hooks

Conversational observability hooks for AI coding agents → [Langfuse](https://langfuse.com).

Each hook captures full conversation turns — user messages, assistant responses, tool calls, file edits, token usage, and timing — and ships them to Langfuse as structured traces. Hooks are **event-driven** (no polling daemons) and **fail-open** (an unreachable Langfuse never blocks the agent).

## Supported agents

| Agent | Folder | Trigger mechanism |
|---|---|---|
| [Claude Code](https://claude.ai/code) (Anthropic) | `claude-code/` | `Stop` hook in `settings.json` |
| [Codex](https://openai.com/codex) (OpenAI) | `codex/` | `notify` in `config.toml` |
| [LiteLLM](https://litellm.ai) | `litellm/` | Native callback (no script needed) |
| [OpenCode](https://opencode.ai) | `opencode/` | TypeScript plugin via OpenCode plugin API |

More agents coming. PRs welcome.

---

## What gets traced

### Claude Code
- Full conversation turns with user message preview as trace name
- Per-generation: model, stop_reason, Anthropic request_id, extended thinking blocks, tool calls with `is_error` flag, service_tier, web search counts
- Edited file spans (`Edit: filename`) with content snippets from `edited_text_file` attachments
- Context files loaded per turn
- Aggregate token totals per turn (input / output / cache_read / cache_creation)
- Session metadata: `cwd`, `version`, `git_branch`, `entrypoint`, `permission_mode`, AI-generated session title
- Tool name list per turn for Langfuse filtering
- Hook stderr surfaced in trace metadata when non-empty
- Cost computed natively by Langfuse from model name + token counts

### Codex
- Full conversation turns (user → agent → tools → file edits)
- File edit spans (`Edit: filename`) with full unified diffs from `patch_apply_end` events
- Shell and MCP tool calls with arguments and output
- Web search calls with extracted query list
- Token usage: input / output / cached / reasoning
- Model, reasoning_effort, collaboration_mode name
- Context window size, session metadata, memory citations, system prompt

### OpenCode
- Full conversation turns with user message preview as trace name
- Per-LLM-step generation spans: model, usageDetails (input / output / reasoning / cache_read / cache_write), cost, stop_reason
- Tool spans nested under each generation: tool name, input args, output, timing, error flag (from `ToolPart` state transitions)
- Reasoning/thinking blocks captured per step
- File edit spans (`Edit: filename`) from `PatchPart.files`
- Session metadata: `session_id`, `directory`, `title`, `version`, `cwd`, `model_id`, `provider_id`
- Dual tracing layers: OTEL `LangfuseSpanProcessor` (baseline) + event-driven (rich per-turn traces)
- Flushes on `session.idle`; final flush + SDK shutdown on `server.instance.disposed`

---

## Prerequisites

- Python 3.10+
- Langfuse SDK 4.x: `pip install "langfuse>=4.0,<5"`
- OpenTelemetry API: `pip install opentelemetry-api`

```bash
pip install "langfuse>=4.0,<5" opentelemetry-api
```

On macOS with Homebrew Python:
```bash
/opt/homebrew/bin/pip3 install "langfuse>=4.0,<5" opentelemetry-api
```

---

## 1. Langfuse setup

### Self-hosted (recommended for full data control)

```bash
# Docker Compose quick-start
git clone https://github.com/langfuse/langfuse.git
cd langfuse
cp .env.example .env   # fill in DATABASE_URL and a random NEXTAUTH_SECRET
docker compose up -d
```

See [Langfuse self-hosting docs](https://langfuse.com/docs/deployment/self-host) for production setup.

### Cloud

Sign up at [cloud.langfuse.com](https://cloud.langfuse.com). Free tier available.

### Create two projects

Create one Langfuse project per agent so traces are separated:

1. **Claude Code** — copy the public and secret keys
2. **Codex** — copy the public and secret keys

---

## 2. Claude Code setup

### 2a. Install the hook script

```bash
mkdir -p ~/.claude/hooks
cp claude-code/hooks/langfuse_hook.py ~/.claude/hooks/langfuse_hook.py
mkdir -p ~/.claude/state   # log and state files go here
```

### 2b. Configure settings.json

Add (or merge) the following into `~/.claude/settings.json`:

```json
{
  "env": {
    "TRACE_TO_LANGFUSE": "true",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-your-claude-code-public-key",
    "LANGFUSE_SECRET_KEY": "sk-lf-your-claude-code-secret-key",
    "LANGFUSE_BASE_URL": "https://your-langfuse.example.com",
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
```

**Environment variable reference:**

| Variable | Required | Default | Description |
|---|---|---|---|
| `TRACE_TO_LANGFUSE` | Yes | — | Must be `"true"` to enable the hook |
| `LANGFUSE_PUBLIC_KEY` | Yes | — | Langfuse project public key |
| `LANGFUSE_SECRET_KEY` | Yes | — | Langfuse project secret key |
| `LANGFUSE_BASE_URL` | No | `https://cloud.langfuse.com` | Your Langfuse host |
| `CC_LANGFUSE_DEBUG` | No | `false` | Verbose logging to `~/.claude/state/langfuse_hook.log` |
| `CC_LANGFUSE_MAX_CHARS` | No | `20000` | Max characters per field before truncation |

You can also use `CC_LANGFUSE_PUBLIC_KEY` / `CC_LANGFUSE_SECRET_KEY` / `CC_LANGFUSE_BASE_URL` as overrides if you want Claude Code to use different keys than your shell environment.

### 2c. Verify

Run any Claude Code session, then check your Langfuse dashboard. You should see a trace named after your first message.

Logs: `~/.claude/state/langfuse_hook.log`

---

## 3. Codex setup

### 3a. Install the hook scripts

```bash
cp codex/langfuse_hook.py ~/.codex/langfuse_hook.py
cp codex/notify-wrapper.sh ~/.codex/notify-wrapper.sh
chmod +x ~/.codex/notify-wrapper.sh
mkdir -p ~/.codex/state   # log and state files go here
```

### 3b. Set environment variables

The Codex hook reads credentials from environment variables. Add to your shell profile (`~/.zshrc`, `~/.bashrc`, or `~/.profile`):

```bash
export LANGFUSE_PUBLIC_KEY="pk-lf-your-codex-public-key"
export LANGFUSE_SECRET_KEY="sk-lf-your-codex-secret-key"
export LANGFUSE_BASE_URL="https://your-langfuse.example.com"   # omit for cloud
```

Then reload: `source ~/.zshrc`

**Environment variable reference:**

| Variable | Required | Default | Description |
|---|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | Yes | — | Langfuse project public key |
| `LANGFUSE_SECRET_KEY` | Yes | — | Langfuse project secret key |
| `LANGFUSE_BASE_URL` | No | `https://cloud.langfuse.com` | Your Langfuse host |
| `CODEX_LANGFUSE_DEBUG` | No | `false` | Verbose logging to `~/.codex/state/langfuse_codex_hook.log` |
| `CODEX_NOTIFY_BINARY` | No | — | Path to Codex Computer Use notify binary (macOS only, for desktop notifications) |

### 3c. Wire up the notify hook

Edit `~/.codex/config.toml` and set the `notify` key:

```toml
notify = ["/Users/yourname/.codex/notify-wrapper.sh", "turn-ended"]
```

> **Note:** Use the absolute path — Codex does not expand `~` in the notify field.

### 3d. Optional: Codex Computer Use notifications (macOS)

If you use Codex Computer Use and want to keep desktop notifications working alongside Langfuse, set `CODEX_NOTIFY_BINARY` in your shell profile to point to the `SkyComputerUseClient` binary installed with the Codex app:

```bash
export CODEX_NOTIFY_BINARY="$HOME/.codex/computer-use/Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient"
```

### 3e. Verify

Run any Codex session. After the first turn completes, check your Langfuse dashboard for a trace. Sessions from the last 48 hours are processed on each trigger.

Logs: `~/.codex/state/langfuse_codex_hook.log`

---

## 4. Model pricing in Langfuse

The hooks send model name + token counts. Langfuse computes cost from its built-in model registry — **no pricing is hardcoded in the scripts**, so you never need to update the scripts when Anthropic or OpenAI change prices.

To add or update a model's pricing:

1. Open your Langfuse instance → **Settings → Models**
2. Click **Add model**
3. Set the model name pattern (e.g. `claude-sonnet-4-6`), input price, output price, cache read/write prices per million tokens
4. Cost appears automatically on all matching traces — including historical ones

Current pricing reference:
- [Anthropic pricing](https://www.anthropic.com/pricing)
- [OpenAI pricing](https://openai.com/api/pricing)

---

## 5. Troubleshooting

### No traces appearing

1. Check environment variables are set: `echo $LANGFUSE_PUBLIC_KEY`
2. Check the log file for errors:
   - Claude Code: `tail -f ~/.claude/state/langfuse_hook.log`
   - Codex: `tail -f ~/.codex/state/langfuse_codex_hook.log`
3. Enable debug logging temporarily:
   - Claude Code: set `CC_LANGFUSE_DEBUG: "true"` in `settings.json`
   - Codex: `export CODEX_LANGFUSE_DEBUG=true` before running Codex
4. Test the hook directly:
   ```bash
   # Claude Code
   echo '{"sessionId":"test","transcriptPath":"/nonexistent"}' | python3 ~/.claude/hooks/langfuse_hook.py

   # Codex (runs and exits cleanly if keys are set)
   python3 ~/.codex/langfuse_hook.py
   ```

### `ModuleNotFoundError: No module named 'langfuse'`

The hook is running under a Python that doesn't have the SDK installed. Fix:

```bash
# Find which python3 the hook uses
which python3

# Install into that interpreter
python3 -m pip install "langfuse>=4.0,<5" opentelemetry-api

# Or with Homebrew Python explicitly
/opt/homebrew/bin/pip3 install "langfuse>=4.0,<5" opentelemetry-api
```

### `RuntimeError: Langfuse SDK missing internal API`

The installed Langfuse SDK is outside the `>=4.0,<5` range. Pin it:

```bash
pip install "langfuse>=4.0,<5"
```

### Codex hook not triggering

- Confirm `notify` in `config.toml` uses an **absolute path** (no `~`)
- Confirm the script is executable: `ls -la ~/.codex/notify-wrapper.sh`
- The hook processes sessions modified within the last 48 hours. If you're testing with an older session, it won't appear.

### 403 Forbidden from Langfuse

Your API key is wrong or belongs to a different project. Regenerate the key in Langfuse UI → Settings → API Keys.

---

## Adding a new agent

1. Create a new folder: `mkdir <agent-name>/`
2. Add a `langfuse_hook.py` that reads the agent's session/transcript format and calls `Langfuse(public_key=..., secret_key=..., host=...)` using env vars
3. Add a `README.md` explaining the trigger mechanism for that agent
4. Open a PR

---

## Repo structure

```
langfuse-ai-agent-hooks/
├── claude-code/
│   └── hooks/
│       └── langfuse_hook.py    # Stop hook — reads JSONL transcripts
├── codex/
│   ├── langfuse_hook.py        # Reads ~/.codex/sessions/**/*.jsonl
│   └── notify-wrapper.sh       # Called by Codex on every turn-end
├── litellm/
│   └── README.md               # Native callback — no script needed
└── README.md
```
