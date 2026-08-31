import os
import threading
import time
import unittest
from unittest import mock

from ovb_rc003 import legacy_key_suppressor_windows as suppressor


class LegacyKeySuppressorDecisionTests(unittest.TestCase):
    def test_suppresses_configured_non_injected_vk(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        self.assertTrue(gate.should_suppress(0x74, 0))

    def test_does_not_suppress_sendinput_injected_vk(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        self.assertFalse(gate.should_suppress(0x74, suppressor.LLKHF_INJECTED))

    def test_physicalizes_only_marked_right_alt_event(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        event = suppressor.KBDLLHOOKSTRUCT(
            vkCode=0xA5,
            scanCode=0x38,
            flags=(
                suppressor.LLKHF_EXTENDED
                | suppressor.LLKHF_INJECTED
                | suppressor.LLKHF_LOWER_IL_INJECTED
            ),
            time=123,
            dwExtraInfo=suppressor.VOICE_EVENT_EXTRA_INFO,
        )

        self.assertTrue(gate.physicalize_injected_event(event))
        self.assertEqual(event.flags, suppressor.LLKHF_EXTENDED)
        self.assertEqual(event.dwExtraInfo, 0)

    def test_does_not_physicalize_unmarked_or_other_injected_events(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        for vk_code, extra_info in (
            (0xA5, 0),
            (0xA4, suppressor.VOICE_EVENT_EXTRA_INFO),
        ):
            event = suppressor.KBDLLHOOKSTRUCT(
                vkCode=vk_code,
                scanCode=0x38,
                flags=suppressor.LLKHF_EXTENDED | suppressor.LLKHF_INJECTED,
                time=123,
                dwExtraInfo=extra_info,
            )
            original = (int(event.flags), int(event.dwExtraInfo))
            self.assertFalse(gate.physicalize_injected_event(event))
            self.assertEqual((int(event.flags), int(event.dwExtraInfo)), original)

    def test_does_not_suppress_unconfigured_vk_codes(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        self.assertFalse(gate.should_suppress(0x5B, 0))  # VK_LWIN
        self.assertFalse(gate.should_suppress(0x48, 0))  # H

    def test_suppressed_physical_edge_is_forwarded_without_forwarding_injected_input(self):
        events = []
        gate = suppressor.LegacyKeySuppressor(
            {0x74}, on_key_event=lambda vk_code, is_pressed: events.append(
                (vk_code, is_pressed)
            )
        )

        self.assertTrue(gate.handle_key_event(0x74, 0, True))
        self.assertTrue(gate.handle_key_event(0x74, 0, False))
        self.assertFalse(gate.handle_key_event(0x74, suppressor.LLKHF_INJECTED, True))
        self.assertEqual(events, [(0x74, True), (0x74, False)])

    def test_armed_raw_input_edge_is_consumed_once_and_only_with_exact_identity(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        gate.arm_key_event(0x26, 0x48, True, True)

        self.assertFalse(gate.consume_armed_key_event(0x26, 0x48, False, True))
        self.assertTrue(gate.consume_armed_key_event(0x26, 0x48, True, True))
        self.assertFalse(gate.consume_armed_key_event(0x26, 0x48, True, True))

    def test_armed_five_is_left_to_the_dedicated_voice_suppressor(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        gate.arm_key_event(0x74, 0x3F, False, True)
        self.assertFalse(gate.consume_armed_key_event(0x74, 0x3F, False, True))

    def test_tracked_hold_consumes_every_repeated_down_until_legacy_up(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        gate.arm_tracked_key_event(0x26, 0x48, True, True)

        self.assertTrue(
            gate.consume_armed_key_event(0x26, 0x48, True, True, wait_seconds=0)
        )
        self.assertTrue(
            gate.consume_armed_key_event(0x26, 0x48, True, True, wait_seconds=0)
        )
        self.assertTrue(
            gate.consume_armed_key_event(0x26, 0x48, True, True, wait_seconds=0)
        )

        self.assertTrue(
            gate.consume_armed_key_event(0x26, 0x48, True, False, wait_seconds=0)
        )
        self.assertFalse(
            gate.consume_armed_key_event(0x26, 0x48, True, True, wait_seconds=0)
        )

    def test_tracked_release_consumes_late_repeat_before_legacy_up(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        gate.arm_tracked_key_event(0x26, 0x48, True, True)
        self.assertTrue(
            gate.consume_armed_key_event(0x26, 0x48, True, True, wait_seconds=0)
        )

        gate.arm_tracked_key_event(0x26, 0x48, True, False)

        self.assertTrue(
            gate.consume_armed_key_event(0x26, 0x48, True, True, wait_seconds=0)
        )
        self.assertTrue(
            gate.consume_armed_key_event(0x26, 0x48, True, False, wait_seconds=0)
        )
        self.assertFalse(
            gate.consume_armed_key_event(0x26, 0x48, True, True, wait_seconds=0)
        )

    def test_tracked_key_does_not_consume_another_key(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        gate.arm_tracked_key_event(0x26, 0x48, True, True)

        self.assertFalse(
            gate.consume_armed_key_event(0x27, 0x4D, True, True, wait_seconds=0)
        )
        self.assertFalse(gate.should_suppress(0x33, suppressor.LLKHF_INJECTED))

    def test_late_device_release_does_not_leave_a_stale_release_arm(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        gate.arm_tracked_key_event(0x26, 0x48, True, True)
        self.assertTrue(
            gate.consume_armed_key_event(0x26, 0x48, True, True, wait_seconds=0)
        )
        self.assertTrue(
            gate.consume_armed_key_event(0x26, 0x48, True, False, wait_seconds=0)
        )

        gate.arm_tracked_key_event(0x26, 0x48, True, False)

        self.assertFalse(
            gate.consume_armed_key_event(0x26, 0x48, True, False, wait_seconds=0)
        )


class LegacyKeySuppressorRaceTests(unittest.TestCase):
    """The low-level hook and the Raw Input thread race for the same physical
    press. The consumer must wait briefly for an arming edge that is in
    flight, without blocking ordinary keyboard input."""

    def test_consume_waits_for_an_arm_that_arrives_right_afterward(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        # The hook fires before the Raw Input thread delivers the same
        # physical press, so the arming edge lands a moment later. The
        # consumer must wait for it instead of releasing a double action.
        def raw_input_thread():
            import time

            time.sleep(0.01)
            gate.arm_key_event(0x26, 0x48, True, True)

        thread = threading.Thread(target=raw_input_thread)
        thread.start()
        try:
            matched = gate.consume_armed_key_event(
                0x26, 0x48, True, True, wait_seconds=0.200
            )
        finally:
            thread.join(timeout=2)
        self.assertTrue(matched)
        # The edge is consumed exactly once.
        self.assertFalse(gate.consume_armed_key_event(0x26, 0x48, True, True))

    def test_no_recent_arm_returns_immediately_without_blocking(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        # No RC003 press has been armed recently; the hook must pass an
        # unrelated physical key straight through after the short wait.
        self.assertFalse(gate.consume_armed_key_event(0x26, 0x48, True, True))

    def test_unrelated_key_does_not_wait_for_a_pending_arm(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        gate.arm_key_event(0x26, 0x48, True, True)
        # A different key passing through the hook must not be blocked or
        # consumed just because some RC003 edge is armed.
        self.assertFalse(gate.consume_armed_key_event(0x27, 0x49, True, True))

    def test_key_outside_rc003_set_passes_through_immediately(self):
        gate = suppressor.LegacyKeySuppressor(
            {0x74},
            rc003_vk_codes=frozenset({0x74, 0x26, 0x27, 0x25, 0x28}),
            consume_wait_seconds=10.0,
        )
        # Ordinary keyboard letters are not part of the RC003 surface, so the
        # hook must not wait (or block) for an arming edge at all.
        start = time.monotonic()
        self.assertFalse(gate.consume_armed_key_event(0x4E, 0x31, False, True))
        self.assertLess(time.monotonic() - start, 0.25)

    def test_untracked_rc003_release_passes_through_without_waiting(self):
        gate = suppressor.LegacyKeySuppressor(
            {0x74},
            rc003_vk_codes=frozenset({0x74, 0x26, 0x27, 0x25, 0x28}),
            consume_wait_seconds=10.0,
        )

        start = time.monotonic()
        self.assertFalse(gate.consume_armed_key_event(0x26, 0x48, True, False))
        self.assertLess(time.monotonic() - start, 0.25)

    def test_rc003_key_waits_for_a_late_arming_edge_by_default(self):
        gate = suppressor.LegacyKeySuppressor(
            {0x74},
            rc003_vk_codes=frozenset({0x74, 0x26, 0x27, 0x25, 0x28}),
        )

        def raw_input_thread():
            time.sleep(0.03)
            gate.arm_key_event(0x26, 0x48, True, True)

        thread = threading.Thread(target=raw_input_thread)
        thread.start()
        try:
            matched = gate.consume_armed_key_event(0x26, 0x48, True, True)
        finally:
            thread.join(timeout=2)
        self.assertTrue(matched)


class LegacyKeySuppressorLifecycleTests(unittest.TestCase):
    def test_build_gate_rejects_a_real_keyboard_hook(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        with mock.patch.dict(os.environ, {"RC003_DISABLE_LIVE_INPUT": "1"}):
            with self.assertRaises(suppressor.LegacyKeySuppressorUnavailableError):
                gate.start()

    def test_real_hook_creates_its_message_queue_before_reporting_ready(self):
        import inspect

        source = inspect.getsource(suppressor.LegacyKeySuppressor._run)
        self.assertIn("PeekMessageW.argtypes", source)
        self.assertIn("PeekMessageW.restype", source)
        self.assertLess(
            source.index("PeekMessageW(None"),
            source.index("self._ready_event.set()"),
        )

    def test_empty_suppressor_is_a_noop(self):
        gate = suppressor.LegacyKeySuppressor(set())
        gate.start(_run_target=lambda: None)
        self.assertFalse(gate.is_running)

    def test_rejects_second_start_while_thread_is_running(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        release = threading.Event()

        def fake_run():
            gate._ready_event.set()
            release.wait()

        try:
            gate.start(_run_target=fake_run)
            with self.assertRaises(suppressor.LegacyKeySuppressorUnavailableError):
                gate.start(_run_target=fake_run)
        finally:
            release.set()
            gate.stop()

    def test_start_timeout_retains_a_thread_that_did_not_stop(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        release = threading.Event()

        def fake_run():
            release.wait()

        try:
            with self.assertRaises(suppressor.LegacyKeySuppressorUnavailableError):
                gate.start(start_timeout=0.01, _run_target=fake_run)
            self.assertTrue(gate.is_running)
            with self.assertRaises(suppressor.LegacyKeySuppressorUnavailableError):
                gate.start(start_timeout=0.01, _run_target=fake_run)
        finally:
            release.set()
            gate.stop()

    def test_start_error_retains_a_thread_until_it_really_exits(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        release = threading.Event()

        def fake_run():
            gate._start_error = suppressor.LegacyKeySuppressorUnavailableError(
                "simulated startup error"
            )
            gate._ready_event.set()
            release.wait()

        try:
            with self.assertRaises(suppressor.LegacyKeySuppressorUnavailableError):
                gate.start(_run_target=fake_run)
            self.assertTrue(gate.is_running)
        finally:
            release.set()
            gate.stop()


if __name__ == "__main__":
    unittest.main()
