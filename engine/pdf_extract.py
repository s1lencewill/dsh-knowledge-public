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
import hashlib
import json
from pathlib import Path
import re
import sys

EXTRACTOR = "extract-literature"
VERSION = "2.0"
#: Vertical tolerance (points) for two fragments to count as the same visual line.
LINE_TOLERANCE = 2.0
#: Lines whose baseline sits within this fraction of the page height of an edge are running heads/feet.
MARGIN_FRACTION = 0.055
#: A page counts as two-column when both x-clusters hold at least this share of the page's lines.
COLUMN_MIN_SHARE = 0.20
#: Minimum horizontal gutter between the two column clusters, as a fraction of the page width.
COLUMN_MIN_GUTTER = 0.12
REPLACEMENT = "\ufffd"
CAPTION = re.compile(r"^\s*(fig(?:ure)?s?\.?|figs?\.?|tab(?:le)?s?\.?|tabs?\.?|scheme|chart|图|表)\s*"
                     r"[\d一二三四五六七八九十]", re.IGNORECASE)
HEADING = re.compile(r"^(\d+(\.\d+)*\s+\S|第[一二三四五六七八九十]+[章节部分]|[\u4e00-\u9fff]{2,20}$)")
MERGED_LATIN = re.compile(r"\b[A-Za-z]{16,}\b")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    value = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def is_private_use(character):
    return 0xE000 <= ord(character) <= 0xF8FF or 0xF0000 <= ord(character) <= 0x10FFFD


def glyph_advance(text, font_dict, size):
    """Advance width of a text run in device units, from the font's own /Widths where available.

    Estimating word widths from character counts is what makes layout reconstruction unreliable: a line
    of lowercase prose is about a quarter of an em per character, a line of capitals nearer three
    quarters. The PDF already says how wide each glyph is, so the widths are read from the font
    dictionary and only fall back to an estimate for fonts that carry none (CID fonts).
    """
    widths = None
    first = 0
    if isinstance(font_dict, dict):
        table = font_dict.get("/Widths")
        if table is not None:
            try:
                widths = [float(value) for value in table]
                first = int(font_dict.get("/FirstChar", 0) or 0)
            except (TypeError, ValueError):
                widths = None
    total = 0.0
    for character in text:
        if character == "\n":
            continue
        if widths is not None:
            code = ord(character)
            if first <= code - first < len(widths) or 0 <= code - first < len(widths):
                index = code - first
                if 0 <= index < len(widths):
                    total += widths[index] / 1000.0
                    continue
        total += 1.0 if _cjk(character) else (0.25 if character == " " else 0.5)
    return total * (size or 0.0)


def collect_fragments(page):
    """Every text-showing operation as (x, y, text, size, advance) in device space."""
    fragments = []

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        scale = (cm[0] ** 2 + cm[1] ** 2) ** 0.5 or 1.0
        size = abs(font_size or 0) * scale
        fragments.append((x, y, text, size, glyph_advance(text, font_dict, size)))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception as error:
        return [], "%s: %s" % (type(error).__name__, error)
    return fragments, None


def split_fragment(text, x, y, size, advance):
    """Split one text operation into visual-line pieces at the breaks pypdf inserted.

    A single `TJ` can carry a whole multi-line run; pypdf marks the inner breaks with `\\n`. Keeping the
    newline would merge two visual lines into one, so the piece after a break becomes its own line one
    standard leading below the first - an inference, so the page audit counts these (`multiLineFragments`)
    and they are visible in `flags`. The advance is shared out by character count.
    """
    pieces = text.split("\n")
    if len(pieces) == 1:
        return [(x, y, text, size, advance)], 0
    leading = 1.2 * (size or 10.0)
    total = sum(len(piece) for piece in pieces) or 1
    result = []
    for index, piece in enumerate(pieces):
        if piece.strip():
            result.append((x, y - leading * index, piece, size, advance * len(piece) / total))
    return result, len(pieces) - 1


def cluster_rows(fragments):
    """Group fragments into rows by baseline, then order each row by x."""
    rows = []
    for x, y, text, size, advance in sorted(fragments, key=lambda item: (-item[1], item[0])):
        for row in rows:
            if abs(row["y"] - y) <= max(LINE_TOLERANCE, 0.5 * (row["size"] or 0)):
                row["fragments"].append((x, text, size, advance))
                row["y"] = (row["y"] * row["n"] + y) / (row["n"] + 1)
                row["n"] += 1
                row["size"] = max(row["size"], size)
                break
        else:
            rows.append({"y": y, "size": size, "n": 1, "fragments": [(x, text, size, advance)]})
    for row in rows:
        row["fragments"].sort(key=lambda item: item[0])
    return rows


def _row_gaps(row):
    """True gaps between consecutive fragments on a row: next start minus previous end."""
    gaps = []
    for first, second in zip(row["fragments"], row["fragments"][1:]):
        gaps.append(second[0] - (first[0] + first[3]))
    return gaps


def _page_gap_baseline(rows):
    """A region-boundary threshold for the whole page, measured from its own gap distribution.

    With real glyph advances on hand, the gaps are true whitespace: word spaces and superscript offsets
    are a few points, region boundaries are tens of points. The lower quartile captures the small-gap
    scale, and the threshold is a few times that, capped at a fraction of the page width so a tight
    gutter is still caught. A CJK page, where every glyph is its own fragment, lands on the one-em floor.
    """
    gaps = [gap for row in rows for gap in _row_gaps(row) if gap > 0]
    if not gaps:
        return None
    ordered = sorted(gaps)
    return ordered[len(ordered) // 4]


def split_rows(rows, baseline=None, page_width=None):
    """Split a row into segments wherever the horizontal gap is too large to be a word space.

    A row is a baseline, not a reading unit: on a page with a side box, a full-width abstract or two
    columns, unrelated regions share baselines. Joining them would interleave two different texts into
    one line, so a region-sized gap starts a new segment.
    """
    baseline = _page_gap_baseline(rows) if baseline is None else baseline
    limit = 0.075 * page_width if page_width else None
    candidates = [value for value in (3.0 * baseline if baseline else None, limit) if value]
    boundary = min(candidates) if candidates else None
    segments = []
    for row in rows:
        size = row["size"] or 10.0
        threshold = max(0.9 * size, boundary) if boundary else 0.9 * size
        current, previous = [], None
        for item in row["fragments"]:
            if previous is not None:
                gap = item[0] - (previous[0] + previous[3])
                if gap > threshold:
                    segments.append(_segment(current, row, bound=item[0], page_width=page_width))
                    current = []
            current.append(item)
            previous = item
        if current:
            segments.append(_segment(current, row, bound=page_width, page_width=page_width))
    return segments


def _segment(fragments, row, bound=None, page_width=None):
    """One visual-line segment; its right edge comes from the fonts' own glyph advances."""
    estimate = max(x + advance for x, _, _, advance in fragments)
    right = min(estimate, bound) if bound else estimate
    if page_width:
        right = min(right, page_width)
    x0 = min(x for x, _, _, _ in fragments)
    return {"y": row["y"], "size": row["size"], "fragments": list(fragments),
            "text": "".join(text for _, text, _, _ in fragments),
            "x0": x0, "x1": max(x0, right)}


def detect_columns(segments, width):
    """(column_count, gutter, ambiguous) from where the segments start.

    Full-width elements (title, abstract) are excluded from the mode analysis, because they start left
    and would otherwise hide a genuine right-hand column. `ambiguous` marks the honest middle case: a
    page that clearly has a right-hand region but no clean gutter between the regions, where the line
    order follows the page's baselines rather than the visual columns and a reader must check the PDF.
    """
    candidates = [segment for segment in segments if len(segment["text"].strip()) > 2]
    narrow = [segment for segment in candidates if (segment["x1"] - segment["x0"]) < 0.55 * width]
    if len(candidates) < 8 or len(narrow) < 8:
        return 1, None, False
    left = [segment for segment in narrow if segment["x0"] < width * 0.5]
    right = [segment for segment in narrow if segment["x0"] >= width * 0.5]
    if len(left) < len(narrow) * 0.25 or len(right) < len(narrow) * 0.25:
        return 1, None, False
    gutter = (max(segment["x1"] for segment in left), min(segment["x0"] for segment in right))
    if gutter[1] - gutter[0] < width * 0.02:
        return 1, None, True
    return 2, gutter, False


def _span(segment):
    size = segment["size"] or 10.0
    return segment["x0"], segment["x1"], segment["y"] - 0.3 * size, segment["y"] + 0.8 * size


def build_blocks(segments):
    """Group segments into layout blocks: horizontally overlapping and vertically adjacent.

    A page is not a list of lines. An abstract next to a side box, a table next to a paragraph, a
    caption under a figure - those are separate regions whose baselines interleave. Grouping them into
    blocks first is what lets the reading order follow the layout instead of the baseline grid.
    """
    parent = list(range(len(segments)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first, second):
        first, second = find(first), find(second)
        if first != second:
            parent[max(first, second)] = min(first, second)

    spans = [_span(segment) for segment in segments]
    for i in range(len(segments)):
        xi0, xi1, yi0, yi1 = spans[i]
        for j in range(i + 1, len(segments)):
            xj0, xj1, yj0, yj1 = spans[j]
            overlap = min(xi1, xj1) - max(xi0, xj0)
            if overlap <= 0:
                continue
            narrower = min(xi1 - xi0, xj1 - xj0)
            if overlap < 0.30 * narrower:
                continue
            gap = max(yi0 - yj1, yj0 - yi1)
            if gap > 1.6 * max(segments[i]["size"] or 10.0, segments[j]["size"] or 10.0):
                continue
            union(i, j)

    blocks = {}
    for index, segment in enumerate(segments):
        blocks.setdefault(find(index), []).append(segment)
    result = []
    for members in blocks.values():
        result.append({
            "segments": members,
            "x0": min(segment["x0"] for segment in members),
            "x1": max(segment["x1"] for segment in members),
            "y1": max(segment["y"] for segment in members),
            "y0": min(segment["y"] for segment in members),
        })
    return result


def _vertical_cut(segments):
    """Split into left/right groups at the widest x band that no segment crosses, or None."""
    ordered = sorted(segments, key=lambda segment: segment["x0"])
    best, best_gap = None, 0.0
    for index in range(1, len(ordered)):
        left, right = ordered[:index], ordered[index:]
        if max(segment["x1"] for segment in left) > min(segment["x0"] for segment in right):
            continue
        gap = min(segment["x0"] for segment in right) - max(segment["x1"] for segment in left)
        if gap > best_gap:
            best, best_gap = (left, right), gap
    return best


def _horizontal_cut(segments, minimum=2.0):
    """Split into top/bottom groups at the widest y band that no segment crosses, or None.

    Horizontal whitespace separates the running head, the title block and the body. Cutting there first
    keeps a header line that happens to reach across a column gutter from blocking the column split
    below it.
    """
    intervals = sorted(((segment["y"] - 0.3 * (segment["size"] or 10.0),
                         segment["y"] + 0.8 * (segment["size"] or 10.0), segment)
                        for segment in segments), key=lambda item: item[0])
    best, best_gap, highest_end = None, minimum, None
    for index in range(len(intervals) - 1):
        highest_end = intervals[index][1] if highest_end is None else max(highest_end, intervals[index][1])
        gap = intervals[index + 1][0] - highest_end
        if gap > best_gap:
            best, best_gap = index, gap
    if best is None:
        return None
    # intervals run bottom to top (y grows upward), so the higher group is read first
    return ([item[2] for item in intervals[best + 1:]], [item[2] for item in intervals[:best + 1]])


def _gutter_is_wide(segments, page_width, minimum=0.02):
    """True when the widest crossing-free x band is wide enough to be a column gutter."""
    cut = _vertical_cut(segments)
    if cut is None:
        return False
    gap = min(segment["x0"] for segment in cut[1]) - max(segment["x1"] for segment in cut[0])
    return gap >= minimum * page_width


def order_region(segments, page_width, depth=0, trace=None):
    """Reading order of one region: full-width lines split it into bands, then a gutter splits columns.

    This is the classic recursive XY-cut, with one addition that real research pages need: a line that
    spans most of the *page* and starts at its text margin (title, abstract rule, a full-width equation)
    is a band boundary, so the side-by-side parts below it are ordered as separate regions instead of
    being interleaved line by line. Everything is decided from geometry; no text is moved across regions.

    `trace`, when a list is passed, receives one record per decision so a diagnostic or a test can see
    why a page was ordered the way it was.
    """
    if len(segments) <= 1 or depth > 8:
        if trace is not None:
            trace.append({"depth": depth, "segments": len(segments), "decision": "leaf"})
        return sorted(segments, key=lambda segment: (-segment["y"], segment["x0"]))
    # A gutter wider than a couple of percent of the page is a real column boundary and is read
    # column-by-column; otherwise the page is a stack of bands and is read band by band.
    gutter = _vertical_cut(segments) if _gutter_is_wide(segments, page_width) else None
    if gutter:
        if trace is not None:
            trace.append({"depth": depth, "segments": len(segments), "gutter": True})
        return (order_region(gutter[0], page_width, depth + 1, trace)
                + order_region(gutter[1], page_width, depth + 1, trace))
    horizontal = _horizontal_cut(segments)
    if trace is not None:
        trace.append({"depth": depth, "segments": len(segments),
                      "horizontal": None if horizontal is None else [len(horizontal[0]), len(horizontal[1])]})
    if horizontal:
        return (order_region(horizontal[0], page_width, depth + 1, trace)
                + order_region(horizontal[1], page_width, depth + 1, trace))
    wide = [segment for segment in segments
            if (segment["x1"] - segment["x0"]) > 0.65 * page_width and segment["x0"] < 0.22 * page_width]
    if trace is not None:
        trace.append({"depth": depth, "segments": len(segments), "wide": len(wide),
                      "span": [round(min(s["x0"] for s in segments), 1), round(max(s["x1"] for s in segments), 1)]})
    if wide:
        bands, current = [], []
        for segment in sorted(segments, key=lambda segment: -segment["y"]):
            if any(segment is item for item in wide):
                bands.append(current)
                current = []
                bands.append([segment])
            else:
                current.append(segment)
        bands.append(current)
        ordered = []
        for band in bands:
            ordered.extend(order_region(band, page_width, depth + 1, trace))
        return ordered
    cut = _vertical_cut(segments)
    if trace is not None:
        trace.append({"depth": depth, "segments": len(segments), "gutter": None if cut is None
                      else [round(min(s["x0"] for s in cut[1]), 1)]})
    if cut:
        return (order_region(cut[0], page_width, depth + 1, trace)
                + order_region(cut[1], page_width, depth + 1, trace))
    return sorted(segments, key=lambda segment: (-segment["y"], segment["x0"]))


def order_lines(segments, width, height, trace=None):
    """Reading order over the page's layout; inside a region, top to bottom then left to right."""
    return order_region(segments, width, 0, trace)


MARKUP_ONLY = re.compile(r"^[\d\s.,;:+\-–—()\[\]/×*†‡§¶'\"a-z]+$")


def merge_inline_markup(lines):
    """Attach super/subscript fragments to the body line they belong to.

    A short line whose baseline sits just off a neighbouring body line and whose x-range overlaps that
    line (or starts right after it) is an inline superscript or subscript - a citation number, an
    exponent, a formula index. Leaving it as its own line would break the sentence and invent a line
    break, so it is merged back at its x position. The decision uses glyph geometry only, never the
    text's meaning; the closest eligible host wins.
    """
    absorbed, merged = set(), 0
    for line in lines:
        text = line["text"]
        if not text or len(text) > 6 or not MARKUP_ONLY.match(text):
            continue
        best = None
        for host in lines:
            if host is line or id(host) in absorbed or line.get("isHeaderFooter") or host.get("isHeaderFooter"):
                continue
            if host.get("column", 1) != line.get("column", 1):
                continue
            size = max(host["size"] or 10.0, 1.0)
            gap = abs(line["y"] - host["y"])
            if gap == 0 or gap > 0.9 * size:
                continue
            if line["x0"] > host["x1"] + 1.5 * size or line["x1"] < host["x0"] - 1.5 * size:
                continue
            if best is None or gap < best[0]:
                best = (gap, host)
        if best is None:
            continue
        host = best[1]
        host["fragments"].extend(line["fragments"])
        host["fragments"].sort(key=lambda item: item[0])
        host["text"] = "".join(piece for _, piece, _, _ in host["fragments"])
        host["x0"] = min(x for x, _, _, _ in host["fragments"])
        host["x1"] = max(x + advance for x, _, _, advance in host["fragments"])
        absorbed.add(id(line))
        merged += 1
    return [line for line in lines if id(line) not in absorbed], merged


def page_segments(page):
    """The page's segments plus raw page facts (shared by the extractor and its diagnostics).

    A fragment whose device position falls outside the page box is a transform artifact *for layout
    purposes*, but it can still carry real text (a footer drawn below the media box, a rotated page).
    It is therefore clamped into the page rather than dropped: nothing disappears from the text, the
    layout maths still sees sane geometry, and the audit records how many were clamped.
    """
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    crop = getattr(page, "cropbox", None)
    left, bottom = 0.0, 0.0
    if crop is not None:
        try:
            left, bottom = float(crop.left), float(crop.bottom)
            width, height = float(crop.width), float(crop.height)
        except (AttributeError, TypeError, ValueError):
            left, bottom = 0.0, 0.0
    fragments, error = collect_fragments(page)
    kept, clamped = [], 0
    for x, y, text, size, advance in fragments:
        inside_x = left - 8.0 <= x <= left + width + 8.0
        inside_y = bottom - 8.0 <= y <= bottom + height + 8.0
        if not inside_x:
            x, clamped = min(max(x, left), left + width), clamped + 1
        if not inside_y:
            y, clamped = min(max(y, bottom), bottom + height), clamped + 1
        kept.append((x, y, text, size, advance))
    split_fragments, multi_line = [], 0
    for x, y, text, size, advance in kept:
        pieces, extra = split_fragment(text, x, y, size, advance)
        split_fragments.extend(pieces)
        multi_line += extra
    return (split_rows(cluster_rows(split_fragments), page_width=width),
            {"extractError": error, "clampedFragments": clamped,
             "multiLineFragments": multi_line, "width": width, "height": height,
             "left": left, "bottom": bottom})


def _region_alternations(ordered, split):
    """How often the ordered *visual lines* switch between the left and right half of the page.

    A correctly ordered two-column page switches a handful of times (header, left column, right column).
    A high count means the line order still follows the baseline grid because the regions share
    baselines without a clean gutter - the honest "check the PDF" case, recorded per page. Segments that
    share a baseline are one visual line, so they cannot count as a switch.
    """
    previous_side, previous_y, count = None, None, 0
    for segment in ordered:
        if previous_y is not None and abs(segment["y"] - previous_y) <= max(LINE_TOLERANCE, 0.5 * (segment["size"] or 0)):
            continue
        side = 0 if segment["x0"] < split else 1
        if previous_side is not None and side != previous_side:
            count += 1
        previous_side, previous_y = side, segment["y"]
    return count


def page_lines(page):
    """Visual lines of one page, plus page flags."""
    segments, page_flags = page_segments(page)
    width, height = page_flags["width"], page_flags["height"]
    error = page_flags["extractError"]
    outside = page_flags["clampedFragments"]
    multi_line = page_flags["multiLineFragments"]
    columns, gutter, ambiguous = detect_columns(segments, width)
    trace = []
    ordered = order_lines(segments, width, height, trace)
    split = (gutter[0] + gutter[1]) / 2 if gutter else width * 0.5
    alternations = _region_alternations(ordered, split)
    for index, line in enumerate(ordered):
        line["column"] = 1 if line["x0"] < split else 2
        line["orderIndex"] = index
    embedded = 0
    for line in ordered:
        line["text"], breaks = clean_line(line["text"])
        embedded += breaks
        text = line["text"]
        line["isHeaderFooter"] = line["y"] > height * (1 - MARGIN_FRACTION) or line["y"] < height * MARGIN_FRACTION
        line["isCaption"] = bool(CAPTION.match(text))
        line["isHeading"] = bool(HEADING.match(text)) and len(text) <= 40
        line["mergedLatin"] = MERGED_LATIN.findall(text)
    ordered, inline_markup = merge_inline_markup(ordered)
    for line in ordered:
        line["text"], breaks = clean_line(line["text"])
        embedded += breaks
        line["singleChar"] = len(line["text"]) == 1
    flags = {"columns": columns, "gutter": gutter, "extractError": error,
             "inlineMarkupMerged": inline_markup, "embeddedBreaksFolded": embedded,
             "layoutAmbiguous": ambiguous, "regionAlternations": alternations,
             "clampedFragments": outside, "multiLineFragments": multi_line,
             "width": width, "height": height}
    return [line for line in ordered if line["text"]], flags


def _cjk(character):
    return bool(character) and ("\u3400" <= character <= "\u9fff" or "\u3000" <= character <= "\u30ff")


def clean_line(value):
    """One visual line must not contain a line break.

    pypdf hands back fragments that can carry their own `\\n` (a positioning move inside one text
    operation). Left in place those breaks would inflate the physical line count and, worse, make the
    page/line map disagree with the file - which is exactly what citations are built on. They are
    folded into the line: a space between Latin words, nothing between CJK characters.
    """
    embedded = value.count("\n")
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    value = re.sub(r" {2,}", " ", value)
    value = re.sub(r"(?<=[\u3400-\u9fff\u3000-\u30ff]) (?=[\u3400-\u9fff\u3000-\u30ff])", "", value)
    value = re.sub(r"(?<=[\u3400-\u9fff\u3000-\u30ff]) (?=[.,;:!?)\]}，。；：！？）】》])", "", value)
    return value.strip(), embedded


def paragraph_join(lines):
    """Join wrapped visual lines into paragraphs **only** where the layout says it is safe.

    A join needs the same column, an adjacent baseline, no paragraph gap, neither line being a heading,
    caption or running head, and the previous line not ending a sentence. Everything else starts a new
    paragraph, so tables, captions, headings and page furniture stay separate.
    """
    paragraphs, current, previous = [], [], None
    for line in lines:
        text = line["text"].strip()
        if not text:
            continue
        breaks = (previous is None
                  or line["isHeading"] or line["isCaption"] or line["isHeaderFooter"]
                  or previous["isHeading"] or previous["isCaption"] or previous["isHeaderFooter"]
                  or (previous["text"].strip().endswith((".", "。", "！", "？", "!", "?", "；", ";", "：", ":"))
                      and not re.search(r"(et al|e\.g|i\.e|Fig|Eq)\.$", previous["text"].strip()))
                  or line["column"] != previous["column"]
                  or abs(line["y"] - previous["y"]) > 2.2 * max(previous.get("size") or 10.0, 1.0))
        if breaks and current:
            paragraphs.append("".join(current))
            current = []
        if current:
            previous_text = current[-1]
            joiner = "" if _cjk(text[:1]) and _cjk(previous_text[-1:]) else (
                "" if _cjk(text[:1]) or _cjk(previous_text[-1:]) else " ")
            current.append(joiner + text)
        else:
            current.append(text)
        previous = line
    if current:
        paragraphs.append("".join(current))
    return paragraphs


def normalize_for_match(text):
    """Matching-only normalization: never quoted, never cited.

    It exists so a comparison is not defeated by a missing space, a wrapped hyphen or a hard line
    break. Nothing here is used to build quotations.
    """
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"-\n(?=[a-z])", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{2,}", "\n", value)
    return value.strip()


def squash(text):
    """Space-insensitive key, so a title whose spaces were lost in the text layer still matches."""
    return re.sub(r"[\s\u3000]+", "", text).casefold()


def page_audit(lines, page_number, flags):
    return {
        "page": page_number,
        "lines": len(lines),
        "characters": sum(len(line["text"]) for line in lines),
        "nonWhitespace": sum(1 for line in lines for character in line["text"] if not character.isspace()),
        "singleCharLines": sum(1 for line in lines if line["singleChar"]),
        "replacementChars": sum(line["text"].count(REPLACEMENT) for line in lines),
        "privateUseChars": sum(1 for line in lines for character in line["text"] if is_private_use(character)),
        "captions": sum(1 for line in lines if line["isCaption"]),
        "headings": sum(1 for line in lines if line["isHeading"]),
        "headerFooter": sum(1 for line in lines if line["isHeaderFooter"]),
        "mergedLatinTokens": sorted({token for line in lines for token in line["mergedLatin"]}),
        "columns": flags.get("columns", 1),
        "layoutAmbiguous": bool(flags.get("layoutAmbiguous")),
        "regionAlternations": flags.get("regionAlternations"),
        "multiLineFragments": flags.get("multiLineFragments"),
        "clampedFragments": flags.get("clampedFragments"),
        "embeddedBreaksFolded": flags.get("embeddedBreaksFolded"),
        "inlineMarkupMerged": flags.get("inlineMarkupMerged"),
        "width": flags.get("width"), "height": flags.get("height"),
    }


def strip_markers(text):
    return re.sub(r"^PDF page \d+\s*$", "", text, flags=re.MULTILINE)


def content(text):
    """Non-whitespace content with page markers removed - what a version diff should compare."""
    return re.sub(r"\s+", "", strip_markers(text))


def compare(old_text, new_text):
    old_lines, new_lines = old_text.split("\n"), new_text.split("\n")
    old_content, new_content = content(old_text), content(new_text)
    old_counts, new_counts = {}, {}
    for character in old_content:
        old_counts[character] = old_counts.get(character, 0) + 1
    for character in new_content:
        new_counts[character] = new_counts.get(character, 0) + 1
    missing = sum(max(0, old_counts.get(character, 0) - new_counts.get(character, 0))
                  for character in set(old_counts) | set(new_counts))
    added = sum(max(0, new_counts.get(character, 0) - old_counts.get(character, 0))
                for character in set(old_counts) | set(new_counts))
    return {
        "oldLines": len(old_lines), "newLines": len(new_lines),
        "oldChars": len(old_text), "newChars": len(new_text),
        "oldContentChars": len(old_content), "newContentChars": len(new_content),
        "oldSingleCharLines": sum(1 for line in old_lines if len(line.strip()) == 1),
        "newSingleCharLines": sum(1 for line in new_lines if len(line.strip()) == 1),
        "oldCjkRuns": len(re.findall(r"[\u3400-\u9fff]{2,}", old_text)),
        "newCjkRuns": len(re.findall(r"[\u3400-\u9fff]{2,}", new_text)),
        "contentCharsMissingFromNew": missing,
        "contentCharsAddedInNew": added,
        "sameContent": missing == 0 and added == 0,
    }


def render(pdf, identifier, ocr_pages=None, old_text=None):
    from pypdf import PdfReader
    reader = PdfReader(str(pdf))
    body, paragraph_body, page_map, audits, flags = [], [], [], [], []
    line_number = 0
    for index, page in enumerate(reader.pages, start=1):
        lines, page_flags = page_lines(page)
        line_number += 1
        start = line_number
        body.append("PDF page %d" % index)
        paragraph_body.append("PDF page %d" % index)
        audit = page_audit(lines, index, page_flags)
        for line in lines:
            line_number += 1
            body.append(line["text"])
        paragraph_body.extend(paragraph_join(lines))
        page_map.append({"page": index, "startLine": start, "endLine": line_number,
                         "characters": audit["characters"], "nonWhitespace": audit["nonWhitespace"],
                         "replacementChars": audit["replacementChars"],
                         "privateUseChars": audit["privateUseChars"],
                         "singleCharLines": audit["singleCharLines"], "columns": audit["columns"]})
        audits.append(audit)
        if page_flags.get("extractError"):
            flags.append({"page": index, "flag": "extract-error", "detail": page_flags["extractError"]})
        if audit["replacementChars"] or audit["privateUseChars"]:
            flags.append({"page": index, "flag": "character-loss",
                          "replacementChars": audit["replacementChars"],
                          "privateUseChars": audit["privateUseChars"]})
        if audit["columns"] == 2:
            flags.append({"page": index, "flag": "two-column"})
        if audit["layoutAmbiguous"]:
            flags.append({"page": index, "flag": "layout-ambiguous",
                          "detail": "right-hand region without a clean gutter; line order follows baselines"})
        if audit["captions"]:
            flags.append({"page": index, "flag": "caption-present", "count": audit["captions"]})
        if audit["mergedLatinTokens"]:
            flags.append({"page": index, "flag": "possible-space-loss",
                          "count": len(audit["mergedLatinTokens"])})

    text = "\n".join(body) + "\n"
    paragraphs = "\n".join(paragraph_body) + "\n"
    audit = {
        "extractor": EXTRACTOR, "extractorVersion": VERSION,
        "parameters": {"lineTolerance": LINE_TOLERANCE, "marginFraction": MARGIN_FRACTION,
                       "columnMinShare": COLUMN_MIN_SHARE, "columnMinGutter": COLUMN_MIN_GUTTER},
        "pdf": Path(pdf).name, "pdfSHA256": sha256_file(pdf), "pdfBytes": Path(pdf).stat().st_size,
        "text": identifier, "textSHA256": digest(text.encode("utf-8")),
        "textBytes": len(text.encode("utf-8")),
        "pages": page_map, "pageAudits": audits, "flags": flags,
        "singleCharLines": sum(item["singleCharLines"] for item in page_map),
        "replacementChars": sum(item["replacementChars"] for item in page_map),
        "privateUseChars": sum(item["privateUseChars"] for item in page_map),
        "ocrPages": sorted(ocr_pages or {}),
    }
    if old_text is not None:
        audit["previous"] = compare(old_text, text)
    return text, normalize_for_match(text), paragraphs, audit

