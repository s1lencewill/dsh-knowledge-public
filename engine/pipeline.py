"""Trusted parent-side query pipeline.

Only this module (running in the service parent, or in a harness that acts as the parent) touches
the original library:

1. `build_snapshot` copies the library into the job directory and keeps the manifest in memory;
2. the engine receives `worker_request(...)`, which contains the snapshot directory and the query
   but **no path into the library**, so an isolated engine never needs library access;
3. `finalize` re-verifies every candidate the engine returned against the original file (SHA256 +
   verbatim text) and only then produces citations. Engine output is untrusted input.

Splitting the work this way is what makes AppContainer-based isolation possible later: the engine
process only needs read access to `snapshotDir` and write access to `stateDir`.
"""
from pathlib import Path
import asyncio
import math
import re

from evidence import SnapshotCapacityError, snapshot, verify
import source_card

EXCERPT_LINES = 12
EXCERPT_CONTEXT_LINES = 2
FRAGMENT_MIN_CHARS = 8
#: What this service is for, stated once per result.
#:
#: This replaces a line that read "final answer synthesis and complete path isolation are pending". Both halves
#: were wrong, and wrong in the worst place - every search result the model reads before composing its answer:
#:
#: * answer synthesis is not a pending capability, it is the **agreed division of labour**: this service returns
#:   verified excerpts and the calling Agent writes the answer from them (`docs/windows-text-candidate.md`);
#: * engine isolation is not pending either, and whether it is active is a **per-job fact** that only the
#:   service can observe - it is passed in as `isolation` below rather than asserted here. A static claim would
#:   be wrong in one deployment or the other, and "pending" was wrong in both.
EXCERPTS_ONLY = ("M0: verified excerpts only; this service does not compose an answer - the calling Agent "
                 "writes it from these excerpts")

#: The isolation line for `run_local`, which is a harness entry point: it runs the engine in a child process of
#: the caller and never places it in a container. Saying so keeps the absence of an isolation claim from being
#: read as "isolation was fine".
HARNESS_ISOLATION = ("ENGINE_ISOLATION: not applied - this entry point runs the engine as the caller's own "
                     "child process (the service is the layer that isolates)")
# Where a citation's text came from. Callers must not treat a parent-side window as a minimal
# fragment: only "engine" and "engine-fragment" are verbatim upstream excerpts.
ENGINE_EXCERPT = "engine"
ENGINE_FRAGMENT = "engine-fragment"
SOURCE_WINDOW = "source-window"


class SnapshotMissing(Exception):
    """The parent did not prepare a snapshot for this query."""


def build_snapshot(root, work_dir, max_file_bytes=None):
    """Copy the library into `<work_dir>/files`. Raises SnapshotCapacityError, never truncates.

    `max_file_bytes` is the library's resolved ceiling (`capacity.resolve`); omitting it means the
    default, which is what a harness with no library entry to read gets. There is deliberately no way to
    ask for "no limit": the value is validated by `evidence.byte_limit` and a nonsense value raises
    rather than silently becoming the default.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    if max_file_bytes is None:
        return snapshot(root, work_dir / "files")
    return snapshot(root, work_dir / "files", max_file_bytes)


def capacity_failure(error):
    """Fail closed: no partial snapshot is ever searched, and no model client is constructed."""
    return {"status": "failed", "answer": "", "evidence": [], "warnings": [str(error)]}


def worker_request(request, work_dir):
    """The child request: snapshot directory and query only, with every library path removed."""
    work_dir = Path(work_dir)
    snapshot_dir = work_dir / "files"
    if not snapshot_dir.is_dir():
        raise SnapshotMissing("SNAPSHOT_MISSING: the parent must build the snapshot first")
    return {
        "mode": request["mode"],
        "query": request["query"],
        "libraryId": request["libraryId"],
        "cacheRoot": request.get("cacheRoot", str(work_dir.parent)),
        "workDir": str(work_dir),
        "snapshotDir": str(snapshot_dir),
        "stateDir": str(work_dir / "state"),
    }


def snippet_text(snippet):
    """Source text of one upstream snippet.

    Sirchmunk 0.1.0 returns `snippets` as plain strings, each prefixed with a "[path]" header that
    is not part of the source text. Other shapes use a dict with a "snippet" key. Anything else is
    ignored rather than guessed at, because unverifiable text must never become evidence.
    """
    if isinstance(snippet, dict):
        text = snippet.get("snippet") or snippet.get("text") or ""
    elif isinstance(snippet, str):
        text = snippet
    else:
        return ""
    if not isinstance(text, str):
        return ""
    lines = text.splitlines()
    if len(lines) > 1 and lines[0].startswith("[") and lines[0].rstrip().endswith("]"):
        text = "\n".join(lines[1:])
    return text.strip()


def source_text(source):
    """The original file as normalised text (the bytes verify() hashes)."""
    return Path(source).read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


#: A line that opens like a figure/table caption rather than prose. Deliberately shallow and
#: format-agnostic: it is a *preference* between fragments, never a filter, because for some queries a
#: caption really is the only place the answer appears.
#: A caption starts with one of these labels. Plural and abbreviated forms are included: the caption
#: block that produced the wrong citation opened with "Panels show ...", and a singular-only pattern
#: silently treated that line as prose. Still purely label-based - no document name, number or wording.
_CAPTION_LINE = re.compile(
    r"^\s*(?:fig(?:ure)?s?\.?|figs?\.?|tab(?:le)?s?\.?|tabs?\.?|panels?|extended\s+data|"
    r"supplementary\s+(?:fig(?:ure)?s?|tab(?:le)?s?))\b",
    re.IGNORECASE)
#: A page marker from the extraction, and the bound on how far a caption may run. Both exist so that
#: prose after a caption is not swallowed when the extraction contains no blank line to stop at.
_CAPTION_PAGE_MARKER = re.compile(r"^\s*PDF\s+page\s+\d+\s*$", re.IGNORECASE)
_CAPTION_MAX_LINES = 25


def _caption_lines(text):
    """1-based line numbers of every line that belongs to a caption block, read from the document.

    A caption is a label line plus the lines that continue it, and those continuation lines carry no
    label of their own - which is exactly how the wrong citation was produced ("Fig. 4 | ... at the" /
    "interface. a, b Free energy surfaces ..."). Classifying a run by its first line alone therefore
    misses every continuation line, so the block is reconstructed here from the original text.

    The block ends at a blank line, at a page marker, or after a bounded number of lines. That bound is
    what keeps ordinary prose after a caption from being treated as caption text in PDF extractions
    that contain no blank lines at all.
    """
    lines = text.split("\n")
    captions = set()
    index = 0
    while index < len(lines):
        if not _CAPTION_LINE.match(lines[index]):
            index += 1
            continue
        captions.add(index + 1)
        cursor = index
        while cursor + 1 < len(lines) and (cursor + 1 - index) < _CAPTION_MAX_LINES:
            following = lines[cursor + 1]
            if not following.strip() or _CAPTION_PAGE_MARKER.match(following):
                break
            cursor += 1
            captions.add(cursor + 1)
        index = cursor + 1
    return captions


def _verbatim_line(text, block):
    """The 1-based line `block` starts on, or None when it is not a line-aligned substring of `text`."""
    start = 0
    while True:
        found = text.find(block, start)
        if found < 0:
            return None
        if found == 0 or text[found - 1] == "\n":
            return text.count("\n", 0, found) + 1
        start = found + 1


def _verbatim_spans(lines, text, min_chars):
    """Every contiguous verbatim line run of at least `min_chars`, longest first per starting line.

    Spans are trimmed to non-empty lines, so two passages an extraction separated with a blank line are
    two runs rather than one padded run that overlaps both.
    """
    for start in range(len(lines)):
        for end in range(len(lines), start, -1):
            while end > start and not lines[end - 1].strip():
                end -= 1
            if end <= start or not lines[start].strip():
                continue
            block = "\n".join(lines[start:end]).strip()
            if len(block) < min_chars:
                continue
            if block in text:
                yield start, end, block
                break  # any shorter run from this start is a subset of the one just yielded


def _verbatim_runs(lines, text, min_chars):
    """The blocks of `_verbatim_spans`, for callers that do not need the line offsets."""
    for _start, _end, block in _verbatim_spans(lines, text, min_chars):
        yield block


#: An ASCII word, a run of CJK characters, and a quantity. A Chinese question shares no characters with
#: an English passage, so a feature set built from ASCII words alone collapses onto whichever entity
#: name happens to be spelled in ASCII - which is how a caption that literally repeats that name
#: outranked the sentence that answered the question.
_ASCII_FEATURE = re.compile(r"[0-9a-z][0-9a-z_\-.]{1,}", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_QUANTITY = re.compile(r"\d+(?:[.,]\d+)?\s*(%|‰|per\s*cent|percent|倍|成)?", re.IGNORECASE)
#: A sentence end, used only to decide how far a chosen run may be completed.
_SENTENCE_END = re.compile(r"""[.!?][)\]}"'’”]?\s*$""")
_SENTENCE_COMPLETION_LINES = 6
#: How many complementary fragments one file may contribute, and the ceiling on their combined size.
MAX_FRAGMENTS_PER_FILE = 2
FRAGMENT_BUDGET_FACTOR = 2
FRAGMENT_BUDGET_MIN_CHARS = 600


def _normalise_for_match(value):
    """Case-folded and whitespace-free, so a feature matches across an extraction's line wrapping."""
    return re.sub(r"\s+", "", value or "").lower()


def query_features(query):
    """Features of a question in any script: ASCII words, CJK bigrams and quantities.

    Bigrams rather than single characters keep a Chinese phrase specific enough to weigh; a quantity
    keeps its unit, so "1%" is one feature and not a bare "1" that occurs everywhere.
    """
    text = query or ""
    features = set()
    for match in _ASCII_FEATURE.finditer(text):
        token = match.group(0).lower()
        # A purely numeric token is a quantity, handled with its unit below; an alphanumeric identifier
        # such as an entity name is a word and must stay one.
        if not re.fullmatch(r"[0-9][0-9.,]*", token):
            features.add(token)
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            features.add(run)
        else:
            features.update(run[index:index + 2] for index in range(len(run) - 1))
    features.update(_quantity_features(text))
    return features


def _quantity_features(text):
    """Quantities a question actually asks about: a number carrying a unit, or a decimal value.

    A bare integer inside an identifier or a year ("note-03", "SO2", "2026") is not a quantity: taking
    it as one would reintroduce exactly the ubiquitous-token noise the weighting exists to remove.
    """
    features = set()
    for match in _QUANTITY.finditer(text or ""):
        token = match.group(0).strip()
        if not any(character.isdigit() for character in token):
            continue
        if (match.group(1) or "").strip():
            features.add(re.sub(r"\s+", "", token).lower())
        elif re.fullmatch(r"\d+[.,]\d+", token):
            features.add(token.lower())
    return features


def _feature_weights(text, features):
    """One weight per feature from how rare it is in this document.

    A term the document repeats on every other line ("the entity the paper is about") carries almost no
    information about *which* passage answers a question, while a term that occurs once or twice does.
    This is what stops a small number of high-frequency ASCII words from deciding the ranking.
    """
    normalised_lines = [_normalise_for_match(line) for line in (text or "").split("\n")]
    total = max(1, len(normalised_lines))
    weights = {}
    for feature in features:
        needle = _normalise_for_match(feature)
        count = sum(1 for line in normalised_lines if needle in line)
        weights[feature] = round(math.log(1.0 + (total + 1.0) / (count + 1.0)), 4)
    return weights


def _fragment_score(normalised_features, weights, block):
    """Share of the query's *weighted* features this block carries.

    Literal overlap only: it decides which passage is quoted, and is never a statement that the passage
    supports an answer. A block can score high and still not answer the question.
    """
    total = sum(weights.values())
    if not total:
        return 0.0
    lowered = _normalise_for_match(block)
    hit = sum(weight for feature, weight in weights.items()
              if normalised_features[feature] in lowered)
    return round(hit / total, 4)


def _complete_sentence(lines, start, end, text, limit=_SENTENCE_COMPLETION_LINES):
    """Grow a run to the sentence containing it, while it stays verbatim in the file.

    A run that stops mid-sentence drops whatever the rest of the sentence says - the negation, the
    comparison or the unit ("... hydrolysis contributes" / "only 1%."), and a run that starts
    mid-sentence drops its subject. Growth is bounded and reverts as soon as the text would no longer be
    a verbatim run of the file, so nothing is ever assembled from discontiguous places.
    """
    block = "\n".join(lines[start:end]).strip()
    for _ in range(limit):
        if end >= len(lines) or not lines[end].strip() or _SENTENCE_END.search(lines[end - 1].strip()):
            break
        grown = "\n".join(lines[start:end + 1]).strip()
        if grown not in text:
            break
        block, end = grown, end + 1
    for _ in range(limit):
        if start <= 0 or not lines[start - 1].strip() or _SENTENCE_END.search(lines[start - 1].strip()):
            break
        grown = "\n".join(lines[start - 1:end]).strip()
        if grown not in text:
            break
        block, start = grown, start - 1
    return block, start, end


def _closes_a_sentence(text):
    """True when the text ends a statement and does not stop inside a hyphenated word.

    An extraction wraps a word across lines, so a quoted range can end "... which quantifies the frac-".
    That is a cut-off word, not the end of anything, and it must not be presented as a complete quote.
    """
    trimmed = (text or "").rstrip()
    if not trimmed:
        return False
    if re.search(r"[A-Za-z]-$", trimmed):
        return False
    return _SENTENCE_END.search(trimmed) is not None


def _close_the_sentence(lines, start, end, text):
    """Prefer a boundary that ends a sentence, using only lines that are verbatim in the file.

    First the run is extended to the sentence it belongs to (so a continuation, a negation or a unit is
    not cut off). If it still does not close, whole trailing lines are given back until the quote ends a
    real sentence. When neither is possible the caller is told `False` and marks the citation truncated -
    text is never invented to make a quote look complete.
    """
    block, cstart, cend = _complete_sentence(lines, start, end, text)
    if _closes_a_sentence(block):
        return block, cstart, cend, True
    for cut in range(cend - 1, cstart, -1):
        candidate = "\n".join(lines[cstart:cut]).strip()
        if (len(candidate) >= FRAGMENT_MIN_CHARS and candidate in text
                and _closes_a_sentence(candidate)):
            return candidate, cstart, cut, True
    return block, cstart, cend, False


def select_fragments(snippet, text, query=None, min_chars=FRAGMENT_MIN_CHARS,
                     max_parts=MAX_FRAGMENTS_PER_FILE):
    """The most relevant verbatim runs of the engine's snippet, best first.

    Returns `(parts, ranked)`: the blocks to quote (up to `max_parts`, non-overlapping, inside a bounded
    excerpt budget) and the scored candidate runs with the reason each unselected one was dropped. The
    reasons are metadata, which is what lets the parent explain a poor citation afterwards without
    recording any text.

    Ranking: weighted query-feature score first, then prose before caption as a *tie-break only*, then
    length. No category is prioritised or excluded outright - a title, an abstract, body prose or a
    caption can all be the passage that answers the question.

    The second fragment is **not** required to match a query word. The engine's window is already a
    bounded, relevance-driven selection, and demanding a new feature dropped exactly the continuations a
    quote needs: the other half of a range, the line carrying a value's unit, the sentence that states the
    negation. What bounds the output instead is the per-file fragment cap and the shared excerpt budget,
    and a fragment already contained in the selection is never repeated.
    """
    if not snippet or not text:
        return [], []
    lines = snippet.split("\n")
    features = query_features(query)
    normalised_features = {feature: _normalise_for_match(feature) for feature in features}
    weights = _feature_weights(text, features)
    captions = _caption_lines(text)
    ranked = []
    for start, end, _block in _verbatim_spans(lines, text, min_chars):
        block, cstart, cend, complete = _close_the_sentence(lines, start, end, text)
        first_line = _verbatim_line(text, block)
        is_caption = (_CAPTION_LINE.match(block) is not None
                      or (first_line is not None and first_line in captions))
        ranked.append({"block": block, "chars": len(block), "firstLine": first_line,
                       "endLine": None if first_line is None else first_line + block.count("\n"),
                       "start": cstart, "end": cend, "complete": complete,
                       "score": _fragment_score(normalised_features, weights, block),
                       "caption": is_caption})
    ranked.sort(key=lambda item: (item["score"], 0 if item["caption"] else 1, item["chars"]),
                reverse=True)
    # Identical spans can fall out of the run enumeration more than once (a trailing blank line trims
    # away); one entry per range keeps the record and the log cap meaningful.
    unique, seen_spans = [], set()
    for item in ranked:
        key = (item["start"], item["end"], item["firstLine"], item["endLine"])
        if key in seen_spans:
            continue
        seen_spans.add(key)
        unique.append(item)
    ranked = unique

    parts, claimed, selected_chars, chosen_text = [], set(), 0, ""
    budget = max(FRAGMENT_BUDGET_MIN_CHARS, FRAGMENT_BUDGET_FACTOR * (ranked[0]["chars"] if ranked else 0))
    for item in ranked:
        item["selected"] = False
        item["reason"] = None
    for item in ranked:
        if len(parts) >= max_parts:
            item["reason"] = "enough_parts"
            continue
        span = set(range(item["start"], item["end"]))
        if span & claimed:
            item["reason"] = "overlaps_selected"
            continue
        if item["block"] in chosen_text:
            item["reason"] = "contained_in_selected"
            continue
        if parts and selected_chars + item["chars"] > budget:
            item["reason"] = "over_budget"
            continue
        item["selected"] = True
        parts.append(item["block"])
        claimed |= span
        chosen_text = "\n".join(parts)
        selected_chars += item["chars"]
    for item in ranked:
        if not item["selected"] and item["reason"] is None:
            item["reason"] = "lower_rank"
    return parts, ranked


def verbatim_fragment(snippet, text, query=None, min_chars=FRAGMENT_MIN_CHARS):
    """The single most relevant verbatim run - the first part of `select_fragments`."""
    parts, _ranked = select_fragments(snippet, text, query, min_chars=min_chars, max_parts=1)
    return parts[0] if parts else ""


def source_window(source, query, context_lines=EXCERPT_CONTEXT_LINES, limit=EXCERPT_LINES):
    from markdown_windows import choose
    text = source_text(source)
    features = set(sorted(query_features(query))[:128])
    weights = _feature_weights(text, features)
    normalised = {f: _normalise_for_match(f) for f in features}
    return choose(text, query, lambda block: _fragment_score(normalised, weights, block), context_lines, limit)


def source_excerpt(source, query, context_lines=EXCERPT_CONTEXT_LINES, limit=EXCERPT_LINES):
    """Bounded original-text window; structure and lexical ranking never assess support."""
    return source_window(source, query, context_lines, limit)[0]


def _locate_span(text, block):
    """The line span `block` occupies in `text`, or None when it cannot be located verbatim."""
    line = _verbatim_line(text, block)
    if line is None:
        return None
    return line, line + block.count("\n")


def _fragment_layers(engine_spans, worker_spans, runs, file_id):
    """The same file as the engine sampling, the worker output and the parent scoring saw it.

    Only ranges, sizes and a locatable flag are kept - never text - and a fragment that cannot be
    located says so instead of being given a range. Each layer carries how its ranges were obtained,
    because they are not equally trustworthy:

    * `engine` is *self-reported by the engine* and re-verified by nobody - the parent has no way to tell
      whether the engine really sampled those lines, so `locatable` there means only "the reported range
      is inside the file", not "the parent confirmed it was read";
    * `worker` is *located by the parent* against the snapshot, so a `locatable` range there is a fact the
      parent checked;
    * `parent` is the parent's own scored runs.

    These lists are diagnostics. They never enter a citation, and a range the parent scored is not evidence
    of what the engine sampled (nor the other way round).
    """
    markers = {"engine": "engine-reported", "worker": "parent-located", "parent": "parent-scored"}
    layers = []
    for name, entries in (("engine", engine_spans), ("worker", worker_spans), ("parent", runs)):
        fragments = []
        for index, entry in enumerate(entries or []):
            if name == "parent":
                start = entry.get("firstLine")
                end = entry.get("endLine")
                size = entry.get("chars")
            else:
                values = list(entry)
                start, end = values[0], values[1]
                size = values[2] if len(values) > 2 else None
            fragments.append({"fragmentId": "%s-%02d" % (name, index), "fileId": file_id,
                              "startLine": start, "endLine": end, "chars": size,
                              "locatable": start is not None})
        layers.append({"layer": name, "verification": markers[name], "fragments": fragments})
    return layers


def verify_candidates(manifest, candidates, query, library_id, diag=None, engine_facts=None, handles=None):
    """Turn engine candidates into citations. Returns (evidence, windowQuotedCount).

    Preference order, tightest first: the engine's own snippet verbatim, the verbatim fragment(s) chosen
    inside that snippet, then a parent-side window around the query (explicitly marked). One file may
    contribute up to `MAX_FRAGMENTS_PER_FILE` complementary fragments - two passages that each carry a
    query feature the other lacks - so answering a question whose evidence is split across two places
    does not require dropping one of them. Each entry is separately verified and separately line-ranged,
    and carries `excerptComplete`: that flag is only the result of a boundary check - the quote ends a
    sentence and does not stop inside a hyphenated word. It says nothing about whether the excerpt is
    semantically complete, whether its conditions and units are all present, or whether it supports the
    question; those are judged elsewhere, and a caller that reads it as "the statement is complete" is
    reading more than it says.
    """
    evidence, cited, quoted = [], {}, 0
    # Diagnostic metadata from the worker: untrusted like everything else it returns, and shaped
    # defensively because a malformed value must not be able to disturb citation building.
    engine_spans = engine_facts.get("sampledSpans") if isinstance(engine_facts, dict) else None
    if not isinstance(engine_spans, dict):
        engine_spans = {}
    for unit in candidates or []:
        if not isinstance(unit, dict) or unit.get("is_found") is False:
            continue
        path = unit.get("file") or ""
        try:
            resolved = str(Path(path).resolve())
            entry = manifest.get(resolved)
        except (OSError, ValueError):
            entry = None
        if not entry:
            continue  # citations come from this run's snapshot only
        seen = cited.setdefault(resolved, set())
        if len(seen) >= MAX_FRAGMENTS_PER_FILE:
            continue
        item, origin, text = None, None, None
        try:
            text = source_text(entry["source"])
        except OSError:
            text = ""
        # The card's coordinates are read from the same in-memory text the excerpt is verified
        # against, so the page marker and the line numbers can never disagree with the quotation.
        lines = source_card.split_lines(text)
        selected, runs, worker_spans = [], [], []
        for snippet in unit.get("snippets", []) or []:
            candidate_text = snippet_text(snippet)
            if not candidate_text:
                continue
            span = _locate_span(text, candidate_text)
            worker_spans.append((span[0], span[1], len(candidate_text)) if span
                                else (None, None, len(candidate_text)))
            item = verify(manifest, path, candidate_text, library_id)
            if item:
                selected, origin = [item], ENGINE_EXCERPT
                break
            blocks, ranked = select_fragments(candidate_text, text, query)
            runs.extend(ranked)
            verified = [verified for verified in
                        (verify(manifest, path, block, library_id) for block in blocks) if verified]
            if verified:
                selected, origin = verified, ENGINE_FRAGMENT
                break
        if not selected:
            # No verbatim engine text: quote a window of the original and mark it as such.
            block, facts = source_window(entry["source"], query)
            item = verify(manifest, path, block, library_id)
            if item:
                item.update(facts)
                if diag:
                    diag.record("fallback_window", structureType=facts["structureType"], fallbackReason=facts["fallbackReason"])
            if item:
                selected, origin, runs, quoted = [item], SOURCE_WINDOW, [], quoted + 1
        if selected:
            fresh = []
            for item in selected:
                key = (item.get("startLine"), item.get("endLine"))
                if key in seen:
                    continue  # the same range again adds nothing; duplicate candidates stay one citation
                seen.add(key)
                # A boundary check only: sentence end reached, no hyphen-broken word. Never a claim that
                # the excerpt carries every condition, unit or the answer itself.
                item["excerptComplete"] = _closes_a_sentence(item.get("excerpt"))
                fresh.append(item)
            for item in fresh:
                item["excerptSource"] = origin
                from markdown_windows import quality_hints
                item["contentWarnings"] = quality_hints(item.get("excerpt"))
                # The location axis, measured here rather than asserted downstream: the offsets have to
                # rebuild the excerpt out of this exact text.
                item["locationVerified"] = source_card.reproduces(
                    text, item.get("excerpt"), item.get("startChar"), item.get("endChar"))
                # The parent holds the text but no catalogue, so it cannot resolve a page: a page needs the
                # record's binding, which only the service has. What it can do is read the extraction's
                # marker as a **hint** and pass it along, where the card treats it as a diagnostic and never
                # as the reported page. (Resolving here would also mean calling the resolver with a record
                # this scope does not have - which is how a list of lines once landed in the record slot.)
                item["pageHint"] = source_card.pdf_page_for_line(lines, item.get("startLine"))
                if handles is not None:
                    # The opaque identifier an on-demand read needs (engine/reader.py). Additive: a
                    # client that ignores it keeps working exactly as before, and it carries no path
                    # the caller could substitute - only the parent can mint one.
                    item["fileHandle"] = handles.issue(entry["relativePath"], entry["version"])
                evidence.append(item)
            cited[resolved] = seen
        if diag is not None and (runs or worker_spans):
            # Metadata only: ranges, sizes, scores, why each run lost - never any text. Candidates are
            # deduplicated by range before the logging cap, and the counters say whether this record is
            # the whole candidate set or a bounded subset of it.
            reported = engine_spans.get(str(path))
            if not isinstance(reported, list):
                reported = []
            unique, seen_keys = [], set()
            for run in runs:
                key = (run["firstLine"], run["endLine"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                unique.append(run)
            file_id = diag.file_id(str(path))
            logged = unique[:12]
            diag.record("fragment_choice", fragments={
                "parts": len(selected), "budgetChars": None,
                "totalCandidates": len(runs), "uniqueCandidates": len(unique),
                "loggedCandidates": len(logged),
                "truncatedCount": max(0, len(unique) - len(logged)),
                "runs": [{"fragmentId": "run-%02d" % index, "fileId": file_id,
                          "startLine": run["firstLine"], "endLine": run["endLine"],
                          "chars": run["chars"], "score": run["score"], "caption": run["caption"],
                          "selected": run["selected"], "reason": run["reason"],
                          "locatable": run["firstLine"] is not None} for index, run in enumerate(logged)],
                "layers": _fragment_layers(reported, worker_spans, unique, file_id)})
    return evidence, quoted


def finalize(payload, manifest, request, snapshot_warnings, diag=None, handles=None, isolation=None):
    """Merge the parent's snapshot state with the engine's untrusted result.

    `handles` (optional) is a parent-side issuer (`reader.Issuer`); when present every citation also
    carries a `fileHandle` so the conversation can read the original context around it later. It is
    injected rather than imported so this module keeps knowing nothing about the service token.

    `isolation` (optional) is the caller's statement of what actually happened to the engine for **this**
    job, appended verbatim. It is a parameter and not a constant here because this module does not run the
    container: the service does, and only the service can say whether it did. A caller that passes nothing
    makes no isolation claim, which is the honest default for an in-process harness.
    """
    warnings = list(snapshot_warnings)
    if payload is None:
        return {"status": "failed", "answer": "", "evidence": [],
                "warnings": warnings + ["Worker returned no result"]}
    warnings += list(payload.get("warnings") or [])
    if payload.get("status") == "failed":
        return {"status": "failed", "answer": "", "evidence": [], "warnings": warnings}
    usage = payload.get("usage") or None
    # Everything below ran to completion inside one engine path, so the "what this service does" line is
    # always accurate and always attached; the isolation line is attached only when the caller supplied one.
    closing = [EXCERPTS_ONLY] + ([isolation] if isolation else [])
    if request["mode"] == "FILENAME_ONLY":
        return {"status": "partial", "answer": "", "evidence": [], "warnings": warnings + closing,
                "matches": payload.get("matches") or [], "usage": usage}
    evidence, quoted = verify_candidates(manifest, payload.get("candidates"), request.get("query"),
                                         request["libraryId"], diag, payload.get("engineFacts"), handles)
    if quoted:
        warnings.append("ENGINE_SNIPPET_UNVERIFIED: %d citation(s) quoted directly from the source file" % quoted)
    incomplete = [item for item in evidence if item.get("excerptComplete") is False]
    if incomplete:
        # Say so instead of letting a quote that stops mid-sentence read as the whole statement.
        warnings.append("EXCERPT_TRUNCATED: %d excerpt(s) end mid-sentence; the quoted range is not the "
                        "complete statement" % len(incomplete))
    # Never present the unverified upstream answer as a grounded final response.
    return {"status": "partial", "answer": "", "evidence": evidence, "warnings": warnings + closing,
            "usage": usage}


async def run_local(request, runner, timeout=None):
    """Parent-side query used by harnesses and tests; the service keeps its own process control.

    `request` must carry `workDir` (created by the caller, who also cleans it up) and `root`.
    `runner(child_request) -> payload` must run the engine in its own process. Snapshot creation,
    capacity rejection and citation verification all happen here, in the caller's process.
    """
    work_dir = Path(request["workDir"])
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        manifest, warnings = build_snapshot(request["root"], work_dir, request.get("maxFileBytes"))
    except SnapshotCapacityError as error:
        return capacity_failure(error)
    if not manifest:
        return {"status": "partial", "answer": "", "evidence": [], "warnings": warnings + ["No supported files"]}
    child_request = worker_request(request, work_dir)
    try:
        payload = await asyncio.wait_for(runner(child_request), timeout) if timeout else await runner(child_request)
    except asyncio.TimeoutError:
        return {"status": "partial", "answer": "", "evidence": [],
                "warnings": warnings + ["BUDGET_EXCEEDED"]}
    return finalize(payload, manifest, request, warnings, isolation=HARNESS_ISOLATION)
