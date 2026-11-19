import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from ovb_rc003 import receiver_startup_errors as errors
from ovb_rc003 import receiver_startup_report as report
from ovb_rc003.chromecast_channel import Channel, SessionIdentity
from ovb_rc003.chromecast_hid_tap_windows import startup_record
from ovb_rc003.chromecast_observation import valid_record


class ReceiverStartupReportTests(unittest.TestCase):
    def test_actual_failure_root_can_be_compared_without_exporting_path(self):
        with patch.dict(os.environ, TEMP='C:\\Private Folder\\Temp\\', TMP=r'C:\Other Account\Temp', TMPDIR=''):
            data = errors.failure_environment()
            self.assertEqual(data['configured_mask'], 3)
            self.assertNotEqual(data['temp_ref'], data['tmp_ref'])
            self.assertEqual(data['temp_ref'], errors.root_reference(r'c:\private folder\temp'))
            self.assertNotIn('Private', json.dumps(data))
            row = startup_record('attach_failure_environment', 'failed', details=data)
            self.assertTrue(valid_record(row))

    def test_paths_removed_native_reason_retained(self):
        for text in (rf'error creating directory {PureWindowsPath("C:/Users")}\Private Name\frida-abc: No such file or directory',
                     "Unable to spawn helper executable at 'C:\\Users\\Private Name\\frida.exe': 0x00000005",
                     "error creating file '\\\\server\\Private Name\\agent.dll': Permission denied",
                     "error creating directory 'C:\\Users\\Private's folder\\frida': No such file or directory"):
            with self.subTest(text=text):
                result = errors.safe_message(text)
                self.assertNotIn('Private', result)
                self.assertIn(text.rsplit(': ', 1)[1], result)
                self.assertEqual(errors.safe_message(result), result)

    def test_unicode_failure_survives_real_pipe_contract(self):
        identity = SessionIdentity.create('a' * 64)
        from frida import PermissionDeniedError
        for text in ('错误' * 500, 'unknown native failure', ''):
            row = startup_record('attach_failure_text', 'failed', error=PermissionDeniedError(text),
                                 details=errors.failure_details(PermissionDeniedError(text)))
            self.assertTrue(valid_record(row))
            outgoing = Channel(identity, commands=False)
            incoming = Channel(identity, commands=False)
            raw = outgoing.encode('evidence', record=row)
            self.assertLessEqual(len(raw), 2048)
            self.assertEqual(incoming.decode(raw)['record'], row)
        self.assertNotIn('PRIVATE', json.dumps(errors.failure_details(RuntimeError('PRIVATE user data'))))

    def test_rejects_raw_path_and_control_characters(self):
        for text in (rf'{PureWindowsPath("C:/Users")}\Private\secret', 'line\nline'):
            row = startup_record('attach_failure_text', 'failed', details={'error_message': text})
            self.assertIsNone(row)

    def test_native_temp_health_normal_missing_file_and_reparse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(report.probe_root('TEMP', str(root))['outcome'], 'passed')
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(report.probe_root('TMP', str(root / 'missing'))['outcome'], 'failed')
            item = root / 'not-directory'
            item.write_text('owned test file')
            self.assertEqual(report.probe_root('TMP', str(item))['outcome'], 'not_plain_directory')
            self.assertEqual(item.read_text(), 'owned test file')

    def test_events_keep_only_supported_metadata_and_hide_command_output(self):
        xml = r'''<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
          <System><Provider Name="private@example.invalid"/><EventID>3077</EventID>
          <TimeCreated SystemTime="2026-11-18T08:00:00.0123456Z"/></System>
          <EventData><Data Name="FileName">C:\Person\frida.dll</Data>
          <Data Name="Path">C:\Person\unrelated.txt</Data>
          <Data Name="Status">0xC0000022</Data>
          <Data Name="ErrorCode">private@example.invalid</Data>
          <Data Name="CommandLine">personal material</Data></EventData>
        </Event>'''
        with patch.object(report.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, stdout=xml.encode('utf-16-le'))) as run:
            row = report.event_query('Application')
            self.assertIs(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
        event, = row['events']
        self.assertEqual((event['event_id'], event['time'], event['provider']),
                         ('3077', '2026-11-18T08:00:00.0123456Z', 'other'))
        self.assertEqual(event['data'], [
            dict(name='FileName', component='frida', path_ref=errors.root_reference(r'C:\Person\frida.dll')),
            dict(name='Status', value='0xC0000022'),
        ])
        with patch.object(report.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 5, stderr=b'private diagnostic content')):
            self.assertEqual(report.event_query('Application'),
                             dict(channel='Application', return_code=5, outcome='unavailable'))

    def test_native_report_timeout_cancel_and_exit_export_partial(self):
        # Real Windows children and Job lifetime; no Frida injection or device.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, command, cancellation, expected in (
                ('timeout', [sys.executable, '-c', 'import time;time.sleep(30)'], None, 'timeout'),
                ('cancel', [sys.executable, '-c', 'import time;time.sleep(30)'], threading.Event(), 'cancelled'),
                ('exit', [sys.executable, '-c', 'raise SystemExit(17)'], None, 'child_failed'),
            ):
                if cancellation:
                    cancellation.set()
                with self.subTest(name=name), patch.object(report, '_child_command', return_value=command):
                    destination = root / (name + '.zip')
                    result = report.collect(destination, root=root, timeout=1, cancel=cancellation)
                    self.assertEqual(result.outcome, 'exported')
                    self.assertTrue(result.incomplete)
                    with zipfile.ZipFile(destination) as archive:
                        captured = json.loads(archive.read('receiver-startup-report.json'))
                        self.assertEqual(captured['outcome'], expected)
                    self.assertEqual(list(root.glob('.remote-mic-report-*')), [])

    def test_collection_failure_retained_and_existing_zip_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'capture.zip'
            with patch.object(report, '_Job', side_effect=PermissionError(5, 'denied')):
                self.assertEqual(report.collect(destination, root=destination.parent).outcome, 'exported')
            with zipfile.ZipFile(destination) as archive:
                captured = json.loads(archive.read('receiver-startup-report.json'))
                self.assertEqual(captured['outcome'], 'collection_failed')
                self.assertIn('collection_error', captured)
            before = destination.read_bytes()
            self.assertEqual(report.collect(destination).outcome, 'invalid_destination')
            self.assertEqual(before, destination.read_bytes())

    def test_window_cancel_preserves_previous_file_and_reaps_started_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / 'capture.zip'
            destination.write_bytes(b'previous archive')
            cancel = threading.Event()
            command = [sys.executable, '-c', 'import time; time.sleep(30)']
            launch = subprocess.Popen
            children = []

            def start(*args, **kwargs):
                child = launch(*args, **kwargs)
                children.append(child)
                cancel.set()
                return child

            with (patch.object(report, '_child_command', return_value=command),
                  patch.object(report.subprocess, 'Popen', side_effect=start)):
                result = report.collect(destination, root=root, cancel=cancel, overwrite=True,
                                        discard_on_cancel=True)
            self.assertEqual(result.outcome, 'cancelled')
            self.assertEqual(destination.read_bytes(), b'previous archive')
            self.assertIsNotNone(children[0].poll())
            self.assertEqual(list(root.iterdir()), [destination])

    def test_no_normal_startup_or_receiver_actions_in_report(self):
        # CLI dispatch exits before the application's normal start/rebind path.
        from ovb_rc003 import __main__ as entry
        with patch.object(sys, 'argv', ['tool', report.FLAG, 'relative.zip']), patch.object(entry, '_run_settings') as settings:
            with self.assertRaises(SystemExit) as raised:
                entry.main()
            self.assertEqual(raised.exception.code, 2)
            settings.assert_not_called()

    def test_worker_failure_saves_actual_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'request.json').write_text(json.dumps({'kind': 'receiver_startup_report', 'schema': 1}))
            with (patch.object(report, 'temporary_roots', side_effect=OSError(5, 'native temporary query failed')),
                  patch.object(report, 'event_query', return_value={'outcome': 'captured'}) as query):
                self.assertEqual(report.worker(root), 0)
                self.assertEqual(query.call_count, len(report.CHANNELS))
            rows = [json.loads(line) for line in (root / 'evidence.jsonl').read_text().splitlines()]
            error, = [row['data'] for row in rows if row['kind'] == 'collection_error']
            self.assertEqual(error['section'], 'temporary_roots')
            self.assertEqual(error['native_code'], 5)
            self.assertNotIn('native temporary query failed', error['error_message'])

    def test_job_close_reaps_owned_descendant(self):
        import ctypes as C
        from ctypes import wintypes as W
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / 'descendant.pid'
            code = ('import pathlib,subprocess,sys,time;'
                    'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
                    f'pathlib.Path({str(marker)!r}).write_text(str(p.pid));time.sleep(60)')
            with patch.object(report, '_child_command', return_value=[sys.executable, '-c', code]):
                self.assertEqual(report.collect(root / 'report.zip', root=root, timeout=2).outcome, 'exported')
            pid = int(marker.read_text())
            kernel = C.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (W.DWORD, W.BOOL, W.DWORD)
            kernel.OpenProcess.restype = W.HANDLE
            kernel.WaitForSingleObject.argtypes = (W.HANDLE, W.DWORD)
            kernel.CloseHandle.argtypes = (W.HANDLE,)
            handle = kernel.OpenProcess(0x100000, False, pid)
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
                finally:
                    kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
