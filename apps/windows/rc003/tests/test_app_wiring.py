"""App-wiring/thread-safety tests for app.py's RC003App (XRBM-018 DoD 4).

``RC003App.__init__`` is safe to construct off Windows: config/hotkey/
voice-controller/supervisor setup is pure Python, and the real Win32/WinRT
calls only happen inside ``_connect_once()``/the HID listener, which these
tests never call. Constructing a real ``RC003App`` and substituting its
BLE-session/playback collaborators with lightweight recorders lets these
tests exercise the actual wiring DECISIONS app.py makes - host hotkey
failure suppresses MIC_OPEN, playback write failure fails closed and
requests a reconnect, and that request happens correctly from a real
worker thread - without any Windows API, matching this project's existing
"test contracts, not implementation-mirroring fakes" approach.

The host-hotkey-unavailable case doesn't even need mocking: off Windows,
win32_input.py's ``_require_windows()`` genuinely raises
``Win32InputUnavailableError`` on every call, so it exercises the exact
"hotkey failed to deliver" branch app.py must fail closed on - not a stand-
in for it.
"""

import asyncio
import logging
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import app as app_module
from ovb_rc003 import (
    bridge_runtime_status,
    config,
    frida_compat,
    key_detection_bridge,
    key_mapping,
    logging_setup,
    raw_input_windows,
    voice_program_manager,
    win32_input,
)
from ovb_rc003.atvv_session import AudioStarted, AudioStopped, MicButtonPressed

DEFAULT_VOICE_TOKENS = ("ralt",)


def _run(coro):
    # Explicitly closing the loop (XRBM-018 review round 2 evidence: a
    # ResourceWarning for an unclosed test event loop) rather than letting
    # it be garbage-collected.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeBleSession:
    def __init__(self, close_raises=False):
        self.mic_open_calls = 0
        self.mic_close_calls = 0
        self.close_raises = close_raises
        self.close_calls = 0

    def send_mic_open_threadsafe(self):
        self.mic_open_calls += 1

    def send_mic_close_threadsafe(self):
        self.mic_close_calls += 1

    async def close(self):
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("simulated BLE worker thread that did not stop")


class _FakeHidListener:
    def __init__(self, stop_raises=False):
        self.stop_raises = stop_raises
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        if self.stop_raises:
            raise RuntimeError("simulated Raw Input listener thread that did not stop")


class _FakePlaybackSink:
    def __init__(self, fail_write=False, close_raises=False):
        self.fail_write = fail_write
        self.close_raises = close_raises
        self.write_calls = []
        self.closed = False
        self.close_calls = 0

    def write(self, samples):
        self.write_calls.append(samples)
        if self.fail_write:
            raise OSError("simulated PortAudio write failure")

    def close(self):
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("simulated PortAudio stream that did not close")
        self.closed = True


class _FakeHidListenerForFailedStart:
    """XRBM-019 review round 1 P1 #3: a fake standing in for
    RawInputButtonListener itself (not just its ``start()`` outcome), so
    ``_start_hid_listener()`` can be exercised end-to-end off Windows -
    ``is_running`` is the source of truth a failed ``start()`` must consult
    before deciding whether to keep or discard the owner reference.
    """

    def __init__(self, is_running_after_failed_start):
        self._is_running_after_failed_start = is_running_after_failed_start
        self.start_calls = 0

    @property
    def is_running(self):
        return self._is_running_after_failed_start

    def start(self, device_path):
        self.start_calls += 1
        raise app_module.raw_input_windows.RawInputUnavailableError("simulated failed start")

    def stop(self):
        pass


def _build_app(tmp_root: Path) -> "app_module.RC003App":
    # Redirect config_root (and therefore logging_setup's log directory) at
    # a throwaway temp directory instead of the real machine's config/log
    # location - RC003App.__init__ always calls config.config_root()/
    # logging_setup.get_logger(), neither of which touch any Windows API.
    original = config.config_root
    config.config_root = lambda: tmp_root
    try:
        return app_module.RC003App()
    finally:
        config.config_root = original


def _build_app_with_owned_loop(tmp_root: Path):
    """Like _build_app(), but explicitly creates a fresh event loop and sets
    it as this thread's current loop before constructing the app (XRBM-026).

    RC003App.__init__ builds a ConnectionSupervisor, whose __init__ captures
    ``loop or asyncio.get_event_loop()`` (connection_supervisor.py) - called
    here synchronously, off any running loop. Without an owned loop already
    set, that would silently create-and-cache this thread's implicit default
    loop the first time any test builds an RC003App - a loop nothing then
    ever closes (see EventLoopOwnershipRegressionTests for the exact red
    evidence this reproduces and fixes). Returns ``(app, loop)``; the caller
    owns ``loop`` and must ``asyncio.set_event_loop(None)`` then
    ``loop.close()`` it when done - exactly mirroring the real app's own
    ``asyncio.run(_run())`` construction, which owns and closes its loop too.
    """

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = _build_app(tmp_root)
    return app, loop


class _AppWiringTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # XRBM-026 red evidence (real Windows run 29644660267): 425 tests
        # passed, then the process printed an ignored "unclosed event loop"
        # ResourceWarning for a ProactorEventLoop plus two unclosed self-pipe
        # sockets - AFTER unittest's own summary, so -W error::ResourceWarning
        # never sees it and the step still exits 0 (a ResourceWarning-turned-
        # exception raised inside a __del__/finalizer is unraisable; Python
        # can only print it via sys.unraisablehook, never let it change an
        # already-computed exit code - see EventLoopOwnershipRegressionTests
        # below for a deterministic, isolated-subprocess reproduction).
        # _build_app_with_owned_loop() above threads a per-test owned loop
        # into ConnectionSupervisor instead - never the ambient, never-closed
        # default the old bare _build_app() call left behind.
        self.app, self._loop = _build_app_with_owned_loop(Path(self._tmp.name))
        self.app._playback = _FakePlaybackSink()
        self.app._ble_session = _FakeBleSession()

    def tearDown(self):
        playback_writer = self.app._playback_writer
        if playback_writer is not None:
            playback_writer.flush(1.0)
            playback_writer.stop(1.0)
            self.app._playback_writer = None
        # XRBM-023: logging_setup.get_logger() configures its FileHandler
        # exactly once per process (module-global ``_configured``) and never
        # closes it - correct for a real long-running app, but in this suite
        # it leaves an open handle inside THIS test's temp directory. Windows
        # (unlike POSIX, where you can unlink a file while a handle is still
        # open on it) refuses to delete a directory containing an open file
        # handle, so ``self._tmp.cleanup()`` below would raise on Windows
        # once any prior test in this class had already configured the
        # logger. Close/remove the handler and reset the one-time-config
        # flag first so every test starts and ends with no logging state
        # leaked into the next one.
        logger = logging.getLogger(logging_setup.LOGGER_NAME)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        logging_setup._configured = False
        self._tmp.cleanup()
        # XRBM-026: close the loop this test owns (see setUp()) and detach
        # it as the thread's current loop, so its own eventual __del__ finds
        # is_closed() already True and stays silent - and so the NEXT test's
        # setUp() cannot mistake this now-closed loop for a live ambient one.
        asyncio.set_event_loop(None)
        self._loop.close()

    def _drain_event_loop(self):
        self._loop.run_until_complete(asyncio.sleep(0))

    def _flush_playback(self):
        writer = self.app._playback_writer
        self.assertIsNotNone(writer)
        return writer.flush(1.0)

    def _save_voice_settings(self, *, mode: str, hotkey_text: str) -> None:
        refreshed = config.load_config(self.app._config_path)
        refreshed["voice_trigger_mode"] = mode
        refreshed["voice_hotkey"] = hotkey_text
        config.save_config(self.app._config_path, refreshed)

    def _set_voice_mapping(
        self,
        button_id: str,
        mode: key_mapping.VoiceTriggerMode,
    ) -> None:
        bindings = self.app._bindings["bindings"]
        for existing_button, raw_action in list(bindings.items()):
            try:
                action = key_mapping.ButtonAction.from_dict(raw_action)
            except (KeyError, TypeError, ValueError):
                continue
            if key_mapping.is_voice_action(action):
                bindings[existing_button] = key_mapping.ButtonAction(
                    key_mapping.ActionKind.DISABLED
                ).to_dict()
        bindings[button_id] = key_mapping.voice_action_for_trigger_mode(mode).to_dict()


class StartupIdentityLoggingTests(unittest.TestCase):
    def test_frozen_startup_logs_version_runtime_and_package_directory(self):
        logger = mock.Mock(spec=logging.Logger)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                package_dir = Path(tmp) / "RemoteMicRC003-localtest"
                executable = package_dir / "RemoteMicRC003.exe"
                with mock.patch.object(
                    logging_setup, "get_logger", return_value=logger
                ), mock.patch.object(
                    app_module.sys, "frozen", True, create=True
                ), mock.patch.object(
                    app_module.sys, "executable", str(executable)
                ):
                    _build_app(Path(tmp))
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        logger.info.assert_any_call(
            "startup: app identity: version=%s runtime=%s package=%s",
            app_module.__version__,
            "frozen",
            package_dir.name,
        )


class LiveSettingsReloadTests(_AppWiringTestCase):
    def test_zero_voice_blank_shortcuts_construct_without_crashing(self):
        stored_config = config.default_config()
        stored_config["voice_hotkey"] = ""
        stored_config["voice_hotkeys"] = {"toggle": "", "hold": ""}
        config.save_config(self.app._config_path, stored_config)
        stored_bindings = config.default_key_bindings()
        stored_bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        config.save_key_bindings(self.app._bindings_path, stored_bindings)

        reconstructed = _build_app(self.app._config_root)

        self.assertEqual(reconstructed._config["voice_hotkey"], "ralt")
        self.assertEqual(reconstructed._configured_voice_buttons(), [])
        self.assertEqual(reconstructed._voice_hotkey.serialize(), "ralt")

    def test_voice_mode_and_hotkey_reload_while_idle(self):
        self._save_voice_settings(mode="hold", hotkey_text="ctrl+l")

        self.app._reload_settings_if_changed()

        self.assertEqual(self.app._voice.trigger_mode, key_mapping.VoiceTriggerMode.HOLD)
        self.assertEqual(self.app._voice_hotkey.serialize(), "ctrl+l")
        self.assertIsNone(self.app._pending_voice_settings)

    def test_audio_only_trigger_reloads_voice_settings_before_dispatch(self):
        self._set_voice_mapping("mic", key_mapping.VoiceTriggerMode.HOLD)
        self._save_voice_settings(mode="hold", hotkey_text="ctrl+l")
        delivered = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: delivered.append(tokens),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))

        self.assertEqual(self.app._voice.trigger_mode, key_mapping.VoiceTriggerMode.HOLD)
        self.assertEqual(self.app._voice_hotkey.serialize(), "ctrl+l")
        self.assertEqual(delivered, [("ctrl", "l")])

    def test_voice_settings_reload_is_deferred_until_active_hold_releases(self):
        self.app._voice.on_mic_button_pressed()
        self._save_voice_settings(mode="hold", hotkey_text="ctrl+l")

        self.app._reload_settings_if_changed()

        self.assertEqual(self.app._voice.trigger_mode, key_mapping.VoiceTriggerMode.HOLD)
        self.assertEqual(self.app._voice_hotkey.serialize(), "ralt")
        self.assertIsNotNone(self.app._pending_voice_settings)

        self.app._voice.on_mic_button_released()
        with self.app._voice_trigger_lock:
            self.app._apply_pending_voice_settings_if_idle_locked()

        self.assertEqual(self.app._voice.trigger_mode, key_mapping.VoiceTriggerMode.HOLD)
        self.assertEqual(self.app._voice_hotkey.serialize(), "ctrl+l")
        self.assertIsNone(self.app._pending_voice_settings)

    def test_invalid_voice_settings_keep_the_last_valid_runtime_values(self):
        refreshed = config.load_config(self.app._config_path)
        refreshed["voice_hotkey"] = "ctrl"
        config.save_config(self.app._config_path, refreshed)

        self.app._reload_settings_if_changed()

        self.assertEqual(self.app._voice.trigger_mode, key_mapping.VoiceTriggerMode.HOLD)
        self.assertEqual(self.app._voice_hotkey.serialize(), "ralt")
        self.assertIsNone(self.app._pending_voice_settings)


class CandidateResolutionWiringTests(_AppWiringTestCase):
    def test_runtime_status_identifies_build_channels_recent_button_and_voice(self):
        self.app._runtime_raw_input_state = "ready"
        self.app._runtime_hid_tap_state = frida_compat.HidTapState.READY.value
        with mock.patch.object(app_module.time, "time", return_value=123.0), mock.patch.object(
            app_module.time,
            "monotonic",
            return_value=10.0,
        ):
            self.app._record_runtime_button("hid")
        self.app._set_runtime_voice_active(True)
        self.app._publish_runtime_status(
            bridge_runtime_status.BridgeConnectionState.CONNECTED
        )

        status = bridge_runtime_status.read_status(self.app._config_root)

        self.assertEqual(status.app_version, app_module.__version__)
        self.assertTrue(status.runtime_id)
        self.assertEqual(status.raw_input_state, "ready")
        self.assertEqual(status.hid_tap_state, frida_compat.HidTapState.READY.value)
        self.assertEqual(status.last_button_at, 123.0)
        self.assertEqual(status.last_button_source, "hid")
        self.assertTrue(status.voice_active)

    def test_runtime_status_cleanup_only_removes_the_current_process_file(self):
        other_pid = app_module.os.getpid() + 1
        bridge_runtime_status.publish_status(
            self.app._config_root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=other_pid,
        )

        self.app.clear_runtime_status()

        self.assertEqual(
            bridge_runtime_status.read_status(self.app._config_root).pid,
            other_pid,
        )
        bridge_runtime_status.publish_status(
            self.app._config_root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=app_module.os.getpid(),
        )
        self.app.clear_runtime_status()
        self.assertIsNone(bridge_runtime_status.read_status(self.app._config_root))

    def test_connect_once_uses_connectable_candidate_resolver(self):
        candidates = [object(), object()]
        chosen = object()
        resolver_calls = []
        connected = []

        async def discover():
            return candidates

        async def resolve(received):
            resolver_calls.append(received)
            return chosen

        class Session:
            def __init__(self, **_kwargs):
                pass

            async def connect(self, candidate):
                connected.append(candidate)

            async def close(self):
                pass

        with mock.patch.object(
            app_module.ble_transport_winrt, "discover_candidates", discover
        ), mock.patch.object(
            app_module.ble_transport_winrt,
            "select_connectable_candidate",
            resolve,
        ), mock.patch.object(
            app_module.ble_transport_winrt, "RC003BleSession", Session
        ), mock.patch.object(
            self.app, "_start_hid_listener"
        ) as start_hid, mock.patch.object(
            self.app, "_start_hid_report_tap"
        ) as start_tap, mock.patch.object(
            self.app, "_publish_runtime_status"
        ) as publish_status:
            self._loop.run_until_complete(self.app._connect_once())

        self.assertEqual(resolver_calls, [candidates])
        self.assertEqual(connected, [chosen])
        start_hid.assert_called_once_with()
        start_tap.assert_called_once_with()
        self.assertEqual(
            publish_status.call_args_list,
            [
                mock.call(
                    bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
                ),
                mock.call(bridge_runtime_status.BridgeConnectionState.CONNECTED),
            ],
        )

    def test_disconnect_and_protocol_error_return_runtime_status_to_waiting(self):
        reconnects = []
        self.app._supervisor.request_reconnect = lambda: reconnects.append(True)
        with mock.patch.object(self.app, "_publish_runtime_status") as publish_status:
            self.app._on_disconnected()
            self.app._on_session_error(RuntimeError("simulated protocol error"))

        waiting = bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        self.assertEqual(
            publish_status.call_args_list,
            [mock.call(waiting), mock.call(waiting)],
        )
        self.assertEqual(reconnects, [True, True])


class HostHotkeyFailureSuppressesMicOpenTests(_AppWiringTestCase):
    """Hold-to-talk must fail closed and never leave a host key held."""

    @unittest.skipIf(
        sys.platform == "win32",
        "only exercises the off-Windows input-backend gate",
    )
    def test_hotkey_unavailable_off_windows_suppresses_mic_open(self):
        self.app._handle_mic_button_pressed()

        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice.active)

    def test_hotkey_partial_delivery_suppresses_mic_open(self):
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=OSError("simulated partial SendInput delivery"),
        ):
            self.app._handle_mic_button_pressed()

        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice.active)

    def test_incomplete_hotkey_rollback_is_retained_for_a_later_safety_release(self):
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=win32_input.InputCleanupIncompleteError(
                "simulated stuck modifier"
            ),
        ):
            self.app._handle_mic_button_pressed()

        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice.active)
        self.assertEqual(self.app._voice_hotkey_release_pending, DEFAULT_VOICE_TOKENS)

    def test_safety_release_uses_the_original_shortcut_after_settings_change(self):
        self.app._voice_hotkey_release_pending = ("ralt",)
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+l")
        calls = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.assertTrue(self.app._release_pending_voice_hotkey())

        self.assertEqual(calls, [("ralt",)])

    def test_wetype_provider_uses_provider_controller_for_start_and_stop(self):
        self.app._config["voice_program"] = (
            voice_program_manager.normalize_voice_program_settings(
                {"provider": voice_program_manager.VOICE_PROGRAM_WETYPE}
            )
        )
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse(
            "lctrl+lshift+f9"
        )
        calls = []

        with mock.patch.object(
            self.app._wetype_voice_control,
            "start",
            side_effect=lambda tokens: calls.append(("wetype_start", tokens)) or True,
        ), mock.patch.object(
            self.app._wetype_voice_control,
            "stop",
            side_effect=lambda tokens: calls.append(("wetype_stop", tokens)) or True,
        ), mock.patch.object(
            win32_input,
            "send_wetype_voice_key_combo_down",
        ) as wetype_down, mock.patch.object(
            win32_input,
            "send_wetype_voice_key_combo_up",
        ) as wetype_up, mock.patch.object(
            win32_input, "send_voice_key_combo_tap"
        ) as marked_tap, mock.patch.object(
            win32_input, "send_voice_key_combo_down"
        ) as marked_down, mock.patch.object(
            win32_input, "send_voice_key_combo_up"
        ) as marked_up:
            self.assertTrue(
                self.app._apply_voice_action(
                    app_module.voice_controller.VoiceHostAction.KEY_DOWN
                )
            )
            self.assertTrue(
                self.app._apply_voice_action(
                    app_module.voice_controller.VoiceHostAction.KEY_UP
                )
            )

        expected_tokens = ("lctrl", "lshift", "f9")
        self.assertEqual(
            calls,
            [
                ("wetype_start", expected_tokens),
                ("wetype_stop", expected_tokens),
            ],
        )
        wetype_down.assert_not_called()
        wetype_up.assert_not_called()
        marked_tap.assert_not_called()
        marked_down.assert_not_called()
        marked_up.assert_not_called()
        self.assertIsNone(self.app._voice_hotkey_release_pending)

    def test_successful_hold_down_is_owned_until_matching_key_up(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.assertTrue(
                self.app._apply_voice_action(
                    app_module.voice_controller.VoiceHostAction.KEY_DOWN
                )
            )
            self.assertEqual(
                self.app._voice_hotkey_release_pending,
                DEFAULT_VOICE_TOKENS,
            )
            self.assertTrue(
                self.app._apply_voice_action(
                    app_module.voice_controller.VoiceHostAction.KEY_UP
                )
            )

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertIsNone(self.app._voice_hotkey_release_pending)

    def test_wetype_physical_release_waits_for_audio_stop_before_second_tap(self):
        self.app._config["voice_program"] = (
            voice_program_manager.normalize_voice_program_settings(
                {"provider": voice_program_manager.VOICE_PROGRAM_WETYPE}
            )
        )
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse(
            "lctrl+lshift+f9"
        )
        calls = []

        with mock.patch.object(
            self.app._wetype_voice_control,
            "start",
            side_effect=lambda tokens: calls.append(("start", tokens)) or True,
        ), mock.patch.object(
            self.app._wetype_voice_control,
            "stop",
            side_effect=lambda tokens: calls.append(("stop", tokens)) or True,
        ):
            self.app._handle_mic_button_pressed()
            self.app._voice_audio_stream_active = True
            self.assertTrue(self.app._voice_pcm_forwarding_enabled)

            self.assertTrue(
                self.app._release_hold_voice_on_physical_release_locked(
                    "test physical release"
                )
            )

            self.assertTrue(self.app._voice.active)
            self.assertTrue(self.app._voice_pcm_forwarding_enabled)
            self.assertEqual(
                calls,
                [("start", ("lctrl", "lshift", "f9"))],
            )

            self.app._on_control_event(AudioStopped())

        self.assertFalse(self.app._voice.active)
        self.assertFalse(self.app._voice_pcm_forwarding_enabled)
        self.assertEqual(
            calls,
            [
                ("start", ("lctrl", "lshift", "f9")),
                ("stop", ("lctrl", "lshift", "f9")),
            ],
        )

    def test_non_wetype_providers_keep_the_marked_voice_backend(self):
        providers = (
            voice_program_manager.VOICE_PROGRAM_NONE,
            voice_program_manager.VOICE_PROGRAM_SOGOU,
            voice_program_manager.VOICE_PROGRAM_WINDOWS_DICTATION,
            voice_program_manager.VOICE_PROGRAM_CUSTOM,
        )
        for provider in providers:
            with self.subTest(provider=provider):
                self.app._config["voice_program"] = (
                    voice_program_manager.normalize_voice_program_settings(
                        {"provider": provider}
                    )
                )
                self.app._voice_hotkey_active_backend = None
                with mock.patch.object(
                    win32_input, "send_voice_key_combo_down"
                ) as marked_down, mock.patch.object(
                    win32_input, "send_wetype_voice_key_combo_down"
                ) as wetype_down:
                    self.assertTrue(
                        self.app._apply_voice_action(
                            app_module.voice_controller.VoiceHostAction.KEY_DOWN
                        )
                    )

                marked_down.assert_called_once_with(DEFAULT_VOICE_TOKENS)
                wetype_down.assert_not_called()

    def test_wetype_safety_release_keeps_the_failed_session_backend(self):
        self.app._config["voice_program"] = (
            voice_program_manager.normalize_voice_program_settings(
                {"provider": voice_program_manager.VOICE_PROGRAM_WETYPE}
            )
        )
        with mock.patch.object(
            self.app._wetype_voice_control,
            "start",
            side_effect=win32_input.InputCleanupIncompleteError(
                "simulated stuck WeType modifier"
            ),
        ):
            self.assertFalse(
                self.app._apply_voice_action(
                    app_module.voice_controller.VoiceHostAction.KEY_DOWN
                )
            )

        self.app._config["voice_program"] = (
            voice_program_manager.normalize_voice_program_settings(
                {"provider": voice_program_manager.VOICE_PROGRAM_SOGOU}
            )
        )
        with mock.patch.object(
            win32_input, "send_wetype_voice_key_combo_up"
        ) as wetype_up, mock.patch.object(
            win32_input, "send_voice_key_combo_up"
        ) as marked_up:
            self.assertTrue(self.app._release_pending_voice_hotkey())

        wetype_up.assert_called_once_with(DEFAULT_VOICE_TOKENS)
        marked_up.assert_not_called()

    def test_hotkey_success_sends_mic_open(self):
        with mock.patch.object(win32_input, "send_voice_key_combo_down"):
            self.app._handle_mic_button_pressed()

        self.assertEqual(self.app._ble_session.mic_open_calls, 1)
        self.assertTrue(self.app._voice.active)

    def test_raw_input_press_runs_before_matching_atvv_events(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(MicButtonPressed())
            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertFalse(self.app._voice.active)

    def test_suppressed_legacy_f5_does_not_own_the_voice_shortcut(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

        self.assertEqual(calls, [])
        self.assertFalse(self.app._voice.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_legacy_f5_cannot_release_a_direct_hid_voice_session(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()
            self.assertTrue(self.app._voice.active)
            self.app._on_button_event("mic", False, event_source="hid_tap")

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice.active)

    def test_audio_start_uses_configured_hotkey_without_waiting_for_f5(self):
        self._save_voice_settings(mode="hold", hotkey_text="ralt")
        self.app._reload_settings_if_changed()
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))

        self.assertEqual(calls, [("ralt",)])
        self.assertTrue(self.app._voice.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_audio_start_can_fallback_without_a_physical_key_edge(self):
        self._save_voice_settings(mode="hold", hotkey_text="ctrl+l")
        self.app._reload_settings_if_changed()
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))

        self.assertEqual(calls, [("ctrl", "l")])
        self.assertTrue(self.app._voice.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_duplicate_audio_start_does_not_send_a_second_key_down(self):
        self._save_voice_settings(mode="hold", hotkey_text="ctrl+l")
        self.app._reload_settings_if_changed()
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStarted(session_id=1))

        self.assertEqual(calls, [("ctrl", "l")])
        self.assertTrue(self.app._voice.active)

    def test_hid_mic_button_is_ignored_until_ble_session_is_connected(self):
        self.app._ble_session = None
        with mock.patch.object(win32_input, "send_voice_key_combo_down") as hotkey:
            self.app._on_button_event("mic", True)

        hotkey.assert_not_called()
        self.assertFalse(self.app._voice.active)

    def test_mic_button_before_audio_start_does_not_send_a_second_key_down(self):
        self._save_voice_settings(mode="hold", hotkey_text="ctrl+l")
        self.app._reload_settings_if_changed()
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(AudioStarted(session_id=1))

        self.assertEqual(calls, [("ctrl", "l")])
        self.assertTrue(self.app._voice.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 1)

    def test_no_usable_endpoint_suppresses_hotkey_and_mic_open(self):
        self.app._playback = None
        self.app._config["output_endpoint_name"] = "some endpoint that is not open"

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as hotkey:
            self.app._handle_mic_button_pressed()

        hotkey.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_removed_release_finish_setting_never_sends_an_extra_tap(self):
        # Schema 2 exposed this field. Keep the runtime fail-safe even if an
        # old in-memory config reaches the app before it has been re-saved.
        self.app._config["voice_release_finish_tap_enabled"] = True
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ), mock.patch.object(win32_input, "send_voice_key_combo_tap") as finish_tap:
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_control_event(AudioStopped())

            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=2))
            self.app._on_control_event(AudioStopped())
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(
            calls,
            [
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
            ],
        )
        finish_tap.assert_not_called()
        self.assertFalse(self.app._voice.active)

    def test_windows_actually_delivers_the_hold_hotkey(self):
        original_platform = sys.platform
        original_sender = win32_input._real_voice_event
        sys.platform = "win32"
        win32_input._real_voice_event = lambda vk, key_up: None
        try:
            self.app._handle_mic_button_pressed()
        finally:
            sys.platform = original_platform
            win32_input._real_voice_event = original_sender

        self.assertEqual(self.app._ble_session.mic_open_calls, 1)
        self.assertTrue(self.app._voice.active)


class CorruptButtonBindingFailsClosedTests(_AppWiringTestCase):
    def test_unknown_or_malformed_binding_does_not_escape_raw_input_callback(self):
        for malformed in (
            {"kind": "unknown", "keys": []},
            {"kind": "key_combo", "keys": ["not_a_real_key"]},
            {"kind": "key_combo", "keys": "ctrl+a"},
            {},
            "not-a-mapping",
        ):
            self.app._bindings = {"bindings": {"back": malformed}}
            self.app._on_button_event("back", True)

        self.assertFalse(self.app._voice.active)


class OrdinaryButtonGestureWiringTests(_AppWiringTestCase):
    def test_saved_mapping_is_reloaded_before_the_next_button_event(self):
        updated = config.default_key_bindings()
        updated["bindings"]["back"] = {"kind": "key_combo", "keys": ["f8"]}
        config.save_key_bindings(self.app._bindings_path, updated)

        calls = []
        original = win32_input.send_key_combo_tap
        win32_input.send_key_combo_tap = lambda keys: calls.append(tuple(keys))
        try:
            self.app._on_button_event("back", True)
            self.app._on_button_event("back", False)
        finally:
            win32_input.send_key_combo_tap = original

        self.assertEqual(calls, [("f8",)])

    def test_legacy_f5_auto_repeat_is_collapsed_to_one_physical_press(self):
        calls = []
        with mock.patch.object(
            self.app,
            "_dispatch_legacy_key_event",
            side_effect=lambda _generation, is_pressed: calls.append(is_pressed),
        ):
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

        self.assertEqual(calls, [True, False])

    def test_legacy_f5_hook_callback_never_waits_for_the_voice_lock(self):
        returned = []

        def invoke():
            self.app._on_legacy_key_event(0x74, True)
            returned.append(True)

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
        ):
            self.app._voice_trigger_lock.acquire()
            worker = threading.Thread(target=invoke)
            try:
                worker.start()
                worker.join(timeout=0.2)
                returned_without_lock = not worker.is_alive()
            finally:
                self.app._voice_trigger_lock.release()
            worker.join(timeout=1.0)
            self._drain_event_loop()

        self.assertTrue(returned_without_lock)
        self.assertEqual(returned, [True])

    def test_legacy_f5_hook_rechecks_input_after_cleanup_wins_the_lock(self):
        dispatched = []

        class CleanupWinsGate:
            def __enter__(inner_self):
                self.app._accept_input_events = False
                self.app._legacy_f5_is_down = False
                return inner_self

            def __exit__(inner_self, exc_type, exc, traceback):
                return False

        self.app._legacy_f5_hook_lock = CleanupWinsGate()
        with mock.patch.object(
            self.app,
            "_dispatch_legacy_key_event",
            side_effect=lambda generation, is_pressed: dispatched.append(
                (generation, is_pressed)
            ),
        ):
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()

        self.assertFalse(self.app._legacy_f5_is_down)
        self.assertEqual(dispatched, [])

    def test_semantic_arrow_action_uses_its_function_executor(self):
        calls = []
        original = getattr(win32_input, "send_arrow_up", None)
        win32_input.send_arrow_up = lambda: calls.append("arrow_up")
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP)
            )
        finally:
            if original is None:
                delattr(win32_input, "send_arrow_up")
            else:
                win32_input.send_arrow_up = original

        self.assertEqual(calls, ["arrow_up"])

    def test_element_navigation_action_uses_the_isolated_controller(self):
        result = app_module.element_navigation_control_windows.ToggleResult(
            app_module.element_navigation_control_windows.ToggleResultKind.DELIVERED,
            321,
        )
        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "toggle_element_navigation",
            return_value=result,
        ) as toggle:
            self.app._apply_button_action(
                key_mapping.ButtonAction(
                    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE
                )
            )

        toggle.assert_called_once_with()

    def test_element_navigation_failure_does_not_stop_button_processing(self):
        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "toggle_element_navigation",
            side_effect=RuntimeError("simulated companion failure"),
        ), self.assertLogs(level="ERROR") as captured:
            self.app._apply_button_action(
                key_mapping.ButtonAction(
                    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE
                )
            )

        self.assertTrue(
            any("element navigation toggle failed" in line for line in captured.output)
        )

    def test_incomplete_button_rollback_is_released_before_the_next_action(self):
        original_up = win32_input.send_arrow_up
        original_down = win32_input.send_arrow_down
        original_release = win32_input.send_key_combo_up
        releases = []
        actions = []
        win32_input.send_arrow_up = lambda: (_ for _ in ()).throw(
            win32_input.InputCleanupIncompleteError("simulated stuck arrow key")
        )
        win32_input.send_arrow_down = lambda: actions.append("arrow_down")
        win32_input.send_key_combo_up = lambda keys: releases.append(tuple(keys))
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP)
            )
            self.assertEqual(self.app._button_key_release_pending, ("up",))

            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
            )
        finally:
            win32_input.send_arrow_up = original_up
            win32_input.send_arrow_down = original_down
            win32_input.send_key_combo_up = original_release

        self.assertEqual(releases, [("up",)])
        self.assertEqual(actions, ["arrow_down"])
        self.assertIsNone(self.app._button_key_release_pending)

    def test_pending_button_release_blocks_new_actions_when_retry_is_incomplete(self):
        original_down = win32_input.send_arrow_down
        original_release = win32_input.send_key_combo_up
        actions = []
        self.app._button_key_release_pending = ("ctrl", "l")
        win32_input.send_arrow_down = lambda: actions.append("arrow_down")
        win32_input.send_key_combo_up = lambda _keys: (_ for _ in ()).throw(
            win32_input.InputCleanupIncompleteError("still stuck")
        )
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
            )
        finally:
            win32_input.send_arrow_down = original_down
            win32_input.send_key_combo_up = original_release

        self.assertEqual(actions, [])
        self.assertEqual(self.app._button_key_release_pending, ("ctrl", "l"))

    def test_open_app_action_uses_application_executor(self):
        calls = []
        original = getattr(app_module, "open_configured_application", None)
        app_module.open_configured_application = lambda action: calls.append(action.kind)
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CODEX)
            )
        finally:
            if original is None:
                delattr(app_module, "open_configured_application")
            else:
                app_module.open_configured_application = original

        self.assertEqual(calls, [key_mapping.ActionKind.OPEN_CODEX])

    def test_one_physical_press_emits_one_mapping_action(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=lambda: calls.append("up"),
        ):
            self.app._on_button_event("up", True)
            self.app._on_button_event("up", True)  # Raw Input repeat/duplicate
            self.app._on_button_event("up", False)

        self.assertEqual(calls, ["up"])

    def test_custom_combo_on_direction_button_does_not_start_hold_repeat(self):
        self.app._bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.KEY_COMBO,
            ("shift", "3"),
        ).to_dict()

        with mock.patch.object(win32_input, "send_key_combo_tap") as action:
            self.app._on_button_event("up", True)

        action.assert_called_once_with(("shift", "3"))
        self.assertNotIn("up", self.app._button_gestures._repeat_timers)

        self.app._on_button_event("up", False)

    def test_remote_button_combo_consumes_both_single_actions(self):
        self.app._bindings["bindings"]["tv"] = {
            "kind": "escape",
            "keys": [],
        }
        self.app._bindings["bindings"]["up"] = {
            "kind": "arrow_up",
            "keys": [],
        }
        self.app._bindings["combo_bindings"] = {
            "modifier": "tv",
            "bindings": {"up": {"kind": "return", "keys": []}},
            "display_notes": {},
        }

        with mock.patch.object(win32_input, "send_escape") as tv_action, mock.patch.object(
            win32_input, "send_arrow_up"
        ) as up_action, mock.patch.object(win32_input, "send_return") as combo_action:
            self.app._on_button_event("tv", True)
            self.app._on_button_event("up", True)
            self.app._on_button_event("up", False)
            self.app._on_button_event("tv", False)

        combo_action.assert_called_once_with()
        tv_action.assert_not_called()
        up_action.assert_not_called()

    def test_unused_remote_combo_modifier_keeps_its_single_click(self):
        self.app._bindings["bindings"]["tv"] = {
            "kind": "escape",
            "keys": [],
        }
        self.app._bindings["combo_bindings"] = {
            "modifier": "tv",
            "bindings": {"up": {"kind": "return", "keys": []}},
            "display_notes": {},
        }

        with mock.patch.object(win32_input, "send_escape") as tv_action:
            self.app._on_button_event("tv", True)
            self.app._on_button_event("tv", False)

        tv_action.assert_called_once_with()

    def test_quicker_uri_action_uses_the_protocol_executor(self):
        action = key_mapping.ButtonAction(
            key_mapping.ActionKind.QUICKER_URI,
            uri="quicker:runaction:pin-window",
        )

        with mock.patch.object(
            app_module.action_executor, "open_quicker_uri"
        ) as launcher:
            self.app._apply_button_action(action)

        launcher.assert_called_once_with(action)

    def test_raw_keyboard_edge_is_armed_for_low_level_duplicate_suppression(self):
        armed = []

        class _Suppressor:
            def arm_tracked_key_event(self, *args):
                armed.append(args)

        self.app._legacy_key_suppressor = _Suppressor()
        self.app._on_raw_input_event(
            raw_input_windows.RawInputEvent(
                source="keyboard",
                is_pressed=True,
                button_id="up",
                vkey=0x26,
                make_code=0x48,
                flags=0x02,
            )
        )

        self.assertEqual(armed, [(0x26, 0x48, True, True)])

    def test_direct_hid_edges_track_the_full_physical_hold(self):
        armed = []

        class _Suppressor:
            def arm_tracked_key_event(self, *args):
                armed.append(args)

        self.app._legacy_key_suppressor = _Suppressor()
        usage = next(
            usage
            for usage, button_id in app_module.frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "up"
        )

        self.app._arm_from_direct_usage(usage, True)
        self.app._arm_from_direct_usage(usage, False)

        self.assertEqual(
            armed,
            [
                (0x26, 0x48, True, True),
                (0x26, 0x48, True, False),
            ],
        )

    def test_unknown_or_unbound_raw_keyboard_edge_is_not_armed(self):
        armed = []

        class _Suppressor:
            def arm_key_event(self, *args):
                armed.append(args)

        self.app._legacy_key_suppressor = _Suppressor()
        for event in (
            raw_input_windows.RawInputEvent(
                source="keyboard",
                is_pressed=True,
                button_id=None,
                vkey=0xFF,
                make_code=0x70,
                flags=0,
            ),
            raw_input_windows.RawInputEvent(
                source="keyboard",
                is_pressed=True,
                button_id="volume_mute",
                vkey=0x20,
                make_code=0x20,
                flags=0x02,
            ),
        ):
            self.app._on_raw_input_event(event)

        self.assertEqual(armed, [])


class VoiceMappingProductBoundaryTests(_AppWiringTestCase):
    def _save_bindings(self, bindings):
        config.save_key_bindings(self.app._bindings_path, bindings)
        self.app._reload_settings_if_changed()

    def test_non_mic_voice_mapping_is_disabled_as_a_whole_button(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        bindings["secondary_bindings"]["up"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.ARROW_DOWN
            ).to_dict()
        }
        self._save_bindings(bindings)

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as voice, mock.patch.object(
            win32_input, "send_arrow_down"
        ) as secondary:
            self.app._on_button_event("up", True)
            self.app._on_button_event("up", False)
            self.app._on_button_trigger(
                "up", app_module.button_gesture.ButtonTrigger.DOUBLE_CLICK
            )

        self.assertEqual(self.app._removed_voice_bindings, {"up": "voice_hold"})
        voice.assert_not_called()
        secondary.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_old_toggle_mapping_disables_mic_and_its_secondary_gestures(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_TOGGLE
        ).to_dict()
        bindings["secondary_bindings"]["mic"] = {
            "long_press": key_mapping.ButtonAction(
                key_mapping.ActionKind.ARROW_UP
            ).to_dict()
        }
        self._save_bindings(bindings)

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as voice, mock.patch.object(
            win32_input, "send_arrow_up"
        ) as secondary:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)
            self.app._on_button_trigger(
                "mic", app_module.button_gesture.ButtonTrigger.LONG_PRESS
            )

        self.assertEqual(self.app._removed_voice_bindings, {"mic": "voice_toggle"})
        voice.assert_not_called()
        secondary.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_secondary_voice_mapping_disables_the_whole_button(self):
        bindings = config.default_key_bindings()
        bindings["secondary_bindings"]["left"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.VOICE_HOLD
            ).to_dict()
        }
        self._save_bindings(bindings)

        with mock.patch.object(win32_input, "send_arrow_left") as primary:
            self.app._on_button_event("left", True)
            self.app._on_button_event("left", False)

        self.assertEqual(self.app._removed_voice_bindings, {"left": "voice_hold"})
        primary.assert_not_called()
        self.assertFalse(
            self.app._is_button_action_configured(
                "left", app_module.button_gesture.ButtonTrigger.SINGLE_CLICK
            )
        )

    def test_reselecting_an_ordinary_action_clears_the_removed_marker(self):
        legacy = config.default_key_bindings()
        legacy["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        self._save_bindings(legacy)
        self.assertIn("up", self.app._removed_voice_bindings)

        refreshed = config.default_key_bindings()
        refreshed["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        self._save_bindings(refreshed)

        with mock.patch.object(win32_input, "send_arrow_up") as action:
            self.app._on_button_event("up", True)
            self.app._on_button_event("up", False)

        self.assertNotIn("up", self.app._removed_voice_bindings)
        action.assert_called_once_with()

    def test_physical_mic_hold_release_sends_one_down_and_one_up(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice.active)

    def test_legacy_f5_does_not_trigger_or_release_voice(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

        self.assertEqual(calls, [])
        self.assertFalse(self.app._voice.active)

    def test_legacy_f5_release_absorbs_late_raw_input_down_after_audio_stop(self):
        calls = []
        self.app._direct_hid_tap_active = False
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            # Exact order captured on the remote machine: audio owns the first
            # session, F5 remains down through AudioStopped, then Raw Input
            # reports one late down without a matching up.
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()
            self.app._on_control_event(AudioStopped())

            self.assertEqual(
                calls,
                [
                    ("down", DEFAULT_VOICE_TOKENS),
                    ("up", DEFAULT_VOICE_TOKENS),
                ],
            )
            self.assertEqual(
                self.app._voice_mic_gesture_sources_down,
                {"legacy_f5"},
            )
            self.assertTrue(self.app._voice_mic_gesture_audio_stopped)

            self.app._on_button_event("mic", True, event_source="hid")
            self.assertEqual(
                self.app._voice_mic_gesture_sources_down,
                {"legacy_f5", "hid"},
            )
            self.assertEqual(len(calls), 2)

            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

            self.assertEqual(self.app._voice_mic_gesture_sources_down, set())
            self.assertFalse(self.app._voice_mic_gesture_active)

            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=2))
            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
            ],
        )
        self.assertFalse(self.app._voice.active)

    def test_legacy_f5_up_before_audio_stop_never_releases_the_host_shortcut(self):
        calls = []
        self.app._direct_hid_tap_active = False
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()
            self.app._on_button_event("mic", True, event_source="hid")

            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

            self.assertEqual(calls, [("down", DEFAULT_VOICE_TOKENS)])
            self.assertTrue(self.app._voice.active)
            self.assertEqual(self.app._voice_mic_gesture_sources_down, {"hid"})

            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice_mic_gesture_active)

    def test_tap_status_change_does_not_pin_a_raw_input_source(self):
        calls = []
        self.app._direct_hid_tap_active = False
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()

            # The tap may become globally ready because of another usage even
            # though this mic gesture never produced a direct HID mic edge.
            self.app._direct_hid_tap_active = True
            self.app._on_control_event(AudioStopped())
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

            self.assertFalse(self.app._voice_mic_gesture_active)
            self.assertEqual(self.app._voice_mic_gesture_sources_down, set())

            self.app._on_control_event(AudioStarted(session_id=2))
            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
            ],
        )

    def test_late_f5_up_cannot_close_the_next_raw_input_session(self):
        calls = []
        self.app._direct_hid_tap_active = False
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_control_event(AudioStopped())

            self.assertFalse(self.app._voice_mic_gesture_active)
            self.assertTrue(self.app._legacy_f5_voice_blocked_until_up)

            self.app._on_button_event("mic", True, event_source="hid")
            self.assertTrue(self.app._voice.active)

            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

            self.assertTrue(self.app._voice.active)
            self.assertEqual(self.app._voice_mic_gesture_sources_down, {"hid"})
            self.assertEqual(
                calls,
                [
                    ("down", DEFAULT_VOICE_TOKENS),
                    ("up", DEFAULT_VOICE_TOKENS),
                    ("down", DEFAULT_VOICE_TOKENS),
                ],
            )

            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
            ],
        )
        self.assertFalse(self.app._voice.active)

    def test_retired_f5_up_is_consumed_before_an_ordinary_mapping_reload(self):
        calls = []
        default_mic_action = dict(self.app._bindings["bindings"]["mic"])
        self.app._direct_hid_tap_active = False
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ), mock.patch.object(win32_input, "send_arrow_up") as ordinary_action:
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_control_event(AudioStopped())

            self.assertTrue(self.app._legacy_f5_voice_blocked_until_up)
            self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
                key_mapping.ActionKind.ARROW_UP
            ).to_dict()
            request = key_detection_bridge.request_detection(self.app._config_root)

            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

            self.assertFalse(self.app._legacy_f5_voice_blocked_until_up)
            ordinary_action.assert_not_called()
            self.assertIsNone(key_detection_bridge.poll_detection(request))
            key_detection_bridge.cancel_detection(request)

            self.app._bindings["bindings"]["mic"] = default_mic_action
            self.app._on_control_event(AudioStarted(session_id=2))
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()
            self.assertEqual(
                self.app._voice_mic_gesture_sources_down,
                {"legacy_f5"},
            )
            self.app._on_control_event(AudioStopped())
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

        self.assertEqual(
            calls,
            [
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
            ],
        )
        self.assertFalse(self.app._voice_mic_gesture_active)

    def test_retired_f5_up_does_not_join_an_active_detection_gesture(self):
        self.app._direct_hid_tap_active = False
        self.app._on_button_event("mic", True, event_source="hid")
        self.app._on_legacy_key_event(0x74, True)
        self._drain_event_loop()
        self.app._on_button_event("mic", False, event_source="hid")
        self.app._on_control_event(AudioStopped())

        self.assertTrue(self.app._legacy_f5_voice_blocked_until_up)
        request = key_detection_bridge.request_detection(self.app._config_root)
        self.app._on_button_event("mic", True, event_source="hid")
        self.assertEqual(key_detection_bridge.poll_detection(request), "mic")
        self.assertEqual(self.app._key_detection_mic_sources_down, {"hid"})

        self.app._on_legacy_key_event(0x74, False)
        self._drain_event_loop()

        self.assertFalse(self.app._legacy_f5_voice_blocked_until_up)
        self.assertEqual(self.app._key_detection_mic_sources_down, {"hid"})
        self.app._on_button_event("mic", False, event_source="hid")

    def test_stray_hid_tap_up_does_not_unlock_audio_continuation(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            # Leave the queued down undispatched so AudioStarted must use the
            # hook-state snapshot rather than event-loop timing.
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_button_event("mic", False, event_source="hid_tap")

            self.assertEqual(calls, [("down", DEFAULT_VOICE_TOKENS)])
            self.assertTrue(self.app._voice.active)
            self.assertTrue(self.app._voice_audio_stream_active)

            self.app._on_control_event(AudioStopped())
            self.app._on_control_event(AudioStarted(session_id=2))

            self.assertEqual(
                calls,
                [
                    ("down", DEFAULT_VOICE_TOKENS),
                    ("up", DEFAULT_VOICE_TOKENS),
                ],
            )
            self.assertFalse(self.app._voice.active)
            self.assertEqual(
                self.app._voice_mic_gesture_sources_down,
                {"legacy_f5"},
            )

            self.app._on_control_event(AudioStopped())
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()
            self.assertFalse(self.app._voice_mic_gesture_active)

            self.app._on_control_event(AudioStarted(session_id=3))
            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
            ],
        )

    def test_first_hid_tap_press_retires_a_stopped_startup_gesture(self):
        calls = []
        self.app._direct_hid_tap_active = False
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStopped())
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStarted(session_id=2))

            self.app._direct_hid_tap_active = True
            self.app._on_button_event("mic", True, event_source="hid_tap")

            self.assertEqual(
                calls,
                [
                    ("down", DEFAULT_VOICE_TOKENS),
                    ("up", DEFAULT_VOICE_TOKENS),
                    ("down", DEFAULT_VOICE_TOKENS),
                ],
            )
            self.assertEqual(
                self.app._voice_mic_gesture_sources_down,
                {"hid_tap"},
            )
            self.assertTrue(self.app._legacy_f5_voice_blocked_until_up)

            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()
            self.assertTrue(self.app._voice.active)

            self.app._on_button_event("mic", False, event_source="hid_tap")
            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
                ("down", DEFAULT_VOICE_TOKENS),
                ("up", DEFAULT_VOICE_TOKENS),
            ],
        )
        self.assertFalse(self.app._voice.active)

    def test_direct_hid_session_is_independent_from_legacy_f5(self):
        calls = []
        self.app._direct_hid_tap_active = True
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()
            self.assertTrue(self.app._voice.active)
            self.assertEqual(
                self.app._voice_mic_gesture_sources_down,
                {"hid_tap"},
            )
            self.app._on_button_event("mic", False, event_source="hid_tap")

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice.active)

    def test_audio_lifecycle_fallback_works_without_legacy_f5(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStopped())

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertFalse(self.app._voice.active)

    def test_physical_mic_release_failure_retains_state_and_reconnects(self):
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        with mock.patch.object(win32_input, "send_voice_key_combo_down"), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=OSError("simulated release failure"),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertTrue(self.app._voice.active)
        self.assertEqual(reconnect_calls, [1])

    def test_mic_normal_mapping_dispatches_once_and_rejects_unsolicited_voice(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        actions = []
        with mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=lambda: actions.append("up"),
        ), mock.patch.object(win32_input, "send_voice_key_combo_down") as voice:
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_button_event("mic", True, event_source="legacy_f5")
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_button_event("mic", False, event_source="legacy_f5")

        self.assertEqual(actions, ["up"])
        voice.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_close_calls, 1)

    def test_ordinary_mic_ignores_a_late_second_source_from_same_press(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        clock = [100.0]
        actions = []
        with mock.patch.object(
            app_module.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ), mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=lambda: actions.append("up"),
        ):
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_button_event("mic", False, event_source="hid_tap")
            clock[0] += app_module._ORDINARY_MIC_RELEASE_GUARD_SECONDS / 2
            self.app._on_button_event("mic", True, event_source="legacy_f5")
            self.app._on_button_event("mic", False, event_source="legacy_f5")
            self.assertEqual(actions, ["up"])

            clock[0] += app_module._ORDINARY_MIC_RELEASE_GUARD_SECONDS + 0.01
            self.app._on_button_event("mic", True, event_source="legacy_f5")
            self.app._on_button_event("mic", False, event_source="legacy_f5")

        self.assertEqual(actions, ["up", "up"])

    def test_ordinary_mic_same_source_next_press_is_not_suppressed(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        actions = []
        with mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=lambda: actions.append("up"),
        ):
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_button_event("mic", False, event_source="hid_tap")
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_button_event("mic", False, event_source="hid_tap")

        self.assertEqual(actions, ["up", "up"])

    def test_non_mic_legacy_voice_data_is_removed_from_runtime_bindings(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        self._save_bindings(bindings)

        self.assertIn("up", self.app._removed_voice_bindings)

    def test_active_voice_mapping_reload_is_deferred_until_release(self):
        with mock.patch.object(win32_input, "send_voice_key_combo_down"), mock.patch.object(
            win32_input, "send_voice_key_combo_up"
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            refreshed = config.default_key_bindings()
            refreshed["bindings"]["mic"] = key_mapping.ButtonAction(
                key_mapping.ActionKind.ESCAPE
            ).to_dict()
            config.save_key_bindings(self.app._bindings_path, refreshed)
            self.app._reload_settings_if_changed()
            self.assertIsNotNone(self.app._pending_bindings)

            self.app._on_button_event("mic", False, event_source="hid")

        self.assertIsNone(self.app._pending_bindings)
        self.assertEqual(
            self.app._bindings["bindings"]["mic"]["kind"],
            key_mapping.ActionKind.ESCAPE.value,
        )

    def test_ordinary_mic_release_finishes_before_voice_mapping_applies(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()

        with mock.patch.object(win32_input, "send_arrow_up"):
            self.app._on_button_event("mic", True, event_source="hid")

            refreshed = config.default_key_bindings()
            config.save_key_bindings(self.app._bindings_path, refreshed)
            self.app._reload_settings_if_changed()
            self.assertIsNotNone(self.app._pending_bindings)

            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(self.app._ordinary_mic_sources_down, set())
        self.assertFalse(self.app._ordinary_mic_gesture_active)
        self.assertIsNone(self.app._pending_bindings)
        self.assertEqual(
            self.app._bindings["bindings"]["mic"]["kind"],
            key_mapping.ActionKind.VOICE_HOLD.value,
        )

    def test_detected_removed_voice_button_reports_but_does_not_execute(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        self._save_bindings(bindings)
        request = key_detection_bridge.request_detection(self.app._config_root)

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as voice:
            self.app._on_button_event("up", True)
            self.app._on_button_event("up", False)

        self.assertEqual(key_detection_bridge.poll_detection(request), "up")
        voice.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)


class LiveBridgeKeyDetectionTests(_AppWiringTestCase):
    def test_next_ordinary_button_is_reported_and_its_mapping_is_suppressed(self):
        request = key_detection_bridge.request_detection(self.app._config_root)
        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_button_event("back", True)
            self.app._on_button_event("back", False)

        self.assertEqual(key_detection_bridge.poll_detection(request), "back")
        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._key_detection_suppressed_buttons, set())

    def test_detected_mic_button_never_triggers_host_or_device_voice(self):
        request = key_detection_bridge.request_detection(self.app._config_root)
        with mock.patch.object(win32_input, "send_voice_key_combo_down") as hotkey:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)

        self.assertEqual(key_detection_bridge.poll_detection(request), "mic")
        hotkey.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)

    def test_pending_detection_can_capture_suppressed_legacy_f5(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.HOLD
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("ralt")
        request = key_detection_bridge.request_detection(self.app._config_root)

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as hotkey:
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()

        self.assertEqual(key_detection_bridge.poll_detection(request), "mic")
        hotkey.assert_not_called()

    def test_audio_started_first_detection_suppresses_all_late_mic_sources(self):
        self.app._voice.trigger_mode = key_mapping.VoiceTriggerMode.HOLD
        self.app._voice_hotkey = app_module.hotkey.HotkeySpec.parse("ralt")
        self.app._playback = None
        request = key_detection_bridge.request_detection(self.app._config_root)
        hotkey_calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: hotkey_calls.append(tokens),
        ), mock.patch.object(
            self.app,
            "_open_playback_for_new_session",
        ) as open_playback:
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(MicButtonPressed())

            self.app._on_control_event(AudioStopped())
            self.app._on_button_event("mic", False, event_source="hid")
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

        self.assertEqual(key_detection_bridge.poll_detection(request), "mic")
        self.assertEqual(hotkey_calls, [])
        open_playback.assert_not_called()
        self.assertFalse(self.app._voice.active)
        self.assertFalse(self.app._voice_audio_stream_active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)

    def test_hid_first_detection_suppresses_atvv_audio_and_legacy_f5(self):
        self.app._playback = None
        request = key_detection_bridge.request_detection(self.app._config_root)
        hotkey_calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: hotkey_calls.append(tokens),
        ), mock.patch.object(
            self.app,
            "_open_playback_for_new_session",
        ) as open_playback:
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()

            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()
            self.app._on_control_event(AudioStopped())
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(key_detection_bridge.poll_detection(request), "mic")
        self.assertEqual(hotkey_calls, [])
        open_playback.assert_not_called()
        self.assertFalse(self.app._voice.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)

    def test_detection_requested_mid_voice_waits_for_the_next_press(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()

            request = key_detection_bridge.request_detection(self.app._config_root)
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_control_event(AudioStopped())
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

            self.assertEqual(
                calls,
                [
                    ("down", DEFAULT_VOICE_TOKENS),
                    ("up", DEFAULT_VOICE_TOKENS),
                ],
            )
            self.assertFalse(self.app._voice.active)
            self.assertFalse(self.app._voice_mic_gesture_active)
            self.assertIsNone(key_detection_bridge.poll_detection(request))

            self.app._on_legacy_key_event(0x74, True)
            self._drain_event_loop()
            self.assertEqual(key_detection_bridge.poll_detection(request), "mic")
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

    def test_atvv_first_detection_expires_and_next_normal_press_triggers_voice(self):
        clock = [100.0]
        request = key_detection_bridge.request_detection(self.app._config_root)
        hotkey_calls = []
        with mock.patch.object(
            app_module.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: hotkey_calls.append(tokens),
        ):
            self.app._on_control_event(MicButtonPressed())
            self.app._on_control_event(AudioStarted(session_id=1))
            self.app._on_control_event(AudioStopped())

            clock[0] += app_module._KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS + 0.01
            self.app._on_button_event("mic", True, event_source="hid")

        self.assertEqual(key_detection_bridge.poll_detection(request), "mic")
        self.assertEqual(hotkey_calls, [DEFAULT_VOICE_TOKENS])
        self.assertTrue(self.app._voice.active)
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)


class PlaybackWriteFailureTests(_AppWiringTestCase):
    """XRBM-014 review round 2 P1 #6: a playback write failure must fail
    closed (discard the sink) and request a reconnect, not log indefinitely
    while the device keeps streaming.
    """

    def test_write_failure_requests_reconnect_and_cleanup_closes_sink(self):
        sink = _FakePlaybackSink(fail_write=True)
        self.app._playback = sink
        self.app._voice_pcm_forwarding_enabled = True
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])
        result = self._flush_playback()

        self.assertTrue(result.completed)
        self.assertIsInstance(result.error, OSError)
        self.assertFalse(sink.closed)
        self.assertIs(self.app._playback, sink)
        self.assertEqual(reconnect_calls, [1])

        _run(self.app._cleanup_once())
        self.assertTrue(sink.closed)
        self.assertIsNone(self.app._playback)

    def test_write_success_does_not_touch_playback_or_reconnect(self):
        self.app._voice_pcm_forwarding_enabled = True
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])
        self.assertTrue(self._flush_playback().ok)

        self.assertIsNotNone(self.app._playback)
        self.assertEqual(reconnect_calls, [])

    def test_write_success_logs_the_latest_playback_timing_snapshot(self):
        class TimedSink(_FakePlaybackSink):
            def timing_snapshot(self):
                return app_module.audio_playback.PlaybackTimingSnapshot(
                    open_elapsed_ms=12.0,
                    last_write_elapsed_ms=2.5,
                    max_write_elapsed_ms=3.5,
                    write_count=1,
                    underflow_count=2,
                )

        self.app._playback = TimedSink()
        self.app._voice_pcm_forwarding_enabled = True

        with self.assertLogs(self.app._logger, level="INFO") as captured:
            self.app._on_pcm_frame([1, 2, 3])
            self.assertTrue(self._flush_playback().ok)

        self.assertIn(
            "write_ms=2.50 max_write_ms=3.50 underflows=2",
            "\n".join(captured.output),
        )

    def test_pcm_enqueue_does_not_wait_for_a_blocking_sink_write(self):
        write_started = threading.Event()
        release_write = threading.Event()

        class BlockingSink(_FakePlaybackSink):
            def write(self, samples):
                write_started.set()
                release_write.wait(2.0)
                super().write(samples)

        self.app._playback = BlockingSink()
        self.app._voice_pcm_forwarding_enabled = True

        started = time.monotonic()
        self.app._on_pcm_frame([1, 2, 3])
        elapsed = time.monotonic() - started

        self.assertTrue(write_started.wait(1.0))
        self.assertLess(elapsed, 0.1)
        release_write.set()
        self.assertTrue(self._flush_playback().ok)

    def test_audio_stop_waits_for_queued_pcm_before_releasing_hotkey(self):
        write_started = threading.Event()
        release_write = threading.Event()
        key_up_calls = []

        class BlockingSink(_FakePlaybackSink):
            def write(self, samples):
                write_started.set()
                release_write.wait(2.0)
                super().write(samples)

        self.app._playback = BlockingSink()
        self.app._voice.on_mic_button_pressed()
        self.app._voice_audio_stream_active = True
        self.app._voice_audio_stop_processed = False
        self.app._voice_pcm_forwarding_enabled = True
        self.app._on_pcm_frame([1, 2, 3])
        self.assertTrue(write_started.wait(1.0))

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: key_up_calls.append(tokens),
        ):
            stop_thread = threading.Thread(
                target=self.app._on_control_event,
                args=(AudioStopped(),),
            )
            stop_thread.start()
            time.sleep(0.05)
            self.assertTrue(stop_thread.is_alive())
            self.assertEqual(key_up_calls, [])
            release_write.set()
            stop_thread.join(1.0)

        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(key_up_calls, [DEFAULT_VOICE_TOKENS])

    def test_full_playback_queue_disables_forwarding_and_requests_reconnect(self):
        write_started = threading.Event()
        release_write = threading.Event()
        reconnect_calls = []
        sink = _FakePlaybackSink()

        def write(samples):
            write_started.set()
            release_write.wait(2.0)
            self.app._write_playback_frame(sink, samples)

        self.app._playback = sink
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)
        writer = app_module.audio_playback_worker.PlaybackWriteWorker(
            write,
            self.app._on_playback_worker_error,
            max_pending_frames=1,
        )
        writer.start()
        self.app._playback_writer = writer
        self.app._voice_pcm_forwarding_enabled = True
        self.app._on_pcm_frame([1])
        self.assertTrue(write_started.wait(1.0))
        self.app._on_pcm_frame([2])
        self.app._on_pcm_frame([3])

        self.assertFalse(self.app._voice_pcm_forwarding_enabled)
        self.assertEqual(reconnect_calls, [1])
        self.assertIsInstance(
            writer.failure,
            app_module.audio_playback_worker.PlaybackBackpressureError,
        )
        release_write.set()
        self.assertTrue(self._flush_playback().completed)

    def test_no_playback_open_is_a_silent_no_op(self):
        self.app._playback = None
        self.app._voice_pcm_forwarding_enabled = True
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])  # must not raise

        self.assertEqual(reconnect_calls, [])

    def test_ordinary_mic_unsolicited_audio_never_reaches_existing_sink(self):
        sink = self.app._playback
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()

        self.app._on_control_event(AudioStarted(session_id=1))
        self.app._on_pcm_frame([1, 2, 3])

        self.assertEqual(sink.write_calls, [])
        self.assertFalse(self.app._voice_pcm_forwarding_enabled)
        self.assertEqual(self.app._ble_session.mic_close_calls, 1)


class CrossThreadReconnectTests(_AppWiringTestCase):
    """ble_transport_winrt.py invokes _on_pcm_frame on its own dedicated
    worker thread, never the event-loop thread - a playback failure there
    must still correctly reach request_reconnect().
    """

    def test_on_pcm_frame_failure_from_a_real_worker_thread_requests_reconnect(self):
        self.app._playback = _FakePlaybackSink(fail_write=True)
        self.app._voice_pcm_forwarding_enabled = True
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(
            threading.current_thread()
        )

        worker = threading.Thread(target=self.app._on_pcm_frame, args=([0, 0],))
        worker.start()
        worker.join(timeout=2.0)
        self._flush_playback()

        self.assertEqual(len(reconnect_calls), 1)
        self.assertNotEqual(reconnect_calls[0], threading.main_thread())


class CleanupOwnershipTests(_AppWiringTestCase):
    """XRBM-019 P1 #2/#5: _cleanup_once() must attempt every one of the
    four steps (voice, HID, BLE, playback) regardless of any single step's
    outcome, must retain (not clear) the owner reference for a step whose
    resource reports it is still alive, and must aggregate and raise once
    every step has been attempted - so ConnectionSupervisor.run_forever()
    fails the whole retry loop closed instead of starting a fresh connect()
    generation over resources that might still be live.
    """

    def test_cleanup_clears_every_owner_on_full_success(self):
        self.app._hid_listener = _FakeHidListener()
        self.app._ble_session = _FakeBleSession()
        self.app._playback = _FakePlaybackSink()

        _run(self.app._cleanup_once())  # must not raise

        self.assertIsNone(self.app._hid_listener)
        self.assertIsNone(self.app._ble_session)
        self.assertIsNone(self.app._playback)

    def test_cleanup_drops_queued_f5_and_resets_hook_deduplication(self):
        calls = []
        dispatched = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_legacy_key_event(0x74, True)
            self.assertTrue(self.app._legacy_f5_is_down)
            _run(self.app._cleanup_once())
            self._drain_event_loop()

        self.assertEqual(calls, [])
        self.assertFalse(self.app._legacy_f5_is_down)
        self.assertFalse(self.app._voice.active)
        self.assertIsNone(self.app._voice_hotkey_release_pending)

        self.app._accept_input_events = True
        generation = self.app._legacy_voice_event_generation
        with mock.patch.object(
            self.app,
            "_dispatch_legacy_key_event",
            side_effect=lambda queued_generation, is_pressed: dispatched.append(
                (queued_generation, is_pressed)
            ),
        ):
            self.app._on_legacy_key_event(0x74, True)
            self.app._on_legacy_key_event(0x74, False)
            self._drain_event_loop()

        self.assertEqual(dispatched, [(generation, True), (generation, False)])
        self.assertFalse(self.app._legacy_f5_is_down)

    def test_cleanup_never_releases_alt_without_bridge_owned_down(self):
        with mock.patch.object(win32_input, "send_voice_key_combo_up") as release:
            _run(self.app._cleanup_once())

        release.assert_not_called()

    def test_successful_cleanup_applies_deferred_bindings_before_reconnect(self):
        pending = config.default_key_bindings()
        pending["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        self.app._pending_bindings = pending

        _run(self.app._cleanup_once())

        self.assertIsNone(self.app._pending_bindings)
        self.assertEqual(
            self.app._bindings["bindings"]["mic"]["kind"],
            key_mapping.ActionKind.ESCAPE.value,
        )

    def test_incomplete_cleanup_retains_deferred_bindings(self):
        original_kind = self.app._bindings["bindings"]["mic"]["kind"]
        pending = config.default_key_bindings()
        pending["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        self.app._pending_bindings = pending
        self.app._hid_listener = _FakeHidListener(stop_raises=True)

        with self.assertRaises(app_module.CleanupIncompleteError):
            _run(self.app._cleanup_once())

        self.assertIs(self.app._pending_bindings, pending)
        self.assertEqual(
            self.app._bindings["bindings"]["mic"]["kind"],
            original_kind,
        )

    def test_cleanup_releases_and_clears_pending_ordinary_button_keys(self):
        original = win32_input.send_key_combo_up
        released = []
        self.app._button_key_release_pending = ("ctrl", "l")
        win32_input.send_key_combo_up = lambda keys: released.append(tuple(keys))
        try:
            _run(self.app._cleanup_once())
        finally:
            win32_input.send_key_combo_up = original

        self.assertEqual(released, [("ctrl", "l")])
        self.assertIsNone(self.app._button_key_release_pending)

    def test_cleanup_retains_incomplete_ordinary_button_release(self):
        original = win32_input.send_key_combo_up
        self.app._button_key_release_pending = ("ctrl", "l")
        win32_input.send_key_combo_up = lambda _keys: (_ for _ in ()).throw(
            win32_input.InputCleanupIncompleteError("still stuck")
        )
        try:
            with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
                _run(self.app._cleanup_once())
        finally:
            win32_input.send_key_combo_up = original

        self.assertIn("ordinary button key", str(ctx.exception))
        self.assertEqual(self.app._button_key_release_pending, ("ctrl", "l"))

    def test_hid_stop_failure_retains_hid_owner_but_still_completes_ble_and_playback(self):
        hid = _FakeHidListener(stop_raises=True)
        ble = _FakeBleSession()
        playback = _FakePlaybackSink()
        self.app._hid_listener = hid
        self.app._ble_session = ble
        self.app._playback = playback

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())
        self.assertIn("Raw Input listener", str(ctx.exception))

        # Retained, not hidden:
        self.assertIs(self.app._hid_listener, hid)
        # Every other step still ran and cleared normally:
        self.assertEqual(ble.close_calls, 1)
        self.assertIsNone(self.app._ble_session)
        self.assertTrue(playback.closed)
        self.assertIsNone(self.app._playback)

    def test_ble_close_failure_retains_ble_owner_but_still_completes_hid_and_playback(self):
        hid = _FakeHidListener()
        ble = _FakeBleSession(close_raises=True)
        playback = _FakePlaybackSink()
        self.app._hid_listener = hid
        self.app._ble_session = ble
        self.app._playback = playback

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())
        self.assertIn("BLE session", str(ctx.exception))

        # Retained, not hidden:
        self.assertIs(self.app._ble_session, ble)
        self.assertEqual(ble.close_calls, 1)
        # Every other step still ran and cleared normally:
        self.assertEqual(hid.stop_calls, 1)
        self.assertIsNone(self.app._hid_listener)
        self.assertTrue(playback.closed)
        self.assertIsNone(self.app._playback)

    def test_both_hid_and_ble_failures_retain_both_owners_and_aggregate(self):
        hid = _FakeHidListener(stop_raises=True)
        ble = _FakeBleSession(close_raises=True)
        playback = _FakePlaybackSink()
        self.app._hid_listener = hid
        self.app._ble_session = ble
        self.app._playback = playback

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())
        message = str(ctx.exception)
        self.assertIn("Raw Input listener", message)
        self.assertIn("BLE session", message)

        self.assertIs(self.app._hid_listener, hid)
        self.assertIs(self.app._ble_session, ble)
        # Playback is unconditionally attempted/cleared even though both
        # owner-retaining steps failed.
        self.assertTrue(playback.closed)
        self.assertIsNone(self.app._playback)

    def test_cleanup_failure_propagates_out_of_run_forever_without_a_second_connect(self):
        """End-to-end: wires _cleanup_once() as the real
        ConnectionSupervisor.cleanup callable and proves a retained-owner
        failure ends run_forever() entirely - no second connect()
        generation is ever attempted over the still-live HID listener.
        """

        hid = _FakeHidListener(stop_raises=True)
        self.app._hid_listener = hid
        self.app._ble_session = _FakeBleSession()
        self.app._playback = _FakePlaybackSink()

        connect_calls = []

        async def scenario():
            # ConnectionSupervisor.__init__ captured its ``_loop`` at
            # construction time (setUp() built self.app synchronously,
            # off any running loop - see connection_supervisor.py's module
            # docstring). Rebind it to the loop this coroutine is actually
            # running on before calling request_reconnect(), exactly as
            # the real app does by constructing everything inside one
            # asyncio.run(); otherwise request_reconnect()'s
            # call_soon_threadsafe hop lands on a loop nothing drives and
            # run_forever() hangs forever on _disconnect_event.wait()
            # (XRBM-019 review round 1 P1 #1 - the prior version of this
            # test only "passed" because that loop mismatch raised into
            # the cleanup path, never proving the intended behavior).
            self.app._supervisor._loop = asyncio.get_running_loop()
            self.app._supervisor._connect = lambda: _record_connect(connect_calls)

            task = asyncio.ensure_future(self.app._supervisor.run_forever())
            # Let run_forever() run its first connect() and reach the
            # disconnect_event.wait() suspension point before we end the
            # attempt explicitly - request_reconnect() is what a real BLE
            # disconnect/protocol-error/playback-failure callback would
            # call; nothing here relies on an accidental cross-loop
            # exception to unblock the wait.
            await asyncio.sleep(0)
            self.app._supervisor.request_reconnect()

            # Bounded so a real regression (e.g. cleanup ownership lost
            # again, or the wait never unblocking) fails the test instead
            # of hanging the whole suite.
            with self.assertRaises(app_module.CleanupIncompleteError):
                await asyncio.wait_for(task, timeout=5.0)

        _run(scenario())

        self.assertEqual(connect_calls, [1])  # only the first attempt ever ran
        self.assertEqual(self.app._supervisor.attempt_count, 1)
        # Still retained after the whole supervisor loop ended:
        self.assertIs(self.app._hid_listener, hid)


class StartHidListenerOwnershipTests(_AppWiringTestCase):
    """XRBM-019 review round 1 P1 #3: RawInputButtonListener intentionally
    retains its thread/window when its own bounded failed-start cleanup
    cannot stop them (see raw_input_windows.py's ``_abandon_failed_start``).
    ``_start_hid_listener()`` must consult ``is_running`` rather than
    unconditionally clearing ``self._hid_listener`` to ``None`` on any
    failed ``start()`` - doing so would lose the owner and let a later
    ``_connect_once()`` generation start a second listener over a still-
    live one (the exact defect class XRBM-019 exists to eliminate; see
    also CleanupOwnershipTests' end-to-end supervisor test above, which
    proves no second connect() generation is ever reached once cleanup
    itself fails on a retained owner).
    """

    def _patch_device_discovery(self, fake_listener):
        original_enumerate = app_module.raw_input_windows.enumerate_matching_device_paths
        original_select = app_module.hid_identity.select_single_device_path
        original_listener_cls = app_module.raw_input_windows.RawInputButtonListener
        app_module.raw_input_windows.enumerate_matching_device_paths = lambda: ["fake-path"]
        app_module.hid_identity.select_single_device_path = lambda paths: paths[0]
        app_module.raw_input_windows.RawInputButtonListener = lambda callback: fake_listener

        def _restore():
            app_module.raw_input_windows.enumerate_matching_device_paths = original_enumerate
            app_module.hid_identity.select_single_device_path = original_select
            app_module.raw_input_windows.RawInputButtonListener = original_listener_cls

        return _restore

    def test_a_failed_start_that_is_still_running_retains_the_owner_and_raises(self):
        fake_listener = _FakeHidListenerForFailedStart(is_running_after_failed_start=True)
        restore = self._patch_device_discovery(fake_listener)
        try:
            with self.assertRaises(app_module.raw_input_windows.RawInputUnavailableError):
                self.app._start_hid_listener()
        finally:
            restore()

        self.assertIs(self.app._hid_listener, fake_listener)
        self.assertEqual(fake_listener.start_calls, 1)

    def test_a_failed_start_confirmed_stopped_clears_the_owner(self):
        fake_listener = _FakeHidListenerForFailedStart(is_running_after_failed_start=False)
        restore = self._patch_device_discovery(fake_listener)
        try:
            self.app._start_hid_listener()  # must not raise
        finally:
            restore()

        self.assertIsNone(self.app._hid_listener)
        self.assertEqual(fake_listener.start_calls, 1)

    def test_legacy_guard_failed_start_retains_live_owner_and_raises(self):
        class StartedListener:
            is_running = True

            def set_physical_bindings(self, _bindings):
                pass

            def set_raw_event_callback(self, _callback):
                pass

            def start(self, _device_path):
                pass

            def stop(self):
                pass

        class StuckSuppressor:
            is_running = True

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                raise app_module.legacy_key_suppressor_windows.LegacyKeySuppressorUnavailableError(
                    "simulated failed start"
                )

            def stop(self):
                pass

        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["fake-path"],
        ), mock.patch.object(
            app_module.hid_identity,
            "select_single_device_path",
            return_value="fake-path",
        ), mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            return_value=StartedListener(),
        ), mock.patch.object(
            app_module.legacy_key_suppressor_windows,
            "LegacyKeySuppressor",
            StuckSuppressor,
        ):
            with self.assertRaises(
                app_module.legacy_key_suppressor_windows.LegacyKeySuppressorUnavailableError
            ):
                self.app._start_hid_listener()

        self.assertIsInstance(self.app._legacy_key_suppressor, StuckSuppressor)


class HidTapStartupStateTests(_AppWiringTestCase):
    def test_unhealthy_tap_forces_release_of_its_active_voice_source(self):
        usage = next(
            usage
            for usage, button_id in app_module.frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id == "mic"
        )
        report = usage.to_bytes(2, "little") + b"\x00\x00\x00\x00"

        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.app._on_direct_hid_report(1, report)
            self.assertEqual(self.app._voice_mic_gesture_sources_down, {"hid_tap"})
            self.app._on_hid_tap_status("unhealthy", "socket_lost")

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertEqual(self.app._voice_mic_gesture_sources_down, set())
        self.assertFalse(self.app._voice.active)
        self.assertFalse(self.app._direct_hid_tap_active)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)

    def test_thread_start_is_logged_separately_from_verified_ready(self):
        instances = []

        class FakeTap:
            def __init__(self, _report_handler, *, status_handler):
                self.status_handler = status_handler
                self.status = "starting"
                instances.append(self)

            def start(self):
                return True

            def stop(self):
                pass

        with mock.patch.object(
            app_module.frida_compat, "RC003HidReportTap", FakeTap
        ), self.assertLogs(self.app._logger, level="INFO") as captured:
            self.app._start_hid_report_tap()
            instances[0].status_handler("ready", "hid_io_verified")

        text = "\n".join(captured.output)
        self.assertIn("tap thread started; state=starting", text)
        self.assertNotIn("tap enabled", text)
        self.assertIn("tap state: ready detail=hid_io_verified", text)

    def test_failed_start_cleanup_retains_tap_owner_and_raises(self):
        instances = []

        class StuckTap:
            status = "starting"

            def __init__(self, _report_handler, *, status_handler):
                instances.append(self)

            def start(self):
                raise RuntimeError("start failed")

            def stop(self):
                raise RuntimeError("stop failed")

        with mock.patch.object(
            app_module.frida_compat, "RC003HidReportTap", StuckTap
        ):
            with self.assertRaises(RuntimeError):
                self.app._start_hid_report_tap()

        self.assertIs(self.app._hid_report_tap, instances[0])


class VoiceCleanupFailurePreservesPendingStateTests(_AppWiringTestCase):
    """A failed key-up must remain owed after cleanup or audio stop."""

    def test_cleanup_once_preserves_hold_key_up_on_failure(self):
        self.app._voice.on_mic_button_pressed()
        self.assertTrue(self.app._voice.holding)

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=OSError("simulated key-up delivery failure"),
        ):
            with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
                _run(self.app._cleanup_once())

        self.assertIn("voice hotkey", str(ctx.exception))
        self.assertTrue(self.app._voice.holding)
        self.assertTrue(self.app._voice.active)

    def test_audio_stopped_preserves_hold_key_up_on_failure_and_reconnects(self):
        self.app._voice.on_mic_button_pressed()
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=OSError("simulated key-up delivery failure"),
        ):
            self.app._on_control_event(AudioStopped())

        self.assertTrue(self.app._voice.holding)
        self.assertEqual(reconnect_calls, [1])


class PlaybackCleanupOwnershipTests(_AppWiringTestCase):
    """XRBM-019 review round 1 P1 #5: both _cleanup_once() and
    _on_pcm_frame() must retain (not discard) the playback sink owner when
    its own close() call fails - EndpointPlaybackSink owns a PortAudio
    stream, and clearing the reference would hide an incompletely closed
    resource and let a reconnect open a second sink over it.
    """

    def test_open_success_logs_selected_endpoint_and_host_api(self):
        class OpenSink:
            owns_stream = False
            ready = True
            output_sample_rate_hz = 48000
            output_channels = 2

            def __init__(self, _name, _host_api):
                pass

            def open(self):
                pass

            def timing_snapshot(self):
                return app_module.audio_playback.PlaybackTimingSnapshot(
                    open_elapsed_ms=12.5,
                    last_write_elapsed_ms=0.0,
                    max_write_elapsed_ms=0.0,
                    write_count=0,
                    underflow_count=0,
                )

        self.app._playback = None
        self.app._config["output_endpoint_name"] = "CABLE Input"
        self.app._config["output_endpoint_host_api"] = "Windows WASAPI"
        endpoint = app_module.audio_output.AudioEndpoint(
            name="CABLE Input", host_api="Windows WASAPI"
        )

        with mock.patch.object(
            app_module.audio_output,
            "enumerate_output_endpoints",
            return_value=[endpoint],
        ), mock.patch.object(
            app_module.audio_output,
            "resolve_selected_endpoint",
            return_value=endpoint,
        ), mock.patch.object(
            app_module.audio_playback,
            "EndpointPlaybackSink",
            OpenSink,
        ), self.assertLogs(self.app._logger, level="INFO") as captured:
            self.assertTrue(self.app._open_playback_for_new_session())

        self.assertIn(
            "voice playback opened: endpoint=CABLE Input "
            "host_api=Windows WASAPI sample_rate=48000 channels=2 open_ms=12.50",
            "\n".join(captured.output),
        )

    def test_cleanup_once_retains_playback_owner_on_close_failure(self):
        sink = _FakePlaybackSink(close_raises=True)
        self.app._hid_listener = None
        self.app._ble_session = _FakeBleSession()
        self.app._playback = sink

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())
        self.assertIn("audio playback", str(ctx.exception))

        self.assertIs(self.app._playback, sink)
        self.assertEqual(sink.close_calls, 1)
        self.assertFalse(sink.closed)

    def test_cleanup_retains_sink_when_playback_writer_does_not_stop(self):
        sink = _FakePlaybackSink()

        class StuckWriter:
            def flush(self, _timeout=None):
                return app_module.audio_playback_worker.PlaybackFlushResult(
                    False,
                    app_module.audio_playback_worker.PlaybackFlushTimeoutError(
                        "stuck"
                    ),
                )

            def stop(self, _timeout=None):
                return False

        self.app._hid_listener = None
        self.app._ble_session = _FakeBleSession()
        self.app._playback = sink
        self.app._playback_writer = StuckWriter()

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            _run(self.app._cleanup_once())

        self.assertIn("audio playback writer", str(ctx.exception))
        self.assertIs(self.app._playback, sink)
        self.assertIsNotNone(self.app._playback_writer)
        self.assertEqual(sink.close_calls, 0)

    def test_write_fail_then_cleanup_close_raise_retains_owner(self):
        sink = _FakePlaybackSink(fail_write=True, close_raises=True)
        self.app._playback = sink
        self.app._voice_pcm_forwarding_enabled = True
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)

        self.app._on_pcm_frame([0, 0])  # must not raise
        result = self._flush_playback()
        self.assertTrue(result.completed)
        self.assertIsInstance(result.error, OSError)

        with self.assertRaises(app_module.CleanupIncompleteError):
            _run(self.app._cleanup_once())

        # Retained, not discarded - close() also failed:
        self.assertIs(self.app._playback, sink)
        self.assertEqual(sink.close_calls, 1)
        # Still fails closed via reconnect either way:
        self.assertEqual(reconnect_calls, [1])

    def test_open_failure_with_unclean_stream_retains_owner_and_reconnects(self):
        instances = []

        class FailedOpenSink:
            owns_stream = True
            ready = False

            def __init__(self, _name, _host_api):
                instances.append(self)

            def open(self):
                raise app_module.audio_output.AudioOutputUnavailableError(
                    "simulated open failure"
                )

        self.app._playback = None
        self.app._config["output_endpoint_name"] = "CABLE Input"
        self.app._config["output_endpoint_host_api"] = "Windows WASAPI"
        reconnect_calls = []
        self.app._supervisor.request_reconnect = lambda: reconnect_calls.append(1)
        endpoint = app_module.audio_output.AudioEndpoint(
            name="CABLE Input", host_api="Windows WASAPI"
        )

        with mock.patch.object(
            app_module.audio_output,
            "enumerate_output_endpoints",
            return_value=[endpoint],
        ), mock.patch.object(
            app_module.audio_output,
            "resolve_selected_endpoint",
            return_value=endpoint,
        ), mock.patch.object(
            app_module.audio_playback,
            "EndpointPlaybackSink",
            FailedOpenSink,
        ):
            self.assertFalse(self.app._open_playback_for_new_session())

        self.assertIs(self.app._playback, instances[0])
        self.assertEqual(reconnect_calls, [1])


class LoggingHandlerCleanupRegressionTests(unittest.TestCase):
    """Regression for XRBM-023 outcome 1: proves _AppWiringTestCase's
    tearDown fix (close/remove the FileHandler, reset ``_configured``)
    actually decouples one app build's logging handler from the next -
    the exact defect that made a real Windows CI runner's
    ``tempfile.TemporaryDirectory().cleanup()`` raise a PermissionError
    on the very first test the suite's discovery order ever runs
    (``CleanupOwnershipTests.test_ble_close_failure_retains_ble_owner_but_
    still_completes_hid_and_playback``): ``logging_setup.get_logger()``
    configures its FileHandler exactly once per process and never closes
    it, so without this cleanup the handle stays open inside that first
    test's temp directory for the rest of the run - and Windows, unlike
    POSIX, refuses to delete a directory containing a still-open handle.
    """

    def test_a_second_app_build_gets_its_own_fresh_handler_after_cleanup(self):
        tmp1 = tempfile.TemporaryDirectory()
        loop1 = None
        try:
            # _build_app_with_owned_loop() (not the bare _build_app()): this
            # test constructs RC003App synchronously, same as
            # _AppWiringTestCase.setUp() - see XRBM-026's
            # EventLoopOwnershipRegressionTests for why a bare
            # asyncio.get_event_loop() call here would leak too.
            _, loop1 = _build_app_with_owned_loop(Path(tmp1.name))
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            self.assertEqual(len(logger.handlers), 1)
            handler1 = logger.handlers[0]
            self.assertEqual(
                Path(handler1.baseFilename).parent, Path(tmp1.name) / "logs"
            )
            self.assertIsNotNone(handler1.stream)

            # Exactly what _AppWiringTestCase.tearDown now does.
            handler1.close()
            logger.removeHandler(handler1)
            logging_setup._configured = False

            self.assertIsNone(handler1.stream)
            self.assertEqual(logger.handlers, [])
        finally:
            asyncio.set_event_loop(None)
            if loop1 is not None:
                loop1.close()
            # Must not raise: on Windows this would be the PermissionError
            # from outcome 1 if the handle above were still open.
            tmp1.cleanup()

        tmp2 = tempfile.TemporaryDirectory()
        loop2 = None
        try:
            _, loop2 = _build_app_with_owned_loop(Path(tmp2.name))
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            self.assertEqual(len(logger.handlers), 1)
            handler2 = logger.handlers[0]
            self.assertIsNot(handler2, handler1)
            self.assertEqual(
                Path(handler2.baseFilename).parent, Path(tmp2.name) / "logs"
            )

            handler2.close()
            logger.removeHandler(handler2)
            logging_setup._configured = False
        finally:
            asyncio.set_event_loop(None)
            if loop2 is not None:
                loop2.close()
            tmp2.cleanup()


class EventLoopOwnershipRegressionTests(unittest.TestCase):
    """Regression for XRBM-026 red evidence (real Windows run 29644660267):
    425 tests passed ("OK (skipped=3)"), then the process printed an ignored
    "unclosed event loop" ResourceWarning for a ProactorEventLoop plus two
    unclosed self-pipe sockets - AFTER unittest's own summary, so
    -W error::ResourceWarning never saw it and the step still exited 0.

    Root cause: RC003App.__init__ builds a ConnectionSupervisor, whose
    __init__ captures ``loop or asyncio.get_event_loop()``
    (connection_supervisor.py). _build_app() runs synchronously in
    _AppWiringTestCase.setUp(), off any running loop - unlike the real app,
    which only ever constructs RC003App inside ``asyncio.run(_run())``
    (app.py), where get_event_loop() correctly returns asyncio.run()'s own
    loop. With no running loop and nothing set for this thread,
    asyncio.get_event_loop() silently creates and caches an implicit
    default loop - shared by every _AppWiringTestCase subclass's setUp() -
    that nothing in the old test suite ever closed.

    These tests prove both halves of the fix: (1) the fixed setUp()/
    tearDown() pattern threads a per-test OWNED loop into ConnectionSupervisor
    instead of that ambient default, and (2) deterministically forcing the
    exact condition real interpreter shutdown eventually creates (every
    strong reference to a loop dropped, including asyncio's own thread-local
    cache, then a GC pass) reproduces the red evidence exactly for the OLD
    pattern while the FIXED pattern never reproduces it - in an isolated
    subprocess, so this test process's own asyncio/event-loop state is never
    touched either way.
    """

    def test_build_app_under_the_fixed_setup_pattern_captures_the_owned_loop(self):
        tmp = tempfile.TemporaryDirectory()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            app = _build_app(Path(tmp.name))
            self.assertIs(app._supervisor._loop, loop)
        finally:
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            logging_setup._configured = False
            tmp.cleanup()
            asyncio.set_event_loop(None)
            loop.close()

        self.assertTrue(loop.is_closed())

    def test_unowned_default_loop_pattern_reproduces_the_exact_red_evidence(self):
        # Recreate the OLD ambient-default-loop ownership pattern explicitly.
        # Python 3.14 no longer creates a loop for a bare get_event_loop(), so
        # the subprocess installs the same unowned default first. Dropping
        # every reference then deterministically reproduces one unclosed-loop
        # warning plus the two Proactor self-pipe socket warnings while the
        # script still exits 0.
        script = (
            "import asyncio, gc\n"
            "loop = asyncio.new_event_loop()\n"
            "asyncio.set_event_loop(loop)\n"
            "class _Sup:\n"
            "    def __init__(self):\n"
            "        self._loop = asyncio.get_event_loop()\n"
            "objs = [_Sup() for _ in range(3)]\n"
            "assert all(o._loop is objs[0]._loop for o in objs)\n"
            "del objs\n"
            "asyncio.set_event_loop(None)\n"
            "del loop\n"
            "gc.collect()\n"
            "print('done')\n"
        )
        result = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("unclosed event loop", result.stderr)
        self.assertEqual(result.stderr.count("unclosed <socket.socket"), 2)

    def test_owned_and_closed_loop_pattern_never_reproduces_the_red_evidence(self):
        # Same forced-shutdown stress as the test above, but using the FIXED
        # pattern (_AppWiringTestCase.setUp()/tearDown()'s own approach: a
        # fresh loop is created, set current, then explicitly closed)
        # instead of the bare default-loop getter - proving the fix, not
        # just the bug.
        script = (
            "import asyncio, gc\n"
            "class _Sup:\n"
            "    def __init__(self, loop=None):\n"
            "        self._loop = loop or asyncio.get_event_loop()\n"
            "def _build_owned():\n"
            "    loop = asyncio.new_event_loop()\n"
            "    asyncio.set_event_loop(loop)\n"
            "    sup = _Sup()\n"
            "    asyncio.set_event_loop(None)\n"
            "    loop.close()\n"
            "    return sup\n"
            "objs = [_build_owned() for _ in range(3)]\n"
            "del objs\n"
            "asyncio.get_event_loop_policy()._local._loop = None\n"
            "gc.collect()\n"
            "print('done')\n"
        )
        result = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("ResourceWarning", result.stderr)
        self.assertNotIn("unclosed", result.stderr)


async def _record_connect(connect_calls):
    connect_calls.append(1)


if __name__ == "__main__":
    unittest.main()
