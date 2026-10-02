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
        names = ('app.log', 'app.log.1', 'app.log.2', 'app.log.3',
                 'hid-helper.log', 'hid-helper.log.1', 'diagnostic-trace.jsonl',
                 'diagnostic-trace.jsonl.1', 'diagnostic-trace.jsonl.2',
                 'diagnostic-trace.jsonl.3', 'diagnostic-report.json')
        for name in names:
            self.write_log(name)
        for name in ('config.json', 'capture.wav', 'app.log.4', 'hid-helper.log.2',
                     'diagnostic-trace.jsonl.4',
                     'app.log.99', 'unknown.log', 'diagnostic-report.tmp'):
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
            self.assertEqual(sum(row['status'] == 'included' for row in manifest['files']), len(names))

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

    def test_only_empty_logs_do_not_report_success_or_replace_an_existing_zip(self):
        names = ('app.log', 'diagnostic-trace.jsonl', 'hid-helper.log')
        for name in names:
            self.write_log(name, b'')
        self.destination.write_bytes(b'previous archive')
        result = log_export.export_logs(self.destination, root=self.root)
        self.assertEqual(result.outcome, 'no_logs')
        self.assertEqual(result.file_count, 0)
        self.assertIn('暂无可导出的日志', log_export.describe_result(result))
        self.assertEqual(self.destination.read_bytes(), b'previous archive')
        self.assertTrue(all((self.logs / name).read_bytes() == b'' for name in names))
        self.assertEqual(list(self.root.glob('.remote-mic-logs-*')), [])

    def test_empty_logs_beside_real_content_keep_the_zip_manifest_consistent(self):
        self.write_log(content=b'')
        self.write_log('hid-helper.log', b'actual event\n')
        result = log_export.export_logs(self.destination, root=self.root)
        self.assertEqual(result.outcome, 'exported')
        self.assertEqual(result.file_count, 2)
        self.assertFalse(result.incomplete)
        with zipfile.ZipFile(self.destination) as archive:
            manifest = json.loads(archive.read('export-info.json'))
            included = {row['name'] for row in manifest['files'] if row['status'] == 'included'}
            self.assertEqual(included, {'app.log', 'hid-helper.log'})
            self.assertEqual(set(archive.namelist()), included | {'export-info.json'})
            self.assertEqual(archive.read('app.log'), b'')
            self.assertEqual(archive.read('hid-helper.log'), b'actual event\n')
            self.assertEqual(manifest['schema_version'], 1)

    def test_empty_logs_do_not_hide_a_read_failure(self):
        self.write_log(content=b'')
        original = log_export._snapshot
        def read(path):
            if path.name == 'hid-helper.log':
                raise PermissionError('cannot read')
            return original(path)
        with mock.patch.object(log_export, '_snapshot', side_effect=read):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertEqual(result.outcome, 'read_failed')
        self.assertFalse(self.destination.exists())

    def test_empty_collection_with_a_flush_failure_is_not_reported_as_no_logs(self):
        self.write_log(content=b'')
        self.destination.write_bytes(b'previous archive')
        for module, function in (
            (log_export.logging_setup, 'flush_application_logs'),
            (log_export.diagnostic_trace, 'flush_diagnostic_logs'),
            (log_export.diagnostic_trace, 'flush_fault_report'),
        ):
            with self.subTest(function=function), mock.patch.object(module, function, return_value=False):
                result = log_export.export_logs(self.destination, root=self.root)
                self.assertEqual(result.outcome, 'read_failed')
                self.assertEqual(self.destination.read_bytes(), b'previous archive')

    def test_truncation_that_retains_no_complete_line_is_reported_as_read_failure(self):
        self.write_log(content=b'one line longer than the export cap')
        with mock.patch.object(log_export, 'MAX_FILE_BYTES', 8):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertEqual(result.outcome, 'read_failed')
        self.assertFalse(self.destination.exists())

    def test_large_text_file_retains_recent_complete_lines_and_marks_truncation(self):
        self.write_log(content=b'old line\nnew line\nlast line\n')
        with mock.patch.object(log_export, 'MAX_FILE_BYTES', 16):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertTrue(result.incomplete)
        with zipfile.ZipFile(self.destination) as archive:
            self.assertEqual(archive.read('app.log'), b'last line\n')

    def test_legacy_log_export_uses_current_512_kib_cap(self):
        names = ('app.log', 'app.log.2', 'diagnostic-trace.jsonl.3')
        for name in names:
            self.write_log(name, b'old marker\n' + (b'x' * 100 + b'\n') * 6000 + b'new marker\n')
        result = log_export.export_logs(self.destination, root=self.root)
        self.assertTrue(result.incomplete)
        with zipfile.ZipFile(self.destination) as archive:
            records = {row['name']: row for row in json.loads(archive.read('export-info.json'))['files']}
            for name in names:
                exported = archive.read(name)
                self.assertLessEqual(len(exported), log_export.MAX_FILE_BYTES)
                self.assertNotIn(b'old marker', exported)
                self.assertTrue(exported.endswith(b'new marker\n'))
                self.assertTrue(records[name]['truncated'])

    def test_report_keeps_its_separate_capacity_when_log_export_is_bounded(self):
        report = b'{"schema":2,"incidents":[]}'
        self.write_log('diagnostic-report.json', report)
        with mock.patch.object(log_export, 'MAX_FILE_BYTES', 16):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertFalse(result.incomplete)
        with zipfile.ZipFile(self.destination) as archive:
            self.assertEqual(archive.read('diagnostic-report.json'), report)

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

    def test_rotation_retries_whole_collection_and_keeps_newest_log_once(self):
        self.write_log(content=b'previous current\n')
        self.write_log('app.log.1', b'old backup\n')
        original = log_export._snapshot
        rotated = False
        def read(path):
            nonlocal rotated
            result = original(path)
            if path.name == 'app.log' and not rotated:
                rotated = True
                (self.logs / 'app.log.1').replace(self.logs / 'app.log.2')
                path.replace(self.logs / 'app.log.1')
                path.write_bytes(b'new during export\n')
            return result
        with mock.patch.object(log_export, '_snapshot', side_effect=read):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertFalse(result.incomplete)
        with zipfile.ZipFile(self.destination) as archive:
            self.assertEqual(archive.read('app.log'), b'new during export\n')
            self.assertEqual(archive.read('app.log.1'), b'previous current\n')
            self.assertEqual(archive.read('app.log.2'), b'old backup\n')
            manifest = json.loads(archive.read('export-info.json'))
            self.assertEqual(manifest['snapshot_attempts'], 2)
            self.assertTrue(manifest['snapshot_stable'])
            self.assertNotIn('_identity', str(manifest))

    def test_continuous_file_changes_are_bounded_and_marked_incomplete(self):
        self.write_log()
        original = log_export._snapshot
        attempts = []
        def read(path):
            result = original(path)
            if path.name == 'app.log':
                attempts.append(1)
                path.replace(self.logs / 'app.log.1')
                path.write_bytes(b'next current\n')
            return result
        with mock.patch.object(log_export, '_snapshot', side_effect=read):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(result.incomplete)
        self.assertIn('部分日志', log_export.describe_result(result))
        with zipfile.ZipFile(self.destination) as archive:
            manifest = json.loads(archive.read('export-info.json'))
            self.assertFalse(manifest['snapshot_stable'])
            self.assertTrue(manifest['incomplete'])
        from tests.test_log_bundle_scope import audit_module
        limitations = audit_module.audit(self.destination)['limitations']
        self.assertIn('export_changed_during_collection', limitations)
        self.assertIn('export_marked_incomplete', limitations)

    def test_changed_during_read_reaches_ui_and_manifest(self):
        self.write_log()
        original = log_export._snapshot
        def read(path):
            content, metadata = original(path)
            metadata['changed_during_read'] = True
            return content, metadata
        with mock.patch.object(log_export, '_snapshot', side_effect=read):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertTrue(result.incomplete)
        self.assertIn('部分日志', log_export.describe_result(result))
        with zipfile.ZipFile(self.destination) as archive:
            manifest = json.loads(archive.read('export-info.json'))
            self.assertTrue(manifest['files'][0]['changed_during_read'])
            self.assertTrue(manifest['incomplete'])

    def test_new_file_in_an_already_read_slot_triggers_retry(self):
        self.write_log('app.log.1')
        original = log_export._snapshot
        def read(path):
            result = original(path)
            if path.name == 'app.log.1' and not (self.logs / 'app.log').exists():
                self.write_log(content=b'new current\n')
            return result
        with mock.patch.object(log_export, '_snapshot', side_effect=read):
            result = log_export.export_logs(self.destination, root=self.root)
        self.assertFalse(result.incomplete)
        with zipfile.ZipFile(self.destination) as archive:
            self.assertEqual(archive.read('app.log'), b'new current\n')

    def test_non_regular_log_is_unreadable_and_never_followed(self):
        self.write_log()
        metadata = types.SimpleNamespace(st_mode=stat.S_IFLNK)
        with mock.patch.object(Path, 'lstat', return_value=metadata), mock.patch.object(Path, 'open') as opened:
            with self.assertRaises(OSError):
                log_export._snapshot(self.logs / 'app.log')
            opened.assert_not_called()
