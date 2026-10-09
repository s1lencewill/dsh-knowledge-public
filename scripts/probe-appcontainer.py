"""Windows OS isolation primitive probe, NOT the production worker launcher.

Creates a temporary AppContainer profile and deletes it after the probe.
Only the temporary snapshot receives an ACL grant; no user/system ACL is changed.
"""
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


def run(sid, path, *, argv=None, environment=None, timeout_ms=10000):
    class SI(c.Structure):
        _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                    ("title", w.LPWSTR)] + [(n, w.DWORD) for n in
                    ("x", "y", "xs", "ys", "xc", "yc", "fill", "flags")] + [
                    ("show", w.WORD), ("reserved2", w.WORD), ("bytes", c.c_void_p),
                    ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]
    class SX(c.Structure):
        _fields_ = [("si", SI), ("attributes", c.c_void_p)]
    class PI(c.Structure):
        _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]
    class CAPS(c.Structure):
        _fields_ = [("sid", c.c_void_p), ("caps", c.c_void_p), ("count", w.DWORD), ("reserved", w.DWORD)]
    class LIMIT(c.Structure):
        _fields_ = [("processTime", c.c_int64), ("jobTime", c.c_int64), ("flags", w.DWORD),
                    ("minWorking", c.c_size_t), ("maxWorking", c.c_size_t), ("active", w.DWORD),
                    ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class EXTENDED(c.Structure):
        _fields_ = [("basic", LIMIT), ("io", c.c_uint64 * 6), ("processMemory", c.c_size_t),
                    ("jobMemory", c.c_size_t), ("peakProcess", c.c_size_t), ("peakJob", c.c_size_t)]
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.InitializeProcThreadAttributeList.argtypes = [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)]
    kernel.UpdateProcThreadAttribute.argtypes = [c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p]
    kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD, c.c_void_p, w.LPCWSTR, c.c_void_p, c.POINTER(PI)]
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.TerminateProcess.argtypes = [w.HANDLE, w.DWORD]
    kernel.DeleteProcThreadAttributeList.argtypes = [c.c_void_p]
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    kernel.ResumeThread.argtypes = [w.HANDLE]
    size = c.c_size_t()
    kernel.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
    attributes = c.create_string_buffer(size.value)
    if not kernel.InitializeProcThreadAttributeList(attributes, 1, 0, c.byref(size)):
        raise c.WinError(c.get_last_error())
    pi = PI()
    job = None
    try:
        job = kernel.CreateJobObjectW(None, None)
        limits = EXTENDED(); limits.basic.flags = 0x2000  # kill entire tree on handle close
        if not job or not kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)):
            raise c.WinError(c.get_last_error())
        caps = CAPS(sid, None, 0, 0)
        if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x20009, c.byref(caps), c.sizeof(caps), None, None):
            raise c.WinError(c.get_last_error())
        sx = SX(); sx.si.cb = c.sizeof(sx); sx.attributes = c.cast(attributes, c.c_void_p)
        exe = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        if argv:
            exe = str(argv[0])
        command = c.create_unicode_buffer(subprocess.list2cmdline(argv) if argv else f'"{exe}" /d /c type "{path}"')
        names = ("SystemRoot", "SystemDrive", "WINDIR", "TEMP", "TMP", "USERPROFILE",
                 "LOCALAPPDATA", "APPDATA", "ProgramData", "ComSpec", "PATH")
        selected = environment if environment is not None else {k: os.environ[k] for k in names if k in os.environ}
        env = c.create_unicode_buffer("\0".join(f"{k}={selected[k]}" for k in sorted(selected)) + "\0\0")
        if not kernel.CreateProcessW(exe, command, None, None, False, 0x08080404, env,
                                     os.environ["SystemRoot"], c.byref(sx), c.byref(pi)):
            raise c.WinError(c.get_last_error())
        if not kernel.AssignProcessToJobObject(job, pi.process):
            kernel.TerminateProcess(pi.process, 1)
            raise c.WinError(c.get_last_error())
        if kernel.ResumeThread(pi.thread) == -1:
            raise c.WinError(c.get_last_error())
        if kernel.WaitForSingleObject(pi.process, timeout_ms) != 0:
            kernel.TerminateJobObject(job, 1)
            kernel.WaitForSingleObject(pi.process, 10000)
            raise TimeoutError("AppContainer probe budget exceeded")
        code = w.DWORD()
        if not kernel.GetExitCodeProcess(pi.process, c.byref(code)):
            raise c.WinError(c.get_last_error())
        return code.value
    finally:
        if job: kernel.CloseHandle(job)
        if pi.thread: kernel.CloseHandle(pi.thread)
        if pi.process: kernel.CloseHandle(pi.process)
        kernel.DeleteProcThreadAttributeList(attributes)


def main():
    if os.name != "nt":
        raise RuntimeError("Windows required")
    userenv = c.WinDLL("userenv")
    userenv.CreateAppContainerProfile.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD, c.POINTER(c.c_void_p)]
    userenv.DeleteAppContainerProfile.argtypes = [w.LPCWSTR]
    sid = c.c_void_p()
    profile = "DSH.Knowledge.Probe." + uuid.uuid4().hex
    result = userenv.CreateAppContainerProfile(profile, profile, "Temporary synthetic isolation probe", None, 0, c.byref(sid))
    if result != 0:
        raise RuntimeError(f"Cannot create temporary AppContainer: HRESULT {result:#x}")
    adv = c.WinDLL("advapi32")
    adv.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    adv.FreeSid.argtypes = [c.c_void_p]
    value = w.LPWSTR()
    if not adv.ConvertSidToStringSidW(sid, c.byref(value)):
        adv.FreeSid(sid)
        userenv.DeleteAppContainerProfile(profile)
        raise RuntimeError("Cannot format SID")
    try:
        with tempfile.TemporaryDirectory(prefix="appcontainer-", dir=Path(__file__).resolve().parents[1] / ".local") as temp:
            root = Path(temp); snapshot = root / "snapshot"; snapshot.mkdir()
            allowed = snapshot / "allowed.txt"; allowed.write_text("synthetic allowed")
            outside = root / "outside.txt"; outside.write_text("synthetic private canary")
            host = subprocess.run([os.environ["ComSpec"], "/d", "/c", "type", str(outside)], capture_output=True)
            if host.returncode != 0 or b"synthetic private canary" not in host.stdout:
                raise RuntimeError("Host canary control failed")
            subprocess.run(["icacls", str(root), "/grant", "*" + value.value + ":(RX)"], check=True, capture_output=True)
            subprocess.run(["icacls", str(snapshot), "/grant", "*" + value.value + ":(OI)(CI)(RX)"], check=True, capture_output=True)
            baseline = run(sid, allowed)
            denied = run(sid, outside)
            report = {"snapshotExit": baseline, "outsideExit": denied,
                      "hostOutsideReadable": True,
                      "primitivePassed": baseline == 0 and denied != 0,
                      "productionWorkerIsolated": False}
            print(json.dumps(report))
            return 0 if report["primitivePassed"] else 1
    finally:
        kernel = c.WinDLL("kernel32"); kernel.LocalFree.argtypes = [c.c_void_p]
        kernel.LocalFree(c.cast(value, c.c_void_p)); adv.FreeSid(sid)
        if userenv.DeleteAppContainerProfile(profile) != 0:
            raise RuntimeError("Temporary AppContainer profile cleanup failed: " + profile)


if __name__ == "__main__":
    raise SystemExit(main())
