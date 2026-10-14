"""Regressions using in-memory native callbacks; never install a desktop hook."""
import ctypes
import unittest
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import hotkey, hotkey_capture_windows as capture
from ovb_rc003 import voice_key_physicalizer_windows as hooks, win32_input as inputs
from ovb_rc003 import voice_controller, voice_shortcut_session as shortcuts
from ovb_rc003.chromecast_wetype_toggle import ToggleShortcut
from tests.test_app_wiring import _AppWiringTestCase, _FakeVoicePhysicalizer


class CaptureProbeTests(unittest.TestCase):
    def test_ctrl_shift_zero_survives_probe_for_local_and_remote_keyboards(self):
        for injected in (0, capture.LLKHF_INJECTED):
            with self.subTest(injected=injected):
                result = []
                recorder = capture.HotkeyCapture(result.append, accept_injected=True)
                events = [(0xA3, 0x1D, injected | 1, False),
                          (0xFC, 0, 0x10, False), (0xFC, 0, 0x90, True),
                          (0xA1, 0x36, injected, False), (0x30, 0x0B, injected, False),
                          (0x30, 0x0B, injected, False), (0x30, 0x0B, injected | 0x80, True),
                          (0xA1, 0x36, injected | 0x80, True),
                          (0xA3, 0x1D, injected | 0x81, True)]
                for vk, scan, flags, up in events:
                    consumed = recorder._handle_event(
                        capture.WM_KEYUP if up else capture.WM_KEYDOWN,
                        capture.KBDLLHOOKSTRUCT(vk, scan, flags, 0, 0),
                    )
                    self.assertEqual(consumed, vk != 0xFC)
                self.assertEqual(result, ["rctrl+rshift+0"])
                for mapping in (True, False):
                    self.assertEqual(hotkey.HotkeySpec.from_user_text(
                        result[0], mapping=mapping).serialize(), result[0])

    def test_probe_alone_does_not_complete_or_cancel_capture(self):
        result = []
        recorder = capture.HotkeyCapture(result.append, accept_injected=True)
        for message in (capture.WM_KEYDOWN, capture.WM_KEYUP):
            self.assertFalse(recorder._handle_event(
                message, capture.KBDLLHOOKSTRUCT(0xFC, 0, 0x10, 0, 0x31307946)))
        self.assertEqual(result, [])
        self.assertFalse(recorder._stop_event.is_set())


@unittest.skipUnless(hasattr(ctypes, "WINFUNCTYPE"), "Windows native callback ABI")
class ChatterflyNativeForwardingTests(unittest.TestCase):
    def setUp(self):
        self.gate = hooks.VoiceKeyPhysicalizer()
        self.gate._hook = 17
        self.gate._thread = mock.Mock()
        self.gate._thread.is_alive.return_value = True
        self.gate._thread_id = ctypes.c_ulong(77)
        self.user32, self.kernel32 = mock.Mock(), mock.Mock()
        self.user32.SetWindowsHookExW.side_effect = range(18, 100)
        self.user32.UnhookWindowsHookEx.return_value = True
        self.kernel32.GetModuleHandleW.return_value = 1
        self.native_edges, self.accepted, self.held = [], [], set()
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                           ctypes.c_size_t, ctypes.c_ssize_t)

        def host_callback(_code, message, address):
            event = hooks.KBDLLHOOKSTRUCT.from_address(address)
            # Same admission rule verified in the active Chatterfly callback.
            if event.flags & hooks.LLKHF_INJECTED:
                return 0
            up = message == hooks.WM_KEYUP
            self.accepted.append((event.vkCode, up, event.flags, event.dwExtraInfo))
            if up:
                self.held.discard(event.vkCode)
            else:
                self.held.add(event.vkCode)
            return 1

        self.host_callback = callback_type(host_callback)
        self.gate._hookproc_keepalive = callback_type(self.gate._hookproc)
        self.user32.CallNextHookEx.side_effect = lambda _h, c, m, p: self.host_callback(c, m, p)

        def post(*_args):
            self.gate._refresh_forwarding_order_on_owner(self.user32, self.kernel32)
            return 1

        self.user32.PostThreadMessageW.side_effect = post
        patcher = mock.patch.object(ctypes, "windll", SimpleNamespace(
            user32=self.user32, kernel32=self.kernel32))
        patcher.start()
        self.addCleanup(patcher.stop)
        hooks._set_physical_tracker_active(True, owner=self.gate, hook_handle=17,
                                          _query=lambda _vk: False)
        self.addCleanup(hooks._set_physical_tracker_active, False)
        self.addCleanup(inputs.set_diagnostic_trace, None)
        patcher = mock.patch.object(inputs, "_real_keybd_event", side_effect=self.emit)
        self.native_send = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(hooks.set_diagnostic_trace, None)

    def emit(self, vk, up, *, _extra_info):
        flags = 0x12 | (0x80 if up else 0) | (1 if vk == 0xA3 else 0)
        event = hooks.KBDLLHOOKSTRUCT(vk, 0, flags, 0, _extra_info)
        self.native_edges.append((vk, up))
        self.gate._hookproc_keepalive(0, hooks.WM_KEYUP if up else hooks.WM_KEYDOWN,
                                      ctypes.addressof(event))
        self.assertEqual((event.flags, event.dwExtraInfo), (flags, _extra_info))

    def send(self, name, **kwargs):
        getattr(inputs, 'send_voice_key_combo_' + name)(
            ("rctrl", "rshift", "0"), physicalize=True,
            _key_down_query=kwargs.pop('query', lambda _vk: False), **kwargs)

    def test_hold_reaches_filter_on_all_six_edges_and_restores_metadata(self):
        self.send('down')
        self.assertEqual(self.held, {0xA3, 0xA1, 0x30})
        self.send('up')
        self.assertEqual(self.held, set())
        self.assertEqual(self.native_edges, [(0xA3, False), (0xA1, False), (0x30, False),
                                             (0x30, True), (0xA1, True), (0xA3, True)])
        self.assertEqual(len(self.accepted), 6)
        self.assertTrue(all(not flags & 0x12 and marker == 0 for _, _, flags, marker in self.accepted))
        self.assertEqual(self.user32.SetWindowsHookExW.call_count, 6)
        self.assertEqual(hooks._PHYSICAL_KEYS_DOWN, set())
        self.assertEqual(hooks.snapshot_health().pending_confirmations, 0)

    def test_toggle_uses_the_same_confirmed_release_path(self):
        with mock.patch.object(inputs.time, 'sleep'):
            self.send('tap')
        self.assertEqual(len(self.accepted), 6)
        self.assertEqual(self.held, set())

    def test_foreign_injected_key_is_unchanged_and_host_still_rejects_it(self):
        for marker in (0, hooks.VOICE_EVENT_EXTRA_INFO):
            event = hooks.KBDLLHOOKSTRUCT(0x30, 0, 0x12, 0, marker)
            self.gate._hookproc_keepalive(0, hooks.WM_KEYDOWN, ctypes.addressof(event))
            self.assertEqual((event.flags, event.dwExtraInfo), (0x12, marker))
        self.assertEqual(self.accepted, [])

    def test_ticket_cannot_authorize_another_key_or_edge(self):
        ticket = hooks.begin_marked_voice_event(False, vk_code=0x30)
        for vk, up in ((0xA3, False), (0x30, True)):
            event = hooks.KBDLLHOOKSTRUCT(vk, 0, 0x10, 0, ticket.marker)
            self.assertIsNone(hooks.physicalize_injected_event(event, up))
            self.assertEqual(event.flags, 0x10)
        hooks.cancel_marked_voice_event(ticket)
        event = hooks.KBDLLHOOKSTRUCT(0x30, 0, 0x10, 0, ticket.marker)
        self.assertIsNone(hooks.physicalize_injected_event(event, False))

    def test_order_preparation_failure_emits_nothing(self):
        self.user32.SetWindowsHookExW.side_effect = None
        self.user32.SetWindowsHookExW.return_value = 0
        with self.assertRaises(inputs.Win32InputUnavailableError):
            self.send('down')
        self.native_send.assert_not_called()

    def test_mid_combo_preparation_failure_releases_only_delivered_keys(self):
        self.user32.SetWindowsHookExW.side_effect = [18, 0, 19]
        with self.assertRaises(inputs.Win32InputUnavailableError):
            self.send('down')
        self.assertEqual(self.native_edges, [(0xA3, False), (0xA3, True)])
        self.assertFalse(self.held)

    def test_user_press_mid_combo_prevents_overwriting_the_physical_key(self):
        def query(vk):
            return vk == 0xA1 and bool(self.native_edges)
        with self.assertRaises(inputs.PhysicalKeyInUseError):
            self.send('down', query=query)
        self.assertEqual(self.native_edges, [(0xA3, False), (0xA3, True)])

    def test_release_failure_attempts_remaining_keys_and_keeps_cleanup_debt(self):
        self.send('down')
        self.user32.SetWindowsHookExW.side_effect = [0, 24, 25]
        with self.assertRaises(inputs.InputCleanupIncompleteError):
            self.send('up')
        self.assertEqual(self.held, {0x30})
        self.user32.SetWindowsHookExW.side_effect = range(26, 40)
        self.send('up')
        self.assertFalse(self.held)

    def test_reordering_preserves_real_modifier_ownership(self):
        hooks.record_physical_key_event(hooks.KBDLLHOOKSTRUCT(0xA1, 0x36, 0, 0, 0),
                                        hooks.WM_KEYDOWN)
        before = hooks.snapshot_health().generation
        hooks.ensure_voice_forwarding_order()
        self.assertEqual(hooks.snapshot_health().generation, before)
        self.assertTrue(hooks.physical_key_is_down(0xA1))

    def test_pending_ticket_blocks_reorder_without_invalidating_receipt(self):
        ticket = hooks.begin_marked_voice_event(False)
        with self.assertRaises(hooks.VoiceKeyPhysicalizerUnavailableError):
            hooks.ensure_voice_forwarding_order()
        self.user32.SetWindowsHookExW.assert_not_called()
        self.assertFalse(ticket.cancelled)

    def test_message_timeout_does_not_send_or_leave_request(self):
        self.user32.PostThreadMessageW.side_effect = lambda *_args: 1
        self.assertFalse(self.gate.refresh_forwarding_order(timeout=0.001))
        self.assertIsNone(self.gate._refresh_request)
        self.native_send.assert_not_called()

    def test_failed_old_hook_removal_rolls_back_replacement(self):
        self.user32.UnhookWindowsHookEx.side_effect = [False, True]
        self.assertFalse(self.gate.refresh_forwarding_order())
        self.assertEqual(self.gate._hook, 17)
        self.assertEqual(self.user32.UnhookWindowsHookEx.call_args_list,
                         [mock.call(17), mock.call(18)])
        self.assertEqual(self.gate._extra_hooks, [])

    def test_unconfirmed_replacement_cannot_physicalize_until_cleanup_succeeds(self):
        self.user32.UnhookWindowsHookEx.side_effect = [False, False]
        self.assertFalse(self.gate.refresh_forwarding_order())
        self.assertEqual(self.gate._extra_hooks, [18])
        with self.assertRaises(hooks.VoiceKeyPhysicalizerUnavailableError):
            hooks.begin_marked_voice_event(False)
        self.user32.UnhookWindowsHookEx.side_effect = None
        hooks.ensure_voice_forwarding_order()
        self.assertEqual(self.gate._extra_hooks, [])
        ticket = hooks.begin_marked_voice_event(False)
        hooks.cancel_marked_voice_event(ticket)

    def test_shutdown_does_not_refresh_or_send(self):
        self.gate._stop_event.set()
        with self.assertRaises(inputs.Win32InputUnavailableError):
            self.send('down')
        self.user32.SetWindowsHookExW.assert_not_called()
        self.native_send.assert_not_called()

    def test_drain_allows_confirmed_release_but_rejects_new_down(self):
        self.send('down')
        hooks._begin_physical_tracker_drain()
        self.send('up')
        self.assertFalse(self.held)
        with self.assertRaises(inputs.Win32InputUnavailableError):
            self.send('down')
        self.assertEqual(len(self.native_edges), 6)

    def test_missing_acknowledgement_is_not_reported_as_delivery(self):
        real_forward = hooks.physicalize_injected_event

        def drop_down(event, up):
            return real_forward(event, up) if up else None

        with mock.patch.object(hooks, 'physicalize_injected_event', side_effect=drop_down), \
             mock.patch.object(inputs, '_VOICE_EVENT_CONFIRM_TIMEOUT_SECONDS', 0.001):
            with self.assertRaises(inputs.InputCleanupIncompleteError):
                self.send('down')
        self.assertFalse(self.held)
        self.assertEqual(self.native_edges, [(0xA3, False), (0xA3, True)])
        self.assertFalse(hooks.snapshot_health().accepting_new_down)


class ChatterflyRoutingTests(unittest.TestCase):
    def test_chatterfly_cannot_fall_back_to_synthetic_key_edges(self):
        with mock.patch.object(inputs, 'send_voice_key_combo_down') as down, \
             mock.patch.object(inputs, 'send_voice_key_combo_up') as up:
            for action in (voice_controller.VoiceHostAction.KEY_DOWN,
                           voice_controller.VoiceHostAction.KEY_UP):
                with self.assertRaises(OSError):
                    shortcuts.VoiceShortcutSession.send_action(
                        action, ('rctrl', 'rshift', '0'), 'chatterfly_hotkey')
            down.assert_not_called()
            up.assert_not_called()

    def test_toggle_cleanup_never_resends_down(self):
        pulse = ToggleShortcut('rctrl+rshift+0', prepare=lambda: False,
                               ensure_key_tracking=lambda _: True, physicalize=True)
        with mock.patch.object(inputs, 'send_voice_key_combo_tap',
                               side_effect=inputs.InputCleanupIncompleteError('test')) as tap, \
             mock.patch.object(inputs, 'send_voice_key_combo_up') as up:
            with self.assertRaises(inputs.InputCleanupIncompleteError):
                pulse.send(lambda: False)
            self.assertTrue(pulse.release_pending)
            self.assertTrue(pulse.cleanup())
            tap.assert_called_once_with(('rctrl', 'rshift', '0'), physicalize=True)
            up.assert_called_once_with(('rctrl', 'rshift', '0'), physicalize=True)

    def test_toggle_preparation_failure_or_cancellation_never_sends(self):
        for ready in (False, True):
            pulse = ToggleShortcut('rctrl+rshift+0', prepare=lambda: False,
                                   ensure_key_tracking=lambda _: ready, physicalize=True)
            cancelled = iter((False, True))
            with mock.patch.object(inputs, 'send_voice_key_combo_tap') as tap:
                with self.assertRaises((OSError, inputs.Win32InputUnavailableError)):
                    pulse.send(lambda: next(cancelled))
                tap.assert_not_called()
                self.assertFalse(pulse.release_pending)


class ChatterflyXiaomiWiringTests(_AppWiringTestCase):
    def setUp(self):
        super().setUp()
        self.app._config['voice_program'] = {'provider': 'chatterfly'}
        self.app._config['voice_hotkeys_by_provider'] = {
            'chatterfly': {'hold': 'rctrl+rshift+0', 'source': 'manual'}}
        self.app._voice_shortcut.hotkey = hotkey.HotkeySpec.parse('rctrl+rshift+0')
        self.app._voice_key_physicalizer = _FakeVoicePhysicalizer()
        self.app._voice_key_physicalizer.is_running = True
        self.control = mock.Mock(current_generation=1, cleanup_pending=True)
        self.control.start.return_value = True
        self.control.stop.return_value = True
        self.app._voice_shortcut.chatterfly_control = self.control

    def test_xiaomi_waits_for_owned_bubble_and_release_keeps_original_backend(self):
        with mock.patch.object(inputs, 'send_voice_key_combo_down') as down, \
             mock.patch.object(inputs, 'send_voice_key_combo_up') as up:
            self.assertEqual(self.app._configured_voice_hotkey_backend(), 'chatterfly_hotkey')
            self.assertTrue(self.app._handle_mic_button_pressed())
            down.assert_not_called()
            self.assertEqual(self.app._ble_session.mic_open_calls, 0)
            self.assertFalse(self.app._voice_pcm_forwarding_enabled)
            self.app._on_chatterfly_voice_state(1, 'ready')
            self.assertEqual(self.app._ble_session.mic_open_calls, 1)
            self.assertTrue(self.app._voice_pcm_forwarding_enabled)
            self.app._config['voice_program'] = {'provider': 'wetype'}
            self.assertTrue(self.app._voice_shortcut.release_pending())
            self.control.stop.assert_called_once_with()
            up.assert_not_called()

    def test_xiaomi_does_not_open_device_if_ipc_start_fails(self):
        self.control.start.return_value = False
        self.control.cleanup_pending = False
        with mock.patch.object(self.app, '_ensure_voice_key_physicalizer_for_hotkey',
                               return_value=False) as prepare, \
             mock.patch.object(inputs, 'send_voice_key_combo_down') as down:
            self.assertFalse(self.app._handle_mic_button_pressed())
            prepare.assert_not_called()
            down.assert_not_called()
            self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_chatterfly_no_longer_depends_on_keyboard_physicalizer(self):
        self.app._voice_key_physicalizer_ready = False
        with mock.patch.object(self.app, '_start_voice_key_physicalizer') as start:
            self.assertTrue(self.app._ensure_voice_key_physicalizer_for_hotkey(
                ('rctrl', 'rshift', '0'), 'chatterfly_hotkey'))
            start.assert_not_called()

    def test_stale_or_released_ready_never_enables_audio(self):
        self.assertTrue(self.app._handle_mic_button_pressed())
        self.app._on_chatterfly_voice_state(0, 'ready')
        self.assertFalse(self.app._voice_pcm_forwarding_enabled)
        self.app._voice_shortcut.controller.reset()
        self.app._on_chatterfly_voice_state(1, 'ready')
        self.assertFalse(self.app._voice_pcm_forwarding_enabled)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_normal_async_close_does_not_reconnect_or_lose_cleanup_ownership(self):
        self.control.stop.return_value = False
        self.control.completion_pending = True
        self.assertTrue(self.app._handle_mic_button_pressed())
        with mock.patch.object(self.app._supervisor, 'request_reconnect') as reconnect:
            self.assertTrue(self.app._release_hold_voice_on_physical_release_locked('test release'))
            reconnect.assert_not_called()
        self.assertFalse(self.app._voice_shortcut.controller.active)
        self.assertIsNotNone(self.app._voice_shortcut.pending_tokens)
        self.app._on_chatterfly_voice_state(1, 'cleanup_pending')
        self.assertEqual(self.app._current_voice_runtime_state(), 'host_stop_failed')
        self.control.stop.return_value = True
        self.assertTrue(self.app._voice_shortcut.release_pending())
        self.assertIsNone(self.app._voice_shortcut.pending_tokens)
