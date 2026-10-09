"""Offline FAST replay: drive the *real* Sirchmunk FAST path with scripted model replies.

No network and no model tokens: the only thing stubbed is the chat call. Retrieval (rga/rg), evidence
sampling, the `SHOULD_ANSWER`/`SHOULD_SAVE` parsing, the cluster, the worker's candidates and the
parent's verification are all production code.

    .\\.venv\\Scripts\\python.exe scripts\\replay-fast-offline.py                    # synthetic text
    .\\.venv\\Scripts\\python.exe scripts\\replay-fast-offline.py --texts <dir>      # e.g. trial texts
    .\\.venv\\Scripts\\python.exe scripts\\replay-fast-offline.py --cases no-match

Console output is structural only (layer by layer, counts, verdicts). Full detail - sampled text and
excerpts included - goes to the report file, so literature never lands in a terminal log.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import types

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "engine"))
import pipeline  # noqa: E402
import worker  # noqa: E402

OUT = BASE / ".local/verification/fast-replay"
NO_RESULTS = "No results found."

ANALYSIS = json.dumps({
    "type": "search", "primary": ["N2O5 solvation"], "fallback": ["N2O5", "solvation"],
    "idf": {"N2O5 solvation": 8.0, "N2O5": 6.0, "solvation": 4.0},
    "primary_alt": [], "fallback_alt": [], "file_hints": [], "intent": "find the source passage",
})
NO_MATCH_ANALYSIS = json.dumps({
    "type": "search", "primary": ["zzzabsenttermzzz"], "fallback": ["absenttermqqq"], "idf": {},
    "primary_alt": [], "fallback_alt": [], "file_hints": [], "intent": "nothing should match",
})


#: Synthetic corpora for the fragment-choice question. Each file is larger than the engine's
#: small-file threshold so the sampling path runs, and the keyword sits both in a prose line and in a
#: two-line caption-shaped block - the competition that produced a caption citation in the real run.
ANSWER_MARKER = "dominates the total rate"
CAPTION_MARKER = "Panels show"
KEYWORD = "interfacial hydrolysis"
PROSE_LINE = ("Abstract. We find that %s in the bulk phase %s, while the interface contributes a "
              "negligible fraction." % (KEYWORD, ANSWER_MARKER))
BODY_LINE = ("Discussion. Under the conditions studied here, %s in the bulk phase %s, and the "
             "interface is not the dominant pathway." % (KEYWORD, ANSWER_MARKER))
CAPTION = ("Figure 1 | Rates of %s in the bulk and at the interface.\n"
           "Panels show %s contributions for the interface region and for the bulk region."
           % (KEYWORD, KEYWORD))
#: The same caption block with the answer sentence inside it. This is the case the rule must not lose:
#: the only place holding the answer is caption text, so returning "nothing but caption" is correct.
CAPTION_WITH_ANSWER = ("Figure 1 | Rates of %s in the bulk and at the interface.\n"
                       "Panels show %s contributions for the interface region and for the bulk region; "
                       "the bulk phase %s." % (KEYWORD, KEYWORD, ANSWER_MARKER))
#: The ASCII entity name the synthetic question is built around, used so one window can carry it while
#: the other carries the quantity: neither window alone states both halves of the answer.
ENTITY_NAME = "SO2"
#: A second marker, far from the first one, so "the information is split across two windows" is
#: checkable by looking for both markers rather than by reading the text.
VALUE_MARKER = "was measured at only 1%"
FILLER = "".join("Ordinary prose line %04d describing unrelated measurements and apparatus.\n" % i
                 for i in range(120))


def corpus_text(kind):
    if kind == "answer-in-abstract":
        return PROSE_LINE + "\n" + FILLER + "\n" + CAPTION + "\n" + FILLER
    if kind == "answer-in-body":
        return "# report\n" + FILLER[:1500] + "\n" + BODY_LINE + "\n" + FILLER[1500:] + "\n" + CAPTION + "\n"
    if kind == "answer-only-in-caption":
        return "# report\n" + FILLER + "\n" + CAPTION_WITH_ANSWER + "\n" + FILLER
    if kind == "answer-in-two-windows":
        # Two hits far apart, each carrying a different query feature - the entity name in one, the
        # quantity in the other (on the line it continues on) - and neither window alone states both
        # halves of the answer, so the answer is only complete when both come back.
        return ("# report\n"
                + "The measured %s hydrolysis of " % KEYWORD + ENTITY_NAME + " in the bulk phase\n"
                + ANSWER_MARKER + " under all tested conditions.\n"
                + FILLER + "\n"
                + KEYWORD + " in the surface region\n"
                + VALUE_MARKER + " of the total flux.\n"
                + FILLER)
    if kind == "no-answer":
        return "# report\n" + FILLER + "\nAnother paragraph without the searched phrase.\n"
    raise SystemExit("unknown corpus %r" % kind)


def build_corpus(kind, root=None):
    directory = Path(root or tempfile.mkdtemp(prefix="fast-fragment-")) / kind
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ("%s.md" % kind)).write_text(corpus_text(kind), encoding="utf-8")
    return directory


def legacy_verbatim_fragment(snippet, text, query=None, min_chars=8):
    """The previous longest-run-only rule, kept here so the loss point can be shown side by side."""
    if not snippet or not text:
        return ""
    lines = snippet.split("\n")
    best = ""
    for start in range(len(lines)):
        for end in range(len(lines), start, -1):
            block = "\n".join(lines[start:end]).strip()
            if len(block) < min_chars or len(block) <= len(best):
                continue
            if block in text:
                best = block
                break
    return best


ENGLISH_QUERY = "N2O5 adsorption and solvation in water"
# Query-A-shaped case: a Chinese question whose ASCII keyword extraction is the phrase that appears
# both in the paper's abstract sentence and in a later figure caption. That is the real competition.
ABSTRACT_VS_CAPTION_QUERY = "哪篇论文认为 SO2 水解主要发生在体相，界面贡献只有约 1%？"
ABSTRACT_VS_CAPTION_ANALYSIS = json.dumps({
    "type": "search", "primary": ["interfacial hydrolysis"], "fallback": ["hydrolysis", "interfacial"],
    "idf": {"interfacial hydrolysis": 8.0, "hydrolysis": 5.0, "interfacial": 5.0},
    "primary_alt": [], "fallback_alt": [], "file_hints": [], "intent": "bulk versus interface claim",
})
# Test 1 of the real work order: a Chinese query. `_compute_keyword_coverage` tokenises the query with
# an ASCII-only pattern, so a Chinese query yields no keywords and coverage 0.0 - below the 0.3 gate -
# which means the acceptance heuristic can reject evidence the engine did find.
CHINESE_QUERY = "哪篇论文认为 SO2 水解主要发生在体相，界面贡献只有约 1%？请查找直接支持或反驳这一说法的原文"
ZH_ANALYSIS = json.dumps({
    "type": "search", "primary": ["interfacial hydrolysis"], "fallback": ["hydrolysis", "interfacial"],
    "idf": {"interfacial hydrolysis": 8.0, "hydrolysis": 5.0, "interfacial": 5.0},
    "primary_alt": [], "fallback_alt": [], "file_hints": [], "intent": "test the bulk-vs-interface claim",
})


def briefing(should_answer=True, should_save=True, precise="", summary="briefing body"):
    return (f"<SUMMARY>{summary}</SUMMARY>\n<PRECISE_ANSWER>{precise}</PRECISE_ANSWER>\n"
            f"<SHOULD_ANSWER>{'true' if should_answer else 'false'}</SHOULD_ANSWER>\n"
            f"<SHOULD_SAVE>{'true' if should_save else 'false'}</SHOULD_SAVE>")


#: case -> query, analysis reply, and the summary replies in call order
CASES = {
    "answer-no-cache": (ENGLISH_QUERY, ANALYSIS, [briefing(True, False, "solvation free energy")]),
    "answer-and-cache": (ENGLISH_QUERY, ANALYSIS, [briefing(True, True, "solvation free energy")]),
    "refused": (ENGLISH_QUERY, ANALYSIS, [briefing(False, False, "should answer false"),
                                         briefing(False, False, "should answer false")]),
    "zh-refused": (CHINESE_QUERY, ZH_ANALYSIS, [briefing(False, False, "should answer false"),
                                                briefing(False, False, "should answer false")]),
    # The rejection path that produced the real zero-candidate result: the analysis keywords find and
    # read a file, the model declines, and the acceptance heuristic also fails because the *query's*
    # own terms are absent from the sampled window (coverage < 0.3, no numeric intent).
    "keyword-mismatch": ("tell me about phonon entanglement in crystals", ANALYSIS,
                         [briefing(False, False, "should answer false"),
                          briefing(False, False, "should answer false")]),
    "no-match": ("N2O5 adsorption and solvation in water", NO_MATCH_ANALYSIS, []),
    "fabricated": (ENGLISH_QUERY, ANALYSIS, [briefing(True, False,
                                                      "see /nonexistent/fabricated.md")]),
    # The task-A reproduction: keywords that match the abstract sentence and a figure caption in the
    # same file; the model answers, so only the parent's fragment choice decides which one is cited.
    "abstract-vs-caption": (ABSTRACT_VS_CAPTION_QUERY, ABSTRACT_VS_CAPTION_ANALYSIS,
                            [briefing(True, False, "interfacial hydrolysis")]),
}


class _AsyncCloser:
    async def close(self):
        return None


class StubChat:
    """OpenAIChat stand-in: scripted replies plus a record of every prompt that was sent."""

    prompts = []
    replies = []

    def __init__(self, **kwargs):
        self._client = types.SimpleNamespace(close=lambda: None)
        self._async_client = _AsyncCloser()
        self._model = kwargs.get("model") or "stub-model"

    async def achat(self, messages, **kwargs):
        prompt = messages[-1]["content"] if messages else ""
        StubChat.prompts.append(prompt)
        index = len(StubChat.prompts) - 1
        if index < len(StubChat.replies):
            content = StubChat.replies[index]
        elif StubChat.replies:
            content = StubChat.replies[-1]
        else:
            content = NO_RESULTS
        usage = {"total_tokens": 7, "prompt_tokens": 5, "completion_tokens": 2}
        return types.SimpleNamespace(content=content, usage=usage, role="assistant")


def find_phrase(root, phrase):
    """The first file under `root` containing `phrase`, with the line range it spans."""
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig").split("\n")
        except OSError:
            continue
        hits = [index + 1 for index, line in enumerate(lines) if phrase in line]
        if hits:
            return str(path.resolve()), hits[0], hits[-1]
    return None


def locate_across_stages(facts, texts_dir, phrase, worker_text, parent_text):
    """At which stage the range containing `phrase` first goes missing.

    A diagnostic aid for offline replay only: the phrase is supplied by the caller and never reaches the
    product's ranking. Each stage is judged on its own evidence - the engine's reported sampled spans,
    the worker's snippets, the parent's excerpts - so a range the parent scored is never used as proof of
    what the engine sampled.
    """
    result = {"locatePhrase": phrase, "answerFile": None, "answerLines": None, "engineHasRange": None,
              "workerHasPhrase": None, "parentHasPhrase": None, "firstMissingStage": None}
    hit = find_phrase(str(texts_dir), phrase)
    if not hit:
        result["firstMissingStage"] = "corpus"
        return result
    path, first, last = hit
    result["answerFile"] = Path(path).name
    result["answerLines"] = [first, last]
    # The engine reports its spans against the snapshot copy, so correlate by file name: the library
    # path and the snapshot path differ, the file is the same one.
    spans = []
    for key, value in (facts.get("engineSampledSpans") or {}).items():
        if Path(key).name == Path(path).name:
            spans = value or []
            break
    result["engineHasRange"] = any(len(span) >= 2 and span[0] <= first and last <= span[1]
                                   for span in spans)
    result["workerHasPhrase"] = phrase in worker_text
    result["parentHasPhrase"] = phrase in parent_text
    for stage, present in (("engine", result["engineHasRange"]), ("worker", result["workerHasPhrase"]),
                           ("parent", result["parentHasPhrase"])):
        if not present:
            result["firstMissingStage"] = stage
            break
    return result


def run_case(case, texts_dir, recovery=True, locate_phrase=None):
    """One case through the real engine; returns the six observation layers."""
    work = Path(tempfile.mkdtemp(prefix="fast-replay-"))
    manifest, snapshot_warnings = pipeline.build_snapshot(str(texts_dir), work)
    query, analysis, summaries = CASES[case]
    request = pipeline.worker_request({"mode": "FAST", "query": query,
                                       "libraryId": "replay", "cacheRoot": str(work.parent),
                                       "workDir": str(work)}, work)
    request["mode"] = "FAST"
    StubChat.prompts = []
    StubChat.replies = [analysis] + list(summaries)

    import sirchmunk_compat
    holder = {}
    # The worker imported the name at module load, so patch the binding it actually calls.
    original_configure = worker.configure_retrieval

    def recording_configure(searcher):
        installed = original_configure(searcher)
        original_search = searcher.search

        async def recording_search(*args, **kwargs):
            result = await original_search(*args, **kwargs)
            holder["context"] = result
            return result

        searcher.search = recording_search
        holder["searcher"] = searcher
        return installed

    # Patch only the class the worker imports; replacing the whole `sirchmunk.llm` module breaks other
    # importers that expect its remaining names.
    import sirchmunk.llm as llm_module
    real_chat = llm_module.OpenAIChat
    llm_module.OpenAIChat = StubChat

    worker.configure_retrieval = recording_configure
    disabled = None
    if not recovery:
        # Before/after evidence: keep the retrieve-window part of the compat layer but skip the
        # FAST evidence return, which is exactly the upstream behaviour being reported on.
        disabled = sirchmunk_compat.install_fast_evidence_return
        sirchmunk_compat.install_fast_evidence_return = lambda searcher: "disabled-for-replay"
    try:
        result = asyncio.run(worker.run(request))
    finally:
        worker.configure_retrieval = original_configure
        llm_module.OpenAIChat = real_chat
        if disabled is not None:
            sirchmunk_compat.install_fast_evidence_return = disabled

    searcher = holder.get("searcher")
    context = holder.get("context") if isinstance(holder.get("context"), dict) else {}
    cluster = context.get("cluster") or {}
    facts = {
        "case": case,
        # 1. keyword analysis + file hits
        "promptCount": len(StubChat.prompts),
        "promptKinds": ["analysis" if "Return JSON only" in prompt else "summary"
                        for prompt in StubChat.prompts],
        "searchHistory": context.get("search_history"),
        "readFiles": [Path(path).name for path in (context.get("read_file_ids") or [])],
        # 2. sampled source text
        "sampledFiles": [Path(path).name for path, _ in getattr(searcher, "_dsh_sampled_evidence", [])]
        if searcher is not None else [],
        "sampledChars": [len(text) for _, text in getattr(searcher, "_dsh_sampled_evidence", [])]
        if searcher is not None else [],
        # 3. SHOULD_ANSWER / SHOULD_SAVE decisions (as the engine parsed them)
        "engineAnswerIsNoResults": (context.get("answer") or "").strip() == NO_RESULTS,
        # 4. engine cluster
        "clusterEvidences": [{"file": Path(unit.get("file_or_url") or "").name, "snippets": len(unit.get("snippets") or []),
                              "snippetChars": [len(str(s)) for s in (unit.get("snippets") or [])]}
                             for unit in (cluster.get("evidences") or [])],
        "recovery": context.get("dshFastEvidenceRecovery"),
        # 5. worker candidates
        "candidates": len(result.get("candidates") or []),
        "candidateSnippets": [len(unit.get("snippets") or []) for unit in (result.get("candidates") or [])],
        # The raw snippet text is what the parent has to choose a fragment from; keep a bounded slice
        # in the local report so the competition between fragments is visible after the fact.
        "candidateSnippetTexts": [[str(s)[:900] for s in (unit.get("snippets") or [])]
                                  for unit in (result.get("candidates") or [])],
        "warnings": result.get("warnings"),
        "usageCalls": len(result.get("usage") or []),
        # The engine stage's own account of what it sampled, as ranges rather than text.
        "engineSampledSpans": (context.get("dshFastEvidenceRecovery") or {}).get("sampledSpans"),
    }
    if searcher is not None:
        facts["engineAnswerChars"] = len(context.get("answer") or "")
    # 6. parent verification
    evidence, quoted = pipeline.verify_candidates(manifest, result.get("candidates"),
                                                  request["query"], request["libraryId"])
    snapshot_root = work / "files"
    detail = []
    for item in evidence:
        resolved = str((snapshot_root / (item.get("relativePath") or "")).resolve())
        entry = manifest.get(resolved)
        verbatim = None
        if entry:
            verbatim = item["excerpt"].strip() in Path(entry["source"]).read_text(encoding="utf-8-sig")
        detail.append({"relativePath": item["relativePath"], "startLine": item["startLine"],
                       "endLine": item["endLine"], "excerptSource": item.get("excerptSource"),
                       "excerptChars": len(item["excerpt"]),
                       "verbatimInSource": verbatim,
                       "excerpt": item["excerpt"][:400]})
    facts["parentEvidence"] = detail
    facts["evidenceParts"] = len(detail)
    if locate_phrase:
        worker_text = "\n".join(str(s) for unit in (result.get("candidates") or [])
                                for s in (unit.get("snippets") or []))
        parent_text = "\n".join(item["excerpt"] for item in evidence)
        facts.update(locate_across_stages(facts, texts_dir, locate_phrase, worker_text, parent_text))
    facts["parentQuotedWindows"] = quoted
    facts["manifestEntries"] = len(manifest)
    facts["snapshotWarnings"] = snapshot_warnings
    return facts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--texts", default=None)
    parser.add_argument("--cases", default="all")
    parser.add_argument("--no-recovery", action="store_true",
                        help="skip the FAST evidence return, to show the upstream behaviour")
    parser.add_argument("--legacy-fragment", action="store_true",
                        help="restore the previous longest-run fragment rule for comparison")
    parser.add_argument("--no-context-expansion", action="store_true",
                        help="disable the sampler's context expansion, to record the layer before it")
    parser.add_argument("--synthetic-suite", action="store_true",
                        help="run the fragment-choice case over the four synthetic corpora")
    parser.add_argument("--locate", default=None,
                        help="diagnostic aid: report at which stage the range containing this phrase "
                             "first goes missing (never used by the product's ranking)")
    parser.add_argument("--out", default=str(OUT / "report.json"))
    arguments = parser.parse_args()

    if arguments.texts:
        texts_dir = Path(arguments.texts)
    else:
        texts_dir = Path(tempfile.mkdtemp(prefix="fast-replay-texts-"))
        (texts_dir / "note-n2o5.md").write_text(
            "N2O5 adsorption at the water interface.\n"
            "The solvation free energy is -4.3 kcal/mol.\n"
            "Bulk phase dominates the hydrolysis.\n", encoding="utf-8")
    os.environ.setdefault("DSH_KNOWLEDGE_RG", str(BASE / ".local/tools/rg.exe"))
    os.environ.setdefault("DSH_KNOWLEDGE_RGA", str(BASE / ".local/tools/rga-patched/rga.exe"))
    # The chat call is stubbed, so these only satisfy the engine's configuration check. No request is
    # ever sent: the stub replaces OpenAIChat entirely.
    os.environ.setdefault("LLM_BASE_URL", "https://stub.invalid/v1")
    os.environ.setdefault("LLM_MODEL_NAME", "stub-model")
    os.environ.setdefault("LLM_API_KEY", "stub-not-used")

    wanted = list(CASES) if arguments.cases == "all" else arguments.cases.split(",")
    if arguments.no_context_expansion:
        # Record the sampler layer as it was before the context fix: identical run, one layer disabled.
        import sirchmunk_compat as compat_module
        compat_module.expand_snippet_context = lambda file_path, evidence, *args, **kwargs: evidence
    if arguments.legacy_fragment:
        pipeline.verbatim_fragment = legacy_verbatim_fragment
        # The previous code path chose exactly one run per snippet, so emulating it means the
        # complementary-fragment step must be off too - otherwise the comparison would show the new
        # behaviour on both sides.
        pipeline.select_fragments = lambda snippet, text, query=None, min_chars=8, max_parts=1: (
            ([legacy_verbatim_fragment(snippet, text, query, min_chars)]
             if legacy_verbatim_fragment(snippet, text, query, min_chars) else []), [])
    if arguments.synthetic_suite:
        reports = []
        for kind in ("answer-in-abstract", "answer-in-body", "answer-only-in-caption",
                     "answer-in-two-windows", "no-answer"):
            texts_dir = build_corpus(kind)
            try:
                facts = run_case("abstract-vs-caption", texts_dir)
            except Exception as error:
                facts = {"case": kind, "error": "%s: %s" % (type(error).__name__, error)}
            excerpt = (facts.get("parentEvidence") or [{}])[0].get("excerpt", "") if facts.get("parentEvidence") else ""
            parts = [entry.get("excerpt") or "" for entry in (facts.get("parentEvidence") or [])]
            excerpt = "\n".join(parts)
            window = "\n".join(s for group in (facts.get("candidateSnippetTexts") or []) for s in group)
            facts["corpus"] = kind
            facts["evidenceParts"] = len(parts)
            facts["windowChars"] = len(window)
            facts["windowHasClaim"] = ANSWER_MARKER in window
            facts["windowHasValue"] = VALUE_MARKER in window
            facts["chosenAnswersTheQuestion"] = ANSWER_MARKER in excerpt
            facts["chosenIsCaptionText"] = CAPTION_MARKER in excerpt or "Figure 1" in excerpt
            reports.append(facts)
        _write_and_print(reports, arguments.out)
        return 0

    reports = []
    for case in wanted:
        if case not in CASES:
            raise SystemExit("unknown case %r (known: %s)" % (case, ", ".join(CASES)))
        try:
            reports.append(run_case(case, texts_dir, recovery=not arguments.no_recovery,
                                    locate_phrase=arguments.locate))
        except Exception as error:  # a case that cannot run is itself a finding
            import traceback
            reports.append({"case": case, "error": "%s: %s" % (type(error).__name__, error),
                            "traceback": traceback.format_exc()[-1200:]})

    _write_and_print(reports, arguments.out)
    return 0


def _write_and_print(reports, out):
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"cases": reports}, indent=2, ensure_ascii=False), encoding="utf-8")
    for report in reports:
        line = json.dumps({key: report.get(key) for key in
                           ("corpus", "case", "promptKinds", "readFiles", "sampledFiles", "sampledChars",
                            "windowChars", "windowHasClaim", "windowHasValue", "evidenceParts",
                            "engineSampledSpans", "locatePhrase", "answerFile", "answerLines",
                            "engineHasRange", "workerHasPhrase", "parentHasPhrase", "firstMissingStage",
                            "engineAnswerIsNoResults", "clusterEvidences", "recovery", "candidates",
                            "warnings", "parentEvidence", "parentQuotedWindows",
                            "chosenAnswersTheQuestion", "chosenIsCaptionText", "error")},
                          ensure_ascii=False)
        # Written as UTF-8 bytes: a cp936 console cannot encode the literature text in the report.
        sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
        sys.stdout.flush()
    print("report: %s" % target)


if __name__ == "__main__":
    raise SystemExit(main())
