"""Standalone PyInstaller entry point for the narrow elevated HID helper."""

from __future__ import annotations

HELPER_EXIT_UNEXPECTED_FAILURE = 5


def _load_helper_main():
    from ovb_rc003.hid_elevation_windows import helper_main

    return helper_main


def main() -> int:
    """Keep frozen import failures silent and visible through the exit code."""

    try:
        helper_main = _load_helper_main()
    except Exception:
        return HELPER_EXIT_UNEXPECTED_FAILURE
    try:
        return int(helper_main())
    except Exception:
        return HELPER_EXIT_UNEXPECTED_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
