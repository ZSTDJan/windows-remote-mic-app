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
        identity = bridge_runtime_status.current_runtime_identity(
            "test-version",
            frozen=False,
            source_root=self.root,
        )
        written = bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
            pid=1234,
            identity=identity,
            raw_input_state="ready",
            hid_tap_state="unavailable",
            last_button_at=40.0,
            last_button_source="hid",
            voice_active=True,
            now=lambda: 42.5,
        )

        self.assertEqual(
            written.state,
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
        )
        self.assertEqual(written.schema, bridge_runtime_status.SCHEMA_VERSION)
        self.assertEqual(written.runtime_id, identity.runtime_id)
        self.assertTrue(written.voice_active)
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
        self.assertNotIn("text", payload)
        self.assertFalse(payload["voice_active"])

    def test_schema_one_status_remains_readable_during_upgrade(self):
        path = bridge_runtime_status.status_path(self.root)
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "state": "connected",
                    "pid": 1234,
                    "updated_at": 42.5,
                }
            ),
            encoding="utf-8",
        )

        status = bridge_runtime_status.read_status(self.root)

        self.assertIsNotNone(status)
        self.assertEqual(status.schema, 1)
        self.assertEqual(status.runtime_id, "")
        self.assertFalse(status.voice_active)

    def test_runtime_identity_distinguishes_package_roots_without_exposing_them(self):
        first = bridge_runtime_status.current_runtime_identity(
            "1.2.3",
            frozen=True,
            executable=str(self.root / "first" / "RemoteMicRC003.exe"),
        )
        second = bridge_runtime_status.current_runtime_identity(
            "1.2.3",
            frozen=True,
            executable=str(self.root / "second" / "RemoteMicRC003.exe"),
        )

        self.assertNotEqual(first.runtime_id, second.runtime_id)
        self.assertNotIn(str(self.root), first.runtime_id)

    def test_health_helpers_do_not_treat_expected_unavailability_as_crash(self):
        identity = bridge_runtime_status.current_runtime_identity(
            "1.2.3", frozen=False, source_root=self.root
        )
        status = bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=1234,
            identity=identity,
            raw_input_state="unavailable",
            hid_tap_state="disabled",
        )

        self.assertTrue(
            bridge_runtime_status.runtime_identity_matches(status, identity)
        )
        self.assertFalse(bridge_runtime_status.input_channels_failed(status))

    def test_health_helpers_detect_two_explicitly_failed_button_channels(self):
        identity = bridge_runtime_status.current_runtime_identity(
            "1.2.3", frozen=False, source_root=self.root
        )
        status = bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=1234,
            identity=identity,
            raw_input_state="failed",
            hid_tap_state="unhealthy",
        )

        self.assertTrue(bridge_runtime_status.input_channels_failed(status))

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
