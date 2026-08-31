"""Tests for the native Windows title-bar color adapter."""

from __future__ import annotations

import ctypes
import unittest

from ovb_rc003 import window_chrome_windows


class _Color:
    def __init__(self, red: int, green: int, blue: int) -> None:
        self._channels = red, green, blue

    def red(self) -> int:
        return self._channels[0]

    def green(self) -> int:
        return self._channels[1]

    def blue(self) -> int:
        return self._channels[2]


class _Window:
    def __init__(self) -> None:
        self._properties = {
            "nativeBorderColor": _Color(216, 216, 216),
            "nativeCaptionColor": _Color(229, 229, 229),
            "nativeCaptionTextColor": _Color(23, 25, 29),
        }

    def winId(self) -> int:
        return 4321

    def property(self, name: str) -> object:
        return self._properties[name]


class WindowChromeWindowsTests(unittest.TestCase):
    def test_non_windows_is_a_noop(self) -> None:
        self.assertFalse(
            window_chrome_windows.apply_settings_window_chrome(
                _Window(),
                platform="linux",
            )
        )

    def test_applies_border_caption_and_text_colors(self) -> None:
        calls: list[tuple[int, int, int]] = []

        def set_attribute(hwnd, attribute, value, size) -> int:
            calls.append(
                (
                    int(hwnd.value),
                    int(attribute.value),
                    ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32)).contents.value,
                )
            )
            self.assertEqual(int(size.value), ctypes.sizeof(ctypes.c_uint32))
            return 0

        self.assertTrue(
            window_chrome_windows.apply_settings_window_chrome(
                _Window(),
                platform="win32",
                set_window_attribute=set_attribute,
            )
        )
        self.assertEqual(
            calls,
            [
                (4321, 34, 0x00D8D8D8),
                (4321, 35, 0x00E5E5E5),
                (4321, 36, 0x001D1917),
            ],
        )

    def test_missing_qml_properties_fails_without_touching_dwm(self) -> None:
        class MissingProperties:
            def winId(self) -> int:
                return 4321

        touched = False

        def set_attribute(*_args) -> int:
            nonlocal touched
            touched = True
            return 0

        self.assertFalse(
            window_chrome_windows.apply_settings_window_chrome(
                MissingProperties(),
                platform="win32",
                set_window_attribute=set_attribute,
            )
        )
        self.assertFalse(touched)


if __name__ == "__main__":
    unittest.main()
