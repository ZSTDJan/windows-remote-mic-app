import logging
import threading
import time
import unittest
from unittest import mock

from ovb_rc003.chatterfly_control_windows import ChatterflyVoiceControl, Target


class Transport:
    def __init__(self):
        self.target = Target(21, 41, 51, 61)
        self.windows = frozenset()
        self.running = True
        self.commands = []
        self.closed = []
        self.on_send = lambda command: None

    def discover(self):
        return self.target

    def alive(self, target):
        return self.running and target == self.target

    def visible(self, target):
        return self.windows

    def send(self, target, command):
        assert self.alive(target)
        self.commands.append((target, command))
        self.on_send(command)

    def close(self, target):
        self.closed.append(target)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport()
        self.states = []
        self.ready = threading.Event()
        def state(generation, value):
            self.states.append((generation, value))
            if value == "ready":
                self.ready.set()
        self.control = ChatterflyVoiceControl(logger=mock.Mock(), transport=self.transport, on_state=state)
        self.control.POLL_SECONDS = .001
        self.control.START_SECONDS = .12
        self.control.STOP_SECONDS = .12
        self.addCleanup(self.finish)

    def finish(self):
        self.transport.running = False
        self.control.stop()
        if self.control.thread:
            self.control.thread.join(1)
            self.assertFalse(self.control.thread.is_alive())

    def await_finished(self):
        self.control.thread.join(1)
        self.assertFalse(self.control.thread.is_alive())

    def auto_visibility(self, command):
        self.transport.windows = frozenset({101}) if command == 2 else frozenset()

    def test_real_ready_requires_new_visible_window_and_stop_is_explicit_once(self):
        self.assertTrue(self.control.start())
        self.assertFalse(self.ready.wait(.01))
        self.assertEqual(self.control.activity(), ())
        self.transport.windows = frozenset({101})
        self.assertTrue(self.ready.wait(.5))
        self.assertEqual(self.control.activity()[0].identity, ("ChatterflyVoiceIpc", 51, 41, 21, 1))
        self.transport.on_send = self.auto_visibility
        self.control.stop()
        self.await_finished()
        self.assertTrue(self.control.stop())
        self.assertEqual([command for _, command in self.transport.commands], [2, 3])
        self.assertFalse(self.control.cleanup_pending)

    def test_preexisting_manual_recording_is_never_started_or_stopped(self):
        self.transport.windows = frozenset({101})
        self.assertFalse(self.control.start())
        self.assertTrue(self.control.stop())
        self.assertEqual(self.transport.commands, [])

    def test_cancel_before_dispatch_sends_nothing(self):
        self.assertFalse(self.control.start(cancelled=lambda: True))
        self.assertEqual(self.transport.commands, [])

    def test_cancel_while_starting_waits_for_late_bubble_then_closes_it(self):
        self.assertTrue(self.control.start())
        self.control.stop()
        self.transport.on_send = self.auto_visibility
        self.transport.windows = frozenset({101})
        self.await_finished()
        self.assertFalse(self.ready.is_set())
        self.assertEqual([command for _, command in self.transport.commands], [2, 3])

    def test_start_timeout_does_not_stop_an_unobserved_session(self):
        self.assertTrue(self.control.start())
        self.await_finished()
        self.assertEqual(self.states, [(1, "failed")])
        self.assertEqual([command for _, command in self.transport.commands], [2])

    def test_replaced_process_cannot_receive_cleanup_for_previous_owner(self):
        self.transport.on_send = self.auto_visibility
        self.control.start()
        self.assertTrue(self.ready.wait(.5))
        self.transport.target = Target(21, 41, 99, 100)
        self.control.stop()
        self.await_finished()
        self.assertEqual([command for _, command in self.transport.commands], [2])

    def test_replacement_voice_window_is_not_adopted_for_stop(self):
        self.transport.on_send = self.auto_visibility
        self.control.start()
        self.assertTrue(self.ready.wait(.5))
        self.transport.windows = frozenset({102})
        self.control.stop()
        self.await_finished()
        self.assertEqual([command for _, command in self.transport.commands], [2])

    def test_stop_timeout_keeps_debt_and_never_repeats_ambiguous_command(self):
        def send(command):
            if command == 2:
                self.transport.windows = frozenset({101})
            else:
                raise OSError("timeout after dispatch")
        self.transport.on_send = send
        self.control.start()
        self.assertTrue(self.ready.wait(.5))
        self.control.stop()
        self.await_finished()
        self.assertTrue(self.control.cleanup_pending)
        self.assertFalse(self.control.start())
        self.control.stop()
        self.await_finished()
        self.assertEqual([command for _, command in self.transport.commands], [2, 3])
        self.transport.windows = frozenset()
        self.control.stop()
        self.await_finished()
        self.assertFalse(self.control.cleanup_pending)

    def test_start_ack_timeout_observes_and_cleans_without_publishing_ready(self):
        def send(command):
            self.auto_visibility(command)
            if command == 2:
                raise OSError("ack timeout")
        self.transport.on_send = send
        self.assertFalse(self.control.start())
        self.await_finished()
        self.assertFalse(self.ready.is_set())
        self.assertEqual([command for _, command in self.transport.commands], [2, 3])

    def test_observer_start_failure_retains_owned_cleanup_after_dispatch(self):
        self.transport.on_send = self.auto_visibility
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")):
            self.assertFalse(self.control.start())
        self.assertTrue(self.control.cleanup_pending)
        self.assertFalse(self.control.stop())
        self.await_finished()
        self.assertFalse(self.ready.is_set())
        self.assertFalse(self.control.cleanup_pending)
        self.assertEqual([command for _, command in self.transport.commands], [2, 3])


if __name__ == "__main__":
    unittest.main()
