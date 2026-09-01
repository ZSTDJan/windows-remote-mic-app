import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import key_detection_bridge


class KeyDetectionBridgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_request_publish_poll_and_cancel_are_one_shot(self):
        request = key_detection_bridge.request_detection(self.root)

        self.assertTrue(key_detection_bridge.has_pending_request(self.root))
        self.assertTrue(
            key_detection_bridge.publish_next_button(self.root, "volume_up")
        )
        self.assertFalse(key_detection_bridge.has_pending_request(self.root))
        self.assertEqual(
            key_detection_bridge.poll_detection(request),
            "volume_up",
        )
        self.assertFalse(
            key_detection_bridge.publish_next_button(self.root, "volume_down")
        )

        key_detection_bridge.cancel_detection(request)
        self.assertIsNone(key_detection_bridge.poll_detection(request))

    def test_only_one_concurrent_button_can_claim_a_request(self):
        request = key_detection_bridge.request_detection(self.root)
        barrier = threading.Barrier(3)
        results = []

        def publish(button_id):
            barrier.wait()
            results.append(
                key_detection_bridge.publish_next_button(self.root, button_id)
            )

        threads = [
            threading.Thread(target=publish, args=("left",)),
            threading.Thread(target=publish, args=("right",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5.0)

        self.assertEqual(sorted(results), [False, True])
        self.assertIn(
            key_detection_bridge.poll_detection(request),
            {"left", "right"},
        )

    def test_request_is_read_only_after_the_claim_lock_is_owned(self):
        request = key_detection_bridge.request_detection(self.root)
        first_reader_entered = threading.Event()
        release_first_reader = threading.Event()
        read_calls = []
        results = []
        original = key_detection_bridge._read_request

        def blocked_read(path, *, now):
            read_calls.append(path)
            first_reader_entered.set()
            self.assertTrue(release_first_reader.wait(timeout=5.0))
            return original(path, now=now)

        with mock.patch.object(
            key_detection_bridge,
            "_read_request",
            side_effect=blocked_read,
        ):
            first = threading.Thread(
                target=lambda: results.append(
                    key_detection_bridge.publish_next_button(self.root, "left")
                )
            )
            second = threading.Thread(
                target=lambda: results.append(
                    key_detection_bridge.publish_next_button(self.root, "right")
                )
            )
            first.start()
            self.assertTrue(first_reader_entered.wait(timeout=5.0))
            second.start()
            second.join(timeout=5.0)
            self.assertFalse(second.is_alive())
            release_first_reader.set()
            first.join(timeout=5.0)

        self.assertEqual(len(read_calls), 1)
        self.assertEqual(sorted(results), [False, True])
        self.assertIn(
            key_detection_bridge.poll_detection(request),
            {"left", "right"},
        )

    def test_stale_request_is_removed_and_never_claimed(self):
        request = key_detection_bridge.request_detection(
            self.root,
            now=lambda: 100.0,
        )

        self.assertFalse(
            key_detection_bridge.publish_next_button(
                self.root,
                "back",
                now=lambda: 100.0 + key_detection_bridge.STALE_AFTER_SECONDS + 1.0,
            )
        )
        self.assertFalse(request.request_path.exists())

    def test_result_contains_only_protocol_fields_and_logical_button(self):
        request = key_detection_bridge.request_detection(self.root)
        self.assertTrue(key_detection_bridge.publish_next_button(self.root, "ok"))
        result_text = request.result_path.read_text(encoding="utf-8")

        self.assertIn('"button_id": "ok"', result_text)
        self.assertNotIn("bluetooth", result_text.casefold())
        self.assertNotIn("device", result_text.casefold())

    def test_publish_claims_the_oldest_request_not_uuid_name_order(self):
        first = key_detection_bridge.request_detection(self.root)
        second = key_detection_bridge.request_detection(self.root)
        now_ns = time.time_ns()
        os.utime(first.request_path, ns=(now_ns - 2_000_000_000,) * 2)
        os.utime(second.request_path, ns=(now_ns - 1_000_000_000,) * 2)

        self.assertTrue(key_detection_bridge.publish_next_button(self.root, "left"))

        self.assertEqual(key_detection_bridge.poll_detection(first), "left")
        self.assertIsNone(key_detection_bridge.poll_detection(second))

    def test_atomic_write_failure_removes_the_temporary_file(self):
        target = self.root / "result.json"

        with mock.patch.object(
            key_detection_bridge.os,
            "replace",
            side_effect=OSError("simulated replace failure"),
        ), self.assertRaises(OSError):
            key_detection_bridge._write_json_atomic(target, {"schema": 1})

        self.assertEqual(list(self.root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
