import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import config, voice_program_manager as programs
from ovb_rc003 import chromecast_voice_host as host_module
from ovb_rc003 import voice_controller
import tests.test_chromecast_voice as fixtures


def visible_voice_window(pid=41):
    return SimpleNamespace(identity=("ChatterflyVoiceUi", 0, pid), state=1)


class ChatterflyConfigurationTests(unittest.TestCase):
    def test_new_provider_does_not_inherit_previous_shortcut(self):
        old = config.default_config()
        old["voice_program"] = {"provider": programs.VOICE_PROGRAM_CHATTERFLY}
        old["voice_hotkey"] = "ralt"
        old.pop("voice_hotkeys_by_provider", None)
        config._normalize_voice_hotkey(old)
        self.assertEqual(config.voice_hotkey_for_provider(old, "chatterfly"), "")
        self.assertEqual(old["voice_hotkey"], "")

    def test_discovery_and_settings_follow_versioned_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Chatterfly"
            version = root / "1.0.0.5516"
            version.mkdir(parents=True)
            (version / "ChatterflyCloud.exe").touch()
            (version / "FySetting.exe").touch()
            launcher = root / "ChatterflyExe" / "ChatterflyExe.exe"
            launcher.parent.mkdir()
            launcher.touch()
            reader = lambda: (str(root),)
            process_iter = lambda: ()
            resolved = programs.resolve_voice_program(
                {"provider": "chatterfly"}, platform="win32",
                process_iter=process_iter, chatterfly_install_value_reader=reader,
            )
            self.assertEqual(resolved.executable, version / "ChatterflyCloud.exe")
            target = programs.resolve_voice_program_settings_target(
                {"provider": "chatterfly"}, platform="win32",
                process_iter=process_iter, chatterfly_install_value_reader=reader,
            )
            self.assertEqual(target.target, str(launcher))
            self.assertEqual(target.arguments, f'"{version / "FySetting.exe"}" --page=settings')


class ChatterflyHostTests(unittest.TestCase):
    def setUp(self):
        fixtures.HostTests.setUp(self)
        self.owner._config.update(
            voice_program={"provider": "chatterfly"},
            voice_hotkeys_by_provider={"chatterfly": {"hold": "f8", "source": "manual"}},
        )
        self.owner._voice_shortcut.hotkey = SimpleNamespace(modifiers=(), key="f8")
        self.owner._configured_voice_hotkey_backend.return_value = "marked"
        patcher = mock.patch.object(host_module, "read_chatterfly_voice_windows", return_value=())
        self.windows = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch(
            "ovb_rc003.chromecast_wetype_toggle.win32_input.send_voice_key_combo_tap"
        )
        self.tap = patcher.start()
        self.addCleanup(patcher.stop)

    def test_toggle_waits_for_visible_voice_window_and_stops_once(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.tap.assert_called_once_with(("f8",))
        self.host.client.voice_host.assert_not_called()
        self.windows.return_value = (visible_voice_window(),)
        self.host._poll_host()
        self.host.client.voice_host.assert_called_once_with(1, "ready")
        self.host._handle({"event": "state", "attempt": 1, "data": "recording"})
        stop = {"event": "host_stop", "attempt": 1, "data": "second_press"}
        self.host._handle(stop)
        self.host._handle(stop)
        self.assertEqual(self.tap.call_count, 2)
        self.owner._voice_shortcut.apply.assert_not_called()

    def test_toggle_rejects_an_existing_voice_window(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.windows.return_value = (visible_voice_window(),)
        self.host._start_host(1)
        self.tap.assert_not_called()
        self.host.client.voice_host.assert_called_with(1, "failed")

    def test_empty_provider_shortcut_never_uses_the_runtime_fallback(self):
        self.owner._config["voice_hotkeys_by_provider"]["chatterfly"]["hold"] = ""
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.tap.assert_not_called()
        self.owner._voice_shortcut.apply.assert_not_called()
        self.host.client.voice_host.assert_called_with(1, "failed")

    def test_time_limit_stops_only_a_still_visible_voice_window(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.windows.return_value = (visible_voice_window(),)
        self.host._poll_host()
        self.host._handle({"event": "state", "attempt": 1, "data": "recording"})
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "time_limit"})
        self.assertEqual(self.tap.call_count, 2)

    def test_time_limit_does_not_restart_an_already_hidden_voice_window(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.host._start_host(1)
        self.windows.return_value = (visible_voice_window(),)
        self.host._poll_host()
        self.host._handle({"event": "state", "attempt": 1, "data": "recording"})
        self.windows.return_value = ()
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "time_limit"})
        self.tap.assert_called_once_with(("f8",))

    def test_no_visible_window_times_out_without_a_compensating_toggle(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        with mock.patch.object(host_module.time, "monotonic", return_value=10) as clock:
            self.host._start_host(1)
            clock.return_value = 19.1
            self.host._poll_host()
        self.host.client.voice_host.assert_any_call(1, "failed")
        self.tap.assert_called_once_with(("f8",))

    def test_hold_uses_key_edges_and_waits_for_window(self):
        self.owner._config["remote_recording_mode"] = "hold"
        self.host._start_host(1)
        self.owner._voice_shortcut.apply.assert_called_once_with(
            voice_controller.VoiceHostAction.KEY_DOWN
        )
        self.host.client.voice_host.assert_not_called()
        self.windows.return_value = (visible_voice_window(),)
        self.host._poll_host()
        self.host.client.voice_host.assert_called_once_with(1, "ready")
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "released"})
        self.assertEqual(
            self.owner._voice_shortcut.apply.call_args_list,
            [mock.call(voice_controller.VoiceHostAction.KEY_DOWN),
             mock.call(voice_controller.VoiceHostAction.KEY_UP)],
        )
        self.tap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
