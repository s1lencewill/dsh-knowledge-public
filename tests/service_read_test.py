"""`knowledge_read` over HTTP: authorization, identifier lifetime, limits and information completion.

Everything here is offline: the query that produces a citation is driven by a stub executor that still
runs the *real* snapshot, the real citation verification and the real handle issuance, and no model
client, engine worker or container is started. No test in this file proves retrieval quality.
"""
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
os.environ.setdefault("DSH_KNOWLEDGE_TOKEN", "synthetic-token-for-service-test")
# These tests use the plain path; isolation has its own file.
os.environ["DSH_KNOWLEDGE_ISOLATION"] = "off"
from fastapi.testclient import TestClient
import evidence
import pipeline
import reader
import real_data
import service

#: Value and unit on separate lines, a range whose lower and upper bounds are split, a negation that
#: continues onto the next line, and a caption a search snippet can cut off.
SAMPLE = ("样品 A 的测量值\n"
          "数值 3.0\n"
          "单位 M/atm\n"
          "区间 10-40\n"
          "nm\n"
          "结论：界面水解并不主导，\n"
          "而是体相过程主导。\n"
          "Figure 1. Products identified: N2O5, HNO3,\n"
          "ClNO2 and HCl.\n"
          "无答案行：本文没有报告 pH 依赖。\n")
CAPTION_REST = "ClNO2 and HCl."


class ReadHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.texts = self.workspace / "texts"
        self.trial_texts = self.workspace / "trial"
        self.texts.mkdir()
        self.trial_texts.mkdir()
        (self.texts / "a.txt").write_text(SAMPLE, encoding="utf-8")
        (self.texts / "long.txt").write_text("短行\n" + "长" * 900 + "\n尾行\n", encoding="utf-8")
        (self.texts / "empty.txt").write_text("", encoding="utf-8")
        (self.trial_texts / "t.txt").write_text("真实资料样本一行\n第二行\n", encoding="utf-8")
        self.other_workspace = Path(tempfile.mkdtemp(prefix="read-other-ws-"))
        self.addCleanup(shutil.rmtree, self.other_workspace, True)
        self.other_texts = self.other_workspace / "texts"
        self.other_texts.mkdir()
        (self.other_texts / "a.txt").write_text("另一个 workspace 的内容\n", encoding="utf-8")
        self.config = self.workspace / "config.json"
        self.write_config()
        self.patch_config = patch.object(service, "CONFIG", self.config)
        self.patch_config.start()
        self.addCleanup(self.patch_config.stop)
        self.patch_jobs = patch.object(service, "JOBS", {})
        self.patch_jobs.start()
        self.addCleanup(self.patch_jobs.stop)
        self.patch_sessions = patch.object(service, "READ_SESSIONS", reader.Sessions())
        self.patch_sessions.start()
        self.addCleanup(self.patch_sessions.stop)
        self.client = TestClient(service.app)
        self.auth = {"Authorization": "Bearer " + service.TOKEN}

    # --- fixtures -------------------------------------------------------------------------------

    def trial_files(self):
        return {name.name: real_data.sha256(name) for name in sorted(self.trial_texts.iterdir())}

    def write_config(self, trial=True):
        trial_library = {"id": "trial", "name": "trial", "workspace": str(self.workspace),
                         "root": str(self.trial_texts), "syntheticFixture": False}
        if trial:
            trial_library["realDataTrial"] = {"authorized": True, "files": self.trial_files()}
        self.config.write_text(json.dumps({"libraries": [
            {"id": "fixture", "name": "fixture", "workspace": str(self.workspace),
             "root": str(self.texts), "syntheticFixture": True},
            trial_library,
            {"id": "remote", "name": "remote", "workspace": str(self.other_workspace),
             "root": str(self.other_texts), "syntheticFixture": True},
        ]}), encoding="utf-8")

    def handle(self, name="a.txt", library="fixture", workspace=None, root=None, version=None):
        root = Path(root or (self.texts if library == "fixture" else self.trial_texts))
        data = (root / name).read_bytes()
        return reader.issue_handle(reader.derive_secret(service.TOKEN), str(workspace or self.workspace),
                                   library, name, version or evidence.digest(data))

    def read(self, **body):
        payload = {"workspace": str(self.workspace), "libraryId": "fixture"}
        payload.update(body)
        return self.client.post("/reads", json=payload, headers=self.auth)

    def ok(self, **body):
        response = self.read(**body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # --- how the window ended: the three cases, over the wire --------------------------------

    def test_a_window_with_more_content_after_it_is_reported_as_truncated(self):
        window = self.ok(fileHandle=self.handle(), startLine=1, maxLines=2)
        self.assertTrue(window["hasMore"], "the sample has more lines than this window")
        self.assertFalse(window["excerptComplete"], "and it stops before a sentence boundary")
        self.assertTrue(any(w.startswith("EXCERPT_TRUNCATED") for w in window["warnings"]), window["warnings"])
        self.assertFalse(any(w.startswith("EXCERPT_INCOMPLETE_AT_EOF") for w in window["warnings"]), window["warnings"])
        card = window["sourceCard"]
        self.assertTrue(card["truncated"], "a window cut while the file continues IS a truncation")
        self.assertIn("EXCERPT_TRUNCATED", card["truncationNotices"], card["truncationNotices"])
        self.assertNotIn("EXCERPT_INCOMPLETE_AT_EOF", card["truncationNotices"])

    def test_a_window_at_the_end_of_the_file_is_not_reported_as_truncated(self):
        # The recorded session's third defect: line 423 of 423, `hasMore: false`, no cursor, and a warning
        # telling the model to read on with one. Over the wire the three claims must now be distinct: the
        # excerpt is incomplete *as text*, the window is not truncated, and nothing advises another read.
        window = self.ok(fileHandle=self.handle())
        self.assertFalse(window["hasMore"])
        self.assertIsNone(window.get("nextCursor"))
        self.assertFalse(window["excerptComplete"], "the sample ends in a full-width stop, which the check ignores")
        notices = [w.split(":")[0] for w in window["warnings"]]
        self.assertIn("EXCERPT_INCOMPLETE_AT_EOF", notices)
        self.assertNotIn("EXCERPT_TRUNCATED", notices)
        advice = " ".join(window["warnings"])
        self.assertNotIn("read on", advice)
        self.assertNotIn("nextCursor", advice, "the wording must not name a field the plugin renames")
        card = window["sourceCard"]
        self.assertFalse(card["truncated"], "the text ends here; the window is not a truncated one")
        self.assertEqual(card["truncationNotices"], ["EXCERPT_INCOMPLETE_AT_EOF"])

    def test_a_file_that_ends_a_sentence_carries_no_such_notice(self):
        (self.texts / "closed.txt").write_text("first line ends here.\nsecond line does too.\n", encoding="utf-8")
        window = self.ok(fileHandle=self.handle(name="closed.txt"))
        self.assertFalse(window["hasMore"])
        self.assertTrue(window["excerptComplete"])
        self.assertFalse(any(w.startswith(("EXCERPT_TRUNCATED", "EXCERPT_INCOMPLETE_AT_EOF")) for w in window["warnings"]),
                         window["warnings"])
        self.assertFalse(window["sourceCard"]["truncated"])
        self.assertEqual(window["sourceCard"]["truncationNotices"], [])

    def refusal(self, status, code, **body):
        response = self.read(**body)
        self.assertEqual(response.status_code, status, response.text)
        self.assertIn(code, response.text)
        return response

    def install_search_stub(self, snippet=None):
        """A search that exercises the real snapshot + verification + handle issuance, with no engine.

        It also opens the real per-job diagnostics sink, the same way `service.execute` does, so the
        tests can assert what a completed query does and does not record.
        """
        async def finish(job, request):
            work_dir = Path(request["cacheRoot"]) / ("job-" + str(job["id"]))
            work_dir.mkdir(parents=True, exist_ok=True)
            diag = service.stage_diagnostics.open_for_job(job.get("id"), enabled=service.diagnostics_enabled(),
                                                          mode=request["mode"], libraryId=request.get("libraryId"))
            try:
                manifest, warnings = pipeline.build_snapshot(request["root"], work_dir)
                relative = "a.txt"
                copy = work_dir / "files" / relative
                text = snippet if snippet is not None else "\n".join(SAMPLE.split("\n")[:3])
                job["result"] = pipeline.finalize(
                    {"status": "partial", "answer": "", "warnings": [],
                     "candidates": [{"file": str(copy.resolve()), "snippets": [text]}]},
                    manifest, request, warnings, diag, service.read_handles(request))
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
        return patch.object(service, "execute", finish)

    def search(self, snippet=None, query="q"):
        # The session is part of the scope the route now requires, and the same scope has to be named on every
        # recovery call - one session cannot reach another's job.
        session = "session-read"
        owner = {"ownerWorkspace": str(self.workspace), "ownerLibrary": "fixture", "ownerSession": session}
        with self.install_search_stub(snippet):
            submitted = self.client.post("/queries", json={"workspace": str(self.workspace),
                                                           "libraryId": "fixture", "query": query,
                                                           "session": session}, headers=self.auth)
            self.assertEqual(submitted.status_code, 200, submitted.text)
            identity = submitted.json()["id"]
            for _ in range(200):
                # The state payload the route now returns carries the lifecycle as well as the result: `state`
                # is what a client polls, and `result` is present exactly when the job reached a terminal one.
                body = self.client.get("/queries/" + identity, params=owner, headers=self.auth).json()
                result = body["result"]
                if result is not None:
                    self.assertEqual(body["state"] in ("completed", "failed", "cancelled"), True,
                                     "a result is only ever returned for a terminal state")
                    self.assertTrue(body["terminal"])
                    break
                self.assertIn(body["state"], ("received", "accepted", "running", "cancelling"))
                time.sleep(0.01)
        self.assertIsNotNone(result, "the stub search must finish")
        return identity, result

    # --- authorization --------------------------------------------------------------------------

    def test_a_read_requires_authentication(self):
        response = self.client.post("/reads", json={"workspace": str(self.workspace), "libraryId": "fixture",
                                                    "fileHandle": self.handle()})
        self.assertEqual(response.status_code, 401)

    def test_the_library_must_still_belong_to_the_calling_workspace(self):
        response = self.client.post("/reads", json={"workspace": str(self.other_workspace), "libraryId": "fixture",
                                                    "fileHandle": self.handle()}, headers=self.auth)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Unknown library for workspace", response.text)

    def test_a_handle_is_bound_to_its_workspace_and_library(self):
        self.refusal(403, reader.READ_HANDLE_FOREIGN, libraryId="remote", workspace=str(self.other_workspace),
                     fileHandle=self.handle(root=self.other_texts, workspace=str(self.other_workspace)))
        self.refusal(403, reader.READ_HANDLE_FOREIGN, fileHandle=self.handle(name="t.txt", library="trial",
                                                                            root=self.trial_texts))

    def test_a_forged_or_rotated_identifier_is_refused(self):
        handle = self.handle()
        prefix, body, signature = handle.split(".")
        self.refusal(404, reader.READ_HANDLE_INVALID,
                     fileHandle="%s.%s.%s" % (prefix, body, signature[:-1] + ("A" if signature[-1] != "A" else "B")))
        self.refusal(404, reader.READ_HANDLE_INVALID, fileHandle="krh1.not.a.handle")
        # A handle minted before a token rotation no longer verifies, while the live token still does.
        rotated = reader.issue_handle(reader.derive_secret("a-rotated-service-token-value"), str(self.workspace),
                                      "fixture", "a.txt", evidence.digest((self.texts / "a.txt").read_bytes()))
        self.refusal(404, reader.READ_HANDLE_INVALID, fileHandle=rotated)
        self.ok(fileHandle=handle)

    def test_a_path_shaped_argument_is_not_part_of_the_interface(self):
        response = self.client.post("/reads", json={"workspace": str(self.workspace), "libraryId": "fixture",
                                                    "path": "a.txt"}, headers=self.auth)
        # Extra fields are ignored, and no path can stand in for the identifier.
        self.assertIn(response.status_code, (400, 422), response.text)

    # --- lifecycle ------------------------------------------------------------------------------

    def test_a_citation_can_be_read_after_its_query_is_acknowledged(self):
        identity, result = self.search()
        self.assertEqual(result["status"], "partial")
        item = result["evidence"][0]
        self.assertIn("fileHandle", item, "a citation carries the identifier the read needs")
        self.assertEqual(item["relativePath"], "a.txt")
        acknowledged = self.client.request(
            "DELETE", "/queries/" + identity, headers=self.auth,
            json={"workspace": str(self.workspace), "libraryId": "fixture", "session": "session-read"}).json()
        # Settling a finished job releases its slot and hands the result back one last time - it never
        # rewrites it as "cancelled", which is what "the client has read this" has to mean.
        self.assertFalse(acknowledged["cancellationConfirmed"])
        self.assertTrue(acknowledged["settled"])
        self.assertEqual(acknowledged["result"]["status"], "partial")
        self.assertNotIn(identity, service.JOBS, "the settled job releases its capacity")
        window = self.ok(fileHandle=item["fileHandle"], sourceVersion=item["sourceVersion"], startLine=item["startLine"])
        self.assertIn("数值 3.0", window["excerpt"])
        self.assertEqual(window["sourceVersion"], item["sourceVersion"])

    def test_confirmation_and_cancellation_are_different_answers_for_the_same_job(self):
        """`POST /acknowledge` confirms; the same route with `cancelIntent` stops. One job, two facts."""
        identity, result = self.search()
        owner = {"workspace": str(self.workspace), "libraryId": "fixture", "session": "session-read"}
        settled = self.client.post("/queries/" + identity + "/acknowledge", json=owner, headers=self.auth)
        self.assertEqual(settled.status_code, 200, settled.text)
        body = settled.json()
        self.assertTrue(body["acknowledged"])
        self.assertTrue(body["released"])
        self.assertEqual(body["result"]["status"], "partial",
                         "confirmation reports the result the client read, not a cancellation")
        again = self.client.post("/queries/" + identity + "/acknowledge", json=owner, headers=self.auth).json()
        self.assertTrue(again["alreadyRemoved"])

    def test_identifiers_survive_a_service_restart_with_the_same_token(self):
        handle = self.handle()
        service.JOBS.clear()
        with patch.object(service, "READ_SESSIONS", reader.Sessions()):
            window = self.ok(fileHandle=handle)
        self.assertEqual(window["excerpt"].split("\n")[0], "样品 A 的测量值")

    def test_a_read_does_not_start_a_worker_or_build_a_model_client(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("a read must not construct a model client or start the engine")

        with patch.object(service.model_proxy, "provider_from_environment", forbidden), \
             patch.object(service, "run_isolated_async", forbidden), \
             patch.object(service.asyncio, "create_subprocess_exec", forbidden):
            window = self.ok(fileHandle=self.handle())
        self.assertIn("样品 A", window["excerpt"])

    # --- consistency ----------------------------------------------------------------------------

    def test_a_changed_file_demands_a_new_search(self):
        handle = self.handle()
        (self.texts / "a.txt").write_text("已被替换的正文\n", encoding="utf-8")
        response = self.refusal(409, reader.SOURCE_VERSION_CHANGED, fileHandle=handle)
        self.assertIn("knowledge_search", response.text)

    def test_a_deleted_or_replaced_file_is_refused(self):
        handle = self.handle()
        (self.texts / "a.txt").unlink()
        self.refusal(404, reader.READ_FILE_UNAVAILABLE, fileHandle=handle)
        (self.texts / "a.txt").write_text(SAMPLE + "附加行\n", encoding="utf-8")
        self.refusal(409, reader.SOURCE_VERSION_CHANGED, fileHandle=handle)

    def test_a_version_field_that_disagrees_is_refused(self):
        self.refusal(400, reader.READ_SOURCE_VERSION_MISMATCH, fileHandle=self.handle(), sourceVersion="0" * 64)

    def test_the_real_data_trial_is_rechecked_on_every_read(self):
        handle = self.handle(name="t.txt", library="trial", root=self.trial_texts)
        window = self.ok(libraryId="trial", fileHandle=handle)
        self.assertTrue(any(w.startswith("REAL_DATA_TRIAL_LIBRARY") for w in window["warnings"]),
                        "a real-data read must be labelled as such")
        self.write_config(trial=False)
        self.refusal(403, real_data.NOT_AUTHORIZED, libraryId="trial", fileHandle=handle)
        self.write_config()
        (self.trial_texts / "extra.txt").write_text("新增文件\n", encoding="utf-8")
        self.refusal(403, real_data.FILES_CHANGED, libraryId="trial", fileHandle=handle)

    # --- limits ---------------------------------------------------------------------------------

    def test_the_single_read_ceiling_is_enforced(self):
        window = self.ok(fileHandle=self.handle(), maxChars=10 ** 9)
        self.assertLessEqual(len(window["excerpt"]), reader.MAX_READ_CHARS)
        self.assertLessEqual(window["endChar"] - window["startChar"], reader.MAX_READ_CHARS)

    def test_the_session_read_count_is_enforced_per_session(self):
        with patch.object(service, "READ_SESSIONS", reader.Sessions(max_reads=2)):
            handle = self.handle()
            for _ in range(2):
                self.ok(fileHandle=handle, session="one")
            self.refusal(429, reader.READ_LIMIT_EXCEEDED, fileHandle=handle, session="one")
            self.ok(fileHandle=handle, session="two")

    def test_the_session_read_budget_can_be_pinned_by_configuration(self):
        # A trial window pins the cap; that configures the existing limit rather than adding one, and
        # the refusal still comes from the service, not from the prompt.
        with patch.dict(os.environ, {service.READ_MAX_PER_SESSION_ENV: "1", service.READ_MAX_CHARS_ENV: "5000"}):
            self.assertEqual(service.read_max_per_session(), 1)
            self.assertEqual(service.read_max_chars(), 5000)
            pinned = reader.Sessions(service.read_max_per_session(), service.read_max_chars())
            with patch.object(service, "READ_SESSIONS", pinned):
                handle = self.handle()
                self.ok(fileHandle=handle, session="pinned")
                self.refusal(429, reader.READ_LIMIT_EXCEEDED, fileHandle=handle, session="pinned")
        for invalid in ("0", "-3", "soon", ""):
            with patch.dict(os.environ, {service.READ_MAX_PER_SESSION_ENV: invalid}):
                self.assertEqual(service.read_max_per_session(), reader.MAX_READS_PER_SESSION, invalid)

    def test_health_reports_the_effective_read_limits(self):
        body = self.client.get("/health", headers=self.auth).json()
        self.assertEqual(body["readLimits"]["perSession"], service.read_max_per_session())
        self.assertEqual(body["readLimits"]["sessionChars"], service.read_max_chars())
        self.assertEqual(body["readLimits"]["perResponse"], reader.MAX_READ_CHARS)

    def test_neither_the_query_nor_the_read_path_records_a_whole_identifier(self):
        sink = Path(tempfile.mkdtemp(prefix="read-diagnostics-"))
        self.addCleanup(shutil.rmtree, sink, True)
        with patch.object(service.stage_diagnostics, "DEFAULT_DIR", sink), \
             patch.object(service, "diagnostics_enabled", lambda: True):
            identity, result = self.search(snippet="数值 3.0\n单位 M/atm")
            item = result["evidence"][0]
            self.assertIn("fileHandle", item)
            self.ok(fileHandle=item["fileHandle"], session="diagnostics")
            self.client.delete("/queries/" + identity, headers=self.auth)
            written = "".join(path.read_text(encoding="utf-8") for path in sorted(sink.iterdir()))
        self.assertTrue(written.strip(), "the query must have written a diagnostics record")
        self.assertNotIn("krh1.", written, "the diagnostics must not carry the whole identifier")
        self.assertNotIn("krc1.", written)
        self.assertNotIn(item["fileHandle"], written)
        self.assertNotIn("a.txt", written, "and not the recorded file name either")

    def test_the_session_character_budget_shrinks_a_window_and_then_refuses(self):
        with patch.object(service, "READ_SESSIONS", reader.Sessions(max_reads=50, max_chars=250, min_chars=100)):
            handle = self.handle()
            first = self.ok(fileHandle=handle, maxChars=6000, session="budget")
            self.assertTrue(any(w.startswith("READ_SESSION_BUDGET") for w in first["warnings"]))
            self.assertLessEqual(first["limits"]["sessionCharsUsed"], 250)
            for _ in range(10):
                response = self.read(fileHandle=handle, maxChars=6000, session="budget")
                if response.status_code == 429:
                    break
                self.assertEqual(response.status_code, 200, response.text)
                self.assertLessEqual(response.json()["limits"]["sessionCharsUsed"], 250, response.text)
            self.assertEqual(response.status_code, 429, response.text)
            self.assertIn(reader.READ_LIMIT_EXCEEDED, response.text)
            self.ok(fileHandle=handle, session="another")

    def test_out_of_range_requests_are_refused_or_clamped(self):
        for body in ({"startLine": 0}, {"maxChars": 0}, {"maxLines": 0}, {"overlapLines": -1}):
            response = self.read(fileHandle=self.handle(), **body)
            self.assertEqual(response.status_code, 422, body)
        self.refusal(400, reader.READ_RANGE_INVALID, fileHandle=self.handle(), startLine=9999)
        window = self.ok(fileHandle=self.handle(), maxLines=2)
        self.assertEqual(window["excerpt"].count("\n") + 1, 2)

    def test_a_missing_identifier_is_refused(self):
        self.refusal(400, reader.READ_REQUEST_INVALID)
        # A present but unusable identifier is a different failure: nothing can be resolved from it.
        self.refusal(404, reader.READ_HANDLE_INVALID, fileHandle=" ")

    # --- the structured refusal both generations of caller read ----------------------------------

    def test_a_range_refusal_carries_both_the_legacy_detail_and_the_structured_body(self):
        response = self.read(fileHandle=self.handle(), startLine=9999)
        self.assertEqual(response.status_code, 400, response.text)
        body = response.json()
        # The legacy half: an older plugin parses exactly this string and still recognises a refusal.
        self.assertIsInstance(body["detail"], str)
        self.assertTrue(body["detail"].startswith(reader.READ_RANGE_INVALID + ":"), body["detail"])
        # The structured half: closed-set tokens, integers and the correlation id, and nothing else.
        error = body["error"]
        self.assertEqual(set(error), {"code", "reason", "requestedStartLine", "fileLines",
                                      "suggestedAction", "correlationId"}, error)
        self.assertEqual(error["code"], reader.READ_RANGE_INVALID)
        self.assertEqual(error["reason"], reader.READ_REASON_START_LINE_PAST_END)
        self.assertEqual(error["requestedStartLine"], 9999)
        self.assertEqual(error["fileLines"], len(SAMPLE.split("\n")) - 1, "the file's real line count")
        self.assertEqual(error["suggestedAction"], reader.READ_ACTION_CORRECT_START_LINE)
        self.assertRegex(error["correlationId"], r"^k-[0-9a-z]{8}$",
                         "the id must match the shape the plugin validates before it displays one")
        # The prose the service sends is its own fixed sentence: no path, no identifier, no exception text.
        self.assertNotIn(str(self.texts), body["detail"])

    def test_a_refusal_writes_one_structured_log_line_naming_its_correlation_id(self):
        import contextlib
        import io
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            body = self.read(fileHandle=self.handle(), startLine=9999).json()
        lines = [line for line in captured.getvalue().splitlines() if line.startswith("READ_REFUSAL ")]
        self.assertEqual(len(lines), 1, captured.getvalue())
        logged = json.loads(lines[0][len("READ_REFUSAL "):])
        self.assertEqual(logged["correlationId"], body["error"]["correlationId"],
                         "the log line and the response must name the same refusal")
        self.assertEqual(logged["reason"], reader.READ_REASON_START_LINE_PAST_END)
        self.assertEqual(logged["fileLines"], body["error"]["fileLines"])
        self.assertNotIn("a.txt", lines[0], "the log line must not name a file")
        self.assertNotIn(str(self.texts), lines[0])

    def test_a_bad_parameter_type_is_refused_without_echoing_what_was_sent(self):
        # FastAPI's default 422 body repeats the offending value. A read must not: the same closed-set shape
        # is used instead, naming the parameter but never the input.
        response = self.read(fileHandle=self.handle(), startLine="not-a-line")
        self.assertEqual(response.status_code, 422, response.text)
        body = response.json()
        self.assertIsInstance(body["detail"], str)
        self.assertNotIn("not-a-line", response.text, "the offending value must not be echoed")
        error = body["error"]
        self.assertEqual(error["code"], reader.READ_REQUEST_INVALID)
        self.assertEqual(error["reason"], reader.READ_REASON_PARAMETER_TYPE_INVALID)
        self.assertEqual(error["parameter"], "startLine")
        self.assertEqual(error["suggestedAction"], reader.READ_ACTION_CORRECT_PARAMETER)
        self.assertIsNone(error["fileLines"], "no file was opened, so no line count may be reported")

    def test_another_route_keeps_the_host_frameworks_own_422(self):
        # The rewrite is scoped to `/reads`: only that route reads a library file, and the other routes'
        # contracts are pinned by their own tests.
        response = self.client.post("/documents", json={"workspace": str(self.workspace), "libraryId": "fixture",
                                                        "title": "x" * 400}, headers=self.auth)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIsInstance(response.json()["detail"], list, "the default body is a list of violations")

    def test_a_refusal_before_the_file_is_opened_reports_no_line_count(self):
        # The leak boundary, over the wire: nowhere in this response may a line count appear.
        response = self.read()  # no identifier at all
        self.assertEqual(response.status_code, 400, response.text)
        error = response.json()["error"]
        self.assertEqual(error["reason"], reader.READ_REASON_IDENTIFIER_MISSING)
        self.assertIsNone(error["fileLines"])
        self.assertIsNone(error["requestedStartLine"])
        foreign = self.read(fileHandle=self.handle(name="a.txt"), libraryId="remote")
        self.assertEqual(foreign.status_code, 403, foreign.text)
        self.assertNotIn("error", foreign.json(), "an authorization refusal is not a request-shape refusal")

    def test_the_facts_survive_a_hostile_start_line_value(self):
        # A value that is not a line number the caller could act on is refused as a request problem, and the
        # echoed field stays null. This is the case the old path turned into a bare "code: prose" with no
        # numbers at all. `"9" * 40` is the interesting one: the framework coerces a numeric string into a
        # Python int, so it arrives as a *valid* integer that no line count could ever be - the service drops
        # it rather than echoing forty digits, and the plugin would drop it too (it is not a safe integer).
        for value in ("", "1e9", [], {}, "9" * 40):
            response = self.read(fileHandle=self.handle(), startLine=value)
            self.assertIn(response.status_code, (400, 422), response.text)
            error = response.json()["error"]
            self.assertIsNone(error["requestedStartLine"], repr(value))
            self.assertNotIn("9999999999", response.text)
        # A number the caller *can* act on is still reported, so the message can name the bound it missed.
        response = self.read(fileHandle=self.handle(), startLine=9999)
        self.assertEqual(response.json()["error"]["requestedStartLine"], 9999)

    # --- information completion -----------------------------------------------------------------

    def test_the_next_line_carries_the_unit_the_citation_missed(self):
        window = self.ok(fileHandle=self.handle(), startLine=2, maxLines=4)
        self.assertIn("数值 3.0", window["excerpt"])
        self.assertIn("单位 M/atm", window["excerpt"])

    def test_a_range_split_across_lines_is_completed(self):
        window = self.ok(fileHandle=self.handle(), startLine=4, maxLines=3)
        self.assertIn("区间 10-40", window["excerpt"])
        self.assertIn("nm", window["excerpt"])

    def test_a_negation_that_continues_on_the_next_line_is_completed(self):
        window = self.ok(fileHandle=self.handle(), startLine=6, maxLines=3)
        self.assertIn("界面水解并不主导", window["excerpt"])
        self.assertIn("而是体相过程主导。", window["excerpt"])

    def test_a_caption_a_snippet_truncated_is_completed_from_the_original(self):
        truncated = "Figure 1. Products identified: N2O5, HNO3,"      # the snippet stops here
        identity, result = self.search(snippet=truncated)
        item = result["evidence"][0]
        self.assertNotIn(CAPTION_REST, item["excerpt"], "the citation itself is the truncated snippet")
        window = self.ok(fileHandle=item["fileHandle"], startLine=item["startLine"], maxLines=5)
        self.assertIn(CAPTION_REST, window["excerpt"])
        self.assertIn(window["excerpt"], SAMPLE, "the completion is verbatim file text, never assembled")
        self.client.delete("/queries/" + identity, headers=self.auth)

    def test_reading_cannot_manufacture_an_answer_the_file_does_not_have(self):
        # A file that simply does not discuss the fact being asked about.
        (self.texts / "noanswer.txt").write_text("本文件只描述颜色与尺寸。\n第一行描述颜色为蓝色。\n第二行描述尺寸为三厘米。\n",
                                                 encoding="utf-8")
        text = evidence.normalised_text((self.texts / "noanswer.txt").read_bytes())
        handle = self.handle(name="noanswer.txt")
        seen, cursor = [], None
        while True:
            window = self.ok(fileHandle=handle, cursor=cursor, maxChars=120)
            self.assertIn(window["excerpt"], text, "every window is a verbatim slice of the file")
            seen.append(window["excerpt"])
            cursor = window["nextCursor"]
            if cursor is None:
                break
        joined = "\n".join(seen)
        self.assertEqual(joined.replace("\n", ""), text.replace("\n", "").rstrip("\n"),
                         "the reads return exactly the file and nothing more")
        self.assertNotIn("pH", joined, "the file has no pH result, and no read invents one")
        self.assertIn("尺寸为三厘米", joined, "what the file does say is returned instead")

    def test_a_long_line_and_an_empty_file_report_their_state(self):
        long_window = self.ok(fileHandle=self.handle(name="long.txt"), maxChars=reader.MIN_READ_CHARS)
        self.assertTrue(long_window["midLineStop"])
        self.assertTrue(any(w.startswith("READ_LONG_LINE") for w in long_window["warnings"]))
        empty = self.ok(fileHandle=self.handle(name="empty.txt"))
        self.assertEqual(empty["excerpt"], "")
        self.assertFalse(empty["hasMore"])
        self.assertTrue(any(w.startswith("READ_EMPTY_FILE") for w in empty["warnings"]))

    def test_the_returned_offsets_rebuild_the_excerpt(self):
        window = self.ok(fileHandle=self.handle(), startLine=2, maxLines=3)
        text = evidence.normalised_text((self.texts / "a.txt").read_bytes())
        self.assertEqual(evidence.excerpt_from_offsets(text, window), window["excerpt"])
        self.assertEqual(window["excerptComplete"], pipeline._closes_a_sentence(window["excerpt"]))
        for field in ("libraryId", "relativePath", "fileHandle", "sourceVersion", "startLine", "endLine",
                      "startChar", "endChar", "startOffset", "endOffset", "excerpt", "excerptComplete",
                      "endsAtLineBoundary", "midLineStart", "midLineStop", "lineLimitReached",
                      "overlapsPrevious", "hasMore", "nextStartChar", "nextCursor", "fileChars",
                      "fileLines", "warnings", "limits"):
            self.assertIn(field, window)


if __name__ == "__main__":
    unittest.main()
