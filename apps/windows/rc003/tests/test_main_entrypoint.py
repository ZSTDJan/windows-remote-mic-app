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
import unittest
from pathlib import Path

from ovb_rc003 import __main__ as main_module
from ovb_rc003 import (
    app,
    config,
    device_catalog,
    element_navigation_runtime,
    frida_compat,
    product_identity,
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
        self._original_guard_cls = single_instance.BridgeInstanceGuard
        self._original_settings_guard_cls = single_instance.SettingsInstanceGuard
        self._original_activate_settings = single_instance.activate_existing_settings_window
        self._original_app_main = app.main
        self._original_notice = single_instance.show_bridge_startup_blocked_notice
        self._original_load_config = config.load_config
        self._original_qt_runtime_check = main_module._qt_runtime_check
        self._original_element_navigation_runtime = (
            element_navigation_runtime.run_element_navigation
        )
        # XRBM-023: default every test in this suite to a safe no-op stub for
        # the visible-notice callable. show_bridge_startup_blocked_notice's
        # real implementation opens a real, SYSTEMMODAL Win32 MessageBoxW -
        # a test that deliberately triggers a blocked startup but forgets to
        # override this explicitly would otherwise open that real dialog and
        # hang the whole headless CI runner waiting for user input (the
        # test_duplicate_launch_never_calls_app_main defect this task fixes).
        # Tests that need to assert on the exact notice text/call count still
        # override this in their own body, same as before.
        single_instance.show_bridge_startup_blocked_notice = lambda message: None
        single_instance.SettingsInstanceGuard = _make_guard_class()
        single_instance.activate_existing_settings_window = lambda: True
        config.load_config = lambda path: {
            "selected_device_profile": device_catalog.RC003_ID
        }

    def tearDown(self):
        sys.argv = self._original_argv
        single_instance.BridgeInstanceGuard = self._original_guard_cls
        single_instance.SettingsInstanceGuard = self._original_settings_guard_cls
        single_instance.activate_existing_settings_window = self._original_activate_settings
        app.main = self._original_app_main
        single_instance.show_bridge_startup_blocked_notice = self._original_notice
        config.load_config = self._original_load_config
        main_module._qt_runtime_check = self._original_qt_runtime_check
        element_navigation_runtime.run_element_navigation = (
            self._original_element_navigation_runtime
        )


class BridgeModeRoutingTests(_ArgvRestoringTestCase):
    def test_dji_profile_never_starts_the_rc003_bridge(self):
        app.main = lambda: self.fail("DJI Mic 2 must not start the RC003 bridge")
        config.load_config = lambda path: {
            "selected_device_profile": device_catalog.DJI_MIC_2_ID
        }
        notice_calls = []
        single_instance.show_bridge_startup_blocked_notice = (
            lambda message, **kwargs: notice_calls.append((message, kwargs))
        )
        sys.argv = ["ovb_rc003", "--bridge"]

        main_module.main()

        self.assertEqual(len(notice_calls), 1)
        self.assertIn("DJI Mic 2", notice_calls[0][0])
        self.assertEqual(
            notice_calls[0][1]["title"], product_identity.DISPLAY_NAME
        )

    def test_unexpected_bridge_runtime_failure_is_visible_and_sanitized(self):
        notice_calls = []
        single_instance.show_bridge_startup_blocked_notice = notice_calls.append
        # Keep this runtime-failure test independent of any real bridge that
        # may already be running on the developer machine.
        single_instance.BridgeInstanceGuard = _make_guard_class()
        app.main = lambda: (_ for _ in ()).throw(RuntimeError("private detail"))
        sys.argv = ["ovb_rc003", "--bridge"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(ctx.exception.code, main_module.BRIDGE_RUNTIME_FAILED_EXIT_CODE)
        self.assertEqual(len(notice_calls), 1)
        self.assertNotIn("private detail", notice_calls[0])

    def test_bridge_flag_calls_app_main_exactly_once_on_first_owner(self):
        app_main_calls = []
        app.main = lambda: app_main_calls.append(1)
        single_instance.BridgeInstanceGuard = _make_guard_class()
        sys.argv = ["ovb_rc003", "--bridge"]

        main_module.main()  # must not raise

        self.assertEqual(app_main_calls, [1])

    def test_duplicate_launch_never_calls_app_main(self):
        app_main_calls = []
        app.main = lambda: app_main_calls.append(1)
        single_instance.BridgeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already running")
        )
        sys.argv = ["ovb_rc003", "--bridge"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(app_main_calls, [])
        self.assertEqual(ctx.exception.code, single_instance.DUPLICATE_INSTANCE_EXIT_CODE)
        self.assertNotEqual(single_instance.DUPLICATE_INSTANCE_EXIT_CODE, 0)

    def test_duplicate_launch_without_an_explicit_notice_override_reaches_the_real_notice_function(self):
        """Regression for XRBM-023 test 245: reproduces exactly why the
        original test_duplicate_launch_never_calls_app_main hung the real
        Windows CI runner - it left the REAL show_bridge_startup_blocked_
        notice wired up, which by default calls single_instance's real
        SYSTEMMODAL Win32 MessageBoxW, and a headless runner then blocks
        waiting for user input on that dialog forever.

        This drives that same REAL notice function (self._original_notice,
        undoing setUp's safety-net no-op stub) through main()'s exact
        duplicate-launch path, proving it does get called - but with its
        own ``_message_box`` collaborator swapped for a safe recorder, so
        this regression test itself never risks opening a real dialog on
        any OS/CI runner, including a real Windows one.
        """
        message_box_calls = []

        def _spy_notice(message):
            self._original_notice(
                message,
                _message_box=lambda title, msg: message_box_calls.append((title, msg)) or 1,
            )

        single_instance.show_bridge_startup_blocked_notice = _spy_notice
        app.main = lambda: self.fail("app.main() must never run on a duplicate launch")
        single_instance.BridgeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already running")
        )
        sys.argv = ["ovb_rc003", "--bridge"]

        with self.assertRaises(SystemExit):
            main_module.main()

        self.assertEqual(len(message_box_calls), 1)

    def test_duplicate_launch_shows_the_visible_notice_exactly_once(self):
        app.main = lambda: self.fail("app.main() must never run on a duplicate launch")
        single_instance.BridgeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already running")
        )
        notice_calls = []
        single_instance.show_bridge_startup_blocked_notice = lambda msg: notice_calls.append(msg)
        sys.argv = ["ovb_rc003", "--bridge"]

        with self.assertRaises(SystemExit):
            main_module.main()

        self.assertEqual(len(notice_calls), 1)
        self.assertIn("already running", notice_calls[0])

    def test_settings_duplicate_returns_immediately_without_modal_notice(self):
        app.main = lambda: self.fail("app.main() must never run on a duplicate launch")
        single_instance.BridgeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already running")
        )
        notice_calls = []
        single_instance.show_bridge_startup_blocked_notice = lambda msg: notice_calls.append(msg)
        sys.argv = [
            "ovb_rc003",
            "--bridge",
            "--bridge-from-settings",
        ]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(ctx.exception.code, single_instance.DUPLICATE_INSTANCE_EXIT_CODE)
        self.assertEqual(notice_calls, [])

    def test_settings_managed_bridge_suppresses_the_second_tray_icon(self):
        calls = []
        app.main = lambda **kwargs: calls.append(kwargs)
        single_instance.BridgeInstanceGuard = _make_guard_class()
        sys.argv = ["ovb_rc003", "--bridge", "--bridge-from-settings"]

        main_module.main()

        self.assertEqual(calls, [{"show_notification_icon": False}])

    def test_guard_unavailable_fails_closed_and_never_calls_app_main(self):
        # XRBM-021 review round 1 P1 #1: the guard FAILS CLOSED - an
        # acquisition failure it cannot resolve is treated the same as a
        # proven duplicate, not as license to start anyway.
        app.main = lambda: self.fail(
            "app.main() must never run when the guard is unavailable"
        )
        single_instance.BridgeInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.SingleInstanceUnavailableError("not on windows")
        )
        notice_calls = []
        single_instance.show_bridge_startup_blocked_notice = lambda msg: notice_calls.append(msg)
        sys.argv = ["ovb_rc003", "--bridge"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(ctx.exception.code, single_instance.GUARD_UNAVAILABLE_EXIT_CODE)
        self.assertNotEqual(single_instance.GUARD_UNAVAILABLE_EXIT_CODE, 0)
        self.assertNotEqual(
            single_instance.GUARD_UNAVAILABLE_EXIT_CODE,
            single_instance.DUPLICATE_INSTANCE_EXIT_CODE,
        )
        self.assertEqual(len(notice_calls), 1)

    def test_mutex_cleanup_failure_after_a_clean_run_shows_a_sanitized_notice(self):
        # MutexCleanupError surfaces from the guard's __exit__, i.e. AFTER
        # app.main() already ran (here: to a clean, immediate return) - it
        # must still produce a visible notice and a deterministic nonzero
        # exit, since the packaged executable is windowed (console=False)
        # and an unhandled exception's traceback is otherwise never seen.
        app_main_calls = []
        app.main = lambda: app_main_calls.append(1)

        class _CleanupFailingGuard:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                raise single_instance.MutexCleanupError(
                    "mutex cleanup did not fully succeed: "
                    "ReleaseMutex returned FALSE; CloseHandle returned FALSE"
                )

        single_instance.BridgeInstanceGuard = lambda: _CleanupFailingGuard()
        notice_calls = []
        single_instance.show_bridge_startup_blocked_notice = lambda msg: notice_calls.append(msg)
        sys.argv = ["ovb_rc003", "--bridge"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(app_main_calls, [1])  # app.main() DID run to completion
        self.assertEqual(ctx.exception.code, single_instance.CLEANUP_FAILED_EXIT_CODE)
        self.assertNotEqual(single_instance.CLEANUP_FAILED_EXIT_CODE, 0)
        self.assertEqual(len(notice_calls), 1)
        # The user-visible notice must be sanitized - never the raw
        # MutexCleanupError text (which itself is already sanitized, but
        # the notice text is deliberately a separate, fixed sentence, not
        # str(exc), so it can never regress even if the exception message
        # shape changes).
        self.assertNotIn("ReleaseMutex", notice_calls[0])
        self.assertNotIn("CloseHandle", notice_calls[0])


class ArgumentModeBypassTests(_ArgvRestoringTestCase):
    """Bridge and settings modes use separate guards; utility modes use none.
    """

    def test_no_arguments_opens_settings_under_only_the_settings_guard(self):
        from ovb_rc003 import settings_ui

        bridge_enter_calls = []
        settings_enter_calls = []
        single_instance.BridgeInstanceGuard = _make_guard_class(
            enter_calls=bridge_enter_calls
        )
        single_instance.SettingsInstanceGuard = _make_guard_class(
            enter_calls=settings_enter_calls
        )
        app.main = lambda: self.fail("no-argument launch must never start the bridge")
        original_settings_main = settings_ui.main
        settings_ui.main = lambda: None
        sys.argv = ["ovb_rc003"]

        try:
            main_module.main()  # returns normally, no SystemExit
        finally:
            settings_ui.main = original_settings_main

        self.assertEqual(bridge_enter_calls, [])
        self.assertEqual(settings_enter_calls, [1])

    def test_dry_run_never_touches_the_guard(self):
        bridge_enter_calls = []
        settings_enter_calls = []
        single_instance.BridgeInstanceGuard = _make_guard_class(
            enter_calls=bridge_enter_calls
        )
        single_instance.SettingsInstanceGuard = _make_guard_class(
            enter_calls=settings_enter_calls
        )
        app.main = lambda: self.fail("--dry-run must never call app.main()")
        sys.argv = ["ovb_rc003", "--dry-run"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(bridge_enter_calls, [])
        self.assertEqual(settings_enter_calls, [])

    def test_qt_runtime_check_never_touches_the_guard(self):
        bridge_enter_calls = []
        settings_enter_calls = []
        check_calls = []
        single_instance.BridgeInstanceGuard = _make_guard_class(
            enter_calls=bridge_enter_calls
        )
        single_instance.SettingsInstanceGuard = _make_guard_class(
            enter_calls=settings_enter_calls
        )
        main_module._qt_runtime_check = lambda: check_calls.append(1) or 0
        app.main = lambda: self.fail("Qt runtime check must never call app.main()")
        sys.argv = ["ovb_rc003", "--qt-runtime-check"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(check_calls, [1])
        self.assertEqual(bridge_enter_calls, [])
        self.assertEqual(settings_enter_calls, [])

    def test_help_never_touches_the_guard(self):
        bridge_enter_calls = []
        settings_enter_calls = []
        single_instance.BridgeInstanceGuard = _make_guard_class(
            enter_calls=bridge_enter_calls
        )
        single_instance.SettingsInstanceGuard = _make_guard_class(
            enter_calls=settings_enter_calls
        )
        app.main = lambda: self.fail("--help must never call app.main()")
        sys.argv = ["ovb_rc003", "--help"]

        main_module.main()  # returns normally, no SystemExit

        self.assertEqual(bridge_enter_calls, [])
        self.assertEqual(settings_enter_calls, [])

    def test_settings_uses_only_the_settings_guard(self):
        from ovb_rc003 import settings_ui

        bridge_enter_calls = []
        settings_enter_calls = []
        single_instance.BridgeInstanceGuard = _make_guard_class(
            enter_calls=bridge_enter_calls
        )
        single_instance.SettingsInstanceGuard = _make_guard_class(
            enter_calls=settings_enter_calls
        )
        app.main = lambda: self.fail("--settings must never call app.main()")
        original_settings_main = settings_ui.main
        settings_ui.main = lambda: None
        sys.argv = ["ovb_rc003", "--settings"]

        try:
            main_module.main()  # returns normally, no SystemExit
        finally:
            settings_ui.main = original_settings_main

        self.assertEqual(bridge_enter_calls, [])
        self.assertEqual(settings_enter_calls, [1])

    def test_background_start_keeps_the_existing_window_hidden(self):
        from ovb_rc003 import settings_ui

        settings_calls = []
        original_settings_main = settings_ui.main
        settings_ui.main = lambda **kwargs: settings_calls.append(kwargs)
        sys.argv = ["ovb_rc003", "--background"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original_settings_main

        self.assertEqual(settings_calls, [{"start_hidden": True}])

    def test_duplicate_background_start_does_not_pop_the_window_open(self):
        from ovb_rc003 import settings_ui

        activation_calls = []
        single_instance.SettingsInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.activate_existing_settings_window = (
            lambda: activation_calls.append(1) or True
        )
        original_settings_main = settings_ui.main
        settings_ui.main = lambda **kwargs: self.fail(
            "duplicate background start must not build another window"
        )
        sys.argv = ["ovb_rc003", "--background"]
        try:
            main_module.main()
        finally:
            settings_ui.main = original_settings_main

        self.assertEqual(activation_calls, [])

    def test_explicit_settings_wins_when_bridge_flag_is_also_present(self):
        from ovb_rc003 import settings_ui

        bridge_enter_calls = []
        settings_enter_calls = []
        settings_calls = []
        single_instance.BridgeInstanceGuard = _make_guard_class(
            enter_calls=bridge_enter_calls
        )
        single_instance.SettingsInstanceGuard = _make_guard_class(
            enter_calls=settings_enter_calls
        )
        app.main = lambda: self.fail("--settings must take precedence over --bridge")
        original_settings_main = settings_ui.main
        settings_ui.main = lambda: settings_calls.append(1)
        sys.argv = ["ovb_rc003", "--bridge", "--settings"]

        try:
            main_module.main()
        finally:
            settings_ui.main = original_settings_main

        self.assertEqual(settings_calls, [1])
        self.assertEqual(bridge_enter_calls, [])
        self.assertEqual(settings_enter_calls, [1])

    def test_duplicate_settings_launch_activates_existing_window_without_opening_another(self):
        from ovb_rc003 import settings_ui

        activation_calls = []
        single_instance.SettingsInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.activate_existing_settings_window = (
            lambda: activation_calls.append(1) or True
        )
        original_settings_main = settings_ui.main
        settings_ui.main = lambda: self.fail("duplicate launch must not build another window")
        sys.argv = ["ovb_rc003", "--settings"]

        try:
            main_module.main()
        finally:
            settings_ui.main = original_settings_main

        self.assertEqual(activation_calls, [1])

    def test_settings_startup_failure_is_visible_and_has_a_stable_exit_code(self):
        from ovb_rc003 import settings_ui

        notice_calls = []
        single_instance.show_bridge_startup_blocked_notice = notice_calls.append
        original_settings_main = settings_ui.main
        settings_ui.main = lambda: (_ for _ in ()).throw(ValueError("private detail"))
        sys.argv = ["ovb_rc003", "--settings"]

        try:
            with self.assertRaises(SystemExit) as ctx:
                main_module.main()
        finally:
            settings_ui.main = original_settings_main

        self.assertEqual(ctx.exception.code, main_module.SETTINGS_STARTUP_FAILED_EXIT_CODE)
        self.assertEqual(len(notice_calls), 1)
        self.assertNotIn("private detail", notice_calls[0])

    def test_bridge_config_failure_is_visible_and_never_touches_the_guard(self):
        enter_calls = []
        notice_calls = []
        single_instance.BridgeInstanceGuard = _make_guard_class(enter_calls=enter_calls)
        single_instance.show_bridge_startup_blocked_notice = notice_calls.append
        config.load_config = lambda path: (_ for _ in ()).throw(ValueError("private detail"))
        app.main = lambda: self.fail("invalid config must never start the bridge")
        sys.argv = ["ovb_rc003", "--bridge"]

        with self.assertRaises(SystemExit) as ctx:
            main_module.main()

        self.assertEqual(ctx.exception.code, main_module.BRIDGE_CONFIG_FAILED_EXIT_CODE)
        self.assertEqual(enter_calls, [])
        self.assertEqual(len(notice_calls), 1)
        self.assertNotIn("private detail", notice_calls[0])

    def test_diagnose_ble_candidates_never_touches_the_guard(self):
        enter_calls = []
        single_instance.BridgeInstanceGuard = _make_guard_class(enter_calls=enter_calls)
        app.main = lambda: self.fail("--diagnose-ble-candidates must never call app.main()")
        sys.argv = ["ovb_rc003", "--diagnose-ble-candidates", "/tmp/result.json"]

        original_entrypoint = windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint
        windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint = lambda result_path: 0
        try:
            with self.assertRaises(SystemExit):
                main_module.main()
        finally:
            windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint = original_entrypoint

        self.assertEqual(enter_calls, [])

    def test_vb_cable_loopback_child_never_touches_the_guard(self):
        enter_calls = []
        single_instance.BridgeInstanceGuard = _make_guard_class(enter_calls=enter_calls)
        app.main = lambda: self.fail("loopback child must never call app.main()")
        sys.argv = [
            "ovb_rc003",
            "--diagnose-vb-cable-loopback",
            "/tmp/request.json",
            "/tmp/result.json",
        ]

        original_entrypoint = (
            windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint
        )
        windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint = (
            lambda request_path, result_path: 0
        )
        try:
            with self.assertRaises(SystemExit):
                main_module.main()
        finally:
            windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint = (
                original_entrypoint
            )

        self.assertEqual(enter_calls, [])

    def test_hid_injector_child_never_touches_the_guard(self):
        enter_calls = []
        received_args = []
        single_instance.BridgeInstanceGuard = _make_guard_class(enter_calls=enter_calls)
        app.main = lambda: self.fail("HID injector child must never call app.main()")
        original_injector_main = frida_compat.injector_main
        frida_compat.injector_main = lambda args: received_args.append(args) or 4
        sys.argv = [
            "ovb_rc003",
            frida_compat.HID_TAP_INJECTOR_FLAG,
            "--pid",
            "1234",
        ]
        try:
            with self.assertRaises(SystemExit) as ctx:
                main_module.main()
        finally:
            frida_compat.injector_main = original_injector_main

        self.assertEqual(ctx.exception.code, 4)
        self.assertEqual(received_args, [["--pid", "1234"]])
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
        self.assertNotIn("--on-request-probe", buffer.getvalue())


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
