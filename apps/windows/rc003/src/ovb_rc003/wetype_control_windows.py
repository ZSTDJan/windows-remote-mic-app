"""Control WeType voice input through its own status-bar button on Windows.

WeType 2.1.3.18 observes physical low-level keyboard input and ignores the
keyboard injection paths available to Remote Mic. This module therefore does
not synthesize a shortcut: it clicks WeType's microphone button for both press
and release, then bounds any panel left behind after submission.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

from . import win32_input

_WM_CLOSE = 0x0010
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WETYPE_TOOLBAR_CLASS = "wetype.statusbar.window"
_VOICE_PANEL_TITLE = "语音输入"
_START_WAIT_SECONDS = 0.8
_STALE_CLOSE_WAIT_SECONDS = 0.3
_COMPLETION_WAIT_SECONDS = 5.0
_COMPLETION_POLL_SECONDS = 0.05


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _user32():
    win32_input._require_windows()
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(_RECT))
    user32.GetClientRect.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    user32.PostMessageW.restype = wintypes.BOOL
    return user32


def _enum_windows(visitor: Callable[[int], bool]) -> None:
    user32 = _user32()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd, _lparam):
        return bool(visitor(int(hwnd)))

    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows(callback, 0)


def _find_window_by_class(expected_class: str) -> Optional[int]:
    found: Optional[int] = None

    def visit(hwnd: int) -> bool:
        nonlocal found
        buffer = ctypes.create_unicode_buffer(128)
        _user32().GetClassNameW(hwnd, buffer, len(buffer))
        if buffer.value.casefold() == expected_class.casefold():
            found = hwnd
            return False
        return True

    _enum_windows(visit)
    return found


def _find_voice_panel() -> Optional[int]:
    toolbar = _find_window_by_class(_WETYPE_TOOLBAR_CLASS)
    if toolbar is None:
        return None
    toolbar_pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(toolbar, ctypes.byref(toolbar_pid))
    if toolbar_pid.value == 0:
        return None
    found: Optional[int] = None

    def visit(hwnd: int) -> bool:
        nonlocal found
        user32 = _user32()
        if not user32.IsWindowVisible(hwnd):
            return True
        candidate_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(candidate_pid))
        if candidate_pid.value != toolbar_pid.value:
            return True
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        if _VOICE_PANEL_TITLE.casefold() in buffer.value.casefold():
            found = hwnd
            return False
        return True

    _enum_windows(visit)
    return found


def _click_toolbar() -> bool:
    toolbar = _find_window_by_class(_WETYPE_TOOLBAR_CLASS)
    if toolbar is None:
        return False
    rectangle = _RECT()
    user32 = _user32()
    if not user32.GetClientRect(toolbar, ctypes.byref(rectangle)):
        return False
    if rectangle.right <= 0 or rectangle.bottom <= 0:
        return False
    x = max(1, rectangle.right * 45 // 142)
    y = max(1, rectangle.bottom // 2)
    point = (y << 16) | (x & 0xFFFF)
    down = bool(user32.PostMessageW(toolbar, _WM_LBUTTONDOWN, 1, point))
    up = bool(user32.PostMessageW(toolbar, _WM_LBUTTONUP, 0, point))
    return down and up


def _close_panel(panel: int) -> bool:
    return bool(_user32().PostMessageW(panel, _WM_CLOSE, 0, 0))


def _start_daemon(callback: Callable[[], None]) -> None:
    threading.Thread(
        target=callback,
        name="wetype-voice-completion",
        daemon=True,
    ).start()


class WeTypeVoiceControl:
    """Open and finish one WeType session through the native toolbar."""

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        find_panel: Callable[[], Optional[int]] = _find_voice_panel,
        click_toolbar: Callable[[], bool] = _click_toolbar,
        close_panel: Callable[[int], bool] = _close_panel,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        schedule: Callable[[Callable[[], None]], None] = _start_daemon,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._find_panel = find_panel
        self._click_toolbar = click_toolbar
        self._close_panel = close_panel
        self._sleep = sleep
        self._monotonic = monotonic
        self._schedule = schedule
        self._state_lock = threading.Lock()
        self._generation = 0
        self._active_generation: Optional[int] = None

    def _begin_session(self) -> int:
        with self._state_lock:
            self._generation += 1
            self._active_generation = None
            return self._generation

    def _is_current_generation(self, generation: int) -> bool:
        with self._state_lock:
            return self._generation == generation

    def _mark_active(self, generation: int) -> bool:
        with self._state_lock:
            if self._generation != generation:
                return False
            self._active_generation = generation
            return True

    def _clear_active(self, generation: int) -> None:
        with self._state_lock:
            if self._active_generation == generation:
                self._active_generation = None

    def _wait_for_panel(self, *, present: bool, timeout_seconds: float) -> bool:
        deadline = self._monotonic() + timeout_seconds
        while self._monotonic() < deadline:
            if (self._find_panel() is not None) == present:
                return True
            self._sleep(0.025)
        return (self._find_panel() is not None) == present

    def _close_stale_panel(self) -> bool:
        panel = self._find_panel()
        if panel is None:
            return True
        if not self._close_panel(panel):
            self._logger.warning("WeType stale voice panel could not be closed")
            return False
        closed = self._wait_for_panel(
            present=False,
            timeout_seconds=_STALE_CLOSE_WAIT_SECONDS,
        )
        if not closed:
            self._logger.warning("WeType stale voice panel remained visible")
        return closed

    def _finish_submission(self, generation: int) -> None:
        deadline = self._monotonic() + _COMPLETION_WAIT_SECONDS
        while self._monotonic() < deadline:
            if not self._is_current_generation(generation):
                self._logger.info("WeType completion wait superseded by a new session")
                return
            if self._find_panel() is None:
                self._logger.info("WeType voice panel closed after submission")
                return
            self._sleep(_COMPLETION_POLL_SECONDS)

        with self._state_lock:
            if self._generation != generation:
                self._logger.info("WeType completion wait superseded by a new session")
                return
            panel = self._find_panel()
            if panel is None:
                self._logger.info("WeType voice panel closed after submission")
                return
            sent = self._close_panel(panel)
        self._logger.info(
            "WeType voice panel close requested after submission timeout sent=%s",
            sent,
        )

    def start(self) -> bool:
        generation = self._begin_session()
        if not self._close_stale_panel():
            return False
        if not self._click_toolbar():
            self._logger.warning("WeType status-bar voice button was not available")
            return False
        if not self._wait_for_panel(present=True, timeout_seconds=_START_WAIT_SECONDS):
            self._logger.warning("WeType voice panel did not open after toolbar click")
            return False
        if not self._mark_active(generation):
            return False
        self._logger.info("WeType voice panel opened through status-bar toolbar")
        return True

    def stop(self) -> bool:
        with self._state_lock:
            generation = self._active_generation
        if generation is None:
            if self._find_panel() is None:
                self._logger.info("WeType voice panel already closed before submission")
                return True
            self._logger.warning("WeType voice panel is open without an owned session")
            return False
        if not self._is_current_generation(generation):
            return False
        if self._find_panel() is None:
            self._clear_active(generation)
            self._logger.info("WeType voice panel already closed before submission")
            return True
        if not self._click_toolbar():
            self._logger.warning("WeType voice button could not finish the session")
            return False
        self._clear_active(generation)
        try:
            self._schedule(lambda: self._finish_submission(generation))
        except RuntimeError:
            panel = self._find_panel()
            close_sent = panel is None or self._close_panel(panel)
            closed = close_sent and self._wait_for_panel(
                present=False,
                timeout_seconds=_STALE_CLOSE_WAIT_SECONDS,
            )
            if not closed:
                self._mark_active(generation)
            self._logger.exception(
                "WeType completion monitor could not start; immediate close "
                "sent=%s closed=%s",
                close_sent,
                closed,
            )
            if not closed:
                return False
        self._logger.info("WeType voice submission requested through status-bar toolbar")
        return True
