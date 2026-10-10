"""Loopback authenticated M0 service; explicitly experimental, fixture-only."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from typing import Literal
from runtime import child_environment, normalize_proxy_environment
from evidence import SnapshotCapacityError
from pipeline import build_snapshot, capacity_failure, finalize, worker_request
import appcontainer
import capacity
import catalog
import diagnostics as stage_diagnostics
import evidence
import ipc
import model_proxy
import reader
import real_data
import source_card
import onboarding
import staging
import release_identity

TOKEN = os.environ.get("DSH_KNOWLEDGE_TOKEN", "")
CONFIG = os.environ.get("DSH_KNOWLEDGE_CONFIG", ".local/libraries.json")
CONFIG = Path(CONFIG).resolve()
# A reasoning model needs far more than the original 30s/120s; both values stay configurable.
BUDGET_ENV = {"FAST": "DSH_KNOWLEDGE_BUDGET_FAST", "DEEP": "DSH_KNOWLEDGE_BUDGET_DEEP"}
BUDGET_DEFAULT = {"FAST": 90, "DEEP": 240}
#: A trial window pins its read budget here. This configures the existing hard per-session limit, it
#: does not add a capability: the value is read at startup and every refusal still comes from
#: reader.Sessions, so an over-budget read fails closed regardless of what the conversation does.
READ_MAX_PER_SESSION_ENV = "DSH_KNOWLEDGE_READ_MAX_PER_SESSION"
READ_MAX_CHARS_ENV = "DSH_KNOWLEDGE_READ_MAX_CHARS"


def _positive_int(name, default):
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def read_max_per_session():
    """The effective hard session read count (default 40; a trial window may pin it lower)."""
    return _positive_int(READ_MAX_PER_SESSION_ENV, reader.MAX_READS_PER_SESSION)


def read_max_chars():
    """The effective hard per-session character budget. A tiny value refuses every read, by design:
    the session budget is a ceiling, and `reader.MIN_READ_CHARS` is only the smallest useful window."""
    return _positive_int(READ_MAX_CHARS_ENV, reader.MAX_SESSION_CHARS)


JOBS = {}
MAX_JOBS = 100
#: How long a finished-but-unread result is kept. A client that submitted a job and then lost the response
#: has to be able to find its own job again by `requestId`; the lease is what makes that bounded instead of
#: unbounded accumulation, and an expired job is reclaimed by the next admission rather than by a background
#: sweeper. One entry the client is actively polling is never reclaimed.
RESULT_LEASE_SECONDS = 600
# Process-local logical-submission records: no question/result text. Valid entries are never evicted.
REQUEST_RETENTION_SECONDS = 1200
MAX_REQUEST_RECORDS = 2048
REQUEST_RECORDS = {}
#: Cancellation intents: "stop this request id when it appears". Bounded by count and by age, because they
#: exist for one race - a cleanup that arrives before the job it is about - and must not accumulate.
MAX_CANCEL_INTENTS = 64
CANCEL_INTENT_TTL_SECONDS = 300
CANCEL_INTENTS = {}
#: The states one submission can be in. A closed set: the client reports which one it observed rather than
#: inferring one from "the request timed out".
STATE_RECEIVED = "received"
STATE_ACCEPTED = "accepted"
STATE_RUNNING = "running"
STATE_CANCELLING = "cancelling"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_UNKNOWN = "unknown"
JOB_STATES = (STATE_RECEIVED, STATE_ACCEPTED, STATE_RUNNING, STATE_CANCELLING, STATE_COMPLETED,
              STATE_FAILED, STATE_CANCELLED, STATE_UNKNOWN)
#: The refusal codes the query routes return as `error.code`. A closed set, mirroring
#: `src/failures.ts::KNOWN_SERVICE_CODES`: the client keeps a member and drops anything else, so a code
#: invented here would reach no caller.
CODE_QUERY_BUSY = "QUERY_BUSY"
CODE_RESULT_CAPACITY = "RESULT_CAPACITY"
CODE_REQUEST_CONFLICT = "QUERY_REQUEST_CONFLICT"
CODE_REQUEST_INVALID = "QUERY_REQUEST_INVALID"
#: The closed set of reasons a container can end without a usable result. A token here is written into a
#: result warning, so the set is what bounds what that warning can say.
WORKER_NO_RESULT_REASONS = ("NO_RESULT_FILE", "RESULT_UNREADABLE", "RESULT_NOT_AN_OBJECT", "RESULT_EMPTY")
#: The closed set a lifecycle stage may report. Stages carry timing and a closed-set status - never a
#: question, a library name, a file name or an exception message.
ADMISSION_STAGES = ("request_received", "admission", "response_ready", "snapshot", "container_started",
                    "cancel_requested", "cancel_confirmed", "terminal")
#: Per-process read budget. Reads are one small local file read each, so this is a safety bound on a
#: runaway conversation rather than a cost control - it is enforced in the service, not in the prompt.
#: Both values are configurable (see the *_ENV names above) so a trial can pin a smaller budget.
READ_SESSIONS = reader.Sessions(read_max_per_session(), read_max_chars())
# Isolation is the default. "off" is an explicit development opt-out (structural smoke tests,
# budget/cancel probes) and must be requested by name; anything else refuses to start.
ISOLATION_MODES = ("required", "off")
ISOLATION_ENV = "DSH_KNOWLEDGE_ISOLATION"
PROJECT = Path(__file__).resolve().parents[1]
RUNNING_BUILD = release_identity.capture(PROJECT)
RUNTIME = Path(os.environ.get("DSH_KNOWLEDGE_RUNTIME") or (PROJECT / ".local/appcontainer-runtime")).resolve()
PATCHED_RGA = PROJECT / ".local/tools/rga-patched/rga.exe"
ISOLATION_STATE = {}
PROFILE_STATE_FILE = Path(os.environ.get("DSH_KNOWLEDGE_STATE_DIR") or (PROJECT / ".local/state")) / "isolation-profile.json"
#: Seconds to wait for a killed container tree to actually go away before the job is reported. This is
#: termination slack, not work budget: the model deadline and the container wait both stop at the
#: job's deadline.
TERMINATION_GRACE_SECONDS = 10
CONTAINER_PAYLOAD = staging.ENGINE_FILES  # kept as the public name of the staged payload list


def isolation_mode():
    """The effective isolation mode, resolved from the environment on every use.

    Unset means `required`: isolation is the default and `off` has to be named. Reading it at call
    time keeps /health honest about the mode the process is actually running in, instead of
    reporting whatever the variable happened to be when the module was imported.
    """
    return (os.environ.get(ISOLATION_ENV) or "").strip().lower() or "required"


def isolation_required():
    return isolation_mode() == "required"


def isolation_note(isolated):
    """What actually happened to this job's engine, stated per job rather than asserted once.

    Derived from the **code path that ran**, not from the configuration a reader might assume: `off` means the
    engine ran as an ordinary child process, and an isolation claim would then be false in exactly the
    deployment where a reader most needs the truth. There is no third state for a job that reaches this point -
    a service that cannot isolate refuses to start (ISOLATION_UNAVAILABLE), so it has no result to annotate.
    """
    if isolated:
        return ("ENGINE_ISOLATION: AppContainer enforced for this query - the engine, its model calls (through "
                "the parent proxy) and its citation checks all ran under it")
    return ("ENGINE_ISOLATION: disabled for this service (%s=off); the engine ran as an ordinary child process "
            "with the parent's own access" % ISOLATION_ENV)


def budget_seconds(mode):
    raw = os.environ.get(BUDGET_ENV.get(mode, ""), "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return BUDGET_DEFAULT.get(mode, BUDGET_DEFAULT["FAST"])


def cache_root():
    return CONFIG.parent / "query-cache"


def sweep_stale_jobs(root=None):
    """Remove job directories left behind by a previous service instance.

    A worker that is killed at its budget cannot clean up after itself, so the service parent owns
    the directory (see execute()) and also sweeps on startup: at that point no query is running.
    """
    root = Path(root or cache_root())
    removed = 0
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and (child.name.startswith("job-") or child.name.startswith("tmp")):
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    return removed


async def authenticated(request: Request, authorization: str = Header(default="")):
    if len(TOKEN) < 24 or not hmac.compare_digest(authorization, "Bearer " + TOKEN):
        raise HTTPException(401, "Unauthorized")
    # Old clients may still poll and cancel an admitted job. Only new business operations are gated.
    if RUNNING_BUILD is not None and (request.url.path in ("/libraries", "/reads", "/documents", "/catalogue", "/library-admin") or (request.url.path == "/queries" and request.method == "POST")):
        if not RUNNING_BUILD.get("valid") or request.headers.get("X-Knowledge-Build") != RUNNING_BUILD.get("id"):
            raise HTTPException(409, "BUILD_MISMATCH")
        status = release_identity.runtime_status(RUNNING_BUILD, RUNTIME, PROJECT, bool(ISOLATION_STATE.get("profile")))
        if status["status"] != "verified":
            raise HTTPException(409, "RUNTIME_BUILD_MISMATCH")


def stage_isolation_payload():
    """Copy the engine files and tools the container needs into the staged runtime.

    Idempotent: it runs at every startup so an upgraded engine never leaves a stale payload inside
    the container image.
    """
    target = RUNTIME / "engine"
    target.mkdir(parents=True, exist_ok=True)
    for name in staging.ENGINE_FILES:
        shutil.copy2(PROJECT / "engine" / name, target / name)
    for name, source in staging.resolve_tools(PROJECT).items():
        shutil.copy2(source, RUNTIME / (name + ".exe"))


def check_runtime_freshness():
    """Refuse to run on a runtime staged from older inputs."""
    ok, detail = staging.verify(RUNTIME, PROJECT)
    if not ok:
        raise appcontainer.IsolationError("STALE_RUNTIME: %s" % detail)


def check_runtime_compatibility(environment=None):
    """Refuse to serve a configuration the pruned runtime cannot honour.

    A capability that was pruned away must fail loudly at startup, not degrade silently: the
    container environment is an allow-list, so an embedding setting would simply never reach the
    engine and the query would quietly run without it.
    """
    import runtime_min
    problems = runtime_min.incompatibilities(environment or os.environ, evidence.EXTENSIONS)
    if problems:
        raise RuntimeError("RUNTIME_INCOMPATIBLE: " + "; ".join(problems))
    return True


def start_isolation():
    """Create the job profile and grant the runtime. Raises IsolationError after cleaning up."""
    try:
        if not appcontainer.available():
            raise appcontainer.IsolationError("Windows is required for AppContainer isolation")
        if not (RUNTIME / "ready").is_file():
            raise appcontainer.IsolationError("staged runtime missing at %s; run scripts/stage-appcontainer-runtime.py" % RUNTIME)
        check_runtime_freshness()
        stage_isolation_payload()
        reclaimed = appcontainer.reclaim_stale_profile(PROFILE_STATE_FILE)
        profile = appcontainer.Profile.create(appcontainer.PROFILE_PREFIX + uuid.uuid4().hex[:12])
        appcontainer.record_profile(PROFILE_STATE_FILE, profile.name, os.getpid())
        ISOLATION_STATE.clear()
        ISOLATION_STATE.update({"profile": profile, "sid": profile.sid,
                                "sidString": profile.sid_string.value, "reclaimed": reclaimed})
        appcontainer.grant(RUNTIME, profile.sid_string.value, ":(OI)(CI)(RX)")
        print("ISOLATION_START " + json.dumps({"profile": profile.name, "reclaimed": reclaimed}, sort_keys=True),
              file=sys.stderr, flush=True)
        return ISOLATION_STATE
    except BaseException:
        # A half-created profile (or a granted runtime) must never survive a failed startup.
        stop_isolation()
        raise


def stop_isolation():
    state = dict(ISOLATION_STATE)
    profile = state.get("profile")
    if profile is None:
        PROFILE_STATE_FILE.unlink(missing_ok=True)
        return
    try:
        appcontainer.revoke(RUNTIME, state.get("sidString", ""))
    finally:
        profile.delete()
        PROFILE_STATE_FILE.unlink(missing_ok=True)
        ISOLATION_STATE.clear()


async def stop(job):
    container = job.get("container")
    if container is not None:
        container.terminate()
    process = job.get("process")
    if process and process.returncode is None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                                                         stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


@asynccontextmanager
async def lifespan(app):
    mode = isolation_mode()
    if mode not in ISOLATION_MODES:
        raise RuntimeError("DSH_KNOWLEDGE_ISOLATION must be one of %s, got %r"
                           % (" or ".join(ISOLATION_MODES), mode))
    if len(TOKEN) < 24:
        raise RuntimeError("DSH_KNOWLEDGE_TOKEN must be set (at least 24 characters)")
    # One consistent proxy list for this process and everything it spawns (see runtime.py).
    os.environ.update(normalize_proxy_environment(os.environ))
    sweep_stale_jobs()
    # Cheap and independent of the runtime's presence: a configuration the pruned image cannot honour
    # must be refused before anything is created.
    check_runtime_compatibility()
    if mode == "required":
        try:
            start_isolation()
        except Exception:
            # Never half-start: remove anything that was created, then refuse to serve.
            stop_isolation()
            raise
    yield
    for job in JOBS.values():
        await stop(job)
    stop_isolation()


app = FastAPI(lifespan=lifespan, dependencies=[Depends(authenticated)], docs_url=None, redoc_url=None, openapi_url=None)


def inside(child, parent):
    """Whether `child` is `parent` or sits under it, compared **component-wise on resolved paths**.

    Component-wise and never a string prefix: `C:\\work\\kb2` is not inside `C:\\work\\kb`, while on Windows the
    comparison is case-insensitive and a trailing separator is irrelevant, so `DATA` and `data/` are the same
    directory. Both operands are expected to be resolved already.
    """
    return child == parent or parent in child.parents


def library_config():
    """The parsed library configuration, or an HTTP refusal.

    Read on every request instead of cached: it is a small operator-owned file, and a library registered
    or a manifest refreshed while the service is running has to take effect on the next call rather than
    at the next restart. A structurally unusable config is refused as a whole - there is no partial
    reading of it that would not amount to guessing which libraries the operator meant.
    """
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict) or not isinstance(config.get("libraries"), list):
            raise ValueError()
        return config
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        raise HTTPException(503, "LIBRARY_CONFIG_UNAVAILABLE") from None


def libraries(workspace, config=None):
    try:
        path = Path(workspace)
        if not path.is_absolute():
            raise ValueError()
        workspace = path.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError()
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(400, "WORKSPACE_INVALID: an existing absolute directory is required") from None
    try:
        selected = []
        for lib in (config if config is not None else library_config())["libraries"]:
            # A deleted configured workspace should not break all other libraries.
            configured = Path(lib["workspace"])
            if not configured.is_absolute():
                raise ValueError()
            root = Path(lib["root"])
            if not root.is_absolute():
                raise ValueError()
            # Two conditions, both component-wise on resolved paths, and both about the *session*:
            #
            #   1. the session directory is at or under the library's declared workspace - the declared
            #      workspace stays a hard gate, so a session opened outside it never sees the library;
            #   2. the library's own root is at or under the session directory - so a session never names
            #      material that lies outside the directory the operator actually opened.
            #
            # Why not equality of the declared workspace alone (which is what this used to be): DSH sessions
            # run in whatever directory the operator picked, and `.../data/library` is as reasonable a choice
            # as `.../data`. Equality made the library invisible in the first case with no diagnosis beyond
            # "none configured", which is exactly the trap this rule removes. It is not a widening of the
            # authorisation: condition 1 still excludes everything outside the declared workspace, condition 2
            # still excludes siblings (`kb` never matches `kb2`), and a config whose `root` escapes its own
            # `workspace` is now refused rather than honoured.
            if not inside(workspace, configured.resolve()):
                continue
            try:
                root = root.resolve(strict=True)
            except OSError:
                # A library whose files are gone is skipped; it is not an authorisation failure for the rest.
                continue
            try:
                visibility_root = Path(lib.get("sourceRoot") or root).resolve(strict=True)
            except OSError:
                continue  # A missing original directory cannot hide unrelated libraries.
            if lib.get("sourceRoot"):
                if not inside(visibility_root, configured.resolve()) or evidence.linked(Path(lib["sourceRoot"])):
                    raise ValueError()
                managed = CONFIG.parent / "managed-libraries"
                if not root.is_relative_to(managed.resolve()):
                    raise ValueError()
            if inside(visibility_root, workspace):
                # Resolved here, once, so that every later decision about a file in this library - the
                # snapshot, the read, the catalogue's readability check and the handle it mints - is made
                # against one number. A declaration this service cannot honour refuses the request while
                # naming the library: falling back to the default would answer with a limit the operator
                # did not choose and could not see, and would look exactly like a real capacity refusal.
                capacity.resolve(lib)
                selected.append(lib)
        return selected
    except capacity.CapacityConfigError as error:
        raise HTTPException(503, str(error)) from None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        raise HTTPException(503, "LIBRARY_CONFIG_UNAVAILABLE") from None


def catalogue_source(selected, config):
    """`(path, require_attribution)` for one library's catalogue, or None when it declares none.

    Two shapes, and the difference is the fix for a real defect rather than a preference:

    * **declared** - the library entry names its own `catalogue`. The catalogue must then be attributed
      to this library (`check_binding`), and there is no fallback: a missing, corrupt or foreign
      catalogue refuses the call instead of being replaced by some other file.
    * **undeclared** - allowed only while the configuration holds exactly one library, where the older
      process-wide `DSH_KNOWLEDGE_CATALOG` arrangement cannot confuse two libraries and is accepted
      unattributed. With two or more libraries it returns None, so the caller refuses. Otherwise the
      second library would be served the first library's titles, file names and page counts - and, since
      the entries point at files that library does not contain, would be handed them marked
      `readable: false`: another library's index presented as this library's, which is precisely the
      shape of answer this service exists not to give.
    """
    declared = selected.get("catalogue")
    if isinstance(declared, str) and declared.strip():
        return declared.strip(), True
    entries = config.get("libraries") if isinstance(config, dict) else None
    if isinstance(entries, list) and len(entries) == 1:
        path = (os.environ.get(catalog.CATALOG_ENV) or "").strip()
        return (path, False) if path else None
    return None


def legacy_catalogue_source():
    """The process-wide catalogue, for direct callers that predate per-library catalogues.

    The HTTP routes never use this - they resolve the catalogue from the library's own configuration -
    but `perform_find`/`perform_catalogue`/`perform_read` are also called directly by harnesses and
    tests that already know which library they are asking about, so an omitted argument keeps meaning
    exactly what it meant before. It is deliberately *unattributed*: the caller supplied the library, so
    there is no second library for the answer to be confused with.
    """
    path = (os.environ.get(catalog.CATALOG_ENV) or "").strip()
    return (path, False) if path else None


@app.get("/health")
async def health():
    from importlib.metadata import version
    # The window's provider budget as this process actually sees it. Reported here so an operator can
    # confirm a ceiling is in force *before* spending anything on it: without this, the only way to find
    # out whether the cap was picked up is to make a request and see whether it was refused.
    used, limit, path = model_proxy.quota_state()
    running = busy_job()
    return {"build": RUNNING_BUILD, "runtimeBuild": release_identity.runtime_status(RUNNING_BUILD, RUNTIME, PROJECT, bool(ISOLATION_STATE.get("profile"))), "protocol": 1, "sirchmunk": version("sirchmunk"), "experimental": True, "fixtureOnly": True,
            "isolation": isolation_mode(), "isolationActive": bool(ISOLATION_STATE.get("profile")),
            # "The service is healthy" and "a query can be admitted right now" are different facts, and
            # conflating them is how a healthy 200 read as "search will work" while every submission was
            # being refused. Only closed-set facts about the running job: its state, mode and elapsed time -
            # never another session's question, library name or result.
            "query": {"busy": running is not None,
                      "state": job_state(running) if running else None,
                      "mode": running.get("mode") if running else None,
                      "elapsedSeconds": elapsed_seconds(running) if running else None,
                      "retained": len(JOBS), "capacity": MAX_JOBS},
            "providerBudget": {"configured": path is not None, "persisted": path is not None,
                               "enforced": limit is not None, "used": used if path is not None else None,
                               "max": limit, "remaining": (max(0, limit - used)
                                                           if limit is not None and path is not None else None)},
            # The effective read budget of this process, so a trial can verify the pinned limits
            # instead of trusting the prompt or a default.
            "readLimits": {"perSession": read_max_per_session(), "sessionChars": read_max_chars(),
                           "perResponse": reader.MAX_READ_CHARS}}


@app.get("/libraries")
async def list_libraries(workspace: str):
    return [{"id": lib["id"], "name": lib["name"]} for lib in libraries(workspace)]

def read_handles(request):
    """The handle issuer for one job: derived from the service token, scoped to its workspace/library.

    Nothing is persisted, so a citation's `fileHandle` remains readable after the job directory (and
    the snapshot inside it) is gone, and after a service restart on the same token. A request that
    carries no workspace (harnesses calling `execute` directly) gets no issuer: a handle is bound to
    the workspace it was minted for, so minting one without it would only produce a useless token.
    """
    if not request.get("workspace"):
        return None
    return reader.Issuer(reader.derive_secret(TOKEN), request["workspace"], request["libraryId"])


#: Distinguishes "the caller did not supply a catalogue source" (a direct harness/test call, which keeps
#: the legacy process-wide catalogue) from "the caller supplied none" (the service resolved this library's
#: configuration and found no catalogue, which is a refusal). The two must not collapse into one value.
_UNSET = object()


def catalogue_records(source, library_id, root):
    """`textFile` -> catalogue record, for card enrichment only.

    A card is presentation, so an unconfigured, unreadable or foreign catalogue must not fail a call that would
    otherwise succeed: the identity axis then honestly reads `not-assessed`, which is exactly what the
    service knows. Building the card never consults the catalogue for evidence - only for display - but it
    still resolves through `load_bound`, so a card cannot be decorated with another library's titles,
    file names or page counts either.
    """
    if not source:
        return {}
    path, require_attribution = source
    if not path:
        return {}
    try:
        loaded, _ = catalog.load_bound(path, library_id, root, require_attribution)
    except catalog.CatalogError:
        return {}
    return {str(record.get("textFile")): record for record in loaded.values()
            if isinstance(record, dict) and record.get("textFile")}


def attach_source_cards(result, catalogue=_UNSET, selected=None):
    """Attach a source card to every citation in a finished query result. Adds no claim.

    `location` and `locationVerified` come from the parent's own verification (pipeline), so a card
    cannot describe a position the verifier never confirmed. The page is then re-resolved here, where the
    catalogue is available: the extraction's recorded per-page line ranges are authoritative, and the
    pipeline's marker-derived page is only what a source without such a map falls back to. Identity and
    parse quality come from the same record, or stay `not-assessed` when there is none.
    """
    evidence_items = result.get("evidence") if isinstance(result, dict) else None
    if not evidence_items:
        return result
    if catalogue is _UNSET:
        catalogue = legacy_catalogue_source()
    library_id = (selected or {}).get("id")
    records = catalogue_records(catalogue, library_id, (selected or {}).get("root") or "")
    for item in evidence_items:
        if not isinstance(item, dict):
            continue
        record = records.get(str(item.get("relativePath")), {})
        relative = str(item.get("relativePath") or "")
        notices = []
        # The per-evidence truncation flags the pipeline already computed; the card must not invent a
        # second, softer notion of "complete".
        if item.get("excerptComplete") is False:
            notices.append("EXCERPT_TRUNCATED")
        item["sourceCard"] = source_card.card(
            library_id=item.get("libraryId"),
            file_name=record.get("fileName") or (relative.rsplit("/", 1)[-1] or None),
            excerpt=item.get("excerpt"),
            # A snippet is a citation, not a window, so "truncated" here keeps its original meaning: the quoted
            # range stops mid-statement. There is no hasMore for a quote.
            truncated=item.get("excerptComplete") is False,
            # The record decides the page. The pipeline's marker scan is passed only as the diagnostic hint
            # for a PDF whose record carries no usable map, so it can never become the reported page.
            location_fields=source_card.location(
                item.get("startLine"), item.get("endLine"),
                item.get("startChar"), item.get("endChar"), record=record,
                hint=item.get("pageHint")),
            source_version=item.get("sourceVersion"), citation_origin=item.get("excerptSource"),
            truncation_notices=notices,
            parse_quality=record.get("quality"),
            identity=record.get("verification") or source_card.IDENTITY_NOT_ASSESSED,
            identity_detail=record.get("verificationDetail"),
            conflicts=record.get("conflicts") or None,
            location_verified=bool(item.get("locationVerified")),
            title=record.get("titleRaw"), document_id=record.get("documentId"),
            relative_path=relative or None)
        for field in ("structureType", "fallbackReason", "bodyStartLine", "contentWarnings"):
            if field in item: item["sourceCard"][field] = item[field]
    return result


class Query(BaseModel):
    workspace: str
    libraryId: str
    query: str = Field(min_length=1, max_length=4000)
    mode: Literal["FAST", "DEEP"] = "FAST"
    #: The client's id for this submission, chosen **before** it is sent. It is what makes a lost response
    #: recoverable: the same id can be polled and cancelled, and a repeat of the same id with the same
    #: parameters is answered with the job that already exists instead of starting a second billed one.
    #: Optional so that an older client keeps working unchanged.
    requestId: str | None = Field(default=None, max_length=64)
    #: The client's own session grouping key. Used for the ownership comparison - a job may only be polled
    #: or cancelled from the scope that submitted it - and never returned to any caller.
    session: str | None = Field(default=None, max_length=200)

    @field_validator("requestId")
    @classmethod
    def _request_id_shape(cls, value):
        """Refuse a malformed id rather than ignoring it.

        Ignoring it would look identical to a first submission and silently create a second job under a
        new id, which is the exact double-billing the idempotency rule exists to prevent.
        """
        if value is None or value == "":
            return None
        if not REQUEST_ID_PATTERN.match(value):
            raise ValueError("a request id is 'kq-' followed by 8 to 32 lowercase alphanumerics")
        return value


def _container_result(state_dir):
    """The container's result file as a dict, or None when it is absent or unusable."""
    payload = _read_container_result(state_dir)
    return payload if isinstance(payload, dict) else None


def worker_checkpoint(state_dir):
    # Container self-report only. A missing marker cannot prove that a stage was never entered.
    try:
        path = Path(state_dir) / "entry-progress.json"
        if path.stat().st_size > 256:
            return "UNKNOWN"
        value = json.loads(path.read_text(encoding="utf-8"))
        stage = value.get("stage") if isinstance(value, dict) else None
        return stage if isinstance(stage, str) and stage in {"BOOTSTRAP", "WORKER_IMPORT", "ENGINE_RUN", "ENGINE_RETURNED",
                                  "RESULT_SERIALIZE", "RESULT_WRITE", "RESULT_WRITTEN"} else "UNKNOWN"
    except (OSError, ValueError):
        return "UNKNOWN"


def _read_container_result(state_dir):
    """The container's result, or a closed-set token saying *why* there is none.

    The distinction matters to the client: "the worker never wrote a result" and "the worker wrote something
    that is not a result" are different failures with different next steps, and both used to arrive as one
    generic "produced no result". The token is a member of `(NO_RESULT_FILE, RESULT_UNREADABLE,
    RESULT_NOT_AN_OBJECT, RESULT_EMPTY)`, never an exception message.
    """
    result_file = Path(state_dir) / "result.json"
    if not result_file.is_file():
        return "NO_RESULT_FILE"
    try:
        raw = result_file.read_text(encoding="utf-8")
    except OSError:
        return "RESULT_UNREADABLE"
    try:
        payload = json.loads(raw)
    except ValueError:
        return "RESULT_UNREADABLE"
    if payload is None:
        return "RESULT_EMPTY"
    if not isinstance(payload, dict):
        return "RESULT_NOT_AN_OBJECT"
    return payload


# --- the query lifecycle ----------------------------------------------------------------------------
#
# One submission is tracked as a job record with an explicit state, an owner and a request id, because the
# failure this exists for is a *lost* response: the client's single request timed out at its own 10s bound
# while the service was still building a snapshot, so the client never learned an id and could neither poll
# nor cancel. Everything below keeps that job reachable by the id the client chose in advance, and keeps the
# state the client asks about explicit rather than inferred.

REQUEST_ID_PATTERN = re.compile(r"^kq-[0-9a-z]{8,32}$")


def request_id_of(value):
    """The caller's request id, validated by shape, or None when absent/invalid."""
    if value is None:
        return None
    if not isinstance(value, str) or not REQUEST_ID_PATTERN.match(value):
        raise refusal(400, CODE_REQUEST_INVALID)
    return value


def owner_of(workspace=None, library_id=None, session=None):
    """The ownership scope of one submission: workspace, library and the caller's session key.

    A job may only be polled or cancelled from the scope that submitted it. The session value is the
    client's own grouping key (the durable session id) and is used for comparison only - it is never
    stored in a result, logged, or reported to another session.

    `None` means "this caller asserted no scope at all", which is what an older client does: the three
    fields are then all absent and the result is None rather than an all-empty scope that could never
    match anything. That distinction is load-bearing - an empty-string workspace is a *different* claim
    from an absent one, and collapsing them would refuse every job to every caller that omitted them.
    """
    if workspace is None and library_id is None and session is None:
        return None
    # `None` stays `None`, and that is not cosmetic: `str(None)` is the *string* `"None"`, which would make an
    # omitted field look present to `full_scope`'s check for emptiness and would let a partially-specified
    # request compare equal to a job whose field really is absent. An omitted field has to be distinguishable
    # from a field whose value happens to be the word "None".
    def field(value):
        if value is None:
            return None
        text = str(value).strip()
        return text or None
    return {"workspace": field(workspace), "libraryId": field(library_id), "session": field(session)}


def fingerprint_of(queries):
    """A digest of everything that makes two submissions the same job.

    Used for idempotent admission only: a repeat carrying the same request id *and* the same fingerprint
    is the same submission retried after a lost response, and is answered with the existing id. A repeat
    that changes the parameters is a different query and is refused instead, because reusing the id would
    hide a second, separately billed job behind the first one.
    """
    material = json.dumps([queries.get(name) for name in ("workspace", "libraryId", "query", "mode")],
                          sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:32]


def job_state(job):
    """The state of one job, derived from what actually happened rather than from what was requested.

    The **result** is read first, because it is the terminal fact: a job that has one has finished, whatever
    the task object says about itself (a harness or an in-process caller can finish a job without the task
    bookkeeping having caught up). Only a job with no result is described by its task.
    """
    # A shutdown whose writers could not be observed is **not** cancelled, whatever the task or an earlier
    # terminal value says: the job still holds its slot and something may still be writing on its behalf.
    # Reporting it as cancelled would be exactly the "confirmed before the writer exited" defect, one layer
    # down - so this is checked before the result, not after it.
    if job.get("cancelUnconfirmed"):
        return STATE_CANCELLING
    task = job.get("task")
    if task is not None and not task.done():
        return job.get("state") or STATE_RUNNING
    result = job.get("result")
    if isinstance(result, dict):
        status = result.get("status")
        if status == "cancelled":
            return STATE_CANCELLED
        if status == "failed":
            return STATE_FAILED
        return STATE_COMPLETED
    if job.get("state") == STATE_CANCELLING:
        return STATE_CANCELLING
    task = job.get("task")
    if task is None or not task.done():
        # An explicit None means the job is live and no more specific state has been recorded for it yet.
        return job["state"] if "state" in job else STATE_RUNNING
    return STATE_UNKNOWN


def elapsed_seconds(job):
    started = job.get("startedAt")
    return None if started is None else round(time.monotonic() - started, 2)


def _state_payload(identity, job):
    """The public lifecycle view of one job: state, terminal result, and the deadline it is under."""
    result = job.get("result")
    state = job_state(job)
    return {"id": identity, "requestId": job.get("requestId"), "state": state,
            "terminal": job_settled(job) and state in (STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED) and result is not None,
            "acknowledged": bool(job.get("acknowledged", False)),
            "mode": job.get("mode"), "elapsedSeconds": elapsed_seconds(job),
            "budgetSeconds": job.get("budgetSeconds"),
            "result": result}


def refusal(status, code, detail=None, message=None):
    """One structured query refusal, as the exception the routes raise.

    It is an `HTTPException` subclass so the status code is still the one an older caller checks, and so
    existing callers that catch `HTTPException` keep working; the `error` object is added to the body by
    the handler below. The machine-readable half is what this round is about: the client keeps only a code
    from a closed set, and an HTTP 429 is no longer one undifferentiated "quota" fact.

    `message` is the legacy `detail` string. It defaults to the code, and a route that inherited a longer
    established sentence (the trial gate's own code, "Unknown library for workspace") passes it through so
    the older half of the body is unchanged.
    """
    return QueryRefusal(status_code=status, detail=message or code,
                        error={"code": code, **(detail or {})})


class QueryRefusal(HTTPException):
    """A query-route refusal that carries a closed-set `error` object as well as its status code."""

    def __init__(self, status_code, detail, error):
        super().__init__(status_code=status_code, detail=detail)
        self.error = error


@app.exception_handler(QueryRefusal)
async def query_refusal(request, exc):
    """The body both generations read: the legacy `detail` code, and the structured `error` object."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "error": exc.error})



def worker_failure(exit_code=None, reason=None):
    """Classify a container/worker that ran but produced no usable result.

    This is deliberately **not** ISOLATION_UNAVAILABLE: the container did start and run (a job that could
    not be isolated never reaches this point - the service refuses such a query outright). Folding
    "the worker exited" into "isolation is unavailable" is what made an observed 75-second job with four
    provider calls report itself as an isolation problem.

    `reason` says *which* no-result it was, as a member of a closed set (`NO_RESULT_FILE`,
    `RESULT_UNREADABLE`, `RESULT_NOT_AN_OBJECT`, `RESULT_EMPTY`), because "nothing was written" and "what was
    written is not a result" are different defects in the worker.
    """
    detail = reason if isinstance(reason, str) and reason in WORKER_NO_RESULT_REASONS else None
    if exit_code is None:
        return {"code": "WORKER_NO_RESULT", "reason": detail or "no_result_file", "exitCode": None}
    if exit_code == 0:
        return {"code": "WORKER_NO_RESULT", "reason": detail or "exited_zero_without_result", "exitCode": 0}
    return {"code": "WORKER_EXITED", "reason": detail or "nonzero_exit", "exitCode": int(exit_code)}


def lifecycle_result(code, reason=None, exit_code=None, note=None):
    """A failed terminal result that names a closed-set code, and never an arbitrary string.

    The `reason` is either the caller's own closed-set token or `worker_failure`'s; an exception message
    from anywhere else is dropped, so no path, handle or upstream sentence can reach a client through a
    warning.
    """
    facts = ["%s" % code]
    safe_reason = reason if isinstance(reason, str) and re.match(r"^[A-Za-z0-9_.:\-]{1,64}$", reason) else None
    if safe_reason:
        facts.append(safe_reason)
    if isinstance(exit_code, int):
        facts.append("exitCode=%d" % exit_code)
    if note:
        facts.append(note)
    return {"status": "failed", "answer": "", "evidence": [], "warnings": [": ".join(facts)]}


class ContainerNoResult(appcontainer.IsolationError):
    """The container ran to completion but produced no result file. A *worker* failure, not an isolation one."""

    def __init__(self, message, reason="NO_RESULT_FILE"):
        super().__init__(message)
        self.reason = reason


class ContainerFailed(appcontainer.IsolationError):
    """The container ran and exited non-zero without a result. Also a worker failure."""

    def __init__(self, message, exit_code=None, reason="NO_RESULT_FILE"):
        super().__init__(message)
        self.exit_code = exit_code
        self.reason = reason


def _prep_deadline(job, task):
    """The callback that ends a job's preparation when its budget is gone.

    A closure rather than a lambda so the two facts it records are explicit: the job ran out of budget (which
    is what tells the exception handler *why* preparation stopped), and the preparation task is what gets
    cancelled - the timer cannot reach the worker thread itself.
    """
    def fire():
        job["deadlineHit"] = True
        task.cancel()
    return fire


async def _await_task(task, timeout=None):
    """Wait for one task, absorbing cancellation and returning its outcome instead of raising.

    Two callers need this and for the same reason: a shutdown path must not be *undone* by the cancellation
    it is serving. Absorbing the exception here keeps the `await` in the caller's frame - which is where the
    first `CancelledError` becomes an exception the caller can catch and turn into a terminal state - while
    still guaranteeing the waited-for work has actually ended before the next line runs.
    """
    try:
        if timeout is None:
            await task
        else:
            await asyncio.wait_for(asyncio.shield(task), timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        return False
    return True


def prep_settled(job):
    """Whether the job's synchronous preparation (snapshot and fingerprinting) has finished.

    This is the fact the old implementation got wrong: `asyncio.to_thread` runs the function in a worker
    thread that **`Task.cancel()` does not stop**, so a task cancelled in the middle of a snapshot returned a
    "cancelled" answer while the thread was still writing files into the job directory - which the cleanup
    then deleted, and the thread recreated. Nothing may be reported as stopped while this is false.
    """
    prep = job.get("prep")
    writer = job.get("writer")
    return (prep is None or prep.done()) and (writer is None or writer.done())


async def shutdown_job(job, timeout):
    """Stop one job and wait for everything that writes on its behalf to finish. Returns a closed-set outcome.

    The sequence matters and each step is here for a measured reason:

    1. **mark the job cancelling** before anything else, so a poll that arrives during the shutdown reports
       the transition instead of a job that still looks healthy;
    2. **cancel the task**, then await it - the await is where the cancellation actually propagates;
    3. **await the preparation task**, because the snapshot runs in a thread that task cancellation cannot
       reach. Without this step the "cancelled" answer is confirmed while a writer is still running;
    4. **stop the process/container** (`stop()`), which also waits for the tree to be gone;
    5. only then report the outcome.

    `timeout` bounds steps 3 and 4 only, never step 2: reporting is the caller's whole point, and a bound on
    reporting is what left the confirmation unobservable in the first place. When that bound expires the job
    is reported as `cancelling`/unconfirmed, the query lock is **not** released, and the caller is told the
    truth rather than being handed a success it cannot observe.
    """
    job["state"] = STATE_CANCELLING
    job["cancelRequested"] = True
    task = job.get("task")
    deadline = time.monotonic() + timeout
    if task is not None and not task.done():
        task.cancel()
        await _await_task(task, max(0.001, deadline - time.monotonic()))
    prep = job.get("prep")
    prepared = True
    if prep is not None and not prep.done():
        prepared = await _await_task(prep, max(0.001, deadline - time.monotonic()))
    stopped = await _await_task(asyncio.ensure_future(stop(job)), max(0.001, deadline - time.monotonic()))
    settled = bool(task is None or task.done()) and prep_settled(job) and bool(job.get("process") is None
                                                        or job["process"].returncode is not None)
    if not (prepared and stopped and settled):
        job["cancelUnconfirmed"] = True
        return {"code": "CANCEL_PARTIAL", "settled": False}
    if job.get("result") is None:
        job["result"] = terminal("cancelled", "Local worker stopped; remote requests already sent may still be billed")
    job["cancelUnconfirmed"] = False
    job["state"] = None
    return {"code": "CANCELLED", "settled": True}


def _grant_job_paths(work_dir, state_dir, request_file, sid_string):
    """Per-job SID grants: exactly the snapshot (read), the request file (read) and state (write)."""
    appcontainer.grant(work_dir, sid_string, ":(RX)")
    appcontainer.grant(work_dir / "files", sid_string, ":(OI)(CI)(RX)")
    appcontainer.grant(request_file, sid_string, ":(R)")
    appcontainer.grant(state_dir, sid_string, ":(OI)(CI)(M)")
    low_integrity, detail = appcontainer.set_low_integrity(state_dir)
    return {"lowIntegrity": low_integrity, "lowIntegrityDetail": detail}


async def run_isolated_async(job, request, child_request, work_dir, remaining, diag=None):
    """Run the engine inside the job's AppContainer, with the model reached through the parent.

    `remaining` is what is left of the *job's* deadline after the snapshot was built - the container
    never gets the full budget again, so queueing and snapshotting cannot extend a job's wall clock.
    The blocking container wait runs in a thread so cancellation still reaches stop(); any setup
    failure raises IsolationError, which the caller turns into a failed query. There is deliberately
    no fallback to a plain process.
    """
    state = ISOLATION_STATE
    if not state.get("sid"):
        raise appcontainer.IsolationError("isolation is required but no container profile is active")
    state_dir = work_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    pipe = proxy = None
    payload = dict(child_request)
    payload["stateDir"] = str(state_dir)
    if request["mode"] != "FILENAME_ONLY":
        provider = model_proxy.provider_from_environment()
        pipe = ipc.PipeServer("dsh-model-" + uuid.uuid4().hex[:12], sid_string=state["sidString"]).listen()
        # The proxy deadline is the job's, not a fresh budget: retries and reconnects in the child
        # cannot buy more time than the job has left.
        proxy = model_proxy.ModelProxy(pipe, provider, limits=model_proxy.Limits(deadline_seconds=remaining),
                                       diagnostics=diag, job=job.get("id"))
        model_proxy.serve_in_thread(proxy, read_timeout=10)
        payload["modelPipe"] = pipe.path
        payload["modelName"] = provider["model"]
        # Hand the child the same deadline so its own read/retry loop is bounded the same way.
        payload["modelBudgetSeconds"] = max(0.0, remaining)
    request_file = work_dir / "request.json"
    request_file.write_text(json.dumps({"request": payload, "stateDir": str(state_dir)}), encoding="utf-8")
    grants = _grant_job_paths(work_dir, state_dir, request_file, state["sidString"])
    entry = RUNTIME / "engine/container_entry.py"
    environment = appcontainer.container_environment(state_dir, RUNTIME)
    container = appcontainer.ContainerJob().start(
        state["sid"], [str(RUNTIME / "python.exe"), str(entry), str(request_file)], environment)
    job["container"] = container
    if diag is not None:
        diag.stage("container_started", job.get("id"), pid=getattr(container, "pid", None))
    try:
        try:
            # The container gets exactly what is left of the job budget, not a fresh copy of it. When
            # that expires the tree is terminated; the extra wait is only a safety net for the kill
            # landing, never extra work budget.
            await asyncio.wait_for(asyncio.to_thread(container.wait, int(max(1.0, remaining) * 1000)),
                                   timeout=max(1.0, remaining) + TERMINATION_GRACE_SECONDS)
        except TimeoutError:
            # The deadline and the engine's last write can race. If a result landed, keep it: a job
            # that finished inside its budget must never be reported as a bare timeout.
            if proxy is not None:
                proxy.cancel()
            if diag is not None:
                # This is the discrimination the diagnostics exist for: calls that started and never
                # finished were cut off in flight; zero in-flight calls means none had started. Read
                # once, so the id list and the count in the same event cannot disagree.
                in_flight = proxy.in_flight() if proxy else []
                diag.stage("container_deadline", job.get("id"), status="deadline",
                           reason="in_flight=%s" % (",".join(in_flight) or "none"),
                           calls=len(in_flight))
            container.terminate()
            written = _container_result(state_dir)
            if written is not None:
                job["budgetRace"] = "kept a result written at the deadline"
                return written
            raise
        except BaseException:
            # Cancellation or a wait failure: the container tree must die with it, and the proxy must
            # refuse any further model call for this job. A cancelled job deliberately returns no
            # result - the client asked for it to stop.
            if proxy is not None:
                proxy.cancel()
            container.terminate()
            raise
        if diag is not None:
            diag.stage("worker_checkpoint", job.get("id"), status=worker_checkpoint(state_dir))
        written = _read_container_result(state_dir)
        if not isinstance(written, dict):
            # The container started, ran to completion and produced no usable result. That is a **worker**
            # failure and is reported as one; "the engine produced no result" and "the engine could not be
            # isolated" are different facts and used to be collapsed into the same warning. Which *kind* of
            # no-result it was is carried as a closed-set token, never as an exception message.
            code = getattr(container, "exit_code", None)
            if isinstance(code, int) and code != 0:
                raise ContainerFailed("container exited %d without a result" % code, exit_code=code,
                                      reason=written)
            raise ContainerNoResult("container produced no result", reason=written)
        return written
    finally:
        job["container"] = None
        # The exit code has to be read before the handle is closed.
        exit_code = getattr(container, "exit_code", None)
        container.close()
        if proxy is not None:
            # Never a cancellation here: the job ended on its own, so leave `cancelled` alone and let
            # the diagnostics distinguish a finished job from an aborted one.
            proxy.stop()
        if pipe is not None:
            pipe.close()
        isolation_facts = {"job": job.get("id"), "exitCode": exit_code,
                           "pid": getattr(container, "pid", None),
                           "modelCalls": proxy.calls if proxy else 0,
                           # Two of the three window budgets, reported where they can be read afterwards
                           # without opening a private fragment: requests *started* at the provider, and the
                           # bytes this parent forwarded. The third budget (main-Agent requests) belongs to
                           # the host and is counted there, not here.
                           "modelSentBytes": proxy.sent_bytes if proxy else 0,
                           "tokens": proxy.tokens if proxy else 0,
                           "deduplicated": proxy.deduplicated if proxy else 0,
                           "cancelled": bool(proxy.cancelled) if proxy else False,
                           "terminated": bool(getattr(container, "terminated", False)),
                           "lowIntegrity": grants.get("lowIntegrity")}
        job["isolation"] = isolation_facts
        print("ISOLATION_JOB " + json.dumps(isolation_facts, sort_keys=True), file=sys.stderr, flush=True)


def _size_of(path):
    """The size of one snapshot file, or 0 when it vanished before it could be measured.

    Only ever used to report how much material the container's snapshot *holds*, which is a different fact
    from how much of it reached a provider. A file disappearing under a running job is a real possibility
    (the job directory is the parent's, and cleanup races), and a missing size must not fail the job.
    """
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def diagnostics_enabled():
    """Stage diagnostics are on by default; `DSH_KNOWLEDGE_DIAGNOSTICS=off` disables them."""
    return (os.environ.get("DSH_KNOWLEDGE_DIAGNOSTICS") or "on").strip().lower() not in ("off", "0", "false")


def mark_running(job, mode):
    """Record that the job's own coroutine has begun, and under which budget.

    Separate from `execute` only so a test that replaces `execute` with a stub can still reproduce the state
    the real one would have recorded: the state machine is the thing under test, and a stub that skipped this
    would make every "accepted" and "running" assertion vacuous.
    """
    job["state"] = STATE_RUNNING
    job["budgetSeconds"] = budget_seconds(mode)


async def execute(job, request):
    # One deadline for the whole job: queueing, the snapshot walk, the container start-up, every model
    # call and any retry all spend from the same budget, so a slow snapshot cannot extend the wall
    # clock and a retry cannot buy extra time.
    deadline = job.get("startedAt", time.monotonic()) + budget_seconds(request["mode"])
    mark_running(job, request["mode"])
    # Parent-side and desensitized: the only record that survives the job directory, which is why it
    # exists - "was a call in flight when the deadline hit?" is unanswerable afterwards otherwise.
    diag = stage_diagnostics.open_for_job(job.get("id"), enabled=diagnostics_enabled(),
                                          mode=request["mode"], libraryId=request.get("libraryId"))
    diag.stage("job_running", job.get("id"), status=STATE_RUNNING, mode=request["mode"])

    def remaining():
        return deadline - time.monotonic()

    def stop_requested():
        """Whether this job has been asked to stop. Checked at the boundaries between synchronous stages.

        A task cancellation only becomes an exception at a real await, and the snapshot is synchronous - so
        without an explicit check a cancel that arrives during it would not be observed until the snapshot was
        already finished and the container about to start.
        """
        return job.get("state") == STATE_CANCELLING

    # The parent owns the worker's working directory. A worker killed at its budget or on
    # cancellation never runs its own cleanup, so removal cannot live inside the child.
    request.setdefault("cacheRoot", str(cache_root()))
    work_dir = Path(request["cacheRoot"]) / ("job-" + str(job.get("id") or uuid.uuid4().hex))
    work_dir.mkdir(parents=True, exist_ok=True)
    request["workDir"] = str(work_dir)
    job["workDir"] = str(work_dir)
    ticker = None

    async def prepare():
        def blocking():
            if not job.get("library", {"syntheticFixture": True}).get("syntheticFixture", False):
                allowed, detail, warning = real_data.authorize(job["library"], evidence.EXTENSIONS)
                if not allowed:
                    return {"authorizationDenied": True, "detail": detail}
                request["trialWarning"] = warning
            if stop_requested() or remaining() <= 0:
                return {"preparationStopped": True}
            try:
                outcome = build_snapshot(request["root"], work_dir, request.get("maxFileBytes"))
                if not job.get("library", {"syntheticFixture": True}).get("syntheticFixture", False):
                    expected, _ = real_data.expected_files(job["library"])
                    copied = {value["relativePath"]: value["version"] for value in outcome[0].values()}
                    if copied != expected:
                        return {"authorizationDenied": True, "detail": "REAL_DATA_TRIAL_FILES_CHANGED: snapshot differs from authorization"}
                return outcome
            except SnapshotCapacityError as error:
                return error
        writer = asyncio.create_task(asyncio.to_thread(blocking))
        job["writer"] = writer
        try:
            return await asyncio.shield(writer)
        except asyncio.CancelledError:
            # Python cannot interrupt the filesystem writer. Drain it before acknowledging cancellation.
            await _await_task(writer)
            raise

    try:
        # Trusted side: only the parent reads the library. Capacity rejection happens here, before
        # any worker (and therefore any model client) exists.
        #
        # It runs in a worker thread, and that is a correctness requirement rather than a performance one:
        # walking a large library is blocking file I/O, and doing it on the event loop holds the *whole*
        # service - `/health`, polling for this job and every other route - for as long as the walk takes.
        # That is the mechanism behind the observed failure: a submission whose response could not be written
        # until the snapshot had finished, and a client whose own request bound expired first.
        #
        # The thread is bounded by the *job's* deadline, which is the correction this round adds: a slow walk
        # used to consume a fresh, unbounded window of its own, so a job could spend its whole budget in the
        # snapshot and still be handed a worker. One timer for the phase - not one per await - is what makes
        # the steps share the budget instead of each taking a new slice of it.
        preparation = asyncio.ensure_future(prepare())
        job["prep"] = preparation
        left = deadline - time.monotonic()
        if left <= 0:
            job["deadlineHit"] = True
            preparation.cancel()
        else:
            ticker = asyncio.get_running_loop().call_later(left, _prep_deadline(job, preparation))
        try:
            outcome = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            # Two different events reach here and they are not the same fact: the job ran out of budget, or
            # the caller stopped it. Both leave the preparation unfinished, and neither may be reported as a
            # snapshot result - the caller waits for the writer before saying anything (see shutdown_job).
            if job.get("deadlineHit"):
                diag.stage("job_deadline", job.get("id"), status="deadline",
                           reason="BUDGET_EXCEEDED_DURING_SNAPSHOT")
                job["result"] = terminal("partial", "BUDGET_EXCEEDED during snapshot")
                return
            raise
        finally:
            if ticker is not None:
                ticker.cancel()
                ticker = None
        if isinstance(outcome, dict) and outcome.get("authorizationDenied"):
            job["failureCode"] = "REAL_DATA_UNAUTHORIZED"
            job["result"] = terminal("failed", outcome["detail"])
            return
        if isinstance(outcome, dict) and outcome.get("preparationStopped"):
            if stop_requested():
                raise asyncio.CancelledError()
            job["result"] = terminal("partial", "BUDGET_EXCEEDED during preparation")
            return
        if isinstance(outcome, SnapshotCapacityError):
            diag.stage("snapshot_done", job.get("id"), status="capacity", reason="LIBRARY_CAPACITY_EXCEEDED")
            job["result"] = capacity_failure(outcome)
            return
        manifest, snapshot_warnings = outcome
        if stop_requested():
            raise asyncio.CancelledError()
        if not manifest:
            diag.stage("snapshot_done", job.get("id"), status="empty", files=0)
            job["result"] = {"status": "partial", "answer": "", "evidence": [],
                             "warnings": snapshot_warnings + ["No supported files"]}
            return
        if remaining() <= 0:
            # The snapshot alone consumed the job budget: say so instead of starting a doomed worker.
            diag.stage("job_deadline", job.get("id"), status="deadline", reason="BUDGET_EXCEEDED_DURING_SNAPSHOT")
            job["result"] = terminal("partial", "BUDGET_EXCEEDED during snapshot")
            return
        diag.stage("snapshot_done", job.get("id"), files=len(manifest),
                   bytes=await asyncio.to_thread(lambda: sum(_size_of(target) for target in manifest)))
        child_request = worker_request(request, work_dir)
        # Which path runs is the fact the result reports: it is decided here, not inferred by a reader.
        isolated = isolation_required()
        if isolated:
            payload = await run_isolated_async(job, request, child_request, work_dir, remaining(), diag)
        else:
            options = {"start_new_session": True} if os.name != "nt" else {}
            process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name("worker.py")),
                          stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                          env=child_environment(os.environ), **options)
            job["process"] = process
            diag.stage("container_started", job.get("id"), pid=process.pid)
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(child_request).encode()),
                                               max(0.001, remaining()))
            lines = output.decode(errors="replace").splitlines()
            values = [line.removeprefix("DSH_KNOWLEDGE_RESULT=") for line in lines if line.startswith("DSH_KNOWLEDGE_RESULT=")]
            payload = json.loads(values[-1]) if values else None
            if payload is None:
                # The plain path's equivalent of a container that produced no result: the worker ran, but
                # nothing usable came back. Reported as a worker failure, never as an isolation one.
                facts = worker_failure(process.returncode)
                job["failureCode"] = facts["code"]
                diag.stage("job_deadline", job.get("id"), status="failed", reason=facts["code"])
                job["result"] = lifecycle_result(facts["code"], facts["reason"], facts["exitCode"])
                return
        # Untrusted payload: every candidate is re-verified here against the original file.
        race = job.pop("budgetRace", None)
        if race:
            snapshot_warnings = list(snapshot_warnings) + ["DEADLINE_REACHED_RESULT_KEPT: " + race]
        if request.get("trialWarning"):
            snapshot_warnings = list(snapshot_warnings) + [request["trialWarning"]]
        job["result"] = attach_source_cards(
            finalize(payload, manifest, request, snapshot_warnings, diag, read_handles(request),
                     isolation=isolation_note(isolated)),
            request.get("catalogue", _UNSET), job.get("library"))
    except asyncio.CancelledError:
        await stop(job)
        diag.stage("job_deadline", job.get("id"), status="cancelled", reason="CLIENT_CANCELLED",
                   calls=len(diag.in_flight()))
        job["result"] = terminal("cancelled", "Local worker stopped; remote requests already sent may still be billed")
    except TimeoutError:
        await stop(job)
        diag.stage("job_deadline", job.get("id"), status="deadline", reason="BUDGET_EXCEEDED",
                   calls=len(diag.in_flight()))
        job["result"] = terminal("partial", "BUDGET_EXCEEDED")
    except (ContainerNoResult, ContainerFailed) as error:
        # The container ran and produced no usable result. Keep the two worker outcomes apart from an
        # isolation failure, and record only closed-set facts: an exit code and *why* no result was usable,
        # never a stack or a message.
        await stop(job)
        facts = worker_failure(getattr(error, "exit_code", None), getattr(error, "reason", None))
        job["failureCode"] = facts["code"]
        diag.stage("job_deadline", job.get("id"), status="failed", reason=facts["code"])
        job["result"] = lifecycle_result(facts["code"], facts["reason"], facts["exitCode"])
    except appcontainer.IsolationError:
        await stop(job)
        diag.stage("job_deadline", job.get("id"), status="failed", reason="ISOLATION_UNAVAILABLE")
        job["result"] = lifecycle_result("ISOLATION_UNAVAILABLE", "isolation_setup_failed")
    except Exception:
        await stop(job)
        diag.stage("job_deadline", job.get("id"), status="failed", reason="WORKER_FAILURE")
        job["result"] = lifecycle_result("WORKER_FAILURE", "unclassified_parent_side_error")
    finally:
        prep = job.get("prep")
        if prep is not None and not prep.done():
            await _await_task(prep)
        # Runs after stop() has confirmed the process is gone, so the snapshot copy is always removed.
        job["budget"] = {"seconds": budget_seconds(request["mode"]),
                         "elapsedSeconds": round(budget_seconds(request["mode"]) - remaining(), 2)}
        print("JOB_BUDGET " + json.dumps(dict(job["budget"], job=job.get("id"),
                                              status=(job.get("result") or {}).get("status")),
                                         sort_keys=True), file=sys.stderr, flush=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        job["workDirRemoved"] = not work_dir.exists()
        # When the job stopped working: the lease on an unread result is counted from here, so a client
        # that lost the response still has its bounded window to come back and collect it.
        job["finishedAt"] = job.get("finishedAt") or time.monotonic()
        if job.get("state") != STATE_CANCELLING:
            job["state"] = None  # derived from the terminal result from here on (see job_state)
        print("JOB_TERMINAL " + json.dumps(
            {"job": job.get("id"), "state": job_state(job), "requestId": job.get("requestId"),
             "failureCode": job.get("failureCode"), "elapsedSeconds": elapsed_seconds(job)},
            sort_keys=True), file=sys.stderr, flush=True)
        diag.stage("cleanup_done", job.get("id"), status="cleaned",
                   reason="workDirRemoved=%s" % job["workDirRemoved"],
                   calls=len(diag.in_flight()), elapsedMs=diag.elapsed_ms())


def terminal(status, warning):
    return {"status": status, "answer": "", "evidence": [], "warnings": [warning]}


def build_state_payload(identity, job):
    """The lifecycle view of one job, as the routes return it (see `_state_payload`)."""
    return _state_payload(identity, job)


class CancelRequest(BaseModel):
    """Who is asking to cancel. Optional so an older caller keeps working, checked when supplied."""
    workspace: str | None = None
    libraryId: str | None = None
    session: str | None = None


class AcknowledgeRequest(BaseModel):
    """One settlement call: confirm a result, stop a job, or record the intent to stop a job that is not here.

    `cancelIntent` is the difference between the two actions, and `requestId` is what lets a stop that has no
    job to address leave a durable marker instead of nothing.
    """
    workspace: str | None = None
    libraryId: str | None = None
    session: str | None = None
    requestId: str | None = Field(default=None, max_length=64)
    cancelIntent: bool = False


def job_settled(job):
    task = job.get("task")
    process = job.get("process")
    return (task is not None and task.done() and prep_settled(job)
            and (process is None or process.returncode is not None)
            and not job.get("cancelUnconfirmed"))


def prune_request_records(now=None):
    now = time.monotonic() if now is None else now
    for key, record in list(REQUEST_RECORDS.items()):
        if record.get("expiresAt") is not None and now >= record["expiresAt"]:
            REQUEST_RECORDS.pop(key, None)


def remember_finished(job):
    if job.get("task") is not None and job["task"].done() and prep_settled(job) and (job.get("process") is None or job["process"].returncode is not None):
        job["cancelUnconfirmed"] = False
    if not job.get("requestId") or not job_settled(job):
        return
    key = intent_key(job.get("owner"), job["requestId"])
    record = REQUEST_RECORDS.get(key)
    if record is None:
        return
    finished = job.get("finishedAt") or record.get("finishedAt") or time.monotonic()
    record.update(state=job_state(job), finishedAt=finished,
                  expiresAt=finished + REQUEST_RETENTION_SECONDS,
                  acknowledged=bool(job.get("acknowledged")), released=bool(job.get("released")))


def release_job(identity, job):
    # Preserve the terminal facts before dropping potentially large evidence/result payloads.
    remember_finished(job)
    JOBS.pop(identity, None)


def completed_record(identity, owner):
    prune_request_records()
    for record in REQUEST_RECORDS.values():
        if record["id"] == identity:
            scoped(record, owner, identity)
            return record
    return None


def released_payload(record):
    return {"id": record["id"], "requestId": record["requestId"], "known": True,
            "state": record.get("state", STATE_UNKNOWN),
            "terminal": record.get("expiresAt") is not None,
            "acknowledged": bool(record.get("acknowledged")), "released": True,
            "cancellationConfirmed": record.get("state") == STATE_CANCELLED,
            "result": None, "resultReleased": True, "expiresInSeconds":
            max(0, int(record["expiresAt"] - time.monotonic())) if record.get("expiresAt") else None}


def reclaim_finished(now=None):
    """Drop finished jobs whose result lease has expired or that the client has already acknowledged.

    Two rules, and both are needed:

    * an **acknowledged** job is done with - the client has read its result and said so;
    * an **unacknowledged** one is kept for `RESULT_LEASE_SECONDS` so a client that lost the response can
      still find its own job by `requestId`. Without the lease, a client that never came back would pin the
      capacity for as long as the process lives, and the next honest submission would be refused with
      RESULT_CAPACITY for a reason that is not about that client at all.
    """
    now = time.monotonic() if now is None else now
    for key in list(JOBS):
        job = JOBS[key]
        task = job.get("task")
        if not job_settled(job):
            continue
        if job.get("acknowledged", False):
            release_job(key, job)
            continue
        lease = job.get("finishedAt")
        if lease is not None and now - lease > RESULT_LEASE_SECONDS:
            release_job(key, job)


def find_by_request_id(request_id, owner):
    """The job a repeated submission refers to, when it belongs to the same owner scope.

    A submission always asserts its scope (the route requires the workspace and the library), so this is a
    comparison of two asserted scopes and never of an absent one against a present one.
    """
    for job in JOBS.values():
        if job.get("requestId") == request_id and job.get("owner") == owner:
            return job
    prune_request_records()
    return REQUEST_RECORDS.get(intent_key(owner, request_id))


def intent_key(owner, request_id):
    """The lookup key for a cancel intent: one owner scope, one request id."""
    return (owner or {}).get("workspace"), (owner or {}).get("libraryId"), (owner or {}).get("session"), request_id


def pending_intent(owner, request_id):
    """The intent asking for this request id to be stopped, when one was registered before the job existed."""
    entry = CANCEL_INTENTS.get(intent_key(owner, request_id))
    if entry is None:
        return None
    # A stale intent must not cancel a job submitted much later by the same scope under the same id: the
    # window is what makes this a *recovery* rule rather than a permanent delete key.
    if time.monotonic() - entry["at"] > CANCEL_INTENT_TTL_SECONDS:
        CANCEL_INTENTS.pop(intent_key(owner, request_id), None)
        return None
    return entry


def register_intent(owner, request_id):
    """Record "stop this request when it appears", bounded by count and by age.

    Deliberately **not** a job record: an intent that lived in the job table would be visible to the idempotency
    lookup, which would then treat the later real submission as a duplicate of the holder (a 409 instead of a
    cancellation). Two bounded structures with one job each are clearer than one structure with two meanings.
    """
    if not owner or not full_scope(owner) or not request_id:
        return None
    key = intent_key(owner, request_id)
    if key not in CANCEL_INTENTS and len(CANCEL_INTENTS) >= MAX_CANCEL_INTENTS:
        oldest = min(CANCEL_INTENTS, key=lambda item: CANCEL_INTENTS[item]["at"])
        CANCEL_INTENTS.pop(oldest, None)
    CANCEL_INTENTS[key] = {"at": time.monotonic()}
    return key


def take_intent(owner, request_id):
    """Consume a registered intent, returning True exactly once per registration."""
    key = intent_key(owner, request_id)
    entry = CANCEL_INTENTS.pop(key, None)
    if entry is None:
        return False
    return time.monotonic() - entry["at"] <= CANCEL_INTENT_TTL_SECONDS


def busy_job():
    for job in JOBS.values():
        task = job.get("task")
        if task is not None and not task.done():
            return job
        # A job whose shutdown could not be observed is *still holding the slot*: the preparation thread may
        # still be writing, and admitting a second query against a half-removed snapshot is exactly the
        # overlap the one-at-a-time rule exists to prevent.
        if job.get("cancelUnconfirmed"):
            return job
    return None


def full_scope(owner):
    """Whether a caller asserted the whole ownership scope: workspace, library **and** session.

    All three, and this is the tightening this round adds. A submission records a complete scope; a caller
    that supplies a prefix of it is not "the same owner with less detail", it is a caller this service cannot
    identify - and the previous version treated that as "no scope asserted" and served the job. Omitting a
    field is the cheapest possible way to reach another session's job, which is why an omission is refused
    rather than tolerated.
    """
    return bool(owner) and bool(owner.get("workspace")) and bool(owner.get("libraryId")) \
        and bool(owner.get("session"))


def scoped(job, owner, identity):
    """Authorise one job-recovery call, or refuse without saying anything about the job.

    Two rules, and the difference between them is the point:

    * a job **with** an owner is reachable only by a caller that asserts the same complete scope. An
      incomplete assertion is refused (403 `QUERY_SCOPE_REQUIRED`, which names only the *missing* fields -
      members of this route's own declared parameter set) rather than treated as "no scope";
    * a job **without** an owner predates this contract: it exists only in a direct in-process call (a test,
      a harness) or in a table restored from an older build, and it is served to a caller that asserts
      nothing. That tolerance is deliberately narrow - it requires a job that lacks the field, not merely a
      caller that omits it.

    What this is: a scope constraint between sessions that share one bearer token. What it is not: an identity
    check against a client that already holds the token - such a client can name any scope it likes. The
    honest description is "one session cannot accidentally reach another's job, and a foreign client has to
    lie explicitly rather than by omission".
    """
    recorded = job.get("owner")
    if recorded:
        if not full_scope(owner):
            missing = [name for name in ("workspace", "libraryId", "session") if not (owner or {}).get(name)]
            raise refusal(403, "QUERY_SCOPE_REQUIRED", {"reason": "scope_incomplete", "missing": missing})
        if recorded != owner:
            raise HTTPException(status_code=404, detail="unknown query")
        return job
    return job


def request_scope(request):
    """The scope one recovery call asserts, from its body."""
    return owner_of(getattr(request, "workspace", None), getattr(request, "libraryId", None),
                    getattr(request, "session", None))


def take_result_lease():
    """The oldest *unread* result, when the result table is full, or None.

    Returned so an admission that needs capacity can say *why* and for how long, instead of reporting a bare
    refusal. Only a job whose client never came back is a candidate - an unacknowledged result inside its
    lease is never taken from the client that is still polling it.
    """
    candidates = [(job.get("finishedAt") or 0.0, key, job) for key, job in JOBS.items()
                  if job.get("result") is not None and not job.get("acknowledged", False)]
    if not candidates:
        return None
    _, key, job = min(candidates, key=lambda item: item[0])
    return key, job


@app.post("/queries")
async def submit(query: Query):
    # The scope comes first, before the library and before any trial authorisation. It is a statement about
    # this request's own shape, and answering it after those gates would report a *different* problem - or,
    # for a library that is merely unauthorized, a refusal whose subject is unrelated to the missing scope.
    owner = owner_of(query.workspace, query.libraryId, query.session)
    if not full_scope(owner):
        raise refusal(403, "QUERY_SCOPE_REQUIRED",
                      {"reason": "scope_incomplete",
                       "missing": [name for name in ("workspace", "libraryId", "session")
                                   if not (owner or {}).get(name)]})
    config = library_config()
    selected = next((lib for lib in libraries(query.workspace, config) if lib["id"] == query.libraryId), None)
    if not selected:
        # The sentence is kept verbatim: an older caller reads exactly this, and the tests that pin it are
        # pinning the compatibility this route still owes them. The refusal class passes it through as the
        # legacy `detail`, so it is not lost by the structured half being added.
        raise refusal(403, "UNKNOWN_LIBRARY", None, message="Unknown library for workspace")
    if not selected.get("syntheticFixture", False):
        declared, detail = real_data.expected_files(selected)  # shape only; no filesystem hash here
        if declared is None:
            raise refusal(403, "REAL_DATA_UNAUTHORIZED", {"reason": "trial_authorization"}, message=detail)
    fingerprint = fingerprint_of(query.model_dump())
    # Idempotent admission: a repeat carrying the same request id and the same parameters is the *same*
    # submission arriving twice (a client that lost the response and asked again), and is answered with the
    # id it already has. A repeat that changes the parameters is refused rather than quietly reusing the id,
    # because that would hide a second, separately billed job behind the first one.
    if query.requestId:
        existing = find_by_request_id(query.requestId, owner)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise refusal(409, CODE_REQUEST_CONFLICT, {"reason": "request_id_reused"})
            if existing["id"] not in JOBS:
                raise refusal(410, "QUERY_RESULT_RELEASED", {"reason": "confirmed_result_released"})
            return {"id": existing["id"], **build_state_payload(existing["id"], existing)}
    reclaim_finished()
    running = busy_job()
    if running is not None:
        # QUERY_BUSY, not "quota": one query at a time is a concurrency limit, and a client that waits for
        # the state to change is doing the right thing, while a client that concludes its quota is spent
        # retries into the same refusal. The running job's mode and elapsed time are safe facts - they say
        # nothing about another session's question or library.
        raise refusal(429, CODE_QUERY_BUSY, {
            "reason": "one_running_query", "retryable": True, "phase": job_state(running),
            "retryAfterMs": max(0, int((running.get("budgetSeconds") or 0) * 1000
                                       - (elapsed_seconds(running) or 0) * 1000)),
            "mode": running.get("mode")})
    if len(JOBS) >= MAX_JOBS:
        # The table is full. An acknowledged or expired job was reclaimed above, so what is left is a result
        # whose client has not read it yet - and the honest answer names that, with the time it has left,
        # rather than reporting a capacity limit that would read as "the library is unavailable".
        lease = take_result_lease()
        retry_after = None
        expired = False
        if lease is not None:
            key, holder = lease
            left = RESULT_LEASE_SECONDS - (time.monotonic() - (holder.get("finishedAt") or 0.0))
            retry_after = max(0, int(left * 1000))
            if left <= 0:
                release_job(key, holder)
                expired = True
        if not expired:
            raise refusal(429, CODE_RESULT_CAPACITY,
                          {"reason": "result_lease_open", "retryable": True,
                           **({"retryAfterMs": retry_after} if retry_after is not None else {})})
    prune_request_records()
    if query.requestId and len(REQUEST_RECORDS) >= MAX_REQUEST_RECORDS:
        raise refusal(429, "QUERY_REQUEST_CAPACITY", {"reason": "dedup_retention_full", "retryable": True})
    identity = str(uuid.uuid4())
    job = {"id": identity, "result": None, "acknowledged": False, "library": selected,
           "requestId": query.requestId, "owner": owner, "fingerprint": fingerprint,
           "state": STATE_ACCEPTED, "mode": query.mode, "startedAt": time.monotonic(),
           "finishedAt": None, "budgetSeconds": budget_seconds(query.mode), "failureCode": None,
           "cancelPending": None, "cancelAttemptId": None, "released": False}
    JOBS[identity] = job
    request = {**query.model_dump(), "root": selected["root"], "cacheRoot": str(cache_root()),
               # Resolved once, here, and carried to the snapshot and the cards: the same number the
               # visibility check already validated, not a second resolution that could disagree.
               "maxFileBytes": capacity.resolve(selected),
               "catalogue": catalogue_source(selected, config)}
    if query.requestId:
        REQUEST_RECORDS[intent_key(owner, query.requestId)] = {
            "id": identity, "owner": owner, "requestId": query.requestId, "fingerprint": fingerprint,
            "state": STATE_ACCEPTED, "expiresAt": None}
    job["task"] = asyncio.create_task(execute(job, request))
    job["task"].add_done_callback(lambda task: remember_finished(job))
    # A cleanup that could not reach this service may have registered "stop this id" **before** the job
    # existed. It is honoured here, at the moment the job becomes real, so a late-admitted job is stopped
    # rather than left running with nobody holding its id.
    if query.requestId and take_intent(owner, query.requestId):
        job["cancelRequested"] = True
        await shutdown_job(job, max(1, int(job.get("budgetSeconds") or 0)))
        job["acknowledged"] = True
        job["released"] = True
        diag_intent(job)
        release_job(identity, job)
    # Admission is complete and bounded: the expensive work is inside the task, so this response is
    # written while the job is still in its first stage. The client's own admission bound can therefore
    # stay small and separate from the job budget.
    return {"id": identity, **build_state_payload(identity, job),
            **({"cancelIntentHonoured": True} if job.get("cancelRequested") else {})}


@app.get("/queries")
async def find_query(requestId: str, ownerWorkspace: str | None = None, ownerLibrary: str | None = None,
                     ownerSession: str | None = None):
    """What this service knows about one request id, for a client whose admission response was lost.

    The answer is deliberately soft **and** scoped. Soft: `known: false` with a null state means this service
    has no such job - a fact about *this* instance (a restart forgets its jobs), not a claim that nothing was
    ever submitted. Scoped: a caller must assert the same complete scope the job recorded, and an incomplete
    assertion is refused rather than served. It is also why this route exposes only the state and never the
    result or the mode: a client that lost the response gets enough to decide whether to wait or cancel.
    """
    request_id = request_id_of(requestId)
    owner = owner_of(ownerWorkspace, ownerLibrary, ownerSession)
    for identity, job in JOBS.items():
        if job.get("requestId") != request_id:
            continue
        # `scoped` raises on a foreign or incomplete scope; the loop only skips jobs that are not this caller's
        # (a legacy ownerless job is served to a caller that asserts nothing, by design).
        try:
            scoped(job, owner, identity)
        except HTTPException:
            continue
        return {"known": True, "id": identity, "state": job_state(job),
                "terminal": job_state(job) in (STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED),
                "elapsedSeconds": elapsed_seconds(job)}
    record = find_by_request_id(request_id, owner)
    if record is not None and record["id"] not in JOBS:
        return released_payload(record)
    return {"known": False, "id": None, "state": None, "terminal": False, "elapsedSeconds": None,
            "note": "a false answer describes this service instance's table, not proof that nothing was submitted"}


@app.post("/queries/acknowledge")
async def acknowledge_by_request(body: AcknowledgeRequest):
    """Register the intent to stop a request id this service has no job for **yet**.

    This is the race the review proved: a client's cleanup can arrive before the submission it is about - the
    admission response was lost, the job is not in the table yet, and the client has no id to address. Asking
    once and giving up leaves a running job with nobody holding its id, so the answer here records the intent
    and the *next* submission under that id honours it.

    It is deliberately a separate route from `POST /queries/{identity}/acknowledge`: that one acts on a job
    that exists, and this one records a fact about one that does not. Mixing them would make "there is no such
    job" and "stop it when it appears" the same call.
    """
    owner = request_scope(body)
    if not full_scope(owner):
        raise refusal(403, "QUERY_SCOPE_REQUIRED",
                      {"reason": "scope_incomplete",
                       "missing": [name for name in ("workspace", "libraryId", "session")
                                   if not (owner or {}).get(name)]})
    if not body.requestId or not REQUEST_ID_PATTERN.match(body.requestId):
        raise refusal(400, CODE_REQUEST_INVALID, {"reason": "request_id_invalid"})
    existing = find_by_request_id(body.requestId, owner)
    if existing is not None and existing["id"] not in JOBS:
        return released_payload(existing)
    if existing is not None and not body.cancelIntent:
        return await acknowledge(existing["id"], body)
    if existing is not None and body.cancelIntent:
        # The job appeared between the client's question and this call: honour the intent now.
        outcome = await shutdown_job(existing, max(1, int(existing.get("budgetSeconds") or 0)))
        settled = outcome["settled"]
        if settled:
            existing["acknowledged"] = True
            existing["released"] = True
            release_job(existing["id"], existing)
        return {"id": existing["id"], "known": True, "state": job_state(existing),
                "terminal": job_settled(existing) and existing.get("result") is not None, "acknowledged": bool(existing.get("acknowledged")),
                "released": bool(existing.get("released")), "cancellationConfirmed": settled and job_state(existing) == STATE_CANCELLED, "settled": settled,
                "result": existing.get("result")}
    recorded = register_intent(owner, body.requestId) is not None if body.cancelIntent else False
    return {"id": existing["id"] if existing else None, "known": existing is not None, "state": STATE_UNKNOWN,
            "terminal": False, "acknowledged": recorded, "released": False,
            "cancellationConfirmed": False, "cancelIntentRecorded": recorded}


@app.get("/queries/{identity}")
async def get_query(identity: str, ownerWorkspace: str | None = None, ownerLibrary: str | None = None,
                    ownerSession: str | None = None):
    if identity not in JOBS:
        record = completed_record(identity, owner_of(ownerWorkspace, ownerLibrary, ownerSession))
        if record is not None:
            return released_payload(record)
        raise HTTPException(status_code=404, detail="unknown query")
    job = scoped(JOBS[identity], owner_of(ownerWorkspace, ownerLibrary, ownerSession), identity)
    return build_state_payload(identity, job)


@app.post("/queries/{identity}/acknowledge")
async def acknowledge(identity: str, body: "AcknowledgeRequest | None" = None):
    """Confirm this job's outcome, or stop it - the two are different actions on the same job.

    A caller that has read a terminal result and a caller that wants to stop a running job arrive here with
    different intents, and the route keeps them apart:

    * **stop** (`cancelIntent: true`) - the job is shut down, and the answer says whether that was *observed*:
      `cancellationConfirmed` is true only after the worker, the container and the local preparation have all
      finished. A shutdown that cannot be observed answers `cancelling` with `settled: false`, and the job
      keeps holding the query slot rather than being reported as stopped.
    * **confirm** (no `cancelIntent`) - the job is released if it has a terminal result. A completed job keeps
      its result and is never rewritten as "cancelled"; that is what makes this different from `DELETE`.
    """
    request = body or AcknowledgeRequest()
    owner = request_scope(request)
    if identity not in JOBS:
        record = completed_record(identity, owner)
        if record is not None:
            return released_payload(record)
        # Either it never existed, or it was already acknowledged and released. Both are answered the same
        # way - as "nothing of yours is running here" - and neither is reported as a cancellation.
        if request.cancelIntent and request.requestId and REQUEST_ID_PATTERN.match(request.requestId):
            # The job is not here *now*. Record the intent so that a job admitted a moment later under this id
            # is stopped instead of left running: this is the race the review found, and the intent entry is
            # what closes it. It is itself a job record, so it is bounded by the same lease and capacity.
            holder = intent_holder(owner, request.requestId)
            return {"id": None, "known": False, "state": STATE_UNKNOWN, "terminal": False,
                    "acknowledged": True, "released": False, "cancellationConfirmed": False,
                    "cancelIntentRecorded": holder is not None}
        return {"id": identity, "known": False, "state": STATE_UNKNOWN, "terminal": False,
                "acknowledged": False, "released": False, "cancellationConfirmed": False,
                "alreadyRemoved": True}
    job = scoped(JOBS[identity], owner, identity)
    if request.cancelIntent:
        outcome = await shutdown_job(job, max(1, int(job.get("budgetSeconds") or 0)))
        settled = outcome["settled"]
        if settled:
            job["acknowledged"] = True
            job["released"] = True
            release_job(identity, job)
        return {"id": identity, "known": True, "state": job_state(job), "terminal": job_settled(job) and job.get("result") is not None,
                "acknowledged": bool(job.get("acknowledged")), "released": bool(job.get("released")),
                "cancellationConfirmed": settled and job_state(job) == STATE_CANCELLED, "settled": settled, "result": job.get("result")}
    if not job_settled(job) or job.get("result") is None:
        raise refusal(409, "QUERY_NOT_TERMINAL", {"reason": "writers_not_settled"})
    result = job["result"]
    job["acknowledged"] = True
    job["released"] = True
    release_job(identity, job)
    return {"id": identity, "known": True, "state": job_state(job), "terminal": True,
            "acknowledged": True, "released": True,
            "cancellationConfirmed": job_state(job) == STATE_CANCELLED, "result": result}


def intent_holder(owner, request_id):
    """Register "stop that request when it appears" and report whether it was accepted.

    Kept out of the job table on purpose: an intent that lived there would be visible to the idempotency
    lookup, and the later real submission would then look like a duplicate of the holder (a 409 instead of a
    cancellation). It is bounded by count and by age instead, and the submission that finds it consumes it.
    """
    return register_intent(owner, request_id) is not None


def diag_intent(job):
    """Record a honoured cancel intent as a stage event - the reason a late-admitted job ended."""
    try:
        diag = stage_diagnostics.open_for_job(job.get("id"), enabled=diagnostics_enabled(), mode=job.get("mode"))
        diag.stage("cancel_intent", job.get("id"), status="honoured", reason="CANCEL_INTENT_HONOURED")
    except Exception:  # pragma: no cover - observability must never fail the job it describes
        pass


@app.delete("/queries/{identity}")
async def cancel(identity: str, body: "CancelRequest | None" = None):
    """Stop a job and wait for its writers. Syntactic sugar over the acknowledge route's `cancelIntent`."""
    request = body or CancelRequest()
    owner = request_scope(request)
    if identity not in JOBS:
        # Cleanup/acknowledgement is idempotent even after acknowledged-result eviction.
        return {"id": identity, "result": None, "alreadyRemoved": True, "state": STATE_UNKNOWN,
                "terminal": False, "cancellationConfirmed": False, "known": False}
    job = scoped(JOBS[identity], owner, identity)
    outcome = await shutdown_job(job, max(1, int(job.get("budgetSeconds") or 0)))
    settled = outcome["settled"]
    if settled:
        job["acknowledged"] = True
        job["released"] = True
        release_job(identity, job)
    return {"id": identity, "state": job_state(job), "terminal": job_settled(job) and job.get("result") is not None,
            "cancellationConfirmed": settled and job_state(job) == STATE_CANCELLED, "settled": settled, "known": True,
            "alreadyRemoved": False, "result": job.get("result")}


class Read(BaseModel):
    """One bounded read of a file a citation already points at.

    There is deliberately no path field: the client identifies the file with the opaque `fileHandle`
    the search returned, and continues with the `cursor` the previous read returned. `startLine` is a
    convenience for reading around a citation's line range.
    """
    workspace: str
    libraryId: str
    fileHandle: str | None = Field(default=None, max_length=reader.MAX_TOKEN_CHARS)
    cursor: str | None = Field(default=None, max_length=reader.MAX_TOKEN_CHARS)
    sourceVersion: str | None = Field(default=None, max_length=128)
    startLine: int | None = Field(default=None, ge=1)
    maxChars: int | None = Field(default=None, ge=1)
    maxLines: int | None = Field(default=None, ge=1)
    overlapLines: int | None = Field(default=None, ge=0)
    #: An opaque caller-chosen key used only to group reads under one budget; stored hashed.
    session: str | None = Field(default=None, max_length=200)


def read_session_key(session, workspace):
    raw = (session or "").strip() or ("workspace:" + str(workspace))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _effective_char_request(body):
    value = body.maxChars if body.maxChars is not None else reader.DEFAULT_READ_CHARS
    return max(reader.MIN_READ_CHARS, min(reader.MAX_READ_CHARS, int(value)))


def perform_read(selected, body, catalogue=_UNSET):
    """Read one file of an already-authorized library. No engine, no model client, no worker.

    The two authorization checks the query path performs are repeated here rather than inherited: the
    calling workspace must still list this library (done by the route) and a real library must still
    pass its trial check, so revoking either takes effect on the next read. The library's own
    `maxFileBytes` is repeated too, for the same reason: a read is not a way around the ceiling the
    search applies, and a library configured to accept a large file must not be refused by the reader.
    """
    trial_warning = None
    if not selected.get("syntheticFixture", False):
        allowed, detail, trial_warning = real_data.authorize(selected, evidence.EXTENSIONS)
        if not allowed:
            raise HTTPException(403, detail)
    if catalogue is _UNSET:
        catalogue = legacy_catalogue_source()
    secret = reader.derive_secret(TOKEN)
    key = read_session_key(body.session, body.workspace)
    state, shrunk = {}, []

    def reserve(chars):
        state["lease"], budget = READ_SESSIONS.reserve(key, chars)
        if budget < chars:
            shrunk.append(budget)
        return budget

    result = reader.read(secret, body.workspace, body.libraryId, selected["root"],
                         handle=body.fileHandle, cursor=body.cursor, source_version=body.sourceVersion,
                         start_line=body.startLine, max_chars=_effective_char_request(body),
                         max_lines=body.maxLines or reader.DEFAULT_READ_LINES,
                         overlap_lines=body.overlapLines or 0, before_read=reserve,
                         max_file_bytes=capacity.resolve(selected))
    READ_SESSIONS.charge(state["lease"], len(result["excerpt"]))
    if shrunk:
        result["warnings"].append("READ_SESSION_BUDGET: the window was shortened to %d characters - the "
                                  "remainder of this session's read budget" % shrunk[0])
    if trial_warning:
        result["warnings"].append(trial_warning)
    result["limits"] = READ_SESSIONS.report(state["lease"])
    result["sourceCard"] = read_source_card(result, catalogue, selected)
    from markdown_windows import quality_hints
    result["contentWarnings"] = quality_hints(result.get("excerpt"))
    result["sourceCard"]["contentWarnings"] = result["contentWarnings"]
    return result


def read_source_card(result, catalogue=_UNSET, selected=None):
    """The card for one read window. A read is not a citation, so the origin says `read`.

    The location check comes from `reader.read`, which derived it from the exact bytes it verified
    against the source version; nothing is re-derived here from a second read that could see a different
    version of the file. The page is resolved against the catalogue's `pageMap` when there is one, and
    only falls back to the in-text marker (which `reader.read` already computed) otherwise - a marker is
    something the document can contain, so it is never the authoritative answer.
    """
    if catalogue is _UNSET:
        catalogue = legacy_catalogue_source()
    record = catalogue_records(catalogue, (selected or {}).get("id"),
                               (selected or {}).get("root") or "").get(str(result.get("relativePath")), {})
    notices = []
    # Three cases, kept apart on purpose (see `reader.read` for the same split in the model-facing warnings):
    # the window was cut while more content exists; it reached the end of the file without the sentence-boundary
    # check recognizing an ending; or it reached the end and closed a sentence. Only the first is a *truncation*
    # of the window - the second says something about the extracted text, and saying "truncated" there would
    # describe a text property as a window property.
    more = bool(result.get("hasMore"))
    if result.get("excerpt") and not result.get("excerptComplete"):
        notices.append("EXCERPT_TRUNCATED" if more else "EXCERPT_INCOMPLETE_AT_EOF")
    if result.get("midLineStop"):
        notices.append("READ_LONG_LINE")
    if result.get("lineLimitReached"):
        notices.append("READ_LINE_LIMIT")
    relative = str(result.get("relativePath") or "")
    # `reader.read` resolved this page by scanning the text for the extraction's marker, which is a hint
    # and not an authority: hand it over as the hint and let the record decide whether a page can be
    # reported at all.
    located = source_card.location(result.get("startLine"), result.get("endLine"),
                                   result.get("startChar"), result.get("endChar"),
                                   record=record, hint=result.get("pdfPage"))
    # One page number per response: the authoritative one is published on the window too, so a caller
    # reading `pdfPage` directly and a caller reading the card cannot disagree.
    result["pdfPage"] = located["pdfPage"]
    result["pageSource"] = located["pageSource"]
    result["pageHint"] = located["pageHint"]
    return source_card.card(
        library_id=result.get("libraryId"),
        file_name=record.get("fileName") or (relative.rsplit("/", 1)[-1] or None),
        excerpt=result.get("excerpt"),
        location_fields=located,
        source_version=result.get("sourceVersion"), citation_origin="read",
        truncated=bool(result.get("excerpt")) and (bool(result.get("midLineStop"))
                                                  or bool(result.get("lineLimitReached"))
                                                  or (not result.get("excerptComplete") and more)),
        truncation_notices=notices,
        parse_quality=record.get("quality"),
        identity=record.get("verification") or source_card.IDENTITY_NOT_ASSESSED,
        identity_detail=record.get("verificationDetail"),
        conflicts=record.get("conflicts") or None,
        location_verified=bool(result.get("locationVerified")),
        title=record.get("titleRaw"), document_id=record.get("documentId"),
        relative_path=relative or None)


@app.post("/reads")
async def read_original(body: Read):
    config = library_config()
    selected = next((lib for lib in libraries(body.workspace, config) if lib["id"] == body.libraryId), None)
    if not selected:
        raise HTTPException(403, "Unknown library for workspace")
    try:
        return perform_read(selected, body, catalogue_source(selected, config))
    except reader.ReadError as error:
        return read_refusal(error)


#: The read route's own field names. A schema refusal may name a parameter only if it is in this set, so
#: the reported name is a member of this route's schema rather than whatever string arrived.
READ_FIELD_NAMES = ("workspace", "libraryId", "fileHandle", "cursor", "sourceVersion", "startLine",
                    "maxChars", "maxLines", "overlapLines", "session")


def new_correlation_id():
    """The one id for one refusal, in the shape the plugin's own references use.

    The plugin validates a reference against `k-[0-9a-z]{8}` before it displays one, so an id minted here in
    that exact shape is what lets the card show *this* id instead of inventing a second one that no service
    log line could be matched against.
    """
    return "k-" + uuid.uuid4().hex[:8]


def _refusal_line(body):
    """One structured log line per refusal: closed-set tokens and numbers, never the sentence or an id."""
    try:
        print("READ_REFUSAL " + json.dumps(body, sort_keys=True), file=sys.stderr, flush=True)
    except (TypeError, ValueError):  # pragma: no cover - the body is JSON-safe by construction
        pass


def read_refusal(error):
    """One refused `/reads`, as a body both generations of caller can read.

    Two audiences, and this is why the body has two halves:

    * `detail` stays the string `"CODE: sentence"` it has always been. An older plugin parses exactly that
      and folds the code into a generic refusal - a *recognised* failure, which is the compatibility that
      has to hold;
    * `error` is the structured half a newer caller reads: the code, the fixed `reason`/`suggestedAction`
      members and the numbers, plus the correlation id. Every field is a closed-set token or an integer (or
      null), so nothing here can reflect a path, a handle, a request value or an exception message.

    The sentence itself is this service's own fixed English text, never an exception's, and it already
    contains no path or identifier.
    """
    facts = dict(error.facts or {})
    body = {"code": error.code, **facts, "correlationId": new_correlation_id()}
    _refusal_line(body)
    return JSONResponse(status_code=error.status, content={"detail": str(error), "error": body})


@app.exception_handler(RequestValidationError)
async def schema_refusal(request, exc):
    """A schema violation on `/reads`, described without echoing the value that violated it.

    FastAPI's default 422 body carries the offending `input` verbatim. For a read that is exactly the
    reflection `reader.read_facts` refuses to make, so this route's body is replaced by the same closed-set
    shape a range refusal uses: the parameter name (a member of this route's own schema) and
    `reason: PARAMETER_TYPE_INVALID`, with no line facts (no file was opened) and no copy of what arrived.

    Any other path keeps FastAPI's default body, unchanged: nothing there reads a library file, and their
    tests pin the status code they expect.
    """
    if request.url.path != "/reads":
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
    names = []
    for item in exc.errors():
        location = item.get("loc") or ()
        name = location[-1] if location else None
        if isinstance(name, str) and name in READ_FIELD_NAMES and name not in names:
            names.append(name)
    body = {"code": reader.READ_REQUEST_INVALID, "reason": reader.READ_REASON_PARAMETER_TYPE_INVALID,
            "requestedStartLine": None, "fileLines": None,
            "suggestedAction": reader.READ_ACTION_CORRECT_PARAMETER,
            "parameter": names[0] if names else None,
            "correlationId": new_correlation_id()}
    _refusal_line(body)
    return JSONResponse(status_code=422, content={
        "detail": "%s: the request did not match this route's schema" % reader.READ_REQUEST_INVALID,
        "error": body})


class FindDocuments(BaseModel):
    """Locate a library document from a title, author, DOI or file-name clue.

    This is a *librarian* call, not a search: it never starts the engine and never builds a model
    client, and a hit means "this file is in the library and can be read", not "this text answers the
    question". The caller still has to read the original through `knowledge_read`.
    """
    workspace: str
    libraryId: str
    doi: str | None = Field(default=None, max_length=catalog.MAX_QUERY_CHARS)
    title: str | None = Field(default=None, max_length=catalog.MAX_QUERY_CHARS)
    authors: str | None = Field(default=None, max_length=catalog.MAX_QUERY_CHARS)
    year: str | None = Field(default=None, max_length=8)
    fileName: str | None = Field(default=None, max_length=catalog.MAX_QUERY_CHARS)
    limit: int | None = Field(default=None, ge=1, le=catalog.MAX_CANDIDATES)
    session: str | None = Field(default=None, max_length=200)


def _catalogue_source_for(selected, catalogue):
    """The `(path, require_attribution)` pair for a librarian call, or an HTTP refusal.

    A library with no usable catalogue refuses rather than answering from a global one. That refusal is
    the fix for the multi-library defect: the older code read the process-wide catalogue for whichever
    library asked, so a second library was handed the first library's titles and file names, marked
    `readable: false` because those files are not in it. A refusal names the missing configuration; the
    old behaviour named the wrong library's documents.
    """
    if catalogue is _UNSET:
        catalogue = legacy_catalogue_source()
    if not catalogue or not catalogue[0]:
        raise HTTPException(503, catalog.CATALOG_UNAVAILABLE + ": this library declares no catalogue")
    return catalogue


def perform_find(selected, body, catalogue=_UNSET):
    """Ranked candidates from the local catalogue, each verified before it gets a handle.

    Authorization mirrors the read path: the workspace must list the library (the route checks that) and
    a real-data library must still pass its trial check. Every candidate is then re-resolved against the
    library - inside its root, under its own size ceiling, hashing to the recorded version - so a stale,
    forged or out-of-library catalogue entry is refused rather than acted on.
    """
    if not selected.get("syntheticFixture", False):
        allowed, detail, _ = real_data.authorize(selected, evidence.EXTENSIONS)
        if not allowed:
            raise HTTPException(403, detail)
    path, require_attribution = _catalogue_source_for(selected, catalogue)
    limit = capacity.resolve(selected)
    try:
        entries, _version = catalog.load_bound(path, selected["id"], selected["root"], require_attribution)
        query = {"doi": body.doi, "title": body.title, "authors": body.authors,
                 "year": body.year, "fileName": body.fileName}
        ranked = catalog.find(entries, query, body.limit)
    except catalog.CatalogError as error:
        raise HTTPException(error.status, str(error)) from None
    issuer = reader.Issuer(reader.derive_secret(TOKEN), body.workspace, body.libraryId)
    documents, warnings = [], []
    for item in ranked:
        record = item["record"]
        window, refusal = catalog.verify_text_file(selected, record, issuer, entries, max_file_bytes=limit)
        document = {
            "documentId": item["documentId"],
            "title": record.get("titleRaw"),
            "titleNormalized": record.get("titleNormalized"),
            "authors": record.get("authors"), "year": record.get("year"), "doi": record.get("doi"),
            "fileName": record.get("fileName"), "textFile": record.get("textFile"),
            "pages": record.get("pages"), "quality": record.get("quality"),
            "verification": record.get("verification"), "conflicts": record.get("conflicts") or [],
            "score": item["score"], "matched": item["matched"], "mismatched": item["mismatched"],
        }
        if window:
            document.update({"readable": True, "fileHandle": window["fileHandle"],
                             "sourceVersion": window["sourceVersion"]})
        else:
            document.update({"readable": False, "refusal": refusal.get("code"),
                             "refusalDetail": refusal.get("detail")})
            warnings.append("%s: %s" % (refusal.get("code"), refusal.get("detail")))
        # A card even when nothing is readable: identity, provenance and parse quality are known
        # without a read, and they are what tells a reader whether the mapping is trustworthy. There
        # is deliberately no location and no excerpt - this call has not read anything yet - and the
        # support axis stays `not-assessed` for the same reason.
        document["sourceCard"] = source_card.card(
            library_id=body.libraryId, file_name=record.get("fileName"),
            excerpt=None, location_fields=None, source_version=document.get("sourceVersion"),
            citation_origin="catalogue", parse_quality=record.get("quality"),
            identity=record.get("verification") or source_card.IDENTITY_NOT_ASSESSED,
            identity_detail=record.get("verificationDetail"),
            conflicts=record.get("conflicts") or None, location_verified=False,
            title=record.get("titleRaw"), document_id=document["documentId"],
            relative_path=record.get("textFile"))
        if not window:
            document["sourceCard"]["truncationNotices"] = ["DOCUMENT_NOT_READABLE"]
            document["sourceCard"]["truncated"] = True
        documents.append(document)
    if not documents:
        warnings.append("NO_CATALOG_MATCH: no document in this library matches the clue; it is a location "
                        "miss, not evidence that the library lacks the material")
    return {"libraryId": body.libraryId, "documents": documents, "warnings": warnings,
            "note": "locating a document is not evidence: read the original before answering content questions"}


@app.post("/documents")
async def find_documents(body: FindDocuments):
    config = library_config()
    selected = next((lib for lib in libraries(body.workspace, config) if lib["id"] == body.libraryId), None)
    if not selected:
        raise HTTPException(403, "Unknown library for workspace")
    return perform_find(selected, body, catalogue_source(selected, config))


class Catalogue(BaseModel):
    """One page of a library's document catalogue.

    The enumeration counterpart of {@link FindDocuments}: it answers "which documents exist", not "which
    document matches this clue". It is still a librarian call - no engine, no model client, no job directory -
    and its order is fixed by the catalogue, so `offset` means the same thing on every call.

    `catalogueVersion` is what makes paging safe. Supplying the version the previous page reported turns "the
    catalogue changed underneath this listing" into a refusal a caller can see, instead of a page that quietly
    skipped or repeated entries.
    """

    workspace: str
    libraryId: str
    offset: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1, le=catalog.MAX_PAGE_SIZE)
    #: The `catalogueVersion` a previous page reported. Compared, never trusted as authority.
    catalogueVersion: str | None = Field(default=None, max_length=128)
    session: str | None = Field(default=None, max_length=200)


def perform_catalogue(selected, body, catalogue=_UNSET):
    """A deterministic page of the catalogue, with every readable entry re-verified against the library.

    Authorization mirrors the read and locate paths: the workspace must still list the library (the route
    checks that) and a real-data library must still pass its trial check. Every entry that gets a handle is
    re-resolved inside the library first - including its own `maxFileBytes` - so a stale or forged catalogue
    entry is listed as unreadable rather than acted on, and the handle is re-checked all over again when it
    is actually used to read.
    """
    if not selected.get("syntheticFixture", False):
        allowed, detail, _ = real_data.authorize(selected, evidence.EXTENSIONS)
        if not allowed:
            raise HTTPException(403, detail)
    path, require_attribution = _catalogue_source_for(selected, catalogue)
    limit = capacity.resolve(selected)
    try:
        entries, version = catalog.load_bound(path, selected["id"], selected["root"], require_attribution)
        page = catalog.enumerate_catalog(entries, body.offset or 0, body.limit)
    except catalog.CatalogError as error:
        raise HTTPException(error.status, str(error)) from None

    if body.catalogueVersion and body.catalogueVersion != version:
        # The listing this caller is part-way through no longer exists. Returning the *new* page here would be
        # the silent skip the version exists to prevent: the caller's offset was computed against a different
        # order. So: no entries, the new version, and an instruction to start again.
        return {"libraryId": body.libraryId, "catalogueVersion": version, "status": "catalogue-changed",
                "total": page["total"], "offset": page["offset"], "limit": page["limit"], "returned": 0,
                "hasMore": page["total"] > 0, "nextOffset": 0, "entries": [],
                "warnings": ["%s: the catalogue changed while it was being listed, so this page was not "
                             "produced; restart from offset 0 and page through again" % catalog.CATALOGUE_CHANGED],
                "note": "a catalogue entry is a location, not evidence: read the original before answering "
                        "content questions"}

    issuer = reader.Issuer(reader.derive_secret(TOKEN), body.workspace, body.libraryId)
    documents, warnings = [], []
    for position, item in enumerate(page["entries"]):
        record = item["record"]
        window, refusal = catalog.verify_text_file(selected, record, issuer, entries, max_file_bytes=limit)
        document = {
            "index": page["offset"] + position,
            "documentId": item["documentId"],
            "title": record.get("titleRaw"), "titleNormalized": record.get("titleNormalized"),
            "authors": record.get("authors"), "year": record.get("year"), "doi": record.get("doi"),
            "fileName": record.get("fileName"), "textFile": record.get("textFile"),
            "pages": record.get("pages"), "quality": record.get("quality"),
            "verification": record.get("verification"), "conflicts": record.get("conflicts") or [],
        }
        if window:
            document.update({"readable": True, "fileHandle": window["fileHandle"],
                             "sourceVersion": window["sourceVersion"]})
        else:
            document.update({"readable": False, "refusal": refusal.get("code")})
            warnings.append("%s: %s" % (refusal.get("code"), refusal.get("detail")))
        # Identity and provenance are known without reading; there is deliberately no location and no excerpt,
        # and the support axis stays `not-assessed`, because listing a document reads nothing out of it.
        document["sourceCard"] = source_card.card(
            library_id=body.libraryId, file_name=record.get("fileName"),
            excerpt=None, location_fields=None, source_version=document.get("sourceVersion"),
            citation_origin="catalogue", parse_quality=record.get("quality"),
            identity=record.get("verification") or source_card.IDENTITY_NOT_ASSESSED,
            identity_detail=record.get("verificationDetail"),
            conflicts=record.get("conflicts") or None, location_verified=False,
            title=record.get("titleRaw"), document_id=document["documentId"],
            relative_path=record.get("textFile"))
        if not window:
            document["sourceCard"]["truncationNotices"] = ["DOCUMENT_NOT_READABLE"]
            document["sourceCard"]["truncated"] = True
        documents.append(document)

    if page["offset"] > 0 and not body.catalogueVersion:
        # A continuation that carried no version cannot be checked: say so rather than implying the pages join.
        warnings.append("CATALOGUE_UNVERIFIED_CONTINUATION: no catalogueVersion was supplied, so this page "
                        "could not be checked against the one before it")
    if page["total"] == 0:
        warnings.append("CATALOGUE_EMPTY: this library's catalogue records no documents")
    return {"libraryId": body.libraryId, "catalogueVersion": version, "status": "ok",
            "total": page["total"], "offset": page["offset"], "limit": page["limit"],
            "returned": page["returned"], "hasMore": page["hasMore"], "nextOffset": page["nextOffset"],
            "entries": documents, "warnings": warnings,
            "note": "catalogue coverage is `total` documents; a listed entry is a location, not evidence, and "
                    "its handle was verified against the library when this page was built"}


@app.post("/catalogue")
async def list_catalogue(body: Catalogue):
    config = library_config()
    selected = next((lib for lib in libraries(body.workspace, config) if lib["id"] == body.libraryId), None)
    if not selected:
        raise HTTPException(403, "Unknown library for workspace")
    return perform_catalogue(selected, body, catalogue_source(selected, config))

# The browser calls through the authenticated host bridge. There is deliberately no model tool for this.
ONBOARDING = onboarding.Manager(CONFIG)
class LibraryAdmin(BaseModel):
    model_config = {"extra": "forbid"}
    workspace: str = Field(min_length=1, max_length=2048)
    session: str = Field(min_length=1, max_length=128)
    action: Literal["list", "preview", "publish", "cancel", "remove"]
    root: str | None = Field(default=None, max_length=2048)
    name: str | None = Field(default=None, max_length=120)
    libraryId: str | None = Field(default=None, max_length=128)
    previewId: str | None = Field(default=None, max_length=80)
    consent: bool = Field(default=False, strict=True)

@app.post("/library-admin")
async def library_admin(body: LibraryAdmin):
    try:
        visible = libraries(body.workspace)
        if body.action == "list":
            result = []
            for lib in visible:
                count = None
                try:
                    source = catalogue_source(lib, library_config())
                    if source:
                        docs, _ = catalog.load_bound(source[0], lib["id"], lib["root"], require_attribution=source[1])
                        count = len(docs)
                except Exception:
                    pass
                result.append({"id": lib["id"], "name": lib["name"], "documents": count,
                               "state": "ready" if count is not None else "unavailable",
                               "updateAvailable": bool(lib.get("sourceRoot"))})
            # Non-sensitive effective provider identity; never serialize credentials or raw endpoint paths.
            from urllib.parse import urlparse
            host = urlparse(os.environ.get("LLM_BASE_URL", "")).hostname
            return {"libraries": result, "engineProvider": host or "not-configured",
                    "agentProvider": "CURRENT_DSH_CONVERSATION_MODEL"}
        if body.libraryId and not any(lib["id"] == body.libraryId for lib in visible):
            raise onboarding.Refused("LIBRARY_NOT_VISIBLE")
        if body.action == "remove":
            return await asyncio.to_thread(ONBOARDING.unregister, body.libraryId, body.workspace, body.consent)
        if body.action == "preview":
            if body.root is None and body.libraryId is None:
                raise onboarding.Refused("FOLDER_INVALID")
            return await asyncio.to_thread(ONBOARDING.preview, body.workspace, body.session,
                                           body.root, body.libraryId, body.name)
        if body.action == "publish":
            return await asyncio.to_thread(ONBOARDING.publish, body.previewId, body.workspace, body.session, body.consent)
        return ONBOARDING.cancel(body.previewId, body.workspace, body.session)
    except onboarding.Refused as error:
        return JSONResponse({"code": error.code}, status_code=409)
    except (OSError, ValueError, TypeError):
        return JSONResponse({"code": "LOCAL_BUILD_FAILED"}, status_code=400)
