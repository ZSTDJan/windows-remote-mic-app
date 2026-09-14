import json
import tempfile
import time
import unittest
from pathlib import Path

from ovb_rc003.diagnostic_trace import DiagnosticTrace


class DiagnosticTraceTests(unittest.TestCase):
    def test_disabled_trace_has_no_writer_or_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = DiagnosticTrace(Path(tmp), enabled=False)
            self.assertFalse(trace.emit("ignored"))
            trace.close()
            self.assertFalse((Path(tmp) / "logs").exists())

    def test_enabled_trace_correlates_gesture_and_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = DiagnosticTrace(root, enabled=True)
            gesture_id = trace.begin_gesture("menu", "hid_tap", True)
            trace.set_current_gesture(gesture_id)
            attempt_id = trace.begin_attempt(gesture_id, provider="wetype")
            trace.record_send_input(backend="SendInput", requested=2, returned=2)
            trace.end_attempt(attempt_id, "hotkey_sent")
            trace.begin_gesture("menu", "hid_tap", False)
            trace.close()

            path = root / "logs" / "diagnostic-trace.jsonl"
            self.assertTrue(path.is_file())
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(records), 5)
            self.assertEqual({record["session_id"] for record in records}, {trace.session_id})
            self.assertEqual([record["seq"] for record in records], list(range(1, len(records) + 1)))
            attempt = next(record for record in records if record["event"] == "attempt_started")
            finished = next(record for record in records if record["event"] == "attempt_finished")
            self.assertEqual(attempt["attempt_id"], finished["attempt_id"])
            self.assertEqual(attempt["gesture_id"], gesture_id)

    def test_full_queue_does_not_block_and_counts_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = DiagnosticTrace(Path(tmp), enabled=True, queue_size=1)
            for index in range(100):
                trace.emit("burst", index=index)
            self.assertGreater(trace.dropped_count, 0)
            trace.close()

    def test_sensitive_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = DiagnosticTrace(Path(tmp), enabled=True)
            trace.emit("event", text="secret", title="chat", payload="audio", path="device")
            trace.close()
            records = [json.loads(line) for line in (Path(tmp) / "logs" / "diagnostic-trace.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertNotIn("text", records[-1])
            self.assertNotIn("title", records[-1])
            self.assertNotIn("payload", records[-1])
            self.assertNotIn("path", records[-1])

    def test_trace_can_be_enabled_and_disabled_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = DiagnosticTrace(Path(tmp), enabled=False)
            self.assertTrue(trace.set_enabled(True))
            first_session = trace.session_id
            trace.emit("live")
            self.assertTrue(trace.set_enabled(False))
            self.assertFalse(trace.emit("after_stop"))
            self.assertTrue(trace.set_enabled(True))
            self.assertNotEqual(trace.session_id, first_session)
            trace.close()

            records = [
                json.loads(line)
                for line in (Path(tmp) / "logs" / "diagnostic-trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            sessions = {record["session_id"] for record in records}
            self.assertEqual(len(sessions), 2)
            for session_id in sessions:
                sequence = [
                    record["seq"]
                    for record in records
                    if record["session_id"] == session_id
                ]
                self.assertEqual(sequence, list(range(1, len(sequence) + 1)))

    def test_repeated_toggle_keeps_writer_sessions_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = DiagnosticTrace(Path(tmp), enabled=False)
            session_ids = []
            for index in range(5):
                self.assertTrue(trace.set_enabled(True))
                session_ids.append(trace.session_id)
                trace.emit("toggle_probe", index=index)
                self.assertTrue(trace.set_enabled(False))
                self.assertIsNone(trace._thread)

            records = [
                json.loads(line)
                for line in (Path(tmp) / "logs" / "diagnostic-trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(set(session_ids)), 5)
            for session_id in session_ids:
                session_records = [
                    record
                    for record in records
                    if record["session_id"] == session_id
                ]
                self.assertEqual(
                    [record["seq"] for record in session_records],
                    list(range(1, len(session_records) + 1)),
                )
                self.assertEqual(
                    [record["event"] for record in session_records],
                    ["session_started", "toggle_probe", "session_finished"],
                )

    def test_gesture_summary_counts_late_sources_and_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = DiagnosticTrace(Path(tmp), enabled=True)
            gesture_id = trace.begin_gesture("menu", "hid_tap", True)
            trace.emit(
                "mapping_trigger",
                gesture_id=gesture_id,
                button_id="menu",
            )
            trace.begin_gesture("menu", "hid_tap", False)
            trace.begin_gesture("menu", "raw_keyboard", True)
            trace.emit(
                "raw_input_event",
                gesture_id=gesture_id,
                edge="down",
                late=True,
            )
            trace.begin_gesture("menu", "raw_keyboard", False)
            time.sleep(0.35)
            trace.close()

            records = [
                json.loads(line)
                for line in (Path(tmp) / "logs" / "diagnostic-trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            summary = next(
                record for record in records if record["event"] == "gesture_summary"
            )
            self.assertEqual(summary["gesture_id"], gesture_id)
            self.assertEqual(summary["mapping_triggers"], 1)
            self.assertEqual(summary["raw_down"], 1)
            self.assertEqual(summary["late_events"], 1)


if __name__ == "__main__":
    unittest.main()
