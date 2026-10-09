"""Real FILENAME_ONLY engine under AppContainer. No model credentials or network capability.

Prerequisite: project-local staged Python runtime (.local/appcontainer-runtime).
This is an explicit harness, not a production service switch or fallback.
"""
import ctypes as c
from ctypes import wintypes as w
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "engine"))
import pipeline
from runtime import executable

PATCHED_RGA = BASE / ".local/tools/rga-patched/rga.exe"


def main():
    timeout_probe = "--timeout-probe" in sys.argv
    runtime = BASE / ".local/appcontainer-runtime"
    if not (runtime / "ready").is_file():
        raise RuntimeError("Stage runtime first")
    engine = runtime / "engine"; engine.mkdir(exist_ok=True)
    # The isolated payload only needs the candidate producer and its compat shims; the trusted
    # parent (pipeline/evidence) never runs inside the container.
    for name in ("worker.py", "runtime.py", "sirchmunk_compat.py"):
        shutil.copy2(BASE / "engine" / name, engine / name)
    shutil.copy2(BASE / "scripts/appcontainer-entry.py", engine / "entry.py")
    for name in ("rg", "rga"):
        source = PATCHED_RGA if (name == "rga" and PATCHED_RGA.is_file()) else executable(name, os.environ, BASE, Path.home())
        shutil.copy2(source, runtime / (name + ".exe"))
    spec = importlib.util.spec_from_file_location("ac", BASE / "scripts/probe-appcontainer.py")
    ac = importlib.util.module_from_spec(spec); spec.loader.exec_module(ac)
    api = c.WinDLL("userenv")
    api.CreateAppContainerProfile.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD, c.POINTER(c.c_void_p)]
    api.DeleteAppContainerProfile.argtypes = [w.LPCWSTR]
    adv = c.WinDLL("advapi32")
    adv.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    adv.FreeSid.argtypes = [c.c_void_p]
    profile = "DSH.Engine.Probe." + uuid.uuid4().hex
    sid = c.c_void_p(); formatted = w.LPWSTR()
    if api.CreateAppContainerProfile(profile, profile, "Temporary engine probe", None, 0, c.byref(sid)) != 0:
        raise RuntimeError("AppContainer creation failed")
    granted_runtime = False
    report = {"productionWorkerIsolated": False}
    try:
        if not adv.ConvertSidToStringSidW(sid, c.byref(formatted)):
            raise RuntimeError("SID conversion failed")
        principal = "*" + formatted.value
        def acl(path, *args):
            subprocess.run(["icacls", str(path), *args], check=True, capture_output=True)
        granted_runtime = True
        acl(runtime, "/grant", principal + ":(OI)(CI)(RX)")
        with tempfile.TemporaryDirectory(prefix="isolated-engine-", dir=BASE / ".local") as temporary:
            work = Path(temporary); state = work / "state"; state.mkdir()
            root = work / "original"; root.mkdir()
            original = root / "note-03.md"; original.write_text("# synthetic\nWALNUT-03\n", encoding="utf-8")
            outside = work / "outside.txt"; outside.write_text("synthetic canary")
            request = {"root": str(root), "workDir": str(work), "libraryId": "fixtures",
                       "query": "note-03", "mode": "FILENAME_ONLY"}
            manifest, warnings = pipeline.build_snapshot(root, work)
            child = pipeline.worker_request(request, work)
            config = work / "request.json"
            config.write_text(json.dumps({"request": child, "hang": timeout_probe, "deniedReads": {"original": str(original), "outside": str(outside)},
                                          "allowedRead": next(iter(manifest))}), encoding="utf-8")
            acl(work, "/grant", principal + ":(RX)")
            acl(config, "/grant", principal + ":(R)")
            acl(work / "files", "/grant", principal + ":(OI)(CI)(RX)")
            acl(state, "/grant", principal + ":(OI)(CI)(M)")
            acl(state, "/setintegritylevel", "(OI)(CI)L")
            names = ("SystemRoot", "SystemDrive", "WINDIR", "ProgramData", "ComSpec")
            env = {k: os.environ[k] for k in names if k in os.environ}
            rga_project = state / "rga-project"
            rga_project.mkdir(exist_ok=True)
            rga_cache = state / "rga-cache"
            rga_cache.mkdir(exist_ok=True)
            env.update({"USERPROFILE": str(state), "LOCALAPPDATA": str(state), "APPDATA": str(state),
                        "TEMP": str(state), "TMP": str(state),
                        "DSH_RGA_PROJECT_DIR": str(rga_project), "RGA_CACHE_DIR": str(rga_cache),
                        "PATH": str(runtime) + ";" + str(runtime / "Library/bin") + ";" + os.environ["SystemRoot"] + "\\System32",
                        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                        "DSH_KNOWLEDGE_RG": str(runtime / "rg.exe"), "DSH_KNOWLEDGE_RGA": str(runtime / "rga.exe")})
            try:
                code = ac.run(sid, None, argv=[str(runtime / "python.exe"), str(engine / "entry.py"), str(config)],
                              environment=env, timeout_ms=6000 if timeout_probe else 60000)
            except TimeoutError:
                code = 124
            report["exitCode"] = code
            for name in ("boundaries", "result"):
                path = state / (name + ".json")
                report[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            logs = BASE / ".local/verification" / ("appcontainer-timeout" if timeout_probe else "appcontainer-engine")
            logs.mkdir(parents=True, exist_ok=True)
            for name in ("stdout.txt", "stderr.txt"):
                if (state / name).exists(): shutil.copy2(state / name, logs / name)
            report["filenameAndBoundaryPassed"] = code == 0 and bool(report["boundaries"]) and all([
                report["boundaries"]["original"]["denied"], report["boundaries"]["outside"]["denied"],
                report["boundaries"]["snapshotRead"], report["boundaries"]["snapshotWriteDenied"], report["boundaries"]["stateWrite"]
            ]) and "note-03.md" in [x["name"] for x in (report["result"] or {}).get("matches", [])]
            tools = (report["boundaries"] or {}).get("tools", {})
            no_fallback = (report["boundaries"] or {}).get("rgaNoFallback") or {}
            staged_rga = runtime / "rga.exe"
            report["rgaBareExit"] = tools.get("rga", {}).get("exit")
            report["rgaCacheFlag"] = (report["boundaries"] or {}).get("rgaCacheFlag")
            report["rgaNoFallback"] = no_fallback
            report["rgaBinary"] = str(PATCHED_RGA.relative_to(BASE)) if PATCHED_RGA.is_file() else "upstream pinned 0.10.9"
            report["rgaPatched"] = PATCHED_RGA.is_file()
            report["rgaSha256"] = hashlib.sha256(staged_rga.read_bytes()).hexdigest() if staged_rga.is_file() else None
            report["rgaPassed"] = bool(
                report["rgaBareExit"] == 0
                and str(report["rgaCacheFlag"] or "").startswith("--rga-cache-path=")
                and no_fallback.get("returncode") in (0, 1)
                and not no_fallback.get("fallback")
                and "note-03.md" in (no_fallback.get("stdout") or ""))
            report["passed"] = report["filenameAndBoundaryPassed"] and report["rgaPassed"]
            if timeout_probe:
                marker = state / "child-pid"
                report["childStarted"] = marker.exists()
                report["childStopped"] = False
                if marker.exists():
                    kernel = c.WinDLL("kernel32", use_last_error=True)
                    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; kernel.OpenProcess.restype = w.HANDLE
                    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
                    kernel.CloseHandle.argtypes = [w.HANDLE]
                    handle = kernel.OpenProcess(0x100000, False, int(marker.read_text()))
                    if handle:
                        report["childStopped"] = kernel.WaitForSingleObject(handle, 5000) == 0
                        kernel.CloseHandle(handle)
                    else:
                        report["childStopped"] = c.get_last_error() == 87
                report["passed"] = code == 124 and report["childStarted"] and report["childStopped"]
    finally:
        try:
            if granted_runtime:
                subprocess.run(["icacls", str(runtime), "/remove:g", "*" + formatted.value], check=True, capture_output=True)
        finally:
            if formatted:
                kernel = c.WinDLL("kernel32"); kernel.LocalFree.argtypes = [c.c_void_p]
                kernel.LocalFree(c.cast(formatted, c.c_void_p))
            adv.FreeSid(sid)
            if api.DeleteAppContainerProfile(profile) != 0:
                raise RuntimeError("Profile cleanup failed: " + profile)
    report["cleanupCompleted"] = True
    (logs / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
