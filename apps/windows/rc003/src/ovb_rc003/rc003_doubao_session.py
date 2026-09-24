"""Attempt ownership for Xiaomi RC003 -> Doubao cold-start voice sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Callable, Optional


PREPARING = "preparing"
DISPATCHING = "dispatching"
WAITING_HOST = "waiting_host"
ACTIVE = "active"
CANCELLING = "cancelling"
FINISHED = "finished"
FAILED = "failed"


@dataclass(frozen=True)
class DoubaoAttemptSnapshot:
    ble_generation: int
    remote_key: str
    settings_identity: tuple[object, ...]
    tokens: tuple[str, ...]
    endpoint_name: str
    endpoint_host_api: str
    send_device_open: bool
    arrival_watermark: int


@dataclass
class DoubaoAttempt:
    token: object
    snapshot: DoubaoAttemptSnapshot
    cancel_event: threading.Event = field(default_factory=threading.Event)
    worker_done: threading.Event = field(default_factory=threading.Event)
    settled: threading.Event = field(default_factory=threading.Event)
    phase: str = PREPARING
    outcome: str = "pending"
    dispatch_started: bool = False
    cleanup_complete: bool = False
    error_type: str = ""
    physicalizer_generation: Optional[int] = None
    physicalizer_pid: Optional[int] = None
    cleanup_debts: set[str] = field(default_factory=set)
    resources: dict[str, object] = field(default_factory=dict, repr=False)
    cleanup_worker_running: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _refresh_settlement_locked(self) -> bool:
        self.cleanup_complete = bool(
            self.worker_done.is_set()
            and not self.cleanup_debts
            and not self.resources
            and not self.cleanup_worker_running
        )
        if self.cleanup_complete:
            self.phase = FINISHED
            self.settled.set()
            return True
        return False

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def transition(self, phase: str) -> bool:
        with self._lock:
            if self.settled.is_set():
                return False
            self.phase = str(phase)
            return True

    def mark_dispatch_started(self) -> None:
        with self._lock:
            self.dispatch_started = True
            self.phase = DISPATCHING

    def mark_active(self) -> None:
        with self._lock:
            if not self.settled.is_set():
                self.outcome = "active"
                self.phase = ACTIVE
                self.cleanup_debts.add("active_session")
                self.cleanup_complete = False

    def is_active(self) -> bool:
        with self._lock:
            return self.outcome == "active" and not self.settled.is_set()

    def bind_physicalizer(self, generation: int, pid: int) -> None:
        with self._lock:
            self.physicalizer_generation = int(generation)
            self.physicalizer_pid = int(pid)

    def target_identity(self) -> tuple[Optional[int], Optional[int]]:
        with self._lock:
            return self.physicalizer_generation, self.physicalizer_pid

    def retain_resource(self, name: str, value: object) -> None:
        with self._lock:
            self.resources[str(name)] = value
            self.cleanup_complete = False

    def resource(self, name: str) -> Optional[object]:
        with self._lock:
            return self.resources.get(str(name))

    def release_resource(self, name: str, value: object) -> bool:
        with self._lock:
            key = str(name)
            if self.resources.get(key) is not value:
                return False
            self.resources.pop(key, None)
            self._refresh_settlement_locked()
            return True

    def add_cleanup_debts(self, *names: str) -> None:
        with self._lock:
            self.cleanup_debts.update(str(name) for name in names if name)
            self.cleanup_complete = False

    def has_cleanup_debt(self, name: str) -> bool:
        with self._lock:
            return str(name) in self.cleanup_debts

    def cleanup_debt_snapshot(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self.cleanup_debts)

    def replace_cleanup_debt(self, old: str, *new: str) -> None:
        with self._lock:
            self.cleanup_debts.discard(str(old))
            self.cleanup_debts.update(str(name) for name in new if name)
            self._refresh_settlement_locked()

    def resolve_cleanup_debt(self, name: str) -> bool:
        with self._lock:
            self.cleanup_debts.discard(str(name))
            return self._refresh_settlement_locked()

    def begin_cleanup_worker(self) -> bool:
        with self._lock:
            if self.settled.is_set() or self.cleanup_worker_running:
                return False
            self.cleanup_worker_running = True
            self.cleanup_complete = False
            self.phase = CANCELLING
            return True

    def end_cleanup_worker(self) -> None:
        with self._lock:
            self.cleanup_worker_running = False
            self._refresh_settlement_locked()

    def finish(
        self,
        outcome: str,
        *,
        cleanup_complete: Optional[bool] = None,
        error_type: str = "",
        cleanup_debts: tuple[str, ...] = (),
    ) -> None:
        with self._lock:
            if self.worker_done.is_set():
                return
            self.outcome = str(outcome)
            self.error_type = str(error_type)
            self.cleanup_debts.update(str(name) for name in cleanup_debts if name)
            self.worker_done.set()
            if not self._refresh_settlement_locked():
                self.phase = FAILED if outcome != "active" else ACTIVE


class DoubaoSessionCoordinator:
    """Serialize one attempt without making input callbacks wait for it."""

    def __init__(self, *, thread_factory=threading.Thread) -> None:
        self._thread_factory = thread_factory
        self._lock = threading.Lock()
        self._current: Optional[DoubaoAttempt] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def current(self) -> Optional[DoubaoAttempt]:
        with self._lock:
            return self._current

    @property
    def busy(self) -> bool:
        with self._lock:
            attempt = self._current
            return attempt is not None and (
                not attempt.settled.is_set() or not attempt.cleanup_complete
            )

    def is_current(self, attempt: DoubaoAttempt) -> bool:
        with self._lock:
            return self._current is attempt

    def begin(
        self,
        snapshot: DoubaoAttemptSnapshot,
        runner: Callable[[DoubaoAttempt], None],
    ) -> Optional[DoubaoAttempt]:
        with self._lock:
            current = self._current
            if current is not None and (
                not current.settled.is_set() or not current.cleanup_complete
            ):
                return None
            attempt = DoubaoAttempt(object(), snapshot)
            self._current = attempt

            def run() -> None:
                try:
                    runner(attempt)
                except BaseException as exc:  # owner must become observable
                    attempt.finish(
                        "failed",
                        error_type=type(exc).__name__,
                    )
                finally:
                    if not attempt.worker_done.is_set():
                        attempt.finish("failed")
            try:
                thread = self._thread_factory(
                    target=run,
                    name="rc003-doubao-start",
                    daemon=True,
                )
                self._thread = thread
            except BaseException:
                attempt.finish("failed")
                raise
        try:
            thread.start()
        except BaseException:
            attempt.finish("failed")
            raise
        return attempt

    def request_cleanup(
        self,
        attempt: DoubaoAttempt,
        runner: Callable[[DoubaoAttempt], None],
    ) -> bool:
        with self._lock:
            if self._current is not attempt or not attempt.begin_cleanup_worker():
                return False

            def run() -> None:
                try:
                    runner(attempt)
                finally:
                    attempt.end_cleanup_worker()

            try:
                thread = self._thread_factory(
                    target=run,
                    name="rc003-doubao-cleanup",
                    daemon=True,
                )
                self._thread = thread
            except BaseException:
                attempt.end_cleanup_worker()
                return False
        try:
            thread.start()
        except BaseException:
            attempt.end_cleanup_worker()
            return False
        return True

    def cancel_current(self) -> Optional[DoubaoAttempt]:
        with self._lock:
            attempt = self._current
            if attempt is not None and not attempt.settled.is_set():
                attempt.cancel_event.set()
                attempt.transition(CANCELLING)
            return attempt

    def wait_current(self, timeout: float) -> bool:
        attempt = self.current
        if attempt is None:
            return True
        if not attempt.settled.wait(max(0.0, float(timeout))):
            return False
        return attempt.cleanup_complete
