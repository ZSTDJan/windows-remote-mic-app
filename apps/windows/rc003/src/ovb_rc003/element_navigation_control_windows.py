"""Lifecycle and Win32 command channel for the element navigator.

The bridge never performs UI Automation work in-process. It sends a small
command to one companion process, or starts that companion and delivers the
queued command as soon as its hidden command window is ready.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque, Optional, Sequence, Tuple

from . import dev_session, single_instance


ELEMENT_NAVIGATION_WINDOW_CLASS = "ElementNavigation.Command.v1"
ELEMENT_NAVIGATION_WINDOW_TITLE = "Element Navigation Command"
ELEMENT_NAVIGATION_MESSAGE_NAME = "ElementNavigation.Command.v1"

ELEMENT_NAVIGATION_COMMAND_TOGGLE = 1
ELEMENT_NAVIGATION_COMMAND_QUIT = 2

_COMMAND_TIMEOUT_MS = 120
_STARTUP_DELIVERY_TIMEOUT_SECONDS = 6.0
_STARTUP_DELIVERY_POLL_SECONDS = 0.03
_SHUTDOWN_DELIVERY_TIMEOUT_SECONDS = 1.5
_SHUTDOWN_WORKER_JOIN_GRACE_SECONDS = 0.25
_PROCESS_STOP_TIMEOUT_SECONDS = 1.0

_LOGGER = logging.getLogger(__name__)


def _remote_mic_quicker_state_file() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return ""
    return os.path.join(
        local_app_data,
        "RemoteMic",
        "RC003",
        "quicker-navigation.json",
    )


class CommandSendResult(Enum):
    DELIVERED = "delivered"
    NOT_RUNNING = "not_running"
    FAILED = "failed"


class ToggleResultKind(Enum):
    DELIVERED = "delivered"
    STARTED = "started"
    QUEUED = "queued"
    FAILED = "failed"


@dataclass(frozen=True)
class ToggleResult:
    kind: ToggleResultKind
    target_hwnd: int
    pid: Optional[int] = None
    error: str = ""


def _terminate_started_process(process: Optional[object]) -> bool:
    if process is None:
        return True
    if _started_process_exit_code(process) is not None:
        return True
    terminate = getattr(process, "terminate", None)
    if not callable(terminate):
        return False
    try:
        terminate()
    except Exception:
        return False
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return False
    try:
        wait(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
        return True
    except Exception:
        kill = getattr(process, "kill", None)
        if not callable(kill):
            return False
        try:
            kill()
            wait(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
        except Exception:
            return False
        return True


def _started_process_exit_code(process: Optional[object]) -> Optional[int]:
    if process is None:
        return None
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return None
    try:
        exit_code = poll()
    except Exception:
        return None
    return int(exit_code) if exit_code is not None else None


def _wait_for_started_process(
    process: Optional[object],
    timeout_seconds: float,
) -> bool:
    if process is None or _started_process_exit_code(process) is not None:
        return True
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return False
    try:
        wait(timeout=timeout_seconds)
    except Exception:
        return False
    return True


def _finish_owned_process(
    process: Optional[object],
    command_result: CommandSendResult,
) -> bool:
    if process is None:
        return True
    if command_result == CommandSendResult.DELIVERED and _wait_for_started_process(
        process,
        _PROCESS_STOP_TIMEOUT_SECONDS,
    ):
        return True
    return _terminate_started_process(process)


def _log_background_failure(reason: str) -> None:
    _LOGGER.warning(
        "element navigation companion startup failed asynchronously: reason=%s",
        reason,
    )


def build_element_navigation_command(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    owner_pid: Optional[int] = None,
) -> list[str]:
    """Build the hidden companion command for source and frozen runs."""

    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if executable is None:
        executable = sys.executable
    if owner_pid is None:
        owner_pid = os.getpid()
    if not executable:
        raise RuntimeError("sys.executable is empty")
    managed_arguments = [
        "--managed-companion",
        "--owner-pid",
        str(max(0, int(owner_pid))),
    ]
    quicker_state_file = _remote_mic_quicker_state_file()
    if quicker_state_file:
        managed_arguments.extend(
            ["--quicker-state-file", quicker_state_file]
        )
    if frozen:
        return dev_session.mark_command(
            [executable, "--element-navigation", *managed_arguments]
        )
    return dev_session.mark_command(
        [
            executable,
            "-m",
            "ovb_rc003",
            "--element-navigation",
            *managed_arguments,
        ]
    )


def _require_windows() -> None:
    if sys.platform != "win32":
        raise OSError("element navigation control is only available on Windows")


def _real_foreground_window() -> int:
    _require_windows()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    handle = user32.GetForegroundWindow()
    return int(handle) if handle else 0


def _real_find_command_window() -> int:
    _require_windows()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    handle = user32.FindWindowW(
        ELEMENT_NAVIGATION_WINDOW_CLASS,
        ELEMENT_NAVIGATION_WINDOW_TITLE,
    )
    return int(handle) if handle else 0


def _real_send_window_command(
    command_window: int,
    command: int,
    target_hwnd: int,
) -> bool:
    _require_windows()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
    user32.RegisterWindowMessageW.restype = wintypes.UINT
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
    message_id = int(
        user32.RegisterWindowMessageW(ELEMENT_NAVIGATION_MESSAGE_NAME)
    )
    if not message_id:
        return False
    result = ctypes.c_size_t()
    smto_abort_if_hung = 0x0002
    smto_block = 0x0001
    delivered = user32.SendMessageTimeoutW(
        command_window,
        message_id,
        int(command),
        int(target_hwnd),
        smto_abort_if_hung | smto_block,
        _COMMAND_TIMEOUT_MS,
        ctypes.byref(result),
    )
    return bool(delivered and result.value)


def send_element_navigation_command(
    command: int,
    target_hwnd: int = 0,
    *,
    _find_window: Callable[[], int] = _real_find_command_window,
    _send_window_command: Callable[[int, int, int], bool] = (
        _real_send_window_command
    ),
) -> CommandSendResult:
    try:
        command_window = int(_find_window())
    except Exception:
        return CommandSendResult.FAILED
    if command_window <= 0:
        return CommandSendResult.NOT_RUNNING
    try:
        delivered = _send_window_command(
            command_window,
            int(command),
            int(target_hwnd),
        )
    except Exception:
        return CommandSendResult.FAILED
    return CommandSendResult.DELIVERED if delivered else CommandSendResult.FAILED


class ElementNavigationClient:
    """Non-blocking bridge-side controller with ordered startup delivery."""

    def __init__(
        self,
        *,
        foreground_window: Callable[[], int] = _real_foreground_window,
        send_command: Callable[[int, int], CommandSendResult] = (
            send_element_navigation_command
        ),
        instance_running: Callable[[], bool] = (
            single_instance.element_navigation_instance_running
        ),
        build_command: Callable[[], Sequence[str]] = build_element_navigation_command,
        popen: Callable[..., object] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        background_failure: Callable[[str], None] = _log_background_failure,
    ) -> None:
        self._foreground_window = foreground_window
        self._send_command = send_command
        self._instance_running = instance_running
        self._build_command = build_command
        self._popen = popen
        self._sleep = sleep
        self._monotonic = monotonic
        self._background_failure = background_failure
        self._lock = threading.Lock()
        self._pending: Deque[Tuple[int, int]] = deque()
        self._delivery_thread: Optional[threading.Thread] = None
        self._starting_process: Optional[object] = None
        self._owned_process: Optional[object] = None
        self._shutdown_requested = False
        self._shutdown_result = CommandSendResult.NOT_RUNNING
        self._closed = False

    def toggle(self) -> ToggleResult:
        with self._lock:
            if self._closed:
                return ToggleResult(
                    ToggleResultKind.FAILED,
                    0,
                    error="client_shutdown",
                )
        try:
            target_hwnd = max(0, int(self._foreground_window()))
        except Exception as exc:
            return ToggleResult(
                ToggleResultKind.FAILED,
                0,
                error=type(exc).__name__,
            )

        with self._lock:
            if self._closed:
                return ToggleResult(
                    ToggleResultKind.FAILED,
                    target_hwnd,
                    error="client_shutdown",
                )
            if self._delivery_thread is not None:
                self._pending.append(
                    (ELEMENT_NAVIGATION_COMMAND_TOGGLE, target_hwnd)
                )
                return ToggleResult(ToggleResultKind.QUEUED, target_hwnd)

        sent = self._send_command(ELEMENT_NAVIGATION_COMMAND_TOGGLE, target_hwnd)
        if sent == CommandSendResult.DELIVERED:
            return ToggleResult(ToggleResultKind.DELIVERED, target_hwnd)
        if sent == CommandSendResult.FAILED:
            return ToggleResult(
                ToggleResultKind.FAILED,
                target_hwnd,
                error="command_delivery_failed",
            )

        with self._lock:
            if self._closed:
                return ToggleResult(
                    ToggleResultKind.FAILED,
                    target_hwnd,
                    error="client_shutdown",
                )
            if self._delivery_thread is not None:
                self._pending.append(
                    (ELEMENT_NAVIGATION_COMMAND_TOGGLE, target_hwnd)
                )
                return ToggleResult(ToggleResultKind.QUEUED, target_hwnd)
            self._shutdown_requested = False
            self._pending.append((ELEMENT_NAVIGATION_COMMAND_TOGGLE, target_hwnd))
            try:
                already_starting = bool(self._instance_running())
            except Exception:
                already_starting = False
            process = None
            if not already_starting:
                try:
                    command = list(self._build_command())
                    kwargs = {}
                    if self._popen is subprocess.Popen and sys.platform == "win32":
                        kwargs["creationflags"] = getattr(
                            subprocess, "CREATE_NO_WINDOW", 0
                        )
                    process = self._popen(command, **kwargs)
                except Exception as exc:
                    self._pending.clear()
                    return ToggleResult(
                        ToggleResultKind.FAILED,
                        target_hwnd,
                        error=type(exc).__name__,
                    )
            self._starting_process = process
            if process is not None:
                self._owned_process = process
            worker = threading.Thread(
                target=self._deliver_pending,
                name="element-navigation-command-delivery",
                daemon=True,
            )
            self._delivery_thread = worker
            try:
                worker.start()
            except Exception as exc:
                self._pending.clear()
                self._delivery_thread = None
                self._starting_process = None
                if self._owned_process is process:
                    self._owned_process = None
                _terminate_started_process(process)
                return ToggleResult(
                    ToggleResultKind.FAILED,
                    target_hwnd,
                    error=type(exc).__name__,
                )
            return ToggleResult(
                ToggleResultKind.QUEUED if already_starting else ToggleResultKind.STARTED,
                target_hwnd,
                pid=getattr(process, "pid", None) if process is not None else None,
            )

    def shutdown(self) -> CommandSendResult:
        process_to_stop = None
        owned_process = None
        with self._lock:
            self._closed = True
            worker = self._delivery_thread
            if worker is not None:
                self._pending.clear()
                self._shutdown_result = CommandSendResult.NOT_RUNNING
                if self._starting_process is not None:
                    process_to_stop = self._owned_process or self._starting_process
                    self._starting_process = None
                    self._owned_process = None
                    self._shutdown_requested = False
                    self._delivery_thread = None
                else:
                    self._shutdown_requested = True
            else:
                owned_process = self._owned_process
                self._owned_process = None
        if worker is None:
            result = self._send_command(ELEMENT_NAVIGATION_COMMAND_QUIT, 0)
            return (
                result
                if _finish_owned_process(owned_process, result)
                else CommandSendResult.FAILED
            )
        if worker is threading.current_thread():
            return CommandSendResult.FAILED

        if process_to_stop is not None:
            process_stopped = _terminate_started_process(process_to_stop)
            worker.join(timeout=_SHUTDOWN_WORKER_JOIN_GRACE_SECONDS)
            return (
                CommandSendResult.NOT_RUNNING
                if process_stopped and not worker.is_alive()
                else CommandSendResult.FAILED
            )

        worker.join(timeout=_SHUTDOWN_DELIVERY_TIMEOUT_SECONDS)
        if worker.is_alive():
            with self._lock:
                if self._delivery_thread is worker:
                    self._pending.clear()
                    self._starting_process = None
                    owned_process = self._owned_process
                    self._owned_process = None
                    self._shutdown_requested = False
                    self._delivery_thread = None
            worker.join(timeout=_SHUTDOWN_WORKER_JOIN_GRACE_SECONDS)
            _terminate_started_process(owned_process)
            return CommandSendResult.FAILED
        with self._lock:
            result = self._shutdown_result
            owned_process = self._owned_process
            self._owned_process = None
        return (
            result
            if _finish_owned_process(owned_process, result)
            else CommandSendResult.FAILED
        )

    def _deliver_pending(self) -> None:
        worker = threading.current_thread()
        deadline = self._monotonic() + _STARTUP_DELIVERY_TIMEOUT_SECONDS
        failure_reason = "startup_timeout"
        try:
            while self._monotonic() < deadline:
                with self._lock:
                    if self._delivery_thread is not worker:
                        return
                    shutting_down = self._shutdown_requested
                    command = (
                        (ELEMENT_NAVIGATION_COMMAND_QUIT, 0)
                        if shutting_down
                        else (self._pending[0] if self._pending else None)
                    )
                    if command is None:
                        self._delivery_thread = None
                        return
                result = self._send_command(command[0], command[1])
                if result == CommandSendResult.DELIVERED:
                    with self._lock:
                        if self._delivery_thread is not worker:
                            return
                        if shutting_down:
                            self._shutdown_result = CommandSendResult.DELIVERED
                            self._shutdown_requested = False
                            self._pending.clear()
                            self._delivery_thread = None
                            return
                        self._starting_process = None
                        if self._pending and self._pending[0] == command:
                            self._pending.popleft()
                    continue
                with self._lock:
                    starting_process = self._starting_process
                exit_code = _started_process_exit_code(starting_process)
                if exit_code is not None:
                    failure_reason = f"companion_exited:{exit_code}"
                    break
                if result == CommandSendResult.FAILED:
                    failure_reason = "command_delivery_timeout"
                self._sleep(_STARTUP_DELIVERY_POLL_SECONDS)
        except Exception as exc:
            failure_reason = f"worker_error:{type(exc).__name__}"

        process_to_stop = None
        report_failure = False
        with self._lock:
            if self._delivery_thread is worker:
                process_to_stop = self._owned_process or self._starting_process
                self._starting_process = None
                self._owned_process = None
                self._pending.clear()
                report_failure = not self._shutdown_requested
                if self._shutdown_requested:
                    self._shutdown_result = CommandSendResult.FAILED
                self._shutdown_requested = False
                self._delivery_thread = None
        process_stopped = _terminate_started_process(process_to_stop)
        if report_failure:
            if not process_stopped:
                failure_reason += ":cleanup_failed"
            try:
                self._background_failure(failure_reason)
            except Exception:
                _LOGGER.exception(
                    "element navigation background failure reporter crashed"
                )


_DEFAULT_CLIENT = ElementNavigationClient()


def toggle_element_navigation() -> ToggleResult:
    return _DEFAULT_CLIENT.toggle()


def shutdown_element_navigation() -> CommandSendResult:
    return _DEFAULT_CLIENT.shutdown()


class ElementNavigationCommandServer:
    """Invisible Win32 window that forwards tiny commands to the Qt loop."""

    _WM_CLOSE = 0x0010
    _WM_DESTROY = 0x0002

    def __init__(self, callback: Callable[[int, int], None]) -> None:
        self._callback = callback
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="element-navigation-command-window",
            daemon=True,
        )
        self._hwnd = 0
        self._startup_error: Optional[BaseException] = None
        self._window_proc = None

    def start(self, timeout_seconds: float = 3.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout_seconds):
            raise RuntimeError("元素导航命令窗口启动超时")
        if self._startup_error is not None or self._hwnd <= 0:
            raise RuntimeError("元素导航命令窗口启动失败") from self._startup_error

    def stop(self) -> None:
        if self._hwnd > 0 and self._thread.is_alive():
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostMessageW.argtypes = (
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                )
                user32.PostMessageW.restype = wintypes.BOOL
                user32.PostMessageW(self._hwnd, self._WM_CLOSE, 0, 0)
            except Exception:
                pass
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        atom = 0
        hinstance = 0
        try:
            _require_windows()
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            lresult = ctypes.c_ssize_t
            window_proc_type = ctypes.WINFUNCTYPE(
                lresult,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            class WindowClass(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", window_proc_type),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", wintypes.HICON),
                ]

            user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
            user32.RegisterWindowMessageW.restype = wintypes.UINT
            message_id = int(
                user32.RegisterWindowMessageW(ELEMENT_NAVIGATION_MESSAGE_NAME)
            )
            if not message_id:
                raise OSError("RegisterWindowMessageW failed")

            user32.DefWindowProcW.argtypes = (
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.DefWindowProcW.restype = lresult
            user32.DestroyWindow.argtypes = (wintypes.HWND,)
            user32.DestroyWindow.restype = wintypes.BOOL
            user32.PostQuitMessage.argtypes = (ctypes.c_int,)
            user32.PostQuitMessage.restype = None
            user32.GetMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.GetMessageW.restype = wintypes.BOOL
            user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
            user32.TranslateMessage.restype = wintypes.BOOL
            user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
            user32.DispatchMessageW.restype = lresult
            user32.RegisterClassExW.argtypes = (ctypes.POINTER(WindowClass),)
            user32.RegisterClassExW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = (
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            )
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.UnregisterClassW.argtypes = (
                wintypes.LPCWSTR,
                wintypes.HINSTANCE,
            )
            user32.UnregisterClassW.restype = wintypes.BOOL
            kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            hinstance = int(kernel32.GetModuleHandleW(None))

            @window_proc_type
            def window_proc(hwnd, message, wparam, lparam):
                if int(message) == message_id:
                    try:
                        self._callback(int(wparam), int(lparam))
                    except Exception:
                        return 0
                    return 1
                if int(message) == self._WM_CLOSE:
                    user32.DestroyWindow(hwnd)
                    return 0
                if int(message) == self._WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)

            self._window_proc = window_proc
            window_class = WindowClass()
            window_class.cbSize = ctypes.sizeof(WindowClass)
            window_class.lpfnWndProc = window_proc
            window_class.hInstance = hinstance
            window_class.lpszClassName = ELEMENT_NAVIGATION_WINDOW_CLASS
            atom = int(user32.RegisterClassExW(ctypes.byref(window_class)))
            if not atom:
                raise OSError("RegisterClassExW failed")
            handle = user32.CreateWindowExW(
                0,
                ELEMENT_NAVIGATION_WINDOW_CLASS,
                ELEMENT_NAVIGATION_WINDOW_TITLE,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                hinstance,
                None,
            )
            self._hwnd = int(handle) if handle else 0
            if self._hwnd <= 0:
                raise OSError("CreateWindowExW failed")
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            self._hwnd = 0
            if atom and hinstance:
                try:
                    user32.UnregisterClassW(
                        ELEMENT_NAVIGATION_WINDOW_CLASS,
                        hinstance,
                    )
                except Exception:
                    pass
