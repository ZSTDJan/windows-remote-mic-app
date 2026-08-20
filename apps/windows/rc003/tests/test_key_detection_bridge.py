import tempfile
import threading
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
