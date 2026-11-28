"""验证完整 8 位语音流编号的启动、停止、续期与诊断。"""
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import chromecast_device_windows as device
from ovb_rc003 import chromecast_voice_receiver as adapter
from ovb_rc003.chromecast_observation import Observation, valid_record
from ovb_rc003.chromecast_pipe_windows import PipeError
from ovb_rc003.chromecast_voice import Recording


class StreamRangeTests(unittest.TestCase):
    def test_hold_start_extend_stop_and_return_to_idle(self):
        for stream in (1, 128, 129, 255):
            with self.subTest(stream=stream):
                gate = Recording("hold")
                effects = gate.control(bytes((4, 3, 2, stream)), 1.0)
                self.assertTrue(any(e.kind == "host_start" for e in effects))
                self.assertEqual(gate.stream, (stream, 3))
                gate.host_result(gate.attempt, True, 1.1)
                self.assertEqual(gate.state, "recording")
                self.assertEqual(gate.tick(20.9), [])
                gate.audio(bytes(20), 21.0)
                effects = gate.tick(21.1)
                self.assertEqual([e.value for e in effects], [bytes((14, stream))])
                self.assertTrue(gate.command_valid(effects[0]))
                effects = gate.finish(22.0, "released")
                commands = [e for e in effects if e.kind == "device"]
                self.assertEqual([e.value for e in commands], [bytes((13, stream))])
                self.assertTrue(gate.command_submitted(commands[0]))
                gate.command_result(commands[0], True, 22.1)
                gate.control(b"\x00\x02", 22.2)
                gate.host_stopped(gate.attempt, True, 22.3)
                gate.tick(22.6)
                self.assertEqual(gate.state, "idle")

    def test_physical_zero_and_nonzero_software_stream_stay_rejected(self):
        for payload in (b"\x04\x03\x02\x00", b"\x04\x00\x02\x81"):
            gate = Recording("hold")
            self.assertEqual(gate.control(payload, 1.0), [])
            self.assertEqual(gate.control_result, "unsupported_voice_stream")
            self.assertIsNone(gate.stream)

    def test_detection_accepts_full_range_without_starting_audio(self):
        for stream in (128, 129, 255, 0):
            with self.subTest(stream=stream):
                sent = []
                with mock.patch.object(adapter.config, "load_config", return_value={}):
                    receiver = adapter.VoiceReceiver(
                        SimpleNamespace(voice_attributes={63: "control"}),
                        lambda *args: sent.append(args), detect_only=True)
                receiver.available = True
                receiver.notification(63, bytes((4, 3, 2, stream)), 1.0)
                self.assertEqual(receiver.mic_down, stream != 0)
                presses = [e for e in sent if e[0] == "mic"]
                self.assertEqual(len(presses), int(stream != 0))
                receiver.notification(63, b"\x00\x02", 1.1)
                self.assertFalse(receiver.mic_down)
                self.assertFalse(any(e[0] in ("audio", "host_start") for e in sent))

    def test_command_evidence_keeps_full_range_and_rejects_out_of_range(self):
        for stream in (-2, -1, 0, 128, 129, 255, 256, True):
            with self.subTest(stream=stream):
                fields = dict(attempt=1, opcode=14, stream=stream, state="written",
                              duration_ms=0, error_type="none", error_family="unclassified",
                              native_code=-1, status=-1, protocol_error=-1)
                expected = type(stream) is int and -1 <= stream <= 255
                self.assertEqual(valid_record(dict(kind="voice_command", elapsed_ms=0, **fields)), expected)
                rows = []
                Observation(rows.append).voice_command(fields)
                self.assertEqual(len(rows), int(expected))


def fake_winrt():
    names = ("winrt", "winrt.windows", "winrt.windows.devices",
             "winrt.windows.devices.bluetooth", "winrt.windows.storage",
             "winrt.windows.storage.streams")
    modules = {name: types.ModuleType(name) for name in names}
    modules["winrt.windows.devices.bluetooth"].BluetoothConnectionStatus = SimpleNamespace(CONNECTED=1)

    class DataWriter:
        def write_bytes(self, value):
            self.value = value

        def detach_buffer(self):
            return self.value

        def close(self):
            pass

    modules["winrt.windows.storage.streams"].DataWriter = DataWriter
    return modules


class DeviceCommandRangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_writer_accepts_stop_and_extend_with_full_stream_byte(self):
        selected = device.SelectedDevice("a" * 64)
        selected.device = SimpleNamespace(connection_status=1)
        selected.voice_write_option = object()
        write = mock.AsyncMock(return_value=SimpleNamespace(status=0))
        selected.voice_tx = SimpleNamespace(write_value_with_result_and_option_async=write)
        commands = (b"\x0c\x00",) + tuple(bytes((opcode, stream))
                    for opcode in (13, 14) for stream in (0, 128, 129, 255))
        with mock.patch.dict(sys.modules, fake_winrt()):
            for command in commands:
                await selected.write_voice(command)
        self.assertEqual([call.args[0] for call in write.await_args_list], list(commands))

    async def test_real_writer_still_rejects_invalid_commands(self):
        selected = device.SelectedDevice("a" * 64)
        selected.device = SimpleNamespace(connection_status=1)
        selected.voice_tx = SimpleNamespace(write_value_with_result_and_option_async=mock.AsyncMock())
        with mock.patch.dict(sys.modules, fake_winrt()):
            for command in (b"", b"\x0a", b"\x0c\x81", b"\x0d", b"\x0d\xff\x00",
                            b"\xff\xff", bytearray(b"\x0d\xff")):
                with self.subTest(command=command), self.assertRaises(PipeError):
                    await selected.write_voice(command)
        selected.voice_tx.write_value_with_result_and_option_async.assert_not_awaited()
