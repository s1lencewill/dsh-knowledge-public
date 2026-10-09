"""SID-scoped named-pipe IPC used to cross the AppContainer boundary.

The trusted parent creates a pipe whose DACL grants exactly one AppContainer SID read/write;
a different container SID is denied by the same ACL. Framing is a 4-byte little-endian length
followed by UTF-8 JSON, and the length is validated against a hard cap before any body read, so an
untrusted child can never make the parent allocate an unbounded buffer.

Everything here is stdlib ctypes, so the same module runs in the parent and inside the container.
"""
import ctypes as c
from ctypes import wintypes as w
import json
import struct
import threading
import time

MAX_MESSAGE = 256 * 1024
FILE_GENERIC_READ = 0x120089
FILE_GENERIC_WRITE = 0x120116
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_UNLIMITED_INSTANCES = 255
INVALID_HANDLE_VALUE = c.c_void_p(-1).value
ERROR_PIPE_BUSY = 231
ERROR_PIPE_CONNECTED = 535
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_LISTENING = 536
ERROR_SEM_TIMEOUT = 121
GRANT_ACCESS = 1
TRUSTEE_IS_SID = 0
FILE_ALL_ACCESS = 0x001F01FF
# A pipe client needs read, write, attribute/EA access and synchronize (0x12019B):
# FILE_GENERIC_READ | FILE_GENERIC_WRITE minus FILE_APPEND_DATA. FILE_APPEND_DATA shares its bit
# with FILE_CREATE_PIPE_INSTANCE on named pipes, so excluding it is what stops the container from
# creating another instance of the same name and hijacking the parent's pipe.
PIPE_CLIENT_ACCESS = 0x0012019B
TOKEN_QUERY = 0x0008
TOKEN_USER = 1

kernel32 = c.WinDLL("kernel32", use_last_error=True)
advapi32 = c.WinDLL("advapi32", use_last_error=True)


class TRUSTEE_W(c.Structure):
    _fields_ = [("pMultipleTrustee", c.c_void_p), ("MultipleTrusteeOperation", w.DWORD),
                ("TrusteeForm", w.DWORD), ("TrusteeType", w.DWORD), ("ptstrName", c.c_void_p)]


class EXPLICIT_ACCESS_W(c.Structure):
    _fields_ = [("grfAccessPermissions", w.DWORD), ("grfAccessMode", w.DWORD),
                ("grfInheritance", w.DWORD), ("Trustee", TRUSTEE_W)]


class SECURITY_ATTRIBUTES(c.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", c.c_void_p), ("bInheritHandle", w.BOOL)]


class SECURITY_DESCRIPTOR(c.Structure):
    _fields_ = [("Revision", c.c_ubyte), ("Sbz1", c.c_ubyte), ("Control", c.c_ushort),
                ("Owner", c.c_void_p), ("Group", c.c_void_p), ("Sacl", c.c_void_p), ("Dacl", c.c_void_p)]


advapi32.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, c.POINTER(c.c_void_p)]
advapi32.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
advapi32.SetEntriesInAclW.argtypes = [w.DWORD, c.POINTER(EXPLICIT_ACCESS_W), c.c_void_p, c.POINTER(c.c_void_p)]
advapi32.InitializeSecurityDescriptor.argtypes = [c.POINTER(SECURITY_DESCRIPTOR), w.DWORD]
advapi32.SetSecurityDescriptorDacl.argtypes = [c.POINTER(SECURITY_DESCRIPTOR), w.BOOL, c.c_void_p, w.BOOL]
advapi32.IsValidSecurityDescriptor.argtypes = [c.POINTER(SECURITY_DESCRIPTOR)]
advapi32.FreeSid.argtypes = [c.c_void_p]
kernel32.LocalFree.argtypes = [c.c_void_p]
kernel32.CreateNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, c.c_void_p]
kernel32.CreateNamedPipeW.restype = w.HANDLE
kernel32.ConnectNamedPipe.argtypes = [w.HANDLE, c.c_void_p]
kernel32.DisconnectNamedPipe.argtypes = [w.HANDLE]
kernel32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
kernel32.CreateFileW.restype = w.HANDLE
kernel32.WaitNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD]
kernel32.ReadFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
kernel32.WriteFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
kernel32.PeekNamedPipe.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.c_void_p, c.POINTER(w.DWORD), c.c_void_p]
kernel32.CancelIoEx.argtypes = [w.HANDLE, c.c_void_p]
kernel32.CloseHandle.argtypes = [w.HANDLE]


class PipeError(RuntimeError):
    pass


class PipeDisconnected(PipeError):
    pass


class PipeTimeout(PipeError):
    pass


class PipeMessageTooLarge(PipeError):
    pass


class _AclHolder:
    """Keeps the native SIDs/ACL/descriptor buffers alive as long as the pipe exists.

    The DACL grants the parent user FILE_ALL_ACCESS - without FILE_CREATE_PIPE_INSTANCE the parent
    itself could not create a second instance of the same name later - and the job's AppContainer
    SID read/write only. The container therefore cannot hijack the name.
    """

    def __init__(self, sid_string, owner_sid_string=None):
        self.sid = c.c_void_p()
        if not advapi32.ConvertStringSidToSidW(sid_string, c.byref(self.sid)):
            raise c.WinError(c.get_last_error())
        owner = owner_sid_string or current_user_sid_string()
        self.owner_sid = c.c_void_p()
        if not advapi32.ConvertStringSidToSidW(owner, c.byref(self.owner_sid)):
            raise c.WinError(c.get_last_error())
        entries = (EXPLICIT_ACCESS_W * 2)(
            EXPLICIT_ACCESS_W(PIPE_CLIENT_ACCESS, GRANT_ACCESS, 0,
                              TRUSTEE_W(None, 0, TRUSTEE_IS_SID, 0, c.cast(self.sid, c.c_void_p))),
            EXPLICIT_ACCESS_W(FILE_ALL_ACCESS, GRANT_ACCESS, 0,
                              TRUSTEE_W(None, 0, TRUSTEE_IS_SID, 0, c.cast(self.owner_sid, c.c_void_p))))
        self.acl = c.c_void_p()
        result = advapi32.SetEntriesInAclW(2, entries, None, c.byref(self.acl))
        if result != 0:
            raise c.WinError(result)
        self.descriptor = SECURITY_DESCRIPTOR()
        if not advapi32.InitializeSecurityDescriptor(c.byref(self.descriptor), 1):
            raise c.WinError(c.get_last_error())
        if not advapi32.SetSecurityDescriptorDacl(c.byref(self.descriptor), True, self.acl, False):
            raise c.WinError(c.get_last_error())
        self.attributes = SECURITY_ATTRIBUTES(c.sizeof(SECURITY_ATTRIBUTES),
                                               c.cast(c.byref(self.descriptor), c.c_void_p), False)

    def free(self):
        """Release the native SIDs and ACL; the kernel copied them when the pipe was created."""
        if self.acl:
            kernel32.LocalFree(self.acl)
            self.acl = None
        for name in ("sid", "owner_sid"):
            sid = getattr(self, name, None)
            if sid:
                advapi32.FreeSid(sid)
                setattr(self, name, None)


def current_user_sid_string():
    """The process token's user SID, so the DACL can keep the parent fully in control."""
    advapi32.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    kernel32.GetCurrentProcess.restype = w.HANDLE
    token = w.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, c.byref(token)):
        raise c.WinError(c.get_last_error())
    try:
        size = w.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, c.byref(size))
        if not size.value:
            raise c.WinError(c.get_last_error())
        buffer = c.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER, buffer, size.value, c.byref(size)):
            raise c.WinError(c.get_last_error())
        user_sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
        text = w.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(c.c_void_p(user_sid), c.byref(text)):
            raise c.WinError(c.get_last_error())
        try:
            return text.value
        finally:
            kernel32.LocalFree(c.cast(text, c.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def sid_security_attributes(sid_string, owner_sid_string=None):
    """SECURITY_ATTRIBUTES granting one AppContainer SID read/write on the object."""
    holder = _AclHolder(sid_string, owner_sid_string)
    if not advapi32.IsValidSecurityDescriptor(c.byref(holder.descriptor)):
        raise PipeError("constructed an invalid security descriptor")
    return holder


def write_message(handle, payload, max_message=MAX_MESSAGE):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > max_message:
        raise PipeMessageTooLarge("message of %d bytes exceeds %d" % (len(body), max_message))
    _write_all(handle, struct.pack("<I", len(body)) + body)


def read_message(handle, max_message=MAX_MESSAGE, timeout=None):
    """Return the decoded message, or None when the peer closed the connection.

    `timeout` bounds the whole frame (header and body) so an untrusted peer cannot stall the
    parent in a blocking read; without it the read waits indefinitely.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    header = _read_exact(handle, 4, allow_eof=True, deadline=deadline)
    if header is None:
        return None
    (size,) = struct.unpack("<I", header)
    if size > max_message:
        raise PipeMessageTooLarge("peer announced %d bytes, cap is %d" % (size, max_message))
    body = _read_exact(handle, size, allow_eof=False, deadline=deadline)
    return json.loads(body.decode("utf-8"))


def _write_all(handle, data):
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + 65536]
        written = w.DWORD()
        if not kernel32.WriteFile(handle, chunk, len(chunk), c.byref(written), None):
            error = c.get_last_error()
            if error in (ERROR_BROKEN_PIPE, ERROR_NO_DATA):
                raise PipeDisconnected("peer closed while writing (WinError %d)" % error)
            raise c.WinError(error)
        offset += written.value


def _wait_readable(handle, deadline):
    """True when bytes are available, False on disconnect, None when the deadline expires."""
    while True:
        available = w.DWORD()
        if kernel32.PeekNamedPipe(handle, None, 0, None, c.byref(available), None):
            if available.value:
                return True
        else:
            error = c.get_last_error()
            if error in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED):
                return False
            if error != ERROR_PIPE_LISTENING:  # no client has connected yet: keep waiting
                raise c.WinError(error)
        if deadline is not None and time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def _read_exact(handle, size, allow_eof, deadline=None):
    if size == 0:
        return b""
    buffer = c.create_string_buffer(size)
    offset = 0
    while offset < size:
        if deadline is not None:
            state = _wait_readable(handle, deadline)
            if state is False:
                if allow_eof and offset == 0:
                    return None
                raise PipeDisconnected("peer closed in the middle of a frame")
            if state is None:
                raise PipeTimeout("frame did not complete before the read deadline")
        read = w.DWORD()
        if not kernel32.ReadFile(handle, c.byref(buffer, offset), size - offset, c.byref(read), None):
            error = c.get_last_error()
            if allow_eof and offset == 0 and error in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED):
                return None
            raise PipeDisconnected("read failed (WinError %d)" % error)
        if read.value == 0:
            if allow_eof and offset == 0:
                return None
            raise PipeDisconnected("peer closed in the middle of a frame")
        offset += read.value
    return buffer.raw[:size]


class PipeServer:
    """One accepted connection at a time, used for a single job."""

    def __init__(self, name, sid_string=None, max_message=MAX_MESSAGE):
        self.name = name
        self.max_message = max_message
        self.holder = sid_security_attributes(sid_string) if sid_string else None
        self.handle = None
        self.connected = threading.Event()
        self.finished = threading.Event()
        self.error = None
        self._thread = None

    @property
    def path(self):
        return "\\\\.\\pipe\\" + self.name

    def listen(self):
        """Create the pipe and start waiting for the container to connect."""
        attributes = c.cast(c.byref(self.holder.attributes), c.c_void_p) if self.holder else None
        self.handle = kernel32.CreateNamedPipeW(self.path, PIPE_ACCESS_DUPLEX,
                                               PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                                               PIPE_UNLIMITED_INSTANCES, self.max_message, self.max_message, 0, attributes)
        if self.handle == INVALID_HANDLE_VALUE:
            raise c.WinError(c.get_last_error())
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()
        return self

    def _accept(self):
        try:
            if not kernel32.ConnectNamedPipe(self.handle, None):
                error = c.get_last_error()
                if error != ERROR_PIPE_CONNECTED:
                    raise c.WinError(error)
            self.connected.set()
        except Exception as error:  # surfaced to the caller instead of killing the thread
            self.error = error
            self.connected.set()
        finally:
            self.finished.set()

    def wait_connected(self, timeout=10.0):
        self.connected.wait(timeout)
        if self.error:
            raise self.error
        return self.connected.is_set()

    def receive(self, timeout=None):
        return read_message(self.handle, self.max_message, timeout)

    def send(self, payload):
        write_message(self.handle, payload, self.max_message)

    def close(self):
        handle, self.handle = self.handle, None
        if handle and handle != INVALID_HANDLE_VALUE:
            # A pending ConnectNamedPipe in the accept thread would otherwise block teardown and
            # keep the name occupied for the next instance.
            kernel32.CancelIoEx(handle, None)
            if self._thread is not None:
                self._thread.join(timeout=5)
            try:
                kernel32.DisconnectNamedPipe(handle)
            except OSError:
                pass
            kernel32.CloseHandle(handle)
        if self.holder:
            self.holder.free()


def connect(path, timeout_ms=5000, max_message=MAX_MESSAGE):
    """Client side: connect to a pipe, retrying while the server is starting.

    Explicit access rights, not GENERIC_*: for named pipes the generic write set includes a bit
    that aliases FILE_CREATE_PIPE_INSTANCE, so a generic request would force the DACL to hand the
    client the ability to create further instances of the same name.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        handle = kernel32.CreateFileW(path, PIPE_CLIENT_ACCESS, 0, None, OPEN_EXISTING, 0, None)
        if handle != INVALID_HANDLE_VALUE:
            return handle
        error = c.get_last_error()
        if error == ERROR_PIPE_BUSY and time.monotonic() < deadline:
            kernel32.WaitNamedPipeW(path, 200)
            continue
        raise c.WinError(error)


def close(handle):
    if handle and handle != INVALID_HANDLE_VALUE:
        kernel32.CloseHandle(handle)


def roundtrip(path, payload, timeout_ms=5000, max_message=MAX_MESSAGE):
    handle = connect(path, timeout_ms, max_message)
    try:
        write_message(handle, payload, max_message)
        return read_message(handle, max_message)
    finally:
        close(handle)
