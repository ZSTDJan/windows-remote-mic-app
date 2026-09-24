"""Privacy-safe focus/result diagnostics for Windows voice input.

Only handles, process IDs, class names, native input metadata and text lengths are observed. Window
titles and user text are deliberately never read or logged.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
import os
from pathlib import PureWindowsPath
import time
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
    foreground_handle: int = 0
    foreground_thread_id: int = 0
    foreground_executable: str = ""
    process_query_status: str = "unavailable"
    keyboard_layout: int = 0
    foreground_stable: bool = False
    focus_editable: Optional[bool] = None
    focus_read_only: Optional[bool] = None
    focus_native_caret_detected: Optional[bool] = None
    focus_input_status: str = "unknown"


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


def _focus_input_state(user32, focus: int, class_name: str, info,
                       gui_info_ok: bool, stable: bool) -> dict:
    """Best-effort native metadata, never a verdict on user error or IME readiness."""
    result = dict(focus_editable=None, focus_read_only=None,
                  focus_native_caret_detected=None, focus_input_status="unknown")
    if not stable:
        result["focus_input_status"] = "foreground_changed"
        return result
    if not gui_info_ok or not focus or int(info.hwndFocus or 0) != focus:
        result["focus_input_status"] = "focus_unavailable"
        return result
    # False means no native caret observed on this focus, not no text cursor:
    # browsers and other custom controls can draw their own caret.
    result["focus_native_caret_detected"] = int(info.hwndCaret or 0) == focus
    if class_name.casefold() not in {
        "edit", "richedit", "richedit20a", "richedit20w", "richedit50w",
        "richeditd2dpt",
    }:
        result["focus_input_status"] = "unsupported_control"
        return result
    try:
        ctypes.set_last_error(0)
        style = int(user32.GetWindowLongW(wintypes.HWND(focus), -16))
        if not style and ctypes.get_last_error():
            result["focus_input_status"] = "style_unavailable"
            return result
        read_only = bool(style & 0x0800)  # ES_READONLY, also used by Rich Edit.
        result.update(focus_read_only=read_only,
                      focus_editable=not read_only and not bool(style & 0x08000000),
                      focus_input_status="native_edit_style")  # WS_DISABLED
    except (AttributeError, OSError, ValueError):
        result["focus_input_status"] = "style_unavailable"
    return result


def _process_basename(process_id: int) -> tuple[str, str]:
    """Query this PID afresh; never persist paths or cache identities across PID reuse."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, process_id)
    if not handle:
        return "", "open_failed"
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return "", "query_failed"
        name = PureWindowsPath(buffer.value).name
        if len(name) > 128 or any(ord(c) < 32 for c in name):
            return "", "name_rejected"
        return name, "captured"
    finally:
        kernel.CloseHandle(handle)


def _capture_windows_focus(*, include_text_length: bool = True) -> FocusSnapshot:
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
    user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
    user32.GetKeyboardLayout.restype = wintypes.HANDLE
    user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowLongW.restype = wintypes.LONG

    foreground = user32.GetForegroundWindow()
    if not foreground:
        return FocusSnapshot(True, error="no_foreground_window")
    process_id = wintypes.DWORD(0)
    thread_id = user32.GetWindowThreadProcessId(foreground, ctypes.byref(process_id))
    info = _GuiThreadInfo()
    info.cbSize = ctypes.sizeof(_GuiThreadInfo)
    focus = int(foreground)
    gui_info_ok = bool(thread_id and user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)))
    if gui_info_ok:
        focus = int(info.hwndFocus or foreground)
    try:
        executable, process_status = _process_basename(int(process_id.value))
    except (OSError, AttributeError, ValueError):
        executable, process_status = "", "query_failed"
    layout = int(user32.GetKeyboardLayout(thread_id) or 0) if thread_id else 0
    focus_class = _window_class_name(user32, focus)
    text_length = _window_text_length(user32, focus) if include_text_length else None
    input_state = _focus_input_state(user32, focus, focus_class, info, gui_info_ok, True)
    stable = int(user32.GetForegroundWindow() or 0) == int(foreground)
    if not stable:
        input_state = _focus_input_state(user32, focus, focus_class, info, gui_info_ok, False)
    return FocusSnapshot(
        supported=True,
        foreground_pid=int(process_id.value),
        foreground_class=_window_class_name(user32, int(foreground)),
        focus_handle=focus,
        focus_class=focus_class,
        text_length=text_length,
        foreground_handle=int(foreground),
        foreground_thread_id=int(thread_id),
        foreground_executable=executable,
        process_query_status=process_status,
        keyboard_layout=layout,
        foreground_stable=stable,
        **input_state,
    )


def capture_focus_snapshot(*, platform: Optional[str] = None,
                           include_text_length: bool = True) -> FocusSnapshot:
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return FocusSnapshot(False, error="unsupported_platform")
    try:
        return _capture_windows_focus() if include_text_length else _capture_windows_focus(include_text_length=False)
    except (AttributeError, OSError, ValueError):
        return FocusSnapshot(True, error="capture_failed")


def context_fields(snapshot: FocusSnapshot) -> dict:
    """Common local observation schema; HKL alone does not identify every TSF IME."""
    return dict(foreground_available=snapshot.supported and bool(snapshot.foreground_pid),
                foreground_pid=snapshot.foreground_pid,
                foreground_thread_id=snapshot.foreground_thread_id,
                foreground_handle=snapshot.foreground_handle,
                foreground_executable=snapshot.foreground_executable,
                process_query_status=snapshot.process_query_status,
                foreground_class=snapshot.foreground_class,
                foreground_stable=snapshot.foreground_stable,
                foreground_is_app=snapshot.foreground_pid == os.getpid(),
                focus_handle=snapshot.focus_handle, focus_class=snapshot.focus_class,
                focus_editable=snapshot.focus_editable,
                focus_read_only=snapshot.focus_read_only,
                focus_native_caret_detected=snapshot.focus_native_caret_detected,
                focus_input_status=snapshot.focus_input_status,
                keyboard_layout=snapshot.keyboard_layout,
                input_method_identity="not_inferred_from_layout",
                observation_status=snapshot.error or "captured")


class ContextTimeline:
    """Writer-owned, limited observation of local changes around an input action."""
    def __init__(self, capture=None, clock=None, environment=None):
        from .input_environment_diagnostics_windows import EnvironmentSampler
        self.capture = capture or (lambda: capture_focus_snapshot(include_text_length=False))
        self.clock = clock or time.monotonic
        self.environment = environment if environment is not None else EnvironmentSampler(clock=self.clock)
        self.deadline = 0.0
        self.next_sample = 0.0
        self.previous = None
        self.last_observation = 0.0
        self.context = {}
        self.voice_active = False

    def accept(self, item: dict) -> None:
        event = item.get("event")
        if event == "mapping_trigger" and self.context.get("attempt_id") and (self.voice_active or self.deadline > self.clock()):
            return  # A concurrent ordinary action must not truncate the voice observation.
        if event in ("attempt_started", "mapping_trigger"):
            self.voice_active = event == 'attempt_started'
            self.context = {k: item[k] for k in ("attempt_id", "gesture_id", "provider") if k in item}
            self.deadline = self.clock() + (30 if event == "attempt_started" else 2)
            self.previous = None
            self.next_sample = 0
        elif event == "attempt_finished" and item.get("attempt_id") == self.context.get("attempt_id"):
            self.voice_active = False
            # Recognition/automatic submission can happen after the held key is released.
            # Reopen a bounded tail even when a long recording exhausted the first window.
            self.deadline = self.clock() + 10
            self.next_sample = 0

    def poll(self) -> Optional[dict]:
        now = self.clock()
        if not self.deadline or now < self.next_sample:
            return None
        if now > self.deadline:
            self.deadline = 0
            return dict(self.context, phase="observation_ended", sampling_interval_ms=250,
                        observation_scope="local_input_environment", target_response="unknown")
        self.next_sample = now + 0.25
        try:
            fields = context_fields(self.capture())
        except Exception:
            fields = {"observation_status": "capture_failed"}
        try:
            fields.update(self.environment.sample(fields))
        except Exception:
            fields['environment_status'] = 'capture_failed'
        fields['observation_stage'] = ('voice_active' if self.voice_active else
                                       'after_voice' if self.context.get('attempt_id') else 'mapping')
        # Sampling timestamps describe freshness, not a change in the environment.
        comparison = {key: value for key, value in fields.items() if key not in {
            "process_sample_monotonic_ms", "response_sample_monotonic_ms"}}
        unchanged = comparison == self.previous
        if unchanged and now - self.last_observation < 5.0:
            return None
        phase = "initial" if self.previous is None else "heartbeat" if unchanged else "changed"
        self.previous = comparison
        self.last_observation = now
        return dict(self.context, **fields, phase=phase, sampling_interval_ms=250,
                    observation_scope="local_input_environment", target_response="unknown")


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
