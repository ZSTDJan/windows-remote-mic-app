"""Zhipu AutoGLM (智谱AI输入法) discovery, shortcut sync and Chromecast host wiring."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.test_chromecast_voice as fixtures

from ovb_rc003 import chromecast_host_activity as activity
from ovb_rc003 import chromecast_voice_host as host_module
from ovb_rc003 import config
from ovb_rc003 import voice_hotkey_sync_windows as sync
from ovb_rc003 import voice_playback_session_windows as playback
from ovb_rc003 import voice_program_manager as programs

MARKED_BACKEND = host_module._VOICE_HOTKEY_BACKEND_MARKED


def active_capture(pid=88, instance="autoglm-capture"):
    return playback.CaptureSession("endpoint", instance, pid, 1)


def write_store(root, *, hold="RightCtrl", toggle=""):
    store = Path(root) / "智谱AI输入法"
    store.mkdir(parents=True, exist_ok=True)
    (store / "settings.json").write_text(
        json.dumps({"keyMap": {"recordToggle": toggle, "recordHold": hold}}),
        encoding="utf-8",
    )


class AutoGlmProviderTests(unittest.TestCase):
    def test_provider_is_selectable_and_keeps_legacy_indices(self):
        self.assertIn("autoglm", programs.VOICE_PROGRAM_SELECTABLE_PROVIDER_ORDER)
        self.assertEqual(programs.provider_index("custom"), 4)
        self.assertEqual(programs.provider_index("bageshuo"), 6)
        self.assertEqual(programs.provider_index("autoglm"), 7)
        self.assertEqual(
            programs.provider_id_for_index(programs.provider_index("autoglm")),
            "autoglm",
        )
        self.assertEqual(
            programs.VOICE_PROGRAM_PROVIDER_NAMES["autoglm"], "智谱AI输入法"
        )

    def test_discovery_prefers_running_process_then_per_user_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary) / "Programs" / "AutoGLM" / "AutoGLM.exe"
            installed.parent.mkdir(parents=True)
            installed.touch()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
                resolved = programs.resolve_voice_program(
                    {"provider": "autoglm"},
                    platform="win32",
                    process_iter=lambda: (),
                )
                self.assertEqual(resolved.executable, installed)
                self.assertEqual(resolved.source, "discovered")
                target = programs.resolve_voice_program_settings_target(
                    {"provider": "autoglm"},
                    platform="win32",
                    process_iter=lambda: (),
                )
                self.assertEqual(target.target, str(installed))

    def test_discovery_returns_nothing_without_an_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
                resolved = programs.resolve_voice_program(
                    {"provider": "autoglm"},
                    platform="win32",
                    process_iter=lambda: (),
                )
                self.assertIsNone(resolved.executable)
                self.assertEqual(resolved.source, "missing")

    def test_running_process_wins_over_the_install_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary) / "Programs" / "AutoGLM" / "AutoGLM.exe"
            installed.parent.mkdir(parents=True)
            installed.touch()
            running = Path(temporary) / "elsewhere" / "AutoGLM.exe"
            running.parent.mkdir(parents=True)
            running.touch()
            process = programs.ProcessInfo(
                pid=5150, name="AutoGLM.exe", executable=running
            )
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
                resolved = programs.resolve_voice_program(
                    {"provider": "autoglm"},
                    platform="win32",
                    process_iter=lambda: (process,),
                )
                self.assertEqual(resolved.executable, running)

    def test_diagnostic_process_enumeration_names_every_named_process(self):
        # Electron spawns several identically named processes; all of them are
        # candidates and the capture reader filters by actual session ownership.
        processes = (
            programs.ProcessInfo(pid=900, name="AutoGLM.exe"),
            programs.ProcessInfo(pid=901, name="AutoGLM.exe"),
        )
        with mock.patch.object(
            programs, "_iter_windows_processes", return_value=processes
        ):
            found = programs.diagnostic_voice_processes()
        self.assertEqual(
            found,
            (("autoglm", 900, "AutoGLM.exe"), ("autoglm", 901, "AutoGLM.exe")),
        )

    def test_capture_reader_only_uses_the_autoglm_processes(self):
        processes = (
            ("autoglm", 900, "AutoGLM.exe"),
            ("bageshuo", 901, "bageshuo.exe"),
        )
        with mock.patch.object(
            programs, "diagnostic_voice_processes", return_value=processes
        ), mock.patch.object(
            activity.audio, "read_capture_sessions", return_value=()
        ) as reader:
            activity.read_autoglm_capture()
        reader.assert_called_once_with({900})


class AutoGlmShortcutTests(unittest.TestCase):
    def test_hold_shortcut_is_read_and_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            write_store(temporary, hold="RightCtrl")
            result = sync.read_provider_hotkey("autoglm", appdata=Path(temporary))
            self.assertTrue(result.ok, result)
            self.assertEqual(result.hotkey, "rctrl")

    def test_hold_and_toggle_are_independent_shortcuts(self):
        with tempfile.TemporaryDirectory() as temporary:
            write_store(temporary, hold="RightCtrl", toggle="RightCtrl+RightShift")
            hold = sync.read_provider_hotkey("autoglm", appdata=Path(temporary))
            toggle = sync.read_provider_hotkey(
                "autoglm", appdata=Path(temporary), trigger="toggle"
            )
            self.assertTrue(hold.ok, hold)
            self.assertEqual(hold.hotkey, "rctrl")
            self.assertTrue(toggle.ok, toggle)
            self.assertEqual(toggle.hotkey, "rctrl+rshift")

    def test_unset_toggle_reports_not_configured(self):
        with tempfile.TemporaryDirectory() as temporary:
            write_store(temporary, hold="RightCtrl", toggle="")
            result = sync.read_provider_hotkey(
                "autoglm", appdata=Path(temporary), trigger="toggle"
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "not_configured")
            self.assertIn("点按录音", result.message)

    def test_unset_hold_reports_not_configured(self):
        with tempfile.TemporaryDirectory() as temporary:
            write_store(temporary, hold="", toggle="")
            result = sync.read_provider_hotkey("autoglm", appdata=Path(temporary))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "not_configured")
            self.assertIn("按住录音", result.message)

    def test_missing_store_reports_not_found(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = sync.read_provider_hotkey("autoglm", appdata=Path(temporary))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "not_found")

    def test_unreadable_store_reports_unreadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "智谱AI输入法"
            store.mkdir(parents=True)
            (store / "settings.json").write_text("{not json", encoding="utf-8")
            result = sync.read_provider_hotkey("autoglm", appdata=Path(temporary))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "unreadable")

    def test_unknown_key_name_reports_invalid_hotkey(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "智谱AI输入法"
            store.mkdir(parents=True)
            (store / "settings.json").write_text(
                json.dumps({"keyMap": {"recordHold": "NotAKey"}}), encoding="utf-8"
            )
            result = sync.read_provider_hotkey("autoglm", appdata=Path(temporary))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "invalid_hotkey")

    def test_default_shortcut_is_right_control(self):
        self.assertEqual(sync.default_hotkey("autoglm"), "rctrl")

    def test_local_save_keeps_the_third_party_shortcut_manual(self):
        result = sync.sync_provider_hotkey("autoglm", "rctrl")
        self.assertTrue(result.ok, result)
        self.assertEqual(result.code, "local_only")
        self.assertIn("按住录音", result.message)
        toggle = sync.sync_provider_hotkey("autoglm", "rctrl", trigger="toggle")
        self.assertTrue(toggle.ok, toggle)
        self.assertIn("点按录音", toggle.message)

    def test_toggle_shortcut_can_be_persisted(self):
        document = config.default_config()
        document["voice_program"] = {"provider": programs.VOICE_PROGRAM_AUTOGLM}
        config.set_voice_hotkey_for_provider(
            document, "autoglm", "rctrl+rshift", trigger="toggle"
        )
        self.assertEqual(
            config.voice_hotkey_for_provider(document, "autoglm", trigger="toggle"),
            "rctrl+rshift",
        )

    def test_toggle_is_available_in_toggle_recording_mode(self):
        self.assertEqual(
            config.voice_hotkey_trigger_for_settings(
                {
                    "voice_program": {"provider": "autoglm"},
                    "remote_recording_mode": "toggle",
                },
                "autoglm",
            ),
            "toggle",
        )


class AutoGlmHostTests(unittest.TestCase):
    def setUp(self):
        fixtures.HostTests.setUp(self)
        self.owner._config.update(
            voice_program={"provider": "autoglm"},
            voice_hotkeys_by_provider={
                "autoglm": {
                    "hold": "rctrl",
                    "source": "manual",
                    "toggle": "rctrl+rshift",
                    "toggle_source": "manual",
                }
            },
        )
        self.owner._voice_shortcut.hotkey = SimpleNamespace(modifiers=(), key="rctrl")
        self.owner._configured_voice_hotkey_backend.return_value = MARKED_BACKEND
        patcher = mock.patch.object(host_module, "read_autoglm_capture", return_value=())
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
        self.tap.assert_called_once_with(("rctrl", "rshift"))
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

    def test_toggle_without_its_own_shortcut_never_falls_back_to_hold(self):
        # The recordHold key only spans one key-down/key-up pair, so reusing it
        # for a toggle tap would open and close dictation inside one frame and
        # drop the audio under AutoGLM's 1 s upload threshold.
        entry = self.owner._config["voice_hotkeys_by_provider"]["autoglm"]
        entry["toggle"] = ""
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.tap.assert_not_called()
        self.owner._voice_shortcut.apply.assert_not_called()
        self.host.client.voice_host.assert_called_with(1, "failed")

    def test_hold_mode_uses_the_key_edge_path_not_a_toggle_tap(self):
        # AutoGLM really has a hold-to-talk key, so the device hold gesture must
        # stay on the ordinary key-down/key-up path.
        self.owner._config["remote_recording_mode"] = "hold"
        self.host._start_host(1)
        self.tap.assert_not_called()
        self.owner._voice_shortcut.apply.assert_called_once()

    def test_hold_mode_reports_failure_when_tracking_is_unavailable(self):
        self.owner._config["remote_recording_mode"] = "hold"
        self.owner._ensure_voice_key_physicalizer_for_hotkey.return_value = False
        self.host._start_host(1)
        self.owner._voice_shortcut.apply.assert_not_called()
        self.host.client.voice_host.assert_called_with(1, "failed")

    def test_time_limit_stops_only_a_still_active_capture_session(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.capture.return_value = (active_capture(),)
        self.host._poll_host()
        self.host._handle({"event": "state", "attempt": 1, "data": "recording"})
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "time_limit"})
        self.assertEqual(self.tap.call_count, 2)

    def test_start_timeout_still_closes_a_running_capture(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.capture.return_value = (active_capture(),)
        self.host._handle({"event": "host_stop", "attempt": 1, "data": ""})
        self.assertEqual(self.tap.call_count, 2)

    def test_idle_capture_never_gets_a_closing_press(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "released"})
        self.tap.assert_called_once_with(("rctrl", "rshift"))

    def test_unreadable_capture_state_skips_the_closing_press(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.capture.side_effect = OSError("voice process enumeration is incomplete")
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "released"})
        self.tap.assert_called_once_with(("rctrl", "rshift"))

    def test_closing_press_is_sent_at_most_once_per_session(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.capture.return_value = (active_capture(),)
        stop = {"event": "host_stop", "attempt": 1, "data": "released"}
        self.host._handle(stop)
        self.host._handle(stop)
        self.assertEqual(self.tap.call_count, 2)

    def test_hold_release_never_sends_a_closing_toggle(self):
        # A hold attempt owns no ToggleShortcut; the release already delivered
        # the key-up, so a closing press would start a NEW dictation.
        self.owner._config["remote_recording_mode"] = "hold"
        self.host._start_host(1)
        self.capture.return_value = (active_capture(),)
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "released"})
        self.tap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
