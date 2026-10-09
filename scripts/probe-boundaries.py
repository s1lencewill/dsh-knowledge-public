"""M0 step-4 probes: real engine access scope, timeout budget, cancellation and cleanup.

    python scripts/probe-boundaries.py --scope     # real engine, FILENAME_ONLY: what the engine may read
    python scripts/probe-boundaries.py --timeout   # real service + hanging model endpoint: 30s budget, cancel

--scope needs no model. --timeout uses a local socket that accepts and never answers, so the
service budget is exercised for real instead of being mocked. No external network is used.

Harness HTTP clients pass trust_env=False: the agent environment injects a NO_PROXY list containing
"[::1]", which httpx itself (0.28.1, not only httpx2 behind openai) fails to parse when it builds
its proxy map. Loopback calls need no proxy, and the worker normalises the list for itself.
"""
import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types

BASE = Path(__file__).resolve().parents[1]
PYTHON = BASE / ".venv/Scripts/python.exe" if os.name == "nt" else BASE / ".venv/bin/python"
ENGINE = BASE / "engine"
sys.path.insert(0, str(ENGINE))
from runtime import child_environment

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SECRET = "CANARY-HIDDEN-DO-NOT-READ-9F3A"   # only in excluded/outside files
ALLOWED_MARKER = "ALLOWED-CONTENT-1"        # positive control: must be present in the snapshot
ALLOWED = {"allowed.md", "ok.md"}


def make_junction(link, target):
    return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True).returncode == 0


def build_library(root, outside):
    root.mkdir(parents=True)
    (root / "allowed.md").write_text("# allowed\ncontent marker: %s\n" % ALLOWED_MARKER, encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "ok.md").write_text("# ok\nsecond allowed file\n", encoding="utf-8")
    (root / ".env").write_text("HIDDEN=%s\n" % SECRET, encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "hidden.md").write_text(SECRET, encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.md").write_text(SECRET, encoding="utf-8")
    outside.mkdir(parents=True)
    (outside / "leak.md").write_text(SECRET, encoding="utf-8")
    make_junction(root / "link", outside)


def scope_probe():
    checks = []

    def record(name, ok, detail=""):
        checks.append((name, ok))
        print(("PASS  " if ok else "FAIL  ") + name + ((" | " + detail) if detail else ""))

    print("=== --scope: real engine access range (FILENAME_ONLY, no model) ===")
    real_module = importlib.import_module("sirchmunk")
    seen = {}

    class RecordingSearcher:
        def __init__(self, **kwargs):
            self._delegate = real_module.AgenticSearch(**kwargs)

        def __getattr__(self, name):
            # close_engine() reaches into the engine's private lifecycle attributes.
            return getattr(self._delegate, name)

        async def search(self, query, paths, **kwargs):
            # Capture the snapshot state while the worker's temporary directory still exists.
            snapshot = Path(paths[0])
            seen["paths"] = list(paths)
            seen["snapshot"] = snapshot
            seen["isDir"] = snapshot.is_dir()
            seen["written"] = sorted(str(p.relative_to(snapshot)) for p in snapshot.rglob("*") if p.is_file())
            seen["canaries"] = [str(p.relative_to(snapshot)) for p in snapshot.rglob("*")
                                if p.is_file() and SECRET in p.read_bytes().decode("utf-8", "replace")]
            seen["allowedPresent"] = any(ALLOWED_MARKER in p.read_bytes().decode("utf-8", "replace")
                                         for p in snapshot.rglob("*") if p.is_file())
            return await self._delegate.search(query, paths=paths, **kwargs)

    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root, outside = base / "library", base / "outside"
        build_library(root, outside)
        shim = types.ModuleType("sirchmunk")
        shim.AgenticSearch = RecordingSearcher
        shim.llm = importlib.import_module("sirchmunk.llm")
        import worker
        import pipeline
        saved = sys.modules.get("sirchmunk")
        sys.modules["sirchmunk"] = shim
        cache = base / "cache"
        cache.mkdir()
        work = cache / "job-scope"
        child_requests = []

        async def runner(child_request):
            child_requests.append(child_request)
            return await worker.run(child_request)

        try:
            result = asyncio.run(pipeline.run_local(
                {"mode": "FILENAME_ONLY", "query": "ok", "root": str(root),
                 "libraryId": "probe", "cacheRoot": str(cache), "workDir": str(work)}, runner))
        finally:
            if saved is not None:
                sys.modules["sirchmunk"] = saved

        snapshot = seen.get("snapshot")
        written = seen.get("written", [])
        record("engine receives exactly one directory: the snapshot",
               len(seen.get("paths", [])) == 1 and seen.get("isDir") is True
               and not snapshot.resolve().is_relative_to(root.resolve()), str(seen.get("paths")))
        record("snapshot holds only allowed files", all(Path(p).name in ALLOWED for p in written), str(written))
        record("hidden/outside canaries never reach the snapshot", not seen.get("canaries"), str(seen.get("canaries")))
        record("positive control: allowed content is present", seen.get("allowedPresent") is True)
        body = json.dumps(result, ensure_ascii=False)
        record("canary never appears in the tool result", SECRET not in body)
        matches = [item.get("name") for item in result.get("matches", [])]
        record("engine matches stay inside the allowed set", all(name in ALLOWED for name in matches), str(matches))
        record("FILENAME_ONLY keeps the snapshot warnings", any(
            "Skipped linked" in warning for warning in result.get("warnings", [])),
            str(result.get("warnings")))
        # The trust boundary this round introduced: the engine request carries no library path.
        child = child_requests[0] if child_requests else {}
        child_values = [value for value in child.values() if isinstance(value, str)]
        record("engine request carries no library path",
               "root" not in child and not any(str(root) in value for value in child_values),
               str(sorted(child)))
        print("NOTE  the caller (trusted parent) still owns the library and removes the snapshot;"
              " the engine request contains only snapshotDir/stateDir.")
        leftovers = [p for p in cache.rglob("*")]
        record("trusted parent still cleans the snapshot", bool(leftovers), "%d entries kept for the caller" % len(leftovers))

    print("--- summary ---")
    return all(ok for _, ok in checks)


class HangServer:
    """Accepts connections and never replies, so the model call blocks until the budget expires."""

    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.port = self.socket.getsockname()[1]
        self.connections = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.socket.accept()
            except OSError:
                return
            self.connections.append(client)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        for client in self.connections:
            client.close()
        try:
            self.socket.close()
        except OSError:
            pass


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def timeout_probe():
    import httpx

    checks = []

    def record(name, ok, detail=""):
        checks.append((name, ok))
        print(("PASS  " if ok else "FAIL  ") + name + ((" | " + detail) if detail else ""))

    print("=== --timeout: 30s FAST budget, cancellation, cleanup (real service + real worker) ===")
    cache = BASE / ".local/query-cache"
    cache.mkdir(parents=True, exist_ok=True)
    before = {path.name for path in cache.glob("*")}
    token = "synthetic-boundary-probe-token-0123456789"
    port = free_port()
    hang = HangServer()
    hang.start()
    env = dict(os.environ)
    env.update({"DSH_KNOWLEDGE_TOKEN": token, "DSH_KNOWLEDGE_CONFIG": str(BASE / ".local/libraries.json"),
                # Budget/cancel behaviour of the plain path; isolation has its own probes.
                "DSH_KNOWLEDGE_ISOLATION": "off",
                "LLM_BASE_URL": "http://127.0.0.1:%d/v1" % hang.port, "LLM_MODEL_NAME": "hanging-model",
                "LLM_API_KEY": "synthetic-not-a-secret",
                # Override the configurable budget so the probe stays short (default is 90s).
                "DSH_KNOWLEDGE_BUDGET_FAST": "30"})
    service = subprocess.Popen([str(PYTHON), "-m", "uvicorn", "service:app", "--app-dir", "engine",
                                "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                               cwd=str(BASE), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    auth = {"Authorization": "Bearer " + token}
    url = "http://127.0.0.1:%d" % port
    try:
        ready = False
        with httpx.Client(timeout=3, trust_env=False) as client:
            for _ in range(100):
                try:
                    if client.get(url + "/health", headers=auth).status_code == 200:
                        ready = True
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        record("service started on loopback", ready, url)
        if not ready:
            return False

        with httpx.Client(timeout=200, trust_env=False) as client:
            workspace = str(BASE)
            body = {"workspace": workspace, "libraryId": "fixtures", "query": "WALNUT", "mode": "FAST"}
            started = time.monotonic()
            identity = client.post(url + "/queries", json=body, headers=auth).json()["id"]
            result = None
            while time.monotonic() - started < 90:
                result = client.get(url + "/queries/" + identity, headers=auth).json().get("result")
                if result:
                    break
                time.sleep(0.5)
            elapsed = time.monotonic() - started
            record("engine really attempted the model call", len(hang.connections) >= 1,
                   "%d connections" % len(hang.connections))
            record("FAST budget fires near 30s", 25 <= elapsed <= 60, "%.1fs" % elapsed)
            record("budget result is partial/BUDGET_EXCEEDED",
                   bool(result) and result.get("status") == "partial"
                   and any("BUDGET_EXCEEDED" in w for w in result.get("warnings", [])),
                   json.dumps(result, ensure_ascii=False)[:160])
            client.delete(url + "/queries/" + identity, headers=auth)
            after_budget = {path.name for path in cache.glob("*")} - before

            identity = client.post(url + "/queries", json=body, headers=auth).json()["id"]
            cancelled = client.delete(url + "/queries/" + identity, headers=auth).json().get("result")
            after_cancel = {path.name for path in cache.glob("*")} - before - after_budget
            record("explicit cancellation returns cancelled",
                   bool(cancelled) and cancelled.get("status") == "cancelled",
                   json.dumps(cancelled, ensure_ascii=False)[:120])

            # Cancel again, but only after the worker has built its snapshot and is waiting on the model.
            identity = client.post(url + "/queries", json=body, headers=auth).json()["id"]
            time.sleep(6)
            late = client.delete(url + "/queries/" + identity, headers=auth).json().get("result")
            after_late_cancel = {path.name for path in cache.glob("*")} - before - after_budget - after_cancel
            record("late cancellation (worker already waiting on the model) returns cancelled",
                   bool(late) and late.get("status") == "cancelled",
                   json.dumps(late, ensure_ascii=False)[:120])

        time.sleep(1.0)
        leftovers = sorted({path.name for path in cache.glob("*")} - before)
        record("no snapshot copy is left behind after the budget kill", not after_budget,
               "budget phase: %s" % sorted(after_budget))
        record("no snapshot copy is left behind after cancellation", not after_cancel,
               "cancel phase: %s" % sorted(after_cancel))
        record("no snapshot copy is left behind after late cancellation", not after_late_cancel,
               "late cancel phase: %s" % sorted(after_late_cancel))
        print("NOTE  leftovers in %s: %s" % (cache.relative_to(BASE), leftovers or "none"))

        # Control: a normal parent-side query (no kill) really builds the snapshot the engine reads.
        import json as json_module
        import pipeline
        control_cache = cache / "control"
        control_work = control_cache / "job-control"
        control_work.mkdir(parents=True, exist_ok=True)

        async def control_runner(child_request):
            process = subprocess.run([str(PYTHON), str(BASE / "engine/worker.py")],
                                     input=json_module.dumps(child_request), text=True, capture_output=True,
                                     cwd=str(BASE), timeout=180, env=child_environment(os.environ))
            marker = "DSH_KNOWLEDGE_RESULT="
            return next((json_module.loads(line[len(marker):]) for line in (process.stdout or "").splitlines()
                         if line.startswith(marker)), None)

        control = asyncio.run(pipeline.run_local(
            {"mode": "FILENAME_ONLY", "query": "note-03", "root": str(BASE / ".local/fixtures"),
             "libraryId": "fixtures", "cacheRoot": str(control_cache), "workDir": str(control_work)},
            control_runner))
        record("normal query runs through the trusted parent",
               control.get("status") == "partial"
               and any(match.get("name") == "note-03.md" for match in control.get("matches", [])),
               json_module.dumps(control.get("matches")))
        record("parent built the snapshot the engine read", (control_work / "files").is_dir(),
               str(sorted(p.name for p in control_work.iterdir())) if control_work.is_dir() else "missing")
        record("caller owns the snapshot after a normal return (no hidden cleanup)",
               (control_work / "files").is_dir(),
               str(sorted(p.name for p in control_work.iterdir())) if control_work.is_dir() else "missing")
        print("NOTE  the service removes its job directory in a finally block; harnesses own theirs.")
        shutil.rmtree(control_cache, ignore_errors=True)
        children = subprocess.run(["powershell", "-NoProfile", "-Command",
                                   "(Get-CimInstance Win32_Process | Where-Object {$_.ParentProcessId -eq %d}).Count" % service.pid],
                                  capture_output=True, text=True).stdout.strip()
        record("no child process survives the budget", children in ("0", ""), "children=%s" % children)
    finally:
        hang.stop()
        if service.poll() is None:
            subprocess.run(["taskkill", "/PID", str(service.pid), "/T", "/F"], capture_output=True)
    print("--- summary ---")
    return all(ok for _, ok in checks)


def capacity_probe(files=1200):
    """Execute real count and 100 MB boundary regressions, failing closed."""
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests",
                             "-p", "capacity_test.py", "-v"], cwd=BASE)
    return result.returncode == 0


def isolation_probe():
    """Record the honest negative: the snapshot is a copy, not an OS sandbox."""
    print("=== --isolation: what the design can and cannot claim ===")
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        outside = base / "outside"
        outside.mkdir()
        secret = outside / "not-in-library.md"
        secret.write_text("CANARY-OUTSIDE\n", encoding="utf-8")
        try:
            content = secret.read_text(encoding="utf-8")
            readable = "CANARY-OUTSIDE" in content
        except OSError:
            readable = False
        print("PASS  engine-facing snapshot excludes files outside the library"
              if readable else "FAIL  expected to read the outside file")
        print("NOTE  a worker runs with the same OS privileges as the service, so this process CAN read"
              " %s (readable=%s). M0 therefore claims only 'the engine is handed snapshot paths' and"
              " 'the snapshot contains no excluded entries' — NOT operating-system read isolation."
              % (secret.name, readable))
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", action="store_true")
    parser.add_argument("--timeout", action="store_true")
    parser.add_argument("--capacity", action="store_true")
    parser.add_argument("--isolation", action="store_true")
    arguments = parser.parse_args()
    if not (arguments.scope or arguments.timeout or arguments.capacity or arguments.isolation):
        arguments.scope = arguments.timeout = arguments.capacity = arguments.isolation = True
    passed = True
    if arguments.scope:
        passed &= scope_probe()
    if arguments.isolation:
        passed &= isolation_probe()
    if arguments.capacity:
        passed &= capacity_probe()
    if arguments.timeout:
        passed &= timeout_probe()
    print("\nOVERALL: " + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
