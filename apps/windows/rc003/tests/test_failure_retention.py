"""Failure evidence retention, ordinary-log integration and bounded storage."""
import json
import logging
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from ovb_rc003 import diagnostic_trace as trace, log_export, logging_setup


class RetentionTests(unittest.TestCase):
    def test_flood_cannot_displace_first_failure_or_its_prelude(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = trace._FaultReport(Path(tmp))
            with mock.patch.object(report, 'flush'):
                report.accept(dict(event='before', session_id='a', wall_time=99))
                report.accept(dict(event='failure', session_id='a', wall_time=100, success=False))
                for n in range(1000):
                    report.accept(dict(event='cleanup', session_id='a', wall_time=101 + n / 100))
                report.accept(dict(event='after_window', session_id='a', wall_time=116))
            report.flush(force=True)
            saved = json.loads(report.path.read_bytes())['incidents'][0]
            self.assertEqual([r['event'] for r in saved['events'][:2]], ['before', 'failure'])
            self.assertEqual(len(saved['events']), trace.REPORT_EVENT_COUNT)
            self.assertGreater(saved['dropped_after'], 0)
            self.assertNotIn('after_window', [r['event'] for r in saved['events']])

    def test_same_failure_merges_without_extending_post_capture_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = trace._FaultReport(Path(tmp))
            with mock.patch.object(report, 'flush'):
                for stamp in range(100, 500, 20):
                    report.accept(dict(event='failed', session_id='a', wall_time=stamp,
                                       failure_key='host_start'))
            saved = report.incidents[0]
            self.assertEqual(len(report.incidents), 1)
            self.assertEqual(saved['failure_count'], 20)
            self.assertEqual(saved['capture_until'], 115)
            self.assertEqual(len(saved['events']), 1)

    def test_report_retains_at_most_three_groups_and_respects_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = trace._FaultReport(Path(tmp))
            with mock.patch.object(report, 'flush'):
                for n in range(8):
                    report.accept(dict(event='failure', session_id='a', wall_time=100+n*100,
                                       failure_key=str(n)))
                    for j in range(190):
                        report.accept(dict(event='details', session_id='a', wall_time=101+n*100,
                                           details='x'*3000))
            report.flush(force=True)
            self.assertLessEqual(report.path.stat().st_size, trace.REPORT_MAX_BYTES)
            self.assertLessEqual(len(report.incidents), 3)
            self.assertEqual(report.incidents[-1]['failure_key'], '7')

    def test_full_queue_preserves_failure_and_accounts_for_loss(self):
        writer = trace._ReportWriter.__new__(trace._ReportWriter)
        writer.lock, writer.stop = threading.Lock(), threading.Event()
        writer.thread = mock.Mock()
        writer.thread.is_alive.return_value = True
        writer.queue, writer.dropped, writer.session_id = queue.Queue(maxsize=1), 0, 'a'
        writer.submit(dict(event='old'))
        writer.submit(dict(event='failed', success=False))
        item = writer.queue.get_nowait()
        self.assertEqual(item['event'], 'failed')
        self.assertEqual(item['queue_dropped_before'], 1)

    def test_existing_schema_one_report_survives_new_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, trace.REPORT_FILENAME)
            path.write_text(json.dumps(dict(schema=1, incidents=[dict(session_id='old',
                started_at=1, last_failure_at=1, failure_count=1, events=[dict(event='old_failure')])])), encoding='utf-8')
            report = trace._FaultReport(Path(tmp))
            report.accept(dict(event='failed', session_id='new', wall_time=100, success=False))
            report.flush(force=True)
            self.assertEqual([i['session_id'] for i in json.loads(path.read_bytes())['incidents']], ['old', 'new'])


class IntegrationTests(unittest.TestCase):
    def test_voice_and_google_explicit_failure_markers_work_with_trace_disabled(self):
        from ovb_rc003 import app, chromecast_client
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / 'logs'
            logs.mkdir()
            handler = logging_setup.EvidenceFileHandler(logs/'app.log', encoding='utf-8')
            logger = logging.Logger('test-failure-entry')
            logger.addHandler(handler)
            owner = SimpleNamespace(_runtime_status_lock=threading.Lock(), _logger=logger,
                                    _publish_runtime_status=mock.Mock(),
                                    _diagnostic_trace=trace.DiagnosticTrace(root, enabled=False))
            try:
                app.RC003App._set_runtime_voice_result(owner, 'host_start_failed', provider='wetype')
                client = chromecast_client.Client('a'*64, mode='run', on_edge=lambda _: None)
                with mock.patch.object(chromecast_client.logging, 'getLogger', return_value=logger), \
                        mock.patch.object(chromecast_client.diagnostics, 'request'):
                    client._observe_diagnostic(dict(stage='capture', phase='failed', reason='capture_failed'))
                self.assertTrue(handler._report_writer.flush())
                report = json.loads((logs/trace.REPORT_FILENAME).read_bytes())
                self.assertEqual({i['failure_key'] for i in report['incidents']},
                                 {'voice:wetype:host_start_failed', 'chromecast:capture:capture_failed'})
                self.assertFalse((logs/trace.TRACE_FILENAME).exists())
            finally:
                owner._diagnostic_trace.close()
                handler.close()

    def test_failed_archive_flush_is_visible_in_export_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root/'logs'
            logs.mkdir()
            handler = logging_setup.EvidenceFileHandler(logs/'app.log', encoding='utf-8')
            try:
                with mock.patch.object(Path, 'write_bytes', side_effect=PermissionError('disk')):
                    handler.handle(logging.LogRecord('test', logging.ERROR, __file__, 0, 'failure', (), None))
                    result = log_export.export_logs(root/'logs.zip', root=root)
                self.assertEqual(result.outcome, 'exported')
                self.assertTrue(result.incomplete)
                with zipfile.ZipFile(result.path) as archive:
                    self.assertFalse(json.loads(archive.read('export-info.json'))['fault_report_flushed'])
            finally:
                handler.close()

    def test_ordinary_failure_and_trace_share_archive_and_export_after_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / 'logs'
            logs.mkdir()
            handler = logging_setup.EvidenceFileHandler(logs / 'app.log', maxBytes=256, backupCount=1, encoding='utf-8')
            handler.addFilter(logging_setup.PrivacySafeExceptionFilter())
            logger = logging.Logger('ovb_rc003.retention-test')
            logger.addHandler(handler)
            detailed = trace.DiagnosticTrace(root, enabled=True)
            try:
                self.assertIs(handler._report_writer, detailed._report_writer)
                detailed.emit('before_voice', attempt_id='test-attempt')
                detailed.close()
                self.assertFalse(detailed.emit('private_off_event'))
                logger.error('host failed: %s', OSError('private exception'))
                for n in range(220):
                    logger.info('cleanup %s', n)
                result = log_export.export_logs(root / 'logs.zip', root=root)
                self.assertEqual(result.outcome, 'exported')
                with zipfile.ZipFile(result.path) as archive:
                    report = json.loads(archive.read(trace.REPORT_FILENAME))
                    manifest = json.loads(archive.read('export-info.json'))
                serialized = json.dumps(report)
                self.assertTrue(manifest['fault_report_flushed'])
                self.assertIn('before_voice', serialized)
                self.assertIn('host failed: OSError', serialized)
                self.assertNotIn('private exception', serialized)
                self.assertNotIn('private_off_event', serialized)
                self.assertNotIn('host failed', (logs / 'app.log').read_text())
                self.assertNotIn('host failed', (logs / 'app.log.1').read_text())
            finally:
                detailed.close()
                logger.removeHandler(handler)
                handler.close()
            self.assertNotIn(logs.resolve(), trace._report_writers)

    def test_warning_is_not_failure_without_explicit_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp, 'logs')
            logs.mkdir()
            handler = logging_setup.EvidenceFileHandler(logs / 'app.log', maxBytes=256, backupCount=1, encoding='utf-8')
            try:
                record = logging.LogRecord('test', logging.WARNING, __file__, 0, 'waiting for device', (), None)
                handler.handle(record)
                handler._report_writer.flush()
                self.assertFalse((logs / trace.REPORT_FILENAME).exists())
                record.failure_key = 'confirmed_start_failure'
                handler.handle(record)
                handler._report_writer.flush()
                self.assertTrue((logs / trace.REPORT_FILENAME).exists())
            finally:
                handler.close()

    def test_helper_log_rotates_once_closes_and_exports_backup(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(logging_setup, 'HID_HELPER_LOG_MAX_BYTES', 256):
            root = Path(tmp)
            for n in range(25):
                self.assertTrue(logging_setup.write_parent_hid_helper_event('result', detail=str(n), root=root))
            self.assertEqual({p.name for p in (root/'logs').iterdir()}, {'hid-helper.log', 'hid-helper.log.1'})
            result = log_export.export_logs(root/'test.zip', root=root)
            with zipfile.ZipFile(result.path) as archive:
                self.assertIn('hid-helper.log.1', archive.namelist())
            for path in (root/'logs').iterdir():
                self.assertLessEqual(path.stat().st_size, 256)
                path.unlink()  # All handles are released after each one-shot write.
