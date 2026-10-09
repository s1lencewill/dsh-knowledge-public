"""Synthetic HTTP service for the real-transport lifecycle acceptance. No model, no container, no network.

What this is: the **real** `engine/service.py` served by a real uvicorn on a real loopback port, with two
things replaced so the acceptance can run offline and deterministically:

* `build_snapshot` sleeps for `SYNTHETIC_PREP_MS` - a controlled slow preparation, which is the stage the
  client's request bound used to expire on;
* the worker is never spawned. `asyncio.create_subprocess_exec` is replaced by a stub that writes a result
  file and records a counter, so "no second worker was started" is a number rather than an inference.

It also adds a tiny `/synthetic/stats` route (a roll-up of those counters and a note that nothing is
forwarded) and a one-shot admission-response loss for the recovery test. Neither is part of the product
surface: both are guarded by the environment variables this script sets, and the service module refuses to
install them unless they are present.

`SYNTHETIC_PORT=<port>` is printed on stdout once the port is bound, which is what the test waits for.
"""
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
TOKEN = os.environ["SYNTHETIC_TOKEN"]
CONFIG = os.environ["SYNTHETIC_CONFIG"]
STATE = Path(os.environ["SYNTHETIC_STATE"])
PREP_MS = int(os.environ.get("SYNTHETIC_PREP_MS", "0"))
FAIL_ADMIT_ONCE = os.environ.get("SYNTHETIC_FAIL_ADMIT_ONCE", "0") == "1"
WORKER = os.environ.get("SYNTHETIC_WORKER", "ok")

os.environ["DSH_KNOWLEDGE_TOKEN"] = TOKEN
os.environ["DSH_KNOWLEDGE_CONFIG"] = CONFIG
os.environ["DSH_KNOWLEDGE_ISOLATION"] = "off"
os.environ.setdefault("DSH_KNOWLEDGE_BUDGET_FAST", os.environ.get("SYNTHETIC_BUDGET_FAST", "90"))
os.environ.setdefault("DSH_KNOWLEDGE_BUDGET_DEEP", os.environ.get("SYNTHETIC_BUDGET_DEEP", "240"))
os.environ["DSH_KNOWLEDGE_DIAGNOSTICS"] = "off"
# The service's event loop must be the stdlib one: the acceptance asserts that a blocking preparation does not
# hold the loop, and a C-compiled loop implementation would make the failure mode unreproducible.
os.environ["UVICORN_LOOP"] = "asyncio"

import service  # noqa: E402  (import after the environment is set)
from fastapi.responses import JSONResponse  # noqa: E402  (same reason: it lives beside the app)

COUNTERS = {"admissions": 0, "workersStarted": 0, "providerCalls": 0, "cancelled": 0, "forwarded": 0,
            "prepStarted": 0, "prepFinished": 0, "authStarted": 0, "authFinished": 0, "authAllowed": 0}


_state_lock = __import__("threading").RLock()

def write_state():
    """Publish the counters atomically: a reader must never see a half-written file.

    `write_text` truncates and then writes, so a concurrent reader can observe an empty file. The acceptance
    reads this file from another process, and a parse error there would be a flake in the test rather than a
    fact about the service - so the write goes to a temporary name and is renamed into place.
    """
    with _state_lock:
        temporary = STATE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(COUNTERS, sort_keys=True), encoding="utf-8")
        os.replace(temporary, STATE)


_original_authorize = service.real_data.authorize
_auth_ms = int(os.environ.get("SYNTHETIC_AUTH_MS", "0"))

def delayed_real_authorize(*args):
    COUNTERS["authStarted"] += 1
    write_state()
    time.sleep(_auth_ms / 1000)
    result = _original_authorize(*args)
    COUNTERS["authFinished"] += 1
    COUNTERS["authAllowed"] += int(result[0])
    write_state()
    return result

service.real_data.authorize = delayed_real_authorize
_original_to_thread = asyncio.to_thread
async def diagnostic_thread(*args, **kwargs):
    try:
        return await _original_to_thread(*args, **kwargs)
    except Exception:
        __import__("traceback").print_exc()
        raise
service.asyncio.to_thread = diagnostic_thread


_original_snapshot = service.build_snapshot

def real_build_snapshot(*args, **kwargs):
    COUNTERS["prepStarted"] += 1
    write_state()
    if PREP_MS > 0:
        time.sleep(PREP_MS / 1000.0)
    COUNTERS["prepFinished"] += 1
    write_state()
    # One supported file: enough for the pipeline to have something to finalize, and the worker stub then
    # produces the result instead of the engine.
    return _original_snapshot(*args, **kwargs)


class StubProcess:
    """The stand-in worker: it writes a result and records that it ran."""

    def __init__(self, work_dir):
        self.work_dir = work_dir
        self.pid = 0
        self.returncode = 0

    async def communicate(self, data=None):
        COUNTERS["workersStarted"] += 1
        write_state()
        await asyncio.sleep(0.05)
        if WORKER == "fail":
            self.returncode = 1
            return b"", b""
        relative = "note.md"
        copy = Path(self.work_dir) / "files" / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        if not copy.exists():
            copy.write_text("# note\nWALNUT\nsecond line.\n", encoding="utf-8")
        payload = {"status": "partial", "answer": "", "warnings": [],
                   "candidates": [{"file": str(copy.resolve()), "snippets": ["WALNUT"]}]}
        return b"DSH_KNOWLEDGE_RESULT=" + json.dumps(payload).encode() + b"\n", b""

    def kill(self):
        pass

    async def wait(self):
        return 0


async def stubbed_exec(*args, **kwargs):
    # The work directory is the last `job-...` path argument the service passed.
    work_dir = next((arg for arg in reversed(args) if isinstance(arg, str) and "job-" in arg), os.getcwd())
    return StubProcess(work_dir)


service.build_snapshot = real_build_snapshot
service.asyncio.create_subprocess_exec = stubbed_exec

_admissions = {"count": 0}


def note_admission():
    _admissions["count"] += 1
    COUNTERS["admissions"] = _admissions["count"]
    write_state()


original_shutdown = service.shutdown_job
_loss_left = [1 if os.environ.get("SYNTHETIC_FAIL_ADMIT_ONCE") == "1" else 0]


@service.app.middleware("http")
async def count_and_lose_one_admission(request, call_next):
    """Count every admission, and make at most one of their responses unusable.

    The route is called first, so the job exists and its preparation is under way when the response is
    replaced - which is exactly what the client's recovery has to find. Counting here rather than by wrapping
    the route function is deliberate: the route object holds the original function, so a module attribute
    assignment would count nothing.
    """
    if request.method == "POST" and request.url.path == "/queries":
        note_admission()
    response = await call_next(request)
    if _loss_left[0] and request.method == "POST" and request.url.path == "/queries":
        _loss_left[0] = 0
        # The job exists and its preparation is under way; the client is told the admission failed. A real 500
        # rather than a mangled 200, so the assertion is about a *service* error the client recovers from.
        return JSONResponse(status_code=500, content={"detail": "SYNTHETIC_ADMISSION_RESPONSE_LOST"})
    return response


async def counting_shutdown(job, timeout):
    outcome = await original_shutdown(job, timeout)
    if outcome.get("settled"):
        COUNTERS["cancelled"] += 1
        write_state()
    return outcome


service.shutdown_job = counting_shutdown


@service.app.get("/synthetic/stats")
async def synthetic_stats():
    """The counters the acceptance reads, plus the one fact that matters about this process: it forwards nothing."""
    COUNTERS["providerCalls"] = 0
    COUNTERS["forwarded"] = 0
    return dict(COUNTERS)


async def main():
    import uvicorn
    config = uvicorn.Config(service.app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.05)
    servers = getattr(server, "servers", [])
    port = servers[0].sockets[0].getsockname()[1] if servers and servers[0].sockets else None
    if port is None:
        raise SystemExit("SYNTHETIC_PORT_UNKNOWN")
    print("SYNTHETIC_PORT=%d" % port, flush=True)
    write_state()
    while thread.is_alive():
        time.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
