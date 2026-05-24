# LiteLLM → Langfuse

LiteLLM has native Langfuse integration via callbacks — no custom hook script needed.

## Setup

```python
import litellm

litellm.success_callback = ["langfuse"]
litellm.failure_callback = ["langfuse"]
```

Set the same environment variables used by the other hooks:

```bash
export LANGFUSE_PUBLIC_KEY="pk-lf-..."
export LANGFUSE_SECRET_KEY="sk-lf-..."
export LANGFUSE_BASE_URL="https://your-langfuse.example.com"   # omit for cloud
```

See the [LiteLLM Langfuse docs](https://docs.litellm.ai/docs/observability/langfuse_integration) for proxy and advanced configuration.
