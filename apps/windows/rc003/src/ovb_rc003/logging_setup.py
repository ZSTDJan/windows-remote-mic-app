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
import time
import hashlib
import copy
import weakref
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from . import config

LOGGER_NAME = "ovb_rc003"
LOG_FILENAME = "app.log"
HID_HELPER_LOG_FILENAME = "hid-helper.log"
HID_HELPER_LOG_MAX_BYTES = 1024 * 1024
HID_HELPER_LOG_BACKUP_COUNT = 1
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_HID_EVENT_MARKER_PATTERN = re.compile(r"[A-Za-z0-9_.:=+-]{1,160}")

_configured = False
_configuration_lock = threading.Lock()
_one_shot_write_lock = threading.Lock()
_async_handlers = weakref.WeakSet()
_async_handlers_lock = threading.Lock()
LOG_QUEUE_SIZE = 2048
LOG_RETRY_SECONDS = 1.0


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


class EvidenceFileHandler(RotatingFileHandler):
    """Optional bounded application writer; fault archives use the shared writer.

    The application enables asynchronous mode. Synchronous mode remains for
    standalone handlers and tests. Only the worker owns an asynchronous stream.
    """
    def __init__(self, filename, *, asynchronous=False, **kwargs):
        self._worker = None
        self._asynchronous = asynchronous
        self._retry_after = 0.0
        if asynchronous:
            kwargs['delay'] = True
        super().__init__(filename, **kwargs)
        from .diagnostic_trace import acquire_fault_report
        self._report_writer = acquire_fault_report(Path(filename).parent)
        self._pending = deque()
        self._pending_condition = threading.Condition()
        self._stopping = False
        self._dropped = 0
        self._write_failed = False
        if asynchronous:
            self._worker = threading.Thread(target=self._write_loop,
                                            name="remote-mic-application-log", daemon=True)
            self._worker.start()
            with _async_handlers_lock:
                _async_handlers.add(self)

    def _open(self):
        if self._asynchronous:
            Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()

    def emit(self, record):
        if self._worker is None:
            self._emit_record(record)
            self._submit_evidence(record)
            return
        # Freeze already-filtered arguments before their owners can mutate them.
        record = copy.copy(record)
        record._evidence_monotonic_ms = time.monotonic_ns() // 1_000_000
        if record.levelno >= logging.ERROR and not getattr(record, "failure_key", ""):
            record.failure_key = "log_error:" + hashlib.sha256(str(record.msg).encode()).hexdigest()[:16]
        record.msg, record.args = record.getMessage(), ()
        with self._pending_condition:
            if self._stopping:
                return
            # Preserve publication order with detailed events without waiting for
            # app.log I/O. Even a record dropped from that queue can aid diagnosis.
            self._submit_evidence(record)
            if len(self._pending) >= LOG_QUEUE_SIZE:
                self._dropped += 1
                if record.levelno >= logging.ERROR or getattr(record, "failure_key", ""):
                    for index, old in enumerate(self._pending):
                        if (isinstance(old, logging.LogRecord) and old.levelno < logging.ERROR
                                and not getattr(old, "failure_key", "")):
                            del self._pending[index]
                            break
                    else:
                        return
                else:
                    return
            self._pending.append(record)
            self._pending_condition.notify()

    def _emit_record(self, record):
        super().emit(record)

    def _submit_evidence(self, record):
        writer = getattr(self, "_report_writer", None)
        if writer is None:
            return
        try:
            explicit = getattr(record, "failure_key", "")
            key = (str(explicit)[:256] if explicit else
                   "log_error:" + hashlib.sha256(str(record.msg).encode()).hexdigest()[:16]
                   if record.levelno >= logging.ERROR else "")
            # No extra inspection: this is exactly the already privacy-filtered log.
            message = record.getMessage()
            evidence = dict(event="application_log", session_id=writer.session_id,
                               wall_time=record.created,
                               monotonic_ms=getattr(record, "_evidence_monotonic_ms", None)
                                   or time.monotonic_ns() // 1_000_000,
                               pid=record.process, thread_id=record.thread,
                               level=record.levelname, logger=record.name,
                               message=message[:2048], message_truncated=len(message) > 2048,
                               failure_key=key)
            failure_id = getattr(record, "failure_id", "")
            if failure_id:
                evidence["failure_id"] = str(failure_id)[:128]
            writer.submit(evidence)
        except Exception:
            pass

    def handleError(self, record):
        self._write_failed = True
        self._retry_after = time.monotonic() + LOG_RETRY_SECONDS
        if not self._asynchronous:
            super().handleError(record)

    def _write_record_safely(self, record):
        if time.monotonic() < self._retry_after:
            return
        try:
            self._emit_record(record)
        except Exception:
            # A transient file/formatting failure must not kill the queue owner.
            self._write_failed = True
            self._retry_after = time.monotonic() + LOG_RETRY_SECONDS

    def _write_loop(self):
        try:
            while True:
                with self._pending_condition:
                    self._pending_condition.wait_for(lambda: self._pending or self._stopping)
                    if not self._pending:
                        break
                    item = self._pending.popleft()
                    dropped, self._dropped = self._dropped, 0
                if dropped:
                    overflow = logging.LogRecord(LOGGER_NAME, logging.WARNING, __file__, 0,
                        "application log queue overflow: dropped=%s", (dropped,), None)
                    overflow.failure_key = "application_log_queue_overflow"
                    self._submit_evidence(overflow)
                    self._write_record_safely(overflow)
                if isinstance(item, threading.Event):
                    try:
                        self.flush()
                    except Exception:
                        self._write_failed = True
                    finally:
                        item.persisted = not self._write_failed
                        item.set()
                else:
                    self._write_record_safely(item)
        except Exception:
            self._write_failed = True
        finally:
            from .diagnostic_trace import release_fault_report
            writer, self._report_writer = self._report_writer, None
            release_fault_report(writer)
            # Only this worker owns the stream. Do not acquire Handler.lock:
            # logging.shutdown holds it while waiting for close() to finish.
            try:
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None
            except OSError:
                self._write_failed = True
            finally:
                logging.Handler.close(self)

    def flush_pending(self, timeout=1.0):
        if self._worker is None:
            super().flush()
            return not self._write_failed
        with self._pending_condition:
            if self._stopping or not self._worker.is_alive():
                return not self._worker.is_alive() and not self._write_failed
            if len(self._pending) >= LOG_QUEUE_SIZE:
                return False
            barrier = threading.Event()
            self._pending.append(barrier)
            self._pending_condition.notify()
        return barrier.wait(timeout) and getattr(barrier, "persisted", False)

    def flush(self):
        worker = getattr(self, "_worker", None)
        if worker is not None and threading.current_thread() is not worker:
            self.flush_pending()
        elif worker is not None:
            if self.stream is not None:
                self.stream.flush()
        else:
            super().flush()

    def close(self):
        if self._worker is not None:
            with self._pending_condition:
                self._stopping = True
                self._pending_condition.notify()
            if threading.current_thread() is not self._worker:
                self._worker.join(1.0)
            return
        from .diagnostic_trace import release_fault_report
        writer, self._report_writer = getattr(self, "_report_writer", None), None
        try:
            release_fault_report(writer)
        finally:
            super().close()


def flush_application_logs(directory: Path) -> bool:
    directory = directory.resolve()
    with _async_handlers_lock:
        handlers = [h for h in _async_handlers if Path(h.baseFilename).parent == directory]
    return all([handler.flush_pending() for handler in handlers])


def get_logger(root: Optional[Path] = None) -> logging.Logger:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    with _configuration_lock:
        if _configured:
            return logger

        logger.setLevel(logging.INFO)
        directory = log_dir(root)

        handler = EvidenceFileHandler(
            directory / LOG_FILENAME,
            asynchronous=True,
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
    try:
        with _one_shot_write_lock:
            directory = log_dir(root)
            directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(hid_helper_log_file_path(root),
                                          maxBytes=HID_HELPER_LOG_MAX_BYTES,
                                          backupCount=HID_HELPER_LOG_BACKUP_COUNT,
                                          encoding="utf-8", delay=True)
            try:
                handler.setFormatter(logging.Formatter(LOG_FORMAT))
                # Preserve this API's failure result; StreamHandler.emit otherwise
                # swallows write errors and could claim the event was persisted.
                line = handler.format(record) + "\n"
                if handler.shouldRollover(record):
                    handler.doRollover()
                if handler.stream is None:
                    handler.stream = handler._open()
                handler.stream.write(line)
                handler.flush()
            finally:
                handler.close()
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
