"""Shutdown evidence with fake processes only; no device or input operations."""
import ctypes
import logging
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile
import json
import subprocess
import sys

from ovb_rc003 import bridge_launcher, chromecast_client, diagnostic_trace, log_export
from ovb_rc003.chromecast_voice_host import VoiceHost
from ovb_rc003 import chromecast_diagnostics_windows as system_evidence
from ovb_rc003.chromecast_channel import Channel, SessionIdentity, ChannelError
from ovb_rc003.chromecast_buttons import empty_stop_details


class ShutdownDiagnosticsTests(unittest.TestCase):
    def test_voice_cleanup_failure_reports_owned_resources(self):
        owner = SimpleNamespace(_logger=logging.getLogger('ovb_rc003'),
                                _voice_audio=SimpleNamespace(writer=None, sink=None))
        from ovb_rc003.app import RC003App
        services = mock.Mock()
        services._voice_audio = owner._voice_audio
        services._logger = owner._logger
        host = RC003App._create_chromecast_voice_host(services)
        host.thread = SimpleNamespace(join=lambda _: None, is_alive=lambda: False)
        host.closed = False
        with mock.patch.object(host, '_close_resources'):
            with self.assertLogs('ovb_rc003', level='WARNING') as captured:
                with self.assertRaises(RuntimeError):
                    host.stop()
        self.assertIn('thread_alive=False closed=False', captured.output[0])
        self.assertIn('writer_present=False playback_present=False', captured.output[0])

    def test_abnormal_exit_is_visible_without_retaining_a_dead_owner(self):
        client = chromecast_client.Client('a' * 64, mode='run', on_edge=lambda _: None)
        client.thread = SimpleNamespace(join=lambda _: None, is_alive=lambda: False)
        client._process = 42
        client._started = True
        def exit_code(handle, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = 2
            return True
        client._kernel = SimpleNamespace(WaitForSingleObject=lambda *_: 0,
            GetExitCodeProcess=exit_code, CloseHandle=mock.Mock())
        with self.assertLogs('ovb_rc003', level='INFO') as captured:
            client.stop()
        output = '\n'.join(captured.output)
        self.assertIn('exit_code=2', output)
        self.assertTrue(client.cleanup_confirmed)
        self.assertFalse(client.exit_succeeded)
        self.assertIsNone(client._process)
        self.assertNotIn(client.identity.token, output)
        with self.assertNoLogs('ovb_rc003'):
            client.stop()

    def test_exception_exit_queues_system_evidence_after_os_confirms_exit(self):
        client = chromecast_client.Client('a' * 64, mode='run', on_edge=lambda _: None)
        client._process = 42
        client._started = True
        client._process_identities['launcher'] = SimpleNamespace(pid=321, born=456)
        def exit_code(_handle, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = 0xc0000005
            return True
        client._kernel = SimpleNamespace(WaitForSingleObject=lambda *_: 0,
            GetExitCodeProcess=exit_code, CloseHandle=mock.Mock())
        with mock.patch.object(system_evidence, 'request_process_exit') as request, \
             self.assertLogs('ovb_rc003', level='INFO') as captured:
            client._confirm_exit()
        request.assert_called_once_with(client.identity.generation, 321, 456, 0xc0000005)
        self.assertIn('cause=unconfirmed', '\n'.join(captured.output))
        self.assertIn('exit_hex=0xC0000005', '\n'.join(captured.output))
        self.assertTrue(client.cleanup_confirmed)
        self.assertFalse(client.exit_succeeded)
        self.assertIsNone(client._process)

    def test_timeout_snapshots_threads_but_success_does_not(self):
        for stopped in (False, True):
            handle = SimpleNamespace(is_alive=True, request_stop=mock.Mock(), wait=mock.Mock(return_value=stopped))
            with mock.patch.object(bridge_launcher, '_in_process_handle', handle), \
                 mock.patch.object(diagnostic_trace, 'log_shutdown_threads') as snapshot:
                self.assertEqual(bridge_launcher.stop_in_process_bridge(timeout=.01), stopped)
                self.assertEqual(snapshot.call_count, int(not stopped))

    def test_snapshot_exports_code_locations_without_source_or_locals(self):
        code = SimpleNamespace(co_name='stop', co_filename='C:/PRIVATE/user/program.py')
        frame = SimpleNamespace(f_globals={'__name__': 'ovb_rc003.chromecast_client'},
            f_code=code, f_lineno=105, f_back=None, f_locals={'token': 'PRIVATE_TOKEN'})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'logs').mkdir()
            handler = logging.FileHandler(root / 'logs/app.log', encoding='utf-8')
            logger = logging.getLogger('shutdown-export-test')
            logger.addHandler(handler)
            try:
                with mock.patch('sys._current_frames', return_value={threading.get_ident(): frame}):
                    diagnostic_trace.log_shutdown_threads(logger)
            finally:
                logger.removeHandler(handler)
                handler.close()
            destination = root / 'logs.zip'
            self.assertEqual(log_export.export_logs(destination, root=root).outcome, 'exported')
            with zipfile.ZipFile(destination) as archive:
                output = archive.read('app.log').decode('utf-8')
            self.assertIn('ovb_rc003.chromecast_client:stop:105', output)
            self.assertNotIn('PRIVATE', output)

    def test_snapshot_failure_is_diagnostic_only(self):
        with mock.patch('sys._current_frames', side_effect=RuntimeError('PRIVATE')):
            with self.assertLogs('ovb_rc003', level='WARNING') as captured:
                diagnostic_trace.log_shutdown_threads(logging.getLogger('ovb_rc003'))
        self.assertIn('error_type=RuntimeError', '\n'.join(captured.output))
        self.assertNotIn('PRIVATE', '\n'.join(captured.output))

    def test_compiled_frame_absence_still_reports_cleanup_and_collection_result(self):
        logger = logging.getLogger('ovb_rc003')
        progress = diagnostic_trace.CleanupProgress(logger, 'receiver', 'a' * 12)
        progress.update('pipe_close', 'begin')
        progress.update('pipe_close', 'done')
        progress.update('process_exit_wait', 'begin')
        # Native builds may expose only standard-library frames, or none.
        frame = SimpleNamespace(f_globals={'__name__': 'threading'},
            f_code=SimpleNamespace(co_name='wait'), f_lineno=331, f_back=None)
        for frames in ({}, {threading.get_ident(): frame}):
            with mock.patch('sys._current_frames', return_value=frames), \
                 self.assertLogs('ovb_rc003', level='WARNING') as captured:
                diagnostic_trace.log_shutdown_threads(logger)
            output = '\n'.join(captured.output)
            self.assertIn('business_locations=unavailable', output)
            self.assertIn('stage=process_exit_wait state=begin', output)
            self.assertIn('last_completed=pipe_close', output)
            self.assertIn('native_stack=unavailable', output)

    def test_progress_duration_and_logging_failure_do_not_break_cleanup(self):
        logger = mock.Mock()
        with mock.patch.object(diagnostic_trace.time, 'monotonic', side_effect=[10, 10, 10.25]):
            progress = diagnostic_trace.CleanupProgress(logger, 'receiver')
            progress.update('pipe_close', 'begin')
            logger.info.side_effect = OSError('PRIVATE')
            progress.update('pipe_close', 'done')
        self.assertEqual(progress.state[4], 250)
        self.assertEqual(progress.state[3], 'pipe_close')

    def test_duration_contract_rejects_unbounded_or_private_fields(self):
        identity = SessionIdentity.create('a' * 64)
        for value in (-1, 0, 125, 2**31 - 1):
            sender, reader = (Channel(identity, commands=False) for _ in range(2))
            row = reader.decode(sender.encode('diagnostic', stage='hid_script_unload', phase='done',
                reason='stopped', elapsed_ms=value, **empty_stop_details()))
            self.assertEqual(row['elapsed_ms'], value)
        for value in (-2, True, 1.5, 'PRIVATE', 2**31):
            with self.assertRaises(ChannelError):
                Channel(identity, commands=False).encode('diagnostic', stage='capture_stop', phase='done',
                    reason='stopped', elapsed_ms=value, **empty_stop_details())

    def test_stop_origin_is_recorded_and_arbitrary_text_is_not(self):
        from ovb_rc003 import bridge_control_windows as control
        with mock.patch.object(bridge_launcher, 'stop_in_process_bridge', return_value=True) as stop, \
             mock.patch.object(control.bridge_runtime_status, 'read_status', return_value=None), \
             self.assertLogs('ovb_rc003', level='INFO') as captured:
            self.assertTrue(control.request_bridge_exit(reason='user_restart').stopped)
        stop.assert_called_once_with(timeout=5.0, reason='user_restart')
        self.assertIn('reason=user_restart', '\n'.join(captured.output))
        self.assertEqual(bridge_launcher.stop_reason('PRIVATE'), 'unspecified')

    def test_crash_projection_and_unavailable_states(self):
        raw = dict(status='matched', events=[dict(module='PRIVATE/path.dll', module_version='1.2.3.4',
            exception_code='c0000005', fault_offset='00001234', correlation='pid_creation_and_time',
            message='PRIVATE token')])
        with mock.patch.object(system_evidence, '_environment', return_value=raw), \
             self.assertLogs('ovb_rc003', level='WARNING') as captured:
            system_evidence._collect_exit('a' * 64, 4, 123, 0xc0000005, 1000)
        output = '\n'.join(captured.output)
        self.assertIn('c0000005', output)
        self.assertIn('pid_creation_and_time', output)
        self.assertNotIn('PRIVATE', output)
        for result in ({'status': 'no_matching_event'}, {'error': 'query_cancelled'}):
            with mock.patch.object(system_evidence, '_environment', return_value=result), \
                 self.assertLogs('ovb_rc003', level='WARNING') as captured:
                system_evidence._collect_exit('a' * 64, 4, 123, 2, 1000)
            self.assertIn(next(iter(result.values())), '\n'.join(captured.output))
        with mock.patch.object(system_evidence, '_environment', side_effect=subprocess.TimeoutExpired('PRIVATE', 8)), \
             self.assertLogs('ovb_rc003', level='WARNING') as captured:
            system_evidence._collect_exit('a' * 64, 4, 123, 2, 1000)
        self.assertIn('query_timeout', '\n'.join(captured.output))
        self.assertNotIn('PRIVATE', '\n'.join(captured.output))

    def test_hid_detach_failure_is_reported_and_other_resource_still_detaches(self):
        from ovb_rc003.chromecast_hid_tap_windows import HidTap
        tap = HidTap('a' * 64)
        tap.script = SimpleNamespace(unload=mock.Mock(side_effect=OSError('PRIVATE')))
        tap.session = SimpleNamespace(detach=mock.Mock())
        phases = []
        self.assertFalse(tap.close(diagnostic=lambda stage, phase: phases.append((stage, phase))))
        self.assertEqual(phases, [('hid_script_unload', 'begin'), ('hid_script_unload', 'failed'),
                                  ('hid_session_detach', 'begin'), ('hid_session_detach', 'done')])
        tap.session = SimpleNamespace(detach=mock.Mock())
        self.assertTrue(tap.close(diagnostic=lambda *_: (_ for _ in ()).throw(OSError('PRIVATE'))))
        self.assertIsNone(tap.session)

    def test_crash_query_uses_same_bounded_queue_and_deduplicates_process(self):
        with mock.patch.object(system_evidence, '_pending', {}), \
             mock.patch.object(system_evidence, '_recent', {}), \
             mock.patch.object(system_evidence, '_running', True), \
             mock.patch.object(system_evidence, '_closing', False), \
             mock.patch.object(system_evidence, '_collect_exit') as collect:
            for _ in range(2):
                system_evidence.request_process_exit('a' * 64, 4, 123, 2)
            self.assertEqual(len(system_evidence._pending), 1)
            system_evidence._drain()
            collect.assert_called_once()
        with mock.patch.object(system_evidence, '_closing', True), \
             self.assertLogs('ovb_rc003', level='WARNING') as captured:
            system_evidence.request_process_exit('a' * 64, 4, 123, 2)
        self.assertIn('application_closing', '\n'.join(captured.output))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell projection')
    def test_real_powershell_query_projects_synthetic_event_and_rejects_other_birth(self):
        prefix = r'''
function Get-WinEvent {
 $xml='<Event><EventData><Data Name="ProcessId">0x4</Data><Data Name="ProcessCreationTime">0x7b</Data><Data Name="ModuleName">C:\PRIVATE\module.dll</Data><Data Name="ModuleVersion">1.2.3.4</Data><Data Name="ExceptionCode">c0000005</Data><Data Name="FaultingOffset">00001234</Data></EventData></Event>'
 $e=[pscustomobject]@{Xml=$xml}
 $e | Add-Member ScriptMethod ToXml { $this.Xml }
 return $e
}
'''
        for birth, expected in ((123, 'matched'), (124, 'no_matching_event')):
            query = system_evidence._EXIT_QUERY.replace('__PID__', '4').replace('__BORN__', str(birth)).replace('__STAMP__', '1000')
            row = system_evidence._environment(prefix + query, timeout=8)
            self.assertEqual(row['status'], expected)
            self.assertNotIn('PRIVATE', json.dumps(row))
            if expected == 'matched':
                self.assertEqual(row['events'][0]['module'], 'module.dll')
