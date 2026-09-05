import inspect
import threading
import unittest

from ovb_rc003 import voice_key_physicalizer_windows as physicalizer


class VoiceKeyPhysicalizerDecisionTests(unittest.TestCase):
    def setUp(self):
        physicalizer._set_physical_tracker_active(
            True,
            _query=lambda _vk: False,
        )

    def tearDown(self):
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
        self.assertTrue(physicalizer.wait_for_marked_voice_event(ticket, 0.01))

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

    def test_hook_has_no_key_suppression_or_waiting_path(self):
        source = inspect.getsource(physicalizer.VoiceKeyPhysicalizer._hookproc)

        self.assertNotIn("return 1", source)
        self.assertNotIn("sleep", source)
        self.assertNotIn("consume", source)


class PhysicalModifierTrackingTests(unittest.TestCase):
    def setUp(self):
        physicalizer._set_physical_tracker_active(False)

    def tearDown(self):
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
        source = inspect.getsource(physicalizer.VoiceKeyPhysicalizer._run)

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
        self.assertTrue(
            physicalizer.wait_for_marked_voice_event(ticket, 0.01)
        )


if __name__ == "__main__":
    unittest.main()
