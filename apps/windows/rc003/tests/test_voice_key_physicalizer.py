import inspect
import threading
import unittest

from ovb_rc003 import voice_key_physicalizer_windows as physicalizer


class VoiceKeyPhysicalizerDecisionTests(unittest.TestCase):
    def test_physicalizes_only_marked_injected_right_alt(self):
        event = physicalizer.KBDLLHOOKSTRUCT(
            vkCode=physicalizer.VK_RMENU,
            scanCode=0x38,
            flags=(
                physicalizer.LLKHF_INJECTED
                | physicalizer.LLKHF_LOWER_IL_INJECTED
            ),
            time=0,
            dwExtraInfo=physicalizer.VOICE_EVENT_EXTRA_INFO,
        )

        self.assertTrue(physicalizer.physicalize_injected_event(event))
        self.assertEqual(event.flags, 0)
        self.assertEqual(event.dwExtraInfo, 0)

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
                self.assertFalse(physicalizer.physicalize_injected_event(event))
                self.assertEqual(event.flags, flags)
                self.assertEqual(event.dwExtraInfo, extra_info)

    def test_hook_has_no_key_suppression_or_waiting_path(self):
        source = inspect.getsource(physicalizer.VoiceKeyPhysicalizer._hookproc)

        self.assertNotIn("return 1", source)
        self.assertNotIn("sleep", source)
        self.assertNotIn("consume", source)


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


if __name__ == "__main__":
    unittest.main()
