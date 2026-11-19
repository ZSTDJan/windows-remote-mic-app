"""Bounded observation of owned WinRT cleanup, without discarding live tasks.

Only the authenticated receiver entry may retire its own process. A timeout is
not proof that a native operation or the remote microphone has stopped.
"""
from __future__ import annotations

import asyncio


CLEANUP_TIMEOUT = 3.0
_CANCEL_SETTLE_TIMEOUT = .1


class PendingCleanup(TimeoutError):
    def __init__(self, task):
        super().__init__("cleanup_unconfirmed")
        self.task = task


class RetireWorker(Exception):
    """Native work is pending after the input hook was confirmed detached."""


def _cancel_once(task):
    # A second cancellation can interrupt PyWinRT's native-completion wait and
    # make a dead Task look settled while its native operation is still alive.
    if not task.done() and not getattr(task, "cancelling", lambda: 0)():
        task.cancel()


async def finish_cleanup(task, *, cancel=False):
    """Wait for completion, never await an unbounded cancellation handshake.

    The caller retains the task/resources on PendingCleanup. In particular,
    asyncio.wait_for cannot enforce this deadline: PyWinRT waits for native
    completion even after the task has been cancelled.
    """
    if cancel:
        _cancel_once(task)
    try:
        done, _ = await asyncio.wait((task,), timeout=CLEANUP_TIMEOUT)
    except asyncio.CancelledError:
        if not task.done():
            _cancel_once(task)
            done, _ = await asyncio.wait((task,), timeout=_CANCEL_SETTLE_TIMEOUT)
            if not done:
                raise PendingCleanup(task) from None
        await asyncio.gather(task, return_exceptions=True)
        raise
    if not done:
        _cancel_once(task)
        if not cancel:
            # Ordinary cancellation often settles on the next loop turn. Keep
            # that existing cleanup path, without waiting for a stuck WinRT
            # completion indefinitely.
            done, _ = await asyncio.wait((task,), timeout=_CANCEL_SETTLE_TIMEOUT)
        if not done:
            raise PendingCleanup(task)
        await asyncio.gather(task, return_exceptions=True)
        raise TimeoutError("cleanup_timeout")
    if cancel:
        # Cancellation may finish with an error; it is still settled, and the
        # former gather(return_exceptions=True) contract remains unchanged.
        await asyncio.gather(task, return_exceptions=True)
        return None
    return task.result()
