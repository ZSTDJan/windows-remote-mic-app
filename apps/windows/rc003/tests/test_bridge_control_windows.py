import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import bridge_runtime_status
from ovb_rc003 import bridge_control_windows as control
from ovb_rc003 import single_instance


class BridgeControlWindowsTests(unittest.TestCase):
    def test_already_stopped_needs_no_window_message(self):
        result = control.request_bridge_exit(
            platform="win32",
            stop_internal=lambda **_kwargs: None,
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
            stop_internal=lambda **_kwargs: None,
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
            stop_internal=lambda **_kwargs: None,
            bridge_running=lambda: True,
            find_window=lambda: 0,
        )

        self.assertFalse(result.stopped)
        self.assertIn("通知区域", result.error)

    def test_post_failure_explains_elevated_bridge_boundary(self):
        result = control.request_bridge_exit(
            platform="win32",
            stop_internal=lambda **_kwargs: None,
            bridge_running=lambda: True,
            find_window=lambda: 123,
            post_exit_command=lambda _hwnd: False,
        )

        self.assertFalse(result.stopped)
        self.assertIn("管理员权限", result.error)

    def test_in_process_bridge_stops_without_using_the_legacy_window(self):
        calls = []
        result = control.request_bridge_exit(
            platform="win32",
            timeout=3.0,
            stop_internal=lambda **kwargs: calls.append(kwargs) or True,
            bridge_running=lambda: self.fail("不应检查旧版互斥锁"),
            find_window=lambda: self.fail("不应查找旧版通知区域窗口"),
        )

        self.assertEqual(calls, [{"timeout": 3.0}])
        self.assertTrue(result.requested)
        self.assertTrue(result.stopped)

    def test_in_process_bridge_timeout_does_not_fall_back_to_old_process(self):
        result = control.request_bridge_exit(
            platform="win32",
            stop_internal=lambda **_kwargs: False,
            bridge_running=lambda: self.fail("不应把当前进程误判为旧版桥接"),
            find_window=lambda: self.fail("不应向旧版窗口发送退出命令"),
        )

        self.assertTrue(result.requested)
        self.assertFalse(result.stopped)
        self.assertIn("限定时间", result.error)

    def test_stopped_bridge_cleans_the_status_captured_before_exit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bridge_runtime_status.publish_status(
                root,
                bridge_runtime_status.BridgeConnectionState.CONNECTED,
                pid=1111,
            )

            result = control.request_bridge_exit(
                platform="win32",
                stop_internal=lambda **_kwargs: True,
                runtime_status_root=root,
            )
            remaining = bridge_runtime_status.read_status(root)

        self.assertTrue(result.stopped)
        self.assertFalse(result.cleanup_failed)
        self.assertIsNone(remaining)

    def test_new_status_during_exit_is_preserved_and_blocks_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bridge_runtime_status.publish_status(
                root,
                bridge_runtime_status.BridgeConnectionState.CONNECTED,
                pid=1111,
            )

            def stop_and_replace(**_kwargs):
                bridge_runtime_status.publish_status(
                    root,
                    bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
                    pid=2222,
                )
                return True

            result = control.request_bridge_exit(
                platform="win32",
                stop_internal=stop_and_replace,
                runtime_status_root=root,
            )
            remaining = bridge_runtime_status.read_status(root)

        self.assertFalse(result.stopped)
        self.assertTrue(result.cleanup_failed)
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining.pid, 2222)

    def test_stopping_one_windows_session_preserves_another_session_status(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            other_session = bridge_runtime_status.publish_status(
                root,
                bridge_runtime_status.BridgeConnectionState.CONNECTED,
                pid=1111,
                session_id=2,
            )
            with mock.patch.object(
                bridge_runtime_status.sys,
                "platform",
                "win32",
            ), mock.patch.object(
                bridge_runtime_status,
                "_default_status_scope",
                bridge_runtime_status._STATUS_SCOPE_UNSET,
            ), mock.patch.object(
                single_instance,
                "current_process_session_id",
                return_value=1,
            ):
                result = control.request_bridge_exit(
                    platform="win32",
                    stop_internal=lambda **_kwargs: True,
                    runtime_status_root=root,
                )

            remaining = bridge_runtime_status.read_status(
                root,
                session_id=2,
            )

        self.assertTrue(result.stopped)
        self.assertFalse(result.cleanup_failed)
        self.assertEqual(remaining, other_session)
