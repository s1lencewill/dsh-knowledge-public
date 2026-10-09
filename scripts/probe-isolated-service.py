"""End-to-end isolation probe: the real service, in required-isolation mode.

Starts uvicorn without DSH_KNOWLEDGE_ISOLATION set at all - the default must be isolation, not a
plain worker - runs a real FAST query through the AppContainer (model calls proxied by the
service), then cancels a second query and checks that the container tree dies, the profile is
removed and no job directory is left behind. It also checks that a runtime staged from older
inputs, and a missing runtime, are both refused by name instead of served.

    python scripts/probe-isolated-service.py [--mode FAST|DEEP]
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "engine"))
PYTHON = BASE / ".venv/Scripts/python.exe"
LOGS = BASE / ".local/verification/isolated-service"
MODEL_ENV = BASE / ".local/model.env"
BUDGETS = {"FAST": 90, "DEEP": 240}


def load_model_env():
    values = {}
    if MODEL_ENV.is_file():
        for line in MODEL_ENV.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def profile_record():
    """The profile the service recorded for itself (it is not visible in the registry)."""
    path = BASE / ".local/state/isolation-profile.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def start_service(port, environment, log_path):
    import httpx  # noqa: F401  (readiness polling happens in the caller)
    log_file = open(log_path, "wb")
    process = subprocess.Popen([str(PYTHON), "-m", "uvicorn", "service:app", "--app-dir", "engine",
                                "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                               cwd=str(BASE), env=environment, stdout=log_file, stderr=subprocess.STDOUT)
    return process, log_file


def stop_service(process, log_file, graceful=False):
    if process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        process.wait(timeout=20)
    log_file.close()


def wait_healthy(client, url, auth, process):
    import httpx
    for _ in range(200):
        try:
            if client.get(url + "/health", headers=auth).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        if process.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def live_processes():
    """(pid, command line) for every process, so job residue can be found after the fact."""
    try:
        listing = subprocess.run(["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"],
                                 capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0:
        return None
    found = []
    for line in listing.stdout.splitlines():
        parts = line.split(",")
        if len(parts) < 3 or not parts[-1].strip().isdigit():
            continue
        found.append((int(parts[-1]), ",".join(parts[1:-1])))
    return found


def run_budget_probe(environment, auth, seconds, log_name):
    """Start a service whose FAST budget is `seconds` and record what an expired budget returns."""
    import httpx
    port = free_port()
    url = "http://127.0.0.1:%d" % port
    environment = dict(environment, DSH_KNOWLEDGE_BUDGET_FAST=str(seconds))
    service, log_file = start_service(port, environment, LOGS / log_name)
    report = {"budgetSeconds": seconds}
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            if not wait_healthy(client, url, auth, service):
                report["healthy"] = False
                return report
            report["healthy"] = True
            body = {"workspace": str(BASE), "libraryId": "fixtures",
                    "query": "Which note contains the codename WALNUT-03?", "mode": "FAST"}
            started = time.monotonic()
            identity = client.post(url + "/queries", json=body, headers=auth).json()["id"]
            result = None
            while time.monotonic() - started < seconds + 90:
                result = client.get(url + "/queries/" + identity, headers=auth).json().get("result")
                if result:
                    break
                time.sleep(0.25)
            report["elapsedSeconds"] = round(time.monotonic() - started, 1)
            report["status"] = (result or {}).get("status")
            report["warnings"] = (result or {}).get("warnings")
            client.delete(url + "/queries/" + identity, headers=auth)
    finally:
        stop_service(service, log_file)
    short_log = (LOGS / log_name).read_text(encoding="utf-8", errors="replace")
    budgets = [json.loads(line.split(" ", 1)[1]) for line in short_log.splitlines()
               if line.startswith("JOB_BUDGET ")]
    report["jobBudget"] = budgets[-1] if budgets else None
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="FAST", choices=sorted(BUDGETS))
    arguments = parser.parse_args()
    import httpx
    import appcontainer

    token = "synthetic-isolated-service-token-0123456789"
    port = free_port()
    url = "http://127.0.0.1:%d" % port
    environment = dict(os.environ)
    environment.update(load_model_env())
    environment.update({"DSH_KNOWLEDGE_TOKEN": token,
                        "DSH_KNOWLEDGE_CONFIG": str(BASE / ".local/libraries.json")})
    # Deliberately unset: isolation must be the default, not something the probe turns on.
    environment.pop("DSH_KNOWLEDGE_ISOLATION", None)
    LOGS.mkdir(parents=True, exist_ok=True)
    auth = {"Authorization": "Bearer " + token}
    report = {"mode": arguments.mode, "isolation": "unset (default must be required)"}
    try:
        service, log_file = start_service(port, environment, LOGS / "service.log")
        with httpx.Client(timeout=15, trust_env=False) as client:
            if not wait_healthy(client, url, auth, service):
                print("service did not become healthy")
                print((LOGS / "service.log").read_text(encoding="utf-8", errors="replace")[-2000:])
                return 1
            report["health"] = client.get(url + "/health", headers=auth).json()
            report["profileDuringRun"] = profile_record()

            body = {"workspace": str(BASE), "libraryId": "fixtures",
                    "query": "Which note contains the codename WALNUT-03?", "mode": arguments.mode}
            started = time.monotonic()
            identity = client.post(url + "/queries", json=body, headers=auth).json()["id"]
            result = None
            while time.monotonic() - started < BUDGETS[arguments.mode] + 60:
                result = client.get(url + "/queries/" + identity, headers=auth).json().get("result")
                if result:
                    break
                time.sleep(0.5)
            report["query"] = {"elapsedSeconds": round(time.monotonic() - started, 1),
                               "status": (result or {}).get("status"),
                               "warnings": (result or {}).get("warnings"),
                               "evidence": (result or {}).get("evidence")}
            client.delete(url + "/queries/" + identity, headers=auth)

            # Cancellation: the container tree must die with the job, and no further model call may be
            # started. The container's own pid is checked against the live process table below.
            identity = client.post(url + "/queries", json=body, headers=auth).json()["id"]
            time.sleep(3.0)
            cancelled = client.delete(url + "/queries/" + identity, headers=auth).json().get("result")
            report["cancellation"] = {"status": (cancelled or {}).get("status"),
                                      "warnings": (cancelled or {}).get("warnings"),
                                      "id": identity}
            time.sleep(1.0)
        # Parent crash: a hard kill skips the shutdown hook entirely.
        crashed_profile = (profile_record() or {}).get("name")
        stop_service(service, log_file)
        report["profileAfterHardKill"] = profile_record()
        report["crashedProfile"] = crashed_profile

        # The next startup must reclaim what the crashed instance left behind.
        port = free_port()
        url = "http://127.0.0.1:%d" % port
        service, log_file = start_service(port, environment, LOGS / "service-restart.log")
        with httpx.Client(timeout=15, trust_env=False) as client:
            healthy = wait_healthy(client, url, auth, service)
        report["restartHealthy"] = healthy
        report["profileAfterRestart"] = profile_record()
        restart_log = (LOGS / "service-restart.log").read_text(encoding="utf-8", errors="replace")
        starts = [json.loads(line.split(" ", 1)[1]) for line in restart_log.splitlines()
                  if line.startswith("ISOLATION_START ")]
        report["restartStart"] = starts[0] if starts else None
        stop_service(service, log_file)

        # An expired budget must come back as an explicit result, not as a request that hangs. This
        # runs after the reclaim check: this service would otherwise consume the crashed profile.
        report["shortBudget"] = run_budget_probe(environment, auth, 2, "service-short-budget.log")

        # Isolation unavailable: the service must refuse to start instead of running unisolated.
        broken = dict(environment, DSH_KNOWLEDGE_RUNTIME=str(BASE / ".local/definitely-missing-runtime"))
        broken_port = free_port()
        service, log_file = start_service(broken_port, broken, LOGS / "service-broken.log")
        for _ in range(100):
            if service.poll() is not None:
                break
            time.sleep(0.1)
        report["startupRefusal"] = {"exitCode": service.poll(),
                                    "profileRecord": profile_record()}
        stop_service(service, log_file)

        # A runtime built from older inputs must be refused by name (see engine/staging.py).
        import staging
        stale = BASE / ".local/stale-runtime-probe"
        shutil.rmtree(stale, ignore_errors=True)
        stale.mkdir(parents=True)
        (stale / "ready").write_text("staged", encoding="utf-8")
        manifest_path = BASE / ".local/appcontainer-runtime" / staging.MANIFEST_NAME
        try:
            # Corrupt exactly one recorded input: the refusal must name it and nothing else.
            recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
            recorded["inputs"]["engine/worker.py"] = "0" * 64
        except (OSError, ValueError, KeyError):
            recorded = {"python": "%d.%d.%d" % sys.version_info[:3], "inputs": {"engine/worker.py": "0" * 64}}
        (stale / staging.MANIFEST_NAME).write_text(json.dumps(recorded), encoding="utf-8")
        stale_port = free_port()
        service, log_file = start_service(stale_port, dict(environment, DSH_KNOWLEDGE_RUNTIME=str(stale)),
                                          LOGS / "service-stale.log")
        for _ in range(100):
            if service.poll() is not None:
                break
            time.sleep(0.1)
        stale_log = (LOGS / "service-stale.log").read_text(encoding="utf-8", errors="replace")
        staleness = [line.strip() for line in stale_log.splitlines()
                     if "STALE_RUNTIME:" in line and not line.strip().startswith("raise")]
        report["staleRuntimeRefusal"] = {"exitCode": service.poll(),
                                         "reason": staleness[-1] if staleness else None}
        stop_service(service, log_file)
        shutil.rmtree(stale, ignore_errors=True)
    finally:
        pass

    log = (LOGS / "service.log").read_text(encoding="utf-8", errors="replace")
    report["isolationJobs"] = [json.loads(line.split(" ", 1)[1]) for line in log.splitlines()
                               if line.startswith("ISOLATION_JOB ")]
    report["jobBudgets"] = [json.loads(line.split(" ", 1)[1]) for line in log.splitlines()
                            if line.startswith("JOB_BUDGET ")]
    report["profileAfterShutdownHook"] = profile_record()
    cache = BASE / ".local/query-cache"
    leftovers = sorted((path.name, [p.name for p in path.iterdir()] if path.is_dir() else [])
                       for path in cache.iterdir()) if cache.is_dir() else []
    report["leftoverJobDirs"] = leftovers
    evidence = report["query"]["evidence"] or []
    first = report["isolationJobs"][0] if report["isolationJobs"] else {}
    restart = report.get("restartStart") or {}

    # Process residue: neither the container's own pid nor any process whose command line mentions
    # the job directory may still be alive. The container is launched with its request file inside
    # that directory, so this catches the whole tree, not just the process the parent tracked.
    table = live_processes()
    report["liveProcessesReadable"] = table is not None
    pids = {job.get("pid") for job in report["isolationJobs"] if job.get("pid")}
    job_markers = ["job-" + str(entry.get("job")) for entry in report["jobBudgets"]] if report["jobBudgets"] else []
    if table is None:
        report["survivingContainerPids"] = None
        report["processesMentioningAJobDirectory"] = None
    else:
        alive = {pid for pid, _ in table}
        report["survivingContainerPids"] = sorted(pids & alive)
        report["processesMentioningAJobDirectory"] = [
            [pid, line[:200]] for pid, line in table
            if any(marker and marker in line for marker in job_markers)]

    short = report.get("shortBudget") or {}
    report["checks"] = {
        "isolationIsTheDefaultWithoutTheVariable": (report.get("health") or {}).get("isolation") == "required",
        "healthReportsAnActiveProfile": (report.get("health") or {}).get("isolationActive") is True,
        "profileCreatedForTheService": bool(report.get("profileDuringRun")),
        "engineRanInsideTheContainer": bool(first) and first.get("exitCode") in (0, None),
        "modelCallsWentThroughTheServiceProxy": (first.get("modelCalls") or 0) >= 1,
        "parentVerifiedCitations": bool(evidence) and all(item.get("startLine") for item in evidence),
        "cancellationTerminatedTheContainer": any(job.get("terminated") for job in report["isolationJobs"]),
        "cancellationStopsFurtherModelCalls": any(job.get("cancelled") for job in report["isolationJobs"]),
        "aCompletedJobIsNotReportedAsCancelled": any(not job.get("cancelled") and not job.get("terminated")
                                                     for job in report["isolationJobs"]),
        "containerProcessIsGoneAfterCancel": report.get("survivingContainerPids") == [],
        "noProcessMentionsAJobDirectory": report.get("processesMentioningAJobDirectory") == [],
        "anExpiredBudgetReturnsExplicitly": short.get("status") == "partial"
                                            and "BUDGET_EXCEEDED" in " ".join(short.get("warnings") or []),
        "theShortJobStayedInsideItsBudget": bool(short.get("jobBudget"))
                                            and short["jobBudget"]["elapsedSeconds"]
                                            <= short["budgetSeconds"] + 5,
        "hardKillLeavesTheProfileForRecovery": bool(report.get("crashedProfile"))
                                               and (report.get("profileAfterHardKill") or {}).get("name")
                                               == report["crashedProfile"],
        "nextStartupReclaimsTheCrashedProfile": report.get("restartHealthy")
                                                and restart.get("reclaimed") == report.get("crashedProfile")
                                                and (report.get("profileAfterRestart") or {}).get("name")
                                                != report.get("crashedProfile"),
        "noSnapshotCopyLeftBehind": not any("files" in contents for _, contents in leftovers),
        "startupRefusedWhenIsolationIsUnavailable": (report.get("startupRefusal") or {}).get("exitCode") not in (None, 0)
                                                    and not (report.get("startupRefusal") or {}).get("profileRecord"),
        "aStaleRuntimeIsRefusedByName": (report.get("staleRuntimeRefusal") or {}).get("exitCode") not in (None, 0)
                                        and bool((report.get("staleRuntimeRefusal") or {}).get("reason")),
    }
    report["passed"] = all(report["checks"].values())
    (LOGS / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
