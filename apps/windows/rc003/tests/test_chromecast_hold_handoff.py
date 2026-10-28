"""A new physical hold must survive successful cleanup, never outlive release."""
import unittest
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest import mock

from ovb_rc003.chromecast_voice import Recording, Effect, STOP_TIMEOUT
from ovb_rc003.chromecast_voice_receiver import VoiceReceiver
from tests import test_chromecast_voice as voice_tests


START = bytes((4, 3, 2, 1))
NEXT = bytes((4, 3, 2, 2))
RELEASE = bytes((0, 2))
STOP = bytes((0, 0))
SYNC = bytes((10, 0, 0, 0, 0, 0, 0))


class HoldHandoffTests(unittest.TestCase):
    def ending_hold(self):
        gate = Recording("hold")
        gate.control(START, 1)
        gate.host_result(1, True, 1.118)
        gate.control(RELEASE, 2.245)
        return gate

    def test_log_sequence_waits_for_cleanup_then_starts_once(self):
        gate = self.ending_hold()
        self.assertEqual(gate.control(NEXT, 2.245), [])
        self.assertEqual(gate.state, "stopping")
        self.assertEqual(gate.attempt, 1)
        self.assertEqual(gate.tick(2.6), [])
        effects = gate.host_stopped(1, True, 2.614)
        self.assertEqual([(e.kind, e.attempt, e.value) for e in effects], [
            ("state", 1, "idle"), ("host_start", 2, None),
            ("state", 2, "starting"), ("control", 2, NEXT)])
        self.assertEqual(gate.control(NEXT, 2.62), [])
        gate.host_result(2, True, 2.7)
        self.assertEqual(gate.state, "recording")
        self.assertEqual(gate.attempt, 2)
        gate.control(RELEASE, 3)
        gate.host_stopped(2, True, 3.4)
        self.assertEqual(gate.state, "idle")

    def test_new_audio_and_sync_wait_for_the_new_decoder_only(self):
        gate = self.ending_hold()
        gate.audio(b"old", 2.25)  # Still old until a new physical START proves it.
        gate.control(NEXT, 2.26)
        self.assertEqual(gate.control(SYNC, 2.27), [])
        self.assertEqual(gate.audio(b"first", 2.28), [])
        self.assertEqual(gate.audio(b"second", 2.58), [])
        effects = gate.host_stopped(1, True, 2.6)
        self.assertEqual([(e.kind, e.value) for e in effects[3:]], [
            ("control", NEXT), ("control", SYNC),
            ("audio", b"first"), ("audio", b"second")])
        self.assertTrue(all(e.attempt == 2 for e in effects[1:]))
        self.assertEqual(gate.last_audio, 2.58)

    def test_quiet_period_still_applies_even_when_host_releases_early(self):
        gate = self.ending_hold()
        gate.control(NEXT, 2.25)
        self.assertEqual(gate.host_stopped(1, True, 2.26), [])
        self.assertEqual(gate.tick(2.54), [])
        self.assertTrue(any(e.kind == "host_start" for e in gate.tick(2.55)))

    def test_release_or_device_stop_before_cleanup_cancels_new_hold(self):
        for stop in (RELEASE, STOP):
            with self.subTest(stop=stop):
                gate = self.ending_hold()
                gate.control(NEXT, 2.25)
                gate.audio(b"not to replay", 2.26)
                gate.control(stop, 2.27)
                effects = gate.host_stopped(1, True, 2.7) + gate.tick(10)
                self.assertEqual(gate.state, "idle")
                self.assertEqual(gate.attempt, 1)
                self.assertFalse(any(e.kind in ("host_start", "audio") for e in effects))


    def test_cleanup_failure_does_not_start_later_when_release_recovers(self):
        gate = self.ending_hold()
        gate.control(NEXT, 2.25)
        effects = gate.host_stopped(1, False, 2.3)
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x02"])
        gate.control(STOP, 2.4)
        effects += gate.host_stopped(1, True, 2.8)
        self.assertFalse(any(e.kind == "host_start" for e in effects))
        self.assertEqual(gate.attempt, 1)

    def test_cleanup_timeout_closes_pending_stream_and_never_restarts(self):
        gate = self.ending_hold()
        gate.control(NEXT, 2.25)
        effects = gate.tick(2.245 + STOP_TIMEOUT)
        self.assertEqual(gate.state, "blocked")
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x02"])
        gate.control(STOP, 7.3)
        effects += gate.host_stopped(1, True, 7.7)
        self.assertFalse(any(e.kind == "host_start" for e in effects))

    def test_shutdown_host_cancel_and_invalid_control_discard_pending_hold(self):
        for action in ("shutdown", "host_cancel", "invalid"):
            with self.subTest(action=action):
                gate = self.ending_hold()
                gate.control(NEXT, 2.25)
                gate.audio(b"private", 2.26)
                effects = (gate.control(b"\x00\xff", 2.3) if action == "invalid"
                           else gate.finish(2.3, "host_stop", shutdown=action == "shutdown"))
                self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x02"])
                gate.control(STOP, 2.4)
                effects += gate.host_stopped(1, True, 2.8)
                self.assertFalse(any(e.kind in ("host_start", "audio") for e in effects))

    def test_failed_start_and_toggle_cleanup_keep_original_close_policy(self):
        for mode, reason in (("hold", "host_start_failed"), ("toggle", "second_press")):
            with self.subTest(mode=mode, reason=reason):
                gate = Recording(mode)
                gate.control(START, 1)
                gate.finish(2, reason)
                gate.control(STOP, 2.1)
                effects = gate.control(NEXT, 2.2)
                self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x02"])
                gate.control(STOP, 2.3)
                effects += gate.host_stopped(1, True, 2.7)
                self.assertFalse(any(e.kind == "host_start" for e in effects))

    def test_newer_physical_segment_replaces_only_pending_segment(self):
        gate = self.ending_hold()
        gate.control(NEXT, 2.25)
        gate.audio(b"superseded", 2.26)
        newest = bytes((4, 3, 2, 3))
        gate.control(newest, 2.27)
        gate.audio(b"current", 2.28)
        effects = gate.host_stopped(1, True, 2.7)
        self.assertEqual([e.value for e in effects if e.kind == "control"], [newest])
        self.assertEqual([e.value for e in effects if e.kind == "audio"], [b"current"])

    def test_old_host_replies_and_old_commands_cannot_stop_new_attempt(self):
        gate = self.ending_hold()
        gate.control(NEXT, 2.25)
        gate.host_stopped(1, True, 2.7)
        self.assertEqual(gate.host_result(1, False, 2.8), [])
        self.assertEqual(gate.host_stopped(1, False, 2.9), [])
        stale = Effect("device", 1, b"\x0d\x02")
        self.assertFalse(gate.command_submitted(stale))
        self.assertEqual(gate.command_result(stale, False, 3), [])
        gate.host_result(2, True, 3.1)
        self.assertEqual(gate.state, "recording")

    def test_pending_audio_is_bounded_and_overflow_cancels_restart(self):
        for packet in (bytes(512), b"x"):
            with self.subTest(size=len(packet)):
                gate = self.ending_hold()
                gate.control(NEXT, 2.25)
                effects = []
                for index in range(300):
                    effects += gate.audio(packet, 2.26 + index / 1000)
                self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x02"])
                gate.control(STOP, 2.6)
                effects += gate.host_stopped(1, True, 3)
                self.assertFalse(any(e.kind in ("host_start", "audio") for e in effects))

    def test_cleanup_success_after_deadline_cannot_revive_waiting_press(self):
        gate = self.ending_hold()
        gate.control(NEXT, 2.25)
        effects = gate.host_stopped(1, True, 7.3)
        self.assertEqual(gate.state, "blocked")
        self.assertFalse(any(e.kind == "host_start" for e in effects))
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x02"])

    def test_software_open_is_never_promoted_to_a_physical_press(self):
        gate = self.ending_hold()
        gate.control(NEXT, 2.25)
        effects = gate.control(bytes((4, 0, 2, 0)), 2.26)
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x00"])
        gate.control(STOP, 2.3)
        effects += gate.host_stopped(1, True, 2.7)
        self.assertFalse(any(e.kind == "host_start" for e in effects))

    def test_repeated_short_presses_do_not_extend_cleanup_deadline(self):
        gate = self.ending_hold()
        for index in range(4):
            gate.control(NEXT, 2.3 + index)
            gate.control(RELEASE, 2.4 + index)
        gate.control(START, 6.4)  # Reused physical stream IDs remain supported.
        effects = gate.host_stopped(1, True, 7.3)
        self.assertEqual(gate.state, "blocked")
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x01"])

    def test_failed_new_host_closes_only_new_stream_and_is_not_retried(self):
        gate = self.ending_hold()
        gate.control(NEXT, 2.25)
        gate.host_stopped(1, True, 2.7)
        effects = gate.host_result(2, False, 2.8)
        self.assertEqual([e.value for e in effects if e.kind == "device"], [b"\x0d\x02"])
        gate.control(STOP, 2.9)
        effects += gate.host_stopped(2, True, 3.3)
        self.assertEqual(gate.state, "idle")
        self.assertEqual(gate.attempt, 2)
        self.assertFalse(any(e.kind == "host_start" for e in effects))

class HoldHandoffIntegrationTests(unittest.TestCase):
    """Real receiver, recording policy, host and decoder; only external I/O is fake."""
    def setUp(self):
        voice_tests.HostTests.setUp(self)
        self.now = 1.0
        self.replies = deque()
        self.delivered = []
        patcher = mock.patch("ovb_rc003.chromecast_voice_receiver.time",
                             SimpleNamespace(monotonic=lambda: self.now))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("ovb_rc003.chromecast_voice_receiver.config.load_config",
                             return_value={"remote_recording_mode": "hold"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device = SimpleNamespace(voice_attributes={0x3f: "control", 0x3c: "audio"},
                                      write_voice=mock.AsyncMock())
        self.receiver = self.make_receiver()
        self.host.client.voice_host.side_effect = lambda attempt, result: self.replies.append(
            {"attempt": attempt, "result": result})
        self.writers = []

        def open_output():
            writer = mock.Mock()
            writer.submit.return_value = True
            self.writers.append(writer)
            self.owner._voice_audio.writer = writer
            self.owner._voice_audio.sink = mock.Mock()
            self.owner._voice_shortcut.runtime_mic_confirmed = None
            return True

        self.owner._voice_audio.open.side_effect = open_output

    def make_receiver(self):
        def send(kind, attempt, data, stamp):
            event = {"event": kind, "attempt": attempt, "data": data}
            self.host.enqueue(event)
            self.delivered.append(event)
        receiver = VoiceReceiver(self.device, send, direct_notifications=True)
        receiver.available = True
        receiver.capabilities = voice_tests.CAPS
        return receiver

    def notify(self, data, when, kind="control"):
        self.now = when
        self.receiver.notification(0x3f if kind == "control" else 0x3c, data, when)

    def pump(self):
        while not self.host.events.empty() or self.replies:
            while not self.host.events.empty():
                self.host._handle(self.host.events.get_nowait())
            while self.replies:
                self.receiver.host(self.replies.popleft())

    def first_recording(self):
        self.notify(START, 1)
        self.pump()
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.pump()
        self.assertEqual(self.receiver.gate.state, "recording")
        self.notify(RELEASE, 2.245)
        self.notify(NEXT, 2.245)

    def test_two_rounds_forward_new_audio_only_after_old_host_closes(self):
        self.first_recording()
        # Two actual 160-byte ADPCM frames split across the cleanup boundary.
        self.notify(bytes([0x11]) * 160, 2.3, "audio")
        self.assertEqual(self.owner._voice_audio.open.call_count, 1)
        self.now = 2.614
        self.pump()
        self.assertEqual(self.receiver.gate.attempt, 2)
        self.assertEqual(self.host.attempt, 2)
        self.assertEqual(self.owner._voice_audio.open.call_count, 2)
        self.assertGreater(self.host.pending_samples, 0)
        self.writers[0].submit.assert_not_called()
        self.writers[1].submit.assert_not_called()
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.pump()
        self.notify(bytes([0x22]) * 160, 2.8, "audio")
        self.pump()
        self.assertEqual(self.receiver.gate.state, "recording")
        self.notify(RELEASE, 3)
        self.pump()
        self.now = 3.4
        asyncio.run(self.receiver.tick())
        self.pump()
        self.assertEqual(self.receiver.gate.state, "idle")
        self.assertEqual(self.host.state, "idle")
        self.assertTrue(self.host.closed)
        self.assertFalse(self.owner._voice_shortcut.controller.active)
        self.assertTrue(self.writers[1].submit.called)
        self.device.write_voice.assert_not_called()
        starts = [e["attempt"] for e in self.delivered if e["event"] == "host_start"]
        self.assertEqual(starts, [1, 2])
        old_idle = next(e for e in self.delivered if e["event"] == "lifecycle"
                        and e["attempt"] == 1 and e["data"].startswith("idle/"))
        self.assertEqual(old_idle["data"], "idle/hold/120/1245/released")

    def test_release_before_host_cleanup_never_emits_second_hotkey(self):
        self.first_recording()
        self.notify(bytes(160), 2.26, "audio")
        self.notify(RELEASE, 2.27)
        self.now = 2.7
        self.pump()
        self.assertEqual(self.receiver.gate.state, "idle")
        self.assertEqual(self.host.attempt, 1)
        self.assertEqual(self.owner._voice_audio.open.call_count, 1)
        self.assertTrue(self.host.closed)

    def test_receiver_stop_cancels_pending_start_and_new_connection_is_fresh(self):
        self.first_recording()
        self.notify(bytes(160), 2.26, "audio")
        self.now = 2.27
        self.receiver.stop()
        self.pump()
        self.assertFalse(self.receiver.hardware_stopped)
        self.assertEqual([e.value for e in self.receiver.queue], [b"\x0d\x02"])
        self.notify(STOP, 2.3)
        self.now = 2.7
        asyncio.run(self.receiver.tick())
        self.pump()
        self.assertTrue(self.receiver.hardware_stopped)
        self.assertTrue(self.host.closed)
        self.assertEqual(self.host.attempt, 1)
        asyncio.run(self.receiver.close())
        fresh = self.make_receiver()
        self.assertEqual(fresh.gate.state, "idle")
        self.assertEqual(fresh.gate.attempt, 0)
        self.now = 3
        fresh.notification(0x3c, bytes(160), 3)
        self.assertEqual(fresh.gate.attempt, 0)
        fresh.notification(0x3f, START, 3)
        self.assertEqual(fresh.gate.state, "starting")
        self.assertEqual(fresh.gate.attempt, 1)

    def test_release_after_handoff_but_before_queued_host_start_cancels_hotkey(self):
        self.first_recording()
        self.now = 2.3
        self.pump()  # Old host released; original quiet window has not expired.
        self.now = 2.6
        asyncio.run(self.receiver.tick())
        self.assertEqual(self.receiver.gate.attempt, 2)
        self.notify(RELEASE, 2.61)
        self.pump()
        self.assertEqual(self.owner._voice_audio.open.call_count, 1)
        self.assertTrue(self.host.closed)
        self.assertFalse(self.owner._voice_shortcut.controller.active)

    def test_exit_after_handoff_but_before_host_start_never_opens_output(self):
        self.first_recording()
        self.now = 2.3
        self.pump()
        self.now = 2.6
        asyncio.run(self.receiver.tick())
        self.receiver.stop()
        self.host.request_stop()
        self.pump()
        self.assertEqual(self.owner._voice_audio.open.call_count, 1)
        self.assertTrue(self.host.closed)

    def test_maximum_buffer_fits_real_host_queue_without_cancelling_receiver(self):
        from ovb_rc003.chromecast_voice import _HOLD_HANDOFF_PACKETS
        self.first_recording()
        for index in range(_HOLD_HANDOFF_PACKETS):
            self.notify(b"\x11", 2.3 + index / 1000, "audio")
        self.now = 2.7
        self.pump()
        self.assertFalse(self.host.stopping.is_set())
        self.host.client.cancel.set.assert_not_called()
        self.assertEqual(self.host.attempt, 2)
        self.assertEqual(self.receiver.gate.state, "starting")
        self.notify(RELEASE, 3)
        self.pump()

    def test_buffered_audio_is_counted_once_and_overflow_is_not_audio_acceptance(self):
        from ovb_rc003.chromecast_voice import _HOLD_HANDOFF_PACKETS
        self.first_recording()
        observation = self.receiver.observation = mock.Mock()
        for index in range(_HOLD_HANDOFF_PACKETS):
            self.notify(b"\x11", 2.3 + index / 1000, "audio")
        self.assertEqual(observation.count.call_args_list,
                         [mock.call("accepted_audio")] * _HOLD_HANDOFF_PACKETS)
        self.notify(b"\x11", 2.5, "audio")
        self.assertEqual(observation.count.call_args_list[-1], mock.call("ignored_audio"))
        self.assertEqual([e.value for e in self.receiver.queue], [b"\x0d\x02"])
        self.notify(STOP, 2.6)
        self.now = 3
        self.pump()
        self.assertEqual(self.host.attempt, 1)
