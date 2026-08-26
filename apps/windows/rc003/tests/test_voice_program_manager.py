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
            },
        )

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
            },
        )

    def test_provider_options_use_the_expandable_custom_program_name(self):
        self.assertEqual(
            manager.provider_options(),
            ["不管理", "搜狗语音输入", "其他输入法或自定义程序"],
        )


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
