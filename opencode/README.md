# OpenCode → Langfuse Plugin

Rich per-turn observability for [OpenCode](https://opencode.ai) via [Langfuse](https://langfuse.com).

## What gets traced

Each user→assistant exchange = one Langfuse **trace**:

```
Trace: "<user message preview>"
  tags: [opencode, model-id, provider:provider-id]
  metadata: session_id, directory, title, version, cwd, model,
            step/tool/file counts, total tokens, total cost

  ├── Generation: "Step 1"
  │   model, usageDetails (input/output/reasoning/cache_read/cache_write), cost, stop_reason
  │   input: user message (first step) or tool results (subsequent steps)
  │   output: { content, thinking, tool_calls }
  │   ├── Tool: "bash"         ← per tool call, with input/output/timing/error flag
  │   └── Tool: "read_file"
  │
  ├── Generation: "Step 2"     ← if the model makes multiple LLM calls per turn
  │   input: tool results from Step 1
  │   └── Tool: "write_file"
  │
  └── Edit: "foo.ts"           ← per file patched (from PatchPart)
```

Two tracing layers run in parallel:
- **OTEL layer** (`LangfuseSpanProcessor`) — automatic baseline from OpenCode's native OpenTelemetry instrumentation, enabled by `experimental.openTelemetry`
- **Event-driven layer** — subscribes to the OpenCode event bus and builds structured per-turn traces with full token usage, tool I/O, reasoning blocks, and timing

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/odhomane/langfuse-ai-agent-hooks.git
```

### 2. Build the plugin

```bash
cd langfuse-ai-agent-hooks/opencode
bun install
bun run build
```

### 3. Add env vars to your shell profile

OpenCode reads env vars from the shell environment. Add these to `~/.zshrc` (or `~/.bashrc`):

```bash
# OpenCode → Langfuse tracing (opencode-plugin-langfuse-rich)
# Uses OC_LANGFUSE_* prefix so keys don't bleed into Claude Code or Codex hooks
export OC_LANGFUSE_PUBLIC_KEY="pk-lf-..."
export OC_LANGFUSE_SECRET_KEY="sk-lf-..."
export OC_LANGFUSE_BASE_URL="https://langfuse.dhomane.com"
export OC_LANGFUSE_ENVIRONMENT="opencode"
```

Then reload: `source ~/.zshrc`

> **Why `OC_` prefix?** If you also use Claude Code or Codex Langfuse hooks, they read the
> generic `LANGFUSE_*` names. Using a distinct prefix keeps each agent's keys isolated.

### 4. Configure `~/.config/opencode/opencode.json`

Reference the plugin by its **absolute path on disk** — OpenCode's plugin loader treats
absolute paths as local file plugins (skips npm lookup):

```json
{
  "plugin": [
    "/absolute/path/to/langfuse-ai-agent-hooks/opencode"
  ],
  "experimental": {
    "openTelemetry": true
  }
}
```

> **Note:** Do not use `"env": { ... }` in `opencode.json` — OpenCode does not support that key
> and will refuse to start. Set credentials via shell env vars as shown in step 3.

### 5. Restart OpenCode

On the next launch, the plugin logs `Langfuse OpenCode tracing started → <url>` via the
app log service. Traces appear in Langfuse after the first `session.idle` event (i.e. after
the model finishes its first response).

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OC_LANGFUSE_PUBLIC_KEY` | Yes* | — | Langfuse public key (OpenCode-specific) |
| `OC_LANGFUSE_SECRET_KEY` | Yes* | — | Langfuse secret key (OpenCode-specific) |
| `OC_LANGFUSE_BASE_URL` | No | `https://cloud.langfuse.com` | Self-hosted instance URL |
| `OC_LANGFUSE_ENVIRONMENT` | No | `opencode` | Environment tag shown in Langfuse |

*Falls back to the generic `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` if the `OC_` variants are not set.

## Data captured per turn

| Field | Source |
|---|---|
| User message text | `message.part.updated` → `TextPart` (role=user) |
| Assistant text per step | `message.part.updated` → `TextPart` (role=assistant) |
| Reasoning/thinking blocks | `message.part.updated` → `ReasoningPart` |
| Token counts (input/output/reasoning/cache read+write) | `message.part.updated` → `StepFinishPart` |
| Per-step cost | `StepFinishPart.cost` |
| Tool name, input, output, timing, error state | `message.part.updated` → `ToolPart` state transitions |
| Files edited | `message.part.updated` → `PatchPart.files` |
| Model ID + provider | `message.updated` → `AssistantMessage.modelID / providerID` |
| Working directory | `AssistantMessage.path.cwd` |
| Session title, version, directory | `session.updated` |
| Flush trigger | `session.idle` (fires when the model finishes each response) |
| Final flush + shutdown | `server.instance.disposed` |

## Development

```bash
bun install
bun run build      # compile TypeScript → dist/
bun run dev        # watch mode (recompiles on save)
```

After rebuilding, just restart OpenCode — no reinstall needed since the plugin is loaded
directly from disk via the absolute path in `opencode.json`.
