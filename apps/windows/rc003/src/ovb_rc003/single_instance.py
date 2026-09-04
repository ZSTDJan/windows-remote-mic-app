"""Windows named-mutex guards for the desktop app and bridge ownership.

The settings mutex is now the product-wide application mutex: one desktop
process owns the Qt window, notification area, bridge worker and element
navigation runtime. A duplicate launch restores that process instead of
creating another one. The older bridge mutex remains around the in-process
worker so a legacy standalone bridge from an earlier build cannot race it for
BLE, Raw Input, synthetic-key or audio resources during upgrade.

The product mutex intentionally stays stable across versions and locations.
A second mutex hashes the current version plus executable location so a visible
launch can distinguish the same copy (restore it) from a different/older copy
(offer a safe handoff). A fixed handoff mutex allows only one such waiter in a
Windows logon session. Handoff exit requests carry an owner token and the new
copy starts only after its own pending request is consumed or safely removed.

Fail-closed contract (XRBM-021 review round 1 P1 #1): a caller that cannot
PROVE it is the sole owner - whether because another instance already owns
the mutex (``DuplicateInstanceError``) or because the Win32 API itself
could not be used to check at all (``SingleInstanceUnavailableError``) -
must never fall through to starting protected resources. ``__main__.py``
applies this to the one desktop application process, while
``bridge_launcher.py`` applies it to the in-process bridge worker and the
legacy bridge-compatibility mutex.

Ctypes ABI: every Win32 call here (``CreateMutexW``, ``ReleaseMutex``,
``CloseHandle``, ``MessageBoxW``) declares an explicit ``argtypes``/
``restype`` using ``ctypes.wintypes`` before it is ever invoked - the same
"never leave a handle/pointer-sized argument at ctypes' unprototyped
default" rule XRBM-019 established for ``PostMessageW`` (see
raw_input_windows.py) and XRBM-020 established for ``SendInput``'s
INPUT-array pointer (see win32_input.py). ``wintypes.HANDLE`` is
pointer-sized on every host (backed by ``c_void_p``), so it is never
truncated on 64-bit Windows the way an unprototyped bare ``int`` argument
would be.

Last-error capture (XRBM-021 review round 1 P1 #2): Win32's last-error
value is thread-local state that ANY subsequent Win32 call can overwrite -
so it must be read immediately adjacent to ``CreateMutexW`` itself, inside
the same ``_real_create_mutex()`` call, never via a separate later call.
``_real_create_mutex()`` therefore uses a ``ctypes.WinDLL(...,
use_last_error=True)`` handle (ctypes caches the error value itself, right
after the call returns) and returns the handle and that error code together
as one ``MutexCreationResult`` - there is no standalone, independently
callable "get the last error now" function for callers to accidentally
call too late.

Testability: the real Win32 calls are isolated in module-level
``_real_*`` functions, each individually injectable via
``BridgeInstanceGuard``'s keyword-only ``_create_mutex``/``_release_mutex``/
``_close_handle`` parameters - the same dependency-injection seam
``win32_input.py``'s ``_sender`` parameter already established, letting
tests/test_single_instance.py exercise the full acquire/duplicate/release/
cleanup contract deterministically on any OS without ``ctypes.windll``
(which does not exist off Windows). Only the ``_real_*`` functions
themselves call ``_require_windows()`` - the guard class never does - so an
injected fake never has to fight a platform gate that has nothing to do
with the logic under test.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

from . import __version__, product_identity

# Local\ (not Global\) scopes the mutex to the current Terminal Services /
# Windows logon session. The settings-named mutex is retained as the
# product-wide application identity for upgrade compatibility; the
# bridge-named mutex protects hardware ownership against an older standalone
# bridge from the same session.
_MUTEX_NAME = r"Local\RemoteMicRC003_BridgeInstance"
_SETTINGS_MUTEX_NAME = r"Local\RemoteMicRC003_SettingsInstance"
_ELEMENT_NAVIGATION_MUTEX_NAME = r"Local\RemoteMicRC003_ElementNavigationInstance"
_APPLICATION_RUNTIME_MUTEX_PREFIX = r"Local\RemoteMicRC003_Runtime_"
_APPLICATION_HANDOFF_MUTEX_NAME = r"Local\RemoteMicRC003_ApplicationHandoff"
_SETTINGS_WINDOW_PROPERTY = "RemoteMicRC003.SettingsWindow"
_BRIDGE_START_REQUEST_FILENAME = "bridge-start-request.json"
_BRIDGE_START_REQUEST_SCHEMA = 1
_BRIDGE_START_REQUEST_MAX_AGE_SECONDS = 30.0
_APPLICATION_EXIT_REQUEST_FILENAME = "application-exit-request.json"
_APPLICATION_EXIT_REQUEST_SCHEMA = 1
_APPLICATION_EXIT_REQUEST_MAX_AGE_SECONDS = 30.0

# https://learn.microsoft.com/windows/win32/debug/system-error-codes--0-499-
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5
_ERROR_FILE_NOT_FOUND = 2
_SYNCHRONIZE = 0x00100000

# Deterministic, documented nonzero exit codes (DoD 2's "deterministic
# nonzero exit"), distinct from Python's generic ``1`` (an unhandled
# exception) so they are unambiguous in logs/scripts, and from each other so
# a duplicate launch is distinguishable from a guard that could not be used
# at all, and from a cleanup-only failure on the way out.
DUPLICATE_INSTANCE_EXIT_CODE = 3
GUARD_UNAVAILABLE_EXIT_CODE = 4
CLEANUP_FAILED_EXIT_CODE = 5


class SingleInstanceUnavailableError(Exception):
    """Raised when the Win32 mutex API itself could not be used to prove
    single ownership: either this is not Windows, or ``CreateMutexW``
    failed for a reason OTHER than "already exists" (e.g. access denied).
    Distinguishable from ``DuplicateInstanceError`` for logging/diagnostic
    purposes only; callers fail closed instead of starting protected
    application or bridge resources without proven ownership.
    """


class DuplicateInstanceError(Exception):
    """Raised when another instance already owns the selected named mutex
    in this Windows logon session."""


class MutexCleanupError(RuntimeError):
    """Raised by ``BridgeInstanceGuard.__exit__`` whenever releasing/closing
    the mutex handle did not fully succeed (``ReleaseMutex``/
    ``CloseHandle`` returned FALSE, or either call itself raised) -
    unconditionally, even when the wrapped body ALSO raised (XRBM-021
    review round 1 correction: a cleanup failure that only surfaces when
    the body happened not to raise is still a silently-accepted failure the
    rest of the time). Cleanup (release then close, best-effort - one
    failing never skips the other) is always attempted first regardless.

    When the body already raised, this exception's ``__context__`` is that
    body exception - ordinary Python behavior for raising a new exception
    while another is propagating (`PEP 3134
    <https://peps.python.org/pep-3134/>`_), requiring no special handling
    here. A caller can always recover the original failure via
    ``mutex_cleanup_error.__context__`` - it is never discarded, only no
    longer the exception type that continues propagating.
    """


class MutexCreationResult(NamedTuple):
    """The handle and last-error code from ONE ``CreateMutexW`` call,
    captured together - see the module docstring's "Last-error capture"
    note for why these must never be split across two separate calls.
    """

    handle: int
    last_error: int


class MutexOpenResult(NamedTuple):
    handle: int
    last_error: int


CreateMutexFn = Callable[[str], MutexCreationResult]
ReleaseMutexFn = Callable[[int], bool]
CloseHandleFn = Callable[[int], bool]
OpenMutexFn = Callable[[str], MutexOpenResult]
SetWindowPropertyFn = Callable[[int, str], bool]
ActivateMarkedWindowFn = Callable[[str], bool]
ConfirmApplicationHandoffFn = Callable[[str, str], bool]


def application_runtime_mutex_name(
    *,
    executable_path: str | os.PathLike[str] | None = None,
    version: str | None = None,
) -> str:
    """Return the per-build-and-location identity for this desktop copy."""

    if executable_path is None:
        if getattr(sys, "frozen", False):
            runtime_path = Path(sys.executable)
        else:
            runtime_path = Path(__file__).resolve()
    else:
        runtime_path = Path(executable_path)
    try:
        runtime_path = runtime_path.resolve(strict=False)
    except OSError:
        runtime_path = runtime_path.absolute()
    normalized_path = str(runtime_path).replace("/", "\\").casefold()
    identity = f"{normalized_path}\0{str(version or __version__).strip()}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{_APPLICATION_RUNTIME_MUTEX_PREFIX}{digest}"


def application_runtime_window_property(
    *,
    executable_path: str | os.PathLike[str] | None = None,
    version: str | None = None,
) -> str:
    """Return the private HWND marker for this exact executable copy."""

    mutex_name = application_runtime_mutex_name(
        executable_path=executable_path,
        version=version,
    )
    digest = mutex_name.removeprefix(_APPLICATION_RUNTIME_MUTEX_PREFIX)
    return f"{_SETTINGS_WINDOW_PROPERTY}.{digest}"


def _require_windows() -> None:
    if sys.platform != "win32":
        raise SingleInstanceUnavailableError(
            "the single-instance Win32 helpers are only available on Windows"
        )


def _real_create_mutex(name: str) -> MutexCreationResult:
    """Calls the real ``CreateMutexW`` and captures ``GetLastError()``
    immediately afterward, via the same ``use_last_error=True`` WinDLL
    handle - see the module docstring. Returns handle 0 (NULL) on failure;
    never raises for that case, so ``BridgeInstanceGuard`` can distinguish
    "failed outright" from "succeeded but the object already existed" using
    only this one result.
    """

    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # HANDLE CreateMutexW(LPSECURITY_ATTRIBUTES, BOOL bInitialOwner, LPCWSTR lpName)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    raw_handle = kernel32.CreateMutexW(None, True, name)
    last_error = ctypes.get_last_error()
    handle = int(raw_handle) if raw_handle else 0
    return MutexCreationResult(handle=handle, last_error=last_error)


def _real_release_mutex(handle: int) -> bool:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    # BOOL ReleaseMutex(HANDLE hMutex)
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    return bool(kernel32.ReleaseMutex(handle))


def _real_open_mutex(name: str) -> MutexOpenResult:
    """Open the bridge mutex without acquiring or changing its ownership."""

    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    raw_handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    last_error = ctypes.get_last_error()
    return MutexOpenResult(
        handle=int(raw_handle) if raw_handle else 0,
        last_error=last_error,
    )


def _real_close_handle(handle: int) -> bool:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    # BOOL CloseHandle(HANDLE hObject)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return bool(kernel32.CloseHandle(handle))


def _real_set_window_property(hwnd: int, property_name: str) -> bool:
    """Mark the native settings HWND without duplicating its visible title."""

    _require_windows()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetPropW.argtypes = (
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.HANDLE,
    )
    user32.SetPropW.restype = wintypes.BOOL
    return bool(user32.SetPropW(hwnd, property_name, wintypes.HANDLE(1)))


def mark_settings_window(
    hwnd: int,
    *,
    _set_property: SetWindowPropertyFn = _real_set_window_property,
) -> bool:
    """Best-effort marker used only to find and reactivate the settings HWND.

    The named mutex remains the authoritative single-instance guard. A marker
    failure must not prevent the first settings window from opening.
    """

    if not hwnd:
        return False
    marked = False
    for property_name in (
        _SETTINGS_WINDOW_PROPERTY,
        application_runtime_window_property(),
    ):
        try:
            marked = bool(_set_property(hwnd, property_name)) or marked
        except Exception:
            continue
    return marked


def _real_activate_marked_window(property_name: str) -> bool:
    _require_windows()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_windows_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )
    user32.EnumWindows.argtypes = (enum_windows_proc, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
    user32.GetPropW.restype = wintypes.HANDLE
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.FlashWindow.argtypes = (wintypes.HWND, wintypes.BOOL)
    user32.FlashWindow.restype = wintypes.BOOL

    matches: List[int] = []

    @enum_windows_proc
    def visit(hwnd, _lparam):
        if user32.GetPropW(hwnd, property_name):
            matches.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(visit, 0)
    if not matches:
        return False

    hwnd = matches[0]
    _SW_SHOW = 5
    _SW_RESTORE = 9
    user32.ShowWindow(hwnd, _SW_RESTORE if user32.IsIconic(hwnd) else _SW_SHOW)
    user32.BringWindowToTop(hwnd)
    if not user32.SetForegroundWindow(hwnd):
        user32.FlashWindow(hwnd, True)
    return True


def activate_existing_settings_window(
    *,
    _activate: ActivateMarkedWindowFn = _real_activate_marked_window,
) -> bool:
    """Restore the already-running settings window after a duplicate launch."""

    try:
        return bool(_activate(_SETTINGS_WINDOW_PROPERTY))
    except Exception:
        return False


def activate_current_runtime_settings_window(
    *,
    _activate: ActivateMarkedWindowFn = _real_activate_marked_window,
) -> bool:
    """Restore only the window belonging to this version and location."""

    try:
        return bool(_activate(application_runtime_window_property()))
    except Exception:
        return False


def bridge_start_request_path(config_root: Path) -> Path:
    return Path(config_root) / _BRIDGE_START_REQUEST_FILENAME


def application_exit_request_path(config_root: Path) -> Path:
    return Path(config_root) / _APPLICATION_EXIT_REQUEST_FILENAME


def _write_request(
    path: Path,
    *,
    schema: int,
    action: str,
    now: Callable[[], float],
    request_id: str | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        payload = {
            "schema": schema,
            "action": action,
            "created_at": float(now()),
        }
        if request_id is not None:
            normalized_request_id = str(request_id).strip()
            if not normalized_request_id:
                raise ValueError("request_id must not be empty")
            payload["request_id"] = normalized_request_id
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def write_bridge_start_request(
    config_root: Path,
    *,
    now: Callable[[], float] = time.time,
) -> Path:
    """Atomically ask the existing desktop process to start its bridge."""

    return _write_request(
        bridge_start_request_path(config_root),
        schema=_BRIDGE_START_REQUEST_SCHEMA,
        action="start_bridge",
        now=now,
    )


def write_application_exit_request(
    config_root: Path,
    *,
    now: Callable[[], float] = time.time,
    request_id: str | None = None,
) -> Path:
    """Atomically ask the resident desktop process to exit completely."""

    return _write_request(
        application_exit_request_path(config_root),
        schema=_APPLICATION_EXIT_REQUEST_SCHEMA,
        action="exit_application",
        now=now,
        request_id=request_id,
    )


def owned_application_exit_request_pending(
    config_root: Path,
    request_id: str,
) -> bool:
    """Return whether the public request is still the handoff request we wrote."""

    path = application_exit_request_path(config_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("schema") == _APPLICATION_EXIT_REQUEST_SCHEMA
        and payload.get("action") == "exit_application"
        and payload.get("request_id") == str(request_id)
    )


def clear_owned_application_exit_request(
    config_root: Path,
    request_id: str,
) -> bool:
    """Atomically remove only the still-pending exit request we created."""

    path = application_exit_request_path(config_root)
    claimed = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.handoff-cleanup"
    )
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return True
    except OSError:
        return False

    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = None
    owned = bool(
        isinstance(payload, dict)
        and payload.get("schema") == _APPLICATION_EXIT_REQUEST_SCHEMA
        and payload.get("action") == "exit_application"
        and payload.get("request_id") == str(request_id)
    )
    if owned:
        try:
            claimed.unlink()
        except OSError:
            return False
        return True

    # Another writer replaced our request before cleanup. Restore that
    # request atomically only if the public path is still empty; otherwise
    # leave this claimed copy intact and fail closed rather than deleting
    # or overwriting either writer's request.
    try:
        os.link(claimed, path)
    except OSError:
        return False
    try:
        claimed.unlink()
    except OSError:
        return False
    return False


def _consume_request(
    path: Path,
    *,
    schema: int,
    action: str,
    now: Callable[[], float],
    max_age_seconds: float,
) -> bool:
    claimed = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.claimed")
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return False
    try:
        try:
            payload = json.loads(claimed.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        created_at = payload.get("created_at")
        if (
            payload.get("schema") != schema
            or payload.get("action") != action
            or not isinstance(created_at, (int, float))
            or isinstance(created_at, bool)
        ):
            return False
        age = float(now()) - float(created_at)
        return 0.0 <= age <= max(0.0, float(max_age_seconds))
    finally:
        try:
            claimed.unlink()
        except FileNotFoundError:
            pass


def consume_bridge_start_request(
    config_root: Path,
    *,
    now: Callable[[], float] = time.time,
    max_age_seconds: float = _BRIDGE_START_REQUEST_MAX_AGE_SECONDS,
) -> bool:
    """Claim and validate one request without deleting a newer replacement."""

    return _consume_request(
        bridge_start_request_path(config_root),
        schema=_BRIDGE_START_REQUEST_SCHEMA,
        action="start_bridge",
        now=now,
        max_age_seconds=max_age_seconds,
    )


def consume_application_exit_request(
    config_root: Path,
    *,
    now: Callable[[], float] = time.time,
    max_age_seconds: float = _APPLICATION_EXIT_REQUEST_MAX_AGE_SECONDS,
) -> bool:
    """Claim one fresh full-application exit request exactly once."""

    return _consume_request(
        application_exit_request_path(config_root),
        schema=_APPLICATION_EXIT_REQUEST_SCHEMA,
        action="exit_application",
        now=now,
        max_age_seconds=max_age_seconds,
    )


def _named_mutex_running(
    *,
    name: str,
    _open_mutex: OpenMutexFn = _real_open_mutex,
    _close_handle: CloseHandleFn = _real_close_handle,
) -> bool:
    """Return whether one named per-session mutex currently exists."""

    try:
        result = _open_mutex(name)
    except Exception as exc:  # noqa: BLE001 - fail closed on an unknown owner state
        raise SingleInstanceUnavailableError(
            "OpenMutexW raised an exception while checking bridge status"
        ) from exc
    if result.handle:
        try:
            closed = _close_handle(result.handle)
        except Exception as exc:
            raise MutexCleanupError(
                "mutex status probe cleanup did not fully succeed: "
                "CloseHandle raised an exception"
            ) from exc
        if not closed:
            raise MutexCleanupError(
                "mutex status probe cleanup did not fully succeed: "
                "CloseHandle returned FALSE"
            )
        return True
    # A protected object can deny SYNCHRONIZE access while still proving
    # that the named mutex exists in this logon session.
    if result.last_error == _ERROR_ACCESS_DENIED:
        return True
    if result.last_error == _ERROR_FILE_NOT_FOUND:
        return False
    raise SingleInstanceUnavailableError(
        f"OpenMutexW failed while checking instance status "
        f"(GetLastError={result.last_error})"
    )


def bridge_instance_running(
    *,
    name: str = _MUTEX_NAME,
    _open_mutex: OpenMutexFn = _real_open_mutex,
    _close_handle: CloseHandleFn = _real_close_handle,
) -> bool:
    """Return whether bridge mode already owns the per-session mutex."""

    return _named_mutex_running(
        name=name,
        _open_mutex=_open_mutex,
        _close_handle=_close_handle,
    )


def application_instance_running(
    *,
    name: str = _SETTINGS_MUTEX_NAME,
    _open_mutex: OpenMutexFn = _real_open_mutex,
    _close_handle: CloseHandleFn = _real_close_handle,
) -> bool:
    """Return whether the resident desktop application currently exists."""

    return _named_mutex_running(
        name=name,
        _open_mutex=_open_mutex,
        _close_handle=_close_handle,
    )


def element_navigation_instance_running(
    *,
    name: str = _ELEMENT_NAVIGATION_MUTEX_NAME,
    _open_mutex: OpenMutexFn = _real_open_mutex,
    _close_handle: CloseHandleFn = _real_close_handle,
) -> bool:
    return _named_mutex_running(
        name=name,
        _open_mutex=_open_mutex,
        _close_handle=_close_handle,
    )


class BridgeInstanceGuard:
    """Owns (or fails to own) the per-session bridge single-instance mutex.

    Use as a context manager around bridge-mode startup::

        with BridgeInstanceGuard():
            app.main()

    - First owner: ``__enter__`` returns normally; ``__exit__`` always
      attempts ``ReleaseMutex`` THEN ``CloseHandle`` (best-effort - one
      failing never skips the other), INCLUDING when the wrapped body
      raises (ordinary context-manager semantics: ``__exit__`` always runs
      on the way out of a ``with`` block - this is what gives the "release
      in finally" guarantee without an explicit ``try/finally`` here). A
      cleanup failure (FALSE return or a raised exception from either call)
      ALWAYS raises ``MutexCleanupError`` - even when the wrapped body
      itself also raised. This never discards the body's exception: Python
      chains it onto ``MutexCleanupError.__context__`` automatically (see
      that class's docstring), so both are observable rather than the
      cleanup failure being silently accepted whenever a body exception
      happened to already be in flight.
    - Duplicate owner: ``CreateMutexW`` still hands back a valid handle to
      the EXISTING object (Win32 semantics - the caller does not become the
      owner), so ``__enter__`` closes that handle immediately (never
      releases a mutex it never owned) and raises ``DuplicateInstanceError``
      before the ``with`` block's body ever runs - a close failure here is
      folded into that same exception's message rather than raised
      separately, since the duplicate signal is the primary, more
      actionable event. Note that ``__exit__`` is NOT invoked when
      ``__enter__`` raises (a plain Python ``with``-statement guarantee),
      which is exactly why the duplicate path must close its own handle
      right here rather than relying on ``__exit__``.
    - Acquisition failure (``CreateMutexW`` itself failed, e.g. access
      denied): ``__enter__`` raises ``SingleInstanceUnavailableError``.
      There is no handle to close in this case (a failed ``CreateMutexW``
      returns NULL).

    Every raised message deliberately omits the raw handle value (DoD 4:
    "no raw address/handle is persisted or logged") - only Win32 error
    codes (small integers, not addresses) and fixed diagnostic strings like
    "ReleaseMutex returned FALSE" ever appear in them.
    """

    def __init__(
        self,
        *,
        name: str = _MUTEX_NAME,
        _create_mutex: CreateMutexFn = _real_create_mutex,
        _release_mutex: ReleaseMutexFn = _real_release_mutex,
        _close_handle: CloseHandleFn = _real_close_handle,
        _duplicate_message: str = (
            f"{product_identity.DISPLAY_NAME}的遥控器服务已在当前 Windows 会话中运行"
        ),
        _access_denied_means_duplicate: bool = False,
    ) -> None:
        self._name = name
        self._create_mutex = _create_mutex
        self._release_mutex = _release_mutex
        self._close_handle = _close_handle
        self._duplicate_message = _duplicate_message
        self._access_denied_means_duplicate = _access_denied_means_duplicate
        self._handle: Optional[int] = None

    def __enter__(self) -> "BridgeInstanceGuard":
        result = self._create_mutex(self._name)
        if not result.handle:
            if (
                self._access_denied_means_duplicate
                and result.last_error == _ERROR_ACCESS_DENIED
            ):
                raise DuplicateInstanceError(self._duplicate_message)
            raise SingleInstanceUnavailableError(
                f"CreateMutexW failed (GetLastError={result.last_error})"
            )
        if result.last_error == _ERROR_ALREADY_EXISTS:
            close_failure = self._safe_close(result.handle)
            message = self._duplicate_message
            if close_failure:
                message += f" (additionally, {close_failure})"
            raise DuplicateInstanceError(message)
        self._handle = result.handle
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self._handle
        self._handle = None
        if not handle:
            return None

        failures: List[str] = []
        release_failure = self._safe_release(handle)
        if release_failure:
            failures.append(release_failure)
        # Always attempted, regardless of whether release itself raised or
        # returned FALSE - one cleanup step's failure must never skip the
        # other (XRBM-021 review round 1 P1 #3).
        close_failure = self._safe_close(handle)
        if close_failure:
            failures.append(close_failure)

        if failures:
            # Unconditional - even if the body (exc_type is not None) also
            # raised. Python automatically chains that body exception onto
            # __context__ (see MutexCleanupError's docstring) rather than
            # discarding it, so this never has to choose between the two;
            # a cleanup failure must never go silently accepted just
            # because a body exception happened to already be in flight.
            raise MutexCleanupError(
                "mutex cleanup did not fully succeed: " + "; ".join(failures)
            )
        return None

    def _safe_release(self, handle: int) -> Optional[str]:
        # Fixed diagnostic text only (DoD 4: "no raw address/handle is
        # persisted or logged") - the underlying exception's own message is
        # deliberately NEVER interpolated here, since it could itself
        # contain a raw handle/address (e.g. a WinError message quoting the
        # value ReleaseMutex was called with).
        try:
            released = self._release_mutex(handle)
        except Exception:  # noqa: BLE001 - must not skip the close step below
            return "ReleaseMutex raised an exception"
        return None if released else "ReleaseMutex returned FALSE"

    def _safe_close(self, handle: int) -> Optional[str]:
        try:
            closed = self._close_handle(handle)
        except Exception:  # noqa: BLE001
            return "CloseHandle raised an exception"
        return None if closed else "CloseHandle returned FALSE"


class ApplicationInstanceGuard(BridgeInstanceGuard):
    """Product-wide guard for the one resident desktop application process."""

    def __init__(
        self,
        *,
        name: str = _SETTINGS_MUTEX_NAME,
        _create_mutex: CreateMutexFn = _real_create_mutex,
        _release_mutex: ReleaseMutexFn = _real_release_mutex,
        _close_handle: CloseHandleFn = _real_close_handle,
    ) -> None:
        super().__init__(
            name=name,
            _create_mutex=_create_mutex,
            _release_mutex=_release_mutex,
            _close_handle=_close_handle,
            _duplicate_message=(
                f"{product_identity.DISPLAY_NAME}已在当前 Windows 会话中运行"
            ),
            _access_denied_means_duplicate=True,
        )


class ApplicationRuntimeInstanceGuard(BridgeInstanceGuard):
    """Guard one executable version in one location, including handoff waiters."""

    def __init__(
        self,
        *,
        name: str | None = None,
        _create_mutex: CreateMutexFn = _real_create_mutex,
        _release_mutex: ReleaseMutexFn = _real_release_mutex,
        _close_handle: CloseHandleFn = _real_close_handle,
    ) -> None:
        super().__init__(
            name=name or application_runtime_mutex_name(),
            _create_mutex=_create_mutex,
            _release_mutex=_release_mutex,
            _close_handle=_close_handle,
            _duplicate_message=(
                f"{product_identity.DISPLAY_NAME}当前版本已在启动或运行"
            ),
            _access_denied_means_duplicate=True,
        )


class ApplicationHandoffInstanceGuard(BridgeInstanceGuard):
    """Allow only one cross-version/location handoff in this logon session."""

    def __init__(
        self,
        *,
        name: str = _APPLICATION_HANDOFF_MUTEX_NAME,
        _create_mutex: CreateMutexFn = _real_create_mutex,
        _release_mutex: ReleaseMutexFn = _real_release_mutex,
        _close_handle: CloseHandleFn = _real_close_handle,
    ) -> None:
        super().__init__(
            name=name,
            _create_mutex=_create_mutex,
            _release_mutex=_release_mutex,
            _close_handle=_close_handle,
            _duplicate_message=(
                f"{product_identity.DISPLAY_NAME}正在切换运行版本"
            ),
            _access_denied_means_duplicate=True,
        )


class SettingsInstanceGuard(ApplicationInstanceGuard):
    """Compatibility name for callers from the former settings-only model."""


class ElementNavigationInstanceGuard(BridgeInstanceGuard):
    """Compatibility guard for the older standalone navigator entry point."""

    def __init__(
        self,
        *,
        name: str = _ELEMENT_NAVIGATION_MUTEX_NAME,
        _create_mutex: CreateMutexFn = _real_create_mutex,
        _release_mutex: ReleaseMutexFn = _real_release_mutex,
        _close_handle: CloseHandleFn = _real_close_handle,
    ) -> None:
        super().__init__(
            name=name,
            _create_mutex=_create_mutex,
            _release_mutex=_release_mutex,
            _close_handle=_close_handle,
            _duplicate_message=(
                f"{product_identity.DISPLAY_NAME}元素导航已在当前 Windows 会话中运行"
            ),
            _access_denied_means_duplicate=True,
        )


def _real_message_box(title: str, message: str) -> int:
    _require_windows()
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    # int MessageBoxW(HWND hWnd, LPCWSTR lpText, LPCWSTR lpCaption, UINT uType)
    user32.MessageBoxW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
    user32.MessageBoxW.restype = ctypes.c_int
    _MB_OK = 0x00000000
    _MB_ICONWARNING = 0x00000030
    _MB_SYSTEMMODAL = 0x00001000  # visible/on-top even with no owner window
    return user32.MessageBoxW(None, message, title, _MB_OK | _MB_ICONWARNING | _MB_SYSTEMMODAL)


def _real_confirm_application_handoff(title: str, message: str) -> bool:
    _require_windows()
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.MessageBoxW.argtypes = (
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    )
    user32.MessageBoxW.restype = ctypes.c_int
    _MB_YESNO = 0x00000004
    _MB_ICONQUESTION = 0x00000020
    _MB_SYSTEMMODAL = 0x00001000
    _IDYES = 6
    return (
        user32.MessageBoxW(
            None,
            message,
            title,
            _MB_YESNO | _MB_ICONQUESTION | _MB_SYSTEMMODAL,
        )
        == _IDYES
    )


def confirm_application_handoff(
    current_version: str,
    *,
    _confirm: ConfirmApplicationHandoffFn = _real_confirm_application_handoff,
) -> bool:
    """Ask before replacing a different executable copy that owns the app."""

    title = f"{product_identity.DISPLAY_NAME} {current_version}"
    message = (
        "检测到另一个目录或旧版本的无线麦正在运行。\n\n"
        f"是否退出旧版并打开当前版本 {current_version}？\n\n"
        "选择“是”后，支持自动退出的版本会正常退出；程序会尝试把更早版本唤到前台，"
        "被唤出的窗口仍是旧版，请从通知区域选择“完全退出”。旧版退出后，"
        "当前版本会自动继续打开；窗口标题显示当前版本号才表示切换完成。\n\n"
        "不会强制结束进程，也不会丢弃未保存的按键修改。"
    )
    try:
        return bool(_confirm(title, message))
    except Exception:
        print(
            f"{product_identity.DISPLAY_NAME}: 无法显示版本切换确认。",
            file=sys.stderr,
        )
        return False


def show_bridge_startup_blocked_notice(
    message: str,
    *,
    title: str = product_identity.DISPLAY_NAME,
    _message_box: Callable[[str, str], int] = _real_message_box,
) -> None:
    """Shows a visible Windows message box for a bridge launch the
    single-instance guard blocked - either a proven duplicate or an
    acquisition failure it could not resolve (XRBM-021 In-scope item 3;
    fail-closed contract in the module docstring). The packaged executable
    is windowed (``console=False`` in build/RemoteMicRC003.spec), so
    stdout/stderr are never visible to the user there - a message box is
    the only reliable user-visible signal. Off-Windows, or if the Win32
    call itself fails, this falls back to a stderr print so the signal is
    never completely silent during development/testing, even though that
    fallback path is not itself the task's "visible Windows notice".
    """

    try:
        _message_box(title, message)
    except Exception:
        print(f"{product_identity.DISPLAY_NAME}: {message}", file=sys.stderr)
