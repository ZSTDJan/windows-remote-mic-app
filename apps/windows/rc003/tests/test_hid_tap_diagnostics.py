import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import frida_compat, frida_hid_tap_runtime
from ovb_rc003.diagnostic_trace import DiagnosticTrace
from ovb_rc003.logging_setup import PrivacySafeExceptionFilter


class HidTapDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.logger = logging.Logger("isolated_hid_diagnostic", logging.INFO)
        handler = logging.StreamHandler(self.output)
        handler.addFilter(PrivacySafeExceptionFilter())
        self.logger.addHandler(handler)
        patch = mock.patch.object(frida_compat, "_LOGGER", self.logger)
        patch.start()
        self.addCleanup(patch.stop)
        self.trace = mock.Mock(enabled=False)
        self.tap = frida_compat.RC003HidReportTap(mock.Mock(), enabled=False, diagnostic_trace=self.trace)

    def records(self):
        return [json.loads(line.split("HID diagnostic: ", 1)[1]) for line in self.output.getvalue().splitlines()]

    def test_hook_failures_preserve_stage_but_not_arbitrary_message(self):
        for code in frida_compat._HOOK_ERROR_CODES:
            self.tap._record_hook_error({"code": code, "message": "private-content"})
            self.assertEqual(self.records()[-1]["reason"], code)
        self.tap._record_hook_error({"code": {}, "message": "private-content"})
        self.assertEqual(self.records()[-1]["reason"], "hook_error_unresolved")
        self.assertNotIn("private-content", self.output.getvalue())
        self.tap._record_hook_error({"message": "bound_copy_handle_closed"})
        self.assertEqual(self.records()[-1]["reason"], "bound_copy_handle_closed")

    def test_old_loaded_component_is_reported_without_rejecting_protocol_four(self):
        self.tap._record_ready_diagnostics({"protocol": 4, "hook_installed": True})
        self.assertFalse(self.records()[-1]["diagnostics_available"])
        self.assertIsNone(self.records()[-1]["loaded_diagnostic_revision"])
        self.tap._record_ready_diagnostics({"protocol": 4, "hook_installed": True, "diagnostic_revision": 1})
        self.assertTrue(self.records()[-1]["diagnostics_available"])
        self.assertEqual(self.tap.status, "disabled_non_windows")

    def test_missing_hook_is_visible_in_ready_after_preconnection_error(self):
        self.tap._record_ready_diagnostics({
            "protocol": 4, "hook_installed": False, "hook_error_code": "close_export_missing",
        })
        self.assertEqual(self.records()[-1]["reason"], "close_export_missing")

    def test_entry_resolution_status_is_preserved_without_arbitrary_details(self):
        for status in ("verified", "invalid_import_table", "import_ambiguous", "target_unverified"):
            self.tap._record_ready_diagnostics({
                "protocol": 4, "hook_installed": True, "source_entry_status": status,
                "private_detail": "private-content",
            })
            self.assertEqual(self.records()[-1]["source_entry_status"], status)
        for status in (None, {"reason": "private-content"}, "private-content"):
            self.tap._record_ready_diagnostics({
                "protocol": 4, "hook_installed": True, "source_entry_status": status,
            })
            self.assertIsNone(self.records()[-1]["source_entry_status"])
        self.assertNotIn("private-content", self.output.getvalue())

    def test_copy_failure_logs_numeric_status_and_restoration_without_mapping(self):
        self.tap._record_copy_failure({"ntstatus": 0xC0000001, "restored": True, "raw": "private-content"})
        row = self.records()[-1]
        self.assertEqual(row["ntstatus"], 0xC0000001)
        self.assertTrue(row["restored"])
        self.tap.report_handler.assert_not_called()
        self.assertNotIn("private-content", self.output.getvalue())
        self.tap._record_copy_failure({"ntstatus": True, "restored": "yes"})
        self.assertIsNone(self.records()[-1]["ntstatus"])
        self.assertIsNone(self.records()[-1]["restored"])

    def test_health_is_bounded_and_no_report_does_not_mean_incompatible(self):
        health = {"source_bound": False, "intercepted_reports": 0, "ioctl_calls": 2,
                  "private": "private-content", "layout_matches": True}
        with mock.patch.object(frida_compat.time, "monotonic", side_effect=[100, 105, 130, 131]):
            self.tap._record_copy_health({"copy_health": health})
            self.tap._record_copy_health({"copy_health": health})
            self.assertEqual(len(self.records()), 1)
            self.tap._record_copy_health({"copy_health": health})
            self.assertEqual(len(self.records()), 2)
            self.tap._record_copy_health({"copy_health": {**health, "source_bound": True, "waiting_neutral": True}})
        self.assertEqual(self.records()[0]["assessment"], "copy_layout_not_seen_cause_unresolved")
        self.assertEqual(self.records()[-1]["assessment"], "waiting_for_existing_hold_release")
        self.assertNotIn("layout_matches", self.records()[0])
        self.assertNotIn("private-content", self.output.getvalue())

    def test_health_judgments_keep_observation_separate_from_effect(self):
        for health, expected in (
            ({}, "no_intercepted_report_yet_cause_unresolved"),
            ({"layout_matches": 2}, "no_rc003_report_observed_cause_unresolved"),
            ({"candidate_reports": 1}, "waiting_for_source_binding"),
            ({"source_bound": True}, "bound_waiting_for_next_report"),
            ({"intercepted_reports": 3}, "reports_intercepted_not_target_effect_confirmed"),
            ({"copy_failures": 1}, "copy_failed_observed"),
        ):
            self.tap._record_copy_health({"copy_health": health})
            self.assertEqual(self.records()[-1]["assessment"], expected)

    def test_control_rejection_records_exact_allowlisted_reason(self):
        self.assertFalse(self.tap._record_control_ack({
            "protocol": 4, "accepted": False, "action": "bind_copy_handle", "state": "disabled",
            "detail": "stale_copy_candidate",
        }))
        self.assertEqual(self.records()[0]["reason"], "stale_copy_candidate")
        self.assertEqual(self.tap.status_detail, "gadget_control_rejected")

    def test_source_check_is_persisted_even_when_trace_disabled(self):
        def deny(_pid, *, diagnostic, selected_key):
            diagnostic.update(reason="shared_host", host_members=2, scan_complete=True)
            return False
        with mock.patch.object(frida_hid_tap_runtime, "rc003_hidogatt_host_is_exclusive", side_effect=deny):
            self.assertFalse(self.tap._bind_copy_candidate(mock.Mock(), {"protocol": 4, "handle": "0x123", "epoch": 1}, 99))
        self.assertEqual(self.records()[0]["reason"], "shared_host")
        self.assertFalse(self.records()[0]["verified"])

    def test_logging_failures_do_not_change_control_acceptance(self):
        self.trace.emit.side_effect = RuntimeError("private-content")
        with mock.patch.object(frida_compat, "_LOGGER") as logger:
            logger.info.side_effect = OSError("disk full")
            self.assertTrue(self.tap._record_control_ack({
                "protocol": 4, "accepted": True, "action": "bind_copy_handle", "state": "bound",
            }))
            self.tap._record_copy_failure({"ntstatus": 1})
        self.tap.report_handler.assert_not_called()

    def test_plain_file_has_failure_with_real_trace_off_and_no_trace_file(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = DiagnosticTrace(Path(directory), enabled=False)
            self.tap._diagnostic_trace = trace
            path = Path(directory) / "app.log"
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.addFilter(PrivacySafeExceptionFilter())
            self.logger.addHandler(handler)
            try:
                self.tap._record_hook_error({"code": "copy_entry_exception", "message": "private-content"})
            finally:
                self.logger.removeHandler(handler)
                handler.close()
                trace.close()
            content = path.read_text(encoding="utf-8")
            self.assertIn("copy_entry_exception", content)
            self.assertNotIn("private-content", content)
            self.assertFalse(trace.path.exists())

    def test_environment_only_fingerprints_fixed_system_files(self):
        version = mock.Mock(platform_version=(10, 0, 22621))
        with mock.patch.object(frida_compat.os, "name", "nt"), mock.patch.object(
            frida_compat.sys, "getwindowsversion", return_value=version, create=True
        ), mock.patch("ovb_rc003.hid_elevation_windows.query_process_elevated", return_value=True), mock.patch.object(
            frida_hid_tap_runtime, "sha256_file", side_effect=["a" * 64, PermissionError("private-content"), FileNotFoundError("private-content")]
        ) as read_hash:
            fields = frida_compat._diagnostic_environment()
        self.assertEqual(read_hash.call_count, 3)
        self.assertEqual(fields["windows_version"], [10, 0, 22621])
        self.assertEqual(fields["system_files"]["WUDFRd.sys"]["error_type"], "PermissionError")
        self.assertEqual(fields["fingerprint_scope"], "on_disk_not_loaded_modules")
        self.assertNotIn("private-content", json.dumps(fields))

    def test_host_discovery_reports_denied_missing_and_valid_separately(self):
        service = frida_hid_tap_runtime.HID_SERVICE_PREFIX + "_" + frida_hid_tap_runtime.RC003_HARDWARE_TOKEN
        exhausted = OSError("end")
        exhausted.winerror = 259
        for outcome in ("denied", "no_service", "no_host_pid", "found"):
            registry = mock.MagicMock(HKEY_LOCAL_MACHINE=0)
            if outcome == "denied":
                registry.OpenKey.side_effect = PermissionError("private-content")
            elif outcome == "no_service":
                registry.EnumKey.side_effect = exhausted
            else:
                registry.EnumKey.side_effect = [service, "instance", exhausted, exhausted]
                if outcome == "no_host_pid":
                    registry.QueryValueEx.side_effect = FileNotFoundError("private-content")
                else:
                    registry.QueryValueEx.return_value = (42, 4)
            fields = {}
            with mock.patch.object(frida_hid_tap_runtime.os, "name", "nt"), mock.patch.object(
                frida_hid_tap_runtime, "winreg", registry
            ):
                pid = frida_hid_tap_runtime.find_rc003_hidogatt_host_pid(diagnostic=fields)
            self.assertEqual(fields["reason"], {
                "denied": "registry_access_denied", "no_service": "rc003_service_not_found",
                "no_host_pid": "host_pid_unavailable", "found": "host_found",
            }[outcome])
            self.assertEqual(pid, 42 if outcome == "found" else None)
            self.assertNotIn("private-content", json.dumps(fields))

    def test_received_failure_messages_reach_plain_log_without_mapping(self):
        tap = self.tap
        tap.injector = mock.Mock()
        tap.client_pid_resolver = lambda _client: 99

        def status_handler(_status, detail):
            if detail == "gadget_hook_error":
                tap.stop_event.set()

        tap.status_handler = status_handler
        messages = [
            {"kind": "ready", "hook_installed": True, "protocol": 4, "diagnostic_revision": 1},
            {"kind": "control_ack", "action": "enable", "state": "enabled", "accepted": True, "protocol": 4},
            {"kind": "copy_failure", "ntstatus": 0xC0000001, "restored": True},
            {"kind": "heartbeat", "copy_health": {"copy_failures": 1}},
            {"kind": "error", "code": "bound_copy_handle_closed", "message": "private-content"},
        ]
        client = mock.Mock()
        client.recv.return_value = ("\n".join(json.dumps(message) for message in messages) + "\n").encode()
        server = mock.MagicMock()
        server.accept.return_value = (client, ("127.0.0.1", 1))
        with mock.patch.object(frida_hid_tap_runtime, "find_rc003_hidogatt_host_pid", return_value=99), mock.patch.object(
            frida_compat.socket, "socket", return_value=server
        ):
            tap._run()
        events = {row["event"] for row in self.records()}
        self.assertTrue({"hid_runtime_capabilities", "hid_copy_failure", "hid_copy_health", "hid_hook_failure"} <= events)
        self.assertIn("bound_copy_handle_closed", self.output.getvalue())
        self.assertNotIn("private-content", self.output.getvalue())
        tap.report_handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
