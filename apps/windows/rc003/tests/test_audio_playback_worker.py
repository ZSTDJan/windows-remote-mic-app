import queue
import threading
import time
import unittest

from ovb_rc003 import audio_playback_worker


class PlaybackWriteWorkerTests(unittest.TestCase):
    def test_submit_does_not_wait_for_a_blocking_write(self):
        write_started = threading.Event()
        release_write = threading.Event()
        failures = []

        def write(_samples):
            write_started.set()
            release_write.wait(2.0)

        worker = audio_playback_worker.PlaybackWriteWorker(write, failures.append)
        worker.start()
        started = time.monotonic()
        self.assertTrue(worker.submit([1, 2, 3]))
        elapsed = time.monotonic() - started
        self.assertTrue(write_started.wait(1.0))
        self.assertLess(elapsed, 0.1)
        release_write.set()
        self.assertTrue(worker.flush(1.0).ok)
        self.assertTrue(worker.stop(1.0))
        self.assertEqual(failures, [])

    def test_flush_barrier_preserves_fifo_order(self):
        writes = []
        worker = audio_playback_worker.PlaybackWriteWorker(
            lambda samples: writes.append(tuple(samples)),
            lambda _error: None,
        )
        worker.start()
        self.assertTrue(worker.submit([1]))
        self.assertTrue(worker.submit([2]))

        result = worker.flush(1.0)

        self.assertTrue(result.ok)
        self.assertEqual(writes, [(1,), (2,)])
        self.assertTrue(worker.stop(1.0))

    def test_two_flush_cycles_keep_consecutive_sessions_separate(self):
        writes = []
        worker = audio_playback_worker.PlaybackWriteWorker(
            lambda samples: writes.append(tuple(samples)),
            lambda _error: None,
        )
        worker.start()
        self.assertTrue(worker.submit([1]))
        self.assertTrue(worker.flush(1.0).ok)
        self.assertEqual(writes, [(1,)])

        self.assertTrue(worker.submit([2]))
        self.assertTrue(worker.flush(1.0).ok)

        self.assertEqual(writes, [(1,), (2,)])
        self.assertTrue(worker.stop(1.0))

    def test_write_failure_is_reported_and_reaches_the_barrier(self):
        failure_seen = threading.Event()
        failures = []

        def write(_samples):
            raise OSError("write failed")

        def on_error(error):
            failures.append(error)
            failure_seen.set()

        worker = audio_playback_worker.PlaybackWriteWorker(write, on_error)
        worker.start()
        self.assertTrue(worker.submit([1]))
        self.assertTrue(failure_seen.wait(1.0))

        result = worker.flush(1.0)

        self.assertTrue(result.completed)
        self.assertIsInstance(result.error, OSError)
        self.assertFalse(worker.submit([2]))
        self.assertTrue(worker.stop(1.0))
        self.assertEqual(len(failures), 1)

    def test_full_queue_fails_closed_instead_of_growing(self):
        write_started = threading.Event()
        release_write = threading.Event()
        failures = []

        def write(_samples):
            write_started.set()
            release_write.wait(2.0)

        worker = audio_playback_worker.PlaybackWriteWorker(
            write,
            failures.append,
            max_pending_frames=1,
        )
        worker.start()
        self.assertTrue(worker.submit([1]))
        self.assertTrue(write_started.wait(1.0))
        self.assertTrue(worker.submit([2]))
        self.assertFalse(worker.submit([3]))
        self.assertEqual(worker.pending_count, 1)
        self.assertIsInstance(
            failures[0],
            audio_playback_worker.PlaybackBackpressureError,
        )

        release_write.set()
        result = worker.flush(1.0)
        self.assertTrue(result.completed)
        self.assertIsInstance(
            result.error,
            audio_playback_worker.PlaybackBackpressureError,
        )
        self.assertTrue(worker.stop(1.0))

    def test_blocked_serial_control_callback_releases_a_burst_into_bounded_writer(self):
        """Characterize the incident mechanism without claiming exact log inventory.

        The BLE transport dispatches control and audio on one serial worker.  A
        slow control callback can therefore leave audio notifications queued;
        once it returns, those notifications reach the playback writer as a
        burst.  This diagnostic harness deliberately keeps the playback sink
        paced/blocked and proves that the burst can exceed a bounded queue.
        """

        incoming = queue.Queue()
        prepare_started = threading.Event()
        release_prepare = threading.Event()
        write_started = threading.Event()
        release_write = threading.Event()
        dispatch_done = threading.Event()
        accepted = []
        failures = []
        dispatch_errors = []

        def write(_samples):
            write_started.set()
            release_write.wait(2.0)

        playback = audio_playback_worker.PlaybackWriteWorker(
            write,
            failures.append,
            max_pending_frames=2,
        )
        playback.start()

        def dispatch_serially():
            try:
                while True:
                    kind, value = incoming.get()
                    if kind == "stop":
                        return
                    if kind == "control":
                        prepare_started.set()
                        if not release_prepare.wait(1.0):
                            raise AssertionError("preparation was not released")
                        continue
                    accepted.append(playback.submit([value]))
                    if value == 1 and not write_started.wait(1.0):
                        raise AssertionError("paced playback write did not start")
            except BaseException as exc:  # Surface worker failures in the test thread.
                dispatch_errors.append(exc)
            finally:
                dispatch_done.set()

        dispatcher = threading.Thread(target=dispatch_serially, daemon=True)
        dispatcher.start()
        try:
            incoming.put(("control", None))
            self.assertTrue(prepare_started.wait(1.0))
            for frame in range(1, 5):
                incoming.put(("audio", frame))
            self.assertEqual(incoming.qsize(), 4)

            release_prepare.set()
            incoming.put(("stop", None))
            self.assertTrue(dispatch_done.wait(1.0))
            self.assertEqual(dispatch_errors, [])
            self.assertEqual(accepted, [True, True, True, False])
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(
                failures[0],
                audio_playback_worker.PlaybackBackpressureError,
            )
        finally:
            release_prepare.set()
            release_write.set()
            incoming.put(("stop", None))
            dispatcher.join(1.0)
            playback.stop(1.0)

    def test_stop_is_bounded_when_write_does_not_return(self):
        write_started = threading.Event()
        release_write = threading.Event()

        def write(_samples):
            write_started.set()
            release_write.wait(2.0)

        worker = audio_playback_worker.PlaybackWriteWorker(
            write,
            lambda _error: None,
        )
        worker.start()
        self.assertTrue(worker.submit([1]))
        self.assertTrue(write_started.wait(1.0))

        started = time.monotonic()
        self.assertFalse(worker.stop(0.05))
        self.assertLess(time.monotonic() - started, 0.2)
        release_write.set()
        worker._thread.join(1.0)
        self.assertFalse(worker.is_alive)


if __name__ == "__main__":
    unittest.main()
