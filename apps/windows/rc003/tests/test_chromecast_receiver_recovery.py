"""Actual worker/client/runtime over isolated queues; no UAC, hooks or devices."""
import asyncio
from collections import deque
from contextlib import ExitStack
import ctypes
from dataclasses import replace
import json
import queue
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from ovb_rc003 import chromecast_client as client
from ovb_rc003 import chromecast_hid_worker as worker
from ovb_rc003 import chromecast_runtime as runtime
from ovb_rc003 import chromecast_voice_receiver as voice_module
from ovb_rc003.chromecast_channel import Channel, ChannelError, SessionIdentity
from ovb_rc003.chromecast_pipe_windows import PipeError


class RecoveryContractTests(unittest.TestCase):
    def test_start_returns_for_a_ready_that_failed_before_owner_observed_it(self):
        receiver = client.Client("a" * 64, mode="run", on_edge=mock.Mock())
        def run():
            receiver.failed.set()
            receiver.retry_ready.set()
            receiver.cleanup_confirmed = True
        clock = iter((0, 41))
        with mock.patch.object(receiver, "_run", side_effect=run), \
             mock.patch.object(client, "time", NS(monotonic=lambda: next(clock))):
            receiver.start()
        receiver.thread.join(1)
        self.assertTrue(receiver.retry_ready.is_set())
        self.assertFalse(receiver.ready.is_set())

    def test_restart_is_run_only_opt_in_and_rejects_same_or_invalid_generation(self):
        identity = SessionIdentity.create("a" * 64)
        for mode, recover in (("detect", False), ("setup", False), ("run", False)):
            channel = Channel(identity, commands=True)
            channel.encode("start", mode=mode)
            with self.assertRaises(ChannelError):
                channel.encode("restart", next_generation="b" * 64)
        for generation in (identity.generation, "bad", True, "A" * 64):
            channel = Channel(identity, commands=True)
            channel.encode("start", mode="run", recover=True)
            with self.assertRaises(ChannelError):
                channel.encode("restart", next_generation=generation)

    def test_restart_closes_old_command_channel_and_old_events_are_rejected(self):
        identity = SessionIdentity.create("a" * 64)
        commands = Channel(identity, commands=True)
        commands.encode("start", mode="run", recover=True)
        commands.encode("restart", next_generation="b" * 64)
        self.assertTrue(commands.closed)
        old_events = Channel(identity, commands=False)
        raw = old_events.encode("ready")
        new_events = Channel(SessionIdentity(identity.entity, "b" * 64, identity.token), commands=False)
        with self.assertRaises(ChannelError):
            new_events.decode(raw)


class ReceiverRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.inbox, self.outbox = queue.Queue(), queue.Queue()
        self.release_cleanup = threading.Event()
        self.release_cleanup.set()
        self.cleanup_entered = threading.Event()
        self.done = threading.Event()
        self.worker_errors, self.threads = [], []
        self.taps, self.devices, self.hosts, self.states, self.edges, self.frames = [], [], [], [], [], []
        self.clock_offset = 0
        self.host_release_ok = True
        self.input_release_ok = True
        self.cleanup_ok = True
        self.fail_every_tap = False
        self.selected = True
        self.stale_next_edge = False
        self.peer = NS(pid=123, born=1, sid="test", session=1, image="test")
        fixture = self

        class Tap:
            def __init__(self, entity):
                self.reports = deque()
                self.announced = False
                self.alive = True
                fixture.taps.append(self)

            def start(self):
                pass

            def abort_start(self):
                pass

            def source_alive(self):
                return self.alive

            def poll(self):
                if not self.announced:
                    self.announced = True
                    return [("hook_ready", "", time.monotonic())]
                if fixture.fail_every_tap:
                    return [("error", "hid_host_detached", 0)]
                rows = list(self.reports)
                self.reports.clear()
                return rows

            def close(self, **kwargs):
                fixture.cleanup_entered.set()
                if not fixture.release_cleanup.wait(5):
                    raise AssertionError("fixture cleanup release timeout")
                return fixture.cleanup_ok

        def device_factory(entity):
            device = NS(changed=threading.Event(), open=mock.AsyncMock(),
                        close_voice=mock.AsyncMock(), close=mock.Mock(), poll_voice=lambda: [])
            self.devices.append(device)
            return device

        def voice_factory(*args, **kwargs):
            return NS(open=mock.AsyncMock(), retry_ready=lambda: False, tick=mock.AsyncMock(),
                      stop=mock.Mock(), hardware_stopped=True, close=mock.AsyncMock(),
                      note_activity=mock.Mock(), cancel_input=mock.Mock(), host=mock.Mock())

        def host_factory():
            def stop():
                if not self.host_release_ok:
                    raise RuntimeError("fixture pending host keys")
            host = NS(start=mock.Mock(), stop=mock.Mock(side_effect=stop), request_stop=mock.Mock(), enqueue=mock.Mock())
            self.hosts.append(host)
            return host

        def exit_code(handle, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = 0
            return True

        kernel = NS(GetProcessId=lambda _: 123,
                    WaitForSingleObject=lambda handle, ms: 0 if self.done.wait(min(ms / 1000, 2)) else 258,
                    GetExitCodeProcess=exit_code, CloseHandle=mock.Mock())
        def read_parent():
            value = self.take(self.outbox)
            if value is None and self.done.is_set():
                raise PipeError("pipe_read_failed")
            if value is not None and self.stale_next_edge:
                event = json.loads(value)
                if event["type"] == "edge":
                    self.stale_next_edge = False
                    event["time"] -= 2
                    value = json.dumps(event).encode()
            return value
        parent_pipe = NS(connect=lambda *_: None, read=read_parent,
                         write=self.inbox.put, close=lambda: None)
        self.launch = mock.Mock(side_effect=self.launch_worker)
        self.stack.enter_context(mock.patch.multiple(worker, _selected=lambda _: self.selected,
            SelectedDevice=device_factory, HidTap=Tap, inspect_peer=lambda _: self.peer, verify_peer=lambda *_: None,
            time=NS(monotonic=lambda: time.monotonic() + self.clock_offset)))
        self.stack.enter_context(mock.patch.multiple(client, inspect_peer=lambda _: self.peer,
            verify_peer=lambda *_: None, Pipe=lambda *_a, **_kw: parent_pipe, launch_worker=self.launch,
            api=lambda: (kernel, None), bind_worker_peer=lambda *_: (self.peer, None),
            time=NS(monotonic=lambda: time.monotonic() + self.clock_offset, sleep=time.sleep)))
        self.stack.enter_context(mock.patch.object(client.diagnostics, "request"))
        self.stack.enter_context(mock.patch.object(voice_module, "VoiceReceiver", side_effect=voice_factory))
        self.stack.enter_context(mock.patch.object(runtime, "RECOVERY_DELAYS", (.01, .01, .01)))
        self.services = runtime.RuntimeServices(selected_key=lambda: "a" * 64, logger=mock.Mock(),
            create_voice_host=host_factory, set_input_state=lambda **s: self.states.append(s),
            publish_status=mock.Mock(), start_input=mock.Mock(), stop_input=mock.Mock(), disable_input=mock.Mock(),
            cancel_mappings=mock.Mock(), release_inputs=lambda: self.input_release_ok, route_edge=self.edges.append,
            resume_input=mock.Mock())
        self.owner = runtime.ChromecastRuntime(self.services)
        self.task = asyncio.create_task(self.owner.run())
        await self.until(lambda: self.owner.client is not None and self.owner.client.ready.is_set())

    @staticmethod
    def take(q):
        try:
            return q.get_nowait()
        except queue.Empty:
            return None

    def launch_worker(self, identity, parent):
        def write(raw):
            self.frames.append(json.loads(raw))
            self.outbox.put(raw)
        def run():
            try:
                asyncio.run(worker.receive(identity, parent, NS(read=lambda: self.take(self.inbox), write=write)))
            except BaseException as error:
                self.worker_errors.append(error)
            finally:
                self.done.set()
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return 42

    async def until(self, predicate):
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail(f"fixture wait timeout: states={self.states}, errors={self.worker_errors}")

    async def asyncTearDown(self):
        self.release_cleanup.set()
        self.host_release_ok = self.input_release_ok = True
        self.owner.stopping.set()
        receiver = self.owner.client
        if receiver is not None:
            await asyncio.to_thread(receiver.stop)
        await asyncio.wait_for(self.task, 4)
        for thread in self.threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertFalse(self.worker_errors)

    def report(self, value, *, stale=False):
        self.taps[-1].reports.append(("report", value, time.monotonic() + self.clock_offset - (1.2 if stale else 0)))

    async def lose_and_recover(self):
        old = self.owner.client.identity.generation
        self.report("0300000000000000", stale=True)
        await self.until(lambda: self.owner.client.identity.generation != old and self.owner.client.ready.is_set())
        return old

    async def test_failure_is_visible_before_native_cleanup_and_recovery_waits(self):
        self.release_cleanup.clear()
        self.report("0300000000000000", stale=True)
        await self.until(lambda: self.cleanup_entered.is_set() and self.owner.client.failed.is_set())
        self.assertFalse(self.owner.client.ready.is_set())
        self.assertEqual(self.states[-1]["raw_input_state"], "chromecast_capture_lost")
        self.assertTrue(self.services.disable_input.called)
        self.assertEqual(len(self.taps), 1)
        old = self.owner.client.identity.generation
        self.release_cleanup.set()
        await self.until(lambda: self.owner.client.identity.generation != old and self.owner.client.ready.is_set())
        self.assertEqual(self.launch.call_count, 1)

    async def test_new_session_requires_release_then_delivers_new_press_once(self):
        self.report("0300000000000000")
        await self.until(lambda: bool(self.edges))
        await self.lose_and_recover()
        count = len(self.edges)
        self.report("0300000000000000")  # Still held from the old session.
        self.report("0000000000000000")
        await asyncio.sleep(.15)
        self.assertEqual(len(self.edges), count)
        self.report("0300000000000000")
        self.report("0000000000000000")
        await self.until(lambda: len(self.edges) == count + 2)
        self.assertEqual([e.action for e in self.edges[-2:]], ["down", "up"])
        self.assertEqual(len(self.hosts), 2)
        self.assertTrue(self.hosts[0].stop.called)
        self.assertEqual(self.devices[0].close.call_count, 1)
        self.assertEqual(self.launch.call_count, 1)

    async def test_host_release_failure_does_not_create_second_session(self):
        self.host_release_ok = False
        self.report("0300000000000000", stale=True)
        await self.until(lambda: self.states[-1].get("raw_input_state") == "failed_stopping")
        self.assertEqual(len(self.taps), 1)
        self.assertEqual(len(self.hosts), 1)
        self.host_release_ok = True
        await self.until(lambda: len(self.taps) == 2 and self.owner.client.ready.is_set())

    async def test_pending_button_release_does_not_create_second_session(self):
        self.input_release_ok = False
        self.report("0300000000000000", stale=True)
        await self.until(lambda: self.states[-1].get("raw_input_state") == "failed_stopping")
        self.assertEqual(len(self.taps), 1)
        self.input_release_ok = True
        await self.until(lambda: len(self.taps) == 2 and self.owner.client.ready.is_set())

    async def test_suspend_gap_reauthenticates_and_recovers_without_new_elevation(self):
        old = self.owner.client.identity.generation
        self.clock_offset = 11
        await self.until(lambda: self.owner.client.identity.generation != old and self.owner.client.ready.is_set())
        self.assertEqual(self.launch.call_count, 1)
        self.assertTrue(any(f.get("reason") == "peer_lost" for f in self.frames))
        self.report("0000000000000000")
        self.report("0300000000000000")
        self.report("0000000000000000")
        await self.until(lambda: any(e.action == "up" for e in self.edges))

    async def test_changed_host_rebinds_only_after_old_tap_closes(self):
        old = self.owner.client.identity.generation
        self.taps[-1].alive = False
        await self.until(lambda: self.owner.client.identity.generation != old and self.owner.client.ready.is_set())
        self.assertEqual(self.devices[0].close.call_count, 1)
        self.assertEqual(self.launch.call_count, 1)

    async def test_repeated_failures_exhaust_fixed_budget(self):
        self.fail_every_tap = True
        await self.until(lambda: self.owner.client.finished.is_set())
        self.assertEqual(len(self.taps), 4)
        self.assertEqual(len([f for f in self.frames if f["type"] == "retry"]), 3)
        self.assertEqual(self.launch.call_count, 1)

    async def test_stop_during_cleanup_prevents_restart(self):
        self.release_cleanup.clear()
        self.report("0300000000000000", stale=True)
        await self.until(lambda: self.cleanup_entered.is_set() and self.owner.client.failed.is_set())
        self.owner.stopping.set()
        self.owner.client.cancel.set()
        self.release_cleanup.set()
        await self.until(lambda: self.owner.client.finished.is_set())
        self.assertEqual(len(self.taps), 1)

    async def test_device_selection_change_is_terminal_without_retry(self):
        self.selected = False
        await self.until(lambda: self.owner.client.finished.is_set())
        self.assertEqual(len(self.taps), 1)
        self.assertFalse(any(f["type"] == "retry" for f in self.frames))

    async def test_native_cleanup_failure_does_not_retry_over_dirty_resources(self):
        self.cleanup_ok = False
        self.report("0300000000000000", stale=True)
        await self.until(lambda: self.owner.client.finished.is_set())
        self.assertEqual(len(self.taps), 1)
        self.assertFalse(any(f["type"] == "retry" for f in self.frames))

    async def test_parent_detected_delayed_event_requests_worker_recovery(self):
        old = self.owner.client.identity.generation
        self.stale_next_edge = True
        self.report("0300000000000000")
        await self.until(lambda: self.owner.client.identity.generation != old and self.owner.client.ready.is_set())
        self.assertFalse(any(edge.action == "down" for edge in self.edges))
        self.assertEqual(self.launch.call_count, 1)

    async def test_real_voice_host_releases_then_accepts_first_attempt_of_new_session(self):
        from tests.test_chromecast_voice import HostTests
        from ovb_rc003.app import RC003App
        fixture = HostTests("test_delayed_confirmation_preserves_pcm_without_exhausting_writer")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        audio = fixture.owner._voice_audio
        def open_audio():
            audio.writer = mock.Mock()
            audio.sink = mock.Mock()
            fixture.owner._voice_shortcut.runtime_mic_confirmed = None
            return True
        audio.open.side_effect = open_audio
        def host_factory():
            return RC003App._create_chromecast_voice_host(fixture.owner)
        self.owner.services = replace(self.services, create_voice_host=host_factory)
        old_host = self.owner.voice_host = fixture.host
        old_host.start(self.owner.client)
        self.owner.client.on_voice = old_host.enqueue
        def press(host):
            host.enqueue(dict(event="host_start", attempt=1, data="epoch/0", time=time.monotonic()))
        press(old_host)
        await self.until(lambda: old_host.engaged)
        await self.lose_and_recover()
        self.assertTrue(old_host.closed)
        self.assertFalse(old_host.thread.is_alive())
        new_host = self.owner.voice_host
        self.assertIsNot(old_host, new_host)
        press(new_host)
        await self.until(lambda: new_host.engaged)
        self.assertEqual(audio.open.call_count, 2)
        self.assertEqual(new_host.attempt, 1)
