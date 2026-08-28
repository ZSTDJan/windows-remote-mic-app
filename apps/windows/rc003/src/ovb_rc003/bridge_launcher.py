"""Launches the bridge process from the settings window (XRBM-029), and
reports what actually happened - not just "a process was created". This
module never conflates a launched/still-running process with "RC003 is
connected": that fact is only observable from ``app.log`` (see
``logging_setup.py``), never from process liveness alone.

Command construction (``build_launch_command``) covers exactly the two ways
this package's own entry point (``__main__.py``) is ever invoked, so a
future third mode cannot silently fall through unnoticed:

- **Frozen** (the packaged ``RemoteMicRC003.exe``, built from
  ``src/launcher.py`` - see that module's docstring): ``sys.executable`` IS
  that same exe, and running it again with ``--bridge`` enters
  ``__main__.main()``'s ``_run_bridge()`` branch. The settings-only hidden
  flag also tells a duplicate child to return its stable exit code without
  opening a modal notice that would block launch-outcome polling. The
  no-argument form opens settings, so the bridge is always launched
  EXPLICITLY - never ``--settings``, which would just open a second settings
  window instead of starting the bridge.
- **Source** (``python -m ovb_rc003``): ``sys.executable`` is the
  interpreter itself; ``[sys.executable, "-m", "ovb_rc003", "--bridge"]``
  enters the same bridge branch. This relies on the child inheriting the
  parent process's environment (``subprocess.Popen`` does this by default) -
  in particular ``PYTHONPATH=src``, which the settings window's own process
  needed to have been started with in order to import ``ovb_rc003`` at all
  (see the root README's "Running from source" section).

Both branches deliberately append ``--bridge`` plus the hidden settings-
launch marker, and never ``--settings``:
that argument would recursively open another settings window instead of
starting the bridge (In-scope item 2's "不得递归打开 --settings").

Launch-outcome detection (``launch_bridge``) distinguishes five states by
polling the child for a short grace period rather than assuming
"``Popen()`` did not raise" means "the bridge is running":

- ``STARTED``: the process is still alive once the grace period elapses -
  the best evidence available from process state alone that startup is
  proceeding, NOT proof of an RC003 connection.
- ``ALREADY_RUNNING``: the process exited within the grace period with
  exactly ``single_instance.DUPLICATE_INSTANCE_EXIT_CODE`` - the
  single-instance guard in ``single_instance.py``/``__main__.py`` refused a
  second concurrent bridge instance. Reusing that exact constant (rather
  than redefining a second one here) keeps the two modules from silently
  drifting apart if the exit code is ever renumbered.
- ``QUICK_EXIT``: the process exited within the grace period with any OTHER
  code (including ``GUARD_UNAVAILABLE_EXIT_CODE``/``CLEANUP_FAILED_EXIT_CODE``
  or an unhandled exception's implicit ``1``) - a real failure, whose exact
  code is always surfaced to the caller rather than swallowed, so a user or
  reviewer can distinguish it from a clean exit without guessing.
- ``LAUNCH_FAILED``: ``Popen()`` itself raised ``OSError`` (e.g. the target
  executable is missing or not executable) - no process was ever created at
  all.
- ``STATUS_UNKNOWN``: a process was created, but querying its status raised
  ``OSError``. The owner PID is retained and the UI must not tell the user to
  retry, because the child may still be running.

Testability: every OS-facing call (``_popen``, ``_sleep``) is injectable, so
tests/test_bridge_launcher.py drives all four outcomes deterministically -
including the grace-period polling loop - without spawning a real process or
sleeping in real wall-clock time, the same dependency-injection pattern this
package's other Win32-facing modules already use (see e.g.
``single_instance.py``'s ``_create_mutex``/``_release_mutex``/
``_close_handle`` parameters).
"""

from __future__ import annotations

import subprocess
import sys
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
    """Create the bridge process and perform only the immediate status poll.

    A still-live child is returned as ``PendingBridgeLaunch``. Callers can
    poll it without blocking; ``launch_bridge`` below remains the synchronous
    compatibility wrapper for non-GUI callers.
    """

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
