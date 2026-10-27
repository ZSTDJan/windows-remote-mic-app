"""NetEase Bageshuo (网易叭哥说) discovery, shortcut sync and Chromecast host wiring."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.test_chromecast_voice as fixtures

from ovb_rc003 import chromecast_voice_host as host_module
from ovb_rc003 import config
from ovb_rc003 import voice_controller
from ovb_rc003 import voice_hotkey_sync_windows as sync
from ovb_rc003 import voice_playback_session_windows as playback
from ovb_rc003 import voice_program_manager as programs

MARKED_BACKEND = host_module._VOICE_HOTKEY_BACKEND_MARKED


def active_capture(pid=77, instance="bageshuo-capture"):
    return playback.CaptureSession("endpoint", instance, pid, 1)


def write_store(root, shortcut):
    store = Path(root) / "com.bageshuo"
    store.mkdir(parents=True, exist_ok=True)
    (store / "bageshuo-store.json").write_text(
        json.dumps({"settings": {"polishHotkey": shortcut}}),
        encoding="utf-8",
    )


class BageshuoProviderTests(unittest.TestCase):
    def test_provider_is_selectable_and_keeps_legacy_indices(self):
        self.assertIn("bageshuo", programs.VOICE_PROGRAM_SELECTABLE_PROVIDER_ORDER)
        self.assertEqual(programs.provider_index("custom"), 4)
        self.assertEqual(programs.provider_index("chatterfly"), 5)
        self.assertEqual(programs.provider_index("bageshuo"), 6)
        self.assertEqual(
            programs.provider_id_for_index(programs.provider_index("bageshuo")),
            "bageshuo",
        )
        self.assertEqual(
            programs.VOICE_PROGRAM_PROVIDER_NAMES["bageshuo"], "网易叭哥说"
        )

    def test_discovery_prefers_running_process_then_per_user_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary) / "bageshuo" / "bageshuo.exe"
            installed.parent.mkdir(parents=True)
            installed.touch()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
                resolved = programs.resolve_voice_program(
                    {"provider": "bageshuo"},
                    platform="win32",
                    process_iter=lambda: (),
                )
                self.assertEqual(resolved.executable, installed)
                self.assertEqual(resolved.source, "discovered")
                target = programs.resolve_voice_program_settings_target(
                    {"provider": "bageshuo"},
                    platform="win32",
                    process_iter=lambda: (),
                )
                self.assertEqual(target.target, str(installed))

    def test_discovery_returns_nothing_without_an_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
                resolved = programs.resolve_voice_program(
                    {"provider": "bageshuo"},
                    platform="win32",
                    process_iter=lambda: (),
                )
                self.assertIsNone(resolved.executable)
                self.assertEqual(resolved.source, "missing")

    def test_diagnostic_process_enumeration_names_the_provider(self):
        process = programs.ProcessInfo(pid=4242, name="bageshuo.exe")
        names = {
            "wetype": programs.VOICE_PROGRAM_WETYPE,
            "bageshuo.exe": programs.VOICE_PROGRAM_BAGESHUO,
        }
        with mock.patch.object(
            programs, "_iter_windows_processes", return_value=(process,)
        ):
            found = programs.diagnostic_voice_processes()
        self.assertEqual(found, (("bageshuo", 4242, "bageshuo.exe"),))
        self.assertIn("bageshuo.exe", names)


class BageshuoShortcutTests(unittest.TestCase):
    def test_store_shortcut_is_read_and_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            write_store(temporary, "RightAlt")
            result = sync.read_provider_hotkey("bageshuo", appdata=Path(temporary))
            self.assertTrue(result.ok, result)
            self.assertEqual(result.hotkey, "ralt")

    def test_combined_store_shortcut_is_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            write_store(temporary, "RightAlt+RightShift")
            result = sync.read_provider_hotkey("bageshuo", appdata=Path(temporary))
            self.assertTrue(result.ok, result)
            self.assertEqual(result.hotkey, "ralt+rshift")

    def test_toggle_read_returns_the_same_shortcut(self):
        with tempfile.TemporaryDirectory() as temporary:
            write_store(temporary, "RightAlt+Space")
            result = sync.read_provider_hotkey(
                "bageshuo", appdata=Path(temporary), trigger="toggle"
            )
            self.assertTrue(result.ok, result)
            self.assertEqual(result.hotkey, "ralt+space")

    def test_missing_store_reports_not_found(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = sync.read_provider_hotkey("bageshuo", appdata=Path(temporary))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "not_found")

    def test_unreadable_store_reports_unreadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "com.bageshuo"
            store.mkdir(parents=True)
            (store / "bageshuo-store.json").write_text("{not json", encoding="utf-8")
            result = sync.read_provider_hotkey("bageshuo", appdata=Path(temporary))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "unreadable")

    def test_default_shortcut_is_right_alt(self):
        self.assertEqual(sync.default_hotkey("bageshuo"), "ralt")

    def test_legacy_scalar_is_not_inherited(self):
        document = config.default_config()
        document["voice_program"] = {"provider": programs.VOICE_PROGRAM_BAGESHUO}
        document["voice_hotkey"] = "f8"
        document.pop("voice_hotkeys_by_provider", None)
        config._normalize_voice_hotkey(document)
        self.assertEqual(config.voice_hotkey_for_provider(document, "bageshuo"), "ralt")

    def test_toggle_shortcut_can_be_persisted(self):
        document = config.default_config()
        document["voice_program"] = {"provider": programs.VOICE_PROGRAM_BAGESHUO}
        config.set_voice_hotkey_for_provider(
            document, "bageshuo", "ralt", trigger="toggle"
        )
        self.assertEqual(
            config.voice_hotkey_for_provider(document, "bageshuo", trigger="toggle"),
            "ralt",
        )

    def test_toggle_is_available_in_toggle_recording_mode(self):
        self.assertEqual(
            config.voice_hotkey_trigger_for_settings(
                {
                    "voice_program": {"provider": "bageshuo"},
                    "remote_recording_mode": "toggle",
                },
                "bageshuo",
            ),
            "toggle",
        )


class BageshuoHostTests(unittest.TestCase):
    def setUp(self):
        fixtures.HostTests.setUp(self)
        self.owner._config.update(
            voice_program={"provider": "bageshuo"},
            voice_hotkeys_by_provider={
                "bageshuo": {
                    "hold": "ralt",
                    "source": "manual",
                    "toggle": "ralt",
                    "toggle_source": "manual",
                }
            },
        )
        self.owner._voice_shortcut.hotkey = SimpleNamespace(modifiers=(), key="ralt")
        self.owner._configured_voice_hotkey_backend.return_value = MARKED_BACKEND
        patcher = mock.patch.object(host_module, "read_bageshuo_capture", return_value=())
        self.capture = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch(
            "ovb_rc003.chromecast_wetype_toggle.win32_input.send_voice_key_combo_tap"
        )
        self.tap = patcher.start()
        self.addCleanup(patcher.stop)

    def test_toggle_waits_for_a_new_capture_session_and_stops_once(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.tap.assert_called_once_with(("ralt",))
        self.host.client.voice_host.assert_not_called()
        self.capture.return_value = (active_capture(),)
        self.host._poll_host()
        self.host.client.voice_host.assert_called_once_with(1, "ready")
        self.host._handle({"event": "state", "attempt": 1, "data": "recording"})
        stop = {"event": "host_stop", "attempt": 1, "data": "second_press"}
        self.host._handle(stop)
        self.host._handle(stop)
        self.assertEqual(self.tap.call_count, 2)
        self.owner._voice_shortcut.apply.assert_not_called()

    def test_toggle_rejects_an_already_active_capture_session(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.capture.return_value = (active_capture(),)
        self.host._start_host(1)
        self.tap.assert_not_called()
        self.host.client.voice_host.assert_called_with(1, "failed")

    def test_toggle_without_any_shortcut_never_sends(self):
        entry = self.owner._config["voice_hotkeys_by_provider"]["bageshuo"]
        entry["hold"] = ""
        entry["toggle"] = ""
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.tap.assert_not_called()
        self.owner._voice_shortcut.apply.assert_not_called()
        self.host.client.voice_host.assert_called_with(1, "failed")

    def test_device_hold_mode_still_uses_the_toggle_path(self):
        # 网易叭哥说 has no hold-to-talk, so the device-level recording mode
        # must never put it on the key-edge path.
        self.owner._config["remote_recording_mode"] = "hold"
        self.host._start_host(1)
        self.tap.assert_called_once_with(("ralt",))
        self.owner._voice_shortcut.apply.assert_not_called()

    def test_toggle_falls_back_to_the_hold_column_shortcut(self):
        # 网易叭哥说 exposes a single shortcut, so a value recorded only under
        # the hold column must still drive the toggle.
        entry = self.owner._config["voice_hotkeys_by_provider"]["bageshuo"]
        entry["toggle"] = ""
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.tap.assert_called_once_with(("ralt",))

    def test_toggle_reports_failure_when_right_alt_tracking_is_unavailable(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.owner._ensure_voice_key_physicalizer_for_hotkey.return_value = False
        self.host._start_host(1)
        self.tap.assert_not_called()
        self.host.client.voice_host.assert_called_with(1, "failed")

    def test_toggle_prepares_right_alt_tracking_before_sending(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.owner._ensure_voice_key_physicalizer_for_hotkey.assert_called_once_with(
            ("ralt",), MARKED_BACKEND
        )
        self.tap.assert_called_once_with(("ralt",))

    def test_time_limit_stops_only_a_still_active_capture_session(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.capture.return_value = (active_capture(),)
        self.host._poll_host()
        self.host._handle({"event": "state", "attempt": 1, "data": "recording"})
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "time_limit"})
        self.assertEqual(self.tap.call_count, 2)

    def test_no_capture_times_out_without_a_compensating_toggle(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        with mock.patch.object(host_module.time, "monotonic", return_value=10) as clock:
            self.host._start_host(1)
            clock.return_value = 19.1
            self.host._poll_host()
        self.host.client.voice_host.assert_any_call(1, "failed")
        self.tap.assert_called_once_with(("ralt",))

    def test_start_timeout_still_closes_a_running_capture(self):
        # Holding the physical button past START_TIMEOUT in toggle mode never
        # activates the software stream, so that lifecycle ends with an empty
        # reason instead of device_ended/time_limit. The dictation is still
        # running and must be closed: otherwise the next attempt is rejected as
        # "already recording" and the user cannot recover from the app at all.
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.capture.return_value = (active_capture(),)
        self.host._handle({"event": "host_stop", "attempt": 1, "data": ""})
        self.assertEqual(self.tap.call_count, 2)

    def test_hold_release_closes_a_running_capture(self):
        # In hold mode the release ends the lifecycle with "released", which is
        # not a device-reported end either. 网易叭哥说 exposes no hold API, so
        # this closing press is what makes the emulated push-to-talk work.
        self.owner._config["remote_recording_mode"] = "hold"
        self.host._start_host(1)
        self.capture.return_value = (active_capture(),)
        self.host._poll_host()
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "released"})
        self.assertEqual(self.tap.call_count, 2)

    def test_release_before_readiness_still_closes_a_running_capture(self):
        # A short tap can end the lifecycle before this host ever confirmed
        # readiness, so self.confirmed alone would refuse the closing press
        # even though the shortcut was already delivered and dictation runs.
        self.owner._config["remote_recording_mode"] = "hold"
        self.host._start_host(1)
        self.assertFalse(self.host.confirmed)
        self.capture.return_value = (active_capture(),)
        self.host._handle({"event": "host_stop", "attempt": 1, "data": ""})
        self.assertEqual(self.tap.call_count, 2)

    def test_idle_capture_never_gets_a_closing_press(self):
        # Sending the toggle while 网易叭哥说 is idle would START a dictation
        # instead of ending one, so an unobserved capture must stay silent.
        self.owner._config["remote_recording_mode"] = "hold"
        self.host._start_host(1)
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "released"})
        self.tap.assert_called_once_with(("ralt",))

    def test_unreadable_capture_state_skips_the_closing_press(self):
        self.owner._config["remote_recording_mode"] = "hold"
        self.host._start_host(1)
        self.capture.side_effect = OSError("voice process enumeration is incomplete")
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "released"})
        self.tap.assert_called_once_with(("ralt",))

    def test_closing_press_is_sent_at_most_once_per_session(self):
        self.owner._config["remote_recording_mode"] = "hold"
        self.host._start_host(1)
        self.capture.return_value = (active_capture(),)
        stop = {"event": "host_stop", "attempt": 1, "data": "released"}
        self.host._handle(stop)
        self.host._handle(stop)
        self.assertEqual(self.tap.call_count, 2)

if __name__ == "__main__":
    unittest.main()
