"""Real QML selection/path/startup workflow; temporary config, no host launch."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests.test_hid_runtime_upgrade_qml import _SETUP


_PROBE = _SETUP.replace('QQuickStyle.setStyle("Basic")',
                       'QQuickStyle.setStyle(os.environ.get("VOICE_SELECTION_STYLE", "Basic"))') + r'''
from PySide6.QtCore import QMetaObject, Q_ARG
from unittest import mock
find(window, "bridgeStatusRefreshTimer").setProperty("running", False)
find(window, "tabBar").setProperty("currentIndex", 2)
window.setProperty("width", 640)
window.setProperty("height", 480)
render(window, app)
rc003_recording_row = find(window, "rc003RemoteRecordingModeRow")
assert rc003_recording_row.property("visible")
assert rc003_recording_row.property("descriptionText") == "仅支持按住型"
assert not rc003_recording_row.property("editorColumnVisible")
assert not find(window, "remoteRecordingModeRow").property("visible")
combo = find(window, "voiceProgramCombo")
assert combo.property("count") == 4
assert combo.property("currentIndex") == -1, (combo.property("currentIndex"), controller.selectedVoiceProgramIndex)
assert combo.property("displayText") == "请选择语音程序"
assert not find(window, "voiceHotkeyRow").property("visible")
assert not find(window, "voiceProgramAutoStartRow").property("visible")

def choose(index, provider):
    assert QMetaObject.invokeMethod(combo, "activated", Q_ARG(int, index))
    wait_for_voice_hotkey_idle(controller, app)
    render(window, app)
    assert controller._voice_program_settings["provider"] == provider
    assert combo.property("currentIndex") == index

choose(0, "sogou")
switch = find(window, "voiceProgramAutoStartSwitch")
assert not switch.property("visible")
assert controller.voiceProgramLaunchOnBridgeStart
for provider_index, provider in ((0, "sogou"), (1, "wetype"), (2, "doubao_ime")):
    choose(provider_index, provider)
    section = find(window, "voiceProgramSection")
    row = find(window, "voiceHotkeyRow")
    assert row.property("visible") and row.height() > 0
    assert row.y() + row.height() <= section.height() + 1
    for name in ("refreshVoiceHotkeyButton", "recordVoiceHotkeyButton"):
        assert find(window, name).property("visible"), (provider, name)
choose(1, "wetype")
assert not switch.property("visible")
choose(2, "doubao_ime")
assert not switch.property("visible")
choose(3, "custom")
assert switch.property("visible")
switch.setProperty("checked", False)
assert QMetaObject.invokeMethod(switch, "toggled")
assert not controller.voiceProgramLaunchOnBridgeStart
field = find(window, "voiceProgramCustomPathField")
assert not field.property("readOnly")
path = m.config.config_root() / "voice test.exe"
path.parent.mkdir(parents=True, exist_ok=True)
path.touch()
field.setProperty("text", '"' + str(path) + '"')
assert QMetaObject.invokeMethod(field, "editingFinished")
render(window, app)
assert controller.voiceProgramCustomPath == str(path)
field.setProperty("text", str(path) + " --argument")
assert QMetaObject.invokeMethod(field, "editingFinished")
render(window, app)
assert controller.voiceProgramCustomPath == str(path)
assert field.property("text") == str(path)
image_dir = os.environ.get("VOICE_SELECTION_SCREENSHOTS")
if image_dir:
    image_path = Path(image_dir)
    image_path.mkdir(parents=True, exist_ok=True)
    window.grabWindow().save(str(image_path / (os.environ["VOICE_SELECTION_STYLE"] + ".png")))
choose(0, "sogou")
assert controller.voiceProgramLaunchOnBridgeStart
choose(3, "custom")
assert not controller.voiceProgramLaunchOnBridgeStart

controller._config["remote_selection"] = {"schema": 1, "active": "a" * 64,
    "devices": [{"key": "a" * 64, "profile": "chromecast-remote"}]}
controller.remoteSelectionChanged.emit()
controller.selectedDeviceChanged.emit()
controller._voice_program_status_code = "running"
controller.voiceProgramStatusCodeChanged.emit()
render(window, app)
assert not rc003_recording_row.property("visible")
assert find(window, "remoteRecordingModeRow").property("visible")
row = find(window, "voiceProgramSelectionRow")
assert row.property("stateText") == "尚未接通"
assert "已接通微信、搜狗和豆包" in row.property("descriptionText")
editor_bounds = geometry(find(window, "voiceProgramSelectionRow_editorColumn"))
permission_bounds = geometry(find(window, "voiceProgramElevatedCheckBox"))
note_bounds = geometry(find(window, "voiceProgramSelectionRow_descriptionLabel"))
assert permission_bounds["right"] <= editor_bounds["right"] + 1, (editor_bounds, permission_bounds)
assert note_bounds["x"] >= permission_bounds["right"], (note_bounds, permission_bounds)
if image_dir:
    window.grabWindow().save(str(image_path / (os.environ["VOICE_SELECTION_STYLE"] + "-unsupported.png")))
# The current page remains usable at minimum width; horizontal overflow must
# not hide the provider, permission option or custom path behind other columns.
for name in ("voiceProgramCombo", "voiceProgramElevatedCheckBox"):
    item = find(window, name)
    bounds = geometry(item)
    assert bounds["x"] >= 0 and bounds["right"] <= window.property("width"), (name, bounds)
choose(3, "custom")
note_bounds = geometry(find(window, "voiceProgramSelectionRow_descriptionLabel"))
permission_bounds = geometry(find(window, "voiceProgramElevatedCheckBox"))
assert note_bounds["x"] >= permission_bounds["right"], (note_bounds, permission_bounds)
if image_dir:
    window.grabWindow().save(str(image_path / (os.environ["VOICE_SELECTION_STYLE"] + "-custom-unsupported.png")))
choose(1, "wetype")
assert row.property("stateText") != "尚未接通"
choose(2, "doubao_ime")
assert controller.remoteRecordingModeIndex == 0
assert row.property("stateText") != "尚未接通"
controller.setRemoteRecordingPreferences(1, controller.remoteRecordingLimitIndex)
render(window, app)
assert row.property("stateText") != "尚未接通"
assert row.property("descriptionText") == ""
assert find(window, "remoteRecordingModeRow").property("descriptionText") == "按一下开始，再按结束；到时或操作键盘也会结束"
assert find(window, "refreshVoiceHotkeyButton").property("enabled")
assert find(window, "recordVoiceHotkeyButton").property("enabled")

# Returning to Xiaomi restores the read-only row without stealing focus or
# causing a configuration write/restart as a side effect of rendering it.
config_path = m.config.config_path(m.config.config_root())
config_before = config_path.read_bytes()
controller._config["remote_selection"]["devices"][0]["profile"] = "xiaomi-rc003"
controller.remoteSelectionChanged.emit()
controller.selectedDeviceChanged.emit()
with mock.patch.object(m.config, "save_config_and_load", wraps=m.config.save_config_and_load) as save, \
        mock.patch.object(controller, "_start_bridge_process") as restart:
    render(window, app)
    assert rc003_recording_row.property("visible")
    assert not find(window, "remoteRecordingModeRow").property("visible")
    assert not rc003_recording_row.property("editorColumnVisible")
    assert not rc003_recording_row.property("activeFocus")
    save.assert_not_called()
    restart.assert_not_called()
assert config_path.read_bytes() == config_before
print(json.dumps({"warnings": [str(w) for w in warnings]}))
window.hide()
m._shutdown_diagnostics_workers()
'''


class VoiceProgramSelectionQmlTests(unittest.TestCase):
    def test_real_selection_and_custom_path_controls(self):
        for style in ("Basic", "Fusion", "FluentWinUI3"):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as root:
                env = dict(os.environ, LOCALAPPDATA=root, QT_QPA_PLATFORM="offscreen",
                           QT_SCALE_FACTOR="1.5", VOICE_SELECTION_STYLE=style)
                result = subprocess.run([sys.executable, "-B", "-c", _PROBE],
                                        env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])["warnings"], [])
