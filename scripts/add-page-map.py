"""Add the extraction's per-page line ranges to an existing identity catalogue, as a new version.

This is an **enrichment**, not a rebuild: it re-extracts nothing, recomputes no identity, and never touches
the text files. It reads the audits the extractor already wrote, proves each map belongs to the very text
and PDF the catalogue describes, and publishes a new catalogue beside the old one - the old file is left
exactly as it was, which is the rollback path.

Why the binding is the whole point: a page map is only meaningful for the text version it was derived from.
A map whose binding is not recorded, or no longer matches, is not "a slightly stale map" - it is a map for a
different document, and the service is required to resolve no page at all in that case (see
`engine/source_card.py`). So every check below is a precondition for writing `pageMap`, not a warning.

    python scripts/add-page-map.py --catalogue <identity.json> --library-root <texts dir> \
        --audit-dir <audit dir> --out <new catalogue.json> [--report <report.json>]

Per document it verifies:

* the citable text file still hashes to the catalogue's `textSHA256` (nothing edited the text);
* the audit's `textSHA256` equals the catalogue's (the map describes this text);
* the audit's `pdfSHA256` equals the catalogue's, when the catalogue carries one;
* the audit's page count equals the catalogue's `pages`, when the catalogue carries one.

And it audits the map itself, because a map that silently drops lines is a defect the reader would only
meet as an unexplained `map-gap` later: it reports coverage over the text's own line count, every uncovered
line range, every overlap, and whether the page numbers ascend from 1.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sys

#: The layout `engine/source_card.py` reads. Written into the catalogue so a future reader can refuse a map
#: whose shape it does not actually understand, instead of half-parsing it.
MAPPING_VERSION = 1


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_json(path):
    return json.loads(Path(path).read_bytes().decode("utf-8-sig"))


def audit_index(audit_dir):
    """`textFile` -> `(path, audit)`, so a record finds its audit by the text the extraction produced.

    Both spellings the extractor writes are indexed: `textFile` is the full name, while `text` is the stem
    it was given on the command line. Indexing only the stem would silently match nothing.
    """
    index = {}
    for path in sorted(Path(audit_dir).glob("*.json")):
        try:
            audit = load_json(path)
        except (OSError, ValueError):
            continue
        for key in (audit.get("textFile"), audit.get("text")):
            if isinstance(key, str) and key:
                index.setdefault(key, (path, audit))
                if not key.endswith(".txt"):
                    index.setdefault(key + ".txt", (path, audit))
    return index


def map_of(audit):
    return [{"page": page.get("page"), "startLine": page.get("startLine"), "endLine": page.get("endLine")}
            for page in (audit.get("pages") or [])
            if isinstance(page, dict) and isinstance(page.get("page"), int)
            and isinstance(page.get("startLine"), int) and isinstance(page.get("endLine"), int)]


def text_line_count(path):
    """The text's line count, by the same definition the reader and the verifier use.

    `str.split("\\n")` is **not** that definition: a file ending in a newline splits to one more element
    than it has lines, which would report a one-line gap at the end of every document and drown the real
    ones. `reader.window_for` counts newlines and adds one only for a final unterminated line.
    """
    raw = Path(path).read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    return raw.count("\n") + (0 if raw.endswith("\n") else 1)


def coverage(page_map, line_count):
    """How much of the text the map accounts for, and exactly where it does not."""
    covered = sorted((entry["startLine"], entry["endLine"]) for entry in page_map)
    gaps, overlaps = [], []
    cursor = 1
    for start, end in covered:
        if start > cursor:
            gaps.append([cursor, start - 1])
        elif start < cursor:
            overlaps.append([start, end])
        cursor = max(cursor, end + 1)
    if cursor <= line_count:
        gaps.append([cursor, line_count])
    pages = [entry["page"] for entry in page_map]
    return {
        "lines": line_count,
        "coveredLines": sum(end - start + 1 for start, end in covered),
        "gaps": gaps,
        "overlaps": overlaps,
        "pagesAscendingFromOne": pages == list(range(1, len(pages) + 1)),
    }


def enrich(record, root, audits):
    """`(page_map, source, problem)`: the map only when its binding proves it belongs to this record."""
    text_file = record.get("textFile")
    if not isinstance(text_file, str):
        return None, None, "record has no textFile"
    located = audits.get(text_file)
    if located is None:
        return None, None, "no audit for this text file"
    audit_path, audit = located

    target = Path(root) / text_file
    if not target.is_file():
        return None, None, "the citable text file is missing"
    actual = digest(target.read_bytes())
    if actual != record.get("textSHA256"):
        return None, None, "the text file no longer matches the catalogue textSHA256"
    if audit.get("textSHA256") != record.get("textSHA256"):
        return None, None, "the audit was written for a different text version"
    recorded_pdf = record.get("pdfSHA256")
    if isinstance(recorded_pdf, str) and recorded_pdf and audit.get("pdfSHA256") != recorded_pdf:
        return None, None, "the audit was written for a different PDF"
    page_map = map_of(audit)
    if not page_map:
        return None, None, "the audit records no page line ranges"
    if page_map[0]["startLine"] != 1:
        return None, None, "the map does not start at line 1"
    if page_map[-1]["endLine"] != max(entry["endLine"] for entry in page_map):
        return None, None, "the map does not end at its last recorded line"

    source = {
        "mappingVersion": MAPPING_VERSION,
        "source": "extraction-audit",
        "auditFile": audit_path.name,
        "extractor": audit.get("extractor"),
        "extractorVersion": audit.get("extractorVersion"),
        "auditTextSHA256": audit.get("textSHA256"),
        "pdfSHA256": audit.get("pdfSHA256"),
    }
    return page_map, source, None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Enrich an identity catalogue with bound page maps.")
    parser.add_argument("--catalogue", required=True)
    parser.add_argument("--library-root", required=True, help="the directory the textFile paths are relative to")
    parser.add_argument("--audit-dir", required=True)
    parser.add_argument("--out", required=True, help="the NEW catalogue to write; the input is never modified")
    parser.add_argument("--report", help="where to write the per-document audit")
    parser.add_argument("--activation", help="where to record the switch and its rollback target")
    arguments = parser.parse_args(argv)

    source_path = Path(arguments.catalogue).resolve()
    out_path = Path(arguments.out).resolve()
    if out_path == source_path:
        raise SystemExit("--out must differ from --catalogue: the input is the rollback path")

    catalogue = load_json(source_path)
    documents = catalogue.get("documents")
    if not isinstance(documents, dict):
        raise SystemExit("the catalogue carries no documents mapping")
    audits = audit_index(arguments.audit_dir)

    enriched, report = {}, []
    for key, record in documents.items():
        if not isinstance(record, dict):
            continue
        # A non-PDF source (Markdown) has no pages to map, which is a fact about the source and not a
        # failure: it is recorded as such and left without a map.
        is_pdf = record.get("fileName", "").lower().endswith(".pdf") or isinstance(record.get("pages"), int)
        page_map, map_source, problem = enrich(record, arguments.library_root, audits)
        updated = dict(record)
        entry = {"documentId": record.get("documentId") or key, "textFile": record.get("textFile"),
                 "pdf": bool(is_pdf)}
        if page_map:
            updated["pageMap"] = page_map
            updated["pageMapSource"] = map_source
            text_lines = text_line_count(Path(arguments.library_root, record["textFile"]))
            entry.update({"mapped": True, "pages": len(page_map), "coverage": coverage(page_map, text_lines)})
        else:
            updated.pop("pageMap", None)
            updated.pop("pageMapSource", None)
            entry.update({"mapped": False, "reason": problem})
        enriched[key] = updated
        report.append(entry)

    payload = {
        "catalogueVersion": "%s+pagemap%d" % (catalogue.get("version", "unknown"), MAPPING_VERSION),
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pageMapVersion": MAPPING_VERSION,
        "derivedFrom": source_path.name,
        "documents": enriched,
    }
    for key in catalogue:
        if key not in payload and key != "documents":
            payload[key] = catalogue[key]
    out_path.write_bytes(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    mapped = [entry for entry in report if entry["mapped"]]
    pooled = [entry for entry in report if not entry["mapped"]]
    gaps = [{"documentId": entry["documentId"], "gaps": entry["coverage"]["gaps"]}
            for entry in mapped if entry["coverage"]["gaps"]]
    summary = {
        "source": str(source_path), "out": str(out_path),
        "documents": len(report), "mapped": len(mapped), "unmapped": len(pooled),
        "unmappedReasons": sorted({entry["reason"] for entry in pooled if entry.get("reason")}),
        "documentsWithGaps": len(gaps),
        "documentsWhosePagesDoNotAscend": [entry["documentId"] for entry in mapped
                                           if not entry["coverage"]["pagesAscendingFromOne"]],
        "documentsWithOverlaps": [entry["documentId"] for entry in mapped if entry["coverage"]["overlaps"]],
    }
    if arguments.report:
        Path(arguments.report).write_bytes(json.dumps(
            {"summary": summary, "documents": report, "gaps": gaps}, indent=2, ensure_ascii=False).encode("utf-8"))
    if arguments.activation:
        # The switch is a file swap, so it is recorded as one: which file the service should point
        # DSH_KNOWLEDGE_CATALOG at, what it hashes to, and the exact file to go back to. Only written when
        # every document that could be mapped was, so a partial map is never published as active.
        activation = {
            "switchedAt": payload["generatedAt"],
            "pageMapVersion": MAPPING_VERSION,
            "active": out_path.name,
            "activeSHA256": digest(out_path.read_bytes()),
            "activeMappedDocuments": len(mapped),
            "previous": source_path.name,
            "previousSHA256": digest(source_path.read_bytes()),
            "pointServiceAt": "DSH_KNOWLEDGE_CATALOG=%s" % out_path,
            "rollback": "set DSH_KNOWLEDGE_CATALOG back to %s (left unmodified)" % source_path,
            "unmappedDocuments": [entry["documentId"] for entry in pooled],
        }
        Path(arguments.activation).write_bytes(
            json.dumps(activation, indent=2, ensure_ascii=False).encode("utf-8"))
        summary["activation"] = activation
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if mapped else 1


if __name__ == "__main__":
    sys.exit(main())
