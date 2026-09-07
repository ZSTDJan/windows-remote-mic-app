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

        return wetype_control_windows.WeTypeVoiceControl(
            logger=logging.getLogger("test"),
            find_panel=find_panel,
            click_toolbar=click_toolbar,
            close_panel=close_panel,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=scheduled.append,
        )

    def test_toolbar_path_opens_and_finishes_one_session(self):
        state = {"panel": False}
        calls = []
        scheduled = []
        controller = self._controller(state, calls, FakeClock(), scheduled)

        self.assertTrue(controller.start())
        self.assertTrue(controller.stop())

        self.assertEqual(calls, ["toolbar", "toolbar"])
        self.assertFalse(state["panel"])
        self.assertEqual(len(scheduled), 1)
        scheduled[0]()
        self.assertNotIn("close", calls)

    def test_start_fails_when_toolbar_button_is_unavailable(self):
        clock = FakeClock()
        controller = wetype_control_windows.WeTypeVoiceControl(
            find_panel=lambda: None,
            click_toolbar=lambda: False,
            close_panel=lambda _panel: True,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=lambda _callback: None,
        )

        self.assertFalse(controller.start())

    def test_start_fails_when_panel_does_not_open(self):
        clock = FakeClock()
        controller = wetype_control_windows.WeTypeVoiceControl(
            find_panel=lambda: None,
            click_toolbar=lambda: True,
            close_panel=lambda _panel: True,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=lambda _callback: None,
        )

        self.assertFalse(controller.start())

    def test_stale_panel_must_close_before_a_new_session(self):
        state = {"panel": True}
        calls = []
        controller = self._controller(state, calls, FakeClock())

        self.assertTrue(controller.start())

        self.assertEqual(calls[:2], ["close", "toolbar"])

    def test_stale_panel_close_failure_blocks_a_new_toolbar_click(self):
        calls = []
        clock = FakeClock()
        controller = wetype_control_windows.WeTypeVoiceControl(
            find_panel=lambda: 101,
            click_toolbar=lambda: calls.append("toolbar") or True,
            close_panel=lambda _panel: False,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=lambda _callback: None,
        )

        self.assertFalse(controller.start())
        self.assertEqual(calls, [])

    def test_failed_stop_keeps_the_session_retryable(self):
        state = {"panel": False}
        calls = []
        scheduled = []
        clock = FakeClock()
        stop_attempts = 0

        def click_toolbar():
            nonlocal stop_attempts
            calls.append("toolbar")
            if not state["panel"]:
                state["panel"] = True
                return True
            stop_attempts += 1
            if stop_attempts == 1:
                return False
            state["panel"] = False
            return True

        controller = wetype_control_windows.WeTypeVoiceControl(
            find_panel=lambda: 101 if state["panel"] else None,
            click_toolbar=click_toolbar,
            close_panel=lambda _panel: True,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=scheduled.append,
        )

        self.assertTrue(controller.start())
        self.assertFalse(controller.stop())
        self.assertTrue(controller.stop())
        self.assertEqual(len(scheduled), 1)

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
            find_panel=lambda: 101 if state["panel"] else None,
            click_toolbar=click_toolbar,
            close_panel=close_panel,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=scheduled.append,
        )

        self.assertTrue(controller.start())
        self.assertTrue(controller.stop())
        scheduled[0]()

        self.assertEqual(calls, ["toolbar", "toolbar", "close"])
        self.assertFalse(state["panel"])

    def test_completion_thread_failure_closes_a_remaining_panel_immediately(self):
        state = {"panel": False}
        calls = []
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

        def schedule(_callback):
            raise RuntimeError("thread unavailable")

        controller = wetype_control_windows.WeTypeVoiceControl(
            find_panel=lambda: 101 if state["panel"] else None,
            click_toolbar=click_toolbar,
            close_panel=close_panel,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=schedule,
        )

        self.assertTrue(controller.start())
        self.assertTrue(controller.stop())

        self.assertEqual(calls, ["toolbar", "toolbar", "close"])
        self.assertFalse(state["panel"])

    def test_completion_thread_and_immediate_close_failure_remain_retryable(self):
        state = {"panel": False}
        calls = []
        clock = FakeClock()
        toolbar_calls = 0
        close_attempts = 0

        def click_toolbar():
            nonlocal toolbar_calls
            toolbar_calls += 1
            calls.append("toolbar")
            if toolbar_calls == 1:
                state["panel"] = True
                return True
            if toolbar_calls == 2:
                return True
            state["panel"] = False
            return True

        def close_panel(_panel):
            nonlocal close_attempts
            close_attempts += 1
            calls.append("close")
            return False

        def schedule(_callback):
            raise RuntimeError("thread unavailable")

        controller = wetype_control_windows.WeTypeVoiceControl(
            find_panel=lambda: 101 if state["panel"] else None,
            click_toolbar=click_toolbar,
            close_panel=close_panel,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=schedule,
        )

        self.assertTrue(controller.start())
        self.assertFalse(controller.stop())
        self.assertTrue(state["panel"])
        self.assertTrue(controller.stop())
        self.assertFalse(state["panel"])
        self.assertEqual(close_attempts, 1)

    def test_new_session_supersedes_previous_completion_cleanup(self):
        state = {"panel": False}
        calls = []
        scheduled = []
        clock = FakeClock()
        toolbar_calls = 0

        def click_toolbar():
            nonlocal toolbar_calls
            toolbar_calls += 1
            calls.append("toolbar")
            if toolbar_calls in {1, 3}:
                state["panel"] = True
            return True

        def close_panel(_panel):
            calls.append("close")
            state["panel"] = False
            return True

        controller = wetype_control_windows.WeTypeVoiceControl(
            find_panel=lambda: 101 if state["panel"] else None,
            click_toolbar=click_toolbar,
            close_panel=close_panel,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            schedule=scheduled.append,
        )

        self.assertTrue(controller.start())
        self.assertTrue(controller.stop())
        self.assertTrue(controller.start())
        close_calls_before_cleanup = calls.count("close")

        scheduled[0]()

        self.assertTrue(state["panel"])
        self.assertEqual(calls.count("close"), close_calls_before_cleanup)


if __name__ == "__main__":
    unittest.main()
