"""Exercises win32_input.py's batching/rollback logic with an injected fake
``_sender`` callable, so it runs on any OS without needing ``ctypes.windll``
(which does not exist off Windows) - see the module docstring in
win32_input.py for why this dependency-injection seam exists.
"""

import unittest
from unittest import mock

from ovb_rc003 import win32_input


class RecordingSender:
    """A fake RawSender: returns a scripted "sent count" for each call (or
    the full length if the script runs out) and records every call.
    """

    def __init__(self, sent_counts=None):
        self._sent_counts = list(sent_counts or [])
        self.calls = []

    def __call__(self, events):
        self.calls.append(list(events))
        if self._sent_counts:
            return self._sent_counts.pop(0)
        return len(events)


class RaiseOnceThenRecordSender:
    """A fake RawSender simulating XRBM-020's exact scenario: the initial
    batch call raises a generic exception (delivery is now unknown, not
    "nothing landed"), and every SUBSEQUENT call (the best-effort release
    fallback) succeeds and is recorded - so a test can assert exactly which
    individual release calls the fallback made, in what order.
    """

    def __init__(self, exception=None):
        self.exception = exception or RuntimeError("simulated driver hiccup")
        self.calls = []
        self._raised = False

    def __call__(self, events):
        self.calls.append(list(events))
        if not self._raised:
            self._raised = True
            raise self.exception
        return len(events)


class LiveInputSafetyGateTests(unittest.TestCase):
    def test_real_input_backends_are_blocked_when_build_gate_is_enabled(self):
        with mock.patch.dict(
            "os.environ",
            {"RC003_DISABLE_LIVE_INPUT": "1"},
        ):
            with self.assertRaises(win32_input.Win32InputUnavailableError):
                win32_input._real_send_input_batch([(0x41, False)])
            with self.assertRaises(win32_input.Win32InputUnavailableError):
                win32_input._real_send_mouse_input_batch(
                    [(win32_input._MOUSEEVENTF_LEFTDOWN, 0)]
                )
            with self.assertRaises(win32_input.Win32InputUnavailableError):
                win32_input._real_keybd_event(0xA5, False)


class SendKeyComboDownTests(unittest.TestCase):
    def test_full_delivery_sends_one_batched_call_in_order(self):
        sender = RecordingSender()
        win32_input.send_key_combo_down(("win", "d"), _sender=sender)
        self.assertEqual(len(sender.calls), 1)
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        vk_d = win32_input.win32_keys.VK_CODES["d"]
        self.assertEqual(sender.calls[0], [(vk_win, False), (vk_d, False)])

    def test_partial_delivery_rolls_back_exactly_the_keys_that_went_down(self):
        # 2-key combo, only the first key-down "lands" (sent=1).
        sender = RecordingSender(sent_counts=[1])
        with self.assertRaises(OSError):
            win32_input.send_key_combo_down(("win", "d"), _sender=sender)
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        # Second call must be the rollback: release exactly the one key that
        # went down (win), not the one that never landed (d).
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(sender.calls[1], [(vk_win, True)])

    def test_zero_delivery_rolls_back_nothing_but_still_raises(self):
        sender = RecordingSender(sent_counts=[0])
        with self.assertRaises(OSError):
            win32_input.send_key_combo_down(("a",), _sender=sender)
        self.assertEqual(len(sender.calls), 1)  # no rollback call needed

    def test_generic_failure_after_submission_releases_every_key_individually(self):
        # XRBM-020 (fixing the XRBM-019 REPLAN gap - see
        # XRBM-019's independent review round 2): a generic
        # exception raised by the batch call itself does not prove zero
        # key-downs landed - every key must get its OWN best-effort release
        # attempt (one call each), not be skipped just because the initial
        # batch raised instead of reporting a partial count.
        sender = RaiseOnceThenRecordSender()
        with self.assertRaises(OSError):
            win32_input.send_key_combo_down(("win", "d"), _sender=sender)
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        vk_d = win32_input.win32_keys.VK_CODES["d"]
        self.assertEqual(len(sender.calls), 3)  # failed batch + 2 individual releases
        self.assertEqual(sender.calls[1], [(vk_d, True)])
        self.assertEqual(sender.calls[2], [(vk_win, True)])

    def test_generic_failure_chains_the_original_exception(self):
        original = RuntimeError("simulated driver hiccup")

        def failing_sender(events):
            raise original

        with self.assertRaises(OSError) as ctx:
            win32_input.send_key_combo_down(("a",), _sender=failing_sender)
        self.assertIs(ctx.exception.__cause__, original)

    def test_one_fallback_release_failure_does_not_skip_the_other_key(self):
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        calls = []

        def sender(events):
            calls.append(list(events))
            if len(events) > 1:
                raise RuntimeError("simulated driver hiccup")
            if events[0][0] == vk_win:
                raise RuntimeError("simulated fallback release failure")
            return 1

        # The original OSError must still surface - a fallback release
        # failure is swallowed internally, never replaces the observable
        # delivery failure.
        with self.assertRaises(OSError):
            win32_input.send_key_combo_down(("win", "d"), _sender=sender)
        # Both individual release attempts were still made despite the
        # "win" one failing.
        self.assertEqual(len(calls), 3)

    def test_zero_count_from_fallback_release_is_reported_as_incomplete_cleanup(self):
        sender = RecordingSender(sent_counts=[1, 0])

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_key_combo_down(("win", "d"), _sender=sender)

        self.assertEqual(len(sender.calls), 2)

    def test_physical_modifier_blocks_synthetic_combo_before_submission(self):
        sender = RecordingSender()
        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]

        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_key_combo_down(
                ("lctrl", "f9"),
                _sender=sender,
                _key_down_query=lambda vk: vk == lctrl,
            )

        self.assertEqual(sender.calls, [])


class SendKeyComboUpTests(unittest.TestCase):
    def test_full_delivery_releases_in_reverse_order(self):
        sender = RecordingSender()
        win32_input.send_key_combo_up(("win", "d"), _sender=sender)
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        vk_d = win32_input.win32_keys.VK_CODES["d"]
        self.assertEqual(sender.calls[0], [(vk_d, True), (vk_win, True)])

    def test_partial_delivery_retries_the_remaining_keys_then_reports_failure(self):
        # XRBM-019 review round 1 P1 #4: a partial key-up delivery still
        # gets its best-effort retry of whatever didn't land, but must now
        # be OBSERVABLE to the caller (app.py's _cleanup_once) instead of
        # silently reporting success it cannot back up.
        sender = RecordingSender(sent_counts=[1])
        with self.assertRaises(OSError):
            win32_input.send_key_combo_up(("win", "d"), _sender=sender)
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(sender.calls[1], [(vk_win, True)])

    def test_generic_failure_now_raises_after_swallowing_used_to_hide_it(self):
        # XRBM-019 review round 1 P1 #4: this used to swallow every generic
        # failure so "cleanup must survive" - but that also meant a failed
        # HOLD-mode KEY_UP left the host key physically down while the
        # caller's own state already recorded it as released. Cleanup must
        # still survive by wrapping this call (see app.py's
        # _cleanup_once/win32_input.py's own module docstring), not by this
        # function pretending the release worked.
        def failing_sender(events):
            raise RuntimeError("simulated driver hiccup")

        with self.assertRaises(OSError):
            win32_input.send_key_combo_up(("a",), _sender=failing_sender)

    def test_generic_failure_after_submission_releases_every_key_individually(self):
        # XRBM-020 (fixing the XRBM-019 REPLAN gap - see
        # XRBM-019's independent review round 2): the round-2
        # adversarial probe found this exact branch raised OSError with NO
        # rollback attempt at all - `calls=1`, containing only the failed
        # batch, for a two-key combo. Every key must now get its own
        # best-effort release attempt.
        sender = RaiseOnceThenRecordSender()
        with self.assertRaises(OSError):
            win32_input.send_key_combo_up(("win", "d"), _sender=sender)
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        vk_d = win32_input.win32_keys.VK_CODES["d"]
        self.assertEqual(len(sender.calls), 3)  # failed batch + 2 individual releases
        # send_key_combo_up already reverses key order once (releases most-
        # recently-pressed first) - the fallback reuses that same order.
        self.assertEqual(sender.calls[1], [(vk_d, True)])
        self.assertEqual(sender.calls[2], [(vk_win, True)])

    def test_generic_failure_chains_the_original_exception(self):
        original = RuntimeError("simulated driver hiccup")

        def failing_sender(events):
            raise original

        with self.assertRaises(OSError) as ctx:
            win32_input.send_key_combo_up(("a",), _sender=failing_sender)
        self.assertIs(ctx.exception.__cause__, original)

    def test_one_fallback_release_failure_does_not_skip_the_other_key(self):
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        calls = []

        def sender(events):
            calls.append(list(events))
            if len(events) > 1:
                raise RuntimeError("simulated driver hiccup")
            if events[0][0] == vk_win:
                raise RuntimeError("simulated fallback release failure")
            return 1

        with self.assertRaises(OSError):
            win32_input.send_key_combo_up(("win", "d"), _sender=sender)
        self.assertEqual(len(calls), 3)

    def test_still_raises_win32_input_unavailable_error(self):
        def unavailable_sender(events):
            raise win32_input.Win32InputUnavailableError("not on windows")

        with self.assertRaises(win32_input.Win32InputUnavailableError):
            win32_input.send_key_combo_up(("a",), _sender=unavailable_sender)

    def test_physical_modifier_is_not_released_by_synthetic_cleanup(self):
        sender = RecordingSender()
        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]
        f9 = win32_input.win32_keys.VK_CODES["f9"]

        win32_input.send_key_combo_up(
            ("lctrl", "f9"),
            _sender=sender,
            _key_down_query=lambda vk: vk == lctrl,
        )

        self.assertEqual(sender.calls, [[(f9, True)]])

    def test_physical_state_query_failure_does_not_skip_other_key_ups(self):
        sender = RecordingSender()
        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]
        f9 = win32_input.win32_keys.VK_CODES["f9"]

        def query(vk):
            if vk == lctrl:
                raise RuntimeError("physical state unavailable")
            return False

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_key_combo_up(
                ("lctrl", "f9"),
                _sender=sender,
                _key_down_query=query,
            )

        self.assertEqual(sender.calls, [[(f9, True)]])


class SendKeyComboTapTests(unittest.TestCase):
    def test_show_desktop_uses_idempotent_minimize_combo(self):
        sender = RecordingSender()

        win32_input.send_show_desktop(_sender=sender)

        vk_win = win32_input.win32_keys.VK_CODES["win"]
        vk_m = win32_input.win32_keys.VK_CODES["m"]
        self.assertEqual(
            sender.calls,
            [[(vk_win, False), (vk_m, False), (vk_m, True), (vk_win, True)]],
        )

    def test_full_delivery_is_one_batched_call_down_then_up_reversed(self):
        sender = RecordingSender()
        win32_input.send_key_combo_tap(("win", "d"), _sender=sender)
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        vk_d = win32_input.win32_keys.VK_CODES["d"]
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(
            sender.calls[0],
            [(vk_win, False), (vk_d, False), (vk_d, True), (vk_win, True)],
        )

    def test_partial_delivery_during_down_half_rolls_back_only_landed_downs(self):
        # 2-key tap => 4 events total (2 down + 2 up). Only 1 lands.
        sender = RecordingSender(sent_counts=[1])
        with self.assertRaises(OSError):
            win32_input.send_key_combo_tap(("win", "d"), _sender=sender)
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(sender.calls[1], [(vk_win, True)])

    def test_partial_delivery_during_up_half_finishes_releasing_remaining_ups(self):
        # 2-key tap => 4 events (down win, down d, up d, up win). 3 land
        # (both downs + first up); the final "up win" doesn't.
        sender = RecordingSender(sent_counts=[3])
        with self.assertRaises(OSError):
            win32_input.send_key_combo_tap(("win", "d"), _sender=sender)
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(sender.calls[1], [(vk_win, True)])

    def test_full_up_delivery_needs_no_rollback_call(self):
        sender = RecordingSender()  # always "delivers everything"
        win32_input.send_key_combo_tap(("a",), _sender=sender)
        self.assertEqual(len(sender.calls), 1)

    def test_generic_failure_after_submission_releases_every_key_individually(self):
        # XRBM-020 (fixing the XRBM-019 REPLAN gap): a generic exception
        # raised by the combined down+up batch call does not prove zero
        # events landed - every key in the tap must get its own
        # best-effort release attempt before OSError is raised.
        sender = RaiseOnceThenRecordSender()
        with self.assertRaises(OSError):
            win32_input.send_key_combo_tap(("win", "d"), _sender=sender)
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        vk_d = win32_input.win32_keys.VK_CODES["d"]
        self.assertEqual(len(sender.calls), 3)  # failed batch + 2 individual releases
        self.assertEqual(sender.calls[1], [(vk_d, True)])
        self.assertEqual(sender.calls[2], [(vk_win, True)])

    def test_generic_failure_chains_the_original_exception(self):
        original = RuntimeError("simulated driver hiccup")

        def failing_sender(events):
            raise original

        with self.assertRaises(OSError) as ctx:
            win32_input.send_key_combo_tap(("a",), _sender=failing_sender)
        self.assertIs(ctx.exception.__cause__, original)

    def test_one_fallback_release_failure_does_not_skip_the_other_key(self):
        vk_win = win32_input.win32_keys.VK_CODES["win"]
        calls = []

        def sender(events):
            calls.append(list(events))
            if len(events) > 1:
                raise RuntimeError("simulated driver hiccup")
            if events[0][0] == vk_win:
                raise RuntimeError("simulated fallback release failure")
            return 1

        with self.assertRaises(OSError):
            win32_input.send_key_combo_tap(("win", "d"), _sender=sender)
        self.assertEqual(len(calls), 3)

    def test_still_raises_win32_input_unavailable_error(self):
        def unavailable_sender(events):
            raise win32_input.Win32InputUnavailableError("not on windows")

        with self.assertRaises(win32_input.Win32InputUnavailableError):
            win32_input.send_key_combo_tap(("a",), _sender=unavailable_sender)


class MouseInputTests(unittest.TestCase):
    def test_each_button_click_is_one_down_up_batch(self):
        expected = {
            "left": (
                win32_input._MOUSEEVENTF_LEFTDOWN,
                win32_input._MOUSEEVENTF_LEFTUP,
                0,
            ),
            "right": (
                win32_input._MOUSEEVENTF_RIGHTDOWN,
                win32_input._MOUSEEVENTF_RIGHTUP,
                0,
            ),
            "middle": (
                win32_input._MOUSEEVENTF_MIDDLEDOWN,
                win32_input._MOUSEEVENTF_MIDDLEUP,
                0,
            ),
            "x1": (
                win32_input._MOUSEEVENTF_XDOWN,
                win32_input._MOUSEEVENTF_XUP,
                win32_input._XBUTTON1,
            ),
            "x2": (
                win32_input._MOUSEEVENTF_XDOWN,
                win32_input._MOUSEEVENTF_XUP,
                win32_input._XBUTTON2,
            ),
        }
        for button, (down_flag, up_flag, mouse_data) in expected.items():
            sender = RecordingSender()
            win32_input.send_mouse_button_click(button, _sender=sender)
            self.assertEqual(
                sender.calls,
                [[(down_flag, mouse_data), (up_flag, mouse_data)]],
                button,
            )

    def test_each_physical_button_hold_blocks_the_matching_synthetic_click(self):
        for button in ("left", "right", "middle", "x1", "x2"):
            sender = RecordingSender()
            with self.assertRaises(win32_input.MouseButtonInUseError):
                win32_input.send_mouse_button_click(
                    button,
                    _sender=sender,
                    _button_down_query=lambda candidate, expected=button: (
                        candidate == expected
                    ),
                )
            self.assertEqual(sender.calls, [], button)

    def test_unrelated_physical_button_does_not_block_the_requested_click(self):
        sender = RecordingSender()

        win32_input.send_mouse_button_click(
            "right",
            _sender=sender,
            _button_down_query=lambda button: button == "left",
        )

        self.assertEqual(len(sender.calls), 1)

    def test_partial_click_after_down_immediately_releases_the_button(self):
        sender = RecordingSender(sent_counts=[1])
        with self.assertRaises(OSError) as ctx:
            win32_input.send_mouse_button_click("left", _sender=sender)
        self.assertNotIsInstance(
            ctx.exception, win32_input.InputCleanupIncompleteError
        )
        self.assertEqual(
            sender.calls[1], [(win32_input._MOUSEEVENTF_LEFTUP, 0)]
        )

    def test_partial_click_retains_cleanup_error_when_release_still_fails(self):
        sender = RecordingSender(sent_counts=[1, 0])
        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_mouse_button_click("x1", _sender=sender)
        self.assertEqual(
            sender.calls[1],
            [(win32_input._MOUSEEVENTF_XUP, win32_input._XBUTTON1)],
        )

    def test_generic_click_failure_treats_delivery_as_unknown_and_releases(self):
        sender = RaiseOnceThenRecordSender()
        with self.assertRaises(OSError) as ctx:
            win32_input.send_mouse_button_click("right", _sender=sender)
        self.assertNotIsInstance(
            ctx.exception, win32_input.InputCleanupIncompleteError
        )
        self.assertEqual(
            sender.calls[1], [(win32_input._MOUSEEVENTF_RIGHTUP, 0)]
        )

    def test_mouse_up_retries_and_reports_completed_fallback(self):
        sender = RecordingSender(sent_counts=[0, 1])
        with self.assertRaises(OSError) as ctx:
            win32_input.send_mouse_button_up("middle", _sender=sender)
        self.assertNotIsInstance(
            ctx.exception, win32_input.InputCleanupIncompleteError
        )
        self.assertEqual(len(sender.calls), 2)

    def test_vertical_wheel_uses_one_windows_wheel_delta_per_click(self):
        up_sender = RecordingSender()
        down_sender = RecordingSender()
        win32_input.send_mouse_wheel(1, _sender=up_sender)
        win32_input.send_mouse_wheel(-1, _sender=down_sender)
        self.assertEqual(
            up_sender.calls,
            [[(win32_input._MOUSEEVENTF_WHEEL, win32_input._WHEEL_DELTA)]],
        )
        self.assertEqual(
            down_sender.calls,
            [[(win32_input._MOUSEEVENTF_WHEEL, -win32_input._WHEEL_DELTA)]],
        )

    def test_unknown_button_and_zero_wheel_are_rejected_before_submission(self):
        sender = RecordingSender()
        with self.assertRaises(ValueError):
            win32_input.send_mouse_button_click("unknown", _sender=sender)
        with self.assertRaises(ValueError):
            win32_input.send_mouse_wheel(0, _sender=sender)
        self.assertEqual(sender.calls, [])


class VoiceKeyComboTests(unittest.TestCase):
    def test_down_uses_virtual_key_edges_in_order(self):
        calls = []

        win32_input.send_voice_key_combo_down(
            ("ctrl", "alt", "f8"),
            _sender=lambda vk, key_up: calls.append((vk, key_up)),
        )

        self.assertEqual(
            calls,
            [
                (win32_input.win32_keys.VK_CODES["ctrl"], False),
                (win32_input.win32_keys.VK_CODES["alt"], False),
                (win32_input.win32_keys.VK_CODES["f8"], False),
            ],
        )

    def test_up_releases_virtual_key_edges_in_reverse_order(self):
        calls = []

        win32_input.send_voice_key_combo_up(
            ("ctrl", "alt", "f8"),
            _sender=lambda vk, key_up: calls.append((vk, key_up)),
        )

        self.assertEqual(
            calls,
            [
                (win32_input.win32_keys.VK_CODES["f8"], True),
                (win32_input.win32_keys.VK_CODES["alt"], True),
                (win32_input.win32_keys.VK_CODES["ctrl"], True),
            ],
        )

    def test_tap_holds_the_virtual_key_shortcut_before_releasing(self):
        calls = []

        with mock.patch.object(win32_input.time, "sleep") as sleep:
            win32_input.send_voice_key_combo_tap(
                ("ralt",), _sender=lambda vk, key_up: calls.append((vk, key_up))
            )

        vk = win32_input.win32_keys.VK_CODES["ralt"]
        self.assertEqual(calls, [(vk, False), (vk, True)])
        sleep.assert_called_once_with(0.07)

    def test_down_failure_releases_every_key_that_may_have_landed(self):
        calls = []

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if len(calls) == 3:
                raise RuntimeError("simulated voice sender failure")

        with self.assertRaises(OSError) as ctx:
            win32_input.send_voice_key_combo_down(
                ("ctrl", "alt", "f8"), _sender=sender
            )

        self.assertNotIsInstance(
            ctx.exception,
            win32_input.InputCleanupIncompleteError,
        )

        self.assertEqual(
            calls,
            [
                (win32_input.win32_keys.VK_CODES["ctrl"], False),
                (win32_input.win32_keys.VK_CODES["alt"], False),
                (win32_input.win32_keys.VK_CODES["f8"], False),
                (win32_input.win32_keys.VK_CODES["f8"], True),
                (win32_input.win32_keys.VK_CODES["alt"], True),
                (win32_input.win32_keys.VK_CODES["ctrl"], True),
            ],
        )

    def test_up_failure_retries_every_modifier_that_may_still_be_down(self):
        calls = []

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if len(calls) == 1:
                raise RuntimeError("simulated voice sender failure")

        with self.assertRaises(OSError) as ctx:
            win32_input.send_voice_key_combo_up(
                ("ctrl", "alt", "f8"), _sender=sender
            )

        self.assertNotIsInstance(
            ctx.exception,
            win32_input.InputCleanupIncompleteError,
        )

        self.assertEqual(
            calls,
            [
                (win32_input.win32_keys.VK_CODES["f8"], True),
                (win32_input.win32_keys.VK_CODES["f8"], True),
                (win32_input.win32_keys.VK_CODES["alt"], True),
                (win32_input.win32_keys.VK_CODES["ctrl"], True),
            ],
        )

    def test_up_reports_when_a_retry_still_cannot_release_the_primary_key(self):
        f8 = win32_input.win32_keys.VK_CODES["f8"]
        alt = win32_input.win32_keys.VK_CODES["alt"]
        ctrl = win32_input.win32_keys.VK_CODES["ctrl"]
        calls = []

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if vk == f8 and key_up:
                raise RuntimeError("simulated persistent F8 release failure")

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_up(
                ("ctrl", "alt", "f8"),
                _sender=sender,
            )

        self.assertEqual(
            calls,
            [
                (f8, True),
                (f8, True),
                (alt, True),
                (ctrl, True),
            ],
        )

    def test_tap_releases_keys_when_the_hold_delay_is_interrupted(self):
        calls = []

        with mock.patch.object(
            win32_input.time, "sleep", side_effect=KeyboardInterrupt
        ), self.assertRaises(KeyboardInterrupt):
            win32_input.send_voice_key_combo_tap(
                ("ralt", "space"),
                _sender=lambda vk, key_up: calls.append((vk, key_up)),
            )

        self.assertEqual(
            calls[-2:],
            [
                (win32_input.win32_keys.VK_CODES["space"], True),
                (win32_input.win32_keys.VK_CODES["ralt"], True),
            ],
        )

    def test_tap_reports_when_final_safety_release_is_incomplete(self):
        calls = []

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if key_up:
                raise RuntimeError("simulated persistent key-up failure")

        with mock.patch.object(
            win32_input.time, "sleep", side_effect=KeyboardInterrupt
        ), self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_tap(
                ("ralt", "space"),
                _sender=sender,
            )

        self.assertGreaterEqual(len(calls), 4)

    def test_tap_downgrades_prior_incomplete_error_after_final_release_succeeds(self):
        calls = []
        failed_once = {win32_input.win32_keys.VK_CODES["space"]: False}

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if key_up and vk in failed_once and not failed_once[vk]:
                failed_once[vk] = True
                raise RuntimeError("simulated transient key-up failure")

        with self.assertRaises(OSError) as ctx:
            win32_input.send_voice_key_combo_tap(
                ("ralt", "space"),
                _sender=sender,
            )

        self.assertNotIsInstance(
            ctx.exception,
            win32_input.InputCleanupIncompleteError,
        )
        self.assertEqual(
            calls[-2:],
            [
                (win32_input.win32_keys.VK_CODES["space"], True),
                (win32_input.win32_keys.VK_CODES["ralt"], True),
            ],
        )

    def test_unavailable_voice_sender_is_re_raised(self):
        def unavailable_sender(vk, key_up):
            raise win32_input.Win32InputUnavailableError("not on windows")

        with self.assertRaises(win32_input.Win32InputUnavailableError):
            win32_input.send_voice_key_combo_down(("ralt",), _sender=unavailable_sender)

    def test_voice_down_reports_when_compensating_key_up_also_fails(self):
        calls = []

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if len(calls) >= 2:
                raise RuntimeError("simulated persistent voice sender failure")

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_down(("ralt", "space"), _sender=sender)

        self.assertGreaterEqual(len(calls), 4)

    def test_physical_modifier_blocks_voice_combo_before_submission(self):
        calls = []
        lalt = win32_input.win32_keys.VK_CODES["lalt"]

        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_voice_key_combo_down(
                ("lalt", "f8"),
                _sender=lambda vk, key_up: calls.append((vk, key_up)),
                _key_down_query=lambda vk: vk == lalt,
            )

        self.assertEqual(calls, [])

    def test_voice_release_preserves_physical_modifier_and_releases_other_key(self):
        calls = []
        lalt = win32_input.win32_keys.VK_CODES["lalt"]
        f8 = win32_input.win32_keys.VK_CODES["f8"]

        win32_input.send_voice_key_combo_up(
            ("lalt", "f8"),
            _sender=lambda vk, key_up: calls.append((vk, key_up)),
            _key_down_query=lambda vk: vk == lalt,
        )

        self.assertEqual(calls, [(f8, True)])

    def test_inactive_tracker_cannot_suppress_the_matching_voice_key_up(self):
        calls = []
        ralt = win32_input.win32_keys.VK_CODES["ralt"]
        tracking = {"available": True}

        with mock.patch.object(
            win32_input.raw_input_windows,
            "physical_keyboard_tracking_available",
            return_value=False,
        ), mock.patch.object(
            win32_input.voice_key_physicalizer_windows,
            "physical_key_tracking_available",
            side_effect=lambda _vk: tracking["available"],
        ), mock.patch.object(
            win32_input.raw_input_windows,
            "physical_key_is_down",
            return_value=False,
        ), mock.patch.object(
            win32_input.raw_input_windows,
            "physical_key_is_down_before_injection",
            return_value=False,
        ), mock.patch.object(
            win32_input.voice_key_physicalizer_windows,
            "physical_key_is_down",
            return_value=False,
        ), mock.patch.object(
            win32_input,
            "_real_voice_event",
            side_effect=lambda vk, key_up: calls.append((vk, key_up)),
        ):
            win32_input.send_voice_key_combo_down(("ralt",))
            tracking["available"] = False
            win32_input.send_voice_key_combo_up(("ralt",))

        self.assertEqual(calls, [(ralt, False), (ralt, True)])

    def test_voice_physical_query_failure_still_releases_other_keys(self):
        calls = []
        lalt = win32_input.win32_keys.VK_CODES["lalt"]
        f8 = win32_input.win32_keys.VK_CODES["f8"]

        def query(vk):
            if vk == lalt:
                raise RuntimeError("physical state unavailable")
            return False

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_up(
                ("lalt", "f8"),
                _sender=lambda vk, key_up: calls.append((vk, key_up)),
                _key_down_query=query,
            )

        self.assertEqual(calls, [(f8, True)])


class MarkedVoiceEventConfirmationTests(unittest.TestCase):
    def test_real_right_alt_edge_uses_its_hook_ticket(self):
        sent = []
        ticket = mock.Mock(marker=12345)
        with mock.patch.object(
            win32_input.voice_key_physicalizer_windows,
            "begin_marked_voice_event",
            return_value=ticket,
        ) as begin, mock.patch.object(
            win32_input,
            "_real_keybd_event",
            side_effect=lambda vk, key_up, **kwargs: sent.append(
                (vk, key_up, kwargs["_extra_info"])
            ),
        ), mock.patch.object(
            win32_input.voice_key_physicalizer_windows,
            "wait_for_marked_voice_event",
            return_value=True,
        ) as wait:
            win32_input._real_voice_event(
                win32_input.win32_keys.VK_CODES["ralt"],
                False,
            )

        begin.assert_called_once_with(False)
        wait.assert_called_once_with(
            ticket,
            win32_input._VOICE_EVENT_CONFIRM_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            sent,
            [(win32_input.win32_keys.VK_CODES["ralt"], False, 12345)],
        )

    def test_right_alt_hook_timeout_is_always_incomplete_cleanup(self):
        ticket = mock.Mock(marker=12345)
        with mock.patch.object(
            win32_input.voice_key_physicalizer_windows,
            "begin_marked_voice_event",
            return_value=ticket,
        ), mock.patch.object(
            win32_input,
            "_real_keybd_event",
        ), mock.patch.object(
            win32_input.voice_key_physicalizer_windows,
            "wait_for_marked_voice_event",
            return_value=False,
        ):
            with self.assertRaises(win32_input.InputCleanupIncompleteError):
                win32_input._real_voice_event(
                    win32_input.win32_keys.VK_CODES["ralt"],
                    True,
                )

    def test_combo_keeps_confirmation_failure_even_if_retry_returns(self):
        calls = []

        def sender(_vk, key_up):
            calls.append(key_up)
            if not key_up:
                raise win32_input.InputCleanupIncompleteError("no ack")

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_down(
                ("ralt",),
                _sender=sender,
            )

        self.assertEqual(calls, [False, True])


class VolumeTests(unittest.TestCase):
    def test_volume_up_taps_the_volume_up_key(self):
        sender = RecordingSender()
        win32_input.send_volume_up(_sender=sender)
        vk = win32_input.win32_keys.VK_CODES["volume_up"]
        self.assertEqual(sender.calls[0], [(vk, False), (vk, True)])

    def test_volume_down_taps_the_volume_down_key(self):
        sender = RecordingSender()
        win32_input.send_volume_down(_sender=sender)
        vk = win32_input.win32_keys.VK_CODES["volume_down"]
        self.assertEqual(sender.calls[0], [(vk, False), (vk, True)])


if __name__ == "__main__":
    unittest.main()
