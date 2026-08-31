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
``_cleanup_once()`` releases the voice hotkey, stops the Raw Input listener
(which itself force-releases any stuck button), and closes the BLE session
(which sends MIC_CLOSE, unsubscribes, and closes the device/service) - every
step is individually wrapped so one step's failure never skips the rest.

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
stopping the Raw Input listener or closing the BLE session can now each
raise when the resource they own reports it is still alive (a thread that
did not stop within its join timeout - see raw_input_windows.py's
``stop()``/ble_transport_winrt.py's ``close()``). ``_cleanup_once()`` still
attempts every one of the four steps (voice hotkey, HID, BLE, playback)
regardless of any single step's outcome, but a step whose owner reports it
is still alive is intentionally left set on ``self._hid_listener``/
``self._ble_session`` - not cleared to ``None`` - so no later code can
mistake a still-running listener/session for a clean slate. Once every step
has been attempted, any such retained-owner failure is aggregated and
raised from ``_cleanup_once()`` itself, which is
``ConnectionSupervisor.run_forever()``'s injected ``cleanup`` callable: that
exception propagates out of ``run_forever()``'s ``finally`` block and ends
the connect/retry loop entirely - the supervisor fails closed rather than
starting a fresh ``connect()`` generation over resources that might still
be live.
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
    action_executor,
    ble_transport_winrt,
    bridge_launcher,
    bridge_runtime_status,
    bridge_tray_windows,
    button_combo,
    button_gesture,
    config,
    connection_supervisor,
    element_navigation_control_windows,
    frida_compat,
    hid_identity,
    hotkey,
    key_detection_bridge,
    key_mapping,
    legacy_key_suppressor_windows,
    logging_setup,
    raw_input_windows,
    voice_controller,
    voice_interaction_diagnostics_windows,
    voice_program_manager,
    wetype_control_windows,
    win32_input,
    win32_keys,
)
from .atvv_session import AudioStarted, AudioStopped, CapsReceived, MicButtonPressed, PcmStats


class CleanupIncompleteError(RuntimeError):
    """Raised by ``RC003App._cleanup_once()`` when the Raw Input listener
    and/or the BLE session report they are still alive after cleanup was
    attempted - see the module docstring's "Cleanup ownership" note. Every
    other cleanup step still ran before this is raised.
    """


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
    key_mapping.ActionKind.APP_SWITCHER: ("alt", "tab"),
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: ("volume_up",),
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: ("volume_down",),
    key_mapping.ActionKind.SYSTEM_VOLUME_MUTE: ("volume_mute",),
    key_mapping.ActionKind.PLAY_PAUSE: ("media_play_pause",),
}

_KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS = 1.0
_KEY_DETECTION_MIC_MAX_SECONDS = 10.0
_ORDINARY_MIC_RELEASE_GUARD_SECONDS = 0.12
_RUNTIME_STATUS_HEARTBEAT_SECONDS = 5.0
_VOICE_HOTKEY_BACKEND_MARKED = "marked_keybd_event"
_VOICE_HOTKEY_BACKEND_WETYPE = "wetype_virtual_key_sendinput"


def open_configured_application(action: key_mapping.ButtonAction) -> bool:
    """Application-action seam kept at the app boundary for testability."""

    return action_executor.open_configured_application(action)


class RC003App:
    def __init__(self) -> None:
        self._config_root = config.config_root()
        self._config_path = config.config_path(self._config_root)
        self._config = config.load_config(self._config_path)
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
        self._button_gestures = button_gesture.ButtonGestureDispatcher(
            is_action_configured=self._is_button_action_configured,
            is_repeatable=self._is_button_repeatable,
            on_trigger=self._on_button_trigger,
        )
        self._button_combos = button_combo.ButtonComboRecognizer()
        self._logger: logging.Logger = logging_setup.get_logger(self._config_root)
        self._runtime_identity = bridge_runtime_status.current_runtime_identity(
            __version__
        )
        self._runtime_status_lock = threading.Lock()
        self._runtime_connection_state = (
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        self._runtime_raw_input_state = "starting"
        self._runtime_hid_tap_state = "starting"
        self._runtime_last_button_at: Optional[float] = None
        self._runtime_last_button_source = ""
        self._runtime_voice_active = False
        self._runtime_last_button_publish_monotonic = 0.0
        self._logger.info(
            "startup: app identity: version=%s runtime=%s package=%s",
            self._runtime_identity.app_version,
            self._runtime_identity.runtime_kind,
            self._runtime_identity.package_name,
        )
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
        configured_voice_program = (
            voice_program_manager.normalize_voice_program_settings(
                self._config.get("voice_program")
            )
        )
        if configured_voice_program["provider"] == voice_program_manager.VOICE_PROGRAM_SOGOU:
            sogou_prewarm = voice_program_manager.prewarm_sogou_voice_component()
            self._logger.info(
                "voice program: Sogou component prewarm=%s",
                sogou_prewarm.code,
            )
        if self._removed_voice_bindings:
            self._logger.warning(
                "legacy voice mappings disabled until user reselects actions: %s",
                sorted(self._removed_voice_bindings),
            )
        self._voice = voice_controller.VoiceController()
        runtime_hotkey_text = str(self._config.get("voice_hotkey", "")).strip()
        if not runtime_hotkey_text:
            runtime_hotkey_text = key_mapping.voice_hotkey_for_trigger_mode(
                self._voice.trigger_mode
            )
        self._voice_hotkey = hotkey.HotkeySpec.parse(runtime_hotkey_text)
        self._pending_voice_settings = None
        self._pending_config = None
        self._pending_bindings = None
        self._voice_audio_start_fallback_pending = False
        self._voice_hotkey_release_pending: Optional[Tuple[str, ...]] = None
        self._voice_hotkey_active_backend: Optional[str] = None
        self._voice_hotkey_release_pending_backend: Optional[str] = None
        self._voice_focus_before: Optional[
            voice_interaction_diagnostics_windows.FocusSnapshot
        ] = None
        self._voice_focus_provider = ""
        self._voice_focus_submit_method = ""
        self._sogou_readiness_lock = threading.Lock()
        self._sogou_readiness_check_running = False
        self._wetype_voice_control = wetype_control_windows.WeTypeVoiceControl(
            logger=self._logger
        )
        self._button_key_release_pending: Optional[Tuple[str, ...]] = None
        # Raw Input and the ATVV control channel arrive on different worker
        # threads. Serialize the voice state machine so one physical press
        # cannot race into two host shortcut deliveries.
        self._voice_trigger_lock = threading.Lock()
        self._logger.info(
            "startup: voice settings active: trigger_mode=%s hotkey=%s",
            self._voice.trigger_mode.value,
            self._voice_hotkey.serialize(),
        )
        # One RC003 microphone press is reported independently by the legacy
        # F5 hook, HID/Raw Input, the ATVV mic opcode, and sometimes
        # AUDIO_STARTED first. Keep all reports in one gesture so one real
        # press produces one host key-down and one release.
        self._voice_mic_gesture_active = False
        self._voice_mic_gesture_audio_started = False
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = False
        self._voice_mic_gesture_hid_released = False
        self._voice_mic_gesture_started_without_direct_hid = False
        self._voice_mic_gesture_direct_hid_seen = False
        self._voice_mic_gesture_legacy_f5_released = False
        self._voice_mic_gesture_sources_down: set[str] = set()
        self._ordinary_mic_lock = threading.Lock()
        self._ordinary_mic_sources_down: set[str] = set()
        self._ordinary_mic_late_sources_down: set[str] = set()
        self._ordinary_mic_sources_seen: set[str] = set()
        self._ordinary_mic_release_guard_until = 0.0
        self._ordinary_mic_gesture_active = False
        self._unsolicited_mic_close_pending = False
        self._voice_audio_stream_active = False
        self._voice_audio_stop_processed = False
        self._voice_pcm_forwarding_enabled = False
        self._voice_raw_input_trigger_pending = False
        # WH_KEYBOARD_LL must never wait for the voice/audio state machine.
        # This private lock collapses repeated legacy F5 records before they
        # are queued and exposes a brief down-state snapshot to the voice
        # state machine. No hook callback holds it while acquiring voice state.
        self._legacy_f5_hook_lock = threading.Lock()
        self._legacy_f5_is_down = False
        # A matched HID release can retire a still-down F5 duplicate before its
        # delayed up arrives. Keep that old pair quarantined so it cannot attach
        # to the next voice gesture.
        self._legacy_f5_voice_blocked_until_up = False
        self._ble_session: Optional[ble_transport_winrt.RC003BleSession] = None
        self._hid_listener: Optional[raw_input_windows.RawInputButtonListener] = None
        self._legacy_key_suppressor: Optional[
            legacy_key_suppressor_windows.LegacyKeySuppressor
        ] = None
        self._hid_report_tap: Optional[frida_compat.RC003HidReportTap] = None
        self._direct_hid_usages: set[int] = set()
        self._direct_hid_lock = threading.Lock()
        # True once the tap has reported at least one full keyboard snapshot.
        # While the tap side channel is live, the keyboard Raw Input path
        # stands down so the same physical edge is not armed/dispatched twice.
        self._direct_hid_tap_active = False
        self._key_detection_suppressed_buttons: set[str] = set()
        self._key_detection_mic_lock = threading.Lock()
        self._key_detection_mic_gesture_active = False
        self._key_detection_mic_gesture_started_at = 0.0
        self._key_detection_mic_release_deadline: Optional[float] = None
        self._key_detection_mic_audio_started = False
        self._key_detection_mic_sources_down: set[str] = set()
        self._playback: Optional[audio_playback.EndpointPlaybackSink] = None
        self._playback_writer: Optional[
            audio_playback_worker.PlaybackWriteWorker
        ] = None
        self._voice_pcm_stats = PcmStats()
        self._event_loop = asyncio.get_event_loop()
        self._legacy_voice_event_generation = 0
        # Cleanup disables every input callback before releasing host keys.
        # A reconnect explicitly re-enables the next generation.
        self._accept_input_events = True

        self._supervisor = connection_supervisor.ConnectionSupervisor(
            connect=self._connect_once,
            cleanup=self._cleanup_once,
            retry_delay=float(self._config.get("retry_delay", 2.0)),
            max_retry_delay=float(self._config.get("max_retry_delay", 60.0)),
            logger=self._logger,
            loop=self._event_loop,
        )

    # -- lifecycle: driven by ConnectionSupervisor -------------------------

    async def run_forever(self) -> None:
        heartbeat = self._event_loop.create_task(
            self._runtime_status_heartbeat()
        )
        try:
            await self._supervisor.run_forever()
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def stop(self) -> None:
        await self._supervisor.stop()

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
            try:
                bridge_runtime_status.publish_status(
                    self._config_root,
                    self._runtime_connection_state,
                    identity=self._runtime_identity,
                    raw_input_state=self._runtime_raw_input_state,
                    hid_tap_state=self._runtime_hid_tap_state,
                    last_button_at=self._runtime_last_button_at,
                    last_button_source=self._runtime_last_button_source,
                    voice_active=self._runtime_voice_active,
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
    ) -> None:
        with self._runtime_status_lock:
            if raw_input_state is not None:
                self._runtime_raw_input_state = str(raw_input_state)
            if hid_tap_state is not None:
                self._runtime_hid_tap_state = str(hid_tap_state)
        self._publish_runtime_status()

    def _set_runtime_voice_active(self, active: bool) -> None:
        active = bool(active)
        with self._runtime_status_lock:
            if active == self._runtime_voice_active:
                return
            self._runtime_voice_active = active
        self._publish_runtime_status()

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
        with self._voice_trigger_lock:
            self._accept_input_events = True
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        self._set_runtime_voice_active(False)
        self._logger.info("startup: resolving RC003 identity")
        candidates = await ble_transport_winrt.discover_candidates()
        # A sole exact identity match remains the fast path. If Windows keeps
        # multiple paired records, accept only the unique candidate that is
        # currently reachable and exposes the ATVV voice service; zero or
        # multiple reachable devices still fail closed instead of guessing.
        candidate = await ble_transport_winrt.select_connectable_candidate(candidates)
        self._logger.info("startup: exactly one RC003 candidate resolved")

        self._ble_session = ble_transport_winrt.RC003BleSession(
            on_pcm_frame=self._on_pcm_frame,
            on_control_event=self._on_control_event,
            on_error=self._on_session_error,
            on_disconnected=self._on_disconnected,
            gain_db=float(self._config["gain_db"]),
        )
        await self._ble_session.connect(candidate)

        self._start_hid_listener()
        self._start_hid_report_tap()
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.CONNECTED
        )


    def _start_hid_listener(self) -> None:
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

        self._set_runtime_input_state(raw_input_state="starting")
        try:
            paths = raw_input_windows.enumerate_matching_device_paths()
            device_path = hid_identity.select_single_device_path(paths)
        except raw_input_windows.RawInputUnavailableError as exc:
            self._set_runtime_input_state(raw_input_state="unavailable")
            self._logger.info("startup: Raw Input unavailable; buttons disabled: %s", exc)
            return
        except hid_identity.NoDevicePathFoundError:
            self._set_runtime_input_state(raw_input_state="no_device")
            self._logger.info("startup: no RC003 HID device path found; buttons unavailable")
            return
        except hid_identity.AmbiguousDevicePathError as exc:
            self._set_runtime_input_state(raw_input_state="ambiguous")
            self._logger.info(
                "startup: buttons failing closed, ambiguous HID device paths: %s", exc
            )
            return

        self._hid_listener = raw_input_windows.RawInputButtonListener(self._on_button_event)
        set_physical_bindings = getattr(
            self._hid_listener, "set_physical_bindings", None
        )
        if callable(set_physical_bindings):
            set_physical_bindings(self._bindings.get("physical_bindings", {}))
        set_raw_event_callback = getattr(self._hid_listener, "set_raw_event_callback", None)
        if set_raw_event_callback is not None:
            set_raw_event_callback(self._on_raw_input_event)
        try:
            self._hid_listener.start(device_path)
        except raw_input_windows.RawInputUnavailableError as exc:
            if self._hid_listener.is_running:
                self._set_runtime_input_state(raw_input_state="failed_running")
                self._logger.exception(
                    "startup: Raw Input listener failed to start but is still running; "
                    "owner retained for cleanup to retry"
                )
                raise
            self._logger.info("startup: Raw Input listener failed to start: %s", exc)
            self._hid_listener = None
            self._set_runtime_input_state(raw_input_state="failed")
            return
        self._set_runtime_input_state(raw_input_state="ready")

        # RC003's voice key is also reported as a legacy F5. Keep that record
        # out of the foreground application, but never turn it into the host
        # voice shortcut inside the low-level hook. HID/ATVV/audio own the
        # voice session lifecycle.
        self._legacy_key_suppressor = legacy_key_suppressor_windows.LegacyKeySuppressor(
            {0x74},
            on_key_event=self._on_legacy_key_event,
            rc003_vk_codes=frozenset(raw_input_windows.KEYBOARD_VK_TO_BUTTON),
        )
        self._legacy_voice_event_generation += 1
        try:
            self._legacy_key_suppressor.start()
            self._logger.info("startup: RC003 voice legacy-key guard enabled")
        except legacy_key_suppressor_windows.LegacyKeySuppressorUnavailableError as exc:
            if self._legacy_key_suppressor.is_running:
                self._logger.exception(
                    "startup: RC003 voice legacy-key guard failed to start but is "
                    "still running; owner retained for cleanup to retry"
                )
                raise
            self._logger.warning("startup: RC003 voice legacy-key guard unavailable: %s", exc)
            self._legacy_key_suppressor = None

    def _start_hid_report_tap(self) -> None:
        """Start the upstream-derived tap for usages Windows drops.

        This is independent of the normal Raw Input listener.  A missing or
        unverified Gadget is a button-only degradation and must not prevent
        BLE voice from starting.
        """

        self._set_runtime_input_state(hid_tap_state="starting")
        tap = frida_compat.RC003HidReportTap(
            self._on_direct_hid_report,
            status_handler=self._on_hid_tap_status,
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

    def _on_hid_tap_status(self, status: str, detail: str) -> None:
        self._set_runtime_input_state(hid_tap_state=status)
        message = "RC003 HID report tap state: %s"
        args = [status]
        if detail:
            message += " detail=%s"
            args.append(detail)
        if status in {
            frida_compat.HidTapState.FAILED.value,
            frida_compat.HidTapState.UNHEALTHY.value,
        }:
            self._logger.warning(message, *args)
            with self._direct_hid_lock:
                stale_usages = set(self._direct_hid_usages)
                self._direct_hid_usages.clear()
            self._direct_hid_tap_active = False
            for usage in sorted(stale_usages):
                button = frida_compat.TAP_USAGE_TO_BUTTON.get(usage)
                if button is not None:
                    self._on_button_event(button, False, event_source="hid_tap")
        else:
            self._logger.info(message, *args)

    def _on_direct_hid_report(self, report_id: int, payload: bytes) -> None:
        """Translate every RC003 keyboard HID usage into button edges.

        The tap observes the full keyboard report on its own socket thread,
        which the low-level keyboard hook does not block.  Arming the
        duplicate suppressor from this side channel makes the arming edge
        arrive inside the hook's wait window (the WM_INPUT arm arrives too
        late, ~63-72ms after the hook on this device). The microphone usage
        also enters the shared edge path now that mic may own an ordinary
        mapping; app-level source tracking collapses its F5/Raw/HID reports.
        """

        if not self._accept_input_events or report_id != 1 or len(payload) != 6:
            return
        active = {
            int.from_bytes(payload[index : index + 2], "little")
            for index in range(0, len(payload), 2)
        } & set(frida_compat.TAP_USAGE_TO_BUTTON)
        with self._direct_hid_lock:
            previous = self._direct_hid_usages
            if active == previous:
                return
            pressed = active - previous
            released = previous - active
            self._direct_hid_usages = set(active)
        if active:
            self._direct_hid_tap_active = True
        for usage in sorted(pressed):
            button = frida_compat.TAP_USAGE_TO_BUTTON[usage]
            self._logger.info(
                "RC003 direct HID usage down: 0x%04x -> %s",
                usage,
                button,
            )
            self._arm_from_direct_usage(usage, True)
            self._on_button_event(button, True, event_source="hid_tap")
        for usage in sorted(released):
            button = frida_compat.TAP_USAGE_TO_BUTTON[usage]
            self._logger.info(
                "RC003 direct HID usage up: 0x%04x -> %s",
                usage,
                button,
            )
            self._arm_from_direct_usage(usage, False)
            self._on_button_event(button, False, event_source="hid_tap")

    def _arm_from_direct_usage(self, usage: int, is_pressed: bool) -> None:
        """Arm the exact physical edge seen by the tap's socket thread.

        Uses the same vk/scan/extended values the low-level hook observes for
        that physical key, so ``consume_armed_key_event`` matches regardless
        of whether the arm arrived from Raw Input or from the tap.
        """

        suppressor = self._legacy_key_suppressor
        if suppressor is None:
            return
        key = frida_compat.TAP_USAGE_TO_KEY.get(usage)
        if key is None:
            return
        vk_code, make_code, extended = key
        if not self._accept_input_events:
            return
        if vk_code == 0x74:
            return
        suppressor.arm_tracked_key_event(vk_code, make_code, extended, is_pressed)


    async def _cleanup_once(self) -> None:
        """Every step is independently attempted: one failing must never
        skip the rest (XRBM-014 review RETRY P1 #4). XRBM-019 P1 #2/#5: the
        HID listener and BLE session owners are only cleared to ``None``
        when their own stop()/close() call reports success - if either
        reports its resource is still alive (raises), the owner reference
        is deliberately retained so no later code can mistake a still-
        running listener/session for a clean slate, and this method raises
        ``CleanupIncompleteError`` once every step has still been
        attempted (see the module docstring's "Cleanup ownership" note for
        how that ends the connect/retry loop).
        """

        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        failures: List[str] = []
        with self._voice_trigger_lock:
            self._accept_input_events = False
            self._legacy_voice_event_generation += 1
            self._legacy_f5_voice_blocked_until_up = False
        with self._legacy_f5_hook_lock:
            self._legacy_f5_is_down = False

        if self._hid_report_tap is not None:
            try:
                self._hid_report_tap.stop()
                self._hid_report_tap = None
                self._set_runtime_input_state(hid_tap_state="stopped")
            except Exception:
                self._set_runtime_input_state(hid_tap_state="failed_stopping")
                self._logger.exception("cleanup: stopping the RC003 HID report tap failed")
                failures.append("RC003 HID report tap did not stop; owner retained")
        else:
            self._set_runtime_input_state(hid_tap_state="stopped")
        with self._direct_hid_lock:
            self._direct_hid_usages.clear()
        self._direct_hid_tap_active = False
        self._key_detection_suppressed_buttons.clear()
        with self._ordinary_mic_lock:
            self._ordinary_mic_sources_down.clear()
            self._ordinary_mic_late_sources_down.clear()
            self._ordinary_mic_sources_seen.clear()
            self._ordinary_mic_release_guard_until = 0.0
            self._ordinary_mic_gesture_active = False
        with self._key_detection_mic_lock:
            self._reset_key_detection_mic_gesture_locked()

        # Cancel gesture timers before stopping Raw Input. The listener's
        # forced releases then clear the dispatcher state without a late
        # double/long callback racing the next connection generation.
        self._button_combos.reset()
        self._button_gestures.reset()

        try:
            with self._voice_trigger_lock:
                self._voice_audio_start_fallback_pending = False
                self._voice_raw_input_trigger_pending = False
                self._voice_audio_stream_active = False
                self._voice_audio_stop_processed = False
                self._voice_pcm_forwarding_enabled = False
                flush_result = self._flush_playback_writer_locked("cleanup")
                if not flush_result.completed:
                    failures.append(
                        "audio playback queue did not flush; owner retained"
                    )
                self._unsolicited_mic_close_pending = False
                self._finish_voice_mic_gesture()
                if self._voice_hotkey_release_pending is not None:
                    if self._release_pending_voice_hotkey():
                        self._voice.cancel_pending()
                    else:
                        failures.append(
                            "voice hotkey safety release did not fully deliver; state retained"
                        )
                else:
                    reset_action = self._voice.reset()
                    if reset_action is not None and not self._apply_voice_action(
                        reset_action
                    ):
                        # _apply_voice_action() already logged the specific failure.
                        # reset() already cleared the controller's own pending
                        # state before we knew delivery would fail - restore it so
                        # a held shortcut isn't recorded as released while it may
                        # still be physically down (XRBM-019 review round 1 P1 #4).
                        self._voice.restore_pending(reset_action)
                        failures.append(
                            "voice hotkey release did not fully deliver; state retained"
                        )
                self._wetype_voice_control.clear()
                self._voice_focus_before = None
                self._voice_focus_provider = ""
                self._voice_focus_submit_method = ""
        except Exception:
            self._logger.exception("cleanup: releasing the voice hotkey failed")
            failures.append("voice hotkey cleanup failed; state retained")

        if self._button_key_release_pending is not None:
            if self._release_pending_button_keys():
                self._button_key_release_pending = None
            else:
                failures.append(
                    "ordinary button key safety release did not fully deliver; "
                    "state retained"
                )

        if self._hid_listener is not None:
            try:
                self._hid_listener.stop()
                self._hid_listener = None
                self._set_runtime_input_state(raw_input_state="stopped")
            except Exception:
                self._set_runtime_input_state(raw_input_state="failed_stopping")
                self._logger.exception("cleanup: stopping the Raw Input listener failed")
                failures.append("Raw Input listener did not stop; owner retained")
                # self._hid_listener is intentionally NOT cleared here: it
                # may still be a live thread/window.
        else:
            self._set_runtime_input_state(raw_input_state="stopped")

        if self._legacy_key_suppressor is not None:
            try:
                self._legacy_key_suppressor.stop()
                self._legacy_key_suppressor = None
            except Exception:
                self._logger.exception("cleanup: stopping RC003 voice legacy-key guard failed")
                failures.append("RC003 voice legacy-key guard did not stop; owner retained")

        if self._ble_session is not None:
            try:
                await self._ble_session.close()
                self._ble_session = None
            except Exception:
                self._logger.exception("cleanup: closing the BLE session failed")
                failures.append("BLE session did not fully close; owner retained")
                # self._ble_session is intentionally NOT cleared here either.

        playback_writer_stopped = True
        if self._playback_writer is not None:
            playback_writer_stopped = self._playback_writer.stop()
            if playback_writer_stopped:
                self._playback_writer = None
            else:
                failures.append(
                    "audio playback writer did not stop; owner retained"
                )

        if self._playback is not None and playback_writer_stopped:
            try:
                self._playback.close()
                self._playback = None
            except Exception:
                self._logger.exception("cleanup: closing audio playback failed")
                failures.append("audio playback did not fully close; owner retained")
                # self._playback is intentionally NOT cleared here either -
                # it owns a PortAudio stream; discarding the reference would
                # hide an incompletely closed resource and let a reconnect
                # open a second sink over it (XRBM-019 review round 1 P1
                # #5).

        if not failures:
            with self._voice_trigger_lock:
                self._apply_pending_voice_settings_if_idle_locked()

        self._logger.info("cleanup: attempted release of hotkey state and BLE/HID/audio")

        if failures:
            raise CleanupIncompleteError(
                "cleanup could not release all owned resources: " + "; ".join(failures)
            )

    # -- disconnect / error callbacks: hand off to the supervisor ----------

    def _on_disconnected(self) -> None:
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        self._logger.info("BLE reported disconnected; requesting reconnect")
        self._supervisor.request_reconnect()

    def _on_session_error(self, exc: BaseException) -> None:
        self._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        self._logger.info("ATVV protocol error, requesting reconnect: %s", exc)
        self._supervisor.request_reconnect()

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
        return _VOICE_HOTKEY_BACKEND_MARKED

    def _voice_hotkey_uses_toggle_protocol(self) -> bool:
        return (
            self._voice_hotkey_active_backend
            or self._configured_voice_hotkey_backend()
        ) == _VOICE_HOTKEY_BACKEND_WETYPE

    def _prepare_voice_mapping_locked(
        self,
        button_id: str,
        action: key_mapping.ButtonAction,
    ) -> bool:
        if self._voice_mode_for_primary_button(button_id, action) is None:
            self._logger.warning(
                "voice mapping ignored: only the physical microphone button "
                "supports hold-to-talk; button=%s configured=%s",
                button_id,
                self._configured_voice_buttons(),
            )
            return False
        mode = key_mapping.VoiceTriggerMode.HOLD
        try:
            voice_hotkey = hotkey.HotkeySpec.parse(
                self._voice_hotkey_text_for_mode(mode)
            )
            win32_keys.resolve_vk_codes(
                tuple(voice_hotkey.modifiers) + (voice_hotkey.key,)
            )
        except (hotkey.HotkeyParseError, win32_keys.UnknownKeyTokenError) as exc:
            self._logger.warning(
                "voice mapping ignored: invalid %s shortcut: %s",
                mode.value,
                exc,
            )
            return False
        requested = (mode, voice_hotkey.serialize())
        current = (self._voice.trigger_mode, self._voice_hotkey.serialize())
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
            self._track_legacy_f5_down_snapshot_locked()
            return False
        self._voice_mic_gesture_active = True
        self._voice_mic_gesture_audio_started = source == "audio_started"
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = physical_down
        self._voice_mic_gesture_hid_released = False
        self._voice_mic_gesture_started_without_direct_hid = (
            not self._direct_hid_tap_active
        )
        self._voice_mic_gesture_direct_hid_seen = (
            physical_down and source == "hid_tap"
        )
        self._voice_mic_gesture_legacy_f5_released = False
        self._voice_mic_gesture_sources_down = {source} if physical_down else set()
        self._track_legacy_f5_down_snapshot_locked()
        return True

    def _finish_voice_mic_gesture(self) -> None:
        """Release the current cross-source mic gesture latch."""

        self._voice_mic_gesture_active = False
        self._voice_mic_gesture_audio_started = False
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = False
        self._voice_mic_gesture_hid_released = False
        self._voice_mic_gesture_started_without_direct_hid = False
        self._voice_mic_gesture_direct_hid_seen = False
        self._voice_mic_gesture_legacy_f5_released = False
        self._voice_mic_gesture_sources_down.clear()

    def _rollover_completed_voice_mic_gesture_locked(self, next_source: str) -> bool:
        """Detach stale sources once a matched HID release proves a new press."""

        startup_hid_handoff = (
            next_source == "hid_tap"
            and self._voice_mic_gesture_started_without_direct_hid
            and not self._voice_mic_gesture_direct_hid_seen
        )
        if not (
            self._voice_mic_gesture_active
            and self._voice_mic_gesture_audio_stopped
            and not self._voice.active
            and (
                self._voice_mic_gesture_hid_released
                or startup_hid_handoff
            )
        ):
            return False
        stale_sources = sorted(self._voice_mic_gesture_sources_down)
        if "legacy_f5" in self._voice_mic_gesture_sources_down:
            self._legacy_f5_voice_blocked_until_up = True
        self._finish_voice_mic_gesture()
        self._logger.info(
            "voice completed gesture rolled over for new source=%s; "
            "detached late duplicate sources=%s",
            next_source,
            stale_sources,
        )
        return True

    def _track_legacy_f5_down_snapshot_locked(self) -> bool:
        """Attach the current F5 pair to an existing voice gesture only.

        The caller holds ``_voice_trigger_lock``. The hook lock is held only
        long enough to copy its down latch; the low-level hook never acquires
        the voice lock, so this lock order has no reverse path.
        """

        if (
            not self._voice_mic_gesture_active
            or self._voice_mic_gesture_direct_hid_seen
            or self._legacy_f5_voice_blocked_until_up
            or self._voice_mic_gesture_legacy_f5_released
            or "legacy_f5" in self._voice_mic_gesture_sources_down
        ):
            return False
        with self._legacy_f5_hook_lock:
            legacy_f5_is_down = self._legacy_f5_is_down
        if not legacy_f5_is_down:
            return False
        self._voice_mic_gesture_sources_down.add("legacy_f5")
        self._logger.info(
            "voice legacy F5 down attached for release bookkeeping only"
        )
        return True

    def _retire_legacy_f5_on_matched_hid_release_locked(self) -> None:
        """Detach a duplicate F5 pair once matching HID already proved release."""

        if "legacy_f5" not in self._voice_mic_gesture_sources_down:
            return
        self._voice_mic_gesture_sources_down.discard("legacy_f5")
        self._voice_mic_gesture_legacy_f5_released = False
        self._legacy_f5_voice_blocked_until_up = True
        self._logger.info(
            "voice legacy F5 pair retired by matched HID release; late up quarantined"
        )

    def _release_hold_voice_on_physical_release_locked(
        self,
        reason: str,
    ) -> bool:
        """Release HOLD shortcuts without depending solely on AUDIO_STOP."""

        if (
            self._voice_hotkey_uses_toggle_protocol()
            and self._voice_audio_stream_active
        ):
            self._logger.info(
                "voice provider toggle stop deferred until audio stop on %s",
                reason,
            )
            return True

        action = self._voice.on_mic_button_released()
        if action is None:
            return True
        self._voice_raw_input_trigger_pending = False
        self._voice_audio_start_fallback_pending = False
        self._voice_pcm_forwarding_enabled = False
        if self._apply_voice_action(action):
            self._logger.info("voice hold hotkey released on %s", reason)
            if not self._voice_audio_stream_active:
                self._set_runtime_voice_active(False)
                self._log_voice_submission_observation()
            return True

        self._voice.restore_pending(action)
        self._logger.info(
            "voice hold hotkey release failed on %s; state retained, "
            "requesting reconnect",
            reason,
        )
        self._supervisor.request_reconnect()
        return False

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
        with self._voice_trigger_lock:
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

    def _on_legacy_key_event(self, vk_code: int, is_pressed: bool) -> None:
        """Deduplicate and queue an already-suppressed legacy F5 edge.

        The callback runs inside WH_KEYBOARD_LL. It only updates the private F5
        latch and releases that lock before queueing application work, so it
        never waits for PortAudio or the voice state machine. Voice code may
        briefly snapshot the latch later. The queued edge may support key
        detection or an ordinary mic mapping, but never owns the voice shortcut
        lifecycle.
        """

        if vk_code != 0x74:
            return
        with self._legacy_f5_hook_lock:
            # Cleanup can close input after the hook callback passed its first
            # instruction but before it acquired this lock. Recheck here so a
            # stale callback cannot re-arm the down latch after cleanup reset it.
            if not self._accept_input_events:
                return
            if is_pressed:
                if self._legacy_f5_is_down:
                    return
                self._legacy_f5_is_down = True
            elif not self._legacy_f5_is_down:
                return
            else:
                self._legacy_f5_is_down = False
            generation = self._legacy_voice_event_generation
        try:
            self._event_loop.call_soon_threadsafe(
                self._dispatch_legacy_key_event,
                generation,
                is_pressed,
            )
        except RuntimeError:
            # The original F5 remains swallowed while the loop is closing.
            pass

    def _dispatch_legacy_key_event(
        self,
        generation: int,
        is_pressed: bool,
    ) -> None:
        if (
            generation != self._legacy_voice_event_generation
            or not self._accept_input_events
        ):
            return
        if not is_pressed:
            with self._voice_trigger_lock:
                if self._legacy_f5_voice_blocked_until_up:
                    self._legacy_f5_voice_blocked_until_up = False
                    self._logger.info(
                        "voice retired legacy F5 up consumed before dispatch"
                    )
                    return
        self._reload_settings_if_changed()
        mic_action = self._primary_button_action("mic")
        if self._voice_mode_for_primary_button("mic", mic_action) is None:
            self._on_button_event("mic", is_pressed, event_source="legacy_f5")
            return
        detection_handled, detection_captured = self._handle_key_detection_mic_event(
            "physical_down" if is_pressed else "physical_up",
            "legacy_f5",
        )
        if detection_handled:
            if detection_captured:
                self._logger.info(
                    "key detection captured button=mic source=legacy_f5; "
                    "voice action suppressed"
                )
            return
        tracked = self._handle_voice_legacy_f5_edge(is_pressed)
        if is_pressed and not tracked:
            self._logger.info(
                "voice legacy F5 swallowed; HID/ATVV owns the voice session"
            )

    def _handle_voice_legacy_f5_edge(self, is_pressed: bool) -> bool:
        """Track an F5 pair only as release bookkeeping for an owned gesture.

        Some Windows stacks report the RC003 microphone down through Raw Input
        but omit its matching up while still producing the global legacy F5 up.
        F5 may keep that already-owned gesture open long enough to absorb a late
        Raw Input down, but it never opens or releases the host shortcut.
        """

        with self._voice_trigger_lock:
            if is_pressed:
                if (
                    self._voice_mic_gesture_direct_hid_seen
                    or self._legacy_f5_voice_blocked_until_up
                    or not self._voice_mic_gesture_active
                    or self._voice_mic_gesture_legacy_f5_released
                    or "legacy_f5" in self._voice_mic_gesture_sources_down
                ):
                    return False
                self._voice_mic_gesture_sources_down.add("legacy_f5")
                self._logger.info(
                    "voice legacy F5 down attached for release bookkeeping only"
                )
                return True

            if (
                not self._voice_mic_gesture_active
                or "legacy_f5" not in self._voice_mic_gesture_sources_down
            ):
                return False

            self._voice_mic_gesture_sources_down.discard("legacy_f5")
            self._voice_mic_gesture_legacy_f5_released = True
            self._logger.info(
                "voice legacy F5 up recorded for release bookkeeping only"
            )

            if (
                self._voice_mic_gesture_audio_stopped
                and not self._voice.active
                and not self._voice_mic_gesture_direct_hid_seen
                and self._voice_mic_gesture_sources_down == {"hid"}
            ):
                self._voice_mic_gesture_sources_down.clear()
                self._logger.info(
                    "voice missing Raw Input mic up cleared after legacy F5 release"
                )
            if (
                self._voice_mic_gesture_active
                and not self._voice_mic_gesture_sources_down
                and not self._voice.active
                and (
                    self._voice_mic_gesture_audio_stopped
                    or not self._voice_mic_gesture_audio_started
                )
            ):
                self._finish_voice_mic_gesture()
            self._apply_pending_voice_settings_if_idle_locked()
            return True

    def _on_raw_input_event(self, event: raw_input_windows.RawInputEvent) -> None:
        """Arm the exact original keyboard edge for duplicate suppression.

        The selected RC003 Raw Input listener is device-scoped; the global
        low-level keyboard hook is not.  Passing the observed VKey/MakeCode
        pair across this seam lets the hook swallow only the remote's
        original arrow/Enter/Home/consumer event before the injected mapping
        action is delivered.
        """

        if not self._accept_input_events:
            return
        suppressor = self._legacy_key_suppressor
        if (
            suppressor is None
            or event.source != "keyboard"
            or event.button_id == "mic"
            or event.button_id is None
            or event.vkey is None
            or event.make_code is None
        ):
            return
        # While the Frida tap side channel is reporting full keyboard
        # snapshots, it already arms and dispatches every ordinary button on
        # its own socket thread.  Stand the Raw Input path down so one
        # physical edge is not armed and dispatched twice.
        if self._direct_hid_tap_active:
            return
        # Only arm a physical edge when this RC003 button has at least one
        # configured ordinary gesture.  Unknown usages and deliberately
        # unbound controls must remain ordinary Windows input instead of
        # being swallowed with no replacement action.
        if not any(
            self._is_button_action_configured(event.button_id, trigger)
            for trigger in button_gesture.ButtonTrigger
        ) and not self._is_button_combo_participant(event.button_id):
            return
        # RAWKEYBOARD uses RI_KEY_E0 (0x02) for the extended prefix; the
        # low-level hook uses LLKHF_EXTENDED (0x01).
        suppressor.arm_tracked_key_event(
            event.vkey,
            event.make_code,
            bool((event.flags or 0) & 0x02),
            event.is_pressed,
        )

    # -- HID button events --------------------------------------------------

    @staticmethod
    def _settings_file_mtime_ns(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1

    def _voice_settings_idle_locked(self) -> bool:
        return not (
            self._voice.active
            or self._voice_mic_gesture_active
            or self._voice_audio_stream_active
            or self._ordinary_mic_gesture_active
            or self._voice_hotkey_release_pending is not None
        )

    def _apply_voice_settings_locked(
        self,
        trigger_mode: key_mapping.VoiceTriggerMode,
        voice_hotkey: hotkey.HotkeySpec,
    ) -> None:
        if trigger_mode != key_mapping.VoiceTriggerMode.HOLD:
            raise ValueError("RC003 voice settings support hold-to-talk only")
        self._voice = voice_controller.VoiceController()
        self._voice_hotkey = voice_hotkey
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
        if self._pending_bindings is not None:
            self._button_combos.reset()
            self._bindings = self._pending_bindings
            self._pending_bindings = None
            self._removed_voice_bindings = dict(
                self._bindings.get(config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {})
            )
            self._logger.info(
                "deferred settings mappings applied after voice became idle"
            )

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
            refreshed_bindings = config.load_key_bindings(self._bindings_path)
            removed_voice_bindings = config.normalize_voice_product_boundary(
                refreshed_config,
                refreshed_bindings,
            )
            trigger_mode = key_mapping.VoiceTriggerMode.HOLD
            refreshed_hotkey_text = str(
                refreshed_config.get("voice_hotkey", "")
            ).strip()
            if not refreshed_hotkey_text:
                refreshed_hotkey_text = key_mapping.voice_hotkey_for_trigger_mode(
                    trigger_mode
                )
            voice_hotkey = hotkey.HotkeySpec.parse(refreshed_hotkey_text)
        except Exception as exc:  # noqa: BLE001 - keep the last valid settings
            self._logger.warning("settings reload skipped: %s", exc)
            with self._voice_trigger_lock:
                self._config_mtime_ns = current_config_mtime_ns
                self._bindings_mtime_ns = current_bindings_mtime_ns
            return

        with self._voice_trigger_lock:
            refreshed_settings = (trigger_mode, voice_hotkey.serialize())
            current_settings = (
                self._voice.trigger_mode,
                self._voice_hotkey.serialize(),
            )
            if self._voice_settings_idle_locked():
                self._config = refreshed_config
                self._button_combos.reset()
                self._bindings = refreshed_bindings
                self._removed_voice_bindings = removed_voice_bindings
                self._pending_config = None
                self._pending_bindings = None
                self._pending_voice_settings = None
                if refreshed_settings != current_settings:
                    self._apply_voice_settings_locked(trigger_mode, voice_hotkey)
            else:
                self._pending_config = refreshed_config
                self._pending_bindings = refreshed_bindings
                self._pending_voice_settings = (trigger_mode, voice_hotkey)
                self._logger.info(
                    "settings reload deferred until active voice session is idle"
                )
            self._config_mtime_ns = current_config_mtime_ns
            self._bindings_mtime_ns = current_bindings_mtime_ns
        if removed_voice_bindings:
            self._logger.warning(
                "legacy voice mappings disabled until user reselects actions: %s",
                sorted(removed_voice_bindings),
            )
        if self._pending_bindings is None:
            self._logger.info("settings reloaded from disk")

    def _handle_ordinary_mic_edge(
        self, event_source: str, is_pressed: bool
    ) -> None:
        """Collapse F5/HID duplicates before ordinary mic gesture dispatch."""

        dispatch = False
        with self._ordinary_mic_lock:
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
                    self._ordinary_mic_sources_seen.add(event_source)
                    self._logger.info(
                        "ordinary mic late duplicate ignored: source=%s",
                        event_source,
                    )
                    return
                self._ordinary_mic_sources_down.add(event_source)
                self._ordinary_mic_sources_seen = {event_source}
                self._ordinary_mic_release_guard_until = 0.0
                dispatch = True
            else:
                if event_source in self._ordinary_mic_late_sources_down:
                    self._ordinary_mic_late_sources_down.discard(event_source)
                    return
                if event_source not in self._ordinary_mic_sources_down:
                    return
                self._ordinary_mic_sources_down.discard(event_source)
                dispatch = not self._ordinary_mic_sources_down
                if dispatch:
                    self._ordinary_mic_release_guard_until = (
                        time.monotonic() + _ORDINARY_MIC_RELEASE_GUARD_SECONDS
                    )
            self._ordinary_mic_gesture_active = bool(
                self._ordinary_mic_sources_down
            )
        if not dispatch:
            return
        if is_pressed:
            self._button_gestures.press("mic")
        else:
            self._button_gestures.release("mic")
            with self._voice_trigger_lock:
                self._apply_pending_voice_settings_if_idle_locked()

    def _on_button_event(
        self,
        button_id: str,
        is_pressed: bool,
        *,
        event_source: str = "hid",
    ) -> None:
        if not self._accept_input_events:
            return
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
            return
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
            self._logger.info(
                "key detection captured button=%s; mapped action suppressed",
                button_id,
            )
            return
        if button_id == "mic" and self._ordinary_mic_gesture_active:
            self._handle_ordinary_mic_edge(event_source, is_pressed)
            return
        self._reload_settings_if_changed()

        if button_id in self._removed_voice_bindings:
            if not is_pressed:
                self._button_gestures.release(button_id)
            else:
                self._logger.warning(
                    "button action suppressed until removed voice mapping is reselected: %s",
                    button_id,
                )
            return

        primary_action = self._primary_button_action(button_id)
        voice_mode = self._voice_mode_for_primary_button(
            button_id,
            primary_action,
        )

        if button_id == "mic":
            if voice_mode is None:
                self._handle_ordinary_mic_edge(event_source, is_pressed)
                return
            if event_source == "legacy_f5":
                self._logger.info(
                    "voice legacy F5 edge ignored after suppression: pressed=%s",
                    is_pressed,
                )
                return

            if not is_pressed:
                with self._voice_trigger_lock:
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

                    self._track_legacy_f5_down_snapshot_locked()
                    matched_hid_released = event_source in {"hid", "hid_tap"}
                    if matched_hid_released:
                        self._voice_mic_gesture_hid_released = True
                        self._retire_legacy_f5_on_matched_hid_release_locked()
                    self._voice_mic_gesture_sources_down.discard(event_source)
                    if (
                        self._voice.trigger_mode
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
            with self._voice_trigger_lock:
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
                if self._voice.active:
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
                if not self._voice.active:
                    self._voice_raw_input_trigger_pending = False
            return

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
        action = key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK,
        )
        return key_mapping.action_allows_repeat(action)

    def _on_button_trigger(
        self, button_id: str, trigger: button_gesture.ButtonTrigger
    ) -> None:
        self._reload_settings_if_changed()
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
        try:
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
                win32_keys.resolve_vk_codes(action.keys)
        except (KeyError, TypeError, ValueError, win32_keys.UnknownKeyTokenError):
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
        if self._button_key_release_pending is not None:
            if not self._release_pending_button_keys():
                self._logger.info(
                    "button action suppressed: an earlier key release is still pending"
                )
                return
            self._button_key_release_pending = None

        try:
            if action.kind == key_mapping.ActionKind.DISABLED:
                return
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
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
            elif action.kind == key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE:
                try:
                    result = (
                        element_navigation_control_windows.toggle_element_navigation()
                    )
                except Exception:
                    self._logger.exception("element navigation toggle failed unexpectedly")
                    return
                if (
                    result.kind
                    == element_navigation_control_windows.ToggleResultKind.FAILED
                ):
                    self._logger.warning(
                        "element navigation toggle failed: %s",
                        result.error or "unknown_error",
                    )
            elif action.kind == key_mapping.ActionKind.QUICKER_URI:
                action_executor.open_quicker_uri(action)
            elif action_executor.is_application_action(action):
                if not open_configured_application(action):
                    self._logger.warning(
                        "application action unavailable: action=%s", action.kind.value
                    )
            # Voice actions are edge-driven in _on_button_event and never
            # enter this tap-only ordinary action executor.
        except win32_input.Win32InputUnavailableError:
            self._logger.info("button action skipped: SendInput unavailable here")
        except win32_input.InputCleanupIncompleteError:
            tokens = self._button_action_key_tokens(action)
            if tokens is not None:
                self._button_key_release_pending = tokens
            self._logger.exception(
                "button action failed and safety key-up remains pending"
            )
        except OSError:
            self._logger.exception("button action failed to fully deliver")

    @staticmethod
    def _button_action_key_tokens(
        action: key_mapping.ButtonAction,
    ) -> Optional[Tuple[str, ...]]:
        if action.kind == key_mapping.ActionKind.KEY_COMBO:
            return tuple(action.keys)
        return _BUTTON_ACTION_KEY_TOKENS.get(action.kind)

    def _release_pending_button_keys(self) -> bool:
        tokens = self._button_key_release_pending
        if tokens is None:
            return True
        try:
            win32_input.send_key_combo_up(tokens)
        except win32_input.InputCleanupIncompleteError:
            self._logger.exception("button key safety release remains incomplete")
            return False
        except win32_input.Win32InputUnavailableError:
            self._logger.info("button key safety release unavailable")
            return False
        except OSError:
            # send_key_combo_up raises ordinary OSError only after its own
            # per-key fallback confirmed every requested key-up.
            self._logger.exception(
                "button key safety release needed fallback but completed"
            )
        else:
            self._logger.info("button key safety release completed")
        return True

    # -- ATVV control-channel events (mic button + audio start/stop) ------

    def _on_control_event(self, event: object) -> None:
        if not self._accept_input_events:
            return
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
            mic_action = self._primary_button_action("mic")
            mic_voice_mode = self._voice_mode_for_primary_button(
                "mic",
                mic_action,
            )
            if mic_voice_mode is None:
                with self._voice_trigger_lock:
                    if not self._voice.active:
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
            with self._voice_trigger_lock:
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
            with self._voice_trigger_lock:
                self._logger.info("voice audio started")
                if self._voice_audio_stream_active:
                    self._logger.info(
                        "voice duplicate audio start ignored: session_id=%s",
                        event.session_id,
                    )
                    return
                self._voice_audio_stream_active = True
                self._voice_audio_stop_processed = False
                self._voice_pcm_stats.reset()
                self._voice_audio_start_fallback_pending = False
                mic_action = self._primary_button_action("mic")
                mic_voice_mode = self._voice_mode_for_primary_button(
                    "mic",
                    mic_action,
                )
                if not self._voice.active and mic_voice_mode is None:
                    self._voice_pcm_forwarding_enabled = False
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
                    not self._voice.active
                    and not self._prepare_voice_mapping_locked("mic", mic_action)
                ):
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
                elif not self._voice.active:
                    self._logger.info("voice audio start used as microphone trigger")
                    if self._begin_voice_mic_gesture("audio_started"):
                        self._handle_mic_button_pressed(send_device_open=False)
                        self._voice_audio_start_fallback_pending = self._voice.active
        elif isinstance(event, AudioStopped):
            detection_handled, _ = self._handle_key_detection_mic_event(
                "audio_stopped",
                "audio_started",
            )
            if detection_handled:
                self._logger.info(
                    "key detection mic audio stopped; voice state unchanged"
                )
                return
            with self._voice_trigger_lock:
                if (
                    not self._voice_audio_stream_active
                    and self._voice_audio_stop_processed
                ):
                    self._logger.info("voice duplicate audio stop ignored")
                    return
                self._voice_audio_stream_active = False
                self._voice_audio_stop_processed = True
                self._voice_pcm_forwarding_enabled = False
                self._set_runtime_voice_active(False)
                flush_result = self._flush_playback_writer_locked("audio stop")
                if flush_result.error is not None:
                    self._logger.error(
                        "voice playback flush completed with failure: %s",
                        flush_result.error,
                    )
                self._logger.info("voice audio stopped")
                stats = self._voice_pcm_stats.summary()
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
                    self._playback, "timing_snapshot", None
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
                    self._track_legacy_f5_down_snapshot_locked()
                    self._voice_mic_gesture_audio_started = False
                    self._voice_mic_gesture_audio_stopped = True
                action = self._voice.on_audio_stopped()
                action_applied = (
                    True if action is None else self._apply_voice_action(action)
                )
                if action is not None and not action_applied:
                    # Same rule as _cleanup_once(): on_audio_stopped() already
                    # cleared the controller's pending state before we knew
                    # whether the closing key-up actually delivered. A failure
                    # here must not
                    # be recorded as a clean close - restore the owed state and
                    # fail closed by requesting a reconnect, the same way a BLE
                    # disconnect or a playback write failure does (XRBM-019
                    # review round 1 P1 #4).
                    self._voice.restore_pending(action)
                    self._logger.info(
                        "voice closing action failed to fully deliver; state retained, "
                        "requesting reconnect"
                    )
                    self._supervisor.request_reconnect()
                else:
                    if (
                        self._voice_mic_gesture_active
                        and self._voice_mic_gesture_legacy_f5_released
                        and not self._voice_mic_gesture_direct_hid_seen
                        and self._voice_mic_gesture_sources_down == {"hid"}
                    ):
                        self._voice_mic_gesture_sources_down.clear()
                        self._logger.info(
                            "voice missing Raw Input mic up cleared at audio stop "
                            "after legacy F5 release"
                        )
                    if (
                        self._voice_mic_gesture_active
                        and self._voice_mic_gesture_audio_stopped
                        and not self._voice_mic_gesture_sources_down
                    ):
                        self._finish_voice_mic_gesture()
                self._log_voice_submission_observation()
                self._apply_pending_voice_settings_if_idle_locked()

    def _handle_mic_button_pressed(
        self,
        *,
        send_device_open: bool = True,
    ) -> None:
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

        if not self._accept_input_events:
            self._voice_pcm_forwarding_enabled = False
            return

        if self._ble_session is None:
            self._voice_pcm_forwarding_enabled = False
            self._logger.info("voice ignored: BLE voice session is not connected")
            return

        if self._voice_hotkey_release_pending is not None:
            if not self._release_pending_voice_hotkey():
                self._voice_pcm_forwarding_enabled = False
                self._logger.info(
                    "voice failing closed: an earlier hotkey release is still pending"
                )
                return
            self._voice_hotkey_release_pending = None

        if not self._open_playback_for_new_session():
            self._voice_pcm_forwarding_enabled = False
            self._logger.info(
                "voice failing closed: no usable output endpoint; hotkey/MIC_OPEN suppressed"
            )
            return

        self._capture_voice_focus_before()
        action = self._voice.on_mic_button_pressed()
        action_delivered = self._apply_voice_action(action)
        if not action_delivered:
            self._voice_focus_before = None
            self._voice_focus_provider = ""
            self._voice_focus_submit_method = ""
            self._voice_pcm_forwarding_enabled = False
            # Nothing physically landed (win32_input.py's own batching already
            # rolled back any partial key-down), so clear the logical hold
            # without attempting a second delivery.
            self._voice.cancel_pending()
            self._logger.info(
                "voice failing closed: host hotkey delivery failed; device command suppressed"
            )
            return

        self._voice_pcm_forwarding_enabled = True
        self._set_runtime_voice_active(True)
        self._schedule_sogou_readiness_check()
        if send_device_open and self._ble_session is not None:
            self._ble_session.send_mic_open_threadsafe()

    def _schedule_sogou_readiness_check(self) -> None:
        provider_settings = voice_program_manager.normalize_voice_program_settings(
            self._config.get("voice_program")
        )
        if provider_settings["provider"] != voice_program_manager.VOICE_PROGRAM_SOGOU:
            return
        with self._sogou_readiness_lock:
            if self._sogou_readiness_check_running:
                return
            self._sogou_readiness_check_running = True

        def check() -> None:
            try:
                if voice_program_manager.wait_for_sogou_voice_window(timeout=0.7):
                    self._logger.info("voice program: Sogou voice window ready")
                    return
                if not self._accept_input_events:
                    return
                repair = voice_program_manager.prewarm_sogou_voice_component()
                ready_after_repair = (
                    repair.code == "started"
                    and self._accept_input_events
                    and voice_program_manager.wait_for_sogou_voice_window(timeout=0.8)
                )
                if ready_after_repair:
                    self._logger.info(
                        "voice program: Sogou voice window became ready after one prewarm"
                    )
                else:
                    self._logger.warning(
                        "voice program: Sogou process may be running but the voice "
                        "window is not ready after one prewarm; no shortcut retry sent"
                    )
            finally:
                with self._sogou_readiness_lock:
                    self._sogou_readiness_check_running = False

        try:
            threading.Thread(
                target=check,
                name="sogou-voice-readiness",
                daemon=True,
            ).start()
        except RuntimeError:
            with self._sogou_readiness_lock:
                self._sogou_readiness_check_running = False
            self._logger.exception("voice program: Sogou readiness check could not start")

    def _capture_voice_focus_before(self) -> None:
        provider_settings = voice_program_manager.normalize_voice_program_settings(
            self._config.get("voice_program")
        )
        self._voice_focus_provider = str(provider_settings["provider"])
        self._voice_focus_submit_method = (
            "wetype_panel"
            if self._configured_voice_hotkey_backend() == _VOICE_HOTKEY_BACKEND_WETYPE
            else "hotkey_hold"
        )
        snapshot = voice_interaction_diagnostics_windows.capture_focus_snapshot()
        self._voice_focus_before = snapshot
        self._logger.info(
            "voice interaction start: provider=%s method=%s foreground_pid=%s "
            "foreground_class=%s focus_class=%s text_length=%s diagnostic=%s",
            self._voice_focus_provider,
            self._voice_focus_submit_method,
            snapshot.foreground_pid,
            snapshot.foreground_class or "unknown",
            snapshot.focus_class or "unknown",
            snapshot.text_length if snapshot.text_length is not None else "unavailable",
            snapshot.error or "captured",
        )

    def _log_voice_submission_observation(self) -> None:
        before = self._voice_focus_before
        if before is None:
            return
        after = voice_interaction_diagnostics_windows.capture_focus_snapshot()
        observation = voice_interaction_diagnostics_windows.compare_submission(
            before,
            after,
        )
        self._logger.info(
            "voice interaction result: provider=%s method=%s focus=%s "
            "text_length=%s delta=%s; panel close alone does not prove text insertion",
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

    def _apply_voice_action(self, action: voice_controller.VoiceHostAction) -> bool:
        tokens = tuple(self._voice_hotkey.modifiers) + (self._voice_hotkey.key,)
        backend = self._configured_voice_hotkey_backend()
        if action == voice_controller.VoiceHostAction.KEY_UP:
            backend = (
                self._voice_hotkey_active_backend
                or self._voice_hotkey_release_pending_backend
                or backend
            )
        if backend == _VOICE_HOTKEY_BACKEND_WETYPE:
            try:
                delivered = (
                    self._wetype_voice_control.start(tokens)
                    if action == voice_controller.VoiceHostAction.KEY_DOWN
                    else self._wetype_voice_control.stop(tokens)
                )
            except win32_input.Win32InputUnavailableError:
                self._logger.info(
                    "WeType voice control skipped: no usable Windows input backend"
                )
                return False
            except win32_input.InputCleanupIncompleteError:
                self._voice_hotkey_release_pending = tokens
                self._voice_hotkey_release_pending_backend = backend
                self._logger.exception(
                    "WeType voice control failed and safety key-up remains pending"
                )
                return False
            except OSError:
                self._logger.exception("WeType voice control failed to fully deliver")
                return False
            if delivered:
                if action == voice_controller.VoiceHostAction.KEY_DOWN:
                    self._voice_hotkey_active_backend = backend
                else:
                    self._voice_hotkey_active_backend = None
                self._voice_hotkey_release_pending = None
                self._voice_hotkey_release_pending_backend = None
                return True
            self._logger.warning(
                "WeType voice control did not confirm the panel for logical %s",
                action.value,
            )
            return False

        provider_action = action
        try:
            if provider_action == voice_controller.VoiceHostAction.KEY_DOWN:
                self._voice_hotkey_release_pending = tokens
                self._voice_hotkey_release_pending_backend = backend
            self._send_voice_hotkey_action(provider_action, tokens, backend)
            if action == voice_controller.VoiceHostAction.KEY_DOWN:
                self._voice_hotkey_active_backend = backend
            if action in {
                voice_controller.VoiceHostAction.TAP,
                voice_controller.VoiceHostAction.KEY_UP,
            }:
                self._voice_hotkey_release_pending = None
                self._voice_hotkey_release_pending_backend = None
                self._voice_hotkey_active_backend = None
            return True
        except win32_input.Win32InputUnavailableError:
            if provider_action == voice_controller.VoiceHostAction.KEY_DOWN:
                self._voice_hotkey_release_pending = None
                self._voice_hotkey_release_pending_backend = None
                self._voice_hotkey_active_backend = None
            self._logger.info("voice hotkey action skipped: no usable voice input backend")
            return False
        except win32_input.InputCleanupIncompleteError:
            self._voice_hotkey_release_pending = tokens
            self._voice_hotkey_release_pending_backend = backend
            self._logger.exception(
                "voice hotkey action failed and safety key-up remains pending"
            )
            return False
        except OSError:
            if provider_action == voice_controller.VoiceHostAction.KEY_DOWN:
                self._voice_hotkey_release_pending = None
                self._voice_hotkey_release_pending_backend = None
                self._voice_hotkey_active_backend = None
            self._logger.exception("voice hotkey action failed to fully deliver")
            return False

    @staticmethod
    def _send_voice_hotkey_action(
        action: voice_controller.VoiceHostAction,
        tokens: Tuple[str, ...],
        backend: str,
    ) -> None:
        if backend == _VOICE_HOTKEY_BACKEND_WETYPE:
            if action == voice_controller.VoiceHostAction.TAP:
                win32_input.send_wetype_voice_key_combo_tap(tokens)
            elif action == voice_controller.VoiceHostAction.KEY_DOWN:
                win32_input.send_wetype_voice_key_combo_down(tokens)
            else:
                win32_input.send_wetype_voice_key_combo_up(tokens)
            return
        if action == voice_controller.VoiceHostAction.TAP:
            win32_input.send_voice_key_combo_tap(tokens)
        elif action == voice_controller.VoiceHostAction.KEY_DOWN:
            win32_input.send_voice_key_combo_down(tokens)
        else:
            win32_input.send_voice_key_combo_up(tokens)

    def _release_pending_voice_hotkey(self) -> bool:
        tokens = self._voice_hotkey_release_pending
        if tokens is None:
            return True
        backend = (
            self._voice_hotkey_release_pending_backend
            or self._voice_hotkey_active_backend
            or self._configured_voice_hotkey_backend()
        )
        try:
            self._send_voice_hotkey_action(
                voice_controller.VoiceHostAction.KEY_UP,
                tokens,
                backend,
            )
        except (win32_input.Win32InputUnavailableError, OSError):
            self._logger.exception("voice hotkey safety release failed")
            return False
        self._voice_hotkey_release_pending = None
        self._voice_hotkey_release_pending_backend = None
        self._voice_hotkey_active_backend = None
        self._logger.info("voice hotkey safety release completed")
        return True

    def _open_playback_for_new_session(self) -> bool:
        if self._playback is not None:
            if getattr(self._playback, "ready", True):
                return self._ensure_playback_writer(self._playback)
            self._logger.warning(
                "voice playback cannot reopen while a failed stream remains owned; "
                "requesting cleanup"
            )
            self._supervisor.request_reconnect()
            return False
        endpoint_name = self._config.get("output_endpoint_name") or ""
        endpoint_host_api = self._config.get("output_endpoint_host_api") or ""
        sink = None
        try:
            endpoints = audio_output.enumerate_output_endpoints()
            audio_output.resolve_selected_endpoint(endpoints, endpoint_name, endpoint_host_api)
            sink = audio_playback.EndpointPlaybackSink(endpoint_name, endpoint_host_api)
            self._playback = sink
            sink.open()
            if not self._ensure_playback_writer(sink):
                raise audio_output.AudioOutputUnavailableError(
                    "audio playback writer could not start"
                )
            timing_snapshot = getattr(sink, "timing_snapshot", None)
            timing = timing_snapshot() if callable(timing_snapshot) else None
            if timing is None:
                self._logger.info(
                    "voice playback opened: endpoint=%s host_api=%s "
                    "sample_rate=%s channels=%s",
                    endpoint_name or "unspecified",
                    endpoint_host_api or "unspecified",
                    sink.output_sample_rate_hz,
                    sink.output_channels,
                )
            else:
                self._logger.info(
                    "voice playback opened: endpoint=%s host_api=%s "
                    "sample_rate=%s channels=%s open_ms=%.2f",
                    endpoint_name or "unspecified",
                    endpoint_host_api or "unspecified",
                    sink.output_sample_rate_hz,
                    sink.output_channels,
                    timing.open_elapsed_ms,
                )
            return True
        except audio_output.AudioOutputUnavailableError as exc:
            self._logger.info("voice audio unavailable, failing closed: %s", exc)
            if sink is None or not sink.owns_stream:
                self._playback = None
            else:
                self._logger.warning(
                    "voice audio open cleanup incomplete; playback owner retained"
                )
                self._supervisor.request_reconnect()
            return False
        except Exception:
            self._logger.exception("voice audio failed to open, failing closed")
            if sink is None or not sink.owns_stream:
                self._playback = None
            else:
                self._logger.warning(
                    "voice audio open cleanup incomplete; playback owner retained"
                )
                self._supervisor.request_reconnect()
            return False

    def _ensure_playback_writer(self, sink) -> bool:
        writer = self._playback_writer
        if writer is not None:
            if writer.is_alive and writer.failure is None:
                return True
            self._logger.warning(
                "voice playback writer is unavailable; requesting cleanup"
            )
            self._supervisor.request_reconnect()
            return False
        writer = audio_playback_worker.PlaybackWriteWorker(
            lambda samples: self._write_playback_frame(sink, samples),
            self._on_playback_worker_error,
        )
        try:
            writer.start()
        except Exception:
            self._logger.exception("voice playback writer failed to start")
            return False
        self._playback_writer = writer
        return True

    def _on_playback_worker_error(self, error: BaseException) -> None:
        self._voice_pcm_forwarding_enabled = False
        self._logger.error("audio playback worker failed; failing closed: %s", error)
        self._supervisor.request_reconnect()

    def _flush_playback_writer_locked(
        self,
        reason: str,
    ) -> audio_playback_worker.PlaybackFlushResult:
        writer = self._playback_writer
        if writer is None:
            return audio_playback_worker.PlaybackFlushResult(True)
        result = writer.flush()
        if not result.completed:
            self._voice_pcm_forwarding_enabled = False
            self._logger.error(
                "audio playback flush failed during %s: %s",
                reason,
                result.error or "unknown error",
            )
            self._supervisor.request_reconnect()
        return result

    def _write_playback_frame(self, sink, samples) -> None:
        if sink is not self._playback:
            raise RuntimeError("audio playback sink changed while a write was queued")
        self._voice_pcm_stats.add(samples)
        sink.write(samples)
        if (
            self._voice_pcm_stats.frames in (1, 10)
            or self._voice_pcm_stats.frames % 200 == 0
        ):
            stats = self._voice_pcm_stats.summary()
            timing_snapshot = getattr(sink, "timing_snapshot", None)
            timing = timing_snapshot() if callable(timing_snapshot) else None
            if timing is None:
                self._logger.info(
                    "voice PCM progress: frames=%s samples=%s peak=%s rms=%.1f "
                    "mean_abs=%.1f clipped=%.3f%%",
                    stats["frames"],
                    stats["samples"],
                    stats["peak"],
                    stats["rms"],
                    stats["mean_abs"],
                    stats["clipped_pct"],
                )
            else:
                self._logger.info(
                    "voice PCM progress: frames=%s samples=%s peak=%s rms=%.1f "
                    "mean_abs=%.1f clipped=%.3f%% write_ms=%.2f "
                    "max_write_ms=%.2f underflows=%s",
                    stats["frames"],
                    stats["samples"],
                    stats["peak"],
                    stats["rms"],
                    stats["mean_abs"],
                    stats["clipped_pct"],
                    timing.last_write_elapsed_ms,
                    timing.max_write_elapsed_ms,
                    timing.underflow_count,
                )

    def _on_pcm_frame(self, samples) -> None:
        """Queue one immutable PCM frame without blocking the BLE worker."""

        with self._voice_trigger_lock:
            sink = self._playback
            if (
                sink is None
                or not self._accept_input_events
                or not self._voice_pcm_forwarding_enabled
            ):
                return
            if not self._ensure_playback_writer(sink):
                self._voice_pcm_forwarding_enabled = False
                return
            writer = self._playback_writer
            if writer is None or not writer.submit(samples):
                self._voice_pcm_forwarding_enabled = False


async def _run(
    *,
    app_factory=None,
    tray_factory=None,
    settings_launcher=None,
    show_notification_icon: bool = True,
) -> None:
    app_factory = app_factory or RC003App
    tray_factory = tray_factory or bridge_tray_windows.BridgeTray
    settings_launcher = settings_launcher or bridge_launcher.launch_settings
    app = app_factory()
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

    tray = None
    try:
        try:
            tray_kwargs = dict(
                on_open_settings=open_settings,
                on_exit_requested=request_exit,
                status_handler=lambda message: app._logger.info(
                    "notification area: %s", message
                ),
            )
            if not show_notification_icon:
                tray_kwargs["show_icon"] = False
            tray = tray_factory(**tray_kwargs)
            if tray.start():
                app._logger.info(
                    "notification area: %s started",
                    "bridge control icon"
                    if show_notification_icon
                    else "hidden bridge control",
                )
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
                try:
                    await app.stop()
                finally:
                    clear_runtime_status = getattr(app, "clear_runtime_status", None)
                    if callable(clear_runtime_status):
                        clear_runtime_status()
            finally:
                try:
                    result = (
                        element_navigation_control_windows.shutdown_element_navigation()
                    )
                    if (
                        result
                        == element_navigation_control_windows.CommandSendResult.FAILED
                    ):
                        app._logger.warning(
                            "element navigation companion did not stop cleanly"
                        )
                except Exception:
                    app._logger.exception(
                        "element navigation companion shutdown failed unexpectedly"
                    )


def main(*, show_notification_icon: bool = True) -> None:
    asyncio.run(_run(show_notification_icon=show_notification_icon))


if __name__ == "__main__":
    main()
