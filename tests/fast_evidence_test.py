"""FAST evidence return: the engine's caching/answering decision must not decide what the parent sees.

Sirchmunk 0.1.0 ends FAST without a cluster in two places - the model answered but declined to cache
(`SHOULD_SAVE=false`), or it did not accept the evidence. Before the compat layer recovered the files
the engine had actually read, the first case handed the parent a cluster whose only "snippet" was the
model's own prose (unverifiable, so only a parent-side window could be quoted), and the second case
returned no candidates at all: the real trial produced `NO_ENGINE_CANDIDATES` from a file that had
already been found and read.

These tests drive the **real** FAST path - real rga retrieval, real evidence sampling, real
`SHOULD_ANSWER`/`SHOULD_SAVE` parsing, real worker candidates, real parent verification - and stub only
the chat call. No model is contacted and no token is spent.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))
import pipeline
import sirchmunk_compat
import worker

TEXT = ("# interface note\n"
        "N2O5 adsorption and solvation at the water interface are described here.\n"
        "The solvation free energy is -4.3 kcal/mol.\n"
        "Bulk phase dominates the hydrolysis reaction.\n")
#: Above `_FAST_SMALL_FILE_THRESHOLD` (3,000), so FAST uses keyword-centred sampling
#: (`_fast_sample_evidence`) rather than reading the file inline - the two evidence paths behave
#: differently and both are worth pinning.
LARGE_TEXT = TEXT + "".join(
    "filler line %04d about halide chemistry, interfaces and uptake coefficients.\n" % index
    for index in range(120))
ANALYSIS = json.dumps({"type": "search", "primary": ["N2O5 solvation"],
                       "fallback": ["N2O5", "solvation"], "idf": {}, "primary_alt": [],
                       "fallback_alt": [], "file_hints": [], "intent": "find the passage"})
NO_MATCH_ANALYSIS = json.dumps({"type": "search", "primary": ["zzzabsentzzz"],
                                "fallback": ["absentzzz"], "idf": {}, "primary_alt": [],
                                "fallback_alt": [], "file_hints": [], "intent": "nothing"})


def briefing(should_answer=True, should_save=True, precise="value", summary="body"):
    return (f"<SUMMARY>{summary}</SUMMARY><PRECISE_ANSWER>{precise}</PRECISE_ANSWER>"
            f"<SHOULD_ANSWER>{'true' if should_answer else 'false'}</SHOULD_ANSWER>"
            f"<SHOULD_SAVE>{'true' if should_save else 'false'}</SHOULD_SAVE>")


class _AsyncCloser:
    async def close(self):
        return None


class StubChat:
    prompts = []
    replies = []

    def __init__(self, **kwargs):
        self._client = types.SimpleNamespace(close=lambda: None)
        self._async_client = _AsyncCloser()
        self._model = kwargs.get("model") or "stub"

    async def achat(self, messages, **kwargs):
        prompt = messages[-1]["content"] if messages else ""
        StubChat.prompts.append(prompt)
        index = len(StubChat.prompts) - 1
        if index < len(StubChat.replies):
            content = StubChat.replies[index]
        elif StubChat.replies:
            content = StubChat.replies[-1]
        else:
            content = "No results found."
        return types.SimpleNamespace(content=content, usage={"total_tokens": 5}, role="assistant")


class FastEvidenceTests(unittest.TestCase):
    def setUp(self):
        # The real pinned tools must exist: this test drives real retrieval, not a stubbed one.
        base = Path(__file__).resolve().parents[1]
        rg = base / ".local/tools/rg.exe"
        rga = base / ".local/tools/rga-patched/rga.exe"
        if not (rg.is_file() and rga.is_file()):
            self.skipTest("pinned rg/rga are missing; run scripts/pin-tools.py")
        self.environment = patch.dict(os.environ, {
            # The chat call is stubbed, so these only satisfy the engine's configuration check.
            "LLM_BASE_URL": "https://stub.invalid/v1", "LLM_MODEL_NAME": "stub-model",
            "LLM_API_KEY": "stub-not-used",
            "DSH_KNOWLEDGE_RG": str(rg), "DSH_KNOWLEDGE_RGA": str(rga),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.texts = Path(self.temp.name) / "texts"
        self.texts.mkdir()
        (self.texts / "interface.md").write_text(TEXT, encoding="utf-8")
        # A second library whose file is large enough to take the sampling path.
        self.large = Path(self.temp.name) / "large"
        self.large.mkdir()
        (self.large / "interface-large.md").write_text(LARGE_TEXT, encoding="utf-8")

    def run_case(self, query, analysis, summaries, texts=None):
        """Drive the real FAST path with a stub chat and return what each layer produced."""
        texts = texts or self.texts
        work = Path(tempfile.mkdtemp(prefix="fast-evidence-"))
        manifest, _ = pipeline.build_snapshot(str(texts), work)
        request = pipeline.worker_request({"mode": "FAST", "query": query, "libraryId": "t",
                                           "cacheRoot": str(work.parent), "workDir": str(work)}, work)
        request["mode"] = "FAST"
        StubChat.prompts, StubChat.replies = [], [analysis] + list(summaries)
        holder = {}
        original_configure = worker.configure_retrieval

        def recording_configure(searcher):
            installed = original_configure(searcher)
            original_search = searcher.search

            async def recording_search(*args, **kwargs):
                result = await original_search(*args, **kwargs)
                holder["context"] = result
                return result

            searcher.search = recording_search
            return installed

        import sirchmunk.llm as llm_module
        real_chat = llm_module.OpenAIChat
        llm_module.OpenAIChat = StubChat
        worker.configure_retrieval = recording_configure
        try:
            result = asyncio.run(worker.run(request))
        finally:
            worker.configure_retrieval = original_configure
            llm_module.OpenAIChat = real_chat
        context = holder.get("context") if isinstance(holder.get("context"), dict) else {}
        evidence, _ = pipeline.verify_candidates(manifest, result.get("candidates"), query, "t")
        return {"result": result, "context": context, "evidence": evidence, "manifest": manifest,
                "work": work}

    def test_the_cache_decision_does_not_decide_what_the_parent_can_verify(self):
        # A large file takes the sampling path, so the engine's own verbatim text reaches the parent.
        facts = self.run_case("N2O5 adsorption and solvation in water", ANALYSIS,
                              [briefing(True, False)], texts=self.large)
        self.assertEqual(len(facts["result"]["candidates"]), 1, "the read file must reach the parent")
        self.assertTrue(any("ENGINE_CACHE_SKIPPED_BY_MODEL" in warning
                            for warning in facts["result"]["warnings"]), facts["result"]["warnings"])
        self.assertIn(facts["evidence"][0]["excerptSource"], ("engine", "engine-fragment"),
                      "the engine's sampled text verifies verbatim instead of model prose")
        self.assertTrue(facts["evidence"][0]["excerpt"].strip() in LARGE_TEXT)

    def test_a_small_file_still_yields_a_marked_parent_window(self):
        # Under the inline-read threshold the engine hands over the file without a snippet: the parent
        # quotes its own window and says so, rather than repeating model prose.
        facts = self.run_case("N2O5 adsorption and solvation in water", ANALYSIS,
                              [briefing(True, False)])
        self.assertEqual(len(facts["result"]["candidates"]), 1)
        self.assertEqual([item["excerptSource"] for item in facts["evidence"]], ["source-window"])
        self.assertTrue(facts["evidence"][0]["excerpt"].strip() in TEXT)

    def test_evidence_the_engine_rejects_still_reaches_the_parent_with_a_warning(self):
        # The query's own terms are absent from the sampled window, so the acceptance heuristic
        # rejects and upstream returns the no-results sentinel with no cluster - the real failure.
        facts = self.run_case("tell me about phonon entanglement in crystals", ANALYSIS,
                              [briefing(False, False), briefing(False, False)])
        self.assertEqual(facts["context"].get("answer"), "No results found.")
        self.assertEqual(len(facts["result"]["candidates"]), 1)
        self.assertTrue(any("ENGINE_REJECTED_EVIDENCE" in warning
                            for warning in facts["result"]["warnings"]), facts["result"]["warnings"])
        self.assertTrue(facts["evidence"], "the parent can still locate what was read")

    def test_the_engine_never_claims_acceptance_it_did_not_give(self):
        facts = self.run_case("N2O5 adsorption and solvation in water", ANALYSIS,
                              [briefing(False, False)])
        self.assertEqual(facts["result"]["answer"], "", "the worker never forwards an engine answer")
        self.assertTrue(facts["result"]["status"] == "partial")
        self.assertNotIn("COMPLETE", " ".join(facts["result"]["warnings"]))

    def test_a_true_no_match_is_still_reported_as_no_candidates(self):
        facts = self.run_case("N2O5 adsorption and solvation in water", NO_MATCH_ANALYSIS, [])
        self.assertEqual(facts["result"]["candidates"], [])
        self.assertEqual(facts["result"]["warnings"], ["NO_ENGINE_CANDIDATES"])
        self.assertEqual(facts["evidence"], [])

    def test_candidates_come_only_from_files_this_run_read(self):
        facts = self.run_case("N2O5 adsorption and solvation in water", ANALYSIS,
                              [briefing(True, False)])
        read = {str(path) for path in (facts["context"].get("read_file_ids") or [])}
        self.assertTrue(read, "the run must have read something for this test to mean anything")
        for candidate in facts["result"]["candidates"]:
            self.assertIn(str(Path(candidate["file"]).resolve()), {str(Path(p).resolve()) for p in read},
                          "a candidate must be a file this run actually read")

    def test_a_fabricated_path_never_becomes_a_candidate(self):
        facts = self.run_case("N2O5 adsorption and solvation in water", ANALYSIS,
                              [briefing(True, False, precise="see /nonexistent/fabricated.md")])
        files = [candidate["file"] for candidate in facts["result"]["candidates"]]
        self.assertTrue(files)
        for path in files:
            self.assertNotIn("fabricated", path)
            self.assertIn(str(Path(path).resolve()), facts["manifest"],
                          "every candidate must be a snapshot file the parent can verify")

    def test_model_prose_is_never_offered_as_a_verbatim_snippet(self):
        facts = self.run_case("N2O5 adsorption and solvation in water", ANALYSIS,
                              [briefing(True, False, precise="a phrase that is not in any source")])
        for candidate in facts["result"]["candidates"]:
            for snippet in candidate["snippets"]:
                text = snippet if isinstance(snippet, str) else str(snippet)
                if text:
                    self.assertIn(text.strip()[:40], TEXT,
                                  "an unverifiable snippet must not be handed to the parent as evidence")

    def test_the_contract_guard_refuses_a_changed_upstream_shape(self):
        # The FAST method losing its pinned shape must refuse to be adapted, not silently skip.
        from sirchmunk.search import AgenticSearch
        installed = AgenticSearch.__dict__.get(sirchmunk_compat._FAST_EVIDENCE_FLAG)
        real_fast = AgenticSearch.__dict__["_search_fast"]
        searcher = AgenticSearch.__new__(AgenticSearch)
        try:
            AgenticSearch._dsh_fast_evidence_installed = False
            AgenticSearch._search_fast = lambda self, query: None  # wrong signature, not a coroutine
            with self.assertRaisesRegex(RuntimeError, "FAST evidence contract"):
                sirchmunk_compat.install_fast_evidence_return(searcher)
        finally:
            AgenticSearch._search_fast = real_fast
            AgenticSearch._dsh_fast_evidence_installed = installed

    def test_a_double_that_is_not_the_real_engine_is_left_alone(self):
        fake = types.SimpleNamespace()
        self.assertEqual(sirchmunk_compat.install_fast_evidence_return(fake),
                         "skipped:not-the-real-engine")


if __name__ == "__main__":
    unittest.main()
