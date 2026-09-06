import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            voice_key_physicalizer_state="recovering",
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
        self.assertEqual(written.voice_key_physicalizer_state, "recovering")
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

    def test_explicit_windows_sessions_keep_independent_status_files(self):
        first = bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=1111,
            session_id=1,
        )
        second = bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
            pid=2222,
            session_id=2,
        )

        self.assertNotEqual(
            bridge_runtime_status.status_path(self.root, session_id=1),
            bridge_runtime_status.status_path(self.root, session_id=2),
        )
        self.assertEqual(
            bridge_runtime_status.read_status(self.root, session_id=1), first
        )
        self.assertEqual(
            bridge_runtime_status.read_status(self.root, session_id=2), second
        )
        self.assertTrue(
            bridge_runtime_status.clear_status(
                self.root,
                pid=2222,
                session_id=2,
            )
        )
        self.assertEqual(
            bridge_runtime_status.read_status(self.root, session_id=1), first
        )

    def test_windows_session_query_failure_uses_a_process_scoped_file(self):
        with mock.patch.object(
            bridge_runtime_status.sys,
            "platform",
            "win32",
        ), mock.patch.object(
            bridge_runtime_status,
            "_default_status_scope",
            bridge_runtime_status._STATUS_SCOPE_UNSET,
        ), mock.patch(
            "ovb_rc003.single_instance.current_process_session_id",
            side_effect=OSError("session unavailable"),
        ):
            path = bridge_runtime_status.status_path(self.root)

        self.assertEqual(
            path.name,
            f"bridge-runtime-status-p{os.getpid()}.json",
        )

    def test_default_windows_scope_stays_fixed_after_query_result_changes(self):
        for initial, later, expected in (
            (7, OSError("later failure"), "bridge-runtime-status-s7.json"),
            (
                OSError("initial failure"),
                7,
                f"bridge-runtime-status-p{os.getpid()}.json",
            ),
        ):
            with self.subTest(initial=initial), mock.patch.object(
                bridge_runtime_status.sys,
                "platform",
                "win32",
            ), mock.patch.object(
                bridge_runtime_status,
                "_default_status_scope",
                bridge_runtime_status._STATUS_SCOPE_UNSET,
            ), mock.patch(
                "ovb_rc003.single_instance.current_process_session_id",
                side_effect=(initial, later),
            ) as session_query:
                first = bridge_runtime_status.status_path(self.root)
                second = bridge_runtime_status.status_path(self.root)

            self.assertEqual(first, second)
            self.assertEqual(first.name, expected)
            self.assertEqual(session_query.call_count, 1)

    def test_windows_session_never_falls_back_to_the_legacy_shared_file(self):
        legacy_path = self.root / bridge_runtime_status.STATUS_FILENAME
        legacy_path.write_text(
            json.dumps(
                {
                    "schema": bridge_runtime_status.SCHEMA_VERSION,
                    "state": "connected",
                    "pid": 1111,
                    "updated_at": 1.0,
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            bridge_runtime_status.sys,
            "platform",
            "win32",
        ), mock.patch.object(
            bridge_runtime_status,
            "_default_status_scope",
            bridge_runtime_status._STATUS_SCOPE_UNSET,
        ), mock.patch(
            "ovb_rc003.single_instance.current_process_session_id",
            return_value=2,
        ):
            self.assertIsNone(bridge_runtime_status.read_status(self.root))
            self.assertFalse(bridge_runtime_status.clear_status(self.root))

        self.assertTrue(legacy_path.exists())

    def test_negative_or_boolean_session_id_is_rejected(self):
        for session_id in (-1, True):
            with self.subTest(session_id=session_id), self.assertRaises(
                ValueError
            ):
                bridge_runtime_status.status_path(
                    self.root,
                    session_id=session_id,
                )

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
        self.assertEqual(status.voice_key_physicalizer_state, "unknown")

    def test_existing_schema_two_status_defaults_new_channel_to_unknown(self):
        path = bridge_runtime_status.status_path(self.root)
        path.write_text(
            json.dumps(
                {
                    "schema": bridge_runtime_status.SCHEMA_VERSION,
                    "state": "connected",
                    "pid": 1234,
                    "updated_at": 42.5,
                    "app_version": "old",
                    "runtime_kind": "frozen",
                    "package_name": "old-package",
                    "runtime_id": "old-id",
                    "raw_input_state": "ready",
                    "hid_tap_state": "ready",
                    "last_button_source": "",
                    "voice_active": False,
                }
            ),
            encoding="utf-8",
        )

        status = bridge_runtime_status.read_status(self.root)

        self.assertIsNotNone(status)
        self.assertEqual(status.voice_key_physicalizer_state, "unknown")

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

    def test_health_helpers_treat_two_terminally_unavailable_channels_as_failed(self):
        identity = bridge_runtime_status.current_runtime_identity(
            "1.2.3", frozen=False, source_root=self.root
        )
        status = bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=1234,
            identity=identity,
            raw_input_state="unavailable",
            hid_tap_state="unavailable_gadget_not_verified",
        )

        self.assertTrue(
            bridge_runtime_status.runtime_identity_matches(status, identity)
        )
        self.assertTrue(bridge_runtime_status.input_channels_failed(status))
        self.assertFalse(bridge_runtime_status.input_channels_ready(status))

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

    def test_any_ready_input_channel_allows_detection(self):
        raw_ready = bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=1234,
            raw_input_state="ready",
            hid_tap_state="unavailable_gadget_not_verified",
        )
        self.assertTrue(bridge_runtime_status.input_channels_ready(raw_ready))

        tap_ready = bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=1234,
            raw_input_state="failed",
            hid_tap_state="attached_waiting_for_hid_io",
        )
        self.assertTrue(bridge_runtime_status.input_channels_ready(tap_ready))

    def test_legacy_status_cannot_claim_a_ready_input_channel(self):
        status = bridge_runtime_status.BridgeRuntimeStatus(
            schema=bridge_runtime_status.LEGACY_SCHEMA_VERSION,
            state=bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=1234,
            updated_at=1.0,
            raw_input_state="ready",
        )

        self.assertFalse(bridge_runtime_status.input_channels_ready(status))

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

    def test_clear_does_not_delete_status_replaced_after_atomic_claim(self):
        bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=1111,
        )
        original_read = bridge_runtime_status._read_status_file

        def read_claimed_and_replace(path):
            claimed = original_read(path)
            bridge_runtime_status.publish_status(
                self.root,
                bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
                pid=2222,
            )
            return claimed

        with mock.patch.object(
            bridge_runtime_status,
            "_read_status_file",
            side_effect=read_claimed_and_replace,
        ):
            self.assertTrue(
                bridge_runtime_status.clear_status(self.root, pid=1111)
            )

        remaining = bridge_runtime_status.read_status(self.root)
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining.pid, 2222)

    def test_wrong_pid_restores_claim_without_overwriting_new_status(self):
        bridge_runtime_status.publish_status(
            self.root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=2222,
        )
        original_read = bridge_runtime_status._read_status_file

        def read_claimed_and_replace(path):
            claimed = original_read(path)
            bridge_runtime_status.publish_status(
                self.root,
                bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
                pid=3333,
            )
            return claimed

        with mock.patch.object(
            bridge_runtime_status,
            "_read_status_file",
            side_effect=read_claimed_and_replace,
        ):
            self.assertFalse(
                bridge_runtime_status.clear_status(self.root, pid=1111)
            )

        remaining = bridge_runtime_status.read_status(self.root)
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining.pid, 3333)

    def test_non_positive_pid_is_rejected(self):
        with self.assertRaises(ValueError):
            bridge_runtime_status.publish_status(
                self.root,
                bridge_runtime_status.BridgeConnectionState.CONNECTED,
                pid=0,
            )


if __name__ == "__main__":
    unittest.main()
