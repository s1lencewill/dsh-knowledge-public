"""Parent-side model proxy: the isolated engine never holds the key or a URL.

The container has no network capability, so every model call travels over the job's named pipe.
This module owns the provider configuration (base URL, model, key) and enforces the limits the
child cannot relax:

* only `messages`, `enable_thinking`, `max_tokens` and `timeout` may be sent;
* `model`, `base_url`, `api_key`, any header, proxy or transport override is rejected outright
  (and never forwarded), so the child cannot choose a provider or smuggle credentials;
* request size, number of calls, total tokens and the wall-clock deadline are all capped here.

Requests are treated as untrusted input: a malformed or oversized frame is answered with a
structured error instead of taking the parent down.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import time

from ipc import PipeDisconnected, PipeMessageTooLarge, PipeTimeout, MAX_MESSAGE

PROTOCOL = 1
ALLOWED_KEYS = {"enable_thinking", "max_tokens", "timeout"}
FORBIDDEN_KEYS = {
    "model", "models", "base_url", "api_base", "api_key", "api_key_env", "organization", "project",
    "headers", "extra_headers", "default_headers", "authorization", "auth", "proxy", "proxies",
    "transport", "http_client", "client", "stream", "n", "tools", "tool_choice", "response_format",
}
#: A test-window spend ceiling that survives process restarts. It is checked, and spent, *before* a
#: request is sent, so a request that later fails or is cancelled has already counted against it.
QUOTA_FILE_ENV = "DSH_KNOWLEDGE_QUOTA_FILE"
QUOTA_MAX_ENV = "DSH_KNOWLEDGE_QUOTA_MAX"
_QUOTA_LOCK_TIMEOUT_SECONDS = 5.0


def quota_state(path=None, limit=None):
    """(used, limit, path) of the window budget; (None, None, None) when no budget is configured."""
    path = path if path is not None else os.environ.get(QUOTA_FILE_ENV)
    if not path:
        return None, None, None
    if limit is None:
        try:
            limit = int(os.environ.get(QUOTA_MAX_ENV, ""))
        except (TypeError, ValueError):
            limit = None
    used = 0
    try:
        used = int(json.loads(Path(path).read_text(encoding="utf-8")).get("used", 0))
    except (OSError, ValueError, AttributeError):
        used = 0
    return used, limit, path


def reserve_quota():
    """Spend one slot of the window budget. Returns None to go ahead, else the reason to refuse.

    The reserve happens before the provider is called, so the count is of requests *sent*, including the
    ones that fail or are cancelled afterwards. An unreadable budget file refuses the request rather than
    letting it through: a ceiling that a disk error can lift is not a ceiling.
    """
    used, limit, path = quota_state()
    if path is None or limit is None:
        return None
    lock = path + ".lock"
    handle, deadline = None, time.monotonic() + _QUOTA_LOCK_TIMEOUT_SECONDS
    while handle is None:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() > deadline:
                return "quota file is locked by another writer"
            time.sleep(0.02)
        except OSError as error:
            return "quota file unavailable: %s" % type(error).__name__
    try:
        used, limit, _ = quota_state()
        if limit is None:
            return None
        if used >= limit:
            return "provider request budget of %d is spent" % limit
        Path(path).write_text(json.dumps({"used": used + 1, "limit": limit}), encoding="utf-8")
        return None
    except OSError as error:
        return "quota file unavailable: %s" % type(error).__name__
    finally:
        os.close(handle)
        try:
            os.unlink(lock)
        except OSError:
            pass


class Limits:
    def __init__(self, max_request_bytes=64 * 1024, max_calls=40, max_total_tokens=60_000,
                 deadline_seconds=240.0, max_messages=200):
        self.max_request_bytes = max_request_bytes
        self.max_calls = max_calls
        self.max_total_tokens = max_total_tokens
        self.deadline_seconds = deadline_seconds
        self.max_messages = max_messages


def _int_or_zero(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def error(code, message):
    return {"type": "error", "code": code, "message": str(message)[:500]}


def _result(response, model, calls, tokens):
    """Normalise a provider response into the frame the bridge expects.

    A provider (or a compromised endpoint) can answer with anything at all, so the frame is built
    from explicitly checked values: a non-dict body, a missing `content` or a bogus usage block must
    degrade into an empty/absent field rather than an exception in the parent.
    """
    if not isinstance(response, dict):
        response = {}
    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    content = response.get("content")
    return {"type": "chat_result", "content": content if isinstance(content, str) else "",
            "role": response.get("role") if isinstance(response.get("role"), str) else "assistant",
            "usage": usage,
            "model": response.get("model") if isinstance(response.get("model"), str) else model,
            "finish_reason": response.get("finish_reason"),
            "thinking_content": response.get("thinking_content"),
            "calls": calls, "tokens": tokens}


class ModelProxy:
    """Serves model requests for one job over one pipe.

    `call` is injected for tests: `async call(messages, kwargs, model) -> dict` with the same shape
    as the provider response (`content`, `usage`, `model`, ...). The default performs the real call
    with the parent's credentials.
    """

    def __init__(self, pipe, provider, limits=None, call=None, clock=time.monotonic,
                 diagnostics=None, job=None):
        self.pipe = pipe
        self.provider = provider
        self.limits = limits or Limits()
        self.clock = clock
        self.call = call or self._provider_call
        self.diagnostics = diagnostics
        self.job = job
        self.started = None
        self.calls = 0
        self.tokens = 0
        #: Bytes the parent actually forwarded to the provider for dispatched requests. This - not the
        #: caller's read window - is the outbound boundary this layer can measure: a read of 6000 characters
        #: says nothing about how much text the engine then sampled and sent here.
        self.sent_bytes = 0
        self.audit = []
        self.closed = False
        self.cancelled = False
        # Reply cache keyed by the child's request id. The child retries a roundtrip when the pipe
        # breaks; without this, a reply lost on the way back would be paid for twice - and a retry
        # would otherwise let the child exceed the call budget it is supposed to be bounded by.
        self.replies = {}
        self.deduplicated = 0

    # -- limits ---------------------------------------------------------------------------------
    def _remaining_seconds(self):
        if self.started is None:
            return self.limits.deadline_seconds
        return self.limits.deadline_seconds - (self.clock() - self.started)

    def _check(self, request):
        if self.cancelled:
            return error("JOB_CANCELLED", "the job was cancelled; no further model call is started")
        if self.closed:
            return error("JOB_FINISHED", "the job already ended; no further model call is started")
        if not isinstance(request, dict) or request.get("type") != "chat":
            return error("BAD_REQUEST", "expected {type: 'chat', messages: [...]}")
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            return error("BAD_REQUEST", "messages must be a non-empty list")
        if len(messages) > self.limits.max_messages:
            return error("REQUEST_TOO_LARGE", "more than %d messages" % self.limits.max_messages)
        payload = json.dumps(request, ensure_ascii=False)
        if len(payload.encode("utf-8")) > self.limits.max_request_bytes:
            return error("REQUEST_TOO_LARGE", "request exceeds %d bytes" % self.limits.max_request_bytes)
        options = request.get("options") or {}
        if not isinstance(options, dict):
            return error("BAD_REQUEST", "options must be an object")
        forbidden = sorted(set(options) & FORBIDDEN_KEYS)
        if forbidden:
            return error("FORBIDDEN_PARAMETER",
                         "the parent fixes the provider and model; rejected: " + ", ".join(forbidden))
        unknown = sorted(set(options) - ALLOWED_KEYS - FORBIDDEN_KEYS)
        if unknown:
            return error("FORBIDDEN_PARAMETER", "unsupported option(s): " + ", ".join(unknown))
        if self.calls >= self.limits.max_calls:
            return error("CALL_BUDGET_EXHAUSTED", "job already used %d model calls" % self.calls)
        if self.tokens >= self.limits.max_total_tokens:
            return error("TOKEN_BUDGET_EXHAUSTED", "job already used %d tokens" % self.tokens)
        if self._remaining_seconds() <= 0:
            return error("DEADLINE_EXCEEDED", "job model deadline already expired")
        return None

    def _account(self, response):
        """Count a reply. A malformed usage block must never raise: it is untrusted input too."""
        usage = (response or {}).get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            usage = {}
        total = _int_or_zero(usage.get("total_tokens"))
        if total <= 0:
            total = _int_or_zero(usage.get("prompt_tokens")) + _int_or_zero(usage.get("completion_tokens"))
        self.calls += 1
        self.tokens += min(max(0, total), self.limits.max_total_tokens)
        self.audit.append({"call": self.calls, "tokens": total, "remaining": self._remaining_seconds()})

    # -- serving --------------------------------------------------------------------------------
    def _handle(self, request):
        key = request.get("id") if isinstance(request, dict) else None
        if key and key in self.replies:
            # A retry of a roundtrip whose reply was lost: answer again from the cache, pay nothing.
            self.deduplicated += 1
            self.audit.append({"deduplicated": str(key)[:64]})
            return self.replies[key]
        failure = self._check(request)
        if failure:
            self.audit.append({"rejected": failure["code"], "detail": failure["message"][:120]})
            return failure
        if self.started is None:
            self.started = self.clock()
        options = dict(request.get("options") or {})
        remaining = self._remaining_seconds()
        budget_tokens = self.limits.max_total_tokens - self.tokens
        requested = options.get("max_tokens")
        options["max_tokens"] = min(requested, budget_tokens) if isinstance(requested, int) else budget_tokens
        options["timeout"] = min(float(options.get("timeout") or remaining), remaining)
        call_id = str(key or "call-%d" % (self.calls + 1))
        request_bytes = len(json.dumps(request, ensure_ascii=True).encode("utf-8"))
        refusal = reserve_quota()
        if refusal:
            # Checked before the request goes out, and spent at that moment: a refusal here means the
            # window's budget is used up, not that this call failed.
            self.audit.append({"rejected": "QUOTA_EXHAUSTED", "detail": refusal[:120]})
            if self.diagnostics is not None:
                self.diagnostics.record("provider_call_refused", job=self.job, callId=call_id,
                                        reason="quota_exhausted")
            return error("QUOTA_EXHAUSTED", refusal)
        # Counted at the moment of dispatch, not on success: a request that is sent and then times out,
        # fails or is cancelled has already been forwarded, and the outbound total must say so.
        self.sent_bytes += request_bytes
        if self.diagnostics is not None:
            self.diagnostics.record("proxy_request_received", job=self.job, callId=call_id,
                                    requestBytes=request_bytes)
            self.diagnostics.call_started(self.job, call_id, request_bytes=request_bytes,
                                          remaining_ms=int(remaining * 1000))
        try:
            response = _run(self._invoke(request["messages"], options, remaining))
        except asyncio.TimeoutError:
            self.audit.append({"error": "DEADLINE_EXCEEDED"})
            if self.diagnostics is not None:
                self.diagnostics.call_finished(self.job, call_id, error="DEADLINE_EXCEEDED")
            return error("DEADLINE_EXCEEDED", "the model call exceeded the job deadline")
        except Exception as failure_error:  # provider errors must not look like a protocol failure
            self.audit.append({"error": type(failure_error).__name__})
            if self.diagnostics is not None:
                self.diagnostics.call_finished(self.job, call_id, error=type(failure_error).__name__)
            return error("PROVIDER_ERROR", "%s: %s" % (type(failure_error).__name__, failure_error))
        if self.cancelled:
            # The job was cancelled while the provider call was in flight: the tokens are spent
            # anyway, so account for them, but tell the engine not to continue.
            self._account(response)
            if self.diagnostics is not None:
                self.diagnostics.call_finished(self.job, call_id, cancelled=True)
            return error("JOB_CANCELLED", "the job was cancelled during the model call")
        self._account(response)
        if self.diagnostics is not None:
            usage = (response or {}).get("usage") if isinstance(response, dict) else None
            self.diagnostics.call_finished(self.job, call_id, usage=usage,
                                           error="MALFORMED_RESPONSE" if usage is None else None)
        body = _result(response, self.provider["model"], self.calls, self.tokens)
        if key:
            self.replies[key] = body
        return body

    async def _invoke(self, messages, options, remaining):
        """One provider call, bounded by whatever remains of the job deadline."""
        result = self.call(messages, options, self.provider["model"], remaining)
        if asyncio.iscoroutine(result):
            return await asyncio.wait_for(result, remaining)
        return result

    def serve(self, read_timeout=None, connect_timeout=60):
        """Answer requests until the peer disconnects or the job deadline expires.

        The deadline starts when the engine connects, not while it is still starting up.
        """
        if not self.pipe.wait_connected(connect_timeout):
            self.audit.append({"stopped": "no_client"})
            return "no_client"
        self.started = self.clock()
        while not self.closed:
            remaining = self._remaining_seconds()
            if remaining <= 0:
                self.audit.append({"stopped": "DEADLINE_EXCEEDED"})
                return "deadline"
            try:
                request = self.pipe.receive(timeout=min(remaining, read_timeout or remaining))
            except PipeTimeout:
                continue
            except (PipeDisconnected, PipeMessageTooLarge) as failure:
                self.audit.append({"stopped": type(failure).__name__})
                return type(failure).__name__
            if request is None:
                self.audit.append({"stopped": "peer_closed"})
                return "peer_closed"
            try:
                response = self._handle(request)
            except Exception as failure:  # a malformed frame must not take the parent down
                self.audit.append({"error": "INTERNAL_ERROR", "detail": type(failure).__name__})
                response = error("INTERNAL_ERROR", type(failure).__name__)
            try:
                self.pipe.send(response)
            except PipeDisconnected:
                self.audit.append({"stopped": "peer_closed_mid_response"})
                return "peer_closed"

    def in_flight(self):
        """Correlation ids that started and have no terminal event yet (empty when diagnostics are off)."""
        return self.diagnostics.in_flight() if self.diagnostics is not None else []

    def stop(self):
        """The job ended; refuse further requests, but do not call it a cancellation."""
        self.closed = True

    def cancel(self):
        """The job was cancelled or timed out: no further provider call may be started.

        Distinct from `stop()` so the diagnostics can tell a completed job from an aborted one; the
        engine is told JOB_CANCELLED either way it arrives.
        """
        self.cancelled = True
        self.closed = True

    # -- real provider --------------------------------------------------------------------------
    def _provider_call(self, messages, options, model, remaining):
        return self._call_provider(messages, options, model, remaining)

    async def _call_provider(self, messages, options, model, remaining):
        import os
        from sirchmunk.llm import OpenAIChat
        from sirchmunk_compat import close_engine
        from runtime import normalize_proxy_environment
        # The parent may inherit a NO_PROXY list containing "[::1]", which httpx2 cannot parse while
        # building a client - the client constructor then fails before any request is sent. The
        # worker already avoids this; the parent must do the same because it is the process that now
        # talks to the provider.
        os.environ.update(normalize_proxy_environment(os.environ))
        llm = OpenAIChat(base_url=self.provider["base_url"], api_key=self.provider["api_key"], model=model)
        try:
            call = llm.achat(messages=messages, stream=False,
                             enable_thinking=options.get("enable_thinking", False),
                             max_tokens=options.get("max_tokens"), timeout=options.get("timeout"))
            response = await asyncio.wait_for(call, remaining)
            return response.to_dict()
        finally:
            await close_engine(None, llm)


def _run(coroutine):
    """Run a coroutine from synchronous code, even inside a loop-less proxy thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise RuntimeError("the proxy must be driven from a thread without a running event loop")


def provider_from_environment(environment=None, model=None):
    """Build the fixed provider configuration from the parent's environment."""
    import os
    environment = environment or os.environ
    base_url = (environment.get("LLM_BASE_URL") or "").strip()
    api_key = (environment.get("LLM_API_KEY") or "").strip()
    name = (model or environment.get("LLM_MODEL_NAME") or "").strip()
    if not (base_url and api_key and name):
        raise RuntimeError("MODEL_NOT_CONFIGURED: set LLM_BASE_URL, LLM_MODEL_NAME and LLM_API_KEY")
    return {"base_url": base_url, "api_key": api_key, "model": name}


def serve_in_thread(proxy, read_timeout=None):
    """Run `serve()` on a daemon thread; returns (thread, results-holder)."""
    import threading
    outcome = {}

    def run():
        try:
            outcome["stopped"] = proxy.serve(read_timeout)
        except Exception as failure:  # pragma: no cover - defensive
            outcome["error"] = repr(failure)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome
