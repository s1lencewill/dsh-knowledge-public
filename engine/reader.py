"""On-demand reads of the original file behind a verified citation.

Search finds candidates and the parent verifies excerpts; a verified excerpt can still be the wrong
size for the question - it can stop one line before the unit, split a range across two fragments, or
cut the caption that names the species. This module lets the conversation pull the missing context
out of the original text without leaving the boundaries the search already established:

* The client never sends a path. It sends a **handle** the parent minted while producing the citation.
  The handle is an HMAC-signed blob carrying a workspace fingerprint, the library, the recorded
  relative path and the file's `sourceVersion`. A handle from another workspace or library, a
  tampered handle and a handle minted under a different service token all fail closed before any file
  is opened, so guessing an identifier grants nothing.
* Handles and cursors need no server-side table: they stay usable after the query that produced them
  has been acknowledged and deleted, and across a service restart, for as long as the service token
  is unchanged. Rotating the token invalidates them and the caller must search again.
* One read is one `read_bytes()`. The SHA256 that is verified, the `sourceVersion` that is reported and
  the code point offsets that index the excerpt all come from that same byte string, so a file that
  changes while the read is in progress cannot produce a quote that matches no version.
* Reads happen in the trusted parent only. They never start the engine, never build a model client and
  never give the container a library path; this module is deliberately absent from
  `staging.ENGINE_FILES`.

Offsets follow the frozen citation contract in `evidence.py`: `startChar`/`endChar` are 0-based,
half-open **Unicode code point** offsets into "UTF-8 decoded, BOM stripped, CRLF folded to LF" text,
with `text[startChar:endChar] == excerpt`. A JavaScript consumer must convert code points to UTF-16
code units first (`scripts/excerpt-offsets.mjs`).
"""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import re

import capacity
import evidence
import source_card

#: Token prefixes. They are also part of the signature, so a cursor cannot be presented as a handle.
HANDLE_PREFIX = "krh1"
CURSOR_PREFIX = "krc1"
TOKEN_CONTEXT = b"dsh-knowledge/read-handle/v1"
PAYLOAD_VERSION = 1

DEFAULT_READ_CHARS = 6000
#: Hard per-response ceiling. The window is the only large field, so this bounds the response too.
MAX_READ_CHARS = 20000
#: Below this a window is not worth returning; the session budget refuses instead of truncating to it.
MIN_READ_CHARS = 200
DEFAULT_READ_LINES = 200
MAX_READ_LINES = 200
MAX_OVERLAP_LINES = 3
#: Per-service-process session budget. Enforced before the bytes are read (count) and on the returned
#: window (characters). It is a blunt local safety bound, not a billing meter.
MAX_READS_PER_SESSION = 40
MAX_SESSION_CHARS = 400_000
MAX_SESSIONS = 200
#: The default per-file ceiling, so this module stays usable on its own. The effective value is the
#: *library's*, resolved once by `capacity.resolve` and passed in: a read may not be a way around the
#: snapshot's limit, and equally it must not be a second, stricter limit that refuses a file the
#: library was configured to accept.
MAX_FILE_BYTES = capacity.DEFAULT_MAX_FILE_BYTES
MAX_TOKEN_CHARS = 4096

READ_HANDLE_INVALID = "READ_HANDLE_INVALID"
READ_HANDLE_FOREIGN = "READ_HANDLE_FOREIGN"
READ_REQUEST_INVALID = "READ_REQUEST_INVALID"
READ_RANGE_INVALID = "READ_RANGE_INVALID"
READ_FILE_UNAVAILABLE = "READ_FILE_UNAVAILABLE"
READ_FILE_NOT_TEXT = "READ_FILE_NOT_TEXT"
SOURCE_VERSION_CHANGED = "SOURCE_VERSION_CHANGED"
READ_SOURCE_VERSION_MISMATCH = "READ_SOURCE_VERSION_MISMATCH"
READ_LIMIT_EXCEEDED = "READ_LIMIT_EXCEEDED"

#: The two codes whose refusals are about the **shape of the request** and are therefore recoverable by
#: changing an argument. They are the only ones that carry `facts` below: every other refusal is about
#: identity, authorisation, the file or a budget, and a caller cannot repair it by editing a parameter -
#: telling it "reason: X, try again" would invite a retry that cannot succeed.
RECOVERABLE_CODES = (READ_REQUEST_INVALID, READ_RANGE_INVALID)

#: Fixed reasons for a recoverable refusal. A closed set, so a caller switches on a token rather than
#: parsing prose, and so a reason can never be invented by whoever raises the error.
READ_REASON_START_LINE_INVALID = "START_LINE_INVALID"
READ_REASON_START_LINE_PAST_END = "START_LINE_PAST_END"
READ_REASON_START_OUTSIDE_FILE = "START_OUTSIDE_FILE"
READ_REASON_CURSOR_OUTSIDE_FILE = "CURSOR_OUTSIDE_FILE"
READ_REASON_IDENTIFIER_MISSING = "IDENTIFIER_MISSING"
READ_REASON_IDENTIFIER_CONFLICT = "IDENTIFIER_CONFLICT"
#: Minted by the service's schema guard, not by this module: the request never reached a read. It lives here
#: because this tuple is the one authority both halves compare themselves against.
READ_REASON_PARAMETER_TYPE_INVALID = "PARAMETER_TYPE_INVALID"
READ_REASONS = (
    READ_REASON_START_LINE_INVALID,
    READ_REASON_START_LINE_PAST_END,
    READ_REASON_START_OUTSIDE_FILE,
    READ_REASON_CURSOR_OUTSIDE_FILE,
    READ_REASON_IDENTIFIER_MISSING,
    READ_REASON_IDENTIFIER_CONFLICT,
    READ_REASON_PARAMETER_TYPE_INVALID,
)

#: Fixed next steps. Also a closed set: "retry with a different start line", "locate the file again" and
#: "fix the parameter's type or range" are the only repairs these refusals have, and naming one of them is not
#: the same as guessing a value - `fileLines` gives the bound and the caller chooses inside it.
READ_ACTION_CORRECT_START_LINE = "CORRECT_START_LINE"
READ_ACTION_LOCATE_AGAIN = "LOCATE_AGAIN"
READ_ACTION_CORRECT_PARAMETER = "CORRECT_PARAMETER"
READ_ACTIONS = (READ_ACTION_CORRECT_START_LINE, READ_ACTION_LOCATE_AGAIN, READ_ACTION_CORRECT_PARAMETER)

#: The largest integer this service will report in a refusal's numeric fields. It is JavaScript's
#: `Number.MAX_SAFE_INTEGER`, and it is a bound rather than a convenience: the plugin drops any number that is
#: not a safe integer before it displays one, so a service that echoed a larger value would be answering with a
#: number the card silently discards - the two halves have to agree about what counts as a reportable number.
MAX_REPORTED_LINE = 9007199254740991


def _reportable_line(value):
    """A line number or line count this service is willing to report, or None.

    Only a non-negative integer at or below {@link MAX_REPORTED_LINE} qualifies. Everything else - a string, a
    boolean, a float, a negative, a number too large for the caller's own arithmetic - becomes None rather than
    being repeated: a refusal must not become a way to have an arbitrary request value echoed back.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_REPORTED_LINE:
        return None
    return value


def read_facts(reason=None, requested_start_line=None, file_lines=None, suggested_action=None):
    """The structured, validated facts of a recoverable read refusal.

    Every field is either a member of a closed set, a reportable integer, or None - there is no free-text
    field, so this structure cannot become a way to echo a request value, a path or a handle back to the
    caller. `requestedStartLine` is reported **only** when it is an integer at or above 1: an echoed value that
    was never a valid line number (a string, a boolean, 0, a negative shot past what any caller can hold) is
    dropped rather than repeated, which is what keeps a type error from becoming a reflection of the input.
    """
    line = _reportable_line(requested_start_line)
    if line is not None and line < 1:
        line = None
    return {
        "reason": reason if reason in READ_REASONS else None,
        "requestedStartLine": line,
        "fileLines": _reportable_line(file_lines),
        "suggestedAction": suggested_action if suggested_action in READ_ACTIONS else None,
    }


class ReadError(Exception):
    """A refusal, named by code. The service turns `status` into the HTTP status and `code` into the
    error detail the caller sees, so a refused read is always explainable and never a bare 500.

    `facts` is the recoverable half of the refusal, and it exists only for `RECOVERABLE_CODES`: a
    refusal about identity, authorisation, the file or a budget carries none, so a caller cannot use the
    error channel to learn anything about a file it was not allowed to open.
    """

    def __init__(self, code, status=400, detail="", facts=None):
        self.code, self.status, self.detail = code, status, detail
        self.facts = facts if (code in RECOVERABLE_CODES and isinstance(facts, dict)) else None
        super().__init__(code + (": " + detail if detail else ""))


def _b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def derive_secret(token):
    """The signing key for handles and cursors.

    Derived from the service token instead of stored, so no new secret appears on disk and a restarted
    service (same token) still recognises the handles it already handed out. A rotated token makes all
    outstanding handles invalid, which is the intended failure: search again.
    """
    if not isinstance(token, str) or len(token) < 24:
        raise ValueError("the service token must contain at least 24 characters")
    return hmac.new(TOKEN_CONTEXT, token.encode("utf-8"), hashlib.sha256).digest()


def workspace_fingerprint(workspace):
    """A stable, non-reversible workspace identity. The handle carries this, never the path."""
    return hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:32]


def _sign(secret, prefix, payload):
    body = _b64e(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = hmac.new(secret, (prefix + "." + body).encode("ascii"), hashlib.sha256).digest()
    return prefix + "." + body + "." + _b64e(signature)


def issue_handle(secret, workspace, library_id, relative_path, source_version):
    """Mint the opaque file identifier a citation carries. Only the parent can do this."""
    return _sign(secret, HANDLE_PREFIX, {"v": PAYLOAD_VERSION, "k": "h", "w": workspace_fingerprint(workspace),
                                         "l": str(library_id), "p": str(relative_path), "s": str(source_version)})


def issue_cursor(secret, workspace, library_id, relative_path, source_version, start_char):
    """Mint a continuation cursor bound to the same workspace, library, file and version."""
    return _sign(secret, CURSOR_PREFIX, {"v": PAYLOAD_VERSION, "k": "c", "w": workspace_fingerprint(workspace),
                                         "l": str(library_id), "p": str(relative_path), "s": str(source_version),
                                         "n": int(start_char)})


def _decode(secret, token, prefix, kind):
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        raise ReadError(READ_HANDLE_INVALID, 404, "the identifier is malformed; run knowledge_search again")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != prefix:
        raise ReadError(READ_HANDLE_INVALID, 404, "the identifier is not a read handle; run knowledge_search again")
    body, signature = parts[1], parts[2]
    expected = _b64e(hmac.new(secret, (prefix + "." + body).encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature):
        raise ReadError(READ_HANDLE_INVALID, 404, "the identifier is not valid for this service; run knowledge_search again")
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ReadError(READ_HANDLE_INVALID, 404, "the identifier is unreadable; run knowledge_search again") from None
    if not isinstance(payload, dict) or payload.get("v") != PAYLOAD_VERSION or payload.get("k") != kind:
        raise ReadError(READ_HANDLE_INVALID, 404, "the identifier is not valid for this service; run knowledge_search again")
    for field in ("w", "l", "p", "s"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ReadError(READ_HANDLE_INVALID, 404, "the identifier is incomplete; run knowledge_search again")
    if kind == "c" and not isinstance(payload.get("n"), int):
        raise ReadError(READ_HANDLE_INVALID, 404, "the cursor is incomplete; run knowledge_search again")
    return payload


class Issuer:
    """Per-workspace, per-library handle factory; the service keeps one for the life of a query."""

    def __init__(self, secret, workspace, library_id):
        self.secret, self.workspace, self.library_id = secret, str(workspace), str(library_id)

    def issue(self, relative_path, source_version):
        return issue_handle(self.secret, self.workspace, self.library_id, relative_path, source_version)


class Sessions:
    """The per-process read budget, keyed by an opaque session key the caller supplies.

    The key is stored hashed: the service has no use for the caller's own identifier. Entries are
    bounded, and the oldest entry is dropped first when the table is full - a new session is never
    refused because an old one was long-lived.
    """

    def __init__(self, max_reads=MAX_READS_PER_SESSION, max_chars=MAX_SESSION_CHARS,
                 max_entries=MAX_SESSIONS, min_chars=MIN_READ_CHARS):
        self._max_reads, self._max_chars = int(max_reads), int(max_chars)
        self._max_entries, self._min_chars = int(max_entries), int(min_chars)
        self._state = {}

    def reserve(self, key, requested_chars):
        """Count one read attempt and return `(state, effective_char_budget)`.

        Called after the handle and the file have been validated and before any byte is read, so a
        forged identifier costs nothing while a real read cannot start beyond the session budget. The
        returned budget is the request clamped to what the session has left, so a long answer can never
        overrun the accumulated cap; when even the minimum window no longer fits, the read is refused.
        """
        state = self._state.get(key)
        if state is None:
            while len(self._state) >= self._max_entries:
                self._state.pop(next(iter(self._state)))
            state = self._state[key] = {"reads": 0, "chars": 0}
        if state["reads"] >= self._max_reads:
            raise ReadError(READ_LIMIT_EXCEEDED, 429,
                            "session read count limit %d reached; answer from what you already have" % self._max_reads)
        remaining = self._max_chars - state["chars"]
        if remaining < self._min_chars:
            raise ReadError(READ_LIMIT_EXCEEDED, 429,
                            "session read budget of %d characters is exhausted" % self._max_chars)
        state["reads"] += 1
        return state, max(self._min_chars, min(int(requested_chars), remaining))

    def charge(self, state, chars):
        state["chars"] += max(0, int(chars))

    def report(self, state):
        return {"readsUsed": state["reads"], "readsLimit": self._max_reads,
                "sessionCharsUsed": state["chars"], "sessionCharsLimit": self._max_chars,
                "sessionCharsRemaining": max(0, self._max_chars - state["chars"])}


def _clamp(value, low, high, default):
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


def _line_start_before(text, position):
    """The start of the line preceding `position`, or 0 when there is none."""
    if position <= 0:
        return 0
    newline = text.rfind("\n", 0, position - 1)
    return 0 if newline < 0 else newline + 1


def file_line_count(text):
    """How many lines the text has, by the same rule the read window reports (`window_for`'s
    `fileLines`). One definition, so a refusal's `fileLines` and a window's `fileLines` cannot disagree."""
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def line_start(text, line_number, file_lines=None):
    """The code point offset where 1-based `line_number` begins.

    `file_lines` is the bound the caller may name in a refusal. It is passed in rather than computed here
    because `window_for` owns that definition; when it is omitted the refusal still says which reason
    applied and simply reports no line count.
    """
    if isinstance(line_number, bool) or not isinstance(line_number, int) or line_number < 1:
        raise ReadError(READ_REQUEST_INVALID, 400, "startLine must be a positive integer",
                        facts=read_facts(READ_REASON_START_LINE_INVALID,
                                         requested_start_line=line_number,
                                         file_lines=file_lines,
                                         suggested_action=READ_ACTION_CORRECT_START_LINE))
    if line_number == 1:
        return 0
    position = -1
    for _ in range(line_number - 1):
        position = text.find("\n", position + 1)
        if position < 0:
            raise ReadError(READ_RANGE_INVALID, 400, "startLine is past the end of the file",
                            facts=read_facts(READ_REASON_START_LINE_PAST_END,
                                             requested_start_line=line_number,
                                             file_lines=file_lines,
                                             suggested_action=READ_ACTION_CORRECT_START_LINE))
    return position + 1


def window_for(text, start, max_chars=DEFAULT_READ_CHARS, max_lines=MAX_READ_LINES, overlap_lines=0):
    """The next readable window of `text`, starting at code point `start`.

    Whole lines are preferred: the window ends at a line boundary whenever that still leaves a usable
    amount of text, and a line longer than one whole read is cut mid-line and *flagged* instead of
    being silently dropped. The newline that ends a window is a separator and is not part of the
    excerpt, so `endChar` is the end of quoted content and the next cursor points at the character
    after that newline. Joined windows therefore reproduce the file exactly except for those
    separators - never a gap inside the returned text and never a repeated character.
    """
    length = len(text)
    if isinstance(start, bool) or not isinstance(start, int) or start < 0 or start > length:
        # Not recoverable by editing `startLine`: this refusal is about the resolved **offset**, which the
        # caller did not send. It is reported as a request problem without a line suggestion, because
        # suggesting one would be guessing at an internal offset the caller cannot compute.
        raise ReadError(READ_REQUEST_INVALID, 400, "the read start is outside the file",
                        facts=read_facts(READ_REASON_START_OUTSIDE_FILE, file_lines=file_line_count(text),
                                         suggested_action=READ_ACTION_LOCATE_AGAIN))
    # Only the hard ceiling is enforced here. A budget below MIN_READ_CHARS is honoured, not raised:
    # the session budget that produced it is a hard limit, and a caller may deliberately ask for a
    # small window. Callers that want a floor apply it before calling (the service does).
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        max_chars = DEFAULT_READ_CHARS
    max_chars = max(1, min(max_chars, MAX_READ_CHARS))
    max_lines = _clamp(max_lines, 1, MAX_READ_LINES, DEFAULT_READ_LINES)
    overlap_lines = _clamp(overlap_lines, 0, MAX_OVERLAP_LINES, 0)

    begin = start
    if overlap_lines:
        for _ in range(overlap_lines):
            begin = _line_start_before(text, begin)
    mid_line_start = begin > 0 and text[begin - 1] != "\n"

    stop, mid_line_stop = min(begin + max_chars, length), False
    if stop < length and (stop == begin or text[stop - 1] != "\n"):
        boundary = text.rfind("\n", begin, stop)
        # Give back the partially covered last line unless doing so would throw away most of the
        # window: a single line longer than the requested read is cut and flagged instead.
        floor = min(MIN_READ_CHARS, max(1, (stop - begin) // 2))
        if boundary >= 0 and boundary + 1 - begin >= floor:
            stop = boundary + 1
        else:
            mid_line_stop = True

    line_limit_reached = False
    if text.count("\n", begin, stop) > max_lines:
        position = begin
        for _ in range(max_lines):
            position = text.find("\n", position) + 1
        stop, line_limit_reached = position, True

    end = stop - 1 if stop > begin and text[stop - 1] == "\n" else stop
    excerpt = text[begin:end]
    start_line = text.count("\n", 0, begin) + 1
    next_start = None if stop >= length else stop
    return {
        "startLine": start_line, "endLine": start_line + text.count("\n", begin, end),
        "startChar": begin, "endChar": end,
        "startOffset": begin - (text.rfind("\n", 0, begin) + 1),
        "endOffset": end - (text.rfind("\n", 0, end) + 1),
        "excerpt": excerpt,
        "excerptComplete": _closes_a_sentence(excerpt),
        "endsAtLineBoundary": stop > begin and text[stop - 1] == "\n",
        "midLineStart": mid_line_start, "midLineStop": mid_line_stop,
        "lineLimitReached": line_limit_reached,
        "overlapsPrevious": begin < start,
        "hasMore": next_start is not None, "nextStartChar": next_start,
        "fileChars": length, "fileLines": file_line_count(text),
    }


def _closes_a_sentence(text):
    """The same boundary check the citation contract uses (`pipeline._closes_a_sentence`).

    Kept as a local copy rather than imported so the reader has no dependency on the query pipeline;
    `tests/reader_test.py` asserts the two agree, which is the stronger guarantee - they cannot drift
    without a failing test. It only means "ends a statement and stops inside no hyphenated word": it
    says nothing about whether conditions or units are present.
    """
    trimmed = (text or "").rstrip()
    if not trimmed:
        return False
    if re.search(r"[A-Za-z]-$", trimmed):
        return False
    return re.search(r"""[.!?][)\]}"'’”]?\s*$""", trimmed) is not None


def resolve(root, relative_path, max_file_bytes=MAX_FILE_BYTES):
    """The library file a handle records, or a refusal.

    The recorded path is treated as data, not as authority: it must be relative, must contain no
    traversal, every component must be a real directory entry (no symlink or junction, the same rule
    the snapshot walk applies), the result must still resolve inside the library, and the file must
    pass the same extension and size limits the snapshot enforces - where "the same size limit" means
    the library's own resolved ceiling (`capacity.resolve`), not a constant repeated here.
    """
    limit = evidence.byte_limit(max_file_bytes)
    root = Path(root)
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        raise ReadError(READ_FILE_UNAVAILABLE, 404, "the library root is unavailable") from None
    if not isinstance(relative_path, str) or not relative_path or relative_path.startswith(("/", "\\")):
        raise ReadError(READ_FILE_UNAVAILABLE, 404, "the recorded path is not a relative library path")
    parts = Path(relative_path.replace("\\", "/")).parts
    if not parts or any(part in ("", "..", ".") for part in parts) or ":" in parts[0]:
        raise ReadError(READ_FILE_UNAVAILABLE, 404, "the recorded path is not a relative library path")
    current = resolved_root
    for part in parts:
        current = current / part
        try:
            if current.is_symlink() or current.is_junction():
                raise ReadError(READ_FILE_UNAVAILABLE, 404, "the recorded file is a link, not library content")
        except OSError:
            raise ReadError(READ_FILE_UNAVAILABLE, 404, "the recorded file is unavailable") from None
    if not current.is_file():
        raise ReadError(READ_FILE_UNAVAILABLE, 404, "the recorded file is no longer in the library")
    try:
        real = current.resolve(strict=True)
        if not real.is_relative_to(resolved_root):
            raise ReadError(READ_FILE_UNAVAILABLE, 404, "the recorded file resolves outside the library")
        if current.suffix.lower() not in evidence.EXTENSIONS:
            raise ReadError(READ_FILE_UNAVAILABLE, 404, "the recorded file is not a supported text file")
        if current.stat().st_size > limit:
            raise ReadError(READ_FILE_UNAVAILABLE, 404, "the file exceeds the %d-byte library limit" % limit)
    except (OSError, ValueError, RuntimeError):
        raise ReadError(READ_FILE_UNAVAILABLE, 404, "the recorded file is unavailable") from None
    return current


def read_text(path, max_file_bytes=MAX_FILE_BYTES):
    """Read once, and return `(sourceVersion, normalisedText)` from those same bytes."""
    limit = evidence.byte_limit(max_file_bytes)
    try:
        data = path.read_bytes()
    except OSError:
        raise ReadError(READ_FILE_UNAVAILABLE, 404, "the recorded file is unreadable") from None
    if len(data) > limit:
        raise ReadError(READ_FILE_UNAVAILABLE, 404, "the file exceeds the %d-byte library limit" % limit)
    version = evidence.digest(data)
    try:
        text = evidence.normalised_text(data)
    except UnicodeDecodeError:
        raise ReadError(READ_FILE_NOT_TEXT, 415, "the file is not UTF-8 text; the library does not support it") from None
    return version, text


def _select(secret, handle, cursor, workspace, library_id):
    handle_payload = _decode(secret, handle, HANDLE_PREFIX, "h") if handle is not None else None
    cursor_payload = _decode(secret, cursor, CURSOR_PREFIX, "c") if cursor is not None else None
    if handle_payload is None and cursor_payload is None:
        # No identifier, so no file was located: the refusal deliberately carries no line count, because
        # that number would describe a file this caller never named.
        raise ReadError(READ_REQUEST_INVALID, 400, "a fileHandle or a cursor is required",
                        facts=read_facts(READ_REASON_IDENTIFIER_MISSING,
                                         suggested_action=READ_ACTION_LOCATE_AGAIN))
    if handle_payload is not None and cursor_payload is not None:
        first, second = handle_payload, cursor_payload
        if (first["w"], first["l"], first["p"], first["s"]) != (second["w"], second["l"], second["p"], second["s"]):
            raise ReadError(READ_REQUEST_INVALID, 400, "the cursor and the fileHandle describe different files",
                            facts=read_facts(READ_REASON_IDENTIFIER_CONFLICT,
                                             suggested_action=READ_ACTION_LOCATE_AGAIN))
    payload = cursor_payload if cursor_payload is not None else handle_payload
    if payload["w"] != workspace_fingerprint(workspace) or payload["l"] != str(library_id):
        # Guessing a valid identifier still buys nothing: it must belong to this workspace and library.
        raise ReadError(READ_HANDLE_FOREIGN, 403, "the identifier belongs to another workspace or library")
    return payload, (cursor_payload["n"] if cursor_payload is not None else None)


def read(secret, workspace, library_id, root, handle=None, cursor=None, source_version=None,
         start_line=None, max_chars=DEFAULT_READ_CHARS, max_lines=DEFAULT_READ_LINES, overlap_lines=0,
         before_read=None, max_file_bytes=MAX_FILE_BYTES):
    """One bounded, verified read. Returns the response body the service sends back."""
    payload, resume = _select(secret, handle, cursor, workspace, library_id)
    if source_version is not None and str(source_version) != payload["s"]:
        raise ReadError(READ_SOURCE_VERSION_MISMATCH, 400,
                        "sourceVersion does not match the file this identifier was issued for; "
                        "use the value from the citation you are reading")
    path = resolve(root, payload["p"], max_file_bytes)
    if before_read is not None:
        # The caller charges its session budget here: after validation (a forged identifier costs
        # nothing) and before the read (a real read cannot start beyond the budget).
        max_chars = before_read(_clamp(max_chars, MIN_READ_CHARS, MAX_READ_CHARS, DEFAULT_READ_CHARS))
    version, text = read_text(path, max_file_bytes)
    if version != payload["s"]:
        raise ReadError(SOURCE_VERSION_CHANGED, 409,
                        "the file changed since this identifier was issued; run knowledge_search again")
    if resume is not None:
        start = resume
        if start < 0 or start > len(text):
            raise ReadError(READ_REQUEST_INVALID, 400, "the cursor is outside the file; run knowledge_search again",
                            facts=read_facts(READ_REASON_CURSOR_OUTSIDE_FILE, file_lines=file_line_count(text),
                                             suggested_action=READ_ACTION_LOCATE_AGAIN))
    else:
        # The file has already been read and its version checked by this point, so the line count is a fact
        # about an **authorised** file - never a way to measure one the caller could not open. A caller that
        # is refused before this line (bad signature, foreign library, a changed version) gets no count.
        start = line_start(text, 1 if start_line is None else start_line, file_line_count(text))

    window = window_for(text, start, max_chars, max_lines, overlap_lines)
    # The extraction's own page marker at or above this window, or None. Taken from the bytes this
    # read already verified, so the page cannot describe a different version of the file, and a source
    # without markers (every Markdown file, a PDF extracted without them) honestly reports no page.
    window["pdfPage"] = source_card.pdf_page_for_line(source_card.split_lines(text), window["startLine"])
    # The location axis, measured against the bytes this read already verified: the offsets have to
    # rebuild the excerpt from the same text the version was taken from.
    window["locationVerified"] = text[window["startChar"]:window["endChar"]] == window["excerpt"]
    warnings = []
    if resume is not None and start_line is not None:
        warnings.append("READ_CURSOR_PRECEDENCE: the cursor decides where this read starts; startLine was ignored")
    if not window["excerpt"] and window["fileChars"] == 0:
        warnings.append("READ_EMPTY_FILE: the file has no text to read")
    elif not window["excerpt"]:
        warnings.append("READ_AT_END: the read starts at the end of the file; there is nothing further")
    if window["midLineStop"]:
        warnings.append("READ_LONG_LINE: a single line is longer than one read; the window stops mid-line "
                        "and the continuation this response returns continues inside that line")
    if window["lineLimitReached"]:
        warnings.append("READ_LINE_LIMIT: the window ended at the %d-line limit" % _clamp(max_lines, 1, MAX_READ_LINES, DEFAULT_READ_LINES))
    if window["overlapsPrevious"]:
        warnings.append("READ_OVERLAP: the window starts %d line(s) before the resume point and repeats that context"
                        % overlap_lines)
    if window["excerpt"] and not window["excerptComplete"]:
        # Three different situations used to share one sentence, and the recorded session shows why that is
        # wrong: a window that reached line 423 of 423 (`hasMore: false`, no cursor at all) still said "read on
        # with nextCursor before concluding anything from it" - advice pointing at a cursor the same response
        # reports as null. Splitting them keeps the mechanical flag honest and stops the tool from asking for a
        # window that cannot exist. Nothing here softens the incompleteness: the notice stays, only the advice
        # changes.
        if window["hasMore"]:
            warnings.append("EXCERPT_TRUNCATED: this window stops before a sentence boundary and the file "
                            "continues after it; read on with the continuation this response returns before "
                            "concluding anything from this window")
        else:
            warnings.append("EXCERPT_INCOMPLETE_AT_EOF: this window reaches the end of the file (line %d of %d) "
                            "and the sentence-boundary check found no ending; there is no next window, and that "
                            "check is mechanical - it looks for an ASCII . ! or ? at the end and does not look "
                            "past trailing markup, so a bold marker, a table cell or a full-width CJK stop all "
                            "read the same to it" % (window["endLine"], window["fileLines"]))
    result = dict(window)
    result.update({"libraryId": str(library_id), "relativePath": payload["p"],
                   "fileHandle": issue_handle(secret, workspace, library_id, payload["p"], payload["s"]),
                   "sourceVersion": version, "warnings": warnings})
    result["nextCursor"] = (issue_cursor(secret, workspace, library_id, payload["p"], payload["s"], window["nextStartChar"])
                            if window["nextStartChar"] is not None else None)
    return result
