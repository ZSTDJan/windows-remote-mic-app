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
        self.owner._configured_voice_hotkey_backend.return_value = "chatterfly_hotkey"
        self.windows = mock.Mock(return_value=())
        self.owner._voice_shortcut.chatterfly_control = SimpleNamespace(activity=self.windows)
        patcher = mock.patch("ovb_rc003.chromecast_wetype_toggle.win32_input.send_voice_key_combo_tap")
        self.tap = patcher.start()
        self.addCleanup(patcher.stop)

    def test_both_device_modes_use_explicit_start_stop_and_wait_for_owned_window(self):
        for mode in ("hold", "toggle"):
            with self.subTest(mode=mode):
                self.owner._config["remote_recording_mode"] = mode
                self.host._start_host(self.host.attempt + 1)
                self.assertTrue(self.host.engaged)
                self.assertFalse(self.host.ready_sent)
                call = self.owner._voice_shortcut.apply.call_args
                self.assertEqual(call.args, (voice_controller.VoiceHostAction.KEY_DOWN,))
                self.assertEqual(call.kwargs["backend_override"], "chatterfly_hotkey")
                self.windows.return_value = (visible_voice_window(),)
                self.host._poll_host()
                self.assertTrue(self.host.ready_sent)
                self.host._handle({"event": "host_stop", "attempt": self.host.attempt,
                                   "data": "second_press" if mode == "toggle" else "released"})
                call = self.owner._voice_shortcut.apply.call_args
                self.assertEqual(call.args, (voice_controller.VoiceHostAction.KEY_UP,))
                self.assertEqual(call.kwargs["backend_override"], "chatterfly_hotkey")
                self.assertTrue(self.host.closed)
                self.tap.assert_not_called()
                self.host._handle({"event": "state", "attempt": self.host.attempt, "data": "idle"})
                self.windows.return_value = ()

    def test_ipc_refusal_does_not_publish_ready(self):
        self.owner._voice_shortcut.apply.return_value = False
        self.host._start_host(1)
        self.assertFalse(self.host.engaged)
        self.host.client.voice_host.assert_called_with(1, "failed")
        self.tap.assert_not_called()

    def test_empty_shortcut_and_unavailable_key_hook_do_not_block_ipc(self):
        self.owner._config["voice_hotkeys_by_provider"]["chatterfly"]["hold"] = ""
        self.owner._ensure_voice_key_physicalizer_for_hotkey.return_value = False
        self.host._start_host(1)
        self.assertTrue(self.host.engaged)
        self.owner._ensure_voice_key_physicalizer_for_hotkey.assert_not_called()
        self.tap.assert_not_called()

    def test_duplicate_end_never_dispatches_an_extra_start_or_stop(self):
        self.host._start_host(1)
        self.windows.return_value = (visible_voice_window(),)
        self.host._poll_host()
        self.host._handle({"event": "state", "attempt": 1, "data": "recording"})
        for _ in range(2):
            self.host._handle({"event": "host_stop", "attempt": 1, "data": "time_limit"})
        self.assertEqual([c.args[0] for c in self.owner._voice_shortcut.apply.call_args_list],
                         [voice_controller.VoiceHostAction.KEY_DOWN, voice_controller.VoiceHostAction.KEY_UP])

    def test_no_window_times_out_and_asks_same_owner_to_clean_up(self):
        with mock.patch.object(host_module.time, "monotonic", return_value=10) as clock:
            self.host._start_host(1)
            clock.return_value = 19.1
            self.host._poll_host()
        self.host.client.voice_host.assert_any_call(1, "failed")
        self.assertTrue(self.host.closed)
        self.tap.assert_not_called()

    def test_cancel_during_start_releases_same_owner_without_readiness(self):
        self.host._start_host(1)
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "released"})
        self.windows.return_value = (visible_voice_window(),)
        self.host._poll_host()
        self.assertNotIn(mock.call(1, "ready"), self.host.client.voice_host.call_args_list)
        self.assertTrue(self.host.closed)

    def test_normal_async_close_waits_without_reporting_release_failure(self):
        self.host._start_host(1)
        self.owner._voice_shortcut.release_pending.return_value = False
        self.owner._voice_shortcut.chatterfly_control.completion_pending = True
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "released"})
        self.assertFalse(self.host.closed)
        self.owner._set_runtime_voice_result.assert_called_with('finishing')
        self.assertNotIn(mock.call(1, 'release_failed'), self.host.client.voice_host.call_args_list)
        self.owner._voice_shortcut.release_pending.return_value = True
        self.owner._voice_shortcut.chatterfly_control.completion_pending = False
        self.assertTrue(self.host._stop_host())
        self.host.client.voice_host.assert_called_with(1, 'released')


if __name__ == "__main__":
    unittest.main()
