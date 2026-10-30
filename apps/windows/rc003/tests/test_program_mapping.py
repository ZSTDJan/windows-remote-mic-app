"""User-selected EXE mappings, independent payloads and bounded launch requests."""
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import action_executor as executor, config, key_mapping as km, mapping_presets, settings_ui
from tests import test_app_wiring, test_qt_settings_app

PROGRAM = r'C:\Program Files\测试 & 工具\程序.exe'


class ProgramMappingTests(unittest.TestCase):
    def test_literal_paths_round_trip_without_repeat_or_payload_conflicts(self):
        for path in (PROGRAM, r'\\server\共享\tool.EXE', r'C:/工具/app.exe', '  "' + PROGRAM + '"  '):
            with self.subTest(path=path):
                normalized = km.normalize_program_path(path)
                action = km.ButtonAction(km.ActionKind.OPEN_PROGRAM, program_path=normalized)
                self.assertEqual(km.ButtonAction.from_dict(action.to_dict()), action)
                self.assertNotIn(normalized, repr(action))
                self.assertFalse(km.action_allows_repeat(action))
                self.assertEqual(settings_ui._action_to_display(action), '打开程序')
                for trigger in km.ButtonTrigger:
                    parsed = settings_ui._validated_button_action('ok', trigger.value, '打开程序', program_path=path)
                    self.assertEqual(parsed, action)

    def test_reject_relative_paths_commands_urls_and_other_file_types(self):
        for path in ('', None, 42, 'tool.exe', r'C:tool.exe', r'\tool.exe', 'Ctrl+C',
                     r'C:\app.exe --flag', '"C:\\app.exe" "x"', r'C:\a.exe:other.exe',
                     'file:///C:/app.exe', 'quicker:runaction:x', r'C:\app.lnk',
                     r'C:\app.bat', r'C:\app.ps1', 'C:\\x\0.exe', 'C:\\x\n.exe',
                     'C:\\x\ud800.exe', r'\\?\C:\tool.exe', r'\\.\pipe\tool.exe',
                     '//./pipe/tool.exe'):
            with self.subTest(path=repr(path)), self.assertRaises(ValueError):
                km.ButtonAction.from_dict({'kind': 'open_program', 'program_path': path})
        for payload in ({'keys': ['enter']}, {'uri': 'quicker:runaction:x'}, {'text': 'literal'}):
            with self.assertRaises(ValueError):
                km.ButtonAction.from_dict(dict(kind='open_program', program_path=PROGRAM, **payload))
        with self.assertRaises(ValueError):
            km.ButtonAction.from_dict({'kind': 'text', 'text': 'hello', 'program_path': PROGRAM})

    def test_save_and_export_import_all_gestures_for_both_profiles(self):
        for profile in ('xiaomi-rc003', 'chromecast-remote'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / 'config'
                root.mkdir()
                original = config.default_key_bindings(profile)
                display = {b: settings_ui._action_to_display(km.ButtonAction.from_dict(a)) for b, a in original['bindings'].items()}
                display['ok'] = '打开程序'
                paths = {'ok': {t.value: PROGRAM.replace('程序.exe', t.value + '.exe') for t in km.ButtonTrigger}}
                settings, bindings = settings_ui.build_save_model(
                    button_display_map=display,
                    secondary_display_map={'ok': {'double_click': '打开程序', 'long_press': '打开程序'}},
                    program_path_map=paths, hotkey_text='ralt', trigger_mode=km.VoiceTriggerMode.HOLD,
                    endpoint_display_text='', base_config=config.default_config(), base_bindings=original,
                    selected_device_profile=profile, validate_voice_hotkeys=False)
                config.save_settings_pair(config.config_path(root), settings, config.key_bindings_path(root), bindings)
                loaded = config.load_key_bindings(config.key_bindings_path(root))
                for trigger in km.ButtonTrigger:
                    self.assertEqual(km.button_action_for(loaded, 'ok', trigger).program_path, paths['ok'][trigger.value])
                store = mapping_presets.PresetStore(root)
                snap = mapping_presets.snapshot(loaded, profile)
                identity = store.create('程序', profile, snap)
                exported = Path(folder) / 'export.json'
                store.export_file(identity, exported)
                imported = store.import_file(exported, profile)
                self.assertEqual(store.get(imported)['mapping'], snap)
                applied = mapping_presets.apply_to(original, store.get(imported), profile)
                self.assertEqual(applied['bindings']['ok']['program_path'], paths['ok']['single_click'])

    def test_google_mic_still_rejects_program_action(self):
        bindings = config.default_key_bindings('chromecast-remote')
        bindings['bindings']['mic'] = km.ButtonAction(km.ActionKind.OPEN_PROGRAM, program_path=PROGRAM).to_dict()
        with self.assertRaisesRegex(ValueError, '谷歌话筒键'):
            mapping_presets.snapshot(bindings, 'chromecast-remote')


class ProgramLaunchTests(unittest.TestCase):
    def setUp(self):
        self.action = km.ButtonAction(km.ActionKind.OPEN_PROGRAM, program_path=PROGRAM)
        self.log = mock.Mock(spec=logging.Logger)

    def wait_finished(self):
        self.assertTrue(executor._program_launch_lock.acquire(timeout=3), 'launcher worker did not finish')
        executor._program_launch_lock.release()

    def test_one_background_request_no_queue_while_windows_is_busy(self):
        entered, release = threading.Event(), threading.Event()
        def launch(path):
            self.assertEqual(path, PROGRAM)
            entered.set()
            self.assertTrue(release.wait(3))
        with mock.patch.object(executor, '_shell_open_program', side_effect=launch) as native:
            try:
                self.assertTrue(executor.request_program_launch(self.action, logger=self.log))
                self.assertTrue(entered.wait(3))
                self.assertFalse(executor.request_program_launch(self.action, logger=self.log))
                native.assert_called_once_with(PROGRAM)
            finally:
                release.set()
                self.wait_finished()
            self.assertTrue(executor.request_program_launch(self.action, logger=self.log))
            self.wait_finished()
            self.assertEqual(native.call_count, 2)

    def test_failed_launch_reports_once_without_retry_or_path_in_log(self):
        with mock.patch.object(executor, '_shell_open_program', side_effect=FileNotFoundError(PROGRAM)) as native, \
                mock.patch.object(executor, '_show_program_launch_error') as report:
            self.assertTrue(executor.request_program_launch(self.action, logger=self.log))
            self.wait_finished()
            native.assert_called_once()
            report.assert_called_once()
            self.assertNotIn(PROGRAM, str(self.log.mock_calls))

    def test_cancelled_authorization_does_not_show_another_dialog(self):
        error = OSError('cancelled')
        error.winerror = 1223
        with mock.patch.object(executor, '_shell_open_program', side_effect=error), \
                mock.patch.object(executor, '_show_program_launch_error') as report:
            self.assertTrue(executor.request_program_launch(self.action, logger=self.log))
            self.wait_finished()
            report.assert_not_called()

    def test_thread_creation_failure_releases_guard(self):
        with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                executor.request_program_launch(self.action, logger=self.log)
        self.wait_finished()

    @unittest.skipUnless(__import__('sys').platform == 'win32', 'Windows path checks')
    def test_missing_file_and_directory_are_not_sent_to_the_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'missing.exe'
            directory = Path(folder) / 'folder.exe'
            directory.mkdir()
            for path in (missing, directory):
                with self.subTest(path=path), mock.patch.object(executor.ctypes, 'WinDLL') as dll:
                    with self.assertRaises(FileNotFoundError):
                        executor._shell_open_program(str(path))
                    dll.assert_not_called()


class ProgramActionWiringTests(test_app_wiring._AppWiringTestCase):
    def test_each_gesture_dispatches_once_and_does_not_repeat(self):
        action = km.ButtonAction(km.ActionKind.OPEN_PROGRAM, program_path=PROGRAM)
        self.app._bindings['bindings']['ok'] = action.to_dict()
        self.app._bindings.setdefault('secondary_bindings', {})['ok'] = {
            t.value: action.to_dict() for t in (km.ButtonTrigger.DOUBLE_CLICK, km.ButtonTrigger.LONG_PRESS)}
        with mock.patch.object(executor, 'request_program_launch', return_value=True) as launch:
            for trigger in km.ButtonTrigger:
                self.app._on_button_trigger('ok', trigger)
            self.assertEqual(launch.call_count, 3)
        self.assertFalse(self.app._is_button_repeatable('ok'))

    def test_stopped_input_does_not_launch(self):
        self.app._accept_input_events = False
        with mock.patch.object(executor, 'request_program_launch') as launch:
            self.app._apply_button_action(km.ButtonAction(km.ActionKind.OPEN_PROGRAM, program_path=PROGRAM))
        launch.assert_not_called()


@unittest.skipUnless(test_qt_settings_app._HAS_PYSIDE6, test_qt_settings_app._SKIP_REASON)
class ProgramControllerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_qt_settings_app.SettingsControllerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.controller, self.model = self.fixture._make_controller()
        self.row = self.model.index_of('ok')

    def test_path_only_edit_failure_retry_and_switch_to_shortcut(self):
        c, m, row = self.controller, self.model, self.row
        with mock.patch.object(executor, 'request_program_launch') as launch:
            m.setProgramPathAt(row, 'single_click', PROGRAM)
            m.setActionTextAt(row, '打开程序')
            self.assertTrue(c._save_mapping())
            c._load_bindings_into_model()
            self.assertEqual(m.programPathAt(row, 'single_click'), PROGRAM)
            changed = PROGRAM.replace('程序.exe', '另一个.exe')
            m.setProgramPathAt(row, 'single_click', changed)
            with mock.patch.object(config, 'save_settings_pair', side_effect=OSError('test')):
                self.assertFalse(c._save_mapping())
            self.assertEqual(m.programPathAt(row, 'single_click'), changed)
            self.assertEqual(c._bindings['bindings']['ok']['program_path'], PROGRAM)
            self.assertTrue(c._save_mapping())
            self.assertEqual(c._bindings['bindings']['ok']['program_path'], changed)
            m.setActionTextAt(row, 'Ctrl+C')
            self.assertTrue(c._save_mapping())
            self.assertNotIn('program_path', c._bindings['bindings']['ok'])
            self.assertEqual(m.programPathAt(row, 'single_click'), '')
            launch.assert_not_called()

    def test_option_available_without_install_and_file_url_decoding(self):
        c = self.controller
        with mock.patch.object(executor, 'resolve_application_command', return_value=None):
            for options in (c.primaryActionOptionsFor, c.secondaryActionOptionsFor, c.longPressActionOptionsFor):
                self.assertIn('打开程序', options('ok'))
        self.assertIn('填写', c.buttonActionValidationMessage('ok', 'single_click', '打开程序', '', ''))
        self.assertEqual(c.buttonActionValidationMessage('ok', 'single_click', '打开程序', '', PROGRAM), '')
        from PySide6.QtCore import QUrl
        self.assertEqual(c.programPathFromFileUrl(QUrl.fromLocalFile(PROGRAM).toString()).replace('/', '\\'), PROGRAM)
        self.assertEqual(c.programPathFromFileUrl('https://example.com/app.exe'), '')
