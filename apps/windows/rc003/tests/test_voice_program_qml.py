import json
import os
import subprocess
import sys
import tempfile
import unittest


_PROBE = r"""
import json
import os
import time

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from ovb_rc003 import qt_settings_app as m
from unittest.mock import Mock
m.wetype_control_windows.begin_input_profile_selection = Mock(
    return_value=Mock(result=Mock(return_value=False)))
from ovb_rc003 import chromecast_etw_windows
chromecast_etw_windows.sensitive_logging_enabled = lambda: True


def find(root, name):
    children = list(root.children())
    child_items = getattr(root, "childItems", None)
    if callable(child_items):
        children.extend(child for child in child_items() if child not in children)
    for child in children:
        if child.objectName() == name:
            return child
        found = find(child, name)
        if found is not None:
            return found
    return None


def render(window, app, count=10):
    image = None
    for _ in range(count):
        image = window.grabWindow()
        app.processEvents()
    return image


def wait_for_voice_hotkey_idle(controller, app, timeout=3.0):
    deadline = time.monotonic() + timeout
    while controller.voiceHotkeyBusy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert not controller.voiceHotkeyBusy


def geometry(item):
    if hasattr(item, "mapToScene"):
        point = item.mapToScene(QPointF(0, 0))
        x = point.x()
        y = point.y()
    else:
        x = float(item.property("x"))
        y = float(item.property("y"))
    width = float(item.property("width"))
    height = float(item.property("height"))
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "right": x + width,
        "bottom": y + height,
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


class FakeHotkeyCapture:
    def __init__(self, on_captured, *, accept_injected=False):
        self.on_captured = on_captured
        self.accept_injected = accept_injected
        self.is_running = False

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False


m.hotkey_capture_windows.HotkeyCapture = FakeHotkeyCapture


def inspect_voice_program(settings):
    provider_id = str(settings.get("provider", ""))
    return m.voice_program_manager.VoiceProgramStatus(
        provider_id=provider_id,
        display_name=m.voice_program_manager.VOICE_PROGRAM_PROVIDER_NAMES.get(
            provider_id, "测试语音程序"
        ),
        available=False,
        running=False,
        elevated=None,
        executable=None,
        code="not_found",
    )


m.voice_program_manager.inspect_voice_program = inspect_voice_program
m.voice_program_manager.launch_voice_program = lambda settings: (
    m.voice_program_manager.VoiceProgramLaunchResult(
        str(settings.get("provider", "")), False, True, "already_running"
    )
)


provider_read_requests = []


def read_provider_hotkey(
    provider_id,
    *,
    allow_settings_window=False,
    cancel_event=None,
    trigger="hold",
):
    provider_id = str(provider_id)
    provider_read_requests.append((provider_id, allow_settings_window))
    shortcuts = {
        "sogou": "rctrl",
        "wetype": "lctrl+lwin",
        "doubao_ime": "ralt",
    }
    shortcut = shortcuts.get(provider_id, "")
    if provider_id == "wetype" and not allow_settings_window:
        shortcut = ""
    return m.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
        provider_id,
        bool(shortcut),
        "read" if shortcut else "local_only",
        shortcut,
        "test provider shortcut",
    )


m.voice_hotkey_sync_windows.read_provider_hotkey = read_provider_hotkey

QQuickStyle.setStyle("Basic")
app = QGuiApplication.instance() or QGuiApplication([])
m.single_instance.bridge_instance_running = lambda: False
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics = DiagnosticsController(controller, m.config.config_root())
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
    diagnostics,
)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
warnings = []
engine.warnings.connect(lambda values: warnings.extend(values))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
assert len(engine.rootObjects()) == 1
window = engine.rootObjects()[0]
window.show()
render(window, app)

tab_bar = find(window, "tabBar")
tab_bar.setProperty("currentIndex", 2)
render(window, app)

controller.selectedVoiceProgramIndex = 1
wait_for_voice_hotkey_idle(controller, app)
render(window, app)
controller._voice_program_status_code = "not_found"
controller.voiceProgramStatusCodeChanged.emit()
render(window, app)
elevated = find(window, "voiceProgramElevatedCheckBox")
elevated_indicator = elevated.property("indicator")
image = window.grabWindow()
screenshot = os.environ.get("VOICE_PROGRAM_SCREENSHOT")
if screenshot:
    image.save(screenshot)

controls = {
    name: find(window, name)
    for name in (
        "voiceProgramSection",
        "voiceProgramSelectionRow",
        "voiceProgramCombo",
        "holdVoiceHotkeyField",
        "voiceHotkeyRow",
        "voiceProgramElevatedCheckBox",
        "recordVoiceHotkeyButton",
        "refreshVoiceHotkeyButton",
        "openVoiceProgramSettingsButton",
    )
}
assert all(control is not None for control in controls.values())

voice_page = find(window, "voiceScroll").parent()
hotkey_field = controls["holdVoiceHotkeyField"]
record_button = controls["recordVoiceHotkeyButton"]
refresh_button = controls["refreshVoiceHotkeyButton"]
refresh_state = {
    "visible": bool(refresh_button.property("visible")),
    "enabled": bool(refresh_button.property("enabled")),
}
controller._set_settings_save_busy(True)
render(window, app)
refresh_state["saving_enabled"] = bool(refresh_button.property("enabled"))
controller._set_settings_save_busy(False)
render(window, app)
controller._voice_runtime_state = "active"
controller.voiceRuntimeStatusChanged.emit()
render(window, app)
refresh_state["active_enabled"] = bool(refresh_button.property("enabled"))
controller._voice_runtime_state = "mic_confirmed"
controller.voiceRuntimeStatusChanged.emit()
render(window, app)
refresh_state["mic_confirmed_enabled"] = bool(refresh_button.property("enabled"))
controller._voice_runtime_state = "receiving_audio"
controller.voiceRuntimeStatusChanged.emit()
render(window, app)
refresh_state["receiving_audio_enabled"] = bool(refresh_button.property("enabled"))
controller._voice_runtime_state = "finishing"
controller.voiceRuntimeStatusChanged.emit()
render(window, app)
refresh_state["finishing_enabled"] = bool(refresh_button.property("enabled"))
controller._voice_runtime_state = "not_tested"
controller.voiceRuntimeStatusChanged.emit()
render(window, app)
original_hotkey = str(controller.holdVoiceHotkeyText)
record_button_center = record_button.mapToScene(
    QPointF(
        record_button.property("width") / 2,
        record_button.property("height") / 2,
    )
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, record_button_center)
render(window, app)
recording_prompt = str(hotkey_field.property("text"))
outside_target = find(window, "voiceProgramSectionTitle")
assert outside_target is not None
outside_center = outside_target.mapToScene(
    QPointF(
        outside_target.property("width") / 2,
        outside_target.property("height") / 2,
    )
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, outside_center)
render(window, app)
hotkey_cancel = {
    "recording_prompt": recording_prompt,
    "recording": bool(voice_page.property("voiceHotkeyRecording")),
    "field_text": str(hotkey_field.property("text")),
    "controller_text": str(controller.holdVoiceHotkeyText),
    "original_text": original_hotkey,
}

controller.selectedVoiceProgramIndex = 2
wait_for_voice_hotkey_idle(controller, app)
render(window, app)
assert ("wetype", True) not in provider_read_requests
controller.holdVoiceHotkeyText = "lctrl+lshift+f9"
wait_for_voice_hotkey_idle(controller, app)
render(window, app)
refresh_center = refresh_button.mapToScene(
    QPointF(refresh_button.property("width") / 2, refresh_button.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, refresh_center)
wait_for_voice_hotkey_idle(controller, app)
render(window, app)
wetype_refresh = {
    "text": str(controller.holdVoiceHotkeyText),
    "source": str(controller.voiceHotkeySource),
    "explicit_reads": provider_read_requests.count(("wetype", True)),
    "error": str(controller.errorMessage),
}
wetype_before = str(controller.holdVoiceHotkeyText)
record_button_center = record_button.mapToScene(
    QPointF(record_button.property("width") / 2, record_button.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, record_button_center)
render(window, app)
wetype_hotkey = {
    "recording": bool(voice_page.property("voiceHotkeyRecording")),
    "enabled": bool(hotkey_field.property("enabled")),
    "field_text": str(hotkey_field.property("text")),
    "controller_text": str(controller.holdVoiceHotkeyText),
    "original_text": wetype_before,
    "description": str(controls["voiceHotkeyRow"].property("descriptionText")),
    "state": str(controls["voiceHotkeyRow"].property("stateText")),
}
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, record_button_center)
render(window, app)

controller._voice_program_status_code = "stopped"
controller.voiceProgramStatusCodeChanged.emit()
render(window, app)
wetype_program_installed = str(
    controls["voiceProgramSelectionRow"].property("stateText")
)
controller._voice_program_status_code = "running"
controller.voiceProgramStatusCodeChanged.emit()
render(window, app)
wetype_program_running = str(
    controls["voiceProgramSelectionRow"].property("stateText")
)

controller.selectedVoiceProgramIndex = 4
wait_for_voice_hotkey_idle(controller, app)
render(window, app)
custom_record_button_center = record_button.mapToScene(
    QPointF(
        record_button.property("width") / 2,
        record_button.property("height") / 2,
    )
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, custom_record_button_center)
render(window, app)
QTest.keyPress(window, Qt.Key_Control, Qt.ControlModifier)
QTest.keyPress(window, Qt.Key_Shift, Qt.ControlModifier | Qt.ShiftModifier)
QTest.keyPress(window, Qt.Key_F9, Qt.ControlModifier | Qt.ShiftModifier)
QTest.keyRelease(window, Qt.Key_F9, Qt.ControlModifier | Qt.ShiftModifier)
QTest.keyRelease(window, Qt.Key_Shift, Qt.ControlModifier)
QTest.keyRelease(window, Qt.Key_Control, Qt.NoModifier)
wait_for_voice_hotkey_idle(controller, app)
render(window, app)
qt_fallback_hotkey = {
    "recording": bool(voice_page.property("voiceHotkeyRecording")),
    "field_text": str(hotkey_field.property("text")),
    "controller_text": str(controller.holdVoiceHotkeyText),
}

controller.selectedVoiceProgramIndex = 1
wait_for_voice_hotkey_idle(controller, app)
render(window, app)
controller._voice_program_status_code = "not_found"
controller.voiceProgramStatusCodeChanged.emit()
render(window, app)
current_hotkey = str(controller.holdVoiceHotkeyText)

elevated = controls["voiceProgramElevatedCheckBox"]
elevated_before = bool(controller.voiceProgramLaunchElevated)
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, record_button_center)
render(window, app)
elevated_center = elevated.mapToScene(
    QPointF(
        elevated.property("width") / 2,
        elevated.property("height") / 2,
    )
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, elevated_center)
render(window, app)
hotkey_other_action = {
    "recording": bool(voice_page.property("voiceHotkeyRecording")),
    "action_completed": bool(controller.voiceProgramLaunchElevated)
    != elevated_before,
    "field_text": str(hotkey_field.property("text")),
    "original_text": current_hotkey,
}
controller.voiceProgramLaunchElevated = elevated_before
controller._voice_program_status_code = "not_found"
controller.voiceProgramStatusCodeChanged.emit()
render(window, app)

voice_page.setProperty("voiceHotkeyRecording", True)
controller.hotkeyCaptureError.emit("模拟启动失败")
render(window, app)
inactive_capture_error = {
    "recording": bool(voice_page.property("voiceHotkeyRecording")),
    "description": str(controls["voiceHotkeyRow"].property("descriptionText")),
}
voice_page.setProperty("voiceHotkeyCaptureError", "")

retained_capture = FakeHotkeyCapture(lambda _chord: None)
retained_capture.is_running = True
controller._hotkey_capture = retained_capture
controller._set_input_operation_state("hotkey", "active")
voice_page.setProperty("voiceHotkeyRecording", True)
controller.hotkeyCaptureError.emit("模拟停止前异常")
render(window, app)
active_capture_error = {
    "recording": bool(voice_page.property("voiceHotkeyRecording")),
    "description": str(controls["voiceHotkeyRow"].property("descriptionText")),
}
controller.stopHotkeyCapture()
render(window, app)
voice_page.setProperty("voiceHotkeyCaptureError", "")

managed = {
    name: {
        "visible": bool(control.property("visible")),
        "enabled": bool(control.property("enabled")),
        "geometry": geometry(control),
        "text": str(control.property("text"))
        if name == "openVoiceProgramSettingsButton" else "",
    }
    for name, control in controls.items()
}
managed_auto_start = bool(controller.voiceProgramLaunchOnBridgeStart)
managed_elevated = bool(controller.voiceProgramLaunchElevated)
managed_hotkey_state = str(controls["voiceHotkeyRow"].property("stateText"))

controller.selectedVoiceProgramIndex = 2
render(window, app)
system_managed = {
    "provider": bool(controller.voiceProgramSystemManaged),
    "auto_start": bool(controller.voiceProgramLaunchOnBridgeStart),
    "elevated_visible": bool(elevated.property("visible")),
    "custom_path_visible": bool(
        find(window, "voiceProgramCustomPathField").property("visible")
    ),
    "settings_visible": bool(
        find(window, "openVoiceProgramSettingsButton").property("visible")
    ),
}

controller.selectedVoiceProgramIndex = 3
wait_for_voice_hotkey_idle(controller, app)
render(window, app)
doubao = {
    "provider": bool(controller.voiceProgramSystemManaged),
    "launchable": bool(controller.voiceProgramLaunchable),
    "auto_start": bool(controller.voiceProgramLaunchOnBridgeStart),
    "elevated_visible": bool(elevated.property("visible")),
    "settings_visible": bool(
        find(window, "openVoiceProgramSettingsButton").property("visible")
    ),
    "refresh_visible": bool(
        find(window, "refreshVoiceHotkeyButton").property("visible")
    ),
    "description": str(controls["voiceHotkeyRow"].property("descriptionText")),
}

controller.selectedVoiceProgramIndex = 4
wait_for_voice_hotkey_idle(controller, app)
render(window, app)
custom_program = {
    "path_visible": bool(
        find(window, "voiceProgramCustomPathField").property("visible")
    ),
    "elevated_visible": bool(elevated.property("visible")),
    "settings_visible": bool(
        find(window, "openVoiceProgramSettingsButton").property("visible")
    ),
}

# Load a legacy unconfigured document; index zero can no longer be selected.
controller._replace_voice_program_settings({"provider": "none"})
render(window, app)
unmanaged_elevated = {
    "visible": bool(elevated.property("visible")),
    "enabled": bool(elevated.property("enabled")),
}

result = {
    "warnings": [value.toString() for value in warnings],
    "window_width": float(window.property("width")),
    "window_height": float(window.property("height")),
    "managed": managed,
    "elevated_indicator": geometry(elevated_indicator),
    "managed_auto_start": managed_auto_start,
    "managed_elevated": managed_elevated,
    "managed_hotkey_state": managed_hotkey_state,
    "refresh_state": refresh_state,
    "hotkey_cancel": hotkey_cancel,
    "wetype_hotkey": wetype_hotkey,
    "wetype_refresh": wetype_refresh,
    "wetype_program_installed": wetype_program_installed,
    "wetype_program_running": wetype_program_running,
    "qt_fallback_hotkey": qt_fallback_hotkey,
    "hotkey_other_action": hotkey_other_action,
    "inactive_capture_error": inactive_capture_error,
    "active_capture_error": active_capture_error,
    "system_managed": system_managed,
    "doubao": doubao,
    "custom_program": custom_program,
    "unmanaged_elevated": unmanaged_elevated,
    "retired_controls_absent": all(
        find(window, name) is None
        for name in (
            "voiceProgramButton",
            "voiceProgramDialog",
            "voiceProgramAutoStartCheckBox",
            "saveVoiceProgramButton",
            "refreshVoiceProgramButton",
            "launchVoiceProgramButton",
            "voiceProgramSpecificRow",
            "voiceProgramLaunchText",
            "voiceProgramStatusRefreshTimer",
            "useWindowsDictationHotkeyButton",
        )
    ),
}
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
print(json.dumps(result, ensure_ascii=False))
"""


class VoiceProgramQmlTests(unittest.TestCase):
    def test_running_sogou_is_shown_as_running_without_a_voice_window(self):
        probe = _PROBE.replace(
            'controller.shutdownBackgroundTasks()',
            '''controller._voice_program_settings = (
    m.voice_program_manager.normalize_voice_program_settings({"provider": "sogou"})
)
controller.selectedVoiceProgramIndexChanged.emit()
controller._voice_program_status_code = "running"
controller.voiceProgramStatusCodeChanged.emit()
render(window, app)
result["sogou_running_state"] = str(
    controls["voiceProgramSelectionRow"].property("stateText")
)
controller.shutdownBackgroundTasks()''',
        )
        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", probe],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"voice-program QML probe failed: {result.stdout}\n{result.stderr}",
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data["sogou_running_state"], "运行中")

    def test_voice_page_owns_the_provider_specific_voice_program_controls(self):
        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", _PROBE],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"voice-program QML probe failed: {result.stdout}\n{result.stderr}",
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data["warnings"], [])
        self.assertTrue(data["retired_controls_absent"])
        self.assertTrue(all(item["visible"] for item in data["managed"].values()))
        self.assertTrue(all(item["enabled"] for item in data["managed"].values()))
        self.assertEqual(
            data["managed"]["openVoiceProgramSettingsButton"]["text"],
            "去安装",
        )
        self.assertTrue(
            data["managed"]["openVoiceProgramSettingsButton"]["visible"]
        )
        self.assertTrue(data["managed_auto_start"])
        self.assertTrue(data["managed_elevated"])
        self.assertTrue(data["refresh_state"]["visible"])
        self.assertTrue(data["refresh_state"]["enabled"])
        self.assertEqual(data["managed_hotkey_state"], "自动识别")
        self.assertFalse(data["refresh_state"]["saving_enabled"])
        self.assertFalse(data["refresh_state"]["active_enabled"])
        self.assertFalse(data["refresh_state"]["mic_confirmed_enabled"])
        self.assertFalse(data["refresh_state"]["receiving_audio_enabled"])
        self.assertFalse(data["refresh_state"]["finishing_enabled"])
        self.assertEqual(data["hotkey_cancel"]["recording_prompt"], "请按快捷键")
        self.assertFalse(data["hotkey_cancel"]["recording"])
        self.assertEqual(
            data["hotkey_cancel"]["field_text"],
            data["hotkey_cancel"]["original_text"],
        )
        self.assertEqual(
            data["hotkey_cancel"]["controller_text"],
            data["hotkey_cancel"]["original_text"],
        )
        self.assertTrue(data["wetype_hotkey"]["recording"])
        self.assertTrue(data["wetype_hotkey"]["enabled"])
        self.assertEqual(data["wetype_hotkey"]["field_text"], "请按快捷键")
        self.assertEqual(
            data["wetype_hotkey"]["controller_text"],
            data["wetype_hotkey"]["original_text"],
        )
        self.assertEqual(
            data["wetype_hotkey"]["description"],
            "刷新时会打开微信设置",
        )
        self.assertEqual(data["wetype_refresh"], {
            "text": "lctrl+lwin", "source": "auto", "explicit_reads": 1, "error": "",
        })
        self.assertEqual(data["wetype_hotkey"]["state"], "录入中")
        self.assertEqual(data["wetype_program_installed"], "已安装")
        self.assertEqual(data["wetype_program_running"], "运行中")
        self.assertFalse(data["qt_fallback_hotkey"]["recording"])
        self.assertEqual(
            data["qt_fallback_hotkey"]["field_text"],
            "ctrl+shift+f9",
        )
        self.assertEqual(
            data["qt_fallback_hotkey"]["controller_text"],
            "ctrl+shift+f9",
        )
        self.assertFalse(data["hotkey_other_action"]["recording"])
        self.assertTrue(data["hotkey_other_action"]["action_completed"])
        self.assertEqual(
            data["hotkey_other_action"]["field_text"],
            data["hotkey_other_action"]["original_text"],
        )
        self.assertFalse(data["inactive_capture_error"]["recording"])
        self.assertEqual(
            data["inactive_capture_error"]["description"],
            "模拟启动失败",
        )
        self.assertTrue(data["active_capture_error"]["recording"])
        self.assertEqual(
            data["active_capture_error"]["description"],
            "模拟停止前异常",
        )
        self.assertEqual(data["elevated_indicator"]["width"], 16)
        self.assertEqual(data["elevated_indicator"]["height"], 16)
        self.assertAlmostEqual(
            data["managed"]["voiceProgramCombo"]["geometry"]["y"],
            data["managed"]["voiceProgramElevatedCheckBox"]["geometry"]["y"],
            delta=1,
        )
        self.assertTrue(data["system_managed"]["provider"])
        self.assertFalse(data["system_managed"]["auto_start"])
        self.assertFalse(data["system_managed"]["elevated_visible"])
        self.assertFalse(data["system_managed"]["custom_path_visible"])
        self.assertTrue(data["system_managed"]["settings_visible"])
        self.assertFalse(data["doubao"]["provider"])
        self.assertFalse(data["doubao"]["launchable"])
        self.assertFalse(data["doubao"]["auto_start"])
        self.assertFalse(data["doubao"]["elevated_visible"])
        self.assertTrue(data["doubao"]["settings_visible"])
        self.assertTrue(data["doubao"]["refresh_visible"])
        self.assertEqual(
            data["doubao"]["description"],
            "自动读取按住型快捷键",
        )
        self.assertTrue(data["custom_program"]["path_visible"])
        self.assertTrue(data["custom_program"]["elevated_visible"])
        self.assertFalse(data["custom_program"]["settings_visible"])
        self.assertFalse(data["unmanaged_elevated"]["visible"])
        self.assertFalse(data["unmanaged_elevated"]["enabled"])
        self.assertLessEqual(
            data["managed"]["holdVoiceHotkeyField"]["geometry"]["right"],
            data["managed"]["recordVoiceHotkeyButton"]["geometry"]["x"] + 1,
        )
        self.assertGreaterEqual(
            data["managed"]["refreshVoiceHotkeyButton"]["geometry"]["x"],
            data["managed"]["holdVoiceHotkeyField"]["geometry"]["x"],
        )
        self.assertLessEqual(
            data["managed"]["refreshVoiceHotkeyButton"]["geometry"]["right"],
            data["managed"]["holdVoiceHotkeyField"]["geometry"]["right"] + 1,
        )
        self.assertEqual(
            data["managed"]["refreshVoiceHotkeyButton"]["geometry"]["width"],
            28,
        )
        for item in data["managed"].values():
            bounds = item["geometry"]
            self.assertGreater(bounds["width"], 0)
            self.assertGreater(bounds["height"], 0)
            self.assertGreaterEqual(bounds["x"], 0)
            self.assertGreaterEqual(bounds["y"], 0)
            self.assertLessEqual(bounds["right"], data["window_width"])
            self.assertLessEqual(bounds["bottom"], data["window_height"])


if __name__ == "__main__":
    unittest.main()
