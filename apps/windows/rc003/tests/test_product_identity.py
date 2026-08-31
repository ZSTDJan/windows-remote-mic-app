import inspect
import unittest
from pathlib import Path

from ovb_rc003 import (
    bridge_tray_windows,
    key_mapping,
    product_identity,
    qt_settings_app,
    settings_ui,
    voice_hotkey_sync_windows,
    voice_program_manager,
)


_RC003_ROOT = Path(__file__).resolve().parents[1]


class ProductIdentityTests(unittest.TestCase):
    def test_user_visible_name_has_one_runtime_source(self):
        self.assertEqual(product_identity.DISPLAY_NAME, "无线麦")
        self.assertEqual(
            settings_ui._action_to_display(
                key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_REMOTE_MIC)
            ),
            "打开无线麦",
        )

        tooltip_default = inspect.signature(
            bridge_tray_windows.BridgeTray
        ).parameters["tooltip"].default
        self.assertEqual(tooltip_default, "无线麦 · 小米遥控器2 Pro")

    def test_voice_statuses_use_the_display_name(self):
        read_result = voice_hotkey_sync_windows.read_provider_hotkey(
            voice_program_manager.VOICE_PROGRAM_CUSTOM,
            platform="win32",
        )
        save_result = voice_hotkey_sync_windows.sync_provider_hotkey(
            voice_program_manager.VOICE_PROGRAM_WETYPE,
            "lctrl+lshift+f9",
            platform="win32",
        )
        disabled_status = voice_program_manager.VoiceProgramStatus(
            provider_id=voice_program_manager.VOICE_PROGRAM_NONE,
            display_name="不管理",
            available=False,
            running=False,
            elevated=None,
            executable=None,
            code="disabled",
        )

        for text in (
            read_result.message,
            save_result.message,
            voice_program_manager.status_text(disabled_status),
        ):
            self.assertIn(product_identity.DISPLAY_NAME, text)
            self.assertNotIn("Remote Mic", text)

    def test_qml_reads_the_controller_identity_instead_of_copying_the_name(self):
        qml_dir = _RC003_ROOT / "src" / "ovb_rc003" / "qml"
        for filename in ("main.qml", "DevicePage.qml", "VoicePage.qml"):
            text = (qml_dir / filename).read_text(encoding="utf-8")
            self.assertIn("SettingsController.applicationDisplayName", text)
            self.assertNotIn("Remote Mic", text)

        main_qml = (qml_dir / "main.qml").read_text(encoding="utf-8")
        self.assertIn("title: SettingsController.applicationDisplayName", main_qml)
        self.assertNotIn('title: qsTr("%1 设置")', main_qml)

    def test_qt_process_identity_uses_the_shared_display_name(self):
        calls = []

        class FakeApplication:
            def setApplicationName(self, value):
                calls.append(value)

        qt_settings_app._apply_application_identity(FakeApplication())
        self.assertEqual(calls, [product_identity.DISPLAY_NAME])

    def test_element_navigation_keeps_its_standalone_process_identity(self):
        navigation_host = (
            _RC003_ROOT / "scripts" / "element_navigation_windows_host.py"
        ).read_text(encoding="utf-8")

        self.assertIn('app.setApplicationName("元素导航")', navigation_host)
        self.assertNotIn("from ovb_rc003 import product_identity", navigation_host)
        self.assertNotIn("product_identity.DISPLAY_NAME", navigation_host)
        self.assertNotIn("Remote Mic Element Navigation", navigation_host)

    def test_installer_changes_only_user_visible_identity_and_cleans_old_shortcuts(self):
        installer = (
            _RC003_ROOT / "installer" / "RemoteMicRC003Setup.iss"
        ).read_text(encoding="utf-8")
        self.assertIn('#define AppName "无线麦"', installer)
        self.assertIn("DefaultGroupName=无线麦", installer)
        self.assertIn("UsePreviousGroup=no", installer)
        self.assertIn("[InstallDelete]", installer)
        self.assertIn("Remote Mic · 小米遥控器2 Pro.lnk", installer)
        self.assertIn("Remote Mic · RC003.lnk", installer)
        self.assertIn('#define AppExeName "RemoteMicRC003.exe"', installer)
        self.assertIn("DefaultDirName={localappdata}\\RemoteMic\\{#AppFolder}", installer)
        self.assertIn("'RemoteMicRC003'", installer)

    def test_development_shortcut_uses_new_name_and_validates_legacy_owner(self):
        script = (
            _RC003_ROOT / "build" / "install-dev-shortcut.ps1"
        ).read_text(encoding="utf-8")
        for codepoint in ("0x65E0", "0x7EBF", "0x9EA6"):
            self.assertIn(codepoint, script)
        self.assertIn("LegacyMatchesThisCheckout", script)
        self.assertIn("WorkingDirectory", script)
        self.assertIn("$LegacyShortcut.Arguments -eq $ShortcutArguments", script)
        self.assertIn("Remove-Item -LiteralPath $LegacyShortcutPath", script)


if __name__ == "__main__":
    unittest.main()
