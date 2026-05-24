# OpenCode → Langfuse Plugin

Rich per-turn observability for [OpenCode](https://opencode.ai) via [Langfuse](https://langfuse.com).

## What gets traced

Each user→assistant exchange = one Langfuse **trace**:

```
Trace: "<user message preview>"
  tags: [opencode, model-id, provider:provider-id]
  metadata: session_id, directory, title, version, cwd, model, step/tool/file counts, token totals, cost

  ├── Generation: "Step 1"
  │   model, usageDetails (input/output/reasoning/cache_read/cache_write), cost, stop_reason
  │   input: user message (first step) or tool results (subsequent steps)
  │   output: { content, thinking, tool_calls }
  │   ├── Tool: "bash"              ← per tool call, with input/output/timing
  │   └── Tool: "read_file"
  │
  ├── Generation: "Step 2"          ← if the model makes multiple LLM calls
  │   input: tool results from Step 1
  │   └── Tool: "write_file"
  │
  └── Edit: "foo.ts"                ← per file touched (from PatchPart)
```

Two tracing layers run in parallel:
- **OTEL layer** (`LangfuseSpanProcessor`) — automatic baseline from OpenCode's native OpenTelemetry instrumentation
- **Event-driven layer** — subscribes to OpenCode's event bus and constructs structured per-turn traces with full token usage, tool I/O, and timing

## Setup

### 1. Install

```bash
# From the langfuse-ai-agent-hooks repo root:
cd ~/.config/opencode
bun add file:/path/to/langfuse-ai-agent-hooks/opencode
```

### 2. Configure `~/.config/opencode/opencode.json`

```json
{
  "plugin": ["opencode-plugin-langfuse-rich"],
  "experimental": {
    "openTelemetry": true
  },
  "env": {
    "LANGFUSE_PUBLIC_KEY": "pk-lf-...",
    "LANGFUSE_SECRET_KEY": "sk-lf-...",
    "LANGFUSE_BASE_URL": "https://langfuse.dhomane.com",
    "LANGFUSE_ENVIRONMENT": "opencode"
  }
}
```

### 3. Rebuild after changes

```bash
cd /path/to/langfuse-ai-agent-hooks/opencode
bun run build
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | Yes | — | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | Yes | — | Langfuse secret key |
| `LANGFUSE_BASE_URL` or `LANGFUSE_BASEURL` | No | `https://cloud.langfuse.com` | Self-hosted instance URL |
| `LANGFUSE_ENVIRONMENT` | No | `opencode` | Environment tag in Langfuse |

## Data captured per turn

| Field | Source |
|---|---|
| User message text | `message.part.updated` → `TextPart` (role=user) |
| Assistant text | `message.part.updated` → `TextPart` (role=assistant) |
| Reasoning/thinking | `message.part.updated` → `ReasoningPart` |
| Token counts (input/output/reasoning/cache) | `message.part.updated` → `StepFinishPart` |
| Per-step cost | `StepFinishPart.cost` |
| Tool name + input + output + timing | `message.part.updated` → `ToolPart` state transitions |
| Files edited | `message.part.updated` → `PatchPart.files` |
| Model ID + provider | `message.updated` → `AssistantMessage.modelID/providerID` |
| Working directory | `AssistantMessage.path.cwd` |
| Session title, version, directory | `session.updated` |
| Flush trigger | `session.idle` |

## Development

```bash
bun install
bun run build      # compile TypeScript → dist/
bun run dev        # watch mode
```
