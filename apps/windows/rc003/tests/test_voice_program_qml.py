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


def wait_for_voice_program_refresh(controller, app, timeout=10.0):
    deadline = time.monotonic() + timeout
    while (
        controller._voice_program_status_refresh_running
        or controller._voice_program_status_refresh_pending
    ) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert not controller._voice_program_status_refresh_running
    assert not controller._voice_program_status_refresh_pending


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
    def __init__(self, on_captured):
        self.on_captured = on_captured
        self.is_running = False

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False


m.hotkey_capture_windows.HotkeyCapture = FakeHotkeyCapture

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
find(window, "voiceProgramStatusRefreshTimer").setProperty("running", False)
wait_for_voice_program_refresh(controller, app)

controller.selectedVoiceProgramIndex = 1
wait_for_voice_program_refresh(controller, app)
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
        "voiceProgramCombo",
        "holdVoiceHotkeyField",
        "voiceProgramElevatedCheckBox",
        "voiceProgramLaunchText",
        "openVoiceProgramSettingsButton",
    )
}
assert all(control is not None for control in controls.values())

voice_page = find(window, "voiceScroll").parent()
hotkey_field = controls["holdVoiceHotkeyField"]
original_hotkey = str(controller.holdVoiceHotkeyText)
hotkey_center = hotkey_field.mapToScene(
    QPointF(
        hotkey_field.property("width") / 2,
        hotkey_field.property("height") / 2,
    )
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, hotkey_center)
render(window, app)
recording_prompt = str(hotkey_field.property("text"))
outside_target = controls["voiceProgramLaunchText"]
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

elevated = controls["voiceProgramElevatedCheckBox"]
elevated_before = bool(controller.voiceProgramLaunchElevated)
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, hotkey_center)
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
    "original_text": original_hotkey,
}
controller.voiceProgramLaunchElevated = elevated_before
controller._voice_program_status_code = "not_found"
controller.voiceProgramStatusCodeChanged.emit()
render(window, app)

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
managed_status = str(controls["voiceProgramLaunchText"].property("text"))


def rendered_status(
    code,
    text,
    elevation,
    *,
    bridge_running,
    settings_dirty,
    voice_program_dirty,
):
    controller._set_bridge_running(bridge_running)
    controller._set_settings_dirty(settings_dirty)
    controller._set_voice_program_settings_dirty(voice_program_dirty)
    controller._voice_program_status_code = code
    controller._voice_program_status_text = text
    controller._voice_program_elevation_status = elevation
    controller.voiceProgramStatusCodeChanged.emit()
    controller.voiceProgramStatusTextChanged.emit()
    controller.voiceProgramElevationStatusChanged.emit()
    render(window, app)
    label = controls["voiceProgramLaunchText"]
    return {
        "text": str(label.property("text")),
        "color": label.property("color").name(),
        "settings_button_text": str(
            controls["openVoiceProgramSettingsButton"].property("text")
        ),
        "settings_button_visible": bool(
            controls["openVoiceProgramSettingsButton"].property("visible")
        ),
    }


status_cases = {
    "unknown_running": rendered_status(
        "running",
        "正在运行（权限状态未知）。",
        "unknown",
        bridge_running=True,
        settings_dirty=False,
        voice_program_dirty=False,
    ),
    "standard_mismatch": rendered_status(
        "running",
        "正在运行（普通权限）。",
        "standard",
        bridge_running=True,
        settings_dirty=False,
        voice_program_dirty=False,
    ),
}
controller.voiceProgramLaunchElevated = False
status_cases["standard_running"] = rendered_status(
    "running",
    "正在运行（普通权限）。",
    "standard",
    bridge_running=True,
    settings_dirty=False,
    voice_program_dirty=False,
)
status_cases["stopped_clean"] = rendered_status(
    "stopped",
    "已找到，当前未运行。",
    "unknown",
    bridge_running=True,
    settings_dirty=False,
    voice_program_dirty=False,
)
status_cases["stopped_unrelated_dirty"] = rendered_status(
    "stopped",
    "已找到，当前未运行。",
    "unknown",
    bridge_running=True,
    settings_dirty=True,
    voice_program_dirty=False,
)
status_cases["stopped_voice_program_dirty"] = rendered_status(
    "stopped",
    "已找到，当前未运行。",
    "unknown",
    bridge_running=True,
    settings_dirty=True,
    voice_program_dirty=True,
)

controller.selectedVoiceProgramIndex = 2
render(window, app)
system_managed = {
    "provider": bool(controller.voiceProgramSystemManaged),
    "auto_start": bool(controller.voiceProgramLaunchOnBridgeStart),
    "elevated_visible": bool(elevated.property("visible")),
    "launch_text": str(find(window, "voiceProgramLaunchText").property("text")),
    "custom_path_visible": bool(
        find(window, "voiceProgramCustomPathField").property("visible")
    ),
    "settings_visible": bool(
        find(window, "openVoiceProgramSettingsButton").property("visible")
    ),
}

controller.selectedVoiceProgramIndex = 3
render(window, app)
windows_dictation = {
    "provider": bool(controller.voiceProgramSystemManaged),
    "auto_start": bool(controller.voiceProgramLaunchOnBridgeStart),
    "elevated_visible": bool(elevated.property("visible")),
    "hotkey_button_visible": bool(
        find(window, "useWindowsDictationHotkeyButton").property("visible")
    ),
    "settings_visible": bool(
        find(window, "openVoiceProgramSettingsButton").property("visible")
    ),
    "hotkey_field_geometry": geometry(find(window, "holdVoiceHotkeyField")),
    "hotkey_button_geometry": geometry(
        find(window, "useWindowsDictationHotkeyButton")
    ),
    "settings_button_geometry": geometry(
        find(window, "openVoiceProgramSettingsButton")
    ),
    "launch_text": str(find(window, "voiceProgramLaunchText").property("text")),
}

controller.selectedVoiceProgramIndex = 4
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

controller.selectedVoiceProgramIndex = 0
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
    "status": managed_status,
    "managed_auto_start": managed_auto_start,
    "managed_elevated": managed_elevated,
    "hotkey_cancel": hotkey_cancel,
    "hotkey_other_action": hotkey_other_action,
    "status_cases": status_cases,
    "system_managed": system_managed,
    "windows_dictation": windows_dictation,
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
        )
    ),
}
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
print(json.dumps(result, ensure_ascii=False))
"""


class VoiceProgramQmlTests(unittest.TestCase):
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
        self.assertTrue(
            all(
                not case["settings_button_visible"]
                for case in data["status_cases"].values()
            )
        )
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
        self.assertFalse(data["hotkey_other_action"]["recording"])
        self.assertTrue(data["hotkey_other_action"]["action_completed"])
        self.assertEqual(
            data["hotkey_other_action"]["field_text"],
            data["hotkey_other_action"]["original_text"],
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
        self.assertEqual(
            data["system_managed"]["launch_text"],
            "由 Windows 管理，无需本程序启动",
        )
        self.assertTrue(data["windows_dictation"]["provider"])
        self.assertFalse(data["windows_dictation"]["auto_start"])
        self.assertFalse(data["windows_dictation"]["elevated_visible"])
        self.assertTrue(data["windows_dictation"]["hotkey_button_visible"])
        self.assertTrue(data["windows_dictation"]["settings_visible"])
        self.assertLessEqual(
            data["windows_dictation"]["hotkey_field_geometry"]["right"],
            data["windows_dictation"]["hotkey_button_geometry"]["x"] + 1,
        )
        self.assertLessEqual(
            data["windows_dictation"]["hotkey_button_geometry"]["right"],
            data["windows_dictation"]["settings_button_geometry"]["x"] + 1,
        )
        self.assertLessEqual(
            data["windows_dictation"]["settings_button_geometry"]["right"],
            data["window_width"] + 1,
        )
        self.assertEqual(
            data["windows_dictation"]["launch_text"],
            "使用 Windows 听写与联机语音识别",
        )
        self.assertTrue(data["custom_program"]["path_visible"])
        self.assertTrue(data["custom_program"]["elevated_visible"])
        self.assertFalse(data["custom_program"]["settings_visible"])
        self.assertEqual(
            data["status_cases"]["unknown_running"]["text"],
            "运行中 · 权限未知；设置：在任务栏（含隐藏图标）右键搜狗语音图标",
        )
        self.assertEqual(
            data["status_cases"]["unknown_running"]["settings_button_text"],
            "打开设置",
        )
        expected_sogou_status = {
            "standard_mismatch": "需重启为管理员",
            "standard_running": "普通权限运行中",
            "stopped_clean": "已找到 · 待启动",
            "stopped_unrelated_dirty": "已找到 · 待启动",
            "stopped_voice_program_dirty": "已修改 · 待应用",
        }
        for case_name, status_text in expected_sogou_status.items():
            self.assertEqual(
                data["status_cases"][case_name]["text"],
                status_text
                + "；设置：在任务栏（含隐藏图标）右键搜狗语音图标",
            )
        self.assertTrue(data["unmanaged_elevated"]["visible"])
        self.assertFalse(data["unmanaged_elevated"]["enabled"])
        for item in data["managed"].values():
            bounds = item["geometry"]
            self.assertGreater(bounds["width"], 0)
            self.assertGreater(bounds["height"], 0)
            self.assertGreaterEqual(bounds["x"], 0)
            self.assertGreaterEqual(bounds["y"], 0)
            self.assertLessEqual(bounds["right"], data["window_width"])
            self.assertLessEqual(bounds["bottom"], data["window_height"])
        self.assertTrue(data["status"])


if __name__ == "__main__":
    unittest.main()
