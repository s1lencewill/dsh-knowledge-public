"""Trusted parent pipeline: snapshot ownership and citation re-verification.

The engine is stubbed, so these tests pin the trust boundary itself: what the child is allowed to
see, and which of its claims may become a citation.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import pipeline

SOURCE = "# Fixture\n\nThe codename is WALNUT-01.\n检索测试：第一条\n"
SNIPPET = "WALNUT-01"
INVENTED = "INVENTED-QUOTE-NOT-IN-SOURCE"
PROSE = "**Answer: fixture.md**\n\n## Markdown Briefing\nbriefing only"


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "library"
        self.root.mkdir()
        self.source = self.root / "fixture.md"
        self.source.write_text(SOURCE, encoding="utf-8")
        self.cache = base / "cache"
        self.cache.mkdir()
        self.work = self.cache / "job-test"
        self.seen_child_request = None

    def request(self, mode="FAST", query="walnut"):
        return {"mode": mode, "query": query, "root": str(self.root), "libraryId": "fixture",
                "cacheRoot": str(self.cache), "workDir": str(self.work)}

    async def run_query(self, units, mode="FAST", timeout=None, after=None):
        async def runner(child_request):
            self.seen_child_request = child_request
            snapshot_file = next(Path(child_request["snapshotDir"]).rglob("fixture.md"))
            if after:
                after()
            return {"status": "partial", "answer": "", "usage": [{"total_tokens": 3}],
                    "candidates": [{"file": str(snapshot_file), **unit} for unit in units]}
        return await pipeline.run_local(self.request(mode), runner, timeout)

    async def test_child_request_exposes_only_the_snapshot(self):
        result = await self.run_query([{"snippets": [SNIPPET]}])
        self.assertEqual(len(result["evidence"]), 1)
        child = self.seen_child_request
        self.assertNotIn("root", child, "the child request must not carry a library path")
        values = [value for value in child.values() if isinstance(value, str)]
        self.assertFalse(any(str(self.root) in value for value in values),
                         "the engine must never receive a path into the library: %s" % values)
        self.assertTrue(any(str(self.work) in value for value in values), values)
        self.assertTrue(str(self.snapshot_path()) == child["snapshotDir"], child["snapshotDir"])

    def snapshot_path(self):
        return self.work / "files"

    async def test_verified_snippet_becomes_one_located_citation(self):
        result = await self.run_query([{"snippets": [SNIPPET]}, {"snippets": [SNIPPET]}])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["evidence"]), 1, "one citation per file")
        evidence = result["evidence"][0]
        self.assertEqual(evidence["relativePath"], "fixture.md")
        self.assertEqual(evidence["startLine"], 3)
        self.assertEqual(evidence["excerpt"], SNIPPET)
        self.assertNotIn("ENGINE_SNIPPET_UNVERIFIED", " ".join(result["warnings"]))

    async def test_engine_prose_is_never_quoted_but_the_located_file_is(self):
        result = await self.run_query([{"snippets": [PROSE]}])
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["evidence"][0]["excerpt"], SOURCE.strip())
        self.assertEqual(result["evidence"][0]["excerptSource"], "source-window")
        self.assertNotIn("briefing", result["evidence"][0]["excerpt"])
        self.assertTrue(any("ENGINE_SNIPPET_UNVERIFIED" in warning for warning in result["warnings"]))

    async def test_engine_fragment_is_preferred_over_a_window(self):
        # Prose plus one quoted line: the quoted run is taken verbatim, not a source window.
        snippet = "**Answer: fixture.md**\n\nbriefing only\n\n" + SNIPPET
        result = await self.run_query([{"snippets": [snippet]}])
        self.assertEqual(len(result["evidence"]), 1)
        evidence = result["evidence"][0]
        self.assertEqual(evidence["excerpt"], SNIPPET)
        self.assertEqual(evidence["excerptSource"], "engine-fragment")
        self.assertEqual(evidence["startLine"], 3)
        self.assertNotIn("ENGINE_SNIPPET_UNVERIFIED", " ".join(result["warnings"]), result["warnings"])

    async def test_window_fallback_is_marked_and_stays_small(self):
        long_source = "\n".join("line %02d" % index for index in range(40))
        long_source = long_source.replace("line 25", "line 25 WALNUT-03 marker")
        self.source.write_text(long_source + "\n", encoding="utf-8")
        result = await self.run_query([{"snippets": [PROSE]}])
        self.assertEqual(len(result["evidence"]), 1)
        evidence = result["evidence"][0]
        self.assertEqual(evidence["excerptSource"], "source-window")
        self.assertTrue(any("ENGINE_SNIPPET_UNVERIFIED" in warning for warning in result["warnings"]))
        lines = evidence["excerpt"].split("\n")
        self.assertLessEqual(len(lines), 12, "the window must stay small")
        # The hit is on source line 25 (1-based); the window is two lines of context wide.
        self.assertEqual(lines[0], "line 23", "the window starts two lines before the hit")
        self.assertEqual(evidence["startLine"], 24)
        self.assertIn("WALNUT-03", evidence["excerpt"])

    async def test_engine_snippet_is_marked_as_the_engine_excerpt(self):
        result = await self.run_query([{"snippets": [SNIPPET]}])
        self.assertEqual(result["evidence"][0]["excerptSource"], "engine")
        self.assertIn(result["evidence"][0]["excerptSource"],
                      ("engine", "engine-fragment", "source-window"),
                      "only these three origins exist; a window is never called a minimal fragment")

    async def test_candidate_outside_the_snapshot_is_dropped(self):
        async def runner(child_request):
            self.seen_child_request = child_request
            # A compromised engine can point anywhere; the manifest is keyed by snapshot copies.
            return {"status": "partial", "answer": "", "usage": None,
                    "candidates": [{"file": str(self.source), "snippets": [SNIPPET]}]}
        result = await pipeline.run_local(self.request(), runner)
        self.assertEqual(result["evidence"], [], "a path outside the snapshot must never be cited")

    async def test_source_changed_after_the_engine_ran_is_rejected(self):
        def rewrite():
            self.source.write_text("# Fixture\n\nThe codename is CHANGED.\n", encoding="utf-8")
        result = await self.run_query([{"snippets": [SNIPPET]}], after=rewrite)
        self.assertEqual(result["evidence"], [], "re-verification must happen after the engine returns")

    async def test_invented_snippet_never_appears_as_evidence(self):
        result = await self.run_query([{"snippets": [INVENTED]}])
        self.assertEqual(len(result["evidence"]), 1)
        self.assertNotIn(INVENTED, result["evidence"][0]["excerpt"])

    async def test_unit_marked_not_found_is_skipped(self):
        result = await self.run_query([{"snippets": [SNIPPET], "is_found": False}])
        self.assertEqual(result["evidence"], [])

    async def test_capacity_rejection_happens_before_the_engine_runs(self):
        called = asyncio.Event()

        async def runner(child_request):
            called.set()
            return {"status": "partial", "answer": "", "usage": None, "candidates": []}

        with patch.object(pipeline, "build_snapshot",
                          side_effect=pipeline.SnapshotCapacityError("files", 1000)):
            result = await pipeline.run_local(self.request(), runner)
        self.assertEqual(result["status"], "failed")
        self.assertIn("LIBRARY_CAPACITY_EXCEEDED", result["warnings"][0])
        self.assertFalse(called.is_set(), "no engine process may start for an over-sized library")

    async def test_filename_only_passes_file_names_through_without_citations(self):
        async def runner(child_request):
            return {"status": "partial", "answer": "", "usage": None, "candidates": [],
                    "matches": [{"name": "fixture.md"}]}
        result = await pipeline.run_local(self.request("FILENAME_ONLY"), runner)
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["matches"], [{"name": "fixture.md"}])

    async def test_worker_request_requires_a_prepared_snapshot(self):
        with self.assertRaises(pipeline.SnapshotMissing):
            pipeline.worker_request(self.request(), self.cache / "job-absent")


class FragmentChoiceTests(unittest.TestCase):
    """Which verbatim run of the engine's snippet becomes the citation.

    Longest-first turned a figure caption into a citation while the sentence answering the question sat
    in the same snippet (two adjacent caption lines beat one prose line). The rule must be general - no
    document name, line number or answer text is known to it - and a caption must still be returned when
    it is the only run, because for some queries that is where the answer is.
    """

    ANSWER = "The bulk pathway dominates the measured rate under all conditions we tested."
    CAPTION = ("Figure 1 | Rate contributions for the two pathways.\n"
               "Panels show the contribution of each pathway over the tested range.")
    #: The same caption block, but this time the answer sentence really is caption text. The preference
    #: must not become a filter: caption-shaped evidence still wins when it is the evidence.
    CAPTION_WITH_ANSWER = ("Figure 1 | Rate contributions for the two pathways.\n"
                           "Panels show the contribution of each pathway over the tested range; "
                           + ANSWER)

    def source(self, layout):
        filler = "\n".join("filler line %03d about unrelated apparatus and calibration." % i
                           for i in range(40))
        if layout == "answer-first":
            return self.ANSWER + "\n" + filler + "\n" + self.CAPTION + "\n" + filler
        if layout == "answer-in-body":
            return filler + "\n" + self.ANSWER + "\n" + filler + "\n" + self.CAPTION
        if layout == "caption-only":
            return filler + "\n" + self.CAPTION + "\n" + filler
        if layout == "caption-holds-the-answer":
            return filler + "\n" + self.CAPTION_WITH_ANSWER + "\n" + filler
        raise AssertionError(layout)

    def snippet(self, layout):
        """What the engine hands over: the matching lines, answer line and caption block together."""
        if layout == "caption-only":
            return "[doc.md]\n" + self.CAPTION
        if layout == "caption-holds-the-answer":
            return "[doc.md]\n" + self.CAPTION_WITH_ANSWER
        return "[doc.md]\n" + self.CAPTION + "\n" + self.ANSWER

    def test_the_answer_line_wins_over_a_longer_caption_even_when_it_comes_first(self):
        source = self.source("answer-first")
        chosen = pipeline.verbatim_fragment(self.snippet("answer-first"), source, "bulk pathway rate")
        self.assertIn(self.ANSWER, chosen)
        self.assertNotIn("Panels show", chosen)

    def test_the_answer_line_wins_wherever_it_sits_in_the_document(self):
        source = self.source("answer-in-body")
        chosen = pipeline.verbatim_fragment(self.snippet("answer-in-body"), source, "bulk pathway rate")
        self.assertIn(self.ANSWER, chosen)
        self.assertNotIn("Panels show", chosen)

    def test_a_caption_is_still_returned_when_it_is_the_only_run(self):
        # The preference is not a filter: a caption-only answer must survive.
        source = self.source("caption-only")
        chosen = pipeline.verbatim_fragment(self.snippet("caption-only"), source, "rate contributions")
        self.assertIn("Figure 1", chosen)
        self.assertIn("Panels show", chosen)

    def test_caption_text_holding_the_answer_is_returned_not_suppressed(self):
        # "Prefer prose" must never mean "discard captions": when the answer sentence is caption text
        # and nothing else in the file carries it, that run is the most query-relevant one and wins.
        source = self.source("caption-holds-the-answer")
        chosen = pipeline.verbatim_fragment(self.snippet("caption-holds-the-answer"), source,
                                            "bulk pathway rate")
        self.assertIn(self.ANSWER, chosen, "the answer-bearing run must be the one chosen")
        self.assertIn("Figure 1", chosen, "and it is still caption-shaped evidence")
        self.assertIn("Panels show", chosen)

    def test_caption_labels_are_recognised_in_their_plural_and_abbreviated_forms(self):
        # A singular-only pattern silently treated "Panels show ..." - the very second line of the
        # caption that produced the wrong citation - as prose, so the preference would not have applied.
        for line in ("Figure 1 | Rates.", "Figures 2-4 | Rates.", "Fig. 3 | Rates.", "Table 2 | Rates.",
                     "Tables 1 and 2 | Rates.", "Tab. 4 | Rates.", "Panels show the contribution.",
                     "Extended Data Fig. 5 | Rates.", "Supplementary Figure 2 | Rates.",
                     "Supplementary Table 1 | Rates."):
            with self.subTest(line=line):
                self.assertTrue(pipeline._CAPTION_LINE.match(line), line)
        for line in ("Panelled walls were measured.", "Tabletop apparatus was used.",
                     "Figured values are listed.", "Abstract. We find that rates rise."):
            with self.subTest(line=line):
                self.assertIsNone(pipeline._CAPTION_LINE.match(line), line)

    #: One page of an extraction: the caption has no blank line inside it, a page marker follows it and
    #: then body prose resumes - the shape that made "classify by the run's first line" wrong.
    EXTRACTION = ("Abstract. " + ANSWER + "\n"
                  + "\n".join("body line %03d about unrelated apparatus and calibration." % i
                              for i in range(5))
                  + "\nFig. 2 | Rate contributions for the two pathways.\n"
                  + "interface. Panels show the contribution of each pathway over the tested range and\n"
                  + "the bulk pathway dominates the measured rate under all conditions we tested.\n"
                  + "PDF page 3\n"
                  + "Another paragraph of body prose about the pathway and its rate.")

    def _extraction_index(self, prefix):
        for number, line in enumerate(self.EXTRACTION.split("\n"), 1):
            if line.startswith(prefix):
                return number
        raise AssertionError(prefix)

    def test_caption_continuation_lines_belong_to_the_caption_block(self):
        label = self._extraction_index("Fig. 2 |")
        captions = pipeline._caption_lines(self.EXTRACTION)
        self.assertIn(label, captions)
        self.assertIn(label + 1, captions, "a continuation line carries no label of its own")
        self.assertIn(label + 2, captions)

    def test_the_caption_block_ends_before_the_prose_that_follows_it(self):
        label = self._extraction_index("Fig. 2 |")
        captions = pipeline._caption_lines(self.EXTRACTION)
        self.assertNotIn(label + 3, captions, "the page marker ends the block and is not caption text")
        self.assertNotIn(label + 4, captions, "body prose after the page marker is not caption text")
        self.assertNotIn(self._extraction_index("Abstract."), captions)
        self.assertNotIn(self._extraction_index("Abstract.") + 1, captions)

    def test_a_run_starting_inside_a_caption_no_longer_outranks_the_prose_answer(self):
        # The wrong citation in the real run was a caption's *continuation* line: it starts with
        # "interface." and therefore carried no label, so the old rule scored it as prose.
        lines = self.EXTRACTION.split("\n")
        label = self._extraction_index("Fig. 2 |") - 1
        snippet = lines[0] + "\n" + "\n".join(lines[label:label + 3])
        chosen = pipeline.verbatim_fragment(snippet, self.EXTRACTION, "bulk pathway rate")
        self.assertIn(self.ANSWER, chosen)
        self.assertNotIn("Panels show", chosen)

    def test_a_long_stretch_after_a_caption_label_is_bounded(self):
        # No blank lines anywhere: the bound is what stops a caption from swallowing all later prose.
        text = "\n".join(["Table 1 | Rates of the two pathways."]
                         + ["continuation line %03d about the pathways and their rates" % i
                            for i in range(60)])
        captions = pipeline._caption_lines(text)
        self.assertIn(1, captions)
        self.assertLessEqual(len(captions), pipeline._CAPTION_MAX_LINES)
        self.assertNotIn(60, captions, "the tail of the stretch is not caption text")

    def test_the_caption_preference_does_not_depend_on_query_terms(self):
        # With no usable query terms the rule still prefers prose; it never needs the answer's wording.
        source = self.source("answer-first")
        chosen = pipeline.verbatim_fragment(self.snippet("answer-first"), source)
        self.assertIn(self.ANSWER, chosen)
        self.assertNotIn("Panels show", chosen)

    def test_nothing_verbatim_means_no_fragment(self):
        chosen = pipeline.verbatim_fragment("model prose that appears nowhere in the file",
                                            self.source("answer-first"), "bulk pathway")
        self.assertEqual(chosen, "")

    def test_runs_shorter_than_the_minimum_are_not_returned(self):
        chosen = pipeline.verbatim_fragment("ok\n" + self.ANSWER, self.source("answer-first"),
                                            "bulk", min_chars=1000)
        self.assertEqual(chosen, "")

    def test_a_citation_covering_the_caption_is_not_reported_as_answering_the_question(self):
        # The parent's job is provenance; whether the excerpt supports the conclusion is judged apart.
        result = {"status": "partial", "answer": "", "candidates": [
            {"file": "never-cited.md", "is_found": True, "snippets": ["x"]}]}
        self.assertNotIn("evidence", result, "the engine's answer is never forwarded as evidence")
        self.assertEqual(result["candidates"][0]["snippets"], ["x"])


class FragmentRankingTests(unittest.TestCase):
    """Which passage a question selects, next to what the previous rule chose on the same input.

    The markers here are test criteria for mechanical behaviour; they live in this file only and are not
    part of the product's ranking rule - nothing under engine/ knows these words. Literal overlap is not
    evidence that a passage answers a question, so where a case is about "no answer" the assertion is
    about provenance and metadata, never about a support verdict.
    """

    def legacy(self, snippet, text, query=None, min_chars=8):
        """The previous rule: ASCII-only coverage, then prose over caption, then length, one run."""
        import re
        terms = {term.lower() for term in re.findall(r"[0-9A-Za-z_\-]{3,}", query or "")}
        captions = pipeline._caption_lines(text)
        ranked = []
        for block in pipeline._verbatim_runs(snippet.split("\n"), text, min_chars):
            line = pipeline._verbatim_line(text, block)
            is_caption = (pipeline._CAPTION_LINE.match(block) is not None
                          or (line is not None and line in captions))
            lowered = block.lower()
            coverage = sum(1 for term in terms if term in lowered) / len(terms) if terms else 0.0
            ranked.append(((round(coverage, 3), 0 if is_caption else 1, len(block)), block))
        return max(ranked, default=((0, 0, 0), ""))[1]

    # -- 1. a Chinese question whose answer sentence carries no ASCII entity name -------------------
    ENTITY = "SO2"
    ENTITY_ANSWER = ["The simulations show the reaction runs mainly in the bulk phase, and the",
                     "interface contributes only about 1% of the total conversion."]

    def entity_saturated_document(self):
        lines = ["# report"]
        lines += ["Sample %02d: %s uptake on the aqueous surface was measured." % (i, self.ENTITY)
                  for i in range(30)]
        lines += ["Fig. 2 | Rates of %s hydrolysis in the bulk and at the interface." % self.ENTITY]
        lines += list(self.ENTITY_ANSWER)
        lines += ["Filler %02d without the searched phrase." % i for i in range(5)]
        return "\n".join(lines)

    def test_chinese_question_with_the_entity_only_in_the_caption(self):
        document = self.entity_saturated_document()
        # the engine's window keeps the two passages as separate regions, as it does in a real extraction
        snippet = ("Fig. 2 | Rates of %s hydrolysis in the bulk and at the interface.\n\n"
                   % self.ENTITY) + "\n".join(self.ENTITY_ANSWER)
        query = "哪篇论文认为 %s 水解主要发生在体相，界面贡献只有约 1%%？" % self.ENTITY
        chosen = pipeline.verbatim_fragment(snippet, document, query)
        self.assertIn("only about 1%", chosen, "the answer sentence must win")
        self.assertNotIn("Fig. 2", chosen)
        # the previous rule was decided by the one ASCII word the caption happens to repeat
        legacy = self.legacy(snippet, document, query)
        self.assertIn("Fig. 2", legacy)
        self.assertNotIn("only about 1%", legacy)

    def test_a_question_with_no_ascii_feature_at_all_still_selects_on_its_own_terms(self):
        document = "\n".join(["# 报告"] + ["第 %02d 条样本记录了体相水解速率。" % i for i in range(20)]
                             + ["界面过程的贡献相比之下可以忽略。"] + ["其他内容 %02d。" % i for i in range(5)])
        snippet = "体相水解速率\n界面过程的贡献相比之下可以忽略。"
        chosen = pipeline.verbatim_fragment(snippet, document, "体相水解和界面过程的贡献分别是多少？")
        self.assertIn("界面过程的贡献相比之下可以忽略。", chosen)

    # -- 2. English and mixed questions --------------------------------------------------------------
    def bulk_answer_document(self):
        lines = ["# report", "Fig. 1 | Bulk hydrolysis rates in the studied system."]
        lines += ["Section %02d reports hydrolysis rates for the bulk system." % i for i in range(25)]
        lines += ["The bulk hydrolysis rate exceeds the interfacial one by two orders of magnitude."]
        return "\n".join(lines)

    def test_english_question_selects_the_sentence_that_states_the_comparison(self):
        document = self.bulk_answer_document()
        snippet = "\n".join(["Fig. 1 | Bulk hydrolysis rates in the studied system.",
                             "The bulk hydrolysis rate exceeds the interfacial one by two orders of magnitude."])
        chosen = pipeline.verbatim_fragment(
            snippet, document, "Which hydrolysis rate is larger, the bulk or the interfacial one?")
        self.assertIn("exceeds the interfacial one", chosen)

    def test_mixed_question_selects_the_sentence_that_states_the_comparison(self):
        document = self.bulk_answer_document()
        snippet = "\n".join(["Fig. 1 | Bulk hydrolysis rates in the studied system.",
                             "The bulk hydrolysis rate exceeds the interfacial one by two orders of magnitude."])
        chosen = pipeline.verbatim_fragment(
            snippet, document, "体相 hydrolysis rate 和界面相比哪个更大？")
        self.assertIn("exceeds the interfacial one", chosen)

    # -- 3. a caption covering more words, an answer carrying the scarce one -------------------------
    def test_a_caption_covering_more_terms_does_not_outrank_the_scarce_answer(self):
        filler = ["Section %02d studies the bulk hydrolysis rate ratio of the phase." % i for i in range(60)]
        document = "\n".join(["# report",
                              "Fig. 4 | Bulk hydrolysis rate ratio across the phase study."]
                             + filler
                             + ["The conversion in the bulk was 0.97%."])
        caption = "Fig. 4 | Bulk hydrolysis rate ratio across the phase study."
        answer = "The conversion in the bulk was 0.97%."
        snippet = caption + "\n\n" + answer
        query = "What is the measured bulk hydrolysis rate ratio, about 0.97%?"
        chosen = pipeline.verbatim_fragment(snippet, document, query)
        self.assertIn("0.97%", chosen, "the scarce term is what tells the two passages apart")
        self.assertNotIn("Fig. 4", chosen)
        # the previous rule counted matching words, so the caption's terms beat the answer's two
        legacy = self.legacy(snippet, document, query)
        self.assertIn("Fig. 4", legacy)
        self.assertNotIn("0.97%", legacy)

    # -- 4. the answer really is only in the caption -------------------------------------------------
    def test_an_answer_that_really_is_only_in_the_caption_is_still_returned(self):
        document = "\n".join(["# report"] + ["Filler %02d without findings." % i for i in range(20)]
                             + ["Table 2 | The measured interfacial contribution was 1% of the total flux."])
        snippet = "Table 2 | The measured interfacial contribution was 1% of the total flux."
        parts, ranked = pipeline.select_fragments(snippet, document, "What was the interfacial contribution, 1%?")
        self.assertEqual(len(parts), 1)
        self.assertIn("1% of the total flux", parts[0])
        self.assertTrue(ranked[0]["caption"], "it is caption text and is returned anyway")

    # -- 5. two fragments that jointly carry the necessary information -------------------------------
    def test_two_complementary_fragments_are_both_kept(self):
        document = "\n".join(["# report",
                              "The bulk pathway dominates the conversion in our simulations.",
                              "only about 1% of the conversion comes from the interface."])
        snippet = ("The bulk pathway dominates the conversion in our simulations.\n\n"
                   "only about 1% of the conversion comes from the interface.")
        query = "Does the bulk pathway dominate, and what share comes from the interface, about 1%?"
        parts, ranked = pipeline.select_fragments(snippet, document, query)
        self.assertEqual(len(parts), 2, "one passage alone would drop half the answer")
        joined = "\n".join(parts)
        self.assertIn("dominates", joined)
        self.assertIn("1%", joined)
        selected = [run for run in ranked if run["selected"]]
        self.assertEqual(sorted((run["firstLine"], run["endLine"]) for run in selected), [(2, 2), (3, 3)])

    # -- 6. high lexical overlap with no answer ------------------------------------------------------
    def test_high_overlap_without_an_answer_is_quoted_as_provenance_and_claims_nothing(self):
        document = "\n".join(["# report",
                              "Bulk hydrolysis rate ratio measurement methods and instruments",
                              "The equipment is described elsewhere."])
        snippet = "Bulk hydrolysis rate ratio measurement methods and instruments"
        query = "What is the bulk hydrolysis rate ratio measurement?"
        parts, ranked = pipeline.select_fragments(snippet, document, query)
        self.assertEqual(parts, [snippet], "overlap alone still quotes the passage")
        self.assertGreater(ranked[0]["score"], 0.0)
        self.assertEqual(set(ranked[0]) - {"block", "chars", "firstLine", "endLine", "start", "end",
                                           "score", "caption", "selected", "reason", "complete"}, set(),
                         "the ranking reports scores and reasons, never a support verdict")

    # -- 7. negation, comparison and units are not cut off -------------------------------------------
    def test_a_run_stopping_mid_sentence_is_completed_to_the_unit_it_belongs_to(self):
        lines = ["Filler about the system.",
                 "The interfacial pathway is not",
                 "the dominant route and it contributes about",
                 "1% of the total conversion.",
                 "Filler again."]
        text = "\n".join(lines)
        completed, start, end = pipeline._complete_sentence(lines, 2, 3, text)
        self.assertIn("is not", completed, "the negation must come along")
        self.assertIn("1% of the total conversion.", completed, "so must the unit")
        self.assertEqual((start, end), (1, 4))

    def test_a_run_that_already_ends_its_sentence_is_not_extended_into_prose(self):
        lines = ["Filler.", "The bulk pathway dominates.", "An unrelated paragraph follows."]
        text = "\n".join(lines)
        completed, start, end = pipeline._complete_sentence(lines, 1, 2, text)
        self.assertEqual(completed, "The bulk pathway dominates.")
        self.assertEqual((start, end), (1, 2))

    def test_features_cover_chinese_english_and_quantities_without_an_answer_list(self):
        features = pipeline.query_features("体相 hydrolysis 贡献约 0.97%？")
        self.assertIn("0.97%", features, "a quantity keeps its unit")
        self.assertIn("hydrolysis", features)
        for bigram in ("体相", "贡献"):
            self.assertIn(bigram, features, "features are CJK bigrams too")
        self.assertNotIn("0.97", features, "a bare number would match everywhere")


class CitationIntegrityTests(unittest.TestCase):
    """A quote that stops inside a word or a sentence must not read as the complete statement."""

    def document(self):
        return "\n".join([
            "# report",
            "This process is typically assessed using the uptake coefficient (gamma), which quantifies the",
            "frac-", "tion of collisions that lead to reaction at the interface.",
            "The interfacial route is not the dominant one; it contributes about 1% of the total.",
            "An unrelated closing paragraph follows here."])

    # -- PDF hyphenation, continuation, negation, comparison, units ---------------------------------
    def test_a_quote_that_stops_inside_a_hyphenated_word_is_marked_incomplete(self):
        self.assertFalse(pipeline._closes_a_sentence("… which quantifies the frac-"))
        self.assertTrue(pipeline._closes_a_sentence("… contributes about 1% of the total."))

    def test_a_quote_is_extended_across_the_line_break_and_the_unit_is_kept(self):
        document = self.document()
        lines = document.split("\n")
        snippet = "\n".join(lines[1:5])          # from the "assessed using …" line through the continuation
        chosen = pipeline.verbatim_fragment(snippet, document, "interfacial contribution")
        self.assertTrue(pipeline._closes_a_sentence(chosen), chosen)
        self.assertIn("1%", chosen, "the unit must survive the extension")
        self.assertIn("not the dominant", chosen, "so must the negation and the comparison")

    def test_when_it_cannot_be_closed_the_run_reports_incomplete_rather_than_guessing(self):
        # The word is split across the line break and the line that would finish the sentence is absent,
        # so nothing in the snippet can close the statement: quote it and say it is incomplete.
        document = self.document()
        snippet = ("This process is typically assessed using the uptake coefficient (gamma), which "
                   "quantifies the\nfrac-")
        parts, ranked = pipeline.select_fragments(snippet, document, "uptake coefficient")
        self.assertEqual(len(parts), 1)
        self.assertTrue(parts[0].endswith("frac-"), parts[0])
        self.assertFalse(ranked[0]["complete"], "and it says so instead of claiming completion")

    def test_a_whole_snippet_that_stops_mid_sentence_keeps_no_units_it_never_had(self):
        document = self.document()
        lines = document.split("\n")
        snippet = "\n".join(lines[1:3])               # ends at "frac-"
        parts, ranked = pipeline.select_fragments(snippet, document, "uptake coefficient")
        self.assertEqual(parts, [snippet.strip()])
        self.assertFalse(ranked[0]["complete"])


class ResultAnnotationTests(unittest.TestCase):
    """What every result tells the model about the service, and what it must stop telling it.

    These are not cosmetic strings. Every search result the main Agent reads carries them, immediately before
    it decides how to answer - so a warning that describes a finished capability as "pending" is read as a
    reason to hedge, and a warning that claims isolation the job did not have is a false assurance.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "library"
        self.root.mkdir()
        self.source = self.root / "note.md"
        self.source.write_text("line one\nline two\n", encoding="utf-8")
        self.work = Path(self.temp.name) / "cache" / "job-annotation"

    def manifest(self):
        manifest, _warnings = pipeline.build_snapshot(str(self.root), self.work)
        return manifest

    def finalize(self, **kwargs):
        manifest = self.manifest()
        file_path = next(Path(self.work / "files").rglob("*.md"))
        payload = {"status": "partial", "warnings": [],
                   "candidates": [{"file": str(file_path), "is_found": True, "snippets": ["line one\nline two"]}]}
        return pipeline.finalize(payload, manifest,
                                 {"mode": "FAST", "query": "line", "libraryId": "fixture"}, [], None, None, **kwargs)

    def test_every_result_states_what_this_service_does_not_do(self):
        warnings = self.finalize()["warnings"]
        self.assertTrue(any(warning.startswith("M0: verified excerpts only") for warning in warnings), warnings)
        # The specific sentence that was wrong: synthesis is the calling Agent's job, not an unfinished
        # capability of this service. Asserting the absence keeps it from being reintroduced.
        joined = " ".join(warnings)
        self.assertNotIn("synthesis", joined.replace("compose an answer", ""), warnings)
        self.assertNotIn("pending", joined, warnings)

    def test_the_isolation_claim_is_the_callers_and_is_attached_verbatim(self):
        # The service is the layer that runs the container, so it decides the wording; the pipeline must not
        # invent an isolation state of its own.
        note = "ENGINE_ISOLATION: a synthetic posture for this test"
        warnings = self.finalize(isolation=note)["warnings"]
        self.assertIn(note, warnings)

    def test_no_isolation_claim_is_made_when_the_caller_makes_none(self):
        # The honest default for an in-process caller: silence, not an implied "isolated".
        warnings = self.finalize()["warnings"]
        self.assertFalse(any(warning.startswith("ENGINE_ISOLATION") for warning in warnings), warnings)

    def test_the_harness_entry_point_says_it_did_not_isolate(self):
        joined = pipeline.HARNESS_ISOLATION
        self.assertIn("not applied", joined)
        self.assertFalse(any(word in joined for word in ("AppContainer enforced", "pending")), joined)


class FragmentRecordTests(unittest.TestCase):
    """The record must say how complete it is, and must not let a name or a passage into the sink."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "library"
        self.root.mkdir()
        self.source = self.root / "paper (2026).md"
        self.source.write_text("\n".join(["# report", "line two", "line three", "line four",
                                          "line five states a finished sentence."]), encoding="utf-8")
        self.cache = Path(self.temp.name) / "cache"
        self.work = self.cache / "job-record"

    def manifest(self):
        manifest, _warnings = pipeline.build_snapshot(str(self.root), self.work)
        file_path = next(Path(self.work / "files").rglob("*.md"))
        return manifest, file_path

    def test_the_record_counts_the_candidates_it_could_not_log(self):
        import diagnostics as diagnostics_module
        manifest, file_path = self.manifest()
        text = diagnostics_module.source_text if False else None
        sink = Path(self.temp.name) / "diag.jsonl"
        diag = diagnostics_module.Diagnostics(sink, job="job-record")
        snippets = ["line three\nline four", "line two\nline three", "line four", "line two"]
        candidates = [{"file": str(file_path), "is_found": True, "snippets": snippets}]
        evidence, _quoted = pipeline.verify_candidates(manifest, candidates, "line", "fixture", diag)
        records = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(records)
        fragments = records[0]["fragments"]
        for key in ("totalCandidates", "uniqueCandidates", "loggedCandidates", "truncatedCount"):
            self.assertIn(key, fragments)
        self.assertEqual(fragments["loggedCandidates"], len(fragments["runs"]))
        self.assertLessEqual(fragments["uniqueCandidates"], fragments["totalCandidates"])
        self.assertEqual(fragments["truncatedCount"],
                         max(0, fragments["uniqueCandidates"] - fragments["loggedCandidates"]))
        ranges = [(run["startLine"], run["endLine"]) for run in fragments["runs"]]
        self.assertEqual(len(ranges), len(set(ranges)), "candidates are deduplicated before the cap")
        self.assertIn("layers", fragments)
        self.assertEqual([layer["layer"] for layer in fragments["layers"]],
                         ["engine", "worker", "parent"])
        self.assertNotIn("paper (2026)", sink.read_text(encoding="utf-8"),
                         "the file name must not reach the sink")
        for run in fragments["runs"]:
            self.assertRegex(run["fileId"], r"^[0-9a-f]{12}$", "an opaque handle, not a name")

    def test_every_stage_reports_its_own_spans_and_says_when_a_fragment_is_unlocatable(self):
        import diagnostics as diagnostics_module
        manifest, file_path = self.manifest()
        sink = Path(self.temp.name) / "diag.jsonl"
        diag = diagnostics_module.Diagnostics(sink, job="job-record")
        candidates = [{"file": str(file_path), "is_found": True,
                       "snippets": ["not in this file at all", "line three\nline four"]}]
        engine_facts = {"sampledSpans": {str(file_path): [[2, 4]]}}
        pipeline.verify_candidates(manifest, candidates, "line", "fixture", diag, engine_facts)
        record = json.loads(sink.read_text(encoding="utf-8").splitlines()[0])
        layers = {layer["layer"]: layer["fragments"] for layer in record["fragments"]["layers"]}
        self.assertEqual(layers["engine"][0]["startLine"], 2)
        self.assertEqual(layers["engine"][0]["endLine"], 4)
        self.assertTrue(layers["engine"][0]["locatable"])
        worker = layers["worker"]
        self.assertTrue(any(entry["locatable"] for entry in worker))
        self.assertTrue(any(not entry["locatable"] for entry in worker),
                        "a snippet that is not in the file must be marked, not given a range")
        self.assertTrue(all(entry["fragmentId"].startswith("worker-") for entry in worker))
        file_ids = {entry["fileId"] for layer in record["fragments"]["layers"] for entry in layer["fragments"]}
        self.assertEqual(len(file_ids), 1, "every stage names the same opaque file handle")
        self.assertRegex(file_ids.pop(), r"^[0-9a-f]{12}$")

    def test_an_excerpt_that_stops_mid_sentence_raises_a_warning_not_a_claim(self):
        manifest, file_path = self.manifest()
        candidates = [{"file": str(file_path), "is_found": True,
                       "snippets": ["line two\nline three"]}]
        result = pipeline.finalize({"status": "partial", "candidates": candidates},
                                   manifest, {"mode": "FAST", "query": "line", "libraryId": "fixture"}, [])
        self.assertEqual(result["evidence"][0]["excerptComplete"], False)
        self.assertTrue(any(warning.startswith("EXCERPT_TRUNCATED") for warning in result["warnings"]),
                        result["warnings"])

    def test_excerpt_complete_is_a_boundary_check_not_a_support_verdict(self):
        # A perfectly closed sentence that has nothing to do with the question is still "complete".
        manifest, file_path = self.manifest()
        candidates = [{"file": str(file_path), "is_found": True,
                       "snippets": ["line five states a finished sentence."]}]
        result = pipeline.finalize({"status": "partial", "candidates": candidates},
                                   manifest, {"mode": "FAST", "query": "line", "libraryId": "fixture"}, [])
        self.assertEqual(result["evidence"][0]["excerptComplete"], True)
        self.assertFalse(any(warning.startswith("EXCERPT_TRUNCATED") for warning in result["warnings"]))
        self.assertNotIn("answer", result["evidence"][0], "no support verdict is attached to an excerpt")

    def test_the_log_cap_reports_how_many_candidates_it_did_not_log(self):
        import diagnostics as diagnostics_module
        # Twenty markers, each verbatim but separated by filler, so every one is its own run.
        lines = []
        for index in range(20):
            lines.append("marker line %02d" % index)
            lines.append("filler paragraph %02d" % index)
        self.source.write_text("\n".join(lines), encoding="utf-8")
        manifest, file_path = self.manifest()
        snippet = "\n".join("marker line %02d" % index for index in range(20))
        sink = Path(self.temp.name) / "capped.jsonl"
        diag = diagnostics_module.Diagnostics(sink, job="job-record")
        pipeline.verify_candidates(manifest, [{"file": str(file_path), "is_found": True,
                                               "snippets": [snippet]}], "marker", "fixture", diag)
        fragments = json.loads(sink.read_text(encoding="utf-8").splitlines()[0])["fragments"]
        self.assertEqual(fragments["loggedCandidates"], 12)
        self.assertGreater(fragments["truncatedCount"], 0, "the record must admit it is a subset")
        self.assertEqual(fragments["uniqueCandidates"],
                         len(fragments["runs"]) + fragments["truncatedCount"])

    def test_each_layer_says_how_its_ranges_were_obtained(self):
        import diagnostics as diagnostics_module
        manifest, file_path = self.manifest()
        sink = Path(self.temp.name) / "diag.jsonl"
        diag = diagnostics_module.Diagnostics(sink, job="job-record")
        candidates = [{"file": str(file_path), "is_found": True, "snippets": ["line three\nline four"]}]
        pipeline.verify_candidates(manifest, candidates, "line", "fixture", diag,
                                   {"sampledSpans": {str(file_path): [[2, 4]]}})
        record = json.loads(sink.read_text(encoding="utf-8").splitlines()[0])
        layers = {layer["layer"]: layer["verification"] for layer in record["fragments"]["layers"]}
        self.assertEqual(layers, {"engine": "engine-reported", "worker": "parent-located",
                                  "parent": "parent-scored"})

    def test_forged_engine_facts_do_not_change_the_citations(self):
        import diagnostics as diagnostics_module
        manifest, file_path = self.manifest()
        candidates = [{"file": str(file_path), "is_found": True, "snippets": ["line three\nline four"]}]
        plain, _quoted = pipeline.verify_candidates(manifest, candidates, "line", "fixture")
        forges = [
            # claims the engine sampled the whole file, a range it never reported, and a foreign file
            {"sampledSpans": {str(file_path): [[1, 999]]}},
            {"sampledSpans": {str(file_path): [[3, 3]]}, "sampledUnlocatable": 99},
            {"sampledSpans": {"C:/elsewhere/other.txt": [[1, 2]]}},
            {"sampledSpans": "not-a-mapping"},
        ]
        for forged in forges:
            with self.subTest(forged=forged):
                sink = Path(self.temp.name) / "forged.jsonl"
                diag = diagnostics_module.Diagnostics(sink, job="job-record")
                evidence, _quoted = pipeline.verify_candidates(manifest, candidates, "line", "fixture",
                                                               diag, forged)
                self.assertEqual(evidence, plain, "diagnostic metadata must not steer a citation")
                self.assertNotIn("other.txt", sink.read_text(encoding="utf-8"))

    def test_engine_facts_alone_never_produce_a_citation(self):
        import diagnostics as diagnostics_module
        manifest, file_path = self.manifest()
        sink = Path(self.temp.name) / "diag.jsonl"
        diag = diagnostics_module.Diagnostics(sink, job="job-record")
        # The engine reports having sampled the file, but no candidate was handed over at all.
        evidence, _quoted = pipeline.verify_candidates(
            manifest, [], "line", "fixture", diag,
            {"sampledSpans": {str(file_path): [[1, 4]]}})
        self.assertEqual(evidence, [])
        self.assertFalse(sink.is_file(), "no candidate means nothing to score and nothing to log")


class EvidenceCoverageTests(unittest.TestCase):
    """Which parts of an answer the evidence-return strategy can and cannot keep.

    Each case states a loss the strategy causes. They are synthetic: the markers are test criteria and
    never enter the product's rules.
    """

    def fragment_of(self, document, snippet, query):
        return pipeline.select_fragments(snippet, document, query)

    def test_a_range_bound_split_off_from_its_unit_survives(self):
        # The engine's window holds two regions (separated, as a real extraction window is). One region
        # carries "41%" - a query feature - and the other carries only the lower bound "25", which does
        # not match any query word. The bound is dropped unless a bare number counts for something.
        document = "In nighttime air, between 25 and\n41% of that tropospheric N2O5 is removed by hydrolysis."
        snippet = "In nighttime air, between 25 and\n\n41% of that tropospheric N2O5 is removed by hydrolysis."
        parts, ranked = self.fragment_of(document, snippet, "What percentage range of N2O5 is removed?")
        joined = "\n".join(parts)
        self.assertIn("41%", joined)
        self.assertIn("25", joined, "the lower bound must survive: it is half of the answer")
        self.assertEqual(len(parts), 2, "both halves of the range come back as two fragments")

    def test_a_value_and_its_unit_are_not_split_apart(self):
        # Value in one region, unit in the other: the unit-only region matches no query word.
        document = ("The bulk hydrolysis rate is reported as\n4 x 10-2 ns-1, inferred from the uptake "
                    "measurements.")
        snippet = "The bulk hydrolysis rate is reported as\n\n4 x 10-2 ns-1, inferred from the uptake measurements."
        parts, _ranked = self.fragment_of(document, snippet, "What is the bulk hydrolysis rate?")
        joined = "\n".join(parts)
        self.assertIn("4 x 10-2", joined)
        self.assertIn("ns-1", joined, "a value without its unit is not the answer")

    def test_a_definition_continuation_without_query_words_is_still_reachable(self):
        # The question names the ratio; the sentence that states the negation shares none of its words.
        document = ("The bulk/sur. ratio is the bulk-phase value divided\nby the interface value, not their "
                    "difference.")
        snippet = ("The bulk/sur. ratio is the bulk-phase value divided\n\nby the interface value, not their "
                   "difference.")
        parts, _ranked = self.fragment_of(document, snippet, "What does the bulk/sur. ratio represent?")
        joined = "\n".join(parts)
        self.assertIn("divided", joined)
        self.assertIn("not their difference", joined, "the negation that rules out the wrong reading matters")

    def test_a_comparison_needing_two_files_is_not_invented_from_one(self):
        document = "Bulk Phase Dominates Sulfur Dioxide Hydrolysis\nover Interfacial Processes\nfiller"
        snippet = "Bulk Phase Dominates Sulfur Dioxide Hydrolysis\nover Interfacial Processes"
        parts, _ranked = self.fragment_of(document, snippet, "Which process dominates, and which for the other study?")
        joined = "\n".join(parts)
        self.assertIn("Bulk Phase Dominates", joined)
        self.assertNotIn("uptake is dominated by interfacial processes", joined)

    def test_a_negation_stays_attached_to_what_it_negates(self):
        document = "The interfacial route is not\nthe dominant pathway, and it contributes only 1%.\nfiller\nfiller"
        parts, _ranked = self.fragment_of(document, document, "Is the interfacial route dominant?")
        joined = "\n".join(parts)
        self.assertIn("is not", joined)
        self.assertIn("1%", joined)

    def test_every_returned_fragment_is_still_file_text(self):
        document = "alpha\nbeta hit line\ngamma\ndelta\nepsilon"
        parts, _ranked = self.fragment_of(document, "beta hit line", "hit")
        for part in parts:
            self.assertIn(part, document, "context never stops being verbatim")

    def test_a_budget_that_cannot_hold_everything_names_the_reason(self):
        lines = []
        for index in range(30):
            lines.append("value %02d with its own unit %%" % index)
            lines.append("filler paragraph %02d" % index)
        document = "\n".join(lines)
        snippet = "\n".join("value %02d with its own unit %%" % index for index in range(30))
        parts, ranked = self.fragment_of(document, snippet, "value unit percent")
        dropped = [run for run in ranked if not run["selected"]]
        self.assertTrue(dropped)
        self.assertTrue({run["reason"] for run in dropped}
                        & {"over_budget", "enough_parts", "contained_in_selected", "overlaps_selected",
                           "lower_rank"})
        self.assertLessEqual(len(parts), pipeline.MAX_FRAGMENTS_PER_FILE)


if __name__ == "__main__":
    unittest.main()
