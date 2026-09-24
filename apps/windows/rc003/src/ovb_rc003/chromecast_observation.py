"""Bounded metadata journal for one Google receiver; never records audio.

Owned by the worker, shipped as ordinary application source. Observations do
not replay packets or change device/recording state. Unproven sources contribute
anonymous counts/structure only, never payloads or a claimed device identity.
"""
from collections import deque
import time

from .chromecast_buttons import STOP_REASONS

COUNTERS = frozenset({"rx_acl", "tx_acl", "other_connection", "preproof_notify",
    "controls", "audio_packets", "audio_bytes", "ordinary", "other_notify",
    "no_handler", "not_available", "accepted_audio", "ignored_audio",
    "control_rows_lost", "send_failed"})
STATES = frozenset({"idle", "starting", "recording", "stopping", "blocked", "unavailable"})
RESULTS = frozenset({"unknown_control", "invalid_control", "unsupported_voice_stream",
    "accepted_start", "duplicate_start", "awaiting_start", "ignored_mic", "second_press",
    "invalid_stop", "accepted_stop", "device_open_failed", "accepted_sync",
    "detect_ignored", "detect_pressed", "detect_release", "not_available", "no_handler",
    "accepted_caps", "unsupported_caps", "unexpected_caps"})
STAGES = frozenset({"run", "detect", "setup", "source_ready", "source_probe_scheduled",
    "hid_primary_start", "hid_primary_ready",
    "hid_fallback_start", "hid_fallback_ready", "voice_open_begin",
    "voice_ready", "voice_unavailable", "stop", "voice_caps_request", "voice_caps_ready",
    "voice_caps_write_failed", "voice_caps_timeout", "voice_caps_unsupported", "voice_caps_overflow"})
MAX_COUNT = 2**31 - 1
GATT_NUMBERS = frozenset({"status", "tx", "audio", "control", "tx_handle", "audio_handle",
                          "control_handle", "service_count", "characteristic_count", "mtu"})
CAPTURE_COUNTS = frozenset({"received", "other_provider", "other_event", "other_kind",
    "hci", "rx", "tx", "queued", "queue_overflow", "parse_failed"})


def _integer(value, low=0, high=MAX_COUNT):
    return type(value) is int and low <= value <= high


def valid_record(row):
    if not isinstance(row, dict):
        return False
    kind = row.get("kind")
    if kind == "source_probe":
        return (set(row) == {"kind", "elapsed_ms", "state", "duration_ms", "status", "services", "hresult"}
                and row["state"] in {"begin", "completed", "failed", "cancelled"}
                and all(_integer(row[k]) for k in ("elapsed_ms", "duration_ms"))
                and _integer(row["status"], -1, 65535) and _integer(row["services"], -1, 65535)
                and _integer(row["hresult"], -1, 0xffffffff))
    if kind == "source_state":
        return (set(row) == {"kind", "elapsed_ms", "phase", "reason", "api_ok", "request_seen",
                            "response_seen", "ready", "input_attribute", "proof_handle", "packet_relation"}
                and _integer(row["elapsed_ms"]) and row["phase"] in {"ready", "stop"}
                and row["reason"] in STOP_REASONS | {"ready"}
                and all(type(row[k]) is bool for k in ("api_ok", "request_seen", "response_seen", "ready"))
                and _integer(row["input_attribute"], -1, 65535)
                and _integer(row["proof_handle"], -1, 4095)
                and row["packet_relation"] in {"unknown", "same_handle", "other_handle"})
    if kind == "capture_flow":
        counts = row.get("counts")
        return (set(row) == {"kind", "counts", "first_other_event", "first_other_version", "first_other_kind",
                            "schema_step", "schema_property", "schema_code"}
                and isinstance(counts, dict) and set(counts) == CAPTURE_COUNTS
                and all(_integer(v) for v in counts.values())
                and _integer(row["first_other_event"], -1, 65535)
                and _integer(row["first_other_version"], -1, 255)
                and _integer(row["first_other_kind"], -1, 255)
                and row["schema_step"] in {"none", "size", "read", "limit", "length"}
                and row["schema_property"] in {"none", "BIP_Type", "BIP_Data", "BIP_DataLen"}
                and _integer(row["schema_code"], -1, 0xffffffff))
    if kind == "capture":
        return (set(row) == {"kind", "operation", "result", "attempt", "owner_ref"}
                and row["operation"] in {"owner_busy", "start", "restart", "recover_query", "recover_stop", "stop", "close_consumer"}
                and _integer(row["result"], 0, 0xffffffff) and _integer(row["attempt"], 1, 2)
                and isinstance(row["owner_ref"], str) and len(row["owner_ref"]) == 32
                and all(c in "0123456789abcdef" for c in row["owner_ref"]))
    if kind == "control":
        fields = row.get("fields")
        opcode = row.get("opcode")
        maximum = {0: 1, 4: 3, 8: 0, 11: 8, 12: 2}.get(opcode, 0)
        return (set(row) == {"kind", "n", "source_ms", "received_ms", "length", "opcode", "fields", "state", "result"}
                and _integer(row["n"], 1) and _integer(row["source_ms"], -60000)
                and _integer(row["received_ms"]) and _integer(row["length"], 0, 4096)
                and _integer(opcode, -1, 255) and isinstance(fields, list)
                and len(fields) <= maximum and all(_integer(v, 0, 255) for v in fields)
                and row["state"] in STATES and row["result"] in RESULTS)
    if kind == "summary":
        counts = row.get("counts")
        return (set(row) == {"kind", "elapsed_ms", "final", "counts", "audio_min", "audio_max"}
                and _integer(row["elapsed_ms"]) and type(row["final"]) is bool
                and isinstance(counts, dict) and set(counts) == COUNTERS
                and all(_integer(v) for v in counts.values())
                and _integer(row["audio_min"], 0, 4096) and _integer(row["audio_max"], 0, 4096))
    if kind == "stage":
        return (set(row) == {"kind", "elapsed_ms", "stage"}
                and _integer(row["elapsed_ms"]) and row["stage"] in STAGES)
    if kind == "gatt":
        return (set(row) == {"kind", "elapsed_ms", "step"} | GATT_NUMBERS
                and _integer(row["elapsed_ms"]) and row["step"] in (
                    "service", "characteristics", "audio_subscription", "control_subscription", "ready")
                and all(_integer(row[k], -1, 65535) for k in GATT_NUMBERS))
    return False


class Observation:
    def __init__(self, send, clock=time.monotonic):
        self.send, self.clock = send, clock
        self.started = clock()
        self.last_summary = self.started
        self.counts = dict.fromkeys(sorted(COUNTERS), 0)
        self.rows = deque()
        self.number = 0
        self.audio_min = self.audio_max = 0

    def count(self, name, amount=1):
        self.counts[name] = min(MAX_COUNT, self.counts[name] + amount)

    def _ms(self, stamp):
        return max(-60000, min(MAX_COUNT, round((stamp - self.started) * 1000)))

    def _send(self, row):
        try:
            self.send(row)
        except Exception:
            self.count("send_failed")

    def stage(self, name):
        self._send(dict(kind="stage", elapsed_ms=max(0, self._ms(self.clock())), stage=name))

    def gatt(self, data):
        self._send(dict(kind="gatt", elapsed_ms=max(0, self._ms(self.clock())), **data))

    def source_probe(self, state, started, *, status=-1, services=-1, hresult=-1):
        now = self.clock()
        self._send(dict(kind="source_probe", elapsed_ms=max(0, self._ms(now)), state=state,
                        duration_ms=max(0, min(MAX_COUNT, round((now - started) * 1000))),
                        status=status, services=services, hresult=hresult))

    def source_state(self, **data):
        self._send(dict(kind="source_state", elapsed_ms=max(0, self._ms(self.clock())), **data))

    def notification(self, attribute, value, ordinary=False):
        if ordinary:
            self.count("ordinary")
        elif attribute == 0x3f:
            self.count("controls")
        elif attribute == 0x3c:
            self.count("audio_packets")
            self.count("audio_bytes", len(value))
            self.audio_min = min(self.audio_min, len(value)) if self.counts["audio_packets"] > 1 else len(value)
            self.audio_max = max(self.audio_max, len(value))
        else:
            self.count("other_notify")

    def control(self, value, stamp, state, result):
        self.number = min(MAX_COUNT, self.number + 1)
        opcode = value[0] if value else -1
        # CAPS and fixed command headers only. SYNC contains audio predictor
        # samples; unknown opcodes and SYNC never expose their payload bytes.
        size = {0: 1, 4: 3, 8: 0, 11: 8, 12: 2}.get(opcode, 0)
        row = dict(kind="control", n=self.number, source_ms=self._ms(stamp),
                   received_ms=max(0, self._ms(self.clock())), length=len(value),
                   opcode=opcode, fields=list(value[1:1 + size]), state=state, result=result)
        if len(self.rows) == 256:
            self.rows.popleft()
            self.count("control_rows_lost")
        self.rows.append(row)

    def flush(self, final=False):
        # No audio-rate IPC. At most eight controls per worker iteration;
        # preserve repeats and source times. Summary is cumulative, including zero.
        for _ in range(min(8, len(self.rows))):
            self._send(self.rows.popleft())
        if final and self.rows:
            self.count("control_rows_lost", len(self.rows))
            self.rows.clear()
        now = self.clock()
        if final or now - self.last_summary >= 2:
            self._send(dict(kind="summary", elapsed_ms=max(0, self._ms(now)), final=final,
                            counts=dict(self.counts), audio_min=self.audio_min, audio_max=self.audio_max))
            self.last_summary = now
