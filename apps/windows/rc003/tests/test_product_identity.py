import inspect
import json
import subprocess
import sys
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
    def test_windows_portable_presentation_uses_full_version(self):
        version = "1.0.51-candidate.51"
        self.assertEqual(
            product_identity.windows_presentation_label(version),
            "无线麦 win版 1.0.51-candidate.51",
        )
        self.assertEqual(
            product_identity.windows_executable_name(version),
            "无线麦 win版 1.0.51-candidate.51.exe",
        )
        self.assertEqual(
            product_identity.windows_portable_folder_name(version),
            "无线麦 win版 1.0.51-candidate.51",
        )
        self.assertEqual(
            product_identity.windows_fixed_file_version(version),
            (1, 0, 51, 0),
        )
        self.assertTrue(product_identity.windows_version_is_prerelease(version))

    def test_windows_executable_recognition_is_exact_and_layout_aware(self):
        cases = {
            "RemoteMicRC003.exe": "legacy",
            "REMOTEMICRC003.EXE": "legacy",
            "无线麦 win版 1.0.51-candidate.51.exe": "current",
            "无线麦 WIN版 1.0.51-candidate.51.EXE": "current",
            "无线麦 win版 1.0.51.exe": "current",
        }
        for name, layout in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    product_identity.recognized_windows_executable_layout(name),
                    layout,
                )

        for name in (
            "RemoteMicRC003-old.exe",
            "无线麦 win版.exe",
            "无线麦 win版 1.0.exe",
            "无线麦 win版 01.0.51.exe",
            "无线麦 win版 1.0.51-.exe",
            "C:\\Apps\\无线麦 win版 1.0.51.exe",
            "..\\无线麦 win版 1.0.51.exe",
            "无线麦 win版 1.0.51.exe.bak",
        ):
            with self.subTest(name=name):
                self.assertIsNone(
                    product_identity.recognized_windows_executable_layout(name)
                )

        self.assertEqual(
            product_identity.windows_runtime_relative_directory_for_executable(
                "RemoteMicRC003.exe"
            ),
            Path("_internal"),
        )
        self.assertEqual(
            product_identity.windows_runtime_relative_directory_for_executable(
                "无线麦 win版 1.0.51-candidate.51.exe"
            ),
            Path("程序文件"),
        )

    def test_windows_version_rejects_unsafe_or_unrepresentable_values(self):
        for version in (
            " 1.0.51",
            "1.0",
            "1.0.51-",
            "1.0.51/other",
            "1.0.51\\other",
            "01.0.51",
            "1.0.51-candidate..51",
        ):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    product_identity.validate_version(version)
        with self.assertRaises(ValueError):
            product_identity.windows_fixed_file_version("65536.0.0")

    def test_build_adapter_exports_ascii_json_from_the_same_contract(self):
        script = _RC003_ROOT / "build" / "product-presentation.py"
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--source-root",
                str(_RC003_ROOT / "src"),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="ascii",
        )
        payload = json.loads(completed.stdout)
        version = (
            _RC003_ROOT / "src" / "ovb_rc003" / "VERSION"
        ).read_text(encoding="ascii").strip()
        self.assertEqual(payload["version"], version)
        self.assertEqual(
            payload["main_executable_name"],
            product_identity.windows_executable_name(version),
        )
        self.assertEqual(payload["runtime_directory_name"], "程序文件")
        self.assertEqual(payload["documentation_directory_name"], "说明与许可")
        self.assertEqual(payload["portable_readme_name"], "使用说明.txt")
        self.assertTrue(completed.stdout.isascii())

    def test_windows_version_metadata_distinguishes_main_and_helper(self):
        main = product_identity.windows_main_version_metadata("1.0.51-candidate.51")
        helper = product_identity.windows_hid_helper_version_metadata(
            "1.0.51-candidate.51"
        )
        self.assertEqual(main["file_description"], "无线麦 win版 1.0.51-candidate.51")
        self.assertEqual(main["original_filename"], "无线麦 win版 1.0.51-candidate.51.exe")
        self.assertEqual(main["internal_name"], "RemoteMicRC003")
        self.assertEqual(helper["file_description"], "无线麦 权限助手")
        self.assertEqual(helper["original_filename"], "RemoteMicRC003HidHelper.exe")
        self.assertTrue(main["prerelease"])

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
        ):
            self.assertIn(product_identity.DISPLAY_NAME, text)
            self.assertNotIn("Remote Mic", text)
        self.assertEqual(voice_program_manager.status_text(disabled_status),
                         "请选择语音程序；未配置时仅普通按键可用。")

    def test_qml_reads_the_controller_identity_instead_of_copying_the_name(self):
        qml_dir = _RC003_ROOT / "src" / "ovb_rc003" / "qml"
        main_qml = (qml_dir / "main.qml").read_text(encoding="utf-8")
        self.assertIn("SettingsController.applicationDisplayName", main_qml)
        self.assertIn("SettingsController.applicationPresentationLabel", main_qml)

        voice_qml = (qml_dir / "VoicePage.qml").read_text(encoding="utf-8")
        self.assertNotIn("Remote Mic", voice_qml)

        device_qml = (qml_dir / "DevicePage.qml").read_text(encoding="utf-8")
        self.assertNotIn("Remote Mic", device_qml)

        self.assertIn(
            "title: SettingsController.applicationPresentationLabel", main_qml
        )
        self.assertNotIn('title: "%1 · %2"', main_qml)
        self.assertNotIn(qt_settings_app.__version__, main_qml)
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
        self.assertNotIn("[InstallDelete]", installer)
        cleanup_start = installer.index("procedure DeleteObsoleteShortcuts;")
        cleanup_end = installer.index("\nend;", cleanup_start)
        cleanup = installer[cleanup_start:cleanup_end]
        for obsolete_shortcut in (
            r"{userdesktop}\Remote Mic · 小米遥控器2 Pro.lnk",
            r"{userdesktop}\Remote Mic · RC003.lnk",
            r"{userprograms}\Remote Mic\Remote Mic · 小米遥控器2 Pro.lnk",
            r"{userprograms}\Remote Mic\Remote Mic · 小米遥控器2 Pro 设置.lnk",
            r"{userprograms}\Remote Mic\停止 Remote Mic · 小米遥控器2 Pro.lnk",
            r"{userprograms}\Remote Mic\卸载 Remote Mic · 小米遥控器2 Pro.lnk",
            r"{userprograms}\Remote Mic\Remote Mic · RC003.lnk",
            r"{userprograms}\Remote Mic\Remote Mic · RC003 设置.lnk",
            r"{userprograms}\Remote Mic\停止 Remote Mic · RC003.lnk",
            r"{userprograms}\Remote Mic\卸载 Remote Mic · RC003.lnk",
        ):
            self.assertIn(
                f"DeleteFile(ExpandConstant('{obsolete_shortcut}'))",
                cleanup,
            )
        self.assertIn(
            r"RemoveDir(ExpandConstant('{userprograms}\Remote Mic'))",
            cleanup,
        )

        post_install = installer.index("procedure CurStepChanged(CurStep: TSetupStep);")
        validation = installer.index(
            "if not ValidateInstalledApplication(ValidationError)", post_install
        )
        commit = installer.index(
            "if not CommitUpgradeRuntimeQuarantine(CommitError)", validation
        )
        files_completed = installer.index(
            "InstallFilesCompleted := True;", commit
        )
        cleanup_call = installer.index("DeleteObsoleteShortcuts;", files_completed)
        helper_install = installer.index(
            "HidHelperInstallSucceeded := RunApplicationMaintenance(", cleanup_call
        )
        self.assertLess(validation, commit)
        self.assertLess(commit, files_completed)
        self.assertLess(files_completed, cleanup_call)
        self.assertLess(cleanup_call, helper_install)
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
