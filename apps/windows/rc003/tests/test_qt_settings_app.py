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
    bridge_launcher,
    config,
    device_catalog,
    frida_compat,
    hotkey,
    key_detection_bridge,
    key_mapping,
    qt_settings_app,
    remote_layout,
    settings_ui,
    shell_targets,
    single_instance,
    vb_cable_bundle,
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

    def tearDown(self):
        self._bridge_status_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _make_controller(self):
        model = self.Model()
        controller = self.Controller(model)
        return controller, model

    def test_hotkey_text_defaults_to_the_configured_default(self):
        controller, _ = self._make_controller()
        self.assertEqual(controller.hotkeyText, "ralt+space")

    def test_primary_options_include_both_voice_lifecycles(self):
        controller, _ = self._make_controller()
        self.assertIn(settings_ui._VOICE_TOGGLE_DISPLAY, controller.primaryActionOptions)
        self.assertIn(settings_ui._VOICE_HOLD_DISPLAY, controller.primaryActionOptions)

    def test_secondary_options_exclude_voice_lifecycles(self):
        controller, _ = self._make_controller()
        self.assertNotIn(
            settings_ui._VOICE_TOGGLE_DISPLAY,
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

    def test_recording_a_hotkey_does_not_change_trigger_semantics(self):
        controller, _ = self._make_controller()
        controller._on_hotkey_capture_result("lctrl+lwin")
        self.assertEqual(controller.triggerModeIndex, 0)
        controller.hotkeyText = "lctrl+lwin"
        self.assertEqual(controller.hotkeyText, "lctrl+lwin")

    def test_launch_status_starts_as_the_not_started_constant(self):
        controller, _ = self._make_controller()
        self.assertEqual(controller.launchStatusText, settings_ui.LAUNCH_NOT_STARTED_TEXT)

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
            controller.selectedDeviceIndex,
            controller._DEVICE_ORDER.index(device_catalog.RC003_ID),
        )
        self.assertTrue(controller.isRc003Device)
        self.assertFalse(controller.isDjiMic2Device)
        self.assertEqual(controller.mappingPageTitle, "按键映射")

    def test_selecting_dji_changes_the_ui_contract_and_persists(self):
        controller, _ = self._make_controller()
        controller.selectedDeviceIndex = controller._DEVICE_ORDER.index(
            device_catalog.DJI_MIC_2_ID
        )
        self.assertFalse(controller.isRc003Device)
        self.assertTrue(controller.isDjiMic2Device)
        self.assertEqual(controller.mappingPageTitle, "设备控制")
        self.assertIn("Windows 录音输入", controller.selectedDeviceDescription)
        self.assertTrue(controller.saveSettings())
        stored = config.load_config(config.config_path(Path(self._tmpdir.name) / "RemoteMic" / "RC003"))
        self.assertEqual(stored["selected_device_profile"], device_catalog.DJI_MIC_2_ID)

    def test_dji_control_rows_match_the_truthful_device_catalog(self):
        controller, _ = self._make_controller()
        self.assertEqual(
            [row["name"] for row in controller.djiControlRows],
            ["录音键", "连接键", "电源键"],
        )
        self.assertTrue(all("映射" in row["mapping"] for row in controller.djiControlRows))

    def test_dji_save_and_launch_never_starts_the_rc003_bridge(self):
        controller, _ = self._make_controller()
        controller.selectedDeviceIndex = controller._DEVICE_ORDER.index(
            device_catalog.DJI_MIC_2_ID
        )
        with mock.patch.object(bridge_launcher, "launch_bridge") as fake_launch:
            controller.saveAndLaunch()
        fake_launch.assert_not_called()
        self.assertIn("不启动 RC003", controller.launchStatusText)

    def test_save_settings_persists_and_clears_error_message(self):
        controller, model = self._make_controller()
        model.setActionTextAt(model.index_of("power"), "escape")
        self.assertTrue(controller.saveSettings())
        self.assertEqual(controller.errorMessage, "")
        self.assertIn("已保存", controller.statusMessage)

    def test_save_settings_reports_a_persistence_failure(self):
        controller, _ = self._make_controller()
        with mock.patch.object(
            config, "save_settings_pair", side_effect=OSError("settings file is locked")
        ):
            self.assertFalse(controller.saveSettings())
        self.assertIn("保存失败", controller.errorMessage)
        self.assertIn("settings file is locked", controller.errorMessage)

    def test_save_settings_uses_the_mapped_voice_lifecycle(self):
        controller, model = self._make_controller()
        controller.toggleVoiceHotkeyText = "lalt+space"
        controller.holdVoiceHotkeyText = "ctrl+l"
        model.setActionTextAt(model.index_of("mic"), "Escape")
        model.setActionTextAt(
            model.index_of("up"),
            settings_ui._VOICE_HOLD_DISPLAY,
        )

        self.assertTrue(controller.saveSettings())

        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(saved["voice_trigger_mode"], "hold")
        self.assertEqual(saved["voice_hotkey"], "ctrl+l")
        self.assertEqual(saved["voice_hotkeys"]["toggle"], "lalt+space")
        self.assertEqual(saved["voice_hotkeys"]["hold"], "ctrl+l")
        saved_bindings = config.load_key_bindings(
            config.key_bindings_path(config.config_root())
        )
        self.assertEqual(saved_bindings["bindings"]["mic"]["kind"], "escape")
        self.assertEqual(saved_bindings["bindings"]["up"]["kind"], "voice_hold")

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
        controller.toggleVoiceHotkeyText = ""
        controller.holdVoiceHotkeyText = ""

        with mock.patch.object(
            qt_settings_app.audio_playback,
            "preflight_output_endpoint",
            side_effect=AssertionError("preflight must be skipped without voice"),
        ) as preflight:
            self.assertTrue(controller.saveSettings())

        preflight.assert_not_called()
        saved = config.load_config(config.config_path(config.config_root()))
        self.assertEqual(saved["voice_hotkeys"], {"toggle": "", "hold": ""})

    def test_save_settings_with_empty_hotkey_fails_and_reports_error(self):
        controller, _ = self._make_controller()
        controller.toggleVoiceHotkeyText = ""
        self.assertFalse(controller.saveSettings())
        self.assertNotEqual(controller.errorMessage, "")

    def test_save_and_launch_never_launches_when_save_fails(self):
        controller, _ = self._make_controller()
        controller.toggleVoiceHotkeyText = ""
        with mock.patch.object(bridge_launcher, "launch_bridge") as fake_launch:
            controller.saveAndLaunch()
        fake_launch.assert_not_called()
        self.assertEqual(controller.launchStatusText, settings_ui.LAUNCH_NOT_STARTED_TEXT)

    def test_save_and_launch_launches_and_reports_started_when_save_succeeds(self):
        controller, _ = self._make_controller()
        fake_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.STARTED, command=("exe",), pid=4321
        )
        with mock.patch.object(bridge_launcher, "launch_bridge", return_value=fake_result):
            controller.saveAndLaunch()
        self.assertIn("4321", controller.launchStatusText)
        self.assertNotIn("已连接", controller.launchStatusText)

    def test_restore_defaults_resets_both_hotkeys_and_mic_mapping(self):
        controller, model = self._make_controller()
        controller.toggleVoiceHotkeyText = "shift+z"
        controller.holdVoiceHotkeyText = "ctrl+l"
        model.setActionTextAt(model.index_of("mic"), "Escape")
        controller.restoreDefaults()
        self.assertEqual(controller.toggleVoiceHotkeyText, "ralt+space")
        self.assertEqual(controller.holdVoiceHotkeyText, "ralt")
        mic_index = model.index(model.index_of("mic"), 0)
        self.assertEqual(
            model.data(mic_index, model.ActionTextRole),
            settings_ui._VOICE_TOGGLE_DISPLAY,
        )

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
        self.assertIn("Raw Input unavailable", controller.keyDetectionText)

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
        self.assertIn("无法安全确认后台桥接状态", controller.keyDetectionText)
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
        self.assertIn("无法安全确认后台桥接状态", controller.keyDetectionText)
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
        self.assertFalse(controller.keyDetectionActive)
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
        self.assertFalse(controller.keyDetectionActive)

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

    def test_select_and_persist_output_endpoint_succeeds_and_updates_options(self):
        controller, _ = self._make_controller()
        with mock.patch.object(
            qt_settings_app.audio_playback, "preflight_output_endpoint"
        ) as preflight:
            result = controller.selectAndPersistOutputEndpoint(
                "CABLE Input", "Windows WASAPI"
            )
        self.assertTrue(result)
        preflight.assert_called_once_with("CABLE Input", "Windows WASAPI")
        reloaded = config.load_config(config.config_path(controller._config_root))
        self.assertEqual(reloaded["output_endpoint_name"], "CABLE Input")
        self.assertEqual(reloaded["output_endpoint_host_api"], "Windows WASAPI")

    def test_select_and_persist_output_endpoint_returns_false_on_persistence_failure(self):
        # XRBM-031 RETRY 1 item 3: a config-save failure (disk full,
        # permission denied, ...) must never raise out of this Slot and
        # must never be reported as a successful save.
        controller, _ = self._make_controller()
        original_config = dict(controller._config)
        with mock.patch.object(
            qt_settings_app.audio_playback, "preflight_output_endpoint"
        ), mock.patch.object(config, "save_config", side_effect=OSError("disk full")):
            result = controller.selectAndPersistOutputEndpoint("CABLE Input", "Windows WASAPI")
        self.assertFalse(result)
        # The in-memory config must not look saved when it was not.
        self.assertEqual(controller._config, original_config)

    def test_select_and_persist_output_endpoint_never_raises_on_unexpected_error(self):
        controller, _ = self._make_controller()
        with mock.patch.object(
            qt_settings_app.audio_playback, "preflight_output_endpoint"
        ), mock.patch.object(config, "save_config", side_effect=RuntimeError("boom")):
            result = controller.selectAndPersistOutputEndpoint("CABLE Input", "")
        self.assertFalse(result)

    def test_select_and_persist_output_endpoint_rejects_failed_preflight(self):
        controller, _ = self._make_controller()
        original_config = dict(controller._config)
        with mock.patch.object(
            qt_settings_app.audio_playback,
            "preflight_output_endpoint",
            side_effect=audio_output.AudioOutputUnavailableError("cannot open"),
        ), mock.patch.object(config, "save_config") as save_config:
            result = controller.selectAndPersistOutputEndpoint(
                "CABLE Input", "Windows WASAPI"
            )
        self.assertFalse(result)
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

    def tearDown(self):
        qt_settings_app._diagnostics_shutdown_event.clear()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _make_settings_controller(self):
        model = self.Model()
        return self.SettingsController(model)

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
        self.assertEqual(len(diag.checkResults), 7)
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
        self.assertEqual(len(diag.checkResults), 7)  # a real prior run populated these
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
        self.assertEqual(len(diag.checkResults), 7)

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

    def test_select_detected_cable_input_persists_via_settings_controller(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        endpoint = audio_output.AudioEndpoint(name="CABLE Input", host_api="Windows WASAPI")
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=[endpoint]
        ), mock.patch.object(
            qt_settings_app.audio_playback, "preflight_output_endpoint"
        ):
            result = diag.selectDetectedCableInputAsOutput()

        self.assertTrue(result)
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

        self.assertFalse(result)
        self.assertIn("未检测到", diag.driverErrorMessage)

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
                "selectAndPersistOutputEndpoint",
                side_effect=RuntimeError("boom"),
            ):
                result = diag.selectDetectedCableInputAsOutput()

        self.assertFalse(result)
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
                settings_controller, "selectAndPersistOutputEndpoint", return_value=False
            ):
                result = diag.selectDetectedCableInputAsOutput()

        self.assertFalse(result)
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

        self.assertFalse(result)
        self.assertIn("无法唯一确定", diag.driverErrorMessage)

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
            qt_settings_app.audio_playback, "preflight_output_endpoint"
        ):
            result = diag.selectDetectedCableInputAsOutput()

        self.assertTrue(result)
        reloaded = config.load_config(config.config_path(self._config_root))
        self.assertEqual(reloaded["output_endpoint_host_api"], "Windows WASAPI")

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
        self.assertIn("重新检测", diag.driverInfoMessage)
        self.assertNotIn("安装成功", diag.driverInfoMessage)


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class RunSettingsWindowShutdownCoverageTests(unittest.TestCase):
    """XRBM-035 RETRY 1 P2/E: ``run_settings_window()``'s production
    shutdown contract must cover every exit path starting right after
    ``DiagnosticsController`` is constructed (that constructor already
    started a real background worker) - not only ``app.exec()`` returning
    normally, which is all the previous round's ``try/finally`` covered.
    Drives the REAL function end to end (never a source-level/AST proxy) -
    only the Qt WINDOW plumbing (``QGuiApplication``/``QQmlApplicationEngine``
    /``QQuickStyle``/``QUrl``/``qmlRegisterSingletonInstance``) is replaced
    with small, fully-controllable fakes (real PySide6/shiboken C++ types
    are not reliably monkeypatchable, and no real QML window needs to exist
    to prove this contract) - ``ButtonMappingModel``/``SettingsController``/
    ``DiagnosticsController`` stay the REAL classes ``_load_qt_classes()``
    itself already produced, so the background worker this test is actually
    about is completely real.
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

    def test_hotkey_cleanup_failure_cannot_skip_detection_or_worker_shutdown(self):
        fake_classes = self._fake_classes(root_objects=[object()], exec_return=0)
        controller_class = fake_classes["SettingsController"]
        detection_calls = []
        with mock.patch.object(
            qt_settings_app, "_load_qt_classes", return_value=fake_classes
        ), mock.patch.object(
            controller_class,
            "stopHotkeyCapture",
            side_effect=RuntimeError("simulated capture cleanup failure"),
        ), mock.patch.object(
            controller_class,
            "stopKeyDetection",
            side_effect=lambda instance: detection_calls.append(instance),
            autospec=True,
        ), mock.patch.object(
            qt_settings_app,
            "_shutdown_diagnostics_workers",
            wraps=qt_settings_app._shutdown_diagnostics_workers,
        ) as shutdown_spy:
            with self.assertRaises(RuntimeError):
                qt_settings_app.run_settings_window()

        self.assertEqual(len(detection_calls), 1)
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
_QML_LOAD_PROBE_SCRIPT = r"""
import faulthandler
import json
import sys

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
engine.warnings.connect(lambda ws: warnings.extend(ws))
print("STAGE:connected", file=sys.stderr, flush=True)

engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
print("STAGE:loaded", file=sys.stderr, flush=True)

root_objects = engine.rootObjects()
result = {
    "root_count": len(root_objects),
    "warnings": [w.toString() for w in warnings],
    "width": root_objects[0].property("width") if root_objects else None,
    "height": root_objects[0].property("height") if root_objects else None,
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
m._shutdown_diagnostics_workers()
print("STAGE:shutdown", file=sys.stderr, flush=True)

print(json.dumps(result))
"""


_DJI_DEVICE_PAGE_PROBE_SCRIPT = r"""
import json

from ovb_rc003 import qt_settings_app as m


def find_child(root, name):
    for child in root.children():
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
model = ButtonMappingModel()
controller = SettingsController(model)
controller.selectedDeviceIndex = controller._DEVICE_ORDER.index(m.device_catalog.DJI_MIC_2_ID)
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

dji_layout = find_child(window, "djiControlLayout")
rc003_layout = find_child(window, "rc003MappingLayout")
assert dji_layout is not None
assert rc003_layout is not None
result = {
    "dji_visible": bool(dji_layout.property("visible")),
    "rc003_visible": bool(rc003_layout.property("visible")),
    "mapping_page_title": controller.mappingPageTitle,
    "control_names": [row["name"] for row in controller.djiControlRows],
}
tab_bar.setProperty("currentIndex", 2)
for _ in range(10):
    window.grabWindow()
    app.processEvents()
result.update(
    {
        "bluetooth_permission_visible": bool(
            find_child(window, "bluetoothPermissionBlock").property("visible")
        ),
        "optional_enhancements_visible": bool(
            find_child(window, "optionalEnhancementsSection").property("visible")
        ),
        "host_voice_setup_visible": bool(
            find_child(window, "hostVoiceSetupBlock").property("visible")
        ),
        "microphone_permission_text": str(
            find_child(window, "microphonePermissionDescription").property("text")
        ),
    }
)
m._shutdown_diagnostics_workers()
print(json.dumps(result))
"""


_SETTINGS_SHELL_LAYOUT_PROBE_SCRIPT = r"""
import json
import os

from PySide6.QtCore import QPointF
from ovb_rc003 import qt_settings_app as m


def find_child(root, name):
    for child in root.children():
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
    return {
        "visible": bool(item.property("visible")),
        "x": float(origin.x()),
        "y": float(origin.y()),
        "width": width,
        "height": float(item.property("height")),
        "right": float(origin.x()) + width,
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
    "openLogButton",
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
    "save_highlighted": bool(find_child(window, "deviceSaveButton").property("highlighted")),
    "launch_highlighted": bool(find_child(window, "saveAndLaunchButton").property("highlighted")),
}

tab_bar.setProperty("currentIndex", 2)
render(window, app)
permissions_names = (
    "permissionsPageContent",
    "requiredPermissionsSection",
    "openBluetoothSettingsButton",
    "openMicrophonePrivacyButton",
    "openSoundInputSettingsButton",
    "optionalEnhancementsSection",
    "openDiagnosticsButton",
    "manualSetupSection",
    "openMappingButton",
    "openSpeechSettingsButton",
    "permissionsTroubleshootingSection",
    "permissionsOpenLogButton",
)
permissions_scroll = find_child(window, "permissionsScroll")
result["permissions"] = {
    "items": {name: bounds(window, name) for name in permissions_names},
    "content_width": float(permissions_scroll.property("contentWidth")),
    "available_width": float(permissions_scroll.property("availableWidth")),
}

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

m._shutdown_diagnostics_workers()
print(json.dumps(result))
"""


_TAB_FOCUS_SCROLL_PROBE_SCRIPT = r"""
import json

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from ovb_rc003 import qt_settings_app as m


def find_child(root, name):
    for child in root.children():
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


def tab_to(window, app, target, count):
    for _ in range(count):
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
connection_reached = tab_to(window, app, launch, 4)
QTest.keyClick(window, Qt.Key_Tab)
render(window, app)
connection_escaped = not bool(launch.property("activeFocus")) and navigation_has_focus(window)

tab_bar.setProperty("currentIndex", 2)
render(window, app)
permissions_scroll = find_child(window, "permissionsScroll")
permissions_flickable = permissions_scroll.property("contentItem")
permissions_flickable.setProperty("contentY", 0)
microphone = find_child(window, "openMicrophonePrivacyButton")
log_button = find_child(window, "permissionsOpenLogButton")
microphone.forceActiveFocus(Qt.TabFocusReason)
render(window, app)
permissions_reached = tab_to(window, app, log_button, 5)
QTest.keyClick(window, Qt.Key_Tab)
render(window, app)
permissions_escaped = not bool(log_button.property("activeFocus")) and navigation_has_focus(window)

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
        "target_visible": visible_in_window(log_button, window),
    },
}
m._shutdown_diagnostics_workers()
print(json.dumps(result))
"""


class SettingsShellSourceContractTests(unittest.TestCase):
    def setUp(self):
        qml_dir = Path(qt_settings_app.__file__).resolve().parent / "qml"
        self.main_qml = (qml_dir / "main.qml").read_text(encoding="utf-8")
        self.connection_qml = (qml_dir / "ConnectionPage.qml").read_text(
            encoding="utf-8"
        )
        self.permissions_qml = (qml_dir / "PermissionsPage.qml").read_text(
            encoding="utf-8"
        )
        self.buttons_qml = (qml_dir / "ButtonsPage.qml").read_text(
            encoding="utf-8"
        )

    def test_settings_feedback_has_one_global_owner(self):
        self.assertIn('objectName: "globalStatusBar"', self.main_qml)
        for page_text in (
            self.connection_qml,
            self.permissions_qml,
            self.buttons_qml,
        ):
            self.assertNotIn("SettingsController.errorMessage", page_text)
            self.assertNotIn("SettingsController.statusMessage", page_text)

    def test_connection_keeps_save_and_launch_as_distinct_commands(self):
        self.assertIn('qsTr("仅保存设置")', self.connection_qml)
        self.assertIn('qsTr("保存并启动桥接")', self.connection_qml)
        self.assertIn("SettingsController.saveSettings()", self.connection_qml)
        self.assertIn("SettingsController.saveAndLaunch()", self.connection_qml)
        self.assertIn("恢复按键与语音默认", self.connection_qml)
        self.assertNotIn("恢复全部默认", self.connection_qml)
        self.assertIn("已保存但当前缺失的端点", self.connection_qml)
        self.assertIn("启用语音映射时", self.connection_qml)

    def test_permissions_page_states_real_boundaries_without_fake_grants(self):
        for heading in ("运行必需", "可选增强", "手动操作"):
            self.assertIn(heading, self.permissions_qml)
        for misleading_claim in (
            "已授权",
            "Remote Mic 需要管理员权限",
            "VB-CABLE 安装成功",
        ):
            self.assertNotIn(misleading_claim, self.permissions_qml)
        self.assertIn("仅 Win+H", self.permissions_qml)
        self.assertIn("普通按键映射不依赖这些设置", self.permissions_qml)

    def test_permissions_navigation_reuses_existing_pages(self):
        self.assertIn("signal openMappingRequested()", self.permissions_qml)
        self.assertIn("signal openDiagnosticsRequested()", self.permissions_qml)
        self.assertIn("onOpenMappingRequested: tabBar.currentIndex = 1", self.main_qml)
        self.assertIn(
            "onOpenDiagnosticsRequested: tabBar.currentIndex = 3", self.main_qml
        )

    def test_buttons_page_uses_the_full_width_mapping_matrix(self):
        self.assertIn('objectName: "mappingMatrixHeader"', self.buttons_qml)
        for column_name in ("遥控器按键", "单击", "双击", "长按"):
            self.assertIn(f'qsTr("{column_name}")', self.buttons_qml)
        self.assertIn('objectName: "editMapping_" + mappingRow.buttonId', self.buttons_qml)
        self.assertIn('objectName: "actionEditorDialog"', self.buttons_qml)
        self.assertIn("mappingRow.index < mappingList.count - 1", self.buttons_qml)
        self.assertIn("ListView {", self.buttons_qml)
        self.assertNotIn("GridView {", self.buttons_qml)


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
        self.assertGreater(data["width"], 0)
        self.assertGreater(data["height"], 0)

    def test_dji_device_page_hides_rc003_mapping_and_shows_dji_controls(self):
        import json
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", _DJI_DEVICE_PAGE_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0, f"DJI QML page probe failed: {result.stderr}"
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(data["dji_visible"])
        self.assertFalse(data["rc003_visible"])
        self.assertEqual(data["mapping_page_title"], "设备控制")
        self.assertEqual(data["control_names"], ["录音键", "连接键", "电源键"])
        self.assertFalse(data["bluetooth_permission_visible"])
        self.assertFalse(data["optional_enhancements_visible"])
        self.assertFalse(data["host_voice_setup_visible"])
        self.assertIn("DJI Mic 2", data["microphone_permission_text"])
        self.assertNotIn("CABLE Output", data["microphone_permission_text"])

    def test_settings_shell_fits_supported_logical_viewports_without_horizontal_overflow(self):
        import json
        import subprocess

        # Logical sizes corresponding to the supported physical viewports:
        # 1024x720@100%, 1366x768@125%, 1920x1080@150%, plus the stricter
        # 1024x720@150% fallback used on small high-DPI laptops.
        scenarios = ((1024, 720), (1093, 614), (1280, 720), (683, 480))

        for width, height in scenarios:
            with self.subTest(width=width, height=height), tempfile.TemporaryDirectory() as tmpdir:
                env = dict(os.environ)
                env.setdefault("QT_QPA_PLATFORM", "offscreen")
                env["LOCALAPPDATA"] = tmpdir
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
                self.assertFalse(data["initial_status_visible"])

                for group_name in ("navigation",):
                    for item_name, item in data[group_name].items():
                        self.assertGreater(item["width"], 0, item_name)
                        self.assertGreaterEqual(item["x"], -1, item_name)
                        self.assertLessEqual(item["right"], width + 1, item_name)

                for page_name in ("connection", "permissions"):
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

                connection_items = data["connection"]["items"]
                self.assertLess(
                    connection_items["deviceSection"]["y"],
                    connection_items["rc003OutputSection"]["y"],
                )
                self.assertLess(
                    connection_items["rc003OutputSection"]["y"],
                    connection_items["bridgeSection"]["y"],
                )
                self.assertLess(
                    connection_items["bridgeSection"]["y"],
                    connection_items["connectionActionRow"]["y"],
                )
                self.assertFalse(data["connection"]["save_highlighted"])
                self.assertTrue(data["connection"]["launch_highlighted"])
                self.assertNotIn("RC003 已连接", data["connection"]["launch_status"])

                permission_items = data["permissions"]["items"]
                self.assertLess(
                    permission_items["requiredPermissionsSection"]["y"],
                    permission_items["optionalEnhancementsSection"]["y"],
                )
                self.assertLess(
                    permission_items["optionalEnhancementsSection"]["y"],
                    permission_items["manualSetupSection"]["y"],
                )
                self.assertLess(
                    permission_items["manualSetupSection"]["y"],
                    permission_items["permissionsTroubleshootingSection"]["y"],
                )

                self.assertTrue(data["neutral_status"]["visible"])
                self.assertEqual(data["neutral_status"]["text"], "neutral status")
                self.assertTrue(data["error_status"]["visible"])
                self.assertEqual(data["error_status"]["text"], "priority error")

    def test_tab_focus_scrolls_connection_and_permissions_commands_into_view(self):
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
            self.assertGreater(page["content_y"], 0, page_name)
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
import sys

from ovb_rc003 import qt_settings_app as m
from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest


def _find_child_by_object_name(root, name):
    for child in root.children():
        if child.objectName() == name:
            return child
        found = _find_child_by_object_name(child, name)
        if found is not None:
            return found
    return None


def _find_mapping_row_control(mapping_list, button_id, model, object_name):
    mapping_list.setProperty("currentIndex", model.index_of(button_id))
    current_item = mapping_list.property("currentItem")
    if current_item is None:
        return None
    return _find_child_by_object_name(current_item, object_name)


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
assert _find_child_by_object_name(window, "toggleVoiceModeButton") is None
assert _find_child_by_object_name(window, "holdVoiceModeButton") is None
for field_name in ("toggleVoiceHotkeyField", "holdVoiceHotkeyField"):
    field = _find_child_by_object_name(window, field_name)
    assert field is not None and field.property("visible"), field_name + " missing"

edit_button = _find_mapping_row_control(mapping_list, "mic", model, "editMapping_mic")
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
assert not double_combo.property("visible") and not long_combo.property("visible")

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
# The model already reflects the live, uncommitted-by-Enter edit (blocker
# 1's fix) - checked BEFORE any save runs, so a regression that removes the
# live-commit wiring but leaves onAccepted/onActivated intact cannot
# silently pass this test by "saving" a value it only just picked up.
assert model.to_display_map()["mic"] == typed
for _ in range(3):
    window.grabWindow()
    app.processEvents()
assert double_combo.property("visible") and long_combo.property("visible")
assert double_combo.property("enabled") and long_combo.property("enabled")

done_button = _find_child_by_object_name(window, "actionEditorDoneButton")
assert done_button is not None
done_center = done_button.mapToScene(
    QPointF(done_button.property("width") / 2, done_button.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, done_center)
for _ in range(3):
    window.grabWindow()
    app.processEvents()
assert not editor.property("visible")

# Real click on "保存映射" - deliberately never press Enter/Return anywhere
# in this test.
save_button = _find_child_by_object_name(window, "saveMappingButton")
assert save_button is not None
save_center = save_button.mapToScene(
    QPointF(save_button.property("width") / 2, save_button.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, save_center)
app.processEvents()

assert controller.errorMessage == "", f"save reported a validation error: {controller.errorMessage}"
print("OK")
"""


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class ButtonsPageDirectSaveIntegrationTests(unittest.TestCase):
    """XRBM-030 RETRY 1 blocker 1: a REAL Qt/QML interaction test (see
    ``_DIRECT_SAVE_PROBE_SCRIPT`` above) proving a user can type a custom
    chord through the microphone row's matrix editor and click "保存映射"
    WITHOUT ever pressing Enter. It also locks the fix15 UI contract: both
    host-shortcut fields exist, the old global lifecycle buttons do not, and
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
            self.assertIn("OK", result.stdout)

            # The real, persisted file - read back from the SAME
            # LOCALAPPDATA the subprocess wrote to, after it has exited.
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmpdir}):
                bindings = config.load_key_bindings(
                    config.key_bindings_path(config.config_root())
                )
            action = key_mapping.ButtonAction.from_dict(bindings["bindings"]["mic"])
            self.assertEqual(action.kind, key_mapping.ActionKind.KEY_COMBO)
            self.assertEqual(action.keys, ("ctrl", "shift", "p"))


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
    for child in root.children():
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
for object_name in ("connectionTabButton", "openLogButton"):
    sample_control(results, image, object_name)

tab_bar = _find(window, "tabBar")
if tab_bar is None:
    print(json.dumps({"error": "tabBar not found"}))
    sys.exit(1)
tab_bar.setProperty("currentIndex", 1)
image = render()
sample_control(results, image, "toggleVoiceHotkeyField")

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
        "connectionTabButton": "「连接」tab label",
        "openLogButton": "「打开日志目录」button",
        "toggleVoiceHotkeyField": "开关型语音快捷键 TextField",
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
            result.returncode, 0, f"contrast probe subprocess failed: {result.stderr}"
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


# Proves, against the real rendered ButtonsPage, that the mapping matrix owns
# exactly 13 rows and a real click on another row updates the shared selected
# button. This replaces the old product-photo hotspot UI contract.
_MAPPING_MATRIX_PROBE_SCRIPT = r"""
import json
import sys

from ovb_rc003 import qt_settings_app as m
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest


def _find(root, name):
    for child in root.childItems():
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
window.show()
content_item = window.property("contentItem")
for _ in range(10):
    window.grabWindow()
    app.processEvents()

# Switch to the "按键" tab (index 1) so the matrix delegates lay out.
tab_bar = _find(content_item, "tabBar")
assert tab_bar is not None
tab_bar.setProperty("currentIndex", 1)
for _ in range(10):
    window.grabWindow()
    app.processEvents()

mapping_list = _find(content_item, "mappingList")
header = _find(content_item, "mappingMatrixHeader")
assert mapping_list is not None, "mappingList not found"
assert header is not None and header.property("visible"), "matrix header not visible"
assert mapping_list.property("count") == 13

# The default selected row is OK. Move to Power so its delegate is realized,
# then click the row body away from the edit button.
power_index = model.index_of("power")
mapping_list.setProperty("currentIndex", power_index)
for _ in range(5):
    window.grabWindow()
    app.processEvents()
power_row = mapping_list.property("currentItem")
assert power_row is not None
click_point = power_row.mapToScene(
    QPointF(power_row.property("width") / 2.0, power_row.property("height") / 2.0)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, click_point)
for _ in range(5):
    window.grabWindow()
    app.processEvents()

results_out = {
    "row_count": mapping_list.property("count"),
    "header_visible": header.property("visible"),
    "selected_after_power_click": controller.property("selectedButtonId"),
}
print(json.dumps(results_out))
"""


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class ButtonsPageMappingMatrixTests(unittest.TestCase):
    """The full-width matrix renders all physical buttons and row clicks
    continue to drive the shared selection used by real-key detection.
    """

    def _run_probe(self):
        import json
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", _MAPPING_MATRIX_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"mapping matrix probe subprocess failed: {result.stdout}\n{result.stderr}",
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_matrix_renders_all_thirteen_rows_and_header(self):
        data = self._run_probe()
        self.assertEqual(data["row_count"], 13)
        self.assertTrue(data["header_visible"])

    def test_real_click_on_power_row_selects_power(self):
        data = self._run_probe()
        self.assertEqual(
            data["selected_after_power_click"],
            "power",
            "a real QTest click on the Power matrix row did not select Power",
        )


if __name__ == "__main__":
    unittest.main()
