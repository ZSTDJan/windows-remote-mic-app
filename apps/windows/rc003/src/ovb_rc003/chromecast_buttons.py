"""Bounded, transport-independent Chromecast ordinary-button receive core.

No Windows APIs, raw capture files, voice decoding, gesture policy or injection.
The transport must resolve the selected entity/radio and discover its HID input
attribute; this module never guesses either from a name or packet shape. All
state belongs to one run. ETW direction numbers: event=2, ACL RX=3, ACL TX=4.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct
import uuid

from . import remote_layout, remote_selection

_KEYS = {value: key for key, value in remote_layout.CHROMECAST_REPORT_CODES.items()}
_PROOF_SECONDS = 15.0
_FRAGMENT_SECONDS = 2.0
_MAX_L2CAP_BYTES = 4096
_MAX_FRAGMENTS = 32
_MAX_DELIVERY_SECONDS = 1.0
_INPUT_RECOVERY_SECONDS = 5.0
_INPUT_LOSS_BUDGET = 2
STOP_REASONS = frozenset({"stopped", "source_time_reversed", "fragment_timeout", "source_timeout",
    "source_query_failed", "repeated_source_result", "stale_source_packet", "ambiguous_probe",
    "invalid_source_time", "invalid_acl", "invalid_acl_length", "orphan_fragment", "replaced_fragment",
    "unsupported_acl_boundary", "missing_l2cap_header", "invalid_l2cap_length", "fragment_capacity",
    "invalid_hci_event", "connection_changed", "unknown_key_report", "input_payload_unavailable"})
_CAPTURE_DETAIL_KEYS = frozenset({
    "capture_metadata_available",
    "capture_layout",
    "capture_event_id",
    "capture_event_version",
    "capture_user_data_length",
    "capture_property_length",
    "capture_extracted_length",
    "capture_length_match",
})


def empty_stop_details():
    return dict(
        event_kind=0,
        attribute=0,
        length=0,
        report_code=-1,
        tail_nonzero=False,
        acl_header_parsed=False,
        acl_issue="none",
        acl_flags=-1,
        acl_handle=-1,
        acl_boundary=-1,
        acl_declared_length=-1,
        acl_actual_length=-1,
        acl_length_relation="unknown",
        acl_handle_valid=False,
        acl_reserved_flags_clear=False,
        acl_payload_nonempty=False,
        acl_length_match=False,
        l2cap_header_parsed=False,
        l2cap_declared_length=-1,
        l2cap_cid=-1,
        att_header_parsed=False,
        att_opcode=-1,
        att_attribute=-1,
        att_missing_length=-1,
        capture_metadata_available=False,
        capture_layout="unknown",
        capture_event_id=-1,
        capture_event_version=-1,
        capture_user_data_length=-1,
        capture_property_length=-1,
        capture_extracted_length=-1,
        capture_length_match=False,
    )


class ReceiveError(ValueError):
    """Reception is uncertain; cancel held input, never synthesize a click."""

    def __init__(self, reason: str, *, details=None):
        super().__init__(reason)
        self.details = dict(details or {})


def _acl_header_details(data: bytes):
    """Return bounded structural evidence without retaining packet contents."""

    flags, declared_length = struct.unpack_from("<HH", data)
    handle, boundary = flags & 0xFFF, (flags >> 12) & 3
    actual_length = len(data) - 4
    if actual_length < declared_length:
        relation = "short"
    elif actual_length > declared_length:
        relation = "surplus"
    else:
        relation = "exact"
    handle_valid = handle <= 0xEFF
    reserved_flags_clear = not bool(flags & 0xC000)
    payload_nonempty = declared_length > 0
    length_match = actual_length == declared_length
    if not handle_valid:
        issue = "invalid_handle"
    elif not reserved_flags_clear:
        issue = "reserved_flags"
    elif not payload_nonempty:
        issue = "empty_payload"
    elif not length_match:
        issue = "declared_length_mismatch"
    else:
        issue = "none"
    return dict(
        acl_header_parsed=True,
        acl_issue=issue,
        acl_flags=flags,
        acl_handle=handle,
        acl_boundary=boundary,
        acl_declared_length=declared_length,
        acl_actual_length=actual_length,
        acl_length_relation=relation,
        acl_handle_valid=handle_valid,
        acl_reserved_flags_clear=reserved_flags_clear,
        acl_payload_nonempty=payload_nonempty,
        acl_length_match=length_match,
    )


def _available_payload_details(data: bytes):
    """Describe only headers that are actually present in a short ACL record."""

    details = dict(
        l2cap_header_parsed=False,
        l2cap_declared_length=-1,
        l2cap_cid=-1,
        att_header_parsed=False,
        att_opcode=-1,
        att_attribute=-1,
        att_missing_length=-1,
    )
    if not isinstance(data, bytes) or len(data) < 8:
        return details
    payload = data[4:]
    length, cid = struct.unpack_from("<HH", payload)
    details.update(
        l2cap_header_parsed=True,
        l2cap_declared_length=length,
        l2cap_cid=cid,
    )
    if len(payload) < 7:
        return details
    details.update(
        att_header_parsed=True,
        att_opcode=payload[4],
        att_attribute=int.from_bytes(payload[5:7], "little"),
        att_missing_length=max(0, length - (len(payload) - 4)),
    )
    return details


def _time(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ReceiveError("invalid_source_time")
    return float(value)


@dataclass(frozen=True)
class ButtonEdge:
    entity: str
    generation: str
    time: float
    button: str
    action: str  # down / up / cancel; cancel must not execute release gestures.


class HidButtonReceiver:
    """Selected-device HID reports are the sole ordinary-button source."""

    def __init__(self, entity: str, generation: str):
        self.entity, self.generation = entity, generation
        self.button = None
        self.ready = False
        self.stopped = False
        self.reason = "stopped"
        self.last_time = 0.0
        self.stop_details = empty_stop_details()

    def confirm_source(self):
        if self.stopped or self.ready:
            raise ReceiveError("hid_source_unconfirmed")
        self.ready = True

    def feed(self, value: bytes, now: float):
        if self.stopped or not self.ready:
            raise ReceiveError("hid_source_unconfirmed")
        now = _time(now)
        if now < self.last_time:
            return self.stop(self.last_time, "source_time_reversed")
        self.last_time = now
        if not isinstance(value, bytes) or len(value) != 8 or any(value[1:]) or (value[0] and value[0] not in _KEYS):
            self.stop_details.update(length=len(value) if isinstance(value, bytes) else 0,
                                     report_code=value[0] if value else -1,
                                     tail_nonzero=bool(any(value[1:])) if isinstance(value, bytes) else False)
            return self.stop(now, "unknown_key_report")
        button = _KEYS.get(value[0])
        if button == self.button:
            return []
        edges = []
        if self.button:
            edges.append(ButtonEdge(self.entity, self.generation, now, self.button,
                                    "cancel" if button else "up"))
        self.button = button
        if button:
            edges.append(ButtonEdge(self.entity, self.generation, now, button, "down"))
        return edges

    def stop(self, now: float, reason="stopped"):
        if self.stopped:
            return []
        now = max(self.last_time, _time(now))
        edges = ([ButtonEdge(self.entity, self.generation, now, self.button, "cancel")]
                 if self.button else [])
        self.button = None
        self.ready = False
        self.stopped = True
        self.reason = reason
        return edges


class AttAssembler:
    """Separate RX/TX fragments, fixed memory/age bounds, exact L2CAP lengths."""
    def __init__(self):
        self.fragments = {}

    def clear(self):
        self.fragments.clear()

    def expire(self, now):
        expired = {key[1] for key, value in self.fragments.items() if now - value[0] > _FRAGMENT_SECONDS}
        self.fragments = {key: value for key, value in self.fragments.items() if now - value[0] <= _FRAGMENT_SECONDS}
        return expired

    def feed(self, kind, data, now):
        if kind not in (3, 4) or not isinstance(data, bytes) or len(data) < 4:
            raise ReceiveError("invalid_acl", details={"acl_issue": "header_unavailable"})
        details = _acl_header_details(data)
        size = details["acl_declared_length"]
        handle, boundary = details["acl_handle"], details["acl_boundary"]
        key = (kind, handle)
        if (
            not details["acl_handle_valid"]
            or not details["acl_reserved_flags_clear"]
            or not details["acl_length_match"]
            or not details["acl_payload_nonempty"]
        ):
            self.fragments.pop(key, None)
            raise ReceiveError("invalid_acl_length", details=details)
        payload = data[4:]
        if boundary == 1:
            prior = self.fragments.pop(key, None)
            if prior is None or now - prior[0] > _FRAGMENT_SECONDS:
                raise ReceiveError("orphan_fragment")
            started, buffer = prior
            payload = buffer + payload
        elif boundary in (0, 2):
            if self.fragments.pop(key, None) is not None:
                raise ReceiveError("replaced_fragment")
            started = now
        else:
            raise ReceiveError("unsupported_acl_boundary")
        if len(payload) < 4:
            raise ReceiveError("missing_l2cap_header")
        length, cid = struct.unpack_from("<HH", payload)
        if length > _MAX_L2CAP_BYTES or len(payload) > length + 4:
            raise ReceiveError("invalid_l2cap_length")
        if len(payload) < length + 4:
            if len(self.fragments) >= _MAX_FRAGMENTS:
                raise ReceiveError("fragment_capacity")
            self.fragments[key] = (started, payload)
            return None
        if cid != 4:
            return None
        return handle, payload[4:]


class ButtonReceiver:
    """One entity/run/radio, fresh nonce proof, then discovered ordinary input.

    Call feed on the ordered capture thread; call advance periodically even when
    silent. API callbacks are serialized onto that thread too. api_result is
    supplied by the selected-entity uncached GATT query, not by a wire message.
    Source time must be normalized to the same monotonic clock used at creation.
    """
    def __init__(self, entity: str, generation: str, radio: str, started: float, *,
                 on_notification=None, on_diagnostic=None, observation=None):
        if not all(remote_selection.valid_key(item) for item in (entity, generation, radio)):
            raise ReceiveError("invalid_identity")
        self.entity, self.generation, self.radio = entity, generation, radio
        self.started = self.last_time = _time(started)
        self.last_source_time = self.started
        self.marker = uuid.uuid4()
        self._request = b"\x06\x01\x00\xff\xff\x00\x28" + self.marker.bytes[::-1]
        self.assembler = AttAssembler()
        self.handle = None
        self.attribute = None
        self.sent_at = None
        self.response = False
        self.api_ok = False
        self.stopped = False
        self.button = None
        self.reason = "awaiting_source"
        self.on_notification = on_notification
        self.on_diagnostic = on_diagnostic
        self.observation = observation
        self._input_loss_diagnostic_sent = False
        self._input_uncertain = False
        self._input_loss_count = 0
        self._input_recovery_deadline = None
        self._input_loss_details = None
        self.using_hid = False
        self.stop_details = empty_stop_details()
        self._event_details = empty_stop_details()

    @property
    def ready(self):
        return not self.stopped and self.api_ok and self.response and self.attribute is not None

    def _edge(self, action, now):
        return ButtonEdge(self.entity, self.generation, now, self.button, action)

    def observe_source(self, phase, reason="ready"):
        # Snapshot before stop() clears proof. A handle match alone is NOT proof
        # of the selected device; ready remains the only full attribution gate.
        if self.observation is None:
            return
        packet_handle = self._event_details.get("acl_handle", -1)
        relation = "unknown"
        if self.handle is not None and packet_handle >= 0:
            relation = "same_handle" if self.handle == packet_handle else "other_handle"
        self.observation.source_state(phase=phase, reason=reason, api_ok=self.api_ok,
            request_seen=self.sent_at is not None, response_seen=self.response, ready=self.ready,
            input_attribute=self.attribute if self.attribute is not None else -1,
            proof_handle=self.handle if self.handle is not None else -1, packet_relation=relation)

    def stop(self, now, reason="stopped"):
        if self.stopped:
            return []  # Keep the first failure through subsequent cleanup.
        now = max(self.last_time, _time(now))
        edges = [self._edge("cancel", now)] if self.button else []
        self.observe_source("stop", reason)
        self.button = None
        self.stopped = True
        self.reason = reason
        self.stop_details = dict(self._event_details)
        self.assembler.clear()
        self.handle = self.attribute = None
        self.api_ok = self.response = False
        self._input_uncertain = False
        self._input_recovery_deadline = None
        self._input_loss_details = None
        self.using_hid = False
        return edges

    def advance(self, now):
        now = _time(now)
        if self.stopped:
            return []
        self._event_details = empty_stop_details()
        if now < self.last_time:
            return self.stop(self.last_time, "source_time_reversed")
        self.last_time = now
        expired = self.assembler.expire(now)
        if self.handle in expired:
            return self.stop(now, "fragment_timeout")
        if (
            self._input_uncertain and not self.using_hid
            and self._input_recovery_deadline is not None
            and now > self._input_recovery_deadline
        ):
            self._event_details = dict(self._input_loss_details or empty_stop_details())
            return self.stop(now, "input_payload_unavailable")
        if not self.ready and now - self.started > _PROOF_SECONDS:
            return self.stop(now, "source_timeout")
        return []

    def api_result(self, entity, generation, *, success, services, input_attribute, now):
        """Bind only after both the API and live request/response agree.

        input_attribute must come from same-device verified HID discovery; no
        default handle is provided. A later transport test must prove discovery.
        """
        if entity != self.entity or generation != self.generation:
            return []
        edges = self.advance(now)
        if self.stopped:
            return edges
        if (success is not True or type(services) is not int or services != 0
                or type(input_attribute) is not int or not 1 <= input_attribute <= 0xFFFF):
            return self.stop(now, "source_query_failed")
        if self.api_ok:
            return self.stop(now, "repeated_source_result")
        self.api_ok = True
        self.attribute = input_attribute
        if self.ready:
            self.reason = "ready"
        return edges

    def feed(self, radio, generation, kind, data, now, *, received_at=None, capture_details=None):
        if self.stopped or radio != self.radio or generation != self.generation:
            return []
        if self.observation and kind in (3, 4):
            self.observation.count("rx_acl" if kind == 3 else "tx_acl")
        # An unrelated connection must not cancel or finish the selected key.
        if kind in (3, 4) and isinstance(data, bytes) and len(data) >= 2 and self.ready:
            if int.from_bytes(data[:2], "little") & 0xFFF != self.handle:
                if self.observation:
                    self.observation.count("other_connection")
                return []
        now = _time(now)
        received_at = now if received_at is None else _time(received_at)
        edges = self.advance(received_at)
        if self.stopped:
            return edges
        self._event_details = dict(empty_stop_details(), event_kind=kind if kind in (2, 3, 4) else 0,
                                   length=min(len(data), 65535) if isinstance(data, bytes) else 0)
        if isinstance(capture_details, dict):
            self._event_details.update(
                (key, capture_details[key])
                for key in _CAPTURE_DETAIL_KEYS
                if key in capture_details
            )
        if now < self.last_source_time or not 0 <= received_at - now <= _MAX_DELIVERY_SECONDS:
            return edges + self.stop(received_at, "stale_source_packet")
        self.last_source_time = now
        if kind == 2:
            return edges + self._event(data, now)
        if kind not in (3, 4):
            return edges
        had_fragment = False
        if isinstance(data, bytes) and len(data) >= 4:
            flags = int.from_bytes(data[:2], "little")
            had_fragment = (kind, flags & 0xFFF) in self.assembler.fragments
        try:
            packet = self.assembler.feed(kind, data, received_at)
        except ReceiveError as error:
            self._event_details.update(error.details)
            self._event_details.update(_available_payload_details(data))
            if self._recoverable_input_loss(kind, data, capture_details, had_fragment):
                self.assembler.clear()
                if self.using_hid:
                    return edges
                if self.button:
                    edges.append(self._edge("cancel", now))
                    self.button = None
                self._input_uncertain = True
                self._input_loss_count += 1
                if self._input_recovery_deadline is None:
                    self._input_recovery_deadline = received_at + _INPUT_RECOVERY_SECONDS
                self._input_loss_details = dict(self._event_details)
                if not self._input_loss_diagnostic_sent and self.on_diagnostic is not None:
                    self._input_loss_diagnostic_sent = True
                    try:
                        self.on_diagnostic("input_payload_unavailable", dict(self._event_details))
                    except BaseException:
                        pass  # Diagnostics cannot change receive ownership.
                if self._input_loss_count >= _INPUT_LOSS_BUDGET:
                    return edges + self.stop(now, "input_payload_unavailable")
                return edges
            return edges + self.stop(now, str(error))
        if packet is None:
            return edges
        handle, att = packet
        if kind == 4 and att == self._request:
            if self.sent_at is not None:
                return edges + self.stop(now, "ambiguous_probe")
            self.handle, self.sent_at = handle, now
        elif (kind == 3 and self.handle == handle and self.sent_at is not None
              and att == b"\x01\x06\x01\x00\x0a"):
            if not self.response:
                self.response = True
        elif kind == 3 and len(att) >= 3 and not self.ready:
            if self.observation and att[0] in (0x1B, 0x1D):
                self.observation.count("preproof_notify")  # Anonymous: not yet proven selected source.
        elif kind == 3 and self.ready and self.handle == handle and len(att) >= 3:
            if att[0] in (0x1B, 0x1D):
                attribute = int.from_bytes(att[1:3], "little")
                if self.observation:
                    self.observation.notification(attribute, att[3:], attribute == self.attribute)
                if attribute == self.attribute:
                    if not self.using_hid:
                        edges += self._report(att[3:], now)
                elif self.on_notification is not None:
                    self.on_notification(attribute, att[3:], now)
                elif self.observation and attribute in (0x3c, 0x3f):
                    self.observation.count("no_handler")
                    if attribute == 0x3f:
                        self.observation.control(att[3:], now, "unavailable", "no_handler")
        if self.ready:
            self.reason = "ready"
        return edges

    def begin_hid_fallback(self, now):
        """Retire ETW ordinary input after its first proven eight-byte loss."""
        if self.stopped or not self.ready or not self._input_uncertain or self.using_hid:
            raise ReceiveError("input_payload_unavailable")
        self.using_hid = True
        self._input_uncertain = False
        self._input_recovery_deadline = None
        self._input_loss_details = None
        return []

    def feed_hid(self, value, now):
        """Accept one source-bound driver report only after the transport switch."""
        if self.stopped or not self.ready or not self.using_hid:
            raise ReceiveError("input_payload_unavailable")
        # ETW records are processed first; a callback queued a few ms earlier
        # must not reverse the shared monotonic clock.
        now = max(self.last_time, _time(now))
        edges = self.advance(now)
        if self.stopped:
            return edges
        return edges + self._report(value, now)

    def _event(self, data, now):
        if not isinstance(data, bytes) or len(data) < 2 or len(data) != data[1] + 2:
            return self.stop(now, "invalid_hci_event")
        body = data[2:]
        handle = None
        if data[0] == 5 and len(body) == 4 and body[0] == 0:
            handle = int.from_bytes(body[1:3], "little")
        elif data[0] == 0x3E and len(body) >= 4 and body[0] in (1, 10, 0x29) and body[1] == 0:
            handle = int.from_bytes(body[2:4], "little")
        if handle is not None and handle == self.handle:
            return self.stop(now, "connection_changed")
        return []

    def _report(self, value, now):
        # Only the selected ordinary HID report contributes these bounded
        # shape fields. Never retain a raw packet or any voice notification.
        self._event_details.update(attribute=self.attribute, length=min(len(value), 65535),
                                   report_code=value[0] if value else -1, tail_nonzero=bool(any(value[1:])))
        if len(value) != 8 or any(value[1:]) or (value[0] and value[0] not in _KEYS):
            return self.stop(now, "unknown_key_report")
        if self._input_uncertain:
            if value[0]:
                return []
            self._input_uncertain = False
            self._input_loss_count = 0
            self._input_recovery_deadline = None
            self._input_loss_details = None
            return []
        button = _KEYS.get(value[0])
        if button == self.button:
            return []
        edges = []
        if self.button:
            edges.append(self._edge("cancel" if button else "up", now))
        self.button = button
        if button:
            edges.append(self._edge("down", now))
        return edges

    def _recoverable_input_loss(self, kind, data, capture_details, had_fragment):
        """Recognize the observed BTHPORT header-only ordinary notification."""

        details = self._event_details
        return (
            kind == 3
            and self.ready
            and not had_fragment
            and details["acl_header_parsed"]
            and details["acl_handle"] == self.handle
            and details["acl_boundary"] in (0, 2)
            and details["acl_handle_valid"]
            and details["acl_reserved_flags_clear"]
            and details["acl_declared_length"] == 15
            and details["acl_actual_length"] == 7
            and details["l2cap_header_parsed"]
            and details["l2cap_declared_length"] == 11
            and details["l2cap_cid"] == 4
            and details["att_header_parsed"]
            and details["att_opcode"] in (0x1B, 0x1D)
            and details["att_attribute"] == self.attribute
            and details["att_missing_length"] == 8
            and isinstance(capture_details, dict)
            and details["capture_metadata_available"]
            and details["capture_layout"] == "bthport_402_tdh"
            and details["capture_event_id"] == 402
            and details["capture_event_version"] == 0
            and details["capture_user_data_length"] == 19
            and details["capture_property_length"] == len(data) == 11
            and details["capture_extracted_length"] == 11
            and details["capture_length_match"]
        )
