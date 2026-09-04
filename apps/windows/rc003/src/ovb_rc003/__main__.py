"""``python -m ovb_rc003`` (or the packaged ``RemoteMicRC003.exe``,
built from the standalone ``src/launcher.py`` entry point - see XRBM-021):

- (no args)     open the settings window - the DEFAULT double-click
                behavior. One per-session application guard owns the window,
                notification icon, bridge worker and embedded navigator; a
                repeat launch of the same copy restores that process. A
                different version/location asks to switch, waits for the old
                copy to exit normally, then continues the original launch.
- ``--settings``  open the settings window (explicit form of the default)
- ``--background``  start the desktop shell hidden in the notification area
- ``--bridge``  compatibility form for old shortcuts. It starts the same
                desktop application hidden and asks that single process to
                run its bridge worker; it never creates a second long-lived
                bridge process.
- ``--bridge-from-settings``  HIDDEN compatibility marker retained in old
                launch commands. Current product startup already routes
                ``--bridge`` to the one desktop process, so the marker has no
                separate runtime role.
- ``--request-exit``  HIDDEN maintenance entry point. It writes one bounded,
                atomic full-exit request for the resident desktop process and
                waits for the application mutex to disappear. It never opens
                a window or starts BLE, HID, navigation, or audio resources.
- ``--dry-run``   import every first-party module and exit 0, touching no
                   GUI, BLE, Raw Input, or audio device - the safe smoke
                  check build-candidate.ps1 and
                  .github/workflows/windows-rc003-ci.yml run against a
                  freshly built artifact (XRBM-014 review RETRY P2 #3: a
                   build that produces an executable which cannot even be
                   launched is not caught by the PyInstaller build step
                   alone).
- ``--qt-runtime-check``  HIDDEN build-only smoke check that imports the Qt
                   modules used by the settings window and verifies the
                   frozen main QML file exists. It constructs no window and
                   touches no BLE/HID/audio resource.
- ``--diagnose-ble-candidates <result-path>``  HIDDEN, undocumented in
                  ``--help`` on purpose (XRBM-035 RETRY 1): the settings
                  window's "检查与修复" page's BLE candidate check
                  re-invokes this same entry point (source:
                  ``sys.executable -m ovb_rc003 --diagnose-ble-candidates
                  <result-path>``; frozen build: the packaged .exe
                  re-invoked with just the flag + path - see
                  ``windows_diagnostics.build_ble_diagnostics_subprocess_
                  command()``) as a disposable CHILD PROCESS purely so the
                  parent can forcibly terminate/kill it with a real,
                  OS-confirmed hard bound if the native WinRT call it makes
                  never returns - something no in-process asyncio
                  cancellation can guarantee (see
                  ``windows_diagnostics.py``'s "-- BLE candidate --"
                  section for the full story). ``<result-path>`` is where
                  this process writes its ONE, strictly-shaped result JSON
                  file - NEVER stdout, which a real PyInstaller
                  ``console=False`` build sets to ``None`` (see that same
                  module section for the citation) - and a missing/empty
                  path here fails this branch CLOSED (a nonzero exit,
                  before ever attempting discovery, never a fallback
                  location and never falling through to running the
                bridge). Never launched by a real end user directly; not
                part of this program's public CLI surface.
- ``--diagnose-vb-cable-loopback <request-path> <result-path>``  HIDDEN
                child-process entry point for the explicit active audio
                test. The parent can terminate this disposable process if
                PortAudio blocks while opening, running, or closing a
                stream, so settings shutdown never depends on that native
                call returning in-process.
- ``--preflight-output-endpoint <request-path> <result-path>``  HIDDEN
                child-process entry point for opening one selected audio
                endpoint. It uses the same bounded disposable-process
                boundary, so a blocked PortAudio open/close cannot freeze
                the settings window.
- ``--rc003-hid-injector --pid <pid>``  HIDDEN child-process entry point for
                the verified HID tap injector. It validates the current
                RC003 WUDFHost target and returns a stable exit code; it never
                 falls through to settings or bridge startup.
- ``--element-navigation``  HIDDEN compatibility entry point for older
                  standalone navigator launches. Normal product navigation
                  is hosted by the desktop Qt process; this branch never
                  falls through to desktop or bridge startup.
- ``--help``/``-h``  print this usage and exit 0

``--settings``, ``--bridge``, ``--request-exit``, ``--dry-run``, ``--qt-runtime-check``,
``--diagnose-ble-candidates``, ``--diagnose-vb-cable-loopback``,
``--preflight-output-endpoint`` and
``--help``/``-h`` are all checked and dispatched before desktop startup.
Dry-run, diagnostics and help touch neither application nor bridge ownership.
"""

from __future__ import annotations

import sys
import time
import uuid

from . import __version__
from . import dev_session
from . import product_identity

SETTINGS_STARTUP_FAILED_EXIT_CODE = 15
ELEMENT_NAVIGATION_RUNTIME_FAILED_EXIT_CODE = 18
APPLICATION_EXIT_REQUEST_FAILED_EXIT_CODE = 19
APPLICATION_EXIT_REQUEST_TIMEOUT_EXIT_CODE = 20
APPLICATION_EXIT_REQUEST_TIMEOUT_SECONDS = 45.0
APPLICATION_EXIT_REQUEST_POLL_SECONDS = 0.1
APPLICATION_HANDOFF_TIMEOUT_SECONDS = 180.0
APPLICATION_HANDOFF_POLL_SECONDS = 0.1
APPLICATION_HANDOFF_REQUEST_GRACE_SECONDS = 5.0


def _print_help() -> None:
    print(
        f"{product_identity.DISPLAY_NAME} - 小米遥控器2 Pro Windows 客户端 "
        f"{__version__}"
    )
    print("Partially real-device verified - see this package's README.md and TESTING.md.")
    print()
    print("Usage:")
    print("  python -m ovb_rc003               open the settings window (default)")
    print("  python -m ovb_rc003 --settings    open the settings window")
    print("  python -m ovb_rc003 --background  start hidden in the notification area")
    print("  python -m ovb_rc003 --bridge      start hidden and run the bridge")
    print("  python -m ovb_rc003 --dry-run     import every module and exit 0 (CI smoke check)")
    print("  python -m ovb_rc003 --help        show this message and exit 0")


def _dry_run() -> int:
    """Imports every first-party module this package ships, without
    constructing a Qt window, opening a BLE connection, starting Raw Input,
    or touching an audio device. Importing ``settings_ui``/``qt_settings_app``
    never requires PySide6-Essentials to be installed (XRBM-030): both defer
    any Qt import to inside a function body, only reached when the settings
    window is actually opened - see qt_settings_app.py's module docstring.
    """

    from . import (  # noqa: F401
        app,
        atvv_protocol,
        atvv_session,
        audio_output,
        audio_playback,
        ble_transport_winrt,
        bridge_launcher,
        bridge_tray_windows,
        config,
        connection_supervisor,
        device_catalog,
        device_profile,
        dev_session,
        doubao_rpc,
        element_navigation_control_windows,
        element_navigation_runtime,
        frida_compat,
        frida_hid_tap_injector,
        hid_elevation_windows,
        hid_identity,
        hotkey,
        identity,
        key_testing,
        key_mapping,
        logging_setup,
        product_identity,
        qt_settings_app,
        raw_input_windows,
        remote_layout,
        resources,
        settings_ui,
        shell_targets,
        single_instance,
        startup_windows,
        voice_controller,
        voice_key_physicalizer_windows,
        win32_input,
        win32_keys,
    )

    print("dry-run: all ovb_rc003 modules imported successfully")
    return 0


def _qt_runtime_check() -> int:
    """Load the frozen Qt dependency chain without constructing a window."""

    import importlib

    from . import qt_settings_app

    qt_modules = (
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickControls2",
        "PySide6.QtSvg",
        "PySide6.QtWidgets",
    )
    for module_name in qt_modules:
        importlib.import_module(module_name)

    main_qml = qt_settings_app._qml_directory() / "main.qml"
    if not main_qml.is_file():
        raise RuntimeError(f"frozen QML entry point is missing: {main_qml}")

    print("qt-runtime-check: Qt modules and main.qml are available")
    return 0


def _request_application_exit(
    *,
    timeout: float = APPLICATION_EXIT_REQUEST_TIMEOUT_SECONDS,
    poll_interval: float = APPLICATION_EXIT_REQUEST_POLL_SECONDS,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> int:
    """Request full cleanup from the resident process and wait for exit."""

    from . import config, single_instance

    root = config.config_root()
    request_path = single_instance.application_exit_request_path(root)
    try:
        running = single_instance.application_instance_running()
    except Exception as exc:
        print(
            "application exit status unavailable: "
            f"error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        return APPLICATION_EXIT_REQUEST_FAILED_EXIT_CODE
    if not running:
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass
        return 0

    try:
        single_instance.write_application_exit_request(root)
    except OSError as exc:
        print(
            "application exit request failed: "
            f"error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        return APPLICATION_EXIT_REQUEST_FAILED_EXIT_CODE

    deadline = monotonic() + max(0.1, float(timeout))
    while True:
        try:
            running = single_instance.application_instance_running()
        except Exception as exc:
            print(
                "application exit confirmation failed: "
                f"error_type={type(exc).__name__}",
                file=sys.stderr,
            )
            return APPLICATION_EXIT_REQUEST_FAILED_EXIT_CODE
        if not running:
            try:
                request_path.unlink(missing_ok=True)
            except OSError:
                pass
            return 0
        if monotonic() >= deadline:
            return APPLICATION_EXIT_REQUEST_TIMEOUT_EXIT_CODE
        sleep(max(0.01, float(poll_interval)))


def _handoff_previous_application(
    *,
    timeout: float = APPLICATION_HANDOFF_TIMEOUT_SECONDS,
    poll_interval: float = APPLICATION_HANDOFF_POLL_SECONDS,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> bool:
    """Wait for a different executable copy to exit, without force-stopping it."""

    from . import config, single_instance

    try:
        handoff_guard = single_instance.ApplicationHandoffInstanceGuard()
        handoff_guard.__enter__()
    except single_instance.DuplicateInstanceError:
        if not single_instance.activate_existing_settings_window():
            single_instance.show_bridge_startup_blocked_notice(
                "另一个无线麦副本正在切换运行版本，但旧窗口未能自动唤出。"
                "请从通知区域打开旧版并选择“完全退出”；不要重复启动。"
            )
            return False
        single_instance.show_bridge_startup_blocked_notice(
            "另一个无线麦副本正在切换运行版本。请先处理已经唤出的旧版窗口，"
            "不要重复启动。"
        )
        return False
    except Exception:
        single_instance.show_bridge_startup_blocked_notice(
            "无法安全建立版本切换。当前版本没有启动；请完全退出旧版后重试。"
        )
        return False

    try:
        try:
            running = single_instance.application_instance_running()
        except Exception:
            single_instance.show_bridge_startup_blocked_notice(
                "无法确认旧版是否仍在运行。当前版本没有启动；"
                "请完全退出旧版后重试。"
            )
            return False
        if not running:
            return True
        if not single_instance.confirm_application_handoff(__version__):
            if not single_instance.activate_existing_settings_window():
                single_instance.show_bridge_startup_blocked_notice(
                    "旧版窗口未能自动唤出。请从通知区域打开现有无线麦。"
                )
            return False

        root = config.config_root()
        request_id: str | None = None
        success = False
        failure_message = ""
        try:
            candidate_request_id = uuid.uuid4().hex
            try:
                single_instance.write_application_exit_request(
                    root,
                    request_id=candidate_request_id,
                )
                request_id = candidate_request_id
            except OSError:
                # Older portable builds do not consume this request anyway.
                # Their visible "完全退出" action remains the compatibility
                # path when the request cannot be written.
                pass

            activated = single_instance.activate_existing_settings_window()
            manual_exit_notice_pending = not activated
            started_at = monotonic()
            bounded_timeout = max(0.1, float(timeout))
            deadline = started_at + bounded_timeout
            manual_notice_deadline = started_at + min(
                APPLICATION_HANDOFF_REQUEST_GRACE_SECONDS,
                bounded_timeout,
            )
            while True:
                try:
                    running = single_instance.application_instance_running()
                except Exception:
                    failure_message = (
                        "无法确认旧版是否已经完全退出。当前版本没有启动；"
                        "请从旧版通知区域选择“完全退出”后，再双击当前版本。"
                    )
                    break
                if not running:
                    success = True
                    break
                now = monotonic()
                if manual_exit_notice_pending and request_id is not None:
                    if not single_instance.owned_application_exit_request_pending(
                        root,
                        request_id,
                    ):
                        manual_exit_notice_pending = False
                    elif now < manual_notice_deadline:
                        pass
                    else:
                        single_instance.show_bridge_startup_blocked_notice(
                            "旧版窗口未能自动唤出，退出请求也尚未被接收。"
                            "请从通知区域打开旧版，保存需要保留的修改后选择“完全退出”。"
                        )
                        manual_exit_notice_pending = False
                elif manual_exit_notice_pending:
                    single_instance.show_bridge_startup_blocked_notice(
                        "旧版窗口未能自动唤出。当前版本正在等待；请从通知区域打开旧版，"
                        "保存需要保留的修改后选择“完全退出”。"
                    )
                    manual_exit_notice_pending = False
                if now >= deadline:
                    failure_message = (
                        "旧版仍在运行，当前版本没有启动。请从旧版通知区域选择"
                        "“完全退出”后，再双击当前版本。"
                    )
                    break
                sleep(max(0.01, float(poll_interval)))
        finally:
            if request_id is not None and not single_instance.clear_owned_application_exit_request(
                root,
                request_id,
            ):
                if success:
                    failure_message = (
                        "旧版已经退出，但版本切换请求未能安全清理。当前版本没有启动；"
                        "请稍后重新双击当前版本。"
                    )
                elif failure_message:
                    failure_message += " 同时，版本切换请求也未能安全清理。"
                else:
                    failure_message = (
                        "版本切换请求未能安全清理。当前版本没有启动；"
                        "请稍后重新双击当前版本。"
                    )
                success = False
        if failure_message:
            single_instance.show_bridge_startup_blocked_notice(failure_message)
        return success
    finally:
        handoff_guard.__exit__(None, None, None)


def main() -> None:
    args = dev_session.consume_marker(sys.argv[1:])
    if "--help" in args or "-h" in args:
        _print_help()
        return
    if "--dry-run" in args:
        raise SystemExit(_dry_run())
    if "--qt-runtime-check" in args:
        raise SystemExit(_qt_runtime_check())
    if "--request-exit" in args:
        raise SystemExit(_request_application_exit())
    if "--diagnose-ble-candidates" in args:
        # XRBM-035 RETRY 1: hidden, undocumented child-process entry point -
        # see this module's own docstring. Always raises SystemExit from
        # this branch (never `return`s, never falls through below), which
        # is itself part of the fail-closed contract: whatever
        # run_ble_diagnostics_subprocess_entrypoint() decides, this process
        # can never end up starting the desktop application by accident. A missing
        # result-path argument (index out of range) is passed through as
        # None - that function's own contract is to fail closed on that,
        # not this dispatch site's job to second-guess.
        from . import windows_diagnostics

        flag_index = args.index("--diagnose-ble-candidates")
        result_path = args[flag_index + 1] if flag_index + 1 < len(args) else None
        raise SystemExit(
            windows_diagnostics.run_ble_diagnostics_subprocess_entrypoint(result_path)
        )
    if "--diagnose-vb-cable-loopback" in args:
        from . import windows_diagnostics

        flag_index = args.index("--diagnose-vb-cable-loopback")
        request_path = args[flag_index + 1] if flag_index + 1 < len(args) else None
        result_path = args[flag_index + 2] if flag_index + 2 < len(args) else None
        raise SystemExit(
            windows_diagnostics.run_vb_cable_loopback_subprocess_entrypoint(
                request_path, result_path
            )
        )
    if "--preflight-output-endpoint" in args:
        from . import windows_diagnostics

        flag_index = args.index("--preflight-output-endpoint")
        request_path = args[flag_index + 1] if flag_index + 1 < len(args) else None
        result_path = args[flag_index + 2] if flag_index + 2 < len(args) else None
        raise SystemExit(
            windows_diagnostics.run_output_endpoint_preflight_subprocess_entrypoint(
                request_path, result_path
            )
        )
    if "--rc003-hid-injector" in args:
        # Hidden entry point used only by the verified Frida Gadget tap. It
        # must never fall through to bridge startup.
        from . import frida_compat

        flag_index = args.index("--rc003-hid-injector")
        raise SystemExit(frida_compat.injector_main(args[flag_index + 1 :]))
    if "--element-navigation" in args:
        from . import element_navigation_runtime

        flag_index = args.index("--element-navigation")
        try:
            exit_code = element_navigation_runtime.run_element_navigation(
                args[flag_index + 1 :]
            )
        except Exception as exc:
            print(
                "element navigation runtime failed: "
                f"error_type={type(exc).__name__}",
                file=sys.stderr,
            )
            exit_code = ELEMENT_NAVIGATION_RUNTIME_FAILED_EXIT_CODE
        raise SystemExit(exit_code)
    if "--settings" in args:
        _run_settings()
        return
    if "--background" in args:
        _run_settings(start_hidden=True, activate_duplicate=False)
        return
    if "--bridge" in args:
        # Compatibility for old shortcuts: bridge mode is now the same
        # desktop application, started hidden with its in-process bridge on.
        _run_settings(
            start_hidden=True,
            start_bridge=True,
            activate_duplicate=False,
        )
        return

    # Default (no arguments) and explicit --settings both open the settings
    # window. A user who double-clicks the packaged exe must see a window,
    # never a headless bridge process with no UI.
    _run_settings()


def _run_settings(
    *,
    start_hidden: bool = False,
    start_bridge: bool = False,
    activate_duplicate: bool = True,
) -> None:
    from . import settings_ui, single_instance

    def run_owned_application() -> None:
        with single_instance.ApplicationInstanceGuard():
            if start_hidden:
                settings_ui.main(
                    start_hidden=True,
                    start_bridge=start_bridge,
                )
            else:
                settings_ui.main(start_bridge=start_bridge)

    def handle_current_runtime_duplicate() -> None:
        if start_bridge:
            from . import config

            try:
                single_instance.write_bridge_start_request(config.config_root())
            except OSError:
                single_instance.show_bridge_startup_blocked_notice(
                    "现有程序正在运行，但无法向它发送启动遥控器服务的请求。"
                    "请打开现有窗口后手动启动服务。"
                )
            return
        if activate_duplicate:
            if single_instance.activate_current_runtime_settings_window():
                return
            activated = single_instance.activate_existing_settings_window()
            if activated:
                single_instance.show_bridge_startup_blocked_notice(
                    "当前副本已有一个启动或运行流程。刚刚唤出的窗口可能是旧版，"
                    "请以窗口标题版本号为准；若是旧版，请完成已有的版本切换。"
                    "旧版退出后，等待中的当前版本会自动打开，不要再次双击。"
                )
            else:
                single_instance.show_bridge_startup_blocked_notice(
                    f"{product_identity.DISPLAY_NAME}当前副本已经在启动、等待旧版退出，"
                    "或已经在运行，但暂时无法唤出窗口。请从通知区域打开现有程序，"
                    "完成已有切换；不要重复启动。"
                )
    try:
        with single_instance.ApplicationRuntimeInstanceGuard():
            try:
                run_owned_application()
            except single_instance.DuplicateInstanceError:
                if start_bridge or not activate_duplicate:
                    handle_current_runtime_duplicate()
                    return
                if not _handoff_previous_application():
                    return
                try:
                    run_owned_application()
                except single_instance.DuplicateInstanceError:
                    single_instance.activate_existing_settings_window()
                    single_instance.show_bridge_startup_blocked_notice(
                        "旧版退出后，另一个无线麦副本先取得了运行权。"
                        "当前副本没有继续启动，请使用已经打开的窗口。"
                    )
    except single_instance.DuplicateInstanceError:
        handle_current_runtime_duplicate()
        return
    except Exception as exc:
        print(
            f"settings startup failed: error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        single_instance.show_bridge_startup_blocked_notice(
            f"{product_identity.DISPLAY_NAME}设置窗口无法启动。现有配置不会被自动覆盖；"
            "请检查日志目录和配置文件后重试。"
        )
        raise SystemExit(SETTINGS_STARTUP_FAILED_EXIT_CODE)


if __name__ == "__main__":
    main()
