"""Local document location over a versioned corpus catalogue (M0).

Search answers "what text matches this question"; this answers "which document in the library is this,
and how do I read it" - from a title, author, DOI or file-name clue, without Sirchmunk, without a model
client and without waiting for a retrieval hit.

Nothing in a catalogue is authority. A catalogue entry is a *hint* that must be re-checked against the
library before any identifier is minted:

* the calling workspace must still be configured with that library, and a real-data library must still
  pass its authorization check (the service does both, as for search and read);
* the catalogue must be **attributed to this library** - it records the `libraryId` and the normalised
  root identity it was built for, and a catalogue built for another library or another root is refused
  rather than consulted (`check_binding`);
* the recorded text file must resolve inside the library root (same link, extension and size rules as a
  read, including the library's own `maxFileBytes`) and its SHA256 must match both the catalogue and, for
  a real library, the authorized file set.

A mismatch refuses the entry instead of minting a handle, so a forged, stale or out-of-library
catalogue cannot widen access.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata

import capacity
import evidence
import reader

CATALOG_ENV = "DSH_KNOWLEDGE_CATALOG"
MAX_CANDIDATES = 10
MAX_QUERY_CHARS = 300
MAX_CATALOG_BYTES = 4_000_000
#: One page of the enumeration. Larger than a find because enumeration is a listing, not a ranking: a caller
#: that must page a 19-document library ten at a time spends the turn budget on bookkeeping.
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 20

#: The two fields that attribute a catalogue to exactly one library. `libraryId` says which library, and
#: `rootIdentity` (see `capacity.root_identity`) says which directory that library was at when the
#: catalogue was built. A catalogue file can be copied, and a config line can point at the wrong one; the
#: binding is what turns that from "library B is handed library A's titles" into a refusal.
BINDING_LIBRARY = "libraryId"
BINDING_ROOT = "rootIdentity"

CATALOG_UNAVAILABLE = "CATALOG_UNAVAILABLE"
CATALOG_UNREADABLE = "CATALOG_UNREADABLE"
CATALOGUE_NO_LIBRARY = "CATALOGUE_NO_LIBRARY"
CATALOGUE_LIBRARY_MISMATCH = "CATALOGUE_LIBRARY_MISMATCH"
FIND_REQUEST_INVALID = "FIND_REQUEST_INVALID"
CATALOGUE_REQUEST_INVALID = "CATALOGUE_REQUEST_INVALID"
CATALOGUE_CHANGED = "CATALOGUE_CHANGED"
DOCUMENT_NOT_READABLE = "DOCUMENT_NOT_READABLE"

DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_cache = {"fingerprint": None, "payload": None, "version": None, "binding": (None, None)}


def fingerprint(path, stat):
    """What a parsed catalogue is cached against.

    Not the modification time alone. A file that is rewritten inside one mtime tick - which is exactly
    what a refresh does when it publishes a catalogue - would otherwise be served from the cache as the
    previous listing, and a caller paging through it would be describing the file that is no longer
    there. Size and file index are added because they change when the bytes do: a replacement via
    `os.replace` gets a new file index, and any content change of a different length moves the size.
    """
    return (str(path), stat.st_mtime, stat.st_size, stat.st_ino)


class CatalogError(Exception):
    """A refusal with a code the caller can act on."""

    def __init__(self, code, status=400, detail=""):
        self.code, self.status, self.detail = code, status, detail
        super().__init__(code + (": " + detail if detail else ""))


def normalise(value):
    """Case- and whitespace-insensitive key for matching (never used to build a quotation)."""
    return re.sub(r"[\s\u3000]+", " ", unicodedata.normalize("NFKC", value or "")).strip().casefold()


def squash(value):
    """Space- and punctuation-insensitive key, so a title whose spaces were lost still matches."""
    text = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"[\s\u3000]+", "", re.sub(r"[^\w\u3400-\u9fff]+", " ", text)).casefold()


def normalise_doi(value):
    text = DOI_PREFIX.sub("", (value or "").strip())
    return text.rstrip(".,;)]}>").casefold()


def binding_fields(library_id, root):
    """The attribution fields a catalogue must carry, for whoever writes one."""
    return {BINDING_LIBRARY: str(library_id), BINDING_ROOT: capacity.root_identity(root)}


def check_binding(binding, library_id, root, require_attribution=True):
    """Refuse a catalogue that belongs to another library, or to none at all where that is not allowed.

    Two cases, and the difference matters:

    * **unattributed** - the file declares neither field. Accepted only when
      `require_attribution=False`, which the service passes for a *single-library* configuration: with
      one library there is nothing a catalogue could be confused with, so the older
      `DSH_KNOWLEDGE_CATALOG`-points-at-a-file arrangement keeps working unchanged. In a multi-library
      configuration the same call is refused, because "the process-wide catalogue" would otherwise
      answer for whichever library asked first, and the failure would be a listing of library A's
      titles and file names under library B - the one outcome worse than a refusal.
    * **attributed** - both fields must be present, name this library, and name this library's root.
      A half-written binding is a malformed catalogue, not a legacy one, and is refused as such.

    There is no fallback path in either direction: a declared but unusable catalogue is never quietly
    replaced by another file, because that would answer the question with the wrong library's index.
    """
    library, identity = binding
    if library is None and identity is None:
        if require_attribution:
            raise CatalogError(CATALOGUE_NO_LIBRARY, 503,
                               "this library's catalogue records no %s/%s; rebuild it for this library"
                               % (BINDING_LIBRARY, BINDING_ROOT))
        return
    if not isinstance(library, str) or not library or not isinstance(identity, str) or not identity:
        raise CatalogError(CATALOG_UNREADABLE, 503, "the catalogue's library binding is incomplete")
    if library != str(library_id):
        raise CatalogError(CATALOGUE_LIBRARY_MISMATCH, 503,
                           "the catalogue belongs to library %r, not %r" % (library, str(library_id)))
    if identity != capacity.root_identity(root):
        raise CatalogError(CATALOGUE_LIBRARY_MISMATCH, 503,
                           "the catalogue was built for a different library root; rebuild it for this library")


def _parse(path):
    """`(documents, version, binding)` for one catalogue file, cached by path+mtime.

    A malformed catalogue is a refusal, not a guess. The version is returned together with the payload on
    purpose: computing it from a second read of the same path could describe a different file than the one the
    page was built from, which is precisely the silent skip paging has to avoid. The binding is returned
    **raw** - what the file literally declared - so `check_binding` can tell "declares nothing" (legacy)
    from "declares something incomplete" (malformed).
    """
    target = Path(path)
    if not target.is_file():
        raise CatalogError(CATALOG_UNAVAILABLE, 503, "no catalogue is configured at that location")
    try:
        stat = target.stat()
    except OSError:
        raise CatalogError(CATALOG_UNAVAILABLE, 503, "the catalogue is unreadable") from None
    if _cache["fingerprint"] == fingerprint(target, stat):
        return _cache["payload"], _cache["version"], _cache["binding"]
    if stat.st_size > MAX_CATALOG_BYTES:
        raise CatalogError(CATALOG_UNREADABLE, 503, "the catalogue is larger than this service accepts")
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise CatalogError(CATALOG_UNREADABLE, 503, "the catalogue is not valid JSON") from None
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(documents, dict):
        raise CatalogError(CATALOG_UNREADABLE, 503, "the catalogue carries no documents mapping")
    clean = {}
    for key, record in documents.items():
        if not isinstance(record, dict) or not isinstance(record.get("textFile"), str):
            continue
        clean[str(key)] = record
    version = catalogue_version(clean)
    binding = ((payload or {}).get(BINDING_LIBRARY), (payload or {}).get(BINDING_ROOT))
    _cache.update({"fingerprint": fingerprint(target, stat), "payload": clean, "version": version,
                   "binding": binding})
    return clean, version, binding


def load_versioned(path):
    """The catalogue payload **and the version of the listing it represents**, cached by path+mtime."""
    documents, version, _ = _parse(path)
    return documents, version


def load(path):
    """The catalogue payload alone, for callers that do not page it."""
    return load_versioned(path)[0]


def load_bound(path, library_id, root, require_attribution=True):
    """`(documents, version)` for the library the caller asked about, or a refusal. No fallback.

    The one entry point a *service* path should use: it is `load_versioned` plus the binding check, so
    a catalogue that belongs to another library is refused before a single record is read out of it.
    """
    documents, version, binding = _parse(path)
    check_binding(binding, library_id, root, require_attribution)
    return documents, version


def catalogue_version(payload):
    """A digest of what the catalogue *says*, not of the file's bytes.

    Paging needs an answer to one question: "is the thing I am part-way through listing still the same
    listing?". Hashing the file would move on a reformat that changed nothing and stay put on a swap that
    changed everything a reader cares about. Hashing the canonical projection of every entry's identity and
    recorded text hash moves exactly when an entry appears, disappears, or comes to point at different bytes.

    Sorted keys are what makes it deterministic: the mapping's insertion order is not a property of the
    library, so it must not be able to change the version on its own.
    """
    projection = [
        {"id": str(record.get("documentId") or key),
         "textFile": str(record.get("textFile") or ""),
         "textSHA256": str(record.get("textSHA256") or ""),
         "title": str(record.get("titleRaw") or ""),
         "pages": record.get("pages") if isinstance(record.get("pages"), int) else None}
        for key, record in sorted(payload.items(), key=lambda item: str(item[0]))
    ]
    body = json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def field(record, name):
    value = record.get(name)
    return value if isinstance(value, str) else ""


def score_record(record, query):
    """(score, matched aspects, mismatches) for one catalogue record against one query."""
    score, matched, mismatched = 0.0, [], []
    if query.get("doi"):
        wanted = normalise_doi(query["doi"])
        if wanted and wanted == normalise_doi(field(record, "doi")):
            score += 1.0
            matched.append("doi-exact")
    if query.get("title"):
        wanted = squash(query["title"])
        candidate = squash(field(record, "titleNormalized") or field(record, "titleRaw"))
        if wanted and candidate:
            if wanted == candidate:
                score += 1.0
                matched.append("title-exact")
            else:
                tokens = {token for token in re.split(r"\W+", normalise(query["title"])) if len(token) > 2}
                other = {token for token in re.split(r"\W+", normalise(field(record, "titleRaw"))) if len(token) > 2}
                overlap = len(tokens & other) / max(len(tokens), 1)
                if overlap >= 0.6:
                    score += 0.5 + 0.4 * overlap
                    matched.append("title-partial")
                elif overlap >= 0.5:
                    # A partially remembered title: offered, but with a small score so a better clue wins.
                    score += 0.3 * overlap
                    matched.append("title-weak")
    if query.get("fileName"):
        wanted = normalise(Path(query["fileName"]).name)
        candidate = normalise(field(record, "fileName"))
        if wanted and candidate:
            if wanted == candidate:
                score += 0.9
                matched.append("filename-exact")
            elif wanted in candidate:
                score += 0.5
                matched.append("filename-partial")
    if query.get("authors"):
        wanted = [token for token in re.split(r"\W+", normalise(query["authors"])) if len(token) > 2]
        authors = normalise(field(record, "authors"))
        hits = [token for token in wanted if token in authors] if authors else []
        if hits:
            score += 0.3 * min(1.0, len(hits) / max(len(wanted), 1))
            matched.append("authors-match")
    if query.get("year"):
        wanted = re.sub(r"\D", "", query["year"])
        candidate = re.sub(r"\D", "", field(record, "year"))
        if wanted:
            if candidate and wanted == candidate:
                score += 0.2
                matched.append("year-match")
            elif candidate:
                score -= 0.2
                mismatched.append("year-mismatch")
    return round(score, 4), matched, mismatched


def find(catalog, query, limit=None):
    """Ranked candidates; zero hits stay zero hits instead of falling back to "the first document"."""
    if not any(query.get(field_name) for field_name in ("doi", "title", "fileName", "authors", "year")):
        raise CatalogError(FIND_REQUEST_INVALID, 400,
                           "give at least one of doi, title, fileName, authors or year")
    if limit is None and isinstance(query.get("limit"), int):
        limit = query["limit"]
    for name, value in query.items():
        if isinstance(value, str) and len(value) > MAX_QUERY_CHARS:
            raise CatalogError(FIND_REQUEST_INVALID, 400, "%s is longer than %d characters" % (name, MAX_QUERY_CHARS))
    ranked = []
    for key, record in catalog.items():
        score, matched, mismatched = score_record(record, query)
        if score <= 0:
            continue
        ranked.append({"documentId": record.get("documentId") or key, "score": score,
                       "matched": matched, "mismatched": mismatched, "record": record})
    ranked.sort(key=lambda item: (-item["score"], item["documentId"]))
    return ranked[:max(1, min(limit or MAX_CANDIDATES, MAX_CANDIDATES))]


def ordered_entries(catalog):
    """Every entry, in the one order the enumeration is defined over.

    Ordered by document id and then by catalogue key, so the order is a property of the library rather than of
    a dict's insertion order or of any query. That is what lets `offset` mean the same thing on two calls - and
    it is only half the guarantee: the other half is a caller sending back the version it was given, so a
    catalogue that changed in between is *reported* rather than silently skipped.
    """
    entries = [{"documentId": str(record.get("documentId") or key), "key": key, "record": record}
               for key, record in catalog.items()]
    entries.sort(key=lambda item: (item["documentId"], item["key"]))
    return entries


def enumerate_catalog(catalog, offset=0, limit=None):
    """One deterministic page of the whole catalogue, independent of any clue or keyword.

    Deliberately not `find` with an empty query: a keyword search answers "what matches", and no keyword makes
    it answer "what exists". This answers the second question, and says so in its own counts.
    """
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CatalogError(CATALOGUE_REQUEST_INVALID, 400, "offset must be an integer >= 0")
    size = DEFAULT_PAGE_SIZE if limit is None else limit
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise CatalogError(CATALOGUE_REQUEST_INVALID, 400, "limit must be an integer >= 1")
    size = min(size, MAX_PAGE_SIZE)
    ordered = ordered_entries(catalog)
    total = len(ordered)
    page = ordered[offset:offset + size]
    end = offset + len(page)
    return {"total": total, "offset": offset, "limit": size, "returned": len(page),
            "hasMore": end < total, "nextOffset": end if end < total else None, "entries": page}


def verify_text_file(selected, record, issuer, catalog=None, audit=None, max_file_bytes=None):
    """Resolve and verify the file a catalogue entry points at, then mint the read handle.

    Returns `(window_fields, refusal)`: exactly one of them is set. Nothing here trusts the catalogue:
    the path must resolve inside the library under the read rules - including the library's own
    `maxFileBytes`, so a catalogue cannot offer a document the reader would refuse, or hide one the
    library was configured to accept - the bytes must hash to the recorded text version, and a
    real-data library must still list the file in its authorized set.

    A catalogue entry is a *location*, never an authorization: this runs again for every entry of every
    page, and the handle it mints is re-checked all over again when it is actually used to read.
    """
    limit = capacity.resolve(selected) if max_file_bytes is None else max_file_bytes
    relative = field(record, "textFile")
    try:
        path = reader.resolve(selected["root"], relative, limit)
    except reader.ReadError as error:
        return None, {"code": error.code, "detail": error.detail}
    try:
        data = path.read_bytes()
    except OSError:
        return None, {"code": reader.READ_FILE_UNAVAILABLE, "detail": "the mapped text file is unreadable"}
    version = evidence.digest(data)
    recorded = field(record, "textSHA256")
    if recorded and version != recorded:
        return None, {"code": "DOCUMENT_CHANGED",
                      "detail": "the text file no longer matches the catalogue; rebuild the catalogue"}
    if not selected.get("syntheticFixture", False):
        authorized = ((selected.get("realDataTrial") or {}).get("files") or {})
        name = Path(relative).as_posix()
        if name not in authorized:
            return None, {"code": DOCUMENT_NOT_READABLE,
                          "detail": "the mapped file is not in this library's authorized set"}
        if authorized[name].lower() != version:
            return None, {"code": "DOCUMENT_CHANGED",
                          "detail": "the authorized hash differs from the file on disk"}
    handle = issuer.issue(relative, version)
    return {"fileHandle": handle, "sourceVersion": version, "relativePath": relative}, None
