"""Discovery evidence: native failures, unchanged selection, and the exported ZIP."""
import contextlib
import ctypes
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
import zipfile

from ovb_rc003 import app, diagnostic_trace, frida_compat, hid_identity, log_export, logging_setup
from ovb_rc003 import frida_hid_tap_runtime as runtime
from ovb_rc003 import raw_input_windows as raw, remote_selection as selection

CONTAINER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
KEY = selection.container_key(CONTAINER)
PATH = r"\\?\hid#vid_2717&pid_32b8#PRIVATE-device"


def os_error(code=5):
    error = PermissionError("PRIVATE-message") if code == 5 else OSError("PRIVATE-message")
    error.winerror = code
    return error


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


class Registry:
    HKEY_LOCAL_MACHINE = ""

    def __init__(self, rows=(), *, fail_stage=None, error=None):
        self.service = runtime.HID_SERVICE_PREFIX + "_" + runtime.RC003_HARDWARE_TOKEN
        self.rows = rows
        self.fail_stage = fail_stage
        self.error = error or os_error()

    def OpenKey(self, parent, name):
        stage = ("root" if name == runtime.BTHLE_ENUM_KEY else "service" if name == self.service
                 else "host_open" if name.endswith(runtime.WUDF_DIAGNOSTIC_SUFFIX) else "instance")
        if self.fail_stage == stage:
            raise self.error
        return contextlib.nullcontext((parent + "\\" if parent else "") + name)

    def EnumKey(self, path, index):
        stage = "services" if path == runtime.BTHLE_ENUM_KEY else "instances"
        if self.fail_stage == stage:
            raise self.error
        children = ([self.service] if self.rows else []) if stage == "services" else [str(i) for i in range(len(self.rows))]
        if index >= len(children):
            raise os_error(259)
        return children[index]

    def QueryValueEx(self, path, name):
        instance = path.split(self.service + "\\", 1)[1].split("\\", 1)[0]
        value = self.rows[int(instance)][0 if name == "ContainerID" else 1]
        if isinstance(value, BaseException):
            raise value
        return value, 1


def host_lookup(registry):
    fields = {}
    with mock.patch.object(runtime.os, "name", "nt"), mock.patch.object(runtime, "winreg", registry):
        pid = runtime.find_rc003_hidogatt_host_pid(selected_key=KEY, diagnostic=fields)
    return pid, fields


class DiscoveryDiagnosticsTests(unittest.TestCase):
    def test_host_lookup_distinguishes_all_pre_injection_outcomes(self):
        cases = [
            ([], None, "rc003_service_not_found"),
            ([(OTHER, 42)], None, "selected_identity_mismatch"),
            ([(os_error(), 42)], None, "identity_read_failed"),
            ([("PRIVATE-invalid-guid", 42)], None, "identity_read_failed"),
            ([(CONTAINER, FileNotFoundError("PRIVATE-message"))], None, "host_pid_missing"),
            ([(CONTAINER, 0)], None, "host_pid_invalid"),
            ([(CONTAINER, -1)], None, "host_pid_invalid"),
            ([(CONTAINER, 42), (CONTAINER, 43)], None, "ambiguous_selected_hosts"),
            ([(CONTAINER, 42), (CONTAINER, 42)], 42, "selected_host_found"),
            ([(CONTAINER, 42), (os_error(), 43)], 42, "selected_host_found"),
        ]
        for rows, expected, outcome in cases:
            with self.subTest(outcome=outcome, count=len(rows)):
                pid, fields = host_lookup(Registry(rows))
                self.assertEqual(pid, expected)
                self.assertEqual(fields["lookup_outcome"], outcome)
                self.assertEqual(fields["instances_seen"], len(rows))
                self.assertNotIn("PRIVATE", json.dumps(fields))
                if fields["identity_failures"]:
                    self.assertFalse(fields["scan_complete"])
        _, fields = host_lookup(Registry([(os_error(), 0)]))
        self.assertEqual(fields["error_samples"][0]["stage"], "read_container_id")
        self.assertEqual(fields["error_samples"][0]["winerror"], 5)
        self.assertEqual(fields["reason"], "host_pid_unavailable")

    def test_registry_open_and_enumeration_failures_keep_exact_stage(self):
        for failing, expected in (("root", "open_enumerator"), ("services", "enumerate_services"),
                                  ("service", "open_service"), ("instances", "enumerate_instances"),
                                  ("instance", "open_instance"), ("host_open", "open_host_diagnostic")):
            with self.subTest(stage=failing):
                pid, fields = host_lookup(Registry([(CONTAINER, 42)], fail_stage=failing))
                self.assertIsNone(pid)
                self.assertFalse(fields["scan_complete"])
                self.assertEqual(fields["error_samples"][0]["stage"], expected)
                self.assertEqual(fields["error_samples"][0]["winerror"], 5)

    def test_pid_errors_do_not_turn_partial_scan_into_success(self):
        for value, reason in ((os_error(), "registry_access_denied"), ("PRIVATE-invalid", "host_pid_invalid")):
            pid, fields = host_lookup(Registry([(CONTAINER, 42), (CONTAINER, value)]))
            self.assertIsNone(pid)
            self.assertEqual(fields["reason"], reason)
            self.assertEqual(fields["positive_pid_count"], 1)
            self.assertFalse(fields["scan_complete"])
            self.assertNotIn("PRIVATE", json.dumps(fields))

    def test_error_samples_are_bounded_without_losing_failure_counts(self):
        _, fields = host_lookup(Registry([(os_error(), 0)] * 10))
        self.assertEqual(fields["identity_failures"], 10)
        self.assertEqual(len(fields["error_samples"]), 3)
        self.assertEqual(fields["errors_omitted"], 7)
        fields = {}
        with self.assertRaises(hid_identity.NoDevicePathFoundError):
            selection.selected_raw_path([PATH] * 10, KEY, diagnostic=fields,
                                        resolve=mock.Mock(side_effect=os_error()))
        self.assertEqual(fields["identity_failures"], 10)
        self.assertEqual(len(fields["error_samples"]), 3)
        self.assertEqual(fields["errors_omitted"], 7)
        self.assertNotIn("PRIVATE", json.dumps(fields))

    def test_selection_preserves_results_and_reports_mixed_causes(self):
        for values, reason, expected_error in (
            ([], "no_matching_interface", hid_identity.NoDevicePathFoundError),
            ([selection.container_key(OTHER)], "selected_identity_mismatch", hid_identity.NoDevicePathFoundError),
            ([os_error(), selection.container_key(OTHER)], "identity_read_failed", hid_identity.NoDevicePathFoundError),
            ([KEY, KEY], "ambiguous_selected_interface", hid_identity.AmbiguousDevicePathError),
            ([KEY, os_error()], "selected_interface_found", None),
        ):
            with self.subTest(reason=reason):
                fields = {}
                resolve = mock.Mock(side_effect=values)
                if expected_error:
                    with self.assertRaises(expected_error):
                        selection.selected_raw_path([PATH] * len(values), KEY, resolve=resolve, diagnostic=fields)
                else:
                    self.assertEqual(selection.selected_raw_path([PATH] * len(values), KEY,
                                     resolve=resolve, diagnostic=fields), PATH)
                self.assertEqual(fields["reason"], reason)
                self.assertNotIn("PRIVATE", json.dumps(fields))

    def test_configuration_manager_codes_and_invalid_property_shapes_survive_selection(self):
        for stage in ("interface_property", "locate_devnode", "container_property"):
            for failure in (13, 0):
                if stage == "locate_devnode" and failure == 0:
                    continue
                with self.subTest(stage=stage, code=failure):
                    def interface(_path, _prop, kind, _name, size, _flags):
                        kind._obj.value = 0x12 if stage != "interface_property" or failure else 0
                        size._obj.value = 10
                        return failure if stage == "interface_property" else 0
                    def locate(*_args):
                        return failure if stage == "locate_devnode" else 0
                    def container(_node, _prop, kind, _buffer, size, _flags):
                        kind._obj.value = 0x0D if failure else 0
                        size._obj.value = 16
                        return failure
                    cfg = SimpleNamespace(CM_Get_Device_Interface_PropertyW=Function(interface),
                                          CM_Locate_DevNodeW=Function(locate),
                                          CM_Get_DevNode_PropertyW=Function(container))
                    fields = {}
                    with mock.patch.object(selection.ctypes, "WinDLL", return_value=cfg, create=True), \
                            self.assertRaises(hid_identity.NoDevicePathFoundError):
                        selection.selected_raw_path([PATH], KEY, diagnostic=fields)
                    sample = fields["error_samples"][0]
                    self.assertEqual(sample["stage"], stage)
                    self.assertEqual(sample["configret"], failure)
                    self.assertNotIn("winerror", sample)

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes last-error storage")
    def test_raw_list_count_fill_name_and_empty_results(self):
        for failing in ("device_list_count", "device_list_fill", "device_name_count", "device_name_fill", "empty", "ok"):
            with self.subTest(stage=failing):
                def device_list(items, count, _size):
                    stage = "device_list_count" if items is None else "device_list_fill"
                    if stage == failing:
                        ctypes.set_last_error(5)
                        return 0xFFFFFFFF
                    count._obj.value = 0 if failing == "empty" else 1
                    if items is not None:
                        items[0].hDevice = 100
                        return 1
                    return 0
                def device_info(_handle, _command, buffer, size):
                    stage = "device_name_count" if buffer is None else "device_name_fill"
                    if failing == stage:
                        ctypes.set_last_error(6)
                        return 0xFFFFFFFF
                    size._obj.value = len(PATH) + 1
                    if buffer is not None:
                        buffer.value = PATH
                        return len(PATH)
                    return 0
                user32 = SimpleNamespace(GetRawInputDeviceList=Function(device_list),
                                         GetRawInputDeviceInfoW=Function(device_info))
                fields = {}
                with mock.patch.object(raw.ctypes, "WinDLL", return_value=user32):
                    if failing.startswith("device_list"):
                        with self.assertRaises(raw.RawInputUnavailableError):
                            raw.enumerate_matching_device_paths(diagnostic=fields)
                    else:
                        paths = raw.enumerate_matching_device_paths(diagnostic=fields)
                        self.assertEqual(paths, [PATH] if failing == "ok" else [])
                if failing.startswith("device_"):
                    sample = fields["error_samples"][0]
                    self.assertEqual(sample["stage"], failing)
                    self.assertEqual(sample["winerror"], 5 if "list" in failing else 6)
                self.assertNotIn("PRIVATE", json.dumps(fields))


def isolated_app(logger, trace):
    owner = app.RC003App.__new__(app.RC003App)
    owner._logger, owner._diagnostic_trace = logger, trace
    owner._last_raw_input_lookup_diagnostic = None
    owner._selected_remote_key = KEY
    return owner


class DiscoveryEvidenceTests(unittest.TestCase):
    def test_runtime_failure_wires_both_stages_to_basic_and_detailed_logs(self):
        owner = isolated_app(mock.Mock(), mock.Mock())
        owner._input_arbitration_lock = threading.RLock()
        owner._raw_input_lifecycle_lock = threading.RLock()
        owner._raw_input_stopping, owner._accept_input_events = False, True
        owner._hid_listener, owner._raw_input_generation = None, 0
        owner._remote_profile = selection.RC003_PROFILE
        owner._set_runtime_input_state = mock.Mock()
        owner._schedule_raw_input_recovery_locked = mock.Mock()
        def enumerate_paths(*, diagnostic):
            diagnostic.update(reason="enumerated", matching_paths=1)
            return [PATH]
        with mock.patch.object(raw, "enumerate_matching_device_paths", side_effect=enumerate_paths), \
                mock.patch.object(selection, "raw_path_key", side_effect=os_error()), \
                mock.patch.object(raw, "RawInputButtonListener") as listener:
            owner._start_hid_listener_owned()
        listener.assert_not_called()
        owner._schedule_raw_input_recovery_locked.assert_called_once()
        event, = owner._diagnostic_trace.emit.call_args.args
        fields = owner._diagnostic_trace.emit.call_args.kwargs
        self.assertEqual(event, "hid_raw_input_lookup")
        self.assertEqual(fields["outcome"], "no_device")
        self.assertEqual(fields["enumeration"]["matching_paths"], 1)
        self.assertEqual(fields["selection"]["reason"], "identity_read_failed")
        self.assertEqual(fields["selection"]["error_samples"][0]["winerror"], 5)

    def test_duplicate_retries_are_bounded_but_recovery_and_identity_changes_survive(self):
        owner = isolated_app(mock.Mock(), mock.Mock())
        owner._record_raw_input_lookup(1, "no_device", {}, {"reason": "no_matching_interface"})
        owner._record_raw_input_lookup(2, "no_device", {}, {"reason": "no_matching_interface"})
        owner._record_raw_input_lookup(3, "path_selected", {}, {"reason": "selected_interface_found"})
        owner._selected_remote_key = selection.container_key(OTHER)
        owner._record_raw_input_lookup(4, "path_selected", {}, {"reason": "selected_interface_found"})
        self.assertEqual(owner._diagnostic_trace.emit.call_count, 3)
        self.assertEqual(owner._logger.info.call_count, 3)
        self.assertEqual(owner._diagnostic_trace.emit.call_args.kwargs["generation"], 4)

    def test_log_failure_does_not_break_discovery_or_hide_a_later_retry(self):
        owner = isolated_app(mock.Mock(), mock.Mock())
        owner._logger.info.side_effect = OSError("PRIVATE-disk")
        owner._diagnostic_trace.emit.side_effect = OSError("PRIVATE-disk")
        owner._record_raw_input_lookup(1, "no_device", {}, {})
        self.assertIsNone(owner._last_raw_input_lookup_diagnostic)
        owner._logger.info.side_effect = None
        owner._record_raw_input_lookup(2, "no_device", {}, {})
        self.assertIsNotNone(owner._last_raw_input_lookup_diagnostic)

    def test_actual_export_preserves_failures_and_recovery_with_trace_on_or_off(self):
        for enabled in (False, True):
            with self.subTest(trace_enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                handler = logging_setup.EvidenceFileHandler(root / "logs" / "app.log", asynchronous=True)
                logger = logging.Logger("isolated_discovery", logging.INFO)
                logger.addHandler(handler)
                trace = diagnostic_trace.DiagnosticTrace(root, enabled=enabled)
                owner = isolated_app(logger, trace)
                tap = frida_compat.RC003HidReportTap(mock.Mock(), enabled=False, diagnostic_trace=trace)
                try:
                    fields = {}
                    with self.assertRaises(hid_identity.NoDevicePathFoundError):
                        selection.selected_raw_path([PATH], KEY, diagnostic=fields,
                                                    resolve=mock.Mock(side_effect=os_error()))
                    owner._record_raw_input_lookup(1, "no_device", {"matching_paths": 1}, fields)
                    with mock.patch.object(frida_compat, "_LOGGER", logger):
                        for rows in ([(os_error(), 42)], [(OTHER, 42)],
                                     [(CONTAINER, FileNotFoundError("PRIVATE"))],
                                     [(CONTAINER, 42), (CONTAINER, 43)], [(CONTAINER, 42)]):
                            _, fields = host_lookup(Registry(rows))
                            tap._record_diagnostic("hid_host_lookup", **fields)
                    owner._record_raw_input_lookup(2, "path_selected", {"matching_paths": 1},
                                                   {"reason": "selected_interface_found"})
                    self.assertTrue(handler.flush_pending())
                    trace.close()
                    self.assertTrue(diagnostic_trace.flush_fault_report(root / "logs"))
                    exported = log_export.export_logs(root / "evidence.zip", root=root)
                    self.assertEqual(exported.outcome, "exported")
                    self.assertFalse(exported.incomplete)
                    with zipfile.ZipFile(exported.path) as archive:
                        self.assertIsNone(archive.testzip())
                        basic = archive.read("app.log").decode("utf-8")
                        expected = ("identity_read_failed", "selected_identity_mismatch", "host_pid_missing",
                                    "ambiguous_selected_hosts", "selected_host_found", "path_selected")
                        for outcome in expected:
                            self.assertIn(outcome, basic)
                        self.assertIn('"winerror": 5', basic)
                        report = json.loads(archive.read(diagnostic_trace.REPORT_FILENAME))
                        incidents = report["incidents"]
                        self.assertTrue(any(i["failure_key"] == "hid_discovery:raw_input" for i in incidents))
                        self.assertTrue(any(i["failure_key"] == "hid_discovery:host_lookup" for i in incidents))
                        for incident in incidents:
                            self.assertTrue(all(not e.get("message_truncated") for e in incident["events"]))
                        self.assertIn("identity_read_failed", json.dumps(report))
                        if enabled:
                            detailed = archive.read("diagnostic-trace.jsonl").decode("utf-8")
                            for outcome in expected:
                                self.assertIn(outcome, detailed)
                            self.assertIn('"winerror":5', detailed.replace(" ", ""))
                            self.assertIn("session_finished", detailed)
                        else:
                            self.assertNotIn("diagnostic-trace.jsonl", archive.namelist())
                        for name in archive.namelist():
                            content = archive.read(name).decode("utf-8")
                            self.assertNotIn("PRIVATE", content)
                            self.assertNotIn(CONTAINER, content)
                            self.assertNotIn(KEY, content)
                finally:
                    trace.close()
                    handler.close()

    def test_late_failure_retains_discovery_context_after_recent_window_rolls(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = diagnostic_trace._FaultReport(Path(tmp))
            with mock.patch.object(report, "flush"):
                report.accept(dict(event="hid_host_lookup", session_id="fixture", wall_time=1,
                                   lookup_outcome="identity_read_failed", identity_failures=1))
                report.accept(dict(event="hid_raw_input_lookup", session_id="fixture", wall_time=2,
                                   outcome="no_device", selection={"reason": "identity_read_failed"}))
                for index in range(600):
                    report.accept(dict(event="other", session_id="fixture", wall_time=100+index))
                report.accept(dict(event="voice_edge_confirmation", session_id="fixture", wall_time=900,
                                   success=False, reason="input_channel_unavailable"))
            report.flush(force=True)
            saved = json.loads(report.path.read_bytes())["incidents"][-1]
            self.assertEqual(saved["context"]["hid_host_lookup"]["identity_failures"], 1)
            self.assertEqual(saved["context"]["hid_raw_input_lookup"]["selection"]["reason"], "identity_read_failed")

    def test_basic_failure_report_survives_rollover_and_restart_with_trace_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handler = logging_setup.EvidenceFileHandler(root / "logs" / "app.log", asynchronous=True,
                                                       maxBytes=1024, backupCount=3)
            logger = logging.Logger("isolated_discovery_rollover", logging.INFO)
            logger.addHandler(handler)
            trace = diagnostic_trace.DiagnosticTrace(root, enabled=False)
            owner = isolated_app(logger, trace)
            try:
                owner._record_raw_input_lookup(1, "no_device", {}, {"reason": "identity_read_failed"})
                self.assertTrue(handler.flush_pending())
                # Force the normal bounded rotations, then simulate a new process's report load.
                for _ in range(5):
                    logger.info("rotation %s", "x" * 1000)
                self.assertTrue(handler.flush_pending())
                self.assertTrue(diagnostic_trace.flush_fault_report(root / "logs"))
            finally:
                trace.close()
                handler.close()
            restarted = diagnostic_trace._FaultReport(root / "logs")
            self.assertTrue(restarted.incidents)
            exported = log_export.export_logs(root / "after-exit.zip", root=root)
            with zipfile.ZipFile(exported.path) as archive:
                self.assertNotIn("identity_read_failed", archive.read("app.log").decode())
                report = archive.read(diagnostic_trace.REPORT_FILENAME).decode()
                self.assertIn("identity_read_failed", report)
                self.assertIn("hid_discovery:raw_input", report)


@unittest.skipUnless(sys.platform == "win32", "isolated real Windows APIs")
class NativeDiscoveryDiagnosticsTests(unittest.TestCase):
    def test_real_access_denied_invalid_handle_and_cfgmgr_failure_in_child(self):
        # A private HKCU fixture and reduced-access HANDLE cause a real native denial.
        # No hardware registry keys, DACLs, pairing, drivers or input are modified.
        script = r'''
import ctypes, json, uuid, winreg
from unittest import mock
from ovb_rc003 import remote_selection as selection, raw_input_windows as raw
from ovb_rc003 import frida_hid_tap_runtime as runtime
root_name = r"Software\RemoteMic-DiscoveryTest-" + uuid.uuid4().hex
service = runtime.HID_SERVICE_PREFIX + "_" + runtime.RC003_HARDWARE_TOKEN
paths = [root_name, root_name + "\\" + service, root_name + "\\" + service + r"\instance"]
container = "11111111-1111-4111-8111-111111111111"
try:
    for name in paths:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, name) as key:
            winreg.SetValueEx(key, "ContainerID", 0, winreg.REG_SZ, container)
    class Registry:
        HKEY_LOCAL_MACHINE = winreg.HKEY_CURRENT_USER
        EnumKey = staticmethod(winreg.EnumKey)
        QueryValueEx = staticmethod(winreg.QueryValueEx)
        @staticmethod
        def OpenKey(parent, name):
            if name == runtime.BTHLE_ENUM_KEY:
                name = root_name
            access = winreg.KEY_ENUMERATE_SUB_KEYS if name == "instance" else winreg.KEY_READ
            return winreg.OpenKey(parent, name, 0, access)
    fields = {}
    with mock.patch.object(runtime, "winreg", Registry):
        assert runtime.find_rc003_hidogatt_host_pid(selected_key=selection.container_key(container), diagnostic=fields) is None
    assert fields["lookup_outcome"] == "identity_read_failed", fields
    sample = fields["error_samples"][0]
    assert sample["stage"] == "read_container_id" and sample["winerror"] == 5, fields
    native_name = {}
    assert raw._get_device_name(ctypes.WinDLL("user32", use_last_error=True), 0, 0x20000007, diagnostic=native_name) is None
    assert native_name["winerror"] == 6, native_name
    try:
        selection.raw_path_key(r"\\?\hid#vid_2717&pid_32b8#RemoteMic-absent-fixture")
        raise AssertionError("unexpected fixture identity")
    except selection.SelectionError as error:
        native_identity = error.diagnostic
        assert native_identity["stage"] == "interface_property" and native_identity["configret"] != 0, native_identity
    print(json.dumps(dict(registry=fields, raw_name=native_name, identity=native_identity)))
finally:
    for name in reversed(paths):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, name)
        except FileNotFoundError:
            pass
'''
        result = subprocess.run([sys.executable, "-B", "-W", "error::ResourceWarning", "-c", script],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = json.loads(result.stdout)
        self.assertEqual(fields["registry"]["error_samples"][0]["winerror"], 5)
        self.assertEqual(fields["raw_name"]["winerror"], 6)
        self.assertGreater(fields["identity"]["configret"], 0)


if __name__ == "__main__":
    unittest.main()
