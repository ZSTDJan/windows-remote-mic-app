"""Logging configuration that never touches voice content or device identity.

Callers must only ever log opaque markers (button names, session ids,
sample/frame counts, boolean flags) - never a Bluetooth address, HID
interface path, device token, or decoded voice content. The primary guarantee
comes from code review plus tests/test_privacy_contract.py, which statically
scans this package's source for sensitive field names and MAC-address-shaped
literals. As defense in depth, the persistent handler also removes traceback
text and replaces exception arguments with their exception type; this is not
a substitute for keeping sensitive values out of ordinary log messages.

``log_dir``/``log_file_path``/``hid_helper_log_file_path`` (XRBM-029) expose
the canonical log locations without creating them. ``app.log`` is owned by
the long-running bridge logger; the ordinary desktop parent writes completed
administrator-helper results to ``hid-helper.log`` and immediately closes it.
The elevated helper never writes a user-controlled path.
``describe_log_location``/``open_log_location`` build on those two path
functions to answer "does it exist" and "open it" respectively, the latter
via an injectable ``_open_directory`` callable (mirrors this package's other
OS-facing modules, e.g. ``single_instance.py``) so
tests/test_logging_setup_location.py can exercise every branch without
actually invoking Windows Explorer.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import re
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from . import config

LOGGER_NAME = "ovb_rc003"
LOG_FILENAME = "app.log"
HID_HELPER_LOG_FILENAME = "hid-helper.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_HID_EVENT_MARKER_PATTERN = re.compile(r"[A-Za-z0-9_.:=+-]{1,160}")

_configured = False
_configuration_lock = threading.Lock()
_one_shot_write_lock = threading.Lock()


class PrivacySafeExceptionFilter(logging.Filter):
    """Keep native exception details out of the persistent log.

    WinRT, Raw Input, PortAudio, Frida, and shell exceptions can embed a
    device interface path or other machine-local identifier in ``str(exc)``
    and in a formatted traceback. Preserve the exception type for diagnosis,
    but never persist the raw exception text or traceback.
    """

    @staticmethod
    def _safe_argument(value):
        if isinstance(value, BaseException):
            return type(value).__name__
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._safe_argument(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: self._safe_argument(value) for key, value in record.args.items()
            }
        if record.exc_info is not None:
            record.exc_info = None
            record.exc_text = None
        return True


def get_logger(root: Optional[Path] = None) -> logging.Logger:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    with _configuration_lock:
        if _configured:
            return logger

        logger.setLevel(logging.INFO)
        directory = log_dir(root)
        directory.mkdir(parents=True, exist_ok=True)

        handler = RotatingFileHandler(
            directory / LOG_FILENAME,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.addFilter(PrivacySafeExceptionFilter())
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
        _configured = True
    return logger


def write_parent_hid_helper_event(
    event: str,
    *,
    available: Optional[bool] = None,
    detail: str = "",
    root: Optional[Path] = None,
) -> bool:
    """Log a finished helper result from the ordinary, non-elevated parent.

    Elevated helper entry points must not call this function: LocalAppData is
    user controlled and therefore not a valid privileged write destination.
    """

    safe_event = str(event)
    if _HID_EVENT_MARKER_PATTERN.fullmatch(safe_event) is None:
        safe_event = "invalid_event"
    safe_detail = str(detail or "none")
    if _HID_EVENT_MARKER_PATTERN.fullmatch(safe_detail) is None:
        safe_detail = "invalid_detail"
    available_text = "unknown" if available is None else str(bool(available)).lower()
    record = logging.LogRecord(
        LOGGER_NAME,
        logging.INFO,
        __file__,
        0,
        "HID helper event: event=%s available=%s detail=%s",
        (safe_event, available_text, safe_detail),
        None,
    )
    line = logging.Formatter(LOG_FORMAT).format(record) + "\n"
    try:
        with _one_shot_write_lock:
            directory = log_dir(root)
            directory.mkdir(parents=True, exist_ok=True)
            with hid_helper_log_file_path(root).open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(line)
        return True
    except (OSError, ValueError):
        return False


def log_dir(root: Optional[Path] = None) -> Path:
    """The canonical log directory - does NOT create it (see module
    docstring); only an actual log write creates it.
    """

    return (root or config.config_root()) / "logs"


def log_file_path(root: Optional[Path] = None) -> Path:
    return log_dir(root) / LOG_FILENAME


def hid_helper_log_file_path(root: Optional[Path] = None) -> Path:
    return log_dir(root) / HID_HELPER_LOG_FILENAME


class LogLocationStatus(Enum):
    DIRECTORY_MISSING = "directory_missing"
    FILE_MISSING = "file_missing"
    READY = "ready"


@dataclass(frozen=True)
class LogLocation:
    status: LogLocationStatus
    directory: Path
    file_path: Path


def describe_log_location(root: Optional[Path] = None) -> LogLocation:
    """Inspect the log directory without creating it.

    ``FILE_MISSING`` means the directory exists but neither supported log
    exists. A helper-only directory is ready to open.
    """

    directory = log_dir(root)
    file_path = log_file_path(root)
    if not directory.is_dir():
        return LogLocation(LogLocationStatus.DIRECTORY_MISSING, directory, file_path)
    if not file_path.is_file():
        helper_file_path = hid_helper_log_file_path(root)
        if helper_file_path.is_file():
            return LogLocation(LogLocationStatus.READY, directory, helper_file_path)
        return LogLocation(LogLocationStatus.FILE_MISSING, directory, file_path)
    return LogLocation(LogLocationStatus.READY, directory, file_path)


class LogOpenOutcome(Enum):
    OPENED = "opened"
    DIRECTORY_MISSING = "directory_missing"
    OPEN_FAILED = "open_failed"


@dataclass(frozen=True)
class LogOpenResult:
    outcome: LogOpenOutcome
    location: LogLocation
    error: Optional[str] = None


def _default_open_directory(directory: Path) -> None:
    # os.startfile only exists on Windows (CPython does not define it on
    # other platforms at all) - checked explicitly rather than caught as an
    # AttributeError, so the resulting OSError message is clear about why.
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError("os.startfile is only available on Windows")
    startfile(str(directory))


def open_log_location(
    root: Optional[Path] = None,
    *,
    _open_directory: Callable[[Path], None] = _default_open_directory,
) -> LogOpenResult:
    """Opens the log directory in the OS file browser - never the log FILE
    directly, since which application handles a bare ``.log`` extension is
    unpredictable and not this project's concern. Refuses to open (or
    create) a directory that does not exist yet; any exception the actual
    open call raises is caught and reported rather than propagated, since
    this is called directly from a Tk button handler that must not crash
    the settings window over it.
    """

    location = describe_log_location(root)
    if location.status is LogLocationStatus.DIRECTORY_MISSING:
        return LogOpenResult(LogOpenOutcome.DIRECTORY_MISSING, location)
    try:
        _open_directory(location.directory)
    except Exception as exc:  # noqa: BLE001 - report, never crash the settings window
        return LogOpenResult(LogOpenOutcome.OPEN_FAILED, location, error=str(exc))
    return LogOpenResult(LogOpenOutcome.OPENED, location)
