"""Pure, platform-independent layout data for the RC003 product-photo
hotspots and the physical-button display strings shown next to each mapping
row (XRBM-030).

Hotspot coordinates are calibrated for the bundled RC003 product photo and
stored as fractions (0..1) of the photo's own width/height, so they are
resolution-independent. ButtonsPage preserves each source photo's aspect
ratio and positions hotspots relative to the rendered image, including its
transparent padding, so the same fractions apply at any display size.

This module has no Tk/Qt import at all, so it is directly unit-testable
(see tests/test_remote_layout.py) and importable from a plain ``--dry-run``
smoke check without any GUI toolkit installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from . import device_profile

# Reverse of device_profile.BUTTON_USAGE_IDS (usage -> button_id); "mic" has
# no HID usage at all (it arrives over the ATVV BLE control channel, not a
# keyboard-page HID report - see device_profile.py's module docstring).
_BUTTON_ID_TO_HID_USAGE: Dict[str, int] = {
    button_id: usage for usage, button_id in device_profile.BUTTON_USAGE_IDS.items()
}

# Chinese name of the PHYSICAL button itself (not the action it is currently
# mapped to), plus the fixed ATVV microphone button.
BUTTON_DISPLAY_NAMES: Dict[str, str] = {
    "power": "电源键",
    "up": "上键",
    "left": "左键",
    "ok": "确定键",
    "right": "右键",
    "down": "下键",
    "back": "返回键",
    "volume_up": "音量 + 键",
    "home": "主页键",
    "volume_down": "音量 − 键",
    "menu": "菜单键",
    "tv": "TV 键",
    "mic": "麦克风键",
}


def hid_usage_display(button_id: str) -> str:
    """The HID usage column text for a mapping row. The mic button is
    deliberately never given a fabricated HID usage id - it is driven by the
    ATVV control channel, not a keyboard-page HID report (see
    device_profile.py), so this says so explicitly instead of inventing one.
    """

    usage = _BUTTON_ID_TO_HID_USAGE.get(button_id)
    if usage is None:
        return "ATVV 语音控制通道（非 HID）"
    return f"0x{usage:04X}"


@dataclass(frozen=True)
class ButtonHotspot:
    """One clickable region on the product photo, as a fraction (0..1) of
    the photo's own width/height - see module docstring for provenance.
    """

    button_id: str
    x: float
    y: float
    width: float
    height: float
    is_voice: bool = False


# Exactly the 13 physical buttons the real RC003 has (12 ordinary HID
# buttons + the fixed microphone) - deliberately excludes "volume_mute"
# (device_profile.BUTTON_USAGE_IDS carries it only for HID usage-table
# completeness; the physical remote has no dedicated mute key - see
# key_mapping.py's module docstring and settings_ui.py's
# _USER_FACING_BUTTON_IDS, which this table's ordering matches).
BUTTON_HOTSPOTS: Tuple[ButtonHotspot, ...] = (
    ButtonHotspot("power", x=0.386, y=0.099, width=0.15, height=0.072),
    ButtonHotspot("mic", x=0.630, y=0.099, width=0.15, height=0.072, is_voice=True),
    ButtonHotspot("up", x=0.502, y=0.179, width=0.18, height=0.065),
    ButtonHotspot("left", x=0.362, y=0.246, width=0.15, height=0.080),
    ButtonHotspot("ok", x=0.502, y=0.246, width=0.19, height=0.095),
    ButtonHotspot("right", x=0.638, y=0.246, width=0.15, height=0.080),
    ButtonHotspot("down", x=0.502, y=0.317, width=0.18, height=0.065),
    ButtonHotspot("back", x=0.406, y=0.389, width=0.17, height=0.080),
    ButtonHotspot("volume_up", x=0.604, y=0.390, width=0.16, height=0.080),
    ButtonHotspot("home", x=0.406, y=0.479, width=0.17, height=0.080),
    ButtonHotspot("volume_down", x=0.604, y=0.480, width=0.16, height=0.080),
    ButtonHotspot("menu", x=0.406, y=0.569, width=0.17, height=0.080),
    ButtonHotspot("tv", x=0.604, y=0.569, width=0.17, height=0.080),
)

# button_id -> hotspot, and the fixed display order every list/model uses -
# both derived from BUTTON_HOTSPOTS so there is exactly one source of truth.
BUTTON_HOTSPOTS_BY_ID: Dict[str, ButtonHotspot] = {
    hotspot.button_id: hotspot for hotspot in BUTTON_HOTSPOTS
}
BUTTON_ORDER: Tuple[str, ...] = tuple(hotspot.button_id for hotspot in BUTTON_HOTSPOTS)


CHROMECAST_HOTSPOTS = (
    ButtonHotspot("up", .50, .060, .22, .045),
    ButtonHotspot("left", .20, .152, .17, .065),
    ButtonHotspot("ok", .50, .152, .28, .095),
    ButtonHotspot("right", .81, .152, .17, .065),
    ButtonHotspot("down", .50, .250, .22, .045),
    ButtonHotspot("back", .26, .355, .28, .090),
    ButtonHotspot("mic", .73, .355, .28, .090, is_voice=True),
    ButtonHotspot("home", .26, .483, .28, .090),
    ButtonHotspot("volume_mute", .73, .483, .28, .090),
    ButtonHotspot("youtube", .26, .612, .28, .090),
    ButtonHotspot("netflix", .73, .612, .28, .090),
    ButtonHotspot("power", .26, .733, .16, .052),
    ButtonHotspot("input_source", .73, .733, .16, .052),
    # Side rocker: the supplied front photo shows only its right edge.
    ButtonHotspot("volume_up", .98, .208, .04, .035),
    ButtonHotspot("volume_down", .98, .252, .04, .035),
)
CHROMECAST_DISPLAY_NAMES = {
    **{key: value for key, value in BUTTON_DISPLAY_NAMES.items() if key not in {"menu", "tv"}},
    "mic": "语音 / Assistant", "volume_mute": "静音键",
    "youtube": "YouTube", "netflix": "Netflix", "input_source": "输入源",
}
CHROMECAST_REPORT_CODES = {
    "power": 0x01, "up": 0x03, "down": 0x04, "left": 0x05, "right": 0x06,
    "ok": 0x07, "volume_mute": 0x08, "home": 0x0A, "back": 0x0B,
    "volume_up": 0x0C, "volume_down": 0x0D, "youtube": 0x0E,
    "netflix": 0x0F, "input_source": 0x11,
}


def button_order(profile: str = "xiaomi-rc003") -> Tuple[str, ...]:
    if profile == "chromecast-remote":
        return tuple(item.button_id for item in CHROMECAST_HOTSPOTS)
    return BUTTON_ORDER


def display_names(profile: str = "xiaomi-rc003") -> Dict[str, str]:
    return CHROMECAST_DISPLAY_NAMES if profile == "chromecast-remote" else BUTTON_DISPLAY_NAMES


def default_actions(profile: str = "xiaomi-rc003") -> dict:
    from . import key_mapping
    defaults = key_mapping.default_button_actions()
    if profile != "chromecast-remote":
        return defaults
    result = {button: defaults.get(button, key_mapping.ButtonAction(key_mapping.ActionKind.DISABLED))
              for button in button_order(profile)}
    result["volume_mute"] = key_mapping.ButtonAction(key_mapping.ActionKind.SYSTEM_VOLUME_MUTE)
    return result


def hotspot_for(button_id: str, profile: str = "xiaomi-rc003") -> Optional[ButtonHotspot]:
    if profile == "chromecast-remote":
        return next((item for item in CHROMECAST_HOTSPOTS if item.button_id == button_id), None)
    return BUTTON_HOTSPOTS_BY_ID.get(button_id)
