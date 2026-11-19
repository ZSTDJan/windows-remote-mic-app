"""Real PyWinRT cancellation adapter and owned receiver teardown; no hardware."""
import asyncio
from collections import deque
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from winrt.runtime._internals import wrap_async
from ovb_rc003 import chromecast_cleanup as cleanup
from ovb_rc003 import chromecast_client as clients
from ovb_rc003 import chromecast_pipe_windows as pipes
from ovb_rc003 import chromecast_device_windows as devices
from ovb_rc003 import chromecast_hid_worker as worker
from ovb_rc003 import chromecast_worker as entry
from ovb_rc003 import chromecast_voice_receiver as receivers
from ovb_rc003.chromecast_channel import Channel, SessionIdentity
from ovb_rc003.chromecast_pipe_windows import PipeError
from tests.test_chromecast_voice_discovery import service, session


class PendingNativeOperation:
    """Controlled native completion using the exact shipped WinRT await adapter."""
    def __init__(self):
        self.completed = None
        self.cancel_count = 0

    def cancel(self):
        self.cancel_count += 1

    def get_results(self):
        return 0

    def finish(self):
        if self.completed is not None:
            self.completed(self, None)


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.timeout = mock.patch.object(cleanup, "CLEANUP_TIMEOUT", .03)
        self.timeout.start()
        self.addCleanup(self.timeout.stop)
        self.pending = []

    async def asyncTearDown(self):
        for op, task in self.pending:
            op.finish()
            await asyncio.gather(task, return_exceptions=True)

    def native_task(self):
        op = PendingNativeOperation()
        task = asyncio.ensure_future(wrap_async(op))
        self.pending.append((op, task))
        return op, task

    def device(self, *, pending=True):
        target = devices.SelectedDevice("a" * 64)
        voice = service()
        target.device = NS(close=mock.Mock(), connection_status=1,
                           device_access_information=NS(current_status=1))
        target.voice_service = voice
        target.voice_session = session()
        target._voice_services = [voice]
        target._voice_subscribed = voice.chars[1:]
        target._voice_tokens = [(c, object()) for c in voice.chars[1:]]
        if pending:
            op = PendingNativeOperation()
            def begin(_):
                task = asyncio.ensure_future(wrap_async(op))
                self.pending.append((op, task))
                return task
            voice.chars[1].write_client_characteristic_configuration_descriptor_async = mock.Mock(side_effect=begin)
        return target, voice

    async def test_normal_result_and_failure_keep_original_outcome(self):
        async def ok():
            return 12
        self.assertEqual(await cleanup.finish_cleanup(asyncio.create_task(ok())), 12)
        async def fail():
            raise OSError("test")
        with self.assertRaises(OSError):
            await cleanup.finish_cleanup(asyncio.create_task(fail()))

    async def test_real_winrt_adapter_does_not_make_timeout_wait_for_native_completion(self):
        op, task = self.native_task()
        started = time.monotonic()
        with self.assertRaises(cleanup.PendingCleanup) as caught:
            await cleanup.finish_cleanup(task)
        await asyncio.sleep(0)
        self.assertIs(caught.exception.task, task)
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(op.cancel_count, 1)
        self.assertFalse(task.done())
        # Re-observation must not send a second Task.cancel() and cut through
        # the adapter's native-completion barrier.
        with self.assertRaises(cleanup.PendingCleanup):
            await cleanup.finish_cleanup(task, cancel=True)
        self.assertEqual(op.cancel_count, 1)
        self.assertEqual(task.cancelling(), 1)
        self.assertFalse(task.done())

    async def test_cancellation_that_settles_does_not_require_retirement(self):
        task = asyncio.create_task(asyncio.sleep(30))
        await asyncio.sleep(0)
        self.assertIsNone(await cleanup.finish_cleanup(task, cancel=True))
        self.assertTrue(task.cancelled())

    async def test_outer_cancellation_preserves_the_native_task(self):
        op, task = self.native_task()
        waiter = asyncio.create_task(cleanup.finish_cleanup(task))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(cleanup.PendingCleanup):
            await waiter
        await asyncio.sleep(0)
        self.assertEqual(op.cancel_count, 1)
        self.assertFalse(task.done())

    async def test_unsubscribe_timeout_preserves_resources_and_does_not_write_again(self):
        target, voice = self.device()
        evidence = []
        target.cleanup_observer = evidence.append
        with self.assertRaises(PipeError):
            await target.close_voice()
        self.assertTrue(target.cleanup_pending)
        self.assertTrue(target._voice_cleanup_failed)
        voice.close.assert_not_called()
        target.voice_session.close.assert_not_called()
        target.device.close.assert_not_called()
        for c in voice.chars[1:]:
            c.remove_value_changed.assert_called_once()
        voice.chars[2].write_client_characteristic_configuration_descriptor_async.assert_not_called()
        self.assertTrue(any(r["step"] == "subscription_disable" and r["outcome"] == "timeout" for r in evidence))
        self.assertEqual(evidence[-1]["remaining_services"], 1)
        with self.assertRaises(PipeError):
            target.close()
        with self.assertRaises(PipeError):
            await target.close_voice()
        # Even a late completion cannot recycle this failed owner.
        self.pending[-1][0].finish()
        await asyncio.sleep(.01)
        self.assertTrue(target.cleanup_pending)

    async def test_normal_unsubscribe_still_releases_all_owners(self):
        target, voice = self.device(pending=False)
        held, native = target.voice_session, target.device
        await target.close_voice()
        target.close()
        self.assertFalse(target.cleanup_pending)
        self.assertFalse(target._voice_cleanup_failed)
        voice.close.assert_called_once()
        held.close.assert_called_once()
        native.close.assert_called_once()
        for c in voice.chars[1:]:
            c.write_client_characteristic_configuration_descriptor_async.assert_awaited_once()

    async def test_pending_voice_command_is_retained_on_close_timeout(self):
        op, task = self.native_task()
        receiver = object.__new__(receivers.VoiceReceiver)
        receiver._startup = deque()
        receiver.pending = (NS(value=b"\x0d\x01", attempt=1), task)
        receiver._pending_started = time.monotonic()
        receiver._observe_command = mock.Mock()
        await asyncio.sleep(0)
        with self.assertRaises(cleanup.PendingCleanup):
            await receiver.close()
        self.assertIs(receiver.pending[1], task)
        self.assertFalse(task.done())
        self.assertEqual(op.cancel_count, 1)
        self.assertFalse(any(call.args[2] == "cancel_settled" for call in receiver._observe_command.call_args_list))

    async def run_worker(self, target, *, detach=True):
        identity = SessionIdentity.create("a" * 64)
        commands = Channel(identity, commands=True)
        incoming = deque([commands.encode("start", mode="run")])
        frames = []
        def write(raw):
            row = json.loads(raw)
            frames.append(row)
            if row["type"] == "ready":
                incoming.append(commands.encode("stop"))
        tap = NS(start=mock.Mock(), abort_start=mock.Mock(), source_alive=lambda: True,
                 close=mock.Mock(return_value=detach),
                 poll=mock.Mock(side_effect=[[("hook_ready", "", time.monotonic())]]))
        target.open = mock.AsyncMock()
        pipe = NS(read=lambda: incoming.popleft() if incoming else None, write=write)
        with mock.patch.multiple(worker, SelectedDevice=lambda _: target, HidTap=lambda _: tap,
                                 _selected=lambda _: True, inspect_peer=lambda _: NS(), verify_peer=mock.Mock()):
            result = await worker._receive_once(identity, NS(pid=123), pipe, {"attempt": 0})
        return result, frames, tap

    async def test_worker_retires_after_verified_hook_detach_and_keeps_native_owners(self):
        target, voice = self.device()
        with self.assertRaises(cleanup.RetireWorker):
            await self.run_worker(target)
        voice.close.assert_not_called()
        target.device.close.assert_not_called()

    async def test_worker_normal_stop_returns_without_retirement(self):
        target, voice = self.device(pending=False)
        result, frames, tap = await self.run_worker(target)
        self.assertEqual(result, 0)
        self.assertTrue(any(f["type"] == "stopped" for f in frames))
        tap.close.assert_called_once()
        voice.close.assert_called_once()

    async def test_failed_hook_detach_cannot_force_a_process_exit(self):
        target, voice = self.device()
        task = asyncio.create_task(self.run_worker(target, detach=False))
        for _ in range(100):
            if target.cleanup_pending:
                break
            await asyncio.sleep(.01)
        self.assertTrue(target.cleanup_pending)
        self.assertFalse(task.done())
        self.pending[-1][0].finish()
        result, frames, _ = await asyncio.wait_for(task, 1)
        self.assertEqual(result, 2)
        self.assertFalse(any(f["type"] in ("retry", "stopped") for f in frames))
        voice.close.assert_not_called()


class OwnedProcessTests(unittest.TestCase):
    def test_real_child_retires_without_waiting_for_pending_winrt_cancel(self):
        # A real child and the real dependency wrapper exercise the production
        # self-exit boundary. No Bluetooth, UAC or other process is touched.
        code = '''
import asyncio, os, ovb_rc003
if os.environ.get('REMOTE_MIC_TEST_CLEANUP_NATIVE'):
    ovb_rc003.__path__.insert(0, os.environ['REMOTE_MIC_TEST_CLEANUP_NATIVE'])
from winrt.runtime._internals import wrap_async
from ovb_rc003.chromecast_cleanup import finish_cleanup, PendingCleanup, RetireWorker
from ovb_rc003.chromecast_worker import _receive_owned
class Op:
    completed = None
    def cancel(self): pass
    def get_results(self): return 0
async def receive(*args):
    task = asyncio.ensure_future(wrap_async(Op()))
    try:
        await finish_cleanup(task)
    except PendingCleanup:
        raise RetireWorker('cleanup_unconfirmed')
asyncio.run(_receive_owned(receive, None, None, None))
'''
        child = subprocess.Popen([sys.executable, "-B", "-c", code],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            stdout, stderr = child.communicate(timeout=8)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
        self.assertEqual(child.returncode, 2, (stdout, stderr))
        # Use an actual Win32 process handle in the existing Client.stop path.
        # A failed exit remains a failure, but must not remain a live owner and
        # prevent the next service from starting.
        kernel, _ = pipes.api()
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, child.pid)
        self.assertTrue(handle)
        receiver = clients.Client("a" * 64, mode="run", on_edge=mock.Mock())
        receiver._process, receiver._kernel = handle, kernel
        receiver.thread = threading.Thread(target=lambda: None)
        receiver.thread.start()
        receiver.thread.join()
        with mock.patch.object(clients.diagnostics, "_crash_unavailable"):
            receiver.stop()
        self.assertTrue(receiver.cleanup_confirmed)
        self.assertFalse(receiver.exit_succeeded)
        self.assertIsNone(receiver._process)
        self.assertIsNone(receiver._worker_process)
        restarted = subprocess.run([sys.executable, "-B", "-c", "raise SystemExit(0)"],
                                   capture_output=True, timeout=5,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(restarted.returncode, 0)

    def test_normal_owned_entry_preserves_exit_result(self):
        async def receive(*args):
            return 0
        with mock.patch.object(entry.os, "_exit") as stop:
            self.assertEqual(asyncio.run(entry._receive_owned(receive, None, None, None)), 0)
        stop.assert_not_called()
