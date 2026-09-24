import json
import unittest

from ovb_rc003.chromecast_channel import Channel, ChannelError, ParentLease, SessionIdentity
from ovb_rc003.chromecast_buttons import ButtonEdge, empty_stop_details


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.identity = SessionIdentity.create("a" * 64)

    def test_fixed_control_round_trip(self):
        sender, receiver = (Channel(self.identity, commands=True) for _ in range(2))
        for kind, fields in (("start", {"mode": "detect"}), ("heartbeat", {}), ("stop", {})):
            self.assertEqual(receiver.decode(sender.encode(kind, **fields))["type"], kind)
        with self.assertRaises(ChannelError):
            sender.encode("heartbeat")

    def test_event_round_trip_and_identity(self):
        sender, receiver = (Channel(self.identity, commands=False) for _ in range(2))
        receiver.decode(sender.encode("ready"))
        edge = ButtonEdge(self.identity.entity, self.identity.generation, 123, "input_source", "down")
        self.assertEqual(receiver.decode(sender.encode_edge(edge))["button"], "input_source")
        self.assertNotEqual(self.identity.generation, self.identity.token)

    def test_diagnostics_are_bounded_nonterminal_and_never_commands(self):
        fields = empty_stop_details() | dict(
            stage="receive", phase="failed", reason="unknown_key_report",
            event_kind=3, attribute=0x71, length=8, report_code=9, tail_nonzero=False,
        )
        sender, receiver = (Channel(self.identity, commands=False) for _ in range(2))
        receiver.decode(sender.encode("diagnostic", **fields))
        receiver.decode(sender.encode("ready"))
        receiver.decode(sender.encode("diagnostic", **fields))
        self.assertTrue(receiver.ready)
        self.assertFalse(receiver.closed)
        parsed = fields | {
            "reason": "invalid_acl_length",
            "length": 11,
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
            "capture_metadata_available": True,
            "capture_layout": "bthport_402_tdh",
            "capture_event_id": 402,
            "capture_event_version": 1,
            "capture_user_data_length": 31,
            "capture_property_length": 11,
            "capture_extracted_length": 11,
            "capture_length_match": True,
        }
        parsed_sender, parsed_receiver = (
            Channel(self.identity, commands=False) for _ in range(2)
        )
        encoded = parsed_sender.encode("diagnostic", **parsed)
        decoded = parsed_receiver.decode(encoded)
        self.assertEqual(decoded["acl_actual_length"], 7)
        self.assertEqual(decoded["capture_extracted_length"], 11)
        self.assertLess(len(encoded), 2048)
        self.assertNotIn(b"feedface", encoded)
        for change in ({"raw": "secret"}, {"reason": "arbitrary text"}, {"stage": "path"},
                       {"length": 65536}, {"report_code": 256}, {"event_kind": True}, {"tail_nonzero": 1},
                       {"acl_issue": "raw_packet"}, {"acl_flags": 65536}, {"acl_handle": 4096},
                       {"acl_boundary": 4}, {"acl_declared_length": True}, {"acl_actual_length": -2},
                       {"acl_length_relation": "truncated"}, {"acl_header_parsed": 1},
                       {"acl_handle_valid": 1}, {"acl_reserved_flags_clear": 1},
                       {"acl_payload_nonempty": 1}, {"acl_length_match": 1},
                       {"acl_header_parsed": True}, {"acl_issue": "header_unavailable", "acl_flags": 0}):
            with self.subTest(change=change), self.assertRaises(ChannelError):
                Channel(self.identity, commands=False).encode("diagnostic", **(fields | change))
        with self.assertRaises(ChannelError):
            Channel(self.identity, commands=True).encode("diagnostic", **fields)

        for change in (
            {"capture_metadata_available": True},
            {"capture_layout": "raw_offset"},
            {"capture_event_id": 402},
            {"capture_event_version": True},
            {"capture_property_length": 65537},
            {"capture_extracted_length": 65537},
            {"capture_length_match": True},
        ):
            with self.subTest(capture_change=change), self.assertRaises(ChannelError):
                Channel(self.identity, commands=False).encode(
                    "diagnostic", **(fields | change)
                )

    def test_no_arbitrary_command_path_payload_or_voice(self):
        for kind, fields in (("start", {"mode": "run", "path": "anything"}),
                             ("execute", {"command": "anything"}), ("start", {"mode": "voice"}),
                             ("start", {"mode": "run", "address": "anything"})):
            with self.subTest(kind=kind, fields=fields), self.assertRaises(ChannelError):
                Channel(self.identity, commands=True).encode(kind, **fields)
        for button in ("mic", "unknown", "menu"):
            with self.assertRaises(ChannelError):
                Channel(self.identity, commands=False).encode("edge", button=button, action="down", time=1)

    def test_replay_gap_unknown_field_and_old_run_rejected(self):
        sender = Channel(self.identity, commands=False)
        original = sender.encode("ready")
        for change in ({"seq": 2}, {"seq": True}, {"version": True}, {"token": "b" * 64},
                       {"generation": "c" * 64}, {"entity": "d" * 64}, {"raw": "bytes"}):
            receiver = Channel(self.identity, commands=False)
            with self.assertRaises(ChannelError):
                receiver.decode(json.dumps(json.loads(original) | change).encode())
            self.assertTrue(receiver.closed)
        receiver = Channel(self.identity, commands=False)
        receiver.decode(original)
        with self.assertRaises(ChannelError):
            receiver.decode(original)

    def test_duplicate_fields_oversize_and_invalid_time(self):
        for raw in (b'{"version":1,"version":1}', b"x" * 2049, b"\xff", b"[[]]", b"[" * 1500):
            with self.assertRaises(ChannelError):
                Channel(self.identity, commands=False).decode(raw)
        for now in (True, float("nan"), float("inf"), -1):
            with self.assertRaises(ChannelError):
                Channel(self.identity, commands=False).encode("edge", button="ok", action="up", time=now)

    def test_lost_parent_cannot_revive_old_session(self):
        lease = ParentLease(1)
        lease.renew(5)
        self.assertTrue(lease.alive(14))
        self.assertFalse(lease.alive(16))
        with self.assertRaises(ChannelError):
            lease.renew(16)
        self.assertFalse(lease.alive(5))

    def test_edge_requires_ready_and_failure_returns_cancel_once(self):
        with self.assertRaises(ChannelError):
            Channel(self.identity, commands=False).encode("edge", button="ok", action="down", time=1)
        sender, receiver = (Channel(self.identity, commands=False) for _ in range(2))
        receiver.decode(sender.encode("ready"))
        receiver.decode(sender.encode("edge", button="ok", action="down", time=1))
        with self.assertRaises(ChannelError):
            receiver.decode(b"not-json")
        cancelled = receiver.cancel_pending(2)
        self.assertEqual((cancelled.button, cancelled.action), ("ok", "cancel"))
        self.assertIsNone(receiver.cancel_pending(3))

    def test_session_token_not_in_debug_representation(self):
        self.assertNotIn(self.identity.token, repr(self.identity))

    def test_ready_and_edge_order_fail_closed(self):
        for kind, fields in (("ready", {}), ("edge", {"button": "up", "action": "up", "time": 1})):
            channel = Channel(self.identity, commands=False)
            channel.encode("ready")
            with self.assertRaises(ChannelError):
                channel.encode(kind, **fields)


if __name__ == "__main__":
    unittest.main()
