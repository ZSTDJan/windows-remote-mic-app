"""Exercises Raw Input parsing without creating a real Windows message loop.

The body parsers consume plain bytes. The packet reader uses a small fake
``user32`` object so header probing, sizing, device scoping and short reads
are covered without touching the user's actual input devices.

This is the "at least establish it at the source contract layer" evidence
for the Raw Input adapter (XRBM-014 review RETRY P1 #5/#9): the actual
window creation, RegisterRawInputDevices, and GetRawInputData calls remain
genuinely Windows-only and untested here - see raw_input_windows.py's module
docstring for the honest, disclosed uncertainty about which Raw Input event
shape (RIM_TYPEKEYBOARD vs RIM_TYPEHID) the real device actually produces.
"""

import ctypes
import struct
import threading
import unittest
from ctypes import wintypes
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import hid_identity, raw_input_windows

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105


def _rawkeyboard_body(
    vkey: int,
    message: int = WM_KEYDOWN,
    *,
    make_code: int = 0,
    flags: int | None = None,
) -> bytes:
    # RAWKEYBOARD: MakeCode(u16) Flags(u16) Reserved(u16) VKey(u16) Message(u32) ExtraInformation(u32)
    if flags is None:
        flags = raw_input_windows._RI_KEY_BREAK if message in (WM_KEYUP, WM_SYSKEYUP) else 0
    return struct.pack("<HHHHII", make_code, flags, 0, vkey, message, 0)


def _rawhid_body(reports) -> bytes:
    size_hid = len(reports[0]) if reports else 0
    body = size_hid.to_bytes(4, "little") + len(reports).to_bytes(4, "little")
    for report in reports:
        body += report
    return body


def _hid_report(usages):
    payload = bytearray(6)
    for index, usage in enumerate(usages[:3]):
        payload[index * 2 : index * 2 + 2] = usage.to_bytes(2, "little")
    return bytes((0x01, 0x00, 0x00)) + bytes(payload)


class RecordingListener:
    def __init__(self):
        self.events = []
        self.raw_events = []
        self.listener = raw_input_windows.RawInputButtonListener(
            self._on_event, self.raw_events.append
        )

    def _on_event(self, button_id, is_pressed):
        self.events.append((button_id, is_pressed))


class _CallableWin32Function:
    def __init__(self, callback):
        self._callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._callback(*args)


class _FakeMessageLoopUser32:
    def __init__(self, get_message_result=-1):
        self.get_message_calls = 0
        self.destroyed_windows = []
        self.unregistered_classes = []
        self.RegisterClassW = _CallableWin32Function(lambda *_args: 1)
        self.CreateWindowExW = _CallableWin32Function(lambda *_args: 1001)
        self.DefWindowProcW = _CallableWin32Function(lambda *_args: 0)
        self.DestroyWindow = _CallableWin32Function(self._destroy_window)
        self.PostQuitMessage = _CallableWin32Function(lambda *_args: None)
        self.GetMessageW = _CallableWin32Function(
            lambda *_args: self._get_message(get_message_result)
        )
        self.TranslateMessage = _CallableWin32Function(lambda *_args: 1)
        self.DispatchMessageW = _CallableWin32Function(lambda *_args: 0)
        self.UnregisterClassW = _CallableWin32Function(
            self._unregister_class
        )
        self.RegisterRawInputDevices = _CallableWin32Function(
            lambda *_args: 1
        )

    def _get_message(self, result):
        self.get_message_calls += 1
        return result

    def _destroy_window(self, hwnd):
        self.destroyed_windows.append(hwnd)
        return 1

    def _unregister_class(self, class_name, hinstance):
        self.unregistered_classes.append((class_name, hinstance))
        return 1


class _FakeMessageLoopKernel32:
    def __init__(self):
        self.GetModuleHandleW = _CallableWin32Function(lambda *_args: 2001)


class _RawInputHeader(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class _FakeRawInputUser32:
    RID_INPUT = 0x10000003
    RID_HEADER = 0x10000005
    UINT_ERROR = 0xFFFFFFFF

    def __init__(
        self,
        body: bytes,
        *,
        device_path: str = r"\\?\hid#vid_2717&pid_32b5#selected",
        device_handle: int = 123,
        packet_handle: int | None = None,
        probe_result: int | None = None,
        size_result: int = 0,
        written_result: int | None = None,
        packet_size_delta: int = 0,
    ):
        self.device_path = device_path
        self.device_handle = device_handle
        header_size = ctypes.sizeof(_RawInputHeader)
        packet_size = header_size + len(body)
        self.probe_header = _RawInputHeader(
            dwType=1,
            dwSize=packet_size,
            hDevice=device_handle,
            wParam=0,
        )
        self.packet_header = _RawInputHeader(
            dwType=1,
            dwSize=packet_size + packet_size_delta,
            hDevice=device_handle if packet_handle is None else packet_handle,
            wParam=0,
        )
        self.packet = bytes(self.packet_header) + body
        self.probe_result = header_size if probe_result is None else probe_result
        self.size_result = size_result
        self.written_result = (
            len(self.packet) if written_result is None else written_result
        )
        self.GetRawInputData = _CallableWin32Function(self._get_raw_input_data)
        self.GetRawInputDeviceInfoW = _CallableWin32Function(
            self._get_raw_input_device_info
        )

    def _get_raw_input_data(self, _handle, command, data, size_ptr, _header_size):
        if command == self.RID_HEADER:
            if self.probe_result == self.UINT_ERROR:
                return self.UINT_ERROR
            ctypes.memmove(
                data,
                ctypes.byref(self.probe_header),
                ctypes.sizeof(self.probe_header),
            )
            size_ptr._obj.value = ctypes.sizeof(self.probe_header)
            return self.probe_result
        if data is None:
            size_ptr._obj.value = len(self.packet)
            return self.size_result
        ctypes.memmove(data, self.packet, len(self.packet))
        return self.written_result

    def _get_raw_input_device_info(self, _handle, _command, data, size_ptr):
        if not self.device_path:
            size_ptr._obj.value = 0
            return 0
        if data is None:
            size_ptr._obj.value = len(self.device_path) + 1
            return 0
        encoded = ctypes.create_unicode_buffer(self.device_path)
        ctypes.memmove(data, encoded, ctypes.sizeof(encoded))
        return len(self.device_path)


class RawPacketReadTests(unittest.TestCase):
    DEVICE_PATH = r"\\?\hid#vid_2717&pid_32b5#selected"

    def _listener(self):
        rec = RecordingListener()
        rec.listener._normalized_device_path = hid_identity.normalize_device_path(
            self.DEVICE_PATH
        )
        corruptions = []
        rec.listener.set_input_corruption_callback(corruptions.append)
        return rec, corruptions

    def test_valid_selected_keyboard_packet_is_decoded(self):
        rec, corruptions = self._listener()
        user32 = _FakeRawInputUser32(
            _rawkeyboard_body(0x26),
            device_path=self.DEVICE_PATH,
        )

        rec.listener._handle_raw_input(1, _user32=user32)

        self.assertEqual(rec.events, [("up", True)])
        self.assertEqual(corruptions, [])
        self.assertEqual(rec.listener._selected_device_handles, {123})

    def test_header_probe_failure_disables_the_listener_generation(self):
        rec, corruptions = self._listener()
        losses = []
        rec.listener.set_physical_keyboard_tracking_lost_callback(losses.append)
        raw_input_windows._set_physical_keyboard_tracker_active(True)
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26))
        user32 = _FakeRawInputUser32(
            _rawkeyboard_body(0x26),
            probe_result=_FakeRawInputUser32.UINT_ERROR,
        )
        try:
            rec.listener._handle_raw_input(1, _user32=user32)
            rec.listener._handle_raw_input(
                1,
                _user32=_FakeRawInputUser32(
                    _rawkeyboard_body(0x27),
                    device_path=self.DEVICE_PATH,
                ),
            )
        finally:
            raw_input_windows._set_physical_keyboard_tracker_active(False)

        self.assertEqual(rec.events, [("up", True)])
        self.assertEqual(corruptions, ["raw_input_header_unavailable"])
        self.assertEqual(losses, ["raw_input_header_unavailable"])
        self.assertFalse(rec.listener._raw_input_header_healthy)
        self.assertEqual(rec.listener._active_keyboard_buttons, frozenset())
        self.assertEqual(rec.listener._selected_device_handles, set())

    def test_selected_device_name_failure_reports_corruption(self):
        rec, corruptions = self._listener()
        rec.listener._selected_device_handles.add(123)
        user32 = _FakeRawInputUser32(
            _rawkeyboard_body(0x26),
            device_path="",
        )

        rec.listener._handle_raw_input(1, _user32=user32)

        self.assertEqual(rec.events, [])
        self.assertEqual(corruptions, ["selected_device_name_unavailable"])

    def test_packet_size_query_failure_reports_corruption(self):
        rec, corruptions = self._listener()
        user32 = _FakeRawInputUser32(
            _rawkeyboard_body(0x26),
            device_path=self.DEVICE_PATH,
            size_result=_FakeRawInputUser32.UINT_ERROR,
        )

        rec.listener._handle_raw_input(1, _user32=user32)

        self.assertEqual(rec.events, [])
        self.assertEqual(corruptions, ["raw_input_size_unavailable"])

    def test_short_packet_read_reports_corruption(self):
        rec, corruptions = self._listener()
        user32 = _FakeRawInputUser32(
            _rawkeyboard_body(0x26),
            device_path=self.DEVICE_PATH,
            written_result=1,
        )

        rec.listener._handle_raw_input(1, _user32=user32)

        self.assertEqual(rec.events, [])
        self.assertEqual(corruptions, ["raw_input_packet_truncated"])

    def test_header_mismatch_reports_corruption(self):
        rec, corruptions = self._listener()
        user32 = _FakeRawInputUser32(
            _rawkeyboard_body(0x26),
            device_path=self.DEVICE_PATH,
            packet_handle=456,
        )

        rec.listener._handle_raw_input(1, _user32=user32)

        self.assertEqual(rec.events, [])
        self.assertEqual(corruptions, ["raw_input_header_mismatch"])


@unittest.skipUnless(
    hasattr(ctypes, "WINFUNCTYPE"),
    "Raw Input message-loop callbacks require Windows ctypes",
)
class MessageLoopFailureTests(unittest.TestCase):
    def test_run_getmessage_failure_reports_listener_exit_once(self):
        corruptions = []
        tracking_losses = []
        listener = raw_input_windows.RawInputButtonListener(
            lambda *_args: None
        )
        listener.set_input_corruption_callback(corruptions.append)
        listener.set_physical_keyboard_tracking_lost_callback(
            tracking_losses.append
        )
        user32 = _FakeMessageLoopUser32(get_message_result=-1)
        kernel32 = _FakeMessageLoopKernel32()

        with mock.patch.object(
            ctypes,
            "windll",
            SimpleNamespace(user32=user32, kernel32=kernel32),
        ):
            listener._run()

        self.assertTrue(listener._ready_event.is_set())
        self.assertIsNone(listener._start_error)
        self.assertEqual(user32.get_message_calls, 1)
        self.assertEqual(corruptions, ["raw_input_listener_exited"])
        self.assertEqual(tracking_losses, ["raw_input_listener_exited"])
        self.assertEqual(len(user32.destroyed_windows), 1)
        self.assertEqual(len(user32.unregistered_classes), 1)
        self.assertIsNone(listener._hwnd)


class PhysicalKeyboardTrackingTests(unittest.TestCase):
    RC003_PATH = r"\\?\hid#vid_2717&pid_32b8#selected"
    KEYBOARD_PATH = r"\\?\hid#vid_046d&pid_c52b#keyboard"

    def setUp(self):
        raw_input_windows._set_physical_keyboard_tracker_active(True)
        self.listener = RecordingListener().listener
        self.listener._normalized_device_path = hid_identity.normalize_device_path(
            self.RC003_PATH
        )

    def tearDown(self):
        raw_input_windows._set_physical_keyboard_tracker_active(False)

    def _send(
        self,
        vkey,
        message=WM_KEYDOWN,
        *,
        device_handle=501,
        device_path=None,
        make_code=0,
        flags=None,
    ):
        user32 = _FakeRawInputUser32(
            _rawkeyboard_body(
                vkey,
                message,
                make_code=make_code,
                flags=flags,
            ),
            device_path=device_path or self.KEYBOARD_PATH,
            device_handle=device_handle,
        )
        self.listener._handle_raw_input(1, _user32=user32)

    def test_non_rc003_keyboard_tracks_ordinary_key_down_and_up(self):
        self._send(ord("H"))
        self.assertTrue(raw_input_windows.physical_key_is_down(ord("H")))

        self._send(ord("H"), WM_KEYUP)
        self.assertFalse(raw_input_windows.physical_key_is_down(ord("H")))

    def test_rc003_keyboard_collection_never_enters_physical_state(self):
        self._send(
            ord("H"),
            device_path=r"\\?\hid#vid_2717&pid_32b8&col02#remote",
        )

        self.assertFalse(raw_input_windows.physical_key_is_down(ord("H")))

    def test_multiple_keyboards_keep_a_key_down_until_every_owner_releases(self):
        self._send(ord("H"), device_handle=501)
        self._send(ord("H"), device_handle=502)
        self._send(ord("H"), WM_KEYUP, device_handle=501)
        self.assertTrue(raw_input_windows.physical_key_is_down(ord("H")))

        self._send(ord("H"), WM_KEYUP, device_handle=502)
        self.assertFalse(raw_input_windows.physical_key_is_down(ord("H")))

    def test_repeated_down_on_one_keyboard_needs_only_one_release(self):
        self._send(ord("H"))
        self._send(ord("H"))
        self._send(ord("H"), WM_KEYUP)

        self.assertFalse(raw_input_windows.physical_key_is_down(ord("H")))

    def test_device_removal_clears_only_that_keyboards_state(self):
        self._send(ord("H"), device_handle=501)
        self._send(ord("J"), device_handle=502)

        self.listener._handle_device_change(raw_input_windows._GIDC_REMOVAL, 501)

        self.assertFalse(raw_input_windows.physical_key_is_down(ord("H")))
        self.assertTrue(raw_input_windows.physical_key_is_down(ord("J")))

    def test_generic_modifiers_are_normalized_to_the_physical_side(self):
        self._send(0x11, make_code=0x1D, flags=raw_input_windows._RI_KEY_E0)

        self.assertTrue(raw_input_windows.physical_key_is_down(0x11))
        self.assertTrue(raw_input_windows.physical_key_is_down(0xA3))
        self.assertFalse(raw_input_windows.physical_key_is_down(0xA2))

    def test_invalid_message_disables_tracking_and_notifies_once(self):
        losses = []
        self.listener.set_physical_keyboard_tracking_lost_callback(losses.append)

        self._send(ord("H"), message=0, flags=0)
        self._send(ord("J"), message=0, flags=0)

        self.assertFalse(raw_input_windows.physical_keyboard_tracking_available())
        self.assertEqual(losses, ["physical_keyboard_message_invalid"])

    def test_break_flag_mismatch_disables_tracking_without_storing_the_key(self):
        losses = []
        self.listener.set_physical_keyboard_tracking_lost_callback(losses.append)

        self._send(ord("H"), WM_KEYDOWN, flags=raw_input_windows._RI_KEY_BREAK)

        self.assertFalse(raw_input_windows.physical_key_is_down(ord("H")))
        self.assertFalse(raw_input_windows.physical_keyboard_tracking_available())
        self.assertEqual(losses, ["physical_keyboard_edge_mismatch"])


class KeyboardBodyTests(unittest.TestCase):
    def test_recognized_vk_down_emits_press(self):
        rec = RecordingListener()
        vk_right = 0x27
        rec.listener._handle_keyboard_body(_rawkeyboard_body(vk_right, WM_KEYDOWN))
        self.assertEqual(rec.events, [("right", True)])

    def test_recognized_vk_up_emits_release(self):
        rec = RecordingListener()
        vk_right = 0x27
        rec.listener._handle_keyboard_body(_rawkeyboard_body(vk_right, WM_KEYDOWN))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(vk_right, WM_KEYUP))
        self.assertEqual(rec.events, [("right", True), ("right", False)])

    def test_repeated_keyboard_keydown_is_one_logical_press(self):
        rec = RecordingListener()
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYUP))
        self.assertEqual(rec.events, [("up", True), ("up", False)])

    def test_distinct_press_after_release_is_not_debounced(self):
        rec = RecordingListener()
        for _ in range(2):
            rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
            rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYUP))
        self.assertEqual(
            rec.events,
            [("up", True), ("up", False), ("up", True), ("up", False)],
        )

    def test_mapping_change_while_held_releases_the_original_keyboard_button(self):
        rec = RecordingListener()
        signature = "keyboard:vkey=0x0026;make=0x0000;flags=0x0000"

        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
        rec.listener.set_physical_bindings({signature: "right"})
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYUP))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYUP))

        self.assertEqual(
            rec.events,
            [("up", True), ("up", False), ("right", True), ("right", False)],
        )

    def test_physical_override_retains_the_real_windows_keyboard_button(self):
        rec = RecordingListener()
        signature = "keyboard:vkey=0x0026;make=0x0000;flags=0x0000"
        rec.listener.set_physical_bindings({signature: "ok"})

        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYUP))

        self.assertEqual(
            [
                (event.button_id, event.windows_button_id, event.is_pressed)
                for event in rec.raw_events
            ],
            [("ok", "up", True), ("ok", "up", False)],
        )

    def test_two_keyboard_signatures_mapped_to_one_button_release_once(self):
        rec = RecordingListener()
        right_signature = "keyboard:vkey=0x0027;make=0x0000;flags=0x0000"
        rec.listener.set_physical_bindings({right_signature: "up"})

        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x27, WM_KEYDOWN))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYUP))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x27, WM_KEYUP))

        self.assertEqual(rec.events, [("up", True), ("up", False)])

    def test_unrecognized_vk_emits_nothing(self):
        rec = RecordingListener()
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x99, WM_KEYDOWN))
        self.assertEqual(rec.events, [])

    def test_real_rc003_mic_vk_f5_emits_mic_press(self):
        rec = RecordingListener()
        vk_f5 = 0x74
        rec.listener._handle_keyboard_body(_rawkeyboard_body(vk_f5, WM_KEYDOWN))
        self.assertEqual(rec.events, [("mic", True)])

    def test_tv_oem3_translation_emits_tv_press(self):
        rec = RecordingListener()
        vk_oem_3 = 0xC0
        rec.listener._handle_keyboard_body(_rawkeyboard_body(vk_oem_3, WM_KEYDOWN))
        self.assertEqual(rec.events, [("tv", True)])

    def test_keyboard_power_translation_emits_power_press(self):
        rec = RecordingListener()
        vk_sleep = 0x5F
        rec.listener._handle_keyboard_body(_rawkeyboard_body(vk_sleep, WM_KEYDOWN))
        self.assertEqual(rec.events, [("power", True)])

    def test_keyboard_event_exposes_observed_values_to_learning_ui(self):
        rec = RecordingListener()
        rec.listener._handle_keyboard_body(
            struct.pack("<HHHHII", 0x5E, 0x0002, 0, 0xFF, WM_KEYDOWN, 0)
        )
        self.assertEqual(len(rec.raw_events), 1)
        self.assertEqual(rec.raw_events[0].button_id, "power")
        self.assertEqual(rec.raw_events[0].vkey, 0xFF)
        self.assertEqual(rec.raw_events[0].make_code, 0x5E)

    def test_untranslated_power_scan_code_emits_power_press(self):
        rec = RecordingListener()
        rec.listener._handle_keyboard_body(
            struct.pack("<HHHHII", 0x5E, 0x0002, 0, 0xFF, WM_KEYDOWN, 0)
        )
        self.assertEqual(rec.events, [("power", True)])

    def test_untranslated_remote_scan_codes_map_back_and_volume(self):
        expected = ((0x6A, "back"), (0x30, "volume_up"), (0x2E, "volume_down"))
        for make_code, button in expected:
            rec = RecordingListener()
            rec.listener._handle_keyboard_body(
                struct.pack("<HHHHII", make_code, 0x0002, 0, 0xFF, WM_KEYDOWN, 0)
            )
            self.assertEqual(rec.events, [(button, True)], msg=hex(make_code))

    def test_too_short_body_is_ignored_without_raising(self):
        rec = RecordingListener()
        corruptions = []
        rec.listener.set_input_corruption_callback(corruptions.append)
        rec.listener._handle_keyboard_body(b"\x00\x00")
        self.assertEqual(rec.events, [])
        self.assertEqual(corruptions, ["keyboard_body_too_short"])

    def test_short_keyboard_body_preserves_state_for_the_real_release(self):
        rec = RecordingListener()
        corruptions = []
        rec.listener.set_input_corruption_callback(corruptions.append)
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
        rec.events.clear()

        rec.listener._handle_keyboard_body(b"\x00\x00")

        self.assertEqual(corruptions, ["keyboard_body_too_short"])
        self.assertEqual(rec.listener._active_keyboard_buttons, frozenset({"up"}))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYUP))
        self.assertEqual(rec.events, [("up", False)])

    def test_every_table_entry_resolves_to_a_known_button(self):
        from ovb_rc003 import device_profile

        for vk, button in raw_input_windows.KEYBOARD_VK_TO_BUTTON.items():
            self.assertIn(button, device_profile.ALL_BUTTON_IDS, msg=f"vk={vk:#x}")


class HidBodyTests(unittest.TestCase):
    def test_single_report_press_emits_event(self):
        rec = RecordingListener()
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x0028])]))
        self.assertEqual(rec.events, [("ok", True)])

    def test_release_emitted_when_usage_disappears(self):
        rec = RecordingListener()
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x0028])]))
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([])]))
        self.assertEqual(rec.events, [("ok", True), ("ok", False)])

    def test_malformed_rawhid_body_is_ignored_without_raising(self):
        rec = RecordingListener()
        corruptions = []
        rec.listener.set_input_corruption_callback(corruptions.append)
        rec.listener._handle_hid_body(b"\x00\x00")
        self.assertEqual(rec.events, [])
        self.assertEqual(corruptions, ["hid_payload_invalid"])

    def test_malformed_hid_body_preserves_state_for_the_real_release(self):
        rec = RecordingListener()
        corruptions = []
        rec.listener.set_input_corruption_callback(corruptions.append)
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x0052])]))
        rec.events.clear()

        rec.listener._handle_hid_body(b"\x00\x00")

        self.assertEqual(corruptions, ["hid_payload_invalid"])
        self.assertEqual(rec.listener._active_hid_buttons, frozenset({"up"}))
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([])]))
        self.assertEqual(rec.events, [("up", False)])

    def test_invalid_individual_hid_report_notifies_corruption(self):
        rec = RecordingListener()
        corruptions = []
        rec.listener.set_input_corruption_callback(corruptions.append)

        rec.listener._handle_hid_body(_rawhid_body([b"short"]))

        self.assertEqual(rec.events, [])
        self.assertEqual(corruptions, ["hid_report_invalid"])

    def test_multiple_reports_in_one_body_both_processed(self):
        rec = RecordingListener()
        rec.listener._handle_hid_body(
            _rawhid_body([_hid_report([0x0028]), _hid_report([0x0028, 0x0052])])
        )
        self.assertEqual(rec.events, [("ok", True), ("up", True)])

    def test_snapshot_releases_old_button_before_pressing_replacement(self):
        rec = RecordingListener()
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x0035])]))
        rec.events.clear()

        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x0052])]))

        self.assertEqual(rec.events, [("tv", False), ("up", True)])

    def test_snapshot_presses_combo_modifier_before_its_target(self):
        rec = RecordingListener()

        rec.listener._handle_hid_body(
            _rawhid_body([_hid_report([0x0052, 0x0035])])
        )

        self.assertEqual(rec.events, [("tv", True), ("up", True)])

    def test_real_rc003_mic_usage_f5_emits_mic_press(self):
        rec = RecordingListener()
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x003E])]))
        self.assertEqual(rec.events, [("mic", True)])

    def test_keyboard_and_hid_sources_share_one_logical_edge(self):
        rec = RecordingListener()
        # The same physical Up key may be surfaced once as a translated
        # keyboard event and once as a raw HID usage.  They must not invoke
        # the mapped action twice, and release only after both sources clear.
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x0052])]))
        self.assertEqual(rec.events, [("up", True)])

        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYUP))
        self.assertEqual(rec.events, [("up", True)])

        rec.listener._handle_hid_body(_rawhid_body([_hid_report([])]))
        self.assertEqual(rec.events, [("up", True), ("up", False)])

    def test_logical_release_keeps_the_source_that_owned_the_press(self):
        events = []
        listener = raw_input_windows.RawInputButtonListener(
            lambda *_args: self.fail("legacy callback should not be used"),
            on_sourced_button_event=lambda button, pressed, source, _windows: events.append(
                (button, pressed, source)
            ),
        )

        listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
        listener._handle_hid_body(_rawhid_body([_hid_report([0x0052])]))
        listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYUP))
        listener._handle_hid_body(_rawhid_body([_hid_report([])]))

        self.assertEqual(
            events,
            [("up", True, "keyboard"), ("up", False, "keyboard")],
        )

    def test_hid_owned_press_keeps_hid_source_when_keyboard_releases_last(self):
        events = []
        listener = raw_input_windows.RawInputButtonListener(
            lambda *_args: self.fail("legacy callback should not be used"),
            on_sourced_button_event=lambda button, pressed, source, _windows: events.append(
                (button, pressed, source)
            ),
        )

        listener._handle_hid_body(_rawhid_body([_hid_report([0x0028])]))
        listener._handle_keyboard_body(_rawkeyboard_body(0x0D, WM_KEYDOWN))
        listener._handle_hid_body(_rawhid_body([_hid_report([])]))
        listener._handle_keyboard_body(_rawkeyboard_body(0x0D, WM_KEYUP))

        self.assertEqual(
            events,
            [("ok", True, "hid"), ("ok", False, "hid")],
        )

    def test_mapping_change_while_held_releases_the_original_hid_button(self):
        rec = RecordingListener()
        signature = "hid:usages=0x0052"

        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x0052])]))
        rec.listener.set_physical_bindings({signature: "right"})
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([])]))
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x0052])]))
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([])]))

        self.assertEqual(
            rec.events,
            [("up", True), ("up", False), ("right", True), ("right", False)],
        )

    def test_physical_override_retains_the_real_windows_hid_button(self):
        rec = RecordingListener()
        signature = "hid:usages=0x0052"
        rec.listener.set_physical_bindings({signature: "ok"})

        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x0052])]))
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([])]))

        self.assertEqual(
            [
                (event.button_id, event.windows_button_id, event.is_pressed)
                for event in rec.raw_events
            ],
            [("ok", "up", True), ("ok", "up", False)],
        )


class StopWithoutStartTests(unittest.TestCase):
    """stop() must be safe to call, and must release stuck buttons, even
    though start() (which touches ctypes/ a real window) was never called -
    self._hwnd stays None, so stop()'s ctypes branch is skipped, exercising
    only pure Python cleanup logic.
    """

    def test_stop_releases_stuck_hid_usages(self):
        rec = RecordingListener()
        rec.listener._handle_hid_body(_rawhid_body([_hid_report([0x0028, 0x0052])]))
        rec.events.clear()
        rec.listener.stop()
        self.assertEqual(set(rec.events), {("ok", False), ("up", False)})

    def test_stop_releases_stuck_keyboard_buttons(self):
        rec = RecordingListener()
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x27, WM_KEYDOWN))
        rec.events.clear()
        rec.listener.stop()
        self.assertEqual(rec.events, [("right", False)])

    def test_stop_preserves_the_real_source_on_forced_release(self):
        events = []
        listener = raw_input_windows.RawInputButtonListener(
            lambda *_args: self.fail("legacy callback should not be used"),
            on_sourced_button_event=lambda button, pressed, source, _windows: events.append(
                (button, pressed, source)
            ),
        )
        listener._handle_keyboard_body(_rawkeyboard_body(0x27, WM_KEYDOWN))
        events.clear()

        listener.stop()

        self.assertEqual(events, [("right", False, "keyboard")])

    def test_stop_is_a_no_op_when_nothing_is_active(self):
        rec = RecordingListener()
        rec.listener.stop()
        self.assertEqual(rec.events, [])

    def test_is_running_is_false_before_start(self):
        rec = RecordingListener()
        self.assertFalse(rec.listener.is_running)


class DeviceChangeTests(unittest.TestCase):
    def test_selected_device_removal_hands_state_loss_to_the_owner(self):
        rec = RecordingListener()
        removed = []
        rec.listener.set_device_removed_callback(lambda: removed.append(True))
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x26, WM_KEYDOWN))
        rec.listener._selected_device_handles.add(123)
        rec.events.clear()

        rec.listener._handle_device_change(raw_input_windows._GIDC_REMOVAL, 123)

        self.assertEqual(removed, [True])
        self.assertEqual(rec.events, [])
        self.assertEqual(rec.listener._active_keyboard_buttons, frozenset())
        self.assertEqual(rec.listener._selected_device_handles, set())

    def test_selected_device_removal_force_releases_without_an_owner_callback(self):
        rec = RecordingListener()
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x27, WM_KEYDOWN))
        rec.listener._selected_device_handles.add(456)
        rec.events.clear()

        rec.listener._handle_device_change(raw_input_windows._GIDC_REMOVAL, 456)

        self.assertEqual(rec.events, [("right", False)])

    def test_arrival_and_unrelated_removal_do_not_clear_active_state(self):
        rec = RecordingListener()
        rec.listener._handle_keyboard_body(_rawkeyboard_body(0x28, WM_KEYDOWN))
        rec.listener._selected_device_handles.add(789)
        rec.events.clear()

        rec.listener._handle_device_change(1, 789)
        rec.listener._handle_device_change(raw_input_windows._GIDC_REMOVAL, 999)

        self.assertEqual(rec.events, [])
        self.assertEqual(rec.listener._active_keyboard_buttons, frozenset({"down"}))
        self.assertEqual(rec.listener._selected_device_handles, {789})


class StartTimeoutInjectionTests(unittest.TestCase):
    """XRBM-018 RETRY 1 P2 #5: exercises start()'s timeout/fail-closed path
    via the ``_run_target`` injection seam (the same dependency-injection
    pattern win32_input.py's ``_sender`` already uses), rather than racing
    ``start_timeout=0.0`` against how fast a real window/thread would start
    on an actual Windows machine - a race that can flip either way depending
    on OS scheduling. This runs on every OS: the injected target never
    touches ctypes/a real window/``ctypes.windll`` at all, so it also never
    needs the Windows-only skip guard.
    """

    def test_a_target_that_never_signals_ready_times_out_deterministically(self):
        rec = RecordingListener()

        def _blocked_target():
            # Never calls self._ready_event.set() - the timeout below is
            # therefore guaranteed to fire, not a race against real work.
            # Polls _stop_event (which _abandon_failed_start() sets first
            # thing) so this test doesn't have to wait out the full
            # stop-join timeout once start() gives up - see
            # ThreadNeverStopsHonestyTests below for that scenario.
            while not rec.listener._stop_event.wait(timeout=0.01):
                pass

        with self.assertRaises(raw_input_windows.RawInputUnavailableError):
            rec.listener.start(
                "some-device-path", start_timeout=0.05, _run_target=_blocked_target
            )
        self.assertFalse(rec.listener.is_running)


class ThreadNeverStopsHonestyTests(unittest.TestCase):
    """XRBM-018 RETRY 1 P1 #2: a timed join that does NOT actually stop the
    background thread must never be reported as "stopped" - is_running must
    keep telling the truth, and a subsequent start() must fail closed
    instead of silently orphaning the still-running thread.
    """

    def test_abandon_after_failed_start_does_not_hide_a_thread_that_wont_stop(self):
        rec = RecordingListener()
        release = threading.Event()

        def _truly_stuck_target():
            # Deliberately ignores _stop_event entirely, unlike the target
            # above - simulates a thread that genuinely will not exit
            # within the join timeout (e.g. blocked deep inside a real
            # Win32 call in production), which is exactly the scenario
            # is_running must not lie about.
            release.wait()

        with self.assertRaises(raw_input_windows.RawInputUnavailableError):
            rec.listener.start(
                "some-device-path", start_timeout=0.05, _run_target=_truly_stuck_target
            )

        # Honest state: the thread really is still alive, so is_running
        # must say so instead of pretending the listener stopped.
        self.assertTrue(rec.listener.is_running)
        self.assertIsNotNone(rec.listener._thread)

        # start() while already (still) running must fail closed instead of
        # silently overwriting self._thread and orphaning the stuck one.
        with self.assertRaises(raw_input_windows.RawInputUnavailableError):
            rec.listener.start("some-other-device-path")

        release.set()  # let the stuck thread exit so it doesn't leak
        rec.listener._thread.join(timeout=2.0)
        self.assertFalse(rec.listener._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
