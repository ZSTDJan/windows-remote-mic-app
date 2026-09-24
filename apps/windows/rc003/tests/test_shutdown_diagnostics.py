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

from ovb_rc003 import bridge_launcher, chromecast_client, diagnostic_trace, log_export
from ovb_rc003.chromecast_voice_host import VoiceHost


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
