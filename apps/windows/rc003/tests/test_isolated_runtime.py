"""Source test entry, filesystem and control boundaries; no live OS resources."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ovb_rc003 import (
    __main__ as entry, config, dev_session, startup_windows,
    single_instance, settings_ui, hid_elevation_windows,
    bridge_control_windows, element_navigation_control_windows as navigation,
)


class IsolatedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {dev_session.ISOLATED_ENV: "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_config_and_navigation_use_source_root_not_localappdata(self):
        expected = Path(dev_session.__file__).resolve().parents[2] / ".build" / "isolated-runtime"
        self.assertEqual(config.config_root(), expected)
        self.assertEqual(Path(navigation._remote_mic_quicker_state_file()).parent, expected)

    def test_config_round_trip_only_changes_test_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            test_root = Path(temporary) / "test"
            live = Path(temporary) / "live"
            live.mkdir()
            original = live / "config.json"
            original.write_bytes(b'{"sentinel": "unchanged"}')
            before = original.read_bytes()
            with patch.object(dev_session, "isolated_root", return_value=test_root):
                model = config.load_config(config.config_path())
                config.save_config(config.config_path(), model)
                self.assertTrue(config.config_path().is_file())
            self.assertEqual(original.read_bytes(), before)

    def test_cli_marker_is_inherited_without_repurposing_old_marker(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dev_session.consume_marker(["--settings", dev_session.ISOLATED_FLAG]), ["--settings"])
            self.assertTrue(dev_session.is_isolated())
            self.assertFalse(dev_session.is_active())
            self.assertIn(dev_session.ISOLATED_FLAG, dev_session.mark_command(["child"]))

    def test_frozen_test_entry_fails_closed(self):
        with patch.object(dev_session.sys, "frozen", True, create=True):
            with self.assertRaises(ValueError):
                config.config_root()

    def test_no_self_start_registry_access(self):
        with patch.object(startup_windows, "_load_winreg") as registry:
            self.assertFalse(startup_windows.read_startup_state().enabled)
            self.assertTrue(startup_windows.set_startup_enabled(True).error)
            self.assertTrue(startup_windows.set_startup_enabled(False).error)
            self.assertFalse(startup_windows.rebind_owned_frozen_startup(frozen=True).enabled)
            registry.assert_not_called()

    def test_no_helper_install_uninstall(self):
        launch = Mock()
        self.assertFalse(hid_elevation_windows.request_install_elevation(_launch=launch).available)
        self.assertFalse(hid_elevation_windows.request_uninstall_elevation(_launch=launch).available)
        launch.assert_not_called()

    def test_maintenance_modes_never_dispatch(self):
        for flag in ("--request-exit", "--install-hid-helper", "--uninstall-hid-helper", "--background", "--bridge", "--element-navigation"):
            with self.subTest(flag=flag), patch.object(entry.sys, "argv", ["app", flag]), patch.object(entry, "_run_settings") as desktop:
                with self.assertRaises(SystemExit) as result:
                    entry.main()
                self.assertEqual(result.exception.code, entry.INVALID_ARGUMENTS_EXIT_CODE)
                desktop.assert_not_called()

    def test_owned_entry_does_not_migrate_or_register_or_handoff(self):
        guard = Mock()
        guard.__enter__ = Mock()
        guard.__exit__ = Mock(return_value=False)
        with patch.object(single_instance, "ApplicationInstanceGuard", return_value=guard), \
             patch.object(single_instance, "bridge_instance_running", return_value=False), \
             patch.object(settings_ui, "main") as desktop, \
             patch.object(entry, "_register_current_hid_helper_consumer") as register, \
             patch.object(entry, "_migrate_legacy_bridge_before_desktop_start") as migrate, \
             patch.object(entry, "_handoff_previous_application") as handoff:
            entry._run_settings(start_bridge=True)
            desktop.assert_called_once_with(start_bridge=False)
            register.assert_not_called()
            migrate.assert_not_called()
            handoff.assert_not_called()

    def test_duplicate_or_unknown_ownership_does_not_open_or_contact_other_app(self):
        for error in (single_instance.DuplicateInstanceError(), single_instance.SingleInstanceUnavailableError()):
            with self.subTest(error=type(error).__name__), \
                 patch.object(single_instance, "ApplicationInstanceGuard", side_effect=error), \
                 patch.object(single_instance, "show_bridge_startup_blocked_notice"), \
                 patch.object(settings_ui, "main") as desktop, \
                 patch.object(entry, "_handoff_previous_application") as handoff:
                with self.assertRaises(SystemExit):
                    entry._run_settings()
                desktop.assert_not_called()
                handoff.assert_not_called()

    def test_no_legacy_bridge_or_navigation_control(self):
        find, send = Mock(), Mock()
        result = bridge_control_windows.request_bridge_exit(
            platform="win32", stop_internal=lambda **kwargs: None,
            bridge_running=lambda: True, find_window=find, post_exit_command=send)
        self.assertFalse(result.stopped)
        self.assertEqual(navigation.send_element_navigation_command(1, _find_window=find, _send_window_command=send), navigation.CommandSendResult.FAILED)
        find.assert_not_called()
        send.assert_not_called()

    def test_live_legacy_bridge_blocks_entry_before_desktop(self):
        from contextlib import nullcontext
        with patch.object(single_instance, "ApplicationInstanceGuard", return_value=nullcontext()), \
             patch.object(single_instance, "bridge_instance_running", return_value=True), \
             patch.object(single_instance, "show_bridge_startup_blocked_notice"), \
             patch.object(settings_ui, "main") as desktop:
            with self.assertRaises(SystemExit) as result:
                entry._run_settings()
            self.assertEqual(result.exception.code, single_instance.DUPLICATE_INSTANCE_EXIT_CODE)
            desktop.assert_not_called()

    def test_redirected_test_directory_fails_closed(self):
        original_resolve = Path.resolve
        def redirected(path, *args, **kwargs):
            return Path('D:/elsewhere') if path.name == 'isolated-runtime' else original_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected), self.assertRaises(ValueError):
            dev_session.isolated_root()


if __name__ == "__main__":
    unittest.main()
