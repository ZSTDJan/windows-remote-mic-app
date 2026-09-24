"""Google's active receiver must start without an ETW packet proof."""
import asyncio
from collections import deque
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock
import uuid

from ovb_rc003 import chromecast_hid_worker as worker
from ovb_rc003 import chromecast_device_windows as device
from ovb_rc003.chromecast_buttons import HidButtonReceiver
from ovb_rc003.chromecast_channel import Channel, SessionIdentity
from ovb_rc003.chromecast_pipe_windows import PipeError
from ovb_rc003.chromecast_voice_receiver import VoiceReceiver
from ovb_rc003.chromecast_voice import GET_CAPABILITIES


ENTITY = "a" * 64


class HidButtonTests(unittest.TestCase):
    def test_selected_reports_have_one_press_one_release_and_cancel_on_stop(self):
        receiver = HidButtonReceiver(ENTITY, "b" * 64)
        with self.assertRaisesRegex(ValueError, "hid_source_unconfirmed"):
            receiver.feed(bytes.fromhex("0300000000000000"), 1)
        receiver.confirm_source()
        self.assertEqual([edge.action for edge in receiver.feed(bytes.fromhex("0300000000000000"), 1)], ["down"])
        self.assertEqual(receiver.feed(bytes.fromhex("0300000000000000"), 2), [])
        self.assertEqual([edge.action for edge in receiver.feed(bytes(8), 3)], ["up"])
        self.assertEqual([edge.action for edge in receiver.feed(bytes.fromhex("0400000000000000"), 4)], ["down"])
        self.assertEqual([edge.action for edge in receiver.stop(5)], ["cancel"])

    def test_invalid_report_stops_instead_of_reusing_previous_button(self):
        receiver = HidButtonReceiver(ENTITY, "b" * 64)
        receiver.confirm_source()
        receiver.feed(bytes.fromhex("0300000000000000"), 1)
        self.assertEqual([edge.action for edge in receiver.feed(b"\x03\x01" + bytes(6), 2)], ["cancel"])
        self.assertTrue(receiver.stopped)
        self.assertEqual(receiver.reason, "unknown_key_report")


class HidWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_hid_starts_without_etw_and_delivers_edges(self):
        identity = SessionIdentity.create(ENTITY)
        commands, events = Channel(identity, commands=True), Channel(identity, commands=False)
        inbox = deque([commands.encode("start", mode="run")])
        received = []
        reports = deque([("hook_ready", "", time.monotonic())])
        target = SimpleNamespace(changed=threading.Event(),
                                 open=mock.AsyncMock(), close_voice=mock.AsyncMock(), close=mock.Mock())

        class Tap:
            def start(self):
                pass

            def source_alive(self):
                return True

            def poll(self):
                result = list(reports)
                reports.clear()
                return result

            def close(self):
                pass

        def read():
            return inbox.popleft() if inbox else None

        def write(raw):
            event = events.decode(raw)
            received.append(event)
            if event["type"] == "ready":
                reports.extend((("report", "0300000000000000", time.monotonic()),
                                ("report", "0000000000000000", time.monotonic())))
            if event.get("action") == "up":
                inbox.append(commands.encode("stop"))

        with mock.patch.object(worker, "_selected", return_value=True), \
             mock.patch.object(worker, "SelectedDevice", return_value=target), \
             mock.patch.object(worker, "HidTap", return_value=Tap()), \
             mock.patch.object(worker, "verify_peer"), mock.patch.object(worker, "inspect_peer"):
            result = await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1),
                                                            SimpleNamespace(read=read, write=write)), 3)
        self.assertEqual(result, 0)
        target.open.assert_awaited_once_with(require_input=False)
        self.assertEqual([event["type"] for event in received if event["type"] not in ("evidence", "diagnostic")],
                         ["ready", "edge", "edge", "stopped"])
        self.assertEqual([event["action"] for event in received if event["type"] == "edge"], ["down", "up"])
        target.close_voice.assert_awaited_once()

    async def test_device_closes_even_if_voice_unsubscribe_fails(self):
        identity = SessionIdentity.create(ENTITY)
        commands, events = Channel(identity, commands=True), Channel(identity, commands=False)
        inbox = deque([commands.encode("start", mode="run")])
        reports = deque([("hook_ready", "", time.monotonic())])
        target = SimpleNamespace(changed=threading.Event(), open=mock.AsyncMock(),
                                 close_voice=mock.AsyncMock(side_effect=PipeError("cleanup_failed")),
                                 close=mock.Mock())

        class Tap:
            def start(self):
                pass

            def source_alive(self):
                return True

            def poll(self):
                result = list(reports)
                reports.clear()
                return result

            def close(self):
                pass

        def write(raw):
            if events.decode(raw)["type"] == "ready":
                inbox.append(commands.encode("stop"))

        with mock.patch.object(worker, "_selected", return_value=True), \
             mock.patch.object(worker, "SelectedDevice", return_value=target), \
             mock.patch.object(worker, "HidTap", return_value=Tap()), \
             mock.patch.object(worker, "verify_peer"), mock.patch.object(worker, "inspect_peer"):
            result = await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1),
                SimpleNamespace(read=lambda: inbox.popleft() if inbox else None, write=write)), 3)
        self.assertEqual(result, 2)
        target.close.assert_called_once()

    async def test_voice_starts_from_direct_gatt_without_etw_probe(self):
        identity = SessionIdentity.create(ENTITY)
        commands, events = Channel(identity, commands=True), Channel(identity, commands=False)
        inbox = deque([commands.encode("start", mode="run", voice=True)])
        received = []
        reports = deque([("hook_ready", "", time.monotonic())])
        notifications = deque()

        class Tap:
            def start(self):
                pass

            def source_alive(self):
                return True

            def poll(self):
                result = list(reports)
                reports.clear()
                return result

            def close(self):
                pass

        async def write_voice(command):
            if command == GET_CAPABILITIES:
                notifications.append((0x3F, bytes((11, 1, 0, 2, 3, 0, 160, 0, 0)), time.monotonic()))

        def poll_voice():
            result = list(notifications)
            notifications.clear()
            return result

        target = SimpleNamespace(changed=threading.Event(), voice_attributes={0x3F: "control", 0x3C: "audio"},
                                 voice_evidence=None, open=mock.AsyncMock(), open_voice=mock.AsyncMock(),
                                 write_voice=write_voice, poll_voice=poll_voice,
                                 close_voice=mock.AsyncMock(), close=mock.Mock())

        def write(raw):
            event = events.decode(raw)
            received.append(event)
            if event["type"] == "voice" and event["event"] == "available" and event["data"] == "ready":
                inbox.append(commands.encode("stop"))

        with mock.patch.object(worker, "_selected", return_value=True), \
             mock.patch.object(worker, "SelectedDevice", return_value=target), \
             mock.patch.object(worker, "HidTap", return_value=Tap()), \
             mock.patch.object(worker, "verify_peer"), mock.patch.object(worker, "inspect_peer"), \
             mock.patch("ovb_rc003.chromecast_voice_receiver.config.load_config", return_value={}):
            result = await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1),
                SimpleNamespace(read=lambda: inbox.popleft() if inbox else None, write=write)), 3)
        self.assertEqual(result, 0)
        target.open_voice.assert_awaited_once_with(direct=True)
        self.assertIn(("available", "ready"),
                      [(event["event"], event["data"]) for event in received if event["type"] == "voice"])


class DirectVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_negotiation_uses_direct_notifications(self):
        observed = []
        target = SimpleNamespace(
            voice_attributes={0x3F: "control", 0x3C: "audio"},
            voice_evidence=None,
            open_voice=mock.AsyncMock(), write_voice=mock.AsyncMock(),
        )
        receiver = VoiceReceiver(target, lambda *event: observed.append(event),
                                 direct_notifications=True)
        task = asyncio.create_task(receiver.open())
        for _ in range(100):
            if target.write_voice.await_count:
                break
            await asyncio.sleep(.01)
        target.write_voice.assert_awaited_once_with(GET_CAPABILITIES)
        receiver.notification(0x3F, bytes((11, 1, 0, 2, 3, 0, 160, 0, 0)), time.monotonic())
        await asyncio.wait_for(task, 2)
        target.open_voice.assert_awaited_once_with(direct=True)
        self.assertEqual([event[0:3] for event in observed if event[0] == "available"],
                         [("available", 0, "ready")])

    async def test_selected_voice_notifications_subscribe_before_delivery_and_unsubscribe(self):
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattCharacteristicProperties

        chars = []
        for part, handle, prop in ((2, 0x39, GattCharacteristicProperties.WRITE),
                                   (3, 0x3B, GattCharacteristicProperties.NOTIFY),
                                   (4, 0x3E, GattCharacteristicProperties.NOTIFY)):
            characteristic = SimpleNamespace(
                uuid=uuid.UUID(f"ab5e000{part}-5a21-4f05-bc7d-af01f617b664"),
                attribute_handle=handle, characteristic_properties=prop,
                add_value_changed=mock.Mock(return_value=part), remove_value_changed=mock.Mock(),
                write_client_characteristic_configuration_descriptor_async=mock.AsyncMock(return_value=0))
            chars.append(characteristic)
        service = SimpleNamespace(get_characteristics_with_cache_mode_async=mock.AsyncMock(
            return_value=SimpleNamespace(status=0, characteristics=chars)))
        target = device.SelectedDevice(ENTITY)
        target.device = SimpleNamespace(get_gatt_services_for_uuid_with_cache_mode_async=mock.AsyncMock(
            return_value=SimpleNamespace(status=0, services=[service])))
        await target.open_voice(direct=True)
        self.assertEqual(len(target._voice_tokens), 2)
        chars[2].add_value_changed.call_args.args[0](None, SimpleNamespace(characteristic_value=b"\x0b"))
        chars[1].add_value_changed.call_args.args[0](None, SimpleNamespace(characteristic_value=b"\x01\x02"))
        self.assertEqual([(attribute, value) for attribute, value, _ in target.poll_voice()],
                         [(0x3F, b"\x0b"), (0x3C, b"\x01\x02")])
        await target.close_voice()
        chars[1].remove_value_changed.assert_called_once_with(3)
        chars[2].remove_value_changed.assert_called_once_with(4)
