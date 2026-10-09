"""Production AppContainer launcher: profile lifecycle, SID-scoped grants, killable container jobs.

Extracted from the probe that validated the primitive so the service can run the engine inside a
container:

* one profile per service instance, created at startup and deleted at shutdown; stale profiles from a
  crashed parent are swept at startup because nobody else can clean them up;
* grants are SID-scoped and only ever touch the staged runtime and the job directory - never the
  library, the user profile or the Python installation;
* every job runs inside a Job Object with KILL_ON_JOB_CLOSE, so terminating (or the parent dying)
  kills the whole container process tree.

Isolation is a hard requirement at the call site: nothing here falls back to a plain process.
"""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess


class IsolationError(RuntimeError):
    """Raised when the isolated path cannot be set up; callers must fail loudly, never degrade."""


PROFILE_PREFIX = "DSH.Knowledge.Svc."
STORAGE_KEY = (r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion"
               r"\AppContainer\Storage")
CREATE_FLAGS = 0x08080404  # proven in the primitive probe: suspended + unicode env + no window
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
KILL_ON_JOB_CLOSE = 0x2000
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x20009
INFINITE = 0xFFFFFFFF


def available():
    return os.name == "nt"


class _Structures:
    """ctypes structures and prototypes shared by the profile and job helpers."""

    def __init__(self):
        class STARTUPINFO(c.Structure):
            _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                        ("title", w.LPWSTR)] + [(name, w.DWORD) for name in
                        ("x", "y", "xs", "ys", "xc", "yc", "fill", "flags")] + [
                        ("show", w.WORD), ("reserved2", w.WORD), ("bytes", c.c_void_p),
                        ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]

        class STARTUPINFOEX(c.Structure):
            _fields_ = [("si", STARTUPINFO), ("attributes", c.c_void_p)]

        class PROCESS_INFORMATION(c.Structure):
            _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]

        class SECURITY_CAPABILITIES(c.Structure):
            _fields_ = [("sid", c.c_void_p), ("caps", c.c_void_p), ("count", w.DWORD), ("reserved", w.DWORD)]

        class LIMIT(c.Structure):
            _fields_ = [("processTime", c.c_int64), ("jobTime", c.c_int64), ("flags", w.DWORD),
                        ("minWorking", c.c_size_t), ("maxWorking", c.c_size_t), ("active", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class EXTENDED(c.Structure):
            _fields_ = [("basic", LIMIT), ("io", c.c_uint64 * 6), ("processMemory", c.c_size_t),
                        ("jobMemory", c.c_size_t), ("peakProcess", c.c_size_t), ("peakJob", c.c_size_t)]

        self.STARTUPINFOEX = STARTUPINFOEX
        self.PROCESS_INFORMATION = PROCESS_INFORMATION
        self.SECURITY_CAPABILITIES = SECURITY_CAPABILITIES
        self.EXTENDED = EXTENDED
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.userenv = c.WinDLL("userenv")
        self.advapi32 = c.WinDLL("advapi32")
        kernel = self.kernel
        kernel.InitializeProcThreadAttributeList.argtypes = [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)]
        kernel.UpdateProcThreadAttribute.argtypes = [c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p,
                                                     c.c_size_t, c.c_void_p, c.c_void_p]
        kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD,
                                          c.c_void_p, w.LPCWSTR, c.c_void_p, c.POINTER(PROCESS_INFORMATION)]
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        kernel.TerminateProcess.argtypes = [w.HANDLE, w.DWORD]
        kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.ResumeThread.argtypes = [w.HANDLE]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.DeleteProcThreadAttributeList.argtypes = [c.c_void_p]
        kernel.LocalFree.argtypes = [c.c_void_p]
        self.userenv.CreateAppContainerProfile.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, c.c_void_p,
                                                           w.DWORD, c.POINTER(c.c_void_p)]
        self.userenv.DeleteAppContainerProfile.argtypes = [w.LPCWSTR]
        self.advapi32.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
        self.advapi32.FreeSid.argtypes = [c.c_void_p]


_api = None


def _structures():
    global _api
    if _api is None:
        if not available():
            raise IsolationError("AppContainer isolation requires Windows")
        _api = _Structures()
    return _api


class Profile:
    """A temporary AppContainer profile identified by name."""

    def __init__(self, name, sid=None, sid_string=None):
        self.name = name
        self.sid = sid
        self.sid_string = sid_string

    @classmethod
    def create(cls, name):
        api = _structures()
        sid = c.c_void_p()
        if api.userenv.CreateAppContainerProfile(name, name, "DSH knowledge engine", None, 0,
                                                 c.byref(sid)) != 0:
            raise IsolationError("cannot create AppContainer profile %s" % name)
        formatted = w.LPWSTR()
        if not api.advapi32.ConvertSidToStringSidW(sid, c.byref(formatted)):
            api.advapi32.FreeSid(sid)
            api.userenv.DeleteAppContainerProfile(name)
            raise IsolationError("cannot format the SID of %s" % name)
        return cls(name, sid, formatted)

    def delete(self):
        api = _structures()
        if self.sid:
            api.advapi32.FreeSid(self.sid)
            self.sid = None
        if api.userenv.DeleteAppContainerProfile(self.name) != 0:
            raise IsolationError("cannot delete AppContainer profile %s" % self.name)


def reclaim_stale_profile(state_file):
    """Delete the profile recorded by a previous service instance, then drop the record.

    Profiles created with CreateAppContainerProfile do NOT appear under the AppContainer Storage
    registry key (that key lists packaged-app containers), so a crashed parent cannot be found by
    enumeration. The service therefore records its profile name and reclaims it at the next start.
    Returns the reclaimed name, or None.
    """
    import json
    path = Path(state_file)
    if not path.is_file():
        return None
    try:
        name = json.loads(path.read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError):
        name = None
    if not name:
        path.unlink(missing_ok=True)
        return None
    try:
        Profile(name).delete()
    except IsolationError:
        pass
    path.unlink(missing_ok=True)
    return name


def record_profile(state_file, name, pid=None):
    import json
    import time
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "pid": pid, "startedAt": int(time.time())}), encoding="utf-8")


def grant(path, sid_string, rights):
    """Grant an ACL to one AppContainer SID. `rights` is an icacls right specification."""
    result = subprocess.run(["icacls", str(path), "/grant", "*" + sid_string + rights],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise IsolationError("cannot grant %s on %s: %s" % (rights, path, (result.stderr or "").strip()[:200]))


def revoke(path, sid_string):
    subprocess.run(["icacls", str(path), "/remove:g", "*" + sid_string], capture_output=True, text=True)


def set_low_integrity(path):
    """Best effort: mark the job's state directory low integrity.

    Lowering the integrity label is an optional defence-in-depth layer on top of the ACL - the
    read-only snapshot is already enforced by permissions. Some volumes (this project's G: drive
    among them) reject the relabel operation with access denied, so the result is reported instead
    of raised and the caller records it.
    """
    result = subprocess.run(["icacls", str(path), "/setintegritylevel", "(OI)(CI)L"],
                            capture_output=True, text=True)
    if result.returncode == 0:
        return True, ""
    return False, ((result.stdout or "") + (result.stderr or "")).strip()[:200]


class ContainerJob:
    """One container process tree, killable as a unit."""

    def __init__(self):
        api = _structures()
        self.api = api
        self.job = None
        self.process = None
        self.thread = None
        self.pid = None
        self.exit_code = None
        self.terminated = False
        self.attributes = None

    def start(self, sid, argv, environment, cwd=None):
        try:
            return self._start(sid, argv, environment, cwd)
        except BaseException:
            # Never leak a job object or process handle when the launch fails halfway.
            self.close()
            raise

    def _start(self, sid, argv, environment, cwd=None):
        api = self.api
        size = c.c_size_t()
        api.kernel.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
        self.attributes = c.create_string_buffer(size.value)
        if not api.kernel.InitializeProcThreadAttributeList(self.attributes, 1, 0, c.byref(size)):
            raise IsolationError("InitializeProcThreadAttributeList: %s" % c.WinError(c.get_last_error()))
        self.job = api.kernel.CreateJobObjectW(None, None)
        if not self.job:
            raise IsolationError("CreateJobObject: %s" % c.WinError(c.get_last_error()))
        limits = api.EXTENDED()
        limits.basic.flags = KILL_ON_JOB_CLOSE
        if not api.kernel.SetInformationJobObject(self.job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                  c.byref(limits), c.sizeof(limits)):
            raise IsolationError("SetInformationJobObject: %s" % c.WinError(c.get_last_error()))
        capabilities = api.SECURITY_CAPABILITIES(sid, None, 0, 0)
        if not api.kernel.UpdateProcThreadAttribute(self.attributes, 0,
                                                    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                                                    c.byref(capabilities), c.sizeof(capabilities), None, None):
            raise IsolationError("UpdateProcThreadAttribute: %s" % c.WinError(c.get_last_error()))
        info = api.STARTUPINFOEX()
        info.si.cb = c.sizeof(info)
        info.attributes = c.cast(self.attributes, c.c_void_p)
        executable = str(argv[0])
        command = c.create_unicode_buffer(subprocess.list2cmdline([str(part) for part in argv]))
        block = "\0".join("%s=%s" % (key, environment[key]) for key in sorted(environment)) + "\0\0"
        environment_buffer = c.create_unicode_buffer(block)
        created = api.PROCESS_INFORMATION()
        if not api.kernel.CreateProcessW(executable, command, None, None, False, CREATE_FLAGS,
                                         environment_buffer, cwd or os.environ.get("SystemRoot"),
                                         c.byref(info), c.byref(created)):
            raise IsolationError("CreateProcessW: %s" % c.WinError(c.get_last_error()))
        self.process, self.thread, self.pid = created.process, created.thread, created.pid
        if not api.kernel.AssignProcessToJobObject(self.job, self.process):
            self.terminate()
            raise IsolationError("AssignProcessToJobObject: %s" % c.WinError(c.get_last_error()))
        if api.kernel.ResumeThread(self.thread) == -1:
            self.terminate()
            raise IsolationError("ResumeThread: %s" % c.WinError(c.get_last_error()))
        return self

    def wait(self, timeout_ms):
        """Blocking wait; raises TimeoutError when the budget expires (the job keeps running)."""
        if self.api.kernel.WaitForSingleObject(self.process, timeout_ms) != 0:
            raise TimeoutError("container job exceeded %d ms" % timeout_ms)
        code = w.DWORD()
        if not self.api.kernel.GetExitCodeProcess(self.process, c.byref(code)):
            raise IsolationError("GetExitCodeProcess: %s" % c.WinError(c.get_last_error()))
        self.exit_code = code.value
        return self.exit_code

    def terminate(self):
        self.terminated = True
        if self.job:
            self.api.kernel.TerminateJobObject(self.job, 1)
        elif self.process:
            self.api.kernel.TerminateProcess(self.process, 1)

    def close(self):
        api = self.api
        if self.job:
            api.kernel.CloseHandle(self.job)
            self.job = None
        if self.thread:
            api.kernel.CloseHandle(self.thread)
            self.thread = None
        if self.process:
            api.kernel.CloseHandle(self.process)
            self.process = None
        if self.attributes is not None:
            api.kernel.DeleteProcThreadAttributeList(self.attributes)
            self.attributes = None


def container_environment(state_dir, runtime, extra=None):
    """The minimal environment a container payload gets: OS basics, no credentials, no library."""
    names = ("SystemRoot", "SystemDrive", "WINDIR", "ComSpec", "ProgramData")
    environment = {key: os.environ[key] for key in names if key in os.environ}
    environment.update({
        "USERPROFILE": str(state_dir), "LOCALAPPDATA": str(state_dir), "APPDATA": str(state_dir),
        "TEMP": str(state_dir), "TMP": str(state_dir),
        "RGA_CACHE_DIR": str(Path(state_dir) / "rga-cache"),
        "PATH": str(runtime) + ";" + os.environ.get("SystemRoot", "C:\\Windows") + "\\System32",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        # The runtime is a copy of the base interpreter prefix, not a venv, so user site-packages
        # would otherwise be searched *ahead of* the staged Lib/site-packages. Inside the container
        # that copy lives in the user profile, which the ACL happens to deny - relying on that would
        # make "the image is self-contained" an accident. Refuse the directory outright.
        "PYTHONNOUSERSITE": "1",
        "DSH_KNOWLEDGE_RG": str(Path(runtime) / "rg.exe"),
        "DSH_KNOWLEDGE_RGA": str(Path(runtime) / "rga.exe"),
    })
    environment.update(extra or {})
    return environment
