"""Virtual-key code table and pure key-token resolution.

Split out from the actual Win32 ``SendInput`` call (see app.py) so the token
-> VK mapping is unit-testable without ctypes/user32 on any OS.
"""

from __future__ import annotations

import re
from typing import List, Sequence

# Standard Windows virtual-key codes (winuser.h).
VK_CODES = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "left_ctrl": 0xA2,
    "right_ctrl": 0xA3,
    "lshift": 0xA0,
    "rshift": 0xA1,
    "left_shift": 0xA0,
    "right_shift": 0xA1,
    "lalt": 0xA4,
    "ralt": 0xA5,
    "left_alt": 0xA4,
    "right_alt": 0xA5,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "page_up": 0x21,
    "pageup": 0x21,
    "page_down": 0x22,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "left_win": 0x5B,
    "right_win": 0x5C,
    "volume_mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "apps": 0x5D,
    "caps_lock": 0x14,
    "num_lock": 0x90,
    "scroll_lock": 0x91,
    "print_screen": 0x2C,
    "pause": 0x13,
    "browser_back": 0xA6,
    "browser_forward": 0xA7,
    "media_next": 0xB0,
    "media_previous": 0xB1,
    "media_stop": 0xB2,
    "media_play_pause": 0xB3,
    "semicolon": 0xBA,
    "equals": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "slash": 0xBF,
    "backtick": 0xC0,
    "left_bracket": 0xDB,
    "backslash": 0xDC,
    "right_bracket": 0xDD,
    "quote": 0xDE,
    "numpad_multiply": 0x6A,
    "numpad_add": 0x6B,
    "numpad_subtract": 0x6D,
    "numpad_decimal": 0x6E,
    "numpad_divide": 0x6F,
}

for _digit in range(10):
    VK_CODES[str(_digit)] = 0x30 + _digit
for _letter_ord in range(ord("a"), ord("z") + 1):
    VK_CODES[chr(_letter_ord)] = 0x41 + (_letter_ord - ord("a"))
for _function in range(1, 25):
    VK_CODES[f"f{_function}"] = 0x6F + _function
for _digit in range(10):
    VK_CODES[f"numpad{_digit}"] = 0x60 + _digit

# NumLock-off keys share a VK with the navigation cluster, but not its E0
# identity. The current VK-only storage cannot preserve that distinction.
KEYPAD_NAVIGATION_KEYS = {
    0x0C: ("numpad_clear", 0x4C),
    0x21: ("numpad_page_up", 0x49), 0x22: ("numpad_page_down", 0x51),
    0x23: ("numpad_end", 0x4F), 0x24: ("numpad_home", 0x47),
    0x25: ("numpad_left", 0x4B), 0x26: ("numpad_up", 0x48),
    0x27: ("numpad_right", 0x4D), 0x28: ("numpad_down", 0x50),
    0x2D: ("numpad_insert", 0x52), 0x2E: ("numpad_delete", 0x53),
}


# Labels and accepted spellings share the runtime key vocabulary. Labels
# describe keys, not the text produced with Shift or an input method enabled.
KEY_SYMBOL_TOKENS = {
    ";": "semicolon", "=": "equals", ",": "comma", "-": "minus",
    ".": "period", "/": "slash", chr(96): "backtick", "[": "left_bracket",
    "\\": "backslash", "]": "right_bracket", "'": "quote",
}
# Qt fallback only: Shift must remain a separate recorded modifier.
KEY_SHIFTED_SYMBOL_TOKENS = dict(zip(
    ':<>_?~{}|"!@#$%^&*()', (
        "semicolon", "comma", "period", "minus", "slash", "backtick",
        "left_bracket", "right_bracket", "backslash", "quote",
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
    )
))
KEY_SHIFTED_SYMBOL_TOKENS["+"] = "equals"
KEY_LABELS = {
    "ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "win": "Win",
    "lctrl": "左 Ctrl", "rctrl": "右 Ctrl",
    "lshift": "左 Shift", "rshift": "右 Shift",
    "lalt": "左 Alt", "ralt": "右 Alt", "lwin": "左 Win", "rwin": "右 Win",
    "backspace": "Backspace", "tab": "Tab", "enter": "Enter",
    "escape": "Esc", "space": "Space", "page_up": "PageUp",
    "page_down": "PageDown", "home": "Home", "end": "End",
    "left": "←", "right": "→", "up": "↑", "down": "↓",
    "insert": "Insert", "delete": "Delete", "apps": "菜单键",
    "caps_lock": "CapsLock", "num_lock": "NumLock",
    "scroll_lock": "ScrollLock", "print_screen": "PrintScreen", "pause": "Pause",
    "browser_back": "浏览器后退", "browser_forward": "浏览器前进",
    "volume_mute": "静音键", "volume_down": "音量减", "volume_up": "音量加",
    "media_next": "下一曲", "media_previous": "上一曲",
    "media_stop": "停止播放", "media_play_pause": "播放暂停",
    "numpad_add": "小键盘加号", "numpad_subtract": "小键盘减号",
    "numpad_multiply": "小键盘乘号", "numpad_divide": "小键盘除号",
    "numpad_decimal": "小键盘小数点",
    "numpad_enter": "小键盘 Enter", "unsupported_key": "未识别按键",
}
KEY_LABELS.update({token: symbol for symbol, token in KEY_SYMBOL_TOKENS.items()})
KEY_LABELS.update({str(n): str(n) for n in range(10)})
KEY_LABELS.update({chr(n): chr(n).upper() for n in range(ord("a"), ord("z") + 1)})
KEY_LABELS.update({f"f{n}": f"F{n}" for n in range(1, 25)})
KEY_LABELS.update({f"numpad{n}": f"小键盘 {n}" for n in range(10)})
KEY_LABELS.update({
    token: "小键盘 " + KEY_LABELS.get(token.removeprefix("numpad_"), "Clear")
    for token, _scan in KEYPAD_NAVIGATION_KEYS.values()
})

UNSUPPORTED_KEY_MESSAGES = {
    "numpad_enter": "暂不支持区分小键盘 Enter，请改用主键盘 Enter。",
    "unsupported_key": "无法识别录入的按键，请重新录入或手动填写键名。",
    **{
        token: f"暂不支持区分{KEY_LABELS[token]}；请开启 NumLock 后录入数字键，或改用主键盘导航键。"
        for token, _scan in KEYPAD_NAVIGATION_KEYS.values()
    },
}

_CANONICAL_ALIASES = {
    "esc": "escape", "pageup": "page_up", "pagedown": "page_down",
    "left_ctrl": "lctrl", "right_ctrl": "rctrl",
    "left_shift": "lshift", "right_shift": "rshift",
    "left_alt": "lalt", "right_alt": "ralt",
    "left_win": "lwin", "right_win": "rwin",
}


def _text_key(value: str) -> str:
    return "".join(value.casefold().split())


KEY_TEXT_ALIASES = {
    _text_key(label): token for token, label in KEY_LABELS.items()
}
KEY_TEXT_ALIASES.update({
    "control": "ctrl", "leftctrl": "lctrl", "rightctrl": "rctrl",
    "leftshift": "lshift", "rightshift": "rshift",
    "leftalt": "lalt", "rightalt": "ralt",
    "leftwin": "lwin", "rightwin": "rwin",
    "del": "delete", "ins": "insert", "return": "enter",
    "prtsc": "print_screen", "prtscr": "print_screen",
    "pgup": "page_up", "pgdn": "page_down", "制表": "tab",
    "上": "up", "下": "down", "左": "left", "右": "right",
    "numpadenter": "numpad_enter", "小键盘回车": "numpad_enter",
})


KEY_TEXT_ALIASES.update({
    "控制": "ctrl",
    "控制键": "ctrl",
    "左控制": "lctrl",
    "左控制键": "lctrl",
    "左ctrl": "lctrl",
    "右控制": "rctrl",
    "右控制键": "rctrl",
    "右ctrl": "rctrl",
    "左shift": "lshift",
    "右shift": "rshift",
    "左alt": "lalt",
    "右alt": "ralt",
    "windows": "win",
    "windows键": "win",
    "win键": "win",
    "徽标键": "win",
    "左win": "lwin",
    "左windows": "lwin",
    "左windows键": "lwin",
    "右win": "rwin",
    "右windows": "rwin",
    "右windows键": "rwin",
    "左箭头": "left",
    "左方向键": "left",
    "方向左": "left",
    "右箭头": "right",
    "右方向键": "right",
    "方向右": "right",
    "上箭头": "up",
    "上方向键": "up",
    "方向上": "up",
    "下箭头": "down",
    "下方向键": "down",
    "方向下": "down",
    "空格": "space",
    "空格键": "space",
    "回车": "enter",
    "回车键": "enter",
    "退格": "backspace",
    "退格键": "backspace",
    "删除": "delete",
    "删除键": "delete",
    "菜单键": "apps",
})


def canonical_key_token(token: str) -> str:
    key = token.strip().lower()
    return _CANONICAL_ALIASES.get(key, key)


_VK_TO_TOKEN = {}
for _token, _vk in VK_CODES.items():
    _VK_TO_TOKEN.setdefault(_vk, canonical_key_token(_token))
_VK_TO_TOKEN[0x5B] = "lwin"


def key_token_for_vk(vk: int) -> str:
    """Name a VK without inferring any missing physical-key information."""
    return _VK_TO_TOKEN.get(vk, f"vk_{vk:02x}")


def key_token_from_text(text: str) -> str:
    """Resolve one complete user-entered key name, without guessing Shift."""
    key = _text_key(text)
    if key in KEY_SHIFTED_SYMBOL_TOKENS:
        base = key_label(KEY_SHIFTED_SYMBOL_TOKENS[key])
        raise UnknownKeyTokenError(f"请明确填写 Shift + {base}，或用按键录入“{text}”。")
    token = canonical_key_token(KEY_TEXT_ALIASES.get(key, key))
    visible_vk = re.fullmatch(r"键码0x([0-9a-f]{2})", token)
    labelled_vk = re.fullmatch(r"(.+)（键码0x([0-9a-f]{2})）", token)
    if labelled_vk:
        raw_token = "vk_" + labelled_vk.group(2)
        if _text_key(key_label(raw_token)) == key:
            token = raw_token
    if visible_vk:
        token = "vk_" + visible_vk.group(1)
    try:
        resolve_vk_codes((token,))
    except UnknownKeyTokenError as exc:
        if token in UNSUPPORTED_KEY_MESSAGES or _DYNAMIC_VK_TOKEN.fullmatch(token):
            raise
        raise UnknownKeyTokenError(f"不认识按键“{text.strip()}”，请检查键名或使用按键录入。") from exc
    # A raw VK may be the legacy trigger even when it names a modifier.
    # Chord parsing owns any safe conversion to a named token.
    return token


def key_label(token: str) -> str:
    key = canonical_key_token(token)
    dynamic = _DYNAMIC_VK_TOKEN.fullmatch(key)
    if dynamic:
        vk = int(dynamic.group(1), 16)
        label = KEY_LABELS.get(key_token_for_vk(vk))
        code = f"键码 0x{vk:02X}"
        return f"{label}（{code}）" if label else code
    return KEY_LABELS.get(key, key)


class UnknownKeyTokenError(ValueError):
    pass


_DYNAMIC_VK_TOKEN = re.compile(r"^vk_([0-9a-f]{2})$")


def resolve_vk_codes(tokens: Sequence[str]) -> List[int]:
    """Resolve an ordered sequence of key tokens (e.g. ``("win", "d")``) into
    Windows virtual-key codes, raising if any token is unrecognized.
    """

    codes = []
    for token in tokens:
        key = token.strip().lower()
        if key in UNSUPPORTED_KEY_MESSAGES:
            raise UnknownKeyTokenError(UNSUPPORTED_KEY_MESSAGES[key])
        if key in VK_CODES:
            codes.append(VK_CODES[key])
            continue
        dynamic = _DYNAMIC_VK_TOKEN.fullmatch(key)
        if dynamic is None:
            raise UnknownKeyTokenError(f"unknown key token: {token!r}")
        vk = int(dynamic.group(1), 16)
        if vk in {0x00, 0xFF}:
            raise UnknownKeyTokenError(f"键码 0x{vk:02X} 无效，请重新录入。")
        if vk in {0x01, 0x02, 0x04, 0x05, 0x06}:
            raise UnknownKeyTokenError("鼠标键码不能用作键盘快捷键，请选择已有的鼠标操作。")
        if vk in {0xE5, 0xE7}:
            raise UnknownKeyTokenError("输入法或文字输入事件不能还原为快捷键，请直接录入实体键或手动填写键名。")
        codes.append(vk)
    return codes
