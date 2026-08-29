"""Bounded FIFO worker for blocking audio playback writes."""

from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time
from typing import Callable, Optional, Sequence


DEFAULT_MAX_PENDING_FRAMES = 64
DEFAULT_FLUSH_TIMEOUT_SECONDS = 5.0
DEFAULT_STOP_TIMEOUT_SECONDS = 5.0


class PlaybackWorkerError(RuntimeError):
    """Base class for playback worker failures."""


class PlaybackBackpressureError(PlaybackWorkerError):
    """Raised when the bounded PCM queue cannot accept another frame."""


class PlaybackFlushTimeoutError(PlaybackWorkerError):
    """Raised when a FIFO flush barrier does not complete in time."""


@dataclass(frozen=True)
class PlaybackFlushResult:
    completed: bool
    error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.completed and self.error is None


@dataclass
class _QueuedFrame:
    samples: tuple
    enqueued_at: float


@dataclass
class _FlushBarrier:
    completed: threading.Event
    error: Optional[BaseException] = None


_STOP = object()


class PlaybackWriteWorker:
    """Serializes blocking writes without blocking the BLE decode worker."""

    def __init__(
        self,
        write: Callable[[Sequence[int]], None],
        on_error: Callable[[BaseException], None],
        *,
        max_pending_frames: int = DEFAULT_MAX_PENDING_FRAMES,
        thread_name: str = "audio-playback-write",
    ) -> None:
        if max_pending_frames <= 0:
            raise ValueError("max_pending_frames must be positive")
        self._write = write
        self._on_error = on_error
        self._queue: "queue.Queue[object]" = queue.Queue(
            maxsize=max_pending_frames
        )
        self._thread = threading.Thread(
            target=self._run,
            name=thread_name,
            daemon=True,
        )
        self._state_lock = threading.Lock()
        self._failure: Optional[BaseException] = None
        self._started = False
        self._stopping = False

    @property
    def failure(self) -> Optional[BaseException]:
        with self._state_lock:
            return self._failure

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                return
            if self._stopping:
                raise PlaybackWorkerError("playback worker is stopping")
            self._started = True
        try:
            self._thread.start()
        except BaseException:
            with self._state_lock:
                self._started = False
            raise

    def submit(self, samples: Sequence[int]) -> bool:
        with self._state_lock:
            if not self._started or self._stopping or self._failure is not None:
                return False
        item = _QueuedFrame(tuple(samples), time.monotonic())
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            pending, oldest_ms = self._queue_metrics()
            self._record_failure(
                PlaybackBackpressureError(
                    "audio playback queue full: "
                    f"pending={pending} oldest_ms={oldest_ms:.1f}"
                )
            )
            return False

    def flush(
        self,
        timeout: float = DEFAULT_FLUSH_TIMEOUT_SECONDS,
    ) -> PlaybackFlushResult:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._state_lock:
            if not self._started or self._stopping:
                return PlaybackFlushResult(False, self._failure)
        barrier = _FlushBarrier(threading.Event())
        remaining = max(0.0, deadline - time.monotonic())
        try:
            self._queue.put(barrier, timeout=remaining)
        except queue.Full:
            error = PlaybackFlushTimeoutError(
                "audio playback flush barrier could not enter the queue"
            )
            self._record_failure(error)
            return PlaybackFlushResult(False, error)
        remaining = max(0.0, deadline - time.monotonic())
        if not barrier.completed.wait(remaining):
            error = PlaybackFlushTimeoutError(
                "audio playback flush barrier timed out"
            )
            self._record_failure(error)
            return PlaybackFlushResult(False, error)
        return PlaybackFlushResult(True, barrier.error)

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._state_lock:
            if not self._started:
                self._stopping = True
                return True
            if not self._thread.is_alive():
                self._stopping = True
                return True
            self._stopping = True
        remaining = max(0.0, deadline - time.monotonic())
        try:
            self._queue.put(_STOP, timeout=remaining)
        except queue.Full:
            return False
        remaining = max(0.0, deadline - time.monotonic())
        self._thread.join(timeout=remaining)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                if isinstance(item, _FlushBarrier):
                    item.error = self.failure
                    item.completed.set()
                    continue
                if not isinstance(item, _QueuedFrame):
                    continue
                if self.failure is not None:
                    continue
                try:
                    self._write(item.samples)
                except BaseException as exc:  # noqa: BLE001 - fail worker closed
                    self._record_failure(exc)
            finally:
                self._queue.task_done()

    def _record_failure(self, error: BaseException) -> None:
        with self._state_lock:
            if self._failure is not None:
                return
            self._failure = error
        try:
            self._on_error(error)
        except Exception:
            pass

    def _queue_metrics(self) -> tuple[int, float]:
        with self._queue.mutex:
            items = list(self._queue.queue)
        oldest = next(
            (item for item in items if isinstance(item, _QueuedFrame)),
            None,
        )
        oldest_ms = (
            max(0.0, time.monotonic() - oldest.enqueued_at) * 1000.0
            if oldest is not None
            else 0.0
        )
        return len(items), oldest_ms
