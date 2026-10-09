"""Real FAST/DEEP engine inside an AppContainer, with the model reached through the parent proxy.

The container gets the snapshot, the query and a pipe name - no credentials, no URL, no network.
The parent owns the provider, serves the pipe, and re-verifies every citation the engine returns.

    python scripts/probe-isolated-model.py [--mode FAST|DEEP] [--timeout-probe]

Requires the staged runtime (scripts/stage-appcontainer-runtime.py) and .local/model.env.
"""
import argparse
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "engine"))
import ipc
import pipeline
import model_proxy
from runtime import executable

RUNTIME = BASE / ".local/appcontainer-runtime"
LOGS = BASE / ".local/verification/appcontainer-model"
MODEL_ENV = BASE / ".local/model.env"
PATCHED_RGA = BASE / ".local/tools/rga-patched/rga.exe"
BUDGETS = {"FAST": 90, "DEEP": 240, "FILENAME_ONLY": 60}


def load_model_env():
    values = {}
    if MODEL_ENV.is_file():
        for line in MODEL_ENV.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def create_profile(name):
    userenv = c.WinDLL("userenv")
    userenv.CreateAppContainerProfile.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD,
                                                  c.POINTER(c.c_void_p)]
    userenv.DeleteAppContainerProfile.argtypes = [w.LPCWSTR]
    sid = c.c_void_p()
    if userenv.CreateAppContainerProfile(name, name, "Temporary isolated model probe", None, 0, c.byref(sid)) != 0:
        raise RuntimeError("cannot create AppContainer " + name)
    advapi32 = c.WinDLL("advapi32")
    advapi32.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    advapi32.FreeSid.argtypes = [c.c_void_p]
    formatted = w.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, c.byref(formatted)):
        advapi32.FreeSid(sid)
        userenv.DeleteAppContainerProfile(name)
        raise RuntimeError("cannot format SID")
    return userenv, advapi32, sid, formatted


def drop_profile(userenv, advapi32, sid, formatted, name):
    kernel32 = c.WinDLL("kernel32")
    kernel32.LocalFree.argtypes = [c.c_void_p]
    kernel32.LocalFree(c.cast(formatted, c.c_void_p))
    advapi32.FreeSid(sid)
    if userenv.DeleteAppContainerProfile(name) != 0:
        raise RuntimeError("profile cleanup failed: " + name)


def acl(path, *arguments):
    subprocess.run(["icacls", str(path), *arguments], check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="FAST", choices=sorted(BUDGETS))
    parser.add_argument("--timeout-probe", action="store_true", help="only check that the job is killed")
    arguments = parser.parse_args()
    mode = arguments.mode

    if not (RUNTIME / "ready").is_file():
        raise SystemExit("stage the AppContainer runtime first")
    model = load_model_env()
    provider = model_proxy.provider_from_environment(model)
    launcher_spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
        "ac", BASE / "scripts/probe-appcontainer.py")
    launcher = __import__("importlib.util", fromlist=["util"]).module_from_spec(launcher_spec)
    launcher_spec.loader.exec_module(launcher)

    engine = RUNTIME / "engine"
    engine.mkdir(exist_ok=True)
    for name in ("worker.py", "runtime.py", "sirchmunk_compat.py", "ipc.py", "llm_bridge.py"):
        shutil.copy2(BASE / "engine" / name, engine / name)
    shutil.copy2(BASE / "scripts/appcontainer-entry.py", engine / "entry.py")
    for name in ("rg", "rga"):
        source = PATCHED_RGA if (name == "rga" and PATCHED_RGA.is_file()) else executable(name, os.environ, BASE, Path.home())
        shutil.copy2(source, RUNTIME / (name + ".exe"))

    profile_name = "DSH.Model.Probe." + uuid.uuid4().hex
    profile = create_profile(profile_name)
    sid_string = profile[3].value
    report = {"mode": mode, "provider": {"model": provider["model"], "host": provider["base_url"].split("/")[2]},
              "keyInChild": False, "productionWorkerIsolated": False}
    pipe = None
    try:
        pipe = ipc.PipeServer("dsh-model-%s" % uuid.uuid4().hex[:8], sid_string=sid_string)
        pipe.listen()
        proxy = model_proxy.ModelProxy(pipe, provider,
                                       limits=model_proxy.Limits(deadline_seconds=BUDGETS[mode] + 60))
        thread, outcome = model_proxy.serve_in_thread(proxy)
        acl(RUNTIME, "/grant", "*" + sid_string + ":(OI)(CI)(RX)")

        with tempfile.TemporaryDirectory(prefix="isolated-model-", dir=BASE / ".local") as temporary:
            work = Path(temporary)
            state = work / "state"
            state.mkdir()
            (state / "rga-cache").mkdir()
            root = work / "original"
            root.mkdir()
            (root / "note-03.md").write_text("# synthetic\nWALNUT-03\n", encoding="utf-8")
            request = {"root": str(root), "workDir": str(work), "libraryId": "fixtures",
                       "query": "Which note contains the codename WALNUT-03?", "mode": mode}
            manifest, warnings = pipeline.build_snapshot(root, work)
            child = pipeline.worker_request(request, work)
            child["modelPipe"] = pipe.path
            child["modelName"] = provider["model"]
            config = work / "request.json"
            config.write_text(json.dumps({"request": child, "hang": arguments.timeout_probe,
                                          "deniedReads": {"original": str(root / "note-03.md")},
                                          "allowedRead": next(iter(manifest))}), encoding="utf-8")
            for path, rights in ((work, ":(RX)"), (config, ":(R)"), (work / "files", ":(OI)(CI)(RX)"),
                                 (state, ":(OI)(CI)(M)")):
                acl(path, "/grant", "*" + sid_string + rights)
            acl(state, "/setintegritylevel", "(OI)(CI)L")

            names = ("SystemRoot", "SystemDrive", "WINDIR", "ComSpec", "ProgramData")
            env = {key: os.environ[key] for key in names if key in os.environ}
            env.update({"USERPROFILE": str(state), "LOCALAPPDATA": str(state), "APPDATA": str(state),
                        "TEMP": str(state), "TMP": str(state), "RGA_CACHE_DIR": str(state / "rga-cache"),
                        "PATH": str(RUNTIME) + ";" + os.environ["SystemRoot"] + "\\System32",
                        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                        "DSH_KNOWLEDGE_RG": str(RUNTIME / "rg.exe"), "DSH_KNOWLEDGE_RGA": str(RUNTIME / "rga.exe")})
            # The child must not be able to see any provider credential.
            report["keyInChild"] = any(key.startswith("LLM_") or "API_KEY" in key for key in env)
            timeout = 8000 if arguments.timeout_probe else (BUDGETS[mode] + 60) * 1000
            try:
                code = launcher.run(profile[2], None,
                                    argv=[str(RUNTIME / "python.exe"), str(engine / "entry.py"), str(config)],
                                    environment=env, timeout_ms=timeout)
            except TimeoutError:
                code = 124
            report["exitCode"] = code
            boundaries = json.loads((state / "boundaries.json").read_text(encoding="utf-8")) if (state / "boundaries.json").exists() else None
            payload = json.loads((state / "result.json").read_text(encoding="utf-8")) if (state / "result.json").exists() else None
            report["rgaNoFallback"] = (boundaries or {}).get("rgaNoFallback")
            report["boundaries"] = {key: boundaries.get(key) for key in ("original", "outside", "snapshotRead",
                                                                         "snapshotWriteDenied", "stateWrite")} if boundaries else None
            if arguments.timeout_probe:
                report["passed"] = code == 124
                print(json.dumps(report))
                return 0 if report["passed"] else 1
            # Trusted parent: turn the engine's candidates into verified citations.
            result = pipeline.finalize(payload, manifest, request, warnings)
            report["result"] = {"status": result["status"], "warnings": result["warnings"][:6],
                                "evidence": result["evidence"]}
            report["proxy"] = {"calls": proxy.calls, "tokens": proxy.tokens, "audit": proxy.audit[-4:]}
            report["checks"] = {
                "engineReturnedCandidates": bool((payload or {}).get("candidates")),
                "modelCallWentThroughTheProxy": proxy.calls >= 1,
                "rgaWithoutFallback": bool((report["rgaNoFallback"] or {}).get("returncode") in (0, 1)
                                           and not (report["rgaNoFallback"] or {}).get("fallback")),
                "citationsVerifiedByTheParent": bool(result["evidence"]) and all(
                    item["startLine"] > 0 and item["excerpt"] for item in result["evidence"]),
                "noKeyInContainer": not report["keyInChild"],
            }
            report["passed"] = all(report["checks"].values())
        proxy.stop()
        thread.join(timeout=10)
    finally:
        if pipe is not None:
            pipe.close()
        drop_profile(profile[0], profile[1], profile[2], profile[3], profile_name)
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / ("report-%s.json" % mode)).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
