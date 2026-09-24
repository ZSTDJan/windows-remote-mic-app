import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import config, remote_selection, chromecast_channel as channel
from ovb_rc003 import chromecast_wetype_toggle as pulse
from ovb_rc003 import voice_hotkey_sync_windows as sync
from ovb_rc003.chromecast_voice import Recording
from ovb_rc003 import wetype_settings_hotkey_windows as reader
import tests.test_chromecast_voice as fixtures
from tests.test_wetype_settings_hotkey_windows import page
from tests.test_chromecast_host_activity import session


class PulseTests(unittest.TestCase):
    def test_prepare_then_single_tap_and_cleanup_never_taps(self):
        events = []
        with (mock.patch.object(pulse.wetype, '_run_on_sta_thread', side_effect=lambda fn: events.append('prepare')),
                mock.patch.object(pulse.win32_input, 'send_voice_key_combo_tap', side_effect=lambda keys: events.append(keys)),
                mock.patch.object(pulse.win32_input, 'send_voice_key_combo_up') as up):
            control = pulse.ToggleShortcut('lshift+f9')
            control.send(lambda: False)
            self.assertTrue(control.cleanup())
            self.assertEqual(events, ['prepare', ('lshift', 'f9')])
            up.assert_not_called()

    def test_cancel_after_preparation_sends_nothing(self):
        with mock.patch.object(pulse.wetype, '_run_on_sta_thread'), mock.patch.object(pulse.win32_input, 'send_voice_key_combo_tap') as tap:
            with self.assertRaises(OSError):
                pulse.ToggleShortcut('lshift+f9').send(mock.Mock(side_effect=[False, True]))
            tap.assert_not_called()

    def test_capture_baseline_is_taken_after_preparation_before_tap(self):
        events = []
        control = pulse.ToggleShortcut('lshift+f9', prepare=lambda: events.append('prepare'))
        with mock.patch.object(
                pulse.win32_input,
                'send_voice_key_combo_tap',
                side_effect=lambda keys: events.append(('tap', keys))):
            control.send(lambda: False, before_send=lambda: events.append('baseline'))
        self.assertEqual(events, ['prepare', 'baseline', ('tap', ('lshift', 'f9'))])

    def test_cancel_after_capture_baseline_sends_nothing(self):
        with mock.patch.object(pulse.win32_input, 'send_voice_key_combo_tap') as tap:
            with self.assertRaises(OSError):
                pulse.ToggleShortcut('lshift+f9', prepare=lambda: None).send(
                    mock.Mock(side_effect=[False, False, True]),
                    before_send=lambda: None,
                )
        tap.assert_not_called()

    def test_failed_release_only_retries_up(self):
        with (mock.patch.object(pulse.wetype, '_run_on_sta_thread'), mock.patch.object(
                pulse.win32_input, 'send_voice_key_combo_tap', side_effect=pulse.win32_input.InputCleanupIncompleteError('up')) as tap,
                mock.patch.object(pulse.win32_input, 'send_voice_key_combo_up', side_effect=[OSError('up'), None]) as up):
            control = pulse.ToggleShortcut('lshift+f9')
            with self.assertRaises(pulse.win32_input.InputCleanupIncompleteError):
                control.send(lambda: False)
            self.assertFalse(control.cleanup())
            with self.assertRaises(OSError):
                control.send(lambda: False)
            self.assertTrue(control.cleanup())
            self.assertEqual(tap.call_count, 1)
            self.assertEqual(up.call_count, 2)


class ToggleHostTests(unittest.TestCase):
    def setUp(self):
        fixtures.HostTests.setUp(self)
        self.owner._config.update(remote_recording_mode='toggle', voice_hotkeys_by_provider={
            'wetype': {'hold': 'lctrl+lwin', 'source': 'manual', 'toggle': 'lshift+f9', 'toggle_source': 'manual'}})
        for target in ('_run_on_sta_thread',):
            patcher = mock.patch.object(pulse.wetype, target, return_value=True)
            self.prepare = patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(pulse.win32_input, 'send_voice_key_combo_tap')
        self.tap = patcher.start()
        self.addCleanup(patcher.stop)

    def _confirm_capture(self, identity='wetype-capture'):
        self.host.capture_watch.reader = mock.Mock(return_value=(session(identity),))
        self.host._poll_host()

    def test_start_checks_capture_promptly_without_sending_extra_shortcuts(self):
        from ovb_rc003 import chromecast_voice_host as host_module
        with mock.patch.object(host_module.time, 'monotonic', return_value=10) as clock:
            self.host._start_host(1)
            self.host._poll_host()
            self.assertFalse(self.host.confirmed)
            self.host.capture_watch.reader = mock.Mock(return_value=(session('wetype-capture'),))
            clock.return_value = 10.06
            self.host._poll_host()
            self.assertTrue(self.host.confirmed)
            self.host.client.voice_host.assert_called_once_with(1, 'ready')
            self.tap.assert_called_once()

    def test_explicit_presses_pulse_once_no_hold_no_consumption(self):
        self.host._start_host(1)
        self.host._start_host(1)
        self.tap.assert_called_once_with(('lshift', 'f9'))
        self.owner._voice_shortcut.apply.assert_not_called()
        self.owner._ensure_voice_diagnostic_attempt.assert_called_once_with(
            provider_shortcut_mode='toggle',
            effective_hotkey_tokens=('lshift', 'f9'),
            effective_backend='toggle_shortcut',
        )
        self.assertIsNotNone(self.host.capture_watch)
        self.assertEqual(self.host.capture_watch.baseline, set())
        self.assertNotIn(mock.call(True), self.owner._set_runtime_voice_active.call_args_list)
        self.host.client.voice_host.assert_not_called()
        self._confirm_capture()
        self.owner._set_runtime_voice_active.assert_called_once_with(True)
        self.host.client.voice_host.assert_called_once_with(1, 'ready')
        event = {'event': 'host_stop', 'attempt': 1, 'data': 'second_press'}
        self.host._handle(event)
        self.host._handle(event)
        self.assertEqual(self.tap.call_count, 2)
        self.assertEqual(self.prepare.call_count, 2)
        self.assertTrue(self.host.closed)

    def test_queued_second_press_keeps_reason_after_an_earlier_audio_event(self):
        self.host._start_host(1)
        self._confirm_capture()
        self.host.decoder = mock.Mock(mic_open=True)
        self.host.enqueue({"event": "audio", "attempt": 1, "data": "0102"})
        self.host.enqueue({"event": "host_stop", "attempt": 1, "data": "second_press"})

        self.host._handle(self.host.events.get_nowait())
        self.host._poll_host()
        self.assertFalse(self.host.closed)
        self.assertEqual(self.tap.call_count, 1)

        self.host._handle(self.host.events.get_nowait())
        self.host._poll_host()
        self.assertTrue(self.host.closed)
        self.assertEqual(self.tap.call_count, 2)
        self.host._handle({"event": "host_stop", "attempt": 1, "data": "second_press"})
        self.assertEqual(self.tap.call_count, 2)

    def test_automatic_stop_never_toggles_or_guesses_host(self):
        self.host._start_host(1)
        self.host._poll_host()
        self.host._handle({'event': 'host_stop', 'attempt': 1, 'data': ''})
        self.tap.assert_called_once()
        self.owner._voice_shortcut.record_audio_result.assert_not_called()
        self.owner._set_runtime_voice_result.assert_called_with('not_tested')
        self.assertTrue(self.host.closed)

    def test_capture_end_stops_once_without_toggle_and_new_attempt_can_start(self):
        with (mock.patch('ovb_rc003.chromecast_host_activity.read_wetype_capture',
                         side_effect=[(), (session(),), (), (), (), (session('next'),)]) as read,
                mock.patch('ovb_rc003.chromecast_voice_host.time.monotonic', return_value=10) as clock):
            self.host._start_host(1)
            self.host._poll_host()
            self.assertEqual(self.host.capture_watch.status, 'tracking')
            clock.return_value = 10.25
            self.host._poll_host()
            self.assertFalse(self.host.closed)
            clock.return_value = 11
            self.host._poll_host()
            self.assertTrue(self.host.closed)
            self.assertIsNone(self.host.capture_watch)
            for now in (12, 13):
                clock.return_value = now
                self.host._poll_host()
            self.assertEqual(read.call_count, 4)
            stops = [call for call in self.host.client.voice_host.call_args_list if call == mock.call(1, 'stop')]
            self.assertEqual(len(stops), 1)
            self.host._handle({'event': 'host_stop', 'attempt': 1, 'data': 'second_press'})
            self.tap.assert_called_once()
            self.host._handle({'event': 'state', 'attempt': 1, 'data': 'idle'})
            self.host._start_host(2)
            self.assertEqual(self.tap.call_count, 2)
            self.host._poll_host()
            self.assertEqual(self.host.capture_watch.status, 'tracking')

    def test_capture_end_cleanup_failure_does_not_repeat_stop_or_send_late_toggle(self):
        self.host._start_host(1)
        watch = mock.Mock(status='ended', identity=None, baseline=())
        watch.poll.return_value = True
        self.host.capture_watch = watch
        self.owner._voice_audio.flush.return_value.completed = False
        self.host._poll_host()
        self.assertFalse(self.host.closed)
        self.assertEqual(self.host.state, 'stopping')
        self.host._poll_host()
        self.host._handle({'event': 'host_stop', 'attempt': 1, 'data': 'second_press'})
        watch.poll.assert_called_once()
        self.tap.assert_called_once()
        stops = [call for call in self.host.client.voice_host.call_args_list if call == mock.call(1, 'stop')]
        self.assertEqual(len(stops), 1)

    def test_failed_capture_baseline_refuses_start_without_sending_toggle(self):
        with mock.patch('ovb_rc003.chromecast_host_activity.read_wetype_capture', side_effect=OSError('unavailable')) as read:
            self.host._start_host(1)
            read.assert_called_once()
            self.tap.assert_not_called()
            self.assertFalse(self.host.closed)
            self.host.client.voice_host.assert_called_once_with(1, 'failed')
            self.host._handle({'event': 'host_stop', 'attempt': 1, 'data': ''})
            self.assertTrue(self.host.closed)

    def test_existing_capture_refuses_start_without_sending_toggle(self):
        with mock.patch(
                'ovb_rc003.chromecast_host_activity.read_wetype_capture',
                return_value=(session('already-active'),)):
            self.host._start_host(1)
        self.tap.assert_not_called()
        self.host.client.voice_host.assert_called_once_with(1, 'failed')

    def test_runtime_flag_cannot_confirm_toggle_without_new_capture(self):
        self.host._start_host(1)
        self.owner._voice_shortcut.runtime_mic_confirmed = True
        self.host._poll_host()
        self.assertFalse(self.host.confirmed)
        self.host.client.voice_host.assert_not_called()

    def test_runtime_failure_cannot_reject_toggle_with_new_capture(self):
        self.host._start_host(1)
        self.owner._voice_shortcut.runtime_mic_confirmed = False
        self._confirm_capture()
        self.assertTrue(self.host.confirmed)
        self.host.client.voice_host.assert_called_once_with(1, 'ready')

    def test_unconfirmed_second_press_does_not_send_another_toggle(self):
        self.host._start_host(1)
        self.host._handle({'event': 'host_stop', 'attempt': 1, 'data': 'second_press'})
        self.tap.assert_called_once()
        self.assertTrue(self.host.closed)

    def test_capture_start_timeout_fails_without_retry(self):
        with mock.patch('ovb_rc003.chromecast_voice_host.time.monotonic', return_value=10) as clock:
            self.host._start_host(1)
            clock.return_value = 19
            self.host._poll_host()
        self.tap.assert_called_once()
        self.assertFalse(self.host.confirmed)
        self.assertTrue(self.host.closed)
        self.assertIn(mock.call(1, 'failed'), self.host.client.voice_host.call_args_list)
        self.assertNotIn(mock.call(1, 'ready'), self.host.client.voice_host.call_args_list)

    def test_capture_first_seen_at_deadline_does_not_confirm_late(self):
        with mock.patch('ovb_rc003.chromecast_voice_host.time.monotonic', return_value=10) as clock:
            self.host._start_host(1)
            self.host.capture_watch.reader = mock.Mock(return_value=(session('late'),))
            clock.return_value = 19
            self.host._poll_host()
        self.assertFalse(self.host.confirmed)
        self.assertTrue(self.host.closed)
        self.assertNotIn(mock.call(1, 'ready'), self.host.client.voice_host.call_args_list)

    def test_capture_query_crossing_deadline_does_not_confirm_late(self):
        current = {'now': 10.0}
        with mock.patch(
                'ovb_rc003.chromecast_voice_host.time.monotonic',
                side_effect=lambda: current['now']):
            self.host._start_host(1)

            def late_capture():
                current['now'] = 19.1
                return (session('late'),)

            self.host.capture_watch.reader = mock.Mock(side_effect=late_capture)
            current['now'] = 18.9
            self.host._poll_host()
        self.tap.assert_called_once()
        self.assertFalse(self.host.confirmed)
        self.assertTrue(self.host.closed)
        self.assertNotIn(mock.call(True), self.owner._set_runtime_voice_active.call_args_list)
        self.assertNotIn(mock.call(1, 'ready'), self.host.client.voice_host.call_args_list)

    def test_stop_during_capture_query_cannot_publish_late_ready(self):
        self.host._start_host(1)

        def stop_then_capture():
            self.host.request_stop()
            return (session('late'),)

        self.host.capture_watch.reader = mock.Mock(side_effect=stop_then_capture)
        self.host._poll_host()
        self.assertTrue(self.host.closed)
        self.assertFalse(self.host.confirmed)
        self.assertNotIn(mock.call(True), self.owner._set_runtime_voice_active.call_args_list)
        self.assertNotIn(mock.call(1, 'ready'), self.host.client.voice_host.call_args_list)

    def test_tracker_loss_during_capture_query_cannot_publish_late_ready(self):
        self.host._start_host(1)

        def cancel_then_capture():
            self.host.cancel_recording()
            return (session('late'),)

        self.host.capture_watch.reader = mock.Mock(side_effect=cancel_then_capture)
        self.host._poll_host()
        self.assertTrue(self.host.closed)
        self.assertFalse(self.host.confirmed)
        self.assertNotIn(mock.call(True), self.owner._set_runtime_voice_active.call_args_list)
        self.assertNotIn(mock.call(1, 'ready'), self.host.client.voice_host.call_args_list)
        self.assertIn(mock.call(1, 'stop'), self.host.client.voice_host.call_args_list)

    def test_timeout_failure_cannot_open_receiver_software_stream(self):
        gate = Recording('toggle')
        gate.control(fixtures.PHYSICAL, 1)
        gate.control(fixtures.RELEASE, 1.1)
        receiver_effects = []

        def receive_result(attempt, result):
            if result in ('ready', 'failed'):
                receiver_effects.extend(gate.host_result(attempt, result == 'ready', 2))

        self.host.client.voice_host.side_effect = receive_result
        with mock.patch('ovb_rc003.chromecast_voice_host.time.monotonic', return_value=10) as clock:
            self.host._start_host(1)
            clock.return_value = 19
            self.host._poll_host()
        self.assertEqual(gate.state, 'stopping')
        self.assertFalse(gate.host_ready)
        self.assertFalse(any(effect.kind == 'device' and effect.value == b'\x0c\x00'
                             for effect in receiver_effects))

    def test_missing_toggle_does_not_use_hold_fallback(self):
        del self.owner._config['voice_hotkeys_by_provider']['wetype']['toggle']
        self.host._start_host(1)
        self.tap.assert_not_called()
        self.host.client.voice_host.assert_called_once_with(1, 'failed')

    def test_send_failure_has_no_start_retry(self):
        self.tap.side_effect = OSError('failed')
        with mock.patch.object(pulse.win32_input, 'send_voice_key_combo_up'):
            self.host._start_host(1)
            self.host._poll_host()
            self.host._stop_host()
        self.tap.assert_called_once()

    def test_shutdown_during_preparation_does_not_send(self):
        self.prepare.side_effect = lambda fn: self.host.request_stop()
        self.host._start_host(1)
        self.tap.assert_not_called()

    def test_cleanup_failure_blocks_new_attempt_without_resend(self):
        self.tap.side_effect = pulse.win32_input.InputCleanupIncompleteError('up')
        with mock.patch.object(pulse.win32_input, 'send_voice_key_combo_up', side_effect=OSError('up')):
            self.host._start_host(1)
            self.host._stop_host()
            self.host._start_host(2)
            self.assertFalse(self.host.closed)
            self.tap.assert_called_once()


class PreferencesTests(unittest.TestCase):
    def test_separate_modes_round_trip_and_no_default_toggle(self):
        settings = config.default_config()
        self.assertEqual(config.voice_hotkey_for_provider(settings, 'wetype', trigger='toggle'), '')
        original = config.voice_hotkey_for_provider(settings, 'wetype')
        config.set_voice_hotkey_for_provider(settings, 'wetype', 'lshift+f9', source='manual', trigger='toggle')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.json'
            config.save_config(path, settings)
            saved = config.load_config(path)
        self.assertEqual(config.voice_hotkey_for_provider(saved, 'wetype'), original)
        self.assertEqual(config.voice_hotkey_for_provider(saved, 'wetype', trigger='toggle'), 'lshift+f9')
        config.set_voice_hotkey_for_provider(saved, 'wetype', 'ralt')
        self.assertEqual(config.voice_hotkey_for_provider(saved, 'wetype', trigger='toggle'), 'lshift+f9')

    def test_reader_selects_toggle_not_hold(self):
        root = page()[0]
        self.assertEqual(reader._snapshot(root, lambda: None, trigger='toggle')[0], '右 Alt')
        self.assertEqual(reader._snapshot(root, lambda: None)[0], '左 Ctrl 左 Win')
        with (mock.patch.object(reader, 'read_toggle_shortcut', return_value='左Shift F9') as toggle,
                mock.patch.object(reader, 'read_hold_shortcut') as hold):
            result = sync.read_provider_hotkey('wetype', platform='win32', allow_settings_window=True, trigger='toggle')
        self.assertTrue(result.ok, result)
        self.assertEqual(result.hotkey, 'lshift+f9')
        toggle.assert_called_once()
        hold.assert_not_called()

    def test_stop_preserves_explicit_press_and_time_limit_reasons(self):
        for reason in ('second_press', 'time_limit', 'receiver_stop', 'host_stop', 'device_ended'):
            gate = Recording('toggle')
            gate.control(fixtures.PHYSICAL, 1)
            effect = gate.finish(2, reason)[0]
            self.assertEqual(effect.value, reason if reason in ('second_press', 'time_limit', 'device_ended') else None)

    def test_toggle_preference_is_owned_by_selected_remote(self):
        import tests.test_remote_settings as entity
        fixture = entity.EntitySettingsTests('test_load_is_read_only')
        fixture.setUp()
        try:
            google, _ = fixture.switch(entity.B)
            google['voice_program']['provider'] = 'wetype'
            google['remote_recording_mode'] = 'toggle'
            config.set_voice_hotkey_for_provider(google, 'wetype', 'lshift+f9', trigger='toggle', source='manual')
            config.save_config(fixture.settings, google)
            xiaomi, _ = fixture.switch(entity.A)
            self.assertEqual(config.voice_hotkey_for_provider(xiaomi, 'wetype', trigger='toggle'), '')
            self.assertEqual(config.voice_hotkey_trigger_for_settings(xiaomi, 'wetype'), 'hold')
            google, _ = fixture.switch(entity.B)
            self.assertEqual(config.voice_hotkey_for_provider(google, 'wetype', trigger='toggle'), 'lshift+f9')
        finally:
            fixture.doCleanups()

    def test_second_press_intent_survives_codec(self):
        identity = channel.SessionIdentity.create('a' * 64)
        tx, rx = channel.Channel(identity, commands=False), channel.Channel(identity, commands=False)
        rx.decode(tx.encode('ready'))
        value = rx.decode(tx.encode('voice', event='host_stop', attempt=1, data='second_press', time=1.0))
        self.assertEqual(value['data'], 'second_press')
