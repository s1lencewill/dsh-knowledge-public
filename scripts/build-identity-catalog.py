"""Build the literature identity catalogue for a versioned corpus (M0, offline, local only).

For every document the catalogue records a stable id, the actual file names, the in-text title,
authors, year and DOI, where that identity came from, how far it could be verified, the PDF/text
hashes and the extraction-quality marks. Two rules drive the design:

* a file name is a *clue*, never the identity. The identity fields are read from the document's own
  title block, its publisher `Cite This` line, or its header/footer - and the file name's own journal
  tokens are compared against them, so a swapped file name shows up as a recorded conflict instead of
  a silent identity;
* a reference-list entry or a publisher recommendation is never mistaken for the document. Those lines
  are classified and kept separately, with the section context that decided it.

Output is `identity.json` next to the corpus (gitignored); nothing is renamed or modified.

    .\\.venv\\Scripts\\python.exe scripts\\build-identity-catalog.py \
        --corpus <version dir> --pdf-map <previous source-map.json with absolute pdf paths>
"""
import argparse
import importlib.util
import json
import pathlib
import re
import sys
import unicodedata

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOI = re.compile(r"10\.\d{4,9}/[-._;()/:<>A-Za-z0-9]+")
CITE_THIS = re.compile(r"cite\s*this|cite\s*as", re.IGNORECASE)
REFERENCES = re.compile(r"^\s*(references|reference|bibliography|参考文献)\s*$", re.IGNORECASE)
RECOMMENDATION = re.compile(r"articles?\s+you\s+may\s+be\s+interested|read\s+online|check\s+for\s+updates|"
                            r"why\s+publish\s+with\s+us|export\s+citation|view\s+online|"
                            r"recommended\s+articles|related\s+articles", re.IGNORECASE)
JOURNAL_LINE = re.compile(r"(J\.\s*Am\.\s*Chem\.\s*Soc|J\.\s*Chem\.\s*Phys|J\.\s*Phys\.\s*Chem|"
                          r"J\.\s*Chem\.\s*Theory\s*Comput|Nat\.?\s*Commun|Nature|Science|PNAS|Langmuir|"
                          r"ACS\s*Phys|J\.\s*Phys\.\s*Chem\.\s*Lett|Phys\.\s*Chem\.\s*Chem\.\s*Phys)", re.IGNORECASE)
YEAR = re.compile(r"(19|20)\d{2}")
FILENAME_JOURNAL = re.compile(r"(?P<journal>[A-Za-z.]+?)[-_ ]?(?P<year>(?:19|20)\d{2})[-_, ]+(?P<volume>\d+)[-_, ]+(?P<page>\d+)")
AFFILIATION = re.compile(r"(university|institute|department|academy|college|laborator|"
                         r"affiliations? are included|corresponding author|e-?mail)", re.IGNORECASE)
AUTHORISH = re.compile(r"^[^.;:!?]{4,140}$")


def looks_like_authors(text):
    """A loose author-line test: several capitalised name tokens, no sentence punctuation.

    Strict name grammars fail on real author lines ("Samuel G. H. Brookesa,b,c" carries affiliation
    superscripts), so the test is deliberately soft: at least two capitalised tokens, no terminal
    sentence punctuation, and no affiliation vocabulary. Anything that fails simply leaves the field
    empty, which the catalogue reports rather than guessing.
    """
    if not AUTHORISH.match(text.strip()) or AFFILIATION.search(text):
        return False
    tokens = re.findall(r"\b[A-Z][\w.\-'\u00c0-\u024f]{1,}", text)
    return len(tokens) >= 2 and not text.strip().endswith((".", "。"))


def load_extractor():
    spec = importlib.util.spec_from_file_location("extract_literature", ROOT / "scripts/extract-literature.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_title(value):
    """Normalized form for matching: case-folded, punctuation and whitespace removed.

    Used for *matching* only. The raw title is kept as extracted, because a title whose spaces were lost
    in the PDF's text layer must not be silently "repaired" into the stored identity.
    """
    text = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"[\s\u3000]+", "", re.sub(r"[^\w\u3400-\u9fff]+", " ", text)).casefold()


def slug(value):
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_value.casefold())[:40]


def page_lines_with_context(page, extractor):
    """(line text, y, size, isHeading) for one page, in reading order."""
    segments, flags = extractor.page_segments(page)
    ordered = extractor.order_lines(segments, flags["width"], flags["height"])
    return [{"text": segment["text"], "y": segment["y"], "size": segment["size"],
             "x0": segment["x0"]} for segment in ordered], flags


def classify(lines):
    """Split a page's lines into identity / citation / recommendation regions by section context.

    Only the reference list is a sticky region (it really is a section, and a DOI inside it belongs to
    some *other* paper). A publisher recommendation such as "Read Online" or "Articles You May Be
    Interested In" marks its own line, so it cannot swallow the article's own title block.
    """
    regions, in_references = [], False
    for line in lines:
        text = line["text"].strip()
        if REFERENCES.match(text):
            in_references = True
        if in_references:
            regions.append("citation")
        elif RECOMMENDATION.search(text):
            regions.append("recommendation")
        else:
            regions.append("identity")
    return regions


def identity_of(identifier, pdf_path, corpus_record, extractor):
    from pypdf import PdfReader
    reader = PdfReader(str(pdf_path))
    record = {"documentId": corpus_record["documentId"], "fileName": pathlib.Path(pdf_path).name,
              "textFile": corpus_record["text"], "pdfSHA256": corpus_record["pdfSHA256"],
              "textSHA256": corpus_record["textSHA256"], "pages": corpus_record["pageCount"],
              # The extraction's own per-page line ranges, carried through so a citation's page number
              # can be resolved from the PDF's page objects instead of from a line of text that any
              # document could contain. This is the authoritative page source (source_card.PAGE_MAP);
              # without it a card may only report an in-text marker, labelled as unverified.
              "pageMap": [{"page": page.get("page"), "startLine": page.get("startLine"),
                           "endLine": page.get("endLine")}
                          for page in (corpus_record.get("pages") or [])
                          if isinstance(page, dict) and isinstance(page.get("page"), int)
                          and isinstance(page.get("startLine"), int)
                          and isinstance(page.get("endLine"), int)],
              "quality": {"replacementChars": sum(f.get("replacementChars", 0) for f in corpus_record["flags"]
                                                  if f["flag"] == "character-loss"),
                          "privateUseChars": sum(f.get("privateUseChars", 0) for f in corpus_record["flags"]
                                                 if f["flag"] == "character-loss"),
                          "reviewPages": corpus_record["reviewPages"],
                          "searchableCjkRuns": None},
              "titleRaw": None, "titleNormalized": None, "authors": None, "year": None, "doi": None,
              "identitySource": [], "referenceDoiExamples": 0, "recommendationLines": 0,
              "verification": "undetermined", "conflicts": [], "notes": []}

    metadata = {}
    try:
        metadata = {key.lower(): (value or "") for key, value in (reader.metadata or {}).items()}
    except Exception as error:  # a malformed metadata dictionary must not break the catalogue
        record["notes"].append("metadata unreadable: %s" % type(error).__name__)

    # ---- page 1 (and page 2 when page 1 is a publisher cover) ----------------------------------
    pages = []
    for index in (1, 2):
        if index <= len(reader.pages):
            lines, flags = page_lines_with_context(reader.pages[index - 1], extractor)
            pages.append((index, lines, classify(lines)))

    candidates = []
    for index, lines, regions in pages:
        for position, (line, region) in enumerate(zip(lines, regions)):
            candidates.append({"page": index, "position": position, "region": region, **line})

    # ---- title: the largest type in the top part of page 1 --------------------------------------
    page_one = [item for item in candidates if item["page"] == 1]
    identity_one = [item for item in page_one if item["region"] == "identity"]
    sizes = sorted((item["size"] or 0) for item in page_one)
    median_size = sizes[len(sizes) // 2] if sizes else 10.0
    big = [item for item in identity_one if (item["size"] or 0) > 1.35 * median_size
           and item["position"] < max(12, len(identity_one) // 3)]
    if big:
        top = max(item["y"] for item in big)
        title_items = [item for item in big if abs(item["y"] - top) < 6.0 * (item["size"] or 10.0)]
        title_items.sort(key=lambda item: -item["y"])
        record["titleRaw"] = " ".join(item["text"].strip() for item in title_items).strip()
        record["identitySource"].append({"field": "title", "page": 1,
                                         "lines": [item["position"] for item in title_items],
                                         "how": "largest type in the page-1 title block"})
    elif metadata.get("title"):
        record["titleRaw"] = metadata["title"].strip()
        record["identitySource"].append({"field": "title", "page": None, "lines": [],
                                         "how": "PDF /Title metadata (no title block detected)"})
        record["notes"].append("title came from metadata, not from the visible title block")
    if record["titleRaw"]:
        record["titleNormalized"] = normalize_title(record["titleRaw"])

    # ---- authors: first plausible name line after the title, else metadata ----------------------
    if record["titleRaw"] and big:
        tail = [item for item in page_one if item["y"] < min(item["y"] for item in big) - 1.0]
        for item in tail[:8]:
            text = item["text"].strip()
            if AFFILIATION.search(text) or DOI.search(text) or len(text) < 6:
                continue
            if looks_like_authors(text) and not JOURNAL_LINE.search(text):
                record["authors"] = text
                record["identitySource"].append({"field": "authors", "page": 1, "lines": [item["position"]],
                                                 "how": "name line directly below the title"})
                break
    if not record["authors"] and metadata.get("author"):
        record["authors"] = metadata["author"].strip()
        record["identitySource"].append({"field": "authors", "page": None, "lines": [],
                                         "how": "PDF /Author metadata"})

    # ---- DOI: page 1 header/footer or an explicit Cite This line, page 2 as a fallback -----------
    for item in candidates:
        if item["region"] != "identity" or (item["page"] == 2 and record["doi"] is not None):
            continue
        match = DOI.search(item["text"])
        if match:
            doi = match.group(0).rstrip(".,);")
            if record["doi"] is None:
                record["doi"] = doi
                record["identitySource"].append({"field": "doi", "page": item["page"],
                                                 "lines": [item["position"]],
                                                 "how": "DOI pattern in the %s identity region" %
                                                         ("page-1" if item["page"] == 1 else "page-2 (page 1 had none)")})
    record["referenceDoiExamples"] = sum(1 for item in candidates
                                         if item["region"] == "citation" and DOI.search(item["text"]))

    # ---- year / journal: an explicit Cite This line wins over any other line --------------------
    cite_index = next((index for index, item in enumerate(candidates) if CITE_THIS.search(item["text"])), None)
    cite_line = candidates[cite_index] if cite_index is not None else None
    if cite_line:
        # The citation can wrap: "Cite This: J. Am. Chem. Soc." with the year, volume and page on the
        # following line. Read the next two lines of the same region as part of the same citation.
        window = [item for item in candidates[cite_index:cite_index + 3]
                  if item["page"] == cite_line["page"] and item["region"] == cite_line["region"]]
        citation_text = " ".join(item["text"].strip() for item in window)
        record["identitySource"].append({"field": "citation-of-record", "page": cite_line["page"],
                                         "lines": [item["position"] for item in window],
                                         "how": "publisher 'Cite This' line",
                                         "text": citation_text.strip()[:160]})
        match = YEAR.search(citation_text)
        if match:
            record["year"] = match.group(0)
        cite_line = dict(cite_line, text=citation_text)
    if record["year"] is None:
        journal = next((item for item in candidates if item["region"] == "identity" and JOURNAL_LINE.search(item["text"])), None)
        if journal:
            match = YEAR.search(journal["text"])
            if match:
                record["year"] = match.group(0)
                record["identitySource"].append({"field": "year", "page": journal["page"],
                                                 "lines": [journal["position"]], "how": "journal line on page 1"})

    record["recommendationLines"] = sum(1 for item in candidates if item["region"] == "recommendation")

    # ---- verification: identity vs the file name ------------------------------------------------
    problems = []
    if not record["titleRaw"]:
        problems.append("no title found")
    if not record["doi"]:
        problems.append("no DOI found on page 1")
    filename_tokens = FILENAME_JOURNAL.search(record["fileName"])
    if cite_line and filename_tokens:
        file_year = filename_tokens.group("year")
        year_in_cite = YEAR.search(cite_line["text"])
        compact_cite = re.sub(r"\s+", "", cite_line["text"])
        if year_in_cite and year_in_cite.group(0) != file_year:
            record["conflicts"].append({"field": "year", "fileName": file_year,
                                        "inText": year_in_cite.group(0),
                                        "source": "publisher Cite This line",
                                        "note": "the file name and the document's own citation disagree"})
        # Only a *volume* mismatch is treated as a conflict: file names commonly carry an issue number
        # where the page goes ("...-14-36-8175..."), so a page comparison would be a false positive.
        volume_in_cite = re.search(r"(?:19|20)\d{2},\s*(\d+)", cite_line["text"])
        if volume_in_cite and volume_in_cite.group(1) != filename_tokens.group("volume"):
            record["conflicts"].append({"field": "volume", "fileName": filename_tokens.group("volume"),
                                        "inText": volume_in_cite.group(1),
                                        "source": "publisher Cite This line",
                                        "note": "volume differs from the file name"})
    if record["conflicts"]:
        record["verification"] = "conflict"
    elif not problems and record["authors"]:
        record["verification"] = "verified"
    elif not problems:
        record["verification"] = "partial"
    else:
        record["verification"] = "partial" if record["titleRaw"] or record["doi"] else "undetermined"
    record["problems"] = problems
    return record


def detect_swaps(catalogue):
    """Flag files whose own citation matches another file's name.

    A file name is only a clue, so a mismatch with the document's own citation is recorded rather than
    "corrected". This catches the specific case of two files whose contents were swapped: the citation
    inside one of them is the bibliographic identity of the other one's file name. Both files are
    flagged, and both values are kept, because deciding which name is right is a human call.
    """
    identities = {}
    for key, record in catalogue.items():
        cite = next((source for source in record["identitySource"]
                     if source["field"] == "citation-of-record"), None)
        if not cite or not cite.get("text"):
            continue
        found = re.search(r"((?:19|20)\d{2}),\s*(\d+),\s*(\d+)", cite["text"])
        if found:
            identities[key] = {"year": found.group(1), "volume": found.group(2), "page": found.group(3)}
    for key, identity in identities.items():
        name = FILENAME_JOURNAL.search(catalogue[key]["fileName"])
        if not name:
            continue
        if (name.group("year"), name.group("volume")) == (identity["year"], identity["volume"]):
            continue     # the file's own citation agrees with its file name
        own_page = re.search(r",\s*(\d+)\s*(?:H|S|R|N|C|$)", catalogue[key]["fileName"])
        for other, other_record in catalogue.items():
            if other == key:
                continue
            other_name = FILENAME_JOURNAL.search(other_record["fileName"])
            if not other_name:
                continue
            if (other_name.group("year"), other_name.group("volume")) != (identity["year"], identity["volume"]):
                continue
            # The page number distinguishes genuine swaps from files that merely share a journal year
            # and volume: the citing document's first page must be the other file's page.
            if other_name.group("page") != identity["page"]:
                continue
            note = ("the document's own citation (%s %s, page %s) is the bibliographic identity of the "
                    "file named %s; the two files appear to be swapped" %
                    (identity["year"], identity["volume"], identity["page"], catalogue[other]["fileName"]))
            catalogue[key]["conflicts"].append({"field": "identity", "fileName": catalogue[key]["fileName"],
                                                "inText": catalogue[other]["fileName"],
                                                "source": "cross-file check",
                                                "note": "this document's own citation belongs to another "
                                                        "file in the library; the pair appears swapped"})
            catalogue[key]["verification"] = "conflict"
            catalogue[other]["conflicts"].append({"field": "identity", "fileName": catalogue[other]["fileName"],
                                                  "inText": catalogue[key]["fileName"],
                                                  "source": "cross-file check", "note": note})
            catalogue[other]["verification"] = "conflict"


def main():
    parser = argparse.ArgumentParser(description="Build the literature identity catalogue.")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--pdf-map", required=True, help="a source-map.json that still carries absolute pdf paths")
    parser.add_argument("--out")
    arguments = parser.parse_args()

    extractor = load_extractor()
    corpus = json.loads((pathlib.Path(arguments.corpus) / "source-map.json").read_text(encoding="utf-8-sig"))
    paths = {entry["text"]: entry["pdf"] for entry in
             json.loads(pathlib.Path(arguments.pdf_map).read_text(encoding="utf-8-sig"))}
    catalogue, problems = {}, []
    for record in corpus:
        try:
            catalogue[record["documentId"]] = identity_of(record["documentId"], paths[record["text"]],
                                                          record, extractor)
        except Exception as error:
            problems.append({"documentId": record["documentId"], "error": "%s: %s" % (type(error).__name__, error)})
    detect_swaps(catalogue)
    payload = {"version": extractor.VERSION, "documents": catalogue, "failures": problems,
               "documentCount": len(catalogue)}
    target = pathlib.Path(arguments.out) if arguments.out else pathlib.Path(arguments.corpus) / "identity.json"
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {}
    for record in catalogue.values():
        summary[record["verification"]] = summary.get(record["verification"], 0) + 1
    print(json.dumps({"documents": len(catalogue), "verification": summary,
                      "withDoi": sum(1 for r in catalogue.values() if r["doi"]),
                      "withAuthors": sum(1 for r in catalogue.values() if r["authors"]),
                      "conflicts": [r["documentId"] for r in catalogue.values() if r["conflicts"]],
                      "failures": problems}, ensure_ascii=False))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
