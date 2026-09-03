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
    element_navigation_runtime,
    frida_compat,
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
        self._original_application_guard_cls = (
            single_instance.ApplicationInstanceGuard
        )
        self._original_activate_settings = (
            single_instance.activate_existing_settings_window
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
        # Never let a failure-path test open a real system-modal Win32
        # message box on a developer machine or headless CI runner.
        single_instance.show_bridge_startup_blocked_notice = lambda message: None
        single_instance.ApplicationInstanceGuard = _make_guard_class()
        single_instance.activate_existing_settings_window = lambda: True
        single_instance.write_bridge_start_request = lambda _root: None

    def tearDown(self):
        sys.argv = self._original_argv
        single_instance.ApplicationInstanceGuard = (
            self._original_application_guard_cls
        )
        single_instance.activate_existing_settings_window = (
            self._original_activate_settings
        )
        app.main = self._original_app_main
        single_instance.show_bridge_startup_blocked_notice = self._original_notice
        single_instance.write_bridge_start_request = (
            self._original_bridge_start_request
        )
        main_module._qt_runtime_check = self._original_qt_runtime_check
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

        activation_calls = []
        single_instance.ApplicationInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.activate_existing_settings_window = (
            lambda: activation_calls.append(1) or True
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

        self.assertEqual(activation_calls, [1])

    def test_duplicate_visible_launch_reports_when_the_window_cannot_be_activated(self):
        from ovb_rc003 import settings_ui

        activation_calls = []
        notice_calls = []
        single_instance.ApplicationInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.activate_existing_settings_window = (
            lambda: activation_calls.append(1) or False
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

        self.assertEqual(activation_calls, [1])
        self.assertEqual(len(notice_calls), 1)
        self.assertIn("已经在运行", notice_calls[0])
        self.assertIn("不要重复启动", notice_calls[0])

    def test_duplicate_bridge_launch_requests_existing_app_without_popping_window(self):
        from ovb_rc003 import settings_ui

        activation_calls = []
        request_calls = []
        single_instance.ApplicationInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.activate_existing_settings_window = (
            lambda: activation_calls.append(1) or True
        )
        single_instance.write_bridge_start_request = (
            lambda root: request_calls.append(root)
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

        self.assertEqual(len(request_calls), 1)
        self.assertEqual(activation_calls, [])

    def test_duplicate_bridge_request_failure_is_visible_without_a_second_app(self):
        from ovb_rc003 import settings_ui

        activation_calls = []
        notice_calls = []
        single_instance.ApplicationInstanceGuard = _make_guard_class(
            raise_on_enter=single_instance.DuplicateInstanceError("already open")
        )
        single_instance.activate_existing_settings_window = (
            lambda: activation_calls.append(1) or True
        )
        single_instance.write_bridge_start_request = (
            lambda _root: (_ for _ in ()).throw(PermissionError("private detail"))
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

    def test_duplicate_background_start_does_not_pop_the_window_open(self):
        from ovb_rc003 import settings_ui

        activation_calls = []
        single_instance.ApplicationInstanceGuard = _make_guard_class(
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

    def test_help_never_touches_the_guard(self):
        enter_calls = self._assert_application_guard_unused()
        sys.argv = ["ovb_rc003", "--help"]

        main_module.main()

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
