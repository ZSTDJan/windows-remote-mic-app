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

# Google initialization is explicit on every receiver connection. The runtime
# below implements v1.0, 16-kHz ADPCM and physical hold-to-talk streams only.
GET_CAPABILITIES = bytes((0x0a, 1, 0, 0, 2, 3))
CAPABILITIES_TIMEOUT = 3.0


def supported_capabilities(payload):
    return (isinstance(payload, bytes) and len(payload) == 9
            and payload[:3] == b"\x0b\x01\x00" and payload[3] & 2 != 0
            and payload[4] == 3 and 1 <= int.from_bytes(payload[5:7], "big") <= 512)


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

    def _clock(self, now):
        if (type(now) not in (int, float) or not math.isfinite(now)
                or now < self.last_now):
            raise ValueError("invalid_voice_clock")
        self.last_now = now

    def _effect(self, kind, value=None):
        return Effect(kind, self.attempt, value)

    def _begin(self, now):
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
        if self.state == "idle" and self.stream is None and not self.possible_open:
            return []
        if self.state in ("stopping", "blocked"):
            return self._close_stream()
        self.state, self.phase_at, self.reason = "stopping", now, reason
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
        if attempt == self.attempt and self.state in ("stopping", "blocked"):
            self.host_released = success is True
        return self.tick(now)

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
            self.stream, self.stop_seen = stream, None
            if stream[1] == 3:
                # A new proven HTT stream replaces the old software stream.
                self.possible_open = False
            self.closed_streams.discard(stream[0])
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
            return [self._effect("control", payload)]
        return []

    def audio(self, payload, now):
        self._clock(now)
        if self.state in ("stopping", "blocked"):
            self.last_audio = now  # Late packets postpone quiet confirmation.
            return []
        if (self.stream is None or self.state not in ("starting", "recording")
                or not isinstance(payload, bytes) or not 1 <= len(payload) <= 512):
            return []
        self.last_audio = now
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
            if (self.host_released and self.stream is None and not self.possible_open
                    and self.stop_seen is not None and now - max(self.stop_seen, self.last_audio) >= QUIET_SECONDS):
                self.state = "idle"
                return [self._effect("state", "idle")]
            if self.state == "stopping" and now - self.phase_at >= STOP_TIMEOUT:
                self.state, self.reason = "blocked", "cleanup_unconfirmed"
                return [self._effect("state", "blocked")]
        return []
