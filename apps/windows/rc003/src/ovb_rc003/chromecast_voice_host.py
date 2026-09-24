"""Main-process voice consumer; reuses the existing output and host shortcut.

Callbacks only enqueue bounded messages. A single host handles preparation,
decode and cleanup. No audio is logged or saved. WeType's existing microphone
confirmation is the first supported startup proof; it is not visual bubble OCR.
"""
from __future__ import annotations
from collections import deque
import hashlib
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable
from .voice_audio_session import VoiceAudioSession
from .voice_shortcut_session import VoiceShortcutSession

from .atvv_session import ATVVSession
from .chromecast_host_activity import (
    CaptureWatch,
    read_doubao_capture,
    read_sogou_capture,
)
from .chromecast_wetype_toggle import ToggleShortcut
from . import chromecast_doubao_handsfree
from . import chromecast_wetype_finish
from . import config
from . import hotkey
from . import sogou_submit_windows
from . import voice_playback_session_windows as playback_sessions
from . import bridge_runtime_status as status, voice_controller, key_detection_bridge, win32_input, voice_program_manager


# ATVV decodes mono 16 kHz PCM. Five seconds of startup audio occupy at most
# 50 worker slots; live audio uses the same 100 ms blocks to preserve headroom.
_OUTPUT_BLOCK_SAMPLES = 1600
_MAX_STARTUP_SAMPLES = 80000


@dataclass(frozen=True)
class VoiceHostServices:
    """Only the shared capabilities used by the Chromecast recording policy."""
    audio: VoiceAudioSession
    shortcut: VoiceShortcutSession
    settings: Callable
    config_root: object
    logger: object
    configured_backend: Callable
    voice_mapping_enabled: Callable
    ensure_key_tracking: Callable
    reload_settings: Callable
    apply_pending_settings: Callable
    begin_diagnostic: Callable
    finish_diagnostic: Callable
    set_active: Callable
    set_result: Callable
    wait_sogou: Callable
    disable_forwarding: Callable


class VoiceHost:
    def __init__(self, services: VoiceHostServices):
        self.services = services
        self.client = None
        self.events = queue.Queue(maxsize=256)
        self.stopping = threading.Event()
        self.input_lost = threading.Event()
        self.input_epoch = 0
        self._input_lock = threading.Lock()
        self.thread = None
        self._cleanup_lock = threading.Lock()
        self.attempt = 0
        self.state = "idle"
        self.confirmed = False
        self.ready_sent = False
        self.engaged = False
        self.diagnostic_started = False
        self.closed = True
        self.decoder = None
        self.pending_pcm = deque()
        self.pending_samples = 0
        self._output_pcm = []
        self._playback_cleanup_pending = False
        self.start_at = 0.0
        self.failure_result = None
        self.provider = "wetype"
        self.capture_watch = None
        self._capture_watch_status = None
        self._tracking_loss_seen = False
        self._tracking_loss_at = 0.0
        self._tracking_keys_released = False
        self._toggle = None
        self._toggle_stop_sent = False
        self._sogou_finish_attempted = False
        self._wetype_finish_binding = chromecast_wetype_finish.TargetBinding()
        self._wetype_finish_attempted = False
        self._toggle_playback_guard = None
        self._toggle_guard_deadline = self._toggle_guard_next = 0.0
        self._hold_backend = None
        self._hold_tokens = None
        self._doubao_physicalizer_generation = None
        self._doubao_handsfree = False
        self._doubao_finish_attempted = False
        self._settings_claimed = False
        self._cancelled_attempts = set()

    def start(self, client):
        self.client = client
        self.thread = threading.Thread(target=self._run, name="chromecast-voice-host", daemon=True)
        self.thread.start()

    def enqueue(self, event):
        if self.stopping.is_set():
            return
        try:
            with self._input_lock:
                if event.get("event") == "host_stop":
                    self._cancelled_attempts.add(int(event.get("attempt", 0)))
                    if len(self._cancelled_attempts) > 32:
                        self._cancelled_attempts.discard(min(self._cancelled_attempts))
                self.events.put_nowait(dict(event, _input_epoch=self.input_epoch,
                                            _received_at=time.monotonic()))
        except queue.Full:
            self.request_stop()
            if self.client:
                self.client.cancel.set()

    def request_stop(self):
        self.stopping.set()

    def cancel_recording(self):
        """Cancel on tracker loss without destroying the reusable voice services.

        A local epoch rejects queued starts from before the loss, even if the
        tracker has already recovered when the voice thread handles them.
        """
        with self._input_lock:
            self.input_epoch += 1
            self.input_lost.set()

    def cancel_recording_and_wait(self, timeout):
        """Give a failing right-Alt hook a bounded window to deliver owned UPs."""

        self.cancel_recording()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while not self.closed and time.monotonic() < deadline:
            time.sleep(.01)
        return self.closed

    def stop(self):
        self.request_stop()
        if self.thread:
            self.thread.join(6)
        if self.thread and not self.thread.is_alive() and not self.closed:
            self._close_resources()
        if self.thread and self.thread.is_alive() or not self.closed:
            snapshot = (bool(self.thread and self.thread.is_alive()), self.closed,
                        self.services.audio.writer is not None, self.services.audio.sink is not None)
            if snapshot != getattr(self, "_last_stop_snapshot", None):
                self._last_stop_snapshot = snapshot
                self.services.logger.warning(
                    "Chromecast voice stop pending: thread_alive=%s closed=%s writer_present=%s playback_present=%s",
                    *snapshot)
            raise RuntimeError("语音清理尚未确认，暂时不能切换设备。")

    def reply(self, result):
        if self.client and self.attempt:
            self.client.voice_host(self.attempt, result)

    def _attempt_cancelled(self, attempt):
        with self._input_lock:
            return attempt in self._cancelled_attempts

    def _start_host(self, attempt, input_epoch=None):
        services = self.services
        if self.state != "idle" or not self.closed or attempt <= self.attempt:
            return
        # Refresh before claiming the settings services.  Once claimed, a reload
        # can only become pending, so provider/mode/backend/hotkey cannot be
        # mixed across one attempt.
        services.reload_settings()
        with services.shortcut.lock:
            if self.state != "idle" or not self.closed or attempt <= self.attempt:
                return
            self.attempt, self.state = attempt, "starting"
            self.start_at = time.monotonic()
            self.closed = False
            self._settings_claimed = True
            self.confirmed = self.ready_sent = False
            self.engaged = False
            self.failure_result = None
            self.capture_watch = None
            self._capture_watch_status = None
            self._tracking_loss_seen = False
            self._tracking_loss_at = 0.0
            self._tracking_keys_released = False
            self._toggle = None
            self._toggle_stop_sent = False
            self._sogou_finish_attempted = False
            self._wetype_finish_binding = chromecast_wetype_finish.TargetBinding()
            self._wetype_finish_attempted = False
            self._toggle_playback_guard = None
            self._hold_backend = None
            self._hold_tokens = None
            self._doubao_physicalizer_generation = None
            self._doubao_handsfree = False
            self._doubao_finish_attempted = False
            self.pending_pcm.clear()
            self.pending_samples = 0
            self._output_pcm.clear()
            self._playback_cleanup_pending = False
            self.provider = voice_program_manager.normalize_voice_program_settings(
                services.settings().get("voice_program")
            )["provider"]
            issue = voice_program_manager.voice_configuration_issue(
                services.settings().get("voice_program")
            )
            trigger = str(
                services.settings().get("remote_recording_mode", "hold")
            ).strip().lower()
        # The running service also owns the mapping page's detection request.
        # Claim before output/hotkey preparation; suppress this whole attempt.
        if key_detection_bridge.publish_next_button(services.config_root, "mic"):
            services.logger.info("key detection captured button=mic; mapped action suppressed")
            with services.shortcut.lock:
                self.closed, self.state = True, "stopping"
                self._settings_claimed = False
                services.apply_pending_settings()
            self.reply("failed")  # Receiver closes the observed physical stream only.
            return
        if (self.stopping.is_set() or self.input_lost.is_set()
                or self._attempt_cancelled(attempt)
                or (input_epoch is not None and input_epoch != self.input_epoch)):
            self._fail_voice("keyboard tracking changed before voice startup")
            return
        if issue:
            self._fail_voice(issue)
            return
        self.decoder = ATVVSession(gain_db=services.settings().get("gain_db", 10.0))
        # Do not call a generic shortcut return value 'host ready'. Other host
        # proof adapters can be added without changing the recording policy.
        if (self.provider not in (
                    "wetype",
                    "sogou",
                    voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
                )
                or (self.provider == "wetype" and services.configured_backend() != "wetype_hotkey")
                or not services.voice_mapping_enabled()):
            services.logger.warning("Chromecast voice: selected provider is not supported")
            self._fail_voice("voice host is not supported or voice mapping is disabled")
            return
        with services.shortcut.lock:
            if services.shortcut.pending_tokens and not services.shortcut.release_pending():
                self._fail_voice("previous voice shortcut cleanup is still pending")
                return
            if self.provider == "sogou":
                self._start_sogou(input_epoch)
                return
            if (
                self.provider == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME
                and trigger == "toggle"
            ):
                self._start_doubao_handsfree(input_epoch)
                return
            if config.voice_hotkey_trigger_for_settings(services.settings()) == "toggle":
                self._start_toggle(input_epoch)
                return
            tokens = tuple(services.shortcut.hotkey.modifiers) + (services.shortcut.hotkey.key,)
            backend = services.configured_backend()
            if not win32_input.can_begin_tracked_hold(tokens):
                self._fail_voice("keyboard safety tracking is unavailable; shortcut was not sent")
                return
            if (
                self.provider == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME
                and not services.ensure_key_tracking(tokens, backend)
            ):
                self._fail_voice("豆包右 Alt 按键跟踪不可用；未发送快捷键。")
                return
            if not services.audio.open():
                self._fail_voice("audio output could not open", status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED)
                return
            services.audio.stats.reset()
            services.begin_diagnostic(
                provider_shortcut_mode="hold",
                effective_hotkey_tokens=tokens,
                effective_backend=backend,
            )
            self.diagnostic_started = True
            self.capture_watch = CaptureWatch(
                read_doubao_capture
                if self.provider == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME
                else None,
                fast_start=self.provider == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
            )
            self.capture_watch.begin()
            cancelled = lambda: (
                self.stopping.is_set()
                or self.input_lost.is_set()
                or self._attempt_cancelled(attempt)
                or input_epoch is not None and input_epoch != self.input_epoch
            )
            if cancelled():
                return
            action = services.shortcut.controller.on_mic_button_pressed()
            if self.provider == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME:
                self._hold_backend = backend
                self._hold_tokens = tokens
                delivered = services.shortcut.apply(
                    action,
                    backend_override=backend,
                    tokens_override=tokens,
                    cancelled=cancelled,
                )
                self._doubao_physicalizer_generation = (
                    services.shortcut.doubao_physicalizer.generation
                )
            else:
                delivered = services.shortcut.apply(action)
            if not delivered:
                services.shortcut.controller.cancel_pending()
                if cancelled():
                    services.set_result(status.VOICE_RUNTIME_NOT_TESTED)
                    return  # Intentional startup cancellation, not host failure.
                self._fail_voice("voice shortcut was not delivered")
                return
            self.engaged = True
            if cancelled():
                return  # Poll cancels this attempt before ready/PCM can escape.
            services.set_active(True)
            services.set_result(status.VOICE_RUNTIME_ACTIVE)

    def _start_doubao_handsfree(self, input_epoch):
        services = self.services
        shortcut = config.voice_hotkey_for_provider(
            services.settings(),
            voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
            trigger="toggle",
        )
        if not shortcut:
            self._fail_voice("请先刷新或手动录入豆包免按模式快捷键。")
            return
        try:
            spec = hotkey.HotkeySpec.parse(shortcut)
            tokens = (*spec.modifiers, spec.key)
            if any(item.state == 1 for item in read_doubao_capture()):
                self._fail_voice("豆包已有未结束的语音采集；本次未发送启动快捷键。")
                return
        except Exception:
            self._fail_voice("豆包采集状态无法确认；本次未发送启动快捷键。")
            return
        backend = services.configured_backend()
        if not win32_input.can_begin_tracked_hold(tokens):
            self._fail_voice("keyboard safety tracking is unavailable; shortcut was not sent")
            return
        if not services.ensure_key_tracking(tokens, backend):
            self._fail_voice("豆包按键跟踪不可用；未发送免按模式快捷键。")
            return
        if not services.audio.open():
            self._fail_voice("audio output could not open", status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED)
            return
        services.audio.stats.reset()
        services.begin_diagnostic(
            provider_shortcut_mode="toggle",
            effective_hotkey_tokens=tuple(tokens),
            effective_backend=backend,
        )
        self.diagnostic_started = True
        self.capture_watch = CaptureWatch(read_doubao_capture, fast_start=True)
        self.capture_watch.begin()
        cancelled = lambda: (
            self.stopping.is_set()
            or self.input_lost.is_set()
            or self._attempt_cancelled(self.attempt)
            or input_epoch is not None and input_epoch != self.input_epoch
        )
        if cancelled():
            return
        down = services.shortcut.controller.on_mic_button_pressed()
        delivered = services.shortcut.apply(
            down,
            backend_override=backend,
            tokens_override=tokens,
            cancelled=cancelled,
        )
        self._doubao_physicalizer_generation = services.shortcut.doubao_physicalizer.generation
        if not delivered:
            services.shortcut.controller.cancel_pending()
            self._fail_voice("豆包免按模式启动快捷键未成功发送。")
            return
        up = services.shortcut.controller.on_mic_button_released()
        if up is None or not services.shortcut.apply(
            up,
            backend_override=backend,
            tokens_override=tokens,
        ):
            if up is not None:
                services.shortcut.controller.restore_pending(up)
            self._fail_voice("豆包免按模式启动快捷键未完整释放。")
            return
        self._hold_backend = backend
        self._hold_tokens = tokens
        self._doubao_handsfree = True
        self.engaged = True
        if cancelled():
            return
        services.set_active(True)
        services.set_result(status.VOICE_RUNTIME_ACTIVE)
        services.logger.info(
            "Chromecast Doubao hands-free attempt=%s action=start sent_once=True "
            "keys_released=True capture_state=%s",
            self.attempt,
            self.capture_watch.status,
        )

    def _prepare_sogou_shortcut(self):
        if not self.services.wait_sogou():
            raise OSError("搜狗语音程序尚未运行")
        return False  # Global shortcut: no input-profile switching.

    def _start_sogou(self, input_epoch):
        services = self.services
        trigger = config.voice_hotkey_trigger_for_settings(services.settings())
        shortcut = config.voice_hotkey_for_provider(services.settings(), "sogou", trigger=trigger)
        if not shortcut:
            self._fail_voice("请先刷新或手动录入搜狗当前模式的快捷键。")
            return
        if not services.audio.open():
            self._fail_voice("audio output could not open", status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED)
            return
        self.capture_watch = CaptureWatch(read_sogou_capture, fast_start=trigger == "hold")
        self.capture_watch.begin()
        services.audio.stats.reset()
        if trigger == "toggle":
            try:
                diagnostic_spec = hotkey.HotkeySpec.parse(shortcut)
                diagnostic_tokens = tuple(
                    (*diagnostic_spec.modifiers, diagnostic_spec.key)
                )
            except hotkey.HotkeyParseError:
                diagnostic_tokens = (shortcut,)
        else:
            diagnostic_tokens = tuple(
                (*services.shortcut.hotkey.modifiers, services.shortcut.hotkey.key)
            )
        services.begin_diagnostic(
            provider_shortcut_mode=trigger,
            effective_hotkey_tokens=diagnostic_tokens,
            effective_backend=(
                "toggle_shortcut"
                if trigger == "toggle"
                else services.configured_backend()
            ),
        )
        self.diagnostic_started = True
        cancelled = lambda: (self.stopping.is_set() or self.input_lost.is_set() or
                             input_epoch is not None and input_epoch != self.input_epoch)
        try:
            if trigger == "toggle":
                self._toggle = ToggleShortcut(shortcut, prepare=self._prepare_sogou_shortcut)
                self._toggle.send(cancelled)
            else:
                self._prepare_sogou_shortcut()
                if cancelled():
                    raise OSError("Sogou start cancelled before shortcut")
                if not services.shortcut.apply(services.shortcut.controller.on_mic_button_pressed()):
                    raise OSError("Sogou hold shortcut was not delivered")
        except Exception:
            services.logger.exception("Chromecast Sogou shortcut failed; no automatic resend")
            self._fail_voice("搜狗快捷键未成功发送；未自动重发。")
            return
        self.engaged = True
        if cancelled():
            self._fail_voice("Sogou cancelled after send; no compensating toggle")
            return
        services.set_active(True)
        services.set_result(status.VOICE_RUNTIME_ACTIVE)
        services.logger.info("Chromecast Sogou attempt=%s trigger=%s shortcut=%s sent_once=True capture_state=%s",
                           self.attempt, trigger, shortcut, self.capture_watch.status)
        if trigger == "toggle":
            self.confirmed = self.ready_sent = True  # Local audio permission, not recognition proof.
            self.reply("ready")

    def _start_toggle(self, input_epoch):
        services = self.services
        shortcut = config.voice_hotkey_for_provider(services.settings(), "wetype", trigger="toggle")
        if not shortcut:
            self._fail_voice("请先刷新或手动录入微信“启动语音输入”的开关快捷键。")
            return
        if not services.audio.open():
            self._fail_voice("audio output could not open", status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED)
            return
        self._toggle = ToggleShortcut(shortcut)
        try:
            self._toggle_playback_guard = playback_sessions.prepare_playback_mute_guard(
                services.settings().get("output_endpoint_name", ""))
        except Exception:
            services.logger.exception("Chromecast toggle playback mute baseline unavailable")
        services.audio.stats.reset()
        try:
            diagnostic_spec = hotkey.HotkeySpec.parse(shortcut)
            diagnostic_tokens = tuple(
                (*diagnostic_spec.modifiers, diagnostic_spec.key)
            )
        except hotkey.HotkeyParseError:
            diagnostic_tokens = (shortcut,)
        services.begin_diagnostic(
            provider_shortcut_mode="toggle",
            effective_hotkey_tokens=diagnostic_tokens,
            effective_backend="toggle_shortcut",
        )
        self.diagnostic_started = True
        cancelled = lambda: (self.stopping.is_set() or self.input_lost.is_set() or
                             self._attempt_cancelled(self.attempt) or
                             input_epoch is not None and input_epoch != self.input_epoch)
        baseline_failure = None

        def begin_capture_watch():
            nonlocal baseline_failure
            # Preparation can switch the active input profile.  Take the
            # baseline afterwards so only this shortcut's new capture counts.
            self.capture_watch = CaptureWatch(fast_start=True)
            self.capture_watch.begin()
            if self.capture_watch.baseline is None:
                baseline_failure = "微信录音状态无法确认；本次未发送启动快捷键。"
                raise OSError("WeType capture baseline unavailable")
            if self.capture_watch.baseline:
                baseline_failure = "微信已有未结束的语音采集；本次未发送启动快捷键。"
                raise OSError("WeType capture already active")
        try:
            switched = self._toggle.send(cancelled, before_send=begin_capture_watch)
        except Exception:
            services.logger.exception("Chromecast toggle shortcut failed; no automatic resend")
            self._fail_voice(baseline_failure or "微信开关快捷键发送失败；未自动重发。")
            return
        self.engaged = True
        if cancelled():
            self._fail_voice("toggle cancelled after send; no compensating toggle")
            return
        self._toggle_guard_deadline = time.monotonic() + 2.5
        self._toggle_guard_next = 0.0
        services.logger.info("Chromecast toggle attempt=%s action=physical_press shortcut=%s switched=%s "
                           "sent_once=True keys_released=True host_state=waiting_capture",
                           self.attempt, shortcut, switched)

    def _toggle_second_press(self):
        if (self._toggle is None or self._toggle_stop_sent or self.closed or not self.engaged
                or self.state not in ("starting", "recording")):
            return
        if self.provider == "wetype" and not self.confirmed:
            return
        self._toggle_stop_sent = True  # Never resend after a failure or duplicate notification.
        try:
            switched = self._toggle.send(lambda: self.stopping.is_set() or self.input_lost.is_set())
            self.services.logger.info("Chromecast toggle attempt=%s action=physical_press "
                                   "sent_once=True keys_released=True switched=%s host_state=unobserved",
                                   self.attempt, switched)
        except Exception:
            self.services.logger.exception("Chromecast toggle second press failed; no automatic resend")

    def _fail_voice(self, reason, result=status.VOICE_RUNTIME_HOST_START_FAILED, *, command="failed"):
        self.failure_result = result
        self.services.logger.warning("Chromecast voice failed: %s", reason)
        self.services.set_result(result)
        self.reply(command)

    def _observe_tracking_loss(self):
        # Shared key ownership remains mandatory. This is a tracker failure,
        # not a keyboard/mouse gesture or foreground-window change.
        if not self.closed and self.input_lost.is_set() and not self._tracking_loss_seen:
            self._tracking_loss_seen = True
            self._tracking_loss_at = time.monotonic()
            self.services.logger.info(
                "Chromecast recording attempt=%s 原因=输入跟踪失效停止(keyboard_tracking_lost); "
                "phase=tracking_loss_observed elapsed_ms=%.1f", self.attempt, 0.0)
            self.reply("stop")

    def _release_host_shortcut(self):
        if self._toggle is not None:
            return self._toggle.cleanup()  # UP-only, never an automatic toggle.
        services = self.services
        action = services.shortcut.controller.reset()
        if action is not None and self._hold_backend is not None:
            delivered = services.shortcut.apply(
                action,
                backend_override=self._hold_backend,
                tokens_override=self._hold_tokens,
            )
        elif action is not None:
            delivered = services.shortcut.apply(action)
        else:
            delivered = True
        if action is not None and not delivered:
            services.shortcut.controller.restore_pending(action)
        pending_released = services.shortcut.release_pending()
        if self.provider == "sogou":
            return pending_released and not services.shortcut.controller.active
        backend = self._hold_backend or "wetype_hotkey"
        return (pending_released and not services.shortcut.controller.active
                and not services.shortcut.control_flag("completion_pending", backend)
                and not services.shortcut.control_flag("cleanup_pending", backend))

    def _stop_host(self, reason="", received_at=None):
        # Preserve only this attempt's bound capture before normal cleanup clears
        # its watcher. No other stop reason or cleanup retry may submit the host.
        finish_deadline = ((time.monotonic() if received_at is None else received_at)
                           + chromecast_wetype_finish.STOP_WINDOW_SECONDS)
        finish_wetype = (
            reason in ("device_ended", "time_limit") and self.provider == "wetype"
            and self._toggle is not None and not self._toggle_stop_sent
            and not self._wetype_finish_attempted and not self.closed
            and self.engaged and self.confirmed and self.state == "recording"
            and self.capture_watch is not None and self.capture_watch.status == "tracking"
        )
        if finish_wetype:
            self._wetype_finish_attempted = True
        if self.provider == "wetype" and self._toggle is not None and not self.closed:
            self.services.logger.info(
                "Chromecast WeType finish decision attempt=%s reason=%s eligible=%s "
                "bound=%s confirmed=%s capture_state=%s",
                self.attempt, reason or "cleanup", finish_wetype,
                self._wetype_finish_binding.target is not None, self.confirmed,
                self.capture_watch.status if self.capture_watch is not None else "unbound")
        wetype_target = self._wetype_finish_binding.take_target()
        finish_sogou = (reason == "time_limit" and self.provider == "sogou"
                        and self._toggle is not None and not self._toggle_stop_sent
                        and not self._sogou_finish_attempted and not self.closed
                        and self.engaged and self.state == "recording")
        finish_doubao = (
            reason in ("second_press", "time_limit")
            and self.provider == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME
            and self._doubao_handsfree
            and not self._doubao_finish_attempted
            and not self.closed
            and self.engaged
            and self.state == "recording"
        )
        capture_identity = self.capture_watch.identity if self.capture_watch is not None else None
        if finish_sogou:
            self._sogou_finish_attempted = True
        if finish_doubao:
            self._doubao_finish_attempted = True
        # Cleanup retries must not keep querying or repeat capture-ended stop requests.
        self.capture_watch = None
        services = self.services
        if self.closed:
            self.reply("released")
            return True
        output_healthy = self._doubao_physicalizer_healthy()
        self.state, self.confirmed = "stopping", False
        self._toggle_playback_guard = None
        self.pending_pcm.clear()
        self.pending_samples = 0
        released = False
        self._observe_tracking_loss()
        with services.shortcut.lock:
            services.disable_forwarding()
            # Tracker loss can arrive while acquiring the lock or before the
            # receiver's stop event. Preserve key-first cleanup on that failure.
            self._observe_tracking_loss()
            tail, self._output_pcm = self._output_pcm, []
            if (tail and not self._tracking_loss_seen and not self.stopping.is_set()
                    and not self.input_lost.is_set() and output_healthy
                    and self.failure_result is None):
                # A normal remote release drains the last partial block before
                # releasing its shortcut. Tracker loss discards that tail.
                self._submit_output(tail)
            if self._tracking_loss_seen:
                if not self._release_host_shortcut():
                    services.set_active(False)
                    services.set_result(status.VOICE_RUNTIME_HOST_STOP_FAILED)
                    self.reply("release_failed")
                    return False  # Keep ownership; retry keys before any slow audio flush.
                if not self._tracking_keys_released:
                    self._tracking_keys_released = True
                    services.logger.info(
                        "Chromecast recording attempt=%s phase=shortcut_released elapsed_ms=%.1f",
                        self.attempt, (time.monotonic() - self._tracking_loss_at) * 1000)
                audio_released = self._finish_playback("Chromecast keyboard tracking lost")
                released = True
            else:
                audio_released = self._finish_playback("Chromecast voice end")
                released = self._release_host_shortcut()
            released = released and audio_released
            services.set_active(False)
            if released and (self._toggle is not None or self.provider == "sogou") and self.failure_result is None:
                # Do not leave the settings page stuck in ACTIVE, or claim that
                # WeType recognized audio merely because a toggle was delivered.
                services.set_result(status.VOICE_RUNTIME_NOT_TESTED)
            if released and self.engaged:
                if self.failure_result is None and self._toggle is None and self.provider in {
                    "wetype",
                    voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
                }:
                    services.shortcut.record_audio_result(services.audio.stats.frames)
                elif self.failure_result is not None:
                    services.set_result(self.failure_result)
            if released:
                self._settings_claimed = False
                self._doubao_physicalizer_generation = None
                services.apply_pending_settings()
            elif not released:
                services.set_result(status.VOICE_RUNTIME_HOST_STOP_FAILED)
            if released and self.diagnostic_started:
                services.finish_diagnostic(
                    "chromecast_session_closed", pcm_frames=services.audio.stats.frames)
                self.diagnostic_started = False
        self.closed = released
        if finish_wetype:
            result, observed, error = "cleanup_incomplete", "unobserved", 0
            if released and self.failure_result is None:
                result, observed, error = chromecast_wetype_finish.finish(
                    wetype_target, deadline=finish_deadline,
                    cancelled=lambda: (self.stopping.is_set() or self.input_lost.is_set()
                                       or self._tracking_loss_seen))
            services.logger.info(
                "Chromecast WeType finish attempt=%s reason=%s result=%s "
                "observation=%s native_error=%s shortcut_fallback=False retry=False",
                self.attempt, reason, result, observed, error)
        if finish_sogou:
            # Outside the audio lock. Native UIA is isolated in a time-bounded
            # child so an unresponsive host cannot hold up cleanup indefinitely.
            outcome = "audio_failed" if self._playback_cleanup_pending else "cleanup_incomplete"
            if released and self.failure_result is None:
                outcome = ("recording_cancelled" if self.stopping.is_set() or self.input_lost.is_set()
                           else sogou_submit_windows.finish_with_timeout(
                               capture_identity, cancel_event=self.stopping))
            services.logger.info("Chromecast Sogou finish attempt=%s reason=time_limit result=%s "
                               "shortcut_fallback=False retry=False completion=unobserved",
                               self.attempt, outcome)
        if finish_doubao:
            outcome = "audio_failed" if self._playback_cleanup_pending else "cleanup_incomplete"
            if released and self.failure_result is None:
                outcome = (
                    "recording_cancelled"
                    if self.stopping.is_set() or self.input_lost.is_set()
                    else chromecast_doubao_handsfree.finish_with_timeout(
                        capture_identity,
                        cancel_event=self.stopping,
                    )
                )
            services.logger.info(
                "Chromecast Doubao hands-free finish attempt=%s reason=%s "
                "result=%s private_rpc=True retry=False completion=unobserved",
                self.attempt,
                reason,
                outcome,
            )
        if self._toggle is not None:
            services.logger.info("Chromecast toggle attempt=%s audio_cleanup=%s automatic_toggle_sent=False "
                               "host_state=unobserved", self.attempt, released)
        if released:
            with self._input_lock:
                self._cancelled_attempts.discard(self.attempt)
            if self._tracking_loss_seen:
                services.logger.info(
                    "Chromecast recording attempt=%s phase=cleanup_finished elapsed_ms=%.1f",
                    self.attempt, (time.monotonic() - self._tracking_loss_at) * 1000)
        self.reply("released" if released else "release_failed")
        return released

    def _handle(self, event):
        kind, attempt = event["event"], event["attempt"]
        if kind == "available":
            self.services.logger.info("Chromecast voice control availability=%s", event["data"])
            return
        if kind == "host_start":
            self._start_host(attempt, event.get("_input_epoch"))
            return
        if attempt != self.attempt:
            return
        if kind == "host_stop":
            with self._input_lock:
                self._cancelled_attempts.add(attempt)
            if event.get("data") == "second_press":
                self._toggle_second_press()
            self._stop_host(event.get("data", ""), event.get("_received_at"))
        elif kind == "state":
            self.services.logger.info("Chromecast recording attempt=%s state=%s", attempt, event["data"])
            if event["data"] == "idle" and self.closed:
                self.state = "idle"
                self.decoder = None
            elif (not self._attempt_cancelled(attempt)
                  and event["data"] == "recording" and self.confirmed):
                self.state = "recording"
        elif kind == "control" and self.decoder and self.state in ("starting", "recording"):
            if self._attempt_cancelled(attempt):
                return
            self.decoder.handle_control(bytes.fromhex(event["data"]))
        elif kind == "audio" and self.decoder and self.decoder.mic_open and self.state in ("starting", "recording"):
            if self._attempt_cancelled(attempt):
                return
            samples = self.decoder.handle_audio(bytes.fromhex(event["data"]))
            if not samples:
                return
            if self.confirmed:
                self._output(samples)
            elif self.pending_samples + len(samples) <= _MAX_STARTUP_SAMPLES:
                self.pending_pcm.append(samples)
                self.pending_samples += len(samples)
            else:
                self.reply("failed")
                self._stop_host()

    def _output(self, samples):
        if (self.stopping.is_set() or self.input_lost.is_set() or not self.confirmed
                or self._attempt_cancelled(self.attempt)
                or not self._doubao_physicalizer_healthy()):
            return
        self._output_pcm.extend(samples)
        while len(self._output_pcm) >= _OUTPUT_BLOCK_SAMPLES:
            block = self._output_pcm[:_OUTPUT_BLOCK_SAMPLES]
            del self._output_pcm[:_OUTPUT_BLOCK_SAMPLES]
            if not self._submit_output(block):
                self._stop_host()
                return

    def _submit_output(self, samples):
        writer = self.services.audio.writer
        if writer is not None and writer.submit(samples):
            return True
        self.failure_result = self.failure_result or status.VOICE_RUNTIME_HOST_STOP_FAILED
        self._playback_cleanup_pending = True
        self.reply("stop")
        return False

    def _finish_playback(self, reason):
        if not self._playback_cleanup_pending:
            flush = self.services.audio.flush(reason)
            if flush.error is None:
                return flush.completed
            # A failed recording remains failed, but a historical write error
            # is not evidence that its resources are still alive.
            self.failure_result = self.failure_result or status.VOICE_RUNTIME_HOST_STOP_FAILED
            self._playback_cleanup_pending = True
            self.services.logger.error("Chromecast audio failed during %s: %s", reason, flush.error)
        return self._retire_playback()

    def _retire_playback(self):
        services = self.services
        writer = services.audio.writer
        if writer is not None:
            if not services.audio.stop_writer():
                return False
        if services.audio.sink is not None:
            try:
                services.audio.close_sink()
            except Exception:
                services.logger.exception("Chromecast audio endpoint cleanup pending")
                return False
        return True

    def _poll_host(self):
        if self._attempt_cancelled(self.attempt) and not self.closed:
            # The queued host_stop still owns its reason (second_press,
            # time_limit, etc.).  Cancellation blocks ready/PCM immediately,
            # but the event itself performs the one semantic stop action.
            return
        if not self._doubao_physicalizer_healthy():
            with self._input_lock:
                self._cancelled_attempts.add(self.attempt)
            self._fail_voice(
                "Doubao input adapter was lost; recording cancelled",
                command="stop",
            )
            self._stop_host()
            return
        if self._toggle_playback_guard is not None:
            now = time.monotonic()
            if now >= self._toggle_guard_deadline or self.stopping.is_set() or self.input_lost.is_set():
                self._toggle_playback_guard = None
            elif now >= self._toggle_guard_next:
                self._toggle_guard_next = now + .1
                try:
                    outcome = self._toggle_playback_guard.restore_if_muted()
                except Exception:
                    outcome = "failed"
                if outcome != "waiting":
                    self._toggle_playback_guard = None
                    self.services.logger.info("Chromecast toggle playback mute guard=%s", outcome)
        if self.input_lost.is_set():
            if not self.closed:
                # "failed" only cancels a STARTING receiver; an already
                # RECORDING receiver needs the explicit stop command.
                self._fail_voice("keyboard safety tracking was lost; recording cancelled", command="stop")
                self._stop_host()
            self.input_lost.clear()
            return
        now = time.monotonic()
        if self.engaged and not self.closed and self.capture_watch is not None:
            ended = self.capture_watch.poll(now)
            current = self.capture_watch.status
            if self.provider == "wetype" and self._toggle is not None:
                outcome = self._wetype_finish_binding.poll(
                    self.capture_watch.identity, current, now)
                if outcome is not None:
                    self.services.logger.info("Chromecast WeType finish binding attempt=%s result=%s",
                                            self.attempt, outcome)
            if current != self._capture_watch_status:
                identity = self.capture_watch.identity
                # Only process identity and a correlation token; session strings
                # can contain paths. Capture ownership is not bubble visibility.
                capture_token = (hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()[:16]
                                 if identity is not None else "none")
                self.services.logger.info(
                    "Chromecast host activity attempt=%s state=%s provider=%s "
                    "capture_pid=%s capture_token=%s baseline_count=%s bubble=unobserved",
                    self.attempt, current, self.provider,
                    identity[2] if identity is not None else 0, capture_token,
                    len(self.capture_watch.baseline) if self.capture_watch.baseline is not None else -1)
                self._capture_watch_status = current
            if ended:
                label = {
                    "sogou": "搜狗",
                    "wetype": "微信",
                    voice_program_manager.VOICE_PROGRAM_DOUBAO_IME: "豆包",
                }.get(self.provider, "语音程序")
                self.services.logger.info("Chromecast recording attempt=%s 原因=%s接收已结束(host_capture_ended)", self.attempt, label)
                self.reply("stop")
                self._stop_host()
                return
        if self.state != "starting" or self.ready_sent or not self.engaged:
            return
        services = self.services
        wetype_toggle = self.provider == "wetype" and self._toggle is not None
        decision_now = time.monotonic() if wetype_toggle else now
        if wetype_toggle and self._attempt_cancelled(self.attempt):
            # The queued host_stop still owns its reason and cleanup action.
            return
        if wetype_toggle and self.stopping.is_set():
            self._stop_host()
            return
        if wetype_toggle and self.input_lost.is_set():
            self._fail_voice(
                "keyboard safety tracking was lost; recording cancelled",
                command="stop",
            )
            self._stop_host()
            self.input_lost.clear()
            return
        if wetype_toggle and decision_now - self.start_at >= 9:
            confirmed = False
        elif self.provider in {
            "sogou",
            voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
        } or wetype_toggle:
            confirmed = True if self.capture_watch is not None and self.capture_watch.status == "tracking" else None
        else:
            with services.shortcut.runtime_lock:
                confirmed = services.shortcut.runtime_mic_confirmed
        if confirmed is True:
            if self._attempt_cancelled(self.attempt):
                self._stop_host()
                return
            self.confirmed = self.ready_sent = True
            if wetype_toggle:
                services.set_active(True)
                services.set_result(status.VOICE_RUNTIME_ACTIVE)
                services.logger.info(
                    "Chromecast toggle attempt=%s host_state=confirmed_capture ready=True",
                    self.attempt,
                )
            self.reply("ready")
            while self.pending_pcm and self.confirmed:
                self._output(self.pending_pcm.popleft())
            self.pending_samples = 0
        elif confirmed is False or decision_now - self.start_at >= 9:
            self.ready_sent = True
            label = {
                "sogou": "Sogou",
                "wetype": "WeType",
                voice_program_manager.VOICE_PROGRAM_DOUBAO_IME: "Doubao",
            }.get(self.provider, "Voice host")
            self._fail_voice(f"{label} microphone startup was not confirmed")
            self._stop_host()

    def _doubao_physicalizer_healthy(self):
        generation = self._doubao_physicalizer_generation
        if (
            self.provider != voice_program_manager.VOICE_PROGRAM_DOUBAO_IME
            or not self.engaged
            or self.closed
            or self.state == "stopping"
            or generation is None
        ):
            return True
        return self.services.shortcut.doubao_physicalizer.is_active_generation(generation)

    def _run(self):
        try:
            while not self.stopping.is_set():
                try:
                    event = self.events.get(timeout=.02)
                except queue.Empty:
                    event = None
                if event is not None:
                    self._handle(event)
                self._poll_host()
                if self.state == "stopping" and not self.closed:
                    self._stop_host()
        except Exception:
            self.services.logger.exception("Chromecast voice processing failed")
            self.reply("stop")
        finally:
            self.reply("stop")
            self._close_resources()

    def _close_resources(self):
        with self._cleanup_lock:
            session_closed = self._stop_host()
            # Clear confirmed-dead resources even when a shortcut still needs
            # release; never tie their ownership to an earlier session error.
            audio_closed = self._retire_playback()
            self.closed = session_closed and audio_closed
