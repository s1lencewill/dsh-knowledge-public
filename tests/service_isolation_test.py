"""Service integration for the isolated path: hard failures, cancellation, timeouts, cleanup.

The container itself is faked here so the state machine is deterministic; probes exercise the real
container (scripts/probe-isolated-*.py).
"""
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
os.environ.setdefault("DSH_KNOWLEDGE_TOKEN", "synthetic-token-for-isolation-test")
import appcontainer
import service


class FakeProxy:
    """Stands in for model_proxy.ModelProxy when a test controls the in-flight call state.

    `mode` is the knob: "in-flight" leaves one call started with no terminal event (what a job-level
    deadline interrupts), "idle" starts nothing (what "the model was never called" looks like).
    """

    mode = "in-flight"
    instances = []

    def __init__(self, pipe, provider, limits=None, diagnostics=None, job=None):
        self.diagnostics = diagnostics
        self.job = job
        self.calls = 0
        self.tokens = 0
        # The outbound total the service reports per job. The stand-in has a started call (mode
        # "in-flight"), so it forwards what such a call would have: the fake is the interface, and an
        # interface a fake omits is one the real code would crash on.
        self.sent_bytes = 64 if FakeProxy.mode == "in-flight" else 0
        self.deduplicated = 0
        self.cancelled = False
        FakeProxy.instances.append(self)
        if FakeProxy.mode == "in-flight" and diagnostics is not None:
            diagnostics.call_started(job, "call-1", request_bytes=64, remaining_ms=1000)

    def in_flight(self):
        return self.diagnostics.in_flight() if self.diagnostics is not None else []

    def cancel(self):
        self.cancelled = True

    def stop(self):
        pass


class FakeContainer:
    """Stands in for appcontainer.ContainerJob."""

    instances = []

    def __init__(self, behavior="ok", payload=None):
        self.behavior = behavior
        self.payload = payload
        self.terminated = False
        self.closed = False
        self.state_dir = None
        #: Mirrors `ContainerJob.exit_code`: set by `wait()` when the process is observed to end, and read by
        #: the service to tell "exited non-zero without a result" from "ended cleanly with nothing to report".
        self.exit_code = None
        FakeContainer.instances.append(self)

    def start(self, sid, argv, environment, cwd=None):
        config = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        self.state_dir = Path(config["stateDir"])
        self.request = config["request"]
        if self.behavior == "raise":
            # Mirror ContainerJob.start(), which closes everything before re-raising.
            self.close()
            raise appcontainer.IsolationError("fake launch failure")
        return self

    def wait(self, timeout_ms):
        self.timeout_ms = timeout_ms
        if self.behavior == "timeout":
            raise TimeoutError("fake budget exceeded")
        if self.behavior == "timeout-with-result":
            # The race: the engine wrote its result just as the deadline fired.
            (self.state_dir / "result.json").write_text(json.dumps(self.payload), encoding="utf-8")
            raise TimeoutError("fake budget exceeded at the same moment the result landed")
        if self.behavior == "hang":
            import time
            time.sleep(3)
        if self.behavior == "result-then-hang":
            # The result exists on disk while the job is still running: the window a cancel must win.
            (self.state_dir / "result.json").write_text(json.dumps(self.payload), encoding="utf-8")
            import time
            time.sleep(3)
            return 0
        if self.payload is not None:
            (self.state_dir / "result.json").write_text(json.dumps(self.payload), encoding="utf-8")
        # A "crash" behaviour: the container is observed to exit non-zero and writes no result. This is the
        # shape the recorded 75.89s job had (exitCode=1, no result.json) and the shape that used to be
        # reported as ISOLATION_UNAVAILABLE.
        self.exit_code = 1 if self.behavior == "crash" else 0
        return self.exit_code

    def terminate(self):
        self.terminated = True

    def close(self):
        self.closed = True


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeContainer.instances = []
        FakeProxy.instances = []
        FakeProxy.mode = "in-flight"
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "library"
        self.root.mkdir()
        (self.root / "note-03.md").write_text("# synthetic\nWALNUT-03\n", encoding="utf-8")
        self.cache = Path(self.temp.name) / "cache"
        self.addCleanup(self._stop)

    def _stop(self):
        pass

    def request(self, mode="FILENAME_ONLY"):
        return {"mode": mode, "query": "note-03", "root": str(self.root), "libraryId": "fixtures",
                "cacheRoot": str(self.cache)}

    def isolated(self, container, sid="S-1-15-2-999"):
        # The env -> mode gate is covered by ServiceTests; here the isolated branch is selected
        # directly so these tests do not depend on the ambient variable.
        return patch.multiple(service, isolation_required=lambda: True,
                              ISOLATION_STATE={"sid": "fake-sid", "sidString": sid}), \
            patch.object(service.appcontainer, "ContainerJob", return_value=container), \
            patch.object(service, "_grant_job_paths", lambda *args: {"lowIntegrity": False}), \
            patch.object(service.appcontainer, "container_environment", lambda *args, **kwargs: {})

    async def test_isolation_failure_is_a_hard_error_without_a_plain_spawn(self):
        job = {"id": "t1", "result": None}
        state, jobs, grants, env = self.isolated(FakeContainer(behavior="raise"))
        with state, jobs, grants, env, \
             patch.object(service.asyncio, "create_subprocess_exec") as spawn:
            await service.execute(job, self.request())
        self.assertEqual(job["result"]["status"], "failed")
        self.assertIn("ISOLATION_UNAVAILABLE", job["result"]["warnings"][0])
        self.assertFalse(spawn.called, "a failed isolation setup must never fall back to a plain worker")
        self.assertTrue(FakeContainer.instances[-1].closed)

    async def test_a_container_that_produced_no_result_is_a_worker_failure(self):
        # A container that started, ran and wrote no result is a *worker* outcome, not an isolation one. The two
        # used to collapse into one ISOLATION_UNAVAILABLE warning, which is how an observed 75-second job with
        # four provider calls came back described as an isolation problem. The isolation code stays for the
        # setup failing, which is the test above.
        job = {"id": "t2", "result": None}
        state, jobs, grants, env = self.isolated(FakeContainer(behavior="ok", payload=None))
        with state, jobs, grants, env:
            await service.execute(job, self.request())
        self.assertEqual(job["result"]["status"], "failed")
        self.assertIn("WORKER_NO_RESULT", job["result"]["warnings"][0])
        self.assertNotIn("ISOLATION_UNAVAILABLE", job["result"]["warnings"][0])
        self.assertEqual(job["failureCode"], "WORKER_NO_RESULT")

    async def test_a_container_that_exited_non_zero_is_reported_as_an_exit_not_as_a_isolation_failure(self):
        # The recorded job's shape: the container ran, made provider calls and exited non-zero without writing a
        # result. It is named by its exit code and kept apart from the isolation code, so "the worker failed"
        # stops reading as "the engine could not be isolated".
        job = {"id": "t3", "result": None}
        state, jobs, grants, env = self.isolated(FakeContainer(behavior="crash"))
        with state, jobs, grants, env:
            await service.execute(job, self.request())
        self.assertEqual(job["result"]["status"], "failed")
        self.assertEqual(job["failureCode"], "WORKER_EXITED")
        self.assertIn("WORKER_EXITED", job["result"]["warnings"][0])
        self.assertIn("exitCode=1", job["result"]["warnings"][0])
        self.assertNotIn("ISOLATION_UNAVAILABLE", job["result"]["warnings"][0])
        # The facts survive into the job record's own isolation report, so a later reader sees the same number.
        self.assertEqual(job["isolation"]["exitCode"], 1)

    async def test_an_isolated_job_says_so_in_its_own_result(self):
        # The result the model reads must describe the posture this job actually ran under. The service is the
        # only layer that knows, which is why the line is derived from the code path here and not asserted as a
        # constant: the older constant version claimed isolation was "pending" in every deployment, including
        # this one, where the engine really is inside an AppContainer.
        payload = {"status": "partial", "answer": "", "warnings": [],
                   "candidates": [{"file": "", "is_found": True, "snippets": ["WALNUT-03"]}]}
        container = FakeContainer(behavior="ok", payload=payload)
        job = {"id": "annotation", "result": None}
        state, jobs, grants, env = self.isolated(container)
        with state, jobs, grants, env:
            await service.execute(job, self.request())
        warnings = job["result"]["warnings"]
        self.assertTrue(any(warning.startswith("ENGINE_ISOLATION: AppContainer enforced") for warning in warnings),
                        warnings)
        self.assertFalse(any("disabled" in warning for warning in warnings), warnings)
        self.assertFalse(any("pending" in warning for warning in warnings), warnings)

    async def test_container_candidates_are_verified_by_the_parent(self):
        payload = {"status": "partial", "answer": "", "usage": {"total_tokens": 5},
                   "candidates": [{"file": "", "is_found": True, "snippets": ["WALNUT-03"]}]}
        container = FakeContainer(behavior="ok", payload=payload)
        job = {"id": "t3", "result": None}
        state, jobs, grants, env = self.isolated(container)
        with state, jobs, grants, env:
            await service.execute(job, self.request())
        # The snapshot path is only known inside the run; the fake is told to point at it.
        self.assertEqual(job["result"]["status"], "partial")
        self.assertEqual(container.request["mode"], "FILENAME_ONLY")

    async def test_timeout_terminates_the_container(self):
        container = FakeContainer(behavior="timeout")
        job = {"id": "t4", "result": None}
        state, jobs, grants, env = self.isolated(container)
        with state, jobs, grants, env:
            await service.execute(job, self.request())
        self.assertEqual(job["result"]["status"], "partial")
        self.assertEqual(job["result"]["warnings"], ["BUDGET_EXCEEDED"])
        self.assertTrue(container.terminated, "the container tree must die with the budget")
        self.assertTrue(container.closed)

    async def test_cancellation_terminates_the_container_and_reports_cancelled(self):
        container = FakeContainer(behavior="hang")
        job = {"id": "t5", "result": None}
        state, jobs, grants, env = self.isolated(container)
        with state, jobs, grants, env:
            task = asyncio.create_task(service.execute(job, self.request()))
            for _ in range(100):
                if job.get("container") is not None:
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(job.get("container"), "container should be running")
            task.cancel()
            # execute() records the cancellation instead of propagating it to the caller.
            await task
        self.assertTrue(container.terminated, "cancellation must terminate the container tree")
        self.assertEqual(job["result"]["status"], "cancelled")

    async def test_stale_profile_is_reclaimed_at_startup(self):
        seen = {}
        state_file = Path(self.temp.name) / "isolation-profile.json"

        def fake_create(name):
            seen["created"] = name
            profile = type("P", (), {})()
            profile.name = name
            profile.sid = "sid"
            profile.sid_string = type("S", (), {"value": "S-1-15-2-1"})()
            profile.delete = lambda: seen.setdefault("deleted_created", True)
            return profile

        with patch.object(service.appcontainer, "available", lambda: True), \
             patch.object(service, "RUNTIME", Path(self.temp.name)), \
             patch.object(service, "check_runtime_freshness"), \
             patch.object(service, "stage_isolation_payload"), \
             patch.object(service, "PROFILE_STATE_FILE", state_file), \
             patch.object(service.appcontainer, "reclaim_stale_profile", return_value="DSH.Knowledge.Svc.old") as reclaim, \
             patch.object(service.appcontainer, "Profile", type("P", (), {"create": staticmethod(fake_create)})), \
             patch.object(service.appcontainer, "record_profile") as record, \
             patch.object(service.appcontainer, "grant"), \
             patch.object(Path, "is_file", lambda self: True):
            service.start_isolation()
            self.assertTrue(reclaim.called)
            self.assertEqual(service.ISOLATION_STATE["reclaimed"], "DSH.Knowledge.Svc.old")
            self.assertTrue(record.called)
            service.stop_isolation()
        self.assertEqual(service.ISOLATION_STATE, {})
        self.assertFalse(state_file.exists())

    async def test_start_failure_cleans_up_and_refuses_to_serve(self):
        class Boom:
            @staticmethod
            def create(name):
                raise appcontainer.IsolationError("cannot create profile")

        with patch.object(service.appcontainer, "available", lambda: True), \
             patch.object(service, "check_runtime_freshness"), \
             patch.object(service, "stage_isolation_payload"), \
             patch.object(service.appcontainer, "reclaim_stale_profile", return_value=None), \
             patch.object(service.appcontainer, "Profile", Boom), \
             patch.object(service, "stop_isolation") as cleanup:
            with self.assertRaises(appcontainer.IsolationError):
                service.start_isolation()
        self.assertTrue(cleanup.called, "a failed startup must clean up before refusing")

    async def test_a_runtime_staged_from_older_inputs_refuses_startup(self):
        # The headline guarantee of the manifest: never serve from an image built from old code.
        with patch.object(service.appcontainer, "available", lambda: True), \
             patch.object(service, "RUNTIME", Path(self.temp.name)), \
             patch.object(service.staging, "verify", return_value=(False, "changed since staging: engine/worker.py")), \
             patch.object(service, "stage_isolation_payload") as stage, \
             patch.object(service.appcontainer, "Profile") as profile:
            (Path(self.temp.name) / "ready").write_text("staged", encoding="utf-8")
            with self.assertRaises(appcontainer.IsolationError) as raised:
                service.start_isolation()
        self.assertIn("STALE_RUNTIME", str(raised.exception))
        self.assertIn("engine/worker.py", str(raised.exception))
        self.assertFalse(stage.called, "a stale runtime must be refused before anything else happens")
        self.assertFalse(profile.create.called)

    async def test_the_snapshot_spends_from_the_same_job_budget_as_the_model(self):
        # Queueing and snapshotting must come out of one deadline: the container is only given what
        # is left, so a slow snapshot cannot extend the job's wall clock.
        container = FakeContainer(behavior="ok", payload={"status": "partial", "answer": "", "candidates": []})
        state, jobs, grants, env = self.isolated(container)
        original = service.build_snapshot

        def slow_snapshot(root, work_dir, max_file_bytes=None):
            time.sleep(0.8)
            return original(root, work_dir, max_file_bytes)

        with state, jobs, grants, env, \
             patch.dict(os.environ, {"DSH_KNOWLEDGE_BUDGET_FAST": "3"}), \
             patch.object(service, "build_snapshot", slow_snapshot), \
             patch.object(service.model_proxy, "provider_from_environment",
                          return_value={"base_url": "http://127.0.0.1:9/v1", "api_key": "x", "model": "m"}):
            job = {"id": "t9", "result": None}
            await service.execute(job, self.request(mode="FAST"))
        remaining = container.request["modelBudgetSeconds"]
        self.assertLess(remaining, 2.9, "the snapshot time must be subtracted from the job budget")
        self.assertGreater(remaining, 1.2)
        self.assertLessEqual(container.timeout_ms, int((remaining + 5.5) * 1000),
                             "the container never gets the full budget a second time")
        self.assertGreaterEqual(job["budget"]["elapsedSeconds"], 0.7, job["budget"])

    async def test_a_snapshot_that_eats_the_budget_never_starts_the_container(self):
        container = FakeContainer(behavior="ok")
        state, jobs, grants, env = self.isolated(container)
        original = service.build_snapshot

        def slow_snapshot(root, work_dir, max_file_bytes=None):
            time.sleep(1.2)
            return original(root, work_dir, max_file_bytes)

        with state, jobs, grants, env, \
             patch.dict(os.environ, {"DSH_KNOWLEDGE_BUDGET_FAST": "1"}), \
             patch.object(service, "build_snapshot", slow_snapshot):
            job = {"id": "t10", "result": None}
            await service.execute(job, self.request(mode="FAST"))
        self.assertEqual(job["result"]["warnings"], ["BUDGET_EXCEEDED during snapshot"])
        self.assertIsNone(container.state_dir, "an exhausted budget must not launch the container")
        self.assertTrue(job["workDirRemoved"])

    async def test_a_result_written_at_the_deadline_is_kept_not_overwritten(self):
        # The deadline and the engine's last write race. A payload that landed must be the result the
        # client sees, flagged as having arrived at the deadline - never replaced by a bare timeout.
        work_dir = self.cache / "job-race"
        payload = {"status": "partial", "answer": "", "warnings": ["engine said so"],
                   "candidates": [{"file": str(work_dir / "files" / "note-03.md"), "is_found": True,
                                   "snippets": ["WALNUT-03"]}]}
        container = FakeContainer(behavior="timeout-with-result", payload=payload)
        state, jobs, grants, env = self.isolated(container)
        with state, jobs, grants, env, \
             patch.object(service.model_proxy, "provider_from_environment",
                          return_value={"base_url": "http://127.0.0.1:9/v1", "api_key": "x", "model": "m"}):
            job = {"id": "race", "result": None}
            await service.execute(job, self.request(mode="FAST"))
        self.assertEqual(job["result"]["status"], "partial")
        self.assertEqual(len(job["result"]["evidence"]), 1, "the late payload was still verified")
        self.assertEqual(job["result"]["evidence"][0]["relativePath"], "note-03.md")
        self.assertIn("engine said so", job["result"]["warnings"])
        self.assertTrue(any("DEADLINE_REACHED_RESULT_KEPT" in warning for warning in job["result"]["warnings"]),
                        job["result"]["warnings"])
        self.assertNotIn("BUDGET_EXCEEDED", " ".join(job["result"]["warnings"]))
        self.assertTrue(job["isolation"]["terminated"], "the tree is still terminated first")

    async def test_a_cancelled_job_never_keeps_a_result_it_already_wrote(self):
        # The opposite race: a result may exist on disk, but the client asked to stop - that must win.
        payload = {"status": "partial", "answer": "", "warnings": [], "candidates": []}
        container = FakeContainer(behavior="result-then-hang", payload=payload)
        state, jobs, grants, env = self.isolated(container)
        with state, jobs, grants, env:
            job = {"id": "cancelled-race", "result": None}
            task = asyncio.create_task(service.execute(job, self.request()))
            written = False
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if container.state_dir is not None and (container.state_dir / "result.json").is_file():
                    written = True
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(written, "the result must exist before the cancel")
            self.assertFalse(task.done(), "the job must still be running when it is cancelled")
            task.cancel()
            await asyncio.wait_for(task, 10)
        self.assertEqual(job["result"]["status"], "cancelled")
        self.assertEqual(job["result"]["warnings"],
                         ["Local worker stopped; remote requests already sent may still be billed"])

    async def test_unusable_deadline_results_never_reach_finalize(self):
        for content in ('{"status":', '[]', '"not-an-object"'):
            with self.subTest(content=content):
                class BrokenResult(FakeContainer):
                    def wait(self, timeout_ms):
                        (self.state_dir / "result.json").write_text(content, encoding="utf-8")
                        raise TimeoutError("deadline after incomplete or invalid result")
                container = BrokenResult()
                state, jobs, grants, env = self.isolated(container)
                with state, jobs, grants, env, patch.object(service, "finalize") as finalize:
                    job = {"id": "broken-deadline", "result": None}
                    await service.execute(job, self.request())
                finalize.assert_not_called()
                self.assertEqual(job["result"]["evidence"], [])
                self.assertEqual(job["result"]["warnings"], ["BUDGET_EXCEEDED"])
                self.assertTrue(container.terminated)
                self.assertTrue(container.closed)
                self.assertTrue(job["workDirRemoved"])

    # -- stage diagnostics -------------------------------------------------------------------------
    def diagnostics_events(self, directory):
        files = sorted(Path(directory).glob("*.jsonl"))
        self.assertEqual(len(files), 1, "one daily sink per run")
        return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]

    async def test_stage_diagnostics_outlive_the_removed_job_directory(self):
        # The job directory - and the container log inside it - is deleted when the job ends, which is
        # exactly when the parent's own record is the only thing left to review.
        container = FakeContainer(behavior="ok", payload={"status": "partial", "answer": "", "candidates": []})
        state, jobs, grants, env = self.isolated(container)
        sink = Path(self.temp.name) / "diagnostics"
        with state, jobs, grants, env, patch.object(service.stage_diagnostics, "DEFAULT_DIR", sink):
            job = {"id": "diag-1", "result": None}
            await service.execute(job, self.request())
        self.assertTrue(job["workDirRemoved"], "the audit must survive the directory it describes")
        events = self.diagnostics_events(sink)
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds[0], "request_received")
        self.assertEqual((events[0]["mode"], events[0]["libraryId"]), ("FILENAME_ONLY", "fixtures"))
        self.assertIn("snapshot_done", kinds)
        self.assertIn("container_started", kinds)
        self.assertEqual(kinds[-1], "cleanup_done")
        self.assertEqual(events[-1]["reason"], "workDirRemoved=True")
        self.assertEqual(events[-1]["calls"], 0)

    async def test_a_call_in_flight_at_the_deadline_is_not_recorded_as_never_started(self):
        # The distinction the last real run could not make: "the model was never called" and "the call
        # was cut off in flight" both looked like 0 calls / 0 tokens in the final result.
        container = FakeContainer(behavior="timeout")
        state, jobs, grants, env = self.isolated(container)
        sink = Path(self.temp.name) / "diagnostics"
        FakeProxy.mode = "in-flight"
        with state, jobs, grants, env, \
             patch.object(service.stage_diagnostics, "DEFAULT_DIR", sink), \
             patch.object(service.model_proxy, "ModelProxy", FakeProxy), \
             patch.object(service.model_proxy, "serve_in_thread", lambda *args, **kwargs: (None, {})), \
             patch.object(service.model_proxy, "provider_from_environment",
                          return_value={"base_url": "http://127.0.0.1:9/v1", "api_key": "x", "model": "m"}):
            job = {"id": "diag-flight", "result": None}
            await service.execute(job, self.request(mode="FAST"))
        events = {event["event"]: event for event in self.diagnostics_events(sink)}
        self.assertEqual(events["container_deadline"]["calls"], 1)
        self.assertIn("call-1", events["container_deadline"]["reason"])
        self.assertEqual(events["job_deadline"]["reason"], "BUDGET_EXCEEDED")
        self.assertIn("diag-flight", events["job_deadline"]["job"])
        self.assertEqual(job["result"]["warnings"], ["BUDGET_EXCEEDED"])
        self.assertTrue(job["workDirRemoved"])

    async def test_a_job_that_never_called_the_model_records_zero_in_flight(self):
        container = FakeContainer(behavior="timeout")
        state, jobs, grants, env = self.isolated(container)
        sink = Path(self.temp.name) / "diagnostics"
        FakeProxy.mode = "idle"
        with state, jobs, grants, env, \
             patch.object(service.stage_diagnostics, "DEFAULT_DIR", sink), \
             patch.object(service.model_proxy, "ModelProxy", FakeProxy), \
             patch.object(service.model_proxy, "serve_in_thread", lambda *args, **kwargs: (None, {})), \
             patch.object(service.model_proxy, "provider_from_environment",
                          return_value={"base_url": "http://127.0.0.1:9/v1", "api_key": "x", "model": "m"}):
            job = {"id": "diag-idle", "result": None}
            await service.execute(job, self.request(mode="FAST"))
        events = {event["event"]: event for event in self.diagnostics_events(sink)}
        self.assertEqual(events["container_deadline"]["calls"], 0)
        self.assertEqual(events["container_deadline"]["reason"], "in_flight=none")

    async def test_diagnostics_never_change_cancellation_or_budget_behaviour(self):
        # Same cancellation as the existing test, with diagnostics switched off: the outcome, the
        # result and the termination must be identical, or observability would be load-bearing.
        container = FakeContainer(behavior="hang")
        state, jobs, grants, env = self.isolated(container)
        sink = Path(self.temp.name) / "diagnostics"
        with state, jobs, grants, env, \
             patch.dict(os.environ, {"DSH_KNOWLEDGE_DIAGNOSTICS": "off"}), \
             patch.object(service.stage_diagnostics, "DEFAULT_DIR", sink):
            job = {"id": "diag-off", "result": None}
            task = asyncio.create_task(service.execute(job, self.request()))
            for _ in range(100):
                if job.get("container") is not None:
                    break
                await asyncio.sleep(0.02)
            task.cancel()
            await task
        self.assertTrue(container.terminated)
        self.assertEqual(job["result"]["status"], "cancelled")
        self.assertEqual(list(sink.glob("*.jsonl")), [], "a disabled sink writes nothing at all")

    async def test_the_parent_records_why_each_candidate_fragment_won_or_lost(self):
        # Lines that are each verbatim but not verbatim in this order: the whole-snippet path cannot
        # accept it, so the ranking runs and the parent has to say what it chose and what it dropped.
        work_dir = self.cache / "job-frag"
        snippet = "WALNUT-03\n# synthetic"
        payload = {"status": "partial", "answer": "", "warnings": [], "candidates": [
            {"file": str(work_dir / "files" / "note-03.md"), "is_found": True, "snippets": [snippet]}]}
        container = FakeContainer(behavior="timeout-with-result", payload=payload)
        state, jobs, grants, env = self.isolated(container)
        sink = Path(self.temp.name) / "diagnostics"
        with state, jobs, grants, env, \
             patch.object(service.stage_diagnostics, "DEFAULT_DIR", sink), \
             patch.object(service.model_proxy, "provider_from_environment",
                          return_value={"base_url": "http://127.0.0.1:9/v1", "api_key": "x", "model": "m"}):
            job = {"id": "frag", "result": None}
            await service.execute(job, self.request(mode="FAST"))
        records = [event for event in self.diagnostics_events(sink) if event["event"] == "fragment_choice"]
        self.assertTrue(records, json.dumps({"events": [event["event"] for event in self.diagnostics_events(sink)],
                                             "result": job["result"]})[:500])
        record = records[0]
        self.assertEqual(record["job"], "frag")
        self.assertNotIn("file", record, "the file name must not be recorded")
        fragments = record["fragments"]
        runs = fragments["runs"]
        self.assertTrue(runs)
        self.assertTrue(all("reason" in run and "score" in run for run in runs))
        self.assertTrue(any(run["selected"] for run in runs))
        # Two non-overlapping runs are both kept now (the per-file cap is two); a run that is dropped
        # gets a reason, and the pipeline suite covers the reasons themselves.
        self.assertTrue(all(run["reason"] is None if run["selected"] else run["reason"] for run in runs))
        self.assertEqual(sum(1 for run in runs if run["selected"]), fragments["parts"])
        self.assertLessEqual(fragments["parts"], 2)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{12}", run["fileId"]) for run in runs))
        self.assertEqual([layer["layer"] for layer in fragments["layers"]], ["engine", "worker", "parent"])
        for key in ("totalCandidates", "uniqueCandidates", "loggedCandidates", "truncatedCount"):
            self.assertIn(key, fragments)
        recorded_text = "\n".join(path.read_text(encoding="utf-8") for path in Path(sink).glob("*.jsonl"))
        self.assertNotIn("WALNUT-03", recorded_text,
                         "the ranking record carries ranges and scores, never the text")
        self.assertNotIn("note-03.md", recorded_text, "nor the file name")

    async def test_stage_payload_copies_engine_and_tools(self):
        with tempfile.TemporaryDirectory() as runtime:
            runtime = Path(runtime)
            tools = Path(self.temp.name) / "tools"
            tools.mkdir()
            (tools / "rga-patched.exe").write_bytes(b"stub")
            with patch.object(service, "RUNTIME", runtime), \
                 patch.object(service, "PATCHED_RGA", tools / "rga-patched.exe"), \
                 patch("runtime.executable", return_value=tools / "rga-patched.exe"):
                service.stage_isolation_payload()
            for name in service.CONTAINER_PAYLOAD:
                self.assertTrue((runtime / "engine" / name).is_file(), name)
            self.assertTrue((runtime / "rga.exe").is_file())
            self.assertTrue((runtime / "rg.exe").is_file())


class ContainerEnvironmentTests(unittest.TestCase):
    def test_the_container_never_inherits_the_host_profile_or_credentials(self):
        # The staged runtime copies the base interpreter prefix, so the real user site-packages
        # would be searched ahead of the staged one unless it is refused outright.
        with tempfile.TemporaryDirectory() as state:
            environment = appcontainer.container_environment(Path(state), Path("C:/staged-runtime-probe"))
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(environment["PYTHONUTF8"], "1")
        for name in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"):
            self.assertEqual(environment[name], state, "%s must point at the job state, not the profile" % name)
        self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(Path("C:/staged-runtime-probe")))
        for secret in ("DSH_KNOWLEDGE_TOKEN", "DSH_KNOWLEDGE_LLM_API_KEY", "NO_PROXY", "HTTP_PROXY"):
            self.assertNotIn(secret, environment, "the container must not receive %s" % secret)


class ContainerJobCleanupTests(unittest.TestCase):
    def test_failed_launch_leaves_no_handle_behind(self):
        # Real ctypes path: a launch that cannot start must not leak the job/process/attribute list.
        container = appcontainer.ContainerJob()
        with self.assertRaises(Exception):
            container.start(None, [str(Path(os.environ["SystemRoot"]) / "System32/missing-notepad.exe")], {})
        self.assertIsNone(container.job)
        self.assertIsNone(container.process)
        self.assertIsNone(container.thread)
        self.assertIsNone(container.attributes)


if __name__ == "__main__":
    unittest.main()
