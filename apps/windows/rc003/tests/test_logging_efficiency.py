"""Logging latency and evidence guarantees, using only temporary/simulated inputs."""
import json
import logging
from pathlib import Path
import queue
import tempfile
import threading
import types
import unittest
from unittest import mock
import zipfile

from ovb_rc003 import diagnostic_trace as trace, logging_setup, log_export
from ovb_rc003 import voice_interaction_diagnostics_windows as focus
from ovb_rc003.input_environment_diagnostics_windows import EnvironmentSampler
from ovb_rc003.voice_response_diagnostics_windows import ResponseSampler


def record(message, level=logging.INFO):
    return logging.LogRecord('test', level, __file__, 0, message, (), None)


class LoggingEfficiencyTests(unittest.TestCase):
    def test_failed_export_flush_does_not_stop_later_logging(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'app.log')
            handler = logging_setup.EvidenceFileHandler(path, asynchronous=True)
            try:
                handler.handle(record('before failure'))
                self.assertTrue(handler.flush_pending())
                with mock.patch.object(handler, 'flush', side_effect=OSError('temporary disk failure')):
                    self.assertFalse(handler.flush_pending(timeout=.2))
                self.assertTrue(handler._worker.is_alive())
                handler.handle(record('after recovery'))
                # A past write failure remains visible even after writing resumes.
                self.assertFalse(handler.flush_pending())
                self.assertIn('after recovery', path.read_text())
            finally:
                handler.close()

    def test_slow_basic_file_cannot_delay_fault_prelude_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'logs').mkdir()
            handler = logging_setup.EvidenceFileHandler(root / 'logs' / 'app.log', asynchronous=True)
            detailed = trace.DiagnosticTrace(root, enabled=True)
            entered, release = threading.Event(), threading.Event()
            original = handler._emit_record

            def slow_write(item):
                if item.msg == 'before fault marker':
                    entered.set()
                    release.wait(3)
                original(item)

            handler._emit_record = slow_write
            try:
                before = record('before fault marker')
                before.created -= .1  # Simulate a prelude 100 ms before the failure.
                handler.handle(before)
                self.assertTrue(entered.wait(1))
                detailed.emit('fault_marker', success=False)
                self.assertTrue(detailed._report_writer.flush())
                saved = json.loads((root / 'logs' / trace.REPORT_FILENAME).read_text())
                incident = saved['incidents'][0]
                prelude = incident['events'][:incident['before_count']]
                self.assertEqual(sum(e.get('message') == 'before fault marker' for e in prelude), 1)
                release.set()
                self.assertTrue(handler.flush_pending())
                self.assertTrue(detailed._report_writer.flush())
                saved = json.loads((root / 'logs' / trace.REPORT_FILENAME).read_text())
                self.assertEqual(sum(e.get('message') == 'before fault marker'
                                     for e in saved['incidents'][0]['events']), 1)
            finally:
                release.set()
                detailed.close()
                handler.close()

    def test_disabled_voice_diagnostics_skip_capture_and_preserve_attempt_state(self):
        from ovb_rc003.app import RC003App
        from ovb_rc003 import wetype_control_windows as hotkey
        with tempfile.TemporaryDirectory() as tmp:
            owner = RC003App.__new__(RC003App)
            owner._voice_attempt_id = None
            owner._voice_shortcut = types.SimpleNamespace(ui_confirmation='confirmed')
            owner._voice_text_observation = 'grew'
            owner._diagnostic_trace = trace.DiagnosticTrace(Path(tmp), enabled=False)
            with mock.patch.object(trace, 'foreground_context') as capture, \
                    mock.patch.object(hotkey, '_diagnostic_trace', owner._diagnostic_trace):
                owner._ensure_voice_diagnostic_attempt()
                hotkey._trace('failure', capture_foreground=True)
                capture.assert_not_called()
            self.assertEqual(owner._voice_attempt_id, '')
            self.assertEqual(owner._voice_shortcut.ui_confirmation, 'unknown')
            owner._finish_voice_diagnostic_attempt('done')
            self.assertIsNone(owner._voice_attempt_id)
            self.assertFalse((Path(tmp) / 'logs').exists())

    def test_slow_file_never_blocks_producer_and_overflow_keeps_first_failure(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(logging_setup, 'LOG_QUEUE_SIZE', 3):
            handler = logging_setup.EvidenceFileHandler(Path(tmp) / 'app.log', asynchronous=True)
            entered, release, producer_done = threading.Event(), threading.Event(), threading.Event()
            real_emit = handler._emit_record

            def slow_emit(item):
                if item.msg == 'slow':
                    entered.set()
                    release.wait(3)
                real_emit(item)

            handler._emit_record = slow_emit
            try:
                def produce():
                    handler.handle(record('slow'))
                    producer_done.set()
                producer = threading.Thread(target=produce)
                producer.start()
                self.assertTrue(entered.wait(1))
                self.assertTrue(producer_done.wait(.5))
                handler.handle(record('first failure', logging.ERROR))
                for index in range(15):
                    handler.handle(record(f'ordinary {index}'))
                handler.handle(record('second failure', logging.ERROR))
                self.assertLessEqual(len(handler._pending), 3)
                self.assertFalse(handler.flush_pending(timeout=0))
                release.set()
                handler.close()
                self.assertFalse(handler._worker.is_alive())
                content = Path(tmp, 'app.log').read_text()
                self.assertIn('first failure', content)
                self.assertIn('second failure', content)
                self.assertIn('queue overflow', content)
                producer.join(1)
            finally:
                release.set()
                handler.close()

    def test_export_flushes_live_basic_trace_and_fault_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'logs').mkdir()
            handler = logging_setup.EvidenceFileHandler(root / 'logs' / 'app.log', asynchronous=True)
            handler.addFilter(logging_setup.PrivacySafeExceptionFilter())
            detailed = trace.DiagnosticTrace(root, enabled=True)
            try:
                handler.handle(logging.LogRecord('test', logging.ERROR, '', 0,
                    'failed %s', (OSError('private-path'),), None))
                detailed.emit('latest_trace_marker')
                exported = log_export.export_logs(root / 'export.zip', root=root)
                self.assertFalse(exported.incomplete)
                with zipfile.ZipFile(exported.path) as archive:
                    info = json.loads(archive.read('export-info.json'))
                    self.assertTrue(info['application_log_flushed'])
                    self.assertTrue(info['diagnostic_trace_flushed'])
                    self.assertTrue(info['fault_report_flushed'])
                    self.assertIn(b'failed OSError', archive.read('app.log'))
                    self.assertNotIn(b'private-path', archive.read('app.log'))
                    self.assertIn(b'latest_trace_marker', archive.read(trace.TRACE_FILENAME))
                    self.assertIn(b'failed OSError', archive.read(trace.REPORT_FILENAME))
            finally:
                detailed.close()
                handler.close()

    def test_normal_shutdown_with_handler_lock_drains_and_releases_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'app.log')
            handler = logging_setup.EvidenceFileHandler(path, asynchronous=True, maxBytes=500, backupCount=1)
            for index in range(100):
                handler.handle(record(f'message {index}'))
            handler.acquire()  # logging.shutdown uses exactly this lock order.
            try:
                handler.flush()
                handler.close()
            finally:
                handler.release()
            self.assertFalse(handler._worker.is_alive())
            self.assertIn('message 99', path.read_text())
            path.unlink()

    def test_application_write_failure_is_visible_to_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'logs').mkdir()
            handler = logging_setup.EvidenceFileHandler(root / 'logs' / 'app.log', asynchronous=True)
            try:
                with mock.patch.object(handler, '_emit_record', side_effect=OSError('disk unavailable')):
                    handler.handle(record('cannot write'))
                    self.assertFalse(handler.flush_pending())
                self.assertTrue(handler._worker.is_alive())
                handler._retry_after = 0  # Advance past the disk-error retry backoff.
                handler.handle(record('writing resumed'))
                self.assertFalse(handler.flush_pending())
                self.assertIn('writing resumed', (root / 'logs' / 'app.log').read_text())
                result = log_export.export_logs(root / 'export.zip', root=root)
                self.assertTrue(result.incomplete)
                with zipfile.ZipFile(result.path) as archive:
                    self.assertFalse(json.loads(archive.read('export-info.json'))['application_log_flushed'])
            finally:
                handler.close()

    def test_async_errors_keep_template_grouping_and_frozen_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'app.log')
            handler = logging_setup.EvidenceFileHandler(path, asynchronous=True)
            try:
                values = ['original']
                first = logging.LogRecord('test', logging.ERROR, '', 0, 'failure %s', (values,), None)
                handler.handle(first)
                values.append('later')
                handler.handle(logging.LogRecord('test', logging.ERROR, '', 0, 'failure %s', ('second',), None))
                self.assertTrue(handler.flush_pending())
                self.assertTrue(handler._report_writer.flush())
                report = json.loads(Path(tmp, trace.REPORT_FILENAME).read_text())
                self.assertEqual(len(report['incidents']), 1)
                self.assertEqual(report['incidents'][0]['failure_count'], 2)
                self.assertNotIn('later', path.read_text())
            finally:
                handler.close()

    def test_idle_fault_writer_blocks_until_work_or_close(self):
        entered = threading.Event()
        timeouts = []
        original_queue = queue.Queue

        class ObservedQueue(original_queue):
            def get(self, block=True, timeout=None):
                timeouts.append(timeout)
                entered.set()
                return super().get(block, timeout)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(trace.queue, 'Queue', ObservedQueue):
            writer = trace.acquire_fault_report(Path(tmp))
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(timeouts, [None])
            finally:
                trace.release_fault_report(writer)
            self.assertFalse(writer.thread.is_alive())

    def test_stable_environment_uses_heartbeat_but_changes_are_immediate(self):
        now = [10.0]
        clock = lambda: now[0]
        snapshot = [focus.FocusSnapshot(True, foreground_pid=42)]
        response = ResponseSampler(clock=clock, windows=lambda _: {}, microphone=lambda _: {})
        environment = EnvironmentSampler(clock=clock, processes=lambda: [],
            security=lambda pid: {'pid': pid}, clipboard=lambda: {'sequence': 1}, response=response)
        timeline = focus.ContextTimeline(capture=lambda: snapshot[0], clock=clock, environment=environment)
        timeline.accept({'event': 'attempt_started', 'attempt_id': 'test'})
        observations = []
        for index in range(121):
            now[0] = 10 + index * .25
            item = timeline.poll()
            if item is not None:
                observations.append(item)
        self.assertLessEqual(len(observations), 8)
        self.assertIn('heartbeat', [item['phase'] for item in observations])
        now[0] += .25
        timeline.accept({'event': 'attempt_finished', 'attempt_id': 'test'})
        snapshot[0] = focus.FocusSnapshot(True, foreground_pid=43)
        self.assertEqual(timeline.poll()['foreground_pid'], 43)

    def test_detailed_records_batch_flush_and_error_flushes_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            counts = {'flush': 0}
            opened = Path.open
            error_flushed = threading.Event()

            class Stream:
                def __init__(self, inner):
                    self.inner = inner
                    self.error_written = False
                def write(self, value):
                    self.error_written |= 'test_failure' in value
                    return self.inner.write(value)
                def flush(self):
                    counts['flush'] += 1
                    self.inner.flush()
                    if self.error_written:
                        error_flushed.set()
                def close(self):
                    self.inner.close()

            def open_stream(path, *args, **kwargs):
                inner = opened(path, *args, **kwargs)
                return Stream(inner) if path.name == trace.TRACE_FILENAME and args and args[0] == 'a' else inner

            with mock.patch.object(Path, 'open', open_stream), mock.patch.object(trace, 'TRACE_FLUSH_SECONDS', 60):
                detailed = trace.DiagnosticTrace(root, enabled=True)
                try:
                    for index in range(200):
                        detailed.emit('ordinary', index=index)
                    self.assertTrue(detailed.flush())
                    self.assertLess(counts['flush'], 10)
                    detailed.emit('test_failure', success=False)
                    self.assertTrue(error_flushed.wait(1))
                finally:
                    detailed.close()
            rows = [json.loads(line) for line in (root / 'logs' / trace.TRACE_FILENAME).read_text().splitlines()]
            self.assertEqual(sum(item['event'] == 'ordinary' for item in rows), 200)
            self.assertEqual(rows[-1]['event'], 'session_finished')

    def test_buffered_trace_rotation_and_close_keep_complete_records(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(trace, 'TRACE_MAX_BYTES', 1024):
            root = Path(tmp)
            detailed = trace.DiagnosticTrace(root, enabled=True)
            for index in range(6):
                detailed.emit('rotation_marker', index=index)
            detailed.close()
            rows = []
            for path in (root / 'logs').glob('diagnostic-trace.jsonl*'):
                self.assertLessEqual(path.stat().st_size, 1024)
                rows.extend(json.loads(line) for line in path.read_text().splitlines())
            self.assertEqual(sorted(row['index'] for row in rows if row['event'] == 'rotation_marker'), list(range(6)))
            self.assertIn('session_finished', [row['event'] for row in rows])


if __name__ == '__main__':
    unittest.main()
