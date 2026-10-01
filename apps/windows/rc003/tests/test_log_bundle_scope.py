"""Developer audit scope: prioritize feedback without losing its full run."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).parents[1] / 'scripts' / 'audit_log_bundle.py'
spec = importlib.util.spec_from_file_location('audit_log_bundle_scope', SCRIPT)
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


def line(time, message, level='INFO'):
    return f'2026-09-28 {time},000 {level} app: {message}\n'


def start(time, version='1.0.80'):
    return line(time, f'startup: app identity: version={version}')


def run(time, key, failed=False):
    record = dict(kind='hid_startup', step='attach', state='failed' if failed else 'success')
    return line(time, f'Chromecast evidence run={key} seq=1 record={json.dumps(record)}')


class LogBundleScopeTests(unittest.TestCase):
    def bundle(self, files, version='1.0.80'):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        path = Path(root.name) / 'logs.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            archive.writestr('export-info.json', json.dumps(dict(app_version=version,
                exported_at='2026-09-28T23:00:00+08:00', files=[], application_log_flushed=True,
                diagnostic_trace_flushed=True, fault_report_flushed=True)))
        return path

    def test_latest_round_keeps_retries_but_not_same_version_morning_or_old_versions(self):
        text = start('07:00:00') + run('07:01:00', 'aaaaaaaaaaaa', True)
        text += start('10:00:00') + run('10:00:01', 'bbbbbbbbbbbb', True)
        text += start('10:00:05') + run('10:00:06', 'cccccccccccc', True)
        path = self.bundle({'app.log': text,
            'app.log.1': start('06:00:00', '1.0.72') + run('06:00:01', 'dddddddddddd', True)})
        result = audit_module.audit(path)
        view = audit_module.focused_output(result)
        self.assertEqual(list(view['focus']['runs']), ['bbbbbbbbbbbb', 'cccccccccccc'])
        self.assertEqual(view['focus']['first'], '2026-09-28 10:00:00,000')
        self.assertEqual(view['history_summary']['outside_focus_runs'], 2)
        self.assertNotIn('runs', view)  # No unbounded history payload in default output.
        self.assertEqual(view['focus']['basis'], 'inferred_latest_round')
        self.assertEqual(len(result['runs']), 4)  # Full inventory remains available.

    def test_window_expands_overlapping_run_across_rotation_with_cleanup_and_trace(self):
        key = 'aaaaaaaaaaaa'
        before = start('10:00:00') + run('10:00:01', key, True) + 'Traceback first line\n'
        after = run('10:02:00', key) + line('10:02:02', 'cleanup finished')
        after += start('11:00:00') + run('11:00:01', 'bbbbbbbbbbbb', True)
        epoch = audit_module.datetime.fromisoformat('2026-09-28T10:02:02+08:00').timestamp()
        trace = json.dumps(dict(event='query_result', wall_time=epoch)) + '\n'
        report = dict(incidents=[dict(started_at=epoch-121, last_failure_at=epoch-120),
                                 dict(started_at=epoch-3600, last_failure_at=epoch-3590)])
        path = self.bundle({'app.log.1': before, 'app.log': after,
                            'diagnostic-trace.jsonl': trace, 'diagnostic-report.json': json.dumps(report)})
        focus = audit_module.audit(path, since='2026-09-28T02:01:00Z',
                                   until='2026-09-28T10:01:30')['focus']
        self.assertEqual(focus['basis'], 'specified_window')
        self.assertEqual(focus['requested']['since'], '2026-09-28 10:01:00,000')
        self.assertEqual(focus['first'], '2026-09-28 10:00:00,000')
        self.assertEqual(focus['last'], '2026-09-28 10:02:02,000')
        self.assertEqual(len(focus['runs'][key]['failures']), 1)
        ranges = {row['name']: row for row in focus['source_ranges']}
        self.assertEqual(ranges['app.log.1']['last_line'], 3)  # Full traceback continuation.
        self.assertEqual(ranges['app.log']['last_line'], 2)
        self.assertEqual(focus['trace_events']['diagnostic-trace.jsonl'], {'query_result': 1})
        self.assertEqual(len(focus['report_incidents']), 1)

    def test_window_includes_same_round_retries_on_both_sides_only(self):
        text = start('07:00:00') + run('07:00:01', 'aaaaaaaaaaaa', True)
        text += start('10:00:00') + run('10:00:01', 'bbbbbbbbbbbb', True)
        text += run('10:01:00', 'cccccccccccc', True) + run('10:02:00', 'dddddddddddd', True)
        text += start('11:00:00') + run('11:00:01', 'eeeeeeeeeeee', True)
        focus = audit_module.audit(self.bundle({'app.log': text}),
            since='2026-09-28T10:00:59', until='2026-09-28T10:01:01')['focus']
        self.assertEqual(list(focus['runs']), ['bbbbbbbbbbbb', 'cccccccccccc', 'dddddddddddd'])
        self.assertEqual(focus['window_overlapping_runs'], ['cccccccccccc'])
        self.assertEqual(focus['round_expansion_runs'], ['bbbbbbbbbbbb', 'dddddddddddd'])
        self.assertEqual(focus['requested']['since'], '2026-09-28 10:00:59,000')
        self.assertIn('specified_window_expanded_to_inferred_round_review_added_runs', focus['warnings'])

    def test_context_after_run_start_cannot_assign_or_filter_that_run(self):
        text = start('10:00:00') + run('10:00:01', 'aaaaaaaaaaaa', True)
        text += run('10:00:05', 'bbbbbbbbbbbb', True)
        epoch = audit_module.datetime.fromisoformat('2026-09-28T10:00:00+08:00').timestamp()
        trace = '\n'.join(json.dumps(dict(event='device_context', app_version='1.0.80',
                         wall_time=epoch+delta, selected_ref=device))
                         for delta, device in [(0, 'aaa'), (5.5, 'bbb')])
        path = self.bundle({'app.log': text, 'diagnostic-trace.jsonl': trace})
        focus = audit_module.audit(path, device='aaa')['focus']
        self.assertEqual(list(focus['runs']), ['aaaaaaaaaaaa', 'bbbbbbbbbbbb'])
        self.assertEqual(focus['runs']['bbbbbbbbbbbb']['device_ref'], 'aaa')
        # A startup of the same version still invalidates stale device context.
        text = text.replace(run('10:00:05', 'bbbbbbbbbbbb', True),
                            start('10:00:04') + run('10:00:05', 'bbbbbbbbbbbb', True))
        result = audit_module.audit(self.bundle({'app.log': text, 'diagnostic-trace.jsonl': trace}), device='aaa')
        self.assertIsNone(result['runs']['bbbbbbbbbbbb']['device_ref'])
        self.assertIn('bbbbbbbbbbbb', result['focus']['runs'])
        self.assertIn('some_runs_have_no_device_binding', result['focus']['warnings'])

    def test_round_expansion_stops_at_device_and_version_switches(self):
        text = start('10:00:00') + run('10:00:01', 'aaaaaaaaaaaa', True)
        text += run('10:00:05', 'bbbbbbbbbbbb', True) + run('10:00:09', 'cccccccccccc', True)
        text += start('10:00:10', '1.0.79') + run('10:00:11', 'dddddddddddd', True)
        epoch = audit_module.datetime.fromisoformat('2026-09-28T10:00:00+08:00').timestamp()
        trace = '\n'.join(json.dumps(dict(event='device_context', app_version='1.0.80',
                         wall_time=epoch+delta, selected_ref=device))
                         for delta, device in [(0, 'aaa'), (4, 'bbb'), (8, 'aaa')])
        focus = audit_module.audit(self.bundle({'app.log': text, 'diagnostic-trace.jsonl': trace}),
            since='2026-09-28T10:00:08', until='2026-09-28T10:00:09', device='aaa')['focus']
        self.assertEqual(list(focus['runs']), ['cccccccccccc'])
        self.assertFalse(focus['round_expansion_runs'])

    def test_new_program_with_no_runs_does_not_relabel_old_failures(self):
        path = self.bundle({'app.log': start('10:00:00', '1.0.79') +
            run('10:00:01', 'aaaaaaaaaaaa', True) + start('10:00:05') +
            line('10:00:06', 'startup failed', 'ERROR')})
        focus = audit_module.audit(path)['focus']
        self.assertEqual(focus['run_count'], 0)
        self.assertEqual(len(focus['application_findings']), 1)
        self.assertIn('no_matching_runs_does_not_mean_no_failure', focus['warnings'])
        self.assertEqual(focus['startup_context']['version'], '1.0.80')

    def test_nearby_empty_restart_is_kept_and_version_switch_breaks_round(self):
        text = start('10:00:00') + run('10:00:01', 'aaaaaaaaaaaa', True)
        text += start('10:00:05', '1.0.79') + start('10:00:10')
        text += run('10:00:11', 'bbbbbbbbbbbb', True)
        text += start('10:00:15') + line('10:00:16', 'startup failed', 'ERROR')
        focus = audit_module.audit(self.bundle({'app.log': text}))['focus']
        self.assertEqual(list(focus['runs']), ['bbbbbbbbbbbb'])
        self.assertIn('latest_startup_has_no_receiver_run', focus['warnings'])
        self.assertEqual(focus['last'], '2026-09-28 10:00:16,000')
        self.assertEqual(len(focus['application_findings']), 1)

    def test_filters_do_not_join_tests_across_another_device(self):
        text = ''
        contexts = []
        for minute, key, device in [(0, 'aaaaaaaaaaaa', 'aaa'),
                                     (5, 'bbbbbbbbbbbb', 'bbb'), (10, 'cccccccccccc', 'aaa')]:
            t = f'10:{minute:02d}:00'
            text += start(t) + run(t[:-2]+'01', key, True)
            wall = audit_module.datetime.fromisoformat(f'2026-09-28T{t}+08:00').timestamp()
            contexts.append(json.dumps(dict(event='device_context', wall_time=wall,
                                            app_version='1.0.80', selected_ref=device)))
        path = self.bundle({'app.log': text, 'diagnostic-trace.jsonl': '\n'.join(contexts)})
        focus = audit_module.audit(path, device='aaa')['focus']
        self.assertEqual(list(focus['runs']), ['cccccccccccc'])
        self.assertIn('version_and_device_association_requires_context_check', focus['warnings'])
        wrong = audit_module.audit(path, device='missing')['focus']
        self.assertEqual(wrong['run_count'], 0)
        self.assertIn('requested_device_not_confirmed_in_scope', wrong['warnings'])

    def test_missing_identity_and_invalid_trace_stay_visible(self):
        path = self.bundle({'app.log': run('10:00:01', 'aaaaaaaaaaaa', True),
                            'diagnostic-trace.jsonl': 'BROKEN\n'})
        result = audit_module.audit(path)
        self.assertIn('startup_boundary_missing_or_rotated_out', result['focus']['warnings'])
        self.assertIn('unscoped_lines:diagnostic-trace.jsonl', result['focus']['warnings'])
        self.assertIn('invalid_trace:diagnostic-trace.jsonl', result['limitations'])
        self.assertIn('export_version_differs_from_selected_runs', result['focus']['warnings'])

    def test_unknown_device_runs_are_not_silently_discarded(self):
        path = self.bundle({'app.log': start('10:00:00') + run('10:00:01', 'aaaaaaaaaaaa', True)})
        focus = audit_module.audit(path, device='aaa')['focus']
        self.assertEqual(focus['run_count'], 1)
        self.assertIn('some_runs_have_no_device_binding', focus['warnings'])
        self.assertIn('requested_device_not_confirmed_in_scope', focus['warnings'])

    def test_latest_unbound_run_is_kept_when_group_has_another_known_device(self):
        text = start('09:58:00') + run('09:58:01', 'bbbbbbbbbbbb')
        text += start('10:00:00') + run('10:00:01', 'aaaaaaaaaaaa')
        text += start('10:01:00') + run('10:01:01', 'cccccccccccc', True)
        trace = '\n'.join(json.dumps(dict(event='device_context', app_version='1.0.80',
                selected_ref=device, wall_time=audit_module.datetime.fromisoformat(
                    f'2026-09-28T{time}+08:00').timestamp()))
                for time, device in [('09:58:00', 'bbb'), ('10:00:00', 'aaa')])
        result = audit_module.audit(self.bundle({'app.log': text, 'diagnostic-trace.jsonl': trace}), device='bbb')
        self.assertEqual(list(result['focus']['runs']), ['cccccccccccc'])
        self.assertIsNone(result['focus']['runs']['cccccccccccc']['device_ref'])
        self.assertIn('some_runs_have_no_device_binding', result['focus']['warnings'])
        self.assertIn('requested_device_not_confirmed_in_scope', result['focus']['warnings'])

    def test_device_filter_warns_if_only_an_older_matching_round_exists(self):
        text = start('10:00:00') + run('10:00:01', 'aaaaaaaaaaaa')
        text += run('10:01:01', 'bbbbbbbbbbbb')
        trace = '\n'.join(json.dumps(dict(event='device_context', app_version='1.0.80',
                selected_ref=device, wall_time=audit_module.datetime.fromisoformat(
                    f'2026-09-28T{time}+08:00').timestamp()))
                for time, device in [('10:00:00', 'aaa'), ('10:01:00', 'bbb')])
        result = audit_module.audit(self.bundle({'app.log': text, 'diagnostic-trace.jsonl': trace}), device='aaa')
        self.assertEqual(list(result['focus']['runs']), ['aaaaaaaaaaaa'])
        self.assertIn('matching_round_is_older_than_latest_receiver_run', result['focus']['warnings'])

    def test_invalid_or_empty_requested_range_never_falls_back_to_latest_runs(self):
        path = self.bundle({'app.log': start('10:00:00') + run('10:00:01', 'aaaaaaaaaaaa', True)})
        with self.assertRaisesRegex(ValueError, 'since_after_until'):
            audit_module.audit(path, since='2026-09-28T11:00', until='2026-09-28T10:00')
        focus = audit_module.audit(path, since='2026-09-28T09:00', until='2026-09-28T09:01')['focus']
        self.assertEqual(focus['run_count'], 0)
        self.assertFalse(focus['source_ranges'])
        focus = audit_module.audit(path, version='does-not-exist')['focus']
        self.assertFalse(focus['source_ranges'])
        self.assertIn('requested_version_not_found', focus['warnings'])
        for bounds in [dict(since='2026-09-28T11:00'), dict(until='2026-09-28T09:00'),
                       dict(since='2026-09-28T11:00', until='2026-09-28T12:00')]:
            focus = audit_module.audit(path, **bounds)['focus']
            self.assertEqual(focus['run_count'], 0)
            self.assertFalse(focus['source_ranges'])
            self.assertIn('scope_has_no_available_records', focus['warnings'])

    def test_wide_requested_window_points_to_actual_selected_startup(self):
        text = start('07:00:00', '1.0.72') + line('07:00:01', 'old error', 'ERROR')
        text += start('10:00:00') + run('10:00:01', 'aaaaaaaaaaaa', True)
        focus = audit_module.audit(self.bundle({'app.log': text}),
            since='2026-09-28T09:00', until='2026-09-28T11:00')['focus']
        self.assertEqual(focus['first'], '2026-09-28 09:00:00,000')
        self.assertEqual(focus['startup_context']['version'], '1.0.80')
        self.assertFalse(focus['application_findings'])

    def test_overlapping_device_runs_are_not_hidden_by_selected_device(self):
        text = start('10:00:00') + run('10:00:01', 'aaaaaaaaaaaa')
        text += run('10:00:03', 'bbbbbbbbbbbb') + run('10:00:05', 'aaaaaaaaaaaa', True)
        epoch = audit_module.datetime.fromisoformat('2026-09-28T10:00:00+08:00').timestamp()
        trace = '\n'.join(json.dumps(dict(event='device_context', app_version='1.0.80',
                            wall_time=epoch+delta, selected_ref=device))
                            for delta, device in [(0, 'aaa'), (3, 'bbb')])
        path = self.bundle({'app.log': text, 'diagnostic-trace.jsonl': trace})
        focus = audit_module.audit(path, since='2026-09-28T10:00',
                                   until='2026-09-28T10:01', device='aaa')['focus']
        self.assertEqual(list(focus['runs']), ['aaaaaaaaaaaa'])
        self.assertEqual(focus['other_runs_in_context'], ['bbbbbbbbbbbb'])
        self.assertIn('context_contains_other_runs_do_not_attribute_all_to_selected_device', focus['warnings'])

    def test_command_defaults_to_focus_and_history_requires_explicit_option(self):
        path = self.bundle({'app.log': start('10:00:00') + run('10:00:01', 'aaaaaaaaaaaa', True)})
        base = [sys.executable, '-B', str(SCRIPT), str(path)]
        view = json.loads(subprocess.run(base, check=True, capture_output=True).stdout)
        self.assertIn('focus', view)
        self.assertNotIn('runs', view)
        full = json.loads(subprocess.run(base + ['--all-history'], check=True, capture_output=True).stdout)
        self.assertIn('runs', full)


if __name__ == '__main__':
    unittest.main()
