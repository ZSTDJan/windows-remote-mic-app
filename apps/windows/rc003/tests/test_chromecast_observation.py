"""Synthetic diagnosis path only; no Bluetooth, registry writes or speech."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ovb_rc003.chromecast_observation import Observation, valid_record
from ovb_rc003.chromecast_channel import Channel, ChannelError, SessionIdentity
from ovb_rc003.chromecast_buttons import ButtonReceiver
from ovb_rc003.chromecast_voice_receiver import VoiceReceiver
from tests.test_chromecast_buttons import acl, notification, ENTITY, GENERATION, RADIO


class ObservationTests(unittest.TestCase):
    def make(self):
        rows = []
        observation = Observation(rows.append, clock=lambda: 10)
        return observation, rows

    def test_repeated_control_order_and_safe_headers_survive(self):
        obs, rows = self.make()
        for value in (b'\x08', b'\x04\x03\x02\x01', b'\x00\x02', b'\x08',
                      b'\x0b\x01\x00\x02\x03\x00\x78\x00\x00', b'\x0aPRIVATE', b'\xffSECRET'):
            obs.control(value, 10.01, 'idle', 'unknown_control')
        obs.flush(final=True)
        self.assertEqual([r['opcode'] for r in rows[:-1]], [8, 4, 0, 8, 11, 10, 255])
        self.assertEqual([r['n'] for r in rows[:-1]], list(range(1, 8)))
        self.assertEqual(rows[4]['fields'], [1, 0, 2, 3, 0, 120, 0, 0])
        self.assertEqual(rows[5]['fields'], [])
        self.assertEqual(rows[6]['fields'], [])
        self.assertTrue(all(valid_record(r) for r in rows))
        self.assertNotIn('PRIVATE', str(rows))
        self.assertNotIn('SECRET', str(rows))

    def test_backlog_and_final_truncation_are_explicit_and_bounded(self):
        obs, rows = self.make()
        for _ in range(300):
            obs.control(b'\x08', 10, 'idle', 'awaiting_start')
        self.assertEqual(len(obs.rows), 256)
        obs.flush(final=True)
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[-1]['counts']['control_rows_lost'], 292)
        self.assertEqual(rows[0]['n'], 45)

    def test_zero_summary_and_failed_sink_do_not_change_receive_behavior(self):
        obs, rows = self.make()
        obs.flush(final=True)
        self.assertEqual(rows[0]['counts']['controls'], 0)
        obs.send = lambda row: (_ for _ in ()).throw(OSError())
        obs.stage('source_ready')
        self.assertEqual(obs.counts['send_failed'], 1)

    def test_ipc_allows_evidence_before_ready_but_never_as_command_or_audio(self):
        obs, rows = self.make()
        obs.stage('run')
        obs.flush(final=True)
        identity = SessionIdentity.create(ENTITY)
        sender, reader = Channel(identity, commands=False), Channel(identity, commands=False)
        for row in rows:
            self.assertEqual(reader.decode(sender.encode('evidence', record=row))['record'], row)
        with self.assertRaises(ChannelError):
            Channel(identity, commands=True).encode('evidence', record=rows[0])
        obs.control(b'\x0aSECRET', 10, 'idle', 'unknown_control')
        bad = dict(obs.rows[0], fields=[1])
        self.assertFalse(valid_record(bad))
        for bad in (dict(rows[0], audio='raw'), dict(rows[0], stage='arbitrary-text')):
            with self.assertRaises(ChannelError):
                sender.encode('evidence', record=bad)

    def test_unproven_notifications_only_count_then_selected_notifications_are_explained(self):
        obs, rows = self.make()
        receiver = ButtonReceiver(ENTITY, GENERATION, RADIO, 10, observation=obs)
        receiver.feed(RADIO, GENERATION, 3, acl(b'\x1b\x3f\x00\x04\x03\x02\x01'), 10.01)
        self.assertEqual(obs.counts['preproof_notify'], 1)
        self.assertFalse(obs.rows)
        receiver.feed(RADIO, GENERATION, 4, acl(receiver._request), 10.02)
        receiver.feed(RADIO, GENERATION, 3, acl(b'\x01\x06\x01\x00\x0a'), 10.03)
        receiver.api_result(ENTITY, GENERATION, success=True, services=0, input_attribute=0x29, now=10.04)
        receiver.feed(RADIO, GENERATION, 3, acl(b'\x1b\x3f\x00\x08'), 10.05)
        receiver.feed(RADIO, GENERATION, 3, acl(b'\x1b\x3c\x00' + bytes(20)), 10.06)
        receiver.feed(RADIO, GENERATION, 3, acl(b'\x1b\x3f\x00\x08', handle=0x32), 10.07)
        self.assertEqual(obs.counts['controls'], 1)
        self.assertEqual(obs.counts['audio_bytes'], 20)
        self.assertEqual(obs.counts['other_connection'], 1)
        self.assertEqual(obs.counts['no_handler'], 2)
        self.assertEqual(obs.rows[0]['result'], 'no_handler')

    def test_not_ready_and_normal_repeated_controls_record_each_decision_without_audio(self):
        obs, rows = self.make()
        with patch('ovb_rc003.chromecast_voice_receiver.config.load_config', return_value={}):
            receiver = VoiceReceiver(SimpleNamespace(voice_attributes={0x3f:'control',0x3c:'audio'}),
                                     lambda *args: None, observation=obs)
            receiver.notification(0x3f, b'\x04\x03\x02\x01', time.monotonic())
            receiver.available = True
            receiver.notification(0x3f, b'\x08', time.monotonic())
            receiver.notification(0x3f, b'\x08', time.monotonic())
            receiver.notification(0x3c, b'PRIVATE-AUDIO', time.monotonic())
        obs.flush(final=True)
        self.assertEqual([r['result'] for r in rows[:-1]], ['not_available', 'awaiting_start', 'awaiting_start'])
        self.assertEqual(obs.counts['ignored_audio'], 1)
        self.assertEqual(receiver.gate.state, 'idle')
        self.assertNotIn('PRIVATE', str(rows))


if __name__ == '__main__':
    unittest.main()
