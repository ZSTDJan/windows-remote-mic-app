"""Physicalize only Remote Mic's marked injected right-Alt voice edges.

Some voice-input hosts ignore keyboard events that still carry Windows'
injected flags.  Remote Mic marks its legacy ``keybd_event`` voice edges and
this narrow low-level hook removes those flags before forwarding the event.
It never suppresses, delays, correlates, or transforms any physical key.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from typing import Callable, Optional


WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
LLKHF_INJECTED = 0x00000010
LLKHF_LOWER_IL_INJECTED = 0x00000002
PM_NOREMOVE = 0x0000
VK_RMENU = 0xA5

# "RMICRC03" as a pointer-sized value. Only win32_input's marked voice path
# emits it; the hook clears it before forwarding the event downstream.
VOICE_EVENT_EXTRA_INFO = 0x524D494352433033


class VoiceKeyPhysicalizerUnavailableError(Exception):
    """Raised when the Windows low-level keyboard hook cannot be started."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise VoiceKeyPhysicalizerUnavailableError(
            "voice key physicalizer is only available on Windows"
        )


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


def physicalize_injected_event(event: KBDLLHOOKSTRUCT) -> bool:
    """Clear injection metadata only for Remote Mic's marked right Alt."""

    if not (int(event.flags) & LLKHF_INJECTED):
        return False
    if int(event.vkCode) != VK_RMENU:
        return False
    if int(event.dwExtraInfo) != VOICE_EVENT_EXTRA_INFO:
        return False
    event.flags = int(event.flags) & ~(
        LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
    )
    event.dwExtraInfo = 0
    return True


class VoiceKeyPhysicalizer:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._stop_event = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._thread_id = wintypes.DWORD(0)
        self._hook = None
        self._hookproc_keepalive = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        *,
        start_timeout: float = 5.0,
        _run_target: Optional[Callable[[], None]] = None,
    ) -> None:
        if self.is_running:
            raise VoiceKeyPhysicalizerUnavailableError(
                "voice key physicalizer is already running; call stop() first"
            )
        if _run_target is None:
            if os.environ.get("RC003_DISABLE_LIVE_INPUT") == "1":
                raise VoiceKeyPhysicalizerUnavailableError(
                    "live Windows keyboard hooks are disabled for this process"
                )
            _require_windows()
        self._ready_event.clear()
        self._stop_event.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=_run_target or self._run,
            name="remote-mic-voice-key-physicalizer",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(timeout=start_timeout):
            self._stop_event.set()
            self._join_thread_or_report_alive()
            raise VoiceKeyPhysicalizerUnavailableError(
                f"voice key physicalizer did not become ready within {start_timeout}s"
            )
        if self._start_error is not None:
            error = self._start_error
            self._stop_event.set()
            self._join_thread_or_report_alive()
            raise error

    def _join_thread_or_report_alive(self) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            return False
        self._thread = None
        self._thread_id = wintypes.DWORD(0)
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is None:
            return
        if sys.platform == "win32" and self._thread_id.value:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.PostThreadMessageW.argtypes = (
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW(self._thread_id.value, 0x0012, 0, 0)
        if not self._join_thread_or_report_alive():
            raise VoiceKeyPhysicalizerUnavailableError(
                "voice key physicalizer thread did not stop within 2.0s"
            )

    def _run(self) -> None:
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

            hook_proc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            hookproc = hook_proc_type(self._hookproc)
            self._hookproc_keepalive = hookproc

            kernel32.GetCurrentThreadId.argtypes = ()
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            self._thread_id = wintypes.DWORD(kernel32.GetCurrentThreadId())

            kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.SetWindowsHookExW.argtypes = (
                ctypes.c_int,
                hook_proc_type,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            )
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.PeekMessageW.restype = wintypes.BOOL

            self._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                hookproc,
                kernel32.GetModuleHandleW(None),
                0,
            )
            if not self._hook:
                raise VoiceKeyPhysicalizerUnavailableError(
                    "SetWindowsHookExW failed"
                )

            # Ensure the message queue exists before start() reports ready.
            # PeekMessageW requires a writable MSG pointer even when no
            # message is removed.
            queue_probe = wintypes.MSG()
            user32.PeekMessageW(
                ctypes.byref(queue_probe),
                None,
                0,
                0,
                PM_NOREMOVE,
            )
            self._ready_event.set()

            msg = wintypes.MSG()
            user32.GetMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.GetMessageW.restype = ctypes.c_int
            while not self._stop_event.is_set():
                if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0:
                    break
        except BaseException as exc:  # noqa: BLE001 - surfaced to start()
            self._start_error = exc
            self._ready_event.set()
        finally:
            if self._hook:
                try:
                    ctypes.windll.user32.UnhookWindowsHookEx(self._hook)  # type: ignore[attr-defined]
                except Exception:
                    pass
                self._hook = None

    def _hookproc(self, n_code, w_param, l_param):
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.CallNextHookEx.argtypes = (
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        if n_code >= 0 and int(w_param) in (
            WM_KEYDOWN,
            WM_KEYUP,
            WM_SYSKEYDOWN,
            WM_SYSKEYUP,
        ):
            event = KBDLLHOOKSTRUCT.from_address(int(l_param))
            original_flags = int(event.flags)
            original_extra_info = int(event.dwExtraInfo)
            if physicalize_injected_event(event):
                try:
                    return user32.CallNextHookEx(
                        self._hook,
                        n_code,
                        w_param,
                        int(l_param),
                    )
                finally:
                    event.flags = original_flags
                    event.dwExtraInfo = original_extra_info
        return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)
