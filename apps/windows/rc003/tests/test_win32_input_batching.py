"""Exercises win32_input.py's batching/rollback logic with an injected fake
``_sender`` callable, so it runs on any OS without needing ``ctypes.windll``
(which does not exist off Windows) - see the module docstring in
win32_input.py for why this dependency-injection seam exists.
"""

import unittest
import types
from unittest import mock

from ovb_rc003 import win32_input


class RejectedInputTraceTests(unittest.TestCase):
    def test_rejection_records_key_state_without_submitting_input(self):
        trace = mock.Mock(enabled=True)
        sender = mock.Mock()
        with mock.patch.object(win32_input, "_diagnostic_trace", trace), mock.patch.object(
            win32_input.raw_input_windows, "physical_key_is_down", return_value=True
        ), mock.patch.object(
            win32_input.voice_key_physicalizer_windows, "physical_key_is_down", return_value=False
        ), mock.patch.object(
            win32_input.raw_input_windows, "_real_async_key_is_down", return_value=False
        ), mock.patch.object(
            win32_input.raw_input_windows, "physical_key_has_ambiguous_owners", return_value=False
        ), self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_key_combo_tap(
                ("ctrl", "alt", "tab"), _sender=sender, _key_down_query=lambda vk: vk == 0x11
            )
        sender.assert_not_called()
        trace.emit.assert_called_once_with(
            "input_preflight_rejected", held_vks=[0x11],
            state_after_rejection=[{
                "vk": 0x11, "raw_down": True, "hook_down": False,
                "windows_down": False, "ambiguous_owners": False,
            }],
        )

    def test_trace_failure_preserves_physical_key_protection(self):
        with mock.patch.object(win32_input, "_diagnostic_trace", mock.Mock(enabled=True)), mock.patch.object(
            win32_input.raw_input_windows, "physical_key_is_down", side_effect=RuntimeError
        ), self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input._ensure_keys_not_physically_down([0x11], lambda vk: True)


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
            with self.assertRaises(win32_input.Win32InputUnavailableError):
                win32_input._real_lock_workstation()


class DiagnosticTraceIsolationTests(unittest.TestCase):
    def tearDown(self):
        win32_input.set_diagnostic_trace(None)

    def test_trace_failure_does_not_change_send_input_result(self):
        trace = mock.Mock()
        trace.record_send_input.side_effect = RuntimeError("trace failed")
        win32_input.set_diagnostic_trace(trace)
        user32 = mock.Mock()
        user32.SendInput = mock.Mock(return_value=1)

        with mock.patch.object(
            win32_input.ctypes,
            "windll",
            types.SimpleNamespace(user32=user32),
        ), mock.patch.object(
            win32_input,
            "_require_live_input_allowed",
        ), mock.patch.object(
            win32_input,
            "_require_windows",
        ):
            sent = win32_input._real_send_input_batch([(0x41, False)])

        self.assertEqual(sent, 1)
        user32.SendInput.assert_called_once()


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
    def test_win_l_uses_the_dedicated_workstation_lock_operation(self):
        calls = []

        for win_token in ("win", "lwin", "rwin"):
            with self.subTest(win_token=win_token):
                win32_input.send_key_combo_tap(
                    (win_token, "l"),
                    _lock_sender=lambda: calls.append(win_token) or True,
                )

        self.assertEqual(calls, ["win", "lwin", "rwin"])

    def test_win_l_lock_failure_is_observable(self):
        with self.assertRaisesRegex(OSError, "did not lock"):
            win32_input.send_key_combo_tap(
                ("win", "l"),
                _lock_sender=lambda: False,
            )

    def test_injected_sender_can_still_exercise_win_l_batching_without_locking(self):
        sender = RecordingSender()

        win32_input.send_key_combo_tap(("win", "l"), _sender=sender)

        vk_win = win32_input.win32_keys.VK_CODES["win"]
        vk_l = win32_input.win32_keys.VK_CODES["l"]
        self.assertEqual(
            sender.calls,
            [[(vk_win, False), (vk_l, False), (vk_l, True), (vk_win, True)]],
        )

    def test_show_desktop_uses_idempotent_minimize_combo(self):
        sender = RecordingSender()

        win32_input.send_show_desktop(_sender=sender)

        vk_win = win32_input.win32_keys.VK_CODES["win"]
        vk_m = win32_input.win32_keys.VK_CODES["m"]
        self.assertEqual(
            sender.calls,
            [[(vk_win, False), (vk_m, False), (vk_m, True), (vk_win, True)]],
        )

    def test_app_switcher_uses_persistent_windows_switcher_combo(self):
        sender = RecordingSender()

        win32_input.send_app_switcher(_sender=sender)

        vk_ctrl = win32_input.win32_keys.VK_CODES["ctrl"]
        vk_alt = win32_input.win32_keys.VK_CODES["alt"]
        vk_tab = win32_input.win32_keys.VK_CODES["tab"]
        self.assertEqual(
            sender.calls,
            [
                [
                    (vk_ctrl, False),
                    (vk_alt, False),
                    (vk_tab, False),
                    (vk_tab, True),
                    (vk_alt, True),
                    (vk_ctrl, True),
                ]
            ],
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

    def test_unknown_button_is_rejected_before_submission(self):
        sender = RecordingSender()
        with self.assertRaises(ValueError):
            win32_input.send_mouse_button_click("unknown", _sender=sender)
        self.assertEqual(sender.calls, [])


class WeTypeVoiceKeyComboTests(unittest.TestCase):
    def test_down_sends_one_virtual_key_edge_per_call_with_80ms_gap(self):
        sender = RecordingSender()
        waits = []

        win32_input.send_wetype_voice_key_combo_down(
            ("lctrl", "lwin"),
            _sender=sender,
            _sleep=waits.append,
        )

        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]
        lwin = win32_input.win32_keys.VK_CODES["lwin"]
        self.assertEqual(sender.calls, [[(lctrl, False)], [(lwin, False)]])
        self.assertEqual(waits, [0.08])

    def test_up_releases_in_reverse_order_with_80ms_gap(self):
        sender = RecordingSender()
        waits = []

        win32_input.send_wetype_voice_key_combo_up(
            ("lctrl", "lwin"),
            _sender=sender,
            _sleep=waits.append,
        )

        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]
        lwin = win32_input.win32_keys.VK_CODES["lwin"]
        self.assertEqual(sender.calls, [[(lwin, True)], [(lctrl, True)]])
        self.assertEqual(waits, [0.08])

    def test_second_down_delivery_failure_rolls_back_the_first_key(self):
        sender = RecordingSender(sent_counts=[1, 0])

        with self.assertRaises(OSError) as ctx:
            win32_input.send_wetype_voice_key_combo_down(
                ("lctrl", "lwin"),
                _sender=sender,
                _sleep=lambda _seconds: None,
            )

        self.assertNotIsInstance(
            ctx.exception,
            win32_input.InputCleanupIncompleteError,
        )
        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]
        self.assertEqual(sender.calls[-1], [(lctrl, True)])

    def test_unknown_second_down_delivery_releases_both_possible_keys(self):
        calls = []

        def sender(events):
            calls.append(list(events))
            if len(calls) == 2:
                raise RuntimeError("unknown delivery")
            return len(events)

        with self.assertRaises(OSError) as ctx:
            win32_input.send_wetype_voice_key_combo_down(
                ("lctrl", "lwin"),
                _sender=sender,
                _sleep=lambda _seconds: None,
            )

        self.assertNotIsInstance(
            ctx.exception,
            win32_input.InputCleanupIncompleteError,
        )
        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]
        lwin = win32_input.win32_keys.VK_CODES["lwin"]
        self.assertEqual(calls[-2:], [[(lwin, True)], [(lctrl, True)]])

    def test_failed_down_rollback_reports_incomplete_cleanup(self):
        sender = RecordingSender(sent_counts=[1, 0, 0])

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_wetype_voice_key_combo_down(
                ("lctrl", "lwin"),
                _sender=sender,
                _sleep=lambda _seconds: None,
            )

    def test_up_failure_retries_every_key_before_reporting(self):
        sender = RecordingSender(sent_counts=[0, 1, 1])

        with self.assertRaises(OSError) as ctx:
            win32_input.send_wetype_voice_key_combo_up(
                ("lctrl", "lwin"),
                _sender=sender,
                _sleep=lambda _seconds: None,
            )

        self.assertNotIsInstance(
            ctx.exception,
            win32_input.InputCleanupIncompleteError,
        )
        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]
        lwin = win32_input.win32_keys.VK_CODES["lwin"]
        self.assertEqual(
            sender.calls,
            [[(lwin, True)], [(lwin, True)], [(lctrl, True)]],
        )

    def test_physical_modifier_blocks_wetype_before_submission(self):
        sender = RecordingSender()
        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]

        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_wetype_voice_key_combo_down(
                ("lctrl", "lwin"),
                _sender=sender,
                _key_down_query=lambda vk: vk == lctrl,
            )

        self.assertEqual(sender.calls, [])

    def test_interrupted_gap_releases_the_first_key(self):
        sender = RecordingSender()

        with self.assertRaises(KeyboardInterrupt):
            win32_input.send_wetype_voice_key_combo_down(
                ("lctrl", "lwin"),
                _sender=sender,
                _sleep=lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt),
            )

        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]
        self.assertEqual(sender.calls, [[(lctrl, False)], [(lctrl, True)]])


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

    def test_up_failure_attempts_each_modifier_once_and_reports_incomplete(self):
        calls = []

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if len(calls) == 1:
                raise RuntimeError("simulated voice sender failure")

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_up(
                ("ctrl", "alt", "f8"), _sender=sender
            )

        self.assertEqual(
            calls,
            [
                (win32_input.win32_keys.VK_CODES["f8"], True),
                (win32_input.win32_keys.VK_CODES["alt"], True),
                (win32_input.win32_keys.VK_CODES["ctrl"], True),
            ],
        )

    def test_up_reports_when_primary_key_release_remains_unresolved(self):
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
                (alt, True),
                (ctrl, True),
            ],
        )

    def test_incomplete_right_alt_up_is_not_retried_inside_same_release(self):
        rctrl = win32_input.win32_keys.VK_CODES["rctrl"]
        ralt = win32_input.win32_keys.VK_CODES["ralt"]
        calls = []

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if vk == ralt and key_up:
                raise win32_input.InputCleanupIncompleteError("still down")

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_up(
                ("rctrl", "ralt"),
                _sender=sender,
            )

        self.assertEqual(calls, [(ralt, True), (rctrl, True)])

    def test_mixed_incomplete_and_generic_failures_do_not_restart_release(self):
        rctrl = win32_input.win32_keys.VK_CODES["rctrl"]
        ralt = win32_input.win32_keys.VK_CODES["ralt"]
        calls = []

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if vk == ralt:
                raise win32_input.InputCleanupIncompleteError("still down")
            raise OSError("later failure")

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_up(
                ("rctrl", "ralt"),
                _sender=sender,
            )

        self.assertEqual(calls, [(ralt, True), (rctrl, True)])

    def test_tap_does_not_add_nested_retry_after_incomplete_right_alt_up(self):
        ralt = win32_input.win32_keys.VK_CODES["ralt"]
        calls = []

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if key_up:
                raise win32_input.InputCleanupIncompleteError("still down")

        with mock.patch.object(win32_input.time, "sleep"), self.assertRaises(
            win32_input.InputCleanupIncompleteError
        ):
            win32_input.send_voice_key_combo_tap(
                ("ralt",),
                _sender=sender,
            )

        self.assertEqual(calls, [(ralt, False), (ralt, True)])

    def test_partial_down_rollback_attempts_each_up_only_once(self):
        rctrl = win32_input.win32_keys.VK_CODES["rctrl"]
        ralt = win32_input.win32_keys.VK_CODES["ralt"]
        calls = []

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if vk == ralt and not key_up:
                raise win32_input.InputCleanupIncompleteError("down unknown")
            if vk == ralt and key_up:
                raise win32_input.InputCleanupIncompleteError("up unknown")

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_down(
                ("rctrl", "ralt"),
                _sender=sender,
            )

        self.assertEqual(
            calls,
            [
                (rctrl, False),
                (ralt, False),
                (ralt, True),
                (rctrl, True),
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

    def test_tap_keeps_generic_up_failure_incomplete_without_retry(self):
        calls = []
        failed_once = {win32_input.win32_keys.VK_CODES["space"]: False}

        def sender(vk, key_up):
            calls.append((vk, key_up))
            if key_up and vk in failed_once and not failed_once[vk]:
                failed_once[vk] = True
                raise RuntimeError("simulated transient key-up failure")

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_tap(
                ("ralt", "space"),
                _sender=sender,
            )

        self.assertEqual(
            calls,
            [
                (win32_input.win32_keys.VK_CODES["ralt"], False),
                (win32_input.win32_keys.VK_CODES["space"], False),
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

        with self.assertRaises(win32_input.InputCleanupIncompleteError):
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


class VoiceRightAltOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.raw = win32_input.raw_input_windows
        self.hook = win32_input.voice_key_physicalizer_windows
        self.ralt = win32_input.win32_keys.VK_CODES["ralt"]
        self.windows_down = False
        self.sent = []
        self.raw._set_physical_keyboard_tracker_active(True)
        self.hook._set_physical_tracker_active(True, _query=lambda _vk: False)
        self.addCleanup(self.raw._set_physical_keyboard_tracker_active, False)
        self.addCleanup(self.hook._set_physical_tracker_active, False)
        for module in (self.raw, self.hook):
            patcher = mock.patch.object(
                module, "_real_async_key_is_down",
                side_effect=lambda vk: vk == self.ralt and self.windows_down,
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            win32_input, "_real_voice_event", side_effect=self._send
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, vk, key_up):
        self.sent.append((vk, key_up))
        self.windows_down = not key_up

    def _physical_edge(self, key_up, *, injected=False):
        event = self.hook.KBDLLHOOKSTRUCT(
            vkCode=self.ralt,
            scanCode=0x38,
            flags=(0x81 if key_up else 0x21) | (0x10 if injected else 0),
        )
        self.hook.record_physical_key_event(
            event, self.hook.WM_KEYUP if key_up else self.hook.WM_SYSKEYDOWN
        )

    def _replay_missing_raw_release(self):
        # Live 2026-09-11 trace: native UP reached the hook, but not Raw Input.
        self._physical_edge(False)
        self.raw.record_physical_keyboard_event(
            101, vkey=0x12, make_code=0x38, flags=2, message=0x104
        )
        self._physical_edge(True)
        self._physical_edge(True, injected=True)
        self.assertTrue(self.raw.physical_key_is_down(self.ralt))
        self.assertFalse(self.hook.physical_key_is_down(self.ralt))

    def test_missing_raw_release_does_not_block_voice_down_or_its_release(self):
        self._replay_missing_raw_release()
        for _ in range(2):
            win32_input.send_voice_key_combo_down(("ralt",))
            win32_input.send_voice_key_combo_up(("ralt",))
        self.assertEqual(self.sent, [(self.ralt, False), (self.ralt, True)] * 2)
        self.assertTrue(self.raw.physical_key_is_down(self.ralt))

    def test_physical_right_alt_still_blocks_even_before_windows_updates(self):
        self._physical_edge(False)
        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_voice_key_combo_down(("ralt",))
        self.assertEqual(self.sent, [])

    def test_stale_hook_right_alt_owner_no_longer_blocks_voice(self):
        self._physical_edge(False)
        with self.hook._PHYSICAL_KEY_STATE_LOCK:
            self.hook._PHYSICAL_KEY_LAST_EDGE_AT[self.ralt] -= 1.0

        win32_input.send_voice_key_combo_down(("ralt",))
        win32_input.send_voice_key_combo_up(("ralt",))

        self.assertEqual(self.sent, [(self.ralt, False), (self.ralt, True)])
        self.assertFalse(self.hook.physical_key_is_down(self.ralt))

    def test_current_windows_hold_still_blocks_after_hook_reports_release(self):
        self._replay_missing_raw_release()
        self.windows_down = True
        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_voice_key_combo_down(("ralt",))
        self.assertEqual(self.sent, [])

    def test_unavailable_windows_state_fails_before_submission(self):
        self._replay_missing_raw_release()
        with mock.patch.object(
            self.hook, "_real_async_key_is_down", side_effect=OSError("unavailable")
        ), self.assertRaises(win32_input.Win32InputUnavailableError):
            win32_input.send_voice_key_combo_down(("ralt",))
        self.assertEqual(self.sent, [])

    def test_physical_press_during_voice_hold_is_not_released(self):
        self._replay_missing_raw_release()
        win32_input.send_voice_key_combo_down(("ralt",))
        self._physical_edge(False)
        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_up(("ralt",))
        self.assertEqual(self.sent, [(self.ralt, False)])
        self._physical_edge(True)
        win32_input.send_voice_key_combo_up(("ralt",))
        self.assertEqual(self.sent, [(self.ralt, False), (self.ralt, True)])

    def test_hook_loss_cannot_restore_stale_raw_ownership_during_cleanup(self):
        self._replay_missing_raw_release()
        win32_input.send_voice_key_combo_down(("ralt",))
        self.hook._set_physical_tracker_active(False)
        win32_input.send_voice_key_combo_up(("ralt",))
        self.assertEqual(self.sent, [(self.ralt, False), (self.ralt, True)])

    def test_down_failure_still_sends_compensating_up_with_stale_raw_state(self):
        self._replay_missing_raw_release()

        def fail_after_down(vk, key_up):
            self._send(vk, key_up)
            if not key_up:
                raise OSError("submission failed after key-down")

        with mock.patch.object(
            win32_input, "_real_voice_event", side_effect=fail_after_down
        ), self.assertRaises(OSError):
            win32_input.send_voice_key_combo_down(("ralt",))
        self.assertEqual(self.sent, [(self.ralt, False), (self.ralt, True)])

    def test_tap_uses_the_same_right_alt_ownership_rules(self):
        self._replay_missing_raw_release()
        with mock.patch.object(win32_input.time, "sleep"):
            win32_input.send_voice_key_combo_tap(("ralt",))
        self.assertEqual(self.sent, [(self.ralt, False), (self.ralt, True)])

    def test_other_voice_keys_keep_device_scoped_raw_protection(self):
        for token in ("lalt", "lctrl", "lshift", "f8"):
            with self.subTest(token=token):
                vk = win32_input.win32_keys.VK_CODES[token]
                self.raw.record_physical_keyboard_event(
                    101, vkey=vk, make_code=0, flags=0, message=0x100
                )
                with self.assertRaises(win32_input.PhysicalKeyInUseError):
                    win32_input.send_voice_key_combo_down((token,))
        self.assertEqual(self.sent, [])

    def test_ordinary_right_alt_mapping_keeps_its_existing_raw_protection(self):
        self._replay_missing_raw_release()
        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_key_combo_down(("ralt",))
        self.assertEqual(self.sent, [])

    def test_one_keyboard_release_cannot_override_another_right_alt_owner(self):
        for handle in (101, 102):
            self._physical_edge(False)
            self.raw.record_physical_keyboard_event(
                handle, vkey=0x12, make_code=0x38, flags=2, message=0x104
            )
        self._physical_edge(True)
        self.raw.record_physical_keyboard_event(
            102, vkey=0x12, make_code=0x38, flags=3, message=0x101
        )
        self.assertTrue(self.raw.physical_key_has_ambiguous_owners(self.ralt))
        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_voice_key_combo_down(("ralt",))
        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input.send_voice_key_combo_up(("ralt",))
        self.assertEqual(self.sent, [])
        self.raw.record_physical_keyboard_event(
            101, vkey=0x12, make_code=0x38, flags=3, message=0x101
        )
        self.assertFalse(self.raw.physical_key_has_ambiguous_owners(self.ralt))
        win32_input.send_voice_key_combo_down(("ralt",))
        win32_input.send_voice_key_combo_up(("ralt",))
        self.assertEqual(self.sent, [(self.ralt, False), (self.ralt, True)])

    def test_disconnecting_one_keyboard_keeps_the_other_owner_protected(self):
        for handle in (101, 102):
            self.raw.record_physical_keyboard_event(
                handle, vkey=0x12, make_code=0x38, flags=2, message=0x104
            )
        self.raw.remove_physical_keyboard_device(102)
        self.assertTrue(self.raw.physical_key_has_ambiguous_owners(self.ralt))
        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_voice_key_combo_down(("ralt",))
        self.raw.remove_physical_keyboard_device(101)
        self.assertFalse(self.raw.physical_key_has_ambiguous_owners(self.ralt))

    def test_tracker_reset_clears_multi_device_history(self):
        for clear in (
            self.raw._clear_physical_keyboard_snapshot,
            lambda: self.raw._set_physical_keyboard_tracker_active(True),
        ):
            for handle in (101, 102):
                self.raw.record_physical_keyboard_event(
                    handle, vkey=0x12, make_code=0x38, flags=2, message=0x104
                )
            clear()
            self._replay_missing_raw_release()
            self.assertFalse(self.raw.physical_key_has_ambiguous_owners(self.ralt))
            win32_input.send_voice_key_combo_down(("ralt",))
            win32_input.send_voice_key_combo_up(("ralt",))


class MarkedVoiceEventConfirmationTests(unittest.TestCase):
    def test_confirmation_trace_binds_to_dispatch_receipt_generation(self):
        trace = mock.Mock(enabled=True)
        trace.current_context.return_value = {}
        ticket = types.SimpleNamespace(
            marker=12345,
            generation=23,
            installation_epoch=41,
        )
        with mock.patch.object(
            win32_input,
            "_diagnostic_trace",
            trace,
        ), mock.patch.object(
            win32_input.diagnostic_trace,
            "foreground_context",
            return_value={},
        ), mock.patch.object(
            win32_input.voice_key_physicalizer_windows,
            "begin_marked_voice_event",
            return_value=ticket,
        ), mock.patch.object(
            win32_input,
            "_real_keybd_event",
        ), mock.patch.object(
            win32_input.voice_key_physicalizer_windows,
            "wait_for_marked_voice_event",
            return_value=True,
        ):
            win32_input._real_voice_event(
                win32_input.win32_keys.VK_CODES["ralt"],
                False,
            )

        fields = trace.emit.call_args.kwargs
        self.assertEqual(fields["physicalizer_binding"], "bound")
        self.assertEqual(fields["physicalizer_generation"], 23)
        self.assertEqual(fields["physicalizer_installation_epoch"], 41)

    def test_failure_before_dispatch_receipt_is_never_reported_as_bound(self):
        trace = mock.Mock(enabled=True)
        trace.current_context.return_value = {}
        with mock.patch.object(
            win32_input,
            "_diagnostic_trace",
            trace,
        ), mock.patch.object(
            win32_input.diagnostic_trace,
            "foreground_context",
            return_value={},
        ), mock.patch.object(
            win32_input.voice_key_physicalizer_windows,
            "begin_marked_voice_event",
            side_effect=(
                win32_input.voice_key_physicalizer_windows.
                VoiceKeyPhysicalizerUnavailableError("unavailable")
            ),
        ), self.assertRaises(win32_input.Win32InputUnavailableError):
            win32_input._real_voice_event(
                win32_input.win32_keys.VK_CODES["ralt"],
                False,
            )

        fields = trace.emit.call_args.kwargs
        self.assertEqual(fields["physicalizer_binding"], "unbound")
        self.assertEqual(fields["physicalizer_generation"], -1)
        self.assertEqual(fields["physicalizer_installation_epoch"], -1)

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
        ), mock.patch.object(
            win32_input.voice_key_physicalizer_windows,
            "mark_required_confirmation_failed",
            return_value=(False, mock.Mock()),
        ) as mark_failed:
            with self.assertRaises(win32_input.InputCleanupIncompleteError):
                win32_input._real_voice_event(
                    win32_input.win32_keys.VK_CODES["ralt"],
                    True,
                )

        mark_failed.assert_called_once_with(ticket)

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


class RightAltReleasePostconditionTests(unittest.TestCase):
    def setUp(self):
        self.ralt = win32_input.win32_keys.VK_CODES["ralt"]
        self.confirmation = mock.Mock(
            generation=17,
            installation_epoch=23,
        )

    @staticmethod
    def _guard(revision=1):
        return win32_input.voice_key_physicalizer_windows.PhysicalReleaseGuard(
            generation=17,
            installation_epoch=23,
            callback_revision=revision,
        )

    @staticmethod
    def _clock():
        value = {"now": 0.0}

        def tick():
            value["now"] += 0.1
            return value["now"]

        return tick

    def test_valid_delayed_release_is_confirmed_without_another_input_edge(self):
        states = iter((True, False))
        win32_input._ensure_right_alt_release_completed(
            self.ralt,
            self.confirmation,
            _state_query=lambda _vk: next(states),
            _guard_query=lambda _vk: self._guard(),
            _ambiguous_owner_query=lambda _vk: False,
            _sleep=lambda _seconds: None,
            _clock=iter((0.0, 0.01, 0.02, 0.03)).__next__,
        )

    def test_still_down_retains_release_debt_without_hidden_retry(self):
        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input._ensure_right_alt_release_completed(
                self.ralt,
                self.confirmation,
                _state_query=lambda _vk: True,
                _guard_query=lambda _vk: self._guard(),
                _ambiguous_owner_query=lambda _vk: False,
                _sleep=lambda _seconds: None,
                _clock=self._clock(),
            )

    def test_unknown_state_observation_retains_release_debt(self):
        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input._ensure_right_alt_release_completed(
                self.ralt,
                self.confirmation,
                _state_query=lambda _vk: None,
                _guard_query=lambda _vk: self._guard(),
                _ambiguous_owner_query=lambda _vk: False,
                _sleep=lambda _seconds: None,
                _clock=self._clock(),
            )

    def test_tracker_replacement_during_observation_retains_release_debt(self):
        guards = iter((self._guard(1), self._guard(2)))
        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input._ensure_right_alt_release_completed(
                self.ralt,
                self.confirmation,
                _state_query=lambda _vk: False,
                _guard_query=lambda _vk: next(guards),
                _ambiguous_owner_query=lambda _vk: False,
                _sleep=lambda _seconds: None,
                _clock=self._clock(),
            )

    def test_ambiguous_device_owner_retains_release_debt(self):
        with self.assertRaises(win32_input.InputCleanupIncompleteError):
            win32_input._ensure_right_alt_release_completed(
                self.ralt,
                self.confirmation,
                _state_query=lambda _vk: False,
                _guard_query=lambda _vk: self._guard(),
                _ambiguous_owner_query=lambda _vk: True,
                _sleep=lambda _seconds: None,
                _clock=self._clock(),
            )

    def test_native_zero_is_unknown_when_foreground_access_is_unavailable(self):
        query = mock.Mock(return_value=0)
        self.assertIsNone(
            win32_input._real_async_key_state_observation(
                self.ralt,
                _context_query=lambda: None,
                _query=query,
            )
        )
        query.assert_not_called()

    def test_native_zero_is_unknown_when_access_context_changes(self):
        contexts = iter(
            (
                ("Default", 10, 20, 8192, 8192),
                ("Default", 10, 20, 8192, 4096),
            )
        )
        self.assertIsNone(
            win32_input._real_async_key_state_observation(
                self.ralt,
                _context_query=lambda: next(contexts),
                _query=lambda _vk: 0,
            )
        )

    def test_native_zero_is_valid_up_in_stable_access_context(self):
        context = ("Default", 10, 20, 8192, 8192)
        self.assertFalse(
            win32_input._real_async_key_state_observation(
                self.ralt,
                _context_query=lambda: context,
                _query=lambda _vk: 0,
            )
        )

    def test_production_context_rejects_higher_integrity_foreground(self):
        self.assertIsNone(
            win32_input._real_key_state_query_context(
                _desktop_query=lambda: "Default",
                _current_integrity_query=lambda: 8192,
                _foreground_query=lambda: (10, 20, 12288),
            )
        )

    def test_production_context_rejects_missing_required_desktop_access(self):
        self.assertIsNone(
            win32_input._real_key_state_query_context(
                _desktop_query=lambda: None,
                _current_integrity_query=lambda: 8192,
                _foreground_query=lambda: (10, 20, 8192),
            )
        )

    def test_desktop_probe_requests_key_state_access_rights(self):
        user32 = mock.Mock()
        kernel32 = mock.Mock()
        user32.OpenInputDesktop.return_value = 101
        user32.GetThreadDesktop.return_value = 102
        user32.CloseDesktop.return_value = True
        kernel32.GetCurrentThreadId.return_value = 7

        def load_library(name, **_kwargs):
            return user32 if name == "user32" else kernel32

        with mock.patch.object(
            win32_input.ctypes,
            "WinDLL",
            side_effect=load_library,
        ), mock.patch.object(
            win32_input,
            "_require_windows",
        ), mock.patch.object(
            win32_input,
            "_require_live_input_allowed",
        ), mock.patch.object(
            win32_input,
            "_desktop_name",
            side_effect=("Default", "Default"),
        ):
            self.assertEqual(
                win32_input._real_input_desktop_name_with_key_state_access(),
                "Default",
            )

        user32.OpenInputDesktop.assert_called_once_with(
            0,
            False,
            0x0001 | 0x0008 | 0x0010,
        )
        user32.CloseDesktop.assert_called_once_with(101)

    def test_production_context_accepts_verified_equal_integrity(self):
        self.assertEqual(
            win32_input._real_key_state_query_context(
                _desktop_query=lambda: "Default",
                _current_integrity_query=lambda: 8192,
                _foreground_query=lambda: (10, 20, 8192),
            ),
            ("Default", 10, 20, 8192, 8192),
        )

    def test_draining_tracker_can_validate_owned_cleanup_up(self):
        physicalizer = win32_input.voice_key_physicalizer_windows
        physicalizer._set_physical_tracker_active(
            True,
            _query=lambda _vk: False,
        )
        failed = physicalizer.begin_marked_voice_event(False)
        try:
            applied, _snapshot = physicalizer.mark_required_confirmation_failed(
                failed
            )
            self.assertTrue(applied)
            release = physicalizer.begin_marked_voice_event(True)
            event = physicalizer.KBDLLHOOKSTRUCT(
                vkCode=physicalizer.VK_RMENU,
                scanCode=0x38,
                flags=physicalizer.LLKHF_INJECTED,
                time=0,
                dwExtraInfo=release.marker,
            )
            self.assertIs(
                physicalizer.physicalize_injected_event(event, True),
                release,
            )
            physicalizer.complete_marked_voice_event(
                release,
                downstream_result=0,
            )
            self.assertTrue(
                physicalizer.wait_for_marked_voice_event(release, 0.01)
            )

            win32_input._ensure_right_alt_release_completed(
                self.ralt,
                release,
                _state_query=lambda _vk: False,
                _ambiguous_owner_query=lambda _vk: False,
                _sleep=lambda _seconds: None,
                _clock=self._clock(),
            )
            with self.assertRaises(
                physicalizer.VoiceKeyPhysicalizerUnavailableError
            ):
                physicalizer.begin_marked_voice_event(False)
        finally:
            physicalizer.cancel_marked_voice_event(failed)
            physicalizer._set_physical_tracker_active(False)

    def test_real_voice_up_waits_for_release_postcondition(self):
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
            return_value=True,
        ), mock.patch.object(
            win32_input,
            "_ensure_right_alt_release_completed",
        ) as ensure:
            win32_input._real_voice_event(self.ralt, True)

        ensure.assert_called_once_with(self.ralt, ticket)

    def test_failed_release_postcondition_propagates_incomplete_cleanup(self):
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
            return_value=True,
        ), mock.patch.object(
            win32_input,
            "_ensure_right_alt_release_completed",
            side_effect=win32_input.InputCleanupIncompleteError("still down"),
        ):
            with self.assertRaises(win32_input.InputCleanupIncompleteError):
                win32_input._real_voice_event(self.ralt, True)


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
