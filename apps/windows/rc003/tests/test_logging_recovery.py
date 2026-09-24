"""Disk failure and writer ownership regressions; no user files or devices."""
import json
import logging
from pathlib import Path
import queue
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

from ovb_rc003 import diagnostic_trace as trace, logging_setup, log_export


class LoggingRecoveryTests(unittest.TestCase):
    def test_trace_queue_counts_all_rejections_until_a_record_is_accepted(self):
        entered, release = threading.Event(), threading.Event()
        original = trace.DiagnosticTrace._writer_loop

        def paused(owner, items, stop):
            entered.set()
            release.wait(5)
            original(owner, items, stop)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(trace.DiagnosticTrace, '_writer_loop', paused):
            detailed = trace.DiagnosticTrace(Path(tmp), enabled=True, queue_size=1)
            try:
                self.assertTrue(entered.wait(1))
                for _ in range(100):
                    self.assertFalse(detailed.emit('rejected'))
                self.assertEqual(detailed.dropped_count, 100)
                # Make room deterministically, without racing the real writer.
                detailed._queue.get_nowait()
                self.assertTrue(detailed.emit('accepted'))
                self.assertEqual(detailed._queue.get_nowait()['dropped_before'], 100)
                self.assertEqual(detailed.dropped_count, 0)
            finally:
                release.set()
                detailed.close()

    def test_new_trace_instance_waits_for_old_file_owner(self):
        self._check_new_trace_instance_handoff()

    def test_waiting_trace_can_close_without_waiting_for_stuck_disk(self):
        self._check_new_trace_instance_handoff(close_waiter=True)

    def _check_new_trace_instance_handoff(self, close_waiter=False):
        entered, release = threading.Event(), threading.Event()
        opened, streams = Path.open, []

        class SlowStream:
            def __init__(self, inner):
                self.inner = inner
            def write(self, value):
                if 'blocked_write' in value:
                    entered.set()
                    release.wait(5)
                return self.inner.write(value)
            def flush(self):
                return self.inner.flush()
            def close(self):
                return self.inner.close()

        def open_stream(path, *args, **kwargs):
            inner = opened(path, *args, **kwargs)
            if path.name == trace.TRACE_FILENAME and args and args[0] == 'a':
                streams.append(inner)
                return SlowStream(inner)
            return inner

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Path, 'open', open_stream):
            first = trace.DiagnosticTrace(Path(tmp), enabled=True)
            second = None
            try:
                first.emit('blocked_write')
                self.assertTrue(entered.wait(1))
                first.close(.01)
                second = trace.DiagnosticTrace(Path(tmp), enabled=True)
                second.emit('new_instance')
                self.assertFalse(second.flush(.1))
                self.assertEqual(sum(not stream.closed for stream in streams), 1)
                independent = trace.DiagnosticTrace(Path(tmp, 'independent'), enabled=True)
                try:
                    self.assertTrue(independent.flush())
                finally:
                    independent.close()
                if close_waiter:
                    waiting_thread = second._thread
                    second.close(.5)
                    self.assertFalse(waiting_thread.is_alive())
                    self.assertFalse(second.flush())
                release.set()
                if close_waiter:
                    first.close()
                    second = trace.DiagnosticTrace(Path(tmp), enabled=True)
                    second.emit('new_instance')
                self.assertTrue(second.flush(3))
                first.close()
                second.close()
                rows = [json.loads(line) for line in second.path.read_text().splitlines()]
                self.assertEqual(sum(row['event'] == 'new_instance' for row in rows), 1)
                self.assertEqual(len({row['session_id'] for row in rows}), 2)
            finally:
                release.set()
                first.close()
                if second is not None:
                    second.close()

    def test_rotation_retries_do_not_shift_backups_again(self):
        for denied_name in (trace.TRACE_FILENAME, trace.TRACE_FILENAME + '.1'):
            with self.subTest(denied=denied_name), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(trace, 'TRACE_MAX_BYTES', 1024):
                logs = Path(tmp, 'logs')
                logs.mkdir()
                active = logs / trace.TRACE_FILENAME
                active.write_text('original-active\n' * 100)
                for index in range(1, 4):
                    active.with_name(f'{trace.TRACE_FILENAME}.{index}').write_text(f'backup-{index}')
                replace = Path.replace
                def denied(path, target):
                    if path.name == denied_name:
                        raise PermissionError('reader denies rename')
                    return replace(path, target)
                detailed = None
                try:
                    with mock.patch.object(Path, 'replace', denied):
                        detailed = trace.DiagnosticTrace(Path(tmp), enabled=True)
                        self.assertFalse(detailed.flush())
                        snapshot = {p.name: p.read_bytes() for p in logs.glob(trace.TRACE_FILENAME + '*')}
                        for _ in range(3):
                            detailed._retry_after = 0
                            detailed.emit('retry')
                            self.assertFalse(detailed.flush())
                            self.assertEqual({p.name: p.read_bytes() for p in logs.glob(trace.TRACE_FILENAME + '*')}, snapshot)
                        # Service restarts create a different trace object.
                        detailed.close()
                        detailed = trace.DiagnosticTrace(Path(tmp), enabled=True)
                        self.assertFalse(detailed.flush())
                        self.assertEqual({p.name: p.read_bytes() for p in logs.glob(trace.TRACE_FILENAME + '*')}, snapshot)
                    detailed._retry_after = 0
                    detailed.emit('recovered')
                    self.assertFalse(detailed.flush())
                    self.assertIn('recovered', active.read_text())
                    self.assertEqual(active.with_name(trace.TRACE_FILENAME + '.1').read_text(), 'original-active\n' * 100)
                    self.assertEqual(active.with_name(trace.TRACE_FILENAME + '.2').read_text(), 'backup-1')
                    self.assertEqual(active.with_name(trace.TRACE_FILENAME + '.3').read_text(), 'backup-2')
                finally:
                    if detailed is not None:
                        detailed.close()

    def test_unwritable_basic_log_does_not_block_logger_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(logging_setup, '_configured', False), \
                mock.patch.object(logging_setup, 'LOGGER_NAME', 'test.log.recovery'):
            logger = logging.getLogger('test.log.recovery')
            handler = None
            try:
                with mock.patch.object(logging_setup.EvidenceFileHandler, '_open',
                                       side_effect=PermissionError('unwritable')):
                    logger = logging_setup.get_logger(Path(tmp))
                    handler = logger.handlers[-1]
                    logger.info('cannot save')
                    self.assertFalse(handler.flush_pending())
                # Retry timing is controlled without sleeping through a real backoff.
                handler._retry_after = 0
                logger.info('recovered')
                self.assertFalse(handler.flush_pending())  # Earlier loss remains visible.
                self.assertIn('recovered', Path(tmp, 'logs', 'app.log').read_text())
            finally:
                if handler is not None:
                    handler.close()
                    logger.removeHandler(handler)

    def test_uncreatable_log_directory_does_not_block_logger(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(logging_setup, '_configured', False), \
                mock.patch.object(logging_setup, 'LOGGER_NAME', 'test.log.directory'), \
                mock.patch.object(Path, 'mkdir', side_effect=PermissionError('denied')):
            logger = logging_setup.get_logger(Path(tmp))
            handler = logger.handlers[-1]
            try:
                logger.info('unavailable directory')
                self.assertFalse(handler.flush_pending())
            finally:
                handler.close()
                logger.removeHandler(handler)

    def test_fault_report_recovers_after_log_directory_becomes_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp, 'logs')
            writer = trace.acquire_fault_report(directory)
            try:
                with mock.patch.object(Path, 'mkdir', side_effect=PermissionError('denied')):
                    writer.submit(dict(event='first_failure', success=False, wall_time=1))
                    self.assertFalse(writer.flush())
                    self.assertTrue(writer.thread.is_alive())
                self.assertTrue(writer.flush())
                saved = json.loads((directory / trace.REPORT_FILENAME).read_text())
                self.assertEqual(saved['incidents'][0]['events'][0]['event'], 'first_failure')
            finally:
                trace.release_fault_report(writer)

    def test_trace_open_failure_keeps_worker_and_recovers_with_bounded_retries(self):
        opened = Path.open
        failed = threading.Event()
        attempts = []

        def unavailable(path, *args, **kwargs):
            if path.name == trace.TRACE_FILENAME and args and args[0] == 'a':
                attempts.append(1)
                failed.set()
                raise PermissionError('unwritable')
            return opened(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            detailed = None
            try:
                with mock.patch.object(Path, 'open', unavailable):
                    detailed = trace.DiagnosticTrace(Path(tmp), enabled=True)
                    self.assertTrue(failed.wait(1))
                    for index in range(100):
                        detailed.emit('during_failure', index=index)
                    self.assertFalse(detailed.flush())
                    self.assertTrue(detailed._thread.is_alive())
                    self.assertEqual(len(attempts), 1)
                detailed._retry_after = 0
                detailed.emit('after_recovery')
                self.assertFalse(detailed.flush())
                self.assertIn('after_recovery', detailed.path.read_text())
            finally:
                if detailed is not None:
                    detailed.close()

    def test_trace_flush_failure_recovers_and_export_stays_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            detailed = trace.DiagnosticTrace(Path(tmp), enabled=True)
            try:
                self.assertTrue(detailed.flush())
                with mock.patch.object(detailed._stream, 'flush', side_effect=OSError('disk full')):
                    self.assertFalse(detailed.flush())
                self.assertTrue(detailed._thread.is_alive())
                detailed._retry_after = 0
                detailed.emit('after_flush_failure')
                self.assertFalse(detailed.flush())
                self.assertIn('after_flush_failure', detailed.path.read_text())
                result = log_export.export_logs(Path(tmp, 'export.zip'), root=Path(tmp))
                self.assertTrue(result.incomplete)
                with zipfile.ZipFile(result.path) as archive:
                    info = json.loads(archive.read('export-info.json'))
                    self.assertFalse(info['diagnostic_trace_flushed'])
            finally:
                detailed.close()

    def test_reenable_after_timeout_has_one_file_owner(self):
        self._check_timed_out_handoff()

    def test_pending_session_closes_before_file_handoff(self):
        self._check_timed_out_handoff(close_pending=True)

    def test_repeated_toggles_during_stalled_write_stay_bounded(self):
        self._check_timed_out_handoff(repeat=True)

    def _check_timed_out_handoff(self, close_pending=False, repeat=False):
        entered, release = threading.Event(), threading.Event()
        opened = Path.open
        opens = []

        class SlowStream:
            def __init__(self, inner):
                self.inner = inner
            def write(self, value):
                if 'blocked_write' in value:
                    entered.set()
                    release.wait(3)
                return self.inner.write(value)
            def flush(self):
                self.inner.flush()
            def close(self):
                self.inner.close()

        def open_stream(path, *args, **kwargs):
            inner = opened(path, *args, **kwargs)
            if path.name == trace.TRACE_FILENAME and args and args[0] == 'a':
                opens.append(1)
                return SlowStream(inner)
            return inner

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Path, 'open', open_stream):
            detailed = trace.DiagnosticTrace(Path(tmp), enabled=True)
            try:
                detailed.emit('blocked_write')
                self.assertTrue(entered.wait(1))
                old_thread = detailed._thread
                detailed.close(timeout=.01)
                self.assertTrue(detailed.set_enabled(True))
                if repeat:
                    for _ in range(3):
                        detailed.emit('superseded_session')
                        detailed.close(timeout=.01)
                        self.assertTrue(detailed.set_enabled(True))
                detailed.emit('new_session')
                self.assertIs(detailed._thread, old_thread)
                self.assertEqual(len(opens), 1)
                if close_pending:
                    detailed.close(timeout=.01)
                release.set()
                if not close_pending:
                    self.assertEqual(detailed.flush(), not repeat)
                detailed.close()
                self.assertFalse(old_thread.is_alive())
                rows = [json.loads(line) for line in detailed.path.read_text().splitlines()]
                self.assertEqual(sum(row['event'] == 'new_session' for row in rows), 1)
                self.assertEqual(len({row['session_id'] for row in rows}), 2)
            finally:
                release.set()
                detailed.close()

    def test_failed_record_write_recovers(self):
        opened = Path.open
        fail = [True]

        class Stream:
            def __init__(self, inner):
                self.inner = inner
            def write(self, value):
                if fail[0]:
                    raise OSError('disk full')
                return self.inner.write(value)
            def flush(self):
                self.inner.flush()
            def close(self):
                self.inner.close()

        def open_stream(path, *args, **kwargs):
            inner = opened(path, *args, **kwargs)
            return (Stream(inner) if path.name == trace.TRACE_FILENAME
                    and args and args[0] == 'a' else inner)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Path, 'open', open_stream):
            detailed = trace.DiagnosticTrace(Path(tmp), enabled=True)
            try:
                self.assertFalse(detailed.flush())
                fail[0] = False
                detailed._retry_after = 0
                detailed.emit('after_record_failure')
                self.assertFalse(detailed.flush())
                self.assertIn('after_record_failure', detailed.path.read_text())
            finally:
                detailed.close()

    def test_failed_rotation_stops_growth_and_later_resumes(self):
        replace = Path.replace
        attempts = []

        def deny_rotation(path, target):
            if path.name == trace.TRACE_FILENAME:
                attempts.append(1)
                raise PermissionError('reader denies rename')
            return replace(path, target)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(trace, 'TRACE_MAX_BYTES', 1024):
            detailed = trace.DiagnosticTrace(Path(tmp), enabled=True)
            try:
                with mock.patch.object(Path, 'replace', deny_rotation):
                    for index in range(30):
                        detailed.emit('rotation_probe', index=index)
                    self.assertFalse(detailed.flush())
                    self.assertLessEqual(detailed.path.stat().st_size, 1024)
                    self.assertEqual(len(attempts), 1)
                detailed._retry_after = 0
                detailed.emit('rotation_recovered')
                self.assertFalse(detailed.flush())
                self.assertIn('rotation_recovered', detailed.path.read_text())
            finally:
                detailed.close()

    def test_full_fault_queue_keeps_first_error_and_barriers(self):
        writer = trace._ReportWriter.__new__(trace._ReportWriter)
        writer.lock, writer.stop = threading.Lock(), threading.Event()
        writer.thread = mock.Mock()
        writer.thread.is_alive.return_value = True
        writer.queue, writer.dropped, writer.session_id = queue.Queue(maxsize=3), 0, 'probe'
        writer.submit(dict(event='first', success=False))
        barrier = threading.Event()
        writer.queue.put_nowait(barrier)
        writer.submit(dict(event='ordinary'))
        writer.submit(dict(event='second', success=False))
        writer.submit(dict(event='third', success=False))
        self.assertEqual(writer.queue.qsize(), 3)
        self.assertEqual(writer.queue.get_nowait()['event'], 'first')
        self.assertIs(writer.queue.get_nowait(), barrier)
        second = writer.queue.get_nowait()
        self.assertEqual(second['event'], 'second')
        self.assertEqual(second['queue_dropped_before'], 1)
        self.assertEqual(writer.dropped, 1)
