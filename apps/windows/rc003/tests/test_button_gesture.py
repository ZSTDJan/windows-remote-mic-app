import unittest
import threading

from ovb_rc003.button_gesture import (
    ButtonGestureDispatcher,
    ButtonGestureRecognizer,
    ButtonTrigger,
    GestureCommand,
)


class ButtonGestureRecognizerTests(unittest.TestCase):
    def test_single_click_is_delayed_only_when_double_click_is_configured(self):
        recognizer = ButtonGestureRecognizer()

        commands = recognizer.press(
            "up", recognizes_double_click=True, recognizes_long_press=False
        )
        self.assertEqual(commands, [])
        commands = recognizer.release("up")
        self.assertEqual(
            commands,
            [
                GestureCommand.schedule_double_click_timeout("up"),
            ],
        )
        self.assertEqual(
            recognizer.double_click_timed_out("up"),
            [GestureCommand.trigger("up", ButtonTrigger.SINGLE_CLICK)],
        )

    def test_second_press_converts_pending_single_to_double_click(self):
        recognizer = ButtonGestureRecognizer()
        recognizer.press("ok", recognizes_double_click=True, recognizes_long_press=True)
        recognizer.release("ok")

        self.assertEqual(
            recognizer.press(
                "ok", recognizes_double_click=True, recognizes_long_press=True
            ),
            [
                GestureCommand.cancel_double_click_timeout("ok"),
                GestureCommand.schedule_long_press_timeout("ok"),
            ],
        )
        self.assertEqual(
            recognizer.release("ok"),
            [
                GestureCommand.cancel_long_press_timeout("ok"),
                GestureCommand.trigger("ok", ButtonTrigger.DOUBLE_CLICK),
            ],
        )

    def test_long_press_suppresses_single_click(self):
        recognizer = ButtonGestureRecognizer()
        recognizer.press("power", recognizes_double_click=False, recognizes_long_press=True)

        self.assertEqual(
            recognizer.long_press_timed_out("power"),
            [GestureCommand.trigger("power", ButtonTrigger.LONG_PRESS)],
        )
        self.assertEqual(
            recognizer.release("power"),
            [GestureCommand.cancel_long_press_timeout("power")],
        )

    def test_unconfigured_button_triggers_single_click_on_release(self):
        recognizer = ButtonGestureRecognizer()
        recognizer.press("back", recognizes_double_click=False, recognizes_long_press=False)
        self.assertEqual(
            recognizer.release("back"),
            [GestureCommand.trigger("back", ButtonTrigger.SINGLE_CLICK)],
        )


class _FakeTimer:
    def __init__(self, callback, *, start_error=None, cancel_error=None):
        self.callback = callback
        self.start_error = start_error
        self.cancel_error = cancel_error
        self.cancelled = False
        self.started = False

    def start(self):
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def cancel(self):
        self.cancelled = True
        if self.cancel_error is not None:
            raise self.cancel_error

    def fire(self):
        if not self.cancelled:
            self.callback()


class ButtonGestureDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.timers = []
        self.triggers = []

        def timer_factory(_delay, callback):
            timer = _FakeTimer(callback)
            self.timers.append(timer)
            return timer

        self.dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                (button == "up" and trigger == ButtonTrigger.SINGLE_CLICK)
                or (button == "ok" and trigger == ButtonTrigger.DOUBLE_CLICK)
            ),
            is_repeatable=lambda button: button == "up",
            on_trigger=lambda button, trigger: self.triggers.append((button, trigger)),
            timer_factory=timer_factory,
        )

    def test_simple_button_emits_once_and_does_not_duplicate_on_repeat_press(self):
        self.dispatcher.press("up")
        self.dispatcher.press("up")
        self.assertEqual(self.triggers, [("up", ButtonTrigger.SINGLE_CLICK)])
        self.dispatcher.release("up")

    def test_double_click_timer_resolves_single_click(self):
        self.dispatcher.press("ok")
        self.dispatcher.release("ok")
        self.assertTrue(self.dispatcher.has_active_gestures())
        self.assertEqual(self.triggers, [])
        self.assertEqual(len(self.timers), 1)

        self.timers[0].fire()
        self.assertEqual(self.triggers, [("ok", ButtonTrigger.SINGLE_CLICK)])
        self.assertFalse(self.dispatcher.has_active_gestures())

    def test_delayed_callback_keeps_mapping_activity_reserved_until_it_returns(self):
        entered = threading.Event()
        release = threading.Event()

        def blocking_trigger(button_id, trigger):
            entered.set()
            release.wait(1.0)
            self.triggers.append((button_id, trigger))

        self.dispatcher._on_trigger = blocking_trigger
        self.dispatcher.press("ok")
        self.dispatcher.release("ok")

        worker = threading.Thread(target=self.timers[0].fire)
        worker.start()
        self.assertTrue(entered.wait(1.0))
        self.assertTrue(self.dispatcher.has_active_gestures())

        release.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.dispatcher.has_active_gestures())

    def test_reset_does_not_release_a_callback_reservation_before_callback_exit(self):
        entered = threading.Event()
        release = threading.Event()

        def blocking_trigger(_button_id, _trigger):
            entered.set()
            release.wait(1.0)

        self.dispatcher._on_trigger = blocking_trigger
        self.dispatcher.press("ok")
        self.dispatcher.release("ok")
        callback_worker = threading.Thread(target=self.timers[0].fire)
        callback_worker.start()
        self.assertTrue(entered.wait(1.0))

        reset_worker = threading.Thread(target=self.dispatcher.reset)
        reset_worker.start()
        self.assertTrue(self.dispatcher.has_active_gestures())

        release.set()
        callback_worker.join(1.0)
        reset_worker.join(1.0)
        self.assertFalse(callback_worker.is_alive())
        self.assertFalse(reset_worker.is_alive())
        self.assertFalse(self.dispatcher.has_active_gestures())

    def test_button_callbacks_never_run_concurrently(self):
        first_entered = threading.Event()
        allow_first_to_finish = threading.Event()
        second_entered = threading.Event()
        callback_order = []

        def on_trigger(button_id, _trigger):
            callback_order.append(f"{button_id}:start")
            if button_id == "up":
                first_entered.set()
                allow_first_to_finish.wait(1.0)
            else:
                second_entered.set()
            callback_order.append(f"{button_id}:end")

        dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda _button, trigger: (
                trigger == ButtonTrigger.SINGLE_CLICK
            ),
            is_repeatable=lambda _button: False,
            on_trigger=on_trigger,
            hold_timer_factory=lambda _delay, callback: _FakeTimer(callback),
        )
        first = threading.Thread(target=lambda: dispatcher.press("up"))
        second = threading.Thread(target=lambda: dispatcher.press("down"))
        try:
            first.start()
            self.assertTrue(first_entered.wait(1.0))
            second.start()
            self.assertFalse(second_entered.wait(0.1))
        finally:
            allow_first_to_finish.set()
            first.join(1.0)
            second.join(1.0)
            dispatcher.release("up")
            dispatcher.release("down")

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(
            callback_order,
            ["up:start", "up:end", "down:start", "down:end"],
        )

    def test_immediate_hold_remains_active_until_release(self):
        self.dispatcher.press("up")
        self.assertTrue(self.dispatcher.has_active_gestures())

        self.dispatcher.release("up")

        self.assertFalse(self.dispatcher.has_active_gestures())

    def test_double_click_emits_only_double_action(self):
        self.dispatcher.press("ok")
        self.dispatcher.release("ok")
        first_timer = self.timers[0]
        self.dispatcher.press("ok")
        self.dispatcher.release("ok")
        first_timer.fire()
        self.assertEqual(self.triggers, [("ok", ButtonTrigger.DOUBLE_CLICK)])

    def test_long_press_emits_long_action_and_release_emits_no_single(self):
        self.dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "ok" and trigger == ButtonTrigger.LONG_PRESS
            ),
            is_repeatable=lambda button: False,
            on_trigger=lambda button, trigger: self.triggers.append((button, trigger)),
            timer_factory=lambda delay, callback: self._new_timer(callback),
        )
        self.dispatcher.press("ok")
        self.assertEqual(len(self.timers), 1)
        self.timers[0].fire()
        self.assertEqual(self.triggers, [("ok", ButtonTrigger.LONG_PRESS)])
        self.dispatcher.release("ok")
        self.assertEqual(self.triggers, [("ok", ButtonTrigger.LONG_PRESS)])

    def test_cancelled_double_click_timer_cannot_fire_after_reset(self):
        self.dispatcher.press("ok")
        self.dispatcher.release("ok")
        stale_callback = self.timers[0].callback

        self.dispatcher.reset()
        stale_callback()

        self.assertEqual(self.triggers, [])

    def test_cancelled_repeat_timer_cannot_fire_after_reset(self):
        self.dispatcher.press("up")
        stale_callback = self.timers[0].callback

        self.dispatcher.reset()
        stale_callback()

        self.assertEqual(self.triggers, [("up", ButtonTrigger.SINGLE_CLICK)])

    def test_cancel_one_button_preserves_another_active_repeat(self):
        repeat_timers = []
        hold_timers = []
        dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button in {"up", "right"}
                and trigger == ButtonTrigger.SINGLE_CLICK
            ),
            is_repeatable=lambda button: button in {"up", "right"},
            on_trigger=lambda button, trigger: self.triggers.append(
                (button, trigger)
            ),
            timer_factory=lambda _delay, callback: repeat_timers.append(
                _FakeTimer(callback)
            )
            or repeat_timers[-1],
            hold_timer_factory=lambda _delay, callback: hold_timers.append(
                _FakeTimer(callback)
            )
            or hold_timers[-1],
        )
        dispatcher.press("up")
        dispatcher.press("right")

        dispatcher.cancel_buttons({"right"})
        repeat_timers[0].fire()

        self.assertEqual(
            self.triggers,
            [
                ("up", ButtonTrigger.SINGLE_CLICK),
                ("right", ButtonTrigger.SINGLE_CLICK),
                ("up", ButtonTrigger.SINGLE_CLICK),
            ],
        )
        self.assertFalse(repeat_timers[0].cancelled)
        self.assertTrue(repeat_timers[1].cancelled)
        self.assertTrue(dispatcher.has_active_gestures())
        dispatcher.release("up")
        self.assertFalse(dispatcher.has_active_gestures())

    def test_cancel_rejects_a_timeout_callback_already_waiting_at_the_gate(self):
        reserved = threading.Event()
        original_reserve = self.dispatcher._reserve_callbacks_locked

        def reserve(callbacks):
            reservation = original_reserve(callbacks)
            if reservation is not None:
                reserved.set()
            return reservation

        self.dispatcher._reserve_callbacks_locked = reserve
        self.dispatcher.press("ok")
        self.dispatcher.release("ok")
        callback_worker = threading.Thread(target=self.timers[0].callback)
        cancel_worker = threading.Thread(
            target=lambda: self.dispatcher.cancel_buttons({"ok"})
        )

        self.dispatcher._callback_lock.acquire()
        try:
            callback_worker.start()
            self.assertTrue(reserved.wait(1.0))
            cancel_worker.start()
            deadline = threading.Event()
            for _index in range(100):
                with self.dispatcher._lock:
                    if self.dispatcher._button_generations.get("ok") == 1:
                        deadline.set()
                        break
                threading.Event().wait(0.005)
            self.assertTrue(deadline.is_set())
        finally:
            self.dispatcher._callback_lock.release()

        callback_worker.join(1.0)
        cancel_worker.join(1.0)
        self.assertFalse(callback_worker.is_alive())
        self.assertFalse(cancel_worker.is_alive())
        self.assertEqual(self.triggers, [])
        self.assertFalse(self.dispatcher.has_active_gestures())

    def test_stale_repeat_timer_cannot_attach_to_a_new_hold(self):
        self.dispatcher.press("up")
        stale_callback = self.timers[0].callback
        self.dispatcher.release("up")
        self.dispatcher.press("up")

        stale_callback()

        self.assertEqual(
            self.triggers,
            [
                ("up", ButtonTrigger.SINGLE_CLICK),
                ("up", ButtonTrigger.SINGLE_CLICK),
            ],
        )
        self.assertEqual(len(self.timers), 2)

    def test_stale_long_timer_cannot_trigger_a_new_hold_early(self):
        self.dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "ok" and trigger == ButtonTrigger.LONG_PRESS
            ),
            is_repeatable=lambda button: False,
            on_trigger=lambda button, trigger: self.triggers.append((button, trigger)),
            timer_factory=lambda delay, callback: self._new_timer(callback),
        )
        self.dispatcher.press("ok")
        stale_callback = self.timers[0].callback
        self.dispatcher.release("ok")
        self.dispatcher.press("ok")

        stale_callback()

        self.assertEqual(self.triggers, [("ok", ButtonTrigger.SINGLE_CLICK)])
        self.assertEqual(len(self.timers), 2)

    def test_release_cancels_repeat_while_action_callback_is_blocked(self):
        callback_started = threading.Event()
        allow_callback_to_finish = threading.Event()
        idle_calls = []

        def on_trigger(button, trigger):
            self.triggers.append((button, trigger))
            if len(self.triggers) > 1:
                callback_started.set()
                allow_callback_to_finish.wait(1.0)

        self.dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "up" and trigger == ButtonTrigger.SINGLE_CLICK
            ),
            is_repeatable=lambda button: button == "up",
            on_trigger=on_trigger,
            on_idle=lambda: idle_calls.append("idle"),
            timer_factory=lambda delay, callback: self._new_timer(callback),
        )
        self.dispatcher.press("up")
        repeat_thread = threading.Thread(target=self.timers[0].callback)
        repeat_thread.start()
        self.assertTrue(callback_started.wait(1.0))

        release_finished = threading.Event()
        release_thread = threading.Thread(
            target=lambda: (self.dispatcher.release("up"), release_finished.set())
        )
        release_thread.start()
        self.assertTrue(release_finished.wait(0.5))
        allow_callback_to_finish.set()
        repeat_thread.join(1.0)
        release_thread.join(1.0)

        self.assertFalse(repeat_thread.is_alive())
        self.assertFalse(release_thread.is_alive())
        self.assertEqual(len(self.timers), 1)
        self.assertEqual(idle_calls, ["idle"])

    def test_repeat_stops_if_the_current_mapping_is_no_longer_repeatable(self):
        repeatable = {"up": True}
        self.dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "up" and trigger == ButtonTrigger.SINGLE_CLICK
            ),
            is_repeatable=lambda button: repeatable.get(button, False),
            on_trigger=lambda button, trigger: self.triggers.append((button, trigger)),
            timer_factory=lambda delay, callback: self._new_timer(callback),
        )
        self.dispatcher.press("up")
        repeatable["up"] = False

        self.timers[0].fire()

        self.assertEqual(self.triggers, [("up", ButtonTrigger.SINGLE_CLICK)])
        self.assertEqual(len(self.timers), 1)

    def test_repeat_is_not_rescheduled_if_mapping_changes_during_callback(self):
        repeatable = {"up": True}

        def on_trigger(button, trigger):
            self.triggers.append((button, trigger))
            if len(self.triggers) > 1:
                repeatable[button] = False

        self.dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "up" and trigger == ButtonTrigger.SINGLE_CLICK
            ),
            is_repeatable=lambda button: repeatable.get(button, False),
            on_trigger=on_trigger,
            timer_factory=lambda delay, callback: self._new_timer(callback),
        )
        self.dispatcher.press("up")

        self.timers[0].fire()

        self.assertEqual(
            self.triggers,
            [
                ("up", ButtonTrigger.SINGLE_CLICK),
                ("up", ButtonTrigger.SINGLE_CLICK),
            ],
        )
        self.assertEqual(len(self.timers), 1)

    def test_double_click_timer_creation_failure_resolves_one_single_click(self):
        dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "ok"
                and trigger
                in {ButtonTrigger.SINGLE_CLICK, ButtonTrigger.DOUBLE_CLICK}
            ),
            is_repeatable=lambda _button: False,
            on_trigger=lambda button, trigger: self.triggers.append((button, trigger)),
            timer_factory=lambda _delay, _callback: (_ for _ in ()).throw(
                RuntimeError("timer unavailable")
            ),
            hold_timer_factory=lambda _delay, callback: _FakeTimer(callback),
        )

        dispatcher.press("ok")
        dispatcher.release("ok")

        self.assertEqual(self.triggers, [("ok", ButtonTrigger.SINGLE_CLICK)])
        self.assertFalse(dispatcher.has_active_gestures())

    def test_long_press_timer_start_failure_blocks_until_release_without_click(self):
        dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "ok"
                and trigger
                in {ButtonTrigger.SINGLE_CLICK, ButtonTrigger.LONG_PRESS}
            ),
            is_repeatable=lambda _button: False,
            on_trigger=lambda button, trigger: self.triggers.append((button, trigger)),
            timer_factory=lambda _delay, callback: _FakeTimer(
                callback,
                start_error=RuntimeError("timer start failed"),
            ),
            hold_timer_factory=lambda _delay, callback: _FakeTimer(callback),
        )

        dispatcher.press("ok")
        dispatcher.press("ok")
        self.assertEqual(self.triggers, [])
        self.assertFalse(dispatcher.has_active_gestures())

        dispatcher.release("ok")
        self.assertFalse(dispatcher.has_active_gestures())

    def test_initial_repeat_timer_creation_failure_keeps_only_initial_click(self):
        dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "up" and trigger == ButtonTrigger.SINGLE_CLICK
            ),
            is_repeatable=lambda button: button == "up",
            on_trigger=lambda button, trigger: self.triggers.append((button, trigger)),
            timer_factory=lambda _delay, _callback: (_ for _ in ()).throw(
                RuntimeError("timer unavailable")
            ),
            hold_timer_factory=lambda _delay, callback: _FakeTimer(callback),
        )

        dispatcher.press("up")
        dispatcher.press("up")
        dispatcher.release("up")

        self.assertEqual(self.triggers, [("up", ButtonTrigger.SINGLE_CLICK)])
        self.assertFalse(dispatcher.has_active_gestures())

    def test_repeat_reschedule_failure_stops_repeating_but_releases_cleanly(self):
        timers = []

        def timer_factory(_delay, callback):
            if timers:
                raise RuntimeError("repeat reschedule failed")
            timer = _FakeTimer(callback)
            timers.append(timer)
            return timer

        dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "up" and trigger == ButtonTrigger.SINGLE_CLICK
            ),
            is_repeatable=lambda button: button == "up",
            on_trigger=lambda button, trigger: self.triggers.append((button, trigger)),
            timer_factory=timer_factory,
            hold_timer_factory=lambda _delay, callback: _FakeTimer(callback),
        )

        dispatcher.press("up")
        timers[0].fire()
        dispatcher.release("up")

        self.assertEqual(
            self.triggers,
            [
                ("up", ButtonTrigger.SINGLE_CLICK),
                ("up", ButtonTrigger.SINGLE_CLICK),
            ],
        )
        self.assertFalse(dispatcher.has_active_gestures())

    def test_hold_guard_start_failure_never_leaves_a_repeat_or_hold_latched(self):
        dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "up" and trigger == ButtonTrigger.SINGLE_CLICK
            ),
            is_repeatable=lambda button: button == "up",
            on_trigger=lambda button, trigger: self.triggers.append((button, trigger)),
            timer_factory=lambda _delay, callback: _FakeTimer(callback),
            hold_timer_factory=lambda _delay, callback: _FakeTimer(
                callback,
                start_error=RuntimeError("hold guard unavailable"),
            ),
        )

        dispatcher.press("up")
        dispatcher.press("up")
        dispatcher.release("up")

        self.assertEqual(self.triggers, [("up", ButtonTrigger.SINGLE_CLICK)])
        self.assertFalse(dispatcher.has_active_gestures())

    def test_timer_cancel_failure_still_clears_all_dispatcher_state(self):
        timers = []

        def timer_factory(_delay, callback):
            timer = _FakeTimer(
                callback,
                cancel_error=RuntimeError("cancel failed"),
            )
            timers.append(timer)
            return timer

        dispatcher = ButtonGestureDispatcher(
            is_action_configured=lambda button, trigger: (
                button == "ok" and trigger == ButtonTrigger.DOUBLE_CLICK
            ),
            is_repeatable=lambda _button: False,
            on_trigger=lambda button, trigger: self.triggers.append((button, trigger)),
            timer_factory=timer_factory,
            hold_timer_factory=timer_factory,
        )

        dispatcher.press("ok")
        dispatcher.release("ok")
        dispatcher.reset()
        for timer in timers:
            timer.callback()

        self.assertEqual(self.triggers, [])
        self.assertFalse(dispatcher.has_active_gestures())

    def _new_timer(self, callback):
        timer = _FakeTimer(callback)
        self.timers.append(timer)
        return timer


if __name__ == "__main__":
    unittest.main()
