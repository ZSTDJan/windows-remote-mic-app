from tests.source_contract import source_text
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from ovb_rc003 import wetype_control_windows, win32_input
from ovb_rc003 import doubao_input_profile_windows as directed


def profile(seed, *, flags=2):
    return wetype_control_windows._InputProfileSnapshot(
        profile_type=1,
        language_id=0x0804,
        clsid=bytes([seed]) * 16,
        profile_guid=bytes([seed + 1]) * 16,
        flags=flags,
    )


PREVIOUS_PROFILE = profile(1)
OTHER_PROFILE = profile(3)
WETYPE_PROFILE = profile(5)
DOUBAO_PROFILE = profile(7)


def profile_registry_text(candidate):
    if candidate.clsid == WETYPE_PROFILE.clsid:
        return "wetype 微信输入法"
    if candidate.clsid == DOUBAO_PROFILE.clsid:
        return "doubao 豆包输入法"
    return "other input method"


class FakeProfileManager:
    def __init__(self, active_profiles, available_profiles=()):
        self.active_profiles = list(active_profiles)
        self.available_profiles = tuple(available_profiles)
        self.activations = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get_active_profile(self):
        return self.active_profiles.pop(0)

    def enum_profiles(self, language_id):
        if language_id != 0x0804:
            return ()
        return self.available_profiles

    def activate_profile(self, active_profile):
        self.activations.append(active_profile)


class InputProfileTests(unittest.TestCase):
    def tearDown(self):
        wetype_control_windows.set_diagnostic_trace(None)

    def test_activation_trace_records_before_target_after_and_result(self):
        trace = mock.Mock()
        trace.current_context.return_value = {
            "gesture_id": "gesture-1",
            "attempt_id": "attempt-1",
        }
        wetype_control_windows.set_diagnostic_trace(trace)
        manager = FakeProfileManager(
            [PREVIOUS_PROFILE, WETYPE_PROFILE],
            [OTHER_PROFILE, WETYPE_PROFILE],
        )
        with mock.patch.object(
            wetype_control_windows,
            "_InputProfileManager",
            return_value=manager,
        ), mock.patch.object(
            wetype_control_windows,
            "_profile_registry_text",
            side_effect=profile_registry_text,
        ):
            self.assertTrue(wetype_control_windows._activate_wetype_input_profile())

        events = [call.args[0] for call in trace.emit.call_args_list]
        self.assertIn("input_profile_activation_started", events)
        self.assertEqual(events.count("input_profile_snapshot"), 2)
        finished = next(
            call
            for call in trace.emit.call_args_list
            if call.args[0] == "input_profile_activation_finished"
        )
        self.assertTrue(finished.kwargs["confirmed"])
        self.assertTrue(finished.kwargs["switched"])
        self.assertEqual(finished.kwargs["attempt_id"], "attempt-1")

    def test_activation_switches_and_verifies_wetype(self):
        manager = FakeProfileManager(
            [PREVIOUS_PROFILE, WETYPE_PROFILE],
            [OTHER_PROFILE, WETYPE_PROFILE],
        )
        with mock.patch.object(
            wetype_control_windows,
            "_InputProfileManager",
            return_value=manager,
        ), mock.patch.object(
            wetype_control_windows,
            "_profile_registry_text",
            side_effect=profile_registry_text,
        ):
            switched = wetype_control_windows._activate_wetype_input_profile()

        self.assertTrue(switched)
        self.assertEqual(manager.activations, [WETYPE_PROFILE])

    def test_activation_is_a_noop_when_wetype_is_already_active(self):
        manager = FakeProfileManager(
            [profile(5, flags=9)],
            [WETYPE_PROFILE],
        )
        with mock.patch.object(
            wetype_control_windows,
            "_InputProfileManager",
            return_value=manager,
        ), mock.patch.object(
            wetype_control_windows,
            "_profile_registry_text",
            side_effect=profile_registry_text,
        ):
            switched = wetype_control_windows._activate_wetype_input_profile()

        self.assertFalse(switched)
        self.assertEqual(manager.activations, [])

    def test_activation_switches_and_verifies_doubao(self):
        manager = FakeProfileManager(
            [PREVIOUS_PROFILE, DOUBAO_PROFILE],
            [WETYPE_PROFILE, DOUBAO_PROFILE],
        )
        with mock.patch.object(
            wetype_control_windows,
            "_InputProfileManager",
            return_value=manager,
        ), mock.patch.object(
            wetype_control_windows,
            "_profile_registry_text",
            side_effect=profile_registry_text,
        ), mock.patch.object(directed, "current_target", return_value="target"), \
                mock.patch.object(directed, "activate_for_target", return_value=(True, "target")) as switch:
            switched = wetype_control_windows._activate_doubao_input_profile()

        self.assertTrue(switched)
        self.assertEqual(manager.activations, [])
        self.assertEqual(switch.call_args.args[0], DOUBAO_PROFILE)

    def test_unconfirmed_activation_restores_the_previous_profile(self):
        manager = FakeProfileManager(
            [PREVIOUS_PROFILE, OTHER_PROFILE],
            [WETYPE_PROFILE],
        )
        with mock.patch.object(
            wetype_control_windows,
            "_InputProfileManager",
            return_value=manager,
        ), mock.patch.object(
            wetype_control_windows,
            "_profile_registry_text",
            side_effect=profile_registry_text,
        ), self.assertRaises(OSError):
            wetype_control_windows._activate_wetype_input_profile()

        self.assertEqual(
            manager.activations,
            [WETYPE_PROFILE, PREVIOUS_PROFILE],
        )

    def test_cancelled_activation_restores_before_the_settle_delay(self):
        manager = FakeProfileManager(
            [PREVIOUS_PROFILE, WETYPE_PROFILE],
            [WETYPE_PROFILE],
        )
        sleep = mock.Mock(side_effect=AssertionError("settle delay must be skipped"))
        with mock.patch.object(
            wetype_control_windows,
            "_InputProfileManager",
            return_value=manager,
        ), mock.patch.object(
            wetype_control_windows,
            "_profile_registry_text",
            side_effect=profile_registry_text,
        ), mock.patch.object(
            wetype_control_windows,
            "_sta_cancelled",
            side_effect=[False, True],
        ):
            with self.assertRaises(TimeoutError):
                wetype_control_windows._activate_wetype_for_voice_start(
                    _sleep=sleep
                )

        sleep.assert_not_called()
        self.assertEqual(
            manager.activations,
            [WETYPE_PROFILE, PREVIOUS_PROFILE],
        )

    def test_cold_switch_waits_50ms_before_returning(self):
        waits = []
        manager = FakeProfileManager(
            [PREVIOUS_PROFILE, WETYPE_PROFILE],
            [WETYPE_PROFILE],
        )
        with mock.patch.object(
            wetype_control_windows,
            "_InputProfileManager",
            return_value=manager,
        ), mock.patch.object(
            wetype_control_windows,
            "_profile_registry_text",
            side_effect=profile_registry_text,
        ):
            switched = wetype_control_windows._activate_wetype_for_voice_start(
                _sleep=waits.append
            )

        self.assertTrue(switched)
        self.assertEqual(waits, [0.05])

    def test_already_active_profile_has_no_rebind_wait(self):
        waits = []
        manager = FakeProfileManager(
            [WETYPE_PROFILE],
            [WETYPE_PROFILE],
        )
        with mock.patch.object(
            wetype_control_windows,
            "_InputProfileManager",
            return_value=manager,
        ), mock.patch.object(
            wetype_control_windows,
            "_profile_registry_text",
            side_effect=profile_registry_text,
        ):
            switched = wetype_control_windows._activate_wetype_for_voice_start(
                _sleep=waits.append
            )

        self.assertFalse(switched)
        self.assertEqual(waits, [])

    def test_changed_com_apartment_mode_is_rejected(self):
        fake_ole32 = mock.Mock()
        fake_ole32.CoInitializeEx.return_value = -2147417850
        with mock.patch.object(
            wetype_control_windows,
            "_ole32",
            return_value=fake_ole32,
        ), self.assertRaisesRegex(OSError, "80010106"):
            wetype_control_windows._InputProfileManager().__enter__()

        fake_ole32.CoCreateInstance.assert_not_called()


class StaThreadTests(unittest.TestCase):
    def test_voice_joins_selection_once_and_cancel_does_not_cancel_selection(self):
        release = threading.Event()
        calls = []
        def activation():
            calls.append(True)
            release.wait(1)
            return True
        with mock.patch.object(wetype_control_windows, "_activate_doubao_for_voice_start", activation):
            source = wetype_control_windows.begin_input_profile_selection("doubao_ime")
            try:
                observer = wetype_control_windows._start_sta_operation(activation)
                observer.cancel()
                with self.assertRaises(wetype_control_windows.InputProfilePreparationError):
                    observer.result(0.1)
                self.assertFalse(source.cancel_event.is_set())
                second = wetype_control_windows._start_sta_operation(activation)
                release.set()
                self.assertTrue(second.result(0.5))
                self.assertEqual(calls, [True])
            finally:
                release.set()
                self.assertTrue(source.settled.wait(1))

    def test_selection_wait_cannot_extend_deadline_or_start_another_provider(self):
        release = threading.Event()
        def activation():
            release.wait(1)
            return True
        with mock.patch.object(wetype_control_windows, "_activate_doubao_for_voice_start", activation):
            source = wetype_control_windows.begin_input_profile_selection("doubao_ime")
            try:
                with self.assertRaises(wetype_control_windows.InputProfilePreparationError):
                    wetype_control_windows._start_sta_operation(lambda: True)
                waiter = wetype_control_windows._start_sta_operation(activation)
                source.deadline = time.monotonic() - 1
                with self.assertRaises(wetype_control_windows.InputProfilePreparationError):
                    waiter.result(5)
                release.set()
                self.assertTrue(source.settled.wait(1))
                with self.assertRaises(TimeoutError):
                    source.result(0)
            finally:
                release.set()
                source.settled.wait(1)

    def test_failed_selection_is_not_success_for_joined_voice(self):
        release = threading.Event()
        def activation():
            release.wait(1)
            raise OSError("activation failed")
        with mock.patch.object(wetype_control_windows, "_activate_doubao_for_voice_start", activation):
            source = wetype_control_windows.begin_input_profile_selection("doubao_ime")
            try:
                waiter = wetype_control_windows._start_sta_operation(activation)
                release.set()
                with self.assertRaises(wetype_control_windows.InputProfilePreparationError):
                    waiter.result(0.5)
            finally:
                release.set()
                source.settled.wait(1)

    def test_selection_cancel_after_activation_does_not_switch_back(self):
        cancel = threading.Event()
        manager = FakeProfileManager([PREVIOUS_PROFILE], [DOUBAO_PROFILE])
        def activate(target, **_kwargs):
            manager.activations.append(target)
            cancel.set()
            raise TimeoutError("selection cancelled")
        with mock.patch.object(wetype_control_windows, "_InputProfileManager", return_value=manager), \
                mock.patch.object(wetype_control_windows, "_profile_registry_text", side_effect=profile_registry_text), \
                mock.patch.object(directed, "current_target", return_value="target"), \
                mock.patch.object(directed, "activate_for_target", side_effect=activate):
            source = wetype_control_windows.begin_input_profile_selection("doubao_ime", cancel_event=cancel)
            with self.assertRaises(TimeoutError):
                source.result(0.5)
            self.assertTrue(source.settled.wait(1))
        self.assertEqual(manager.activations, [DOUBAO_PROFILE])

    def test_callback_runs_on_a_different_thread(self):
        caller_thread = threading.get_ident()
        worker_thread = wetype_control_windows._run_on_sta_thread(
            threading.get_ident
        )
        self.assertNotEqual(worker_thread, caller_thread)

    def test_callback_failure_is_propagated(self):
        with self.assertRaisesRegex(OSError, "activation failed"):
            wetype_control_windows._run_on_sta_thread(
                lambda: (_ for _ in ()).throw(OSError("activation failed"))
            )

    def test_wait_is_bounded(self):
        release = threading.Event()
        try:
            with self.assertRaises(TimeoutError):
                wetype_control_windows._run_on_sta_thread(
                    lambda: release.wait(1.0),
                    timeout_seconds=0.01,
                )
        finally:
            release.set()

    def test_timed_out_request_blocks_new_sta_threads_until_it_finishes(self):
        release = threading.Event()
        started = threading.Event()

        def blocked_callback():
            started.set()
            release.wait(1.0)

        try:
            with self.assertRaises(TimeoutError):
                wetype_control_windows._run_on_sta_thread(
                    blocked_callback,
                    timeout_seconds=0.01,
                )
            self.assertTrue(started.is_set())
            with self.assertRaisesRegex(OSError, "still in progress"):
                wetype_control_windows._run_on_sta_thread(lambda: True)
        finally:
            release.set()
        deadline = __import__("time").monotonic() + 1.0
        while wetype_control_windows._STA_OPERATION_LOCK.locked():
            if __import__("time").monotonic() >= deadline:
                self.fail("timed-out STA operation did not release its gate")
            __import__("time").sleep(0.01)

    def test_timed_out_activation_restores_before_the_next_request_runs(self):
        first_activation_started = threading.Event()
        allow_first_activation = threading.Event()
        previous_restored = threading.Event()

        class BlockingProfileManager:
            def __init__(self):
                self.current = PREVIOUS_PROFILE
                self.activations = []
                self.target_activation_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def enum_profiles(self, language_id):
                return (WETYPE_PROFILE,) if language_id == 0x0804 else ()

            def get_active_profile(self):
                return self.current

            def activate_profile(self, active_profile):
                self.activations.append(active_profile)
                if wetype_control_windows._same_profile(
                    active_profile,
                    WETYPE_PROFILE,
                ):
                    self.target_activation_count += 1
                    if self.target_activation_count == 1:
                        first_activation_started.set()
                        allow_first_activation.wait(1.0)
                self.current = active_profile
                if wetype_control_windows._same_profile(
                    active_profile,
                    PREVIOUS_PROFILE,
                ):
                    previous_restored.set()

        manager = BlockingProfileManager()
        with mock.patch.object(
            wetype_control_windows,
            "_InputProfileManager",
            return_value=manager,
        ), mock.patch.object(
            wetype_control_windows,
            "_profile_registry_text",
            side_effect=profile_registry_text,
        ):
            with self.assertRaises(TimeoutError):
                wetype_control_windows._run_on_sta_thread(
                    lambda: wetype_control_windows._activate_wetype_for_voice_start(
                        _sleep=lambda _delay: None
                    ),
                    timeout_seconds=0.01,
                )
            self.assertTrue(first_activation_started.is_set())
            allow_first_activation.set()
            self.assertTrue(previous_restored.wait(1.0))

            self.assertTrue(
                wetype_control_windows._run_on_sta_thread(
                    lambda: wetype_control_windows._activate_wetype_for_voice_start(
                        _sleep=lambda _delay: None
                    )
                )
            )

        self.assertEqual(
            manager.activations,
            [WETYPE_PROFILE, PREVIOUS_PROFILE, WETYPE_PROFILE],
        )


class WeTypeVoiceControlTests(unittest.TestCase):
    def test_failed_selection_preparation_never_falls_back_to_press(self):
        press = mock.Mock()
        def failed(callback):
            raise wetype_control_windows.InputProfilePreparationError("selection timed out")
        control = wetype_control_windows.WeTypeVoiceControl(
            run_sta=failed, press_keys=press, capture_reader=None,
        )
        self.assertFalse(control.start(("lctrl", "lwin")))
        press.assert_not_called()

    VOICE_KEYS = ("lctrl", "lshift", "f9")

    def tearDown(self):
        wetype_control_windows.set_diagnostic_trace(None)

    @staticmethod
    def make_controller(**overrides):
        options = {
            "activate_profile": lambda: True,
            "run_sta": lambda callback: callback(),
            "press_keys": lambda _keys: None,
            "release_keys": lambda _keys: None,
            "capture_reader": None,
        }
        options.update(overrides)
        return wetype_control_windows.WeTypeVoiceControl(**options)

    def test_start_activates_on_sta_then_presses_configured_shortcut(self):
        calls = []
        controller = self.make_controller(
            activate_profile=lambda: calls.append("activate") or True,
            run_sta=lambda callback: calls.append("sta") or callback(),
            press_keys=lambda keys: calls.append(("down", tuple(keys))),
        )

        self.assertTrue(controller.start(self.VOICE_KEYS))

        self.assertEqual(
            calls,
            [
                "sta",
                "activate",
                ("down", self.VOICE_KEYS),
            ],
        )
        self.assertTrue(controller.cleanup_pending)
        self.assertFalse(controller.completion_pending)

    def test_already_active_profile_still_sends_the_configured_shortcut(self):
        pressed = []
        controller = self.make_controller(
            activate_profile=lambda: False,
            press_keys=lambda keys: pressed.append(tuple(keys)),
        )

        self.assertTrue(controller.start(self.VOICE_KEYS))
        self.assertEqual(pressed, [self.VOICE_KEYS])

    def test_activation_failure_still_sends_the_configured_shortcut(self):
        pressed = []
        controller = self.make_controller(
            activate_profile=lambda: (_ for _ in ()).throw(OSError("failed")),
            press_keys=lambda keys: pressed.append(tuple(keys)),
        )

        self.assertTrue(controller.start(self.VOICE_KEYS))
        self.assertEqual(pressed, [self.VOICE_KEYS])
        self.assertTrue(controller.cleanup_pending)

    def test_completed_press_failure_does_not_leave_cleanup_pending(self):
        controller = self.make_controller(
            press_keys=lambda _keys: (_ for _ in ()).throw(OSError("failed"))
        )

        self.assertFalse(controller.start(self.VOICE_KEYS))
        self.assertFalse(controller.cleanup_pending)

    def test_incomplete_press_failure_is_retained_for_release_retry(self):
        releases = []
        controller = self.make_controller(
            press_keys=lambda _keys: (_ for _ in ()).throw(
                win32_input.InputCleanupIncompleteError("still down")
            ),
            release_keys=lambda keys: releases.append(tuple(keys)),
        )

        self.assertFalse(controller.start(self.VOICE_KEYS))
        self.assertTrue(controller.cleanup_pending)
        self.assertTrue(controller.stop())
        self.assertEqual(
            releases,
            [self.VOICE_KEYS],
        )
        self.assertFalse(controller.cleanup_pending)

    def test_stop_releases_the_started_shortcut_and_is_idempotent(self):
        releases = []
        controller = self.make_controller(
            release_keys=lambda keys: releases.append(tuple(keys))
        )
        self.assertTrue(controller.start(self.VOICE_KEYS))

        self.assertTrue(controller.stop())
        self.assertTrue(controller.stop())

        self.assertEqual(
            releases,
            [self.VOICE_KEYS],
        )
        self.assertFalse(controller.cleanup_pending)

    def test_failed_release_stays_pending_and_blocks_a_new_start(self):
        release_results = [
            win32_input.InputCleanupIncompleteError("right Alt still down"),
            None,
        ]
        activations = []

        def release(_keys):
            result = release_results.pop(0)
            if result is not None:
                raise result

        controller = self.make_controller(
            activate_profile=lambda: activations.append(True) or True,
            release_keys=release,
        )
        self.assertTrue(controller.start(self.VOICE_KEYS))

        self.assertFalse(controller.stop())
        self.assertTrue(controller.cleanup_pending)
        self.assertFalse(controller.start(("ralt",)))
        self.assertEqual(len(activations), 1)
        self.assertTrue(controller.stop())
        self.assertFalse(controller.cleanup_pending)

    def test_swallowed_right_alt_up_remains_reachable_for_later_cleanup(self):
        ralt = win32_input.win32_keys.VK_CODES["ralt"]
        confirmation = mock.Mock(generation=7, installation_epoch=11)
        guard = win32_input.voice_key_physicalizer_windows.PhysicalReleaseGuard(
            generation=7,
            installation_epoch=11,
            callback_revision=3,
        )
        release_calls = []

        def state_query(vk):
            if len(release_calls) != 1:
                return False
            return win32_input._real_async_key_state_observation(
                vk,
                _context_query=lambda: win32_input._real_key_state_query_context(
                    _desktop_query=lambda: "Default",
                    _current_integrity_query=lambda: 8192,
                    _foreground_query=lambda: (10, 20, 12288),
                ),
                _query=lambda _vk: 0,
            )

        def release(keys):
            release_calls.append(tuple(keys))
            win32_input._ensure_right_alt_release_completed(
                ralt,
                confirmation,
                _state_query=state_query,
                _guard_query=lambda _vk: guard,
                _ambiguous_owner_query=lambda _vk: False,
                _sleep=lambda _seconds: None,
                _clock=iter((0.0, 1.0)).__next__,
            )

        controller = self.make_controller(release_keys=release)
        self.assertTrue(controller.start(self.VOICE_KEYS))

        self.assertFalse(controller.stop())
        self.assertTrue(controller.cleanup_pending)
        self.assertFalse(controller.start(("ralt",)))
        self.assertTrue(controller.stop())
        self.assertEqual(release_calls, [self.VOICE_KEYS, self.VOICE_KEYS])
        self.assertFalse(controller.cleanup_pending)

    def test_generation_advances_only_when_a_new_session_begins(self):
        controller = self.make_controller()

        self.assertTrue(controller.start(self.VOICE_KEYS))
        first = controller.current_generation
        self.assertFalse(controller.start(("ralt",)))
        self.assertEqual(controller.current_generation, first)
        self.assertTrue(controller.stop())
        self.assertTrue(controller.start(("ralt",)))
        self.assertEqual(controller.current_generation, first + 1)

    def test_empty_shortcut_is_rejected_before_profile_activation(self):
        activations = []
        controller = self.make_controller(
            activate_profile=lambda: activations.append(True) or True
        )

        self.assertFalse(controller.start(()))
        self.assertEqual(activations, [])
        self.assertFalse(controller.cleanup_pending)

    def test_controller_has_no_status_bar_or_mouse_click_fallback(self):
        source = source_text(wetype_control_windows)
        self.assertNotIn("wetype.statusbar.window", source)
        self.assertNotIn("PostMessageW", source)
        self.assertNotIn("WM_LBUTTON", source)

    def test_registration_failure_keeps_pressed_keys_owned_for_cleanup(self):
        released = []
        controller = self.make_controller(
            on_started=mock.Mock(side_effect=OSError("registration failed")),
            release_keys=lambda keys: released.append(tuple(keys)),
        )
        self.assertFalse(controller.start(self.VOICE_KEYS))
        self.assertTrue(controller.cleanup_pending)
        self.assertFalse(controller.confirmation_pending)
        self.assertTrue(controller.stop())
        self.assertEqual(released, [self.VOICE_KEYS])


class WeTypeCaptureObservationTests(unittest.TestCase):
    KEYS = ("lctrl", "lwin")
    CAPTURE = wetype_control_windows.voice_playback_session_windows.CaptureSession("mic", "new", 42, 1)

    def setUp(self):
        self.clock = 0.0
        self.edges = []
        self.confirmations = []
        self.workers = []
        self.controls = []
        self.time_patch = mock.patch.object(wetype_control_windows.time, "monotonic", side_effect=lambda: self.clock)
        self.time_patch.start()

    def tearDown(self):
        for control in self.controls:
            control.stop()
        for worker in self.workers:
            worker.join(1)
            self.assertFalse(worker.is_alive(), "observer did not settle")
        self.time_patch.stop()
        wetype_control_windows.set_diagnostic_trace(None)

    def advance(self, delay):
        self.clock += delay

    def wait_for_cancel(self, cancelled, delay):
        self.advance(delay)
        return cancelled.is_set()

    def make_control(self, **overrides):
        down = [False]
        def press(keys):
            self.edges.append(("down", tuple(keys), self.clock))
            down[0] = True
        def release(keys):
            self.edges.append(("up", tuple(keys), self.clock))
            down[0] = False
        def spawn(**options):
            thread = threading.Thread(**options)
            self.workers.append(thread)
            return thread
        options = dict(
            capture_reader=lambda: (self.CAPTURE,) if down[0] else (),
            press_keys=press, release_keys=release,
            on_confirmation=lambda gen, ok: self.confirmations.append((gen, ok)),
            thread_factory=spawn, wait_for_cancel=self.wait_for_cancel, logger=mock.Mock(),
        )
        options.update(overrides)
        control = WeTypeVoiceControlTests.make_controller(**options)
        self.controls.append(control)
        return control

    def settle(self):
        for worker in self.workers:
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_basic_hold_confirms_without_repress_and_ends_on_release(self):
        registered = []
        control = self.make_control(on_started=lambda gen: registered.append(gen))
        self.assertTrue(control.start(self.KEYS))
        self.settle()
        self.assertEqual(registered, [1])
        self.assertEqual(self.confirmations, [(1, True)])
        self.assertEqual([edge[0] for edge in self.edges], ["down"])
        self.clock = 15.0
        self.assertTrue(control.stop())
        self.assertEqual([edge[0] for edge in self.edges], ["down", "up"])
        self.assertFalse(control.cleanup_pending)

    def test_late_capture_uses_original_baseline_and_does_not_repress(self):
        for start_at in (4, 8.5, 12):
            with self.subTest(start_at=start_at):
                self.clock = 0.0
                self.edges.clear()
                self.confirmations.clear()
                guard = mock.Mock()
                guard.restore_if_muted.return_value = "restored"
                control = self.make_control(
                    capture_reader=lambda: (self.CAPTURE,) if self.clock >= start_at else (),
                    prepare_playback_mute_guard=lambda: guard,
                )
                self.assertTrue(control.start(self.KEYS))
                self.settle()
                self.assertEqual(self.confirmations, [(1, True)])
                self.assertGreaterEqual(self.clock, start_at)
                self.assertEqual([edge[0] for edge in self.edges], ["down"])
                guard.restore_if_muted.assert_called_once_with()
                self.assertTrue(control.stop())

    def test_missing_old_or_ambiguous_capture_never_interrupts_hold(self):
        other = wetype_control_windows.voice_playback_session_windows.CaptureSession("mic", "other", 43, 1)
        for kind in ("empty", "old", "ambiguous", "baseline_error", "query_error"):
            with self.subTest(kind=kind):
                self.clock = 0.0
                self.confirmations.clear()
                self.edges.clear()
                reads = []
                def read():
                    reads.append(self.clock)
                    if kind == "baseline_error" or (kind == "query_error" and len(reads) > 1):
                        raise OSError("metadata unavailable")
                    if kind == "old":
                        return (self.CAPTURE,)
                    if kind == "ambiguous" and len(reads) > 1:
                        return (self.CAPTURE, other)
                    return ()
                guard = mock.Mock()
                def wait(cancelled, delay):
                    self.advance(delay)
                    if self.clock >= 15:
                        # The device ends this hold, never the observer.
                        self.assertEqual([edge[0] for edge in self.edges], ["down"])
                        control.stop()
                    return cancelled.is_set()
                control = self.make_control(capture_reader=read, prepare_playback_mute_guard=lambda: guard,
                                            wait_for_cancel=wait)
                self.assertTrue(control.start(self.KEYS))
                self.settle()
                self.assertEqual(self.confirmations, [(1, False)] if kind == "baseline_error" else [])
                self.assertFalse(control.confirmation_pending)
                self.assertLessEqual(len(reads), 69)
                guard.restore_if_muted.assert_not_called()
                self.assertTrue(control.stop())
                self.assertEqual([edge[0] for edge in self.edges], ["down", "up"])

    def test_transient_query_error_does_not_reset_baseline(self):
        read = mock.Mock(side_effect=[(), OSError("busy"), (self.CAPTURE,)])
        control = self.make_control(capture_reader=read)
        control.start(self.KEYS)
        self.settle()
        self.assertEqual(self.confirmations, [(1, True)])

    def test_slow_queries_have_no_catch_up_burst_and_can_confirm_late(self):
        reads = []
        def read():
            reads.append(self.clock)
            if len(reads) > 1:
                self.clock += .6
            return (self.CAPTURE,) if self.clock >= 4 else ()
        control = self.make_control(capture_reader=read)
        control.start(self.KEYS)
        self.settle()
        self.assertEqual(self.confirmations, [(1, True)])
        self.assertLessEqual(len(reads), 7)
        self.assertTrue(all(b - a >= .049 for a, b in zip(reads, reads[1:])))

    def test_blocked_baseline_is_bounded_and_cannot_spawn_more_workers(self):
        entered, proceed = threading.Event(), threading.Event()
        def read():
            entered.set()
            proceed.wait(2)
            return ()
        control = self.make_control(capture_reader=read)
        try:
            with mock.patch.object(wetype_control_windows, "_WETYPE_BASELINE_WAIT_SECONDS", .02):
                self.assertTrue(control.start(self.KEYS))
            self.assertTrue(entered.is_set())
            self.assertEqual(self.confirmations, [(1, False)])
            self.assertTrue(control.stop())
            self.assertTrue(control.start(self.KEYS))
            self.assertEqual(self.confirmations, [(1, False), (2, False)])
            self.assertEqual(len(self.workers), 1)
        finally:
            proceed.set()
            self.settle()
        self.assertFalse(control.confirmation_pending)
        self.assertTrue(control.cleanup_pending)

    def test_blocked_post_press_query_does_not_lock_release_or_touch_next_hold(self):
        entered, proceed = threading.Event(), threading.Event()
        count = [0]
        def read():
            count[0] += 1
            if count[0] == 1:
                return ()
            entered.set()
            proceed.wait(2)
            return (self.CAPTURE,)
        guard = mock.Mock()
        control = self.make_control(capture_reader=read, prepare_playback_mute_guard=lambda: guard)
        try:
            self.assertTrue(control.start(self.KEYS))
            self.assertTrue(entered.wait(1))
            self.assertTrue(control.stop())
            self.assertTrue(control.start(self.KEYS))
            self.assertEqual(self.confirmations, [(2, False)])
            self.assertEqual(len(self.workers), 1)
        finally:
            proceed.set()
            self.settle()
        guard.restore_if_muted.assert_not_called()
        self.assertEqual(self.confirmations, [(2, False)])
        self.assertTrue(control.cleanup_pending)

    def test_capture_returning_after_release_cannot_confirm_or_unmute(self):
        count = [0]
        def read():
            count[0] += 1
            if count[0] == 1:
                return ()
            self.clock = 4
            control.stop()
            return (self.CAPTURE,)
        guard = mock.Mock()
        control = self.make_control(capture_reader=read, prepare_playback_mute_guard=lambda: guard)
        control.start(self.KEYS)
        self.settle()
        self.assertEqual(self.confirmations, [])
        guard.restore_if_muted.assert_not_called()

    def test_thread_failure_is_unknown_and_keeps_key_ownership(self):
        factory = mock.Mock(side_effect=RuntimeError("no thread"))
        control = self.make_control(thread_factory=factory)
        self.assertTrue(control.start(self.KEYS))
        self.assertEqual(self.confirmations, [(1, False)])
        self.assertFalse(control.confirmation_pending)
        self.assertTrue(control.stop())

    def test_release_before_first_probe_cancels_confirmation(self):
        control = self.make_control(wait_for_cancel=lambda event, delay: control.stop())
        self.assertTrue(control.start(self.KEYS))
        self.settle()
        self.assertEqual(self.confirmations, [])
        self.assertFalse(control.cleanup_pending)

    def test_failed_key_down_or_registration_cancels_observer(self):
        for target in ("press_keys", "on_started"):
            with self.subTest(target=target):
                control = self.make_control(**{target: mock.Mock(side_effect=OSError("failed"))})
                self.assertFalse(control.start(self.KEYS))
                self.settle()
                self.assertFalse(control.confirmation_pending)
                self.assertEqual(self.confirmations, [])
                self.assertTrue(control.stop())

    def test_cancel_during_baseline_does_not_deliver_keys(self):
        cancelled = threading.Event()
        def read():
            cancelled.set()
            return ()
        control = self.make_control(capture_reader=read)
        self.assertFalse(control.start(self.KEYS, cancelled=cancelled.is_set))
        self.settle()
        self.assertEqual(self.edges, [])
        self.assertFalse(control.cleanup_pending)

    def test_playback_baseline_precedes_key_down_and_restores_once_per_hold(self):
        calls = []
        guard = mock.Mock()
        guard.restore_if_muted.return_value = "restored"
        control = self.make_control(
            prepare_playback_mute_guard=lambda: calls.append("baseline") or guard,
            press_keys=lambda _: calls.append("down"),
            capture_reader=lambda: (self.CAPTURE,) if self.clock > 0 else ())
        for _ in range(2):
            self.clock = 0.0
            control.start(self.KEYS)
            self.settle()
            control.stop()
        self.assertEqual(calls, ["baseline", "down", "baseline", "down"])
        self.assertEqual(guard.restore_if_muted.call_count, 2)
        self.assertEqual(self.confirmations, [(1, True), (2, True)])

    def test_late_mute_window_stops_after_restore_or_session_change(self):
        for results, expected in ((["waiting", "restored"], 2), (["waiting"] * 16, 16), (["session_changed"], 1)):
            with self.subTest(results=results):
                guard = mock.Mock()
                guard.restore_if_muted.side_effect = results
                control = self.make_control(prepare_playback_mute_guard=lambda: guard)
                control.start(self.KEYS)
                self.settle()
                self.assertEqual(guard.restore_if_muted.call_count, expected)
                control.stop()

    def test_guard_errors_cannot_break_confirmation_or_release(self):
        for baseline_error in (False, True):
            with self.subTest(baseline_error=baseline_error):
                guard = mock.Mock()
                guard.restore_if_muted.side_effect = OSError("session gone")
                prepare = mock.Mock(return_value=guard)
                if baseline_error:
                    prepare.side_effect = OSError("unavailable")
                control = self.make_control(prepare_playback_mute_guard=prepare)
                control.start(self.KEYS)
                self.settle()
                self.assertTrue(self.confirmations[-1][1])
                self.assertTrue(control.stop())

    def test_failed_release_stops_mute_recovery_and_only_retries_key_up(self):
        guard = mock.Mock()
        guard.restore_if_muted.return_value = "waiting"
        release = mock.Mock(side_effect=[OSError("busy"), None])
        def wait(cancelled, delay):
            self.advance(delay)
            if delay == .1:
                self.assertFalse(control.stop())
            return cancelled.is_set()
        control = self.make_control(prepare_playback_mute_guard=lambda: guard, release_keys=release,
                                    wait_for_cancel=wait)
        control.start(self.KEYS)
        self.settle()
        self.assertTrue(control.cleanup_pending)
        self.assertFalse(control.start(self.KEYS))
        self.assertTrue(control.stop())
        self.assertEqual(release.call_count, 2)
        self.assertEqual(len(self.edges), 1)
        guard.restore_if_muted.assert_called_once_with()

    def test_setter_and_key_release_are_serialized(self):
        entered, proceed, stopping = threading.Event(), threading.Event(), threading.Event()
        calls = []
        guard = mock.Mock()
        def restore():
            entered.set()
            proceed.wait(2)
            calls.append("restore")
            return "restored"
        guard.restore_if_muted.side_effect = restore
        control = self.make_control(prepare_playback_mute_guard=lambda: guard, release_keys=lambda _: calls.append("up"))
        stopper = threading.Thread(target=lambda: (stopping.set(), control.stop()))
        try:
            control.start(self.KEYS)
            self.assertTrue(entered.wait(1))
            stopper.start()
            self.assertTrue(stopping.wait(1))
            self.assertEqual(calls, [])
        finally:
            proceed.set()
            stopper.join(1)
            self.settle()
        self.assertEqual(calls, ["restore", "up"])
        control._protect_playback_session(1, guard)
        guard.restore_if_muted.assert_called_once_with()


class WeTypeObservationHandoffTests(unittest.TestCase):
    def test_rapid_reopen_wakes_wait_and_hands_off_before_new_baseline(self):
        # Use real Event waits; pausing the cancelled worker's unwind makes
        # this exercise the handoff too, not just favourable scheduling.
        for phase in ("capture", "mute"):
            with self.subTest(phase=phase):
                entered, cancelled_wait, unwind = (threading.Event() for _ in range(3))
                workers, edges, confirmations, baseline_generations = [], [], [], []
                down = False
                generation = 0
                guards = [mock.Mock(), mock.Mock()]
                guards[0].restore_if_muted.return_value = "waiting"
                guards[1].restore_if_muted.return_value = "restored"
                capture = wetype_control_windows.voice_playback_session_windows.CaptureSession("mic", "new", 42, 1)

                def press(keys):
                    nonlocal down, generation
                    generation += 1
                    down = True
                    edges.append("down")

                def release(keys):
                    nonlocal down
                    down = False
                    edges.append("up")

                def read():
                    if not down:
                        baseline_generations.append(generation + 1)
                    return (capture,) if down and (generation == 2 or phase == "mute") else ()

                def wait(cancelled, delay):
                    target = .25 if phase == "capture" else .1
                    if generation == 1 and delay == target:
                        entered.set()
                        result = cancelled.wait(1)
                        if result:
                            cancelled_wait.set()
                            unwind.wait(1)
                        return result
                    return cancelled.wait(delay)

                def spawn(**options):
                    worker = threading.Thread(**options)
                    workers.append(worker)
                    return worker

                control = WeTypeVoiceControlTests.make_controller(
                    capture_reader=read, press_keys=press, release_keys=release,
                    on_confirmation=lambda gen, ok: confirmations.append((gen, ok)),
                    prepare_playback_mute_guard=lambda: guards[generation],
                    wait_for_cancel=wait, thread_factory=spawn, logger=mock.Mock(),
                )
                timer = threading.Timer(.04, unwind.set)
                try:
                    self.assertTrue(control.start(("lctrl", "lwin")))
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(control.stop())
                    self.assertTrue(cancelled_wait.wait(1), "release did not wake the observer")
                    # No join between release and reopen. The previous worker
                    # still owns the observer gate when the new start arrives.
                    self.assertTrue(workers[0].is_alive())
                    timer.start()
                    self.assertTrue(control.start(("lctrl", "lwin")))
                    for worker in workers:
                        worker.join(1)
                    self.assertTrue(all(not worker.is_alive() for worker in workers))
                    self.assertEqual(len(workers), 2)
                    self.assertEqual(baseline_generations, [1, 2])
                    self.assertEqual(confirmations, ([(1, True)] if phase == "mute" else []) + [(2, True)])
                    self.assertEqual(edges, ["down", "up", "down"])
                    self.assertEqual(guards[0].restore_if_muted.call_count, 1 if phase == "mute" else 0)
                    guards[1].restore_if_muted.assert_called_once_with()
                finally:
                    unwind.set()
                    control.stop()
                    if timer.ident is not None:
                        timer.join(1)
                    for worker in workers:
                        worker.join(1)


class DoubaoVoiceControlTests(unittest.TestCase):
    def test_start_timeout_cancels_starting_generation_until_sta_settles(self):
        activation_started = threading.Event()
        allow_activation = threading.Event()
        activation_count = 0
        pressed = []

        def activate():
            nonlocal activation_count
            activation_count += 1
            if activation_count == 1:
                activation_started.set()
                allow_activation.wait(1.0)
            return True

        controller = wetype_control_windows.DoubaoVoiceControl(
            activate_profile=activate,
            press_keys=lambda keys: pressed.append(tuple(keys)),
            release_keys=lambda _keys: None,
        )
        try:
            with mock.patch.object(
                wetype_control_windows,
                "_STA_RESULT_TIMEOUT_SECONDS",
                0.01,
            ):
                self.assertFalse(controller.start(("ralt",)))
            self.assertTrue(activation_started.is_set())
            self.assertEqual(pressed, [])
            self.assertTrue(controller.cleanup_pending)
            self.assertFalse(controller.stop())
        finally:
            allow_activation.set()

        deadline = time.monotonic() + 1.0
        while wetype_control_windows._STA_OPERATION_LOCK.locked():
            if time.monotonic() >= deadline:
                self.fail("timed-out STA start did not settle")
            time.sleep(0.01)

        self.assertFalse(controller.cleanup_pending)
        self.assertTrue(controller.stop())
        self.assertTrue(controller.start(("ralt",)))
        self.assertEqual(pressed, [("ralt",)])
        self.assertTrue(controller.stop())

    def test_prepare_timeout_retains_sta_owner_until_worker_settles(self):
        activation_started = threading.Event()
        allow_activation = threading.Event()
        pressed = []

        def activate():
            activation_started.set()
            allow_activation.wait(1.0)
            return True

        controller = wetype_control_windows.DoubaoVoiceControl(
            activate_profile=activate,
            press_keys=lambda keys: pressed.append(tuple(keys)),
        )
        try:
            with mock.patch.object(
                wetype_control_windows,
                "_STA_RESULT_TIMEOUT_SECONDS",
                0.01,
            ):
                prepared = controller.prepare(("ralt",))
            self.assertTrue(activation_started.is_set())
            self.assertIsNotNone(prepared)
            self.assertFalse(prepared.ready)
            self.assertFalse(prepared.settled)
            controller.cancel_prepared(prepared)
            self.assertFalse(controller.dispatch_prepared(prepared))
        finally:
            allow_activation.set()

        self.assertTrue(prepared.wait_settled(1.0))
        self.assertEqual(pressed, [])
        self.assertFalse(controller.cleanup_pending)

    def test_start_activates_doubao_then_owns_the_hold_until_stop(self):
        calls = []
        controller = wetype_control_windows.DoubaoVoiceControl(
            activate_profile=lambda: calls.append("activate") or True,
            run_sta=lambda callback: calls.append("sta") or callback(),
            press_keys=lambda keys: calls.append(("down", tuple(keys))),
            release_keys=lambda keys: calls.append(("up", tuple(keys))),
        )

        self.assertTrue(controller.start(("ralt",)))
        self.assertTrue(controller.cleanup_pending)
        self.assertTrue(controller.stop())

        self.assertEqual(
            calls,
            ["sta", "activate", ("down", ("ralt",)), ("up", ("ralt",))],
        )
        self.assertFalse(controller.cleanup_pending)

    def test_activation_failure_still_blocks_doubao_shortcut(self):
        pressed = []
        controller = wetype_control_windows.DoubaoVoiceControl(
            activate_profile=lambda: (_ for _ in ()).throw(OSError("failed")),
            run_sta=lambda callback: callback(),
            press_keys=lambda keys: pressed.append(tuple(keys)),
        )

        self.assertFalse(controller.start(("ralt",)))
        self.assertEqual(pressed, [])
        self.assertFalse(controller.cleanup_pending)


if __name__ == "__main__":
    unittest.main()
