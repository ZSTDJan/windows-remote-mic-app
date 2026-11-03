"""Paired-device scan failures retain bounded evidence without changing discovery."""
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile
import remote_scan_bootstrap as bootstrap

from ovb_rc003 import ble_transport_winrt, log_export, logging_setup
from ovb_rc003 import remote_selection as selection, windows_diagnostics


class RemoteScanDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def scan_child(self, devices=(), failure=None):
        query = mock.AsyncMock(side_effect=failure) if failure else mock.AsyncMock(return_value=devices)
        api = SimpleNamespace(
            bluetooth_le_device=SimpleNamespace(get_device_selector_from_pairing_state=lambda paired: "paired"),
            device_information=SimpleNamespace(find_all_async_aqs_filter_and_additional_properties=query))
        result, evidence = self.root / "rows.json", self.root / "diagnostic.json"
        with mock.patch.object(ble_transport_winrt, "_import_winrt", return_value=api):
            code = selection.scan_main([str(result), str(evidence)])
        return code, result, json.loads(evidence.read_text(encoding="utf-8").splitlines()[-1])

    def test_success_and_identity_skip_are_distinct_without_names(self):
        devices = [SimpleNamespace(name="Chromecast Remote", properties={
            "System.Devices.Aep.ContainerId": "11111111-1111-4111-8111-111111111111"}),
            SimpleNamespace(name="PRIVATE DEVICE NAME", properties={})]
        code, result, evidence = self.scan_child(devices)
        self.assertEqual(code, 0)
        self.assertEqual(len(selection._read_scan_result(str(result), code)), 1)
        self.assertEqual(evidence["state"], "success")
        self.assertEqual(evidence["enumerated"], 2)
        self.assertEqual(evidence["identity_failed"], 1)
        self.assertEqual(evidence["supported"], 1)
        self.assertNotIn("PRIVATE", json.dumps(evidence))
        self.assertNotIn("11111111", json.dumps(evidence))

    def test_native_error_codes_survive_child_boundary_without_text(self):
        failure = OSError(5, "PRIVATE PATH OR DEVICE")
        failure.winerror = 5
        code, result, evidence = self.scan_child(failure=failure)
        self.assertEqual(code, 1)
        self.assertFalse(result.exists())
        self.assertEqual(evidence["failure_stage"], "enumerate")
        self.assertEqual(evidence["stage"], "loop_shutdown")
        safe = selection._read_scan_diagnostic(self.root / "diagnostic.json")
        self.assertEqual(safe["child_errors"][0]["winerror"], 5)
        self.assertNotIn("PRIVATE", json.dumps(safe))

    def test_untrusted_diagnostic_fields_cannot_reach_logs(self):
        path = self.root / "diagnostic.json"
        path.write_text(json.dumps(dict(schema=1, stage="enumerate", state="failed",
            path="PRIVATE", errors=[dict(error_type="PRIVATE/PATH", winerror=5, text="PRIVATE")])) )
        safe = selection._read_scan_diagnostic(path)
        self.assertEqual(safe["child_errors"], [{"error_type": "Exception", "winerror": 5}])
        self.assertNotIn("PRIVATE", json.dumps(safe))
        path.write_text("{" + "x" * 65536)
        self.assertEqual(selection._read_scan_diagnostic(path),
                         {"evidence_unavailable": True, "evidence_reason": "too_large"})

    def run_parent(self, child_code, *, logger=None, timeout=2.0, cancel=None):
        original = windows_diagnostics._run_ble_diagnostics_subprocess
        def run(command, **kwargs):
            return original([sys.executable, "-B", "-c", child_code, *command[-2:]], **kwargs)
        with mock.patch.object(windows_diagnostics, "_run_ble_diagnostics_subprocess", side_effect=run), \
             mock.patch.object(windows_diagnostics, "BLE_DISCOVERY_TIMEOUT_SECONDS", timeout):
            return selection.scan_paired(cancel_event=cancel, logger=logger)

    def record(self, logger):
        return json.loads(logger.log.call_args.args[2])

    def test_real_child_exit_timeout_cancel_and_bad_result_remain_distinct(self):
        cases = [("import sys;sys.exit(7)", "failed", 2.0),
                 ("import time;time.sleep(30)", "timeout", 0.1),
                 ("import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('[')", "failed", 2.0)]
        for code, state, timeout in cases:
            logger = mock.Mock()
            with self.subTest(state=state, code=code), self.assertRaises(Exception):
                self.run_parent(code, logger=logger, timeout=timeout)
            row = self.record(logger)
            self.assertEqual(row["state"], state)
            if "exit(7)" in code:
                self.assertEqual(row["exit_code"], 7)
            if "write_text" in code:
                self.assertEqual(row["errors"][0]["error_type"], "JSONDecodeError")
        cancel = threading.Event()
        cancel.set()
        logger = mock.Mock()
        with self.assertRaises(windows_diagnostics.BleDiscoveryCancelledError):
            self.run_parent("raise RuntimeError('must not run')", logger=logger, cancel=cancel)
        self.assertEqual(self.record(logger)["state"], "cancelled")

    def test_temporary_directory_failure_is_logged_without_changing_exception(self):
        logger = mock.Mock()
        failure = PermissionError(5, "PRIVATE DIRECTORY")
        with mock.patch.object(selection.tempfile, "TemporaryDirectory", side_effect=failure), \
             self.assertRaises(PermissionError):
            selection.scan_paired(logger=logger)
        row = self.record(logger)
        self.assertEqual(row["stage"], "temporary_directory")
        self.assertNotIn("PRIVATE", json.dumps(row))

    def test_collection_failures_and_interrupted_last_write_are_distinct(self):
        path = self.root / "diagnostic.json"
        self.assertEqual(selection._read_scan_diagnostic(path)["evidence_reason"], "missing")
        path.touch()
        self.assertEqual(selection._read_scan_diagnostic(path)["evidence_reason"], "empty")
        path.write_bytes(b'not json')
        self.assertEqual(selection._read_scan_diagnostic(path)["evidence_reason"], "invalid_record")
        diagnostic = bootstrap.ScanDiagnostic(path)
        diagnostic.stage("loop_init")
        with path.open("ab") as stream:
            stream.write(b'{"schema":2,"stage":')
        record = selection._read_scan_diagnostic(path)
        self.assertEqual(record["child_stage"], "loop_init")
        self.assertTrue(record["evidence_partial"])
        with mock.patch.object(Path, "open", side_effect=PermissionError(13, "PRIVATE")):
            record = selection._read_scan_diagnostic(path)
        self.assertEqual(record["evidence_reason"], "read_failed")
        self.assertEqual(record["evidence_errors"][0]["errno"], 13)
        self.assertNotIn("PRIVATE", json.dumps(record))

    def test_initialization_is_recorded_before_the_coroutine_runs(self):
        logger = mock.Mock()
        code = ("import sys,time\nimport remote_scan_bootstrap as b\n"
                "sys.argv=[sys.argv[0],b.FLAG,*sys.argv[1:]]\nb.activate()\n"
                "b.mark('loop_init')\ntime.sleep(30)")
        with self.assertRaises(windows_diagnostics.BleDiscoveryCancelledError):
            self.run_parent(code, logger=logger, timeout=0.5)
        row = self.record(logger)
        self.assertEqual((row["state"], row["child_stage"]), ("timeout", "loop_init"))
        self.assertTrue(row["child_started"])
        self.assertTrue(row["child_exited"])
        self.assertIsNotNone(row["child_exit_code"])

    def test_cancel_running_child_preserves_progress_and_confirms_exit(self):
        cancel = threading.Event()
        timer = threading.Timer(0.6, cancel.set)
        code = ("import sys,time\nimport remote_scan_bootstrap as b\n"
                "sys.argv=[sys.argv[0],b.FLAG,*sys.argv[1:]]\nb.activate()\n"
                "b.mark('enumerate')\ntime.sleep(30)")
        logger = mock.Mock()
        timer.start()
        try:
            with self.assertRaises(windows_diagnostics.BleDiscoveryCancelledError):
                self.run_parent(code, logger=logger, timeout=4, cancel=cancel)
        finally:
            timer.cancel()
            timer.join()
        row = self.record(logger)
        self.assertEqual((row["state"], row["child_stage"]), ("cancelled", "enumerate"))
        self.assertTrue(row["child_exited"])

    def test_ordinary_process_does_not_install_fatal_hook(self):
        old_hook = sys.excepthook
        with mock.patch.object(sys, "argv", ["app", "--settings"]):
            self.assertFalse(bootstrap.activate())
        self.assertIs(sys.excepthook, old_hook)

    def test_inherited_channel_survives_unwritable_path(self):
        logger = mock.Mock()
        code = ("import pathlib,sys\nimport remote_scan_bootstrap as b\n"
                "result=sys.argv[1]\nsys.argv=[sys.argv[0],b.FLAG,result,sys.argv[2]]\n"
                "b.activate()\nb._active.path=result+'/not-a-directory/diagnostic'\n"
                "b.mark('enumerate')\npathlib.Path(result).write_text('[]')\n"
                "b._active.record.update(stage='finished',state='success')\n"
                "b._active.save()\nb._active.close()")
        self.assertEqual(self.run_parent(code, logger=logger), [])
        row = self.record(logger)
        self.assertEqual(row["child_stage"], "finished")
        self.assertEqual(row["child_state"], "success")
        self.assertNotIn("evidence_unavailable", row)

    def test_channel_open_failure_does_not_block_query(self):
        original_open = Path.open
        def open_file(path, mode="r", *args, **kwargs):
            if path.name == 'diagnostic.json' and mode == 'wb':
                raise PermissionError(13, "PRIVATE")
            return original_open(path, mode, *args, **kwargs)
        logger = mock.Mock()
        with mock.patch.object(Path, "open", open_file):
            self.assertEqual(self.run_parent(
                "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('[]')", logger=logger), [])
        self.assertEqual(self.record(logger)["evidence_channel"], "open_failed")

    @unittest.skipUnless(sys.platform == 'win32', 'Windows handle inheritance')
    def test_failed_handle_inheritance_does_not_block_query(self):
        logger = mock.Mock()
        with mock.patch.object(os, 'set_handle_inheritable', side_effect=OSError(5, 'PRIVATE')):
            self.assertEqual(self.run_parent(
                "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('[]')", logger=logger), [])
        self.assertEqual(self.record(logger)["evidence_channel"], "inherit_failed")

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native early failure')
    def test_early_native_failure_and_frame_location_survive_without_package_import(self):
        code = ("import ctypes,sys\nimport remote_scan_bootstrap as b\n"
                "sys.argv=[sys.argv[0],b.FLAG,*sys.argv[1:]]\nb.activate()\n"
                "load=ctypes.WinDLL('kernel32',use_last_error=True).LoadLibraryW\n"
                "load.argtypes=[ctypes.c_wchar_p];load.restype=ctypes.c_void_p\n"
                "class FailApplicationImport:\n"
                " def find_spec(self,name,*args):\n"
                "  if name=='ovb_rc003':\n"
                "   if not load('RemoteMic_absent_early_component_20261103.dll'):\n"
                "    raise ctypes.WinError(ctypes.get_last_error())\n"
                "sys.meta_path.insert(0,FailApplicationImport())\nb.source_main()")
        logger = mock.Mock()
        with self.assertRaises(selection.SelectionError):
            self.run_parent(code, logger=logger, timeout=5)
        row = self.record(logger)
        self.assertEqual(row["exit_code"], 1)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["failure_stage"], "entrypoint_import")
        self.assertEqual(row["child_errors"][0]["winerror"], 126)
        self.assertEqual(row["child_errors"][0]["frames"][-1]["module"], 'remote_scan_bootstrap')
        self.assertNotIn('RemoteMic_absent', json.dumps(row))

    def test_observer_failure_does_not_turn_success_into_failure(self):
        logger = mock.Mock()
        logger.log.side_effect = OSError("observer failed")
        rows = self.run_parent(
            "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('[]')", logger=logger)
        self.assertEqual(rows, [])

    @unittest.skipUnless(sys.platform == "win32", "Windows native component loading")
    def test_native_missing_component_evidence_survives_actual_exported_zip(self):
        logger = logging.Logger("ovb_rc003.scan-validation", logging.INFO)
        handler = logging_setup.EvidenceFileHandler(self.root / "logs" / "app.log",
            asynchronous=True, encoding="utf-8")
        handler.addFilter(logging_setup.PrivacySafeExceptionFilter())
        handler.setFormatter(logging.Formatter(logging_setup.LOG_FORMAT))
        logger.addHandler(handler)
        try:
            # The DLL load really fails in an owned native child, rather than a mocked OSError.
            code = ("import ctypes,sys\nfrom ovb_rc003 import remote_selection as s,ble_transport_winrt as b\n"
                    "load=ctypes.WinDLL('kernel32',use_last_error=True).LoadLibraryW\n"
                    "load.argtypes=[ctypes.c_wchar_p];load.restype=ctypes.c_void_p\n"
                    "def missing_component():\n"
                    " ctypes.set_last_error(0)\n"
                    " if not load('RemoteMic_scan_component_not_present_20261102.dll'):\n"
                    "  raise ctypes.WinError(ctypes.get_last_error())\n"
                    "b._import_winrt=missing_component\nsys.exit(s.scan_main(sys.argv[1:]))")
            with self.assertRaises(selection.SelectionError):
                self.run_parent(code, logger=logger, timeout=5.0)
            early = ("import ctypes,sys\nimport remote_scan_bootstrap as b\n"
                     "sys.argv=[sys.argv[0],b.FLAG,*sys.argv[1:]]\nb.activate()\n"
                     "b.mark('entrypoint_import')\n"
                     "load=ctypes.WinDLL('kernel32',use_last_error=True).LoadLibraryW\n"
                     "load.argtypes=[ctypes.c_wchar_p];load.restype=ctypes.c_void_p\n"
                     "if not load('RemoteMic_absent_before_import_20261103.dll'):\n"
                     " raise ctypes.WinError(ctypes.get_last_error())")
            with self.assertRaises(selection.SelectionError):
                self.run_parent(early, logger=logger, timeout=5)
            with self.assertRaises(windows_diagnostics.BleDiscoveryCancelledError):
                self.run_parent("import sys,time\nimport remote_scan_bootstrap as b\n"
                    "sys.argv=[sys.argv[0],b.FLAG,*sys.argv[1:]]\nb.activate()\n"
                    "b.mark('loop_init')\ntime.sleep(30)", logger=logger, timeout=.5)
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(windows_diagnostics.BleDiscoveryCancelledError):
                self.run_parent('raise AssertionError', logger=logger, cancel=cancel)
            self.assertEqual(self.run_parent(
                "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('[]')", logger=logger), [])
            with self.assertRaises(selection.SelectionError):
                self.run_parent('import sys;sys.exit(7)', logger=logger)
            self.assertTrue(handler.flush_pending())
            destination = self.root / "diagnostic.zip"
            exported = log_export.export_logs(destination, root=self.root)
            self.assertEqual(exported.outcome, "exported")
            with zipfile.ZipFile(destination) as archive:
                text = archive.read("app.log").decode("utf-8")
                rows = [json.loads(line.split('Remote device scan: ', 1)[1])
                        for line in text.splitlines() if 'Remote device scan: ' in line]
                self.assertEqual(len(rows), 6)
                row = rows[0]
                self.assertEqual(row["failure_stage"], "winrt_import")
                self.assertEqual(row["child_errors"][0].get("winerror"), 126, row)
                self.assertEqual(row["exit_code"], 1)
                self.assertNotIn("RemoteMic_scan_component_not_present", text)
                self.assertEqual(rows[1]['failure_stage'], 'entrypoint_import')
                self.assertEqual(rows[1]['child_errors'][0]['winerror'], 126)
                self.assertEqual((rows[2]['state'], rows[2]['child_stage']), ('timeout', 'loop_init'))
                self.assertEqual(rows[3]['state'], 'cancelled')
                self.assertFalse(rows[3]['child_started'])
                self.assertEqual(rows[4]['state'], 'success')
                self.assertEqual((rows[5]['exit_code'], rows[5]['evidence_reason']), (7, 'empty'))
                manifest = json.loads(archive.read('export-info.json'))
                self.assertFalse(manifest['incomplete'])
                self.assertNotIn('RemoteMic_absent_before_import', text)
                self.assertNotIn(str(self.root), text)
        finally:
            logger.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    unittest.main()
