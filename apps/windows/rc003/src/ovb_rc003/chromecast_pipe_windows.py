"""Local-only, same-user fixed receiver pipe and OS peer authentication.

No installation, registry changes, arbitrary executable requests or inherited
handles. Win32 bindings are initialized only when explicitly opening a channel.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import time

from . import dev_session, hid_elevation_windows
from .chromecast_channel import SessionIdentity, MAX_MESSAGE_BYTES

WORKER_FLAG = "--chromecast-worker"


class PipeError(RuntimeError):
    pass


class SecurityAttributes(C.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p), ("inherit", W.BOOL)]


class ShellInfo(C.Structure):
    _fields_ = [("size", W.DWORD), ("mask", W.ULONG), ("window", W.HWND),
               ("verb", W.LPCWSTR), ("file", W.LPCWSTR), ("parameters", W.LPCWSTR),
               ("directory", W.LPCWSTR), ("show", C.c_int), ("instance", W.HINSTANCE),
               ("id_list", C.c_void_p), ("class_name", W.LPCWSTR), ("class_key", W.HKEY),
               ("hotkey", W.DWORD), ("icon", W.HANDLE), ("process", W.HANDLE)]


def _bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, arguments
    return function


def api():
    if sys.platform != "win32":
        raise PipeError("windows_only")
    k = C.WinDLL("kernel32", use_last_error=True)
    a = C.WinDLL("advapi32", use_last_error=True)
    _bind(k, "CloseHandle", W.BOOL, W.HANDLE)
    _bind(k, "LocalFree", C.c_void_p, C.c_void_p)
    _bind(k, "OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
    _bind(k, "GetProcessTimes", W.BOOL, W.HANDLE, *([C.POINTER(W.FILETIME)] * 4))
    _bind(k, "ProcessIdToSessionId", W.BOOL, W.DWORD, C.POINTER(W.DWORD))
    _bind(k, "QueryFullProcessImageNameW", W.BOOL, W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
    _bind(k, "WaitForSingleObject", W.DWORD, W.HANDLE, W.DWORD)
    _bind(k, "GetExitCodeProcess", W.BOOL, W.HANDLE, C.POINTER(W.DWORD))
    _bind(k, "GetProcessId", W.DWORD, W.HANDLE)
    _bind(k, "CreateNamedPipeW", W.HANDLE, W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD,
          W.DWORD, W.DWORD, W.DWORD, C.POINTER(SecurityAttributes))
    _bind(k, "ConnectNamedPipe", W.BOOL, W.HANDLE, C.c_void_p)
    _bind(k, "CreateFileW", W.HANDLE, W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE)
    _bind(k, "SetNamedPipeHandleState", W.BOOL, W.HANDLE, C.POINTER(W.DWORD), C.c_void_p, C.c_void_p)
    for name in ("GetNamedPipeClientProcessId", "GetNamedPipeServerProcessId"):
        _bind(k, name, W.BOOL, W.HANDLE, C.POINTER(W.ULONG))
    for name in ("ReadFile", "WriteFile"):
        _bind(k, name, W.BOOL, W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p)
    _bind(a, "OpenProcessToken", W.BOOL, W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
    _bind(a, "GetTokenInformation", W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD))
    _bind(a, "ConvertSidToStringSidW", W.BOOL, C.c_void_p, C.POINTER(W.LPWSTR))
    _bind(a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", W.BOOL,
          W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p)
    return k, a


@dataclass(frozen=True)
class Peer:
    pid: int
    born: int
    sid: str
    session: int
    image: str


def inspect_peer(pid: int) -> Peer:
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        raise PipeError("invalid_peer")
    k, a = api()
    process = k.OpenProcess(0x1000 | 0x100000, False, pid)
    if not process:
        raise PipeError("peer_unavailable")
    token = W.HANDLE()
    text = W.LPWSTR()
    try:
        if k.WaitForSingleObject(process, 0) != 258:
            raise PipeError("peer_exited")
        times = [W.FILETIME() for _ in range(4)]
        session, size = W.DWORD(), W.DWORD(32768)
        image = C.create_unicode_buffer(size.value)
        if (not k.GetProcessTimes(process, *[C.byref(value) for value in times])
                or not k.ProcessIdToSessionId(pid, C.byref(session))
                or not k.QueryFullProcessImageNameW(process, 0, image, C.byref(size))
                or not a.OpenProcessToken(process, 8, C.byref(token))):
            raise PipeError("peer_identity_unavailable")
        needed = W.DWORD()
        a.GetTokenInformation(token, 1, None, 0, C.byref(needed))
        if not 8 <= needed.value <= 4096:
            raise PipeError("peer_token_invalid")
        buffer = C.create_string_buffer(needed.value)
        if not a.GetTokenInformation(token, 1, buffer, needed, C.byref(needed)):
            raise PipeError("peer_token_invalid")
        sid = C.cast(buffer, C.POINTER(C.c_void_p))[0]
        if not a.ConvertSidToStringSidW(sid, C.byref(text)):
            raise PipeError("peer_sid_invalid")
        return Peer(pid, times[0].dwLowDateTime | (times[0].dwHighDateTime << 32),
                    str(text.value), session.value, os.path.normcase(image.value))
    finally:
        if text:
            k.LocalFree(C.cast(text, C.c_void_p))
        if token:
            k.CloseHandle(token)
        k.CloseHandle(process)


def verify_peer(actual: Peer, expected: Peer):
    if actual != expected:
        raise PipeError("peer_changed")


def verify_same_user_image(actual: Peer, current: Peer):
    if (actual.sid, actual.session, actual.image) != (current.sid, current.session, current.image):
        raise PipeError("peer_not_same_user_session_image")


def process_parent_pid(pid: int) -> int:
    """Read an immediate parent from a Win32 process snapshot, not a name search."""
    class Entry(C.Structure):
        _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD),
                    ("heap", C.c_size_t), ("module", W.DWORD), ("threads", W.DWORD),
                    ("parent", W.DWORD), ("priority", W.LONG), ("flags", W.DWORD),
                    ("name", W.WCHAR * 260)]
    k, _ = api()
    _bind(k, "CreateToolhelp32Snapshot", W.HANDLE, W.DWORD, W.DWORD)
    _bind(k, "Process32FirstW", W.BOOL, W.HANDLE, C.POINTER(Entry))
    _bind(k, "Process32NextW", W.BOOL, W.HANDLE, C.POINTER(Entry))
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, C.c_void_p(-1).value):
        raise PipeError("peer_parent_unavailable")
    try:
        entry = Entry()
        entry.size = C.sizeof(Entry)
        available = k.Process32FirstW(snapshot, C.byref(entry))
        while available:
            if entry.pid == pid:
                return entry.parent
            available = k.Process32NextW(snapshot, C.byref(entry))
        raise PipeError("peer_parent_unavailable")
    finally:
        k.CloseHandle(snapshot)


def verify_worker_peer(actual: Peer, launched: Peer, parent: Peer) -> None:
    """Allow a fixed launcher's immediate child, with all identity checks intact.

    CPython's Windows venv executable is a redirector, not the interpreter.
    The pipe PID identifies the interpreter; the ShellExecute handle identifies
    its redirector. A direct process (including packaged builds) still works.
    """
    verify_same_user_image(actual, parent)
    if actual.pid == launched.pid:
        verify_peer(actual, launched)
        return
    if ((launched.sid, launched.session) != (parent.sid, parent.session)
            or launched.image != os.path.normcase(os.path.abspath(sys.executable))
            or actual.born < launched.born
            or process_parent_pid(actual.pid) != launched.pid):
        raise PipeError("peer_launch_mismatch")


def bind_worker_peer(pipe, launched: Peer, parent: Peer):
    """Authenticate the live pipe endpoint and retain its own exit handle."""
    actual = inspect_peer(pipe.peer_pid())
    verify_worker_peer(actual, launched, parent)
    k, _ = api()
    handle = k.OpenProcess(0x1000 | 0x100000, False, actual.pid)
    if not handle:
        raise PipeError("peer_unavailable")
    try:
        verify_peer(inspect_peer(launched.pid), launched)
        verify_peer(inspect_peer(actual.pid), actual)
        return actual, handle
    except BaseException:
        k.CloseHandle(handle)
        raise


class Pipe:
    def __init__(self, identity: SessionIdentity, *, server: bool):
        self.k, self.a = api()
        self.handle = None
        self.name = "\\\\.\\pipe\\RemoteMic.Chromecast." + identity.generation
        self.server = server
        if server:
            sid = hid_elevation_windows.canonical_user_sid(hid_elevation_windows.current_user_sid())
            descriptor = C.c_void_p()
            if not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    f"D:P(A;;GA;;;{sid})(A;;GA;;;SY)", 1, C.byref(descriptor), None):
                raise PipeError("pipe_acl_failed")
            try:
                attributes = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
                self.handle = self.k.CreateNamedPipeW(self.name, 3 | 0x80000, 4 | 2 | 1 | 8,
                                                     1, 65536, 65536, 0, C.byref(attributes))
                self._check_handle()
            finally:
                self.k.LocalFree(descriptor)

    def _check_handle(self):
        if self.handle in (None, C.c_void_p(-1).value):
            self.handle = None
            raise PipeError("pipe_open_failed")

    def connect(self, deadline, cancel):
        while not cancel.is_set() and time.monotonic() < deadline:
            if self.server:
                connected = self.k.ConnectNamedPipe(self.handle, None)
                error = 0 if connected else C.get_last_error()
                if error in (0, 535):
                    return
                if error != 536:
                    raise PipeError("pipe_connect_failed")
            else:
                self.handle = self.k.CreateFileW(self.name, 0xC0000000, 0, None, 3, 0x100000, None)
                if self.handle not in (None, C.c_void_p(-1).value):
                    mode = W.DWORD(2 | 1)
                    if not self.k.SetNamedPipeHandleState(self.handle, C.byref(mode), None, None):
                        raise PipeError("pipe_mode_failed")
                    return
                self.handle = None
                if C.get_last_error() not in (2, 231):
                    raise PipeError("pipe_connect_failed")
            cancel.wait(.02)
        raise PipeError("pipe_connect_cancelled")

    def peer_pid(self):
        pid = W.ULONG()
        function = self.k.GetNamedPipeClientProcessId if self.server else self.k.GetNamedPipeServerProcessId
        if not function(self.handle, C.byref(pid)):
            raise PipeError("pipe_peer_unavailable")
        return pid.value

    def read(self):
        buffer, size = C.create_string_buffer(MAX_MESSAGE_BYTES + 1), W.DWORD()
        if not self.k.ReadFile(self.handle, buffer, len(buffer), C.byref(size), None):
            if C.get_last_error() == 232:  # PIPE_NOWAIT, no message currently available
                return None
            raise PipeError("pipe_read_failed")
        if not 0 < size.value <= MAX_MESSAGE_BYTES:
            raise PipeError("pipe_frame_invalid")
        return buffer.raw[:size.value]

    def write(self, data):
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_MESSAGE_BYTES:
            raise PipeError("pipe_frame_invalid")
        buffer, size = C.create_string_buffer(data), W.DWORD()
        if not self.k.WriteFile(self.handle, buffer, len(data), C.byref(size), None) or size.value != len(data):
            raise PipeError("pipe_write_failed")

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            if not self.k.CloseHandle(handle):
                raise PipeError("pipe_close_failed")


def launch_worker(identity: SessionIdentity, parent: Peer):
    """Launch this exact distribution's fixed worker; never accept a path."""
    command = [] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve().parents[1] / "launcher.py")]
    command += [WORKER_FLAG, str(parent.pid), str(parent.born), identity.entity, identity.generation, identity.token]
    command = dev_session.mark_command(command)
    shell = C.WinDLL("shell32", use_last_error=True)
    _bind(shell, "ShellExecuteExW", W.BOOL, C.POINTER(ShellInfo))
    info = ShellInfo()
    info.size, info.mask, info.show = C.sizeof(info), 0x40 | 0x100, 0
    info.verb, info.file = "runas", sys.executable
    info.parameters = subprocess.list2cmdline(command)
    if not shell.ShellExecuteExW(C.byref(info)) or not info.process:
        raise PipeError("permission_cancelled")
    return info.process
