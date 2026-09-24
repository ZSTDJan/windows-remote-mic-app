import threading
import unittest

from ovb_rc003 import rc003_doubao_session as session


def _snapshot():
    return session.DoubaoAttemptSnapshot(
        ble_generation=3,
        remote_key="a" * 64,
        settings_identity=("doubao", "ralt"),
        tokens=("ralt",),
        endpoint_name="CABLE Input",
        endpoint_host_api="Windows WASAPI",
        send_device_open=True,
        arrival_watermark=17,
    )


class DoubaoSessionCoordinatorTests(unittest.TestCase):
    def test_debt_resolved_before_worker_finish_settles_atomically(self):
        attempt = session.DoubaoAttempt(object(), _snapshot())
        attempt.add_cleanup_debts("hotkey")

        self.assertFalse(attempt.resolve_cleanup_debt("hotkey"))
        self.assertFalse(attempt.settled.is_set())
        attempt.finish("cancelled", cleanup_complete=False)

        self.assertTrue(attempt.settled.is_set())
        self.assertTrue(attempt.cleanup_complete)

    def test_runner_exception_retains_owned_resource_until_release(self):
        coordinator = session.DoubaoSessionCoordinator()
        resource = object()

        def fail_after_acquire(attempt):
            attempt.retain_resource("endpoint", resource)
            raise RuntimeError("failed after acquire")

        attempt = coordinator.begin(_snapshot(), fail_after_acquire)
        self.assertIsNotNone(attempt)
        self.assertTrue(attempt.worker_done.wait(1.0))
        self.assertFalse(attempt.settled.is_set())
        self.assertTrue(coordinator.busy)
        self.assertIsNone(coordinator.begin(_snapshot(), lambda _attempt: None))

        self.assertTrue(attempt.release_resource("endpoint", resource))
        self.assertTrue(coordinator.wait_current(1.0))

    def test_release_cancels_pending_attempt_without_waiting_in_callback(self):
        coordinator = session.DoubaoSessionCoordinator()
        entered = threading.Event()
        release = threading.Event()

        def run(attempt):
            entered.set()
            release.wait(1.0)
            attempt.finish("cancelled", cleanup_complete=True)

        attempt = coordinator.begin(_snapshot(), run)
        self.assertIsNotNone(attempt)
        self.assertTrue(entered.wait(1.0))
        self.assertIs(coordinator.cancel_current(), attempt)
        self.assertTrue(attempt.cancel_event.is_set())
        self.assertFalse(attempt.settled.is_set())
        release.set()
        self.assertTrue(coordinator.wait_current(1.0))

    def test_unsettled_cleanup_blocks_a_new_attempt(self):
        coordinator = session.DoubaoSessionCoordinator()

        first = coordinator.begin(
            _snapshot(),
            lambda attempt: attempt.finish(
                "cleanup_failed",
                cleanup_complete=False,
                cleanup_debts=("endpoint",),
            ),
        )
        self.assertIsNotNone(first)
        self.assertFalse(coordinator.wait_current(1.0))
        self.assertTrue(coordinator.busy)
        self.assertIsNone(coordinator.begin(_snapshot(), lambda _attempt: None))

        first.resolve_cleanup_debt("endpoint")
        self.assertTrue(coordinator.wait_current(1.0))
        self.assertIsNotNone(
            coordinator.begin(
                _snapshot(),
                lambda attempt: attempt.finish(
                    "cancelled", cleanup_complete=True
                ),
            )
        )

    def test_thread_factory_failure_does_not_wedge_the_next_attempt(self):
        calls = 0

        def factory(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("thread unavailable")
            return threading.Thread(**kwargs)

        coordinator = session.DoubaoSessionCoordinator(thread_factory=factory)
        with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
            coordinator.begin(_snapshot(), lambda _attempt: None)

        attempt = coordinator.begin(
            _snapshot(),
            lambda owned: owned.finish("cancelled", cleanup_complete=True),
        )
        self.assertIsNotNone(attempt)
        self.assertTrue(coordinator.wait_current(1.0))

    def test_completed_attempt_allows_the_next_attempt(self):
        coordinator = session.DoubaoSessionCoordinator()
        first = coordinator.begin(
            _snapshot(),
            lambda attempt: attempt.finish("active", cleanup_complete=True),
        )
        self.assertIsNotNone(first)
        self.assertTrue(coordinator.wait_current(1.0))
        second = coordinator.begin(
            _snapshot(),
            lambda attempt: attempt.finish("cancelled", cleanup_complete=True),
        )
        self.assertIsNotNone(second)
        self.assertIsNot(first, second)
        self.assertTrue(coordinator.wait_current(1.0))


if __name__ == "__main__":
    unittest.main()
