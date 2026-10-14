import threading
from unittest import mock
import unittest

from ovb_rc003 import key_mapping, win32_input
from tests import test_app_wiring as wiring


class MouseHoldWiringTests(wiring._AppWiringTestCase):
    _use_recorded_repeat_timers = wiring.OrdinaryButtonGestureWiringTests._use_recorded_repeat_timers
    _save_button_bindings = wiring.LiveSettingsReloadTests._save_button_bindings

    def setUp(self):
        super().setUp()
        for function in ("send_mouse_button_down", "send_mouse_button_up", "send_mouse_button_click"):
            patch = mock.patch.object(win32_input, function)
            setattr(self, function, patch.start())
            self.addCleanup(patch.stop)
        self.timers = self._use_recorded_repeat_timers()

    def bind(self, button="ok", kind=key_mapping.ActionKind.MOUSE_LEFT_HOLD):
        self.app._bindings.setdefault("secondary_bindings", {})[button] = {
            "long_press": key_mapping.ButtonAction(kind).to_dict()}

    def press_long(self, button="ok", source="hid_tap"):
        self.app._on_button_event(button, True, event_source=source)
        self.timers[-1].fire()

    def test_four_buttons_both_sources_hold_until_release_without_repeat_or_ten_second_guard(self):
        for source in ("hid_tap", "chromecast"):
            for kind, mouse in key_mapping.MOUSE_HOLD_BUTTONS.items():
                with self.subTest(source=source, mouse=mouse):
                    self.bind(kind=kind)
                    self.send_mouse_button_down.reset_mock()
                    self.send_mouse_button_up.reset_mock()
                    self.press_long(source=source)
                    self.send_mouse_button_down.assert_called_once_with(mouse)
                    self.send_mouse_button_up.assert_not_called()
                    self.assertNotIn("ok", self.app._button_gestures._hold_timers)
                    self.app._on_button_event("ok", True, event_source=source)
                    self.timers[-1].callback()
                    self.send_mouse_button_down.assert_called_once_with(mouse)
                    self.app._on_button_event("ok", False, event_source=source)
                    self.send_mouse_button_up.assert_called_once_with(mouse)
                    self.assertEqual(self.app._button_mouse_holds, {})

    def test_short_tap_and_stale_long_timeout_do_not_press_mouse(self):
        self.bind()
        with mock.patch.object(self.app, "_apply_button_action") as tap:
            self.app._on_button_event("ok", True, event_source="hid_tap")
            stale = self.timers[-1]
            self.app._on_button_event("ok", False, event_source="hid_tap")
            stale.callback()
            self.assertEqual(tap.call_count, 1)
        self.send_mouse_button_down.assert_not_called()
        self.send_mouse_button_up.assert_not_called()

    def test_failed_up_retries_and_blocks_new_down_without_losing_release(self):
        self.bind()
        self.press_long()
        self.send_mouse_button_up.side_effect = win32_input.InputCleanupIncompleteError("blocked")
        self.app._on_button_event("ok", False, event_source="hid_tap")
        self.assertEqual(self.app._button_hold_release_pending, {"left"})
        self.assertTrue(self._button_input_release_timers)
        self.press_long()
        self.send_mouse_button_down.assert_called_once_with("left")
        self.send_mouse_button_up.side_effect = None
        self._button_input_release_timers[-1].fire()
        self.assertEqual(self.app._button_hold_release_pending, set())
        self.app._on_button_event("ok", False, event_source="hid_tap")

    def test_uncertain_down_retries_cleanup_and_completed_fallback_is_accepted(self):
        self.bind()
        self.send_mouse_button_down.side_effect = win32_input.InputCleanupIncompleteError("partial")
        self.press_long()
        self.assertEqual(self.app._button_hold_release_pending, {"left"})
        self.send_mouse_button_up.side_effect = OSError("fallback completed")
        self._button_input_release_timers[-1].fire()
        self.assertFalse(self.app._button_hold_release_pending)
        self.assertFalse(self.app._button_mouse_holds)
        self.app._on_button_event("ok", False, event_source="hid_tap")

    def test_physical_mouse_conflict_does_not_claim_or_release_it(self):
        self.bind()
        self.send_mouse_button_down.side_effect = win32_input.MouseButtonInUseError("held")
        self.press_long()
        self.app._on_button_event("ok", False, event_source="hid_tap")
        self.send_mouse_button_up.assert_not_called()
        self.assertFalse(self.app._button_mouse_holds)

    def test_multiple_owners_and_cancel_release_only_after_last_owner(self):
        self.bind("ok")
        self.bind("home")
        self.press_long("ok")
        self.press_long("home")
        self.send_mouse_button_down.assert_called_once_with("left")
        self.app._cancel_input_gestures_for_buttons({"ok"}, reason="source_lost", block_until_release=True)
        self.send_mouse_button_up.assert_not_called()
        self.app._cancel_input_gestures({"home"}, reason="disconnect", block_until_release=True)
        self.send_mouse_button_up.assert_called_once_with("left")
        self.assertFalse(self.app._button_mouse_holds)

    def test_reset_drains_multiple_mouse_buttons_even_if_one_up_fails(self):
        self.bind("ok")
        self.bind("home", key_mapping.ActionKind.MOUSE_RIGHT_HOLD)
        self.press_long("ok")
        self.press_long("home")
        def up(button):
            if button == "left":
                raise win32_input.InputCleanupIncompleteError("blocked")
        self.send_mouse_button_up.side_effect = up
        self.app._cancel_chromecast_mappings()
        self.assertFalse(self.app._button_mouse_holds)
        self.assertEqual(self.app._button_hold_release_pending, {"left"})
        self.assertFalse(self.app._release_chromecast_inputs())
        self.send_mouse_button_up.side_effect = None
        self.assertTrue(self.app._release_chromecast_inputs())

    def test_reserved_long_callback_cannot_press_after_release(self):
        self.bind()
        dispatcher = self.app._button_gestures
        self.app._on_button_event("ok", True, event_source="hid_tap")
        reserved = threading.Event()
        original = dispatcher._emit
        def emit(*args, **kwargs):
            if kwargs.get("require_sustained_token") is not None:
                reserved.set()
            return original(*args, **kwargs)
        with mock.patch.object(dispatcher, "_emit", side_effect=emit):
            with dispatcher._callback_lock:
                worker = threading.Thread(target=self.timers[-1].fire)
                worker.start()
                self.assertTrue(reserved.wait(1))
                self.app._on_button_event("ok", False, event_source="hid_tap")
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.send_mouse_button_down.assert_not_called()

    def test_applying_new_mapping_during_hold_keeps_original_up_then_uses_new_action(self):
        from ovb_rc003 import config
        self.bind()
        self._save_button_bindings(self.app._bindings)
        self.press_long()
        changed = config.load_key_bindings(self.app._bindings_path)
        changed["secondary_bindings"]["ok"]["long_press"] = {"kind": "mouse_right_hold"}
        self._save_button_bindings(changed)
        self.assertIsNotNone(self.app._pending_bindings)
        self.send_mouse_button_up.assert_not_called()
        self.app._on_button_event("ok", False, event_source="hid_tap")
        self.send_mouse_button_up.assert_called_once_with("left")
        self.press_long()
        self.assertEqual(self.send_mouse_button_down.call_args, mock.call("right"))
        self.app._on_button_event("ok", False, event_source="hid_tap")
        self.assertEqual(self.send_mouse_button_up.call_args, mock.call("right"))


class MouseDownDeliveryTests(unittest.TestCase):
    def test_all_four_down_events_and_physical_conflict(self):
        for button in key_mapping.MOUSE_HOLD_BUTTONS.values():
            sender = mock.Mock(return_value=1)
            win32_input.send_mouse_button_down(button, _sender=sender, _button_down_query=lambda _: False)
            self.assertEqual(len(sender.call_args.args[0]), 1)
            sender.reset_mock()
            with self.assertRaises(win32_input.MouseButtonInUseError):
                win32_input.send_mouse_button_down(button, _sender=sender, _button_down_query=lambda _: True)
            sender.assert_not_called()

    def test_uncertain_down_and_failed_cleanup_has_specific_error(self):
        sender = mock.Mock(side_effect=[OSError("uncertain"), 0])
        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_mouse_button_down("left", _sender=sender)
        self.assertEqual(sender.call_count, 2)

    def test_zero_delivery_does_not_send_unowned_up(self):
        sender = mock.Mock(return_value=0)
        with self.assertRaises(OSError) as raised:
            win32_input.send_mouse_button_down("left", _sender=sender)
        self.assertNotIsInstance(raised.exception, win32_input.InputCleanupIncompleteError)
        self.assertEqual(sender.call_count, 1)
