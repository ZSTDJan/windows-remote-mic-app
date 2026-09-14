"""Diagnostic failures and isolated host evidence; no real device/input changes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ovb_rc003 import diagnostic_trace as diagnostics, win32_input
from ovb_rc003.frida_compat import RC003HidReportTap
from ovb_rc003.frida_hid_tap_runtime import GADGET_SCRIPT, GADGET_SCRIPT_BUILD_ID


class IncidentTests(unittest.TestCase):
    def test_before_after_and_previous_session_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(4):
                trace = diagnostics.DiagnosticTrace(root, enabled=True)
                trace.emit("device_context", selected_ref="abcdef012345")
                trace.emit("before", probe=index)
                trace.emit("voice_edge_confirmation", success=False, reason="local_hook_confirmation_timeout")
                trace.emit("recovered", success=True)
                trace.close()
            report = json.loads((root / "logs" / diagnostics.REPORT_FILENAME).read_bytes())
            self.assertEqual(len(report["incidents"]), 3)
            self.assertEqual(len({i["session_id"] for i in report["incidents"]}), 3)
            for incident in report["incidents"]:
                events = [event["event"] for event in incident["events"]]
                self.assertLess(events.index("before"), events.index("voice_edge_confirmation"))
                self.assertGreater(events.index("recovered"), events.index("voice_edge_confirmation"))
                self.assertEqual(incident["context"]["device_context"]["selected_ref"], "abcdef012345")
                self.assertIsInstance(incident["events"][0]["native_thread_id"], int)

    def test_corrupt_existing_report_and_large_event_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for old in ("[1]", "{bad", '{"schema":1,"incidents":[{"events":[]}]}'):
                (root / "logs").mkdir(exist_ok=True)
                path = root / "logs" / diagnostics.REPORT_FILENAME
                path.write_text(old)
                trace = diagnostics.DiagnosticTrace(root, enabled=True)
                trace.emit("failure", success=False, details="x" * 20000, text="private")
                trace.close()
                result = json.loads(path.read_bytes())
                self.assertEqual(len(result["incidents"]), 1)
                self.assertLess(path.stat().st_size, diagnostics.REPORT_MAX_BYTES)
                self.assertNotIn("private", path.read_text())
                self.assertNotIn("x" * 100, path.read_text())

    def test_disk_failure_does_not_interrupt_input_or_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs" / diagnostics.REPORT_FILENAME).mkdir(parents=True)
            trace = diagnostics.DiagnosticTrace(root, enabled=True)
            self.assertTrue(trace.emit("failed_action", success=False))
            self.assertTrue(trace.emit("still_running"))
            trace.close()
            records = (root / "logs" / diagnostics.TRACE_FILENAME).read_text()
            self.assertIn("still_running", records)
            self.assertIn("session_finished", records)

    def test_incident_window_and_size_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = diagnostics._FaultReport(Path(tmp))
            with mock.patch.object(report, "flush"):
                report.accept(dict(event="device_context", session_id="a", wall_time=1, selected_ref="123"))
                for i in range(600):
                    report.accept(dict(event="failure", session_id="a", wall_time=100+i/100,
                                       success=False, details="x" * 7500))
                report.accept(dict(event="too_late", session_id="a", wall_time=200))
            report.flush(force=True)
            self.assertLessEqual(report.path.stat().st_size, diagnostics.REPORT_MAX_BYTES)
            saved = json.loads(report.path.read_bytes())["incidents"][-1]
            self.assertLessEqual(len(saved["events"]), 192)
            self.assertEqual(saved["failure_count"], 600)
            self.assertNotIn("too_late", [i["event"] for i in saved["events"]])
            self.assertEqual(saved["context"]["device_context"]["selected_ref"], "123")


class VoiceEvidenceTests(unittest.TestCase):
    def test_edge_result_preserves_input_behavior_and_records_context(self):
        for key_up in (False, True):
            for confirmed in (False, True):
                with self.subTest(key_up=key_up, confirmed=confirmed):
                    trace = mock.Mock(enabled=True)
                    trace.current_context.return_value = {"attempt_id": "attempt-1"}
                    with mock.patch.object(win32_input, "_diagnostic_trace", trace), \
                         mock.patch.object(diagnostics, "foreground_context", return_value={"foreground_is_app": True}), \
                         mock.patch.object(win32_input, "_real_keybd_event") as send, \
                         mock.patch.object(win32_input.voice_key_physicalizer_windows, "begin_marked_voice_event", return_value=mock.Mock(marker=123)), \
                         mock.patch.object(win32_input.voice_key_physicalizer_windows, "wait_for_marked_voice_event", return_value=confirmed):
                        if confirmed:
                            win32_input._real_voice_event(0xA5, key_up)
                        else:
                            with self.assertRaises(win32_input.InputCleanupIncompleteError):
                                win32_input._real_voice_event(0xA5, key_up)
                        send.assert_called_once_with(0xA5, key_up, _extra_info=123)
                    fields = trace.emit.call_args.kwargs
                    self.assertEqual(fields["success"], confirmed)
                    self.assertEqual(fields["edge"], "up" if key_up else "down")
                    self.assertEqual(fields["attempt_id"], "attempt-1")
                    self.assertTrue(fields["foreground_is_app"])
                    self.assertEqual(fields["reason"], "local_hook_confirmed" if confirmed else "local_hook_confirmation_timeout")

    def test_broken_sink_cannot_turn_success_into_input_failure(self):
        trace = mock.Mock(enabled=True)
        trace.current_context.side_effect = RuntimeError("sink failed")
        with mock.patch.object(win32_input, "_diagnostic_trace", trace), \
             mock.patch.object(win32_input, "_real_keybd_event"), \
             mock.patch.object(win32_input.voice_key_physicalizer_windows, "begin_marked_voice_event", return_value=mock.Mock(marker=123)), \
             mock.patch.object(win32_input.voice_key_physicalizer_windows, "wait_for_marked_voice_event", return_value=True):
            win32_input._real_voice_event(0xA5, True)


class HostEvidenceTests(unittest.TestCase):
    def test_untrusted_fields_are_filtered(self):
        fields = RC003HidReportTap._source_evidence_fields(dict(
            source_binding_revision=1, key_scope="device_parameters", key_bus="bthledevice",
            winerror=2, caller_rva=True, instance_ref="a"*12, instance_matches_selected=True,
            path="private", name="private", caller_module="private.dll", instance_open_status=-1))
        self.assertEqual(fields, dict(reason="device_source_unverified", key_scope="device_parameters",
                                     key_bus="bthledevice", winerror=2, instance_ref="a"*12,
                                     instance_matches_selected=True))

    def test_loaded_script_absent_mismatched_and_matching_are_distinct(self):
        tap = RC003HidReportTap.__new__(RC003HidReportTap)
        for build, expected in ((None, None), ("0"*64, False), (GADGET_SCRIPT_BUILD_ID, True)):
            with mock.patch.object(tap, "_record_diagnostic") as record:
                tap._record_ready_diagnostics(dict(script_build_id=build, hook_installed=True))
            self.assertIs(record.call_args.kwargs["script_matches_expected"], expected)


EVIDENCE_HARNESS = r"""
rpc.exports.evidence = function () {
  resetFixture();
  const selected = containerSourceKey(ptr(0x900));
  enableSelected(selected);
  sourceApi.query = () => 2;
  let count = 0;
  const originalEvidence = registrySourceEvidence;
  registrySourceEvidence = () => { count++; return {key_scope:"device_parameters"}; };
  deviceCopy(0, RIGHT);
  ensure(count === 0, "diagnostics queried while disabled");
  handleControl({kind:"intercept_control",action:"renew",protocol:4,lease_ms:5000,
    selected_key:selected,source_binding_revision:1,source_diagnostics:true});
  deviceCopy(0, RIGHT); deviceCopy(0, RIGHT);
  ensure(count === 1, "source evidence not bounded per enable");
  deviceCopy(1, RIGHT); deviceCopy(1, RIGHT);
  ensure(count === 2, "first remote hid the second remote's diagnostic evidence");
  ensure(records.some(r => r.kind === "source_evidence" && r.key_scope === "device_parameters"), "missing read position");
  ensure(pendingCopyCandidate === null && boundCopyHandle === null, "diagnostics changed source policy");
  registrySourceEvidence = originalEvidence;
  return true;
};
rpc.exports.registryevidence = function () {
  const native = NativeFunction;
  const ntdll = Process.getModuleByName("ntdll.dll"), advapi = Process.getModuleByName("advapi32.dll");
  let opened = "", closed = 0;
  const fakeKey = "\\REGISTRY\\MACHINE\\SYSTEM\\CurrentControlSet\\Enum\\BTHLEDEVICE\\PrivateHardware\\PrivateInstance\\Device Parameters";
  NativeFunction = function (address, result, args) {
    if (address.equals(ntdll.getExportByName("NtQueryKey"))) return (key, kind, output, size, needed) => {
      ensure(kind === 3 && size === 8192, "unexpected registry query");
      output.writeU32(fakeKey.length*2); output.add(4).writeUtf16String(fakeKey); return 0;
    };
    if (address.equals(advapi.getExportByName("RegOpenKeyExW"))) return (key, name, options, access, output) => {
      ensure(key.equals(ptr("0xffffffff80000002")) && access === 1 && options === 0, "non-readonly query");
      opened = name.readUtf16String(); output.writePointer(ptr(0x900)); return 0;
    };
    if (address.equals(advapi.getExportByName("RegCloseKey"))) return key => {
      ensure(key.equals(ptr(0x900)), "closed source handle"); closed++; return 0;
    };
    return new native(address, result, args);
  };
  try {
    resetFixture(); selectedSourceKey = containerSourceKey(ptr(0x900));
    const evidence = registrySourceEvidence(ptr(0x999));
    ensure(evidence.key_scope === "device_parameters" && evidence.key_depth === 1 && evidence.key_bus === "bthledevice", "wrong location");
    ensure(evidence.instance_matches_selected && evidence.instance_ref.length === 12, "parent identity missing");
    ensure(opened === "SYSTEM\\CurrentControlSet\\Enum\\BTHLEDEVICE\\PrivateHardware\\PrivateInstance" && closed === 1, "wrong parent/lifetime");
    ensure(!JSON.stringify(evidence).includes("Private"), "raw identity leaked");
    return true;
  } finally { NativeFunction = native; }
};
rpc.exports.nativeregistry = function () {
  const advapi = Process.getModuleByName("advapi32.dll");
  const open = new originalNativeFunction(advapi.getExportByName("RegOpenKeyExW"), "long", ["pointer","pointer","uint","uint","pointer"]);
  const close = new originalNativeFunction(advapi.getExportByName("RegCloseKey"), "long", ["pointer"]);
  const key = Memory.alloc(8);
  ensure(open(ptr("0xffffffff80000002"), Memory.allocUtf16String("SOFTWARE"), 0, 1, key) === 0, "readonly open failed");
  try {
    const evidence = registrySourceEvidence(key.readPointer());
    ensure(evidence.key_name_status === 0 && evidence.key_scope === "other", "actual NtQueryKey failed");
    return true;
  } finally { close(key.readPointer()); }
};
"""


@unittest.skipUnless(os.name == "nt" and importlib.util.find_spec("frida"), "Windows Frida required")
class NativeEvidenceTests(unittest.TestCase):
    def test_opt_in_scope_privacy_and_native_query(self):
        import frida
        from tests.test_hid_copy_interception import HARNESS
        from tests.test_hid_device_source import SOURCE_HARNESS
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=subprocess.CREATE_NO_WINDOW)
        session = None
        try:
            session = frida.attach(child.pid)
            source = GADGET_SCRIPT
            for target, callback in (("target", "copyCallbacks"), ("closeTarget", "closeCallbacks"), ("deviceTarget", "deviceCallbacks")):
                prefix = "if (deviceTarget !== null) " if target == "deviceTarget" else ""
                source = source.replace(f"{prefix}Interceptor.attach({target}, {{", f"globalThis.{callback} = ({{")
            script = session.create_script(source + HARNESS + SOURCE_HARNESS + EVIDENCE_HARNESS)
            script.load()
            self.assertTrue(script.exports_sync.evidence())
            self.assertTrue(script.exports_sync.registryevidence())
            self.assertTrue(script.exports_sync.nativeregistry())
        finally:
            if session is not None:
                session.detach()
            child.terminate()
            child.wait(timeout=5)
