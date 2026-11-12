"""Pure Chromecast logical recording policy; no Windows/UI/audio side effects.

One owner serializes all calls. Logical attempt IDs fence host replies and GATT
completion callbacks; the enclosing receiver must additionally fence entity,
connection and process generation. The caller executes only returned Effects,
and rechecks command_valid immediately before submitting a device command.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

MODES = ("hold", "toggle")
LIMITS = (60, 120, 180, 300, 600)
DEFAULT_LIMIT = 120
START_TIMEOUT = 10.0
STOP_TIMEOUT = 5.0
QUIET_SECONDS = .3
EXTEND_SECONDS = 20.0  # Internal protocol maintenance, never a UI preference.
_HOLD_HANDOFF_PACKETS = 128  # Leave room in the host's 256-event queue for control/live data.
_HOLD_HANDOFF_BYTES = 32768  # Bounded RAM only; never keep a recording on disk.
MAX_INPUT_EPOCH = 2**31 - 1


# Google initialization is explicit on every receiver connection. The runtime
# below implements v1.0, 16-kHz ADPCM and physical hold-to-talk streams only.
GET_CAPABILITIES = bytes((0x0a, 1, 0, 0, 2, 3))
CAPABILITIES_TIMEOUT = 3.0
CAPABILITIES_MAX_ATTEMPTS = 5
VOICE_OPEN_RETRY_DELAYS = (1.0, 3.0, 5.0)
# Initial connection, three timed retries, then at most one fresh-button fallback.
VOICE_OPEN_MAX_ATTEMPTS = 1 + len(VOICE_OPEN_RETRY_DELAYS) + 1


def parse_start_epoch(data):
    """A receiver-owned cancellation generation, not the event's arrival time."""
    if not isinstance(data, str) or not re.fullmatch(r"epoch/(?:0|[1-9][0-9]{0,9})", data):
        raise ValueError("invalid_voice_start_epoch")
    epoch = int(data[6:])
    if epoch > MAX_INPUT_EPOCH:
        raise ValueError("invalid_voice_start_epoch")
    return epoch


def supported_capabilities(payload):
    """Validate an ATVV v1.0 CAPS_RESP notification.

    Layout: 0x0b | version (2) | codecs (1) | interaction (1) | frame size (2).
    ``payload[3]`` is the codec bitmap (bit1 = IMA ADPCM 16 kHz) and
    ``payload[4]`` the interaction bitmap (bit0 = push-to-talk, bit1 =
    hands-free). The interaction byte must be non-zero: a remote answering
    interaction=0 accepts the session but never emits MIC stage events, which
    is indistinguishable from a voice key that does nothing.

    Some Chromecast remotes (VID 18d1:9450, revision 0110) answer with ten
    bytes — the canonical nine-byte record plus one trailing byte. The
    canonical fields keep their offsets, so only that trailing byte is dropped
    here; every other length still fails closed.
    """
    if isinstance(payload, bytes) and len(payload) == 10 and payload[:1] == b"\x0b":
        payload = payload[:9]
    return (isinstance(payload, bytes) and len(payload) == 9
            and payload[:3] == b"\x0b\x01\x00"
            and payload[3] & 2 != 0
            and payload[4] & 0x03 != 0
            and 1 <= int.from_bytes(payload[5:7], "big") <= 512)


# A bounded diagnostic vocabulary shared by the receiver and parent. It carries
# no audio, recognized text, device address or arbitrary exception text.
LIFECYCLE_STATES = {
    "starting": "启动中", "recording": "录音中", "stopping": "结束中",
    "idle": "已结束", "blocked": "清理未确认",
}
LIFECYCLE_REASONS = {
    "none": "无", "second_press": "再次短按结束", "time_limit": "到时自动停止",
    "released": "松开语音键结束（遥控器释放通知）", "device_ended": "遥控器报告录音结束",
    "stopped": "停止请求", "host_stop": "语音主程序请求停止",
    "receiver_stop": "接收器退出（停止服务、切换或连接异常）",
    "host_start_failed": "语音程序启动失败", "start_timeout": "语音启动超时",
    "invalid_control": "控制通知无效", "unsupported_voice_stream": "语音格式不支持",
    "unsolicited_open": "收到非预期的开麦通知", "unexpected_stop": "启动中意外停止",
    "device_stop_failed": "遥控器停止通知异常", "device_open_failed": "遥控器开麦失败",
    "device_command_failed": "遥控器控制命令失败",
    "device_stop_unconfirmed": "遥控器关麦未确认", "cleanup_unconfirmed": "录音清理超时未确认",
}


def parse_lifecycle(data):
    """Validate the fixed metadata shape at the IPC boundary."""
    if not isinstance(data, str):
        raise ValueError("invalid_voice_lifecycle")
    parts = data.split("/")
    if len(parts) != 5:
        raise ValueError("invalid_voice_lifecycle")
    state, mode, limit, elapsed, reason = parts
    if (state not in LIFECYCLE_STATES or mode not in MODES
            or limit not in tuple(str(n) for n in LIMITS)
            or not re.fullmatch(r"[0-9]{1,13}", elapsed)
            or reason not in LIFECYCLE_REASONS):
        raise ValueError("invalid_voice_lifecycle")
    return state, mode, int(limit), int(elapsed), reason


def lifecycle_log_text(data):
    state, mode, limit, elapsed, reason = parse_lifecycle(data)
    limit_text = f"{limit}秒" if mode == "toggle" else "不适用"
    return (f"阶段={LIFECYCLE_STATES[state]} 模式={'开关型' if mode == 'toggle' else '按住型'} "
            f"上限={limit_text} 录音经过={elapsed / 1000:.2f}秒 "
            f"原因={LIFECYCLE_REASONS[reason]}({reason})")


@dataclass(frozen=True)
class Effect:
    kind: str
    attempt: int
    value: object = None
    reason: str | None = None  # Snapshot when one call completes and starts attempts.


class Recording:
    """Idle -> starting -> recording -> stopping -> idle (or blocked).

    'recording' requires both confirmed host readiness and an observed device
    stream. Shortcut delivery alone must never call host_result(True). Errors
    retain cleanup ownership; a timeout does not turn an unknown mic into idle.
    """
    def __init__(self, mode="hold", limit=DEFAULT_LIMIT):
        if mode not in MODES or type(limit) is not int or limit not in LIMITS:
            raise ValueError("invalid_voice_preference")
        self.mode, self.limit = mode, limit
        self.state = "idle"
        self.attempt = 0
        self.stream = None
        self.reason = ""
        self.host_ready = False
        self.host_released = True
        self.possible_open = False
        self.open_pending = False
        self.shutting_down = False
        self.started = self.phase_at = self.last_audio = self.last_now = 0.0
        self.next_extend = 0.0
        self.stop_seen = None
        self.closed_streams = set()
        self.audio_accepted = False
        self._clear_hold_start()

    def _clear_hold_start(self):
        self._hold_restart_allowed = False
        self._hold_start = None
        self._hold_packets = []
        self._hold_bytes = 0
        self._hold_audio_at = 0.0

    def _buffer_hold_packet(self, kind, payload, now):
        if (len(self._hold_packets) >= _HOLD_HANDOFF_PACKETS
                or self._hold_bytes + len(payload) > _HOLD_HANDOFF_BYTES):
            # Do not start with a truncated decoder history or wait indefinitely.
            self._clear_hold_start()
            return self._close_stream()
        self._hold_packets.append((kind, payload))
        self._hold_bytes += len(payload)
        if kind == "audio":
            self._hold_audio_at = now
        return []

    def _clock(self, now):
        if (type(now) not in (int, float) or not math.isfinite(now)
                or now < self.last_now):
            raise ValueError("invalid_voice_clock")
        self.last_now = now

    def _effect(self, kind, value=None):
        return Effect(kind, self.attempt, value)

    def _begin(self, now):
        self._clear_hold_start()
        self.attempt += 1
        self.state, self.phase_at = "starting", now
        self.reason = ""
        self.host_ready, self.host_released = False, False
        self.started, self.next_extend = 0.0, now + EXTEND_SECONDS
        self.last_audio = 0.0
        self.stop_seen = None
        self.closed_streams.clear()
        return [self._effect("host_start"), self._effect("state", "starting")]

    def _activate(self, now):
        if (self.state == "starting" and self.host_ready and self.stream is not None
                and (self.mode == "hold" or self.stream == (0, 0))):
            self.state, self.started = "recording", now
            return [self._effect("state", "recording")]
        return []

    def _close_stream(self):
        stream = self.stream[0] if self.stream is not None else 0 if self.possible_open else None
        if stream is None or stream in self.closed_streams:
            return []
        self.closed_streams.add(stream)
        return [self._effect("device", bytes((0x0d, stream)))]

    def finish(self, now, reason="stopped", *, shutdown=False):
        self._clock(now)
        self.shutting_down |= shutdown
        self._clear_hold_start()
        if self.state == "idle" and self.stream is None and not self.possible_open:
            return []
        if self.state in ("stopping", "blocked"):
            return self._close_stream()
        self.state, self.phase_at, self.reason = "stopping", now, reason
        self._hold_restart_allowed = self.mode == "hold" and reason == "released" and not self.shutting_down
        self.open_pending = False
        effects = [self._effect("host_stop", reason if reason in ("second_press", "time_limit", "device_ended") else None),
                   self._effect("state", "stopping")]
        effects += self._close_stream()
        if self.stream is None and not self.possible_open:
            self.stop_seen = now
        return effects

    def host_result(self, attempt, success, now):
        self._clock(now)
        if attempt != self.attempt or self.state != "starting":
            return []
        if success is not True:
            return self.finish(now, "host_start_failed")
        self.host_ready = True
        effects = self._activate(now)
        if self.mode == "toggle" and self.stream is None and not self.open_pending:
            self.open_pending = True
            effects.append(self._effect("device", b"\x0c\x00"))
        return effects

    def host_stopped(self, attempt, success, now):
        self._clock(now)
        effects = []
        if attempt == self.attempt and self.state in ("stopping", "blocked"):
            self.host_released = success is True
            if not self.host_released:
                pending_hold = self._hold_start is not None
                self._clear_hold_start()
                if pending_hold:
                    effects += self._close_stream()
        return effects + self.tick(now)

    def control(self, payload, now):
        self._clock(now)
        self.control_result = "unknown_control"
        if not isinstance(payload, bytes) or not payload:
            self.control_result = "invalid_control"
            return self.finish(now, "invalid_control")
        opcode = payload[0]
        if opcode == 0x04:  # AUDIO_START: reason, codec, stream.
            if len(payload) != 4 or payload[2] != 2 or not (
                    (payload[1] == 3 and 1 <= payload[3] <= 128)
                    or (payload[1] == 0 and payload[3] == 0)):
                self.control_result = "unsupported_voice_stream"
                return self.finish(now, "unsupported_voice_stream")
            self.control_result = "accepted_start"
            stream = (payload[3], payload[1])
            if stream == self.stream:
                self.control_result = "duplicate_start"
                return []  # Duplicate start must not reset host or decoder.
            # Only a fresh physical hold following a normal release may cross
            # cleanup. Keep the old STOP/quiet proof separate from new audio.
            handoff = (self.state == "stopping" and self._hold_restart_allowed
                       and not self.shutting_down and not self.possible_open
                       and stream[1] == 3 and now - self.phase_at < STOP_TIMEOUT
                       and (self.stream is None or self._hold_start is not None))
            self.stream = stream
            if stream[1] == 3:
                # A new proven HTT stream replaces the old software stream.
                self.possible_open = False
            self.closed_streams.discard(stream[0])
            if handoff:
                self._clear_hold_start()
                self._hold_restart_allowed = True
                self._hold_start = payload
                return self.tick(now)
            self.stop_seen = None
            if self.state in ("stopping", "blocked") or self.shutting_down:
                return self.finish(now, "stopped", shutdown=self.shutting_down)
            if self.state == "idle":
                if stream[1] != 3:
                    return self.finish(now, "unsolicited_open")
                effects = self._begin(now)
            elif self.state == "recording" and self.mode == "toggle" and stream[1] == 3:
                # Second physical press can start HTT BEFORE its MIC_BUTTON.
                return self.finish(now, "second_press")
            else:
                effects = []
            if stream == (0, 0):
                if not self.possible_open:
                    return effects + self.finish(now, "unsolicited_open")
                self.open_pending = False
            effects.append(self._effect("control", payload))
            return effects + self._activate(now)
        if opcode == 0x08:  # MICROPHONE_SEARCH. No fabricated release edge.
            self.control_result = "awaiting_start" if self.state == "idle" else "ignored_mic"
            if self.state == "recording" and self.mode == "toggle":
                self.control_result = "second_press"
                return self.finish(now, "second_press")
            return []  # A physical start is the proven, deduplicated first press.
        if opcode == 0x00:
            if len(payload) != 2 or payload[1] not in (0, 2):
                self.control_result = "invalid_stop"
                return self.finish(now, "device_stop_failed")
            self.control_result = "accepted_stop"
            # Once released, a waiting press must never start in the background.
            if self._hold_start is not None:
                allowed = self._hold_restart_allowed
                self._clear_hold_start()
                self._hold_restart_allowed = allowed
            previous, self.stream = self.stream, None
            self.stop_seen = now
            if previous == (0, 0):
                self.possible_open = False
            if self.state in ("stopping", "blocked"):
                # A STOP for a physical segment does not prove an outstanding
                # software OPEN has been cancelled; keep its cleanup liability.
                return self.tick(now)
            if self.state == "idle":
                return []
            effects = [self._effect("control", payload)]
            if self.mode == "hold" or self.state == "recording":
                return effects + self.finish(now, "released" if self.mode == "hold" and payload[1] == 2
                                             else "device_ended")
            if previous and previous[1] == 3 and payload[1] == 2:
                if self.host_ready and not self.open_pending:
                    self.open_pending = True
                    effects.append(self._effect("device", b"\x0c\x00"))
                return effects
            return effects + self.finish(now, "unexpected_stop")
        if opcode == 0x0c:
            self.control_result = "device_open_failed"
            return self.finish(now, "device_open_failed")
        if opcode == 0x0a and len(payload) == 7 and self.stream is not None:
            self.control_result = "accepted_sync"
            if self._hold_start is not None:
                return self._buffer_hold_packet("control", payload, now)
            return [self._effect("control", payload)]
        return []

    def audio(self, payload, now):
        self._clock(now)
        self.audio_accepted = False
        if self._hold_start is not None:
            if isinstance(payload, bytes) and 1 <= len(payload) <= 512:
                effects = self._buffer_hold_packet("audio", payload, now)
                self.audio_accepted = self._hold_start is not None
                return effects
            return []
        if self.state in ("stopping", "blocked"):
            self.last_audio = now  # Late packets postpone quiet confirmation.
            return []
        if (self.stream is None or self.state not in ("starting", "recording")
                or not isinstance(payload, bytes) or not 1 <= len(payload) <= 512):
            return []
        self.last_audio = now
        self.audio_accepted = True
        return [self._effect("audio", payload)]

    def command_valid(self, effect):
        if effect.kind != "device" or effect.attempt != self.attempt:
            return False
        command = effect.value
        if command == b"\x0c\x00":
            return (self.state == "starting" and self.host_ready and self.open_pending
                    and self.stream is None and not self.shutting_down)
        if not isinstance(command, bytes) or len(command) != 2:
            return False
        if command[0] == 0x0e:
            return (self.state == "recording" and not self.shutting_down
                    and self.stream is not None and self.stream[0] == command[1])
        return (command[0] == 0x0d and self.state in ("stopping", "blocked")
                and ((self.stream is not None and command[1] == self.stream[0])
                     or (command[1] == 0 and self.possible_open)))

    def command_submitted(self, effect):
        if not self.command_valid(effect):
            return False
        if effect.value == b"\x0c\x00":
            self.possible_open = True
        return True

    def command_result(self, effect, success, now):
        self._clock(now)
        if effect.attempt != self.attempt or success is True:
            return []
        if effect.value[0] == 0x0d:
            self._clear_hold_start()
            self.state, self.reason = "blocked", "device_stop_unconfirmed"
            return [self._effect("state", "blocked")]
        return self.finish(now, "device_command_failed")

    def tick(self, now):
        self._clock(now)
        if self.state == "starting" and now - self.phase_at >= START_TIMEOUT:
            return self.finish(now, "start_timeout")
        if self.state == "recording":
            if self.mode == "toggle" and now - self.started >= self.limit:
                return self.finish(now, "time_limit")
            if self.stream is not None and now >= self.next_extend and 0 < now - self.last_audio < 2:
                self.next_extend = now + EXTEND_SECONDS
                return [self._effect("device", bytes((0x0e, self.stream[0])))]
        if self.state in ("stopping", "blocked"):
            if self._hold_start is not None:
                if now - self.phase_at >= STOP_TIMEOUT:
                    self._clear_hold_start()
                    self.state, self.reason = "blocked", "cleanup_unconfirmed"
                    return self._close_stream() + [self._effect("state", "blocked")]
                if (self.host_released and self.stop_seen is not None
                        and now - max(self.stop_seen, self.last_audio) >= QUIET_SECONDS):
                    start, packets, last_audio = self._hold_start, self._hold_packets, self._hold_audio_at
                    # Host needs the old idle before claiming the next attempt.
                    effects = [Effect("state", self.attempt, "idle", self.reason)] + self._begin(now)
                    self.last_audio = last_audio
                    return (effects + [self._effect("control", start)]
                            + [self._effect(kind, data) for kind, data in packets])
            if (self.host_released and self.stream is None and not self.possible_open
                    and self.stop_seen is not None and now - max(self.stop_seen, self.last_audio) >= QUIET_SECONDS):
                self.state = "idle"
                return [self._effect("state", "idle")]
            if self.state == "stopping" and now - self.phase_at >= STOP_TIMEOUT:
                self._clear_hold_start()
                self.state, self.reason = "blocked", "cleanup_unconfirmed"
                return [self._effect("state", "blocked")]
        return []
