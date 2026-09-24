"""Elevated voice adapter: discovered same-device handles and fixed commands.

The worker is the only owner; no thread races the recording policy or writes a
GATT characteristic. Failed voice discovery does not disable ordinary buttons.
"""
from __future__ import annotations
import asyncio
from collections import deque
import time
from . import config
from .chromecast_voice import Recording, GET_CAPABILITIES, CAPABILITIES_TIMEOUT, supported_capabilities


class VoiceReceiver:
    def __init__(self, device, send, *, detect_only=False, observation=None, direct_notifications=False):
        self.device, self.send = device, send
        self.observation = observation
        settings = config.load_config(config.config_path())
        self.gate = Recording(settings.get("remote_recording_mode", "hold"),
                              settings.get("remote_recording_limit_seconds", 120))
        self.queue = deque()
        self.pending = None
        self.available = False
        self.stopping = False
        self.pipe_failed = False
        self.detect_only = detect_only
        self.direct_notifications = direct_notifications
        self.mic_down = False
        self.seen_attributes = set()
        self.seen_decisions = set()
        self.recording_elapsed_ms = 0
        self.capabilities = None
        self._caps_event = asyncio.Event()
        self._caps_waiting = False
        self._caps_requested_at = 0.0
        self._startup = deque()
        self._startup_bytes = 0
        self._startup_overflow = False

    def _stage(self, name):
        if self.observation:
            self.observation.stage(name)

    async def _negotiate(self):
        self._caps_waiting = True
        self._caps_requested_at = time.monotonic()
        self._stage("voice_caps_request")
        try:
            try:
                await self.device.write_voice(GET_CAPABILITIES)
            except Exception:
                self._stage("voice_caps_write_failed")
                raise
            try:
                await asyncio.wait_for(self._caps_event.wait(), CAPABILITIES_TIMEOUT)
            except TimeoutError:
                self._stage("voice_caps_timeout")
                raise
            if not supported_capabilities(self.capabilities):
                self._stage("voice_caps_unsupported")
                raise ValueError("unsupported_voice_capabilities")
            if self._startup_overflow:
                self._stage("voice_caps_overflow")
                raise ValueError("voice_startup_overflow")
            self._stage("voice_caps_ready")
        finally:
            self._caps_waiting = False

    def emit(self, kind, attempt, data, stamp):
        try:
            self.send(kind, attempt, data, stamp)
        except Exception:
            self.pipe_failed = True  # Metadata delivery must not interrupt closing a stream.

    async def open(self):
        if self.observation:
            self.observation.stage("voice_open_begin")
        try:
            if self.direct_notifications:
                await self.device.open_voice(direct=True)
            else:
                await self.device.open_voice()
            if not self.detect_only:
                await self._negotiate()
            self.available = True
        except Exception:
            self.available = False
        if self.observation:
            details = getattr(self.device, "voice_evidence", None)
            if details:
                self.observation.gatt(details)
            self.observation.stage("voice_ready" if self.available else "voice_unavailable")
        self.emit("available", 0, "ready" if self.available else "unavailable", time.monotonic())
        if self.available:
            handles = {kind: handle for handle, kind in self.device.voice_attributes.items()}
            self.emit("probe", 0, f"layout/{handles['audio']:04x}/{handles['control']:04x}", time.monotonic())
            # CAPS and START may arrive in the same capture batch, before the
            # asynchronous write completes. Replay only selected-device packets
            # received AFTER valid CAPS; bounded RAM only, never a disk recording.
            for attribute, value, stamp in self._startup:
                self.notification(attribute, value, stamp)
        self._startup.clear()
        self._startup_bytes = 0

    def effects(self, effects):
        for effect in effects:
            if effect.kind == "device":
                if len(self.queue) >= 8:
                    raise RuntimeError("voice_command_overflow")
                self.queue.append(effect)
            else:
                data = effect.value.hex() if isinstance(effect.value, bytes) else effect.value or ""
                self.emit(effect.kind, effect.attempt, data, time.monotonic())
                if effect.kind == "host_start" and self.capabilities:
                    # Each host attempt creates a new decoder; apply negotiated
                    # frame size before its START/audio, including toggle mode.
                    self.emit("control", effect.attempt, self.capabilities.hex(), time.monotonic())
                if effect.kind == "state" and effect.attempt > 0:
                    gate = self.gate
                    if data == "starting":
                        self.recording_elapsed_ms = 0
                    elif data == "stopping":
                        # Freeze at stop request, excluding host startup and
                        # cleanup delays. This is not speech-only duration.
                        self.recording_elapsed_ms = (
                            max(0, round((gate.phase_at - gate.started) * 1000))
                            if gate.started else 0)
                    self.emit("lifecycle", effect.attempt,
                              f"{data}/{gate.mode}/{gate.limit}/{self.recording_elapsed_ms}/{gate.reason or 'none'}",
                              time.monotonic())

    def notification(self, attribute, value, stamp):
        kind = self.device.voice_attributes.get(attribute)
        if self._caps_waiting and kind == "control" and value[:1] == b"\x0b":
            fresh = stamp >= self._caps_requested_at and not self._caps_event.is_set()
            if fresh:
                self.capabilities = value
                self._caps_event.set()
            if self.observation:
                result = ("accepted_caps" if supported_capabilities(value) else "unsupported_caps") if fresh else "unexpected_caps"
                self.observation.control(value, stamp, "unavailable", result)
            return
        if (self._caps_waiting and supported_capabilities(self.capabilities)
                and kind in ("control", "audio")):
            if len(self._startup) < 256 and self._startup_bytes + len(value) <= 32768:
                self._startup.append((attribute, value, stamp))
                self._startup_bytes += len(value)
            else:
                self._startup_overflow = True
            return
        if not self.available:
            if self.observation and attribute in (0x3c, 0x3f):
                self.observation.count("not_available")
                if attribute == 0x3f:
                    self.observation.control(value, stamp, "unavailable", "not_available")
            return
        now = time.monotonic()
        # Bounded metadata only; never audio bytes, addresses or arbitrary text.
        if attribute not in self.seen_attributes and len(self.seen_attributes) < 8:
            self.seen_attributes.add(attribute)
            self.emit("probe", 0, f"notify/{attribute:04x}/{len(value)}", now)
        if kind == "control" and value[:1] == b"\x04":
            # Only the fixed AUDIO_START header fields, never payload/audio.
            fields = [value[i] if i < len(value) else 256 for i in (1, 2, 3)]
            self._probe_decision(f"start_fields/{fields[0]}/{fields[1]}/{fields[2]}", now)
        if self.detect_only:
            decision = "detect_ignored"
            if kind == "control" and not self.stopping:
                if value == b"\x08" or (len(value) == 4 and value[:3] == b"\x04\x03\x02"
                                        and 1 <= value[3] <= 128):
                    if not self.mic_down:
                        self.emit("mic", 0, "pressed", now)
                    self.mic_down = True
                    decision = "detect_pressed"
                elif len(value) in (1, 2) and value[0] == 0:
                    self.mic_down = False
                    decision = "detect_release"
            if kind == "control":
                if self.observation:
                    self.observation.control(value, stamp, "idle", decision)
                self._probe_decision(f"control/{value[0] if value else 256}/{len(value)}/idle/{decision}", now)
            elif kind == "audio":
                if self.observation:
                    self.observation.count("ignored_audio")
                self._probe_decision(f"audio/{len(value)}/idle/no_stream/detect", now)
            return  # Detection never decodes, opens, extends or closes a mic.
        if kind == "control":
            if self.gate.state == "idle" and value[:1] == b"\x04" and not self.stopping:
                settings = config.load_config(config.config_path())
                gate = Recording(settings.get("remote_recording_mode", "hold"),
                                 settings.get("remote_recording_limit_seconds", 120))
                gate.attempt = self.gate.attempt
                self.gate = gate
            previous = self.gate.state
            effects = self.gate.control(value, now)
            if self.observation:
                self.observation.control(value, stamp, previous, self.gate.control_result)
            self._probe_decision(f"control/{value[0] if value else 256}/{len(value)}/{previous}/{self.gate.control_result}", now)
            self.effects(effects)
        elif kind == "audio":
            effects = self.gate.audio(value, now)
            if self.observation:
                self.observation.count("accepted_audio" if effects else "ignored_audio")
            stream = "stream" if self.gate.stream is not None else "no_stream"
            decision = "accepted" if effects else "ignored"
            self._probe_decision(f"audio/{len(value)}/{self.gate.state}/{stream}/{decision}", now)
            self.effects(effects)

    def _probe_decision(self, data, now):
        # Log distinct decisions/lengths, not just the first packet per handle.
        # A hard per-receiver bound prevents audio-rate logging and IPC flooding.
        if data not in self.seen_decisions and len(self.seen_decisions) < 48:
            self.seen_decisions.add(data)
            self.emit("probe", 0, data, now)

    def host(self, message):
        if not self.available or self.detect_only:
            return
        attempt, result, now = message["attempt"], message["result"], time.monotonic()
        if result in ("ready", "failed"):
            self.effects(self.gate.host_result(attempt, result == "ready", now))
        elif result in ("released", "release_failed"):
            self.effects(self.gate.host_stopped(attempt, result == "released", now))
        elif result == "stop" and attempt == self.gate.attempt:
            self.effects(self.gate.finish(now, "host_stop"))

    def stop(self):
        self.stopping = True
        if not self.detect_only:
            self.effects(self.gate.finish(time.monotonic(), "receiver_stop", shutdown=True))

    @property
    def hardware_stopped(self):
        gate = self.gate
        return (self.detect_only or not self.available or (self.pending is None and not self.queue and gate.stream is None
                and not gate.possible_open and (gate.stop_seen is None or time.monotonic()
                - max(gate.stop_seen, gate.last_audio) >= .3)))

    async def tick(self):
        if not self.available or self.detect_only:
            return
        if self.pending is not None and self.pending[1].done():
            effect, task = self.pending
            self.pending = None
            try:
                await task
                success = True
            except Exception:
                success = False
            self.effects(self.gate.command_result(effect, success, time.monotonic()))
        self.effects(self.gate.tick(time.monotonic()))
        if self.pending is None:
            while self.queue:
                effect = self.queue.popleft()
                if self.gate.command_submitted(effect):
                    self.pending = effect, asyncio.create_task(self.device.write_voice(effect.value))
                    break

    async def close(self):
        self._caps_waiting = False
        self._startup.clear()
        self._startup_bytes = 0
        if self.pending:
            self.pending[1].cancel()
            await asyncio.gather(self.pending[1], return_exceptions=True)
            self.pending = None
