"""Doubao-only foreground TSF switching through Windows' active-context proxy.

IAICProxy is an internal COM contract, not a supported public Windows API.
Its IID/signatures were checked against matching Microsoft symbols for x64
19041.7725, 22621 (local) and 26100.9278; only the local OS was exercised live.
Use COM interfaces, never DLL offsets or remote memory. No global activation
fallback: an unavailable interface or unconfirmed target fails this attempt.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import time

from . import wetype_control_windows as profiles

_AIC_PROXY_IID = profiles._guid_bytes("c1a97c88-de59-470f-adcb-a6c916fca6c2")
_SWITCH_TIMEOUT_SECONDS = 0.4
# Observed Doubao 0.9 caches its foreground decision for 100 ms. After a cold
# switch, keep the confirmed profile stable past that cache; not voice readiness.
_COLD_PROFILE_STABLE_SECONDS = 0.12


@dataclass(frozen=True)
class InputTarget:
    hwnd: int
    pid: int
    thread_id: int
    focus_hwnd: int
    focus_pid: int
    focus_thread_id: int


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("active", wintypes.HWND), ("focus", wintypes.HWND),
        ("capture", wintypes.HWND), ("menu", wintypes.HWND),
        ("move_size", wintypes.HWND), ("caret", wintypes.HWND),
        ("caret_rect", wintypes.RECT),
    ]


def _user32():
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo))
    user.GetGUIThreadInfo.restype = wintypes.BOOL
    return user


def current_target() -> InputTarget:
    user = _user32()
    hwnd = user.GetForegroundWindow()
    pid, focus_pid = wintypes.DWORD(), wintypes.DWORD()
    tid = user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    info = _GuiThreadInfo()
    info.size = ctypes.sizeof(info)
    if not hwnd or not tid or not user.GetGUIThreadInfo(tid, ctypes.byref(info)) or not info.focus:
        raise OSError("Doubao input target is unavailable")
    focus_tid = user.GetWindowThreadProcessId(info.focus, ctypes.byref(focus_pid))
    if not focus_tid or not focus_pid.value or user.GetForegroundWindow() != hwnd:
        raise OSError("Doubao input target changed while reading focus")
    # Modern Notepad's edit control can have a different thread from its frame.
    return InputTarget(int(hwnd), pid.value, tid, int(info.focus), focus_pid.value, focus_tid)


def target_is_current(target: InputTarget) -> bool:
    try:
        return current_target() == target
    except OSError:
        return False


def _method(pointer, index, *argument_types):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argument_types)(table[index])


def _require_ok(hr: int, operation: str) -> None:
    if hr != 0:
        raise OSError(f"Doubao {operation} failed: HRESULT 0x{hr & 0xffffffff:08X}")


class _ActiveContextProxy:
    """Owned entirely by the caller's already initialized STA thread."""

    def __init__(self):
        self._manager = ctypes.c_void_p()
        self._proxy = ctypes.c_void_p()
        self._msctf = None

    def __enter__(self):
        try:
            if ctypes.sizeof(ctypes.c_void_p) != 8:
                raise OSError("Doubao targeted switching requires a 64-bit process")
            msctf = ctypes.WinDLL("msctf.dll", winmode=0x800)  # System32 only
            self._msctf = msctf
            create = msctf.TF_CreateLangBarMgr
            create.argtypes = (ctypes.POINTER(ctypes.c_void_p),)
            create.restype = ctypes.c_long
            _require_ok(create(ctypes.byref(self._manager)), "create language manager")
            if not self._manager.value:
                raise OSError("Doubao language manager is unavailable")
            kernel = ctypes.WinDLL("kernel32")
            kernel.GetCurrentThreadId.restype = wintypes.DWORD
            iid = profiles._native_guid(_AIC_PROXY_IID)
            _require_ok(_method(self._manager, 5, wintypes.DWORD, wintypes.DWORD,
                ctypes.POINTER(profiles._GUID), ctypes.POINTER(ctypes.c_void_p))(
                    self._manager, kernel.GetCurrentThreadId(), 2,
                    ctypes.byref(iid), ctypes.byref(self._proxy)), "get active-context proxy")
            if not self._proxy.value:
                raise OSError("Doubao active-context proxy is unavailable")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args):
        try:
            if self._proxy.value:
                _method(self._proxy, 2)(self._proxy)
        finally:
            self._proxy = ctypes.c_void_p()
            if self._manager.value:
                _method(self._manager, 2)(self._manager)
            self._manager = ctypes.c_void_p()

    def context(self) -> tuple[int, int]:
        tid, flags, hwnd = wintypes.DWORD(), wintypes.DWORD(), wintypes.HWND()
        _require_ok(_method(self._proxy, 3, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.HWND), ctypes.POINTER(wintypes.DWORD))(
                self._proxy, ctypes.byref(tid), ctypes.byref(hwnd), ctypes.byref(flags)), "read target")
        return tid.value, int(hwnd.value or 0)

    def active_profile(self):
        category = profiles._native_guid(profiles._KEYBOARD_CATEGORY_GUID)
        value = profiles._TF_INPUTPROCESSORPROFILE()
        hr = _method(self._proxy, 9, ctypes.POINTER(profiles._GUID),
            ctypes.POINTER(profiles._TF_INPUTPROCESSORPROFILE))(
                self._proxy, ctypes.byref(category), ctypes.byref(value))
        if hr == 1:
            return None
        _require_ok(hr, "read active profile")
        return profiles._InputProfileSnapshot(
            int(value.dwProfileType), int(value.langid), bytes(value.clsid),
            bytes(value.guidProfile), int(value.hkl or 0), int(value.dwFlags))

    def activate(self, target):
        clsid = profiles._native_guid(target.clsid)
        guid = profiles._native_guid(target.profile_guid)
        _require_ok(_method(self._proxy, 12, ctypes.c_uint16, wintypes.HKL,
            ctypes.POINTER(profiles._GUID), ctypes.POINTER(profiles._GUID))(
                self._proxy, target.language_id, None, ctypes.byref(clsid), ctypes.byref(guid)),
            "request targeted switch")

    def pump(self, seconds):
        user = _user32()
        user.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                     wintypes.UINT, wintypes.UINT, wintypes.UINT)
        user.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user.DispatchMessageW.restype = ctypes.c_ssize_t
        end = time.monotonic() + seconds
        msg = wintypes.MSG()
        while time.monotonic() < end:
            while time.monotonic() < end and user.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x12:  # WM_QUIT: don't continue a cancelled apartment
                    raise OSError("Doubao input switching thread is quitting")
                user.TranslateMessage(ctypes.byref(msg))
                user.DispatchMessageW(ctypes.byref(msg))
            time.sleep(min(.005, max(0, end - time.monotonic())))


def activate_for_target(target, *, cancelled, trace, expected=None):
    """Request once, confirm the same input target, never roll back or broadcast.

    Profile confirmation is not proof that Doubao has started voice capture.
    The caller still uses the existing capture confirmation for that decision.
    """
    expected = expected or current_target()
    deadline = time.monotonic() + _SWITCH_TIMEOUT_SECONDS
    requested = False
    confirmed_since = None
    last_snapshot = None
    last_context = None

    def check():
        if cancelled() or time.monotonic() >= deadline:
            raise TimeoutError("Doubao targeted input switch cancelled or timed out")
        if not target_is_current(expected):
            raise OSError("Doubao input target changed during preparation")

    with _ActiveContextProxy() as proxy:
        while True:
            check()
            proxy.pump(.005)
            check()
            context_tid, context_hwnd = proxy.context()
            if (context_tid, context_hwnd) != (expected.focus_thread_id, expected.focus_hwnd):
                confirmed_since = None
                if last_context != (context_tid, context_hwnd):
                    trace("input_profile_target_wait", provider="doubao",
                          reason="system_target_not_current", target_hwnd=expected.hwnd,
                          focus_hwnd=expected.focus_hwnd, focus_thread_id=expected.focus_thread_id,
                          context_hwnd=context_hwnd, context_thread_id=context_tid)
                    last_context = context_tid, context_hwnd
                continue
            active = proxy.active_profile()
            snapshot_key = (requested, active)
            if snapshot_key != last_snapshot:
                trace("input_profile_target", provider="doubao", route="active_context_proxy",
                  target_hwnd=expected.hwnd, target_pid=expected.pid,
                  focus_hwnd=expected.focus_hwnd, focus_pid=expected.focus_pid,
                  focus_thread_id=expected.focus_thread_id,
                  context_hwnd=context_hwnd, context_thread_id=context_tid,
                  phase="after" if requested else "before",
                  **(profiles._profile_trace_fields("active", active) if active else {}),
                  **profiles._profile_trace_fields("target", target))
                last_snapshot = snapshot_key
            check()
            if active is not None and profiles._same_profile(active, target):
                if not requested:
                    return False, expected
                if confirmed_since is None:
                    confirmed_since = time.monotonic()
                if time.monotonic() - confirmed_since >= _COLD_PROFILE_STABLE_SECONDS:
                    return True, expected
                proxy.pump(.01)
                continue
            confirmed_since = None
            if not requested:
                # Recheck the proxy after message pumping. It owns the target TID;
                # no keyboard emulation, global flags or per-version memory offsets.
                if proxy.context() != (expected.focus_thread_id, expected.focus_hwnd):
                    continue
                check()
                proxy.activate(target)
                requested = True
                trace("input_profile_target_requested", provider="doubao",
                      route="active_context_proxy", focus_thread_id=expected.focus_thread_id)
            proxy.pump(.01)
