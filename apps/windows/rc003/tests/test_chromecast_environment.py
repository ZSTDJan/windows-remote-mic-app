"""Synthetic environment/IPC failure regressions; no live device operations."""
import json
import asyncio
import itertools
import threading
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import chromecast_diagnostics_windows as evidence
from ovb_rc003 import chromecast_device_windows as device
from ovb_rc003 import chromecast_client as client, chromecast_channel as channel
from ovb_rc003 import chromecast_worker as worker
from ovb_rc003 import chromecast_pipe_windows as pipes
from ovb_rc003.chromecast_buttons import empty_stop_details

ENTITY = "a" * 64
PATH = r"\\?\HID#dev_vid&0118d1_pid&9450_rev&0110#private-address"


class InputWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_absent_interface_can_appear_without_another_service_start(self):
        target = device.SelectedDevice(ENTITY)
        with mock.patch.object(device, "input_handle", side_effect=[pipes.PipeError("input_interface_unavailable"), 0x29]) as lookup, \
             mock.patch.object(device.asyncio, "sleep", new_callable=mock.AsyncMock) as sleep:
            self.assertEqual(await target._resolve_input(), 0x29)
        self.assertEqual(lookup.call_count, 2)
        sleep.assert_awaited_once_with(.25)
        self.assertFalse(target.input_pending)

    async def test_unsupported_revision_is_not_retried(self):
        target = device.SelectedDevice(ENTITY)
        with mock.patch.object(device, "input_handle", side_effect=pipes.PipeError("unsupported_layout")) as lookup, \
             self.assertRaisesRegex(pipes.PipeError, "unsupported_layout"):
            await target._resolve_input()
        lookup.assert_called_once()

    async def test_wait_is_cancellable_and_radio_change_is_rejected(self):
        target = device.SelectedDevice(ENTITY)
        with mock.patch.object(device, "input_handle", side_effect=pipes.PipeError("input_interface_unavailable")):
            task = asyncio.create_task(target._resolve_input())
            await asyncio.sleep(0)
            self.assertTrue(target.input_pending)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(target.input_pending)
        target.changed.set()
        with self.assertRaisesRegex(pipes.PipeError, "radio_ambiguous"):
            await target._resolve_input()

    async def test_worker_deadline_cancels_wait_and_preserves_missing_interface_reason(self):
        identity = channel.SessionIdentity.create(ENTITY)
        incoming = channel.Channel(identity, commands=True)
        outgoing = channel.Channel(identity, commands=False)
        commands = [incoming.encode("start", mode="run")]
        received, cancelled = [], []
        async def pending_open():
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)
        target = SimpleNamespace(input_pending=True, changed=threading.Event(), open=pending_open, close=mock.Mock())
        clock = itertools.count()
        pipe = SimpleNamespace(read=lambda: commands.pop(0) if commands else None,
                               write=lambda raw: received.append(outgoing.decode(raw)))
        with mock.patch.multiple(worker, _selected=lambda _: True, sensitive_logging_enabled=lambda: True,
                                 inspect_peer=lambda _: None, verify_peer=lambda *_: None,
                                 SelectedDevice=lambda _: target,
                                 time=SimpleNamespace(monotonic=lambda: next(clock)),
                                 ParentLease=lambda _: SimpleNamespace(alive=lambda _: True, renew=lambda _: None)), \
             mock.patch.object(worker, "Capture") as capture:
            await worker.receive(identity, SimpleNamespace(pid=1), pipe)
        self.assertEqual(received[-1]["reason"], "input_interface_unavailable")
        self.assertEqual(cancelled, [True])
        target.close.assert_called_once()
        capture.assert_not_called()


class EnvironmentTests(unittest.TestCase):
    def test_interface_absence_identity_mismatch_and_revision_are_distinct(self):
        for paths, resolver, reason in (
            ([], lambda _: ENTITY, "input_interface_unavailable"),
            ([PATH], lambda _: "b" * 64, "input_identity_unconfirmed"),
            ([PATH.replace("0110", "0111")], lambda _: ENTITY, "unsupported_layout"),
            ([PATH], mock.Mock(side_effect=OSError()), "input_identity_unconfirmed"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(pipes.PipeError, reason):
                device.input_handle(ENTITY, enumerate_paths=lambda **_: paths, resolve=resolver)
            identity = channel.SessionIdentity.create(ENTITY)
            raw = channel.Channel(identity, commands=False).encode("error", reason=reason)
            self.assertEqual(channel.Channel(identity, commands=False).decode(raw)["reason"], reason)

    def test_raw_snapshot_does_not_log_paths_or_other_device_identity(self):
        with mock.patch.object(evidence.raw_input_windows, "enumerate_matching_device_paths", return_value=[PATH, "private-keyboard"]), \
             mock.patch.object(evidence.remote_selection, "raw_path_key", side_effect=[ENTITY, "b" * 64]):
            snapshot = evidence._raw_snapshot(ENTITY)
        text = json.dumps(snapshot)
        self.assertEqual(snapshot["interfaces"][0]["pnp_tuple"], "18d1:9450:0110")
        self.assertNotIn("private", text)
        self.assertNotIn("b" * 12, text)

    def test_failure_reason_survives_pipe_disconnect_after_failed_diagnostic(self):
        receiver = client.Client(ENTITY, mode="run", on_edge=mock.Mock())
        events = channel.Channel(receiver.identity, commands=False)
        failed = events.encode("diagnostic", stage="receive", phase="failed", reason="input_payload_unavailable", **empty_stop_details())
        pipe = mock.Mock()
        pipe.read.side_effect = [failed, pipes.PipeError("pipe_read_failed")]
        peer = pipes.Peer(1, 2, "sid", 3, "image")
        kernel = SimpleNamespace(GetProcessId=lambda _: 1, WaitForSingleObject=lambda *_: 0,
                                 GetExitCodeProcess=lambda *_: True, CloseHandle=mock.Mock())
        with mock.patch.multiple(client, inspect_peer=lambda _: peer, Pipe=lambda *a, **kw: pipe,
                                 launch_worker=lambda *_: 42, api=lambda: (kernel, None),
                                 bind_worker_peer=lambda *_: (peer, 43), verify_peer=lambda *_: None), \
             mock.patch.object(evidence, "request") as request:
            receiver._run()
        self.assertEqual(receiver.reason, "input_payload_unavailable")
        self.assertEqual(receiver.failure_code, "pipe_read_failed")
        request.assert_called_once_with(ENTITY, "input_payload_unavailable")
        receiver.on_edge.assert_not_called()

    def test_recoverable_diagnostic_and_cleanup_do_not_override_primary(self):
        receiver = client.Client(ENTITY, mode="run", on_edge=mock.Mock())
        with mock.patch.object(evidence, "request") as request:
            receiver._observe_diagnostic(dict(stage="receive", phase="done", reason="input_payload_unavailable"))
            self.assertEqual(receiver._first_worker_failure, "")
            receiver._observe_diagnostic(dict(stage="receive", phase="failed", reason="input_payload_unavailable"))
            receiver._observe_diagnostic(dict(stage="voice_stop", phase="failed", reason="cleanup_unconfirmed"))
        self.assertEqual(receiver._first_worker_failure, "input_payload_unavailable")
        request.assert_called_once()

    def test_timeout_keeps_quick_snapshot_and_omits_exception_text(self):
        settings = {"MaxEtwBytes": {"state": "missing", "ready": False}}
        with mock.patch.object(evidence, "_raw_snapshot", return_value={"total": 0}), \
             mock.patch("ovb_rc003.chromecast_etw_windows.capture_settings_snapshot", return_value=settings), \
             mock.patch.object(evidence, "_environment", side_effect=subprocess.TimeoutExpired("private-command", 25)), \
             self.assertLogs("ovb_rc003", level="INFO") as logs:
            evidence._collect(ENTITY, "ready", 123)
        self.assertIn('"raw_input":{"total":0}', logs.output[0])
        self.assertIn('"capture_settings":{"MaxEtwBytes":{"state":"missing","ready":false}}', logs.output[0])
        self.assertIn("query_timeout", logs.output[1])
        self.assertNotIn("private-command", str(logs.output))

    def test_requests_are_bounded_throttled_and_never_run_inline(self):
        with mock.patch.object(evidence, "_pending", {}), mock.patch.object(evidence, "_recent", {}), \
             mock.patch.object(evidence, "_running", False), mock.patch.object(evidence.threading, "Thread") as thread, \
             mock.patch.object(evidence, "_collect") as collect:
            evidence.request(ENTITY, "ready")
            evidence.request(ENTITY, "ready")
            evidence.request(ENTITY, "input_payload_unavailable")
            evidence.request(ENTITY, "unsupported_layout")
            evidence.request(ENTITY, "private-text")
            self.assertEqual(len(evidence._pending), 2)
            thread.assert_called_once()
            collect.assert_not_called()

    def test_environment_subprocess_is_hidden_bounded_and_uses_fixed_query(self):
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (b'{}', None)
        process.poll.return_value = 0
        with mock.patch.object(evidence.subprocess, "Popen", return_value=process) as run:
            self.assertEqual(evidence._environment(), {})
        process.communicate.assert_called_once_with(timeout=25)
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertNotIn("-ExecutionPolicy", run.call_args.args[0])

    def test_timeout_and_application_exit_kill_only_owned_query(self):
        process = mock.Mock(returncode=-1)
        process.communicate.side_effect = [subprocess.TimeoutExpired('query',25), (b'',None)]
        process.poll.return_value = None
        with mock.patch.object(evidence.subprocess, "Popen", return_value=process), self.assertRaises(subprocess.TimeoutExpired):
            evidence._environment()
        process.kill.assert_called_once()
        self.assertIsNone(evidence._child)
        process.reset_mock()
        with mock.patch.object(evidence, "_closing", False), mock.patch.object(evidence, "_child", process):
            evidence._shutdown()
            self.assertTrue(evidence._closing)
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=1)
