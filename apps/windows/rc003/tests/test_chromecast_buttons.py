"""Synthetic HCI/ATT only. No addresses, capture files or Windows resources."""
import struct
import unittest

from ovb_rc003.chromecast_buttons import (
    AttAssembler,
    ButtonReceiver,
    ReceiveError,
    empty_stop_details,
)
from ovb_rc003.remote_layout import CHROMECAST_REPORT_CODES

ENTITY, GENERATION, RADIO = "a" * 64, "b" * 64, "c" * 64


def acl(att, handle=0x31):
    payload = struct.pack("<HH", len(att), 4) + att
    return struct.pack("<HH", handle | 0x2000, len(payload)) + payload


def notification(code, attribute=0x71):
    return b"\x1b" + attribute.to_bytes(2, "little") + bytes([code]) + bytes(7)


def incomplete_notification(attribute=0x71, handle=0x31):
    payload = struct.pack("<HH", 11, 4) + b"\x1b" + attribute.to_bytes(2, "little")
    return struct.pack("<HH", handle | 0x2000, 15) + payload


CAPTURE_DETAILS = {
    "capture_metadata_available": True,
    "capture_layout": "bthport_402_tdh",
    "capture_event_id": 402,
    "capture_event_version": 0,
    "capture_user_data_length": 19,
    "capture_property_length": 11,
    "capture_extracted_length": 11,
    "capture_length_match": True,
}


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self.receiver = ButtonReceiver(ENTITY, GENERATION, RADIO, 10)
        self.now = 10

    def feed(self, att, kind=3, handle=0x31, radio=RADIO, generation=GENERATION):
        self.now += .01
        return self.receiver.feed(radio, generation, kind, acl(att, handle), self.now)

    def prove(self, api_first=False):
        if api_first:
            self.api()
        request = b"\x06\x01\x00\xff\xff\x00\x28" + self.receiver.marker.bytes[::-1]
        self.feed(request, kind=4)
        self.feed(b"\x01\x06\x01\x00\x0a")
        if not api_first:
            self.api()

    def api(self, **kwargs):
        return self.receiver.api_result(ENTITY, GENERATION, now=self.now,
                                       **({"success": True, "services": 0, "input_attribute": 0x71} | kwargs))

    def test_all_fourteen_codes_and_release(self):
        self.prove()
        for button, code in CHROMECAST_REPORT_CODES.items():
            with self.subTest(button=button):
                down, = self.feed(notification(code))
                self.assertEqual((down.button, down.action, down.entity, down.generation), (button, "down", ENTITY, GENERATION))
                self.assertEqual(self.feed(notification(code)), [])
                up, = self.feed(notification(0))
                self.assertEqual((up.button, up.action), (button, "up"))

    def test_first_stop_cause_and_report_shape_survive_cleanup(self):
        self.prove()
        self.feed(notification(9))
        self.receiver.advance(self.now + 1)
        self.receiver.stop(self.now + 2)
        self.assertEqual(self.receiver.reason, "unknown_key_report")
        self.assertEqual(self.receiver.stop_details, empty_stop_details() | dict(
            event_kind=3, attribute=0x71, length=8, report_code=9, tail_nonzero=False))
        self.assertFalse(self.receiver.ready)

    def test_attributed_invalid_acl_length_retains_bounded_header_evidence(self):
        request = b"\x06\x01\x00\xff\xff\x00\x28" + self.receiver.marker.bytes[::-1]
        self.feed(request, kind=4)
        packet = struct.pack("<HH", 0x2031, 8) + bytes(7)
        capture_details = {
            "capture_metadata_available": True,
            "capture_layout": "bthport_402_tdh",
            "capture_event_id": 402,
            "capture_event_version": 1,
            "capture_user_data_length": 31,
            "capture_property_length": 11,
            "capture_extracted_length": 11,
            "capture_length_match": True,
        }

        self.receiver.feed(
            RADIO, GENERATION, 3, packet, self.now, capture_details=capture_details
        )

        self.assertEqual(self.receiver.reason, "invalid_acl_length")
        self.assertEqual(self.receiver.stop_details["length"], 11)
        self.assertEqual(
            {key: self.receiver.stop_details[key] for key in (
                "acl_header_parsed", "acl_issue", "acl_flags", "acl_handle", "acl_boundary",
                "acl_declared_length", "acl_actual_length", "acl_length_relation",
                "acl_handle_valid", "acl_reserved_flags_clear", "acl_payload_nonempty",
                "acl_length_match",
            )},
            {
                "acl_header_parsed": True,
                "acl_issue": "declared_length_mismatch",
                "acl_flags": 0x2031,
                "acl_handle": 0x31,
                "acl_boundary": 2,
                "acl_declared_length": 8,
                "acl_actual_length": 7,
                "acl_length_relation": "short",
                "acl_handle_valid": True,
                "acl_reserved_flags_clear": True,
                "acl_payload_nonempty": True,
                "acl_length_match": False,
            },
        )
        self.assertEqual(
            {key: self.receiver.stop_details[key] for key in capture_details},
            capture_details,
        )

    def test_post_ready_header_only_input_recovers_after_complete_neutral(self):
        diagnostics = []
        self.receiver = ButtonReceiver(
            ENTITY,
            GENERATION,
            RADIO,
            self.now,
            on_diagnostic=lambda *args: diagnostics.append(args),
        )
        self.prove()
        self.now += .01
        self.assertEqual(self.receiver.feed(
            RADIO, GENERATION, 3, incomplete_notification(), self.now,
            capture_details=CAPTURE_DETAILS,
        ), [])
        self.assertFalse(self.receiver.stopped)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0][0], "input_payload_unavailable")
        self.assertEqual(diagnostics[0][1]["att_attribute"], 0x71)
        self.assertEqual(diagnostics[0][1]["att_missing_length"], 8)
        self.assertEqual(self.feed(notification(7)), [])
        self.assertEqual(self.feed(notification(0)), [])
        down, = self.feed(notification(7))
        self.assertEqual((down.button, down.action), ("ok", "down"))
        self.assertTrue(self.receiver.ready)

    def test_repeated_header_only_input_cancels_once_then_fails_explicitly(self):
        self.prove()
        self.feed(notification(7))
        self.now += .01
        cancelled, = self.receiver.feed(
            RADIO, GENERATION, 3, incomplete_notification(), self.now,
            capture_details=CAPTURE_DETAILS,
        )
        self.assertEqual((cancelled.button, cancelled.action), ("ok", "cancel"))
        self.now += .01
        self.assertEqual(self.receiver.feed(
            RADIO, GENERATION, 3, incomplete_notification(), self.now,
            capture_details=CAPTURE_DETAILS,
        ), [])
        self.assertTrue(self.receiver.stopped)
        self.assertEqual(self.receiver.reason, "input_payload_unavailable")
        self.assertEqual(self.receiver.stop_details["l2cap_cid"], 4)
        self.assertEqual(self.receiver.stop_details["att_opcode"], 0x1B)

    def test_verified_missing_payload_switches_only_ordinary_buttons_to_hid(self):
        self.prove()
        self.feed(notification(7))
        self.now += .01
        cancelled, = self.receiver.feed(
            RADIO, GENERATION, 3, incomplete_notification(), self.now,
            capture_details=CAPTURE_DETAILS,
        )
        self.assertEqual((cancelled.button, cancelled.action), ("ok", "cancel"))
        self.receiver.begin_hid_fallback(self.now)
        self.assertTrue(self.receiver.ready)
        self.assertTrue(self.receiver.using_hid)
        self.assertEqual(self.feed(notification(7)), [])  # No second ETW down.
        self.now += .01
        self.assertEqual(self.receiver.feed(
            RADIO, GENERATION, 3, incomplete_notification(), self.now,
            capture_details=CAPTURE_DETAILS,
        ), [])
        self.assertFalse(self.receiver.stopped)
        down, = self.receiver.feed_hid(bytes([7]) + bytes(7), self.now + .01)
        self.assertEqual((down.button, down.action), ("ok", "down"))
        up, = self.receiver.feed_hid(bytes(8), self.now + .02)
        self.assertEqual((up.button, up.action), ("ok", "up"))

    def test_hid_report_is_rejected_without_proven_etw_source_and_switch(self):
        with self.assertRaises(ReceiveError):
            self.receiver.feed_hid(bytes(8), self.now)
        self.prove()
        with self.assertRaises(ReceiveError):
            self.receiver.begin_hid_fallback(self.now)

    def test_header_only_shape_is_not_relaxed_for_wrong_attribute_or_metadata(self):
        for packet, details in (
            (incomplete_notification(attribute=0x72), CAPTURE_DETAILS),
            (incomplete_notification(), CAPTURE_DETAILS | {"capture_event_version": 1}),
        ):
            with self.subTest(packet=packet, details=details):
                self.setUp()
                self.prove()
                self.now += .01
                self.receiver.feed(
                    RADIO, GENERATION, 3, packet, self.now, capture_details=details
                )
                self.assertTrue(self.receiver.stopped)
                self.assertEqual(self.receiver.reason, "invalid_acl_length")

    def test_header_only_input_recovery_deadline_does_not_slide(self):
        self.prove()
        self.now += .01
        self.receiver.feed(
            RADIO, GENERATION, 3, incomplete_notification(), self.now,
            capture_details=CAPTURE_DETAILS,
        )
        self.receiver.advance(self.now + 5.01)
        self.assertTrue(self.receiver.stopped)
        self.assertEqual(self.receiver.reason, "input_payload_unavailable")
        self.assertEqual(self.receiver.stop_details["att_attribute"], 0x71)

    def test_timeout_does_not_reuse_previous_packet_metadata(self):
        self.feed(b"unrelated")
        if not self.receiver.stopped:
            self.receiver.advance(self.now + 16)
        self.assertEqual(self.receiver.reason, "source_timeout")
        self.assertEqual(self.receiver.stop_details["report_code"], -1)
        self.assertEqual(self.receiver.stop_details["length"], 0)

    def test_all_proof_parts_required_in_either_api_order(self):
        self.assertFalse(self.receiver.ready)
        self.assertEqual(self.feed(notification(7)), [])
        self.api()
        self.assertFalse(self.receiver.ready)
        self.prove(api_first=False)  # duplicate API must fail closed
        self.assertFalse(self.receiver.ready)
        self.setUp()
        self.prove(api_first=True)
        self.assertTrue(self.receiver.ready)

    def test_other_radio_generation_and_connection_do_not_release_current_key(self):
        self.prove()
        self.feed(notification(7))
        for fields in ({"radio": "d" * 64}, {"generation": "d" * 64}, {"handle": 0x32}):
            self.assertEqual(self.feed(notification(0), **fields), [])
        self.assertEqual(self.receiver.button, "ok")
        self.assertEqual(self.feed(notification(0))[0].action, "up")

    def test_voice_control_and_wrong_attribute_never_become_keys(self):
        self.prove()
        self.assertEqual(self.feed(b"\x1b\x82\x00\x08"), [])
        self.assertEqual(self.feed(notification(8, attribute=0x72)), [])
        self.assertEqual(self.feed(notification(8))[0].button, "volume_mute")

    def test_voice_callback_only_receives_proven_selected_connection(self):
        received = []
        self.receiver.on_notification = lambda *args: received.append(args)
        voice = b"\x1b\x3f\x00\x04\x03\x02\x01"
        self.feed(voice)
        self.assertFalse(received)
        self.prove()
        for fields in ({"radio": "d" * 64}, {"generation": "d" * 64}, {"handle": 0x32}):
            self.feed(voice, **fields)
        self.assertFalse(received)
        self.feed(voice)
        self.assertEqual(received[0][:2], (0x3f, b"\x04\x03\x02\x01"))
        self.receiver.stop(self.now)
        self.feed(voice)
        self.assertEqual(len(received), 1)

    def test_missing_release_cancels_old_key_not_click(self):
        self.prove()
        self.feed(notification(7))
        result = self.feed(notification(3))
        self.assertEqual([(edge.button, edge.action) for edge in result], [("ok", "cancel"), ("up", "down")])

    def test_unknown_reports_cancel_and_require_new_proof(self):
        for report in (notification(0xFF), notification(7) + b"\x00", notification(7)[:-1] + b"\x01"):
            self.setUp()
            self.prove()
            self.feed(notification(7))
            self.assertEqual(self.feed(report)[0].action, "cancel")
            self.assertFalse(self.receiver.ready)
            self.assertEqual(self.feed(notification(7)), [])

    def test_stop_and_disconnect_cancel_without_release(self):
        for event in (b"\x05\x04\x00\x31\x00\x13", b"\x3e\x04\x0a\x00\x31\x00"):
            self.setUp()
            self.prove()
            self.feed(notification(7))
            result = self.receiver.feed(RADIO, GENERATION, 2, event, self.now)
            self.assertEqual(result[0].action, "cancel")
            self.assertFalse(self.receiver.ready)
            self.assertEqual(self.receiver.stop(self.now), [])

    def test_query_failure_and_deadline(self):
        self.api(success=False)
        self.assertTrue(self.receiver.stopped)
        self.setUp()
        self.receiver.advance(26)
        self.assertEqual(self.receiver.reason, "source_timeout")

    def test_no_reversed_source_time_or_nonfinite_clock(self):
        self.prove()
        self.feed(notification(7))
        self.assertEqual(self.receiver.advance(9)[0].action, "cancel")
        for value in (float("nan"), float("inf"), True, -1):
            with self.assertRaises(ReceiveError):
                ButtonReceiver(ENTITY, GENERATION, RADIO, value)

    def test_rx_fragment_reassembly_and_timeout(self):
        self.prove()
        packet = acl(notification(7))
        first = struct.pack("<HH", 0x2031, 6) + packet[4:10]
        tail = struct.pack("<HH", 0x1031, len(packet[10:])) + packet[10:]
        self.assertEqual(self.receiver.feed(RADIO, GENERATION, 3, first, self.now), [])
        self.assertEqual(self.receiver.feed(RADIO, GENERATION, 3, tail, self.now)[0].button, "ok")
        self.receiver.feed(RADIO, GENERATION, 3, first, self.now)
        self.assertEqual(self.receiver.advance(self.now + 3)[0].action, "cancel")
        self.assertEqual(self.receiver.reason, "fragment_timeout")

    def test_bad_packet_other_handle_cannot_cancel_selected_key(self):
        self.prove()
        self.feed(notification(7))
        self.assertEqual(self.receiver.feed(RADIO, GENERATION, 3, b"\x32\x20\xff", self.now), [])
        self.assertEqual(self.receiver.button, "ok")

    def test_periodic_tick_does_not_reject_slightly_delayed_source_timestamp(self):
        self.prove()
        self.receiver.advance(self.now + .15)
        result = self.receiver.feed(RADIO, GENERATION, 3, acl(notification(7)),
                                    self.now + .05, received_at=self.now + .2)
        self.assertEqual(result[0].action, "down")
        self.assertEqual(result[0].time, self.now + .05)

    def test_excessive_delivery_delay_cancels_without_replaying(self):
        self.prove()
        self.feed(notification(7))
        result = self.receiver.feed(RADIO, GENERATION, 3, acl(notification(3)),
                                    self.now + .05, received_at=self.now + 2)
        self.assertEqual([edge.action for edge in result], ["cancel"])

    def test_nonce_seen_on_two_connections_is_ambiguous(self):
        request = b"\x06\x01\x00\xff\xff\x00\x28" + self.receiver.marker.bytes[::-1]
        self.feed(request, kind=4)
        self.feed(request, kind=4, handle=0x32)
        self.assertEqual(self.receiver.reason, "ambiguous_probe")


class AssemblerTests(unittest.TestCase):
    def test_invalid_acl_predicates_are_recorded_without_packet_contents(self):
        cases = (
            (struct.pack("<HH", 0x2F00, 1) + b"x", "invalid_handle"),
            (struct.pack("<HH", 0x6031, 1) + b"x", "reserved_flags"),
            (struct.pack("<HH", 0x2031, 0), "empty_payload"),
            (struct.pack("<HH", 0x2031, 1) + b"xx", "declared_length_mismatch"),
        )
        for packet, issue in cases:
            with self.subTest(issue=issue), self.assertRaises(ReceiveError) as raised:
                AttAssembler().feed(3, packet, 1)
            details = raised.exception.details
            self.assertEqual(details["acl_issue"], issue)
            self.assertNotIn("raw", details)
            self.assertNotIn("data", details)

    def test_overlapping_acl_failures_keep_all_predicates(self):
        packet = struct.pack("<HH", 0x6F00, 0) + b"x"

        with self.assertRaises(ReceiveError) as raised:
            AttAssembler().feed(3, packet, 1)

        self.assertEqual(raised.exception.details["acl_issue"], "invalid_handle")
        self.assertFalse(raised.exception.details["acl_handle_valid"])
        self.assertFalse(raised.exception.details["acl_reserved_flags_clear"])
        self.assertFalse(raised.exception.details["acl_payload_nonempty"])
        self.assertFalse(raised.exception.details["acl_length_match"])
        self.assertEqual(raised.exception.details["acl_length_relation"], "surplus")

    def test_missing_acl_header_is_explicitly_unknown(self):
        with self.assertRaises(ReceiveError) as raised:
            AttAssembler().feed(3, b"\x31\x20\x08", 1)
        self.assertEqual(str(raised.exception), "invalid_acl")
        self.assertEqual(raised.exception.details, {"acl_issue": "header_unavailable"})

    def test_non_att_fragment_chain_is_ignored_without_orphan_error(self):
        assembler = AttAssembler()
        first = struct.pack("<HHHH", 0x2031, 4, 5, 6)
        tail = struct.pack("<HH", 0x1031, 5) + bytes(5)
        self.assertIsNone(assembler.feed(3, first, 1))
        self.assertIsNone(assembler.feed(3, tail, 1))
        self.assertFalse(assembler.fragments)

    def test_capacity_bounds(self):
        assembler = AttAssembler()
        for handle in range(32):
            self.assertIsNone(assembler.feed(3, struct.pack("<HHHH", handle | 0x2000, 4, 200, 4), 1))
        with self.assertRaises(ReceiveError):
            assembler.feed(3, struct.pack("<HHHH", 32 | 0x2000, 4, 200, 4), 1)
        self.assertEqual(len(assembler.fragments), 32)
        self.assertEqual(len(assembler.expire(4)), 32)
        self.assertFalse(assembler.fragments)

    def test_rx_and_tx_fragments_do_not_mix(self):
        assembler = AttAssembler()
        assembler.feed(3, struct.pack("<HHHH", 0x2031, 4, 5, 4), 1)
        with self.assertRaises(ReceiveError):
            assembler.feed(4, struct.pack("<HH", 0x1031, 5) + bytes(5), 1)
        self.assertEqual(assembler.feed(3, struct.pack("<HH", 0x1031, 5) + bytes(5), 1), (0x31, bytes(5)))


if __name__ == "__main__":
    unittest.main()
