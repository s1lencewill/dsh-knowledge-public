"""Parent-side stage diagnostics: desensitized, persistent, and able to tell "not started" from "cut".

The job directory is deleted when a job ends, so the only way to answer "was a model call in flight when
the deadline hit?" afterwards is a sink the parent owns. These tests pin what it records, what it must
never record, and that it does not change the cancellation or budget behaviour it observes.
"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import diagnostics
import model_proxy

PROVIDER = {"base_url": "http://127.0.0.1:9/v1", "api_key": "synthetic-key", "model": "fixed-model"}


def stub_result(content="pong", usage=None):
    return {"content": content, "usage": usage if usage is not None else {"total_tokens": 11},
            "model": "fixed-model", "finish_reason": "stop"}


class DiagnosticsSinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "20260101.jsonl"
        self.sink = diagnostics.Diagnostics(self.path)

    def events(self):
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_only_allow_listed_fields_are_written(self):
        self.sink.record("x", job="job-1", status="started",
                         prompt="the whole question", messages=[{"role": "user", "content": "text"}],
                         url="https://example.com/secret", api_key="sk-should-never-appear",
                         notes="some free text that must not be stored")
        line = self.path.read_text(encoding="utf-8")
        for leaked in ("the whole question", "role", "example.com", "sk-should-never-appear",
                       "free text", "content"):
            self.assertNotIn(leaked, line)
        self.assertEqual(list(self.events()[0]), ["event", "job", "monotonicMs", "status"])

    def test_free_text_values_collapse_to_unknown_rather_than_being_truncated_in(self):
        self.sink.record("x", job="job-1", reason="Provider said: the document mentions a specific phrase")
        self.assertEqual(self.events()[0]["reason"], diagnostics.UNKNOWN)

    def test_urls_paths_and_prompts_cannot_hide_in_a_reason(self):
        for value in ("https://example.com/v1/chat/completions", "C:/secret/library/paper.txt",
                      "Summarize the abstract of the attached paper"):
            with self.subTest(value=value):
                self.sink.record("x", job="job-1", reason=value)
                self.assertEqual(self.events()[-1]["reason"], diagnostics.UNKNOWN,
                                 "a value that is not a structured token must not be stored")

    def test_structured_reasons_survive_intact(self):
        self.sink.record("x", job="job-1", reason="workDirRemoved=True")
        self.sink.record("x", job="job-1", reason="in_flight=call-1,call-2")
        self.assertEqual(self.events()[0]["reason"], "workDirRemoved=True")
        self.assertEqual(self.events()[1]["reason"], "in_flight=call-1,call-2")

    def test_fragment_ranking_is_recorded_without_any_text(self):
        self.sink.record("fragment_choice", job="job-1", file="paper.txt", fragments={
            "parts": 2, "budgetChars": 900,
            "runs": [{"startLine": 4, "endLine": 6, "chars": 120, "score": 0.42, "caption": False,
                      "selected": True, "reason": None,
                      "text": "the whole passage", "excerpt": "and its excerpt"},
                     {"startLine": 40, "endLine": 41, "chars": 80, "score": 0.11, "caption": True,
                      "selected": False, "reason": "lower_rank"}]})
        recorded = self.events()[0]["fragments"]
        self.assertEqual(recorded["parts"], 2)
        self.assertEqual((recorded["runs"][0]["startLine"], recorded["runs"][0]["endLine"],
                          recorded["runs"][0]["chars"]), (4, 6, 120))
        self.assertEqual(recorded["runs"][1]["reason"], "lower_rank")
        self.assertNotIn("text", recorded["runs"][0])
        self.assertNotIn("excerpt", recorded["runs"][0])
        self.assertNotIn("the whole passage", self.path.read_text(encoding="utf-8"))

    def test_a_fragment_record_is_bounded_and_free_text_cannot_pass_as_a_reason(self):
        runs = [{"startLine": index, "reason": "a passage lifted out of the document"}
                for index in range(50)]
        self.sink.record("fragment_choice", job="job-1", fragments={"runs": runs})
        recorded = self.events()[0]["fragments"]["runs"]
        self.assertEqual(len(recorded), diagnostics._FRAGMENT_MAX_RUNS, "the record stays bounded")
        self.assertEqual(recorded[0]["reason"], diagnostics.UNKNOWN)

    def test_the_sink_supplies_its_job_id_when_the_caller_omits_it(self):
        sink = diagnostics.Diagnostics(self.path, job="job-9")
        sink.record("x")
        self.assertEqual(self.events()[0]["job"], "job-9")

    def test_unknown_usage_is_marked_unknown_not_zero(self):
        self.sink.record("x", job="job-1", usage=None)
        self.assertEqual(self.events()[0]["usage"], "unknown")
        self.sink.record("x", job="job-1", usage={"prompt_tokens": 4, "completion_tokens": 7})
        self.assertEqual(self.events()[1]["usage"], {"prompt_tokens": 4, "completion_tokens": 7})

    def test_the_sink_survives_the_job_directory(self):
        work = Path(self.temp.name) / "job-1"
        work.mkdir()
        self.sink.stage("snapshot_done", "job-1", files=3)
        import shutil
        shutil.rmtree(work)
        self.assertFalse(work.exists())
        self.assertTrue(self.path.is_file())
        self.assertIn("snapshot_done", [event["event"] for event in self.events()])

    def test_in_flight_tracks_started_calls_without_a_terminal_event(self):
        self.sink.call_started("job-1", "abc", request_bytes=120)
        self.assertEqual(self.sink.in_flight(), ["abc"])
        self.sink.call_finished("job-1", "abc", usage={"total_tokens": 5})
        self.assertEqual(self.sink.in_flight(), [])
        kinds = [event["event"] for event in self.events()]
        self.assertEqual(kinds, ["provider_call_started", "provider_call_completed"])
        self.assertEqual(self.events()[1]["status"], "completed")

    def test_one_request_event_carries_the_mode_and_library(self):
        directory = Path(self.temp.name) / "jobs"
        sink = diagnostics.open_for_job("job-9", directory=directory, mode="ENGINE",
                                        libraryId="library-1")
        sink.stage("snapshot_done", "job-9", files=2)
        events = [json.loads(line) for line in
                  (directory / time.strftime("%Y%m%d")).with_suffix(".jsonl")
                  .read_text(encoding="utf-8").splitlines() if line.strip()]
        received = [event for event in events if event["event"] == "request_received"]
        self.assertEqual(len(received), 1, "one transition, one event")
        self.assertEqual((received[0]["mode"], received[0]["libraryId"]), ("ENGINE", "library-1"))

    def test_a_disabled_sink_writes_nothing(self):
        directory = Path(self.temp.name) / "off"
        sink = diagnostics.open_for_job("job-1", directory=directory, enabled=False)
        sink.stage("snapshot_done", "job-1", files=1)
        sink.call_started("job-1", "abc")
        self.assertEqual(sink.in_flight(), [])
        self.assertEqual(list(directory.glob("*.jsonl")), [])

    def test_an_unwritable_sink_goes_quiet_instead_of_failing_the_job(self):
        blocker = Path(self.temp.name) / "not-a-directory"
        blocker.write_text("file", encoding="utf-8")
        sink = diagnostics.Diagnostics(blocker / "nested" / "20260101.jsonl")
        self.assertIsNone(sink.record("x", job="job-1"))
        self.assertFalse(sink.enabled, "an unusable sink must disable itself, not keep raising")
        self.assertIsNone(sink.record("x", job="job-1"), "and stay quiet for the rest of the job")


class ProxyDiagnosticsTests(unittest.TestCase):
    """The four call states, observed through the real ModelProxy code path with stub providers."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "run.jsonl"

    def proxy(self, call, limits=None):
        self.sink = diagnostics.Diagnostics(self.path)
        return model_proxy.ModelProxy(None, PROVIDER, limits=limits, call=call,
                                      diagnostics=self.sink, job="job-1")

    def events(self):
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def request(self, call_id="cid-1"):
        return {"type": "chat", "id": call_id, "messages": [{"role": "user", "content": "hi"}], "options": {}}

    def test_a_completed_call_records_bytes_elapsed_and_usage(self):
        async def call(messages, options, model, remaining):
            return stub_result(usage={"prompt_tokens": 3, "completion_tokens": 8, "total_tokens": 11})

        proxy = self.proxy(call)
        proxy._handle(self.request())
        kinds = [event["event"] for event in self.events()]
        self.assertEqual(kinds, ["proxy_request_received", "provider_call_started", "provider_call_completed"])
        started = self.events()[1]
        self.assertGreater(started["requestBytes"], 0)
        self.assertEqual(self.events()[2]["usage"], {"prompt_tokens": 3, "completion_tokens": 8, "total_tokens": 11})
        self.assertNotIn("messages", self.path.read_text(encoding="utf-8"))

    def test_a_failing_call_is_named_by_class_only(self):
        async def call(messages, options, model, remaining):
            raise ConnectionResetError("reset while reading a specific response body")

        proxy = self.proxy(call)
        frame = proxy._handle(self.request())
        self.assertEqual(frame["code"], "PROVIDER_ERROR")
        terminal = self.events()[-1]
        self.assertEqual((terminal["event"], terminal["status"]), ("provider_call_failed", "failed"))
        self.assertEqual(terminal["reason"], "ConnectionResetError")
        self.assertNotIn("specific response body", self.path.read_text(encoding="utf-8"))

    def test_a_call_cut_by_the_deadline_is_started_without_a_completion(self):
        entered = threading.Event()

        async def call(messages, options, model, remaining):
            entered.set()
            import asyncio
            await asyncio.sleep(5)
            return stub_result()

        proxy = self.proxy(call, model_proxy.Limits(deadline_seconds=1.0))
        observed = {}

        def run():
            observed["frame"] = proxy._handle(self.request())

        thread = threading.Thread(target=run)
        thread.start()
        # Wait for the provider call to be entered rather than for a wall-clock guess: `call_started`
        # is recorded before the call is invoked, so once the stub runs, the marker must be there.
        self.assertTrue(entered.wait(10), "the provider call was never entered")
        observed["in_flight"] = proxy.in_flight()
        thread.join(timeout=20)
        self.assertEqual(observed["in_flight"], ["cid-1"])
        self.assertEqual(observed["frame"]["code"], "DEADLINE_EXCEEDED")
        self.assertEqual(proxy.in_flight(), [], "the terminal event must clear the in-flight marker")
        self.assertEqual([event["event"] for event in self.events()][-1], "provider_call_failed")

    def test_no_call_started_is_distinguishable_from_a_call_in_flight(self):
        # A proxy that never received a request has nothing in flight; that is the "not started yet" case.
        async def call(messages, options, model, remaining):
            return stub_result()

        proxy = self.proxy(call)
        self.assertEqual(proxy.in_flight(), [])
        self.assertFalse(self.path.is_file(), "no events at all when nothing happened")

    def test_cancellation_still_refuses_and_is_recorded_as_cancelled(self):
        entered = threading.Event()

        async def call(messages, options, model, remaining):
            entered.set()
            import asyncio
            await asyncio.sleep(0.6)
            return stub_result()

        proxy = self.proxy(call)
        frames = {}

        def run():
            frames["frame"] = proxy._handle(self.request())

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(10), "the provider call was never entered")
        proxy.cancel()
        thread.join(timeout=20)
        self.assertEqual(frames["frame"]["code"], "JOB_CANCELLED", "cancellation behaviour is unchanged")
        terminal = self.events()[-1]
        self.assertEqual((terminal["event"], terminal["status"]), ("provider_call_cancelled", "cancelled"))
        self.assertEqual(terminal["usage"], "unknown", "a cancelled call has no reliable usage")
        self.assertEqual(proxy.calls, 1, "the in-flight call is still accounted exactly once")

    def test_a_call_after_cancellation_starts_nothing_new(self):
        async def call(messages, options, model, remaining):
            return stub_result()

        proxy = self.proxy(call)
        proxy.cancel()
        frame = proxy._handle(self.request("cid-2"))
        self.assertEqual(frame["code"], "JOB_CANCELLED")
        self.assertEqual([event["event"] for event in self.events()], [],
                         "a refused request never reaches the provider, so it is not a started call")

    def test_an_unusable_sink_does_not_change_the_call_outcome(self):
        async def call(messages, options, model, remaining):
            return stub_result()

        blocker = Path(self.temp.name) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        sink = diagnostics.Diagnostics(blocker / "nested.jsonl")
        proxy = model_proxy.ModelProxy(None, PROVIDER, call=call, diagnostics=sink, job="job-1")
        frame = proxy._handle(self.request())
        self.assertEqual(frame["type"], "chat_result", "diagnostics must not be able to fail a call")
        self.assertEqual((proxy.calls, proxy.tokens), (1, 11), "budget accounting is unchanged")


if __name__ == "__main__":
    unittest.main()
