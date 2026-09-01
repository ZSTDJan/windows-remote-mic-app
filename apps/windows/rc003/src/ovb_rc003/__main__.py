"""``python -m ovb_rc003`` (or the packaged ``RemoteMicRC003.exe``,
built from the standalone ``src/launcher.py`` entry point - see XRBM-021):

- (no args)     open the settings window - the DEFAULT double-click
                behavior. Settings has its own per-session single-instance
                guard: a repeat launch restores the existing window without
                touching BLE/HID/audio or the bridge guard.
- ``--settings``  open the settings window (explicit form of the default)
- ``--background``  start the desktop shell hidden in the notification area
- ``--bridge``  run the bridge - guarded by a per-session Windows named-
                mutex (single_instance.py) so a second concurrent launch
                never starts BLE/HID/audio: it shows a visible notice and
                exits with a deterministic nonzero code instead of ever
                calling ``app.main()``. This guard FAILS CLOSED (XRBM-021
                review round 1 P1 #1): if the mutex API itself could not be
                used to prove single ownership - not just a confirmed
                duplicate - the bridge does not start either. A caller
                that cannot prove it is the only owner of BLE/HID/audio
                resources must never gamble on being one anyway.
- ``--bridge-from-settings``  HIDDEN marker used only together with
                ``--bridge`` by the settings window. A confirmed duplicate
                still exits with the same nonzero code, but skips the modal
                notice so the parent can classify the result immediately.
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
- ``--element-navigation``  HIDDEN companion-process entry point for the
                  isolated UI Automation navigator. The bridge communicates
                  with it through a local Win32 command window; it never
                  falls through to settings or bridge startup.
- ``--help``/``-h``  print this usage and exit 0

``--settings``, ``--bridge``, ``--dry-run``, ``--qt-runtime-check``,
``--diagnose-ble-candidates``, ``--diagnose-vb-cable-loopback``,
``--preflight-output-endpoint`` and
``--help``/``-h`` are all checked and dispatched BEFORE the bridge branch
below is ever reached. Settings uses its own mutex; dry-run, diagnostics and
help touch neither settings nor bridge ownership.
"""

from __future__ import annotations

import sys

from . import __version__
from . import dev_session
from . import product_identity

SETTINGS_STARTUP_FAILED_EXIT_CODE = 15
BRIDGE_CONFIG_FAILED_EXIT_CODE = 16
BRIDGE_RUNTIME_FAILED_EXIT_CODE = 17
ELEMENT_NAVIGATION_RUNTIME_FAILED_EXIT_CODE = 18


def _print_help() -> None:
    print(
        f"{product_identity.DISPLAY_NAME} - 小米遥控器2 Pro Windows 客户端 "
        f"{__version__}"
    )
    print("Real-device acceptance completed; see README.md and TESTING.md.")
    print()
    print("Usage:")
    print("  python -m ovb_rc003               open the settings window (default)")
    print("  python -m ovb_rc003 --settings    open the settings window")
    print("  python -m ovb_rc003 --background  start hidden in the notification area")
    print("  python -m ovb_rc003 --bridge      run the bridge")
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


def _run_bridge(*, quiet_duplicate: bool = False) -> None:
    """No-argument bridge mode: guarded by the per-session single-instance
    mutex (XRBM-021 In-scope items 2-3). ``app.main()`` is only ever called
    from INSIDE the guard's ``with`` block (first owner) - NEVER from any
    failure branch below. This fails CLOSED (XRBM-021 review round 1 P1
    #1): a confirmed duplicate and an acquisition failure the guard could
    not resolve are both treated as "cannot prove sole ownership", so both
    show a visible notice, exit nonzero, and never start BLE/HID/audio - a
    caller that cannot verify it is safe must not start anyway.

    ``MutexCleanupError`` (raised from the guard's ``__exit__``, i.e. AFTER
    ``app.main()`` has already run to completion or raised) is also caught
    here: the packaged executable is windowed (``console=False``), so an
    unhandled exception's traceback would never be seen by the user at all
    - this still needs a visible, SANITIZED notice (not the raw exception
    text, which is for diagnostics/stderr only) and a deterministic nonzero
    exit rather than silently disappearing.
    """

    from . import app, config, device_catalog, single_instance

    try:
        selected_device_id = device_catalog.normalize_device_id(
            config.load_config(config.config_path()).get("selected_device_profile")
        )
    except Exception as exc:
        print(
            f"bridge configuration load failed: error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        single_instance.show_bridge_startup_blocked_notice(
            f"{product_identity.DISPLAY_NAME}无法读取现有配置，因此不会启动桥接，"
            "也不会覆盖原配置。"
            "请先打开设置目录检查 config.json 和 key_bindings.json。"
        )
        raise SystemExit(BRIDGE_CONFIG_FAILED_EXIT_CODE)
    if selected_device_id == device_catalog.DJI_MIC_2_ID:
        single_instance.show_bridge_startup_blocked_notice(
            "当前设备是 DJI Mic 2。它由 Windows 作为系统录音输入使用，不需要也不会启动 "
            f"小米遥控器2 Pro 的蓝牙按键与语音桥接。请在{product_identity.DISPLAY_NAME}"
            "设置中检查录音端点。",
            title=product_identity.DISPLAY_NAME,
        )
        return

    try:
        with single_instance.BridgeInstanceGuard():
            if quiet_duplicate:
                app.main(show_notification_icon=False)
            else:
                app.main()
    except single_instance.DuplicateInstanceError as exc:
        if not quiet_duplicate:
            single_instance.show_bridge_startup_blocked_notice(str(exc))
        raise SystemExit(single_instance.DUPLICATE_INSTANCE_EXIT_CODE)
    except single_instance.SingleInstanceUnavailableError as exc:
        single_instance.show_bridge_startup_blocked_notice(
            f"{product_identity.DISPLAY_NAME} could not verify no other instance "
            f"is already running, so it will not start. ({exc})"
        )
        raise SystemExit(single_instance.GUARD_UNAVAILABLE_EXIT_CODE)
    except single_instance.MutexCleanupError as exc:
        # Diagnostic detail (fixed operation names/error codes only, per
        # single_instance.py's "no raw handle" contract - see that
        # exception's own message construction) goes to stderr for anyone
        # who can see it; the user-visible notice stays a fixed, sanitized
        # sentence regardless of the exact underlying failure.
        print(f"single-instance mutex cleanup failed: {exc}", file=sys.stderr)
        single_instance.show_bridge_startup_blocked_notice(
            f"{product_identity.DISPLAY_NAME} closed, but could not fully release "
            "its single-instance lock. If it will not start again, check "
            "Task Manager for a lingering process before retrying."
        )
        raise SystemExit(single_instance.CLEANUP_FAILED_EXIT_CODE)
    except Exception as exc:
        print(
            f"bridge runtime failed: error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        single_instance.show_bridge_startup_blocked_notice(
            f"{product_identity.DISPLAY_NAME}桥接启动或运行失败，已停止本次进程。"
            "请打开日志目录查看固定诊断标记后重试。"
        )
        raise SystemExit(BRIDGE_RUNTIME_FAILED_EXIT_CODE)


def main() -> None:
    args = dev_session.consume_marker(sys.argv[1:])
    if "--help" in args or "-h" in args:
        _print_help()
        return
    if "--dry-run" in args:
        raise SystemExit(_dry_run())
    if "--qt-runtime-check" in args:
        raise SystemExit(_qt_runtime_check())
    if "--diagnose-ble-candidates" in args:
        # XRBM-035 RETRY 1: hidden, undocumented child-process entry point -
        # see this module's own docstring. Always raises SystemExit from
        # this branch (never `return`s, never falls through below), which
        # is itself part of the fail-closed contract: whatever
        # run_ble_diagnostics_subprocess_entrypoint() decides, this process
        # can never end up calling _run_bridge() by accident. A missing
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
        from . import bridge_launcher

        _run_bridge(
            quiet_duplicate=bridge_launcher.SETTINGS_LAUNCH_FLAG in args
        )
        return

    # Default (no arguments) and explicit --settings both open the settings
    # window. A user who double-clicks the packaged exe must see a window,
    # never a headless bridge process with no UI.
    _run_settings()


def _run_settings(
    *, start_hidden: bool = False, activate_duplicate: bool = True
) -> None:
    from . import settings_ui, single_instance

    try:
        with single_instance.SettingsInstanceGuard():
            if start_hidden:
                settings_ui.main(start_hidden=True)
            else:
                settings_ui.main()
    except single_instance.DuplicateInstanceError:
        if activate_duplicate:
            single_instance.activate_existing_settings_window()
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
