"""Minimal IPC prototype across the AppContainer boundary.

Creates two temporary AppContainer profiles - the job and an unrelated "intruder" container - and
checks, with the real launcher used by the engine probes:

* the job container can talk to a pipe whose DACL grants its SID (global and LOCAL\\ namespaces);
* the job container is denied a pipe that does not grant its SID;
* a different container SID is denied the granted pipes;
* a disconnect is detected and the pipe name can be reused afterwards.

Run with the project Python; it touches no existing DSH profile and no user ACL.
"""
import ctypes as c
from ctypes import wintypes as w
import importlib.util
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

RUNTIME = BASE / ".local/appcontainer-runtime"
LOGS = BASE / ".local/verification/appcontainer-ipc"


def load_launcher():
    spec = importlib.util.spec_from_file_location("ac", BASE / "scripts/probe-appcontainer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_profile(name):
    """Returns (userenv, advapi32, sid, sid_string_buffer)."""
    userenv = c.WinDLL("userenv")
    userenv.CreateAppContainerProfile.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD,
                                                  c.POINTER(c.c_void_p)]
    userenv.DeleteAppContainerProfile.argtypes = [w.LPCWSTR]
    sid = c.c_void_p()
    if userenv.CreateAppContainerProfile(name, name, "Temporary IPC probe", None, 0, c.byref(sid)) != 0:
        raise RuntimeError("cannot create AppContainer %s" % name)
    advapi32 = c.WinDLL("advapi32")
    advapi32.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    advapi32.FreeSid.argtypes = [c.c_void_p]
    formatted = w.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, c.byref(formatted)):
        advapi32.FreeSid(sid)
        userenv.DeleteAppContainerProfile(name)
        raise RuntimeError("cannot format SID for %s" % name)
    return userenv, advapi32, sid, formatted


def drop_profile(userenv, advapi32, sid, formatted, name):
    kernel32 = c.WinDLL("kernel32")
    kernel32.LocalFree.argtypes = [c.c_void_p]
    kernel32.LocalFree(c.cast(formatted, c.c_void_p))
    advapi32.FreeSid(sid)
    if userenv.DeleteAppContainerProfile(name) != 0:
        raise RuntimeError("profile cleanup failed: " + name)


class Served:
    """Provides one pipe: answers one request, then notices the disconnect."""

    def __init__(self, label, pipe):
        self.label = label
        self.pipe = pipe
        self.request = None
        self.disconnected = False
        self.error = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            if not self.pipe.wait_connected(60):
                self.error = self.pipe.error or "no client connected"
                return
            self.request = self.pipe.receive(timeout=60)
            self.pipe.send({"server": self.label, "echo": self.request})
            self.disconnected = self.pipe.receive(timeout=60) is None
        except Exception as error:
            self.error = repr(error)

    def close(self):
        self.pipe.close()


def acl(path, *arguments):
    subprocess.run(["icacls", str(path), *arguments], check=True, capture_output=True)


def main():
    if not (RUNTIME / "ready").is_file():
        raise SystemExit("stage the AppContainer runtime first (scripts/stage-appcontainer-runtime.py)")
    launcher = load_launcher()
    engine = RUNTIME / "engine"
    engine.mkdir(exist_ok=True)
    shutil.copy2(BASE / "engine" / "ipc.py", engine / "ipc.py")
    shutil.copy2(BASE / "scripts/ipc-entry.py", engine / "ipc_entry.py")

    token = uuid.uuid4().hex[:8]
    job_name, other_name = "DSH.Ipc.Job." + token, "DSH.Ipc.Other." + token
    job = create_profile(job_name)
    other = create_profile(other_name)
    job_sid, other_sid = job[3].value, other[3].value
    served, report = [], {"paths": {}, "job": {}, "intruder": {}, "cleanup": {}}
    try:
        pipes = {
            "granted-global": ipc.PipeServer("dsh-ipc-%s" % token, sid_string=job_sid),
            "granted-local": ipc.PipeServer("LOCAL\\dsh-ipc-%s" % token, sid_string=job_sid),
            "ungranted": ipc.PipeServer("dsh-ipc-%s-none" % token),
        }
        for label, server in pipes.items():
            server.listen()
            served.append(Served(label, server))
        report["paths"] = {label: server.path for label, server in pipes.items()}

        with tempfile.TemporaryDirectory(prefix="ipc-probe-", dir=BASE / ".local") as temporary:
            work = Path(temporary)
            # Both containers need the staged runtime; only the job SID gets the pipe rights.
            for sid_text in (job_sid, other_sid):
                acl(RUNTIME, "/grant", "*" + sid_text + ":(OI)(CI)(RX)")

            def entry(container_sid, sid_string, label, selected, create_attempt=None):
                directory = work / label
                directory.mkdir(exist_ok=True)
                config = directory / "config.json"
                payload = {"stateDir": str(directory), "pipes": selected}
                if create_attempt:
                    payload["createAttempt"] = create_attempt
                config.write_text(json.dumps(payload), encoding="utf-8")
                # The container may write only inside its own probe directory.
                acl(directory, "/grant", "*" + sid_string + ":(OI)(CI)(M)")
                acl(directory, "/setintegritylevel", "(OI)(CI)L")
                names = ("SystemRoot", "SystemDrive", "WINDIR", "ComSpec", "ProgramData")
                env = {key: os.environ[key] for key in names if key in os.environ}
                env.update({"USERPROFILE": str(directory), "LOCALAPPDATA": str(directory), "APPDATA": str(directory),
                            "TEMP": str(directory), "TMP": str(directory), "PYTHONUTF8": "1",
                            "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                            "PATH": str(RUNTIME) + ";" + os.environ["SystemRoot"] + "\\System32"})
                try:
                    code = launcher.run(container_sid, None,
                                        argv=[str(RUNTIME / "python.exe"), str(engine / "ipc_entry.py"), str(config)],
                                        environment=env, timeout_ms=60000)
                except TimeoutError:
                    code = 124
                results = {}
                if (directory / "ipc.json").exists():
                    results = json.loads((directory / "ipc.json").read_text(encoding="utf-8"))
                return code, results

            code, payload = entry(job[2], job_sid, "job", report["paths"],
                                  create_attempt="dsh-ipc-%s" % token)
            report["job"] = {"exitCode": code, "results": payload.get("results", {}),
                             "createAttempt": payload.get("createAttempt")}
            code, payload = entry(other[2], other_sid, "intruder",
                                  {label: path for label, path in report["paths"].items() if label != "ungranted"})
            report["intruder"] = {"exitCode": code, "results": payload.get("results", {})}

        for serviced in served:
            serviced.thread.join(timeout=70)
        report["served"] = {serviced.label: {"request": serviced.request,
                                             "disconnected": serviced.disconnected,
                                             "error": serviced.error} for serviced in served}

        # The name must be free again: a leaked instance makes this fail with ERROR_ACCESS_DENIED.
        replacement = ipc.PipeServer("dsh-ipc-%s" % token, sid_string=job_sid).listen()
        report["cleanup"]["nameReusable"] = replacement.handle != ipc.INVALID_HANDLE_VALUE
        replacement.close()
        for serviced in served:
            serviced.close()

        job_results, intruder_results = report["job"]["results"], report["intruder"]["results"]
        granted_labels = ("granted-global", "granted-local")
        used_pipes = [serviced for serviced in served if serviced.request is not None]
        report["checks"] = {
            # The container reaches a parent-created pipe by its global name; the LOCAL\ prefix is
            # reported for information only (a non-AppContainer-created pipe does not appear there).
            "jobConnectedOnGlobalPipe": bool(job_results.get("granted-global", {}).get("connected")),
            "jobDeniedOnUngrantedPipe": job_results.get("ungranted", {}).get("connected") is False,
            "intruderDeniedOnGrantedPipes": all(intruder_results.get(label, {}).get("connected") is False
                                                for label in granted_labels),
            "containerCannotTakeOverTheName": bool((report["job"].get("createAttempt") or {}).get("denied")),
            "crossBoundaryRoundTrip": any((serviced.request or {}).get("hello") == serviced.label
                                          for serviced in used_pipes),
            "disconnectObserved": all(serviced.disconnected for serviced in used_pipes) and bool(used_pipes),
            "nameReusableAfterDisconnect": bool(report["cleanup"]["nameReusable"]),
        }
        report["passed"] = all(report["checks"].values())
    finally:
        for serviced in served:
            serviced.close()
        drop_profile(job[0], job[1], job[2], job[3], job_name)
        drop_profile(other[0], other[1], other[2], other[3], other_name)
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
