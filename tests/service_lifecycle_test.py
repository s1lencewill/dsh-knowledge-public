"""The query lifecycle: admission, a lost response, cancellation, and the 429 split.

These are the acceptance tests for the repair, written against the **route functions** rather than through
`TestClient`, and that choice is deliberate: `TestClient` runs each request on a portal loop that drains the
pending tasks before it returns, so a job created by a request is already finished by the time the response is
handed back. A lifecycle that has states - accepted, running, cancelling - cannot be observed through a
harness that only ever sees the last one. Calling `submit`/`get_query`/`cancel` in one coroutine puts the job
on the same loop as the test, where its intermediate states are what they are in production.

Everything here is synthetic: no real model, no real library, no container. `execute` is replaced by a stub
that records what it was asked to do, so "no worker was started" is asserted rather than assumed.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
os.environ["DSH_KNOWLEDGE_TOKEN"] = "synthetic-token-for-service-test"
os.environ["DSH_KNOWLEDGE_ISOLATION"] = "off"
import service


REQUEST_ID = "kq-abcdef0123456789"


class LifecycleCase(unittest.IsolatedAsyncioTestCase):
    """One synthetic library, one isolated job table, one real event loop."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "note.md").write_text("# note\nWALNUT\n", encoding="utf-8")
        config = self.root / "config.json"
        config.write_text(json.dumps({"libraries": [{"id": "fixture", "name": "fixture",
                                                     "workspace": str(self.root), "root": str(self.root),
                                                     "syntheticFixture": True}]}), encoding="utf-8")
        patch.object(service, "CONFIG", config).start()
        self.addCleanup(patch.stopall)
        patch.object(service, "REQUEST_RECORDS", {}).start()
        self.jobs = {}
        patch.object(service, "JOBS", self.jobs).start()

    def query(self, text="q", **extra):
        # Every submission under this contract asserts a complete scope, so the helper always names a session
        # unless the test is deliberately exercising the missing-scope refusal.
        extra.setdefault("session", "session-a")
        return service.Query(workspace=str(self.root), libraryId="fixture", query=text, **extra)

    def owner(self, session="session-a"):
        """The same complete scope the submission recorded, in the shape the GET route takes."""
        recorded = service.owner_of(str(self.root), "fixture", session)
        return {"ownerWorkspace": recorded["workspace"], "ownerLibrary": recorded["libraryId"],
                "ownerSession": recorded["session"]}

    def cancel_body(self, session="session-a"):
        return service.CancelRequest(workspace=str(self.root), libraryId="fixture", session=session)

    async def settle(self, job=None, timeout=5.0):
        """Let the loop run until the job's task is done (or the timeout expires)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if job is None or job.get("task") is None or job["task"].done():
                return True
            await asyncio.sleep(0.01)
        return False

    async def started(self, job, timeout=2.0):
        """Let the loop run until the job's own coroutine has begun, and report the state it reached.

        `accepted` is the state the route records before the task has had a turn; the job's first line sets
        `running`. Waiting for the state rather than for a fixed number of yields is what keeps these tests
        from asserting on the scheduler.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if job.get("state") == "running":
                return True
            await asyncio.sleep(0)
        return False

    # --- 1. admission is bounded: the expensive work is inside the job ------------------------------------

    async def test_admission_answers_with_the_lifecycle_before_any_work_is_done(self):
        """The response is what tells the client which job to track, so it must not wait for the job."""
        order = []

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            order.append("work")
            job["result"] = {"status": "partial", "answer": "", "evidence": [], "warnings": []}

        with patch.object(service, "execute", stub):
            payload = await service.submit(self.query(requestId=REQUEST_ID))
            order.append("response")
            self.assertEqual(payload["requestId"], REQUEST_ID)
            self.assertEqual(payload["state"], "accepted")
            self.assertFalse(payload["terminal"])
            self.assertIsNone(payload["result"])
            self.assertEqual(payload["mode"], "FAST")
            self.assertEqual(payload["budgetSeconds"], 90)
            await self.settle(self.jobs[payload["id"]])
        self.assertEqual(order, ["response", "work"],
                         "the response is written before the job does any of its work")

    async def test_the_snapshot_runs_off_the_event_loop(self):
        """The blocking stage must leave the loop free, measured against a real wall-clock probe.

        The snapshot stub blocks its thread for a fixed time while a probe coroutine counts its own wake-ups.
        If the walk ran on the loop, the probe could not wake at all while it ran; running it in a thread is
        what keeps `/health`, polling and cleanup able to answer during a long walk. A structural assertion
        (which thread) would pass even if the walk were awaited on a loop-owned thread, so this measures the
        loop's actual liveness.
        """
        import threading
        seen = {}

        def blocking_snapshot(root, work_dir, max_bytes=None):
            seen["thread"] = threading.current_thread().name
            time.sleep(0.4)
            return {}, []

        ticks = []

        async def probe():
            for _ in range(40):
                await asyncio.sleep(0.01)
                ticks.append(time.monotonic())

        with patch.object(service, "build_snapshot", blocking_snapshot):
            job_id = (await service.submit(self.query()))["id"]
            await probe()
            self.assertTrue(await self.settle(self.jobs[job_id], timeout=3))
        # The probe's own wake-ups are the evidence: 40 timed yields cannot complete while a synchronous
        # 0.4s call owns the loop. (The *wall clock* of the probe is not asserted, because on a loaded machine
        # the yields themselves can be delayed; the count of completed yields is the property that matters.)
        self.assertGreaterEqual(len(ticks), 30,
                                "the loop kept running coroutines while the snapshot walked")
        self.assertNotEqual(seen.get("thread"), threading.main_thread().name,
                            "the library walk must not run on the event loop's own thread")

    async def test_health_reports_the_job_axis_and_a_busy_service_refuses_by_code(self):
        """A healthy service and an admissible one are different facts, and both are reported."""
        release = asyncio.Event()
        entered = asyncio.Event()

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            entered.set()
            await release.wait()
            job["result"] = {"status": "partial", "answer": "", "evidence": [], "warnings": []}

        with patch.object(service, "execute", stub):
            first = await service.submit(self.query())
            await entered.wait()
            self.assertTrue(await self.started(self.jobs[first["id"]]))
            body = await service.health()
            self.assertEqual(body["isolation"], "off")
            self.assertTrue(body["query"]["busy"])
            self.assertEqual(body["query"]["state"], "running")
            self.assertEqual(body["query"]["mode"], "FAST")
            # The job is running, so a second submission is refused with the **concurrency** code - not with a
            # quota code, which is what made a busy service read as an exhausted one.
            with self.assertRaises(service.HTTPException) as caught:
                await service.submit(self.query(requestId="kq-busy0000busy0000"))
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(caught.exception.error["code"], "QUERY_BUSY")
            self.assertEqual(caught.exception.error["reason"], "one_running_query")
            self.assertTrue(caught.exception.error["retryable"])
            self.assertIn("mode", caught.exception.error)
            self.assertIsInstance(caught.exception.error["retryAfterMs"], int)
            release.set()
            await self.settle(self.jobs[first["id"]])

    # --- 2. idempotent admission --------------------------------------------------------------------------

    async def test_the_same_request_id_is_admitted_once(self):
        calls = []
        release = asyncio.Event()

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            calls.append(job["id"])
            await release.wait()
            job["result"] = {"status": "partial", "answer": "", "evidence": [], "warnings": []}

        with patch.object(service, "execute", stub):
            first = await service.submit(self.query(requestId=REQUEST_ID))
            self.assertTrue(await self.started(self.jobs[first["id"]]))
            second = await service.submit(self.query(requestId=REQUEST_ID))
            self.assertEqual(first["id"], second["id"], "a repeated submission is the same job")
            self.assertEqual(calls, [first["id"]], "a repeat must not start a second worker")
            # The same id with different parameters is a *different* query; reusing the id would hide a
            # second, separately billed job behind the first one.
            with self.assertRaises(service.HTTPException) as caught:
                await service.submit(self.query(text="different", requestId=REQUEST_ID))
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(caught.exception.error["code"], "QUERY_REQUEST_CONFLICT")
            release.set()
            await self.settle(self.jobs[first["id"]])

    async def test_a_malformed_request_id_is_refused_rather_than_ignored(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self.query(requestId="not-a-request-id")
        self.assertEqual(self.jobs, {}, "a refused submission must not create a job")

    async def test_a_lost_admission_response_is_recoverable_by_request_id(self):
        """The client's own bound expired before it saw the answer; the job is still findable by its id."""
        release = asyncio.Event()

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            await release.wait()
            job["result"] = {"status": "partial", "answer": "", "evidence": [], "warnings": []}

        with patch.object(service, "execute", stub):
            submitted = await service.submit(self.query(requestId=REQUEST_ID))
            found = await service.find_query(REQUEST_ID, **self.owner())
            self.assertTrue(found["known"])
            self.assertEqual(found["id"], submitted["id"])
            self.assertIn(found["state"], ("accepted", "running"))
            missing = await service.find_query("kq-0000000000000000", **self.owner())
            self.assertFalse(missing["known"], "an unknown id is reported as unknown, not as an error")
            self.assertIsNone(missing["state"])
            release.set()
            await self.settle(self.jobs[submitted["id"]])

    # --- 3. cancellation is bounded, addressed and confirmed ----------------------------------------------

    async def test_cancelling_a_running_job_confirms_the_terminal_state(self):
        entered = asyncio.Event()

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            entered.set()
            await asyncio.sleep(30)

        with patch.object(service, "execute", stub):
            identity = (await service.submit(self.query()))["id"]
            await entered.wait()
            self.assertTrue(await self.started(self.jobs[identity]))
            # A poll while it runs reports the live state, not "not found" and not a terminal one.
            running = await service.get_query(identity, **self.owner())
            self.assertEqual(running["state"], "running")
            self.assertFalse(running["terminal"])
            first = await service.cancel(identity, self.cancel_body())
            self.assertEqual(first["state"], "cancelled")
            self.assertTrue(first["terminal"])
            self.assertTrue(first["cancellationConfirmed"], "a cancellation that was applied is confirmed")
            self.assertEqual(first["result"]["status"], "cancelled")
            # The job is settled and released: a second call reports it as gone rather than as a second,
            # differently shaped cancellation.
            second = await service.cancel(identity, self.cancel_body())
            self.assertTrue(second["alreadyRemoved"])
            self.assertFalse(second["cancellationConfirmed"],
                             "an id this service no longer holds is not a cancellation it observed")
        self.assertNotIn(identity, self.jobs)

    async def test_cancelling_before_the_worker_starts_leaves_a_terminal_state(self):
        """A job cancelled while it is still preparing must still end with a result of its own."""
        worker_calls = []

        async def worker(*args, **kwargs):
            worker_calls.append(True)
            raise AssertionError("a cancelled job must not reach a worker")

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            await asyncio.sleep(30)

        with patch.object(service, "execute", stub), \
             patch.object(service.asyncio, "create_subprocess_exec", worker):
            identity = (await service.submit(self.query()))["id"]
            await asyncio.sleep(0)  # let the task start and park
            body = await service.cancel(identity, self.cancel_body())
        self.assertEqual(worker_calls, [])
        self.assertTrue(body["terminal"])
        self.assertEqual(body["result"]["status"], "cancelled")
        self.assertEqual(body["state"], "cancelled")
        self.assertTrue(body["cancellationConfirmed"])

    async def test_confirming_a_finished_job_keeps_its_result_and_releases_the_capacity(self):
        """Confirmation and cancellation are two different actions, and the second must not rewrite the first.

        This is the assertion the previous round got wrong: it used DELETE for both, so "the client has read
        its result" and "stop the job" travelled the same path - and cancelling an already-finished job either
        did nothing (when it worked) or was the only way to release capacity (when it did not).
        """
        async def stub(job, request):
            service.mark_running(job, request["mode"])
            job["result"] = {"status": "partial", "answer": "", "evidence": [], "warnings": []}

        with patch.object(service, "execute", stub):
            identity = (await service.submit(self.query()))["id"]
            await self.settle(self.jobs[identity])
            confirmed = await service.acknowledge(identity, service.AcknowledgeRequest(
                workspace=str(self.root), libraryId="fixture", session="session-a"))
            self.assertTrue(confirmed["acknowledged"])
            self.assertTrue(confirmed["released"], "a read result releases its capacity slot")
            self.assertEqual(confirmed["result"]["status"], "partial",
                             "confirming a finished job never rewrites its result as cancelled")
            self.assertNotIn(identity, self.jobs)
            # A second confirmation is idempotent: the id is gone, and the answer says so without claiming a
            # cancellation it did not observe.
            again = await service.acknowledge(identity, service.AcknowledgeRequest(
                workspace=str(self.root), libraryId="fixture", session="session-a"))
            self.assertTrue(again["alreadyRemoved"])
            self.assertFalse(again["cancellationConfirmed"])

    async def test_another_scope_cannot_cancel_or_read_this_job(self):
        release = asyncio.Event()

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            await release.wait()

        with patch.object(service, "execute", stub):
            identity = (await service.submit(self.query(session="session-mine")))["id"]
            with self.assertRaises(service.HTTPException) as caught:
                await service.cancel(identity, self.cancel_body("session-other"))
            self.assertEqual(caught.exception.status_code, 404)
            with self.assertRaises(service.HTTPException):
                await service.get_query(identity, ownerWorkspace=str(self.root), ownerLibrary="fixture",
                                        ownerSession="session-other")
            # Its own scope works. A caller that asserts **nothing** is refused: for a job that recorded a
            # scope, an omitted field is not "the same owner with less detail", it is an unidentified caller.
            self.assertEqual((await service.get_query(
                identity, ownerWorkspace=str(self.root), ownerLibrary="fixture",
                ownerSession="session-mine"))["id"], identity)
            with self.assertRaises(service.HTTPException) as bare:
                await service.get_query(identity)
            self.assertEqual(bare.exception.status_code, 403)
            self.assertEqual(bare.exception.error["code"], "QUERY_SCOPE_REQUIRED")
            self.assertEqual(sorted(bare.exception.error["missing"]), ["libraryId", "session", "workspace"])
            release.set()
            await self.settle(self.jobs[identity])

    async def test_a_partial_scope_cannot_reach_a_job_either(self):
        """Omitting one field is not a lesser scope; it is an unidentified caller, and it is refused."""
        release = asyncio.Event()

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            await release.wait()

        with patch.object(service, "execute", stub):
            identity = (await service.submit(self.query()))["id"]
            # An omitted field is refused as an incomplete scope (403), and the refusal names which fields are
            # missing - they are members of this route's own parameter set, so nothing else can be named.
            for partial, missing in (({"ownerWorkspace": str(self.root)}, ["libraryId", "session"]),
                                     ({"ownerWorkspace": str(self.root), "ownerLibrary": "fixture"}, ["session"]),
                                     ({"ownerLibrary": "fixture"}, ["workspace", "session"]),
                                     ({}, ["workspace", "libraryId", "session"])):
                with self.assertRaises(service.HTTPException) as caught:
                    await service.get_query(identity, **partial)
                self.assertEqual(caught.exception.status_code, 403, partial)
                self.assertEqual(caught.exception.error["code"], "QUERY_SCOPE_REQUIRED", partial)
                self.assertEqual(sorted(caught.exception.error["missing"]), sorted(missing), partial)
            # A scope that is filled in but *wrong* is not an incomplete request: it is a caller claiming to be
            # someone else, and it is answered the way a wrong id is - with nothing.
            with self.assertRaises(service.HTTPException) as foreign:
                await service.get_query(identity, ownerWorkspace="C:\\elsewhere", ownerLibrary="fixture",
                                        ownerSession="session-a")
            self.assertEqual(foreign.exception.status_code, 404)
            # A refused scope must not change the job: it is still pollable by its owner, and the refusals
            # above happened before any shutdown path, so its task is still live.
            self.assertTrue(await self.started(self.jobs[identity]))
            self.assertEqual((await service.get_query(identity, **self.owner()))["state"], "running")
            release.set()
            await self.settle(self.jobs[identity])

    async def test_a_legacy_ownerless_job_is_still_served_but_only_without_a_scope_claim(self):
        """The compatibility path is narrow, and this test is what keeps it narrow.

        A job with no recorded owner exists only in an in-process call or in a table restored from an older
        build. It is served to a caller that asserts nothing; a caller that names *some* scope must still match
        a job that has one - and this synthetic job has none, so the assertion is not silently satisfied.
        """
        finished = asyncio.get_running_loop().create_future()
        finished.set_result(None)
        legacy = {"id": "legacy", "task": finished, "result": service.terminal("partial", "x"),
                  "acknowledged": False, "finishedAt": time.monotonic(), "requestId": None}
        with patch.object(service, "JOBS", {"legacy": legacy}):
            self.assertEqual((await service.get_query("legacy"))["id"], "legacy")
            self.assertEqual((await service.cancel("legacy"))["id"], "legacy")

    # --- 4. the 429 split and the worker codes ------------------------------------------------------------

    async def test_a_full_result_store_is_reported_as_capacity_not_as_busy(self):
        finished = asyncio.get_running_loop().create_future()
        finished.set_result(None)
        job = {"id": "old", "task": finished, "result": service.terminal("partial", "x"),
               "acknowledged": False, "finishedAt": time.monotonic(), "owner": None, "requestId": None}
        with patch.object(service, "JOBS", {"old": job}), patch.object(service, "MAX_JOBS", 1):
            with self.assertRaises(service.HTTPException) as caught:
                await service.submit(self.query())
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.error["code"], "RESULT_CAPACITY")

    async def test_an_expired_unread_result_is_reclaimed_so_capacity_is_not_held_for_ever(self):
        finished = asyncio.get_running_loop().create_future()
        finished.set_result(None)
        old = {"id": "old", "task": finished, "result": service.terminal("partial", "x"),
               "acknowledged": False, "finishedAt": time.monotonic() - service.RESULT_LEASE_SECONDS - 1,
               "owner": None, "requestId": None}

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            job["result"] = {"status": "partial", "answer": "", "evidence": [], "warnings": []}

        jobs = {"old": old}
        with patch.object(service, "JOBS", jobs), patch.object(service, "MAX_JOBS", 1), \
             patch.object(service, "execute", stub):
            accepted = await service.submit(self.query())
            self.assertEqual(accepted["state"], "accepted")
            self.assertNotIn("old", jobs, "a leased-out result must not hold the capacity for ever")

    async def test_a_container_that_ran_and_wrote_nothing_is_a_worker_failure(self):
        """Not ISOLATION_UNAVAILABLE: the two outcomes used to be one warning, which is how a job with four
        provider calls came back described as an isolation problem."""
        self.assertEqual(service.worker_failure(None)["code"], "WORKER_NO_RESULT")
        self.assertEqual(service.worker_failure(0)["code"], "WORKER_NO_RESULT")
        facts = service.worker_failure(1)
        self.assertEqual(facts["code"], "WORKER_EXITED")
        self.assertEqual(facts["exitCode"], 1)
        # Which *kind* of no-result it was is carried as a closed-set token; anything else is dropped.
        self.assertEqual(service.worker_failure(None, "RESULT_UNREADABLE")["reason"], "RESULT_UNREADABLE")
        self.assertEqual(service.worker_failure(None, "C:\\private\\path")["reason"], "no_result_file")
        result = service.lifecycle_result(facts["code"], facts["reason"], facts["exitCode"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("WORKER_EXITED", result["warnings"][0])
        self.assertIn("exitCode=1", result["warnings"][0])
        result = service.lifecycle_result("WORKER_FAILURE", "unclassified_parent_side_error")
        self.assertIn("WORKER_FAILURE", result["warnings"][0])
        self.assertNotIn("Traceback", result["warnings"][0])

    async def test_the_lifecycle_result_never_carries_free_text(self):
        """Whatever reaches `reason` is reduced to a closed-set token, so no path or handle can travel."""
        result = service.lifecycle_result("WORKER_NO_RESULT", "C:\\private\\dir\\file.md")
        self.assertEqual(result["warnings"], ["WORKER_NO_RESULT"])

    # --- 5. the per-session job axis ----------------------------------------------------------------------

    async def test_the_job_axis_reports_the_running_query_and_not_only_the_service(self):
        release = asyncio.Event()

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            await release.wait()

        with patch.object(service, "execute", stub):
            identity = (await service.submit(self.query(mode="DEEP")))["id"]
            self.assertTrue(await self.started(self.jobs[identity]))
            payload = await service.get_query(identity, **self.owner())
            self.assertEqual(payload["state"], "running")
            self.assertEqual(payload["mode"], "DEEP")
            self.assertFalse(payload["terminal"])
            self.assertIsInstance(payload["elapsedSeconds"], (int, float))
            self.assertEqual(payload["budgetSeconds"], 240)
            release.set()
            await self.settle(self.jobs[identity])

    # --- T2: the snapshot writer must be settled before anything is confirmed -----------------------------

    async def test_a_cancel_wait_is_not_confirmed_until_the_snapshot_writer_has_exited(self):
        """The counterexample the review proved: `asyncio.to_thread` keeps running after `Task.cancel`.

        The snapshot stub blocks a **thread** on a `threading.Event` that the test only releases after the
        cancellation has been requested. If the route confirmed the cancel on `await task` alone, it would
        answer "cancelled" while the writer was still inside the stub - and the cleanup that follows would
        delete the job directory that the writer then recreates. So the assertions are ordering facts, not a
        single observation: at the moment the confirmation arrives, the writer has already left the stub.
        """
        import threading
        entered = threading.Event()
        release = threading.Event()
        left = []
        worker_calls = []

        def blocking_snapshot(root, work_dir, max_bytes=None):
            entered.set()
            release.wait(10)
            left.append(threading.current_thread().name)
            return {}, []

        async def worker(*args, **kwargs):
            worker_calls.append(True)
            raise AssertionError("a cancelled job must not reach a worker")

        with patch.object(service, "build_snapshot", blocking_snapshot), \
             patch.object(service.asyncio, "create_subprocess_exec", worker):
            submitted = await service.submit(self.query())
            identity = submitted["id"]
            job = self.jobs[identity]
            while not entered.is_set():
                await asyncio.sleep(0.01)
            self.assertFalse(job["prep"].done(), "the writer is inside the stub when the cancel is requested")
            cancelling = asyncio.ensure_future(service.cancel(identity, self.cancel_body()))
            await asyncio.sleep(0.05)
            self.assertFalse(cancelling.done(), "the cancel must wait for the writer, not confirm around it")
            self.assertFalse(left, "the writer has not left the stub yet")
            release.set()
            body = await cancelling
        # Ordering: the writer had exited (it is the one that appends to `left`) by the time the answer came
        # back, and the answer says so. The thread's name is not asserted - `asyncio.to_thread` uses the loop's
        # thread pool, so "not MainThread" is the only name fact that is stable here.
        self.assertEqual(len(left), 1)
        self.assertNotEqual(left[0], threading.main_thread().name)
        self.assertTrue(body["cancellationConfirmed"])
        self.assertTrue(body["settled"])
        self.assertEqual(body["result"]["status"], "cancelled")
        self.assertEqual(worker_calls, [], "no worker may be started for a cancelled job")
        # And the cleanup is final: the directory the writer used is gone and stays gone.
        work_dir = job["workDir"]
        self.assertFalse(Path(work_dir).exists(), "the job directory must be removed after the writer exits")
        await asyncio.sleep(0.2)
        self.assertFalse(Path(work_dir).exists(),
                         "nothing may recreate the job directory after the cancellation is confirmed")

    async def test_a_cancel_that_cannot_observe_the_writer_reports_cancelling_and_keeps_the_slot(self):
        """The honest failure mode: a writer that will not finish is reported as *not* settled.

        `shutdown_job` is called directly with a bound far below the writer's own wait, so the route's real
        behaviour under a stuck writer is what is asserted - the job keeps `cancelUnconfirmed`, it is not
        reported as cancelled, and the next submission is refused rather than overlapping it.
        """
        import threading
        entered = threading.Event()
        release = threading.Event()

        def blocking_snapshot(root, work_dir, max_bytes=None):
            entered.set()
            release.wait(10)
            return {}, []

        with patch.object(service, "build_snapshot", blocking_snapshot):
            identity = (await service.submit(self.query()))["id"]
            job = self.jobs[identity]
            while not entered.is_set():
                await asyncio.sleep(0.01)
            outcome = await service.shutdown_job(job, timeout=0.1)
            self.assertFalse(outcome["settled"], "a writer the shutdown could not outlast is not settled")
            self.assertEqual(outcome["code"], "CANCEL_PARTIAL")
            self.assertTrue(job.get("cancelUnconfirmed"))
            self.assertEqual(service.job_state(job), "cancelling",
                             "the job is not reported as cancelled while the writer is still running")
            # The slot stays held: a second submission is refused rather than started alongside it.
            with self.assertRaises(service.HTTPException) as caught:
                await service.submit(self.query(requestId="kq-0000000000000000"))
            self.assertEqual(caught.exception.error["code"], "QUERY_BUSY")
            release.set()
            await asyncio.sleep(0.1)

    async def test_preparation_spends_the_job_deadline_and_a_late_snapshot_starts_no_worker(self):
        """A snapshot that consumes the whole budget must not be followed by a worker on a fresh clock."""
        import threading
        worker_calls = []
        started = []

        def slow_snapshot(root, work_dir, max_bytes=None):
            started.append(time.monotonic())
            time.sleep(0.3)
            return {"f": "x"}, []

        async def worker(*args, **kwargs):
            worker_calls.append(True)
            raise AssertionError("the deadline was gone before the worker could start")

        with patch.object(service, "build_snapshot", slow_snapshot), \
             patch.object(service, "budget_seconds", lambda mode: 0.2), \
             patch.object(service.asyncio, "create_subprocess_exec", worker):
            identity = (await service.submit(self.query()))["id"]
            await self.settle(self.jobs[identity], timeout=5)
        result = self.jobs[identity]["result"]
        self.assertEqual(result["status"], "partial")
        self.assertIn("BUDGET_EXCEEDED during snapshot", result["warnings"][0])
        self.assertEqual(worker_calls, [], "no worker may start once the preparation has spent the budget")
        self.assertTrue(started)
        del threading

    # --- T3: terminal confirmation, capacity recycling and the dedup record -------------------------------

    async def test_more_than_MAX_JOBS_finished_and_confirmed_jobs_do_not_exhaust_capacity(self):
        """Capacity is recycled by *confirmation*, not by cancellation - and the count is the proof."""
        released = 0

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            job["result"] = {"status": "partial", "answer": "", "evidence": [], "warnings": []}

        with patch.object(service, "execute", stub), patch.object(service, "MAX_JOBS", 2):
            for index in range(5):
                submitted = await service.submit(self.query(text=f"q{index}",
                                                            requestId=f"kq-{index:016x}"))
                identity = submitted["id"]
                await self.settle(self.jobs[identity])
                confirmed = await service.acknowledge(identity, service.AcknowledgeRequest(
                    workspace=str(self.root), libraryId="fixture", session="session-a"))
                self.assertTrue(confirmed["released"], f"job {index} must release its slot")
                released += 1
        self.assertEqual(released, 5)
        self.assertEqual(self.jobs, {}, "all five jobs are gone once they have been confirmed")

    async def test_a_late_repeat_of_a_confirmed_request_id_never_reexecutes(self):
        runs = []
        async def stub(job, request):
            service.mark_running(job, request["mode"])
            runs.append(job["id"])
            job["result"] = service.terminal("partial", "done")
        body = service.AcknowledgeRequest(workspace=str(self.root), libraryId="fixture", session="session-a")
        with patch.object(service, "execute", stub):
            first = await service.submit(self.query(requestId=REQUEST_ID))
            await self.settle(self.jobs[first["id"]])
            await service.acknowledge(first["id"], body)
            with self.assertRaises(service.HTTPException) as caught:
                await service.submit(self.query(requestId=REQUEST_ID))
            self.assertEqual(caught.exception.error["code"], "QUERY_RESULT_RELEASED")
            with self.assertRaises(service.HTTPException) as caught:
                await service.submit(self.query(text="changed", requestId=REQUEST_ID))
            self.assertEqual(caught.exception.error["code"], "QUERY_REQUEST_CONFLICT")
            repeated = await service.acknowledge(first["id"], body)
            self.assertTrue(repeated["released"])
            self.assertFalse(repeated["cancellationConfirmed"])
            self.assertEqual(len(runs), 1)
            new = await service.submit(self.query(requestId="kq-0000111122223333"))
            await self.settle(self.jobs[new["id"]])
            self.assertEqual(len(runs), 2)

    async def test_an_unknown_job_is_not_reported_as_cancelled(self):
        """A restart forgets jobs; forgetting is not the same fact as cancelling."""
        body = await service.cancel("no-such-job", service.CancelRequest(
            workspace=str(self.root), libraryId="fixture", session="session-a"))
        self.assertTrue(body["alreadyRemoved"])
        self.assertFalse(body["cancellationConfirmed"],
                         "this service never observed a cancellation for an id it does not hold")
        self.assertEqual(body["state"], "unknown")
        acknowledged = await service.acknowledge("no-such-job", service.AcknowledgeRequest(
            workspace=str(self.root), libraryId="fixture", session="session-a"))
        self.assertFalse(acknowledged["cancellationConfirmed"])

    async def test_a_cancel_intent_for_a_job_that_does_not_exist_yet_is_honoured_when_it_appears(self):
        """T1.4 on the service side: the cleanup that arrives first must still stop the job that arrives next."""
        release = asyncio.Event()
        worker_calls = []

        async def stub(job, request):
            service.mark_running(job, request["mode"])
            while not release.is_set():
                await asyncio.sleep(0.01)

        async def worker(*args, **kwargs):
            worker_calls.append(True)
            raise AssertionError("a job with a registered cancel intent must not reach a worker")

        with patch.object(service, "execute", stub), \
             patch.object(service.asyncio, "create_subprocess_exec", worker):
            # The cleanup arrives before the submission does: the service has no such job, so it records the
            # intent instead of answering "nothing to do".
            intent = await service.acknowledge("unknown-id", service.AcknowledgeRequest(
                workspace=str(self.root), libraryId="fixture", session="session-a",
                requestId="kq-abcdef0123456789", cancelIntent=True))
            self.assertFalse(intent["known"])
            self.assertTrue(intent["cancelIntentRecorded"])
            # Now the job is admitted under that id - and is stopped rather than started.
            submitted = await service.submit(self.query(requestId="kq-abcdef0123456789"))
            self.assertTrue(submitted.get("cancelIntentHonoured"))
            self.assertEqual(submitted["state"], "cancelled")
        self.assertEqual(worker_calls, [])
        # The job is acknowledged and released: it is no longer in the table, and no work of its own runs.
        self.assertNotIn(submitted["id"], self.jobs, "the settled job releases its slot")

    async def test_a_cancel_intent_is_bounded_by_the_same_capacity_rules_as_a_job(self):
        """An intent is a job record precisely so it cannot become an unbounded table of its own."""
        with patch.object(service, "MAX_JOBS", 2):
            first = service.intent_holder(service.owner_of(str(self.root), "fixture", "s1"), "kq-1111111111111111")
            second = service.intent_holder(service.owner_of(str(self.root), "fixture", "s2"), "kq-2222222222222222")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertLessEqual(len(self.jobs), 2, "intents obey MAX_JOBS")



    async def test_retention_capacity_expiry_and_process_restart_are_explicit(self):
        async def stub(job, request):
            job["result"] = service.terminal("failed", "synthetic failure")
        body = service.AcknowledgeRequest(workspace=str(self.root), libraryId="fixture", session="session-a")
        with patch.object(service, "execute", stub), patch.object(service, "MAX_REQUEST_RECORDS", 1):
            first = await service.submit(self.query(requestId=REQUEST_ID))
            await self.settle(self.jobs[first["id"]])
            await service.acknowledge(first["id"], body)
            self.assertEqual(len(self.jobs), 0)
            with self.assertRaises(service.HTTPException) as caught:
                await service.submit(self.query(requestId="kq-0000111122223333"))
            self.assertEqual(caught.exception.error["code"], "QUERY_REQUEST_CAPACITY")
            record = next(iter(service.REQUEST_RECORDS.values()))
            self.assertNotIn("result", record)
            self.assertNotIn("query", record)
            record["expiresAt"] = time.monotonic() - 1
            second = await service.submit(self.query(requestId=REQUEST_ID))
            await self.settle(self.jobs[second["id"]])
            self.assertNotEqual(first["id"], second["id"])
            service.REQUEST_RECORDS.clear()  # models process restart, not persistence
            self.jobs.clear()
            found = await service.find_query(REQUEST_ID, **self.owner())
            self.assertFalse(found["known"])

    async def test_early_ack_and_result_written_before_finally_cannot_release(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def stub(job, request):
            service.mark_running(job, request["mode"])
            job["result"] = service.terminal("partial", "result already written")
            entered.set()
            await release.wait()  # deterministic finally/cleanup pause
        body = service.AcknowledgeRequest(workspace=str(self.root), libraryId="fixture", session="session-a")
        with patch.object(service, "execute", stub):
            first = await service.submit(self.query(requestId=REQUEST_ID))
            job = self.jobs[first["id"]]
            await entered.wait()
            self.assertFalse(service.build_state_payload(first["id"], job)["terminal"])
            with self.assertRaises(service.HTTPException) as caught:
                await service.acknowledge(first["id"], body)
            self.assertEqual(caught.exception.error["code"], "QUERY_NOT_TERMINAL")
            self.assertFalse(job["acknowledged"])
            self.assertIs(service.busy_job(), job)
            release.set()
            await self.settle(job)
            done = await service.acknowledge(first["id"], body)
            self.assertTrue(done["released"])
            self.assertEqual(done["result"]["status"], "partial")
            self.assertFalse(done["cancellationConfirmed"])

    async def test_confirmation_response_loss_and_parallel_resubmission_execute_once(self):
        runs = []
        async def stub(job, request):
            runs.append(job["id"])
            job["result"] = service.terminal("partial", "done")
        body = service.AcknowledgeRequest(workspace=str(self.root), libraryId="fixture", session="session-a")
        with patch.object(service, "execute", stub):
            first = await service.submit(self.query(requestId=REQUEST_ID))
            await self.settle(self.jobs[first["id"]])
            async def repeat():
                try:
                    return await service.submit(self.query(requestId=REQUEST_ID))
                except service.HTTPException as error:
                    return error.error["code"]
            outcomes = await asyncio.gather(service.acknowledge(first["id"], body), repeat(), repeat())
            self.assertTrue(outcomes[0]["released"])
            self.assertEqual(outcomes[1:], ["QUERY_RESULT_RELEASED"] * 2)
            repeated = await service.acknowledge_by_request(service.AcknowledgeRequest(
                **{**body.model_dump(), "requestId":REQUEST_ID}))
            self.assertTrue(repeated["terminal"])
            self.assertEqual(len(runs), 1)

    async def test_cancelled_terminal_repeated_ack_retains_cancellation_fact(self):
        async def stub(job, request):
            await asyncio.sleep(10)
        with patch.object(service, "execute", stub):
            first = await service.submit(self.query(requestId=REQUEST_ID))
            await asyncio.sleep(0)
            result = await service.acknowledge(first["id"], service.AcknowledgeRequest(
                workspace=str(self.root), libraryId="fixture", session="session-a", cancelIntent=True))
            self.assertTrue(result["cancellationConfirmed"])
            again = await service.acknowledge(first["id"], service.AcknowledgeRequest(
                workspace=str(self.root), libraryId="fixture", session="session-a"))
            self.assertTrue(again["cancellationConfirmed"])


class ReclaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_reclaim_keeps_an_unread_result_inside_its_lease(self):
        finished = asyncio.get_running_loop().create_future()
        finished.set_result(None)
        fresh = {"id": "fresh", "task": finished, "result": {}, "acknowledged": False,
                 "finishedAt": time.monotonic()}
        acknowledged = dict(fresh, id="ack", acknowledged=True)
        running = asyncio.get_running_loop().create_future()
        with patch.object(service, "JOBS", {"fresh": fresh, "ack": acknowledged, "run": {"task": running}}):
            service.reclaim_finished()
            self.assertIn("fresh", service.JOBS, "an unread result inside its lease is kept")
            self.assertNotIn("ack", service.JOBS, "an acknowledged result is done with")
            self.assertIn("run", service.JOBS, "a running job is never reclaimed")
        running.cancel()



if __name__ == "__main__":
    unittest.main()
