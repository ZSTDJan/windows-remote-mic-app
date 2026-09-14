"""Manual Doubao probe using an existing Interception installation.

This diagnostics-only script is not part of the application runtime. It loads
an explicitly supplied DLL, matches only an RC003 keyboard, and never installs
a driver or changes persistent configuration. Without --send it is read-only.
"""

from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import sys
import time


RC003_MARKERS = ("VID_2717", "PID_32B8")
KEYBOARD_DEVICE_IDS = range(1, 11)
RIGHT_ALT_SCAN_CODE = 0x38
KEY_UP = 0x0001
KEY_E0 = 0x0002
DOUBAO_VOICE_WINDOW_CLASS = "OimeVoiceWaveWindow"


class KeyStroke(ctypes.Structure):
    _fields_ = (
        ("code", ctypes.c_ushort),
        ("state", ctypes.c_ushort),
        ("information", ctypes.c_uint),
    )


class InterceptionApi:
    def __init__(self, dll_path: Path) -> None:
        dll = ctypes.WinDLL(str(dll_path), use_last_error=True)
        dll.interception_create_context.argtypes = ()
        dll.interception_create_context.restype = ctypes.c_void_p
        dll.interception_destroy_context.argtypes = (ctypes.c_void_p,)
        dll.interception_destroy_context.restype = None
        dll.interception_get_hardware_id.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_wchar),
            ctypes.c_uint,
        )
        dll.interception_get_hardware_id.restype = ctypes.c_uint
        dll.interception_send.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(KeyStroke),
            ctypes.c_uint,
        )
        dll.interception_send.restype = ctypes.c_int
        context = dll.interception_create_context()
        if not context:
            raise RuntimeError(
                "Interception context could not be created; the driver is not "
                "installed or is unavailable"
            )
        self._dll = dll
        self.context = context

    def close(self) -> None:
        if self.context:
            self._dll.interception_destroy_context(self.context)
            self.context = None

    def hardware_id(self, device: int) -> str:
        buffer = ctypes.create_unicode_buffer(512)
        copied = self._dll.interception_get_hardware_id(
            self.context, device, buffer, ctypes.sizeof(buffer)
        )
        return buffer.value if copied else ""

    def send_right_alt(self, device: int, *, key_up: bool) -> bool:
        stroke = KeyStroke(
            RIGHT_ALT_SCAN_CODE,
            KEY_E0 | (KEY_UP if key_up else 0),
            0,
        )
        return (
            self._dll.interception_send(
                self.context, device, ctypes.byref(stroke), 1
            )
            == 1
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List the RC003 Interception keyboard or explicitly send one "
            "right-Alt hold to test Doubao."
        )
    )
    parser.add_argument(
        "--dll",
        default=os.environ.get("INTERCEPTION_DLL"),
        help="path to interception.dll (or set INTERCEPTION_DLL)",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="send the test; without this flag the probe is read-only",
    )
    parser.add_argument(
        "--device",
        type=int,
        help="device ID to use when multiple RC003 keyboards are listed",
    )
    parser.add_argument("--delay-seconds", type=float, default=3.0)
    parser.add_argument("--hold-ms", type=int, default=1000)
    return parser.parse_args()


def _dll_path(value: str | None) -> Path:
    if not value:
        raise FileNotFoundError("pass --dll with the Axonkey interception.dll path")
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileNotFoundError(f"Interception DLL does not exist: {value}") from exc
    if path.name.casefold() != "interception.dll":
        raise FileNotFoundError("the DLL path must end with interception.dll")
    return path


def _rc003_devices(api: InterceptionApi) -> list[tuple[int, str]]:
    devices = []
    for device in KEYBOARD_DEVICE_IDS:
        hardware_id = api.hardware_id(device)
        normalized = hardware_id.upper()
        if all(marker in normalized for marker in RC003_MARKERS):
            devices.append((device, hardware_id))
    return devices


def _doubao_voice_window_visible() -> bool:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
    user32.FindWindowW.restype = ctypes.c_void_p
    return bool(user32.FindWindowW(DOUBAO_VOICE_WINDOW_CLASS, None))


def main() -> int:
    args = _parse_args()
    if sys.platform != "win32":
        print("ERROR: Windows only", file=sys.stderr)
        return 2
    if args.delay_seconds < 0 or args.hold_ms < 200:
        print("ERROR: delay must be nonnegative and hold must be at least 200ms")
        return 2

    try:
        path = _dll_path(args.dll)
        api = InterceptionApi(path)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    try:
        devices = _rc003_devices(api)
        for device, hardware_id in devices:
            print(f"RC003 device={device} hardware_id={hardware_id}")
        if not devices:
            print("ERROR: no RC003 keyboard was found", file=sys.stderr)
            return 4
        if not args.send:
            print("READ-ONLY: no key was sent; add --send for the Doubao test")
            return 0

        selected = args.device
        if selected is None and len(devices) == 1:
            selected = devices[0][0]
        if selected not in {device for device, _hardware_id in devices}:
            print("ERROR: select one listed RC003 device with --device", file=sys.stderr)
            return 4

        print(
            f"Right Alt will start in {args.delay_seconds:g}s; focus a text "
            "field with Doubao active."
        )
        time.sleep(args.delay_seconds)
        if not api.send_right_alt(selected, key_up=False):
            api.send_right_alt(selected, key_up=True)
            print("ERROR: right-Alt key-down failed", file=sys.stderr)
            return 5

        voice_window_seen = False
        try:
            deadline = time.monotonic() + args.hold_ms / 1000.0
            while time.monotonic() < deadline:
                voice_window_seen |= _doubao_voice_window_visible()
                time.sleep(0.02)
        finally:
            released = api.send_right_alt(selected, key_up=True)
        if not released:
            print("ERROR: right-Alt key-up failed", file=sys.stderr)
            return 5
        if voice_window_seen:
            print("PASS: Doubao voice window appeared")
            return 0
        print("NOT CONFIRMED: Doubao voice window was not observed", file=sys.stderr)
        return 6
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
