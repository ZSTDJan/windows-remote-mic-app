"""Fixed control/event contract for the optional Chromecast receiver.

This is validation, NOT an authenticated IPC implementation. The Windows
pipe adapter must compare OS-reported peer PID, creation time, SID and logon
session before calling these functions. A matching token alone proves nothing
about process identity. Requests carry host state, not arbitrary device commands,
paths or addresses. Optional bounded voice events never enter persistent logs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import json
import math
import re
import secrets

from . import remote_selection, remote_layout
from .chromecast_buttons import ButtonEdge, STOP_REASONS
from .chromecast_voice import parse_lifecycle
from .chromecast_observation import valid_record

VERSION = 15
MAX_MESSAGE_BYTES = 2048
HEARTBEAT_TIMEOUT = 10.0
_BUTTONS = frozenset(remote_layout.CHROMECAST_REPORT_CODES)
_BASE = frozenset({"version", "entity", "generation", "token", "seq", "type"})
_REASONS = frozenset({"source_unconfirmed", "unsupported_layout", "radio_ambiguous",
    "input_interface_unavailable", "input_identity_unconfirmed",
    "permission_required", "sensitive_logging_disabled", "sensitive_logging_enable_failed", "configured", "capture_lost", "capture_failed",
    "peer_lost", "stopped", "connection_changed", "input_payload_unavailable",
    "hid_source_unconfirmed", "hid_symbol_unavailable", "hid_capture_failed", "invalid_message"})
DIAGNOSTIC_REASONS = _REASONS | STOP_REASONS | frozenset({"cleanup_unconfirmed", "cleanup_failed"})
DIAGNOSTIC_STAGES = frozenset({"start", "selection", "logging", "device_open", "source_probe",
    "capture", "receive", "voice_open", "voice_tick", "voice_stop", "voice_close", "capture_stop", "device_close"})
ACL_ISSUES = frozenset({"none", "header_unavailable", "invalid_handle", "reserved_flags",
    "empty_payload", "declared_length_mismatch"})
ACL_LENGTH_RELATIONS = frozenset({"unknown", "short", "exact", "surplus"})
CAPTURE_LAYOUTS = frozenset({"unknown", "bthport_402_tdh"})


class ChannelError(ValueError):
    pass


def _finite_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ChannelError("duplicate_field")
        result[key] = value
    return result


def _valid_acl_diagnostic(message):
    if not message["acl_header_parsed"]:
        return (
            message["acl_issue"] in ("none", "header_unavailable")
            and all(message[key] == -1 for key in (
                "acl_flags", "acl_handle", "acl_boundary", "acl_declared_length", "acl_actual_length"
            ))
            and message["acl_length_relation"] == "unknown"
            and not any(message[key] for key in (
                "acl_handle_valid", "acl_reserved_flags_clear", "acl_payload_nonempty", "acl_length_match"
            ))
        )
    flags = message["acl_flags"]
    declared, actual = message["acl_declared_length"], message["acl_actual_length"]
    if min(flags, message["acl_handle"], message["acl_boundary"], declared, actual) < 0:
        return False
    relation = "short" if actual < declared else "surplus" if actual > declared else "exact"
    predicates = (
        message["acl_handle"] <= 0xEFF,
        not bool(flags & 0xC000),
        declared > 0,
        actual == declared,
    )
    issue = next((name for name, valid in zip(
        ("invalid_handle", "reserved_flags", "empty_payload", "declared_length_mismatch"),
        predicates,
    ) if not valid), "none")
    return (
        message["acl_handle"] == (flags & 0xFFF)
        and message["acl_boundary"] == ((flags >> 12) & 3)
        and message["acl_length_relation"] == relation
        and tuple(message[key] for key in (
            "acl_handle_valid", "acl_reserved_flags_clear", "acl_payload_nonempty", "acl_length_match"
        )) == predicates
        and message["acl_issue"] == issue
    )


def _valid_capture_diagnostic(message):
    if not message["capture_metadata_available"]:
        return (
            message["capture_layout"] == "unknown"
            and all(message[key] == -1 for key in (
                "capture_event_id", "capture_event_version", "capture_user_data_length",
                "capture_property_length", "capture_extracted_length",
            ))
            and not message["capture_length_match"]
        )
    return (
        message["capture_layout"] == "bthport_402_tdh"
        and message["capture_event_id"] == 402
        and 0 <= message["capture_event_version"] <= 255
        and 0 <= message["capture_user_data_length"] <= 65535
        and 0 <= message["capture_property_length"] <= 65536
        and 0 <= message["capture_extracted_length"] <= 65536
        and message["capture_length_match"]
        == (message["capture_property_length"] == message["capture_extracted_length"])
    )


def _valid_payload_diagnostic(message):
    if not message["l2cap_header_parsed"]:
        return (
            message["l2cap_declared_length"] == -1
            and message["l2cap_cid"] == -1
            and not message["att_header_parsed"]
            and message["att_opcode"] == -1
            and message["att_attribute"] == -1
            and message["att_missing_length"] == -1
        )
    if not (
        0 <= message["l2cap_declared_length"] <= 4096
        and 0 <= message["l2cap_cid"] <= 65535
    ):
        return False
    if not message["att_header_parsed"]:
        return (
            message["att_opcode"] == -1
            and message["att_attribute"] == -1
            and message["att_missing_length"] == -1
        )
    return (
        0 <= message["att_opcode"] <= 255
        and 0 <= message["att_attribute"] <= 65535
        and 0 <= message["att_missing_length"] <= 4096
    )
@dataclass(frozen=True)
class SessionIdentity:
    entity: str
    generation: str
    token: str = field(repr=False)

    def __post_init__(self):
        if not all(remote_selection.valid_key(value) for value in (self.entity, self.generation, self.token)):
            raise ChannelError("invalid_session")

    @classmethod
    def create(cls, entity):
        return cls(entity, secrets.token_hex(32), secrets.token_hex(32))


class Channel:
    """One ordered direction; reject gaps/replay/old run and unknown fields.

    Each pipe endpoint keeps separate incoming/outgoing instances. On any error
    close the run and cancel held input; never drop a malformed release and keep
    executing. This contract intentionally has no 'run arbitrary payload' path.
    """
    def __init__(self, identity: SessionIdentity, *, commands: bool):
        self.identity = identity
        self.commands = commands
        self.sequence = 0
        self.closed = False
        self.ready = False
        self.held_button = None
        self.last_edge_time = 0.0
        self.mode = ""
        self.voice_enabled = False

    def _validate(self, message):
        if self.closed or not isinstance(message, dict):
            raise ChannelError("channel_closed")
        if not _BASE <= set(message) or type(message["version"]) is not int or message["version"] != VERSION:
            raise ChannelError("unsupported_message")
        identity = self.identity
        if (message["entity"] != identity.entity or message["generation"] != identity.generation
                or not isinstance(message["token"], str)
                or not hmac.compare_digest(message["token"], identity.token)):
            raise ChannelError("session_mismatch")
        if type(message["seq"]) is not int or message["seq"] != self.sequence + 1 or message["seq"] > 2**53 - 1:
            raise ChannelError("sequence_mismatch")
        kind = message["type"]
        expected = set(_BASE)
        if self.commands:
            if kind == "start":
                expected.add("mode")
                if self.sequence != 0 or message.get("mode") not in ("detect", "run", "setup"):
                    raise ChannelError("invalid_start")
                if "voice" in message:
                    expected.add("voice")
                    if type(message["voice"]) is not bool or message.get("mode") == "setup":
                        raise ChannelError("invalid_voice_start")
            elif kind == "voice_host":
                expected.update(("attempt", "result"))
                if (self.mode != "run" or not self.voice_enabled or type(message.get("attempt")) is not int
                        or not 1 <= message["attempt"] <= 2**31
                        or message.get("result") not in ("ready", "failed", "released", "release_failed", "stop")):
                    raise ChannelError("invalid_voice_host")
            elif kind not in ("stop", "heartbeat") or self.sequence == 0:
                raise ChannelError("invalid_command")
        elif kind == "evidence":
            expected.add("record")
            if not valid_record(message.get("record")):
                raise ChannelError("invalid_evidence")
        elif kind == "diagnostic":
            expected.update(("stage", "phase", "reason", "event_kind", "attribute", "length", "report_code",
                             "tail_nonzero", "acl_header_parsed", "acl_issue", "acl_flags", "acl_handle",
                             "acl_boundary", "acl_declared_length", "acl_actual_length", "acl_length_relation",
                             "acl_handle_valid", "acl_reserved_flags_clear", "acl_payload_nonempty", "acl_length_match",
                             "l2cap_header_parsed", "l2cap_declared_length", "l2cap_cid", "att_header_parsed",
                             "att_opcode", "att_attribute", "att_missing_length",
                             "capture_metadata_available", "capture_layout", "capture_event_id", "capture_event_version",
                             "capture_user_data_length", "capture_property_length", "capture_extracted_length",
                             "capture_length_match"))
            if (message.get("stage") not in DIAGNOSTIC_STAGES
                    or message.get("phase") not in ("begin", "done", "failed")
                    or message.get("reason") not in DIAGNOSTIC_REASONS
                    or type(message.get("event_kind")) is not int or message["event_kind"] not in (0, 2, 3, 4)
                    or type(message.get("attribute")) is not int or not 0 <= message["attribute"] <= 65535
                    or type(message.get("length")) is not int or not 0 <= message["length"] <= 65535
                    or type(message.get("report_code")) is not int or not -1 <= message["report_code"] <= 255
                    or type(message.get("tail_nonzero")) is not bool
                    or type(message.get("acl_header_parsed")) is not bool
                    or message.get("acl_issue") not in ACL_ISSUES
                    or type(message.get("acl_flags")) is not int or not -1 <= message["acl_flags"] <= 65535
                    or type(message.get("acl_handle")) is not int or not -1 <= message["acl_handle"] <= 4095
                    or type(message.get("acl_boundary")) is not int or not -1 <= message["acl_boundary"] <= 3
                    or type(message.get("acl_declared_length")) is not int or not -1 <= message["acl_declared_length"] <= 65535
                    or type(message.get("acl_actual_length")) is not int or not -1 <= message["acl_actual_length"] <= 65535
                    or message.get("acl_length_relation") not in ACL_LENGTH_RELATIONS
                    or type(message.get("acl_handle_valid")) is not bool
                    or type(message.get("acl_reserved_flags_clear")) is not bool
                    or type(message.get("acl_payload_nonempty")) is not bool
                    or type(message.get("acl_length_match")) is not bool
                    or type(message.get("l2cap_header_parsed")) is not bool
                    or type(message.get("l2cap_declared_length")) is not int or not -1 <= message["l2cap_declared_length"] <= 4096
                    or type(message.get("l2cap_cid")) is not int or not -1 <= message["l2cap_cid"] <= 65535
                    or type(message.get("att_header_parsed")) is not bool
                    or type(message.get("att_opcode")) is not int or not -1 <= message["att_opcode"] <= 255
                    or type(message.get("att_attribute")) is not int or not -1 <= message["att_attribute"] <= 65535
                    or type(message.get("att_missing_length")) is not int or not -1 <= message["att_missing_length"] <= 4096
                    or type(message.get("capture_metadata_available")) is not bool
                    or message.get("capture_layout") not in CAPTURE_LAYOUTS
                    or type(message.get("capture_event_id")) is not int or not -1 <= message["capture_event_id"] <= 65535
                    or type(message.get("capture_event_version")) is not int or not -1 <= message["capture_event_version"] <= 255
                    or type(message.get("capture_user_data_length")) is not int or not -1 <= message["capture_user_data_length"] <= 65535
                    or type(message.get("capture_property_length")) is not int or not -1 <= message["capture_property_length"] <= 65536
                    or type(message.get("capture_extracted_length")) is not int or not -1 <= message["capture_extracted_length"] <= 65536
                    or type(message.get("capture_length_match")) is not bool
                    or not _valid_acl_diagnostic(message)
                    or not _valid_payload_diagnostic(message)
                    or not _valid_capture_diagnostic(message)):
                raise ChannelError("invalid_diagnostic")
        elif kind == "voice":
            expected.update(("attempt", "event", "data", "time"))
            if (not self.ready or type(message.get("attempt")) is not int
                    or not 0 <= message["attempt"] <= 2**31
                    or not _finite_time(message.get("time"))
                    or message.get("event") not in ("host_start", "host_stop", "state", "control", "audio", "available", "mic", "probe", "lifecycle")
                    or not isinstance(message.get("data"), str) or len(message["data"]) > 1024):
                raise ChannelError("invalid_voice_event")
            if message["event"] in ("control", "audio"):
                value = message["data"]
                if not value or len(value) % 2 or any(ch not in "0123456789abcdef" for ch in value):
                    raise ChannelError("invalid_voice_payload")
            elif (message["event"] == "host_start" and message["data"] or
                  message["event"] == "host_stop" and message["data"] not in ("", "second_press", "time_limit", "device_ended")):
                raise ChannelError("invalid_voice_host_payload")
            elif message["event"] == "state" and message["data"] not in ("idle", "starting", "recording", "stopping", "blocked"):
                raise ChannelError("invalid_voice_state")
            elif message["event"] == "available" and message["data"] not in ("ready", "unavailable"):
                raise ChannelError("invalid_voice_availability")
            elif message["event"] == "lifecycle":
                if message["attempt"] == 0:
                    raise ChannelError("invalid_voice_lifecycle")
                try:
                    parse_lifecycle(message["data"])
                except ValueError as exc:
                    raise ChannelError("invalid_voice_lifecycle") from exc
            elif message["event"] == "mic" and (message["attempt"] != 0 or message["data"] != "pressed"):
                raise ChannelError("invalid_mic_detection")
            elif message["event"] == "probe" and (message["attempt"] != 0 or not re.fullmatch(
                    r"layout/[0-9a-f]{4}/[0-9a-f]{4}|notify/[0-9a-f]{4}/[0-9]{1,4}"
                    r"|start_fields/[0-9]{1,3}/[0-9]{1,3}/[0-9]{1,3}"
                    r"|control/[0-9]{1,3}/[0-9]{1,5}/(?:idle|starting|recording|stopping|blocked)/"
                    r"(?:unknown_control|invalid_control|unsupported_voice_stream|accepted_start|duplicate_start|"
                    r"awaiting_start|ignored_mic|second_press|invalid_stop|accepted_stop|device_open_failed|"
                    r"accepted_sync|detect_ignored|detect_pressed|detect_release)"
                    r"|audio/[0-9]{1,5}/(?:idle|starting|recording|stopping|blocked)/(?:stream|no_stream)/"
                    r"(?:accepted|ignored|detect)", message["data"])):
                raise ChannelError("invalid_voice_probe")
        elif kind == "edge":
            expected.update(("button", "action", "time"))
            if (message.get("button") not in _BUTTONS or message.get("action") not in ("down", "up", "cancel")
                    or not _finite_time(message.get("time"))):
                raise ChannelError("invalid_edge")
            if not self.ready or message["time"] < self.last_edge_time:
                raise ChannelError("edge_before_ready_or_out_of_order")
            if ((message["action"] == "down" and self.held_button is not None)
                    or (message["action"] != "down" and self.held_button != message["button"])):
                raise ChannelError("invalid_edge_order")
        elif kind in ("error", "stopped"):
            expected.add("reason")
            if message.get("reason") not in _REASONS:
                raise ChannelError("invalid_reason")
        elif kind != "ready" or self.ready:
            raise ChannelError("invalid_event")
        if set(message) != expected:
            raise ChannelError("unexpected_fields")
        self.sequence = message["seq"]
        if self.commands and kind == "start":
            self.mode, self.voice_enabled = message["mode"], message.get("voice", False)
        if not self.commands and kind == "ready":
            self.ready = True
        elif not self.commands and kind == "edge":
            self.held_button = message["button"] if message["action"] == "down" else None
            self.last_edge_time = message["time"]
        if kind in ("stop", "stopped", "error"):
            self.closed = True
        return message

    def decode(self, raw: bytes):
        try:
            if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_MESSAGE_BYTES:
                raise ChannelError("message_size")
            message = json.loads(raw, object_pairs_hook=_unique_object)
            return self._validate(message)
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            self.closed = True
            raise ChannelError("invalid_message") from error

    def encode(self, kind, **fields):
        identity = self.identity
        message = {"version": VERSION, "entity": identity.entity, "generation": identity.generation,
                   "token": identity.token, "seq": self.sequence + 1, "type": kind, **fields}
        try:
            raw = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError) as error:
            self.closed = True
            raise ChannelError("invalid_message") from error
        self.decode(raw)
        return raw

    def cancel_pending(self, now):
        """On pipe error/EOF/stop, cancel once; never manufacture an up/click."""
        self.closed = True
        self.ready = False
        button, self.held_button = self.held_button, None
        if not button:
            return None
        stamp = max(self.last_edge_time, now) if _finite_time(now) else self.last_edge_time
        return ButtonEdge(self.identity.entity, self.identity.generation, stamp, button, "cancel")

    def encode_edge(self, edge: ButtonEdge):
        if edge.entity != self.identity.entity or edge.generation != self.identity.generation:
            self.closed = True
            raise ChannelError("session_mismatch")
        return self.encode("edge", button=edge.button, action=edge.action, time=edge.time)


class ParentLease:
    """Finite parent liveness; malformed/backward clocks fail closed.

    Only authenticated, validated control frames may renew this lease. Every
    native capture timeout checks it; expiration stops only this run's session.
    """
    def __init__(self, now):
        if not _finite_time(now):
            raise ChannelError("invalid_clock")
        self.last_seen = now
        self.closed = False

    def alive(self, now):
        if not _finite_time(now) or not self.last_seen <= now <= self.last_seen + HEARTBEAT_TIMEOUT:
            self.closed = True
        return not self.closed

    def renew(self, now):
        if not self.alive(now):
            raise ChannelError("peer_lost")
        self.last_seen = now
