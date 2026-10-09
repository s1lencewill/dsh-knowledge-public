"""M0 text snapshot and exact citation verification; no model-generated line numbers."""
import hashlib
import os
from pathlib import Path

import capacity

EXTENSIONS = {".md", ".txt", ".py", ".ts", ".tsx", ".js", ".json", ".yaml", ".yml"}
EXCLUDED = {".git", "node_modules", ".venv", ".local", "__pycache__"}


class SnapshotCapacityError(Exception):
    """Fail closed: no partial manifest may be searched after exceeding a limit."""
    def __init__(self, dimension, limit):
        self.dimension, self.limit = dimension, limit
        super().__init__(f"LIBRARY_CAPACITY_EXCEEDED: {dimension} limit={limit}; query rejected")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def excluded(name):
    """Whether a directory or file name is outside a library. Hidden names are never library content."""
    return name.startswith(".") or name in EXCLUDED


def linked(path):
    """A symlink or Windows junction. Neither is ever followed, for files or for directories."""
    return path.is_symlink() or path.is_junction()


def walk_text_files(root, extensions=EXTENSIONS):
    """The **one** file-selection rule of this service, as a generator.

    Yields `{"relative", "source", "resolved"}` for each candidate file, and `{"warning": text}` for
    anything the rule excludes or cannot read. The rule itself: hidden names and the `EXCLUDED`
    directories are pruned, a symlink or junction is never followed (directories are pruned before
    descent, which is also what stops a junction loop), only the whitelisted text extensions are
    candidates, and the file must still resolve inside `root`.

    Size, count and total-byte limits are deliberately **not** applied here. They differ per caller:
    `snapshot` applies the library's `maxFileBytes` and fails closed on it, while the builder wants to
    report every oversized file at once instead of only the first. Selection is the part that has to be
    identical everywhere, so it is the part that lives in one place.
    """
    root = Path(root).resolve(strict=True)
    problems = []
    for directory, directories, filenames in os.walk(
            root, topdown=True, followlinks=False,
            onerror=lambda _: problems.append("Unreadable directory skipped")):
        parent = Path(directory)
        retained = []
        for name in directories:
            path = parent / name
            if excluded(name):
                continue
            if linked(path):
                problems.append("Skipped linked directory: " + str(path.relative_to(root)))
                continue
            retained.append(name)
        directories[:] = retained  # Prune before descent, including Windows junction loops.
        while problems:
            yield {"warning": problems.pop(0)}
        for name in filenames:
            source = parent / name
            relative = source.relative_to(root)
            if excluded(name):
                continue
            if linked(source):
                yield {"warning": "Skipped linked file: " + str(relative)}
                continue
            if source.suffix.lower() not in extensions:
                continue
            try:
                resolved = source.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    yield {"warning": "Skipped outside file: " + str(relative)}
                    continue
            except (OSError, ValueError, RuntimeError):
                yield {"warning": "Unreadable or changed file: " + str(relative)}
                continue
            yield {"relative": relative.as_posix(), "source": source, "resolved": resolved}
    while problems:
        yield {"warning": problems.pop(0)}


def selected_files(root, extensions=EXTENSIONS):
    """`([entry, ...], warnings)` - exactly the files a snapshot of `root` would copy.

    The same walk `snapshot` uses, read to its end. `real_data.actual_files` and the library builder
    both consume this, so "which files is this library made of" cannot be answered three ways.
    """
    files, warnings = [], []
    for entry in walk_text_files(root, extensions):
        if "warning" in entry:
            warnings.append(entry["warning"])
        else:
            files.append(entry)
    return files, warnings


def byte_limit(max_file_bytes):
    """Validate a per-file ceiling a caller passed in, so the number is the same one everywhere."""
    if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 1:
        raise ValueError("max_file_bytes must be a whole number of bytes, at least 1")
    return max_file_bytes


def snapshot(root, destination, max_file_bytes=capacity.DEFAULT_MAX_FILE_BYTES):
    """Copy the library into `destination` and return `(manifest, warnings)`.

    `max_file_bytes` is the library's OWN ceiling, already resolved by `capacity.resolve`. It is a
    parameter and not a constant because the ceiling is a property of the corpus: a notes wiki with one
    2,815,653-byte article is a complete, searchable library at 4,000,000 bytes and an unusable one at
    2,000,000. What does not change is that the limit is enforced exactly, and that exceeding it raises
    instead of quietly dropping the file - a partially indexed library reported as a search result is
    the failure this refusal exists to prevent.
    """
    limit = byte_limit(max_file_bytes)
    root = Path(root).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination.is_relative_to(root):
        raise ValueError("Snapshot must be outside the library root")
    manifest, warnings = {}, []
    total = 0
    for entry in walk_text_files(root):
        if "warning" in entry:
            warnings.append(entry["warning"])
            continue
        source, relative = entry["source"], entry["relative"]
        try:
            if len(manifest) >= capacity.MAX_FILES:
                raise SnapshotCapacityError("files", capacity.MAX_FILES)
            if source.stat().st_size > limit:
                raise SnapshotCapacityError("fileBytes", limit)
            with source.open("rb") as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise SnapshotCapacityError("fileBytes", limit)
        except (OSError, ValueError, RuntimeError):
            warnings.append("Unreadable or changed file: " + relative)
            continue
        total += len(data)
        if total > capacity.MAX_TOTAL_BYTES:
            raise SnapshotCapacityError("totalBytes", capacity.MAX_TOTAL_BYTES)
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            warnings.append("Non-UTF8 file: " + relative)
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest[str(target.resolve())] = {"source": str(entry["resolved"]), "relativePath": relative,
                                           "version": digest(data)}
    return manifest, warnings


def normalised_text(data):
    """The text an excerpt is located in: UTF-8 (BOM allowed) with CRLF folded to LF.

    Every offset reported by `verify` is a **Unicode code point** index into exactly this string - a
    Python `str` index. It is deliberately *not* a UTF-16 code unit index, which is what JavaScript's
    `String.prototype.slice` counts: for text containing characters outside the BMP (emoji, rarer CJK
    ideographs) the two numbering schemes differ by one unit per such character, so a Node consumer must
    convert before slicing. `scripts/excerpt-offsets.mjs` does that conversion.
    """
    return data.decode("utf-8-sig").replace("\r\n", "\n")


def excerpt_from_offsets(text, item):
    """Rebuild an excerpt from the returned fields alone - the contract `verify` promises.

    `text` is code point indexed (Python str). A JavaScript consumer must convert the offsets first; see
    the module docstring above.
    """
    start, end = item["startChar"], item["endChar"]
    if text[start:end] != item["excerpt"]:
        raise ValueError("offsets do not reproduce the excerpt")
    return text[start:end]


def verify(manifest, file_path, excerpt, library_id):
    entry = manifest.get(str(Path(file_path).resolve()))
    if not entry or not isinstance(excerpt, str) or not excerpt.strip():
        return None
    try:
        data = Path(entry["source"]).read_bytes()
    except OSError:
        return None
    if digest(data) != entry["version"]:
        return None
    text = normalised_text(data)
    excerpt = excerpt.replace("\r\n", "\n").strip()
    offset = text.find(excerpt)
    if offset < 0:
        return None
    start = text[:offset].count("\n") + 1
    end = offset + len(excerpt)
    # Line numbers alone are not enough: a quote can start mid-line and, more often, stop before the end
    # of its last line, so the line range alone cannot rebuild it. The offsets below are the contract.
    first_line_start = text.rfind("\n", 0, offset) + 1
    last_line_start = text.rfind("\n", 0, end) + 1
    return {"id": digest((library_id + entry["relativePath"] + entry["version"] + str(offset) + excerpt).encode()),
            "libraryId": library_id, "relativePath": entry["relativePath"], "sourceVersion": entry["version"],
            "startLine": start, "endLine": start + excerpt.count("\n"), "excerpt": excerpt,
            "startChar": offset, "endChar": end,
            "startOffset": offset - first_line_start, "endOffset": end - last_line_start}
