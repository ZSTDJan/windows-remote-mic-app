"""Qt-gated tests for qt_settings_app.py (XRBM-030): ButtonMappingModel,
SettingsController, and an offscreen load of the real main.qml. Every test
class here self-skips with an explicit reason if PySide6-Essentials is not
installed - same "skip with reason, never silently pass" convention as
tests/windows/test_windows_only.py, except gated on PySide6 availability
rather than the host OS, since these are meant to run for REAL wherever Qt
is actually installed (including the real Windows CI runner, now that
PySide6-Essentials is a pinned requirement - see requirements.txt).

No real Windows device or real BLE/HID/audio hardware is ever touched here:
bridge_launcher.launch_bridge()/shell_targets.open_external_target() are
monkeypatched exactly like tests/test_bridge_launcher.py and
tests/test_shell_targets.py already do at their own layer. Real, disposable
CHILD PROCESSES are deliberately spawned by some tests below (XRBM-035
RETRY 1): DiagnosticsController's real BLE candidate check now runs its
discovery in a genuinely separate OS process (see windows_diagnostics.py's
"-- BLE candidate --" section) - this file's own shutdown/probe tests
exercise that real process boundary rather than mocking it away, since the
whole point of that design is a real, OS-confirmed hard bound this project
cannot prove any other way.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import (
    audio_output,
    bridge_control_windows,
    bridge_launcher,
    bridge_runtime_status,
    config,
    device_catalog,
    frida_compat,
    hotkey,
    key_detection_bridge,
    key_mapping,
    product_identity,
    qt_settings_app,
    remote_layout,
    settings_ui,
    shell_targets,
    single_instance,
    vb_cable_bundle,
    voice_program_manager,
    windows_diagnostics,
)


def _has_pyside6() -> bool:
    try:
        import PySide6.QtCore  # noqa: F401
    except ImportError:
        return False
    return True


_HAS_PYSIDE6 = _has_pyside6()

if _HAS_PYSIDE6:
    # Must be set before any QGuiApplication is constructed anywhere in this
    # process - a real display server/compositor is neither available nor
    # wanted in a test run (matches how this task's own isolated-venv
    # screenshot step invokes the same app code). ``qt_settings_app`` itself
    # is already imported unconditionally above (importing it never
    # requires PySide6 - only actually calling ``_load_qt_classes()``/
    # constructing a QGuiApplication does, both of which only happen later,
    # inside test methods, after this env var is already set).
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    # Every test below that needs to drive a real QQmlApplicationEngine/QML
    # window (load, click, type, render) does so inside an isolated
    # subprocess - see _QML_LOAD_PROBE_SCRIPT/_DIRECT_SAVE_PROBE_SCRIPT/
    # _CONTRAST_PROBE_SCRIPT below for why: QQuickStyle is a process-global,
    # set-once setting, and separately, multiple QQmlApplicationEngine
    # instances loading Qt Quick Controls QML within one process turned out
    # to conflict over other internal per-style singletons too (both
    # reproduced empirically while writing these tests). This module's own
    # process therefore never needs to import PySide6's GUI/QML/Test
    # classes directly - only ``qt_settings_app`` itself, for the
    # ButtonMappingModel/SettingsController unit tests above, which never
    # construct a QQmlApplicationEngine at all.


_SKIP_REASON = "PySide6-Essentials not installed - Qt settings UI not verified here"


class DiagnosticsThreadLifecycleAtExitTests(unittest.TestCase):
    """Pure-Python coverage of the diagnostics worker-thread registry and
    its atexit join hook - deliberately NOT gated on PySide6 (importing
    ``qt_settings_app`` never requires it), since none of this touches Qt at
    all. XRBM-031 RETRY 1 item 6: proves the atexit hook is a genuinely
    BOUNDED, best-effort courtesy - never a guarantee every tracked thread
    has actually stopped - matching the corrected module docstring.
    """

    def test_join_at_exit_is_bounded_and_returns_even_if_a_thread_never_finishes(self):
        blocker = threading.Event()
        thread = threading.Thread(target=blocker.wait, daemon=True)
        qt_settings_app._remember_diagnostics_thread(thread)
        thread.start()
        try:
            # A tiny timeout (not the real 2.0s default) keeps this test
            # fast without weakening the production default anywhere.
            with mock.patch.object(
                qt_settings_app, "_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS", 0.05
            ):
                started = time.monotonic()
                qt_settings_app._join_diagnostics_threads_at_exit()
                elapsed = time.monotonic() - started
            # The hook must return promptly - it must never block on a
            # thread that simply never finishes.
            self.assertLess(elapsed, 2.0)
            # The thread is genuinely still alive - the join was bounded,
            # not a guarantee of completion, exactly as documented.
            self.assertTrue(thread.is_alive())
        finally:
            blocker.set()
            thread.join(timeout=5.0)
            qt_settings_app._forget_diagnostics_thread(thread)

    def test_join_at_exit_actually_joins_a_thread_that_finishes_in_time(self):
        release_event = threading.Event()
        thread = threading.Thread(target=release_event.wait, daemon=True)
        qt_settings_app._remember_diagnostics_thread(thread)
        thread.start()
        release_event.set()
        try:
            qt_settings_app._join_diagnostics_threads_at_exit()
            self.assertFalse(thread.is_alive())
        finally:
            qt_settings_app._forget_diagnostics_thread(thread)

    def test_diagnostics_worker_thread_is_created_as_a_daemon_thread(self):
        # The actual safety property against a process-exit hang: the real
        # production code's own diagnostics-worker Thread() construction
        # must pass daemon=True, so CPython itself never waits for it at
        # interpreter shutdown regardless of what the best-effort atexit
        # join above managed to join in time. A static source check (no
        # PySide6 needed) rather than constructing a real DiagnosticsController.
        import inspect

        source = inspect.getsource(qt_settings_app)
        self.assertIn('threading.Thread(target=_run_in_background, daemon=True)', source)

    def test_full_exit_budget_covers_background_cleanup_bridge_stop_and_margin(self):
        minimum = (
            qt_settings_app._SETTINGS_BACKGROUND_JOIN_TIMEOUT_SECONDS
            + bridge_control_windows.DEFAULT_EXIT_TIMEOUT_SECONDS
            + 1.0
        )
        self.assertGreaterEqual(
            qt_settings_app._APPLICATION_EXIT_WAIT_TIMEOUT_SECONDS,
            minimum,
        )


class DiagnosticsShutdownOrderingTests(unittest.TestCase):
    """XRBM-031 RETRY 2: an independent review found that the previous fix
    registered ``_join_diagnostics_threads_at_exit`` and
    ``_release_qt_classes_cache`` as two SEPARATE ``atexit`` callbacks -
    since ``atexit`` runs registered functions in LIFO order, the
    second-registered function (cache release) actually ran FIRST at real
    process exit, the reverse of what the module docstring claimed. These
    tests are deliberately NOT gated on PySide6 (none of this touches Qt -
    the functions under test are pure Python, and are exercised here via
    mocks standing in for the real join/cache-release steps).
    """

    def tearDown(self):
        # Defensive: no test below should leave this set, but clear it
        # unconditionally so a failure here never leaks into a later test
        # in the same process (this is process-global, persistent state).
        qt_settings_app._diagnostics_shutdown_event.clear()

    def test_module_registers_exactly_one_atexit_shutdown_hook(self):
        # Guards against silently reintroducing the two-separate-
        # registrations bug this RETRY fixes: an AST-level count of actual
        # atexit.register(...) CALL expressions (not a naive substring
        # count, which would also match this module's own docstring prose
        # explaining why there is only one) - introspecting the real source
        # rather than CPython's private atexit internals.
        import ast
        import inspect

        source = inspect.getsource(qt_settings_app)
        tree = ast.parse(source)
        register_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "atexit"
        ]
        self.assertEqual(len(register_calls), 1)
        (call,) = register_calls
        self.assertEqual(call.args[0].id, "_shutdown_qt_settings_app_at_exit")

    def test_shutdown_at_exit_runs_flag_then_join_then_release_cache_in_order(self):
        calls = []
        with mock.patch.object(
            qt_settings_app,
            "_begin_diagnostics_shutdown",
            side_effect=lambda: calls.append("begin_shutdown"),
        ):
            with mock.patch.object(
                qt_settings_app,
                "_join_diagnostics_threads_at_exit",
                side_effect=lambda: calls.append("join"),
            ):
                with mock.patch.object(
                    qt_settings_app,
                    "_release_qt_classes_cache",
                    side_effect=lambda: calls.append("release_cache"),
                ):
                    qt_settings_app._shutdown_qt_settings_app_at_exit()
        self.assertEqual(calls, ["begin_shutdown", "join", "release_cache"])

    def test_begin_diagnostics_shutdown_sets_the_event(self):
        self.assertFalse(qt_settings_app._diagnostics_shutdown_event.is_set())
        qt_settings_app._begin_diagnostics_shutdown()
        self.assertTrue(qt_settings_app._diagnostics_shutdown_event.is_set())

    def test_join_diagnostics_threads_at_exit_never_touches_the_shutdown_event(self):
        # Kept orthogonal on purpose (see _join_diagnostics_threads_at_exit's
        # docstring) so RETRY 1's own tests, which call this function
        # directly without expecting a global side effect, keep working.
        self.assertFalse(qt_settings_app._diagnostics_shutdown_event.is_set())
        qt_settings_app._join_diagnostics_threads_at_exit()
        self.assertFalse(qt_settings_app._diagnostics_shutdown_event.is_set())


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class ButtonMappingModelTests(unittest.TestCase):
    def setUp(self):
        self.Model = qt_settings_app._load_qt_classes()["ButtonMappingModel"]

    def test_row_count_is_thirteen(self):
        model = self.Model()
        self.assertEqual(model.rowCount(), 13)

    def test_role_names_cover_every_expected_role(self):
        model = self.Model()
        role_names = {value.data().decode() for value in model.roleNames().values()}
        for expected in (
            "buttonId", "displayName", "hidUsage", "actionText", "isMic",
            "doubleClickText", "longPressText",
            "isSelected", "hotspotX", "hotspotY", "hotspotWidth",
            "hotspotHeight", "isVoice",
        ):
            self.assertIn(expected, role_names)

    def test_data_matches_remote_layout_for_the_ok_button(self):
        model = self.Model()
        index = model.index(model.index_of("ok"), 0)
        self.assertEqual(model.data(index, model.ButtonIdRole), "ok")
        self.assertEqual(
            model.data(index, model.DisplayNameRole),
            remote_layout.BUTTON_DISPLAY_NAMES["ok"],
        )
        self.assertEqual(
            model.data(index, model.HidUsageRole), remote_layout.hid_usage_display("ok")
        )
        self.assertFalse(model.data(index, model.IsMicRole))

    def test_hotspot_geometry_matches_remote_layout_for_mic(self):
        model = self.Model()
        index = model.index(model.index_of("mic"), 0)
        hotspot = remote_layout.hotspot_for("mic")
        self.assertEqual(model.data(index, model.XRole), hotspot.x)
        self.assertEqual(model.data(index, model.YRole), hotspot.y)
        self.assertEqual(model.data(index, model.WidthRole), hotspot.width)
        self.assertEqual(model.data(index, model.HeightRole), hotspot.height)
        self.assertTrue(model.data(index, model.IsVoiceRole))

    def test_mic_row_uses_the_loaded_mapping(self):
        model = self.Model()
        model.load_display_map({"mic": "Escape"})
        index = model.index(model.index_of("mic"), 0)
        self.assertTrue(model.data(index, model.IsMicRole))
        self.assertEqual(model.data(index, model.ActionTextRole), "Escape")

    def test_set_action_text_at_updates_a_non_mic_row(self):
        model = self.Model()
        row = model.index_of("power")
        model.setActionTextAt(row, "escape")
        index = model.index(row, 0)
        self.assertEqual(model.data(index, model.ActionTextRole), "escape")

    def test_mapping_edited_emits_only_when_an_action_really_changes(self):
        model = self.Model()
        changes = []
        model.mappingEdited.connect(lambda: changes.append(True))
        row = model.index_of("power")

        model.setActionTextAt(row, "escape")
        model.setActionTextAt(row, "escape")
        model.setSecondaryActionTextAt(row, "double_click", "f5")
        model.setSecondaryActionTextAt(row, "double_click", "f5")

        self.assertEqual(len(changes), 2)

    def test_set_action_text_at_updates_the_mic_row(self):
        model = self.Model()
        row = model.index_of("mic")
        model.setActionTextAt(row, "方向上")
        index = model.index(row, 0)
        self.assertEqual(model.data(index, model.ActionTextRole), "方向上")

    def test_secondary_action_text_can_be_set_and_round_tripped(self):
        model = self.Model()
        row = model.index_of("power")
        model.setSecondaryActionTextAt(row, "double_click", "f5")
        model.setSecondaryActionTextAt(row, "long_press", "系统音量 +")
        index = model.index(row, 0)
        self.assertEqual(model.data(index, model.DoubleClickTextRole), "f5")
        self.assertEqual(model.data(index, model.LongPressTextRole), "系统音量 +")
        self.assertEqual(
            model.to_secondary_display_map()["power"],
            {"double_click": "f5", "long_press": "系统音量 +"},
        )

    def test_mic_secondary_action_can_be_set_and_round_tripped(self):
        model = self.Model()
        row = model.index_of("mic")
        model.setSecondaryActionTextAt(row, "double_click", "Escape")
        self.assertEqual(
            model.to_secondary_display_map()["mic"]["double_click"],
            "Escape",
        )

    def test_display_note_can_be_set_and_round_tripped(self):
        model = self.Model()
        row = model.index_of("power")
        model.setDisplayNoteAt(row, "single_click", "  关机  ")
        index = model.index(row, 0)
        self.assertEqual(model.data(index, model.SingleNoteRole), "关机")
        self.assertEqual(
            model.to_display_note_map(),
            {"power": {"single_click": "关机"}},
        )

    def test_to_display_map_round_trips_all_physical_buttons(self):
        model = self.Model()
        model.load_display_map({"power": "escape", "up": "up", "mic": "Escape"})
        result = model.to_display_map()
        self.assertEqual(result["power"], "escape")
        self.assertEqual(result["up"], "up")
        self.assertEqual(result["mic"], "Escape")

    def test_unconfigured_secondary_actions_have_an_explicit_display_value(self):
        model = self.Model()
        model.load_display_map({"power": "escape"})
        index = model.index(model.index_of("power"), 0)
        self.assertEqual(
            model.data(index, model.DoubleClickTextRole),
            settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
        )
        self.assertEqual(
            model.data(index, model.LongPressTextRole),
            settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
        )
        self.assertEqual(
            model.to_secondary_display_map()["power"],
            {"double_click": "", "long_press": ""},
        )

    def test_load_display_map_updates_rows_without_resetting_or_losing_selection(self):
        model = self.Model()
        resets = []
        changes = []
        model.modelReset.connect(lambda: resets.append(True))
        model.dataChanged.connect(
            lambda first, last, roles: changes.append(
                (first.row(), last.row(), tuple(roles))
            )
        )
        model.set_selected_button("power")

        model.load_display_map(
            {"power": "Escape", "mic": "按住说话"},
            {"power": {"double_click": "Return", "long_press": ""}},
            {"power": {"single_click": "关机"}},
        )

        self.assertEqual(resets, [])
        self.assertTrue(changes)
        self.assertEqual(model.selected_button_id(), "power")
        power = model.index(model.index_of("power"), 0)
        self.assertTrue(model.data(power, model.IsSelectedRole))
        self.assertEqual(model.data(power, model.ActionTextRole), "Escape")
        self.assertEqual(model.data(power, model.DoubleClickTextRole), "Return")
        self.assertEqual(model.data(power, model.SingleNoteRole), "关机")

    def test_selecting_a_button_flags_only_that_row_as_selected(self):
        model = self.Model()
        self.assertEqual(model.selected_button_id(), "ok")
        model.set_selected_button("power")
        self.assertEqual(model.selected_button_id(), "power")
        power_index = model.index(model.index_of("power"), 0)
        ok_index = model.index(model.index_of("ok"), 0)
        self.assertTrue(model.data(power_index, model.IsSelectedRole))
        self.assertFalse(model.data(ok_index, model.IsSelectedRole))

    def test_index_of_button_slot_matches_the_plain_python_helper(self):
        model = self.Model()
        self.assertEqual(model.indexOfButton("tv"), model.index_of("tv"))

    def test_index_of_button_returns_negative_one_for_unknown_id(self):
        model = self.Model()
        self.assertEqual(model.indexOfButton("does_not_exist"), -1)


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class SettingsControllerTests(unittest.TestCase):
    def setUp(self):
        classes = qt_settings_app._load_qt_classes()
        self.Model = classes["ButtonMappingModel"]
        self.Controller = classes["SettingsController"]
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmpdir.name})
        self._env_patch.start()
        self._bridge_status_patch = mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=False,
        )
        self._bridge_status_patch.start()
        self._startup_state_patch = mock.patch.object(
            qt_settings_app.startup_windows,
            "read_startup_state",
            return_value=qt_settings_app.startup_windows.StartupState(False),
        )
        self._startup_state_patch.start()
        self._voice_hotkey_read_patch = mock.patch.object(
            qt_settings_app.voice_hotkey_sync_windows,
            "read_provider_hotkey",
            side_effect=lambda provider_id: (
                qt_settings_app.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                    str(provider_id),
                    False,
                    "local_only",
                    message="test uses remembered shortcut",
                )
            ),
        )
        self._voice_hotkey_read_mock = self._voice_hotkey_read_patch.start()
        self._voice_hotkey_sync_patch = mock.patch.object(
            qt_settings_app.voice_hotkey_sync_windows,
            "sync_provider_hotkey",
            side_effect=lambda provider_id, shortcut: (
                qt_settings_app.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                    str(provider_id),
                    True,
                    "synced",
                    shortcut,
                    "test shortcut synchronized",
                )
            ),
        )
        self._voice_hotkey_sync_mock = self._voice_hotkey_sync_patch.start()
        qt_settings_app._vb_cable_test_active_event.clear()
        qt_settings_app._driver_action_active_event.clear()

    def tearDown(self):
        qt_settings_app._vb_cable_test_active_event.clear()
        qt_settings_app._driver_action_active_event.clear()
        self._startup_state_patch.stop()
        self._voice_hotkey_sync_patch.stop()
        self._voice_hotkey_read_patch.stop()
        self._bridge_status_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _make_controller(self):
        model = self.Model()
        controller = self.Controller(
            model,
            background_task_runner=lambda target, _name: target(),
        )
        return controller, model

    def _continue_save_and_launch(self, controller):
        controller._continue_save_and_launch()

    def test_desktop_behavior_defaults_hide_close_and_do_not_auto_start_bridge(self):
        controller, _model = self._make_controller()
        self.assertFalse(controller.launchAtLogin)
        self.assertFalse(controller.launchBridgeOnAppStart)
        self.assertEqual(controller.closeBehavior, "hide_to_tray")
        self.assertTrue(
            controller.trayIconSource.endswith("remote-mic-unavailable.svg")
        )

    def test_desktop_behavior_changes_are_persisted_immediately(self):
        controller, _model = self._make_controller()
        controller.setLaunchBridgeOnAppStart(True)
        controller.setCloseBehaviorIndex(1)

        saved = config.load_config(config.config_path(Path(self._tmpdir.name) / "RemoteMic" / "RC003"))
        self.assertTrue(saved["launch_bridge_on_app_start"])
        self.assertEqual(saved["close_behavior"], "quit")

    def test_launch_at_login_uses_the_scoped_windows_owner(self):
        controller, _model = self._make_controller()
        with mock.patch.object(
            qt_settings_app.startup_windows,
            "set_startup_enabled",
            return_value=qt_settings_app.startup_windows.StartupState(True),
        ) as setter:
            controller.setLaunchAtLogin(True)

        setter.assert_called_once_with(True)
        self.assertTrue(controller.launchAtLogin)
        self.assertFalse(controller.launchBridgeOnAppStart)

    def test_application_start_option_reuses_the_normal_bridge_start(self):
        controller, _model = self._make_controller()
        controller._launch_bridge_on_app_start = True
        calls = []
        controller.startBridge = lambda: calls.append(True)

        controller.startBridgeOnApplicationStart()

        self.assertEqual(calls, [True])
        self.assertFalse(controller.launchAtLogin)

    def test_full_exit_without_a_bridge_is_immediate(self):
        controller, _model = self._make_controller()
        ready = []
        controller.applicationExitReady.connect(
            lambda: ready.append(controller.applicationExitConfirmed)
        )
        with mock.patch.object(
            qt_settings_app.bridge_control_windows,
            "request_bridge_exit",
        ) as request_exit:
            controller.requestApplicationExit()

        self.assertEqual(ready, [True])
        request_exit.assert_not_called()
        self.assertFalse(controller._application_exit_requested)

    def test_full_exit_waits_for_an_in_progress_settings_save(self):
        controller, _model = self._make_controller()
        callbacks = []
        ready = []
        controller.applicationExitReady.connect(lambda: ready.append(True))
        controller._set_settings_save_busy(True)

        with mock.patch(
            "PySide6.QtCore.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            controller.requestApplicationExit()

            self.assertEqual(ready, [])
            self.assertTrue(controller._application_exit_requested)
            self.assertTrue(controller._application_exit_waiting_for_save)
            self.assertFalse(controller._application_exit_intent.is_set())

            controller._set_settings_save_busy(False)
            controller._begin_application_exit()
            self.assertEqual(ready, [])
            callbacks.pop()()

        self.assertEqual(ready, [True])
        self.assertFalse(controller._application_exit_requested)
        self.assertFalse(controller._application_exit_waiting_for_save)

    def test_full_exit_save_wait_is_bounded_without_spending_the_cleanup_budget(self):
        controller, _model = self._make_controller()
        callbacks = []
        failures = []
        now = [10.0]
        controller.applicationExitFailed.connect(failures.append)
        controller._set_settings_save_busy(True)

        with mock.patch.object(
            qt_settings_app.time,
            "monotonic",
            side_effect=lambda: now[0],
        ), mock.patch(
            "PySide6.QtCore.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            controller.requestApplicationExit()
            save_deadline = controller._application_exit_deadline
            self.assertEqual(
                save_deadline,
                now[0]
                + qt_settings_app._APPLICATION_EXIT_SAVE_WAIT_TIMEOUT_SECONDS,
            )

            now[0] += 5.0
            controller._set_settings_save_busy(False)
            controller._begin_application_exit()

            self.assertEqual(
                controller._application_exit_deadline,
                now[0] + qt_settings_app._APPLICATION_EXIT_WAIT_TIMEOUT_SECONDS,
            )
            self.assertEqual(failures, [])
            callbacks.pop()()

        self.assertFalse(controller._application_exit_requested)

    def test_full_exit_save_wait_times_out_instead_of_polling_forever(self):
        controller, _model = self._make_controller()
        callbacks = []
        failures = []
        now = [10.0]
        controller.applicationExitFailed.connect(failures.append)
        controller._set_settings_save_busy(True)

        with mock.patch.object(
            qt_settings_app.time,
            "monotonic",
            side_effect=lambda: now[0],
        ), mock.patch(
            "PySide6.QtCore.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            controller.requestApplicationExit()
            now[0] = controller._application_exit_deadline
            callbacks.pop()()

        self.assertEqual(
            failures,
            ["完全退出超时：设置仍在保存，请稍后重试。"],
        )
        self.assertFalse(controller._application_exit_requested)
        self.assertFalse(controller._application_exit_waiting_for_save)

    def test_full_exit_reuses_the_existing_poll_and_starts_one_bridge_stop(self):
        model = self.Model()
        background_tasks = []
        callbacks = []

        def runner(target, name):
            if name == "remote-mic-application-exit":
                background_tasks.append((target, name))
            else:
                target()

        controller = self.Controller(
            model,
            background_task_runner=runner,
        )
        controller._set_settings_save_busy(True)

        with mock.patch(
            "PySide6.QtCore.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ), mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=True,
        ):
            controller.requestApplicationExit()
            self.assertEqual(len(callbacks), 1)

            controller._set_settings_save_busy(False)
            controller._begin_application_exit()
            self.assertEqual(background_tasks, [])

            callbacks.pop()()
            self.assertEqual(len(background_tasks), 1)
            self.assertEqual(
                background_tasks[0][1],
                "remote-mic-application-exit",
            )

            controller._continue_application_exit()
            self.assertEqual(len(background_tasks), 1)

    def test_full_exit_stays_open_when_an_in_progress_save_fails(self):
        saved_config = config.default_config()
        saved_config["output_endpoint_name"] = "CABLE Input"
        saved_config["output_endpoint_host_api"] = "Windows WASAPI"
        config.save_config(config.config_path(config.config_root()), saved_config)
        saved_bindings = config.default_key_bindings()
        saved_bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE,
        ).to_dict()
        config.save_key_bindings(
            config.key_bindings_path(config.config_root()),
            saved_bindings,
        )
        endpoint = audio_output.AudioEndpoint(
            name="CABLE Input",
            host_api="Windows WASAPI",
        )
        deferred = []

        def runner(target, name):
            if name == "audio-endpoint-preflight":
                deferred.append(target)
            else:
                target()

        failures = []
        with mock.patch.object(
            audio_output,
            "enumerate_output_endpoints",
            return_value=[endpoint],
        ), mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
            side_effect=audio_output.AudioOutputUnavailableError("cannot open"),
        ):
            model = self.Model()
            controller = self.Controller(
                model,
                background_task_runner=runner,
            )
            model.setActionTextAt(
                model.index_of("mic"),
                settings_ui._VOICE_HOLD_DISPLAY,
            )
            controller.applicationExitFailed.connect(failures.append)

            self.assertTrue(controller.saveSettings())
            self.assertTrue(controller.settingsSaveBusy)
            self.assertEqual(len(deferred), 1)

            controller.requestApplicationExit()

            self.assertTrue(controller._application_exit_requested)
            self.assertTrue(controller._application_exit_waiting_for_save)
            self.assertFalse(controller._application_exit_intent.is_set())
            deferred.pop()()

        self.assertEqual(failures, ["设置保存未完成，程序没有退出。"])
        self.assertTrue(controller.settingsDirty)
        self.assertFalse(controller._application_exit_requested)
        self.assertFalse(controller._application_exit_waiting_for_save)
        self.assertFalse(controller._application_exit_intent.is_set())

    def test_full_exit_continues_after_a_real_async_save_succeeds(self):
        saved_config = config.default_config()
        saved_config["output_endpoint_name"] = "CABLE Input"
        saved_config["output_endpoint_host_api"] = "Windows WASAPI"
        config.save_config(config.config_path(config.config_root()), saved_config)
        saved_bindings = config.default_key_bindings()
        saved_bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE,
        ).to_dict()
        config.save_key_bindings(
            config.key_bindings_path(config.config_root()),
            saved_bindings,
        )
        endpoint = audio_output.AudioEndpoint(
            name="CABLE Input",
            host_api="Windows WASAPI",
        )
        deferred = []
        callbacks = []

        def runner(target, name):
            if name == "audio-endpoint-preflight":
                deferred.append(target)
            else:
                target()

        ready = []
        with mock.patch.object(
            audio_output,
            "enumerate_output_endpoints",
            return_value=[endpoint],
        ), mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
        ), mock.patch(
            "PySide6.QtCore.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            model = self.Model()
            controller = self.Controller(
                model,
                background_task_runner=runner,
            )
            model.setActionTextAt(
                model.index_of("mic"),
                settings_ui._VOICE_HOLD_DISPLAY,
            )
            controller.applicationExitReady.connect(lambda: ready.append(True))

            self.assertTrue(controller.saveSettings())
            controller.requestApplicationExit()
            deferred.pop()()

            self.assertTrue(controller._application_exit_requested)
            self.assertFalse(controller._application_exit_waiting_for_save)
            self.assertTrue(controller._application_exit_intent.is_set())
            self.assertEqual(ready, [])
            callbacks.pop()()

        self.assertEqual(ready, [True])
        self.assertFalse(controller._application_exit_requested)

    def test_full_exit_waits_for_detect_and_save_driver_action(self):
        controller, _model = self._make_controller()
        ready = []
        controller.applicationExitReady.connect(lambda: ready.append(True))
        qt_settings_app._driver_action_active_event.set()

        controller.requestApplicationExit()

        self.assertEqual(ready, [])
        self.assertTrue(controller._application_exit_requested)

        qt_settings_app._driver_action_active_event.clear()
        controller._continue_application_exit()

        self.assertEqual(ready, [True])

    def test_save_settings_and_exit_waits_for_the_save_result(self):
        controller, _model = self._make_controller()
        completions = []
        finished = []
        controller.saveSettingsAndExitFinished.connect(finished.append)
        controller._save = lambda completion=None: (
            completions.append(completion) or True
        )
        controller.requestApplicationExit = mock.Mock()

        controller.saveSettingsAndExit()

        controller.requestApplicationExit.assert_not_called()
        self.assertEqual(len(completions), 1)
        completions[0](True)
        self.assertEqual(finished, [True])
        controller.requestApplicationExit.assert_called_once_with()

    def test_save_settings_and_exit_stays_open_when_save_fails(self):
        controller, _model = self._make_controller()
        completions = []
        finished = []
        controller.saveSettingsAndExitFinished.connect(finished.append)
        controller._save = lambda completion=None: (
            completions.append(completion) or True
        )
        controller.requestApplicationExit = mock.Mock()

        controller.saveSettingsAndExit()
        completions[0](False)

        self.assertEqual(finished, [False])
        controller.requestApplicationExit.assert_not_called()

    def test_full_exit_waits_for_the_bridge_to_stop(self):
        controller, _model = self._make_controller()
        ready = []
        controller.applicationExitReady.connect(
            lambda: ready.append(controller.applicationExitConfirmed)
        )
        with (
            mock.patch.object(
                qt_settings_app.single_instance,
                "bridge_instance_running",
                return_value=True,
            ),
            mock.patch.object(
                qt_settings_app.bridge_control_windows,
                "request_bridge_exit",
                return_value=qt_settings_app.bridge_control_windows.BridgeExitResult(
                    requested=True,
                    stopped=True,
                ),
            ),
        ):
            controller.requestApplicationExit()

        self.assertEqual(ready, [True])
        self.assertFalse(controller._application_exit_requested)

    def test_full_exit_control_failure_is_visible_and_retryable(self):
        controller, _model = self._make_controller()
        failures = []
        controller.applicationExitFailed.connect(failures.append)
        with (
            mock.patch.object(
                qt_settings_app.single_instance,
                "bridge_instance_running",
                return_value=True,
            ),
            mock.patch.object(
                qt_settings_app.bridge_control_windows,
                "request_bridge_exit",
                side_effect=RuntimeError("simulated control failure"),
            ) as request_exit,
        ):
            controller.requestApplicationExit()
            controller.requestApplicationExit()

        self.assertEqual(request_exit.call_count, 2)
        self.assertEqual(failures, ["完全退出失败：RuntimeError"] * 2)
        self.assertFalse(controller._application_exit_requested)

    def test_application_exit_ready_is_connected_to_qt_quit(self):
        callbacks = []
        quit_calls = []

        class FakeSignal:
            def connect(self, callback):
                callbacks.append(callback)

        class FakeController:
            applicationExitReady = FakeSignal()

        class FakeApplication:
            def quit(self):
                quit_calls.append(True)

        qt_settings_app._connect_application_exit(
            FakeApplication(), FakeController()
        )
        callbacks[0]()

        self.assertEqual(quit_calls, [True])

    def test_hotkey_text_defaults_to_the_configured_default(self):
        controller, _ = self._make_controller()
        self.assertEqual(controller.hotkeyText, "ralt")

    def test_voice_program_management_defaults_to_optional_and_disabled(self):
        controller, _ = self._make_controller()
        self.assertEqual(controller.voiceProgramOptions[0], "不管理")
        self.assertEqual(controller.voiceProgramOptions[2], "微信输入法")
        self.assertEqual(
            controller.voiceProgramOptions[3], "Windows 语音输入（Win+H）"
        )
        self.assertEqual(controller.voiceProgramOptions[4], "自定义程序")
        self.assertEqual(controller.selectedVoiceProgramIndex, 0)
        self.assertFalse(controller.voiceProgramSystemManaged)
        self.assertFalse(controller.voiceProgramLaunchOnBridgeStart)
        self.assertFalse(controller.voiceProgramLaunchElevated)
        self.assertFalse(controller.voiceProgramSettingsDirty)
        self.assertEqual(controller.voiceProgramElevationStatus, "unknown")
        self.assertIn("不会管理", controller.voiceProgramStatusText)

    def test_voice_program_status_exposes_actual_elevation_without_parsing_text(self):
        controller, _ = self._make_controller()
        for elevated, expected in (
            (True, "elevated"),
            (False, "standard"),
            (None, "unknown"),
        ):
            status = voice_program_manager.VoiceProgramStatus(
                provider_id="sogou",
                display_name="搜狗输入法",
                available=True,
                running=True,
                elevated=elevated,
                executable=Path("C:/Program Files/Sogou/voice.exe"),
                code="running",
            )
            with mock.patch.object(
                qt_settings_app.voice_program_manager,
                "inspect_voice_program",
                return_value=status,
            ):
                controller._refresh_voice_program_status()
            self.assertEqual(controller.voiceProgramElevationStatus, expected)

    def test_voice_program_status_refresh_runs_in_a_worker_thread(self):
        controller, _ = self._make_controller()
        controller._background_task_runner = None
        caller_thread = threading.get_ident()
        worker_threads = []
        completed = threading.Event()

        def payload(settings):
            worker_threads.append(threading.get_ident())
            completed.set()
            return "后台结果", "running", "standard"

        with mock.patch.object(
            controller, "_voice_program_status_payload", side_effect=payload
        ):
            controller._request_voice_program_status_refresh()
            self.assertTrue(completed.wait(2.0))

        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], caller_thread)

    def test_background_shutdown_joins_workers_and_suppresses_all_results(self):
        controller, _ = self._make_controller()
        controller._background_task_runner = None
        endpoint_started = threading.Event()
        status_started = threading.Event()
        hotkey_started = threading.Event()
        release_workers = threading.Event()
        hotkey_completions = []
        original_options = list(controller.endpointOptions)
        original_status = controller.voiceProgramStatusText

        def endpoint_payload(_config_snapshot):
            endpoint_started.set()
            release_workers.wait(2.0)
            return {
                "options": ["关闭后结果"],
                "values": [],
                "recommended_index": -1,
                "selected_index": -1,
                "migration_message": "",
            }

        def status_payload(_settings_snapshot):
            status_started.set()
            release_workers.wait(2.0)
            return "关闭后状态", "running", "standard"

        def hotkey_payload():
            hotkey_started.set()
            release_workers.wait(2.0)
            return "关闭后快捷键"

        with mock.patch.object(
            controller, "_endpoint_options_payload", side_effect=endpoint_payload
        ), mock.patch.object(
            controller, "_voice_program_status_payload", side_effect=status_payload
        ):
            controller._request_endpoint_options_refresh()
            controller._request_voice_program_status_refresh()
            controller._submit_voice_hotkey_step(
                hotkey_payload,
                lambda ok, payload: hotkey_completions.append((ok, payload)),
            )
            self.assertTrue(endpoint_started.wait(1.0))
            self.assertTrue(status_started.wait(1.0))
            self.assertTrue(hotkey_started.wait(1.0))

            releaser = threading.Timer(0.05, release_workers.set)
            releaser.start()
            controller.shutdownBackgroundTasks()
            releaser.join()

        self.assertEqual(len(controller._background_threads), 0)
        self.assertEqual(controller.endpointOptions, original_options)
        self.assertEqual(controller.voiceProgramStatusText, original_status)
        self.assertEqual(hotkey_completions, [])
        self.assertFalse(controller.voiceHotkeyBusy)

        controller._on_endpoint_options_refresh_ready(
            (
                dict(controller._config),
                {
                    "options": ["迟到端点"],
                    "values": [],
                    "recommended_index": -1,
                    "selected_index": -1,
                    "migration_message": "",
                },
            )
        )
        controller._on_voice_program_status_refresh_ready(
            (dict(controller._voice_program_settings), ("迟到状态", "running", "standard"))
        )
        controller._on_voice_hotkey_task_ready(
            (controller._voice_hotkey_task_token, (True, "迟到快捷键"))
        )
        self.assertEqual(controller.endpointOptions, original_options)
        self.assertEqual(controller.voiceProgramStatusText, original_status)
        self.assertEqual(hotkey_completions, [])

    def test_injected_background_runner_does_not_register_threads(self):
        controller, _ = self._make_controller()

        self.assertEqual(len(controller._background_threads), 0)

        controller.shutdownBackgroundTasks()
        self.assertTrue(controller._background_shutdown_event.is_set())

    def test_voice_program_status_refresh_coalesces_repeated_requests(self):
        controller, _ = self._make_controller()
        controller._voice_program_status_refresh_running = True

        with mock.patch.object(controller, "_schedule_voice_program_status_refresh") as schedule:
            controller._request_voice_program_status_refresh()
            controller._request_voice_program_status_refresh()
            self.assertTrue(controller._voice_program_status_refresh_pending)
            controller._on_voice_program_status_refresh_ready(
                (
                    dict(controller._voice_program_settings),
                    ("后台结果", "running", "standard"),
                )
            )

        self.assertFalse(controller._voice_program_status_refresh_pending)
        self.assertEqual(controller.voiceProgramStatusText, "后台结果")
        schedule.assert_called_once_with()

    def test_stale_voice_program_status_result_is_discarded_and_refreshed(self):
        controller, _ = self._make_controller()
        old_settings = dict(controller._voice_program_settings)
        controller._voice_program_settings = voice_program_manager.normalize_voice_program_settings(
            {"provider": "sogou"}
        )

        with mock.patch.object(controller, "_schedule_voice_program_status_refresh") as schedule:
            controller._voice_program_status_refresh_running = True
            controller._on_voice_program_status_refresh_ready(
                (old_settings, ("旧结果", "running", "standard"))
            )

        self.assertNotEqual(controller.voiceProgramStatusText, "旧结果")
        schedule.assert_called_once_with()

    def test_selecting_a_voice_program_enables_bridge_autostart_automatically(self):
        controller, _ = self._make_controller()

        controller.selectedVoiceProgramIndex = 1

        self.assertTrue(controller.voiceProgramLaunchOnBridgeStart)
        self.assertFalse(controller.voiceProgramSettingsDirty)
        self.assertFalse(controller.settingsDirty)
        self.assertTrue(controller.voiceProgramLaunchElevated)
        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(saved["voice_program"]["provider"], "sogou")
        self.assertTrue(saved["voice_program"]["launch_on_bridge_start"])
        self.assertTrue(saved["voice_program"]["launch_elevated"])

    def test_selecting_wetype_uses_windows_management_without_autostart(self):
        controller, _ = self._make_controller()

        controller.selectedVoiceProgramIndex = 2

        self.assertTrue(controller.voiceProgramSystemManaged)
        self.assertFalse(controller.voiceProgramLaunchOnBridgeStart)
        self.assertFalse(controller.voiceProgramSettingsDirty)

    def test_selecting_sogou_adopts_and_remembers_its_detected_shortcut(self):
        self._voice_hotkey_read_mock.side_effect = None
        self._voice_hotkey_read_mock.return_value = (
            qt_settings_app.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                "sogou",
                True,
                "read",
                "lctrl+lshift+f9",
                "read from Sogou",
            )
        )
        controller, _ = self._make_controller()

        controller.selectedVoiceProgramIndex = 1

        self.assertEqual(controller.holdVoiceHotkeyText, "lctrl+lshift+f9")
        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(
            saved["voice_hotkeys_by_provider"]["sogou"]["hold"],
            "lctrl+lshift+f9",
        )

    def test_selecting_wetype_uses_its_remembered_default_without_an_error(self):
        controller, _ = self._make_controller()

        controller.selectedVoiceProgramIndex = 2

        self.assertEqual(controller.holdVoiceHotkeyText, "lctrl+lwin")
        self.assertEqual(controller.errorMessage, "")

    def test_provider_sync_failure_restores_the_previous_shortcut(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        previous = controller.holdVoiceHotkeyText
        self._voice_hotkey_sync_mock.side_effect = None
        self._voice_hotkey_sync_mock.return_value = (
            qt_settings_app.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                "sogou",
                False,
                "write_failed",
                message="Sogou rejected shortcut",
            )
        )

        controller.holdVoiceHotkeyText = "lctrl+lshift+f9"

        self.assertEqual(controller.holdVoiceHotkeyText, previous)
        self.assertIn("Sogou rejected", controller.errorMessage)

    def test_reentering_the_current_provider_refreshes_its_shortcut(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        self._voice_hotkey_read_mock.reset_mock()

        controller.selectedVoiceProgramIndex = 1

        self._voice_hotkey_read_mock.assert_called_once_with("sogou")
        self.assertFalse(controller.voiceHotkeyBusy)

    def test_reentering_the_same_hotkey_still_synchronizes_the_provider(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        current = controller.holdVoiceHotkeyText
        self._voice_hotkey_sync_mock.reset_mock()

        controller.holdVoiceHotkeyText = current

        self._voice_hotkey_sync_mock.assert_called_once_with("sogou", current)
        self.assertFalse(controller.voiceHotkeyBusy)

    def test_provider_sync_exception_keeps_the_saved_shortcut_and_clears_busy(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        previous = controller.holdVoiceHotkeyText
        self._voice_hotkey_sync_mock.side_effect = OSError("provider unavailable")

        controller.holdVoiceHotkeyText = "lctrl+lshift+f9"

        self.assertEqual(controller.holdVoiceHotkeyText, previous)
        self.assertFalse(controller.voiceHotkeyBusy)
        self.assertIn("provider unavailable", controller.errorMessage)

    def test_failed_provider_value_adoption_restores_the_saved_display(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        previous = controller.holdVoiceHotkeyText
        self._voice_hotkey_sync_mock.side_effect = None
        self._voice_hotkey_sync_mock.return_value = (
            qt_settings_app.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                "sogou",
                False,
                "rollback_failed",
                "lctrl+lshift+f8",
                "provider remains changed",
            )
        )

        with mock.patch.object(
            config, "save_config", side_effect=OSError("settings file is locked")
        ):
            controller.holdVoiceHotkeyText = "lctrl+lshift+f9"

        self.assertEqual(controller.holdVoiceHotkeyText, previous)
        self.assertIn("两边仍不一致", controller.errorMessage)

    def test_readback_failure_restores_disk_ui_and_provider_to_previous_hotkey(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        previous = controller.holdVoiceHotkeyText
        config_path = config.config_path(config.config_root())
        previous_bytes = config_path.read_bytes()
        self._voice_hotkey_sync_mock.side_effect = [
            qt_settings_app.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                "sogou",
                True,
                "synced",
                "lctrl+lshift+f9",
                "shortcut synchronized",
            ),
            qt_settings_app.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                "sogou",
                True,
                "synced",
                previous,
                "shortcut restored",
            ),
        ]

        with mock.patch.object(
            config,
            "load_config",
            side_effect=config.ConfigFormatError("readback failed"),
        ):
            controller.holdVoiceHotkeyText = "lctrl+lshift+f9"

        self.assertEqual(config_path.read_bytes(), previous_bytes)
        self.assertEqual(controller.holdVoiceHotkeyText, previous)
        self.assertFalse(controller.voiceHotkeyBusy)
        self.assertIn("语音程序快捷键已恢复原值", controller.errorMessage)
        self.assertEqual(
            self._voice_hotkey_sync_mock.call_args_list,
            [
                mock.call("sogou", "lctrl+lshift+f9"),
                mock.call("sogou", previous),
            ],
        )

    def test_failed_local_save_reports_when_provider_rollback_also_fails(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        previous = controller.holdVoiceHotkeyText
        self._voice_hotkey_sync_mock.side_effect = [
            qt_settings_app.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                "sogou",
                True,
                "synced",
                "lctrl+lshift+f9",
                "shortcut synchronized",
            ),
            qt_settings_app.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                "sogou",
                False,
                "write_failed",
                message="rollback failed",
            ),
        ]

        with mock.patch.object(
            config, "save_config", side_effect=OSError("settings file is locked")
        ):
            controller.holdVoiceHotkeyText = "lctrl+lshift+f9"

        self.assertEqual(controller.holdVoiceHotkeyText, previous)
        self.assertIn("语音设置保存失败", controller.errorMessage)
        self.assertIn("第三方程序快捷键也未能恢复", controller.errorMessage)
        self.assertEqual(
            self._voice_hotkey_sync_mock.call_args_list,
            [
                mock.call("sogou", "lctrl+lshift+f9"),
                mock.call("sogou", previous),
            ],
        )

    def test_refresh_adopts_an_external_provider_change(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        self._voice_hotkey_read_mock.side_effect = None
        self._voice_hotkey_read_mock.return_value = (
            qt_settings_app.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                "sogou",
                True,
                "read",
                "lctrl+lshift+f9",
                "read from Sogou",
            )
        )

        controller.refreshVoiceHotkeyFromProvider()

        self.assertEqual(controller.holdVoiceHotkeyText, "lctrl+lshift+f9")
        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(
            saved["voice_hotkeys_by_provider"]["sogou"]["hold"],
            "lctrl+lshift+f9",
        )

    def test_refreshing_local_only_provider_does_not_leave_processing_status(self):
        controller, _ = self._make_controller()
        controller._set_status_message("existing voice result", 2)

        controller.refreshVoiceHotkeyFromProvider()

        self.assertFalse(controller.voiceHotkeyBusy)
        self.assertEqual(controller.statusMessage, "existing voice result")
        self._voice_hotkey_read_mock.assert_not_called()

    def test_selecting_windows_dictation_uses_system_management_and_win_h_helper(self):
        controller, _ = self._make_controller()

        controller.selectedVoiceProgramIndex = 3
        controller.holdVoiceHotkeyText = "ctrl+shift+x"

        self.assertTrue(controller.voiceProgramSystemManaged)
        self.assertFalse(controller.voiceProgramLaunchOnBridgeStart)
        self.assertEqual(controller.holdVoiceHotkeyText, "win+h")
        self.assertFalse(controller.settingsDirty)
        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(saved["voice_hotkeys"], {"hold": "win+h"})

    def test_voice_program_options_keep_missing_apps_and_mark_them(self):
        controller, _ = self._make_controller()

        def inspect(settings):
            provider_id = settings["provider"]
            return voice_program_manager.VoiceProgramStatus(
                provider_id=provider_id,
                display_name=voice_program_manager.VOICE_PROGRAM_PROVIDER_NAMES[
                    provider_id
                ],
                available=provider_id != voice_program_manager.VOICE_PROGRAM_SOGOU,
                running=False,
                elevated=None,
                executable=None,
                code=(
                    "not_found"
                    if provider_id == voice_program_manager.VOICE_PROGRAM_SOGOU
                    else "stopped"
                ),
            )

        with mock.patch.object(
            qt_settings_app.voice_program_manager,
            "inspect_voice_program",
            side_effect=inspect,
        ):
            controller.refreshVoiceProgramOptions()

        self.assertEqual(controller.voiceProgramOptions[1], "搜狗语音输入（未安装）")
        self.assertEqual(controller.voiceProgramOptions[2], "微信输入法")
        self.assertEqual(len(controller.voiceProgramOptions), 5)

    def test_unrelated_mapping_edit_does_not_mark_voice_program_dirty(self):
        controller, model = self._make_controller()

        model.setActionTextAt(model.index_of("power"), "f5")

        self.assertTrue(controller.settingsDirty)
        self.assertFalse(controller.voiceProgramSettingsDirty)

    def test_disabling_management_keeps_a_disabled_elevation_preference_in_memory(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        controller.voiceProgramLaunchElevated = False

        controller.selectedVoiceProgramIndex = 0

        self.assertFalse(controller.voiceProgramLaunchOnBridgeStart)
        self.assertFalse(controller.voiceProgramLaunchElevated)

    def test_disabled_management_keeps_a_disabled_elevation_preference_after_reopen(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        controller.voiceProgramLaunchElevated = False
        controller.selectedVoiceProgramIndex = 0

        self.assertTrue(controller.saveSettings())

        reopened, _ = self._make_controller()
        self.assertEqual(reopened.selectedVoiceProgramIndex, 0)
        self.assertFalse(reopened.voiceProgramLaunchOnBridgeStart)
        self.assertFalse(reopened.voiceProgramLaunchElevated)

    def test_sogou_and_custom_elevation_preferences_are_remembered_separately(self):
        controller, _ = self._make_controller()

        controller.selectedVoiceProgramIndex = 1
        self.assertTrue(controller.voiceProgramLaunchElevated)
        controller.voiceProgramLaunchElevated = False
        controller.selectedVoiceProgramIndex = 4
        self.assertFalse(controller.voiceProgramLaunchElevated)
        controller.voiceProgramLaunchElevated = True
        controller.selectedVoiceProgramIndex = 1
        self.assertFalse(controller.voiceProgramLaunchElevated)
        controller.selectedVoiceProgramIndex = 4
        self.assertTrue(controller.voiceProgramLaunchElevated)

        reopened, _ = self._make_controller()
        self.assertEqual(reopened.selectedVoiceProgramIndex, 4)
        self.assertTrue(reopened.voiceProgramLaunchElevated)
        reopened.selectedVoiceProgramIndex = 1
        self.assertFalse(reopened.voiceProgramLaunchElevated)

    def test_existing_managed_program_without_autostart_is_preserved(self):
        saved = config.default_config()
        saved["voice_program"] = {
            "provider": "sogou",
            "custom_executable": "",
            "launch_on_bridge_start": False,
            "launch_elevated": False,
        }
        config.save_config(config.config_path(config.config_root()), saved)

        controller, _ = self._make_controller()

        self.assertFalse(controller.voiceProgramLaunchOnBridgeStart)
        self.assertFalse(controller.voiceProgramLaunchElevated)
        self.assertFalse(controller.voiceProgramSettingsDirty)
        self.assertFalse(controller.settingsDirty)
        self.assertNotIn("随桥接启动", controller.statusMessage)

    def test_voice_program_settings_persist_without_the_mapping_save(self):
        executable = Path(self._tmpdir.name) / "voice.exe"
        executable.touch()
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 4
        controller.voiceProgramCustomPath = str(executable)
        controller.voiceProgramLaunchOnBridgeStart = True
        controller.voiceProgramLaunchElevated = True

        self.assertFalse(controller.settingsDirty)
        self.assertFalse(controller.voiceProgramSettingsDirty)

        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(
            saved["voice_program"],
            {
                "provider": "custom",
                "custom_executable": str(executable),
                "launch_on_bridge_start": True,
                "launch_elevated": True,
                "launch_elevated_by_provider": {
                    "sogou": True,
                    "custom": True,
                },
            },
        )

    def test_voice_auto_save_failure_restores_the_last_saved_values(self):
        controller, _ = self._make_controller()

        with mock.patch.object(
            config, "save_config", side_effect=OSError("settings file is locked")
        ):
            controller.selectedVoiceProgramIndex = 1

        self.assertEqual(controller.selectedVoiceProgramIndex, 0)
        self.assertFalse(controller.voiceProgramSettingsDirty)
        self.assertIn("语音设置保存失败", controller.errorMessage)

        with mock.patch.object(
            config, "save_config", side_effect=OSError("settings file is locked")
        ):
            controller.holdVoiceHotkeyText = "ctrl+l"

        self.assertEqual(controller.holdVoiceHotkeyText, "ralt")
        self.assertIn("语音设置保存失败", controller.errorMessage)

    def test_mapping_save_is_rejected_during_voice_hotkey_work(self):
        controller, _ = self._make_controller()
        controller._set_voice_hotkey_busy(True)

        with mock.patch.object(config, "save_settings_pair") as save_pair:
            self.assertFalse(controller.saveSettings())

        save_pair.assert_not_called()
        self.assertIn("语音快捷键正在处理", controller.errorMessage)

    def test_voice_settings_do_not_start_during_conflicting_audio_work(self):
        blockers = (
            (
                "channel_test",
                lambda controller: qt_settings_app._vb_cable_test_active_event.set(),
                lambda controller: qt_settings_app._vb_cable_test_active_event.clear(),
            ),
            (
                "bridge_launch",
                lambda controller: controller._set_bridge_launch_phase("starting"),
                lambda controller: controller._set_bridge_launch_phase("idle"),
            ),
            (
                "endpoint_preflight",
                lambda controller: setattr(controller, "_endpoint_preflight_busy", True),
                lambda controller: setattr(controller, "_endpoint_preflight_busy", False),
            ),
            (
                "driver_action",
                lambda controller: qt_settings_app._driver_action_active_event.set(),
                lambda controller: qt_settings_app._driver_action_active_event.clear(),
            ),
        )

        for name, start, stop in blockers:
            with self.subTest(name=name):
                controller, _ = self._make_controller()
                self._voice_hotkey_read_mock.reset_mock()
                self._voice_hotkey_sync_mock.reset_mock()
                start(controller)
                try:
                    controller.holdVoiceHotkeyText = "ctrl+l"
                    controller.selectedVoiceProgramIndex = 1
                    controller.refreshVoiceHotkeyFromProvider()
                finally:
                    stop(controller)

                self.assertEqual(controller.holdVoiceHotkeyText, "ralt")
                self.assertEqual(controller.selectedVoiceProgramIndex, 0)
                self._voice_hotkey_read_mock.assert_not_called()
                self._voice_hotkey_sync_mock.assert_not_called()

    def test_mapping_save_is_rejected_during_audio_configuration_work(self):
        blockers = (
            (
                "bridge_launch",
                lambda controller: controller._set_bridge_launch_phase("starting"),
                lambda controller: controller._set_bridge_launch_phase("idle"),
            ),
            (
                "endpoint_preflight",
                lambda controller: setattr(controller, "_endpoint_preflight_busy", True),
                lambda controller: setattr(controller, "_endpoint_preflight_busy", False),
            ),
            (
                "driver_action",
                lambda controller: qt_settings_app._driver_action_active_event.set(),
                lambda controller: qt_settings_app._driver_action_active_event.clear(),
            ),
        )

        for name, start, stop in blockers:
            with self.subTest(name=name):
                controller, _ = self._make_controller()
                start(controller)
                try:
                    with mock.patch.object(controller, "_save") as save:
                        self.assertFalse(controller.saveSettings())
                finally:
                    stop(controller)

                save.assert_not_called()
                self.assertIn("其它操作正在进行", controller.errorMessage)

    def test_voice_program_launch_reports_a_normal_provider_result(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        result = voice_program_manager.VoiceProgramLaunchResult(
            provider_id="sogou",
            started=True,
            already_running=False,
            code="started",
            elevated=True,
        )
        with mock.patch.object(
            qt_settings_app.voice_program_manager,
            "launch_voice_program",
            return_value=result,
        ):
            controller.launchVoiceProgram()

        self.assertIn("管理员权限", controller.statusMessage)
        self.assertEqual(controller.errorMessage, "")

    def test_existing_right_alt_hotkey_is_preserved_when_loading_saved_config(self):
        saved = config.default_config()
        saved["voice_hotkey"] = "ralt"
        saved["voice_hotkeys"] = {"hold": "ralt"}
        config.save_config(config.config_path(config.config_root()), saved)

        controller, _ = self._make_controller()

        self.assertEqual(controller.holdVoiceHotkeyText, "ralt")

    def test_only_mic_primary_options_include_hold_to_talk(self):
        controller, _ = self._make_controller()
        self.assertNotIn(settings_ui._VOICE_HOLD_DISPLAY, controller.primaryActionOptions)
        self.assertNotIn(
            "开关型语音",
            controller.primaryActionOptionsFor("mic"),
        )
        self.assertIn(
            settings_ui._VOICE_HOLD_DISPLAY,
            controller.primaryActionOptionsFor("mic"),
        )
        self.assertNotIn(
            settings_ui._VOICE_HOLD_DISPLAY,
            controller.primaryActionOptionsFor("up"),
        )

    def test_secondary_options_exclude_voice_lifecycles(self):
        controller, _ = self._make_controller()
        self.assertNotIn(
            "开关型语音",
            controller.secondaryActionOptions,
        )
        self.assertNotIn(
            settings_ui._VOICE_HOLD_DISPLAY,
            controller.secondaryActionOptions,
        )
        self.assertIn(
            settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
            controller.secondaryActionOptions,
        )
        self.assertNotIn("禁用", controller.secondaryActionOptions)

    def test_element_navigation_is_available_to_every_mapping_entry(self):
        controller, _ = self._make_controller()
        self.assertIn("元素导航开关", controller.primaryActionOptions)
        self.assertIn(
            "元素导航开关",
            controller.primaryActionOptionsFor("mic"),
        )
        self.assertIn("元素导航开关", controller.secondaryActionOptions)

    def test_application_display_name_comes_from_product_identity(self):
        controller, _ = self._make_controller()
        self.assertEqual(
            controller.applicationDisplayName,
            product_identity.DISPLAY_NAME,
        )

    def test_action_group_metadata_only_marks_real_group_starts(self):
        controller, _ = self._make_controller()
        self.assertEqual(controller.actionOptionGroupTitle("Escape"), "按键操作")
        self.assertTrue(controller.actionOptionStartsGroup("Escape"))
        self.assertEqual(
            controller.actionOptionGroupTitle("元素导航开关"),
            "鼠标与导航",
        )
        self.assertTrue(controller.actionOptionStartsGroup("元素导航开关"))
        self.assertEqual(controller.actionOptionGroupTitle("方向上"), "按键操作")
        self.assertFalse(controller.actionOptionStartsGroup("方向上"))
        self.assertEqual(controller.actionOptionGroupTitle("未设置"), "")
        self.assertFalse(controller.actionOptionStartsGroup("未设置"))

    def test_combo_rows_cover_the_supported_second_keys(self):
        controller, _ = self._make_controller()
        self.assertEqual(controller.comboModifierOptions, ["TV", "菜单", "主页"])
        self.assertEqual(controller.comboModifierIndex, 0)
        self.assertEqual(
            [row["buttonId"] for row in controller.comboRows],
            list(key_mapping.COMBO_ACTION_BUTTON_IDS),
        )

    def test_combo_edits_persist_with_the_mapping_save(self):
        controller, _ = self._make_controller()
        controller.comboModifierIndex = 1
        controller.setComboActionText("up", "quicker:runaction:pin-window")
        controller.setComboNoteText("up", "置顶窗口")

        self.assertTrue(controller.settingsDirty)
        self.assertTrue(controller.saveSettings())

        saved = config.load_key_bindings(config.key_bindings_path(config.config_root()))
        self.assertEqual(saved["combo_bindings"]["modifier"], "menu")
        self.assertEqual(
            saved["combo_bindings"]["bindings"]["up"]["uri"],
            "quicker:runaction:pin-window",
        )
        self.assertEqual(saved["combo_bindings"]["display_notes"], {"up": "置顶窗口"})

    def test_recording_a_hotkey_does_not_change_trigger_semantics(self):
        controller, _ = self._make_controller()
        captured = []
        controller.hotkeyCaptured.connect(captured.append)
        controller._hotkey_capture = object()
        controller._set_input_operation_state("hotkey", "active")
        controller._on_hotkey_capture_result("lctrl+lwin")
        self.assertEqual(captured, ["lctrl+lwin"])
        controller.hotkeyText = "lctrl+lwin"
        self.assertEqual(controller.hotkeyText, "lctrl+lwin")

    def test_late_hotkey_results_are_ignored_after_cleanup_or_exit_begins(self):
        cases = (
            "stopping",
            "cleanup",
            "hide",
            "exit_requested",
            "exit_confirmed",
            "exit_intent",
        )
        for case in cases:
            with self.subTest(case=case):
                controller, _ = self._make_controller()
                captured = []
                controller.hotkeyCaptured.connect(captured.append)
                controller._hotkey_capture = object()
                controller._set_input_operation_state("hotkey", "active")
                if case == "stopping":
                    controller._set_input_operation_state("hotkey", "stopping")
                elif case == "cleanup":
                    controller._input_cleanup_requested = True
                elif case == "hide":
                    controller._window_hide_requested = True
                elif case == "exit_requested":
                    controller._application_exit_requested = True
                elif case == "exit_confirmed":
                    controller._application_exit_confirmed = True
                else:
                    controller._application_exit_intent.set()

                controller._on_hotkey_capture_result("ctrl+alt+n")

                self.assertEqual(captured, [])

    def test_launch_status_starts_as_the_not_started_constant(self):
        controller, _ = self._make_controller()
        self.assertEqual(controller.launchStatusText, settings_ui.LAUNCH_NOT_STARTED_TEXT)
        self.assertFalse(controller.bridgeRunning)

    def test_launch_status_recognizes_an_existing_bridge(self):
        self._bridge_status_patch.stop()
        self._bridge_status_patch = mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=True,
        )
        self._bridge_status_patch.start()

        controller, _ = self._make_controller()

        self.assertTrue(controller.bridgeRunning)
        self.assertFalse(controller.bridgeConnected)
        self.assertEqual(controller.bridgeLaunchPhase, "waiting")
        self.assertIn("服务运行中", controller.launchStatusText)
        self.assertIn("状态未知", controller.launchStatusText)

    def test_launch_status_reads_the_bridge_reported_connected_state(self):
        self._bridge_status_patch.stop()
        self._bridge_status_patch = mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=True,
        )
        self._bridge_status_patch.start()
        bridge_runtime_status.publish_status(
            config.config_root(),
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=4321,
        )

        controller, _ = self._make_controller()

        self.assertTrue(controller.bridgeRunning)
        self.assertTrue(controller.bridgeConnected)
        self.assertEqual(controller.bridgeLaunchPhase, "connected")
        self.assertIn("小米遥控器2 Pro 已连接", controller.launchStatusText)

    def test_current_bridge_status_exposes_version_channels_and_recent_button(self):
        self._bridge_status_patch.stop()
        self._bridge_status_patch = mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=True,
        )
        self._bridge_status_patch.start()
        identity = bridge_runtime_status.current_runtime_identity(
            qt_settings_app.__version__
        )
        bridge_runtime_status.publish_status(
            config.config_root(),
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=4321,
            identity=identity,
            raw_input_state="ready",
            hid_tap_state=frida_compat.HidTapState.READY.value,
            last_button_at=time.time(),
            last_button_source="hid",
        )

        controller, _ = self._make_controller()

        self.assertIn("当前版本", controller.launchStatusText)
        self.assertIn("两个按键通道正常", controller.launchStatusText)
        self.assertIn("刚收到按键", controller.launchStatusText)
        self.assertFalse(controller.bridgeRestartRecommended)

    def test_legacy_bridge_recommends_manual_restart_without_auto_stopping(self):
        self._bridge_status_patch.stop()
        self._bridge_status_patch = mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=True,
        )
        self._bridge_status_patch.start()
        path = bridge_runtime_status.status_path(config.config_root())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"schema":1,"state":"connected","pid":4321,"updated_at":1}',
            encoding="utf-8",
        )

        controller, _ = self._make_controller()
        callbacks = []
        with mock.patch(
            "PySide6.QtCore.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            controller.refreshBridgeState()

        self.assertTrue(controller.bridgeRestartRecommended)
        self.assertEqual(callbacks, [])

    def test_mismatched_idle_bridge_schedules_only_one_controlled_recovery(self):
        self._bridge_status_patch.stop()
        self._bridge_status_patch = mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=True,
        )
        self._bridge_status_patch.start()
        identity = bridge_runtime_status.current_runtime_identity(
            qt_settings_app.__version__,
            frozen=False,
            source_root=Path(self._tmpdir.name) / "other-build",
        )
        bridge_runtime_status.publish_status(
            config.config_root(),
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=4321,
            identity=identity,
            raw_input_state="ready",
            hid_tap_state=frida_compat.HidTapState.READY.value,
        )
        controller, _ = self._make_controller()
        callbacks = []
        recoveries = []

        with mock.patch(
            "PySide6.QtCore.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            controller.refreshBridgeState()
            controller.refreshBridgeState()
            self.assertEqual(len(callbacks), 1)
            with mock.patch.object(
                controller,
                "_begin_bridge_restart",
                side_effect=lambda **kwargs: recoveries.append(kwargs),
            ):
                callbacks.pop()()

        self.assertEqual(recoveries, [{"automatic": True}])

    def test_manual_restart_refuses_to_interrupt_active_voice(self):
        self._bridge_status_patch.stop()
        self._bridge_status_patch = mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=True,
        )
        self._bridge_status_patch.start()
        identity = bridge_runtime_status.current_runtime_identity(
            qt_settings_app.__version__
        )
        bridge_runtime_status.publish_status(
            config.config_root(),
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=4321,
            identity=identity,
            voice_active=True,
        )
        controller, _ = self._make_controller()

        with mock.patch.object(
            qt_settings_app.bridge_control_windows,
            "request_bridge_exit",
        ) as request_exit:
            controller.restartBridge()

        request_exit.assert_not_called()
        self.assertIn("正在语音输入", controller.errorMessage)

    def test_live_bridge_refresh_tracks_external_start_and_exit(self):
        controller, _ = self._make_controller()

        with mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=True,
        ):
            controller.refreshBridgeState()

        self.assertTrue(controller.bridgeRunning)
        self.assertEqual(controller.bridgeLaunchPhase, "waiting")
        self.assertIn("状态未知", controller.launchStatusText)

        controller.refreshBridgeState()

        self.assertFalse(controller.bridgeRunning)
        self.assertEqual(
            controller.launchStatusText,
            settings_ui.LAUNCH_NOT_STARTED_TEXT,
        )

    def test_bridge_refresh_does_not_mistake_active_loopback_guard_for_bridge(self):
        controller, _ = self._make_controller()
        qt_settings_app._vb_cable_test_active_event.set()
        try:
            with mock.patch.object(
                qt_settings_app.single_instance,
                "bridge_instance_running",
            ) as status_probe:
                running = controller._refresh_bridge_status()
        finally:
            qt_settings_app._vb_cable_test_active_event.clear()

        self.assertFalse(running)
        self.assertFalse(controller.bridgeRunning)
        status_probe.assert_not_called()

    def test_live_bridge_refresh_recovers_after_an_unknown_initial_state(self):
        self._bridge_status_patch.stop()
        self._bridge_status_patch = mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            side_effect=single_instance.SingleInstanceUnavailableError(
                "status unavailable"
            ),
        )
        self._bridge_status_patch.start()
        controller, _ = self._make_controller()
        self.assertEqual(
            controller.launchStatusText,
            settings_ui.LAUNCH_STATUS_UNKNOWN_TEXT,
        )

        with mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=False,
        ):
            controller.refreshBridgeState()

        self.assertFalse(controller.bridgeRunning)
        self.assertEqual(
            controller.launchStatusText,
            settings_ui.LAUNCH_NOT_STARTED_TEXT,
        )

    def test_unsupported_saved_wdmks_endpoint_preselects_preferred_cable_input(self):
        saved = config.default_config()
        saved["output_endpoint_name"] = "Output (VB-Audio Point)"
        saved["output_endpoint_host_api"] = "Windows WDM-KS"
        config.save_config(config.config_path(config.config_root()), saved)
        endpoints = [
            audio_output.AudioEndpoint(
                name="CABLE Input (VB-Audio Virtual Cable)",
                host_api="Windows DirectSound",
            ),
            audio_output.AudioEndpoint(
                name="CABLE Input (VB-Audio Virtual Cable)",
                host_api="Windows WASAPI",
            ),
        ]

        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=endpoints
        ):
            controller, _ = self._make_controller()

        selected = controller.endpointOptions[controller.selectedEndpointIndex]
        self.assertIn("CABLE Input", selected)
        self.assertIn("Windows WASAPI", selected)
        self.assertIn("WDM-KS", controller.statusMessage)
        self.assertIn("点击保存后才会写入", controller.statusMessage)
        self.assertTrue(controller.settingsDirty)

    def test_endpoint_recommendation_prefers_unique_cable_input_wasapi(self):
        endpoints = [
            audio_output.AudioEndpoint(
                name="CABLE Input (VB-Audio Virtual Cable)",
                host_api="Windows DirectSound",
            ),
            audio_output.AudioEndpoint(
                name="Speakers",
                host_api="Windows WASAPI",
            ),
            audio_output.AudioEndpoint(
                name="CABLE Input (VB-Audio Virtual Cable)",
                host_api="Windows WASAPI",
            ),
        ]

        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=endpoints
        ):
            controller, _ = self._make_controller()

        self.assertEqual(
            controller.endpointOptions[controller.recommendedEndpointIndex],
            settings_ui._endpoint_display(endpoints[2]),
        )
        self.assertTrue(
            all("推荐" not in option for option in controller.endpointOptions),
            "the display-only recommendation must never pollute saved endpoint text",
        )

    def test_endpoint_recommendation_falls_back_to_unique_directsound_cable_input(self):
        endpoints = [
            audio_output.AudioEndpoint(
                name="Speakers",
                host_api="Windows WASAPI",
            ),
            audio_output.AudioEndpoint(
                name="CABLE Input",
                host_api="Windows DirectSound",
            ),
        ]

        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=endpoints
        ):
            controller, _ = self._make_controller()

        self.assertGreaterEqual(controller.recommendedEndpointIndex, 0)
        self.assertEqual(
            controller.endpointOptions[controller.recommendedEndpointIndex],
            settings_ui._endpoint_display(endpoints[1]),
        )

    def test_endpoint_recommendation_fails_closed_for_unsupported_or_ambiguous_matches(self):
        cases = (
            [
                audio_output.AudioEndpoint(
                    name="CABLE Input",
                    host_api="MME",
                )
            ],
            [
                audio_output.AudioEndpoint(
                    name="CABLE Input",
                    host_api="Windows WASAPI",
                ),
                audio_output.AudioEndpoint(
                    name="CABLE Input",
                    host_api="Windows WASAPI",
                ),
            ],
            [
                audio_output.AudioEndpoint(
                    name="CABLE Input Splitter Pro",
                    host_api="Windows WASAPI",
                )
            ],
        )

        for endpoints in cases:
            with self.subTest(endpoints=endpoints), mock.patch.object(
                audio_output, "enumerate_output_endpoints", return_value=endpoints
            ):
                controller, _ = self._make_controller()
                self.assertEqual(controller.recommendedEndpointIndex, -1)

    def test_missing_saved_endpoint_is_not_recommended_and_does_not_hide_live_recommendation(self):
        saved = config.default_config()
        saved["output_endpoint_name"] = "Missing Speakers"
        saved["output_endpoint_host_api"] = "Windows WASAPI"
        config.save_config(config.config_path(config.config_root()), saved)
        live_endpoint = audio_output.AudioEndpoint(
            name="CABLE Input",
            host_api="Windows WASAPI",
        )

        with mock.patch.object(
            audio_output,
            "enumerate_output_endpoints",
            return_value=[live_endpoint],
        ):
            controller, _ = self._make_controller()

        self.assertEqual(controller.selectedEndpointIndex, 0)
        self.assertEqual(controller.recommendedEndpointIndex, 1)
        self.assertIn("Missing Speakers", controller.endpointOptions[0])
        self.assertEqual(
            controller.endpointOptions[1],
            settings_ui._endpoint_display(live_endpoint),
        )

    def test_photo_available_and_source_are_consistent(self):
        controller, _ = self._make_controller()
        # This repository ships Resources/RC003-remote-photo.png, so this
        # should be true in every real checkout - but the assertion is
        # written to hold either way (never a stretched/fake image, see
        # task DoD): availability and a non-empty source string must agree.
        self.assertEqual(controller.photoAvailable, bool(controller.photoSource))

    def test_device_selector_defaults_to_rc003_for_existing_users(self):
        controller, _ = self._make_controller()
        self.assertEqual(
            controller.deviceOptions,
            [device_catalog.profile_for(device_catalog.RC003_ID).display_name],
        )
        self.assertEqual(
            controller.selectedDeviceIndex,
            controller._DEVICE_ORDER.index(device_catalog.RC003_ID),
        )
        self.assertTrue(controller.isRc003Device)
        self.assertEqual(controller.mappingPageTitle, "按键映射")

    def test_legacy_dji_selection_falls_back_to_rc003_without_rewriting_on_open(self):
        saved = config.default_config()
        saved["selected_device_profile"] = device_catalog.DJI_MIC_2_ID
        path = config.config_path(config.config_root())
        config.save_config(path, saved)

        controller, _ = self._make_controller()

        self.assertEqual(
            controller.deviceOptions,
            [device_catalog.profile_for(device_catalog.RC003_ID).display_name],
        )
        self.assertTrue(controller.isRc003Device)
        self.assertEqual(
            config.load_config(path)["selected_device_profile"],
            device_catalog.DJI_MIC_2_ID,
        )

    def test_save_settings_persists_and_clears_error_message(self):
        controller, model = self._make_controller()
        model.setActionTextAt(model.index_of("power"), "escape")
        self.assertTrue(controller.settingsDirty)
        self.assertTrue(controller.saveSettings())
        self.assertFalse(controller.settingsDirty)
        self.assertEqual(controller.errorMessage, "")
        self.assertIn("已保存", controller.statusMessage)

        model.setActionTextAt(model.index_of("power"), "Return")
        self.assertTrue(controller.settingsDirty)
        self.assertEqual(controller.statusMessage, "")

    def test_display_note_persists_after_save_and_reopen(self):
        controller, model = self._make_controller()
        power_row = model.index_of("power")
        model.setActionTextAt(power_row, "ctrl+c")
        model.setDisplayNoteAt(power_row, "single_click", "复制")

        self.assertTrue(controller.saveSettings())

        _, reopened_model = self._make_controller()
        self.assertEqual(
            reopened_model.to_display_note_map()["power"]["single_click"],
            "复制",
        )
        self.assertEqual(reopened_model.to_display_map()["power"], "ctrl+c")

    def test_failed_save_keeps_the_last_persisted_display_note(self):
        controller, model = self._make_controller()
        power_row = model.index_of("power")
        model.setDisplayNoteAt(power_row, "single_click", "复制")
        self.assertTrue(controller.saveSettings())

        model.setDisplayNoteAt(power_row, "single_click", "粘贴")
        with mock.patch.object(
            config,
            "save_settings_pair",
            side_effect=OSError("settings file is locked"),
        ):
            self.assertFalse(controller.saveSettings())

        persisted = config.load_key_bindings(
            config.key_bindings_path(config.config_root())
        )
        self.assertEqual(
            persisted["display_notes"]["power"]["single_click"],
            "复制",
        )

    def test_save_settings_reports_a_persistence_failure(self):
        controller, model = self._make_controller()
        model.setActionTextAt(model.index_of("power"), "ctrl+l")
        self.assertTrue(controller.settingsDirty)
        with mock.patch.object(
            config, "save_settings_pair", side_effect=OSError("settings file is locked")
        ):
            self.assertFalse(controller.saveSettings())
        self.assertTrue(controller.settingsDirty)
        self.assertIn("保存失败", controller.errorMessage)
        self.assertIn("settings file is locked", controller.errorMessage)

    def test_voice_controls_auto_save_without_clearing_mapping_edits(self):
        controller, model = self._make_controller()
        self.assertFalse(controller.settingsDirty)

        model.setSecondaryActionTextAt(
            model.index_of("power"), "long_press", "f5"
        )
        self.assertTrue(controller.settingsDirty)
        self.assertTrue(controller.saveSettings())
        self.assertFalse(controller.settingsDirty)

        controller.holdVoiceHotkeyText = "ctrl+l"
        self.assertFalse(controller.settingsDirty)
        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(saved["voice_hotkeys"], {"hold": "ctrl+l"})

        model.setActionTextAt(model.index_of("power"), "escape")
        self.assertTrue(controller.settingsDirty)

        controller.selectedVoiceProgramIndex = 1
        self.assertTrue(controller.settingsDirty)
        self.assertIn("按键映射仍未保存", controller.statusMessage)
        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(saved["voice_program"]["provider"], "sogou")

    def test_feedback_is_scoped_to_the_page_that_owns_the_action(self):
        controller, model = self._make_controller()

        controller.activePageIndex = 0
        controller.holdVoiceHotkeyText = "ctrl+l"
        self.assertEqual(controller.feedbackPageIndex, 2)

        controller.activePageIndex = 2
        model.setActionTextAt(model.index_of("power"), "escape")
        self.assertTrue(controller.saveSettings())
        self.assertEqual(controller.feedbackPageIndex, 1)

        opened = shell_targets.ExternalTargetResult(
            outcome=shell_targets.ExternalTargetOutcome.OPENED,
            target=shell_targets.BLUETOOTH_SETTINGS_URI,
            error="",
        )
        with mock.patch.object(
            shell_targets,
            "open_external_target",
            return_value=opened,
        ):
            controller.openBluetoothSettings()
        self.assertEqual(controller.feedbackPageIndex, 0)

        controller.activePageIndex = 0
        with mock.patch.object(
            shell_targets,
            "open_external_target",
            return_value=opened,
        ):
            controller.openMicrophonePrivacySettings()
        self.assertEqual(controller.feedbackPageIndex, 2)

    def test_output_endpoint_selection_auto_saves_without_mapping_changes(self):
        endpoints = [
            audio_output.AudioEndpoint(
                name="CABLE Input (VB-Audio Virtual Cable)",
                host_api="Windows WASAPI",
            ),
            audio_output.AudioEndpoint(
                name="Speakers",
                host_api="Windows WASAPI",
            ),
        ]
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=endpoints
        ), mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
        ) as preflight:
            controller, _ = self._make_controller()
            self.assertFalse(controller.settingsDirty)
            controller.selectedEndpointIndex = 0

        self.assertFalse(controller.settingsDirty)
        preflight.assert_called_once_with(
            "CABLE Input (VB-Audio Virtual Cable)",
            "Windows WASAPI",
            cancel_event=mock.ANY,
        )
        cancel_event = preflight.call_args.kwargs["cancel_event"]
        self.assertFalse(cancel_event.is_set())
        controller._application_exit_intent.set()
        self.assertTrue(cancel_event.is_set())
        controller._application_exit_intent.clear()
        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(
            saved["output_endpoint_name"],
            "CABLE Input (VB-Audio Virtual Cable)",
        )

    def test_output_endpoint_auto_save_failure_keeps_the_saved_selection(self):
        endpoints = [
            audio_output.AudioEndpoint(
                name="CABLE Input",
                host_api="Windows WASAPI",
            )
        ]
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=endpoints
        ):
            controller, _ = self._make_controller()

        with mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
        ), mock.patch.object(
            config, "save_config", side_effect=OSError("disk full")
        ):
            controller.selectedEndpointIndex = 0

        self.assertEqual(controller.selectedEndpointIndex, -1)
        self.assertIn("输出端点保存失败", controller.errorMessage)

    def test_async_save_does_not_overwrite_an_edit_made_during_preflight(self):
        saved_config = config.default_config()
        saved_config["output_endpoint_name"] = "CABLE Input"
        saved_config["output_endpoint_host_api"] = "Windows WASAPI"
        config.save_config(config.config_path(config.config_root()), saved_config)
        saved_bindings = config.default_key_bindings()
        saved_bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE,
        ).to_dict()
        config.save_key_bindings(
            config.key_bindings_path(config.config_root()),
            saved_bindings,
        )
        endpoint = audio_output.AudioEndpoint(
            name="CABLE Input",
            host_api="Windows WASAPI",
        )
        deferred = []

        def runner(target, name):
            if name == "audio-endpoint-preflight":
                deferred.append(target)
            else:
                target()

        with mock.patch.object(
            audio_output,
            "enumerate_output_endpoints",
            return_value=[endpoint],
        ), mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
        ), mock.patch.object(config, "save_settings_pair") as save_pair:
            model = self.Model()
            controller = self.Controller(
                model,
                background_task_runner=runner,
            )
            model.setActionTextAt(
                model.index_of("mic"),
                settings_ui._VOICE_HOLD_DISPLAY,
            )
            completion = []

            self.assertTrue(controller._save(completion=completion.append))
            self.assertTrue(controller.settingsSaveBusy)
            self.assertEqual(len(deferred), 1)

            model.setActionTextAt(model.index_of("power"), "f5")
            deferred.pop()()

        save_pair.assert_not_called()
        self.assertEqual(model.to_display_map()["power"], "f5")
        self.assertTrue(controller.settingsDirty)
        self.assertFalse(controller.settingsSaveBusy)
        self.assertEqual(completion, [False])
        self.assertIn("避免覆盖新修改", controller.errorMessage)

    def test_restore_defaults_marks_unsaved_changes(self):
        controller, _ = self._make_controller()
        self.assertFalse(controller.settingsDirty)

        controller.restoreDefaults()

        self.assertTrue(controller.settingsDirty)
        self.assertIn("尚未保存", controller.statusMessage)

    def test_legacy_toggle_error_uses_the_chinese_button_name(self):
        config_file = config.config_path(config.config_root())
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "voice_trigger_mode": "toggle",
                    "voice_hotkey": "lalt+space",
                    "voice_hotkeys": {
                        "toggle": "lalt+space",
                        "hold": "ralt+space",
                    },
                }
            ),
            encoding="utf-8",
        )
        bindings = config.default_key_bindings()
        bindings["bindings"]["mic"] = {"kind": "voice_toggle", "keys": []}
        config.save_key_bindings(
            config.key_bindings_path(config.config_root()), bindings
        )

        controller, _ = self._make_controller()

        self.assertTrue(controller.settingsDirty)
        self.assertFalse(controller.saveSettings())
        self.assertIn("「话筒键」映射无效", controller.errorMessage)
        self.assertNotIn("「mic」", controller.errorMessage)
        self.assertTrue(controller.settingsDirty)

    def test_save_settings_uses_hold_to_talk_on_the_mic_button(self):
        controller, model = self._make_controller()
        controller.holdVoiceHotkeyText = "ctrl+l"
        model.setActionTextAt(
            model.index_of("mic"),
            settings_ui._VOICE_HOLD_DISPLAY,
        )

        self.assertTrue(controller.saveSettings())

        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(saved["voice_trigger_mode"], "hold")
        self.assertEqual(saved["voice_hotkey"], "ctrl+l")
        self.assertEqual(saved["voice_hotkeys"], {"hold": "ctrl+l"})
        saved_bindings = config.load_key_bindings(
            config.key_bindings_path(config.config_root())
        )
        self.assertEqual(saved_bindings["bindings"]["mic"]["kind"], "voice_hold")

    def test_legacy_generic_voice_mapping_saves_as_explicit_selected_mode(self):
        saved_config = config.default_config()
        saved_config["voice_trigger_mode"] = "hold"
        saved_config["voice_hotkey"] = "ctrl+l"
        saved_config["voice_hotkeys"]["hold"] = "ctrl+l"
        config.save_config(config.config_path(config.config_root()), saved_config)
        saved_bindings = config.default_key_bindings()
        saved_bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE
        ).to_dict()
        config.save_key_bindings(
            config.key_bindings_path(config.config_root()),
            saved_bindings,
        )

        controller, model = self._make_controller()

        mic_index = model.index(model.index_of("mic"), 0)
        self.assertEqual(
            model.data(mic_index, model.ActionTextRole),
            settings_ui._VOICE_HOLD_DISPLAY,
        )
        self.assertTrue(controller.saveSettings())
        reloaded = config.load_key_bindings(
            config.key_bindings_path(config.config_root())
        )
        self.assertEqual(reloaded["bindings"]["mic"]["kind"], "voice_hold")

    def test_mic_ordinary_primary_and_secondary_actions_persist_and_reload(self):
        controller, model = self._make_controller()
        mic_row = model.index_of("mic")
        model.setActionTextAt(mic_row, "Escape")
        model.setSecondaryActionTextAt(mic_row, "double_click", "f5")
        model.setSecondaryActionTextAt(mic_row, "long_press", "系统音量 +")

        self.assertTrue(controller.saveSettings())

        reloaded_controller, reloaded_model = self._make_controller()
        del reloaded_controller
        mic_index = reloaded_model.index(reloaded_model.index_of("mic"), 0)
        self.assertEqual(reloaded_model.data(mic_index, reloaded_model.ActionTextRole), "Escape")
        self.assertEqual(reloaded_model.data(mic_index, reloaded_model.DoubleClickTextRole), "f5")
        self.assertEqual(
            reloaded_model.data(mic_index, reloaded_model.LongPressTextRole),
            "系统音量 +",
        )

    def test_zero_voice_mapping_skips_audio_endpoint_preflight(self):
        saved_config = config.default_config()
        saved_config["output_endpoint_name"] = "Missing CABLE Input"
        saved_config["output_endpoint_host_api"] = "Windows WASAPI"
        config.save_config(config.config_path(config.config_root()), saved_config)

        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=[]
        ):
            controller, model = self._make_controller()
        model.setActionTextAt(model.index_of("mic"), "Escape")
        controller.holdVoiceHotkeyText = ""

        with mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
            side_effect=AssertionError("preflight must be skipped without voice"),
        ) as preflight:
            self.assertTrue(controller.saveSettings())

        preflight.assert_not_called()
        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(saved["voice_hotkeys"], {"hold": "ralt"})

    def test_save_settings_with_empty_hotkey_fails_and_reports_error(self):
        controller, _ = self._make_controller()
        controller._voice_hotkeys[key_mapping.VoiceTriggerMode.HOLD] = ""
        self.assertFalse(controller.saveSettings())
        self.assertNotEqual(controller.errorMessage, "")

    def test_save_and_launch_never_launches_when_save_fails(self):
        controller, _ = self._make_controller()
        controller._voice_hotkeys[key_mapping.VoiceTriggerMode.HOLD] = ""
        with mock.patch.object(bridge_launcher, "start_bridge_launch") as fake_launch:
            controller.saveAndLaunch()
            self.assertEqual(controller.bridgeLaunchPhase, "saving")
            self.assertTrue(controller.bridgeLaunchBusy)
            self._continue_save_and_launch(controller)
        fake_launch.assert_not_called()
        self.assertEqual(controller.bridgeLaunchPhase, "failed")
        self.assertFalse(controller.bridgeLaunchBusy)
        self.assertIn("保存未完成", controller.launchStatusText)

    def test_save_and_launch_waits_for_async_endpoint_preflight(self):
        controller, _ = self._make_controller()
        completions = []
        controller._save = lambda completion=None: (
            completions.append(completion) or True
        )
        controller._start_bridge_process = mock.Mock()

        controller.saveAndLaunch()
        self._continue_save_and_launch(controller)

        controller._start_bridge_process.assert_not_called()
        self.assertEqual(controller.bridgeLaunchPhase, "saving")
        completions[0](True)
        controller._start_bridge_process.assert_called_once_with()

    def test_save_and_launch_launches_and_reports_started_when_save_succeeds(self):
        controller, _ = self._make_controller()
        fake_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.STARTED, command=("exe",), pid=4321
        )
        with mock.patch.object(
            bridge_launcher, "start_bridge_launch", return_value=fake_result
        ) as start_launch, mock.patch.object(
            bridge_launcher,
            "launch_bridge",
            side_effect=AssertionError("GUI must not call the blocking launch wrapper"),
        ), mock.patch.object(
            qt_settings_app.voice_program_manager,
            "launch_voice_program",
        ) as launch_voice_program:
            controller.saveAndLaunch()
            self.assertEqual(controller.bridgeLaunchPhase, "saving")
            self.assertFalse(start_launch.called)
            self._continue_save_and_launch(controller)
        launch_voice_program.assert_not_called()
        self.assertTrue(controller.bridgeRunning)
        self.assertFalse(controller.bridgeConnected)
        self.assertEqual(controller.bridgeLaunchPhase, "waiting")
        self.assertIn("服务运行中", controller.launchStatusText)
        self.assertIn("约 1 分钟", controller.launchStatusText)
        self.assertNotIn("已连接", controller.launchStatusText)

        controller.refreshBridgeState()

        self.assertFalse(controller.bridgeRunning)
        self.assertEqual(controller.bridgeLaunchPhase, "failed")
        self.assertIn("服务已退出", controller.launchStatusText)

    def test_device_page_start_bridge_does_not_save_unrelated_dirty_edits(self):
        controller, model = self._make_controller()
        model.setActionTextAt(model.index_of("power"), "f5")
        callbacks = []
        fake_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.STARTED,
            command=("exe",),
            pid=4321,
        )

        with mock.patch.object(controller, "_save") as save, mock.patch.object(
            bridge_launcher, "start_bridge_launch", return_value=fake_result
        ), mock.patch(
            "PySide6.QtCore.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            controller.startBridge()
            self.assertEqual(controller.bridgeLaunchPhase, "starting")
            self.assertEqual(len(callbacks), 1)
            callbacks.pop()()

        save.assert_not_called()
        self.assertTrue(controller.settingsDirty)
        self.assertTrue(controller.bridgeRunning)
        self.assertEqual(controller.bridgeLaunchPhase, "waiting")

    def test_start_bridge_double_press_queues_only_one_launch(self):
        controller, _ = self._make_controller()
        callbacks = []
        fake_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.STARTED,
            command=("exe",),
            pid=4321,
        )
        with mock.patch(
            "PySide6.QtCore.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ), mock.patch.object(
            bridge_launcher,
            "start_bridge_launch",
            return_value=fake_result,
        ) as start_launch:
            controller.startBridge()
            controller.startBridge()

            self.assertEqual(controller.bridgeLaunchPhase, "starting")
            self.assertEqual(len(callbacks), 1)
            callbacks.pop()()

        start_launch.assert_called_once_with()

    def test_start_bridge_rejects_output_configuration_work(self):
        blockers = (
            (
                "endpoint_preflight",
                lambda controller: setattr(controller, "_endpoint_preflight_busy", True),
                lambda controller: setattr(controller, "_endpoint_preflight_busy", False),
            ),
            (
                "driver_action",
                lambda controller: qt_settings_app._driver_action_active_event.set(),
                lambda controller: qt_settings_app._driver_action_active_event.clear(),
            ),
        )

        for name, start, stop in blockers:
            with self.subTest(name=name):
                controller, _ = self._make_controller()
                start(controller)
                try:
                    with mock.patch("PySide6.QtCore.QTimer.singleShot") as single_shot:
                        controller.startBridge()
                finally:
                    stop(controller)

                single_shot.assert_not_called()
                self.assertEqual(controller.bridgeLaunchPhase, "idle")
                self.assertIn("输出端点正在处理", controller.errorMessage)

    def test_queued_bridge_start_is_cancelled_after_exit_confirmation(self):
        controller, _ = self._make_controller()
        callbacks = []
        ready = []
        controller.applicationExitReady.connect(lambda: ready.append(True))
        with mock.patch(
            "PySide6.QtCore.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ), mock.patch.object(
            bridge_launcher,
            "start_bridge_launch",
        ) as start_launch:
            controller.startBridge()
            self.assertEqual(len(callbacks), 1)

            controller.requestApplicationExit()
            self.assertEqual(ready, [True])
            self.assertTrue(controller.applicationExitConfirmed)
            callbacks.pop()()

        start_launch.assert_not_called()
        self.assertEqual(controller.bridgeLaunchPhase, "idle")

    def test_existing_bridge_applies_the_saved_voice_program_without_restart(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        bridge_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.ALREADY_RUNNING,
            command=("exe",),
            exit_code=bridge_launcher.ALREADY_RUNNING_EXIT_CODE,
        )
        voice_result = voice_program_manager.VoiceProgramLaunchResult(
            provider_id="sogou",
            started=True,
            already_running=False,
            code="started",
        )

        with mock.patch.object(
            bridge_launcher, "start_bridge_launch", return_value=bridge_result
        ), mock.patch.object(
            qt_settings_app.voice_program_manager,
            "launch_voice_program",
            return_value=voice_result,
        ) as launch_voice_program:
            controller.saveAndLaunch()
            self._continue_save_and_launch(controller)

        launch_voice_program.assert_called_once_with(controller._voice_program_settings)
        self.assertTrue(controller.bridgeRunning)
        self.assertEqual(controller.bridgeLaunchPhase, "waiting")
        self.assertIn("已启动语音程序", controller.statusMessage)

    def test_existing_bridge_survives_an_unexpected_voice_program_launch_error(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        bridge_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.ALREADY_RUNNING,
            command=("exe",),
            exit_code=bridge_launcher.ALREADY_RUNNING_EXIT_CODE,
        )

        with mock.patch.object(
            bridge_launcher, "start_bridge_launch", return_value=bridge_result
        ), mock.patch.object(
            qt_settings_app.voice_program_manager,
            "launch_voice_program",
            side_effect=RuntimeError("unexpected launch failure"),
        ):
            controller.saveAndLaunch()
            self._continue_save_and_launch(controller)

        self.assertTrue(controller.bridgeRunning)
        self.assertEqual(controller.bridgeLaunchPhase, "waiting")
        self.assertIn("桥接不受影响", controller.errorMessage)

    def test_pending_launch_stays_in_starting_until_poll_finishes(self):
        controller, _ = self._make_controller()
        pending = bridge_launcher.PendingBridgeLaunch(
            command=("exe",),
            process=object(),
            pid=4321,
            checks_remaining=3,
        )
        started = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.STARTED,
            command=("exe",),
            pid=4321,
        )
        with mock.patch.object(
            bridge_launcher, "start_bridge_launch", return_value=pending
        ), mock.patch.object(
            bridge_launcher, "poll_bridge_launch", side_effect=[None, started]
        ) as poll_launch:
            controller.saveAndLaunch()
            self._continue_save_and_launch(controller)
            self.assertEqual(controller.bridgeLaunchPhase, "starting")
            self.assertTrue(controller.bridgeLaunchBusy)
            controller.pollBridgeLaunch()
            self.assertEqual(controller.bridgeLaunchPhase, "starting")
            controller.pollBridgeLaunch()

        self.assertEqual(poll_launch.call_count, 2)
        self.assertEqual(controller.bridgeLaunchPhase, "waiting")
        self.assertFalse(controller.bridgeLaunchBusy)

    def test_failed_launch_keeps_the_bridge_warning_active(self):
        controller, _ = self._make_controller()
        fake_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.LAUNCH_FAILED,
            command=("exe",),
            error="missing executable",
        )

        with mock.patch.object(
            bridge_launcher, "start_bridge_launch", return_value=fake_result
        ):
            controller.saveAndLaunch()
            self._continue_save_and_launch(controller)

        self.assertFalse(controller.bridgeRunning)
        self.assertIn("启动失败", controller.launchStatusText)

        launch_text = controller.launchStatusText
        with mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            side_effect=single_instance.SingleInstanceUnavailableError(
                "status unavailable"
            ),
        ):
            controller.refreshBridgeState()

        self.assertFalse(controller.bridgeRunning)
        self.assertEqual(controller.launchStatusText, launch_text)

    def test_restore_defaults_resets_voice_settings_and_mic_mapping(self):
        controller, model = self._make_controller()
        controller.holdVoiceHotkeyText = "ctrl+l"
        model.setActionTextAt(model.index_of("mic"), "Escape")
        controller.comboModifierIndex = 2
        controller.setComboActionText("up", "Escape")
        controller.restoreDefaults()
        self.assertEqual(controller.holdVoiceHotkeyText, "ralt")
        self.assertEqual(controller.comboModifierIndex, 0)
        self.assertTrue(all(not row["actionText"] for row in controller.comboRows))
        mic_index = model.index(model.index_of("mic"), 0)
        self.assertEqual(
            model.data(mic_index, model.ActionTextRole),
            settings_ui._VOICE_HOLD_DISPLAY,
        )

    def test_restore_mapping_defaults_does_not_change_voice_program_or_hotkey(self):
        controller, model = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        controller.holdVoiceHotkeyText = "ctrl+l"
        controller.voiceProgramLaunchElevated = True
        power_row = model.index_of("power")
        model.setActionTextAt(power_row, "f5")
        model.setDisplayNoteAt(power_row, "single_click", "刷新")

        controller.restoreMappingDefaults()

        self.assertEqual(controller.holdVoiceHotkeyText, "ctrl+l")
        self.assertEqual(controller.selectedVoiceProgramIndex, 1)
        self.assertTrue(controller.voiceProgramLaunchElevated)
        power_index = model.index(power_row, 0)
        self.assertNotEqual(model.data(power_index, model.ActionTextRole), "f5")
        self.assertEqual(model.data(power_index, model.SingleNoteRole), "")
        self.assertEqual(controller.comboModifierIndex, 0)
        self.assertTrue(all(not row["actionText"] for row in controller.comboRows))

    def test_select_button_updates_both_the_controller_and_the_model(self):
        controller, model = self._make_controller()
        controller.selectButton("power")
        self.assertEqual(controller.selectedButtonId, "power")
        self.assertEqual(model.selected_button_id(), "power")

    def test_real_key_detection_selects_captured_button_without_executing_mapping(self):
        controller, model = self._make_controller()
        callbacks = []

        class FakeListener:
            def __init__(self, _button_callback, raw_callback):
                callbacks.append(raw_callback)
                self.started_with = None
                self.stop_calls = 0

            def start(self, device_path):
                self.started_with = device_path

            def stop(self):
                self.stop_calls += 1

        with mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["rc003-device-path"],
        ), mock.patch.object(
            qt_settings_app.raw_input_windows.hid_identity,
            "select_single_device_path",
            return_value="rc003-device-path",
        ), mock.patch.object(
            qt_settings_app.raw_input_windows,
            "RawInputButtonListener",
            FakeListener,
        ):
            controller.startKeyDetection()

        self.assertTrue(controller.keyDetectionActive)
        callbacks[0](
            qt_settings_app.raw_input_windows.RawInputEvent(
                source="keyboard",
                is_pressed=True,
                button_id="power",
                vkey=0xFF,
                make_code=0x5E,
                flags=0x0002,
                message=0x0100,
            )
        )
        self.assertFalse(controller.keyDetectionActive)
        self.assertEqual(controller.selectedButtonId, "power")
        self.assertEqual(model.selected_button_id(), "power")
        self.assertIn("电源键", controller.keyDetectionText)
        self.assertIn("0x0066", controller.keyDetectionText)

    def test_real_key_detection_failure_is_reported_in_the_ui(self):
        controller, _ = self._make_controller()
        class UnavailableTap:
            status = frida_compat.HidTapState.UNAVAILABLE.value

            def __init__(self, _report_handler, *, status_handler):
                self.status_handler = status_handler

            def start(self):
                return False

        with mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
            side_effect=RuntimeError("Raw Input unavailable"),
        ), mock.patch.object(
            qt_settings_app.frida_compat,
            "RC003HidReportTap",
            UnavailableTap,
        ):
            controller.startKeyDetection()
        self.assertFalse(controller.keyDetectionActive)
        self.assertIn("Windows 按键通道启动失败", controller.keyDetectionText)
        self.assertIn("补充按键通道启动失败", controller.keyDetectionText)
        self.assertNotIn("Raw Input", controller.keyDetectionText)
        self.assertNotIn("HID tap", controller.keyDetectionText)

    def test_closed_supplemental_key_channel_explains_temporary_limit(self):
        controller, _ = self._make_controller()
        controller._key_detection_active = True
        controller._set_input_operation_state("key_detection", "active")

        controller._on_hid_tap_detection_status(
            frida_compat.HidTapState.UNHEALTHY.value,
            "gadget_connection_closed",
        )

        self.assertIn("补充按键通道暂不可用", controller.keyDetectionText)
        self.assertIn("返回键、音量键", controller.keyDetectionText)
        self.assertIn("正在重连", controller.keyDetectionText)
        self.assertNotIn("gadget_connection_closed", controller.keyDetectionText)
        self.assertNotIn("Raw Input", controller.keyDetectionText)
        self.assertNotIn("HID tap", controller.keyDetectionText)

    def test_local_detection_distinguishes_connecting_from_ready(self):
        controller, _ = self._make_controller()
        tap_instances = []

        class FakeListener:
            def __init__(self, _button_callback, _raw_callback):
                pass

            def start(self, _device_path):
                pass

            def stop(self):
                pass

        class ConnectingTap:
            def __init__(self, _report_handler, *, status_handler):
                self.status_handler = status_handler
                tap_instances.append(self)

            def start(self):
                return True

            def stop(self):
                pass

        with mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["rc003-device-path"],
        ), mock.patch.object(
            qt_settings_app.raw_input_windows.hid_identity,
            "select_single_device_path",
            return_value="rc003-device-path",
        ), mock.patch.object(
            qt_settings_app.raw_input_windows,
            "RawInputButtonListener",
            FakeListener,
        ), mock.patch.object(
            qt_settings_app.frida_compat,
            "RC003HidReportTap",
            ConnectingTap,
        ):
            controller.startKeyDetection()

        self.assertEqual(
            controller._KEY_DETECTION_TIMEOUT_SECONDS,
            key_detection_bridge.STALE_AFTER_SECONDS,
        )
        self.assertIn("Windows 按键通道已启动", controller.keyDetectionText)
        self.assertIn("等待补充通道连接", controller.keyDetectionText)
        self.assertIn("约 1 分钟", controller.keyDetectionText)
        self.assertNotIn("均已就绪", controller.keyDetectionText)
        self.assertGreater(controller._key_detection_started_at, 0.0)

        tap_instances[0].status_handler(
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            "",
        )

        self.assertIn("补充按键通道已连接", controller.keyDetectionText)
        self.assertIn("请按要检测的按键", controller.keyDetectionText)

        tap_instances[0].status_handler(frida_compat.HidTapState.READY.value, "")

        self.assertIn("两条按键通道均已就绪", controller.keyDetectionText)
        self.assertIn("13 个已知按键", controller.keyDetectionText)

    def test_tap_only_detection_tells_the_user_to_press_after_connection(self):
        controller, model = self._make_controller()
        tap_instances = []

        class ConnectingTap:
            def __init__(self, report_handler, *, status_handler):
                self.report_handler = report_handler
                self.status_handler = status_handler
                tap_instances.append(self)

            def start(self):
                return True

            def stop(self):
                pass

        with mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
            side_effect=RuntimeError("Windows channel unavailable"),
        ), mock.patch.object(
            qt_settings_app.frida_compat,
            "RC003HidReportTap",
            ConnectingTap,
        ):
            controller.startKeyDetection()

        self.assertTrue(controller.keyDetectionActive)
        self.assertIn("补充按键通道连接中", controller.keyDetectionText)
        self.assertIn("连接后请按要检测的按键", controller.keyDetectionText)

        tap_instances[0].status_handler(
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            "",
        )

        self.assertIn("请按要检测的按键", controller.keyDetectionText)

        tap_instances[0].report_handler(1, bytes.fromhex("520000000000"))

        self.assertFalse(controller.keyDetectionActive)
        self.assertEqual(controller.selectedButtonId, "up")
        self.assertEqual(model.selected_button_id(), "up")
        self.assertIn("0x0052", controller.keyDetectionText)

    def test_tap_detection_ignores_unknown_or_empty_reports(self):
        controller, _ = self._make_controller()
        controller._key_detection_active = True

        controller._on_key_detection_hid_report(1, bytes.fromhex("000000000000"))
        controller._on_key_detection_hid_report(1, bytes.fromhex("990000000000"))
        controller._on_key_detection_hid_report(1, bytes.fromhex("7f0000000000"))
        controller._on_key_detection_hid_report(2, bytes.fromhex("520000000000"))
        controller._on_key_detection_hid_report(1, bytes.fromhex("52000000"))

        self.assertTrue(controller.keyDetectionActive)
        self.assertEqual(controller.selectedButtonId, "ok")

    def test_local_detection_times_out_and_releases_both_channels(self):
        controller, _ = self._make_controller()
        listener = mock.Mock()
        tap = mock.Mock()
        listener_type = mock.Mock(return_value=listener)
        tap_type = mock.Mock(return_value=tap)
        tap.start.return_value = True

        with mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["rc003-device-path"],
        ), mock.patch.object(
            qt_settings_app.raw_input_windows.hid_identity,
            "select_single_device_path",
            return_value="rc003-device-path",
        ), mock.patch.object(
            qt_settings_app.raw_input_windows,
            "RawInputButtonListener",
            listener_type,
        ), mock.patch.object(
            qt_settings_app.frida_compat,
            "RC003HidReportTap",
            tap_type,
        ):
            controller.startKeyDetection()

        controller._key_detection_started_at -= (
            controller._KEY_DETECTION_TIMEOUT_SECONDS + 1.0
        )
        controller.pollKeyDetectionBridge()

        self.assertFalse(controller.keyDetectionActive)
        self.assertIsNone(controller._key_detection_listener)
        self.assertIsNone(controller._key_detection_tap)
        listener.stop.assert_called_once_with()
        tap.stop.assert_called_once_with()
        self.assertIn("等待按键超时", controller.keyDetectionText)

    def test_local_detection_timeout_preserves_a_cleanup_error(self):
        controller, _ = self._make_controller()
        listener = mock.Mock()
        listener.stop.side_effect = RuntimeError("listener stop failed")
        controller._key_detection_listener = listener
        controller._key_detection_active = True
        controller._key_detection_started_at = (
            time.monotonic() - controller._KEY_DETECTION_TIMEOUT_SECONDS - 1.0
        )

        controller.pollKeyDetectionBridge()

        self.assertTrue(controller.keyDetectionActive)
        self.assertIs(controller._key_detection_listener, listener)
        self.assertIn("停止 Windows 按键通道时出错", controller.keyDetectionText)
        self.assertNotIn("等待真实按键超时", controller.keyDetectionText)

    def test_real_key_detection_stops_when_bridge_status_is_unavailable(self):
        controller, _ = self._make_controller()

        with mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            side_effect=single_instance.SingleInstanceUnavailableError(
                "status unavailable"
            ),
        ), mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
        ) as enumerate_paths, mock.patch.object(
            qt_settings_app.frida_compat,
            "RC003HidReportTap",
        ) as tap:
            controller.startKeyDetection()

        self.assertFalse(controller.keyDetectionActive)
        self.assertIn("无法确认后台服务状态", controller.keyDetectionText)
        enumerate_paths.assert_not_called()
        tap.assert_not_called()

    def test_real_key_detection_stops_when_bridge_status_cleanup_fails(self):
        controller, _ = self._make_controller()

        with mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            side_effect=single_instance.MutexCleanupError("close failed"),
        ), mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
        ) as enumerate_paths, mock.patch.object(
            qt_settings_app.frida_compat,
            "RC003HidReportTap",
        ) as tap:
            controller.startKeyDetection()

        self.assertFalse(controller.keyDetectionActive)
        self.assertIn("无法确认后台服务状态", controller.keyDetectionText)
        enumerate_paths.assert_not_called()
        tap.assert_not_called()

    def test_failed_raw_listener_cleanup_retains_the_owner(self):
        controller, _ = self._make_controller()
        instances = []

        class StuckListener:
            def __init__(self, _button_callback, _raw_callback):
                instances.append(self)

            def start(self, _device_path):
                raise RuntimeError("start failed")

            def stop(self):
                raise RuntimeError("stop failed")

        with mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["rc003-device-path"],
        ), mock.patch.object(
            qt_settings_app.raw_input_windows.hid_identity,
            "select_single_device_path",
            return_value="rc003-device-path",
        ), mock.patch.object(
            qt_settings_app.raw_input_windows,
            "RawInputButtonListener",
            StuckListener,
        ), mock.patch.object(
            qt_settings_app.frida_compat,
            "RC003HidReportTap",
        ) as tap:
            controller.startKeyDetection()

        self.assertIs(controller._key_detection_listener, instances[0])
        self.assertTrue(controller.keyDetectionActive)
        tap.assert_not_called()

    def test_stop_key_detection_retains_each_owner_that_failed_to_stop(self):
        controller, _ = self._make_controller()
        listener = mock.Mock()
        tap = mock.Mock()
        listener.stop.side_effect = RuntimeError("listener stop failed")
        tap.stop.side_effect = RuntimeError("tap stop failed")
        controller._key_detection_listener = listener
        controller._key_detection_tap = tap
        controller._key_detection_active = True

        controller.stopKeyDetection()

        self.assertIs(controller._key_detection_listener, listener)
        self.assertIs(controller._key_detection_tap, tap)
        self.assertTrue(controller.keyDetectionActive)

    def test_hotkey_start_failure_retains_a_still_running_capture(self):
        controller, _ = self._make_controller()
        capture = mock.Mock()
        capture.is_running = True
        capture.start.side_effect = RuntimeError("start failed")

        with mock.patch.object(
            qt_settings_app.hotkey_capture_windows,
            "HotkeyCapture",
            return_value=capture,
        ):
            controller.startHotkeyCapture()

        self.assertIs(controller._hotkey_capture, capture)

    def test_hotkey_stop_failure_retains_capture_for_retry(self):
        controller, _ = self._make_controller()
        capture = mock.Mock()
        capture.stop.side_effect = RuntimeError("stop failed")
        controller._hotkey_capture = capture

        controller.stopHotkeyCapture()

        self.assertIs(controller._hotkey_capture, capture)

    def test_input_stop_worker_owns_the_resource_during_process_shutdown(self):
        controller, _ = self._make_controller()
        controller._background_task_runner = None
        entered = threading.Event()
        release = threading.Event()
        capture = mock.Mock()

        def stop_capture():
            entered.set()
            self.assertTrue(release.wait(timeout=2.0))

        capture.stop.side_effect = stop_capture
        controller._hotkey_capture = capture
        controller._set_input_operation_state("hotkey", "active")

        self.assertTrue(controller.stopHotkeyCapture())
        self.assertTrue(entered.wait(timeout=2.0))
        release.set()
        controller.shutdownForProcessExit()

        capture.stop.assert_called_once_with()
        self.assertIsNone(controller._hotkey_capture)

    def test_stop_worker_start_failure_restores_the_transferred_resource(self):
        controller, _ = self._make_controller()
        capture = mock.Mock()
        controller._hotkey_capture = capture
        controller._set_input_operation_state("hotkey", "active")

        def runner(_target, name):
            if name == "hotkey-stop":
                raise RuntimeError("worker unavailable")
            _target()

        controller._background_task_runner = runner

        self.assertFalse(controller.stopHotkeyCapture())
        self.assertIs(controller._hotkey_capture, capture)
        self.assertEqual(controller._input_operation_phase, "active")
        capture.stop.assert_not_called()

    def test_process_exit_still_releases_input_after_worker_shutdown_failure(self):
        controller, _ = self._make_controller()
        capture = mock.Mock()
        controller._hotkey_capture = capture
        controller._set_input_operation_state("hotkey", "active")
        controller.shutdownBackgroundTasks = mock.Mock(
            side_effect=RuntimeError("worker shutdown failed")
        )

        with self.assertRaises(RuntimeError):
            controller.shutdownForProcessExit()

        capture.stop.assert_called_once_with()
        self.assertIsNone(controller._hotkey_capture)

    def test_real_key_detection_accepts_missing_usage_from_hid_tap_and_stops_both(self):
        controller, model = self._make_controller()
        raw_instances = []
        tap_instances = []

        class FakeListener:
            def __init__(self, _button_callback, _raw_callback):
                self.stop_calls = 0
                raw_instances.append(self)

            def start(self, _device_path):
                pass

            def stop(self):
                self.stop_calls += 1

        class FakeTap:
            def __init__(self, report_handler, *, status_handler):
                self.report_handler = report_handler
                self.status_handler = status_handler
                self.status = frida_compat.HidTapState.STARTING.value
                self.stop_calls = 0
                tap_instances.append(self)

            def start(self):
                return True

            def stop(self):
                self.stop_calls += 1

        with mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["rc003-device-path"],
        ), mock.patch.object(
            qt_settings_app.raw_input_windows.hid_identity,
            "select_single_device_path",
            return_value="rc003-device-path",
        ), mock.patch.object(
            qt_settings_app.raw_input_windows,
            "RawInputButtonListener",
            FakeListener,
        ), mock.patch.object(
            qt_settings_app.frida_compat,
            "RC003HidReportTap",
            FakeTap,
        ):
            controller.startKeyDetection()
            tap_instances[0].report_handler(1, bytes.fromhex("f10000000000"))

        self.assertFalse(controller.keyDetectionActive)
        self.assertEqual(controller.selectedButtonId, "back")
        self.assertEqual(model.selected_button_id(), "back")
        self.assertIn("0x00F1", controller.keyDetectionText)
        self.assertEqual(raw_instances[0].stop_calls, 1)
        self.assertEqual(tap_instances[0].stop_calls, 1)

    def test_running_bridge_detection_returns_button_without_starting_local_hid(self):
        controller, model = self._make_controller()
        self._bridge_status_patch.stop()
        self._bridge_status_patch = mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=True,
        )
        self._bridge_status_patch.start()

        with mock.patch.object(
            qt_settings_app.raw_input_windows,
            "RawInputButtonListener",
        ) as raw_listener, mock.patch.object(
            qt_settings_app.frida_compat,
            "RC003HidReportTap",
        ) as tap:
            controller.startKeyDetection()

        self.assertTrue(controller.keyDetectionActive)
        self.assertIsNotNone(controller._key_detection_bridge_request)
        raw_listener.assert_not_called()
        tap.assert_not_called()

        self.assertTrue(
            key_detection_bridge.publish_next_button(
                controller._config_root,
                "volume_down",
            )
        )
        controller.pollKeyDetectionBridge()

        self.assertFalse(controller.keyDetectionActive)
        self.assertEqual(controller.selectedButtonId, "volume_down")
        self.assertEqual(model.selected_button_id(), "volume_down")
        self.assertIn("后台桥接", controller.keyDetectionText)

    def test_running_bridge_detection_times_out_cleanly(self):
        controller, _ = self._make_controller()
        self._bridge_status_patch.stop()
        self._bridge_status_patch = mock.patch.object(
            qt_settings_app.single_instance,
            "bridge_instance_running",
            return_value=True,
        )
        self._bridge_status_patch.start()
        controller.startKeyDetection()
        request = controller._key_detection_bridge_request
        controller._key_detection_started_at -= (
            controller._KEY_DETECTION_TIMEOUT_SECONDS + 1.0
        )

        controller.pollKeyDetectionBridge()

        self.assertFalse(controller.keyDetectionActive)
        self.assertFalse(request.request_path.exists())
        self.assertIn("超时", controller.keyDetectionText)

    def test_open_log_location_reports_honestly_when_never_run(self):
        controller, _ = self._make_controller()
        controller.openLogLocation()
        self.assertIn("尚不存在", controller.statusMessage)

    def test_open_bluetooth_settings_reports_the_uri_it_opened(self):
        controller, _ = self._make_controller()
        controller._set_error_message("stale error")
        fake_result = shell_targets.ExternalTargetResult(
            outcome=shell_targets.ExternalTargetOutcome.OPENED,
            target=shell_targets.BLUETOOTH_SETTINGS_URI,
        )
        with mock.patch.object(
            shell_targets, "open_external_target", return_value=fake_result
        ) as fake_open:
            controller.openBluetoothSettings()
        fake_open.assert_called_once_with(shell_targets.BLUETOOTH_SETTINGS_URI)
        self.assertIn(shell_targets.BLUETOOTH_SETTINGS_URI, controller.statusMessage)
        self.assertEqual(controller.errorMessage, "")

    def test_open_microphone_privacy_settings_uses_the_windows_privacy_uri(self):
        controller, _ = self._make_controller()
        fake_result = shell_targets.ExternalTargetResult(
            outcome=shell_targets.ExternalTargetOutcome.OPENED,
            target=shell_targets.MICROPHONE_PRIVACY_SETTINGS_URI,
        )
        with mock.patch.object(
            shell_targets, "open_external_target", return_value=fake_result
        ) as fake_open:
            controller.openMicrophonePrivacySettings()
        fake_open.assert_called_once_with(
            shell_targets.MICROPHONE_PRIVACY_SETTINGS_URI
        )

    def test_open_sound_settings_uses_the_windows_sound_uri(self):
        controller, _ = self._make_controller()
        fake_result = shell_targets.ExternalTargetResult(
            outcome=shell_targets.ExternalTargetOutcome.OPENED,
            target=shell_targets.SOUND_SETTINGS_URI,
        )
        with mock.patch.object(
            shell_targets, "open_external_target", return_value=fake_result
        ) as fake_open:
            controller.openSoundSettings()
        fake_open.assert_called_once_with(shell_targets.SOUND_SETTINGS_URI)

    def test_open_speech_settings_reports_a_failure_honestly(self):
        controller, _ = self._make_controller()
        fake_result = shell_targets.ExternalTargetResult(
            outcome=shell_targets.ExternalTargetOutcome.OPEN_FAILED,
            target=shell_targets.SPEECH_SETTINGS_URI,
            error="no handler registered",
        )
        with mock.patch.object(
            shell_targets, "open_external_target", return_value=fake_result
        ):
            controller.openSpeechSettings()
        self.assertEqual(controller.statusMessage, "")
        self.assertIn("no handler registered", controller.errorMessage)

    def test_installed_sogou_settings_use_manual_tray_guidance(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        executable = Path(
            r"C:\Program Files\SogouInput\sogou_voice_assistant.exe"
        )
        target = voice_program_manager.VoiceProgramSettingsTarget(
            "sogou",
            "搜狗语音输入",
            "sogou_manual",
            str(executable),
        )
        with mock.patch.object(
            qt_settings_app.voice_program_manager,
            "resolve_voice_program_settings_target",
            return_value=target,
        ):
            controller.openVoiceProgramSettings()

        self.assertEqual(controller.errorMessage, "")
        self.assertIn("展开隐藏图标", controller.statusMessage)
        self.assertIn("右键搜狗语音图标", controller.statusMessage)

    def test_missing_sogou_voice_opens_the_ai_toolbox(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        toolbox = Path(r"C:\Program Files\SogouInput\SOGOUSmartAssistant.exe")
        target = voice_program_manager.VoiceProgramSettingsTarget(
            "sogou",
            "搜狗语音输入",
            "sogou_toolbox",
            str(toolbox),
            "--from=menutool",
        )
        with mock.patch.object(
            qt_settings_app.voice_program_manager,
            "resolve_voice_program_settings_target",
            return_value=target,
        ), mock.patch.object(
            qt_settings_app.voice_program_manager,
            "open_voice_program_settings",
        ) as open_settings:
            controller.openVoiceProgramSettings()

        open_settings.assert_called_once_with(toolbox, "--from=menutool")
        self.assertEqual(controller.errorMessage, "")
        self.assertIn("请手动安装", controller.statusMessage)

    def test_missing_sogou_toolbox_uses_the_status_bar(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 1
        target = voice_program_manager.VoiceProgramSettingsTarget(
            "sogou",
            "搜狗语音输入",
            "missing",
        )
        with mock.patch.object(
            qt_settings_app.voice_program_manager,
            "resolve_voice_program_settings_target",
            return_value=target,
        ):
            controller.openVoiceProgramSettings()

        self.assertEqual(controller.errorMessage, "")
        self.assertIn("AI 工具箱中手动安装", controller.statusMessage)

    def test_open_wetype_settings_uses_the_installed_settings_program(self):
        controller, _ = self._make_controller()
        controller.selectedVoiceProgramIndex = 2
        executable = Path(r"C:\Program Files\Tencent\WeType\wetype_update.exe")
        target = voice_program_manager.VoiceProgramSettingsTarget(
            "wetype",
            "微信输入法",
            "executable",
            str(executable),
            "-showsetting",
        )
        with mock.patch.object(
            qt_settings_app.voice_program_manager,
            "resolve_voice_program_settings_target",
            return_value=target,
        ), mock.patch.object(
            qt_settings_app.voice_program_manager,
            "open_voice_program_settings",
        ) as open_settings:
            controller.openVoiceProgramSettings()

        open_settings.assert_called_once_with(executable, "-showsetting")
        self.assertIn("已打开微信输入法设置", controller.statusMessage)

    def test_select_and_persist_output_endpoint_succeeds_and_updates_options(self):
        controller, _ = self._make_controller()
        with mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
        ) as preflight:
            result = controller.selectAndPersistOutputEndpoint(
                "CABLE Input", "Windows WASAPI"
            )
        self.assertTrue(result)
        preflight.assert_called_once_with(
            "CABLE Input",
            "Windows WASAPI",
            cancel_event=mock.ANY,
        )
        cancel_event = preflight.call_args.kwargs["cancel_event"]
        self.assertFalse(cancel_event.is_set())
        controller._application_exit_intent.set()
        self.assertTrue(cancel_event.is_set())
        controller._application_exit_intent.clear()
        reloaded = config.load_config(config.config_path(controller._config_root))
        self.assertEqual(reloaded["output_endpoint_name"], "CABLE Input")
        self.assertEqual(reloaded["output_endpoint_host_api"], "Windows WASAPI")

    def test_output_endpoint_change_rejects_bridge_and_driver_actions(self):
        blockers = (
            (
                "bridge_launch",
                lambda controller: controller._set_bridge_launch_phase("starting"),
                lambda controller: controller._set_bridge_launch_phase("idle"),
            ),
            (
                "driver_action",
                lambda controller: qt_settings_app._driver_action_active_event.set(),
                lambda controller: qt_settings_app._driver_action_active_event.clear(),
            ),
        )

        for name, start, stop in blockers:
            with self.subTest(name=name):
                controller, _ = self._make_controller()
                start(controller)
                try:
                    with mock.patch.object(
                        qt_settings_app.windows_diagnostics,
                        "preflight_output_endpoint_isolated",
                    ) as preflight:
                        result = controller.selectAndPersistOutputEndpoint(
                            "CABLE Input", "Windows WASAPI"
                        )
                finally:
                    stop(controller)

                self.assertFalse(result)
                preflight.assert_not_called()

    def test_select_and_persist_output_endpoint_returns_false_on_persistence_failure(self):
        # XRBM-031 RETRY 1 item 3: a config-save failure (disk full,
        # permission denied, ...) must never raise out of this Slot and
        # must never be reported as a successful save.
        controller, _ = self._make_controller()
        original_config = dict(controller._config)
        with mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
        ), mock.patch.object(config, "save_config", side_effect=OSError("disk full")):
            result = controller.selectAndPersistOutputEndpoint("CABLE Input", "Windows WASAPI")
        self.assertFalse(result)
        # The in-memory config must not look saved when it was not.
        self.assertEqual(controller._config, original_config)

    def test_select_and_persist_output_endpoint_never_raises_on_unexpected_error(self):
        controller, _ = self._make_controller()
        with mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
        ), mock.patch.object(config, "save_config", side_effect=RuntimeError("boom")):
            result = controller.selectAndPersistOutputEndpoint("CABLE Input", "")
        self.assertFalse(result)

    def test_select_and_persist_output_endpoint_rejects_failed_preflight(self):
        controller, _ = self._make_controller()
        original_config = dict(controller._config)
        with mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
            side_effect=audio_output.AudioOutputUnavailableError("cannot open"),
        ), mock.patch.object(config, "save_config") as save_config:
            result = controller.selectAndPersistOutputEndpoint(
                "CABLE Input", "Windows WASAPI"
            )
        self.assertFalse(result)
        save_config.assert_not_called()
        self.assertEqual(controller._config, original_config)

    def test_output_endpoint_preflight_cancelled_by_exit_reports_exit_reason(self):
        model = self.Model()
        background_tasks = []
        completions = []

        def runner(target, name):
            if name == "audio-endpoint-preflight":
                background_tasks.append((target, name))
            else:
                target()

        controller = self.Controller(
            model,
            background_task_runner=runner,
        )
        original_config = dict(controller._config)

        with mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
            side_effect=audio_output.AudioOutputUnavailableError("cancelled"),
        ), mock.patch.object(config, "save_config") as save_config:
            self.assertTrue(
                controller._select_and_persist_output_endpoint(
                    "CABLE Input",
                    "Windows WASAPI",
                    lambda ok, message: completions.append((ok, message)),
                )
            )
            self.assertEqual(len(background_tasks), 1)

            controller.requestApplicationExit()
            background_tasks.pop()[0]()

        self.assertEqual(
            completions,
            [(False, "程序正在退出，未保存输出端点。")],
        )
        save_config.assert_not_called()
        self.assertEqual(controller._config, original_config)


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class DiagnosticsControllerTests(unittest.TestCase):
    """DiagnosticsController (XRBM-031) runs every check on a background
    thread and delivers results via a cross-thread Qt signal - unlike
    SettingsControllerTests above, these tests need a real (offscreen)
    QGuiApplication instance so ``processEvents()`` can actually pump the
    queued cross-thread delivery; constructing one here never touches
    QQuickStyle/QML, so it does not conflict with the separate
    once-per-process QQuickStyle constraint the QML-engine subprocess tests
    below work around.
    """

    def setUp(self):
        classes = qt_settings_app._load_qt_classes()
        self.Model = classes["ButtonMappingModel"]
        self.SettingsController = classes["SettingsController"]
        self.DiagnosticsController = classes["DiagnosticsController"]
        QGuiApplication = classes["QGuiApplication"]
        self.app = QGuiApplication.instance() or QGuiApplication([])
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmpdir.name})
        self._env_patch.start()
        # Matches config_root() with the SAME LOCALAPPDATA-derived path
        # SettingsController itself computes internally (config.config_root()),
        # NOT the bare tmpdir - otherwise DiagnosticsController would read/
        # write a different directory than the one SettingsController's own
        # config actually lives in.
        self._config_root = config.config_root()
        # Defensive (XRBM-031 RETRY 2): this is process-global, persistent
        # state - never start a test with it left set by a previous
        # test/failure, and never leave it set for the next one.
        qt_settings_app._diagnostics_shutdown_event.clear()
        qt_settings_app._vb_cable_test_active_event.clear()
        qt_settings_app._driver_action_active_event.clear()

    def tearDown(self):
        qt_settings_app._diagnostics_shutdown_event.clear()
        qt_settings_app._vb_cable_test_active_event.clear()
        qt_settings_app._driver_action_active_event.clear()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _make_settings_controller(self):
        model = self.Model()
        return self.SettingsController(
            model,
            background_task_runner=lambda target, _name: target(),
        )

    def _pump_until(self, predicate, timeout_seconds=5.0):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_initial_construction_starts_a_refresh_that_completes(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self.assertTrue(diag.isRefreshing)
        self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))
        self.assertEqual(len(diag.checkResults), 6)
        ids = {row["checkId"] for row in diag.checkResults}
        self.assertIn("dictation", ids)

    def test_check_results_never_contain_a_placeholder_raw_path_or_address(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        for row in diag.checkResults:
            self.assertNotIn("VID_", row["detail"])
            self.assertNotIn("\\\\?\\", row["detail"])

    def test_unexpected_report_failure_clears_stale_results_and_shows_a_page_error(self):
        # XRBM-031 RETRY 1 item 2: a background-thread report failure (e.g.
        # windows_diagnostics.run_diagnostics() itself raising, despite its
        # own per-check isolation) must never leave a PREVIOUS successful
        # run's rows on screen looking current, and must surface a clear,
        # page-level error - not only a driver-card message below the fold.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))
        self.assertEqual(len(diag.checkResults), 6)  # a real prior run populated these
        self.assertEqual(diag.diagnosticsErrorMessage, "")

        with mock.patch.object(
            windows_diagnostics, "run_diagnostics", side_effect=RuntimeError("boom")
        ):
            diag.refreshDiagnostics()
            self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))

        self.assertEqual(diag.checkResults, [])
        self.assertNotEqual(diag.diagnosticsErrorMessage, "")
        self.assertIn("重新检测", diag.diagnosticsErrorMessage)

    def test_successful_refresh_after_a_failure_clears_the_page_error(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            windows_diagnostics, "run_diagnostics", side_effect=RuntimeError("boom")
        ):
            diag.refreshDiagnostics()
            self._pump_until(lambda: not diag.isRefreshing)
        self.assertNotEqual(diag.diagnosticsErrorMessage, "")

        diag.refreshDiagnostics()
        self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))
        self.assertEqual(diag.diagnosticsErrorMessage, "")
        self.assertEqual(len(diag.checkResults), 6)

    def test_worker_thread_is_deregistered_once_finished(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        self.assertTrue(
            self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)
        )

    def test_repeated_refresh_click_is_ignored_while_a_check_is_already_running(self):
        release_event = threading.Event()
        call_count = {"n": 0}

        def _blocking_run_diagnostics(**kwargs):
            call_count["n"] += 1
            release_event.wait(timeout=5.0)
            return windows_diagnostics.DiagnosticsReport(checks=())

        settings_controller = self._make_settings_controller()
        with mock.patch.object(
            windows_diagnostics, "run_diagnostics", side_effect=_blocking_run_diagnostics
        ):
            diag = self.DiagnosticsController(settings_controller, self._config_root)
            # __init__ already started one worker, currently blocked on
            # release_event - both of these must be no-ops, never a second
            # concurrent call into the (blocking) fake.
            diag.refreshDiagnostics()
            diag.refreshDiagnostics()
            release_event.set()
            self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))
        self.assertEqual(call_count["n"], 1)

    def test_bridge_connection_automatically_refreshes_stale_diagnostics(self):
        call_count = {"n": 0}

        def _run_diagnostics(**kwargs):
            call_count["n"] += 1
            return windows_diagnostics.DiagnosticsReport(checks=())

        settings_controller = self._make_settings_controller()
        with mock.patch.object(
            windows_diagnostics,
            "run_diagnostics",
            side_effect=_run_diagnostics,
        ):
            diag = self.DiagnosticsController(settings_controller, self._config_root)
            self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))
            self.assertEqual(call_count["n"], 1)

            settings_controller._set_bridge_connected(True)
            self.assertTrue(
                self._pump_until(
                    lambda: call_count["n"] == 2 and not diag.isRefreshing
                )
            )

        self.assertEqual(call_count["n"], 2)

    def test_bridge_connection_refresh_waits_for_the_active_check(self):
        release_first = threading.Event()
        state = {"calls": 0, "active": 0, "max_active": 0}

        def _run_diagnostics(**kwargs):
            state["calls"] += 1
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            try:
                if state["calls"] == 1:
                    release_first.wait(timeout=5.0)
                return windows_diagnostics.DiagnosticsReport(checks=())
            finally:
                state["active"] -= 1

        settings_controller = self._make_settings_controller()
        with mock.patch.object(
            windows_diagnostics,
            "run_diagnostics",
            side_effect=_run_diagnostics,
        ):
            diag = self.DiagnosticsController(settings_controller, self._config_root)
            self.assertTrue(diag.isRefreshing)
            settings_controller._set_bridge_connected(True)
            self.app.processEvents()
            self.assertEqual(state["calls"], 1)

            release_first.set()
            self.assertTrue(
                self._pump_until(
                    lambda: state["calls"] == 2 and not diag.isRefreshing
                )
            )

        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["max_active"], 1)

    def test_no_worker_thread_survives_after_completion(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)
        for thread in list(qt_settings_app._diagnostics_threads):
            self.assertFalse(thread.is_alive())

    def test_refresh_diagnostics_refuses_to_start_once_shutdown_has_begun(self):
        # XRBM-031 RETRY 2: once process shutdown is flagged, a "重新检测"
        # click (or anything else calling refreshDiagnostics()) must be a
        # no-op - there is nothing left alive to usefully deliver a result
        # to, and starting a new thread this late only works against the
        # atexit hook's own bounded join.
        settings_controller = self._make_settings_controller()
        empty_report = windows_diagnostics.DiagnosticsReport(checks=())
        with mock.patch.object(
            windows_diagnostics, "run_diagnostics", return_value=empty_report
        ):
            diag = self.DiagnosticsController(settings_controller, self._config_root)
            self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))
            self.assertTrue(
                self._pump_until(
                    lambda: len(qt_settings_app._diagnostics_threads) == 0
                )
            )

        qt_settings_app._diagnostics_shutdown_event.set()
        diag.refreshDiagnostics()
        # Give any (incorrectly) started worker a real chance to flip
        # isRefreshing/register itself before asserting neither happened.
        self.app.processEvents()
        self.assertFalse(diag.isRefreshing)
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)

    def test_worker_already_running_skips_emit_once_shutdown_begins_mid_run(self):
        # XRBM-031 RETRY 2: a worker that was already running when shutdown
        # began must notice the flag and skip delivering its result,
        # rather than emitting into a DiagnosticsController/Qt runtime that
        # may already be mid-teardown by the time it finishes.
        release_event = threading.Event()

        def _blocking_run_diagnostics(**kwargs):
            release_event.wait(timeout=5.0)
            return windows_diagnostics.DiagnosticsReport(checks=())

        settings_controller = self._make_settings_controller()
        with mock.patch.object(
            windows_diagnostics, "run_diagnostics", side_effect=_blocking_run_diagnostics
        ):
            diag = self.DiagnosticsController(settings_controller, self._config_root)
            # __init__ already started one worker, currently blocked.
            self.assertTrue(diag.isRefreshing)

            qt_settings_app._diagnostics_shutdown_event.set()
            release_event.set()
            # The worker still finishes and deregisters itself (proven via
            # the registry, since the - correctly skipped - emit means
            # isRefreshing/checkResults are never touched by
            # _on_diagnostics_ready and so cannot be used as the signal
            # here).
            self.assertTrue(
                self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)
            )
        # The skipped emit is exactly why these are still in their
        # constructed-but-never-updated state.
        self.assertTrue(diag.isRefreshing)
        self.assertEqual(diag.checkResults, [])

    def test_registry_is_cleaned_up_even_if_emit_raises(self):
        # XRBM-031 RETRY 2: a receiver/Qt runtime that is already tearing
        # down could still make the emit call itself raise (the shutdown-
        # flag check narrows this window but cannot close it perfectly) -
        # the worker thread must still deregister itself from
        # _diagnostics_threads regardless, via its `finally` block.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)

        with mock.patch.object(
            diag, "_emit_diagnostics_ready", side_effect=RuntimeError("boom")
        ):
            diag.refreshDiagnostics()
            self.assertTrue(
                self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)
            )
        # The worker thread itself never crashed the process/left a
        # dangling registry entry despite the injected emit failure.
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)

    def test_shutdown_helper_actually_kills_a_hanging_ble_diagnostics_subprocess_and_worker_never_emits(self):
        # XRBM-035 RETRY 1: end-to-end reproduction of the real Windows CI
        # crash cause - a hung WinRT BLE discovery still running when the
        # process started exiting - now using the REAL, unmodified
        # windows_diagnostics.run_diagnostics()/check_ble_candidate()/
        # _discover_ble_candidates_sync()/_run_ble_diagnostics_subprocess()
        # call chain. Only the CHILD PROCESS COMMAND itself is replaced
        # (build_ble_diagnostics_subprocess_command()) with one that spawns
        # a genuine, artificially-hanging OS process instead of the real
        # ovb_rc003 entrypoint - discovery still runs in a real, separate
        # process, and the real terminate()/kill()/wait() escalation this
        # RETRY exists for is exercised for real, through the production
        # _shutdown_diagnostics_workers() helper - the same one
        # run_settings_window() and the real QML load probe both call.
        hang_script = "import time\ntime.sleep(120)\n"

        def _fake_command(result_path, **kwargs):
            return [sys.executable, "-c", hang_script]

        settings_controller = self._make_settings_controller()
        with mock.patch.object(
            windows_diagnostics, "build_ble_diagnostics_subprocess_command", _fake_command
        ):
            diag = self.DiagnosticsController(settings_controller, self._config_root)
            self.assertTrue(diag.isRefreshing)
            self.app.processEvents()

            # Give the worker thread a real chance to actually spawn the
            # hanging child before shutdown is requested - not strictly
            # required for correctness (the subprocess module handles a
            # not-yet-spawned/racing spawn fine either way), but makes this
            # test reliably exercise the "kill an already-running child"
            # path rather than sometimes short-circuiting before spawn.
            time.sleep(0.2)

            started = time.monotonic()
            qt_settings_app._shutdown_diagnostics_workers()
            elapsed = time.monotonic() - started

        self.assertLess(
            elapsed,
            qt_settings_app._DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS + 1.0,
            "shutdown must bound-wait, not hang on the killed worker",
        )
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0, "the worker thread must have finished")
        # The worker was cancelled before it could finish - its (never
        # produced) result must never have been emitted, matching the
        # never-emit-after-shutdown contract the RETRY 2 tests above cover
        # for the non-hanging case.
        self.assertTrue(diag.isRefreshing)
        self.assertEqual(diag.checkResults, [])

    def test_shutdown_helper_kills_a_hanging_vb_cable_child(self):
        hang_script = "import time\ntime.sleep(120)\n"

        def _fake_command(request_path, result_path, **kwargs):
            return [sys.executable, "-c", hang_script]

        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))

        with mock.patch.object(
            settings_controller, "_get_bridge_launch_busy", return_value=False
        ), mock.patch.object(
            settings_controller, "_refresh_bridge_status", return_value=False
        ), mock.patch.object(
            windows_diagnostics,
            "build_vb_cable_loopback_subprocess_command",
            _fake_command,
        ):
            diag.testVbCableChannel()
            self.assertTrue(diag.vbCableTestRunning)
            time.sleep(0.2)

            started = time.monotonic()
            qt_settings_app._shutdown_diagnostics_workers()
            elapsed = time.monotonic() - started

        self.assertLess(
            elapsed,
            qt_settings_app._DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS + 1.0,
        )
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)
        self.assertFalse(qt_settings_app._vb_cable_test_active_event.is_set())
        # Shutdown suppresses the result signal, so the now-closing UI is
        # never updated from the worker after Qt teardown has begun.
        self.assertTrue(diag.vbCableTestRunning)

    def test_select_detected_cable_input_persists_via_settings_controller(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        endpoint = audio_output.AudioEndpoint(name="CABLE Input", host_api="Windows WASAPI")
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=[endpoint]
        ), mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
        ):
            result = diag.selectDetectedCableInputAsOutput()

        self.assertTrue(result)
        self.assertTrue(self._pump_until(lambda: not diag.driverActionRunning))
        self.assertIn("CABLE Input", diag.driverStatusMessage)
        # Persisted for real - reloading config from disk shows the change.
        reloaded = config.load_config(config.config_path(self._config_root))
        self.assertEqual(reloaded["output_endpoint_name"], "CABLE Input")
        self.assertEqual(reloaded["output_endpoint_host_api"], "Windows WASAPI")

    def test_select_detected_cable_input_fails_closed_when_none_found(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(audio_output, "enumerate_output_endpoints", return_value=[]):
            result = diag.selectDetectedCableInputAsOutput()

        self.assertTrue(result)
        self.assertTrue(self._pump_until(lambda: not diag.driverActionRunning))
        self.assertIn("未找到", diag.driverErrorMessage)

    def test_select_detected_cable_input_refuses_to_start_during_exit(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        settings_controller._application_exit_intent.set()

        with mock.patch.object(
            audio_output,
            "enumerate_output_endpoints",
        ) as enumerate_endpoints:
            self.assertFalse(diag.selectDetectedCableInputAsOutput())

        enumerate_endpoints.assert_not_called()
        self.assertFalse(diag.driverActionRunning)
        self.assertFalse(qt_settings_app._driver_action_active_event.is_set())

    def test_select_detected_cable_input_refuses_bridge_and_endpoint_work(self):
        blockers = (
            (
                "bridge_launch",
                lambda controller: controller._set_bridge_launch_phase("starting"),
                lambda controller: controller._set_bridge_launch_phase("idle"),
            ),
            (
                "endpoint_preflight",
                lambda controller: setattr(controller, "_endpoint_preflight_busy", True),
                lambda controller: setattr(controller, "_endpoint_preflight_busy", False),
            ),
        )

        for name, start, stop in blockers:
            with self.subTest(name=name):
                settings_controller = self._make_settings_controller()
                diag = self.DiagnosticsController(
                    settings_controller, self._config_root
                )
                self._pump_until(lambda: not diag.isRefreshing)
                start(settings_controller)
                try:
                    with mock.patch.object(
                        audio_output, "enumerate_output_endpoints"
                    ) as enumerate_endpoints:
                        self.assertFalse(diag.selectDetectedCableInputAsOutput())
                finally:
                    stop(settings_controller)

                enumerate_endpoints.assert_not_called()
                self.assertFalse(diag.driverActionRunning)

    def test_select_detected_cable_input_reports_an_honest_error_on_persistence_failure(self):
        # XRBM-031 RETRY 1 item 3: a config persistence failure must never
        # raise out of this Slot, must never be reported as a successful
        # save, and must never expose a local path/device identifier.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        endpoint = audio_output.AudioEndpoint(name="CABLE Input", host_api="Windows WASAPI")
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=[endpoint]
        ):
            with mock.patch.object(
                settings_controller,
                "_select_and_persist_output_endpoint",
                side_effect=RuntimeError("boom"),
            ):
                result = diag.selectDetectedCableInputAsOutput()

        self.assertTrue(result)
        self.assertTrue(self._pump_until(lambda: not diag.driverActionRunning))
        self.assertNotIn("boom", diag.driverErrorMessage)
        self.assertEqual(diag.driverStatusMessage, "")
        error_lower = diag.driverErrorMessage.lower()
        self.assertNotIn(str(self._config_root).lower(), error_lower)

    def test_select_detected_cable_input_reports_an_honest_error_when_persistence_returns_false(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        endpoint = audio_output.AudioEndpoint(name="CABLE Input", host_api="Windows WASAPI")
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=[endpoint]
        ):
            with mock.patch.object(
                settings_controller,
                "_select_and_persist_output_endpoint",
                return_value=False,
            ):
                result = diag.selectDetectedCableInputAsOutput()

        self.assertTrue(result)
        self.assertTrue(self._pump_until(lambda: not diag.driverActionRunning))
        self.assertNotEqual(diag.driverErrorMessage, "")
        self.assertEqual(diag.driverStatusMessage, "")

    def test_select_detected_cable_input_fails_closed_when_ambiguous(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        endpoints = [
            audio_output.AudioEndpoint(name="CABLE Input", host_api="A"),
            audio_output.AudioEndpoint(name="CABLE Input", host_api="B"),
        ]
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=endpoints
        ):
            result = diag.selectDetectedCableInputAsOutput()

        self.assertTrue(result)
        self.assertTrue(self._pump_until(lambda: not diag.driverActionRunning))
        self.assertIn("请在语音页", diag.driverErrorMessage)

    def test_select_detected_cable_input_prefers_wasapi_over_directsound(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        endpoints = [
            audio_output.AudioEndpoint(
                name="CABLE Input", host_api="Windows DirectSound"
            ),
            audio_output.AudioEndpoint(name="CABLE Input", host_api="Windows WASAPI"),
        ]
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=endpoints
        ), mock.patch.object(
            qt_settings_app.windows_diagnostics,
            "preflight_output_endpoint_isolated",
        ):
            result = diag.selectDetectedCableInputAsOutput()

        self.assertTrue(result)
        self.assertTrue(self._pump_until(lambda: not diag.driverActionRunning))
        reloaded = config.load_config(config.config_path(self._config_root))
        self.assertEqual(reloaded["output_endpoint_host_api"], "Windows WASAPI")

    def test_vb_cable_channel_test_delivers_success_back_to_the_gui_thread(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        result = windows_diagnostics.CheckResult(
            "vb_cable_loopback",
            "VB-CABLE 本地通道",
            windows_diagnostics.CheckGroup.OPTIONAL_DRIVER,
            windows_diagnostics.CheckStatus.PASS,
            "测试信号已到达；不代表输入法已经识别文字。",
        )

        with mock.patch.object(
            settings_controller, "_refresh_bridge_status", return_value=False
        ), mock.patch.object(
            windows_diagnostics, "check_vb_cable_loopback_isolated", return_value=result
        ):
            diag.testVbCableChannel()
            self.assertTrue(diag.vbCableTestRunning)
            self.assertTrue(self._pump_until(lambda: not diag.vbCableTestRunning))

        self.assertEqual(diag.vbCableTestStatus, "pass")
        self.assertIn("不代表输入法", diag.vbCableTestMessage)

    def test_refresh_invalidates_an_old_vb_cable_success(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        diag._on_vb_cable_test_ready(
            windows_diagnostics.CheckResult(
                "vb_cable_loopback",
                "VB-CABLE 本地通道",
                windows_diagnostics.CheckGroup.OPTIONAL_DRIVER,
                windows_diagnostics.CheckStatus.PASS,
                "OLD_PASS",
            )
        )
        self.assertEqual(diag.vbCableTestStatus, "pass")

        with mock.patch.object(
            windows_diagnostics,
            "run_diagnostics",
            return_value=windows_diagnostics.DiagnosticsReport(checks=()),
        ):
            diag.refreshDiagnostics()
            self.assertEqual(diag.vbCableTestStatus, "idle")
            self.assertEqual(diag.vbCableTestMessage, "")
            self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))

    def test_endpoint_selection_change_invalidates_an_old_vb_cable_success(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        diag._on_vb_cable_test_ready(
            windows_diagnostics.CheckResult(
                "vb_cable_loopback",
                "VB-CABLE 本地通道",
                windows_diagnostics.CheckGroup.OPTIONAL_DRIVER,
                windows_diagnostics.CheckStatus.PASS,
                "OLD_PASS",
            )
        )

        settings_controller.selectedEndpointIndexChanged.emit()

        self.assertEqual(diag.vbCableTestStatus, "idle")
        self.assertEqual(diag.vbCableTestMessage, "")

    def test_vb_cable_channel_test_rejects_a_running_bridge(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            settings_controller, "_refresh_bridge_status", return_value=True
        ), mock.patch.object(
            windows_diagnostics, "check_vb_cable_loopback_isolated"
        ) as loopback:
            diag.testVbCableChannel()

        self.assertFalse(diag.vbCableTestRunning)
        self.assertEqual(diag.vbCableTestStatus, "fail")
        self.assertIn("临时停止", diag.vbCableTestMessage)
        loopback.assert_not_called()

    def test_vb_cable_channel_test_temporarily_stops_and_restores_bridge(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        loopback_result = windows_diagnostics.CheckResult(
            "vb_cable_loopback",
            "VB-CABLE 本地通道",
            windows_diagnostics.CheckGroup.OPTIONAL_DRIVER,
            windows_diagnostics.CheckStatus.PASS,
            "测试信号已到达。",
        )
        restart_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.STARTED,
            command=("RemoteMicRC003.exe", "--bridge"),
        )

        with mock.patch.object(
            settings_controller, "_refresh_bridge_status", return_value=True
        ), mock.patch.object(
            bridge_control_windows,
            "request_bridge_exit",
            return_value=bridge_control_windows.BridgeExitResult(True, True),
        ), mock.patch.object(
            windows_diagnostics,
            "check_vb_cable_loopback_isolated",
            return_value=loopback_result,
        ), mock.patch.object(
            bridge_launcher, "launch_bridge", return_value=restart_result
        ):
            diag.testVbCableChannelWithBridgeRestart()
            self.assertTrue(diag.vbCableTestRunning)
            self.assertTrue(self._pump_until(lambda: not diag.vbCableTestRunning))

        self.assertEqual(diag.vbCableTestStatus, "pass")
        self.assertIn("自动恢复", diag.vbCableTestMessage)
        self.assertFalse(diag.vbCableBridgeRecoveryNeeded)

    def test_vb_cable_channel_test_exposes_manual_recovery_when_restart_fails(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        loopback_result = windows_diagnostics.CheckResult(
            "vb_cable_loopback",
            "VB-CABLE 本地通道",
            windows_diagnostics.CheckGroup.OPTIONAL_DRIVER,
            windows_diagnostics.CheckStatus.PASS,
            "测试信号已到达。",
        )
        restart_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.LAUNCH_FAILED,
            command=(),
            error="denied",
        )

        with mock.patch.object(
            settings_controller, "_refresh_bridge_status", return_value=True
        ), mock.patch.object(
            bridge_control_windows,
            "request_bridge_exit",
            return_value=bridge_control_windows.BridgeExitResult(True, True),
        ), mock.patch.object(
            windows_diagnostics,
            "check_vb_cable_loopback_isolated",
            return_value=loopback_result,
        ), mock.patch.object(
            bridge_launcher, "launch_bridge", return_value=restart_result
        ):
            diag.testVbCableChannelWithBridgeRestart()
            self.assertTrue(self._pump_until(lambda: not diag.vbCableTestRunning))

        self.assertEqual(diag.vbCableTestStatus, "fail")
        self.assertTrue(diag.vbCableBridgeRecoveryNeeded)
        self.assertIn("未能自动恢复", diag.vbCableTestMessage)

    def test_vb_cable_channel_test_does_not_run_when_graceful_stop_fails(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            settings_controller, "_refresh_bridge_status", return_value=True
        ), mock.patch.object(
            bridge_control_windows,
            "request_bridge_exit",
            return_value=bridge_control_windows.BridgeExitResult(
                True, False, "服务未停止"
            ),
        ), mock.patch.object(
            windows_diagnostics, "check_vb_cable_loopback_isolated"
        ) as loopback:
            diag.testVbCableChannelWithBridgeRestart()
            self.assertTrue(self._pump_until(lambda: not diag.vbCableTestRunning))

        self.assertEqual(diag.vbCableTestStatus, "fail")
        self.assertIn("服务未停止", diag.vbCableTestMessage)
        self.assertFalse(diag.vbCableBridgeRecoveryNeeded)
        loopback.assert_not_called()

    def test_vb_cable_channel_test_rejects_a_bridge_launch_in_progress(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            settings_controller, "_get_bridge_launch_busy", return_value=True
        ), mock.patch.object(
            windows_diagnostics, "check_vb_cable_loopback_isolated"
        ) as loopback:
            diag.testVbCableChannel()

        self.assertFalse(diag.vbCableTestRunning)
        self.assertEqual(diag.vbCableTestStatus, "fail")
        self.assertIn("正在启动", diag.vbCableTestMessage)
        loopback.assert_not_called()

    def test_vb_cable_channel_test_rejects_a_driver_action_in_progress(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        diag._set_driver_action_running(True)

        try:
            with mock.patch.object(
                windows_diagnostics, "check_vb_cable_loopback_isolated"
            ) as loopback:
                diag.testVbCableChannel()
        finally:
            diag._set_driver_action_running(False)

        self.assertFalse(diag.vbCableTestRunning)
        self.assertEqual(diag.vbCableTestStatus, "fail")
        self.assertIn("输出端点正在处理", diag.vbCableTestMessage)
        loopback.assert_not_called()

    def test_vb_cable_channel_test_rejects_voice_hotkey_work_in_progress(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        settings_controller._set_voice_hotkey_busy(True)

        with mock.patch.object(
            windows_diagnostics, "check_vb_cable_loopback_isolated"
        ) as loopback:
            diag.testVbCableChannel()

        self.assertFalse(diag.vbCableTestRunning)
        self.assertEqual(diag.vbCableTestStatus, "fail")
        self.assertIn("语音快捷键正在处理", diag.vbCableTestMessage)
        loopback.assert_not_called()

    def test_vb_cable_channel_test_and_refresh_are_mutually_exclusive(self):
        release_event = threading.Event()
        call_count = {"n": 0}

        def _blocking_check(*_args, **_kwargs):
            call_count["n"] += 1
            release_event.wait(timeout=5.0)
            return windows_diagnostics.CheckResult(
                "vb_cable_loopback",
                "VB-CABLE 本地通道",
                windows_diagnostics.CheckGroup.OPTIONAL_DRIVER,
                windows_diagnostics.CheckStatus.FAIL,
                "未收到测试信号。",
            )

        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        with mock.patch.object(
            settings_controller, "_refresh_bridge_status", return_value=False
        ), mock.patch.object(
            windows_diagnostics,
            "check_vb_cable_loopback_isolated",
            side_effect=_blocking_check,
        ), mock.patch.object(
            windows_diagnostics, "run_diagnostics", wraps=windows_diagnostics.run_diagnostics
        ) as refresh:
            diag.testVbCableChannel()
            self.assertTrue(diag.vbCableTestRunning)
            self.assertFalse(settings_controller.saveSettings())
            self.assertIn("通道测试正在运行", settings_controller.errorMessage)
            diag.testVbCableChannel()
            diag.refreshDiagnostics()
            self.assertFalse(diag.isRefreshing)
            release_event.set()
            self.assertTrue(self._pump_until(lambda: not diag.vbCableTestRunning))

        self.assertEqual(call_count["n"], 1)
        refresh.assert_not_called()

    def test_vb_cable_channel_test_refuses_to_start_after_shutdown(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        qt_settings_app._diagnostics_shutdown_event.set()

        with mock.patch.object(
            windows_diagnostics, "check_vb_cable_loopback_isolated"
        ) as loopback:
            diag.testVbCableChannel()

        self.assertFalse(diag.vbCableTestRunning)
        loopback.assert_not_called()

    def test_launch_vb_cable_setup_reports_bundle_not_found_as_an_error(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            vb_cable_bundle,
            "prepare_and_launch_vendor_setup",
            side_effect=vb_cable_bundle.BundleNotFoundError("no bundle"),
        ):
            diag.launchVbCableSetup()

        self.assertIn("未找到", diag.driverErrorMessage)

    def test_launch_vb_cable_setup_reports_uac_cancellation_as_neutral_info_not_error_or_success(self):
        # XRBM-031 RETRY 1 item 7: a UAC cancellation must never render in
        # the success/green color (driverStatusMessage) - it is neutral
        # informational text (driverInfoMessage) instead.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            vb_cable_bundle,
            "prepare_and_launch_vendor_setup",
            side_effect=vb_cable_bundle.UacCancelledError("用户取消了 UAC 提升请求；未安装任何内容。"),
        ):
            diag.launchVbCableSetup()

        self.assertEqual(diag.driverErrorMessage, "")
        self.assertEqual(diag.driverStatusMessage, "")
        self.assertIn("取消", diag.driverInfoMessage)

    def test_launch_vb_cable_setup_success_is_neutral_info_never_claims_installation_succeeded(self):
        # XRBM-031 RETRY 1 item 7: launching the vendor UI is informational
        # (driverInfoMessage), not a completed success (driverStatusMessage)
        # - only a later endpoint recheck can confirm an actual install.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            vb_cable_bundle, "prepare_and_launch_vendor_setup", return_value=None
        ):
            diag.launchVbCableSetup()

        self.assertEqual(diag.driverErrorMessage, "")
        self.assertEqual(diag.driverStatusMessage, "")
        self.assertIn("重新检查", diag.driverInfoMessage)
        self.assertNotIn("安装成功", diag.driverInfoMessage)


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class RunSettingsWindowShutdownCoverageTests(unittest.TestCase):
    """XRBM-035 RETRY 1 P2/E: ``run_settings_window()``'s production
    shutdown contract must cover every exit path starting right after
    both controllers are constructed, not only ``app.exec()`` returning.
    ``SettingsController`` already owns endpoint/program workers at that
    point; diagnostics are deferred until QML reports its first frame.
    Drives the REAL function end to end (never a source-level/AST proxy) -
    only the Qt WINDOW plumbing (``QGuiApplication``/``QQmlApplicationEngine``
    /``QQuickStyle``/``QUrl``/``qmlRegisterSingletonInstance``) is replaced
    with small, fully-controllable fakes (real PySide6/shiboken C++ types
    are not reliably monkeypatchable, and no real QML window needs to exist
    to prove this contract) - ``ButtonMappingModel``/``SettingsController``/
    ``DiagnosticsController`` stay the REAL classes ``_load_qt_classes()``
    itself already produced, so their cleanup methods and worker lifecycle
    remain the production implementations.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmpdir.name})
        self._env_patch.start()
        qt_settings_app._diagnostics_shutdown_event.clear()

    def tearDown(self):
        qt_settings_app._diagnostics_shutdown_event.clear()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _fake_classes(self, *, load_side_effect=None, root_objects=None, exec_return=0):
        real_classes = qt_settings_app._load_qt_classes()
        root_objects = [] if root_objects is None else root_objects

        class _FakeEngine:
            def addImportPath(self, path):
                pass

            def load(self, url):
                if load_side_effect is not None:
                    raise load_side_effect

            def rootObjects(self):
                return root_objects

        class _FakeApp:
            def __init__(self, argv=None):
                pass

            @staticmethod
            def instance():
                return None

            def exec(self):
                return exec_return

            def quit(self):
                pass

        class _FakeQQuickStyle:
            @staticmethod
            def setStyle(name):
                pass

        class _FakeQUrl:
            @staticmethod
            def fromLocalFile(path):
                return path

        fake_classes = dict(real_classes)
        fake_classes["QGuiApplication"] = _FakeApp
        fake_classes["QQmlApplicationEngine"] = _FakeEngine
        fake_classes["QQuickStyle"] = _FakeQQuickStyle
        fake_classes["QUrl"] = _FakeQUrl
        fake_classes["qmlRegisterSingletonInstance"] = lambda *args, **kwargs: None
        return fake_classes

    def test_engine_load_raising_still_runs_the_production_shutdown_helper(self):
        fake_classes = self._fake_classes(load_side_effect=RuntimeError("simulated engine.load() failure"))
        with mock.patch.object(qt_settings_app, "_load_qt_classes", return_value=fake_classes):
            with mock.patch.object(
                qt_settings_app,
                "_shutdown_diagnostics_workers",
                wraps=qt_settings_app._shutdown_diagnostics_workers,
            ) as shutdown_spy:
                with self.assertRaises(RuntimeError):
                    qt_settings_app.run_settings_window()

        shutdown_spy.assert_called()
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)

    def test_empty_root_objects_still_runs_the_production_shutdown_helper(self):
        fake_classes = self._fake_classes(root_objects=[])
        with mock.patch.object(qt_settings_app, "_load_qt_classes", return_value=fake_classes):
            with mock.patch.object(
                qt_settings_app,
                "_shutdown_diagnostics_workers",
                wraps=qt_settings_app._shutdown_diagnostics_workers,
            ) as shutdown_spy:
                with self.assertRaises(qt_settings_app.QtUnavailableError):
                    qt_settings_app.run_settings_window()

        shutdown_spy.assert_called()
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)

    def test_app_exec_returning_normally_still_runs_the_production_shutdown_helper(self):
        fake_classes = self._fake_classes(root_objects=[object()], exec_return=0)
        with mock.patch.object(qt_settings_app, "_load_qt_classes", return_value=fake_classes):
            with mock.patch.object(
                qt_settings_app,
                "_shutdown_diagnostics_workers",
                wraps=qt_settings_app._shutdown_diagnostics_workers,
            ) as shutdown_spy:
                exit_code = qt_settings_app.run_settings_window()

        self.assertEqual(exit_code, 0)
        shutdown_spy.assert_called()
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)

    def test_loaded_native_window_is_marked_for_duplicate_activation(self):
        class _FakeWindow:
            def winId(self):
                return 4321

        fake_classes = self._fake_classes(root_objects=[_FakeWindow()], exec_return=0)
        with mock.patch.object(
            qt_settings_app, "_load_qt_classes", return_value=fake_classes
        ), mock.patch.object(
            qt_settings_app.sys, "platform", "win32"
        ), mock.patch.object(
            qt_settings_app.single_instance,
            "mark_settings_window",
            return_value=True,
        ) as marker:
            self.assertEqual(qt_settings_app.run_settings_window(), 0)

        marker.assert_called_once_with(4321)

    def test_settings_cleanup_failure_cannot_skip_diagnostics_shutdown(self):
        fake_classes = self._fake_classes(root_objects=[object()], exec_return=0)
        controller_class = fake_classes["SettingsController"]
        cleanup_calls = []

        def fail_cleanup(instance):
            cleanup_calls.append(instance)
            raise RuntimeError("simulated settings cleanup failure")

        with mock.patch.object(
            qt_settings_app, "_load_qt_classes", return_value=fake_classes
        ), mock.patch.object(
            controller_class,
            "shutdownForProcessExit",
            side_effect=fail_cleanup,
            autospec=True,
        ), mock.patch.object(
            qt_settings_app,
            "_shutdown_diagnostics_workers",
            wraps=qt_settings_app._shutdown_diagnostics_workers,
        ) as shutdown_spy:
            with self.assertRaises(RuntimeError):
                qt_settings_app.run_settings_window()

        self.assertEqual(len(cleanup_calls), 1)
        shutdown_spy.assert_called()
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)


# Loads the REAL qml/main.qml (not a stand-in snippet) with QT_QPA_
# PLATFORM=offscreen and reports rootObjects()/warnings/window size as
# JSON - run in an isolated subprocess (see OffscreenQmlLoadTests below for
# why: a second QQmlApplicationEngine loading ComboBox-containing QML
# within the SAME process as another engine that already loaded one -
# regardless of matching style - empirically broke with "Type ComboBox
# unavailable" / "TextEditingContextMenu unavailable", a separate QQC2
# per-process-singleton limitation from the FluentWinUI3 Config one
# documented on RenderedContrastTests above).
_APPLICATION_EXIT_PROBE_SCRIPT = r"""
import json
import time

from PySide6.QtCore import QTimer
from ovb_rc003 import qt_settings_app as m

m.single_instance.bridge_instance_running = lambda: False
original_connect_application_exit = m._connect_application_exit


def connect_application_exit_and_schedule(app, controller):
    original_connect_application_exit(app, controller)
    QTimer.singleShot(50, controller.requestApplicationExit)


m._connect_application_exit = connect_application_exit_and_schedule
started = time.monotonic()
result = m.run_settings_window(start_hidden=True)
print(json.dumps({
    "result": result,
    "elapsed": time.monotonic() - started,
}))
"""


_QML_LOAD_PROBE_SCRIPT = r"""
import faulthandler
import json
import sys
import time

# XRBM-034's "engine.warnings connected to a Python callback" theory for
# this probe's 0xC0000005 was disproven by real Windows CI evidence
# (XRBM-034 REPLAN, run 29681031609): a named-callback connect()/
# disconnect() pair still crashed AFTER printing STAGE:connected/loaded/
# disconnected, and faulthandler's own thread dump showed the actual crash
# thread deep inside ble_transport_winrt.discover_candidates()'s WinRT
# await - a background DiagnosticsController worker this script starts
# below, not anything QML-warnings-related. faulthandler.enable() is kept
# (XRBM-035 In-scope item 6) purely as cheap forensic insurance if this
# script ever fails again - it costs nothing on the passing path.
faulthandler.enable()

from ovb_rc003 import qt_settings_app as m
from PySide6.QtCore import QObject

classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle("Basic")
app = QGuiApplication.instance() or QGuiApplication([])
m.single_instance.bridge_instance_running = lambda: False
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))

warnings = []
engine.warnings.connect(lambda ws: warnings.extend(ws))
print("STAGE:connected", file=sys.stderr, flush=True)

engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
print("STAGE:loaded", file=sys.stderr, flush=True)

root_objects = engine.rootObjects()
app.processEvents()
initial_settings_dirty = bool(controller.settingsDirty)
tab_bar = root_objects[0].findChild(QObject, "tabBar") if root_objects else None
if tab_bar is not None:
    tab_bar.setProperty("currentIndex", 2)
    app.processEvents()
voice_scroll = (
    root_objects[0].findChild(QObject, "voiceScroll") if root_objects else None
)
voice_page = voice_scroll.parent() if voice_scroll is not None else None
if voice_page is not None:
    voice_page.setProperty("voiceHotkeyRecording", True)
    controller.hotkeyCaptured.emit("ctrl+shift+f8")
    app.processEvents()
    deadline = time.monotonic() + 5.0
    while controller.voiceHotkeyBusy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
saved_config = m.config.load_config(m.config.config_path(m.config.config_root()))
voice_save_status = controller.statusMessage
status_bar = (
    root_objects[0].findChild(QObject, "globalStatusBar") if root_objects else None
)
tab_bar.setProperty("currentIndex", 0)
app.processEvents()
voice_feedback_on_device = bool(status_bar.property("hasStatus"))
tab_bar.setProperty("currentIndex", 2)
app.processEvents()
voice_feedback_on_voice = bool(status_bar.property("hasStatus"))
tab_bar.setProperty("currentIndex", 1)
model.setActionTextAt(model.index_of("power"), "escape")
app.processEvents()
mapping_dirty_on_buttons = bool(status_bar.property("hasStatus"))
tab_bar.setProperty("currentIndex", 2)
app.processEvents()
mapping_dirty_on_voice = bool(status_bar.property("hasStatus"))
result = {
    "root_count": len(root_objects),
    "warnings": [w.toString() for w in warnings],
    "width": root_objects[0].property("width") if root_objects else None,
    "height": root_objects[0].property("height") if root_objects else None,
    "initial_settings_dirty": initial_settings_dirty,
    "retired_finish_tap_control_exists": bool(
        root_objects
        and root_objects[0].findChild(QObject, "voiceReleaseFinishTapSwitch")
    ),
    "voice_hotkey_recording": (
        voice_page.property("voiceHotkeyRecording")
        if voice_page is not None else None
    ),
    "voice_hotkey_busy": bool(controller.voiceHotkeyBusy),
    "saved_voice_hotkey": saved_config.get("voice_hotkeys", {}).get("hold"),
    "voice_save_status": voice_save_status,
    "voice_feedback_on_device": voice_feedback_on_device,
    "voice_feedback_on_voice": voice_feedback_on_voice,
    "mapping_dirty_on_buttons": mapping_dirty_on_buttons,
    "mapping_dirty_on_voice": mapping_dirty_on_voice,
}

# XRBM-035: the real fast-close gate this probe exists to be - calls the
# EXACT SAME production shutdown helper run_settings_window() calls right
# after app.exec() returns (see qt_settings_app.py's module docstring and
# _shutdown_diagnostics_workers()'s own docstring), while every Qt/Python
# object built above (including the DiagnosticsController constructed by
# _load_qt_classes()/DiagnosticsController(...) above, which already
# started a REAL background BLE-discovery worker thread in its own
# __init__ - never faked/skipped here) is still fully alive. This is what
# actually reproduces the real settings-window-closes-quickly race, and
# actually exercises the fix for it.
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
print("STAGE:shutdown", file=sys.stderr, flush=True)

print(json.dumps(result))
"""


_RC003_ONLY_DEVICE_PAGE_PROBE_SCRIPT = r"""
import json

from ovb_rc003 import qt_settings_app as m


def find_child(root, name):
    children = list(root.children())
    child_items = getattr(root, "childItems", None)
    if callable(child_items):
        children.extend(child for child in child_items() if child not in children)
    for child in children:
        if child.objectName() == name:
            return child
        found = find_child(child, name)
        if found is not None:
            return found
    return None


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle("Basic")
app = QGuiApplication.instance() or QGuiApplication([])
m.single_instance.bridge_instance_running = lambda: False
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
assert len(engine.rootObjects()) == 1, "main.qml failed to load"
window = engine.rootObjects()[0]
window.show()
tab_bar = find_child(window, "tabBar")
assert tab_bar is not None
tab_bar.setProperty("currentIndex", 1)
for _ in range(10):
    window.grabWindow()
    app.processEvents()

rc003_layout = find_child(window, "rc003MappingLayout")
assert rc003_layout is not None
result = {
    "rc003_visible": bool(rc003_layout.property("visible")),
    "mapping_page_title": controller.mappingPageTitle,
    "device_options": list(controller.deviceOptions),
}
tab_bar.setProperty("currentIndex", 0)
for _ in range(10):
    window.grabWindow()
    app.processEvents()
result["device_rows"] = all(
    find_child(window, name) is not None
    for name in (
        "currentDeviceRow",
        "buttonReceiverRow",
        "remoteServiceRow",
        "runtimeLogRow",
    )
)
tab_bar.setProperty("currentIndex", 2)
for _ in range(10):
    window.grabWindow()
    app.processEvents()
result["voice_sections"] = all(
    find_child(window, name) is not None
    for name in (
        "audioPrerequisiteSection",
        "voiceProgramSection",
        "voiceTestSection",
    )
)
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
print(json.dumps(result))
"""


_SETTINGS_SHELL_LAYOUT_PROBE_SCRIPT = r"""
import json
import os

from PySide6.QtCore import QPointF
from PySide6.QtTest import QTest
from ovb_rc003 import qt_settings_app as m


def find_child(root, name):
    children = list(root.children())
    child_items = getattr(root, "childItems", None)
    if callable(child_items):
        children.extend(child for child in child_items() if child not in children)
    for child in children:
        if child.objectName() == name:
            return child
        found = find_child(child, name)
        if found is not None:
            return found
    return None


def render(window, app):
    for _ in range(12):
        window.grabWindow()
        app.processEvents()


def bounds(window, name):
    item = find_child(window, name)
    assert item is not None, name + " missing"
    origin = item.mapToScene(QPointF(0, 0))
    width = float(item.property("width"))
    height = float(item.property("height"))
    return {
        "visible": bool(item.property("visible")),
        "x": float(origin.x()),
        "y": float(origin.y()),
        "width": width,
        "height": height,
        "right": float(origin.x()) + width,
        "bottom": float(origin.y()) + height,
    }


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle(os.environ.get("PROBE_STYLE", "Basic"))
app = QGuiApplication.instance() or QGuiApplication([])
bridge_state = {"running": False}
m.single_instance.bridge_instance_running = lambda: bridge_state["running"]
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
warnings = []
engine.warnings.connect(lambda values: warnings.extend(values))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
assert len(engine.rootObjects()) == 1, "main.qml failed to load"
window = engine.rootObjects()[0]
window.setWidth(int(os.environ["PROBE_WIDTH"]))
window.setHeight(int(os.environ["PROBE_HEIGHT"]))
window.show()
render(window, app)

tab_bar = find_child(window, "tabBar")
assert tab_bar is not None
status_bar = find_child(window, "globalStatusBar")
status_text = find_child(window, "globalStatusText")
assert status_bar is not None and status_text is not None

result = {
    "warnings": [],
    "width": int(window.property("width")),
    "height": int(window.property("height")),
    "initial_status_visible": bool(status_bar.property("visible")),
    "initial_status_has_status": bool(status_bar.property("hasStatus")),
    "initial_status_text_visible": bool(status_text.property("visible")),
    "navigation": {
        name: bounds(window, name)
        for name in (
            "navigationBar",
            "connectionTabButton",
            "mappingTabButton",
            "permissionsTabButton",
            "diagnosticsTabButton",
        )
    },
}

tab_bar.setProperty("currentIndex", 0)
render(window, app)
connection_names = (
    "connectionPageContent",
    "deviceSection",
    "deviceCombo",
    "rc003OutputSection",
    "endpointCombo",
    "bridgeSection",
    "bridgeNotRunningWarning",
    "connectionActionRow",
    "restoreDefaultsButton",
    "deviceSaveButton",
    "saveAndLaunchButton",
)
connection_scroll = find_child(window, "connectionScroll")
result["connection"] = {
    "items": {name: bounds(window, name) for name in connection_names},
    "content_width": float(connection_scroll.property("contentWidth")),
    "available_width": float(connection_scroll.property("availableWidth")),
    "launch_status": str(find_child(window, "launchStatusText").property("text")),
    "bridge_warning": str(find_child(window, "bridgeNotRunningWarning").property("text")),
    "save_highlighted": bool(find_child(window, "deviceSaveButton").property("highlighted")),
    "launch_highlighted": bool(find_child(window, "saveAndLaunchButton").property("highlighted")),
}

controller._bridge_launch_elapsed_seconds = 12
controller.bridgeLaunchElapsedSecondsChanged.emit()
controller._set_bridge_launch_phase("waiting")
render(window, app)
result["connection"]["waiting_progress"] = {
    "visible": bool(find_child(window, "bridgeLaunchProgress").property("visible")),
    "indicator_running": bool(
        find_child(window, "bridgeLaunchBusyIndicator").property("running")
    ),
    "stage_text": str(find_child(window, "bridgeLaunchStageText").property("text")),
    "elapsed_visible": bool(
        find_child(window, "bridgeLaunchElapsedText").property("visible")
    ),
    "elapsed_text": str(find_child(window, "bridgeLaunchElapsedText").property("text")),
}

controller._set_bridge_launch_phase("connected")
render(window, app)
result["connection"]["connected_progress"] = {
    "visible": bool(find_child(window, "bridgeLaunchProgress").property("visible")),
    "indicator_running": bool(
        find_child(window, "bridgeLaunchBusyIndicator").property("running")
    ),
    "stage_text": str(find_child(window, "bridgeLaunchStageText").property("text")),
}

controller._set_launch_status("AUDIT_LAUNCH_RESULT_MUST_BE_VISIBLE")
render(window, app)
result["connection"]["explicit_launch_status"] = str(
    find_child(window, "launchStatusText").property("text")
)

bridge_timer = find_child(window, "bridgeStatusRefreshTimer")
assert bridge_timer is not None
bridge_state["running"] = True
controller.refreshBridgeState()
render(window, app)
result["connection"]["warning_after_external_start"] = str(
    find_child(window, "bridgeNotRunningWarning").property("text")
)
bridge_state["running"] = False
controller.refreshBridgeState()
render(window, app)
result["connection"]["warning_after_external_exit"] = str(
    find_child(window, "bridgeNotRunningWarning").property("text")
)

tab_bar.setProperty("currentIndex", 1)
render(window, app)
mapping_names = (
    "rc003MappingLayout",
    "mappingActionsPanel",
    "mappingList",
    "mappingLines",
    "activeMappingLine",
    "leftMappingCards",
    "photoSidebar",
    "rightMappingCards",
    "mappingListFrame",
    "detectRealKeyButton",
    "restoreMappingDefaultsButton",
    "saveMappingButton",
    "editMapping_power",
    "editMapping_tv",
)
result["mapping"] = {
    "items": {name: bounds(window, name) for name in mapping_names},
}

tab_bar.setProperty("currentIndex", 2)
render(window, app)
permissions_names = (
    "permissionsPageContent",
    "requiredPermissionsSection",
    "openBluetoothSettingsButton",
    "openMicrophonePrivacyButton",
    "openSoundInputSettingsButton",
)
permissions_scroll = find_child(window, "permissionsScroll")
result["permissions"] = {
    "items": {name: bounds(window, name) for name in permissions_names},
    "content_width": float(permissions_scroll.property("contentWidth")),
    "available_width": float(permissions_scroll.property("availableWidth")),
}

tab_bar.setProperty("currentIndex", 3)
render(window, app)
diagnostics_names = (
    "diagnosticsPageContent",
    "refreshButton",
    "optionalDriverSection",
    "optionalDriverDescription",
    "selectCableInputButton",
    "launchDriverSetupButton",
    "vbCableChannelTestSection",
    "vbCableChannelDescription",
    "testVbCableChannelButton",
    "diagnosticsFooterSection",
    "diagnosticsFooterDescription",
    "openSpeechSettingsButton",
    "diagnosticsOpenLogButton",
)
diagnostics_scroll = find_child(window, "diagnosticsScroll")
result["diagnostics"] = {
    "items": {name: bounds(window, name) for name in diagnostics_names},
    "content_width": float(diagnostics_scroll.property("contentWidth")),
    "available_width": float(diagnostics_scroll.property("availableWidth")),
    "content_height": float(diagnostics_scroll.property("contentHeight")),
    "available_height": float(diagnostics_scroll.property("availableHeight")),
}

diagnostics_controller._check_rows = [
    {
        "checkId": "audit_refresh",
        "title": "刷新测试",
        "group": "ordinary_buttons",
        "status": "pass",
        "detail": "AUDIT_DETAIL_FIRST",
    }
]
diagnostics_controller.checkResultsChanged.emit()
render(window, app)
diagnostic_result = find_child(window, "diagnosticResult_audit_refresh")
assert diagnostic_result is not None
result["diagnostics"]["detail_before_refresh"] = str(
    diagnostic_result.property("detailText")
)

diagnostics_controller._check_rows = [
    {
        "checkId": "audit_refresh",
        "title": "刷新测试",
        "group": "ordinary_buttons",
        "status": "fail",
        "detail": "AUDIT_DETAIL_AFTER_REFRESH",
    }
]
diagnostics_controller.checkResultsChanged.emit()
render(window, app)
diagnostic_result = find_child(window, "diagnosticResult_audit_refresh")
assert diagnostic_result is not None
result["diagnostics"]["detail_after_refresh"] = str(
    diagnostic_result.property("detailText")
)

controller._set_status_message("neutral status")
render(window, app)
result["neutral_status"] = {
    "visible": bool(status_bar.property("visible")),
    "text": str(status_text.property("text")),
}
controller._set_error_message("priority error")
render(window, app)
result["error_status"] = {
    "visible": bool(status_bar.property("visible")),
    "text": str(status_text.property("text")),
}
result["warnings"] = [warning.toString() for warning in warnings]

controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
print(json.dumps(result))
"""


_TAB_FOCUS_SCROLL_PROBE_SCRIPT = r"""
import json

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from ovb_rc003 import qt_settings_app as m


def find_child(root, name):
    children = list(root.children())
    child_items = getattr(root, "childItems", None)
    if callable(child_items):
        children.extend(child for child in child_items() if child not in children)
    for child in children:
        if child.objectName() == name:
            return child
        found = find_child(child, name)
        if found is not None:
            return found
    return None


def render(window, app):
    for _ in range(10):
        window.grabWindow()
        app.processEvents()


def tab_to(window, app, target, limit=20):
    for _ in range(limit):
        if bool(target.property("activeFocus")):
            return True
        QTest.keyClick(window, Qt.Key_Tab)
        render(window, app)
    return bool(target.property("activeFocus"))


def navigation_has_focus(window):
    return any(
        bool(find_child(window, name).property("activeFocus"))
        for name in (
            "connectionTabButton",
            "mappingTabButton",
            "permissionsTabButton",
            "diagnosticsTabButton",
        )
    )


def visible_in_window(item, window):
    origin = item.mapToScene(QPointF(0, 0))
    return (
        origin.y() >= 0
        and origin.y() + float(item.property("height")) <= float(window.property("height"))
    )


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle("Basic")
app = QGuiApplication.instance() or QGuiApplication([])
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
warnings = []
engine.warnings.connect(lambda values: warnings.extend(values))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
assert len(engine.rootObjects()) == 1, "main.qml failed to load"
window = engine.rootObjects()[0]
window.setWidth(640)
window.setHeight(480)
window.show()
render(window, app)

tab_bar = find_child(window, "tabBar")
tab_bar.setProperty("currentIndex", 0)
render(window, app)
connection_scroll = find_child(window, "connectionScroll")
connection_flickable = connection_scroll.property("contentItem")
connection_flickable.setProperty("contentY", 0)
endpoint = find_child(window, "endpointCombo")
launch = find_child(window, "saveAndLaunchButton")
endpoint.forceActiveFocus(Qt.TabFocusReason)
render(window, app)
connection_reached = tab_to(window, app, launch)
QTest.keyClick(window, Qt.Key_Tab)
render(window, app)
connection_escaped = not bool(launch.property("activeFocus")) and navigation_has_focus(window)

tab_bar.setProperty("currentIndex", 2)
render(window, app)
permissions_scroll = find_child(window, "permissionsScroll")
permissions_flickable = permissions_scroll.property("contentItem")
permissions_flickable.setProperty("contentY", 0)
microphone = find_child(window, "openMicrophonePrivacyButton")
sound_button = find_child(window, "openSoundInputSettingsButton")
microphone.forceActiveFocus(Qt.TabFocusReason)
render(window, app)
permissions_reached = tab_to(window, app, sound_button)
QTest.keyClick(window, Qt.Key_Tab)
render(window, app)
permissions_escaped = not bool(sound_button.property("activeFocus")) and navigation_has_focus(window)

result = {
    "warnings": [warning.toString() for warning in warnings],
    "connection": {
        "reached": connection_reached,
        "escaped": connection_escaped,
        "content_y": float(connection_flickable.property("contentY")),
        "target_visible": visible_in_window(launch, window),
    },
    "permissions": {
        "reached": permissions_reached,
        "escaped": permissions_escaped,
        "content_y": float(permissions_flickable.property("contentY")),
        "target_visible": visible_in_window(sound_button, window),
    },
}
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
print(json.dumps(result))
"""


_SELECTION_COMBO_STATE_PROBE_SCRIPT = r"""
import json

from ovb_rc003 import qt_settings_app as m
from PySide6.QtCore import QObject, QMetaObject, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuickControls2 import QQuickStyle

QQuickStyle.setStyle("Basic")
app = QGuiApplication.instance() or QGuiApplication([])
engine = QQmlEngine()
qml_dir = m._qml_directory()
component = QQmlComponent(engine)
component.setData(
    b'''import QtQuick
import QtQuick.Controls
ApplicationWindow {
    visible: true
    width: 480
    height: 280
    SelectionComboBox {
        objectName: "emptyCombo"
        model: []
        currentIndex: -1
        recommendedIndex: -1
    }
    SelectionComboBox {
        objectName: "ordinaryCombo"
        model: ["Speakers"]
        currentIndex: 0
        recommendedIndex: -1
    }
    SelectionComboBox {
        objectName: "recommendedCombo"
        model: ["CABLE Input"]
        currentIndex: 0
        recommendedIndex: 0
    }
    SelectionComboBox {
        id: movingCombo
        objectName: "movingCombo"
        y: 120
        width: 320
        model: ["First", "Second"]
        currentIndex: 0
        recommendedIndex: -1
        property bool firstBoldForProbe: false
        property bool secondBoldForProbe: false
        function openPopupForProbe() {
            popup.open()
        }
        function selectSecondForProbe() {
            currentIndex = 1
        }
        function captureBoldForProbe() {
            const first = popup.contentItem.itemAtIndex(0)
            const second = popup.contentItem.itemAtIndex(1)
            firstBoldForProbe = first ? first.contentItem.font.weight >= Font.DemiBold : false
            secondBoldForProbe = second ? second.contentItem.font.weight >= Font.DemiBold : false
        }
    }
}''',
    QUrl.fromLocalFile(str(qml_dir / "SelectionComboBoxProbe.qml")),
)
assert component.isReady(), [error.toString() for error in component.errors()]
window = component.create()
assert window is not None, [error.toString() for error in component.errors()]
app.processEvents()

result = {}
for key, object_name in (
    ("empty", "emptyCombo"),
    ("ordinary", "ordinaryCombo"),
    ("recommended", "recommendedCombo"),
):
    combo = window.findChild(QObject, object_name)
    assert combo is not None, object_name
    result[key] = combo.property("displayText")

moving = window.findChild(QObject, "movingCombo")
assert moving is not None
assert QMetaObject.invokeMethod(
    moving,
    "openPopupForProbe",
    Qt.ConnectionType.DirectConnection,
)
for _ in range(12):
    app.processEvents()
    window.grabWindow()
assert QMetaObject.invokeMethod(
    moving,
    "captureBoldForProbe",
    Qt.ConnectionType.DirectConnection,
)
result["bold_initial"] = [
    moving.property("firstBoldForProbe"),
    moving.property("secondBoldForProbe"),
]
assert QMetaObject.invokeMethod(
    moving,
    "selectSecondForProbe",
    Qt.ConnectionType.DirectConnection,
)
for _ in range(4):
    app.processEvents()
    window.grabWindow()
assert QMetaObject.invokeMethod(
    moving,
    "captureBoldForProbe",
    Qt.ConnectionType.DirectConnection,
)
result["bold_after_move"] = [
    moving.property("firstBoldForProbe"),
    moving.property("secondBoldForProbe"),
]

print(json.dumps(result, ensure_ascii=False))
"""


_THREE_PAGE_LAYOUT_PROBE_SCRIPT = r"""
import json
import os

from PySide6.QtCore import QMetaObject, QPointF, Qt
from ovb_rc003 import qt_settings_app as m


def find_child(root, name):
    children = list(root.children())
    child_items = getattr(root, "childItems", None)
    if callable(child_items):
        children.extend(child for child in child_items() if child not in children)
    for child in children:
        if child.objectName() == name:
            return child
        found = find_child(child, name)
        if found is not None:
            return found
    return None


def render(window, app, count=10):
    image = None
    for _ in range(count):
        image = window.grabWindow()
        app.processEvents()
    return image


def bounds(window, name):
    item = find_child(window, name)
    assert item is not None, name + " missing"
    map_to_scene = getattr(item, "mapToScene", None)
    if callable(map_to_scene):
        origin = map_to_scene(QPointF(0, 0))
        origin_x = float(origin.x())
        origin_y = float(origin.y())
    else:
        origin_x = float(item.property("x"))
        origin_y = float(item.property("y"))
    width = float(item.property("width"))
    height = float(item.property("height"))
    return {
        "visible": bool(item.property("visible")),
        "x": origin_x,
        "y": origin_y,
        "width": width,
        "height": height,
        "right": origin_x + width,
        "bottom": origin_y + height,
    }


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle(os.environ.get("PROBE_STYLE", "Basic"))
app = QGuiApplication.instance() or QGuiApplication([])
m.single_instance.bridge_instance_running = lambda: False
m.windows_diagnostics.run_diagnostics = lambda **_kwargs: (
    m.windows_diagnostics.DiagnosticsReport(checks=())
)
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(
    SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller
)
qmlRegisterSingletonInstance(
    ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model
)
qmlRegisterSingletonInstance(
    DiagnosticsController,
    "OvbRc003Settings",
    1,
    0,
    "DiagnosticsController",
    diagnostics_controller,
)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
warnings = []
engine.warnings.connect(lambda values: warnings.extend(values))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
assert len(engine.rootObjects()) == 1, "main.qml failed to load"
window = engine.rootObjects()[0]
window.setWidth(int(os.environ["PROBE_WIDTH"]))
window.setHeight(int(os.environ["PROBE_HEIGHT"]))
window.show()
render(window, app)

tab_bar = find_child(window, "tabBar")
assert tab_bar is not None
result = {
    "width": float(window.property("width")),
    "height": float(window.property("height")),
    "warnings": [],
    "client_shell": bounds(window, "clientShell"),
    "client_shell_outline": bounds(window, "clientShellOutline"),
    "global_status_bar": bounds(window, "globalStatusBar"),
    "pages": {},
}


def capture_page(index, name, content_name, item_names):
    tab_bar.setProperty("currentIndex", index)
    render(window, app)
    screenshot_dir = os.environ.get("PROBE_SCREENSHOT_DIR")
    if screenshot_dir:
        os.makedirs(screenshot_dir, exist_ok=True)
        render(window, app, 2).save(
            os.path.join(
                screenshot_dir,
                f"{os.environ.get('PROBE_STYLE', 'Basic')}-"
                f"{int(result['width'])}x{int(result['height'])}-{name}.png",
            )
        )
    result["pages"][name] = {
        "content": bounds(window, content_name),
        "items": {item_name: bounds(window, item_name) for item_name in item_names},
    }


capture_page(
    0,
    "device",
    "devicePageContent",
    (
        "devicePrerequisiteSection",
        "devicePrerequisiteSectionTitle",
        "currentDeviceRow",
        "buttonReceiverRow",
        "remoteServiceRow",
        "runtimeLogRow",
        "desktopBehaviorSection",
        "desktopBehaviorSectionTitle",
        "launchAtLoginRow",
        "launchBridgeOnAppStartRow",
        "closeBehaviorRow",
    ),
)
capture_page(
    1,
    "buttons",
    "rc003MappingLayout",
    (
        "mappingActionsPanel",
        "mappingList",
        "photoSidebar",
    ),
)
controller.selectedVoiceProgramIndex = 4
capture_page(
    2,
    "voice",
    "voicePageContent",
    (
        "audioPrerequisiteSection",
        "audioPrerequisiteSectionTitle",
        "voiceProgramSection",
        "voiceProgramSectionTitle",
        "voiceProgramCustomPathRow",
        "voiceTestSection",
        "voiceTestSectionTitle",
    ),
)

voice_row_names = (
    "virtualAudioRow",
    "outputEndpointRow",
    "microphonePrivacyRow",
    "voiceProgramSelectionRow",
    "voiceProgramCustomPathRow",
    "voiceHotkeyRow",
    "voiceProgramSpecificRow",
    "soundChannelTestRow",
    "actualSpeechTestRow",
)
result["voice_columns"] = {
    "states": {
        name: bounds(window, name + "_stateColumn") for name in voice_row_names
    },
    "actions": {
        name: bounds(window, name + "_actionColumn") for name in voice_row_names
    },
    "editors": {
        name: bounds(window, name)
        for name in (
            "endpointCombo",
            "voiceProgramCombo",
            "voiceProgramCustomPathField",
            "holdVoiceHotkeyField",
        )
    },
    "right_controls": {
        name: bounds(window, name)
        for name in (
            "applyVirtualAudioButton",
            "openMicrophonePrivacyButton",
            "browseVoiceProgramButton",
            "voiceProgramElevatedCheckBox",
            "testVbCableChannelButton",
            "trySpeakingButton",
        )
    },
    "left_controls": {
        "installVirtualAudioButton": bounds(window, "installVirtualAudioButton"),
    },
    "descriptions": {
        name: bounds(window, name)
        for name in (
            "voiceProgramLaunchText",
            "soundChannelTestDescription",
            "actualSpeechTestDescription",
        )
    },
    "editor_columns": {
        name: bounds(window, name + "_editorColumn")
        for name in (
            "voiceProgramSpecificRow",
            "soundChannelTestRow",
            "actualSpeechTestRow",
        )
    },
}

diagnostics_controller._set_vb_cable_bridge_recovery_needed(True)
render(window, app)
result["voice_recovery_editor"] = {
    "column": bounds(window, "soundChannelTestRow_editorColumn"),
    "button": bounds(window, "recoverBridgeButton"),
}
diagnostics_controller._set_vb_cable_bridge_recovery_needed(False)
render(window, app)

controller.selectedVoiceProgramIndex = 3
render(window, app)
result["voice_windows_actions"] = {
    name: bounds(window, name)
    for name in (
        "openVoiceProgramSettingsButton",
    )
}

speak_dialog = find_child(window, "speakTestDialog")
assert speak_dialog is not None
assert QMetaObject.invokeMethod(
    speak_dialog,
    "open",
    Qt.ConnectionType.DirectConnection,
)
render(window, app)
result["speak_dialog"] = {
    "dialog": bounds(window, "speakTestDialog"),
    "input_frame": bounds(window, "speakTestInputFrame"),
    "input": bounds(window, "speakTestInput"),
    "close": bounds(window, "speakTestCloseButton"),
}
screenshot_dir = os.environ.get("PROBE_SCREENSHOT_DIR")
if screenshot_dir:
    render(window, app, 2).save(
        os.path.join(
            screenshot_dir,
            f"{os.environ.get('PROBE_STYLE', 'Basic')}-"
            f"{int(result['width'])}x{int(result['height'])}-speak-dialog.png",
        )
    )
speak_dialog.close()
speak_dialog.setProperty("visible", False)
render(window, app, 2)

mapping_button = find_child(window, "mappingTabButton")
tab_bar.setProperty("currentIndex", 2)
render(window, app, 2)
assert QMetaObject.invokeMethod(
    mapping_button,
    "pressed",
    Qt.ConnectionType.DirectConnection,
)
result["mapping_index_on_press"] = int(tab_bar.property("currentIndex"))
result["navigation_backgrounds"] = {
    name: bounds(window, name)
    for name in (
        "deviceTabButton",
        "deviceTabButton_background",
        "mappingTabButton",
        "mappingTabButton_background",
        "voiceTabButton",
        "voiceTabButton_background",
    )
}

result["warnings"] = [warning.toString() for warning in warnings]
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
print(json.dumps(result))
"""


class SettingsShellSourceContractTests(unittest.TestCase):
    def setUp(self):
        qml_dir = Path(qt_settings_app.__file__).resolve().parent / "qml"
        self.main_qml = (qml_dir / "main.qml").read_text(encoding="utf-8")
        self.device_qml = (qml_dir / "DevicePage.qml").read_text(encoding="utf-8")
        self.voice_qml = (qml_dir / "VoicePage.qml").read_text(encoding="utf-8")
        self.buttons_qml = (qml_dir / "ButtonsPage.qml").read_text(
            encoding="utf-8"
        )
        self.section_frame_qml = (qml_dir / "SectionFrame.qml").read_text(
            encoding="utf-8"
        )
        self.form_field_qml = (qml_dir / "FormField.qml").read_text(
            encoding="utf-8"
        )
        self.ui_label_qml = (qml_dir / "UiLabel.qml").read_text(
            encoding="utf-8"
        )
        self.selection_combo_qml = (qml_dir / "SelectionComboBox.qml").read_text(
            encoding="utf-8"
        )
        self.nav_button_qml = (qml_dir / "NavButton.qml").read_text(
            encoding="utf-8"
        )
        self.dialog_close_button_qml = (qml_dir / "DialogCloseButton.qml").read_text(
            encoding="utf-8"
        )
        self.icon_glyph_qml = (qml_dir / "IconGlyph.qml").read_text(
            encoding="utf-8"
        )
        self.inline_settings_row_qml = (qml_dir / "InlineSettingsRow.qml").read_text(
            encoding="utf-8"
        )
        self.mapping_card_qml = (qml_dir / "MappingCard.qml").read_text(
            encoding="utf-8"
        )
        self.mapping_key_label_qml = (qml_dir / "MappingKeyLabel.qml").read_text(
            encoding="utf-8"
        )
        self.tokens_qml = (qml_dir / "Tokens.qml").read_text(encoding="utf-8")
        self.compact_tooltip_qml = (qml_dir / "CompactToolTip.qml").read_text(
            encoding="utf-8"
        )
        self.settings_list_row_qml = (qml_dir / "SettingsListRow.qml").read_text(
            encoding="utf-8"
        )
        self.settings_section_title_qml = (
            qml_dir / "SettingsSectionTitle.qml"
        ).read_text(encoding="utf-8")
        self.diagnostic_result_row_qml = (
            qml_dir / "DiagnosticResultRow.qml"
        ).read_text(encoding="utf-8")

    def test_settings_feedback_has_one_global_owner(self):
        self.assertIn('objectName: "globalStatusBar"', self.main_qml)
        self.assertIn(
            "SettingsController.activePageIndex = currentIndex",
            self.main_qml,
        )
        self.assertIn(
            "SettingsController.feedbackPageIndex === tabBar.currentIndex",
            self.main_qml,
        )
        self.assertIn(
            "tabBar.currentIndex === 1 && SettingsController.settingsDirty",
            self.main_qml,
        )
        for page_text in (
            self.device_qml,
            self.voice_qml,
            self.buttons_qml,
        ):
            self.assertNotIn("SettingsController.errorMessage", page_text)
            self.assertNotIn("SettingsController.statusMessage", page_text)

    def test_touched_pages_reuse_shared_compact_sources(self):
        self.assertIn("default property alias contentData", self.section_frame_qml)
        self.assertIn("default property alias editorData", self.form_field_qml)
        for kind in ("bodyKind", "noteKind", "sectionTitleKind", "pageTitleKind"):
            self.assertIn(kind, self.ui_label_qml)

        for page_text in (
            self.device_qml,
            self.voice_qml,
        ):
            self.assertIn("SectionFrame {", page_text)
            self.assertIn("InlineSettingsRow {", page_text)

        self.assertIn("SelectionComboBox {", self.voice_qml)
        self.assertIn("CompactTextField {", self.voice_qml)
        self.assertIn(
            "property string descriptionObjectName", self.inline_settings_row_qml
        )
        self.assertIn("property alias editorData", self.inline_settings_row_qml)
        self.assertIn("property bool editorColumnVisible", self.inline_settings_row_qml)
        self.assertIn("property int stateColumnWidth", self.inline_settings_row_qml)
        self.assertIn("property int actionColumnWidth", self.inline_settings_row_qml)
        self.assertIn("default property alias actionData", self.inline_settings_row_qml)
        self.assertIn("AbstractButton {", self.mapping_card_qml)
        self.assertIn("implicitHeight: 49", self.mapping_card_qml)
        self.assertNotIn("mappingCardHeight", self.buttons_qml)

    def test_settings_card_section_titles_share_one_compact_style(self):
        self.assertIn("kind: bodyKind", self.settings_section_title_qml)
        self.assertIn("font.weight: Font.Bold", self.settings_section_title_qml)
        self.assertIn(
            "Layout.leftMargin: tokens.spacingLarge",
            self.settings_section_title_qml,
        )
        self.assertIn(
            "Layout.topMargin: tokens.spacingSmall",
            self.settings_section_title_qml,
        )
        self.assertIn(
            "Layout.bottomMargin: tokens.spacingSmall",
            self.settings_section_title_qml,
        )
        self.assertEqual(self.device_qml.count("SettingsSectionTitle {"), 2)
        self.assertEqual(self.voice_qml.count("SettingsSectionTitle {"), 3)
        for object_name in (
            "devicePrerequisiteSectionTitle",
            "desktopBehaviorSectionTitle",
        ):
            self.assertIn(f'objectName: "{object_name}"', self.device_qml)
        for object_name in (
            "audioPrerequisiteSectionTitle",
            "voiceProgramSectionTitle",
            "voiceTestSectionTitle",
        ):
            self.assertIn(f'objectName: "{object_name}"', self.voice_qml)

    def test_mapping_views_share_key_titles_and_combo_table_metrics(self):
        self.assertIn(
            "font.pixelSize: root.tokens.fontSizeMappingKey",
            self.mapping_key_label_qml,
        )
        self.assertIn("font.weight: Font.Medium", self.mapping_key_label_qml)
        self.assertEqual(self.mapping_card_qml.count("MappingKeyLabel {"), 1)
        self.assertEqual(self.buttons_qml.count("MappingKeyLabel {"), 1)

        for token_name in (
            "comboMappingKeyColumnWidth",
            "comboMappingNoteColumnWidth",
            "comboMappingHeaderHeight",
            "comboMappingRowHeight",
            "comboMappingRowSpacing",
            "comboMappingHorizontalPadding",
            "comboMappingColumnSpacing",
        ):
            self.assertIn(f"property int {token_name}", self.tokens_qml)
            self.assertIn(f"tokens.{token_name}", self.buttons_qml)

        combo_rows_index = self.buttons_qml.index('objectName: "comboMappingRows"')
        combo_rows_end = self.buttons_qml.index("Repeater {", combo_rows_index)
        combo_rows_contract = self.buttons_qml[combo_rows_index:combo_rows_end]
        self.assertIn("Layout.fillHeight: false", combo_rows_contract)
        self.assertIn("Item { Layout.fillHeight: true }", self.buttons_qml)

    def test_all_selectors_reuse_one_selected_option_delegate(self):
        self.assertEqual(self.voice_qml.count("SelectionComboBox {"), 2)
        self.assertIn(
            "recommendedIndex: SettingsController.recommendedEndpointIndex",
            self.voice_qml,
        )
        self.assertIn(
            "font.weight: index === root.currentIndex ? Font.DemiBold : Font.Normal",
            self.selection_combo_qml,
        )
        self.assertIn("recommendedIndex >= 0 && index >= 0", self.selection_combo_qml)
        self.assertIn('qsTr("（推荐）")', self.selection_combo_qml)
        self.assertIn("displayText: decoratedText(currentIndex, currentText)", self.selection_combo_qml)
        self.assertEqual(self.buttons_qml.count("SelectionComboBox {"), 1)
        self.assertIn(
            "model: SettingsController.comboModifierOptions",
            self.buttons_qml,
        )
        self.assertIn(
            "model: SettingsController.voiceProgramOptions",
            self.voice_qml,
        )

    def test_log_location_has_one_formal_entry_point(self):
        self.assertNotIn("SettingsController.openLogLocation()", self.voice_qml)
        self.assertNotIn("SettingsController.openLogLocation()", self.buttons_qml)
        self.assertEqual(
            self.device_qml.count("SettingsController.openLogLocation()"),
            1,
        )
        self.assertIn('titleText: qsTr("运行日志")', self.device_qml)

    def test_device_start_and_virtual_audio_apply_keep_distinct_commands(self):
        self.assertIn("SettingsController.startBridge()", self.device_qml)
        self.assertNotIn("SettingsController.saveSettings()", self.device_qml)
        self.assertIn(
            "DiagnosticsController.selectDetectedCableInputAsOutput()",
            self.voice_qml,
        )
        self.assertNotIn("SettingsController.saveSettings()", self.voice_qml)
        self.assertIn(
            "descriptionText: SettingsController.launchStatusText", self.device_qml
        )
        self.assertNotIn("saveAndLaunch", self.device_qml)
        self.assertNotIn("stopBridge", self.device_qml)

    def test_diagnostics_keep_details_in_the_rows_that_use_them(self):
        self.assertIn("function combinedDetail(checkIds, fallback)", self.device_qml)
        self.assertIn("function checkDetail(checkId, fallback)", self.voice_qml)
        for check_id in ("ble_candidate", "os_version", "raw_input"):
            self.assertIn(f'"{check_id}"', self.device_qml)
        for check_id in ("vb_cable_endpoints", "output_endpoint"):
            self.assertIn(f'"{check_id}"', self.voice_qml)
        self.assertNotIn('checkState("dictation")', self.voice_qml)

    def test_diagnostics_exposes_loopback_as_an_explicit_nonautomatic_action(self):
        self.assertIn('objectName: "soundChannelTestRow"', self.voice_qml)
        self.assertIn('objectName: "testVbCableChannelButton"', self.voice_qml)
        self.assertIn(
            "DiagnosticsController.testVbCableChannelWithBridgeRestart()",
            self.voice_qml,
        )
        self.assertIn("DiagnosticsController.vbCableTestRunning", self.voice_qml)
        self.assertIn('objectName: "bridgeTestConfirmDialog"', self.voice_qml)
        self.assertGreaterEqual(
            self.voice_qml.count("!root.configurationWriteBusy"),
            2,
        )

    def test_service_state_has_one_formal_device_page_location(self):
        self.assertIn('objectName: "remoteServiceRow"', self.device_qml)
        self.assertIn('objectName: "restartBridgeButton"', self.device_qml)
        self.assertIn(
            "SettingsController.bridgeRestartRecommended",
            self.device_qml,
        )
        self.assertIn("SettingsController.restartBridge()", self.device_qml)
        self.assertNotIn('objectName: "mappingBridgeWarning"', self.buttons_qml)
        self.assertNotIn('objectName: "mappingListFrame"', self.buttons_qml)
        self.assertIn("SettingsController.bridgeRunning", self.device_qml)
        self.assertIn('return qsTr("未运行")', self.device_qml)
        self.assertNotIn("语音和真实按键检测不可用", self.buttons_qml)

    def test_main_window_owns_the_single_live_bridge_refresh_timer(self):
        self.assertIn('objectName: "bridgeStatusRefreshTimer"', self.main_qml)
        self.assertIn("? 1000 : 2000", self.main_qml)
        self.assertIn("running: true", self.main_qml)
        self.assertIn(
            "onTriggered: SettingsController.refreshBridgeState()",
            self.main_qml,
        )
        self.assertIn("onActiveChanged", self.main_qml)
        self.assertIn('objectName: "bridgeLaunchPollTimer"', self.main_qml)
        self.assertIn("interval: 150", self.main_qml)
        self.assertIn("SettingsController.pollBridgeLaunch()", self.main_qml)
        for page_text in (self.device_qml, self.buttons_qml, self.voice_qml):
            self.assertNotIn("refreshBridgeState()", page_text)

    def test_navigation_switches_on_press_without_internal_check_state(self):
        self.assertIn("checkable: false", self.nav_button_qml)
        for inset in ("leftInset", "rightInset", "topInset", "bottomInset"):
            self.assertIn(f"{inset}: 0", self.nav_button_qml)
        self.assertIn('objectName: root.objectName + "_background"', self.nav_button_qml)
        self.assertEqual(self.main_qml.count("onPressed: window.requestPage("), 3)
        self.assertNotIn("onPressed: tabBar.currentIndex =", self.main_qml)
        self.assertNotIn("onClicked: tabBar.currentIndex =", self.main_qml)

    def test_navigation_and_unsaved_exit_wait_for_cleanup_and_lock_during_save(self):
        self.assertIn("function requestPage(index)", self.main_qml)
        self.assertIn("SettingsController.stopInputCapture()", self.main_qml)
        self.assertIn("function onInputCleanupReady()", self.main_qml)
        self.assertIn("function onInputCleanupFailed(message)", self.main_qml)
        self.assertIn("pendingExitPrompt", self.main_qml)
        self.assertGreaterEqual(
            self.main_qml.count("!SettingsController.settingsSaveBusy"),
            4,
        )
        self.assertIn("Popup.NoAutoClose", self.main_qml)
        save_function = self.main_qml[
            self.main_qml.index("function saveAndExit()"):
            self.main_qml.index("function discardAndExit()")
        ]
        self.assertNotIn("unsavedExitDialog.close()", save_function)
        self.assertIn("onSaveSettingsAndExitFinished", self.main_qml)
        exit_function = self.main_qml[
            self.main_qml.index("function requestFullExit()"):
            self.main_qml.index("function saveAndExit()")
        ]
        busy_index = exit_function.index("SettingsController.settingsSaveBusy")
        dirty_index = exit_function.index("SettingsController.settingsDirty")
        self.assertLess(busy_index, dirty_index)
        self.assertIn("SettingsController.requestApplicationExit()", exit_function)
        self.assertIn("property bool applicationExitInProgress: false", self.main_qml)
        self.assertIn("window.applicationExitInProgress = true", exit_function)
        self.assertGreaterEqual(
            self.main_qml.count("&& !window.applicationExitInProgress"),
            2,
        )
        self.assertGreaterEqual(
            self.main_qml.count("window.applicationExitInProgress = false"),
            2,
        )

    def test_device_page_owns_the_three_desktop_behavior_options(self):
        self.assertIn('objectName: "desktopBehaviorSection"', self.device_qml)
        self.assertIn(
            'objectName: "desktopBehaviorSectionTitle"', self.device_qml
        )
        self.assertIn('text: qsTr("通用设置")', self.device_qml)
        self.assertIn('objectName: "launchAtLoginSwitch"', self.device_qml)
        self.assertIn('objectName: "launchBridgeOnAppStartSwitch"', self.device_qml)
        self.assertIn('objectName: "closeBehaviorCombo"', self.device_qml)
        self.assertIn("SettingsController.setLaunchAtLogin", self.device_qml)
        self.assertIn("SettingsController.setLaunchBridgeOnAppStart", self.device_qml)
        self.assertIn("SettingsController.setCloseBehaviorIndex", self.device_qml)
        self.assertIn('titleText: qsTr("启动程序时自动启动桥接")', self.device_qml)
        self.assertIn("与随 Windows 启动互不绑定", self.device_qml)
        self.assertNotIn("GeneralPage", self.main_qml)
        self.assertNotIn('objectName: "generalTabButton"', self.main_qml)
        self.assertIn('objectName: "systemTrayIcon"', self.main_qml)
        self.assertIn("SettingsController.requestApplicationExit()", self.main_qml)
        self.assertIn("SettingsController.applicationExitConfirmed", self.main_qml)

        runtime_log_index = self.device_qml.index(
            'objectName: "runtimeLogRow"'
        )
        general_title_index = self.device_qml.index(
            'objectName: "desktopBehaviorSectionTitle"'
        )
        first_option_index = self.device_qml.index(
            'objectName: "launchAtLoginRow"'
        )
        self.assertLess(runtime_log_index, general_title_index)
        self.assertLess(general_title_index, first_option_index)
        self.assertNotIn("iconGlyph:", self.device_qml)
        self.assertNotIn("property string iconGlyph", self.settings_list_row_qml)
        self.assertNotIn("IconGlyph {", self.settings_list_row_qml)

    def test_windows_prefers_the_system_chinese_ui_font(self):
        self.assertIn(
            'preferredWindowsUiFont: "Microsoft YaHei UI"', self.main_qml
        )
        self.assertIn('Qt.platform.os === "windows"', self.main_qml)
        self.assertIn("Qt.fontFamilies().indexOf(preferredWindowsUiFont)", self.main_qml)
        self.assertIn("window.preferredWindowsUiFontAvailable", self.main_qml)

    def test_dialogs_share_the_fluent_close_button(self):
        self.assertIn('glyph: "\\uE711"', self.dialog_close_button_qml)
        self.assertIn('Accessible.name: qsTr("关闭")', self.dialog_close_button_qml)
        self.assertNotIn("ToolTip", self.dialog_close_button_qml)
        self.assertIn("renderType: Text.QtRendering", self.icon_glyph_qml)
        self.assertEqual(self.buttons_qml.count("DialogCloseButton {"), 2)
        self.assertEqual(self.voice_qml.count("DialogCloseButton {"), 1)

    def test_tooltips_are_compact_and_follow_each_truncated_text(self):
        self.assertIn("ToolTip {", self.compact_tooltip_qml)
        self.assertIn("delay: 450", self.compact_tooltip_qml)
        self.assertIn("font.pixelSize: root.tokens.fontSizeTiny", self.compact_tooltip_qml)
        self.assertIn(
            "y: -implicitHeight - root.tokens.spacingSmall "
            "- tooltipBackground.border.width",
            self.compact_tooltip_qml,
        )
        for inset in ("leftInset", "rightInset", "topInset", "bottomInset"):
            self.assertIn(f"{inset}: 0", self.compact_tooltip_qml)
        self.assertIn("maximumTextWidth: 260", self.compact_tooltip_qml)
        self.assertEqual(self.buttons_qml.count("CompactToolTip {"), 4)
        for title_id in (
            "primaryGestureTitle",
            "doubleGestureTitle",
            "longGestureTitle",
            "comboGestureTitle",
        ):
            self.assertIn(f"id: {title_id}", self.buttons_qml)
        for title_id in (
            "primaryGestureTitle",
            "doubleGestureTitle",
            "longGestureTitle",
            "comboGestureTitle",
        ):
            title_start = self.buttons_qml.index(f"id: {title_id}")
            tooltip_start = self.buttons_qml.index("CompactToolTip {", title_start)
            title_source = self.buttons_qml[title_start:tooltip_start]
            self.assertIn("Layout.fillHeight: true", title_source)
            self.assertIn("verticalAlignment: Text.AlignVCenter", title_source)
        self.assertNotIn("ToolTip.visible", self.buttons_qml)
        self.assertIn("gestureHover.hovered && !cell.empty", self.mapping_card_qml)
        self.assertIn("cell.usingNote || valueLabel.truncated", self.mapping_card_qml)
        self.assertNotIn("actionHover", self.mapping_card_qml)
        for row_qml in (
            self.inline_settings_row_qml,
            self.settings_list_row_qml,
            self.diagnostic_result_row_qml,
        ):
            self.assertIn("HoverHandler { id: titleHover }", row_qml)
            self.assertGreaterEqual(row_qml.count("CompactToolTip {"), 2)
            self.assertIn("titleHover.hovered && titleLabel.truncated", row_qml)
            self.assertNotIn("ToolTip.visible", row_qml)
        self.assertIn("descriptionHover.hovered && descriptionLabel.truncated", self.inline_settings_row_qml)
        self.assertIn("descriptionHover.hovered && descriptionLabel.truncated", self.settings_list_row_qml)
        self.assertIn("detailHover.hovered && detailLabel.truncated", self.diagnostic_result_row_qml)
        self.assertIn("globalStatusHover.hovered", self.main_qml)
        self.assertIn("globalStatusText.truncated", self.main_qml)

    def test_icon_glyph_has_windows_10_font_fallback(self):
        self.assertIn('Qt.fontFamilies().indexOf("Segoe Fluent Icons")', self.icon_glyph_qml)
        self.assertIn('Qt.fontFamilies().indexOf("Segoe MDL2 Assets")', self.icon_glyph_qml)
        self.assertIn("font.family: iconFontFamily", self.icon_glyph_qml)

    def test_device_page_shows_real_service_state_and_start_progress(self):
        self.assertIn("SettingsController.bridgeLaunchBusy", self.device_qml)
        self.assertIn("SettingsController.bridgeConnected", self.device_qml)
        self.assertIn("SettingsController.bridgeRunning", self.device_qml)
        self.assertIn("SettingsController.bridgeLaunchPhase", self.device_qml)
        self.assertIn('qsTr("启动中…")', self.device_qml)
        self.assertIn("enabled: !SettingsController.bridgeLaunchBusy", self.device_qml)

    def test_voice_page_states_real_windows_boundaries_without_fake_grants(self):
        for object_name in (
            "openMicrophonePrivacyButton",
            "openVoiceProgramSettingsButton",
        ):
            self.assertIn(f'objectName: "{object_name}"', self.voice_qml)
        self.assertNotIn('objectName: "openSoundInputSettingsButton"', self.voice_qml)
        self.assertIn('titleText: qsTr("麦克风权限")', self.voice_qml)
        self.assertNotIn('stateText: qsTr("待确认")', self.voice_qml)
        self.assertIn(
            'qsTr("自动读取并同步搜狗当前的按住说快捷键")',
            self.voice_qml,
        )
        self.assertIn(
            'qsTr("按程序记忆；请在微信输入法设置中保持一致")',
            self.voice_qml,
        )
        self.assertIn('? qsTr("录入中")', self.voice_qml)
        self.assertIn(
            'root.voiceHotkeyBusy ? qsTr("处理中") : qsTr("已保存")',
            self.voice_qml,
        )
        for misleading_claim in (
            "已授权",
            "无线麦需要管理员权限",
            "VB-CABLE 安装成功",
        ):
            self.assertNotIn(misleading_claim, self.voice_qml)
        self.assertIn("由 Windows 管理", self.voice_qml)

    def test_only_device_page_owns_internal_navigation_to_buttons(self):
        self.assertIn("signal openButtonsRequested()", self.device_qml)
        self.assertIn("onOpenButtonsRequested: window.requestPage(1)", self.main_qml)
        self.assertNotIn("openButtonsRequested", self.voice_qml)

    def test_buttons_page_keeps_the_mapping_cards_and_photo_sidebar(self):
        for object_name in ("photoSidebar", "photoFrame", "photoImage"):
            self.assertIn(f'objectName: "{object_name}"', self.buttons_qml)
        self.assertIn("SettingsController.photoSource", self.buttons_qml)
        self.assertIn("SettingsController.photoAvailable", self.buttons_qml)
        self.assertIn('objectName: "photoHotspot_" + buttonId', self.buttons_qml)
        self.assertIn(
            'objectName: "photoHotspotMarker_" + photoHotspot.buttonId',
            self.buttons_qml,
        )
        self.assertIn('objectName: "mappingLines"', self.buttons_qml)
        self.assertIn('objectName: "activeMappingLine"', self.buttons_qml)
        self.assertIn("function connectorControlRadius(startX, endX)", self.buttons_qml)
        self.assertIn("function connectorRoute(card, hotspot, coordinateItem)", self.buttons_qml)
        self.assertEqual(self.buttons_qml.count("ctx.bezierCurveTo("), 1)
        self.assertNotIn("ctx.quadraticCurveTo(", self.buttons_qml)
        self.assertNotIn("ctx.lineTo(endX", self.buttons_qml)
        self.assertIn('ctx.lineCap = "round"', self.buttons_qml)
        self.assertIn("active ? 2.2 : 1.25", self.buttons_qml)
        self.assertIn("hotspotX * photoImage.paintedWidth", self.buttons_qml)
        self.assertIn("hotspotY * photoImage.paintedHeight", self.buttons_qml)
        self.assertIn("visible: photoHotspot.isSelected", self.buttons_qml)
        self.assertIn('objectName: "leftMappingCards"', self.buttons_qml)
        self.assertIn('objectName: "rightMappingCards"', self.buttons_qml)
        self.assertIn("MappingCard {", self.buttons_qml)
        self.assertIn("leftCardRepeater.itemAt(i)", self.buttons_qml)
        self.assertIn("rightCardRepeater.itemAt(i)", self.buttons_qml)
        self.assertIn("photoHotspotRepeater.itemAt(i)", self.buttons_qml)
        self.assertNotIn("function targetY(buttonId)", self.buttons_qml)
        self.assertIn("root.selected ? root.tokens.accentSoft", self.mapping_card_qml)
        self.assertIn("border.width: root.tokens.hairlineWidth", self.mapping_card_qml)
        self.assertIn("root.tokens.cardBorder", self.mapping_card_qml)
        self.assertIn("leftPadding: 4", self.mapping_card_qml)
        self.assertIn("columnSpacing: 0", self.mapping_card_qml)
        self.assertIn("spacing: 0", self.mapping_card_qml)
        self.assertIn("root.tokens.surfaceMuted", self.mapping_card_qml)
        self.assertIn("MappingKeyLabel {", self.mapping_card_qml)
        self.assertIn("fontFamilyMono", self.mapping_card_qml)
        self.assertIn('"Delete": "Del"', self.mapping_card_qml)
        self.assertIn('"Delete（退格）": "Del"', self.mapping_card_qml)
        self.assertIn('noteText.trim() !== qsTr("未命名")', self.mapping_card_qml)
        self.assertIn("VoicePausedCell", self.mapping_card_qml)
        self.assertIn('qsTr("语音模式下暂停")', self.mapping_card_qml)
        for column_name in ("单击", "双击", "长按"):
            self.assertIn(f'qsTr("{column_name}")', self.mapping_card_qml)
        self.assertIn('objectName: exposeObjectNames ? "editMapping_" + cardId', self.mapping_card_qml)

    def test_action_combo_group_headers_do_not_add_model_rows(self):
        self.assertIn(
            "SettingsController.actionOptionGroupTitle(String(modelData))",
            self.buttons_qml,
        )
        self.assertIn(
            "SettingsController.actionOptionStartsGroup(String(modelData))",
            self.buttons_qml,
        )
        self.assertIn("Math.ceil(tokens.fontSizeTiny) + tokens.spacingMedium", self.buttons_qml)
        self.assertIn("topPadding: groupHeaderHeight", self.buttons_qml)
        self.assertIn("bottomPadding: 0", self.buttons_qml)
        self.assertIn("color: tokens.textSecondary", self.buttons_qml)
        self.assertIn("color: tokens.border", self.buttons_qml)
        self.assertIn("height: optionDelegate.groupHeaderHeight", self.buttons_qml)
        self.assertNotIn('ListElement { text: "按键操作"', self.buttons_qml)

    def test_all_four_mapping_inputs_share_the_grouped_action_sources(self):
        self.assertIn("model: SettingsController.primaryActionOptionsFor(", self.buttons_qml)
        self.assertIn("actionEditor.buttonId", self.buttons_qml)
        self.assertGreaterEqual(
            self.buttons_qml.count("model: SettingsController.secondaryActionOptions"),
            3,
        )
        self.assertEqual(self.buttons_qml.count("cardId: buttonId"), 2)
        self.assertIn('objectName: "actionEditorDialog"', self.buttons_qml)
        self.assertIn('readonly property string headerText: buttonName.length > 0', self.buttons_qml)
        self.assertIn('text: actionEditor.headerText', self.buttons_qml)
        self.assertIn('text: shortcutRecorder.headerText', self.buttons_qml)
        self.assertNotIn('text: actionEditor.title', self.buttons_qml)
        self.assertNotIn('text: shortcutRecorder.title', self.buttons_qml)
        self.assertNotIn('text: "×"', self.buttons_qml)
        self.assertNotIn('text: "X"', self.buttons_qml)
        self.assertEqual(self.buttons_qml.count("DialogCloseButton {"), 2)
        self.assertIn("optionDelegate.highlighted", self.buttons_qml)
        self.assertNotIn('objectName: "mappingListFrame"', self.buttons_qml)
        self.assertIn("property int count: 13", self.buttons_qml)
        self.assertNotIn("ListView {", self.buttons_qml)
        self.assertNotIn('objectName: "voiceSettingsPanel"', self.buttons_qml)
        self.assertNotIn('objectName: "voiceProgramDialog"', self.buttons_qml)
        self.assertIn('objectName: "voiceProgramCombo"', self.voice_qml)
        self.assertIn('objectName: "holdVoiceHotkeyField"', self.voice_qml)
        self.assertIn("SettingsController.restoreMappingDefaults()", self.buttons_qml)
        self.assertIn('qsTr("检测真实按键")', self.buttons_qml)
        self.assertIn('text: qsTr("保存映射")', self.buttons_qml)

    def test_buttons_page_switches_views_above_content_and_keeps_actions_below(self):
        switch_index = self.buttons_qml.index('objectName: "mappingViewSwitcher"')
        single_index = self.buttons_qml.index('objectName: "mappingList"')
        combo_index = self.buttons_qml.index('objectName: "comboMappingList"')
        actions_index = self.buttons_qml.index('objectName: "mappingActionsPanel"')
        switcher_source = self.buttons_qml[switch_index:single_index]

        self.assertLess(switch_index, single_index)
        self.assertLess(switch_index, combo_index)
        self.assertLess(single_index, actions_index)
        self.assertLess(combo_index, actions_index)
        self.assertEqual(switcher_source.count("Layout.preferredWidth: 1"), 2)
        self.assertIn('text: qsTr("单键映射")', self.buttons_qml)
        self.assertIn('text: qsTr("组合按键映射")', self.buttons_qml)
        self.assertIn("model: SettingsController.comboRows", self.buttons_qml)
        self.assertIn('objectName: "comboActionEditor_" + buttonId', self.buttons_qml)

    def test_voice_hotkey_field_is_owned_by_the_voice_page(self):
        self.assertIn('placeholderText: qsTr("点击录入")', self.voice_qml)
        self.assertIn(
            'qsTr("自动读取并同步搜狗当前的按住说快捷键")',
            self.voice_qml,
        )
        self.assertIn(
            'qsTr("按程序记忆；请在微信输入法设置中保持一致")',
            self.voice_qml,
        )
        self.assertIn('objectName: "openVoiceProgramSettingsButton"', self.voice_qml)
        self.assertNotIn('objectName: "holdVoiceHotkeyField"', self.buttons_qml)

    def test_voice_program_status_uses_structured_privilege_and_dirty_state(self):
        self.assertIn(
            "SettingsController.voiceProgramElevationStatus",
            self.voice_qml,
        )
        self.assertIn(
            "SettingsController.voiceProgramSettingsDirty",
            self.voice_qml,
        )
        self.assertNotIn("SettingsController.settingsDirty", self.voice_qml)
        self.assertNotIn("voiceProgramStatusText.indexOf", self.voice_qml)

    def test_voice_rows_use_the_shared_fixed_action_column(self):
        self.assertIn(
            "settingsActionColumnWidth: 84",
            self.voice_qml,
        )
        self.assertRegex(
            self.voice_qml,
            r'(?s)objectName: "testVbCableChannelButton".*?'
            r"Layout\.fillWidth: true",
        )

    def test_conflicting_audio_work_temporarily_locks_configuration_writes(self):
        self.assertIn(
            "readonly property bool endpointPreflightBusy:",
            self.voice_qml,
        )
        self.assertIn("readonly property bool configurationWriteBusy:", self.voice_qml)
        for busy_source in (
            "DiagnosticsController.driverActionRunning",
            "DiagnosticsController.vbCableTestRunning",
            "SettingsController.bridgeLaunchBusy",
            "root.endpointPreflightBusy",
        ):
            self.assertIn(busy_source, self.voice_qml)
        self.assertGreaterEqual(
            self.voice_qml.count("!root.configurationWriteBusy"),
            9,
        )
        self.assertIn(
            "&& !SettingsController.endpointPreflightBusy",
            self.device_qml,
        )
        self.assertIn(
            "&& !DiagnosticsController.driverActionRunning",
            self.device_qml,
        )
        for busy_source in (
            "&& !SettingsController.bridgeLaunchBusy",
            "&& !SettingsController.endpointPreflightBusy",
            "&& !DiagnosticsController.driverActionRunning",
            "&& !DiagnosticsController.vbCableTestRunning",
        ):
            self.assertIn(busy_source, self.buttons_qml)


class ThreePageSettingsSourceContractTests(unittest.TestCase):
    def setUp(self):
        qml_dir = Path(qt_settings_app.__file__).resolve().parent / "qml"
        self.main_qml = (qml_dir / "main.qml").read_text(encoding="utf-8")
        self.tokens_qml = (qml_dir / "Tokens.qml").read_text(encoding="utf-8")
        self.device_qml = (qml_dir / "DevicePage.qml").read_text(encoding="utf-8")
        self.voice_qml = (qml_dir / "VoicePage.qml").read_text(encoding="utf-8")
        self.buttons_qml = (qml_dir / "ButtonsPage.qml").read_text(encoding="utf-8")
        self.inline_row_qml = (qml_dir / "InlineSettingsRow.qml").read_text(
            encoding="utf-8"
        )
        self.mapping_card_qml = (qml_dir / "MappingCard.qml").read_text(
            encoding="utf-8"
        )
        self.compact_button_qml = (qml_dir / "CompactButton.qml").read_text(
            encoding="utf-8"
        )
        self.compact_text_field_qml = (qml_dir / "CompactTextField.qml").read_text(
            encoding="utf-8"
        )
        self.compact_switch_qml = (qml_dir / "CompactSwitch.qml").read_text(
            encoding="utf-8"
        )
        self.selection_combo_qml = (qml_dir / "SelectionComboBox.qml").read_text(
            encoding="utf-8"
        )
        self.section_frame_qml = (qml_dir / "SectionFrame.qml").read_text(
            encoding="utf-8"
        )

    def test_navigation_has_only_device_buttons_and_voice(self):
        for object_name, label in (
            ("deviceTabButton", "设备"),
            ("mappingTabButton", "按键"),
            ("voiceTabButton", "语音"),
        ):
            self.assertIn(f'objectName: "{object_name}"', self.main_qml)
            self.assertIn(f'text: qsTr("{label}")', self.main_qml)
        for retired in (
            "connectionTabButton",
            "permissionsTabButton",
            "diagnosticsTabButton",
            "generalTabButton",
            "PermissionsPage",
            "DiagnosticsPage",
            "GeneralPage",
        ):
            self.assertNotIn(retired, self.main_qml)
        self.assertIn("DevicePage {", self.main_qml)
        self.assertIn("ButtonsPage {", self.main_qml)
        self.assertIn("VoicePage {", self.main_qml)

    def test_window_uses_the_inset_rounded_client_shell(self):
        for token_name in (
            "windowFrame",
            "nativeWindowBorder",
            "windowFrameBorder",
            "windowClientRadius",
            "windowFrameGap",
        ):
            self.assertRegex(
                self.tokens_qml,
                rf"property\s+(?:color|int)\s+{token_name}:",
            )
        self.assertIn('objectName: "clientShell"', self.main_qml)
        self.assertIn('objectName: "clientContent"', self.main_qml)
        self.assertIn('objectName: "clientShellOutline"', self.main_qml)
        self.assertIn("anchors.leftMargin: tokens.windowFrameGap", self.main_qml)
        self.assertIn("anchors.rightMargin: tokens.windowFrameGap", self.main_qml)
        self.assertIn("anchors.bottomMargin: tokens.windowFrameGap", self.main_qml)
        self.assertIn("radius: tokens.windowClientRadius", self.main_qml)
        self.assertIn("border.color: tokens.windowFrameBorder", self.main_qml)
        self.assertIn(
            "readonly property color nativeBorderColor: tokens.nativeWindowBorder",
            self.main_qml,
        )
        self.assertIn(
            'property color nativeWindowBorder: darkMode ? "#4a5059" : "#a8adb4"',
            self.tokens_qml,
        )
        self.assertIn(
            'property color windowFrame: darkMode ? "#101318" : "#eef0f2"',
            self.tokens_qml,
        )
        self.assertIn(
            'property color sidebar: darkMode ? "#1b1e24" : "#f5f6f8"',
            self.tokens_qml,
        )
        self.assertIn("property real hairlineWidth: 0.5", self.tokens_qml)
        self.assertIn("property int structuralDividerWidth: 1", self.tokens_qml)
        self.assertNotIn("import QtQuick.Effects", self.main_qml)
        self.assertNotIn("clientShellShadow", self.main_qml)
        self.assertNotIn("windowShadow", self.tokens_qml)

    def test_global_status_bar_uses_background_without_a_top_rule(self):
        status_bar_source = self.main_qml[
            self.main_qml.index('id: globalStatusBar'):
            self.main_qml.index('id: globalStatusText')
        ]
        self.assertIn("tokens.statusBackground", status_bar_source)
        self.assertNotIn("height: tokens.hairlineWidth", status_bar_source)

    def test_regular_frames_share_one_hairline_width(self):
        for source in (
            self.main_qml,
            self.voice_qml,
            self.buttons_qml,
            self.inline_row_qml,
            self.mapping_card_qml,
            self.compact_button_qml,
            self.compact_text_field_qml,
            self.selection_combo_qml,
            self.section_frame_qml,
        ):
            self.assertIn("hairlineWidth", source)
            self.assertNotIn("activeFocus ? 2 : 1", source)
        self.assertNotIn("border.width: 0.75", self.mapping_card_qml)

    def test_device_page_contains_device_and_desktop_behavior_rows(self):
        for object_name in (
            "currentDeviceRow",
            "buttonReceiverRow",
            "remoteServiceRow",
            "runtimeLogRow",
            "launchAtLoginRow",
            "launchBridgeOnAppStartRow",
            "closeBehaviorRow",
        ):
            self.assertIn(f'objectName: "{object_name}"', self.device_qml)
        for button_name in (
            "refreshDeviceChecksButton",
            "openBluetoothSettingsButton",
            "openButtonSettingsButton",
            "startBridgeButton",
            "deviceOpenLogButton",
        ):
            self.assertIn(f'objectName: "{button_name}"', self.device_qml)
        self.assertIn("SettingsController.startBridge()", self.device_qml)
        self.assertNotIn("SettingsController.saveSettings()", self.device_qml)
        self.assertNotIn("stopBridge", self.device_qml)
        self.assertNotIn('text: qsTr("设备")', self.device_qml)
        self.assertIn("SettingsController.remoteDisplayName", self.device_qml)
        self.assertIn('qsTr("启动桥接")', self.device_qml)

    def test_device_toggles_share_the_compact_accent_switch(self):
        self.assertEqual(self.device_qml.count("CompactSwitch {"), 2)
        self.assertNotIn("                    Switch {", self.device_qml)
        self.assertIn("implicitWidth: 28", self.compact_switch_qml)
        self.assertIn("implicitHeight: 14", self.compact_switch_qml)
        self.assertIn("? root.tokens.accent", self.compact_switch_qml)
        self.assertIn("? root.tokens.accentText", self.compact_switch_qml)

    def test_sidebar_separator_uses_a_visible_structural_divider(self):
        navigation_source = self.main_qml[
            self.main_qml.index('id: navigationBar'):
            self.main_qml.index('id: pageStack')
        ]
        self.assertIn("width: tokens.structuralDividerWidth", navigation_source)
        self.assertIn("color: tokens.borderStrong", navigation_source)

    def test_diagnostics_are_reused_inside_their_own_rows(self):
        for check_id in ("os_version", "raw_input", "ble_candidate"):
            self.assertIn(f'"{check_id}"', self.device_qml)
        for check_id in (
            "vb_cable_endpoints",
            "output_endpoint",
        ):
            self.assertIn(f'"{check_id}"', self.voice_qml)
        self.assertIn("DiagnosticsController.refreshDiagnostics()", self.device_qml)

    def test_voice_page_has_three_sections_and_provider_specific_content(self):
        for object_name in (
            "audioPrerequisiteSection",
            "voiceProgramSection",
            "voiceTestSection",
        ):
            self.assertIn(f'objectName: "{object_name}"', self.voice_qml)
        self.assertIn('objectName: "voiceProgramCombo"', self.voice_qml)
        self.assertIn('objectName: "voiceProgramCustomPathRow"', self.voice_qml)
        self.assertIn('objectName: "useWindowsDictationHotkeyButton"', self.voice_qml)
        self.assertIn('objectName: "openVoiceProgramSettingsButton"', self.voice_qml)
        self.assertIn(
            "SettingsController.voiceProgramWindowsDictationSelected",
            self.voice_qml,
        )
        self.assertIn(
            "SettingsController.voiceProgramCustomSelected",
            self.voice_qml,
        )
        self.assertNotIn("selectedVoiceProgramIndex ===", self.voice_qml)
        self.assertIn(
            "SettingsController.refreshVoiceHotkeyFromProvider()", self.voice_qml
        )
        self.assertIn("readonly property bool voiceHotkeyBusy", self.voice_qml)

    def test_voice_audio_rows_auto_save_and_keep_manual_privacy_only(self):
        for object_name in (
            "installVirtualAudioButton",
            "endpointCombo",
            "applyVirtualAudioButton",
            "openMicrophonePrivacyButton",
            "microphonePrivacyRow",
        ):
            self.assertIn(f'objectName: "{object_name}"', self.voice_qml)
        self.assertIn("recommendedIndex: SettingsController.recommendedEndpointIndex", self.voice_qml)
        self.assertIn(
            "SettingsController.selectAndPersistOutputEndpointIndex(index)",
            self.voice_qml,
        )
        self.assertIn(
            "DiagnosticsController.selectDetectedCableInputAsOutput()",
            self.voice_qml,
        )
        self.assertNotIn("SettingsController.saveSettings()", self.voice_qml)
        self.assertNotIn('objectName: "openSoundInputSettingsButton"', self.voice_qml)
        self.assertNotIn('text: qsTr("声音输入")', self.voice_qml)

    def test_voice_tests_use_a_focused_text_box_and_safe_service_recovery(self):
        for object_name in (
            "bridgeTestConfirmDialog",
            "testVbCableChannelButton",
            "recoverBridgeButton",
            "trySpeakingButton",
            "speakTestDialog",
            "speakTestInput",
        ):
            self.assertIn(f'objectName: "{object_name}"', self.voice_qml)
        self.assertIn(
            "DiagnosticsController.testVbCableChannelWithBridgeRestart()",
            self.voice_qml,
        )
        self.assertIn("speakTestInput.forceActiveFocus()", self.voice_qml)
        self.assertIn('objectName: "speakTestInputFrame"', self.voice_qml)
        self.assertIn('objectName: "speakTestCloseButton"', self.voice_qml)
        self.assertIn("Layout.minimumHeight: 150", self.voice_qml)
        self.assertIn("vbCableBridgeRecoveryNeeded", self.voice_qml)

    def test_button_page_keeps_real_cards_and_confirms_built_in_defaults(self):
        self.assertIn("MappingCard {", self.buttons_qml)
        self.assertEqual(self.buttons_qml.count("cardId: buttonId"), 2)
        self.assertIn('objectName: "restoreMappingDefaultsDialog"', self.buttons_qml)
        self.assertIn('text: qsTr("恢复内置默认")', self.buttons_qml)
        self.assertIn("SettingsController.restoreMappingDefaults()", self.buttons_qml)
        detect_index = self.buttons_qml.index('objectName: "detectRealKeyButton"')
        note_index = self.buttons_qml.index('objectName: "voiceGestureRestrictionText"')
        self.assertLess(detect_index, note_index)
        self.assertIn("设为语音动作后，双击和长按不可用", self.buttons_qml)
        self.assertIn('qsTr("语音模式下暂停")', self.mapping_card_qml)

    def test_inline_rows_do_not_restore_the_old_blue_circle_icon(self):
        self.assertNotIn("iconGlyph", self.inline_row_qml)
        self.assertNotIn("radius: 14", self.inline_row_qml)
        self.assertIn("titleText", self.inline_row_qml)
        self.assertIn("descriptionText", self.inline_row_qml)
        self.assertIn("stateText", self.inline_row_qml)

    def test_inline_diagnostic_notes_strip_terminal_sentence_marks(self):
        normalization = 'replace(/[。；;]+$/, "")'
        self.assertIn(normalization, self.device_qml)
        self.assertIn(normalization, self.voice_qml)


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class SelectionComboBoxBehaviorTests(unittest.TestCase):
    def test_recommendation_decorates_only_a_real_recommended_index(self):
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-c", _SELECTION_COMBO_STATE_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"selection combo probe failed: {result.stderr}",
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data["empty"], "")
        self.assertEqual(data["ordinary"], "Speakers")
        self.assertEqual(data["recommended"], "（推荐） CABLE Input")
        self.assertEqual(data["bold_initial"], [True, False])
        self.assertEqual(data["bold_after_move"], [False, True])


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class ApplicationExitIntegrationTests(unittest.TestCase):
    def test_full_exit_closes_the_real_hidden_qml_window(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmpdir:
            env = dict(os.environ)
            env.setdefault("QT_QPA_PLATFORM", "offscreen")
            env["LOCALAPPDATA"] = tmpdir
            result = subprocess.run(
                [sys.executable, "-c", _APPLICATION_EXIT_PROBE_SCRIPT],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )

        self.assertEqual(
            result.returncode,
            0,
            f"application exit probe failed: {result.stderr}",
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data["result"], 0)
        self.assertLess(data["elapsed"], 5.0)


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class OffscreenQmlLoadTests(unittest.TestCase):
    """Loads the REAL qml/main.qml (not a stand-in snippet), in an isolated
    subprocess (see ``_QML_LOAD_PROBE_SCRIPT`` above), and fails the test on
    ANY QML warning - this is the same check this task's own screenshot
    step relies on, so a future change that breaks the singleton wiring (see
    qt_settings_app.py and main.qml's module docstrings for the exact Qt
    Quick Controls internal-property-name collision this works around)
    fails fast in CI instead of only being noticed visually.
    """

    def test_main_qml_loads_with_zero_warnings_and_a_reasonable_window_size(self):
        import json
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", _QML_LOAD_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0, f"QML load probe subprocess failed: {result.stderr}"
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])

        self.assertEqual(data["root_count"], 1, "main.qml failed to instantiate")
        self.assertEqual(
            data["warnings"], [], "main.qml produced QML warnings/errors during load"
        )
        self.assertEqual(data["width"], 720)
        self.assertEqual(data["height"], 560)
        self.assertFalse(data["initial_settings_dirty"])
        self.assertFalse(data["retired_finish_tap_control_exists"])
        self.assertFalse(data["voice_hotkey_recording"])
        self.assertFalse(data["voice_hotkey_busy"])
        self.assertEqual(data["saved_voice_hotkey"], "ctrl+shift+f8")
        self.assertIn("快捷键已保存到无线麦", data["voice_save_status"])
        self.assertFalse(data["voice_feedback_on_device"])
        self.assertTrue(data["voice_feedback_on_voice"])
        self.assertTrue(data["mapping_dirty_on_buttons"])
        self.assertFalse(data["mapping_dirty_on_voice"])

    def test_rc003_only_three_page_shell_is_rendered(self):
        import json
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", _RC003_ONLY_DEVICE_PAGE_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0, f"RC003-only QML probe failed: {result.stderr}"
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(
            data["device_options"],
            [device_catalog.profile_for(device_catalog.RC003_ID).display_name],
        )
        self.assertTrue(data["rc003_visible"])
        self.assertEqual(data["mapping_page_title"], "按键映射")
        self.assertTrue(data["device_rows"])
        self.assertTrue(data["voice_sections"])

    def test_three_page_shell_fits_compact_viewports_without_horizontal_overflow(self):
        import json
        import subprocess

        scenarios = (
            ("Basic", 720, 500),
            ("Basic", 640, 480),
            ("FluentWinUI3", 720, 560),
            ("FluentWinUI3", 720, 500),
            ("FluentWinUI3", 640, 480),
        )
        for style, width, height in scenarios:
            with self.subTest(style=style, width=width, height=height), tempfile.TemporaryDirectory() as tmpdir:
                env = dict(os.environ)
                env.setdefault("QT_QPA_PLATFORM", "offscreen")
                env["LOCALAPPDATA"] = tmpdir
                env["PROBE_STYLE"] = style
                env["PROBE_WIDTH"] = str(width)
                env["PROBE_HEIGHT"] = str(height)
                result = subprocess.run(
                    [sys.executable, "-c", _THREE_PAGE_LAYOUT_PROBE_SCRIPT],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    "three-page layout probe failed at "
                    f"{width}x{height}: {result.stdout}\n{result.stderr}",
                )
                data = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(data["warnings"], [])
                self.assertEqual((data["width"], data["height"]), (width, height))
                self.assertEqual(data["mapping_index_on_press"], 1)

                shell = data["client_shell"]
                self.assertAlmostEqual(shell["x"], 3, delta=0.5)
                self.assertAlmostEqual(shell["y"], 0, delta=0.5)
                self.assertAlmostEqual(shell["right"], width - 3, delta=0.5)
                self.assertAlmostEqual(shell["bottom"], height - 3, delta=0.5)
                outline = data["client_shell_outline"]
                for edge in ("x", "y", "right", "bottom"):
                    self.assertAlmostEqual(outline[edge], shell[edge], delta=0.5)

                descriptions = data["voice_columns"]["descriptions"]
                launch_x = descriptions["voiceProgramLaunchText"]["x"]
                self.assertAlmostEqual(
                    descriptions["soundChannelTestDescription"]["x"],
                    launch_x,
                    delta=0.5,
                )
                self.assertAlmostEqual(
                    descriptions["actualSpeechTestDescription"]["x"],
                    launch_x,
                    delta=0.5,
                )
                editor_columns = data["voice_columns"]["editor_columns"]
                self.assertFalse(editor_columns["voiceProgramSpecificRow"]["visible"])
                self.assertFalse(editor_columns["soundChannelTestRow"]["visible"])
                self.assertFalse(editor_columns["actualSpeechTestRow"]["visible"])
                self.assertTrue(data["voice_recovery_editor"]["column"]["visible"])
                self.assertTrue(data["voice_recovery_editor"]["button"]["visible"])

                nav = data["navigation_backgrounds"]
                for button_name in (
                    "deviceTabButton",
                    "mappingTabButton",
                    "voiceTabButton",
                ):
                    background = nav[button_name + "_background"]
                    button = nav[button_name]
                    for edge in ("x", "y", "right", "bottom"):
                        self.assertAlmostEqual(
                            background[edge], button[edge], delta=0.5
                        )

                speak = data["speak_dialog"]
                self.assertGreaterEqual(speak["input_frame"]["height"], 150)
                self.assertGreaterEqual(speak["input"]["height"], 150)
                self.assertEqual(speak["close"]["width"], 28)
                self.assertEqual(speak["close"]["height"], 28)
                self.assertGreaterEqual(
                    speak["input_frame"]["y"], speak["dialog"]["y"]
                )
                self.assertLessEqual(
                    speak["input_frame"]["bottom"], speak["dialog"]["bottom"] + 1
                )
                for page_name, page in data["pages"].items():
                    content = page["content"]
                    self.assertTrue(content["visible"], page_name)
                    self.assertGreater(content["width"], 0, page_name)
                    self.assertGreaterEqual(content["x"], -1, page_name)
                    self.assertLessEqual(content["right"], width + 1, page_name)
                    for item_name, item in page["items"].items():
                        self.assertTrue(item["visible"], item_name)
                        self.assertGreater(item["width"], 0, item_name)
                        self.assertGreaterEqual(item["x"], -1, item_name)
                        self.assertLessEqual(item["right"], width + 1, item_name)

                device_items = data["pages"]["device"]["items"]
                self.assertGreaterEqual(
                    device_items["devicePrerequisiteSectionTitle"]["y"],
                    device_items["devicePrerequisiteSection"]["y"],
                )
                self.assertLessEqual(
                    device_items["devicePrerequisiteSectionTitle"]["bottom"],
                    device_items["currentDeviceRow"]["y"] + 1,
                )
                for first, second in (
                    ("currentDeviceRow", "buttonReceiverRow"),
                    ("buttonReceiverRow", "remoteServiceRow"),
                    ("remoteServiceRow", "runtimeLogRow"),
                ):
                    self.assertLessEqual(
                        device_items[first]["bottom"], device_items[second]["y"] + 1
                    )
                self.assertLessEqual(
                    device_items["runtimeLogRow"]["bottom"],
                    device_items["desktopBehaviorSection"]["y"] + 1,
                )
                self.assertGreaterEqual(
                    device_items["desktopBehaviorSectionTitle"]["y"],
                    device_items["desktopBehaviorSection"]["y"],
                )
                for first, second in (
                    ("desktopBehaviorSectionTitle", "launchAtLoginRow"),
                    ("launchAtLoginRow", "launchBridgeOnAppStartRow"),
                    ("launchBridgeOnAppStartRow", "closeBehaviorRow"),
                ):
                    self.assertLessEqual(
                        device_items[first]["bottom"],
                        device_items[second]["y"] + 1,
                    )
                self.assertLessEqual(
                    device_items["closeBehaviorRow"]["bottom"],
                    device_items["desktopBehaviorSection"]["bottom"] + 1,
                )

                voice_items = data["pages"]["voice"]["items"]
                if style == "FluentWinUI3" and (width, height) == (720, 560):
                    self.assertLessEqual(
                        voice_items["voiceTestSection"]["bottom"] + 6,
                        data["global_status_bar"]["y"],
                    )
                self.assertLessEqual(
                    voice_items["audioPrerequisiteSection"]["bottom"],
                    voice_items["voiceProgramSection"]["y"] + 1,
                )
                self.assertLessEqual(
                    voice_items["voiceProgramSection"]["bottom"],
                    voice_items["voiceTestSection"]["y"] + 1,
                )
                self.assertGreaterEqual(
                    voice_items["voiceProgramCustomPathRow"]["y"],
                    voice_items["voiceProgramSection"]["y"],
                )
                self.assertLessEqual(
                    voice_items["voiceProgramCustomPathRow"]["bottom"],
                    voice_items["voiceProgramSection"]["bottom"] + 1,
                )
                for section_name, title_name in (
                    ("audioPrerequisiteSection", "audioPrerequisiteSectionTitle"),
                    ("voiceProgramSection", "voiceProgramSectionTitle"),
                    ("voiceTestSection", "voiceTestSectionTitle"),
                ):
                    self.assertGreaterEqual(
                        voice_items[title_name]["y"],
                        voice_items[section_name]["y"],
                    )
                    self.assertLessEqual(
                        voice_items[title_name]["bottom"],
                        voice_items[section_name]["bottom"] + 1,
                    )

                voice_columns = data["voice_columns"]
                action_columns = list(voice_columns["actions"].values())
                privacy_state = voice_columns["states"]["microphonePrivacyRow"]
                state_columns = [
                    column
                    for name, column in voice_columns["states"].items()
                    if name != "microphonePrivacyRow"
                ]
                reference_action = action_columns[0]
                reference_state = state_columns[0]
                self.assertFalse(privacy_state["visible"])
                for column in action_columns:
                    self.assertTrue(column["visible"])
                    self.assertAlmostEqual(column["x"], reference_action["x"], delta=1)
                    self.assertAlmostEqual(column["width"], 84, delta=1)
                for column in state_columns:
                    self.assertTrue(column["visible"])
                    self.assertAlmostEqual(column["x"], reference_state["x"], delta=1)
                    self.assertAlmostEqual(column["width"], 54, delta=1)
                    self.assertLessEqual(column["right"], reference_action["x"] + 1)

                for control in (
                    *voice_columns["right_controls"].values(),
                    *data["voice_windows_actions"].values(),
                ):
                    self.assertTrue(control["visible"])
                    self.assertAlmostEqual(control["x"], reference_action["x"], delta=1)
                    self.assertAlmostEqual(control["width"], 84, delta=1)
                    self.assertLessEqual(control["right"], width + 1)

                install_button = voice_columns["left_controls"][
                    "installVirtualAudioButton"
                ]
                self.assertTrue(install_button["visible"])
                self.assertAlmostEqual(
                    install_button["x"],
                    voice_columns["editors"]["endpointCombo"]["x"],
                    delta=1,
                )
                self.assertLessEqual(
                    install_button["right"], reference_state["x"] + 1
                )

                editor_map = voice_columns["editors"]
                editors = list(editor_map.values())
                for editor in editors:
                    self.assertTrue(editor["visible"])
                    self.assertAlmostEqual(editor["x"], editors[0]["x"], delta=1)
                    self.assertGreaterEqual(editor["width"], 130)
                    self.assertLessEqual(editor["right"], reference_state["x"] + 1)
                self.assertAlmostEqual(
                    editor_map["voiceProgramCombo"]["width"],
                    editor_map["endpointCombo"]["width"],
                    delta=1,
                )
                self.assertAlmostEqual(
                    editor_map["holdVoiceHotkeyField"]["width"],
                    editor_map["endpointCombo"]["width"] / 2,
                    delta=1,
                )

    def _legacy_settings_shell_fits_supported_logical_viewports_without_horizontal_overflow(self):
        import json
        import subprocess

        # The compact default plus logical sizes corresponding to the
        # supported physical viewports and small high-DPI fallbacks.
        scenarios = (
            ("Basic", 840, 720),
            ("Basic", 1024, 720),
            ("Basic", 1093, 614),
            ("Basic", 1280, 720),
            ("Basic", 683, 480),
            ("Basic", 640, 480),
            ("FluentWinUI3", 840, 720),
            ("FluentWinUI3", 720, 464),
            ("FluentWinUI3", 683, 480),
            ("FluentWinUI3", 640, 480),
            ("FluentWinUI3", 640, 440),
        )

        for style, width, height in scenarios:
            with self.subTest(style=style, width=width, height=height), tempfile.TemporaryDirectory() as tmpdir:
                env = dict(os.environ)
                env.setdefault("QT_QPA_PLATFORM", "offscreen")
                env["LOCALAPPDATA"] = tmpdir
                env["PROBE_STYLE"] = style
                env["PROBE_WIDTH"] = str(width)
                env["PROBE_HEIGHT"] = str(height)
                result = subprocess.run(
                    [sys.executable, "-c", _SETTINGS_SHELL_LAYOUT_PROBE_SCRIPT],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    "settings layout probe failed at "
                    f"{width}x{height}: {result.stdout}\n{result.stderr}",
                )
                data = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(data["warnings"], [])
                self.assertEqual((data["width"], data["height"]), (width, height))
                self.assertTrue(data["initial_status_visible"])
                self.assertFalse(data["initial_status_has_status"])
                self.assertFalse(data["initial_status_text_visible"])
                self.assertEqual(
                    data["connection"]["explicit_launch_status"],
                    "AUDIT_LAUNCH_RESULT_MUST_BE_VISIBLE",
                )
                self.assertEqual(
                    data["diagnostics"]["detail_before_refresh"],
                    "AUDIT_DETAIL_FIRST",
                )
                self.assertEqual(
                    data["diagnostics"]["detail_after_refresh"],
                    "AUDIT_DETAIL_AFTER_REFRESH",
                )

                for group_name in ("navigation",):
                    for item_name, item in data[group_name].items():
                        self.assertGreater(item["width"], 0, item_name)
                        self.assertGreaterEqual(item["x"], -1, item_name)
                        self.assertLessEqual(item["right"], width + 1, item_name)

                for page_name in ("connection", "permissions", "diagnostics"):
                    page = data[page_name]
                    self.assertLessEqual(
                        page["content_width"], page["available_width"] + 1
                    )
                    for item_name, item in page["items"].items():
                        if not item["visible"]:
                            continue
                        self.assertGreater(item["width"], 0, item_name)
                        self.assertGreaterEqual(item["x"], -1, item_name)
                        self.assertLessEqual(item["right"], width + 1, item_name)

                diagnostics_items = data["diagnostics"]["items"]
                self.assertLessEqual(
                    data["diagnostics"]["content_height"],
                    data["diagnostics"]["available_height"] + 1,
                )
                for first_name, second_name, section_name in (
                    (
                        "selectCableInputButton",
                        "launchDriverSetupButton",
                        "optionalDriverSection",
                    ),
                    (
                        "openSpeechSettingsButton",
                        "diagnosticsOpenLogButton",
                        "diagnosticsFooterSection",
                    ),
                ):
                    first = diagnostics_items[first_name]
                    second = diagnostics_items[second_name]
                    section = diagnostics_items[section_name]
                    self.assertAlmostEqual(first["width"], second["width"], delta=1)
                    self.assertAlmostEqual(first["height"], second["height"], delta=1)
                    self.assertAlmostEqual(first["y"], second["y"], delta=1)
                    self.assertLess(first["right"], second["x"])
                    self.assertGreaterEqual(first["x"], section["x"])
                    self.assertLessEqual(second["right"], section["right"])

                for description_name, action_name, section_name in (
                    (
                        "optionalDriverDescription",
                        "selectCableInputButton",
                        "optionalDriverSection",
                    ),
                    (
                        "vbCableChannelDescription",
                        "testVbCableChannelButton",
                        "vbCableChannelTestSection",
                    ),
                    (
                        "diagnosticsFooterDescription",
                        "openSpeechSettingsButton",
                        "diagnosticsFooterSection",
                    ),
                ):
                    description = diagnostics_items[description_name]
                    action = diagnostics_items[action_name]
                    section = diagnostics_items[section_name]
                    self.assertGreaterEqual(description["y"], section["y"])
                    self.assertLessEqual(description["bottom"], section["bottom"] - 1)
                    self.assertLess(description["right"], action["x"])

                mapping_items = data["mapping"]["items"]
                for item_name, item in mapping_items.items():
                    if not item["visible"]:
                        continue
                    self.assertGreater(item["width"], 0, item_name)
                    self.assertGreaterEqual(item["x"], -1, item_name)
                    self.assertLessEqual(item["right"], width + 1, item_name)
                self.assertLess(
                    mapping_items["mappingActionsPanel"]["y"],
                    mapping_items["mappingList"]["y"],
                )
                self.assertLess(
                    mapping_items["mappingList"]["y"],
                    mapping_items["mappingListFrame"]["y"],
                )
                for canvas_name in ("mappingLines", "activeMappingLine"):
                    canvas = mapping_items[canvas_name]
                    board = mapping_items["mappingList"]
                    self.assertAlmostEqual(canvas["x"], board["x"], delta=1)
                    self.assertAlmostEqual(canvas["y"], board["y"], delta=1)
                    self.assertAlmostEqual(canvas["width"], board["width"], delta=1)
                    self.assertAlmostEqual(canvas["height"], board["height"], delta=1)
                self.assertLessEqual(
                    mapping_items["leftMappingCards"]["right"],
                    mapping_items["photoSidebar"]["x"],
                )
                self.assertLessEqual(
                    mapping_items["photoSidebar"]["right"],
                    mapping_items["rightMappingCards"]["x"],
                )
                self.assertAlmostEqual(
                    mapping_items["restoreMappingDefaultsButton"]["width"],
                    mapping_items["saveMappingButton"]["width"],
                    delta=1,
                )

                connection_items = data["connection"]["items"]
                if width == 840:
                    for combo_name in ("deviceCombo", "endpointCombo"):
                        combo = connection_items[combo_name]
                        section = connection_items["rc003OutputSection"]
                        self.assertGreaterEqual(
                            combo["width"], section["width"] - 100
                        )
                        self.assertLessEqual(combo["x"] - section["x"], 90)
                        self.assertLessEqual(
                            section["right"] - combo["right"], 50
                        )
                self.assertTrue(connection_items["bridgeNotRunningWarning"]["visible"])
                self.assertIn("语音键", data["connection"]["bridge_warning"])
                self.assertNotIn(
                    "未运行",
                    data["connection"]["warning_after_external_start"],
                )
                self.assertIn(
                    "未运行",
                    data["connection"]["warning_after_external_exit"],
                )
                self.assertLessEqual(
                    connection_items["deviceSection"]["right"],
                    connection_items["rc003OutputSection"]["x"],
                )
                self.assertAlmostEqual(
                    connection_items["deviceSection"]["y"],
                    connection_items["rc003OutputSection"]["y"],
                    delta=1,
                )
                self.assertLess(
                    connection_items["deviceCombo"]["y"],
                    connection_items["endpointCombo"]["y"],
                )
                self.assertLess(
                    connection_items["endpointCombo"]["y"],
                    connection_items["bridgeSection"]["y"],
                )
                self.assertLess(
                    connection_items["bridgeSection"]["y"],
                    connection_items["connectionActionRow"]["y"],
                )
                self.assertFalse(data["connection"]["save_highlighted"])
                self.assertTrue(data["connection"]["launch_highlighted"])
                self.assertNotIn("小米遥控器2 Pro 已连接", data["connection"]["launch_status"])
                waiting_progress = data["connection"]["waiting_progress"]
                self.assertTrue(waiting_progress["visible"])
                self.assertTrue(waiting_progress["indicator_running"])
                self.assertIn("等待小米遥控器2 Pro 连接", waiting_progress["stage_text"])
                self.assertTrue(waiting_progress["elapsed_visible"])
                self.assertEqual(waiting_progress["elapsed_text"], "12 秒")
                connected_progress = data["connection"]["connected_progress"]
                self.assertTrue(connected_progress["visible"])
                self.assertFalse(connected_progress["indicator_running"])
                self.assertIn("小米遥控器2 Pro 已连接", connected_progress["stage_text"])

                permission_items = data["permissions"]["items"]
                self.assertTrue(permission_items["requiredPermissionsSection"]["visible"])
                self.assertTrue(data["neutral_status"]["visible"])
                self.assertEqual(data["neutral_status"]["text"], "neutral status")
                self.assertTrue(data["error_status"]["visible"])
                self.assertEqual(data["error_status"]["text"], "priority error")

    def _legacy_tab_focus_scrolls_connection_and_permissions_commands_into_view(self):
        import json
        import subprocess

        with tempfile.TemporaryDirectory() as tmpdir:
            env = dict(os.environ)
            env.setdefault("QT_QPA_PLATFORM", "offscreen")
            env["LOCALAPPDATA"] = tmpdir
            result = subprocess.run(
                [sys.executable, "-c", _TAB_FOCUS_SCROLL_PROBE_SCRIPT],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(
            result.returncode,
            0,
            f"Tab focus probe failed: {result.stdout}\n{result.stderr}",
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data["warnings"], [])
        for page_name in ("connection", "permissions"):
            page = data[page_name]
            self.assertTrue(page["reached"], page_name)
            self.assertTrue(page["escaped"], page_name)
            self.assertGreaterEqual(page["content_y"], 0, page_name)
            self.assertTrue(page["target_visible"], page_name)


class QmlLoadProbeCallsProductionShutdownHelperTests(unittest.TestCase):
    """XRBM-035: a source-level, PySide6-independent regression guard
    (never touches Qt, matching ``DiagnosticsShutdownOrderingTests``/
    ``OffscreenQmlLoadTests`` above) that ``_QML_LOAD_PROBE_SCRIPT`` calls
    the SAME production shutdown helper ``run_settings_window()`` calls
    right after ``app.exec()`` returns - see that function and
    ``_shutdown_diagnostics_workers()``'s own docstrings. Without this, a
    future edit could silently start relying on this module's ``atexit``
    hook alone again (the exact contract a real Windows CI crash already
    disproved), or start mocking/skipping the real BLE discovery this probe
    deliberately still exercises - defeating the whole point of this being
    a REAL fast-close race reproduction rather than a stand-in for one.
    """

    def test_probe_script_calls_the_production_shutdown_helper_before_printing_result(self):
        self.assertIn("m._shutdown_diagnostics_workers()", _QML_LOAD_PROBE_SCRIPT)
        shutdown_index = _QML_LOAD_PROBE_SCRIPT.index("m._shutdown_diagnostics_workers()")
        result_index = _QML_LOAD_PROBE_SCRIPT.index("result = {")
        print_index = _QML_LOAD_PROBE_SCRIPT.index("print(json.dumps(result))")

        self.assertLess(
            result_index,
            shutdown_index,
            "the probe must build its `result` dict from the real QML load "
            "BEFORE requesting diagnostics-worker shutdown",
        )
        self.assertLess(
            shutdown_index,
            print_index,
            "the production shutdown helper must run before the probe prints "
            "its result and exits, mirroring run_settings_window() calling "
            "it before returning",
        )

    def test_probe_script_never_mocks_or_skips_the_real_diagnostics_controller(self):
        # This probe's entire value is exercising the REAL background BLE
        # discovery a real DiagnosticsController.__init__() starts - a
        # future "just make CI green" edit replacing it with a fake/no-op
        # would silently stop testing the actual crash this task fixed.
        self.assertIn("DiagnosticsController(controller,", _QML_LOAD_PROBE_SCRIPT)
        self.assertNotIn("mock", _QML_LOAD_PROBE_SCRIPT.lower())


# Real mouse clicks and real key events via QTest, delivered through the
# ACTUAL QQmlApplicationEngine-loaded main.qml (not a Python-level call to
# setActionTextAt()) - types a custom chord into the "mic" row's visible
# ComboBox and clicks "保存映射" WITHOUT ever pressing Enter. Run in an
# isolated subprocess (see OffscreenQmlLoadTests/RenderedContrastTests
# above for the two separate same-process-multi-engine QQC2 limitations
# this sidesteps); the config file it persists to (LOCALAPPDATA, passed
# via env by the outer test) is read back by the OUTER test afterward,
# since that is real state on disk that survives the subprocess exiting.
_DIRECT_SAVE_PROBE_SCRIPT = r"""
import hashlib
import json
import os
import sys

from ovb_rc003 import qt_settings_app as m
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest


def _find_child_by_object_name(root, name):
    children = list(root.children())
    child_items = getattr(root, "childItems", None)
    if callable(child_items):
        children.extend(child for child in child_items() if child not in children)
    for child in children:
        if child.objectName() == name:
            return child
        found = _find_child_by_object_name(child, name)
        if found is not None:
            return found
    return None


def _select_combo_option(window, app, combo, option_index):
    indicator_point = combo.mapToScene(
        QPointF(combo.property("width") - 8, combo.property("height") / 2)
    ).toPoint()
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, indicator_point)
    app.processEvents()
    assert combo.property("down"), "ComboBox popup did not open"
    QTest.keyClick(window, Qt.Key_Home)
    for _ in range(option_index):
        QTest.keyClick(window, Qt.Key_Down)
    QTest.keyClick(window, Qt.Key_Return)
    for _ in range(3):
        window.grabWindow()
        app.processEvents()


def _render(window, app, passes=10):
    image = None
    for _ in range(passes):
        image = window.grabWindow()
        app.processEvents()
    return image


def _geometry(item):
    point = item.mapToScene(QPointF(0.0, 0.0))
    return {
        "x": float(point.x()),
        "y": float(point.y()),
        "width": float(item.property("width")),
        "height": float(item.property("height")),
        "visible": bool(item.property("visible")),
    }


def _png_digest(image):
    encoded = QByteArray()
    buffer = QBuffer(encoded)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return hashlib.sha256(bytes(encoded)).hexdigest()


def _mapping_snapshot(window, app, model, controller, screenshot_env):
    QTest.mouseMove(window, QPoint(2, 2))
    app.processEvents()
    image = _render(window, app)
    mapping = _find_child_by_object_name(window, "mappingList")
    assert mapping is not None
    origin = mapping.mapToScene(QPointF(0.0, 0.0))
    cropped = image.copy(
        int(round(origin.x())),
        int(round(origin.y())),
        int(round(mapping.property("width"))),
        int(round(mapping.property("height"))),
    )
    screenshot_path = os.environ.get(screenshot_env, "")
    if screenshot_path:
        assert cropped.save(screenshot_path)
    button_ids = (
        "power", "up", "left", "back", "home", "menu",
        "mic", "right", "ok", "down", "volume_up", "volume_down", "tv",
    )
    return {
        "mapping": _geometry(mapping),
        "cards": {
            button_id: _geometry(
                _find_child_by_object_name(window, "editMapping_" + button_id)
            )
            for button_id in button_ids
        },
        "hotspots": {
            button_id: _geometry(
                _find_child_by_object_name(window, "photoHotspot_" + button_id)
            )
            for button_id in button_ids
        },
        "canvases": {
            name: _geometry(_find_child_by_object_name(window, name))
            for name in ("mappingLines", "activeMappingLine")
        },
        "photo_frame": _geometry(
            _find_child_by_object_name(window, "photoFrame")
        ),
        "selected_button_id": controller.property("selectedButtonId"),
        "display_map": model.to_display_map(),
        "secondary_display_map": model.to_secondary_display_map(),
        "display_note_map": model.to_display_note_map(),
        "pixel_sha256": _png_digest(cropped),
    }


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle("Basic")
app = QGuiApplication.instance() or QGuiApplication([])
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
assert len(engine.rootObjects()) == 1, "main.qml failed to load"
window = engine.rootObjects()[0]
# Real Qt Quick Controls delegates (ListView rows in particular) only get
# their real size/instantiate their children once a real layout+render
# pass has actually run - under the offscreen platform, plain
# processEvents() calls alone are not sufficient; grabWindow() (which
# forces a real frame to be produced) interleaved with processEvents()
# reliably settles it (confirmed empirically while writing this test - a
# mapping row's ComboBox does not exist at all, and its ListView reports
# height 0, without this).
window.show()
for _ in range(10):
    window.grabWindow()
    app.processEvents()

# Switch to the "按键" tab (index 1) - the ComboBox under test only exists
# once ButtonsPage is the active StackLayout page.
tab_bar = _find_child_by_object_name(window, "tabBar")
assert tab_bar is not None
tab_bar.setProperty("currentIndex", 1)
for _ in range(10):
    window.grabWindow()
    app.processEvents()

mapping_list = _find_child_by_object_name(window, "mappingList")
assert mapping_list is not None
assert mapping_list.property("count") == 13
assert _find_child_by_object_name(window, "toggleVoiceModeButton") is None
assert _find_child_by_object_name(window, "holdVoiceModeButton") is None
assert _find_child_by_object_name(window, "toggleVoiceHotkeyField") is None

edit_button = _find_child_by_object_name(window, "editMapping_mic")
assert edit_button is not None, "mic row's edit button not found - is it in view?"
edit_center = edit_button.mapToScene(
    QPointF(edit_button.property("width") / 2, edit_button.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, edit_center)
for _ in range(5):
    window.grabWindow()
    app.processEvents()

editor = _find_child_by_object_name(window, "actionEditorDialog")
combo = _find_child_by_object_name(window, "actionEditorPrimaryCombo")
double_combo = _find_child_by_object_name(window, "actionEditorDoubleCombo")
long_combo = _find_child_by_object_name(window, "actionEditorLongCombo")
assert editor is not None and editor.property("visible")
assert combo is not None and double_combo is not None and long_combo is not None
assert combo.property("visible")
assert double_combo.property("visible") and long_combo.property("visible")
assert int(combo.property("count")) == len(controller.primaryActionOptionsFor("mic"))
assert int(double_combo.property("count")) == len(controller.secondaryActionOptions)
assert int(long_combo.property("count")) == len(controller.secondaryActionOptions)
assert not double_combo.property("enabled") and not long_combo.property("enabled")
assert combo.property("selectTextByMouse")
assert double_combo.property("selectTextByMouse")
assert long_combo.property("selectTextByMouse")
assert combo.property("contentItem").property("selectByMouse")
assert double_combo.property("contentItem").property("selectByMouse")
assert long_combo.property("contentItem").property("selectByMouse")

current_index = int(combo.property("currentIndex"))
indicator_point = combo.mapToScene(
    QPointF(combo.property("width") - 8, combo.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, indicator_point)
for _ in range(5):
    window.grabWindow()
    app.processEvents()
current_option = _find_child_by_object_name(
    window,
    "actionEditorPrimaryCombo_option_" + str(current_index),
)
assert current_option is not None, "current editor option was not instantiated"
current_option_label = current_option.property("contentItem")
current_option_state = {
    "highlighted": bool(current_option.property("highlighted")),
    "font_weight": int(current_option_label.property("font").weight()),
}
assert current_option_state["highlighted"]
assert current_option_state["font_weight"] >= 600
grouped_option = _find_child_by_object_name(
    window,
    "actionEditorPrimaryCombo_option_1",
)
assert grouped_option is not None
assert int(grouped_option.property("groupHeaderHeight")) > 0
before_header_click = (
    int(combo.property("currentIndex")),
    str(combo.property("editText")),
)
header_point = grouped_option.mapToScene(
    QPointF(
        grouped_option.property("width") / 2,
        grouped_option.property("groupHeaderHeight") / 2,
    )
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, header_point)
for _ in range(3):
    window.grabWindow()
    app.processEvents()
assert combo.property("down"), "group header click unexpectedly closed the popup"
assert (
    int(combo.property("currentIndex")),
    str(combo.property("editText")),
) == before_header_click
body_point = grouped_option.mapToScene(
    QPointF(
        grouped_option.property("width") / 2,
        grouped_option.property("height") - 4,
    )
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, body_point)
for _ in range(3):
    window.grabWindow()
    app.processEvents()
assert not combo.property("down")
assert combo.property("editText") == "Escape"
assert editor.property("primaryText") == "Escape"

# Choose real preset rows through each visible ComboBox popup. The editable
# field and backing model must change as soon as the popup activates the row;
# clicking the dialog's "完成" button is deliberately deferred until after
# every assertion below.
assert model.to_display_map()["mic"] == "按住说话"
assert double_combo.property("visible") and long_combo.property("visible")

_select_combo_option(window, app, double_combo, 10)
assert double_combo.property("editText") == "f5"
double_indicator = double_combo.mapToScene(
    QPointF(double_combo.property("width") - 8, double_combo.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, double_indicator)
app.processEvents()
assert double_combo.property("down")
QTest.keyClick(window, Qt.Key_Down)
QTest.keyClick(window, Qt.Key_Return)
for _ in range(3):
    window.grabWindow()
    app.processEvents()
assert double_combo.property("editText") == "元素导航开关"

_select_combo_option(window, app, double_combo, 1)
assert double_combo.property("editText") == "Escape"
assert editor.property("doubleText") == "Escape"
assert model.to_secondary_display_map()["mic"]["double_click"] == ""

_select_combo_option(window, app, long_combo, 2)
assert long_combo.property("editText") == "回车"
assert editor.property("longText") == "回车"
assert model.to_secondary_display_map()["mic"]["long_press"] == ""

# Real mouse click into the ComboBox's editable text area -
# forceActiveFocus() on the ComboBox item alone is NOT equivalent (proven
# while writing this test: it does not hand keyboard focus to the internal
# editable TextInput the way a real click does).
center = combo.mapToScene(
    QPointF(combo.property("width") / 2, combo.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, center)
app.processEvents()
assert combo.property("activeFocus"), "click did not focus the ComboBox"

# Select all existing text, then really TYPE the replacement - one real
# QKeyEvent per character, through the window, exactly as a user's
# keystrokes would arrive. Lowercase ASCII ordinals (not the Qt.Key_X enum,
# whose values are the UPPERCASE-letter ASCII codes) so the synthesized
# text matches real lowercase typing exactly.
QTest.keySequence(window, QKeySequence.SelectAll)
app.processEvents()
typed = "ctrl+shift+p"
for ch in typed:
    QTest.keyClick(window, Qt.Key_Plus if ch == "+" else ord(ch))
    app.processEvents()

assert combo.property("editText") == typed
assert editor.property("primaryText") == typed
assert model.to_display_map()["mic"] == "按住说话"
for _ in range(3):
    window.grabWindow()
    app.processEvents()
assert double_combo.property("visible") and long_combo.property("visible")
assert double_combo.property("enabled") and long_combo.property("enabled")

editor_save_button = _find_child_by_object_name(window, "actionEditorSaveButton")
assert editor_save_button is not None
done_center = editor_save_button.mapToScene(
    QPointF(
        editor_save_button.property("width") / 2,
        editor_save_button.property("height") / 2,
    )
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, done_center)
for _ in range(3):
    window.grabWindow()
    app.processEvents()
assert not editor.property("visible")
assert model.to_display_map()["mic"] == typed
assert model.to_secondary_display_map()["mic"]["double_click"] == "Escape"
assert model.to_secondary_display_map()["mic"]["long_press"] == "回车"
assert controller.settingsDirty

before_save = _mapping_snapshot(
    window,
    app,
    model,
    controller,
    "RC003_MAPPING_BEFORE_SCREENSHOT",
)

# Real click on "保存映射" - deliberately never press Enter/Return anywhere
# in this test.
save_button = _find_child_by_object_name(window, "saveMappingButton")
assert save_button is not None
save_center = save_button.mapToScene(
    QPointF(save_button.property("width") / 2, save_button.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, save_center)
after_save = _mapping_snapshot(
    window,
    app,
    model,
    controller,
    "RC003_MAPPING_AFTER_SCREENSHOT",
)

assert controller.errorMessage == "", f"save reported a validation error: {controller.errorMessage}"

# Switch to the remote-combination view and exercise its real editable field
# through the same direct-save path. This catches a QML-only regression where
# the visible text changes but SettingsController never receives it.
combo_view_button = _find_child_by_object_name(window, "comboMappingViewButton")
assert combo_view_button is not None
combo_view_center = combo_view_button.mapToScene(
    QPointF(
        combo_view_button.property("width") / 2,
        combo_view_button.property("height") / 2,
    )
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, combo_view_center)
for _ in range(5):
    window.grabWindow()
    app.processEvents()

combo_editor = _find_child_by_object_name(window, "comboActionEditor_up")
assert combo_editor is not None and combo_editor.property("visible")
combo_editor_center = combo_editor.mapToScene(
    QPointF(
        combo_editor.property("width") / 2,
        combo_editor.property("height") / 2,
    )
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, combo_editor_center)
app.processEvents()
assert combo_editor.property("activeFocus")
QTest.keySequence(window, QKeySequence.SelectAll)
app.processEvents()
combo_typed = "ctrl+alt+p"
for ch in combo_typed:
    QTest.keyClick(window, Qt.Key_Plus if ch == "+" else ord(ch))
    app.processEvents()
assert combo_editor.property("editText") == combo_typed

save_center = save_button.mapToScene(
    QPointF(save_button.property("width") / 2, save_button.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, save_center)
for _ in range(3):
    window.grabWindow()
    app.processEvents()
assert controller.errorMessage == "", f"combo save reported a validation error: {controller.errorMessage}"

print(json.dumps({
    "before": before_save,
    "after": after_save,
    "current_option": current_option_state,
    "combo_typed": combo_typed,
}))
"""


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class ButtonsPageDirectSaveIntegrationTests(unittest.TestCase):
    """XRBM-030 RETRY 1 blocker 1: a REAL Qt/QML interaction test (see
    ``_DIRECT_SAVE_PROBE_SCRIPT`` above) proving a user can type a custom
    chord through the microphone row's matrix editor and click "保存映射"
    WITHOUT ever pressing Enter. It also locks the current UI contract: both
    single host-shortcut field exists, the old lifecycle controls do not, and
    the editor exposes primary/double/long controls.
    """

    def test_typed_chord_survives_a_direct_save_click_with_no_enter_pressed(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmpdir:
            env = dict(os.environ)
            env.setdefault("QT_QPA_PLATFORM", "offscreen")
            env["LOCALAPPDATA"] = tmpdir
            result = subprocess.run(
                [sys.executable, "-c", _DIRECT_SAVE_PROBE_SCRIPT],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"direct-save probe subprocess failed: {result.stdout}\n{result.stderr}",
            )
            visual = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(visual["before"], visual["after"])
            self.assertTrue(visual["current_option"]["highlighted"])
            self.assertGreaterEqual(visual["current_option"]["font_weight"], 600)

            # The real, persisted file - read back from the SAME
            # LOCALAPPDATA the subprocess wrote to, after it has exited.
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmpdir}):
                bindings = config.load_key_bindings(
                    config.key_bindings_path(config.config_root())
                )
            action = key_mapping.ButtonAction.from_dict(bindings["bindings"]["mic"])
            self.assertEqual(action.kind, key_mapping.ActionKind.KEY_COMBO)
            self.assertEqual(action.keys, ("ctrl", "shift", "p"))
            combo_action = key_mapping.ButtonAction.from_dict(
                bindings["combo_bindings"]["bindings"]["up"]
            )
            self.assertEqual(combo_action.kind, key_mapping.ActionKind.KEY_COMBO)
            self.assertEqual(combo_action.keys, ("ctrl", "alt", "p"))
            self.assertEqual(visual["combo_typed"], "ctrl+alt+p")


def _contrast_ratio(luminance_a, luminance_b):
    """WCAG 2.x contrast ratio (always >= 1.0) between two luminances."""

    lighter, darker = max(luminance_a, luminance_b), min(luminance_a, luminance_b)
    return (lighter + 0.05) / (darker + 0.05)


# Renders the REAL main.qml with the REAL "FluentWinUI3" style in a fresh,
# throwaway subprocess and prints one JSON line with the darkest-pixel
# luminance and corner-background luminance sampled inside each of three
# controls (see RenderedContrastTests below for why this must be a
# SEPARATE process rather than sharing one with the rest of this test
# file's Qt-loading tests): QQuickStyle is a process-global, set-once
# setting - once any OTHER test in this file has loaded Qt Quick Controls
# QML with "Basic" (or any other style), a later attempt to set
# "FluentWinUI3" in the SAME process either has no effect or raises
# "QQuickStyle::setStyle() must be called before loading QML that imports
# Qt Quick Controls 2." (both reproduced while writing this test).
_CONTRAST_PROBE_SCRIPT = r"""
import json
import sys

from ovb_rc003 import qt_settings_app as m
from PySide6.QtCore import QPointF, QObject


def _linearize(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(color):
    return 0.2126 * _linearize(color.red()) + 0.7152 * _linearize(color.green()) + 0.0722 * _linearize(color.blue())


def _find(root, name):
    children = list(root.children())
    child_items = getattr(root, "childItems", None)
    if callable(child_items):
        children.extend(child for child in child_items() if child not in children)
    for child in children:
        if child.objectName() == name:
            return child
        found = _find(child, name)
        if found is not None:
            return found
    return None


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle("FluentWinUI3")
app = QGuiApplication.instance() or QGuiApplication([])
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
if len(engine.rootObjects()) != 1:
    print(json.dumps({"error": "main.qml failed to load"}))
    sys.exit(1)

window = engine.rootObjects()[0]
window.show()


def render():
    for _ in range(10):
        window.grabWindow()
        app.processEvents()
    return window.grabWindow()


def sample_control(results, image, object_name):
    item = _find(window, object_name)
    if item is None:
        print(json.dumps({"error": object_name + " not found"}))
        sys.exit(1)
    margin = 3
    corner = item.mapToScene(QPointF(1, 1))
    background_luminance = _luminance(image.pixelColor(int(corner.x()), int(corner.y())))
    top_left = item.mapToScene(QPointF(margin, margin))
    width = int(item.property("width") - 2 * margin)
    height = int(item.property("height") - 2 * margin)
    x0, y0 = int(top_left.x()), int(top_left.y())
    darkest = 1.0
    for x in range(x0, x0 + width):
        for y in range(y0, y0 + height):
            luminance = _luminance(image.pixelColor(x, y))
            if luminance < darkest:
                darkest = luminance
    results[object_name] = {"background": background_luminance, "darkest": darkest}


results = {}
image = render()
sample_control(results, image, "deviceTabButton")

tab_bar = _find(window, "tabBar")
if tab_bar is None:
    print(json.dumps({"error": "tabBar not found"}))
    sys.exit(1)
tab_bar.setProperty("currentIndex", 1)
image = render()
sample_control(results, image, "restoreMappingDefaultsButton")

tab_bar.setProperty("currentIndex", 2)
image = render()
sample_control(results, image, "holdVoiceHotkeyField")

print(json.dumps(results))
"""


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class RenderedContrastTests(unittest.TestCase):
    """XRBM-030 RETRY 1 blocker 2: renders the REAL main.qml with the REAL
    "FluentWinUI3" style (in an isolated subprocess - see
    ``_CONTRAST_PROBE_SCRIPT`` above for why) and asserts a real, measured
    WCAG contrast ratio between each sampled control's own background and
    its darkest rendered pixel - not merely "zero QML warnings" (which the
    white-on-light regression this fixes produced zero of; a rendering bug
    is not a QML error). Calibrated against the actual pre-fix regression:
    removing ``main.qml``'s explicit ``palette.*`` bindings (see that
    file's own module docstring for why they are needed) reproduces a
    measured contrast ratio of exactly 1.00 (the "text" is pixel-identical
    to the background - completely invisible) on every one of the three
    controls checked here; this test's threshold (3.0) sits far below the
    actual fixed measurement (18+) and far above the broken one (1.00), so
    it cannot pass by accident either way.
    """

    _MIN_CONTRAST_RATIO = 3.0
    _LABELS = {
        "deviceTabButton": "「设备」tab label",
        "restoreMappingDefaultsButton": "「恢复内置默认」button",
        "holdVoiceHotkeyField": "语音按键 TextField",
    }

    def test_tab_button_plain_button_and_text_field_are_all_readable(self):
        import json
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", _CONTRAST_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"contrast probe subprocess failed: {result.stdout}\n{result.stderr}",
        )
        last_line = result.stdout.strip().splitlines()[-1]
        data = json.loads(last_line)
        self.assertNotIn("error", data, data.get("error"))

        for object_name, label in self._LABELS.items():
            measurement = data[object_name]
            ratio = _contrast_ratio(measurement["background"], measurement["darkest"])
            self.assertGreaterEqual(
                ratio,
                self._MIN_CONTRAST_RATIO,
                f"{label} contrast ratio {ratio:.2f} is below "
                f"{self._MIN_CONTRAST_RATIO} - text is not reliably "
                "readable against its own background",
            )


# Proves, against the real rendered ButtonsPage, that the compact left/right
# card board remains contained, keeps three equal gesture cells, and shares
# selection with the calibrated product photo.
_MAPPING_CARD_BOARD_PROBE_SCRIPT = r"""
import json
import os

from ovb_rc003 import qt_settings_app as m, remote_layout
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest


def _find(root, name):
    children = list(root.children())
    child_items = getattr(root, "childItems", None)
    if callable(child_items):
        children.extend(child for child in child_items() if child not in children)
    for child in children:
        if child.objectName() == name:
            return child
        found = _find(child, name)
        if found is not None:
            return found
    return None


def _render(window, app, count=10):
    for _ in range(count):
        window.grabWindow()
        app.processEvents()


def _geometry(item):
    assert item is not None
    position = item.mapToScene(QPointF(0.0, 0.0))
    width = float(item.property("width"))
    height = float(item.property("height"))
    return {
        "x": position.x(),
        "y": position.y(),
        "width": width,
        "height": height,
        "right": position.x() + width,
        "bottom": position.y() + height,
        "visible": bool(item.property("visible")),
    }


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle(os.environ["RC003_TEST_STYLE"])
app = QGuiApplication.instance() or QGuiApplication([])
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
assert len(engine.rootObjects()) == 1, "main.qml failed to load"
window = engine.rootObjects()[0]
window.setProperty("width", int(os.environ["RC003_TEST_VIEWPORT_WIDTH"]))
window.setProperty("height", int(os.environ["RC003_TEST_VIEWPORT_HEIGHT"]))
window.show()
_render(window, app)

# Switch to the "按键" page so all mapping-card delegates lay out.
tab_bar = _find(window, "tabBar")
assert tab_bar is not None
tab_bar.setProperty("currentIndex", 1)
_render(window, app)

mapping_list = _find(window, "mappingList")
actions_panel = _find(window, "mappingActionsPanel")
view_switcher = _find(window, "mappingViewSwitcher")
single_view_button = _find(window, "singleMappingViewButton")
combo_view_button = _find(window, "comboMappingViewButton")
mapping_lines = _find(window, "mappingLines")
active_mapping_line = _find(window, "activeMappingLine")
left_cards = _find(window, "leftMappingCards")
right_cards = _find(window, "rightMappingCards")
photo_sidebar = _find(window, "photoSidebar")
photo_frame = _find(window, "photoFrame")
photo_image = _find(window, "photoImage")
assert mapping_list is not None, "mappingList not found"
assert actions_panel is not None
assert view_switcher is not None
assert single_view_button is not None and combo_view_button is not None
assert mapping_lines is not None and active_mapping_line is not None
assert left_cards is not None and right_cards is not None
assert photo_sidebar is not None, "photoSidebar not found"
assert photo_frame is not None and photo_frame.property("visible"), "photoFrame not visible"
assert photo_image is not None and photo_image.property("visible"), "photoImage not visible"
assert mapping_list.property("count") == 13

combo_click = combo_view_button.mapToScene(QPointF(
    combo_view_button.property("width") / 2.0,
    combo_view_button.property("height") / 2.0,
)).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, combo_click)
_render(window, app, 5)
combo_mapping_list = _find(window, "comboMappingList")
combo_modifier = _find(window, "comboModifierCombo")
combo_mapping_header = _find(window, "comboMappingHeader")
combo_mapping_rows = _find(window, "comboMappingRows")
combo_rows = {
    button_id: _find(window, "comboMappingRow_" + button_id)
    for button_id in ("up", "down", "left", "right", "ok", "back", "volume_up", "volume_down")
}
combo_editors = {
    button_id: _find(window, "comboActionEditor_" + button_id)
    for button_id in combo_rows
}
combo_titles = {
    button_id: _find(window, "comboMappingTitle_" + button_id)
    for button_id in combo_rows
}
assert combo_mapping_list is not None and combo_mapping_list.property("visible")
assert combo_modifier is not None and combo_modifier.property("visible")
assert combo_mapping_header is not None and combo_mapping_header.property("visible")
assert combo_mapping_rows is not None and combo_mapping_rows.property("visible")
assert all(item is not None and item.property("visible") for item in combo_rows.values())
assert all(item is not None and item.property("visible") for item in combo_editors.values())
assert all(item is not None and item.property("visible") for item in combo_titles.values())
combo_screenshot = os.environ.get("RC003_COMBO_MAPPING_SCREENSHOT")
if combo_screenshot:
    _render(window, app, 2)
    assert window.grabWindow().save(combo_screenshot)

single_click = single_view_button.mapToScene(QPointF(
    single_view_button.property("width") / 2.0,
    single_view_button.property("height") / 2.0,
)).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, single_click)
_render(window, app, 5)
assert mapping_list.property("visible")

for _ in range(100):
    if photo_image.property("paintedWidth") > 0 and photo_image.property("paintedHeight") > 0:
        break
    QTest.qWait(20)
    _render(window, app, 1)
assert photo_image.property("paintedWidth") > 0, "photo did not paint"

card_ids = (
    "power", "up", "left", "back", "home", "menu",
    "mic", "right", "ok", "down", "volume_up", "volume_down", "tv",
)
cards = {button_id: _find(window, "editMapping_" + button_id) for button_id in card_ids}
assert all(item is not None and item.property("visible") for item in cards.values())
single_key_title = _find(window, "mappingKeyCell_power")
assert single_key_title is not None and single_key_title.property("visible")
hotspots = {
    button_id: _find(window, "photoHotspot_" + button_id)
    for button_id in card_ids
}
assert all(item is not None and item.property("visible") for item in hotspots.values())
power_column_names = {
    "key": "mappingKeyCell_power",
    "single": "mappingSingleCell_power",
    "double": "mappingDoubleCell_power",
    "long": "mappingLongCell_power",
}
power_columns = {
    key: _geometry(_find(window, object_name))
    for key, object_name in power_column_names.items()
}
value_names = {
    "single": "mappingSinglePrimaryText_power",
    "double": "mappingDoublePrimaryText_power",
    "long": "mappingLongPrimaryText_power",
}
value_baselines = {}
value_geometries = {}
for key, object_name in value_names.items():
    item = _find(window, object_name)
    assert item is not None, object_name
    position = item.mapToScene(QPointF(0.0, 0.0))
    value_baselines[key] = position.y() + float(item.property("baselineOffset"))
    value_geometries[key] = _geometry(item)

power_card = cards["power"]
click_point = power_card.mapToScene(
    QPointF(power_card.property("width") / 2.0, power_card.property("height") / 2.0)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, click_point)
_render(window, app, 5)

power_hotspot = _find(window, "photoHotspot_power")
ok_hotspot = _find(window, "photoHotspot_ok")
power_marker = _find(window, "photoHotspotMarker_power")
ok_marker = _find(window, "photoHotspotMarker_ok")
editor = _find(window, "actionEditorDialog")
assert power_hotspot is not None, "Power photo hotspot not found"
assert ok_hotspot is not None, "OK photo hotspot not found"
assert power_marker is not None, "Power photo marker not found"
assert ok_marker is not None, "OK photo marker not found"
assert editor is not None
primary_title = _find(window, "actionEditorPrimaryTitle")
primary_combo = _find(window, "actionEditorPrimaryCombo")
assert primary_title is not None and primary_combo is not None
primary_title_center = primary_title.mapToScene(QPointF(
    primary_title.property("width") / 2.0,
    primary_title.property("height") / 2.0,
)).toPoint()
QTest.mouseMove(window, primary_title_center)
QTest.qWait(520)
_render(window, app, 5)
primary_help_background = _find(window, "actionEditorPrimaryHelp_background")
assert primary_help_background is not None
editor_screenshot = os.environ.get("RC003_MAPPING_EDITOR_SCREENSHOT")
if editor_screenshot:
    _render(window, app, 2)
    assert window.grabWindow().save(editor_screenshot)

power_layout = remote_layout.hotspot_for("power")
assert power_layout is not None
letterbox_x = (photo_image.property("width") - photo_image.property("paintedWidth")) / 2.0
letterbox_y = (photo_image.property("height") - photo_image.property("paintedHeight")) / 2.0
expected_power_center = photo_image.mapToScene(QPointF(
    letterbox_x + power_layout.x * photo_image.property("paintedWidth"),
    letterbox_y + power_layout.y * photo_image.property("paintedHeight"),
))
actual_power_center = power_hotspot.mapToScene(QPointF(
    power_hotspot.property("width") / 2.0,
    power_hotspot.property("height") / 2.0,
))

results_out = {
    "card_count": sum(bool(item.property("visible")) for item in cards.values()),
    "selected_after_power_click": controller.property("selectedButtonId"),
    "editor_visible": bool(editor.property("visible")),
    "primary_help": {
        "tooltip": _geometry(primary_help_background),
        "input": _geometry(primary_combo),
    },
    "view_switcher": _geometry(view_switcher),
    "actions_panel": _geometry(actions_panel),
    "mapping_list": _geometry(mapping_list),
    "combo_view": {
        "list": _geometry(combo_mapping_list),
        "modifier": _geometry(combo_modifier),
        "header": _geometry(combo_mapping_header),
        "rows_container": _geometry(combo_mapping_rows),
        "key_title_pixel_sizes": {
            "single": int(single_key_title.property("font").pixelSize()),
            "combo": int(combo_titles["up"].property("font").pixelSize()),
        },
        "rows": {
            button_id: _geometry(item)
            for button_id, item in combo_rows.items()
        },
        "editors": {
            button_id: _geometry(item)
            for button_id, item in combo_editors.items()
        },
    },
    "canvases": {
        "base": _geometry(mapping_lines),
        "active": _geometry(active_mapping_line),
    },
    "left_cards": _geometry(left_cards),
    "right_cards": _geometry(right_cards),
    "cards": {button_id: _geometry(item) for button_id, item in cards.items()},
    "power_columns": power_columns,
    "value_baselines": value_baselines,
    "value_geometries": value_geometries,
    "photo": {
        "sidebar": _geometry(photo_sidebar),
        "frame": _geometry(photo_frame),
        "painted_width": photo_image.property("paintedWidth"),
        "painted_height": photo_image.property("paintedHeight"),
        "power_marker_visible": power_marker.property("visible"),
        "ok_marker_visible": ok_marker.property("visible"),
        "hotspots": {
            button_id: _geometry(item)
            for button_id, item in hotspots.items()
        },
        "power_center_error_x": actual_power_center.x() - expected_power_center.x(),
        "power_center_error_y": actual_power_center.y() - expected_power_center.y(),
    },
    "viewport_width": window.property("width"),
    "viewport_height": window.property("height"),
}
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
print(json.dumps(results_out))
"""


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class ButtonsPageMappingCardTests(unittest.TestCase):
    """The compact card board, photo marker, and real editor stay connected."""

    def _run_probe(self, width=1024, height=720, style="Basic"):
        import json
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        env["RC003_TEST_VIEWPORT_WIDTH"] = str(width)
        env["RC003_TEST_VIEWPORT_HEIGHT"] = str(height)
        env["RC003_TEST_STYLE"] = style
        result = subprocess.run(
            [sys.executable, "-c", _MAPPING_CARD_BOARD_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"mapping card probe subprocess failed: {result.stdout}\n{result.stderr}",
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_board_renders_all_thirteen_cards(self):
        data = self._run_probe(720, 500)
        self.assertEqual(data["card_count"], 13)
        self.assertTrue(data["actions_panel"]["visible"])
        self.assertTrue(data["canvases"]["base"]["visible"])
        self.assertTrue(data["canvases"]["active"]["visible"])

    def test_real_click_on_power_card_selects_power_and_opens_editor(self):
        data = self._run_probe(720, 500)
        self.assertEqual(
            data["selected_after_power_click"],
            "power",
            "a real QTest click on the Power card did not select Power",
        )
        self.assertTrue(data["editor_visible"])

    def test_editor_help_tooltip_is_compact_and_does_not_cover_the_input(self):
        for style in ("Basic", "FluentWinUI3"):
            with self.subTest(style=style):
                data = self._run_probe(720, 500, style)
                tooltip = data["primary_help"]["tooltip"]
                action_input = data["primary_help"]["input"]
                self.assertTrue(tooltip["visible"])
                self.assertLessEqual(tooltip["width"], 274)
                self.assertLessEqual(tooltip["bottom"], action_input["y"])

    def test_selected_power_is_marked_at_its_calibrated_photo_position(self):
        data = self._run_probe(720, 500)
        self.assertTrue(data["photo"]["power_marker_visible"])
        self.assertFalse(data["photo"]["ok_marker_visible"])
        self.assertAlmostEqual(data["photo"]["power_center_error_x"], 0, delta=1)
        self.assertAlmostEqual(data["photo"]["power_center_error_y"], 0, delta=1)

    def test_curved_connector_routes_stay_ordered_without_overlap(self):
        data = self._run_probe(720, 500)
        cards = data["cards"]
        hotspots = data["photo"]["hotspots"]
        groups = (
            ("power", "up", "left", "back", "home", "menu"),
            ("mic", "right", "ok", "down", "volume_up", "volume_down", "tv"),
        )

        def route_for(button_id, left_side):
            card = cards[button_id]
            hotspot = hotspots[button_id]
            start_x = card["right"] if left_side else card["x"]
            start_y = card["y"] + card["height"] / 2
            end_x = hotspot["x"] + hotspot["width"] / 2
            end_y = hotspot["y"] + hotspot["height"] / 2
            span = abs(end_x - start_x)
            preferred = max(12, min(72, span * 0.56))
            radius = min(preferred, span * 0.48)
            direction = 1 if left_side else -1
            return (
                (start_x, start_y),
                (start_x + direction * radius, start_y),
                (end_x - direction * radius, end_y),
                (end_x, end_y),
            )

        def point_at(route, t):
            one_minus_t = 1 - t
            weights = (
                one_minus_t ** 3,
                3 * one_minus_t * one_minus_t * t,
                3 * one_minus_t * t * t,
                t ** 3,
            )
            return (
                sum(point[0] * weight for point, weight in zip(route, weights)),
                sum(point[1] * weight for point, weight in zip(route, weights)),
            )

        def y_at_x(route, x):
            increasing = route[-1][0] >= route[0][0]
            low = 0.0
            high = 1.0
            for _ in range(30):
                t = (low + high) / 2
                current_x = point_at(route, t)[0]
                if (current_x < x) == increasing:
                    low = t
                else:
                    high = t
            return point_at(route, (low + high) / 2)[1]

        for button_ids in groups:
            left_side = button_ids[0] == "power"
            routes = [route_for(button_id, left_side) for button_id in button_ids]
            for first_route, second_route in zip(routes, routes[1:]):
                low_x = max(
                    min(point[0] for point in first_route),
                    min(point[0] for point in second_route),
                )
                high_x = min(
                    max(point[0] for point in first_route),
                    max(point[0] for point in second_route),
                )
                self.assertLess(low_x, high_x)
                for step in range(101):
                    x = low_x + (high_x - low_x) * step / 100
                    self.assertLess(
                        y_at_x(first_route, x),
                        y_at_x(second_route, x),
                    )

        right_hotspot = hotspots["right"]
        ok_hotspot = hotspots["ok"]
        right_center = right_hotspot["y"] + right_hotspot["height"] / 2
        ok_center = ok_hotspot["y"] + ok_hotspot["height"] / 2
        self.assertAlmostEqual(right_center, ok_center, delta=1)
        self.assertNotAlmostEqual(right_hotspot["right"], ok_hotspot["right"], delta=1)
        board = data["mapping_list"]
        for canvas in data["canvases"].values():
            self.assertAlmostEqual(canvas["x"], board["x"], delta=1)
            self.assertAlmostEqual(canvas["y"], board["y"], delta=1)
            self.assertAlmostEqual(canvas["right"], board["right"], delta=1)
            self.assertAlmostEqual(canvas["bottom"], board["bottom"], delta=1)

    def test_board_fits_default_minimum_and_large_windows(self):
        viewports = (
            ("Basic", 720, 500),
            ("Basic", 640, 480),
            ("FluentWinUI3", 720, 500),
            ("FluentWinUI3", 640, 480),
            ("FluentWinUI3", 840, 720),
        )
        for style, width, height in viewports:
            data = self._run_probe(width, height, style)
            self.assertAlmostEqual(data["photo"]["sidebar"]["width"], 86, delta=0.5)
            self.assertLessEqual(data["mapping_list"]["right"], width + 1)
            self.assertLessEqual(data["actions_panel"]["right"], width + 1)
            self.assertLessEqual(
                data["view_switcher"]["bottom"], data["mapping_list"]["y"] + 1
            )
            self.assertLessEqual(data["mapping_list"]["bottom"], data["actions_panel"]["y"] + 1)
            self.assertLessEqual(data["combo_view"]["list"]["bottom"], data["actions_panel"]["y"] + 1)
            self.assertLess(data["left_cards"]["x"], data["photo"]["sidebar"]["x"])
            self.assertLess(data["photo"]["sidebar"]["x"], data["right_cards"]["x"])
            for card_column in (data["left_cards"], data["right_cards"]):
                self.assertGreaterEqual(card_column["y"], data["mapping_list"]["y"] - 1)
                self.assertLessEqual(
                    card_column["bottom"], data["mapping_list"]["bottom"] + 1
                )
            for card in data["cards"].values():
                self.assertGreaterEqual(card["height"], 48)
                self.assertLessEqual(card["height"], 50)
                self.assertLessEqual(card["right"], width + 1)
                self.assertLessEqual(card["bottom"], height + 1)
            previous_bottom = None
            for button_id in key_mapping.COMBO_ACTION_BUTTON_IDS:
                row = data["combo_view"]["rows"][button_id]
                editor = data["combo_view"]["editors"][button_id]
                self.assertLessEqual(row["right"], width + 1)
                self.assertLessEqual(row["bottom"], height + 1)
                self.assertLessEqual(
                    row["bottom"], data["combo_view"]["list"]["bottom"] + 1
                )
                self.assertGreater(editor["width"], 120)
                if previous_bottom is not None:
                    self.assertLessEqual(previous_bottom, row["y"] + 1)
                previous_bottom = row["bottom"]

            first_combo_row = data["combo_view"]["rows"][
                key_mapping.COMBO_ACTION_BUTTON_IDS[0]
            ]
            last_combo_row = data["combo_view"]["rows"][
                key_mapping.COMBO_ACTION_BUTTON_IDS[-1]
            ]
            self.assertAlmostEqual(
                first_combo_row["y"] - data["combo_view"]["header"]["bottom"],
                6,
                delta=1,
            )
            self.assertAlmostEqual(
                data["combo_view"]["rows_container"]["y"],
                first_combo_row["y"],
                delta=1,
            )
            self.assertAlmostEqual(
                data["combo_view"]["rows_container"]["height"],
                last_combo_row["bottom"] - first_combo_row["y"],
                delta=1,
            )
            self.assertEqual(
                data["combo_view"]["key_title_pixel_sizes"]["single"],
                data["combo_view"]["key_title_pixel_sizes"]["combo"],
            )

    def test_power_card_gesture_cells_are_equal_and_aligned(self):
        for style, width, height in (("Basic", 720, 500), ("Basic", 640, 480), ("FluentWinUI3", 720, 500)):
            data = self._run_probe(width, height, style)
            columns = data["power_columns"]
            for left, right in (("single", "double"), ("double", "long")):
                self.assertAlmostEqual(columns[left]["width"], columns[right]["width"], delta=1)
                self.assertLessEqual(columns[left]["right"], columns[right]["x"] + 1)
            baselines = data["value_baselines"]
            self.assertAlmostEqual(baselines["single"], baselines["double"], delta=1)
            self.assertAlmostEqual(baselines["double"], baselines["long"], delta=1)
            power_card = data["cards"]["power"]
            for item in data["value_geometries"].values():
                self.assertGreaterEqual(item["y"], power_card["y"] - 1)
                self.assertLessEqual(item["bottom"], power_card["bottom"] + 1)


if __name__ == "__main__":
    unittest.main()
