import asyncio
import unittest

from ovb_rc003 import app as app_module
from ovb_rc003 import bridge_launcher, bridge_tray_windows


class TrayMenuDispatchTests(unittest.TestCase):
    def test_open_settings_command_calls_only_open_callback(self):
        calls = []
        handled = bridge_tray_windows.dispatch_menu_command(
            bridge_tray_windows.MENU_OPEN_SETTINGS,
            on_open_settings=lambda: calls.append("open"),
            on_exit_requested=lambda: calls.append("exit"),
        )
        self.assertTrue(handled)
        self.assertEqual(calls, ["open"])

    def test_exit_command_calls_only_exit_callback(self):
        calls = []
        handled = bridge_tray_windows.dispatch_menu_command(
            bridge_tray_windows.MENU_EXIT_BRIDGE,
            on_open_settings=lambda: calls.append("open"),
            on_exit_requested=lambda: calls.append("exit"),
        )
        self.assertTrue(handled)
        self.assertEqual(calls, ["exit"])

    def test_unknown_command_is_ignored(self):
        calls = []
        handled = bridge_tray_windows.dispatch_menu_command(
            9999,
            on_open_settings=lambda: calls.append("open"),
            on_exit_requested=lambda: calls.append("exit"),
        )
        self.assertFalse(handled)
        self.assertEqual(calls, [])


class _RecordingLogger:
    def __init__(self):
        self.rows = []

    def info(self, message, *args):
        self.rows.append(("info", message, args))

    def warning(self, message, *args):
        self.rows.append(("warning", message, args))

    def exception(self, message, *args):
        self.rows.append(("exception", message, args))


class _FakeBridgeApp:
    def __init__(self):
        self._logger = _RecordingLogger()
        self.started = asyncio.Event()
        self.cancelled = False
        self.stop_calls = 0

    async def run_forever(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def stop(self):
        self.stop_calls += 1


class _FakeTray:
    def __init__(self, **callbacks):
        self.on_open_settings = callbacks["on_open_settings"]
        self.on_exit_requested = callbacks["on_exit_requested"]
        self.status_handler = callbacks["status_handler"]
        self.startup_error = ""
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1
        return True

    def stop(self):
        self.stop_calls += 1
        return True


class BridgeTrayLifecycleTests(unittest.TestCase):
    def test_open_settings_and_exit_are_wired_to_bridge_lifecycle(self):
        async def scenario():
            fake_app = _FakeBridgeApp()
            holder = {}
            settings_calls = []

            def tray_factory(**callbacks):
                tray = _FakeTray(**callbacks)
                holder["tray"] = tray
                return tray

            def launch_settings():
                settings_calls.append(1)
                return bridge_launcher.SettingsLaunchResult(
                    command=("exe", "--settings"),
                    pid=4321,
                )

            task = asyncio.create_task(
                app_module._run(
                    app_factory=lambda: fake_app,
                    tray_factory=tray_factory,
                    settings_launcher=launch_settings,
                )
            )
            await asyncio.wait_for(fake_app.started.wait(), timeout=1.0)
            tray = holder["tray"]
            tray.on_open_settings()
            tray.on_exit_requested()
            await asyncio.wait_for(task, timeout=1.0)

            self.assertEqual(settings_calls, [1])
            self.assertTrue(fake_app.cancelled)
            self.assertEqual(fake_app.stop_calls, 1)
            self.assertEqual(tray.start_calls, 1)
            self.assertEqual(tray.stop_calls, 1)

        asyncio.run(scenario())

    def test_tray_stop_exception_never_skips_bridge_cleanup(self):
        async def scenario():
            fake_app = _FakeBridgeApp()
            holder = {}

            class RaisingStopTray(_FakeTray):
                def stop(self):
                    self.stop_calls += 1
                    raise RuntimeError("simulated tray stop failure")

            def tray_factory(**callbacks):
                tray = RaisingStopTray(**callbacks)
                holder["tray"] = tray
                return tray

            task = asyncio.create_task(
                app_module._run(
                    app_factory=lambda: fake_app,
                    tray_factory=tray_factory,
                )
            )
            await asyncio.wait_for(fake_app.started.wait(), timeout=1.0)
            holder["tray"].on_exit_requested()
            await asyncio.wait_for(task, timeout=1.0)

            self.assertEqual(fake_app.stop_calls, 1)
            self.assertEqual(holder["tray"].stop_calls, 1)
            self.assertTrue(
                any(
                    row[0] == "warning" and "stop failed" in row[1]
                    for row in fake_app._logger.rows
                )
            )

        asyncio.run(scenario())
