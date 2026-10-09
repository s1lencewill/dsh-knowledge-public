"""Build a versioned literature corpus with the layout-aware extractor (M0, offline).

Reads the previous corpus's `source-map.json` for the PDF list, extracts every document with
`scripts/extract-literature.py`, and writes a new, self-contained version directory:

    texts/        citable text (coordinate-reconstructed visual lines + page markers)
    normalized/   matching-only normalization (never quoted)
    paragraphs/   matching-only paragraph join (never quoted)
    audit/        per-document extractor, version, parameters, page map, flags, version diff
    ocr/          auxiliary OCR cross-checks for flagged pages
    source-map.json / corpus.json

The previous version is never modified; the PDFs are only read.

    .\\.venv\\Scripts\\python.exe scripts\\build-literature-corpus.py \
        --source-map <old source-map.json> --out <new version dir> [--only <text name>] [--ocr <json>]
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]


def load_extractor():
    spec = importlib.util.spec_from_file_location("extract_literature", ROOT / "scripts/extract-literature.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def slug(value):
    """Stable, ASCII-safe, human-readable document id base."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    ascii_value = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-").lower()
    if ascii_value:
        return ascii_value[:48]
    return "doc-" + re.sub(r"[^0-9a-f]", "", __import__("hashlib").sha256(value.encode()).hexdigest())[:12]


def main():
    parser = argparse.ArgumentParser(description="Build a versioned literature corpus.")
    parser.add_argument("--source-map", required=True, help="the previous version's source-map.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", action="append", default=[], help="limit to these text names")
    parser.add_argument("--ocr", help="JSON {text name: {page: ocr text path}}")
    arguments = parser.parse_args()

    extractor = load_extractor()
    previous = json.loads(Path(arguments.source_map).read_text(encoding="utf-8-sig"))
    ocr_map = {name: {int(page): Path(path) for page, path in pages.items()}
               for name, pages in (json.loads(Path(arguments.ocr).read_text(encoding="utf-8-sig"))
                                   if arguments.ocr else {}).items()}
    out = Path(arguments.out)
    old_texts = Path(arguments.source_map).parent / "texts"
    summary, corpus = [], {}
    for entry in previous:
        name = entry["text"]
        if arguments.only and name not in arguments.only:
            continue
        pdf = Path(entry["pdf"])
        identifier = name[:-4] if name.lower().endswith(".txt") else name
        old_path = old_texts / name
        old_text = old_path.read_text(encoding="utf-8-sig") if old_path.is_file() else None
        text, normalized, paragraphs, audit = extractor.render(pdf, identifier, ocr_map.get(name, {}), old_text)
        document_id = "%s-%s" % (slug(identifier), audit["pdfSHA256"][:6])
        audit["documentId"] = document_id
        audit["textFile"] = name
        audit["normalizedFile"] = name
        for folder, payload in (("texts", text), ("normalized", normalized), ("paragraphs", paragraphs)):
            (out / folder).mkdir(parents=True, exist_ok=True)
            (out / folder / name).write_bytes(payload.encode("utf-8"))
        (out / "audit").mkdir(parents=True, exist_ok=True)
        (out / "audit" / (identifier + ".json")).write_text(
            json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
        review = sorted({flag["page"] for flag in audit["flags"]
                         if flag["flag"] in ("character-loss", "extract-error")})
        record = {"documentId": document_id, "text": name, "pdf": Path(entry["pdf"]).name,
                  "pdfSHA256": audit["pdfSHA256"], "pdfBytes": audit["pdfBytes"],
                  "textSHA256": audit["textSHA256"], "textBytes": audit["textBytes"],
                  "pages": audit["pages"], "pageCount": len(audit["pages"]),
                  "reviewPages": review, "flags": audit["flags"],
                  "extractor": audit["extractor"], "extractorVersion": audit["extractorVersion"],
                  "parameters": audit["parameters"], "ocrPages": audit["ocrPages"],
                  "previous": audit.get("previous")}
        corpus[document_id] = record
        summary.append({"id": document_id, "name": name, "pages": len(audit["pages"]),
                        "lines": len(text.split("\n")), "singleCharLines": audit["singleCharLines"],
                        "replacementChars": audit["replacementChars"], "privateUseChars": audit["privateUseChars"],
                        "flags": len(audit["flags"]), "reviewPages": review,
                        "diff": audit.get("previous")})

    (out / "source-map.json").write_text(json.dumps(list(corpus.values()), indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    (out / "corpus.json").write_text(json.dumps({
        "version": extractor.VERSION, "extractor": extractor.EXTRACTOR,
        "parameters": {"lineTolerance": extractor.LINE_TOLERANCE, "marginFraction": extractor.MARGIN_FRACTION,
                       "columnMinShare": extractor.COLUMN_MIN_SHARE, "columnMinGutter": extractor.COLUMN_MIN_GUTTER},
        "documents": len(corpus), "from": str(arguments.source_map), "documentsById": list(corpus),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    totals = {"documents": len(summary),
              "singleCharLines": sum(item["singleCharLines"] for item in summary),
              "replacementChars": sum(item["replacementChars"] for item in summary),
              "privateUseChars": sum(item["privateUseChars"] for item in summary),
              "pages": sum(item["pages"] for item in summary)}
    print(json.dumps(totals, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
