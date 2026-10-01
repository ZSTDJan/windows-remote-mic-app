"""Hotkey specification parsing/serialization.

Deliberately simple compared to the upstream reference's low-level-keyboard-
hook shortcut capture: it models a hotkey as an ordered tuple of modifier
tokens plus one final trigger token, serialized as "mod+mod+key" (e.g.
"win+h"). A chord made entirely from modifier keys, such as
"lctrl+win", stores its last modifier as that final trigger token so it can
use the same runtime and serialization path as ordinary key combinations.
The actual OS-level key-down/key-up hooking lives in the Windows-only app
wiring (app.py), not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from . import key_mapping, win32_keys

_MODIFIER_ORDER = (
    "ctrl", "shift", "alt", "win",
    "lctrl", "rctrl", "lshift", "rshift", "lalt", "ralt", "lwin", "rwin",
)
_VALID_MODIFIERS = frozenset(_MODIFIER_ORDER)
_TOKEN_ALIASES = {
    token: win32_keys.canonical_key_token(token)
    for token in win32_keys.VK_CODES
    if token not in _VALID_MODIFIERS
    and win32_keys.canonical_key_token(token) in _VALID_MODIFIERS
}


class HotkeyParseError(ValueError):
    pass


@dataclass(frozen=True)
class HotkeySpec:
    modifiers: Tuple[str, ...]
    key: str

    def __post_init__(self) -> None:
        if not self.key:
            raise HotkeyParseError("hotkey must have a trigger key")
        for modifier in self.modifiers:
            if modifier not in _VALID_MODIFIERS:
                raise HotkeyParseError(f"unknown modifier: {modifier!r}")

    def serialize(self) -> str:
        ordered = tuple(m for m in _MODIFIER_ORDER if m in self.modifiers)
        return "+".join((*ordered, self.key.lower()))

    @classmethod
    def from_user_text(cls, text: str, *, mapping: bool = False) -> "HotkeySpec":
        """Parse a complete editor value; persisted tokens still use parse()."""
        value = str(text).strip().replace("＋", "+")
        parts = [part.strip() for part in value.split("+")]
        if not value or any(not part for part in parts):
            raise HotkeyParseError(
                "请补全 + 两侧的按键名；主键盘加号用 Shift+=，小键盘加号填“小键盘加号”。"
            )
        try:
            tokens = [win32_keys.key_token_from_text(p) for p in parts]
        except win32_keys.UnknownKeyTokenError as exc:
            raise HotkeyParseError(str(exc)) from exc
        # Persisted raw modifier VKs were parsed as the final trigger, even
        # when written before a named modifier. Retain that role on edit.
        # With an ordinary key present, raw modifier aliases can still be
        # normalized like other modifier spellings.
        candidates = [t for t in tokens if t not in _VALID_MODIFIERS]
        raw_trigger = candidates[0] if len(candidates) == 1 else None
        tokens = [
            t if not t.startswith("vk_") or (
                t == raw_trigger
                and win32_keys.key_token_for_vk(int(t[3:], 16)) in _VALID_MODIFIERS
            ) else win32_keys.key_token_for_vk(int(t[3:], 16))
            for t in tokens
        ]
        if len([t for t in tokens if t not in _VALID_MODIFIERS]) > 1:
            raise HotkeyParseError("组合键只能包含一个普通键，另可搭配 Ctrl、Shift、Alt、Win。")
        # Repeated modifiers are harmless spellings; repeated ordinary keys
        # are an invalid chord and must not disappear during normalization.
        # In a modifier-only chord, keep the last modifier as its trigger.
        tokens = list(reversed(dict.fromkeys(reversed(tokens))))
        if mapping and tokens == ["win"]:
            tokens = ["lwin"]
        if len(tokens) == 1 and tokens[0] in {"ctrl", "shift", "alt", "win"}:
            raise HotkeyParseError(f"单独使用 {win32_keys.key_label(tokens[0])} 时，请明确填写左键或右键。")
        return cls.parse("+".join(tokens))

    @classmethod
    def parse(cls, text: str) -> "HotkeySpec":
        if not text or not text.strip():
            raise HotkeyParseError("hotkey text must not be empty")
        tokens = [
            _TOKEN_ALIASES.get(token.strip().lower(), token.strip().lower())
            for token in text.split("+")
            if token.strip()
        ]
        if not tokens:
            raise HotkeyParseError(f"could not parse hotkey: {text!r}")
        # A directional modifier can itself be the trigger key (the voice
        # client's HOLD mode uses the physical right Alt key alone).
        if len(tokens) == 1 and tokens[0] in {
            "lctrl", "rctrl", "lshift", "rshift", "lalt", "ralt", "lwin", "rwin"
        }:
            return cls(modifiers=(), key=tokens[0])
        modifiers = tuple(modifier for modifier in _MODIFIER_ORDER if modifier in tokens)
        keys = [token for token in tokens if token not in _VALID_MODIFIERS]
        if not keys:
            # A modifier-only chord still has a meaningful edge: press the
            # earlier modifiers first and use the final modifier as the
            # trigger. This is what makes settings such as left Ctrl + Win
            # representable instead of incorrectly rejecting them as having
            # no "real" key.
            if len(tokens) < 2:
                raise HotkeyParseError(
                    f"hotkey must contain at least two keys: {text!r}"
                )
            trigger = tokens[-1]
            modifiers = tuple(
                modifier
                for modifier in _MODIFIER_ORDER
                if modifier in tokens[:-1]
            )
            return cls(modifiers=modifiers, key=trigger)
        if len(keys) != 1:
            raise HotkeyParseError(
                f"hotkey must have exactly one non-modifier key: {text!r}"
            )
        return cls(modifiers=modifiers, key=keys[0])


def format_hotkey_text(text: str) -> str:
    """Project canonical tokens to editable labels without changing storage."""
    try:
        spec = HotkeySpec.parse(text)
        tokens = (*spec.modifiers, spec.key)
    except HotkeyParseError:
        return text
    return " + ".join(win32_keys.key_label(token) for token in tokens)


# The established right-Alt hold trigger remains the fresh-install default.
# Existing config files keep their saved value because config.load_config()
# normalizes persisted voice fields before merging defaults.
DEFAULT_VOICE_HOTKEY = HotkeySpec.parse(
    key_mapping.voice_hotkey_for_trigger_mode(key_mapping.VoiceTriggerMode.HOLD)
)
