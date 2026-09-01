import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ovb_rc003 import voice_hotkey_sync_windows


class VoiceHotkeyDefaultsTests(unittest.TestCase):
    def test_defaults_match_each_provider_native_or_fixed_shortcut(self):
        self.assertEqual(voice_hotkey_sync_windows.default_hotkey("sogou"), "rctrl")
        self.assertEqual(
            voice_hotkey_sync_windows.default_hotkey("wetype"), "lctrl+lwin"
        )
        self.assertEqual(
            voice_hotkey_sync_windows.default_hotkey("windows_dictation"),
            "win+h",
        )
        self.assertEqual(voice_hotkey_sync_windows.default_hotkey("custom"), "ralt")

    def test_windows_dictation_is_read_as_a_fixed_shortcut(self):
        result = voice_hotkey_sync_windows.read_provider_hotkey(
            "windows_dictation", platform="win32"
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "fixed")
        self.assertEqual(result.hotkey, "win+h")


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
    def test_read_uses_the_per_program_memory_without_opening_wetype(self):
        result = voice_hotkey_sync_windows.read_provider_hotkey(
            "wetype", platform="win32"
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "local_only")
        self.assertIn("不自动打开", result.message)

    def test_sync_only_updates_remote_mic_and_keeps_the_normalized_value(self):
        result = voice_hotkey_sync_windows.sync_provider_hotkey(
            "wetype", "lctrl+lshift+f9", platform="win32"
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "local_only")
        self.assertEqual(result.hotkey, "lctrl+lshift+f9")
        self.assertIn("微信输入法设置", result.message)


if __name__ == "__main__":
    unittest.main()
