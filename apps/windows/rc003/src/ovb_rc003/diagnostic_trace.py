"""Low-overhead, opt-in correlation trace for RC003 input and voice paths.

The normal application log remains human-oriented.  This module writes a
separate JSONL stream only when ``REMOTE_MIC_DIAGNOSTIC_TRACE=1`` (or when a
caller explicitly enables it), and never performs disk I/O on the input
callback thread.  Data is limited to button/action metadata and validated RC003
keyboard report bytes; text, window titles, device paths, and audio are never
accepted by the public helpers.
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
from typing import Any, Optional


SCHEMA_VERSION = 1
TRACE_FILENAME = "diagnostic-trace.jsonl"
TRACE_MAX_BYTES = 5 * 1024 * 1024
TRACE_BACKUP_COUNT = 3
TRACE_QUEUE_SIZE = 2048
GESTURE_FINALIZE_DELAY_SECONDS = 0.25
REPORT_FILENAME = "diagnostic-report.json"
REPORT_MAX_BYTES = 1024 * 1024


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
    """Bounded cross-session incident history, written only by the trace writer."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / REPORT_FILENAME
        self.recent = deque(maxlen=96)
        self.context: dict[str, dict] = {}
        self.incidents: list[dict] = []
        self.last_write = 0.0
        self.dirty = False
        try:
            if self.path.is_file() and self.path.stat().st_size <= REPORT_MAX_BYTES:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if saved.get("schema") == 1 and isinstance(saved.get("incidents"), list):
                    self.incidents = [item for item in saved["incidents"][-3:]
                                      if isinstance(item, dict) and isinstance(item.get("events"), list)
                                      and isinstance(item.get("started_at"), (int, float))
                                      and isinstance(item.get("last_failure_at"), (int, float))
                                      and type(item.get("failure_count")) is int]
        except (OSError, ValueError, AttributeError, RecursionError):
            pass

    @staticmethod
    def failed(item: dict) -> bool:
        result = str(item.get("result", ""))
        return (item.get("success") is False or item.get("verified") is False
                or result.startswith("failed") or result in {"timeout", "host_stop_failed"}
                or str(item.get("status")) in {"failed", "unhealthy", "selected_device_shared_host"}
                or str(item.get("event")) in {"hid_hook_failure", "hid_copy_failure"})

    def accept(self, item: dict) -> None:
        if len(json.dumps(item, ensure_ascii=True)) > 8192:
            item = {key: item[key] for key in ("event", "session_id", "seq", "wall_time",
                    "pid", "thread_id", "success", "verified", "result", "status") if key in item}
            item["report_fields_truncated"] = True
        # Keep sticky context even when a user waits a long time before reproducing.
        if item.get("event") in {"session_started", "device_context", "hid_environment",
                "hid_runtime_capabilities", "hid_copy_host_scope", "hid_source_evidence",
                "voice_attempt_context", "voice_edge_confirmation", "voice_hotkey_control"}:
            self.context[item["event"]] = item
        self.recent.append(item)
        now = float(item.get("wall_time", 0))
        if self.failed(item):
            incident = self.incidents[-1] if self.incidents else None
            if (incident is None or incident.get("session_id") != item.get("session_id")
                    or now - incident.get("started_at", 0) > 30):
                incident = {"session_id": item.get("session_id"), "started_at": now,
                            "last_failure_at": now, "failure_count": 0,
                            "context": dict(self.context), "events": list(self.recent)[:-1]}
                self.incidents.append(incident)
                self.incidents = self.incidents[-3:]
            incident["last_failure_at"] = now
            incident["failure_count"] += 1
            incident["context"] = dict(self.context)
            self.dirty = True
        if self.incidents:
            incident = self.incidents[-1]
            if (incident.get("session_id") == item.get("session_id")
                    and now - incident.get("last_failure_at", 0) <= 10):
                incident["events"].append(item)
                incident["events"] = incident["events"][-192:]
                self.dirty = True
        self.flush()

    def flush(self, *, force: bool = False) -> None:
        if not self.dirty or (not force and time.monotonic() - self.last_write < 0.5):
            return
        payload = {"schema": 1, "updated_at": time.time(), "scope": "diagnostic_events_only",
                   "incidents": self.incidents}
        temporary = self.path.with_suffix(".tmp")
        try:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            while len(encoded) > REPORT_MAX_BYTES:
                if len(self.incidents) > 1:
                    self.incidents.pop(0)
                elif len(self.incidents[0]["events"]) > 1:
                    self.incidents[0]["events"] = self.incidents[0]["events"][::2]
                else:
                    return
                encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            temporary.write_bytes(encoded)
            temporary.replace(self.path)
            self.dirty = False
        except OSError:
            pass  # Logging failures never change device/input behavior.
        finally:
            self.last_write = time.monotonic()


def _truthy(value: object) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


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
        self._stream = None
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
                self._dropped = 0
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
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

        with self._lifecycle_lock:
            enabled = bool(enabled)
            if enabled == self.enabled:
                return False
            if not enabled:
                self.close()
                return True
            trace_queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(
                maxsize=self._queue.maxsize
            )
            self.session_id = uuid.uuid4().hex
            with self._lock:
                self._queue = trace_queue
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
            thread = threading.Thread(
                target=self._writer_loop,
                args=(trace_queue,),
                name="remote-mic-diagnostic-trace",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except RuntimeError:
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
            if not self.enabled or thread is None:
                return
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
            self.enabled = False
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
                        trace_queue.get_nowait()
                    except queue.Empty:
                        pass
                    if time.monotonic() >= deadline:
                        break
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
            if not thread.is_alive() and self._thread is thread:
                self._thread = None

    def _writer_loop(
        self, trace_queue: queue.Queue[Optional[dict[str, Any]]]
    ) -> None:
        stream = None
        report = None
        try:
            from .voice_interaction_diagnostics_windows import ContextTimeline
            timeline = ContextTimeline()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            report = _FaultReport(self.path.parent)
            stream = self.path.open("a", encoding="utf-8")
            self._stream = stream
            while True:
                if self.enabled:
                    observed = timeline.poll()
                    if observed is not None:
                        self.emit("input_context_observation", **observed)
                try:
                    item = trace_queue.get(timeout=0.25)
                except queue.Empty:
                    report.flush()
                    continue
                if item is None:
                    break
                line = json.dumps(item, ensure_ascii=True, separators=(",", ":")) + "\n"
                if stream.tell() + len(line.encode("utf-8")) > TRACE_MAX_BYTES:
                    stream.close()
                    for index in range(TRACE_BACKUP_COUNT - 1, 0, -1):
                        source = self.path.with_name(f"{TRACE_FILENAME}.{index}")
                        target = self.path.with_name(f"{TRACE_FILENAME}.{index + 1}")
                        try:
                            if target.exists():
                                target.unlink()
                            if source.exists():
                                source.replace(target)
                        except OSError:
                            pass
                    try:
                        self.path.replace(self.path.with_name(f"{TRACE_FILENAME}.1"))
                    except OSError:
                        pass
                    stream = self.path.open("a", encoding="utf-8")
                stream.write(line)
                stream.flush()
                report.accept(item)
                timeline.accept(item)
        except OSError:
            return
        finally:
            if report is not None:
                report.flush(force=True)
            if stream is not None:
                stream.close()
            self._stream = None
