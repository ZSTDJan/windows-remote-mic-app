import asyncio
import ctypes
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from ovb_rc003 import ble_candidate_probe_process as worker
from ovb_rc003 import ble_transport_winrt, identity


async def wait_until(predicate):
    deadline = asyncio.get_running_loop().time() + 8
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("child did not reach the expected phase")
        await asyncio.sleep(0.01)


def fake_command(marker, blocked=False, windowed=False, failure=None):
    command, env = worker._command()
    script = f'''
import ctypes, json, os, sys
from pathlib import Path
from ovb_rc003 import ble_transport_winrt as transport, __main__ as entry
async def query(candidate, winrt=None):
    Path({str(marker)!r}).write_text(json.dumps({{"pid": os.getpid(), "executable": sys.executable, "prefix": sys.prefix}}))
    if {blocked!r}:
        ctypes.WinDLL("kernel32").Sleep(60000)
    if {failure!r} == "native":
        from types import SimpleNamespace
        async def unavailable(_id):
            raise OSError("private-native-error-text")
        return await original_query(candidate, SimpleNamespace(
            bluetooth_le_device=SimpleNamespace(from_id_async=unavailable)))
    if {failure!r} == "worker":
        raise RuntimeError("private-worker-error-text")
    return candidate.handle.id == "private-live-record"
original_query = transport._candidate_has_voice_service
transport._candidate_has_voice_service = query
sys.argv = ["worker", "--probe-rc003-candidate"]
if {windowed!r}:
    sys.stdin = sys.stdout = sys.stderr = None
entry.main()
'''
    return [command[0], "-B", "-c", script], env


class CommandTests(unittest.TestCase):
    def test_internal_flag_never_falls_through_to_desktop(self):
        from ovb_rc003 import __main__ as entry

        def exit_with_code(code):
            raise SystemExit(code)

        for args in ((worker.FLAG, "unexpected"), ("--settings", worker.FLAG)):
            with self.subTest(args=args), mock.patch.object(sys, "argv", ["test", *args]), \
                    mock.patch.object(worker.os, "_exit", side_effect=exit_with_code) as exit_process:
                with self.assertRaises(SystemExit) as raised:
                    entry.main()
                self.assertEqual(raised.exception.code, 2)
                if args[0] == worker.FLAG:
                    exit_process.assert_called_once_with(2)

    def test_frozen_command_uses_only_internal_flag(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            command, _env = worker._command()
        self.assertEqual(command, [sys.executable, worker.FLAG])

    def test_child_rejects_arguments_before_reading_input(self):
        with mock.patch.object(worker, "_pipe_fd") as incoming:
            self.assertEqual(worker.child_main(["unexpected"]), 2)
        incoming.assert_not_called()

    def test_bad_requests_never_touch_winrt(self):
        for value in (b"{}\n", b'{"device_id":"x", "extra":1}\n', b'""\n', b"null\n",
                      b"123\n", b"bad\n", b"x" * 2049):
            chunks = iter(bytes([item]) for item in value)
            with self.subTest(value=value[:24]), mock.patch.object(worker, "_pipe_fd", return_value=0), \
                    mock.patch.object(worker.os, "read", side_effect=lambda *_: next(chunks, b"")), \
                    mock.patch.object(ble_transport_winrt, "_candidate_has_voice_service") as query:
                self.assertEqual(worker.child_main([]), 2)
                query.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Windows native process ownership")
class NativeProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.marker = Path(self.directory.name) / "started.json"
        self.processes = []
        original_popen = subprocess.Popen

        def record(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            self.processes.append(process)
            return process

        self.popen_patch = mock.patch.object(worker.subprocess, "Popen", side_effect=record)
        self.popen_patch.start()
        self.addCleanup(self.popen_patch.stop)
        self.addCleanup(self.reap_children)

    def reap_children(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait(3)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()

    def command(self, blocked=False, windowed=False, failure=None):
        return fake_command(self.marker, blocked, windowed, failure)

    def test_real_child_success_failure_and_windowed_streams_release_ownership(self):
        for device_id, expected, windowed in (("private-live-record", True, False),
                                             ("private-stale-record", False, False),
                                             ("private-live-record", True, True)):
            command = self.command(windowed=windowed)
            with self.subTest(device_id=device_id, windowed=windowed), \
                    mock.patch.object(worker, "_command", return_value=command):
                self.assertIs(asyncio.run(worker.probe(device_id, timeout=5)), expected)
                self.assertNotIn(device_id, command[0])
                self.assertNotIn(device_id, worker._owners)
                self.assertIsNotNone(self.processes[-1].poll())
                self.assertTrue(self.processes[-1].stdin.closed)
                runtime = json.loads(self.marker.read_text())
                self.assertEqual(Path(runtime["executable"]), Path(sys.executable))
                self.assertEqual(Path(runtime["prefix"]), Path(sys.prefix))

    def test_native_timeout_reaps_child_and_same_record_can_retry(self):
        blocked = self.command(blocked=True)
        with mock.patch.object(worker, "_command", return_value=blocked):
            self.assertIsNone(asyncio.run(worker.probe("private-live-record", timeout=1)))
        self.assertTrue(self.marker.exists(), "must reach the native blocked call")
        self.assertIsNotNone(self.processes[-1].poll())
        self.assertNotIn("private-live-record", worker._owners)
        ready = self.command()
        with mock.patch.object(worker, "_command", return_value=ready):
            self.assertTrue(asyncio.run(worker.probe("private-live-record", timeout=5)))

    def test_cancel_reaps_native_query_before_restart_of_same_record(self):
        blocked, ready = self.command(blocked=True), self.command()

        async def scenario():
            with mock.patch.object(worker, "_command", return_value=blocked):
                task = asyncio.create_task(worker.probe("private-live-record", timeout=30))
                await wait_until(self.marker.exists)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            self.assertIsNotNone(self.processes[-1].poll())
            self.assertNotIn("private-live-record", worker._owners)
            with mock.patch.object(worker, "_command", return_value=ready):
                self.assertTrue(await worker.probe("private-live-record", timeout=5))

        asyncio.run(scenario())

    def test_resolver_skips_native_timeout_and_reaches_other_selected_record(self):
        key = "a" * 64
        records = [identity.RC003Candidate("MI RC", False, mock.Mock(id=value), key)
                   for value in ("private-stale-record", "private-live-record")]
        commands = [self.command(blocked=True), self.command()]
        with mock.patch.object(worker, "_command", side_effect=commands):
            chosen = asyncio.run(ble_transport_winrt.select_connectable_candidate(
                records, selected_key=key, probe_timeout=1))
        self.assertIs(chosen, records[1])
        self.assertTrue(all(process.poll() is not None for process in self.processes))

    def test_application_stop_restart_reconnect_and_exit(self):
        from ovb_rc003 import app as app_module, bridge_runtime_status, config

        root = Path(self.directory.name)
        key = "a" * 64
        records = [identity.RC003Candidate("MI RC", False, mock.Mock(id=value), key)
                   for value in ("private-live-record", "private-stale-record")]
        blocked, ready = self.command(blocked=True), self.command()
        sessions, callbacks, apps, stops = [], [], [], []

        def make_session(**kwargs):
            session = mock.Mock(connect=mock.AsyncMock(), close=mock.AsyncMock())
            sessions.append(session)
            callbacks.append(kwargs)
            return session

        def make_app():
            app = app_module.RC003App(launch_voice_program_on_start=False)
            app._selected_remote_key = key
            app._supervisor._retry_delay = 0
            apps.append(app)
            return app

        async def run_service():
            await app_module._run(app_factory=make_app, show_notification_icon=False,
                                  on_runtime_ready=stops.append)

        async def scenario():
            with mock.patch.object(worker, "_command", return_value=blocked):
                first = asyncio.create_task(run_service())
                try:
                    await wait_until(self.marker.exists)
                finally:
                    if stops:
                        stops[-1]()
                    await asyncio.wait_for(first, 3)
            self.assertEqual(sessions, [])
            self.assertIsNone(bridge_runtime_status.read_status(root))
            self.assertIsNotNone(self.processes[-1].poll())
            self.assertNotIn("private-live-record", worker._owners)

            with mock.patch.object(worker, "_command", return_value=ready):
                second = asyncio.create_task(run_service())
                try:
                    await wait_until(lambda: len(sessions) == 1 and sessions[0].connect.await_count == 1)
                    self.assertEqual(bridge_runtime_status.read_status(root).state,
                                     bridge_runtime_status.BridgeConnectionState.CONNECTED)
                    callbacks[0]["on_disconnected"]()
                    await wait_until(lambda: len(sessions) == 2 and sessions[1].connect.await_count == 1)
                    self.assertEqual(bridge_runtime_status.read_status(root).state,
                                     bridge_runtime_status.BridgeConnectionState.CONNECTED)
                finally:
                    stops[-1]()
                    await asyncio.wait_for(second, 3)
            self.assertEqual(len(apps), 2)
            for session in sessions:
                session.connect.assert_awaited_once_with(records[0])
                session.close.assert_awaited_once()
            self.assertIsNone(bridge_runtime_status.read_status(root))
            self.assertTrue(all(process.poll() is not None for process in self.processes))
            self.assertFalse(worker._owners)

        with mock.patch.object(config, "config_root", return_value=root), \
                mock.patch.object(app_module.logging_setup, "get_logger", return_value=logging.getLogger(__name__)), \
                mock.patch.object(app_module.RC003App, "_start_input_channels"), \
                mock.patch.object(app_module.RC003App, "_stop_input_channels"), \
                mock.patch.object(ble_transport_winrt, "discover_candidates", return_value=records), \
                mock.patch.object(ble_transport_winrt, "RC003BleSession", side_effect=make_session):
            asyncio.run(scenario())

    def test_child_failure_diagnostics_survive_real_export_without_private_text(self):
        from ovb_rc003 import log_export, logging_setup

        root = Path(self.directory.name)
        logs = root / "logs"
        logs.mkdir()
        logger = logging.getLogger("ovb_rc003.ble_transport_winrt")
        previous_level = logger.level
        handler = logging_setup.EvidenceFileHandler(
            logs / "app.log", asynchronous=True, encoding="utf-8")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            for failure in ("native", "worker"):
                command = self.command(failure=failure)
                with mock.patch.object(worker, "_command", return_value=command):
                    self.assertFalse(asyncio.run(worker.probe("private-live-record", timeout=5)))
            destination = root / "export.zip"
            result = log_export.export_logs(destination, root=root)
            self.assertEqual(result.outcome, "exported")
            with zipfile.ZipFile(destination) as archive:
                text = archive.read("app.log").decode("utf-8")
            self.assertIn("ATVV candidate probe unavailable: error_type=OSError", text)
            self.assertIn("ATVV candidate probe worker failed: error_type=RuntimeError", text)
            self.assertNotIn("private-", text)
        finally:
            handler.close()
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

    def test_abrupt_parent_exit_ends_its_native_query(self):
        command, env = self.command(blocked=True)
        leave = Path(self.directory.name) / "leave"
        parent_script = f'''
import asyncio, os
from pathlib import Path
from ovb_rc003 import ble_candidate_probe_process as w
w._command = lambda: ({command!r}, dict(os.environ))
async def main():
    task = asyncio.create_task(w.probe("private-live-record", timeout=30))
    while not Path({str(leave)!r}).exists():
        await asyncio.sleep(.01)
    os._exit(0)
asyncio.run(main())
'''
        parent = subprocess.Popen([command[0], "-B", "-c", parent_script], env=env,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.WaitForSingleObject.restype = ctypes.c_ulong
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = None
        try:
            asyncio.run(wait_until(self.marker.exists))
            handle = kernel.OpenProcess(0x00100000, False, json.loads(self.marker.read_text())["pid"])
            self.assertTrue(handle)
            leave.touch()
            self.assertEqual(parent.wait(3), 0)
            self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(3)
            if handle:
                kernel.CloseHandle(handle)


class CleanupFailureTests(unittest.TestCase):
    def test_cleanup_failure_preserves_cancel_so_supervisor_does_not_restart(self):
        process = mock.Mock(stdin=io.BytesIO(), stdout=io.BytesIO())
        process.poll.return_value = None
        process.terminate.side_effect = PermissionError()
        process.kill.side_effect = PermissionError()
        process.wait.side_effect = subprocess.TimeoutExpired("test", 0)

        async def scenario():
            task = asyncio.create_task(worker.probe("cancel-cleanup", timeout=30))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        try:
            with mock.patch.object(worker.subprocess, "Popen", return_value=process), \
                    self.assertLogs("ovb_rc003.ble_transport_winrt", level="ERROR") as logs:
                asyncio.run(scenario())
            self.assertIn("cleanup unconfirmed", logs.output[0])
            self.assertIs(worker._owners["cancel-cleanup"].process, process)
            self.assertFalse(worker._owners["cancel-cleanup"].active)
        finally:
            worker._owners.pop("cancel-cleanup", None)
            process.stdin.close()
            process.stdout.close()

    def test_start_failure_does_not_leave_an_owner(self):
        with mock.patch.object(worker.subprocess, "Popen", side_effect=OSError("cannot start")):
            with self.assertRaises(OSError):
                asyncio.run(worker.probe("failed-start", timeout=1))
        self.assertNotIn("failed-start", worker._owners)

    def test_single_record_never_starts_a_probe_process(self):
        key = "a" * 64
        candidate = identity.RC003Candidate("MI RC", False, mock.Mock(id="single"), key)
        with mock.patch.object(worker.subprocess, "Popen") as start:
            self.assertIs(asyncio.run(ble_transport_winrt.select_connectable_candidate(
                [candidate], selected_key=key)), candidate)
        start.assert_not_called()

    def test_invalid_diagnostic_pipe_data_cannot_turn_into_a_success(self):
        for payload in (b"invalid", b"x" * 2049, b'[["unknown", "OSError"]]',
                        b'[["unavailable", "private-device-path"]]', b'[[[], "OSError"]]'):
            process = mock.Mock(stdin=io.BytesIO(), stdout=io.BytesIO(payload), returncode=0)
            process.poll.return_value = 0
            with self.subTest(payload=payload[:32]), \
                    mock.patch.object(worker.subprocess, "Popen", return_value=process):
                self.assertFalse(asyncio.run(worker.probe("bad-result", timeout=1)))
            self.assertNotIn("bad-result", worker._owners)
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)

    def test_unconfirmed_exit_keeps_owner_and_prevents_another_process(self):
        process = mock.Mock(stdin=io.BytesIO(), stdout=io.BytesIO())
        process.poll.return_value = None
        process.terminate.side_effect = PermissionError()
        process.kill.side_effect = PermissionError()
        process.wait.side_effect = subprocess.TimeoutExpired("test", 0)
        try:
            with mock.patch.object(worker.subprocess, "Popen", return_value=process) as start:
                for _ in range(2):
                    with self.assertRaises(worker.ProbeCleanupError):
                        asyncio.run(worker.probe("failed-cleanup", timeout=0.001))
                start.assert_called_once()
            self.assertIs(worker._owners["failed-cleanup"].process, process)
        finally:
            worker._owners.pop("failed-cleanup", None)
            process.stdin.close()
            process.stdout.close()


if __name__ == "__main__":
    unittest.main()
