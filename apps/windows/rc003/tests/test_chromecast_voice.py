import unittest
import ctypes
from unittest import mock
from types import SimpleNamespace
import threading
import asyncio
import time
from collections import deque
import os
import subprocess
import sys
import tempfile

from ovb_rc003.chromecast_voice import Recording, Effect, LIMITS, DEFAULT_LIMIT, GET_CAPABILITIES
from ovb_rc003.chromecast_voice_host import VoiceHost
from ovb_rc003.audio_playback_worker import PlaybackWriteWorker
from ovb_rc003 import voice_controller, chromecast_channel as channel, win32_input, raw_input_windows

PHYSICAL = bytes((4, 3, 2, 1))
SOFTWARE = bytes((4, 0, 2, 0))
RELEASE = bytes((0, 2))
STOP = bytes((0, 0))
CAPS = bytes((11, 1, 0, 2, 3, 0, 160, 0, 0))


def reply_caps(receiver, command):
    if command == GET_CAPABILITIES:
        receiver.notification(0x3f, CAPS, time.monotonic())


class RecordingTests(unittest.TestCase):
    def open_toggle(self, limit=120):
        gate = Recording("toggle", limit)
        gate.control(PHYSICAL, 1)
        gate.control(b"\x08", 1.01)
        gate.control(RELEASE, 1.1)
        command = gate.host_result(1, True, 1.2)[0]
        self.assertTrue(gate.command_submitted(command))
        gate.control(SOFTWARE, 1.3)
        self.assertEqual(gate.state, "recording")
        return gate

    def test_defaults_and_validation(self):
        self.assertEqual(DEFAULT_LIMIT, 120)
        for limit in LIMITS:
            self.assertEqual(Recording("toggle", limit).limit, limit)
        for mode, limit in [("other", 120), ("hold", True), ("toggle", 0)]:
            with self.assertRaises(ValueError):
                Recording(mode, limit)

    def test_release_reason_is_not_inferred_from_any_device_stop(self):
        for payload, reason in ((RELEASE, "released"), (STOP, "device_ended")):
            gate = Recording()
            gate.control(PHYSICAL, 1)
            gate.host_result(1, True, 1.1)
            gate.control(payload, 2)
            self.assertEqual(gate.reason, reason)
            self.assertEqual(gate.state, "stopping")

    def test_starting_repeat_is_not_toggle_or_second_host_start(self):
        gate = Recording("toggle")
        initial = gate.control(PHYSICAL, 1)
        self.assertEqual(sum(e.kind == "host_start" for e in initial), 1)
        for now in (1.1, 1.2, 1.3):
            self.assertEqual(gate.control(b"\x08", now), [])
            self.assertEqual(gate.control(PHYSICAL, now), [])
        self.assertEqual(gate.state, "starting")
        gate.control(RELEASE, 1.4)
        effects = gate.host_result(1, True, 1.5)
        self.assertEqual([e.value for e in effects], [b"\x0c\x00"])
        self.assertEqual(gate.state, "starting")
        self.assertTrue(gate.command_submitted(effects[0]))
        gate.control(SOFTWARE, 1.6)
        self.assertEqual(gate.state, "recording")

    def test_host_ready_before_release_does_not_send_open_while_physical_stream_runs(self):
        gate = Recording("toggle")
        gate.control(PHYSICAL, 1)
        self.assertEqual(gate.host_result(1, True, 1.01), [])
        effects = gate.control(RELEASE, 1.1)
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0c\x00"])

    def test_second_physical_start_closes_before_its_button_notice(self):
        gate = self.open_toggle()
        effects = gate.control(bytes((4, 3, 2, 2)), 2)
        self.assertEqual(gate.state, "stopping")
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x02"])
        self.assertEqual(gate.control(b"\x08", 2.01), [])
        self.assertFalse(any(e.kind == "host_start" for e in effects))

        gate.control(RELEASE, 2.1)
        gate.host_stopped(1, True, 2.5)
        self.assertEqual(gate.state, "idle")

    def test_second_button_stops_software_stream(self):
        gate = self.open_toggle()
        effects = gate.control(b"\x08", 2)
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x00"])
        gate.control(STOP, 2.1)
        gate.host_stopped(1, True, 2.15)
        self.assertEqual(gate.state, "stopping")
        gate.tick(2.5)
        self.assertEqual(gate.state, "idle")

    def test_two_minute_cap_is_logical_not_stream_segments(self):
        gate = self.open_toggle()
        effects = gate.tick(121.3)
        self.assertEqual(gate.state, "stopping")
        self.assertEqual(gate.reason, "time_limit")
        self.assertTrue(any(e.kind == "host_stop" for e in effects))

    def test_extend_requires_fresh_audio_and_valid_attempt(self):
        gate = self.open_toggle()
        self.assertEqual(gate.tick(22), [])
        gate.audio(bytes(20), 22.1)
        command = gate.tick(22.2)[0]
        self.assertEqual(command.value, b"\x0e\x00")
        self.assertTrue(gate.command_valid(command))
        gate.finish(23)
        self.assertFalse(gate.command_valid(command))
        self.assertFalse(gate.command_valid(Effect("device", 0, b"\x0d\x00")))

    def test_hold_release_never_opens_software_stream(self):
        gate = Recording()
        gate.control(PHYSICAL, 1)
        gate.host_result(1, True, 1.1)
        self.assertEqual(gate.state, "recording")
        effects = gate.control(RELEASE, 2)
        self.assertEqual(gate.state, "stopping")
        self.assertFalse(any(e.kind == "device" for e in effects))
        gate.host_stopped(1, True, 2.4)
        self.assertEqual(gate.state, "idle")

    def test_failure_cannot_restart_before_host_release(self):
        gate = Recording()
        gate.control(PHYSICAL, 1)
        gate.host_result(1, False, 1.1)
        gate.control(STOP, 1.2)
        gate.tick(7)
        self.assertEqual(gate.state, "blocked")
        effects = gate.control(bytes((4, 3, 2, 2)), 8)
        self.assertFalse(any(e.kind == "host_start" for e in effects))
        self.assertEqual(gate.state, "blocked")
        gate.control(STOP, 8.1)
        gate.host_stopped(1, True, 8.5)
        self.assertEqual(gate.state, "idle")
        gate.control(PHYSICAL, 9)
        self.assertEqual(gate.attempt, 2)
        self.assertEqual(gate.host_result(1, True, 9.1), [])

    def test_open_in_flight_shutdown_keeps_liability(self):
        gate = Recording("toggle")
        gate.control(PHYSICAL, 1)
        gate.control(RELEASE, 1.1)
        command = gate.host_result(1, True, 1.2)[0]
        gate.command_submitted(command)
        effects = gate.finish(1.3, shutdown=True)
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x00"])
        gate.host_stopped(1, True, 1.4)
        gate.control(STOP, 1.5)
        gate.tick(2)
        self.assertNotEqual(gate.state, "idle")
        # Late successful OPEN still requires a new exact close.
        effects = gate.control(SOFTWARE, 2.1)
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x00"])
        gate.control(STOP, 2.2)
        gate.tick(2.6)
        self.assertEqual(gate.state, "idle")
        effects = gate.control(PHYSICAL, 3)
        self.assertFalse(any(e.kind == "host_start" for e in effects))

    def test_no_stale_audio_forwarding_and_quiet_gate(self):
        gate = self.open_toggle()
        gate.finish(2)
        gate.control(STOP, 2.1)
        gate.host_stopped(1, True, 2.2)
        self.assertEqual(gate.audio(bytes(20), 2.35), [])
        gate.tick(2.5)
        self.assertEqual(gate.state, "stopping")
        gate.tick(2.7)
        self.assertEqual(gate.state, "idle")

    def test_timeout_not_success_and_no_auto_retry(self):
        gate = Recording("toggle")
        gate.control(PHYSICAL, 1)
        gate.tick(11)
        self.assertEqual(gate.state, "stopping")
        self.assertEqual(gate.reason, "start_timeout")
        gate.tick(16)
        self.assertEqual(gate.state, "blocked")

    def test_write_error_is_not_shutdown_confirmation(self):
        gate = self.open_toggle()
        command = next(e for e in gate.finish(2) if e.kind == "device")
        gate.command_result(command, False, 2.1)
        self.assertEqual(gate.state, "blocked")
        self.assertEqual(gate.stream, (0, 0))

    def test_invalid_clock(self):
        gate = Recording()
        gate.tick(2)
        for value in (1, float("nan"), True, -1):
            with self.assertRaises(ValueError):
                gate.tick(value)


class HostTests(unittest.TestCase):
    def setUp(self):
        detection = mock.patch("ovb_rc003.chromecast_voice_host.key_detection_bridge.publish_next_button", return_value=False)
        self.detect = detection.start()
        self.addCleanup(detection.stop)
        raw_input_windows._set_physical_keyboard_tracker_active(True)
        self.addCleanup(raw_input_windows._set_physical_keyboard_tracker_active, False)
        self.owner = mock.Mock()
        o = self.owner
        from ovb_rc003.voice_audio_session import VoiceAudioSession
        # Exercise the real ownership operations; only the device I/O is mocked.
        o._voice_audio.stop_writer.side_effect = lambda: VoiceAudioSession.stop_writer(o._voice_audio)
        o._voice_audio.close_sink.side_effect = lambda: VoiceAudioSession.close_sink(o._voice_audio)
        o._config = {"gain_db": 0, "voice_program": {"provider": "wetype"}}
        o._voice_shortcut.lock = threading.Lock()
        o._voice_shortcut.runtime_lock = threading.Lock()
        o._voice_shortcut.runtime_mic_confirmed = None
        o._voice_shortcut.pending_tokens = None
        o._voice_shortcut.hotkey = SimpleNamespace(modifiers=("lctrl",), key="lwin")
        o._voice_shortcut.controller = voice_controller.VoiceController()
        o._configured_voice_hotkey_backend.return_value = "wetype_hotkey"
        o._voice_mode_for_primary_button.return_value = "hold"
        o._voice_audio.open.return_value = True
        o._voice_shortcut.apply.return_value = True
        o._voice_shortcut.release_pending.return_value = True
        o._voice_shortcut.control_flag.return_value = False
        o._voice_audio.flush.return_value = SimpleNamespace(completed=True, error=None)
        o._voice_audio.stats.frames = 2
        from ovb_rc003.app import RC003App
        self.host = RC003App._create_chromecast_voice_host(o)
        for target, result in (("ovb_rc003.chromecast_host_activity.read_wetype_capture", ()),):
            patcher = mock.patch(target, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.host.client = mock.Mock()

    def test_delayed_confirmation_preserves_pcm_without_exhausting_writer(self):
        # Hold the real worker while five seconds of small decoder frames arrive.
        # Capacity must cover samples, not depend on the worker winning a race.
        unblock = threading.Event()
        output = []
        writer = PlaybackWriteWorker(
            lambda samples: (unblock.wait(3), output.extend(samples)), lambda error: None)
        writer.start()
        self.addCleanup(writer.stop)
        self.addCleanup(unblock.set)
        self.host._start_host(1)
        self.owner._voice_audio.writer = writer
        self.owner._voice_audio.flush.side_effect = lambda reason: writer.flush()
        expected = list(range(80000))
        self.host.pending_pcm.extend(expected[i:i + 160] for i in range(0, 80000, 160))
        self.host.pending_samples = len(expected)
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.assertIsNone(writer.failure)
        # Live frames must use the same batching; otherwise they fill the slots
        # left by the startup backlog before that backlog can drain.
        for _ in range(100):
            self.host._output([7] * 160)
        self.host._output([8, 9, 10])
        self.assertIsNone(writer.failure)
        unblock.set()
        self.assertTrue(self.host._stop_host())
        self.assertEqual(output, expected + [7] * 16000 + [8, 9, 10])

    def test_failed_writer_is_retired_and_next_attempt_can_open_fresh_output(self):
        failed = threading.Event()
        def broken_write(samples):
            raise OSError("test output lost")
        writer = PlaybackWriteWorker(broken_write, lambda error: failed.set())
        writer.start()
        self.addCleanup(writer.stop)
        self.host._start_host(1)
        self.owner._voice_audio.writer = writer
        old_sink = self.owner._voice_audio.sink
        self.owner._voice_audio.flush.side_effect = lambda reason: writer.flush()
        writer.submit([1])
        self.assertTrue(failed.wait(1))
        self.assertTrue(self.host._stop_host())
        self.assertFalse(writer.is_alive)
        self.assertIsNone(self.owner._voice_audio.writer)
        self.assertIsNone(self.owner._voice_audio.sink)
        old_sink.close.assert_called_once()
        self.assertIsNotNone(self.host.failure_result)
        self.owner._voice_shortcut.record_audio_result.assert_not_called()
        self.host._handle({"event": "state", "attempt": 1, "data": "idle"})
        self.host._start_host(2)
        self.assertTrue(self.host.engaged)
        self.assertEqual(self.host.attempt, 2)
        self.assertIsNone(self.host.failure_result)

    def test_shutdown_does_not_retain_dead_writer_after_failed_flush(self):
        writer = PlaybackWriteWorker(lambda samples: None, lambda error: None)
        writer.start()
        self.addCleanup(writer.stop)
        self.host._start_host(1)
        self.owner._voice_audio.writer = writer
        self.owner._voice_audio.flush.side_effect = lambda reason: writer.flush()
        writer._record_failure(OSError("test earlier output failure"))
        self.host._close_resources()
        self.assertTrue(self.host.closed)
        self.assertFalse(writer.is_alive)
        self.assertIsNone(self.owner._voice_audio.writer)
        self.assertIsNone(self.owner._voice_audio.sink)

    def test_failed_audio_still_blocks_restart_until_writer_and_endpoint_close(self):
        self.host._start_host(1)
        writer, sink = self.owner._voice_audio.writer, self.owner._voice_audio.sink
        writer.stop.return_value = False
        self.owner._voice_audio.flush.return_value = SimpleNamespace(
            completed=True, error=OSError("test write failed"))
        self.assertFalse(self.host._stop_host())
        self.assertIs(self.owner._voice_audio.writer, writer)
        sink.close.assert_not_called()
        self.host._start_host(2)
        self.assertEqual(self.host.attempt, 1)
        writer.stop.return_value = True
        sink.close.side_effect = OSError("test endpoint busy")
        self.assertFalse(self.host._stop_host())
        self.assertIsNone(self.owner._voice_audio.writer)
        self.assertIs(self.owner._voice_audio.sink, sink)
        self.host._start_host(2)
        self.assertEqual(self.host.attempt, 1)
        sink.close.side_effect = None
        self.assertTrue(self.host._stop_host())
        self.assertIsNone(self.owner._voice_audio.sink)
        # The retained failure is not polled through a now-stopped writer.
        self.owner._voice_audio.flush.assert_called_once()

    def test_shutdown_retiring_audio_does_not_hide_pending_key_release(self):
        self.host._start_host(1)
        writer, sink = self.owner._voice_audio.writer, self.owner._voice_audio.sink
        self.owner._voice_shortcut.release_pending.return_value = False
        self.host._close_resources()
        self.assertFalse(self.host.closed)
        self.assertIsNone(self.owner._voice_audio.writer)
        self.assertIsNone(self.owner._voice_audio.sink)
        self.owner._voice_shortcut.release_pending.return_value = True
        self.host._close_resources()
        self.assertTrue(self.host.closed)
        writer.stop.assert_called_once()
        sink.close.assert_called_once()

    def test_input_cancellation_discards_partial_output_before_releasing_keys(self):
        self.host._start_host(1)
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.host._output([1, 2, 3])
        self.host.cancel_recording()
        self.host._poll_host()
        self.assertTrue(self.host.closed)
        self.owner._voice_audio.writer.submit.assert_not_called()
        self.host._handle({"event": "state", "attempt": 1, "data": "idle"})
        self.host._start_host(2)
        self.assertTrue(self.host._stop_host())
        self.owner._voice_audio.writer.submit.assert_not_called()

    def test_service_start_does_not_install_keyboard_mouse_or_foreground_hooks(self):
        user32 = mock.Mock()
        with mock.patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True), \
             mock.patch.object(self.host, "_run") as run:
            self.host.start(self.host.client)
            self.host.thread.join(1)
            self.assertFalse(self.host.thread.is_alive())
            run.assert_called_once()
        user32.SetWindowsHookExW.assert_not_called()
        user32.GetForegroundWindow.assert_not_called()
        self.assertFalse(hasattr(self.host, "input_guard"))

    def test_hotkey_delivery_is_not_host_confirmation(self):
        self.host._start_host(1)
        self.host._poll_host()
        self.host.client.voice_host.assert_not_called()
        self.assertFalse(self.host.confirmed)
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.host.client.voice_host.assert_called_once_with(1, "ready")
        self.host._poll_host()
        self.assertEqual(self.host.client.voice_host.call_count, 1)

    def test_capture_diagnostics_identify_owner_without_exposing_session_strings(self):
        from ovb_rc003.chromecast_host_activity import CaptureWatch
        from ovb_rc003.voice_playback_session_windows import CaptureSession
        self.host._start_host(1)
        captured = CaptureSession("private endpoint", "private session path", 42, 1)
        reader = mock.Mock(side_effect=[(), (captured,)])
        self.host.capture_watch = CaptureWatch(reader)
        self.host.capture_watch.begin()
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.host._poll_host()
        logs = [c.args for c in self.owner._logger.info.call_args_list
                if c.args[0].startswith("Chromecast host activity")]
        self.assertEqual(len(logs), 1)
        message = logs[0][0] % logs[0][1:]
        self.assertIn("capture_pid=42", message)
        self.assertIn("baseline_count=0", message)
        self.assertIn("bubble=unobserved", message)
        self.assertNotIn("private", message)
        self.host.client.voice_host.assert_called_once_with(1, "ready")

    def test_hold_confirmation_does_not_republish_runtime_active(self):
        self.host._start_host(1)
        self.owner._set_runtime_voice_active.reset_mock()
        self.owner._set_runtime_voice_result.reset_mock()
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.host.client.voice_host.assert_called_once_with(1, "ready")
        self.owner._set_runtime_voice_active.assert_not_called()
        self.owner._set_runtime_voice_result.assert_not_called()
    def test_stop_during_starting_prevents_late_ready_and_invalidates_queued_start(self):
        self.host._start_host(1, 0)
        self.host.cancel_recording()
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.assertNotIn(mock.call(1, "ready"), self.host.client.voice_host.call_args_list)
        self.host._handle({"event": "state", "attempt": 1, "data": "idle"})
        self.owner._voice_shortcut.apply.reset_mock()
        self.host._start_host(2, 0)
        self.owner._voice_shortcut.apply.assert_not_called()

    def test_host_capture_end_stops_recording_not_just_starting(self):
        self.host._start_host(1)
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.host.state = "recording"
        self.host.capture_watch = mock.Mock(status="ended", identity=None, baseline=set())
        self.host.capture_watch.poll.return_value = True
        self.host.client.reset_mock()
        self.host._poll_host()
        self.assertIn(mock.call(1, "stop"), self.host.client.voice_host.call_args_list)
        self.assertTrue(self.host.closed)

    def test_tracking_loss_release_failure_blocks_next_recording(self):
        self.host._start_host(1)
        self.host.cancel_recording()
        self.owner._voice_shortcut.release_pending.return_value = False
        self.host._poll_host()
        self.assertFalse(self.host.closed)
        self.assertEqual(self.host.state, "stopping")
        self.owner._voice_shortcut.apply.reset_mock()
        self.host._start_host(2)
        self.owner._voice_shortcut.apply.assert_not_called()

    def test_tracking_loss_releases_keys_before_audio_and_logs_separate_phases(self):
        self.host._start_host(1)
        order = []
        self.owner._voice_shortcut.apply.side_effect = lambda action: order.append("keys") or True
        self.host.cancel_recording()

        def flush(_reason):
            order.append("audio")
            self.assertFalse(self.host.closed)
            self.assertFalse(self.owner._voice_shortcut.controller.active)
            self.assertTrue(self.host._tracking_keys_released)
            self.host._start_host(2)  # No new session while old PCM still drains.
            self.assertEqual(self.host.attempt, 1)
            return SimpleNamespace(completed=True, error=None)

        self.owner._voice_audio.flush.side_effect = flush
        self.host._poll_host()
        self.assertEqual(order, ["keys", "audio"])
        self.assertTrue(self.host.closed)
        logs = [call.args for call in self.owner._logger.info.call_args_list if "phase=" in call.args[0]]
        self.assertEqual(len(logs), 3)
        for log, phase in zip(logs, ("tracking_loss_observed", "shortcut_released", "cleanup_finished")):
            self.assertIn("phase=" + phase, log[0])
            self.assertGreaterEqual(log[-1], 0)
        self.assertLessEqual(logs[1][-1], logs[2][-1])

    def test_normal_remote_stop_preserves_audio_before_keys(self):
        self.host._start_host(1)
        order = []
        self.owner._voice_shortcut.apply.side_effect = lambda action: order.append("keys") or True
        self.owner._voice_audio.flush.side_effect = lambda reason: (
            order.append("audio") or SimpleNamespace(completed=True, error=None))
        self.host._handle({"event": "host_stop", "attempt": 1})
        self.assertEqual(order, ["audio", "keys"])
        self.assertTrue(self.host.closed)
        self.assertFalse(self.host._tracking_loss_seen)

    def test_receiver_stop_before_poll_still_prioritizes_tracker_loss(self):
        self.host._start_host(1, 0)
        self.host.cancel_recording()
        self.owner._voice_audio.flush.side_effect = lambda reason: (
            self.assertFalse(self.owner._voice_shortcut.controller.active) or SimpleNamespace(completed=True, error=None))
        self.host._handle({"event": "host_stop", "attempt": 1})
        self.assertTrue(self.host._tracking_keys_released)
        self.assertEqual(self.host.input_epoch, 1)
        self.host._poll_host()
        self.assertEqual(self.host.input_epoch, 1)

    def test_tracking_release_failure_retries_before_audio_and_keeps_owner(self):
        self.host._start_host(1)
        self.host.cancel_recording()
        self.owner._voice_shortcut.apply.return_value = False
        self.host._poll_host()
        self.assertTrue(self.owner._voice_shortcut.controller.active)
        self.assertFalse(self.host.closed)
        self.assertFalse(self.host._tracking_keys_released)
        self.owner._voice_audio.flush.assert_not_called()
        self.host._start_host(2)
        self.assertEqual(self.host.attempt, 1)
        self.owner._voice_shortcut.apply.return_value = True
        self.assertTrue(self.host._stop_host())
        self.assertFalse(self.owner._voice_shortcut.controller.active)
        self.owner._voice_audio.flush.assert_called_once()

    def test_audio_cleanup_failure_does_not_rehold_keys_or_allow_new_session(self):
        self.host._start_host(1)
        self.host.cancel_recording()
        self.owner._voice_audio.flush.return_value = SimpleNamespace(completed=False, error=None)
        self.host._poll_host()
        self.assertTrue(self.host._tracking_keys_released)
        self.assertFalse(self.host.closed)
        self.host._start_host(2)
        self.assertEqual(self.host.attempt, 1)
        self.owner._voice_shortcut.apply.reset_mock()
        self.owner._voice_audio.flush.return_value = SimpleNamespace(completed=True, error=None)
        self.assertTrue(self.host._stop_host())
        self.owner._voice_shortcut.apply.assert_not_called()
        logs = [c.args[0] for c in self.owner._logger.info.call_args_list]
        self.assertEqual(sum("phase=shortcut_released" in line for line in logs), 1)

    def test_tracker_loss_uses_key_first_cleanup(self):
        self.host._start_host(1)
        self.host.cancel_recording()
        self.owner._voice_audio.flush.side_effect = lambda reason: (
            self.assertTrue(self.host._tracking_keys_released) or SimpleNamespace(completed=True, error=None))
        self.host._poll_host()
        self.assertTrue(self.host.closed)
        self.assertFalse(self.host.input_lost.is_set())

    def test_missing_tracker_fails_before_switch_or_output(self):
        raw_input_windows._set_physical_keyboard_tracker_active(False)
        with mock.patch.object(win32_input.voice_key_physicalizer_windows,
                               "physical_key_tracking_available", return_value=False):
            self.host._start_host(1)
        self.owner._voice_shortcut.apply.assert_not_called()
        self.owner._voice_audio.open.assert_not_called()
        self.owner._set_runtime_voice_result.assert_called_with("host_start_failed")
        self.host._stop_host()
        self.owner._set_runtime_voice_result.assert_called_with("host_start_failed")

    def test_unconfigured_provider_rejects_voice_without_output_or_hotkey(self):
        self.owner._config["voice_program"] = {"provider": "none", "custom_executable": "old.exe"}
        self.host._start_host(1)
        self.owner._voice_shortcut.apply.assert_not_called()
        self.owner._voice_audio.open.assert_not_called()
        self.host.client.voice_host.assert_called_once_with(1, "failed")

    def test_real_default_wetype_sender_keeps_guard_and_delivers_when_tracked(self):
        # Only mock the native OS boundary, not the default sender or its guard.
        for tokens in (("lctrl", "lwin"), ("lctrl", "h")):
            with self.subTest(tokens=tokens), \
                 mock.patch.object(win32_input, "_real_send_virtual_key_input_batch", return_value=1) as native, \
                 mock.patch.object(raw_input_windows, "_real_async_key_is_down", return_value=False):
                win32_input.send_wetype_voice_key_combo_down(tokens, _sleep=lambda _: None)
                win32_input.send_wetype_voice_key_combo_up(tokens, _sleep=lambda _: None)
                codes = win32_input.win32_keys.resolve_vk_codes(tokens)
                self.assertEqual(native.call_args_list, [mock.call([(v, False)]) for v in codes]
                                 + [mock.call([(v, True)]) for v in reversed(codes)])
        raw_input_windows._set_physical_keyboard_tracker_active(False)
        with mock.patch.object(win32_input, "_real_send_virtual_key_input_batch") as native, \
             mock.patch.object(win32_input.voice_key_physicalizer_windows,
                               "physical_key_tracking_available", return_value=False):
            with self.assertRaises(win32_input.Win32InputUnavailableError):
                win32_input.send_wetype_voice_key_combo_down(("lctrl", "lwin"))
            native.assert_not_called()

    def test_delivery_failure_is_not_left_untested(self):
        self.owner._voice_shortcut.apply.return_value = False
        self.host._start_host(1)
        self.host._stop_host()
        self.owner._set_runtime_voice_result.assert_called_with("host_start_failed")
        self.assertFalse(self.host.engaged)

    def test_tracker_loss_stops_recording_without_destroying_host(self):
        self.host._start_host(1)
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.host.cancel_recording()
        self.host._output([1, 2])
        self.owner._voice_audio.writer.submit.assert_not_called()
        self.host._poll_host()
        self.assertTrue(self.host.closed)
        self.assertFalse(self.host.stopping.is_set())
        self.assertFalse(self.owner._voice_shortcut.controller.active)
        self.owner._voice_shortcut.record_audio_result.assert_not_called()
        self.host._handle({"event": "state", "attempt": 1, "data": "idle"})
        self.host._start_host(2)
        self.assertTrue(self.host.engaged)

    def test_old_queued_start_rejected_after_tracker_recovers(self):
        self.host.enqueue({"event": "host_start", "attempt": 1, "data": ""})
        self.host.cancel_recording()
        self.host._poll_host()
        self.host._handle(self.host.events.get_nowait())
        self.owner._voice_shortcut.apply.assert_not_called()
        self.host.client.voice_host.assert_called_with(1, "failed")
        self.host._handle({"event": "host_stop", "attempt": 1, "data": ""})
        self.host.client.voice_host.assert_called_with(1, "released")

    def test_tracker_loss_closes_real_recording_gate_not_just_host_keys(self):
        gate = Recording()
        gate.control(PHYSICAL, time.monotonic())
        effects = []
        def receive(attempt, result):
            now = time.monotonic()
            if result in ("ready", "failed"):
                effects.extend(gate.host_result(attempt, result == "ready", now))
            elif result == "stop":
                effects.extend(gate.finish(now))
            elif result == "released":
                effects.extend(gate.host_stopped(attempt, True, now))
        self.host.client.voice_host.side_effect = receive
        self.host._start_host(1)
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.assertEqual(gate.state, "recording")
        self.host.cancel_recording()
        self.host._poll_host()
        self.assertEqual(gate.state, "stopping")
        self.assertTrue(any(e.kind == "device" and e.value == b"\x0d\x01" for e in effects))
        self.assertTrue(gate.host_released)

    def test_running_service_detects_mic_without_host_or_output(self):
        self.detect.return_value = True
        self.host._start_host(1)
        self.owner._voice_shortcut.apply.assert_not_called()
        self.owner._voice_audio.open.assert_not_called()
        self.assertTrue(self.host.closed)
        self.host.client.voice_host.assert_called_once_with(1, "failed")
        self.host._handle({"event": "host_stop", "attempt": 1, "data": ""})
        self.host._handle({"event": "state", "attempt": 1, "data": "idle"})
        self.detect.return_value = False
        self.host._start_host(2)
        self.owner._voice_shortcut.apply.assert_called_once()

    def test_failed_output_does_not_press_hotkey_or_reuse_old_confirmation(self):
        self.owner._voice_audio.open.return_value = False
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._start_host(1)
        self.host._poll_host()
        self.owner._voice_shortcut.apply.assert_not_called()
        self.host.client.voice_host.assert_called_once_with(1, "failed")

    def test_unknown_host_proof_is_not_guessed(self):
        self.owner._configured_voice_hotkey_backend.return_value = "marked"
        self.host._start_host(1)
        self.owner._voice_audio.open.assert_not_called()
        self.host.client.voice_host.assert_called_with(1, "failed")

    def test_old_attempt_stop_cannot_stop_new_attempt(self):
        self.host._start_host(2)
        self.host._handle({"event": "host_stop", "attempt": 1, "data": ""})
        self.assertTrue(self.owner._voice_shortcut.controller.active)
        self.assertEqual(self.host.state, "starting")

    def test_confirmation_failure_releases_before_retry(self):
        self.host._start_host(1)
        self.owner._voice_shortcut.runtime_mic_confirmed = False
        self.host._poll_host()
        self.assertEqual(self.host.state, "stopping")
        self.assertTrue(self.host.closed)
        self.assertFalse(self.owner._voice_shortcut.controller.active)
        self.host._start_host(2)
        self.assertEqual(self.host.attempt, 1)
        self.host._handle({"event": "state", "attempt": 1, "data": "idle"})
        self.host._start_host(2)
        self.assertEqual(self.host.attempt, 2)

    def test_pending_host_cleanup_does_not_claim_released(self):
        self.host._start_host(1)
        self.owner._voice_shortcut.control_flag.return_value = True
        self.assertFalse(self.host._stop_host())
        self.host.client.voice_host.assert_called_with(1, "release_failed")
        self.owner._voice_shortcut.control_flag.return_value = False
        self.assertTrue(self.host._stop_host())
        self.host.client.voice_host.assert_called_with(1, "released")

    def test_duplicate_begin_does_not_deliver_twice(self):
        self.host._start_host(1)
        self.host._start_host(1)
        self.host._start_host(2)
        self.owner._voice_shortcut.apply.assert_called_once()

    def test_voice_queue_is_bounded(self):
        for _ in range(257):
            self.host.enqueue({})
        self.assertTrue(self.host.stopping.is_set())
        self.host.client.cancel.set.assert_called_once()

    def test_real_decoder_buffers_until_confirmed_and_drops_after_stop(self):
        self.host._start_host(1)
        self.host._handle({"event": "control", "attempt": 1, "data": PHYSICAL.hex()})
        packet = {"event": "audio", "attempt": 1, "data": (b"\x12" * 120).hex()}
        self.host._handle(packet)
        self.assertEqual(self.host.pending_samples, 240)
        self.owner._voice_audio.writer.submit.assert_not_called()
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.owner._voice_audio.writer.submit.assert_not_called()
        self.assertEqual(self.host.pending_samples, 0)
        self.host._stop_host()
        self.assertEqual(len(self.owner._voice_audio.writer.submit.call_args.args[0]), 240)
        self.host._handle(packet)
        self.owner._voice_audio.writer.submit.assert_called_once()

    def test_each_clean_round_finishes_its_diagnostic_once(self):
        self.host._start_host(1)
        self.host._stop_host()
        self.host._stop_host()
        self.owner._finish_voice_diagnostic_attempt.assert_called_once()
        self.host._handle({"event": "state", "attempt": 1, "data": "idle"})
        self.host._start_host(2)
        self.host._stop_host()
        self.assertEqual(self.owner._finish_voice_diagnostic_attempt.call_count, 2)


class VoiceChannelTests(unittest.TestCase):
    def test_lifecycle_metadata_round_trip_and_strict_fields(self):
        from ovb_rc003.chromecast_voice import lifecycle_log_text
        valid = "stopping/toggle/120/120015/time_limit"
        identity = channel.SessionIdentity.create("a" * 64)
        out, inc = channel.Channel(identity, commands=False), channel.Channel(identity, commands=False)
        inc.decode(out.encode("ready"))
        event = inc.decode(out.encode("voice", event="lifecycle", attempt=1, data=valid, time=1))
        self.assertEqual(event["data"], valid)
        self.assertIn("到时自动停止(time_limit)", lifecycle_log_text(valid))
        self.assertIn("录音经过=120.02秒", lifecycle_log_text(valid))
        for invalid in ("stopping/toggle/120/120015/arbitrary-text", "idle/other/120/0/none",
                        "idle/hold/999/0/none", "idle/hold/120/-1/none",
                        "idle/hold/120/1.2/none", "idle/hold/120/NaN/none",
                        "idle/hold/120/99999999999999/none", valid + "/extra"):
            events = channel.Channel(identity, commands=False)
            events.encode("ready")
            with self.subTest(data=invalid), self.assertRaises(channel.ChannelError):
                events.encode("voice", event="lifecycle", attempt=1, data=invalid, time=1)

    def test_detect_cannot_send_host_commands_or_arbitrary_metadata(self):
        identity = channel.SessionIdentity.create("a" * 64)
        commands = channel.Channel(identity, commands=True)
        commands.encode("start", mode="detect", voice=True)
        with self.assertRaises(channel.ChannelError):
            commands.encode("voice_host", attempt=1, result="ready")
        events = channel.Channel(identity, commands=False)
        events.encode("ready")
        events.encode("voice", event="mic", attempt=0, data="pressed", time=1)
        events.encode("voice", event="probe", attempt=0, data="layout/003c/003f", time=1)
        with self.assertRaises(channel.ChannelError):
            events.encode("voice", event="probe", attempt=0, data="raw-audio-content", time=1)

    def test_fixed_payload_round_trip(self):
        identity = channel.SessionIdentity.create("a" * 64)
        out, inc = channel.Channel(identity, commands=False), channel.Channel(identity, commands=False)
        inc.decode(out.encode("ready"))
        event = inc.decode(out.encode("voice", event="control", attempt=1, data=PHYSICAL.hex(), time=1))
        self.assertEqual(bytes.fromhex(event["data"]), PHYSICAL)
        commands = channel.Channel(identity, commands=True)
        commands.encode("start", mode="run", voice=True)
        commands.encode("voice_host", attempt=1, result="ready")

    def test_arbitrary_command_or_payload_rejected(self):
        for result in ("open_arbitrary_handle", "run", "address"):
            commands = channel.Channel(channel.SessionIdentity.create("a" * 64), commands=True)
            commands.encode("start", mode="run", voice=True)
            with self.assertRaises(channel.ChannelError):
                commands.encode("voice_host", attempt=1, result=result)
        for data in ("zz", "0", "00" * 513):
            events = channel.Channel(channel.SessionIdentity.create("a" * 64), commands=False)
            events.encode("ready")
            with self.assertRaises(channel.ChannelError):
                events.encode("voice", event="audio", attempt=1, data=data, time=1)


class WorkerVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_control_and_audio_are_explained_without_fabricating_a_start(self):
        from ovb_rc003.chromecast_voice_receiver import VoiceReceiver
        from ovb_rc003.chromecast_buttons import AttAssembler
        from tests.test_chromecast_buttons import acl
        target = SimpleNamespace(open_voice=mock.AsyncMock(), write_voice=mock.AsyncMock(),
                                 voice_attributes={0x3f: "control", 0x3c: "audio"})
        sent = []
        with mock.patch("ovb_rc003.chromecast_voice_receiver.config.load_config", return_value={}):
            receiver = VoiceReceiver(target, lambda *args: sent.append(args))
        target.write_voice.side_effect = lambda command: reply_caps(receiver, command)
        await receiver.open()
        # A 20-byte notification can be an exact, fully assembled ATT packet.
        assembler = AttAssembler()
        for attribute, value in ((0x3f, b"\x04"), (0x3f, b"\x08"), (0x3c, bytes(20)),
                                 (0x3c, bytes(160)), (0x3c, bytes(20))):
            stamp = time.monotonic()
            _, att = assembler.feed(3, acl(b"\x1b" + attribute.to_bytes(2, "little") + value), stamp)
            receiver.notification(int.from_bytes(att[1:3], "little"), att[3:], stamp)
        probes = [e[2] for e in sent if e[0] == "probe"]
        self.assertIn("control/4/1/idle/unsupported_voice_stream", probes)
        self.assertIn("start_fields/256/256/256", probes)
        self.assertIn("control/8/1/idle/awaiting_start", probes)
        self.assertIn("audio/20/idle/no_stream/ignored", probes)
        self.assertIn("audio/160/idle/no_stream/ignored", probes)
        self.assertEqual(probes.count("audio/20/idle/no_stream/ignored"), 1)
        self.assertFalse(any(e[0] in ("host_start", "audio", "control") for e in sent))
        # All emitted metadata passes the same strict IPC reader as production.
        sender = channel.Channel(channel.SessionIdentity.create("a" * 64), commands=False)
        sender.encode("ready")
        for kind, attempt, data, stamp in sent:
            sender.encode("voice", event=kind, attempt=attempt, data=data, time=stamp)
        for size in range(1, 100):
            receiver.notification(0x3c, bytes(size), time.monotonic())
        self.assertEqual(len(receiver.seen_decisions), 48)
        target.write_voice.assert_awaited_once_with(GET_CAPABILITIES)

    async def test_rejected_start_retains_header_fields_and_does_not_start_host(self):
        from ovb_rc003.chromecast_voice_receiver import VoiceReceiver
        sent = []
        with mock.patch("ovb_rc003.chromecast_voice_receiver.config.load_config", return_value={}):
            receiver = VoiceReceiver(SimpleNamespace(voice_attributes={0x3f: "control"}),
                                     lambda *args: sent.append(args))
            receiver.available = True
            receiver.notification(0x3f, b"\x04\x03\x01\x02", time.monotonic())
        self.assertIn("start_fields/3/1/2", [e[2] for e in sent])
        self.assertIn("control/4/4/idle/unsupported_voice_stream", [e[2] for e in sent])
        self.assertFalse(any(e[0] == "host_start" for e in sent))

    async def test_lifecycle_logs_stop_causes_and_freezes_elapsed_without_changing_commands(self):
        from ovb_rc003.chromecast_voice_receiver import VoiceReceiver
        from ovb_rc003.chromecast_voice import parse_lifecycle
        for mode, reason in (("hold", "released"), ("toggle", "second_press"), ("toggle", "time_limit")):
            sent = []
            with mock.patch("ovb_rc003.chromecast_voice_receiver.config.load_config",
                            return_value={"remote_recording_mode": mode}):
                receiver = VoiceReceiver(SimpleNamespace(), lambda *args: sent.append(args))
            gate = receiver.gate
            receiver.effects(gate.control(PHYSICAL, 1))
            if mode == "toggle":
                receiver.effects(gate.control(RELEASE, 1.1))
                receiver.effects(gate.host_result(1, True, 1.2))
                command = receiver.queue.popleft()
                self.assertTrue(gate.command_submitted(command))
                receiver.effects(gate.control(SOFTWARE, 1.3))
            else:
                receiver.effects(gate.host_result(1, True, 1.3))
            stop_at = 121.3 if reason == "time_limit" else 5.3
            effects = (gate.tick(stop_at) if reason == "time_limit" else
                       gate.control(RELEASE if mode == "hold" else b"\x08", stop_at))
            receiver.effects(effects)
            summaries = [parse_lifecycle(e[2]) for e in sent if e[0] == "lifecycle"]
            self.assertEqual(summaries[-1], ("stopping", mode, 120,
                                             120000 if reason == "time_limit" else 4000, reason))
            count = len(summaries)
            receiver.effects(gate.finish(stop_at + .1, "host_stop"))
            self.assertEqual(len([e for e in sent if e[0] == "lifecycle"]), count)
            # Ending cleanup does not inflate duration or overwrite the cause.
            receiver.effects(gate.control(STOP, stop_at + .2))
            receiver.effects(gate.host_stopped(1, True, stop_at + .6))
            latest = parse_lifecycle([e for e in sent if e[0] == "lifecycle"][-1][2])
            self.assertEqual(latest, ("idle", *summaries[-1][1:]))
            self.assertEqual([e for e in receiver.queue], [e for e in effects if e.kind == "device"])

    async def test_lifecycle_distinguishes_start_failure_and_cleanup_failure(self):
        from ovb_rc003.chromecast_voice_receiver import VoiceReceiver
        from ovb_rc003.chromecast_voice import parse_lifecycle
        sent = []
        with mock.patch("ovb_rc003.chromecast_voice_receiver.config.load_config", return_value={}):
            receiver = VoiceReceiver(SimpleNamespace(), lambda *args: sent.append(args))
        gate = receiver.gate
        receiver.effects(gate.control(PHYSICAL, 1))
        receiver.effects(gate.host_result(1, False, 2))
        receiver.effects(gate.tick(8))
        summaries = [parse_lifecycle(e[2]) for e in sent if e[0] == "lifecycle"]
        self.assertEqual(summaries[-2], ("stopping", "hold", 120, 0, "host_start_failed"))
        self.assertEqual(summaries[-1], ("blocked", "hold", 120, 0, "cleanup_unconfirmed"))

    async def test_broken_metadata_pipe_does_not_skip_device_close(self):
        from ovb_rc003.chromecast_voice_receiver import VoiceReceiver
        target = SimpleNamespace(open_voice=mock.AsyncMock(), write_voice=mock.AsyncMock(),
                                 voice_attributes={0x3f: "control", 0x3c: "audio"})
        with mock.patch("ovb_rc003.chromecast_voice_receiver.config.load_config", return_value={}):
            receiver = VoiceReceiver(target, mock.Mock(side_effect=OSError("pipe gone")))
            target.write_voice.side_effect = lambda command: reply_caps(receiver, command)
            await receiver.open()
            receiver.notification(0x3f, PHYSICAL, time.monotonic())
            receiver.stop()
            await receiver.tick()
            await asyncio.sleep(0)
        self.assertTrue(receiver.pipe_failed)
        self.assertEqual(target.write_voice.await_args_list,
                         [mock.call(GET_CAPABILITIES), mock.call(b"\x0d\x01")])
        await receiver.close()

    async def test_detection_is_passive_and_deduplicates_assistant(self):
        from ovb_rc003.chromecast_voice_receiver import VoiceReceiver
        target = SimpleNamespace(open_voice=mock.AsyncMock(), write_voice=mock.AsyncMock(),
                                 voice_attributes={0x3f: "control", 0x3c: "audio"})
        sent = []
        with mock.patch("ovb_rc003.chromecast_voice_receiver.config.load_config", return_value={}):
            receiver = VoiceReceiver(target, lambda *args: sent.append(args), detect_only=True)
        await receiver.open()
        for value in (PHYSICAL, b"\x08", PHYSICAL, RELEASE, PHYSICAL, b"\x08", RELEASE):
            receiver.notification(0x3f, value, time.monotonic())
        receiver.notification(0x3c, bytes(160), time.monotonic())
        receiver.host({"attempt": 1, "result": "ready"})
        receiver.stop()
        await receiver.tick()
        self.assertEqual(sum(row[0] == "mic" for row in sent), 2)
        self.assertIn("control/8/1/idle/detect_pressed", [row[2] for row in sent if row[0] == "probe"])
        self.assertFalse(any(row[0] in ("audio", "control", "host_start") for row in sent))
        target.write_voice.assert_not_called()
        self.assertTrue(receiver.hardware_stopped)

    async def test_selected_wire_to_open_close_and_clean_worker_exit(self):
        from ovb_rc003 import chromecast_worker as worker
        from ovb_rc003 import chromecast_voice_receiver as adapter
        from tests.test_chromecast_buttons import acl
        identity = channel.SessionIdentity.create("a" * 64)
        commands = channel.Channel(identity, commands=True)
        events = channel.Channel(identity, commands=False)
        inbound = deque([commands.encode("start", mode="run", voice=True)])
        packets, received, writes = [], [], []
        target = SimpleNamespace(radio="c" * 64, attribute=0x29, changed=threading.Event(),
                                 voice_attributes={0x3f: "control", 0x3c: "audio"})
        def notify(value):
            packets.append((3, acl(b"\x1b\x3f\x00" + value), time.monotonic()))
        async def opened():
            pass
        async def probe(marker):
            packets.extend([(4, acl(b"\x06\x01\x00\xff\xff\x00\x28" + marker.bytes[::-1]), time.monotonic()),
                            (3, acl(b"\x01\x06\x01\x00\x0a"), time.monotonic())])
            return True, 0
        async def voice_write(command):
            writes.append(command)
            notify(CAPS if command == GET_CAPABILITIES else SOFTWARE if command == b"\x0c\x00" else STOP)
        target.open = target.open_voice = opened
        target.probe, target.write_voice, target.close = probe, voice_write, mock.Mock()
        def write(raw):
            event = events.decode(raw)
            received.append(event)
            if event.get("event") == "available":
                notify(PHYSICAL)
            elif event.get("event") == "host_start":
                inbound.append(commands.encode("voice_host", attempt=event["attempt"], result="ready"))
                notify(RELEASE)
            elif event.get("event") == "state" and event["data"] == "recording":
                notify(b"\x08")
            elif event.get("event") == "host_stop":
                inbound.append(commands.encode("voice_host", attempt=event["attempt"], result="released"))
            elif event.get("event") == "state" and event["data"] == "idle":
                inbound.append(commands.encode("stop"))
        def poll():
            result = packets[:]
            packets.clear()
            return result
        capture = SimpleNamespace(start=mock.Mock(), poll=poll, stop=mock.Mock())
        pipe = SimpleNamespace(read=lambda: inbound.popleft() if inbound else None, write=write)
        parent = SimpleNamespace(pid=1)
        with mock.patch.object(worker, "_selected", return_value=True), \
             mock.patch.object(worker, "sensitive_logging_enabled", return_value=True), \
             mock.patch.object(worker, "SelectedDevice", return_value=target), \
             mock.patch.object(worker, "Capture", return_value=capture), \
             mock.patch.object(worker, "verify_peer"), mock.patch.object(worker, "inspect_peer"), \
             mock.patch.object(adapter.config, "load_config", return_value={"remote_recording_mode": "toggle"}):
            result = await asyncio.wait_for(worker.receive(identity, parent, pipe), 4)
        self.assertEqual(result, 0)
        self.assertEqual(writes, [GET_CAPABILITIES, b"\x0c\x00", b"\x0d\x00"])
        self.assertEqual(sum(e.get("event") == "host_start" for e in received), 1)
        journal = [e["record"] for e in received if e["type"] == "evidence"]
        controls = [r for r in journal if r["kind"] == "control"]
        self.assertGreaterEqual(len(controls), 3)
        self.assertEqual([r["n"] for r in controls], list(range(1, len(controls) + 1)))
        self.assertEqual(controls[0]["opcode"], 11)
        self.assertEqual(controls[0]["result"], "accepted_caps")
        self.assertEqual(controls[1]["result"], "accepted_start")
        self.assertTrue(journal[-1]["final"])
        self.assertEqual(received[-1]["type"], "stopped")


class DeviceVoiceLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_winrt_declarations_resolve_verified_notification_value_handles(self):
        from ovb_rc003.chromecast_device_windows import SelectedDevice
        from ovb_rc003.chromecast_pipe_windows import PipeError
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattCharacteristicProperties
        import uuid
        chars = [SimpleNamespace(uuid=uuid.UUID(f"ab5e000{part}-5a21-4f05-bc7d-af01f617b664"),
                                attribute_handle=handle,
                                characteristic_properties=GattCharacteristicProperties.WRITE)
                 for part, handle in ((2, 0x39), (3, 0x3b), (4, 0x3e))]
        service = SimpleNamespace(get_characteristics_with_cache_mode_async=mock.AsyncMock(
            return_value=SimpleNamespace(status=0, characteristics=chars)))
        device = SelectedDevice("a" * 64)
        device.device = SimpleNamespace(get_gatt_services_for_uuid_with_cache_mode_async=mock.AsyncMock(
            return_value=SimpleNamespace(status=0, services=[service])))
        await device.open_voice()
        self.assertEqual(device.voice_attributes, {0x3c: "audio", 0x3f: "control"})
        from ovb_rc003.chromecast_observation import valid_record
        self.assertTrue(valid_record(dict(kind="gatt", elapsed_ms=0, **device.voice_evidence)))
        self.assertEqual(device.voice_evidence["control_handle"], 0x3e)
        self.assertEqual(device.voice_evidence["mtu"], -1)
        chars[1].attribute_handle = 0x4b
        with self.assertRaises(PipeError):
            await device.open_voice()


class VoicePageTests(unittest.TestCase):
    def test_recording_selection_matches_saved_mode_after_busy_and_failure(self):
        from tests.test_remote_selection import _QML_PROBE
        bootstrap = _QML_PROBE.split("def click(name):")[0]
        bootstrap = bootstrap.replace("model = classes['ButtonMappingModel']()", """
settings = m.config.default_config()
settings[selection.KEY] = {'schema': 1, 'active': B, 'devices': [{'key': B, 'profile': 'chromecast-remote'}]}
settings['remote_recording_mode'] = 'toggle'
settings['voice_program']['provider'] = 'doubao_ime'
m.config.save_config(m.config.config_path(), settings)
model = classes['ButtonMappingModel']()
""")
        probe = bootstrap + r'''
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
item('voicePageLoader').setProperty('active', True)
assert QMetaObject.invokeMethod(item('voiceTabButton'), 'pressed')
for _ in range(12):
    app.processEvents()
    window.grabWindow()
mode, limit = item('remoteRecordingModeCombo'), item('remoteRecordingLimitCombo')
engine.globalObject().setProperty('modeProbe', engine.newQObject(mode))
engine.globalObject().setProperty('limitProbe', engine.newQObject(limit))
def js(code):
    result = engine.evaluate(code)
    assert not result.isError(), result.toString()
    app.processEvents()
def check(expected_mode, expected_limit=1):
    saved = m.config.load_config(m.config.config_path())
    assert controller.remoteRecordingModeIndex == expected_mode
    assert mode.property('currentIndex') == expected_mode
    assert limit.property('currentIndex') == expected_limit
    assert saved['remote_recording_mode'] == ('toggle' if expected_mode else 'hold')
    assert saved['remote_recording_limit_seconds'] == (60, 120, 180, 300, 600)[expected_limit]
check(1)
# A shortcut read must disable both selectors, including an already-open popup.
js('modeProbe.popup.open()')
controller._set_voice_hotkey_busy(True)
app.processEvents()
assert not mode.property('enabled')
assert not limit.property('enabled')
# Deliver a selection that was already in flight when the blocker appeared.
js('modeProbe.currentIndex = 0; modeProbe.activated(0); modeProbe.popup.close()')
check(1)
assert controller.errorMessage
controller._set_voice_hotkey_busy(False)
app.processEvents()
assert mode.property('enabled')
# Exercise actual ComboBox key handling, not just the controller setter.
mode.forceActiveFocus()
QTest.keyClick(window, Qt.Key.Key_Up)
app.processEvents()
check(0)
QTest.keyClick(window, Qt.Key.Key_Down)
app.processEvents()
check(1)
# Save failure must restore both selectors and leave their bindings live.
with mock.patch.object(m.config, 'save_config_and_load', side_effect=OSError('test disk failure')):
    js('modeProbe.currentIndex = 0; modeProbe.activated(0)')
    check(1)
    assert '保存失败' in controller.errorMessage
    js('limitProbe.currentIndex = 0; limitProbe.activated(0)')
    check(1)
    assert '保存失败' in controller.errorMessage
js('limitProbe.currentIndex = 0; limitProbe.activated(0)')
check(1, 0)
controller.setRemoteRecordingPreferences(0, 1)
app.processEvents()
check(0)
controller.setRemoteRecordingPreferences(1, 1)
app.processEvents()
check(1)
restored = classes['SettingsController'](classes['ButtonMappingModel'](), background_task_runner=lambda target, name: target())
assert restored.remoteRecordingModeIndex == 1
assert restored.remoteRecordingLimitIndex == 1
restored.shutdownBackgroundTasks()
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
assert not warnings, warnings
'''
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, LOCALAPPDATA=directory, QT_QPA_PLATFORM='offscreen',
                       RC003_DISABLE_LIVE_INPUT='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
            env.pop('TEST_ISOLATED_ROOT', None)
            result = subprocess.run([sys.executable, '-c', probe], env=env, capture_output=True,
                                    text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_compact_row_and_detection_callback_in_production_shell(self):
        self._probe_provider('wetype')

    def test_sogou_mode_fields_in_production_shell(self):
        self._probe_provider('sogou')

    def _probe_provider(self, provider):
        # Reuse only the shell bootstrap, not the independently changing device
        # chooser journey. Persist a synthetic selected entity in a temp root.
        from tests.test_remote_selection import _QML_PROBE
        bootstrap = _QML_PROBE.split("def click(name):")[0]
        bootstrap = bootstrap.replace("model = classes['ButtonMappingModel']()", """
settings = m.config.default_config()
settings[selection.KEY] = {'schema': 1, 'active': B, 'devices': [{'key': B, 'profile': 'chromecast-remote'}]}
settings['voice_program']['provider'] = 'wetype'
m.config.save_config(m.config.config_path(), settings)
model = classes['ButtonMappingModel']()
""")
        bootstrap = bootstrap.replace("['provider'] = 'wetype'", "['provider'] = " + repr(provider))
        probe = bootstrap + r'''
def settle():
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        window.grabWindow()
        if not controller.voiceHotkeyBusy:
            return
        time.sleep(.01)
    assert not controller.voiceHotkeyBusy
item('voicePageLoader').setProperty('active', True)
assert QMetaObject.invokeMethod(item('voiceTabButton'), 'pressed')
for _ in range(12):
    app.processEvents()
    window.grabWindow()
assert controller.activeRemoteProfile == 'chromecast-remote'
assert not item('voicePageLoader').property('item').property('voiceProgramUnsupported')
assert item('voiceHotkeyRow').property('visible')
assert item('refreshVoiceHotkeyButton').property('visible')
assert item('recordVoiceHotkeyButton').property('visible')
assert not item('voiceProgramAutoStartRow').property('visible')
row = item('remoteRecordingModeRow')
program = item('voiceProgramSection')
selection = item('voiceProgramSelectionRow')
assert row.parentItem() == selection.parentItem()
assert selection.y() + selection.height() <= row.y()
assert row.property('titleText') == '按键模式'
assert item('remoteRecordingModeRow_titleLabel').property('font').weight() == 500
assert item('voiceProgramSelectionRow_titleLabel').property('font').weight() == 500
controller.setRemoteRecordingPreferences(1, 1)
settle()
app.processEvents()
assert item('remoteRecordingLimitCombo').property('visible')
assert controller.remoteRecordingLimitIndex == 1
assert row.property('descriptionText') == '按一下开始，再按结束；到时停止遥控音频'
assert controller.holdVoiceHotkeyText == ''  # Never reuse Ctrl+Win as a toggle.
from ovb_rc003 import key_mapping
held_shortcut = m.config.voice_hotkey_for_provider(controller._config, 'wetype')
controller._set_voice_hotkey_text(key_mapping.VoiceTriggerMode.HOLD, 'lshift+f9')
assert controller._persist_voice_settings(hotkey_source='manual')
assert controller.holdVoiceHotkeyText == 'lshift+f9'
assert controller.voiceHotkeySource == 'manual'
assert m.config.voice_hotkey_for_provider(controller._config, 'wetype') == held_shortcut
saved = m.config.load_config(m.config.config_path())
assert m.config.voice_hotkey_for_provider(saved, 'wetype', trigger='toggle') == 'lshift+f9'
restored = classes['SettingsController'](classes['ButtonMappingModel'](), background_task_runner=lambda target, name: target())
assert restored.holdVoiceHotkeyText == 'lshift+f9'
restored.shutdownBackgroundTasks()
for _ in range(8):
    app.processEvents()
    window.grabWindow()
mode, limit = item('remoteRecordingModeCombo'), item('remoteRecordingLimitCombo')
assert mode.parentItem() == limit.parentItem()
assert abs(mode.y() - limit.y()) < 1
assert 0 < limit.x() - mode.x() - mode.width() <= 16
assert 50 <= mode.width() < 92, (window.width(), mode.width(), limit.width())
assert 60 <= limit.width() < 92, (window.width(), mode.width(), limit.width())
editor = item('remoteRecordingModeRow_editorColumn')
assert abs(editor.width() - mode.width() - limit.width() - (limit.x() - mode.x() - mode.width())) < 1
controller.setRemoteRecordingPreferences(0, 1)
settle()
app.processEvents()
assert controller.holdVoiceHotkeyText == held_shortcut
assert not limit.property('visible')
assert row.property('descriptionText') == '按住说话，松开结束'
for _ in range(8):
    app.processEvents()
    window.grabWindow()
assert abs(editor.width() - mode.width()) < 1
controller.setRemoteRecordingPreferences(1, 1)
settle()
assert controller.holdVoiceHotkeyText == 'lshift+f9'
controller.setRemoteRecordingPreferences(0, 1)
settle()
from ovb_rc003 import chromecast_client
from types import SimpleNamespace
import threading
listener = SimpleNamespace(start=mock.Mock(), stop=mock.Mock())
pressed = []
controller._rawKeyDetected.connect(lambda key, extra: pressed.append(key))
with mock.patch.object(chromecast_client, 'Client', return_value=listener) as factory:
    result = controller._start_key_detection_worker(threading.Event(), bridge_running=False, physical_bindings={})
    assert result.ok and result.listener is listener, result
    factory.call_args.kwargs['on_voice']({'event': 'mic', 'data': 'pressed'})
    factory.call_args.kwargs['on_voice']({'event': 'available', 'data': 'ready'})
assert pressed == ['mic'], pressed
controller._stop_input_resources('key_detection', listener=listener)
if controller._voice_program_settings['provider'] == 'sogou':
    reads = []
    def read_mode(provider, **kw):
        trigger = kw.get('trigger', 'hold')
        reads.append(trigger)
        return m.voice_hotkey_sync_windows.VoiceHotkeySyncResult(
            provider, True, 'read', 'lshift+f8' if trigger == 'toggle' else 'lctrl+lshift+f8')
    m.voice_hotkey_sync_windows.read_provider_hotkey = read_mode
    # A manual hold must not prevent automatic reading of the separate toggle.
    m.config.set_voice_hotkey_for_provider(controller._config, 'sogou', 'lctrl+lshift+f8', source='manual')
    m.config.set_voice_hotkey_for_provider(controller._config, 'sogou', '', source='default', trigger='toggle')
    controller._config = m.config.save_config_and_load(m.config.config_path(), controller._config)
    controller.setRemoteRecordingPreferences(1, 1)
    settle()
    assert reads == ['toggle'], reads
    assert controller.holdVoiceHotkeyText == 'lshift+f8'
    assert '开关型' in item('voiceHotkeyRow').property('descriptionText')
    controller.setRemoteRecordingPreferences(1, 2)
    settle()
    assert reads == ['toggle']  # Time-only changes do not reread.
    controller.setRemoteRecordingPreferences(0, 1)
    settle()
    assert reads == ['toggle']  # Preserve manual hold.
    controller.refreshVoiceHotkeyFromProvider()
    settle()
    assert reads == ['toggle', 'hold']
    assert controller.holdVoiceHotkeyText == 'lctrl+lshift+f8'
    controller.setRemoteRecordingPreferences(1, 1)
    settle()
    controller._set_voice_hotkey_text(key_mapping.VoiceTriggerMode.HOLD, 'lshift+f9')
    assert controller._persist_voice_settings(hotkey_source='manual')
    reads.clear()
    controller.loadVoiceHotkeyFromProvider()
    settle()
    assert not reads  # Auto hold must not overwrite manual toggle.
    assert controller.holdVoiceHotkeyText == 'lshift+f9'
    controller.refreshVoiceHotkeyFromProvider()
    settle()
    assert reads == ['toggle'] and controller.holdVoiceHotkeyText == 'lshift+f8'
    m.voice_hotkey_sync_windows.read_provider_hotkey = lambda provider, **kw: m.voice_hotkey_sync_windows.VoiceHotkeySyncResult(provider, False, 'read_failed')
    controller.refreshVoiceHotkeyFromProvider()
    settle()
    assert controller.holdVoiceHotkeyText == 'lshift+f8'  # Failed refresh preserves value.
if os.environ.get('VOICE_ROW_SCREENSHOT'):
    controller.setRemoteRecordingPreferences(1, 1)
    settle()
    engine.globalObject().setProperty('voiceScrollProbe', engine.newQObject(item('voiceScroll')))
    engine.evaluate('voiceScrollProbe.contentItem.contentY = ' + str(max(0, program.y() - 65)))
    for _ in range(12):
        app.processEvents()
        window.grabWindow()
    assert window.grabWindow().save(os.environ['VOICE_ROW_SCREENSHOT'])
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
assert not warnings, warnings
'''
        probe = probe.replace("'wetype'", repr(provider))
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, LOCALAPPDATA=directory, QT_QPA_PLATFORM="offscreen",
                       RC003_DISABLE_LIVE_INPUT="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            env.pop("TEST_ISOLATED_ROOT", None)
            result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                                    text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
