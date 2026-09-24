"""Google initialization against a deterministic, initially legacy peer."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import chromecast_voice_receiver as adapter
from ovb_rc003.atvv_session import ATVVSession
from ovb_rc003.chromecast_channel import Channel, SessionIdentity
from ovb_rc003.chromecast_observation import Observation
from ovb_rc003.chromecast_voice import GET_CAPABILITIES
from tests.test_chromecast_voice import CAPS, PHYSICAL, RELEASE


class NegotiationTests(unittest.IsolatedAsyncioTestCase):
    def test_main_process_applies_caps_to_each_new_decoder(self):
        from ovb_rc003.chromecast_voice_host import VoiceHost
        host = VoiceHost(mock.Mock())
        for attempt in (1, 2):
            host.attempt, host.state = attempt, "starting"
            host.decoder = ATVVSession(gain_db=0)
            host.pending_pcm.clear()
            host.pending_samples = 0
            for kind, data in (("control", CAPS), ("control", PHYSICAL),
                               ("audio", bytes(120))):
                host._handle(dict(event=kind, attempt=attempt, data=data.hex()))
            self.assertEqual(host.pending_samples, 0)
            host._handle(dict(event="audio", attempt=attempt, data=bytes(40).hex()))
            self.assertEqual(host.pending_samples, 320)

    def receiver(self, *, detect=False):
        self.sent, self.rows = [], []
        self.device = SimpleNamespace(open_voice=mock.AsyncMock(), write_voice=mock.AsyncMock(),
                                      voice_attributes={0x3f: "control", 0x3c: "audio"})
        self.observation = Observation(self.rows.append)
        patcher = mock.patch.object(adapter.config, "load_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.target = adapter.VoiceReceiver(self.device, lambda *event: self.sent.append(event),
                                           detect_only=detect, observation=self.observation)
        return self.target

    def notify(self, payload, attribute=0x3f, stamp=None):
        self.target.notification(attribute, payload, time.monotonic() if stamp is None else stamp)

    def validate_journal(self):
        self.observation.flush(final=True)
        tx = Channel(SessionIdentity.create("a" * 64), commands=False)
        tx.encode("ready")
        for row in self.rows:
            tx.encode("evidence", record=row)
        for kind, attempt, data, stamp in self.sent:
            tx.encode("voice", event=kind, attempt=attempt, data=data, time=stamp)

    async def test_initially_legacy_peer_negotiates_then_decodes_first_hold_and_closes(self):
        target = self.receiver()
        async def write(command):
            if command == GET_CAPABILITIES:
                # Before the reply, legacy/startup data cannot start a host.
                self.notify(b"\x04")
                self.notify(bytes(20), 0x3c)
                self.notify(CAPS)
                # Real ETW batches can deliver these before write returns.
                self.notify(PHYSICAL)
                self.notify(bytes(160), 0x3c)
            elif command == b"\x0d\x01":
                self.notify(RELEASE)
        self.device.write_voice.side_effect = write
        await target.open()
        self.assertTrue(target.available)
        sequence = [(e[0], e[2]) for e in self.sent if e[0] in ("host_start", "control", "audio")]
        self.assertEqual(sequence, [("host_start", ""), ("control", CAPS.hex()),
                                    ("control", PHYSICAL.hex()), ("audio", bytes(160).hex())])
        decoder = ATVVSession(gain_db=0)
        pcm = []
        for kind, data in sequence:
            if kind == "control":
                decoder.handle_control(bytes.fromhex(data))
            elif kind == "audio":
                pcm.extend(decoder.handle_audio(bytes.fromhex(data)))
        self.assertEqual(len(pcm), 320)  # Negotiated 160 bytes, not old default 120.
        target.host(dict(attempt=1, result="ready"))
        target.stop()
        await target.tick()
        await asyncio.sleep(0)
        await target.tick()
        self.assertIsNone(target.gate.stream)
        self.assertEqual(self.device.write_voice.await_args_list,
                         [mock.call(GET_CAPABILITIES), mock.call(b"\x0d\x01")])
        self.validate_journal()
        await target.close()

    async def test_unsupported_reply_never_opens_host_or_guesses_codec(self):
        for reply in (bytes((11, 0, 4, 0, 1, 0, 134, 0, 20)),
                      b"\x0b\x01", bytes((11, 1, 0, 1, 3, 0, 160, 0, 0)),
                      bytes((11, 1, 0, 2, 0, 0, 160, 0, 0)),
                      bytes((11, 1, 0, 2, 3, 0, 0, 0, 0))):
            with self.subTest(reply=reply.hex()):
                target = self.receiver()
                self.device.write_voice.side_effect = lambda _: self.notify(reply)
                await target.open()
                self.notify(PHYSICAL)
                self.assertFalse(target.available)
                self.assertFalse(any(e[0] == "host_start" for e in self.sent))
                self.assertIn("voice_caps_unsupported", [r.get("stage") for r in self.rows])
                self.device.write_voice.assert_awaited_once_with(GET_CAPABILITIES)
                self.validate_journal()

    async def test_write_failure_and_missing_response_are_distinct_without_retry(self):
        for write_error, stage in ((OSError("private detail"), "voice_caps_write_failed"),
                                   (None, "voice_caps_timeout")):
            target = self.receiver()
            self.device.write_voice.side_effect = write_error
            with mock.patch.object(adapter, "CAPABILITIES_TIMEOUT", .01):
                await target.open()
            self.assertFalse(target.available)
            self.assertIn(stage, [r.get("stage") for r in self.rows])
            self.device.write_voice.assert_awaited_once_with(GET_CAPABILITIES)
            self.assertNotIn("private detail", str(self.rows))
            self.validate_journal()

    async def test_stale_or_wrong_attribute_caps_cannot_complete_negotiation(self):
        target = self.receiver()
        def write(_):
            self.notify(CAPS, stamp=0)
            self.notify(CAPS, attribute=0x29)
        self.device.write_voice.side_effect = write
        with mock.patch.object(adapter, "CAPABILITIES_TIMEOUT", .01):
            await target.open()
        self.assertFalse(target.available)
        self.validate_journal()

    async def test_startup_buffer_is_bounded_and_overflow_is_not_ready(self):
        target = self.receiver()
        def write(_):
            self.notify(CAPS)
            for _ in range(300):
                self.notify(bytes(160), 0x3c)
            self.assertLessEqual(target._startup_bytes, 32768)
            self.assertLessEqual(len(target._startup), 256)
        self.device.write_voice.side_effect = write
        await target.open()
        self.assertFalse(target.available)
        self.assertFalse(target._startup)
        self.assertIn("voice_caps_overflow", [r.get("stage") for r in self.rows])
        self.validate_journal()

    async def test_cancel_during_negotiation_stays_passive_and_clears_memory(self):
        target = self.receiver()
        entered = asyncio.Event()
        async def write(_):
            self.notify(CAPS)
            self.notify(PHYSICAL)
            entered.set()
            await asyncio.Event().wait()
        self.device.write_voice.side_effect = write
        task = asyncio.create_task(target.open())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        target.stop()
        await target.close()
        self.assertFalse(target.available)
        self.assertFalse(target._caps_waiting)
        self.assertFalse(target._startup)
        self.assertFalse(any(e[0] == "host_start" for e in self.sent))

    async def test_passive_detection_rearms_on_legacy_release_without_writes(self):
        target = self.receiver(detect=True)
        await target.open()
        for payload in (b"\x04", b"\x08", b"\x08", b"\x00", b"\x04", b"\x08", b"\x00"):
            self.notify(payload)
        self.assertEqual(sum(e[0] == "mic" for e in self.sent), 2)
        self.device.write_voice.assert_not_called()
        self.validate_journal()

    async def test_device_command_allowlist_still_rejects_arbitrary_negotiations(self):
        from ovb_rc003.chromecast_device_windows import SelectedDevice
        from ovb_rc003.chromecast_pipe_windows import PipeError
        from winrt.windows.devices.bluetooth import BluetoothConnectionStatus
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattWriteOption
        device = SelectedDevice("a" * 64)
        device.device = SimpleNamespace(connection_status=BluetoothConnectionStatus.CONNECTED)
        device.voice_write_option = GattWriteOption.WRITE_WITHOUT_RESPONSE
        device.voice_tx = SimpleNamespace(write_value_with_result_and_option_async=mock.AsyncMock(
            return_value=SimpleNamespace(status=0)))
        await device.write_voice(GET_CAPABILITIES)
        for bad in (b"\x0a", GET_CAPABILITIES[:-1] + b"\x00", bytearray(GET_CAPABILITIES),
                    b"\x0c\x01", b"\x0d\xff", b"\x0d"):
            with self.assertRaises(PipeError):
                await device.write_voice(bad)
        device.changed.set()
        with self.assertRaises(PipeError):
            await device.write_voice(GET_CAPABILITIES)
        self.assertEqual(device.voice_tx.write_value_with_result_and_option_async.await_count, 1)


if __name__ == "__main__":
    unittest.main()
