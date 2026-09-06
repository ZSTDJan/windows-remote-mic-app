"""Atomic, session-scoped runtime status shared by bridge and settings.

The in-process bridge worker is the sole writer and the settings controller
is read-only. Each Windows logon session owns a separate status file, matching
the Local\\ bridge mutex scope, so two sessions of the same user cannot delete
or display each other's state. The file contains no device identity, address,
HID path, or voice content; it
only distinguishes a live process waiting for RC003 from one whose BLE/ATVV
connection setup completed. A PID guard prevents an exiting old process from
deleting a newer process's status during a restart race.
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
STATUS_FILENAME = "bridge-runtime-status.json"
_SCOPED_STATUS_FILENAME_TEMPLATE = "bridge-runtime-status-{scope}.json"
_STATUS_SCOPE_UNSET = object()
_default_status_scope: object | str = _STATUS_SCOPE_UNSET
_default_status_scope_lock = threading.Lock()
FAILED_RAW_INPUT_STATES = frozenset(
    {
        "unavailable",
        "no_device",
        "ambiguous",
        "failed",
        "failed_running",
        "failed_stopping",
        "stopped",
    }
)
FAILED_HID_TAP_STATES = frozenset(
    {
        "disabled_non_windows",
        "unavailable_gadget_not_verified",
        "unhealthy",
        "failed",
        "failed_stopping",
        "stopped",
    }
)
READY_RAW_INPUT_STATES = frozenset({"ready"})
READY_HID_TAP_STATES = frozenset({"attached_waiting_for_hid_io", "ready"})


class BridgeConnectionState(Enum):
    WAITING_FOR_DEVICE = "waiting_for_device"
    CONNECTED = "connected"


@dataclass(frozen=True)
class BridgeRuntimeIdentity:
    app_version: str
    runtime_kind: str
    package_name: str
    runtime_id: str


@dataclass(frozen=True)
class BridgeRuntimeStatus:
    schema: int
    state: BridgeConnectionState
    pid: int
    updated_at: float
    app_version: str = ""
    runtime_kind: str = ""
    package_name: str = ""
    runtime_id: str = ""
    raw_input_state: str = "unknown"
    hid_tap_state: str = "unknown"
    voice_key_physicalizer_state: str = "unknown"
    last_button_at: Optional[float] = None
    last_button_source: str = ""
    voice_active: bool = False


def current_runtime_identity(
    app_version: str,
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    source_root: Optional[Path] = None,
) -> BridgeRuntimeIdentity:
    """Return a privacy-safe identity shared by settings and bridge.

    The digest distinguishes source runs and separate frozen package folders
    without writing a full local path into the status file.
    """

    resolved_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    runtime_kind = "frozen" if resolved_frozen else "source"
    if resolved_frozen:
        runtime_root = Path(executable or sys.executable).resolve().parent
        package_name = runtime_root.name
    else:
        runtime_root = (
            Path(source_root).resolve()
            if source_root is not None
            else Path(__file__).resolve().parents[2]
        )
        package_name = "source-tree"
    identity_source = "\n".join(
        (str(app_version), runtime_kind, os.path.normcase(str(runtime_root)))
    )
    runtime_id = hashlib.sha256(identity_source.encode("utf-8")).hexdigest()[:16]
    return BridgeRuntimeIdentity(
        app_version=str(app_version),
        runtime_kind=runtime_kind,
        package_name=package_name,
        runtime_id=runtime_id,
    )


def _resolve_default_status_scope() -> str:
    global _default_status_scope

    cached = _default_status_scope
    if isinstance(cached, str):
        return cached
    with _default_status_scope_lock:
        cached = _default_status_scope
        if isinstance(cached, str):
            return cached
        if sys.platform != "win32":
            resolved_scope = ""
        else:
            from . import single_instance

            try:
                resolved_scope = (
                    f"s{single_instance.current_process_session_id()}"
                )
            except (
                single_instance.SingleInstanceUnavailableError,
                OSError,
                ValueError,
            ):
                # The in-process bridge and UI share this process fallback.
                # Caching prevents later query recovery from changing paths.
                resolved_scope = f"p{os.getpid()}"
        _default_status_scope = resolved_scope
        return resolved_scope


def _status_scope(session_id: Optional[int]) -> str:
    if session_id is None:
        return _resolve_default_status_scope()
    if isinstance(session_id, bool):
        raise ValueError("session_id must be a non-negative integer")
    resolved = int(session_id)
    if resolved < 0:
        raise ValueError("session_id must be non-negative")
    return f"s{resolved}"


def status_path(
    config_root: Path,
    *,
    session_id: Optional[int] = None,
) -> Path:
    scope = _status_scope(session_id)
    filename = (
        _SCOPED_STATUS_FILENAME_TEMPLATE.format(scope=scope)
        if scope
        else STATUS_FILENAME
    )
    return Path(config_root) / filename


def runtime_identity_matches(
    status: BridgeRuntimeStatus,
    identity: BridgeRuntimeIdentity,
) -> Optional[bool]:
    if status.schema < SCHEMA_VERSION or not status.runtime_id:
        return None
    return status.runtime_id == identity.runtime_id


def input_channels_failed(status: BridgeRuntimeStatus) -> bool:
    return (
        status.raw_input_state in FAILED_RAW_INPUT_STATES
        and status.hid_tap_state in FAILED_HID_TAP_STATES
    )


def input_channels_ready(status: BridgeRuntimeStatus) -> bool:
    if status.schema < SCHEMA_VERSION:
        return False
    return (
        status.raw_input_state in READY_RAW_INPUT_STATES
        or status.hid_tap_state in READY_HID_TAP_STATES
    )


def publish_status(
    config_root: Path,
    state: BridgeConnectionState,
    *,
    pid: Optional[int] = None,
    identity: Optional[BridgeRuntimeIdentity] = None,
    raw_input_state: str = "unknown",
    hid_tap_state: str = "unknown",
    voice_key_physicalizer_state: str = "unknown",
    last_button_at: Optional[float] = None,
    last_button_source: str = "",
    voice_active: bool = False,
    session_id: Optional[int] = None,
    now: Callable[[], float] = time.time,
) -> BridgeRuntimeStatus:
    resolved_pid = os.getpid() if pid is None else int(pid)
    if resolved_pid <= 0:
        raise ValueError("bridge runtime status requires a positive PID")
    resolved_identity = identity or BridgeRuntimeIdentity("", "", "", "")
    if last_button_at is not None:
        last_button_at = float(last_button_at)
    status = BridgeRuntimeStatus(
        schema=SCHEMA_VERSION,
        state=BridgeConnectionState(state),
        pid=resolved_pid,
        updated_at=float(now()),
        app_version=resolved_identity.app_version,
        runtime_kind=resolved_identity.runtime_kind,
        package_name=resolved_identity.package_name,
        runtime_id=resolved_identity.runtime_id,
        raw_input_state=str(raw_input_state),
        hid_tap_state=str(hid_tap_state),
        voice_key_physicalizer_state=str(voice_key_physicalizer_state),
        last_button_at=last_button_at,
        last_button_source=str(last_button_source),
        voice_active=bool(voice_active),
    )
    path = status_path(config_root, session_id=session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {
                    "schema": SCHEMA_VERSION,
                    "state": status.state.value,
                    "pid": status.pid,
                    "updated_at": status.updated_at,
                    "app_version": status.app_version,
                    "runtime_kind": status.runtime_kind,
                    "package_name": status.package_name,
                    "runtime_id": status.runtime_id,
                    "raw_input_state": status.raw_input_state,
                    "hid_tap_state": status.hid_tap_state,
                    "voice_key_physicalizer_state": (
                        status.voice_key_physicalizer_state
                    ),
                    "last_button_at": status.last_button_at,
                    "last_button_source": status.last_button_source,
                    "voice_active": status.voice_active,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return status


def _read_status_file(path: Path) -> Optional[BridgeRuntimeStatus]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    schema = payload.get("schema")
    if schema not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
        return None
    try:
        state = BridgeConnectionState(payload.get("state"))
    except (TypeError, ValueError):
        return None
    pid = payload.get("pid")
    updated_at = payload.get("updated_at")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
        return None
    if schema == LEGACY_SCHEMA_VERSION:
        return BridgeRuntimeStatus(
            schema=LEGACY_SCHEMA_VERSION,
            state=state,
            pid=pid,
            updated_at=float(updated_at),
        )

    string_fields = {
        name: payload.get(
            name,
            "unknown" if name == "voice_key_physicalizer_state" else "",
        )
        for name in (
            "app_version",
            "runtime_kind",
            "package_name",
            "runtime_id",
            "raw_input_state",
            "hid_tap_state",
            "voice_key_physicalizer_state",
            "last_button_source",
        )
    }
    if not all(isinstance(value, str) for value in string_fields.values()):
        return None
    last_button_at = payload.get("last_button_at")
    if last_button_at is not None and (
        not isinstance(last_button_at, (int, float))
        or isinstance(last_button_at, bool)
    ):
        return None
    voice_active = payload.get("voice_active", False)
    if not isinstance(voice_active, bool):
        return None
    return BridgeRuntimeStatus(
        schema=SCHEMA_VERSION,
        state=state,
        pid=pid,
        updated_at=float(updated_at),
        app_version=string_fields["app_version"],
        runtime_kind=string_fields["runtime_kind"],
        package_name=string_fields["package_name"],
        runtime_id=string_fields["runtime_id"],
        raw_input_state=string_fields["raw_input_state"],
        hid_tap_state=string_fields["hid_tap_state"],
        voice_key_physicalizer_state=(
            string_fields["voice_key_physicalizer_state"]
        ),
        last_button_at=(
            None if last_button_at is None else float(last_button_at)
        ),
        last_button_source=string_fields["last_button_source"],
        voice_active=voice_active,
    )


def read_status(
    config_root: Path,
    *,
    session_id: Optional[int] = None,
) -> Optional[BridgeRuntimeStatus]:
    return _read_status_file(status_path(config_root, session_id=session_id))


def _restore_claimed_status(claimed: Path, path: Path) -> bool:
    """Restore a claimed file only when no newer writer owns ``path``."""

    try:
        if sys.platform == "win32":
            # Windows rename fails instead of replacing an existing target.
            os.rename(claimed, path)
        else:
            # POSIX rename replaces the target, so use a no-replace hard link.
            os.link(claimed, path)
            claimed.unlink()
    except FileExistsError:
        return False
    return True


def clear_status(
    config_root: Path,
    *,
    pid: Optional[int] = None,
    session_id: Optional[int] = None,
) -> bool:
    """Remove one atomically claimed status generation.

    Claiming the current path before checking its PID prevents an exiting old
    bridge from deleting a replacement status published by a newer bridge.
    """

    path = status_path(config_root, session_id=session_id)
    expected_pid = None if pid is None else int(pid)
    claimed = path.with_name(f".{path.name}.{uuid.uuid4().hex}.clear")
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return False

    remove_claimed = True
    try:
        if expected_pid is not None:
            current = _read_status_file(claimed)
            if current is None or current.pid != expected_pid:
                try:
                    _restore_claimed_status(claimed, path)
                except OSError:
                    # Keep the claimed file for diagnosis instead of deleting
                    # the only complete copy after a failed safe restore.
                    remove_claimed = False
                    raise
                return False
        return True
    finally:
        if remove_claimed:
            try:
                claimed.unlink()
            except FileNotFoundError:
                pass
