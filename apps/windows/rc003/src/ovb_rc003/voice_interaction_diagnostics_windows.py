"""Privacy-safe focus/result diagnostics for Windows voice input.

Only handles, process IDs, class names, and text lengths are observed. Window
titles and user text are deliberately never read or logged.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
from typing import Optional


WM_GETTEXTLENGTH = 0x000E
SMTO_ABORTIFHUNG = 0x0002
TEXT_QUERY_TIMEOUT_MS = 20


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    )


@dataclass(frozen=True)
class FocusSnapshot:
    supported: bool
    foreground_pid: int = 0
    foreground_class: str = ""
    focus_handle: int = 0
    focus_class: str = ""
    text_length: Optional[int] = None
    error: str = ""


@dataclass(frozen=True)
class SubmissionObservation:
    focus_state: str
    text_state: str
    text_delta: Optional[int]


def _window_class_name(user32, hwnd: int) -> str:
    if not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    copied = user32.GetClassNameW(wintypes.HWND(hwnd), buffer, len(buffer))
    return buffer.value if copied else ""


def _window_text_length(user32, hwnd: int) -> Optional[int]:
    if not hwnd:
        return None
    result = ctypes.c_size_t(0)
    delivered = user32.SendMessageTimeoutW(
        wintypes.HWND(hwnd),
        WM_GETTEXTLENGTH,
        0,
        0,
        SMTO_ABORTIFHUNG,
        TEXT_QUERY_TIMEOUT_MS,
        ctypes.byref(result),
    )
    return int(result.value) if delivered else None


def _capture_windows_focus() -> FocusSnapshot:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetGUIThreadInfo.argtypes = (
        wintypes.DWORD,
        ctypes.POINTER(_GuiThreadInfo),
    )
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetClassNameW.restype = ctypes.c_int
    user32.SendMessageTimeoutW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    )
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM

    foreground = user32.GetForegroundWindow()
    if not foreground:
        return FocusSnapshot(True, error="no_foreground_window")
    process_id = wintypes.DWORD(0)
    thread_id = user32.GetWindowThreadProcessId(foreground, ctypes.byref(process_id))
    info = _GuiThreadInfo()
    info.cbSize = ctypes.sizeof(_GuiThreadInfo)
    focus = int(foreground)
    if thread_id and user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        focus = int(info.hwndFocus or foreground)
    return FocusSnapshot(
        supported=True,
        foreground_pid=int(process_id.value),
        foreground_class=_window_class_name(user32, int(foreground)),
        focus_handle=focus,
        focus_class=_window_class_name(user32, focus),
        text_length=_window_text_length(user32, focus),
    )


def capture_focus_snapshot(*, platform: Optional[str] = None) -> FocusSnapshot:
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return FocusSnapshot(False, error="unsupported_platform")
    try:
        return _capture_windows_focus()
    except (AttributeError, OSError, ValueError):
        return FocusSnapshot(True, error="capture_failed")


def compare_submission(
    before: Optional[FocusSnapshot],
    after: Optional[FocusSnapshot],
) -> SubmissionObservation:
    if before is None or after is None or not before.supported or not after.supported:
        return SubmissionObservation("unavailable", "unavailable", None)
    if before.error or after.error:
        return SubmissionObservation("unavailable", "unavailable", None)
    same_focus = bool(
        before.focus_handle
        and before.focus_handle == after.focus_handle
        and before.foreground_pid == after.foreground_pid
    )
    focus_state = "same" if same_focus else "changed"
    if before.text_length is None or after.text_length is None:
        return SubmissionObservation(focus_state, "unavailable", None)
    delta = after.text_length - before.text_length
    text_state = "grew" if delta > 0 else "shrunk" if delta < 0 else "unchanged"
    return SubmissionObservation(focus_state, text_state, delta)
