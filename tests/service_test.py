import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
os.environ["DSH_KNOWLEDGE_TOKEN"] = "synthetic-token-for-service-test"
# These tests exercise the plain-worker path (spawn, budgets, capacity); isolation has its own file.
os.environ["DSH_KNOWLEDGE_ISOLATION"] = "off"
from fastapi.testclient import TestClient
import service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = self.root / "config.json"
        config.write_text(json.dumps({"libraries": [{"id":"fixture","name":"fixture", "workspace":str(self.root),
                           "root":str(self.root),"syntheticFixture":False}]}))
        self.patch = patch.object(service, "CONFIG", config)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = TestClient(service.app)
        self.auth = {"Authorization": "Bearer " + service.TOKEN}

    def test_authentication(self):
        self.assertEqual(self.client.get("/health").status_code, 401)
        self.assertEqual(self.client.get("/health", headers=self.auth).status_code, 200)

    def test_workspace_and_fixture_gate(self):
        value = self.client.get("/libraries", params={"workspace":str(self.root)}, headers=self.auth)
        self.assertEqual(value.json(), [{"id":"fixture","name":"fixture"}])
        value = self.client.post("/queries", json={"workspace":str(self.root),"libraryId":"fixture","query":"q"},
                                 headers=self.auth)
        self.assertEqual(value.status_code, 403)

    def test_a_submission_without_a_complete_scope_is_refused(self):
        """The scope is what makes a job recoverable, so it is required rather than optional.

        Two layers answer here and both are correct. A *missing* field cannot reach the route at all: the
        request model declares the workspace and the library required, and the session is part of the scope the
        route checks. An **empty** string is a value the schema accepts and the route refuses, with the code
        that names which of the three was not supplied - which is the shape an older client produces when it
        has the field but nothing to put in it.
        """
        for missing, fields in (("session", {"workspace": str(self.root), "libraryId": "fixture", "query": "q",
                                             "session": ""}),
                                ("session", {"workspace": str(self.root), "libraryId": "fixture", "query": "q",
                                             "session": "   "})):
            response = self.client.post("/queries", json=fields, headers=self.auth)
            self.assertEqual(response.status_code, 403, (missing, response.text))
            self.assertEqual(response.json()["error"]["code"], "QUERY_SCOPE_REQUIRED")
            self.assertIn(missing, response.json()["error"]["missing"])
        # Omitting a field whose model entry has no default is the schema's refusal, and it names the field.
        without_library = self.client.post("/queries", json={"workspace": str(self.root), "query": "q",
                                                             "session": "session-a"}, headers=self.auth)
        self.assertEqual(without_library.status_code, 422)
        self.assertEqual(without_library.json()["detail"][0]["loc"][-1], "libraryId")
        # `session` has a default (so an older client's body still parses) and is then refused by the route -
        # which is the point: the omission is answered with the scope code, not silently recorded.
        without_session = self.client.post("/queries", json={"workspace": str(self.root), "libraryId": "fixture",
                                                             "query": "q"}, headers=self.auth)
        self.assertEqual(without_session.status_code, 403)
        self.assertEqual(without_session.json()["error"]["code"], "QUERY_SCOPE_REQUIRED")

    def test_unknown_library(self):
        value = self.client.post("/queries", json={"workspace":str(self.root),"libraryId":"unknown","query":"q",
                                                   "session": "session-a"}, headers=self.auth)
        self.assertEqual(value.status_code, 403)

    def test_invalid_workspace_returns_400_for_get_and_post(self):
        for workspace in (str(self.root / "missing"), "relative/path", str(service.CONFIG)):
            get = self.client.get("/libraries", params={"workspace":workspace}, headers=self.auth)
            post = self.client.post("/queries", json={"workspace":workspace,"libraryId":"fixture","query":"q",
                                                      "session": "session-a"}, headers=self.auth)
            self.assertEqual(get.status_code, 400)
            self.assertEqual(post.status_code, 400)
            self.assertNotIn(str(self.root), get.text)

    def test_authentication_precedes_workspace_validation(self):
        response = self.client.get("/libraries", params={"workspace":str(self.root / "missing")})
        self.assertEqual(response.status_code, 401)

    def test_missing_configuration_is_diagnosable(self):
        with patch.object(service, "CONFIG", self.root / "not-configured.json"):
            response = self.client.get("/libraries", params={"workspace":str(self.root)}, headers=self.auth)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "LIBRARY_CONFIG_UNAVAILABLE")

    def test_budgets_are_configurable_with_sane_defaults(self):
        self.assertEqual(service.budget_seconds("FAST"), 90)
        self.assertEqual(service.budget_seconds("DEEP"), 240)
        with patch.dict(os.environ, {"DSH_KNOWLEDGE_BUDGET_FAST": "5", "DSH_KNOWLEDGE_BUDGET_DEEP": "7"}):
            self.assertEqual(service.budget_seconds("FAST"), 5)
            self.assertEqual(service.budget_seconds("DEEP"), 7)
        for invalid in ("", "0", "-3", "soon"):
            with patch.dict(os.environ, {"DSH_KNOWLEDGE_BUDGET_FAST": invalid}):
                self.assertEqual(service.budget_seconds("FAST"), 90, invalid)

    def test_startup_sweep_removes_orphaned_worker_directories(self):
        cache = self.root / "query-cache"
        (cache / "job-orphan").mkdir(parents=True)
        (cache / "job-orphan" / "files").mkdir()
        (cache / "tmp-orphan").mkdir()
        (cache / "keep-me").mkdir()
        self.assertEqual(service.sweep_stale_jobs(cache), 2)
        self.assertFalse((cache / "job-orphan").exists())
        self.assertFalse((cache / "tmp-orphan").exists())
        self.assertTrue((cache / "keep-me").is_dir())

    def test_bad_query_schema_is_422(self):
        for extra in ({"query":""}, {"query":"x"*4001}, {"mode":"INVALID"}):
            response = self.client.post("/queries", json={"workspace":str(self.root),"libraryId":"fixture",
                                                          "query":"q", "session":"session-a", **extra},
                                        headers=self.auth)
            self.assertEqual(response.status_code, 422)

    def test_health_reports_the_effective_isolation_mode(self):
        with patch.dict(os.environ, {"DSH_KNOWLEDGE_ISOLATION": "off"}):
            body = self.client.get("/health", headers=self.auth).json()
        self.assertEqual(body["isolation"], "off")
        self.assertFalse(body["isolationActive"], "no container profile is active in the plain path")

    def test_health_reports_required_when_the_variable_is_unset(self):
        # Isolation is the default: an unset variable must not silently mean "off".
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSH_KNOWLEDGE_ISOLATION", None)
            body = self.client.get("/health", headers=self.auth).json()
        self.assertEqual(body["isolation"], "required")
        self.assertFalse(body["isolationActive"], "the report is the mode, not the startup outcome")

    def test_unusable_isolation_mode_refuses_startup(self):
        with patch.dict(os.environ, {"DSH_KNOWLEDGE_ISOLATION": "maybe"}), \
             self.assertRaisesRegex(RuntimeError, "DSH_KNOWLEDGE_ISOLATION"):
            with TestClient(service.app):
                pass

    def test_budgets_and_mode_are_read_after_import(self):
        # The service must not freeze the environment at import time; a host or test may set both.
        self.assertEqual(service.isolation_mode(), "off", "this file opts into the plain path")
        with patch.dict(os.environ, {"DSH_KNOWLEDGE_ISOLATION": " OFF "}):
            self.assertEqual(service.isolation_mode(), "off")
            self.assertFalse(service.isolation_required())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DSH_KNOWLEDGE_ISOLATION", None)
            self.assertEqual(service.isolation_mode(), "required")
            self.assertTrue(service.isolation_required())

    def test_health_reports_required_mode_when_isolation_is_active(self):
        with patch.dict(os.environ, {"DSH_KNOWLEDGE_ISOLATION": "required"}), \
             patch.object(service, "ISOLATION_STATE", {"profile": object()}):
            body = self.client.get("/health", headers=self.auth).json()
        self.assertEqual(body["isolation"], "required")
        self.assertTrue(body["isolationActive"])

    def test_docs_disabled(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path, headers=self.auth).status_code, 404)

    def test_missing_token_refuses_service_startup(self):
        with patch.object(service, "TOKEN", ""), self.assertRaisesRegex(RuntimeError, "DSH_KNOWLEDGE_TOKEN"):
            with TestClient(service.app):
                pass

    def test_an_embedding_configuration_refuses_service_startup(self):
        # The minimal runtime pruned torch/modelscope, so a setting that needs them must fail at
        # startup instead of quietly not applying (the container environment is an allow-list).
        with patch.dict(os.environ, {"EMBEDDING_MODEL_ID": "BAAI/bge-m3"}):
            with self.assertRaisesRegex(RuntimeError, "RUNTIME_INCOMPATIBLE"):
                with TestClient(service.app):
                    pass

    def test_out_of_snapshot_search_paths_refuse_service_startup(self):
        with patch.dict(os.environ, {"SIRCHMUNK_SEARCH_PATHS": "C:\\elsewhere"}):
            with self.assertRaisesRegex(RuntimeError, "RUNTIME_INCOMPATIBLE"):
                with TestClient(service.app):
                    pass


class TrialLibraryGateTests(unittest.TestCase):
    """The real-materials trial: off by default, per library, and pinned to the authorized files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.texts = self.workspace / "texts"
        self.texts.mkdir()
        for name in ("a.txt", "b.txt"):
            (self.texts / name).write_text("authorized text %s\n" % name, encoding="utf-8")
        self.config = self.workspace / "config.json"
        self.jobs = patch.object(service, "JOBS", {})
        self.jobs.start()
        self.addCleanup(self.jobs.stop)
        self.patch = patch.object(service, "CONFIG", self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = TestClient(service.app).__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.auth = {"Authorization": "Bearer " + service.TOKEN}
        self.seen = []

    def write_config(self, library):
        self.config.write_text(json.dumps({"libraries": [library]}), encoding="utf-8")

    def trial_library(self, **overrides):
        import real_data
        files = {name: real_data.sha256(self.texts / name) for name in ("a.txt", "b.txt")}
        library = {"id": "trial", "name": "trial", "workspace": str(self.workspace),
                   "root": str(self.texts), "syntheticFixture": False,
                   "realDataTrial": {"authorized": True, "files": files}}
        library.update(overrides)
        return library

    def submit(self, workspace=None, library_id="trial"):
        # A complete scope: the session is part of what makes a submitted job recoverable, so every test
        # submission names one unless it is deliberately testing the missing-scope refusal.
        body = {"workspace": str(workspace or self.workspace), "libraryId": library_id, "query": "q",
                "session": "session-trial"}
        return self.client.post("/queries", json=body, headers=self.auth)

    def completed(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        job = service.JOBS[response.json()["id"]]
        for _ in range(300):
            if job.get("task") and job["task"].done():
                return job["result"]
            time.sleep(0.01)
        self.fail("job did not settle")

    def install_stub_executor(self):
        outer = self
        class Process:
            pid = 0
            returncode = 0
            async def communicate(self, data):
                outer.seen.append(json.loads(data))
                return b'DSH_KNOWLEDGE_RESULT={"status":"partial","candidates":[]}\n', b''
            def kill(self): pass
            async def wait(self): return 0
        async def spawn(*args, **kwargs): return Process()
        return patch.object(service.asyncio, "create_subprocess_exec", spawn)

    def test_a_real_library_without_authorization_is_refused(self):
        library = self.trial_library()
        del library["realDataTrial"]
        self.write_config(library)
        response = self.submit()
        self.assertEqual(response.status_code, 403)
        self.assertIn("REAL_DATA_TRIAL_NOT_AUTHORIZED", response.text)
        self.assertEqual(self.seen, [], "a refused library must not start a job")

    def test_an_explicitly_authorized_library_is_accepted_and_flagged(self):
        self.write_config(self.trial_library())
        with self.install_stub_executor():
            response = self.submit()
            self.assertEqual(response.status_code, 200, response.text)
            identity = response.json()["id"]
            for _ in range(100):
                if self.seen:
                    break
                time.sleep(0.01)
        self.assertEqual(len(self.seen), 1)
        self.assertIn("REAL_DATA_TRIAL_LIBRARY", " ".join(self.completed(response)["warnings"]))
        self.assertIsNotNone(service.JOBS[identity]["result"])

    def test_a_changed_file_refuses_the_query(self):
        self.write_config(self.trial_library())
        (self.texts / "a.txt").write_text("tampered\n", encoding="utf-8")
        response = self.submit()
        result = self.completed(response)
        self.assertEqual(result["status"], "failed")
        self.assertIn("REAL_DATA_TRIAL_FILES_CHANGED", str(result))
        self.assertIn("1 changed", str(result))
        self.assertNotIn("a.txt", str(result))

    def test_an_added_or_missing_file_refuses_the_query(self):
        self.write_config(self.trial_library())
        (self.texts / "c.txt").write_text("unexpected\n", encoding="utf-8")
        self.assertIn("REAL_DATA_TRIAL_FILES_CHANGED", str(self.completed(self.submit())))
        (self.texts / "c.txt").unlink()
        (self.texts / "b.txt").unlink()
        self.assertIn("REAL_DATA_TRIAL_FILES_CHANGED", str(self.completed(self.submit())))

    def test_the_trial_library_is_still_scoped_to_its_workspace(self):
        self.write_config(self.trial_library())
        elsewhere = Path(tempfile.mkdtemp(prefix="other-workspace-"))
        self.addCleanup(shutil.rmtree, elsewhere, True)
        response = self.submit(workspace=elsewhere)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Unknown library for workspace", response.text)

    def test_a_synthetic_library_keeps_working_without_the_trial(self):
        self.write_config({"id": "fixture", "name": "fixture", "workspace": str(self.workspace),
                           "root": str(self.texts), "syntheticFixture": True})
        with self.install_stub_executor():
            response = self.submit(library_id="fixture")
            self.assertEqual(response.status_code, 200, response.text)
            for _ in range(100):
                if self.seen:
                    break
                time.sleep(0.01)
        self.assertIsNone(self.seen[0].get("trialWarning"),
                          "a synthetic library must not claim to be a real-data trial")

    def test_the_library_list_exposes_the_trial_library(self):
        self.write_config(self.trial_library())
        body = self.client.get("/libraries", params={"workspace": str(self.workspace)}, headers=self.auth).json()
        self.assertEqual(body, [{"id": "trial", "name": "trial"}])


class LibraryScopeTests(unittest.TestCase):
    """Which sessions a configured library is visible to.

    The scope rule is component-wise and two-sided: the session must sit at or under the library's declared
    `workspace`, and the library's `root` must sit at or under the session. This class pins every edge of that
    rule, including the ones the rule must still refuse, because the failure it replaces was silent - the
    operator saw "Configured: (none configured)" and nothing said which directory would have worked.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.workspace = base / "ws"
        self.library = self.workspace / "library"
        self.texts = self.library / "texts"
        self.texts.mkdir(parents=True)
        (self.texts / "a.txt").write_text("synthetic\n", encoding="utf-8")
        (self.workspace / "sources").mkdir()
        (base / "ws2").mkdir()
        self.config = base / "config.json"
        self.write_library(self.workspace, self.texts)
        self.patch = patch.object(service, "CONFIG", self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = TestClient(service.app)
        self.auth = {"Authorization": "Bearer " + service.TOKEN}

    def write_library(self, workspace, root, library_id="samples"):
        self.config.write_text(json.dumps({"libraries": [{"id": library_id, "name": library_id,
            "workspace": str(workspace), "root": str(root), "syntheticFixture": True}]}), encoding="utf-8")

    def ids(self, directory):
        response = self.client.get("/libraries", params={"workspace": str(directory)}, headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        return [entry["id"] for entry in response.json()]

    def test_the_declared_workspace_sees_the_library(self):
        self.assertEqual(self.ids(self.workspace), ["samples"])

    def test_a_session_inside_the_workspace_that_contains_the_root_sees_it(self):
        # This is the case that failed in the installed instance: the operator picked `<instance>\data\library`
        # while the library declared `<instance>\data`, and the answer was an empty list.
        self.assertEqual(self.ids(self.library), ["samples"])

    def test_the_library_root_itself_is_a_usable_session_directory(self):
        self.assertEqual(self.ids(self.texts), ["samples"])

    def test_equivalent_spellings_of_the_same_directory_match(self):
        self.assertEqual(self.ids(str(self.library) + os.sep), ["samples"])
        self.assertEqual(self.ids(str(self.library) + os.sep + "." + os.sep), ["samples"])

    @unittest.skipUnless(os.name == "nt", "case-insensitive directory identity is a Windows property")
    def test_a_case_difference_is_the_same_directory_on_windows(self):
        self.assertEqual(self.ids(str(self.library).upper()), ["samples"])

    def test_a_sibling_under_the_workspace_does_not_see_it(self):
        self.assertEqual(self.ids(self.workspace / "sources"), [])

    def test_a_same_prefix_sibling_does_not_see_it(self):
        self.assertEqual(self.ids(self.workspace.parent / "ws2"), [])

    def test_a_session_outside_the_declared_workspace_does_not_see_it(self):
        self.assertEqual(self.ids(self.workspace.parent), [])

    def test_a_library_whose_root_escapes_its_workspace_is_not_offered(self):
        outside = Path(self.temp.name) / "elsewhere"
        outside.mkdir()
        self.write_library(self.library, outside)
        self.assertEqual(self.ids(self.library), [], "a root outside the declared workspace must not be offered")
        self.assertEqual(self.ids(self.workspace), [])

    def test_a_library_whose_files_are_gone_is_skipped_without_failing_the_others(self):
        missing = self.library / "deleted"
        self.config.write_text(json.dumps({"libraries": [
            {"id": "gone", "name": "gone", "workspace": str(self.workspace), "root": str(missing),
             "syntheticFixture": True},
            {"id": "samples", "name": "samples", "workspace": str(self.workspace), "root": str(self.texts),
             "syntheticFixture": True}]}), encoding="utf-8")
        self.assertEqual(self.ids(self.workspace), ["samples"])

    def test_an_invalid_or_missing_configuration_still_answers_400_and_503(self):
        self.assertEqual(self.client.get("/libraries", params={"workspace": "relative/path"},
                                         headers=self.auth).status_code, 400)
        with patch.object(service, "CONFIG", self.workspace / "not-configured.json"):
            response = self.client.get("/libraries", params={"workspace": str(self.workspace)}, headers=self.auth)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "LIBRARY_CONFIG_UNAVAILABLE")

    def test_the_new_rule_does_not_leak_by_string_prefix(self):
        # Same letters, different directory: `<ws>/library2` must not be treated as inside `<ws>/library`.
        sibling = self.workspace / "library2"
        sibling.mkdir()
        self.write_library(self.workspace, sibling)
        self.assertEqual(self.ids(self.library), [], "a string-prefix match would have offered this library")
        self.assertEqual(self.ids(sibling), ["samples"])


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_unacknowledged_result_survives_capacity_pressure(self):
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(None)
        job = {"task":completed, "result":service.terminal("partial", "fixture"), "acknowledged":False,
               # A job that just finished: the lease is what keeps this result available, so an "infinitely
               # old" job would be reclaimed by the very rule under test instead of exercising it.
               "finishedAt": time.monotonic()}
        with patch.object(service, "JOBS", {"old":job}), patch.object(service, "MAX_JOBS", 1), \
             patch.object(service, "libraries", return_value=[{"id":"f","syntheticFixture":True,"root":"unused"}]):
            with self.assertRaises(service.HTTPException) as caught:
                await service.submit(service.Query(workspace="unused", libraryId="f", query="q",
                                                   session="session-a"))
            self.assertEqual(caught.exception.status_code, 429)
            # The refusal names the fact: a result is still inside its lease, and for how much longer.
            self.assertEqual(caught.exception.error["code"], "RESULT_CAPACITY")
            self.assertIsInstance(caught.exception.error["retryAfterMs"], int)
            self.assertEqual((await service.get_query("old"))["result"], job["result"])

    async def test_an_expired_unread_result_is_reclaimed_before_capacity_is_reported(self):
        """The other half of the same rule: a lease that has run out frees the slot instead of refusing."""
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(None)
        job = {"task": completed, "result": service.terminal("partial", "fixture"), "acknowledged": False,
               "finishedAt": time.monotonic() - service.RESULT_LEASE_SECONDS - 1}
        async def finish(job, request):
            job["result"] = service.terminal("partial", "new")
        with patch.object(service, "JOBS", {"old": job}), patch.object(service, "MAX_JOBS", 1), \
             patch.object(service, "libraries", return_value=[{"id":"f","syntheticFixture":True,"root":"unused"}]), \
             patch.object(service, "execute", finish):
            accepted = await service.submit(service.Query(workspace="unused", libraryId="f", query="q",
                                                          session="session-a"))
            self.assertEqual(accepted["state"], "accepted")
            self.assertNotIn("old", service.JOBS)

    async def test_confirming_a_result_frees_the_capacity_that_a_cancel_used_to(self):
        """The capacity path this round adds: a client confirms what it read, and the slot comes back."""
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(None)
        job = {"task": completed, "result": service.terminal("partial", "fixture"), "acknowledged": False,
               "finishedAt": time.monotonic(), "id": "old", "owner": None}
        with patch.object(service, "JOBS", {"old": job}):
            confirmed = await service.acknowledge("old", service.AcknowledgeRequest())
            self.assertTrue(confirmed["released"])
            self.assertEqual(confirmed["result"]["status"], "partial")
            self.assertNotIn("old", service.JOBS)

    async def test_a_missing_scope_is_refused_before_the_library_gate(self):
        """The refusal a caller gets must be about *their* request, not about a library they did not name."""
        with patch.object(service, "libraries", return_value=[{"id": "f", "syntheticFixture": True, "root": "unused"}]):
            with self.assertRaises(service.HTTPException) as caught:
                await service.submit(service.Query(workspace="unused", libraryId="f", query="q"))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.error["code"], "QUERY_SCOPE_REQUIRED")

    async def test_acknowledged_result_can_be_evicted_and_delete_is_idempotent(self):
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(None)
        job = {"task":completed, "result":service.terminal("partial", "fixture"), "acknowledged":False}
        async def finish(job, request):
            job["result"] = service.terminal("partial", "new fixture")
        with patch.object(service, "JOBS", {"old":job}), patch.object(service, "MAX_JOBS", 1), \
             patch.object(service, "libraries", return_value=[{"id":"f","syntheticFixture":True,"root":"unused"}]), \
             patch.object(service, "execute", finish):
            first = await service.cancel("old")
            self.assertFalse(first["cancellationConfirmed"], "confirming an already completed result is not cancellation")
            # A settled job is gone, so the next admission has the capacity its confirmation released.
            value = await service.submit(service.Query(workspace="unused", libraryId="f", query="q",
                                                       session="session-a"))
            await service.JOBS[value["id"]]["task"]
            self.assertNotIn("old", service.JOBS)
            again = await service.cancel("old")
            self.assertTrue(again["alreadyRemoved"])
            self.assertFalse(again["cancellationConfirmed"],
                             "an id this service no longer holds is not a cancellation it observed")

    async def test_second_running_query_rejected(self):
        running = asyncio.get_running_loop().create_future()
        try:
            with patch.object(service, "JOBS", {"active":{"task":running}}), \
                 patch.object(service, "libraries", return_value=[{"id":"f","syntheticFixture":True}]):
                with self.assertRaises(service.HTTPException) as caught:
                    await service.submit(service.Query(workspace="unused", libraryId="f", query="q",
                                                       session="session-a"))
                self.assertEqual(caught.exception.status_code, 429)
                self.assertEqual(caught.exception.error["code"], "QUERY_BUSY")
        finally:
            running.cancel()

    def _library(self):
        """A minimal real library the parent can snapshot (the child never sees it)."""
        root = Path(tempfile.mkdtemp(prefix="svc-library-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "fixture.md").write_text("# fixture\nWALNUT\n", encoding="utf-8")
        return root

    async def test_the_plain_path_reports_disabled_isolation_in_its_own_result(self):
        # `off` is a development opt-out, and the result the model reads has to say so. The older text told
        # every result that isolation was "pending" - false in this deployment (where the engine really runs in
        # an AppContainer) and misleading here, where the engine runs with the service's own access.
        result_line = b"DSH_KNOWLEDGE_RESULT=" + json.dumps(
            {"status": "partial", "warnings": [], "matches": []}).encode() + b"\n"

        class FakeWorker:
            pid = 4321
            returncode = 0

            async def communicate(self, data=None):
                return result_line, b""

            def kill(self):
                pass

            async def wait(self):
                return 0

        async def fake_exec(*args, **kwargs):
            return FakeWorker()

        job = {"result": None}
        request = {"mode": "FILENAME_ONLY", "root": str(self._library()), "query": "q", "libraryId": "fixture"}
        with patch.object(service.asyncio, "create_subprocess_exec", fake_exec):
            await service.execute(job, request)
        warnings = job["result"]["warnings"]
        self.assertTrue(any(warning.startswith("ENGINE_ISOLATION: disabled") for warning in warnings), warnings)
        self.assertIn(service.ISOLATION_ENV, " ".join(warnings),
                      "the note names the variable that turned it off")
        self.assertFalse(any("AppContainer enforced" in warning for warning in warnings), warnings)
        self.assertFalse(any("pending" in warning for warning in warnings), warnings)

    def test_the_isolation_note_never_claims_more_than_it_can_support(self):
        # One function, two true sentences, and neither may be said when the other is true.
        plain = service.isolation_note(False)
        isolated = service.isolation_note(True)
        self.assertIn("disabled", plain)
        self.assertNotIn("AppContainer enforced", plain)
        self.assertIn("AppContainer enforced", isolated)
        self.assertNotIn("disabled", isolated)
        for note in (plain, isolated):
            self.assertNotIn("pending", note)
            self.assertTrue(note.startswith("ENGINE_ISOLATION: "))

    async def test_worker_spawn_enables_utf8_mode(self):
        captured = {}
        async def capture(*args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop before a real worker starts")
        job = {"result": None}
        with patch.object(service.asyncio, "create_subprocess_exec", capture):
            await service.execute(job, {"mode": "FAST", "root": str(self._library()),
                                        "query": "q", "libraryId": "fixture"})
        self.assertEqual(captured.get("env", {}).get("PYTHONUTF8"), "1",
                         "Sirchmunk mis-decodes non-ASCII sources unless the worker starts in UTF-8 mode")
        self.assertEqual(captured.get("env", {}).get("PYTHONIOENCODING"), "utf-8")

    async def test_worker_child_environment_normalises_the_proxy_list(self):
        captured = {}
        async def capture(*args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop before a real worker starts")
        with patch.dict(os.environ, {"NO_PROXY": "localhost,[::1]"}):
            with patch.object(service.asyncio, "create_subprocess_exec", capture):
                await service.execute({"result": None}, {"mode": "FAST", "root": str(self._library()),
                                                          "query": "q", "libraryId": "fixture"})
        # Windows normalises environment names, so only the upper-case spelling is asserted here;
        # lower-case spellings are covered by the runtime unit tests.
        self.assertEqual(captured["env"]["NO_PROXY"], "localhost,::1")

    async def test_cancellation_kills_real_worker(self):
        original = asyncio.create_subprocess_exec
        async def sleeping_worker(*args, **kwargs):
            if args[0] == sys.executable:
                return await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
            return await original(*args, **kwargs)
        job = {"result":None}
        request = {"mode": "FAST", "root": str(self._library()), "query": "q", "libraryId": "fixture"}
        with patch.object(service.asyncio, "create_subprocess_exec", sleeping_worker):
            task = asyncio.create_task(service.execute(job, request))
            for _ in range(200):
                if job.get("process"):
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(job.get("process"))
            task.cancel()
            await asyncio.wait_for(task, 10)
        self.assertIsNotNone(job["process"].returncode)
        self.assertEqual(job["result"]["status"], "cancelled")

    async def _run_real_marker_worker(self, mode="FAST", budget="", cancel_after=None):
        """Start a real subprocess that writes a marker into the job directory, then blocks."""
        original = asyncio.create_subprocess_exec
        observed = {}
        library = self._library()
        cache = Path(tempfile.mkdtemp(prefix="svc-job-probe-"))
        self.addCleanup(shutil.rmtree, cache, True)
        request = {"mode": mode, "cacheRoot": str(cache), "root": str(library),
                   "query": "q", "libraryId": "fixture"}
        environment = {"DSH_KNOWLEDGE_BUDGET_FAST": budget} if budget else {}
        with patch.dict(os.environ, environment):

            async def marker_worker(*args, **kwargs):
                if args[0] != sys.executable:
                    return await original(*args, **kwargs)
                directories = list(cache.glob("job-*"))
                self.assertEqual(len(directories), 1, "the parent must create exactly one job directory")
                observed["dir"] = directories[0]
                script = ("import os, sys, time;"
                          "open(os.path.join(sys.argv[1], 'marker'), 'w').close();"
                          "time.sleep(60)")
                return await original(sys.executable, "-c", script, str(directories[0]), **kwargs)

            with patch.object(service.asyncio, "create_subprocess_exec", marker_worker):
                job = {"id": "real", "result": None}
                task = asyncio.create_task(service.execute(job, request))
                for _ in range(300):
                    if observed.get("dir") and (observed["dir"] / "marker").exists():
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue((observed["dir"] / "marker").is_file(),
                                "the worker really used the parent-owned directory")
                if cancel_after is not None:
                    await asyncio.sleep(cancel_after)
                    task.cancel()
                await asyncio.wait_for(task, 30)
        return job, observed["dir"]

    async def test_cancelled_worker_snapshot_is_removed_by_the_parent(self):
        job, work_dir = await self._run_real_marker_worker(cancel_after=0)
        self.assertEqual(job["result"]["status"], "cancelled")
        self.assertFalse(work_dir.exists(), "a killed worker must not leave its snapshot copy behind")
        self.assertTrue(job["workDirRemoved"])

    async def test_budget_expiry_removes_the_snapshot_and_reports_the_budget(self):
        job, work_dir = await self._run_real_marker_worker(budget="1")
        self.assertEqual(job["result"]["status"], "partial")
        self.assertEqual(job["result"]["warnings"], ["BUDGET_EXCEEDED"])
        self.assertFalse(work_dir.exists(), "budget kill must not leave its snapshot copy behind")
        self.assertTrue(job["workDirRemoved"])


if __name__ == "__main__":
    unittest.main()
