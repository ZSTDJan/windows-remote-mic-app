"""PySide6-Essentials + Qt Quick/QML settings window (XRBM-030/XRBM-031),
replacing the previous Tk view. Every validation/save/launch/log-status
decision below still goes straight through the same pure functions in
``settings_ui.py`` - this module only bridges them to QML via a
`QAbstractListModel` (``ButtonMappingModel``) and two `QObject`s
(``SettingsController``, ``DiagnosticsController``); it never duplicates
that logic.

This module deliberately does NOT import PySide6 at module import time.
``_load_qt_classes()`` performs that import lazily (and caches the result),
so merely importing ``ovb_rc003.qt_settings_app`` - e.g. transitively via
``settings_ui`` in a ``--dry-run`` smoke check, or from a pure test that
only needs ``remote_layout``/``shell_targets`` - never requires PySide6 to
be installed. ``run_settings_window()`` is the only real entry point, and
raises ``QtUnavailableError`` with an actionable message if PySide6-
Essentials is missing (source/dev runs only: the frozen build always bundles
the Qt runtime itself - see build/RemoteMicRC003.spec - so end users
never need to separately install Python or Qt).

``DiagnosticsController`` (XRBM-031's "检查与修复" fourth page) runs every
``windows_diagnostics.run_diagnostics()`` check on a plain background
``threading.Thread`` - never on the Qt GUI thread, so a slow WinRT/PortAudio
call can never freeze the window - and delivers the result back via a
cross-thread Qt signal (``Signal(object)``), which Qt automatically queues
onto the GUI thread because the receiving ``QObject`` was constructed there;
this is the standard, documented way to marshal a background-thread result
back to a Qt object living on a different thread, and needs no ``QThread``
subclass or extra locking. An ``_is_refreshing`` guard refuses to start a
second worker while one is already running (repeated "重新检测" clicks never
overlap).

Thread lifecycle at window close / process exit (XRBM-035, hardened again in
RETRY 1 - a real Windows CI crash, not merely a theoretical race,
superseded the previous "best-effort atexit join + daemon=True is good
enough" contract described here before): every background thread this
controller starts is tracked in ``_diagnostics_threads``, and
``_shutdown_diagnostics_workers()`` signals shutdown and makes a BOUNDED
join attempt on each (``_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS`` per
thread - DERIVED from ``windows_diagnostics.BLE_DISCOVERY_MAX_CANCELLATION_
SECONDS`` plus a safety margin, not an independently-guessed value - see
that constant's own definition below). The critical fix is WHEN this runs:
``run_settings_window()`` now calls it EXPLICITLY, synchronously, from a
``try/finally`` that starts right after ``DiagnosticsController`` is
constructed (i.e. right after its background worker could first exist) and
covers every exit path through ``app.exec()`` returning, ``engine.load()``
raising, or ``rootObjects()`` coming back empty - while every Qt/Python
object it built is still fully alive - not only via this module's
``atexit`` hook (``_shutdown_qt_settings_app_at_exit()``, still registered
as a defense-in-depth safety net for callers that bypass
``run_settings_window()``). A real Windows CI faulthandler dump proved the
old atexit-only timing insufficient: a background BLE-discovery worker was
still deep inside a native WinRT await when the interpreter itself began
finalizing, producing an ``0xC0000005`` access violation - daemon=True only
guarantees CPython does not block exit on a surviving thread, it says
nothing about whether that thread's native call can safely keep running
concurrently with interpreter teardown.

RETRY 1's independent review found the round-1 fix for the discovery side
itself - cancelling the asyncio Task awaiting ``discover_candidates()`` -
was ALSO insufficient: the locked pywinrt wrapper's own post-cancel wait is
itself unbounded (see ``windows_diagnostics.py``'s "-- BLE candidate --"
section for the exact source citation), so an in-process asyncio
cancellation request could never give a real hard bound either. BLE
candidate discovery therefore now runs in a genuinely separate, disposable
OS PROCESS (``windows_diagnostics._run_ble_diagnostics_subprocess()``) that
the parent can forcibly terminate/kill and CONFIRM dead within a real,
OS-enforced bound - the shutdown event doubles as that cancellation signal
(not just an emit-skip flag - see below), so a discovery attempt in flight
when shutdown begins is actually asked to stop and confirmed to have
stopped, giving the bounded join here a realistic chance to succeed instead
of only ever timing out. Every one of these threads is still created with
``daemon=True`` too, as a last-resort backstop if a future failure mode
ever defeats the process-level isolation above.

Shutdown-vs-teardown ordering (XRBM-031 RETRY 2, still true under XRBM-035):
a diagnostics worker still finishing around shutdown time must never emit
its result INTO a ``DiagnosticsController``/Qt runtime that
``_release_qt_classes_cache()`` may already have started tearing down. Two
things make this safe: ``_diagnostics_shutdown_event`` (a module-level
``threading.Event``) is set FIRST, before anything else, by
``_shutdown_diagnostics_workers()`` - ``refreshDiagnostics()`` refuses to
start a new worker once it is set, and a worker already running checks it
immediately before emitting and skips the emit entirely if it is set (any
exception the emit call raises anyway - the receiver could still be
mid-teardown despite the check - is caught and discarded, never crashing
the worker thread); and every worker's ``finally`` block unconditionally
calls ``_forget_diagnostics_thread()``, so the registry is cleaned up even
on that path. ``_shutdown_qt_settings_app_at_exit()`` still runs all three
shutdown steps (flag the shutdown, join outstanding workers, release the Qt
classes cache) in that explicit order, in one function - never relying on
Python's ``atexit`` LIFO-ordered execution of separately registered
functions (see that function's docstring for the full story).
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass
import gc
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import (
    __version__,
    audio_playback,
    audio_output,
    bridge_control_windows,
    bridge_launcher,
    bridge_runtime_status,
    config,
    device_catalog,
    frida_compat,
    hotkey,
    hotkey_capture_windows,
    key_detection_bridge,
    key_mapping,
    logging_setup,
    product_identity,
    remote_layout,
    raw_input_windows,
    resources,
    settings_ui,
    shell_targets,
    single_instance,
    startup_windows,
    vb_cable_bundle,
    voice_hotkey_sync_windows,
    voice_program_manager,
    win32_keys,
    window_chrome_windows,
    windows_diagnostics,
)

# Only the physical microphone button can own RC003 audio. Other buttons use
# ordinary actions; secondary gestures are ordinary actions for every button.
_ORDINARY_PRIMARY_ACTION_OPTIONS: List[str] = list(
    dict.fromkeys(
        settings_ui._PRESET_KEY_COMBOS
    )
)
_MIC_PRIMARY_ACTION_OPTIONS: List[str] = list(
    dict.fromkeys(
        settings_ui._PRIMARY_VOICE_DISPLAYS + tuple(_ORDINARY_PRIMARY_ACTION_OPTIONS)
    )
)
_SECONDARY_ACTION_OPTIONS: List[str] = list(
    dict.fromkeys(
        (settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,)
        + tuple(
            option
            for option in settings_ui._PRESET_KEY_COMBOS
            if option != "禁用"
        )
    )
)

_COMBO_MODIFIER_LABELS = {
    "tv": "TV",
    "menu": "菜单",
    "home": "主页",
}


class QtUnavailableError(RuntimeError):
    """Raised by run_settings_window() when PySide6-Essentials is not
    importable. See module docstring.
    """


def _apply_application_icon(app, window, icon_type, icon_path: Path) -> None:
    """Applies one state icon to Qt's default and the loaded native window."""

    icon = icon_type(str(icon_path))
    set_application_icon = getattr(app, "setWindowIcon", None)
    if callable(set_application_icon):
        set_application_icon(icon)
    set_window_icon = getattr(window, "setIcon", None)
    if callable(set_window_icon):
        set_window_icon(icon)


def _apply_application_identity(app) -> None:
    """Applies the shared user-visible name to Qt's process identity."""

    set_application_name = getattr(app, "setApplicationName", None)
    if callable(set_application_name):
        set_application_name(product_identity.DISPLAY_NAME)


def _connect_application_exit(app, controller) -> None:
    """Make full exit a host responsibility instead of relying only on QML."""

    controller.applicationExitReady.connect(app.quit)


def _qml_directory() -> Path:
    """Locates the ``qml/`` directory this module's QML files live in,
    mirroring resources.py's frozen-vs-source-checkout lookup: in a frozen
    (PyInstaller) build, ``build/RemoteMicRC003.spec`` collects the
    qml sources under ``ovb_rc003_qml`` inside the COLLECT output, which the
    bootloader exposes via ``sys._MEIPASS`` at runtime (see resources.py's
    module docstring for why ``sys._MEIPASS`` - not the exe's own directory
    - is the correct base path for bundled, non-Python data in a one-dir
    build). In an unfrozen (source checkout or ``pip install``) run, the
    qml/ directory is simply this module's own sibling directory.
    """

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "ovb_rc003_qml"
    return Path(__file__).resolve().parent / "qml"


def _mark_settings_window_for_activation(window: object) -> bool:
    """Mark the real native QML window for duplicate-launch reactivation."""

    if sys.platform != "win32":
        return False
    try:
        hwnd = int(window.winId())  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return False
    return single_instance.mark_settings_window(hwnd)


_qt_classes_cache: Optional[dict] = None

# Every background diagnostics worker thread DiagnosticsController.
# refreshDiagnostics() starts is appended here (and discarded once it
# finishes - see _forget_diagnostics_thread()), purely so tests/diagnostics
# can observe how many are currently tracked. This is NOT what makes thread
# cleanup safe - see module docstring's "Thread lifecycle at process exit"
# section (XRBM-031 RETRY 1 item 6): _join_diagnostics_threads_at_exit()
# below only ever makes a bounded, best-effort join attempt; the actual
# safety property (process exit is never blocked indefinitely) comes from
# every one of these threads being created with daemon=True.
_diagnostics_threads: "list[threading.Thread]" = []
_diagnostics_threads_lock = threading.Lock()

# Set exactly once, by _begin_diagnostics_shutdown() (only ever called from
# _shutdown_qt_settings_app_at_exit() in production - see that function's
# docstring), BEFORE anything else happens at process exit (XRBM-031 RETRY
# 2). refreshDiagnostics() refuses to start a new worker once this is set;
# a worker already running checks it immediately before emitting its result
# and skips the emit entirely if it is set, so a diagnostics result can
# never be delivered into a DiagnosticsController/Qt runtime that may
# already be mid-teardown. Tests that set this directly (rather than via
# _shutdown_qt_settings_app_at_exit()) MUST .clear() it afterward - it is
# process-global, persistent state, not per-test.
_diagnostics_shutdown_event = threading.Event()

# Cross-controller gate for the explicit active audio test. It prevents the
# connection page from saving a different endpoint or launching the bridge
# while the synthetic signal is in flight.
_vb_cable_test_active_event = threading.Event()

# Tracks the multi-step "detect and save CABLE Input" workflow across the
# DiagnosticsController and SettingsController. Full application exit waits
# for this event to clear so a late enumeration/preflight result cannot race
# Qt teardown or persist settings after exit has begun.
_driver_action_active_event = threading.Event()

# Safety margin on top of the longest diagnostics-child cancellation bound
# below. It covers the worker thread's own small amount of Python cleanup
# after the BLE or active-audio subprocess has been confirmed stopped.
_DIAGNOSTICS_THREAD_JOIN_SAFETY_MARGIN_SECONDS = 2.0

# Per-thread bound for the best-effort atexit join below - a module-level
# constant (rather than a literal inline) specifically so a test can lower
# it and prove the join is genuinely bounded/non-hanging without waiting
# out the real default (see tests/test_qt_settings_app.py).
#
# XRBM-035 RETRY 1 P1 #2: derive this from the subprocess layer's own
# cancellation constants, rather than an independent flat guess. The max()
# keeps both BLE discovery and the explicit VB-CABLE child inside the same
# bounded shutdown contract if either implementation changes later.
_VB_CABLE_BRIDGE_RECOVERY_SECONDS = (
    bridge_control_windows.DEFAULT_EXIT_TIMEOUT_SECONDS
    + bridge_launcher.DEFAULT_GRACE_CHECKS
    * bridge_launcher.DEFAULT_POLL_INTERVAL_SECONDS
)
_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS = (
    max(
        windows_diagnostics.BLE_DISCOVERY_MAX_CANCELLATION_SECONDS,
        windows_diagnostics.VB_CABLE_LOOPBACK_MAX_CANCELLATION_SECONDS
        + _VB_CABLE_BRIDGE_RECOVERY_SECONDS,
        windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_MAX_CANCELLATION_SECONDS,
    )
    + _DIAGNOSTICS_THREAD_JOIN_SAFETY_MARGIN_SECONDS
)

# A cancelled input start can spend up to five seconds waiting for its native
# hook/window to become ready, then another five seconds stopping the Raw
# Input listener and HID tap. Endpoint preflight has its own process-level
# cancellation bound. Derive the shared join ceiling from the longer path and
# leave a small margin for Python cleanup before Qt objects are released.
_INPUT_WORKER_MAX_START_SECONDS = 5.0
_INPUT_WORKER_MAX_STOP_SECONDS = 5.0
_SETTINGS_BACKGROUND_JOIN_SAFETY_MARGIN_SECONDS = 2.0
_SETTINGS_BACKGROUND_JOIN_TIMEOUT_SECONDS = (
    max(
        _INPUT_WORKER_MAX_START_SECONDS + _INPUT_WORKER_MAX_STOP_SECONDS,
        windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_MAX_CANCELLATION_SECONDS,
    )
    + _SETTINGS_BACKGROUND_JOIN_SAFETY_MARGIN_SECONDS
)
_APPLICATION_EXIT_WAIT_SAFETY_MARGIN_SECONDS = 2.0
_APPLICATION_EXIT_WAIT_TIMEOUT_SECONDS = (
    _SETTINGS_BACKGROUND_JOIN_TIMEOUT_SECONDS
    + bridge_control_windows.DEFAULT_EXIT_TIMEOUT_SECONDS
    + _APPLICATION_EXIT_WAIT_SAFETY_MARGIN_SECONDS
)
_APPLICATION_EXIT_SAVE_WAIT_TIMEOUT_SECONDS = (
    windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_PROCESS_TIMEOUT_SECONDS
    + windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_MAX_CANCELLATION_SECONDS
    + _SETTINGS_BACKGROUND_JOIN_SAFETY_MARGIN_SECONDS
)
_APPLICATION_EXIT_POLL_INTERVAL_MS = 100
_BRIDGE_STATUS_STALE_AFTER_SECONDS = 20.0


@dataclass(frozen=True)
class _VbCableTestWorkflowResult:
    loopback_result: object = None
    bridge_was_running: bool = False
    stop_error: str = ""
    restart_result: object = None
    restart_skipped_for_exit: bool = False


@dataclass(frozen=True)
class _InputStartResult:
    kind: str
    ok: bool
    message: str = ""
    hotkey_capture: object = None
    bridge_request: object = None
    listener: object = None
    tap: object = None
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class _InputStopResult:
    kind: str
    ok: bool
    message: str = ""
    hotkey_capture: object = None
    bridge_request: object = None
    listener: object = None
    tap: object = None


class _AnyEvent:
    """Read-only event view that is set when any source event is set."""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


def _remember_diagnostics_thread(thread: "threading.Thread") -> None:
    with _diagnostics_threads_lock:
        _diagnostics_threads.append(thread)


def _forget_diagnostics_thread(thread: "threading.Thread") -> None:
    with _diagnostics_threads_lock:
        if thread in _diagnostics_threads:
            _diagnostics_threads.remove(thread)


def _begin_diagnostics_shutdown() -> None:
    """Flags that process shutdown has begun - see
    ``_diagnostics_shutdown_event``'s own comment above and
    ``_shutdown_qt_settings_app_at_exit()``'s docstring below for the full
    ordering contract this is one step of. Idempotent (``Event.set()`` is
    always safe to call more than once); kept as its own tiny function
    purely so a test can call/assert on this ONE step in isolation from the
    join/cache-release steps that follow it.
    """

    _diagnostics_shutdown_event.set()


def _join_diagnostics_threads_at_exit() -> None:
    """Best-effort courtesy only - see module docstring's "Thread lifecycle
    at process exit" section. Never treat this function returning as proof
    every thread it attempted to join has actually stopped. Deliberately
    does NOT itself touch ``_diagnostics_shutdown_event`` (that is
    ``_begin_diagnostics_shutdown()``'s job, called separately, first, by
    ``_shutdown_qt_settings_app_at_exit()``) - kept orthogonal so tests can
    still call this function alone (as XRBM-031 RETRY 1's tests already do)
    without it leaving global shutdown state behind for later tests.
    """

    with _diagnostics_threads_lock:
        threads = list(_diagnostics_threads)
    for thread in threads:
        thread.join(timeout=_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS)


def _release_qt_classes_cache() -> None:
    """Drops this module's cached dynamically-created
    ``QObject``/``QAbstractListModel`` subclasses and asks the cyclic
    garbage collector to reclaim them while the interpreter is still fully
    alive, rather than leaving that to CPython's own shutdown-time
    finalization pass. Only ever called from
    ``_shutdown_qt_settings_app_at_exit()`` below (see that function's
    docstring for why it must run LAST, after diagnostics shutdown/join).

    Root cause this works around: ``PySide6.QtCore.Property`` descriptors
    hold a reference cycle back to their owning class that shiboken's C
    extension type does not fully support ``tp_clear`` for - reproduced
    with a minimal, completely unrelated repro (a single trivial
    ``Property``-having ``QObject`` subclass, left referenced at module
    scope with no closures, no caching, nothing else from this project
    involved). ``gc.collect()`` called explicitly *before* shutdown resolves
    that same cycle cleanly; the identical cycle left for ``Py_FinalizeEx``'s
    own final collection pass instead prints
    ``ResourceWarning: gc: N uncollectable objects at shutdown`` - a
    substring this project's ``windows-rc003-ci.yml`` test-suite step
    explicitly greps for and fails the build on (see
    tests/test_qt_lifecycle_cleanup.py for the subprocess-based regression
    proving this exact command stays clean).

    A deliberate no-op whenever ``_load_qt_classes()`` was never actually
    called in this process (the cache is still ``None``), which covers
    every non-Qt test process and ``--dry-run``.
    """

    global _qt_classes_cache
    if _qt_classes_cache is None:
        return
    _qt_classes_cache = None
    gc.collect()


def _shutdown_diagnostics_workers() -> None:
    """The ONE production shutdown contract for in-flight diagnostics
    workers (XRBM-035): flag shutdown, THEN bounded-wait for every tracked
    worker thread - steps 1+2 of ``_shutdown_qt_settings_app_at_exit()``'s
    three steps, factored out into their own function so both
    ``run_settings_window()`` (called explicitly right after ``app.exec()``
    returns, while every Qt/Python object it built is still fully alive -
    see that function) and the real QML load probe
    (``tests/test_qt_settings_app.py``'s ``_QML_LOAD_PROBE_SCRIPT``, which
    reproduces a settings window closing before a real background BLE
    discovery finishes) call the EXACT SAME helper, instead of each
    reimplementing shutdown or relying solely on this module's ``atexit``
    hook.

    Why this matters (XRBM-034 REPLAN, XRBM-035 red evidence): a Windows CI
    run's faulthandler dump showed the real crash thread still deep inside
    ``ble_transport_winrt.discover_candidates()``'s WinRT
    ``find_all_async_aqs_filter`` await, called from a
    ``DiagnosticsController`` background worker, well after this module's
    own ``atexit``-only join had already returned (its 2-second best-effort
    bound elapsing without the thread actually stopping) - by the time the
    interpreter itself began finalizing, that native WinRT call was still
    running concurrently with CPython/shiboken teardown, producing the
    observed ``0xC0000005`` access violation. Calling this function
    EXPLICITLY, synchronously, at the natural point the window is closing
    (not only via ``atexit``, which can fire arbitrarily late relative to
    Qt/native object teardown) gives every in-flight BLE discovery a real,
    bounded chance to be forcibly terminated and CONFIRMED dead at the OS
    process level (see
    ``windows_diagnostics._run_ble_diagnostics_subprocess()``) before that
    teardown ever begins - not merely a best-effort join on a thread nothing
    ever asked to stop.

    Idempotent - ``_begin_diagnostics_shutdown()`` is (``Event.set()`` is
    always safe to call more than once) and ``_join_diagnostics_threads_at_
    exit()`` is (an empty/already-finished registry joins instantly) - safe
    to call once here and again later via ``_shutdown_qt_settings_app_at_
    exit()`` as a defense-in-depth safety net for any path that does not go
    through ``run_settings_window()``.
    """

    _begin_diagnostics_shutdown()
    _join_diagnostics_threads_at_exit()


def _shutdown_qt_settings_app_at_exit() -> None:
    """The ONLY function this module registers via ``atexit`` (XRBM-031
    RETRY 2). Runs every shutdown step in one explicit, hard-coded order:

    1.-2. ``_shutdown_diagnostics_workers()`` - flags shutdown, then makes a
       bounded join attempt on every tracked worker thread (see that
       function's own docstring for why this is also called explicitly by
       ``run_settings_window()``, not only reached here);
    3. ``_release_qt_classes_cache()`` - only now, after every worker has
       either finished or had its bounded join time out, release the
       cached Qt classes.

    Why this is ONE function instead of separately ``atexit.register()``-ing
    each step (which is what the original XRBM-031 submission and its RETRY
    1 fix both did): ``atexit`` runs its registered functions in LIFO order
    (last registered, first executed) - registering the join hook first and
    the cache-release hook second, as before, meant the cache release
    actually ran FIRST at real process exit, the reverse of the order this
    module's own docstring already claimed. A diagnostics worker finishing
    right around that window could then emit its result into a
    ``DiagnosticsController``/Qt runtime whose cached classes were already
    being torn down. Collapsing every step into one function and
    registering that ONE function removes the dependency on ``atexit``'s
    LIFO ordering (and the silent-breakage risk of some future edit
    reordering two separate ``atexit.register()`` calls) entirely - the
    order is just ordinary, explicit Python statement order here.

    This remains registered as a defense-in-depth safety net (e.g. for a
    caller that never reaches ``run_settings_window()``'s own explicit
    call) - production windows must never depend on ``atexit`` alone; see
    ``_shutdown_diagnostics_workers()``'s docstring for why.
    """

    _shutdown_diagnostics_workers()
    _release_qt_classes_cache()


atexit.register(_shutdown_qt_settings_app_at_exit)


def _load_qt_classes() -> dict:
    """Imports PySide6 and defines every QObject/QAbstractListModel
    subclass this module needs, INSIDE this function body - not at module
    level - so that importing ``qt_settings_app`` itself never requires
    PySide6 (see module docstring). Cached after the first successful call
    within a process; a missing-PySide6 failure is never cached, so a
    caller that installs PySide6 into the same running process (unlikely in
    practice, but exercised by tests) would see a subsequent call succeed.
    """

    global _qt_classes_cache
    if _qt_classes_cache is not None:
        return _qt_classes_cache

    try:
        from PySide6.QtCore import (
            Property,
            QAbstractListModel,
            QByteArray,
            QModelIndex,
            QObject,
            QTimer,
            Qt,
            QUrl,
            Signal,
            Slot,
        )
        from PySide6.QtGui import QGuiApplication, QIcon
        from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterSingletonInstance
        from PySide6.QtQuickControls2 import QQuickStyle
    except ImportError as exc:
        raise QtUnavailableError(
            "PySide6-Essentials 未安装，无法打开 Qt 设置界面。源码运行请先在本项目"
            "的虚拟环境中执行 `pip install -r requirements.txt`（已包含 "
            "PySide6-Essentials）；打包后的 RemoteMicRC003.exe 自带 Qt 运行"
            "时，不需要终端用户单独安装 Python 或 Qt。"
        ) from exc

    _DisplayRole = Qt.ItemDataRole.DisplayRole
    _UserRole = Qt.ItemDataRole.UserRole

    class ButtonMappingModel(QAbstractListModel):
        """One row per physical RC003 button (13 total, in
        remote_layout.BUTTON_ORDER - 12 ordinary HID buttons plus mic),
        exposing its identity, current mapping texts and shared selected
        state to the full-width QML mapping matrix. Hotspot roles remain for
        compatibility with older consumers of the model, but the current
        settings page uses the matrix as its sole RC003 mapping view.
        """

        ButtonIdRole = _UserRole + 1
        DisplayNameRole = _UserRole + 2
        HidUsageRole = _UserRole + 3
        ActionTextRole = _UserRole + 4
        DoubleClickTextRole = _UserRole + 5
        LongPressTextRole = _UserRole + 6
        IsMicRole = _UserRole + 7
        IsSelectedRole = _UserRole + 8
        XRole = _UserRole + 9
        YRole = _UserRole + 10
        WidthRole = _UserRole + 11
        HeightRole = _UserRole + 12
        IsVoiceRole = _UserRole + 13
        SingleNoteRole = _UserRole + 14
        DoubleNoteRole = _UserRole + 15
        LongNoteRole = _UserRole + 16

        # Emitted whenever a QML combo box edits a row's action text
        # (button_id, new display text) - SettingsController does not need
        # this directly (it reads the model back at save time via
        # to_display_map()), but it is kept for any future listener/test.
        actionEdited = Signal(str, str)
        mappingEdited = Signal()

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._button_ids: List[str] = list(remote_layout.BUTTON_ORDER)
            self._action_text: Dict[str, str] = {bid: "" for bid in self._button_ids}
            self._secondary_action_text: Dict[str, Dict[str, str]] = {
                bid: {
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: (
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY
                    ),
                    key_mapping.ButtonTrigger.LONG_PRESS.value: (
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY
                    ),
                }
                for bid in self._button_ids
            }
            self._display_notes: Dict[str, Dict[str, str]] = {
                bid: {
                    key_mapping.ButtonTrigger.SINGLE_CLICK.value: "",
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: "",
                    key_mapping.ButtonTrigger.LONG_PRESS.value: "",
                }
                for bid in self._button_ids
            }
            self._selected_button_id: str = "ok"

        def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008 - QML model convention
            if parent.isValid():
                return 0
            return len(self._button_ids)

        def roleNames(self):
            return {
                self.ButtonIdRole: QByteArray(b"buttonId"),
                self.DisplayNameRole: QByteArray(b"displayName"),
                self.HidUsageRole: QByteArray(b"hidUsage"),
                self.ActionTextRole: QByteArray(b"actionText"),
                self.DoubleClickTextRole: QByteArray(b"doubleClickText"),
                self.LongPressTextRole: QByteArray(b"longPressText"),
                self.IsMicRole: QByteArray(b"isMic"),
                self.IsSelectedRole: QByteArray(b"isSelected"),
                self.XRole: QByteArray(b"hotspotX"),
                self.YRole: QByteArray(b"hotspotY"),
                self.WidthRole: QByteArray(b"hotspotWidth"),
                self.HeightRole: QByteArray(b"hotspotHeight"),
                self.IsVoiceRole: QByteArray(b"isVoice"),
                self.SingleNoteRole: QByteArray(b"singleNote"),
                self.DoubleNoteRole: QByteArray(b"doubleNote"),
                self.LongNoteRole: QByteArray(b"longNote"),
            }

        def data(self, index, role: int = _DisplayRole):
            if not index.isValid() or not (0 <= index.row() < len(self._button_ids)):
                return None
            button_id = self._button_ids[index.row()]
            hotspot = remote_layout.hotspot_for(button_id)
            if role in (self.ButtonIdRole, _DisplayRole):
                return button_id
            if role == self.DisplayNameRole:
                return remote_layout.BUTTON_DISPLAY_NAMES[button_id]
            if role == self.HidUsageRole:
                return remote_layout.hid_usage_display(button_id)
            if role == self.ActionTextRole:
                return self._action_text[button_id]
            if role == self.DoubleClickTextRole:
                return self._secondary_action_text[button_id][
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value
                ]
            if role == self.LongPressTextRole:
                return self._secondary_action_text[button_id][
                    key_mapping.ButtonTrigger.LONG_PRESS.value
                ]
            if role == self.IsMicRole:
                return button_id == "mic"
            if role == self.IsSelectedRole:
                return button_id == self._selected_button_id
            if role == self.XRole:
                return hotspot.x if hotspot else 0.0
            if role == self.YRole:
                return hotspot.y if hotspot else 0.0
            if role == self.WidthRole:
                return hotspot.width if hotspot else 0.0
            if role == self.HeightRole:
                return hotspot.height if hotspot else 0.0
            if role == self.IsVoiceRole:
                return bool(hotspot and hotspot.is_voice)
            if role == self.SingleNoteRole:
                return self._display_notes[button_id][
                    key_mapping.ButtonTrigger.SINGLE_CLICK.value
                ]
            if role == self.DoubleNoteRole:
                return self._display_notes[button_id][
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value
                ]
            if role == self.LongNoteRole:
                return self._display_notes[button_id][
                    key_mapping.ButtonTrigger.LONG_PRESS.value
                ]
            return None

        def load_display_map(
            self,
            display_map: Dict[str, str],
            secondary_display_map: Optional[Dict[str, Dict[str, str]]] = None,
            display_note_map: Optional[Dict[str, Dict[str, str]]] = None,
        ) -> None:
            """Apply persisted values without rebuilding QML delegates.

            The row identity/order never changes. Emitting only the roles
            whose text changed keeps cards, photo hotspots, selected state,
            and connector canvases alive across a save/reload.
            """

            for row, button_id in enumerate(self._button_ids):
                changed_roles = []
                primary_text = display_map.get(button_id, "")
                if primary_text != self._action_text[button_id]:
                    self._action_text[button_id] = primary_text
                    changed_roles.append(self.ActionTextRole)
                trigger_map = (secondary_display_map or {}).get(button_id, {})
                next_secondary = {
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: trigger_map.get(
                        key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
                    ),
                    key_mapping.ButtonTrigger.LONG_PRESS.value: trigger_map.get(
                        key_mapping.ButtonTrigger.LONG_PRESS.value,
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
                    ),
                }
                current_secondary = self._secondary_action_text[button_id]
                if (
                    next_secondary[key_mapping.ButtonTrigger.DOUBLE_CLICK.value]
                    != current_secondary[key_mapping.ButtonTrigger.DOUBLE_CLICK.value]
                ):
                    changed_roles.append(self.DoubleClickTextRole)
                if (
                    next_secondary[key_mapping.ButtonTrigger.LONG_PRESS.value]
                    != current_secondary[key_mapping.ButtonTrigger.LONG_PRESS.value]
                ):
                    changed_roles.append(self.LongPressTextRole)
                self._secondary_action_text[button_id] = next_secondary
                raw_notes = (display_note_map or {}).get(button_id, {})
                next_notes = {
                    key_mapping.ButtonTrigger.SINGLE_CLICK.value: str(
                        raw_notes.get(
                            key_mapping.ButtonTrigger.SINGLE_CLICK.value, ""
                        )
                    ),
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: str(
                        raw_notes.get(
                            key_mapping.ButtonTrigger.DOUBLE_CLICK.value, ""
                        )
                    ),
                    key_mapping.ButtonTrigger.LONG_PRESS.value: str(
                        raw_notes.get(
                            key_mapping.ButtonTrigger.LONG_PRESS.value, ""
                        )
                    ),
                }
                current_notes = self._display_notes[button_id]
                note_roles = {
                    key_mapping.ButtonTrigger.SINGLE_CLICK.value: self.SingleNoteRole,
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: self.DoubleNoteRole,
                    key_mapping.ButtonTrigger.LONG_PRESS.value: self.LongNoteRole,
                }
                for trigger, role in note_roles.items():
                    if next_notes[trigger] != current_notes[trigger]:
                        changed_roles.append(role)
                self._display_notes[button_id] = next_notes
                if changed_roles:
                    model_index = self.index(row, 0)
                    self.dataChanged.emit(model_index, model_index, changed_roles)

        def to_display_map(self) -> Dict[str, str]:
            """Inverse of load_display_map() for build_save_model()."""

            return dict(self._action_text)

        def to_secondary_display_map(self) -> Dict[str, Dict[str, str]]:
            return {
                button_id: {
                    trigger: (
                        ""
                        if text == settings_ui.SECONDARY_UNCONFIGURED_DISPLAY
                        else text
                    )
                    for trigger, text in trigger_map.items()
                }
                for button_id, trigger_map in self._secondary_action_text.items()
            }

        def to_display_note_map(self) -> Dict[str, Dict[str, str]]:
            return {
                button_id: {
                    trigger: note.strip()
                    for trigger, note in trigger_map.items()
                    if note.strip()
                }
                for button_id, trigger_map in self._display_notes.items()
                if any(note.strip() for note in trigger_map.values())
            }

        def index_of(self, button_id: str) -> int:
            try:
                return self._button_ids.index(button_id)
            except ValueError:
                return -1

        @Slot(str, result=int)
        def indexOfButton(self, button_id: str) -> int:
            return self.index_of(button_id)

        @Slot(int, str)
        def setActionTextAt(self, row: int, text: str) -> None:
            if not (0 <= row < len(self._button_ids)):
                return
            button_id = self._button_ids[row]
            if text == self._action_text[button_id]:
                return
            self._action_text[button_id] = text
            model_index = self.index(row, 0)
            self.dataChanged.emit(model_index, model_index, [self.ActionTextRole])
            self.actionEdited.emit(button_id, text)
            self.mappingEdited.emit()

        @Slot(int, str, str)
        def setSecondaryActionTextAt(self, row: int, trigger: str, text: str) -> None:
            if not (0 <= row < len(self._button_ids)):
                return
            if trigger not in {
                key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
                key_mapping.ButtonTrigger.LONG_PRESS.value,
            }:
                return
            button_id = self._button_ids[row]
            if text == self._secondary_action_text[button_id][trigger]:
                return
            self._secondary_action_text[button_id][trigger] = text
            model_index = self.index(row, 0)
            role = (
                self.DoubleClickTextRole
                if trigger == key_mapping.ButtonTrigger.DOUBLE_CLICK.value
                else self.LongPressTextRole
            )
            self.dataChanged.emit(model_index, model_index, [role])
            self.mappingEdited.emit()

        @Slot(int, str, str)
        def setDisplayNoteAt(self, row: int, trigger: str, text: str) -> None:
            if not (0 <= row < len(self._button_ids)):
                return
            valid_roles = {
                key_mapping.ButtonTrigger.SINGLE_CLICK.value: self.SingleNoteRole,
                key_mapping.ButtonTrigger.DOUBLE_CLICK.value: self.DoubleNoteRole,
                key_mapping.ButtonTrigger.LONG_PRESS.value: self.LongNoteRole,
            }
            if trigger not in valid_roles:
                return
            button_id = self._button_ids[row]
            clean_text = str(text).strip()
            if clean_text == self._display_notes[button_id][trigger]:
                return
            self._display_notes[button_id][trigger] = clean_text
            model_index = self.index(row, 0)
            self.dataChanged.emit(
                model_index, model_index, [valid_roles[trigger]]
            )
            self.mappingEdited.emit()

        def set_selected_button(self, button_id: str) -> None:
            if button_id == self._selected_button_id or button_id not in self._action_text:
                return
            old_row = self.index_of(self._selected_button_id)
            self._selected_button_id = button_id
            new_row = self.index_of(button_id)
            for row in (old_row, new_row):
                if row >= 0:
                    model_index = self.index(row, 0)
                    self.dataChanged.emit(model_index, model_index, [self.IsSelectedRole])

        def selected_button_id(self) -> str:
            return self._selected_button_id

    class SettingsController(QObject):
        """QML-facing adapter over settings_ui.py's pure functions plus
        config.py/audio_output.py/bridge_launcher.py/logging_setup.py/
        shell_targets.py - every slot below is a thin wrapper that performs
        no validation or business logic of its own.
        """

        hotkeyTextChanged = Signal()
        holdVoiceHotkeyTextChanged = Signal()
        endpointOptionsChanged = Signal()
        recommendedEndpointIndexChanged = Signal()
        selectedEndpointIndexChanged = Signal()
        bridgeRunningChanged = Signal()
        bridgeConnectedChanged = Signal()
        bridgeLaunchPhaseChanged = Signal()
        bridgeLaunchElapsedSecondsChanged = Signal()
        bridgeRestartRecommendedChanged = Signal()
        desktopBehaviorChanged = Signal()
        trayStateChanged = Signal()
        applicationExitReady = Signal()
        applicationExitFailed = Signal(str)
        windowHideReady = Signal()
        windowHideFailed = Signal(str)
        launchStatusTextChanged = Signal()
        statusMessageChanged = Signal()
        errorMessageChanged = Signal()
        settingsDirtyChanged = Signal()
        settingsSaveBusyChanged = Signal()
        activePageIndexChanged = Signal()
        feedbackPageIndexChanged = Signal()
        selectedButtonIdChanged = Signal()
        comboModifierIndexChanged = Signal()
        comboRowsChanged = Signal()
        selectedDeviceIndexChanged = Signal()
        selectedDeviceChanged = Signal()
        selectedVoiceProgramIndexChanged = Signal()
        voiceProgramOptionsChanged = Signal()
        voiceProgramCustomPathChanged = Signal()
        voiceProgramLaunchOnBridgeStartChanged = Signal()
        voiceProgramLaunchElevatedChanged = Signal()
        voiceProgramSettingsDirtyChanged = Signal()
        voiceProgramStatusTextChanged = Signal()
        voiceProgramStatusCodeChanged = Signal()
        voiceProgramElevationStatusChanged = Signal()
        voiceHotkeyBusyChanged = Signal()
        endpointPreflightBusyChanged = Signal()
        keyDetectionActiveChanged = Signal()
        keyDetectionTextChanged = Signal()
        hotkeyCaptureActiveChanged = Signal()
        inputOperationChanged = Signal()
        _rawKeyDetected = Signal(str, str)
        _hidTapDetectionStatus = Signal(str, str)
        hotkeyCaptured = Signal(str)
        hotkeyCaptureError = Signal(str)
        inputCleanupReady = Signal()
        inputCleanupFailed = Signal(str)
        saveSettingsAndExitFinished = Signal(bool)
        _hotkeyCaptureResult = Signal(str)
        _endpointOptionsRefreshReady = Signal(object)
        _voiceProgramStatusRefreshReady = Signal(object)
        _voiceProgramOptionsRefreshReady = Signal(object)
        _voiceHotkeyTaskReady = Signal(object)
        _endpointPreflightReady = Signal(object)
        _inputOperationReady = Signal(object)
        _applicationExitStopReady = Signal(object)
        _bridgeRestartStopReady = Signal(object)

        _TRIGGER_MODE_ORDER = (key_mapping.VoiceTriggerMode.HOLD,)
        _DEVICE_ORDER = (device_catalog.RC003_ID,)
        _DEVICE_PAGE_INDEX = 0
        _BUTTONS_PAGE_INDEX = 1
        _VOICE_PAGE_INDEX = 2
        _DESKTOP_BEHAVIOR_PAGE_INDEX = _DEVICE_PAGE_INDEX
        _KEY_DETECTION_TIMEOUT_SECONDS = key_detection_bridge.STALE_AFTER_SECONDS
        _KEY_DETECTION_USAGE_TO_BUTTON = {
            usage: button_id
            for usage, button_id in frida_compat.TAP_USAGE_TO_BUTTON.items()
            if button_id in remote_layout.BUTTON_DISPLAY_NAMES
        }

        def __init__(
            self,
            model: "ButtonMappingModel",
            parent=None,
            *,
            start_hidden: bool = False,
            background_task_runner: Optional[
                Callable[[Callable[[], None], str], None]
            ] = None,
        ) -> None:
            super().__init__(parent)
            self._model = model
            self._background_task_runner = background_task_runner
            self._background_shutdown_event = threading.Event()
            self._background_threads: set[threading.Thread] = set()
            # Tests inject a same-thread runner, so emitting a result may
            # synchronously submit the next serialized hotkey step while this
            # guard is held. Production signals are queued across threads.
            self._background_threads_lock = threading.RLock()
            self._input_worker_result_lock = threading.Lock()
            self._input_worker_result = None
            self._config_root = config.config_root()
            self._config = config.load_config(config.config_path(self._config_root))
            self._start_hidden = bool(start_hidden)
            self._launch_bridge_on_app_start = bool(
                self._config.get("launch_bridge_on_app_start", False)
            )
            self._close_behavior = str(
                self._config.get(
                    "close_behavior", config.CLOSE_BEHAVIOR_HIDE_TO_TRAY
                )
            )
            startup_state = startup_windows.read_startup_state()
            self._launch_at_login = startup_state.enabled
            self._application_exit_requested = False
            self._application_exit_confirmed = False
            self._application_exit_intent = threading.Event()
            self._application_exit_deadline = 0.0
            self._application_exit_poll_scheduled = False
            self._application_exit_stop_running = False
            self._application_exit_waiting_for_save = False
            self._save_then_exit_requested = False
            self._applicationExitStopReady.connect(
                self._on_application_exit_stop_ready
            )
            self._bridgeRestartStopReady.connect(
                self._on_bridge_restart_stop_ready
            )
            self._window_hide_requested = False
            self._bindings = config.load_key_bindings(
                config.key_bindings_path(self._config_root)
            )
            self._removed_voice_bindings = config.normalize_voice_product_boundary(
                self._config,
                self._bindings,
            )
            self._trigger_mode_index = 0
            saved_voice_hotkeys = self._config.get("voice_hotkeys", {})
            self._voice_hotkeys = {
                mode: str(
                    saved_voice_hotkeys.get(
                        mode.value,
                        key_mapping.voice_hotkey_for_trigger_mode(mode),
                    )
                )
                for mode in self._TRIGGER_MODE_ORDER
            }
            self._voice_program_settings = (
                voice_program_manager.normalize_voice_program_settings(
                    self._config.get("voice_program")
                )
            )
            self._voice_program_settings_dirty = False
            self._voice_program_status_text = ""
            self._voice_program_status_code = "unknown"
            self._voice_program_elevation_status = "unknown"
            self._voice_program_options = voice_program_manager.provider_options()
            self._voice_program_status_refresh_running = False
            self._voice_program_status_refresh_pending = False
            self._voiceProgramStatusRefreshReady.connect(
                self._on_voice_program_status_refresh_ready
            )
            self._voice_program_options_refresh_running = False
            self._voice_program_options_refresh_pending = False
            self._voiceProgramOptionsRefreshReady.connect(
                self._on_voice_program_options_refresh_ready
            )
            self._voice_hotkey_busy = False
            self._voice_hotkey_task_token = 0
            self._voice_hotkey_task_completion = None
            self._voiceHotkeyTaskReady.connect(
                self._on_voice_hotkey_task_ready
            )
            self._endpoint_preflight_busy = False
            self._endpoint_preflight_token = 0
            self._endpoint_preflight_completion = None
            self._endpointPreflightReady.connect(
                self._on_endpoint_preflight_ready
            )
            try:
                self._bridge_running = single_instance.bridge_instance_running()
            except (
                single_instance.SingleInstanceUnavailableError,
                single_instance.MutexCleanupError,
            ):
                bridge_status_known = False
                self._bridge_running = False
                self._launch_status_text = settings_ui.LAUNCH_STATUS_UNKNOWN_TEXT
            else:
                bridge_status_known = True
                self._launch_status_text = (
                    settings_ui.LAUNCH_ALREADY_RUNNING_TEXT
                    if self._bridge_running
                    else settings_ui.LAUNCH_NOT_STARTED_TEXT
                )
            self._current_runtime_identity = (
                bridge_runtime_status.current_runtime_identity(__version__)
            )
            self._bridge_restart_recommended = False
            self._bridge_recovery_attempted = False
            self._bridge_recovery_running = False
            runtime_status = (
                bridge_runtime_status.read_status(self._config_root)
                if self._bridge_running
                else None
            )
            self._bridge_connected = bool(
                runtime_status is not None
                and runtime_status.state
                is bridge_runtime_status.BridgeConnectionState.CONNECTED
            )
            self._bridge_launch_phase = (
                "connected"
                if self._bridge_connected
                else "waiting"
                if self._bridge_running
                else "idle"
                if bridge_status_known
                else "unknown"
            )
            self._bridge_launch_started_at: Optional[float] = None
            self._bridge_launch_elapsed_seconds = 0
            self._pending_bridge_launch: Optional[
                bridge_launcher.PendingBridgeLaunch
            ] = None
            if self._bridge_connected:
                self._launch_status_text = (
                    f"服务运行中；{device_catalog.RC003_DISPLAY_NAME} 已连接"
                )
            elif self._bridge_running:
                self._launch_status_text = (
                    f"服务运行中；等待{device_catalog.RC003_DISPLAY_NAME} 连接"
                    if runtime_status is not None
                    else (
                        f"服务运行中；{device_catalog.RC003_DISPLAY_NAME} 状态未知，"
                        "正在检查"
                    )
                )
            if self._bridge_running and runtime_status is not None:
                self._launch_status_text = self._describe_runtime_status(
                    runtime_status
                )
                identity_match = bridge_runtime_status.runtime_identity_matches(
                    runtime_status,
                    self._current_runtime_identity,
                )
                self._bridge_restart_recommended = (
                    identity_match is not True
                    or bridge_runtime_status.input_channels_failed(runtime_status)
                )
            self._has_explicit_launch_result = False
            self._status_message = ""
            self._error_message = ""
            self._settings_dirty = bool(self._removed_voice_bindings)
            self._settings_revision = 0
            self._settings_save_busy = False
            self._active_page_index = self._DEVICE_PAGE_INDEX
            self._feedback_page_index = (
                self._BUTTONS_PAGE_INDEX
                if self._removed_voice_bindings
                else self._DEVICE_PAGE_INDEX
            )
            self._selected_button_id = "ok"
            self._combo_modifier_id = key_mapping.COMBO_MODIFIER_BUTTON_IDS[0]
            self._combo_action_text = {
                button_id: "" for button_id in key_mapping.COMBO_ACTION_BUTTON_IDS
            }
            self._combo_note_text = {
                button_id: "" for button_id in key_mapping.COMBO_ACTION_BUTTON_IDS
            }
            selected_device_id = device_catalog.normalize_device_id(
                self._config.get("selected_device_profile")
            )
            if selected_device_id not in self._DEVICE_ORDER:
                selected_device_id = device_catalog.RC003_ID
            self._selected_device_fallback_id = selected_device_id
            self._selected_device_index = self._DEVICE_ORDER.index(selected_device_id)
            self._key_detection_listener = None
            self._key_detection_tap = None
            self._key_detection_bridge_request = None
            self._key_detection_started_at = 0.0
            self._key_detection_tap_usages = set()
            self._key_detection_active = False
            self._key_detection_text = "尚未检测；点击“检测真实按键”，再按一次遥控器按键"
            self._rawKeyDetected.connect(self._on_raw_key_detected)
            self._hidTapDetectionStatus.connect(self._on_hid_tap_detection_status)
            self._hotkey_capture = None
            self._hotkeyCaptureResult.connect(self._on_hotkey_capture_result)
            self._input_operation_kind = ""
            self._input_operation_phase = "idle"
            self._input_operation_token = 0
            self._input_operation_cancel_event: Optional[threading.Event] = None
            self._input_cleanup_requested = False
            self._pending_key_detection_stop_message = ""
            self._inputOperationReady.connect(self._on_input_operation_ready)

            self._endpoint_options: List[str] = []
            self._endpoint_values: List[audio_output.AudioEndpoint] = []
            self._recommended_endpoint_index = -1
            self._selected_endpoint_index = -1
            self._endpoint_options_refresh_running = False
            self._endpoint_options_refresh_pending = False
            self._endpointOptionsRefreshReady.connect(
                self._on_endpoint_options_refresh_ready
            )
            self._load_bindings_into_model()
            self._model.mappingEdited.connect(self._mark_settings_dirty)
            self._model.set_selected_button(self._selected_button_id)
            self._request_endpoint_options_refresh()
            self._request_voice_program_status_refresh()
            if self._removed_voice_bindings:
                affected = "、".join(
                    remote_layout.BUTTON_DISPLAY_NAMES.get(button_id, button_id)
                    for button_id in sorted(self._removed_voice_bindings)
                )
                self._status_message = (
                    f"旧语音配置已停用（{affected}）。请重新选择动作并保存；"
                    "停用前不会执行该按钮的单击、双击或长按动作。"
                )

        # -- internal helpers -------------------------------------------------

        def _start_background_task(
            self, target: Callable[[], None], name: str
        ) -> None:
            runner = self._background_task_runner
            if runner is not None:
                if self._background_shutdown_event.is_set():
                    raise RuntimeError("设置窗口正在关闭")
                runner(target, name)
                return

            def run_tracked() -> None:
                try:
                    if not self._background_shutdown_event.is_set():
                        target()
                finally:
                    with self._background_threads_lock:
                        self._background_threads.discard(threading.current_thread())

            thread = threading.Thread(
                target=run_tracked,
                name=name,
                daemon=True,
            )
            with self._background_threads_lock:
                if self._background_shutdown_event.is_set():
                    raise RuntimeError("设置窗口正在关闭")
                self._background_threads.add(thread)
                try:
                    thread.start()
                except BaseException:
                    self._background_threads.discard(thread)
                    raise

        def _emit_background_result(self, signal, payload: object) -> bool:
            with self._background_threads_lock:
                if self._background_shutdown_event.is_set():
                    return False
                try:
                    signal.emit(payload)
                except RuntimeError:
                    return False
                return True

        def _record_input_worker_result(
            self, token: int, action: str, result: object
        ) -> None:
            with self._input_worker_result_lock:
                self._input_worker_result = (token, action, result)

        def _take_input_worker_result(self):
            with self._input_worker_result_lock:
                result = self._input_worker_result
                self._input_worker_result = None
                return result

        def _clear_input_worker_result(self, token: int, action: str) -> None:
            with self._input_worker_result_lock:
                current = self._input_worker_result
                if current is not None and current[:2] == (token, action):
                    self._input_worker_result = None

        def shutdownBackgroundTasks(self) -> None:
            """Stop accepting background results and bounded-wait for workers."""

            with self._background_threads_lock:
                self._background_shutdown_event.set()
                threads = list(self._background_threads)
            self._endpoint_options_refresh_pending = False
            self._voice_program_status_refresh_pending = False
            self._voice_program_options_refresh_pending = False
            self._voice_hotkey_task_token += 1
            self._voice_hotkey_task_completion = None
            self._voice_hotkey_busy = False
            self._endpoint_preflight_token += 1
            self._endpoint_preflight_completion = None

            deadline = time.monotonic() + _SETTINGS_BACKGROUND_JOIN_TIMEOUT_SECONDS
            current_thread = threading.current_thread()
            for thread in threads:
                if thread is current_thread:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(timeout=remaining)

        def shutdownForProcessExit(self) -> None:
            """Synchronously release input hooks before Qt objects disappear."""

            self._application_exit_intent.set()
            _driver_action_active_event.clear()
            cancel_event = self._input_operation_cancel_event
            if cancel_event is not None:
                cancel_event.set()
            try:
                self.shutdownBackgroundTasks()
            finally:
                worker_result = self._take_input_worker_result()
                if worker_result is not None:
                    _token, _action, result = worker_result
                    self._hotkey_capture = result.hotkey_capture
                    self._key_detection_bridge_request = result.bridge_request
                    self._key_detection_listener = result.listener
                    self._key_detection_tap = result.tap
                result = self._stop_input_resources(
                    self._input_operation_kind,
                    hotkey_capture=self._hotkey_capture,
                    bridge_request=self._key_detection_bridge_request,
                    listener=self._key_detection_listener,
                    tap=self._key_detection_tap,
                )
                self._hotkey_capture = result.hotkey_capture
                self._key_detection_bridge_request = result.bridge_request
                self._key_detection_listener = result.listener
                self._key_detection_tap = result.tap
                self._input_operation_cancel_event = None
                self._set_key_detection_active_state(False)
                self._set_input_operation_state("", "idle")

        def _endpoint_options_payload(self, config_snapshot: dict) -> dict:
            try:
                endpoints = audio_output.enumerate_output_endpoints()
                endpoints = sorted(
                    endpoints,
                    key=lambda endpoint: (
                        audio_output.output_host_api_rank(endpoint.host_api),
                        endpoint.name.casefold(),
                    ),
                )
                options = [settings_ui._endpoint_display(e) for e in endpoints]
            except audio_output.AudioOutputUnavailableError:
                endpoints = []
                options = []

            endpoint_values = list(endpoints)

            recommended_display = ""
            recommendation_candidates = [
                endpoint
                for endpoint in endpoints
                if audio_output.is_cable_input_endpoint(endpoint.name)
                and endpoint.host_api
                in ("Windows WASAPI", "Windows DirectSound")
            ]
            try:
                recommended_endpoint = audio_output.select_preferred_output_endpoint(
                    recommendation_candidates
                )
            except audio_output.AudioOutputUnavailableError:
                pass
            else:
                recommended_display = settings_ui._endpoint_display(
                    recommended_endpoint
                )

            saved_name = config_snapshot.get("output_endpoint_name", "")
            saved_host_api = config_snapshot.get("output_endpoint_host_api", "")
            saved_display = ""
            migrated_display = ""
            migration_message = ""
            if saved_name:
                saved_display = settings_ui._endpoint_display(
                    audio_output.AudioEndpoint(
                        name=saved_name,
                        host_api=saved_host_api,
                    )
                )
            if (
                saved_display
                and audio_output.is_supported_output_host_api(saved_host_api)
                and saved_display not in options
            ):
                # The previously-saved device is no longer enumerated (e.g.
                # unplugged) - still show it as a selectable-but-absent
                # option rather than silently discarding the user's saved
                # choice, matching build_save_model()/_parse_endpoint_display()
                # round-tripping whatever text is present at save time.
                options = [saved_display] + options
                endpoint_values = [
                    audio_output.AudioEndpoint(
                        name=saved_name,
                        host_api=saved_host_api,
                    )
                ] + endpoint_values

            if saved_name and not audio_output.is_supported_output_host_api(
                saved_host_api
            ):
                cable_matches = [
                    endpoint
                    for endpoint in endpoints
                    if audio_output.is_cable_input_endpoint(endpoint.name)
                ]
                try:
                    migrated_endpoint = audio_output.select_preferred_output_endpoint(
                        cable_matches
                    )
                except audio_output.AudioOutputUnavailableError:
                    pass
                else:
                    migrated_display = settings_ui._endpoint_display(
                        migrated_endpoint
                    )
                    migration_message = (
                        "旧的 Windows WDM-KS 语音端点不可用于当前播放方式；"
                        f"已为本次设置预选 {migrated_display}。点击保存后才会写入。"
                    )

            recommended_index = (
                options.index(recommended_display)
                if recommended_display in options
                else -1
            )
            selected_display = migrated_display or saved_display
            selected_index = (
                options.index(selected_display) if selected_display in options else -1
            )
            return {
                "options": options,
                "values": endpoint_values,
                "recommended_index": recommended_index,
                "selected_index": selected_index,
                "migration_message": migration_message,
            }

        def _apply_endpoint_options_payload(self, payload: dict) -> None:
            options = list(payload["options"])
            values = list(payload["values"])
            recommended_index = int(payload["recommended_index"])
            selected_index = int(payload["selected_index"])
            options_changed = options != self._endpoint_options
            recommended_changed = (
                recommended_index != self._recommended_endpoint_index
            )
            selected_changed = selected_index != self._selected_endpoint_index
            self._endpoint_options = options
            self._endpoint_values = values
            self._recommended_endpoint_index = recommended_index
            self._selected_endpoint_index = selected_index
            if options_changed:
                self.endpointOptionsChanged.emit()
            if recommended_changed:
                self.recommendedEndpointIndexChanged.emit()
            if selected_changed:
                self.selectedEndpointIndexChanged.emit()
            migration_message = str(payload.get("migration_message", ""))
            if migration_message:
                self._set_settings_dirty(True)
                self._set_status_message(
                    migration_message,
                    self._VOICE_PAGE_INDEX,
                )

        def _refresh_endpoint_options(self) -> None:
            self._apply_endpoint_options_payload(
                self._endpoint_options_payload(dict(self._config))
            )

        def _request_endpoint_options_refresh(self) -> None:
            if self._endpoint_options_refresh_running:
                self._endpoint_options_refresh_pending = True
                return
            config_snapshot = dict(self._config)
            self._endpoint_options_refresh_running = True

            def run() -> None:
                payload = self._endpoint_options_payload(config_snapshot)
                self._emit_background_result(
                    self._endpointOptionsRefreshReady,
                    (config_snapshot, payload),
                )

            try:
                self._start_background_task(run, "audio-endpoint-refresh")
            except Exception:
                self._endpoint_options_refresh_running = False

        def _on_endpoint_options_refresh_ready(self, result: object) -> None:
            if self._background_shutdown_event.is_set():
                return
            self._endpoint_options_refresh_running = False
            config_snapshot, payload = result
            tracked_keys = (
                "output_endpoint_name",
                "output_endpoint_host_api",
            )
            stale = any(
                config_snapshot.get(key) != self._config.get(key)
                for key in tracked_keys
            )
            if not stale:
                self._apply_endpoint_options_payload(payload)
            refresh_again = self._endpoint_options_refresh_pending or stale
            self._endpoint_options_refresh_pending = False
            if refresh_again:
                QTimer.singleShot(0, self._request_endpoint_options_refresh)

        def _load_bindings_into_model(self) -> None:
            bindings = self._bindings.get("bindings", {})
            display_map: Dict[str, str] = {}
            secondary_display_map: Dict[str, Dict[str, str]] = {}
            raw_display_notes = self._bindings.get("display_notes", {})
            display_note_map = (
                raw_display_notes if isinstance(raw_display_notes, dict) else {}
            )
            for button_id in remote_layout.BUTTON_ORDER:
                if button_id in self._removed_voice_bindings:
                    display_map[button_id] = settings_ui._REMOVED_VOICE_DISPLAY
                    secondary_display_map[button_id] = {}
                    continue
                action_dict = bindings.get(button_id)
                if action_dict is not None:
                    try:
                        action = key_mapping.ButtonAction.from_dict(action_dict)
                        display_map[button_id] = settings_ui._action_to_display(action)
                    except (KeyError, TypeError, ValueError):
                        display_map[button_id] = ""
                else:
                    display_map[button_id] = ""
                secondary_display_map[button_id] = {}
                raw_secondary = self._bindings.get("secondary_bindings", {}).get(
                    button_id, {}
                )
                if isinstance(raw_secondary, dict):
                    for trigger_name in (
                        key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
                        key_mapping.ButtonTrigger.LONG_PRESS.value,
                    ):
                        action_dict = raw_secondary.get(trigger_name)
                        if not isinstance(action_dict, dict):
                            continue
                        try:
                            action = key_mapping.ButtonAction.from_dict(action_dict)
                        except (KeyError, TypeError, ValueError):
                            continue
                        secondary_display_map[button_id][trigger_name] = (
                            settings_ui._action_to_display(action)
                        )
            self._model.load_display_map(
                display_map,
                secondary_display_map,
                display_note_map,
            )
            raw_combo = self._bindings.get("combo_bindings", {})
            if not isinstance(raw_combo, dict):
                raw_combo = {}
            modifier = raw_combo.get("modifier")
            if modifier not in key_mapping.COMBO_MODIFIER_BUTTON_IDS:
                modifier = key_mapping.COMBO_MODIFIER_BUTTON_IDS[0]
            self._combo_modifier_id = str(modifier)
            raw_combo_actions = raw_combo.get("bindings", {})
            raw_combo_notes = raw_combo.get("display_notes", {})
            if not isinstance(raw_combo_actions, dict):
                raw_combo_actions = {}
            if not isinstance(raw_combo_notes, dict):
                raw_combo_notes = {}
            for button_id in key_mapping.COMBO_ACTION_BUTTON_IDS:
                action_text = ""
                raw_action = raw_combo_actions.get(button_id)
                if isinstance(raw_action, dict):
                    try:
                        action = key_mapping.ButtonAction.from_dict(raw_action)
                    except (KeyError, TypeError, ValueError):
                        pass
                    else:
                        if (
                            action.kind != key_mapping.ActionKind.DISABLED
                            and not key_mapping.is_voice_action(action)
                        ):
                            action_text = settings_ui._action_to_display(action)
                self._combo_action_text[button_id] = action_text
                note = raw_combo_notes.get(button_id, "")
                self._combo_note_text[button_id] = (
                    note.strip() if isinstance(note, str) else ""
                )
            self.comboModifierIndexChanged.emit()
            self.comboRowsChanged.emit()

        def _reset_combo_mapping_display(self) -> None:
            self._combo_modifier_id = key_mapping.COMBO_MODIFIER_BUTTON_IDS[0]
            for button_id in key_mapping.COMBO_ACTION_BUTTON_IDS:
                self._combo_action_text[button_id] = ""
                self._combo_note_text[button_id] = ""
            self.comboModifierIndexChanged.emit()
            self.comboRowsChanged.emit()

        def _selected_device_id(self) -> str:
            if 0 <= self._selected_device_index < len(self._DEVICE_ORDER):
                return self._DEVICE_ORDER[self._selected_device_index]
            return self._selected_device_fallback_id

        def _set_launch_status(self, text: str) -> None:
            if text == self._launch_status_text:
                return
            self._launch_status_text = text
            self.launchStatusTextChanged.emit()

        def _set_bridge_running(self, value: bool) -> None:
            value = bool(value)
            if value == self._bridge_running:
                return
            self._bridge_running = value
            self.bridgeRunningChanged.emit()
            self.trayStateChanged.emit()

        def _set_bridge_connected(self, value: bool) -> None:
            value = bool(value)
            if value == self._bridge_connected:
                return
            self._bridge_connected = value
            self.bridgeConnectedChanged.emit()
            self.trayStateChanged.emit()

        def _set_bridge_restart_recommended(self, value: bool) -> None:
            value = bool(value)
            if value == self._bridge_restart_recommended:
                return
            self._bridge_restart_recommended = value
            self.bridgeRestartRecommendedChanged.emit()

        def _describe_runtime_status(
            self,
            status: bridge_runtime_status.BridgeRuntimeStatus,
        ) -> str:
            connected = (
                status.state
                is bridge_runtime_status.BridgeConnectionState.CONNECTED
            )
            connection_text = (
                f"{device_catalog.RC003_DISPLAY_NAME} 已连接"
                if connected
                else f"等待{device_catalog.RC003_DISPLAY_NAME}连接"
            )
            identity_match = bridge_runtime_status.runtime_identity_matches(
                status,
                self._current_runtime_identity,
            )
            if identity_match is True:
                identity_text = f"当前版本 {status.app_version}"
            elif identity_match is False:
                identity_text = f"其它版本 {status.app_version or '未知'}"
            else:
                identity_text = "旧版服务，来源未确认"

            ready_hid_states = {
                frida_compat.HidTapState.READY.value,
                frida_compat.HidTapState.ATTACHED_WAITING_IO.value,
                frida_compat.HidTapState.INJECTING.value,
            }
            raw_ready = status.raw_input_state == "ready"
            tap_ready = status.hid_tap_state in ready_hid_states
            if raw_ready and tap_ready:
                input_text = "两个按键通道正常"
            elif raw_ready or tap_ready:
                input_text = "按键通道可用"
            elif bridge_runtime_status.input_channels_failed(status):
                input_text = "两个按键通道异常"
            else:
                input_text = "按键通道正在检查"
            if status.last_button_at is not None:
                age = max(0.0, time.time() - status.last_button_at)
                if age <= 10.0:
                    input_text += f"，刚收到按键（{status.last_button_source or '来源未知'}）"
            voice_text = "语音进行中" if status.voice_active else "语音空闲"
            stale_text = (
                "；状态更新滞后"
                if time.time() - status.updated_at
                > _BRIDGE_STATUS_STALE_AFTER_SECONDS
                else ""
            )
            return (
                f"服务运行中；{connection_text}；{identity_text}；"
                f"{input_text}；{voice_text}{stale_text}"
            )

        def _update_bridge_restart_recommendation(
            self,
            status: Optional[bridge_runtime_status.BridgeRuntimeStatus],
            *,
            schedule_recovery: bool,
        ) -> None:
            recommended = False
            automatic = False
            if status is not None:
                identity_match = bridge_runtime_status.runtime_identity_matches(
                    status,
                    self._current_runtime_identity,
                )
                if identity_match is not True:
                    recommended = True
                    automatic = identity_match is False and not status.voice_active
                elif bridge_runtime_status.input_channels_failed(status):
                    recommended = True
                    automatic = not status.voice_active
                elif (
                    time.time() - status.updated_at
                    > _BRIDGE_STATUS_STALE_AFTER_SECONDS
                ):
                    recommended = True
            self._set_bridge_restart_recommended(recommended)
            if (
                automatic
                and schedule_recovery
                and not self._bridge_recovery_attempted
                and not self._bridge_recovery_running
                and not self._get_bridge_launch_busy()
                and not self._application_exit_requested
                and not self._application_exit_confirmed
            ):
                self._bridge_recovery_attempted = True
                QTimer.singleShot(0, self._start_automatic_bridge_recovery)

        def _persist_desktop_behavior(self, key: str, value: object) -> bool:
            next_config = dict(self._config)
            next_config[key] = value
            try:
                saved = config.save_config_and_load(
                    config.config_path(self._config_root), next_config
                )
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self._set_status_message("")
                self._set_error_message(
                    f"常规设置保存失败：{type(exc).__name__}",
                    self._DESKTOP_BEHAVIOR_PAGE_INDEX,
                )
                return False
            self._config = saved
            self._bump_settings_revision()
            self._set_error_message("")
            self._set_status_message(
                "启动与窗口设置已保存。",
                self._DESKTOP_BEHAVIOR_PAGE_INDEX,
            )
            return True

        def _tray_icon_state(self) -> str:
            if self._bridge_connected:
                return "connected"
            if self._bridge_running:
                return "waiting"
            return "off"

        def _tray_icon_source(self) -> str:
            path = resources.find_app_icon(self._tray_icon_state())
            return QUrl.fromLocalFile(str(path)).toString() if path else ""

        def _set_bridge_launch_phase(self, value: str) -> None:
            if value == self._bridge_launch_phase:
                return
            self._bridge_launch_phase = value
            self.bridgeLaunchPhaseChanged.emit()

        def _refresh_bridge_launch_elapsed(self) -> None:
            elapsed = (
                0
                if self._bridge_launch_started_at is None
                else max(0, int(time.monotonic() - self._bridge_launch_started_at))
            )
            if elapsed == self._bridge_launch_elapsed_seconds:
                return
            self._bridge_launch_elapsed_seconds = elapsed
            self.bridgeLaunchElapsedSecondsChanged.emit()

        def _sync_bridge_connection_status(self, running: bool) -> None:
            if self._get_bridge_launch_busy():
                return
            runtime_status = (
                bridge_runtime_status.read_status(self._config_root)
                if running
                else None
            )
            connected = bool(
                runtime_status is not None
                and runtime_status.state
                is bridge_runtime_status.BridgeConnectionState.CONNECTED
            )
            self._set_bridge_connected(connected)
            if running:
                self._update_bridge_restart_recommendation(
                    runtime_status,
                    schedule_recovery=True,
                )
                if runtime_status is not None:
                    self._set_bridge_launch_phase(
                        "connected" if connected else "waiting"
                    )
                    self._set_launch_status(
                        self._describe_runtime_status(runtime_status)
                    )
                    return
                if connected:
                    self._set_bridge_launch_phase("connected")
                    self._set_launch_status(
                        f"服务运行中；{device_catalog.RC003_DISPLAY_NAME} 已连接"
                    )
                else:
                    self._set_bridge_launch_phase("waiting")
                    if self._has_explicit_launch_result:
                        self._set_launch_status(
                            f"服务运行中；等待{device_catalog.RC003_DISPLAY_NAME} 连接，"
                            "首次可能约 1 分钟"
                        )
                    elif runtime_status is None:
                        self._set_launch_status(
                            f"服务运行中；{device_catalog.RC003_DISPLAY_NAME} 状态未知，"
                            "正在检查"
                        )
                    else:
                        self._set_launch_status(
                            f"服务运行中；等待{device_catalog.RC003_DISPLAY_NAME} 连接"
                        )
                return

            self._set_bridge_restart_recommended(False)

            previous_phase = self._bridge_launch_phase
            if (
                self._has_explicit_launch_result
                and previous_phase in {"waiting", "connected"}
            ):
                self._set_bridge_launch_phase("failed")
                self._set_launch_status("服务已退出；请查看 app.log")
            elif self._has_explicit_launch_result and previous_phase in {
                "failed",
                "unknown",
            }:
                return
            elif not self._has_explicit_launch_result:
                self._set_bridge_launch_phase("idle")
                self._set_launch_status(settings_ui.LAUNCH_NOT_STARTED_TEXT)

        def _refresh_bridge_status(self) -> bool | None:
            self._refresh_bridge_launch_elapsed()
            if _vb_cable_test_active_event.is_set():
                return self._bridge_running
            try:
                running = single_instance.bridge_instance_running()
            except (
                single_instance.SingleInstanceUnavailableError,
                single_instance.MutexCleanupError,
            ):
                self._set_bridge_running(False)
                self._set_bridge_connected(False)
                if (
                    self._has_explicit_launch_result
                    and self._bridge_launch_phase == "failed"
                ):
                    return None
                if not self._get_bridge_launch_busy():
                    self._set_bridge_launch_phase("unknown")
                    self._set_launch_status(settings_ui.LAUNCH_STATUS_UNKNOWN_TEXT)
                return None

            self._set_bridge_running(running)
            self._sync_bridge_connection_status(running)
            return running

        def _set_feedback_page_index(self, value: int) -> None:
            value = max(
                self._DEVICE_PAGE_INDEX,
                min(self._VOICE_PAGE_INDEX, int(value)),
            )
            if value == self._feedback_page_index:
                return
            self._feedback_page_index = value
            self.feedbackPageIndexChanged.emit()

        def _set_status_message(
            self,
            text: str,
            page_index: Optional[int] = None,
        ) -> None:
            if text:
                self._set_feedback_page_index(
                    self._active_page_index if page_index is None else page_index
                )
            self._status_message = text
            self.statusMessageChanged.emit()

        def _set_error_message(
            self,
            text: str,
            page_index: Optional[int] = None,
        ) -> None:
            if text:
                self._set_feedback_page_index(
                    self._active_page_index if page_index is None else page_index
                )
            self._error_message = text
            self.errorMessageChanged.emit()

        def _set_settings_dirty(self, value: bool) -> None:
            value = bool(value)
            if value == self._settings_dirty:
                return
            self._settings_dirty = value
            self.settingsDirtyChanged.emit()

        def _set_settings_save_busy(self, value: bool) -> None:
            value = bool(value)
            if value == self._settings_save_busy:
                return
            self._settings_save_busy = value
            self.settingsSaveBusyChanged.emit()

        def _bump_settings_revision(self) -> None:
            self._settings_revision += 1

        def _mark_settings_dirty(self) -> None:
            if self._status_message:
                self._set_status_message("")
            self._bump_settings_revision()
            self._set_settings_dirty(True)

        def _set_voice_program_settings_dirty(self, value: bool) -> None:
            value = bool(value)
            if value == self._voice_program_settings_dirty:
                return
            self._voice_program_settings_dirty = value
            self.voiceProgramSettingsDirtyChanged.emit()

        def _set_voice_hotkey_busy(self, value: bool) -> None:
            value = bool(value)
            if value == self._voice_hotkey_busy:
                return
            self._voice_hotkey_busy = value
            self.voiceHotkeyBusyChanged.emit()

        def _set_endpoint_preflight_busy(self, value: bool) -> None:
            value = bool(value)
            if value == self._endpoint_preflight_busy:
                return
            self._endpoint_preflight_busy = value
            self.endpointPreflightBusyChanged.emit()

        def _request_endpoint_preflight(
            self,
            endpoint_name: str,
            endpoint_host_api: str,
            completion: Callable[[bool, str], None],
        ) -> bool:
            if self._endpoint_preflight_busy:
                completion(False, "另一个输出端点正在检查，请稍后重试。")
                return False
            self._endpoint_preflight_token += 1
            token = self._endpoint_preflight_token
            self._endpoint_preflight_completion = completion
            self._set_endpoint_preflight_busy(True)

            def run() -> None:
                try:
                    windows_diagnostics.preflight_output_endpoint_isolated(
                        endpoint_name,
                        endpoint_host_api,
                        cancel_event=_AnyEvent(
                            self._background_shutdown_event,
                            self._application_exit_intent,
                        ),
                    )
                except audio_output.AudioOutputUnavailableError:
                    result = (False, "所选语音输出设备无法实际打开。")
                except Exception:  # noqa: BLE001 - keep UI text sanitized
                    result = (False, "输出端点检查失败，请稍后重试。")
                else:
                    result = (True, "")
                self._emit_background_result(
                    self._endpointPreflightReady,
                    (token, result),
                )

            try:
                self._start_background_task(run, "audio-endpoint-preflight")
            except Exception:
                self._endpoint_preflight_completion = None
                self._set_endpoint_preflight_busy(False)
                completion(False, "无法启动输出端点检查。")
                return False
            return True

        def _on_endpoint_preflight_ready(self, result: object) -> None:
            if self._background_shutdown_event.is_set():
                return
            token, payload = result
            if token != self._endpoint_preflight_token:
                return
            completion = self._endpoint_preflight_completion
            self._endpoint_preflight_completion = None
            self._set_endpoint_preflight_busy(False)
            if completion is not None:
                ok, message = payload
                completion(bool(ok), str(message))
            self._schedule_application_exit_poll()

        def _submit_voice_hotkey_step(
            self,
            callback: Callable[[], object],
            completion: Callable[[bool, object], None],
        ) -> None:
            """Run one serialized provider step and return through a Qt signal."""

            self._voice_hotkey_task_token += 1
            token = self._voice_hotkey_task_token
            self._voice_hotkey_task_completion = completion

            def run() -> None:
                try:
                    payload = (True, callback())
                except BaseException as exc:  # noqa: BLE001 - marshal to GUI thread
                    payload = (False, exc)
                self._emit_background_result(
                    self._voiceHotkeyTaskReady,
                    (token, payload),
                )

            try:
                self._start_background_task(run, "voice-hotkey-provider")
            except BaseException as exc:  # noqa: BLE001 - report start failure
                self._voice_hotkey_task_completion = None
                completion(False, exc)

        def _on_voice_hotkey_task_ready(self, result: object) -> None:
            if self._background_shutdown_event.is_set():
                return
            token, payload = result
            if token != self._voice_hotkey_task_token:
                return
            completion = self._voice_hotkey_task_completion
            self._voice_hotkey_task_completion = None
            if completion is None:
                return
            ok, value = payload
            completion(bool(ok), value)

        def _finish_voice_hotkey_operation(self) -> None:
            self._voice_hotkey_task_completion = None
            self._set_voice_hotkey_busy(False)
            self._schedule_application_exit_poll()

        def _set_key_detection_text(self, text: str) -> None:
            if text != self._key_detection_text:
                self._key_detection_text = text
                self.keyDetectionTextChanged.emit()

        def _get_hotkey_capture_active(self) -> bool:
            return self._hotkey_capture is not None or (
                self._input_operation_kind == "hotkey"
                and self._input_operation_phase != "idle"
            )

        def _get_input_capture_in_use(self) -> bool:
            return bool(
                self._input_operation_phase != "idle"
                or self._hotkey_capture is not None
                or self._key_detection_bridge_request is not None
                or self._key_detection_listener is not None
                or self._key_detection_tap is not None
            )

        def _set_key_detection_active_state(self, value: bool) -> None:
            value = bool(value)
            if value == self._key_detection_active:
                return
            self._key_detection_active = value
            self.keyDetectionActiveChanged.emit()

        def _set_input_operation_state(self, kind: str, phase: str) -> None:
            previous_hotkey_active = self._get_hotkey_capture_active()
            previous_in_use = self._get_input_capture_in_use()
            changed = (
                kind != self._input_operation_kind
                or phase != self._input_operation_phase
            )
            self._input_operation_kind = kind
            self._input_operation_phase = phase
            if changed or previous_in_use != self._get_input_capture_in_use():
                self.inputOperationChanged.emit()
            if previous_hotkey_active != self._get_hotkey_capture_active():
                self.hotkeyCaptureActiveChanged.emit()

        def _stop_input_resources(
            self,
            kind: str,
            *,
            hotkey_capture=None,
            bridge_request=None,
            listener=None,
            tap=None,
        ) -> _InputStopResult:
            errors: List[str] = []
            remaining_capture = hotkey_capture
            remaining_request = bridge_request
            remaining_listener = listener
            remaining_tap = tap

            if hotkey_capture is not None:
                try:
                    hotkey_capture.stop()
                except Exception as exc:  # noqa: BLE001 - returned to Qt
                    errors.append(f"停止真实键盘录制时出错：{exc}")
                else:
                    remaining_capture = None
            if bridge_request is not None:
                try:
                    key_detection_bridge.cancel_detection(bridge_request)
                except Exception as exc:  # noqa: BLE001 - returned to Qt
                    errors.append(f"停止后台按键检测时出错：{exc}")
                else:
                    remaining_request = None
            if listener is not None:
                try:
                    listener.stop()
                except Exception as exc:  # noqa: BLE001 - returned to Qt
                    errors.append(f"停止 Windows 按键通道时出错：{exc}")
                else:
                    remaining_listener = None
            if tap is not None:
                try:
                    tap.stop()
                except Exception as exc:  # noqa: BLE001 - returned to Qt
                    errors.append(f"停止补充按键通道时出错：{exc}")
                else:
                    remaining_tap = None
            return _InputStopResult(
                kind=kind,
                ok=not errors,
                message="；".join(errors),
                hotkey_capture=remaining_capture,
                bridge_request=remaining_request,
                listener=remaining_listener,
                tap=remaining_tap,
            )

        def _start_hotkey_capture_worker(
            self, cancel_event: threading.Event
        ) -> _InputStartResult:
            capture = hotkey_capture_windows.HotkeyCapture(
                lambda chord: self._hotkeyCaptureResult.emit(chord)
            )
            try:
                capture.start()
            except Exception as exc:  # noqa: BLE001 - returned to Qt
                if getattr(capture, "is_running", False):
                    return _InputStartResult(
                        "hotkey",
                        False,
                        f"无法启动真实键盘录制：{exc}",
                        hotkey_capture=capture,
                    )
                return _InputStartResult(
                    "hotkey",
                    False,
                    f"无法启动真实键盘录制：{exc}",
                )
            if cancel_event.is_set():
                stopped = self._stop_input_resources(
                    "hotkey", hotkey_capture=capture
                )
                return _InputStartResult(
                    "hotkey",
                    False,
                    stopped.message,
                    hotkey_capture=stopped.hotkey_capture,
                )
            return _InputStartResult(
                "hotkey", True, hotkey_capture=capture
            )

        def _start_key_detection_worker(
            self,
            cancel_event: threading.Event,
            *,
            bridge_running: bool,
            physical_bindings: dict,
        ) -> _InputStartResult:
            if bridge_running:
                try:
                    request = key_detection_bridge.request_detection(
                        self._config_root
                    )
                except OSError as exc:
                    return _InputStartResult(
                        "key_detection",
                        False,
                        f"无法向后台桥接启动真实按键检测：{exc}",
                    )
                if cancel_event.is_set():
                    stopped = self._stop_input_resources(
                        "key_detection", bridge_request=request
                    )
                    return _InputStartResult(
                        "key_detection",
                        False,
                        stopped.message,
                        bridge_request=stopped.bridge_request,
                    )
                return _InputStartResult(
                    "key_detection", True, bridge_request=request
                )

            listener = None
            tap = None
            failures: List[str] = []
            try:
                paths = raw_input_windows.enumerate_matching_device_paths()
                device_path = (
                    raw_input_windows.hid_identity.select_single_device_path(paths)
                )
                listener = raw_input_windows.RawInputButtonListener(
                    lambda *_: None,
                    self._on_raw_input_event,
                )
                set_physical_bindings = getattr(
                    listener, "set_physical_bindings", None
                )
                if callable(set_physical_bindings):
                    set_physical_bindings(physical_bindings)
                listener.start(device_path)
            except Exception:  # noqa: BLE001 - keep user text stable
                failures.append("Windows 按键通道启动失败")
                if listener is not None:
                    stopped = self._stop_input_resources(
                        "key_detection", listener=listener
                    )
                    listener = stopped.listener
                    if listener is not None:
                        return _InputStartResult(
                            "key_detection",
                            False,
                            stopped.message,
                            listener=listener,
                        )

            tap = frida_compat.RC003HidReportTap(
                self._on_key_detection_hid_report,
                status_handler=self._on_key_detection_tap_status,
            )
            try:
                if not tap.start():
                    failures.append("补充按键通道启动失败")
                    tap = None
            except Exception:  # noqa: BLE001 - keep user text stable
                failures.append("补充按键通道启动失败")
                stopped = self._stop_input_resources(
                    "key_detection", listener=listener, tap=tap
                )
                if stopped.listener is not None or stopped.tap is not None:
                    return _InputStartResult(
                        "key_detection",
                        False,
                        stopped.message,
                        listener=stopped.listener,
                        tap=stopped.tap,
                    )
                listener = None
                tap = None

            if cancel_event.is_set():
                stopped = self._stop_input_resources(
                    "key_detection", listener=listener, tap=tap
                )
                return _InputStartResult(
                    "key_detection",
                    False,
                    stopped.message,
                    listener=stopped.listener,
                    tap=stopped.tap,
                )
            if listener is None and tap is None:
                return _InputStartResult(
                    "key_detection",
                    False,
                    "无法启动真实按键检测：" + "；".join(failures),
                    failures=tuple(failures),
                )
            return _InputStartResult(
                "key_detection",
                True,
                listener=listener,
                tap=tap,
                failures=tuple(failures),
            )

        def _begin_input_operation_start(
            self,
            kind: str,
            worker: Callable[[threading.Event], _InputStartResult],
        ) -> bool:
            if self._application_exit_requested or self._window_hide_requested:
                return False
            if self._get_input_capture_in_use():
                message = "另一项按键输入操作正在进行，请先结束。"
                if kind == "hotkey":
                    self.hotkeyCaptureError.emit(message)
                else:
                    self._set_key_detection_text(message)
                return False
            self._input_operation_token += 1
            token = self._input_operation_token
            self._take_input_worker_result()
            cancel_event = threading.Event()
            self._input_operation_cancel_event = cancel_event
            self._set_input_operation_state(kind, "starting")

            def run() -> None:
                try:
                    result = worker(cancel_event)
                except Exception as exc:  # noqa: BLE001 - marshal to Qt
                    result = _InputStartResult(
                        kind,
                        False,
                        f"按键输入操作启动失败：{type(exc).__name__}",
                    )
                self._record_input_worker_result(token, "start", result)
                self._emit_background_result(
                    self._inputOperationReady,
                    (token, "start", result),
                )

            try:
                self._start_background_task(run, f"{kind}-start")
            except Exception as exc:
                self._input_operation_cancel_event = None
                self._set_input_operation_state("", "idle")
                message = f"无法启动按键输入后台任务：{exc}"
                if kind == "hotkey":
                    self.hotkeyCaptureError.emit(message)
                else:
                    self._set_key_detection_text(message)
                return False
            return True

        def _request_input_stop(
            self,
            *,
            kind: str = "",
            key_detection_success_message: str = "",
        ) -> bool:
            if key_detection_success_message:
                self._pending_key_detection_stop_message = (
                    key_detection_success_message
                )
            if self._input_operation_phase == "starting":
                if kind and kind != self._input_operation_kind:
                    return False
                cancel_event = self._input_operation_cancel_event
                if cancel_event is not None:
                    cancel_event.set()
                return True
            if self._input_operation_phase == "stopping":
                return not kind or kind == self._input_operation_kind

            active_kind = self._input_operation_kind
            if not active_kind:
                if self._hotkey_capture is not None:
                    active_kind = "hotkey"
                elif (
                    self._key_detection_bridge_request is not None
                    or self._key_detection_listener is not None
                    or self._key_detection_tap is not None
                ):
                    active_kind = "key_detection"
            if kind and active_kind and kind != active_kind:
                return False
            if not active_kind:
                self._set_key_detection_active_state(False)
                self._set_input_operation_state("", "idle")
                self._after_input_operation_change()
                return True

            self._input_operation_token += 1
            token = self._input_operation_token
            self._take_input_worker_result()
            self._set_input_operation_state(active_kind, "stopping")
            capture = self._hotkey_capture
            bridge_request = self._key_detection_bridge_request
            listener = self._key_detection_listener
            tap = self._key_detection_tap
            self._hotkey_capture = None
            self._key_detection_bridge_request = None
            self._key_detection_listener = None
            self._key_detection_tap = None

            def run() -> None:
                result = self._stop_input_resources(
                    active_kind,
                    hotkey_capture=capture,
                    bridge_request=bridge_request,
                    listener=listener,
                    tap=tap,
                )
                self._record_input_worker_result(token, "stop", result)
                self._emit_background_result(
                    self._inputOperationReady,
                    (token, "stop", result),
                )

            try:
                self._start_background_task(run, f"{active_kind}-stop")
            except Exception as exc:
                self._hotkey_capture = capture
                self._key_detection_bridge_request = bridge_request
                self._key_detection_listener = listener
                self._key_detection_tap = tap
                self._set_input_operation_state(active_kind, "active")
                message = f"无法启动按键输入停止任务：{exc}"
                if active_kind == "hotkey":
                    self.hotkeyCaptureError.emit(message)
                else:
                    self._set_key_detection_text(message)
                self._fail_pending_input_cleanup(message)
                return False
            return True

        def _on_input_operation_ready(self, payload: object) -> None:
            if self._background_shutdown_event.is_set():
                return
            token, action, result = payload
            if token != self._input_operation_token:
                return
            self._clear_input_worker_result(token, action)
            self._input_operation_cancel_event = None
            if action == "start":
                self._hotkey_capture = result.hotkey_capture
                self._key_detection_bridge_request = result.bridge_request
                self._key_detection_listener = result.listener
                self._key_detection_tap = result.tap
                has_resource = bool(
                    result.hotkey_capture is not None
                    or result.bridge_request is not None
                    or result.listener is not None
                    or result.tap is not None
                )
                if result.ok or has_resource:
                    self._set_input_operation_state(result.kind, "active")
                else:
                    self._set_input_operation_state("", "idle")
                if result.kind == "hotkey":
                    if result.message:
                        self.hotkeyCaptureError.emit(result.message)
                else:
                    self._set_key_detection_active_state(has_resource)
                    if result.ok:
                        self._key_detection_tap_usages.clear()
                        self._key_detection_started_at = time.monotonic()
                        if result.bridge_request is not None:
                            message = (
                                "后台服务等待按键；请按一次，首次连接可能约 1 分钟，"
                                "检测时不执行映射"
                            )
                        elif result.listener is not None and result.tap is not None:
                            message = (
                                "Windows 按键通道已启动；常规按键可立即检测，"
                                "返回键、音量键请等待补充通道连接（约 1 分钟）"
                            )
                        elif result.tap is not None:
                            message = (
                                "补充按键通道连接中；连接后请按要检测的按键"
                                "（约 1 分钟）"
                            )
                        else:
                            message = (
                                "只能检测 Windows 可识别按键；"
                                "返回键、音量键可能测不到"
                            )
                        limited = (
                            f"；受限：{'；'.join(result.failures)}"
                            if result.failures else ""
                        )
                        self._set_key_detection_text(
                            f"{message}；检测时不执行映射{limited}"
                        )
                    elif result.message:
                        self._set_key_detection_text(result.message)
                if (
                    self._window_hide_requested
                    or self._application_exit_requested
                    or self._input_cleanup_requested
                ) and self._get_input_capture_in_use():
                    self._request_input_stop()
                else:
                    self._after_input_operation_change()
                return

            self._hotkey_capture = result.hotkey_capture
            self._key_detection_bridge_request = result.bridge_request
            self._key_detection_listener = result.listener
            self._key_detection_tap = result.tap
            has_resource = bool(
                result.hotkey_capture is not None
                or result.bridge_request is not None
                or result.listener is not None
                or result.tap is not None
            )
            if has_resource:
                self._set_input_operation_state(result.kind, "active")
            else:
                self._set_input_operation_state("", "idle")
            if result.kind == "key_detection":
                self._set_key_detection_active_state(has_resource)
                if not has_resource and self._pending_key_detection_stop_message:
                    self._set_key_detection_text(
                        self._pending_key_detection_stop_message
                    )
                self._pending_key_detection_stop_message = ""
            if result.message:
                if result.kind == "hotkey":
                    self.hotkeyCaptureError.emit(result.message)
                else:
                    self._set_key_detection_text(result.message)
                self._fail_pending_input_cleanup(result.message)
                return
            self._after_input_operation_change()

        def _fail_pending_input_cleanup(self, message: str) -> None:
            if self._input_cleanup_requested:
                self._input_cleanup_requested = False
                self.inputCleanupFailed.emit(message)
            if self._window_hide_requested:
                self._window_hide_requested = False
                self.windowHideFailed.emit(message)
            if self._application_exit_requested:
                self._fail_application_exit(message)

        def _after_input_operation_change(self) -> None:
            if self._input_cleanup_requested and not self._get_input_capture_in_use():
                self._input_cleanup_requested = False
                self.inputCleanupReady.emit()
            if self._window_hide_requested and not self._get_input_capture_in_use():
                self._window_hide_requested = False
                self.windowHideReady.emit()
            self._schedule_application_exit_poll()

        @Slot()
        def prepareForWindowHide(self) -> None:
            if self._window_hide_requested:
                return
            if not self._get_input_capture_in_use():
                self.windowHideReady.emit()
                return
            self._window_hide_requested = True
            if not self._request_input_stop():
                self._window_hide_requested = False
                self.windowHideFailed.emit(
                    "无法停止正在进行的按键录入或检测。"
                )

        @Slot(result=bool)
        def stopInputCapture(self) -> bool:
            """Stop any active input operation before navigation or prompts."""

            if self._input_cleanup_requested:
                return True
            if not self._get_input_capture_in_use():
                self.inputCleanupReady.emit()
                return True
            self._input_cleanup_requested = True
            if self._request_input_stop():
                return True
            self._input_cleanup_requested = False
            self.inputCleanupFailed.emit(
                "无法停止正在进行的按键录入或检测。"
            )
            return False

        def _voice_program_status_payload(
            self, settings: dict
        ) -> tuple[str, str, str]:
            try:
                status = voice_program_manager.inspect_voice_program(
                    settings
                )
                text = voice_program_manager.status_text(status)
                code = status.code
                elevation_status = (
                    "elevated"
                    if status.running and status.elevated is True
                    else "standard"
                    if status.running and status.elevated is False
                    else "unknown"
                )
            except Exception:
                text = "无法读取语音程序状态"
                code = "unknown"
                elevation_status = "unknown"
            return text, code, elevation_status

        def _apply_voice_program_status_payload(
            self, payload: tuple[str, str, str]
        ) -> None:
            text, code, elevation_status = payload
            if elevation_status != self._voice_program_elevation_status:
                self._voice_program_elevation_status = elevation_status
                self.voiceProgramElevationStatusChanged.emit()
            if text != self._voice_program_status_text:
                self._voice_program_status_text = text
                self.voiceProgramStatusTextChanged.emit()
            if code != self._voice_program_status_code:
                self._voice_program_status_code = code
                self.voiceProgramStatusCodeChanged.emit()

        def _refresh_voice_program_status(self) -> None:
            self._apply_voice_program_status_payload(
                self._voice_program_status_payload(
                    dict(self._voice_program_settings)
                )
            )

        def _request_voice_program_status_refresh(self) -> None:
            if self._voice_program_status_refresh_running:
                self._voice_program_status_refresh_pending = True
                return

            settings_snapshot = dict(self._voice_program_settings)
            self._voice_program_status_refresh_running = True

            def run() -> None:
                payload = self._voice_program_status_payload(settings_snapshot)
                self._emit_background_result(
                    self._voiceProgramStatusRefreshReady,
                    (settings_snapshot, payload),
                )

            try:
                self._start_background_task(
                    run,
                    "voice-program-status-refresh",
                )
            except Exception:
                self._voice_program_status_refresh_running = False
                self._apply_voice_program_status_payload(
                    ("无法读取语音程序状态", "unknown", "unknown")
                )

        def _on_voice_program_status_refresh_ready(self, result: object) -> None:
            if self._background_shutdown_event.is_set():
                return
            self._voice_program_status_refresh_running = False
            settings_snapshot, payload = result
            stale = settings_snapshot != self._voice_program_settings
            if not stale:
                self._apply_voice_program_status_payload(payload)

            refresh_again = self._voice_program_status_refresh_pending or stale
            self._voice_program_status_refresh_pending = False
            if refresh_again:
                self._schedule_voice_program_status_refresh()

        def _schedule_voice_program_status_refresh(self) -> None:
            QTimer.singleShot(0, self._request_voice_program_status_refresh)

        def _voice_program_options_payload(self, settings_snapshot: dict) -> List[str]:
            options = voice_program_manager.provider_options()
            for provider_id in (
                voice_program_manager.VOICE_PROGRAM_SOGOU,
                voice_program_manager.VOICE_PROGRAM_WETYPE,
            ):
                candidate = dict(settings_snapshot)
                candidate["provider"] = provider_id
                try:
                    status = voice_program_manager.inspect_voice_program(candidate)
                except Exception:  # noqa: BLE001 - leave the stable name intact
                    continue
                if status.code != "not_found":
                    continue
                index = voice_program_manager.provider_index(provider_id)
                options[index] += "（未安装）"
            return options

        def _request_voice_program_options_refresh(self) -> None:
            if self._voice_program_options_refresh_running:
                self._voice_program_options_refresh_pending = True
                return
            settings_snapshot = dict(self._voice_program_settings)
            self._voice_program_options_refresh_running = True

            def run() -> None:
                payload = self._voice_program_options_payload(settings_snapshot)
                self._emit_background_result(
                    self._voiceProgramOptionsRefreshReady,
                    payload,
                )

            try:
                self._start_background_task(
                    run,
                    "voice-program-options-refresh",
                )
            except Exception:
                self._voice_program_options_refresh_running = False

        def _on_voice_program_options_refresh_ready(self, payload: object) -> None:
            if self._background_shutdown_event.is_set():
                return
            self._voice_program_options_refresh_running = False
            options = [str(item) for item in payload]
            if options != self._voice_program_options:
                self._voice_program_options = options
                self.voiceProgramOptionsChanged.emit()
            refresh_again = self._voice_program_options_refresh_pending
            self._voice_program_options_refresh_pending = False
            if refresh_again:
                QTimer.singleShot(0, self._request_voice_program_options_refresh)

        def _replace_voice_program_settings(self, raw: object) -> None:
            previous = dict(self._voice_program_settings)
            current = voice_program_manager.normalize_voice_program_settings(raw)
            self._voice_program_settings = current
            if current["provider"] != previous.get("provider"):
                self.selectedVoiceProgramIndexChanged.emit()
                # Do not display the previous provider's running/installed
                # state while the newly-selected provider is checked.
                self._apply_voice_program_status_payload(
                    ("", "unknown", "unknown")
                )
            if current["custom_executable"] != previous.get("custom_executable"):
                self.voiceProgramCustomPathChanged.emit()
            if current["launch_on_bridge_start"] != previous.get(
                "launch_on_bridge_start"
            ):
                self.voiceProgramLaunchOnBridgeStartChanged.emit()
            if current["launch_elevated"] != previous.get("launch_elevated"):
                self.voiceProgramLaunchElevatedChanged.emit()
            self._request_voice_program_status_refresh()

        def _voice_settings_write_block_reason(self) -> str:
            if _vb_cable_test_active_event.is_set():
                return "VB-CABLE 通道测试正在运行；测试结束后再修改语音设置。"
            if self._get_bridge_launch_busy():
                return "遥控器服务正在启动；完成后再修改语音设置。"
            if self._endpoint_preflight_busy or _driver_action_active_event.is_set():
                return "输出端点正在处理；完成后再修改语音设置。"
            return ""

        def _voice_settings_write_start_blocked(self) -> bool:
            return bool(
                self._voice_settings_write_block_reason()
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            )

        def _persist_voice_settings(self) -> bool:
            block_reason = self._voice_settings_write_block_reason()
            if block_reason:
                self._set_error_message(block_reason, self._VOICE_PAGE_INDEX)
                return False

            hotkey_text = self._voice_hotkeys[
                key_mapping.VoiceTriggerMode.HOLD
            ].strip()
            try:
                parsed_hotkey = hotkey.HotkeySpec.parse(hotkey_text)
                win32_keys.resolve_vk_codes(
                    tuple(parsed_hotkey.modifiers) + (parsed_hotkey.key,)
                )
            except (hotkey.HotkeyParseError, win32_keys.UnknownKeyTokenError) as exc:
                self._set_error_message(
                    f"语音按键无效：{exc}",
                    self._VOICE_PAGE_INDEX,
                )
                return False

            new_config = dict(self._config)
            new_config["voice_trigger_mode"] = (
                key_mapping.VoiceTriggerMode.HOLD.value
            )
            new_config.pop("voice_release_finish_tap_enabled", None)
            new_config["voice_program"] = dict(self._voice_program_settings)
            config.set_voice_hotkey_for_provider(
                new_config,
                self._voice_program_settings.get("provider"),
                hotkey_text,
            )
            config_path = config.config_path(self._config_root)
            try:
                saved_config = config.save_config_and_load(config_path, new_config)
            except Exception as exc:  # noqa: BLE001 - a Qt slot must not escape
                self._set_error_message(
                    f"语音设置保存失败：{exc}",
                    self._VOICE_PAGE_INDEX,
                )
                return False

            self._config = saved_config
            self._bump_settings_revision()
            saved_hotkey = str(
                saved_config.get("voice_hotkeys", {}).get("hold", "")
            )
            self._set_voice_hotkey_text(
                key_mapping.VoiceTriggerMode.HOLD, saved_hotkey
            )
            self._replace_voice_program_settings(saved_config.get("voice_program"))
            self._set_voice_program_settings_dirty(False)
            self._set_error_message("")
            self._set_status_message(
                "语音设置已自动保存；按键映射仍未保存。"
                if self._settings_dirty
                else "语音设置已自动保存。",
                self._VOICE_PAGE_INDEX,
            )
            return True

        def _update_and_persist_voice_hotkey(self, value: str) -> bool:
            mode = key_mapping.VoiceTriggerMode.HOLD
            previous = self._voice_hotkeys[mode]
            if self._voice_hotkey_busy or self._voice_settings_write_start_blocked():
                return False
            provider_id = str(self._voice_program_settings.get("provider", ""))
            if provider_id == voice_program_manager.VOICE_PROGRAM_WINDOWS_DICTATION:
                value = "win+h"
            self._set_voice_hotkey_busy(True)
            self._set_status_message("正在同步语音快捷键…", self._VOICE_PAGE_INDEX)
            self._set_error_message("")
            self._submit_voice_hotkey_step(
                lambda: voice_hotkey_sync_windows.sync_provider_hotkey(
                    provider_id,
                    value,
                ),
                lambda ok, payload: self._on_voice_hotkey_sync_ready(
                    provider_id,
                    value,
                    previous,
                    ok,
                    payload,
                ),
            )
            return True

        def _on_voice_hotkey_sync_ready(
            self,
            provider_id: str,
            value: str,
            previous: str,
            ok: bool,
            payload: object,
        ) -> None:
            mode = key_mapping.VoiceTriggerMode.HOLD
            if not ok:
                self._set_status_message("")
                self._set_error_message(
                    f"同步语音快捷键失败：{payload}",
                    self._VOICE_PAGE_INDEX,
                )
                self._finish_voice_hotkey_operation()
                return

            result = payload
            if not result.ok:
                self._set_status_message("")
                if result.hotkey and result.hotkey != previous:
                    self._set_voice_hotkey_text(mode, result.hotkey)
                    if self._persist_voice_settings():
                        self._set_status_message("")
                        self._set_error_message(
                            result.message
                            + f" {product_identity.DISPLAY_NAME}已按语音程序当前值更新。",
                            self._VOICE_PAGE_INDEX,
                        )
                    else:
                        adoption_error = self._error_message
                        self._set_voice_hotkey_text(mode, previous)
                        self._set_status_message("")
                        self._set_error_message(
                            result.message
                            + f"；{adoption_error}"
                            + f" 语音程序当前值未能保存到{product_identity.DISPLAY_NAME}，"
                            "两边仍不一致。",
                            self._VOICE_PAGE_INDEX,
                        )
                else:
                    self._set_error_message(result.message, self._VOICE_PAGE_INDEX)
                self._finish_voice_hotkey_operation()
                return

            self._set_voice_hotkey_text(mode, result.hotkey or value)
            if self._persist_voice_settings():
                self._set_status_message(
                    result.message
                    + ("；按键映射仍未保存。" if self._settings_dirty else ""),
                    self._VOICE_PAGE_INDEX,
                )
                self._finish_voice_hotkey_operation()
                return

            local_error = self._error_message
            self._set_voice_hotkey_text(mode, previous)
            if provider_id in {
                voice_program_manager.VOICE_PROGRAM_NONE,
                voice_program_manager.VOICE_PROGRAM_CUSTOM,
                voice_program_manager.VOICE_PROGRAM_WETYPE,
                voice_program_manager.VOICE_PROGRAM_WINDOWS_DICTATION,
            }:
                self._set_status_message("")
                self._set_error_message(local_error, self._VOICE_PAGE_INDEX)
                self._finish_voice_hotkey_operation()
                return

            self._submit_voice_hotkey_step(
                lambda: voice_hotkey_sync_windows.sync_provider_hotkey(
                    provider_id,
                    previous,
                ),
                lambda rollback_ok, rollback_payload: (
                    self._on_voice_hotkey_rollback_ready(
                        provider_id,
                        previous,
                        local_error,
                        rollback_ok,
                        rollback_payload,
                    )
                ),
            )

        def _on_voice_hotkey_rollback_ready(
            self,
            provider_id: str,
            previous: str,
            local_error: str,
            ok: bool,
            payload: object,
        ) -> None:
            if ok:
                rollback = payload
            else:
                rollback = voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                    provider_id,
                    False,
                    "rollback_failed",
                    message=f"恢复第三方快捷键失败：{payload}",
                )
            if rollback.ok:
                self._set_status_message("")
                self._set_error_message(
                    local_error + "；语音程序快捷键已恢复原值。",
                    self._VOICE_PAGE_INDEX,
                )
                self._finish_voice_hotkey_operation()
                return
            if rollback.hotkey:
                self._finish_failed_voice_hotkey_rollback(
                    previous,
                    local_error,
                    rollback,
                    rollback.hotkey,
                )
                return
            self._submit_voice_hotkey_step(
                lambda: voice_hotkey_sync_windows.read_provider_hotkey(provider_id),
                lambda read_ok, read_payload: self._on_voice_hotkey_readback_ready(
                    previous,
                    local_error,
                    rollback,
                    read_ok,
                    read_payload,
                ),
            )

        def _on_voice_hotkey_readback_ready(
            self,
            previous: str,
            local_error: str,
            rollback: object,
            ok: bool,
            payload: object,
        ) -> None:
            observed = payload.hotkey if ok and payload.ok else ""
            self._finish_failed_voice_hotkey_rollback(
                previous,
                local_error,
                rollback,
                observed,
            )

        def _finish_failed_voice_hotkey_rollback(
            self,
            previous: str,
            local_error: str,
            rollback: object,
            observed: str,
        ) -> None:
            mode = key_mapping.VoiceTriggerMode.HOLD
            if observed:
                self._set_voice_hotkey_text(mode, observed)
                if self._persist_voice_settings():
                    self._set_status_message("")
                    self._set_error_message(
                        local_error
                        + f"；语音程序未能恢复原值，{product_identity.DISPLAY_NAME}"
                        "已按其当前值更新。",
                        self._VOICE_PAGE_INDEX,
                    )
                    self._finish_voice_hotkey_operation()
                    return
                reconciliation_error = self._error_message
                self._set_voice_hotkey_text(mode, previous)
                self._set_status_message("")
                self._set_error_message(
                    local_error
                    + f"；第三方程序快捷键未能恢复：{rollback.message}"
                    + f"；{reconciliation_error}"
                    + f" 语音程序当前值未能保存到{product_identity.DISPLAY_NAME}，"
                    "两边仍不一致。",
                    self._VOICE_PAGE_INDEX,
                )
                self._finish_voice_hotkey_operation()
                return
            self._set_status_message("")
            self._set_error_message(
                local_error
                + f"；第三方程序快捷键也未能恢复：{rollback.message}"
                + "；无法确认语音程序当前值，两边可能不一致。",
                self._VOICE_PAGE_INDEX,
            )
            self._finish_voice_hotkey_operation()

        def _update_and_persist_voice_program(self, updated: dict) -> bool:
            previous = dict(self._voice_program_settings)
            previous_dirty = self._voice_program_settings_dirty
            self._replace_voice_program_settings(updated)
            self._set_voice_program_settings_dirty(True)
            if self._persist_voice_settings():
                return True
            self._replace_voice_program_settings(previous)
            self._set_voice_program_settings_dirty(previous_dirty)
            return False

        def _on_raw_input_event(self, event: raw_input_windows.RawInputEvent) -> None:
            if not event.is_pressed:
                return
            if event.source == "keyboard":
                vkey = "--" if event.vkey is None else f"0x{event.vkey:02X}"
                make_code = "--" if event.make_code is None else f"0x{event.make_code:02X}"
                flags = "--" if event.flags is None else f"0x{event.flags:04X}"
                details = (
                    f"Windows 按键事件：键值={vkey}，扫描码={make_code}，"
                    f"标志={flags}"
                )
            else:
                details = f"Windows 按键报告：{event.report.hex(' ')}"
            if event.usages:
                details += "，按键值=" + ",".join(
                    f"0x{usage:04X}" for usage in event.usages
                )
            if event.decode_error:
                details += "，报告未能完整识别"
            self._rawKeyDetected.emit(event.button_id or "", details)

        def _on_raw_key_detected(self, button_id: str, details: str) -> None:
            """Handle one physical press on the Qt GUI thread.

            Raw Input emits only a logical button id; this detector never
            executes the configured action. It stops after the first press,
            selects the corresponding row, and leaves the user in control of
            choosing/saving the Windows mapping.
            """

            if (
                not self._key_detection_active
                or self._input_operation_phase != "active"
            ):
                return
            self.stopKeyDetection()
            if button_id:
                self.selectButton(button_id)
                display_name = remote_layout.BUTTON_DISPLAY_NAMES.get(button_id, button_id)
                usage = remote_layout.hid_usage_display(button_id)
                result = f"已检测：{display_name}（{usage}）"
            else:
                result = "检测到未知按键"
            detail_text = details.strip().rstrip("。")
            if detail_text:
                result += f"；{detail_text}"
            result += "；可设置并保存映射" if button_id else "；请保留提示用于适配"
            self._set_key_detection_text(result)

        def _on_key_detection_hid_report(self, report_id: int, payload: bytes) -> None:
            if (
                report_id != 1
                or len(payload) != 6
                or not self._key_detection_active
                or self._input_operation_phase != "active"
            ):
                return
            active = {
                int.from_bytes(payload[index : index + 2], "little")
                for index in range(0, len(payload), 2)
            } & set(self._KEY_DETECTION_USAGE_TO_BUTTON)
            pressed = active - self._key_detection_tap_usages
            self._key_detection_tap_usages = set(active)
            if not pressed:
                return
            usage = sorted(pressed)[0]
            button_id = self._KEY_DETECTION_USAGE_TO_BUTTON[usage]
            self._rawKeyDetected.emit(
                button_id,
                f"补充按键报告：按键值=0x{usage:04X}",
            )

        def _on_key_detection_tap_status(self, status: str, detail: str) -> None:
            self._hidTapDetectionStatus.emit(status, detail)

        def _on_hid_tap_detection_status(self, status: str, detail: str) -> None:
            if (
                not self._key_detection_active
                or self._input_operation_phase != "active"
            ):
                return
            if status == frida_compat.HidTapState.ATTACHED_WAITING_IO.value:
                if self._key_detection_listener is not None:
                    waiting_text = (
                        "补充按键通道已连接；请按要检测的按键，常规按键也可继续检测"
                    )
                else:
                    waiting_text = "补充按键通道已连接；请按要检测的按键"
                self._set_key_detection_text(waiting_text)
            elif status == frida_compat.HidTapState.READY.value:
                if self._key_detection_listener is not None:
                    ready_text = (
                        "两条按键通道均已就绪；13 个已知按键均可检测，检测时不执行映射"
                    )
                else:
                    ready_text = "补充按键通道已就绪；请按要检测的按键，检测时不执行映射"
                self._set_key_detection_text(ready_text)
            elif status in {
                frida_compat.HidTapState.FAILED.value,
                frida_compat.HidTapState.UNHEALTHY.value,
            }:
                detail_text = {
                    "gadget_connection_closed": "连接已关闭",
                }.get(detail, "")
                suffix = f"（{detail_text}）" if detail_text else ""
                self._set_key_detection_text(
                    f"补充按键通道暂不可用{suffix}；返回键、音量键可能测不到，正在重连"
                )

        def _on_hotkey_capture_result(self, chord: str) -> None:
            """Forward a hook-thread result to QML on the GUI thread."""

            if (
                self._input_operation_kind != "hotkey"
                or self._input_operation_phase != "active"
                or self._hotkey_capture is None
                or self._input_cleanup_requested
                or self._window_hide_requested
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                return
            self.hotkeyCaptured.emit(chord)

        def _save(
            self,
            completion: Optional[Callable[[bool], None]] = None,
        ) -> bool:
            """Same validation as before (settings_ui.build_save_model);
            returns True only on an actual successful save, so
            saveAndLaunch() can gate the launch on it exactly like the
            previous Tk _save_and_launch() did.
            """

            def finish(result: bool) -> bool:
                if completion is not None:
                    completion(bool(result))
                if self._application_exit_waiting_for_save:
                    if result:
                        self._begin_application_exit()
                    else:
                        self._fail_application_exit(
                            "设置保存未完成，程序没有退出。"
                        )
                return bool(result)

            if self._settings_save_busy:
                self._set_error_message(
                    "按键映射正在保存，请等待完成。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(False)

            if self._voice_hotkey_busy:
                self._set_error_message(
                    "语音快捷键正在处理；完成后再保存设置。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(False)

            if _vb_cable_test_active_event.is_set():
                self._set_error_message(
                    "VB-CABLE 通道测试正在运行；测试结束后再保存设置。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(False)

            trigger_mode = key_mapping.VoiceTriggerMode.HOLD
            endpoint_display = (
                self._endpoint_options[self._selected_endpoint_index]
                if 0 <= self._selected_endpoint_index < len(self._endpoint_options)
                else ""
            )
            try:
                new_config, new_bindings = settings_ui.build_save_model(
                    button_display_map=self._model.to_display_map(),
                    secondary_display_map=self._model.to_secondary_display_map(),
                    display_note_map=self._model.to_display_note_map(),
                    hotkey_text=self._voice_hotkeys[trigger_mode],
                    trigger_mode=trigger_mode,
                    endpoint_display_text=endpoint_display,
                    base_config=self._config,
                    base_bindings=self._bindings,
                    selected_device_profile=self._selected_device_id(),
                    voice_hotkeys={
                        mode.value: self._voice_hotkeys[mode]
                        for mode in self._TRIGGER_MODE_ORDER
                    },
                    combo_modifier=self._combo_modifier_id,
                    combo_display_map=dict(self._combo_action_text),
                    combo_note_map=dict(self._combo_note_text),
                )
            except settings_ui.SettingsValidationError as exc:
                button_name = (
                    "话筒键"
                    if exc.button_id == "mic"
                    else remote_layout.BUTTON_DISPLAY_NAMES.get(
                        exc.button_id, exc.button_id
                    )
                )
                title = f"「{button_name}」映射无效" if button_name else "语音热键无效"
                self._set_error_message(
                    f"{title}：{exc.message}",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(False)

            new_config["voice_program"] = dict(self._voice_program_settings)
            config.set_voice_hotkey_for_provider(
                new_config,
                self._voice_program_settings.get("provider"),
                self._voice_hotkeys[trigger_mode],
            )

            endpoint_name = new_config.get("output_endpoint_name", "")
            endpoint_host_api = new_config.get("output_endpoint_host_api", "")
            save_revision = self._settings_revision

            def primary_voice_enabled(document: dict) -> bool:
                raw_bindings = document.get("bindings", {})
                if not isinstance(raw_bindings, dict):
                    return False
                for raw_action in raw_bindings.values():
                    if not isinstance(raw_action, dict):
                        continue
                    try:
                        action = key_mapping.ButtonAction.from_dict(raw_action)
                    except (KeyError, TypeError, ValueError):
                        continue
                    if key_mapping.is_voice_action(action):
                        return True
                return False

            primary_bindings = new_bindings.get("bindings", {})
            voice_mapping_enabled = primary_voice_enabled(
                {"bindings": primary_bindings}
            )
            previous_voice_mapping_enabled = primary_voice_enabled(self._bindings)
            endpoint_changed = (
                endpoint_name != self._config.get("output_endpoint_name", "")
                or endpoint_host_api
                != self._config.get("output_endpoint_host_api", "")
            )
            requires_preflight = bool(
                endpoint_name
                and voice_mapping_enabled
                and (endpoint_changed or not previous_voice_mapping_enabled)
            )

            def persist_candidate() -> bool:
                config_path = config.config_path(self._config_root)
                bindings_path = config.key_bindings_path(self._config_root)
                try:
                    config.save_settings_pair(
                        config_path,
                        new_config,
                        bindings_path,
                        new_bindings,
                    )
                    saved_config = config.load_config(config_path)
                    saved_bindings = config.load_key_bindings(bindings_path)
                except Exception as exc:  # noqa: BLE001 - a Qt slot must not escape
                    self._set_error_message(
                        f"保存失败：{exc}",
                        self._BUTTONS_PAGE_INDEX,
                    )
                    return finish(False)

                self._config = saved_config
                self._bindings = saved_bindings
                self._bump_settings_revision()
                self._replace_voice_program_settings(
                    saved_config.get("voice_program")
                )
                self._set_voice_program_settings_dirty(False)
                self._removed_voice_bindings = (
                    config.normalize_voice_product_boundary(
                        self._config,
                        self._bindings,
                    )
                )
                saved_voice_hotkeys = saved_config.get("voice_hotkeys", {})
                for mode in self._TRIGGER_MODE_ORDER:
                    saved_text = str(saved_voice_hotkeys.get(mode.value, ""))
                    self._set_voice_hotkey_text(mode, saved_text)
                self._load_bindings_into_model()
                self._set_settings_dirty(False)
                self._set_error_message("")
                self._set_status_message(
                    "已保存。按键映射和语音触发将在下一次按键时应用；"
                    "连接/输出设置需重启桥接。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return finish(True)

            if not requires_preflight:
                return persist_candidate()

            self._set_settings_save_busy(True)
            self._set_error_message("")
            self._set_status_message(
                "正在检查语音输出端点…",
                self._BUTTONS_PAGE_INDEX,
            )
            settled = False
            settled_result = False

            def after_preflight(ok: bool, message: str) -> None:
                nonlocal settled, settled_result
                self._set_settings_save_busy(False)
                if not ok:
                    self._set_status_message("")
                    self._set_error_message(
                        "保存失败："
                        + (message or "所选语音输出设备无法实际打开。"),
                        self._BUTTONS_PAGE_INDEX,
                    )
                    settled_result = finish(False)
                elif self._settings_revision != save_revision:
                    self._set_status_message("")
                    self._set_settings_dirty(True)
                    self._set_error_message(
                        "保存期间设置又发生了变化；为避免覆盖新修改，请重新保存。",
                        self._BUTTONS_PAGE_INDEX,
                    )
                    settled_result = finish(False)
                else:
                    settled_result = persist_candidate()
                settled = True
                self._schedule_application_exit_poll()

            accepted = self._request_endpoint_preflight(
                str(endpoint_name),
                str(endpoint_host_api),
                after_preflight,
            )
            return settled_result if settled else accepted

        # -- properties ---------------------------------------------------

        def _get_hotkey_text(self) -> str:
            mode = self._TRIGGER_MODE_ORDER[self._trigger_mode_index]
            return self._voice_hotkeys[mode]

        def _set_hotkey_text(self, value: str) -> None:
            mode = self._TRIGGER_MODE_ORDER[self._trigger_mode_index]
            if self._set_voice_hotkey_text(mode, value):
                self._mark_settings_dirty()

        hotkeyText = Property(str, _get_hotkey_text, _set_hotkey_text, notify=hotkeyTextChanged)

        def _set_voice_hotkey_text(
            self, mode: key_mapping.VoiceTriggerMode, value: str
        ) -> bool:
            if value == self._voice_hotkeys[mode]:
                return False
            self._voice_hotkeys[mode] = value
            self.holdVoiceHotkeyTextChanged.emit()
            self.hotkeyTextChanged.emit()
            return True

        def _get_hold_voice_hotkey_text(self) -> str:
            return self._voice_hotkeys[key_mapping.VoiceTriggerMode.HOLD]

        def _set_hold_voice_hotkey_text(self, value: str) -> None:
            self._update_and_persist_voice_hotkey(value)

        holdVoiceHotkeyText = Property(
            str,
            _get_hold_voice_hotkey_text,
            _set_hold_voice_hotkey_text,
            notify=holdVoiceHotkeyTextChanged,
        )

        def _get_voice_program_options(self) -> List[str]:
            return list(self._voice_program_options)

        voiceProgramOptions = Property(
            list,
            _get_voice_program_options,
            notify=voiceProgramOptionsChanged,
        )

        def _get_selected_voice_program_index(self) -> int:
            return voice_program_manager.provider_index(
                self._voice_program_settings.get("provider")
            )

        def _set_selected_voice_program_index(self, value: int) -> None:
            if self._voice_hotkey_busy or self._voice_settings_write_start_blocked():
                return
            provider_id = voice_program_manager.provider_id_for_index(value)
            if provider_id == self._voice_program_settings.get("provider"):
                self.refreshVoiceHotkeyFromProvider()
                return
            remembered_hotkey = config.voice_hotkey_for_provider(
                self._config, provider_id
            )
            previous_hotkey = self._get_hold_voice_hotkey_text()
            self._set_voice_hotkey_busy(True)
            self._set_status_message("正在读取语音程序快捷键…", self._VOICE_PAGE_INDEX)
            self._submit_voice_hotkey_step(
                lambda: voice_hotkey_sync_windows.read_provider_hotkey(
                    provider_id
                ),
                lambda ok, payload: self._on_selected_voice_program_hotkey_ready(
                    provider_id,
                    remembered_hotkey,
                    previous_hotkey,
                    ok,
                    payload,
                ),
            )

        def _on_selected_voice_program_hotkey_ready(
            self,
            provider_id: str,
            remembered_hotkey: str,
            previous_hotkey: str,
            ok: bool,
            payload: object,
        ) -> None:
            if ok:
                read_result = payload
            else:
                read_result = voice_hotkey_sync_windows.VoiceHotkeySyncResult(
                    provider_id,
                    False,
                    "read_failed",
                    message=f"读取语音程序快捷键失败：{payload}",
                )
            selected_hotkey = (
                read_result.hotkey if read_result.ok else remembered_hotkey
            )
            if provider_id == voice_program_manager.VOICE_PROGRAM_WINDOWS_DICTATION:
                selected_hotkey = "win+h"
            self._set_voice_hotkey_text(
                key_mapping.VoiceTriggerMode.HOLD,
                selected_hotkey,
            )
            updated = dict(self._voice_program_settings)
            updated["provider"] = provider_id
            updated["launch_on_bridge_start"] = (
                provider_id != voice_program_manager.VOICE_PROGRAM_NONE
                and not voice_program_manager.is_system_managed_provider(
                    provider_id
                )
            )
            if not self._update_and_persist_voice_program(updated):
                self._set_voice_hotkey_text(
                    key_mapping.VoiceTriggerMode.HOLD,
                    previous_hotkey,
                )
                self._finish_voice_hotkey_operation()
                return
            if read_result.ok:
                self._set_status_message(
                    read_result.message
                    + (
                        "；按键映射仍未保存。"
                        if self._settings_dirty
                        else ""
                    ),
                    self._VOICE_PAGE_INDEX,
                )
            elif read_result.code != "local_only":
                self._set_status_message("")
                self._set_error_message(
                    read_result.message
                    + " 已改用该程序上次保存的快捷键。",
                    self._VOICE_PAGE_INDEX,
                )
            self._finish_voice_hotkey_operation()

        selectedVoiceProgramIndex = Property(
            int,
            _get_selected_voice_program_index,
            _set_selected_voice_program_index,
            notify=selectedVoiceProgramIndexChanged,
        )

        def _selected_voice_program_is(self, provider_id: str) -> bool:
            return self._voice_program_settings.get("provider") == provider_id

        voiceProgramManaged = Property(
            bool,
            lambda self: not self._selected_voice_program_is(
                voice_program_manager.VOICE_PROGRAM_NONE
            ),
            notify=selectedVoiceProgramIndexChanged,
        )
        voiceProgramSogouSelected = Property(
            bool,
            lambda self: self._selected_voice_program_is(
                voice_program_manager.VOICE_PROGRAM_SOGOU
            ),
            notify=selectedVoiceProgramIndexChanged,
        )
        voiceProgramWeTypeSelected = Property(
            bool,
            lambda self: self._selected_voice_program_is(
                voice_program_manager.VOICE_PROGRAM_WETYPE
            ),
            notify=selectedVoiceProgramIndexChanged,
        )
        voiceProgramWindowsDictationSelected = Property(
            bool,
            lambda self: self._selected_voice_program_is(
                voice_program_manager.VOICE_PROGRAM_WINDOWS_DICTATION
            ),
            notify=selectedVoiceProgramIndexChanged,
        )
        voiceProgramCustomSelected = Property(
            bool,
            lambda self: self._selected_voice_program_is(
                voice_program_manager.VOICE_PROGRAM_CUSTOM
            ),
            notify=selectedVoiceProgramIndexChanged,
        )

        def _get_voice_program_system_managed(self) -> bool:
            return voice_program_manager.is_system_managed_provider(
                self._voice_program_settings.get("provider")
            )

        voiceProgramSystemManaged = Property(
            bool,
            _get_voice_program_system_managed,
            notify=selectedVoiceProgramIndexChanged,
        )

        def _get_voice_hotkey_busy(self) -> bool:
            return self._voice_hotkey_busy

        voiceHotkeyBusy = Property(
            bool,
            _get_voice_hotkey_busy,
            notify=voiceHotkeyBusyChanged,
        )

        endpointPreflightBusy = Property(
            bool,
            lambda self: self._endpoint_preflight_busy,
            notify=endpointPreflightBusyChanged,
        )

        def _get_voice_program_custom_path(self) -> str:
            return str(self._voice_program_settings.get("custom_executable", ""))

        def _set_voice_program_custom_path(self, value: str) -> None:
            if self._voice_hotkey_busy or self._voice_settings_write_start_blocked():
                return
            local_value = QUrl(value).toLocalFile() if value.startswith("file:") else value
            local_value = local_value.strip()
            if local_value == self._voice_program_settings.get("custom_executable"):
                return
            updated = dict(self._voice_program_settings)
            updated["custom_executable"] = local_value
            self._update_and_persist_voice_program(updated)

        voiceProgramCustomPath = Property(
            str,
            _get_voice_program_custom_path,
            _set_voice_program_custom_path,
            notify=voiceProgramCustomPathChanged,
        )

        def _get_voice_program_launch_on_bridge_start(self) -> bool:
            return self._voice_program_settings.get("launch_on_bridge_start") is True

        def _set_voice_program_launch_on_bridge_start(self, value: bool) -> None:
            if self._voice_hotkey_busy or self._voice_settings_write_start_blocked():
                return
            value = bool(value)
            if value == self._get_voice_program_launch_on_bridge_start():
                return
            updated = dict(self._voice_program_settings)
            updated["launch_on_bridge_start"] = value
            self._update_and_persist_voice_program(updated)

        voiceProgramLaunchOnBridgeStart = Property(
            bool,
            _get_voice_program_launch_on_bridge_start,
            _set_voice_program_launch_on_bridge_start,
            notify=voiceProgramLaunchOnBridgeStartChanged,
        )

        def _get_voice_program_launch_elevated(self) -> bool:
            return self._voice_program_settings.get("launch_elevated") is True

        def _set_voice_program_launch_elevated(self, value: bool) -> None:
            if self._voice_hotkey_busy or self._voice_settings_write_start_blocked():
                return
            value = bool(value)
            if value == self._get_voice_program_launch_elevated():
                return
            updated = dict(self._voice_program_settings)
            provider_id = str(updated.get("provider", ""))
            preferences = updated.get("launch_elevated_by_provider")
            next_preferences = (
                dict(preferences) if isinstance(preferences, dict) else {}
            )
            next_preferences[provider_id] = value
            updated["launch_elevated_by_provider"] = next_preferences
            updated["launch_elevated"] = value
            self._update_and_persist_voice_program(updated)

        voiceProgramLaunchElevated = Property(
            bool,
            _get_voice_program_launch_elevated,
            _set_voice_program_launch_elevated,
            notify=voiceProgramLaunchElevatedChanged,
        )

        def _get_voice_program_settings_dirty(self) -> bool:
            return self._voice_program_settings_dirty

        voiceProgramSettingsDirty = Property(
            bool,
            _get_voice_program_settings_dirty,
            notify=voiceProgramSettingsDirtyChanged,
        )

        def _get_voice_program_status_text(self) -> str:
            return self._voice_program_status_text

        voiceProgramStatusText = Property(
            str,
            _get_voice_program_status_text,
            notify=voiceProgramStatusTextChanged,
        )

        def _get_voice_program_status_code(self) -> str:
            return self._voice_program_status_code

        voiceProgramStatusCode = Property(
            str,
            _get_voice_program_status_code,
            notify=voiceProgramStatusCodeChanged,
        )

        def _get_voice_program_elevation_status(self) -> str:
            return self._voice_program_elevation_status

        voiceProgramElevationStatus = Property(
            str,
            _get_voice_program_elevation_status,
            notify=voiceProgramElevationStatusChanged,
        )

        def _get_endpoint_options(self) -> List[str]:
            return list(self._endpoint_options)

        endpointOptions = Property(
            list, _get_endpoint_options, notify=endpointOptionsChanged
        )

        def _get_recommended_endpoint_index(self) -> int:
            return self._recommended_endpoint_index

        recommendedEndpointIndex = Property(
            int,
            _get_recommended_endpoint_index,
            notify=recommendedEndpointIndexChanged,
        )

        def _get_selected_endpoint_index(self) -> int:
            return self._selected_endpoint_index

        def _set_selected_endpoint_index(self, value: int) -> None:
            if value != self._selected_endpoint_index:
                self.selectAndPersistOutputEndpointIndex(value)

        selectedEndpointIndex = Property(
            int,
            _get_selected_endpoint_index,
            _set_selected_endpoint_index,
            notify=selectedEndpointIndexChanged,
        )

        def _get_bridge_running(self) -> bool:
            return self._bridge_running

        bridgeRunning = Property(
            bool,
            _get_bridge_running,
            notify=bridgeRunningChanged,
        )

        def _get_bridge_connected(self) -> bool:
            return self._bridge_connected

        bridgeConnected = Property(
            bool,
            _get_bridge_connected,
            notify=bridgeConnectedChanged,
        )

        bridgeRestartRecommended = Property(
            bool,
            lambda self: self._bridge_restart_recommended,
            notify=bridgeRestartRecommendedChanged,
        )

        startHidden = Property(bool, lambda self: self._start_hidden, constant=True)
        launchAtLogin = Property(
            bool,
            lambda self: self._launch_at_login,
            notify=desktopBehaviorChanged,
        )
        launchBridgeOnAppStart = Property(
            bool,
            lambda self: self._launch_bridge_on_app_start,
            notify=desktopBehaviorChanged,
        )
        closeBehavior = Property(
            str,
            lambda self: self._close_behavior,
            notify=desktopBehaviorChanged,
        )
        applicationExitConfirmed = Property(
            bool,
            lambda self: self._application_exit_confirmed,
        )
        closeBehaviorOptions = Property(
            list,
            lambda self: ["隐藏到通知区域", "完全退出"],
            constant=True,
        )
        trayIconSource = Property(
            str,
            _tray_icon_source,
            notify=trayStateChanged,
        )
        trayTooltip = Property(
            str,
            lambda self: (
                f"{product_identity.DISPLAY_NAME}：{device_catalog.RC003_DISPLAY_NAME} 已连接"
                if self._bridge_connected
                else f"{product_identity.DISPLAY_NAME}：服务运行中，等待"
                f"{device_catalog.RC003_DISPLAY_NAME}"
                if self._bridge_running
                else f"{product_identity.DISPLAY_NAME}：服务未启动"
            ),
            notify=trayStateChanged,
        )

        def _get_bridge_launch_phase(self) -> str:
            return self._bridge_launch_phase

        bridgeLaunchPhase = Property(
            str,
            _get_bridge_launch_phase,
            notify=bridgeLaunchPhaseChanged,
        )

        def _get_bridge_launch_busy(self) -> bool:
            return self._bridge_launch_phase in {"saving", "starting", "restarting"}

        bridgeLaunchBusy = Property(
            bool,
            _get_bridge_launch_busy,
            notify=bridgeLaunchPhaseChanged,
        )

        def _get_bridge_launch_elapsed_seconds(self) -> int:
            return self._bridge_launch_elapsed_seconds

        bridgeLaunchElapsedSeconds = Property(
            int,
            _get_bridge_launch_elapsed_seconds,
            notify=bridgeLaunchElapsedSecondsChanged,
        )

        def _get_launch_status_text(self) -> str:
            return self._launch_status_text

        launchStatusText = Property(str, _get_launch_status_text, notify=launchStatusTextChanged)

        def _get_status_message(self) -> str:
            return self._status_message

        statusMessage = Property(str, _get_status_message, notify=statusMessageChanged)

        def _get_error_message(self) -> str:
            return self._error_message

        errorMessage = Property(str, _get_error_message, notify=errorMessageChanged)

        def _get_settings_dirty(self) -> bool:
            return self._settings_dirty

        settingsDirty = Property(
            bool,
            _get_settings_dirty,
            notify=settingsDirtyChanged,
        )

        settingsSaveBusy = Property(
            bool,
            lambda self: self._settings_save_busy,
            notify=settingsSaveBusyChanged,
        )

        def _get_active_page_index(self) -> int:
            return self._active_page_index

        def _set_active_page_index(self, value: int) -> None:
            value = max(
                self._DEVICE_PAGE_INDEX,
                min(self._VOICE_PAGE_INDEX, int(value)),
            )
            if value == self._active_page_index:
                return
            self._active_page_index = value
            self.activePageIndexChanged.emit()

        activePageIndex = Property(
            int,
            _get_active_page_index,
            _set_active_page_index,
            notify=activePageIndexChanged,
        )

        def _get_feedback_page_index(self) -> int:
            return self._feedback_page_index

        feedbackPageIndex = Property(
            int,
            _get_feedback_page_index,
            notify=feedbackPageIndexChanged,
        )

        def _get_selected_button_id(self) -> str:
            return self._selected_button_id

        selectedButtonId = Property(str, _get_selected_button_id, notify=selectedButtonIdChanged)

        def _get_device_options(self) -> List[str]:
            return [
                device_catalog.profile_for(device_id).display_name
                for device_id in self._DEVICE_ORDER
            ]

        deviceOptions = Property(list, _get_device_options, constant=True)
        remoteDisplayName = Property(
            str,
            lambda self: device_catalog.RC003_DISPLAY_NAME,
            constant=True,
        )
        applicationDisplayName = Property(
            str,
            lambda self: product_identity.DISPLAY_NAME,
            constant=True,
        )

        def _get_device_catalog_available(self) -> bool:
            return device_catalog.CATALOG_ERROR is None

        deviceCatalogAvailable = Property(
            bool, _get_device_catalog_available, constant=True
        )

        def _get_device_catalog_error_text(self) -> str:
            if device_catalog.CATALOG_ERROR is None:
                return ""
            return "设备目录不可用：" + device_catalog.CATALOG_ERROR

        deviceCatalogErrorText = Property(
            str, _get_device_catalog_error_text, constant=True
        )

        def _get_selected_device_index(self) -> int:
            return self._selected_device_index

        def _set_selected_device_index(self, value: int) -> None:
            if value == self._selected_device_index or not (0 <= value < len(self._DEVICE_ORDER)):
                return
            self._selected_device_index = value
            self._selected_device_fallback_id = self._DEVICE_ORDER[value]
            self.selectedDeviceIndexChanged.emit()
            self.selectedDeviceChanged.emit()
            self._mark_settings_dirty()

        selectedDeviceIndex = Property(
            int,
            _get_selected_device_index,
            _set_selected_device_index,
            notify=selectedDeviceIndexChanged,
        )

        def _get_is_rc003_device(self) -> bool:
            return self._selected_device_id() == device_catalog.RC003_ID

        isRc003Device = Property(bool, _get_is_rc003_device, notify=selectedDeviceChanged)

        def _get_selected_device_description(self) -> str:
            if device_catalog.CATALOG_ERROR is not None:
                return self._get_device_catalog_error_text()
            return device_catalog.profile_for(self._selected_device_id()).description

        selectedDeviceDescription = Property(
            str, _get_selected_device_description, notify=selectedDeviceChanged
        )

        mappingPageTitle = Property(str, lambda self: "按键映射", constant=True)

        def _get_key_detection_active(self) -> bool:
            return self._key_detection_active

        keyDetectionActive = Property(
            bool,
            _get_key_detection_active,
            notify=keyDetectionActiveChanged,
        )

        def _get_key_detection_text(self) -> str:
            return self._key_detection_text

        keyDetectionText = Property(
            str,
            _get_key_detection_text,
            notify=keyDetectionTextChanged,
        )

        hotkeyCaptureActive = Property(
            bool,
            _get_hotkey_capture_active,
            notify=hotkeyCaptureActiveChanged,
        )

        inputCaptureInUse = Property(
            bool,
            _get_input_capture_in_use,
            notify=inputOperationChanged,
        )

        def _get_primary_action_options(self) -> List[str]:
            return list(_ORDINARY_PRIMARY_ACTION_OPTIONS)

        primaryActionOptions = Property(
            list, _get_primary_action_options, constant=True
        )

        @Slot(str, result=list)
        def primaryActionOptionsFor(self, button_id: str) -> List[str]:
            if button_id == "mic":
                return list(_MIC_PRIMARY_ACTION_OPTIONS)
            return list(_ORDINARY_PRIMARY_ACTION_OPTIONS)

        @Slot(str, result=str)
        def actionOptionGroupTitle(self, option: str) -> str:
            return settings_ui.ACTION_OPTION_GROUP_BY_LABEL.get(option, "")

        @Slot(str, result=bool)
        def actionOptionStartsGroup(self, option: str) -> bool:
            return option in settings_ui.ACTION_OPTION_GROUP_STARTS

        def _get_secondary_action_options(self) -> List[str]:
            return list(_SECONDARY_ACTION_OPTIONS)

        secondaryActionOptions = Property(
            list, _get_secondary_action_options, constant=True
        )

        def _get_combo_modifier_options(self) -> List[str]:
            return [
                _COMBO_MODIFIER_LABELS[button_id]
                for button_id in key_mapping.COMBO_MODIFIER_BUTTON_IDS
            ]

        comboModifierOptions = Property(
            list, _get_combo_modifier_options, constant=True
        )

        def _get_combo_modifier_index(self) -> int:
            try:
                return key_mapping.COMBO_MODIFIER_BUTTON_IDS.index(
                    self._combo_modifier_id
                )
            except ValueError:
                return 0

        def _set_combo_modifier_index(self, index: int) -> None:
            if not (0 <= index < len(key_mapping.COMBO_MODIFIER_BUTTON_IDS)):
                return
            modifier = key_mapping.COMBO_MODIFIER_BUTTON_IDS[index]
            if modifier == self._combo_modifier_id:
                return
            self._combo_modifier_id = modifier
            self.comboModifierIndexChanged.emit()
            self._mark_settings_dirty()

        comboModifierIndex = Property(
            int,
            _get_combo_modifier_index,
            _set_combo_modifier_index,
            notify=comboModifierIndexChanged,
        )

        def _get_combo_modifier_text(self) -> str:
            return _COMBO_MODIFIER_LABELS.get(self._combo_modifier_id, "TV")

        comboModifierText = Property(
            str,
            _get_combo_modifier_text,
            notify=comboModifierIndexChanged,
        )

        def _get_combo_rows(self) -> List[dict]:
            return [
                {
                    "buttonId": button_id,
                    "buttonName": remote_layout.BUTTON_DISPLAY_NAMES.get(
                        button_id, button_id
                    ),
                    "actionText": self._combo_action_text[button_id],
                    "noteText": self._combo_note_text[button_id],
                }
                for button_id in key_mapping.COMBO_ACTION_BUTTON_IDS
            ]

        comboRows = Property(list, _get_combo_rows, notify=comboRowsChanged)

        @Slot(str, str)
        def setComboActionText(self, button_id: str, text: str) -> None:
            if button_id not in self._combo_action_text:
                return
            value = str(text)
            if value == self._combo_action_text[button_id]:
                return
            self._combo_action_text[button_id] = value
            self._mark_settings_dirty()

        @Slot(str, str)
        def setComboNoteText(self, button_id: str, text: str) -> None:
            if button_id not in self._combo_note_text:
                return
            value = str(text)
            if value == self._combo_note_text[button_id]:
                return
            self._combo_note_text[button_id] = value
            self._mark_settings_dirty()

        # Compatibility alias for older QML probes. New code uses the two
        # semantically distinct option properties above.
        presetActionOptions = Property(
            list, _get_primary_action_options, constant=True
        )

        def _get_photo_source(self) -> str:
            photo_path = resources.find_remote_photo()
            if photo_path is None:
                return ""
            return QUrl.fromLocalFile(str(photo_path)).toString()

        photoSource = Property(str, _get_photo_source, constant=True)

        def _get_photo_available(self) -> bool:
            return resources.find_remote_photo() is not None

        photoAvailable = Property(bool, _get_photo_available, constant=True)

        # -- slots ----------------------------------------------------------

        @Slot(result=bool)
        def saveSettings(self) -> bool:
            if (
                self._get_bridge_launch_busy()
                or self._endpoint_preflight_busy
                or _driver_action_active_event.is_set()
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                self._set_error_message(
                    "当前有其它操作正在进行；完成后再保存按键映射。",
                    self._BUTTONS_PAGE_INDEX,
                )
                return False
            return self._save()

        @Slot(bool)
        def setLaunchAtLogin(self, enabled: bool) -> None:
            enabled = bool(enabled)
            if enabled == self._launch_at_login:
                return
            result = startup_windows.set_startup_enabled(enabled)
            if result.error:
                self.desktopBehaviorChanged.emit()
                self._set_status_message("")
                self._set_error_message(
                    f"无法修改随 Windows 启动：{result.error}",
                    self._DESKTOP_BEHAVIOR_PAGE_INDEX,
                )
                return
            self._launch_at_login = result.enabled
            self.desktopBehaviorChanged.emit()
            self._set_error_message("")
            self._set_status_message(
                "已启用随 Windows 启动。"
                if result.enabled
                else "已关闭随 Windows 启动。",
                self._DESKTOP_BEHAVIOR_PAGE_INDEX,
            )

        @Slot(bool)
        def setLaunchBridgeOnAppStart(self, enabled: bool) -> None:
            enabled = bool(enabled)
            if enabled == self._launch_bridge_on_app_start:
                return
            if not self._persist_desktop_behavior(
                "launch_bridge_on_app_start", enabled
            ):
                self.desktopBehaviorChanged.emit()
                return
            self._launch_bridge_on_app_start = enabled
            self.desktopBehaviorChanged.emit()

        @Slot(int)
        def setCloseBehaviorIndex(self, index: int) -> None:
            value = (
                config.CLOSE_BEHAVIOR_QUIT
                if int(index) == 1
                else config.CLOSE_BEHAVIOR_HIDE_TO_TRAY
            )
            if value == self._close_behavior:
                return
            if not self._persist_desktop_behavior("close_behavior", value):
                self.desktopBehaviorChanged.emit()
                return
            self._close_behavior = value
            self.desktopBehaviorChanged.emit()

        @Slot()
        def startBridgeOnApplicationStart(self) -> None:
            if self._launch_bridge_on_app_start and not self._bridge_running:
                self.startBridge()

        @Slot()
        def saveSettingsAndExit(self) -> None:
            if (
                self._application_exit_requested
                or self._application_exit_confirmed
                or self._save_then_exit_requested
            ):
                return
            self._save_then_exit_requested = True

            def finish(saved: bool) -> None:
                self._save_then_exit_requested = False
                self.saveSettingsAndExitFinished.emit(bool(saved))
                if saved:
                    self.requestApplicationExit()

            self._save(completion=finish)

        @Slot()
        def requestApplicationExit(self) -> None:
            if (
                self._application_exit_requested
                or self._application_exit_confirmed
            ):
                return
            self._application_exit_requested = True
            self._application_exit_deadline = 0.0
            self._window_hide_requested = False
            if self._settings_save_busy:
                self._application_exit_waiting_for_save = True
                self._application_exit_deadline = (
                    time.monotonic()
                    + _APPLICATION_EXIT_SAVE_WAIT_TIMEOUT_SECONDS
                )
                self._schedule_application_exit_poll()
                return
            self._begin_application_exit()

        def _begin_application_exit(self) -> None:
            self._application_exit_waiting_for_save = False
            self._application_exit_deadline = (
                time.monotonic() + _APPLICATION_EXIT_WAIT_TIMEOUT_SECONDS
            )
            self._application_exit_intent.set()
            if self._get_input_capture_in_use():
                self._request_input_stop()
            if self._application_exit_poll_scheduled:
                return
            self._continue_application_exit()

        def _schedule_application_exit_poll(self) -> None:
            if (
                not self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_poll_scheduled
                or self._application_exit_stop_running
            ):
                return
            self._application_exit_poll_scheduled = True
            QTimer.singleShot(
                _APPLICATION_EXIT_POLL_INTERVAL_MS,
                self._continue_application_exit,
            )

        def _continue_application_exit(self) -> None:
            self._application_exit_poll_scheduled = False
            if (
                not self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_stop_running
            ):
                return
            if self._application_exit_waiting_for_save:
                if time.monotonic() >= self._application_exit_deadline:
                    self._fail_application_exit(
                        "完全退出超时：设置仍在保存，请稍后重试。"
                    )
                    return
                self._schedule_application_exit_poll()
                return
            if time.monotonic() >= self._application_exit_deadline:
                self._fail_application_exit(
                    "完全退出超时：后台操作尚未结束，请稍后重试。"
                )
                return

            if self._get_input_capture_in_use():
                self._request_input_stop()
                self._schedule_application_exit_poll()
                return
            if (
                self._voice_hotkey_busy
                or self._settings_save_busy
                or self._endpoint_preflight_busy
                or self._bridge_recovery_running
                or _vb_cable_test_active_event.is_set()
                or _driver_action_active_event.is_set()
            ):
                self._schedule_application_exit_poll()
                return

            if self._pending_bridge_launch is not None:
                self.pollBridgeLaunch()
                if self._pending_bridge_launch is not None:
                    self._schedule_application_exit_poll()
                    return
            if (
                self._bridge_launch_phase in {"saving", "starting"}
                and self._pending_bridge_launch is None
            ):
                self._set_bridge_launch_phase("idle")

            bridge_running = self._refresh_bridge_status()
            if bridge_running is False:
                self._on_application_exit_stop_ready((True, ""))
                return

            self._application_exit_stop_running = True

            def stop_and_exit() -> None:
                try:
                    result = bridge_control_windows.request_bridge_exit()
                except Exception as exc:  # noqa: BLE001 - must remain retryable
                    payload = (False, f"完全退出失败：{type(exc).__name__}")
                else:
                    payload = (
                        bool(result.stopped),
                        result.error or "遥控器服务未能正常停止。",
                    )
                self._emit_background_result(
                    self._applicationExitStopReady,
                    payload,
                )

            try:
                self._start_background_task(
                    stop_and_exit, "remote-mic-application-exit"
                )
            except RuntimeError as exc:
                self._application_exit_stop_running = False
                self._fail_application_exit(str(exc))

        def _on_application_exit_stop_ready(self, payload: object) -> None:
            self._application_exit_stop_running = False
            if not self._application_exit_requested:
                return
            stopped, message = payload
            if stopped:
                self._application_exit_requested = False
                self._application_exit_waiting_for_save = False
                self._application_exit_confirmed = True
                self._application_exit_intent.set()
                self.applicationExitReady.emit()
                return
            self._fail_application_exit(str(message))

        def _fail_application_exit(self, message: str) -> None:
            self._application_exit_requested = False
            self._application_exit_stop_running = False
            self._application_exit_poll_scheduled = False
            self._application_exit_waiting_for_save = False
            self._application_exit_deadline = 0.0
            self._application_exit_intent.clear()
            self.applicationExitFailed.emit(message)

        @Slot()
        def refreshVoiceProgramStatus(self) -> None:
            self._request_voice_program_status_refresh()

        @Slot()
        def refreshVoiceProgramOptions(self) -> None:
            self._request_voice_program_options_refresh()

        @Slot()
        def refreshVoiceHotkeyFromProvider(self) -> None:
            if self._voice_hotkey_busy or self._voice_settings_write_start_blocked():
                return
            provider_id = str(self._voice_program_settings.get("provider", ""))
            if provider_id in {
                voice_program_manager.VOICE_PROGRAM_NONE,
                voice_program_manager.VOICE_PROGRAM_CUSTOM,
                voice_program_manager.VOICE_PROGRAM_WETYPE,
            }:
                return
            self._set_voice_hotkey_busy(True)
            self._set_status_message("正在读取语音程序快捷键…", self._VOICE_PAGE_INDEX)
            self._submit_voice_hotkey_step(
                lambda: voice_hotkey_sync_windows.read_provider_hotkey(
                    provider_id
                ),
                self._on_voice_hotkey_refresh_ready,
            )

        def _on_voice_hotkey_refresh_ready(
            self, ok: bool, payload: object
        ) -> None:
            if not ok:
                self._set_status_message("")
                self._set_error_message(
                    f"读取语音程序快捷键失败：{payload}",
                    self._VOICE_PAGE_INDEX,
                )
                self._finish_voice_hotkey_operation()
                return
            result = payload
            if not result.ok:
                self._set_status_message("")
                self._set_error_message(result.message, self._VOICE_PAGE_INDEX)
                self._finish_voice_hotkey_operation()
                return
            current = self._get_hold_voice_hotkey_text()
            if result.hotkey == current:
                self._set_error_message("")
                self._set_status_message(result.message, self._VOICE_PAGE_INDEX)
                self._finish_voice_hotkey_operation()
                return
            self._set_voice_hotkey_text(
                key_mapping.VoiceTriggerMode.HOLD,
                result.hotkey,
            )
            if not self._persist_voice_settings():
                self._set_voice_hotkey_text(
                    key_mapping.VoiceTriggerMode.HOLD,
                    current,
                )
                self._finish_voice_hotkey_operation()
                return
            self._set_status_message(
                result.message
                + ("；按键映射仍未保存。" if self._settings_dirty else ""),
                self._VOICE_PAGE_INDEX,
            )
            self._finish_voice_hotkey_operation()

        @Slot()
        def launchVoiceProgram(self) -> None:
            self._launch_voice_program(
                feedback_page_index=self._VOICE_PAGE_INDEX
            )

        def _launch_voice_program(
            self,
            *,
            feedback_page_index: int,
        ) -> None:
            try:
                result = voice_program_manager.launch_voice_program(
                    self._voice_program_settings
                )
            except Exception:  # noqa: BLE001 - optional launch must not stop the UI
                self._set_status_message("")
                self._set_error_message(
                    f"语音程序启动失败；{product_identity.DISPLAY_NAME}和桥接不受影响。",
                    feedback_page_index,
                )
                self._request_voice_program_status_refresh()
                return
            message = voice_program_manager.launch_result_text(result)
            if result.code in {"not_found", "launch_failed", "restart_elevated_required"}:
                self._set_status_message("")
                self._set_error_message(message, feedback_page_index)
            else:
                self._set_error_message("")
                self._set_status_message(message, feedback_page_index)
            self._request_voice_program_status_refresh()

        @Slot()
        def refreshBridgeState(self) -> None:
            self._refresh_bridge_status()

        @Slot()
        def startKeyDetection(self) -> None:
            """Listen for one real RC003 press without executing its action."""

            if self._key_detection_active or self._get_input_capture_in_use():
                return
            if self._get_bridge_launch_busy():
                self._set_key_detection_text(
                    "遥控器服务正在启动；完成后再检测真实按键"
                )
                return
            if _vb_cable_test_active_event.is_set():
                self._set_key_detection_text(
                    "声音通道测试正在运行；结束后再检测真实按键"
                )
                return
            if self._selected_device_id() != device_catalog.RC003_ID:
                self._set_key_detection_text(
                    f"当前设备不是{device_catalog.RC003_DISPLAY_NAME}；无法检测遥控器按键"
                )
                return
            bridge_running = self._refresh_bridge_status()
            if bridge_running is None:
                self._set_key_detection_text(
                    "无法确认后台服务状态；请关闭设置窗口和服务后重试"
                )
                return
            self._set_key_detection_text(
                "正在启动真实按键检测…"
            )
            physical_bindings = dict(
                self._bindings.get("physical_bindings", {})
            )
            self._begin_input_operation_start(
                "key_detection",
                lambda cancel_event: self._start_key_detection_worker(
                    cancel_event,
                    bridge_running=bool(bridge_running),
                    physical_bindings=physical_bindings,
                ),
            )

        @Slot()
        def pollKeyDetectionBridge(self) -> None:
            request = self._key_detection_bridge_request
            if not self._key_detection_active:
                return
            if (
                time.monotonic() - self._key_detection_started_at
                >= self._KEY_DETECTION_TIMEOUT_SECONDS
            ):
                if request is not None:
                    timeout_text = "等待后台按键超时；确认遥控器已连接后重试"
                else:
                    timeout_text = "等待按键超时；确认遥控器已连接后重试"
                self._key_detection_started_at = 0.0
                self._key_detection_tap_usages.clear()
                self._request_input_stop(
                    kind="key_detection",
                    key_detection_success_message=timeout_text,
                )
                return
            if request is None:
                return
            button_id = key_detection_bridge.poll_detection(request)
            if button_id is not None:
                self._on_raw_key_detected(button_id, " 来源=后台桥接。")

        @Slot()
        def startHotkeyCapture(self) -> None:
            """Start the EXE-owned physical keyboard shortcut recorder."""

            self._begin_input_operation_start(
                "hotkey", self._start_hotkey_capture_worker
            )

        @Slot(result=bool)
        def stopHotkeyCapture(self) -> bool:
            """Stop the physical recorder, including Cancel/window close."""

            return self._request_input_stop(kind="hotkey")

        @Slot(result=bool)
        def stopKeyDetection(self) -> bool:
            self._key_detection_started_at = 0.0
            self._key_detection_tap_usages.clear()
            return self._request_input_stop(kind="key_detection")

        @Slot()
        def saveAndLaunch(self) -> None:
            """Start the staged save/launch flow without blocking Qt."""

            if self._get_bridge_launch_busy():
                return
            if self._get_input_capture_in_use():
                self._set_error_message(
                    "按键录入或检测正在进行；结束后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            self._has_explicit_launch_result = True
            self._bridge_launch_started_at = time.monotonic()
            if self._bridge_launch_elapsed_seconds != 0:
                self._bridge_launch_elapsed_seconds = 0
                self.bridgeLaunchElapsedSecondsChanged.emit()
            self._set_bridge_launch_phase("saving")
            self._set_bridge_connected(False)
            self._set_launch_status("正在保存设置…")
            QTimer.singleShot(0, self._continue_save_and_launch)

        def _continue_save_and_launch(self) -> None:
            if self._bridge_launch_phase != "saving":
                return
            self._save(completion=self._on_save_for_launch_complete)

        def _on_save_for_launch_complete(self, saved: bool) -> None:
            if self._bridge_launch_phase != "saving":
                return
            if not saved:
                self._set_bridge_launch_phase("failed")
                self._set_launch_status("保存未完成，未启动桥接。")
                return
            self._start_bridge_process()

        def _start_automatic_bridge_recovery(self) -> None:
            if not self._bridge_running or self._bridge_recovery_running:
                return
            status = bridge_runtime_status.read_status(self._config_root)
            if status is None or status.voice_active:
                return
            identity_match = bridge_runtime_status.runtime_identity_matches(
                status,
                self._current_runtime_identity,
            )
            if identity_match is True and not bridge_runtime_status.input_channels_failed(
                status
            ):
                self._set_bridge_restart_recommended(False)
                return
            self._begin_bridge_restart(automatic=True)

        @Slot()
        def restartBridge(self) -> None:
            self._begin_bridge_restart(automatic=False)

        def _begin_bridge_restart(self, *, automatic: bool) -> None:
            if (
                self._bridge_recovery_running
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                return
            if not self._bridge_running:
                self.startBridge()
                return
            status = bridge_runtime_status.read_status(self._config_root)
            if status is not None and status.voice_active:
                self._set_error_message(
                    "当前正在语音输入；结束本次语音后再重新启动服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            self._bridge_recovery_running = True
            self._has_explicit_launch_result = True
            self._bridge_launch_started_at = time.monotonic()
            self._set_bridge_launch_phase("restarting")
            self._set_bridge_restart_recommended(False)
            self._set_error_message("")
            self._set_launch_status(
                "检测到旧版或按键通道异常；正在正常停止服务并启动当前版本…"
                if automatic
                else "正在正常停止服务并启动当前版本…"
            )

            def stop_for_restart() -> None:
                try:
                    result = bridge_control_windows.request_bridge_exit()
                except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                    payload = (False, f"重新启动失败：{type(exc).__name__}")
                else:
                    payload = (
                        bool(result.stopped),
                        result.error or "遥控器服务未能正常停止。",
                    )
                self._emit_background_result(self._bridgeRestartStopReady, payload)

            try:
                self._start_background_task(
                    stop_for_restart,
                    "remote-mic-bridge-restart",
                )
            except RuntimeError as exc:
                self._bridge_recovery_running = False
                self._set_bridge_launch_phase("failed")
                self._set_bridge_restart_recommended(True)
                self._set_launch_status(str(exc))

        def _on_bridge_restart_stop_ready(self, payload: object) -> None:
            self._bridge_recovery_running = False
            stopped, message = payload
            if not stopped:
                self._set_bridge_launch_phase("failed")
                self._set_bridge_restart_recommended(True)
                self._set_launch_status(str(message))
                return
            self._set_bridge_running(False)
            self._set_bridge_connected(False)
            self._set_bridge_launch_phase("starting")
            self._set_launch_status("旧服务已退出；正在启动当前版本…")
            QTimer.singleShot(0, self._start_bridge_process)

        def _start_bridge_process(self) -> None:
            if self._bridge_launch_phase not in {"saving", "starting"}:
                return
            if (
                self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                self._pending_bridge_launch = None
                self._set_bridge_launch_phase("idle")
                self._schedule_application_exit_poll()
                return
            saved_before_launch = self._bridge_launch_phase == "saving"
            self._set_bridge_launch_phase("starting")
            if saved_before_launch:
                self._set_launch_status("设置已保存；正在启动遥控器服务…")
            else:
                self._set_launch_status("正在按上次保存的设置启动遥控器服务…")
            try:
                attempt = bridge_launcher.start_bridge_launch()
            except bridge_launcher.BridgeLaunchConfigurationError as exc:
                self._finish_bridge_launch(
                    bridge_launcher.LaunchResult(
                        outcome=bridge_launcher.LaunchOutcome.LAUNCH_FAILED,
                        command=(),
                        error=type(exc).__name__,
                    )
                )
                return
            if isinstance(attempt, bridge_launcher.LaunchResult):
                self._finish_bridge_launch(attempt)
                return
            self._pending_bridge_launch = attempt

        @Slot()
        def startBridge(self) -> None:
            """Start the bridge without saving unrelated unsaved page edits."""

            if (
                self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                return
            if self._get_bridge_launch_busy():
                return
            if self._settings_save_busy:
                self._set_error_message(
                    "按键映射正在保存；完成后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if self._endpoint_preflight_busy or _driver_action_active_event.is_set():
                self._set_error_message(
                    "输出端点正在处理；完成后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if self._get_input_capture_in_use():
                self._set_error_message(
                    "按键录入或检测正在进行；结束后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if self._voice_hotkey_busy:
                self._set_error_message(
                    "语音快捷键正在处理；完成后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            if _vb_cable_test_active_event.is_set():
                self._set_error_message(
                    "声音通道测试正在运行；测试结束后再启动遥控器服务。",
                    self._DEVICE_PAGE_INDEX,
                )
                return
            self._has_explicit_launch_result = True
            self._bridge_launch_started_at = time.monotonic()
            if self._bridge_launch_elapsed_seconds != 0:
                self._bridge_launch_elapsed_seconds = 0
                self.bridgeLaunchElapsedSecondsChanged.emit()
            self._set_bridge_connected(False)
            self._set_error_message("")
            self._set_bridge_launch_phase("starting")
            self._set_launch_status(
                "正在按上次保存的设置启动遥控器服务…"
            )
            QTimer.singleShot(0, self._start_bridge_process)

        def _finish_bridge_launch(
            self,
            result: bridge_launcher.LaunchResult,
        ) -> None:
            self._pending_bridge_launch = None
            self._refresh_bridge_launch_elapsed()
            if result.outcome in {
                bridge_launcher.LaunchOutcome.STARTED,
                bridge_launcher.LaunchOutcome.ALREADY_RUNNING,
            }:
                self._set_bridge_running(True)
                self._set_bridge_launch_phase("waiting")
                if (
                    result.outcome is bridge_launcher.LaunchOutcome.ALREADY_RUNNING
                    and not self._application_exit_requested
                    and self._voice_program_settings.get("launch_on_bridge_start")
                    is True
                ):
                    self._launch_voice_program(
                        feedback_page_index=self._DEVICE_PAGE_INDEX
                    )
                self._sync_bridge_connection_status(True)
                self._schedule_application_exit_poll()
                return
            self._set_bridge_running(False)
            self._set_bridge_connected(False)
            self._set_bridge_launch_phase(
                "unknown"
                if result.outcome is bridge_launcher.LaunchOutcome.STATUS_UNKNOWN
                else "failed"
            )
            self._set_launch_status(settings_ui.describe_launch_result(result))
            self._schedule_application_exit_poll()

        @Slot()
        def pollBridgeLaunch(self) -> None:
            self._refresh_bridge_launch_elapsed()
            pending = self._pending_bridge_launch
            if pending is None:
                return
            result = bridge_launcher.poll_bridge_launch(pending)
            if result is not None:
                self._finish_bridge_launch(result)

        @Slot()
        def restoreMappingDefaults(self) -> None:
            """Reset only the button-page values without touching voice setup."""

            defaults = settings_ui.default_display_state()
            self._model.load_display_map(
                defaults.button_display_map,
                defaults.secondary_display_map,
                {},
            )
            self._reset_combo_mapping_display()
            self._mark_settings_dirty()
            self._set_error_message("")
            self._set_status_message(
                "已恢复按键默认显示并清空遥控器组合，尚未保存——"
                "点击「保存映射」才会写入设置。",
                self._BUTTONS_PAGE_INDEX,
            )

        @Slot()
        def useWindowsDictationHotkey(self) -> None:
            self._update_and_persist_voice_hotkey("win+h")

        @Slot()
        def restoreDefaults(self) -> None:
            """Resets every widget's DISPLAYED value to the defaults - never
            writes to config.json/key_bindings.json itself (XRBM-030 RETRY 1
            blocker 4: this must never look/claim to be persisted). The
            status message says so explicitly, so a user who restores
            defaults and then simply closes the window without saving is
            not misled into thinking anything was written to disk.
            """

            if self._voice_hotkey_busy:
                return

            defaults = settings_ui.default_display_state()
            self._model.load_display_map(
                defaults.button_display_map,
                defaults.secondary_display_map,
                {},
            )
            self._reset_combo_mapping_display()
            self._set_voice_hotkey_text(
                key_mapping.VoiceTriggerMode.HOLD,
                defaults.voice_hotkeys[key_mapping.VoiceTriggerMode.HOLD.value]
            )
            self._replace_voice_program_settings({})
            self._set_voice_program_settings_dirty(True)
            self._mark_settings_dirty()
            self._set_error_message("")
            self._set_status_message(
                "已恢复默认显示，尚未保存——点击「保存映射」或「仅保存设置」才会写入设置。",
                self._BUTTONS_PAGE_INDEX,
            )

        @Slot()
        def openLogLocation(self) -> None:
            result = logging_setup.open_log_location()
            self._set_status_message(
                settings_ui.describe_log_open_result(result),
                self._DEVICE_PAGE_INDEX,
            )

        @Slot(str)
        def selectButton(self, button_id: str) -> None:
            if button_id == self._selected_button_id:
                return
            self._model.set_selected_button(button_id)
            self._selected_button_id = self._model.selected_button_id()
            self.selectedButtonIdChanged.emit()

        def _report_external_target(self, result, page_index: int) -> None:
            if result.outcome is shell_targets.ExternalTargetOutcome.OPENED:
                self._set_error_message("")
                self._set_status_message(f"已打开：{result.target}", page_index)
            else:
                self._set_status_message("")
                self._set_error_message(
                    f"无法打开 {result.target}（{result.error}）",
                    page_index,
                )

        @Slot()
        def openBluetoothSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.BLUETOOTH_SETTINGS_URI),
                self._DEVICE_PAGE_INDEX,
            )

        @Slot()
        def openMicrophonePrivacySettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(
                    shell_targets.MICROPHONE_PRIVACY_SETTINGS_URI
                ),
                self._VOICE_PAGE_INDEX,
            )

        @Slot()
        def openVoiceProgramSettings(self) -> None:
            try:
                target = (
                    voice_program_manager.resolve_voice_program_settings_target(
                        self._voice_program_settings
                    )
                )
            except Exception as exc:  # noqa: BLE001 - Qt slot must not escape
                self._set_status_message("")
                self._set_error_message(
                    f"无法查找语音程序设置入口（{exc}）",
                    self._VOICE_PAGE_INDEX,
                )
                return
            if not target.available:
                if (
                    target.provider_id
                    == voice_program_manager.VOICE_PROGRAM_SOGOU
                ):
                    self._set_error_message("")
                    self._set_status_message(
                        "未安装搜狗语音，请在搜狗输入法 AI 工具箱中手动安装。",
                        self._VOICE_PAGE_INDEX,
                    )
                    return
                self._set_status_message("")
                message = (
                    "未找到微信输入法设置程序。"
                    if target.provider_id
                    == voice_program_manager.VOICE_PROGRAM_WETYPE
                    else "当前语音程序没有可打开的设置入口。"
                )
                self._set_error_message(message, self._VOICE_PAGE_INDEX)
                return

            success_message = f"已打开{target.display_name}设置。"
            if target.kind == "sogou_manual":
                self._set_error_message("")
                self._set_status_message(
                    "请在任务栏通知区域（必要时先展开隐藏图标）右键搜狗语音图标，选择“设置”。",
                    self._VOICE_PAGE_INDEX,
                )
                return
            if target.kind == "sogou_toolbox":
                try:
                    voice_program_manager.open_voice_program_settings(
                        Path(target.target),
                        target.arguments,
                    )
                except Exception as exc:  # noqa: BLE001 - Qt slot must not escape
                    self._set_status_message("")
                    self._set_error_message(
                        f"无法打开搜狗输入法 AI 工具箱（{exc}）",
                        self._VOICE_PAGE_INDEX,
                    )
                    return
                success_message = (
                    "未安装搜狗语音，已打开 AI 工具箱，请手动安装。"
                )
            elif target.kind == "uri":
                result = shell_targets.open_external_target(target.target)
                if result.outcome is not shell_targets.ExternalTargetOutcome.OPENED:
                    self._set_status_message("")
                    self._set_error_message(
                        f"无法打开{target.display_name}设置（{result.error}）",
                        self._VOICE_PAGE_INDEX,
                    )
                    return
            else:
                try:
                    voice_program_manager.open_voice_program_settings(
                        Path(target.target),
                        target.arguments,
                    )
                except Exception as exc:  # noqa: BLE001 - Qt slot must not escape
                    self._set_status_message("")
                    self._set_error_message(
                        f"无法打开{target.display_name}设置（{exc}）",
                        self._VOICE_PAGE_INDEX,
                    )
                    return

            self._set_error_message("")
            self._set_status_message(
                success_message,
                self._VOICE_PAGE_INDEX,
            )

        @Slot()
        def openSpeechSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.SPEECH_SETTINGS_URI),
                self._VOICE_PAGE_INDEX,
            )

        @Slot()
        def openSoundSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.SOUND_SETTINGS_URI),
                self._VOICE_PAGE_INDEX,
            )

        @Slot()
        def openAppsSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.APPS_SETTINGS_URI),
                self._DEVICE_PAGE_INDEX,
            )

        def _select_and_persist_output_endpoint(
            self,
            name: str,
            host_api: str,
            completion: Optional[Callable[[bool, str], None]] = None,
            *,
            allow_driver_action: bool = False,
        ) -> bool:
            if (
                _vb_cable_test_active_event.is_set()
                or self._voice_hotkey_busy
                or self._settings_save_busy
                or self._get_bridge_launch_busy()
                or (
                    _driver_action_active_event.is_set()
                    and not allow_driver_action
                )
                or self._application_exit_requested
                or self._application_exit_confirmed
                or self._application_exit_intent.is_set()
            ):
                if completion is not None:
                    completion(False, "当前有其它操作正在进行。")
                return False

            current = (
                str(self._config.get("output_endpoint_name", "")),
                str(self._config.get("output_endpoint_host_api", "")),
            )
            requested = (str(name), str(host_api))
            if requested == current:
                if completion is not None:
                    completion(True, "")
                return True

            settled = False
            settled_result = False

            def finish(ok: bool, message: str) -> None:
                nonlocal settled, settled_result
                if (
                    self._application_exit_requested
                    or self._application_exit_confirmed
                    or self._application_exit_intent.is_set()
                ):
                    ok = False
                    message = "程序正在退出，未保存输出端点。"
                if ok:
                    new_config = dict(self._config)
                    new_config["output_endpoint_name"] = requested[0]
                    new_config["output_endpoint_host_api"] = requested[1]
                    try:
                        saved_config = config.save_config_and_load(
                            config.config_path(self._config_root), new_config
                        )
                    except Exception:  # noqa: BLE001 - keep UI text sanitized
                        ok = False
                        message = "输出端点设置无法写入。"
                    else:
                        self._config = saved_config
                        self._bump_settings_revision()
                        self._request_endpoint_options_refresh()
                settled = True
                settled_result = bool(ok)
                if completion is not None:
                    completion(bool(ok), str(message))

            accepted = self._request_endpoint_preflight(
                requested[0], requested[1], finish
            )
            return settled_result if settled else accepted

        @Slot(str, str, result=bool)
        def selectAndPersistOutputEndpoint(self, name: str, host_api: str) -> bool:
            """Queue an isolated endpoint preflight and persist on success."""

            return self._select_and_persist_output_endpoint(name, host_api)

        @Slot(int, result=bool)
        def selectAndPersistOutputEndpointIndex(self, index: int) -> bool:
            if not 0 <= index < len(self._endpoint_values):
                self._set_error_message(
                    "输出端点保存失败：所选端点已经不可用。",
                    self._VOICE_PAGE_INDEX,
                )
                self.selectedEndpointIndexChanged.emit()
                return False

            endpoint = self._endpoint_values[index]
            self._set_error_message("")
            self._set_status_message(
                "正在检查输出端点…",
                self._VOICE_PAGE_INDEX,
            )

            def finished(ok: bool, message: str) -> None:
                if not ok:
                    self._set_status_message("")
                    self._set_error_message(
                        "输出端点保存失败："
                        + (message or "所选设备无法打开或设置无法写入。"),
                        self._VOICE_PAGE_INDEX,
                    )
                    self.selectedEndpointIndexChanged.emit()
                    return
                self._set_error_message("")
                self._set_status_message(
                    "输出端点已自动保存；按键映射仍未保存。"
                    "重启遥控器服务后端点生效。"
                    if self._settings_dirty
                    else "输出端点已自动保存；重启遥控器服务后生效。",
                    self._VOICE_PAGE_INDEX,
                )

            accepted = self._select_and_persist_output_endpoint(
                endpoint.name,
                endpoint.host_api,
                finished,
            )
            if not accepted and not self._endpoint_preflight_busy:
                self.selectedEndpointIndexChanged.emit()
            return accepted

    def _diagnostics_check_to_row(check: "windows_diagnostics.CheckResult") -> dict:
        return {
            "checkId": check.check_id,
            "title": check.title,
            "group": check.group.value,
            "status": check.status.value,
            "detail": check.detail,
        }

    class DiagnosticsController(QObject):
        """QML-facing adapter for the "检查与修复" page (XRBM-031). Every
        check runs off the Qt GUI thread (see module docstring); every
        driver-launch/endpoint-select action is a thin wrapper with no
        business logic of its own, matching SettingsController's contract.
        """

        checkResultsChanged = Signal()
        isRefreshingChanged = Signal()
        diagnosticsErrorMessageChanged = Signal()
        driverStatusMessageChanged = Signal()
        driverInfoMessageChanged = Signal()
        driverErrorMessageChanged = Signal()
        driverActionRunningChanged = Signal()
        vbCableTestChanged = Signal()
        vbCableBridgeRecoveryChanged = Signal()
        # Internal only - never connected to from QML. Carries a
        # windows_diagnostics.DiagnosticsReport (or None on an unexpected
        # worker-thread exception) back from the background thread to this
        # object's own (GUI) thread - see module docstring for why a plain
        # Signal(object) connection is sufficient here.
        _diagnosticsReady = Signal(object)
        _vbCableTestReady = Signal(object)
        _detectedEndpointReady = Signal(object)

        def __init__(
            self,
            settings_controller: "SettingsController",
            config_root,
            parent=None,
            *,
            auto_refresh: bool = True,
        ) -> None:
            super().__init__(parent)
            self._settings_controller = settings_controller
            self._config_root = config_root
            self._is_refreshing = False
            self._check_rows: List[dict] = []
            self._diagnostics_error_message = ""
            self._driver_status_message = ""
            self._driver_info_message = ""
            self._driver_error_message = ""
            self._driver_action_running = False
            self._vb_cable_test_running = False
            self._vb_cable_test_status = "idle"
            self._vb_cable_test_message = ""
            self._vb_cable_bridge_recovery_needed = False
            self._bridge_diagnostics_refresh_pending = False
            self._last_bridge_connected = bool(
                self._settings_controller.bridgeConnected
            )
            self._diagnosticsReady.connect(self._on_diagnostics_ready)
            self._vbCableTestReady.connect(self._on_vb_cable_test_ready)
            self._detectedEndpointReady.connect(
                self._on_detected_endpoint_ready
            )
            self._settings_controller.endpointOptionsChanged.connect(
                self._invalidate_vb_cable_test_result
            )
            self._settings_controller.selectedEndpointIndexChanged.connect(
                self._invalidate_vb_cable_test_result
            )
            self._settings_controller.bridgeConnectedChanged.connect(
                self._on_bridge_connected_changed
            )
            if auto_refresh:
                self.refreshDiagnostics()

        # -- internal helpers -------------------------------------------------

        def _saved_output_endpoint(self):
            saved_config = config.load_config(config.config_path(self._config_root))
            return (
                saved_config.get("output_endpoint_name", ""),
                saved_config.get("output_endpoint_host_api", ""),
            )

        def _set_driver_status(self, text: str) -> None:
            """Genuine, completed success only (e.g. an endpoint was
            actually selected and persisted) - the only message shown in
            the success/green color. See ``_set_driver_info`` for outcomes
            that are merely informational (XRBM-031 RETRY 1 item 7).
            """

            self._driver_status_message = text
            self._driver_info_message = ""
            self._driver_error_message = ""
            self.driverStatusMessageChanged.emit()
            self.driverInfoMessageChanged.emit()
            self.driverErrorMessageChanged.emit()

        def _set_driver_info(self, text: str) -> None:
            """Neutral, informational outcome - never the success/green
            color: a UAC cancellation installed nothing, and a launched
            vendor setup UI is not yet a confirmed install either (XRBM-031
            RETRY 1 item 7 - "still never say installed until endpoint
            recheck"). QML renders this in a neutral tone, distinct from
            both a real success and a real error.
            """

            self._driver_info_message = text
            self._driver_status_message = ""
            self._driver_error_message = ""
            self.driverInfoMessageChanged.emit()
            self.driverStatusMessageChanged.emit()
            self.driverErrorMessageChanged.emit()

        def _set_driver_error(self, text: str) -> None:
            self._driver_error_message = text
            self._driver_status_message = ""
            self._driver_info_message = ""
            self.driverErrorMessageChanged.emit()
            self.driverStatusMessageChanged.emit()
            self.driverInfoMessageChanged.emit()

        def _set_driver_action_running(self, value: bool) -> None:
            value = bool(value)
            if value:
                _driver_action_active_event.set()
            else:
                _driver_action_active_event.clear()
            if value == self._driver_action_running:
                if not value:
                    self._settings_controller._schedule_application_exit_poll()
                return
            self._driver_action_running = value
            self.driverActionRunningChanged.emit()
            if not value:
                self._settings_controller._schedule_application_exit_poll()

        def _set_vb_cable_test_state(
            self, status: str, message: str, *, running: bool
        ) -> None:
            self._vb_cable_test_status = status
            self._vb_cable_test_message = message
            self._vb_cable_test_running = running
            self.vbCableTestChanged.emit()
            if not running and self._bridge_diagnostics_refresh_pending:
                QTimer.singleShot(
                    0, self._try_pending_bridge_diagnostics_refresh
                )

        def _set_vb_cable_bridge_recovery_needed(self, value: bool) -> None:
            value = bool(value)
            if value == self._vb_cable_bridge_recovery_needed:
                return
            self._vb_cable_bridge_recovery_needed = value
            self.vbCableBridgeRecoveryChanged.emit()

        def _invalidate_vb_cable_test_result(self) -> None:
            if self._vb_cable_test_running:
                return
            if self._vb_cable_test_status != "idle" or self._vb_cable_test_message:
                self._set_vb_cable_test_state("idle", "", running=False)

        def _on_bridge_connected_changed(self) -> None:
            connected = bool(self._settings_controller.bridgeConnected)
            transitioned_to_connected = connected and not self._last_bridge_connected
            self._last_bridge_connected = connected
            if not connected:
                self._bridge_diagnostics_refresh_pending = False
                return
            if not transitioned_to_connected:
                return
            self._bridge_diagnostics_refresh_pending = True
            QTimer.singleShot(0, self._try_pending_bridge_diagnostics_refresh)

        def _try_pending_bridge_diagnostics_refresh(self) -> None:
            if not self._bridge_diagnostics_refresh_pending:
                return
            if not self._settings_controller.bridgeConnected:
                self._bridge_diagnostics_refresh_pending = False
                return
            if (
                self._is_refreshing
                or self._vb_cable_test_running
                or _diagnostics_shutdown_event.is_set()
            ):
                return
            self._bridge_diagnostics_refresh_pending = False
            self.refreshDiagnostics()

        def _on_diagnostics_ready(self, report) -> None:
            """Delivered (cross-thread) once ``run_diagnostics()`` returns -
            or, if the background thread's own call raised something
            ``windows_diagnostics.run_diagnostics()``'s own per-check
            isolation did not anticipate (should be exceedingly rare now
            that every individual check is isolated - see
            ``windows_diagnostics._isolated()`` - but still possible, e.g.
            if constructing the report itself somehow failed), ``None``.

            XRBM-031 RETRY 1 item 2: a ``None`` report must never leave the
            PREVIOUS run's rows on screen looking current - stale green/red
            rows next to a page that silently failed to refresh would be
            actively misleading. The rows are cleared and a page-level
            error is shown instead, prominently, not only as a driver-card
            message below the fold.
            """

            if report is not None:
                self._check_rows = [_diagnostics_check_to_row(c) for c in report.checks]
                self._diagnostics_error_message = ""
            else:
                self._check_rows = []
                self._diagnostics_error_message = "检测失败；请重新检测"
            self._is_refreshing = False
            self.checkResultsChanged.emit()
            self.diagnosticsErrorMessageChanged.emit()
            self.isRefreshingChanged.emit()
            if self._bridge_diagnostics_refresh_pending:
                QTimer.singleShot(
                    0, self._try_pending_bridge_diagnostics_refresh
                )

        def _on_vb_cable_test_ready(self, result) -> None:
            if result is None:
                self._set_vb_cable_bridge_recovery_needed(False)
                self._set_vb_cable_test_state(
                    "fail",
                    "通道测试失败；未得到可信结果",
                    running=False,
                )
                return
            if isinstance(result, _VbCableTestWorkflowResult):
                if result.stop_error:
                    self._set_vb_cable_bridge_recovery_needed(False)
                    self._set_vb_cable_test_state(
                        "fail",
                        result.stop_error,
                        running=False,
                    )
                    self._settings_controller._refresh_bridge_status()
                    return
                if result.restart_skipped_for_exit:
                    self._set_vb_cable_bridge_recovery_needed(False)
                    self._set_vb_cable_test_state(
                        "unsupported",
                        "完全退出中，声音通道测试已停止，服务不再恢复",
                        running=False,
                    )
                    self._settings_controller._refresh_bridge_status()
                    return
                loopback_result = result.loopback_result
                restart_result = result.restart_result
                restart_ok = bool(
                    restart_result is not None
                    and restart_result.outcome
                    in {
                        bridge_launcher.LaunchOutcome.STARTED,
                        bridge_launcher.LaunchOutcome.ALREADY_RUNNING,
                    }
                )
                if result.bridge_was_running and not restart_ok:
                    self._set_vb_cable_bridge_recovery_needed(True)
                    recovery_text = (
                        settings_ui.describe_launch_result(restart_result)
                        if restart_result is not None
                        else "未得到启动结果"
                    )
                    loopback_text = (
                        loopback_result.detail
                        if loopback_result is not None
                        else "声音通道测试未得到可信结果"
                    )
                    self._set_vb_cable_test_state(
                        "fail",
                        f"{loopback_text} 遥控器服务未能自动恢复：{recovery_text}",
                        running=False,
                    )
                    self._settings_controller._refresh_bridge_status()
                    return
                self._set_vb_cable_bridge_recovery_needed(False)
                if loopback_result is None:
                    self._set_vb_cable_test_state(
                        "fail",
                        "声音通道测试失败；未得到可信结果"
                        + (
                            "；服务已自动恢复"
                            if result.bridge_was_running else ""
                        ),
                        running=False,
                    )
                else:
                    suffix = (
                        "；服务已自动恢复"
                        if result.bridge_was_running else ""
                    )
                    self._set_vb_cable_test_state(
                        loopback_result.status.value,
                        loopback_result.detail + suffix,
                        running=False,
                    )
                self._settings_controller._refresh_bridge_status()
                return
            self._set_vb_cable_bridge_recovery_needed(False)
            self._set_vb_cable_test_state(
                result.status.value,
                result.detail,
                running=False,
            )

        # -- properties ---------------------------------------------------

        def _get_check_results(self) -> List[dict]:
            return list(self._check_rows)

        checkResults = Property(list, _get_check_results, notify=checkResultsChanged)

        def _get_is_refreshing(self) -> bool:
            return self._is_refreshing

        isRefreshing = Property(bool, _get_is_refreshing, notify=isRefreshingChanged)

        def _get_diagnostics_error_message(self) -> str:
            return self._diagnostics_error_message

        diagnosticsErrorMessage = Property(
            str, _get_diagnostics_error_message, notify=diagnosticsErrorMessageChanged
        )

        def _get_driver_status_message(self) -> str:
            return self._driver_status_message

        driverStatusMessage = Property(
            str, _get_driver_status_message, notify=driverStatusMessageChanged
        )

        def _get_driver_info_message(self) -> str:
            return self._driver_info_message

        driverInfoMessage = Property(
            str, _get_driver_info_message, notify=driverInfoMessageChanged
        )

        def _get_driver_error_message(self) -> str:
            return self._driver_error_message

        driverErrorMessage = Property(
            str, _get_driver_error_message, notify=driverErrorMessageChanged
        )

        driverActionRunning = Property(
            bool,
            lambda self: self._driver_action_running,
            notify=driverActionRunningChanged,
        )

        def _get_vb_cable_test_running(self) -> bool:
            return self._vb_cable_test_running

        vbCableTestRunning = Property(
            bool, _get_vb_cable_test_running, notify=vbCableTestChanged
        )

        def _get_vb_cable_test_status(self) -> str:
            return self._vb_cable_test_status

        vbCableTestStatus = Property(
            str, _get_vb_cable_test_status, notify=vbCableTestChanged
        )

        def _get_vb_cable_test_message(self) -> str:
            return self._vb_cable_test_message

        vbCableTestMessage = Property(
            str, _get_vb_cable_test_message, notify=vbCableTestChanged
        )

        def _get_vb_cable_bridge_recovery_needed(self) -> bool:
            return self._vb_cable_bridge_recovery_needed

        vbCableBridgeRecoveryNeeded = Property(
            bool,
            _get_vb_cable_bridge_recovery_needed,
            notify=vbCableBridgeRecoveryChanged,
        )

        # -- slots ----------------------------------------------------------

        def _emit_diagnostics_ready(self, report) -> None:
            """Thin wrapper around emitting ``_diagnosticsReady``, isolated
            into its own method purely so a test can inject a failure here
            (simulating a receiver/Qt runtime that is already mid-teardown)
            without needing to monkeypatch PySide6's own ``Signal``
            machinery directly. Never called if
            ``_diagnostics_shutdown_event`` is already set when the worker
            checks it - see ``refreshDiagnostics()``'s ``_run_in_background``
            below.
            """

            self._diagnosticsReady.emit(report)

        def _emit_vb_cable_test_ready(self, result) -> None:
            self._vbCableTestReady.emit(result)

        @Slot()
        def startInitialDiagnostics(self) -> None:
            if self._check_rows or self._is_refreshing:
                return
            self.refreshDiagnostics()

        @Slot()
        def refreshDiagnostics(self) -> None:
            """Runs every check on a background thread. Repeated clicks
            while a check is already running are ignored outright (the
            guard below), so this can never start two overlapping workers -
            see the module docstring for the full thread-safety/lifecycle
            contract.

            Also refuses to start once process shutdown has begun
            (``_diagnostics_shutdown_event`` set - XRBM-031 RETRY 2): there
            would be nothing left alive to usefully receive this worker's
            result anyway, and starting one so late would only add another
            thread ``_shutdown_qt_settings_app_at_exit()``'s bounded join
            might not have time for.
            """

            if self._is_refreshing or self._vb_cable_test_running:
                return
            if _diagnostics_shutdown_event.is_set():
                return
            self._invalidate_vb_cable_test_result()
            self._is_refreshing = True
            self.isRefreshingChanged.emit()
            saved_name, saved_host_api = self._saved_output_endpoint()

            def _run_in_background() -> None:
                try:
                    try:
                        report = windows_diagnostics.run_diagnostics(
                            saved_output_name=saved_name,
                            saved_output_host_api=saved_host_api,
                            # XRBM-035: the SAME event this module's
                            # shutdown helpers set - a discovery attempt
                            # still in flight when the settings window
                            # starts closing is now actually cancelled at
                            # the WinRT level (see
                            # windows_diagnostics._discover_candidates_
                            # cancellable()), not merely abandoned to run
                            # concurrently with interpreter shutdown.
                            cancel_event=_diagnostics_shutdown_event,
                        )
                    except Exception:  # noqa: BLE001 - never crash the worker thread
                        report = None
                    if _diagnostics_shutdown_event.is_set():
                        # Process shutdown began while this check was
                        # running (XRBM-031 RETRY 2) - the
                        # DiagnosticsController/Qt runtime this would emit
                        # into may already be mid-teardown by now. Skip the
                        # emit entirely rather than race it.
                        return
                    try:
                        self._emit_diagnostics_ready(report)
                    except Exception:  # noqa: BLE001 - a receiver/Qt runtime
                        # that is ALREADY tearing down despite the check
                        # just above (an inherent, disclosed narrowing-not-
                        # elimination of the race - see module docstring)
                        # must never crash this background thread; the
                        # registry cleanup below still runs regardless.
                        pass
                finally:
                    _forget_diagnostics_thread(threading.current_thread())

            thread = threading.Thread(target=_run_in_background, daemon=True)
            _remember_diagnostics_thread(thread)
            thread.start()

        @Slot()
        def testVbCableChannel(self) -> None:
            """Runs the active CABLE Input -> CABLE Output test on demand."""

            self._start_vb_cable_test(allow_bridge_restart=False)

        @Slot()
        def testVbCableChannelWithBridgeRestart(self) -> None:
            """Temporarily stop a running bridge, test, then restore it."""

            self._start_vb_cable_test(allow_bridge_restart=True)

        def _start_vb_cable_test(self, *, allow_bridge_restart: bool) -> None:

            if self._vb_cable_test_running or self._is_refreshing:
                return
            if _diagnostics_shutdown_event.is_set():
                return
            if (
                self._settings_controller._application_exit_requested
                or self._settings_controller._application_exit_confirmed
                or self._settings_controller._application_exit_intent.is_set()
            ):
                return
            if self._driver_action_running:
                self._set_vb_cable_test_state(
                    "fail",
                    "输出端点正在处理；完成后再测试声音通道",
                    running=False,
                )
                return
            if self._settings_controller._get_voice_hotkey_busy():
                self._set_vb_cable_test_state(
                    "fail",
                    "语音快捷键正在处理；完成后再测试声音通道",
                    running=False,
                )
                return
            if self._settings_controller._get_input_capture_in_use():
                self._set_vb_cable_test_state(
                    "fail",
                    "按键录入或检测正在进行；结束后再测试声音通道",
                    running=False,
                )
                return
            if self._settings_controller._endpoint_preflight_busy:
                self._set_vb_cable_test_state(
                    "fail",
                    "输出端点正在检查；完成后再测试声音通道",
                    running=False,
                )
                return
            if self._settings_controller._get_bridge_launch_busy():
                self._set_vb_cable_test_state(
                    "fail",
                    "服务正在启动；停止后再测试声音通道",
                    running=False,
                )
                return
            bridge_running = self._settings_controller._refresh_bridge_status()
            if bridge_running is None:
                self._set_vb_cable_test_state(
                    "unsupported",
                    "无法确认服务状态；为避免混入真实语音，未启动测试",
                    running=False,
                )
                return
            if bridge_running:
                if not allow_bridge_restart:
                    self._set_vb_cable_test_state(
                        "fail",
                        "服务正在运行；需先确认临时停止并自动恢复",
                        running=False,
                    )
                    return

            saved_name, saved_host_api = self._saved_output_endpoint()
            _vb_cable_test_active_event.set()
            self._set_vb_cable_bridge_recovery_needed(False)
            self._set_vb_cable_test_state(
                "running",
                (
                    "正在停止服务；随后测试并自动恢复"
                    if bridge_running
                    else "正在发送测试信号；检查 CABLE Output"
                ),
                running=True,
            )

            def _run_in_background() -> None:
                try:
                    stop_error = ""
                    loopback_result = None
                    restart_result = None
                    bridge_stopped = False
                    restart_skipped_for_exit = False
                    if bridge_running:
                        stop_result = bridge_control_windows.request_bridge_exit()
                        if not stop_result.stopped:
                            stop_error = stop_result.error or (
                                "未能临时停止服务；未运行声音通道测试"
                            )
                        else:
                            bridge_stopped = True
                    try:
                        if (
                            not stop_error
                            and not _diagnostics_shutdown_event.is_set()
                            and not self._settings_controller._application_exit_intent.is_set()
                        ):
                            loopback_result = (
                                windows_diagnostics.check_vb_cable_loopback_isolated(
                                    saved_name,
                                    saved_host_api,
                                    cancel_event=_AnyEvent(
                                        _diagnostics_shutdown_event,
                                        self._settings_controller._application_exit_intent,
                                    ),
                                )
                            )
                    except Exception:  # noqa: BLE001 - never crash the worker thread
                        loopback_result = None
                    finally:
                        if bridge_stopped and (
                            not self._settings_controller._application_exit_intent.is_set()
                            and not _diagnostics_shutdown_event.is_set()
                        ):
                            try:
                                restart_result = bridge_launcher.launch_bridge()
                            except bridge_launcher.BridgeLaunchConfigurationError as exc:
                                restart_result = bridge_launcher.LaunchResult(
                                    outcome=bridge_launcher.LaunchOutcome.LAUNCH_FAILED,
                                    command=(),
                                    error=type(exc).__name__,
                                )
                            except Exception as exc:  # noqa: BLE001 - report recovery failure
                                restart_result = bridge_launcher.LaunchResult(
                                    outcome=bridge_launcher.LaunchOutcome.LAUNCH_FAILED,
                                    command=(),
                                    error=type(exc).__name__,
                                )
                        elif bridge_stopped:
                            restart_skipped_for_exit = True
                    result = _VbCableTestWorkflowResult(
                        loopback_result=loopback_result,
                        bridge_was_running=bool(bridge_running),
                        stop_error=stop_error,
                        restart_result=restart_result,
                        restart_skipped_for_exit=restart_skipped_for_exit,
                    )
                    _vb_cable_test_active_event.clear()
                    if _diagnostics_shutdown_event.is_set():
                        return
                    try:
                        self._emit_vb_cable_test_ready(result)
                    except Exception:  # noqa: BLE001 - Qt may already be tearing down
                        pass
                finally:
                    _vb_cable_test_active_event.clear()
                    _forget_diagnostics_thread(threading.current_thread())

            thread = threading.Thread(target=_run_in_background, daemon=True)
            _remember_diagnostics_thread(thread)
            try:
                thread.start()
            except Exception:
                _forget_diagnostics_thread(thread)
                _vb_cable_test_active_event.clear()
                self._set_vb_cable_test_state(
                    "fail",
                    "无法启动 VB-CABLE 通道测试后台任务。",
                    running=False,
                )

        @Slot(result=bool)
        def selectDetectedCableInputAsOutput(self) -> bool:
            """Detect and save CABLE Input without blocking the Qt thread."""

            if (
                self._vb_cable_test_running
                or self._driver_action_running
                or self._settings_controller._settings_save_busy
                or self._settings_controller._endpoint_preflight_busy
                or self._settings_controller._get_bridge_launch_busy()
                or self._settings_controller._get_voice_hotkey_busy()
                or _diagnostics_shutdown_event.is_set()
                or self._settings_controller._application_exit_requested
                or self._settings_controller._application_exit_intent.is_set()
                or self._settings_controller._application_exit_confirmed
            ):
                return False
            self._invalidate_vb_cable_test_result()
            self._set_driver_action_running(True)
            self._set_driver_info("正在检测并检查 CABLE Input…")

            def run() -> None:
                try:
                    endpoints = audio_output.enumerate_output_endpoints()
                    matches = [
                        endpoint
                        for endpoint in endpoints
                        if audio_output.is_cable_input_endpoint(endpoint.name)
                    ]
                    if not matches:
                        payload = (
                            None,
                            "未找到 CABLE Input；请安装 VB-CABLE 并重启电脑",
                        )
                    else:
                        try:
                            endpoint = audio_output.select_preferred_output_endpoint(
                                matches
                            )
                        except audio_output.AudioOutputUnavailableError:
                            payload = (
                                None,
                                f"找到 {len(matches)} 个 CABLE Input；"
                                "请在语音页的“输出端点”中选择",
                            )
                        else:
                            payload = (endpoint, "")
                except audio_output.AudioOutputUnavailableError:
                    payload = (None, "无法检测播放端点")
                except Exception:  # noqa: BLE001 - keep UI text sanitized
                    payload = (None, "播放端点检测失败")
                emitted = self._settings_controller._emit_background_result(
                    self._detectedEndpointReady,
                    payload,
                )
                if not emitted:
                    _driver_action_active_event.clear()

            try:
                self._settings_controller._start_background_task(
                    run, "detect-cable-output-endpoint"
                )
            except Exception:
                self._set_driver_action_running(False)
                self._set_driver_error("无法启动播放端点检测")
                return False
            return True

        def _on_detected_endpoint_ready(self, payload: object) -> None:
            if (
                _diagnostics_shutdown_event.is_set()
                or self._settings_controller._application_exit_intent.is_set()
                or self._settings_controller._application_exit_confirmed
            ):
                self._set_driver_action_running(False)
                return
            endpoint, message = payload
            if endpoint is None:
                self._set_driver_action_running(False)
                self._set_driver_error(str(message))
                return

            def finished(ok: bool, error: str) -> None:
                self._set_driver_action_running(False)
                if not ok:
                    self._set_driver_error(
                        "输出端点保存失败；"
                        + (error or "请在语音页重新选择")
                    )
                    return
                self._set_vb_cable_test_state("idle", "", running=False)
                self._set_driver_status(f"已保存输出端点：{endpoint.name}")

            try:
                accepted = self._settings_controller._select_and_persist_output_endpoint(
                    endpoint.name,
                    endpoint.host_api,
                    finished,
                    allow_driver_action=True,
                )
            except Exception:  # noqa: BLE001 - keep the async slot retryable
                accepted = False
            if not accepted and self._driver_action_running:
                self._set_driver_action_running(False)
                self._set_driver_error(
                    "输出端点保存失败；请在语音页重新选择"
                )

        @Slot()
        def launchVbCableSetup(self) -> None:
            """Launches the bundled VB-CABLE vendor setup UI with UAC. Only
            ever reached from a slot the QML page calls after its OWN
            explicit confirmation dialog (see DiagnosticsPage.qml) - never
            automatically on page load/refresh. Never reports installation
            as successful merely because the process launched; see
            vb_cable_bundle.py's module docstring for the full contract.
            """

            self._set_vb_cable_test_state("idle", "", running=False)
            try:
                vb_cable_bundle.prepare_and_launch_vendor_setup()
            except vb_cable_bundle.BundleNotFoundError as exc:
                self._set_driver_error(f"未找到随包的 VB-CABLE 安装包：{exc}")
            except vb_cable_bundle.UacCancelledError as exc:
                # Neutral/informational, never the success color (XRBM-031
                # RETRY 1 item 7): nothing was installed.
                self._set_driver_info(str(exc))
            except vb_cable_bundle.VbCableBundleError as exc:
                self._set_driver_error(f"启动 VB-CABLE 安装程序失败：{exc}")
            else:
                # Also neutral/informational, not the success color: only
                # that the vendor UI was launched, never a confirmed
                # install - that is only ever established later, by a
                # diagnostics recheck finding both endpoints present.
                self._set_driver_info(
                    "已启动 VB-CABLE 安装程序；完成安装并重启电脑后，请重新检查"
                )

    _qt_classes_cache = {
        "QGuiApplication": QGuiApplication,
        "QIcon": QIcon,
        "QQmlApplicationEngine": QQmlApplicationEngine,
        "QQuickStyle": QQuickStyle,
        "QUrl": QUrl,
        "Qt": Qt,
        "qmlRegisterSingletonInstance": qmlRegisterSingletonInstance,
        "ButtonMappingModel": ButtonMappingModel,
        "SettingsController": SettingsController,
        "DiagnosticsController": DiagnosticsController,
    }
    return _qt_classes_cache


# QML module/type names for the two QML singletons below - never
# "controller"/"model" (see run_settings_window()'s registration call for
# why).
_QML_MODULE_URI = "OvbRc003Settings"
_QML_CONTROLLER_TYPE_NAME = "SettingsController"
_QML_MAPPING_MODEL_TYPE_NAME = "ButtonMappingModel"
_QML_DIAGNOSTICS_TYPE_NAME = "DiagnosticsController"  # XRBM-031


def run_settings_window(*, start_hidden: bool = False) -> int:
    """Builds and runs the Qt Quick/QML settings window. Blocks until the
    window is closed (``QGuiApplication.exec()``), then returns its exit
    code. Raises ``QtUnavailableError`` (via ``_load_qt_classes()``) if
    PySide6-Essentials is not installed, or if ``main.qml`` fails to load at
    all (e.g. a corrupted/incomplete frozen build missing its bundled qml/
    directory) - never silently opens a blank/broken window.
    """

    classes = _load_qt_classes()
    QGuiApplication = classes["QGuiApplication"]
    QIcon = classes["QIcon"]
    QQmlApplicationEngine = classes["QQmlApplicationEngine"]
    QQuickStyle = classes["QQuickStyle"]
    QUrl = classes["QUrl"]
    qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
    ButtonMappingModel = classes["ButtonMappingModel"]
    SettingsController = classes["SettingsController"]
    DiagnosticsController = classes["DiagnosticsController"]

    # Windows 11 Fluent look (In-scope item 7/DESIGN_VARIANCE): QQuickStyle
    # must be set before the QGuiApplication/engine is constructed. Qt 6.7+
    # ships "FluentWinUI3" specifically to emulate the Windows 11 Fluent
    # design language for Qt Quick Controls; it renders (as a software
    # fallback, not native WinUI3) on non-Windows hosts too, which is what
    # this candidate's offscreen render/screenshot step below relies on.
    QQuickStyle.setStyle("FluentWinUI3")

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    _apply_application_identity(app)
    set_quit_on_last_window_closed = getattr(
        app, "setQuitOnLastWindowClosed", None
    )
    if callable(set_quit_on_last_window_closed):
        set_quit_on_last_window_closed(False)

    model = ButtonMappingModel()
    controller = SettingsController(model, start_hidden=start_hidden)
    _connect_application_exit(app, controller)

    root_window = None

    def update_application_icon() -> None:
        path = resources.find_app_icon(controller._tray_icon_state())
        if path is not None:
            _apply_application_icon(app, root_window, QIcon, path)

    update_application_icon()
    controller.trayStateChanged.connect(update_application_icon)
    diagnostics_controller = DiagnosticsController(
        controller,
        config.config_root(),
        auto_refresh=False,
    )

    # SettingsController has already started its endpoint/program status
    # workers here. Diagnostics are intentionally deferred until QML reports
    # its first frame, but may also exist before app.exec() returns. Every exit
    # path from this point therefore shares the same cleanup chain, including
    # engine.load() failures and an empty rootObjects() result.
    try:
        # Exposed to QML as SINGLETONS (resolved through the type/import
        # system at document-compile time), not as engine.rootContext()
        # context properties: a root-context property is resolved
        # dynamically through each QML object's context chain, and -
        # empirically, reproduced with a minimal isolated repro during this
        # task - a context property can observe a transient/incorrectly-null
        # value the first time it is read from a binding evaluated during a
        # child component's own construction (e.g. a ListView's
        # currentIndex binding, or any property evaluated inside a
        # ScrollView's deferred content), before every containing component
        # has finished having its own externally-supplied properties
        # assigned. A qmlRegisterSingletonInstance()-registered type has no
        # such hazard: every file that `import`s this module gets the exact
        # same already-fully-constructed instance immediately, with no
        # per-context propagation/ordering involved at all.
        qmlRegisterSingletonInstance(
            SettingsController,
            _QML_MODULE_URI,
            1,
            0,
            _QML_CONTROLLER_TYPE_NAME,
            controller,
        )
        qmlRegisterSingletonInstance(
            ButtonMappingModel,
            _QML_MODULE_URI,
            1,
            0,
            _QML_MAPPING_MODEL_TYPE_NAME,
            model,
        )
        qmlRegisterSingletonInstance(
            DiagnosticsController,
            _QML_MODULE_URI,
            1,
            0,
            _QML_DIAGNOSTICS_TYPE_NAME,
            diagnostics_controller,
        )

        engine = QQmlApplicationEngine()
        qml_dir = _qml_directory()
        engine.addImportPath(str(qml_dir))

        main_qml = qml_dir / "main.qml"
        engine.load(QUrl.fromLocalFile(str(main_qml)))
        root_objects = engine.rootObjects()
        if not root_objects:
            raise QtUnavailableError(f"无法加载 QML 设置界面：{main_qml} 未能成功加载。")
        root_window = root_objects[0]
        update_application_icon()
        window_chrome_windows.apply_settings_window_chrome(root_window)
        _mark_settings_window_for_activation(root_window)

        return app.exec()
    finally:
        try:
            controller.shutdownForProcessExit()
        finally:
            try:
                # XRBM-035: called HERE, synchronously - whether app.exec()
                # returned normally, engine.load() raised, rootObjects()
                # was empty, input cleanup raised, or anything else in this
                # block raised. Independent cleanup steps cannot skip the
                # diagnostics-worker shutdown contract.
                _shutdown_diagnostics_workers()
            finally:
                audio_playback.cleanup_retained_portaudio_test_resources(
                    blocking=False
                )
