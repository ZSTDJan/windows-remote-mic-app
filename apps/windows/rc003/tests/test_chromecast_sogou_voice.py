import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import config, chromecast_voice_host as host_module
from ovb_rc003 import voice_hotkey_sync_windows as sync, voice_controller
from ovb_rc003 import chromecast_host_activity as activity
from tests.test_chromecast_host_activity import session
import tests.test_chromecast_voice as fixtures


class SogouHostTests(unittest.TestCase):
    def setUp(self):
        fixtures.HostTests.setUp(self)
        self.owner._config.update(voice_program={'provider': 'sogou'}, remote_recording_mode='toggle',
            voice_hotkeys_by_provider={'sogou': {'hold': 'lctrl+lshift+f8', 'toggle': 'lshift+f8'}})
        self.owner._voice_shortcut.hotkey = SimpleNamespace(modifiers=('lctrl', 'lshift'), key='f8')
        self.owner._configured_voice_hotkey_backend.return_value = 'marked'
        self.owner._wait_for_sogou_voice_process.return_value = True
        for name, target in (('capture', 'ovb_rc003.chromecast_voice_host.read_sogou_capture'),
                             ('tap', 'ovb_rc003.chromecast_wetype_toggle.win32_input.send_voice_key_combo_tap'),
                             ('activate', 'ovb_rc003.chromecast_wetype_toggle.wetype._run_on_sta_thread')):
            patcher = mock.patch(target, return_value=())
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_hold_checks_capture_promptly_but_toggle_keeps_existing_cadence(self):
        for mode, expected_fast in (("hold", True), ("toggle", False)):
            with self.subTest(mode=mode):
                self.owner._config['remote_recording_mode'] = mode
                self.capture.return_value = ()
                with mock.patch.object(host_module.time, 'monotonic', return_value=10) as clock:
                    self.host._start_host(1 if mode == "hold" else 2)
                    self.host._poll_host()
                    reader = mock.Mock(return_value=(session(pid=30),))
                    self.host.capture_watch.reader = reader
                    clock.return_value = 10.06
                    self.host._poll_host()
                    self.assertEqual(reader.called, expected_fast)
                    self.host.cancel_recording()
                    self.host._poll_host()
                    self.host._handle(dict(event='state', attempt=self.host.attempt, data='idle'))

    def test_toggle_sends_once_per_press_no_profile_switch_or_input_consumption(self):
        self.host._start_host(1)
        self.tap.assert_called_once_with(('lshift', 'f8'))
        self.activate.assert_not_called()
        self.owner._voice_shortcut.apply.assert_not_called()
        self.owner._ensure_voice_diagnostic_attempt.assert_called_once_with(
            provider_shortcut_mode='toggle',
            effective_hotkey_tokens=('lshift', 'f8'),
            effective_backend='toggle_shortcut',
        )
        self.host.client.voice_host.assert_called_once_with(1, 'ready')
        event = dict(event='host_stop', attempt=1, data='second_press')
        self.host._handle(event)
        self.host._handle(event)
        self.assertEqual(self.tap.call_count, 2)
        self.assertTrue(self.host.closed)
        self.owner._voice_shortcut.record_audio_result.assert_not_called()

    def test_queued_second_press_keeps_reason_after_an_earlier_audio_event(self):
        self.host._start_host(1)
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

    def test_capture_end_stops_audio_without_a_second_toggle(self):
        self.capture.side_effect = [(), (session(pid=30),), (), ()]
        with mock.patch.object(host_module.time, 'monotonic', return_value=10) as clock:
            self.host._start_host(1)
            self.host._poll_host()
            clock.return_value = 11
            self.host._poll_host()
            clock.return_value = 11.75
            self.host._poll_host()
        self.assertTrue(self.host.closed)
        self.host.client.voice_host.assert_any_call(1, 'stop')
        self.tap.assert_called_once()

    def test_hold_waits_for_capture_then_releases_existing_shortcut(self):
        self.owner._config['remote_recording_mode'] = 'hold'
        self.capture.side_effect = [(), (session(pid=30),)]
        self.host._start_host(1)
        self.assertFalse(self.host.confirmed)
        self.host.client.voice_host.assert_not_called()
        self.owner._set_runtime_voice_active.reset_mock()
        self.owner._set_runtime_voice_result.reset_mock()
        self.host._poll_host()
        self.host.client.voice_host.assert_called_once_with(1, 'ready')
        self.owner._set_runtime_voice_active.assert_not_called()
        self.owner._set_runtime_voice_result.assert_not_called()
        self.host._handle(dict(event='host_stop', attempt=1, data=''))
        self.assertEqual(self.owner._voice_shortcut.apply.call_args_list,
            [mock.call(voice_controller.VoiceHostAction.KEY_DOWN), mock.call(voice_controller.VoiceHostAction.KEY_UP)])
        self.owner._ensure_voice_diagnostic_attempt.assert_called_once_with(
            provider_shortcut_mode='hold',
            effective_hotkey_tokens=('lctrl', 'lshift', 'f8'),
            effective_backend='marked',
        )
        self.assertTrue(self.host.closed)
        self.tap.assert_not_called()
        self.activate.assert_not_called()

    def test_hold_capture_timeout_cleans_up_without_retry(self):
        self.owner._config['remote_recording_mode'] = 'hold'
        with mock.patch.object(host_module.time, 'monotonic', return_value=10) as clock:
            self.host._start_host(1)
            clock.return_value = 20
            self.host._poll_host()
        self.assertTrue(self.host.closed)
        self.host.client.voice_host.assert_any_call(1, 'failed')
        self.assertEqual(self.owner._voice_shortcut.apply.call_count, 2)

    def test_missing_process_does_not_send_or_claim_ready(self):
        self.owner._wait_for_sogou_voice_process.return_value = False
        self.host._start_host(1)
        self.tap.assert_not_called()
        self.host.client.voice_host.assert_called_once_with(1, 'failed')

    def test_missing_toggle_never_falls_back_to_hold(self):
        del self.owner._config['voice_hotkeys_by_provider']['sogou']['toggle']
        self.host._start_host(1)
        self.tap.assert_not_called()
        self.owner._voice_shortcut.apply.assert_not_called()

    def test_automatic_stop_never_sends_toggle(self):
        self.host._start_host(1)
        self.host._handle(dict(event='host_stop', attempt=1, data=''))
        self.tap.assert_called_once()
        self.assertTrue(self.host.closed)

    def _recording_with_capture(self):
        self.host._start_host(self.host.attempt + 1)
        self.host._handle(dict(event='state', attempt=self.host.attempt, data='recording'))
        self.host.capture_watch.identity = session(pid=30).identity

    def test_every_limit_routes_to_finish_once_after_audio_flush(self):
        from ovb_rc003 import chromecast_channel as channel
        from ovb_rc003.chromecast_voice_receiver import VoiceReceiver
        for limit in fixtures.LIMITS:
            with self.subTest(limit=limit):
                self._recording_with_capture()
                gate = fixtures.RecordingTests().open_toggle(limit)
                gate.attempt = self.host.attempt
                identity = channel.SessionIdentity.create('a' * 64)
                tx, rx = channel.Channel(identity, commands=False), channel.Channel(identity, commands=False)
                rx.decode(tx.encode('ready'))
                receiver = VoiceReceiver.__new__(VoiceReceiver)
                receiver.gate, receiver.queue = gate, fixtures.deque()
                receiver.recording_elapsed_ms = 0
                sent = []
                receiver.send = lambda kind, attempt, data, stamp: sent.append(rx.decode(tx.encode(
                    'voice', event=kind, attempt=attempt, data=data, time=stamp)))
                receiver.effects(gate.tick(1.3 + limit))
                def finish(identity, *, cancel_event):
                    self.assertFalse(self.owner._voice_shortcut.lock.locked())
                    self.owner._voice_audio.flush.assert_called()
                    self.assertEqual(identity, session(pid=30).identity)
                    return 'invoked_once'
                with mock.patch.object(host_module.sogou_submit_windows, 'finish_with_timeout', side_effect=finish) as submit:
                    for event in sent:
                        self.host._handle(event)
                    self.host._handle(dict(event='host_stop', attempt=self.host.attempt, data='time_limit'))
                    self.host._stop_host()
                    submit.assert_called_once()
                self.assertTrue(self.host.closed)
                self.assertEqual(self.tap.call_count, 1)  # Start only; never stop via toggle.
                self.tap.reset_mock()
                self.owner._voice_audio.flush.reset_mock()
                self.host._handle(dict(event='state', attempt=self.host.attempt, data='idle'))

    def test_queued_time_limit_keeps_directed_finish_after_earlier_audio(self):
        self._recording_with_capture()
        self.host.decoder = mock.Mock(mic_open=True)
        self.host.enqueue({"event": "audio", "attempt": self.host.attempt, "data": "0102"})
        self.host.enqueue({"event": "host_stop", "attempt": self.host.attempt, "data": "time_limit"})

        with mock.patch.object(
            host_module.sogou_submit_windows,
            "finish_with_timeout",
            return_value="invoked_once",
        ) as submit:
            self.host._handle(self.host.events.get_nowait())
            self.host._poll_host()
            self.assertFalse(self.host.closed)
            self.host._handle(self.host.events.get_nowait())
            self.host._poll_host()

        submit.assert_called_once_with(
            session(pid=30).identity,
            cancel_event=self.host.stopping,
        )
        self.assertTrue(self.host.closed)
        self.assertEqual(self.tap.call_count, 1)

    def test_missing_finish_or_timeout_still_releases_without_toggle_or_retry(self):
        for result in ('submit_not_identified', 'helper_cancelled_or_timed_out', 'capture_identity_unavailable'):
            with self.subTest(result=result):
                self._recording_with_capture()
                with mock.patch.object(host_module.sogou_submit_windows, 'finish_with_timeout', return_value=result) as submit:
                    event = dict(event='host_stop', attempt=self.host.attempt, data='time_limit')
                    self.host._handle(event)
                    self.host._handle(event)
                    submit.assert_called_once()
                self.host.client.voice_host.assert_any_call(self.host.attempt, 'released')
                self.tap.assert_called_once()
                self.assertTrue(any(result in call.args for call in self.owner._logger.info.call_args_list))
                self.tap.reset_mock()
                self.host._handle(dict(event='state', attempt=self.host.attempt, data='idle'))

    def test_unrelated_stop_stale_attempt_hold_and_wetype_never_submit(self):
        for case in ('manual', 'stale', 'hold', 'wetype', 'cancelled'):
            with self.subTest(case=case):
                self._recording_with_capture()
                event = dict(event='host_stop', attempt=self.host.attempt, data='time_limit')
                if case == 'manual':
                    event['data'] = 'second_press'
                elif case == 'stale':
                    event['attempt'] -= 1
                elif case == 'hold':
                    self.host._toggle = None
                elif case == 'wetype':
                    self.host.provider = 'wetype'
                else:
                    self.host.stopping.set()
                with mock.patch.object(host_module.sogou_submit_windows, 'finish_with_timeout') as submit:
                    self.host._handle(event)
                    submit.assert_not_called()
                self.host.stopping.clear()
                self.host._stop_host()
                self.host._handle(dict(event='state', attempt=self.host.attempt, data='idle'))

    def test_cleanup_failure_does_not_submit_on_retry(self):
        self._recording_with_capture()
        self.owner._voice_audio.flush.return_value = SimpleNamespace(completed=False, error=None)
        with mock.patch.object(host_module.sogou_submit_windows, 'finish_with_timeout') as submit:
            self.host._stop_host('time_limit')
            self.owner._voice_audio.flush.return_value = SimpleNamespace(completed=True, error=None)
            self.host._stop_host('time_limit')
            submit.assert_not_called()
        self.assertTrue(self.host.closed)

    def test_audio_failure_releases_resources_without_finishing_recognition(self):
        self._recording_with_capture()
        self.owner._voice_audio.flush.return_value = SimpleNamespace(
            completed=True, error=OSError("test output failed"))
        with mock.patch.object(host_module.sogou_submit_windows, 'finish_with_timeout') as submit:
            self.assertTrue(self.host._stop_host('time_limit'))
            self.host._stop_host('time_limit')
            submit.assert_not_called()
        self.assertIsNotNone(self.host.failure_result)
        self.assertIsNone(self.owner._voice_audio.writer)
        self.assertIsNone(self.owner._voice_audio.sink)

    def test_hold_release_failure_blocks_new_start_until_up_is_clean(self):
        self.owner._config['remote_recording_mode'] = 'hold'
        self.owner._voice_shortcut.apply.side_effect = [True, False, True]
        self.host._start_host(1)
        self.assertFalse(self.host._stop_host())
        self.host._start_host(2)
        self.assertEqual(self.host.attempt, 1)
        self.assertTrue(self.host._stop_host())


class SogouSettingsTests(unittest.TestCase):
    def test_read_both_shortcuts_and_disabled_or_missing_free_fails(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'sogou_voice_assistant_pc' / 'config.json'
            path.parent.mkdir()
            document = {'setting': {'shortcutKeysPress': ['LeftCtrl', 'LeftShift', 'F8'],
                                   'shortcutKeysFree': ['LeftShift', 'F8'], 'freespeakEnabled': True}}
            path.write_text(json.dumps(document), encoding='utf-8')
            self.assertEqual(sync.read_provider_hotkey('sogou', platform='win32', appdata=Path(root)).hotkey, 'lctrl+lshift+f8')
            self.assertEqual(sync.read_provider_hotkey('sogou', platform='win32', appdata=Path(root), trigger='toggle').hotkey, 'lshift+f8')
            before = path.read_bytes()
            self.assertTrue(sync.sync_provider_hotkey('sogou', 'lshift+f9', platform='win32', appdata=Path(root), trigger='toggle').ok)
            self.assertEqual(path.read_bytes(), before)
            for value in (False, True):
                document['setting']['freespeakEnabled'] = value
                if value:
                    document['setting']['shortcutKeysFree'] = []
                path.write_text(json.dumps(document), encoding='utf-8')
                self.assertFalse(sync.read_provider_hotkey('sogou', platform='win32', appdata=Path(root), trigger='toggle').ok)

    def test_provider_fields_round_trip_do_not_overwrite_hold_or_wetype(self):
        settings = config.default_config()
        config.set_voice_hotkey_for_provider(settings, 'sogou', 'lctrl+lshift+f8')
        config.set_voice_hotkey_for_provider(settings, 'sogou', 'lshift+f8', trigger='toggle', source='auto')
        config.set_voice_hotkey_for_provider(settings, 'wetype', 'lshift+f9', trigger='toggle')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.json'
            config.save_config(path, settings)
            saved = config.load_config(path)
        self.assertEqual(config.voice_hotkey_for_provider(saved, 'sogou'), 'lctrl+lshift+f8')
        self.assertEqual(config.voice_hotkey_for_provider(saved, 'sogou', trigger='toggle'), 'lshift+f8')
        self.assertEqual(config.voice_hotkey_for_provider(saved, 'wetype', trigger='toggle'), 'lshift+f9')

    def test_capture_inventory_only_passes_sogou_pids(self):
        with (mock.patch.object(activity.voice_program_manager, 'diagnostic_voice_processes', return_value=(
                ('wetype', 20, 'wetype_server.exe'), ('sogou', 30, 'sogou_voice_assistant.exe'))),
                mock.patch.object(activity.audio, 'read_capture_sessions', return_value=()) as read):
            activity.read_sogou_capture()
            read.assert_called_once_with({30})
