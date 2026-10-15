"""Disposable ATVV query owned by one candidate-selection attempt.

Only duplicate BLE records use this internal worker. The device ID crosses an
anonymous input pipe, never argv, files or logs; the exit code is the verdict.
Cancellation/timeout confirms process exit before releasing ownership. This is
shipped with the BLE transport, not a standalone utility or a persistent service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

FLAG = "--probe-rc003-candidate"
_MAX_REQUEST_BYTES = 2048
_POLL_SECONDS = 0.02
_TERMINATE_WAIT_SECONDS = 1.0
_KILL_WAIT_SECONDS = 1.0
_MAX_DIAGNOSTIC_BYTES = 2048
_PROBE_LOG_MESSAGES = {
    "worker": "ATVV candidate probe worker failed: error_type=%s",
    "unavailable": "ATVV candidate probe unavailable: error_type=%s",
    "service_cleanup": "ATVV candidate probe service cleanup failed: error_type=%s",
    "device_cleanup": "ATVV candidate probe device cleanup failed: error_type=%s",
}


class ProbeCleanupError(RuntimeError):
    """A child is still owned: do not start another query over it."""


@dataclass
class _Owner:
    process: subprocess.Popen | None = None
    active: bool = True


_owners: dict[str, _Owner] = {}
_owners_lock = threading.Lock()


def _command() -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        return [sys.executable, FLAG], env
    executable = sys.executable
    if sys.platform == "win32" and sys.prefix != sys.base_prefix:
        # Match multiprocessing's Windows venv bootstrap. Bypass only the
        # redirector process, preserving this venv's executable and packages.
        # Otherwise terminate/wait could reap the launcher but leave WinRT alive.
        executable = sys._base_executable
        env["__PYVENV_LAUNCHER__"] = sys.executable
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    return [executable, "-B", "-m", "ovb_rc003", FLAG], env


def _reap(owner: _Owner) -> bool:
    from .windows_diagnostics import _terminate_and_confirm_exit

    process = owner.process
    if process is None:
        return True
    try:
        exited = process.poll() is not None
    except OSError:
        exited = False
    if not exited:
        exited = _terminate_and_confirm_exit(
            process, terminate_wait=_TERMINATE_WAIT_SECONDS,
            kill_wait=_KILL_WAIT_SECONDS)
    if exited:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    exited = False
    return exited


def _read_diagnostics(process: subprocess.Popen) -> bool:
    """Replay only the existing fixed probe messages, never native text/IDs."""
    try:
        raw = process.stdout.read(_MAX_DIAGNOSTIC_BYTES + 1)
        if len(raw) > _MAX_DIAGNOSTIC_BYTES:
            return False
        records = json.loads(raw)
        if not isinstance(records, list) or len(records) > len(_PROBE_LOG_MESSAGES):
            return False
        for row in records:
            if (not isinstance(row, list) or len(row) != 2 or row[0] not in _PROBE_LOG_MESSAGES
                    or not isinstance(row[1], str) or len(row[1]) > 80
                    or not row[1].isascii() or not row[1].isidentifier()):
                return False
        logger = logging.getLogger("ovb_rc003.ble_transport_winrt")
        for stage, error_type in records:
            logger.info(_PROBE_LOG_MESSAGES[stage], error_type)
        return True
    except (OSError, ValueError, TypeError):
        return False


async def probe(device_id: str, *, timeout: float) -> bool | None:
    """Return after exit; None means timeout or another active owner of this ID."""
    from .windows_diagnostics import _popen_kwargs

    if not isinstance(device_id, str) or not device_id:
        return False
    request = (json.dumps(device_id) + "\n").encode("utf-8")
    if len(request) > _MAX_REQUEST_BYTES:
        return False
    with _owners_lock:
        previous = _owners.get(device_id)
        if previous is not None:
            if previous.active:
                return None
            if not _reap(previous):
                raise ProbeCleanupError("previous ATVV probe exit remains unconfirmed")
        owner = _Owner()
        _owners[device_id] = owner
    cancelled = False
    try:
        command, env = _command()
        kwargs = _popen_kwargs()
        # One bounded request fits in the fresh pipe; there are no subsequent
        # writes. Keeping it open also lets the child observe abrupt parent exit.
        kwargs.update(stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, bufsize=0)
        owner.process = subprocess.Popen(command, **kwargs)
        process = owner.process
        if process.stdin.write(request) != len(request):
            return False
        deadline = time.monotonic() + max(0.001, float(timeout))
        while process.poll() is None:
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_POLL_SECONDS)
        if process.returncode not in (0, 1) or not _read_diagnostics(process):
            return False
        return process.returncode == 0
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        # Synchronous bounded cleanup cannot be interrupted by another task
        # cancellation. The GUI owns a different thread from this bridge loop.
        exited = _reap(owner)
        with _owners_lock:
            owner.active = False
            if exited and _owners.get(device_id) is owner:
                _owners.pop(device_id)
        if not exited:
            if cancelled:
                # Keep both the resource owner and the caller's stop intent.
                # Replacing CancelledError would make the supervisor retry
                # instead of finishing the user's requested service shutdown.
                logging.getLogger("ovb_rc003.ble_transport_winrt").error(
                    "ATVV candidate probe cleanup unconfirmed: error_type=ProbeCleanupError")
            else:
                raise ProbeCleanupError("ATVV probe exit could not be confirmed")


def _pipe_fd(name: str, standard_id: int, access: int) -> int:
    stream = getattr(sys, name, None)
    if stream is not None:
        return stream.fileno()
    # A windowed PyInstaller executable has no sys.stdin, even though Popen
    # supplied an inherited pipe. Own the same native standard input handle.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel.GetStdHandle(standard_id)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError("candidate input pipe unavailable")
    return msvcrt.open_osfhandle(handle, os.O_BINARY | access)


def child_main(args: list[str]) -> int:
    if args or sys.platform != "win32":
        return 2
    try:
        incoming = _pipe_fd("stdin", -10, os.O_RDONLY)
        request = bytearray()
        while not request.endswith(b"\n"):
            chunk = os.read(incoming, 1)
            if not chunk or len(request) >= _MAX_REQUEST_BYTES:
                return 2
            request.extend(chunk)
        payload = json.loads(request)
        if not isinstance(payload, str) or not payload:
            return 2

        def watch_parent():
            try:
                os.read(incoming, 1)
            finally:
                # This entire disposable process owns only a service query.
                # EOF must end it even if the native operation ignores cancel.
                os._exit(2)

        threading.Thread(target=watch_parent, name="rc003-probe-parent", daemon=True).start()
        from types import SimpleNamespace
        from . import ble_transport_winrt, identity
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                for stage, message in _PROBE_LOG_MESSAGES.items():
                    if record.msg == message and not any(row[0] == stage for row in records):
                        error_type = record.args[0]
                        if (isinstance(error_type, str) and len(error_type) <= 80
                                and error_type.isascii() and error_type.isidentifier()):
                            records.append([stage, error_type])

        logger = logging.getLogger("ovb_rc003.ble_transport_winrt")
        logger.setLevel(logging.INFO)
        logger.addHandler(Capture())
        candidate = identity.RC003Candidate(
            "MI RC", False, SimpleNamespace(id=payload))
        try:
            reachable = asyncio.run(ble_transport_winrt._candidate_has_voice_service(candidate))
        except Exception as exc:
            logger.info(_PROBE_LOG_MESSAGES["worker"], type(exc).__name__)
            reachable = False
        outgoing = _pipe_fd("stdout", -11, os.O_WRONLY)
        payload = json.dumps(records).encode("utf-8")
        if os.write(outgoing, payload) != len(payload):
            return 2
        return 0 if reachable else 1
    except Exception:
        return 2
