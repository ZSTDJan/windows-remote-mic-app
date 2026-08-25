import json
import tempfile
import unittest
from pathlib import Path

from ovb_rc003 import bridge_runtime_status


class BridgeRuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_publish_and_read_round_trip_waiting_state(self):
        written = bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
            pid=1234,
            now=lambda: 42.5,
        )

        self.assertEqual(
            written.state,
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
        )
        self.assertEqual(bridge_runtime_status.read_status(self.root), written)

    def test_publish_replaces_the_file_with_connected_state(self):
        bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
            pid=1234,
        )
        written = bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=1234,
            now=lambda: 84.0,
        )

        self.assertEqual(bridge_runtime_status.read_status(self.root), written)
        payload = json.loads(
            bridge_runtime_status.status_path(self.root).read_text(encoding="utf-8")
        )
        self.assertEqual(payload["state"], "connected")
        self.assertNotIn("device", payload)
        self.assertNotIn("address", payload)
        self.assertNotIn("voice", payload)

    def test_invalid_or_partial_file_is_treated_as_unknown(self):
        path = bridge_runtime_status.status_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema": 1, "state": "connected"}', encoding="utf-8")

        self.assertIsNone(bridge_runtime_status.read_status(self.root))

    def test_clear_with_wrong_pid_does_not_remove_newer_process_status(self):
        bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=2222,
        )

        self.assertFalse(bridge_runtime_status.clear_status(self.root, pid=1111))
        self.assertIsNotNone(bridge_runtime_status.read_status(self.root))
        self.assertTrue(bridge_runtime_status.clear_status(self.root, pid=2222))
        self.assertIsNone(bridge_runtime_status.read_status(self.root))

    def test_non_positive_pid_is_rejected(self):
        with self.assertRaises(ValueError):
            bridge_runtime_status.publish_status(
                self.root,
                bridge_runtime_status.BridgeConnectionState.CONNECTED,
                pid=0,
            )


if __name__ == "__main__":
    unittest.main()
