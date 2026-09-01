"""Windows control path for WeType voice input.

The controller clicks the WeType status-bar microphone first, falls back to
an 80 ms global-shortcut tap, and closes the session through the same path
that opened it.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional, Sequence

from . import win32_input

_WM_CLOSE = 0x0010
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WETYPE_TOOLBAR_CLASS = "wetype.statusbar.window"
_VOICE_PANEL_TITLE = "语音输入"
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
    found: Optional[int] = None

    def visit(hwnd: int) -> bool:
        nonlocal found
        user32 = _user32()
        if not user32.IsWindowVisible(hwnd):
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
    """Open and submit one WeType session using the configured path order."""

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        find_panel: Callable[[], Optional[int]] = _find_voice_panel,
        click_toolbar: Callable[[], bool] = _click_toolbar,
        close_panel: Callable[[int], bool] = _close_panel,
        hotkey_tap: Callable[[Sequence[str]], None] = (
            win32_input.send_wetype_voice_key_combo_tap
        ),
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        schedule: Callable[[Callable[[], None]], None] = _start_daemon,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._find_panel = find_panel
        self._click_toolbar = click_toolbar
        self._close_panel = close_panel
        self._hotkey_tap = hotkey_tap
        self._sleep = sleep
        self._monotonic = monotonic
        self._schedule = schedule
        self._state_lock = threading.Lock()
        self._generation = 0
        self._source: Optional[str] = None

    def _begin_generation(self) -> int:
        with self._state_lock:
            self._generation += 1
            self._source = None
            return self._generation

    def _is_current_generation(self, generation: int) -> bool:
        with self._state_lock:
            return self._generation == generation

    def _remember_source(self, generation: int, source: str) -> bool:
        with self._state_lock:
            if self._generation != generation:
                return False
            self._source = source
            return True

    def _wait_for_panel(self, *, present: bool, timeout_seconds: float) -> bool:
        deadline = self._monotonic() + timeout_seconds
        while self._monotonic() < deadline:
            if (self._find_panel() is not None) == present:
                return True
            self._sleep(0.025)
        return (self._find_panel() is not None) == present

    def _close_stale_panel(self) -> None:
        panel = self._find_panel()
        if panel is None:
            return
        self._close_panel(panel)
        self._wait_for_panel(present=False, timeout_seconds=0.15)
        self._logger.info("WeType stale voice panel close requested")

    def _finish_submission(self, generation: int) -> None:
        deadline = self._monotonic() + _COMPLETION_WAIT_SECONDS
        while self._monotonic() < deadline:
            if not self._is_current_generation(generation):
                self._logger.info("WeType completion wait superseded by a new session")
                return
            if self._find_panel() is None:
                self._logger.info(
                    "WeType voice panel closed after submit; target text insertion "
                    "requires the separate focus/result diagnostic"
                )
                return
            self._sleep(_COMPLETION_POLL_SECONDS)

        with self._state_lock:
            if self._generation != generation:
                self._logger.info("WeType completion wait superseded by a new session")
                return
            panel = self._find_panel()
            if panel is None:
                self._logger.info(
                    "WeType voice panel closed after submit; target text insertion "
                    "requires the separate focus/result diagnostic"
                )
                return
            sent = self._close_panel(panel)
        self._logger.info(
            "WeType voice panel close requested after submit timeout sent=%s",
            sent,
        )

    def _schedule_completion(self, generation: int) -> None:
        self._schedule(lambda: self._finish_submission(generation))

    def start(self, tokens: Sequence[str]) -> bool:
        generation = self._begin_generation()
        self._close_stale_panel()

        if self._click_toolbar() and self._wait_for_panel(
            present=True, timeout_seconds=0.3
        ):
            if not self._remember_source(generation, "toolbar"):
                return False
            self._logger.info("WeType voice panel opened through status-bar toolbar")
            return True

        self._hotkey_tap(tokens)
        if self._wait_for_panel(present=True, timeout_seconds=0.5):
            if not self._remember_source(generation, "hotkey"):
                return False
            self._logger.info("WeType voice panel opened through 80 ms hotkey fallback")
            return True

        if self._click_toolbar() and self._wait_for_panel(
            present=True, timeout_seconds=0.4
        ):
            if not self._remember_source(generation, "toolbar"):
                return False
            self._logger.info("WeType voice panel opened through toolbar retry")
            return True

        self._logger.warning("WeType voice panel did not open after toolbar/hotkey attempts")
        return False

    def stop(self, tokens: Sequence[str]) -> bool:
        with self._state_lock:
            generation = self._generation
            source = self._source
            self._source = None
        panel = self._find_panel()
        if panel is None:
            self._logger.info("WeType voice panel already closed before submit")
            return True

        if source == "hotkey":
            self._hotkey_tap(tokens)
            if self._wait_for_panel(present=False, timeout_seconds=0.4):
                self._logger.info("WeType voice submitted through 80 ms hotkey")
                return True
            if not self._is_current_generation(generation):
                self._logger.info("WeType hotkey submit superseded by a new session")
                return True
            sent = self._click_toolbar()
            self._logger.info(
                "WeType hotkey submit kept panel open; toolbar fallback sent=%s",
                sent,
            )
            if sent:
                self._schedule_completion(generation)
            return sent

        if not self._is_current_generation(generation):
            self._logger.info("WeType toolbar submit superseded by a new session")
            return True
        sent = self._click_toolbar()
        self._logger.info("WeType voice submitted through status-bar toolbar sent=%s", sent)
        if sent:
            self._schedule_completion(generation)
        return sent

    def clear(self) -> None:
        with self._state_lock:
            self._generation += 1
            self._source = None
