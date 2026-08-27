import unittest

from ovb_rc003 import bridge_control_windows as control


class BridgeControlWindowsTests(unittest.TestCase):
    def test_already_stopped_needs_no_window_message(self):
        result = control.request_bridge_exit(
            platform="win32",
            bridge_running=lambda: False,
            find_window=lambda: self.fail("不应查找通知区域窗口"),
        )

        self.assertTrue(result.stopped)
        self.assertFalse(result.requested)

    def test_posts_exit_and_waits_until_mutex_is_released(self):
        running = iter((True, True, False))
        posted = []
        result = control.request_bridge_exit(
            platform="win32",
            bridge_running=lambda: next(running),
            find_window=lambda: 123,
            post_exit_command=lambda hwnd: posted.append(hwnd) or True,
            sleep=lambda _seconds: None,
            monotonic=iter((0.0, 0.0, 0.1, 0.2)).__next__,
        )

        self.assertEqual(posted, [123])
        self.assertTrue(result.requested)
        self.assertTrue(result.stopped)

    def test_missing_tray_window_refuses_to_force_kill(self):
        result = control.request_bridge_exit(
            platform="win32",
            bridge_running=lambda: True,
            find_window=lambda: 0,
        )

        self.assertFalse(result.stopped)
        self.assertIn("通知区域", result.error)

    def test_post_failure_explains_elevated_bridge_boundary(self):
        result = control.request_bridge_exit(
            platform="win32",
            bridge_running=lambda: True,
            find_window=lambda: 123,
            post_exit_command=lambda _hwnd: False,
        )

        self.assertFalse(result.stopped)
        self.assertIn("管理员权限", result.error)
