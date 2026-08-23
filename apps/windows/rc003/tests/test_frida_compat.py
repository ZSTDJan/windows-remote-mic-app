import hashlib
import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import frida_compat, frida_hid_tap_injector


class AssetDescriptorTests(unittest.TestCase):
    def test_uses_official_release_url(self):
        self.assertTrue(
            frida_compat.FRIDA_GADGET.url.startswith(
                "https://github.com/frida/frida/releases/download/"
            )
        )

    def test_sha256_is_pinned_and_well_formed(self):
        self.assertEqual(len(frida_compat.FRIDA_GADGET.sha256), 64)
        int(frida_compat.FRIDA_GADGET.sha256, 16)


class VerifyAssetTests(unittest.TestCase):
    def test_false_when_missing(self):
        missing = Path("/nonexistent/frida-gadget.dll.xz")
        self.assertFalse(frida_compat.verify_asset(missing, frida_compat.FRIDA_GADGET))

    def test_false_when_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset.bin"
            path.write_bytes(b"not the real gadget")
            self.assertFalse(frida_compat.verify_asset(path, frida_compat.FRIDA_GADGET))

    def test_true_when_hash_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset.bin"
            content = b"pretend gadget bytes"
            path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            asset = frida_compat.ThirdPartyAsset(
                name="test",
                version="0",
                url="https://example.invalid/a",
                sha256=digest,
                license_name="x",
                license_url="https://example.invalid/license",
            )
            self.assertTrue(frida_compat.verify_asset(path, asset))


class ReportDecodeTests(unittest.TestCase):
    def test_decodes_verified_hidogatt_buffer(self):
        self.assertEqual(
            frida_compat.decode_rc003_ioctl_output(
                bytes.fromhex("010000f10080008100")
            ),
            bytes.fromhex("f10080008100"),
        )

    def test_rejects_wrong_prefix_or_length(self):
        self.assertIsNone(frida_compat.decode_rc003_ioctl_output(b"\x01\x00\x00"))
        self.assertIsNone(
            frida_compat.decode_rc003_ioctl_output(
                bytes.fromhex("020000f10080008100")
            )
        )

    def test_extracts_nonzero_little_endian_usages(self):
        self.assertEqual(
            frida_compat.payload_usages(bytes.fromhex("f10000008100")),
            {0xF1, 0x81},
        )
        self.assertEqual(frida_compat.payload_usages(b"short"), set())


class ReportTapTests(unittest.TestCase):
    def test_emits_only_edges_for_missing_usages(self):
        reports = []
        tap = frida_compat.RC003HidReportTap(
            lambda report_id, payload: reports.append((report_id, payload)),
            enabled=False,
        )
        tap._handle_ioctl_output(bytes.fromhex("010000f10080008100"))
        tap._handle_ioctl_output(bytes.fromhex("010000f10000000000"))
        self.assertEqual(
            reports,
            [
                (1, bytes.fromhex("80008100f100")),
                (1, bytes.fromhex("f10000000000")),
            ],
        )

    def test_releases_active_usages_when_stopped(self):
        reports = []
        tap = frida_compat.RC003HidReportTap(
            lambda report_id, payload: reports.append((report_id, payload)),
            enabled=False,
        )
        tap._handle_ioctl_output(bytes.fromhex("010000f10000000000"))
        tap._release_active()
        self.assertEqual(reports[-1], (1, b"\x00" * 6))

    def test_missing_gadget_degrades_without_starting(self):
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            archive_path=Path("/nonexistent/frida-gadget.dll.xz"),
            enabled=True,
        )
        self.assertFalse(tap.available)
        self.assertIn("unavailable", tap.status)
        self.assertFalse(tap.start())

    def test_compatibility_name_accepts_custom_verified_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset.bin"
            content = b"pretend gadget bytes"
            path.write_bytes(content)
            asset = frida_compat.ThirdPartyAsset(
                name="test",
                version="0",
                url="https://example.invalid/a",
                sha256=hashlib.sha256(content).hexdigest(),
                license_name="x",
                license_url="https://example.invalid/license",
            )
            layer = frida_compat.BackKeyCompatLayer(gadget_path=path, asset=asset)
            self.assertTrue(layer.available)
            self.assertEqual(layer.status, "verified_not_started")


class TcpClientIdentityTests(unittest.TestCase):
    def test_resolves_the_unique_peer_endpoint_owner(self):
        client = mock.Mock()
        client.getpeername.return_value = ("127.0.0.1", 41000)
        client.getsockname.return_value = ("127.0.0.1", 30684)
        rows = (
            frida_compat._TcpOwnerRow(41000, 30684, 2468),
            frida_compat._TcpOwnerRow(30684, 41000, 1357),
        )

        self.assertEqual(
            frida_compat.tcp_client_process_id(client, _rows=lambda: rows),
            2468,
        )

    def test_rejects_non_loopback_or_ambiguous_owner(self):
        client = mock.Mock()
        client.getpeername.return_value = ("127.0.0.1", 41000)
        client.getsockname.return_value = ("127.0.0.1", 30684)
        rows = (
            frida_compat._TcpOwnerRow(41000, 30684, 2468),
            frida_compat._TcpOwnerRow(41000, 30684, 9999),
        )
        self.assertIsNone(
            frida_compat.tcp_client_process_id(client, _rows=lambda: rows)
        )

        client.getpeername.return_value = ("192.0.2.1", 41000)
        self.assertIsNone(
            frida_compat.tcp_client_process_id(client, _rows=lambda: rows)
        )


class InjectorSubprocessTests(unittest.TestCase):
    def test_source_command_is_an_argument_array_with_hidden_flag(self):
        command = frida_compat.build_injector_command(
            1234, frozen=False, executable="python.exe"
        )
        self.assertEqual(
            command,
            [
                "python.exe",
                "-m",
                "ovb_rc003",
                frida_compat.HID_TAP_INJECTOR_FLAG,
                "--pid",
                "1234",
            ],
        )

    def test_frozen_command_reuses_the_packaged_executable(self):
        command = frida_compat.build_injector_command(
            4321, frozen=True, executable="RemoteMicRC003.exe"
        )
        self.assertEqual(
            command,
            [
                "RemoteMicRC003.exe",
                frida_compat.HID_TAP_INJECTOR_FLAG,
                "--pid",
                "4321",
            ],
        )

    def test_nonzero_exit_is_captured_as_a_sanitized_failure(self):
        def fake_run(command, **kwargs):
            self.assertIsInstance(command, list)
            self.assertFalse(kwargs["check"])
            return subprocess.CompletedProcess(command, 4)

        with self.assertRaises(frida_compat.HidTapInjectionError) as ctx:
            frida_compat.run_injector_subprocess(1234, _run=fake_run)
        self.assertEqual(str(ctx.exception), "injector_validation_failed")

    def test_permission_exit_has_actionable_sanitized_detail(self):
        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 3)

        with self.assertRaises(frida_compat.HidTapInjectionError) as ctx:
            frida_compat.run_injector_subprocess(1234, _run=fake_run)
        self.assertEqual(str(ctx.exception), "injector_requires_administrator")

    def test_timeout_is_captured_without_raw_child_output(self):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with self.assertRaises(frida_compat.HidTapInjectionError) as ctx:
            frida_compat.run_injector_subprocess(1234, _run=fake_run)
        self.assertEqual(str(ctx.exception), "injector_timeout")

    def test_child_entrypoint_returns_stable_permission_failure_code(self):
        with mock.patch.object(
            frida_hid_tap_injector,
            "inject_current_process",
            side_effect=PermissionError("private detail"),
        ):
            self.assertEqual(frida_hid_tap_injector.main(["--pid", "1234"]), 3)

    def test_child_entrypoint_returns_stable_validation_failure_code(self):
        with mock.patch.object(
            frida_hid_tap_injector,
            "inject_current_process",
            side_effect=RuntimeError("private detail"),
        ):
            self.assertEqual(frida_hid_tap_injector.main(["--pid", "1234"]), 4)


class TapStateTests(unittest.TestCase):
    def test_thread_start_is_starting_not_ready(self):
        statuses = []
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            status_handler=lambda status, detail: statuses.append((status, detail)),
        )
        tap.enabled = True

        class FakeThread:
            def __init__(self, **kwargs):
                self.target = kwargs["target"]
                self.started = False

            def start(self):
                self.started = True

            def is_alive(self):
                return self.started

        with mock.patch.object(
            type(tap), "dependency_available", new_callable=mock.PropertyMock,
            return_value=True,
        ), mock.patch.object(frida_compat.threading, "Thread", FakeThread):
            self.assertTrue(tap.start())

        self.assertEqual(tap.status, frida_compat.HidTapState.STARTING.value)
        self.assertNotEqual(tap.status, frida_compat.HidTapState.READY.value)
        self.assertEqual(statuses[-1][0], frida_compat.HidTapState.STARTING.value)

    def test_valid_hid_io_is_the_event_that_announces_ready(self):
        reports = []
        statuses = []
        tap = frida_compat.RC003HidReportTap(
            lambda report_id, payload: reports.append((report_id, payload)),
            enabled=False,
            injector=lambda _pid: None,
            client_pid_resolver=lambda _client: 2468,
            status_handler=lambda status, detail: statuses.append((status, detail)),
        )

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def settimeout(self, _timeout):
                pass

            def recv(self, _size):
                self.calls += 1
                if self.calls == 1:
                    return (
                        b'{"kind":"ready","hook_installed":true}\n'
                        b'{"kind":"gatt_read","raw":"010000f10000000000"}\n'
                    )
                tap.stop_event.set()
                return b""

            def close(self):
                pass

        class FakeServer:
            def setsockopt(self, *_args):
                pass

            def bind(self, _address):
                pass

            def listen(self, _backlog):
                pass

            def settimeout(self, _timeout):
                pass

            def accept(self):
                return FakeClient(), ("127.0.0.1", 1)

            def close(self):
                pass

        with mock.patch.object(
            frida_compat.frida_hid_tap_runtime,
            "find_rc003_hidogatt_host_pid",
            return_value=2468,
        ), mock.patch.object(frida_compat.socket, "socket", return_value=FakeServer()):
            tap._run()

        state_names = [status for status, _detail in statuses]
        self.assertIn(frida_compat.HidTapState.ATTACHED_WAITING_IO.value, state_names)
        self.assertIn(frida_compat.HidTapState.READY.value, state_names)
        self.assertEqual(
            reports,
            [
                (1, bytes.fromhex("f10000000000")),
                (1, b"\x00" * 6),
            ],
        )

    def test_gadget_handshake_and_heartbeat_do_not_announce_hid_ready(self):
        statuses = []
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            injector=lambda _pid: None,
            client_pid_resolver=lambda _client: 2468,
            status_handler=lambda status, detail: statuses.append((status, detail)),
        )

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def settimeout(self, _timeout):
                pass

            def recv(self, _size):
                self.calls += 1
                if self.calls == 1:
                    return b'{"kind":"ready","hook_installed":true}\n'
                tap.stop_event.set()
                return b'{"kind":"heartbeat","pid":2468}\n'

            def close(self):
                pass

        class FakeServer:
            def setsockopt(self, *_args):
                pass

            def bind(self, _address):
                pass

            def listen(self, _backlog):
                pass

            def settimeout(self, _timeout):
                pass

            def accept(self):
                return FakeClient(), ("127.0.0.1", 1)

            def close(self):
                pass

        with mock.patch.object(
            frida_compat.frida_hid_tap_runtime,
            "find_rc003_hidogatt_host_pid",
            return_value=2468,
        ), mock.patch.object(
            frida_compat.socket,
            "socket",
            return_value=FakeServer(),
        ):
            tap._run()

        state_names = [status for status, _detail in statuses]
        self.assertIn(
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            state_names,
        )
        self.assertNotIn(frida_compat.HidTapState.READY.value, state_names)

    def test_non_object_json_message_is_ignored_without_killing_the_tap(self):
        statuses = []
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            injector=lambda _pid: None,
            client_pid_resolver=lambda _client: 2468,
            status_handler=lambda status, detail: statuses.append((status, detail)),
        )

        class FakeClient:
            def settimeout(self, _timeout):
                pass

            def recv(self, _size):
                tap.stop_event.set()
                return b"[]\n"

            def close(self):
                pass

        server = mock.MagicMock()
        server.accept.return_value = (FakeClient(), ("127.0.0.1", 1))
        with mock.patch.object(
            frida_compat.frida_hid_tap_runtime,
            "find_rc003_hidogatt_host_pid",
            return_value=2468,
        ), mock.patch.object(frida_compat.socket, "socket", return_value=server):
            tap._run()

        self.assertNotIn(
            frida_compat.HidTapState.FAILED.value,
            [status for status, _detail in statuses],
        )

    def test_oversized_unterminated_message_is_rejected_with_a_bounded_buffer(self):
        statuses = []

        def record_status(status, detail):
            statuses.append((status, detail))
            if detail == "gadget_message_too_large":
                tap.stop_event.set()

        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            injector=lambda _pid: None,
            client_pid_resolver=lambda _client: 2468,
            status_handler=record_status,
        )

        client = mock.MagicMock()
        client.recv.return_value = b"x" * (frida_compat.HID_TAP_MAX_BUFFER_BYTES + 1)
        server = mock.MagicMock()
        server.accept.return_value = (client, ("127.0.0.1", 1))
        with mock.patch.object(
            frida_compat.frida_hid_tap_runtime,
            "find_rc003_hidogatt_host_pid",
            return_value=2468,
        ), mock.patch.object(frida_compat.socket, "socket", return_value=server):
            tap._run()

        self.assertIn(
            (
                frida_compat.HidTapState.UNHEALTHY.value,
                "gadget_message_too_large",
            ),
            statuses,
        )

    def test_unexpected_local_client_is_closed_before_any_message_is_read(self):
        statuses = []

        def record_status(status, detail):
            statuses.append((status, detail))
            if detail == "gadget_client_identity_mismatch":
                tap.stop_event.set()

        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            injector=lambda _pid: None,
            client_pid_resolver=lambda _client: 9999,
            status_handler=record_status,
        )
        client = mock.MagicMock()
        server = mock.MagicMock()
        server.accept.return_value = (client, ("127.0.0.1", 1))

        with mock.patch.object(
            frida_compat.frida_hid_tap_runtime,
            "find_rc003_hidogatt_host_pid",
            return_value=2468,
        ), mock.patch.object(frida_compat.socket, "socket", return_value=server):
            tap._run()

        client.recv.assert_not_called()
        client.close.assert_called_once()
        self.assertIn(
            (
                frida_compat.HidTapState.UNHEALTHY.value,
                "gadget_client_identity_mismatch",
            ),
            statuses,
        )

    def test_injection_failure_waits_for_a_new_host_pid_before_retrying(self):
        statuses = []
        injector = mock.Mock(
            side_effect=frida_compat.HidTapInjectionError(
                "injector_requires_administrator"
            )
        )
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            injector=injector,
            status_handler=lambda status, detail: statuses.append((status, detail)),
        )
        wait_count = 0

        def bounded_wait(_delay):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                tap.stop_event.set()

        tap.stop_event.wait = mock.Mock(side_effect=bounded_wait)
        server = mock.MagicMock()
        with mock.patch.object(
            frida_compat.frida_hid_tap_runtime,
            "find_rc003_hidogatt_host_pid",
            return_value=2468,
        ), mock.patch.object(frida_compat.socket, "socket", return_value=server):
            tap._run()

        injector.assert_called_once_with(2468)
        self.assertEqual(
            statuses,
            [
                (frida_compat.HidTapState.INJECTING.value, ""),
                (
                    frida_compat.HidTapState.FAILED.value,
                    "injector_requires_administrator",
                ),
            ],
        )


class InjectorOrderingTests(unittest.TestCase):
    def test_debug_privilege_is_enabled_before_wudfhost_name_query(self):
        calls = []

        with mock.patch.object(frida_hid_tap_injector.os, "name", "nt"), mock.patch.object(
            frida_hid_tap_injector,
            "find_rc003_hidogatt_host_pid",
            return_value=2468,
        ), mock.patch.object(
            frida_hid_tap_injector,
            "enable_debug_privilege",
            side_effect=lambda: calls.append("debug"),
        ), mock.patch.object(
            frida_hid_tap_injector,
            "_target_process_name",
            side_effect=lambda _pid: calls.append("target") or "wudfhost.exe",
        ), mock.patch.object(
            frida_hid_tap_injector,
            "prepare_secure_runtime",
            return_value=Path("verified.dll"),
        ), mock.patch.object(
            frida_hid_tap_injector,
            "sha256_file",
            return_value=frida_hid_tap_injector.GADGET_DLL_SHA256,
        ), mock.patch.object(
            frida_hid_tap_injector,
            "inject_library",
            side_effect=lambda _pid, _path: calls.append("inject"),
        ):
            frida_hid_tap_injector.inject_current_process(2468)

        self.assertEqual(calls, ["debug", "target", "inject"])

    def test_debug_privilege_failure_stops_before_target_query(self):
        with mock.patch.object(frida_hid_tap_injector.os, "name", "nt"), mock.patch.object(
            frida_hid_tap_injector,
            "find_rc003_hidogatt_host_pid",
            return_value=2468,
        ), mock.patch.object(
            frida_hid_tap_injector,
            "enable_debug_privilege",
            side_effect=PermissionError("not assigned"),
        ), mock.patch.object(
            frida_hid_tap_injector, "_target_process_name"
        ) as target_name:
            with self.assertRaises(PermissionError):
                frida_hid_tap_injector.inject_current_process(2468)

        target_name.assert_not_called()


class InjectorCleanupSafetyTests(unittest.TestCase):
    def test_remote_buffer_is_only_freed_after_thread_completion(self):
        source = inspect.getsource(frida_hid_tap_injector.inject_library)

        self.assertIn("remote_thread_completed = False", source)
        self.assertIn("remote_thread_completed = True", source)
        self.assertIn(
            "if remote_path and (thread is None or remote_thread_completed):",
            source,
        )

    def test_wait_timeout_and_failure_are_distinguished(self):
        source = inspect.getsource(frida_hid_tap_injector.inject_library)

        self.assertIn("wait_result == WAIT_TIMEOUT", source)
        self.assertIn("wait_result == WAIT_FAILED", source)


if __name__ == "__main__":
    unittest.main()
