import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import app, config, logging_setup, voice_program_manager as manager


class VoiceProgramSettingsTests(unittest.TestCase):
    def test_defaults_keep_provider_management_disabled(self):
        self.assertEqual(
            manager.normalize_voice_program_settings(None),
            {
                "provider": "none",
                "custom_executable": "",
                "launch_on_bridge_start": False,
                "launch_elevated": False,
                "launch_elevated_by_provider": {
                    "sogou": True,
                    "custom": False,
                },
            },
        )

    def test_unknown_provider_and_non_boolean_switches_fail_closed(self):
        self.assertEqual(
            manager.normalize_voice_program_settings(
                {
                    "provider": "unknown",
                    "custom_executable": " voice.exe ",
                    "launch_on_bridge_start": "yes",
                    "launch_elevated": 1,
                }
            ),
            {
                "provider": "none",
                "custom_executable": "voice.exe",
                "launch_on_bridge_start": False,
                "launch_elevated": False,
                "launch_elevated_by_provider": {
                    "sogou": False,
                    "custom": False,
                },
            },
        )

    def test_inspection_reuses_one_process_snapshot_for_sogou(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "sogou_voice_assistant.exe"
            executable.touch()
            calls = []

            def process_iter():
                calls.append(True)
                return (manager.ProcessInfo(12, executable.name, executable, False),)

            status = manager.inspect_voice_program(
                {"provider": "sogou"},
                platform="win32",
                process_iter=process_iter,
            )

        self.assertEqual(len(calls), 1)
        self.assertTrue(status.running)
        self.assertEqual(status.executable, executable)

    def test_unsupported_inspection_does_not_enumerate_windows_processes(self):
        status = manager.inspect_voice_program(
            {"provider": "sogou"},
            platform="linux",
            process_iter=lambda: self.fail("must not enumerate Windows processes"),
        )

        self.assertFalse(status.available)
        self.assertEqual(status.code, "not_found")

    def test_disabled_management_keeps_only_the_elevation_preference(self):
        self.assertEqual(
            manager.normalize_voice_program_settings(
                {
                    "provider": "none",
                    "custom_executable": " voice.exe ",
                    "launch_on_bridge_start": True,
                    "launch_elevated": True,
                }
            ),
            {
                "provider": "none",
                "custom_executable": "voice.exe",
                "launch_on_bridge_start": False,
                "launch_elevated": True,
                "launch_elevated_by_provider": {
                    "sogou": True,
                    "custom": True,
                },
            },
        )

    def test_new_sogou_selection_defaults_to_elevated_launch(self):
        normalized = manager.normalize_voice_program_settings(
            {"provider": "sogou"}
        )

        self.assertTrue(normalized["launch_elevated"])
        self.assertEqual(
            normalized["launch_elevated_by_provider"],
            {"sogou": True, "custom": False},
        )

    def test_new_custom_selection_defaults_to_standard_launch(self):
        normalized = manager.normalize_voice_program_settings(
            {"provider": "custom"}
        )

        self.assertFalse(normalized["launch_elevated"])
        self.assertEqual(
            normalized["launch_elevated_by_provider"],
            {"sogou": True, "custom": False},
        )

    def test_legacy_elevation_choice_is_migrated_to_both_launchable_providers(self):
        for legacy_value in (False, True):
            with self.subTest(legacy_value=legacy_value):
                normalized = manager.normalize_voice_program_settings(
                    {
                        "provider": "sogou",
                        "launch_elevated": legacy_value,
                    }
                )

                self.assertIs(normalized["launch_elevated"], legacy_value)
                self.assertEqual(
                    normalized["launch_elevated_by_provider"],
                    {"sogou": legacy_value, "custom": legacy_value},
                )

    def test_scoped_elevation_preference_follows_the_selected_provider(self):
        preferences = {"sogou": False, "custom": True}

        sogou = manager.normalize_voice_program_settings(
            {
                "provider": "sogou",
                "launch_elevated": True,
                "launch_elevated_by_provider": preferences,
            }
        )
        custom = manager.normalize_voice_program_settings(
            {
                "provider": "custom",
                "launch_elevated": False,
                "launch_elevated_by_provider": preferences,
            }
        )

        self.assertFalse(sogou["launch_elevated"])
        self.assertTrue(custom["launch_elevated"])

    def test_provider_options_include_windows_dictation_and_custom_program(self):
        self.assertEqual(
            manager.provider_options(),
            [
                "不管理",
                "搜狗语音输入",
                "微信输入法",
                "Windows 语音输入（Win+H）",
                "自定义程序",
            ],
        )

    def test_system_managed_provider_never_requests_bridge_autostart(self):
        normalized = manager.normalize_voice_program_settings(
            {
                "provider": "wetype",
                "launch_on_bridge_start": True,
                "launch_elevated": True,
            }
        )

        self.assertFalse(normalized["launch_on_bridge_start"])
        self.assertTrue(normalized["launch_elevated"])
        self.assertTrue(manager.is_system_managed_provider("wetype"))

    def test_windows_dictation_is_system_managed_and_never_autostarts(self):
        normalized = manager.normalize_voice_program_settings(
            {
                "provider": "windows_dictation",
                "launch_on_bridge_start": True,
                "launch_elevated": True,
            }
        )

        self.assertFalse(normalized["launch_on_bridge_start"])
        self.assertTrue(normalized["launch_elevated"])
        self.assertTrue(manager.is_system_managed_provider("windows_dictation"))


class WindowsDictationTests(unittest.TestCase):
    def test_windows_dictation_is_available_without_an_executable(self):
        status = manager.inspect_voice_program(
            {"provider": "windows_dictation"},
            platform="win32",
            process_iter=lambda: (),
        )
        result = manager.launch_voice_program(
            {"provider": "windows_dictation"},
            platform="win32",
            process_iter=lambda: (),
            start_file=lambda *_: self.fail("Windows 听写不应由 Remote Mic 启动进程"),
        )

        self.assertTrue(status.available)
        self.assertFalse(status.running)
        self.assertEqual(status.code, "stopped")
        self.assertIn("Win+H", manager.status_text(status))
        self.assertEqual(result.code, "system_managed")


class SogouDiscoveryTests(unittest.TestCase):
    def test_running_process_path_is_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "sogou_voice_assistant.exe"
            executable.touch()
            found = manager.discover_sogou_voice_executable(
                platform="win32",
                process_iter=lambda: (
                    manager.ProcessInfo(12, executable.name, executable, True),
                ),
                run_value_reader=lambda: (),
            )
        self.assertEqual(found, executable)

    def test_run_entry_locates_the_newest_installed_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            components = Path(tmp) / "Components"
            manager_path = components / "SogouComMgr.exe"
            manager_path.parent.mkdir(parents=True)
            manager_path.touch()
            older = (
                components
                / "ai_voice_input"
                / "1.0.1.2000"
                / "bin"
                / "sogou_voice_assistant.exe"
            )
            newer = (
                components
                / "ai_voice_input"
                / "1.0.1.3272"
                / "bin"
                / "sogou_voice_assistant.exe"
            )
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.touch()
            newer.touch()

            found = manager.discover_sogou_voice_executable(
                platform="win32",
                process_iter=lambda: (),
                run_value_reader=lambda: (
                    f'"{manager_path}" -invoke AIVoiceInputComBundle',
                ),
            )
        self.assertEqual(found, newer)

    def test_component_prewarm_uses_the_registered_manager_without_a_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager_path = Path(tmp) / "SogouComMgr.exe"
            manager_path.touch()
            calls = []

            result = manager.prewarm_sogou_voice_component(
                platform="win32",
                run_value_reader=lambda: (
                    f'"{manager_path}" -invoke AIVoiceInputComBundle',
                ),
                popen=lambda command, **kwargs: calls.append((command, kwargs)),
            )

        self.assertTrue(result.attempted)
        self.assertEqual(result.code, "started")
        self.assertEqual(calls[0][0][0], str(manager_path))
        self.assertIn("--auto-launch", calls[0][0])
        self.assertNotIn("shell", calls[0][1])

    def test_running_sogou_process_without_visible_window_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "sogou_voice_assistant.exe"
            executable.touch()
            status = manager.inspect_voice_program(
                {"provider": "sogou"},
                platform="win32",
                process_iter=lambda: (
                    manager.ProcessInfo(12, executable.name, executable, False),
                ),
                visible_window_pids=lambda: (),
            )

        self.assertEqual(status.code, "running_not_ready")
        self.assertIn("尚未就绪", manager.status_text(status))

    def test_running_sogou_process_with_visible_window_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "sogou_voice_assistant.exe"
            executable.touch()
            status = manager.inspect_voice_program(
                {"provider": "sogou"},
                platform="win32",
                process_iter=lambda: (
                    manager.ProcessInfo(12, executable.name, executable, False),
                ),
                visible_window_pids=lambda: (12,),
            )

        self.assertEqual(status.code, "running")


class WeTypeDiscoveryTests(unittest.TestCase):
    def test_running_server_path_is_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "wetype_server.exe"
            executable.touch()
            found = manager.discover_wetype_executable(
                platform="win32",
                process_iter=lambda: (
                    manager.ProcessInfo(12, executable.name, executable, False),
                ),
                install_value_reader=lambda: (),
                shortcut_iter=lambda: (),
            )

        self.assertEqual(found, executable)

    def test_install_location_finds_the_server_without_a_saved_user_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            install_root = Path(tmp) / "Tencent" / "WeType"
            executable = install_root / "wetype_server.exe"
            install_root.mkdir(parents=True)
            executable.touch()
            with mock.patch.object(manager, "_wetype_default_roots", return_value=()):
                found = manager.discover_wetype_executable(
                    platform="win32",
                    process_iter=lambda: (),
                    install_value_reader=lambda: (str(install_root),),
                    shortcut_iter=lambda: (),
                )

        self.assertEqual(found, executable)

    def test_system_managed_provider_is_detected_but_never_launched(self):
        with tempfile.TemporaryDirectory() as tmp:
            install_root = Path(tmp) / "Tencent" / "WeType"
            executable = install_root / "wetype_server.exe"
            install_root.mkdir(parents=True)
            executable.touch()
            settings = {"provider": "wetype", "launch_on_bridge_start": True}
            with mock.patch.object(manager, "_wetype_default_roots", return_value=()):
                status = manager.inspect_voice_program(
                    settings,
                    platform="win32",
                    process_iter=lambda: (),
                    wetype_install_value_reader=lambda: (str(install_root),),
                    wetype_shortcut_iter=lambda: (),
                )
                result = manager.launch_voice_program(
                    settings,
                    platform="win32",
                    process_iter=lambda: (),
                    wetype_install_value_reader=lambda: (str(install_root),),
                    wetype_shortcut_iter=lambda: (),
                    start_file=lambda *_: self.fail("微信输入法不应由 Remote Mic 启动"),
                )

        self.assertTrue(status.available)
        self.assertFalse(status.running)
        self.assertEqual(status.code, "stopped")
        self.assertEqual(result.code, "system_managed")


class VoiceProgramSettingsTargetTests(unittest.TestCase):
    def test_installed_sogou_settings_are_manual(self):
        with tempfile.TemporaryDirectory() as tmp:
            components = Path(tmp) / "SogouInput" / "Components"
            executable = (
                components
                / "ai_voice_input"
                / "1.0.1.2"
                / "bin"
                / "sogou_voice_assistant.exe"
            )
            executable.parent.mkdir(parents=True)
            executable.touch()
            manager_executable = components / "SogouComMgr.exe"

            target = manager.resolve_voice_program_settings_target(
                {"provider": "sogou"},
                platform="win32",
                process_iter=lambda: (),
                run_value_reader=lambda: (f'"{manager_executable}" --show',),
            )

        self.assertTrue(target.available)
        self.assertEqual(target.kind, "sogou_manual")
        self.assertEqual(Path(target.target), executable)

    def test_missing_sogou_voice_uses_the_ai_toolbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            components = Path(tmp) / "SogouInput" / "Components"
            toolbox = (
                components
                / "IChat"
                / "1.0.2.3"
                / "SOGOUSmartAssistant.exe"
            )
            toolbox.parent.mkdir(parents=True)
            toolbox.touch()

            target = manager.resolve_voice_program_settings_target(
                {"provider": "sogou"},
                platform="win32",
                process_iter=lambda: (),
                run_value_reader=lambda: (),
                sogou_install_value_reader=lambda: (
                    str(components.parent),
                ),
            )

        self.assertTrue(target.available)
        self.assertEqual(target.kind, "sogou_toolbox")
        self.assertEqual(Path(target.target), toolbox)
        self.assertEqual(target.arguments, "--from=menutool")

    def test_missing_sogou_voice_and_toolbox_stays_missing(self):
        target = manager.resolve_voice_program_settings_target(
            {"provider": "sogou"},
            platform="win32",
            process_iter=lambda: (),
            run_value_reader=lambda: (),
            sogou_install_value_reader=lambda: (),
        )

        self.assertFalse(target.available)
        self.assertEqual(target.kind, "missing")

    def test_wetype_settings_use_the_installed_update_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            install_root = Path(tmp) / "Tencent" / "WeType"
            server = install_root / "wetype_server.exe"
            settings = install_root / "wetype_update.exe"
            install_root.mkdir(parents=True)
            server.touch()
            settings.touch()

            target = manager.resolve_voice_program_settings_target(
                {"provider": "wetype"},
                platform="win32",
                process_iter=lambda: (
                    manager.ProcessInfo(12, server.name, server, False),
                ),
                wetype_install_value_reader=lambda: (),
                wetype_shortcut_iter=lambda: (),
            )

        self.assertTrue(target.available)
        self.assertEqual(target.kind, "executable")
        self.assertEqual(Path(target.target), settings)
        self.assertEqual(target.arguments, "-showsetting")

    def test_windows_dictation_settings_use_the_system_speech_uri(self):
        target = manager.resolve_voice_program_settings_target(
            {"provider": "windows_dictation"}, platform="win32"
        )

        self.assertTrue(target.available)
        self.assertEqual(target.kind, "uri")
        self.assertEqual(target.target, "ms-settings:speech")


class VoiceProgramLaunchTests(unittest.TestCase):
    def _custom_settings(self, executable: Path, **updates):
        settings = {
            "provider": "custom",
            "custom_executable": str(executable),
            "launch_on_bridge_start": False,
            "launch_elevated": False,
        }
        settings.update(updates)
        return settings

    def test_provider_settings_launch_uses_open_with_explicit_arguments(self):
        executable = Path(r"C:\Program Files\Tencent\WeType\wetype_update.exe")
        calls = []

        manager.open_voice_program_settings(
            executable,
            "-showsetting",
            platform="win32",
            start_file=lambda path, operation, arguments, cwd: calls.append(
                (path, operation, arguments, cwd)
            ),
        )

        self.assertEqual(
            calls,
            [
                (
                    str(executable),
                    "open",
                    "-showsetting",
                    str(executable.parent),
                )
            ],
        )

    def test_custom_program_launches_with_current_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "voice.exe"
            executable.touch()
            calls = []
            result = manager.launch_voice_program(
                self._custom_settings(executable),
                platform="win32",
                process_iter=lambda: (),
                start_file=lambda path, operation, cwd: calls.append(
                    (path, operation, cwd)
                ),
            )
        self.assertTrue(result.started)
        self.assertEqual(result.code, "started")
        self.assertEqual(calls[0][1], "open")

    def test_custom_shortcut_matches_its_target_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shortcut = root / "voice.lnk"
            target = root / "bin" / "voice.exe"
            shortcut.touch()
            target.parent.mkdir()
            target.touch()
            status = manager.inspect_voice_program(
                self._custom_settings(shortcut),
                platform="win32",
                process_iter=lambda: (
                    manager.ProcessInfo(42, target.name, target, False),
                ),
                shortcut_resolver=lambda path: target,
            )
        self.assertTrue(status.running)
        self.assertEqual(status.code, "running")

    def test_custom_shortcut_launches_the_shortcut_not_the_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shortcut = root / "voice.lnk"
            target = root / "bin" / "voice.exe"
            shortcut.touch()
            target.parent.mkdir()
            target.touch()
            calls = []
            result = manager.launch_voice_program(
                self._custom_settings(shortcut),
                platform="win32",
                process_iter=lambda: (),
                shortcut_resolver=lambda path: target,
                start_file=lambda path, operation, cwd: calls.append(
                    (path, operation, cwd)
                ),
            )
            self.assertTrue(result.started)
            self.assertEqual(Path(calls[0][0]).resolve(), shortcut.resolve())
            self.assertEqual(Path(calls[0][2]).resolve(), shortcut.parent.resolve())

    def test_custom_program_does_not_match_same_name_from_another_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = root / "configured" / "voice.exe"
            other = root / "other" / "voice.exe"
            configured.parent.mkdir()
            other.parent.mkdir()
            configured.touch()
            other.touch()
            status = manager.inspect_voice_program(
                self._custom_settings(configured),
                platform="win32",
                process_iter=lambda: (
                    manager.ProcessInfo(43, other.name, other, False),
                ),
            )
        self.assertFalse(status.running)
        self.assertEqual(status.code, "stopped")

    def test_custom_program_uses_name_only_when_process_path_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "voice.exe"
            executable.touch()
            status = manager.inspect_voice_program(
                self._custom_settings(executable),
                platform="win32",
                process_iter=lambda: (
                    manager.ProcessInfo(44, executable.name, None, True),
                ),
            )
        self.assertTrue(status.running)
        self.assertTrue(status.elevated)

    def test_unresolved_shortcut_fails_closed_without_launching(self):
        with tempfile.TemporaryDirectory() as tmp:
            shortcut = Path(tmp) / "voice.lnk"
            shortcut.touch()
            status = manager.inspect_voice_program(
                self._custom_settings(shortcut),
                platform="win32",
                shortcut_resolver=lambda path: None,
            )
            result = manager.launch_voice_program(
                self._custom_settings(shortcut),
                platform="win32",
                process_iter=lambda: (),
                shortcut_resolver=lambda path: None,
                start_file=lambda *_: self.fail("unresolved shortcut must not launch"),
            )
        self.assertFalse(status.available)
        self.assertEqual(status.code, "not_found")
        self.assertEqual(result.code, "not_found")

    def test_shortcut_resolution_failure_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shortcut = root / "voice.lnk"
            target = root / "voice.exe"
            shortcut.touch()
            target.touch()
            manager._resolve_shortcut_target_cached.cache_clear()
            try:
                with mock.patch.object(
                    manager,
                    "_read_windows_shortcut_target",
                    side_effect=(None, target),
                ) as reader:
                    self.assertIsNone(manager._resolve_shortcut_target(shortcut))
                    self.assertEqual(
                        manager._resolve_shortcut_target(shortcut), target.resolve()
                    )
                    self.assertEqual(reader.call_count, 2)
            finally:
                manager._resolve_shortcut_target_cached.cache_clear()

    def test_shortcut_target_privilege_mismatch_requires_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shortcut = root / "voice.lnk"
            target = root / "voice.exe"
            shortcut.touch()
            target.touch()
            result = manager.launch_voice_program(
                self._custom_settings(shortcut, launch_elevated=True),
                platform="win32",
                process_iter=lambda: (
                    manager.ProcessInfo(45, target.name, target, False),
                ),
                shortcut_resolver=lambda path: target,
                start_file=lambda *_: self.fail("must not launch a second instance"),
            )
        self.assertEqual(result.code, "restart_elevated_required")

    def test_elevated_launch_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "voice.exe"
            executable.touch()
            calls = []
            result = manager.launch_voice_program(
                self._custom_settings(executable, launch_elevated=True),
                platform="win32",
                process_iter=lambda: (),
                start_file=lambda path, operation, cwd: calls.append(
                    (path, operation, cwd)
                ),
            )
        self.assertTrue(result.started)
        self.assertEqual(calls[0][1], "run" + "as")

    def test_running_lower_privilege_program_is_not_reported_as_elevated(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "voice.exe"
            executable.touch()
            result = manager.launch_voice_program(
                self._custom_settings(executable, launch_elevated=True),
                platform="win32",
                process_iter=lambda: (
                    manager.ProcessInfo(41, executable.name, executable, False),
                ),
                start_file=lambda *_: self.fail("must not launch a second instance"),
            )
        self.assertFalse(result.started)
        self.assertTrue(result.already_running)
        self.assertEqual(result.code, "restart_elevated_required")

    def test_uac_cancellation_is_a_normal_provider_result(self):
        class CancelledError(OSError):
            winerror = 1223

        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "voice.exe"
            executable.touch()
            result = manager.launch_voice_program(
                self._custom_settings(executable, launch_elevated=True),
                platform="win32",
                process_iter=lambda: (),
                start_file=lambda *_: (_ for _ in ()).throw(CancelledError()),
            )
        self.assertEqual(result.code, "cancelled")

    def test_bridge_start_does_nothing_until_explicitly_enabled(self):
        result = manager.launch_configured_at_bridge_start(
            {"voice_program": {"provider": "sogou"}}
        )
        self.assertEqual(result.code, "not_requested")

    def test_bridge_start_uses_the_same_launcher_when_enabled(self):
        calls = []
        expected = manager.VoiceProgramLaunchResult(
            "sogou", True, False, "started"
        )
        result = manager.launch_configured_at_bridge_start(
            {
                "voice_program": {
                    "provider": "sogou",
                    "launch_on_bridge_start": True,
                }
            },
            launcher=lambda settings: calls.append(dict(settings)) or expected,
        )
        self.assertIs(result, expected)
        self.assertEqual(calls[0]["provider"], "sogou")


class BridgeStartupWiringTests(unittest.TestCase):
    def test_app_invokes_the_optional_voice_program_startup_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = manager.VoiceProgramLaunchResult(
                "none", False, False, "not_requested"
            )
            try:
                with (
                    mock.patch.object(
                        config, "config_root", return_value=Path(tmp)
                    ),
                    mock.patch.object(
                        app.voice_program_manager,
                        "launch_configured_at_bridge_start",
                        return_value=result,
                    ) as launch,
                ):
                    app.RC003App()
                launch.assert_called_once()
            finally:
                logger = logging.getLogger(logging_setup.LOGGER_NAME)
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)
                logging_setup._configured = False
                asyncio.set_event_loop(None)
                loop.close()


if __name__ == "__main__":
    unittest.main()
