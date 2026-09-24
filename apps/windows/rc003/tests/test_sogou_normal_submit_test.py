"""Shared normal-submit matcher and bounded product integration guards."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import subprocess
from types import SimpleNamespace
from unittest import TestCase, mock

from ovb_rc003 import sogou_submit_windows as probe


class _Control:
    def __init__(self, *, kind="GroupControl", automation_id="", name="", rect=(0, 0, 100, 100),
                 enabled=True, offscreen=False, invoke=False, children=()):
        self.ControlTypeName = kind
        self.AutomationId = automation_id
        self.Name = name
        self.BoundingRectangle = SimpleNamespace(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3])
        self.IsEnabled = enabled
        self.IsOffscreen = offscreen
        self.invoke = invoke
        self.children = tuple(children)

    def GetChildren(self):
        return self.children

    def GetPattern(self, _):
        return object() if self.invoke else None


class SogouNormalSubmitTestTests(TestCase):
    def _popup(self):
        left = _Control(rect=(150, 248, 190, 288), invoke=True)
        right = _Control(rect=(196, 248, 236, 288), invoke=True)
        bar = _Control(rect=(100, 230, 260, 300), children=(
            _Control(rect=(108, 238, 140, 290)),
            _Control(kind="TextControl", rect=(142, 242, 148, 288)), left, right,
        ))
        semantic = _Control(automation_id="root", rect=(0, 0, 320, 300), children=(bar,))
        return _Control(kind="WindowControl", rect=(0, 0, 320, 300), children=(semantic,)), right

    def test_only_right_of_exact_pair_is_selected(self):
        root, expected = self._popup()
        control, route = probe.find_normal_submit(root, object())
        self.assertIs(control, expected)
        self.assertEqual(route, (0, 0, 3))

    def test_recognition_group_preserves_the_same_right_button(self):
        root, expected = self._popup()
        root.children[0].children[0].children[1].ControlTypeName = "GroupControl"
        diagnostics = {}
        control, route = probe.find_normal_submit(root, object(), diagnostics=diagnostics)
        self.assertIs(control, expected)
        self.assertEqual(route, (0, 0, 3))
        self.assertEqual(diagnostics['bar_child_types'], ['GroupControl'] * 4)

    def test_saved_real_sogou_trees_recognize_only_right_action(self):
        fixture = Path(__file__).with_name('fixtures') / 'sogou_submit_trees.json'
        for frame in json.loads(fixture.read_text(encoding='utf-8')):
            with self.subTest(frame=frame['label']):
                controls = {tuple(row['route']): _Control(
                    kind=row['type'], automation_id=row['automation_id'],
                    rect=row['rect'], enabled=row['enabled'], offscreen=row['offscreen'],
                    invoke=row['invoke']) for row in frame['nodes']}
                for route, control in controls.items():
                    control.children = tuple(controls[key] for key in sorted(controls)
                                             if len(key) == len(route) + 1 and key[:-1] == route)
                right, route = probe.find_normal_submit(controls[()], object())
                self.assertIsNotNone(right)
                self.assertEqual(route, tuple(frame['expected_route']))
                self.assertIs(right, controls[route])

    def test_invoke_uses_actual_group_api_once(self):
        import uiautomation as auto
        # Using the installed class API catches the former nonexistent method.
        group = mock.create_autospec(auto.GroupControl, instance=True, spec_set=True)
        pattern = group.GetPattern.return_value
        pattern.Invoke.return_value = True
        candidate = probe.Candidate(group, 123, 456, (0, 0, 3), (196, 248, 236, 288))
        with mock.patch.object(auto, 'UIAutomationInitializerInThread'), \
                mock.patch.object(probe, '_scan_windows', return_value=({'state': 'ready'}, candidate)), \
                mock.patch.object(probe, '_capture_active', return_value=True):
            result = probe.invoke_once(Path('test.exe'), candidate.record())
        self.assertEqual(result['state'], 'invoked_once')
        group.GetPattern.assert_called_once_with(auto.PatternId.InvokePattern)
        pattern.Invoke.assert_called_once_with(waitTime=0)

    def test_changed_candidate_or_capture_never_invokes(self):
        import uiautomation as auto
        for changed in ('candidate', 'capture'):
            with self.subTest(changed=changed):
                group = mock.create_autospec(auto.GroupControl, instance=True, spec_set=True)
                candidate = probe.Candidate(group, 123, 456, (0, 0, 3), (196, 248, 236, 288))
                expected = candidate.record()
                if changed == 'candidate':
                    expected['hwnd'] = 999
                with mock.patch.object(auto, 'UIAutomationInitializerInThread'), \
                        mock.patch.object(probe, '_scan_windows', return_value=({'state': 'ready'}, candidate)), \
                        mock.patch.object(probe, '_capture_active', return_value=False):
                    result = probe.invoke_once(Path('test.exe'), expected)
                self.assertIn(result['state'], ('candidate_changed', 'capture_changed_before_invoke'))
                group.GetPattern.return_value.Invoke.assert_not_called()

    def test_invoke_failure_is_not_retried(self):
        import uiautomation as auto
        group = mock.create_autospec(auto.GroupControl, instance=True, spec_set=True)
        group.GetPattern.return_value.Invoke.side_effect = OSError('simulated')
        candidate = probe.Candidate(group, 123, 456, (0, 0, 3), (196, 248, 236, 288))
        with mock.patch.object(auto, 'UIAutomationInitializerInThread'), \
                mock.patch.object(probe, '_scan_windows', return_value=({'state': 'ready'}, candidate)), \
                mock.patch.object(probe, '_capture_active', return_value=True):
            result = probe.invoke_once(Path('test.exe'), candidate.record())
        self.assertEqual(result['state'], 'uia_invoke_OSError')
        group.GetPattern.return_value.Invoke.assert_called_once()

    def test_opaque_ui_reports_reason_not_absence(self):
        auto = SimpleNamespace(ControlFromHandle=lambda hwnd: _Control(kind='WindowControl'),
                               PatternId=SimpleNamespace(InvokePattern=10000))
        with mock.patch.object(probe, 'matching_sogou_pids', return_value=(123,)), \
                mock.patch.object(probe, 'visible_windows', return_value=((456, 123),)), \
                mock.patch.object(probe, '_capture_active', return_value=True):
            result, candidate = probe._scan_windows(Path('test.exe'), auto)
        self.assertIsNone(candidate)
        self.assertIsNone(result['action_pair_present'])
        self.assertEqual(result['windows'][0]['reason'], 'semantic_root_unavailable')
        self.assertTrue(result['windows'][0]['capture_active'])

    def test_child_read_exception_is_not_empty_tree(self):
        control = mock.Mock()
        control.GetChildren.side_effect = OSError('simulated')
        with self.assertRaises(OSError):
            probe._children(control)

    def test_failed_name_read_is_not_an_unnamed_finish_button(self):
        root, _ = self._popup()
        bar = root.children[0].children[0]
        right = mock.Mock(wraps=bar.children[3])
        right.ControlTypeName = 'GroupControl'
        right.IsEnabled, right.IsOffscreen = True, False
        type(right).Name = mock.PropertyMock(side_effect=OSError('unavailable'))
        right.AutomationId = ''
        bar.children = bar.children[:3] + (right,)
        control, reason = probe.find_normal_submit(root, object())
        self.assertIsNone(control)
        self.assertEqual(reason, 'action_pair_not_unique')

    def test_structure_drift_is_a_safe_noop(self):
        root, _ = self._popup()
        semantic = root.GetChildren()[0]
        bar = semantic.GetChildren()[0]
        bad = _Control(rect=(100, 230, 260, 300), children=bar.GetChildren()[:3])
        semantic.children = (bad,)
        control, reason = probe.find_normal_submit(root, object())
        self.assertIsNone(control)
        self.assertEqual(reason, "action_pair_shape")

    def test_left_or_right_label_or_id_is_not_trusted(self):
        root, _ = self._popup()
        semantic = root.GetChildren()[0]
        bar = semantic.GetChildren()[0]
        bar.GetChildren()[3].Name = "完成"
        control, reason = probe.find_normal_submit(root, object())
        self.assertIsNone(control)
        self.assertEqual(reason, "action_pair_not_unique")

    def test_hash_mismatch_fails_closed(self):
        executable = Path("C:/test/sogou_voice_assistant.exe")
        with mock.patch("ovb_rc003.voice_program_manager.discover_sogou_voice_executable", return_value=executable):
            with mock.patch.object(probe, "sha256", return_value="not-the-known-package"):
                value, reason = probe.installed_sogou()
        self.assertIsNone(value)
        self.assertEqual(reason, "sogou_package_changed")

    def test_bound_capture_rechecked_immediately_before_invoke(self):
        import uiautomation as auto
        identity = ('endpoint', 'instance', 123)
        for active in (False, True):
            with self.subTest(active=active):
                group = mock.create_autospec(auto.GroupControl, instance=True, spec_set=True)
                group.GetPattern.return_value.Invoke.return_value = True
                candidate = probe.Candidate(group, 123, 456, (0, 0, 3), (196, 248, 236, 288))
                with mock.patch.object(auto, 'UIAutomationInitializerInThread'), \
                        mock.patch.object(probe, '_scan_windows', return_value=({'state': 'ready'}, candidate)), \
                        mock.patch.object(probe, '_capture_active', return_value=True), \
                        mock.patch.object(probe, '_same_capture_active', return_value=active) as capture:
                    result = probe.invoke_once(Path('test.exe'), candidate.record(), capture_identity=identity)
                self.assertEqual(result['state'], 'invoked_once' if active else 'capture_identity_changed')
                self.assertEqual(group.GetPattern.return_value.Invoke.call_count, int(active))
                capture.assert_called_once_with(identity)

    def test_product_rejects_unknown_package_capture_ui_or_wrong_process(self):
        identity = ('endpoint', 'instance', 123)
        for case in ('package', 'capture', 'ui', 'pid', 'ok'):
            with self.subTest(case=case):
                candidate = probe.Candidate(None, 999 if case == 'pid' else 123, 456, (0, 0, 3), (1, 2, 3, 4))
                with mock.patch.object(probe, 'installed_sogou', return_value=(None, 'sogou_package_changed')
                                       if case == 'package' else (Path('test.exe'), '')), \
                        mock.patch.object(probe, '_same_capture_active', return_value=case != 'capture'), \
                        mock.patch.object(probe, '_inspect_with_executable', return_value=(
                            {'state': 'submit_not_identified'}, None if case == 'ui' else candidate)), \
                        mock.patch.object(probe, 'invoke_once', return_value={'state': 'invoked_once'}) as invoke:
                    result = probe.finish_bound_capture(identity)
                self.assertEqual(invoke.call_count, int(case == 'ok'))
                if case == 'ok':
                    invoke.assert_called_once_with(Path('test.exe'), candidate.record(), capture_identity=identity)
                    self.assertEqual(result, {'state': 'invoked_once'})

    def test_capture_session_must_be_exact_unique_and_active(self):
        from ovb_rc003 import voice_playback_session_windows as audio
        wanted = audio.CaptureSession('endpoint', 'instance', 123, 1)
        for rows, expected in (((wanted,), True), ((), False),
                ((audio.CaptureSession('endpoint', 'replacement', 123, 1),), False),
                ((audio.CaptureSession('endpoint', 'instance', 123, 0),), False),
                ((wanted, audio.CaptureSession('other', 'other', 123, 1)), False)):
            with mock.patch.object(audio, 'read_capture_sessions', return_value=rows):
                self.assertEqual(probe._same_capture_active(wanted.identity), expected)
        with mock.patch.object(audio, 'read_capture_sessions', side_effect=OSError):
            self.assertFalse(probe._same_capture_active(wanted.identity))

    def test_child_request_and_result_are_metadata_only(self):
        identity = ('endpoint', 'instance', 123)
        with tempfile.TemporaryDirectory() as directory:
            request, result = Path(directory) / 'request.json', Path(directory) / 'result.json'
            request.write_text(json.dumps(identity), encoding='utf-8')
            with mock.patch.object(probe, 'finish_bound_capture', return_value={'state': 'invoked_once'}) as finish:
                self.assertEqual(probe.child_main([str(request), str(result)]), 0)
                finish.assert_called_once_with(identity)
                self.assertEqual(probe._read_result(result, 0), 'invoked_once')
                self.assertEqual(probe.child_main([str(request), str(result)]), 2)
                finish.assert_called_once()  # Never overwrite/retry an existing result.

    def test_bounded_child_command_supports_source_and_frozen_and_cleans_files(self):
        from ovb_rc003 import windows_diagnostics as diagnostics
        identity = ('endpoint', 'instance', 123)
        for frozen in (False, True):
            for fail in (False, True):
                with self.subTest(frozen=frozen, fail=fail):
                    paths = []
                    def run(command, **kwargs):
                        expected = [sys.executable] + ([] if frozen else ['-m', 'ovb_rc003'])
                        self.assertEqual(command[:-3], expected)
                        self.assertEqual(command[-3], probe.FLAG)
                        paths.extend(map(Path, command[-2:]))
                        self.assertEqual(json.loads(paths[0].read_text()), list(identity))
                        self.assertEqual(kwargs['timeout'], 2.5)
                        if fail:
                            raise diagnostics.BleDiscoveryCancelledError('timeout')
                        paths[1].write_text('{"state":"invoked_once"}', encoding='utf-8')
                        return kwargs['result_reader'](paths[1], 0)
                    with mock.patch.object(sys, 'frozen', frozen, create=True), \
                            mock.patch.object(diagnostics, '_run_ble_diagnostics_subprocess', side_effect=run) as runner:
                        result = probe.finish_with_timeout(identity, cancel_event=threading.Event())
                    self.assertEqual(result, 'helper_cancelled_or_timed_out' if fail else 'invoked_once')
                    runner.assert_called_once()
                    self.assertTrue(all(not path.exists() for path in paths))

    def test_missing_bound_identity_does_not_start_helper(self):
        from ovb_rc003 import windows_diagnostics as diagnostics
        with mock.patch.object(diagnostics, '_run_ble_diagnostics_subprocess') as runner:
            for identity in (None, (), ('endpoint', 'instance', True), ('', 'instance', 123)):
                self.assertEqual(probe.finish_with_timeout(identity, cancel_event=threading.Event()),
                                 'capture_identity_unavailable')
            runner.assert_not_called()

    def test_hidden_entry_missing_arguments_exits_without_desktop(self):
        result = subprocess.run([sys.executable, '-m', 'ovb_rc003', probe.FLAG],
                                capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 2, result.stderr)
