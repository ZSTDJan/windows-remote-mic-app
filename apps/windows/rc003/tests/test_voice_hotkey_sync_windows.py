import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ovb_rc003 import voice_hotkey_sync_windows


class VoiceHotkeyDefaultsTests(unittest.TestCase):
    def test_defaults_match_each_supported_provider_shortcut(self):
        self.assertEqual(voice_hotkey_sync_windows.default_hotkey("sogou"), "rctrl")
        self.assertEqual(
            voice_hotkey_sync_windows.default_hotkey("wetype"), "lctrl+lwin"
        )
        self.assertEqual(
            voice_hotkey_sync_windows.default_hotkey("doubao_ime"), "ralt"
        )
        self.assertEqual(voice_hotkey_sync_windows.default_hotkey("custom"), "ralt")

    def test_removed_windows_dictation_provider_is_not_read(self):
        result = voice_hotkey_sync_windows.read_provider_hotkey(
            "windows_dictation", platform="win32"
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "unsupported_provider")


class SogouVoiceHotkeyTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.appdata = Path(self._tmpdir.name)
        self.path = (
            self.appdata / "sogou_voice_assistant_pc" / "config.json"
        )
        self.path.parent.mkdir(parents=True)
        self.document = {
            "setting": {
                "shortcutKeysPress": ["LeftCtrl", "LeftShift", "F7"],
                "shortcutKeysFree": [],
                "longPressEnabled": True,
                "freespeakEnabled": False,
            },
            "unrelated": {"keep": True},
        }
        self.path.write_text(
            json.dumps(self.document, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_reads_sogou_current_hold_shortcut(self):
        result = voice_hotkey_sync_windows.read_provider_hotkey(
            "sogou", platform="win32", appdata=self.appdata
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.hotkey, "lctrl+lshift+f7")

    def test_accepts_sogou_single_right_ctrl(self):
        result = voice_hotkey_sync_windows.validate_provider_hotkey(
            "sogou", "rctrl"
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.hotkey, "rctrl")

    def test_rejects_sogou_single_letter(self):
        result = voice_hotkey_sync_windows.validate_provider_hotkey("sogou", "a")

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "unsupported_single_key")
        self.assertIn("单键", result.message)

    def test_rejects_sogou_shortcut_longer_than_three_keys(self):
        result = voice_hotkey_sync_windows.validate_provider_hotkey(
            "sogou", "lctrl+lshift+lalt+f9"
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "too_many_keys")

    def test_rejects_key_not_supported_by_sogou(self):
        before = self.path.read_bytes()
        with mock.patch.object(
            voice_hotkey_sync_windows,
            "_sogou_voice_process_running",
        ) as process_check:
            result = voice_hotkey_sync_windows.sync_provider_hotkey(
                "sogou",
                "lctrl+volume_up",
                platform="win32",
                appdata=self.appdata,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "unsupported_key")
        self.assertEqual(self.path.read_bytes(), before)
        process_check.assert_not_called()

    def test_converts_sogou_win_direction_and_punctuation_names(self):
        self.assertEqual(
            voice_hotkey_sync_windows._hotkey_to_provider_tokens(
                "lctrl+lwin+up"
            ),
            ["LeftCtrl", "LeftMeta", "Up"],
        )
        self.assertEqual(
            voice_hotkey_sync_windows._provider_tokens_to_hotkey(
                ["RightMeta", "BracketLeft"]
            ),
            "rwin+left_bracket",
        )

    def test_reads_sogou_generic_modifier_names_from_existing_config(self):
        self.assertEqual(
            voice_hotkey_sync_windows._provider_tokens_to_hotkey(
                ["Ctrl", "Meta", "F8"]
            ),
            "ctrl+win+f8",
        )

    def test_reports_unrepresentable_sogou_numpad_enter(self):
        with self.assertRaisesRegex(ValueError, "不能区分数字键盘 Enter"):
            voice_hotkey_sync_windows._provider_tokens_to_hotkey(
                ["LeftCtrl", "NumpadEnter"]
            )

    def test_writes_and_verifies_sogou_without_changing_other_settings(self):
        with mock.patch.object(
            voice_hotkey_sync_windows,
            "_sogou_voice_process_running",
            return_value=False,
        ):
            result = voice_hotkey_sync_windows.sync_provider_hotkey(
                "sogou",
                "lctrl+lshift+f9",
                platform="win32",
                appdata=self.appdata,
            )

        self.assertTrue(result.ok)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["setting"]["shortcutKeysPress"],
            ["LeftCtrl", "LeftShift", "F9"],
        )
        self.assertTrue(saved["setting"]["longPressEnabled"])
        self.assertEqual(saved["unrelated"], {"keep": True})

    def test_writes_sogou_with_its_native_win_and_direction_names(self):
        with mock.patch.object(
            voice_hotkey_sync_windows,
            "_sogou_voice_process_running",
            return_value=False,
        ):
            result = voice_hotkey_sync_windows.sync_provider_hotkey(
                "sogou",
                "lctrl+lwin+up",
                platform="win32",
                appdata=self.appdata,
            )

        self.assertTrue(result.ok)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["setting"]["shortcutKeysPress"],
            ["LeftCtrl", "LeftMeta", "Up"],
        )

    def test_refuses_to_rewrite_a_running_sogou_assistant(self):
        before = self.path.read_bytes()
        with mock.patch.object(
            voice_hotkey_sync_windows,
            "_sogou_voice_process_running",
            return_value=True,
        ):
            result = voice_hotkey_sync_windows.sync_provider_hotkey(
                "sogou",
                "rctrl",
                platform="win32",
                appdata=self.appdata,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "restart_required")
        self.assertEqual(self.path.read_bytes(), before)

    def test_refuses_to_write_when_sogou_process_state_cannot_be_checked(self):
        before = self.path.read_bytes()
        with mock.patch.object(
            voice_hotkey_sync_windows,
            "_sogou_voice_process_running",
            return_value=None,
        ):
            result = voice_hotkey_sync_windows.sync_provider_hotkey(
                "sogou",
                "rctrl",
                platform="win32",
                appdata=self.appdata,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "process_check_failed")
        self.assertEqual(self.path.read_bytes(), before)

    def test_reports_sogou_config_read_permission_error_without_raising(self):
        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=PermissionError("access denied"),
        ), mock.patch.object(
            voice_hotkey_sync_windows,
            "_sogou_voice_process_running",
            return_value=False,
        ):
            result = voice_hotkey_sync_windows.sync_provider_hotkey(
                "sogou",
                "rctrl",
                platform="win32",
                appdata=self.appdata,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "read_failed")
        self.assertIn("access denied", result.message)

    def test_reports_actual_sogou_hotkey_when_rollback_fails(self):
        real_read = voice_hotkey_sync_windows._read_sogou_hotkey
        real_replace = voice_hotkey_sync_windows._replace_bytes_atomically
        read_count = 0
        replace_count = 0

        def read_with_first_verification_failure(*, appdata):
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                return voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                    "sogou", False, "read_failed", message="verification failed"
                )
            return real_read(appdata=appdata)

        def replace_with_rollback_failure(path, content):
            nonlocal replace_count
            replace_count += 1
            if replace_count == 2:
                raise OSError("rollback locked")
            real_replace(path, content)

        with mock.patch.object(
            voice_hotkey_sync_windows,
            "_sogou_voice_process_running",
            return_value=False,
        ), mock.patch.object(
            voice_hotkey_sync_windows,
            "_read_sogou_hotkey",
            side_effect=read_with_first_verification_failure,
        ), mock.patch.object(
            voice_hotkey_sync_windows,
            "_replace_bytes_atomically",
            side_effect=replace_with_rollback_failure,
        ):
            result = voice_hotkey_sync_windows.sync_provider_hotkey(
                "sogou",
                "lctrl+lshift+f9",
                platform="win32",
                appdata=self.appdata,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "rollback_failed")
        self.assertEqual(result.hotkey, "lctrl+lshift+f9")


class WeTypeVoiceHotkeyTests(unittest.TestCase):
    def test_explicit_read_dispatches_to_the_settings_reader(self):
        expected = voice_hotkey_sync_windows.VoiceHotkeySyncResult(
            "wetype",
            True,
            "read",
            "lctrl+lshift+f9",
            "已读取微信输入法的按住型快捷键。",
        )
        with mock.patch.object(
            voice_hotkey_sync_windows,
            "_read_wetype_hotkey",
            return_value=expected,
        ) as reader:
            result = voice_hotkey_sync_windows.read_provider_hotkey(
                "wetype", platform="win32", allow_settings_window=True
            )

        self.assertEqual(result, expected)
        reader.assert_called_once_with(cancel_event=None)

    def test_parses_the_hold_shortcut_text_from_wetype_settings(self):
        self.assertEqual(
            voice_hotkey_sync_windows._parse_wetype_settings_shortcut(
                "左 Ctrl 左 Shift F9"
            ),
            "lctrl+lshift+f9",
        )

    def test_rejects_old_statusbar_hint_as_a_settings_value(self):
        with self.assertRaises(ValueError):
            voice_hotkey_sync_windows._parse_wetype_settings_shortcut(
                "长按 左Ctrl+左Shift+F9 可使用语音输入"
            )

    def test_silent_read_does_not_open_or_read_wetype_ui(self):
        with mock.patch.object(voice_hotkey_sync_windows, "_read_wetype_hotkey") as reader:
            result = voice_hotkey_sync_windows.read_provider_hotkey("wetype", platform="win32")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "local_only")
        reader.assert_not_called()

    def test_settings_read_failure_never_returns_an_old_shortcut(self):
        from ovb_rc003 import wetype_settings_hotkey_windows as reader

        with mock.patch.object(reader, "read_hold_shortcut", side_effect=reader.SettingsReadError("open_failed", "无法打开")):
            result = voice_hotkey_sync_windows.read_provider_hotkey("wetype", platform="win32", allow_settings_window=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "open_failed")
        self.assertEqual(result.hotkey, "")

    def test_settings_value_preserves_modifier_sides(self):
        self.assertEqual(voice_hotkey_sync_windows._parse_wetype_settings_shortcut("左 Ctrl 左 Win"), "lctrl+lwin")
        self.assertEqual(voice_hotkey_sync_windows._parse_wetype_settings_shortcut("右 Alt"), "ralt")

    def test_sync_validates_and_returns_the_locally_saved_wetype_shortcut(self):
        result = voice_hotkey_sync_windows.sync_provider_hotkey(
            "wetype", "lctrl+lshift+f9", platform="win32"
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "local_only")
        self.assertEqual(result.hotkey, "lctrl+lshift+f9")
        self.assertIn("与微信输入法中的按住型快捷键一致", result.message)

    def test_validation_accepts_supported_wetype_shortcuts(self):
        for shortcut in ("lctrl+lwin", "lctrl+lshift+f9", "ralt"):
            with self.subTest(shortcut=shortcut):
                result = voice_hotkey_sync_windows.validate_provider_hotkey(
                    "wetype", shortcut
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.code, "valid")

    def test_validation_rejects_an_empty_wetype_shortcut(self):
        result = voice_hotkey_sync_windows.validate_provider_hotkey("wetype", "")

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_hotkey")

    def test_validation_rejects_a_wetype_letter_without_a_modifier(self):
        result = voice_hotkey_sync_windows.validate_provider_hotkey("wetype", "a")

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "missing_modifier")

    def test_validation_rejects_a_wetype_shortcut_longer_than_three_keys(self):
        result = voice_hotkey_sync_windows.validate_provider_hotkey(
            "wetype", "lctrl+lshift+lalt+f9"
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "too_many_keys")

    def test_validation_rejects_wetype_blocked_function_keys(self):
        for shortcut in ("lctrl+volume_up", "lctrl+vk_5f", "lctrl+vk_b4"):
            with self.subTest(shortcut=shortcut):
                result = voice_hotkey_sync_windows.validate_provider_hotkey(
                    "wetype", shortcut
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.code, "unsupported_key")

    def test_validation_rejects_equivalent_wetype_modifiers(self):
        result = voice_hotkey_sync_windows.validate_provider_hotkey(
            "wetype", "lctrl+rctrl"
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "duplicate_modifier")

    def test_validation_rejects_wetype_input_method_switch_chords(self):
        result = voice_hotkey_sync_windows.validate_provider_hotkey(
            "wetype", "lctrl+lshift"
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "reserved_hotkey")

    def test_custom_program_keeps_unrestricted_single_key_support(self):
        result = voice_hotkey_sync_windows.sync_provider_hotkey(
            "custom", "a", platform="win32"
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.hotkey, "a")


class DoubaoVoiceHotkeyTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.appdata = Path(self._tmpdir.name)
        self.path = self.appdata / "DoubaoIme" / "conf" / "config.json"
        self.path.parent.mkdir(parents=True)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_shortcut(self, modifier_flags, key_code):
        self.path.write_text(
            json.dumps(
                {
                    "voice": {
                        "voiceLongPressShortcut": {
                            "modifierFlags": modifier_flags,
                            "keyCode": key_code,
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_reads_the_current_doubao_right_alt_hold_shortcut(self):
        self._write_shortcut(2049, 0)

        result = voice_hotkey_sync_windows.read_provider_hotkey(
            "doubao_ime",
            platform="win32",
            appdata=self.appdata,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "read")
        self.assertEqual(result.hotkey, "ralt")

    def test_reads_every_sided_doubao_modifier(self):
        cases = (
            (0x0102, "lctrl"),
            (0x0202, "rctrl"),
            (0x0401, "lalt"),
            (0x0801, "ralt"),
            (0x1004, "lshift"),
            (0x2004, "rshift"),
            (0x4008, "lwin"),
            (0x8008, "rwin"),
        )

        for modifier_flags, expected in cases:
            with self.subTest(expected=expected):
                self._write_shortcut(modifier_flags, 0)
                result = voice_hotkey_sync_windows.read_provider_hotkey(
                    "doubao_ime",
                    platform="win32",
                    appdata=self.appdata,
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.hotkey, expected)

    def test_reads_a_doubao_modifier_and_function_key_shortcut(self):
        self._write_shortcut(0x1106, 0x78)

        result = voice_hotkey_sync_windows.read_provider_hotkey(
            "doubao_ime",
            platform="win32",
            appdata=self.appdata,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.hotkey, "lctrl+lshift+f9")

    def test_unknown_doubao_modifier_flags_fall_back_to_manual_entry(self):
        self._write_shortcut(0x10000, 0)

        result = voice_hotkey_sync_windows.read_provider_hotkey(
            "doubao_ime",
            platform="win32",
            appdata=self.appdata,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "read_failed")
        self.assertIn("暂不识别豆包修饰键标记", result.message)

    def test_inconsistent_doubao_modifier_flags_fall_back_to_manual_entry(self):
        self._write_shortcut(0x0101, 0)

        result = voice_hotkey_sync_windows.read_provider_hotkey(
            "doubao_ime",
            platform="win32",
            appdata=self.appdata,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "read_failed")
        self.assertIn("修饰键标记不一致", result.message)

    def test_manual_doubao_shortcut_is_saved_only_in_remote_mic(self):
        result = voice_hotkey_sync_windows.sync_provider_hotkey(
            "doubao_ime",
            "lctrl+lshift+f9",
            platform="win32",
            appdata=self.appdata,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "local_only")
        self.assertEqual(result.hotkey, "lctrl+lshift+f9")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
