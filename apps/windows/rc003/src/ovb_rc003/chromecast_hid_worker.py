"""Google receiver: selected HID input and same-device WinRT ATVV notifications.

The legacy BTHPORT trace is never a readiness gate or an input source here.
"""
from __future__ import annotations

import asyncio
import time

from . import config, remote_selection
from .chromecast_buttons import HidButtonReceiver, empty_stop_details
from .chromecast_channel import Channel, ParentLease, DIAGNOSTIC_REASONS
from .chromecast_device_windows import SelectedDevice
from .chromecast_hid_tap_windows import HidTap
from .chromecast_observation import Observation
from .chromecast_pipe_windows import PipeError, inspect_peer, verify_peer


def _selected(entity):
    settings = config.load_config(config.config_path())
    return (remote_selection.active_key(settings) == entity
            and remote_selection.active_profile(settings) == remote_selection.CHROMECAST_PROFILE)


async def receive(identity, parent, pipe):
    commands, events = Channel(identity, commands=True), Channel(identity, commands=False)
    lease = ParentLease(time.monotonic())
    device = tap = receiver = voice = voice_open = initialize = tap_start = None
    announced = False
    cleanup_ok = True
    reason, stage = "stopped", "start"
    voice_enabled = False
    observation = Observation(lambda row: pipe.write(events.encode("evidence", record=row)))

    def diagnostic(phase, cause, details=None):
        try:
            pipe.write(events.encode("diagnostic", stage=stage, phase=phase, reason=cause,
                                     **(details if details is not None else empty_stop_details())))
        except Exception:
            pass

    try:
        while True:
            if not lease.alive(time.monotonic()):
                raise PipeError("peer_lost")
            raw = pipe.read()
            if raw is not None:
                start = commands.decode(raw)
                if start["type"] != "start":
                    raise PipeError("invalid_message")
                voice_enabled = start.get("voice", False)
                break
            await asyncio.sleep(.02)
        stage = "selection"
        observation.stage(start["mode"])
        if not _selected(identity.entity):
            raise PipeError("source_unconfirmed")
        if start["mode"] == "setup":
            reason = "configured"
            cleanup_ok = True
            return 0

        stage = "device_open"
        device = SelectedDevice(identity.entity)
        initialize = asyncio.create_task(device.open(require_input=False))
        deadline = time.monotonic() + 20
        last_peer = 0.0
        while True:
            now = time.monotonic()
            if not lease.alive(now):
                raise PipeError("peer_lost")
            if now - last_peer >= 1:
                verify_peer(inspect_peer(parent.pid), parent)
                if not _selected(identity.entity):
                    raise PipeError("source_unconfirmed")
                if tap is not None and tap_start is None and not tap.source_alive():
                    raise PipeError("hid_source_unconfirmed")
                last_peer = now
            stop = False
            for _ in range(32):
                raw = pipe.read()
                if raw is None:
                    break
                command = commands.decode(raw)
                lease.renew(now)
                if command["type"] == "stop":
                    stop = True
                    break
                if command["type"] == "voice_host" and voice:
                    voice.host(command)
            if stop:
                break
            if device.changed.is_set():
                raise PipeError("radio_ambiguous")
            if initialize is not None:
                if not initialize.done():
                    if now > deadline:
                        raise PipeError("source_unconfirmed")
                    await asyncio.sleep(.02)
                    continue
                await initialize
                initialize = None
                stage = "capture"
                receiver = HidButtonReceiver(identity.entity, identity.generation)
                tap = HidTap(identity.entity)
                tap_start = asyncio.create_task(asyncio.to_thread(tap.start))
                deadline = time.monotonic() + 25
                observation.stage("hid_primary_start")
            if tap_start is not None:
                if not tap_start.done():
                    if now > deadline:
                        raise PipeError("hid_capture_failed")
                    await asyncio.sleep(.02)
                    continue
                try:
                    await tap_start
                except Exception as error:
                    code = str(error)
                    raise PipeError(code if code in {"hid_source_unconfirmed", "hid_symbol_unavailable"}
                                    else "hid_capture_failed") from None
                tap_start = None
                deadline = time.monotonic() + 5
            stage = "receive"
            for kind, payload, stamp in tap.poll():
                if kind == "error":
                    raise PipeError("hid_capture_failed")
                if kind == "hook_ready":
                    if not receiver.ready:
                        if not tap.source_alive():
                            raise PipeError("hid_source_unconfirmed")
                        receiver.confirm_source()
                        pipe.write(events.encode("ready"))
                        announced = True
                        observation.stage("hid_primary_ready")
                        observation.stage("source_ready")
                    continue
                if kind != "report" or not receiver.ready:
                    continue
                try:
                    value = bytes.fromhex(payload)
                except (TypeError, ValueError):
                    raise PipeError("hid_capture_failed") from None
                if not 0 <= time.monotonic() - stamp <= 1:
                    raise PipeError("capture_lost")
                for edge in receiver.feed(value, stamp):
                    pipe.write(events.encode_edge(edge))
                if receiver.stopped:
                    diagnostic("failed", receiver.reason, receiver.stop_details)
                    raise PipeError(receiver.reason)
            if not announced and now > deadline:
                raise PipeError("hid_capture_failed")
            if announced and voice_enabled and voice is None:
                from .chromecast_voice_receiver import VoiceReceiver
                voice = VoiceReceiver(device, lambda kind, attempt, data, stamp:
                    pipe.write(events.encode("voice", event=kind, attempt=attempt, data=data, time=stamp)),
                    detect_only=start["mode"] == "detect", observation=observation,
                    direct_notifications=True)
                voice_open = asyncio.create_task(voice.open())
            if voice is not None:
                for attribute, value, stamp in device.poll_voice():
                    voice.notification(attribute, value, stamp)
                if voice_open.done():
                    await voice_open
                    await voice.tick()
            observation.flush()
            await asyncio.sleep(.05)
        cleanup_ok = True
    except Exception as error:
        reason = str(error) if str(error) in DIAGNOSTIC_REASONS else "capture_failed"
        if receiver is None or not receiver.stopped:
            diagnostic("failed", reason)
    finally:
        observation.stage("stop")
        if initialize is not None:
            initialize.cancel()
            await asyncio.gather(initialize, return_exceptions=True)
        if tap_start is not None:
            try:
                await tap_start
            except Exception:
                pass
        if tap is not None:
            try:
                await asyncio.to_thread(tap.close)
            except Exception:
                cleanup_ok = False
        if voice is not None:
            stage = "voice_stop"
            diagnostic("begin", reason)
            try:
                if voice_open is not None and not voice_open.done():
                    voice_open.cancel()
                    await asyncio.gather(voice_open, return_exceptions=True)
                voice.stop()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not voice.hardware_stopped:
                    if device is not None:
                        for attribute, value, stamp in device.poll_voice():
                            voice.notification(attribute, value, stamp)
                    await voice.tick()
                    await asyncio.sleep(.02)
                cleanup_ok = cleanup_ok and voice.hardware_stopped
                diagnostic("done" if voice.hardware_stopped else "failed",
                           reason if voice.hardware_stopped else "cleanup_unconfirmed")
            except Exception:
                cleanup_ok = False
                diagnostic("failed", "cleanup_failed")
            finally:
                stage = "voice_close"
                try:
                    await voice.close()
                    diagnostic("done", reason)
                except Exception:
                    cleanup_ok = False
                    diagnostic("failed", "cleanup_failed")
        if receiver is not None:
            for edge in receiver.stop(time.monotonic()):
                try:
                    if announced and not events.closed:
                        pipe.write(events.encode_edge(edge))
                except Exception:
                    pass
        if device is not None:
            stage = "device_close"
            try:
                await device.close_voice()
            except Exception:
                cleanup_ok = False
                diagnostic("failed", "cleanup_failed")
            try:
                device.close()
            except Exception:
                cleanup_ok = False
                diagnostic("failed", "cleanup_failed")
            else:
                if cleanup_ok:
                    diagnostic("done", reason)
        observation.flush(final=True)
        if cleanup_ok and not events.closed:
            try:
                pipe.write(events.encode("stopped" if reason in ("stopped", "configured") else "error", reason=reason))
            except Exception:
                pass
    return 0 if cleanup_ok else 2
