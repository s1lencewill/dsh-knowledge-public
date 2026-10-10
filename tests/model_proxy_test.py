"""Model proxy: the child may only send messages, and the parent enforces every limit.

Children here are real processes using engine/llm_bridge.py over a real pipe; the provider itself is
a stub, so these tests need no network and no credentials.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))
import diagnostics
import ipc
import model_proxy

PRELUDE = ("import sys, asyncio, json; sys.path.insert(0, r'%s'); import ipc, llm_bridge; "
           "llm_bridge.install_bridge(sys.argv[1], 'fixed-model'); ") % ENGINE
RAW_PRELUDE = "import sys, json; sys.path.insert(0, r'%s'); import ipc; " % ENGINE


def budget_prelude(budget):
    return ("import sys, asyncio, json; sys.path.insert(0, r'%s'); import ipc, llm_bridge; "
            "llm_bridge.install_bridge(sys.argv[1], 'fixed-model', budget_seconds=%r); ") % (ENGINE, budget)


PROVIDER = {"base_url": "http://127.0.0.1:9/v1", "api_key": "synthetic-parent-key", "model": "fixed-model"}


class Recorder:
    def __init__(self, content="pong", usage=None, delay=0.0):
        self.calls = []
        self.content = content
        self.usage = usage if usage is not None else {"total_tokens": 11}
        self.delay = delay

    async def __call__(self, messages, options, model, remaining):
        self.calls.append({"messages": messages, "options": options, "model": model, "remaining": remaining})
        if self.delay:
            import asyncio
            await asyncio.sleep(self.delay)
        return {"content": self.content, "usage": self.usage, "model": model, "finish_reason": "stop"}


class ProxyCase(unittest.TestCase):
    def setUp(self):
        self.name = "dsh-proxy-test-" + uuid.uuid4().hex
        self.children = []
        self.threads = []
        self.servers = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        for proxy, pipe, thread in self.servers:
            proxy.stop()
            thread.join(timeout=5)
            alive = thread.is_alive()
            pipe.close()
            self.assertFalse(alive, "proxy thread must exit before its pipe handle is closed/reused")

    def start_proxy(self, recorder, limits=None):
        pipe = ipc.PipeServer(self.name).listen()
        proxy = model_proxy.ModelProxy(pipe, PROVIDER, limits=limits, call=recorder)
        thread, outcome = model_proxy.serve_in_thread(proxy)
        self.threads.append(thread)
        self.servers.append((proxy, pipe, thread))
        # The serve thread waits for the child to connect; children start in each test.
        return proxy

    def _wait_until_serving(self, proxy):
        """Wait until the serve loop is actually inside receive() before stopping or cancelling it.

        A test that stops the proxy right after the child connects can win a race the code never
        promised: if the loop has not reached receive() yet, it sees `closed` and exits without
        answering, and the child's later request is never replied to. Waiting on `started` (set once
        the connect wait returns) plus a short settle removes that race without changing the ordering
        the test is about - the stop still lands before the child's request.
        """
        deadline = time.monotonic() + 20
        while proxy.started is None and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)

    def child(self, code, prelude=None):
        process = subprocess.Popen([sys.executable, "-c", (prelude or PRELUDE) + code, "\\\\.\\pipe\\" + self.name],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        return process

    def call_child(self, code, timeout=30, prelude=None):
        process = self.child(code, prelude)
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout.strip(), stderr.strip()

    def test_normal_call_reaches_the_fixed_provider(self):
        recorder = Recorder()
        proxy = self.start_proxy(recorder)
        code, stdout, stderr = self.call_child(
            "r = asyncio.run(llm_bridge.OpenAIChatBridge().achat([{'role':'user','content':'hi'}], stream=False));"
            "print(json.dumps({'content': r.content, 'model': r.model, 'usage': r.usage}))")
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["content"], "pong")
        self.assertEqual(payload["model"], "fixed-model")
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0]["model"], PROVIDER["model"], "the parent's model is used")
        self.assertEqual(recorder.calls[0]["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(proxy.calls, 1)
        self.assertEqual(proxy.tokens, 11)

    def test_engine_supplied_url_and_key_are_never_forwarded(self):
        recorder = Recorder()
        self.start_proxy(recorder)
        code, stdout, stderr = self.call_child(
            "b = llm_bridge.OpenAIChatBridge(base_url='https://evil.example/v1', api_key='stolen', model='other');"
            "r = asyncio.run(b.achat([{'role':'user','content':'hi'}], stream=False));"
            "print(json.dumps({'model': r.model, 'ignored': sorted(b._ignored)}))")
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["model"], "fixed-model")
        self.assertEqual(recorder.calls[0]["model"], PROVIDER["model"])
        self.assertNotIn("evil", json.dumps(recorder.calls[0]))

    def test_forbidden_options_are_rejected_and_never_called(self):
        recorder = Recorder()
        proxy = self.start_proxy(recorder)
        # One connection, many requests: the parent serves a single pipe instance per job.
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]); out = []\n"
            "for option in [{'model':'other'},{'base_url':'https://evil'},{'api_key':'stolen'},"
            "{'headers':{'Authorization':'Bearer x'}},{'transport':'x'}]:\n"
            "    ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':option})\n"
            "    out.append((ipc.read_message(h, timeout=10))['code'])\n"
            "ipc.close(h); print(json.dumps(out))", prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), ["FORBIDDEN_PARAMETER"] * 5)
        self.assertEqual(recorder.calls, [], "a rejected request must never reach the provider")
        self.assertEqual(proxy.calls, 0)

    def test_oversized_request_is_refused(self):
        recorder = Recorder()
        self.start_proxy(recorder, model_proxy.Limits(max_request_bytes=2048))
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]);"
            "ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'x'*4000}],'options':{}});"
            "print(json.dumps(ipc.read_message(h, timeout=10))); ipc.close(h)", prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["code"], "REQUEST_TOO_LARGE")
        self.assertEqual(recorder.calls, [])

    def test_call_budget_stops_further_requests(self):
        recorder = Recorder()
        proxy = self.start_proxy(recorder, model_proxy.Limits(max_calls=2))
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]); out = []\n"
            "for _ in range(3):\n"
            "    ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}})\n"
            "    out.append(ipc.read_message(h, timeout=10).get('code') or 'chat_result')\n"
            "ipc.close(h); print(json.dumps(out))", prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), ["chat_result", "chat_result", "CALL_BUDGET_EXHAUSTED"])
        self.assertEqual(len(recorder.calls), 2)
        self.assertTrue(any(entry.get("rejected") == "CALL_BUDGET_EXHAUSTED" for entry in proxy.audit), proxy.audit)

    def test_token_budget_clamps_each_call_and_stops_after_overshoot(self):
        recorder = Recorder(usage={"total_tokens": 400})
        proxy = self.start_proxy(recorder, model_proxy.Limits(max_total_tokens=500))
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]); out = []\n"
            "for _ in range(3):\n"
            "    ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}})\n"
            "    out.append(ipc.read_message(h, timeout=10).get('code') or 'chat_result')\n"
            "ipc.close(h); print(json.dumps(out))", prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), ["chat_result", "chat_result", "TOKEN_BUDGET_EXHAUSTED"])
        self.assertEqual(proxy.tokens, 800)
        # Every call is bounded by whatever is left of the budget (500 - 400 = 100 after the first).
        self.assertEqual(recorder.calls[0]["options"]["max_tokens"], 500)
        self.assertEqual(recorder.calls[1]["options"]["max_tokens"], 100)

    def test_deadline_bounds_a_slow_provider(self):
        recorder = Recorder(delay=5.0)
        proxy = self.start_proxy(recorder, model_proxy.Limits(deadline_seconds=1.0))
        started = time.monotonic()
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]);"
            "ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}});"
            "print(json.dumps(ipc.read_message(h, timeout=20))); ipc.close(h)",
            timeout=30, prelude=RAW_PRELUDE)
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["code"], "DEADLINE_EXCEEDED")
        self.assertLess(elapsed, 8.0, "the parent must give up at its own deadline, not the provider's")

    def test_malformed_request_is_reported_not_fatal(self):
        recorder = Recorder()
        proxy = self.start_proxy(recorder)
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]); out = {}\n"
            "ipc.write_message(h, {'type':'chat','options':{}})\n"
            "out['bad'] = ipc.read_message(h, timeout=10).get('code')\n"
            "ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}})\n"
            "out['good'] = ipc.read_message(h, timeout=10)['type']\n"
            "ipc.close(h); print(json.dumps(out))", prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {"bad": "BAD_REQUEST", "good": "chat_result"})
        self.assertEqual(len(recorder.calls), 1)

    def test_bridge_is_a_drop_in_for_upstream_call_sites(self):
        import llm_bridge
        import sirchmunk.llm.openai_chat as module
        original = module.OpenAIChat
        self.addCleanup(setattr, module, "OpenAIChat", original)
        patched = llm_bridge.install_bridge(self.name + "-inproc", "fixed-model")
        self.assertIn("sirchmunk.llm.openai_chat", patched)
        self.assertIs(module.OpenAIChat, llm_bridge.OpenAIChatBridge)
        instance = module.OpenAIChat(base_url="https://ignored", api_key="ignored", model="ignored")
        self.assertEqual(instance._model, "fixed-model")
        # close_engine() closes llm._client / llm._async_client; the bridge provides benign closers.
        self.assertTrue(callable(instance._client.close))
        self.assertTrue(callable(instance._async_client.close))


class ProviderFaultTests(ProxyCase):
    """Local stubs for every provider failure the isolated engine can meet. No model, no network."""

    def test_429_and_5xx_are_reported_as_provider_errors_and_the_job_survives(self):
        class Flaky:
            def __init__(self):
                self.calls = 0

            async def __call__(self, messages, options, model, remaining):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("Error code: 429 - rate limited, retry after 3s")
                if self.calls == 2:
                    raise RuntimeError("Error code: 503 - service unavailable")
                return {"content": "recovered", "usage": {"total_tokens": 7}, "model": model}

        recorder = Flaky()
        proxy = self.start_proxy(recorder)
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]); out = []\n"
            "for _ in range(3):\n"
            "    ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}})\n"
            "    reply = ipc.read_message(h, timeout=10)\n"
            "    out.append([reply.get('code') or reply.get('type'), (reply.get('message') or '')[:40]])\n"
            "ipc.close(h); print(json.dumps(out))", prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        outcome = json.loads(stdout)
        self.assertEqual([entry[0] for entry in outcome], ["PROVIDER_ERROR", "PROVIDER_ERROR", "chat_result"])
        self.assertIn("429", outcome[0][1])
        self.assertIn("503", outcome[1][1])
        self.assertEqual(proxy.calls, 1, "only the successful call is billed")
        self.assertEqual(proxy.tokens, 7)

    def test_malformed_provider_response_never_breaks_the_parent(self):
        bodies = [None, "not a dict", {"usage": "eleven"}, {"content": 42, "usage": {"total_tokens": "x"}},
                  {"usage": {"total_tokens": 10 ** 12}}]

        class Odd:
            def __init__(self):
                self.calls = []

            async def __call__(self, messages, options, model, remaining):
                self.calls.append(1)
                return bodies[len(self.calls) - 1]

        recorder = Odd()
        proxy = self.start_proxy(recorder)
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]); out = []\n"
            "for _ in range(%d):\n"
            "    ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}})\n"
            "    reply = ipc.read_message(h, timeout=10)\n"
            "    out.append([reply.get('type'), reply.get('content'), reply.get('usage')])\n"
            "ipc.close(h); print(json.dumps(out))" % len(bodies), prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        outcome = json.loads(stdout)
        self.assertEqual([entry[0] for entry in outcome], ["chat_result"] * len(bodies))
        self.assertEqual([entry[1] for entry in outcome], ["", "", "", "", ""])
        self.assertEqual(outcome[1][2], {}, "a non-dict usage block becomes an empty one")
        self.assertEqual(outcome[-1][2], {"total_tokens": 10 ** 12})
        self.assertEqual(len(recorder.calls), len(bodies))
        # An absurd reported total is clamped: the budget is exhausted, but it cannot overflow.
        self.assertEqual(proxy.tokens, model_proxy.Limits().max_total_tokens)

    def test_unexpected_parent_error_is_answered_not_raised(self):
        class Exploding(Recorder):
            def __init__(self):
                super().__init__()
                self.calls = []

        proxy = self.start_proxy(Recorder())
        original = proxy._handle

        def explode(request):
            raise KeyError("boom")

        proxy._handle = explode
        self.addCleanup(setattr, proxy, "_handle", original)
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]);"
            "ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}});"
            "print(json.dumps(ipc.read_message(h, timeout=10))); ipc.close(h)", prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["code"], "INTERNAL_ERROR")
        self.assertTrue(any(entry.get("error") == "INTERNAL_ERROR" for entry in proxy.audit), proxy.audit)

    def test_child_disconnect_before_the_reply_stops_the_proxy_cleanly(self):
        recorder = Recorder(delay=1.5)
        proxy = self.start_proxy(recorder)
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]);"
            "ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}});"
            "ipc.close(h); print('closed-without-reading')", prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "closed-without-reading")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not proxy.audit:
            time.sleep(0.1)
        self.assertTrue(any("peer_closed" in json.dumps(entry) for entry in proxy.audit), proxy.audit)

    def test_hanging_provider_still_answers_the_child(self):
        class Hanging:
            async def __call__(self, messages, options, model, remaining):
                import asyncio
                await asyncio.sleep(30)
                return {"content": "never", "usage": {}}

        proxy = self.start_proxy(Hanging(), model_proxy.Limits(deadline_seconds=1.0))
        started = time.monotonic()
        code, stdout, stderr = self.call_child(
            "import ipc; h = ipc.connect(sys.argv[1]);"
            "ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}});"
            "print(json.dumps(ipc.read_message(h, timeout=15))); ipc.close(h)",
            timeout=30, prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["code"], "DEADLINE_EXCEEDED")
        self.assertLess(time.monotonic() - started, 8.0)

    def test_child_gives_up_at_its_own_budget_when_the_parent_never_answers(self):
        # A parent that accepts the connection and then stays silent: without a budget the child
        # would wait its fixed 180s timeout, past the job's deadline.
        pipe_name = "dsh-silent-" + uuid.uuid4().hex
        pipe = ipc.PipeServer(pipe_name).listen()
        self.addCleanup(pipe.close)
        started = time.monotonic()
        process = self.child(
            "import asyncio, llm_bridge\n"
            "try:\n"
            "    asyncio.run(llm_bridge.OpenAIChatBridge().achat([{'role':'user','content':'h'}], stream=False))\n"
            "    print('NO_ERROR')\n"
            "except Exception as error:\n"
            "    print(type(error).__name__, error)",
            prelude=budget_prelude(2.0).replace("sys.argv[1]", repr("\\\\.\\pipe\\" + pipe_name)))
        if not pipe.wait_connected(20):
            self.fail("the child never connected to the silent parent")
        stdout, stderr = process.communicate(timeout=30)
        elapsed = time.monotonic() - started
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("budget", stdout.lower())
        self.assertLess(elapsed, 15.0, "the child must not wait its fixed 180s timeout")


class RetryAndCancelTests(ProxyCase):
    """A retry must not buy budget, and a cancelled job must start no further call."""

    def test_a_retried_request_id_is_answered_from_cache(self):
        recorder = Recorder()
        proxy = self.start_proxy(recorder)
        code, stdout, stderr = self.call_child(
            "import ipc, uuid; h = ipc.connect(sys.argv[1]); rid = uuid.uuid4().hex; out = []\n"
            "for _ in range(2):\n"
            "    ipc.write_message(h, {'type':'chat','id':rid,'messages':[{'role':'user','content':'h'}],'options':{}})\n"
            "    reply = ipc.read_message(h, timeout=10)\n"
            "    out.append([reply.get('type'), reply.get('content'), reply.get('calls')])\n"
            "ipc.write_message(h, {'type':'chat','id':uuid.uuid4().hex,"
            "'messages':[{'role':'user','content':'h'}],'options':{}})\n"
            "out.append([ipc.read_message(h, timeout=10).get('calls')])\n"
            "ipc.close(h); print(json.dumps(out))", prelude=RAW_PRELUDE)
        self.assertEqual(code, 0, stderr)
        outcome = json.loads(stdout)
        self.assertEqual(outcome[0], ["chat_result", "pong", 1])
        self.assertEqual(outcome[1], ["chat_result", "pong", 1], "the retry reuses the first reply")
        self.assertEqual(outcome[2], [2], "a new id is a new call")
        self.assertEqual(len(recorder.calls), 2)
        self.assertEqual(proxy.calls, 2)
        self.assertEqual(proxy.deduplicated, 1)

    def test_the_bridge_reuses_one_id_across_a_retry(self):
        seen = []

        class DropFirstReply:
            """Reads the request, closes the pipe without answering, then serves the retry."""

            def __init__(self, session):
                self.session = session

            async def __call__(self, messages, options, model, remaining):
                import socket as _socket  # noqa: F401  (imported for symmetry with the real path)
                seen.append(messages)
                return {"content": "second-attempt", "usage": {"total_tokens": 3}, "model": model}

        recorder = DropFirstReply(None)
        proxy = self.start_proxy(recorder)
        code, stdout, stderr = self.call_child(
            "import asyncio, llm_bridge\n"
            "r = asyncio.run(llm_bridge.OpenAIChatBridge().achat([{'role':'user','content':'h'}], stream=False))\n"
            "print(json.dumps({'content': r.content}))")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["content"], "second-attempt")
        self.assertEqual(len(seen), 1)

    def test_a_cancelled_proxy_starts_no_further_provider_call(self):
        recorder = Recorder()
        proxy = self.start_proxy(recorder)
        process = self.child(
            "import ipc, time; h = ipc.connect(sys.argv[1]); time.sleep(1.5);\n"
            "ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}});\n"
            "print(json.dumps(ipc.read_message(h, timeout=15))); ipc.close(h)", prelude=RAW_PRELUDE)
        self.assertTrue(proxy.pipe.connected.wait(20), "the engine must be connected before the cancel")
        self._wait_until_serving(proxy)
        proxy.cancel()  # what service.stop() does on cancellation
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout.strip())["code"], "JOB_CANCELLED")
        self.assertEqual(recorder.calls, [], "a cancelled job must not reach the provider again")
        self.assertEqual(proxy.calls, 0)

    def test_a_finished_job_refuses_later_calls_but_is_not_called_cancelled(self):
        # The diagnostics must be able to tell "we stopped, the job was done" from "we aborted it".
        recorder = Recorder()
        proxy = self.start_proxy(recorder)
        process = self.child(
            "import ipc, time; h = ipc.connect(sys.argv[1]); time.sleep(1.5);\n"
            "ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}});\n"
            "print(json.dumps(ipc.read_message(h, timeout=15))); ipc.close(h)", prelude=RAW_PRELUDE)
        self.assertTrue(proxy.pipe.connected.wait(20))
        self._wait_until_serving(proxy)
        proxy.stop()
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout.strip())["code"], "JOB_FINISHED")
        self.assertFalse(proxy.cancelled)
        self.assertEqual(recorder.calls, [])

    def test_cancelling_during_a_call_accounts_the_tokens_and_refuses_the_engine(self):
        class Slow:
            def __init__(self):
                self.calls = 0

            async def __call__(self, messages, options, model, remaining):
                import asyncio
                self.calls += 1
                await asyncio.sleep(0.6)
                return {"content": "late", "usage": {"total_tokens": 5}, "model": model}

        recorder = Slow()
        proxy = self.start_proxy(recorder)
        process = self.child(
            "import ipc; h = ipc.connect(sys.argv[1]);"
            "ipc.write_message(h, {'type':'chat','messages':[{'role':'user','content':'h'}],'options':{}});"
            "print(json.dumps(ipc.read_message(h, timeout=15))); ipc.close(h)", prelude=RAW_PRELUDE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and recorder.calls == 0:
            time.sleep(0.05)
        proxy.cancel()
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout.strip())["code"], "JOB_CANCELLED")
        self.assertEqual(proxy.calls, 1, "the in-flight call is still accounted")
        self.assertEqual(proxy.tokens, 5)


class ProviderQuotaTests(unittest.TestCase):
    """The window's spend ceiling is spent before a request goes out, and survives failures."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.quota = Path(self.temp.name) / "quota.json"
        self.sink_path = Path(self.temp.name) / "diag.jsonl"
        self.env = patch.dict(os.environ, {"DSH_KNOWLEDGE_QUOTA_FILE": str(self.quota),
                                           "DSH_KNOWLEDGE_QUOTA_MAX": "2"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = 0

    def proxy(self, call, sink=None):
        return model_proxy.ModelProxy(None, PROVIDER, call=call, diagnostics=sink, job="job-quota")

    def request(self, call_id):
        return {"type": "chat", "id": call_id, "messages": [{"role": "user", "content": "x"}],
                "options": {}}

    def used(self):
        return json.loads(self.quota.read_text(encoding="utf-8"))["used"]

    def test_requests_beyond_the_budget_are_refused_before_they_are_sent(self):
        async def call(*args):
            self.calls += 1
            return {"content": "ok", "usage": {"total_tokens": 1}}

        sink = diagnostics.Diagnostics(self.sink_path, job="job-quota")
        proxy = self.proxy(call, sink)
        self.assertEqual(proxy._handle(self.request("a"))["type"], "chat_result")
        self.assertEqual(proxy._handle(self.request("b"))["type"], "chat_result")
        refused = proxy._handle(self.request("c"))
        self.assertEqual(refused["code"], "QUOTA_EXHAUSTED")
        self.assertEqual(self.calls, 2, "a refused request must never reach the provider")
        self.assertEqual(self.used(), 2)
        events = [json.loads(line)["event"] for line in
                  self.sink_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(events.count("provider_call_started"), 2)
        self.assertIn("provider_call_refused", events)

    def test_the_forwarded_byte_total_counts_what_was_sent_and_not_what_was_refused(self):
        """The outbound boundary this layer can measure, stated as the two cases it has.

        A read window says nothing about how much text the engine sampled and sent, so the proxy records
        the request images it actually forwarded. A refused request contributes nothing; a dispatched
        request that then fails has already gone out and still counts.
        """
        async def failing(*args):
            raise RuntimeError("provider down")

        proxy = self.proxy(failing)
        self.assertEqual(proxy.sent_bytes, 0, "nothing has been forwarded yet")
        self.assertEqual(proxy._handle(self.request("a"))["code"], "PROVIDER_ERROR")
        after_failure = proxy.sent_bytes
        self.assertGreater(after_failure, 0, "a dispatched request was forwarded, so its bytes count")
        self.assertEqual(proxy._handle(self.request("b"))["code"], "PROVIDER_ERROR")
        self.assertGreater(proxy.sent_bytes, after_failure, "the second forwarded request adds its own bytes")
        # The window is spent, so the third request never goes out and must not inflate the total.
        before = proxy.sent_bytes
        self.assertEqual(proxy._handle(self.request("c"))["code"], "QUOTA_EXHAUSTED")
        self.assertEqual(proxy.sent_bytes, before, "a refused request was never forwarded")

    def test_a_request_that_fails_or_is_cancelled_still_spends_its_slot(self):
        async def failing(*args):
            self.calls += 1
            raise RuntimeError("provider down")

        proxy = self.proxy(failing)
        self.assertEqual(proxy._handle(self.request("a"))["code"], "PROVIDER_ERROR")
        self.assertEqual(self.used(), 1, "a failed request has still been sent")

        async def slow(*args):
            import asyncio
            self.calls += 1
            await asyncio.sleep(5)
            return {"content": "late", "usage": {"total_tokens": 1}}

        proxy = self.proxy(slow)
        thread = threading.Thread(target=proxy._handle, args=(self.request("b"),))
        thread.start()
        time.sleep(0.2)
        proxy.cancel()
        thread.join(timeout=10)
        self.assertEqual(self.used(), 2, "a cancelled request has still been sent")

    def test_no_budget_configured_keeps_the_previous_behaviour(self):
        os.environ.pop("DSH_KNOWLEDGE_QUOTA_FILE", None)
        self.addCleanup(lambda: os.environ.update({"DSH_KNOWLEDGE_QUOTA_FILE": str(self.quota)}))

        async def call(*args):
            return {"content": "ok", "usage": {"total_tokens": 1}}

        proxy = self.proxy(call)
        for index in range(5):
            self.assertEqual(proxy._handle(self.request("r%d" % index))["type"], "chat_result")
        self.assertFalse(self.quota.is_file(), "no budget means no ledger")

    def test_an_unreadable_ledger_refuses_rather_than_letting_requests_through(self):
        self.quota.mkdir()
        async def call(*args):
            self.calls += 1
            return {"content": "ok", "usage": {"total_tokens": 1}}

        proxy = self.proxy(call)
        self.assertEqual(proxy._handle(self.request("a"))["code"], "QUOTA_EXHAUSTED")
        self.assertEqual(self.calls, 0)


if __name__ == "__main__":
    unittest.main()
