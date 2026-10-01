"""Startup failures keep bounded evidence without touching a real HID host."""
import asyncio
from collections import deque
import json
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from ovb_rc003 import chromecast_hid_tap_windows as tap
from ovb_rc003 import chromecast_hid_worker as worker
from ovb_rc003.chromecast_channel import Channel, ChannelError, SessionIdentity
from ovb_rc003.chromecast_observation import Observation, valid_record


ENTITY = "a" * 64


class StartupEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_thread_and_hook_ready_timeouts_are_distinguishable(self):
        for wait_step in ("wait_device", "wait_start", "wait_ready"):
            with self.subTest(step=wait_step):
                identity = SessionIdentity.create(ENTITY)
                commands, events = Channel(identity, commands=True), Channel(identity, commands=False)
                inbox = deque([commands.encode("start", mode="run")])
                rows, gate = [], threading.Event()
                target = SimpleNamespace(changed=threading.Event(), open=mock.AsyncMock(),
                                         close_voice=mock.AsyncMock(), close=mock.Mock())
                if wait_step == "wait_device":
                    async def pending_open(**kwargs):
                        await asyncio.Event().wait()
                    target.open.side_effect = pending_open
                clock = iter(range(0, 1000, 3))
                instance = SimpleNamespace(start=lambda: gate.wait(2) if wait_step == "wait_start" else None,
                    source_alive=lambda: True, poll=lambda: [], close=lambda **kwargs: True,
                    abort_start=mock.Mock())
                def write(raw):
                    row = events.decode(raw)
                    rows.append(row)
                    if row.get("record", {}).get("step") == "wait_start":
                        gate.set()
                with mock.patch.object(worker, "time", SimpleNamespace(monotonic=lambda:next(clock))), \
                     mock.patch.object(worker, "ParentLease", return_value=SimpleNamespace(alive=lambda now:True, renew=lambda now:None)), \
                     mock.patch.object(worker, "HidTap", return_value=instance), \
                     mock.patch.object(worker, "SelectedDevice", return_value=target), \
                     mock.patch.object(worker, "_selected", return_value=True), \
                     mock.patch.object(worker, "inspect_peer"), mock.patch.object(worker, "verify_peer"):
                    await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1),
                        SimpleNamespace(read=lambda:inbox.popleft() if inbox else None, write=write)), 4)
                self.assertTrue(any(r.get("record", {}).get("step") == wait_step for r in rows))
                self.assertFalse(any(r["type"] in ("ready", "edge", "voice") for r in rows))

    async def run_failure(self, step, *, cleanup_error=False):
        identity = SessionIdentity.create(ENTITY)
        incoming, outgoing = Channel(identity, commands=True), Channel(identity, commands=False)
        inbox = deque([incoming.encode("start", mode="run", voice=True)])
        received = []
        target = SimpleNamespace(changed=threading.Event(), open=mock.AsyncMock(),
                                 close_voice=mock.AsyncMock(), close=mock.Mock())
        session, script = mock.Mock(), mock.Mock()
        session.create_script.return_value = script
        attach = mock.Mock(return_value=session)
        layout = mock.Mock(return_value=(0x1234, 8))
        failure = PermissionError(13, "PRIVATE_PATH_AND_ADDRESS")
        failure.winerror = 5
        if step == "driver_layout":
            layout.side_effect = failure
        elif step == "attach":
            attach.side_effect = failure
        elif step == "create_script":
            session.create_script.side_effect = failure
        elif step == "load_script":
            script.load.side_effect = failure
        elif step == "script_message":
            def script_message():
                callback = script.on.call_args.args[1]
                callback(dict(type="error", description="PRIVATE_SCRIPT", lineNumber=42), None)
            script.load.side_effect = script_message
        if cleanup_error:
            script.unload.side_effect = OSError("PRIVATE_CLEANUP")
        instance = tap.HidTap(ENTITY)
        with mock.patch.dict("sys.modules", frida=SimpleNamespace(attach=attach)), \
             mock.patch("ovb_rc003.chromecast_gadget_windows.loaded_or_pending_for_host", return_value=False), \
             mock.patch.object(tap, "selected_host_and_address", return_value=(42, "010203040506")), \
             mock.patch.object(tap, "_driver_layout", layout), \
             mock.patch.object(worker, "HidTap", return_value=instance), \
             mock.patch.object(worker, "SelectedDevice", return_value=target), \
             mock.patch.object(worker, "_selected", return_value=True), \
             mock.patch.object(worker, "inspect_peer"), mock.patch.object(worker, "verify_peer"):
            result = await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1),
                SimpleNamespace(read=lambda: inbox.popleft() if inbox else None,
                                write=lambda raw: received.append(outgoing.decode(raw)))), 3)
        return result, received, session, script

    async def test_each_start_failure_retains_step_type_code_without_false_cleanup_failure(self):
        for step in ("driver_layout", "attach", "create_script", "load_script"):
            with self.subTest(step=step):
                result, received, session, _ = await self.run_failure(step)
                self.assertEqual(result, 0)
                rows = [r["record"] for r in received if r["type"] == "evidence"
                        and r["record"]["kind"] == "hid_startup"]
                failed = [r for r in rows if r["state"] == "failed" and r["step"] != "worker_failure"
                          and not r['step'].startswith('attach_')]
                self.assertEqual([(r["step"], r["error_type"], r["native_code"]) for r in failed],
                                 [(step, "PermissionError", 5)])
                self.assertEqual(received[-1]["reason"], "hid_capture_failed")
                self.assertFalse(any(r["type"] in ("ready", "edge", "voice") for r in received))
                self.assertFalse(any(r.get("reason") == "cleanup_failed" for r in received))
                self.assertNotIn("PRIVATE", json.dumps(received))
                self.assertTrue(all(valid_record(r) for r in rows))
                if step in ("create_script", "load_script"):
                    session.detach.assert_called_once()

    async def test_real_cleanup_error_remains_a_failure(self):
        result, rows, _, _ = await self.run_failure("load_script", cleanup_error=True)
        self.assertEqual(result, 2)
        self.assertTrue(any(r.get("stage") == "capture_stop" and r.get("phase") == "failed" for r in rows))
        self.assertTrue(any(r.get("record", {}).get("step") == "startup_script_unload" for r in rows))

    def test_normal_cleanup_errors_keep_numeric_codes_and_distinct_stage(self):
        instance = tap.HidTap(ENTITY)
        instance.script, instance.session = mock.Mock(), mock.Mock()
        instance.script.unload.side_effect = PermissionError(13, "PRIVATE")
        instance.session.detach.side_effect = OSError(6, "PRIVATE")
        self.assertFalse(instance.close())
        rows = instance.poll_startup()
        self.assertEqual([(r["step"], r["native_code"]) for r in rows],
                         [("cleanup_script_unload", 13), ("cleanup_session_detach", 6)])
        self.assertTrue(all(valid_record(r) for r in rows))
        self.assertNotIn("PRIVATE", json.dumps(rows))

    def test_rejected_metadata_cannot_interrupt_observation(self):
        rows = []
        observation = Observation(rows.append)
        for invalid in (None, {}, [], "PRIVATE"):
            observation.hid_startup(invalid)
        self.assertEqual(rows, [])
        observation.hid_startup(tap.startup_record("driver_read", "begin"))
        self.assertEqual(len(rows), 1)

    async def test_javascript_error_line_survives_before_ready(self):
        result, rows, _, _ = await self.run_failure("script_message")
        self.assertEqual(result, 0)
        evidence = [r["record"] for r in rows if r.get("record", {}).get("step") == "script_message"]
        self.assertEqual(evidence[0]["script_line"], 42)
        self.assertNotIn("PRIVATE_SCRIPT", json.dumps(rows))
        self.assertEqual(rows[-1]["reason"], "hid_capture_failed")

    def test_cause_and_script_error_are_bounded_and_do_not_contain_message(self):
        import frida
        from ovb_rc003.chromecast_observation import HID_ERROR_TYPES
        self.assertTrue({name for name in dir(frida) if name.endswith('Error')} <= HID_ERROR_TYPES)
        instance = tap.HidTap(ENTITY)
        cause = OSError("PRIVATE")
        cause.winerror = -2147024891
        error = tap.TapError("hid_source_unconfirmed")
        error.__cause__ = cause
        instance._startup_record("selected_host", "failed", error=error)
        instance._startup_record("script_message", "failed", script_line=17)
        rows = instance.poll_startup()
        self.assertEqual(rows[0]["cause_type"], "OSError")
        self.assertEqual(rows[0]["cause_code"], 0x80070005)
        self.assertEqual(rows[1]["script_line"], 17)
        self.assertNotIn("PRIVATE", json.dumps(rows))
        for _ in range(200):
            instance._startup_record("attach", "begin")
        instance._startup_record("attach", "failed", error=PermissionError())
        rows = instance.poll_startup()
        self.assertEqual(len(rows), 129)
        self.assertEqual(rows[0]["details"], {"dropped": 73})
        self.assertEqual(rows[-1]["state"], "failed")
        self.assertEqual(instance.poll_startup(), [])

    def test_contract_rejects_private_text_and_malformed_fields(self):
        instance = tap.HidTap(ENTITY)
        instance._startup_record("attach", "failed", error=PermissionError())
        row = instance.poll_startup()[0]
        sender = Channel(SessionIdentity.create(ENTITY), commands=False)
        self.assertLess(len(sender.encode("evidence", record=row)), 2048)
        for changes in ({"message": "PRIVATE"}, {"step": "PRIVATE"}, {"error_type": "PRIVATE"},
                        {"step": []}, {"native_code": True}, {"host_pid": -2}, {"script_line": 65536},
                        {"reason": "PRIVATE"}, {"details": {"path": "PRIVATE"}},
                        {"details": {"pdb_key": "PRIVATE"}}, {"details": {"constructor_prefix": "zz"}},
                        {"details": {"driver_sha256": "PRIVATE"}}, {"details": {"size": True}},
                        {"details": {"constructor_prefix": "00"*65}}):
            with self.subTest(changes=changes), self.assertRaises(ChannelError):
                self.assertFalse(valid_record(dict(row, **changes)))
                sender = Channel(SessionIdentity.create(ENTITY), commands=False)
                sender.encode("evidence", record=dict(row, **changes))
