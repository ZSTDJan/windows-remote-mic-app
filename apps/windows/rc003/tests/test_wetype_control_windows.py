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
    def _controller(self, state, calls, clock, scheduled=None):
        if scheduled is None:
            scheduled = []

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
            schedule=scheduled.append,
        )

    def test_toolbar_path_is_used_for_start_and_stop(self):
        state = {"panel": False}
        calls = []
        scheduled = []
        clock = FakeClock()
        controller = self._controller(state, calls, clock, scheduled)

        self.assertTrue(controller.start(("lctrl", "win")))
        self.assertTrue(controller.stop(("lctrl", "win")))

        self.assertEqual(calls, ["toolbar", "toolbar"])
        self.assertFalse(state["panel"])
        self.assertEqual(len(scheduled), 1)

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
            schedule=lambda _callback: None,
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

    def test_submit_timeout_closes_a_panel_that_stays_open(self):
        state = {"panel": False}
        calls = []
        scheduled = []
        clock = FakeClock()
        toolbar_calls = 0

        def click_toolbar():
            nonlocal toolbar_calls
            toolbar_calls += 1
            calls.append("toolbar")
            if toolbar_calls == 1:
                state["panel"] = True
            return True

        def close_panel(_panel):
            calls.append("close")
            state["panel"] = False
            return True

        controller = wetype_control_windows.WeTypeVoiceControl(
            logger=logging.getLogger("test"),
            find_panel=lambda: 101 if state["panel"] else None,
            click_toolbar=click_toolbar,
            close_panel=close_panel,
            hotkey_tap=lambda _tokens: None,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=scheduled.append,
        )

        self.assertTrue(controller.start(("lctrl", "win")))
        self.assertTrue(controller.stop(("lctrl", "win")))
        self.assertEqual(len(scheduled), 1)

        scheduled[0]()

        self.assertEqual(calls, ["toolbar", "toolbar", "close"])
        self.assertFalse(state["panel"])

    def test_submit_completion_does_not_close_an_already_finished_panel(self):
        state = {"panel": False}
        calls = []
        scheduled = []
        clock = FakeClock()
        controller = self._controller(state, calls, clock, scheduled)

        self.assertTrue(controller.start(("lctrl", "win")))
        self.assertTrue(controller.stop(("lctrl", "win")))
        self.assertEqual(len(scheduled), 1)

        scheduled[0]()

        self.assertEqual(calls, ["toolbar", "toolbar"])
        self.assertNotIn("close", calls)

    def test_new_session_supersedes_previous_completion_cleanup(self):
        state = {"panel": False}
        calls = []
        scheduled = []
        clock = FakeClock()
        controller = self._controller(state, calls, clock, scheduled)

        self.assertTrue(controller.start(("lctrl", "win")))
        self.assertTrue(controller.stop(("lctrl", "win")))
        self.assertTrue(controller.start(("lctrl", "win")))

        scheduled[0]()

        self.assertTrue(state["panel"])
        self.assertNotIn("close", calls)

    def test_clear_supersedes_previous_completion_cleanup(self):
        state = {"panel": False}
        calls = []
        scheduled = []
        clock = FakeClock()
        toolbar_calls = 0

        def click_toolbar():
            nonlocal toolbar_calls
            toolbar_calls += 1
            calls.append("toolbar")
            if toolbar_calls == 1:
                state["panel"] = True
            return True

        controller = wetype_control_windows.WeTypeVoiceControl(
            logger=logging.getLogger("test"),
            find_panel=lambda: 101 if state["panel"] else None,
            click_toolbar=click_toolbar,
            close_panel=lambda _panel: calls.append("close") or True,
            hotkey_tap=lambda _tokens: None,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=scheduled.append,
        )

        self.assertTrue(controller.start(("lctrl", "win")))
        self.assertTrue(controller.stop(("lctrl", "win")))
        controller.clear()

        scheduled[0]()

        self.assertNotIn("close", calls)


if __name__ == "__main__":
    unittest.main()
