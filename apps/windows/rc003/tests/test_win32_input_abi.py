"""Cross-platform assertions on the real x64 Win32 ``INPUT`` struct shape
declared in win32_input.py (XRBM-018, fixing XRBM-014 review round 2 P1 #1
and P1 #8).

These do not need ``ctypes.windll`` (Windows-only) at all - only
``ctypes.sizeof()``/``ctypes.offsetof()`` on plain ``ctypes.Structure``
classes, which works on any host. See win32_input.py's module-level comment
for why the struct fields use explicit fixed-width ctypes types
(``c_uint32``/``c_int32``/``c_uint16``) rather than ``ctypes.wintypes``
aliases: the latter track the *host* C ``long`` width (8 bytes on 64-bit
macOS/Linux) rather than the Windows ABI's ``long`` (always 4 bytes), which
would make a size assertion here meaningless off Windows.

The genuinely Windows-only half of this contract (a real ``SendInput`` call
with this struct actually succeeding) is covered separately in
tests/windows/test_windows_only.py.
"""

import ctypes
import sys
import unittest
from unittest import mock

from ovb_rc003 import win32_input


class InputStructShapeTests(unittest.TestCase):
    def test_sendinput_marks_all_three_builders_without_changing_input_payload(self):
        user32 = mock.Mock()
        # _build_input_array now resolves ordinary keys' scan codes through
        # ctypes.windll.user32.MapVirtualKeyW; pin it to 0 here so the builder
        # falls back to its wVk path and the before/after payload comparison
        # stays deterministic on every host.
        user32.MapVirtualKeyW.return_value = 0
        for builder, events, union_name in (
            (win32_input._build_input_array, [(0x4e, False), (0x4e, True)], "ki"),
            (win32_input._build_virtual_key_input_array, [(0xa2, False), (0x5b, True)], "ki"),
            (win32_input._build_mouse_input_array, [(win32_input._MOUSEEVENTF_XDOWN, win32_input._XBUTTON2)], "mi"),
        ):
            with self.subTest(builder=builder.__name__):
                with mock.patch.object(win32_input, "_require_live_input_allowed"), \
                     mock.patch.object(win32_input, "_require_windows"), \
                     mock.patch.object(win32_input.ctypes, "windll", mock.Mock(user32=user32), create=True), \
                     mock.patch.object(win32_input.ctypes, "set_last_error", create=True), \
                     mock.patch.object(win32_input.ctypes, "get_last_error", return_value=0, create=True):
                    expected, input_type = builder(events)
                    def send(count, actual, size):
                        self.assertEqual(size, ctypes.sizeof(input_type))
                        self.assertEqual(count, len(events))
                        for index in range(count):
                            item = getattr(actual[index].union, union_name)
                            self.assertTrue(win32_input.is_own_input_event(item.dwExtraInfo))
                            self.assertFalse(win32_input.voice_key_physicalizer_windows._is_voice_event_marker(item.dwExtraInfo))
                            getattr(expected[index].union, union_name).dwExtraInfo = item.dwExtraInfo
                            self.assertEqual(bytes(actual[index]), bytes(expected[index]))
                        return count
                    user32.SendInput.side_effect = send
                    self.assertEqual(win32_input._real_send_input_batch_with_builder(events, builder), len(events))

    def test_sizeof_input_matches_the_documented_x64_win32_abi(self):
        # Microsoft's own SendInput documentation and headers put
        # sizeof(INPUT) at 40 bytes on x64 - this is the exact value
        # SendInput's cbSize parameter must receive; the XRBM-014 round-2
        # regression (a union with only KEYBDINPUT) produced 32 instead.
        self.assertEqual(ctypes.sizeof(win32_input.INPUT), 40)

    def test_union_is_sized_by_its_largest_member_mouseinput(self):
        self.assertEqual(
            ctypes.sizeof(win32_input._INPUT_UNION), ctypes.sizeof(win32_input.MOUSEINPUT)
        )
        self.assertGreater(
            ctypes.sizeof(win32_input.MOUSEINPUT), ctypes.sizeof(win32_input.KEYBDINPUT)
        )

    def test_union_declares_all_three_real_members(self):
        field_names = {name for name, _type in win32_input._INPUT_UNION._fields_}
        self.assertEqual(field_names, {"mi", "ki", "hi"})

    def test_keybdinput_extra_info_is_pointer_sized_ulong_ptr(self):
        # ULONG_PTR is an integer the same width as a pointer, not
        # POINTER(ULONG) - a NULL pointer and a zero ULONG_PTR happen to be
        # bit-identical, which is what let the previous (wrong-typed) field
        # hide this bug on x64.
        field_type = dict(win32_input.KEYBDINPUT._fields_)["dwExtraInfo"]
        self.assertIs(field_type, ctypes.c_size_t)
        self.assertEqual(ctypes.sizeof(field_type), ctypes.sizeof(ctypes.c_void_p))

    def test_input_type_field_is_first_and_four_bytes(self):
        self.assertEqual(win32_input.INPUT.type.offset, 0)
        self.assertEqual(ctypes.sizeof(dict(win32_input.INPUT._fields_)["type"]), 4)

    def test_sendinput_argtypes_use_a_pointer_to_the_real_input_struct(self):
        # _build_input_array is the only place production code constructs
        # the array SendInput receives; assert it hands back the same INPUT
        # type this module declares (not some other ad-hoc struct).
        array, input_type = win32_input._build_input_array([(0x41, False)])
        self.assertIs(input_type, win32_input.INPUT)
        self.assertEqual(ctypes.sizeof(array), ctypes.sizeof(win32_input.INPUT))

    def test_volume_keys_use_extended_virtual_key_events(self):
        for name in ("volume_mute", "volume_down", "volume_up"):
            with self.subTest(name=name):
                vk = win32_input.win32_keys.VK_CODES[name]
                array, _ = win32_input._build_input_array(
                    [(vk, False), (vk, True)]
                )
                key_down = array[0].union.ki
                key_up = array[1].union.ki
                self.assertEqual((key_down.wVk, key_down.wScan), (vk, 0))
                self.assertEqual(
                    key_down.dwFlags, win32_input._KEYEVENTF_EXTENDEDKEY
                )
                self.assertEqual((key_up.wVk, key_up.wScan), (vk, 0))
                self.assertEqual(
                    key_up.dwFlags,
                    win32_input._KEYEVENTF_EXTENDEDKEY
                    | win32_input._KEYEVENTF_KEYUP,
                )

    def test_mouse_builder_uses_the_same_real_input_union(self):
        array, input_type = win32_input._build_mouse_input_array(
            [(win32_input._MOUSEEVENTF_XDOWN, win32_input._XBUTTON2)]
        )
        self.assertIs(input_type, win32_input.INPUT)
        self.assertEqual(array[0].type, win32_input._INPUT_MOUSE)
        self.assertEqual(array[0].union.mi.dwFlags, win32_input._MOUSEEVENTF_XDOWN)
        self.assertEqual(array[0].union.mi.mouseData, win32_input._XBUTTON2)

    def test_right_alt_uses_the_extended_physical_scan_code(self):
        array, _ = win32_input._build_input_array(
            [(win32_input.win32_keys.VK_CODES["ralt"], False)]
        )
        keybd = array[0].union.ki
        self.assertEqual(keybd.wVk, 0)
        self.assertEqual(keybd.wScan, 0x38)
        self.assertEqual(
            keybd.dwFlags,
            win32_input._KEYEVENTF_SCANCODE | win32_input._KEYEVENTF_EXTENDEDKEY,
        )

    def test_left_ctrl_uses_a_non_extended_physical_scan_code(self):
        array, _ = win32_input._build_input_array(
            [(win32_input.win32_keys.VK_CODES["lctrl"], False)]
        )
        keybd = array[0].union.ki
        self.assertEqual(keybd.wVk, 0)
        self.assertEqual(keybd.wScan, 0x1D)
        self.assertEqual(keybd.dwFlags, win32_input._KEYEVENTF_SCANCODE)

    def test_left_win_uses_the_extended_physical_scan_code(self):
        array, _ = win32_input._build_input_array(
            [(win32_input.win32_keys.VK_CODES["win"], False)]
        )
        keybd = array[0].union.ki
        self.assertEqual(keybd.wVk, 0)
        self.assertEqual(keybd.wScan, 0x5B)
        self.assertEqual(
            keybd.dwFlags,
            win32_input._KEYEVENTF_SCANCODE | win32_input._KEYEVENTF_EXTENDEDKEY,
        )

    def test_wetype_builder_keeps_ctrl_and_win_in_virtual_key_fields(self):
        lctrl = win32_input.win32_keys.VK_CODES["lctrl"]
        lwin = win32_input.win32_keys.VK_CODES["lwin"]
        array, _ = win32_input._build_virtual_key_input_array(
            [(lctrl, False), (lwin, True)]
        )

        self.assertEqual((array[0].union.ki.wVk, array[0].union.ki.wScan), (lctrl, 0))
        self.assertFalse(array[0].union.ki.dwFlags & win32_input._KEYEVENTF_SCANCODE)
        self.assertEqual((array[1].union.ki.wVk, array[1].union.ki.wScan), (lwin, 0))
        self.assertTrue(array[1].union.ki.dwFlags & win32_input._KEYEVENTF_KEYUP)
        self.assertTrue(
            array[1].union.ki.dwFlags & win32_input._KEYEVENTF_EXTENDEDKEY
        )

    def test_all_arrow_keys_use_their_extended_physical_scan_codes(self):
        expected = {
            "up": 0x48,
            "down": 0x50,
            "left": 0x4B,
            "right": 0x4D,
        }
        for name, scan_code in expected.items():
            with self.subTest(name=name):
                array, _ = win32_input._build_input_array(
                    [(win32_input.win32_keys.VK_CODES[name], False)]
                )
                keybd = array[0].union.ki
                self.assertEqual(keybd.wVk, 0)
                self.assertEqual(keybd.wScan, scan_code)
                self.assertEqual(
                    keybd.dwFlags,
                    win32_input._KEYEVENTF_SCANCODE
                    | win32_input._KEYEVENTF_EXTENDEDKEY,
                )

    def test_page_keys_use_extended_physical_scan_codes(self):
        for name, scan_code in (("pageup", 0x49), ("pagedown", 0x51)):
            with self.subTest(name=name):
                vk = win32_input.win32_keys.VK_CODES[name]
                array, _ = win32_input._build_input_array(
                    [(vk, False), (vk, True)]
                )
                for index, key_up in ((0, False), (1, True)):
                    keyboard = array[index].union.ki
                    self.assertEqual((keyboard.wVk, keyboard.wScan), (0, scan_code))
                    expected_flags = (
                        win32_input._KEYEVENTF_SCANCODE
                        | win32_input._KEYEVENTF_EXTENDEDKEY
                    )
                    if key_up:
                        expected_flags |= win32_input._KEYEVENTF_KEYUP
                    self.assertEqual(keyboard.dwFlags, expected_flags)

    def test_ordinary_keys_get_a_physical_scan_code(self):
        # MapVirtualKeyW resolves 'A' (0x41) -> 0x1E; the builder must emit
        # that scan code with the SCANCODE flag so Chromium/Electron receive
        # a real event.code instead of an empty string.
        user32 = mock.Mock()
        user32.MapVirtualKeyW.return_value = 0x1E
        with mock.patch.object(
            win32_input.ctypes,
            "windll",
            mock.Mock(user32=user32),
            create=True,
        ):
            array, _ = win32_input._build_input_array([(0x41, False), (0x41, True)])
        for index, key_up in enumerate((False, True)):
            keybd = array[index].union.ki
            self.assertEqual(keybd.wVk, 0)
            self.assertEqual(keybd.wScan, 0x1E)
            self.assertEqual(keybd.dwFlags, win32_input._KEYEVENTF_SCANCODE
                             | (win32_input._KEYEVENTF_KEYUP if key_up else 0))
        self.assertEqual(user32.MapVirtualKeyW.call_args_list,
                         [mock.call(0x41, 4), mock.call(0x41, 4)])

    def test_enhanced_keys_keep_their_physical_identity_on_both_edges(self):
        expected = {
            "home": 0x47, "end": 0x4F, "insert": 0x52, "delete": 0x53,
            "apps": 0x5D, "numpad_divide": 0x35, "print_screen": 0x37,
        }
        with mock.patch.object(win32_input, "_virtual_key_to_scan_code") as mapper:
            for name, scan in expected.items():
                with self.subTest(name=name):
                    vk = win32_input.win32_keys.VK_CODES[name]
                    array, _ = win32_input._build_input_array([(vk, False), (vk, True)])
                    for index, key_up in enumerate((False, True)):
                        keybd = array[index].union.ki
                        self.assertEqual((keybd.wVk, keybd.wScan), (0, scan))
                        self.assertEqual(keybd.dwFlags, 9 | (2 if key_up else 0))
            mapper.assert_not_called()

    def test_dynamic_extended_scan_is_split_into_code_and_flag(self):
        # A supported raw VK can carry an E0 prefix too (Launch Mail).
        user32 = mock.Mock()
        user32.MapVirtualKeyW.return_value = 0xE06C
        with mock.patch.object(win32_input.ctypes, "windll",
                               mock.Mock(user32=user32), create=True):
            array, _ = win32_input._build_input_array([(0xB4, False), (0xB4, True)])
        for index, key_up in enumerate((False, True)):
            keybd = array[index].union.ki
            self.assertEqual((keybd.wVk, keybd.wScan), (0, 0x6C))
            self.assertEqual(keybd.dwFlags, 9 | (2 if key_up else 0))

    def test_numpad_numbers_keep_virtual_key_semantics_with_scan_codes(self):
        # Retaining wVk without SCANCODE prevents NumLock from turning an
        # already-configured numeric action into End/Home/Delete, etc.
        scans = (0x52, 0x4F, 0x50, 0x51, 0x4B, 0x4C, 0x4D, 0x47, 0x48, 0x49)
        cases = [(f"numpad{digit}", scan) for digit, scan in enumerate(scans)]
        cases.append(("numpad_decimal", 0x53))
        user32 = mock.Mock()
        with mock.patch.object(win32_input.ctypes, "windll",
                               mock.Mock(user32=user32), create=True):
            for name, scan in cases:
                with self.subTest(name=name):
                    user32.MapVirtualKeyW.return_value = scan
                    vk = win32_input.win32_keys.VK_CODES[name]
                    array, _ = win32_input._build_input_array([(vk, False), (vk, True)])
                    for index, key_up in enumerate((False, True)):
                        keybd = array[index].union.ki
                        self.assertEqual((keybd.wVk, keybd.wScan), (vk, scan))
                        self.assertEqual(keybd.dwFlags, 2 if key_up else 0)

    def test_e1_unknown_and_empty_scan_codes_keep_virtual_key_delivery(self):
        user32 = mock.Mock()
        with mock.patch.object(win32_input.ctypes, "windll",
                               mock.Mock(user32=user32), create=True):
            for value in (0, 0xE11D, 0xE000, 0xE100, 0xFFFF, -1):
                with self.subTest(scan=value):
                    user32.MapVirtualKeyW.return_value = value
                    array, _ = win32_input._build_input_array([(0x13, False), (0x13, True)])
                    for index, key_up in enumerate((False, True)):
                        keybd = array[index].union.ki
                        self.assertEqual((keybd.wVk, keybd.wScan), (0x13, 0))
                        self.assertEqual(keybd.dwFlags, 2 if key_up else 0)

    def test_mapper_errors_and_unavailable_windows_api_keep_virtual_keys(self):
        user32 = mock.Mock()
        user32.MapVirtualKeyW.side_effect = OSError("mapping unavailable")
        for windll in (None, mock.Mock(user32=user32)):
            with self.subTest(windll=windll), mock.patch.object(
                win32_input.ctypes, "windll", windll, create=True
            ):
                array, _ = win32_input._build_input_array([(0x41, False), (0x41, True)])
                for index, key_up in enumerate((False, True)):
                    keybd = array[index].union.ki
                    self.assertEqual((keybd.wVk, keybd.wScan), (0x41, 0))
                    self.assertEqual(keybd.dwFlags, 2 if key_up else 0)

    @unittest.skipUnless(sys.platform == "win32", "requires native Windows mapping")
    def test_native_mapping_of_ordinary_keys_and_extended_vk_without_injection(self):
        native = ctypes.WinDLL("user32").MapVirtualKeyW
        native.argtypes = (ctypes.c_uint, ctypes.c_uint)
        native.restype = ctypes.c_uint
        for name in ("a", "k", "l", "escape", "space", "enter", "slash", "f24", "vk_b4"):
            with self.subTest(name=name):
                vk = win32_input.win32_keys.resolve_vk_codes((name,))[0]
                expected = int(native(vk, 4))
                self.assertNotEqual(expected & 0xFF, 0)
                array, _ = win32_input._build_input_array([(vk, False), (vk, True)])
                for index, key_up in enumerate((False, True)):
                    keybd = array[index].union.ki
                    self.assertEqual((keybd.wVk, keybd.wScan), (0, expected & 0xFF))
                    self.assertEqual(keybd.dwFlags, 8 | (1 if expected >> 8 == 0xE0 else 0)
                                     | (2 if key_up else 0))

    def test_media_keys_stay_on_the_virtual_key_path(self):
        # Consumer-control keys have no reliable physical scan code; they
        # must keep their wVk-based event even when a mapping is available.
        user32 = mock.Mock()
        user32.MapVirtualKeyW.return_value = 0x30
        with mock.patch.object(
            win32_input.ctypes,
            "windll",
            mock.Mock(user32=user32),
            create=True,
        ):
            for name in ("volume_mute", "volume_down", "volume_up", "media_next",
                         "media_previous", "media_stop", "media_play_pause",
                         "browser_back", "browser_forward"):
                with self.subTest(name=name):
                    vk = win32_input.win32_keys.VK_CODES[name]
                    array, _ = win32_input._build_input_array([(vk, False), (vk, True)])
                    for index, key_up in enumerate((False, True)):
                        keybd = array[index].union.ki
                        self.assertEqual((keybd.wVk, keybd.wScan), (vk, 0))
                        expected_flags = 1 if name.startswith("volume_") else 0
                        self.assertEqual(keybd.dwFlags, expected_flags | (2 if key_up else 0))
            user32.MapVirtualKeyW.assert_not_called()

    def test_scan_code_fallback_keeps_wvk_when_mapping_returns_zero(self):
        # A key with no resolvable scan code must fall back to a wVk-based
        # event rather than raising.
        user32 = mock.Mock()
        user32.MapVirtualKeyW.return_value = 0
        with mock.patch.object(
            win32_input.ctypes,
            "windll",
            mock.Mock(user32=user32),
            create=True,
        ):
            array, _ = win32_input._build_input_array([(0x41, False)])
        keybd = array[0].union.ki
        self.assertEqual((keybd.wVk, keybd.wScan), (0x41, 0))
        self.assertFalse(keybd.dwFlags & win32_input._KEYEVENTF_SCANCODE)

if __name__ == "__main__":
    unittest.main()
