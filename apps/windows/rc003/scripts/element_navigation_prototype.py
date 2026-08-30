"""Keyboard-driven UI Automation spatial navigator.

This file remains the single navigation source used by both the standalone
command and embedded companion-process entries.

Controls:
    Ctrl+Alt+N  scan the foreground window and enter/leave navigation
    Ctrl+Alt+D  enable/disable navigation diagnostics
    Arrow keys  move the highlighted target
    PageUp/Down move to the parent/child element at the same location
    Enter       left-click the highlighted target; press twice to double-click
    Menu        right-click the highlighted target
    Volume +/-  scroll up/down at the highlighted target
    Esc         leave navigation
    Ctrl+Alt+Q  quit the prototype
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from typing import Any, Optional, Sequence


_SPATIAL_NAVIGATION_CORE_NAME = "spatial_navigation_core"
_SPATIAL_NAVIGATION_CORE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "spatial_navigation_core.py",
)


def _load_spatial_navigation_core() -> Any:
    expected_path = os.path.normcase(
        os.path.abspath(_SPATIAL_NAVIGATION_CORE_PATH)
    )
    existing = sys.modules.get(_SPATIAL_NAVIGATION_CORE_NAME)
    if existing is not None:
        existing_path = os.path.normcase(
            os.path.abspath(str(getattr(existing, "__file__", "")))
        )
        if existing_path != expected_path:
            raise ImportError(
                "spatial_navigation_core already refers to a different file"
            )
        return existing

    spec = importlib.util.spec_from_file_location(
        _SPATIAL_NAVIGATION_CORE_NAME,
        _SPATIAL_NAVIGATION_CORE_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load spatial_navigation_core")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SPATIAL_NAVIGATION_CORE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_SPATIAL_NAVIGATION_CORE_NAME, None)
        raise
    return module


_spatial_navigation_core = _load_spatial_navigation_core()
for _core_export in _spatial_navigation_core.__all__:
    globals()[_core_export] = getattr(
        _spatial_navigation_core, _core_export
    )
del _core_export


_ELEMENT_TARGETING_CORE_NAME = "element_targeting_core"
_ELEMENT_TARGETING_CORE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "element_targeting_core.py",
)


def _load_element_targeting_core() -> Any:
    expected_path = os.path.normcase(
        os.path.abspath(_ELEMENT_TARGETING_CORE_PATH)
    )
    existing = sys.modules.get(_ELEMENT_TARGETING_CORE_NAME)
    if existing is not None:
        existing_path = os.path.normcase(
            os.path.abspath(str(getattr(existing, "__file__", "")))
        )
        if existing_path != expected_path:
            raise ImportError(
                "element_targeting_core already refers to a different file"
            )
        return existing

    spec = importlib.util.spec_from_file_location(
        _ELEMENT_TARGETING_CORE_NAME,
        _ELEMENT_TARGETING_CORE_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load element_targeting_core")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ELEMENT_TARGETING_CORE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_ELEMENT_TARGETING_CORE_NAME, None)
        raise
    return module


_element_targeting_core = _load_element_targeting_core()
_overlapping_core_exports = set(_spatial_navigation_core.__all__) & set(
    _element_targeting_core.__all__
)
if _overlapping_core_exports:
    raise ImportError(
        "spatial and targeting core exports overlap: "
        + ", ".join(sorted(_overlapping_core_exports))
    )
for _targeting_export in _element_targeting_core.__all__:
    globals()[_targeting_export] = getattr(
        _element_targeting_core, _targeting_export
    )
del _overlapping_core_exports
del _targeting_export


_ELEMENT_NAVIGATION_SUPPORT_NAME = "element_navigation_support"
_ELEMENT_NAVIGATION_SUPPORT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "element_navigation_support.py",
)


def _load_element_navigation_support() -> Any:
    expected_path = os.path.normcase(
        os.path.abspath(_ELEMENT_NAVIGATION_SUPPORT_PATH)
    )
    existing = sys.modules.get(_ELEMENT_NAVIGATION_SUPPORT_NAME)
    if existing is not None:
        existing_path = os.path.normcase(
            os.path.abspath(str(getattr(existing, "__file__", "")))
        )
        if existing_path != expected_path:
            raise ImportError(
                "element_navigation_support already refers to a different file"
            )
        return existing

    spec = importlib.util.spec_from_file_location(
        _ELEMENT_NAVIGATION_SUPPORT_NAME,
        _ELEMENT_NAVIGATION_SUPPORT_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load element_navigation_support")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ELEMENT_NAVIGATION_SUPPORT_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_ELEMENT_NAVIGATION_SUPPORT_NAME, None)
        raise
    return module


_element_navigation_support = _load_element_navigation_support()
_overlapping_support_exports = (
    set(_spatial_navigation_core.__all__)
    | set(_element_targeting_core.__all__)
) & set(_element_navigation_support.__all__)
if _overlapping_support_exports:
    raise ImportError(
        "navigation support exports overlap existing cores: "
        + ", ".join(sorted(_overlapping_support_exports))
    )
for _support_export in _element_navigation_support.__all__:
    globals()[_support_export] = getattr(
        _element_navigation_support, _support_export
    )
del _overlapping_support_exports
del _support_export


_ELEMENT_NAVIGATION_COMMAND_NAME = "element_navigation_command_windows"
_ELEMENT_NAVIGATION_COMMAND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "element_navigation_command_windows.py",
)


def _load_element_navigation_command() -> Any:
    expected_path = os.path.normcase(
        os.path.abspath(_ELEMENT_NAVIGATION_COMMAND_PATH)
    )
    existing = sys.modules.get(_ELEMENT_NAVIGATION_COMMAND_NAME)
    if existing is not None:
        existing_path = os.path.normcase(
            os.path.abspath(str(getattr(existing, "__file__", "")))
        )
        if existing_path != expected_path:
            raise ImportError(
                "element_navigation_command_windows already refers to a different file"
            )
        return existing

    spec = importlib.util.spec_from_file_location(
        _ELEMENT_NAVIGATION_COMMAND_NAME,
        _ELEMENT_NAVIGATION_COMMAND_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load element_navigation_command_windows")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ELEMENT_NAVIGATION_COMMAND_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_ELEMENT_NAVIGATION_COMMAND_NAME, None)
        raise
    return module


_element_navigation_command = _load_element_navigation_command()


def configure_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            continue


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-depth", type=int, default=16)
    parser.add_argument("--max-elements", type=int, default=300)
    parser.add_argument("--max-nodes", type=int, default=4000)
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="scan the current foreground window once and print the targets",
    )
    parser.add_argument(
        "--window-handle",
        type=lambda value: int(value, 0),
        default=0,
        help="scan this native window handle instead of the foreground window",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="enter navigation after the companion process is ready",
    )
    parser.add_argument(
        "--managed-companion",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--owner-pid",
        type=int,
        default=0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="start with navigation candidate diagnostics enabled",
    )
    parser.add_argument(
        "--quicker-state-file",
        default=os.environ.get(
            QUICKER_STATE_FILE_ENV,
            default_quicker_state_file(),
        ),
        help="optional JSON snapshot exported by a Quicker bridge action",
    )
    return parser.parse_args(argv)


_ELEMENT_NAVIGATION_WINDOWS_HOST_NAME = "element_navigation_windows_host"
_ELEMENT_NAVIGATION_WINDOWS_HOST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "element_navigation_windows_host.py",
)


def _load_element_navigation_windows_host() -> Any:
    expected_path = os.path.normcase(
        os.path.abspath(_ELEMENT_NAVIGATION_WINDOWS_HOST_PATH)
    )
    existing = sys.modules.get(_ELEMENT_NAVIGATION_WINDOWS_HOST_NAME)
    if existing is not None:
        existing_path = os.path.normcase(
            os.path.abspath(str(getattr(existing, "__file__", "")))
        )
        if existing_path != expected_path:
            raise ImportError(
                "element_navigation_windows_host already refers to a different file"
            )
        return existing

    spec = importlib.util.spec_from_file_location(
        _ELEMENT_NAVIGATION_WINDOWS_HOST_NAME,
        _ELEMENT_NAVIGATION_WINDOWS_HOST_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load element_navigation_windows_host")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ELEMENT_NAVIGATION_WINDOWS_HOST_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_ELEMENT_NAVIGATION_WINDOWS_HOST_NAME, None)
        raise
    return module


def _run_windows(args: argparse.Namespace) -> int:
    host = _load_element_navigation_windows_host()
    return host._run_windows(args)


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_standard_streams()
    args = _parse_args(argv)
    if sys.platform != "win32":
        print("这个原型只支持 Windows。", file=sys.stderr)
        return 2
    return _run_windows(args)


if __name__ == "__main__":
    raise SystemExit(main())
