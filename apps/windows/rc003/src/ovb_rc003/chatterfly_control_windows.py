"""Own Chatterfly's existing voice-input IPC, without synthetic keyboard input.

Protocol 1 is used by voiceinput.exe itself (verified with component 1.0.0.3670).
A transport acknowledgement is not recording confirmation. Only a newly visible
voice window authorizes ready; an existing recording is never adopted or stopped.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
from pathlib import PureWindowsPath
import os
import sys
import threading
import time

IPC_WINDOW = "__chatterfly_VoiceInputMessageIpcWnd__"


class _CopyData(C.Structure):
    _fields_ = [("dwData", C.c_size_t), ("cbData", W.DWORD), ("lpData", C.c_void_p)]


@dataclass(frozen=True)
class Target:
    hwnd: int
    pid: int
    created: int
    process: int


@dataclass(frozen=True)
class _Activity:
    identity: tuple
    state: int = 1


class WindowsTransport:
    def __init__(self):
        if sys.platform != "win32" or os.environ.get("RC003_DISABLE_LIVE_INPUT") == "1":
            raise OSError("Chatterfly live control is unavailable")
        self.u = C.WinDLL("user32", use_last_error=True)
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        for dll, name, args, result in (
            (self.u, "FindWindowExW", [W.HWND, W.HWND, W.LPCWSTR, W.LPCWSTR], W.HWND),
            (self.u, "GetWindowThreadProcessId", [W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            (self.u, "SendMessageTimeoutW", [W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT, C.POINTER(C.c_size_t)], C.c_ssize_t),
            (self.u, "EnumWindows", [self.callback, W.LPARAM], W.BOOL),
            (self.u, "IsWindowVisible", [W.HWND], W.BOOL),
            (self.u, "GetClassNameW", [W.HWND, W.LPWSTR, C.c_int], C.c_int),
            (self.k, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.k, "CloseHandle", [W.HANDLE], W.BOOL),
            (self.k, "GetExitCodeProcess", [W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
            (self.k, "GetProcessTimes", [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
            (self.k, "QueryFullProcessImageNameW", [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
        ):
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result

    def discover(self):
        hwnd = self.u.FindWindowExW(None, None, None, IPC_WINDOW)
        if not hwnd or self.u.FindWindowExW(None, hwnd, None, IPC_WINDOW):
            raise OSError("Chatterfly voice IPC target is absent or ambiguous")
        pid = W.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, C.byref(pid))
        handle = self.k.OpenProcess(0x1000, False, pid.value)
        if not handle:
            raise C.WinError(C.get_last_error())
        try:
            name, size = C.create_unicode_buffer(32768), W.DWORD(32768)
            times = [W.FILETIME() for _ in range(4)]
            if (not self.k.QueryFullProcessImageNameW(handle, 0, name, C.byref(size))
                    or not self.k.GetProcessTimes(handle, *(C.byref(t) for t in times))):
                raise C.WinError(C.get_last_error())
            path = PureWindowsPath(name.value)
            if path.name.lower() != "voiceinput.exe" or "chatterfly" not in [p.lower() for p in path.parts]:
                raise OSError("Chatterfly IPC process identity does not match")
            target = Target(int(hwnd), pid.value, (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime, int(handle))
            if not self.alive(target):
                raise OSError("Chatterfly IPC target changed during discovery")
            return target
        except BaseException:
            self.k.CloseHandle(handle)
            raise

    def alive(self, target):
        code, pid = W.DWORD(), W.DWORD()
        return bool(self.k.GetExitCodeProcess(target.process, C.byref(code)) and code.value == 259
                    and self.u.GetWindowThreadProcessId(target.hwnd, C.byref(pid)) and pid.value == target.pid)

    def visible(self, target):
        windows = set()
        def visit(hwnd, _):
            pid = W.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, C.byref(pid))
            if pid.value == target.pid and self.u.IsWindowVisible(hwnd):
                name = C.create_unicode_buffer(64)
                if self.u.GetClassNameW(hwnd, name, len(name)) and name.value in {"VoiceBarWnd", "VoiceBubbleWnd"}:
                    windows.add(int(hwnd))
            return True
        if not self.u.EnumWindows(self.callback(visit), 0):
            raise C.WinError(C.get_last_error())
        return frozenset(windows)

    def send(self, target, command):
        if not self.alive(target):
            raise OSError("Chatterfly IPC target exited or changed")
        if command not in (2, 3):
            raise ValueError("unsupported Chatterfly voice command")
        payload = f"{command}\n1\n".encode("ascii")
        buffer = C.create_string_buffer(payload)
        data = _CopyData(1, len(payload), C.cast(buffer, C.c_void_p))
        reply = C.c_size_t()
        C.set_last_error(0)
        if not self.u.SendMessageTimeoutW(target.hwnd, 0x4A, 0, C.addressof(data), 3, 1000, C.byref(reply)):
            # A timeout can still have executed. The owner observes before cleanup.
            raise OSError(C.get_last_error(), "Chatterfly voice command acknowledgement failed")

    def close(self, target):
        self.k.CloseHandle(target.process)


class ChatterflyVoiceControl:
    START_SECONDS = 3.0
    STOP_SECONDS = 3.0
    POLL_SECONDS = .05

    def __init__(self, *, logger, on_state=lambda generation, state: None, transport=None):
        self.logger, self.on_state = logger, on_state
        self.transport = transport
        self.lock = threading.RLock()
        self.current_generation = 0
        self.target = None
        self.thread = None
        self.stop_requested = threading.Event()
        self.confirmed = False
        self.stop_sent = False
        self.cleanup_failed = False

    @property
    def cleanup_pending(self):
        with self.lock:
            return self.target is not None

    @property
    def completion_pending(self):
        return self.cleanup_pending and self.stop_requested.is_set() and not self.cleanup_failed

    def _notify(self, generation, state):
        self.logger.info("Chatterfly voice IPC generation=%s state=%s", generation, state)
        try:
            self.on_state(generation, state)
        except Exception:
            self.logger.exception("Chatterfly voice state callback failed")

    def start(self, *, cancelled=None):
        with self.lock:
            if self.target is not None or cancelled is not None and cancelled():
                return False
            self.transport = self.transport or WindowsTransport()
            target = self.transport.discover()
            try:
                if self.transport.visible(target):
                    self.logger.warning("Chatterfly already has a voice session; start refused")
                    return False
                if cancelled is not None and cancelled():
                    return False
                self.current_generation += 1
                self.stop_requested.clear()
                self.confirmed = self.stop_sent = False
                self.cleanup_failed = False
                self.target = target
                self.started_at = time.monotonic()
                try:
                    self.transport.send(target, 2)
                except OSError:
                    self.logger.exception("Chatterfly start acknowledgement failed; observing possible late start")
                    self.stop_requested.set()
                try:
                    self._launch_monitor()
                except OSError:
                    self.stop_requested.set()
                    self.cleanup_failed = True
                    self.logger.exception("Chatterfly observer could not start; cleanup ownership retained")
                    return False
                return not self.stop_requested.is_set()
            finally:
                if self.target is not target:
                    self.transport.close(target)

    def _launch_monitor(self):
        self.thread = threading.Thread(target=self._monitor, args=(self.current_generation, self.target),
                                       name="chatterfly-voice-owner", daemon=True)
        try:
            self.thread.start()
        except RuntimeError as exc:
            raise OSError("Chatterfly observer thread could not start") from exc

    def stop(self):
        with self.lock:
            if self.target is None:
                return True
            self.stop_requested.set()
            if self.thread is None or not self.thread.is_alive():
                self._launch_monitor()
            return False  # Completion is the observed close, never the send result.

    def activity(self):
        with self.lock:
            target = self.target
            if target is None or not self.confirmed or not self.transport.alive(target):
                return ()
            if not self.transport.visible(target).intersection(self.owned_windows):
                return ()
            return (_Activity(("ChatterflyVoiceIpc", target.created, target.pid,
                               target.hwnd, self.current_generation)),)

    def _monitor(self, generation, target):
        outcome, announced = "closed", self.confirmed
        deadline = time.monotonic() + self.STOP_SECONDS
        release = False
        try:
            while True:
                if not self.transport.alive(target):
                    release = True
                    outcome = "closed" if announced else "failed"
                    break
                visible = self.transport.visible(target)
                if visible and not self.confirmed:
                    self.confirmed = True
                    self.owned_windows = visible
                    if not self.stop_requested.is_set():
                        announced = True
                        self._notify(generation, "ready")
                if self.confirmed:
                    if not visible or not visible.intersection(self.owned_windows):
                        # Do not adopt a replacement window/session.
                        release = True
                        break
                    if self.stop_requested.is_set() and not self.stop_sent:
                        self.stop_sent = True  # Never duplicate stop after an ambiguous timeout.
                        deadline = time.monotonic() + self.STOP_SECONDS
                        self.transport.send(target, 3)
                    if self.stop_sent and time.monotonic() >= deadline:
                        outcome = "cleanup_pending"
                        break
                elif time.monotonic() - self.started_at >= self.START_SECONDS:
                    # No bubble was ever observed. No unowned stop is sent.
                    release, outcome = True, "failed"
                    break
                time.sleep(self.POLL_SECONDS)
        except Exception:
            self.logger.exception("Chatterfly voice observation/cleanup failed; ownership retained")
            outcome = "cleanup_pending"
        finally:
            self.cleanup_failed = outcome == "cleanup_pending"
            if release:
                with self.lock:
                    if self.target is target:
                        self.target = None
                        self.transport.close(target)
            self._notify(generation, outcome)
