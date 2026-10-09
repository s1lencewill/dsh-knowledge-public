"""Offline contracts for the pinned upstream searcher and safe proxy diagnostics."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from sirchmunk_compat import (configure_retrieval, close_engine, expand_snippet_context,
                              _snippet_mode, _is_snippet_evidence)
from runtime import ModelProxyRejected, safe_error
from worker import candidates_from


class SnippetContextTests(unittest.TestCase):
    """One matched line per hit loses the line the sentence continues on.

    The real failure: the hit line ended "... interfacial hydrolysis contributes" and the next line
    began "only 1%.", so the number the question asked about was never inside the engine's window.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "extracted.txt"

    def write(self, lines):
        self.path.write_text("\n".join(lines), encoding="utf-8")
        return str(self.path)

    def test_the_continuation_line_that_carries_the_value_is_now_included(self):
        path = self.write(["header", "filler one", "filler two",
                           "observed uptake coefficients but show that interfacial hydrolysis contributes",
                           "only 1%. We find that hydrolysis is accelerated in the bulk phase.",
                           "more filler"])
        evidence = "[extracted.txt]\nobserved uptake coefficients but show that interfacial hydrolysis contributes"
        expanded = expand_snippet_context(path, evidence, 3000)
        self.assertIn("interfacial hydrolysis contributes", expanded)
        self.assertIn("only 1%", expanded, "the sentence's continuation is the whole point")

    def test_evidence_is_verbatim_file_text_under_the_unchanged_header(self):
        lines = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
        path = self.write(lines)
        expanded = expand_snippet_context(path, "[extracted.txt]\nbeta\n", 3000)
        self.assertTrue(expanded.startswith("[extracted.txt]\n"))
        body = [line for line in expanded.split("\n")[1:] if line]
        for line in body:
            self.assertIn(line, lines, "only real file lines may be returned")

    def test_scattered_hits_keep_separate_windows_in_document_order(self):
        lines = ["line %03d" % i for i in range(1, 41)]
        lines[4] = "hit alpha here"
        lines[34] = "hit beta here"
        lines[35] = "and its continuation"
        path = self.write(lines)
        expanded = expand_snippet_context(path, "[extracted.txt]\nhit alpha here\nhit beta here", 3000)
        self.assertIn("hit alpha here", expanded)
        self.assertIn("hit beta here", expanded)
        self.assertIn("and its continuation", expanded)
        self.assertLess(expanded.index("hit alpha here"), expanded.index("hit beta here"),
                        "windows come back in document order")
        self.assertIn("\n\n", expanded, "separate windows stay visibly separate")

    def test_the_byte_budget_is_respected_and_dense_regions_do_not_take_it_all(self):
        # One region with a very large window and one far-away single hit: the far hit must survive.
        lines = ["dense %03d" % i for i in range(1, 200)]
        lines[2] = "dense hit line"
        lines[150] = "sparse hit line"
        path = self.write(lines)
        evidence = "[extracted.txt]\ndense hit line\nsparse hit line"
        expanded = expand_snippet_context(path, evidence, 600)
        self.assertLessEqual(len(expanded), 600)
        self.assertIn("sparse hit line", expanded, "a dense region must not starve the other hit")

    def test_hits_arriving_out_of_document_order_still_get_their_own_windows(self):
        # The real window arrived as 76, 319, 14, 163: merging against the previous hit alone would
        # swallow the earlier window - and that window held the sentence's continuation.
        lines = ["line %03d" % i for i in range(1, 61)]
        lines[4] = "hit reported second but early in the file"
        lines[49] = "hit reported first but late in the file"
        lines[50] = "and the value it continues with"
        path = self.write(lines)
        evidence = ("[extracted.txt]\nhit reported first but late in the file"
                    "\nhit reported second but early in the file")
        expanded = expand_snippet_context(path, evidence, 3000)
        self.assertIn("hit reported second but early in the file", expanded,
                      "the earlier window must not be merged away")
        self.assertIn("and the value it continues with", expanded)
        self.assertIn("hit reported first but late in the file", expanded)
        self.assertLess(expanded.index("hit reported second but early in the file"),
                        expanded.index("hit reported first but late in the file"),
                        "windows are emitted in document order, not in match order")

    def test_only_the_fallback_shape_is_expanded(self):
        # With a line number present upstream builds its own context window; leave that decision alone.
        self.assertTrue(_snippet_mode([{"data": {"lines": {"text": "x"}}}]))
        self.assertFalse(_snippet_mode([{"data": {"line_number": 7}}]))
        self.assertFalse(_snippet_mode([{"data": {"line_number": 7}}, {"data": {}}]))

    def test_richer_upstream_evidence_is_left_alone(self):
        # Upstream also answers with the head of the file when the snippet evidence is thin. Rebuilding
        # that from located hits would throw away the context that makes the head useful, so only the
        # one-line-per-hit shape may be expanded.
        matches = [{"data": {"lines": {"text": "the one matched line"}}}]
        fallback = "[doc.md]\nthe one matched line"
        head = "[doc.md]\n" + "\n".join("head line %02d" % index for index in range(20))
        self.assertTrue(_is_snippet_evidence(fallback, matches))
        self.assertFalse(_is_snippet_evidence(head, matches))

    def test_an_unlocatable_hit_leaves_the_evidence_untouched(self):
        path = self.write(["alpha", "beta"])
        evidence = "[extracted.txt]\nnothing in this file matches"
        self.assertEqual(expand_snippet_context(path, evidence, 3000), evidence)

    def test_a_missing_file_leaves_the_evidence_untouched(self):
        evidence = "[gone.txt]\nsome hit"
        self.assertEqual(expand_snippet_context(str(self.path) + ".missing", evidence, 3000), evidence)


class RetrievalCompatTests(unittest.TestCase):
    def test_real_fast_prompt_regression_with_large_multibyte_document(self):
        from sirchmunk import AgenticSearch
        from sirchmunk.llm.openai_chat import OpenAIChatResponse
        class PromptCaptured(Exception):
            pass
        sizes = []
        class OfflineLLM:
            _model = "offline"
            _client = types.SimpleNamespace(close=lambda: None)
            _async_client = types.SimpleNamespace(close=AsyncMock())
            def __init__(self):
                self.calls = 0
            async def achat(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return OpenAIChatResponse(content=json.dumps({"primary": ["marker"],
                        "fallback": ["marker"], "type": "search"}), usage={})
                sizes.append(len(json.dumps({"type": "chat", "id": "0" * 32,
                    "messages": kwargs["messages"], "options": {}}, ensure_ascii=False).encode("utf-8")))
                raise PromptCaptured
        async def exercise(root, fixed):
            llm = OfflineLLM()
            searcher = AgenticSearch(llm=llm, work_path=root / ("fixed" if fixed else "before"),
                                     reuse_knowledge=False, enable_knowledge_evolution=False, verbose=False)
            try:
                searcher._fast_find_best_file = AsyncMock(return_value=[{
                    "path": str(root / "source.txt"), "matches": [], "total_matches": 1}])
                searcher._fast_sample_evidence = AsyncMock(return_value="marker " + "中文" * 10000)
                if fixed:
                    configure_retrieval(searcher)
                with self.assertRaises(PromptCaptured):
                    await searcher._search_fast("marker", paths=[str(root / "source.txt")])
                if fixed:
                    searcher._fast_sample_evidence.assert_awaited_once()
            finally:
                await close_engine(searcher, llm)
        # Constructor auto-installs tools when PATH lacks them; this prompt test
        # mocks retrieval and must never exercise that network-dependent path.
        with tempfile.TemporaryDirectory() as directory, \
                patch("sirchmunk.search.check_dependencies", return_value=True), \
                patch("sirchmunk.search.install_rga", side_effect=AssertionError("offline test")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("offline test")):
            root = Path(directory)
            (root / "source.txt").write_text("marker " + "中文" * 10000, encoding="utf-8")
            asyncio.run(exercise(root, False))
            asyncio.run(exercise(root, True))
        self.assertGreater(sizes[0], 65536, "must reproduce the original defect")
        self.assertLess(sizes[1], 65536, "the real upstream prompt must fit")

    def test_real_upstream_shortcut_is_disabled_without_changing_other_instances(self):
        from sirchmunk import AgenticSearch
        searcher = object.__new__(AgenticSearch)
        original = AgenticSearch._try_direct_doc_analysis
        configure_retrieval(searcher)
        self.assertIsNone(asyncio.run(searcher._try_direct_doc_analysis("compare", ["snapshot"])))
        self.assertIs(AgenticSearch._try_direct_doc_analysis, original)
        self.assertEqual(AgenticSearch._FAST_MAX_EVIDENCE_CHARS, 40000)

    def test_missing_upstream_contract_is_a_hard_failure(self):
        with self.assertRaisesRegex(RuntimeError, "RETRIEVAL_CONTRACT_CHANGED"):
            configure_retrieval(object())

    def test_real_deep_pipeline_reaches_retrieval_instead_of_directory_answer(self):
        from sirchmunk import AgenticSearch
        searcher = object.__new__(AgenticSearch)
        class RetrievalReached(Exception):
            pass
        searcher._detect_compile_artifacts = lambda paths: None
        searcher._try_reuse_cluster = AsyncMock(side_effect=RetrievalReached)
        searcher._make_answer_cluster = lambda *a, **k: self.fail("directory answer shortcut used")
        configure_retrieval(searcher)
        with self.assertRaises(RetrievalReached):
            asyncio.run(searcher._search_deep("Compare the documents", paths=[str(Path.cwd())]))
        searcher._try_reuse_cluster.assert_awaited_once()

    def test_text_window_leaves_room_for_maximum_query_and_json_escaping(self):
        from sirchmunk import AgenticSearch
        from sirchmunk.llm.prompts import ROI_RESULT_SUMMARY
        searcher = object.__new__(AgenticSearch)
        configure_retrieval(searcher)
        # Control characters expand to six JSON bytes each, exceeding CJK/emoji.
        prompt = ROI_RESULT_SUMMARY.format(user_input="\x01" * 4000,
                                            text_content="\x01" * searcher._FAST_MAX_EVIDENCE_CHARS)
        request = {"type": "chat", "id": "0" * 32,
                   "messages": [{"role": "user", "content": prompt}], "options": {}}
        self.assertLess(len(json.dumps(request, ensure_ascii=False).encode("utf-8")), 65536)

    def test_whole_string_snippet_is_not_split_into_characters(self):
        candidates = candidates_from({"cluster": {"evidences": [
            {"file_or_url": "a.txt", "snippets": "原文 fragment"}]}})
        self.assertEqual(candidates[0]["snippets"], ["原文 fragment"])

    def test_bridge_error_preserves_only_allowlisted_code(self):
        import llm_bridge
        with patch.object(llm_bridge, "_PIPE_PATH", "unused"), \
                patch.object(llm_bridge.ipc, "connect", return_value=1), \
                patch.object(llm_bridge.ipc, "write_message"), \
                patch.object(llm_bridge.ipc, "read_message", return_value={
                    "type": "error", "code": "REQUEST_TOO_LARGE", "message": "secret/source/path"}):
            with self.assertRaises(ModelProxyRejected) as caught:
                llm_bridge.OpenAIChatBridge()._roundtrip([], None)
        self.assertEqual(safe_error(caught.exception), "REQUEST_TOO_LARGE")
        self.assertEqual(safe_error(ModelProxyRejected("secret")), "MODEL_PROXY_ERROR")
        self.assertEqual(safe_error(RuntimeError("secret")), "RuntimeError")
