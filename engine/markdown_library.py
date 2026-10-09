"""Build a library's catalogue and authorization manifest from a directory of Markdown notes.

A Markdown corpus is not a literature corpus. The existing identity builder (`scripts/build-identity-
catalog.py`) reads PDF extraction output: title blocks, "Cite This" markers, journal regexes, per-page
maps. None of that exists in a notes wiki, and hard-coding a second, throwaway script for one directory
would leave the next wiki with no path at all. So the reusable half lives here: one module that turns

    a root directory of `.md` files

into

    a catalogue the service can enumerate and locate from (`catalog.load_bound`)
    + an authorization manifest (`realDataTrial.files`) the service re-checks on every call.

What it does **not** do, deliberately:

* it never writes to the corpus. The scan is read-only, every path it opens is under `root`, and it
  follows no link or junction (`evidence.walk_text_files` is the same selection rule the snapshot uses,
  so the manifest cannot describe a file set the search would never see);
* it never fetches a `url:` it read out of front matter, and it never validates one either. Front matter
  is *text in a file an operator pointed the builder at*, not an instruction and not a source;
* it never runs a YAML engine. See `front_matter` for what that buys and what it costs;
* it never treats the presence of metadata as proof of identity. A note's title is the note's own claim,
  and the catalogue records that honestly as `undetermined` rather than dressing it up as `verified`.

Front matter is read for *reuse*, not for authority: a wiki that already carries `title`, `date`, `url`
and topic keys has that work preserved in the listing, but nothing about the listing is believed because
it was written in the note.
"""
import json
import re
import time
from pathlib import Path

import capacity
import catalog
import evidence

# --------------------------------------------------------------------------------------------------
# Front matter
# --------------------------------------------------------------------------------------------------

FRONT_MATTER_DELIMITER = "---"
#: Bounds applied *before* anything is interpreted, so a pathological note cannot make the builder
#: allocate without limit. Each one is a refusal to read further, never a partial parse presented as
#: complete: an over-long block yields no mapping at all, and the note then falls back to its file name.
FRONT_MATTER_MAX_BYTES = 64 * 1024
FRONT_MATTER_MAX_KEYS = 200
FRONT_MATTER_MAX_ITEMS = 50
FRONT_MATTER_MAX_VALUE_CHARS = 2000
FRONT_MATTER_MAX_TOTAL_CHARS = 3000

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
#: The three characters that begin a node this reader refuses to interpret: an anchor, an alias, a tag,
#: a block scalar, a directive, or a flow mapping. They are skipped, not resolved.
_NOT_A_SCALAR = "&*!|>%@`{"
_PLACEHOLDER = "\ufffd"

#: Where a card's `identity` axis value comes from, stated once. `undetermined` is not a placeholder: it
#: is the correct answer to "does anything other than this note agree what this note is?".
IDENTITY = "undetermined"
IDENTITY_FROM_FRONT_MATTER = ("identity is the note's own front-matter title; no external record "
                              "corroborates it")
IDENTITY_FROM_FILE_NAME = "no front-matter title; the file name is the only title"


def is_private_use(character):
    """Private-use code points, the same test `scripts/extract-literature.py` applies to extracted text."""
    code = ord(character)
    return (0xE000 <= code <= 0xF8FF) or (0xF0000 <= code <= 0xFFFFD) or (0x100000 <= code <= 0x10FFFD)


def unquote(text):
    """A quoted scalar without its quotes. No escape processing: the value is kept as it was written."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def strip_comment(text):
    """Drop a trailing ` # comment`. Only when the `#` follows whitespace, so `.../x#section` survives."""
    for index, character in enumerate(text):
        if character == "#" and index > 0 and text[index - 1] in " \t":
            return text[:index].rstrip()
    return text


def split_items(text):
    """Split a flow sequence on commas that are not inside quotes."""
    items, current, quote = [], [], None
    for character in text:
        if quote:
            current.append(character)
            if character == quote:
                quote = None
            continue
        if character in ("'", '"'):
            quote = character
            current.append(character)
            continue
        if character == ",":
            items.append("".join(current))
            current = []
            continue
        current.append(character)
    items.append("".join(current))
    return items


def scalar(raw, budget):
    """The value one raw front-matter value denotes, or None when this reader will not interpret it.

    `budget` is how many characters the block may still spend. A value that would exceed it is not
    silently shortened into a different value: the caller stops reading the block and records that it
    did (`frontMatterTruncated`), so a reader can tell "the note said this" from "the note said more".
    """
    text = raw.strip()
    if not text or budget <= 0:
        return None
    if text[0] in _NOT_A_SCALAR:
        # An anchor, alias, tag, block scalar, directive or flow mapping. Skipped, never resolved: these
        # are the shapes that could reference another node, and nothing this catalogue needs is inside one.
        return None
    if text[0] == "[":
        inner = text[1:-1] if text.endswith("]") else text[1:]
        values = []
        for part in split_items(inner):
            item = strip_comment(unquote(part.strip()))
            if item:
                values.append(item[:FRONT_MATTER_MAX_VALUE_CHARS])
            if len(values) >= FRONT_MATTER_MAX_ITEMS:
                break
        return values or None
    return strip_comment(unquote(text))[:min(FRONT_MATTER_MAX_VALUE_CHARS, budget)]


def parse_block(block):
    """`(mapping, truncated)` for the lines between the delimiters.

    Top-level `key: value` lines only. An indented line, a `- ` item and every indirection syntax are
    skipped rather than guessed at, because a half-understood nested structure is worse than an absent
    one: it would put a value under a key that does not mean what the reader assumed.

    An empty value introduces a nested block, so `key:` with nothing after it stores nothing and says so
    by its absence. "The field was absent" and "the field was empty" stay distinguishable.
    """
    mapping, spent = {}, 0
    for line in block.split("\n"):
        if len(mapping) >= FRONT_MATTER_MAX_KEYS or spent >= FRONT_MATTER_MAX_TOTAL_CHARS:
            return mapping, True
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t", "-"):
            continue
        key, separator, raw = line.partition(":")
        key = key.strip()
        if not separator or not _KEY.match(key):
            continue
        value = scalar(raw, FRONT_MATTER_MAX_TOTAL_CHARS - spent)
        if value is None:
            continue
        mapping[key] = value
        spent += sum(len(item) for item in value) if isinstance(value, list) else len(value)
    return mapping, spent >= FRONT_MATTER_MAX_TOTAL_CHARS


def front_matter(text):
    """`(mapping, note, truncated)` - the leading front-matter block of `text`, read without a YAML engine.

    Only the shape this catalogue needs is understood: a leading `---` line, then `key: value` lines, then
    a closing `---`. This is deliberately not `yaml.safe_load`, although that is safe from code execution:

    * a nested mapping, an anchored or aliased node, a tagged node and a block scalar are all *skipped*.
      There is no node type here that references another, so an alias bomb has nothing to expand;
    * nothing in the block is evaluated. A value is a string or a list of strings, and
      `!!python/object/apply:os.system` is just an unparsed scalar under an unknown key;
    * no URL is fetched, resolved or validated. `url:` is text, stored as text;
    * the block is bounded in bytes, in key count, in value length and in total characters before it is
      interpreted, so a crafted note cannot make the builder allocate without limit;
    * a key that is missing stays missing. Nothing is invented, defaulted or carried over from another
      note, because "the field was absent" and "the field said the default" are different facts and the
      listing must not conflate them.

    `note` is None when a block was read, otherwise a short reason ("no front matter", "unterminated
    front matter", "front matter too large"). A note without front matter is a perfectly good document:
    its title is then its file name, and the record says so.
    """
    if not isinstance(text, str) or not text.startswith(FRONT_MATTER_DELIMITER):
        return {}, "no front matter", False
    opening = text.find("\n")
    if opening < 0 or text[len(FRONT_MATTER_DELIMITER):opening].strip():
        return {}, "no front matter", False
    limit = opening + 1 + FRONT_MATTER_MAX_BYTES
    position, lines = opening + 1, []
    while position <= len(text):
        end = text.find("\n", position)
        line_end = len(text) if end < 0 else end
        line = text[position:line_end].rstrip("\r")
        if line.strip() in (FRONT_MATTER_DELIMITER, "..."):
            mapping, truncated = parse_block("\n".join(lines))
            return mapping, None, truncated
        if line_end >= limit:
            return {}, "front matter too large", False
        lines.append(line)
        if end < 0:
            break
        position = end + 1
    return {}, "unterminated front matter", False


# --------------------------------------------------------------------------------------------------
# Catalogue records
# --------------------------------------------------------------------------------------------------


def parse_quality(text):
    """The parse-quality block the source card renders. Honest counts over the text that was read.

    `reviewPages` is empty and `searchableCjkRuns` is null for the same reason on every Markdown note:
    there are no pages and no extraction pass. Reporting them as `0` or `[]` where the concept does not
    apply would be a measurement the builder never made.
    """
    return {"privateUseChars": sum(1 for character in text if is_private_use(character)),
            "replacementChars": text.count(_PLACEHOLDER),
            "reviewPages": [], "searchableCjkRuns": None}


def year_of(value):
    """The four-digit year a `date` starts with, or None. Never a parsed date object: nothing here needs one."""
    match = re.match(r"\s*(\d{4})", value if isinstance(value, str) else "")
    return match.group(1) if match else None


def document_id(relative_path):
    """The stable id of one note, ordered by path so a listing reads in directory order.

    `catalog.ordered_entries` sorts by document id, so making the id the path makes enumeration follow
    the corpus layout - the 577 posts of a wiki export stay together and paging a 600-document library
    is legible. It is not a claim about the content: the id is an address, and `sourceVersion` is what
    says which bytes it addressed.
    """
    return "md-" + relative_path


def note_record(relative_path, data, front, truncated=False, note=None):
    """One catalogue record for one Markdown note, built from bytes that were already read.

    `data` is the file's bytes; `front` is the mapping `front_matter` returned, and `note` is why there
    is none when there is none. Every field this record carries is either measured here (the SHA256, the
    size, the parse-quality counts) or read out of the note itself (the title, the date, the topic keys) -
    and the two are never mixed up. In particular the `identity` axis is `undetermined` even when a title
    is present, because a title a document asserts about itself is not corroboration;
    `verificationDetail` says exactly that.
    """
    text = data.decode("utf-8-sig")
    title = front.get("title")
    if not isinstance(title, str) or not title.strip():
        title = Path(relative_path).stem
        detail = IDENTITY_FROM_FILE_NAME
    else:
        title = title.strip()
        detail = IDENTITY_FROM_FRONT_MATTER
    record = {
        "documentId": document_id(relative_path),
        "textFile": relative_path,
        "fileName": Path(relative_path).name,
        "titleRaw": title,
        "titleNormalized": catalog.squash(title),
        "authors": front.get("author") if isinstance(front.get("author"), str) else None,
        "year": year_of(front.get("date")),
        "doi": None,
        "textSHA256": evidence.digest(data),
        "textBytes": len(data),
        "chars": len(text),
        "verification": IDENTITY,
        "verificationDetail": detail,
        "quality": parse_quality(text),
        "conflicts": [],
        # Everything the note said about itself, kept as it was written and namespaced so it can never
        # be mistaken for something this builder established. Nothing reads it as evidence.
        "frontMatter": front,
    }
    if note:
        # Why the mapping is empty, so a reader can tell "the note declared nothing" from "the block was
        # refused" without re-reading the file.
        record["frontMatterNote"] = note
    if truncated:
        record["frontMatterTruncated"] = True
    # `pages` is deliberately absent. `source_card.is_pdf_record` treats a positive `pages` (or a `.pdf`
    # file name) as "there is a PDF behind this", and a Markdown note has no page structure: setting it
    # would make the card offer a page number for a document that has none.
    return record


# --------------------------------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------------------------------


def scan(root, library_id, max_file_bytes=None, extensions=None):
    """Read the whole corpus once and return everything the catalogue and the manifest need.

    One pass, one read of each file: the SHA256, the byte count, the text and the front matter all come
    from the same byte string, so the record and the manifest cannot describe two different versions of
    a file. That is the same rule `reader.read` follows, and for the same reason.

    `max_file_bytes` defaults to the library's own ceiling, resolved through `capacity.resolve` on the
    entry-shaped argument; passing an int uses it directly. Over-ceiling files are **reported**, not
    skipped: this function returns violations for the caller to refuse on, because a builder that
    quietly omitted an oversized note would publish a library whose listing looks complete and is not.

    Returns a dict:
      records    {key: record}      decodable notes, for the catalogue
      manifest   {relative: sha256}  every selected text file, for `realDataTrial.files`
      skipped    [{path, reason}]   files that produced no record, and why
      violations [{dimension, path, size, limit}]
      changed    [relative]         files whose size or mtime moved while they were being read
      totalBytes int
      elapsedMs  int
    """
    started = time.monotonic()
    limit = capacity.resolve({"id": library_id, "maxFileBytes": max_file_bytes})
    root_path = Path(root)
    files, warnings = evidence.selected_files(root_path, extensions or evidence.EXTENSIONS)
    records, manifest, skipped, violations = {}, {}, [], []
    observed, total = {}, 0
    for entry in files:
        relative, source = entry["relative"], entry["source"]
        try:
            stat = source.stat()
            data = source.read_bytes()
        except OSError:
            skipped.append({"path": relative, "reason": "unreadable"})
            continue
        manifest[relative] = evidence.digest(data)
        total += len(data)
        observed[relative] = (stat.st_size, stat.st_mtime_ns)
        if len(data) > limit:
            violations.append({"dimension": "fileBytes", "path": relative, "size": len(data), "limit": limit})
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            skipped.append({"path": relative, "reason": "not UTF-8 text"})
            continue
        front, note, truncated = front_matter(text)
        records[document_id(relative)] = note_record(relative, data, front, truncated, note)
    if len(files) > capacity.MAX_FILES:
        violations.append({"dimension": "files", "size": len(files), "limit": capacity.MAX_FILES})
    if total > capacity.MAX_TOTAL_BYTES:
        violations.append({"dimension": "totalBytes", "size": total, "limit": capacity.MAX_TOTAL_BYTES})
    changed = []
    for relative, (size, mtime) in observed.items():
        try:
            stat = (root_path / relative).stat()
        except OSError:
            changed.append(relative)
            continue
        if (stat.st_size, stat.st_mtime_ns) != (size, mtime):
            changed.append(relative)
    return {"libraryId": str(library_id), "root": str(root_path), "records": records,
            "manifest": manifest, "skipped": skipped, "warnings": warnings, "violations": violations,
            "changed": sorted(changed), "totalBytes": total, "files": len(files),
            "elapsedMs": int((time.monotonic() - started) * 1000)}


def catalogue_payload(scanned, library_id, root):
    """The catalogue document for a scan: the binding, then the records."""
    payload = catalog.binding_fields(library_id, root)
    payload["generator"] = "scripts/build-markdown-library.py"
    payload["files"] = scanned["files"]
    payload["totalBytes"] = scanned["totalBytes"]
    payload["documents"] = scanned["records"]
    return payload


def diff(current, scanned):
    """`(added, removed, changed)` between the manifest in a config and a fresh scan.

    Compared by content, not by presence: a note that was edited keeps its name and still has to be
    reported, because its recorded `textSHA256` and its entry in the authorized set both move.
    """
    before = current or {}
    after = scanned["manifest"]
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    return added, removed, changed


def manifest_bytes(manifest):
    """The size the manifest adds to `libraries.json`, so the operator sees what the gate costs."""
    return len(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def hash_cost(scanned):
    """Bytes hashed per call by `real_data.authorize`, which re-hashes the whole set on every request."""
    return scanned["totalBytes"]
