"""Atomic runtime status shared by the bridge and settings window.

The bridge is the sole writer and the settings process is read-only. The
file contains no device identity, address, HID path, or voice content; it
only distinguishes a live process waiting for RC003 from one whose BLE/ATVV
connection setup completed. A PID guard prevents an exiting old process from
deleting a newer process's status during a restart race.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


SCHEMA_VERSION = 1
STATUS_FILENAME = "bridge-runtime-status.json"


class BridgeConnectionState(Enum):
    WAITING_FOR_DEVICE = "waiting_for_device"
    CONNECTED = "connected"


@dataclass(frozen=True)
class BridgeRuntimeStatus:
    state: BridgeConnectionState
    pid: int
    updated_at: float


def status_path(config_root: Path) -> Path:
    return Path(config_root) / STATUS_FILENAME


def publish_status(
    config_root: Path,
    state: BridgeConnectionState,
    *,
    pid: Optional[int] = None,
    now: Callable[[], float] = time.time,
) -> BridgeRuntimeStatus:
    resolved_pid = os.getpid() if pid is None else int(pid)
    if resolved_pid <= 0:
        raise ValueError("bridge runtime status requires a positive PID")
    status = BridgeRuntimeStatus(
        state=BridgeConnectionState(state),
        pid=resolved_pid,
        updated_at=float(now()),
    )
    path = status_path(config_root)
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


def read_status(config_root: Path) -> Optional[BridgeRuntimeStatus]:
    try:
        payload = json.loads(status_path(config_root).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
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
    return BridgeRuntimeStatus(state=state, pid=pid, updated_at=float(updated_at))


def clear_status(config_root: Path, *, pid: Optional[int] = None) -> bool:
    path = status_path(config_root)
    if pid is not None:
        current = read_status(config_root)
        if current is None or current.pid != int(pid):
            return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
