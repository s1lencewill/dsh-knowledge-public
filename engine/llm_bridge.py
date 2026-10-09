"""Child-side LLM adapter: every model call travels over the job pipe.

This replaces `sirchmunk.llm.openai_chat.OpenAIChat` inside the isolated engine. It has no URL, no
key and no network: it can only serialize a request onto the pipe and read the parent's reply. Any
base_url/api_key/model the engine passes is ignored, which is exactly the point - the parent fixes
the provider and enforces the limits.
"""
import asyncio
import threading
import time
import uuid

import ipc
from runtime import ModelProxyRejected
from sirchmunk.llm.openai_chat import OpenAIChatResponse

_PIPE_PATH = None
_PARENT_MODEL = "parent-managed"
_TIMEOUT_MS = 180_000
_CONNECT_TIMEOUT_MS = 60_000
#: What is left of the job's budget when the bridge was installed. Every call - including the
#: reconnect retry - is bounded by it, so a lost reply cannot buy extra wall-clock time.
_BUDGET_SECONDS = None


class _Closer:
    def __init__(self, bridge=None):
        self._bridge = bridge

    def close(self):
        if self._bridge is not None:
            self._bridge.close()
        return None


class _AsyncCloser:
    async def close(self):
        return None


class OpenAIChatBridge:
    """Drop-in replacement for OpenAIChat, backed by the parent's model proxy.

    One persistent connection per job: the parent serves a single pipe instance, and the engine
    makes many calls (FAST is 2-3, DEEP is 6-7).
    """

    def __init__(self, base_url=None, api_key=None, model=None, **kwargs):
        if not _PIPE_PATH:
            raise RuntimeError("MODEL_PROXY_NOT_INSTALLED: install_bridge() must run before the engine")
        # Credentials and URLs passed by the engine are deliberately dropped here; the parent owns
        # the provider. The attribute is kept because upstream reads `llm._model`.
        self._model = _PARENT_MODEL
        self._handle = None
        self._lock = threading.Lock()
        self._client = _Closer(self)
        self._async_client = _AsyncCloser()
        self._ignored = {"base_url": base_url, "api_key": api_key, "model": model, **kwargs}

    def _request(self, messages, enable_thinking, options, request_id):
        payload = {"type": "chat", "id": request_id, "messages": messages, "options": {}}
        if enable_thinking is not None:
            payload["options"]["enable_thinking"] = enable_thinking
        for name in ("max_tokens", "timeout"):
            if options.get(name) is not None:
                payload["options"][name] = options[name]
        return payload

    def close(self):
        if self._handle is not None:
            ipc.close(self._handle)
            self._handle = None

    def _call_deadline(self):
        """The latest moment this logical call (both attempts included) may still be running."""
        limit = _TIMEOUT_MS / 1000.0
        if _BUDGET_SECONDS is not None:
            limit = min(limit, max(0.0, _BUDGET_SECONDS))
        return time.monotonic() + limit

    def _roundtrip(self, messages, enable_thinking, **kwargs):
        options = {name: kwargs[name] for name in ("max_tokens", "timeout") if name in kwargs}
        # One id for the whole logical call, reused across attempts: the parent caches the reply, so
        # a retry after a lost frame is served from that cache instead of paying the provider twice.
        request_id = uuid.uuid4().hex
        request = self._request(messages, enable_thinking, options, request_id)
        deadline = self._call_deadline()
        with self._lock:
            for attempt in (1, 2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("MODEL_CALL_BUDGET_EXHAUSTED: the job budget is spent")
                try:
                    if self._handle is None:
                        self._handle = ipc.connect(_PIPE_PATH,
                                                   timeout_ms=min(_CONNECT_TIMEOUT_MS, int(remaining * 1000)))
                    ipc.write_message(self._handle, request)
                    reply = ipc.read_message(self._handle, timeout=remaining)
                    break
                except ipc.PipeTimeout as failure:
                    self.close()
                    # A timeout before the bridge's own fixed limit means the *job budget* was the
                    # binding constraint: say so, and never retry past it.
                    if _BUDGET_SECONDS is not None and _BUDGET_SECONDS <= _TIMEOUT_MS / 1000.0:
                        raise RuntimeError("MODEL_CALL_BUDGET_EXHAUSTED: the job budget is spent "
                                           "before the parent answered") from failure
                    if attempt == 2:
                        raise
                except (ipc.PipeError, OSError):
                    self.close()
                    if attempt == 2 or deadline - time.monotonic() <= 0:
                        raise
        if not isinstance(reply, dict):
            raise RuntimeError("MODEL_PROXY_NO_REPLY")
        if reply.get("type") == "error":
            raise ModelProxyRejected(reply.get("code"))
        return OpenAIChatResponse(content=reply.get("content") or "", role=reply.get("role") or "assistant",
                                  usage=reply.get("usage") or {}, model=reply.get("model") or _PARENT_MODEL,
                                  finish_reason=reply.get("finish_reason"),
                                  thinking_content=reply.get("thinking_content"))

    def chat(self, messages, stream=False, enable_thinking=False, **kwargs):
        return self._roundtrip(messages, enable_thinking, **kwargs)

    async def achat(self, messages, stream=True, enable_thinking=False, **kwargs):
        # The pipe roundtrip is blocking; run it off the event loop so cancellation still works.
        return await asyncio.to_thread(self._roundtrip, messages, enable_thinking, **kwargs)


def install_bridge(pipe_path, model=None, budget_seconds=None):
    """Point every OpenAIChat reference in sirchmunk at the bridge.

    `budget_seconds` is what the parent says is left of the job's deadline; the bridge bounds every
    call (and its retry) by it instead of by its own fixed timeout.

    Modules that did `from sirchmunk.llm.openai_chat import OpenAIChat` hold their own binding, so
    each of them is patched as well; there is then no code path left that could build a real client.
    """
    global _PIPE_PATH, _PARENT_MODEL, _BUDGET_SECONDS
    _PIPE_PATH = pipe_path
    if model:
        _PARENT_MODEL = model
    if budget_seconds is not None:
        _BUDGET_SECONDS = max(0.0, float(budget_seconds))
    import importlib
    import sirchmunk.llm.openai_chat as openai_chat
    openai_chat.OpenAIChat = OpenAIChatBridge
    patched = ["sirchmunk.llm.openai_chat"]
    for name in ("sirchmunk", "sirchmunk.llm", "sirchmunk.api.chat", "sirchmunk.search", "sirchmunk.settings",
                 "sirchmunk.agentic.react_agent", "sirchmunk.agentic.tools", "sirchmunk.cli.cli"):
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        if getattr(module, "OpenAIChat", None) is not None and module.OpenAIChat is not OpenAIChatBridge:
            module.OpenAIChat = OpenAIChatBridge
            patched.append(name)
    return patched
