"""Loads the single element-navigation source into its companion process."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from . import element_navigation_control_windows, single_instance


_PROTOTYPE_MODULE_NAME = "remote_mic_element_navigation"
_DATA_DIRECTORY_NAME = "element_navigation"


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


__all__ = ("navigation_source_directory", "run_element_navigation")
