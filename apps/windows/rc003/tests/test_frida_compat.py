import hashlib
import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import (
    config,
    frida_compat,
    frida_hid_tap_injector,
    hid_elevation_windows,
    hid_helper_consumers,
)


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

    def test_normal_frozen_app_uses_the_pre_authorized_task(self):
        registered = mock.Mock()

        frida_compat.run_injector_subprocess(
            2468,
            frozen=True,
            _is_elevated=lambda: False,
            _registered_injector=registered,
        )

        registered.assert_called_once_with(2468)

    def test_normal_frozen_app_repairs_a_missing_consumer_before_injection(self):
        tenant = mock.Mock()
        marker = Path("registered-consumer.json")

        with mock.patch.object(
            hid_helper_consumers,
            "current_consumer_is_registered",
            side_effect=(False, True),
        ) as registered_probe, mock.patch.object(
            hid_helper_consumers,
            "register_current_consumer",
            return_value=marker,
        ) as register_consumer, mock.patch.object(
            hid_elevation_windows,
            "run_registered_injector",
            tenant,
        ):
            frida_compat.run_injector_subprocess(
                2468,
                frozen=True,
                _is_elevated=lambda: False,
            )

        self.assertEqual(registered_probe.call_count, 2)
        register_consumer.assert_called_once_with(
            config.config_root(),
            timeout_seconds=(
                frida_compat.HID_CONSUMER_REGISTRATION_TIMEOUT_SECONDS
            ),
        )
        tenant.assert_called_once_with(2468)

    def test_busy_consumer_registration_is_retryable_before_injection(self):
        tenant = mock.Mock()

        with mock.patch.object(
            hid_helper_consumers,
            "current_consumer_is_registered",
            return_value=False,
        ), mock.patch.object(
            hid_helper_consumers,
            "register_current_consumer",
            side_effect=hid_helper_consumers.ConsumerMaintenanceError(
                "helper_consumer_maintenance_busy"
            ),
        ), mock.patch.object(
            hid_elevation_windows,
            "run_registered_injector",
            tenant,
        ):
            with self.assertRaises(frida_compat.HidTapInjectionError) as ctx:
                frida_compat.run_injector_subprocess(
                    2468,
                    frozen=True,
                    _is_elevated=lambda: False,
                )

        self.assertEqual(str(ctx.exception), "hid_helper_operation_busy")
        tenant.assert_not_called()

    def test_elevated_frozen_app_keeps_the_direct_injector_path(self):
        with mock.patch.object(
            frida_compat,
            "_run_direct_injector_subprocess",
        ) as direct:
            frida_compat.run_injector_subprocess(
                2468,
                frozen=True,
                _is_elevated=lambda: True,
                _registered_injector=mock.Mock(),
            )

        direct.assert_called_once_with(
            2468,
            timeout=frida_compat.HID_TAP_INJECTOR_TIMEOUT_SECONDS,
            frozen=True,
            executable=None,
        )

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
    def test_guarded_thread_marks_an_unexpected_return_failed(self):
        statuses = []
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            status_handler=lambda status, detail: statuses.append((status, detail)),
        )
        tap._run = lambda: None

        tap._run_guarded()

        self.assertEqual(
            statuses[-1],
            (frida_compat.HidTapState.FAILED.value, "tap_thread_returned"),
        )

    def test_guarded_thread_catches_base_exception_and_fails_closed(self):
        statuses = []
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            status_handler=lambda status, detail: statuses.append((status, detail)),
        )
        tap._run = mock.Mock(side_effect=SystemExit(7))

        tap._run_guarded()

        self.assertEqual(
            statuses[-1],
            (
                frida_compat.HidTapState.FAILED.value,
                "tap_thread_exception_SystemExit",
            ),
        )

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
                self.sent = []

            def settimeout(self, _timeout):
                pass

            def sendall(self, payload):
                self.sent.append(payload)

            def recv(self, _size):
                self.calls += 1
                if self.calls == 1:
                    return (
                        b'{"kind":"ready","hook_installed":true,"protocol":3}\n'
                        b'{"kind":"control_ack","action":"enable",'
                        b'"accepted":true,"state":"enabled","protocol":3}\n'
                        b'{"kind":"gatt_read","raw":"010000f10000000000",'
                        b'"intercepted":true,"protocol":3}\n'
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

    def test_connection_base_exception_marks_failure_before_neutral_release(self):
        events = []

        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, payload: events.append(
                ("report", payload, tap.status)
            ),
            enabled=False,
            injector=lambda _pid: None,
            client_pid_resolver=lambda _client: 2468,
            status_handler=lambda status, detail: events.append(
                ("status", status, detail)
            ),
        )

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def settimeout(self, _timeout):
                pass

            def sendall(self, _payload):
                pass

            def recv(self, _size):
                self.calls += 1
                if self.calls == 1:
                    return (
                        b'{"kind":"ready","hook_installed":true,"protocol":3}\n'
                        b'{"kind":"control_ack","action":"enable",'
                        b'"accepted":true,"state":"enabled","protocol":3}\n'
                        b'{"kind":"gatt_read","raw":"010000520000000000",'
                        b'"intercepted":true,"protocol":3}\n'
                    )
                raise SystemExit(9)

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
            with self.assertRaises(SystemExit):
                tap._run()

        failure_index = next(
            index
            for index, event in enumerate(events)
            if event[:2]
            == ("status", frida_compat.HidTapState.FAILED.value)
        )
        neutral_index = next(
            index
            for index, event in enumerate(events)
            if event[0] == "report" and event[1] == b"\x00" * 6
        )
        self.assertLess(failure_index, neutral_index)
        self.assertEqual(
            events[neutral_index][2],
            frida_compat.HidTapState.FAILED.value,
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
                self.sent = []

            def settimeout(self, _timeout):
                pass

            def sendall(self, payload):
                self.sent.append(payload)

            def recv(self, _size):
                self.calls += 1
                if self.calls == 1:
                    return (
                        b'{"kind":"ready","hook_installed":true,'
                        b'"protocol":3}\n'
                        b'{"kind":"control_ack","action":"enable",'
                        b'"accepted":true,"state":"enabled","protocol":3}\n'
                    )
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

    def test_invalid_interception_contract_fails_closed(self):
        cases = (
            (
                b'{"kind":"ready","hook_installed":true,"protocol":2}\n',
                "gadget_intercept_protocol_mismatch",
            ),
            (
                b'{"kind":"ready","hook_installed":true,"protocol":3}\n'
                b'{"kind":"control_ack","action":"enable",'
                b'"accepted":true,"state":"enabled","protocol":3}\n'
                b'{"kind":"gatt_read","raw":"010000f10000000000",'
                b'"intercepted":false,"protocol":3}\n',
                "gadget_report_not_intercepted",
            ),
            (
                b'{"kind":"ready","hook_installed":true,"protocol":3}\n'
                b'{"kind":"control_ack","action":"enable",'
                b'"accepted":true,"state":"enabled","protocol":3}\n'
                b'{"kind":"gatt_read","raw":"010000f10000000000",'
                b'"intercepted":true,"protocol":2}\n',
                "gadget_report_not_intercepted",
            ),
        )
        for payload, expected_detail in cases:
            with self.subTest(expected_detail=expected_detail):
                statuses = []
                reports = []

                def record_status(status, detail):
                    statuses.append((status, detail))
                    if detail == expected_detail:
                        tap.stop_event.set()

                tap = frida_compat.RC003HidReportTap(
                    lambda report_id, report: reports.append((report_id, report)),
                    enabled=False,
                    injector=lambda _pid: None,
                    client_pid_resolver=lambda _client: 2468,
                    status_handler=record_status,
                )

                class FakeClient:
                    def sendall(self, _payload):
                        pass

                    def settimeout(self, _timeout):
                        pass

                    def recv(self, _size):
                        return payload

                    def close(self):
                        pass

                server = mock.MagicMock()
                server.accept.return_value = (FakeClient(), ("127.0.0.1", 1))
                with mock.patch.object(
                    frida_compat.frida_hid_tap_runtime,
                    "find_rc003_hidogatt_host_pid",
                    return_value=2468,
                ), mock.patch.object(
                    frida_compat.socket,
                    "socket",
                    return_value=server,
                ):
                    tap._run()

                self.assertIn(
                    (frida_compat.HidTapState.FAILED.value, expected_detail),
                    statuses,
                )
                self.assertNotIn(
                    frida_compat.HidTapState.READY.value,
                    [status for status, _detail in statuses],
                )
                self.assertEqual(reports, [])

    def test_gadget_copies_then_clears_before_reporting_interception(self):
        source = frida_compat.frida_hid_tap_runtime.GADGET_SCRIPT
        intercept = source[
            source.index("function interceptKeyboardReport") :
            source.index("function scheduleReconnect")
        ]
        self.assertLess(
            intercept.index("const raw = hex(pointer, length)"),
            intercept.index("pointer.add(3).writeByteArray"),
        )
        self.assertLess(
            intercept.index("pointer.add(3).writeByteArray"),
            intercept.index("return raw"),
        )

        on_leave = source[
            source.index("onLeave(retval)") :
            source.index("hookInstalled = true")
        ]
        self.assertLess(
            on_leave.index("const raw = interceptKeyboardReport"),
            on_leave.index("emit({"),
        )
        self.assertIn("raw: raw", on_leave)
        self.assertIn("intercepted: true", on_leave)

    def test_gadget_never_clears_a_report_without_a_live_lease(self):
        source = frida_compat.frida_hid_tap_runtime.GADGET_SCRIPT
        intercept = source[
            source.index("function interceptKeyboardReport") :
            source.index("function scheduleReconnect")
        ]
        self.assertIn("!interceptionActive()", intercept)
        self.assertLess(
            intercept.index("!interceptionActive()"),
            intercept.index("pointer.add(3).writeByteArray"),
        )
        self.assertIn("interceptLeaseDeadline = 0", source)

    def test_gadget_waits_for_a_neutral_report_before_taking_ownership(self):
        source = frida_compat.frida_hid_tap_runtime.GADGET_SCRIPT
        intercept = source[
            source.index("function interceptKeyboardReport") :
            source.index("function scheduleReconnect")
        ]

        self.assertIn('raw.slice(6) !== "000000000000"', intercept)
        self.assertLess(
            intercept.index('raw.slice(6) !== "000000000000"'),
            intercept.index("pointer.add(3).writeByteArray"),
        )
        self.assertIn('if (action === "enable") interceptionReady = false;', source)
        self.assertNotIn('if (action === "renew") interceptionReady = false;', source)
        self.assertIn('kind: "intercept_expired"', source)
        self.assertIn('action === "disable"', source)
        self.assertIn('action !== "enable" && action !== "renew"', source)

    def test_stop_disables_interception_before_stopping_the_thread(self):
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
        )

        class FakeClient:
            def __init__(self):
                self.actions = []

            def sendall(self, payload):
                message = __import__("json").loads(payload.decode("ascii"))
                self.actions.append(message["action"])
                tap._record_control_ack(
                    {
                        "kind": "control_ack",
                        "action": message["action"],
                        "accepted": True,
                        "state": "disabled",
                        "protocol": frida_compat.HID_INTERCEPT_PROTOCOL,
                    }
                )

        client = FakeClient()
        tap._set_client(client)
        tap._interception_enabled = True
        with mock.patch.object(frida_compat.time, "sleep") as sleep:
            tap.stop()

        self.assertEqual(client.actions, ["disable"])
        self.assertTrue(tap._stop_requested_event.is_set())
        sleep.assert_not_called()

    def test_late_enable_or_renew_is_converted_to_disable_after_stop_begins(self):
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
        )
        client = mock.Mock()
        tap._stop_requested_event.set()

        self.assertEqual(tap._send_control(client, "enable"), "disable")
        self.assertEqual(tap._send_control(client, "renew"), "disable")

        messages = [
            __import__("json").loads(call.args[0].decode("ascii"))
            for call in client.sendall.call_args_list
        ]
        self.assertEqual([message["action"] for message in messages], ["disable"] * 2)
        self.assertTrue(all("lease_ms" not in message for message in messages))

    def test_missing_disable_ack_waits_out_the_last_possible_lease(self):
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
        )
        client = mock.Mock()
        tap._set_client(client)
        tap._interception_enabled = True

        with mock.patch.object(
            tap._disable_ack_event,
            "wait",
            return_value=False,
        ), mock.patch.object(
            frida_compat.time,
            "monotonic",
            return_value=100.0,
        ), mock.patch.object(frida_compat.time, "sleep") as sleep:
            tap.stop()

        self.assertTrue(tap._stop_requested_event.is_set())
        sleep.assert_called_once_with(
            frida_compat.HID_INTERCEPT_LEASE_SECONDS
            + frida_compat.HID_INTERCEPT_LEASE_SAFETY_SECONDS
        )

    def test_expired_lease_releases_an_active_button_and_disconnects(self):
        reports = []
        statuses = []

        def record_status(status, detail):
            statuses.append((status, detail))
            if detail == "gadget_intercept_lease_expired":
                tap.stop_event.set()

        tap = frida_compat.RC003HidReportTap(
            lambda report_id, payload: reports.append((report_id, payload)),
            enabled=False,
            injector=lambda _pid: None,
            client_pid_resolver=lambda _client: 2468,
            status_handler=record_status,
        )

        class FakeClient:
            def settimeout(self, _timeout):
                pass

            def sendall(self, _payload):
                pass

            def recv(self, _size):
                return (
                    b'{"kind":"ready","hook_installed":true,"protocol":3}\n'
                    b'{"kind":"control_ack","action":"enable",'
                    b'"accepted":true,"state":"enabled","protocol":3}\n'
                    b'{"kind":"gatt_read","raw":"010000520000000000",'
                    b'"intercepted":true,"protocol":3}\n'
                    b'{"kind":"intercept_expired","protocol":3}\n'
                )

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

        self.assertIn(
            (
                frida_compat.HidTapState.UNHEALTHY.value,
                "gadget_intercept_lease_expired",
            ),
            statuses,
        )
        self.assertEqual(
            reports,
            [
                (1, bytes.fromhex("520000000000")),
                (1, b"\x00" * 6),
            ],
        )

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

    def test_busy_registered_helper_retries_the_same_host_pid(self):
        statuses = []
        injector = mock.Mock(
            side_effect=[
                frida_compat.HidTapInjectionError("hid_helper_operation_busy"),
                frida_compat.HidTapInjectionError(
                    "injector_requires_administrator"
                ),
            ]
        )

        def record_status(status, detail):
            statuses.append((status, detail))
            if detail == "injector_requires_administrator":
                tap.stop_event.set()

        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            injector=injector,
            status_handler=record_status,
        )
        tap.stop_event.wait = mock.Mock(return_value=False)
        server = mock.MagicMock()

        with mock.patch.object(
            frida_compat.frida_hid_tap_runtime,
            "find_rc003_hidogatt_host_pid",
            return_value=2468,
        ), mock.patch.object(frida_compat.socket, "socket", return_value=server):
            tap._run()

        self.assertEqual(injector.call_args_list, [mock.call(2468), mock.call(2468)])
        self.assertIn(
            (
                frida_compat.HidTapState.FAILED.value,
                "hid_helper_operation_busy",
            ),
            statuses,
        )
        self.assertIn(
            (
                frida_compat.HidTapState.FAILED.value,
                "injector_requires_administrator",
            ),
            statuses,
        )

    def test_transient_task_start_failure_stops_after_three_attempts_for_same_pid(self):
        for detail in (
            "hid_helper_task_service_unavailable",
            "hid_helper_task_start_failed",
        ):
            with self.subTest(detail=detail):
                injector = mock.Mock(
                    side_effect=frida_compat.HidTapInjectionError(detail)
                )
                tap = frida_compat.RC003HidReportTap(
                    lambda _report_id, _payload: None,
                    enabled=False,
                    injector=injector,
                )
                wait_count = 0

                def bounded_wait(_delay):
                    nonlocal wait_count
                    wait_count += 1
                    if (
                        wait_count
                        == frida_compat.HID_TAP_TRANSIENT_INJECTION_MAX_ATTEMPTS
                        + 1
                    ):
                        tap.stop_event.set()

                tap.stop_event.wait = mock.Mock(side_effect=bounded_wait)
                server = mock.MagicMock()

                with mock.patch.object(
                    frida_compat.frida_hid_tap_runtime,
                    "find_rc003_hidogatt_host_pid",
                    return_value=2468,
                ), mock.patch.object(
                    frida_compat.socket, "socket", return_value=server
                ):
                    tap._run()

                self.assertEqual(
                    injector.call_count,
                    frida_compat.HID_TAP_TRANSIENT_INJECTION_MAX_ATTEMPTS,
                )
                self.assertEqual(
                    wait_count,
                    frida_compat.HID_TAP_TRANSIENT_INJECTION_MAX_ATTEMPTS + 1,
                )

    def test_transient_task_start_retry_budget_resets_for_new_host_pid(self):
        injector = mock.Mock(
            side_effect=frida_compat.HidTapInjectionError(
                "hid_helper_task_start_failed"
            )
        )
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            injector=injector,
        )
        wait_count = 0

        def bounded_wait(_delay):
            nonlocal wait_count
            wait_count += 1
            if wait_count == (
                frida_compat.HID_TAP_TRANSIENT_INJECTION_MAX_ATTEMPTS * 2 + 1
            ):
                tap.stop_event.set()

        tap.stop_event.wait = mock.Mock(side_effect=bounded_wait)
        server = mock.MagicMock()
        first_pid = 2468
        second_pid = 9753
        max_attempts = frida_compat.HID_TAP_TRANSIENT_INJECTION_MAX_ATTEMPTS
        pid_sequence = [first_pid] * max_attempts + [second_pid] * (
            max_attempts + 1
        )

        with mock.patch.object(
            frida_compat.frida_hid_tap_runtime,
            "find_rc003_hidogatt_host_pid",
            side_effect=pid_sequence,
        ), mock.patch.object(frida_compat.socket, "socket", return_value=server):
            tap._run()

        self.assertEqual(
            injector.call_args_list,
            [mock.call(first_pid)] * max_attempts
            + [mock.call(second_pid)] * max_attempts,
        )

    def test_transient_task_start_failure_then_success_waits_for_connection(self):
        statuses = []
        injector = mock.Mock(
            side_effect=[
                frida_compat.HidTapInjectionError(
                    "hid_helper_task_service_unavailable"
                ),
                None,
            ]
        )
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            injector=injector,
            status_handler=lambda status, detail: statuses.append((status, detail)),
        )
        tap.stop_event.wait = mock.Mock(return_value=False)
        server = mock.MagicMock()

        def stop_while_waiting_for_connection():
            tap.stop_event.set()
            raise frida_compat.socket.timeout()

        server.accept.side_effect = stop_while_waiting_for_connection
        with mock.patch.object(
            frida_compat.frida_hid_tap_runtime,
            "find_rc003_hidogatt_host_pid",
            return_value=2468,
        ), mock.patch.object(frida_compat.socket, "socket", return_value=server):
            tap._run()

        self.assertEqual(
            injector.call_args_list,
            [mock.call(2468), mock.call(2468)],
        )
        self.assertEqual(
            statuses[-1],
            (frida_compat.HidTapState.WAITING_CONNECTION.value, ""),
        )

    def test_registered_task_timeout_does_not_retry_the_same_host_pid(self):
        injector = mock.Mock(
            side_effect=frida_compat.HidTapInjectionError(
                "hid_helper_task_timeout"
            )
        )
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            injector=injector,
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

    def test_missing_gadget_connection_becomes_a_stable_failure(self):
        statuses = []
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
            injector=lambda _pid: None,
            connection_timeout=1.0,
            status_handler=lambda status, detail: statuses.append((status, detail)),
        )
        wait_count = 0

        def bounded_wait(_delay):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 1:
                tap.stop_event.set()

        tap.stop_event.wait = mock.Mock(side_effect=bounded_wait)

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
                raise frida_compat.socket.timeout()

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
        ), mock.patch.object(
            frida_compat.time,
            "monotonic",
            side_effect=[10.0, 11.1],
        ):
            tap._run()

        self.assertIn(
            (
                frida_compat.HidTapState.FAILED.value,
                "gadget_connection_timeout",
            ),
            statuses,
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
            with self.assertRaises(
                frida_hid_tap_injector.HidInjectionStageError
            ) as ctx:
                frida_hid_tap_injector.inject_current_process(2468)

        self.assertEqual(str(ctx.exception), "hid_helper_debug_privilege_failed")
        target_name.assert_not_called()

    def test_injector_reports_the_exact_failed_stage(self):
        cases = (
            (
                "target_query",
                {"_target_process_name": OSError("denied")},
                "hid_helper_target_process_open_failed",
            ),
            (
                "target_identity",
                {"_target_process_name": "not-wudfhost.exe"},
                "hid_helper_target_validation_failed",
            ),
            (
                "runtime",
                {"prepare_secure_runtime": OSError("missing")},
                "hid_helper_runtime_preparation_failed",
            ),
            (
                "remote_load",
                {
                    "prepare_secure_runtime": Path("verified.dll"),
                    "sha256_file": frida_hid_tap_injector.GADGET_DLL_SHA256,
                    "inject_library": frida_hid_tap_injector.HidInjectionStageError(
                        "hid_helper_remote_load_failed"
                    ),
                },
                "hid_helper_remote_load_failed",
            ),
        )

        for name, overrides, expected in cases:
            patches = [
                mock.patch.object(frida_hid_tap_injector.os, "name", "nt"),
                mock.patch.object(
                    frida_hid_tap_injector,
                    "find_rc003_hidogatt_host_pid",
                    return_value=2468,
                ),
                mock.patch.object(
                    frida_hid_tap_injector,
                    "enable_debug_privilege",
                ),
            ]
            defaults = {
                "_target_process_name": "wudfhost.exe",
                "prepare_secure_runtime": Path("verified.dll"),
                "sha256_file": frida_hid_tap_injector.GADGET_DLL_SHA256,
                "inject_library": None,
            }
            defaults.update(overrides)
            for target, result in defaults.items():
                kwargs = (
                    {"side_effect": result}
                    if isinstance(result, BaseException)
                    else {"return_value": result}
                )
                patches.append(
                    mock.patch.object(frida_hid_tap_injector, target, **kwargs)
                )
            with self.subTest(name=name):
                with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                    with self.assertRaises(
                        frida_hid_tap_injector.HidInjectionStageError
                    ) as ctx:
                        frida_hid_tap_injector.inject_current_process(2468)
                self.assertEqual(str(ctx.exception), expected)


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
