"""
LiteLLM custom guardrail: redact secrets from prompts and responses before
they reach any logging callback (langfuse_otel, otel, etc.).

Same rationale and pattern set as the Claude Code / Codex / Antigravity /
OpenCode hooks in this repo: a retroactive scan of real trace history found
live RSA private keys, GitHub PATs, Slack tokens, and hundreds of API keys
sitting in plaintext in Langfuse/LangWatch, because tool/shell output and
copy-pasted config routinely contains real credentials with no signal to the
model that it's sensitive.

Config.yaml registration:

    guardrails:
      - guardrail_name: "secret-redaction"
        litellm_params:
          guardrail: secret_redaction_guardrail.SecretRedactionGuardrail
          mode: ["pre_call", "post_call"]

Verified empirically (not assumed from docs, which don't document this):
async_pre_call_hook's in-place mutation of data["messages"] IS what the
langfuse_otel/otel logging callbacks see, since litellm's logging captures
the request/response objects as they exist in the pipeline at
success/failure time -- i.e. after guardrails have run, not the original
wire payload.
"""

import re
from typing import Any, Dict, List, Optional, Tuple, Union

from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import UserAPIKeyAuth
from litellm.caching.caching import DualCache

# ----------------- Secret redaction (identical pattern set to the other hooks) -----------------
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
    (re.compile(r'(?i)((?:password|passwd|pass|pwd|secret|api[_ \-]?key|access[_ \-]?key|auth[_ \-]?token)["\']?\s*(?:is\s+|[:=]\s*))(["\']?)([^"\'\s]{6,})\2'),
     lambda m: f'{m.group(1)}{m.group(2)}[REDACTED]{m.group(2)}'),
    (re.compile(r'([a-zA-Z][a-zA-Z0-9+.\-]*://[^:/\s"\']+):[^@/\s"\']+@'), lambda m: f'{m.group(1)}:[REDACTED]@'),
]


def redact_secrets(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    for pattern, repl in _SECRET_REPLACERS:
        text = pattern.sub(repl, text)
    return text


def _redact_message_content(content: Any) -> Any:
    """Message content is either a plain string or a list of content-part dicts
    (multimodal: [{"type": "text", "text": "..."}, {"type": "image_url", ...}])."""
    if isinstance(content, str):
        return redact_secrets(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                part = {**part, "text": redact_secrets(part["text"])}
            out.append(part)
        return out
    return content


class SecretRedactionGuardrail(CustomGuardrail):
    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: str,
    ) -> Optional[Union[Exception, str, dict]]:
        messages = data.get("messages")
        if messages:
            for message in messages:
                if isinstance(message, dict) and "content" in message:
                    message["content"] = _redact_message_content(message["content"])
        return data

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict: UserAPIKeyAuth,
        response,
    ):
        try:
            import litellm
            if isinstance(response, litellm.ModelResponse):
                for choice in response.choices:
                    if isinstance(choice, litellm.Choices) and isinstance(
                        getattr(choice.message, "content", None), str
                    ):
                        choice.message.content = redact_secrets(choice.message.content)
        except Exception:
            # Never let a redaction bug break the actual response to the caller.
            pass
        return response
