#!/usr/bin/env python3
"""
Codex -> Langfuse conversational observability hook.

Reads ~/.codex/sessions/**/*.jsonl and ships conversation turns to Langfuse.
Triggered by notify-wrapper.sh on every Codex turn-end.

Configuration (environment variables):
  CODEX_LANGFUSE_PUBLIC_KEY   Langfuse project public key  (required)
  CODEX_LANGFUSE_SECRET_KEY   Langfuse project secret key  (required)
  CODEX_LANGFUSE_BASE_URL     Langfuse host URL             (default: https://cloud.langfuse.com)
  CODEX_LANGFUSE_DEBUG        Set to "true" for verbose logging

  Falls back to the generic LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
  LANGFUSE_BASE_URL if the CODEX_LANGFUSE_* variants are not set.
  Using the prefixed variants is recommended when running multiple agents
  (Claude Code, OpenCode) so each agent's keys remain isolated.
"""

import glob
import hashlib
import json
import logging
import os
import platform
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from langfuse import Langfuse, propagate_attributes
    from opentelemetry import trace as otel_trace_api
except Exception:
    sys.exit(0)

_HOSTNAME = socket.gethostname()
try:
    import urllib.request as _ur
    with _ur.urlopen("https://api.ipify.org", timeout=2) as _r:
        _IP_ADDRESS = _r.read().decode().strip()
except Exception:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as _s:
            _s.connect(("8.8.8.8", 80))
            _IP_ADDRESS = _s.getsockname()[0]
    except Exception:
        _IP_ADDRESS = None
_OS_USER  = os.environ.get("USER") or os.environ.get("USERNAME") or os.environ.get("LOGNAME")
_PLATFORM = platform.system()
_PID      = os.getpid()

# ── Config ────────────────────────────────────────────────────────────────────
CODEX_HOME   = Path.home() / ".codex"
SESSIONS_DIR = CODEX_HOME / "sessions"
STATE_DIR    = CODEX_HOME / "state"
STATE_FILE   = STATE_DIR / "langfuse_codex_state.json"
LOG_FILE     = STATE_DIR / "langfuse_codex_hook.log"
MAX_CHARS    = 50_000
RECENT_HOURS = 48
DEBUG        = os.environ.get("CODEX_LANGFUSE_DEBUG", "").lower() == "true"

PUBLIC_KEY = os.environ.get("CODEX_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
SECRET_KEY = os.environ.get("CODEX_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY", "")
HOST       = os.environ.get("CODEX_LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

# ── LangWatch dual-export (opt-in, additive, never blocks Langfuse) ────────────
# Evaluation-period parallel export: attaches a second OTLP span processor to the
# SAME multi-span-processor Langfuse's own tracer uses, so every span already
# built for Langfuse fans out to LangWatch too, with no separate SDK/client and
# no duplicated span-building logic. If anything about this fails, it must never
# affect the Langfuse path — see wire_langwatch_export() below.
LANGWATCH_ENABLED     = os.environ.get("CODEX_LANGWATCH_ENABLED", "").lower() == "true"
LANGWATCH_API_KEY     = os.environ.get("CODEX_LANGWATCH_API_KEY", "")
LANGWATCH_ENDPOINT    = os.environ.get("CODEX_LANGWATCH_ENDPOINT", "https://app.langwatch.ai")

# ── Logging ───────────────────────────────────────────────────────────────────
_logger: Optional[logging.Logger] = None

def _get_logger() -> Optional[logging.Logger]:
    global _logger
    if _logger:
        return _logger
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger("codex_langfuse")
        lg.setLevel(logging.DEBUG if DEBUG else logging.INFO)
        if not lg.handlers:
            h = RotatingFileHandler(str(LOG_FILE), maxBytes=5_000_000, backupCount=2)
            h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            lg.addHandler(h)
        _logger = lg
        return _logger
    except Exception:
        return None

def log_info(msg: str) -> None:
    lg = _get_logger()
    if lg:
        try: lg.info(msg)
        except Exception: pass

def log_debug(msg: str) -> None:
    if not DEBUG: return
    lg = _get_logger()
    if lg:
        try: lg.debug(msg)
        except Exception: pass

# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> Dict[str, Any]:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}

def save_state(state: Dict[str, Any]) -> None:
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        for k in list(state.keys()):
            try:
                ts = datetime.fromisoformat(state[k].get("updated", "").replace("Z", "+00:00"))
                if ts < cutoff:
                    del state[k]
            except Exception:
                pass
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log_debug(f"save_state failed: {e}")

def state_key(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:16]

# ── Session discovery ─────────────────────────────────────────────────────────
def find_recent_sessions() -> List[Path]:
    cutoff = time.time() - (RECENT_HOURS * 3600)
    pattern = str(SESSIONS_DIR / "**" / "rollout-*.jsonl")
    paths = []
    for p in glob.glob(pattern, recursive=True):
        try:
            if os.path.getmtime(p) >= cutoff:
                paths.append(Path(p))
        except Exception:
            pass
    return sorted(paths, key=lambda p: p.stat().st_mtime)

# ── JSONL reading ─────────────────────────────────────────────────────────────
def read_new_lines(path: Path, offset: int) -> Tuple[List[Dict], int]:
    try:
        size = path.stat().st_size
        if size < offset:
            offset = 0
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
        if not chunk:
            return [], offset
        rows = []
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try: rows.append(json.loads(line))
                except Exception: pass
        return rows, new_offset
    except Exception as e:
        log_debug(f"read_new_lines failed on {path}: {e}")
        return [], offset

# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class SessionMeta:
    session_id: str = ""
    cli_version: str = ""
    cwd: str = ""
    model_provider: str = "openai"
    system_prompt: str = ""

@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: Any
    output: Optional[str] = None
    start_ts: Optional[datetime] = None
    end_ts: Optional[datetime] = None

@dataclass
class FileEdit:
    path: str
    unified_diff: str = ""
    success: bool = True
    stdout: str = ""
    stderr: str = ""
    changes: Dict = field(default_factory=dict)
    ts: Optional[datetime] = None

@dataclass
class Turn:
    turn_id: str
    user_message: str = ""
    agent_messages: List[str] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    file_edits: List[FileEdit] = field(default_factory=list)
    memory_citations: List[str] = field(default_factory=list)
    # model info
    model: str = "gpt-5.5"
    reasoning_effort: str = ""
    collab_mode_name: str = ""
    personality: str = ""
    cwd: str = ""
    context_window: int = 0
    # token usage
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    # timing
    start_ts: Optional[datetime] = None
    end_ts: Optional[datetime] = None
    duration_ms: Optional[int] = None
    time_to_first_token_ms: Optional[int] = None

# ── Parsing ───────────────────────────────────────────────────────────────────
def parse_ts(row: Any) -> Optional[datetime]:
    ts = row.get("timestamp") if isinstance(row, dict) else None
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None

def build_turns(rows: List[Dict]) -> Tuple[List[Turn], SessionMeta]:
    meta = SessionMeta()
    turns: List[Turn] = []
    current: Optional[Turn] = None
    pending_tools: Dict[str, ToolCall] = {}
    last_model = "gpt-5.5"
    last_effort = ""
    last_personality = ""
    last_collab_mode_name = ""

    for row in rows:
        rtype   = row.get("type")
        payload = row.get("payload") or {}
        ptype   = payload.get("type") if isinstance(payload, dict) else None
        ts      = parse_ts(row)

        if rtype == "session_meta":
            meta.session_id     = payload.get("id", "")
            meta.cli_version    = payload.get("cli_version", "")
            meta.cwd            = payload.get("cwd", "")
            meta.model_provider = payload.get("model_provider", "openai")
            base_instructions   = payload.get("base_instructions") or {}
            meta.system_prompt  = base_instructions.get("text", "") if isinstance(base_instructions, dict) else ""
            continue

        if rtype == "turn_context":
            last_model       = payload.get("model", last_model)
            last_effort      = payload.get("effort", last_effort)
            last_personality = payload.get("personality", last_personality)
            collab = payload.get("collaboration_mode") or {}
            collab_name = collab.get("name", "")
            if collab_name:
                last_collab_mode_name = collab_name
            settings = collab.get("settings") or {}
            if settings.get("reasoning_effort"):
                last_effort = settings["reasoning_effort"]
            if current:
                current.model            = last_model
                current.reasoning_effort = last_effort
                current.collab_mode_name = last_collab_mode_name
                current.personality      = last_personality
                current.cwd              = payload.get("cwd", meta.cwd)
            continue

        if rtype == "event_msg" and ptype == "task_started":
            if current and current.user_message:
                turns.append(current)
            current = Turn(
                turn_id=payload.get("turn_id", ""),
                model=last_model,
                reasoning_effort=last_effort,
                collab_mode_name=last_collab_mode_name,
                personality=last_personality,
                cwd=meta.cwd,
                context_window=payload.get("model_context_window", 0),
                start_ts=ts,
            )
            pending_tools = {}
            continue

        if rtype == "event_msg" and ptype == "task_complete":
            if current:
                if ts:
                    current.end_ts = ts
                current.duration_ms            = payload.get("duration_ms")
                current.time_to_first_token_ms = payload.get("time_to_first_token_ms")
                turns.append(current)
                current = None
            continue

        if current is None:
            continue

        if rtype == "event_msg" and ptype == "user_message":
            current.user_message = payload.get("message", "")
            if ts and not current.start_ts:
                current.start_ts = ts
            continue

        if rtype == "event_msg" and ptype == "agent_message":
            msg = payload.get("message", "")
            if msg:
                current.agent_messages.append(msg)
            citation = payload.get("memory_citation")
            if citation:
                current.memory_citations.append(citation if isinstance(citation, str) else json.dumps(citation))
            if ts:
                current.end_ts = ts
            continue

        if rtype == "event_msg" and ptype == "token_count":
            info = payload.get("info") or {}
            usage = info.get("last_token_usage") or {}
            if usage:
                current.input_tokens     = usage.get("input_tokens", 0)
                current.output_tokens    = usage.get("output_tokens", 0)
                current.cached_tokens    = usage.get("cached_input_tokens", 0)
                current.reasoning_tokens = usage.get("reasoning_output_tokens", 0)
            continue

        if rtype == "response_item" and ptype in ("function_call", "custom_tool_call"):
            call_id = payload.get("call_id", "")
            tc = ToolCall(
                call_id=call_id,
                name=payload.get("name", "unknown"),
                arguments=payload.get("arguments") or payload.get("input"),
                start_ts=ts,
            )
            pending_tools[call_id] = tc
            current.tool_calls.append(tc)
            continue

        if rtype == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id", "")
            if call_id in pending_tools:
                pending_tools[call_id].output = payload.get("output") or payload.get("result")
                pending_tools[call_id].end_ts  = ts
            continue

        if rtype == "response_item" and ptype == "web_search_call":
            call_id = payload.get("call_id", f"ws-{len(current.tool_calls)}")
            action  = payload.get("action") or {}
            queries = action.get("queries", []) if isinstance(action, dict) else []
            tc = ToolCall(
                call_id=call_id,
                name="web_search",
                arguments={"queries": queries} if queries else {"action": action},
                start_ts=ts,
            )
            pending_tools[call_id] = tc
            current.tool_calls.append(tc)
            continue

        if rtype == "event_msg" and ptype == "web_search_end":
            call_id = payload.get("call_id", "")
            if call_id in pending_tools:
                pending_tools[call_id].output = payload.get("query")
                pending_tools[call_id].end_ts  = ts
            continue

        if rtype == "event_msg" and ptype == "patch_apply_end":
            fe = FileEdit(
                path=payload.get("path", ""),
                unified_diff=payload.get("unified_diff", ""),
                success=payload.get("success", True),
                stdout=payload.get("stdout", ""),
                stderr=payload.get("stderr", ""),
                changes=payload.get("changes") or {},
                ts=ts,
            )
            current.file_edits.append(fe)
            continue

    if current and current.user_message:
        turns.append(current)

    return turns, meta

# ── Langfuse emit ─────────────────────────────────────────────────────────────
def _to_ns(ts: Optional[datetime]) -> Optional[int]:
    return int(ts.timestamp() * 1_000_000_000) if ts else None

def _start_backdated(langfuse: Langfuse, *, name: str, as_type: str,
                     start_time: Optional[datetime],
                     parent_otel_span: Any = None,
                     **kwargs: Any) -> Any:
    if not hasattr(langfuse, "_otel_tracer") or not hasattr(langfuse, "_create_observation_from_otel_span"):
        raise RuntimeError("Langfuse SDK missing internal API — pin to >=4.0,<5")
    start_ns = _to_ns(start_time)
    if parent_otel_span is not None:
        with otel_trace_api.use_span(parent_otel_span, end_on_exit=False):
            otel_span = langfuse._otel_tracer.start_span(name=name, start_time=start_ns)
    else:
        otel_span = langfuse._otel_tracer.start_span(name=name, start_time=start_ns)
    return langfuse._create_observation_from_otel_span(otel_span=otel_span, as_type=as_type, **kwargs)

# ----------------- Secret redaction -----------------
# Applied to every string before it leaves this process for Langfuse/LangWatch.
# See claude-code/hooks/langfuse_hook.py for the full rationale — same patterns,
# validated against a retroactive scan of this instance's real trace history
# that found live RSA private keys, GitHub PATs, Slack tokens, and hundreds of
# API keys sitting in plaintext.
_SECRET_REPLACERS: List[Tuple[Any, Any]] = [
    (re.compile(r'-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----', re.DOTALL),
     lambda m: '[REDACTED:private_key]'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), lambda m: '[REDACTED:aws_key_id]'),
    (re.compile(r'\bsk-lf-[a-f0-9-]{20,}\b'), lambda m: '[REDACTED:langfuse_secret_key]'),
    (re.compile(r'\bpk-lf-[a-f0-9-]{20,}\b'), lambda m: '[REDACTED:langfuse_public_key]'),
    (re.compile(r'\bsk-lw-[A-Za-z0-9_]{20,}\b'), lambda m: '[REDACTED:langwatch_key]'),
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'), lambda m: '[REDACTED:github_token]'),
    (re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b'), lambda m: '[REDACTED:slack_token]'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'), lambda m: '[REDACTED:jwt]'),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b'), lambda m: '[REDACTED:google_api_key]'),
    (re.compile(r'\bnpm_[A-Za-z0-9]{30,}\b'), lambda m: '[REDACTED:npm_token]'),
    (re.compile(r'\bsk-[A-Za-z0-9]{32,}\b'), lambda m: '[REDACTED:api_key]'),
    (re.compile(r'(?i)((?:password|passwd|pass|pwd|secret|token|key)["\']?\s*(?:is\s+|[:=]\s*|,\s*))(["\']?)((?!\[REDACTED)[^"\'\s,]{6,})\2'),
     lambda m: f'{m.group(1)}{m.group(2)}[REDACTED]{m.group(2)}'),
    (re.compile(r'([a-zA-Z][a-zA-Z0-9+.\-]*://[^:/\s"\']+):[^@/\s"\']+@'), lambda m: f'{m.group(1)}:[REDACTED]@'),
]

def redact_secrets(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    for pattern, repl in _SECRET_REPLACERS:
        text = pattern.sub(repl, text)
    return text

def redact_metadata(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_secrets(obj)
    if isinstance(obj, list):
        return [redact_metadata(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_metadata(v) for k, v in obj.items()}
    return obj

def trunc(s: Any, n: int = MAX_CHARS) -> str:
    if s is None:
        return ""
    text = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    text = redact_secrets(text)
    return text[:n] + ("…" if len(text) > n else "")

def trace_name(turn: Turn) -> str:
    # Redact before slicing -- this field isn't covered by trunc()'s redaction
    # (it's derived from the raw message directly), and a trace's *name* is
    # exactly as visible in the Langfuse/LangWatch UI as its input/output.
    preview = redact_secrets(turn.user_message or "").strip().replace("\n", " ")[:72]
    return preview if preview else f"Turn {turn.turn_id[:8]}"

def emit_turn(langfuse: Langfuse, session_id: str, turn_num: int,
              turn: Turn, smeta: SessionMeta, session_path: Path) -> None:
    user_text  = trunc(turn.user_message)
    agent_text = trunc("\n\n".join(turn.agent_messages))
    name       = trace_name(turn)

    tags = ["codex", turn.model]
    if turn.reasoning_effort:
        tags.append(f"effort:{turn.reasoning_effort}")
    if turn.collab_mode_name:
        tags.append(f"mode:{turn.collab_mode_name}")

    trace_metadata = {
        "source":           "codex",
        "session_id":       session_id,
        "turn_number":      turn_num,
        "turn_id":          turn.turn_id,
        "session_path":     str(session_path),
        "tool_count":       len(turn.tool_calls),
        "file_edit_count":  len(turn.file_edits),
        "cli_version":      smeta.cli_version,
        "cwd":              turn.cwd or smeta.cwd,
        "model_provider":   smeta.model_provider,
        "personality":      turn.personality,
        "collab_mode_name": turn.collab_mode_name,
        "hostname":         _HOSTNAME,
        "pid":              _PID,
        "platform":         _PLATFORM,
    }
    if _IP_ADDRESS:
        trace_metadata["ip_address"] = _IP_ADDRESS
    if _OS_USER:
        trace_metadata["os_user"] = _OS_USER
    if turn.context_window:
        trace_metadata["context_window"] = turn.context_window
    if turn.duration_ms is not None:
        trace_metadata["duration_ms"] = turn.duration_ms
    if turn.time_to_first_token_ms is not None:
        trace_metadata["time_to_first_token_ms"] = turn.time_to_first_token_ms
    if smeta.system_prompt:
        trace_metadata["system_prompt"] = trunc(smeta.system_prompt, 2000)

    trace_metadata = redact_metadata(trace_metadata)

    with propagate_attributes(
        session_id=session_id,
        trace_name=name,
        tags=tags,
        version=smeta.cli_version or None,
        user_id=_HOSTNAME,
    ):
        trace_span = _start_backdated(
            langfuse,
            name=name,
            as_type="span",
            start_time=turn.start_ts,
            input={"role": "user", "content": user_text},
            metadata=trace_metadata,
        )
        parent_span = trace_span._otel_span

        gen_metadata: Dict[str, Any] = {
            "tool_count":             len(turn.tool_calls),
            "file_edit_count":        len(turn.file_edits),
            "time_to_first_token_ms": turn.time_to_first_token_ms,
            "duration_ms":            turn.duration_ms,
            "personality":            turn.personality,
        }
        if turn.memory_citations:
            gen_metadata["memory_citations"] = turn.memory_citations

        gen_kwargs: Dict[str, Any] = dict(
            model=turn.model,
            model_parameters={
                "reasoning_effort":   turn.reasoning_effort,
                "collaboration_mode": turn.collab_mode_name,
                "provider":           smeta.model_provider,
            },
            input={"role": "user", "content": user_text},
            output={"role": "assistant", "content": agent_text},
            metadata=gen_metadata,
        )
        if turn.input_tokens or turn.output_tokens:
            usage: Dict[str, int] = {
                "input":  turn.input_tokens,
                "output": turn.output_tokens,
            }
            if turn.cached_tokens:
                usage["cache_read_input_tokens"] = turn.cached_tokens
            if turn.reasoning_tokens:
                usage["reasoning_output_tokens"] = turn.reasoning_tokens
            gen_kwargs["usage_details"] = usage

        gen_span = _start_backdated(
            langfuse,
            name="Codex Generation",
            as_type="generation",
            start_time=turn.start_ts,
            parent_otel_span=parent_span,
            **gen_kwargs,
        )

        for tc in turn.tool_calls:
            args = tc.arguments
            if isinstance(args, str):
                try: args = json.loads(args)
                except Exception: pass
            tool_span = _start_backdated(
                langfuse,
                name=f"Tool: {tc.name}",
                as_type="tool",
                start_time=tc.start_ts or turn.start_ts,
                parent_otel_span=gen_span._otel_span,
                input=trunc(args),
                metadata={"tool_name": tc.name, "call_id": tc.call_id},
            )
            tool_span.update(output=trunc(tc.output))
            tool_span.end(end_time=_to_ns(tc.end_ts or turn.end_ts or turn.start_ts))

        for fe in turn.file_edits:
            file_span = _start_backdated(
                langfuse,
                name=f"Edit: {Path(fe.path).name if fe.path else 'unknown'}",
                as_type="tool",
                start_time=fe.ts or turn.end_ts or turn.start_ts,
                parent_otel_span=gen_span._otel_span,
                input={"path": fe.path, "diff": trunc(fe.unified_diff)},
                metadata={
                    "tool_name":  "patch_apply",
                    "path":       fe.path,
                    "success":    fe.success,
                    "changes":    fe.changes,
                    "has_stderr": bool(fe.stderr),
                },
            )
            file_span.update(output=trunc(fe.stdout or ("" if fe.success else fe.stderr)))
            file_span.end(end_time=_to_ns(fe.ts or turn.end_ts or turn.start_ts))

        gen_span.end(end_time=_to_ns(turn.end_ts or turn.start_ts))
        trace_span.update(output={"role": "assistant", "content": agent_text})
        trace_span.end(end_time=_to_ns(turn.end_ts or turn.start_ts))

def _mirror_langfuse_io_to_langwatch(span: Any) -> None:
    """Copy Langfuse's proprietary input/output attrs onto the langwatch.input/
    langwatch.output keys LangWatch's own ingestion actually reads. Langfuse and
    LangWatch use disjoint OTel attribute namespaces for the same data, so without
    this a dual-exported span shows real content in Langfuse but <empty> in
    LangWatch. Best-effort: any failure here must not affect either export path.
    """
    try:
        # ReadableSpan.attributes always returns a fresh read-only
        # MappingProxyType — mutating it raises TypeError. The real,
        # mutable BoundedAttributes store lives on the private _attributes
        # attribute. On opentelemetry-sdk >=1.43 Span.end() additionally
        # sets attrs._immutable = True right before handing the span to
        # processors (freezing it against further writes) — toggle that
        # flag off for the duration of the write, then restore it, so this
        # works across the SDK-version skew across the fleet.
        attrs = getattr(span, "_attributes", None) or getattr(span, "attributes", None)
        if not attrs:
            return
        lw_in = attrs.get("langfuse.observation.input", attrs.get("langfuse.trace.input"))
        lw_out = attrs.get("langfuse.observation.output", attrs.get("langfuse.trace.output"))
        was_immutable = getattr(attrs, "_immutable", False)
        if was_immutable:
            attrs._immutable = False
        try:
            if lw_in is not None and "langwatch.input" not in attrs:
                attrs["langwatch.input"] = lw_in
            if lw_out is not None and "langwatch.output" not in attrs:
                attrs["langwatch.output"] = lw_out
        finally:
            if was_immutable:
                attrs._immutable = True
    except Exception:
        pass


def wire_langwatch_export(langfuse: "Langfuse") -> None:
    """Attach a LangWatch OTLP exporter to Langfuse's own multi-span-processor.

    Best-effort only: any failure here is logged and swallowed. This must never
    prevent or delay the existing Langfuse export path.
    """
    if not LANGWATCH_ENABLED:
        return
    if not LANGWATCH_API_KEY:
        log_debug("CODEX_LANGWATCH_ENABLED=true but CODEX_LANGWATCH_API_KEY not set; skipping.")
        return
    try:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        class _MirroringOTLPSpanExporter(OTLPSpanExporter):
            def export(self, spans):
                for _span in spans:
                    _mirror_langfuse_io_to_langwatch(_span)
                return super().export(spans)

        exporter = _MirroringOTLPSpanExporter(
            endpoint=f"{LANGWATCH_ENDPOINT.rstrip('/')}/api/otel/v1/traces",
            headers={"Authorization": f"Bearer {LANGWATCH_API_KEY}"},
        )
        processor = BatchSpanProcessor(exporter)
        langfuse._otel_tracer.span_processor.add_span_processor(processor)
        log_debug(f"LangWatch dual-export wired -> {LANGWATCH_ENDPOINT}")
    except Exception as e:
        log_debug(f"LangWatch dual-export setup failed (Langfuse unaffected): {type(e).__name__}: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    start = time.time()
    log_debug("Codex Langfuse hook started")

    if not PUBLIC_KEY or not SECRET_KEY:
        log_debug("CODEX_LANGFUSE_PUBLIC_KEY / CODEX_LANGFUSE_SECRET_KEY not set; exiting.")
        return 0

    langfuse = None
    try:
        langfuse = Langfuse(public_key=PUBLIC_KEY, secret_key=SECRET_KEY, host=HOST)
    except Exception as e:
        log_debug(f"Langfuse init failed: {e}")
        return 0

    wire_langwatch_export(langfuse)

    state = load_state()
    total_emitted = 0

    for session_path in find_recent_sessions():
        key        = state_key(session_path)
        entry      = state.get(key, {})
        offset     = int(entry.get("offset", 0))
        turn_count = int(entry.get("turn_count", 0))
        session_id = entry.get("session_id", "")

        rows, new_offset = read_new_lines(session_path, offset)
        if not rows and new_offset == offset:
            continue

        turns, smeta = build_turns(rows)
        if smeta.session_id:
            session_id = smeta.session_id

        if not turns:
            state[key] = {
                "offset": new_offset, "turn_count": turn_count,
                "session_id": session_id, "path": str(session_path),
                "updated": datetime.now(timezone.utc).isoformat(),
            }
            continue

        emitted = 0
        for turn in turns:
            if not turn.user_message:
                continue
            try:
                emit_turn(langfuse, session_id, turn_count + emitted + 1,
                          turn, smeta, session_path)
                emitted += 1
                log_debug(f"  turn {turn_count+emitted}: model={turn.model} "
                          f"effort={turn.reasoning_effort} "
                          f"in={turn.input_tokens} out={turn.output_tokens} "
                          f"tools={len(turn.tool_calls)}")
            except Exception as e:
                log_info(f"emit_turn failed: {type(e).__name__}: {e}")

        turn_count += emitted
        total_emitted += emitted
        state[key] = {
            "offset": new_offset, "turn_count": turn_count,
            "session_id": session_id, "path": str(session_path),
            "updated": datetime.now(timezone.utc).isoformat(),
        }

    save_state(state)
    if total_emitted:
        log_info(f"Emitted {total_emitted} turns in {time.time()-start:.2f}s")

    if langfuse:
        try:
            t = threading.Thread(target=lambda: (langfuse.flush(), langfuse.shutdown()), daemon=True)
            t.start()
            t.join(8.0)
        except Exception:
            pass

    return 0

if __name__ == "__main__":
    sys.exit(main())
