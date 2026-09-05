"""Per-user consumers of the shared pre-authorized HID helper."""

from __future__ import annotations

import enum
import hashlib
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import hid_elevation_windows, single_instance


CONSUMER_SCHEMA_VERSION = 1
CONSUMER_DIRECTORY_NAME = "hid-helper-consumers"
INSTALLED_KIND = "installed"
PORTABLE_KIND = "portable"
_MAINTENANCE_MUTEX_NAME = r"Global\RemoteMicRC003_HidHelperMaintenance"
_MAINTENANCE_LOCK_TIMEOUT_SECONDS = 130.0
_MAINTENANCE_LOCK_POLL_SECONDS = 0.05
_PROCESS_MAINTENANCE_LOCK = threading.RLock()


class OtherConsumerPresence(str, enum.Enum):
    NONE = "none"
    PRESENT = "present"
    UNKNOWN = "unknown"


class _MarkerState(str, enum.Enum):
    VALID = "valid"
    STALE = "stale"
    UNKNOWN = "unknown"


class ConsumerMaintenanceError(RuntimeError):
    """Raised when shared-helper ownership cannot be changed atomically."""


@contextmanager
def consumer_maintenance_lock(
    *,
    timeout_seconds: float = _MAINTENANCE_LOCK_TIMEOUT_SECONDS,
    poll_interval: float = _MAINTENANCE_LOCK_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Serialize consumer registration and helper removal across processes."""

    timeout = max(0.1, float(timeout_seconds))
    if not _PROCESS_MAINTENANCE_LOCK.acquire(timeout=timeout):
        raise ConsumerMaintenanceError("helper_consumer_maintenance_busy")

    guard: single_instance.BridgeInstanceGuard | None = None
    try:
        if sys.platform == "win32":
            deadline = monotonic() + timeout
            while True:
                candidate = single_instance.BridgeInstanceGuard(
                    name=_MAINTENANCE_MUTEX_NAME,
                    _duplicate_message="HID helper maintenance is already running",
                    _access_denied_means_duplicate=True,
                )
                try:
                    candidate.__enter__()
                except single_instance.DuplicateInstanceError as exc:
                    if monotonic() >= deadline:
                        raise ConsumerMaintenanceError(
                            "helper_consumer_maintenance_busy"
                        ) from exc
                    sleep(max(0.01, float(poll_interval)))
                    continue
                except Exception as exc:
                    raise ConsumerMaintenanceError(
                        "helper_consumer_maintenance_unavailable"
                    ) from exc
                guard = candidate
                break
        yield
    finally:
        try:
            if guard is not None:
                guard.__exit__(None, None, None)
        finally:
            _PROCESS_MAINTENANCE_LOCK.release()


def _lexical_executable_path(executable: Optional[str] = None) -> Path:
    return Path(os.path.abspath(executable or sys.executable))


def _consumer_key(executable: Path) -> str:
    normalized = os.path.normcase(str(_lexical_executable_path(str(executable))))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def consumer_directory(config_root: Path) -> Path:
    return Path(config_root) / CONSUMER_DIRECTORY_NAME


def consumer_marker_path(config_root: Path, executable: Path) -> Path:
    return consumer_directory(config_root) / f"{_consumer_key(executable)}.json"


def _distribution_kind(executable: Path) -> str:
    return (
        INSTALLED_KIND
        if hid_elevation_windows.is_installed_distribution(
            frozen=True, executable=str(executable)
        )
        else PORTABLE_KIND
    )


def _marker_payload(executable: Path) -> dict[str, Any]:
    bundled = executable.parent / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
    return {
        "schema_version": CONSUMER_SCHEMA_VERSION,
        "kind": _distribution_kind(executable),
        "executable_path": str(executable),
        "helper_protocol_version": hid_elevation_windows.HELPER_PROTOCOL_VERSION,
        "helper_generation": hid_elevation_windows.HELPER_GENERATION,
        "bundled_helper_sha256": (
            hid_elevation_windows._sha256(bundled) if bundled.is_file() else ""
        ),
    }


def _register_current_consumer_unlocked(
    config_root: Path,
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> Optional[Path]:
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return None
    app = _lexical_executable_path(executable)
    bundled = app.parent / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
    if not app.is_file() or not bundled.is_file():
        return None
    directory = consumer_directory(config_root)
    directory.mkdir(parents=True, exist_ok=True)
    destination = consumer_marker_path(config_root, app)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(_marker_payload(app), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return destination


def register_current_consumer(
    config_root: Path,
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    timeout_seconds: float = _MAINTENANCE_LOCK_TIMEOUT_SECONDS,
) -> Optional[Path]:
    with consumer_maintenance_lock(timeout_seconds=timeout_seconds):
        return _register_current_consumer_unlocked(
            config_root,
            frozen=frozen,
            executable=executable,
        )


def _unregister_current_consumer_unlocked(
    config_root: Path,
    *,
    executable: Optional[str] = None,
) -> None:
    marker = consumer_marker_path(
        config_root, _lexical_executable_path(executable)
    )
    marker.unlink(missing_ok=True)
    try:
        marker.parent.rmdir()
    except OSError:
        pass


def unregister_current_consumer(
    config_root: Path,
    *,
    executable: Optional[str] = None,
) -> None:
    with consumer_maintenance_lock():
        _unregister_current_consumer_unlocked(
            config_root,
            executable=executable,
        )


def _inspect_marker(
    path: Path,
) -> tuple[_MarkerState, Optional[dict[str, Any]]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _MarkerState.STALE, None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _MarkerState.UNKNOWN, None
    if not isinstance(raw, dict):
        return _MarkerState.UNKNOWN, None
    required = {
        "schema_version",
        "kind",
        "executable_path",
        "helper_protocol_version",
        "helper_generation",
        "bundled_helper_sha256",
    }
    if set(raw) != required or raw.get("schema_version") != CONSUMER_SCHEMA_VERSION:
        return _MarkerState.UNKNOWN, None
    if raw.get("kind") not in {INSTALLED_KIND, PORTABLE_KIND}:
        return _MarkerState.UNKNOWN, None
    executable_path = raw.get("executable_path")
    if not isinstance(executable_path, str) or not executable_path.strip():
        return _MarkerState.UNKNOWN, None
    app = _lexical_executable_path(executable_path)
    if app.name.casefold() != "remotemicrc003.exe":
        return _MarkerState.UNKNOWN, None
    if _consumer_key(app) != path.stem:
        return _MarkerState.UNKNOWN, None
    if not app.is_file():
        return _MarkerState.STALE, None
    if raw["kind"] != _distribution_kind(app):
        return _MarkerState.UNKNOWN, None
    protocol = raw.get("helper_protocol_version")
    generation = raw.get("helper_generation")
    if (
        not isinstance(protocol, int)
        or isinstance(protocol, bool)
        or protocol <= 0
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
    ):
        return _MarkerState.UNKNOWN, None
    bundled = app.parent / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
    if not bundled.is_file():
        # The distribution still exists but is damaged or quarantined. Do not
        # let another copy treat it as gone and remove a helper it may still
        # rely on after repair.
        return _MarkerState.UNKNOWN, None
    digest = str(raw.get("bundled_helper_sha256", "")).strip().lower()
    if len(digest) != 64:
        return _MarkerState.UNKNOWN, None
    try:
        if hid_elevation_windows._sha256(bundled) != digest:
            return _MarkerState.UNKNOWN, None
    except OSError:
        return _MarkerState.UNKNOWN, None
    return _MarkerState.VALID, raw


def _load_valid_marker(path: Path) -> Optional[dict[str, Any]]:
    state, raw = _inspect_marker(path)
    return raw if state is _MarkerState.VALID else None


def _inspect_other_consumers_unlocked(
    config_root: Path,
    *,
    exclude_executable: Optional[str] = None,
) -> OtherConsumerPresence:
    """Fail closed when shared-helper ownership cannot be proven exclusive."""

    directory = consumer_directory(config_root)
    excluded_marker = (
        consumer_marker_path(
            config_root,
            _lexical_executable_path(exclude_executable),
        )
        if exclude_executable
        else None
    )
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return OtherConsumerPresence.NONE
    except OSError:
        return OtherConsumerPresence.UNKNOWN

    uncertain = False
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        marker = Path(entry.path)
        if excluded_marker is not None and marker == excluded_marker:
            continue
        state, _raw = _inspect_marker(marker)
        if state is _MarkerState.VALID:
            return OtherConsumerPresence.PRESENT
        if state is _MarkerState.UNKNOWN:
            uncertain = True
    return (
        OtherConsumerPresence.UNKNOWN
        if uncertain
        else OtherConsumerPresence.NONE
    )


def inspect_other_consumers(
    config_root: Path,
    *,
    exclude_executable: Optional[str] = None,
) -> OtherConsumerPresence:
    with consumer_maintenance_lock():
        return _inspect_other_consumers_unlocked(
            config_root,
            exclude_executable=exclude_executable,
        )


def current_consumer_is_registered(
    config_root: Path,
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> bool:
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return False
    marker = consumer_marker_path(
        config_root,
        _lexical_executable_path(executable),
    )
    state, _raw = _inspect_marker(marker)
    return state is _MarkerState.VALID


def _coerce_helper_state(
    value: object,
    *,
    unavailable_detail: str,
) -> hid_elevation_windows.HidHelperState:
    if isinstance(value, hid_elevation_windows.HidHelperState):
        return value
    return hid_elevation_windows.HidHelperState(False, unavailable_detail)


def _restore_current_consumer_unlocked(
    config_root: Path,
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> bool:
    try:
        return (
            _register_current_consumer_unlocked(
                config_root,
                frozen=frozen,
                executable=executable,
            )
            is not None
        )
    except Exception:
        return False


def install_for_current_consumer(
    config_root: Path,
    install_helper: Callable[[], hid_elevation_windows.HidHelperState],
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> hid_elevation_windows.HidHelperState:
    """Register this distribution and install while removal is excluded."""

    try:
        with consumer_maintenance_lock():
            marker = _register_current_consumer_unlocked(
                config_root,
                frozen=frozen,
                executable=executable,
            )
            if marker is None:
                return hid_elevation_windows.HidHelperState(
                    False, "helper_consumer_registration_failed"
                )
            try:
                result = install_helper()
            except Exception:
                return hid_elevation_windows.HidHelperState(
                    False, "hid_helper_install_request_failed"
                )
            return _coerce_helper_state(
                result,
                unavailable_detail="hid_helper_install_result_unavailable",
            )
    except Exception:
        return hid_elevation_windows.HidHelperState(
            False, "helper_consumer_transaction_failed"
        )


def remove_for_portable_consumer(
    config_root: Path,
    remove_helper: Callable[[], hid_elevation_windows.HidHelperState],
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> hid_elevation_windows.HidHelperState:
    """Remove the shared helper only when this portable copy is sole owner."""

    try:
        with consumer_maintenance_lock():
            presence = _inspect_other_consumers_unlocked(
                config_root,
                exclude_executable=executable or sys.executable,
            )
            if presence is OtherConsumerPresence.PRESENT:
                return hid_elevation_windows.HidHelperState(
                    False, "helper_in_use_by_other_consumer"
                )
            if presence is OtherConsumerPresence.UNKNOWN:
                return hid_elevation_windows.HidHelperState(
                    False, "helper_consumer_inspection_failed"
                )

            _unregister_current_consumer_unlocked(
                config_root,
                executable=executable,
            )
            try:
                result = _coerce_helper_state(
                    remove_helper(),
                    unavailable_detail="hid_helper_removal_result_unavailable",
                )
            except Exception:
                result = hid_elevation_windows.HidHelperState(
                    False, "hid_helper_removal_request_failed"
                )
            if (
                result.available
                and result.detail == "helper_preserved_newer_contract"
            ):
                if not _restore_current_consumer_unlocked(
                    config_root,
                    frozen=frozen,
                    executable=executable,
                ):
                    return hid_elevation_windows.HidHelperState(
                        False, "helper_consumer_marker_restore_failed"
                    )
                return hid_elevation_windows.HidHelperState(
                    False, "helper_preserved_newer_contract"
                )
            if result.available:
                return result
            if not _restore_current_consumer_unlocked(
                config_root,
                frozen=frozen,
                executable=executable,
            ):
                return hid_elevation_windows.HidHelperState(
                    False, "helper_consumer_marker_restore_failed"
                )
            return result
    except Exception:
        return hid_elevation_windows.HidHelperState(
            False, "helper_consumer_transaction_failed"
        )


def uninstall_current_distribution(
    config_root: Path,
    remove_helper: Callable[[], hid_elevation_windows.HidHelperState],
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> hid_elevation_windows.HidHelperState:
    """Drop this distribution and keep the helper for any remaining owner."""

    app = _lexical_executable_path(executable)
    try:
        with consumer_maintenance_lock():
            presence = _inspect_other_consumers_unlocked(
                config_root,
                exclude_executable=app,
            )
            if presence is OtherConsumerPresence.UNKNOWN:
                _unregister_current_consumer_unlocked(
                    config_root,
                    executable=executable,
                )
                return hid_elevation_windows.HidHelperState(
                    True, "helper_kept_for_unknown_consumer"
                )
            _unregister_current_consumer_unlocked(
                config_root,
                executable=executable,
            )
            if presence is OtherConsumerPresence.PRESENT:
                return hid_elevation_windows.HidHelperState(
                    True, "helper_kept_for_other_consumer"
                )
            try:
                result = _coerce_helper_state(
                    remove_helper(),
                    unavailable_detail="hid_helper_removal_result_unavailable",
                )
            except Exception:
                result = hid_elevation_windows.HidHelperState(
                    False, "hid_helper_removal_request_failed"
                )
            if result.available:
                return result
            if not _restore_current_consumer_unlocked(
                config_root,
                frozen=frozen,
                executable=executable,
            ):
                return hid_elevation_windows.HidHelperState(
                    False, "helper_consumer_marker_restore_failed"
                )
            return result
    except Exception:
        return hid_elevation_windows.HidHelperState(
            False, "helper_consumer_transaction_failed"
        )


def has_valid_portable_consumer(
    config_root: Path,
    *,
    exclude_executable: Optional[str] = None,
) -> bool:
    directory = consumer_directory(config_root)
    if not directory.is_dir():
        return False
    excluded = (
        os.path.normcase(str(_lexical_executable_path(exclude_executable)))
        if exclude_executable
        else ""
    )
    for marker in directory.glob("*.json"):
        raw = _load_valid_marker(marker)
        if raw is None or raw["kind"] != PORTABLE_KIND:
            continue
        candidate = os.path.normcase(
            str(_lexical_executable_path(str(raw["executable_path"])))
        )
        if excluded and candidate == excluded:
            continue
        return True
    return False
