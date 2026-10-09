"""Layout-aware literature extraction, versioned (M0).

Why this exists: the previous corpus was produced by a naive text-layer dump. On two Chinese PDFs
every glyph arrived on its own line (so CJK bigram retrieval could not match anything), some Latin
headings lost their spaces, and nine pages carry replacement or private-use characters. This extractor
rebuilds a page from **glyph coordinates** instead of guessing at line structure:

* every text-showing operation contributes a fragment with its device position, so lines are clustered
  by y and ordered by x - the page's real visual lines are recovered without deleting or inserting a
  single newline;
* two-column pages are detected from the x-distribution and ordered column by column, so columns,
  captions, tables and running heads are never merged into one another;
* nothing is invented: a region that cannot be reconstructed faithfully is flagged, never rewritten;
* the citable text is the coordinate-reconstructed visual lines, while separate, clearly marked
  *normalized* and *paragraph* variants exist only for matching and must never be quoted;
* per-document audit (extractor, version, parameters, page/line map, flags, repair counts, version
  diff) is written next to the text; OCR text for a flagged page is recorded as an auxiliary
  cross-check, not as a replacement for the extractor's output.

Usage:

    .\\.venv\\Scripts\\python.exe scripts\\extract-literature.py --pdf <file.pdf> --id <name> \
        --out <dir> [--old <old.txt>] [--ocr <page>=<text file>]

The script never writes outside `--out`, never modifies a PDF, and prints structural counts only.
"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from pdf_extract import *  # Shared layout engine: CLI and onboarding use one implementation.

def main(argv=None):
    parser = argparse.ArgumentParser(description="Layout-aware, versioned literature extraction.")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--id", required=True, help="output basename (no private path in outputs)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--old", help="previous extraction of the same document, for the version diff")
    parser.add_argument("--ocr", action="append", default=[],
                        help="page=path; recorded as an auxiliary cross-check for a flagged page")
    arguments = parser.parse_args(argv)

    ocr_pages = {}
    for item in arguments.ocr:
        page, _, path = item.partition("=")
        if not page.isdigit() or not path:
            parser.error("--ocr expects <page>=<path>")
        ocr_pages[int(page)] = Path(path)

    out = Path(arguments.out)
    for name in ("texts", "normalized", "paragraphs", "audit", "ocr"):
        (out / name).mkdir(parents=True, exist_ok=True)
    old_text = Path(arguments.old).read_text(encoding="utf-8-sig") if arguments.old else None
    text, normalized, paragraphs, audit = render(arguments.pdf, arguments.id, ocr_pages, old_text)
    (out / "texts" / (arguments.id + ".txt")).write_bytes(text.encode("utf-8"))
    (out / "normalized" / (arguments.id + ".txt")).write_bytes(normalized.encode("utf-8"))
    (out / "paragraphs" / (arguments.id + ".txt")).write_bytes(paragraphs.encode("utf-8"))
    if ocr_pages:
        audit["ocr"] = {}
        for page, path in sorted(ocr_pages.items()):
            target = out / "ocr" / ("%s-p%d.txt" % (arguments.id, page))
            content = Path(path).read_text(encoding="utf-8-sig")
            target.write_text(content, encoding="utf-8")
            audit["ocr"][str(page)] = {"file": target.name, "sha256": digest(content.encode("utf-8")),
                                       "characters": len(content)}
    (out / "audit" / (arguments.id + ".json")).write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"id": arguments.id, "pages": len(audit["pages"]), "lines": len(text.split("\n")),
                      "singleCharLines": audit["singleCharLines"],
                      "replacementChars": audit["replacementChars"],
                      "privateUseChars": audit["privateUseChars"],
                      "flags": len(audit["flags"]), "previous": audit.get("previous")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
