import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import action_executor, key_mapping


class SemanticApplicationActionTests(unittest.TestCase):
    def setUp(self):
        action_executor.clear_application_command_cache()

    def tearDown(self):
        action_executor.clear_application_command_cache()

    def test_wechat_shortcut_lookup_does_not_select_enterprise_wechat(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp2:
            start_menu = (
                Path(tmp)
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
            )
            start_menu.mkdir(parents=True)
            (start_menu / "企业微信.lnk").write_bytes(b"shortcut")
            with mock.patch.dict(
                os.environ,
                {"APPDATA": tmp, "PROGRAMDATA": tmp2},
                clear=False,
            ):
                shortcuts = list(
                    action_executor._start_menu_shortcuts(
                        ("微信", "WeChat"), exact_only=True
                    )
                )

        self.assertEqual(shortcuts, [])

    def test_missing_start_menu_environment_never_searches_the_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            working_directory = Path(tmp)
            fake_start_menu = (
                working_directory
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
            )
            fake_start_menu.mkdir(parents=True)
            (fake_start_menu / "Codex.lnk").write_bytes(b"shortcut")
            original_cwd = Path.cwd()
            try:
                os.chdir(working_directory)
                with mock.patch.dict(os.environ, {}, clear=True):
                    shortcuts = list(
                        action_executor._start_menu_shortcuts(("Codex",))
                    )
            finally:
                os.chdir(original_cwd)

        self.assertEqual(shortcuts, [])

    def test_resolves_a_reference_application_action_to_a_real_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "Codex.exe"
            executable.write_bytes(b"not executed by this test")
            action = key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CODEX)

            with mock.patch.object(
                action_executor, "_candidate_paths", return_value=[executable]
            ):
                command = action_executor.resolve_application_command(action)

        self.assertEqual(command, (str(executable),))

    def test_open_uses_the_resolved_command_and_does_not_start_a_real_process(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CHROME)
        calls = []
        with mock.patch.object(
            action_executor,
            "resolve_application_command",
            return_value=("C:/Apps/Chrome.exe",),
        ):
            started = action_executor.open_configured_application(
                action,
                launcher=lambda command: calls.append(tuple(command)),
            )

        self.assertTrue(started)
        self.assertEqual(calls, [("C:/Apps/Chrome.exe",)])

    def test_successful_application_resolution_is_cached_while_target_exists(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CODEX)
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "Codex.exe"
            executable.write_bytes(b"not executed")
            with mock.patch.object(
                action_executor,
                "_candidate_paths",
                return_value=[executable],
            ) as candidates:
                first = action_executor.resolve_application_command(action)
                second = action_executor.resolve_application_command(action)

        self.assertEqual(first, (str(executable),))
        self.assertEqual(second, first)
        self.assertEqual(candidates.call_count, 1)

    def test_missing_application_cache_expires(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CMUX)
        with mock.patch.object(
            action_executor,
            "_candidate_paths",
            return_value=[],
        ) as candidates, mock.patch.object(
            action_executor.time,
            "monotonic",
            side_effect=[10.0, 20.0, 50.1],
        ):
            self.assertIsNone(action_executor.resolve_application_command(action))
            self.assertIsNone(action_executor.resolve_application_command(action))
            self.assertIsNone(action_executor.resolve_application_command(action))

        self.assertEqual(candidates.call_count, 2)

    def test_launch_failure_invalidates_cached_application_command(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CHROME)
        command = ("C:/Apps/Chrome.exe",)
        action_executor._application_command_cache[action.kind] = (1.0, command)
        with mock.patch.object(
            action_executor,
            "resolve_application_command",
            return_value=command,
        ), self.assertRaises(OSError):
            action_executor.open_configured_application(
                action,
                launcher=lambda _command: (_ for _ in ()).throw(OSError("boom")),
            )

        self.assertNotIn(action.kind, action_executor._application_command_cache)

    def test_missing_application_is_reported_without_launching_anything(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CMUX)
        with mock.patch.object(
            action_executor, "resolve_application_command", return_value=None
        ):
            self.assertFalse(action_executor.open_configured_application(action))

    def test_quicker_uri_is_opened_directly_without_a_shell_command(self):
        action = key_mapping.ButtonAction(
            key_mapping.ActionKind.QUICKER_URI,
            uri="quicker:runaction:test-action?hello",
        )
        calls = []

        self.assertTrue(
            action_executor.open_quicker_uri(
                action,
                launcher=lambda uri: calls.append(uri),
            )
        )
        self.assertEqual(calls, ["quicker:runaction:test-action?hello"])

    def test_non_quicker_action_is_not_sent_to_the_uri_launcher(self):
        calls = []
        self.assertFalse(
            action_executor.open_quicker_uri(
                key_mapping.ButtonAction(key_mapping.ActionKind.ESCAPE),
                launcher=lambda uri: calls.append(uri),
            )
        )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
