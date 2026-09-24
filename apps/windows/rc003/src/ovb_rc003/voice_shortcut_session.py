"""Shared shortcut ownership, deferred releases and input-method confirmations.

Device gestures stay in their device policy. Application callbacks supply status,
configuration and the existing recovery policy; this owner retains one set of
keys, release timers and host generations under the original locks.
"""
from __future__ import annotations
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Tuple
from . import (hotkey, voice_controller, voice_program_manager, wetype_control_windows,
               win32_input, win32_keys, bridge_runtime_status, doubao_rpc)

_VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS = 0.1
_VOICE_HOTKEY_RELEASE_RETRY_MAX_SECONDS = 2.0
_VOICE_HOTKEY_BACKEND_MARKED = "marked_keybd_event"
_VOICE_HOTKEY_BACKEND_WETYPE = "wetype_hotkey"
_VOICE_HOTKEY_BACKEND_DOUBAO = "doubao_hotkey"
_INPUT_PROFILE_VOICE_HOTKEY_BACKENDS = frozenset(
    {_VOICE_HOTKEY_BACKEND_WETYPE, _VOICE_HOTKEY_BACKEND_DOUBAO}
)

@dataclass(frozen=True)
class ShortcutServices:
    configured_backend: Callable
    prepare_doubao: Callable
    resume_tracking: Callable
    active_doubao_attempt: Callable
    doubao_session: Callable
    cleanup_doubao: Callable
    set_result: Callable
    current_state: Callable
    request_cleanup: Callable
    prepare_mute_guard: Callable


class VoiceShortcutSession:
    def __init__(self, *, hotkey_text, logger, trace, services):
        self.services = services
        self.logger = logger
        self.trace = trace
        self.lock = threading.Lock()
        self.controller = voice_controller.VoiceController()
        self.hotkey = hotkey.HotkeySpec.parse(hotkey_text)
        self.pending_tokens: Optional[Tuple[str, ...]] = None
        self.active_backend: Optional[str] = None
        self.pending_backend: Optional[str] = None
        self.retry_timer: Optional[object] = None
        self.retry_token: Optional[object] = None
        self.retry_delay = (
            _VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS
        )
        self.retry_stopping = False
        self.timer_factory = threading.Timer
        self.ui_confirmation = "unknown"
        self.runtime_lock = threading.Lock()
        self.runtime_generation: Optional[int] = None
        self.runtime_audio_finished = False
        self.runtime_pcm_frames = 0
        self.runtime_cleanup_result: Optional[bool] = None
        self.runtime_backend: Optional[str] = None
        self.runtime_mic_confirmed: Optional[bool] = None
        self.wetype_control = wetype_control_windows.WeTypeVoiceControl(
            logger=self.logger,
            press_keys=win32_input.send_wetype_voice_key_combo_down,
            release_keys=win32_input.send_wetype_voice_key_combo_up,
            on_completion=self.on_cleanup,
            on_confirmation=self.on_confirmation,
            on_started=lambda generation: self.track_generation(generation, _VOICE_HOTKEY_BACKEND_WETYPE),
            prepare_playback_mute_guard=self.services.prepare_mute_guard,
        )
        self.doubao_control = wetype_control_windows.DoubaoVoiceControl(
            logger=self.logger,
        )
        self.doubao_physicalizer = doubao_rpc.DoubaoPhysicalizer()

    @staticmethod
    def provider_for_backend(backend: str) -> str:
        if backend == _VOICE_HOTKEY_BACKEND_DOUBAO:
            return voice_program_manager.VOICE_PROGRAM_DOUBAO_IME
        return voice_program_manager.VOICE_PROGRAM_WETYPE


    def control_for_backend(self, backend: str):
        if backend == _VOICE_HOTKEY_BACKEND_DOUBAO:
            return self.doubao_control
        return self.wetype_control


    def begin_runtime(
        self, backend: str = _VOICE_HOTKEY_BACKEND_WETYPE
    ) -> None:
        with self.runtime_lock:
            self.runtime_generation = None
            self.runtime_audio_finished = False
            self.runtime_pcm_frames = 0
            self.runtime_cleanup_result = None
            self.runtime_backend = backend
            self.runtime_mic_confirmed = None


    def control_generation(
        self, backend: str = _VOICE_HOTKEY_BACKEND_WETYPE
    ) -> Optional[int]:
        control = self.control_for_backend(backend)
        value = getattr(control, "current_generation", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None


    def control_flag(
        self, name: str, backend: str = _VOICE_HOTKEY_BACKEND_WETYPE
    ) -> bool:
        control = self.control_for_backend(backend)
        value = getattr(control, name, False)
        return value if isinstance(value, bool) else False


    def track_generation(
        self,
        generation: Optional[int],
        backend: str = _VOICE_HOTKEY_BACKEND_WETYPE,
    ) -> None:
        if generation is None:
            return
        with self.runtime_lock:
            self.runtime_generation = generation
            self.runtime_backend = backend


    def record_audio_result(self, frames: int) -> bool:
        with self.runtime_lock:
            if self.runtime_generation is None:
                return False
            self.runtime_audio_finished = True
            self.runtime_pcm_frames = max(0, int(frames))
            cleanup_result = self.runtime_cleanup_result
            backend = self.runtime_backend or _VOICE_HOTKEY_BACKEND_WETYPE
            mic_confirmed = self.runtime_mic_confirmed
        if backend == _VOICE_HOTKEY_BACKEND_WETYPE and mic_confirmed is not True:
            state = bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED
        elif cleanup_result is True:
            state = (
                bridge_runtime_status.VOICE_RUNTIME_SUCCESS
                if frames > 0
                else bridge_runtime_status.VOICE_RUNTIME_AUDIO_EMPTY
            )
        elif cleanup_result is False:
            state = bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED
        else:
            state = bridge_runtime_status.VOICE_RUNTIME_FINISHING
        self.services.set_result(
            state,
            provider=self.provider_for_backend(backend),
        )
        return True


    def on_confirmation(
        self,
        generation: int,
        success: bool,
    ) -> None:
        with self.runtime_lock:
            if (
                generation != self.runtime_generation
                or self.runtime_backend != _VOICE_HOTKEY_BACKEND_WETYPE
            ):
                self.logger.info(
                    "WeType voice confirmation ignored for stale generation=%s",
                    generation,
                )
                return
            self.runtime_mic_confirmed = bool(success)
            self.ui_confirmation = (
                "confirmed" if success else "not_confirmed"
            )
        if success:
            current_state = self.services.current_state()
            if current_state == bridge_runtime_status.VOICE_RUNTIME_ACTIVE:
                self.services.set_result(
                    bridge_runtime_status.VOICE_RUNTIME_MIC_CONFIRMED,
                    provider=voice_program_manager.VOICE_PROGRAM_WETYPE,
                )
            self.logger.info("WeType microphone open confirmed")
            return

        self.logger.warning(
            "WeType microphone open was not confirmed after recovery retries; "
            "preserving the active hold until the physical microphone key is released"
        )


    def on_cleanup(
        self,
        generation: int,
        success: bool,
        backend: Optional[str] = None,
    ) -> None:
        with self.runtime_lock:
            if generation != self.runtime_generation:
                self.logger.info(
                    "input profile runtime completion ignored for stale generation=%s",
                    generation,
                )
                return
            resolved_backend = (
                backend
                or self.runtime_backend
                or _VOICE_HOTKEY_BACKEND_WETYPE
            )
            if (
                self.runtime_backend is not None
                and resolved_backend != self.runtime_backend
            ):
                self.logger.info(
                    "input profile runtime completion ignored for stale backend=%s",
                    resolved_backend,
                )
                return
            self.runtime_cleanup_result = bool(success)
            audio_finished = self.runtime_audio_finished
            frames = self.runtime_pcm_frames
        provider = self.provider_for_backend(resolved_backend)
        if not success:
            tokens = tuple(self.hotkey.modifiers) + (self.hotkey.key,)
            with self.lock:
                self.pending_tokens = tokens
                self.pending_backend = resolved_backend
                self.active_backend = None
            self.services.set_result(
                bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED,
                provider=provider,
            )
            self.services.request_cleanup()
            return
        if not audio_finished:
            self.services.set_result(
                bridge_runtime_status.VOICE_RUNTIME_FINISHING,
                provider=provider,
            )
            return
        self.services.set_result(
            (
                bridge_runtime_status.VOICE_RUNTIME_SUCCESS
                if frames > 0
                else bridge_runtime_status.VOICE_RUNTIME_AUDIO_EMPTY
            ),
            provider=provider,
        )


    def apply(
        self,
        action: voice_controller.VoiceHostAction,
        *,
        backend_override: Optional[str] = None,
        tokens_override: Optional[Tuple[str, ...]] = None,
        cancelled=None,
    ) -> bool:
        backend = backend_override or self.services.configured_backend()
        tokens = tokens_override or (
            tuple(self.hotkey.modifiers) + (self.hotkey.key,)
        )
        if action == voice_controller.VoiceHostAction.KEY_UP and backend_override is None:
            backend = (
                self.active_backend
                or self.pending_backend
                or backend
            )
        trace_context = {
            **self.trace.current_context(),
            "action": str(action.value),
        }
        self.trace.emit(
            "voice_hotkey_requested",
            **trace_context,
            backend=str(backend),
            tokens=list(tokens),
            source="voice_controller",
        )
        if backend in _INPUT_PROFILE_VOICE_HOTKEY_BACKENDS:
            control = self.control_for_backend(backend)
            provider = self.provider_for_backend(backend)
            if action == voice_controller.VoiceHostAction.KEY_DOWN:
                if (
                    backend == _VOICE_HOTKEY_BACKEND_DOUBAO
                    and not self.services.prepare_doubao(tokens)
                ):
                    return False
                self.begin_runtime(backend)
            if backend == _VOICE_HOTKEY_BACKEND_DOUBAO:
                try:
                    expected_markers = len(
                        tuple(dict.fromkeys(win32_keys.resolve_vk_codes(tokens)))
                    )
                except win32_keys.UnknownKeyTokenError:
                    expected_markers = 0
                self.doubao_physicalizer.expect_markers(
                    "down"
                    if action == voice_controller.VoiceHostAction.KEY_DOWN
                    else "up",
                    expected_markers,
                )
            try:
                if action == voice_controller.VoiceHostAction.KEY_DOWN:
                    delivered = (
                        control.start(tokens)
                        if cancelled is None
                        else control.start(tokens, cancelled=cancelled)
                    )
                else:
                    delivered = control.stop()
            except (win32_input.Win32InputUnavailableError, OSError):
                self.logger.exception(
                    "%s voice shortcut control failed",
                    provider,
                )
                return False
            generation = self.control_generation(backend)
            self.trace.emit(
                "voice_hotkey_result",
                **trace_context,
                backend=str(backend),
                delivered=bool(delivered),
                generation=int(generation) if generation is not None else -1,
                cleanup_pending=bool(
                    self.control_flag("cleanup_pending", backend)
                ),
            )
            cleanup_pending = self.control_flag("cleanup_pending", backend)
            if delivered or cleanup_pending:
                self.track_generation(generation, backend)
            if not delivered:
                if action == voice_controller.VoiceHostAction.KEY_DOWN and cleanup_pending:
                    self.pending_tokens = tokens
                    self.pending_backend = backend
                    self.schedule_release_retry()
                self.logger.warning(
                    "%s voice shortcut did not confirm logical %s",
                    provider,
                    action.value,
                )
                return False
            if action == voice_controller.VoiceHostAction.KEY_DOWN:
                self.active_backend = backend
            else:
                self.active_backend = None
                if self.control_flag("completion_pending", backend):
                    self.services.set_result(
                        bridge_runtime_status.VOICE_RUNTIME_FINISHING,
                        provider=provider,
                    )
                elif generation is not None:
                    self.on_cleanup(
                        generation,
                        True,
                        backend,
                    )
            self.pending_tokens = None
            self.pending_backend = None
            if action == voice_controller.VoiceHostAction.KEY_UP:
                self.services.resume_tracking()
            return True
        provider_action = action
        try:
            if provider_action == voice_controller.VoiceHostAction.KEY_DOWN:
                self.pending_tokens = tokens
                self.pending_backend = backend
            self.send_action(provider_action, tokens, backend)
            if action == voice_controller.VoiceHostAction.KEY_DOWN:
                self.active_backend = backend
            if action == voice_controller.VoiceHostAction.KEY_UP:
                self.pending_tokens = None
                self.pending_backend = None
                self.active_backend = None
                self.services.resume_tracking()
            return True
        except win32_input.Win32InputUnavailableError:
            if provider_action == voice_controller.VoiceHostAction.KEY_DOWN:
                self.pending_tokens = None
                self.pending_backend = None
                self.active_backend = None
            self.logger.info("voice hotkey action skipped: no usable voice input backend")
            return False
        except win32_input.InputCleanupIncompleteError:
            self.pending_tokens = tokens
            self.pending_backend = backend
            self.schedule_release_retry()
            self.logger.exception(
                "voice hotkey action failed and safety key-up remains pending"
            )
            return False
        except OSError:
            if provider_action == voice_controller.VoiceHostAction.KEY_DOWN:
                self.pending_tokens = None
                self.pending_backend = None
                self.active_backend = None
            self.logger.exception("voice hotkey action failed to fully deliver")
            return False


    @staticmethod
    def send_action(
        action: voice_controller.VoiceHostAction,
        tokens: Tuple[str, ...],
        backend: str,
    ) -> None:
        if backend in _INPUT_PROFILE_VOICE_HOTKEY_BACKENDS:
            raise OSError("input profile shortcut is owned by its voice control")
        if action == voice_controller.VoiceHostAction.KEY_DOWN:
            win32_input.send_voice_key_combo_down(tokens)
        else:
            win32_input.send_voice_key_combo_up(tokens)


    def cancel_release_retry(
        self,
        *,
        reset_delay: bool = True,
    ) -> None:
        timer = self.retry_timer
        self.retry_timer = None
        self.retry_token = None
        if reset_delay:
            self.retry_delay = (
                _VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS
            )
        if timer is not None:
            cancel = getattr(timer, "cancel", None)
            if callable(cancel):
                cancel()


    def schedule_release_retry(self) -> None:
        if (
            self.retry_stopping
            or self.pending_tokens is None
            or self.retry_timer is not None
        ):
            return
        token = object()
        try:
            timer = self.timer_factory(
                self.retry_delay,
                lambda: self._retry_release(token),
            )
            timer.daemon = True
            self.retry_token = token
            self.retry_timer = timer
            timer.start()
        except Exception:
            if self.retry_token is token:
                self.retry_token = None
                self.retry_timer = None
            self.logger.exception("voice hotkey safety-release timer failed")


    def _retry_release(self, token: object) -> None:
        with self.lock:
            if self.retry_token is not token:
                return
            self.retry_token = None
            self.retry_timer = None
            if (
                self.retry_stopping
                or self.pending_tokens is None
            ):
                return
            self.retry_delay = min(
                _VOICE_HOTKEY_RELEASE_RETRY_MAX_SECONDS,
                max(
                    _VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS,
                    self.retry_delay * 2.0,
                ),
            )
            attempt = self.services.active_doubao_attempt()
            if (
                attempt is not None
                and attempt.has_cleanup_debt("hotkey")
                and self.pending_backend
                == _VOICE_HOTKEY_BACKEND_DOUBAO
            ):
                if not self.services.doubao_session().request_cleanup(
                    attempt,
                    lambda owned: self.services.cleanup_doubao(
                        owned,
                        reason="safety release retry",
                    ),
                ):
                    self.schedule_release_retry()
                return
            self.release_pending()


    def release_pending(self) -> bool:
        tokens = self.pending_tokens
        if tokens is None:
            return True
        backend = (
            self.pending_backend
            or self.active_backend
            or self.services.configured_backend()
        )
        try:
            if backend in _INPUT_PROFILE_VOICE_HOTKEY_BACKENDS:
                control = self.control_for_backend(backend)
                provider = self.provider_for_backend(backend)
                delivered = control.stop()
                if not delivered:
                    self.schedule_release_retry()
                    return False
                generation = self.control_generation(backend)
                if self.control_flag("completion_pending", backend):
                    self.services.set_result(
                        bridge_runtime_status.VOICE_RUNTIME_FINISHING,
                        provider=provider,
                    )
                elif generation is not None:
                    self.on_cleanup(
                        generation,
                        True,
                        backend,
                    )
            else:
                self.send_action(
                    voice_controller.VoiceHostAction.KEY_UP,
                    tokens,
                    backend,
                )
        except (win32_input.Win32InputUnavailableError, OSError):
            self.logger.exception("voice hotkey safety release failed")
            self.schedule_release_retry()
            return False
        self.pending_tokens = None
        self.pending_backend = None
        self.active_backend = None
        self.cancel_release_retry()
        attempt = self.services.doubao_session().current
        if (
            backend == _VOICE_HOTKEY_BACKEND_DOUBAO
            and attempt is not None
            and attempt.has_cleanup_debt("hotkey")
        ):
            attempt.resolve_cleanup_debt("hotkey")
        self.logger.info("voice hotkey safety release completed")
        self.services.resume_tracking()
        return True
