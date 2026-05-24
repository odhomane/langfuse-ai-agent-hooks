import { LangfuseSpanProcessor } from "@langfuse/otel";
import type { Plugin } from "@opencode-ai/plugin";
import type {
  Event,
  Part,
  TextPart,
  ReasoningPart,
  StepFinishPart,
  ToolPart,
  PatchPart,
  AssistantMessage,
  UserMessage,
} from "@opencode-ai/sdk";
import { Langfuse } from "langfuse";
import { NodeSDK } from "@opentelemetry/sdk-node";

// ── Config ────────────────────────────────────────────────────────────────────

const MAX_CHARS = 50_000;

function trunc(s: unknown, max = MAX_CHARS): string {
  if (s === null || s === undefined) return "";
  const str = typeof s === "string" ? s : JSON.stringify(s);
  return str.length > max ? str.slice(0, max) + "…" : str;
}

function msToDate(ms: number | undefined): Date | undefined {
  return ms !== undefined ? new Date(ms) : undefined;
}

// ── State types ───────────────────────────────────────────────────────────────

interface ToolEntry {
  callID: string;
  tool: string;
  status: string;
  isError: boolean;
  input?: Record<string, unknown>;
  output?: string;
  error?: string;
  title?: string;
  startTime?: Date;
  endTime?: Date;
}

interface StepEntry {
  index: number;
  startTime?: Date;
  endTime?: Date;
  tokens: {
    input: number;
    output: number;
    reasoning: number;
    cacheRead: number;
    cacheWrite: number;
  };
  cost: number;
  reason: string;
  textsByPartId: Map<string, string>;
  reasoningByPartId: Map<string, string>;
  tools: ToolEntry[];
}

interface TurnEntry {
  sessionID: string;
  userMessageID: string;
  assistantMessageID?: string;
  userText: string;
  userTime?: Date;
  modelID?: string;
  providerID?: string;
  cwd?: string;
  steps: StepEntry[];
  currentStepIndex: number;
  toolMap: Map<string, ToolEntry>; // callID → entry
  patchFiles: string[];
  emitted: boolean;
}

interface SessionEntry {
  sessionID: string;
  directory: string;
  title: string;
  version: string;
  totalCost: number;
  totalTokens: { input: number; output: number };
  turnOrder: string[]; // userMessageID in order
  turns: Map<string, TurnEntry>; // userMessageID → turn
  assistantToUser: Map<string, string>; // assistantMessageID → userMessageID
  messageRole: Map<string, "user" | "assistant">; // messageID → role
}

// ── Plugin ────────────────────────────────────────────────────────────────────

export const LangfusePlugin: Plugin = async ({ client }) => {
  const publicKey = process.env.LANGFUSE_PUBLIC_KEY;
  const secretKey = process.env.LANGFUSE_SECRET_KEY;
  const baseUrl =
    process.env.LANGFUSE_BASEURL ??
    process.env.LANGFUSE_BASE_URL ??
    "https://cloud.langfuse.com";
  const environment = process.env.LANGFUSE_ENVIRONMENT ?? "opencode";

  const log = (level: "info" | "warn" | "error", message: string) => {
    try {
      client.app.log({ body: { service: "langfuse-opencode", level, message } });
    } catch {}
  };

  if (!publicKey || !secretKey) {
    log("warn", "Missing LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY — tracing disabled");
    return {};
  }

  // OTEL layer — automatic baseline tracing
  const processor = new LangfuseSpanProcessor({ publicKey, secretKey, baseUrl, environment });
  const sdk = new NodeSDK({ spanProcessors: [processor] });
  sdk.start();

  // Event-driven layer — rich per-turn traces
  const langfuse = new Langfuse({ publicKey, secretKey, baseUrl });
  const sessions = new Map<string, SessionEntry>();

  log("info", `Langfuse OpenCode tracing started → ${baseUrl} (env: ${environment})`);

  // ── State helpers ─────────────────────────────────────────────────────────

  function getSession(sessionID: string): SessionEntry {
    let s = sessions.get(sessionID);
    if (!s) {
      s = {
        sessionID,
        directory: "",
        title: "",
        version: "",
        totalCost: 0,
        totalTokens: { input: 0, output: 0 },
        turnOrder: [],
        turns: new Map(),
        assistantToUser: new Map(),
        messageRole: new Map(),
      };
      sessions.set(sessionID, s);
    }
    return s;
  }

  function getTurnForMessage(
    session: SessionEntry,
    messageID: string,
  ): TurnEntry | undefined {
    const role = session.messageRole.get(messageID);
    if (role === "user") return session.turns.get(messageID);
    if (role === "assistant") {
      const uid = session.assistantToUser.get(messageID);
      return uid ? session.turns.get(uid) : undefined;
    }
    return undefined;
  }

  function currentStep(turn: TurnEntry): StepEntry | undefined {
    return turn.steps[turn.currentStepIndex];
  }

  // ── Emit a batch of completed turns ──────────────────────────────────────

  async function emitTurns(session: SessionEntry): Promise<void> {
    for (const uid of session.turnOrder) {
      const turn = session.turns.get(uid);
      if (!turn || turn.emitted || !turn.userText || turn.steps.length === 0) continue;

      try {
        const preview = turn.userText.trim().replace(/\n/g, " ").slice(0, 72);
        const traceName = preview || "OpenCode Turn";

        const tags = ["opencode"];
        if (turn.modelID) tags.push(turn.modelID);
        if (turn.providerID) tags.push(`provider:${turn.providerID}`);

        const totals = turn.steps.reduce(
          (acc, s) => ({
            input: acc.input + s.tokens.input,
            output: acc.output + s.tokens.output,
            reasoning: acc.reasoning + s.tokens.reasoning,
            cacheRead: acc.cacheRead + s.tokens.cacheRead,
            cacheWrite: acc.cacheWrite + s.tokens.cacheWrite,
            cost: acc.cost + s.cost,
          }),
          { input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0, cost: 0 },
        );

        const trace = langfuse.trace({
          name: traceName,
          sessionId: turn.sessionID,
          tags,
          timestamp: turn.userTime,
          input: { role: "user", content: trunc(turn.userText) },
          metadata: {
            source: "opencode",
            session_id: turn.sessionID,
            user_message_id: turn.userMessageID,
            assistant_message_id: turn.assistantMessageID,
            directory: session.directory,
            title: session.title,
            version: session.version,
            cwd: turn.cwd,
            model_id: turn.modelID,
            provider_id: turn.providerID,
            step_count: turn.steps.length,
            tool_count: turn.toolMap.size,
            file_edit_count: turn.patchFiles.length,
            total_input_tokens: totals.input,
            total_output_tokens: totals.output,
            total_reasoning_tokens: totals.reasoning,
            total_cache_read_tokens: totals.cacheRead,
            total_cache_write_tokens: totals.cacheWrite,
            total_cost: totals.cost,
          },
        });

        // One generation span per LLM step
        let prevToolResults: Array<{ tool: string; output: string }> = [];
        let lastStepTexts: string[] = [];

        for (let i = 0; i < turn.steps.length; i++) {
          const step = turn.steps[i];
          const stepTexts = Array.from(step.textsByPartId.values()).filter(Boolean);
          const stepReasoning = Array.from(step.reasoningByPartId.values()).filter(Boolean);
          lastStepTexts = stepTexts;

          const genInput =
            i === 0
              ? { role: "user", content: trunc(turn.userText) }
              : prevToolResults.length > 0
                ? { role: "tool", results: prevToolResults }
                : null;

          const genOutput: Record<string, unknown> = { role: "assistant" };
          const fullText = stepTexts.join("\n\n");
          if (fullText) genOutput.content = trunc(fullText);
          const fullReasoning = stepReasoning.join("\n\n");
          if (fullReasoning) genOutput.thinking = trunc(fullReasoning);
          if (step.tools.length > 0) {
            genOutput.tool_calls = step.tools.map((t) => ({
              name: t.tool,
              input: t.input,
              status: t.status,
              ...(t.title ? { title: t.title } : {}),
            }));
          }

          const gen = trace.generation({
            name: `Step ${i + 1}`,
            model: turn.modelID,
            input: genInput,
            output: genOutput,
            startTime: step.startTime,
            endTime: step.endTime,
            usageDetails: {
              input: step.tokens.input,
              output: step.tokens.output,
              reasoning: step.tokens.reasoning,
              cache_read_input_tokens: step.tokens.cacheRead,
              cache_creation_input_tokens: step.tokens.cacheWrite,
            },
            metadata: {
              step_index: i,
              stop_reason: step.reason,
              cost: step.cost,
            },
          });

          // Tool spans nested under the generation
          prevToolResults = [];
          for (const tool of step.tools) {
            gen.span({
              name: `Tool: ${tool.tool}`,
              input: tool.input ?? {},
              output: tool.isError ? { error: tool.error } : tool.output,
              startTime: tool.startTime,
              endTime: tool.endTime,
              statusMessage: tool.isError ? "error" : undefined,
              metadata: {
                tool_name: tool.tool,
                call_id: tool.callID,
                status: tool.status,
                title: tool.title,
                is_error: tool.isError,
              },
            });
            if (tool.output) {
              prevToolResults.push({ tool: tool.tool, output: trunc(tool.output, 500) });
            }
          }
        }

        // File edit spans — direct children of the trace
        for (const file of turn.patchFiles) {
          const filename = file.split("/").pop() || file;
          trace.span({
            name: `Edit: ${filename}`,
            input: { path: file },
            output: "patched",
            metadata: { tool_name: "file_edit", path: file },
          });
        }

        // Trace output = final assistant text
        const finalText = lastStepTexts.join("\n\n");
        trace.update({
          output: { role: "assistant", content: trunc(finalText) },
        });

        turn.emitted = true;
        log("info", `emitted turn: session=${turn.sessionID} model=${turn.modelID} steps=${turn.steps.length} tools=${turn.toolMap.size}`);
      } catch (err) {
        log("error", `emit_turn failed: ${err}`);
      }
    }
  }

  // ── Event handler ─────────────────────────────────────────────────────────

  async function handleEvent(event: Event): Promise<void> {
    switch (event.type) {
      // Session metadata updates
      case "session.updated": {
        const info = event.properties.info;
        const s = getSession(info.id);
        s.title = info.title ?? s.title;
        s.version = info.version ?? s.version;
        s.directory = info.directory ?? s.directory;
        break;
      }

      // Message created/updated — establishes turn boundaries
      case "message.updated": {
        const msg = event.properties.info;
        const s = getSession(msg.sessionID);

        if (msg.role === "user") {
          const um = msg as UserMessage;
          s.messageRole.set(um.id, "user");
          if (!s.turns.has(um.id)) {
            const turn: TurnEntry = {
              sessionID: msg.sessionID,
              userMessageID: um.id,
              userText: "",
              userTime: um.time?.created ? new Date(um.time.created) : undefined,
              modelID: um.model?.modelID,
              providerID: um.model?.providerID,
              steps: [],
              currentStepIndex: -1,
              toolMap: new Map(),
              patchFiles: [],
              emitted: false,
            };
            s.turns.set(um.id, turn);
            s.turnOrder.push(um.id);
          }
        } else if (msg.role === "assistant") {
          const am = msg as AssistantMessage;
          s.messageRole.set(am.id, "assistant");
          if (am.parentID) {
            s.assistantToUser.set(am.id, am.parentID);
            const turn = s.turns.get(am.parentID);
            if (turn) {
              turn.assistantMessageID = am.id;
              if (!turn.modelID && am.modelID) turn.modelID = am.modelID;
              if (!turn.providerID && am.providerID) turn.providerID = am.providerID;
              if (am.path?.cwd && !turn.cwd) turn.cwd = am.path.cwd;
            }
          }
        }
        break;
      }

      // Part updates — the richest source of data
      case "message.part.updated": {
        const part = event.properties.part as Part;
        const s = getSession(part.sessionID);
        const turn = getTurnForMessage(s, part.messageID);
        if (!turn) break;

        switch (part.type) {
          case "text": {
            const tp = part as TextPart;
            if (tp.synthetic || tp.ignored) break;
            const role = s.messageRole.get(part.messageID);
            if (role === "user") {
              // User message text (may stream in deltas)
              if (tp.text) turn.userText = tp.text;
            } else if (role === "assistant") {
              const step = currentStep(turn);
              if (step && tp.text) {
                step.textsByPartId.set(part.id, tp.text);
              }
            }
            break;
          }

          case "reasoning": {
            const rp = part as ReasoningPart;
            const step = currentStep(turn);
            if (step && rp.text) {
              step.reasoningByPartId.set(part.id, rp.text);
            }
            break;
          }

          case "step-start": {
            const newStep: StepEntry = {
              index: turn.steps.length,
              startTime: new Date(),
              tokens: { input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0 },
              cost: 0,
              reason: "",
              textsByPartId: new Map(),
              reasoningByPartId: new Map(),
              tools: [],
            };
            turn.steps.push(newStep);
            turn.currentStepIndex = turn.steps.length - 1;
            break;
          }

          case "step-finish": {
            const sfp = part as StepFinishPart;
            const step = currentStep(turn);
            if (step) {
              step.endTime = new Date();
              step.tokens = {
                input: sfp.tokens?.input ?? 0,
                output: sfp.tokens?.output ?? 0,
                reasoning: sfp.tokens?.reasoning ?? 0,
                cacheRead: sfp.tokens?.cache?.read ?? 0,
                cacheWrite: sfp.tokens?.cache?.write ?? 0,
              };
              step.cost = sfp.cost ?? 0;
              step.reason = sfp.reason ?? "";
            }
            break;
          }

          case "tool": {
            const tp = part as ToolPart;
            const callID = tp.callID;
            const step = currentStep(turn);

            let entry = turn.toolMap.get(callID);
            if (!entry) {
              entry = {
                callID,
                tool: tp.tool,
                status: tp.state.status,
                isError: false,
              };
              turn.toolMap.set(callID, entry);
              // Associate tool with the step it was called in
              if (step) {
                // Only add if not already in this step
                if (!step.tools.find((t) => t.callID === callID)) {
                  step.tools.push(entry);
                }
              }
            }

            entry.status = tp.state.status;

            if (tp.state.status === "pending") {
              entry.input = tp.state.input;
            } else if (tp.state.status === "running") {
              entry.input = tp.state.input;
              entry.startTime = msToDate(tp.state.time?.start);
            } else if (tp.state.status === "completed") {
              entry.input = tp.state.input;
              entry.output = trunc(tp.state.output);
              entry.title = tp.state.title;
              entry.startTime = entry.startTime ?? msToDate(tp.state.time?.start);
              entry.endTime = msToDate(tp.state.time?.end);
              entry.isError = false;
            } else if (tp.state.status === "error") {
              entry.input = tp.state.input;
              entry.error = tp.state.error;
              entry.startTime = entry.startTime ?? msToDate(tp.state.time?.start);
              entry.endTime = msToDate(tp.state.time?.end);
              entry.isError = true;
            }
            break;
          }

          case "patch": {
            const pp = part as PatchPart;
            for (const f of pp.files ?? []) {
              if (!turn.patchFiles.includes(f)) turn.patchFiles.push(f);
            }
            break;
          }
        }
        break;
      }

      // Session idle = turn complete, flush pending turns
      case "session.idle": {
        const { sessionID } = event.properties;
        const s = getSession(sessionID);
        await emitTurns(s);
        await langfuse.flushAsync();
        break;
      }

      // Server shutdown = flush everything
      case "server.instance.disposed": {
        for (const s of sessions.values()) {
          await emitTurns(s);
        }
        await langfuse.flushAsync();
        await sdk.shutdown();
        break;
      }
    }
  }

  return {
    config: async (config) => {
      if (!config.experimental?.openTelemetry) {
        log(
          "warn",
          "experimental.openTelemetry not enabled — OTEL baseline layer inactive (event-driven layer still running)",
        );
      }
    },

    event: async ({ event }) => {
      try {
        await handleEvent(event);
      } catch (err) {
        log("error", `event handler error: ${err}`);
      }
    },
  };
};

export default LangfusePlugin;
