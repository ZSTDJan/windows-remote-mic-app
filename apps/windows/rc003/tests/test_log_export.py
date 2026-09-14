import json
from pathlib import Path
import stat
import tempfile
import threading
import types
import unittest
from unittest import mock
import zipfile

from ovb_rc003 import __version__, log_export


class LogExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.logs = self.root / 'logs'
        self.destination = self.root / '日志 空格.zip'

    def write_log(self, name='app.log', content=b'recent event\n'):
        self.logs.mkdir(exist_ok=True)
        path = self.logs / name
        path.write_bytes(content)
        return path

    def test_actual_zip_includes_known_logs_and_backups_without_private_extras(self):
        names = ('app.log', 'app.log.1', 'app.log.3', 'hid-helper.log',
                 'diagnostic-trace.jsonl', 'diagnostic-trace.jsonl.3', 'diagnostic-report.json')
        for name in names:
            self.write_log(name)
        for name in ('config.json', 'capture.wav', 'app.log.99', 'unknown.log', 'diagnostic-report.tmp'):
            self.write_log(name, b'PRIVATE')
        result = log_export.export_logs(self.destination, root=self.root)
        self.assertEqual(result.outcome, 'exported')
        self.assertEqual(result.file_count, len(names))
        self.assertFalse(result.incomplete)
        with zipfile.ZipFile(self.destination) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), {*names, 'export-info.json'})
            for name in names:
                self.assertEqual(archive.read(name), (self.logs / name).read_bytes())
            manifest = json.loads(archive.read('export-info.json'))
            self.assertEqual(manifest['app_version'], __version__)
            self.assertNotIn(str(self.root), json.dumps(manifest))
            self.assertFalse(manifest['includes_configuration'])
            self.assertTrue(any(row['status'] == 'missing' for row in manifest['files']))

    def test_no_logs_does_not_create_directory_or_overwrite_destination(self):
        self.destination.write_bytes(b'previous archive')
        result = log_export.export_logs(self.destination, root=self.root)
        self.assertEqual(result.outcome, 'no_logs')
        self.assertFalse(self.logs.exists())
        self.assertEqual(self.destination.read_bytes(), b'previous archive')

    def test_partial_read_failure_is_visible_in_result_and_archive(self):
        self.write_log()
        self.write_log('hid-helper.log')
        original = log_export._snapshot
        def read(path):
            if path.name == 'hid-helper.log':
                raise PermissionError('private path')
            return original(path)
        with mock.patch.object(log_export, '_snapshot', side_effect=read):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertTrue(result.incomplete)
        with zipfile.ZipFile(self.destination) as archive:
            manifest = archive.read('export-info.json')
            self.assertIn(b'unreadable', manifest)
            self.assertNotIn(b'private path', manifest)
        self.assertIn('部分日志', log_export.describe_result(result))

    def test_large_text_file_retains_recent_complete_lines_and_marks_truncation(self):
        self.write_log(content=b'old line\nnew line\nlast line\n')
        with mock.patch.object(log_export, 'MAX_FILE_BYTES', 16):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertTrue(result.incomplete)
        with zipfile.ZipFile(self.destination) as archive:
            self.assertEqual(archive.read('app.log'), b'last line\n')

    def test_failed_publish_preserves_existing_file_and_removes_scratch(self):
        self.write_log()
        self.destination.write_bytes(b'keep')
        with mock.patch.object(log_export.os, 'replace', side_effect=PermissionError('private')):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertEqual(result.outcome, 'write_failed')
        self.assertEqual(self.destination.read_bytes(), b'keep')
        self.assertEqual(list(self.root.glob('.remote-mic-logs-*')), [])

    def test_cancellation_during_compression_discards_temporary_zip(self):
        self.write_log()
        cancelled = threading.Event()
        original = zipfile.ZipFile.writestr
        def write(archive, *args, **kwargs):
            original(archive, *args, **kwargs)
            cancelled.set()
        with mock.patch.object(zipfile.ZipFile, 'writestr', new=write):
            result = log_export.export_logs(self.destination, root=self.root, cancel=cancelled)
        self.assertEqual(result.outcome, 'cancelled')
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.remote-mic-logs-*')), [])

    def test_relative_and_non_zip_destinations_are_rejected(self):
        for destination in (Path('file.zip'), self.root / 'file.txt'):
            self.assertEqual(log_export.export_logs(destination, root=self.root).outcome, 'invalid_destination')

    def test_non_regular_log_is_unreadable_and_never_followed(self):
        self.write_log()
        metadata = types.SimpleNamespace(st_mode=stat.S_IFLNK)
        with mock.patch.object(Path, 'lstat', return_value=metadata), mock.patch.object(Path, 'open') as opened:
            with self.assertRaises(OSError):
                log_export._snapshot(self.logs / 'app.log')
            opened.assert_not_called()
