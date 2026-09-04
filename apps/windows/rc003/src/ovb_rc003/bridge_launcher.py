"""Own the bridge worker inside the single desktop application process.

Normal product calls start one in-process worker. Explicit commands and the
``Popen`` seams remain only for compatibility tooling and deterministic tests;
the settings window and login startup never create a second resident process.
"""

from __future__ import annotations

import subprocess
import os
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import dev_session, single_instance

# Reused, not redefined - see module docstring's ALREADY_RUNNING note.
ALREADY_RUNNING_EXIT_CODE = single_instance.DUPLICATE_INSTANCE_EXIT_CODE
SETTINGS_LAUNCH_FLAG = "--bridge-from-settings"

DEFAULT_GRACE_CHECKS = 10
DEFAULT_POLL_INTERVAL_SECONDS = 0.15


class BridgeLaunchConfigurationError(Exception):
    """Raised when a launch command cannot be constructed at all (e.g.
    ``sys.executable`` is empty - which CPython documents can happen in some
    embedding scenarios). Fails closed rather than handing ``Popen`` an
    empty/garbage argv[0].
    """


@dataclass(frozen=True)
class SettingsLaunchResult:
    command: Tuple[str, ...]
    pid: Optional[int] = None
    error: Optional[str] = None

    @property
    def started(self) -> bool:
        return self.pid is not None and self.error is None


def build_launch_command(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> List[str]:
    """Builds the bridge launch command for the CURRENT process shape.
    Always appends ``--bridge``: the no-argument form of this exe now opens
    the settings window, so the bridge must be requested explicitly.
    ``frozen``/``executable`` are injectable so tests can exercise both
    branches deterministically on any OS - production callers should never
    pass them.
    """

    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if executable is None:
        executable = sys.executable

    if not executable:
        raise BridgeLaunchConfigurationError(
            "sys.executable is empty; cannot construct a bridge launch command"
        )

    if frozen:
        # The same frozen exe handles both modes: with no arguments (or
        # --settings) it opens the settings window, and with --bridge (as
        # launched here) it starts the bridge - see __main__.py's dispatch.
        return dev_session.mark_command(
            [executable, "--bridge", SETTINGS_LAUNCH_FLAG]
        )
    # The current interpreter, `-m ovb_rc003 --bridge`.
    return dev_session.mark_command(
        [
            executable,
            "-m",
            "ovb_rc003",
            "--bridge",
            SETTINGS_LAUNCH_FLAG,
        ]
    )


def build_settings_command(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> List[str]:
    """Build the explicit settings command used by the bridge tray."""

    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if executable is None:
        executable = sys.executable
    if not executable:
        raise BridgeLaunchConfigurationError(
            "sys.executable is empty; cannot construct a settings launch command"
        )
    if frozen:
        return dev_session.mark_command([executable, "--settings"])
    return dev_session.mark_command(
        [executable, "-m", "ovb_rc003", "--settings"]
    )


def launch_settings(
    command: Optional[Sequence[str]] = None,
    *,
    _popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
    _popen_kwargs: Optional[Dict[str, object]] = None,
) -> SettingsLaunchResult:
    """Open a settings window from the bridge tray without using a shell."""

    resolved_command = tuple(command) if command is not None else tuple(
        build_settings_command()
    )
    popen_kwargs = dict(_popen_kwargs) if _popen_kwargs is not None else {}
    if _popen is subprocess.Popen and sys.platform == "win32":
        popen_kwargs.setdefault(
            "creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    try:
        process = _popen(list(resolved_command), **popen_kwargs)
    except OSError as exc:
        return SettingsLaunchResult(
            command=resolved_command,
            error=type(exc).__name__,
        )
    return SettingsLaunchResult(
        command=resolved_command,
        pid=getattr(process, "pid", None),
    )


class LaunchOutcome(Enum):
    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    QUICK_EXIT = "quick_exit"
    LAUNCH_FAILED = "launch_failed"
    STATUS_UNKNOWN = "status_unknown"


@dataclass(frozen=True)
class LaunchResult:
    outcome: LaunchOutcome
    command: Tuple[str, ...]
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None


@dataclass
class PendingBridgeLaunch:
    """A created process whose grace-period result is still pending.

    Settings keeps this object on the GUI thread and polls it from a short Qt
    timer. No sleep or worker thread is needed, so the window can paint every
    real launch stage while the same outcome contract remains in force.
    """

    command: Tuple[str, ...]
    process: object
    pid: Optional[int]
    checks_remaining: int


class _InProcessBridgeHandle:
    """Process-like view of the bridge worker running in this application."""

    pid = os.getpid()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._exit_code: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_callback: Optional[Callable[[], None]] = None
        self._reconnect_callback: Optional[Callable[[], None]] = None
        self._stop_requested = False
        self._finished = threading.Event()

    def attach(self, thread: threading.Thread) -> None:
        self._thread = thread

    def finish(self, exit_code: int) -> None:
        with self._lock:
            self._exit_code = int(exit_code)
            self._stop_callback = None
            self._reconnect_callback = None
        self._finished.set()

    def bind_stop(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._stop_requested:
                call_now = True
            else:
                self._stop_callback = callback
                call_now = False
        if call_now:
            callback()

    def request_stop(self) -> None:
        with self._lock:
            if self._stop_requested:
                return
            self._stop_requested = True
            callback = self._stop_callback
            self._stop_callback = None
        if callback is not None:
            callback()

    def bind_reconnect(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._exit_code is None and not self._stop_requested:
                self._reconnect_callback = callback

    def request_reconnect_now(self) -> bool:
        with self._lock:
            if self._exit_code is not None or self._stop_requested:
                return False
            callback = self._reconnect_callback
        if callback is None:
            return False
        callback()
        return True

    def wait(self, timeout: float) -> bool:
        return self._finished.wait(max(0.0, float(timeout)))

    def poll(self) -> Optional[int]:
        with self._lock:
            return self._exit_code

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and self.poll() is None)


_IN_PROCESS_COMMAND = ("<in-process-bridge>",)
_in_process_lock = threading.Lock()
_in_process_handle: Optional[_InProcessBridgeHandle] = None


def _run_in_process_bridge(handle: _InProcessBridgeHandle) -> None:
    """Own the legacy bridge mutex while the bridge lives in this process."""

    from . import app

    exit_code = 0
    try:
        with single_instance.BridgeInstanceGuard():
            app.main(
                show_notification_icon=False,
                on_runtime_ready=handle.bind_stop,
                on_reconnect_ready=handle.bind_reconnect,
            )
    except single_instance.DuplicateInstanceError:
        exit_code = single_instance.DUPLICATE_INSTANCE_EXIT_CODE
    except single_instance.SingleInstanceUnavailableError:
        exit_code = single_instance.GUARD_UNAVAILABLE_EXIT_CODE
    except single_instance.MutexCleanupError:
        exit_code = single_instance.CLEANUP_FAILED_EXIT_CODE
    except Exception:
        exit_code = 1
    finally:
        handle.finish(exit_code)


def in_process_bridge_running() -> bool:
    with _in_process_lock:
        handle = _in_process_handle
        return bool(handle is not None and handle.is_alive)


def reconnect_in_process_bridge_now() -> Optional[bool]:
    """Wake this process's retry backoff; return None for a legacy owner."""

    global _in_process_handle
    with _in_process_lock:
        handle = _in_process_handle
        if handle is None:
            return None
        if not handle.is_alive:
            _in_process_handle = None
            return None
        return handle.request_reconnect_now()


def stop_in_process_bridge(*, timeout: float = 7.0) -> Optional[bool]:
    """Stop this process's worker; return None when it is not the owner."""

    global _in_process_handle
    with _in_process_lock:
        handle = _in_process_handle
        if handle is None:
            return None
        if not handle.is_alive:
            _in_process_handle = None
            # A finished launch no longer owns the bridge mutex. Returning
            # None lets the caller continue to the legacy standalone bridge
            # probe instead of falsely reporting that another live service
            # was stopped by this process.
            return None
        handle.request_stop()
    stopped = handle.wait(timeout)
    if stopped:
        with _in_process_lock:
            if _in_process_handle is handle:
                _in_process_handle = None
    return stopped


def start_in_process_bridge(
    *, grace_checks: int = DEFAULT_GRACE_CHECKS
) -> Union[LaunchResult, PendingBridgeLaunch]:
    """Start the single bridge worker without creating another OS process."""

    global _in_process_handle
    with _in_process_lock:
        current = _in_process_handle
        if current is not None and current.is_alive:
            return LaunchResult(
                outcome=LaunchOutcome.ALREADY_RUNNING,
                command=_IN_PROCESS_COMMAND,
                pid=os.getpid(),
                exit_code=ALREADY_RUNNING_EXIT_CODE,
            )
        handle = _InProcessBridgeHandle()
        thread = threading.Thread(
            target=_run_in_process_bridge,
            args=(handle,),
            name="remote-mic-bridge",
            daemon=False,
        )
        handle.attach(thread)
        _in_process_handle = handle
        try:
            thread.start()
        except Exception as exc:
            handle.finish(1)
            _in_process_handle = None
            return LaunchResult(
                outcome=LaunchOutcome.LAUNCH_FAILED,
                command=_IN_PROCESS_COMMAND,
                pid=os.getpid(),
                error=type(exc).__name__,
            )
    checks_remaining = max(0, int(grace_checks))
    if checks_remaining == 0:
        return LaunchResult(
            outcome=LaunchOutcome.STARTED,
            command=_IN_PROCESS_COMMAND,
            pid=os.getpid(),
        )
    return PendingBridgeLaunch(
        command=_IN_PROCESS_COMMAND,
        process=handle,
        pid=os.getpid(),
        checks_remaining=checks_remaining,
    )


def launch_in_process_bridge(
    *,
    grace_checks: int = DEFAULT_GRACE_CHECKS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    _sleep: Callable[[float], None] = time.sleep,
) -> LaunchResult:
    """Synchronous wrapper used only by bounded background workflows."""

    attempt = start_in_process_bridge(grace_checks=grace_checks)
    if isinstance(attempt, LaunchResult):
        return attempt
    while True:
        _sleep(poll_interval_seconds)
        result = poll_bridge_launch(attempt)
        if result is not None:
            return result


def _result_for_exit(
    command: Tuple[str, ...],
    pid: Optional[int],
    exit_code: int,
) -> LaunchResult:
    if exit_code == ALREADY_RUNNING_EXIT_CODE:
        outcome = LaunchOutcome.ALREADY_RUNNING
    else:
        outcome = LaunchOutcome.QUICK_EXIT
    return LaunchResult(
        outcome=outcome,
        command=command,
        pid=pid,
        exit_code=exit_code,
    )


def start_bridge_launch(
    command: Optional[Sequence[str]] = None,
    *,
    grace_checks: int = DEFAULT_GRACE_CHECKS,
    _popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
    _popen_kwargs: Optional[Dict[str, object]] = None,
) -> Union[LaunchResult, PendingBridgeLaunch]:
    """Start the bridge and perform only the immediate status poll.

    Production calls without an explicit command run inside the current
    desktop process. Explicit commands and injected ``Popen`` callables keep
    the bounded subprocess path for compatibility tests and tooling.
    """

    if (
        command is None
        and _popen is subprocess.Popen
        and _popen_kwargs is None
    ):
        return start_in_process_bridge(grace_checks=grace_checks)

    resolved_command: Tuple[str, ...] = (
        tuple(command) if command is not None else tuple(build_launch_command())
    )
    popen_kwargs = dict(_popen_kwargs) if _popen_kwargs is not None else {}
    if _popen is subprocess.Popen and sys.platform == "win32":
        popen_kwargs.setdefault(
            "creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    try:
        process = _popen(list(resolved_command), **popen_kwargs)
    except OSError as exc:
        return LaunchResult(
            outcome=LaunchOutcome.LAUNCH_FAILED,
            command=resolved_command,
            error=type(exc).__name__,
        )

    pid = getattr(process, "pid", None)
    try:
        exit_code = process.poll()
    except OSError as exc:
        return LaunchResult(
            outcome=LaunchOutcome.STATUS_UNKNOWN,
            command=resolved_command,
            pid=pid,
            error=type(exc).__name__,
        )
    if exit_code is not None:
        return _result_for_exit(resolved_command, pid, exit_code)
    checks_remaining = max(0, int(grace_checks))
    if checks_remaining == 0:
        return LaunchResult(
            outcome=LaunchOutcome.STARTED,
            command=resolved_command,
            pid=pid,
        )
    return PendingBridgeLaunch(
        command=resolved_command,
        process=process,
        pid=pid,
        checks_remaining=checks_remaining,
    )


def poll_bridge_launch(pending: PendingBridgeLaunch) -> Optional[LaunchResult]:
    """Poll one pending launch once; never sleeps or blocks."""

    try:
        exit_code = pending.process.poll()
    except OSError as exc:
        return LaunchResult(
            outcome=LaunchOutcome.STATUS_UNKNOWN,
            command=pending.command,
            pid=pending.pid,
            error=type(exc).__name__,
        )
    if exit_code is not None:
        return _result_for_exit(pending.command, pending.pid, exit_code)
    pending.checks_remaining -= 1
    if pending.checks_remaining <= 0:
        return LaunchResult(
            outcome=LaunchOutcome.STARTED,
            command=pending.command,
            pid=pending.pid,
        )
    return None


def launch_bridge(
    command: Optional[Sequence[str]] = None,
    *,
    grace_checks: int = DEFAULT_GRACE_CHECKS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    _popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
    _sleep: Callable[[float], None] = time.sleep,
    _popen_kwargs: Optional[Dict[str, object]] = None,
) -> LaunchResult:
    """Starts the bridge and watches it for a short grace period to tell a
    process that is actually running apart from one that merely got created
    and then immediately died. Never raises for an ordinary launch failure
    (``OSError`` from ``_popen``) - that is reported as ``LAUNCH_FAILED``
    instead, since this is called directly from a Tk button handler that
    must not crash the settings window over a failed launch.
    """

    attempt = start_bridge_launch(
        command,
        grace_checks=grace_checks,
        _popen=_popen,
        _popen_kwargs=_popen_kwargs,
    )
    if isinstance(attempt, LaunchResult):
        return attempt
    while True:
        _sleep(poll_interval_seconds)
        result = poll_bridge_launch(attempt)
        if result is not None:
            return result
