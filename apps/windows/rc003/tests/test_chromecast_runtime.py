"""Synthetic receiver lifecycle and local IPC only; no UAC, ETW or BLE start."""
import asyncio
import ctypes as C
import dataclasses
import multiprocessing
import os
import sys
import subprocess
import threading
import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import chromecast_client as client, chromecast_worker as worker
from ovb_rc003 import chromecast_pipe_windows as pipes, chromecast_device_windows as device
from ovb_rc003 import chromecast_etw_windows as etw, chromecast_channel as channel
from ovb_rc003.chromecast_buttons import ButtonEdge, empty_stop_details
from tests.test_chromecast_buttons import CAPTURE_DETAILS, acl, incomplete_notification, notification
from tests.test_app_wiring import _AppWiringTestCase
from ovb_rc003 import key_mapping, remote_selection, key_detection_bridge, bridge_runtime_status, raw_input_windows

ENTITY = "a" * 64


def _pipe_child(identity, queue):
    try:
        p = pipes.Pipe(identity, server=False)
        p.connect(time.monotonic() + 5, threading.Event())
        p.write(b"test")
        queue.put((os.getpid(), p.peer_pid()))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if p.read() == b"stop":
                break
            time.sleep(.01)
        p.close()
    except Exception as error:
        queue.put(str(error))


class NativePipeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Win32 venv redirector")
    def test_real_venv_redirector_authenticates_interpreter_not_launcher(self):
        identity = channel.SessionIdentity.create(ENTITY)
        pipe = pipes.Pipe(identity, server=True)
        child_code = (
            "import sys,time,threading; "
            "from ovb_rc003.chromecast_pipe_windows import Pipe; "
            "from ovb_rc003.chromecast_channel import SessionIdentity; "
            "p=Pipe(SessionIdentity(*sys.argv[1:]),server=False); "
            "p.connect(time.monotonic()+5,threading.Event()); "
            "p.write(b'test'); sys.stdin.readline(); p.close()"
        )
        child = subprocess.Popen([sys.executable, "-c", child_code, identity.entity, identity.generation, identity.token],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        handle = None
        try:
            launched = pipes.inspect_peer(child.pid)
            parent = pipes.inspect_peer(os.getpid())
            pipe.connect(time.monotonic() + 5, threading.Event())
            actual, handle = pipes.bind_worker_peer(pipe, launched, parent)
            self.assertEqual(actual.pid, pipe.peer_pid())
            self.assertEqual(actual.image, parent.image)
            if os.path.normcase(sys.executable) != parent.image:
                self.assertNotEqual(actual.pid, child.pid)
                self.assertEqual(pipes.process_parent_pid(actual.pid), child.pid)
            self.assertEqual(pipe.read(), b"test")
        finally:
            pipe.close()
            out, err = child.communicate("stop\n", timeout=6)
            if handle is not None:
                pipes.api()[0].CloseHandle(handle)
        self.assertEqual(child.returncode, 0, err)

    def test_redirected_peer_requires_immediate_parent_image_identity_and_birth(self):
        parent = pipes.Peer(10, 100, "same_sid", 1, "interpreter")
        launched = pipes.Peer(20, 200, "same_sid", 1, os.path.normcase(os.path.abspath(sys.executable)))
        actual = pipes.Peer(30, 300, "same_sid", 1, "interpreter")
        with mock.patch.object(pipes, "process_parent_pid", return_value=20):
            pipes.verify_worker_peer(actual, launched, parent)
            for bad in (dataclasses.replace(actual, born=199), dataclasses.replace(actual, sid="other"),
                        dataclasses.replace(actual, session=2), dataclasses.replace(actual, image="other")):
                with self.assertRaises(pipes.PipeError):
                    pipes.verify_worker_peer(bad, launched, parent)
            for bad in (dataclasses.replace(launched, image="other"), dataclasses.replace(launched, sid="other"),
                        dataclasses.replace(launched, session=2)):
                with self.assertRaises(pipes.PipeError):
                    pipes.verify_worker_peer(actual, bad, parent)
        with mock.patch.object(pipes, "process_parent_pid", return_value=999), self.assertRaises(pipes.PipeError):
            pipes.verify_worker_peer(actual, launched, parent)

    def test_direct_worker_identity_still_supported(self):
        parent = pipes.Peer(10, 100, "sid", 1, "same_image")
        actual = pipes.Peer(20, 200, "sid", 1, "same_image")
        with mock.patch.object(pipes, "process_parent_pid") as lookup:
            pipes.verify_worker_peer(actual, actual, parent)
        lookup.assert_not_called()

    def test_fixed_entry_is_dispatched_before_desktop_without_opening_resources(self):
        from ovb_rc003 import __main__ as entry
        identity = channel.SessionIdentity.create(ENTITY)
        args = ["app", "--chromecast-worker", "1", "2", identity.entity, identity.generation, identity.token]
        with mock.patch.object(sys, "argv", args), mock.patch.object(worker, "main", return_value=2) as run, mock.patch.object(entry, "_run_settings") as desktop:
            with self.assertRaises(SystemExit) as exit_result:
                entry.main()
        self.assertEqual(exit_result.exception.code, 2)
        run.assert_called_once_with(args[2:])
        desktop.assert_not_called()

    def test_identity_rejects_each_changed_field(self):
        peer = pipes.Peer(1, 2, "S-1-5-21-123", 3, "image")
        pipes.verify_peer(peer, peer)
        for field, value in (("pid", 4), ("born", 4), ("sid", "other"), ("session", 4), ("image", "other")):
            with self.subTest(field=field), self.assertRaises(pipes.PipeError):
                pipes.verify_peer(dataclasses.replace(peer, **{field: value}), peer)

    @unittest.skipUnless(sys.platform == "win32", "Win32 local pipe")
    def test_real_local_pipe_and_os_peer_without_elevation(self):
        identity = channel.SessionIdentity.create(ENTITY)
        p = pipes.Pipe(identity, server=True)
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        child = ctx.Process(target=_pipe_child, args=(identity, queue))
        try:
            child.start()
            p.connect(time.monotonic() + 5, threading.Event())
            self.assertEqual(p.peer_pid(), child.pid)
            current, other = pipes.inspect_peer(os.getpid()), pipes.inspect_peer(child.pid)
            pipes.verify_same_user_image(other, current)
            self.assertEqual(queue.get(timeout=5), (child.pid, os.getpid()))
            self.assertEqual(p.read(), b"test")
            p.write(b"stop")
            child.join(5)
            self.assertEqual(child.exitcode, 0)
        finally:
            p.close()
            child.join(6)
            queue.close()


class LayoutTests(unittest.TestCase):
    def resolve(self, paths):
        return device.input_handle(ENTITY, enumerate_paths=lambda **kw: [p for p in paths if kw["matches"](p)],
                                   resolve=lambda _: ENTITY)

    def test_only_selected_verified_identity(self):
        path = r"\\?\HID#dev_vid&0218d1_pid&9450_rev&0110#sample"
        self.assertEqual(self.resolve([path]), 0x29)
        with self.assertRaises(pipes.PipeError):
            self.resolve([path.replace("0110", "0111")])
        with self.assertRaises(pipes.PipeError):
            self.resolve([path, path.replace("0110", "0111")])
        with self.assertRaises(pipes.PipeError):
            device.input_handle(ENTITY, enumerate_paths=lambda **_: [path], resolve=lambda _: "b" * 64)
        with self.assertRaises(pipes.PipeError):
            self.resolve([])


class DeviceDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_entity_single_radio_and_hotplug_invalidation(self):
        target = device.SelectedDevice(ENTITY)
        callbacks = {}
        watcher = SimpleNamespace(stop=mock.Mock())
        for name in ("added", "removed", "stopped", "enumeration_completed"):
            setattr(watcher, "add_" + name, lambda cb, name=name: callbacks.setdefault(name, cb))
            setattr(watcher, "remove_" + name, mock.Mock())
        def start():
            callbacks["added"](None, SimpleNamespace(id="radio"))
            callbacks["enumeration_completed"](None, None)
        watcher.start = start
        opened = SimpleNamespace(device_id="chosen", close=mock.Mock())
        ble = SimpleNamespace(get_device_selector_from_pairing_state=lambda _: "paired", from_id_async=mock.AsyncMock(return_value=opened))
        infos = SimpleNamespace(create_watcher_aqs_filter=lambda _: watcher,
            find_all_async_aqs_filter_and_additional_properties=mock.AsyncMock(return_value=[SimpleNamespace(id="chosen", name="Chromecast Remote")]))
        modules = {"winrt.windows.devices.bluetooth": SimpleNamespace(BluetoothAdapter=SimpleNamespace(get_device_selector=lambda: "radio"), BluetoothLEDevice=ble),
                   "winrt.windows.devices.enumeration": SimpleNamespace(DeviceInformation=infos)}
        with mock.patch.dict(sys.modules, modules), mock.patch.object(device.remote_selection, "candidate_key", return_value=ENTITY), mock.patch.object(device, "input_handle", return_value=0x29):
            await target.open()
            self.assertFalse(target.changed.is_set())
            ble.from_id_async.assert_awaited_once_with("chosen")
            callbacks["removed"](None, None)
            self.assertTrue(target.changed.is_set())
            target.close()
        opened.close.assert_called_once()
        watcher.stop.assert_called_once()

    async def test_multiple_adapters_rejected_before_ble_discovery(self):
        target = device.SelectedDevice(ENTITY)
        target.ids.update({"radio1", "radio2"})
        target.enumerated.set()
        watcher = mock.Mock()
        infos = SimpleNamespace(create_watcher_aqs_filter=lambda _: watcher,
                                find_all_async_aqs_filter_and_additional_properties=mock.AsyncMock())
        modules = {"winrt.windows.devices.bluetooth": SimpleNamespace(BluetoothAdapter=SimpleNamespace(get_device_selector=lambda: "radio"), BluetoothLEDevice=mock.Mock()),
                   "winrt.windows.devices.enumeration": SimpleNamespace(DeviceInformation=infos)}
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(pipes.PipeError, "radio_ambiguous"):
                await target.open()
            target.close()
        infos.find_all_async_aqs_filter_and_additional_properties.assert_not_called()


class CaptureTests(unittest.TestCase):
    def test_enable_fixed_value_and_verify_then_remain_enabled(self):
        registry = mock.MagicMock(REG_DWORD=4, KEY_READ=1, KEY_SET_VALUE=2)
        values = {"EtwLogSensitiveData": (1, 4)}

        def read(key, name):
            if name not in values:
                raise FileNotFoundError
            return values[name]

        def write(key, name, reserved, kind, value):
            values[name] = (value, kind)

        registry.QueryValueEx.side_effect = read
        registry.SetValueEx.side_effect = write
        with mock.patch.dict(sys.modules, winreg=registry):
            self.assertFalse(etw.sensitive_logging_enabled())
            etw.enable_sensitive_logging()
            self.assertTrue(etw.sensitive_logging_enabled())
            etw.enable_sensitive_logging()
        registry.CreateKeyEx.assert_called_once_with(registry.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\BthPort\Parameters", 0, 2)
        self.assertEqual([(c.args[1], c.args[4]) for c in registry.SetValueEx.call_args_list],
                         [("MaxEtwBytes", 0x400), ("EtwDropLargeEvents", 0)])
        registry.DeleteValue.assert_not_called()

    def test_existing_larger_capture_limit_is_preserved(self):
        registry = mock.MagicMock(REG_DWORD=4)
        values = {"EtwLogSensitiveData": (1, 4), "MaxEtwBytes": (4096, 4), "EtwDropLargeEvents": (0, 4)}
        registry.QueryValueEx.side_effect = lambda key, name: values[name]
        with mock.patch.dict(sys.modules, winreg=registry):
            etw.enable_sensitive_logging()
            self.assertTrue(etw.sensitive_logging_enabled())
        registry.CreateKeyEx.assert_not_called()
        registry.SetValueEx.assert_not_called()

    def test_snapshot_distinguishes_missing_wrong_type_and_unreadable(self):
        registry = mock.MagicMock(REG_DWORD=4)
        registry.QueryValueEx.side_effect = [FileNotFoundError(), ("private-content", 1), PermissionError()]
        with mock.patch.dict(sys.modules, winreg=registry):
            result = etw.capture_settings_snapshot()
        self.assertEqual(result["EtwLogSensitiveData"]["state"], "missing")
        self.assertEqual(result["MaxEtwBytes"]["value"], None)
        self.assertFalse(result["MaxEtwBytes"]["dword"])
        self.assertEqual(result["EtwDropLargeEvents"]["state"], "unavailable")
        self.assertNotIn("private-content", str(result))

    def test_enable_permission_failure_and_failed_verification_are_errors(self):
        for denied in (True, False):
            with self.subTest(denied=denied):
                registry = mock.MagicMock(REG_DWORD=4)
                registry.QueryValueEx.return_value = (0, 4)
                if denied:
                    registry.SetValueEx.side_effect = PermissionError
                with mock.patch.dict(sys.modules, winreg=registry), self.assertRaises(OSError):
                    etw.enable_sensitive_logging()

    def test_partial_write_is_not_ready_and_retry_completes_only_missing_settings(self):
        registry = mock.MagicMock(REG_DWORD=4)
        values = {"EtwLogSensitiveData": (0, 4), "MaxEtwBytes": (20, 4), "EtwDropLargeEvents": (1, 4)}
        registry.QueryValueEx.side_effect = lambda key, name: values[name]

        def write(key, name, reserved, kind, value):
            if name == "MaxEtwBytes":
                raise PermissionError
            values[name] = (value, kind)

        registry.SetValueEx.side_effect = write
        with mock.patch.dict(sys.modules, winreg=registry):
            with self.assertRaises(PermissionError):
                etw.enable_sensitive_logging()
            self.assertFalse(etw.sensitive_logging_enabled())
            self.assertEqual(values["EtwLogSensitiveData"], (1, 4))
            registry.SetValueEx.reset_mock()
            registry.SetValueEx.side_effect = lambda key, name, reserved, kind, value: values.update({name: (value, kind)})
            etw.enable_sensitive_logging()
            self.assertTrue(etw.sensitive_logging_enabled())
        self.assertEqual([c.args[1] for c in registry.SetValueEx.call_args_list],
                         ["MaxEtwBytes", "EtwDropLargeEvents"])

    def test_missing_or_wrong_type_is_not_enabled(self):
        for response in (FileNotFoundError(), (1, 1), (0, 4)):
            registry = mock.MagicMock(REG_DWORD=4)
            if isinstance(response, Exception):
                registry.QueryValueEx.side_effect = response
            else:
                registry.QueryValueEx.return_value = response
            with mock.patch.dict(sys.modules, winreg=registry):
                self.assertFalse(etw.sensitive_logging_enabled())

    @unittest.skipUnless(sys.platform == "win32", "Win32 ABI")
    def test_abi_and_record_decode_bounds(self):
        self.assertEqual((C.sizeof(etw.Wnode), C.sizeof(etw.Header), C.sizeof(etw.Record), C.sizeof(etw.Logfile)), (48, 80, 112, 448))
        self.assertEqual(etw.Logfile.callback.offset, 424)
        capture = etw.Capture("b" * 64)
        record = etw.Record()
        record.header.provider[:] = etw.PROVIDER
        record.header.id = 402
        record.header.version = 7
        record.data_size = 31
        record.header.stamp = int(time.time() * 1e7) + 116444736000000000
        values = {"BIP_Type": b"\x03", "BIP_DataLen": (4).to_bytes(4, "little"), "BIP_Data": b"test"}
        capture._property = lambda _, name, maximum: values[name]
        capture._record(C.byref(record))
        self.assertEqual(capture.queue[0][:2], (3, b"test"))
        self.assertLess(abs(capture.queue[0][2] - time.monotonic()), .1)
        self.assertEqual(capture.queue[0][3], {
            "capture_metadata_available": True,
            "capture_layout": "bthport_402_tdh",
            "capture_event_id": 402,
            "capture_event_version": 7,
            "capture_user_data_length": 31,
            "capture_property_length": 4,
            "capture_extracted_length": 4,
            "capture_length_match": True,
        })
        capture.bytes = 512 * 1024
        capture._record(C.byref(record))
        self.assertEqual(capture.failure, "capture_lost")

    def test_foreign_provider_and_non_packet_events_ignored(self):
        capture = etw.Capture("b" * 64)
        capture._property = mock.Mock(side_effect=AssertionError("not a packet"))
        capture._record(C.byref(etw.Record()))
        self.assertFalse(capture.failure)
        capture._property.assert_not_called()

    def test_loss_and_failed_cleanup_never_succeed(self):
        capture = etw.Capture("b" * 64)
        capture.failure = "capture_lost"
        with self.assertRaisesRegex(pipes.PipeError, "capture_lost"):
            capture.poll()
        capture.session, capture.storage = 9, b"fake"
        capture.adv = SimpleNamespace(ControlTraceW=lambda *args: 5)
        with self.assertRaises(pipes.PipeError):
            capture.stop()
        self.assertEqual(capture.session, 9)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_enables_only_selected_device_without_capture(self):
        for selected, enabled in ((True, False), (True, True), (False, False)):
            with self.subTest(selected=selected, enabled=enabled):
                identity = channel.SessionIdentity.create(ENTITY)
                command = channel.Channel(identity, commands=True).encode("start", mode="setup")
                queue, output = deque([command]), []
                with mock.patch.object(worker, "_selected", return_value=selected), \
                     mock.patch.object(worker, "sensitive_logging_enabled", return_value=enabled), \
                     mock.patch.object(worker, "enable_sensitive_logging") as enable, \
                     mock.patch.object(worker, "SelectedDevice") as device_factory, \
                     mock.patch.object(worker, "Capture") as capture_factory:
                    result = await worker.receive(identity, None, SimpleNamespace(
                        read=lambda: queue.popleft() if queue else None, write=output.append))
                self.assertEqual(result, 0)
                self.assertEqual(enable.call_count, int(selected and not enabled))
                device_factory.assert_not_called()
                capture_factory.assert_not_called()
                incoming = channel.Channel(identity, commands=False)
                event = [incoming.decode(raw) for raw in output][-1]
                self.assertEqual(event["reason"], "configured" if selected else "source_unconfirmed")

    async def test_setup_rejects_voice_or_arbitrary_registry_arguments(self):
        import json
        for extra in ({"voice": True}, {"path": "HKLM\\other"}):
            identity = channel.SessionIdentity.create(ENTITY)
            raw = json.loads(channel.Channel(identity, commands=True).encode("start", mode="setup"))
            raw.update(extra)
            output, queue = [], deque([json.dumps(raw).encode()])
            with mock.patch.object(worker, "enable_sensitive_logging") as enable:
                await worker.receive(identity, None, SimpleNamespace(
                    read=lambda: queue.popleft() if queue else None, write=output.append))
            enable.assert_not_called()
            incoming = channel.Channel(identity, commands=False)
            self.assertNotEqual([incoming.decode(raw) for raw in output][-1]["reason"], "configured")

    async def test_proof_edges_cancel_stop_and_no_raw_ipc(self):
        identity = channel.SessionIdentity.create(ENTITY)
        commands, events = channel.Channel(identity, commands=True), channel.Channel(identity, commands=False)
        queue = deque([commands.encode("start", mode="run")])
        received, packets = [], []
        target = SimpleNamespace(radio="c" * 64, attribute=0x29, changed=threading.Event())
        async def opened():
            return None
        async def probe(marker):
            packets.extend([(4, acl(b"\x06\x01\x00\xff\xff\x00\x28" + marker.bytes[::-1]), time.monotonic()),
                            (3, acl(b"\x01\x06\x01\x00\x0a"), time.monotonic())])
            return True, 0
        target.open, target.probe, target.close = opened, probe, mock.Mock()
        def read():
            return queue.popleft() if queue else None
        def write(raw):
            event = events.decode(raw)
            received.append(event)
            if event["type"] == "ready":
                packets.append((3, acl(notification(7, 0x29)), time.monotonic()))
            if event.get("action") == "down":
                queue.append(commands.encode("stop"))
        def poll():
            result = packets[:]
            packets.clear()
            return result
        capture = SimpleNamespace(start=mock.Mock(), stop=mock.Mock(), poll=poll)
        with mock.patch.multiple(worker, _selected=lambda _: True, sensitive_logging_enabled=lambda: True,
                                 inspect_peer=lambda _: "parent", verify_peer=lambda *_: None,
                                 SelectedDevice=lambda _: target, Capture=lambda _: capture):
            result = await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1), SimpleNamespace(read=read, write=write)), 3)
        self.assertEqual(result, 0)
        self.assertEqual([e["type"] for e in received if e["type"] not in ("diagnostic", "evidence")], ["ready", "edge", "edge", "stopped"])
        journal = [e["record"] for e in received if e["type"] == "evidence"]
        self.assertEqual(journal[0]["stage"], "run")
        self.assertTrue(journal[-1]["final"])
        self.assertEqual([e["action"] for e in received if e["type"] == "edge"], ["down", "cancel"])
        capture.stop.assert_called_once()
        target.close.assert_called_once()

    async def test_post_ready_header_only_input_loss_recovers_in_same_worker(self):
        from ovb_rc003 import chromecast_hid_tap_windows

        identity = channel.SessionIdentity.create(ENTITY)
        commands = channel.Channel(identity, commands=True)
        events = channel.Channel(identity, commands=False)
        queue = deque([commands.encode("start", mode="run")])
        received, packets = [], []
        target = SimpleNamespace(
            radio="c" * 64,
            attribute=0x29,
            changed=threading.Event(),
            open=mock.AsyncMock(),
            close=mock.Mock(),
        )

        async def probe(marker):
            packets.extend([
                (
                    4,
                    acl(b"\x06\x01\x00\xff\xff\x00\x28" + marker.bytes[::-1]),
                    time.monotonic(),
                ),
                (3, acl(b"\x01\x06\x01\x00\x0a"), time.monotonic()),
            ])
            return True, 0

        target.probe = probe

        def poll():
            result, packets[:] = packets[:], []
            return result

        capture = SimpleNamespace(start=mock.Mock(), stop=mock.Mock(), poll=poll)

        class FakeTap:
            started = emitted = False

            def __init__(self, _entity):
                pass

            def start(self):
                self.started = True

            def source_alive(self):
                return True

            def poll(self):
                if not self.started or self.emitted:
                    return []
                self.emitted = True
                now = time.monotonic()
                return [("hook_ready", "", now), ("report", "0000000000000000", now),
                        ("report", "0700000000000000", now),
                        ("report", "0000000000000000", now)]

            def close(self):
                pass

        def write(raw):
            event = events.decode(raw)
            received.append(event)
            if event["type"] == "ready":
                packets.extend([
                    (
                        3,
                        incomplete_notification(0x29),
                        time.monotonic(),
                        CAPTURE_DETAILS,
                    ),
                    (3, acl(notification(0, 0x29)), time.monotonic()),
                    (3, acl(notification(7, 0x29)), time.monotonic()),
                ])
            if event.get("action") == "up":
                queue.append(commands.encode("stop"))

        pipe = SimpleNamespace(
            read=lambda: queue.popleft() if queue else None,
            write=write,
        )
        with mock.patch.object(chromecast_hid_tap_windows, "HidTap", FakeTap), mock.patch.multiple(
            worker,
            _selected=lambda _: True,
            sensitive_logging_enabled=lambda: True,
            inspect_peer=lambda _: "parent",
            verify_peer=lambda *_: None,
            SelectedDevice=lambda _: target,
            Capture=lambda _: capture,
        ):
            result = await asyncio.wait_for(
                worker.receive(identity, SimpleNamespace(pid=1), pipe),
                3,
            )

        self.assertEqual(result, 0)
        recovered = [
            event
            for event in received
            if event["type"] == "diagnostic"
            and event["stage"] == "receive"
            and event["phase"] == "done"
            and event["reason"] == "input_payload_unavailable"
        ]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["acl_handle"], 0x31)
        self.assertEqual(recovered[0]["att_attribute"], 0x29)
        self.assertEqual(recovered[0]["att_missing_length"], 8)
        self.assertTrue(any(event["type"] == "ready" for event in received))
        self.assertEqual([event["action"] for event in received if event["type"] == "edge"], ["down", "up"])
        self.assertFalse(
            any(
                event["type"] == "diagnostic"
                and event["stage"] == "receive"
                and event["phase"] == "failed"
                for event in received
            ), received
        )

    async def test_logging_enable_failure_never_opens_hardware(self):
        identity = channel.SessionIdentity.create(ENTITY)
        outgoing = channel.Channel(identity, commands=True)
        queue = deque([outgoing.encode("start", mode="detect")])
        output = []
        with mock.patch.multiple(worker, _selected=lambda _: True, sensitive_logging_enabled=lambda: False), mock.patch.object(worker, "enable_sensitive_logging", side_effect=OSError), mock.patch.object(worker, "SelectedDevice") as factory:
            result = await worker.receive(identity, None, SimpleNamespace(read=lambda: queue.popleft() if queue else None, write=output.append))
        self.assertEqual(result, 0)
        factory.assert_not_called()
        incoming = channel.Channel(identity, commands=False)
        decoded = [incoming.decode(raw) for raw in output]
        self.assertEqual(next(e for e in decoded if e["type"] == "diagnostic")["stage"], "logging")
        self.assertEqual(decoded[-1]["reason"], "sensitive_logging_enable_failed")

    async def test_first_receive_failure_and_cleanup_failures_are_reported_independently(self):
        from ovb_rc003 import chromecast_voice_receiver as adapter
        for failure in ("unknown_report", "cleanup_report", "voice_close", "capture_stop"):
            with self.subTest(failure=failure):
                identity = channel.SessionIdentity.create(ENTITY)
                commands, events = channel.Channel(identity, commands=True), channel.Channel(identity, commands=False)
                inbound = deque([commands.encode("start", mode="run", voice=True)])
                packets, received = [], []
                target = SimpleNamespace(radio="c" * 64, attribute=0x29, changed=threading.Event(),
                                         open=mock.AsyncMock(), close=mock.Mock())
                async def probe(marker):
                    packets.extend([(4, acl(b"\x06\x01\x00\xff\xff\x00\x28" + marker.bytes[::-1]), time.monotonic()),
                                    (3, acl(b"\x01\x06\x01\x00\x0a"), time.monotonic())])
                    return True, 0
                target.probe = probe
                def poll():
                    result, packets[:] = packets[:], []
                    return result
                capture = SimpleNamespace(start=mock.Mock(), poll=poll,
                    stop=mock.Mock(side_effect=OSError if failure == "capture_stop" else None))
                async def voice_open():
                    if failure == "unknown_report":
                        packets.append((3, acl(notification(9, 0x29)), time.monotonic()))
                    else:
                        inbound.append(commands.encode("stop"))
                def voice_stop():
                    if failure == "cleanup_report":
                        packets.append((3, acl(notification(9, 0x29)), time.monotonic()))
                voice = SimpleNamespace(open=voice_open, tick=mock.AsyncMock(), stop=mock.Mock(side_effect=voice_stop),
                    close=mock.AsyncMock(side_effect=OSError if failure == "voice_close" else None),
                    hardware_stopped=True, notification=mock.Mock())
                pipe = SimpleNamespace(read=lambda: inbound.popleft() if inbound else None,
                                       write=lambda raw: received.append(events.decode(raw)))
                with mock.patch.multiple(worker, _selected=lambda _: True, sensitive_logging_enabled=lambda: True,
                        inspect_peer=lambda _: "parent", verify_peer=lambda *_: None,
                        SelectedDevice=lambda _: target, Capture=lambda _: capture), \
                     mock.patch.object(adapter, "VoiceReceiver", return_value=voice):
                    result = await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1), pipe), 3)
                self.assertEqual(result, 2)
                capture.stop.assert_called_once()
                target.close.assert_called_once()
                voice.close.assert_awaited_once()
                failures = [e for e in received if e["type"] == "diagnostic" and e["phase"] == "failed"]
                if failure in ("unknown_report", "cleanup_report"):
                    self.assertEqual(failures[0]["reason"], "unknown_key_report")
                    self.assertEqual((failures[0]["attribute"], failures[0]["length"], failures[0]["report_code"]), (0x29, 8, 9))
                    self.assertEqual(failures[1]["reason"], "cleanup_unconfirmed")
                    voice.tick.assert_not_awaited()  # no hardware write after source was lost
                else:
                    self.assertEqual(failures[0]["stage"], failure)
                self.assertFalse(any(e["type"] in ("stopped", "error") for e in received))


class ClientTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(client.diagnostics, "request")
        self.evidence_request = patcher.start()
        self.addCleanup(patcher.stop)

    def test_setup_waits_for_confirmed_terminal_and_child_exit(self):
        for reason, cleaned, succeeded in (("configured", True, True), ("sensitive_logging_enable_failed", True, True),
                                           ("configured", False, False), ("configured", True, False)):
            with self.subTest(reason=reason, cleaned=cleaned, succeeded=succeeded):
                receiver = client.Client(ENTITY, mode="setup", on_edge=mock.Mock())
                def run():
                    receiver.reason, receiver.cleanup_confirmed = reason, cleaned
                    receiver.exit_succeeded = succeeded
                    receiver.finished.set()
                with mock.patch.object(receiver, "_run", side_effect=run):
                    if reason == "configured" and cleaned and succeeded:
                        receiver.start()
                    else:
                        with self.assertRaises(RuntimeError):
                            receiver.start()
                receiver.thread.join(1)
                receiver.on_edge.assert_not_called()

    def test_setup_cancel_before_launch_has_no_worker(self):
        receiver = client.Client(ENTITY, mode="setup", on_edge=mock.Mock())
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(client, "launch_worker") as launch, self.assertRaises(RuntimeError):
            receiver.start(cancel_event=cancel)
        launch.assert_not_called()
        self.assertIsNone(receiver.thread)

    def test_identity_failure_is_specific_and_never_sends_start(self):
        receiver = client.Client(ENTITY, mode="run", on_edge=mock.Mock())
        peer = pipes.Peer(1, 2, "sid", 3, "image")
        pipe = mock.Mock()
        kernel = SimpleNamespace(GetProcessId=lambda _: 1, WaitForSingleObject=lambda *_: 0,
                                 GetExitCodeProcess=lambda *_: True, CloseHandle=mock.Mock())
        with mock.patch.multiple(client, inspect_peer=lambda _: peer, Pipe=lambda *_args, **_kwargs: pipe,
                                 launch_worker=lambda *_: 42, api=lambda: (kernel, None)), \
             mock.patch.object(client, "bind_worker_peer", side_effect=pipes.PipeError("peer_launch_mismatch")):
            receiver._run()
        self.assertEqual(receiver.reason, "peer_identity_failed")
        self.assertEqual(receiver.failure_stage, "peer_identity")
        self.assertEqual(receiver.failure_code, "peer_launch_mismatch")
        pipe.write.assert_not_called()
        self.assertIn("身份核对失败", client.message(receiver.reason))

    def test_header_only_input_failure_does_not_recommend_service_restart(self):
        text = client.message("input_payload_unavailable")
        self.assertIn("只提供了谷歌遥控器的按键包头", text)
        self.assertIn("请导出日志", text)
        self.assertNotIn("重启电脑", text)
        self.assertNotIn("重新启动服务", text)

    @unittest.skipUnless(sys.platform == "win32", "Win32 venv process lifecycle")
    def test_real_client_start_stop_with_redirected_process_without_capture(self):
        children = []
        def launch(identity, parent):
            code = '''
import sys,time,threading
from ovb_rc003.chromecast_pipe_windows import Pipe
from ovb_rc003.chromecast_channel import SessionIdentity,Channel
identity=SessionIdentity(*sys.argv[1:4])
p=Pipe(identity,server=False)
p.connect(time.monotonic()+5,threading.Event())
assert p.peer_pid()==int(sys.argv[4])
incoming,outgoing=Channel(identity,commands=True),Channel(identity,commands=False)
deadline=time.monotonic()+5
while time.monotonic()<deadline:
    raw=p.read()
    if raw:
        msg=incoming.decode(raw)
        if msg['type']=='start':
            p.write(outgoing.encode('ready'))
        elif msg['type']=='stop':
            p.write(outgoing.encode('stopped',reason='stopped'))
            break
    time.sleep(.01)
else:
    raise RuntimeError('test_timeout')
p.close()
'''
            process = subprocess.Popen([sys.executable, "-c", code, identity.entity, identity.generation,
                                        identity.token, str(parent.pid)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            children.append(process)
            return pipes.api()[0].OpenProcess(0x1000 | 0x100000, False, process.pid)
        receiver = client.Client(ENTITY, mode="detect", on_edge=mock.Mock())
        try:
            with mock.patch.object(client, "launch_worker", side_effect=launch):
                receiver.start()
                self.assertTrue(receiver.ready.is_set())
                receiver.stop()
            self.assertTrue(receiver.cleanup_confirmed)
            self.assertFalse(receiver.is_running)
            receiver.on_edge.assert_not_called()
        finally:
            receiver.stop()
            for process in children:
                out, err = process.communicate(timeout=6)
                self.assertEqual(process.returncode, 0, err)

    def test_roundtrip_and_cancel_exactly_once(self):
        edges = []
        receiver = client.Client(ENTITY, mode="run", on_edge=edges.append)
        outgoing = channel.Channel(receiver.identity, commands=False)
        inputs = deque([outgoing.encode("ready"), outgoing.encode("diagnostic", stage="receive", phase="failed",
                        reason="unknown_key_report", **(empty_stop_details() | dict(
                            event_kind=3, attribute=0x29, length=8, report_code=9, tail_nonzero=False,
                            l2cap_header_parsed=True, l2cap_declared_length=11, l2cap_cid=4,
                            att_header_parsed=True, att_opcode=0x1B, att_attribute=0x29,
                            att_missing_length=8))),
                        outgoing.encode("edge", button="ok", action="down", time=time.monotonic()),
                        outgoing.encode("error", reason="capture_lost")])
        peer = pipes.Peer(1, 2, "sid", 3, "image")
        pipe = SimpleNamespace(connect=mock.Mock(), peer_pid=lambda: 1, read=lambda: inputs.popleft() if inputs else None,
                               write=mock.Mock(), close=mock.Mock())
        def exit_code(handle, pointer):
            C.cast(pointer, C.POINTER(C.c_ulong)).contents.value = 0
            return True
        kernel = SimpleNamespace(GetProcessId=lambda _: 1, WaitForSingleObject=lambda *_: 0,
                                 GetExitCodeProcess=exit_code, CloseHandle=mock.Mock())
        with mock.patch.multiple(client, inspect_peer=lambda _: peer, Pipe=lambda *_args, **_kwargs: pipe,
                                 launch_worker=lambda *_: 42, api=lambda: (kernel, None),
                                 bind_worker_peer=lambda *_: (peer, None)), \
             self.assertLogs("ovb_rc003", level="INFO") as logs:
            receiver._run()
        self.assertEqual([(e.button, e.action) for e in edges], [("ok", "down"), ("ok", "cancel")])
        self.assertIn("l2cap_declared_length=11", " ".join(logs.output))
        self.assertIn("att_missing_length=8", " ".join(logs.output))
        self.assertTrue(receiver.cleanup_confirmed)
        self.assertTrue(receiver.finished.is_set())

    def test_cancel_before_launch_and_no_late_delivery(self):
        callback = mock.Mock()
        receiver = client.Client(ENTITY, mode="detect", on_edge=callback)
        receiver.cancel.set()
        receiver._deliver(ButtonEdge(ENTITY, receiver.identity.generation, 1, "ok", "down"))
        callback.assert_not_called()
        with mock.patch.object(client, "inspect_peer"), mock.patch.object(client, "Pipe"), mock.patch.object(client, "launch_worker") as launch:
            receiver._run()
        launch.assert_not_called()
        self.assertTrue(receiver.cleanup_confirmed)

    def test_stop_retains_uncertain_owner(self):
        receiver = client.Client(ENTITY, mode="run", on_edge=lambda _: None)
        receiver.thread = SimpleNamespace(join=lambda _: None, is_alive=lambda: False)
        with self.assertRaisesRegex(RuntimeError, "尚未确认退出"):
            receiver.stop()
        self.assertIsNotNone(receiver.thread)

    def test_late_permission_after_cancel_never_starts_capture(self):
        receiver = client.Client(ENTITY, mode="run", on_edge=mock.Mock())
        peer = pipes.Peer(1, 2, "sid", 3, "image")
        pipe = mock.Mock()
        pipe.peer_pid.return_value = 1
        def launch(*_):
            receiver.cancel.set()
            return 42
        def exit_code(handle, pointer):
            C.cast(pointer, C.POINTER(C.c_ulong)).contents.value = 2
            return True
        kernel = SimpleNamespace(GetProcessId=lambda _: 1, WaitForSingleObject=lambda *_: 0,
                                 GetExitCodeProcess=exit_code, CloseHandle=mock.Mock())
        with mock.patch.multiple(client, inspect_peer=lambda _: peer, Pipe=lambda *_args, **_kwargs: pipe,
                                 launch_worker=launch, api=lambda: (kernel, None),
                                 bind_worker_peer=lambda *_: (peer, None)):
            receiver._run()
        pipe.write.assert_not_called()
        self.assertTrue(receiver.cleanup_confirmed)

    def test_late_child_exit_can_be_confirmed_on_retry(self):
        receiver = client.Client(ENTITY, mode="run", on_edge=mock.Mock())
        receiver._process = 42
        receiver._started = True
        def exit_code(handle, pointer):
            C.cast(pointer, C.POINTER(C.c_ulong)).contents.value = 0
            return True
        receiver._kernel = SimpleNamespace(WaitForSingleObject=mock.Mock(side_effect=[258, 0]),
                                           GetExitCodeProcess=exit_code, CloseHandle=mock.Mock())
        receiver._confirm_exit()
        self.assertEqual(receiver._process, 42)
        receiver._confirm_exit()
        self.assertTrue(receiver.cleanup_confirmed)
        self.assertIsNone(receiver._process)

    def test_launcher_exit_does_not_hide_a_running_interpreter(self):
        receiver = client.Client(ENTITY, mode="run", on_edge=mock.Mock())
        receiver._process, receiver._worker_process = 42, 43
        receiver._kernel = SimpleNamespace(WaitForSingleObject=lambda handle, _: 0 if handle == 42 else 258,
                                          GetExitCodeProcess=mock.Mock(), CloseHandle=mock.Mock())
        receiver._confirm_exit()
        self.assertFalse(receiver.cleanup_confirmed)
        self.assertEqual(receiver._worker_process, 43)
        receiver._kernel.CloseHandle.assert_not_called()

    def test_exited_failure_does_not_permanently_block_stop_or_switch(self):
        for read_ok in (True, False):
            with self.subTest(exit_code_read=read_ok):
                receiver = client.Client(ENTITY, mode="run", on_edge=mock.Mock())
                receiver._process, receiver._worker_process, receiver._started = 42, 43, True
                receiver.thread = SimpleNamespace(join=lambda _: None, is_alive=lambda: False)
                def exit_code(handle, pointer):
                    C.cast(pointer, C.POINTER(C.c_ulong)).contents.value = 2
                    return read_ok
                receiver._kernel = SimpleNamespace(WaitForSingleObject=lambda *_: 0,
                    GetExitCodeProcess=exit_code, CloseHandle=mock.Mock())
                with self.assertLogs("ovb_rc003", level="INFO") as logs:
                    receiver.stop()
                    receiver.stop()
                self.assertTrue(receiver.cleanup_confirmed)
                self.assertFalse(receiver.exit_succeeded)
                self.assertIsNone(receiver._process)
                self.assertIsNone(receiver._worker_process)
                self.assertEqual(receiver._kernel.CloseHandle.call_count, 2)
                self.assertIn("exit_code_read=", " ".join(logs.output))


class ProductRoutingTests(_AppWiringTestCase):
    def setUp(self):
        super().setUp()
        self.app._remote_profile = remote_selection.CHROMECAST_PROFILE
        self.app._chromecast_runtime.client = SimpleNamespace(identity=channel.SessionIdentity.create(ENTITY))
        self.app._direct_hid_interception_ready = False
        self.app._reload_settings_if_changed = mock.Mock()

    def edge(self, action, button="ok", entity=ENTITY):
        self.app._chromecast_runtime.on_edge(ButtonEdge(entity, self.app._chromecast_runtime.client.identity.generation,
                                              time.monotonic(), button, action))

    def test_mapping_uses_shared_executor_and_cancel_is_not_release(self):
        with mock.patch.object(self.app, "_apply_button_action") as execute:
            self.edge("down")
            self.edge("up")
            self.assertEqual(execute.call_args[0][0].kind, key_mapping.ActionKind.RETURN)
            execute.reset_mock()
            self.edge("down")
            executed_before_cancel = execute.call_count
            self.edge("cancel")
            self.assertFalse(self.app._button_gestures.has_active_gestures())
            self.assertEqual(execute.call_count, executed_before_cancel)

    def test_other_entity_and_generation_do_not_execute(self):
        with mock.patch.object(self.app, "_on_button_event") as route:
            self.edge("down", entity="b" * 64)
            self.app._chromecast_runtime.on_edge(ButtonEdge(ENTITY, "c" * 64, 1, "ok", "down"))
        route.assert_not_called()

    def test_running_detection_does_not_execute_mapping(self):
        request = key_detection_bridge.request_detection(self.app._config_root)
        with mock.patch.object(self.app, "_apply_button_action") as execute:
            self.edge("down")
            self.edge("up")
        execute.assert_not_called()
        self.assertEqual(key_detection_bridge.poll_detection(request), "ok")

    def test_chromecast_voice_mapping_cannot_open_xiaomi_voice(self):
        with mock.patch.object(self.app, "_primary_button_action", return_value=key_mapping.ButtonAction(key_mapping.ActionKind.VOICE_HOLD)), mock.patch.object(self.app, "_handle_mic_button_pressed") as mic:
            self.edge("down")
            self.edge("up")
        mic.assert_not_called()
        self.assertFalse(self.app._voice_shortcut.controller.active)

    def test_chromecast_runtime_ready_does_not_require_xiaomi_tap(self):
        status = bridge_runtime_status.BridgeRuntimeStatus(3, bridge_runtime_status.BridgeConnectionState.CONNECTED,
            os.getpid(), time.time(), raw_input_state="chromecast_ready", hid_tap_state="not_used")
        self.assertTrue(bridge_runtime_status.input_channels_ready(status))

    def test_chromecast_lifecycle_stops_host_and_receiver_before_trackers(self):
        target = SimpleNamespace(start=mock.Mock(), stop=mock.Mock(), finished=threading.Event(), reason="stopped")
        self.app._chromecast_runtime.stopping.set()
        order = []
        target.start.side_effect = lambda **kw: order.append("receiver_start")
        target.stop.side_effect = lambda: order.append("receiver_stop")
        with mock.patch.object(client, "Client", return_value=target), \
             mock.patch.object(self.app, "_start_input_channels", side_effect=lambda: order.append("tracker_start")), \
             mock.patch.object(self.app, "_stop_input_channels", side_effect=lambda: order.append("tracker_stop")):
            self._loop.run_until_complete(self.app.run_forever())
        self.assertEqual(order, ["tracker_start", "receiver_start", "receiver_stop", "tracker_stop"])
        target.stop.assert_called_once()
        self.assertIsNone(self.app._chromecast_runtime.client)

    def test_keyboard_safety_starts_without_xiaomi_discovery_or_decoding(self):
        listener = mock.Mock(is_running=False)
        listener.start.side_effect = lambda path: setattr(listener, "is_running", True)
        listener.stop.side_effect = lambda: setattr(listener, "is_running", False)
        with mock.patch.object(raw_input_windows, "RawInputButtonListener", return_value=listener), \
             mock.patch.object(raw_input_windows, "enumerate_matching_device_paths") as discover, \
             mock.patch.object(self.app, "_start_voice_key_physicalizer") as hook, \
             mock.patch.object(self.app, "_start_hid_report_tap") as tap:
            self.app._start_input_channels()
            listener.start.assert_called_once_with(None)
            discover.assert_not_called()
            hook.assert_not_called()
            tap.assert_not_called()
            listener.set_raw_event_callback.assert_not_called()
            listener.set_sourced_button_event_callback.assert_not_called()
            listener.set_physical_keyboard_tracking_lost_callback.assert_called_once()
            self.app._stop_input_channels()
        listener.stop.assert_called_once()

    def test_keyboard_loss_cancels_chromecast_not_xiaomi_ble(self):
        self.app._chromecast_runtime.voice_host = mock.Mock(closed=False)
        self.app._runtime_raw_input_state = "chromecast_ready"
        with mock.patch.object(self.app, "_schedule_raw_input_recovery_locked"), \
             mock.patch.object(self.app, "_force_voice_hold_release_locked") as xiaomi_release:
            self.app._on_physical_keyboard_tracking_lost("test")
        self.app._chromecast_runtime.voice_host.cancel_recording.assert_called_once()
        xiaomi_release.assert_not_called()
        self.assertEqual(self.app._runtime_raw_input_state, "chromecast_ready")

    def test_product_start_reaches_default_wetype_sender_with_keyboard_safety(self):
        from ovb_rc003 import wetype_control_windows, win32_input
        from ovb_rc003.chromecast_voice_host import VoiceHost
        self.app._config["voice_program"] = {"provider": "wetype"}
        raw_input_windows._set_physical_keyboard_tracker_active(False)
        activate = mock.Mock(return_value=True)
        control = wetype_control_windows.WeTypeVoiceControl(
            activate_profile=activate, run_sta=lambda fn: fn(), mic_start_reader=None,
            revive_profile=None, press_keys=self.app._press_wetype_voice_keys,
            release_keys=self.app._release_wetype_voice_keys)
        self.app._voice_shortcut.wetype_control = control
        self.app._voice_shortcut.hotkey = SimpleNamespace(modifiers=("lctrl",), key="lwin")
        listener = mock.Mock(is_running=False)
        def start(_path):
            listener.is_running = True
            raw_input_windows._set_physical_keyboard_tracker_active(True)
        def stop():
            listener.is_running = False
            raw_input_windows._set_physical_keyboard_tracker_active(False)
        listener.start.side_effect, listener.stop.side_effect = start, stop
        host = self.app._create_chromecast_voice_host()
        self.app._chromecast_runtime.voice_host = host
        for patcher in (
            mock.patch("ovb_rc003.chromecast_host_activity.read_wetype_capture", return_value=()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        host.client = mock.Mock()
        with mock.patch.object(raw_input_windows, "RawInputButtonListener", return_value=listener), \
             mock.patch.object(raw_input_windows, "_real_async_key_is_down", return_value=False), \
             mock.patch.object(win32_input, "_real_send_virtual_key_input_batch", return_value=1) as native, \
             mock.patch.object(self.app, "_configured_voice_hotkey_backend", return_value="wetype_hotkey"), \
             mock.patch.object(self.app, "_voice_mode_for_primary_button", return_value="hold"), \
             mock.patch.object(self.app._voice_audio, "open", return_value=True), \
             mock.patch.object(self.app, "_ensure_voice_diagnostic_attempt"), \
             mock.patch.object(self.app._voice_audio, "flush", return_value=SimpleNamespace(completed=True, error=None)), \
             mock.patch.object(key_detection_bridge, "publish_next_button", return_value=False):
            host._start_host(1)
            activate.assert_not_called()
            native.assert_not_called()
            host._stop_host()
            host._handle({"event": "state", "attempt": 1, "data": "idle"})
            self.app._start_input_channels()
            host._start_host(2)
            self.assertTrue(host.engaged)
            self.assertFalse(host.confirmed)
            activate.assert_called_once()
            self.assertEqual(native.call_args_list, [mock.call([(0xa2, False)]), mock.call([(0x5b, False)])])
            self.assertTrue(host._stop_host())
            self.assertEqual(native.call_args_list[-2:], [mock.call([(0x5b, True)]), mock.call([(0xa2, True)])])
            self.app._stop_input_channels()
        self.assertFalse(raw_input_windows.physical_keyboard_tracking_available())

    def test_tracker_recovery_waits_for_host_release_and_stale_retry_cannot_restart(self):
        host = self.app._chromecast_runtime.voice_host = mock.Mock(closed=False)
        listener = self.app._hid_listener = mock.Mock(is_running=True)
        token = self.app._raw_input_retry_token = object()
        with mock.patch.object(self.app, "_schedule_raw_input_recovery_locked") as retry, \
             mock.patch.object(self.app, "_start_hid_listener_owned") as start:
            self.app._recover_raw_input_listener(token)
            host.cancel_recording.assert_called_once()
            retry.assert_called_once()
            listener.stop.assert_not_called()
            token = self.app._raw_input_retry_token = object()
            host.closed = True
            self.app._recover_raw_input_listener(token)
            listener.stop.assert_called_once()
            start.assert_called_once_with(recovering=True)
            self.app._raw_input_stopping = True
            self.app._recover_raw_input_listener(token)
            self.assertEqual(start.call_count, 1)

    def test_tracker_recovery_retains_ordinary_key_ownership_on_release_failure(self):
        self.app._chromecast_runtime.voice_host = mock.Mock(closed=True)
        listener = self.app._hid_listener = mock.Mock(is_running=True)
        token = self.app._raw_input_retry_token = object()
        with mock.patch.object(self.app, "_release_pending_button_keys", return_value=False), \
             mock.patch.object(self.app, "_schedule_raw_input_recovery_locked") as retry:
            self.app._recover_raw_input_listener(token)
        listener.stop.assert_not_called()
        retry.assert_called_once()


class DetectionUiTests(unittest.TestCase):
    from tests.test_remote_selection import SelectionControllerTests as _Fixture
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown
    _make_controller = _Fixture._make_controller
    seed_chromecast = _Fixture.seed_chromecast

    def test_detection_block_reason_is_visible_and_logged_without_starting(self):
        from ovb_rc003 import qt_settings_app as qt
        controller, _ = self._make_controller()
        logger = mock.Mock()
        with mock.patch.object(qt.logging_setup, "get_logger", return_value=logger), \
             mock.patch.object(controller, "_get_input_capture_in_use", return_value=True), \
             mock.patch.object(controller, "_begin_input_operation_start") as start:
            controller.startKeyDetection()
        start.assert_not_called()
        self.assertIn("另一项按键操作尚未结束", controller.errorMessage)
        self.assertEqual(controller.errorMessage, controller.keyDetectionText)
        logger.info.assert_called_once_with("key detection start blocked: %s", "input_capture_busy")

    def test_dead_service_channel_has_page_feedback_and_specific_log(self):
        from tests.test_remote_selection import B
        from ovb_rc003 import qt_settings_app as qt
        controller = self.seed_chromecast()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False):
            controller.useRemoteDevice(B)
        logger = mock.Mock()
        with mock.patch.object(qt.logging_setup, "get_logger", return_value=logger), \
             mock.patch.object(controller, "_get_input_capture_in_use", return_value=False), \
             mock.patch.object(controller, "_get_bridge_launch_busy", return_value=False), \
             mock.patch.object(controller, "_refresh_bridge_status", return_value=True), \
             mock.patch.object(bridge_runtime_status, "read_status", return_value=object()), \
             mock.patch.object(bridge_runtime_status, "input_channels_ready", return_value=False), \
             mock.patch.object(controller, "_begin_input_operation_start") as start:
            controller.startKeyDetection()
        start.assert_not_called()
        self.assertIn("没有可用的按键通道", controller.errorMessage)
        logger.info.assert_called_once_with("key detection start blocked: %s", "input_channel_unavailable")

    def test_standalone_detection_uses_chromecast_not_xiaomi(self):
        from tests.test_remote_selection import B
        from ovb_rc003 import qt_settings_app as qt
        controller = self.seed_chromecast()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False):
            controller.useRemoteDevice(B)
        listener = SimpleNamespace(start=mock.Mock(), stop=mock.Mock())
        with mock.patch.object(client, "Client", return_value=listener) as factory, mock.patch.object(qt.raw_input_windows, "RawInputButtonListener") as raw, mock.patch.object(qt.frida_compat, "RC003HidReportTap") as tap:
            result = controller._start_key_detection_worker(threading.Event(), bridge_running=False, physical_bindings={})
        self.assertTrue(result.ok)
        self.assertIs(result.listener, listener)
        self.assertEqual(factory.call_args.kwargs["mode"], "detect")
        self.assertEqual(factory.call_args.args[0], B)
        raw.assert_not_called()
        tap.assert_not_called()
        controller._stop_input_resources("key_detection", listener=listener)
        listener.stop.assert_called_once()

    def test_visible_failure_does_not_claim_xiaomi_or_voice_ready(self):
        from tests.test_remote_selection import B
        controller = self.seed_chromecast()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False):
            controller.useRemoteDevice(B)
        status = bridge_runtime_status.BridgeRuntimeStatus(3, bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE,
            os.getpid(), time.time(), raw_input_state="chromecast_sensitive_logging_disabled", hid_tap_state="not_used")
        text = controller._describe_runtime_status(status)
        self.assertIn("重新使用谷歌遥控器", text)
        self.assertIn("语音请先用微信输入法验证", text)
        self.assertNotIn("小米", text)
        status = dataclasses.replace(status, voice_runtime_state="host_start_failed")
        self.assertIn("语音启动失败", controller._describe_runtime_status(status))


if __name__ == "__main__":
    unittest.main()
