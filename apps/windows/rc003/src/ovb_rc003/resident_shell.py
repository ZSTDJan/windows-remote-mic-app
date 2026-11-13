"""Resident desktop process: notification area, bridge worker and navigation.

The settings window is a separate, on-demand process (``--settings``). This
module owns everything that must stay alive while that window is closed:

- the in-process bridge worker (BLE/ATVV, input, Chromecast supervision),
- the native notification-area icon, and
- the embedded element-navigation runtime, which needs a Qt event loop and is
  triggered by a physical remote button while no window is open.

It deliberately loads no Qt Quick/QML: creating and destroying the settings
window's QML object tree is the settings process's own concern. See
``qt_settings_app.py`` and ``__main__.py`` for that side of the split.

Request handling (all existing file/win32 contracts, no new IPC):

- bridge start: ``single_instance.consume_bridge_start_request`` - lets the
  settings window start the service when the resident was launched with
  ``launch_bridge_on_app_start`` disabled.
- full exit: ``single_instance.consume_and_acknowledge_application_exit_request``
  - the installer and the version handoff use the same request file.

Quitting: tray "退出桥接" and both request paths converge on ``_shutdown`` so
the bridge, navigation hooks and the tray icon are all released before the Qt
event loop ends.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from . import (
    bridge_launcher,
    bridge_tray_windows,
    config,
    element_navigation_control_windows,
    element_navigation_runtime,
    logging_setup,
    single_instance,
)

# How often the resident checks the small, already existing request files.
_REQUEST_POLL_MS = 1000


def _create_capability_window() -> Any:
    """Hidden native window that carries the resident's exit markers."""

    from PySide6.QtWidgets import QWidget

    return QWidget()


class _ResidentShell:
    def __init__(self, application: Any, logger: Any, root: Any) -> None:
        self._application = application
        self._logger = logger
        self._root = root
        self._tray: Optional[bridge_tray_windows.BridgeTray] = None
        self._tray_hwnd = 0
        self._capability_hwnd = 0
        self._capability_window: Any = None
        self._navigation: Any = None
        self._exit_requested = False
        self._shutdown_complete = False
        self._bridge_started = False

    # -- bridge ---------------------------------------------------------
    def start_bridge(
        self,
        *,
        reason: str,
        launch_voice_program_on_start: bool = True,
    ) -> bool:
        if bridge_launcher.in_process_bridge_running():
            self._bridge_started = True
            return True
        try:
            # grace_checks=0: the worker is a thread in this process, so a
            # short "did the process exit" watch adds nothing here.
            result = bridge_launcher.start_in_process_bridge(
                grace_checks=0,
                launch_voice_program_on_start=launch_voice_program_on_start,
            )
        except Exception as exc:  # noqa: BLE001 - tray must stay usable
            self._logger.warning(
                "resident bridge start failed: reason=%s error_type=%s",
                reason,
                type(exc).__name__,
            )
            return False
        started = getattr(result, "outcome", None) in {
            bridge_launcher.LaunchOutcome.STARTED,
            bridge_launcher.LaunchOutcome.ALREADY_RUNNING,
        }
        if not started:
            self._logger.warning(
                "resident bridge start rejected: reason=%s outcome=%s",
                reason,
                getattr(getattr(result, "outcome", None), "value", "unknown"),
            )
            return False
        self._bridge_started = True
        self._logger.info("resident bridge started: reason=%s", reason)
        return True

    def _stop_bridge(self) -> None:
        if not self._bridge_started:
            return
        try:
            stopped = bridge_launcher.stop_in_process_bridge(reason="process_exit")
        except Exception as exc:  # noqa: BLE001 - shutdown stays bounded
            self._logger.warning(
                "resident bridge stop failed: error_type=%s", type(exc).__name__
            )
            return
        if stopped is False:
            self._logger.warning("resident bridge did not stop before exit")

    # -- navigation -----------------------------------------------------
    def start_navigation(self) -> None:
        if sys.platform != "win32":
            return
        if os.environ.get("RC003_DISABLE_LIVE_INPUT") == "1":
            # Same opt-out the embedded desktop path honoured for tests and CI.
            return
        try:
            self._navigation = element_navigation_runtime.start_embedded_element_navigation(
                self._application
            )
        except Exception as exc:  # noqa: BLE001 - optional feature
            self._logger.warning(
                "element navigation unavailable in resident process: %s",
                type(exc).__name__,
            )
            return
        element_navigation_control_windows.bind_embedded_element_navigation(
            self._navigation
        )

    def _stop_navigation(self) -> None:
        if self._navigation is None:
            return
        try:
            element_navigation_control_windows.shutdown_element_navigation()
        except Exception as exc:  # noqa: BLE001 - shutdown stays bounded
            self._logger.warning(
                "element navigation shutdown failed: %s", type(exc).__name__
            )
        finally:
            self._navigation = None

    # -- exit contract ---------------------------------------------------
    def publish_exit_capability(self) -> None:
        """Advertise full exit from a window that never depends on the tray.

        The installer and ``--request-exit`` look for these markers; losing
        them because notification-area registration failed would leave no
        supported way to stop the resident.
        """

        if self._capability_hwnd:
            return
        try:
            holder = _create_capability_window()
            hwnd = int(holder.winId())
        except Exception as exc:  # noqa: BLE001 - optional feature
            self._logger.warning(
                "resident exit capability unavailable: %s", type(exc).__name__
            )
            return
        if not hwnd:
            return
        self._capability_window = holder
        self._capability_hwnd = hwnd
        single_instance.mark_resident_exit_capability(hwnd)

    # -- tray -----------------------------------------------------------
    def start_tray(self) -> None:
        tray = bridge_tray_windows.BridgeTray(
            on_open_settings=self._open_settings,
            on_exit_requested=self._request_exit,
            status_handler=lambda message: self._logger.info(
                "notification area: %s", message
            ),
        )
        try:
            if tray.start():
                self._tray = tray
                self._tray_hwnd = tray.window_handle
                # Advertise the full-exit contract while no settings window
                # exists; the installer and --request-exit look for it.
                single_instance.mark_resident_exit_capability(self._tray_hwnd)
                self._logger.info("notification area: resident control icon started")
            else:
                self._logger.warning(
                    "notification area unavailable: %s",
                    tray.startup_error or "unknown_error",
                )
        except Exception:  # noqa: BLE001 - tray failure must not kill the bridge
            self._logger.exception("notification area failed to initialize")

    def _open_settings(self) -> None:
        result = bridge_launcher.launch_settings()
        if result.started:
            self._logger.info("notification area: settings opened; pid=%s", result.pid)
        else:
            self._logger.warning(
                "notification area: settings launch failed: %s",
                result.error or "unknown_error",
            )

    def _stop_tray(self) -> None:
        tray = self._tray
        self._tray = None
        if tray is None:
            return
        try:
            if not tray.stop():
                self._logger.warning("notification area thread did not stop cleanly")
        except Exception as exc:  # noqa: BLE001 - shutdown stays bounded
            self._logger.warning(
                "notification area stop failed: error_type=%s", type(exc).__name__
            )

    # -- lifecycle ------------------------------------------------------
    def request_exit(self) -> None:
        self._exit_requested = True

    def _request_exit(self) -> None:
        """Tray callback - may run on the tray thread, so only set a flag."""

        self.request_exit()

    def _close_settings_window(self) -> None:
        """Take the on-demand window down with the product it shows."""

        if single_instance.request_settings_window_close():
            self._logger.info("resident exit: settings window close requested")

    def poll(self) -> bool:
        """Consume request files; return True when the process should quit."""

        if self._exit_requested:
            self._close_settings_window()
            return True
        window_request = None
        for hwnd in (self._tray_hwnd, self._capability_hwnd):
            try:
                window_request = (
                    single_instance.consume_settings_window_exit_request(hwnd)
                )
            except (OSError, single_instance.SingleInstanceUnavailableError):
                window_request = None
            if window_request is not None:
                break
        if window_request is not None:
            self._logger.info("resident exit requested by installer window signal")
            self._close_settings_window()
            return True
        try:
            if single_instance.consume_and_acknowledge_application_exit_request(
                self._root
            ) is not None:
                self._logger.info("resident exit requested by application request")
                self._close_settings_window()
                return True
        except (OSError, single_instance.SingleInstanceUnavailableError):
            pass
        try:
            requested = single_instance.consume_bridge_start_request(
                self._root,
                session_scoped=True,
            )
        except (OSError, single_instance.SingleInstanceUnavailableError):
            requested = False
        if requested:
            if bridge_launcher.in_process_bridge_running():
                # A running worker interprets the same request as "wake the
                # retry backoff now" - the settings window's immediate
                # reconnect button uses this path in the split runtime.
                try:
                    bridge_launcher.reconnect_in_process_bridge_now()
                except Exception as exc:  # noqa: BLE001 - tray stays usable
                    self._logger.warning(
                        "resident reconnect request failed: error_type=%s",
                        type(exc).__name__,
                    )
            else:
                self.start_bridge(reason="settings_request")
        return False

    def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self._stop_navigation()
        self._stop_bridge()
        self._stop_tray()


def main(
    *,
    start_bridge: bool = True,
    launch_voice_program_on_start: bool = True,
) -> int:
    """Run the resident process until a tray/exit request ends it."""

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    root = config.config_root()
    logger = logging_setup.get_logger(root)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setQuitOnLastWindowClosed(False)

    shell = _ResidentShell(application, logger, root)
    # The full-exit contract must exist before the notification area is even
    # attempted: installer exit requests and the version handoff depend on it.
    shell.publish_exit_capability()
    # Navigation first: it must be bound before a mapped remote key can try to
    # toggle it, and the settings process no longer hosts it.
    shell.start_navigation()
    if start_bridge:
        shell.start_bridge(
            reason="resident_start",
            launch_voice_program_on_start=launch_voice_program_on_start,
        )
    shell.start_tray()

    timer = QTimer()
    timer.setInterval(_REQUEST_POLL_MS)
    timer.timeout.connect(lambda: shell.poll() and application.quit())
    timer.start()

    try:
        return int(application.exec())
    finally:
        timer.stop()
        shell.shutdown()


__all__ = ("main",)
