"""Diagnostic contract from real producers to exported ZIP, isolated from devices."""
import asyncio
from contextlib import contextmanager
import ctypes
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from ovb_rc003 import application_update as update
from ovb_rc003 import diagnostic_errors, diagnostic_trace as trace, logging_setup, log_export
from ovb_rc003 import chromecast_attach_diagnostics_windows as attach
from ovb_rc003 import chromecast_voice_receiver as receiver
from ovb_rc003.chromecast_channel import Channel, SessionIdentity
from ovb_rc003.chromecast_observation import Observation, valid_record
from ovb_rc003.chromecast_host_activity import CaptureWatch
from ovb_rc003.voice_audio_session import VoiceAudioSession
from ovb_rc003.voice_playback_session_windows import CaptureSession


@contextmanager
def journal(root):
    handler = logging_setup.EvidenceFileHandler(root / 'logs' / 'app.log', asynchronous=True)
    handler.addFilter(logging_setup.PrivacySafeExceptionFilter())
    handler.setFormatter(logging.Formatter(logging_setup.LOG_FORMAT))
    logger = logging.Logger('ovb_rc003.audit', logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield logger, handler
    finally:
        handler.close()


def exported(root, name='evidence.zip'):
    destination = root / name
    result = log_export.export_logs(destination, root=root)
    if result.outcome != 'exported':
        raise AssertionError(result)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise AssertionError('corrupt ZIP')
        files = {n: archive.read(n).decode('utf-8') for n in archive.namelist()}
    return result, json.loads(files['export-info.json']), files


def diagnostic_rows(text):
    return [json.loads(line.split('Diagnostic: ', 1)[1])
            for line in text.splitlines() if 'Diagnostic: ' in line]


class FailureExportTests(unittest.TestCase):
    def test_large_incident_keeps_safe_native_error_instead_of_duplicate_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = trace._FaultReport(Path(tmp))
            snapshot = diagnostic_errors.exception_snapshot(PermissionError(13, 'private-value'))
            report.accept(dict(event='failed_operation', session_id='a', wall_time=time.time(),
                               failure_key='failed', message='x' * 20000, error_detail=snapshot))
            report.flush(force=True)
            saved = json.loads(report.path.read_bytes())['incidents'][0]
            first = saved['events'][saved['before_count']]
            self.assertEqual(first['error_detail']['errors'][0]['errno'], 13)
            self.assertTrue(first['report_fields_truncated'])
            self.assertNotIn('private-value', report.path.read_text())

    def test_error_types_codes_and_private_cause_chain_survive_export_without_text(self):
        with tempfile.TemporaryDirectory() as tmp, journal(Path(tmp)) as (logger, handler):
            for error in (PermissionError(13, 'private-file-one'),
                          TimeoutError('private-file-two'), RuntimeError('private-file-three')):
                try:
                    raise error
                except Exception:
                    logger.exception('operation failed')
            try:
                try:
                    raise OSError(32, 'private-cause')
                except OSError as error:
                    raise RuntimeError('private-wrapper') from error
            except RuntimeError as error:
                logging_setup.log_diagnostic(logger, 'test_error', error=error, failure=True,
                                             operation_id='error-one')
            _, manifest, files = exported(Path(tmp))
            body = '\n'.join(files.values())
            for private in ('private-file-one', 'private-file-two', 'private-file-three',
                            'private-cause', 'private-wrapper', tmp):
                self.assertNotIn(private, body)
            for kind in ('PermissionError', 'TimeoutError', 'RuntimeError', 'BrokenPipeError'):
                self.assertIn(kind, files['app.log'])
            self.assertIn('"errno":13', files['app.log'])
            self.assertIn('"errno":32', files['app.log'])
            self.assertFalse(manifest['incomplete'])

    def test_known_frida_native_forms_pass_real_worker_channel_and_final_zip(self):
        from ovb_rc003.chromecast_hid_tap_windows import startup_record
        with tempfile.TemporaryDirectory() as tmp, journal(Path(tmp)) as (logger, _):
            tx = Channel(SessionIdentity.create('a' * 64), commands=False)
            tx.encode('ready')
            for text, family, code in (
                ("Unable to spawn helper executable at 'private-helper-path': 0x00000005", 'helper_start', 5),
                ('Unexpected error while attaching to process with pid 555 (OpenProcess returned 0xc0000022)',
                 'access_denied', 0xc0000022)):
                details = attach.exception_details(RuntimeError(text))
                self.assertEqual(details['error_family'], family)
                self.assertEqual(details['reported_code'], code)
                row = startup_record('attach_exception', 'failed', error=RuntimeError(text), details=details)
                self.assertTrue(valid_record(row))
                self.assertLess(len(tx.encode('evidence', record=row)), 2048)
                logger.warning('worker evidence: %s', json.dumps(row), extra={'failure_key': 'attach_failed'})
            _, _, files = exported(Path(tmp))
            self.assertIn('helper_start', files['app.log'])
            self.assertIn(str(0xc0000022), files['app.log'])
            self.assertNotIn('private-helper-path', '\n'.join(files.values()))
            self.assertNotIn('reported_code', diagnostic_errors.error_detail(RuntimeError('process pid 555')))

    def test_blocked_triggers_report_unknown_and_short_audio_do_not(self):
        with tempfile.TemporaryDirectory() as tmp, journal(Path(tmp)) as (logger, handler):
            for state in ('host_unconfirmed', 'audio_empty', 'cancelled'):
                logging_setup.log_diagnostic(logger, 'voice_operation', operation_id='voice-one', state=state)
            self.assertTrue(handler._report_writer.flush())
            self.assertFalse((Path(tmp) / 'logs' / trace.REPORT_FILENAME).exists())
            logging_setup.log_diagnostic(logger, 'voice_operation', operation_id='voice-one',
                                         state='blocked', failure=True, phase='receiver_state')
            logging_setup.log_diagnostic(logger, 'audio_pipeline', operation_id='voice-one',
                                         failure=True, reason='cleanup_failed')
            logging_setup.log_diagnostic(logger, 'voice_operation', operation_id='voice-two',
                                         state='blocked', failure=True, phase='receiver_state')
            _, _, files = exported(Path(tmp))
            incidents = json.loads(files[trace.REPORT_FILENAME])['incidents']
            self.assertEqual(len(incidents), 2)
            self.assertEqual(incidents[0]['events'][incidents[0]['before_count']]['state'], 'blocked')
            self.assertEqual(incidents[0]['failure_count'], 2)
            self.assertEqual(incidents[1]['operation_id'], 'voice-two')

    def test_basic_overflow_stays_incomplete_after_writer_exit(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(logging_setup, 'LOG_QUEUE_SIZE', 2):
            root = Path(tmp)
            with journal(root) as (logger, handler):
                entered, release = threading.Event(), threading.Event()
                original = handler._emit_record
                def slow(record):
                    if record.getMessage() == 'hold writer':
                        entered.set()
                        release.wait(2)
                    original(record)
                handler._emit_record = slow
                logger.info('hold writer')
                self.assertTrue(entered.wait(1))
                try:
                    for i in range(30):
                        logger.info('bounded probe %s', i)
                finally:
                    release.set()
                self.assertFalse(handler.flush_pending())
            result, manifest, files = exported(root)
            self.assertTrue(result.incomplete)
            self.assertTrue(manifest['incomplete'])
            self.assertGreater(manifest['diagnostic_coverage']['retained_report_losses']['basic_dropped'], 0)
            self.assertIn('application_log_loss', files[trace.REPORT_FILENAME])

    def test_capture_replacement_is_observable_without_adoption_or_extra_queries(self):
        a = CaptureSession('private-endpoint', 'private-instance-one', 20, 1)
        b = CaptureSession('private-endpoint', 'private-instance-two', 20, 1)
        reader = mock.Mock(side_effect=[(), (a,), (b,), (b,)])
        watch = CaptureWatch(reader)
        watch.begin()
        self.assertFalse(watch.poll(1))
        self.assertFalse(watch.poll(2))
        self.assertEqual(watch.observation['replacement_same_process'], 1)
        self.assertEqual(watch.observation['replacement_same_endpoint'], 1)
        self.assertFalse(watch.observation['bound_present'])
        self.assertTrue(watch.poll(2.75))
        self.assertEqual(watch.identity, a.identity)
        self.assertEqual(reader.call_count, 4)
        self.assertNotIn('private-', json.dumps(watch.observation))

    def test_written_counts_distinguish_failed_output_from_decoded_statistics(self):
        with tempfile.TemporaryDirectory() as tmp, journal(Path(tmp)) as (logger, _):
            audio = VoiceAudioSession(config=lambda: {}, logger=logger, request_cleanup=lambda: None,
                                      disable_forwarding=lambda: None, on_first_frame=lambda: None)
            audio.sink = SimpleNamespace(write=mock.Mock(side_effect=OSError(32, 'private-output')))
            audio.begin_observation('audio-one')
            self.assertTrue(audio.ensure_writer(audio.sink))
            try:
                self.assertTrue(audio.writer.submit((1, 2, 3, 4)))
                self.assertIsNotNone(audio.flush('test').error)
                self.assertEqual(audio.stats.frames, 1)  # Existing contract is preserved.
                counts = audio.writer.diagnostic_counts()
                self.assertEqual(counts['queued_frames'], 1)
                self.assertEqual(counts['written_frames'], 0)
                self.assertEqual(counts['written_samples'], 0)
                self.assertEqual(counts['failed_frames'], 1)
            finally:
                self.assertTrue(audio.stop_writer())
            _, _, files = exported(Path(tmp))
            self.assertIn('"written_frames":0', files['app.log'])
            self.assertIn('"errno":32', files['app.log'])
            self.assertIn('ovb_rc003.voice_audio_session', files['app.log'])
            self.assertNotIn('private-output', '\n'.join(files.values()))

    def test_download_five_attempts_and_cancel_have_distinct_exported_results(self):
        with tempfile.TemporaryDirectory() as tmp, journal(Path(tmp)) as (logger, _), \
                mock.patch.object(update, '_LOGGER', logger), mock.patch.object(update, '_wait_download_retry'):
            failed = mock.Mock(side_effect=update.ApplicationUpdateError('timeout', 'private-network-text'))
            with self.assertRaises(update.ApplicationUpdateError):
                update._retry_download(failed, None, operation_id='download-one')
            self.assertEqual(failed.call_count, 5)
            cancelled = threading.Event()
            cancelled.set()
            unused = mock.Mock()
            with self.assertRaises(update.ApplicationUpdateCancelled):
                update._retry_download(unused, cancelled, operation_id='download-two')
            unused.assert_not_called()
            _, _, files = exported(Path(tmp))
            rows = diagnostic_rows(files['app.log'])
            self.assertEqual([r['attempt'] for r in rows if r.get('phase') == 'attempt_begin'], [1, 2, 3, 4, 5])
            self.assertEqual(sum(r.get('phase') == 'retry_wait' for r in rows), 4)
            self.assertEqual(sum(r.get('phase') == 'attempt_cancelled' for r in rows), 1)
            self.assertNotIn('private-network-text', '\n'.join(files.values()))

    @unittest.skipUnless(sys.platform == 'win32', 'isolated native Windows failures')
    def test_native_permission_and_missing_component_reach_final_zip(self):
        with tempfile.TemporaryDirectory() as tmp, journal(Path(tmp)) as (logger, _):
            root = Path(tmp)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                               ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
            create.restype = ctypes.c_void_p
            handle = create(str(root), 0x40000000, 7, None, 3, 0, None)
            self.assertEqual(handle, ctypes.c_void_p(-1).value)
            code = ctypes.get_last_error()
            self.assertEqual(code, 5)
            permission = ctypes.WinError(code)
            logging_setup.log_diagnostic(logger, 'native_probe', operation_id='native-one',
                                         failure=True, phase='open_own_directory', error=permission)
            load = kernel.LoadLibraryExW
            load.argtypes = (ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32)
            load.restype = ctypes.c_void_p
            self.assertIsNone(load(str(root / 'missing-private-component.dll'), None, 0))
            code = ctypes.get_last_error()
            self.assertEqual(code, 126)
            logging_setup.log_diagnostic(logger, 'native_probe', operation_id='native-two',
                                         failure=True, phase='load_missing_component', error=ctypes.WinError(code))
            _, manifest, files = exported(root)
            body = '\n'.join(files.values())
            self.assertIn('"winerror":5', files['app.log'])
            self.assertIn('"winerror":126', files['app.log'])
            self.assertNotIn(tmp, body)
            self.assertNotIn('missing-private-component', body)
            self.assertFalse(manifest['incomplete'])


class VoiceCommandExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_failed_and_cancelled_commands_keep_existing_gate_results(self):
        rows = []
        observation = Observation(rows.append)
        device = SimpleNamespace(write_voice=mock.AsyncMock())
        with mock.patch.object(receiver.config, 'load_config', return_value={}):
            target = receiver.VoiceReceiver(device, lambda *_: None, observation=observation)
        target.available = True
        target.gate = mock.Mock()
        target.gate.tick.return_value = []
        target.gate.command_result.return_value = []
        effect = SimpleNamespace(value=b'\x0d\x01', attempt=1)
        error = OSError(5, 'private-command')
        error.gatt_status, error.protocol_error = 1, 8
        async def fail():
            raise error
        task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        target.pending = effect, task
        target._pending_started = time.monotonic()
        await target.tick()
        self.assertIs(target.gate.command_result.call_args.args[1], False)
        gate_event = asyncio.Event()
        task = asyncio.create_task(gate_event.wait())
        target.pending = effect, task
        target._pending_started = time.monotonic() - 4
        await target.tick()
        self.assertFalse(task.cancelled())
        await target.close()
        self.assertTrue(task.cancelled())
        commands = [r for r in rows if r['kind'] == 'voice_command']
        self.assertEqual([r['state'] for r in commands],
                         ['failed', 'native_pending', 'cancel_requested', 'cancel_settled'])
        self.assertEqual(commands[0]['status'], 1)
        self.assertEqual(commands[0]['protocol_error'], 8)
        for field in ('state', 'error_type', 'error_family'):
            self.assertFalse(valid_record(dict(commands[0], **{field: []})))
        tx = Channel(SessionIdentity.create('b' * 64), commands=False)
        rx = Channel(tx.identity, commands=False)
        rx.decode(tx.encode('ready'))
        with tempfile.TemporaryDirectory() as tmp, journal(Path(tmp)) as (logger, _):
            from ovb_rc003.chromecast_client import Client
            client = Client.__new__(Client)
            client.identity = tx.identity
            for row in commands:
                self.assertTrue(valid_record(row))
                wire = tx.encode('evidence', record=row)
                self.assertLess(len(wire), 2048)
                with mock.patch('ovb_rc003.chromecast_client.logging.getLogger', return_value=logger):
                    client._observe_evidence(rx.decode(wire))
            _, _, files = exported(Path(tmp))
            self.assertIn('native_pending', files['app.log'])
            self.assertIn('cancel_settled', files['app.log'])
            self.assertNotIn('private-command', '\n'.join(files.values()))
