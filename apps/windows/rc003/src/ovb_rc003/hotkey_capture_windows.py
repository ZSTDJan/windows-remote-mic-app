"""Capture one Windows keyboard chord for the settings recorder.

The QML ``Keys`` layer intentionally does not participate in recording. Qt
normalizes left/right modifiers and only exposes a limited set of key names,
which makes a physical chord such as right Alt + Space impossible to record
faithfully. This module owns a real ``WH_KEYBOARD_LL`` hook instead: it keeps
the first key-down order, preserves directional modifiers, suppresses the
captured events so they cannot affect the foreground application, and emits
one serialized token string after the last captured key is released.

The normal low-level primitive rejects injected events. The settings UI opts
in while the user is explicitly recording so a keyboard forwarded by remote
control software can configure the same field as a local physical keyboard.

The hook is installed only while the settings recorder dialog is open. The
module can still be imported on non-Windows hosts; only ``start()`` requires
the Win32 APIs, so token-formatting tests remain cross-platform.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import win32_keys


WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
VK_ESCAPE = 0x1B

LLKHF_EXTENDED = 0x00000001
LLKHF_INJECTED = 0x00000010
LLKHF_UP = 0x00000080

_STOP_JOIN_TIMEOUT_SECONDS = 2.0
_LOGGER = logging.getLogger("ovb_rc003")


class HotkeyCaptureUnavailableError(Exception):
    """Raised when the real Windows keyboard hook cannot be started."""


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """The cross-platform ctypes shape of Win32 ``KBDLLHOOKSTRUCT``."""

    _fields_ = [
        ("vkCode", ctypes.c_uint32),
        ("scanCode", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_DIRECTIONAL_VK_TO_TOKEN = {
    0xA0: "lshift",
    0xA1: "rshift",
    0xA2: "lctrl",
    0xA3: "rctrl",
    0xA4: "lalt",
    0xA5: "ralt",
    0x5B: "lwin",
    0x5C: "rwin",
}

# If a low-level hook reports a generic modifier VK, its scan code and E0
# flag still identify the side. This fallback also covers keyboard layouts or
# drivers that do not emit VK_L*/VK_R* for the modifier event.
_GENERIC_MODIFIER_BY_PHYSICAL_KEY: Dict[Tuple[int, bool], str] = {
    (0x2A, False): "lshift",
    (0x36, False): "rshift",
    (0x1D, False): "lctrl",
    (0x1D, True): "rctrl",
    (0x38, False): "lalt",
    (0x38, True): "ralt",
}

_GENERIC_MODIFIER_VKS = {0x10, 0x11, 0x12}


def _reverse_vk_table() -> Dict[int, str]:
    reverse: Dict[int, str] = {}
    for token, vk in win32_keys.VK_CODES.items():
        reverse.setdefault(vk, token)
    return reverse


_VK_TO_TOKEN = _reverse_vk_table()


def token_for_keyboard_event(vk_code: int, scan_code: int, flags: int) -> str:
    """Return the lossless token used by the mapping parser for one VK edge.

    Directional modifiers are always returned as ``l*``/``r*`` tokens. A VK
    not in the friendly table is represented as ``vk_XX``; win32_keys.py
    resolves that form back to the original virtual-key code at runtime.
    """

    vk = int(vk_code) & 0xFF
    scan = int(scan_code) & 0xFF
    is_extended = bool(int(flags) & LLKHF_EXTENDED)
    if vk in _DIRECTIONAL_VK_TO_TOKEN:
        return _DIRECTIONAL_VK_TO_TOKEN[vk]
    if vk in _GENERIC_MODIFIER_VKS:
        token = _GENERIC_MODIFIER_BY_PHYSICAL_KEY.get((scan, is_extended))
        if token is not None:
            return token
    token = _VK_TO_TOKEN.get(vk)
    if token is not None:
        return token
    return f"vk_{vk:02x}"


CaptureCallback = Callable[[str], None]


class HotkeyCapture:
    """Own one short-lived global low-level keyboard hook."""

    def __init__(
        self,
        on_captured: CaptureCallback,
        *,
        accept_injected: bool = False,
    ) -> None:
        """Create a recorder; injected input is accepted only by explicit opt-in."""

        self._on_captured = on_captured
        self._accept_injected = bool(accept_injected)
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._hook = None
        self._hookproc_keepalive = None
        self._ready_event = threading.Event()
        self._stop_event = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._state_lock = threading.Lock()
        self._tokens: List[str] = []
        self._pressed_tokens: Set[str] = set()
        self._passthrough_vks: Set[int] = set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        *,
        start_timeout: float = 5.0,
        _run_target: Optional[Callable[[], None]] = None,
    ) -> None:
        if _run_target is None and sys.platform != "win32":
            raise HotkeyCaptureUnavailableError(
                "keyboard shortcut capture is only available on Windows"
            )
        if self.is_running:
            raise HotkeyCaptureUnavailableError(
                "keyboard shortcut capture is already running"
            )
        with self._state_lock:
            self._tokens.clear()
            self._pressed_tokens.clear()
            self._passthrough_vks.clear()
        self._ready_event.clear()
        self._stop_event.clear()
        self._start_error = None
        self._thread = threading.Thread(target=_run_target or self._run, daemon=True)
        self._thread.start()
        if not self._ready_event.wait(timeout=start_timeout):
            self._stop_event.set()
            self._post_quit()
            self._join_thread_or_report_alive()
            raise HotkeyCaptureUnavailableError(
                f"keyboard shortcut capture did not start within {start_timeout}s"
            )
        if self._start_error is not None:
            error = self._start_error
            self._stop_event.set()
            self._post_quit()
            self._join_thread_or_report_alive()
            raise HotkeyCaptureUnavailableError(str(error)) from error

    def _join_thread_or_report_alive(self) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            return False
        self._thread = None
        self._thread_id = 0
        return True

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        self._post_quit()
        if not self._join_thread_or_report_alive():
            raise HotkeyCaptureUnavailableError(
                "keyboard shortcut capture did not stop within the bounded timeout"
            )

    def _post_quit(self) -> None:
        if not self._thread_id:
            return
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.PostThreadMessageW.argtypes = (
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            # The bounded join in stop() remains the authoritative failure
            # signal. A hook thread that has not received WM_QUIT is never
            # silently reported as stopped.
            pass

    def _complete_capture(self) -> None:
        with self._state_lock:
            if not self._tokens or self._pressed_tokens:
                return
            chord = "+".join(self._tokens)
            self._tokens.clear()
        self._stop_event.set()
        self._post_quit()
        try:
            self._on_captured(chord)
        except Exception:
            # A GUI receiver may be in teardown. The hook itself must still
            # unwind and unhook; the controller owns user-visible reporting.
            pass

    def _handle_event(self, message: int, data: KBDLLHOOKSTRUCT) -> bool:
        if self._stop_event.is_set():
            return False
        if int(data.flags) & LLKHF_INJECTED and not self._accept_injected:
            return False
        is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_up = message in (WM_KEYUP, WM_SYSKEYUP) or bool(
            int(data.flags) & LLKHF_UP
        )
        if not (is_down or is_up):
            return False
        # Escape belongs to the focused settings field's cancel action. Let
        # Qt receive both edges instead of recording Escape as a shortcut.
        if int(data.vkCode) == VK_ESCAPE:
            return False

        _LOGGER.info(
            "settings hotkey capture: hook event edge=%s injected=%s",
            "down" if is_down else "up",
            bool(int(data.flags) & LLKHF_INJECTED),
        )

        token = token_for_keyboard_event(data.vkCode, data.scanCode, data.flags)
        owns_event = False
        with self._state_lock:
            vk_code = int(data.vkCode)
            if vk_code in self._passthrough_vks:
                if is_up:
                    self._passthrough_vks.discard(vk_code)
                return False
            if is_down:
                owns_event = True
                if token not in self._pressed_tokens:
                    self._pressed_tokens.add(token)
                    self._tokens.append(token)
            elif token in self._pressed_tokens:
                owns_event = True
                self._pressed_tokens.remove(token)
        if is_up and owns_event:
            self._complete_capture()
        return owns_event

    def _run(self) -> None:
        user32 = None
        hook = None
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.GetCurrentThreadId.argtypes = ()
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            self._thread_id = int(kernel32.GetCurrentThreadId())
            lresult = ctypes.c_ssize_t
            hookproc_type = ctypes.WINFUNCTYPE(
                lresult,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            user32.PeekMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.PeekMessageW.restype = wintypes.BOOL
            user32.GetMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.GetMessageW.restype = ctypes.c_int
            user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
            user32.TranslateMessage.restype = wintypes.BOOL
            user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
            user32.DispatchMessageW.restype = lresult
            user32.SetWindowsHookExW.argtypes = (
                ctypes.c_int,
                hookproc_type,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            )
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.CallNextHookEx.argtypes = (
                wintypes.HHOOK,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.CallNextHookEx.restype = lresult
            user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
            user32.GetAsyncKeyState.restype = ctypes.c_short
            kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE

            def hook_proc(n_code, w_param, l_param):
                if n_code < 0:
                    return user32.CallNextHookEx(hook, n_code, w_param, l_param)
                data = ctypes.cast(
                    l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)
                ).contents
                captured = self._handle_event(int(w_param), data)
                if captured:
                    return 1
                return user32.CallNextHookEx(hook, n_code, w_param, l_param)

            # Low-level hooks are delivered through the installing thread's
            # message queue. Create that queue with a real writable MSG before
            # registering the hook; a NULL lpMsg does not establish a valid
            # queue and can leave SetWindowsHookExW looking successful while
            # no keyboard callback is ever dispatched.
            msg = wintypes.MSG()
            user32.PeekMessageW(
                ctypes.byref(msg),
                None,
                0,
                0,
                PM_NOREMOVE,
            )
            self._hookproc_keepalive = hookproc_type(hook_proc)
            hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                self._hookproc_keepalive,
                kernel32.GetModuleHandleW(None),
                0,
            )
            if not hook:
                raise ctypes.WinError()
            self._hook = hook
            # The foreground application already owns keys that were held
            # before this hook existed. Pass their repeats and matching key-up
            # through until release instead of adopting half of the hold.
            with self._state_lock:
                self._passthrough_vks.update(
                    vk_code
                    for vk_code in range(1, 256)
                    if user32.GetAsyncKeyState(vk_code) & 0x8000
                )
            self._ready_event.set()

            while not self._stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except BaseException as exc:  # noqa: BLE001 - surfaced by start()
            self._start_error = exc
            self._ready_event.set()
        finally:
            if user32 is not None and hook:
                try:
                    user32.UnhookWindowsHookEx(hook)
                except Exception:
                    pass
            self._hook = None
            self._hookproc_keepalive = None
            self._ready_event.set()
            _LOGGER.info(
                "settings hotkey capture: hook thread stopped requested=%s",
                self._stop_event.is_set(),
            )
