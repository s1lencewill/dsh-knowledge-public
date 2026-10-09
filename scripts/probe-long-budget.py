"""Controllable long-budget probe: the real service and the real AppContainer, at the real budgets.

The provider is replaced **in this process**, so nothing is sent anywhere and no token is spent - but
everything else is production: the same uvicorn service, the same single job deadline, the same model
pipe, the same container launch and termination. That is what makes the deadline races observable:
a reply that lands just inside the budget, a reply that never arrives, a provider that fails
mid-call, an engine killed while the parent is mid-answer, and a cancel well before the deadline.

    python scripts/probe-long-budget.py [--modes FAST,DEEP]

Checks per scenario: end-to-end elapsed against the real budget, no provider call started after the
deadline, the container tree gone, no process mentioning the job directory, the job directory removed,
exactly one terminal state (no overwriting, no double completion) and the query slot released.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "engine"))
LOGS = BASE / ".local/verification/long-budget"
TOKEN = "synthetic-long-budget-token-0123456789"
AUTH = {"Authorization": "Bearer " + TOKEN}
#: How many times a job was ended by a terminal(...) or finalize(...) call. The race this round is
#: about is a deadline arriving while a result is being written: the job must end exactly once.
COUNTERS = {"terminal": 0, "finalize": 0}


class StubProvider:
    """Stands in for the provider call. Records when each call started so the deadline can be checked."""

    def __init__(self):
        self.lock = threading.Lock()
        self.delays = []
        self.error_at = None
        self.error = None
        self.calls = 0
        self.started = []
        self.finished = []

    def plan(self, delays, error_at=None, error=None):
        with self.lock:
            self.delays, self.error_at, self.error = list(delays), error_at, error
            self.calls, self.started, self.finished = 0, [], []

    async def __call__(self, messages, options, model, remaining):
        with self.lock:
            index = self.calls
            self.calls += 1
            delay = self.delays[index] if index < len(self.delays) else (self.delays[-1] if self.delays else 0)
            failing = self.error is not None and (self.error_at == "always" or self.error_at == index)
            error = self.error if failing else None
        self.started.append(time.monotonic())
        await asyncio.sleep(delay)
        if error is not None:
            raise error
        self.finished.append(time.monotonic())
        # A reply the parent can turn into a citation-shaped result: a JSON answer plus a snippet.
        return {"content": json.dumps({"answer": "note-03.md", "snippet": "WALNUT-03"}),
                "usage": {"total_tokens": 12}, "model": model, "finish_reason": "stop"}


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def live_processes():
    listing = subprocess.run(["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"],
                             capture_output=True, text=True, timeout=60)
    table = []
    for line in listing.stdout.splitlines():
        parts = line.split(",")
        if len(parts) < 3 or not parts[-1].strip().isdigit():
            continue
        table.append((int(parts[-1]), ",".join(parts[1:-1])))
    return table


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--modes", default="FAST", help="real default budgets to exercise (FAST/DEEP)")
    parser.add_argument("--scenarios", default="all",
                        help="comma-separated subset of near,across,failure,engine-dies,cancel")
    arguments = parser.parse_args()
    import httpx
    import uvicorn

    # The service reads its token and config at import time, so the environment comes first.
    os.environ.update({"DSH_KNOWLEDGE_TOKEN": TOKEN,
                       "DSH_KNOWLEDGE_CONFIG": str(BASE / ".local/libraries.json"),
                       # Values the stub ignores; the provider must still be *configured* to exist.
                       "LLM_BASE_URL": "https://stub.invalid/v1", "LLM_MODEL_NAME": "stub-model",
                       "LLM_API_KEY": "stub-key-not-used"})
    os.environ.pop("DSH_KNOWLEDGE_ISOLATION", None)

    import model_proxy
    import service

    LOGS.mkdir(parents=True, exist_ok=True)
    stub = StubProvider()
    model_proxy.ModelProxy._provider_call = lambda self, messages, options, model, remaining: stub(
        messages, options, model, remaining)

    # Count the ways a job can end, so "exactly one terminal state" is measurable, not assumed.
    original_terminal, original_finalize = service.terminal, service.finalize

    def terminal(status, warning):
        COUNTERS["terminal"] += 1
        return original_terminal(status, warning)

    def finalize(*args, **kwargs):
        COUNTERS["finalize"] += 1
        return original_finalize(*args, **kwargs)

    service.terminal, service.finalize = terminal, finalize

    port = free_port()
    url = "http://127.0.0.1:%d" % port
    config = uvicorn.Config(service.app, host="127.0.0.1", port=port, log_level="warning",
                            access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    print("LONG_BUDGET_START %s budget=%d" % (url, service.budget_seconds("FAST")), file=sys.stderr, flush=True)

    report = {"budgets": {mode: service.budget_seconds(mode) for mode in ("FAST", "DEEP")},
              "scenarios": []}
    runners = {"near": run_near_deadline, "across": run_across_deadline,
               "failure": run_provider_failure, "engine-dies": run_engine_dies_mid_call,
               "cancel": run_cancel_before_deadline}
    wanted = list(runners) if arguments.scenarios == "all" else arguments.scenarios.split(",")
    missing = [name for name in wanted if name not in runners]
    if missing:
        raise SystemExit("unknown scenario(s): %s (known: %s)" % (missing, ", ".join(runners)))
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            for _ in range(60):
                try:
                    if client.get(url + "/health", headers=AUTH).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                print("the service never became healthy; see the log above", file=sys.stderr, flush=True)
                return 2
            report["health"] = client.get(url + "/health", headers=AUTH).json()
            print("LONG_BUDGET_HEALTH " + json.dumps(report["health"]), file=sys.stderr, flush=True)

            for mode in arguments.modes.split(","):
                for scenario_name in wanted:
                    facts = runners[scenario_name](client, url, stub, service, mode)
                    report["scenarios"].append(facts)
                    # Progress on stderr so a long run is observable while it happens.
                    print("LONG_BUDGET " + json.dumps({key: facts[key] for key in
                                                       ("name", "mode", "budgetSeconds", "elapsedSeconds",
                                                        "status", "providerCalls", "terminalCalls",
                                                        "finalizeCalls", "containerPidAlive",
                                                        "workDirRemoved", "slotReleased")},
                                                      sort_keys=True), file=sys.stderr, flush=True)
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        service.terminal, service.finalize = original_terminal, original_finalize

    _score(report, service)
    (LOGS / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"passed": report["passed"],
                      "checks": {name: value for name, value in report["checks"].items()},
                      "scenarios": [{k: v for k, v in s.items() if k != "calls"} for s in report["scenarios"]]},
                     ensure_ascii=False))
    return 0 if report["passed"] else 1


# -- scenario plumbing ------------------------------------------------------------------------------
def submit(client, url, mode="FAST", query="Which note contains the codename WALNUT-03?"):
    body = {"workspace": str(BASE), "libraryId": "fixtures", "query": query, "mode": mode}
    response = client.post(url + "/queries", json=body, headers=AUTH)
    if response.status_code != 200:
        raise AssertionError("submit refused with %d: %s" % (response.status_code, response.text[:200]))
    return response.json()["id"]


def wait_for_result(client, url, identity, limit):
    started = time.monotonic()
    while time.monotonic() - started < limit:
        result = client.get(url + "/queries/" + identity, headers=AUTH).json().get("result")
        if result:
            return result, time.monotonic() - started
        time.sleep(0.2)
    return None, time.monotonic() - started


def observe_job(service, identity, seconds, kill_at=None, cancel_at=None, client=None, url=None, mode="FAST"):
    """Watch one job: record the container pid, optionally kill it or cancel, then collect the facts."""
    job = service.JOBS[identity]
    started, pid, killed, cancelled = time.monotonic(), None, False, False
    while time.monotonic() - started < seconds:
        if pid is None and job.get("container") is not None:
            pid = getattr(job["container"], "pid", None)
        elapsed = time.monotonic() - started
        if cancel_at is not None and not cancelled and elapsed >= cancel_at:
            cancelled = True
            client.delete(url + "/queries/" + identity, headers=AUTH)
        if kill_at is not None and not killed and elapsed >= kill_at and pid:
            killed = True
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        if job["task"].done():
            break
        time.sleep(0.1)
    return {"pid": pid, "killed": killed, "cancelled": cancelled, "job": job}


def slot_released(client, url):
    """Prove the running-job slot is free: a new query must be *accepted*, not answered with 429.

    The query is cancelled immediately - the point is that the previous job released the slot, not
    what this one would have found. `cancel()` awaits the task, so the next scenario cannot see a
    busy slot either. (The public API only accepts FAST/DEEP; FILENAME_ONLY is a harness mode.)
    """
    body = {"workspace": str(BASE), "libraryId": "fixtures", "query": "slot check", "mode": "FAST"}
    response = client.post(url + "/queries", json=body, headers=AUTH)
    if response.status_code != 200:
        return {"released": False, "detail": "submit refused with %d: %s" % (response.status_code,
                                                                             response.text[:160])}
    identity = response.json()["id"]
    cancelled = client.delete(url + "/queries/" + identity, headers=AUTH).json().get("result") or {}
    return {"released": True, "detail": "accepted, then %s" % (cancelled.get("status") or "removed")}


def cleanup_facts(service, identity, work_dir, table_before, pid):
    job = service.JOBS.get(identity) or {}
    table = live_processes()
    alive = {entry[0] for entry in table}
    return {
        "jobBudget": job.get("budget"),
        "workDirRemoved": bool(job.get("workDirRemoved")) and not work_dir.exists(),
        "containerPid": pid,
        "containerPidAlive": bool(pid) and pid in alive,
        "processesMentioningJobDir": [[entry_pid, line[:160]] for entry_pid, line in table
                                      if "job-" + identity in line],
    }


def scenario(name, mode, service, client, url, stub, plan, limit, kill_at=None, cancel_at=None):
    """Run one long-budget scenario and return every fact the checks need."""
    budget = service.budget_seconds(mode)
    stub.plan(*plan)
    table_before = live_processes()
    before_terminal, before_finalize = COUNTERS["terminal"], COUNTERS["finalize"]
    submitted_at = time.monotonic()
    identity = submit(client, url, mode=mode)
    watched = observe_job(service, identity, limit, kill_at=kill_at, cancel_at=cancel_at,
                          client=client, url=url, mode=mode)
    result = watched["job"].get("result")
    work_dir = Path(watched["job"].get("workDir") or (BASE / ".local/query-cache" / ("job-" + identity)))
    facts = {"name": name, "mode": mode, "budgetSeconds": budget,
             "elapsedSeconds": round((watched["job"].get("budget") or {}).get("elapsedSeconds") or 0, 2),
             "httpElapsedSeconds": round(time.monotonic() - submitted_at, 2),
             "status": (result or {}).get("status"),
             "warnings": (result or {}).get("warnings"),
             "evidenceCount": len((result or {}).get("evidence") or []),
             "providerCalls": stub.calls,
             # Relative to the moment the query was submitted: the job's own clock starts there.
             "providerCallsStartedAt": [round(value - submitted_at, 2) for value in stub.started],
             "killAt": kill_at, "cancelAt": cancel_at,
             "terminalCalls": COUNTERS["terminal"] - before_terminal,
             "finalizeCalls": COUNTERS["finalize"] - before_finalize}
    facts.update(cleanup_facts(service, identity, work_dir, table_before, watched["pid"]))
    slot = slot_released(client, url)
    facts["slotReleased"] = slot["released"]
    facts["slotDetail"] = slot["detail"]
    client.delete(url + "/queries/" + identity, headers=AUTH)
    return facts


def run_near_deadline(client, url, stub, service, mode):
    """The last needed reply lands inside the budget: it must be kept, not overwritten by a timeout.

    Which call carries the delay, and how much tail the engine needs afterwards, are **measured**:

    * FAST closes right after its first reply, so the first call is the slow one (~66s of a 90s
      budget was enough);
    * DEEP keeps working after a reply, so the second call is slow - and it needs real tail time. A
      reply at 182s of 240 (58s left) ended with the engine *terminated at the deadline*; a reply at
      ~146s (94s left) finished cleanly. These fixture-specific observations do not establish a
      general minimum finishing time; the spare below is a probe setting, not an engine guarantee.
    """
    budget = service.budget_seconds(mode)
    if mode == "FAST":
        plan = [budget - 25.0] + [0.2] * 4
    else:
        plan = [0.2, budget - 96.0] + [0.2] * 8
    return scenario("a reply that lands just inside the budget", mode, service, client, url, stub,
                    (plan,), limit=budget + 30)


def run_across_deadline(client, url, stub, service, mode):
    """The provider never answers: the job must end at its deadline, with no later call started."""
    budget = service.budget_seconds(mode)
    return scenario("a provider that never answers", mode, service, client, url, stub,
                    ([budget * 3],), limit=budget + 40)


def run_provider_failure(client, url, stub, service, mode):
    """The provider fails mid-call on every attempt: an explicit result, no residue, no crash."""
    return scenario("a provider that fails mid-call", mode, service, client, url, stub,
                    ([2.0], "always", ConnectionResetError("connection reset by peer (stub)")), limit=140)


def run_engine_dies_mid_call(client, url, stub, service, mode):
    """The engine (container) is killed while the parent is mid-answer: the pipe breaks, nothing leaks."""
    return scenario("the engine dies mid-call", mode, service, client, url, stub,
                    ([45.0],), limit=120, kill_at=6.0)


def run_cancel_before_deadline(client, url, stub, service, mode):
    """Cancel while a model call is in flight: no further call may start, nothing may be left running.

    The cancel lands after the container is up (so a call really is in flight), which is what makes
    the in-flight cancel path - and not the "cancelled before startup" shortcut - the one exercised.
    """
    return scenario("cancelled mid-call, well before the deadline", mode, service, client, url, stub,
                    ([60.0],), limit=90, cancel_at=12.0)


# -- scoring ----------------------------------------------------------------------------------------
def _score(report, service):
    scenarios = report["scenarios"]
    checks = {}
    for facts in scenarios:
        name, mode = facts["name"], facts["mode"]
        budget = facts["budgetSeconds"]
        label = "%s [%s]" % (name, mode)
        ends = facts["terminalCalls"] + facts["finalizeCalls"]
        # Observed overshoot at the deadline is ~0.1s; the allowance covers scheduling noise, not a
        # second try. The start-time check is what bounds "no call after the deadline": every call the
        # stub saw must have begun before the budget ran out.
        checks["%s: stayed inside the budget" % label] = facts["elapsedSeconds"] <= budget + 2 \
            and facts["httpElapsedSeconds"] <= budget + 5
        checks["%s: no provider call started after the deadline" % label] = all(
            value <= budget + 1 for value in facts["providerCallsStartedAt"] or [0])
        checks["%s: exactly one terminal state" % label] = ends == 1
        checks["%s: no process or job residue" % label] = (facts["workDirRemoved"]
                                                          and not facts["containerPidAlive"]
                                                          and not facts["processesMentioningJobDir"])
        checks["%s: slot released" % label] = facts["slotReleased"]
        warnings = " ".join(facts["warnings"] or [])
        if name.startswith("a reply that lands"):
            # The race this round is about: a payload that arrives inside the budget must be the one
            # that ends the job, never replaced by a timeout terminal.
            checks["%s: the in-budget payload was kept" % label] = (
                facts["finalizeCalls"] == 1 and facts["terminalCalls"] == 0
                and facts["status"] is not None and "BUDGET_EXCEEDED" not in warnings)
        if name.startswith("a provider that never answers"):
            checks["%s: the timeout is explicit" % label] = any(
                marker in warnings for marker in ("BUDGET_EXCEEDED", "DEADLINE_EXCEEDED", "PROVIDER_ERROR",
                                                  "Worker failure", "ISOLATION_UNAVAILABLE"))
            # Strict version of "no late call": the one in-flight call is the only one ever started,
            # so a retry after the deadline would be visible as a second call.
            checks["%s: only the in-flight call was ever started" % label] = facts["providerCalls"] == 1
        if name.startswith("a provider that fails"):
            checks["%s: the fault is named" % label] = facts["status"] in ("failed", "partial") and bool(warnings)
        if name.startswith("the engine dies"):
            checks["%s: no provider call after the engine died" % label] = facts["providerCalls"] <= 1
        if name.startswith("cancelled mid-call"):
            # The cancel happened while a call was in flight: exactly that one call may exist, and
            # nothing may have started after the cancel.
            checks["%s: the in-flight call was the last one" % label] = (
                facts["status"] == "cancelled" and facts["providerCalls"] == 1
                and all(value <= (facts["cancelAt"] or 0) + 3 for value in facts["providerCallsStartedAt"]))
    report["checks"] = checks
    report["passed"] = all(checks.values())


if __name__ == "__main__":
    raise SystemExit(main())
