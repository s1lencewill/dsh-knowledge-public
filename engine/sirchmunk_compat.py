"""Workarounds pinned to Sirchmunk 0.1.0: lifecycle close, rga cache placement, FAST evidence return.

All of these reach into upstream internals that 0.1.0 does not expose publicly. If an upgrade changes
any shape the functions below raise instead of silently degrading.
"""
import asyncio
import atexit
from datetime import datetime, timezone
import hashlib
import inspect
from pathlib import Path

RGA_CACHE_PREFIX = "--rga-cache-path="
_INSTALL_ATTRIBUTES = ("_run_rga", "_run_search_process")
#: Context telemetry key the worker reads to explain a recovered FAST result to the operator.
RECOVERY_KEY = "dshFastEvidenceRecovery"
#: Class-level marker so the FAST evidence return is installed exactly once per process.
_FAST_EVIDENCE_FLAG = "_dsh_fast_evidence_installed"
#: How much context a hit line gets when upstream could not build a window of its own, and the most
#: any single region may take of the evidence budget.
_CONTEXT_BEFORE = 1
_CONTEXT_AFTER = 3
_CONTEXT_MAX_SHARE = 0.5


def configure_retrieval(searcher):
    """Keep 0.1.0 on file-producing retrieval paths with bounded text evidence.

    The direct-document shortcut creates an answer cluster whose source is the input
    directory, not the files it read. It cannot satisfy the parent's citation contract.
    The text limit leaves room for the 4,000-character query and prompt framing under
    the independent 64 KiB proxy gate. It is a retrieval window, not a transport bypass.
    """
    method = getattr(searcher, "_try_direct_doc_analysis", None)
    if (not inspect.iscoroutinefunction(method)
            or tuple(inspect.signature(method).parameters) != ("query", "paths")
            or not isinstance(getattr(searcher, "_FAST_MAX_EVIDENCE_CHARS", None), int)
            or not isinstance(getattr(searcher, "_FAST_SMALL_FILE_THRESHOLD", None), int)):
        raise RuntimeError("RETRIEVAL_CONTRACT_CHANGED")

    async def no_direct_answer(query, paths):
        return None

    searcher._try_direct_doc_analysis = no_direct_answer
    # ROI_RESULT_SUMMARY inserts text_content twice. At worst JSON takes six bytes
    # per character: 2*3,000*6 + 4,000*6 + template/framing remains below 64 KiB.
    searcher._FAST_MAX_EVIDENCE_CHARS = 3_000
    # Larger documents use keyword-centred sampling instead of only their opening.
    searcher._FAST_SMALL_FILE_THRESHOLD = 3_000

    return install_fast_evidence_return(searcher)


def install_fast_evidence_return(searcher):
    """Return FAST's already-read evidence instead of dropping or paraphrasing it.

    Sirchmunk 0.1.0 ends `_search_fast` in two places without a cluster: when the model answered but
    declined to cache (`SHOULD_SAVE=false`) and when it did not accept the evidence. The caller then
    either builds a cluster whose only "snippet" is the *model's own prose* - which the parent cannot
    verbatim-verify - or, for the no-results sentinel, keeps `cluster=None` so the query reports no
    candidates at all even though files were located and read.

    Neither outcome is acceptable here: the parent's job is to verify what the engine actually read,
    which is independent of the engine's caching or answering decision. This installs a recorder for
    the sampled text and wraps `_search_fast` so that, when upstream returns no cluster, a
    **non-persisted** cluster is rebuilt from exactly the files this run read (sampled text when we
    have it, otherwise the file with no snippet so the parent quotes its own marked window).

    What this deliberately does not do: force `SHOULD_SAVE`/`SHOULD_ANSWER`, persist a cluster the
    model declined, widen the search beyond the files this run read, or change the returned answer.
    """
    try:
        from sirchmunk.search import AgenticSearch
    except Exception:  # a stubbed package (tests) has no real engine class to be an instance of
        AgenticSearch = ()
    if not isinstance(searcher, AgenticSearch):
        # Not the real engine: nothing to adapt, and the drift guards below would only misfire.
        return "skipped:not-the-real-engine"
    if getattr(AgenticSearch, _FAST_EVIDENCE_FLAG, None):
        return "already-installed"
    for name in ("_search_fast", "_fast_sample_evidence", "_build_fast_cluster"):
        if not hasattr(AgenticSearch, name):
            raise RuntimeError("FAST evidence contract: sirchmunk 0.1.0 changed %s; re-verify the pin" % name)
    expected = ("query", "paths", "max_depth", "top_k_files", "enable_dir_scan", "include", "exclude",
                "llm_fallback")
    # Patched on the class, not the instance: a caller that replaces one of these per instance (tests
    # use mocks) must keep seeing its own object instead of a wrapper it did not install.
    original_search = AgenticSearch._search_fast
    original_sample = AgenticSearch._fast_sample_evidence
    if (not inspect.iscoroutinefunction(original_search)
            or tuple(inspect.signature(original_search).parameters)[1:] != expected):
        raise RuntimeError("FAST evidence contract: _search_fast signature changed; re-verify the pin")
    if not inspect.iscoroutinefunction(original_sample):
        raise RuntimeError("FAST evidence contract: _fast_sample_evidence is not a coroutine")
    if tuple(inspect.signature(AgenticSearch._build_fast_cluster).parameters) != (
            "query", "answer", "file_path", "evidence", "keywords"):
        raise RuntimeError("FAST evidence contract: _build_fast_cluster signature changed")

    async def recording_sample(self, file_path, matches):
        evidence = await original_sample(self, file_path, matches)
        raw_chars = len(evidence or "")
        numbered = sum(1 for match in (matches or ())
                       if isinstance(match.get("data"), dict)
                       and isinstance(match["data"].get("line_number"), int))
        if getattr(self, "_dsh_match_facts", None) is None:
            self._dsh_match_facts = []
        self._dsh_match_facts.append([len(matches or ()), numbered])
        if evidence and _snippet_mode(matches) and _is_snippet_evidence(evidence, matches):
            # Upstream had no line numbers to build a window from, so it fell back to "one matched line
            # per hit". A sentence split over two extracted lines then loses its continuation - and with
            # it the value the sentence states. Rebuild the evidence with the lines around each hit.
            expanded = expand_snippet_context(
                file_path, evidence, getattr(self, "_FAST_MAX_EVIDENCE_CHARS", 3_000))
            if expanded != evidence:
                if getattr(self, "_dsh_context_expansions", None) is None:
                    self._dsh_context_expansions = []
                self._dsh_context_expansions.append(str(file_path))
                evidence = expanded
        if evidence:
            if getattr(self, "_dsh_sampled_evidence", None) is None:
                self._dsh_sampled_evidence = []
            self._dsh_sampled_evidence.append((str(file_path), str(evidence)))
        if getattr(self, "_dsh_evidence_sizes", None) is None:
            self._dsh_evidence_sizes = []
        self._dsh_evidence_sizes.append([raw_chars, len(evidence or "")])
        if getattr(self, "_dsh_sampled_spans", None) is None:
            self._dsh_sampled_spans = {}
        spans, unlocatable = locate_evidence_spans(file_path, evidence)
        self._dsh_sampled_spans[str(file_path)] = spans
        self._dsh_sampled_unlocatable = (getattr(self, "_dsh_sampled_unlocatable", 0) or 0) + unlocatable
        return evidence

    async def search_fast_with_evidence(self, query, paths, *, max_depth=None, top_k_files=3,
                                        enable_dir_scan=False, include=None, exclude=None,
                                        llm_fallback=False):
        self._dsh_sampled_evidence = []
        self._dsh_context_expansions = []
        self._dsh_evidence_sizes = []
        self._dsh_match_facts = []
        self._dsh_sampled_spans = {}
        self._dsh_sampled_unlocatable = 0
        answer, cluster, context = await original_search(
            self, query, paths, max_depth=max_depth, top_k_files=top_k_files,
            enable_dir_scan=enable_dir_scan, include=include, exclude=exclude,
            llm_fallback=llm_fallback)
        if cluster is not None:
            return answer, cluster, context
        sampled = list(getattr(self, "_dsh_sampled_evidence", ()) or ())
        read_files = [str(path) for path in (getattr(context, "read_file_ids", None) or ())]
        if not sampled and not read_files:
            return answer, cluster, context  # nothing was read: this really is a no-match
        grounded = _grounded_cluster(self, query, answer, sampled, read_files)
        response = answer or ""
        accepted = response.strip().lower() != "no results found."
        _mark_recovery(context, {
            "reason": "cache-skipped-by-model" if accepted else "engine-rejected-evidence",
            "filesRead": len(read_files),
            "filesWithSampledEvidence": len(sampled),
            "filesContextExpanded": len(getattr(self, "_dsh_context_expansions", ()) or ()),
            # Per file: [evidence chars the sampler produced, chars after context expansion] and
            # [matches, matches that carried a line number]. Counts only - never source text.
            "evidenceChars": list(getattr(self, "_dsh_evidence_sizes", ()) or ()),
            "matchFacts": list(getattr(self, "_dsh_match_facts", ()) or ()),
            # What the engine sampled, as spans per file (locatable ones only) and how many sampled
            # lines could not be located at all.
            "sampledSpans": {name: [list(span) for span in spans]
                             for name, spans in (getattr(self, "_dsh_sampled_spans", {}) or {}).items()},
            "sampledUnlocatable": getattr(self, "_dsh_sampled_unlocatable", 0) or 0,
            "engineAccepted": accepted,
        })
        return answer, grounded, context

    AgenticSearch._fast_sample_evidence = recording_sample
    AgenticSearch._search_fast = search_fast_with_evidence
    AgenticSearch._dsh_sampled_evidence = []
    AgenticSearch._dsh_fast_evidence_installed = True
    return "fast-evidence-return"


def _snippet_mode(matches):
    """True when upstream could not build context windows, i.e. no match carried a line number.

    This is upstream's own fallback condition (`_fast_sample_evidence` collects `line_number` and
    falls back to one matched line per hit when it finds none), so it is read the same way here rather
    than guessed from the evidence text.
    """
    for match in matches or ():
        data = match.get("data") if isinstance(match, dict) else None
        if isinstance(data, dict) and isinstance(data.get("line_number"), int):
            return False
    return True


def _is_snippet_evidence(evidence, matches):
    """True when the evidence is exactly upstream's "one matched line per hit" fallback.

    Upstream has other answers for the same situation - when the snippet evidence is thin it returns the
    head of the file instead, which can be richer than any window around a hit. Rebuilding *that* from
    located hits would throw content away, so the expansion applies only to the fallback shape: every
    line of the body has to be one of the matched lines.
    """
    body = [line for line in (evidence or "").split("\n")[1:] if line.strip()]
    if not body:
        return False
    wanted = set()
    for match in matches or ():
        data = match.get("data") if isinstance(match, dict) else None
        text = data.get("lines", {}).get("text", "") if isinstance(data, dict) else ""
        if text:
            wanted.add(" ".join(str(text).split()))
    if not wanted:
        return False
    return all(" ".join(line.split()) in wanted for line in body)


def _locate_line(lines, hit, used):
    """Index of the file line a hit came from, skipping lines an earlier hit already claimed."""
    needle = " ".join(hit.split())
    if not needle:
        return None
    probe = needle[:30]
    for index, line in enumerate(lines):
        if index in used:
            continue
        if " ".join(line.split()) == needle or " ".join(line.split()).startswith(probe):
            used.add(index)
            return index
    return None


def locate_evidence_spans(file_path, evidence):
    """The line spans the sampled evidence occupies in the file, plus how many were unlocatable.

    This is the engine stage's own account of what it sampled: a region that cannot be located is
    counted, never given a made-up range. Only ranges and sizes are produced - no text.
    """
    body = [line for line in (evidence or "").split("\n")[1:] if line.strip()]
    if not body:
        return [], 0
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return [], len(body)
    used, spans, unlocatable, located = set(), [], 0, []
    for line in body:
        index = _locate_line(source, line, used)
        if index is None:
            unlocatable += 1
            continue
        located.append(index)
    # Hits come back in match order, not document order, so sort before merging neighbours.
    for index in sorted(located):
        if spans and index + 1 <= spans[-1][1]:
            spans[-1] = [spans[-1][0], max(spans[-1][1], index + 1)]
            continue
        spans.append([index + 1, index + 1])
    return spans, unlocatable


def expand_snippet_context(file_path, evidence, max_chars, before=_CONTEXT_BEFORE,
                           after=_CONTEXT_AFTER, max_share=_CONTEXT_MAX_SHARE):
    """Give one-line-per-hit evidence the surrounding lines of the file it came from.

    A sentence in an extracted PDF text is commonly split across two lines ("... hydrolysis
    contributes" / "only 1%."). Upstream's snippet fallback keeps one matched line per hit and nothing
    else, so the continuation - and the number it carries - never reaches the engine's window, no
    matter how the caller later chooses among the fragments it did receive.

    Each hit line is located in the file and given a small window of surrounding lines; windows that
    touch are merged, and they are emitted in document order. The byte budget is unchanged, and no
    single region may take more than `max_share` of it, so a term that occurs everywhere cannot crowd
    out the scattered hits. Text returned here is verbatim file content: the parent still verifies
    every excerpt against the original, and a hit line that cannot be located leaves the evidence as
    the engine produced it.
    """
    body = (evidence or "").split("\n")
    header = body[0] if body and body[0].startswith("[") and body[0].rstrip().endswith("]") else ""
    hits = [line.rstrip() for line in (body[1:] if header else body) if line.strip()]
    if not hits:
        return evidence
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return evidence

    used, located = set(), []
    for hit in hits:
        index = _locate_line(source, hit, used)
        if index is not None:
            located.append(index)
    if not located:
        return evidence
    # Hits come back in match order, not document order (a real window was 76, 319, 14, 163), so sort
    # before merging: merging against the previous hit alone silently drops a window that sits earlier
    # in the file - which was exactly the window holding the sentence's continuation.
    regions = []
    for index in sorted(located):
        start, end = max(0, index - before), min(len(source), index + after + 1)
        if regions and start <= regions[-1][1]:
            regions[-1] = (regions[-1][0], max(regions[-1][1], end))
        else:
            regions.append((start, end))
    if not regions:
        return evidence

    share = max_chars if len(regions) == 1 else max(200, int(max_chars * max_share))
    blocks, total = [], 0
    for start, end in regions:
        block_lines, block_chars = [], 0
        for line in source[start:end]:
            if block_chars + len(line) + 1 > share:
                break
            block_lines.append(line)
            block_chars += len(line) + 1
        if not block_lines:
            block_lines, block_chars = [source[start]], len(source[start]) + 1
        if total + block_chars > max_chars:
            break
        blocks.append("\n".join(block_lines))
        total += block_chars + 1
    if not blocks:
        return evidence
    expanded = "\n\n".join(blocks)
    return "%s\n%s" % (header, expanded) if header else expanded


def _mark_recovery(context, facts):
    """Attach the recovery facts to the context telemetry that `to_dict()` exports."""
    telemetry = getattr(context, "telemetry", None)
    if not isinstance(telemetry, dict):
        telemetry = {}
        context.telemetry = telemetry
    telemetry[RECOVERY_KEY] = facts


def _grounded_cluster(searcher, query, answer, sampled, read_files):
    """One EvidenceUnit per file this run read; snippets only from text we actually sampled."""
    from sirchmunk.schema.knowledge import EvidenceUnit

    units, seen = [], set()
    for file_path, evidence in sampled:
        built = searcher._build_fast_cluster(query, answer or "", file_path, evidence, [])
        unit = (built.evidences or [None])[0]
        if unit is None:
            continue
        units.append(unit)
        seen.add(str(file_path))
    for file_path in read_files:
        if file_path in seen:
            continue
        units.append(EvidenceUnit(
            doc_id=hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:12],
            file_or_url=file_path,
            # No sampled text: the parent quotes its own window rather than trusting prose here.
            summary="",
            is_found=True,
            snippets=[],
            extracted_at=datetime.now(timezone.utc),
        ))
        seen.add(file_path)
    base = searcher._build_fast_cluster(query, answer or "", str(units[0].file_or_url), "", [])
    base.evidences = units
    base.search_results = [str(unit.file_or_url) for unit in units]
    return base


async def close_engine(searcher, llm):
    if searcher is not None:
        tasks = list(getattr(searcher, "_background_tasks", ()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        storage = searcher.knowledge_storage
        # 0.1.0 close() does not unregister atexit and __del__ calls close again.
        # Stop/sync while the temporary directory still exists, then disarm both.
        storage.close()
        atexit.unregister(storage._shutdown_parquet_sync)
        storage.db = None
    if llm is not None:
        llm._client.close()
        await llm._async_client.close()


def with_rga_cache(args, cache_flag):
    """Return `args` carrying the rga cache flag unless the caller already chose a cache policy."""
    args = list(args or [])
    if any(argument == "--rga-no-cache" or argument.startswith(RGA_CACHE_PREFIX) for argument in args):
        return args
    return args + [cache_flag]


def install_rga_cache_path(cache_dir):
    """Point every Sirchmunk rga invocation at a cache directory the caller can write.

    Sirchmunk 0.1.0 passes `--rga-cache-path` only on its keyword path. The async filename path
    (`list_files` -> `_run_rga_async` -> `_run_search_process("rga")`) calls rga bare, so rga
    resolves its own cache through the Windows known-folder API. Inside an AppContainer that
    lookup fails and rga exits 101 with "could not get cache path: no home directory found",
    after which Sirchmunk silently degrades to its rg fallback. Injecting the documented flag is
    the fix; granting the container access to a user directory is not.

    Returns the flag that was installed, and is idempotent.
    """
    try:
        from sirchmunk.retrieve.text_retriever import GrepRetriever
    except Exception as error:  # pragma: no cover - the pin guarantees the module exists
        raise RuntimeError("rga cache compat: sirchmunk.retrieve.text_retriever is unavailable") from error
    for attribute in _INSTALL_ATTRIBUTES:
        if not hasattr(GrepRetriever, attribute):
            raise RuntimeError("rga cache compat: sirchmunk 0.1.0 changed %s; re-verify the pin" % attribute)
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    cache_flag = RGA_CACHE_PREFIX + str(directory)
    if getattr(GrepRetriever, "_dsh_rga_cache_flag", None) == cache_flag:
        return cache_flag

    original_sync = GrepRetriever.__dict__["_run_rga"].__func__
    original_search = GrepRetriever.__dict__["_run_search_process"].__func__

    def sync_rga(args, json_output=True):
        return original_sync(with_rga_cache(args, cache_flag), json_output=json_output)

    async def search_process(command, args, **kwargs):
        prepared = with_rga_cache(args, cache_flag) if command == "rga" else list(args or [])
        return await original_search(command, prepared, **kwargs)

    GrepRetriever._run_rga = staticmethod(sync_rga)
    GrepRetriever._run_search_process = staticmethod(search_process)
    GrepRetriever._dsh_rga_cache_flag = cache_flag
    return cache_flag
