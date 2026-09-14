import ctypes
from ctypes import wintypes
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
from pathlib import Path

from ovb_rc003 import diagnostic_trace as trace
from ovb_rc003 import input_environment_diagnostics_windows as environment
from ovb_rc003 import voice_response_diagnostics_windows as response
from ovb_rc003 import voice_interaction_diagnostics_windows as focus
from ovb_rc003 import voice_key_physicalizer_windows as hook


class WindowEvidenceTests(unittest.TestCase):
    def api(self, *, truncated=False, reused=False):
        user, dwm = mock.Mock(), mock.Mock()
        def enumerate_windows(callback, arg):
            for hwnd in range(10, 50 if truncated else 13):
                if not callback(hwnd, arg):
                    return 0
            return 1
        user.EnumWindows.side_effect = enumerate_windows
        counts = {}
        def pid(hwnd, output):
            counts[hwnd] = counts.get(hwnd, 0) + 1
            output._obj.value = 99 if hwnd == 10 else 42
            if reused and counts[hwnd] > 1:
                output._obj.value = 900
            return 1
        user.GetWindowThreadProcessId.side_effect = pid
        def name(hwnd, output, size):
            self.assertNotEqual(hwnd, 10)  # Other processes: no class/text queries.
            output.value = 'QtVoiceWindow'
            return len(output.value)
        user.GetClassNameW.side_effect = name
        user.GetWindowRect.return_value = 1
        user.IsWindowVisible.return_value = 1
        user.IsIconic.return_value = 0
        dwm.DwmGetWindowAttribute.return_value = 0
        return user, dwm

    def test_only_selected_pids_and_no_bubble_claim_or_text_query(self):
        user, dwm = self.api()
        with mock.patch.object(response.ctypes, 'WinDLL', side_effect=lambda n, **_: user if n=='user32' else dwm), \
             mock.patch.object(response.time, 'monotonic', return_value=1):
            result = response.window_activity({42: 'doubao_ime'})
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['windows']), 2)
        self.assertEqual(result['bubble_confirmation'], 'unknown')
        self.assertEqual({r['pid'] for r in result['windows']}, {42})
        user.GetWindowTextW.assert_not_called()
        user.SendMessageW.assert_not_called()
        user.SetForegroundWindow.assert_not_called()

    def test_window_limit_and_reused_handle_are_not_absence(self):
        for truncated, reused in ((True, False), (False, True)):
            user, dwm = self.api(truncated=truncated, reused=reused)
            with mock.patch.object(response.ctypes, 'WinDLL', side_effect=lambda n, **_: user if n=='user32' else dwm), \
                 mock.patch.object(response.time, 'monotonic', return_value=1):
                result = response.window_activity({42: 'doubao_ime'})
            self.assertFalse(result['complete'])
            self.assertLessEqual(len(result['windows']), 24)
            self.assertNotEqual(result['status'], 'captured')

    def test_errors_and_unsupported_stay_unknown(self):
        with mock.patch.object(response.ctypes, 'WinDLL', side_effect=OSError('private')):
            result = response.window_activity({42: 'wetype'})
        self.assertEqual(result['status'], 'query_failed')
        self.assertNotIn('private', json.dumps(result))
        with mock.patch.object(response.sys, 'platform', 'linux'):
            self.assertEqual(response.window_activity({42: 'wetype'})['status'], 'unsupported_platform')

    def test_hidden_windows_do_not_exclude_later_visible_voice_window(self):
        user, dwm = self.api(truncated=True)
        user.IsWindowVisible.side_effect = lambda hwnd: hwnd == 49
        with mock.patch.object(response.ctypes, 'WinDLL', side_effect=lambda n, **_: user if n=='user32' else dwm), \
             mock.patch.object(response.time, 'monotonic', return_value=1):
            result = response.window_activity({42: 'doubao_ime'})
        self.assertTrue(result['complete'])
        self.assertEqual([r['hwnd'] for r in result['windows']], [49])
        self.assertEqual(result['hidden_classes'][0]['count'], 38)

    @unittest.skipUnless(sys.platform == 'win32', 'read-only own-process window enumeration')
    def test_native_window_query(self):
        result = response.window_activity({os.getpid(): 'test'})
        self.assertIn(result['status'], {'captured', 'partial'})
        self.assertEqual(result['bubble_confirmation'], 'unknown')


class MicrophoneHistoryTests(unittest.TestCase):
    def test_name_filter_latest_history_and_no_live_confirmation(self):
        registry = mock.Mock(HKEY_CURRENT_USER=1, KEY_READ=0x20019)
        names = [r'C:#Private#ImeService.exe', r'C:#Old#ImeService.exe', r'C:#Private#Other.exe']
        def enum(root, index):
            if index < len(names):
                return names[index]
            error = OSError('end'); error.winerror = 259
            raise error
        registry.EnumKey.side_effect = enum
        opened = []
        def open_key(root, name, *args):
            opened.append(name)
            context = mock.MagicMock()
            context.__enter__.return_value = name
            return context
        registry.OpenKey.side_effect = open_key
        registry.QueryValueEx.side_effect = lambda key, field: (
            (200 if 'Private' in key else 100) if field=='LastUsedTimeStart' else 0, 11)
        with mock.patch.dict(sys.modules, {'winreg': registry}):
            result = response.microphone_history({'imeservice.exe': 'doubao_ime'})
        self.assertEqual(result['status'], 'captured')
        self.assertEqual(result['entries'][0]['last_start'], 200)
        self.assertEqual(result['live_capture_confirmation'], 'unknown')
        self.assertEqual(result['process_binding'], 'executable_name_only')
        self.assertNotIn(names[-1], opened)
        self.assertNotIn('Private', json.dumps(result))
        registry.SetValueEx.assert_not_called()

    def test_denied_registry_is_not_microphone_closed(self):
        registry = mock.Mock(HKEY_CURRENT_USER=1, KEY_READ=1)
        registry.OpenKey.side_effect = PermissionError('private')
        with mock.patch.dict(sys.modules, {'winreg': registry}):
            result = response.microphone_history({'imeservice.exe': 'doubao_ime'})
        self.assertEqual(result['status'], 'query_failed')
        self.assertEqual(result['live_capture_confirmation'], 'unknown')


class ResponseCacheTests(unittest.TestCase):
    def test_only_verified_processes_bounded_cadence_and_pid_restart(self):
        now = [1.0]
        windows, microphone = mock.Mock(return_value={}), mock.Mock(return_value={})
        sampler = response.ResponseSampler(windows=windows, microphone=microphone, clock=lambda: now[0])
        rows = [dict(role='doubao_ime', pid=42, status='captured', executable='ImeService.exe', created_filetime=1),
                dict(role='wetype', pid=55, status='identity_unverified', executable='Other.exe')]
        sampler.sample(rows); sampler.sample(rows)
        windows.assert_called_once_with({42: 'doubao_ime'})
        microphone.assert_called_once_with({'imeservice.exe': 'doubao_ime'})
        now[0] = 1.5; sampler.sample(rows)
        self.assertEqual(windows.call_count, 2)
        rows[0]['created_filetime'] = 2; sampler.sample(rows)
        self.assertEqual(windows.call_count, 3)

    def test_failures_do_not_break_environment(self):
        sampler = response.ResponseSampler(windows=mock.Mock(side_effect=OSError('secret')),
            microphone=mock.Mock(side_effect=OSError('secret')))
        result = sampler.sample([])
        self.assertEqual(result['voice_windows']['status'], 'query_failed')
        self.assertNotIn('secret', json.dumps(result))


class ClipboardEvidenceTests(unittest.TestCase):
    def test_owner_query_failure_keeps_other_environment_evidence(self):
        sampler = environment.EnvironmentSampler(processes=lambda: [], security=lambda _: {},
            clipboard=lambda: dict(sequence=1, owner_pid=42), response=mock.Mock(sample=lambda _: {}))
        with mock.patch.object(focus, '_process_basename', side_effect=OSError('private')):
            result = sampler.sample({})
        self.assertEqual(result['clipboard']['owner_query_status'], 'query_failed')
        self.assertEqual(result['clipboard']['sequence'], 1)
        self.assertNotIn('private', json.dumps(result))

    def test_owner_change_during_query_discards_attribution(self):
        user = mock.Mock()
        user.GetClipboardSequenceNumber.side_effect = [10, 11]
        user.GetClipboardOwner.return_value = 55
        def pid(hwnd, out):
            out._obj.value = 42
            return 1
        user.GetWindowThreadProcessId.side_effect = pid
        user.IsClipboardFormatAvailable.return_value = 1
        with mock.patch.object(environment.ctypes, 'WinDLL', return_value=user):
            result = environment.clipboard_metadata()
        self.assertEqual(result['status'], 'changed_during_sample')
        self.assertIsNone(result['owner_pid'])
        self.assertIsNone(result['unicode_text_available'])
        user.OpenClipboard.assert_not_called()
        user.GetClipboardData.assert_not_called()


class WorkflowEvidenceTests(unittest.TestCase):
    def test_actual_sound_test_workflow_logs_both_test_and_restore(self):
        from ovb_rc003 import qt_settings_app
        from tests.test_qt_settings_app import DiagnosticsControllerTests
        fixture = DiagnosticsControllerTests('test_vb_cable_channel_test_temporarily_stops_and_restores_bridge')
        fixture.setUp()
        original = fixture._make_settings_controller
        def controller():
            result = original()
            result._diagnostic_trace_enabled = True
            return result
        fixture._make_settings_controller = controller
        try:
            with mock.patch.object(qt_settings_app.logging_setup, 'get_logger') as logger:
                fixture.test_vb_cable_channel_test_temporarily_stops_and_restores_bridge()
                calls = [c.args for c in logger.return_value.info.call_args_list
                         if c.args and str(c.args[0]).startswith('sound channel diagnostic:')]
            self.assertEqual(len(calls), 2)
            self.assertIn('phase=start', calls[0][0])
            self.assertIn('phase=finished', calls[1][0])
            self.assertEqual(calls[1][1:4], (True, False, 'pass'))
        finally:
            fixture.tearDown()

    def test_sound_test_log_is_opt_in_and_failure_is_isolated(self):
        from ovb_rc003 import qt_settings_app
        method = qt_settings_app._load_qt_classes()['DiagnosticsController']._log_sound_channel_diagnostic
        fake = types.SimpleNamespace(_settings_controller=types.SimpleNamespace(_diagnostic_trace_enabled=False),
                                     _config_root=Path('unused'))
        with mock.patch.object(qt_settings_app.logging_setup, 'get_logger') as logger:
            method(fake, 'phase=start')
            logger.assert_not_called()
            fake._settings_controller._diagnostic_trace_enabled = True
            method(fake, 'phase=finished status=%s', 'pass')
            logger.return_value.info.assert_called_once_with('sound channel diagnostic: phase=finished status=%s', 'pass')
            logger.side_effect = OSError('log unavailable')
            method(fake, 'phase=finished')

    def test_runtime_failure_is_recorded_without_becoming_an_input_failure(self):
        from ovb_rc003.app import RC003App
        app = RC003App.__new__(RC003App)
        app._runtime_status_lock = threading.Lock()
        app._runtime_voice_state, app._runtime_voice_provider = 'active', 'wetype'
        app._publish_runtime_status = mock.Mock()
        app._diagnostic_trace = mock.Mock()
        app._diagnostic_trace.current_context.return_value = {'attempt_id': 'a'}
        app._set_runtime_voice_result('host_start_failed', provider='wetype')
        self.assertEqual(app._runtime_voice_state, 'host_start_failed')
        self.assertEqual(app._diagnostic_trace.emit.call_args.kwargs['attempt_id'], 'a')
        app._diagnostic_trace.emit.side_effect = OSError('log failed')
        app._set_runtime_voice_result('success', provider='wetype')
        self.assertEqual(app._runtime_voice_state, 'success')


class SubmissionKeysTests(unittest.TestCase):
    def test_plain_text_ignored_and_paste_edges_correlated_after_release(self):
        now = [1.0]; keys = trace._SubmissionKeys(clock=lambda: now[0])
        self.assertIsNone(keys.edge(0x56, False, 0))
        keys.start('attempt', 'gesture', 'wetype')
        self.assertIsNone(keys.edge(0x56, False, 0))
        keys.finish('attempt')
        keys.edge(0xA2, False, 0)
        down = keys.edge(0x56, False, 0x10)
        self.assertEqual(down['command'], 'ctrl_v')
        self.assertEqual(down['attempt_id'], 'attempt')
        self.assertTrue(down['injected'])
        keys.edge(0xA2, True, 0)
        self.assertEqual(keys.edge(0x56, True, 0)['edge'], 'up')
        self.assertEqual(down['target_response'], 'unknown')
        self.assertNotIn('text', down)
        now[0] = 12; keys.edge(0xA2, False, 0)
        self.assertIsNone(keys.edge(0x56, False, 0))

    def test_limit_session_replacement_and_long_voice_tail(self):
        now = [1.0]; keys = trace._SubmissionKeys(clock=lambda: now[0])
        keys.start('a', '', 'wetype'); keys.edge(0xA0, False, 0)
        for i in range(64):
            self.assertIsNotNone(keys.edge(0x2D, bool(i%2), 0))
        self.assertIsNone(keys.edge(0x2D, False, 0))
        keys.start('b', '', 'doubao_ime'); keys.finish('a')
        now[0] = 32
        self.assertIsNone(keys.edge(0x2D, False, 0))
        keys.finish('b')
        self.assertEqual(keys.edge(0x2D, False, 0)['attempt_id'], 'b')

    def test_real_writer_contains_response_and_paste_without_hook_or_content(self):
        captured = threading.Event()
        def sample(_):
            captured.set()
            return dict(voice_windows={'bubble_confirmation': 'unknown'},
                        voice_microphone_history={'live_capture_confirmation': 'unknown'})
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(focus, 'capture_focus_snapshot', return_value=focus.FocusSnapshot(False)), \
             mock.patch.object(environment.EnvironmentSampler, 'sample', side_effect=sample):
            writer = trace.DiagnosticTrace(Path(tmp), enabled=True)
            attempt = writer.begin_attempt('g', 'wetype')
            self.assertTrue(captured.wait(2))
            writer.end_attempt(attempt, 'unknown')
            writer.observe_submission_key(0xA2, False, 0)
            writer.observe_submission_key(0x56, False, 0x10)
            writer.close()
            writer.observe_submission_key(0x56, True, 0)
            rows = [json.loads(line) for line in writer.path.read_text().splitlines()]
        paste = [r for r in rows if r['event']=='submission_key_observation']
        self.assertEqual(len(paste), 1)
        self.assertEqual(paste[0]['attempt_id'], attempt)
        self.assertTrue(any('voice_windows' in r for r in rows))

    def test_hook_observer_failure_does_not_swallow_or_change_keys(self):
        user = mock.Mock(); user.CallNextHookEx.return_value = 77
        event = hook.KBDLLHOOKSTRUCT(vkCode=0x56, flags=0x10, dwExtraInfo=0)
        physicalizer = hook.VoiceKeyPhysicalizer()
        sink = mock.Mock(); sink.observe_submission_key.side_effect = OSError('log failure')
        with mock.patch.object(hook.ctypes, 'windll', types.SimpleNamespace(user32=user)), \
             mock.patch.object(hook, '_diagnostic_trace', sink):
            result = physicalizer._hookproc(0, hook.WM_KEYDOWN, ctypes.addressof(event))
        self.assertEqual(result, 77)
        self.assertEqual(event.flags, 0x10)
        user.CallNextHookEx.assert_called_once()


if __name__ == '__main__':
    unittest.main()
