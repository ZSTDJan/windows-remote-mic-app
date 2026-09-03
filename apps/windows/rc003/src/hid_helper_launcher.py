"""Standalone PyInstaller entry point for the narrow elevated HID helper."""

from __future__ import annotations

from ovb_rc003.hid_elevation_windows import helper_main


if __name__ == "__main__":
    raise SystemExit(helper_main())
