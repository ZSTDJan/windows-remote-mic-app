"""Configuration journeys use isolated files and never start real devices."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ovb_rc003 import config, bridge_runtime_status, key_mapping, qt_settings_app
from tests import test_qt_settings_app as controller_tests


@unittest.skipUnless(controller_tests._HAS_PYSIDE6, controller_tests._SKIP_REASON)
class ConfigurationFlowTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtCore import QCoreApplication

        self.qt = QCoreApplication.instance() or QCoreApplication([])
        self.fixture = controller_tests.SettingsControllerTests()
        # We invoke this fixture manually, so unittest will not run its
        # registered patch cleanups. Register them before setup and keep
        # teardown first (cleanups execute in reverse order).
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def controller(self):
        controller, model = self.fixture._make_controller()
        self.addCleanup(lambda: controller._mapping_auto_save_timer.stop())
        return controller, model

    def select_output(self, controller, name="CABLE Input"):
        def preflight(_name, _api, completed):
            completed(True, "")
            return True

        with mock.patch.object(controller, "_request_endpoint_preflight", preflight), \
                mock.patch.object(controller, "_request_endpoint_options_refresh"):
            return controller.selectAndPersistOutputEndpoint(name, "Windows WASAPI")

    def test_output_restart_survives_healthy_poll_and_clears_after_stop(self):
        controller, _ = self.controller()
        controller._set_bridge_running(True)
        controller._set_bridge_connected(True)
        self.assertTrue(self.select_output(controller))
        self.assertTrue(controller.bridgeRestartRecommended)
        bridge_runtime_status.publish_status(
            controller._config_root, bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=os.getpid(), raw_input_state="ready", hid_tap_state="ready",
            voice_key_physicalizer_state="ready",
        )
        status = bridge_runtime_status.read_status(controller._config_root)
        with mock.patch.object(bridge_runtime_status, "runtime_identity_matches", return_value=True):
            controller._update_bridge_restart_recommendation(status, schedule_recovery=True)
        self.assertTrue(controller.bridgeRestartRecommended)
        self.assertFalse(controller._bridge_recovery_attempted)
        controller._set_bridge_running(False)
        controller._set_bridge_restart_recommended(False)
        self.assertFalse(controller.bridgeRestartRecommended)
        controller._set_bridge_running(True)
        self.assertTrue(self.select_output(controller))  # same endpoint
        self.assertFalse(controller.bridgeRestartRecommended)

    def test_output_save_failure_does_not_create_restart_requirement(self):
        controller, _ = self.controller()
        controller._set_bridge_running(True)
        before = dict(controller._config)
        with mock.patch.object(config, "save_config_and_load", side_effect=OSError("locked")):
            self.assertFalse(self.select_output(controller))
        self.assertFalse(controller.bridgeRestartRecommended)
        self.assertEqual(controller._config, before)

    def test_combined_save_also_keeps_output_restart_action(self):
        controller, _ = self.controller()
        controller._set_bridge_running(True)
        endpoint = qt_settings_app.audio_output.AudioEndpoint("CABLE Input", "Windows WASAPI")
        controller._endpoint_values = [endpoint]
        controller._endpoint_options = [qt_settings_app.settings_ui._endpoint_display(endpoint)]
        controller._selected_endpoint_index = 0
        with mock.patch.object(controller, "_request_endpoint_preflight",
                               lambda name, api, done: (done(True, ""), True)[1]):
            self.assertTrue(controller._save(), controller.errorMessage)
        self.assertTrue(controller.bridgeRestartRecommended)

    def test_output_saved_while_stopped_does_not_require_restart(self):
        controller, _ = self.controller()
        self.assertTrue(self.select_output(controller))
        self.assertFalse(controller.bridgeRestartRecommended)

    def test_mapping_autosave_and_restore_work_with_empty_voice_hotkey(self):
        data = config.default_config()
        config.set_voice_hotkey_for_provider(data, "none", "")
        config.save_config(config.config_path(config.config_root()), data)
        controller, model = self.controller()
        model.setActionTextAt(model.index_of("power"), "ctrl+l")
        controller._run_mapping_auto_save()
        self.assertFalse(controller.mappingDirty, controller.errorMessage)
        saved = config.load_key_bindings(config.key_bindings_path(config.config_root()))
        self.assertEqual(saved["bindings"]["power"]["keys"], ["ctrl", "l"])
        self.assertTrue(key_mapping.is_voice_action(
            key_mapping.ButtonAction.from_dict(saved["bindings"]["mic"])
        ))
        self.assertEqual(config.load_config(config.config_path(config.config_root()))["voice_hotkey"], "")
        controller.restoreMappingDefaults()
        controller._run_mapping_auto_save()
        self.assertFalse(controller.mappingDirty, controller.errorMessage)
        self.assertEqual(controller.holdVoiceHotkeyText, "")

    def test_mapping_only_save_still_rejects_invalid_mapping(self):
        controller, model = self.controller()
        model.setActionTextAt(model.index_of("power"), "not_a_key")
        self.assertFalse(controller._save_mapping())
        self.assertTrue(controller.mappingDirty)

    def test_voice_launch_keeps_qt_responsive_and_rejects_duplicate_requests(self):
        from PySide6.QtCore import QTimer

        controller, _ = self.controller()
        controller._background_task_runner = None
        started, release = threading.Event(), threading.Event()
        ticks = []

        def launch(_settings):
            started.set()
            release.wait(2)
            return qt_settings_app.voice_program_manager.VoiceProgramLaunchResult(
                "none", False, False, "cancelled"
            )

        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        try:
            with mock.patch.object(qt_settings_app.voice_program_manager, "launch_voice_program", side_effect=launch) as run:
                controller.launchVoiceProgram()
                self.assertTrue(controller.voiceHotkeyBusy)
                self.assertTrue(started.wait(1))
                controller.launchVoiceProgram()
                deadline = time.monotonic() + 1
                while not ticks and time.monotonic() < deadline:
                    self.qt.processEvents()
                    time.sleep(.005)
                self.assertTrue(ticks, "Qt timer could not run during provider launch")
                self.assertEqual(run.call_count, 1)
                release.set()
                self.fixture._wait_for_qt(self.qt, lambda: not controller.voiceHotkeyBusy, "launch stayed busy")
                self.assertEqual(controller.errorMessage, "")
        finally:
            release.set()
            timer.stop()
            controller.shutdownBackgroundTasks()

    def test_voice_launch_exception_and_late_exit_result_are_safe(self):
        controller, _ = self.controller()
        queued = []
        controller._background_task_runner = lambda task, _name: queued.append(task)
        with mock.patch.object(qt_settings_app.voice_program_manager, "launch_voice_program", side_effect=OSError("failed")):
            controller.launchVoiceProgram()
            queued.pop()()
        self.assertFalse(controller.voiceHotkeyBusy)
        self.assertIn("启动失败", controller.errorMessage)
        controller.launchVoiceProgram()
        message = controller.statusMessage
        controller._application_exit_intent.set()
        with mock.patch.object(qt_settings_app.voice_program_manager, "launch_voice_program"):
            queued.pop()()
        self.assertFalse(controller.voiceHotkeyBusy)
        self.assertEqual(controller.statusMessage, message)

    def test_sogou_timeout_keeps_operation_owned_until_external_write_settles(self):

        controller, _ = self.controller()
        controller.selectedVoiceProgramIndex = 1  # Sogou, through the public property.
        pending = []
        controller._background_task_runner = lambda callback, _name: pending.append(callback)
        config_file = config.config_path(config.config_root())
        local_before = config_file.read_bytes()
        provider_root = config.config_root() / "sogou-appdata"
        provider_file = (
            qt_settings_app.voice_hotkey_sync_windows._sogou_config_path(provider_root)
        )
        provider_file.parent.mkdir(parents=True)
        provider_file.write_text(
            json.dumps(
                {
                    "setting": {
                        "shortcutKeysPress": ["RightCtrl"],
                        "longPressEnabled": True,
                    }
                }
            ),
            encoding="utf-8",
        )
        replace_started = threading.Event()
        release_replace = threading.Event()
        original_replace = qt_settings_app.voice_hotkey_sync_windows._replace_bytes_atomically

        def blocked_replace(path, content, **kwargs):
            replace_started.set()
            if not release_replace.wait(2.0):
                raise TimeoutError("test did not release provider write")
            original_replace(path, content, **kwargs)

        def real_temporary_sync(provider_id, shortcut, **kwargs):
            self.assertEqual(provider_id, "sogou")
            return qt_settings_app.voice_hotkey_sync_windows._sync_sogou_hotkey(
                shortcut,
                appdata=provider_root,
                cancel_event=kwargs.get("cancel_event"),
            )

        self.fixture._voice_hotkey_sync_mock.side_effect = real_temporary_sync
        with mock.patch.object(
            qt_settings_app.voice_hotkey_sync_windows,
            "_sogou_voice_process_running",
            return_value=False,
        ), mock.patch.object(
            qt_settings_app.voice_hotkey_sync_windows,
            "_replace_bytes_atomically",
            side_effect=blocked_replace,
        ):
            controller.holdVoiceHotkeyText = "lctrl+lshift+f10"
            self.assertTrue(controller.voiceHotkeyBusy)
            self.assertEqual(len(pending), 1)
            worker = threading.Thread(target=pending.pop(), daemon=True)
            worker.start()
            try:
                self.assertTrue(replace_started.wait(1.0))
                controller._on_voice_hotkey_task_timeout()
                self.assertTrue(controller.voiceHotkeyBusy)
                self.assertIn("等待安全结束", controller.statusMessage)
            finally:
                release_replace.set()
                worker.join(1.0)

        self.qt.processEvents()
        external = qt_settings_app.voice_hotkey_sync_windows._read_sogou_hotkey(
            appdata=provider_root
        )
        self.assertTrue(external.ok)
        self.assertEqual(external.hotkey, "rctrl")
        self.assertEqual(config_file.read_bytes(), local_before)
        self.assertNotEqual(controller.holdVoiceHotkeyText, "lctrl+lshift+f10")
        self.assertFalse(controller.voiceHotkeyBusy)


@unittest.skipUnless(controller_tests._HAS_PYSIDE6, controller_tests._SKIP_REASON)
class ConfigurationQmlFlowTests(unittest.TestCase):
    def test_saved_output_exposes_existing_restart_action(self):
        from tests.test_hid_runtime_upgrade_qml import _SETUP

        script = _SETUP + r'''
from ovb_rc003 import bridge_runtime_status, hid_elevation_windows
from unittest import mock
find(window, "bridgeStatusRefreshTimer").setProperty("running", False)
find(window, "tabBar").setProperty("currentIndex", 2)
render(window, app)
wait_for_voice_hotkey_idle(controller, app)
controller._bridge_running = True
controller.bridgeRunningChanged.emit()
controller._set_bridge_connected(True)
controller._set_bridge_connection_state("connected")
controller._hid_helper_state = hid_elevation_windows.HidHelperState(True, "ready")
controller.hidHelperStateChanged.emit()
bridge_runtime_status.publish_status(m.config.config_root(), bridge_runtime_status.BridgeConnectionState.CONNECTED,
    pid=os.getpid(), raw_input_state="ready", hid_tap_state="ready", voice_key_physicalizer_state="ready")
controller._set_bridge_input_states(bridge_runtime_status.read_status(m.config.config_root()))
controller._set_bridge_restart_recommended(False)
page = find(window, "deviceScroll").parent()
assert not page.bridgeActionVisible()
controller._request_endpoint_preflight = lambda name, api, done: (done(True, ""), True)[1]
controller._request_endpoint_options_refresh = lambda: None
assert controller.selectAndPersistOutputEndpoint("CABLE Input", "Windows WASAPI")
assert page.bridgeActionVisible()
assert page.bridgeActionText() == "重启服务"
diagnostics._is_refreshing = False
diagnostics.isRefreshingChanged.emit()
assert find(window, "trySpeakingButton").property("enabled")
assert find(window, "actualSpeechInstruction").property("text") == "点击输入框，用遥控器说一句话，查看文字是否输入。"
find(window, "tabBar").setProperty("currentIndex", 0)
with mock.patch.object(controller, "restartBridge") as restart:
    find(window, "bridgeActionButton").clicked.emit()
    restart.assert_called_once_with()
assert '点击“应用”' not in (m._qml_directory() / "VoicePage.qml").read_text(encoding="utf-8")
assert '点击“选推荐端点”' in (m._qml_directory() / "VoicePage.qml").read_text(encoding="utf-8")
print(json.dumps({"warnings": [str(item) for item in warnings]}))
window.hide()
m._shutdown_diagnostics_workers()
'''
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, LOCALAPPDATA=tmp, QT_QPA_PLATFORM="offscreen", RC003_DISABLE_LIVE_INPUT="1")
            result = subprocess.run([sys.executable, "-B", "-c", script], env=env,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])["warnings"], [])
