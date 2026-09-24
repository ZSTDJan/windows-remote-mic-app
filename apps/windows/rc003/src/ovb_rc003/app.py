"""Entry point wiring: connects the pieces into a running RC003 client.

Windows-only end-to-end (BLE via winrt, HID via Raw Input, key injection via
SendInput). NOT exercised against real hardware in this candidate - no
device pairing/control happens anywhere in this repository or its tests, per
the project's hard boundary. See this package's top-level README.md "Known
gaps" section for what remains 待核验 (to be verified) on a real Windows
machine with a paired RC003.

Reconnect/cleanup contract (fixed after XRBM-014 review RETRY P1 #2 - see
XRBM-014's independent review): ``RC003App`` no longer connects once
and waits forever. ``connection_supervisor.ConnectionSupervisor`` drives a
connect/wait/cleanup/retry loop; a BLE disconnect notification or a protocol
error both call ``request_reconnect()``, which ends the current wait and
guarantees ``_cleanup_once()`` runs before the next connect attempt.
``_cleanup_once()`` releases the voice hotkey and closes the BLE/audio attempt.
Raw Input, the verified HID tap, and the marked voice-key
physicalizer instead belong to the bridge-worker lifetime: BLE discovery or
reconnect failure cannot turn ordinary button handling off. Final worker exit
stops those three input owners once, after BLE/voice cleanup has run.

Voice fail-closed ordering (P1 #3): the output endpoint is resolved and
opened BEFORE any hotkey/MIC_OPEN is sent, not lazily after the device has
already started streaming. If the endpoint is missing or fails to open,
neither the hotkey nor MIC_OPEN are sent at all - voice fails fully closed
while ordinary buttons keep working.

Further fail-closed ordering (XRBM-018, fixing XRBM-014 review round 2 P1
#6): the host hotkey is now sent BEFORE MIC_OPEN, and if it fails to fully
deliver, MIC_OPEN is never sent at all - a device streaming into Windows
without ever having actually tapped/held the configured hotkey is exactly
the "voice opened after host-trigger failure" defect the round-2 review
found. A playback write failure now also fails closed (closes and discards
the sink during reconnect cleanup) and requests a reconnect, instead of
logging indefinitely while the device keeps streaming into nothing. Blocking
writes run on a bounded FIFO worker, so ordinary BLE control handling does not
wait for every PortAudio write.

Cleanup ownership (XRBM-019 P1 #2, fixing XRBM-018 round 2 finding #2):
an owner reference is cleared only after that resource confirms it stopped.
BLE/audio cleanup failures end the reconnect supervisor rather than starting
a new session over a live owner. Final input shutdown follows the same rule
for Raw Input, HID tap, and the voice-key physicalizer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import List, Optional, Tuple

from . import __version__
from . import (
    audio_output,
    audio_playback,
    audio_playback_worker,
    voice_audio_session,
    voice_shortcut_session,
    action_executor,
    ble_transport_winrt,
    bridge_launcher,
    bridge_runtime_status,
    bridge_tray_windows,
    button_combo,
    button_gesture,
    chromecast_host_activity,
    chromecast_runtime,
    config,
    connection_supervisor,
    diagnostic_trace,
    doubao_rpc,
    element_navigation_control_windows,
    element_navigation_runtime,
    frida_compat,
    hid_identity,
    hotkey,
    key_detection_bridge,
    key_mapping,
    logging_setup,
    raw_input_windows,
    rc003_doubao_session,
    remote_selection,
    voice_key_physicalizer_windows,
    voice_controller,
    voice_interaction_diagnostics_windows,
    voice_playback_session_windows,
    voice_program_manager,
    wetype_control_windows,
    win32_input,
    win32_keys,
)
from .atvv_session import AudioStarted, AudioStopped, CapsReceived, MicButtonPressed, PcmStats


from .voice_shortcut_session import (
    _VOICE_HOTKEY_RELEASE_RETRY_INITIAL_SECONDS,
    _VOICE_HOTKEY_RELEASE_RETRY_MAX_SECONDS,
    _VOICE_HOTKEY_BACKEND_MARKED,
    _VOICE_HOTKEY_BACKEND_WETYPE,
    _VOICE_HOTKEY_BACKEND_DOUBAO,
    _INPUT_PROFILE_VOICE_HOTKEY_BACKENDS
)


class CleanupIncompleteError(RuntimeError):
    """Raised when an owned BLE/audio or process-input resource stays live."""


_BUTTON_ACTION_KEY_TOKENS = {
    key_mapping.ActionKind.ESCAPE: ("escape",),
    key_mapping.ActionKind.RETURN: ("enter",),
    key_mapping.ActionKind.ARROW_UP: ("up",),
    key_mapping.ActionKind.ARROW_DOWN: ("down",),
    key_mapping.ActionKind.ARROW_LEFT: ("left",),
    key_mapping.ActionKind.ARROW_RIGHT: ("right",),
    key_mapping.ActionKind.DELETE_BACKWARD: ("backspace",),
    key_mapping.ActionKind.SHOW_DESKTOP: ("win", "m"),
    key_mapping.ActionKind.CONTEXT_MENU: ("apps",),
    key_mapping.ActionKind.APP_SWITCHER: ("ctrl", "alt", "tab"),
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: ("volume_up",),
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: ("volume_down",),
    key_mapping.ActionKind.SYSTEM_VOLUME_MUTE: ("volume_mute",),
    key_mapping.ActionKind.PLAY_PAUSE: ("media_play_pause",),
}

_BUTTON_ACTION_MOUSE_BUTTONS = {
    key_mapping.ActionKind.MOUSE_LEFT_CLICK: "left",
    key_mapping.ActionKind.MOUSE_RIGHT_CLICK: "right",
    key_mapping.ActionKind.MOUSE_MIDDLE_CLICK: "middle",
    key_mapping.ActionKind.MOUSE_X1_CLICK: "x1",
    key_mapping.ActionKind.MOUSE_X2_CLICK: "x2",
}

# Semantic tap mappings only. RC003 physical-edge correlation is separate.
_BUTTON_ACTION_NAVIGATION_VKS = {
    key_mapping.ActionKind.ARROW_UP: 0x26,
    key_mapping.ActionKind.ARROW_DOWN: 0x28,
    key_mapping.ActionKind.ARROW_LEFT: 0x25,
    key_mapping.ActionKind.ARROW_RIGHT: 0x27,
    key_mapping.ActionKind.RETURN: 0x0D,
    key_mapping.ActionKind.ESCAPE: 0x1B,
    key_mapping.ActionKind.CONTEXT_MENU: 0x5D,
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: 0xAF,
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: 0xAE,
}

# The remote has no dedicated parent/child actions. A user-mapped, unmodified
# PageUp/PageDown tap keeps those navigation steps available without claiming
# the computer keyboard or changing multi-key shortcut semantics.
_BUTTON_NAVIGATION_SINGLE_KEYS = {
    ("pageup",): 0x21,
    ("page_up",): 0x21,
    ("pagedown",): 0x22,
    ("page_down",): 0x22,
}

_RAW_FALLBACK_KEY_TOKENS = {
    "mic": "f5",
    "right": "right",
    "left": "left",
    "down": "down",
    "up": "up",
    "ok": "enter",
    "home": "home",
    "menu": "apps",
    "tv": "backtick",
    "power": "vk_5f",
    "volume_mute": "volume_mute",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "back": "browser_back",
}

_KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS = 1.0
_KEY_DETECTION_MIC_MAX_SECONDS = 10.0
_ORDINARY_MIC_RELEASE_GUARD_SECONDS = 0.12
_ORDINARY_MIC_SOURCE_STALE_SECONDS = 2.0
_ORDINARY_MIC_MAX_SECONDS = 10.0
_KEY_DETECTION_SUPPRESSION_MAX_SECONDS = 2.0
_RUNTIME_STATUS_HEARTBEAT_SECONDS = 5.0
_VOICE_HOLD_SAFETY_SECONDS = 120.0
_VOICE_KEY_PHYSICALIZER_RETRY_SECONDS = 1.0
_VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS = 10.0
_RAW_INPUT_RETRY_INITIAL_SECONDS = 1.0
_RAW_INPUT_RETRY_MAX_SECONDS = 30.0
_BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS = 0.1
_BUTTON_INPUT_RELEASE_RETRY_MAX_SECONDS = 2.0
_DOUBAO_HOST_READY_TIMEOUT_SECONDS = 3.0
_DOUBAO_HOST_READY_POLL_SECONDS = 0.05
_DOUBAO_PROCESS_READY_TIMEOUT_SECONDS = 2.0
_DOUBAO_ATTEMPT_JOIN_TIMEOUT_SECONDS = 3.5
_DIRECTION_BUTTON_IDS = frozenset({"up", "down", "left", "right"})


def _runtime_voice_hotkey_text(config_data: dict) -> str:
    configured = str(config_data.get("voice_hotkey", "")).strip()
    return configured or key_mapping.voice_hotkey_for_trigger_mode(
        key_mapping.VoiceTriggerMode.HOLD
    )


def open_configured_application(action: key_mapping.ButtonAction) -> bool:
    """Application-action seam kept at the app boundary for testability."""

    return action_executor.open_configured_application(action)


class RC003App:
    def __init__(self, *, launch_voice_program_on_start: bool = True) -> None:
        self._config_root = config.config_root()
        self._config_path = config.config_path(self._config_root)
        self._config = config.load_config(self._config_path)
        from . import remote_selection
        self._selected_remote_key = remote_selection.active_key(self._config)
        selected_profile = remote_selection.active_profile(self._config)
        self._remote_profile = selected_profile
        if selected_profile and not remote_selection.runtime_ready(selected_profile):
            raise remote_selection.SelectionError("当前型号尚未提供接收，请在设备页重新选择。")
        self._config_mtime_ns = self._settings_file_mtime_ns(self._config_path)
        self._bindings_path = config.key_bindings_path(self._config_root)
        self._bindings = config.load_key_bindings(
            self._bindings_path
        )
        self._removed_voice_bindings = config.normalize_voice_product_boundary(
            self._config,
            self._bindings,
        )
        self._bindings_mtime_ns = self._settings_file_mtime_ns(self._bindings_path)
        self._button_mapping_lock = threading.RLock()
        self._button_gestures = button_gesture.ButtonGestureDispatcher(
            is_action_configured=self._is_button_action_configured,
            is_repeatable=self._is_button_repeatable,
            on_trigger=self._on_button_trigger,
            repeat_interval_for=self._button_repeat_interval,
            on_idle=self._apply_pending_settings_if_idle,
            on_diagnostic=self._on_button_gesture_diagnostic,
        )
        self._button_combos = button_combo.ButtonComboRecognizer()
        self._logger: logging.Logger = logging_setup.get_logger(self._config_root)
        self._diagnostic_trace = diagnostic_trace.DiagnosticTrace(
            self._config_root,
            enabled=bool(self._config.get("diagnostic_trace_enabled", False)),
        )
        win32_input.set_diagnostic_trace(self._diagnostic_trace)
        voice_key_physicalizer_windows.set_diagnostic_trace(self._diagnostic_trace)
        wetype_control_windows.set_diagnostic_trace(self._diagnostic_trace)
        doubao_rpc.set_diagnostic_trace(self._diagnostic_trace)
        element_navigation_runtime.set_diagnostic_trace(self._diagnostic_trace)
        self._record_device_diagnostic_context()
        self._voice_attempt_id: Optional[str] = None
        self._runtime_identity = bridge_runtime_status.current_runtime_identity(
            __version__
        )
        self._runtime_status_lock = threading.Lock()
        self._runtime_connection_state = (
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        self._runtime_raw_input_state = "starting"
        self._runtime_hid_tap_state = "starting"
        self._runtime_voice_key_physicalizer_state = "starting"
        self._runtime_last_button_at: Optional[float] = None
        self._runtime_last_button_source = ""
        self._runtime_voice_active = False
        self._runtime_voice_state = bridge_runtime_status.VOICE_RUNTIME_NOT_TESTED
        self._runtime_voice_provider = ""
        self._runtime_voice_updated_at: Optional[float] = None
        self._runtime_battery_level: Optional[int] = None
        self._runtime_last_button_publish_monotonic = 0.0
        self._logger.info(
            "startup: app identity: version=%s runtime=%s package=%s",
            self._runtime_identity.app_version,
            self._runtime_identity.runtime_kind,
            self._runtime_identity.package_name,
        )
        if launch_voice_program_on_start and (
            selected_profile != remote_selection.CHROMECAST_PROFILE
            or self._config.get("voice_program", {}).get("provider")
            == voice_program_manager.VOICE_PROGRAM_SOGOU
        ):
            try:
                voice_program_result = (
                    voice_program_manager.launch_configured_at_bridge_start(self._config)
                )
            except Exception:
                self._logger.exception(
                    "voice program: optional bridge-start launch failed unexpectedly"
                )
            else:
                if voice_program_result.code not in {"not_requested", "disabled"}:
                    self._logger.info(
                        "voice program: provider=%s launch_result=%s",
                        voice_program_result.provider_id,
                        voice_program_result.code,
                    )
        else:
            self._logger.info(
                "voice program: startup launch skipped during diagnostics recovery"
            )
        if self._removed_voice_bindings:
            self._logger.warning(
                "legacy voice mappings disabled until user reselects actions: %s",
                sorted(self._removed_voice_bindings),
            )
        self._voice_shortcut = voice_shortcut_session.VoiceShortcutSession(
            hotkey_text=_runtime_voice_hotkey_text(self._config),
            logger=self._logger, trace=self._diagnostic_trace,
            services=voice_shortcut_session.ShortcutServices(
                configured_backend=lambda: self._configured_voice_hotkey_backend(),
                prepare_doubao=lambda tokens: self._prepare_doubao_voice_physicalizer(tokens),
                resume_tracking=lambda: self._resume_degraded_voice_key_physicalizer_recovery(),
                active_doubao_attempt=lambda: self._active_doubao_attempt_locked(),
                doubao_session=lambda: self._doubao_session,
                cleanup_doubao=lambda attempt, **kw: self._run_active_doubao_cleanup(attempt, **kw),
                set_result=lambda *args, **kw: self._set_runtime_voice_result(*args, **kw),
                current_state=self._current_voice_runtime_state,
                request_cleanup=lambda: self._supervisor.request_reconnect(),
                prepare_mute_guard=self._prepare_wetype_playback_mute_guard,
            ),
        )
        self._pending_voice_settings = None
        self._pending_config = None
        self._pending_bindings = None
        self._voice_audio_start_fallback_pending = False
        self._voice_focus_before: Optional[
            voice_interaction_diagnostics_windows.FocusSnapshot
        ] = None
        self._voice_focus_provider = ""
        self._voice_focus_submit_method = ""
        self._voice_text_observation = "unknown"
        self._doubao_session = rc003_doubao_session.DoubaoSessionCoordinator()
        self._doubao_capture_watch_factory = lambda target_pid: (
            chromecast_host_activity.CaptureWatch(
                reader=lambda: chromecast_host_activity.read_doubao_capture_for_pid(
                    target_pid
                ),
                fast_start=True,
            )
        )
        self._voice_pcm_min_arrival_sequence: Optional[int] = None
        self._button_action_lock = threading.RLock()
        self._button_key_release_pending: Optional[Tuple[str, ...]] = None
        self._button_mouse_release_pending: Optional[str] = None
        self._button_input_release_retry_timer: Optional[object] = None
        self._button_input_release_retry_token: Optional[object] = None
        self._button_input_release_retry_delay = (
            _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS
        )
        self._button_input_release_retry_stopping = False
        self._button_input_release_timer_factory = threading.Timer
        # Raw Input and the ATVV control channel arrive on different worker
        # threads. Serialize the voice state machine so one physical press
        # cannot race into two host shortcut deliveries.
        self._logger.info(
            "startup: voice settings active: trigger_mode=%s hotkey=%s",
            self._voice_shortcut.controller.trigger_mode.value,
            self._voice_shortcut.hotkey.serialize(),
        )
        # One RC003 microphone press is reported independently by HID, the
        # ATVV mic opcode, and sometimes AUDIO_STARTED first. Keep all reports
        # in one gesture so one real press produces one host down and release.
        self._voice_mic_gesture_active = False
        self._voice_mic_gesture_audio_started = False
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = False
        self._voice_mic_gesture_hid_released = False
        self._voice_mic_gesture_direct_hid_seen = False
        self._voice_mic_gesture_sources_down: set[str] = set()
        self._voice_hold_watchdog_timer: Optional[object] = None
        self._voice_hold_watchdog_token: Optional[object] = None
        self._voice_hold_watchdog_timer_factory = threading.Timer
        self._ordinary_mic_lock = threading.Lock()
        self._ordinary_mic_sources_down: set[str] = set()
        self._ordinary_mic_late_sources_down: set[str] = set()
        self._ordinary_mic_late_source_deadlines: dict[str, float] = {}
        self._ordinary_mic_sources_seen: set[str] = set()
        self._ordinary_mic_gesture_started_at = 0.0
        self._ordinary_mic_release_guard_until = 0.0
        self._ordinary_mic_gesture_active = False
        self._unsolicited_mic_close_pending = False
        self._voice_audio_stream_active = False
        self._voice_audio_stop_processed = False
        self._voice_pcm_forwarding_enabled = False
        self._voice_raw_input_trigger_pending = False
        self._ble_session: Optional[ble_transport_winrt.RC003BleSession] = None
        self._hid_listener: Optional[raw_input_windows.RawInputButtonListener] = None
        self._raw_input_lifecycle_lock = threading.RLock()
        self._raw_input_operation_lock = threading.RLock()
        self._raw_input_generation = 0
        self._raw_input_lost_generation = -1
        self._raw_input_stopping = False
        self._raw_input_retry_timer: Optional[object] = None
        self._raw_input_retry_token: Optional[object] = None
        self._raw_input_retry_delay = _RAW_INPUT_RETRY_INITIAL_SECONDS
        self._raw_input_timer_factory = threading.Timer
        self._raw_windows_key_down_query = (
            raw_input_windows.physical_key_is_down_before_injection
        )
        self._voice_key_physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ] = None
        self._voice_key_physicalizer_ready = False
        self._voice_key_physicalizer_lifecycle_lock = threading.RLock()
        self._voice_key_physicalizer_operation_lock = threading.Lock()
        self._voice_key_physicalizer_generation = 0
        self._voice_key_physicalizer_lost_generation = -1
        self._voice_key_physicalizer_stopping = False
        self._voice_key_physicalizer_retry_timer: Optional[object] = None
        self._voice_key_physicalizer_retry_token: Optional[object] = None
        self._voice_key_physicalizer_degraded_retry_deadline = 0.0
        self._voice_key_physicalizer_timer_factory = threading.Timer
        self._hid_report_tap: Optional[frida_compat.RC003HidReportTap] = None
        self._direct_hid_usages: set[int] = set()
        self._direct_hid_report_seq = 0
        self._direct_hid_lock = threading.Lock()
        self._input_arbitration_lock = threading.RLock()
        # True only after the injected endpoint confirms it copied and cleared
        # a real RC003 report before Windows could translate that report.
        self._direct_hid_interception_ready = False
        # The helper reports ATTACHED_WAITING_IO after the hook and lease are
        # armed but before the first intercepted report proves end-to-end I/O.
        # Suspend Raw Input mapping during that short handover so one physical
        # hold cannot be split between two owners.
        self._direct_hid_interception_armed = False
        self._direct_hid_handover_waiting_for_neutral = False
        self._raw_fallback_buttons_down: set[str] = set()
        self._raw_fallback_physical_buttons_down: dict[
            str, tuple[str, Optional[str]]
        ] = {}
        self._raw_fallback_release_debts: dict[
            str, tuple[set[str], set[str]]
        ] = {}
        self._raw_fallback_tracking_active = False
        self._raw_fallback_hold_guards: dict[str, tuple[object, object]] = {}
        self._raw_fallback_timer_factory = threading.Timer
        self._input_rearm_blocked_buttons: set[str] = set()
        self._key_detection_suppressed_buttons: set[str] = set()
        self._key_detection_suppression_deadlines: dict[str, float] = {}
        self._key_detection_mic_lock = threading.Lock()
        self._key_detection_mic_gesture_active = False
        self._key_detection_mic_gesture_started_at = 0.0
        self._key_detection_mic_release_deadline: Optional[float] = None
        self._key_detection_mic_audio_started = False
        self._key_detection_mic_sources_down: set[str] = set()
        self._voice_audio = voice_audio_session.VoiceAudioSession(
            config=lambda: self._config,
            logger=self._logger,
            request_cleanup=lambda: self._supervisor.request_reconnect(),
            disable_forwarding=lambda: setattr(self, "_voice_pcm_forwarding_enabled", False),
            on_first_frame=self._on_voice_audio_first_frame,
        )
        self._event_loop = asyncio.get_event_loop()
        # Physical input belongs to the bridge-worker lifetime, not to one BLE
        # connection attempt. BLE callbacks have their own generation gate.
        self._accept_input_events = True
        self._accept_ble_events = False
        self._ble_callback_generation = 0

        self._supervisor = connection_supervisor.ConnectionSupervisor(
            connect=self._connect_once,
            cleanup=self._cleanup_once,
            retry_delay=float(self._config.get("retry_delay", 2.0)),
            max_retry_delay=float(self._config.get("max_retry_delay", 60.0)),
            logger=self._logger,
            loop=self._event_loop,
        )

        # Construct shared-service bindings after their state is initialized.
        self._chromecast_runtime = chromecast_runtime.ChromecastRuntime(
            chromecast_runtime.RuntimeServices(
                selected_key=lambda: self._selected_remote_key,
                logger=self._logger,
                create_voice_host=self._create_chromecast_voice_host,
                set_input_state=lambda **kw: self._set_runtime_input_state(**kw),
                publish_status=lambda state: self._publish_runtime_status(state),
                start_input=lambda: self._start_input_channels(),
                stop_input=lambda: self._stop_input_channels(),
                disable_input=lambda: setattr(self, "_accept_input_events", False),
                cancel_mappings=self._cancel_chromecast_mappings,
                release_inputs=self._release_chromecast_inputs,
                route_edge=self._route_chromecast_edge,
            )
        )

    def _create_chromecast_voice_host(self):
        from .chromecast_voice_host import VoiceHost, VoiceHostServices
        return VoiceHost(VoiceHostServices(
            audio=self._voice_audio,
            shortcut=self._voice_shortcut,
            settings=lambda: self._config,
            config_root=self._config_root,
            logger=self._logger,
            configured_backend=lambda: self._configured_voice_hotkey_backend(),
            voice_mapping_enabled=lambda: self._voice_mode_for_primary_button(
                "mic", self._primary_button_action("mic")) is not None,
            ensure_key_tracking=lambda *args: self._ensure_voice_key_physicalizer_for_hotkey(*args),
            reload_settings=lambda: self._reload_settings_if_changed(),
            apply_pending_settings=lambda: self._apply_pending_voice_settings_if_idle_locked(),
            begin_diagnostic=lambda *args, **kw: self._ensure_voice_diagnostic_attempt(*args, **kw),
            finish_diagnostic=lambda *args, **kw: self._finish_voice_diagnostic_attempt(*args, **kw),
            set_active=lambda active: self._set_runtime_voice_active(active),
            set_result=lambda *args, **kw: self._set_runtime_voice_result(*args, **kw),
            wait_sogou=lambda: self._wait_for_sogou_voice_process(),
            disable_forwarding=lambda: setattr(self, "_voice_pcm_forwarding_enabled", False),
        ))

    def _current_voice_runtime_state(self):
        with self._runtime_status_lock:
            return self._runtime_voice_state

    def _on_voice_audio_first_frame(self):
        with self._runtime_status_lock:
            voice_active = self._runtime_voice_active
        if voice_active:
            self._set_runtime_voice_result(bridge_runtime_status.VOICE_RUNTIME_RECEIVING_AUDIO)

    def _cancel_chromecast_mappings(self):
        with self._input_arbitration_lock:
            self._accept_input_events = False
            self._button_combos.reset()
            self._button_gestures.reset()

    def _release_chromecast_inputs(self):
        with self._button_action_lock:
            self._button_input_release_retry_stopping = True
            self._cancel_button_input_release_retry_locked(reset_delay=False)
            return self._release_pending_button_inputs()

    def _route_chromecast_edge(self, edge):
        if edge.action == "cancel":
            with self._input_arbitration_lock:
                self._cancel_input_gestures_for_buttons({edge.button}, reason="chromecast_cancel",
                                                       block_until_release=False)
        else:
            self._on_button_event(edge.button, edge.action == "down", event_source="chromecast")

    # -- lifecycle: driven by ConnectionSupervisor -------------------------

    async def run_forever(self) -> None:
        heartbeat = self._event_loop.create_task(
            self._runtime_status_heartbeat()
        )
        try:
            if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
                await self._chromecast_runtime.run()
                return
            self._start_input_channels()
            await self._supervisor.run_forever()
        finally:
            try:
                if self._remote_profile != remote_selection.CHROMECAST_PROFILE:
                    self._stop_input_channels()
            finally:
                element_navigation_runtime.clear_diagnostic_trace(self._diagnostic_trace)
                self._diagnostic_trace.close()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def stop(self) -> None:
        if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
            await self._chromecast_runtime.stop()
            return
        await self._supervisor.stop()

    def request_connection_retry_now(self) -> None:
        if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
            return  # No unsolicited, repeated UAC prompts or cross-device retry.
        self._logger.info("manual reconnect requested; waking retry backoff")
        self._supervisor.request_retry_now()

    def _press_wetype_voice_keys(self, tokens):
        win32_input.send_wetype_voice_key_combo_down(tokens)

    def _release_wetype_voice_keys(self, tokens):
        win32_input.send_wetype_voice_key_combo_up(tokens)




    async def _runtime_status_heartbeat(self) -> None:
        while True:
            self._publish_runtime_status()
            await asyncio.sleep(_RUNTIME_STATUS_HEARTBEAT_SECONDS)

    def _publish_runtime_status(
        self,
        state: Optional[bridge_runtime_status.BridgeConnectionState] = None,
    ) -> None:
        with self._runtime_status_lock:
            if state is not None:
                self._runtime_connection_state = state
            if (
                self._runtime_connection_state
                is not bridge_runtime_status.BridgeConnectionState.CONNECTED
            ):
                self._runtime_battery_level = None
            try:
                bridge_runtime_status.publish_status(
                    self._config_root,
                    self._runtime_connection_state,
                    identity=self._runtime_identity,
                    raw_input_state=self._runtime_raw_input_state,
                    hid_tap_state=self._runtime_hid_tap_state,
                    voice_key_physicalizer_state=(
                        self._runtime_voice_key_physicalizer_state
                    ),
                    last_button_at=self._runtime_last_button_at,
                    last_button_source=self._runtime_last_button_source,
                    voice_active=self._runtime_voice_active,
                    voice_runtime_state=self._runtime_voice_state,
                    voice_runtime_provider=self._runtime_voice_provider,
                    voice_runtime_updated_at=self._runtime_voice_updated_at,
                    battery_level=self._runtime_battery_level,
                )
            except (OSError, ValueError):
                self._logger.exception(
                    "bridge runtime status update failed: state=%s",
                    self._runtime_connection_state.value,
                )

    def _set_runtime_input_state(
        self,
        *,
        raw_input_state: Optional[str] = None,
        hid_tap_state: Optional[str] = None,
        voice_key_physicalizer_state: Optional[str] = None,
    ) -> None:
        with self._runtime_status_lock:
            if raw_input_state is not None:
                self._runtime_raw_input_state = str(raw_input_state)
            if hid_tap_state is not None:
                self._runtime_hid_tap_state = str(hid_tap_state)
            if voice_key_physicalizer_state is not None:
                self._runtime_voice_key_physicalizer_state = str(
                    voice_key_physicalizer_state
                )
        self._publish_runtime_status()

    def _set_runtime_voice_active(self, active: bool) -> None:
        active = bool(active)
        with self._runtime_status_lock:
            if active == self._runtime_voice_active:
                return
            self._runtime_voice_active = active
        self._publish_runtime_status()

    def _set_runtime_battery_level(self, level: Optional[int]) -> None:
        resolved = None if level is None else int(level)
        if resolved is not None and not 0 <= resolved <= 100:
            return
        with self._runtime_status_lock:
            if (
                self._runtime_connection_state
                is not bridge_runtime_status.BridgeConnectionState.CONNECTED
            ):
                return
            if resolved == self._runtime_battery_level:
                return
            self._runtime_battery_level = resolved
        self._publish_runtime_status()

    def _set_runtime_voice_result(
        self,
        state: str,
        *,
        provider: Optional[str] = None,
    ) -> None:
        resolved_provider = provider
        if resolved_provider is None:
            resolved_provider = str(
                voice_program_manager.normalize_voice_program_settings(
                    self._config.get("voice_program")
                )["provider"]
            )
        with self._runtime_status_lock:
            previous_state = getattr(self, '_runtime_voice_state', 'not_tested')
            previous_provider = getattr(self, '_runtime_voice_provider', '')
            self._runtime_voice_state = str(state)
            self._runtime_voice_provider = str(resolved_provider)
            self._runtime_voice_updated_at = time.time()
        self._publish_runtime_status()
        trace = getattr(self, '_diagnostic_trace', None)
        if ((previous_state, previous_provider) != (str(state), str(resolved_provider))
                and state in {bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED,
                              bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED,
                              bridge_runtime_status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED}):
            self._logger.warning("Voice runtime failed: state=%s provider=%s", state, resolved_provider,
                                 extra={"failure_key": f"voice:{resolved_provider}:{state}"})
        if trace is not None and (previous_state, previous_provider) != (str(state), str(resolved_provider)):
            try:
                trace.emit('voice_runtime_state', state=str(state), previous_state=previous_state,
                           provider=str(resolved_provider), **trace.current_context())
            except Exception:
                pass  # Diagnostic failures must never change voice state or cleanup.



    def _prepare_wetype_playback_mute_guard(self):
        sink = self._voice_audio.sink
        if sink is None or not getattr(sink, "ready", False):
            return None
        name = getattr(sink, "endpoint_name", "")
        if not isinstance(name, str) or not name:
            return None
        return voice_playback_session_windows.prepare_playback_mute_guard(name)








    def _record_runtime_button(self, event_source: str) -> None:
        now_wall = time.time()
        now_monotonic = time.monotonic()
        with self._runtime_status_lock:
            self._runtime_last_button_at = now_wall
            self._runtime_last_button_source = str(event_source)
            if now_monotonic - self._runtime_last_button_publish_monotonic < 0.5:
                return
            self._runtime_last_button_publish_monotonic = now_monotonic
        self._publish_runtime_status()

    def clear_runtime_status(self) -> None:
        try:
            bridge_runtime_status.clear_status(
                self._config_root,
                pid=os.getpid(),
            )
        except OSError:
            self._logger.exception("bridge runtime status cleanup failed")

    async def _connect_once(self) -> None:
        with self._voice_shortcut.lock:
            self._ble_callback_generation += 1
            callback_generation = self._ble_callback_generation
            self._accept_ble_events = True
        with self._runtime_status_lock:
            self._runtime_battery_level = None
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.CONNECTING
        )
        self._set_runtime_voice_active(False)
        stage = "discover_candidates"
        try:
            self._logger.info("startup: resolving RC003 identity")
            candidates = await ble_transport_winrt.discover_candidates(with_device_keys=True)
            # The selection is fixed for this service lifetime. Reconnection
            # must never probe or select a different physical remote.
            stage = "select_connectable_candidate"
            candidate = await ble_transport_winrt.select_connectable_candidate(
                candidates, selected_key=self._selected_remote_key
            )
            self._logger.info("startup: exactly one RC003 candidate resolved")

            stage = "create_ble_session"
            self._ble_session = ble_transport_winrt.RC003BleSession(
                on_pcm_frame=lambda samples: self._on_pcm_frame(
                    samples,
                    _ble_generation=callback_generation,
                ),
                on_pcm_frame_with_sequence=lambda samples, sequence: self._on_pcm_frame(
                    samples,
                    _ble_generation=callback_generation,
                    _arrival_sequence=sequence,
                ),
                on_control_event=lambda event: self._on_control_event(
                    event,
                    _ble_generation=callback_generation,
                ),
                on_error=lambda exc: self._on_session_error(
                    exc,
                    _ble_generation=callback_generation,
                ),
                on_disconnected=lambda: self._on_disconnected(
                    _ble_generation=callback_generation,
                ),
                on_battery_level=lambda level: self._on_battery_level(
                    level,
                    _ble_generation=callback_generation,
                ),
                gain_db=float(self._config["gain_db"]),
            )
            stage = "connect_gatt"
            await self._ble_session.connect(candidate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._publish_runtime_status(
                bridge_runtime_status.BridgeConnectionState.RETRY_WAIT
            )
            self._logger.error(
                "startup: RC003 connection failed: stage=%s error_type=%s",
                stage,
                type(exc).__name__,
            )
            raise

        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.CONNECTED
        )
        start_battery_monitor = getattr(
            self._ble_session,
            "start_battery_monitor",
            None,
        )
        if callable(start_battery_monitor):
            try:
                start_battery_monitor()
            except Exception as exc:
                self._logger.warning(
                    "optional RC003 battery monitor start failed: error_type=%s",
                    type(exc).__name__,
                )

    def _start_input_channels(self) -> None:
        """Start process-lifetime input resources before any BLE attempt."""

        from . import remote_selection
        if not self._selected_remote_key:
            raise remote_selection.SelectionError("请先在设备页选择要使用的遥控器。")

        with self._voice_shortcut.lock:
            self._accept_input_events = True
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_stopping = False
        with self._raw_input_lifecycle_lock:
            self._raw_input_stopping = False
        if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
            self._start_hid_listener()
            return
        self._start_voice_key_physicalizer()
        self._start_hid_listener()
        self._start_hid_report_tap()

    def _start_voice_key_physicalizer(self, *, recovering: bool = False) -> None:
        with self._voice_key_physicalizer_operation_lock:
            with self._voice_key_physicalizer_lifecycle_lock:
                if (
                    self._voice_key_physicalizer_stopping
                    or not self._accept_input_events
                ):
                    return
                current = self._voice_key_physicalizer
                if (
                    current is not None
                    and current.is_running
                    and bool(getattr(current, "accepts_new_down", True))
                ):
                    return
                if current is not None and current.is_running:
                    self._voice_key_physicalizer_ready = False
                    if not self._voice_key_physicalizer_degraded_retry_deadline:
                        self._voice_key_physicalizer_degraded_retry_deadline = (
                            time.monotonic()
                            + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
                        )
                    self._schedule_voice_key_physicalizer_recovery_locked()
                    return
                # A queued callback belongs to the owner/generation captured
                # when it was scheduled. Retire that work before publishing a
                # replacement so a failed replacement start can own the sole
                # retry slot.
                self._cancel_voice_key_physicalizer_retry_locked()
                self._voice_key_physicalizer = None
                self._voice_key_physicalizer_generation += 1
                generation = self._voice_key_physicalizer_generation
                self._voice_key_physicalizer_lost_generation = -1
                physicalizer = (
                    voice_key_physicalizer_windows.VoiceKeyPhysicalizer()
                )
                self._voice_key_physicalizer = physicalizer
                self._voice_key_physicalizer_ready = False
                state = "recovering" if recovering else "starting"
            self._set_runtime_input_state(
                voice_key_physicalizer_state=state
            )
            set_tracking_lost_callback = getattr(
                physicalizer,
                "set_tracking_lost_callback",
                None,
            )
            if callable(set_tracking_lost_callback):
                set_tracking_lost_callback(
                    lambda physicalizer=physicalizer, generation=generation: (
                        self._on_voice_key_physicalizer_tracking_lost(
                            physicalizer,
                            generation,
                        )
                    )
                )
            set_health_failure_callback = getattr(
                physicalizer,
                "set_health_failure_callback",
                None,
            )
            if callable(set_health_failure_callback):
                set_health_failure_callback(
                    lambda reason, snapshot, physicalizer=physicalizer,
                    generation=generation: (
                        self._on_voice_key_physicalizer_health_failure(
                            physicalizer,
                            generation,
                            reason,
                            snapshot,
                        )
                    )
                )
            try:
                physicalizer.start()
            except (
                voice_key_physicalizer_windows.
                VoiceKeyPhysicalizerUnavailableError
            ) as exc:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is physicalizer
                        and self._voice_key_physicalizer_generation == generation
                    ):
                        self._voice_key_physicalizer_ready = False
                        if not physicalizer.is_running:
                            self._voice_key_physicalizer = None
                        self._schedule_voice_key_physicalizer_recovery_locked()
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                if physicalizer.is_running:
                    self._logger.exception(
                        "startup: voice key physicalizer failed but is still running; "
                        "owner retained for cleanup"
                    )
                    raise
                self._logger.warning(
                    "startup: voice key physicalizer unavailable; recovery scheduled: %s",
                    exc,
                )
                return

            stop_stale_instance = False
            with self._voice_key_physicalizer_lifecycle_lock:
                current_generation = self._voice_key_physicalizer_generation
                stale = (
                    self._voice_key_physicalizer is not physicalizer
                    or current_generation != generation
                    or self._voice_key_physicalizer_stopping
                )
                lost = self._voice_key_physicalizer_lost_generation == generation
                if stale:
                    stop_stale_instance = physicalizer.is_running
                elif lost or not physicalizer.is_running:
                    self._voice_key_physicalizer_ready = False
                    self._schedule_voice_key_physicalizer_recovery_locked()
                else:
                    self._voice_key_physicalizer_ready = True
                    self._voice_key_physicalizer_degraded_retry_deadline = 0.0
            if stop_stale_instance:
                try:
                    physicalizer.stop()
                except Exception:
                    self._logger.exception(
                        "cleanup: stale voice key physicalizer did not stop"
                    )
                return
            if lost or not physicalizer.is_running:
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="recovering"
                )
                return
            self._set_runtime_input_state(
                voice_key_physicalizer_state="ready"
            )
            self._logger.info("startup: marked voice key physicalizer enabled")

    def _cancel_voice_key_physicalizer_retry_locked(self) -> None:
        timer = self._voice_key_physicalizer_retry_timer
        self._voice_key_physicalizer_retry_timer = None
        self._voice_key_physicalizer_retry_token = None
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_voice_key_physicalizer_recovery_locked(self) -> None:
        if (
            self._voice_key_physicalizer_stopping
            or not self._accept_input_events
            or self._voice_key_physicalizer_retry_timer is not None
        ):
            return
        token = object()
        expected_physicalizer = self._voice_key_physicalizer
        expected_generation = self._voice_key_physicalizer_generation
        try:
            timer = self._voice_key_physicalizer_timer_factory(
                _VOICE_KEY_PHYSICALIZER_RETRY_SECONDS,
                lambda token=token,
                expected_physicalizer=expected_physicalizer,
                expected_generation=expected_generation: (
                    self._recover_voice_key_physicalizer(
                        token,
                        expected_physicalizer,
                        expected_generation,
                    )
                ),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._voice_key_physicalizer_retry_token = token
            self._voice_key_physicalizer_retry_timer = timer
            timer.start()
        except BaseException as exc:
            if self._voice_key_physicalizer_retry_token is token:
                self._voice_key_physicalizer_retry_token = None
                self._voice_key_physicalizer_retry_timer = None
            self._logger.error(
                "voice key physicalizer recovery timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _recover_voice_key_physicalizer(
        self,
        token: object,
        expected_physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ],
        expected_generation: int,
    ) -> None:
        with self._voice_key_physicalizer_lifecycle_lock:
            if self._voice_key_physicalizer_retry_token is not token:
                return
            self._voice_key_physicalizer_retry_token = None
            self._voice_key_physicalizer_retry_timer = None
            if (
                self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            if (
                self._voice_key_physicalizer is not expected_physicalizer
                or self._voice_key_physicalizer_generation
                != expected_generation
            ):
                return
            physicalizer = self._voice_key_physicalizer
            generation = self._voice_key_physicalizer_generation
            running = bool(physicalizer is not None and physicalizer.is_running)
            accepts_new_down = bool(
                running and getattr(physicalizer, "accepts_new_down", True)
            )
            if running and accepts_new_down:
                if self._voice_key_physicalizer_lost_generation == generation:
                    self._schedule_voice_key_physicalizer_recovery_locked()
                    return
                self._voice_key_physicalizer_ready = True
                self._voice_key_physicalizer_degraded_retry_deadline = 0.0
                return
        if running:
            with self._voice_shortcut.lock:
                cleanup_blocks_recovery = bool(
                    self._voice_shortcut.pending_tokens is not None
                    or self._voice_shortcut.controller.active
                    or self._doubao_session.busy
                )
            if cleanup_blocks_recovery:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is not physicalizer
                        or self._voice_key_physicalizer_generation != generation
                        or self._voice_key_physicalizer_stopping
                        or not self._accept_input_events
                    ):
                        return
                    if (
                        self._voice_key_physicalizer_degraded_retry_deadline
                        and time.monotonic()
                        < self._voice_key_physicalizer_degraded_retry_deadline
                    ):
                        self._schedule_voice_key_physicalizer_recovery_locked()
                        return
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                self._diagnostic_trace.emit(
                    "voice_key_physicalizer_health",
                    status="blocked",
                    reason="owned_cleanup_unresolved",
                    app_generation=int(generation),
                )
                self._logger.error(
                    "voice key physicalizer recovery blocked: owned voice "
                    "shortcut cleanup is still unresolved"
                )
                return

            stop_succeeded = False
            with self._voice_key_physicalizer_operation_lock:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is not physicalizer
                        or self._voice_key_physicalizer_generation != generation
                        or self._voice_key_physicalizer_stopping
                        or not self._accept_input_events
                    ):
                        return
                try:
                    physicalizer.stop()
                except Exception:
                    self._logger.exception(
                        "voice key physicalizer recovery could not retire the "
                        "degraded owner; replacement not started"
                    )
                else:
                    stop_succeeded = True
                    with self._voice_key_physicalizer_lifecycle_lock:
                        if (
                            self._voice_key_physicalizer is physicalizer
                            and self._voice_key_physicalizer_generation == generation
                        ):
                            self._voice_key_physicalizer = None
                            self._voice_key_physicalizer_ready = False
            if not stop_succeeded:
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                return
            self._set_runtime_input_state(
                voice_key_physicalizer_state="recovering"
            )
            self._start_voice_key_physicalizer(recovering=True)
            return

        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is not physicalizer
                or self._voice_key_physicalizer_generation != generation
                or self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            self._voice_key_physicalizer = None
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        self._start_voice_key_physicalizer(recovering=True)

    def _resume_degraded_voice_key_physicalizer_recovery(self) -> None:
        """Resume one blocked recovery after authoritative cleanup succeeds."""

        with self._voice_key_physicalizer_lifecycle_lock:
            physicalizer = self._voice_key_physicalizer
            if (
                physicalizer is None
                or not physicalizer.is_running
                or bool(getattr(physicalizer, "accepts_new_down", True))
                or self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            self._voice_key_physicalizer_degraded_retry_deadline = (
                time.monotonic()
                + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
            )
            self._schedule_voice_key_physicalizer_recovery_locked()


    def _cancel_raw_input_retry_locked(self) -> None:
        timer = self._raw_input_retry_timer
        self._raw_input_retry_timer = None
        self._raw_input_retry_token = None
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_raw_input_recovery_locked(self) -> None:
        if (
            self._raw_input_stopping
            or not self._accept_input_events
            or self._raw_input_retry_timer is not None
        ):
            return
        token = object()
        delay = self._raw_input_retry_delay
        self._raw_input_retry_delay = min(
            _RAW_INPUT_RETRY_MAX_SECONDS,
            max(_RAW_INPUT_RETRY_INITIAL_SECONDS, delay * 2.0),
        )
        try:
            timer = self._raw_input_timer_factory(
                delay,
                lambda token=token: self._recover_raw_input_listener(token),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._raw_input_retry_token = token
            self._raw_input_retry_timer = timer
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - input remains failed closed
            if self._raw_input_retry_token is token:
                self._raw_input_retry_token = None
                self._raw_input_retry_timer = None
            self._logger.error(
                "Raw Input recovery timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _raw_input_callback_is_current(
        self,
        listener: Optional[raw_input_windows.RawInputButtonListener],
        generation: Optional[int],
    ) -> bool:
        if listener is None and generation is None:
            return True
        if listener is None or generation is None:
            return False
        with self._raw_input_lifecycle_lock:
            return (
                not self._raw_input_stopping
                and self._hid_listener is listener
                and self._raw_input_generation == int(generation)
            )

    def _cancel_raw_input_ownership_locked(self, *, reason: str) -> set[str]:
        """Cancel every Raw-owned edge while the input lock is held."""

        tracked_logical_buttons, tracked_physical_buttons = (
            self._raw_fallback_tracked_button_ids_locked()
        )
        raw_fallback_buttons = (
            set(self._raw_fallback_buttons_down) | tracked_logical_buttons
        )
        raw_fallback_physical_buttons = (
            self._take_raw_fallback_physical_buttons_locked(
                raw_fallback_buttons
            )
        )
        affected_buttons = raw_fallback_buttons | tracked_physical_buttons
        self._cancel_all_raw_fallback_hold_guards_locked()
        self._raw_fallback_buttons_down.clear()
        if affected_buttons:
            self._cancel_input_gestures(
                affected_buttons,
                reason=reason,
                block_until_release=True,
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason=reason,
            )
        return affected_buttons

    def _recover_raw_input_listener(self, token: object) -> None:
        with self._raw_input_operation_lock:
            with self._input_arbitration_lock:
                with self._raw_input_lifecycle_lock:
                    if self._raw_input_retry_token is not token:
                        return
                    self._raw_input_retry_token = None
                    self._raw_input_retry_timer = None
                    if self._raw_input_stopping or not self._accept_input_events:
                        return
                    if (self._remote_profile == remote_selection.CHROMECAST_PROFILE
                            and self._chromecast_runtime.voice_host is not None
                            and not self._chromecast_runtime.voice_host.closed):
                        # Preserve physical ownership until the voice owner has
                        # released its keys. A tracker restart clears snapshots.
                        self._chromecast_runtime.voice_host.cancel_recording()
                        self._schedule_raw_input_recovery_locked()
                        return
                    if (self._remote_profile == remote_selection.CHROMECAST_PROFILE
                            and not self._release_pending_button_keys()):
                        self._schedule_raw_input_recovery_locked()
                        return
                    listener = self._hid_listener
                    if listener is not None:
                        # Invalidate every callback before stop() can emit
                        # forced releases. The arbitration lock also lets an
                        # already-running callback finish before its resulting
                        # state is cancelled below.
                        self._raw_input_generation += 1
                if listener is not None:
                    self._cancel_raw_input_ownership_locked(
                        reason="raw_input_recovery"
                    )

            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    self._set_raw_listener_state("failed_stopping")
                    self._logger.exception(
                        "Raw Input recovery could not stop the old listener"
                    )
                    with self._raw_input_lifecycle_lock:
                        self._schedule_raw_input_recovery_locked()
                    return
                with self._raw_input_lifecycle_lock:
                    if self._hid_listener is listener:
                        self._hid_listener = None
                        self._raw_fallback_tracking_active = False

            try:
                self._start_hid_listener_owned(recovering=True)
            except raw_input_windows.RawInputUnavailableError:
                self._logger.exception(
                    "Raw Input recovery retained a listener that did not start cleanly"
                )
                with self._raw_input_lifecycle_lock:
                    self._schedule_raw_input_recovery_locked()

    def _start_hid_listener(self, *, recovering: bool = False) -> None:
        with self._raw_input_operation_lock:
            self._start_hid_listener_owned(recovering=recovering)

    def _set_raw_listener_state(self, state: str) -> None:
        # Chromecast's device status belongs to its receiver, not the auxiliary
        # keyboard tracker. Do not replace chromecast_ready on tracker recovery.
        if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
            self._logger.info("keyboard safety tracking state=%s", state)
        else:
            self._set_runtime_input_state(raw_input_state=state)

    def _start_hid_listener_owned(self, *, recovering: bool = False) -> None:
        """Best-effort: buttons fail closed independently of BLE/voice.

        Multiple matching HID device paths -> fail closed for buttons only
        (log and leave the listener unstarted); this must not tear down the
        BLE/voice path, which does not depend on HID at all.

        XRBM-019 review round 1 P1 #3: a failed ``start()`` call does not
        necessarily mean the listener never came alive - it may have left a
        thread/window behind that its own bounded failed-start cleanup could
        not stop (see raw_input_windows.py's ``_abandon_failed_start()``,
        which keeps ``is_running`` honest for exactly this reason). Clearing
        ``self._hid_listener`` to ``None`` unconditionally here would lose
        that owner reference and let a later ``_connect_once()`` generation
        start a second listener over the still-live one. Only clear it once
        the listener itself confirms it is not running; otherwise retain and
        re-raise so this propagates up through ``_connect_once()`` into
        ``ConnectionSupervisor.run_forever()``'s except handler, which still
        falls through to ``_cleanup_once()`` - giving cleanup a chance to
        retry stopping it, exactly like any other retained-owner failure.
        """

        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                if self._raw_input_stopping or not self._accept_input_events:
                    return
                current = self._hid_listener
                if current is not None and bool(
                    getattr(current, "is_running", False)
                ):
                    return
                self._hid_listener = None
                self._raw_fallback_tracking_active = False
                self._raw_input_generation += 1
                generation = self._raw_input_generation
                self._raw_input_lost_generation = -1

        self._set_raw_listener_state("recovering" if recovering else "starting")
        keyboard_only = self._remote_profile == remote_selection.CHROMECAST_PROFILE
        try:
            device_path = None
            if not keyboard_only:
                paths = raw_input_windows.enumerate_matching_device_paths()
                device_path = remote_selection.selected_raw_path(paths, self._selected_remote_key)
        except raw_input_windows.RawInputUnavailableError as exc:
            self._set_runtime_input_state(raw_input_state="unavailable")
            self._logger.info("startup: Raw Input unavailable; buttons disabled: %s", exc)
            with self._raw_input_lifecycle_lock:
                if self._raw_input_generation == generation:
                    self._schedule_raw_input_recovery_locked()
            return
        except hid_identity.NoDevicePathFoundError:
            self._set_runtime_input_state(raw_input_state="no_device")
            self._logger.info("startup: no RC003 HID device path found; buttons unavailable")
            with self._raw_input_lifecycle_lock:
                if self._raw_input_generation == generation:
                    self._schedule_raw_input_recovery_locked()
            return
        except hid_identity.AmbiguousDevicePathError as exc:
            self._set_runtime_input_state(raw_input_state="ambiguous")
            self._logger.info(
                "startup: buttons failing closed, ambiguous HID device paths: %s", exc
            )
            with self._raw_input_lifecycle_lock:
                if self._raw_input_generation == generation:
                    self._schedule_raw_input_recovery_locked()
            return

        listener = raw_input_windows.RawInputButtonListener(
            self._on_raw_button_event
        )
        with self._raw_input_lifecycle_lock:
            if (
                self._raw_input_stopping
                or not self._accept_input_events
                or self._raw_input_generation != generation
            ):
                return
            self._hid_listener = listener
        if not keyboard_only:
            self._sync_physical_bindings_to_listener()
        set_sourced_button_event_callback = getattr(
            listener,
            "set_sourced_button_event_callback",
            None,
        )
        if not keyboard_only and callable(set_sourced_button_event_callback):
            set_sourced_button_event_callback(
                lambda button_id, is_pressed, source, windows_button_id: (
                    self._on_raw_button_event(
                    button_id,
                    is_pressed,
                    source,
                    windows_button_id,
                    _listener=listener,
                    _generation=generation,
                )
                )
            )
        set_raw_event_callback = getattr(
            listener,
            "set_raw_event_callback",
            None,
        )
        if not keyboard_only and callable(set_raw_event_callback):
            set_raw_event_callback(
                lambda event: self._on_raw_physical_event(
                    event,
                    _listener=listener,
                    _generation=generation,
                )
            )
            with self._raw_input_lifecycle_lock:
                if (
                    self._hid_listener is listener
                    and self._raw_input_generation == generation
                ):
                    self._raw_fallback_tracking_active = True
        set_device_removed_callback = getattr(
            listener,
            "set_device_removed_callback",
            None,
        )
        if not keyboard_only and callable(set_device_removed_callback):
            set_device_removed_callback(
                lambda: self._on_raw_input_device_removed(
                    _listener=listener,
                    _generation=generation,
                )
            )
        set_input_corruption_callback = getattr(
            listener,
            "set_input_corruption_callback",
            None,
        )
        if not keyboard_only and callable(set_input_corruption_callback):
            set_input_corruption_callback(
                lambda reason: self._on_raw_input_corruption(
                    reason,
                    _listener=listener,
                    _generation=generation,
                )
            )
        set_physical_keyboard_tracking_lost_callback = getattr(
            listener,
            "set_physical_keyboard_tracking_lost_callback",
            None,
        )
        if callable(set_physical_keyboard_tracking_lost_callback):
            set_physical_keyboard_tracking_lost_callback(
                lambda reason: self._on_physical_keyboard_tracking_lost(
                    reason,
                    _listener=listener,
                    _generation=generation,
                )
            )
        try:
            listener.start(device_path)
        except raw_input_windows.RawInputUnavailableError as exc:
            if listener.is_running:
                self._set_raw_listener_state("failed_running")
                self._logger.exception(
                    "startup: Raw Input listener failed to start but is still running; "
                    "owner retained for cleanup to retry"
                )
                raise
            self._logger.info("startup: Raw Input listener failed to start: %s", exc)
            with self._raw_input_lifecycle_lock:
                if (
                    self._hid_listener is listener
                    and self._raw_input_generation == generation
                ):
                    self._hid_listener = None
                    self._raw_fallback_tracking_active = False
                    self._schedule_raw_input_recovery_locked()
            self._set_raw_listener_state("failed")
            return
        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                stale = (
                    self._raw_input_stopping
                    or not self._accept_input_events
                    or self._hid_listener is not listener
                    or self._raw_input_generation != generation
                )
                lost = self._raw_input_lost_generation == generation
                running = listener.is_running
                if not stale and (lost or not running):
                    self._schedule_raw_input_recovery_locked()
                elif not stale:
                    self._raw_input_retry_delay = (
                        _RAW_INPUT_RETRY_INITIAL_SECONDS
                    )
            if not stale:
                self._set_raw_listener_state("recovering" if lost or not running else "ready")
        if stale:
            if listener.is_running:
                try:
                    listener.stop()
                except Exception:
                    self._logger.exception(
                        "cleanup: stale Raw Input listener did not stop"
                    )
            return
        if lost or not running:
            return

    def _sync_physical_bindings_to_listener(self) -> None:
        with self._button_mapping_lock:
            listener = self._hid_listener
            if listener is None:
                return
            set_physical_bindings = getattr(listener, "set_physical_bindings", None)
            if not callable(set_physical_bindings):
                return
            try:
                set_physical_bindings(
                    self._bindings.get("physical_bindings", {})
                )
            except Exception as exc:  # noqa: BLE001 - keep the last live decoder
                self._logger.warning(
                    "physical button bindings update failed: error_type=%s",
                    type(exc).__name__,
                )

    def _start_hid_report_tap(self) -> None:
        """Start the upstream-derived tap for usages Windows drops.

        This is independent of the normal Raw Input listener.  A missing or
        unverified Gadget is a button-only degradation and must not prevent
        BLE voice from starting.
        """

        self._set_runtime_input_state(hid_tap_state="starting")
        tap = frida_compat.RC003HidReportTap(
            self._on_direct_hid_report,
            selected_key=self._selected_remote_key,
            status_handler=self._on_hid_tap_status,
            diagnostic_trace=self._diagnostic_trace,
        )
        try:
            if tap.start():
                self._hid_report_tap = tap
                self._logger.info(
                    "startup: RC003 HID report tap thread started; state=%s",
                    tap.status,
                )
            else:
                self._set_runtime_input_state(hid_tap_state=tap.status)
                self._logger.info(
                    "startup: RC003 HID report tap unavailable: %s", tap.status
                )
        except Exception:
            self._set_runtime_input_state(
                hid_tap_state=frida_compat.HidTapState.FAILED.value
            )
            self._logger.exception("startup: RC003 HID report tap failed to start")
            try:
                tap.stop()
            except Exception:
                self._logger.exception("startup: RC003 HID report tap cleanup failed")
                self._hid_report_tap = tap
                raise

    @staticmethod
    def _direct_buttons_for_usages(usages: set[int]) -> set[str]:
        return {
            button
            for usage in usages
            if (button := frida_compat.TAP_USAGE_TO_BUTTON.get(usage)) is not None
        }

    def _cancel_input_gestures(
        self,
        buttons: set[str],
        *,
        reason: str,
        block_until_release: bool,
    ) -> None:
        if block_until_release:
            self._block_input_until_release(buttons)
        self._button_combos.reset()
        self._button_gestures.reset()
        self._key_detection_suppressed_buttons.clear()
        self._key_detection_suppression_deadlines.clear()

        if "mic" in buttons:
            with self._ordinary_mic_lock:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
            with self._key_detection_mic_lock:
                self._reset_key_detection_mic_gesture_locked()
            with self._voice_shortcut.lock:
                self._voice_mic_gesture_sources_down.clear()
                self._voice_mic_gesture_hid_released = True
                if self._voice_shortcut.controller.active:
                    self._release_hold_voice_on_physical_release_locked(reason)
                elif self._doubao_session.busy:
                    self._doubao_session.cancel_current()
                if (
                    self._voice_mic_gesture_active
                    and not self._voice_audio_stream_active
                ):
                    self._finish_voice_mic_gesture()
                self._apply_pending_voice_settings_if_idle_locked()

        if buttons:
            self._logger.warning(
                "button input ownership cancelled: reason=%s buttons=%s",
                reason,
                sorted(buttons),
            )

    def _cancel_input_gestures_for_buttons(
        self,
        buttons: set[str],
        *,
        reason: str,
        block_until_release: bool,
    ) -> None:
        """Cancel only input state coupled to the named physical buttons."""

        affected_buttons = set(buttons)
        affected_buttons.update(
            self._button_combos.cancel_buttons(affected_buttons)
        )
        if block_until_release:
            self._block_input_until_release(affected_buttons)
        self._button_gestures.cancel_buttons(affected_buttons)
        self._key_detection_suppressed_buttons.difference_update(
            affected_buttons
        )
        for button_id in affected_buttons:
            self._key_detection_suppression_deadlines.pop(button_id, None)

        if "mic" in affected_buttons:
            with self._ordinary_mic_lock:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
            with self._key_detection_mic_lock:
                self._reset_key_detection_mic_gesture_locked()
            with self._voice_shortcut.lock:
                self._voice_mic_gesture_sources_down.clear()
                self._voice_mic_gesture_hid_released = True
                if self._voice_shortcut.controller.active:
                    self._release_hold_voice_on_physical_release_locked(reason)
                elif self._doubao_session.busy:
                    self._doubao_session.cancel_current()
                if (
                    self._voice_mic_gesture_active
                    and not self._voice_audio_stream_active
                ):
                    self._finish_voice_mic_gesture()
                self._apply_pending_voice_settings_if_idle_locked()

        if affected_buttons:
            self._logger.warning(
                "button input ownership cancelled: reason=%s buttons=%s",
                reason,
                sorted(affected_buttons),
            )

    def _active_audio_owns_late_raw_mic(self) -> bool:
        """Return whether ATVV audio already owns the current mic press."""

        with self._voice_shortcut.lock:
            return (
                self._voice_mic_gesture_active
                and self._voice_mic_gesture_audio_started
                and self._voice_audio_stream_active
                and (
                    self._voice_shortcut.controller.active
                    or self._doubao_session.busy
                    or self._voice_shortcut.pending_tokens is not None
                )
            )

    def _on_raw_physical_event(
        self,
        event: raw_input_windows.RawInputEvent,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        """Track the real Windows key independently of a taught button ID."""

        if not self._raw_input_callback_is_current(_listener, _generation):
            return
        logical_button = event.button_id
        physical_button = event.windows_button_id
        trace_button = logical_button or physical_button or "unknown"
        trace_source = {
            "keyboard": "raw_keyboard",
            "hid": "raw_hid",
        }.get(event.source, "raw_unknown")
        gesture_id = self._diagnostic_trace.begin_gesture(
            trace_button,
            trace_source,
            bool(event.is_pressed),
            vkey=event.vkey if event.vkey is not None else -1,
            scan_code=event.make_code if event.make_code is not None else -1,
            flags=event.flags if event.flags is not None else -1,
        )
        self._diagnostic_trace.emit(
            "raw_input_event",
            gesture_id=gesture_id,
            button_id=event.button_id or "",
            windows_button_id=event.windows_button_id or "",
            edge="down" if event.is_pressed else "up",
            vkey=event.vkey if event.vkey is not None else -1,
            make_code=event.make_code if event.make_code is not None else -1,
            flags=event.flags if event.flags is not None else -1,
            raw_source=event.source,
            interception_armed=bool(self._direct_hid_interception_armed),
            interception_ready=bool(self._direct_hid_interception_ready),
        )
        signature = raw_input_windows.physical_signature(event)
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            self._raw_fallback_tracking_active = True

            if not event.is_pressed:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="release_tracking",
                )
                tracked = self._raw_fallback_physical_buttons_down.pop(
                    signature,
                    None,
                )
                debt_logical_buttons, debt_physical_buttons = (
                    self._raw_fallback_release_debts.pop(
                        signature,
                        (set(), set()),
                    )
                )
                released_buttons = (
                    set(debt_logical_buttons) | set(debt_physical_buttons)
                )
                if tracked is not None:
                    released_buttons.add(tracked[0])
                    if tracked[1] is not None:
                        released_buttons.add(tracked[1])
                else:
                    if logical_button is not None:
                        released_buttons.add(logical_button)
                    if physical_button is not None:
                        released_buttons.add(physical_button)
                remaining = [
                    {
                        candidate
                        for candidate in tracked_pair
                        if candidate is not None
                    }
                    for tracked_pair in (
                        self._raw_fallback_physical_buttons_down.values()
                    )
                ] + [
                    set(logical_buttons) | set(physical_buttons)
                    for logical_buttons, physical_buttons in (
                        self._raw_fallback_release_debts.values()
                    )
                ]
                with self._direct_hid_lock:
                    direct_buttons = self._direct_buttons_for_usages(
                        self._direct_hid_usages
                    )
                for blocked_button in released_buttons:
                    if (
                        blocked_button not in direct_buttons
                        and all(
                            blocked_button not in candidate
                            for candidate in remaining
                        )
                    ):
                        self._input_rearm_blocked_buttons.discard(
                            blocked_button
                        )
                return

            if not self._accept_input_events:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="ignored",
                    reason="input_not_accepted",
                )
                return
            release_debt = self._raw_fallback_release_debts.get(signature)
            if release_debt is not None:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="blocked",
                    reason="release_debt",
                )
                debt_logical_buttons, debt_physical_buttons = release_debt
                if logical_button is not None:
                    debt_logical_buttons.add(logical_button)
                if physical_button is not None:
                    debt_physical_buttons.add(physical_button)
                self._block_input_until_release(
                    set(debt_logical_buttons) | set(debt_physical_buttons)
                )
                if physical_button in _RAW_FALLBACK_KEY_TOKENS:
                    self._release_raw_fallback_keyups(
                        {physical_button},
                        reason="raw_release_debt_repeat",
                    )
                return
            if logical_button is None:
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="ignored",
                    reason="unmapped_physical_key",
                )
                return
            was_new = signature not in self._raw_fallback_physical_buttons_down
            tracked = self._raw_fallback_physical_buttons_down.setdefault(
                signature,
                (logical_button, physical_button),
            )
            tracked_logical, tracked_physical = tracked
            if not (
                self._direct_hid_interception_armed
                or self._direct_hid_interception_ready
            ):
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="windows_original",
                    reason="hid_interception_not_armed",
                )
                return

            physical_was_blocked = (
                tracked_physical in self._input_rearm_blocked_buttons
            )
            blocked_buttons = {tracked_logical}
            if tracked_physical is not None:
                blocked_buttons.add(tracked_physical)
            with self._direct_hid_lock:
                direct_buttons = self._direct_buttons_for_usages(
                    self._direct_hid_usages
                )
            raw_only_buttons = set(blocked_buttons) - direct_buttons
            if (
                "mic" in raw_only_buttons
                and self._active_audio_owns_late_raw_mic()
            ):
                # AudioStarted can beat both the duplicated Windows F5 edge
                # and the direct HID report. The active ATVV gesture remains
                # authoritative until HID-up or AudioStopped closes it.
                raw_only_buttons.discard("mic")
            newly_quarantined = was_new and not physical_was_blocked
            if newly_quarantined:
                # A late Raw Input down is the Windows-side duplicate of a
                # report already owned by direct HID. Quarantine only aliases
                # that direct HID is not currently holding; the real HID up
                # must remain the sole release edge for the active gesture.
                if raw_only_buttons:
                    self._cancel_input_gestures_for_buttons(
                        raw_only_buttons,
                        reason="late_raw_after_hid_handover",
                        block_until_release=True,
                    )
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="quarantined",
                    reason="late_raw_after_hid_handover",
                    late=True,
                )
            else:
                self._block_input_until_release(raw_only_buttons)
                self._diagnostic_trace.emit(
                    "raw_input_decision",
                    gesture_id=gesture_id,
                    button_id=trace_button,
                    decision="blocked",
                    reason="already_quarantined",
                )
            if newly_quarantined and tracked_physical in _RAW_FALLBACK_KEY_TOKENS:
                self._release_raw_fallback_keyups(
                    {tracked_physical},
                    reason="late_raw_after_hid_handover",
                )

    def _take_raw_fallback_physical_buttons_locked(
        self,
        logical_buttons: set[str],
    ) -> set[str]:
        matched_signatures = {
            signature
            for signature, (logical_button, _physical_button) in (
                self._raw_fallback_physical_buttons_down.items()
            )
            if logical_button in logical_buttons
        }
        physical_buttons = {
            self._raw_fallback_physical_buttons_down[signature][1]
            for signature in matched_signatures
            if self._raw_fallback_physical_buttons_down[signature][1]
            in _RAW_FALLBACK_KEY_TOKENS
        }
        for signature in matched_signatures:
            tracked = self._raw_fallback_physical_buttons_down.pop(
                signature,
                None,
            )
            if tracked is not None:
                release_debt = self._raw_fallback_release_debts.setdefault(
                    signature,
                    (set(), set()),
                )
                debt_logical_buttons, debt_physical_buttons = release_debt
                debt_logical_buttons.add(tracked[0])
                if tracked[1] is not None:
                    debt_physical_buttons.add(tracked[1])

        # Compatibility for injected test owners and old listener fakes that
        # do not expose raw physical events. The real listener always enables
        # tracking before it can emit a logical edge, so production remaps
        # never guess the logical key here.
        if not self._raw_fallback_tracking_active:
            physical_buttons.update(
                logical_button
                for logical_button in logical_buttons
                if logical_button in _RAW_FALLBACK_KEY_TOKENS
            )
        return physical_buttons

    def _raw_fallback_tracked_button_ids_locked(
        self,
    ) -> tuple[set[str], set[str]]:
        logical_buttons = {
            logical_button
            for logical_button, _physical_button in (
                self._raw_fallback_physical_buttons_down.values()
            )
        }
        physical_buttons = {
            physical_button
            for _logical_button, physical_button in (
                self._raw_fallback_physical_buttons_down.values()
            )
            if physical_button in _RAW_FALLBACK_KEY_TOKENS
        }
        return logical_buttons, physical_buttons

    def _windows_buttons_down_before_hid_handover(
        self,
    ) -> Optional[set[str]]:
        buttons_down: set[str] = set()
        try:
            for button, token in _RAW_FALLBACK_KEY_TOKENS.items():
                vk_codes = win32_keys.resolve_vk_codes((token,))
                if any(
                    self._raw_windows_key_down_query(vk_code)
                    for vk_code in vk_codes
                ):
                    buttons_down.add(button)
        except Exception as exc:  # noqa: BLE001 - unknown state fails closed
            self._logger.warning(
                "Windows key state unavailable during HID handover: error_type=%s",
                type(exc).__name__,
            )
            return None
        return buttons_down

    def _release_raw_fallback_keyups(
        self,
        physical_buttons: set[str],
        *,
        reason: str,
    ) -> None:
        tokens = tuple(
            dict.fromkeys(
                token
                for button in sorted(physical_buttons)
                if (token := _RAW_FALLBACK_KEY_TOKENS.get(button)) is not None
            )
        )
        if not tokens:
            return
        with self._button_action_lock:
            pending = self._button_key_release_pending or ()
            owed_tokens = tuple(dict.fromkeys((*pending, *tokens)))
            self._button_key_release_pending = owed_tokens
            if self._release_pending_button_keys():
                self._logger.info(
                    "raw Windows key-up safety release completed: reason=%s buttons=%s",
                    reason,
                    sorted(physical_buttons),
                )

    def _cancel_raw_fallback_hold_guard_locked(self, button_id: str) -> None:
        guard = self._raw_fallback_hold_guards.pop(button_id, None)
        if guard is None:
            return
        _token, timer = guard
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException as exc:  # noqa: BLE001 - token is already invalid
                self._logger.warning(
                    "raw fallback hold timer cancel failed: button=%s error_type=%s",
                    button_id,
                    type(exc).__name__,
                )

    def _cancel_all_raw_fallback_hold_guards_locked(self) -> None:
        for button_id in tuple(self._raw_fallback_hold_guards):
            self._cancel_raw_fallback_hold_guard_locked(button_id)

    def _arm_raw_fallback_hold_guard_locked(self, button_id: str) -> bool:
        self._cancel_raw_fallback_hold_guard_locked(button_id)
        token = object()
        try:
            timer = self._raw_fallback_timer_factory(
                button_gesture.ButtonGestureDispatcher.MAX_HOLD_SECONDS,
                lambda button_id=button_id, token=token: (
                    self._raw_fallback_hold_timeout(button_id, token)
                ),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._raw_fallback_hold_guards[button_id] = (token, timer)
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - immediately release the key
            current = self._raw_fallback_hold_guards.get(button_id)
            if current is not None and current[0] is token:
                self._raw_fallback_hold_guards.pop(button_id, None)
            self._logger.error(
                "raw fallback hold timer failed to start: button=%s error_type=%s",
                button_id,
                type(exc).__name__,
            )
            return False
        return True

    def _raw_fallback_hold_timeout(self, button_id: str, token: object) -> None:
        with self._input_arbitration_lock:
            guard = self._raw_fallback_hold_guards.get(button_id)
            if guard is None or guard[0] is not token:
                return
            self._raw_fallback_hold_guards.pop(button_id, None)
            if button_id not in self._raw_fallback_buttons_down:
                return
            self._raw_fallback_buttons_down.discard(button_id)
            physical_buttons = self._take_raw_fallback_physical_buttons_locked(
                {button_id}
            )
            self._block_input_until_release(
                {button_id} | physical_buttons
            )
            self._release_raw_fallback_keyups(
                physical_buttons,
                reason="raw_fallback_hold_timeout",
            )
            self._logger.warning(
                "raw Windows key held for %.0fs; forced release until physical up: "
                "button=%s",
                button_gesture.ButtonGestureDispatcher.MAX_HOLD_SECONDS,
                button_id,
            )

    def _on_raw_input_device_removed(
        self,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        if not self._raw_input_callback_is_current(_listener, _generation):
            return
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            with self._raw_input_lifecycle_lock:
                resolved_generation = (
                    self._raw_input_generation
                    if _generation is None
                    else int(_generation)
                )
                self._raw_input_lost_generation = resolved_generation
            self._set_runtime_input_state(raw_input_state="device_removed")
            with self._direct_hid_lock:
                direct_buttons = self._direct_buttons_for_usages(
                    self._direct_hid_usages
                )
                self._direct_hid_usages.clear()
            tracked_logical_buttons, tracked_physical_buttons = (
                self._raw_fallback_tracked_button_ids_locked()
            )
            raw_fallback_buttons = (
                set(self._raw_fallback_buttons_down)
                | tracked_logical_buttons
            )
            raw_fallback_physical_buttons = (
                self._take_raw_fallback_physical_buttons_locked(
                    raw_fallback_buttons
                )
            )
            self._cancel_all_raw_fallback_hold_guards_locked()
            lost_buttons = (
                raw_fallback_buttons
                | tracked_physical_buttons
                | direct_buttons
                | set(self._input_rearm_blocked_buttons)
            )
            self._direct_hid_interception_ready = False
            self._direct_hid_interception_armed = False
            self._direct_hid_handover_waiting_for_neutral = False
            self._raw_fallback_buttons_down.clear()
            self._input_rearm_blocked_buttons.clear()
            # The Raw Input collection can be re-enumerated while the
            # separately owned HID tap is still alive. Preserve a guard for
            # direct buttons that were down until a later neutral report or
            # matching release proves the old hold is over.
            self._block_input_until_release(lost_buttons)
            self._cancel_input_gestures(
                lost_buttons,
                reason="raw_input_device_removed",
                block_until_release=False,
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason="raw_input_device_removed",
            )
        self._logger.warning("RC003 Raw Input device removed; requesting reconnect")
        with self._raw_input_lifecycle_lock:
            if self._raw_input_callback_is_current(_listener, _generation):
                self._schedule_raw_input_recovery_locked()
        self._supervisor.request_reconnect()

    def _on_raw_input_corruption(
        self,
        reason: str,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        """Cancel Raw Input ownership while preserving the physical release."""

        if not self._raw_input_callback_is_current(_listener, _generation):
            return
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            affected_buttons = self._cancel_raw_input_ownership_locked(
                reason=f"raw_input_corruption_{reason}"
            )
            if reason in {
                "raw_input_header_unavailable",
                "raw_input_listener_exited",
            }:
                with self._raw_input_lifecycle_lock:
                    resolved_generation = (
                        self._raw_input_generation
                        if _generation is None
                        else int(_generation)
                    )
                    self._raw_input_lost_generation = resolved_generation
                    self._schedule_raw_input_recovery_locked()
                self._set_runtime_input_state(raw_input_state="recovering")
        self._logger.warning(
            "RC003 Raw Input data rejected: reason=%s cancelled_buttons=%s",
            reason,
            sorted(affected_buttons),
        )

    def _on_physical_keyboard_tracking_lost(
        self,
        reason: str,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        """End an unsafe ordinary-key HOLD; future downs fail closed."""

        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            with self._raw_input_lifecycle_lock:
                resolved_generation = (
                    self._raw_input_generation
                    if _generation is None
                    else int(_generation)
                )
                self._raw_input_lost_generation = resolved_generation
                self._schedule_raw_input_recovery_locked()
            self._set_raw_listener_state("unhealthy")
            if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
                host = self._chromecast_runtime.voice_host
                if host is not None:
                    host.cancel_recording()
            with self._voice_shortcut.lock:
                tokens = tuple(self._voice_shortcut.hotkey.modifiers) + (
                    self._voice_shortcut.hotkey.key,
                )
                active_hold = (
                    self._voice_shortcut.controller.active
                    or self._voice_shortcut.pending_tokens is not None
                )
                if (self._remote_profile != remote_selection.CHROMECAST_PROFILE
                        and active_hold and not win32_input.can_begin_tracked_hold(tokens)):
                    self._force_voice_hold_release_locked(
                        "physical keyboard tracking lost"
                    )
            self._release_pending_button_keys()
        self._logger.warning(
            "physical keyboard tracking lost: reason=%s; unsafe held shortcuts disabled",
            reason,
        )

    def _on_voice_key_physicalizer_tracking_lost(
        self,
        physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ] = None,
        generation: Optional[int] = None,
    ) -> None:
        """Release an active marked right-Alt hold before the hook disappears."""

        with self._voice_key_physicalizer_lifecycle_lock:
            current = self._voice_key_physicalizer
            current_generation = self._voice_key_physicalizer_generation
            resolved_physicalizer = physicalizer or current
            resolved_generation = (
                current_generation if generation is None else int(generation)
            )
            if (
                resolved_physicalizer is None
                or current is not resolved_physicalizer
                or current_generation != resolved_generation
                or self._voice_key_physicalizer_stopping
            ):
                return
            self._voice_key_physicalizer_lost_generation = resolved_generation
            self._voice_key_physicalizer_ready = False
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
            host = self._chromecast_runtime.voice_host
            if host is not None:
                wait_for_release = getattr(
                    host,
                    "cancel_recording_and_wait",
                    None,
                )
                if callable(wait_for_release):
                    wait_for_release(2.5)
                else:
                    host.cancel_recording()
            with self._voice_key_physicalizer_lifecycle_lock:
                if (
                    self._voice_key_physicalizer is resolved_physicalizer
                    and self._voice_key_physicalizer_generation
                    == resolved_generation
                    and not self._voice_key_physicalizer_stopping
                ):
                    self._schedule_voice_key_physicalizer_recovery_locked()
            self._logger.warning(
                "voice key physicalizer stopped unexpectedly; "
                "Chromecast voice cancelled and recovery scheduled"
            )
            return
        raw_tracking_available = (
            raw_input_windows.physical_keyboard_tracking_available()
        )
        with self._voice_shortcut.lock:
            tokens = tuple(self._voice_shortcut.hotkey.modifiers) + (
                self._voice_shortcut.hotkey.key,
            )
            try:
                uses_right_alt = (
                    win32_keys.VK_CODES["ralt"]
                    in win32_keys.resolve_vk_codes(tokens)
                )
            except win32_keys.UnknownKeyTokenError:
                uses_right_alt = False
            active_hold = (
                self._voice_shortcut.controller.active
                or self._voice_shortcut.pending_tokens is not None
            )
            if (
                active_hold
                and (
                    not raw_tracking_available
                    or (
                        uses_right_alt
                        and self._configured_voice_hotkey_backend()
                        in {
                            _VOICE_HOTKEY_BACKEND_MARKED,
                            _VOICE_HOTKEY_BACKEND_DOUBAO,
                        }
                    )
                )
            ):
                self._force_voice_hold_release_locked(
                    "voice key physicalizer stopped unexpectedly"
                )
        if not raw_tracking_available:
            self._release_pending_button_keys()
        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is resolved_physicalizer
                and self._voice_key_physicalizer_generation
                == resolved_generation
                and not self._voice_key_physicalizer_stopping
            ):
                self._schedule_voice_key_physicalizer_recovery_locked()
        self._logger.warning(
            "voice key physicalizer stopped unexpectedly; recovery scheduled"
        )

    def _on_voice_key_physicalizer_health_failure(
        self,
        physicalizer: voice_key_physicalizer_windows.VoiceKeyPhysicalizer,
        generation: int,
        reason: str,
        snapshot: voice_key_physicalizer_windows.PhysicalizerHealthSnapshot,
    ) -> None:
        """Close admission immediately and recover only after owned cleanup."""

        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is not physicalizer
                or self._voice_key_physicalizer_generation != int(generation)
                or self._voice_key_physicalizer_stopping
            ):
                return
            self._voice_key_physicalizer_ready = False
            self._voice_key_physicalizer_degraded_retry_deadline = (
                time.monotonic()
                + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
            )
            self._schedule_voice_key_physicalizer_recovery_locked()
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        try:
            self._diagnostic_trace.emit(
                "voice_key_physicalizer_health",
                status="degraded",
                reason=str(reason),
                app_generation=int(generation),
                **snapshot.trace_fields(),
            )
        except BaseException:
            pass
        self._logger.warning(
            "voice key physicalizer degraded after required acknowledgement "
            "failure; new voice starts blocked until owned cleanup completes: "
            "generation=%s tracker_generation=%s callback_delta=%s marker_delta=%s",
            generation,
            snapshot.generation,
            snapshot.receipt_callback_entry_delta,
            snapshot.receipt_marker_callback_delta,
        )

    def _on_hid_tap_status(self, status: str, detail: str) -> None:
        self._diagnostic_trace.emit(
            "hid_tap_status",
            status=str(status),
            detail=str(detail or ""),
        )
        self._set_runtime_input_state(hid_tap_state=status)
        interception_ready = status == frida_compat.HidTapState.READY.value
        interception_armed = status in {
            frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
            frida_compat.HidTapState.READY.value,
        }
        message = "RC003 HID report tap state: %s"
        args = [status]
        if detail:
            message += " detail=%s"
            args.append(detail)
        with self._input_arbitration_lock:
            newly_armed = (
                interception_armed
                and not self._direct_hid_interception_armed
            )
            if not interception_ready and self._direct_hid_interception_ready:
                self._logger.warning(message, *args)
                with self._direct_hid_lock:
                    stale_usages = set(self._direct_hid_usages)
                    self._direct_hid_usages.clear()
                stale_buttons = self._direct_buttons_for_usages(stale_usages)
                self._cancel_input_gestures(
                    stale_buttons,
                    reason="hid_tap_ownership_lost",
                    block_until_release=True,
                )
            else:
                self._logger.info(message, *args)
            if newly_armed:
                # ATTACHED_WAITING_IO is emitted only after the helper has
                # acknowledged the interception lease. Cancel any in-flight
                # Raw Input gesture instead of completing it as a click, then
                # ignore the same physical hold until its real release arrives.
                windows_buttons_down = (
                    set()
                    if getattr(self._hid_report_tap, "native_copy_interception", False) is True
                    else self._windows_buttons_down_before_hid_handover()
                )
                self._direct_hid_handover_waiting_for_neutral = (
                    windows_buttons_down is None
                )
                windows_buttons_down = windows_buttons_down or set()
                tracked_logical_buttons, tracked_physical_buttons = (
                    self._raw_fallback_tracked_button_ids_locked()
                )
                raw_fallback_buttons = (
                    set(self._raw_fallback_buttons_down)
                    | tracked_logical_buttons
                )
                raw_fallback_physical_buttons = (
                    self._take_raw_fallback_physical_buttons_locked(
                        raw_fallback_buttons
                    )
                )
                handover_buttons = (
                    raw_fallback_buttons
                    | tracked_physical_buttons
                    | windows_buttons_down
                )
                self._cancel_all_raw_fallback_hold_guards_locked()
                self._cancel_input_gestures(
                    handover_buttons,
                    reason="hid_tap_handover",
                    block_until_release=True,
                )
                self._release_raw_fallback_keyups(
                    raw_fallback_physical_buttons | windows_buttons_down,
                    reason="hid_tap_handover",
                )
                self._raw_fallback_buttons_down.clear()
            elif not interception_armed:
                self._direct_hid_handover_waiting_for_neutral = False
            self._direct_hid_interception_armed = interception_armed
            self._direct_hid_interception_ready = interception_ready

    def _on_direct_hid_report(self, report_id: int, payload: bytes) -> None:
        """Translate every RC003 keyboard HID usage into button edges.

        The injected Gadget copies the report and clears its keyboard usages
        before Windows translates them. This callback therefore owns custom
        mapping only after the tap reports verified interception. Raw Keyboard
        remains observation/fallback and never adds a second mapped action.
        """

        observed_ns = time.perf_counter_ns()
        with self._input_arbitration_lock:
            self._direct_hid_report_seq += 1
            callback_seq = self._direct_hid_report_seq

            def record_decision(reason: str, **fields: object) -> None:
                if self._diagnostic_trace.enabled:
                    self._diagnostic_trace.emit(
                        "hid_report_decision",
                        callback_seq=callback_seq,
                        reason=reason,
                        resulting_usages=sorted(self._direct_hid_usages),
                        **fields,
                    )

            if self._diagnostic_trace.enabled:
                self._diagnostic_trace.emit(
                    "hid_report",
                    callback_seq=callback_seq,
                    observed_perf_counter_ns=observed_ns,
                    report_id=int(report_id),
                    report_length=len(payload),
                    report_hex=(
                        payload.hex() if report_id == 1 and len(payload) == 6 else ""
                    ),
                    previous_usages=sorted(self._direct_hid_usages),
                    accept_input=self._accept_input_events,
                    interception_armed=self._direct_hid_interception_armed,
                    interception_ready=self._direct_hid_interception_ready,
                    waiting_for_neutral=self._direct_hid_handover_waiting_for_neutral,
                )
            tap = self._hid_report_tap
            tap_reports_ready = (
                tap is not None
                and tap.status == frida_compat.HidTapState.READY.value
            )
            if (
                not self._accept_input_events
                or report_id != 1
                or len(payload) != 6
            ):
                record_decision(
                    "input_disabled" if not self._accept_input_events else "invalid_report"
                )
                return
            if not self._direct_hid_interception_ready:
                if not tap_reports_ready:
                    record_decision("tap_not_ready")
                    return
                # A newly received, validated tap report is stronger evidence
                # than the Raw Input collection-removal notification. Restore
                # local ownership without waiting for a duplicate status event.
                if not self._direct_hid_interception_armed:
                    windows_buttons_down = (
                        set()
                        if getattr(tap, "native_copy_interception", False) is True
                        else self._windows_buttons_down_before_hid_handover()
                    )
                    self._direct_hid_handover_waiting_for_neutral = (
                        windows_buttons_down is None
                    )
                    if windows_buttons_down:
                        self._block_input_until_release(windows_buttons_down)
                        self._release_raw_fallback_keyups(
                            windows_buttons_down,
                            reason="hid_tap_report_revalidated",
                        )
                self._direct_hid_interception_armed = True
                self._direct_hid_interception_ready = True
            elif tap is not None and not tap_reports_ready:
                # The tap updates its own state before invoking the app status
                # callback. Reject a report that raced that callback.
                record_decision("tap_lost_readiness")
                return
            active = {
                int.from_bytes(payload[index : index + 2], "little")
                for index in range(0, len(payload), 2)
            } & set(frida_compat.TAP_USAGE_TO_BUTTON)
            active_buttons = self._direct_buttons_for_usages(active)
            with self._direct_hid_lock:
                previous = self._direct_hid_usages
                previous_buttons = self._direct_buttons_for_usages(previous)
                if self._direct_hid_handover_waiting_for_neutral:
                    if active:
                        self._block_input_until_release(
                            active_buttons | previous_buttons
                        )
                        self._direct_hid_usages = set(active)
                        record_decision("waiting_for_neutral")
                        return
                    self._direct_hid_handover_waiting_for_neutral = False
                if not active:
                    # A verified neutral tap snapshot proves that every
                    # pre-handover Raw physical edge has ended. Drop those
                    # records so a later press cannot inherit stale ownership.
                    self._raw_fallback_physical_buttons_down.clear()
                    self._raw_fallback_release_debts.clear()
                self._input_rearm_blocked_buttons.difference_update(
                    self._input_rearm_blocked_buttons
                    - active_buttons
                    - previous_buttons
                )
                if active == previous:
                    record_decision("unchanged")
                    return
                pressed = active - previous
                released = previous - active
                self._direct_hid_usages = set(active)
            record_decision(
                "edges",
                pressed_usages=sorted(pressed),
                released_usages=sorted(released),
            )
            for usage in sorted(released):
                button = frida_compat.TAP_USAGE_TO_BUTTON[usage]
                self._logger.info(
                    "RC003 direct HID usage up: 0x%04x -> %s",
                    usage,
                    button,
                )
                self._record_rc003_direction_edge(usage, False)
                self._on_button_event(
                    button,
                    False,
                    event_source="hid_tap",
                    hid_usage=usage,
                )
            with self._button_mapping_lock:
                combo_modifier = key_mapping.button_combo_modifier(self._bindings)
            for usage in sorted(
                pressed,
                key=lambda candidate: (
                    frida_compat.TAP_USAGE_TO_BUTTON[candidate] != combo_modifier,
                    candidate,
                ),
            ):
                button = frida_compat.TAP_USAGE_TO_BUTTON[usage]
                self._logger.info(
                    "RC003 direct HID usage down: 0x%04x -> %s",
                    usage,
                    button,
                )
                self._record_rc003_direction_edge(usage, True)
                self._on_button_event(
                    button,
                    True,
                    event_source="hid_tap",
                    hid_usage=usage,
                )

    def _record_rc003_direction_edge(
        self,
        usage: int,
        is_pressed: bool,
    ) -> None:
        # The native report is already neutral before the kernel copy. Arming
        # the old global gate here could consume an unrelated keyboard arrow.
        if getattr(self._hid_report_tap, "native_copy_interception", False) is True:
            return
        key = frida_compat.TAP_DIRECTION_USAGE_TO_KEY.get(int(usage))
        if key is None:
            return
        vk, scan_code, extended = key
        with self._voice_key_physicalizer_lifecycle_lock:
            physicalizer = self._voice_key_physicalizer
            physicalizer_ready = self._voice_key_physicalizer_ready
        recorder = getattr(physicalizer, "record_rc003_direction_edge", None)
        if physicalizer_ready and callable(recorder):
            try:
                if recorder(vk, scan_code, extended, is_pressed):
                    return
            except Exception:
                self._logger.exception(
                    "RC003 global direction ownership failed; navigation fallback used"
                )
        element_navigation_control_windows.record_rc003_direction_edge(
            vk,
            scan_code,
            extended,
            is_pressed,
        )

    def _stop_input_channels(self) -> None:
        """Stop process-lifetime input resources exactly once at worker exit."""

        failures: List[str] = []
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_stopping = True
            self._voice_key_physicalizer_generation += 1
            self._voice_key_physicalizer_degraded_retry_deadline = 0.0
            self._cancel_voice_key_physicalizer_retry_locked()
        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                self._raw_input_stopping = True
                self._raw_input_generation += 1
                self._cancel_raw_input_retry_locked()
        with self._button_action_lock:
            self._button_input_release_retry_stopping = True
            self._cancel_button_input_release_retry_locked(reset_delay=False)
        with self._voice_shortcut.lock:
            self._voice_shortcut.retry_stopping = True
            self._voice_shortcut.cancel_release_retry(reset_delay=False)
            self._doubao_session.cancel_current()

        doubao_attempt_settled = self._doubao_session.wait_current(
            _DOUBAO_ATTEMPT_JOIN_TIMEOUT_SECONDS
        )
        if not doubao_attempt_settled:
            failures.append(
                "Doubao startup attempt did not settle; input owners retained"
            )

        # Input loss is cancellation, not a click. Clear gesture timers before
        # listener shutdown emits forced releases, and release any Windows key
        # whose original down was deliberately allowed through during fallback.
        self._button_combos.reset()
        self._button_gestures.reset()
        with self._input_arbitration_lock:
            self._cancel_all_raw_fallback_hold_guards_locked()
            tracked_logical_buttons, _tracked_physical_buttons = (
                self._raw_fallback_tracked_button_ids_locked()
            )
            raw_fallback_physical_buttons = (
                self._take_raw_fallback_physical_buttons_locked(
                    set(self._raw_fallback_buttons_down)
                    | tracked_logical_buttons
                )
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason="input_channels_stopping",
            )

        # Keep dispatch enabled until both listeners have emitted their forced
        # release edges. The HID tap is stopped first so its explicit disable
        # handshake restores the Windows path before Raw Input is closed.
        if self._hid_report_tap is not None:
            try:
                self._hid_report_tap.stop()
                self._hid_report_tap = None
                self._set_runtime_input_state(hid_tap_state="stopped")
            except Exception:
                self._set_runtime_input_state(hid_tap_state="failed_stopping")
                self._logger.exception(
                    "cleanup: stopping the RC003 HID report tap failed"
                )
                failures.append("RC003 HID report tap did not stop; owner retained")
        else:
            self._set_runtime_input_state(hid_tap_state="stopped")

        keyboard_release_complete = True
        if not self._release_pending_button_keys():
            keyboard_release_complete = False
            failures.append(
                "ordinary button key safety release did not fully deliver; "
                "keyboard trackers retained"
            )
        if not self._release_pending_button_mouse():
            failures.append(
                "ordinary button mouse safety release did not fully deliver; "
                "state retained"
            )

        with self._voice_shortcut.lock:
            if self._voice_shortcut.pending_tokens is not None:
                if self._voice_shortcut.release_pending():
                    self._voice_shortcut.controller.cancel_pending()
                else:
                    keyboard_release_complete = False
                    failures.append(
                        "voice hotkey final safety release did not fully deliver; "
                        "keyboard trackers retained"
                    )

        if keyboard_release_complete:
            with self._raw_input_operation_lock:
                listener = self._hid_listener
                if listener is not None:
                    try:
                        listener.stop()
                        with self._raw_input_lifecycle_lock:
                            if self._hid_listener is listener:
                                self._hid_listener = None
                                self._raw_fallback_tracking_active = False
                        self._set_runtime_input_state(raw_input_state="stopped")
                    except Exception:
                        self._set_runtime_input_state(
                            raw_input_state="failed_stopping"
                        )
                        self._logger.exception(
                            "cleanup: stopping the Raw Input listener failed"
                        )
                        failures.append(
                            "Raw Input listener did not stop; owner retained"
                        )
                else:
                    self._set_runtime_input_state(raw_input_state="stopped")
        else:
            self._set_runtime_input_state(raw_input_state="retained_for_cleanup")

        with self._voice_shortcut.lock:
            self._accept_input_events = False

        with self._direct_hid_lock:
            self._direct_hid_usages.clear()
        with self._input_arbitration_lock:
            self._direct_hid_interception_ready = False
            self._direct_hid_interception_armed = False
            self._direct_hid_handover_waiting_for_neutral = False
            self._cancel_all_raw_fallback_hold_guards_locked()
            self._raw_fallback_buttons_down.clear()
            self._raw_fallback_physical_buttons_down.clear()
            self._raw_fallback_release_debts.clear()
            self._input_rearm_blocked_buttons.clear()
        self._key_detection_suppressed_buttons.clear()
        self._key_detection_suppression_deadlines.clear()
        with self._ordinary_mic_lock:
            self._ordinary_mic_sources_down.clear()
            self._ordinary_mic_late_sources_down.clear()
            self._ordinary_mic_late_source_deadlines.clear()
            self._ordinary_mic_sources_seen.clear()
            self._ordinary_mic_gesture_started_at = 0.0
            self._ordinary_mic_release_guard_until = 0.0
            self._ordinary_mic_gesture_active = False
        with self._key_detection_mic_lock:
            self._reset_key_detection_mic_gesture_locked()
        self._button_combos.reset()
        self._button_gestures.reset()

        physicalizer_state = "stopped"
        if keyboard_release_complete and doubao_attempt_settled:
            try:
                self._voice_shortcut.doubao_physicalizer.stop()
            except Exception:
                keyboard_release_complete = False
                physicalizer_state = "failed"
                self._logger.exception(
                    "cleanup: stopping Doubao voice physicalizer failed"
                )
                failures.append(
                    "Doubao voice physicalizer did not stop; owner retained"
                )

        if keyboard_release_complete and doubao_attempt_settled:
            with self._voice_key_physicalizer_operation_lock:
                with self._voice_key_physicalizer_lifecycle_lock:
                    physicalizer = self._voice_key_physicalizer
                if physicalizer is not None:
                    try:
                        physicalizer.stop()
                    except Exception:
                        physicalizer_state = "failed"
                        self._logger.exception(
                            "cleanup: stopping voice key physicalizer failed"
                        )
                        failures.append(
                            "voice key physicalizer did not stop; owner retained"
                        )
                    else:
                        with self._voice_key_physicalizer_lifecycle_lock:
                            if self._voice_key_physicalizer is physicalizer:
                                self._voice_key_physicalizer = None
        else:
            physicalizer_state = "failed"
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_ready = False
        self._set_runtime_input_state(
            voice_key_physicalizer_state=physicalizer_state
        )

        if failures:
            raise CleanupIncompleteError(
                "cleanup could not release all owned input resources: "
                + "; ".join(failures)
            )

    async def _cleanup_once(self) -> None:
        """Release one BLE/voice/audio attempt without stopping input."""

        with self._runtime_status_lock:
            connection_state = self._runtime_connection_state
            self._runtime_battery_level = None
        if connection_state is bridge_runtime_status.BridgeConnectionState.CONNECTED:
            self._publish_runtime_status(
                bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
            )
        failures: List[str] = []
        with self._voice_shortcut.lock:
            self._accept_ble_events = False
            attempt = self._doubao_session.cancel_current()
            active_attempt = self._active_doubao_attempt_locked()
            cleanup_attempt = active_attempt
            if (
                cleanup_attempt is None
                and attempt is not None
                and (
                    attempt.has_cleanup_debt("hotkey")
                    or attempt.has_cleanup_debt("playback")
                )
            ):
                cleanup_attempt = attempt
            if cleanup_attempt is not None:
                self._voice_shortcut.controller.reset()
                self._voice_pcm_forwarding_enabled = False
                self._voice_pcm_min_arrival_sequence = None
                self._request_active_doubao_cleanup_locked(
                    cleanup_attempt,
                    reason="connection cleanup",
                )

        doubao_settled = await asyncio.to_thread(
            self._doubao_session.wait_current,
            _DOUBAO_ATTEMPT_JOIN_TIMEOUT_SECONDS,
        )

        try:
            with self._voice_shortcut.lock:
                doubao_cleanup_running = bool(
                    attempt is not None and attempt.cleanup_worker_running
                )
                self._voice_audio_start_fallback_pending = False
                self._voice_raw_input_trigger_pending = False
                self._voice_audio_stream_active = False
                self._voice_audio_stop_processed = False
                self._voice_pcm_forwarding_enabled = False
                self._voice_pcm_min_arrival_sequence = None
                if not doubao_cleanup_running:
                    flush_result = self._voice_audio.flush("cleanup")
                    if not flush_result.completed:
                        failures.append(
                            "audio playback queue did not flush; owner retained"
                        )
                self._unsolicited_mic_close_pending = False
                self._finish_voice_mic_gesture()
                if self._voice_shortcut.pending_tokens is not None:
                    doubao_release_owned = bool(
                        attempt is not None
                        and attempt.has_cleanup_debt("hotkey")
                        and self._voice_shortcut.pending_backend
                        == _VOICE_HOTKEY_BACKEND_DOUBAO
                    )
                    if doubao_release_owned:
                        self._request_active_doubao_cleanup_locked(
                            attempt,
                            reason="connection cleanup",
                        )
                    elif self._voice_shortcut.release_pending():
                        self._voice_shortcut.controller.cancel_pending()
                    else:
                        failures.append(
                            "voice hotkey safety release did not fully deliver; state retained"
                        )
                else:
                    reset_action = self._voice_shortcut.controller.reset()
                    if reset_action is not None and not self._voice_shortcut.apply(
                        reset_action
                    ):
                        # _apply_voice_action() already logged the specific failure.
                        # reset() already cleared the controller's own pending
                        # state before we knew delivery would fail - restore it so
                        # a held shortcut isn't recorded as released while it may
                        # still be physically down (XRBM-019 review round 1 P1 #4).
                        self._voice_shortcut.controller.restore_pending(reset_action)
                        failures.append(
                            "voice hotkey release did not fully deliver; state retained"
                        )
                self._voice_focus_before = None
                self._voice_focus_provider = ""
                self._voice_focus_submit_method = ""
                self._finish_voice_diagnostic_attempt(
                    "unknown",
                    reason="connection_cleanup",
                )
        except Exception:
            self._logger.exception("cleanup: releasing the voice hotkey failed")
            failures.append("voice hotkey cleanup failed; state retained")

        if not self._release_pending_button_inputs():
            failures.append(
                "ordinary button input safety release did not fully deliver; "
                "state retained"
            )

        if self._ble_session is not None:
            try:
                await self._ble_session.close()
                self._ble_session = None
            except Exception:
                self._logger.exception("cleanup: closing the BLE session failed")
                failures.append("BLE session did not fully close; owner retained")
                # self._ble_session is intentionally NOT cleared here either.

        doubao_cleanup_running = bool(
            attempt is not None and attempt.cleanup_worker_running
        )
        playback_writer_stopped = not doubao_cleanup_running
        if doubao_cleanup_running:
            failures.append(
                "Doubao asynchronous cleanup still owns audio resources"
            )
        elif self._voice_audio.writer is not None:
            playback_writer_stopped = self._voice_audio.stop_writer()
            if playback_writer_stopped:
                if attempt is not None:
                    attempt.resolve_cleanup_debt("playback")
            else:
                failures.append(
                    "audio playback writer did not stop; owner retained"
                )

        if self._voice_audio.sink is not None and playback_writer_stopped:
            playback = self._voice_audio.sink
            try:
                self._voice_audio.close_sink()
                if attempt is not None:
                    if attempt.release_resource("endpoint", playback):
                        attempt.resolve_cleanup_debt("endpoint")
            except Exception:
                self._logger.exception("cleanup: closing audio playback failed")
                failures.append("audio playback did not fully close; owner retained")
                # self._voice_audio.sink is intentionally NOT cleared here either -
                # it owns a PortAudio stream; discarding the reference would
                # hide an incompletely closed resource and let a reconnect
                # open a second sink over it (XRBM-019 review round 1 P1
                # #5).

        if attempt is not None and not attempt.settled.is_set():
            failures.append(
                "Doubao attempt cleanup remains unsettled; owner retained"
            )
        elif not doubao_settled and attempt is not None:
            self._logger.info(
                "Doubao attempt settled during independent cleanup"
            )

        if not failures:
            with self._voice_shortcut.lock:
                self._apply_pending_voice_settings_if_idle_locked()

        self._logger.info("cleanup: attempted release of hotkey state and BLE/HID/audio")

        if failures:
            raise CleanupIncompleteError(
                "cleanup could not release all owned resources: " + "; ".join(failures)
            )

    # -- disconnect / error callbacks: hand off to the supervisor ----------

    def _on_disconnected(self, *, _ble_generation: Optional[int] = None) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        self._logger.info("BLE reported disconnected; requesting reconnect")
        self._supervisor.request_reconnect()

    def _on_session_error(
        self,
        exc: BaseException,
        *,
        _ble_generation: Optional[int] = None,
    ) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.RETRY_WAIT
        )
        self._logger.info("ATVV protocol error, requesting reconnect: %s", exc)
        self._supervisor.request_reconnect()

    def _on_battery_level(
        self,
        level: int,
        *,
        _ble_generation: Optional[int] = None,
    ) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        self._set_runtime_battery_level(level)

    def _accepts_ble_callback(self, generation: Optional[int]) -> bool:
        """Reject callbacks left behind by an older BLE session."""

        return self._accept_ble_events and (
            generation is None or generation == self._ble_callback_generation
        )

    def _primary_button_action(self, button_id: str) -> key_mapping.ButtonAction:
        return key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK,
        )

    def _configured_voice_buttons(self) -> List[str]:
        button_bindings = self._bindings.get("bindings", {})
        if not isinstance(button_bindings, dict):
            return []
        result = []
        for button_id in button_bindings:
            action = self._primary_button_action(button_id)
            if button_id == "mic" and key_mapping.is_supported_voice_action(action):
                result.append(button_id)
        return result

    def _voice_mode_for_primary_button(
        self,
        button_id: str,
        action: Optional[key_mapping.ButtonAction] = None,
    ) -> Optional[key_mapping.VoiceTriggerMode]:
        action = action or self._primary_button_action(button_id)
        if button_id != "mic" or not key_mapping.is_supported_voice_action(action):
            return None
        return key_mapping.VoiceTriggerMode.HOLD

    def _voice_hotkey_text_for_mode(
        self, mode: key_mapping.VoiceTriggerMode
    ) -> str:
        mode_hotkeys = self._config.get("voice_hotkeys", {})
        if isinstance(mode_hotkeys, dict):
            candidate = str(mode_hotkeys.get(mode.value, "")).strip()
            if candidate:
                return candidate
        if self._config.get("voice_trigger_mode") == mode.value:
            candidate = str(self._config.get("voice_hotkey", "")).strip()
            if candidate:
                return candidate
        return key_mapping.voice_hotkey_for_trigger_mode(mode)

    def _configured_voice_hotkey_backend(self) -> str:
        settings = voice_program_manager.normalize_voice_program_settings(
            self._config.get("voice_program")
        )
        if settings["provider"] == voice_program_manager.VOICE_PROGRAM_WETYPE:
            return _VOICE_HOTKEY_BACKEND_WETYPE
        if settings["provider"] == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME:
            return _VOICE_HOTKEY_BACKEND_DOUBAO
        return _VOICE_HOTKEY_BACKEND_MARKED

    def _ensure_voice_key_physicalizer_for_hotkey(
        self,
        tokens: Tuple[str, ...],
        backend: str,
    ) -> bool:
        """Start the process-level right-Alt tracker only when it is needed."""

        if backend not in {
            _VOICE_HOTKEY_BACKEND_MARKED,
            _VOICE_HOTKEY_BACKEND_DOUBAO,
        }:
            return True
        try:
            uses_right_alt = (
                win32_keys.VK_CODES["ralt"]
                in win32_keys.resolve_vk_codes(tokens)
            )
        except win32_keys.UnknownKeyTokenError:
            return False
        if not uses_right_alt:
            return True
        with self._voice_key_physicalizer_lifecycle_lock:
            ready = self._voice_key_physicalizer_ready
            physicalizer = self._voice_key_physicalizer
            accepts_new_down = bool(
                physicalizer is not None
                and getattr(physicalizer, "accepts_new_down", True)
            )
            if ready and not accepts_new_down:
                self._voice_key_physicalizer_ready = False
                ready = False
                if not self._voice_key_physicalizer_degraded_retry_deadline:
                    self._voice_key_physicalizer_degraded_retry_deadline = (
                        time.monotonic()
                        + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
                    )
                self._schedule_voice_key_physicalizer_recovery_locked()
        if not ready:
            self._start_voice_key_physicalizer()
        with self._voice_key_physicalizer_lifecycle_lock:
            physicalizer = self._voice_key_physicalizer
            return bool(
                self._voice_key_physicalizer_ready
                and physicalizer is not None
                and getattr(physicalizer, "accepts_new_down", True)
            )

    def _prepare_voice_mapping_locked(
        self,
        button_id: str,
        action: key_mapping.ButtonAction,
    ) -> bool:
        issue = voice_program_manager.voice_configuration_issue(self._config.get("voice_program"))
        if issue:
            self._logger.warning("voice mapping ignored: %s", issue)
            return False
        if not self._direct_hid_interception_ready:
            self._logger.warning(
                "voice mapping ignored: RC003 HID interception is not ready"
            )
            return False
        if self._voice_mode_for_primary_button(button_id, action) is None:
            self._logger.warning(
                "voice mapping ignored: only the physical microphone button "
                "supports hold-to-talk; button=%s configured=%s",
                button_id,
                self._configured_voice_buttons(),
            )
            return False
        mode = key_mapping.VoiceTriggerMode.HOLD
        backend = self._configured_voice_hotkey_backend()
        if backend == _VOICE_HOTKEY_BACKEND_WETYPE:
            return True
        try:
            voice_hotkey = hotkey.HotkeySpec.parse(
                self._voice_hotkey_text_for_mode(mode)
            )
            tokens = tuple(voice_hotkey.modifiers) + (voice_hotkey.key,)
            resolved_vk_codes = win32_keys.resolve_vk_codes(tokens)
        except (hotkey.HotkeyParseError, win32_keys.UnknownKeyTokenError) as exc:
            self._logger.warning(
                "voice mapping ignored: invalid %s shortcut: %s",
                mode.value,
                exc,
            )
            return False
        if not win32_input.can_begin_tracked_hold(tokens):
            self._logger.warning(
                "voice mapping ignored: physical keyboard tracking is unavailable "
                "for shortcut=%s",
                voice_hotkey.serialize(),
            )
            return False
        if (
            backend
            in {_VOICE_HOTKEY_BACKEND_MARKED, _VOICE_HOTKEY_BACKEND_DOUBAO}
            and win32_keys.VK_CODES["ralt"] in resolved_vk_codes
            and not self._voice_key_physicalizer_ready
        ):
            self._logger.warning(
                "voice mapping ignored: marked right-Alt physicalizer is unavailable"
            )
            return False
        requested = (mode, voice_hotkey.serialize())
        current = (self._voice_shortcut.controller.trigger_mode, self._voice_shortcut.hotkey.serialize())
        if requested != current:
            if not self._voice_settings_idle_locked():
                self._logger.info(
                    "voice mapping change deferred: active session owns %s/%s",
                    current[0].value,
                    current[1],
                )
                return False
            self._apply_voice_settings_locked(mode, voice_hotkey)
        return True

    def _begin_voice_mic_gesture(
        self, source: str, *, physical_down: bool = False
    ) -> bool:
        """Claim one physical mic press for its first arriving event source.

        The caller holds ``_voice_trigger_lock``. Later sources are recorded
        for release tracking but must not toggle the host voice state again.
        """

        if self._voice_mic_gesture_active:
            if physical_down:
                self._voice_mic_gesture_physical_seen = True
                self._voice_mic_gesture_sources_down.add(source)
                if source == "hid_tap":
                    self._voice_mic_gesture_direct_hid_seen = True
            return False
        self._voice_mic_gesture_active = True
        self._voice_mic_gesture_audio_started = source == "audio_started"
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = physical_down
        self._voice_mic_gesture_hid_released = False
        self._voice_mic_gesture_direct_hid_seen = (
            physical_down and source == "hid_tap"
        )
        self._voice_mic_gesture_sources_down = {source} if physical_down else set()
        return True

    def _finish_voice_mic_gesture(self) -> None:
        """Release the current cross-source mic gesture latch."""

        self._cancel_voice_hold_watchdog_locked()
        self._voice_mic_gesture_active = False
        self._voice_mic_gesture_audio_started = False
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = False
        self._voice_mic_gesture_hid_released = False
        self._voice_mic_gesture_direct_hid_seen = False
        self._voice_mic_gesture_sources_down.clear()

    def _cancel_voice_hold_watchdog_locked(self) -> None:
        timer = self._voice_hold_watchdog_timer
        self._voice_hold_watchdog_timer = None
        self._voice_hold_watchdog_token = None
        if timer is not None:
            cancel = getattr(timer, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except BaseException as exc:  # noqa: BLE001 - token is already invalid
                    self._logger.warning(
                        "voice hold safety timer cancel failed: error_type=%s",
                        type(exc).__name__,
                    )

    def _arm_voice_hold_watchdog_locked(self) -> bool:
        self._cancel_voice_hold_watchdog_locked()
        token = object()
        try:
            timer = self._voice_hold_watchdog_timer_factory(
                _VOICE_HOLD_SAFETY_SECONDS,
                lambda token=token: self._voice_hold_watchdog_expired(token),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._voice_hold_watchdog_token = token
            self._voice_hold_watchdog_timer = timer
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - never leave the host key held
            if self._voice_hold_watchdog_token is token:
                self._cancel_voice_hold_watchdog_locked()
            self._logger.error(
                "voice hold safety timer failed to start: error_type=%s",
                type(exc).__name__,
            )
            return False
        return True

    def _force_voice_hold_release_locked(
        self,
        reason: str,
        *,
        reconnect: bool = True,
    ) -> bool:
        if reconnect:
            # The watchdog owns the end of the current BLE attempt. Invalidate
            # every callback already queued by that session before releasing
            # the host key; reconnect publishes a fresh generation.
            self._accept_ble_events = False
            self._ble_callback_generation += 1
        self._voice_pcm_forwarding_enabled = False
        self._voice_pcm_min_arrival_sequence = None
        self._voice_audio_stream_active = False
        self._voice_audio_stop_processed = True
        self._voice_raw_input_trigger_pending = False
        self._voice_audio_start_fallback_pending = False
        self._voice_mic_gesture_sources_down.clear()
        self._voice_mic_gesture_hid_released = True

        doubao_attempt = self._active_doubao_attempt_locked()
        action = self._voice_shortcut.controller.reset()
        released = True
        doubao_cleanup_requested = False
        if doubao_attempt is not None:
            released = self._request_active_doubao_cleanup_locked(
                doubao_attempt,
                reason=reason,
            )
            doubao_cleanup_requested = released
            if released:
                self._set_runtime_voice_result(
                    bridge_runtime_status.VOICE_RUNTIME_FINISHING,
                    provider=voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
                )
        elif action is not None:
            released = self._voice_shortcut.apply(action)
            if not released:
                self._voice_shortcut.controller.restore_pending(action)
        elif self._voice_shortcut.pending_tokens is not None:
            released = self._voice_shortcut.release_pending()

        self._finish_voice_mic_gesture()
        if released and doubao_cleanup_requested:
            self._set_runtime_voice_active(False)
        elif released:
            self._set_runtime_voice_active(False)
            self._log_voice_submission_observation()
            self._finish_voice_diagnostic_attempt(
                "unknown",
                reason="forced_host_release",
            )
            self._apply_pending_voice_settings_if_idle_locked()
        else:
            self._logger.error(
                "%s; %s will retry the host release",
                reason,
                "reconnect cleanup" if reconnect else "the safety timer",
            )
        try:
            if self._ble_session is not None:
                self._ble_session.send_mic_close_threadsafe()
        except BaseException as exc:  # noqa: BLE001 - reconnect must still run
            self._logger.warning(
                "voice hold safety MIC_CLOSE failed: error_type=%s",
                type(exc).__name__,
            )
        finally:
            if reconnect:
                self._supervisor.request_reconnect()
        return released

    def _voice_hold_watchdog_expired(self, token: object) -> None:
        with self._voice_shortcut.lock:
            if self._voice_hold_watchdog_token is not token:
                return
            self._voice_hold_watchdog_timer = None
            self._voice_hold_watchdog_token = None
            if not self._voice_shortcut.controller.active and self._voice_shortcut.pending_tokens is None:
                return

            self._force_voice_hold_release_locked("voice hold safety release failed")
            self._logger.warning(
                "voice hold exceeded %.0fs; forced host release and reconnect",
                _VOICE_HOLD_SAFETY_SECONDS,
            )

    def _rollover_completed_voice_mic_gesture_locked(self, next_source: str) -> bool:
        """Detach stale sources once a matched HID release proves a new press."""

        if not (
            self._voice_mic_gesture_active
            and self._voice_mic_gesture_audio_stopped
            and not self._voice_shortcut.controller.active
            and self._voice_mic_gesture_hid_released
        ):
            return False
        stale_sources = sorted(self._voice_mic_gesture_sources_down)
        self._finish_voice_mic_gesture()
        self._logger.info(
            "voice completed gesture rolled over for new source=%s; "
            "detached late duplicate sources=%s",
            next_source,
            stale_sources,
        )
        return True

    def _release_hold_voice_on_physical_release_locked(
        self,
        reason: str,
    ) -> bool:
        """Release HOLD shortcuts without depending solely on AUDIO_STOP."""

        self._doubao_session.cancel_current()
        action = self._voice_shortcut.controller.on_mic_button_released()
        if action is None:
            self._cancel_voice_hold_watchdog_locked()
            return True
        self._voice_raw_input_trigger_pending = False
        self._voice_audio_start_fallback_pending = False
        self._voice_pcm_forwarding_enabled = False
        self._voice_pcm_min_arrival_sequence = None
        doubao_attempt = self._active_doubao_attempt_locked()
        if doubao_attempt is not None:
            accepted = self._request_active_doubao_cleanup_locked(
                doubao_attempt,
                reason=reason,
            )
            if accepted:
                self._cancel_voice_hold_watchdog_locked()
                self._set_runtime_voice_result(
                    bridge_runtime_status.VOICE_RUNTIME_FINISHING,
                    provider=voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
                )
            return accepted
        if self._voice_shortcut.apply(action):
            self._cancel_voice_hold_watchdog_locked()
            self._logger.info("voice hold hotkey released on %s", reason)
            if not self._voice_audio_stream_active:
                self._set_runtime_voice_active(False)
                self._set_runtime_voice_result(
                    bridge_runtime_status.VOICE_RUNTIME_AUDIO_EMPTY
                )
                self._log_voice_submission_observation()
                self._finish_voice_diagnostic_attempt(
                    "unknown",
                    reason="physical_release_without_audio_stop",
                    audio_signal=False,
                )
            return True

        self._voice_shortcut.controller.restore_pending(action)
        self._set_runtime_voice_result(
            bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED
        )
        self._logger.info(
            "voice hold hotkey release failed on %s; state retained, "
            "requesting reconnect",
            reason,
        )
        self._supervisor.request_reconnect()
        return False

    def _active_doubao_attempt_locked(
        self,
    ) -> Optional[rc003_doubao_session.DoubaoAttempt]:
        attempt = self._doubao_session.current
        if (
            self._remote_profile == remote_selection.RC003_PROFILE
            and attempt is not None
            and attempt.is_active()
        ):
            return attempt
        return None

    def _request_active_doubao_cleanup_locked(
        self,
        attempt: rc003_doubao_session.DoubaoAttempt,
        *,
        reason: str,
    ) -> bool:
        if attempt.has_cleanup_debt("active_session"):
            attempt.replace_cleanup_debt(
                "active_session",
                "hotkey",
                "playback",
            )
        self._voice_shortcut.pending_tokens = attempt.snapshot.tokens
        self._voice_shortcut.pending_backend = _VOICE_HOTKEY_BACKEND_DOUBAO
        self._voice_shortcut.active_backend = _VOICE_HOTKEY_BACKEND_DOUBAO
        started = self._doubao_session.request_cleanup(
            attempt,
            lambda owned: self._run_active_doubao_cleanup(owned, reason=reason),
        )
        if not started and not attempt.cleanup_worker_running:
            self._voice_shortcut.schedule_release_retry()
            self._supervisor.request_reconnect()
        return started or attempt.cleanup_worker_running

    def _run_active_doubao_cleanup(
        self,
        attempt: rc003_doubao_session.DoubaoAttempt,
        *,
        reason: str,
    ) -> None:
        debts = attempt.cleanup_debt_snapshot()
        generation = self._voice_shortcut.control_generation(
            _VOICE_HOTKEY_BACKEND_DOUBAO
        )
        if "playback" in debts:
            writer = self._voice_audio.writer
            if writer is None:
                attempt.resolve_cleanup_debt("playback")
            else:
                try:
                    result = writer.flush()
                except Exception:
                    result = None
                    self._logger.exception(
                        "Doubao playback flush raised during %s",
                        reason,
                    )
                if result is not None and result.completed:
                    attempt.resolve_cleanup_debt("playback")
                else:
                    self._logger.error(
                        "Doubao playback flush failed during %s: %s",
                        reason,
                        (
                            result.error
                            if result is not None and result.error
                            else "unknown error"
                        ),
                    )
                    self._supervisor.request_reconnect()

        released = True
        if "hotkey" in debts:
            try:
                expected_markers = len(
                    tuple(
                        dict.fromkeys(
                            win32_keys.resolve_vk_codes(attempt.snapshot.tokens)
                        )
                    )
                )
            except win32_keys.UnknownKeyTokenError:
                expected_markers = 0
            self._voice_shortcut.doubao_physicalizer.expect_markers("up", expected_markers)
            trace_context = {
                **self._diagnostic_trace.current_context(),
                "action": voice_controller.VoiceHostAction.KEY_UP.value,
            }
            self._diagnostic_trace.emit(
                "voice_hotkey_requested",
                **trace_context,
                backend=_VOICE_HOTKEY_BACKEND_DOUBAO,
                tokens=list(attempt.snapshot.tokens),
                source="rc003_doubao_session",
            )
            try:
                released = bool(self._voice_shortcut.doubao_control.stop())
            except Exception:
                released = False
                self._logger.exception(
                    "Doubao asynchronous voice release failed"
                )
            self._diagnostic_trace.emit(
                "voice_hotkey_result",
                **trace_context,
                backend=_VOICE_HOTKEY_BACKEND_DOUBAO,
                delivered=bool(released),
                generation=int(generation) if generation is not None else -1,
                cleanup_pending=bool(
                    self._voice_shortcut.control_flag(
                        "cleanup_pending", _VOICE_HOTKEY_BACKEND_DOUBAO
                    )
                ),
            )

        with self._voice_shortcut.lock:
            if released and "hotkey" in debts:
                if (
                    self._voice_shortcut.pending_tokens
                    == attempt.snapshot.tokens
                    and self._voice_shortcut.pending_backend
                    == _VOICE_HOTKEY_BACKEND_DOUBAO
                ):
                    self._voice_shortcut.pending_tokens = None
                    self._voice_shortcut.pending_backend = None
                    self._voice_shortcut.active_backend = None
                self._voice_shortcut.cancel_release_retry()
                attempt.resolve_cleanup_debt("hotkey")
            elif not released:
                self._voice_shortcut.schedule_release_retry()
                self._supervisor.request_reconnect()

            cleanup_succeeded = not (
                attempt.has_cleanup_debt("hotkey")
                or attempt.has_cleanup_debt("playback")
            )

        if generation is not None:
            self._voice_shortcut.on_cleanup(
                generation,
                cleanup_succeeded,
                _VOICE_HOTKEY_BACKEND_DOUBAO,
            )

        with self._voice_shortcut.lock:
            if cleanup_succeeded:
                self._set_runtime_voice_active(False)
                if not self._voice_audio_stream_active:
                    stats = self._voice_audio.stats.summary()
                    self._voice_shortcut.record_audio_result(int(stats["frames"]))
                    self._finish_voice_mic_gesture()
                    self._finish_doubao_diagnostic_after_cleanup_locked(
                        reason="asynchronous_release_complete",
                    )
                self._apply_pending_voice_settings_if_idle_locked()
        if cleanup_succeeded:
            self._resume_degraded_voice_key_physicalizer_recovery()

    def _finish_doubao_diagnostic_after_cleanup_locked(
        self,
        *,
        reason: str,
    ) -> None:
        if self._voice_attempt_id is None:
            return
        stats = self._voice_audio.stats.summary()
        observation = self._log_voice_submission_observation()
        text_changed = bool(
            observation is not None
            and observation.text_delta is not None
            and observation.text_delta > 0
        )
        if text_changed:
            result = "text_changed"
        elif self._voice_shortcut.ui_confirmation == "confirmed" and stats["frames"] > 0:
            result = "host_confirmed_audio_ok"
        else:
            result = "unknown"
        self._finish_voice_diagnostic_attempt(
            result,
            frames=int(stats["frames"]),
            samples=int(stats["samples"]),
            audio_signal=bool(stats["frames"] > 0),
            reason=reason,
        )

    def _reset_key_detection_mic_gesture_locked(self) -> None:
        self._key_detection_mic_gesture_active = False
        self._key_detection_mic_gesture_started_at = 0.0
        self._key_detection_mic_release_deadline = None
        self._key_detection_mic_audio_started = False
        self._key_detection_mic_sources_down.clear()

    def _expire_key_detection_mic_gesture_locked(self, now: float) -> None:
        if not self._key_detection_mic_gesture_active:
            return
        release_deadline = self._key_detection_mic_release_deadline
        hard_deadline = (
            self._key_detection_mic_gesture_started_at
            + _KEY_DETECTION_MIC_MAX_SECONDS
        )
        if (release_deadline is not None and now >= release_deadline) or (
            now >= hard_deadline
        ):
            self._reset_key_detection_mic_gesture_locked()

    def _handle_key_detection_mic_event(
        self,
        event_kind: str,
        source: str,
    ) -> Tuple[bool, bool]:
        """Capture or suppress one source from a detected physical mic press.

        A single RC003 mic gesture is reported by multiple independent paths.
        The key-detection request disappears as soon as the first path claims
        it, so a short in-process latch must keep later paths from entering the
        normal voice state machine. Returns ``(handled, newly_captured)``.
        """

        now = time.monotonic()
        starts_gesture = event_kind in {
            "physical_down",
            "atvv_press",
            "audio_started",
        }
        with self._voice_shortcut.lock:
            # A detection request may appear while a real voice press is still
            # active. Its late HID/F5/audio edges belong to that owned press and
            # must remain available to close the host shortcut. Leave the
            # request pending for the next independent press instead.
            if self._voice_mic_gesture_active:
                return False, False
            with self._key_detection_mic_lock:
                self._expire_key_detection_mic_gesture_locked(now)
                newly_captured = False
                if not self._key_detection_mic_gesture_active:
                    if not starts_gesture:
                        return False, False
                    try:
                        newly_captured = key_detection_bridge.publish_next_button(
                            self._config_root,
                            "mic",
                        )
                    except OSError as exc:
                        self._logger.warning("key detection IPC unavailable: %s", exc)
                        return False, False
                    if not newly_captured:
                        return False, False
                    self._key_detection_mic_gesture_active = True
                    self._key_detection_mic_gesture_started_at = now

                if event_kind == "physical_down":
                    self._key_detection_mic_sources_down.add(source)
                    self._key_detection_mic_release_deadline = None
                elif event_kind == "physical_up":
                    self._key_detection_mic_sources_down.discard(source)
                    if (
                        not self._key_detection_mic_sources_down
                        and not self._key_detection_mic_audio_started
                    ):
                        self._key_detection_mic_release_deadline = (
                            now + _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS
                        )
                elif event_kind == "audio_started":
                    self._key_detection_mic_audio_started = True
                    self._key_detection_mic_release_deadline = None
                elif event_kind == "audio_stopped":
                    self._key_detection_mic_audio_started = False
                    if not self._key_detection_mic_sources_down:
                        self._key_detection_mic_release_deadline = (
                            now + _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS
                        )
                elif not self._key_detection_mic_sources_down:
                    # MicButtonPressed has no matching release opcode. Give the
                    # physical/audio paths time to join, then let a future press
                    # through even if neither companion event ever arrives.
                    self._key_detection_mic_release_deadline = (
                        now + _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS
                    )
                return True, newly_captured

    def _on_raw_button_event(
        self,
        button_id: str,
        is_pressed: bool,
        source: str = "unknown",
        windows_button_id: Optional[str] = None,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        event_source = {
            "keyboard": "raw_keyboard",
            "hid": "raw_hid",
        }.get(source, "raw_unknown")
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(_listener, _generation):
                return
            self._on_button_event_serialized(
                button_id,
                is_pressed,
                event_source=event_source,
                raw_windows_button_id=windows_button_id,
            )

    # -- HID button events --------------------------------------------------

    @staticmethod
    def _settings_file_mtime_ns(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1

    def _voice_settings_idle_locked(self) -> bool:
        chromecast_host = self._chromecast_runtime.voice_host
        return not (
            self._voice_shortcut.controller.active
            or self._doubao_session.busy
            or self._voice_mic_gesture_active
            or self._voice_audio_stream_active
            or self._ordinary_mic_gesture_active
            or self._voice_shortcut.pending_tokens is not None
            or (
                chromecast_host is not None
                and getattr(chromecast_host, "_settings_claimed", False)
            )
        )

    def _ordinary_button_mappings_idle(self) -> bool:
        return not (
            self._button_gestures.has_active_gestures()
            or self._button_combos.has_active_combo()
        )

    def _apply_voice_settings_locked(
        self,
        trigger_mode: key_mapping.VoiceTriggerMode,
        voice_hotkey: hotkey.HotkeySpec,
    ) -> None:
        if trigger_mode != key_mapping.VoiceTriggerMode.HOLD:
            raise ValueError("RC003 voice settings support hold-to-talk only")
        self._voice_shortcut.controller = voice_controller.VoiceController()
        self._voice_shortcut.hotkey = voice_hotkey
        self._config["voice_trigger_mode"] = trigger_mode.value
        self._config["voice_hotkey"] = voice_hotkey.serialize()
        self._logger.info(
            "settings voice configuration applied: trigger_mode=%s hotkey=%s",
            trigger_mode.value,
            voice_hotkey.serialize(),
        )

    def _apply_pending_voice_settings_if_idle_locked(self) -> None:
        if not self._voice_settings_idle_locked():
            return
        if self._pending_config is not None:
            self._config = self._pending_config
            self._pending_config = None
        if self._pending_voice_settings is not None:
            trigger_mode, voice_hotkey = self._pending_voice_settings
            self._pending_voice_settings = None
            self._apply_voice_settings_locked(trigger_mode, voice_hotkey)
        if (
            self._pending_bindings is not None
        ):
            with self._button_mapping_lock:
                if self._ordinary_button_mappings_idle():
                    self._button_combos.reset()
                    self._bindings = self._pending_bindings
                    self._pending_bindings = None
                    self._sync_physical_bindings_to_listener()
                    self._removed_voice_bindings = dict(
                        self._bindings.get(
                            config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {}
                        )
                    )
                    self._logger.info(
                        "deferred settings mappings applied after active input became idle"
                    )
                    self._diagnostic_trace.emit("voice_configuration_applied", result="recovered")

    def _apply_pending_settings_if_idle(self) -> None:
        with self._voice_shortcut.lock:
            self._apply_pending_voice_settings_if_idle_locked()

    def _reload_settings_if_changed(self) -> None:
        """Apply mapping and voice-setting edits without a bridge restart."""

        current_config_mtime_ns = self._settings_file_mtime_ns(self._config_path)
        current_bindings_mtime_ns = self._settings_file_mtime_ns(self._bindings_path)
        if (
            current_config_mtime_ns == self._config_mtime_ns
            and current_bindings_mtime_ns == self._bindings_mtime_ns
        ):
            return
        try:
            refreshed_config = config.load_config(self._config_path)
            from . import remote_selection
            if remote_selection.active_key(refreshed_config) != remote_selection.active_key(self._config):
                raise remote_selection.SelectionError("设备选择已变化，须先停止旧服务再切换。")
            refreshed_bindings = config.load_key_bindings(self._bindings_path)
            removed_voice_bindings = config.normalize_voice_product_boundary(
                refreshed_config,
                refreshed_bindings,
            )
            trigger_mode = key_mapping.VoiceTriggerMode.HOLD
            refreshed_hotkey_text = _runtime_voice_hotkey_text(refreshed_config)
            voice_hotkey = hotkey.HotkeySpec.parse(refreshed_hotkey_text)
        except Exception as exc:  # noqa: BLE001 - keep the last valid settings
            self._logger.warning("settings reload skipped: %s", exc)
            with self._voice_shortcut.lock:
                self._config_mtime_ns = current_config_mtime_ns
                self._bindings_mtime_ns = current_bindings_mtime_ns
            return

        self._diagnostic_trace.set_enabled(
            bool(refreshed_config.get("diagnostic_trace_enabled", False))
        )
        self._record_device_diagnostic_context()
        with self._voice_shortcut.lock:
            with self._button_mapping_lock:
                refreshed_settings = (trigger_mode, voice_hotkey.serialize())
                current_settings = (
                    self._voice_shortcut.controller.trigger_mode,
                    self._voice_shortcut.hotkey.serialize(),
                )
                if self._voice_settings_idle_locked():
                    self._config = refreshed_config
                    self._pending_config = None
                    self._pending_voice_settings = None
                    if refreshed_settings != current_settings:
                        self._apply_voice_settings_locked(trigger_mode, voice_hotkey)
                    if self._ordinary_button_mappings_idle():
                        self._button_combos.reset()
                        self._bindings = refreshed_bindings
                        self._sync_physical_bindings_to_listener()
                        self._removed_voice_bindings = removed_voice_bindings
                        self._pending_bindings = None
                    else:
                        self._pending_bindings = refreshed_bindings
                        self._logger.info(
                            "settings mapping reload deferred until active input is idle"
                        )
                else:
                    self._pending_config = refreshed_config
                    self._pending_bindings = refreshed_bindings
                    self._pending_voice_settings = (trigger_mode, voice_hotkey)
                    self._logger.info(
                        "settings reload deferred until active voice session is idle"
                    )
                    self._diagnostic_trace.emit("voice_configuration_deferred",
                        release_pending=self._voice_shortcut.pending_tokens is not None,
                        reason="active_voice_not_idle", **self._diagnostic_trace.current_context())
                self._config_mtime_ns = current_config_mtime_ns
                self._bindings_mtime_ns = current_bindings_mtime_ns
        if removed_voice_bindings:
            self._logger.warning(
                "legacy voice mappings disabled until user reselects actions: %s",
                sorted(removed_voice_bindings),
            )
        if self._pending_bindings is None:
            self._logger.info("settings reloaded from disk")

    def request_settings_reload_now(self) -> None:
        """Apply saved settings promptly from the bridge event-loop thread."""

        self._event_loop.call_soon_threadsafe(self._reload_settings_if_changed)

    def _handle_ordinary_mic_edge(
        self, event_source: str, is_pressed: bool
    ) -> None:
        """Collapse F5/HID duplicates before ordinary mic gesture dispatch."""

        dispatch = False
        cancel_stale_gesture = False
        now = time.monotonic()
        with self._ordinary_mic_lock:
            expired_late_sources = {
                source
                for source, deadline in self._ordinary_mic_late_source_deadlines.items()
                if now >= deadline
            }
            if expired_late_sources:
                self._ordinary_mic_late_sources_down.difference_update(
                    expired_late_sources
                )
                for source in expired_late_sources:
                    self._ordinary_mic_late_source_deadlines.pop(source, None)

            stale_active_gesture = self._ordinary_mic_gesture_active and (
                now - self._ordinary_mic_gesture_started_at
                >= _ORDINARY_MIC_MAX_SECONDS
                or (
                    is_pressed
                    and now - self._ordinary_mic_gesture_started_at
                    >= _ORDINARY_MIC_SOURCE_STALE_SECONDS
                )
            )
            if stale_active_gesture:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
                cancel_stale_gesture = True

            if is_pressed:
                if (
                    event_source in self._ordinary_mic_sources_down
                    or event_source in self._ordinary_mic_late_sources_down
                ):
                    return
                if self._ordinary_mic_sources_down:
                    self._ordinary_mic_sources_down.add(event_source)
                    self._ordinary_mic_sources_seen.add(event_source)
                    return
                now = time.monotonic()
                if (
                    now < self._ordinary_mic_release_guard_until
                    and event_source not in self._ordinary_mic_sources_seen
                ):
                    self._ordinary_mic_late_sources_down.add(event_source)
                    self._ordinary_mic_late_source_deadlines[event_source] = (
                        now + _ORDINARY_MIC_SOURCE_STALE_SECONDS
                    )
                    self._ordinary_mic_sources_seen.add(event_source)
                    self._logger.info(
                        "ordinary mic late duplicate ignored: source=%s",
                        event_source,
                    )
                    return
                self._ordinary_mic_sources_down.add(event_source)
                self._ordinary_mic_sources_seen = {event_source}
                self._ordinary_mic_gesture_started_at = now
                self._ordinary_mic_release_guard_until = 0.0
                dispatch = True
            else:
                if event_source in self._ordinary_mic_late_sources_down:
                    self._ordinary_mic_late_sources_down.discard(event_source)
                    self._ordinary_mic_late_source_deadlines.pop(event_source, None)
                    return
                if event_source not in self._ordinary_mic_sources_down:
                    return
                self._ordinary_mic_sources_down.discard(event_source)
                dispatch = not self._ordinary_mic_sources_down
                if dispatch:
                    self._ordinary_mic_release_guard_until = (
                        now + _ORDINARY_MIC_RELEASE_GUARD_SECONDS
                    )
                    self._ordinary_mic_gesture_started_at = 0.0
            self._ordinary_mic_gesture_active = bool(
                self._ordinary_mic_sources_down
            )
        if cancel_stale_gesture:
            self._button_gestures.reset()
        if not dispatch:
            return
        with self._button_mapping_lock:
            if is_pressed:
                self._button_gestures.press("mic")
            else:
                self._button_gestures.release("mic")

    def _on_button_event(
        self,
        button_id: str,
        is_pressed: bool,
        *,
        event_source: str = "hid",
        raw_windows_button_id: Optional[str] = None,
        hid_usage: Optional[int] = None,
    ) -> None:
        with self._input_arbitration_lock:
            self._on_button_event_serialized(
                button_id,
                is_pressed,
                event_source=event_source,
                raw_windows_button_id=raw_windows_button_id,
                hid_usage=hid_usage,
            )

    def _on_button_event_serialized(
        self,
        button_id: str,
        is_pressed: bool,
        *,
        event_source: str,
        raw_windows_button_id: Optional[str] = None,
        hid_usage: Optional[int] = None,
    ) -> None:
        if not self._accept_input_events:
            return
        gesture_id = (
            self._diagnostic_trace.current_gesture(button_id)
            if event_source.startswith("raw_")
            else None
        )
        if not gesture_id:
            gesture_id = self._diagnostic_trace.begin_gesture(
                button_id,
                event_source,
                bool(is_pressed),
                usage=int(hid_usage) if hid_usage is not None else -1,
            )
        self._diagnostic_trace.set_current_gesture(gesture_id)
        self._diagnostic_trace.emit(
            "logical_button_event",
            gesture_id=gesture_id,
            button_id=str(button_id),
            source=str(event_source),
            edge="down" if is_pressed else "up",
            usage=int(hid_usage) if hid_usage is not None else -1,
        )
        now = time.monotonic()
        with self._input_arbitration_lock:
            if button_id in self._input_rearm_blocked_buttons:
                if not is_pressed:
                    self._input_rearm_blocked_buttons.discard(button_id)
                return
        if event_source == "hid_tap":
            with self._input_arbitration_lock:
                if not self._direct_hid_interception_ready:
                    return
        raw_mapping_bypassed = False
        if event_source.startswith("raw_"):
            with self._input_arbitration_lock:
                if (
                    self._direct_hid_interception_armed
                    or self._direct_hid_interception_ready
                ):
                    return

                # Raw Input can observe Windows' original key event, but it
                # cannot suppress that event. Until the HID tap owns the
                # report, every custom mapping must therefore fail closed.
                raw_mapping_bypassed = True

                if button_id in self._raw_fallback_buttons_down:
                    if not is_pressed:
                        self._cancel_raw_fallback_hold_guard_locked(button_id)
                        self._raw_fallback_buttons_down.discard(button_id)

                else:
                    effective_windows_button = raw_windows_button_id
                    if (
                        effective_windows_button is None
                        and not self._raw_fallback_tracking_active
                    ):
                        effective_windows_button = button_id
                    preserve_windows_original = (
                        effective_windows_button in _RAW_FALLBACK_KEY_TOKENS
                    )
                    if preserve_windows_original:
                        if is_pressed:
                            self._raw_fallback_buttons_down.add(button_id)
                            if not self._arm_raw_fallback_hold_guard_locked(button_id):
                                self._raw_fallback_buttons_down.discard(button_id)
                                physical_buttons = (
                                    self._take_raw_fallback_physical_buttons_locked(
                                        {button_id}
                                    )
                                )
                                self._block_input_until_release(
                                    {button_id} | physical_buttons
                                )
                                self._release_raw_fallback_keyups(
                                    physical_buttons,
                                    reason="raw_fallback_timer_unavailable",
                                )
                            self._logger.warning(
                                "RC003 mapping bypassed for non-intercepted input; "
                                "Windows original retained: button=%s source=%s",
                                button_id,
                                event_source,
                            )
                    elif is_pressed:
                        self._logger.warning(
                            "RC003 mapping bypassed for non-intercepted input; "
                            "no Windows original available: button=%s source=%s",
                            button_id,
                            event_source,
                        )
        if is_pressed:
            self._record_runtime_button(event_source)
        if button_id == "mic":
            detection_handled, detection_captured = (
                self._handle_key_detection_mic_event(
                    "physical_down" if is_pressed else "physical_up",
                    event_source,
                )
            )
            if detection_handled:
                if detection_captured:
                    self._logger.info(
                        "key detection captured button=mic source=%s; "
                        "voice action suppressed",
                        event_source,
                    )
                return
        if button_id in self._key_detection_suppressed_buttons:
            if not is_pressed:
                self._key_detection_suppressed_buttons.discard(button_id)
                self._key_detection_suppression_deadlines.pop(button_id, None)
                return
            deadline = self._key_detection_suppression_deadlines.get(button_id)
            if deadline is None or now < deadline:
                return
            self._key_detection_suppressed_buttons.discard(button_id)
            self._key_detection_suppression_deadlines.pop(button_id, None)
        detection_captured = False
        if is_pressed and button_id != "mic":
            try:
                detection_captured = key_detection_bridge.publish_next_button(
                    self._config_root,
                    button_id,
                )
            except OSError as exc:
                self._logger.warning("key detection IPC unavailable: %s", exc)
        if detection_captured:
            self._key_detection_suppressed_buttons.add(button_id)
            self._key_detection_suppression_deadlines[button_id] = (
                now + _KEY_DETECTION_SUPPRESSION_MAX_SECONDS
            )
            self._logger.info(
                "key detection captured button=%s; mapped action suppressed",
                button_id,
            )
            return
        if raw_mapping_bypassed:
            return
        if button_id == "mic" and self._ordinary_mic_gesture_active:
            self._handle_ordinary_mic_edge(event_source, is_pressed)
            self._apply_pending_settings_if_idle()
            return
        self._reload_settings_if_changed()

        with self._button_mapping_lock:
            removed_voice_binding = button_id in self._removed_voice_bindings
            ordinary_mic_handled = False
            if removed_voice_binding:
                if not is_pressed:
                    self._button_gestures.release(button_id)
                else:
                    self._logger.warning(
                        "button action suppressed until removed voice mapping is reselected: %s",
                        button_id,
                    )
                primary_action = None
                voice_mode = None
            else:
                primary_action = self._primary_button_action(button_id)
                voice_mode = self._voice_mode_for_primary_button(
                    button_id,
                    primary_action,
                )
                if (getattr(self, "_remote_profile", "") == remote_selection.CHROMECAST_PROFILE
                        and key_mapping.is_voice_action(primary_action)):
                    return  # Voice belongs to the next stage, including remapped ordinary buttons.
                if button_id == "mic" and voice_mode is None:
                    self._handle_ordinary_mic_edge(event_source, is_pressed)
                    ordinary_mic_handled = True
        if removed_voice_binding:
            self._apply_pending_settings_if_idle()
            return
        if ordinary_mic_handled:
            self._apply_pending_settings_if_idle()
            return

        if button_id == "mic":
            if not is_pressed:
                with self._voice_shortcut.lock:
                    source_was_down = (
                        self._voice_mic_gesture_active
                        and event_source in self._voice_mic_gesture_sources_down
                    )
                    if not source_was_down:
                        self._logger.info(
                            "voice physical release ignored without matching down: "
                            "source=%s",
                            event_source,
                        )
                        self._apply_pending_voice_settings_if_idle_locked()
                        return

                    matched_hid_released = event_source in {"hid", "hid_tap"}
                    if matched_hid_released:
                        self._voice_mic_gesture_hid_released = True
                    self._voice_mic_gesture_sources_down.discard(event_source)
                    if (
                        self._voice_shortcut.controller.trigger_mode
                        == key_mapping.VoiceTriggerMode.HOLD
                        and (
                            matched_hid_released
                            or not self._voice_mic_gesture_sources_down
                        )
                    ):
                        if (
                            event_source == "hid_tap"
                            and self._voice_mic_gesture_sources_down
                        ):
                            self._logger.info(
                                "voice hold release accepted from direct HID while "
                                "late duplicate sources remain: %s",
                                sorted(self._voice_mic_gesture_sources_down),
                            )
                        self._release_hold_voice_on_physical_release_locked(
                            "physical mic release",
                        )
                    if (
                        self._voice_mic_gesture_active
                        and not self._voice_mic_gesture_sources_down
                        and (
                            self._voice_mic_gesture_audio_stopped
                            or (
                                not self._voice_mic_gesture_audio_started
                            )
                        )
                    ):
                        self._finish_voice_mic_gesture()
                    self._apply_pending_voice_settings_if_idle_locked()
                return
            with self._voice_shortcut.lock:
                self._rollover_completed_voice_mic_gesture_locked(event_source)
                if not self._prepare_voice_mapping_locked(
                    button_id,
                    primary_action,
                ):
                    return
                if not self._begin_voice_mic_gesture(
                    event_source,
                    physical_down=True,
                ):
                    self._logger.info(
                        "voice physical trigger ignored: same mic gesture source=%s",
                        event_source,
                    )
                    return
                if self._voice_shortcut.controller.active:
                    self._logger.info(
                        "voice physical trigger ignored: hold session already active"
                    )
                    return
                # The physical key is the earliest reliable signal. Send the
                # host shortcut before the device's audio-start event so
                # voice input is already armed when PCM arrives. The matching
                # ATVV event is consumed by this pending latch.
                self._voice_raw_input_trigger_pending = True
                self._logger.info(
                    "voice physical mic trigger received before audio start"
                )
                self._handle_mic_button_pressed(
                    send_device_open=False,
                )
                if not self._voice_shortcut.controller.active and not self._doubao_session.busy:
                    self._voice_raw_input_trigger_pending = False
            return

        with self._button_mapping_lock:
            modifier = key_mapping.button_combo_modifier(self._bindings)
            configured_combo_buttons = frozenset(
                candidate
                for candidate in key_mapping.COMBO_ACTION_BUTTON_IDS
                if key_mapping.button_combo_action_for(
                    self._bindings, candidate
                ).kind
                != key_mapping.ActionKind.DISABLED
            )
            commands = (
                self._button_combos.press(
                    button_id,
                    modifier=modifier,
                    configured_buttons=configured_combo_buttons,
                )
                if is_pressed
                else self._button_combos.release(button_id)
            )
            self._dispatch_button_combo_commands(commands)
        self._apply_pending_settings_if_idle()

    def _block_input_until_release(self, buttons: set[str]) -> None:
        if not buttons:
            return
        self._input_rearm_blocked_buttons.update(buttons)

    def _dispatch_button_combo_commands(
        self, commands: List[button_combo.ComboCommand]
    ) -> None:
        for command in commands:
            if command.kind == button_combo.ComboCommandKind.FORWARD_PRESS:
                self._button_gestures.press(command.button_id)
            elif command.kind == button_combo.ComboCommandKind.FORWARD_RELEASE:
                self._button_gestures.release(command.button_id)
            elif command.kind == button_combo.ComboCommandKind.TRIGGER:
                self._on_button_combo_trigger(command.button_id)

    def _on_button_combo_trigger(self, button_id: str) -> None:
        action = key_mapping.button_combo_action_for(self._bindings, button_id)
        if action.kind == key_mapping.ActionKind.DISABLED:
            return
        self._logger.info(
            "button combination triggered: modifier=%s button=%s action=%s",
            key_mapping.button_combo_modifier(self._bindings),
            button_id,
            action.kind.value,
        )
        self._apply_button_action(action)

    def _is_button_combo_participant(self, button_id: str) -> bool:
        modifier = key_mapping.button_combo_modifier(self._bindings)
        if modifier is None:
            return False
        return button_id == modifier or (
            key_mapping.button_combo_action_for(self._bindings, button_id).kind
            != key_mapping.ActionKind.DISABLED
        )

    def _is_button_action_configured(
        self, button_id: str, trigger: button_gesture.ButtonTrigger
    ) -> bool:
        if button_id in self._removed_voice_bindings:
            return False
        action = key_mapping.button_action_for(self._bindings, button_id, trigger)
        if action.kind == key_mapping.ActionKind.DISABLED:
            return False
        if key_mapping.is_voice_action(action):
            return (
                trigger.value == key_mapping.ButtonTrigger.SINGLE_CLICK.value
                and self._voice_mode_for_primary_button(button_id, action) is not None
            )
        if action.kind == key_mapping.ActionKind.KEY_COMBO:
            try:
                win32_keys.resolve_vk_codes(action.keys)
            except win32_keys.UnknownKeyTokenError:
                return False
        if action_executor.is_application_action(action):
            # The action is intentional even if the app is currently not
            # installed.  Do not scan Start Menu/WindowsApps from the Raw
            # Input callback; dispatch will report the missing executable and
            # the configured mapping still correctly owns this physical key.
            return True
        return True

    def _is_button_repeatable(self, button_id: str) -> bool:
        action = key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK,
        )
        if button_id not in {
            "up",
            "down",
            "left",
            "right",
            "back",
            "volume_up",
            "volume_down",
        }:
            return False
        return key_mapping.action_allows_repeat(action)

    def _button_repeat_interval(self, button_id: str, repeat_count: int) -> float:
        action = key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK,
        )
        if button_id == "back":
            return button_gesture.ButtonGestureDispatcher.BACK_REPEAT_INTERVAL_SECONDS
        return button_gesture.ButtonGestureDispatcher.REPEAT_INTERVAL_SECONDS

    def _on_button_gesture_diagnostic(
        self, event: str, button_id: str, **fields: object
    ) -> None:
        self._diagnostic_trace.emit(
            event,
            gesture_id=self._diagnostic_trace.current_gesture(button_id) or "",
            button_id=str(button_id),
            **fields,
        )

    def _on_button_trigger(
        self, button_id: str, trigger: button_gesture.ButtonTrigger
    ) -> None:
        # ButtonGestureDispatcher keeps a callback reservation until this
        # function returns, so settings reload cannot replace the mapping we
        # are reading. Avoid taking _button_mapping_lock here: immediate input
        # enters the dispatcher while already holding it, whereas timer
        # callbacks are serialized by the dispatcher's callback gate.
        if button_id in self._removed_voice_bindings:
            self._logger.warning(
                "delayed button gesture suppressed until removed voice mapping "
                "is reselected: %s",
                button_id,
            )
            return
        action = key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger(trigger.value),
        )
        self._diagnostic_trace.emit(
            "mapping_trigger",
            gesture_id=self._diagnostic_trace.current_gesture(button_id) or "",
            attempt_id=self._diagnostic_trace.current_context()["attempt_id"],
            button_id=str(button_id),
            trigger=str(trigger.value),
            action=str(action.kind.value),
        )
        try:
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
                win32_keys.resolve_vk_codes(action.keys)
        except (
            KeyError,
            TypeError,
            ValueError,
            win32_keys.UnknownKeyTokenError,
        ):
            # A hand-edited or partially corrupted bindings file must disable
            # only the affected button, never escape the Raw Input callback
            # and tear down ordinary-button processing for the whole device.
            self._logger.warning(
                "invalid button binding ignored: button=%s trigger=%s",
                button_id,
                trigger.value,
            )
            return
        if key_mapping.is_voice_action(action):
            self._logger.warning(
                "secondary voice action ignored: button=%s trigger=%s",
                button_id,
                trigger.value,
            )
            return
        self._apply_button_action(action)

    def _apply_button_action(self, action: key_mapping.ButtonAction) -> None:
        with self._button_action_lock:
            self._apply_button_action_locked(action)

    def _apply_button_action_locked(self, action: key_mapping.ButtonAction) -> None:
        if (
            self._button_key_release_pending is not None
            or self._button_mouse_release_pending is not None
        ) and not self._release_pending_button_inputs():
            self._logger.info(
                "button action suppressed: an earlier input release is still pending"
            )
            return

        action_name = str(action.kind.value)
        action_success = False
        action_error = ""
        action_error_type = ""
        self._diagnostic_trace.set_current_action(action_name)
        try:
            if action.kind == key_mapping.ActionKind.DISABLED:
                return
            navigation_vk = _BUTTON_ACTION_NAVIGATION_VKS.get(action.kind)
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
                navigation_vk = _BUTTON_NAVIGATION_SINGLE_KEYS.get(tuple(action.keys))
            if navigation_vk is not None and element_navigation_control_windows.route_mapped_navigation_key(navigation_vk):
                # Accepted commands must not also be injected through SendInput.
                pass
            elif action.kind == key_mapping.ActionKind.KEY_COMBO:
                win32_input.send_key_combo_tap(action.keys)
            elif action.kind == key_mapping.ActionKind.ESCAPE:
                win32_input.send_escape()
            elif action.kind == key_mapping.ActionKind.RETURN:
                win32_input.send_return()
            elif action.kind == key_mapping.ActionKind.ARROW_UP:
                win32_input.send_arrow_up()
            elif action.kind == key_mapping.ActionKind.ARROW_DOWN:
                win32_input.send_arrow_down()
            elif action.kind == key_mapping.ActionKind.ARROW_LEFT:
                win32_input.send_arrow_left()
            elif action.kind == key_mapping.ActionKind.ARROW_RIGHT:
                win32_input.send_arrow_right()
            elif action.kind == key_mapping.ActionKind.DELETE_BACKWARD:
                win32_input.send_delete_backward()
            elif action.kind == key_mapping.ActionKind.SHOW_DESKTOP:
                win32_input.send_show_desktop()
            elif action.kind == key_mapping.ActionKind.CONTEXT_MENU:
                win32_input.send_context_menu()
            elif action.kind == key_mapping.ActionKind.APP_SWITCHER:
                win32_input.send_app_switcher()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_UP:
                win32_input.send_volume_up()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_DOWN:
                win32_input.send_volume_down()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_MUTE:
                win32_input.send_volume_mute()
            elif action.kind == key_mapping.ActionKind.PLAY_PAUSE:
                win32_input.send_play_pause()
            elif action.kind in _BUTTON_ACTION_MOUSE_BUTTONS:
                win32_input.send_mouse_button_click(
                    _BUTTON_ACTION_MOUSE_BUTTONS[action.kind]
                )
            elif action.kind == key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE:
                try:
                    result = (
                        element_navigation_control_windows.toggle_element_navigation()
                    )
                except Exception as exc:
                    action_error = "element_navigation_failed"
                    action_error_type = type(exc).__name__
                    self._logger.exception("element navigation toggle failed unexpectedly")
                    return
                if (
                    result.kind
                    == element_navigation_control_windows.ToggleResultKind.FAILED
                ):
                    action_error = "element_navigation_failed"
                    self._logger.warning(
                        "element navigation toggle failed: %s",
                        result.error or "unknown_error",
                    )
                self._diagnostic_trace.emit(
                    "element_navigation_toggle",
                    **self._diagnostic_trace.current_context(),
                    result=str(result.kind.value),
                    target_hwnd=int(result.target_hwnd),
                )
            elif action.kind == key_mapping.ActionKind.QUICKER_URI:
                action_executor.open_quicker_uri(action)
            elif action_executor.is_application_action(action):
                if not open_configured_application(action):
                    action_error = "application_unavailable"
                    self._logger.warning(
                        "application action unavailable: action=%s", action.kind.value
                    )
            # Voice actions are edge-driven in _on_button_event and never
            # enter this tap-only ordinary action executor.
            action_success = not action_error
        except win32_input.MouseButtonInUseError:
            action_error = "mouse_button_in_use"
            self._logger.info(
                "mouse button action skipped: the physical button is already held"
            )
        except win32_input.Win32InputUnavailableError:
            action_error = "send_input_unavailable"
            self._logger.info("button action skipped: SendInput unavailable here")
        except win32_input.InputCleanupIncompleteError:
            action_error = "input_cleanup_incomplete"
            tokens = self._button_action_key_tokens(action)
            if tokens is not None:
                self._button_key_release_pending = tokens
            mouse_button = _BUTTON_ACTION_MOUSE_BUTTONS.get(action.kind)
            if mouse_button is not None:
                self._button_mouse_release_pending = mouse_button
            self._schedule_button_input_release_retry_locked()
            self._logger.exception(
                "button action failed and safety input-up remains pending"
            )
        except OSError as exc:
            action_error = "delivery_failed"
            action_error_type = type(exc).__name__
            self._logger.exception(
                "button action failed to fully deliver: error_type=%s", action_error_type
            )
        finally:
            self._diagnostic_trace.emit(
                "action_result",
                **self._diagnostic_trace.current_context(),
                success=action_success,
                error=action_error,
                error_type=action_error_type,
            )
            self._diagnostic_trace.set_current_action(None)

    @staticmethod
    def _button_action_key_tokens(
        action: key_mapping.ButtonAction,
    ) -> Optional[Tuple[str, ...]]:
        if action.kind == key_mapping.ActionKind.KEY_COMBO:
            return tuple(action.keys)
        return _BUTTON_ACTION_KEY_TOKENS.get(action.kind)

    def _cancel_button_input_release_retry_locked(
        self,
        *,
        reset_delay: bool = True,
    ) -> None:
        timer = self._button_input_release_retry_timer
        self._button_input_release_retry_timer = None
        self._button_input_release_retry_token = None
        if reset_delay:
            self._button_input_release_retry_delay = (
                _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS
            )
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_button_input_release_retry_locked(self) -> None:
        if (
            self._button_input_release_retry_stopping
            or not self._accept_input_events
            or self._button_input_release_retry_timer is not None
            or (
                self._button_key_release_pending is None
                and self._button_mouse_release_pending is None
            )
        ):
            return
        token = object()
        try:
            timer = self._button_input_release_timer_factory(
                self._button_input_release_retry_delay,
                lambda token=token: self._retry_pending_button_inputs(token),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._button_input_release_retry_token = token
            self._button_input_release_retry_timer = timer
            timer.start()
        except BaseException as exc:
            if self._button_input_release_retry_token is token:
                self._button_input_release_retry_token = None
                self._button_input_release_retry_timer = None
            self._logger.error(
                "button input safety-release timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _retry_pending_button_inputs(self, token: object) -> None:
        with self._button_action_lock:
            if self._button_input_release_retry_token is not token:
                return
            self._button_input_release_retry_token = None
            self._button_input_release_retry_timer = None
            if (
                self._button_input_release_retry_stopping
                or not self._accept_input_events
            ):
                return
            self._button_input_release_retry_delay = min(
                _BUTTON_INPUT_RELEASE_RETRY_MAX_SECONDS,
                max(
                    _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS,
                    self._button_input_release_retry_delay * 2.0,
                ),
            )
            self._release_pending_button_inputs()

    def _finish_button_input_release_if_complete_locked(self) -> None:
        if (
            self._button_key_release_pending is None
            and self._button_mouse_release_pending is None
        ):
            self._cancel_button_input_release_retry_locked()

    def _release_pending_button_keys(self) -> bool:
        with self._button_action_lock:
            tokens = self._button_key_release_pending
            if tokens is None:
                self._finish_button_input_release_if_complete_locked()
                return True
            try:
                win32_input.send_key_combo_up(tokens)
            except win32_input.InputCleanupIncompleteError:
                self._logger.exception("button key safety release remains incomplete")
                self._schedule_button_input_release_retry_locked()
                return False
            except win32_input.Win32InputUnavailableError:
                self._logger.info("button key safety release unavailable")
                self._schedule_button_input_release_retry_locked()
                return False
            except OSError:
                # send_key_combo_up raises ordinary OSError only after its own
                # per-key fallback confirmed every requested key-up.
                self._logger.exception(
                    "button key safety release needed fallback but completed"
                )
            else:
                self._logger.info("button key safety release completed")
            self._button_key_release_pending = None
            self._finish_button_input_release_if_complete_locked()
            return True

    def _release_pending_button_mouse(self) -> bool:
        with self._button_action_lock:
            button = self._button_mouse_release_pending
            if button is None:
                self._finish_button_input_release_if_complete_locked()
                return True
            try:
                win32_input.send_mouse_button_up(button)
            except win32_input.InputCleanupIncompleteError:
                self._logger.exception("button mouse safety release remains incomplete")
                self._schedule_button_input_release_retry_locked()
                return False
            except win32_input.Win32InputUnavailableError:
                self._logger.info("button mouse safety release unavailable")
                self._schedule_button_input_release_retry_locked()
                return False
            except OSError:
                self._logger.exception(
                    "button mouse safety release needed fallback but completed"
                )
            else:
                self._logger.info("button mouse safety release completed")
            self._button_mouse_release_pending = None
            self._finish_button_input_release_if_complete_locked()
            return True

    def _release_pending_button_inputs(self) -> bool:
        with self._button_action_lock:
            key_complete = self._release_pending_button_keys()
            mouse_complete = self._release_pending_button_mouse()
            return key_complete and mouse_complete

    # -- ATVV control-channel events (mic button + audio start/stop) ------

    def _on_control_event(
        self,
        event: object,
        *,
        _ble_generation: Optional[int] = None,
    ) -> None:
        if not self._accepts_ble_callback(_ble_generation):
            return
        atvv_session_id = getattr(event, "session_id", None)
        reason = getattr(event, "reason", None)
        self._diagnostic_trace.emit(
            "voice_control_event",
            event_type=type(event).__name__,
            atvv_session_id=atvv_session_id if atvv_session_id is not None else -1,
            reason=reason if reason is not None else -1,
        )
        # Some machines expose no usable Raw Input/F5 edge for the mic key,
        # leaving AudioStarted as the first event of the next physical press.
        # Refresh here as well so saved voice mode/hotkey edits do not depend
        # on an ordinary HID event arriving first.
        self._reload_settings_if_changed()
        if isinstance(event, CapsReceived):
            self._logger.info(
                "voice capabilities received: version=0x%04x sample_rate=%s frame_size=%s",
                event.capabilities.version,
                event.capabilities.sample_rate,
                event.capabilities.frame_size,
            )
        elif isinstance(event, MicButtonPressed):
            detection_handled, detection_captured = (
                self._handle_key_detection_mic_event("atvv_press", "atvv")
            )
            if detection_handled:
                if detection_captured:
                    self._logger.info(
                        "key detection captured button=mic source=atvv; "
                        "voice action suppressed"
                    )
                return
            if not self._accepts_ble_callback(_ble_generation):
                return
            mic_action = self._primary_button_action("mic")
            mic_voice_mode = self._voice_mode_for_primary_button(
                "mic",
                mic_action,
            )
            if mic_voice_mode is None:
                with self._voice_shortcut.lock:
                    if not self._accepts_ble_callback(_ble_generation):
                        return
                    if not self._voice_shortcut.controller.active:
                        self._logger.info(
                            "ATVV mic trigger ignored: physical mic has an ordinary mapping"
                        )
                        if (
                            self._ble_session is not None
                            and not self._unsolicited_mic_close_pending
                        ):
                            self._unsolicited_mic_close_pending = True
                            self._ble_session.send_mic_close_threadsafe()
                return
            with self._voice_shortcut.lock:
                if not self._accepts_ble_callback(_ble_generation):
                    return
                if not self._prepare_voice_mapping_locked("mic", mic_action):
                    if self._ble_session is not None:
                        self._ble_session.send_mic_close_threadsafe()
                    return
                if self._voice_mic_gesture_active:
                    self._voice_raw_input_trigger_pending = False
                    self._voice_audio_start_fallback_pending = False
                    self._logger.info(
                        "voice mic trigger ignored: matched current multi-source gesture"
                    )
                else:
                    self._logger.info("voice mic trigger received from ATVV control channel")
                    if self._begin_voice_mic_gesture("atvv"):
                        self._handle_mic_button_pressed()
        elif isinstance(event, AudioStarted):
            self._diagnostic_trace.emit(
                "audio_started",
                **self._diagnostic_trace.current_context(),
                atvv_session_id=event.session_id if event.session_id is not None else -1,
                reason=event.reason if event.reason is not None else -1,
                codec=event.codec if event.codec is not None else -1,
            )
            detection_handled, detection_captured = (
                self._handle_key_detection_mic_event(
                    "audio_started",
                    "audio_started",
                )
            )
            if detection_handled:
                if detection_captured:
                    self._logger.info(
                        "key detection captured button=mic source=audio_started; "
                        "voice action suppressed"
                    )
                return
            with self._voice_shortcut.lock:
                if not self._accepts_ble_callback(_ble_generation):
                    return
                self._logger.info("voice audio started")
                if self._voice_audio_stream_active:
                    self._logger.info(
                        "voice duplicate audio start ignored: session_id=%s",
                        event.session_id,
                    )
                    return
                self._voice_audio_stream_active = True
                self._voice_audio_stop_processed = False
                self._voice_audio.stats.reset()
                self._voice_audio_start_fallback_pending = False
                mic_action = self._primary_button_action("mic")
                mic_voice_mode = self._voice_mode_for_primary_button(
                    "mic",
                    mic_action,
                )
                if not self._voice_shortcut.controller.active and mic_voice_mode is None:
                    self._abort_voice_audio_start_locked(
                        "physical mic has an ordinary mapping"
                    )
                    self._logger.info(
                        "unsolicited mic audio ignored: physical mic has an ordinary mapping"
                    )
                    if (
                        self._ble_session is not None
                        and not self._unsolicited_mic_close_pending
                    ):
                        self._unsolicited_mic_close_pending = True
                        self._ble_session.send_mic_close_threadsafe()
                    return
                if (
                    not self._voice_shortcut.controller.active
                    and not self._prepare_voice_mapping_locked("mic", mic_action)
                ):
                    self._abort_voice_audio_start_locked(
                        "mapped shortcut is unavailable"
                    )
                    self._logger.info(
                        "voice audio failing closed: mapped shortcut is unavailable"
                    )
                    if self._ble_session is not None:
                        self._ble_session.send_mic_close_threadsafe()
                    return
                if self._voice_mic_gesture_active:
                    self._rollover_completed_voice_mic_gesture_locked(
                        "audio_started"
                    )
                if self._voice_mic_gesture_active:
                    if self._voice_mic_gesture_audio_stopped:
                        self._logger.info(
                            "voice continuation audio start ignored until physical release"
                        )
                    else:
                        self._voice_mic_gesture_audio_started = True
                        self._logger.info(
                            "voice audio start matched current multi-source gesture"
                        )
                elif not self._voice_shortcut.controller.active:
                    self._logger.info("voice audio start used as microphone trigger")
                    if self._begin_voice_mic_gesture("audio_started"):
                        started = self._handle_mic_button_pressed(
                            send_device_open=False
                        )
                        if not started:
                            self._abort_voice_audio_start_locked(
                                "voice host start failed after audio started"
                            )
                        self._voice_audio_start_fallback_pending = (
                            self._voice_shortcut.controller.active or self._doubao_session.busy
                        )
        elif isinstance(event, AudioStopped):
            self._diagnostic_trace.emit(
                "audio_stopped",
                **self._diagnostic_trace.current_context(),
                reason=event.reason if event.reason is not None else -1,
            )
            detection_handled, _ = self._handle_key_detection_mic_event(
                "audio_stopped",
                "audio_started",
            )
            if detection_handled:
                self._logger.info(
                    "key detection mic audio stopped; voice state unchanged"
                )
                return
            with self._voice_shortcut.lock:
                if not self._accepts_ble_callback(_ble_generation):
                    return
                if (
                    not self._voice_audio_stream_active
                    and self._voice_audio_stop_processed
                ):
                    self._logger.info("voice duplicate audio stop ignored")
                    return
                runtime_voice_was_active = self._runtime_voice_active
                self._doubao_session.cancel_current()
                self._voice_audio_stream_active = False
                self._voice_audio_stop_processed = True
                self._voice_pcm_forwarding_enabled = False
                self._voice_pcm_min_arrival_sequence = None
                self._set_runtime_voice_active(False)
                doubao_attempt = self._active_doubao_attempt_locked()
                defer_doubao_diagnostic = doubao_attempt is not None
                if doubao_attempt is None:
                    flush_result = self._voice_audio.flush("audio stop")
                    if flush_result.error is not None:
                        self._logger.error(
                            "voice playback flush completed with failure: %s",
                            flush_result.error,
                        )
                self._logger.info("voice audio stopped")
                stats = self._voice_audio.stats.summary()
                self._diagnostic_trace.emit(
                    "audio_summary",
                    **self._diagnostic_trace.current_context(),
                    frames=int(stats["frames"]),
                    samples=int(stats["samples"]),
                    audio_ms=float(stats["audio_ms"]),
                    peak=int(stats["peak"]),
                    result=str(stats["result"]),
                )
                self._logger.info(
                    "voice PCM summary: frames=%s samples=%s audio_ms=%.0f "
                    "peak=%s rms=%.1f mean_abs=%.1f mean=%.1f "
                    "clipped=%s(%.3f%%) zero_crossings=%s result=%s",
                    stats["frames"],
                    stats["samples"],
                    stats["audio_ms"],
                    stats["peak"],
                    stats["rms"],
                    stats["mean_abs"],
                    stats["mean"],
                    stats["clipped_samples"],
                    stats["clipped_pct"],
                    stats["zero_crossings"],
                    stats["result"],
                )
                timing_snapshot = getattr(
                    self._voice_audio.sink, "timing_snapshot", None
                )
                if callable(timing_snapshot):
                    timing = timing_snapshot()
                    self._logger.info(
                        "voice playback timing: open_ms=%.2f writes=%s "
                        "last_write_ms=%.2f max_write_ms=%.2f underflows=%s",
                        timing.open_elapsed_ms,
                        timing.write_count,
                        timing.last_write_elapsed_ms,
                        timing.max_write_elapsed_ms,
                        timing.underflow_count,
                )
                self._voice_audio_start_fallback_pending = False
                self._voice_raw_input_trigger_pending = False
                self._unsolicited_mic_close_pending = False
                if self._voice_mic_gesture_active:
                    self._voice_mic_gesture_audio_started = False
                    self._voice_mic_gesture_audio_stopped = True
                action = self._voice_shortcut.controller.on_audio_stopped()
                if doubao_attempt is not None and action is not None:
                    action_applied = self._request_active_doubao_cleanup_locked(
                        doubao_attempt,
                        reason="audio stop",
                    )
                else:
                    action_applied = (
                        True if action is None else self._voice_shortcut.apply(action)
                    )
                if action is None or action_applied:
                    self._cancel_voice_hold_watchdog_locked()
                if action is not None and not action_applied:
                    # Same rule as _cleanup_once(): on_audio_stopped() already
                    # cleared the controller's pending state before we knew
                    # whether the closing key-up actually delivered. A failure
                    # here must not
                    # be recorded as a clean close - restore the owed state and
                    # fail closed by requesting a reconnect, the same way a BLE
                    # disconnect or a playback write failure does (XRBM-019
                    # review round 1 P1 #4).
                    self._voice_shortcut.controller.restore_pending(action)
                    self._logger.info(
                        "voice closing action failed to fully deliver; state retained, "
                        "requesting reconnect"
                    )
                    self._supervisor.request_reconnect()
                else:
                    if (
                        self._voice_mic_gesture_active
                        and self._voice_mic_gesture_audio_stopped
                        and not self._voice_mic_gesture_sources_down
                    ):
                        self._finish_voice_mic_gesture()
                if runtime_voice_was_active:
                    wetype_result_recorded = self._voice_shortcut.record_audio_result(
                        int(stats["frames"])
                    )
                    if action is not None and not action_applied:
                        self._set_runtime_voice_result(
                            bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED
                        )
                    elif wetype_result_recorded:
                        pass
                    elif stats["frames"] > 0:
                        self._set_runtime_voice_result(
                            bridge_runtime_status.VOICE_RUNTIME_SUCCESS
                        )
                    else:
                        self._set_runtime_voice_result(
                            bridge_runtime_status.VOICE_RUNTIME_AUDIO_EMPTY
                        )
                observation = self._log_voice_submission_observation()
                text_changed = bool(
                    observation is not None
                    and observation.text_delta is not None
                    and observation.text_delta > 0
                )
                if text_changed:
                    attempt_result = "text_changed"
                elif (
                    self._voice_shortcut.ui_confirmation == "confirmed"
                    and stats["frames"] > 0
                ):
                    attempt_result = "host_confirmed_audio_ok"
                else:
                    attempt_result = "unknown"
                if not defer_doubao_diagnostic:
                    self._finish_voice_diagnostic_attempt(
                        attempt_result,
                        frames=int(stats["frames"]),
                        samples=int(stats["samples"]),
                        audio_signal=bool(stats["frames"] > 0),
                    )
                self._apply_pending_voice_settings_if_idle_locked()

    def _record_device_diagnostic_context(self) -> None:
        selected = remote_selection.normalize(self._config.get(remote_selection.KEY))
        self._diagnostic_trace.emit(
            "device_context", app_version=__version__,
            selected_ref=selected["active"][:12],
            registered=[{"device_ref": row["key"][:12], "profile": row["profile"]}
                        for row in selected["devices"]],
        )

    def _ensure_voice_diagnostic_attempt(
        self,
        *,
        provider_shortcut_mode: Optional[str] = None,
        effective_hotkey_tokens: Optional[Tuple[str, ...]] = None,
        effective_backend: Optional[str] = None,
    ) -> None:
        if self._voice_attempt_id is not None:
            return
        self._voice_shortcut.ui_confirmation = "unknown"
        self._voice_text_observation = "unknown"
        if not self._diagnostic_trace.enabled:
            # Preserve the active-attempt sentinel without collecting diagnostics.
            self._voice_attempt_id = ""
            return
        backend = effective_backend or self._configured_voice_hotkey_backend()
        tokens = effective_hotkey_tokens or tuple(
            (*self._voice_shortcut.hotkey.modifiers, self._voice_shortcut.hotkey.key)
        )
        shortcut_mode = str(
            provider_shortcut_mode or self._voice_shortcut.controller.trigger_mode.value
        )
        provider = voice_program_manager.normalize_voice_program_settings(
            self._config.get("voice_program")
        )["provider"]
        physicalizer_generation = -1
        physicalizer_installation_epoch = -1
        physicalizer_observed_generation = -1
        physicalizer_observed_installation_epoch = -1
        physicalizer_binding = "not_required"
        physicalizer_observation = "not_required"
        try:
            uses_right_alt = (
                win32_keys.VK_CODES["ralt"]
                in win32_keys.resolve_vk_codes(tokens)
            )
        except win32_keys.UnknownKeyTokenError:
            uses_right_alt = False
        if (
            uses_right_alt
            and backend
            in {
                _VOICE_HOTKEY_BACKEND_MARKED,
                _VOICE_HOTKEY_BACKEND_DOUBAO,
            }
        ):
            with self._voice_key_physicalizer_lifecycle_lock:
                physicalizer = self._voice_key_physicalizer
                if physicalizer is not None:
                    physicalizer_observed_generation = int(
                        getattr(physicalizer, "tracker_generation", -1)
                    )
                    physicalizer_observed_installation_epoch = int(
                        getattr(physicalizer, "installation_epoch", -1)
                    )
                    physicalizer_binding = "observed"
                    physicalizer_observation = (
                        "accepting"
                        if bool(getattr(physicalizer, "accepts_new_down", True))
                        else "degraded"
                    )
                else:
                    physicalizer_binding = "unbound"
                    physicalizer_observation = "missing"
        self._voice_attempt_id = self._diagnostic_trace.begin_attempt(
            self._diagnostic_trace.current_gesture("mic"),
            provider=provider,
        )
        self._diagnostic_trace.emit(
            "voice_attempt_context",
            **self._diagnostic_trace.current_context(),
            backend=str(backend),
            remote_recording_mode=str(
                self._config.get("remote_recording_mode", "hold")
            ),
            provider_shortcut_mode=shortcut_mode,
            hotkey_tokens=list(tokens),
            physicalizer_binding=physicalizer_binding,
            physicalizer_generation=physicalizer_generation,
            physicalizer_installation_epoch=physicalizer_installation_epoch,
            physicalizer_observation=physicalizer_observation,
            physicalizer_observed_generation=physicalizer_observed_generation,
            physicalizer_observed_installation_epoch=(
                physicalizer_observed_installation_epoch
            ),
            **diagnostic_trace.foreground_context(),
        )

    def _finish_voice_diagnostic_attempt(
        self, result: str, **fields: object
    ) -> None:
        payload = {
            "host_ui": self._voice_shortcut.ui_confirmation or "unknown",
            "text_state": self._voice_text_observation or "unknown",
        }
        payload.update(fields)
        self._diagnostic_trace.end_attempt(
            self._voice_attempt_id,
            str(result),
            **payload,
        )
        self._voice_attempt_id = None

    def _doubao_settings_identity(self) -> tuple[object, ...]:
        return (
            self._settings_file_mtime_ns(self._config_path),
            remote_selection.active_key(self._config),
            self._configured_voice_hotkey_backend(),
            self._voice_shortcut.hotkey.serialize(),
            str(self._config.get("output_endpoint_name") or ""),
            str(self._config.get("output_endpoint_host_api") or ""),
        )

    def _doubao_attempt_matches_locked(
        self, attempt: rc003_doubao_session.DoubaoAttempt
    ) -> bool:
        snapshot = attempt.snapshot
        return bool(
            self._doubao_session.is_current(attempt)
            and not attempt.cancelled()
            and self._accept_input_events
            and self._accept_ble_events
            and snapshot.ble_generation == self._ble_callback_generation
            and snapshot.remote_key == self._selected_remote_key
            and snapshot.settings_identity == self._doubao_settings_identity()
            and self._configured_voice_hotkey_backend()
            == _VOICE_HOTKEY_BACKEND_DOUBAO
            and self._voice_mic_gesture_active
            and not self._voice_mic_gesture_hid_released
        )

    def _doubao_attempt_target_healthy(
        self,
        attempt: rc003_doubao_session.DoubaoAttempt,
    ) -> bool:
        generation, pid = attempt.target_identity()
        return bool(
            generation is not None
            and pid is not None
            and self._voice_shortcut.doubao_physicalizer.is_active_generation(generation)
            and self._voice_shortcut.doubao_physicalizer.target_pid == pid
        )

    def _begin_rc003_doubao_attempt_locked(
        self, *, send_device_open: bool
    ) -> bool:
        if self._remote_profile != remote_selection.RC003_PROFILE:
            return False
        if self._doubao_session.busy:
            self._logger.info(
                "Doubao voice start ignored: an earlier attempt still owns cleanup"
            )
            return False
        existing_sink = self._voice_audio.sink
        if existing_sink is not None and not getattr(existing_sink, "ready", True):
            self._logger.warning(
                "Doubao voice start rejected: failed playback owner is retained"
            )
            self._supervisor.request_reconnect()
            return False
        tokens = tuple(self._voice_shortcut.hotkey.modifiers) + (self._voice_shortcut.hotkey.key,)
        session = self._ble_session
        arrival_watermark = int(
            getattr(session, "audio_arrival_watermark", -1)
        )
        snapshot = rc003_doubao_session.DoubaoAttemptSnapshot(
            ble_generation=self._ble_callback_generation,
            remote_key=self._selected_remote_key,
            settings_identity=self._doubao_settings_identity(),
            tokens=tokens,
            endpoint_name=str(self._config.get("output_endpoint_name") or ""),
            endpoint_host_api=str(
                self._config.get("output_endpoint_host_api") or ""
            ),
            send_device_open=bool(send_device_open),
            arrival_watermark=arrival_watermark,
        )
        self._voice_pcm_forwarding_enabled = False
        self._voice_pcm_min_arrival_sequence = None
        try:
            attempt = self._doubao_session.begin(
                snapshot,
                lambda owned: self._run_rc003_doubao_attempt(
                    owned,
                    existing_sink=existing_sink,
                ),
            )
        except BaseException as exc:  # noqa: BLE001 - input callback must fail closed
            self._logger.error(
                "Doubao voice attempt worker failed to start: error_type=%s",
                type(exc).__name__,
            )
            return False
        if attempt is None:
            return False
        self._diagnostic_trace.emit(
            "doubao_start_pending",
            **self._diagnostic_trace.current_context(),
            arrival_watermark=int(arrival_watermark),
            send_device_open=bool(send_device_open),
        )
        return True

    def _create_private_doubao_playback(
        self, snapshot: rc003_doubao_session.DoubaoAttemptSnapshot
    ):
        endpoints = audio_output.enumerate_output_endpoints()
        audio_output.resolve_selected_endpoint(
            endpoints,
            snapshot.endpoint_name,
            snapshot.endpoint_host_api,
        )
        sink = audio_playback.EndpointPlaybackSink(
            snapshot.endpoint_name,
            snapshot.endpoint_host_api,
        )
        return sink

    def _finish_cancelled_doubao_control(
        self,
        attempt: rc003_doubao_session.DoubaoAttempt,
        prepared: Optional[wetype_control_windows.PreparedDoubaoVoiceStart],
        *,
        dispatched: bool,
    ) -> set[str]:
        control = self._voice_shortcut.doubao_control
        if prepared is not None and not dispatched:
            try:
                control.cancel_prepared(prepared)
            except Exception:
                self._logger.exception("Doubao prepared start cancellation failed")
                return {"preparation"}
            wait_settled = getattr(prepared, "wait_settled", None)
            if callable(wait_settled) and not wait_settled(None):
                return {"preparation"}
        if not dispatched and not getattr(control, "cleanup_pending", False):
            return set()
        try:
            expected_markers = len(
                tuple(dict.fromkeys(win32_keys.resolve_vk_codes(attempt.snapshot.tokens)))
            )
        except win32_keys.UnknownKeyTokenError:
            expected_markers = 0
        self._voice_shortcut.doubao_physicalizer.expect_markers("up", expected_markers)
        try:
            released = bool(control.stop())
        except Exception:
            released = False
            self._logger.exception("Doubao cancelled start could not release shortcut")
        if not released:
            attempt.add_cleanup_debts("hotkey")
            with self._voice_shortcut.lock:
                self._voice_shortcut.pending_tokens = attempt.snapshot.tokens
                self._voice_shortcut.pending_backend = (
                    _VOICE_HOTKEY_BACKEND_DOUBAO
                )
                self._voice_shortcut.schedule_release_retry()
            self._supervisor.request_reconnect()
            return {"hotkey"}
        return set()

    def _run_rc003_doubao_attempt(
        self,
        attempt: rc003_doubao_session.DoubaoAttempt,
        *,
        existing_sink,
    ) -> None:
        snapshot = attempt.snapshot
        sink = existing_sink
        private_sink = False
        prepared = None
        dispatched = False
        dispatch_may_have_sent = False
        committed = False
        outcome = "failed_before_dispatch"
        cleanup_debts: set[str] = set()
        error_type = ""
        watch = None
        try:
            if sink is None:
                sink = self._create_private_doubao_playback(snapshot)
                private_sink = True
                attempt.retain_resource("endpoint", sink)
                sink.open()
            if attempt.cancelled():
                outcome = "cancelled_before_dispatch"
                return
            prepared = self._voice_shortcut.doubao_control.prepare(
                snapshot.tokens,
                cancelled=attempt.cancelled,
                cancel_event=attempt.cancel_event,
            )
            if prepared is None:
                outcome = (
                    "cancelled_before_dispatch"
                    if attempt.cancelled()
                    else "prepare_failed"
                )
                return
            if not getattr(prepared, "ready", True):
                outcome = "prepare_timeout"
                return
            if not self._prepare_doubao_voice_physicalizer(
                snapshot.tokens, cancel_event=attempt.cancel_event
            ):
                outcome = (
                    "cancelled_before_dispatch"
                    if attempt.cancelled()
                    else "physicalizer_failed"
                )
                return
            physicalizer_generation = int(self._voice_shortcut.doubao_physicalizer.generation)
            physicalizer_pid = self._voice_shortcut.doubao_physicalizer.target_pid
            if (
                physicalizer_pid is None
                or not self._voice_shortcut.doubao_physicalizer.is_active_generation(
                    physicalizer_generation
                )
            ):
                outcome = "physicalizer_target_unavailable"
                return
            attempt.bind_physicalizer(
                physicalizer_generation,
                int(physicalizer_pid),
            )
            try:
                watch = self._doubao_capture_watch_factory(int(physicalizer_pid))
            except TypeError:
                # Test/injected factories predating target scoping remain
                # usable; the production factory always consumes the PID.
                watch = self._doubao_capture_watch_factory()
            watch.begin()
            self._capture_voice_focus_before()
            with self._voice_shortcut.lock:
                if (
                    not self._doubao_attempt_matches_locked(attempt)
                    or not self._doubao_attempt_target_healthy(attempt)
                ):
                    outcome = "cancelled_before_dispatch"
                    return
            try:
                expected_markers = len(
                    tuple(dict.fromkeys(win32_keys.resolve_vk_codes(snapshot.tokens)))
                )
            except win32_keys.UnknownKeyTokenError:
                expected_markers = 0
            self._voice_shortcut.doubao_physicalizer.expect_markers("down", expected_markers)
            trace_context = {
                **self._diagnostic_trace.current_context(),
                "action": voice_controller.VoiceHostAction.KEY_DOWN.value,
            }
            self._diagnostic_trace.emit(
                "voice_hotkey_requested",
                **trace_context,
                backend=_VOICE_HOTKEY_BACKEND_DOUBAO,
                tokens=list(snapshot.tokens),
                source="rc003_doubao_session",
            )
            attempt.mark_dispatch_started()
            dispatched = bool(
                self._voice_shortcut.doubao_control.dispatch_prepared(
                    prepared,
                    cancelled=attempt.cancelled,
                )
            )
            dispatch_may_have_sent = bool(
                dispatched
                or self._voice_shortcut.control_flag(
                    "cleanup_pending",
                    _VOICE_HOTKEY_BACKEND_DOUBAO,
                )
            )
            generation = self._voice_shortcut.control_generation(
                _VOICE_HOTKEY_BACKEND_DOUBAO
            )
            self._diagnostic_trace.emit(
                "voice_hotkey_result",
                **trace_context,
                backend=_VOICE_HOTKEY_BACKEND_DOUBAO,
                delivered=bool(dispatched),
                generation=int(generation) if generation is not None else -1,
                cleanup_pending=bool(
                    self._voice_shortcut.control_flag(
                        "cleanup_pending", _VOICE_HOTKEY_BACKEND_DOUBAO
                    )
                ),
            )
            if not dispatched:
                outcome = (
                    "cancelled_before_dispatch"
                    if attempt.cancelled()
                    else (
                        "dispatch_uncertain"
                        if dispatch_may_have_sent
                        else "dispatch_failed"
                    )
                )
                return
            attempt.transition(rc003_doubao_session.WAITING_HOST)
            deadline = time.monotonic() + _DOUBAO_HOST_READY_TIMEOUT_SECONDS
            host_ready = False
            while not attempt.cancelled() and time.monotonic() < deadline:
                if not self._doubao_attempt_target_healthy(attempt):
                    outcome = "physicalizer_target_unavailable"
                    return
                watch.poll(time.monotonic())
                if watch.identity is not None and watch.status == "tracking":
                    host_ready = True
                    break
                attempt.cancel_event.wait(_DOUBAO_HOST_READY_POLL_SECONDS)
            if not host_ready:
                outcome = (
                    "cancelled_waiting_host"
                    if attempt.cancelled()
                    else "host_not_ready"
                )
                return
            session = self._ble_session
            readiness_watermark = int(
                getattr(session, "audio_arrival_watermark", snapshot.arrival_watermark)
            )
            with self._voice_shortcut.lock:
                if (
                    not self._doubao_attempt_matches_locked(attempt)
                    or not self._doubao_attempt_target_healthy(attempt)
                ):
                    outcome = "cancelled_waiting_host"
                    return
                if private_sink:
                    if self._voice_audio.sink is not None:
                        outcome = "playback_owner_changed"
                        return
                elif self._voice_audio.sink is not sink:
                    outcome = "playback_owner_changed"
                    return
                action = self._voice_shortcut.controller.on_mic_button_pressed()
                if action != voice_controller.VoiceHostAction.KEY_DOWN:
                    outcome = "logical_start_rejected"
                    return
                if private_sink:
                    self._voice_audio.adopt(sink)
                if not self._voice_audio.ensure_writer(sink):
                    if private_sink and self._voice_audio.sink is sink:
                        self._voice_audio.detach(sink)
                    self._voice_shortcut.controller.cancel_pending()
                    outcome = "playback_writer_failed"
                    return
                if private_sink:
                    attempt.release_resource("endpoint", sink)
                # From this point the ordinary connection cleanup owns the
                # published sink and its writer, including a later watchdog
                # failure. The attempt must not close it behind that worker.
                private_sink = False
                self._voice_shortcut.active_backend = _VOICE_HOTKEY_BACKEND_DOUBAO
                self._voice_shortcut.pending_tokens = None
                self._voice_shortcut.pending_backend = None
                self._voice_shortcut.begin_runtime(_VOICE_HOTKEY_BACKEND_DOUBAO)
                generation = self._voice_shortcut.control_generation(
                    _VOICE_HOTKEY_BACKEND_DOUBAO
                )
                self._voice_shortcut.track_generation(
                    generation,
                    _VOICE_HOTKEY_BACKEND_DOUBAO,
                )
                self._voice_shortcut.ui_confirmation = "confirmed"
                self._voice_pcm_min_arrival_sequence = readiness_watermark
                self._voice_pcm_forwarding_enabled = True
                self._set_runtime_voice_result(
                    bridge_runtime_status.VOICE_RUNTIME_ACTIVE,
                    provider=voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
                )
                self._set_runtime_voice_active(True)
                self._diagnostic_trace.emit(
                    "hotkey_sent",
                    **self._diagnostic_trace.current_context(),
                    backend=_VOICE_HOTKEY_BACKEND_DOUBAO,
                )
                if not self._arm_voice_hold_watchdog_locked():
                    self._force_voice_hold_release_locked(
                        "voice hold safety timer unavailable"
                    )
                    outcome = "watchdog_failed"
                    return
                if snapshot.send_device_open and self._ble_session is not None:
                    self._ble_session.send_mic_open_threadsafe()
                committed = True
                attempt.mark_active()
                self._diagnostic_trace.emit(
                    "doubao_host_ready",
                    **self._diagnostic_trace.current_context(),
                    readiness_watermark=int(readiness_watermark),
                )
            outcome = "active"
        except audio_output.AudioOutputUnavailableError as exc:
            error_type = type(exc).__name__
            outcome = "output_open_failed"
            self._logger.info("Doubao playback unavailable, failing closed: %s", exc)
        except BaseException as exc:  # noqa: BLE001 - always settle attempt ownership
            error_type = type(exc).__name__
            outcome = "failed_after_dispatch" if dispatched else "failed_before_dispatch"
            self._logger.exception("Doubao asynchronous voice start failed")
        finally:
            if not committed:
                cleanup_debts.update(
                    self._finish_cancelled_doubao_control(
                        attempt,
                        prepared,
                        dispatched=dispatched,
                    )
                )
                if private_sink and sink is not None:
                    try:
                        sink.close()
                    except Exception:
                        cleanup_debts.add("endpoint")
                        attempt.add_cleanup_debts("endpoint")
                        with self._voice_shortcut.lock:
                            if self._voice_audio.sink is None:
                                self._voice_audio.adopt(sink)
                        self._logger.exception(
                            "Doubao private playback cleanup failed; owner retained"
                        )
                        self._supervisor.request_reconnect()
                    else:
                        attempt.release_resource("endpoint", sink)
                with self._voice_shortcut.lock:
                    self._voice_pcm_forwarding_enabled = False
                    self._voice_pcm_min_arrival_sequence = None
                    if (
                        self._voice_shortcut.controller.active
                        and self._voice_shortcut.active_backend
                        in (None, _VOICE_HOTKEY_BACKEND_DOUBAO)
                    ):
                        self._voice_shortcut.controller.cancel_pending()
                        self._voice_shortcut.active_backend = None
                    self._voice_focus_before = None
                    self._voice_focus_provider = ""
                    self._voice_focus_submit_method = ""
                    self._set_runtime_voice_active(False)
                    self._set_runtime_voice_result(
                        (
                            bridge_runtime_status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED
                            if outcome == "output_open_failed"
                            else bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED
                        ),
                        provider=voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
                    )
                    self._finish_voice_diagnostic_attempt(
                        (
                            "failed_after_send"
                            if dispatch_may_have_sent
                            else "failed_before_send"
                        ),
                        reason=outcome,
                    )
            if committed:
                attempt.finish(
                    outcome,
                    error_type=error_type,
                )
            else:
                attempt.finish(
                    outcome,
                    error_type=error_type,
                )
            self._diagnostic_trace.emit(
                "doubao_start_finished",
                outcome=outcome,
                dispatch_started=bool(attempt.dispatch_started),
                cleanup_complete=bool(attempt.cleanup_complete),
                error_type=error_type or "none",
                capture_watch_status=(
                    str(watch.status) if watch is not None else "not_started"
                ),
            )

    def _handle_mic_button_pressed(
        self,
        *,
        send_device_open: bool = True,
    ) -> bool:
        """Resolve and open the user-selected output endpoint FIRST; only
        send the hotkey if that succeeds, and only send MIC_OPEN if the
        hotkey itself fully delivered. This is the fail-closed ordering
        XRBM-014 review RETRY P1 #3 (endpoint) and review round 2 P1 #6
        (hotkey) both require: a device streaming audio into Windows
        without the configured hotkey having actually engaged voice typing
        is exactly the "opens after host-trigger failure" defect - so
        failure at either step suppresses MIC_OPEN, not just a missing
        endpoint.
        """

        issue = voice_program_manager.voice_configuration_issue(self._config.get("voice_program"))
        if issue:
            self._logger.warning("voice startup rejected: %s", issue)
            self._set_runtime_voice_result(bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED)
            return False
        self._ensure_voice_diagnostic_attempt()
        if not self._accept_input_events or not self._accept_ble_events:
            self._voice_pcm_forwarding_enabled = False
            self._finish_voice_diagnostic_attempt(
                "failed_before_send", reason="input_unavailable"
            )
            return False

        if self._ble_session is None:
            self._voice_pcm_forwarding_enabled = False
            self._logger.info("voice ignored: BLE voice session is not connected")
            self._finish_voice_diagnostic_attempt(
                "failed_before_send", reason="ble_unavailable"
            )
            return False

        if self._voice_shortcut.pending_tokens is not None:
            if not self._voice_shortcut.release_pending():
                self._voice_pcm_forwarding_enabled = False
                self._set_runtime_voice_result(
                    bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED
                )
                self._logger.info(
                    "voice failing closed: an earlier hotkey release is still pending"
                )
                self._finish_voice_diagnostic_attempt(
                    "failed_before_send", reason="previous_release_pending"
                )
                return False
            self._voice_shortcut.pending_tokens = None

        if (
            self._remote_profile == remote_selection.RC003_PROFILE
            and self._configured_voice_hotkey_backend()
            == _VOICE_HOTKEY_BACKEND_DOUBAO
        ):
            return self._begin_rc003_doubao_attempt_locked(
                send_device_open=send_device_open
            )

        self._voice_pcm_min_arrival_sequence = None
        if not self._voice_audio.open():
            self._voice_pcm_forwarding_enabled = False
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED
            )
            self._logger.info(
                "voice failing closed: no usable output endpoint; hotkey/MIC_OPEN suppressed"
            )
            self._finish_voice_diagnostic_attempt(
                "failed_before_send", reason="output_open_failed"
            )
            return False

        if not self._wait_for_sogou_voice_process():
            self._voice_pcm_forwarding_enabled = False
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED
            )
            self._logger.info(
                "voice failing closed: Sogou voice process is not ready; "
                "hotkey/MIC_OPEN suppressed"
            )
            self._finish_voice_diagnostic_attempt(
                "failed_before_send", reason="provider_not_ready"
            )
            return False

        self._capture_voice_focus_before()
        action = self._voice_shortcut.controller.on_mic_button_pressed()
        action_delivered = self._voice_shortcut.apply(action)
        if not action_delivered:
            self._voice_focus_before = None
            self._voice_focus_provider = ""
            self._voice_focus_submit_method = ""
            self._voice_pcm_forwarding_enabled = False
            self._set_runtime_voice_result(
                bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED
            )
            # Nothing physically landed (win32_input.py's own batching already
            # rolled back any partial key-down), so clear the logical hold
            # without attempting a second delivery.
            self._voice_shortcut.controller.cancel_pending()
            self._logger.info(
                "voice failing closed: host hotkey delivery failed; device command suppressed"
            )
            self._finish_voice_diagnostic_attempt(
                "failed_after_send", reason="hotkey_delivery_failed"
            )
            return False

        self._voice_pcm_forwarding_enabled = True
        self._diagnostic_trace.emit(
            "hotkey_sent",
            **self._diagnostic_trace.current_context(),
            backend=self._configured_voice_hotkey_backend(),
        )
        self._set_runtime_voice_result(bridge_runtime_status.VOICE_RUNTIME_ACTIVE)
        self._set_runtime_voice_active(True)
        if not self._arm_voice_hold_watchdog_locked():
            self._force_voice_hold_release_locked(
                "voice hold safety timer unavailable"
            )
            return False
        if send_device_open and self._ble_session is not None:
            self._ble_session.send_mic_open_threadsafe()
        return True

    def _abort_voice_audio_start_locked(self, reason: str) -> None:
        self._voice_audio_stream_active = False
        self._voice_audio_stop_processed = True
        self._voice_audio_start_fallback_pending = False
        self._voice_raw_input_trigger_pending = False
        self._voice_pcm_forwarding_enabled = False
        self._set_runtime_voice_active(False)
        flush_result = self._voice_audio.flush(reason)
        if flush_result.error is not None:
            self._logger.error(
                "voice audio start rollback flush failed: %s",
                flush_result.error,
            )
        if self._voice_mic_gesture_active:
            self._voice_mic_gesture_audio_started = False
            self._voice_mic_gesture_audio_stopped = True
            if not self._voice_mic_gesture_sources_down:
                self._finish_voice_mic_gesture()

    def _wait_for_sogou_voice_process(self) -> bool:
        provider_settings = voice_program_manager.normalize_voice_program_settings(
            self._config.get("voice_program")
        )
        if provider_settings["provider"] != voice_program_manager.VOICE_PROGRAM_SOGOU:
            return True
        ready = voice_program_manager.wait_for_sogou_voice_process(timeout=0.6)
        if ready:
            self._logger.info("voice program: Sogou voice process ready")
        else:
            self._logger.warning("voice program: Sogou voice process is not running")
        return ready

    def _capture_voice_focus_before(self) -> None:
        provider_settings = voice_program_manager.normalize_voice_program_settings(
            self._config.get("voice_program")
        )
        self._voice_focus_provider = str(provider_settings["provider"])
        self._voice_focus_submit_method = (
            "wetype_hotkey"
            if self._configured_voice_hotkey_backend() == _VOICE_HOTKEY_BACKEND_WETYPE
            else "hotkey_hold"
        )
        snapshot = voice_interaction_diagnostics_windows.capture_focus_snapshot()
        self._voice_focus_before = snapshot
        self._diagnostic_trace.emit(
            "voice_focus_snapshot",
            **self._diagnostic_trace.current_context(),
            phase="before",
            provider=self._voice_focus_provider,
            submit_method=self._voice_focus_submit_method,
            supported=bool(snapshot.supported),
            foreground_pid=int(snapshot.foreground_pid),
            foreground_class=snapshot.foreground_class or "unknown",
            focus_handle=int(snapshot.focus_handle),
            focus_class=snapshot.focus_class or "unknown",
            text_length=(
                int(snapshot.text_length)
                if snapshot.text_length is not None
                else -1
            ),
            diagnostic=snapshot.error or "captured",
            voice_ui="unknown",
            foreground_executable=snapshot.foreground_executable,
            process_query_status=snapshot.process_query_status,
            foreground_thread_id=snapshot.foreground_thread_id,
            foreground_handle=snapshot.foreground_handle,
            foreground_stable=snapshot.foreground_stable,
            keyboard_layout=snapshot.keyboard_layout,
        )
        self._logger.info(
            "voice interaction start: provider=%s method=%s foreground_pid=%s "
            "foreground_class=%s focus_class=%s text_length=%s diagnostic=%s program=%s keyboard_layout=%s",
            self._voice_focus_provider,
            self._voice_focus_submit_method,
            snapshot.foreground_pid,
            snapshot.foreground_class or "unknown",
            snapshot.focus_class or "unknown",
            snapshot.text_length if snapshot.text_length is not None else "unavailable",
            snapshot.error or "captured",
            snapshot.foreground_executable or "unknown",
            snapshot.keyboard_layout,
        )

    def _log_voice_submission_observation(self):
        before = self._voice_focus_before
        if before is None:
            return None
        after = voice_interaction_diagnostics_windows.capture_focus_snapshot()
        observation = voice_interaction_diagnostics_windows.compare_submission(
            before,
            after,
        )
        self._voice_text_observation = str(observation.text_state)
        self._diagnostic_trace.emit(
            "voice_submission_observation",
            **self._diagnostic_trace.current_context(),
            provider=self._voice_focus_provider or "unknown",
            submit_method=self._voice_focus_submit_method or "unknown",
            supported=bool(after.supported),
            foreground_pid=int(after.foreground_pid),
            foreground_class=after.foreground_class or "unknown",
            focus_handle=int(after.focus_handle),
            focus_class=after.focus_class or "unknown",
            text_length=(
                int(after.text_length) if after.text_length is not None else -1
            ),
            focus_state=str(observation.focus_state),
            text_state=str(observation.text_state),
            text_delta=(
                int(observation.text_delta)
                if observation.text_delta is not None
                else -1
            ),
            diagnostic=after.error or "captured",
            voice_ui=self._voice_shortcut.ui_confirmation or "unknown",
            foreground_executable=after.foreground_executable,
            process_query_status=after.process_query_status,
            foreground_thread_id=after.foreground_thread_id,
            foreground_handle=after.foreground_handle,
            foreground_stable=after.foreground_stable,
            keyboard_layout=after.keyboard_layout,
            keyboard_layout_change=(
                "changed" if before.keyboard_layout != after.keyboard_layout else "same"
            ) if before.keyboard_layout and after.keyboard_layout and before.foreground_stable and after.foreground_stable else "unknown",
        )
        self._logger.info(
            "voice interaction result: provider=%s method=%s focus=%s "
            "text_length=%s delta=%s; host control completion alone does not prove "
            "text insertion",
            self._voice_focus_provider or "unknown",
            self._voice_focus_submit_method or "unknown",
            observation.focus_state,
            observation.text_state,
            (
                observation.text_delta
                if observation.text_delta is not None
                else "unavailable"
            ),
        )
        self._voice_focus_before = None
        self._voice_focus_provider = ""
        self._voice_focus_submit_method = ""
        return observation


    def _prepare_doubao_voice_physicalizer(
        self,
        tokens: Tuple[str, ...],
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        try:
            vk_codes = tuple(win32_keys.resolve_vk_codes(tokens))
        except win32_keys.UnknownKeyTokenError:
            self._logger.exception(
                "Doubao voice physicalizer rejected an unknown hotkey token"
            )
            return False
        # ActivateProfile can finish before ImeService.exe is created. Only
        # the asynchronous RC003 attempt may wait here; synchronous callers
        # retain their non-waiting behavior. Never retry a hook/permission/
        # version failure, or resend a shortcut while waiting for the host.
        deadline = time.monotonic() + _DOUBAO_PROCESS_READY_TIMEOUT_SECONDS
        waiting_logged = False
        while cancel_event is None or not cancel_event.is_set():
            if self._voice_shortcut.doubao_physicalizer.start(vk_codes):
                self._logger.info(
                    "Doubao voice physicalizer ready for configured shortcut"
                )
                return True
            if (
                cancel_event is None
                or self._voice_shortcut.doubao_physicalizer.status != "unavailable"
                or self._voice_shortcut.doubao_physicalizer.error != "ImeService.exe is not running"
            ):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not waiting_logged:
                self._logger.info("Waiting for Doubao process after profile activation")
                waiting_logged = True
            if cancel_event.wait(min(_DOUBAO_HOST_READY_POLL_SECONDS, remaining)):
                return False
            if time.monotonic() >= deadline:
                break
        self._logger.warning(
            "Doubao voice physicalizer unavailable: status=%s error=%s",
            self._voice_shortcut.doubao_physicalizer.status,
            self._voice_shortcut.doubao_physicalizer.error or "unknown",
        )
        return False











    def _on_pcm_frame(
        self,
        samples,
        *,
        _ble_generation: Optional[int] = None,
        _arrival_sequence: Optional[int] = None,
    ) -> None:
        """Queue one immutable PCM frame without blocking the BLE worker."""

        with self._voice_shortcut.lock:
            sink = self._voice_audio.sink
            doubao_attempt = self._active_doubao_attempt_locked()
            if (
                doubao_attempt is not None
                and not self._doubao_attempt_target_healthy(doubao_attempt)
            ):
                self._voice_pcm_forwarding_enabled = False
                self._voice_shortcut.controller.on_mic_button_released()
                self._request_active_doubao_cleanup_locked(
                    doubao_attempt,
                    reason="physicalizer target lost",
                )
                return
            cutoff = self._voice_pcm_min_arrival_sequence
            arrived_before_ready = bool(
                cutoff is not None
                and _arrival_sequence is not None
                and int(_arrival_sequence) <= cutoff
            )
            if arrived_before_ready:
                self._diagnostic_trace.emit(
                    "doubao_pcm_discarded",
                    **self._diagnostic_trace.current_context(),
                    arrival_sequence=int(_arrival_sequence),
                    readiness_watermark=int(cutoff),
                )
            if (
                sink is None
                or not self._accepts_ble_callback(_ble_generation)
                or not self._voice_pcm_forwarding_enabled
                or arrived_before_ready
            ):
                return
            if not self._voice_audio.ensure_writer(sink):
                self._voice_pcm_forwarding_enabled = False
                return
            writer = self._voice_audio.writer
            if writer is None or not writer.submit(samples):
                self._voice_pcm_forwarding_enabled = False


async def _run(
    *,
    app_factory=None,
    tray_factory=None,
    settings_launcher=None,
    show_notification_icon: bool = True,
    on_runtime_ready=None,
    on_reconnect_ready=None,
    on_settings_reload_ready=None,
    launch_voice_program_on_start: bool = True,
) -> None:
    tray_factory = tray_factory or bridge_tray_windows.BridgeTray
    settings_launcher = settings_launcher or bridge_launcher.launch_settings
    app = (
        RC003App(launch_voice_program_on_start=launch_voice_program_on_start)
        if app_factory is None
        else app_factory()
    )
    loop = asyncio.get_running_loop()
    tray_exit_requested = threading.Event()
    run_task = asyncio.create_task(app.run_forever())

    def open_settings() -> None:
        result = settings_launcher()
        if result.started:
            app._logger.info("notification area: settings opened; pid=%s", result.pid)
        else:
            app._logger.warning(
                "notification area: settings launch failed: %s",
                result.error or "unknown_error",
            )

    def request_exit() -> None:
        tray_exit_requested.set()
        loop.call_soon_threadsafe(run_task.cancel)

    if on_runtime_ready is not None:
        on_runtime_ready(request_exit)
    if on_reconnect_ready is not None:
        on_reconnect_ready(app.request_connection_retry_now)
    if on_settings_reload_ready is not None:
        on_settings_reload_ready(app.request_settings_reload_now)

    tray = None
    try:
        if show_notification_icon:
            try:
                tray = tray_factory(
                    on_open_settings=open_settings,
                    on_exit_requested=request_exit,
                    status_handler=lambda message: app._logger.info(
                        "notification area: %s", message
                    ),
                )
                if tray.start():
                    app._logger.info("notification area: bridge control icon started")
                else:
                    app._logger.warning(
                        "notification area unavailable: %s",
                        tray.startup_error or "unknown_error",
                    )
            except Exception:
                app._logger.exception("notification area failed to initialize")

        try:
            await run_task
        except asyncio.CancelledError:
            if not tray_exit_requested.is_set():
                raise
            app._logger.info("notification area: graceful bridge exit requested")
    finally:
        try:
            if tray is not None and not tray.stop():
                app._logger.warning("notification area thread did not stop cleanly")
        except Exception as exc:
            app._logger.warning(
                "notification area stop failed: error_type=%s",
                type(exc).__name__,
            )
        finally:
            try:
                await app.stop()
            finally:
                clear_runtime_status = getattr(app, "clear_runtime_status", None)
                if callable(clear_runtime_status):
                    clear_runtime_status()


def main(
    *,
    show_notification_icon: bool = True,
    on_runtime_ready=None,
    on_reconnect_ready=None,
    on_settings_reload_ready=None,
    launch_voice_program_on_start: bool = True,
) -> None:
    asyncio.run(
        _run(
            show_notification_icon=show_notification_icon,
            on_runtime_ready=on_runtime_ready,
            on_reconnect_ready=on_reconnect_ready,
            on_settings_reload_ready=on_settings_reload_ready,
            launch_voice_program_on_start=launch_voice_program_on_start,
        )
    )


if __name__ == "__main__":
    main()
