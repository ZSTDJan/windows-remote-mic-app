"""Chromecast + Doubao hold adapter tests; all native boundaries are mocked."""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import (
    chromecast_host_activity as activity,
    chromecast_doubao_handsfree as handsfree,
    chromecast_voice_host as host_module,
    config,
    doubao_rpc,
    hotkey,
    key_detection_bridge,
    raw_input_windows,
    remote_selection,
    voice_controller,
    voice_key_physicalizer_windows,
    voice_program_manager,
    wetype_control_windows,
    win32_input,
)
from ovb_rc003.chromecast_voice_host import VoiceHost
from tests.test_app_wiring import _AppWiringTestCase
from tests.test_chromecast_host_activity import session
import tests.test_chromecast_voice as fixtures


class DoubaoHostTests(unittest.TestCase):
    def setUp(self):
        fixtures.HostTests.setUp(self)
        self.owner._config.update(
            voice_program={"provider": "doubao_ime"},
            remote_recording_mode="hold",
            voice_hotkeys_by_provider={
                "doubao_ime": {"hold": "ralt+space", "source": "auto"}
            },
        )
        self.owner._voice_shortcut.hotkey = SimpleNamespace(modifiers=("ralt",), key="space")
        self.owner._configured_voice_hotkey_backend.return_value = "doubao_hotkey"
        self.owner._ensure_voice_key_physicalizer_for_hotkey.return_value = True
        self.capture_patch = mock.patch.object(
            host_module, "read_doubao_capture", return_value=()
        )
        self.capture = self.capture_patch.start()
        self.addCleanup(self.capture_patch.stop)
        self.owner._voice_shortcut.doubao_physicalizer.generation = 7
        self.owner._voice_shortcut.doubao_physicalizer.is_active_generation.return_value = True

    def test_hold_and_handsfree_confirm_promptly_but_never_before_capture(self):
        for mode in ("hold", "toggle"):
            with self.subTest(mode=mode):
                self.owner._config["remote_recording_mode"] = mode
                self.owner._config["voice_hotkeys_by_provider"]["doubao_ime"].update(
                    toggle="lshift+f9", toggle_source="auto")
                self.capture.side_effect = None
                self.capture.return_value = ()
                self.host.client.reset_mock()
                with mock.patch.object(host_module.time, "monotonic", return_value=10) as clock:
                    self.host._start_host(1 if mode == "hold" else 2)
                    self.host._poll_host()
                    self.assertFalse(self.host.confirmed)
                    self.owner._voice_audio.writer.submit.assert_not_called()
                    self.capture.return_value = (session("doubao", pid=41),)
                    clock.return_value = 10.06
                    self.host._poll_host()
                    self.assertTrue(self.host.confirmed)
                    self.host.client.voice_host.assert_called_once_with(self.host.attempt, "ready")
                    # Cancel instead of finishing a live native host.
                    self.host.cancel_recording()
                    self.host._poll_host()
                    self.assertTrue(self.host.closed)
                    self.host._handle({"event": "state", "attempt": self.host.attempt, "data": "idle"})

    def _finish_wetype_after_tracker_loss(self):
        self.owner._config["voice_program"] = {"provider": "wetype"}
        self.owner._voice_shortcut.hotkey = SimpleNamespace(
            modifiers=("lctrl",), key="lwin"
        )
        self.owner._configured_voice_hotkey_backend.return_value = "wetype_hotkey"
        self.host._start_host(1)
        self.host.cancel_recording()
        self.host._poll_host()
        self.assertTrue(self.host.closed)
        self.host._handle({"event": "state", "attempt": 1, "data": "idle"})

        self.owner._config["voice_program"] = {"provider": "doubao_ime"}
        self.owner._voice_shortcut.hotkey = SimpleNamespace(
            modifiers=("ralt",), key="space"
        )
        self.owner._configured_voice_hotkey_backend.return_value = "doubao_hotkey"
        self.owner._voice_shortcut.apply.reset_mock()
        self.owner._voice_shortcut.apply.return_value = True
        self.owner._set_runtime_voice_active.reset_mock()
        self.owner._set_runtime_voice_result.reset_mock()
        self.host.client.reset_mock()

    def test_previous_wetype_tracking_loss_does_not_cancel_next_doubao_attempt(self):
        self._finish_wetype_after_tracker_loss()
        self.capture.side_effect = [(), (session("doubao", pid=41),)]
        self.host._start_host(2)

        self.assertTrue(self.host.engaged)
        self.owner._set_runtime_voice_active.assert_called_once_with(True)
        self.owner._set_runtime_voice_result.assert_called_once_with(
            host_module.status.VOICE_RUNTIME_ACTIVE
        )

        self.host.pending_pcm.append([1, 2, 3])
        self.host.pending_samples = 3
        self.host._poll_host()
        self.host.client.voice_host.assert_called_once_with(2, "ready")
        self.owner._voice_audio.writer.submit.assert_not_called()
        self.assertTrue(self.host._stop_host())
        self.owner._voice_audio.writer.submit.assert_called_once_with([1, 2, 3])

    def test_previous_wetype_tracking_loss_does_not_hide_doubao_start_failure(self):
        self._finish_wetype_after_tracker_loss()
        self.owner._voice_shortcut.apply.return_value = False

        self.host._start_host(2)

        self.assertFalse(self.host.engaged)
        self.assertEqual(
            self.host.failure_result,
            host_module.status.VOICE_RUNTIME_HOST_START_FAILED,
        )
        self.owner._set_runtime_voice_result.assert_called_once_with(
            host_module.status.VOICE_RUNTIME_HOST_START_FAILED
        )
        self.host.client.voice_host.assert_called_once_with(2, "failed")
        self.owner._voice_audio.writer.submit.assert_not_called()

    def test_handsfree_starts_with_one_short_tap_then_finishes_bound_capture_once(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.owner._config["voice_hotkeys_by_provider"]["doubao_ime"].update(
            toggle="lshift+f9",
            toggle_source="auto",
        )
        active = session("doubao", pid=41)
        self.capture.side_effect = [(), (), (active,)]

        self.host._start_host(1)
        self.host._poll_host()

        self.assertEqual(
            [call.args[0] for call in self.owner._voice_shortcut.apply.call_args_list],
            [voice_controller.VoiceHostAction.KEY_DOWN, voice_controller.VoiceHostAction.KEY_UP],
        )
        for call in self.owner._voice_shortcut.apply.call_args_list:
            self.assertEqual(call.kwargs["tokens_override"], ("lshift", "f9"))
            self.assertEqual(call.kwargs["backend_override"], "doubao_hotkey")
        self.owner._ensure_voice_diagnostic_attempt.assert_called_once_with(
            provider_shortcut_mode="toggle",
            effective_hotkey_tokens=("lshift", "f9"),
            effective_backend="doubao_hotkey",
        )
        self.host.client.voice_host.assert_called_once_with(1, "ready")
        self.host.state = "recording"

        with mock.patch.object(
            handsfree,
            "finish_with_timeout",
            return_value="sent_once",
        ) as finish:
            event = {"event": "host_stop", "attempt": 1, "data": "second_press"}
            self.host._handle(event)
            self.host._handle(event)

        finish.assert_called_once_with(active.identity, cancel_event=self.host.stopping)
        self.assertTrue(self.host.closed)

    def test_handsfree_capture_end_stops_without_sending_finish_key(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.owner._config["voice_hotkeys_by_provider"]["doubao_ime"].update(
            toggle="lshift+f9",
            toggle_source="auto",
        )
        active = session("doubao", pid=41)
        self.capture.side_effect = [(), (), (active,), (), ()]

        with (
            mock.patch.object(host_module.time, "monotonic", return_value=10) as clock,
            mock.patch.object(handsfree, "finish_with_timeout") as finish,
        ):
            self.host._start_host(1)
            self.host._poll_host()
            clock.return_value = 11
            self.host._poll_host()
            clock.return_value = 11.75
            self.host._poll_host()

        self.assertTrue(self.host.closed)
        finish.assert_not_called()
        self.host.client.voice_host.assert_any_call(1, "stop")

    def test_handsfree_time_limit_finishes_the_bound_capture_once(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.owner._config["voice_hotkeys_by_provider"]["doubao_ime"].update(
            toggle="lshift+f9",
            toggle_source="auto",
        )
        active = session("doubao", pid=41)
        self.capture.side_effect = [(), (), (active,)]
        self.host._start_host(1)
        self.host._poll_host()
        self.host.state = "recording"

        with mock.patch.object(
            handsfree,
            "finish_with_timeout",
            return_value="sent_once",
        ) as finish:
            event = {"event": "host_stop", "attempt": 1, "data": "time_limit"}
            self.host._handle(event)
            self.host._stop_host("time_limit")

        finish.assert_called_once_with(active.identity, cancel_event=self.host.stopping)
        self.assertTrue(self.host.closed)

    def test_handsfree_audio_failure_releases_without_submitting_to_doubao(self):
        self.owner._config["remote_recording_mode"] = "toggle"
        self.owner._config["voice_hotkeys_by_provider"]["doubao_ime"].update(
            toggle="lshift+f9", toggle_source="auto")
        self.capture.side_effect = [(), (), (session("doubao", pid=41),)]
        self.host._start_host(1)
        self.host._poll_host()
        self.host.state = "recording"
        self.owner._voice_audio.flush.return_value = SimpleNamespace(
            completed=True, error=OSError("test output failed"))
        with mock.patch.object(handsfree, "finish_with_timeout") as finish:
            self.assertTrue(self.host._stop_host("second_press"))
            self.host._stop_host("second_press")
            finish.assert_not_called()
        self.assertIsNotNone(self.host.failure_result)
        self.assertIsNone(self.owner._voice_audio.writer)
        self.assertIsNone(self.owner._voice_audio.sink)

    def test_handsfree_and_hold_shortcuts_are_stored_independently(self):
        settings = {
            "voice_program": {"provider": "doubao_ime"},
            "remote_recording_mode": "toggle",
            "voice_hotkeys_by_provider": {
                "doubao_ime": {"hold": "ralt", "source": "auto"}
            },
        }

        config.set_voice_hotkey_for_provider(
            settings,
            "doubao_ime",
            "lshift+f9",
            source="manual",
            trigger="toggle",
        )

        self.assertEqual(config.voice_hotkey_for_provider(settings, "doubao_ime"), "ralt")
        self.assertEqual(
            config.voice_hotkey_for_provider(settings, "doubao_ime", trigger="toggle"),
            "lshift+f9",
        )
        self.assertEqual(config.voice_hotkey_trigger_for_settings(settings), "toggle")

    def test_hold_waits_for_unique_capture_and_reuses_attempt_snapshot_on_release(self):
        self.capture.side_effect = [(), (session("doubao", pid=41),)]

        self.host._start_host(1)

        self.assertTrue(self.host.engaged)
        self.assertFalse(self.host.confirmed)
        self.host.client.voice_host.assert_not_called()
        start_call = self.owner._voice_shortcut.apply.call_args
        self.assertEqual(start_call.args, (voice_controller.VoiceHostAction.KEY_DOWN,))
        self.assertEqual(start_call.kwargs["backend_override"], "doubao_hotkey")
        self.assertEqual(start_call.kwargs["tokens_override"], ("ralt", "space"))
        self.assertTrue(callable(start_call.kwargs["cancelled"]))
        self.owner._ensure_voice_diagnostic_attempt.assert_called_once_with(
            provider_shortcut_mode="hold",
            effective_hotkey_tokens=("ralt", "space"),
            effective_backend="doubao_hotkey",
        )

        self.owner._voice_shortcut.hotkey = SimpleNamespace(modifiers=("lctrl",), key="f9")
        self.owner._configured_voice_hotkey_backend.return_value = "marked"
        self.owner._set_runtime_voice_active.reset_mock()
        self.owner._set_runtime_voice_result.reset_mock()
        self.host._poll_host()
        self.host.client.voice_host.assert_called_once_with(1, "ready")
        self.owner._set_runtime_voice_active.assert_not_called()
        self.owner._set_runtime_voice_result.assert_not_called()

        self.host._handle({"event": "host_stop", "attempt": 1, "data": ""})
        release_call = self.owner._voice_shortcut.apply.call_args_list[-1]
        self.assertEqual(release_call.args, (voice_controller.VoiceHostAction.KEY_UP,))
        self.assertEqual(release_call.kwargs["backend_override"], "doubao_hotkey")
        self.assertEqual(release_call.kwargs["tokens_override"], ("ralt", "space"))
        self.assertTrue(self.host.closed)

    def test_physicalizer_unavailable_fails_before_audio_or_doubao_process_hook(self):
        self.owner._ensure_voice_key_physicalizer_for_hotkey.return_value = False

        self.host._start_host(1)

        self.owner._voice_audio.open.assert_not_called()
        self.owner._voice_shortcut.apply.assert_not_called()

    def test_stop_queued_before_start_prevents_all_start_side_effects(self):
        self.host.enqueue({"event": "host_stop", "attempt": 1, "data": ""})

        self.host._start_host(1)

        self.owner._ensure_voice_key_physicalizer_for_hotkey.assert_not_called()
        self.owner._voice_audio.open.assert_not_called()
        self.owner._voice_shortcut.apply.assert_not_called()

    def test_cancel_during_profile_activation_never_engages_or_forwards_pcm(self):
        def cancel_during_start(_action, **kwargs):
            with self.host._input_lock:
                self.host._cancelled_attempts.add(1)
            self.assertTrue(kwargs["cancelled"]())
            return False

        self.owner._voice_shortcut.apply.side_effect = cancel_during_start
        self.host._start_host(1)
        self.host._output([1, 2, 3])

        self.assertFalse(self.host.engaged)
        self.assertFalse(self.host.confirmed)
        self.owner._voice_audio.writer.submit.assert_not_called()
        self.host.client.voice_host.assert_not_called()

    def test_capture_end_stops_and_does_not_adopt_replacement(self):
        self.capture.side_effect = [
            (),
            (session("ours", pid=41),),
            (session("replacement", pid=42),),
            (),
        ]
        with mock.patch.object(host_module.time, "monotonic", return_value=10) as clock:
            self.host._start_host(1)
            self.host._poll_host()
            clock.return_value = 11
            self.host._poll_host()
            clock.return_value = 11.75
            self.host._poll_host()

        self.assertTrue(self.host.closed)
        self.host.client.voice_host.assert_any_call(1, "stop")

    def test_enqueued_stop_blocks_late_ready_and_buffered_pcm_before_dequeue(self):
        self.capture.side_effect = [(), (session("doubao", pid=41),)]
        self.host._start_host(1)
        self.host.pending_pcm.append([1, 2, 3])
        self.host.pending_samples = 3

        self.host.enqueue({"event": "host_stop", "attempt": 1, "data": ""})
        self.host._poll_host()

        self.assertFalse(self.host.closed)
        self.assertFalse(self.host.confirmed)
        self.assertNotIn(mock.call(1, "ready"), self.host.client.voice_host.call_args_list)
        self.owner._voice_audio.writer.submit.assert_not_called()

        self.host._handle(self.host.events.get_nowait())
        self.host._poll_host()
        self.assertTrue(self.host.closed)

    def test_enqueued_stop_rejects_audio_already_ahead_of_stop_event(self):
        self.host._start_host(1)
        decoder = self.host.decoder = mock.Mock(mic_open=True)
        self.host.confirmed = True
        self.host.state = "recording"
        self.host.enqueue({"event": "host_stop", "attempt": 1, "data": ""})

        self.host._handle({"event": "audio", "attempt": 1, "data": "0102"})

        decoder.handle_audio.assert_not_called()
        self.owner._voice_audio.writer.submit.assert_not_called()

    def test_current_doubao_adapter_loss_cancels_before_capture_ready(self):
        self.capture.side_effect = [(), (session("doubao", pid=41),)]
        self.host._start_host(1)
        self.owner._voice_shortcut.doubao_physicalizer.is_active_generation.return_value = False

        self.host._poll_host()

        self.assertTrue(self.host.closed)
        self.assertFalse(self.host.confirmed)
        self.assertNotIn(mock.call(1, "ready"), self.host.client.voice_host.call_args_list)
        self.owner._voice_audio.writer.submit.assert_not_called()

    def test_adapter_loss_is_not_reported_again_after_successful_cleanup(self):
        self.host._start_host(1)
        self.owner._voice_shortcut.doubao_physicalizer.is_active_generation.return_value = False
        self.host._poll_host()
        calls = list(self.host.client.voice_host.call_args_list)
        results = self.owner._set_runtime_voice_result.call_count

        self.host._poll_host()
        self.host._poll_host()

        self.assertEqual(self.host.client.voice_host.call_args_list, calls)
        self.assertEqual(self.owner._set_runtime_voice_result.call_count, results)
        self.assertIsNone(self.host._doubao_physicalizer_generation)

    def test_detach_after_normal_close_does_not_reopen_old_attempt(self):
        self.host._start_host(1)
        self.assertTrue(self.host._stop_host())
        calls = list(self.host.client.voice_host.call_args_list)
        results = self.owner._set_runtime_voice_result.call_count
        self.owner._voice_shortcut.doubao_physicalizer.is_active_generation.return_value = False

        self.host._poll_host()

        self.assertEqual(self.host.client.voice_host.call_args_list, calls)
        self.assertEqual(self.owner._set_runtime_voice_result.call_count, results)

    def test_adapter_loss_cleanup_retry_stops_observing_then_next_attempt_binds_new_generation(self):
        self.host._start_host(1)
        self.owner._voice_shortcut.doubao_physicalizer.is_active_generation.return_value = False
        self.owner._voice_audio.flush.return_value = SimpleNamespace(
            completed=False,
            error=None,
        )
        self.host._poll_host()
        self.assertFalse(self.host.closed)
        self.assertEqual(self.host.state, "stopping")
        self.assertTrue(self.host._settings_claimed)

        self.owner._voice_audio.flush.return_value = SimpleNamespace(
            completed=True,
            error=None,
        )
        self.assertTrue(self.host._stop_host())
        self.assertIsNone(self.host._doubao_physicalizer_generation)
        self.host._handle({"event": "state", "attempt": 1, "data": "idle"})
        self.owner._voice_shortcut.doubao_physicalizer.generation = 8
        self.owner._voice_shortcut.doubao_physicalizer.is_active_generation.return_value = True

        self.host._start_host(2)

        self.assertTrue(self.host.engaged)
        self.assertEqual(self.host._doubao_physicalizer_generation, 8)

    def test_failed_cleanup_keeps_settings_claim_until_release_succeeds(self):
        self.host._start_host(1)
        self.owner._voice_audio.flush.return_value = SimpleNamespace(
            completed=False,
            error=None,
        )

        self.assertFalse(self.host._stop_host())

        self.assertTrue(self.host._settings_claimed)
        self.owner._apply_pending_voice_settings_if_idle_locked.assert_not_called()

    def test_tracker_loss_wait_window_allows_host_thread_to_release_keys(self):
        self.host._start_host(1)
        result = []
        waiter = threading.Thread(
            target=lambda: result.append(
                self.host.cancel_recording_and_wait(.5)
            )
        )
        waiter.start()
        self.assertTrue(self.host.input_lost.wait(.2))

        self.host._poll_host()
        waiter.join(1.0)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, [True])
        self.assertTrue(self.host.closed)


class DoubaoControlCancellationTests(unittest.TestCase):
    def test_cancellation_after_profile_activation_prevents_key_down(self):
        cancelled = False
        pressed = mock.Mock()

        def activate():
            nonlocal cancelled
            cancelled = True
            return True

        control = wetype_control_windows.DoubaoVoiceControl(
            activate_profile=activate,
            run_sta=lambda callback: callback(),
            press_keys=pressed,
            release_keys=mock.Mock(),
        )

        self.assertFalse(control.start(("ralt", "space"), cancelled=lambda: cancelled))
        pressed.assert_not_called()
        self.assertFalse(control.cleanup_pending)


class DoubaoProductWiringTests(_AppWiringTestCase):
    def setUp(self):
        super().setUp()
        # This class exercises the Chromecast-to-Doubao product wiring.  The
        # physical-key tracker lifecycle has its own tests; pin its availability
        # here so a late teardown from an earlier full-suite case cannot divert
        # these tests into the unrelated safety-failure branch.
        tracked_hold_patch = mock.patch.object(
            raw_input_windows,
            "physical_keyboard_tracking_available",
            return_value=True,
        )
        tracked_hold_patch.start()
        self.addCleanup(tracked_hold_patch.stop)

    def test_chromecast_timeout_retains_then_settles_shared_doubao_owner(self):
        self.app._remote_profile = remote_selection.CHROMECAST_PROFILE
        self.app._config.update(
            voice_program={"provider": voice_program_manager.VOICE_PROGRAM_DOUBAO_IME},
            remote_recording_mode="hold",
            voice_hotkeys_by_provider={
                voice_program_manager.VOICE_PROGRAM_DOUBAO_IME: {
                    "hold": "ralt+space",
                    "source": "auto",
                }
            },
        )
        self.app._voice_shortcut.hotkey = hotkey.HotkeySpec.parse("ralt+space")
        config.save_config(self.app._config_path, self.app._config)
        activation_started = threading.Event()
        activation_count = 0

        def activate():
            nonlocal activation_count
            activation_count += 1
            return True

        control = self.app._voice_shortcut.doubao_control = (
            wetype_control_windows.DoubaoVoiceControl(
                activate_profile=activate,
            )
        )
        operations = []

        def start_sta_operation(callback, *, cancel_event=None):
            operation = wetype_control_windows._StaOperation(cancel_event)
            operations.append(operation)
            if len(operations) == 1:
                activation_started.set()
            else:
                operation.set_result(True, callback())
                operation.mark_settled()
            return operation

        control._doubao_start_sta = start_sta_operation
        self.app._voice_key_physicalizer = mock.Mock(accepts_new_down=True)
        host = self.app._create_chromecast_voice_host()
        self.app._chromecast_runtime.voice_host = host
        host.client = mock.Mock()
        voice_key_physicalizer_windows._set_physical_tracker_active(
            True,
            _query=lambda _vk: False,
        )

        with (
            mock.patch.object(host_module, "read_doubao_capture", return_value=()),
            mock.patch.object(self.app, "_voice_mode_for_primary_button", return_value="hold"),
            mock.patch.object(self.app._voice_audio, "open", return_value=True),
            mock.patch.object(self.app, "_ensure_voice_diagnostic_attempt"),
            mock.patch.object(key_detection_bridge, "publish_next_button", return_value=False),
            mock.patch.object(raw_input_windows, "_real_async_key_is_down", return_value=False),
            mock.patch.object(wetype_control_windows, "_STA_RESULT_TIMEOUT_SECONDS", 0.01),
            mock.patch.object(win32_input, "_real_voice_event") as native,
        ):
            host._start_host(1)
            self.assertTrue(activation_started.is_set())
            self.assertFalse(host.engaged)
            self.assertTrue(control.cleanup_pending)
            native.assert_not_called()

            operations[0].set_result(True, True)
            operations[0].mark_settled()
            self.assertFalse(control.cleanup_pending)

            self.assertTrue(host._stop_host())
            host._handle({"event": "state", "attempt": 1, "data": "idle"})
            host._start_host(2)
            self.assertTrue(host.engaged)
            self.assertTrue(control.cleanup_pending)
            self.assertGreater(native.call_count, 0)
            self.assertTrue(host._stop_host())

    def test_right_alt_tracker_is_required_only_for_shortcuts_that_use_it(self):
        self.app._voice_key_physicalizer_ready = False
        with mock.patch.object(self.app, "_start_voice_key_physicalizer") as start:
            self.assertTrue(
                self.app._ensure_voice_key_physicalizer_for_hotkey(
                    ("lctrl", "f9"), "doubao_hotkey"
                )
            )
            start.assert_not_called()
            self.assertFalse(
                self.app._ensure_voice_key_physicalizer_for_hotkey(
                    ("rctrl", "ralt", "space"), "doubao_hotkey"
                )
            )
            start.assert_called_once()

    def test_product_start_reaches_default_doubao_sender_without_live_input(self):
        self.app._remote_profile = remote_selection.CHROMECAST_PROFILE
        self.app._config.update(
            voice_program={"provider": voice_program_manager.VOICE_PROGRAM_DOUBAO_IME},
            remote_recording_mode="hold",
            voice_hotkeys_by_provider={
                voice_program_manager.VOICE_PROGRAM_DOUBAO_IME: {
                    "hold": "ralt+space",
                    "source": "auto",
                }
            },
        )
        self.app._voice_shortcut.hotkey = hotkey.HotkeySpec.parse("ralt+space")
        config.save_config(self.app._config_path, self.app._config)
        activate = mock.Mock(return_value=True)
        self.app._voice_shortcut.doubao_control = wetype_control_windows.DoubaoVoiceControl(
            activate_profile=activate,
            run_sta=lambda callback: callback(),
        )
        self.app._voice_key_physicalizer = mock.Mock(accepts_new_down=True)
        host = self.app._create_chromecast_voice_host()
        self.app._chromecast_runtime.voice_host = host
        host.client = mock.Mock()
        voice_key_physicalizer_windows._set_physical_tracker_active(
            True,
            _query=lambda _vk: False,
        )

        with (
            mock.patch.object(host_module, "read_doubao_capture", return_value=()),
            mock.patch.object(self.app, "_voice_mode_for_primary_button", return_value="hold"),
            mock.patch.object(self.app._voice_audio, "open", return_value=True),
            mock.patch.object(self.app, "_ensure_voice_diagnostic_attempt"),
            mock.patch.object(
                self.app._voice_audio,
                "flush",
                return_value=SimpleNamespace(completed=True, error=None),
            ),
            mock.patch.object(key_detection_bridge, "publish_next_button", return_value=False),
            mock.patch.object(raw_input_windows, "_real_async_key_is_down", return_value=False),
            mock.patch.object(
                win32_input,
                "_real_voice_event",
            ) as native,
        ):
            host._start_host(1)
            self.assertTrue(host.engaged)
            activate.assert_called_once()
            self.app._voice_shortcut.doubao_physicalizer.start.assert_called_once_with((0xA5, 0x20))
            self.assertEqual(
                native.call_args_list[:2],
                [mock.call(0xA5, False), mock.call(0x20, False)],
            )
            self.assertTrue(host._stop_host())
            self.assertEqual(
                native.call_args_list[-2:],
                [mock.call(0x20, True), mock.call(0xA5, True)],
            )

    def test_doubao_process_physicalizer_failure_prevents_native_key_down(self):
        self.app._config.update(
            voice_program={"provider": voice_program_manager.VOICE_PROGRAM_DOUBAO_IME},
            voice_hotkeys_by_provider={
                voice_program_manager.VOICE_PROGRAM_DOUBAO_IME: {
                    "hold": "rctrl+ralt+space",
                    "source": "manual",
                }
            },
        )
        self.app._voice_shortcut.hotkey = hotkey.HotkeySpec.parse("rctrl+ralt+space")
        config.save_config(self.app._config_path, self.app._config)
        self.app._voice_shortcut.doubao_physicalizer.start.return_value = False
        self.app._voice_shortcut.doubao_control = wetype_control_windows.DoubaoVoiceControl(
            activate_profile=mock.Mock(return_value=True),
            run_sta=lambda callback: callback(),
        )
        host = self.app._create_chromecast_voice_host()
        self.app._chromecast_runtime.voice_host = host
        host.client = mock.Mock()

        with (
            mock.patch.object(host_module, "read_doubao_capture", return_value=()),
            mock.patch.object(self.app, "_voice_mode_for_primary_button", return_value="hold"),
            mock.patch.object(self.app, "_ensure_voice_key_physicalizer_for_hotkey", return_value=True),
            mock.patch.object(self.app._voice_audio, "open", return_value=True),
            mock.patch.object(self.app, "_ensure_voice_diagnostic_attempt"),
            mock.patch.object(key_detection_bridge, "publish_next_button", return_value=False),
            mock.patch.object(win32_input, "_real_voice_event") as native,
        ):
            host._start_host(1)

        native.assert_not_called()
        self.assertFalse(host.engaged)
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_chromecast_physicalizer_loss_cancels_host_without_xiaomi_release(self):
        self.app._remote_profile = remote_selection.CHROMECAST_PROFILE
        physicalizer = self.app._voice_key_physicalizer = mock.Mock(is_running=False)
        self.app._voice_key_physicalizer_generation = 7
        self.app._voice_key_physicalizer_stopping = False
        host = self.app._chromecast_runtime.voice_host = mock.Mock()

        with (
            mock.patch.object(self.app, "_schedule_voice_key_physicalizer_recovery_locked") as retry,
            mock.patch.object(self.app, "_force_voice_hold_release_locked") as xiaomi_release,
        ):
            self.app._on_voice_key_physicalizer_tracking_lost(physicalizer, 7)

        host.cancel_recording_and_wait.assert_called_once_with(2.5)
        retry.assert_called_once()
        xiaomi_release.assert_not_called()

    def test_physicalizer_recovery_can_restart_tracker_for_pending_release_only(self):
        self.app._remote_profile = remote_selection.CHROMECAST_PROFILE
        self.app._chromecast_runtime.voice_host = SimpleNamespace(closed=False)
        token = self.app._voice_key_physicalizer_retry_token = object()
        self.app._voice_key_physicalizer_retry_timer = mock.Mock()

        with (
            mock.patch.object(self.app, "_schedule_voice_key_physicalizer_recovery_locked") as retry,
            mock.patch.object(self.app, "_start_voice_key_physicalizer") as start,
        ):
            self.app._recover_voice_key_physicalizer(
                token,
                self.app._voice_key_physicalizer,
                self.app._voice_key_physicalizer_generation,
            )

        retry.assert_not_called()
        start.assert_called_once_with(recovering=True)


class DoubaoCaptureInventoryTests(unittest.TestCase):
    def test_capture_uses_only_exact_ime_service_process(self):
        candidates = (
            ("doubao_ime", 41, "ImeWatchdog.exe"),
            ("doubao_ime", 42, "ImeService.exe"),
            ("wetype", 43, "wetype_server.exe"),
        )
        with (
            mock.patch.object(
                activity.voice_program_manager,
                "diagnostic_voice_processes",
                return_value=candidates,
            ),
            mock.patch.object(activity.audio, "read_capture_sessions", return_value=()) as read,
        ):
            self.assertEqual(activity.doubao_capture_pids(), {42})
            activity.read_doubao_capture()

        read.assert_called_once_with({42})


class DoubaoHandsFreeFinishTests(unittest.TestCase):
    def test_finish_uses_builtin_stop_once_for_exact_capture(self):
        active = session("doubao", pid=41)
        send = mock.Mock()
        control = handsfree.DoubaoHandsFreeFinish(
            active.identity,
            reader=lambda: (active,),
            send_stop=send,
        )

        self.assertEqual(control.request_finish(), "sent_once")
        self.assertEqual(control.request_finish(), "sent_once")
        send.assert_called_once_with()

    def test_finish_fails_closed_when_capture_identity_changed(self):
        expected = session("doubao", pid=41)
        replacement = session("replacement", pid=42)
        send = mock.Mock()
        control = handsfree.DoubaoHandsFreeFinish(
            expected.identity,
            reader=lambda: (replacement,),
            send_stop=send,
        )

        self.assertEqual(control.request_finish(), "capture_ended")
        send.assert_not_called()

    def test_failed_stop_is_not_retried(self):
        active = session("doubao", pid=41)
        send = mock.Mock(side_effect=OSError("stop"))
        control = handsfree.DoubaoHandsFreeFinish(
            active.identity,
            reader=lambda: (active,),
            send_stop=send,
        )

        self.assertEqual(control.request_finish(), "send_failed")
        self.assertEqual(control.request_finish(), "send_failed")
        send.assert_called_once_with()


class DoubaoPhysicalizerHealthTests(unittest.TestCase):
    def test_only_current_generation_is_invalidated_by_session_detach(self):
        physicalizer = doubao_rpc.DoubaoPhysicalizer()
        session_handle = mock.Mock(is_detached=False)
        physicalizer._session = session_handle
        physicalizer._script = object()
        physicalizer._status = "active"
        physicalizer._generation = 2

        self.assertFalse(physicalizer.is_active_generation(1))
        self.assertEqual(physicalizer.status, "active")
        self.assertTrue(physicalizer.is_active_generation(2))

        session_handle.is_detached = True
        self.assertFalse(physicalizer.is_active_generation(2))
        self.assertEqual(physicalizer.status, "detached")
        self.assertIsNone(physicalizer._session)
        self.assertIsNone(physicalizer._script)


if __name__ == "__main__":
    unittest.main()
