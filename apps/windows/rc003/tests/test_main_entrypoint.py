"""Argument-mode routing tests for ``ovb_rc003.__main__.main()`` (XRBM-021
In-scope items 3-4). Monkeypatches module-level attributes on the real
``ovb_rc003.app``/``ovb_rc003.single_instance`` modules - the same pattern
test_app_wiring.py already uses for win32_input.py - rather than
constructing a real ``BridgeInstanceGuard``/calling the real
``app.main()``, matching this project's established "never touch real
BLE/HID/audio/Tk in a test" convention. ``main()`` reads ``sys.argv``
internally rather than taking a parameter, so each test temporarily
replaces ``sys.argv`` and restores it in ``finally``.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import __main__ as main_module
from ovb_rc003 import (
    app,
    config,
    element_navigation_runtime,
    frida_compat,
    hid_elevation_windows,
    hid_helper_consumers,
    single_instance,
    windows_diagnostics,
)


class DryRunCoverageTests(unittest.TestCase):
    def test_dry_run_imports_every_top_level_first_party_module_in_a_fresh_process(self):
        src_root = Path(__file__).resolve().parents[1] / "src"
        script = """
import pkgutil
import sys

import ovb_rc003
from ovb_rc003 import __main__ as entrypoint

expected = {item.name for item in pkgutil.iter_modules(ovb_rc003.__path__)}
entrypoint._dry_run()
missing = sorted(
    name for name in expected if f"ovb_rc003.{name}" not in sys.modules
)
if missing:
    print("missing first-party modules: " + ", ".join(missing))
    raise SystemExit(1)
"""
        env = dict(os.environ)
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(src_root), existing_pythonpath) if part
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=src_root.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )


class HidHelperMaintenanceEntrypointTests(unittest.TestCase):
    def test_install_delegates_the_consumer_transaction_before_elevation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(
                config, "config_root", return_value=root
            ), mock.patch.object(
                hid_helper_consumers,
                "install_for_current_consumer",
                return_value=hid_elevation_windows.HidHelperState(True),
            ) as install:
                result = main_module._install_hid_helper()

        self.assertEqual(result, 0)
        install.assert_called_once_with(
            root,
            hid_elevation_windows.request_install_elevation,
        )

    def test_install_failure_returns_nonzero_without_starting_the_desktop(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            config, "config_root", return_value=Path(raw)
        ), mock.patch.object(
            hid_helper_consumers,
            "install_for_current_consumer",
            return_value=hid_elevation_windows.HidHelperState(
                False, "uac_cancelled"
            ),
        ):
            result = main_module._install_hid_helper()

        self.assertEqual(
            result, main_module.HID_HELPER_MAINTENANCE_FAILED_EXIT_CODE
        )

    def test_standard_account_install_returns_the_dedicated_installer_code(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            config, "config_root", return_value=Path(raw)
        ), mock.patch.object(
            hid_helper_consumers,
            "install_for_current_consumer",
            return_value=hid_elevation_windows.HidHelperState(
                False, "current_account_cannot_self_elevate"
            ),
        ):
            result = main_module._install_hid_helper()

        self.assertEqual(
            result, main_module.HID_HELPER_ACCOUNT_UNSUPPORTED_EXIT_CODE
        )

    def test_uninstall_delegates_shared_consumer_preservation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(
                config, "config_root", return_value=root
            ), mock.patch.object(
                hid_helper_consumers,
                "uninstall_current_distribution",
                return_value=hid_elevation_windows.HidHelperState(
                    True, "helper_kept_for_other_consumer"
                ),
            ) as uninstall:
                result = main_module._uninstall_hid_helper()

        self.assertEqual(result, 0)
        uninstall.assert_called_once_with(
            root,
            hid_elevation_windows.request_uninstall_elevation,
        )

    def test_failed_uninstall_restores_the_current_consumer_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(
                config, "config_root", return_value=root
            ), mock.patch.object(
                hid_helper_consumers,
                "uninstall_current_distribution",
                return_value=hid_elevation_windows.HidHelperState(
                    False, "uac_cancelled"
                ),
            ) as uninstall:
                result = main_module._uninstall_hid_helper()

        self.assertEqual(
            result, main_module.HID_HELPER_MAINTENANCE_FAILED_EXIT_CODE
        )
        uninstall.assert_called_once_with(
            root,
            hid_elevation_windows.request_uninstall_elevation,
        )

    def test_standard_account_uninstall_restores_marker_and_returns_dedicated_code(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(
                config, "config_root", return_value=root
            ), mock.patch.object(
                hid_helper_consumers,
                "uninstall_current_distribution",
                return_value=hid_elevation_windows.HidHelperState(
                    False, "current_account_cannot_self_elevate"
                ),
            ) as uninstall:
                result = main_module._uninstall_hid_helper()

        self.assertEqual(
            result, main_module.HID_HELPER_ACCOUNT_UNSUPPORTED_EXIT_CODE
        )
        uninstall.assert_called_once_with(
            root,
            hid_elevation_windows.request_uninstall_elevation,
        )

    def test_hidden_install_mode_never_enters_the_desktop_guards(self):
        original_argv = sys.argv
        sys.argv = ["ovb_rc003", "--install-hid-helper"]
        try:
            with mock.patch.object(
                main_module, "_install_hid_helper", return_value=0
            ) as install, mock.patch.object(
                main_module, "_register_current_hid_helper_consumer"
            ) as register, mock.patch.object(
                single_instance, "ApplicationRuntimeInstanceGuard"
            ) as runtime_guard:
                with self.assertRaises(SystemExit) as ctx:
                    main_module.main()
        finally:
            sys.argv = original_argv

        self.assertEqual(ctx.exception.code, 0)
        install.assert_called_once_with()
        register.assert_not_called()
        runtime_guard.assert_not_called()


def _make_guard_class(*, raise_on_enter=None, enter_calls=None):
    """Build a fake instance-guard class for bridge or settings routing.
    """

    class _ScriptedGuard:
        def __enter__(self):
            if enter_calls is not None:
                enter_calls.append(1)
            if raise_on_enter is not None:
                raise raise_on_enter
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    return _ScriptedGuard


class _ArgvRestoringTestCase(unittest.TestCase):
    def setUp(self):
        self._original_argv = sys.argv
        self._original_application_guard_cls = (
            single_instance.ApplicationInstanceGuard
        )
        self._original_runtime_guard_cls = (
            single_instance.ApplicationRuntimeInstanceGuard
        )
        self._original_activate_settings = (
            single_instance.activate_existing_settings_window
        )
        self._original_activate_current_runtime_settings = (
            single_instance.activate_current_runtime_settings_window
        )
        self._original_app_main = app.main
        self._original_notice = single_instance.show_bridge_startup_blocked_notice
        self._original_bridge_start_request = (
            single_instance.write_bridge_start_request
        )
        self._original_qt_runtime_check = main_module._qt_runtime_check
        self._original_element_navigation_runtime = (
            element_navigation_runtime.run_element_navigation
        )
        self._original_handoff_previous_application = (
            main_module._handoff_previous_application
        )
        # Never let a failure-path test open a real system-modal Win32
        # message box on a developer machine or headless CI runner.
        single_instance.show_bridge_startup_blocked_notice = lambda message: None
        single_instance.ApplicationInstanceGuard = _make_guard_class()
        single_instance.ApplicationRuntimeInstanceGuard = _make_guard_class()
        single_instance.activate_existing_settings_window = lambda: True
        single_instance.activate_current_runtime_settings_window = lambda: True
        single_instance.write_bridge_start_request = lambda _root: None

    def tearDown(self):
        sys.argv = self._original_argv
        single_instance.ApplicationInstanceGuard = (
            self._original_application_guard_cls
        )
        single_instance.ApplicationRuntimeInstanceGuard = (
            self._original_runtime_guard_cls
        )
        single_instance.activate_existing_settings_window = (
            self._original_activate_settings
        )
        single_instance.activate_current_runtime_settings_window = (
            self._original_activate_current_runtime_settings
        )
        app.main = self._original_app_main
        single_instance.show_bridge_startup_blocked_notice = self._original_notice
        single_instance.write_bridge_start_request = (
            self._original_bridge_start_request
        )
        main_module._qt_runtime_check = self._original_qt_runtime_check
        main_module._handoff_previous_application = (
            self._original_handoff_previous_application
        )
        element_navigation_runtime.run_element_navigation = (
            self._original_element_navigation_runtime
        )


class DesktopModeRoutingTests(_ArgvRestoringTestCase):
    def _run_with_settings_spy(self, argv):
        from ovb_rc003 import settings_ui

        calls = []
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: calls.append(kwargs)
        sys.argv = argv
        try:
            main_module.main()
        finally:
            settings_ui.main = original
        return calls

    def test_no_arguments_open_the_single_desktop_application(self):
        enter_calls = []
        single_instance.ApplicationInstanceGuard = _make_guard_class(
            enter_calls=enter_calls
        )

        calls = self._run_with_settings_spy(["ovb_rc003"])

        self.assertEqual(enter_calls, [1])
        self.assertEqual(calls, [{"start_bridge": False}])

    def test_explicit_settings_open_the_same_desktop_application(self):
        calls = self._run_with_settings_spy(["ovb_rc003", "--settings"])

        self.assertEqual(calls, [{"start_bridge": False}])

    def test_background_start_keeps_the_single_application_hidden(self):
        calls = self._run_with_settings_spy(["ovb_rc003", "--background"])

        self.assertEqual(
            calls,
            [{"start_hidden": True, "start_bridge": False}],
        )

    def test_bridge_compatibility_mode_starts_the_same_application_hidden(self):
        calls = self._run_with_settings_spy(["ovb_rc003", "--bridge"])

        self.assertEqual(
            calls,
            [{"start_hidden": True, "start_bridge": True}],
        )

    def test_explicit_settings_wins_when_bridge_flag_is_also_present(self):
        calls = self._run_with_settings_spy(
            ["ovb_rc003", "--bridge", "--settings"]
        )

        self.assertEqual(calls, [{"start_bridge": False}])

    def test_duplicate_visible_launch_activates_the_existing_window(self):
        from ovb_rc003 import settings_ui

        current_activation_calls = []
        fallback_activation_calls = []
        single_instance.ApplicationRuntimeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        main_module._handoff_previous_application = lambda: self.fail(
            "the same runtime must activate, not start a version handoff"
        )
        single_instance.activate_current_runtime_settings_window = (
            lambda: current_activation_calls.append(1) or True
        )
        single_instance.activate_existing_settings_window = (
            lambda: fallback_activation_calls.append(1) or True
        )
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: self.fail(
            "duplicate launch must not build another desktop application"
        )
        sys.argv = ["ovb_rc003", "--settings"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(current_activation_calls, [1])
        self.assertEqual(fallback_activation_calls, [])

    def test_duplicate_visible_launch_reports_when_the_window_cannot_be_activated(self):
        from ovb_rc003 import settings_ui

        current_activation_calls = []
        fallback_activation_calls = []
        notice_calls = []
        single_instance.ApplicationRuntimeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.activate_current_runtime_settings_window = (
            lambda: current_activation_calls.append(1) or False
        )
        single_instance.activate_existing_settings_window = (
            lambda: fallback_activation_calls.append(1) or False
        )
        single_instance.show_bridge_startup_blocked_notice = notice_calls.append
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: self.fail(
            "duplicate launch must not build another desktop application"
        )
        sys.argv = ["ovb_rc003", "--settings"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(current_activation_calls, [1])
        self.assertEqual(fallback_activation_calls, [1])
        self.assertEqual(len(notice_calls), 1)
        self.assertEqual(
            notice_calls[0],
            "无线麦已经在启动或运行。\n\n"
            "请从通知区域打开现有程序。",
        )

    def test_duplicate_visible_launch_during_handoff_only_restores_the_old_window(self):
        from ovb_rc003 import settings_ui

        current_activation_calls = []
        fallback_activation_calls = []
        notice_calls = []
        single_instance.ApplicationRuntimeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("handoff waiter")
        )
        single_instance.activate_current_runtime_settings_window = (
            lambda: current_activation_calls.append(1) or False
        )
        single_instance.activate_existing_settings_window = (
            lambda: fallback_activation_calls.append(1) or True
        )
        single_instance.show_bridge_startup_blocked_notice = notice_calls.append
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: self.fail(
            "a second launch must not create another application"
        )
        sys.argv = ["ovb_rc003", "--settings"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(current_activation_calls, [1])
        self.assertEqual(fallback_activation_calls, [1])
        self.assertEqual(notice_calls, [])

    def test_duplicate_bridge_launch_requests_existing_app_without_popping_window(self):
        from ovb_rc003 import settings_ui

        activation_calls = []
        request_calls = []
        single_instance.ApplicationRuntimeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.activate_existing_settings_window = (
            lambda: activation_calls.append(1) or True
        )
        single_instance.write_bridge_start_request = (
            lambda root, *, session_scoped=False: request_calls.append(
                (root, session_scoped)
            )
        )
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: self.fail(
            "duplicate bridge launch must not create another process"
        )
        sys.argv = ["ovb_rc003", "--bridge"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(request_calls, [(config.config_root(), True)])
        self.assertEqual(activation_calls, [])

    def test_duplicate_bridge_request_failure_is_visible_without_a_second_app(self):
        from ovb_rc003 import settings_ui

        activation_calls = []
        notice_calls = []
        single_instance.ApplicationRuntimeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.activate_existing_settings_window = (
            lambda: activation_calls.append(1) or True
        )
        single_instance.write_bridge_start_request = (
            lambda _root, *, session_scoped=False: (
                (_ for _ in ()).throw(PermissionError("private detail"))
            )
        )
        single_instance.show_bridge_startup_blocked_notice = notice_calls.append
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: self.fail(
            "failed duplicate request must not create another application"
        )
        sys.argv = ["ovb_rc003", "--bridge"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(activation_calls, [])
        self.assertEqual(len(notice_calls), 1)
        self.assertNotIn("private detail", notice_calls[0])

    def test_duplicate_bridge_session_probe_failure_is_visible(self):
        from ovb_rc003 import settings_ui

        notice_calls = []
        single_instance.ApplicationRuntimeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.write_bridge_start_request = (
            lambda _root, *, session_scoped=False: (
                (_ for _ in ()).throw(
                    single_instance.SingleInstanceUnavailableError(
                        "session unavailable"
                    )
                )
            )
        )
        single_instance.show_bridge_startup_blocked_notice = notice_calls.append
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: self.fail(
            "failed duplicate request must not create another application"
        )
        sys.argv = ["ovb_rc003", "--bridge"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(len(notice_calls), 1)
        self.assertNotIn("session unavailable", notice_calls[0])

    def test_duplicate_background_start_does_not_pop_the_window_open(self):
        from ovb_rc003 import settings_ui

        activation_calls = []
        single_instance.ApplicationRuntimeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.activate_existing_settings_window = (
            lambda: activation_calls.append(1) or True
        )
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: self.fail(
            "duplicate background start must not build another application"
        )
        sys.argv = ["ovb_rc003", "--background"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(activation_calls, [])

    def test_different_runtime_visible_launch_waits_then_starts_current_copy(self):
        from ovb_rc003 import settings_ui

        enter_calls = []

        class _FirstDuplicateThenOwner:
            def __enter__(self):
                enter_calls.append(1)
                if len(enter_calls) == 1:
                    raise single_instance.DuplicateInstanceError("old copy")
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        handoff_calls = []
        settings_calls = []
        single_instance.ApplicationInstanceGuard = _FirstDuplicateThenOwner
        main_module._handoff_previous_application = (
            lambda: handoff_calls.append(1) or True
        )
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: settings_calls.append(kwargs)
        sys.argv = ["ovb_rc003"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(enter_calls, [1, 1])
        self.assertEqual(handoff_calls, [1])
        self.assertEqual(settings_calls, [{"start_bridge": False}])

    def test_different_runtime_visible_launch_does_not_start_when_handoff_is_cancelled(self):
        from ovb_rc003 import settings_ui

        single_instance.ApplicationInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("old copy")
        )
        handoff_calls = []
        main_module._handoff_previous_application = (
            lambda: handoff_calls.append(1) or False
        )
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: self.fail(
            "cancelled handoff must not start the current application"
        )
        sys.argv = ["ovb_rc003"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(handoff_calls, [1])

    def test_guard_failure_is_visible_and_never_starts_the_application(self):
        from ovb_rc003 import settings_ui

        notice_calls = []
        single_instance.ApplicationInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.SingleInstanceUnavailableError(
                "private detail"
            )
        )
        single_instance.show_bridge_startup_blocked_notice = notice_calls.append
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: self.fail(
            "application must not start without its instance guard"
        )
        sys.argv = ["ovb_rc003"]
        try:
            with self.assertRaises(SystemExit) as ctx:
                main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(
            ctx.exception.code,
            main_module.SETTINGS_STARTUP_FAILED_EXIT_CODE,
        )
        self.assertEqual(len(notice_calls), 1)
        self.assertNotIn("private detail", notice_calls[0])

    def test_settings_startup_failure_is_visible_and_sanitized(self):
        from ovb_rc003 import settings_ui

        notice_calls = []
        single_instance.show_bridge_startup_blocked_notice = notice_calls.append
        original = settings_ui.main
        settings_ui.main = lambda **kwargs: (_ for _ in ()).throw(
            ValueError("private detail")
        )
        sys.argv = ["ovb_rc003", "--settings"]
        try:
            with self.assertRaises(SystemExit) as ctx:
                main_module.main()
        finally:
            settings_ui.main = original

        self.assertEqual(
            ctx.exception.code,
            main_module.SETTINGS_STARTUP_FAILED_EXIT_CODE,
        )
        self.assertEqual(len(notice_calls), 1)
        self.assertNotIn("private detail", notice_calls[0])

    def test_frozen_desktop_refuses_to_run_elevated(self):
        from ovb_rc003 import settings_ui

        notices = []
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            hid_elevation_windows,
            "query_process_elevated",
            return_value=True,
        ), mock.patch.object(
            single_instance,
            "show_bridge_startup_blocked_notice",
            side_effect=notices.append,
        ), mock.patch.object(
            settings_ui,
            "main",
        ) as settings_main:
            with self.assertRaises(SystemExit) as ctx:
                main_module._run_settings()

        self.assertEqual(
            ctx.exception.code,
            main_module.ELEVATED_DESKTOP_UNSUPPORTED_EXIT_CODE,
        )
        settings_main.assert_not_called()
        self.assertEqual(len(notices), 1)
        self.assertIn("不能以管理员身份长期运行", notices[0])

    def test_frozen_desktop_refuses_to_start_when_elevation_is_unknown(self):
        from ovb_rc003 import settings_ui

        notices = []
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            hid_elevation_windows,
            "query_process_elevated",
            side_effect=hid_elevation_windows.HidElevationError("private detail"),
        ), mock.patch.object(
            single_instance,
            "installer_maintenance_running",
        ) as maintenance, mock.patch.object(
            single_instance,
            "show_bridge_startup_blocked_notice",
            side_effect=notices.append,
        ), mock.patch.object(
            settings_ui,
            "main",
        ) as settings_main:
            with self.assertRaises(SystemExit) as ctx:
                main_module._run_settings()

        self.assertEqual(
            ctx.exception.code,
            main_module.ELEVATED_DESKTOP_UNSUPPORTED_EXIT_CODE,
        )
        maintenance.assert_not_called()
        settings_main.assert_not_called()
        self.assertEqual(len(notices), 1)
        self.assertIn("无法确认无线麦当前的权限状态", notices[0])
        self.assertNotIn("private detail", notices[0])

    def test_frozen_desktop_does_not_start_during_installer_maintenance(self):
        from ovb_rc003 import settings_ui

        notices = []
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            hid_elevation_windows,
            "query_process_elevated",
            return_value=False,
        ), mock.patch.object(
            single_instance,
            "installer_maintenance_running",
            return_value=True,
        ), mock.patch.object(
            single_instance,
            "show_bridge_startup_blocked_notice",
            side_effect=notices.append,
        ), mock.patch.object(
            settings_ui,
            "main",
        ) as settings_main:
            with self.assertRaises(SystemExit) as ctx:
                main_module._run_settings()

        self.assertEqual(
            ctx.exception.code,
            main_module.INSTALLER_MAINTENANCE_ACTIVE_EXIT_CODE,
        )
        settings_main.assert_not_called()
        self.assertEqual(notices, ["无线麦正在安装或卸载。完成后再打开。"])


class ArgumentModeBypassTests(_ArgvRestoringTestCase):
    def _assert_application_guard_unused(self):
        enter_calls = []
        single_instance.ApplicationInstanceGuard = _make_guard_class(
            enter_calls=enter_calls
        )
        return enter_calls

    def test_dry_run_never_touches_the_guard(self):
        enter_calls = self._assert_application_guard_unused()
        sys.argv = ["ovb_rc003", "--dry-run"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(enter_calls, [])

    def test_qt_runtime_check_never_touches_the_guard(self):
        enter_calls = self._assert_application_guard_unused()
        check_calls = []
        main_module._qt_runtime_check = lambda: check_calls.append(1) or 0
        sys.argv = ["ovb_rc003", "--qt-runtime-check"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(check_calls, [1])
        self.assertEqual(enter_calls, [])

    def test_application_exit_request_never_enters_the_application_guard(self):
        enter_calls = self._assert_application_guard_unused()
        request_calls = []
        original = main_module._request_application_exit
        main_module._request_application_exit = (
            lambda: request_calls.append(1) or 7
        )
        sys.argv = ["ovb_rc003", "--request-exit"]
        try:
            with self.assertRaises(SystemExit) as ctx:
                main_module.main()
        finally:
            main_module._request_application_exit = original

        self.assertEqual(ctx.exception.code, 7)
        self.assertEqual(request_calls, [1])
        self.assertEqual(enter_calls, [])

    def test_help_never_touches_the_guard(self):
        enter_calls = self._assert_application_guard_unused()
        sys.argv = ["ovb_rc003", "--help"]

        main_module.main()

        self.assertEqual(enter_calls, [])

    def test_version_never_touches_the_guard_or_opens_the_application(self):
        import contextlib
        import io

        enter_calls = self._assert_application_guard_unused()
        output = io.StringIO()
        original_run_settings = main_module._run_settings
        main_module._run_settings = lambda **_kwargs: self.fail(
            "--version must not open the desktop application"
        )
        sys.argv = ["ovb_rc003", "--version"]
        try:
            with contextlib.redirect_stdout(output):
                main_module.main()
        finally:
            main_module._run_settings = original_run_settings

        self.assertEqual(output.getvalue().strip(), main_module.__version__)
        self.assertEqual(enter_calls, [])

    def test_unknown_argument_fails_closed_before_desktop_startup(self):
        import contextlib
        import io

        enter_calls = self._assert_application_guard_unused()
        errors = io.StringIO()
        original_register = main_module._register_current_hid_helper_consumer
        original_run_settings = main_module._run_settings
        main_module._register_current_hid_helper_consumer = lambda: self.fail(
            "unknown arguments must not register a distribution"
        )
        main_module._run_settings = lambda **_kwargs: self.fail(
            "unknown arguments must not open the desktop application"
        )
        sys.argv = ["ovb_rc003", "--version-check"]
        try:
            with contextlib.redirect_stderr(errors), self.assertRaises(
                SystemExit
            ) as ctx:
                main_module.main()
        finally:
            main_module._register_current_hid_helper_consumer = original_register
            main_module._run_settings = original_run_settings

        self.assertEqual(ctx.exception.code, main_module.INVALID_ARGUMENTS_EXIT_CODE)
        self.assertIn("--version-check", errors.getvalue())
        self.assertEqual(enter_calls, [])

    def test_diagnose_ble_candidates_never_touches_the_guard(self):
        enter_calls = self._assert_application_guard_unused()
        sys.argv = ["ovb_rc003", "--diagnose-ble-candidates", "/tmp/result.json"]

        original = windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint
        windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint = (
            lambda result_path: 0
        )
        try:
            with self.assertRaises(SystemExit):
                main_module.main()
        finally:
            windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint = original

        self.assertEqual(enter_calls, [])

    def test_vb_cable_loopback_child_never_touches_the_guard(self):
        enter_calls = self._assert_application_guard_unused()
        sys.argv = [
            "ovb_rc003",
            "--diagnose-vb-cable-loopback",
            "/tmp/request.json",
            "/tmp/result.json",
        ]

        original = (
            windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint
        )
        windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint = (
            lambda request_path, result_path: 0
        )
        try:
            with self.assertRaises(SystemExit):
                main_module.main()
        finally:
            windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint = original

        self.assertEqual(enter_calls, [])

    def test_hid_injector_child_never_touches_the_guard(self):
        enter_calls = self._assert_application_guard_unused()
        received_args = []
        original = frida_compat.injector_main
        frida_compat.injector_main = lambda args: received_args.append(args) or 4
        sys.argv = [
            "ovb_rc003",
            frida_compat.HID_TAP_INJECTOR_FLAG,
            "--pid",
            "321",
        ]
        try:
            with self.assertRaises(SystemExit) as ctx:
                main_module.main()
        finally:
            frida_compat.injector_main = original

        self.assertEqual(ctx.exception.code, 4)
        self.assertEqual(received_args, [["--pid", "321"]])
        self.assertEqual(enter_calls, [])




class ElementNavigationDispatchTests(_ArgvRestoringTestCase):
    def test_hidden_entrypoint_passes_arguments_and_propagates_exit_code(self):
        calls = []
        element_navigation_runtime.run_element_navigation = (
            lambda arguments: calls.append(list(arguments)) or 7
        )
        sys.argv = [
            "ovb_rc003",
            "--element-navigation",
            "--activate",
            "--window-handle",
            "321",
        ]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(ctx.exception.code, 7)
        self.assertEqual(calls, [["--activate", "--window-handle", "321"]])

    def test_runtime_failure_is_sanitized_and_never_falls_through(self):
        import contextlib
        import io

        element_navigation_runtime.run_element_navigation = lambda _arguments: (
            (_ for _ in ()).throw(RuntimeError("private navigation detail"))
        )
        app.main = lambda: self.fail("navigation failure must not start the bridge")
        sys.argv = ["ovb_rc003", "--element-navigation"]
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(
            ctx.exception.code,
            main_module.ELEMENT_NAVIGATION_RUNTIME_FAILED_EXIT_CODE,
        )
        self.assertIn("RuntimeError", stderr.getvalue())
        self.assertNotIn("private navigation detail", stderr.getvalue())


class DiagnoseBleCandidatesDispatchTests(_ArgvRestoringTestCase):
    """XRBM-035 RETRY 1 In-scope item 6: the hidden child-process entry
    point dispatch - fail-closed on a missing result path, never falls
    through to _run_bridge(), and stays absent from the public --help
    surface.
    """

    def setUp(self):
        super().setUp()
        self._original_entrypoint = windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint

    def tearDown(self):
        windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint = self._original_entrypoint
        super().tearDown()

    def test_dispatches_with_the_result_path_argument_and_propagates_its_exit_code(self):
        received_paths = []
        windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint = (
            lambda result_path: received_paths.append(result_path) or 7
        )
        app.main = lambda: self.fail("must never call app.main()")
        sys.argv = ["ovb_rc003", "--diagnose-ble-candidates", "/tmp/result-path.json"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(received_paths, ["/tmp/result-path.json"])
        self.assertEqual(ctx.exception.code, 7)

    def test_missing_result_path_argument_passes_none_through_fail_closed(self):
        # __main__.py itself never guesses a fallback path or falls through
        # to _run_bridge() - it is run_ble_diagnostics_subprocess_
        # entrypoint()'s own job to fail closed on None (see
        # windows_diagnostics.py's own tests for that contract).
        received_paths = []
        windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint = (
            lambda result_path: received_paths.append(result_path) or 1
        )
        app.main = lambda: self.fail("must never call app.main()")
        sys.argv = ["ovb_rc003", "--diagnose-ble-candidates"]  # no path follows the flag

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(received_paths, [None])
        self.assertEqual(ctx.exception.code, 1)

    def test_flag_constant_stays_in_sync_with_windows_diagnostics_module(self):
        # __main__.py's own argv dispatch uses a literal string (kept that
        # way deliberately - see __main__.py's own comment - rather than
        # eagerly importing windows_diagnostics at module level just for
        # this one check, which would add sounddevice/numpy/winrt to every
        # --help/bare invocation's import graph). This regression test is
        # what keeps that literal from silently drifting out of sync with
        # the module that actually owns the IPC contract.
        import inspect

        source = inspect.getsource(main_module)
        self.assertIn(
            f'"{windows_diagnostics.BLE_DIAGNOSTICS_SUBPROCESS_FLAG}" in args', source
        )

    def test_help_text_never_mentions_the_hidden_diagnostics_flag(self):
        # XRBM-035 RETRY 1 In-scope item 6: not part of this program's
        # public CLI surface.
        import io
        import contextlib

        sys.argv = ["ovb_rc003", "--help"]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main_module.main()  # returns normally, no SystemExit

        self.assertNotIn("--diagnose-ble-candidates", buffer.getvalue())
        self.assertNotIn("--diagnose-vb-cable-loopback", buffer.getvalue())
        self.assertNotIn("--preflight-output-endpoint", buffer.getvalue())
        self.assertNotIn("--request-exit", buffer.getvalue())
        self.assertNotIn("--on-request-probe", buffer.getvalue())


class ApplicationExitRequestEntrypointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._original_config_root = config.config_root
        self._original_running = single_instance.application_instance_running
        self._original_write = single_instance.write_application_exit_request
        self._original_exit_guard = single_instance.ApplicationExitRequestGuard
        self._original_capability = single_instance.application_exit_request_capability
        self._original_session_id = single_instance.current_process_session_id
        config.config_root = lambda: self.root
        single_instance.ApplicationExitRequestGuard = _make_guard_class()
        single_instance.application_exit_request_capability = lambda: (
            single_instance.ApplicationExitRequestCapability.SESSION_SUPPORTED
        )
        single_instance.current_process_session_id = lambda: 7

    def tearDown(self):
        config.config_root = self._original_config_root
        single_instance.application_instance_running = self._original_running
        single_instance.write_application_exit_request = self._original_write
        single_instance.ApplicationExitRequestGuard = self._original_exit_guard
        single_instance.application_exit_request_capability = self._original_capability
        single_instance.current_process_session_id = self._original_session_id
        self._tmp.cleanup()

    def test_already_stopped_returns_success_without_deleting_an_unowned_request(self):
        request_path = single_instance.application_exit_request_path(self.root)
        request_path.write_text("stale", encoding="utf-8")
        write_calls = []
        single_instance.application_instance_running = lambda: False
        single_instance.write_application_exit_request = (
            lambda _root, **_kwargs: write_calls.append(1)
        )

        result = main_module._request_application_exit()

        self.assertEqual(result, 0)
        self.assertEqual(write_calls, [])
        self.assertTrue(request_path.exists())

    def test_running_application_gets_one_request_and_is_waited_out(self):
        states = iter((True, True, False))
        writes = []
        sleeps = []
        single_instance.application_instance_running = lambda: next(states)
        single_instance.write_application_exit_request = (
            lambda root, **kwargs: writes.append((root, kwargs))
        )

        result = main_module._request_application_exit(
            monotonic=iter((0.0, 0.01)).__next__,
            sleep=sleeps.append,
        )

        self.assertEqual(result, 0)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], self.root)
        self.assertTrue(writes[0][1]["session_scoped"])
        self.assertTrue(writes[0][1]["request_id"])
        self.assertEqual(sleeps, [main_module.APPLICATION_EXIT_REQUEST_POLL_SECONDS])

    def test_timeout_is_nonzero_and_never_starts_application_resources(self):
        single_instance.application_instance_running = lambda: True
        single_instance.write_application_exit_request = (
            lambda _root, **_kwargs: None
        )

        result = main_module._request_application_exit(
            timeout=0.1,
            monotonic=iter((0.0, 1.0)).__next__,
            sleep=lambda _seconds: self.fail("timeout must be checked before sleep"),
        )

        self.assertEqual(
            result,
            main_module.APPLICATION_EXIT_REQUEST_TIMEOUT_EXIT_CODE,
        )

    def test_capability_probe_failure_returns_the_stable_failure_code(self):
        single_instance.application_instance_running = lambda: True
        single_instance.application_exit_request_capability = lambda: (
            (_ for _ in ()).throw(OSError("probe failed"))
        )

        result = main_module._request_application_exit()

        self.assertEqual(
            result,
            main_module.APPLICATION_EXIT_REQUEST_FAILED_EXIT_CODE,
        )

    def test_rejection_probe_failure_returns_the_stable_failure_code(self):
        single_instance.application_instance_running = lambda: True
        single_instance.write_application_exit_request = (
            lambda _root, **_kwargs: None
        )

        with mock.patch.object(
            single_instance,
            "application_exit_request_rejected",
            side_effect=OSError("probe failed"),
        ):
            result = main_module._request_application_exit(
                monotonic=iter((0.0, 0.01)).__next__,
                sleep=lambda _seconds: self.fail(
                    "a failed response probe must stop immediately"
                ),
            )

        self.assertEqual(
            result,
            main_module.APPLICATION_EXIT_REQUEST_FAILED_EXIT_CODE,
        )


class ApplicationHandoffTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._original_config_root = config.config_root
        self._original_confirm = single_instance.confirm_application_handoff
        self._original_handoff_guard = (
            single_instance.ApplicationHandoffInstanceGuard
        )
        self._original_exit_guard = single_instance.ApplicationExitRequestGuard
        self._original_session_id = single_instance.current_process_session_id
        self._original_activate = single_instance.activate_existing_settings_window
        self._original_running = single_instance.application_instance_running
        self._original_capability = (
            single_instance.application_exit_request_capability
        )
        self._original_write = single_instance.write_application_exit_request
        self._original_acknowledged = (
            single_instance.application_exit_request_acknowledged
        )
        self._original_rejected = single_instance.application_exit_request_rejected
        self._original_clear = (
            single_instance.clear_owned_application_exit_request
        )
        self._original_clear_ack = (
            single_instance.clear_owned_application_exit_acknowledgement
        )
        self._original_clear_response = (
            single_instance.clear_owned_application_exit_response
        )
        self._original_notice = single_instance.show_bridge_startup_blocked_notice
        config.config_root = lambda: self.root
        single_instance.ApplicationHandoffInstanceGuard = _make_guard_class()
        single_instance.ApplicationExitRequestGuard = _make_guard_class()
        single_instance.current_process_session_id = lambda: 7
        single_instance.show_bridge_startup_blocked_notice = lambda _message: None
        single_instance.application_exit_request_capability = lambda: (
            single_instance.ApplicationExitRequestCapability.SESSION_SUPPORTED
        )

    def tearDown(self):
        config.config_root = self._original_config_root
        single_instance.confirm_application_handoff = self._original_confirm
        single_instance.ApplicationHandoffInstanceGuard = (
            self._original_handoff_guard
        )
        single_instance.ApplicationExitRequestGuard = self._original_exit_guard
        single_instance.current_process_session_id = self._original_session_id
        single_instance.activate_existing_settings_window = self._original_activate
        single_instance.application_instance_running = self._original_running
        single_instance.application_exit_request_capability = (
            self._original_capability
        )
        single_instance.write_application_exit_request = self._original_write
        single_instance.application_exit_request_acknowledged = (
            self._original_acknowledged
        )
        single_instance.application_exit_request_rejected = self._original_rejected
        single_instance.clear_owned_application_exit_request = self._original_clear
        single_instance.clear_owned_application_exit_acknowledgement = (
            self._original_clear_ack
        )
        single_instance.clear_owned_application_exit_response = (
            self._original_clear_response
        )
        single_instance.show_bridge_startup_blocked_notice = self._original_notice
        self._tmp.cleanup()

    def test_cancel_keeps_the_old_copy_without_sending_an_exit_request(self):
        single_instance.confirm_application_handoff = lambda _version: False
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "the handoff prompt must not wake the old window"
        )
        single_instance.write_application_exit_request = (
            lambda _root: self.fail("cancel must not write an exit request")
        )
        single_instance.application_instance_running = lambda: True

        self.assertFalse(main_module._handoff_previous_application())

    def test_request_capable_copy_is_closed_automatically(self):
        request_path = single_instance.application_exit_request_path(self.root)
        notices = []
        sleeps = []
        running_calls = 0

        def running():
            nonlocal running_calls
            running_calls += 1
            if running_calls == 1:
                return True
            if running_calls == 2:
                single_instance.consume_and_acknowledge_application_exit_request(
                    self.root
                )
                return True
            return False

        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "automatic handoff must not wake the old window"
        )
        single_instance.application_instance_running = running
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 0.01)).__next__,
            sleep=sleeps.append,
        )

        self.assertTrue(result)
        self.assertEqual(notices, [])
        self.assertEqual(sleeps, [main_module.APPLICATION_HANDOFF_POLL_SECONDS])
        self.assertFalse(request_path.exists())

    def test_starting_session_supported_copy_can_publish_capability_after_two_seconds(self):
        capabilities = iter(
            (
                single_instance.ApplicationExitRequestCapability.UNKNOWN,
                single_instance.ApplicationExitRequestCapability.UNKNOWN,
                single_instance.ApplicationExitRequestCapability.SESSION_SUPPORTED,
            )
        )
        running_states = iter((True, True, True, False))
        confirmations = []
        notices = []
        sleeps = []
        single_instance.application_exit_request_capability = capabilities.__next__
        single_instance.application_instance_running = running_states.__next__
        single_instance.confirm_application_handoff = (
            lambda version: confirmations.append(version) or True
        )
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 3.0, 6.0, 6.1)).__next__,
            sleep=sleeps.append,
        )

        self.assertTrue(result)
        self.assertEqual(confirmations, [main_module.__version__])
        self.assertEqual(notices, [])
        self.assertEqual(
            sleeps,
            [
                main_module.APPLICATION_HANDOFF_POLL_SECONDS,
                main_module.APPLICATION_HANDOFF_POLL_SECONDS,
            ],
        )
        self.assertEqual(list(self.root.rglob("*.request.json")), [])

    def test_current_copy_exit_cancellation_stops_the_wait_immediately(self):
        notices = []
        running_calls = 0

        def running():
            nonlocal running_calls
            running_calls += 1
            if running_calls == 2:
                request = single_instance.consume_and_acknowledge_application_exit_request(
                    self.root,
                    session_id=7,
                )
                self.assertIsNotNone(request)
                single_instance.write_application_exit_rejection(
                    self.root,
                    request.request_id,
                    session_id=7,
                    session_scoped=True,
                )
            return True

        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.application_instance_running = running
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 0.1)).__next__,
            sleep=lambda _seconds: self.fail("a rejection must stop without sleeping"),
        )

        self.assertFalse(result)
        self.assertEqual(notices, [main_module.APPLICATION_HANDOFF_REJECTED_EXIT_NOTICE])
        self.assertEqual(list(self.root.rglob("*.response.json")), [])

    def test_candidate_007_gets_immediate_manual_exit_guidance(self):
        notices = []
        states = iter((True, False))
        single_instance.application_exit_request_capability = lambda: (
            single_instance.ApplicationExitRequestCapability.LEGACY_SUPPORTED
        )
        single_instance.confirm_application_handoff = lambda _version: self.fail(
            "candidate 007 must not offer an unsafe automatic exit"
        )
        single_instance.write_application_exit_request = (
            lambda _root, **_kwargs: self.fail(
                "candidate 007 must not receive a shared exit request"
            )
        )
        single_instance.application_instance_running = states.__next__
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application()

        self.assertTrue(result)
        self.assertEqual(notices, [main_module.APPLICATION_HANDOFF_LEGACY_EXIT_NOTICE])

    def test_truly_unsupported_copy_gets_manual_guidance_without_a_false_prompt(self):
        notices = []
        states = iter((True, True, False))
        single_instance.application_exit_request_capability = lambda: (
            single_instance.ApplicationExitRequestCapability.UNSUPPORTED
        )
        single_instance.confirm_application_handoff = lambda _version: self.fail(
            "an unsupported old copy must not offer automatic exit"
        )
        single_instance.write_application_exit_request = (
            lambda _root, *, request_id: self.fail(
                f"legacy copy must not receive request {request_id}"
            )
        )
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "manual guidance must not wake the old window"
        )
        single_instance.application_instance_running = lambda: next(states)
        single_instance.show_bridge_startup_blocked_notice = notices.append

        self.assertTrue(
            main_module._handoff_previous_application(
                monotonic=iter((0.0, 6.0)).__next__,
                sleep=lambda _seconds: None,
            )
        )
        self.assertEqual(notices, [main_module.APPLICATION_HANDOFF_LEGACY_EXIT_NOTICE])

    def test_candidate_008_requires_manual_exit_without_writing_a_shared_request(self):
        request_path = single_instance.application_exit_request_path(self.root)
        notices = []
        states = iter((True, True, False))
        single_instance.application_exit_request_capability = lambda: (
            single_instance.ApplicationExitRequestCapability.SUPPORTED
        )
        single_instance.confirm_application_handoff = lambda _version: self.fail(
            "candidate 008 must not offer an unsafe automatic exit"
        )
        single_instance.write_application_exit_request = (
            lambda _root, **_kwargs: self.fail(
                "candidate 008 must not receive a shared exit request"
            )
        )
        single_instance.application_instance_running = states.__next__
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 6.0)).__next__,
            sleep=lambda _seconds: None,
        )

        self.assertTrue(result)
        self.assertEqual(notices, [main_module.APPLICATION_HANDOFF_LEGACY_EXIT_NOTICE])
        self.assertFalse(request_path.exists())

    def test_confirm_requests_normal_exit_waits_and_clears_the_request(self):
        request_path = single_instance.application_exit_request_path(self.root)
        sleeps = []
        states = iter((True, True, False))
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "automatic handoff must not wake the old window"
        )
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.application_instance_running = lambda: next(states)
        single_instance.application_exit_request_acknowledged = (
            lambda root, request_id, *, session_scoped=False: (
                single_instance.consume_and_acknowledge_application_exit_request(
                    root,
                    session_id=7,
                )
                and self._original_acknowledged(
                    root,
                    request_id,
                    session_id=7,
                    session_scoped=session_scoped,
                )
            )
        )

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 0.01)).__next__,
            sleep=sleeps.append,
        )

        self.assertTrue(result)
        self.assertEqual(sleeps, [main_module.APPLICATION_HANDOFF_POLL_SECONDS])
        self.assertFalse(request_path.exists())

    def test_legacy_copy_can_be_closed_manually_when_request_write_fails(self):
        notices = []
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "manual guidance must not wake the old window"
        )
        states = iter((True, False))
        single_instance.write_application_exit_request = (
            lambda _root, *, request_id, session_scoped=False: (_ for _ in ()).throw(
                PermissionError("blocked")
            )
        )
        single_instance.application_instance_running = lambda: next(states)
        single_instance.show_bridge_startup_blocked_notice = notices.append

        self.assertTrue(main_module._handoff_previous_application())
        self.assertEqual(notices, [main_module.APPLICATION_HANDOFF_MANUAL_EXIT_NOTICE])

    def test_legacy_copy_can_exit_manually_without_consuming_the_request(self):
        request_path = single_instance.application_exit_request_path(self.root)
        states = iter((True, False))
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: True
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.application_instance_running = lambda: next(states)

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 0.01)).__next__,
            sleep=lambda _seconds: None,
        )

        self.assertTrue(result)
        self.assertFalse(request_path.exists())

    def test_old_copy_that_exits_immediately_needs_no_extra_notice(self):
        request_path = single_instance.application_exit_request_path(self.root)
        notices = []
        states = iter((True, False))
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "automatic handoff must not wake the old window"
        )
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.application_instance_running = lambda: next(states)
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application()

        self.assertTrue(result)
        self.assertEqual(notices, [])
        self.assertFalse(request_path.exists())

    def test_consumed_exit_request_stays_silent_while_old_cleanup_finishes(self):
        request_path = single_instance.application_exit_request_path(self.root)
        notices = []
        sleeps = []
        running_calls = 0

        def running():
            nonlocal running_calls
            running_calls += 1
            if running_calls == 1:
                return True
            if running_calls == 2:
                single_instance.consume_and_acknowledge_application_exit_request(
                    self.root
                )
                return True
            return False

        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "automatic handoff must not wake the old window"
        )
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.application_instance_running = running
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 6.0)).__next__,
            sleep=sleeps.append,
        )

        self.assertTrue(result)
        self.assertEqual(notices, [])
        self.assertEqual(sleeps, [main_module.APPLICATION_HANDOFF_POLL_SECONDS])

    def test_acknowledged_exit_can_wait_longer_than_the_old_45_second_limit(self):
        notices = []
        sleeps = []
        states = iter((True, True, True, False))
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.application_instance_running = states.__next__
        single_instance.application_exit_request_acknowledged = (
            lambda *_args, **_kwargs: True
        )
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 1.0, 60.0)).__next__,
            sleep=sleeps.append,
        )

        self.assertTrue(result)
        self.assertEqual(notices, [])
        self.assertEqual(
            sleeps,
            [main_module.APPLICATION_HANDOFF_POLL_SECONDS] * 2,
        )

    def test_handoff_status_probe_failures_abort_without_an_unhandled_error(self):
        for failing_probe in (
            "application_exit_request_rejected",
            "application_exit_request_acknowledged",
            "owned_application_exit_request_pending",
        ):
            with self.subTest(failing_probe=failing_probe):
                notices = []
                single_instance.confirm_application_handoff = lambda _version: True
                single_instance.application_instance_running = lambda: True
                single_instance.write_application_exit_request = (
                    lambda root, *, request_id, session_scoped=False: (
                        self._original_write(
                            root,
                            request_id=request_id,
                            session_id=7,
                            session_scoped=session_scoped,
                        )
                    )
                )
                probes = {
                    "application_exit_request_rejected": lambda *_args, **_kwargs: False,
                    "application_exit_request_acknowledged": lambda *_args, **_kwargs: False,
                    "owned_application_exit_request_pending": lambda *_args, **_kwargs: True,
                }
                probes[failing_probe] = lambda *_args, **_kwargs: (
                    (_ for _ in ()).throw(OSError("probe failed"))
                )
                single_instance.show_bridge_startup_blocked_notice = notices.append

                with mock.patch.multiple(single_instance, **probes):
                    result = main_module._handoff_previous_application(
                        monotonic=iter((0.0, 0.01)).__next__,
                        sleep=lambda _seconds: self.fail(
                            "a failed handoff probe must stop immediately"
                        ),
                    )

                self.assertFalse(result)
                self.assertEqual(len(notices), 1)
                self.assertIn("无法确认旧版", notices[0])

    def test_unacknowledged_old_copy_gets_manual_exit_notice_only_while_running(self):
        notices = []
        sleeps = []
        states = iter((True, True, False))
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "automatic handoff must not wake the old window"
        )
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.application_instance_running = lambda: next(states)
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 6.0)).__next__,
            sleep=sleeps.append,
        )

        self.assertTrue(result)
        self.assertEqual(len(notices), 1)
        self.assertEqual(
            notices,
            [main_module.APPLICATION_HANDOFF_MANUAL_EXIT_NOTICE],
        )
        self.assertEqual(sleeps, [main_module.APPLICATION_HANDOFF_POLL_SECONDS])

    def test_unconsumed_request_prompts_after_old_window_signal(self):
        notices = []
        sleeps = []
        states = iter((True, True, False))
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "automatic handoff must not wake the old window"
        )
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.application_instance_running = lambda: next(states)
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 6.0)).__next__,
            sleep=sleeps.append,
        )

        self.assertTrue(result)
        self.assertEqual(
            notices,
            [main_module.APPLICATION_HANDOFF_MANUAL_EXIT_NOTICE],
        )
        self.assertEqual(sleeps, [main_module.APPLICATION_HANDOFF_POLL_SECONDS])

    def test_request_write_failure_prompts_while_old_copy_still_runs(self):
        notices = []
        sleeps = []
        states = iter((True, True, False))
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "automatic handoff must not wake the old window"
        )
        single_instance.write_application_exit_request = (
            lambda _root, *, request_id, session_scoped=False: (_ for _ in ()).throw(
                PermissionError("blocked")
            )
        )
        single_instance.application_instance_running = lambda: next(states)
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            monotonic=iter((0.0, 0.01)).__next__,
            sleep=sleeps.append,
        )

        self.assertTrue(result)
        self.assertEqual(
            notices,
            [main_module.APPLICATION_HANDOFF_MANUAL_EXIT_NOTICE],
        )
        self.assertEqual(sleeps, [main_module.APPLICATION_HANDOFF_POLL_SECONDS])

    def test_timeout_leaves_old_copy_running_and_removes_our_stale_request(self):
        notices = []
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: True
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.application_instance_running = lambda: True
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            timeout=0.1,
            monotonic=iter((0.0, 1.0)).__next__,
            sleep=lambda _seconds: self.fail("timeout must be checked before sleep"),
        )

        self.assertFalse(result)
        self.assertEqual(len(notices), 1)
        self.assertIn("旧版仍在运行", notices[0])
        self.assertEqual(list(self.root.rglob("*.request.json")), [])

    def test_cleanup_failure_blocks_the_new_copy_from_starting(self):
        request_paths = []
        notices = []
        states = iter((True, False))
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: True
        def write_request(root, *, request_id, session_scoped=False):
            path = self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
            request_paths.append(path)
            return path

        single_instance.write_application_exit_request = write_request
        single_instance.clear_owned_application_exit_request = (
            lambda _root, _request_id, **_kwargs: False
        )
        single_instance.application_instance_running = lambda: next(states)
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application()

        self.assertFalse(result)
        self.assertEqual(len(request_paths), 1)
        self.assertTrue(request_paths[0].exists())
        self.assertEqual(len(notices), 1)
        self.assertIn("切换请求未能安全清理", notices[0])

    def test_only_one_cross_version_handoff_waiter_is_allowed(self):
        notices = []
        single_instance.ApplicationHandoffInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("handoff busy")
        )
        single_instance.confirm_application_handoff = (
            lambda _version: self.fail("a second waiter must not prompt again")
        )
        single_instance.activate_existing_settings_window = lambda: self.fail(
            "a second handoff waiter must not wake the old window"
        )
        single_instance.show_bridge_startup_blocked_notice = notices.append

        self.assertFalse(main_module._handoff_previous_application())
        self.assertEqual(len(notices), 1)
        self.assertIn("正在切换无线麦版本", notices[0])

    def test_timeout_message_does_not_claim_the_old_copy_exited_when_cleanup_fails(self):
        notices = []
        single_instance.confirm_application_handoff = lambda _version: True
        single_instance.activate_existing_settings_window = lambda: True
        single_instance.write_application_exit_request = (
            lambda root, *, request_id, session_scoped=False: self._original_write(
                root,
                request_id=request_id,
                session_id=7,
                session_scoped=session_scoped,
            )
        )
        single_instance.clear_owned_application_exit_request = (
            lambda _root, _request_id, **_kwargs: False
        )
        single_instance.application_instance_running = lambda: True
        single_instance.show_bridge_startup_blocked_notice = notices.append

        result = main_module._handoff_previous_application(
            timeout=0.1,
            monotonic=iter((0.0, 1.0)).__next__,
            sleep=lambda _seconds: self.fail("timeout must be checked before sleep"),
        )

        self.assertFalse(result)
        self.assertEqual(len(notices), 1)
        self.assertIn("旧版仍在运行", notices[0])
        self.assertNotIn("旧版已经退出", notices[0])


class DiagnoseVbCableLoopbackDispatchTests(_ArgvRestoringTestCase):
    def setUp(self):
        super().setUp()
        self._original_entrypoint = (
            windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint
        )

    def tearDown(self):
        windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint = (
            self._original_entrypoint
        )
        super().tearDown()

    def test_dispatches_both_paths_and_propagates_exit_code(self):
        received = []
        windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint = (
            lambda request_path, result_path: received.append(
                (request_path, result_path)
            )
            or 9
        )
        sys.argv = [
            "ovb_rc003",
            "--diagnose-vb-cable-loopback",
            "/tmp/request.json",
            "/tmp/result.json",
        ]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(received, [("/tmp/request.json", "/tmp/result.json")])
        self.assertEqual(ctx.exception.code, 9)

    def test_missing_paths_pass_none_through_fail_closed(self):
        received = []
        windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint = (
            lambda request_path, result_path: received.append(
                (request_path, result_path)
            )
            or 1
        )
        sys.argv = ["ovb_rc003", "--diagnose-vb-cable-loopback"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(received, [(None, None)])
        self.assertEqual(ctx.exception.code, 1)

    def test_flag_literal_stays_in_sync(self):
        import inspect

        source = inspect.getsource(main_module)
        self.assertIn(
            f'"{windows_diagnostics.VB_CABLE_LOOPBACK_SUBPROCESS_FLAG}" in args',
            source,
        )


class OutputEndpointPreflightDispatchTests(_ArgvRestoringTestCase):
    def setUp(self):
        super().setUp()
        self._original_entrypoint = (
            windows_diagnostics.run_output_endpoint_preflight_subprocess_entrypoint
        )

    def tearDown(self):
        windows_diagnostics.run_output_endpoint_preflight_subprocess_entrypoint = (
            self._original_entrypoint
        )
        super().tearDown()

    def test_dispatches_both_paths_and_propagates_exit_code(self):
        received = []
        windows_diagnostics.run_output_endpoint_preflight_subprocess_entrypoint = (
            lambda request_path, result_path: received.append(
                (request_path, result_path)
            )
            or 11
        )
        sys.argv = [
            "ovb_rc003",
            "--preflight-output-endpoint",
            "/tmp/request.json",
            "/tmp/result.json",
        ]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(received, [("/tmp/request.json", "/tmp/result.json")])
        self.assertEqual(ctx.exception.code, 11)

    def test_missing_paths_pass_none_through_fail_closed(self):
        received = []
        windows_diagnostics.run_output_endpoint_preflight_subprocess_entrypoint = (
            lambda request_path, result_path: received.append(
                (request_path, result_path)
            )
            or 1
        )
        sys.argv = ["ovb_rc003", "--preflight-output-endpoint"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(received, [(None, None)])
        self.assertEqual(ctx.exception.code, 1)

    def test_flag_literal_stays_in_sync(self):
        import inspect

        source = inspect.getsource(main_module)
        self.assertIn(
            f'"{windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_SUBPROCESS_FLAG}" in args',
            source,
        )


if __name__ == "__main__":
    unittest.main()
