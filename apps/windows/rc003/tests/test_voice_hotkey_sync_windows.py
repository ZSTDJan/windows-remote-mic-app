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


class FakeHotkeyGroup:
    def __init__(self, name):
        self.Name = name
        self.clicks = 0

    def Click(self):
        self.clicks += 1


class WeTypeVoiceHotkeyTests(unittest.TestCase):
    def test_opens_wetype_settings_through_the_reviewed_program_launcher(self):
        executable = Path(r"C:\Program Files\Tencent\WeType\wetype_update.exe")
        with mock.patch.object(
            voice_hotkey_sync_windows.voice_program_manager,
            "open_voice_program_settings",
        ) as launcher:
            voice_hotkey_sync_windows._show_wetype_settings(executable)

        launcher.assert_called_once_with(executable, "-showsetting")

    def test_parses_wetype_directional_accessibility_name(self):
        self.assertEqual(
            voice_hotkey_sync_windows._wetype_name_to_hotkey(
                "左\nCtrl\n左\nShift\nF9"
            ),
            "lctrl+lshift+f9",
        )
        self.assertEqual(
            voice_hotkey_sync_windows._wetype_name_to_hotkey("右\nCtrl"),
            "rctrl",
        )

    def test_reads_wetype_hotkey_from_the_semantic_control(self):
        group = FakeHotkeyGroup("左\nCtrl\n左\nShift\nF9")
        with mock.patch.object(
            voice_hotkey_sync_windows,
            "_with_wetype_window",
            side_effect=lambda callback: callback(object()),
        ), mock.patch.object(
            voice_hotkey_sync_windows,
            "_find_wetype_hotkey_group",
            return_value=group,
        ):
            result = voice_hotkey_sync_windows.read_provider_hotkey(
                "wetype", platform="win32"
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.hotkey, "lctrl+lshift+f9")

    def test_writes_wetype_and_requires_matching_readback(self):
        group = FakeHotkeyGroup("左\nCtrl\n左\nShift\nF9")

        def send(tokens):
            self.assertEqual(tuple(tokens), ("lctrl", "lshift", "f8"))
            group.Name = "左\nCtrl\n左\nShift\nF8"

        with mock.patch.object(
            voice_hotkey_sync_windows,
            "_with_wetype_window",
            side_effect=lambda callback: callback(object()),
        ), mock.patch.object(
            voice_hotkey_sync_windows,
            "_find_wetype_hotkey_group",
            return_value=group,
        ), mock.patch.object(
            voice_hotkey_sync_windows.win32_input,
            "send_wetype_voice_key_combo_tap",
            side_effect=send,
        ):
            result = voice_hotkey_sync_windows.sync_provider_hotkey(
                "wetype", "lctrl+lshift+f8", platform="win32"
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.hotkey, "lctrl+lshift+f8")
        self.assertEqual(group.clicks, 1)


if __name__ == "__main__":
    unittest.main()
