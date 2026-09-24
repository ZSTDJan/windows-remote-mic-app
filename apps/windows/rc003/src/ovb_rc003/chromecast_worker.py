"""Fixed elevated receiver: proven selected-source buttons and optional voice."""
from __future__ import annotations
import asyncio
import os
import threading
import time

from . import config, remote_selection, hid_elevation_windows
from .chromecast_buttons import ButtonReceiver, empty_stop_details
from .chromecast_channel import Channel, SessionIdentity, ParentLease, DIAGNOSTIC_REASONS
from .chromecast_pipe_windows import Pipe, PipeError, inspect_peer, verify_peer, verify_same_user_image
from .chromecast_etw_windows import Capture, sensitive_logging_enabled, enable_sensitive_logging
from .chromecast_device_windows import SelectedDevice
from .chromecast_observation import Observation


def _capture_item(value):
    if not isinstance(value, tuple) or len(value) not in (3, 4):
        raise PipeError("capture_schema_failed")
    kind, data, stamp = value[:3]
    details = value[3] if len(value) == 4 else None
    if details is not None and not isinstance(details, dict):
        raise PipeError("capture_schema_failed")
    return kind, data, stamp, details


def _selected(entity):
    settings = config.load_config(config.config_path())
    return remote_selection.active_key(settings) == entity and remote_selection.active_profile(settings) == remote_selection.CHROMECAST_PROFILE


async def _probe_source(device, marker, observation):
    started = time.monotonic()
    observation.source_probe("begin", started)
    try:
        success, services = await device.probe(marker)
    except asyncio.CancelledError:
        observation.source_probe("cancelled", started)
        raise
    except Exception as error:
        code = getattr(error, "hresult", None)
        if type(code) is not int:
            code = getattr(error, "winerror", None)
        code = code & 0xffffffff if type(code) is int else -1
        observation.source_probe("failed", started, status=getattr(device, "probe_status", -1), hresult=code)
        raise
    observation.source_probe("completed", started, status=getattr(device, "probe_status", -1), services=services)
    return success, services


async def receive(identity, parent, pipe):
    commands, events = Channel(identity, commands=True), Channel(identity, commands=False)
    lease = ParentLease(time.monotonic())
    device, capture, receiver, probe = None, None, None, None
    hid_tap, hid_start, hid_deadline, hid_armed = None, None, None, False
    voice, voice_open = None, None
    voice_enabled = False
    reason, cleanup_ok, announced = "stopped", True, False
    stage = "start"
    observation = Observation(lambda row: pipe.write(events.encode("evidence", record=row)))

    def diagnostic(stage, phase, cause, details=None):
        try:
            pipe.write(events.encode("diagnostic", stage=stage, phase=phase, reason=cause,
                                     **(details if details is not None else empty_stop_details())))
        except Exception:
            pass  # A broken diagnostic pipe must never skip resource cleanup.

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
        stage = "logging"
        try:
            if not sensitive_logging_enabled():
                enable_sensitive_logging()
        except OSError:
            raise PipeError("sensitive_logging_enable_failed") from None
        if start["mode"] == "setup":
            reason = "configured"
            return 0
        stage = "device_open"
        device = SelectedDevice(identity.entity)
        # Initialization runs concurrently with the control loop below: native
        # discovery cannot prevent heartbeat/stop checks from being consumed.
        initialize = asyncio.create_task(device.open())
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
                if hid_tap is not None and hid_start is None and not hid_tap.source_alive():
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
            if capture is None:
                if not initialize.done():
                    if now > deadline:
                        raise PipeError("input_interface_unavailable" if getattr(device, "input_pending", False)
                                        else "source_unconfirmed")
                    await asyncio.sleep(.02)
                    continue
                await initialize
                stage = "capture"
                receiver = ButtonReceiver(
                    identity.entity,
                    identity.generation,
                    device.radio,
                    time.monotonic(),
                    observation=observation,
                    on_diagnostic=lambda cause, details: diagnostic(
                        "receive", "done", cause, details
                    ),
                )
                capture = Capture(identity.generation)
                capture.observe = observation._send
                capture.start()
                observation.stage("source_probe_scheduled")
                probe = asyncio.create_task(_probe_source(device, receiver.marker, observation))
            if probe is not None and probe.done():
                stage = "source_probe"
                success, services = await probe
                receiver.api_result(identity.entity, identity.generation, success=success, services=services,
                                    input_attribute=device.attribute, now=time.monotonic())
                probe = None
                if receiver.stopped:
                    diagnostic(stage, "failed", receiver.reason, receiver.stop_details)
                    raise PipeError(receiver.reason if receiver.reason == "input_payload_unavailable"
                                    else "source_unconfirmed")
            stage = "receive"
            for captured in capture.poll():
                kind, data, stamp, capture_details = _capture_item(captured)
                edges = receiver.feed(device.radio, identity.generation, kind, data, stamp,
                                      received_at=time.monotonic(), capture_details=capture_details)
                if receiver.ready and not announced:
                    pipe.write(events.encode("ready"))
                    announced = True
                    observation.stage("source_ready")
                    receiver.observe_source("ready")
                for edge in edges:
                    if announced:
                        pipe.write(events.encode_edge(edge))
                if receiver._input_uncertain and hid_tap is None:
                    from .chromecast_hid_tap_windows import HidTap

                    receiver.begin_hid_fallback(time.monotonic())
                    hid_tap = HidTap(identity.entity)
                    hid_start = asyncio.create_task(asyncio.to_thread(hid_tap.start))
                    hid_deadline = time.monotonic() + 25
                    observation.stage("hid_fallback_start")
            if hid_tap is not None:
                if hid_start is not None and hid_start.done():
                    try:
                        await hid_start
                    except Exception as error:
                        code = str(error)
                        raise PipeError(code if code in {"hid_source_unconfirmed", "hid_symbol_unavailable"}
                                        else "hid_capture_failed") from None
                    hid_start = None
                    hid_deadline = time.monotonic() + 15
                if time.monotonic() > hid_deadline and not hid_armed:
                    raise PipeError("hid_capture_failed")
                if hid_start is None:
                    for kind, payload, stamp in hid_tap.poll():
                        if kind == "error":
                            raise PipeError("hid_capture_failed")
                        if kind != "report":
                            continue
                        try:
                            value = bytes.fromhex(payload)
                        except (TypeError, ValueError):
                            raise PipeError("hid_capture_failed") from None
                        if not 0 <= time.monotonic() - stamp <= 1:
                            raise PipeError("capture_lost")
                        if not hid_armed:
                            if value == bytes(8):
                                hid_armed = True
                                observation.stage("hid_fallback_ready")
                            continue
                        for edge in receiver.feed_hid(value, stamp):
                            if announced:
                                pipe.write(events.encode_edge(edge))
            for edge in receiver.advance(time.monotonic()):
                if announced:
                    pipe.write(events.encode_edge(edge))
            if receiver.stopped:
                diagnostic(stage, "failed", receiver.reason, receiver.stop_details)
                raise PipeError(receiver.reason if receiver.reason == "input_payload_unavailable"
                                else "source_unconfirmed")
            if receiver.ready and not announced:
                pipe.write(events.encode("ready"))
                announced = True
                observation.stage("source_ready")
                receiver.observe_source("ready")
            if announced and voice_enabled and voice is None:
                from .chromecast_voice_receiver import VoiceReceiver
                voice = VoiceReceiver(device, lambda kind, attempt, data, stamp:
                    pipe.write(events.encode("voice", event=kind, attempt=attempt, data=data, time=stamp)),
                    detect_only=start["mode"] == "detect", observation=observation)
                receiver.on_notification = voice.notification
                voice_open = asyncio.create_task(voice.open())
            if voice and voice_open.done():
                stage = "voice_open"
                await voice_open
                stage = "voice_tick"
                await voice.tick()
            observation.flush()
            await asyncio.sleep(.05)
    except Exception as error:
        reason = str(error) if str(error) in {"source_unconfirmed", "unsupported_layout", "radio_ambiguous",
            "input_interface_unavailable", "input_identity_unconfirmed",
            "sensitive_logging_disabled", "sensitive_logging_enable_failed", "capture_lost",
            "input_payload_unavailable", "hid_source_unconfirmed", "hid_symbol_unavailable",
            "hid_capture_failed", "peer_lost", "invalid_message"} else "capture_failed"
        if receiver is None or not receiver.stopped:
            diagnostic(stage, "failed", str(error) if str(error) in DIAGNOSTIC_REASONS else reason)
    finally:
        observation.stage("stop")
        if hid_start is not None:
            try:
                await hid_start
            except Exception:
                pass
        if hid_tap is not None:
            try:
                await asyncio.to_thread(hid_tap.close)
            except Exception:
                cleanup_ok = False
        if voice:
            diagnostic("voice_stop", "begin", reason)
            try:
                if voice_open and not voice_open.done():
                    voice_open.cancel()
                    await asyncio.gather(voice_open, return_exceptions=True)
                voice.stop()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if receiver.stopped:
                        cleanup_ok = False
                        break  # Never write an old command on a replaced connection.
                    for captured in capture.poll():
                        kind, data, stamp, capture_details = _capture_item(captured)
                        receiver.feed(device.radio, identity.generation, kind, data, stamp,
                                      received_at=time.monotonic(), capture_details=capture_details)
                    if receiver.stopped:
                        diagnostic("voice_stop", "failed", receiver.reason, receiver.stop_details)
                        cleanup_ok = False
                        break
                    await voice.tick()
                    if voice.hardware_stopped:
                        break
                    await asyncio.sleep(.02)
                cleanup_ok &= voice.hardware_stopped
                diagnostic("voice_stop", "done" if cleanup_ok else "failed",
                           reason if cleanup_ok else "cleanup_unconfirmed")
            except Exception:
                cleanup_ok = False
                diagnostic("voice_stop", "failed", "cleanup_failed")
            finally:
                diagnostic("voice_close", "begin", reason)
                try:
                    await voice.close()
                    diagnostic("voice_close", "done", reason)
                except Exception:
                    cleanup_ok = False
                    diagnostic("voice_close", "failed", "cleanup_failed")
        if receiver:
            for edge in receiver.stop(time.monotonic()):
                try:
                    if announced and not events.closed:
                        pipe.write(events.encode_edge(edge))
                except Exception:
                    pass
        if capture:
            try:
                capture.report_flow()
            except Exception:
                pass  # Diagnostic snapshots never prevent native cleanup.
        observation.flush(final=True)
        # Stop capture before any asynchronous task/resource cleanup. If native
        # teardown fails, no clean terminal message/zero exit may be reported.
        for resource in (capture, device):
            if resource:
                cleanup_stage = "capture_stop" if resource is capture else "device_close"
                diagnostic(cleanup_stage, "begin", reason)
                try:
                    if resource is capture:
                        resource.stop()
                    else:
                        tasks = [task for task in (locals().get("initialize"), probe) if task is not None]
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        resource.close()
                    diagnostic(cleanup_stage, "done", reason)
                except Exception:
                    cleanup_ok = False
                    diagnostic(cleanup_stage, "failed", "cleanup_failed")
        if cleanup_ok and not events.closed:
            try:
                pipe.write(events.encode("stopped" if reason in ("stopped", "configured") else "error", reason=reason))
            except Exception:
                pass
    return 0 if cleanup_ok else 2


def main(args):
    pipe = None
    try:
        if len(args) != 5 or not args[0].isdigit() or not args[1].isdigit():
            return 2
        pid, born = int(args[0]), int(args[1])
        identity = SessionIdentity(*args[2:])
        if not hid_elevation_windows.query_process_elevated():
            return 2
        parent, current = inspect_peer(pid), inspect_peer(os.getpid())
        if parent.born != born or parent.pid == current.pid:
            return 2
        verify_same_user_image(parent, current)
        pipe = Pipe(identity, server=False)
        pipe.connect(time.monotonic() + 10, threading.Event())
        if pipe.peer_pid() != parent.pid:
            return 2
        verify_peer(inspect_peer(parent.pid), parent)
        from winrt.runtime import init_apartment, uninit_apartment, ApartmentType
        init_apartment(ApartmentType.MULTI_THREADED)
        try:
            from .chromecast_hid_worker import receive as receive_hid
            return asyncio.run(receive_hid(identity, parent, pipe))
        finally:
            uninit_apartment()
    except Exception:
        return 2
    finally:
        if pipe:
            pipe.close()
