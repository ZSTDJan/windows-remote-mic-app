"""Low-overhead, opt-in correlation trace for RC003 input and voice paths.

The normal application log remains human-oriented.  This module writes a
separate JSONL stream only when ``REMOTE_MIC_DIAGNOSTIC_TRACE=1`` (or when a
caller explicitly enables it), and never performs disk I/O on the input
callback thread.  Data is limited to button/action metadata and validated RC003
keyboard report bytes; text, window titles, device paths, and audio are never
accepted by the public helpers.
The shared fault report also retains bounded, already-sanitized application
log evidence independently of the detailed trace switch.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections import deque
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid
import weakref
from typing import Any, Optional


SCHEMA_VERSION = 1
TRACE_FILENAME = "diagnostic-trace.jsonl"
TRACE_MAX_BYTES = 5 * 1024 * 1024
TRACE_BACKUP_COUNT = 3
TRACE_QUEUE_SIZE = 2048
TRACE_FLUSH_SECONDS = 0.25
TRACE_FLUSH_RECORDS = 64
TRACE_RETRY_SECONDS = 1.0
_traces = weakref.WeakSet()
_traces_lock = threading.Lock()
GESTURE_FINALIZE_DELAY_SECONDS = 0.25
REPORT_FILENAME = "diagnostic-report.json"
REPORT_MAX_BYTES = 1024 * 1024
REPORT_BEFORE_SECONDS = 30
REPORT_AFTER_SECONDS = 15
REPORT_MERGE_SECONDS = 60
REPORT_EVENT_BYTES = 4096
REPORT_BEFORE_COUNT = 96
REPORT_EVENT_COUNT = 192


def log_shutdown_threads(logger) -> None:
    """Bounded timeout snapshot: code locations only, never locals or source text."""
    import sys
    try:
        logger.warning("Shutdown exceeded cleanup deadline; capturing thread locations",
                       extra={"failure_key": "shutdown:cleanup_timeout"})
        frames = sys._current_frames()
        for thread in threading.enumerate()[:64]:
            frame = frames.get(thread.ident)
            locations = []
            relevant = False
            while frame is not None and len(locations) < 64:
                module = str(frame.f_globals.get("__name__", ""))
                relevant |= module.startswith("ovb_rc003.")
                # Use the module rather than an absolute filename (user paths).
                locations.append(f"{module}:{frame.f_code.co_name}:{frame.f_lineno}")
                frame = frame.f_back
            if relevant:
                logger.warning("shutdown thread: ident=%s stack=%s", thread.ident,
                               " <- ".join(locations[:32]))
    except Exception as exc:
        logger.warning("shutdown thread snapshot unavailable: error_type=%s", type(exc).__name__)


class _SubmissionKeys:
    """Observe paste-key candidates in the existing hook, never ordinary text."""
    _MODIFIERS = {0x10: 'shift', 0xA0: 'shift', 0xA1: 'shift',
                  0x11: 'ctrl', 0xA2: 'ctrl', 0xA3: 'ctrl',
                  0x12: 'alt', 0xA4: 'alt', 0xA5: 'alt', 0x5B: 'win', 0x5C: 'win'}

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.context = {}
        self.deadline = 0.0
        self.modifiers = set()
        self.held = {}
        self.count = 0

    def start(self, attempt_id, gesture_id, provider):
        with self.lock:
            self.context = dict(attempt_id=attempt_id, gesture_id=gesture_id or '', provider=provider)
            self.deadline, self.count = self.clock() + 30, 0
            self.held.clear()

    def finish(self, attempt_id):
        with self.lock:
            if attempt_id == self.context.get('attempt_id'):
                self.deadline = self.clock() + 10

    def edge(self, vk, key_up, flags):
        with self.lock:
            if vk in self._MODIFIERS:
                if key_up:
                    self.modifiers.discard(vk)
                else:
                    self.modifiers.add(vk)
                return None
            if not self.deadline or self.clock() > self.deadline or self.count >= 64:
                self.held.clear()
                return None
            modifiers = {self._MODIFIERS[v] for v in self.modifiers}
            command = None
            if key_up:
                command = self.held.pop(vk, None)
            elif not modifiers & {'alt', 'win'}:
                if vk == 0x56 and 'ctrl' in modifiers:
                    command = 'ctrl_shift_v' if 'shift' in modifiers else 'ctrl_v'
                elif vk == 0x2D and modifiers == {'shift'}:
                    command = 'shift_insert'
                if command:
                    self.held[vk] = command
            if not command:
                return None
            self.count += 1
            return dict(self.context, command=command, edge='up' if key_up else 'down',
                        injected=bool(flags & 0x10), lower_integrity=bool(flags & 2),
                        origin='unknown', target_response='unknown', scope='local_paste_key_candidates')


def foreground_context() -> dict[str, Any]:
    """Read only process/thread identity, never titles, input text or screenshots."""
    if os.name != "nt":
        return {"foreground_available": False}
    try:
        from .voice_interaction_diagnostics_windows import capture_focus_snapshot, context_fields
        return context_fields(capture_focus_snapshot(include_text_length=False))
    except Exception:
        return {"foreground_available": False}


class _FaultReport:
    """Bounded cross-session incident history, written by the shared report writer."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / REPORT_FILENAME
        self.recent = deque(maxlen=REPORT_BEFORE_COUNT)
        self.seen_failures = deque(maxlen=REPORT_EVENT_COUNT)
        self.context: dict[str, dict] = {}
        self.incidents: list[dict] = []
        self.last_write = 0.0
        self.dirty = False
        try:
            if self.path.is_file() and self.path.stat().st_size <= REPORT_MAX_BYTES:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if saved.get("schema") in (1, 2) and isinstance(saved.get("incidents"), list):
                    self.incidents = [item for item in saved["incidents"][-3:]
                                      if isinstance(item, dict) and isinstance(item.get("events"), list)
                                      and isinstance(item.get("started_at"), (int, float))
                                      and isinstance(item.get("last_failure_at"), (int, float))
                                      and type(item.get("failure_count")) is int]
                    # Old reports remain readable but never become a new live window.
                    for incident in self.incidents:
                        incident["events"] = [e for e in incident["events"][-REPORT_EVENT_COUNT:]
                                              if isinstance(e, dict)]
                        incident.pop("archive_session_id", None)
        except (OSError, ValueError, AttributeError, RecursionError):
            pass

    @staticmethod
    def failed(item: dict) -> bool:
        result = str(item.get("result", ""))
        return (item.get("success") is False or item.get("verified") is False
                or bool(item.get("failure_key"))
                or result.startswith("failed") or result in {"timeout", "host_stop_failed"}
                or str(item.get("state")) in {"host_start_failed", "host_stop_failed", "output_open_failed"}
                or str(item.get("status")) in {"failed", "unhealthy", "selected_device_shared_host"}
                or str(item.get("event")) in {"hid_hook_failure", "hid_copy_failure"})

    def accept(self, item: dict) -> None:
        if len(json.dumps(item, ensure_ascii=True)) > REPORT_EVENT_BYTES:
            item = {key: item[key] for key in ("event", "session_id", "seq", "wall_time",
                    "pid", "thread_id", "success", "verified", "result", "status", "state",
                    "archive_session_id", "failure_key", "failure_id", "queue_dropped_before") if key in item}
            item["report_fields_truncated"] = True
        # Keep sticky context even when a user waits a long time before reproducing.
        if item.get("event") in {"session_started", "device_context", "hid_environment",
                "hid_runtime_capabilities", "hid_copy_host_scope", "hid_source_evidence",
                "voice_attempt_context", "voice_edge_confirmation", "voice_hotkey_control"}:
            self.context[item["event"]] = item
        now = float(item.get("wall_time", 0))
        scope = item.get("archive_session_id", item.get("session_id"))
        trigger = None
        failure_id = item.get("failure_id")
        identity = (scope, failure_id)
        duplicate = bool(failure_id) and identity in self.seen_failures
        if self.failed(item) and not duplicate:
            if failure_id:
                self.seen_failures.append(identity)
            key = str(item.get("failure_key") or ":".join(str(item.get(k, "")) for k in
                      ("event", "provider", "result", "status", "state", "reason")))[:256]
            if item.get("event") == "voice_runtime_state":
                key = f"voice:{item.get('provider', '')}:{item.get('state', '')}"
            incident = next((i for i in reversed(self.incidents)
                             if i.get("archive_session_id") == scope and i.get("failure_key") == key
                             and 0 <= now - i["last_failure_at"] <= REPORT_MERGE_SECONDS), None)
            if incident is None:
                before = [e for e in self.recent if 0 <= now - e.get("wall_time", 0) <= REPORT_BEFORE_SECONDS]
                incident = {"session_id": item.get("session_id"), "started_at": now,
                            "last_failure_at": now, "failure_count": 0,
                            "archive_session_id": scope, "failure_key": key,
                            "capture_until": now + REPORT_AFTER_SECONDS,
                            "before_count": len(before), "dropped_after": 0,
                            "before_limited": len(before) == REPORT_BEFORE_COUNT,
                            "context": dict(self.context), "events": before + [item]}
                self.incidents.append(incident)
                self.incidents = self.incidents[-3:]
                trigger = incident
            incident["last_failure_at"] = now
            incident["failure_count"] += 1
            incident["last_failure_event"] = item
            self.dirty = True
        for incident in self.incidents:
            if (incident is not trigger and incident.get("archive_session_id") == scope
                    and incident["started_at"] <= now <= incident.get("capture_until", 0)):
                incident["events"].append(item)
                if len(incident["events"]) > REPORT_EVENT_COUNT:
                    # Freeze the prelude AND first failure; only the subsequent tail rotates.
                    del incident["events"][incident["before_count"] + 1]
                    incident["dropped_after"] += 1
                self.dirty = True
        self.recent.append(item)
        self.flush(force=trigger is not None)

    def flush(self, *, force: bool = False) -> None:
        if not self.dirty or (not force and time.monotonic() - self.last_write < 0.5):
            return
        payload = {"schema": 2, "updated_at": time.time(), "scope": "existing_logs_and_diagnostic_events",
                   "before_seconds": REPORT_BEFORE_SECONDS, "after_seconds": REPORT_AFTER_SECONDS,
                   "incidents": self.incidents}
        temporary = self.path.with_suffix(".tmp")
        try:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            while len(encoded) > REPORT_MAX_BYTES:
                if len(self.incidents) > 1:
                    self.incidents.pop(0)
                elif len(self.incidents[0]["events"]) > self.incidents[0].get("before_count", 0) + 1:
                    incident = self.incidents[0]
                    del incident["events"][incident.get("before_count", 0) + 1]
                    incident["dropped_after"] = incident.get("dropped_after", 0) + 1
                else:
                    return
                encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(encoded)
            temporary.replace(self.path)
            self.dirty = False
        except OSError:
            pass  # Logging failures never change device/input behavior.
        finally:
            self.last_write = time.monotonic()


_report_writers = {}
_report_writers_lock = threading.Lock()


class _ReportWriter:
    """One bounded writer per log directory, shared by trace and ordinary log."""
    def __init__(self, directory):
        self.directory = directory
        self.session_id = uuid.uuid4().hex
        self.references = 0
        self.queue = queue.Queue(maxsize=TRACE_QUEUE_SIZE)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.dropped = 0
        self.thread = threading.Thread(target=self._run, name="remote-mic-fault-report", daemon=True)
        self.thread.start()

    def submit(self, item):
        with self.lock:
            if self.stop.is_set() or not self.thread.is_alive():
                return
            item = dict(item, archive_session_id=self.session_id)
            if self.dropped:
                item["queue_dropped_before"] = self.dropped
            try:
                self.queue.put_nowait(item)
                self.dropped = 0
            except queue.Full:
                self.dropped += 1
                if _FaultReport.failed(item):
                    # Replace only ordinary evidence, never an earlier failure
                    # or an export barrier. Keep queue size/order atomically.
                    with self.queue.mutex:
                        for index, old in enumerate(self.queue.queue):
                            if isinstance(old, dict) and not _FaultReport.failed(old):
                                del self.queue.queue[index]
                                item["queue_dropped_before"] = self.dropped
                                self.queue.queue.append(item)
                                self.queue.not_empty.notify()
                                self.dropped = 0
                                break

    def flush(self, timeout=.5):
        if not self.thread.is_alive():
            return False
        barrier = threading.Event()
        try:
            self.queue.put_nowait(barrier)
        except queue.Full:
            return False
        return barrier.wait(timeout) and getattr(barrier, "persisted", False)

    def _run(self):
        report = None
        try:
            report = _FaultReport(self.directory)
            while not self.stop.is_set() or not self.queue.empty():
                try:
                    timeout = max(0.001, .5 - (time.monotonic() - report.last_write)) if report.dirty else None
                    item = self.queue.get(timeout=timeout)
                except queue.Empty:
                    report.flush()
                    continue
                if isinstance(item, threading.Event):
                    report.flush(force=True)
                    item.persisted = not report.dirty
                    item.set()
                else:
                    report.accept(item)
        except (OSError, ValueError, TypeError, RecursionError):
            pass  # Evidence failure never changes input, audio or service decisions.
        finally:
            if report is not None:
                report.flush(force=True)


def acquire_fault_report(directory):
    directory = Path(directory).resolve()
    with _report_writers_lock:
        writer = _report_writers.get(directory)
        if writer is not None and writer.stop.is_set() and writer.thread.is_alive():
            return None  # No second disk writer while an old one is still closing.
        if writer is None or not writer.thread.is_alive():
            try:
                writer = _ReportWriter(directory)
            except (OSError, RuntimeError):
                return None
            _report_writers[directory] = writer
        writer.references += 1
        return writer


def release_fault_report(writer, timeout=1.0):
    if writer is None:
        return
    with _report_writers_lock:
        writer.references -= 1
        last = writer.references == 0
        if last:
            with writer.lock:
                writer.stop.set()
                try:
                    writer.queue.put_nowait(threading.Event())
                except queue.Full:
                    pass  # A nonempty queue already wakes the draining writer.
    if last:
        writer.thread.join(max(0.0, timeout))
        with _report_writers_lock:
            if not writer.thread.is_alive() and _report_writers.get(writer.directory) is writer:
                _report_writers.pop(writer.directory, None)
    else:
        writer.flush(min(.5, max(0.0, timeout)))


def flush_fault_report(directory):
    with _report_writers_lock:
        writer = _report_writers.get(Path(directory).resolve())
    return True if writer is None else writer.flush()


def flush_diagnostic_logs(directory):
    directory = Path(directory).resolve()
    with _traces_lock:
        traces = [trace for trace in _traces if trace.path.parent.resolve() == directory]
    return all([trace.flush() for trace in traces])


def _truthy(value: object) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


class _TraceFileState:
    """Process-local ownership and interrupted rotation progress for one path."""
    def __init__(self):
        self.condition = threading.Condition()
        self.owner = None
        self.rotation_index = None

    def rotate(self, path):
        # Only the current file owner calls this. Resume the failed rename;
        # repeating completed steps would overwrite another backup on each retry.
        if self.rotation_index is None:
            self.rotation_index = TRACE_BACKUP_COUNT - 1
        while self.rotation_index > 0:
            index = self.rotation_index
            source = path.with_name(f"{TRACE_FILENAME}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{TRACE_FILENAME}.{index + 1}"))
            self.rotation_index -= 1
        path.replace(path.with_name(f"{TRACE_FILENAME}.1"))
        self.rotation_index = None


# Keep the tiny state for the process lifetime: a service restart replaces the
# trace object and must still resume any rotation interrupted by a disk error.
_trace_file_states = {}
_trace_file_states_lock = threading.Lock()


class DiagnosticTrace:
    """An opt-in asynchronous event writer with gesture/attempt correlation."""

    def __init__(
        self,
        root: Path,
        *,
        enabled: Optional[bool] = None,
        queue_size: int = TRACE_QUEUE_SIZE,
    ) -> None:
        initial_enabled = (
            _truthy(os.environ.get("REMOTE_MIC_DIAGNOSTIC_TRACE", ""))
            if enabled is None
            else bool(enabled)
        )
        self.enabled = False
        self.root = Path(root)
        self.path = self.root / "logs" / TRACE_FILENAME
        self.session_id = uuid.uuid4().hex
        self._queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(
            maxsize=max(1, int(queue_size))
        )
        self._lifecycle_lock = threading.RLock()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._active_queue = None
        self._stream = None
        self._file_state = None
        self._seq = 0
        self._dropped = 0
        self._gestures: dict[str, str] = {}
        self._gesture_sources: dict[str, set[str]] = {}
        self._gesture_all_sources: dict[str, set[str]] = {}
        self._gesture_stats: dict[str, dict[str, int]] = {}
        self._gesture_finalize_timers: dict[str, threading.Timer] = {}
        self._last_gestures: dict[str, str] = {}
        self._attempts: dict[str, str] = {}
        self._local = threading.local()
        self._submission_keys = _SubmissionKeys()
        self._report_writer = None
        self._write_failed = False
        self._retry_after = 0.0
        self._stop = threading.Event()
        with _traces_lock:
            _traces.add(self)
        if initial_enabled:
            self.set_enabled(True)

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    def emit(self, event: str, **fields: Any) -> bool:
        if not self.enabled:
            return False
        safe_fields = {
            key: value
            for key, value in fields.items()
            if key not in {"text", "title", "window_title", "audio", "payload", "path"}
        }
        with self._lock:
            if not self.enabled:
                return False
            self._seq += 1
            item = {
                "schema_version": SCHEMA_VERSION,
                "session_id": self.session_id,
                "event_id": uuid.uuid4().hex,
                "seq": self._seq,
                "wall_time": time.time(),
                "monotonic_ms": time.monotonic_ns() // 1_000_000,
                "pid": os.getpid(),
                "thread_id": threading.get_ident(),
                "thread_name": threading.current_thread().name,
                "native_thread_id": threading.get_native_id(),
                "event": str(event),
                **getattr(self._local, "hid_report_fields", {}),
                **safe_fields,
            }
            self._update_gesture_stats_locked(item)
            if self._dropped:
                item["dropped_before"] = self._dropped
            report_writer = self._report_writer
            if report_writer is not None:
                report_writer.submit(item)
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._dropped += 1
                return False
            self._dropped = 0
        return True

    @contextmanager
    def hid_report_context(self, report_id: str):
        """Associate synchronous parsing/actions with their input report only."""
        previous = getattr(self._local, "hid_report_fields", {})
        self._local.hid_report_fields = {"hid_report_id": report_id}
        try:
            yield
        finally:
            self._local.hid_report_fields = previous

    def begin_gesture(
        self,
        button_id: str,
        source: str,
        is_pressed: bool,
        **edge_fields: Any,
    ) -> str:
        if not self.enabled:
            return ""
        summary = None
        with self._lock:
            gesture_id = self._gestures.get(button_id)
            sources = self._gesture_sources.get(button_id, set())
            all_sources = self._gesture_all_sources.get(button_id, set())
            duplicate = source in sources if is_pressed else source not in sources
            if is_pressed and gesture_id is not None and not sources and source in all_sources:
                summary = self._finalize_gesture_locked(button_id)
                gesture_id = None
            if is_pressed and gesture_id is None:
                gesture_id = uuid.uuid4().hex
                self._gestures[button_id] = gesture_id
                self._gesture_sources[button_id] = set()
                self._gesture_all_sources[button_id] = set()
                self._gesture_stats[gesture_id] = {}
            if gesture_id is None:
                gesture_id = self._last_gestures.get(button_id, uuid.uuid4().hex)
            sources = self._gesture_sources.setdefault(button_id, set())
            all_sources = self._gesture_all_sources.setdefault(button_id, set())
            timer = self._gesture_finalize_timers.pop(button_id, None)
            if timer is not None:
                timer.cancel()
            if is_pressed:
                sources.add(str(source))
                all_sources.add(str(source))
            else:
                sources.discard(str(source))
            gesture_finished = not is_pressed and not sources
            if gesture_finished:
                timer = threading.Timer(
                    GESTURE_FINALIZE_DELAY_SECONDS,
                    self._finalize_gesture,
                    args=(str(button_id), gesture_id),
                )
                timer.daemon = True
                self._gesture_finalize_timers[button_id] = timer
                timer.start()
        if summary is not None:
            self._emit_gesture_summary(*summary)
        self.emit(
            "button_edge",
            gesture_id=gesture_id,
            button_id=str(button_id),
            source=str(source),
            edge="down" if is_pressed else "up",
            duplicate=bool(duplicate),
            **edge_fields,
        )
        return gesture_id

    def current_gesture(self, button_id: str) -> Optional[str]:
        if not self.enabled:
            return None
        with self._lock:
            return self._gestures.get(button_id) or self._last_gestures.get(button_id)

    def _finalize_gesture(self, button_id: str, gesture_id: str) -> None:
        with self._lock:
            if self._gestures.get(button_id) != gesture_id:
                return
            if self._gesture_sources.get(button_id):
                return
            summary = self._finalize_gesture_locked(button_id)
        if summary is not None:
            self._emit_gesture_summary(*summary)

    def _finalize_gesture_locked(
        self, button_id: str
    ) -> Optional[tuple[str, str, dict[str, int]]]:
        gesture_id = self._gestures.pop(button_id, None)
        if gesture_id is None:
            return None
        timer = self._gesture_finalize_timers.pop(button_id, None)
        if timer is not None:
            timer.cancel()
        self._gesture_sources.pop(button_id, None)
        self._gesture_all_sources.pop(button_id, None)
        self._last_gestures[button_id] = gesture_id
        stats = self._gesture_stats.pop(gesture_id, {})
        return gesture_id, button_id, stats

    def _emit_gesture_summary(
        self, gesture_id: str, button_id: str, stats: dict[str, int]
    ) -> None:
        self.emit(
            "gesture_summary",
            gesture_id=gesture_id,
            button_id=button_id,
            **stats,
        )

    def _update_gesture_stats_locked(self, item: dict[str, Any]) -> None:
        gesture_id = str(item.get("gesture_id", "") or "")
        event = str(item.get("event", "") or "")
        if not gesture_id or event == "gesture_summary":
            return
        stats = self._gesture_stats.get(gesture_id)
        if stats is None:
            return

        def add(name: str, amount: int = 1) -> None:
            stats[name] = stats.get(name, 0) + int(amount)

        if event == "button_edge":
            source = str(item.get("source", "unknown")).replace("-", "_")
            edge = str(item.get("edge", "unknown"))
            add(f"{source}_{edge}")
            if item.get("duplicate"):
                add("duplicate_edges")
        elif event == "raw_input_event":
            add(f"raw_{item.get('edge', 'unknown')}")
        elif event == "low_level_hook":
            add(f"hook_{item.get('edge', 'unknown')}")
            add(f"hook_{item.get('decision', 'unknown')}")
        elif event == "mapping_trigger":
            add("mapping_triggers")
        elif event == "send_input":
            add("send_input_batches")
            add("send_input_requested", int(item.get("requested", 0)))
            add("send_input_returned", int(item.get("returned", 0)))
            if int(item.get("returned", 0)) != int(item.get("requested", 0)):
                add("send_input_failures")
        elif event == "action_result":
            add("action_calls")
            if not item.get("success", False):
                add("action_failures")
        if item.get("late"):
            add("late_events")

    def begin_attempt(self, gesture_id: Optional[str], provider: str = "") -> str:
        if not self.enabled:
            return ""
        attempt_id = uuid.uuid4().hex
        with self._lock:
            self._attempts[attempt_id] = gesture_id or ""
        self._submission_keys.start(attempt_id, gesture_id, str(provider))
        self.emit(
            "attempt_started",
            attempt_id=attempt_id,
            gesture_id=gesture_id or "",
            provider=str(provider),
        )
        return attempt_id

    def end_attempt(self, attempt_id: Optional[str], result: str, **fields: Any) -> None:
        if not self.enabled or not attempt_id:
            return
        with self._lock:
            gesture_id = self._attempts.pop(attempt_id, "")
        self._submission_keys.finish(attempt_id)
        self.emit(
            "attempt_finished",
            attempt_id=attempt_id,
            gesture_id=gesture_id,
            result=str(result),
            **fields,
        )

    def observe_submission_key(self, vk: int, key_up: bool, flags: int) -> None:
        if not self.enabled:
            return
        item = self._submission_keys.edge(vk, key_up, flags)
        if item is not None:
            self.emit('submission_key_observation', **item)

    def set_current_gesture(self, gesture_id: Optional[str]) -> None:
        self._local.gesture_id = gesture_id or ""

    def set_current_action(self, action: Optional[str]) -> None:
        self._local.action = action or ""

    def current_context(self) -> dict[str, str]:
        if not self.enabled:
            return {"gesture_id": "", "attempt_id": ""}
        gesture_id = str(getattr(self._local, "gesture_id", "") or "")
        attempt_id = ""
        with self._lock:
            if not gesture_id and len(self._attempts) == 1:
                attempt_id, gesture_id = next(iter(self._attempts.items()))
            for candidate, candidate_gesture in self._attempts.items():
                if candidate_gesture == gesture_id:
                    attempt_id = candidate
                    break
        return {
            "gesture_id": gesture_id,
            "attempt_id": attempt_id,
            "action": str(getattr(self._local, "action", "") or ""),
        }

    def record_send_input(
        self,
        *,
        backend: str,
        requested: int,
        returned: int,
        last_error: int = 0,
        events: Optional[list[dict[str, Any]]] = None,
        started_monotonic_ms: Optional[int] = None,
        finished_monotonic_ms: Optional[int] = None,
    ) -> None:
        self.emit(
            "send_input",
            **self.current_context(),
            backend=str(backend),
            requested=int(requested),
            returned=int(returned),
            last_error=int(last_error),
            events=list(events or ()),
            started_monotonic_ms=(
                int(started_monotonic_ms)
                if started_monotonic_ms is not None
                else -1
            ),
            finished_monotonic_ms=(
                int(finished_monotonic_ms)
                if finished_monotonic_ms is not None
                else -1
            ),
        )

    def set_enabled(self, enabled: bool) -> bool:
        """Start or stop the trace writer without restarting the bridge."""

        if not enabled:
            was_enabled = self.enabled
            self.close()
            return was_enabled
        with self._lifecycle_lock:
            enabled = bool(enabled)
            if enabled == self.enabled:
                return False
            trace_queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(
                maxsize=self._queue.maxsize
            )
            report_writer = acquire_fault_report(self.path.parent)
            with self._lock:
                if self._queue is not self._active_queue and not self._queue.empty():
                    # Repeated toggles during a stuck disk call may supersede a
                    # pending session. Keep memory bounded and expose that loss.
                    self._write_failed = True
                self._queue = trace_queue
                self._stop = threading.Event()
                self.session_id = uuid.uuid4().hex
                self._report_writer = report_writer
                self._seq = 0
                self._dropped = 0
                self._gestures.clear()
                self._gesture_sources.clear()
                self._gesture_all_sources.clear()
                self._gesture_stats.clear()
                for timer in self._gesture_finalize_timers.values():
                    timer.cancel()
                self._gesture_finalize_timers.clear()
                self._last_gestures.clear()
                self._attempts.clear()
                self.enabled = True
            if self._thread is not None and self._thread.is_alive():
                # A timed-out close still owns the file. That same worker will
                # switch to the newest queue only after closing the old stream.
                self.emit("session_started")
                return True
            thread = threading.Thread(
                target=self._writer_main,
                args=(trace_queue, self._stop),
                name="remote-mic-diagnostic-trace",
                daemon=True,
            )
            self._thread = thread
            self._active_queue = trace_queue
            try:
                thread.start()
            except RuntimeError:
                release_fault_report(self._report_writer)
                self._report_writer = None
                with self._lock:
                    self.enabled = False
                    if self._thread is thread:
                        self._thread = None
                raise
            self.emit("session_started")
            return True

    def close(self, timeout: float = 1.0) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            if thread is None:
                return
            if not self.enabled:
                report_writer = None
                deadline = time.monotonic() + max(0.0, float(timeout))
            else:
                report_writer, deadline = self._stop_session(timeout)
        # Never hold the lifecycle lock while the writer finishes its handoff.
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        release_fault_report(report_writer, max(0.0, deadline - time.monotonic()))

    def _stop_session(self, timeout):
        # Called under the lifecycle lock; emitters serialize on _lock.
        if self.enabled:
            trace_queue = self._queue
            summaries = []
            with self._lock:
                for button_id in list(self._gestures):
                    summary = self._finalize_gesture_locked(button_id)
                    if summary is not None:
                        summaries.append(summary)
            for summary in summaries:
                self._emit_gesture_summary(*summary)
            self.emit("session_finished", dropped=self.dropped_count)
            with self._lock:
                self.enabled = False
                self._stop.set()
            state = self._file_state
            if state is not None:
                with state.condition:
                    state.condition.notify_all()
            self._submission_keys = _SubmissionKeys()
            deadline = time.monotonic() + max(0.0, float(timeout))
            while True:
                try:
                    trace_queue.put_nowait(None)
                    break
                except queue.Full:
                    # A full diagnostic queue must never make process shutdown
                    # wait indefinitely. Drop one oldest queued record to make
                    # room for the sentinel; the writer will still flush every
                    # record it already dequeued before exiting.
                    try:
                        discarded = trace_queue.get_nowait()
                        if isinstance(discarded, threading.Event):
                            discarded.persisted = False
                            discarded.set()
                        self._write_failed = True
                    except queue.Empty:
                        pass
            report_writer, self._report_writer = self._report_writer, None
            return report_writer, deadline

    def flush(self, timeout: float = 1.0) -> bool:
        with self._lifecycle_lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return not self._write_failed and (not self.enabled or self._queue.empty())
            barrier = threading.Event()
            try:
                self._queue.put_nowait(barrier)
            except queue.Full:
                return False
        return barrier.wait(timeout) and getattr(barrier, "persisted", False)

    def _writer_main(self, trace_queue, stop):
        path = self.path.resolve()
        with _trace_file_states_lock:
            state = _trace_file_states.get(path)
            if state is None:
                state = _trace_file_states[path] = _TraceFileState()
        self._file_state = state
        while True:
            with state.condition:
                # No polling or disk work on the service/UI thread. A closing
                # waiter can leave promptly even if the old disk call is stuck.
                state.condition.wait_for(lambda: state.owner is None or stop.is_set())
                owns_file = state.owner is None
                if owns_file:
                    state.owner = self
            if owns_file:
                try:
                    self._writer_loop(trace_queue, stop)
                finally:
                    with state.condition:
                        state.owner = None
                        state.condition.notify_all()
            else:
                self._write_failed = True
                while True:
                    try:
                        item = trace_queue.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(item, threading.Event):
                        item.persisted = False
                        item.set()
            with self._lifecycle_lock:
                if self._queue is not trace_queue:
                    trace_queue, stop = self._queue, self._stop
                    self._active_queue = trace_queue
                else:
                    self._thread = None
                    self._active_queue = None
                    return

    def _writer_loop(self, trace_queue, stop) -> None:
        stream = None
        pending = 0
        size = 0
        last_flush = time.monotonic()

        def close_stream():
            nonlocal stream, pending
            closing, stream = stream, None
            self._stream = None
            pending = 0
            if closing is not None:
                try:
                    closing.close()
                except OSError:
                    self._write_failed = True

        def failed_write():
            self._write_failed = True
            self._retry_after = time.monotonic() + TRACE_RETRY_SECONDS
            close_stream()

        try:
            from .voice_interaction_diagnostics_windows import ContextTimeline
            timeline = ContextTimeline()
            while True:
                now = time.monotonic()
                if pending and now - last_flush >= TRACE_FLUSH_SECONDS:
                    try:
                        stream.flush()
                    except OSError:
                        failed_write()
                    pending, last_flush = 0, now
                if not stop.is_set():
                    observed = timeline.poll()
                    if observed is not None:
                        # Do not attribute the old session's sample to a new one.
                        with self._lifecycle_lock:
                            if not stop.is_set():
                                self.emit("input_context_observation", **observed)
                try:
                    waits = []
                    if pending:
                        waits.append(max(0.001, TRACE_FLUSH_SECONDS - (time.monotonic() - last_flush)))
                    if not stop.is_set() and timeline.deadline:
                        waits.append(max(0.001, timeline.next_sample - time.monotonic()))
                    item = trace_queue.get(timeout=min(waits) if waits else None)
                except queue.Empty:
                    continue
                if item is None:
                    break
                if isinstance(item, threading.Event):
                    try:
                        if stream is not None:
                            stream.flush()
                    except OSError:
                        failed_write()
                    pending, last_flush = 0, time.monotonic()
                    item.persisted = not self._write_failed
                    item.set()
                    continue
                if time.monotonic() < self._retry_after:
                    continue  # Bounded loss, already marked; never busy-retry disk I/O.
                line = json.dumps(item, ensure_ascii=True, separators=(",", ":")) + "\n"
                line_bytes = len(line.encode("utf-8"))
                if line_bytes > TRACE_MAX_BYTES:
                    self._write_failed = True
                    continue
                try:
                    if stream is None:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        stream = self.path.open("a", encoding="utf-8", newline="\n")
                        self._stream = stream
                        size = self.path.stat().st_size
                    if (size + line_bytes > TRACE_MAX_BYTES
                            or self._file_state.rotation_index is not None):
                        # Any failed rename stops this append. Never grow an
                        # unrotatable file or silently report a successful flush.
                        stream.close()
                        stream = self._stream = None
                        pending = 0
                        self._file_state.rotate(self.path)
                        stream = self.path.open("a", encoding="utf-8", newline="\n")
                        self._stream = stream
                        size = self.path.stat().st_size
                    stream.write(line)
                    size += line_bytes
                    pending += 1
                    if pending >= TRACE_FLUSH_RECORDS or _FaultReport.failed(item):
                        stream.flush()
                        pending, last_flush = 0, time.monotonic()
                except OSError:
                    failed_write()
                timeline.accept(item)
        finally:
            close_stream()
