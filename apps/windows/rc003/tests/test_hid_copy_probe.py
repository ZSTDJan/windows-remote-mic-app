import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003.diagnostic_trace import DiagnosticTrace
from ovb_rc003.frida_compat import RC003HidReportTap
from ovb_rc003 import frida_hid_tap_runtime


class HidCopyProbeTests(unittest.TestCase):
    def make_tap(self, seconds="0", trace=None):
        with mock.patch.dict("os.environ", {"REMOTE_MIC_RC003_COPY_PROBE_SECONDS": seconds}):
            return RC003HidReportTap(mock.Mock(), enabled=False, diagnostic_trace=trace)

    def test_probe_is_opt_in_bounded_and_requires_enabled_trace(self):
        for configured, enabled, expected in (
            ("0", True, None), ("bad", True, None), ("-1", True, None),
            ("180", False, None), ("180", True, 180), ("9999", True, 300),
        ):
            with self.subTest(configured=configured, enabled=enabled):
                trace = mock.Mock(enabled=enabled)
                tap = self.make_tap(configured, trace)
                client = mock.Mock()
                tap._send_control(client, "enable")
                payload = json.loads(client.sendall.call_args.args[0])
                self.assertEqual(payload.get("copy_probe_seconds"), expected)

    def test_lease_renewal_does_not_restart_probe_window(self):
        tap = self.make_tap("180", mock.Mock(enabled=True))
        for action in ("renew", "disable"):
            client = mock.Mock()
            tap._send_control(client, action)
            self.assertNotIn("copy_probe_seconds", json.loads(client.sendall.call_args.args[0]))

    def test_disabled_probe_does_not_record_unsolicited_messages(self):
        trace = mock.Mock(enabled=True)
        tap = self.make_tap("0", trace)
        tap._record_copy_probe({"phase": "copy", "operation": 2})
        trace.emit.assert_not_called()

    def test_probe_records_known_report_and_rejects_unrelated_content(self):
        trace = mock.Mock(enabled=True)
        tap = self.make_tap("180", trace)
        tap._record_copy_probe({
            "phase": "copy", "operation": 2, "call_id": 7, "handle": "0x123",
            "entry_hex": "0100004f0000000000", "return_hex": "010000040000000000",
            "text": "private", "path": "private", "unknown": "private",
            "stack": [
                {"module": "WUDFHost.exe", "rva": "0x11496"},
                {"module": "private.exe", "rva": "0x100"},
            ],
        })
        fields = trace.emit.call_args.kwargs
        self.assertEqual(fields["entry_hex"], "0100004f0000000000")
        self.assertEqual(fields["stack"], [{"module": "WUDFHost.exe", "rva": "0x11496"}])
        self.assertEqual(fields["operation"], 2)
        for name in ("return_hex", "text", "path", "unknown"):
            self.assertNotIn(name, fields)

    def test_malformed_probe_fields_do_not_break_mapping_thread(self):
        trace = mock.Mock(enabled=True)
        tap = self.make_tap("180", trace)
        tap._record_copy_probe({
            "operation": True, "call_id": -1, "phase": {}, "handle": "0xZZ",
            "entry_hex": "x" * 18, "return_hex": [],
            "stack": [None, {"module": "ntdll.dll", "rva": "0xZZ"}],
        })
        fields = trace.emit.call_args.kwargs
        self.assertNotIn("operation", fields)
        self.assertNotIn("call_id", fields)
        self.assertNotIn("handle", fields)
        self.assertEqual(fields["stack"], [])

    def test_copy_id_correlates_original_report_without_extra_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = DiagnosticTrace(Path(directory), enabled=True)
            tap = self.make_tap("180", trace)
            report = bytes.fromhex("010000350000000000")
            tap._record_copy_probe({"phase": "copy", "call_id": 7, "entry_hex": report.hex()})
            tap._handle_ioctl_output(report, copy_probe_id=7)
            tap._handle_ioctl_output(report, copy_probe_id=8)
            tap._release_active()
            trace.close()
            rows = [json.loads(line) for line in trace.path.read_text().splitlines()]
            reports = [row for row in rows if row["event"] == "hid_tap_report"]
            self.assertEqual([row["copy_probe_id"] for row in reports], [7, 8, 0])
            self.assertEqual([row["forwarded"] for row in reports], [True, False, True])
            self.assertEqual(tap.report_handler.call_count, 2)

    def test_copy_binding_requires_verified_exclusive_source(self):
        message = {"protocol": 4, "handle": "0x17c", "epoch": 3}
        for verified in (False, True):
            with self.subTest(verified=verified), mock.patch.object(
                frida_hid_tap_runtime, "rc003_hidogatt_host_is_exclusive", return_value=verified
            ) as verify:
                tap = self.make_tap()
                client = mock.Mock()
                self.assertEqual(tap._bind_copy_candidate(client, message, 99), verified)
                verify.assert_called_once_with(99, diagnostic={}, selected_key=None)
                if verified:
                    payload = json.loads(client.sendall.call_args.args[0])
                    self.assertEqual(payload["action"], "bind_copy_handle")
                    self.assertEqual(payload["handle"], "0x17c")
                    self.assertEqual(payload["epoch"], 3)
                else:
                    client.sendall.assert_not_called()

    def test_copy_binding_rejects_stopping_or_malformed_candidates(self):
        with mock.patch.object(frida_hid_tap_runtime, "rc003_hidogatt_host_is_exclusive", return_value=True):
            for invalid in ({"protocol": 3}, {"handle": "0x0"}, {"epoch": True}, {"epoch": -1}, {"handle": "0xZZ"}):
                tap = self.make_tap()
                client = mock.Mock()
                message = {"protocol": 4, "handle": "0x17c", "epoch": 3, **invalid}
                self.assertFalse(tap._bind_copy_candidate(client, message, 99))
                client.sendall.assert_not_called()
            tap = self.make_tap()
            tap._stop_requested_event.set()
            client = mock.Mock()
            self.assertFalse(tap._bind_copy_candidate(client, {"protocol": 4, "handle": "0x17c", "epoch": 3}, 99))
            client.sendall.assert_not_called()


class ExclusiveCopyHostTests(unittest.TestCase):
    def verify(self, members, denied=False, diagnostic=None):
        device = frida_hid_tap_runtime.HID_SERVICE_PREFIX + "_" + frida_hid_tap_runtime.RC003_HARDWARE_TOKEN
        tree = {"BTHLEDevice": {device: {"instance": {"Device Parameters": {"WUDFDiagnosticInfo": {"HostPid": 7}}}}}}
        for name, pid in members:
            tree[name] = {"other": {"instance": {"Device Parameters": {"WUDFDiagnosticInfo": {"HostPid": pid}}}}}

        class Key:
            def __init__(self, data):
                self.data = data
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass

        def open_key(parent, path):
            if denied:
                raise PermissionError("unreadable host membership")
            if parent == 0:
                return Key(tree)
            data = parent.data
            try:
                for part in path.split("\\"):
                    data = data[part]
            except KeyError:
                raise FileNotFoundError(path) from None
            return Key(data)

        registry = mock.Mock(HKEY_LOCAL_MACHINE=0)
        registry.OpenKey.side_effect = open_key
        registry.QueryInfoKey.side_effect = lambda key: (len(key.data), 0, 0)
        registry.EnumKey.side_effect = lambda key, index: list(key.data)[index]
        registry.QueryValueEx.side_effect = lambda key, name: (key.data[name], 4)
        with mock.patch.object(frida_hid_tap_runtime, "winreg", registry), mock.patch.object(
            frida_hid_tap_runtime, "find_rc003_hidogatt_host_pid", return_value=7
        ):
            return frida_hid_tap_runtime.rc003_hidogatt_host_is_exclusive(7, diagnostic=diagnostic)

    def test_only_exact_target_may_bind(self):
        self.assertTrue(self.verify([]))
        self.assertTrue(self.verify([("USB", 99)]))
        self.assertFalse(self.verify([("USB", 7)]))
        self.assertFalse(self.verify([], denied=True))

    def test_rejection_reasons_are_distinct_without_device_paths(self):
        for members, denied, reason in (
            ([], False, "exclusive_rc003_host"),
            ([("USB", 7)], False, "shared_host"),
            ([], True, "registry_access_denied"),
        ):
            details = {}
            self.verify(members, denied, details)
            self.assertEqual(details["reason"], reason)
            self.assertEqual(details["scan_complete"], not denied)
            self.assertNotIn("instance", json.dumps(details))
            self.assertNotIn("unreadable", json.dumps(details))

    def test_source_unknown_is_not_called_shared_or_access_denied(self):
        for current_pid, reason in ((None, "host_unavailable_unresolved"), (8, "host_changed")):
            details = {}
            with mock.patch.object(frida_hid_tap_runtime, "winreg", mock.Mock()), mock.patch.object(
                frida_hid_tap_runtime, "find_rc003_hidogatt_host_pid", return_value=current_pid
            ):
                self.assertFalse(frida_hid_tap_runtime.rc003_hidogatt_host_is_exclusive(7, diagnostic=details))
            self.assertEqual(details["reason"], reason)


if __name__ == "__main__":
    unittest.main()
