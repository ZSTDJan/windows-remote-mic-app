import json
import os
import subprocess
import sys
import tempfile
import unittest


_PROBE = r"""
import json
import os

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
tab_bar.setProperty("currentIndex", 0)
render(window, app)

controller.selectedVoiceProgramIndex = 1
render(window, app)
elevated = find(window, "voiceProgramElevatedCheckBox")
point = elevated.mapToScene(
    QPointF(elevated.property("width") / 2, elevated.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
render(window, app, 3)
image = window.grabWindow()
screenshot = os.environ.get("VOICE_PROGRAM_SCREENSHOT")
if screenshot:
    image.save(screenshot)

controls = {
    name: find(window, name)
    for name in (
        "voiceInputSection",
        "voiceProgramCombo",
        "holdVoiceHotkeyField",
        "voiceProgramElevatedCheckBox",
        "voiceProgramStatusLabel",
    )
}
assert all(control is not None for control in controls.values())

managed = {
    name: {
        "visible": bool(control.property("visible")),
        "enabled": bool(control.property("enabled")),
        "geometry": geometry(control),
    }
    for name, control in controls.items()
}
managed_auto_start = bool(controller.voiceProgramLaunchOnBridgeStart)
managed_elevated = bool(controller.voiceProgramLaunchElevated)
managed_status = str(controls["voiceProgramStatusLabel"].property("text"))

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
    "status": managed_status,
    "managed_auto_start": managed_auto_start,
    "managed_elevated": managed_elevated,
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
m._shutdown_diagnostics_workers()
print(json.dumps(result, ensure_ascii=False))
"""


class VoiceProgramQmlTests(unittest.TestCase):
    def test_connection_page_owns_the_compact_optional_voice_program_controls(self):
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
        self.assertTrue(data["managed_auto_start"])
        self.assertTrue(data["managed_elevated"])
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
