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
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from typing import Callable, Iterable

from . import frida_hid_tap_runtime
from .device_profile import BUTTON_USAGE_IDS
from .diagnostic_trace import DiagnosticTrace


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
# usages), not just the three usages Windows' keyboard class drops. Reporting
# every known RC003 keyboard usage gives one authoritative mapping source after
# the Gadget has copied and cleared that report before Windows translates it.
TAP_USAGE_TO_BUTTON = dict(MISSING_USAGE_TO_BUTTON)
for _usage, _button in BUTTON_USAGE_IDS.items():
    TAP_USAGE_TO_BUTTON.setdefault(_usage, _button)

# Direction usages translated by Windows, expressed as
# (virtual-key, scan code, extended). The embedded navigator uses this exact
# identity to suppress only the leaked RC003 legacy edge while keeping the
# replacement SendInput edge and ordinary keyboards independent.
TAP_DIRECTION_USAGE_TO_KEY = {
    0x004F: (0x27, 0x4D, True),  # right
    0x0050: (0x25, 0x4B, True),  # left
    0x0051: (0x28, 0x50, True),  # down
    0x0052: (0x26, 0x48, True),  # up
}

HID_TAP_INJECTOR_FLAG = "--rc003-hid-injector"
HID_TAP_INJECTOR_TIMEOUT_SECONDS = 30.0
HID_CONSUMER_REGISTRATION_TIMEOUT_SECONDS = 1.0
HID_TAP_CONNECTION_TIMEOUT_SECONDS = 10.0
HID_TAP_MAX_BUFFER_BYTES = 64 * 1024
HID_INTERCEPT_PROTOCOL = frida_hid_tap_runtime.INTERCEPT_PROTOCOL
HID_INTERCEPT_LEASE_SECONDS = 2.0
HID_INTERCEPT_RENEW_INTERVAL_SECONDS = 0.5
HID_INTERCEPT_DISABLE_ACK_TIMEOUT_SECONDS = 0.5
HID_INTERCEPT_LEASE_SAFETY_SECONDS = 0.15
_ERROR_INSUFFICIENT_BUFFER = 122
_TCP_TABLE_OWNER_PID_ALL = 5
HID_TAP_INJECTOR_EXIT_DETAILS = {
    3: "injector_requires_administrator",
    4: "injector_validation_failed",
    5: "injector_unexpected_failure",
    6: "hid_helper_host_restarted",
    7: "hid_helper_legacy_runtime_restart_required",
}
HID_TAP_RETRYABLE_INJECTION_DETAILS = frozenset(
    {"hid_helper_operation_busy"}
)
HID_TAP_BOUNDED_RETRYABLE_INJECTION_DETAILS = frozenset(
    {
        "hid_helper_task_service_unavailable",
        "hid_helper_task_start_failed",
    }
)
HID_TAP_TRANSIENT_INJECTION_MAX_ATTEMPTS = 3
_LOGGER = logging.getLogger("ovb_rc003.hid_tap")
_HOOK_ERROR_CODES = frozenset({
    "copy_export_missing", "close_export_missing", "hook_install_exception",
    "copy_entry_exception", "copy_restore_exception", "copy_delivery_exception",
    "bound_copy_handle_closed",
})
_SOURCE_FAILURE_CODES = frozenset({
    "source_host_unverified", "copy_callsite_unverified", "copy_object_unverified",
    "device_interface_unverified", "device_registry_unavailable", "device_registry_read_failed",
    "device_identity_invalid", "device_identity_hash_failed", "copy_frame_unverified",
})


def _diagnostic_environment() -> dict:
    """Read build fingerprints once on the tap worker, never on a key callback."""
    from . import __version__, hid_elevation_windows

    fields = {
        "app_version": __version__, "python_version": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "pointer_bits": ctypes.sizeof(ctypes.c_void_p) * 8,
        "expected_protocol": HID_INTERCEPT_PROTOCOL,
        "expected_diagnostic_revision": frida_hid_tap_runtime.HID_DIAGNOSTIC_REVISION,
        "expected_source_binding_revision": frida_hid_tap_runtime.SOURCE_BINDING_REVISION,
        "expected_gadget_version": frida_hid_tap_runtime.GADGET_VERSION,
        "expected_script_sha256": hashlib.sha256(frida_hid_tap_runtime.GADGET_SCRIPT.encode()).hexdigest(),
        "fingerprint_scope": "on_disk_not_loaded_modules",
    }
    if os.name != "nt":
        fields["environment_status"] = "non_windows"
        return fields
    version = sys.getwindowsversion()
    fields["windows_version"] = list(version.platform_version)
    try:
        fields["elevated"] = hid_elevation_windows.query_process_elevated()
    except Exception as exc:
        fields["elevation_query_error"] = type(exc).__name__
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    files = {}
    for name, relative in (
        ("WUDFHost.exe", "WUDFHost.exe"), ("WUDFRd.sys", "drivers/WUDFRd.sys"),
        ("Microsoft.Bluetooth.Profiles.HidOverGatt.dll", "drivers/umdf/Microsoft.Bluetooth.Profiles.HidOverGatt.dll"),
    ):
        try:
            files[name] = {"sha256": frida_hid_tap_runtime.sha256_file(system / relative)}
        except OSError as exc:
            files[name] = {"error_type": type(exc).__name__, "winerror": getattr(exc, "winerror", None)}
    fields["system_files"] = files
    return fields


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
    RESTART_REQUIRED = "restart_required"
    SHARED_HOST = "selected_device_shared_host"
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


def _run_direct_injector_subprocess(
    pid: int,
    *,
    timeout: float = HID_TAP_INJECTOR_TIMEOUT_SECONDS,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    frozen: bool | None = None,
    executable: str | None = None,
) -> None:
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
        completed = _run(
            build_injector_command(
                pid,
                frozen=frozen,
                executable=executable,
            ),
            **kwargs,
        )
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


_DEFAULT_SUBPROCESS_RUN = subprocess.run


def run_injector_subprocess(
    pid: int,
    *,
    timeout: float = HID_TAP_INJECTOR_TIMEOUT_SECONDS,
    _run: Callable[..., subprocess.CompletedProcess] = _DEFAULT_SUBPROCESS_RUN,
    frozen: bool | None = None,
    executable: str | None = None,
    _is_elevated: Callable[[], bool] | None = None,
    _registered_injector: Callable[[int], None] | None = None,
) -> None:
    """Start the narrow injector without elevating the desktop application."""

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("injector PID must be a positive integer")
    resolved_frozen = (
        bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    )
    # Existing unit tests inject a fake subprocess runner to verify the
    # direct child contract. Keep that seam deterministic instead of probing
    # the real Task Scheduler from a test process.
    if _run is not _DEFAULT_SUBPROCESS_RUN:
        _run_direct_injector_subprocess(
            pid,
            timeout=timeout,
            _run=_run,
            frozen=frozen,
            executable=executable,
        )
        return

    uses_registered_helper = _registered_injector is None
    if _is_elevated is None or _registered_injector is None:
        from . import hid_elevation_windows

        if _is_elevated is None:
            _is_elevated = hid_elevation_windows.is_process_elevated
        if _registered_injector is None:
            _registered_injector = hid_elevation_windows.run_registered_injector

    if resolved_frozen and not _is_elevated():
        if uses_registered_helper:
            from . import config, hid_helper_consumers

            if not hid_helper_consumers.current_consumer_is_registered(
                config.config_root()
            ):
                try:
                    marker = hid_helper_consumers.register_current_consumer(
                        config.config_root(),
                        timeout_seconds=HID_CONSUMER_REGISTRATION_TIMEOUT_SECONDS,
                    )
                except hid_helper_consumers.ConsumerMaintenanceError as exc:
                    raise HidTapInjectionError(
                        "hid_helper_operation_busy"
                    ) from exc
                except Exception as exc:
                    raise HidTapInjectionError(
                        "helper_consumer_unregistered"
                    ) from exc
                if marker is None or not (
                    hid_helper_consumers.current_consumer_is_registered(
                        config.config_root()
                    )
                ):
                    raise HidTapInjectionError(
                        "helper_consumer_unregistered"
                    )
        try:
            _registered_injector(pid)
        except Exception as exc:  # noqa: BLE001 - expose only the stable detail
            detail = str(exc).strip() or "hid_helper_task_start_failed"
            raise HidTapInjectionError(detail) from exc
        return

    _run_direct_injector_subprocess(
        pid,
        timeout=timeout,
        frozen=frozen,
        executable=executable,
    )


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
        connection_timeout: float = HID_TAP_CONNECTION_TIMEOUT_SECONDS,
        status_handler: Callable[[str, str], None] | None = None,
        injector: Callable[[int], None] = run_injector_subprocess,
        client_pid_resolver: Callable[[socket.socket], int | None] = tcp_client_process_id,
        diagnostic_trace: DiagnosticTrace | None = None,
        selected_key: str | None = None,
    ) -> None:
        self.report_handler = report_handler
        self._selected_key = selected_key
        self._diagnostic_trace = diagnostic_trace
        self._diagnostic_tap_id = uuid.uuid4().hex
        self._diagnostic_report_seq = 0
        self._diagnostic_host_pid = None
        self._diagnostic_connection_seq = 0
        self._last_health_time = 0.0
        self._last_health_state = None
        self._last_copy_health: dict = {}
        self._last_host_lookup = None
        self.native_copy_interception = False
        try:
            self._copy_probe_seconds = min(
                300, max(0, int(os.environ.get("REMOTE_MIC_RC003_COPY_PROBE_SECONDS", "0")))
            )
        except ValueError:
            self._copy_probe_seconds = 0
        self.archive_path = archive_path or gadget_archive_path()
        self.enabled = bool(enabled) and os.name == "nt"
        self.retry_delay = max(0.5, float(retry_delay))
        self.heartbeat_timeout = max(10.0, float(heartbeat_timeout))
        self.connection_timeout = max(1.0, float(connection_timeout))
        self.status_handler = status_handler or (lambda _status, _detail: None)
        self.injector = injector
        self.client_pid_resolver = client_pid_resolver
        self.stop_event = threading.Event()
        self._stop_requested_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active_usages: set[int] = set()
        self._state_lock = threading.Lock()
        self._client_lock = threading.Lock()
        self._control_send_lock = threading.Lock()
        self._client: socket.socket | None = None
        self._interception_enabled = False
        self._lease_deadline = 0.0
        self._disable_ack_event = threading.Event()
        self._status = self._initial_status()
        self._status_detail = ""

    def _record_diagnostic(self, event: str, **fields: object) -> None:
        # Callers supply only fixed reason codes, counters and whitelisted metadata.
        payload = {"tap_id": self._diagnostic_tap_id, "app_pid": os.getpid(),
                   "host_pid": self._diagnostic_host_pid,
                   "connection_seq": self._diagnostic_connection_seq, **fields}
        try:
            _LOGGER.info("HID diagnostic: %s", json.dumps({"event": event, **payload}, ensure_ascii=True))
        except Exception:
            pass
        try:
            if self._diagnostic_trace is not None:
                self._diagnostic_trace.emit(event, **payload)
        except Exception:
            pass

    @staticmethod
    def _hook_error_code(value: object) -> str:
        return value if isinstance(value, str) and value in _HOOK_ERROR_CODES else "hook_error_unresolved"

    def _record_hook_error(self, message: dict) -> None:
        code = self._hook_error_code(message.get("code"))
        if code == "hook_error_unresolved":
            code = {
                "NtDeviceIoControlFile export not found": "copy_export_missing",
                "NtClose export not found": "close_export_missing",
                "bound_copy_handle_closed": "bound_copy_handle_closed",
            }.get(message.get("message") if isinstance(message.get("message"), str) else "", code)
        status = message.get("ntstatus")
        self._record_diagnostic("hid_hook_failure", reason=code,
                                ntstatus=status if type(status) is int and 0 <= status <= 0xffffffff else None,
                                root_cause="unresolved" if code == "hook_error_unresolved" else "failure_stage_identified")

    @staticmethod
    def _source_evidence_fields(message: dict) -> dict:
        if (type(message.get("source_binding_revision")) is not int
                or message["source_binding_revision"] != frida_hid_tap_runtime.SOURCE_BINDING_REVISION):
            return {}
        fields: dict = {}
        reason = message.get("reason")
        fields["reason"] = reason if isinstance(reason, str) and reason in _SOURCE_FAILURE_CODES else "device_source_unverified"
        for name in ("winerror", "value_type", "value_bytes", "key_name_status", "key_depth",
                     "instance_open_status", "instance_value_status", "caller_rva"):
            value = message.get(name)
            if type(value) is int and 0 <= value <= 0xffffffff:
                fields[name] = value
        for name in ("query_incomplete", "instance_identity_valid", "instance_matches_selected"):
            if type(message.get(name)) is bool:
                fields[name] = message[name]
        if message.get("key_scope") in ("other", "device_instance", "device_parameters", "enum_descendant"):
            fields["key_scope"] = message["key_scope"]
        if message.get("key_bus") in ("bthledevice", "bthenum", "bthle", "hid", "other"):
            fields["key_bus"] = message["key_bus"]
        if message.get("caller_module") in ("wudfhost.exe", "kernel32.dll", "kernelbase.dll", "ntdll.dll", "apphelp.dll", "other"):
            fields["caller_module"] = message["caller_module"]
        value = message.get("instance_ref")
        if isinstance(value, str) and len(value) == 12 and all(c in "0123456789abcdef" for c in value):
            fields["instance_ref"] = value
        return fields

    def _record_source_evidence(self, message: dict) -> None:
        fields = self._source_evidence_fields(message)
        if fields:
            self._record_diagnostic("hid_source_evidence", verified=False, **fields)

    def _record_ready_diagnostics(self, message: dict) -> None:
        self._last_health_time = 0.0
        self._last_health_state = None
        self._last_copy_health = {}
        revision = message.get("diagnostic_revision")
        source_revision = message.get("source_binding_revision")
        protocol = message.get("protocol")
        entry = message.get("source_entry")
        if (not isinstance(entry, dict) or entry.get("module") not in ("kernel32.dll", "kernelbase.dll", "apphelp.dll", "other")
                or type(entry.get("rva")) is not int or not 0 <= entry["rva"] <= 0xffffffff):
            entry = None
        else:
            entry = {"module": entry["module"], "rva": entry["rva"]}
        build_id = message.get("script_build_id")
        entry_status = message.get("source_entry_status")
        if entry_status not in ("not_checked", "source_host_missing", "invalid_import_table",
                                 "import_missing", "import_ambiguous", "target_unverified", "verified"):
            entry_status = None
        if not isinstance(build_id, str) or len(build_id) != 64 or any(c not in "0123456789abcdef" for c in build_id):
            build_id = None
        self._record_diagnostic(
            "hid_runtime_capabilities", loaded_protocol=protocol if type(protocol) is int else None,
            loaded_diagnostic_revision=revision if type(revision) is int else None,
            loaded_source_binding_revision=source_revision if type(source_revision) is int else None,
            expected_protocol=HID_INTERCEPT_PROTOCOL,
            diagnostics_available=revision == frida_hid_tap_runtime.HID_DIAGNOSTIC_REVISION and type(revision) is int,
            hook_installed=message.get("hook_installed") is True,
            loaded_script_build_id=build_id,
            expected_script_build_id=frida_hid_tap_runtime.GADGET_SCRIPT_BUILD_ID,
            script_matches_expected=build_id == frida_hid_tap_runtime.GADGET_SCRIPT_BUILD_ID if build_id else None,
            source_entry=entry,
            source_entry_status=entry_status,
        )
        if message.get("hook_installed") is not True:
            self._record_hook_error({"code": message.get("hook_error_code")})

    def _record_copy_health(self, message: dict) -> None:
        health = message.get("copy_health")
        if not isinstance(health, dict):
            return
        fields = {}
        for key in ("ioctl_calls", "layout_matches", "candidate_reports", "intercepted_reports", "copy_failures", "last_ntstatus"):
            value = health.get(key)
            if type(value) is int and 0 <= value <= 2**53 - 1:
                fields[key] = value
        for key in ("source_bound", "waiting_neutral"):
            if type(health.get(key)) is bool:
                fields[key] = health[key]
        self._last_copy_health = fields
        if fields.get("copy_failures", 0):
            assessment = "copy_failed_observed"
        elif fields.get("waiting_neutral"):
            assessment = "waiting_for_existing_hold_release"
        elif fields.get("intercepted_reports", 0):
            assessment = "reports_intercepted_not_target_effect_confirmed"
        elif fields.get("source_bound"):
            assessment = "bound_waiting_for_next_report"
        elif fields.get("candidate_reports", 0):
            assessment = "waiting_for_source_binding"
        elif fields.get("layout_matches", 0):
            assessment = "no_rc003_report_observed_cause_unresolved"
        elif fields.get("ioctl_calls", 0):
            assessment = "copy_layout_not_seen_cause_unresolved"
        else:
            assessment = "no_intercepted_report_yet_cause_unresolved"
        state = (assessment, fields.get("source_bound"), fields.get("copy_failures"), fields.get("last_ntstatus"))
        now = time.monotonic()
        if state != self._last_health_state or now - self._last_health_time >= 30.0:
            self._last_health_state, self._last_health_time = state, now
            self._record_diagnostic("hid_copy_health", assessment=assessment, **fields)

    def _record_copy_failure(self, message: dict) -> None:
        status = message.get("ntstatus")
        self._record_diagnostic(
            "hid_copy_failure", reason="copy_call_failed",
            ntstatus=status if type(status) is int and 0 <= status <= 0xffffffff else None,
            restored=message.get("restored") if type(message.get("restored")) is bool else None,
            report_owned=message.get("report_owned") if type(message.get("report_owned")) is bool else None,
            target_effect="unresolved",
        )

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
        self._record_diagnostic("hid_transport_status", status=state.value, detail=detail,
                                last_copy_health=dict(self._last_copy_health))
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
            previous = self.active_usages.copy()
            was_active = bool(self.active_usages)
            self.active_usages.clear()
        if was_active:
            self._deliver_report(
                b"\x00" * 6,
                origin="synthetic_release",
                previous=previous,
                active=set(),
                observed_ns=time.perf_counter_ns(),
            )

    def _deliver_report(
        self,
        report: bytes,
        *,
        origin: str,
        previous: set[int],
        active: set[int],
        observed_ns: int,
        raw: bytes = b"",
        force: bool = False,
        forwarded: bool = True,
        copy_probe_id: int = 0,
    ) -> None:
        trace = self._diagnostic_trace
        if trace is None or not trace.enabled:
            if forwarded:
                self.report_handler(1, report)
            return
        with self._state_lock:
            self._diagnostic_report_seq += 1
            sequence = self._diagnostic_report_seq
        report_id = f"{self._diagnostic_tap_id}:{sequence}"
        with trace.hid_report_context(report_id):
            trace.emit(
                "hid_tap_report",
                tap_id=self._diagnostic_tap_id,
                tap_report_seq=sequence,
                copy_probe_id=copy_probe_id,
                origin=origin,
                observed_perf_counter_ns=observed_ns,
                raw_hex=raw.hex(),
                forwarded_hex=report.hex() if forwarded else "",
                previous_usages=sorted(previous),
                active_usages=sorted(active),
                forced=force,
                forwarded=forwarded,
                decision="forward" if forwarded else "unchanged",
                tap_state=self.status,
                tap_detail=self.status_detail,
            )
            if forwarded:
                self.report_handler(1, report)

    def _handle_ioctl_output(
        self, data: bytes, *, force: bool = False, copy_probe_id: int = 0
    ) -> None:
        observed_ns = time.perf_counter_ns()
        payload = decode_rc003_ioctl_output(data)
        if payload is None:
            return
        active = payload_usages(payload) & set(TAP_USAGE_TO_BUTTON)
        with self._state_lock:
            previous = self.active_usages
            forwarded = active != previous or force
            if forwarded:
                self.active_usages = set(active)
        if not forwarded and (
            self._diagnostic_trace is None or not self._diagnostic_trace.enabled
        ):
            return
        filtered = b"".join(
            value.to_bytes(2, "little") for value in sorted(active)
        )
        self._deliver_report(
            (filtered + b"\x00" * 6)[:6],
            origin="gadget",
            previous=previous,
            active=active,
            observed_ns=observed_ns,
            raw=data,
            force=force,
            forwarded=forwarded,
            copy_probe_id=copy_probe_id,
        )

    def _record_copy_probe(self, message: dict) -> None:
        trace = self._diagnostic_trace
        if trace is None or not trace.enabled or not self._copy_probe_seconds:
            return
        fields = {}
        for key in (
            "host_pid", "wall_ms", "deadline_ms", "call_count", "call_id",
            "native_thread_id", "request_id", "operation", "selector",
            "entry_ms", "return_ms", "status",
        ):
            value = message.get(key)
            if type(value) is int and 0 <= value <= 2**63 - 1:
                fields[key] = value
        if isinstance(message.get("phase"), str) and message["phase"] in {"ready", "copy", "stopped"}:
            fields["phase"] = message["phase"]
        handle = message.get("handle")
        if isinstance(handle, str) and len(handle) <= 18 and handle.startswith("0x"):
            try:
                fields["handle"] = hex(int(handle, 16))
            except ValueError:
                pass
        for key in ("entry_hex", "return_hex"):
            value = message.get(key)
            if not isinstance(value, str) or len(value) != 18:
                continue
            try:
                raw = bytes.fromhex(value)
            except ValueError:
                continue
            payload = decode_rc003_ioctl_output(raw)
            if payload is not None and payload_usages(payload) <= set(TAP_USAGE_TO_BUTTON):
                fields[key] = raw.hex()
        stack = message.get("stack")
        if isinstance(stack, list):
            frames = []
            for frame in stack[:12]:
                if not isinstance(frame, dict):
                    continue
                module, rva = frame.get("module"), frame.get("rva")
                if (
                    isinstance(module, str) and len(module) <= 80
                    and "/" not in module and "\\" not in module
                    and module.lower().startswith(("wudf", "ntdll", "kernelbase", "kernel32", "microsoft.bluetooth."))
                    and isinstance(rva, str) and rva.startswith("0x") and len(rva) <= 18
                ):
                    try:
                        frames.append({"module": module, "rva": hex(int(rva, 16))})
                    except ValueError:
                        pass
            fields["stack"] = frames
        trace.emit("hid_copy_probe", tap_id=self._diagnostic_tap_id, **fields)

    def _set_client(self, client: socket.socket | None) -> None:
        with self._client_lock:
            self._client = client
            if client is None:
                self._interception_enabled = False
                self._lease_deadline = 0.0

    def _send_control(self, client: socket.socket, action: str) -> str:
        with self._control_send_lock:
            if (
                action in {"enable", "renew"}
                and self._stop_requested_event.is_set()
            ):
                action = "disable"
            payload = {
                "kind": "intercept_control",
                "action": action,
                "protocol": HID_INTERCEPT_PROTOCOL,
            }
            if action in {"enable", "renew"}:
                payload["lease_ms"] = int(HID_INTERCEPT_LEASE_SECONDS * 1000)
                payload["source_diagnostics"] = bool(self._diagnostic_trace is not None and self._diagnostic_trace.enabled)
            if action == "enable":
                payload["health_diagnostics"] = frida_hid_tap_runtime.HID_DIAGNOSTIC_REVISION
                if self._selected_key is not None:
                    payload["selected_key"] = self._selected_key
                    payload["source_binding_revision"] = frida_hid_tap_runtime.SOURCE_BINDING_REVISION
                    ownership = {}
                    payload["exclusive_source_verified"] = frida_hid_tap_runtime.rc003_hidogatt_host_is_exclusive(
                        self._diagnostic_host_pid, diagnostic=ownership, selected_key=self._selected_key
                    )
                    self._record_diagnostic("hid_copy_host_scope", exclusive=payload["exclusive_source_verified"], **ownership)
            if (
                action == "enable" and self._copy_probe_seconds
                and self._diagnostic_trace is not None and self._diagnostic_trace.enabled
            ):
                payload["copy_probe_seconds"] = self._copy_probe_seconds
            encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode(
                "ascii"
            )
            client.sendall(encoded)
        return action

    def _record_control_ack(self, message: dict) -> bool:
        action = message.get("action")
        state = message.get("state")
        protocol_valid = message.get("protocol") == HID_INTERCEPT_PROTOCOL
        if action != "renew" or message.get("accepted") is not True or state != "enabled" or not protocol_valid:
            detail = message.get("detail")
            if not protocol_valid:
                detail = "protocol_mismatch"
            self._record_diagnostic(
                "hid_control_ack", action=action if isinstance(action, str) and action in {"enable", "renew", "disable", "bind_copy_handle", "reject_copy_handle"} else "unknown",
                state=state if isinstance(state, str) and state in {"bound", "unbound", "enabled", "disabled"} else "unknown",
                accepted=message.get("accepted") is True,
                protocol_valid=protocol_valid,
                reason=detail if isinstance(detail, str) and detail in {"", "protocol_mismatch", "stale_copy_candidate", "invalid_control", "invalid_source_selection"} else "unresolved",
            )
        if (
            message.get("protocol") != HID_INTERCEPT_PROTOCOL
            or message.get("accepted") is not True
        ):
            self._set_status(HidTapState.FAILED, "gadget_control_rejected")
            return False
        action = message.get("action")
        state = message.get("state")
        if action == "bind_copy_handle" and state == "bound":
            return True
        if action == "reject_copy_handle" and state == "unbound":
            return True
        with self._client_lock:
            if action in {"enable", "renew"} and state == "enabled":
                self._interception_enabled = True
                self._lease_deadline = (
                    time.monotonic() + HID_INTERCEPT_LEASE_SECONDS
                )
                return True
            if action == "disable" and state == "disabled":
                self._interception_enabled = False
                self._lease_deadline = 0.0
                self._disable_ack_event.set()
                return True
        self._set_status(HidTapState.FAILED, "gadget_control_ack_invalid")
        return False

    def _bind_copy_candidate(self, client: socket.socket, message: dict, pid: int) -> bool:
        handle, epoch = message.get("handle"), message.get("epoch")
        if (
            message.get("protocol") != HID_INTERCEPT_PROTOCOL
            or not isinstance(handle, str) or len(handle) > 18 or not handle.startswith("0x")
            or type(epoch) is not int or not 0 < epoch <= 2**53 - 1
        ):
            self._set_status(HidTapState.FAILED, "gadget_copy_candidate_invalid")
            return False
        try:
            parsed_handle = int(handle, 16)
        except ValueError:
            parsed_handle = 0
        source_details = {}
        device_evidence = (
            self._selected_key is not None
            and message.get("source_binding_revision") == frida_hid_tap_runtime.SOURCE_BINDING_REVISION
            and type(message.get("source_binding_revision")) is int
            and message.get("source_key") == self._selected_key
        )
        if device_evidence and message.get("source_kind") == "device":
            source_verified = bool(parsed_handle) and frida_hid_tap_runtime.find_rc003_hidogatt_host_pid(
                selected_key=self._selected_key) == pid
            source_details["reason"] = "device_container_verified" if source_verified else "host_changed"
        elif self._selected_key is None or (device_evidence and message.get("source_kind") == "exclusive"):
            source_verified = bool(parsed_handle) and frida_hid_tap_runtime.rc003_hidogatt_host_is_exclusive(
                pid, diagnostic=source_details, selected_key=self._selected_key
            )
        else:
            source_verified = False
            source_details["reason"] = "device_source_mismatch"
        self._record_diagnostic("hid_copy_source_check", host_pid=pid, verified=source_verified,
                                **(source_details or {"reason": "invalid_handle" if not parsed_handle else "unresolved"}))
        if not source_verified:
            if not (device_evidence and message.get("source_kind") == "exclusive" and parsed_handle):
                self._set_status(HidTapState.FAILED, "gadget_copy_source_unverified")
                return False
            # Another device may have joined since enable. Retire the tentative
            # fallback and wait for per-device evidence on this same connection.
            self._set_status(HidTapState.SHARED_HOST, "selected_device_shared_host")
        with self._control_send_lock:
            if self._stop_requested_event.is_set():
                return False
            payload = {"kind": "intercept_control", "action": "bind_copy_handle" if source_verified else "reject_copy_handle",
                       "protocol": HID_INTERCEPT_PROTOCOL, "handle": handle, "epoch": epoch}
            client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("ascii"))
        if source_verified:
            self._record_diagnostic("hid_copy_source_verified", host_pid=pid, handle=hex(parsed_handle), epoch=epoch)
        return True

    def _run_lease_renewal(
        self,
        client: socket.socket,
        stop_event: threading.Event,
        failed_event: threading.Event,
    ) -> None:
        """Renew interception independently from potentially slow callbacks."""

        while not stop_event.wait(HID_INTERCEPT_RENEW_INTERVAL_SECONDS):
            if self.stop_event.is_set() or self._stop_requested_event.is_set():
                return
            try:
                sent_action = self._send_control(client, "renew")
            except Exception:  # noqa: BLE001 - fail closed on socket/control loss
                if (
                    stop_event.is_set()
                    or self.stop_event.is_set()
                    or self._stop_requested_event.is_set()
                ):
                    return
                self._set_status(
                    HidTapState.UNHEALTHY,
                    "gadget_control_send_failed",
                )
                failed_event.set()
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except (AttributeError, OSError):
                    pass
                return
            if sent_action != "renew":
                return

    def _disable_interception_before_stop(self) -> None:
        with self._client_lock:
            client = self._client
            lease_deadline = self._lease_deadline
        if client is None:
            return

        self._disable_ack_event.clear()
        sent = False
        try:
            self._send_control(client, "disable")
            sent = True
        except OSError:
            pass
        if sent and self._disable_ack_event.wait(
            HID_INTERCEPT_DISABLE_ACK_TIMEOUT_SECONDS
        ):
            return

        # A delivered enable may be awaiting its acknowledgement when stop()
        # begins. Waiting one full bounded lease is therefore the only safe
        # fallback if the explicit disable acknowledgement does not arrive.
        remaining = max(
            lease_deadline - time.monotonic(),
            HID_INTERCEPT_LEASE_SECONDS,
        )
        time.sleep(remaining + HID_INTERCEPT_LEASE_SAFETY_SECONDS)
        with self._client_lock:
            self._interception_enabled = False
            self._lease_deadline = 0.0

    def _run_guarded(self) -> None:
        try:
            self._record_diagnostic(
                "hid_environment", **_diagnostic_environment(),
                detailed_trace_enabled=bool(self._diagnostic_trace is not None and self._diagnostic_trace.enabled),
                copy_probe_configured_seconds=self._copy_probe_seconds,
            )
        except Exception as exc:
            self._record_diagnostic("hid_environment_unavailable", error_type=type(exc).__name__)
        try:
            self._run()
            if not self.stop_event.is_set():
                self._set_status(HidTapState.FAILED, "tap_thread_returned")
        except BaseException as exc:  # noqa: BLE001 - thread must fail closed
            if not self.stop_event.is_set():
                self._set_status(
                    HidTapState.FAILED,
                    f"tap_thread_exception_{type(exc).__name__}",
                )
        finally:
            if self.stop_event.is_set():
                self._set_status(HidTapState.STOPPED)

    def _run(self) -> None:
        reload_grace_deadline = 0.0
        reload_mismatch = ""
        injection_attempted_pid: int | None = None
        injection_failed_pid: int | None = None
        transient_retry_pid: int | None = None
        transient_injection_failures = 0
        connection_deadline: float | None = None
        while not self.stop_event.is_set():
            lookup_details = {}
            pid = frida_hid_tap_runtime.find_rc003_hidogatt_host_pid(
                diagnostic=lookup_details, selected_key=self._selected_key)
            self._diagnostic_host_pid = pid
            lookup_state = (pid, lookup_details)
            if lookup_state != self._last_host_lookup:
                self._last_host_lookup = lookup_state
                self._record_diagnostic("hid_host_lookup", **lookup_details)
            if pid is None:
                injection_attempted_pid = None
                injection_failed_pid = None
                transient_retry_pid = None
                transient_injection_failures = 0
                connection_deadline = None
                self._set_status(HidTapState.WAITING_HOST)
                self.stop_event.wait(self.retry_delay)
                continue
            # A shared PID is permitted; interception remains disabled until the
            # Gadget proves the individual device (or a fresh exclusive binding).
            # Legacy non-reloadable Gadget migration still has its own guard.
            if pid != injection_attempted_pid and pid != injection_failed_pid:
                injection_attempted_pid = None
                injection_failed_pid = None
                connection_deadline = None
                reload_mismatch = ""
            if pid != transient_retry_pid:
                transient_retry_pid = pid
                transient_injection_failures = 0
            if reload_mismatch and time.monotonic() >= reload_grace_deadline:
                injection_failed_pid = pid
                self._set_status(HidTapState.RESTART_REQUIRED, reload_mismatch)
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
                        transient_injection_failures = 0
                        connection_deadline = time.monotonic() + self.connection_timeout
                        reload_grace_deadline = connection_deadline
                        reload_mismatch = ""
                    except Exception as exc:  # noqa: BLE001 - retry with sanitized state
                        detail = (
                            str(exc)
                            if isinstance(exc, HidTapInjectionError)
                            else f"injector_exception_{type(exc).__name__}"
                        )
                        retry_same_pid = detail in HID_TAP_RETRYABLE_INJECTION_DETAILS
                        if detail in HID_TAP_BOUNDED_RETRYABLE_INJECTION_DETAILS:
                            transient_injection_failures += 1
                            retry_same_pid = (
                                transient_injection_failures
                                < HID_TAP_TRANSIENT_INJECTION_MAX_ATTEMPTS
                            )
                        if not retry_same_pid:
                            injection_failed_pid = pid
                        if detail == "hid_helper_host_restarted":
                            self._set_status(HidTapState.WAITING_HOST, detail)
                            self.stop_event.wait(self.retry_delay)
                            continue
                        self._set_status(
                            HidTapState.RESTART_REQUIRED
                            if detail == "hid_helper_legacy_runtime_restart_required"
                            else HidTapState.FAILED,
                            detail,
                        )
                        self.stop_event.wait(self.retry_delay)
                        continue
                if connection_deadline is None:
                    connection_deadline = time.monotonic() + self.connection_timeout
                self._set_status(HidTapState.WAITING_CONNECTION)
                try:
                    client, _address = server.accept()
                except socket.timeout:
                    if time.monotonic() >= connection_deadline:
                        injection_failed_pid = pid
                        self._set_status(
                            HidTapState.RESTART_REQUIRED if reload_mismatch else HidTapState.FAILED,
                            reload_mismatch or "gadget_connection_timeout",
                        )
                    continue
                connection_deadline = None
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
                client.settimeout(0.25)
                self._diagnostic_connection_seq += 1
                self._set_client(client)
                lease_renewal_stop_event = threading.Event()
                lease_renewal_failed_event = threading.Event()
                lease_renewal_thread: threading.Thread | None = None
                try:
                    self._set_status(
                        HidTapState.WAITING_CONNECTION,
                        "gadget_handshake_pending",
                    )
                    buffer = b""
                    last_heartbeat = time.monotonic()
                    io_verified = False
                    control_enabled = False
                    while not self.stop_event.is_set():
                        if reload_mismatch and time.monotonic() >= reload_grace_deadline:
                            injection_failed_pid = pid
                            self._set_status(HidTapState.RESTART_REQUIRED, reload_mismatch)
                            break
                        if lease_renewal_failed_event.is_set():
                            break
                        if frida_hid_tap_runtime.find_rc003_hidogatt_host_pid(selected_key=self._selected_key) != pid:
                            self._set_status(HidTapState.WAITING_HOST, "host_changed")
                            injection_attempted_pid = None
                            connection_deadline = None
                            break
                        try:
                            chunk = client.recv(65536)
                        except socket.timeout:
                            chunk = None
                        except OSError:
                            self._set_status(
                                HidTapState.UNHEALTHY,
                                "gadget_connection_io_failed",
                            )
                            break
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
                                    self._set_status(
                                        HidTapState.UNHEALTHY,
                                        "gadget_message_invalid_json",
                                    )
                                    fatal_message = True
                                    break
                                if not isinstance(message, dict):
                                    self._set_status(
                                        HidTapState.UNHEALTHY,
                                        "gadget_message_not_object",
                                    )
                                    fatal_message = True
                                    break
                                kind = message.get("kind")
                                if kind == "ready":
                                    last_heartbeat = time.monotonic()
                                    self._record_ready_diagnostics(message)
                                    if message.get("hook_installed") is not True:
                                        self._set_status(
                                            HidTapState.FAILED,
                                            "gadget_hook_not_installed",
                                        )
                                        fatal_message = True
                                        break
                                    mismatch = "gadget_intercept_protocol_mismatch" if message.get("protocol") != HID_INTERCEPT_PROTOCOL else ""
                                    if not mismatch and self._selected_key is not None and (
                                        type(message.get("source_binding_revision")) is not int
                                        or message.get("source_binding_revision") != frida_hid_tap_runtime.SOURCE_BINDING_REVISION
                                    ):
                                        mismatch = "gadget_source_binding_revision_mismatch"
                                    if mismatch:
                                        # Preparing a reloadable script and the host's file
                                        # watcher are asynchronous. Never enable the old
                                        # script; allow the replacement to reconnect within
                                        # the original injection deadline, without reinjecting.
                                        if self._selected_key is not None and time.monotonic() < reload_grace_deadline:
                                            reload_mismatch = mismatch
                                            self._set_status(HidTapState.WAITING_CONNECTION, "gadget_runtime_reload_pending")
                                        else:
                                            injection_failed_pid = pid
                                            self._set_status(HidTapState.RESTART_REQUIRED, mismatch)
                                        fatal_message = True
                                        break
                                    reload_grace_deadline = 0.0
                                    reload_mismatch = ""
                                    self.native_copy_interception = True
                                    action = (
                                        "disable"
                                        if self._stop_requested_event.is_set()
                                        else "enable"
                                    )
                                    try:
                                        self._send_control(client, action)
                                    except OSError:
                                        self._set_status(
                                            HidTapState.UNHEALTHY,
                                            "gadget_control_send_failed",
                                        )
                                        fatal_message = True
                                        break
                                elif kind == "heartbeat":
                                    last_heartbeat = time.monotonic()
                                    self._record_copy_health(message)
                                elif kind == "copy_failure":
                                    self._record_copy_failure(message)
                                elif kind == "copy_probe":
                                    self._record_copy_probe(message)
                                elif kind == "copy_candidate":
                                    if not self._bind_copy_candidate(client, message, pid):
                                        fatal_message = True
                                        break
                                elif kind == "source_evidence":
                                    self._record_source_evidence(message)
                                elif kind == "source_observation":
                                    value = message.get("device_ref")
                                    if (message.get("result") == "not_selected" and isinstance(value, str)
                                            and len(value) == 12 and all(c in "0123456789abcdef" for c in value)):
                                        self._record_diagnostic("device_source_observation", device_ref=value, result="not_selected")
                                elif kind == "source_status":
                                    if (self._selected_key is not None
                                        and type(message.get("source_binding_revision")) is int
                                        and message.get("source_binding_revision") == frida_hid_tap_runtime.SOURCE_BINDING_REVISION
                                        and message.get("verified") is False):
                                        io_verified = False
                                        self._record_diagnostic("hid_copy_source_check", verified=False,
                                            **self._source_evidence_fields(message))
                                        self._set_status(HidTapState.SHARED_HOST, "selected_device_shared_host")
                                        self._release_active()
                                elif kind == "control_ack":
                                    if not self._record_control_ack(message):
                                        fatal_message = True
                                        break
                                    action = message.get("action")
                                    if action in {"enable", "renew"}:
                                        control_enabled = True
                                        if action == "enable" and lease_renewal_thread is None:
                                            renewal_thread = threading.Thread(
                                                target=self._run_lease_renewal,
                                                args=(
                                                    client,
                                                    lease_renewal_stop_event,
                                                    lease_renewal_failed_event,
                                                ),
                                                name="rc003-hidogatt-lease-renewal",
                                                daemon=True,
                                            )
                                            try:
                                                renewal_thread.start()
                                            except Exception:
                                                self._set_status(
                                                    HidTapState.UNHEALTHY,
                                                    "gadget_control_thread_start_failed",
                                                )
                                                fatal_message = True
                                                break
                                            lease_renewal_thread = renewal_thread
                                        if not io_verified and action == "enable":
                                            self._set_status(
                                                HidTapState.ATTACHED_WAITING_IO,
                                                "hid_interception_armed",
                                            )
                                    elif action == "disable":
                                        control_enabled = False
                                        self._release_active()
                                elif kind == "intercept_expired":
                                    if message.get("protocol") != HID_INTERCEPT_PROTOCOL:
                                        injection_failed_pid = pid
                                        self._set_status(
                                            HidTapState.RESTART_REQUIRED,
                                            "gadget_intercept_protocol_mismatch",
                                        )
                                    else:
                                        self._set_status(
                                            HidTapState.UNHEALTHY,
                                            "gadget_intercept_lease_expired",
                                        )
                                    control_enabled = False
                                    self._release_active()
                                    fatal_message = True
                                    break
                                elif kind == "gatt_read":
                                    if (
                                        not control_enabled
                                        or
                                        message.get("protocol")
                                        != HID_INTERCEPT_PROTOCOL
                                        or message.get("intercepted") is not True
                                        or (self._selected_key is not None and (
                                            message.get("source_key") != self._selected_key
                                            or type(message.get("source_binding_revision")) is not int
                                            or message.get("source_binding_revision") != frida_hid_tap_runtime.SOURCE_BINDING_REVISION
                                        ))
                                    ):
                                        self._set_status(
                                            HidTapState.FAILED,
                                            "gadget_report_not_intercepted",
                                        )
                                        fatal_message = True
                                        break
                                    raw = message.get("raw", "")
                                    try:
                                        data = bytes.fromhex(raw)
                                    except (TypeError, ValueError):
                                        self._set_status(
                                            HidTapState.UNHEALTHY,
                                            "gadget_report_invalid_hex",
                                        )
                                        fatal_message = True
                                        break
                                    if decode_rc003_ioctl_output(data) is None:
                                        self._set_status(
                                            HidTapState.UNHEALTHY,
                                            "gadget_report_invalid",
                                        )
                                        fatal_message = True
                                        break
                                    first_verified_report = not io_verified
                                    io_verified = True
                                    self._set_status(
                                        HidTapState.READY,
                                        "hid_interception_verified",
                                    )
                                    self._handle_ioctl_output(
                                        data,
                                        force=first_verified_report,
                                        copy_probe_id=(
                                            message["copy_probe_id"]
                                            if type(message.get("copy_probe_id")) is int
                                            and 0 < message["copy_probe_id"] <= 2**63 - 1
                                            else 0
                                        ),
                                    )
                                elif kind == "error":
                                    self._record_hook_error(message)
                                    self._set_status(HidTapState.FAILED, "gadget_hook_error")
                                    fatal_message = True
                                    break
                                else:
                                    self._set_status(
                                        HidTapState.UNHEALTHY,
                                        "gadget_message_kind_invalid",
                                    )
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
                            self._set_status(
                                HidTapState.READY,
                                "hid_interception_verified",
                            )
                except BaseException as exc:
                    # Report loss of interception before synthesizing the
                    # neutral report below. The app treats a status failure as
                    # cancellation; emitting the neutral report first could
                    # otherwise complete a half-seen hold as a normal click.
                    self._set_status(
                        HidTapState.FAILED,
                        f"tap_connection_exception_{type(exc).__name__}",
                    )
                    raise
                finally:
                    lease_renewal_stop_event.set()
                    try:
                        client.shutdown(socket.SHUT_RDWR)
                    except (AttributeError, OSError):
                        pass
                    if (
                        lease_renewal_thread is not None
                        and lease_renewal_thread is not threading.current_thread()
                    ):
                        lease_renewal_thread.join(timeout=1.0)
                        if lease_renewal_thread.is_alive():
                            self._set_status(
                                HidTapState.FAILED,
                                "gadget_control_thread_stuck",
                            )
                    self._set_client(None)
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
        if self._diagnostic_trace is not None:
            self._diagnostic_trace.emit(
                "hid_report_trace_ready",
                tap_id=self._diagnostic_tap_id,
                capture_version=1,
                raw_report_bytes=9,
                synthetic_release_labeled=True,
            )
        self._stop_requested_event.clear()
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
        self._stop_requested_event.set()
        self._disable_interception_before_stop()
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
