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


class WeTypeMicTimestampTests(unittest.TestCase):
    OLD_RECORD = "C:#Tencent#WeType#2.1.2.13#wetype_update.exe"
    CURRENT_RECORD = "C:#Tencent#WeType#2.1.3.18#wetype_update.exe"

    @staticmethod
    @contextmanager
    def registry(records, *, unavailable=False):
        registry = mock.Mock()
        root = mock.MagicMock()
        root.__enter__.return_value = root

        def open_key(parent, name):
            if parent is registry.HKEY_CURRENT_USER:
                if unavailable:
                    raise PermissionError("microphone history unavailable")
                return root
            key = mock.MagicMock()
            key.__enter__.return_value = name
            return key

        def enum_key(_root, index):
            try:
                return list(records)[index]
            except IndexError:
                raise OSError("no more records") from None

        def query_value(key, name):
            if name != "LastUsedTimeStart":
                raise AssertionError(name)
            value = records[key]
            if isinstance(value, Exception):
                raise value
            return value, 11

        registry.OpenKey.side_effect = open_key
        registry.EnumKey.side_effect = enum_key
        registry.QueryValueEx.side_effect = query_value
        with mock.patch.dict("sys.modules", {"winreg": registry}), mock.patch.object(
            wetype_control_windows.os, "name", "nt"
        ):
            yield registry

    def test_latest_record_wins_independently_of_version_enumeration_order(self):
        entries = [(self.OLD_RECORD, 100), (self.CURRENT_RECORD, 200)]
        for ordered in (entries, list(reversed(entries))):
            with self.subTest(ordered=ordered), self.registry(dict(ordered)):
                self.assertEqual(wetype_control_windows._wetype_mic_start_timestamp(), 200)

    def test_unrelated_application_cannot_confirm_wetype(self):
        with self.registry({"C:#DoubaoIME#ImeService.exe": 999, self.CURRENT_RECORD: 200}):
            self.assertEqual(wetype_control_windows._wetype_mic_start_timestamp(), 200)

    def test_invalid_old_record_does_not_hide_a_valid_later_record(self):
        for invalid in (None, "broken", b"200", True, -1, 0, PermissionError("denied")):
            with self.subTest(invalid=invalid), self.registry(
                {self.OLD_RECORD: invalid, self.CURRENT_RECORD: 200}
            ):
                self.assertEqual(wetype_control_windows._wetype_mic_start_timestamp(), 200)

    def test_missing_or_unreadable_history_stays_unconfirmed(self):
        for records, unavailable in (({}, False), ({self.OLD_RECORD: 0}, False), ({}, True)):
            with self.subTest(records=records, unavailable=unavailable), self.registry(
                records, unavailable=unavailable
            ):
                self.assertIsNone(wetype_control_windows._wetype_mic_start_timestamp())

    def test_current_version_capture_does_not_retrigger_an_active_hold(self):
        records = {self.OLD_RECORD: 100, self.CURRENT_RECORD: 200}
        pressed, released, confirmations = [], [], []
        revive = mock.Mock(return_value=True)

        def press(keys):
            pressed.append(tuple(keys))
            records[self.CURRENT_RECORD] = 201

        with self.registry(records):
            controller = WeTypeVoiceControlTests.make_controller(
                mic_start_reader=wetype_control_windows._wetype_mic_start_timestamp,
                press_keys=press,
                release_keys=lambda keys: released.append(tuple(keys)),
                revive_profile=revive,
                on_confirmation=lambda generation, ok: confirmations.append((generation, ok)),
                sleep=lambda _delay: None,
                thread_factory=_ImmediateThread,
            )
            self.assertTrue(controller.start(WeTypeVoiceControlTests.VOICE_KEYS))
            self.assertEqual(confirmations, [(1, True)])
            self.assertEqual(pressed, [WeTypeVoiceControlTests.VOICE_KEYS])
            self.assertEqual(released, [])
            revive.assert_not_called()
            self.assertTrue(controller.stop())
            self.assertEqual(released, [WeTypeVoiceControlTests.VOICE_KEYS])


class WeTypeVoiceControlTests(unittest.TestCase):
    def test_failed_selection_preparation_never_falls_back_to_press(self):
        press = mock.Mock()
        def failed(callback):
            raise wetype_control_windows.InputProfilePreparationError("selection timed out")
        control = wetype_control_windows.WeTypeVoiceControl(
            run_sta=failed, press_keys=press, mic_start_reader=None,
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
            "mic_start_reader": None,
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

    def test_first_microphone_confirmation_succeeds_without_recovery(self):
        confirmations = []
        readings = iter((10, 11))
        controller = self.make_controller(
            mic_start_reader=lambda: next(readings),
            on_confirmation=lambda generation, success: confirmations.append(
                (generation, success)
            ),
            sleep=lambda _delay: None,
            thread_factory=_ImmediateThread,
        )

        self.assertTrue(controller.start(self.VOICE_KEYS))

        self.assertEqual(confirmations, [(1, True)])
        self.assertFalse(controller.confirmation_pending)

    def test_microphone_confirmation_recovers_once_then_succeeds(self):
        calls = []
        readings = iter((10,) * 13 + (11,))
        controller = self.make_controller(
            mic_start_reader=lambda: next(readings),
            on_confirmation=lambda generation, success: calls.append(
                ("confirmed", generation, success)
            ),
            revive_profile=lambda: calls.append("revive") or True,
            press_keys=lambda keys: calls.append(("down", tuple(keys))),
            release_keys=lambda keys: calls.append(("up", tuple(keys))),
            sleep=lambda _delay: None,
            thread_factory=_ImmediateThread,
        )

        self.assertTrue(controller.start(self.VOICE_KEYS))

        self.assertEqual(
            calls,
            [
                ("down", self.VOICE_KEYS),
                "revive",
                ("up", self.VOICE_KEYS),
                ("down", self.VOICE_KEYS),
                ("confirmed", 1, True),
            ],
        )

    def test_microphone_confirmation_fails_after_all_recovery_retries(self):
        confirmations = []
        revivals = []
        controller = self.make_controller(
            mic_start_reader=lambda: 10,
            on_confirmation=lambda generation, success: confirmations.append(
                (generation, success)
            ),
            revive_profile=lambda: revivals.append(True) or True,
            sleep=lambda _delay: None,
            thread_factory=_ImmediateThread,
        )

        self.assertTrue(controller.start(self.VOICE_KEYS))

        self.assertEqual(confirmations, [(1, False)])
        self.assertEqual(len(revivals), 3)

    def test_fast_confirmation_registers_before_result_and_stops_after_ready(self):
        clock, calls = [0.0], []
        controller = self.make_controller(
            mic_start_reader=lambda: 11 if clock[0] >= .05 else 10,
            on_started=lambda generation: calls.append(("registered", generation)),
            on_confirmation=lambda generation, ok: calls.append(("confirmed", generation, ok, clock[0])),
            sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
            thread_factory=_ImmediateThread,
        )
        with mock.patch.object(wetype_control_windows.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(controller.start(self.VOICE_KEYS))
        self.assertEqual(calls, [("registered", 1), ("confirmed", 1, True, .05)])
        self.assertFalse(controller.confirmation_pending)
        self.assertTrue(controller.stop())

    def test_never_ready_has_bounded_queries_and_keeps_original_retry_times(self):
        clock, reads, retries, confirmations = [0.0], [], [], []
        controller = self.make_controller(
            mic_start_reader=lambda: reads.append(clock[0]) or 10,
            revive_profile=lambda: retries.append(clock[0]) or True,
            on_confirmation=lambda gen, ok: confirmations.append((gen, ok)),
            sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
            thread_factory=_ImmediateThread,
        )
        with mock.patch.object(wetype_control_windows.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(controller.start(self.VOICE_KEYS))
        self.assertEqual(len(reads), 18)  # baseline + 10 fast + final + 3 * (baseline + final)
        self.assertEqual([round(t, 3) for t in retries], [.7, 3.4, 7.1])
        self.assertAlmostEqual(clock[0], 12.8)
        self.assertEqual(confirmations, [(1, False)])
        self.assertFalse(controller.confirmation_pending)
        self.assertTrue(controller.stop())
        self.assertFalse(controller.cleanup_pending)

    def test_slow_query_does_not_catch_up_with_burst_of_overdue_probes(self):
        clock, count = [0.0], [0]
        def read():
            count[0] += 1
            clock[0] += .6
            return 10
        controller = self.make_controller(
            mic_start_reader=read, revive_profile=None,
            sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
            thread_factory=_ImmediateThread,
        )
        with mock.patch.object(wetype_control_windows.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(controller.start(self.VOICE_KEYS))
        self.assertEqual(count[0], 3)  # baseline, one slow fast query, final check
        self.assertFalse(controller.confirmation_pending)
        self.assertTrue(controller.stop())

    def test_one_slow_query_does_not_run_remaining_probes_back_to_back(self):
        clock, reads = [0.0], []
        def read():
            reads.append(clock[0])
            if len(reads) == 2:
                clock[0] += .3
            return 10
        controller = self.make_controller(
            mic_start_reader=read, revive_profile=None,
            sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
            thread_factory=_ImmediateThread,
        )
        with mock.patch.object(wetype_control_windows.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(controller.start(self.VOICE_KEYS))
        self.assertTrue(all(b - a >= .049 for a, b in zip(reads, reads[1:])))
        self.assertLessEqual(len(reads), 8)
        self.assertTrue(controller.stop())

    def test_confirmation_thread_start_failure_does_not_leave_pending_flag(self):
        thread = mock.Mock()
        thread.start.side_effect = RuntimeError("thread unavailable")
        controller = self.make_controller(
            mic_start_reader=lambda: 10, thread_factory=lambda **_: thread)
        self.assertTrue(controller.start(self.VOICE_KEYS))
        self.assertFalse(controller.confirmation_pending)
        self.assertTrue(controller.stop())
        self.assertFalse(controller.cleanup_pending)

    def test_blocked_query_does_not_lock_release_or_clear_next_confirmation(self):
        entered, proceed = threading.Event(), threading.Event()
        pending, confirmations, reads = [], [], [0]
        def read():
            reads[0] += 1
            if reads[0] == 2:
                entered.set()
                if not proceed.wait(2):
                    raise TimeoutError("test query release missing")
            return 20 if reads[0] in (2, 4) else 10
        def spawn(**options):
            pending.append(options["target"])
            return mock.Mock()
        controller = self.make_controller(
            mic_start_reader=read, thread_factory=spawn, sleep=lambda _: None,
            on_confirmation=lambda gen, ok: confirmations.append((gen, ok)),
        )
        self.assertTrue(controller.start(self.VOICE_KEYS))
        old = threading.Thread(target=pending[0], daemon=True)
        old.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(controller.stop())
            self.assertFalse(controller.confirmation_pending)
            self.assertTrue(controller.start(self.VOICE_KEYS))
            self.assertTrue(controller.confirmation_pending)
        finally:
            proceed.set()
            old.join(1)
        self.assertFalse(old.is_alive())
        self.assertTrue(controller.confirmation_pending)
        self.assertEqual(confirmations, [])
        pending[1]()
        self.assertEqual(confirmations, [(2, True)])
        self.assertTrue(controller.stop())

    def test_confirmation_error_clears_pending_and_allows_next_hold_after_release(self):
        controller = self.make_controller(
            mic_start_reader=mock.Mock(side_effect=[10, OSError("query failed"), 10, 20]),
            sleep=lambda _: None, thread_factory=_ImmediateThread,
        )
        self.assertTrue(controller.start(self.VOICE_KEYS))
        self.assertFalse(controller.confirmation_pending)
        self.assertTrue(controller.stop())
        self.assertTrue(controller.start(self.VOICE_KEYS))
        self.assertFalse(controller.confirmation_pending)
        self.assertTrue(controller.stop())

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


class _ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self._target = target

    def start(self):
        self._target()


class WeTypePlaybackMuteProtectionTests(unittest.TestCase):
    def make_control(self, guard, **overrides):
        self.pending = []
        def thread_factory(**options):
            self.pending.append(options["target"])
            return mock.Mock()
        options = dict(
            prepare_playback_mute_guard=lambda: guard,
            mic_start_reader=mock.Mock(side_effect=[10, 20, 20, 30]),
            thread_factory=thread_factory,
            sleep=lambda _: None,
        )
        options.update(overrides)
        return WeTypeVoiceControlTests.make_controller(**options)

    def test_baseline_precedes_key_down_and_each_confirmed_hold_restores_once(self):
        calls = []
        guard = mock.Mock()
        guard.restore_if_muted.return_value = "restored"
        control = self.make_control(
            guard,
            prepare_playback_mute_guard=lambda: calls.append("baseline") or guard,
            press_keys=lambda _: calls.append("down"),
        )
        for _ in range(2):
            self.assertTrue(control.start(("f9",)))
            self.pending.pop(0)()
            self.assertTrue(control.stop())
        self.assertEqual(calls, ["baseline", "down", "baseline", "down"])
        self.assertEqual(guard.restore_if_muted.call_count, 2)

    def test_short_press_and_stale_confirmation_do_not_restore(self):
        guard = mock.Mock()
        control = self.make_control(guard)
        self.assertTrue(control.start(("f9",)))
        control.stop()
        self.assertTrue(control.start(("f9",)))
        self.pending[0]()
        guard.restore_if_muted.assert_not_called()
        control.stop()

    def test_no_mic_confirmation_does_not_restore(self):
        guard = mock.Mock()
        control = self.make_control(guard, mic_start_reader=lambda: 10, revive_profile=None)
        control.start(("f9",))
        self.pending[0]()
        guard.restore_if_muted.assert_not_called()
        control.stop()

    def test_late_mute_is_bounded_and_stops_after_first_restore(self):
        for results, expected in ((["waiting", "restored"], 2), (["waiting"] * 16, 16), (["session_changed"], 1)):
            with self.subTest(results=results):
                guard = mock.Mock()
                guard.restore_if_muted.side_effect = results
                control = self.make_control(guard)
                control.start(("f9",))
                self.pending[0]()
                self.assertEqual(guard.restore_if_muted.call_count, expected)
                control.stop()

    def test_release_during_wait_cancels_further_probes(self):
        guard = mock.Mock()
        guard.restore_if_muted.return_value = "waiting"
        control = self.make_control(guard, sleep=lambda delay: control.stop() if delay == 0.1 else None)
        control.start(("f9",))
        self.pending[0]()
        guard.restore_if_muted.assert_called_once_with()
        self.assertFalse(control.cleanup_pending)

    def test_guard_failure_does_not_break_key_release_or_confirmation(self):
        for baseline_failure in (False, True):
            with self.subTest(baseline_failure=baseline_failure):
                guard = mock.Mock()
                guard.restore_if_muted.side_effect = OSError("gone")
                factory = mock.Mock(return_value=guard)
                if baseline_failure:
                    factory.side_effect = OSError("unavailable")
                confirmation = mock.Mock()
                control = self.make_control(guard, prepare_playback_mute_guard=factory, on_confirmation=confirmation)
                control.start(("f9",))
                self.pending[0]()
                confirmation.assert_called_once_with(1, True)
                self.assertTrue(control.stop())

    def test_setter_and_key_release_are_serialized(self):
        entered, proceed, stopping = threading.Event(), threading.Event(), threading.Event()
        calls = []
        guard = mock.Mock()

        def restore():
            entered.set()
            if not proceed.wait(1.0):
                raise AssertionError("test did not release probe")
            calls.append("restore")
            return "restored"

        guard.restore_if_muted.side_effect = restore
        control = self.make_control(guard, release_keys=lambda _: calls.append("up"))
        control.start(("f9",))
        worker = threading.Thread(target=self.pending[0])
        stopper = threading.Thread(target=lambda: (stopping.set(), control.stop()))
        try:
            worker.start()
            self.assertTrue(entered.wait(1.0))
            stopper.start()
            self.assertTrue(stopping.wait(1.0))
            self.assertNotIn("up", calls)
        finally:
            proceed.set()
            worker.join(1.0)
            if stopper.ident is not None:
                stopper.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(calls, ["restore", "up"])
        control._protect_playback_session(1, guard)
        guard.restore_if_muted.assert_called_once_with()

    def test_unavailable_guard_or_failed_key_down_does_not_write(self):
        control = self.make_control(None)
        self.assertTrue(control.start(("f9",)))
        self.pending[0]()
        self.assertTrue(control.stop())
        guard = mock.Mock()
        control = self.make_control(guard, press_keys=mock.Mock(side_effect=OSError("failed")))
        self.assertFalse(control.start(("f9",)))
        self.assertEqual(self.pending, [])
        guard.restore_if_muted.assert_not_called()


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
