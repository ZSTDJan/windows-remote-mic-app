"""Exercise the real device/voice pages with isolated HID upgrade states."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests.test_voice_program_qml import _PROBE as _VOICE_PAGE_PROBE


# Reuse the existing offscreen application setup and its hardware/host stubs.
# Run only setup, before that probe begins its voice-program interactions.
_SETUP = _VOICE_PAGE_PROBE.split('tab_bar = find(window, "tabBar")', 1)[0]
_SETUP = _SETUP.replace(
    "engine = QQmlApplicationEngine()",
    "m.windows_diagnostics.run_diagnostics = lambda **_kwargs: None\n"
    "from pathlib import Path\n"
    "from PySide6.QtGui import QFontDatabase\n"
    "font_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'msyh.ttc'\n"
    "if font_path.is_file():\n"
    "    QFontDatabase.addApplicationFont(str(font_path))\n"
    "engine = QQmlApplicationEngine()",
)
_PROBE = _SETUP + r'''
from ovb_rc003 import bridge_runtime_status, frida_compat, hid_elevation_windows

find(window, "bridgeStatusRefreshTimer").setProperty("running", False)
tab_bar = find(window, "tabBar")
tab_bar.setProperty("currentIndex", 2)
render(window, app)
wait_for_voice_hotkey_idle(controller, app)
deadline = time.monotonic() + 3.0
while diagnostics.isRefreshing and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
assert not diagnostics.isRefreshing
controller._config["remote_selection"] = {"schema": 1, "active": "a" * 64,
    "devices": [{"key": "a" * 64, "profile": "xiaomi-rc003"}]}
controller.remoteSelectionChanged.emit()
controller._bridge_running = True
controller.bridgeRunningChanged.emit()
controller._set_bridge_connected(True)
controller._set_bridge_connection_state("connected")
device_page = find(window, "deviceScroll").parent()
voice_page = find(window, "voiceScroll").parent()
root_path = m.config.config_root()

def input_state(hid_state):
    bridge_runtime_status.publish_status(
        root_path, bridge_runtime_status.BridgeConnectionState.CONNECTED,
        pid=os.getpid(), raw_input_state="ready", hid_tap_state=hid_state,
        voice_key_physicalizer_state="ready",
    )
    status = bridge_runtime_status.read_status(root_path)
    controller._set_bridge_input_states(status)
    deadline = time.monotonic() + 3.0
    while diagnostics.isRefreshing and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert not diagnostics.isRefreshing
    assert not controller._runtime_status_requires_restart(status)

input_state(frida_compat.HidTapState.RESTART_REQUIRED.value)
assert device_page.buttonReceiverStateCode() == "restart_computer"
assert device_page.buttonReceiverStateText() == "请重启电脑一次"
assert "旧按键组件未释放" in find(window, "buttonReceiverRow").property("descriptionText")
assert "重启后重新打开程序" in find(window, "buttonReceiverRow").property("descriptionText")
assert not device_page.bridgeNeedsRestartAction()

# A stale restart-service recommendation must not hide the required host reload.
controller._set_bridge_restart_recommended(True)
assert not device_page.bridgeNeedsRestartAction()
controller._set_bridge_restart_recommended(False)

# Ordinary-permission upgrades must replace the installed helper first.
controller._hid_helper_frozen_distribution = True
controller._hid_helper_process_elevated = False
controller._hid_helper_portable_distribution = False
controller._hid_helper_can_self_elevate = True
controller._hid_helper_state = hid_elevation_windows.HidHelperState(
    False, "protected_helper_outdated"
)
controller.hidHelperStateChanged.emit()
assert controller.hidHelperRepairVisible
assert device_page.buttonReceiverStateCode() == "permission"
controller._hid_helper_portable_distribution = True
controller.hidHelperStateChanged.emit()
assert device_page.buttonReceiverStateCode() == "disabled"
controller._hid_helper_state = hid_elevation_windows.HidHelperState(True, "ready")
controller.hidHelperStateChanged.emit()
assert device_page.buttonReceiverStateCode() == "restart_computer"

# An unresolved source stays explicit without offering service/computer restart.
input_state(frida_compat.HidTapState.SHARED_HOST.value)
assert device_page.buttonReceiverStateText() == "按键来源待区分"
assert not device_page.bridgeNeedsRestartAction()

# After a compatible component has loaded, first input and normal operation
# retain the existing UI states.
input_state(frida_compat.HidTapState.UNAVAILABLE.value)
assert device_page.buttonReceiverStateCode() == "component_unavailable"
assert device_page.buttonReceiverStateText() == "按键组件不完整"
assert "缺失或校验失败" in find(window, "buttonReceiverRow").property("descriptionText")
assert not device_page.bridgeNeedsRestartAction()
assert not device_page.bridgeActionVisible()
assert device_page.bridgeStateText() == "已连接"
controller._set_bridge_restart_recommended(True)
assert not device_page.bridgeNeedsRestartAction()
assert not device_page.bridgeActionVisible()
controller._set_bridge_restart_recommended(False)

# A transient interception failure still offers the existing service restart.
input_state(frida_compat.HidTapState.UNHEALTHY.value)
assert device_page.buttonReceiverStateCode() == "original_only"
assert device_page.bridgeNeedsRestartAction()
assert device_page.bridgeActionVisible()
assert device_page.bridgeActionText() == "重启服务"

input_state(frida_compat.HidTapState.ATTACHED_WAITING_IO.value)
assert device_page.buttonReceiverStateText() == "正常"
input_state(frida_compat.HidTapState.READY.value)
assert device_page.buttonReceiverStateText() == "正常"

# Chromecast does not consume the Xiaomi component or its stale failure state.
controller._config["remote_selection"]["devices"][0]["profile"] = "chromecast-remote"
controller.remoteSelectionChanged.emit()
assert not controller.isRc003Device
bridge_runtime_status.publish_status(
    root_path, bridge_runtime_status.BridgeConnectionState.CONNECTED,
    pid=os.getpid(), raw_input_state="chromecast_ready",
    hid_tap_state=frida_compat.HidTapState.UNAVAILABLE.value,
)
controller._set_bridge_input_states(bridge_runtime_status.read_status(root_path))
assert device_page.buttonReceiverStateText() == "正常"
assert not device_page.bridgeNeedsRestartAction()
controller._config["remote_selection"]["devices"][0]["profile"] = "xiaomi-rc003"
controller.remoteSelectionChanged.emit()

input_state(frida_compat.HidTapState.RESTART_REQUIRED.value)
render(window, app)
screenshot_dir = os.environ.get("HID_UPGRADE_SCREENSHOT_DIR")
if screenshot_dir:
    tab_bar.setProperty("currentIndex", 0)
    render(window, app)
    window.grabWindow().save(os.path.join(screenshot_dir, "hid-upgrade-device.png"))
    find(window, "tabBar").setProperty("currentIndex", 2)
    render(window, app)
    window.grabWindow().save(os.path.join(screenshot_dir, "hid-upgrade-voice.png"))
print(json.dumps({"warnings": [str(warning) for warning in warnings]}))
window.hide()
m._shutdown_diagnostics_workers()
'''


class HidRuntimeUpgradeQmlTests(unittest.TestCase):
    def test_upgrade_and_host_reload_states_in_real_pages(self):
        with tempfile.TemporaryDirectory() as local_app_data:
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            env["LOCALAPPDATA"] = local_app_data
            result = subprocess.run(
                [sys.executable, "-B", "-c", _PROBE],
                env=env, capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data["warnings"], [])


if __name__ == "__main__":
    unittest.main()
