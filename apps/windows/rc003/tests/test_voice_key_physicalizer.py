from tests.source_contract import source_text
import threading
import types
import unittest
from unittest import mock

from ovb_rc003 import voice_key_physicalizer_windows as physicalizer


class VoiceKeyPhysicalizerDecisionTests(unittest.TestCase):
    def setUp(self):
        physicalizer._set_physical_tracker_active(
            True,
            _query=lambda _vk: False,
        )

    def tearDown(self):
        physicalizer.set_diagnostic_trace(None)
        physicalizer._set_physical_tracker_active(False)

    def test_physicalizes_only_marked_injected_right_alt(self):
        ticket = physicalizer.begin_marked_voice_event(False)
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=physicalizer.VK_RMENU,
            scanCode=0x38,
            flags=(
                physicalizer.LLKHF_INJECTED
                | physicalizer.LLKHF_LOWER_IL_INJECTED
            ),
            time=0,
            dwExtraInfo=ticket.marker,
        )

        self.assertTrue(physicalizer.physicalize_injected_event(event, False))
        self.assertEqual(event.flags, 0)
        self.assertEqual(event.dwExtraInfo, 0)
        physicalizer.complete_marked_voice_event(ticket, downstream_result=0)
        self.assertTrue(physicalizer.wait_for_marked_voice_event(ticket, 0.01))

    def test_release_guard_is_bound_to_current_tracker_generation(self):
        guard = physicalizer.snapshot_physical_release_guard(
            physicalizer.VK_RMENU
        )
        self.assertIsNotNone(guard)
        health = physicalizer.snapshot_health()
        self.assertEqual(guard.generation, health.generation)
        self.assertEqual(guard.installation_epoch, health.installation_epoch)

        physicalizer._set_physical_tracker_active(False)
        self.assertIsNone(
            physicalizer.snapshot_physical_release_guard(
                physicalizer.VK_RMENU
            )
        )

    def test_release_guard_rejects_physical_or_inflight_right_alt(self):
        physical_event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=physicalizer.VK_RMENU,
            scanCode=0x38,
            flags=physicalizer.LLKHF_EXTENDED,
            time=0,
            dwExtraInfo=0,
        )
        self.assertTrue(
            physicalizer.record_physical_key_event(
                physical_event,
                physicalizer.WM_SYSKEYDOWN,
            )
        )
        self.assertIsNone(
            physicalizer.snapshot_physical_release_guard(
                physicalizer.VK_RMENU
            )
        )
        self.assertTrue(
            physicalizer.record_physical_key_event(
                physical_event,
                physicalizer.WM_SYSKEYUP,
            )
        )

        generation = physicalizer._begin_physical_ralt_callback(physical_event)
        try:
            self.assertIsNone(
                physicalizer.snapshot_physical_release_guard(
                    physicalizer.VK_RMENU
                )
            )
        finally:
            physicalizer._end_physical_ralt_callback(generation)

    def test_physical_keys_and_unmarked_injected_keys_are_untouched(self):
        cases = (
            (0x26, 0, 0),
            (0x74, 0, 0),
            (
                physicalizer.VK_RMENU,
                physicalizer.LLKHF_INJECTED,
                0,
            ),
            (
                0x26,
                physicalizer.LLKHF_INJECTED,
                physicalizer.VOICE_EVENT_EXTRA_INFO,
            ),
        )
        for vk_code, flags, extra_info in cases:
            with self.subTest(vk_code=vk_code, flags=flags, extra_info=extra_info):
                event = physicalizer.KBDLLHOOKSTRUCT(
                    vkCode=vk_code,
                    scanCode=0,
                    flags=flags,
                    time=0,
                    dwExtraInfo=extra_info,
                )
                self.assertFalse(
                    physicalizer.physicalize_injected_event(event, False)
                )
                self.assertEqual(event.flags, flags)
                self.assertEqual(event.dwExtraInfo, extra_info)

    def test_hook_uses_exact_direction_correlation_without_waiting(self):
        source = source_text(physicalizer.VoiceKeyPhysicalizer._hookproc)

        self.assertIn("consume_rc003_direction_event", source)
        self.assertIn("return 1", source)
        self.assertNotIn("sleep", source)

    def test_hook_trace_failure_does_not_change_direction_consumption(self):
        trace = mock.Mock()
        trace.current_gesture.side_effect = RuntimeError("trace failed")
        physicalizer.set_diagnostic_trace(trace)
        gate = physicalizer.VoiceKeyPhysicalizer()
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=0x25,
            scanCode=0x4B,
            flags=physicalizer.LLKHF_EXTENDED,
            time=0,
            dwExtraInfo=0,
        )
        self.assertTrue(gate.record_rc003_direction_edge(0x25, 0x4B, True, True))
        user32 = mock.Mock()
        user32.CallNextHookEx = mock.Mock(return_value=0)

        with mock.patch.object(
            physicalizer.ctypes,
            "windll",
            types.SimpleNamespace(user32=user32),
        ):
            result = gate._hookproc(
                0,
                physicalizer.WM_KEYDOWN,
                physicalizer.ctypes.addressof(event),
            )

        self.assertEqual(result, 1)
        user32.CallNextHookEx.assert_not_called()

    def test_unmarked_right_alt_is_observed_only_during_matching_receipt(self):
        trace = mock.Mock()
        physicalizer.set_diagnostic_trace(trace)
        gate = physicalizer.VoiceKeyPhysicalizer()
        user32 = mock.Mock()
        user32.CallNextHookEx.return_value = 0
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=physicalizer.VK_RMENU, scanCode=0x38,
            flags=0x81, time=1234, dwExtraInfo=0)
        def invoke(message=physicalizer.WM_KEYUP):
            return gate._hookproc(0, message, physicalizer.ctypes.addressof(event))
        with mock.patch.object(physicalizer.ctypes, "windll", types.SimpleNamespace(user32=user32)):
            invoke()
            trace.emit.assert_not_called()
            receipt = physicalizer.begin_marked_voice_event(True)
            invoke(physicalizer.WM_KEYDOWN)  # Other edge is not correlated.
            event.vkCode = 0x41  # Ordinary typing is never added to this trace.
            invoke()
            trace.emit.assert_not_called()
            event.vkCode = physicalizer.VK_RMENU
            self.assertEqual(invoke(), 0)
            trace.emit.assert_called_once()
            details = trace.emit.call_args.kwargs
            self.assertEqual(details["awaiting_marker"], receipt.marker)
            self.assertEqual(details["receipt_correlation"], "pending_edge_only")
            self.assertEqual(details["event_time_ms"], 1234)
            self.assertEqual(details["decision"], "pass")
            self.assertFalse(receipt.marker_seen)
            self.assertFalse(receipt.confirmed)
            self.assertEqual(event.dwExtraInfo, 0)
            trace.emit.side_effect = RuntimeError("diagnostic output failed")
            self.assertEqual(invoke(), 0)  # Diagnostics cannot consume or confirm it.
            self.assertFalse(receipt.confirmed)
            physicalizer.cancel_marked_voice_event(receipt)
            trace.reset_mock()
            invoke()
            trace.emit.assert_not_called()

    def test_armed_rc003_direction_is_swallowed_through_release(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        down = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=0x25,
            scanCode=0x4B,
            flags=physicalizer.LLKHF_EXTENDED,
            time=0,
            dwExtraInfo=0,
        )
        up = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=0x25,
            scanCode=0x4B,
            flags=physicalizer.LLKHF_EXTENDED,
            time=0,
            dwExtraInfo=0,
        )

        self.assertTrue(gate.record_rc003_direction_edge(0x25, 0x4B, True, True))
        self.assertTrue(gate.consume_rc003_direction_event(down, True))
        self.assertTrue(gate.consume_rc003_direction_event(down, True))
        self.assertTrue(gate.record_rc003_direction_edge(0x25, 0x4B, True, False))
        self.assertTrue(gate.consume_rc003_direction_event(up, False))
        self.assertFalse(gate.consume_rc003_direction_event(down, True))

    def test_release_before_late_windows_edges_keeps_the_pair_owned(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=0x25,
            scanCode=0x4B,
            flags=physicalizer.LLKHF_EXTENDED,
            time=0,
            dwExtraInfo=0,
        )

        self.assertTrue(gate.record_rc003_direction_edge(0x25, 0x4B, True, True))
        self.assertTrue(gate.record_rc003_direction_edge(0x25, 0x4B, True, False))
        self.assertTrue(gate.consume_rc003_direction_event(event, True))
        self.assertTrue(gate.consume_rc003_direction_event(event, False))
        self.assertFalse(gate.consume_rc003_direction_event(event, True))

    def test_two_complete_device_taps_can_arrive_before_windows_edges(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=0x27,
            scanCode=0x4D,
            flags=physicalizer.LLKHF_EXTENDED,
            time=0,
            dwExtraInfo=0,
        )

        for _ in range(2):
            self.assertTrue(
                gate.record_rc003_direction_edge(0x27, 0x4D, True, True)
            )
            self.assertTrue(
                gate.record_rc003_direction_edge(0x27, 0x4D, True, False)
            )
        for _ in range(2):
            self.assertTrue(gate.consume_rc003_direction_event(event, True))
            self.assertTrue(gate.consume_rc003_direction_event(event, False))
        self.assertFalse(gate.consume_rc003_direction_event(event, True))

    def test_windows_down_before_device_arm_keeps_its_release_visible(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=0x26,
            scanCode=0x48,
            flags=physicalizer.LLKHF_EXTENDED,
            time=0,
            dwExtraInfo=0,
        )

        self.assertFalse(gate.consume_rc003_direction_event(event, True))
        self.assertTrue(gate.record_rc003_direction_edge(0x26, 0x48, True, True))
        self.assertTrue(gate.record_rc003_direction_edge(0x26, 0x48, True, False))
        self.assertFalse(gate.consume_rc003_direction_event(event, False))
        self.assertFalse(gate.consume_rc003_direction_event(event, True))

    def test_direction_correlation_keeps_unrelated_keyboard_edges(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        gate.record_rc003_direction_edge(0x26, 0x48, True, True)
        cases = (
            (0x26, 0x48, 0),
            (0x26, 0x47, physicalizer.LLKHF_EXTENDED),
            (
                0x26,
                0x48,
                physicalizer.LLKHF_EXTENDED | physicalizer.LLKHF_INJECTED,
            ),
            (ord("A"), 0x1E, 0),
        )
        for vk_code, scan_code, flags in cases:
            with self.subTest(vk_code=vk_code, scan_code=scan_code, flags=flags):
                event = physicalizer.KBDLLHOOKSTRUCT(
                    vkCode=vk_code,
                    scanCode=scan_code,
                    flags=flags,
                    time=0,
                    dwExtraInfo=0,
                )
                self.assertFalse(gate.consume_rc003_direction_event(event, True))

    def test_unclaimed_direction_release_is_not_swallowed(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=0x27,
            scanCode=0x4D,
            flags=physicalizer.LLKHF_EXTENDED,
            time=0,
            dwExtraInfo=0,
        )

        self.assertFalse(gate.record_rc003_direction_edge(0x27, 0x4D, True, False))
        self.assertFalse(gate.consume_rc003_direction_event(event, False))

    def test_unarmed_physical_direction_down_is_not_swallowed(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=0x28,
            scanCode=0x50,
            flags=physicalizer.LLKHF_EXTENDED,
            time=0,
            dwExtraInfo=0,
        )

        self.assertFalse(gate.consume_rc003_direction_event(event, True))


class PhysicalModifierTrackingTests(unittest.TestCase):
    def setUp(self):
        physicalizer._set_physical_tracker_active(False)

    def tearDown(self):
        physicalizer.set_diagnostic_trace(None)
        physicalizer._set_physical_tracker_active(False)

    @staticmethod
    def _event(vk_code, *, scan_code=0, flags=0):
        return physicalizer.KBDLLHOOKSTRUCT(
            vkCode=vk_code,
            scanCode=scan_code,
            flags=flags,
            time=0,
            dwExtraInfo=0,
        )

    def test_only_side_specific_modifier_keys_are_tracked(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)

        self.assertTrue(
            physicalizer.record_physical_key_event(
                self._event(physicalizer.VK_CONTROL),
                physicalizer.WM_KEYDOWN,
            )
        )
        self.assertTrue(
            physicalizer.physical_key_is_down(physicalizer.VK_LCONTROL)
        )
        self.assertTrue(physicalizer.physical_key_is_down(physicalizer.VK_CONTROL))

        self.assertFalse(
            physicalizer.record_physical_key_event(
                self._event(0x26),
                physicalizer.WM_KEYDOWN,
            )
        )
        self.assertFalse(physicalizer.physical_key_is_down(0x26))

    def test_generic_modifiers_normalize_to_the_physical_side(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        cases = (
            (physicalizer.VK_SHIFT, 0x36, 0, physicalizer.VK_RSHIFT),
            (physicalizer.VK_CONTROL, 0x1D, physicalizer.LLKHF_EXTENDED, physicalizer.VK_RCONTROL),
            (physicalizer.VK_MENU, 0x38, physicalizer.LLKHF_EXTENDED, physicalizer.VK_RMENU),
        )

        for vk_code, scan_code, flags, expected in cases:
            with self.subTest(expected=expected):
                event = self._event(vk_code, scan_code=scan_code, flags=flags)
                self.assertTrue(
                    physicalizer.record_physical_key_event(
                        event,
                        physicalizer.WM_KEYDOWN,
                    )
                )
                self.assertTrue(physicalizer.physical_key_is_down(expected))
                physicalizer.record_physical_key_event(
                    event,
                    physicalizer.WM_KEYUP,
                )
                self.assertFalse(physicalizer.physical_key_is_down(expected))

    def test_injected_modifier_edges_never_enter_physical_state(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        event = self._event(
            physicalizer.VK_LCONTROL,
            flags=physicalizer.LLKHF_INJECTED,
        )

        self.assertFalse(
            physicalizer.record_physical_key_event(
                event,
                physicalizer.WM_KEYDOWN,
            )
        )
        self.assertFalse(
            physicalizer.physical_key_is_down(physicalizer.VK_LCONTROL)
        )

    def test_initial_state_query_cannot_restore_a_modifier_released_mid_scan(self):
        event = self._event(physicalizer.VK_LCONTROL)

        def query(vk_code):
            if vk_code == physicalizer.VK_LCONTROL:
                physicalizer.record_physical_key_event(
                    event,
                    physicalizer.WM_KEYUP,
                )
                return True
            return False

        physicalizer._set_physical_tracker_active(True, _query=query)

        self.assertFalse(
            physicalizer.physical_key_is_down(physicalizer.VK_LCONTROL)
        )

    def test_inactive_tracker_uses_async_state_only_before_injection(self):
        physicalizer._set_physical_tracker_active(False)

        self.assertTrue(
            physicalizer.physical_key_is_down_before_injection(
                physicalizer.VK_LCONTROL,
                _query=lambda _vk: True,
            )
        )
        self.assertFalse(
            physicalizer.physical_key_is_down(physicalizer.VK_LCONTROL)
        )

    def test_active_tracker_also_checks_windows_before_a_new_key_down(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        self.assertTrue(
            physicalizer.physical_key_is_down_before_injection(
                physicalizer.VK_RMENU, _query=lambda _vk: True
            )
        )
        self.assertFalse(physicalizer.physical_key_is_down(physicalizer.VK_RMENU))

    def test_physical_hold_blocks_even_when_windows_state_is_still_up(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        physicalizer.record_physical_key_event(
            self._event(physicalizer.VK_RMENU), physicalizer.WM_KEYDOWN
        )
        query = mock.Mock(return_value=False)
        self.assertTrue(
            physicalizer.physical_key_is_down_before_injection(
                physicalizer.VK_RMENU, _query=query
            )
        )
        query.assert_not_called()

    def _age_right_alt_owner(self):
        with physicalizer._PHYSICAL_KEY_STATE_LOCK:
            physicalizer._PHYSICAL_KEY_LAST_EDGE_AT[physicalizer.VK_RMENU] -= 1.0

    def test_stale_right_alt_owner_is_cleared_after_windows_confirms_release(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        physicalizer.record_physical_key_event(
            self._event(physicalizer.VK_RMENU), physicalizer.WM_KEYDOWN
        )
        self._age_right_alt_owner()
        trace = mock.Mock()
        physicalizer.set_diagnostic_trace(trace)

        self.assertFalse(
            physicalizer.physical_key_is_down_before_injection(
                physicalizer.VK_RMENU, _query=lambda _vk: False
            )
        )

        self.assertFalse(physicalizer.physical_key_is_down(physicalizer.VK_RMENU))
        trace.emit.assert_called_once()
        self.assertEqual(trace.emit.call_args.args, ("input_physical_state_reconciled",))

    def test_delayed_down_gets_its_freshness_time_when_installed(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        real_lock = physicalizer._PHYSICAL_KEY_STATE_LOCK
        entered = threading.Event()
        release = threading.Event()
        now = [1.0]

        class DelayedLock:
            def __enter__(self):
                entered.set()
                if not release.wait(1.0):
                    raise AssertionError("test lock was not released")
                real_lock.acquire()
                return self

            def __exit__(self, *_args):
                real_lock.release()

        event = self._event(physicalizer.VK_RMENU)
        with mock.patch.object(
            physicalizer, "_PHYSICAL_KEY_STATE_LOCK", DelayedLock()
        ), mock.patch.object(
            physicalizer.time, "monotonic", side_effect=lambda: now[0]
        ):
            worker = threading.Thread(
                target=physicalizer.record_physical_key_event,
                args=(event, physicalizer.WM_KEYDOWN),
            )
            worker.start()
            self.assertTrue(entered.wait(1.0))
            now[0] = 10.0
            release.set()
            worker.join(1.0)
            self.assertFalse(worker.is_alive())
            query = mock.Mock(return_value=False)
            self.assertTrue(
                physicalizer.physical_key_is_down_before_injection(
                    physicalizer.VK_RMENU, _query=query
                )
            )
            query.assert_not_called()

    def test_unfinished_right_alt_callback_cannot_be_reconciled(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        event = self._event(physicalizer.VK_RMENU)
        physicalizer.record_physical_key_event(event, physicalizer.WM_KEYDOWN)
        self._age_right_alt_owner()
        generation = physicalizer._begin_physical_ralt_callback(event)
        query = mock.Mock(return_value=False)
        try:
            self.assertTrue(
                physicalizer.physical_key_is_down_before_injection(
                    physicalizer.VK_RMENU, _query=query
                )
            )
            query.assert_not_called()
            self.assertTrue(
                physicalizer.physical_key_is_down(physicalizer.VK_RMENU)
            )
        finally:
            physicalizer._end_physical_ralt_callback(generation)

    def test_hook_tail_keeps_right_alt_callback_fenced_until_forwarded(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        gate = physicalizer.VoiceKeyPhysicalizer()
        event = self._event(physicalizer.VK_RMENU, scan_code=0x38)
        tail_entered = threading.Event()
        release_tail = threading.Event()
        now = [1.0]
        trace = mock.Mock()

        def observe(*_args):
            tail_entered.set()
            if not release_tail.wait(1.0):
                raise AssertionError("hook tail was not released")

        trace.observe_submission_key.side_effect = observe
        physicalizer.set_diagnostic_trace(trace)
        user32 = mock.Mock()
        user32.CallNextHookEx = mock.Mock(return_value=0)

        with mock.patch.object(
            physicalizer.ctypes,
            "windll",
            types.SimpleNamespace(user32=user32),
        ), mock.patch.object(
            physicalizer.time, "monotonic", side_effect=lambda: now[0]
        ):
            worker = threading.Thread(
                target=gate._hookproc,
                args=(
                    0,
                    physicalizer.WM_SYSKEYDOWN,
                    physicalizer.ctypes.addressof(event),
                ),
            )
            worker.start()
            self.assertTrue(tail_entered.wait(1.0))
            now[0] = 2.0
            query = mock.Mock(return_value=False)
            self.assertTrue(
                physicalizer.physical_key_is_down_before_injection(
                    physicalizer.VK_RMENU, _query=query
                )
            )
            query.assert_not_called()
            release_tail.set()
            worker.join(1.0)
            self.assertFalse(worker.is_alive())
            with physicalizer._PHYSICAL_KEY_STATE_LOCK:
                self.assertEqual(physicalizer._PHYSICAL_RALT_CALLBACKS_IN_FLIGHT, 0)
            self.assertFalse(
                physicalizer.physical_key_is_down_before_injection(
                    physicalizer.VK_RMENU, _query=lambda _vk: False
                )
            )

    def test_hook_forwarding_exception_clears_right_alt_callback_fence(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        gate = physicalizer.VoiceKeyPhysicalizer()
        event = self._event(physicalizer.VK_RMENU, scan_code=0x38)
        user32 = mock.Mock()
        user32.CallNextHookEx = mock.Mock(side_effect=RuntimeError("forward failed"))

        with mock.patch.object(
            physicalizer.ctypes,
            "windll",
            types.SimpleNamespace(user32=user32),
        ):
            with self.assertRaisesRegex(RuntimeError, "forward failed"):
                gate._hookproc(
                    0,
                    physicalizer.WM_SYSKEYDOWN,
                    physicalizer.ctypes.addressof(event),
                )

        with physicalizer._PHYSICAL_KEY_STATE_LOCK:
            self.assertEqual(physicalizer._PHYSICAL_RALT_CALLBACKS_IN_FLIGHT, 0)

    def test_stale_hook_completion_does_not_clear_new_generation_fence(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        gate = physicalizer.VoiceKeyPhysicalizer()
        old_event = self._event(physicalizer.VK_RMENU, scan_code=0x38)
        tail_entered = threading.Event()
        release_tail = threading.Event()
        trace = mock.Mock()

        def observe(*_args):
            tail_entered.set()
            if not release_tail.wait(1.0):
                raise AssertionError("hook tail was not released")

        trace.observe_submission_key.side_effect = observe
        physicalizer.set_diagnostic_trace(trace)
        user32 = mock.Mock()
        user32.CallNextHookEx = mock.Mock(return_value=0)

        with mock.patch.object(
            physicalizer.ctypes,
            "windll",
            types.SimpleNamespace(user32=user32),
        ):
            worker = threading.Thread(
                target=gate._hookproc,
                args=(
                    0,
                    physicalizer.WM_SYSKEYDOWN,
                    physicalizer.ctypes.addressof(old_event),
                ),
            )
            worker.start()
            self.assertTrue(tail_entered.wait(1.0))
            physicalizer._set_physical_tracker_active(False)
            physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
            new_generation = physicalizer._begin_physical_ralt_callback(
                self._event(physicalizer.VK_RMENU, scan_code=0x38)
            )
            release_tail.set()
            worker.join(1.0)
            self.assertFalse(worker.is_alive())
            with physicalizer._PHYSICAL_KEY_STATE_LOCK:
                self.assertEqual(physicalizer._PHYSICAL_RALT_CALLBACKS_IN_FLIGHT, 1)
            physicalizer._end_physical_ralt_callback(new_generation)
            with physicalizer._PHYSICAL_KEY_STATE_LOCK:
                self.assertEqual(physicalizer._PHYSICAL_RALT_CALLBACKS_IN_FLIGHT, 0)

    def test_windows_query_failure_keeps_stale_right_alt_owner(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        physicalizer.record_physical_key_event(
            self._event(physicalizer.VK_RMENU), physicalizer.WM_KEYDOWN
        )
        self._age_right_alt_owner()

        with self.assertRaisesRegex(OSError, "unavailable"):
            physicalizer.physical_key_is_down_before_injection(
                physicalizer.VK_RMENU,
                _query=mock.Mock(side_effect=OSError("unavailable")),
            )

        self.assertTrue(physicalizer.physical_key_is_down(physicalizer.VK_RMENU))

    def test_concurrent_right_alt_down_wins_over_stale_reconciliation(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        event = self._event(physicalizer.VK_RMENU)
        physicalizer.record_physical_key_event(event, physicalizer.WM_KEYDOWN)
        self._age_right_alt_owner()

        def query(_vk):
            physicalizer.record_physical_key_event(event, physicalizer.WM_KEYDOWN)
            return False

        self.assertTrue(
            physicalizer.physical_key_is_down_before_injection(
                physicalizer.VK_RMENU, _query=query
            )
        )
        self.assertTrue(physicalizer.physical_key_is_down(physicalizer.VK_RMENU))

    def test_new_right_alt_down_during_windows_query_blocks_injection(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        event = self._event(physicalizer.VK_RMENU)

        def query(_vk):
            physicalizer.record_physical_key_event(event, physicalizer.WM_KEYDOWN)
            return False

        self.assertTrue(
            physicalizer.physical_key_is_down_before_injection(
                physicalizer.VK_RMENU, _query=query
            )
        )

    def test_tracker_restart_during_reconciliation_fails_closed(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        physicalizer.record_physical_key_event(
            self._event(physicalizer.VK_RMENU), physicalizer.WM_KEYDOWN
        )
        self._age_right_alt_owner()

        def query(_vk):
            physicalizer._set_physical_tracker_active(False)
            physicalizer._set_physical_tracker_active(True, _query=lambda _key: False)
            return False

        self.assertTrue(
            physicalizer.physical_key_is_down_before_injection(
                physicalizer.VK_RMENU, _query=query
            )
        )

    def test_other_stale_modifier_keeps_existing_fail_closed_behavior(self):
        physicalizer._set_physical_tracker_active(True, _query=lambda _vk: False)
        physicalizer.record_physical_key_event(
            self._event(physicalizer.VK_LCONTROL), physicalizer.WM_KEYDOWN
        )
        with physicalizer._PHYSICAL_KEY_STATE_LOCK:
            physicalizer._PHYSICAL_KEY_LAST_EDGE_AT[physicalizer.VK_LCONTROL] -= 1.0
        query = mock.Mock(return_value=False)

        self.assertTrue(
            physicalizer.physical_key_is_down_before_injection(
                physicalizer.VK_LCONTROL, _query=query
            )
        )
        query.assert_not_called()


class VoiceKeyPhysicalizerLifecycleTests(unittest.TestCase):
    def test_test_worker_starts_and_stops_cleanly(self):
        gate = physicalizer.VoiceKeyPhysicalizer()

        def run_target():
            gate._ready_event.set()
            gate._stop_event.wait()

        gate.start(_run_target=run_target)
        self.assertTrue(gate.is_running)
        gate.stop()
        self.assertFalse(gate.is_running)

    def test_second_start_is_rejected(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        release = threading.Event()

        def run_target():
            gate._ready_event.set()
            release.wait()

        gate.start(_run_target=run_target)
        try:
            with self.assertRaises(
                physicalizer.VoiceKeyPhysicalizerUnavailableError
            ):
                gate.start(_run_target=run_target)
        finally:
            release.set()
            gate.stop()

    def test_real_hook_creates_message_queue_before_reporting_ready(self):
        source = source_text(physicalizer.VoiceKeyPhysicalizer._run)

        self.assertLess(source.index("PeekMessageW"), source.index("_ready_event.set"))
        self.assertIn("ctypes.byref(queue_probe)", source)
        self.assertNotIn("PeekMessageW(None, None", source)

    def test_ready_then_immediate_exit_is_rejected(self):
        gate = physicalizer.VoiceKeyPhysicalizer()

        def run_target():
            gate._unexpected_exit_event.set()
            gate._ready_event.set()

        with self.assertRaises(
            physicalizer.VoiceKeyPhysicalizerUnavailableError
        ):
            gate.start(_run_target=run_target)

        self.assertFalse(gate.is_running)

    def test_tracking_lost_callback_runs_off_thread_while_owner_pumps(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        callback_threads = []
        pump_calls = []
        pump_seen = threading.Event()

        def callback():
            callback_threads.append(threading.current_thread())
            pump_seen.wait(1.0)

        def pump_once():
            pump_calls.append(True)
            pump_seen.set()

        gate.set_tracking_lost_callback(callback)
        physicalizer._set_physical_tracker_active(
            True,
            _query=lambda _vk: False,
        )
        physicalizer._begin_physical_tracker_drain()
        try:
            gate._notify_tracking_lost_while_hook_active(pump_once)
        finally:
            physicalizer._set_physical_tracker_active(False)

        self.assertTrue(pump_calls)
        self.assertEqual(len(callback_threads), 1)
        self.assertIsNot(callback_threads[0], threading.current_thread())

    def test_repeated_confirmation_failures_emit_one_health_notification(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        notifications = []
        notified = threading.Event()
        gate.set_health_failure_callback(
            lambda reason, snapshot: (
                notifications.append((reason, snapshot.generation)),
                notified.set(),
            )
        )
        physicalizer._set_physical_tracker_active(
            True,
            _query=lambda _vk: False,
            owner=gate,
        )
        try:
            first = physicalizer.begin_marked_voice_event(False)
            self.assertFalse(
                physicalizer.wait_for_marked_voice_event(first, 0.001)
            )
            physicalizer.mark_required_confirmation_failed(first)
            second = physicalizer.begin_marked_voice_event(True)
            self.assertFalse(
                physicalizer.wait_for_marked_voice_event(second, 0.001)
            )
            physicalizer.mark_required_confirmation_failed(second)
            self.assertTrue(notified.wait(1.0))
        finally:
            physicalizer._set_physical_tracker_active(False)

        self.assertEqual(len(notifications), 1)


class VoiceEventConfirmationTests(unittest.TestCase):
    def setUp(self):
        physicalizer._set_physical_tracker_active(
            True,
            _query=lambda _vk: False,
        )

    def tearDown(self):
        physicalizer._set_physical_tracker_active(False)

    @staticmethod
    def _event(marker):
        return physicalizer.KBDLLHOOKSTRUCT(
            vkCode=physicalizer.VK_RMENU,
            scanCode=0x38,
            flags=(
                physicalizer.LLKHF_INJECTED
                | physicalizer.LLKHF_LOWER_IL_INJECTED
            ),
            time=0,
            dwExtraInfo=marker,
        )

    def test_only_the_matching_ticket_and_edge_can_be_physicalized(self):
        ticket = physicalizer.begin_marked_voice_event(False)
        wrong_edge = self._event(ticket.marker)
        wrong_ticket = self._event(ticket.marker + 1)

        self.assertFalse(
            physicalizer.physicalize_injected_event(wrong_edge, True)
        )
        self.assertFalse(
            physicalizer.physicalize_injected_event(wrong_ticket, False)
        )
        self.assertNotEqual(wrong_edge.flags, 0)
        self.assertEqual(wrong_edge.dwExtraInfo, ticket.marker)
        self.assertNotEqual(wrong_ticket.flags, 0)
        self.assertEqual(wrong_ticket.dwExtraInfo, ticket.marker + 1)

        matching = self._event(ticket.marker)
        self.assertTrue(
            physicalizer.physicalize_injected_event(matching, False)
        )
        self.assertEqual(matching.flags, 0)
        self.assertEqual(matching.dwExtraInfo, 0)
        physicalizer.complete_marked_voice_event(ticket, downstream_result=0)
        self.assertTrue(
            physicalizer.wait_for_marked_voice_event(ticket, 0.01)
        )

        duplicate = self._event(ticket.marker)
        self.assertFalse(
            physicalizer.physicalize_injected_event(duplicate, False)
        )
        self.assertNotEqual(duplicate.flags, 0)
        self.assertEqual(duplicate.dwExtraInfo, ticket.marker)

    def test_unconfirmed_ticket_times_out(self):
        ticket = physicalizer.begin_marked_voice_event(True)

        self.assertFalse(
            physicalizer.wait_for_marked_voice_event(ticket, 0.001)
        )
        event = self._event(ticket.marker)
        self.assertFalse(physicalizer.physicalize_injected_event(event, True))
        self.assertNotEqual(event.flags, 0)
        self.assertEqual(event.dwExtraInfo, ticket.marker)

    def test_receipt_waits_until_downstream_hook_returns(self):
        ticket = physicalizer.begin_marked_voice_event(True)
        event = self._event(ticket.marker)
        gate = physicalizer.VoiceKeyPhysicalizer()
        downstream_entered = threading.Event()
        release_downstream = threading.Event()
        user32 = mock.Mock()

        def call_next(*_args):
            downstream_entered.set()
            release_downstream.wait(1.0)
            return 0

        user32.CallNextHookEx = mock.Mock(side_effect=call_next)
        with mock.patch.object(
            physicalizer.ctypes,
            "windll",
            types.SimpleNamespace(user32=user32),
        ):
            worker = threading.Thread(
                target=gate._hookproc,
                args=(
                    0,
                    physicalizer.WM_SYSKEYUP,
                    physicalizer.ctypes.addressof(event),
                ),
            )
            worker.start()
            self.assertTrue(downstream_entered.wait(1.0))
            self.assertTrue(ticket.marker_seen)
            self.assertFalse(ticket.downstream_completed)
            self.assertFalse(ticket.event.is_set())
            release_downstream.set()
            worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(ticket.downstream_completed)
        self.assertTrue(physicalizer.wait_for_marked_voice_event(ticket, 0.01))

    def test_required_timeout_closes_new_down_but_keeps_owned_up_available(self):
        ticket = physicalizer.begin_marked_voice_event(False)
        self.assertFalse(
            physicalizer.wait_for_marked_voice_event(ticket, 0.001)
        )

        applied, snapshot = physicalizer.mark_required_confirmation_failed(ticket)

        self.assertTrue(applied)
        self.assertTrue(snapshot.draining)
        self.assertFalse(snapshot.accepting_new_down)
        with self.assertRaises(
            physicalizer.VoiceKeyPhysicalizerUnavailableError
        ):
            physicalizer.begin_marked_voice_event(False)
        cleanup = physicalizer.begin_marked_voice_event(True)
        cleanup_event = self._event(cleanup.marker)
        self.assertTrue(
            physicalizer.physicalize_injected_event(cleanup_event, True)
        )
        physicalizer.complete_marked_voice_event(cleanup, downstream_result=0)
        self.assertTrue(
            physicalizer.wait_for_marked_voice_event(cleanup, 0.01)
        )

    def test_stale_timeout_cannot_degrade_replacement_generation(self):
        ticket = physicalizer.begin_marked_voice_event(False)
        self.assertFalse(
            physicalizer.wait_for_marked_voice_event(ticket, 0.001)
        )
        physicalizer._set_physical_tracker_active(False)
        physicalizer._set_physical_tracker_active(
            True,
            _query=lambda _vk: False,
        )

        applied, snapshot = physicalizer.mark_required_confirmation_failed(ticket)

        self.assertFalse(applied)
        self.assertFalse(snapshot.draining)
        next_ticket = physicalizer.begin_marked_voice_event(False)
        physicalizer.cancel_marked_voice_event(next_ticket)

    def test_acknowledged_receipt_never_degrades_health(self):
        ticket = physicalizer.begin_marked_voice_event(False)
        event = self._event(ticket.marker)
        self.assertTrue(physicalizer.physicalize_injected_event(event, False))
        physicalizer.complete_marked_voice_event(ticket, downstream_result=0)
        self.assertTrue(
            physicalizer.wait_for_marked_voice_event(ticket, 0.01)
        )

        applied, snapshot = physicalizer.mark_required_confirmation_failed(ticket)

        self.assertFalse(applied)
        self.assertTrue(snapshot.accepting_new_down)

    def test_health_snapshot_distinguishes_callback_entry_from_marker_match(self):
        gate = physicalizer.VoiceKeyPhysicalizer()
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=0x26,
            scanCode=0,
            flags=0,
            time=0,
            dwExtraInfo=0,
        )
        user32 = mock.Mock()
        user32.CallNextHookEx = mock.Mock(return_value=0)

        before = physicalizer.snapshot_health()
        with mock.patch.object(
            physicalizer.ctypes,
            "windll",
            types.SimpleNamespace(user32=user32),
        ):
            gate._hookproc(
                0,
                physicalizer.WM_KEYDOWN,
                physicalizer.ctypes.addressof(event),
            )
        after = physicalizer.snapshot_health()

        self.assertEqual(after.callback_entries, before.callback_entries + 1)
        self.assertEqual(after.marker_callbacks, before.marker_callbacks)
        self.assertEqual(after.marker_matches, before.marker_matches)

    def test_cancelled_ticket_never_changes_the_event(self):
        ticket = physicalizer.begin_marked_voice_event(False)
        physicalizer.cancel_marked_voice_event(ticket)
        event = self._event(ticket.marker)

        self.assertFalse(physicalizer.physicalize_injected_event(event, False))
        self.assertNotEqual(event.flags, 0)
        self.assertEqual(event.dwExtraInfo, ticket.marker)

    def test_old_generation_ticket_never_changes_the_event(self):
        ticket = physicalizer.begin_marked_voice_event(False)
        physicalizer._set_physical_tracker_active(False)
        physicalizer._set_physical_tracker_active(
            True,
            _query=lambda _vk: False,
        )
        event = self._event(ticket.marker)

        self.assertFalse(physicalizer.physicalize_injected_event(event, False))
        self.assertNotEqual(event.flags, 0)
        self.assertEqual(event.dwExtraInfo, ticket.marker)

    def test_drain_rejects_new_down_but_accepts_cleanup_up(self):
        physicalizer._begin_physical_tracker_drain()

        with self.assertRaises(
            physicalizer.VoiceKeyPhysicalizerUnavailableError
        ):
            physicalizer.begin_marked_voice_event(False)
        cleanup = physicalizer.begin_marked_voice_event(True)
        physicalizer.cancel_marked_voice_event(cleanup)

    def test_stopping_the_tracker_cancels_old_generation_tickets(self):
        ticket = physicalizer.begin_marked_voice_event(True)

        physicalizer._set_physical_tracker_active(False)

        self.assertFalse(
            physicalizer.wait_for_marked_voice_event(ticket, 0.01)
        )

    def test_drain_keeps_physical_snapshot_for_cleanup_then_clears_it(self):
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=physicalizer.VK_LCONTROL,
            scanCode=0,
            flags=0,
            time=0,
            dwExtraInfo=0,
        )
        physicalizer.record_physical_key_event(
            event,
            physicalizer.WM_KEYDOWN,
        )

        physicalizer._begin_physical_tracker_drain()

        self.assertFalse(
            physicalizer.physical_key_tracking_available(
                physicalizer.VK_LCONTROL
            )
        )
        self.assertTrue(
            physicalizer.physical_key_is_down(physicalizer.VK_LCONTROL)
        )
        physicalizer._set_physical_tracker_active(False)
        self.assertFalse(
            physicalizer.physical_key_is_down(physicalizer.VK_LCONTROL)
        )

    def test_unmarked_or_wrong_key_event_never_consumes_a_ticket(self):
        ticket = physicalizer.begin_marked_voice_event(False)
        cases = (
            physicalizer.KBDLLHOOKSTRUCT(
                vkCode=physicalizer.VK_RMENU,
                scanCode=0x38,
                flags=physicalizer.LLKHF_INJECTED,
                time=0,
                dwExtraInfo=0,
            ),
            physicalizer.KBDLLHOOKSTRUCT(
                vkCode=0x26,
                scanCode=0,
                flags=physicalizer.LLKHF_INJECTED,
                time=0,
                dwExtraInfo=ticket.marker,
            ),
        )

        for event in cases:
            self.assertFalse(
                physicalizer.physicalize_injected_event(event, False)
            )
        matching = self._event(ticket.marker)
        self.assertTrue(physicalizer.physicalize_injected_event(matching, False))
        physicalizer.complete_marked_voice_event(ticket, downstream_result=0)
        self.assertTrue(
            physicalizer.wait_for_marked_voice_event(ticket, 0.01)
        )


if __name__ == "__main__":
    unittest.main()
