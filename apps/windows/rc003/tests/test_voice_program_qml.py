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
tab_bar.setProperty("currentIndex", 1)
render(window, app)

button = find(window, "voiceProgramButton")
dialog = find(window, "voiceProgramDialog")
assert button is not None and dialog is not None
point = button.mapToScene(
    QPointF(button.property("width") / 2, button.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
render(window, app)

controller.selectedVoiceProgramIndex = 1
render(window, app)
for name in ("voiceProgramAutoStartCheckBox", "voiceProgramElevatedCheckBox"):
    control = find(window, name)
    point = control.mapToScene(
        QPointF(control.property("width") / 2, control.property("height") / 2)
    ).toPoint()
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
    render(window, app, 3)
image = window.grabWindow()
screenshot = os.environ.get("VOICE_PROGRAM_SCREENSHOT")
if screenshot:
    image.save(screenshot)

result = {
    "warnings": [value.toString() for value in warnings],
    "dialog_visible": bool(dialog.property("visible")),
    "dialog": geometry(dialog),
    "window_width": float(window.property("width")),
    "window_height": float(window.property("height")),
    "button": geometry(button),
    "status": str(find(window, "voiceProgramStatusLabel").property("text")),
    "auto_start": bool(controller.voiceProgramLaunchOnBridgeStart),
    "elevated": bool(controller.voiceProgramLaunchElevated),
    "controls": {
        name: bool(find(window, name).property("visible"))
        for name in (
            "voiceProgramCombo",
            "voiceProgramAutoStartCheckBox",
            "voiceProgramElevatedCheckBox",
            "refreshVoiceProgramButton",
            "launchVoiceProgramButton",
            "saveVoiceProgramButton",
        )
    },
}
m._shutdown_diagnostics_workers()
print(json.dumps(result, ensure_ascii=False))
"""


class VoiceProgramQmlTests(unittest.TestCase):
    def test_button_opens_a_contained_feature_complete_dialog(self):
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
        self.assertTrue(data["dialog_visible"])
        self.assertTrue(all(data["controls"].values()))
        self.assertTrue(data["auto_start"])
        self.assertTrue(data["elevated"])
        self.assertGreater(data["dialog"]["width"], 0)
        self.assertGreater(data["dialog"]["height"], 0)
        self.assertGreaterEqual(data["dialog"]["x"], 0)
        self.assertGreaterEqual(data["dialog"]["y"], 0)
        self.assertLessEqual(data["dialog"]["right"], data["window_width"])
        self.assertLessEqual(data["dialog"]["bottom"], data["window_height"])
        self.assertTrue(data["status"])


if __name__ == "__main__":
    unittest.main()
