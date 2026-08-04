#!/usr/bin/env python3
"""
Antigravity (desktop IDE + CLI) -> Langfuse hook, with optional LangWatch dual-export.

Unlike the Claude Code / Codex hooks, this one does NOT rely on parsing the hook's
stdin payload for a session/transcript path — Antigravity's native hook JSON schema
(fired via ~/.antigravity/settings.json's `hooks` block, already wired to a third-party
bridge on this machine) was never confirmed empirically, and guessing wrong would
silently drop data. Instead, this hook treats *any* invocation as a "something
happened, go check" signal and incrementally scans every conversation's own
transcript.jsonl directly:

    ~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl      (desktop IDE)
    ~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl  (CLI)

Each file is a flat JSONL log of steps (not grouped by message like Claude Code's
transcript), one row per step:
    {"step_index":N,"source":"USER_EXPLICIT"|"MODEL"|"SYSTEM","type":"USER_INPUT"|
     "PLANNER_RESPONSE"|"MCP_TOOL"|"RUN_COMMAND"|"CODE_ACTION"|"GREP_SEARCH"|
     "LIST_DIRECTORY"|"READ_URL_CONTENT"|"VIEW_FILE"|"CHECKPOINT"|
     "CONVERSATION_HISTORY"|"SYSTEM_MESSAGE"|"GENERIC",
     "status":"DONE","created_at":"...","content":"...","tool_calls":[...]}

No per-turn model name is available in this log (only surfaced as free text inside a
USER_SETTINGS_CHANGE block when the user changes it) -- known limitation, tracked in
gen_meta as "model_hint" rather than a hard "model" field.
"""

import glob
import hashlib
import json
import logging
import os
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
except Exception as _import_exc:
    try:
        _state_dir = Path.home() / ".antigravity" / "state"
        _state_dir.mkdir(parents=True, exist_ok=True)
        with open(_state_dir / "langfuse_hook.log", "a") as _f:
            _f.write(
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [ERROR] "
                f"Import failed under interpreter {sys.executable}: "
                f"{type(_import_exc).__name__}: {_import_exc}\n"
            )
    except Exception:
        pass
    sys.exit(0)

_HOSTNAME = socket.gethostname()

# --- Roots to scan: (agent_name, glob_pattern) ---
_ROOTS: List[Tuple[str, str]] = [
    ("antigravity", str(Path.home() / ".gemini" / "antigravity" / "brain" / "*" /
                        ".system_generated" / "logs" / "transcript.jsonl")),
    ("antigravity-cli", str(Path.home() / ".gemini" / "antigravity-cli" / "brain" / "*" /
                            ".system_generated" / "logs" / "transcript.jsonl")),
]

STATE_DIR = Path.home() / ".antigravity" / "state"
LOG_FILE = STATE_DIR / "langfuse_hook.log"
STATE_FILE = STATE_DIR / "langfuse_state.json"
LOCK_FILE = STATE_DIR / "langfuse_state.lock"

DEBUG = os.environ.get("AG_LANGFUSE_DEBUG", "").lower() == "true"
try:
    MAX_CHARS = int(os.environ.get("AG_LANGFUSE_MAX_CHARS", "20000"))
except ValueError:
    MAX_CHARS = 20000

# ----------------- Logging -----------------
_logger: Optional[logging.Logger] = None

def _get_logger() -> Optional[logging.Logger]:
    global _logger
    if _logger is not None:
        return _logger
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger("antigravity_langfuse_hook")
        lg.setLevel(logging.DEBUG if DEBUG else logging.INFO)
        if not lg.handlers:
            h = RotatingFileHandler(str(LOG_FILE), maxBytes=5_000_000, backupCount=3)
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
            ))
            lg.addHandler(h)
        _logger = lg
        return _logger
    except Exception:
        return None

def debug(msg: str) -> None:
    if not DEBUG:
        return
    lg = _get_logger()
    if lg is not None:
        try:
            lg.debug(msg)
        except Exception:
            pass

def info(msg: str) -> None:
    lg = _get_logger()
    if lg is not None:
        try:
            lg.info(msg)
        except Exception:
            pass

# ----------------- State locking (best-effort) -----------------
class FileLock:
    def __init__(self, path: Path, timeout_s: float = 2.0):
        self.path = path
        self.timeout_s = timeout_s
        self._fh = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        self.acquired = False
        try:
            import fcntl
        except ImportError:
            return self
        deadline = time.time() + self.timeout_s
        try:
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.acquired = True
                    return self
                except BlockingIOError:
                    if time.time() > deadline:
                        raise TimeoutError(f"could not acquire {self.path} within {self.timeout_s}s")
                    time.sleep(0.05)
        except BaseException:
            try:
                self._fh.close()
            except Exception:
                pass
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass

def load_state() -> Dict[str, Any]:
    try:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state: Dict[str, Any]) -> None:
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for k in list(state.keys()):
            entry = state.get(k)
            if not isinstance(entry, dict):
                continue
            updated = entry.get("updated")
            if not isinstance(updated, str):
                continue
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts < cutoff:
                del state[k]
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        debug(f"save_state failed: {e}")

def state_key(transcript_path: str) -> str:
    return hashlib.sha256(transcript_path.encode("utf-8")).hexdigest()

# ----------------- Incremental reader -----------------
@dataclass
class SessionState:
    offset: int = 0
    buffer: str = ""
    turn_count: int = 0

def load_session_state(global_state: Dict[str, Any], key: str) -> SessionState:
    s = global_state.get(key, {})
    return SessionState(
        offset=int(s.get("offset", 0)),
        buffer=str(s.get("buffer", "")),
        turn_count=int(s.get("turn_count", 0)),
    )

def write_session_state(global_state: Dict[str, Any], key: str, ss: SessionState) -> None:
    global_state[key] = {
        "offset": ss.offset,
        "buffer": ss.buffer,
        "turn_count": ss.turn_count,
        "updated": datetime.now(timezone.utc).isoformat(),
    }

def read_new_jsonl(transcript_path: Path, ss: SessionState) -> Tuple[List[Dict[str, Any]], SessionState]:
    if not transcript_path.exists():
        return [], ss
    try:
        file_size = transcript_path.stat().st_size
        if file_size < ss.offset:
            debug(f"{transcript_path} shrank ({file_size} < {ss.offset}); restarting")
            ss.offset = 0
            ss.buffer = ""
        with open(transcript_path, "rb") as f:
            f.seek(ss.offset)
            chunk = f.read()
            new_offset = f.tell()
    except Exception as e:
        debug(f"read_new_jsonl failed for {transcript_path}: {e}")
        return [], ss

    if not chunk:
        return [], ss

    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        text = chunk.decode(errors="replace")

    combined = ss.buffer + text
    lines = combined.split("\n")
    ss.buffer = lines[-1]
    ss.offset = new_offset

    rows: List[Dict[str, Any]] = []
    for line in lines[:-1]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows, ss

# ----------------- Turn assembly -----------------
_TOOL_TYPES = {
    "MCP_TOOL", "RUN_COMMAND", "CODE_ACTION", "GREP_SEARCH",
    "LIST_DIRECTORY", "READ_URL_CONTENT", "VIEW_FILE",
    "SEARCH_WEB", "ERROR_MESSAGE", "BROWSER_SUBAGENT", "ASK_QUESTION",
}
_SKIP_TYPES = {"CHECKPOINT", "CONVERSATION_HISTORY", "SYSTEM_MESSAGE", "GENERIC"}

@dataclass
class Turn:
    user_row: Dict[str, Any]
    gen_rows: List[Dict[str, Any]] = field(default_factory=list)   # PLANNER_RESPONSE rows
    tool_rows_after: Dict[int, List[Dict[str, Any]]] = field(default_factory=dict)  # gen_row idx -> tool rows

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_MODEL_HINT_RE = re.compile(r"from \S+ to ([^.]+?)\.\s*No need", re.IGNORECASE)

def extract_user_text(content: str) -> str:
    m = _USER_REQUEST_RE.search(content or "")
    return m.group(1).strip() if m else (content or "").strip()

def extract_model_hint(content: str) -> Optional[str]:
    m = _MODEL_HINT_RE.search(content or "")
    return m.group(1).strip() if m else None

# ----------------- Secret redaction -----------------
# Applied to every string before it leaves this process for Langfuse/LangWatch.
# See claude-code/hooks/langfuse_hook.py for the full rationale — same patterns,
# validated against a retroactive scan of this instance's real trace history
# that found live RSA private keys, GitHub PATs, Slack tokens, and hundreds of
# API keys sitting in plaintext. Antigravity's RUN_COMMAND/MCP_TOOL step content
# is raw shell output, same leak surface as Claude Code's Bash tool.
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

def truncate_text(s: str, max_chars: int = MAX_CHARS) -> Tuple[str, Dict[str, Any]]:
    if s is None:
        return "", {"truncated": False, "orig_len": 0}
    s = redact_secrets(s)
    orig_len = len(s)
    if orig_len <= max_chars:
        return s, {"truncated": False, "orig_len": orig_len}
    head = s[:max_chars]
    return head, {"truncated": True, "orig_len": orig_len, "kept_len": len(head)}

def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

def build_turns(rows: List[Dict[str, Any]]) -> Tuple[List[Turn], Optional[str]]:
    """Groups flat step rows into turns: USER_INPUT -> PLANNER_RESPONSE(s) -> tool rows."""
    turns: List[Turn] = []
    current: Optional[Turn] = None
    model_hint: Optional[str] = None

    for row in rows:
        rtype = row.get("type")
        content = row.get("content") or ""

        if rtype == "USER_INPUT":
            current = Turn(user_row=row)
            turns.append(current)
            continue

        if rtype in _SKIP_TYPES:
            hint = extract_model_hint(content)
            if hint:
                model_hint = hint
            continue

        if current is None:
            # Row before any USER_INPUT in this incremental batch (e.g. resumed
            # session) -- attach to a synthetic turn so nothing is silently dropped.
            current = Turn(user_row={"content": "", "created_at": row.get("created_at")})
            turns.append(current)

        if rtype == "PLANNER_RESPONSE":
            current.gen_rows.append(row)
            continue

        # Anything not explicitly a skip type is treated as tool-result content,
        # even if its `type` isn't in the known _TOOL_TYPES set. Google adds new
        # step types over time; defaulting to "capture" rather than "drop" means
        # an unrecognized future type still shows up in Langfuse instead of
        # silently vanishing. Known types are logged as debug info only when
        # they land outside _TOOL_TYPES, so real gaps are still visible.
        if rtype not in _TOOL_TYPES:
            debug(f"unrecognized step type treated as tool content: {rtype}")
        if current.gen_rows:
            idx = len(current.gen_rows) - 1
            current.tool_rows_after.setdefault(idx, []).append(row)

    return turns, model_hint

# ----------------- Langfuse emit (mirrors claude-code hook's backdated-span pattern) -----------------
def _to_ns(ts: Optional[datetime]) -> Optional[int]:
    if ts is None:
        return None
    return int(ts.timestamp() * 1_000_000_000)

def _start_backdated(langfuse: Langfuse, *, name: str, as_type: str,
                     start_time: Optional[datetime], parent_otel_span: Any = None,
                     **obs_kwargs: Any) -> Any:
    if not hasattr(langfuse, "_otel_tracer") or not hasattr(langfuse, "_create_observation_from_otel_span"):
        raise RuntimeError("Langfuse SDK missing _otel_tracer/_create_observation_from_otel_span; pin langfuse>=4.0,<5")
    start_ns = _to_ns(start_time)
    if parent_otel_span is not None:
        with otel_trace_api.use_span(parent_otel_span, end_on_exit=False):
            otel_span = langfuse._otel_tracer.start_span(name=name, start_time=start_ns)
    else:
        otel_span = langfuse._otel_tracer.start_span(name=name, start_time=start_ns)
    return langfuse._create_observation_from_otel_span(otel_span=otel_span, as_type=as_type, **obs_kwargs)

def emit_turn(langfuse: Langfuse, agent: str, conversation_id: str, turn_num: int,
              turn: Turn, transcript_path: Path, model_hint: Optional[str]) -> None:
    user_text_raw = extract_user_text(turn.user_row.get("content", ""))
    user_text, user_text_meta = truncate_text(user_text_raw)
    user_ts = parse_ts(turn.user_row.get("created_at"))

    final_text = ""
    for gr in reversed(turn.gen_rows):
        c = (gr.get("content") or "").strip()
        if c:
            final_text = c
            break
    final_text, _ = truncate_text(final_text)

    # Redact before slicing for the trace name -- this field isn't covered by
    # truncate_text()'s redaction (it's derived from the raw string directly),
    # and a trace's *name* is exactly as visible in the Langfuse/LangWatch UI
    # as its input/output, so it needs the same treatment.
    trace_name = redact_secrets(user_text_raw).strip().replace("\n", " ")[:72] or f"Antigravity - Turn {turn_num}"
    tags = ["antigravity", agent]

    trace_metadata: Dict[str, Any] = {
        "source": agent,
        "conversation_id": conversation_id,
        "turn_number": turn_num,
        "transcript_path": str(transcript_path),
        "hostname": _HOSTNAME,
        "generation_count": len(turn.gen_rows),
    }
    if model_hint:
        trace_metadata["model_hint"] = model_hint

    with propagate_attributes(
        session_id=conversation_id, trace_name=trace_name, tags=tags, user_id=_HOSTNAME,
    ):
        trace_span = _start_backdated(
            langfuse, name=trace_name, as_type="span", start_time=user_ts,
            input={"role": "user", "content": user_text}, metadata=trace_metadata,
        )
        parent_otel_span = trace_span._otel_span

        prev_ts = user_ts
        last_end_ts = user_ts
        for idx, gr in enumerate(turn.gen_rows):
            gr_ts = parse_ts(gr.get("created_at"))
            gr_text, gr_text_meta = truncate_text(gr.get("content") or "")
            tool_calls = gr.get("tool_calls") or []

            gen_output: Dict[str, Any] = {"role": "assistant"}
            if gr_text:
                gen_output["content"] = gr_text
            if tool_calls:
                gen_output["tool_calls"] = tool_calls

            gen_span = _start_backdated(
                langfuse, name=f"Antigravity Step {gr.get('step_index', idx)}", as_type="generation",
                start_time=prev_ts or gr_ts, parent_otel_span=parent_otel_span,
                model=model_hint or "antigravity",
                input={"role": "user", "content": user_text} if idx == 0 else None,
                output=gen_output,
                metadata={"step_index": gr.get("step_index"), "content_meta": gr_text_meta},
            )

            tool_rows = turn.tool_rows_after.get(idx, [])
            batch_end_ts = gr_ts
            for tr in tool_rows:
                tr_ts = parse_ts(tr.get("created_at")) or gr_ts
                tr_out, tr_out_meta = truncate_text(tr.get("content") or "")
                tool_span = _start_backdated(
                    langfuse, name=f"Tool: {tr.get('type', 'unknown')}", as_type="tool",
                    start_time=gr_ts, parent_otel_span=gen_span._otel_span,
                    input={"type": tr.get("type"), "step_index": tr.get("step_index")},
                    metadata={"output_meta": tr_out_meta},
                )
                tool_span.update(output=tr_out)
                tool_span.end(end_time=_to_ns(tr_ts))
                if tr_ts and (batch_end_ts is None or tr_ts > batch_end_ts):
                    batch_end_ts = tr_ts

            gen_end_ts = batch_end_ts or gr_ts or prev_ts
            gen_span.end(end_time=_to_ns(gen_end_ts))
            prev_ts = gen_end_ts
            last_end_ts = gen_end_ts or last_end_ts

        trace_span.update(output={"role": "assistant", "content": final_text})
        trace_span.end(end_time=_to_ns(last_end_ts or user_ts))

# ── LangWatch dual-export: same attribute-mirroring fix as the other hooks ────
def _mirror_langfuse_io_to_langwatch(span: Any) -> None:
    try:
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
    if os.environ.get("AG_LANGWATCH_ENABLED", "").lower() != "true":
        return
    api_key = os.environ.get("AG_LANGWATCH_API_KEY", "")
    endpoint = os.environ.get("AG_LANGWATCH_ENDPOINT", "https://app.langwatch.ai")
    if not api_key:
        debug("AG_LANGWATCH_ENABLED=true but AG_LANGWATCH_API_KEY not set; skipping.")
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
            endpoint=f"{endpoint.rstrip('/')}/api/otel/v1/traces",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        processor = BatchSpanProcessor(exporter)
        langfuse._otel_tracer.span_processor.add_span_processor(processor)
        debug(f"LangWatch dual-export wired -> {endpoint}")
    except Exception as e:
        debug(f"LangWatch dual-export setup failed (Langfuse unaffected): {type(e).__name__}: {e}")

# ----------------- Main -----------------
def main() -> int:
    start = time.time()
    debug("Antigravity hook started")

    if os.environ.get("TRACE_TO_LANGFUSE", "") != "true":
        return 0

    public_key = os.environ.get("AG_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("AG_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("AG_LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"
    if not public_key or not secret_key:
        return 0

    langfuse = None
    try:
        langfuse = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    except Exception:
        return 0

    wire_langwatch_export(langfuse)

    try:
        with FileLock(LOCK_FILE):
            state = load_state()
            total_emitted = 0

            for agent, pattern in _ROOTS:
                for transcript_path_str in glob.glob(pattern):
                    transcript_path = Path(transcript_path_str)
                    conversation_id = transcript_path.parents[2].name  # brain/<id>/.system_generated/logs
                    key = state_key(str(transcript_path))
                    ss = load_session_state(state, key)

                    rows, ss = read_new_jsonl(transcript_path, ss)
                    if not rows:
                        write_session_state(state, key, ss)
                        continue

                    turns, model_hint = build_turns(rows)
                    if not turns:
                        write_session_state(state, key, ss)
                        continue

                    for t in turns:
                        ss.turn_count += 1
                        try:
                            emit_turn(langfuse, agent, conversation_id, ss.turn_count, t,
                                      transcript_path, model_hint)
                            total_emitted += 1
                        except Exception as e:
                            info(f"emit_turn failed for {conversation_id}: {type(e).__name__}: {e}")

                    write_session_state(state, key, ss)

            save_state(state)
        dur = time.time() - start
        info(f"Processed {total_emitted} turns across all conversations in {dur:.2f}s")
        return 0

    except TimeoutError as e:
        debug(f"lock timeout, skipping: {e}")
        return 0
    except Exception as e:
        debug(f"Unexpected failure: {e}")
        return 0
    finally:
        if langfuse is not None:
            try:
                def _flush_and_shutdown():
                    try:
                        langfuse.flush()
                    except Exception:
                        pass
                    langfuse.shutdown()
                t = threading.Thread(target=_flush_and_shutdown, daemon=True)
                t.start()
                t.join(5.0)
            except Exception:
                pass

if __name__ == "__main__":
    sys.exit(main())
