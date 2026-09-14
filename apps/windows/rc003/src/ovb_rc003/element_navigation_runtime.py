"""Load the shared navigator for embedded product and compatibility runs."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from . import element_navigation_control_windows, single_instance


_PROTOTYPE_MODULE_NAME = "remote_mic_element_navigation"
_DATA_DIRECTORY_NAME = "element_navigation"
_diagnostic_trace: Any = None


def set_diagnostic_trace(trace: Any) -> None:
    """Reuse the active bridge writer, including its live diagnostic switch."""
    global _diagnostic_trace
    _diagnostic_trace = trace


def clear_diagnostic_trace(trace: Any) -> None:
    global _diagnostic_trace
    if _diagnostic_trace is trace:
        _diagnostic_trace = None


def _diagnostic_enabled() -> bool:
    trace = _diagnostic_trace
    return trace is not None and bool(trace.enabled)


def _emit_diagnostic(event: str, **fields: Any) -> None:
    # Navigation starts before the bridge writer and stops after it. Keep
    # lifecycle failures in the already configured application log as well.
    if event in {
        "element_navigation_ready", "element_navigation_startup_error",
        "element_navigation_worker_error", "element_navigation_watcher",
        "element_navigation_cleanup",
    }:
        try:
            logging.getLogger("ovb_rc003").info(
                "element navigation lifecycle: event=%s runtime=%s command=%s "
                "outcome=%s error_type=%s error_code=%s failures=%s",
                event, fields.get("navigation_runtime_id"), fields.get("command"),
                fields.get("outcome"), fields.get("error_type"),
                fields.get("error_code"), fields.get("failure_count"),
            )
        except Exception:
            pass
    # The desktop navigator outlives bridge restarts. Resolve the current
    # writer per event instead of retaining a closed writer in its callback.
    trace = _diagnostic_trace
    if trace is not None:
        try:
            trace.emit(event, **fields)
        except Exception:
            pass


def navigation_source_directory() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / _DATA_DIRECTORY_NAME
    return Path(__file__).resolve().parents[2] / "scripts"


def _load_prototype() -> Any:
    source_path = navigation_source_directory() / "element_navigation_prototype.py"
    if not source_path.is_file():
        raise RuntimeError(f"element navigation source is missing: {source_path}")
    expected_path = source_path.resolve()
    existing = sys.modules.get(_PROTOTYPE_MODULE_NAME)
    if existing is not None:
        existing_path = Path(str(getattr(existing, "__file__", ""))).resolve()
        if existing_path != expected_path:
            raise ImportError("element navigation module refers to a different file")
        return existing
    spec = importlib.util.spec_from_file_location(
        _PROTOTYPE_MODULE_NAME,
        expected_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load element navigation entry")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PROTOTYPE_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_PROTOTYPE_MODULE_NAME, None)
        raise
    return module


def _argument_value(arguments: Sequence[str], flag: str) -> Optional[str]:
    try:
        index = arguments.index(flag)
    except ValueError:
        return None
    return arguments[index + 1] if index + 1 < len(arguments) else None


def run_element_navigation(arguments: Sequence[str]) -> int:
    argv = list(arguments)
    try:
        with single_instance.ElementNavigationInstanceGuard():
            return int(_load_prototype().main(argv))
    except single_instance.DuplicateInstanceError:
        if "--activate" not in argv:
            return single_instance.DUPLICATE_INSTANCE_EXIT_CODE
        raw_handle = _argument_value(argv, "--window-handle")
        try:
            target_hwnd = int(raw_handle, 0) if raw_handle else 0
        except ValueError:
            target_hwnd = 0
        result = element_navigation_control_windows.send_element_navigation_command(
            element_navigation_control_windows.ELEMENT_NAVIGATION_COMMAND_TOGGLE,
            target_hwnd,
        )
        if result == element_navigation_control_windows.CommandSendResult.DELIVERED:
            return 0
        return single_instance.DUPLICATE_INSTANCE_EXIT_CODE


def start_embedded_element_navigation(application: object):
    """Start the navigator inside the existing desktop Qt process."""

    try:
        return _load_prototype().start_embedded(
            application, diagnostic_sink=_emit_diagnostic,
            diagnostic_enabled=_diagnostic_enabled,
        )
    except Exception as exc:
        _emit_diagnostic("element_navigation_startup_error", error_type=type(exc).__name__)
        raise


__all__ = (
    "set_diagnostic_trace",
    "clear_diagnostic_trace",
    "navigation_source_directory",
    "run_element_navigation",
    "start_embedded_element_navigation",
)
