import parse_quality as quality_contract
"""The source-card projection: the display facts for one located file, and its three status axes.

A card is *presentation*, never a new evidentiary claim. It exists so a UI can show where a quotation
came from without the model, and without the browser, having to re-derive anything. The parent builds
it because only the parent knows the library, the catalogue and the verified text.

Three axes stay separate and are never folded into one "confidence" number:

* ``identity`` - does the catalogue/locator agree this is the document it claims to be? Reported as
  ``verified`` / ``partial`` / ``conflict`` / ``undetermined`` straight from the identity catalogue,
  or ``not-assessed`` when no catalogue record was consulted (a search citation comes from the engine
  and the parent's own re-verification, not from the librarian).
* ``location`` - do the returned offsets reproduce the excerpt, verbatim, in the exact source version
  named on the card? That is checked here by re-slicing the text, not assumed, and reported as the
  boolean ``locationVerified``.
* ``support`` - does that text support the conclusion drawn from it? **Never derived here.** It is
  always the literal ``not-assessed``: locating a quote proves where text came from, not that it says
  what a reader concludes from it. A card that says otherwise would be inventing a verdict the
  service never made.

The page number follows the same rule, and it is the one place where "verbatim" is not enough. A page
reported from the text alone is only as trustworthy as the text: any document may contain a line that
merely *looks* like the extraction's marker, and a verbatim match against such a line proves nothing
about which PDF page the quotation sits on. So the extraction's own per-page line ranges (recorded in
``audit/<id>.json`` as ``pages``) are carried into the identity catalogue as ``pageMap``, and they are
authoritative:

* a ``pageMap`` that covers the line decides the page, reported as ``pageSource: page-map``;
* a ``pageMap`` that is present but does **not** cover the line reports **no page at all** - it never
  falls back to a marker the document could have written itself;
* only a source with no ``pageMap`` (a library catalogued before the map existed, or a non-PDF source)
  falls back to the in-text marker, and that answer is labelled ``pageSource: text-marker`` so a reader
  can see it is unverified.
"""

import re

#: The marker `scripts/extract-literature.py` writes once per PDF page. Anchored to a whole line: a
#: passing mention of "PDF page" inside a sentence is not a page boundary.
PAGE_MARKER = re.compile(r"^\s*PDF\s+page\s+(\d+)\s*$", re.IGNORECASE)

#: Where a reported page came from, and the only vocabulary a card may use for it.
#:
#: ``page-map`` is the only value that carries a page. Everything else resolves *no* page, because a page
#: is a claim about a PDF's structure and only the extraction's own map can support it:
#:
#: * ``map-gap`` - a usable map covers no range for this line: a mapping defect, reported as one.
#: * ``map-unusable`` - the record claims a map that cannot be trusted (malformed, or its recorded binding
#:   does not match this text/PDF version). Falling back to the text here would be worst of all.
#: * ``unmapped`` - a PDF with no map: the card says the mapping has not been established.
#: * ``not-pdf`` - a source with no PDF behind it (Markdown), which is given line numbers only.
PAGE_MAP = "page-map"
MAP_GAP = "map-gap"
MAP_UNUSABLE = "map-unusable"
UNMAPPED = "unmapped"
NOT_PDF = "not-pdf"

#: The map layout this module understands. A map recorded under a different version is not read: a
#: half-understood map is worse than no map, because it would still produce confident page numbers.
PAGE_MAP_VERSION = 1

#: The only coordinate space a card reports. Every offset and line number refers to the citable
#: extracted text (`texts/`), never to the PDF's own text layer and never to a normalised copy.
COORDINATE_SPACE = "extracted-text"

#: The literal the ``support`` axis always carries. See the module docstring.
SUPPORT_NOT_ASSESSED = "not-assessed"

IDENTITY_NOT_ASSESSED = "not-assessed"


def split_lines(text):
    """Line array of already-normalised text, or ``None`` when no text is available.

    ``str.splitlines`` is not used: the reader and the verifier both count ``\\n``, and a card whose
    line numbers disagreed with the offsets it is explaining would be worse than no card.
    """
    if not isinstance(text, str):
        return None
    return text.split("\n")


def pdf_page_for_line(lines, start_line):
    """The extraction's own page marker at or above ``start_line``, else ``None``.

    This is a **diagnostic** reader, never a page resolver. Any document may contain a line that looks
    like the extraction's marker, so a match proves only that the line exists; the returned value is
    reported as a hint and must not be presented as the page a citation sits on.
    """
    if not lines or not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
        return None
    index = min(start_line, len(lines)) - 1
    while index >= 0:
        marker = PAGE_MARKER.match(lines[index])
        if marker:
            return int(marker.group(1))
        index -= 1
    return None


def is_pdf_record(record):
    """Whether a catalogue entry describes a PDF, so a text-line coordinate is not dressed up as a page.

    The extraction's page count is the primary signal; the file name is the fallback for an entry recorded
    before page counts were stored.
    """
    if not isinstance(record, dict):
        return False
    if isinstance(record.get("pages"), int) and not isinstance(record.get("pages"), bool) and record["pages"] > 0:
        return True
    return str(record.get("fileName") or "").lower().endswith(".pdf")


def usable_page_map(record):
    """``(page_map, problem)`` - the map only when it is bound to this exact version, else ``(None, why)``.

    The binding is what makes the map authoritative. A map is accepted only when it declares the current
    layout version and the text hash it was derived from still matches the record's, so a catalogue whose
    text was re-extracted, whose audit was replaced, or whose map was hand-edited resolves *no* page
    rather than a page from a document that no longer exists. ``problem`` is a short machine-readable
    reason the caller turns into ``map-unusable``.
    """
    if not isinstance(record, dict) or "pageMap" not in record:
        return None, None  # nothing claimed: a legacy entry, handled as `unmapped`
    raw = record.get("pageMap")
    if not isinstance(raw, list) or not raw:
        return None, "pageMap is not a non-empty list"
    entries = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        page, start, end = entry.get("page"), entry.get("startLine"), entry.get("endLine")
        if not (isinstance(page, int) and isinstance(start, int) and isinstance(end, int)):
            continue
        if start < 1 or end < start:
            continue
        entries.append({"page": page, "startLine": start, "endLine": end})
    if not entries:
        return None, "pageMap has no usable entry"
    source = record.get("pageMapSource")
    if not isinstance(source, dict):
        return None, "pageMap has no recorded binding"
    if source.get("mappingVersion") != PAGE_MAP_VERSION:
        return None, "pageMap mappingVersion is %r, expected %r" % (source.get("mappingVersion"), PAGE_MAP_VERSION)
    recorded = source.get("auditTextSHA256")
    if not isinstance(recorded, str) or not recorded or recorded != record.get("textSHA256"):
        return None, "pageMap was derived from a different text version"
    # The PDF binding is checked only when the record carries one: the text hash is the binding that
    # matters for what a citation can be jumped to, and an entry built before PDF hashes were stored must
    # not become unusable over a field it never had.
    pdf_recorded, pdf_current = source.get("pdfSHA256"), record.get("pdfSHA256")
    if isinstance(pdf_recorded, str) and pdf_recorded and isinstance(pdf_current, str) and pdf_current \
            and pdf_recorded != pdf_current:
        return None, "pageMap was derived from a different PDF"
    return entries, None


def page_for_line(start_line, page_map=None):
    """``(page, pageSource)`` for one 1-based line, from a map already accepted by :func:`usable_page_map`.

    A map that covers the line decides the page. A map that covers nothing returns no page and reports
    ``map-gap``: the line is outside every range the extraction recorded, which is a defect to surface
    rather than a reason to ask the document's own text what page it thinks it is on.
    """
    if not page_map:
        return None, UNMAPPED
    for entry in page_map:
        if entry["startLine"] <= start_line <= entry["endLine"]:
            return entry["page"], PAGE_MAP
    return None, MAP_GAP


def location(start_line, end_line, start_char=None, end_char=None, record=None, hint=None):
    """The location half of a card: which file position, in which coordinate space, and how well sourced.

    The page is resolved **only** from a map the record's own binding vouches for (see
    :func:`usable_page_map`). There is deliberately no path from the text to ``pdfPage``: a line that
    looks like the extraction's marker is at most a diagnostic hint (``pageHint``), because a document can
    contain such a line without being on that page, and a source with no map says the mapping has not been
    established rather than guessing from the text. ``kind`` says which coordinate a reader should trust,
    so a consumer never has to infer it from a null.

    ``hint`` is the caller's own marker scan, when it already had the text in hand; it is echoed into
    ``pageHint`` for a PDF that has no map, and never into ``pdfPage``.
    """
    record = record if isinstance(record, dict) else {}
    page_map = None
    if not is_pdf_record(record):
        # No PDF behind this source: line numbers only. A `PDF page N` line here is a string in a text
        # file, not a page, so it is not even offered as a hint.
        resolved, source, hint = None, NOT_PDF, None
    else:
        page_map, problem = usable_page_map(record)
        if problem is not None:
            # The record claims a map it cannot support. Reporting a page from the text instead would
            # quietly substitute the least trustworthy source for the most trustworthy one.
            resolved, source, hint = None, MAP_UNUSABLE, None
        elif page_map is None:
            resolved, source = None, UNMAPPED
        else:
            resolved, source = page_for_line(start_line, page_map=page_map)
            hint = None
    reported = {
        "coordinateSpace": COORDINATE_SPACE,
        "kind": "pdf-page" if resolved is not None else "text-line",
        "pdfPage": resolved,
        # A window can span pages, and one number would then describe only its first line while reading
        # as if it described the whole range. The end page is resolved from the same map, so a crossing
        # is visible instead of being smoothed over.
        "pdfPageEnd": page_for_line(end_line, page_map=page_map)[0] if resolved is not None and page_map else resolved,
        "pageSource": source,
        "pageHint": hint,
        "startLine": start_line,
        "endLine": end_line,
    }
    if isinstance(start_char, int) and isinstance(end_char, int):
        # 0-based half-open code point offsets into the same text the line numbers describe.
        reported["startChar"], reported["endChar"] = start_char, end_char
    return reported


def reproduces(text, excerpt, start_char, end_char):
    """Whether the offsets really rebuild the excerpt, checked against the text in hand.

    This is the ``location`` axis, and it is measured rather than declared. An excerpt that cannot be
    rebuilt is not a located quotation however plausible it looks, so the card must be able to say so.
    """
    if not isinstance(text, str) or not isinstance(excerpt, str):
        return False
    if not isinstance(start_char, int) or not isinstance(end_char, int):
        return False
    if start_char < 0 or end_char < start_char or end_char > len(text):
        return False
    return text[start_char:end_char] == excerpt


def card(library_id, file_name, excerpt, location_fields, source_version,
         citation_origin, truncated=False, truncation_notices=None, parse_quality=None,
         identity=IDENTITY_NOT_ASSESSED, identity_detail=None, conflicts=None,
         location_verified=False, title=None, document_id=None, relative_path=None):
    """One source card. ``sourceVersion`` and the location travel together or not at all.

    A card without a source version cannot be re-checked later, so it is not a card: the caller passes
    ``source_version=None`` only for a candidate that has not been read yet, and then the location is
    ``None`` too.

    ``truncated`` is now exactly what the caller says, and ``truncation_notices`` are notices. They used to be
    collapsed - any notice set ``truncated`` - which made "the quoted range stops mid-statement" and "the
    extracted text ends here without a recognized sentence boundary" the same claim. They are not: the first is
    about a window that has more file after it, the second about the text itself, and a card that says
    "truncated" for the second is describing a property of the window that is not true. Both halves render the
    notices either way, so nothing is hidden by keeping them apart.
    """
    quality, quality_degraded = quality_contract.normalize(parse_quality)
    notices = [notice for notice in (truncation_notices or []) if notice]
    reported = {
        "libraryId": library_id,
        "title": title,
        "fileName": file_name,
        "excerpt": excerpt,
        "location": location_fields,
        "sourceVersion": source_version,
        "citationOrigin": citation_origin,
        "truncated": bool(truncated),
        "truncationNotices": notices,
        "parseQuality": quality,
        "parseQualityDegraded": quality_degraded,
        "status": {
            "identity": identity,
            "identityDetail": identity_detail,
            "locationVerified": bool(location_verified),
            "support": SUPPORT_NOT_ASSESSED,
        },
    }
    if document_id is not None:
        reported["documentId"] = document_id
    if relative_path is not None:
        # The library-relative name of the citable text file. Never an absolute path: the card is
        # rendered in a browser, and a filesystem path there would be both a leak and a temptation to
        # use as one.
        reported["relativePath"] = relative_path
    if conflicts:
        reported["conflicts"] = conflicts
    return reported
