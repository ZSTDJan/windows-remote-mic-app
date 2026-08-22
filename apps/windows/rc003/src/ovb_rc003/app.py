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
the sink) and requests a reconnect, instead of logging indefinitely while
the device keeps streaming into nothing.

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
import threading
import time
from typing import List, Optional, Tuple

from . import (
    audio_output,
    audio_playback,
    action_executor,
    ble_transport_winrt,
    bridge_launcher,
    bridge_tray_windows,
    button_gesture,
    config,
    connection_supervisor,
    doubao_rpc,
    frida_compat,
    hid_identity,
    hotkey,
    key_detection_bridge,
    key_mapping,
    legacy_key_suppressor_windows,
    logging_setup,
    raw_input_windows,
    voice_controller,
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
    key_mapping.ActionKind.SHOW_DESKTOP: ("win", "d"),
    key_mapping.ActionKind.CONTEXT_MENU: ("apps",),
    key_mapping.ActionKind.APP_SWITCHER: ("alt", "tab"),
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: ("volume_up",),
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: ("volume_down",),
    key_mapping.ActionKind.SYSTEM_VOLUME_MUTE: ("volume_mute",),
    key_mapping.ActionKind.PLAY_PAUSE: ("media_play_pause",),
}

_KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS = 1.0
_KEY_DETECTION_MIC_MAX_SECONDS = 10.0
_VOICE_TOGGLE_REOPEN_FALLBACK_SECONDS = 0.2
_VOICE_TOGGLE_REOPEN_ECHO_GUARD_SECONDS = 0.25
_MAPPED_VOICE_RELEASE_GUARD_SECONDS = 0.12


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
        self._bindings_mtime_ns = self._settings_file_mtime_ns(self._bindings_path)
        self._button_gestures = button_gesture.ButtonGestureDispatcher(
            is_action_configured=self._is_button_action_configured,
            is_repeatable=self._is_button_repeatable,
            on_trigger=self._on_button_trigger,
        )
        self._logger: logging.Logger = logging_setup.get_logger(self._config_root)
        self._voice = voice_controller.VoiceController(
            key_mapping.VoiceTriggerMode(self._config["voice_trigger_mode"])
        )
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
        self._voice_audio_started_waiting_for_legacy_f5 = False
        self._voice_toggle_close_pending = False
        self._voice_toggle_reopen_pending = False
        self._voice_toggle_reopen_scheduled = False
        self._voice_toggle_reopen_generation = 0
        self._voice_toggle_reopen_handle: Optional[asyncio.TimerHandle] = None
        self._voice_toggle_reopen_echo_guard_until = 0.0
        self._voice_toggle_reopen_echo_sources_down: set[str] = set()
        self._voice_hotkey_release_pending: Optional[Tuple[str, ...]] = None
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
        # AUDIO_STARTED first. Keep all of those reports in one gesture until
        # the physical/audio release boundary so toggle mode changes state
        # exactly once per real press regardless of arrival order.
        self._voice_mic_gesture_active = False
        self._voice_mic_gesture_audio_started = False
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = False
        self._voice_mic_gesture_sources_down: set[str] = set()
        # A non-mic button mapped to voice has one stable owner across the
        # whole host/device session. Its physical release is required for
        # HOLD and for fix12's deferred TOGGLE reopen, but it must not share
        # the physical mic's multi-source gesture latch.
        self._mapped_voice_button_id: Optional[str] = None
        self._mapped_voice_sources_down: set[str] = set()
        self._mapped_voice_sources_seen: set[str] = set()
        self._mapped_voice_release_guard_until = 0.0
        self._mapped_voice_release_requested = False
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
        # When the built-in HOLD shortcut is selected, the low-level F5 hook
        # can deliver one right-Alt edge through the physicalized low-level
        # hook path. Keep this separate from VoiceController's logical state so
        # the normal audio/ATVV lifecycle still deduplicates correctly without
        # sending a second host shortcut.
        self._voice_legacy_transform_key_down = False
        self._voice_legacy_transform_session = False
        self._voice_legacy_transform_emitted = False
        self._legacy_f5_is_down = False
        self._legacy_voice_transform_snapshot = False
        self._refresh_legacy_voice_transform_snapshot_locked()
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
        self._voice_pcm_stats = PcmStats()
        self._event_loop = asyncio.get_event_loop()
        self._legacy_voice_event_generation = 0

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
        await self._supervisor.run_forever()

    async def stop(self) -> None:
        await self._supervisor.stop()

    async def _connect_once(self) -> None:
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

        try:
            paths = raw_input_windows.enumerate_matching_device_paths()
            device_path = hid_identity.select_single_device_path(paths)
        except raw_input_windows.RawInputUnavailableError as exc:
            self._logger.info("startup: Raw Input unavailable; buttons disabled: %s", exc)
            return
        except hid_identity.NoDevicePathFoundError:
            self._logger.info("startup: no RC003 HID device path found; buttons unavailable")
            return
        except hid_identity.AmbiguousDevicePathError as exc:
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
                self._logger.exception(
                    "startup: Raw Input listener failed to start but is still running; "
                    "owner retained for cleanup to retry"
                )
                raise
            self._logger.info("startup: Raw Input listener failed to start: %s", exc)
            self._hid_listener = None
            return

        # RC003's voice key is reported by Windows' keyboard class as F5 as
        # well as through ATVV. Raw Input is preferred when available; this
        # narrowly intercepts the same legacy F5 leak and emits one marked
        # right-Alt edge before audio starts. Doubao's own callback then
        # physicalizes that marked edge.
        self._legacy_key_suppressor = legacy_key_suppressor_windows.LegacyKeySuppressor(
            {0x74},
            on_key_event=self._on_legacy_key_event,
            on_key_transform=self._transform_legacy_voice_key,
            on_key_emit=self._emit_legacy_voice_key,
            rc003_vk_codes=frozenset(raw_input_windows.KEYBOARD_VK_TO_BUTTON),
        )
        self._legacy_voice_event_generation += 1
        try:
            self._legacy_key_suppressor.start()
            self._logger.info("startup: RC003 voice legacy-key guard enabled")
            if self._legacy_voice_transform_enabled():
                self._logger.info(
                    "startup: RC003 F5 voice edge transforms to one physical right-Alt edge"
                )
                if doubao_rpc.start_physicalizer():
                    self._logger.info(
                        "startup: Doubao low-level voice event physicalizer enabled"
                    )
                else:
                    self._logger.warning(
                        "startup: Doubao voice physicalizer unavailable: %s",
                        doubao_rpc.physicalizer_error() or doubao_rpc.physicalizer_status(),
                    )
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
                self._logger.info(
                    "startup: RC003 HID report tap unavailable: %s", tap.status
                )
        except Exception:
            self._logger.exception("startup: RC003 HID report tap failed to start")
            try:
                tap.stop()
            except Exception:
                self._logger.exception("startup: RC003 HID report tap cleanup failed")
                self._hid_report_tap = tap
                raise

    def _on_hid_tap_status(self, status: str, detail: str) -> None:
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

        if report_id != 1 or len(payload) != 6:
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
        if vk_code == 0x74:
            return
        suppressor.arm_key_event(vk_code, make_code, extended, is_pressed)


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

        failures: List[str] = []
        self._legacy_voice_event_generation += 1

        if self._hid_report_tap is not None:
            try:
                self._hid_report_tap.stop()
                self._hid_report_tap = None
            except Exception:
                self._logger.exception("cleanup: stopping the RC003 HID report tap failed")
                failures.append("RC003 HID report tap did not stop; owner retained")
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
        self._button_gestures.reset()

        try:
            with self._voice_trigger_lock:
                self._voice_audio_start_fallback_pending = False
                self._voice_audio_started_waiting_for_legacy_f5 = False
                self._voice_toggle_close_pending = False
                self._cancel_voice_toggle_reopen_locked("connection cleanup")
                self._voice_toggle_reopen_echo_guard_until = 0.0
                self._voice_toggle_reopen_echo_sources_down.clear()
                self._voice_raw_input_trigger_pending = False
                self._voice_audio_stream_active = False
                self._voice_audio_stop_processed = False
                self._voice_pcm_forwarding_enabled = False
                self._mapped_voice_button_id = None
                self._mapped_voice_sources_down.clear()
                self._mapped_voice_sources_seen.clear()
                self._mapped_voice_release_guard_until = 0.0
                self._mapped_voice_release_requested = False
                self._unsolicited_mic_close_pending = False
                self._finish_voice_mic_gesture()
                if self._voice_hotkey_release_pending is not None:
                    if self._release_pending_voice_hotkey():
                        self._voice_hotkey_release_pending = None
                    else:
                        failures.append(
                            "voice hotkey safety release did not fully deliver; state retained"
                        )
                reset_action = self._voice.reset()
                if reset_action is not None and not self._apply_voice_action(reset_action):
                    # _apply_voice_action() already logged the specific failure.
                    # reset() already cleared the controller's own pending
                    # state before we knew delivery would fail - restore it so
                    # a HOLD-mode key isn't recorded as released while it may
                    # still be physically down, and a TOGGLE-mode closing tap
                    # isn't forgotten (XRBM-019 review round 1 P1 #4).
                    self._voice.restore_pending(reset_action)
                    failures.append("voice hotkey release did not fully deliver; state retained")
                self._voice_legacy_transform_key_down = False
                self._voice_legacy_transform_session = False
                self._voice_legacy_transform_emitted = False
                self._legacy_f5_is_down = False
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
            except Exception:
                self._logger.exception("cleanup: stopping the Raw Input listener failed")
                failures.append("Raw Input listener did not stop; owner retained")
                # self._hid_listener is intentionally NOT cleared here: it
                # may still be a live thread/window.

        if self._legacy_key_suppressor is not None:
            try:
                self._legacy_key_suppressor.stop()
                self._legacy_key_suppressor = None
            except Exception:
                self._logger.exception("cleanup: stopping RC003 voice legacy-key guard failed")
                failures.append("RC003 voice legacy-key guard did not stop; owner retained")

        try:
            doubao_rpc.stop_physicalizer()
        except Exception:
            self._logger.exception("cleanup: stopping Doubao voice physicalizer failed")
            failures.append("Doubao voice physicalizer did not stop")

        if self._ble_session is not None:
            try:
                await self._ble_session.close()
                self._ble_session = None
            except Exception:
                self._logger.exception("cleanup: closing the BLE session failed")
                failures.append("BLE session did not fully close; owner retained")
                # self._ble_session is intentionally NOT cleared here either.

        if self._playback is not None:
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
        self._logger.info("BLE reported disconnected; requesting reconnect")
        self._supervisor.request_reconnect()

    def _on_session_error(self, exc: BaseException) -> None:
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
            if key_mapping.is_voice_action(action):
                result.append(button_id)
        return result

    def _voice_mode_for_primary_button(
        self,
        button_id: str,
        action: Optional[key_mapping.ButtonAction] = None,
    ) -> Optional[key_mapping.VoiceTriggerMode]:
        action = action or self._primary_button_action(button_id)
        try:
            legacy_mode = key_mapping.VoiceTriggerMode(
                self._config.get("voice_trigger_mode", "toggle")
            )
        except ValueError:
            legacy_mode = key_mapping.VoiceTriggerMode.TOGGLE
        mode = key_mapping.voice_trigger_mode_for_action(
            action,
            legacy_mode=legacy_mode,
        )
        if mode is None:
            return None
        voice_buttons = self._configured_voice_buttons()
        if len(voice_buttons) != 1 or voice_buttons[0] != button_id:
            return None
        return mode

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

    def _prepare_voice_mapping_locked(
        self,
        button_id: str,
        action: key_mapping.ButtonAction,
    ) -> bool:
        mode = self._voice_mode_for_primary_button(button_id, action)
        if mode is None:
            self._logger.warning(
                "voice mapping ignored: expected exactly one primary voice action; "
                "button=%s configured=%s",
                button_id,
                self._configured_voice_buttons(),
            )
            return False
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

    def _refresh_legacy_voice_transform_snapshot_locked(self) -> None:
        """Publish one immutable hook-thread view of the current mic profile."""

        mode = self._voice_mode_for_primary_button("mic")
        self._legacy_voice_transform_snapshot = (
            mode == key_mapping.VoiceTriggerMode.HOLD
            and self._voice.trigger_mode == key_mapping.VoiceTriggerMode.HOLD
            and self._voice_hotkey.serialize()
            in {"ralt", "lctrl+win", "lctrl+lwin"}
        )

    def _legacy_voice_transform_enabled(self) -> bool:
        """Whether the physical mic mapping uses the right-Alt HOLD path."""

        # The hook observes F5 before _on_button_event can reload settings.
        # Never transform with a stale mapping; the queued event will reload
        # and use the normal host-action fallback instead.
        if (
            self._settings_file_mtime_ns(self._config_path) != self._config_mtime_ns
            or self._settings_file_mtime_ns(self._bindings_path)
            != self._bindings_mtime_ns
        ):
            return False
        return self._legacy_voice_transform_snapshot

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
            return False
        self._voice_mic_gesture_active = True
        self._voice_mic_gesture_audio_started = source == "audio_started"
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = physical_down
        self._voice_mic_gesture_sources_down = {source} if physical_down else set()
        return True

    def _finish_voice_mic_gesture(self) -> None:
        """Release the current cross-source mic gesture latch."""

        self._voice_mic_gesture_active = False
        self._voice_mic_gesture_audio_started = False
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = False
        self._voice_mic_gesture_sources_down.clear()

    def _release_hold_voice_on_physical_release_locked(self, reason: str) -> bool:
        """Release HOLD shortcuts without depending solely on AUDIO_STOP."""

        action = self._voice.on_mic_button_released()
        if action is None:
            return True
        self._voice_raw_input_trigger_pending = False
        self._voice_audio_start_fallback_pending = False
        self._voice_pcm_forwarding_enabled = False
        if self._apply_voice_action(action):
            self._logger.info("voice hold hotkey released on %s", reason)
            return True

        self._voice.restore_pending(action)
        self._logger.info(
            "voice hold hotkey release failed on %s; state retained, "
            "requesting reconnect",
            reason,
        )
        self._supervisor.request_reconnect()
        return False

    def _cancel_voice_toggle_reopen_locked(self, reason: str = "") -> None:
        was_pending = self._voice_toggle_reopen_pending
        self._voice_toggle_reopen_pending = False
        self._voice_toggle_reopen_scheduled = False
        self._voice_toggle_reopen_generation += 1
        handle = self._voice_toggle_reopen_handle
        self._voice_toggle_reopen_handle = None
        if handle is not None:
            try:
                self._event_loop.call_soon_threadsafe(handle.cancel)
            except RuntimeError:
                handle.cancel()
        if was_pending and reason:
            self._logger.info("voice toggle pending reopen cancelled: %s", reason)

    def _schedule_voice_toggle_reopen_locked(
        self,
        delay: float,
        reason: str,
    ) -> None:
        if (
            not self._voice_toggle_reopen_pending
            or self._voice_toggle_reopen_scheduled
        ):
            return
        self._voice_toggle_reopen_scheduled = True
        generation = self._voice_toggle_reopen_generation

        def install_timer() -> None:
            with self._voice_trigger_lock:
                if (
                    generation != self._voice_toggle_reopen_generation
                    or not self._voice_toggle_reopen_pending
                    or not self._voice_toggle_reopen_scheduled
                ):
                    return
                self._voice_toggle_reopen_handle = self._event_loop.call_later(
                    delay,
                    self._complete_voice_toggle_reopen,
                    generation,
                    reason,
                )

        try:
            self._event_loop.call_soon_threadsafe(install_timer)
        except RuntimeError:
            self._voice_toggle_reopen_pending = False
            self._voice_toggle_reopen_scheduled = False
            self._voice_toggle_reopen_generation += 1
            self._logger.info(
                "voice toggle reopen suppressed: application event loop is closing"
            )

    def _defer_voice_toggle_reopen_locked(self) -> None:
        if self._voice_toggle_reopen_pending:
            return
        self._voice_toggle_reopen_pending = True
        self._voice_toggle_reopen_generation += 1
        if self._voice_mic_gesture_sources_down or self._mapped_voice_sources_down:
            self._logger.info(
                "voice toggle reopen deferred until mapped button release: "
                "mic_sources=%s mapped_button=%s",
                sorted(self._voice_mic_gesture_sources_down),
                self._mapped_voice_button_id if self._mapped_voice_sources_down else "",
            )
            return
        if self._voice_mic_gesture_physical_seen:
            self._logger.info(
                "voice toggle reopen queued after already-completed physical mic release"
            )
            self._schedule_voice_toggle_reopen_locked(
                0.0,
                "physical mic release",
            )
            return
        self._logger.info(
            "voice toggle reopen waiting %.0fms for a late physical mic edge",
            _VOICE_TOGGLE_REOPEN_FALLBACK_SECONDS * 1000.0,
        )
        self._schedule_voice_toggle_reopen_locked(
            _VOICE_TOGGLE_REOPEN_FALLBACK_SECONDS,
            "BLE-only release fallback",
        )

    def _complete_voice_toggle_reopen(self, generation: int, reason: str) -> None:
        with self._voice_trigger_lock:
            if (
                generation != self._voice_toggle_reopen_generation
                or not self._voice_toggle_reopen_pending
            ):
                return
            self._voice_toggle_reopen_handle = None
            self._voice_toggle_reopen_scheduled = False
            if self._voice_mic_gesture_sources_down or self._mapped_voice_sources_down:
                self._logger.info(
                    "voice toggle reopen still waiting for mapped button release: "
                    "mic_sources=%s mapped_button=%s",
                    sorted(self._voice_mic_gesture_sources_down),
                    self._mapped_voice_button_id
                    if self._mapped_voice_sources_down
                    else "",
                )
                return
            if (
                self._voice.trigger_mode != key_mapping.VoiceTriggerMode.TOGGLE
                or not self._voice.active
                or self._voice_toggle_close_pending
                or self._voice_audio_stream_active
                or self._ble_session is None
            ):
                self._cancel_voice_toggle_reopen_locked(
                    "voice session is no longer eligible"
                )
                return
            self._voice_toggle_reopen_pending = False
            self._voice_toggle_reopen_generation += 1
            if (
                self._voice_mic_gesture_active
                and self._voice_mic_gesture_audio_stopped
            ):
                self._finish_voice_mic_gesture()
            self._logger.info(
                "voice toggle reopening device mic after %s",
                reason,
            )
            # Real RC003 logs show a synthetic-looking F5 down/up about 60 ms
            # after an application-issued MIC_OPEN. Without a narrow guard it
            # is mistaken for the user's second toggle press and immediately
            # closes the stream that was just reopened. Keep the guard short
            # and source-paired: the whole echo press is ignored, while a real
            # later press still closes toggle mode normally.
            self._voice_toggle_reopen_echo_guard_until = (
                time.monotonic() + _VOICE_TOGGLE_REOPEN_ECHO_GUARD_SECONDS
            )
            self._voice_toggle_reopen_echo_sources_down.clear()
            self._voice_pcm_forwarding_enabled = True
            self._ble_session.send_mic_open_threadsafe()

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

    def _key_detection_blocks_legacy_mic_transform(self) -> bool:
        now = time.monotonic()
        with self._key_detection_mic_lock:
            self._expire_key_detection_mic_gesture_locked(now)
            if self._key_detection_mic_gesture_active:
                return True
            # Keep the pending-file check serialized with the first source's
            # publish/claim. Otherwise AudioStarted could claim the request in
            # the gap between these two checks and the hook would inject one
            # right-Alt edge before noticing the in-process latch.
            try:
                return key_detection_bridge.has_pending_request(self._config_root)
            except OSError:
                return False

    def _emit_legacy_voice_key(
        self,
        target: legacy_key_suppressor_windows.PhysicalKeyTarget,
        is_pressed: bool,
    ) -> bool:
        """Emit exactly one right-Alt edge for a physical F5 edge.

        The original F5 is swallowed by ``LegacyKeySuppressor``. This callback
        emits the single marked right-Alt edge for Doubao's verified callback
        physicalizer. No second host shortcut is sent for this session.
        """

        expected = legacy_key_suppressor_windows.PhysicalKeyTarget(
            vk_code=0xA5,
            scan_code=0x38,
            extended=True,
            system_key=True,
        )
        if target != expected:
            self._voice_legacy_transform_emitted = False
            return False
        try:
            if is_pressed:
                win32_input.send_voice_key_combo_down(("ralt",))
            else:
                win32_input.send_voice_key_combo_up(("ralt",))
                self._voice_hotkey_release_pending = None
            self._voice_legacy_transform_emitted = True
            self._logger.info(
                "voice physical F5 replaced with one right-Alt edge via %s: %s",
                win32_input.voice_backend_name(),
                "down" if is_pressed else "up",
            )
            return True
        except win32_input.InputCleanupIncompleteError:
            self._voice_hotkey_release_pending = ("ralt",)
            self._voice_legacy_transform_emitted = False
            self._logger.exception(
                "voice physical right-Alt replacement failed and safety "
                "release remains pending"
            )
            return False
        except (win32_input.Win32InputUnavailableError, OSError):
            self._voice_legacy_transform_emitted = False
            if is_pressed:
                self._voice_legacy_transform_key_down = False
            self._logger.exception(
                "voice physical right-Alt replacement failed; using host fallback"
            )
            return False

    def _transform_legacy_voice_key(
        self, vk_code: int, is_pressed: bool
    ) -> Optional[legacy_key_suppressor_windows.PhysicalKeyTarget]:
        """Replace a physical RC003 F5 edge with one physical right-Alt edge.

        The callback runs inside the low-level hook.  It deliberately only
        arms a new down edge while no voice trigger is already in flight; a
        matching up edge is still transformed after the app has marked the
        session active.  This prevents a Raw Input duplicate from opening a
        second host shortcut while preserving the hold/release pair.
        """

        if vk_code != 0x74 or not self._legacy_voice_transform_enabled():
            return None
        if is_pressed:
            if self._key_detection_blocks_legacy_mic_transform():
                # The bridge will report and swallow this press in
                # _on_button_event(), or another source already claimed the
                # same detection gesture; do not inject right-Alt first.
                return None
            if (
                self._voice.active
                or self._voice_raw_input_trigger_pending
                or self._voice_legacy_transform_key_down
                or self._legacy_f5_is_down
            ):
                return None
            self._voice_legacy_transform_key_down = True
        elif not self._voice_legacy_transform_key_down:
            return None
        else:
            self._voice_legacy_transform_key_down = False
        return legacy_key_suppressor_windows.PhysicalKeyTarget(
            vk_code=0xA5,
            scan_code=0x38,
            extended=True,
            system_key=True,
        )

    def _on_legacy_key_event(self, vk_code: int, is_pressed: bool) -> None:
        """Queue the already-suppressed physical F5 as a voice edge.

        Some RC003 firmware/Windows input-class combinations do not produce
        a device-scoped Raw Input keyboard record for the microphone button,
        even though the same physical press is visible to the low-level hook
        as F5. The hook is configured only for that legacy F5 and swallows it
        before it reaches the foreground app. This callback itself runs inside
        WH_KEYBOARD_LL and therefore must return immediately: opening a
        PortAudio endpoint can take longer than Windows' low-level-hook
        timeout, after which Windows may silently remove the hook and let F5
        reach the foreground app. Queue the application work onto the owning
        event loop instead of waiting for the voice-state lock here.
        """

        if vk_code == 0x74:
            if is_pressed:
                # WH_KEYBOARD_LL also reports auto-repeat key-down messages
                # while the remote button is held.  They are not new remote
                # gestures; collapse them until the matching physical up.
                if self._legacy_f5_is_down:
                    return
                self._legacy_f5_is_down = True
                if (
                    self._voice_legacy_transform_emitted
                    or self._voice_legacy_transform_key_down
                ):
                    self._voice_legacy_transform_session = True
            elif not self._legacy_f5_is_down:
                return
            else:
                self._legacy_f5_is_down = False
            host_action_handled = self._voice_legacy_transform_session
            generation = self._legacy_voice_event_generation
            try:
                self._event_loop.call_soon_threadsafe(
                    self._dispatch_legacy_key_event,
                    generation,
                    is_pressed,
                    host_action_handled,
                )
            except RuntimeError:
                # The owning loop is already closing. The original F5 remains
                # swallowed by LegacyKeySuppressor; cleanup owns voice state.
                pass
            self._voice_legacy_transform_emitted = False

    def _dispatch_legacy_key_event(
        self,
        generation: int,
        is_pressed: bool,
        host_action_handled: bool,
    ) -> None:
        if generation != self._legacy_voice_event_generation:
            return
        if is_pressed:
            self._logger.info(
                "voice legacy F5 trigger received from low-level keyboard hook"
            )
        self._on_button_event(
            "mic",
            is_pressed,
            host_action_handled=host_action_handled,
            event_source="legacy_f5",
        )

    def _on_raw_input_event(self, event: raw_input_windows.RawInputEvent) -> None:
        """Arm the exact original keyboard edge for duplicate suppression.

        The selected RC003 Raw Input listener is device-scoped; the global
        low-level keyboard hook is not.  Passing the observed VKey/MakeCode
        pair across this seam lets the hook swallow only the remote's
        original arrow/Enter/Home/consumer event before the injected mapping
        action is delivered.
        """

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
        ):
            return
        # RAWKEYBOARD uses RI_KEY_E0 (0x02) for the extended prefix; the
        # low-level hook uses LLKHF_EXTENDED (0x01).
        suppressor.arm_key_event(
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
            or self._voice_toggle_close_pending
            or self._voice_mic_gesture_active
            or self._voice_audio_stream_active
            or self._voice_legacy_transform_key_down
            or self._voice_legacy_transform_session
            or self._mapped_voice_button_id is not None
            or self._ordinary_mic_gesture_active
            or self._voice_hotkey_release_pending is not None
        )

    def _apply_voice_settings_locked(
        self,
        trigger_mode: key_mapping.VoiceTriggerMode,
        voice_hotkey: hotkey.HotkeySpec,
    ) -> None:
        self._voice = voice_controller.VoiceController(trigger_mode)
        self._voice_hotkey = voice_hotkey
        self._config["voice_trigger_mode"] = trigger_mode.value
        self._config["voice_hotkey"] = voice_hotkey.serialize()
        self._refresh_legacy_voice_transform_snapshot_locked()
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
            self._bindings = self._pending_bindings
            self._pending_bindings = None
            self._logger.info("deferred settings mappings applied after voice became idle")
        self._refresh_legacy_voice_transform_snapshot_locked()

    def _reload_settings_if_changed(self) -> None:
        """Apply mapping and voice-setting edits without a bridge restart."""

        current_config_mtime_ns = self._settings_file_mtime_ns(self._config_path)
        if current_config_mtime_ns != self._config_mtime_ns:
            try:
                refreshed_config = config.load_config(self._config_path)
                trigger_mode = key_mapping.VoiceTriggerMode(
                    refreshed_config["voice_trigger_mode"]
                )
                refreshed_hotkey_text = str(
                    refreshed_config.get("voice_hotkey", "")
                ).strip()
                if not refreshed_hotkey_text:
                    refreshed_hotkey_text = key_mapping.voice_hotkey_for_trigger_mode(
                        trigger_mode
                    )
                voice_hotkey = hotkey.HotkeySpec.parse(
                    refreshed_hotkey_text
                )
            except Exception as exc:  # noqa: BLE001 - keep the last valid settings
                self._logger.warning("voice settings reload skipped: %s", exc)
                with self._voice_trigger_lock:
                    self._legacy_voice_transform_snapshot = False
                    self._config_mtime_ns = current_config_mtime_ns
            else:
                with self._voice_trigger_lock:
                    current_settings = (
                        self._voice.trigger_mode,
                        self._voice_hotkey.serialize(),
                    )
                    refreshed_settings = (trigger_mode, voice_hotkey.serialize())
                    if refreshed_settings != current_settings:
                        if self._voice_settings_idle_locked():
                            self._config = refreshed_config
                            self._pending_config = None
                            self._pending_voice_settings = None
                            self._apply_voice_settings_locked(trigger_mode, voice_hotkey)
                        else:
                            self._pending_config = refreshed_config
                            self._pending_voice_settings = (trigger_mode, voice_hotkey)
                            self._logger.info(
                                "settings voice configuration deferred until idle: "
                                "trigger_mode=%s hotkey=%s",
                                trigger_mode.value,
                                voice_hotkey.serialize(),
                            )
                    else:
                        self._config = refreshed_config
                        self._pending_config = None
                        self._pending_voice_settings = None
                        self._refresh_legacy_voice_transform_snapshot_locked()
                    self._config_mtime_ns = current_config_mtime_ns

        current_mtime_ns = self._settings_file_mtime_ns(self._bindings_path)
        if current_mtime_ns == self._bindings_mtime_ns:
            return
        try:
            refreshed = config.load_key_bindings(self._bindings_path)
        except Exception as exc:  # noqa: BLE001 - keep the last valid mapping
            self._logger.warning("settings reload skipped: %s", exc)
            with self._voice_trigger_lock:
                self._legacy_voice_transform_snapshot = False
                self._bindings_mtime_ns = current_mtime_ns
            return
        with self._voice_trigger_lock:
            if self._voice_settings_idle_locked():
                self._bindings = refreshed
                self._pending_bindings = None
                self._refresh_legacy_voice_transform_snapshot_locked()
            else:
                self._pending_bindings = refreshed
                self._logger.info(
                    "settings mappings deferred until active voice session is idle"
                )
            self._bindings_mtime_ns = current_mtime_ns
        if self._pending_bindings is None:
            self._logger.info("settings mappings reloaded from disk")

    def _maybe_finish_mapped_voice_owner_locked(self) -> None:
        if (
            self._mapped_voice_button_id is not None
            and not self._mapped_voice_sources_down
            and not self._voice.active
            and not self._voice_audio_stream_active
            and not self._voice_toggle_close_pending
            and not self._voice_toggle_reopen_pending
        ):
            self._logger.info(
                "mapped voice session released: button=%s",
                self._mapped_voice_button_id,
            )
            self._mapped_voice_button_id = None
            self._mapped_voice_sources_seen.clear()
            self._mapped_voice_release_guard_until = 0.0
            self._mapped_voice_release_requested = False
            self._apply_pending_voice_settings_if_idle_locked()

    def _handle_mapped_voice_button_edge(
        self,
        button_id: str,
        action: key_mapping.ButtonAction,
        is_pressed: bool,
        event_source: str,
    ) -> None:
        """Drive voice once across Raw Input/direct-HID duplicate edges."""

        with self._voice_trigger_lock:
            owner = self._mapped_voice_button_id
            if owner is not None and owner != button_id:
                self._logger.info(
                    "voice button ignored: active session is owned by %s", owner
                )
                return

            if is_pressed:
                if event_source in self._mapped_voice_sources_down:
                    return
                if self._mapped_voice_sources_down:
                    self._mapped_voice_sources_down.add(event_source)
                    self._mapped_voice_sources_seen.add(event_source)
                    self._logger.info(
                        "mapped voice duplicate source joined: button=%s source=%s",
                        button_id,
                        event_source,
                    )
                    return
                now = time.monotonic()
                if (
                    owner is not None
                    and now < self._mapped_voice_release_guard_until
                    and event_source not in self._mapped_voice_sources_seen
                ):
                    self._mapped_voice_sources_down.add(event_source)
                    self._mapped_voice_sources_seen.add(event_source)
                    self._logger.info(
                        "mapped voice late duplicate ignored: button=%s source=%s",
                        button_id,
                        event_source,
                    )
                    return
                if owner is None:
                    if not self._prepare_voice_mapping_locked(button_id, action):
                        return
                    self._mapped_voice_button_id = button_id
                elif (
                    self._voice.trigger_mode == key_mapping.VoiceTriggerMode.HOLD
                    and self._voice.active
                ):
                    self._logger.info(
                        "hold voice press ignored while audio stop is pending: button=%s",
                        button_id,
                    )
                    return

                self._mapped_voice_sources_down.add(event_source)
                self._mapped_voice_sources_seen = {event_source}
                self._mapped_voice_release_guard_until = 0.0
                self._mapped_voice_release_requested = False
                was_active = self._voice.active
                self._logger.info(
                    "mapped voice button pressed: button=%s mode=%s",
                    button_id,
                    self._voice.trigger_mode.value,
                )
                self._handle_mic_button_pressed()
                if (
                    not was_active
                    and not self._voice.active
                    and not self._voice_toggle_close_pending
                ):
                    self._mapped_voice_sources_down.clear()
                    self._mapped_voice_sources_seen.clear()
                    self._mapped_voice_button_id = None
                    self._apply_pending_voice_settings_if_idle_locked()
                return

            if event_source not in self._mapped_voice_sources_down:
                return
            self._mapped_voice_sources_down.discard(event_source)
            if self._mapped_voice_sources_down:
                return
            self._mapped_voice_release_guard_until = (
                time.monotonic() + _MAPPED_VOICE_RELEASE_GUARD_SECONDS
            )
            if owner is None:
                return
            self._logger.info("mapped voice button released: button=%s", button_id)
            if (
                self._voice_toggle_reopen_pending
                and not self._voice_mic_gesture_sources_down
            ):
                self._schedule_voice_toggle_reopen_locked(
                    0.0,
                    "mapped voice button release",
                )
            if (
                self._voice.trigger_mode == key_mapping.VoiceTriggerMode.HOLD
                and self._voice.active
                and not self._mapped_voice_release_requested
            ):
                self._mapped_voice_release_requested = True
                self._voice_pcm_forwarding_enabled = False
                self._release_hold_voice_on_physical_release_locked(
                    f"mapped button release ({button_id})"
                )
                if self._ble_session is None:
                    self._logger.info(
                        "hold voice release has no BLE session; requesting reconnect"
                    )
                    self._supervisor.request_reconnect()
                else:
                    self._logger.info(
                        "hold voice release: sending MIC_CLOSE for button=%s",
                        button_id,
                    )
                    self._ble_session.send_mic_close_threadsafe()
            self._maybe_finish_mapped_voice_owner_locked()

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
                        time.monotonic() + _MAPPED_VOICE_RELEASE_GUARD_SECONDS
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
        host_action_handled: bool = False,
        event_source: str = "hid",
    ) -> None:
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
        if is_pressed:
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

        primary_action = self._primary_button_action(button_id)
        voice_mode = self._voice_mode_for_primary_button(
            button_id,
            primary_action,
        )

        if button_id == "mic":
            # fix14's synthetic F5 echo can follow any app-issued MIC_OPEN,
            # including one triggered by a different mapped button. Consume
            # the paired down/up before deciding whether mic itself is voice
            # or an ordinary mapping.
            with self._voice_trigger_lock:
                if (
                    not is_pressed
                    and event_source in self._voice_toggle_reopen_echo_sources_down
                ):
                    self._voice_toggle_reopen_echo_sources_down.discard(event_source)
                    self._logger.info(
                        "voice physical release ignored: application MIC_OPEN echo "
                        "source=%s",
                        event_source,
                    )
                    return
                if (
                    is_pressed
                    and time.monotonic() < self._voice_toggle_reopen_echo_guard_until
                    and (
                        self._voice.active
                        or self._voice_audio_stream_active
                        or self._voice_toggle_reopen_pending
                    )
                ):
                    self._voice_toggle_reopen_echo_sources_down.add(event_source)
                    self._logger.info(
                        "voice physical trigger ignored: application MIC_OPEN echo "
                        "source=%s",
                        event_source,
                    )
                    return

            if voice_mode is None:
                self._handle_ordinary_mic_edge(event_source, is_pressed)
                return

            if not is_pressed:
                with self._voice_trigger_lock:
                    self._voice_mic_gesture_sources_down.discard(event_source)
                    if (
                        self._voice.trigger_mode
                        == key_mapping.VoiceTriggerMode.HOLD
                        and not self._voice_mic_gesture_sources_down
                    ):
                        self._release_hold_voice_on_physical_release_locked(
                            "physical mic release"
                        )
                    if (
                        self._voice_toggle_reopen_pending
                        and not self._voice_mic_gesture_sources_down
                    ):
                        self._logger.info(
                            "voice physical mic release completed; queueing toggle reopen"
                        )
                        self._schedule_voice_toggle_reopen_locked(
                            0.0,
                            "physical mic release",
                        )
                    if (
                        self._voice_mic_gesture_active
                        and not self._voice_mic_gesture_sources_down
                        and (
                            self._voice_mic_gesture_audio_stopped
                            or (
                                not self._voice_mic_gesture_audio_started
                                and not self._voice_toggle_close_pending
                            )
                        )
                    ):
                        self._finish_voice_mic_gesture()
                    self._apply_pending_voice_settings_if_idle_locked()
                return
            with self._voice_trigger_lock:
                if not self._prepare_voice_mapping_locked(
                    button_id,
                    primary_action,
                ):
                    return
                if self._voice_toggle_close_pending:
                    if self._voice_mic_gesture_active:
                        self._voice_mic_gesture_sources_down.add(event_source)
                    self._logger.info(
                        "voice physical trigger ignored: toggle close still pending"
                    )
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
                if (
                    self._voice.active
                    and self._voice.trigger_mode != key_mapping.VoiceTriggerMode.TOGGLE
                ):
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
                    host_action_handled=host_action_handled,
                )
                if not self._voice.active and not self._voice_toggle_close_pending:
                    self._voice_raw_input_trigger_pending = False
            return

        if (
            self._mapped_voice_button_id == button_id
            or voice_mode is not None
        ):
            self._handle_mapped_voice_button_edge(
                button_id,
                primary_action,
                is_pressed,
                event_source,
            )
            return
        if is_pressed:
            self._button_gestures.press(button_id)
        else:
            self._button_gestures.release(button_id)

    def _is_button_action_configured(
        self, button_id: str, trigger: button_gesture.ButtonTrigger
    ) -> bool:
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
                if self._voice_toggle_close_pending:
                    self._voice_raw_input_trigger_pending = False
                    self._logger.info(
                        "voice mic trigger ignored: toggle close still pending"
                    )
                elif self._voice_mic_gesture_active:
                    self._voice_raw_input_trigger_pending = False
                    self._voice_audio_start_fallback_pending = False
                    self._logger.info(
                        "voice mic trigger ignored: matched current multi-source gesture"
                    )
                elif self._voice_audio_started_waiting_for_legacy_f5:
                    self._voice_audio_started_waiting_for_legacy_f5 = False
                    self._logger.info(
                        "voice mic trigger received without F5; using host fallback"
                    )
                    if self._begin_voice_mic_gesture("atvv"):
                        self._handle_mic_button_pressed(send_device_open=False)
                else:
                    if self._legacy_voice_transform_enabled():
                        self._logger.info(
                            "voice mic trigger received from ATVV; waiting for physical F5"
                        )
                        self._voice_audio_started_waiting_for_legacy_f5 = True
                        self._open_playback_for_new_session()
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
                if self._voice_toggle_close_pending:
                    self._logger.info(
                        "voice audio start ignored: toggle close still pending"
                    )
                elif self._voice_mic_gesture_active:
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
                    if self._legacy_voice_transform_enabled():
                        self._logger.info(
                            "voice audio started before F5; waiting for physical mic edge"
                        )
                        self._voice_audio_started_waiting_for_legacy_f5 = True
                        self._open_playback_for_new_session()
                    else:
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
                self._voice_audio_start_fallback_pending = False
                self._voice_audio_started_waiting_for_legacy_f5 = False
                self._voice_raw_input_trigger_pending = False
                self._unsolicited_mic_close_pending = False
                if self._voice_mic_gesture_active:
                    self._voice_mic_gesture_audio_started = False
                    self._voice_mic_gesture_audio_stopped = True
                toggle_close_completed = self._voice_toggle_close_pending
                self._voice_toggle_close_pending = False
                action = self._voice.on_audio_stopped()
                transformed_session = self._voice_legacy_transform_session
                action_applied = (
                    True
                    if action is None
                    else self._apply_voice_action(action)
                )
                if transformed_session:
                    self._voice_legacy_transform_session = False
                if action is not None and not action_applied:
                    # Same rule as _cleanup_once(): on_audio_stopped() already
                    # cleared the controller's pending state before we knew
                    # whether the closing action (HOLD's KEY_UP or TOGGLE's
                    # closing TAP) actually delivered. A failure here must not
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
                elif (
                    self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE
                    and self._voice.active
                    and not toggle_close_completed
                    and self._ble_session is not None
                ):
                    self._defer_voice_toggle_reopen_locked()
                elif (
                    self._voice_mic_gesture_active
                    and self._voice_mic_gesture_audio_stopped
                    and not self._voice_mic_gesture_sources_down
                ):
                    self._finish_voice_mic_gesture()
                self._maybe_finish_mapped_voice_owner_locked()
                self._apply_pending_voice_settings_if_idle_locked()

    def _handle_mic_button_pressed(
        self,
        *,
        send_device_open: bool = True,
        host_action_handled: bool = False,
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

        self._voice_audio_started_waiting_for_legacy_f5 = False

        is_toggle_close_request = (
            self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE
            and self._voice.active
        )
        if not is_toggle_close_request and not self._open_playback_for_new_session():
            self._voice_pcm_forwarding_enabled = False
            self._logger.info(
                "voice failing closed: no usable output endpoint; hotkey/MIC_OPEN suppressed"
            )
            return

        was_active = self._voice.active
        action = self._voice.on_mic_button_pressed()
        is_toggle_close = (
            self._voice.trigger_mode == key_mapping.VoiceTriggerMode.TOGGLE
            and was_active
            and not self._voice.active
        )
        action_delivered = (
            True
            if host_action_handled
            else self._apply_voice_action(action)
        )
        if host_action_handled:
            self._logger.info(
                "voice host shortcut already handled by physical F5-to-right-Alt transform"
            )
        if not action_delivered:
            self._voice_pcm_forwarding_enabled = False
            if is_toggle_close:
                # The host is still in voice mode if its closing tap failed.
                self._voice.restore_pending(action)
            else:
                # Nothing physically landed (win32_input.py's own batching
                # already rolled back any partial key-down) - clear the
                # controller's logical state without emitting a second,
                # likely-just-as-doomed compensating action.
                self._voice.cancel_pending()
            self._logger.info(
                "voice failing closed: host hotkey delivery failed; device command suppressed"
            )
            return

        if is_toggle_close and self._ble_session is not None:
            self._voice_pcm_forwarding_enabled = False
            self._voice_toggle_reopen_echo_guard_until = 0.0
            self._voice_toggle_reopen_echo_sources_down.clear()
            # The host closing TAP has already landed. A later asynchronous
            # MIC_CLOSE write failure is a transport failure only: the BLE
            # error callback requests cleanup/reconnect, but must never
            # restore this logical toggle state or emit a second TAP, which
            # could reopen the host voice UI.
            self._cancel_voice_toggle_reopen_locked("toggle close accepted")
            self._voice_toggle_close_pending = True
            self._logger.info("voice toggle closing: sending MIC_CLOSE")
            self._ble_session.send_mic_close_threadsafe()
        else:
            self._voice_pcm_forwarding_enabled = True
        if not is_toggle_close and send_device_open and self._ble_session is not None:
            if self._mapped_voice_button_id is not None:
                self._voice_toggle_reopen_echo_guard_until = (
                    time.monotonic() + _VOICE_TOGGLE_REOPEN_ECHO_GUARD_SECONDS
                )
                self._voice_toggle_reopen_echo_sources_down.clear()
            self._ble_session.send_mic_open_threadsafe()

    def _apply_voice_action(self, action: voice_controller.VoiceHostAction) -> bool:
        tokens = tuple(self._voice_hotkey.modifiers) + (self._voice_hotkey.key,)
        if self._voice_legacy_transform_session:
            if (
                action == voice_controller.VoiceHostAction.KEY_UP
                and self._voice_legacy_transform_key_down
            ):
                # Audio can stop before the remote's leaked F5 key-up arrives.
                # Release the replacement right-Alt edge here so a disconnect
                # or early stream stop can never leave Alt logically held.
                try:
                    win32_input.send_voice_key_combo_up(("ralt",))
                    self._voice_hotkey_release_pending = None
                    self._voice_legacy_transform_key_down = False
                    self._voice_legacy_transform_session = False
                    self._logger.info(
                        "voice released right-Alt replacement before physical F5 key-up"
                    )
                    return True
                except win32_input.InputCleanupIncompleteError:
                    self._voice_hotkey_release_pending = ("ralt",)
                    self._logger.exception(
                        "voice right-Alt replacement release remains pending"
                    )
                    return False
                except (win32_input.Win32InputUnavailableError, OSError):
                    self._logger.exception(
                        "voice right-Alt replacement release failed"
                    )
                    return False
            self._logger.info(
                "voice host action already delivered by physical F5-to-right-Alt transform: %s",
                action.value,
            )
            return True
        try:
            if action == voice_controller.VoiceHostAction.TAP:
                win32_input.send_voice_key_combo_tap(tokens)
            elif action == voice_controller.VoiceHostAction.KEY_DOWN:
                win32_input.send_voice_key_combo_down(tokens)
            else:
                win32_input.send_voice_key_combo_up(tokens)
            if action in {
                voice_controller.VoiceHostAction.TAP,
                voice_controller.VoiceHostAction.KEY_UP,
            }:
                self._voice_hotkey_release_pending = None
            return True
        except win32_input.Win32InputUnavailableError:
            self._logger.info("voice hotkey action skipped: no usable voice input backend")
            return False
        except win32_input.InputCleanupIncompleteError:
            self._voice_hotkey_release_pending = tokens
            self._logger.exception(
                "voice hotkey action failed and safety key-up remains pending"
            )
            return False
        except OSError:
            self._logger.exception("voice hotkey action failed to fully deliver")
            return False

    def _release_pending_voice_hotkey(self) -> bool:
        tokens = self._voice_hotkey_release_pending
        if tokens is None:
            return True
        try:
            win32_input.send_voice_key_combo_up(tokens)
        except (win32_input.Win32InputUnavailableError, OSError):
            self._logger.exception("voice hotkey safety release failed")
            return False
        self._logger.info("voice hotkey safety release completed")
        return True

    def _open_playback_for_new_session(self) -> bool:
        if self._playback is not None:
            if getattr(self._playback, "ready", True):
                return True
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
            self._logger.info(
                "voice playback opened: host_api=%s sample_rate=%s channels=%s",
                endpoint_host_api or "unspecified",
                sink.output_sample_rate_hz,
                sink.output_channels,
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

    def _on_pcm_frame(self, samples) -> None:
        """Fails closed on a write failure (XRBM-014 review round 2 P1 #6):
        a broken playback sink must not be left open logging indefinitely
        while the device keeps streaming into it - request a reconnect so
        the next attempt starts from a clean state, unconditionally either
        way. Whether ``self._playback`` itself is cleared here depends on
        whether the follow-up ``close()`` actually succeeds (XRBM-019
        review round 1 P1 #5): a sink whose close call also failed still
        owns a PortAudio stream, and clearing the reference would hide that
        incompletely closed resource and let a reconnect open a second sink
        over it - the owner is only cleared once close() confirms success,
        same rule as ``_cleanup_once()``'s HID/BLE/playback steps.

        Runs on ble_transport_winrt.py's dedicated worker thread (not the
        event loop thread), so ``request_reconnect()`` must be - and is -
        safe to call cross-thread (see connection_supervisor.py).
        """

        if self._playback is None or not self._voice_pcm_forwarding_enabled:
            return
        try:
            self._voice_pcm_stats.add(samples)
            if self._voice_pcm_stats.frames in (1, 10) or self._voice_pcm_stats.frames % 200 == 0:
                stats = self._voice_pcm_stats.summary()
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
            self._playback.write(samples)
        except Exception:
            self._voice_pcm_forwarding_enabled = False
            self._logger.exception("audio playback write failed; failing closed")
            try:
                self._playback.close()
                self._playback = None
            except Exception:
                self._logger.exception("cleanup: closing the failed playback sink failed")
                # self._playback is intentionally NOT cleared here: it may
                # still own a live PortAudio stream.
            self._supervisor.request_reconnect()


async def _run(
    *,
    app_factory=None,
    tray_factory=None,
    settings_launcher=None,
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
            await app.stop()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
