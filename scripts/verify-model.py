"""Real-model verification harness for M0 steps 2-3.

Runs the real worker against a configured OpenAI-compatible model using synthetic fixtures only,
records latency/usage/status, and re-locates every returned excerpt in the original source file.

The API key is never printed, never written to the report and never passed on a command line.

    python scripts/verify-model.py --selftest        # FILENAME_ONLY plumbing check, no model
    python scripts/verify-model.py                   # FAST and DEEP with the configured model
    python scripts/verify-model.py --mode FAST --require-evidence

Exit codes: 0 verified, 1 checks failed, 2 model configuration missing/invalid.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[1]
PYTHON = BASE / ".venv/Scripts/python.exe" if os.name == "nt" else BASE / ".venv/bin/python"
WORKER = BASE / "engine/worker.py"
FIXTURES = BASE / ".local/fixtures"
CACHE = BASE / ".local/query-cache"
REPORT_DIR = BASE / ".local/verification/model"
MODEL_ENV_FILE = BASE / ".local/model.env"
ENV_NAMES = ("LLM_BASE_URL", "LLM_MODEL_NAME", "LLM_API_KEY")
QUERY = "Which synthetic note contains the project codename WALNUT-03?"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_model_env():
    """Read KEY=VALUE pairs from the gitignored .local/model.env when the process env is empty.

    Only the names are reported; values are never printed and never written to the report.
    """
    if not MODEL_ENV_FILE.is_file():
        return False
    loaded = []
    for line in MODEL_ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name in ENV_NAMES and value and not os.environ.get(name, "").strip():
            os.environ[name] = value
            loaded.append(name)
    if loaded:
        print("loaded %s from %s" % (", ".join(loaded), MODEL_ENV_FILE.relative_to(BASE)))
    return bool(loaded)


def model_config():
    values = {name: os.environ.get(name, "").strip() for name in ENV_NAMES}
    missing = [name for name, value in values.items() if not value]
    return values, missing


def run_worker(mode, query, timeout):
    """Parent-side query: this harness acts as the trusted parent.

    It builds the snapshot, hands the engine only `snapshotDir`, and re-verifies every candidate
    against the original fixture file (engine/pipeline.py) — the same path the service uses.
    """
    sys.path.insert(0, str(BASE / "engine"))
    import uuid
    from runtime import child_environment
    import pipeline
    work_dir = CACHE / ("harness-" + uuid.uuid4().hex[:8])
    request = {"mode": mode, "query": query, "root": str(FIXTURES), "libraryId": "fixtures",
               "cacheRoot": str(CACHE), "workDir": str(work_dir)}
    captured = {"stdout": "", "stderr": "", "timedOut": False, "elapsed": 0.0}

    async def runner(child_request):
        started = time.monotonic()
        try:
            process = subprocess.run([str(PYTHON), str(WORKER)], input=json.dumps(child_request),
                                     text=True, capture_output=True, cwd=str(BASE), timeout=timeout,
                                     env=child_environment(os.environ))
            captured["stdout"], captured["stderr"] = process.stdout or "", process.stderr or ""
        except subprocess.TimeoutExpired as expired:
            captured["stdout"] = (expired.stdout or b"").decode("utf-8", "replace") if isinstance(expired.stdout, bytes) else (expired.stdout or "")
            captured["stderr"] = (expired.stderr or b"").decode("utf-8", "replace") if isinstance(expired.stderr, bytes) else (expired.stderr or "")
            captured["timedOut"] = True
        captured["elapsed"] = round(time.monotonic() - started, 2)
        marker = "DSH_KNOWLEDGE_RESULT="
        return next((json.loads(line[len(marker):]) for line in captured["stdout"].splitlines()
                     if line.startswith(marker)), None)

    started = time.monotonic()
    try:
        result = asyncio.run(pipeline.run_local(request, runner))
    finally:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
    total = round(time.monotonic() - started, 2)
    return {"elapsedSeconds": total, "engineSeconds": captured["elapsed"], "timedOut": captured["timedOut"],
            "payload": result, "stdout": captured["stdout"], "stderr": captured["stderr"]}


def free_port():
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def service_run(mode, query, timeout):
    """Run the same query through the real Uvicorn service, i.e. the path the DSH tool uses.

    The service applies its own budget (30s FAST / 120s DEEP) and kills the worker on expiry, so a
    result here also tells us whether the configured model fits those budgets.
    """
    import httpx
    token = "synthetic-verify-model-token-0123456789"
    port, url = free_port(), None
    url = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env.update({"DSH_KNOWLEDGE_TOKEN": token, "DSH_KNOWLEDGE_CONFIG": str(BASE / ".local/libraries.json")})
    process = subprocess.Popen([str(PYTHON), "-m", "uvicorn", "service:app", "--app-dir", "engine",
                                "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                               cwd=str(BASE), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    auth = {"Authorization": "Bearer " + token}
    started = time.monotonic()
    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            for _ in range(200):
                try:
                    if client.get(url + "/health", headers=auth).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    return {"elapsedSeconds": 0, "timedOut": False, "payload": None,
                            "stderr": "service exited during startup", "stdout": ""}
                time.sleep(0.1)
            body = {"workspace": str(BASE), "libraryId": "fixtures", "query": query, "mode": mode}
            response = client.post(url + "/queries", json=body, headers=auth)
            if response.status_code != 200:
                return {"elapsedSeconds": 0, "timedOut": False, "payload": None,
                        "stderr": "service HTTP %s: %s" % (response.status_code, response.text[:160]), "stdout": ""}
            identity = response.json()["id"]
            payload, timed_out = None, True
            while time.monotonic() - started < timeout:
                payload = client.get(url + "/queries/" + identity, headers=auth).json().get("result")
                if payload:
                    timed_out = False
                    break
                time.sleep(0.5)
            client.delete(url + "/queries/" + identity, headers=auth)
        return {"elapsedSeconds": round(time.monotonic() - started, 2), "timedOut": timed_out,
                "payload": payload, "stdout": "", "stderr": ""}
    finally:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)


def locate(evidence, root):
    """Re-locate one excerpt in the original (non-snapshot) file. Returns a check dict."""
    relative = evidence.get("relativePath", "")
    source = root / relative
    check = {"relativePath": relative, "exists": source.is_file(), "pathInsideLibrary": False,
             "startLineMatches": False, "versionMatches": False, "excerptLocatable": False,
             "reportedStartLine": evidence.get("startLine"), "measuredStartLine": None}
    try:
        check["pathInsideLibrary"] = source.resolve().is_relative_to(root.resolve())
    except OSError:
        return check
    if not check["exists"]:
        return check
    data = source.read_bytes()
    check["versionMatches"] = hashlib.sha256(data).hexdigest() == evidence.get("sourceVersion")
    text = data.decode("utf-8-sig").replace("\r\n", "\n")
    excerpt = (evidence.get("excerpt") or "").replace("\r\n", "\n").strip()
    if not excerpt:
        return check
    offset = text.find(excerpt)
    if offset < 0:
        return check
    check["excerptLocatable"] = True
    check["measuredStartLine"] = text[:offset].count("\n") + 1
    check["startLineMatches"] = check["measuredStartLine"] == evidence.get("startLine")
    return check


def audit(payload, mode):
    result = {"mode": mode, "status": None, "warnings": [], "evidenceCount": 0,
              "evidence": [], "usage": None, "allEvidenceLocatable": None, "notes": []}
    if not payload:
        result["notes"].append("worker returned no DSH_KNOWLEDGE_RESULT line")
        return result
    result["status"] = payload.get("status")
    result["warnings"] = payload.get("warnings", [])
    result["usage"] = payload.get("usage")
    result["usagePresent"] = bool(payload.get("usage"))
    evidence = payload.get("evidence", [])
    result["evidenceCount"] = len(evidence)
    checks = [locate(item, FIXTURES) for item in evidence]
    result["evidence"] = checks
    if evidence:
        result["allEvidenceLocatable"] = all(c["excerptLocatable"] and c["startLineMatches"] and c["versionMatches"] for c in checks)
    else:
        result["allEvidenceLocatable"] = None
        result["notes"].append("engine returned no evidence for this query")
    if mode == "FILENAME_ONLY":
        result["matches"] = [item.get("name") for item in payload.get("matches", [])]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["FAST", "DEEP", "FILENAME_ONLY"], action="append")
    parser.add_argument("--query", default=QUERY)
    parser.add_argument("--selftest", action="store_true", help="run FILENAME_ONLY only (no model)")
    parser.add_argument("--via-service", action="store_true",
                        help="run through the real Uvicorn service so its budget also applies")
    parser.add_argument("--require-evidence", action="store_true", help="fail when no citation is returned")
    arguments = parser.parse_args()

    modes = ["FILENAME_ONLY"] if arguments.selftest else (arguments.mode or ["FAST", "DEEP"])
    if not FIXTURES.is_dir():
        print("Fixture library missing: run .venv python scripts/setup-fixtures.py")
        return 2
    load_model_env()
    values, missing = model_config()
    needs_model = any(mode != "FILENAME_ONLY" for mode in modes)
    if needs_model and missing:
        print("MODEL_NOT_CONFIGURED: set these environment variables in the worker's process environment:")
        for name in missing:
            print("  - " + name)
        print("Base URL must be reachable from this machine; the key is only read from the environment.")
        return 2

    report = {"recordedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": values["LLM_MODEL_NAME"] or None,
              "baseUrlHost": None, "runs": [], "modelSearchValidated": False, "notes": []}
    if values["LLM_BASE_URL"]:
        from urllib.parse import urlsplit
        report["baseUrlHost"] = urlsplit(values["LLM_BASE_URL"]).netloc
    budgets = {"FAST": 90, "DEEP": 240, "FILENAME_ONLY": 60}  # matches the service defaults
    failed = False
    for mode in modes:
        print("=== %s%s ===" % (mode, " (via service)" if arguments.via_service else ""))
        if arguments.via_service:
            run = service_run(mode, arguments.query if mode != "FILENAME_ONLY" else "note-03", budgets[mode] + 60)
        else:
            run = run_worker(mode, arguments.query if mode != "FILENAME_ONLY" else "note-03", budgets[mode] + 30)
        checks = audit(run["payload"], mode)
        run["checks"] = checks
        run.pop("stdout"), run.pop("stderr")
        report["runs"].append({k: v for k, v in run.items()})
        engine_seconds = run.get("engineSeconds") or 0.0
        print("  status: %s | elapsed: %.2fs%s | evidence: %s | usage: %s" % (
            checks["status"], run["elapsedSeconds"],
            (" (engine %.2fs)" % engine_seconds) if engine_seconds else "",
            checks["evidenceCount"], checks["usage"]))
        for warning in checks["warnings"]:
            print("  warning: " + warning)
        for name in checks.get("matches", []) or []:
            print("  match: " + str(name))
        for index, item in enumerate(checks["evidence"]):
            print("  evidence %s lines %s (measured %s) locatable=%s version=%s" % (
                item["relativePath"], item["reportedStartLine"], item["measuredStartLine"],
                item["excerptLocatable"], item["versionMatches"]))
            if not item["excerptLocatable"]:
                excerpt = run["payload"]["evidence"][index].get("excerpt", "")
                print("    excerpt: " + str(excerpt)[:160])
        if checks["allEvidenceLocatable"] is False:
            failed = True
            print("  FAIL: at least one citation could not be re-located in the source")
        if run["timedOut"]:
            failed = True
            print("  FAIL: worker exceeded budget+30s (%ss budget); it cannot serve a real query in time" % budgets[mode])
        if checks["status"] == "failed":
            failed = True
            print("  FAIL: worker reported a failure")
        if arguments.require_evidence and not checks["evidenceCount"]:
            failed = True
            print("  FAIL: no citation returned (--require-evidence)")
        if mode != "FILENAME_ONLY" and not checks["evidenceCount"]:
            for line in (run["stderr"] or "").splitlines()[-4:]:
                print("  stderr: " + line[:200])
        if mode != "FILENAME_ONLY" and run["elapsedSeconds"] > budgets[mode]:
            report["notes"].append("%s exceeded the service budget (%ss)" % (mode, budgets[mode]))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = REPORT_DIR / ("verify-model-%s.json" % stamp)

    # A run certifies nothing unless the model was actually called and every citation could be
    # re-located in the source. An unreachable model can still yield "partial" with 0 evidence.
    searchable = [run for run in report["runs"] if run["checks"]["mode"] != "FILENAME_ONLY"]
    failed_run = any(run["checks"]["status"] == "failed" for run in report["runs"]) or \
        any(run["checks"]["allEvidenceLocatable"] is False for run in report["runs"])
    if not searchable:
        # --selftest: only the plumbing is checked, no model claim is made.
        plumbing = all(run["checks"]["status"] in ("complete", "partial") and run["checks"].get("matches")
                       for run in report["runs"])
        verdict = "failed" if (failed_run or not plumbing) else "plumbing-ok"
        certified = False
    else:
        certified = all(
            run["checks"]["status"] in ("complete", "partial") and run["checks"]["evidenceCount"] > 0
            and run["checks"]["allEvidenceLocatable"] and run["checks"]["usagePresent"]
            for run in searchable)
        verdict = "failed" if failed_run else ("verified" if certified else "inconclusive")
    report["verdict"] = verdict
    report["modelSearchValidated"] = verdict == "verified"
    if verdict == "inconclusive":
        report["notes"].append("model was reachable enough to finish, but no citation was returned:"
                               " the run proves nothing about retrieval quality")

    serialised = json.dumps(report, indent=2, ensure_ascii=False)
    key = values["LLM_API_KEY"]
    if key and key in serialised:
        print("REFUSING to write the report: it would contain the API key")
        return 1
    target.write_text(serialised, encoding="utf-8")
    print("\nreport: %s" % target.relative_to(BASE))
    print("verdict: %s (model search validated: %s)" % (verdict, report["modelSearchValidated"]))
    if verdict == "inconclusive":
        print("No certified answer: at least one mode returned no re-locatable citation.")
    if verdict == "failed":
        return 1
    if verdict == "plumbing-ok":
        print("Plumbing OK: engine probed without a model; no model search was validated.")
        return 0
    if verdict == "inconclusive":
        return 1 if (failed or arguments.require_evidence) else 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
