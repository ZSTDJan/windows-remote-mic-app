"""Metadata diagnosis must leave input filtering and press/release delivery intact."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock
from types import SimpleNamespace

from ovb_rc003.chromecast_hid_tap_windows import HidTap, _SCRIPT
from ovb_rc003.chromecast_observation import HID_COUNTS, Observation, valid_record
from ovb_rc003.chromecast_channel import Channel, ChannelError, SessionIdentity
from ovb_rc003.chromecast_client import Client
from ovb_rc003 import remote_layout


def flow():
    return dict(kind="hid_flow", elapsed_ms=0, sample_ms=0,
                counts=dict.fromkeys(HID_COUNTS, 0), lengths=[], copy_buffer_lengths=[],
                copy_hook_state="ready", copy_scope="selected_host_unattributed",
                last_error_step="none")


class HidDiagnosticsTests(unittest.TestCase):
    def test_client_counts_delivery_suppression_and_callback_failure(self):
        for mode in ("run", "detect"):
            with self.subTest(mode=mode):
                client = Client("a" * 64, mode=mode, on_edge=mock.Mock())
                down = SimpleNamespace(action="down")
                client._deliver(down)
                client.cancel.set()
                client._deliver(down)
                client._deliver(SimpleNamespace(action="cancel"))
                self.assertEqual(client._input_counts["delivered"], 2)
                self.assertEqual(client._input_counts["down_delivered"], 1)
                self.assertEqual(client._input_counts["suppressed"], 1)
                client.cancel.clear()
                client.on_edge.side_effect = ValueError("callback failed")
                with self.assertRaises(ValueError):
                    client._deliver(down)
                self.assertEqual(client._input_counts["callback_failed"], 1)
                with mock.patch("ovb_rc003.chromecast_client.logging.getLogger") as logger:
                    client._log_input_flow()
                    client._log_input_flow()
                    self.assertEqual(logger.return_value.info.call_count, 1)
                    client._log_input_flow(final=True)
                    self.assertEqual(logger.return_value.info.call_count, 2)

    def test_diagnostic_flood_cannot_overflow_or_reorder_input_queue(self):
        tap = HidTap("a" * 64)
        tap._put(("report", "0300000000000000", 1.0))
        for n in range(1000):
            tap._store_diagnostic(dict(flow(), sample_ms=n))
        tap._put(("report", "0000000000000000", 2.0))
        rows = tap.poll()
        self.assertFalse(tap.overflow.is_set())
        self.assertEqual([row[2] for row in rows[1:]], [1.0, 2.0])
        self.assertEqual(rows[0][1]["sample_ms"], 999)
        self.assertEqual(tap.poll(), [])

    def test_failure_snapshot_precedes_terminal_error(self):
        tap = HidTap("a" * 64)
        tap._store_diagnostic(dict(flow(), last_error_step="data"))
        tap._put(("error", "hid_callback_failed", 0.0))
        self.assertEqual([row[0] for row in tap.poll()], ["hid_flow", "error"])

    def test_metadata_survives_real_channel_and_failed_sink_is_nonfatal(self):
        rows = []
        obs = Observation(rows.append, clock=lambda: 10)
        obs.hid_flow(flow())
        identity = SessionIdentity.create("a" * 64)
        sender, reader = Channel(identity, commands=False), Channel(identity, commands=False)
        self.assertEqual(reader.decode(sender.encode("evidence", record=rows[0]))["record"], rows[0])
        obs.send = lambda row: (_ for _ in ()).throw(OSError())
        obs.hid_flow(flow())
        self.assertEqual(obs.counts["send_failed"], 1)

    def test_unknown_data_invalid_counts_and_unbounded_histograms_are_rejected(self):
        cases = [dict(flow(), raw="PRIVATE"), dict(flow(), address="112233445566"),
                 dict(flow(), copy_raw="02 42 00"), dict(flow(), copy_hook_state=[]),
                 dict(flow(), copy_buffer_lengths=[[3, 1], [3, 2]]),
                 dict(flow(), copy_buffer_lengths=[[n, 1] for n in range(17)]),
                 dict(flow(), copy_lengths=[]),
                 dict(flow(), last_error_step="arbitrary"), dict(flow(), last_error_step=[]),
                 dict(flow(), lengths=[[n, 1] for n in range(17)]),
                 dict(flow(), lengths=[[8, 1], [8, 2]]), dict(flow(), lengths=[[8, -1]]),
                 dict(flow(), lengths=[[True, 1]]), dict(flow(), lengths=[[2**32, 1]])]
        for value in (-1, True, 2**31, "1"):
            row = copy.deepcopy(flow())
            row["counts"]["callbacks"] = value
            cases.append(row)
        sender = Channel(SessionIdentity.create("a" * 64), commands=False)
        tap = HidTap("a" * 64)
        for row in cases:
            with self.subTest(row=row):
                self.assertFalse(valid_record(row))
                tap._store_diagnostic(row)
                with self.assertRaises(ChannelError):
                    sender.encode("evidence", record=row)
        self.assertEqual(tap.poll(), [])


@unittest.skipUnless(shutil.which("node"), "Node is needed for synthetic injected-JavaScript checks")
class InjectedScriptTests(unittest.TestCase):
    def test_runtime_module_and_hook_failures_are_identified_before_ready(self):
        for options, step, reason in ((dict(missingModule=True), "runtime_module", "module_missing"),
                                     (dict(modulePath="private/path"), "runtime_module", "module_path_mismatch"),
                                     (dict(failMainHook=True), "runtime_hook", "hook_exception")):
            with self.subTest(reason=reason):
                result = self.run_script([], expectStartupFailure=True, **options)
                self.assertTrue(result["startupFailed"])
                rows = result["messages"]
                self.assertTrue(any(row.get("step") == step and row.get("reason") == reason
                                    and row.get("state") == "failed" for row in rows))
                self.assertFalse(any(row["kind"] == "hook_ready" for row in rows))
                self.assertNotIn("private/path", json.dumps(rows))

    def run_script(self, cases, **options):
        address_offset = options.pop("address_offset", 24)
        script = (_SCRIPT.replace("__PATH__", '"test.dll"').replace("__ADDRESS__", "[1,2,3,4,5,6]")
                  .replace("__ADDRESS_OFFSET__", str(address_offset))
                  .replace("__CONSUMER_CODES__", json.dumps({usage: remote_layout.CHROMECAST_REPORT_CODES[key]
                      for key, usage in remote_layout.CHROMECAST_CONSUMER_USAGES.items()}))
                  .replace("__RVA__", "1").replace("__COUNTS__", json.dumps(sorted(HID_COUNTS))))
        result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("chromecast_hid_tap_harness.js"))],
                                input=json.dumps(dict(script=script, cases=cases, **options)),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_new_and_old_layouts_deliver_only_selected_remote_press_and_release(self):
        for offset in (8, 24):
            with self.subTest(offset=offset):
                result = self.run_script([
                    {"addressOffset": offset}, {"addressOffset": offset, "bytes": [0] * 8},
                    {"addressOffset": offset, "otherAddress": True, "throwData": True},
                ], address_offset=offset)
                self.assertEqual([row["value"] for row in result["messages"] if row["kind"] == "report"],
                                 ["0300000000000000", "0000000000000000"])
                self.assertEqual(result["reads"], 2)
                self.assertEqual(result["messages"][-1]["counts"]["address_mismatch"], 1)

    def test_old_fixed_offset_reproduces_new_driver_failure_without_reading_payload(self):
        result = self.run_script([{"addressOffset": 8}], address_offset=24)
        self.assertEqual(result["reads"], 0)
        self.assertEqual(result["messages"][-1]["counts"]["address_mismatch"], 1)
        self.assertEqual(result["messages"][-1]["counts"]["accepted"], 0)

    def test_consumer_reports_deliver_all_known_buttons_and_release_through_receiver(self):
        from ovb_rc003.chromecast_buttons import HidButtonReceiver
        # Independent protocol examples; do not derive expected usages from production.
        buttons = ((0x19e, "power"), (0x189, "input_source"), (0x42, "up"), (0x43, "down"),
                   (0x44, "left"), (0x45, "right"), (0x41, "ok"), (0x224, "back"),
                   (0x223, "home"), (0xe9, "volume_up"), (0xea, "volume_down"),
                   (0xe2, "volume_mute"), (0x77, "youtube"), (0x78, "netflix"))
        for offset in (8, 24):
            cases, expected = [], []
            for usage, key in buttons:
                cases.extend([dict(length=2, bytes=[usage & 255, usage >> 8], addressOffset=offset),
                              dict(length=2, bytes=[0, 0], addressOffset=offset)])
                expected.extend([(key, "down"), (key, "up")])
            result = self.run_script(cases, address_offset=offset)
            receiver = HidButtonReceiver("a" * 64, "b" * 64)
            receiver.confirm_source()
            edges = []
            for stamp, row in enumerate(r for r in result["messages"] if r["kind"] == "report"):
                edges.extend(receiver.feed(bytes.fromhex(row["value"]), stamp))
            self.assertEqual([(edge.button, edge.action) for edge in edges], expected)
            self.assertEqual(result["messages"][-1]["counts"]["length_rejected"], 0)

    def test_consumer_unknown_and_other_device_never_become_actions(self):
        result = self.run_script([
            dict(length=2, bytes=[0x45, 0], repeat=2),
            dict(length=2, bytes=[0x45, 1]),  # Do not discard the high byte.
            dict(length=2, bytes=[0x45, 0], otherAddress=True, throwData=True),
            dict(length=2, bytes=[0, 0]),
        ])
        counts = result["messages"][-1]["counts"]
        self.assertEqual(counts["code_rejected"], 1)
        self.assertEqual(counts["address_mismatch"], 1)
        self.assertEqual(result["reads"], 4)
        self.assertEqual([row["value"] for row in result["messages"] if row["kind"] == "report"],
                         ["0600000000000000", "0600000000000000", "0000000000000000"])

    def test_zero_and_each_filter_decision_are_distinguishable_without_payloads(self):
        result = self.run_script([
            {"otherAddress": True}, {"nullBuffer": True}, {"length": 3},
            {"lengthError": True}, {"accessError": True}, {"dataError": True},
            {"bytes": [3, 1, 0, 0, 0, 0, 0, 0]}, {"bytes": [255, 0, 0, 0, 0, 0, 0, 0]},
            {}, {"bytes": [0] * 8},
        ])
        rows = result["messages"]
        self.assertEqual(sum(next(row for row in rows if row["kind"] == "hid_flow")["counts"].values()), 0)
        final = rows[-1]
        self.assertTrue(valid_record(final))
        self.assertEqual(final["counts"]["callbacks"], 10)
        self.assertEqual(final["counts"]["address_match"], 9)
        self.assertEqual(final["counts"]["accepted"], 2)
        for name in ("address_mismatch", "buffer_null", "length_rejected", "length_error",
                     "access_error", "data_error", "tail_rejected", "code_rejected"):
            self.assertEqual(final["counts"][name], 1, name)
        self.assertEqual(final["lengths"], [[3, 1], [8, 6]])
        self.assertEqual([row["value"] for row in rows if row["kind"] == "report"],
                         ["0300000000000000", "0000000000000000"])
        self.assertEqual(result["reads"], 4)
        self.assertEqual(result["releases"], 5)
        self.assertEqual(result["beforeTimer"], 7)  # Three startup messages; no per-callback diagnostics.
        self.assertNotIn("value", final)

    def test_mismatched_sources_never_read_report_content_and_histogram_is_bounded(self):
        other = self.run_script([{"otherAddress": True, "throwData": True, "repeat": 10000}])
        self.assertEqual(other["reads"], 0)
        self.assertEqual(other["messages"][-1]["lengths"], [])
        result = self.run_script([{"length": n} for n in range(40)])
        final = result["messages"][-1]
        self.assertEqual(len(final["lengths"]), 16)
        self.assertEqual(final["counts"]["lengths_overflow"], 24)
        self.assertTrue(valid_record(final))

    def test_exceptions_are_located_and_existing_error_delivery_is_preserved(self):
        for case, step in (({"throwAddress": True}, "address"), ({"throwData": True}, "data"),
                           ({"throwRelease": True}, "release")):
            with self.subTest(step=step):
                result = self.run_script([case])
                final = result["messages"][-1]
                self.assertEqual(final["last_error_step"], step)
                self.assertEqual(final["counts"]["callback_error"], 1)
                self.assertEqual(any(row["kind"] == "error" for row in result["messages"]), step != "address")
                immediate = [row for row in result["messages"] if row["kind"] == "hid_flow"][1]
                self.assertEqual(immediate["kind"], "hid_flow")
                self.assertEqual(immediate["last_error_step"], step)

    def test_diagnostic_send_failure_cannot_block_reports(self):
        result = self.run_script([{}, {"bytes": [0] * 8}], failDiagnostics=True)
        self.assertEqual([row["value"] for row in result["messages"] if row["kind"] == "report"],
                         ["0300000000000000", "0000000000000000"])

    def test_copy_probe_reads_three_byte_buffers_when_information_is_zero(self):
        result = self.run_script([{}], copyCases=[
            {"bytes": [2, 0x42, 0]}, {"bytes": [2, 0x45, 0]},
            {"bytes": [2, 0x41, 0]}, {"bytes": [2, 0, 0]},
            {"bytes": [2, 0x43, 0]}, {"bytes": [2, 0x44, 0]},
            {"bytes": [3, 0x42, 0]},
            {"capacity": 9, "information": 3, "throwRead": True},
        ])
        final = result["messages"][-1]
        self.assertTrue(valid_record(final))
        self.assertEqual(final["copy_hook_state"], "ready")
        self.assertEqual(final["copy_scope"], "selected_host_unattributed")
        self.assertEqual(dict(final["copy_buffer_lengths"]), {3: 7, 9: 1})
        self.assertEqual({name: final["counts"][name] for name in (
            "copy_calls", "copy_success", "copy_capacity_three", "copy_read_three",
            "copy_report_two", "copy_up", "copy_down", "copy_left", "copy_right", "copy_ok", "copy_release",
            "copy_read_error")}, {
            "copy_calls": 8, "copy_success": 8, "copy_capacity_three": 7,
            "copy_read_three": 7, "copy_report_two": 6, "copy_up": 1,
            "copy_down": 1, "copy_left": 1, "copy_right": 1, "copy_ok": 1, "copy_release": 1,
            "copy_read_error": 0,
        })
        self.assertEqual(result["copyReads"], 7)
        self.assertEqual(result["copyStatusReads"], 0)
        self.assertEqual([row["value"] for row in result["messages"] if row["kind"] == "report"],
                         ["0300000000000000"])
        self.assertNotIn("raw", final)

    def test_copy_probe_rejects_pending_failed_and_unreadable_buffers(self):
        result = self.run_script([], copyCases=[
            {"result": 0x103}, {"result": 0xC0000001},
            {"nullOutput": True}, {"throwRead": True}, {"bytes": [2, 0x42]},
            {"capacity": 0, "nullOutput": True},
            {"ioctl": 0x1234},
        ])
        counts = result["messages"][-1]["counts"]
        self.assertEqual(counts["copy_calls"], 6)
        self.assertEqual(counts["copy_pending"], 1)
        self.assertEqual(counts["copy_failed"], 1)
        self.assertEqual(counts["copy_read_error"], 3)
        self.assertEqual(counts["copy_read_three"], 0)
        self.assertEqual(counts["copy_report_two"], 0)
        self.assertEqual(result["copyReads"], 2)

    def test_copy_probe_sync_success_uses_call_status_and_buffer_size(self):
        result = self.run_script([], copyCases=[
            {"status": 1}, {"nullStatus": True}, {"information": 4},
            {"informationHigh": 1},
        ])
        counts = result["messages"][-1]["counts"]
        self.assertEqual(counts["copy_up"], 4)
        self.assertEqual(counts["copy_read_error"], 0)
        self.assertEqual(result["copyStatusReads"], 0)

    def test_copy_probe_buffer_histogram_is_bounded_and_never_emits_input(self):
        result = self.run_script([], copyCases=[{"capacity": n} for n in range(40)])
        final = result["messages"][-1]
        self.assertTrue(valid_record(final))
        self.assertEqual(len(final["copy_buffer_lengths"]), 16)
        self.assertEqual(final["counts"]["copy_lengths_overflow"], 24)
        self.assertEqual(result["copyReads"], 1)
        self.assertFalse(any(row["kind"] == "report" for row in result["messages"]))

    def test_copy_probe_install_failure_preserves_primary_capture(self):
        result = self.run_script([{}], failCopyHook=True)
        final = result["messages"][-1]
        self.assertEqual(final["copy_hook_state"], "unavailable")
        self.assertEqual(final["counts"]["copy_hook_error"], 1)
        self.assertEqual(final["counts"]["accepted"], 1)
        self.assertEqual([row["value"] for row in result["messages"] if row["kind"] == "report"],
                         ["0300000000000000"])
