"""RC003 HID-over-GATT compatibility tap.

Windows' normal keyboard stack does not expose the RC003 usages for Back and
the two volume buttons.  The original ``remote-bridge-hub`` Windows client
solves that by observing the completed HID read inside the RC003 WUDF host via
a verified Frida Gadget.  This module reuses that narrow transport and keeps
button policy in the existing Remote Mic application.

The tap is deliberately optional.  Without the explicitly fetched, SHA256
verified Gadget archive the normal BLE/Raw Input client still starts, while
these three missing usages remain unavailable instead of being guessed.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Iterable

from . import frida_hid_tap_runtime
from .device_profile import BUTTON_USAGE_IDS


@dataclass(frozen=True)
class ThirdPartyAsset:
    name: str
    version: str
    url: str
    sha256: str
    license_name: str
    license_url: str


FRIDA_GADGET = ThirdPartyAsset(
    name="Frida Gadget",
    version=frida_hid_tap_runtime.GADGET_VERSION,
    url=(
        "https://github.com/frida/frida/releases/download/17.15.3/"
        "frida-gadget-17.15.3-windows-x86_64.dll.xz"
    ),
    sha256=frida_hid_tap_runtime.GADGET_ARCHIVE_SHA256,
    license_name="Frida core license",
    license_url="https://raw.githubusercontent.com/frida/frida-core/main/COPYING",
)

BACK_USAGE = 0x00F1
VOLUME_UP_USAGE = 0x0080
VOLUME_DOWN_USAGE = 0x0081
MISSING_USAGE_TO_BUTTON = {
    BACK_USAGE: "back",
    VOLUME_UP_USAGE: "volume_up",
    VOLUME_DOWN_USAGE: "volume_down",
}

# The tap observes the full 6-byte keyboard report (three little-endian 16-bit
# usages), not just the three usages Windows' keyboard class drops.  Reporting
# every known RC003 keyboard usage lets the application arm its duplicate
# suppressor from the tap's socket thread - a side channel the low-level hook
# does not block, unlike the WM_INPUT arm that arrives too late (measured
# ~63-72ms after the hook on the RC003).
TAP_USAGE_TO_BUTTON = dict(MISSING_USAGE_TO_BUTTON)
for _usage, _button in BUTTON_USAGE_IDS.items():
    TAP_USAGE_TO_BUTTON.setdefault(_usage, _button)

# usage -> (VK, make code, extended) matching what Windows' keyboard class
# reports for the same physical key, so the hook's consume() sees identical
# vk/scan/extended values whether the arm came from Raw Input or from the tap.
TAP_USAGE_TO_KEY = {
    0x0028: (0x0D, 0x1C, False),  # ok / Enter
    0x0035: (0xC0, 0x29, False),  # tv / grave accent
    0x003E: (0x74, 0x3F, False),  # mic / F5 (voice path, never armed)
    0x004A: (0x24, 0x47, True),  # home
    0x004F: (0x27, 0x4D, True),  # right
    0x0050: (0x25, 0x4B, True),  # left
    0x0051: (0x28, 0x50, True),  # down
    0x0052: (0x26, 0x48, True),  # up
    0x0065: (0x5D, 0x5D, True),  # menu / App key
    0x0066: (0xFF, 0x5E, True),  # power (untranslated VK)
    0x007F: (0xAD, 0x20, True),  # volume_mute
    0x0080: (0xAF, 0x30, True),  # volume_up
    0x0081: (0xAE, 0x2E, True),  # volume_down
    0x00F1: (0xFF, 0x6A, True),  # back (untranslated VK)
}

HID_TAP_INJECTOR_FLAG = "--rc003-hid-injector"
HID_TAP_INJECTOR_TIMEOUT_SECONDS = 30.0
HID_TAP_MAX_BUFFER_BYTES = 64 * 1024
_ERROR_INSUFFICIENT_BUFFER = 122
_TCP_TABLE_OWNER_PID_ALL = 5
HID_TAP_INJECTOR_EXIT_DETAILS = {
    3: "injector_requires_administrator",
    4: "injector_validation_failed",
    5: "injector_unexpected_failure",
}


class HidTapInjectionError(RuntimeError):
    pass


class HidTapState(str, Enum):
    DISABLED = "disabled_non_windows"
    UNAVAILABLE = "unavailable_gadget_not_verified"
    VERIFIED_NOT_STARTED = "verified_not_started"
    STARTING = "starting"
    WAITING_HOST = "waiting_for_rc003_host"
    INJECTING = "injecting"
    WAITING_CONNECTION = "waiting_for_gadget_connection"
    ATTACHED_WAITING_IO = "attached_waiting_for_hid_io"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class _TcpOwnerRow:
    local_port: int
    remote_port: int
    owning_pid: int


class _MibTcpRowOwnerPid(ctypes.Structure):
    _fields_ = (
        ("state", wintypes.DWORD),
        ("local_address", wintypes.DWORD),
        ("local_port", wintypes.DWORD),
        ("remote_address", wintypes.DWORD),
        ("remote_port", wintypes.DWORD),
        ("owning_pid", wintypes.DWORD),
    )


def _tcp_owner_rows() -> tuple[_TcpOwnerRow, ...]:
    """Read IPv4 TCP endpoint ownership without exposing endpoint values."""

    if os.name != "nt":
        raise OSError("TCP owner lookup is only available on Windows")
    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    iphlpapi.GetExtendedTcpTable.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    )
    iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD

    size = wintypes.ULONG(0)
    result = int(
        iphlpapi.GetExtendedTcpTable(
            None,
            ctypes.byref(size),
            False,
            socket.AF_INET,
            _TCP_TABLE_OWNER_PID_ALL,
            0,
        )
    )
    if result not in {0, _ERROR_INSUFFICIENT_BUFFER} or size.value < 4:
        raise OSError("GetExtendedTcpTable size query failed")

    buffer = ctypes.create_string_buffer(size.value)
    result = int(
        iphlpapi.GetExtendedTcpTable(
            buffer,
            ctypes.byref(size),
            False,
            socket.AF_INET,
            _TCP_TABLE_OWNER_PID_ALL,
            0,
        )
    )
    if result != 0:
        raise OSError("GetExtendedTcpTable failed")

    count = int(ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value)
    row_size = ctypes.sizeof(_MibTcpRowOwnerPid)
    required_size = ctypes.sizeof(wintypes.DWORD) + count * row_size
    if required_size > size.value:
        raise OSError("GetExtendedTcpTable returned a truncated table")

    rows = []
    offset = ctypes.sizeof(wintypes.DWORD)
    for index in range(count):
        row = _MibTcpRowOwnerPid.from_buffer_copy(
            buffer,
            offset + index * row_size,
        )
        rows.append(
            _TcpOwnerRow(
                local_port=socket.ntohs(int(row.local_port) & 0xFFFF),
                remote_port=socket.ntohs(int(row.remote_port) & 0xFFFF),
                owning_pid=int(row.owning_pid),
            )
        )
    return tuple(rows)


def tcp_client_process_id(
    client: socket.socket,
    *,
    _rows: Callable[[], Iterable[_TcpOwnerRow]] = _tcp_owner_rows,
) -> int | None:
    """Resolve the process owning the accepted side's peer TCP endpoint."""

    peer = client.getpeername()
    local = client.getsockname()
    if (
        not isinstance(peer, tuple)
        or not isinstance(local, tuple)
        or len(peer) < 2
        or len(local) < 2
        or peer[0] != "127.0.0.1"
        or local[0] != "127.0.0.1"
    ):
        return None
    peer_port = int(peer[1])
    local_port = int(local[1])
    owners = {
        row.owning_pid
        for row in _rows()
        if row.local_port == peer_port
        and row.remote_port == local_port
        and row.owning_pid > 0
    }
    if len(owners) != 1:
        return None
    return owners.pop()


def build_injector_command(
    pid: int,
    *,
    frozen: bool | None = None,
    executable: str | None = None,
) -> list[str]:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("injector PID must be a positive integer")
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if executable is None:
        executable = sys.executable
    if not executable:
        raise HidTapInjectionError("injector executable is unavailable")
    suffix = [HID_TAP_INJECTOR_FLAG, "--pid", str(pid)]
    if frozen:
        return [executable, *suffix]
    return [executable, "-m", "ovb_rc003", *suffix]


def run_injector_subprocess(
    pid: int,
    *,
    timeout: float = HID_TAP_INJECTOR_TIMEOUT_SECONDS,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    """Run the narrow injector out of process and accept only exit code 0."""

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "check": False,
        "timeout": timeout,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = _run(build_injector_command(pid), **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise HidTapInjectionError("injector_timeout") from exc
    except OSError as exc:
        raise HidTapInjectionError("injector_launch_failed") from exc
    if completed.returncode != 0:
        return_code = int(completed.returncode)
        detail = HID_TAP_INJECTOR_EXIT_DETAILS.get(
            return_code, f"injector_exit_code_{return_code}"
        )
        raise HidTapInjectionError(detail)


def verify_asset(path: Path, asset: ThirdPartyAsset = FRIDA_GADGET) -> bool:
    """Return true only when ``path`` is the exact pinned archive."""

    if not path.is_file():
        return False
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest.casefold() == asset.sha256.casefold()


def gadget_archive_path() -> Path:
    return frida_hid_tap_runtime.gadget_archive_path()


def decode_rc003_ioctl_output(data: bytes) -> bytes | None:
    """Extract the six-byte usage payload from a HidOverGatt read buffer."""

    if len(data) != 9 or data[:3] != b"\x01\x00\x00":
        return None
    return data[3:9]


def payload_usages(payload: bytes) -> set[int]:
    if len(payload) != 6:
        return set()
    return {
        int.from_bytes(payload[index : index + 2], "little")
        for index in range(0, len(payload), 2)
    } - {0}


class RC003HidReportTap:
    """Observe missing RC003 usages and emit edge-stable six-byte reports."""

    def __init__(
        self,
        report_handler: Callable[[int, bytes], None],
        *,
        archive_path: Path | None = None,
        enabled: bool = True,
        retry_delay: float = 2.0,
        heartbeat_timeout: float = 15.0,
        status_handler: Callable[[str, str], None] | None = None,
        injector: Callable[[int], None] = run_injector_subprocess,
        client_pid_resolver: Callable[[socket.socket], int | None] = tcp_client_process_id,
    ) -> None:
        self.report_handler = report_handler
        self.archive_path = archive_path or gadget_archive_path()
        self.enabled = bool(enabled) and os.name == "nt"
        self.retry_delay = max(0.5, float(retry_delay))
        self.heartbeat_timeout = max(10.0, float(heartbeat_timeout))
        self.status_handler = status_handler or (lambda _status, _detail: None)
        self.injector = injector
        self.client_pid_resolver = client_pid_resolver
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active_usages: set[int] = set()
        self._state_lock = threading.Lock()
        self._status = self._initial_status()
        self._status_detail = ""

    def _initial_status(self) -> HidTapState:
        if not self.enabled:
            return HidTapState.DISABLED
        if not self.dependency_available:
            return HidTapState.UNAVAILABLE
        return HidTapState.VERIFIED_NOT_STARTED

    def _set_status(self, state: HidTapState, detail: str = "") -> None:
        with self._state_lock:
            if state == self._status and detail == self._status_detail:
                return
            self._status = state
            self._status_detail = detail
        try:
            self.status_handler(state.value, detail)
        except Exception:
            pass

    @property
    def dependency_available(self) -> bool:
        return verify_asset(self.archive_path)

    @property
    def available(self) -> bool:
        return self.dependency_available

    @property
    def status(self) -> str:
        with self._state_lock:
            return self._status.value

    @property
    def status_detail(self) -> str:
        with self._state_lock:
            return self._status_detail

    def _release_active(self) -> None:
        with self._state_lock:
            was_active = bool(self.active_usages)
            self.active_usages.clear()
        if was_active:
            self.report_handler(1, b"\x00" * 6)

    def _handle_ioctl_output(self, data: bytes) -> None:
        payload = decode_rc003_ioctl_output(data)
        if payload is None:
            return
        active = payload_usages(payload) & set(TAP_USAGE_TO_BUTTON)
        with self._state_lock:
            previous = self.active_usages
            if active == previous:
                return
            self.active_usages = set(active)
        filtered = b"".join(
            value.to_bytes(2, "little") for value in sorted(active)
        )
        self.report_handler(1, (filtered + b"\x00" * 6)[:6])

    def _run_guarded(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - thread must report, never disappear
            self._set_status(
                HidTapState.FAILED,
                f"tap_thread_exception_{type(exc).__name__}",
            )
        finally:
            if self.stop_event.is_set():
                self._set_status(HidTapState.STOPPED)

    def _run(self) -> None:
        injection_attempted_pid: int | None = None
        injection_failed_pid: int | None = None
        while not self.stop_event.is_set():
            pid = frida_hid_tap_runtime.find_rc003_hidogatt_host_pid()
            if pid is None:
                injection_attempted_pid = None
                injection_failed_pid = None
                self._set_status(HidTapState.WAITING_HOST)
                self.stop_event.wait(self.retry_delay)
                continue
            if pid != injection_attempted_pid and pid != injection_failed_pid:
                injection_attempted_pid = None
                injection_failed_pid = None
            if pid == injection_failed_pid:
                # Retrying an identical injection into the same system process
                # adds risk and alternates FAILED/INJECTING in the log forever.
                # A new WUDFHost PID is the safe retry boundary.
                self.stop_event.wait(self.retry_delay)
                continue

            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                server.bind(("127.0.0.1", frida_hid_tap_runtime.HID_TAP_PORT))
                server.listen(1)
                server.settimeout(1.0)
                if injection_attempted_pid is None:
                    self._set_status(HidTapState.INJECTING)
                    try:
                        self.injector(pid)
                        injection_attempted_pid = pid
                    except Exception as exc:  # noqa: BLE001 - retry with sanitized state
                        injection_failed_pid = pid
                        self._set_status(
                            HidTapState.FAILED,
                            str(exc)
                            if isinstance(exc, HidTapInjectionError)
                            else f"injector_exception_{type(exc).__name__}",
                        )
                        self.stop_event.wait(self.retry_delay)
                        continue
                self._set_status(HidTapState.WAITING_CONNECTION)
                try:
                    client, _address = server.accept()
                except socket.timeout:
                    continue
                try:
                    client_pid = self.client_pid_resolver(client)
                except Exception:  # noqa: BLE001 - fail closed on identity lookup
                    client_pid = None
                    identity_detail = "gadget_client_identity_unavailable"
                else:
                    identity_detail = "gadget_client_identity_mismatch"
                if client_pid != pid:
                    self._set_status(HidTapState.UNHEALTHY, identity_detail)
                    try:
                        client.close()
                    except OSError:
                        pass
                    continue
                client.settimeout(1.0)
                try:
                    self._set_status(HidTapState.ATTACHED_WAITING_IO)
                    buffer = b""
                    last_heartbeat = time.monotonic()
                    io_verified = False
                    while not self.stop_event.is_set():
                        if frida_hid_tap_runtime.find_rc003_hidogatt_host_pid() != pid:
                            self._set_status(HidTapState.WAITING_HOST, "host_changed")
                            injection_attempted_pid = None
                            break
                        try:
                            chunk = client.recv(65536)
                        except socket.timeout:
                            chunk = None
                        if chunk == b"":
                            if not self.stop_event.is_set():
                                self._set_status(
                                    HidTapState.UNHEALTHY,
                                    "gadget_connection_closed",
                                )
                            break
                        if chunk:
                            if len(buffer) + len(chunk) > HID_TAP_MAX_BUFFER_BYTES:
                                self._set_status(
                                    HidTapState.UNHEALTHY,
                                    "gadget_message_too_large",
                                )
                                break
                            buffer += chunk
                            fatal_message = False
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                try:
                                    message = json.loads(line.decode("utf-8"))
                                except (UnicodeDecodeError, json.JSONDecodeError):
                                    continue
                                if not isinstance(message, dict):
                                    continue
                                kind = message.get("kind")
                                if kind == "ready":
                                    last_heartbeat = time.monotonic()
                                    if message.get("hook_installed") is not True:
                                        self._set_status(
                                            HidTapState.FAILED,
                                            "gadget_hook_not_installed",
                                        )
                                        fatal_message = True
                                        break
                                elif kind == "heartbeat":
                                    last_heartbeat = time.monotonic()
                                elif kind == "gatt_read":
                                    raw = message.get("raw", "")
                                    try:
                                        data = bytes.fromhex(raw)
                                    except (TypeError, ValueError):
                                        data = b""
                                    if decode_rc003_ioctl_output(data) is not None:
                                        io_verified = True
                                        self._set_status(HidTapState.READY, "hid_io_verified")
                                        self._handle_ioctl_output(data)
                                elif kind == "error":
                                    self._set_status(HidTapState.FAILED, "gadget_hook_error")
                                    fatal_message = True
                                    break
                            if fatal_message:
                                break
                        now = time.monotonic()
                        if now - last_heartbeat >= self.heartbeat_timeout:
                            self._set_status(
                                HidTapState.UNHEALTHY,
                                "gadget_heartbeat_stale",
                            )
                            break
                        if io_verified:
                            self._set_status(HidTapState.READY, "hid_io_verified")
                finally:
                    try:
                        client.close()
                    except OSError:
                        pass
                    self._release_active()
            finally:
                server.close()
            if not self.stop_event.is_set():
                self.stop_event.wait(0.5)

    def start(self) -> bool:
        if not self.enabled:
            self._set_status(HidTapState.DISABLED)
            return False
        if not self.dependency_available:
            self._set_status(HidTapState.UNAVAILABLE)
            return False
        if self.thread is not None and self.thread.is_alive():
            return True
        self.stop_event.clear()
        self._set_status(HidTapState.STARTING)
        self.thread = threading.Thread(
            target=self._run_guarded,
            name="rc003-hidogatt-report-tap",
            daemon=True,
        )
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=3.0)
            if self.thread.is_alive():
                raise RuntimeError("RC003 HID report tap did not stop")
        self._release_active()
        self.thread = None
        self._set_status(HidTapState.STOPPED)


class BackKeyCompatLayer(RC003HidReportTap):
    """Compatibility name retained for callers of the earlier back-only shim."""

    def __init__(
        self,
        gadget_path: Path | None = None,
        asset: ThirdPartyAsset = FRIDA_GADGET,
        report_handler: Callable[[int, bytes], None] | None = None,
    ) -> None:
        archive_path = gadget_path or gadget_archive_path()
        # Custom test assets can still use the generic descriptor without
        # changing the production pinned archive.
        self._custom_asset = asset
        super().__init__(
            report_handler or (lambda _report_id, _payload: None),
            archive_path=archive_path,
        )

    @property
    def dependency_available(self) -> bool:
        return verify_asset(self.archive_path, self._custom_asset)


def injector_main(argv: list[str] | None = None) -> int:
    from .frida_hid_tap_injector import main

    return main(argv)
