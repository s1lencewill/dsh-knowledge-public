"""Parent-side, desensitized stage diagnostics that outlive the job directory.

The job directory - and with it the container's own logs - is deleted when a job ends, which is exactly
when "was a call in flight when the deadline hit?" has to be answerable. This sink is owned by the
parent and appends one JSON object per stage transition to a per-day JSONL file under
`.local/verification/diagnostics/`, so a job can be reviewed after cleanup.

Recorded: stage name, status, job id, per-call correlation id, monotonic-clock milliseconds, request
byte counts, and provider usage **when the provider reported it**. Never recorded: prompts, source text,
model replies, credentials, full URLs, or raw container output. `record()` accepts only an allow-listed
set of keys and sanitizes every value, so a caller cannot leak text through this path by accident.
Unknown token usage is written as the string "unknown" - never 0, which would read as "free".
"""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = Path(os.environ.get("DSH_KNOWLEDGE_DIAGNOSTICS_DIR") or (PROJECT / ".local/verification/diagnostics"))
UNKNOWN = "unknown"

#: Every field this sink will write. Anything else is dropped, not truncated.
ALLOWED_FIELDS = frozenset({
    "structureType", "fallbackReason", "event", "job", "callId", "status", "mode", "reason", "libraryId", "file", "fragments",
    "monotonicMs", "elapsedMs", "requestBytes", "files", "pid", "calls", "tokens", "usage",
})
_INT_FIELDS = ("monotonicMs", "elapsedMs", "requestBytes", "files", "pid", "calls", "tokens")
_USAGE_INT_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
#: The fragment record: ranges, sizes, scores and why a run was dropped, plus the same fragment as each
#: stage of the pipeline saw it. Text is never part of it, and a file is named by an opaque per-job id -
#: the file name itself is not recorded, and the structured-token rule is unchanged.
_FRAGMENT_RUN_FIELDS = ("fragmentId", "fileId", "startLine", "endLine", "chars", "score", "caption",
                        "selected", "reason", "locatable")
_FRAGMENT_TOP_FIELDS = ("parts", "budgetChars", "totalCandidates", "uniqueCandidates",
                        "loggedCandidates", "truncatedCount")
_FRAGMENT_LAYER_FIELDS = ("layer", "verification")
_FRAGMENT_LAYER_ENTRY_FIELDS = ("fragmentId", "fileId", "startLine", "endLine", "chars", "locatable")
_FRAGMENT_MAX_RUNS = 12
_FRAGMENT_MAX_LAYER_ENTRIES = 12
#: A structured token, not free text: no whitespace, no slash (so a URL cannot pass), no quoting.
#: `=` and `,` are kept because the informative reasons are small key=value facts and id lists.
_CODE = re.compile(r"^[A-Za-z0-9_.:,\-=\[\]<>]{1,64}$")


def _structured(value):
    """A structured token, a number, a boolean or None - never free text."""
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    text = str(value)
    return text if _CODE.match(text) else UNKNOWN


def _sanitize_fragment_entries(entries, fields, limit):
    """Rebuild a bounded list of records from whitelisted field names."""
    cleaned = []
    for entry in (entries if isinstance(entries, list) else [])[:limit]:
        if not isinstance(entry, dict):
            continue
        cleaned.append({name: _structured(entry[name]) for name in fields if name in entry})
    return cleaned


class Diagnostics:
    """A per-run JSONL sink. `enabled=False` makes every method a no-op (used by unit tests)."""
    def __init__(self, path=None, clock=time.monotonic, enabled=True, keep_days=7, job=None):
        self.path = Path(path) if path else None
        self.clock = clock
        self.enabled = enabled
        self.keep_days = keep_days
        self.job = job           # default correlation id, so call sites need not repeat it
        self.started = clock()
        self.calls = {}          # callId -> start monotonic, for in-flight accounting
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------------------------------
    @classmethod
    def for_job(cls, job_id=None, directory=None, **kwargs):
        """Construct the sink for one job and prune old daily files.

        Records nothing itself: the caller owns the `request_received` transition because it is the
        side that knows the mode and the library.
        """
        directory = Path(directory or DEFAULT_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%Y%m%d") + ".jsonl"
        sink = cls(directory / name, job=job_id, **kwargs)
        sink._prune(directory)
        return sink

    def _prune(self, directory):
        """Keep the sink bounded: drop daily files older than `keep_days`."""
        try:
            cutoff = time.time() - max(1, self.keep_days) * 86400
            for path in directory.glob("*.jsonl"):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
        except OSError:
            pass

    def elapsed_ms(self):
        return int((self.clock() - self.started) * 1000)

    def file_id(self, path):
        """An opaque, per-job handle for a file.

        The name of a source file must not enter the diagnostics, and the structured-token rule is not
        widened to let it in: the handle is a digest of the job and the path, stable for the whole job so
        the same file can be correlated across the engine, worker and parent stages.
        """
        if path is None:
            return UNKNOWN
        return hashlib.sha256(("%s|%s" % (self.job, path)).encode("utf-8", "replace")).hexdigest()[:12]

    # -- recording -------------------------------------------------------------------------------
    def record(self, event, **fields):
        if not self.enabled or self.path is None:
            return None
        payload = {"event": event, "monotonicMs": self.elapsed_ms()}
        if self.job is not None and "job" not in fields:
            fields["job"] = self.job
        for key, value in fields.items():
            if key not in ALLOWED_FIELDS:
                continue  # never fall back to writing the raw value
            payload[key] = self._sanitize(key, value)
        line = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # Observability must never be the reason a job fails: if the sink is unusable, go
                # quiet from here on and let the call it was describing proceed untouched.
                self.enabled = False
                return None
        return payload

    @staticmethod
    def _sanitize(key, value):
        if key in _INT_FIELDS:
            return value if isinstance(value, int) else UNKNOWN
        if key == "usage":
            return Diagnostics._sanitize_usage(value)
        if key == "fragments":
            return Diagnostics._sanitize_fragments(value)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return value
        text = str(value)
        return text if _CODE.match(text) else UNKNOWN

    @staticmethod
    def _sanitize_fragments(fragments):
        """Keep the ranking and cross-stage metadata, rebuilt from whitelisted names, and never text.

        A caller cannot smuggle a passage through this path: only numbers, booleans, None and
        structured-token strings survive, and both the run list and each stage's list are bounded. The
        counters say whether the record is the whole picture: when `truncatedCount` is non-zero the
        logged runs are a subset and must not be read as the complete candidate set.
        """
        if not isinstance(fragments, dict):
            return UNKNOWN
        cleaned = {}
        for name in _FRAGMENT_TOP_FIELDS:
            value = fragments.get(name)
            if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
                cleaned[name] = value
        cleaned["runs"] = _sanitize_fragment_entries(fragments.get("runs"), _FRAGMENT_RUN_FIELDS,
                                                     _FRAGMENT_MAX_RUNS)
        layers = fragments.get("layers")
        cleaned["layers"] = []
        for layer in (layers if isinstance(layers, list) else [])[:4]:
            if not isinstance(layer, dict):
                continue
            record = {"layer": _structured(layer.get("layer")),
                      "verification": _structured(layer.get("verification"))}
            record["fragments"] = _sanitize_fragment_entries(layer.get("fragments"),
                                                             _FRAGMENT_LAYER_ENTRY_FIELDS,
                                                             _FRAGMENT_MAX_LAYER_ENTRIES)
            cleaned["layers"].append(record)
        return cleaned

    @staticmethod
    def _sanitize_usage(usage):
        """Provider usage, or "unknown" when the provider did not report it."""
        if not isinstance(usage, dict):
            return UNKNOWN
        cleaned = {}
        for name in _USAGE_INT_FIELDS:
            value = usage.get(name)
            if isinstance(value, int) and value >= 0:
                cleaned[name] = value
        return cleaned or UNKNOWN

    # -- provider calls --------------------------------------------------------------------------
    def _active(self):
        return self.enabled and self.path is not None

    def call_started(self, job, call_id, request_bytes=None, remaining_ms=None):
        if not self._active():
            return None  # a disabled sink stays inert: it neither records nor tracks state
        with self._lock:
            self.calls[str(call_id)] = self.clock()
        return self.record("provider_call_started", job=job, callId=str(call_id),
                           requestBytes=request_bytes, status="started", reason=(
                               "remainingMs=%d" % remaining_ms if isinstance(remaining_ms, int) else None))

    def call_finished(self, job, call_id, usage=None, error=None, cancelled=False):
        """One terminal event per call: completed, failed, or cancelled."""
        with self._lock:
            start = self.calls.pop(str(call_id), None)
        elapsed = int((self.clock() - start) * 1000) if start is not None else UNKNOWN
        if cancelled:
            event, status, reason = "provider_call_cancelled", "cancelled", None
        elif error:
            event, status, reason = "provider_call_failed", "failed", error
        else:
            event, status, reason = "provider_call_completed", "completed", None
        return self.record(event, job=job, callId=str(call_id), status=status, elapsedMs=elapsed,
                           usage=usage if not cancelled else UNKNOWN, reason=reason)

    def in_flight(self):
        """Correlation ids that started and have no terminal event yet.

        Read under the lock: the deadline paths ask this question while the proxy thread may be
        recording, and an unlocked iteration is exactly the race those paths would hit.
        """
        with self._lock:
            return sorted(self.calls)

    # -- job stages ------------------------------------------------------------------------------
    def stage(self, event, job, **fields):
        return self.record(event, job=job, **fields)


def open_for_job(job_id, directory=None, enabled=True, **fields):
    """Per-job sink; a disabled sink keeps the call sites free of conditionals.

    `fields` describe the accepted request (mode, library) and are recorded on the one
    `request_received` event for this job, so each stage transition appears exactly once.
    """
    if not enabled:
        return Diagnostics(path=None, enabled=False)
    sink = Diagnostics.for_job(job_id, directory=directory)
    sink.record("request_received", job=job_id, **fields)
    return sink
