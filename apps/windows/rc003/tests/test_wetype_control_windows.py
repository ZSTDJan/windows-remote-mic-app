import logging
import unittest

from ovb_rc003 import wetype_control_windows


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WeTypeVoiceControlTests(unittest.TestCase):
    def _controller(self, state, calls, clock):
        def find_panel():
            return 101 if state["panel"] else None

        def click_toolbar():
            calls.append("toolbar")
            state["panel"] = not state["panel"]
            return True

        def close_panel(_panel):
            calls.append("close")
            state["panel"] = False
            return True

        def hotkey_tap(tokens):
            calls.append(("hotkey", tuple(tokens)))
            state["panel"] = not state["panel"]

        return wetype_control_windows.WeTypeVoiceControl(
            logger=logging.getLogger("test"),
            find_panel=find_panel,
            click_toolbar=click_toolbar,
            close_panel=close_panel,
            hotkey_tap=hotkey_tap,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    def test_toolbar_path_is_used_for_start_and_stop(self):
        state = {"panel": False}
        calls = []
        clock = FakeClock()
        controller = self._controller(state, calls, clock)

        self.assertTrue(controller.start(("lctrl", "win")))
        self.assertTrue(controller.stop(("lctrl", "win")))

        self.assertEqual(calls, ["toolbar", "toolbar"])
        self.assertFalse(state["panel"])

    def test_hotkey_fallback_uses_same_path_to_submit(self):
        state = {"panel": False}
        calls = []
        clock = FakeClock()
        toolbar_attempts = 0

        def find_panel():
            return 101 if state["panel"] else None

        def click_toolbar():
            nonlocal toolbar_attempts
            toolbar_attempts += 1
            calls.append("toolbar")
            return False

        def hotkey_tap(tokens):
            calls.append(("hotkey", tuple(tokens)))
            state["panel"] = not state["panel"]

        controller = wetype_control_windows.WeTypeVoiceControl(
            logger=logging.getLogger("test"),
            find_panel=find_panel,
            click_toolbar=click_toolbar,
            close_panel=lambda _panel: True,
            hotkey_tap=hotkey_tap,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        tokens = ("lctrl", "lshift", "f9")
        self.assertTrue(controller.start(tokens))
        self.assertTrue(controller.stop(tokens))

        self.assertEqual(
            calls,
            ["toolbar", ("hotkey", tokens), ("hotkey", tokens)],
        )
        self.assertEqual(toolbar_attempts, 1)
        self.assertFalse(state["panel"])

    def test_stale_panel_is_closed_before_a_new_start(self):
        state = {"panel": True}
        calls = []
        clock = FakeClock()
        controller = self._controller(state, calls, clock)

        self.assertTrue(controller.start(("lctrl", "win")))

        self.assertEqual(calls[:2], ["close", "toolbar"])


if __name__ == "__main__":
    unittest.main()
