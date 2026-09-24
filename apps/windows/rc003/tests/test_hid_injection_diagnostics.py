"""Injection diagnostic transport with fake native APIs and isolated files only."""
import ctypes
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from ovb_rc003 import frida_compat as compat, frida_hid_tap_injector as injector
from ovb_rc003 import hid_elevation_windows as helper, hid_injection_diagnostics as diag
from ovb_rc003 import diagnostic_trace, logging_setup, log_export


def stage_error(stage='remote_write', api='WriteProcessMemory', code=5):
    cause = PermissionError('PRIVATE exception path')
    cause.winerror = code
    error = injector.HidInjectionStageError('hid_helper_remote_memory_failed', stage=stage, api=api)
    error.__cause__ = cause
    return error


class InjectionDiagnosticsTests(unittest.TestCase):
    def test_runtime_reasons_survive_child_transport_and_reject_arbitrary_text(self):
        for reason in diag.REASONS:
            with self.subTest(reason=reason):
                error = injector.HidInjectionStageError(
                    'hid_helper_runtime_preparation_failed', reason=reason)
                records = []
                with mock.patch.object(injector, 'inject_current_process', side_effect=error), \
                        mock.patch.object(diag, 'write_stdout', side_effect=records.append):
                    self.assertEqual(injector.main(['--pid', '2468']), 4)
                runner = mock.Mock(return_value=subprocess.CompletedProcess([], 4, diag.encode(records[0])))
                with self.assertRaises(compat.HidTapInjectionError) as caught:
                    compat.run_injector_subprocess(2468, _run=runner)
                self.assertEqual(diag.failure(caught.exception)['reason'], reason)
        for invalid in ('PRIVATE path or SID', [], {}, 123):
            self.assertIsNone(diag.sanitize({'reason': invalid})['reason'])
        self.assertIsNone(diag.sanitize({})['reason'])

    def test_runtime_reason_reaches_basic_detailed_report_and_export(self):
        reason = 'runtime_directory_acl_invalid'
        for enabled in (False, True):
            with self.subTest(trace_enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                handler = logging_setup.EvidenceFileHandler(root / 'logs' / 'app.log', asynchronous=True)
                logger = logging.Logger('isolated_runtime_reason', logging.INFO)
                logger.addHandler(handler)
                trace = diagnostic_trace.DiagnosticTrace(root, enabled=enabled)
                tap = compat.RC003HidReportTap(mock.Mock(), enabled=False, diagnostic_trace=trace)
                error = injector.HidInjectionStageError(
                    'PRIVATE exception path', stage='runtime_prepare', reason=reason)
                try:
                    with mock.patch.object(compat, '_LOGGER', logger):
                        tap._record_injection_failure(error)
                    self.assertTrue(handler.flush_pending())
                    self.assertTrue(trace.flush())
                    self.assertTrue(diagnostic_trace.flush_fault_report(root / 'logs'))
                    result = log_export.export_logs(root / 'export.zip', root=root)
                    self.assertFalse(result.incomplete)
                    with zipfile.ZipFile(result.path) as archive:
                        names = ['app.log', diagnostic_trace.REPORT_FILENAME]
                        if enabled:
                            names.append(diagnostic_trace.TRACE_FILENAME)
                        for name in names:
                            data = archive.read(name).decode('utf-8')
                            self.assertIn(reason, data)
                            self.assertNotIn('PRIVATE', data)
                finally:
                    trace.close()
                    handler.close()

    def test_detailed_failure_still_triggers_report_if_basic_logger_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            detailed = diagnostic_trace.DiagnosticTrace(root, enabled=True)
            tap = compat.RC003HidReportTap(mock.Mock(), enabled=False, diagnostic_trace=detailed)
            try:
                with mock.patch.object(compat, '_LOGGER') as logger:
                    logger.info.side_effect = RuntimeError('basic logger unavailable')
                    tap._record_injection_failure(stage_error())
                self.assertTrue(detailed.flush())
                self.assertTrue(diagnostic_trace.flush_fault_report(root / 'logs'))
                report = json.loads((root / 'logs' / diagnostic_trace.REPORT_FILENAME).read_text())
                self.assertEqual(len(report['incidents']), 1)
                self.assertEqual(report['incidents'][0]['failure_count'], 1)
                self.assertEqual(report['incidents'][0]['failure_key'], 'hid_injection:remote_write')
            finally:
                detailed.close()

    def test_basic_and_detailed_failure_count_once_per_actual_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handler = logging_setup.EvidenceFileHandler(root / 'logs' / 'app.log', asynchronous=True)
            logger = logging.Logger('isolated_injection', logging.INFO)
            logger.addHandler(handler)
            detailed = diagnostic_trace.DiagnosticTrace(root, enabled=True)
            tap = compat.RC003HidReportTap(mock.Mock(), enabled=False, diagnostic_trace=detailed)
            try:
                with mock.patch.object(compat, '_LOGGER', logger):
                    for index in range(2):
                        tap._diagnostic_injection_id = f'attempt-{index}'
                        tap._record_injection_failure(stage_error())
                        self.assertTrue(detailed.flush())
                        self.assertTrue(handler.flush_pending())
                        self.assertTrue(diagnostic_trace.flush_fault_report(root / 'logs'))
                        report = json.loads((root / 'logs' / diagnostic_trace.REPORT_FILENAME).read_text())
                        self.assertEqual(len(report['incidents']), 1)
                        incident = report['incidents'][0]
                        self.assertEqual(incident['failure_count'], index + 1)
                        self.assertEqual(incident['failure_key'], 'hid_injection:remote_write')
                        detail = [item for item in incident['events'] if item['event'] == 'hid_injection_result']
                        self.assertEqual(len(detail), index + 1)
                        self.assertTrue(all(item['winerror'] == 5 for item in detail))
            finally:
                detailed.close()
                handler.close()

    def test_child_result_reaches_parent_without_changing_exit_detail(self):
        for stage in diag.STAGES - {'complete', 'unknown'}:
            with self.subTest(stage=stage):
                records = []
                with mock.patch.object(injector, 'inject_current_process', side_effect=stage_error(stage)), \
                        mock.patch.object(diag, 'write_stdout', side_effect=records.append):
                    code = injector.main(['--pid', '2468'])
                self.assertEqual(code, 4)
                runner = mock.Mock(return_value=subprocess.CompletedProcess([], code, diag.encode(records[0])))
                with self.assertRaises(compat.HidTapInjectionError) as caught:
                    compat.run_injector_subprocess(2468, _run=runner)
                error = caught.exception
                self.assertEqual(str(error), 'injector_validation_failed')
                fields = diag.failure(error)
                self.assertEqual(fields['stage'], stage)
                self.assertEqual(fields['winerror'], 5)
                self.assertEqual(fields['error_type'], 'PermissionError')
                self.assertEqual(error.injection_diagnostic['execution_route'], 'direct_child')
                self.assertNotIn('PRIVATE', json.dumps(error.injection_diagnostic))

    def test_absent_invalid_legacy_and_mismatched_results_are_explicit(self):
        good = diag.result_record(success=False, target_pid=2468, exit_code=4, exc=stage_error())
        for payload, status in ((None, 'missing'), (b'not JSON', 'invalid'),
                                (b'x' * (diag.RESULT_LIMIT + 1), 'invalid'),
                                (diag.encode(dict(good, target_pid=999)), 'invalid'),
                                (diag.encode(dict(good, exit_code=5)), 'invalid')):
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 4, payload))
            with self.assertRaises(compat.HidTapInjectionError) as caught:
                compat.run_injector_subprocess(2468, _run=runner)
            self.assertEqual(str(caught.exception), 'injector_validation_failed')
            self.assertEqual(caught.exception.injection_diagnostic['result_status'], status)

    def test_real_child_stdout_transport_with_simulated_injection_failure(self):
        # Runs the source child and its Windows stdout handle, never an injector API.
        script = """
from unittest.mock import patch
from ovb_rc003 import frida_hid_tap_injector as injector
error = injector.HidInjectionStageError('hid_helper_remote_thread_failed')
error.winerror = 5
with patch.object(injector, 'inject_current_process', side_effect=error):
    raise SystemExit(injector.main(['--pid', '2468']))
"""
        def runner(_command, **kwargs):
            return subprocess.run([sys.executable, '-B', '-c', script], **kwargs)
        with self.assertRaises(compat.HidTapInjectionError) as caught:
            compat.run_injector_subprocess(2468, _run=runner)
        fields = caught.exception.injection_diagnostic
        self.assertEqual(fields['result_status'], 'received')
        self.assertEqual((fields['stage'], fields['winerror']), ('remote_thread', 5))
        self.assertIsInstance(fields['worker_pid'], int)

    def test_timeout_and_launch_error_keep_original_codes(self):
        for failure, detail, stage in (
            (subprocess.TimeoutExpired(['hidden'], 30), 'injector_timeout', 'child_wait'),
            (OSError(13, 'PRIVATE'), 'injector_launch_failed', 'child_launch'),
        ):
            with self.assertRaises(compat.HidTapInjectionError) as caught:
                compat.run_injector_subprocess(2468, _run=mock.Mock(side_effect=failure))
            self.assertEqual(str(caught.exception), detail)
            self.assertEqual(caught.exception.injection_diagnostic['stage'], stage)
            self.assertNotIn('PRIVATE', json.dumps(caught.exception.injection_diagnostic))

    def test_missing_codes_and_hresult_are_not_invented(self):
        error = RuntimeError('PRIVATE')
        self.assertIsNone(diag.failure(error)['winerror'])
        error.hresult = -2147024891
        self.assertEqual(diag.failure(error)['hresult'], -2147024891)
        self.assertIsNone(diag.failure(error)['winerror'])
        bad = dict(schema=1, exit_code=4, success=False, stage={}, text='PRIVATE')
        self.assertEqual(diag.decode(json.dumps(bad).encode(), exit_code=4)['result_status'], 'invalid')

    def test_diagnostic_failure_does_not_change_child_or_helper_outcome(self):
        with mock.patch.object(diag, 'result_record', side_effect=OSError('unavailable')), \
                mock.patch.object(injector, 'inject_current_process'), \
                mock.patch.object(helper, '_helper_task_instance', return_value=None), \
                mock.patch.object(helper, '_inject_once_impl', return_value=2468):
            self.assertEqual(injector.main(['--pid', '2468']), 0)
            self.assertIsNone(helper._inject_once())
        with mock.patch.object(helper, '_helper_task_instance', return_value=None), \
                mock.patch.object(helper, '_inject_once_impl', side_effect=stage_error()), \
                mock.patch.object(helper, '_publish_injection_result', side_effect=OSError('unavailable')):
            with self.assertRaises(injector.HidInjectionStageError):
                helper._inject_once()

    def _native_kernel(self):
        kernel = mock.Mock()
        for name, value in (('OpenProcess', 11), ('VirtualAllocEx', 22),
                            ('GetModuleHandleW', 33), ('GetProcAddress', 44),
                            ('CreateRemoteThread', 55), ('WaitForSingleObject', 0)):
            getattr(kernel, name).return_value = value
        def write(_process, _remote, _buffer, length, written):
            written._obj.value = length
            return 1
        kernel.WriteProcessMemory.side_effect = write
        def exit_code(_thread, value):
            value._obj.value = 99
            return 1
        kernel.GetExitCodeThread.side_effect = exit_code
        kernel.CloseHandle.side_effect = lambda *_: ctypes.set_last_error(999)
        kernel.VirtualFreeEx.side_effect = lambda *_: ctypes.set_last_error(999)
        return kernel

    def test_native_error_is_captured_before_cleanup_overwrites_it(self):
        for api, stage, result in (
            ('OpenProcess', 'target_open', 0), ('VirtualAllocEx', 'remote_allocate', 0),
            ('WriteProcessMemory', 'remote_write', 0), ('GetModuleHandleW', 'load_library_lookup', 0),
            ('GetProcAddress', 'load_library_lookup', 0), ('CreateRemoteThread', 'remote_thread', 0),
            ('WaitForSingleObject', 'remote_wait', injector.WAIT_FAILED),
            ('GetExitCodeThread', 'remote_exit', 0),
        ):
            with self.subTest(api=api):
                kernel = self._native_kernel()
                def fail(*_, result=result):
                    ctypes.set_last_error(5)
                    return result
                getattr(kernel, api).side_effect = fail
                with mock.patch.object(ctypes, 'WinDLL', return_value=kernel):
                    with self.assertRaises(injector.HidInjectionStageError) as caught:
                        injector.inject_library(2468, Path('test.dll'))
                fields = diag.failure(caught.exception)
                self.assertEqual((fields['stage'], fields['api'], fields['winerror']), (stage, api, 5))

    def test_timeout_and_remote_zero_are_results_not_local_last_error(self):
        for api, value in (('WaitForSingleObject', injector.WAIT_TIMEOUT), ('GetExitCodeThread', 0)):
            kernel = self._native_kernel()
            if api == 'WaitForSingleObject':
                kernel.WaitForSingleObject.return_value = value
            else:
                def remote_zero(_thread, result):
                    result._obj.value = 0
                    return 1
                kernel.GetExitCodeThread.side_effect = remote_zero
            ctypes.set_last_error(123)
            with mock.patch.object(ctypes, 'WinDLL', return_value=kernel):
                with self.assertRaises(injector.HidInjectionStageError) as caught:
                    injector.inject_library(2468, Path('test.dll'))
            fields = diag.failure(caught.exception)
            self.assertIsNone(fields['winerror'])
            self.assertEqual(fields['return_value'], value)

    def test_partial_memory_write_records_counts_without_inventing_error(self):
        kernel = self._native_kernel()
        def partial(_process, _remote, _buffer, _length, written):
            written._obj.value = 2
            return 1
        kernel.WriteProcessMemory.side_effect = partial
        with mock.patch.object(ctypes, 'WinDLL', return_value=kernel):
            with self.assertRaises(injector.HidInjectionStageError) as caught:
                injector.inject_library(2468, Path('test.dll'))
        fields = diag.failure(caught.exception)
        self.assertEqual(fields['actual_bytes'], 2)
        self.assertGreater(fields['expected_bytes'], 2)
        self.assertIsNone(fields['winerror'])

    def test_debug_privilege_not_assigned_retains_1300(self):
        kernel, advapi = mock.Mock(), mock.Mock()
        def adjust(*_):
            ctypes.set_last_error(1300)
            return 1
        advapi.AdjustTokenPrivileges.side_effect = adjust
        kernel.CloseHandle.side_effect = lambda *_: ctypes.set_last_error(999)
        with mock.patch.object(ctypes, 'WinDLL', side_effect=lambda name, **_: advapi if name == 'advapi32' else kernel):
            with self.assertRaises(PermissionError) as caught:
                injector.enable_debug_privilege()
        self.assertEqual(diag.failure(caught.exception)['winerror'], 1300)

    def test_helper_instance_is_the_scheduler_guid(self):
        instance = '123456781234123412341234567890ab'
        task = mock.Mock()
        task.GetInstances.return_value.Count = 1
        task.GetInstances.return_value.Item.return_value.InstanceGuid = '{12345678-1234-1234-1234-1234567890ab}'
        with mock.patch.object(helper.sys, 'frozen', True, create=True), \
                mock.patch.object(helper, '_task_service_session'), \
                mock.patch.object(helper, 'current_user_sid', return_value='S-1-5-21-1-2-3-1001'), \
                mock.patch.object(helper, '_find_registered_task', return_value=task):
            self.assertEqual(helper._helper_task_instance(), instance)
            task.GetInstances.return_value.Count = 2
            self.assertIsNone(helper._helper_task_instance())

    def test_helper_protected_result_reaches_parent_and_rejects_stale_results(self):
        instance = '123456781234123412341234567890ab'
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(helper, '_diagnostic_result_path', return_value=(Path(tmp, 'result.json'), 'test-sid')), \
                mock.patch.object(helper, '_apply_path_security'):
            self.assertEqual(helper._read_injection_result(instance, 40)['result_status'], 'missing')
            error = helper.HidElevationError('hid_helper_remote_memory_failed')
            error.__cause__ = stage_error()
            error.injection_diagnostic = {'target_pid': 2468, 'reason': 'runtime_directory_acl_invalid'}
            with mock.patch.object(helper, '_helper_task_instance', return_value=instance), \
                    mock.patch.object(helper, '_inject_once_impl', side_effect=error):
                with self.assertRaises(helper.HidElevationError):
                    helper._inject_once()
            self.assertEqual(helper._read_injection_result('different', 40)['result_status'], 'instance_mismatch')
            self.assertEqual(helper._read_injection_result(instance, 41)['result_status'], 'invalid')
            running = mock.Mock(State=0, InstanceGuid=instance)
            task = mock.Mock(LastTaskResult=40)
            task.Run.return_value = running
            with mock.patch.object(helper, '_task_service_session'), \
                    mock.patch.object(helper, '_find_registered_task', return_value=task):
                with self.assertRaises(compat.HidTapInjectionError) as caught:
                    compat.run_injector_subprocess(2468, frozen=True, _is_elevated=lambda: False,
                        _registered_injector=lambda _: helper._run_task('task'))
            fields = diag.failure(caught.exception)
            self.assertEqual(str(caught.exception), 'hid_helper_remote_memory_failed')
            self.assertEqual(fields['winerror'], 5)
            self.assertEqual(fields['stage'], 'remote_write')
            self.assertEqual(fields['error_type'], 'PermissionError')
            self.assertEqual(fields['reason'], 'runtime_directory_acl_invalid')
            self.assertEqual(caught.exception.injection_diagnostic['execution_route'], 'registered_helper')
            Path(tmp, 'result.json').write_bytes(b'incomplete JSON')
            self.assertEqual(helper._read_injection_result(instance, 40)['result_status'], 'invalid')
            Path(tmp, 'result.json').write_bytes(b'x' * (diag.RESULT_LIMIT + 1))
            self.assertEqual(helper._read_injection_result(instance, 40)['result_status'], 'invalid')

    def test_protected_cache_failure_cannot_write_elsewhere(self):
        with mock.patch.object(helper, '_diagnostic_result_path', side_effect=helper.HidElevationError('protected_path_reparse_point')), \
                mock.patch.object(helper.tempfile, 'NamedTemporaryFile') as create:
            helper._publish_injection_result({}, 'instance')
            create.assert_not_called()
            self.assertEqual(helper._read_injection_result('instance', 40)['result_status'], 'unavailable')

    def test_missing_helper_result_keeps_the_original_stage_exit(self):
        running = mock.Mock(State=0, InstanceGuid=None)
        task = mock.Mock(LastTaskResult=40)
        task.Run.return_value = running
        with mock.patch.object(helper, '_task_service_session'), \
                mock.patch.object(helper, '_find_registered_task', return_value=task):
            with self.assertRaises(helper.HidElevationError) as caught:
                helper._run_task('task')
        self.assertEqual(str(caught.exception), 'hid_helper_remote_memory_failed')
        self.assertEqual(caught.exception.injection_diagnostic['result_status'], 'missing')

    def test_helper_result_for_another_host_is_not_attributed_to_this_attempt(self):
        error = helper.HidElevationError('hid_helper_remote_memory_failed')
        error.injection_diagnostic = {'target_pid': 999, 'winerror': 5, 'result_status': 'received'}
        with mock.patch.object(helper, '_is_windows', return_value=True), \
                mock.patch.object(helper, 'inspect_installed_helper', return_value=helper.HidHelperState(True)), \
                mock.patch.object(helper, 'current_user_sid', return_value='S-1-5-21-1-2-3-1001'):
            with self.assertRaises(helper.HidElevationError) as caught:
                helper.run_registered_injector(2468, _host_pid=lambda: 2468,
                    _run_registered_task=mock.Mock(side_effect=error))
        self.assertEqual(str(caught.exception), 'hid_helper_remote_memory_failed')
        self.assertEqual(caught.exception.injection_diagnostic, {'result_status': 'invalid'})

    def test_cache_requires_the_existing_protected_directory_acl(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(helper, 'current_user_sid', return_value='S-1-5-21-1-2-3-1001'), \
                mock.patch.object(helper, '_program_files_root', return_value=Path(tmp)), \
                mock.patch.object(helper, '_read_path_security_sddl', return_value='untrusted'), \
                mock.patch.object(helper.tempfile, 'NamedTemporaryFile') as create:
            with self.assertRaises(helper.HidElevationError):
                helper._diagnostic_result_path()
            helper._publish_injection_result({}, 'instance')
            create.assert_not_called()

    def test_log_failure_and_diagnostic_collection_failure_do_not_change_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handler = logging_setup.EvidenceFileHandler(root / 'logs' / 'app.log', asynchronous=True)
            logger = logging.Logger('isolated_failed_log', logging.INFO)
            logger.addHandler(handler)
            tap = compat.RC003HidReportTap(mock.Mock(), enabled=False)
            def fail(_pid):
                tap.stop_event.set()
                raise compat.HidTapInjectionError('injector_validation_failed')
            tap.injector = fail
            try:
                with mock.patch.object(compat, '_LOGGER', logger), \
                        mock.patch.object(handler, '_open', side_effect=PermissionError('PRIVATE')), \
                        mock.patch.object(diag, 'failure', side_effect=RuntimeError('PRIVATE')), \
                        mock.patch.object(compat.frida_hid_tap_runtime, 'find_rc003_hidogatt_host_pid', return_value=2468), \
                        mock.patch.object(compat.socket, 'socket'):
                    tap._run()
                    self.assertFalse(handler.flush_pending())
                self.assertEqual(tap._status_detail, 'injector_validation_failed')
                self.assertEqual(tap.status, compat.HidTapState.FAILED.value)
                tap.report_handler.assert_not_called()
            finally:
                handler.close()

    def test_failure_reaches_basic_fault_report_and_export_with_trace_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handler = logging_setup.EvidenceFileHandler(root / 'logs' / 'app.log', asynchronous=True)
            logger = logging.Logger('isolated_injection', logging.INFO)
            logger.addHandler(handler)
            detailed = diagnostic_trace.DiagnosticTrace(root, enabled=False)
            record = diag.result_record(success=False, target_pid=2468, exit_code=4, exc=stage_error())
            tap = compat.RC003HidReportTap(mock.Mock(), enabled=False, diagnostic_trace=detailed)
            def fail(_pid):
                try:
                    compat.run_injector_subprocess(2468, _run=mock.Mock(
                        return_value=subprocess.CompletedProcess([], 4, diag.encode(record))))
                finally:
                    tap.stop_event.set()
            tap.injector = fail
            try:
                with mock.patch.object(compat, '_LOGGER', logger), \
                        mock.patch.object(compat.frida_hid_tap_runtime, 'find_rc003_hidogatt_host_pid', return_value=2468), \
                        mock.patch.object(compat.socket, 'socket'):
                    tap._run()
                result = log_export.export_logs(root / 'export.zip', root=root)
                self.assertFalse(result.incomplete)
                with zipfile.ZipFile(result.path) as archive:
                    basic = archive.read('app.log').decode()
                    saved = json.loads(archive.read(diagnostic_trace.REPORT_FILENAME))
                    self.assertIn('remote_write', basic)
                    self.assertIn('"winerror": 5', basic)
                    self.assertIn('injection_attempt_id', basic)
                    self.assertIn('hid_injection:remote_write', json.dumps(saved))
                    self.assertNotIn('PRIVATE', basic + json.dumps(saved))
                    self.assertNotIn(diagnostic_trace.TRACE_FILENAME, archive.namelist())
            finally:
                handler.close()
                detailed.close()
