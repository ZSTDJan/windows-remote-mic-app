"""Read and synchronize supported voice shortcuts on Windows.

Sogou exposes a stable on-disk shortcut setting that can be read and updated
without opening its UI. Doubao exposes its current hold shortcut in its user
configuration. WeType is read only from its visible settings page on explicit
refresh; its hidden status-bar hint can retain an obsolete shortcut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from typing import Optional

from . import hotkey, product_identity, voice_program_manager, win32_keys


DEFAULT_PROVIDER_HOTKEYS = {
    voice_program_manager.VOICE_PROGRAM_NONE: "ralt",
    voice_program_manager.VOICE_PROGRAM_SOGOU: "rctrl",
    voice_program_manager.VOICE_PROGRAM_WETYPE: "lctrl+lwin",
    voice_program_manager.VOICE_PROGRAM_DOUBAO_IME: "ralt",
    voice_program_manager.VOICE_PROGRAM_CUSTOM: "ralt",
}

_SOGOU_CONFIG_RELATIVE_PATH = Path("sogou_voice_assistant_pc") / "config.json"
_DOUBAO_CONFIG_RELATIVE_PATH = Path("DoubaoIme") / "conf" / "config.json"
_SOGOU_PROCESS_NAME = "sogou_voice_assistant.exe"
_WETYPE_MODIFIER_TEXT = re.compile(r"^(左|右)?(Ctrl|Shift|Alt|Win)$", re.I)
_WETYPE_KEY_NAMES = {
    "空格": "space",
    "回车": "enter",
    "制表": "tab",
    "退格": "backspace",
    "删除": "delete",
    "上": "up",
    "下": "down",
    "左": "left",
    "右": "right",
}
# Faithfully injectable subset of Sogou Voice Assistant 1.0.1.3272's
# Windows shortcut vocabulary. NumpadEnter needs scan-code identity that the
# current Remote Mic shortcut model cannot preserve.
_REMOTE_TO_SOGOU_TOKEN = {
    "ctrl": "LeftCtrl",
    "lctrl": "LeftCtrl",
    "rctrl": "RightCtrl",
    "shift": "LeftShift",
    "lshift": "LeftShift",
    "rshift": "RightShift",
    "alt": "LeftAlt",
    "lalt": "LeftAlt",
    "ralt": "RightAlt",
    "win": "LeftMeta",
    "lwin": "LeftMeta",
    "rwin": "RightMeta",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "space": "Space",
    "enter": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "page_up": "PageUp",
    "pagedown": "PageDown",
    "page_down": "PageDown",
    "caps_lock": "CapsLock",
    "num_lock": "NumLock",
    "scroll_lock": "ScrollLock",
    "print_screen": "PrintScreen",
    "minus": "Minus",
    "equals": "Equal",
    "left_bracket": "BracketLeft",
    "right_bracket": "BracketRight",
    "backslash": "Backslash",
    "semicolon": "Semicolon",
    "quote": "Quote",
    "comma": "Comma",
    "period": "Period",
    "slash": "Slash",
    "backtick": "Backquote",
    "numpad_add": "NumpadAdd",
    "numpad_subtract": "NumpadSubtract",
    "numpad_multiply": "NumpadMultiply",
    "numpad_divide": "NumpadDivide",
    "numpad_decimal": "NumpadDecimal",
}
for _letter in "abcdefghijklmnopqrstuvwxyz":
    _REMOTE_TO_SOGOU_TOKEN[_letter] = _letter.upper()
for _digit in "0123456789":
    _REMOTE_TO_SOGOU_TOKEN[_digit] = _digit
    _REMOTE_TO_SOGOU_TOKEN[f"numpad{_digit}"] = f"Numpad{_digit}"
for _function in range(1, 25):
    _REMOTE_TO_SOGOU_TOKEN[f"f{_function}"] = f"F{_function}"
_SOGOU_TO_REMOTE_TOKEN = {
    value.casefold(): key for key, value in _REMOTE_TO_SOGOU_TOKEN.items()
}
_SOGOU_TO_REMOTE_TOKEN.update(
    {
        "ctrl": "ctrl",
        "shift": "shift",
        "alt": "alt",
        "meta": "win",
        "leftctrl": "lctrl",
        "rightctrl": "rctrl",
        "leftshift": "lshift",
        "rightshift": "rshift",
        "leftalt": "lalt",
        "rightalt": "ralt",
        "leftmeta": "lwin",
        "rightmeta": "rwin",
        # Older builds and existing test data used Win instead of Meta.
        "leftwin": "lwin",
        "rightwin": "rwin",
        "pageup": "page_up",
        "pagedown": "page_down",
        "escape": "escape",
    }
)

_MODIFIER_FAMILY_BY_TOKEN = {
    "ctrl": "ctrl",
    "lctrl": "ctrl",
    "rctrl": "ctrl",
    "shift": "shift",
    "lshift": "shift",
    "rshift": "shift",
    "alt": "alt",
    "lalt": "alt",
    "ralt": "alt",
    "win": "win",
    "lwin": "win",
    "rwin": "win",
}
# WeType 2.1.3.18 applies these checks in its Windows voice shortcut recorder.
_WETYPE_BLOCKED_KEYS = frozenset(
    {
        "apps",
        "browser_back",
        "browser_forward",
        "media_next",
        "media_previous",
        "media_stop",
        "media_play_pause",
        "volume_mute",
        "volume_down",
        "volume_up",
        "vk_5f",  # Sleep
        "vk_a8",  # Browser refresh
        "vk_a9",  # Browser stop
        "vk_aa",  # Browser search
        "vk_ab",  # Browser favorites
        "vk_ac",  # Browser home
        "vk_b4",  # Launch mail
        "vk_b5",  # Select media
        "vk_b6",  # Launch application 1
        "vk_b7",  # Launch application 2
    }
)
_WETYPE_INPUT_METHOD_SWITCH_CHORDS = frozenset(
    {
        frozenset({"ctrl", "space"}),
        frozenset({"ctrl", "shift"}),
        frozenset({"alt", "shift"}),
        frozenset({"win", "space"}),
    }
)
_DOUBAO_BASIC_MODIFIERS = (
    (0x0002, "ctrl"),
    (0x0004, "shift"),
    (0x0001, "alt"),
    (0x0008, "win"),
)
_DOUBAO_SIDED_MODIFIERS = (
    (0x0100, 0x0002, "lctrl"),
    (0x0200, 0x0002, "rctrl"),
    (0x1000, 0x0004, "lshift"),
    (0x2000, 0x0004, "rshift"),
    (0x0400, 0x0001, "lalt"),
    (0x0800, 0x0001, "ralt"),
    (0x4000, 0x0008, "lwin"),
    (0x8000, 0x0008, "rwin"),
)
_DOUBAO_BASIC_MODIFIER_MASK = 0x000F
_DOUBAO_SIDED_MODIFIER_MASK = 0xFF00


@dataclass(frozen=True, repr=False)
class SogouWriteReceipt:
    original_content: bytes
    written_content: bytes


@dataclass(frozen=True)
class VoiceHotkeySyncResult:
    provider_id: str
    ok: bool
    code: str
    hotkey: str = ""
    message: str = ""
    source_sha256: str = ""
    written_sha256: str = ""
    write_receipt: Optional[SogouWriteReceipt] = field(
        default=None,
        repr=False,
        compare=False,
    )


class _ProviderWriteCancelled(RuntimeError):
    """Raised only before the provider file replacement has begun."""


class _ProviderWriteConflict(RuntimeError):
    """Raised before replacement when the provider changed during staging."""


def _parsed_hotkey(
    provider_id: str, shortcut: str
) -> tuple[Optional[hotkey.HotkeySpec], VoiceHotkeySyncResult]:
    try:
        spec = hotkey.HotkeySpec.parse(shortcut)
        tokens = tuple(spec.modifiers) + (spec.key,)
        win32_keys.resolve_vk_codes(tokens)
    except (hotkey.HotkeyParseError, win32_keys.UnknownKeyTokenError) as exc:
        return None, VoiceHotkeySyncResult(
            provider_id, False, "invalid_hotkey", message=f"快捷键无效：{exc}"
        )
    return spec, VoiceHotkeySyncResult(
        provider_id, True, "valid", spec.serialize()
    )


def _modifier_family(token: str) -> str:
    return _MODIFIER_FAMILY_BY_TOKEN.get(token, "")


def _is_function_key(token: str) -> bool:
    if not token.startswith("f") or not token[1:].isdigit():
        return False
    return 1 <= int(token[1:]) <= 24


def validate_provider_hotkey(
    provider_id: object, shortcut: str
) -> VoiceHotkeySyncResult:
    """Validate one shortcut against the selected program's input rules."""

    provider = str(provider_id).strip().lower()
    spec, parsed = _parsed_hotkey(provider, shortcut)
    if spec is None:
        return parsed
    normalized = parsed.hotkey
    tokens = (*spec.modifiers, spec.key)

    if provider == voice_program_manager.VOICE_PROGRAM_SOGOU:
        if any(token not in _REMOTE_TO_SOGOU_TOKEN for token in tokens):
            return VoiceHotkeySyncResult(
                provider,
                False,
                "unsupported_key",
                message="搜狗语音不支持该按键，请换一个常用组合键。",
            )
        if len(tokens) > 3:
            return VoiceHotkeySyncResult(
                provider,
                False,
                "too_many_keys",
                message="搜狗语音最多允许 3 个按键。",
            )
        if len(tokens) == 1:
            if not (_modifier_family(tokens[0]) or _is_function_key(tokens[0])):
                return VoiceHotkeySyncResult(
                    provider,
                    False,
                    "unsupported_single_key",
                    message=(
                        "搜狗语音的单键只能使用 Ctrl、Shift、Alt、Win 或 F1-F24。"
                    ),
                )
        elif not any(
            _modifier_family(token) or _is_function_key(token) for token in tokens
        ):
            return VoiceHotkeySyncResult(
                provider,
                False,
                "missing_modifier",
                message=(
                    "搜狗语音的组合键必须包含 Ctrl、Shift、Alt、Win 或 F1-F24。"
                ),
            )

    if provider == voice_program_manager.VOICE_PROGRAM_WETYPE:
        if len(tokens) > 3:
            return VoiceHotkeySyncResult(
                provider,
                False,
                "too_many_keys",
                message="微信输入法最多允许 3 个按键。",
            )
        modifier_families = tuple(
            family for token in tokens if (family := _modifier_family(token))
        )
        if not modifier_families:
            return VoiceHotkeySyncResult(
                provider,
                False,
                "missing_modifier",
                message="微信输入法的按住型快捷键必须包含修饰键。",
            )
        if len(tokens) == 1 and modifier_families[0] == "win":
            return VoiceHotkeySyncResult(
                provider,
                False,
                "unsupported_single_key",
                message="微信输入法的单键只能使用 Ctrl、Shift 或 Alt。",
            )
        if len(set(modifier_families)) != len(modifier_families):
            return VoiceHotkeySyncResult(
                provider,
                False,
                "duplicate_modifier",
                message="微信输入法不接受左右同类修饰键同时使用。",
            )
        if any(token in _WETYPE_BLOCKED_KEYS for token in tokens):
            return VoiceHotkeySyncResult(
                provider,
                False,
                "unsupported_key",
                message="微信输入法不支持该功能键，请换一个常用组合键。",
            )
        comparable_tokens = frozenset(
            _modifier_family(token) or token for token in tokens
        )
        if comparable_tokens in _WETYPE_INPUT_METHOD_SWITCH_CHORDS:
            return VoiceHotkeySyncResult(
                provider,
                False,
                "reserved_hotkey",
                message="微信输入法不接受该输入法切换组合，请换一个组合键。",
            )

    return VoiceHotkeySyncResult(provider, True, "valid", normalized)


def default_hotkey(provider_id: object) -> str:
    return DEFAULT_PROVIDER_HOTKEYS.get(
        str(provider_id).strip().lower(),
        DEFAULT_PROVIDER_HOTKEYS[voice_program_manager.VOICE_PROGRAM_NONE],
    )


def default_hotkeys_by_provider() -> dict[str, dict[str, str]]:
    return {
        provider_id: {"hold": shortcut, "source": "default"}
        for provider_id, shortcut in DEFAULT_PROVIDER_HOTKEYS.items()
    }


def read_provider_hotkey(
    provider_id: object,
    *,
    platform: Optional[str] = None,
    appdata: Optional[Path] = None,
    allow_settings_window: bool = False,
    cancel_event=None,
    trigger="hold",
) -> VoiceHotkeySyncResult:
    provider = str(provider_id).strip().lower()
    current_platform = platform or sys.platform
    if provider in {
        voice_program_manager.VOICE_PROGRAM_NONE,
        voice_program_manager.VOICE_PROGRAM_CUSTOM,
    }:
        return VoiceHotkeySyncResult(
            provider,
            False,
            "local_only",
            message=f"该程序只使用{product_identity.DISPLAY_NAME}内记录的按住型快捷键。",
        )
    if current_platform != "win32":
        return VoiceHotkeySyncResult(
            provider, False, "unsupported_platform", message="仅 Windows 支持自动读取。"
        )
    if provider == voice_program_manager.VOICE_PROGRAM_SOGOU:
        return _read_sogou_hotkey(appdata=appdata, trigger=trigger)
    if provider == voice_program_manager.VOICE_PROGRAM_WETYPE:
        if not allow_settings_window:
            return VoiceHotkeySyncResult(
                provider, False, "local_only",
                message="已保留微信快捷键；点击刷新可打开微信设置读取，或手动录入。",
            )
        return (_read_wetype_hotkey(cancel_event=cancel_event, trigger="toggle") if trigger == "toggle"
                else _read_wetype_hotkey(cancel_event=cancel_event))
    if provider == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME:
        return _read_doubao_hotkey(appdata=appdata, trigger=trigger)
    return VoiceHotkeySyncResult(
        provider, False, "unsupported_provider", message="暂不支持读取该程序。"
    )


def sync_provider_hotkey(
    provider_id: object,
    shortcut: str,
    *,
    platform: Optional[str] = None,
    appdata: Optional[Path] = None,
    trigger="hold",
    cancel_event: Optional[threading.Event] = None,
    expected_current: Optional[str] = None,
    expected_current_sha256: Optional[str] = None,
) -> VoiceHotkeySyncResult:
    provider = str(provider_id).strip().lower()
    validation = validate_provider_hotkey(provider, shortcut)
    if not validation.ok:
        return validation
    normalized = validation.hotkey

    current_platform = platform or sys.platform
    if provider in {
        voice_program_manager.VOICE_PROGRAM_NONE,
        voice_program_manager.VOICE_PROGRAM_WETYPE,
        voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
        voice_program_manager.VOICE_PROGRAM_CUSTOM,
    }:
        if provider == voice_program_manager.VOICE_PROGRAM_WETYPE:
            message = ("快捷键已保存到无线麦；请确保与微信输入法中的" +
                       ("启动语音输入" if trigger == "toggle" else "按住型") + "快捷键一致。")
        elif provider == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME:
            message = "快捷键已保存到无线麦；请确保与豆包输入法中的" + (
                "免按模式" if trigger == "toggle" else "按住型"
            ) + "快捷键一致。"
        else:
            message = f"快捷键已保存到{product_identity.DISPLAY_NAME}。"
        return VoiceHotkeySyncResult(
            provider,
            True,
            "local_only",
            normalized,
            message,
        )
    if current_platform != "win32":
        return VoiceHotkeySyncResult(
            provider, False, "unsupported_platform", message="仅 Windows 支持自动同步。"
        )
    if provider == voice_program_manager.VOICE_PROGRAM_SOGOU:
        if trigger == "toggle":
            return VoiceHotkeySyncResult(provider, True, "local_only", normalized,
                "开关型快捷键已保存到无线麦；请确保与搜狗的点按开启快捷键一致。")
        return _sync_sogou_hotkey(
            normalized,
            appdata=appdata,
            cancel_event=cancel_event,
            expected_current=expected_current,
            expected_current_sha256=expected_current_sha256,
        )
    return VoiceHotkeySyncResult(
        provider, False, "unsupported_provider", message="暂不支持同步该程序。"
    )


def restore_provider_write(
    provider_id: object,
    receipt: object,
    *,
    platform: Optional[str] = None,
    appdata: Optional[Path] = None,
    cancel_event: Optional[threading.Event] = None,
) -> VoiceHotkeySyncResult:
    provider = str(provider_id).strip().lower()
    if (platform or sys.platform) != "win32":
        return VoiceHotkeySyncResult(
            provider,
            False,
            "unsupported_platform",
            message="仅 Windows 支持自动恢复。",
        )
    if (
        provider != voice_program_manager.VOICE_PROGRAM_SOGOU
        or not isinstance(receipt, SogouWriteReceipt)
    ):
        return VoiceHotkeySyncResult(
            provider,
            False,
            "rollback_receipt_invalid",
            message="本次写入凭据无效，未覆盖第三方配置。",
        )
    return _restore_sogou_write(
        receipt,
        appdata=appdata,
        cancel_event=cancel_event,
    )


def _sogou_config_path(appdata: Optional[Path] = None) -> Path:
    root = appdata
    if root is None:
        value = os.environ.get("APPDATA", "")
        root = Path(value) if value else Path.home() / "AppData" / "Roaming"
    return root / _SOGOU_CONFIG_RELATIVE_PATH


def _doubao_config_path(appdata: Optional[Path] = None) -> Path:
    root = appdata
    if root is None:
        value = os.environ.get("APPDATA", "")
        root = Path(value) if value else Path.home() / "AppData" / "Roaming"
    return root / _DOUBAO_CONFIG_RELATIVE_PATH


def _preferred_token_for_vk(vk_code: int) -> str:
    preferred = [
        *(chr(code).lower() for code in range(ord("A"), ord("Z") + 1)),
        *(str(code) for code in range(10)),
        *(f"f{code}" for code in range(1, 25)),
        "space",
        "enter",
        "tab",
        "escape",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "page_up",
        "page_down",
        "up",
        "down",
        "left",
        "right",
    ]
    for token in preferred:
        if win32_keys.VK_CODES.get(token) == vk_code:
            return token
    for token, candidate in win32_keys.VK_CODES.items():
        if candidate == vk_code and token not in _MODIFIER_FAMILY_BY_TOKEN:
            return token
    raise ValueError(f"不支持的按键代码：{vk_code}")


def _doubao_shortcut_to_hotkey(raw: object, *, label="快捷键") -> str:
    if not isinstance(raw, dict):
        raise ValueError(f"豆包{label}格式无效")
    try:
        modifier_flags = int(raw.get("modifierFlags", 0))
        key_code = int(raw.get("keyCode", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"豆包{label}包含无效数值") from exc

    if modifier_flags < 0:
        raise ValueError(f"豆包{label}包含无效修饰键标记")
    unknown_flags = modifier_flags & ~(
        _DOUBAO_BASIC_MODIFIER_MASK | _DOUBAO_SIDED_MODIFIER_MASK
    )
    if unknown_flags:
        raise ValueError(f"暂不识别豆包修饰键标记：{modifier_flags}")

    sided_flags = modifier_flags & _DOUBAO_SIDED_MODIFIER_MASK
    if sided_flags:
        modifiers = tuple(
            token
            for side_flag, _family_flag, token in _DOUBAO_SIDED_MODIFIERS
            if sided_flags & side_flag
        )
        expected_family_flags = 0
        for side_flag, family_flag, _token in _DOUBAO_SIDED_MODIFIERS:
            if sided_flags & side_flag:
                expected_family_flags |= family_flag
        if modifier_flags & _DOUBAO_BASIC_MODIFIER_MASK != expected_family_flags:
            raise ValueError(f"豆包{label}的修饰键标记不一致")
    else:
        modifiers = tuple(
            token for flag, token in _DOUBAO_BASIC_MODIFIERS if modifier_flags & flag
        )
    tokens = list(modifiers)
    if key_code:
        tokens.append(_preferred_token_for_vk(key_code))
    if not tokens:
        raise ValueError(f"豆包{label}为空")
    spec = hotkey.HotkeySpec.parse("+".join(tokens))
    win32_keys.resolve_vk_codes((*spec.modifiers, spec.key))
    return spec.serialize()


def _read_doubao_hotkey(*, appdata: Optional[Path], trigger="hold") -> VoiceHotkeySyncResult:
    provider = voice_program_manager.VOICE_PROGRAM_DOUBAO_IME
    path = _doubao_config_path(appdata)
    if not path.is_file():
        return VoiceHotkeySyncResult(
            provider, False, "not_found", message="未找到豆包输入法快捷键配置。"
        )
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
        voice = document.get("voice") if isinstance(document, dict) else None
        if not isinstance(voice, dict):
            raise ValueError("豆包配置缺少 voice")
        toggle = trigger == "toggle"
        label = "免按模式快捷键" if toggle else "按住型快捷键"
        shortcut = _doubao_shortcut_to_hotkey(
            voice.get("voiceShortcut" if toggle else "voiceLongPressShortcut"),
            label=label,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return VoiceHotkeySyncResult(
            provider, False, "read_failed",
            message=f"读取豆包{'免按模式' if trigger == 'toggle' else '按住型'}快捷键失败：{exc}",
        )
    return VoiceHotkeySyncResult(
        provider, True, "read", shortcut,
        f"已读取豆包输入法的{'免按模式' if trigger == 'toggle' else '按住型'}快捷键。",
    )


def _wetype_key_text_to_token(value: str) -> str:
    text = str(value).strip()
    modifier = _WETYPE_MODIFIER_TEXT.fullmatch(text)
    if modifier:
        side, family = modifier.groups()
        prefix = "l" if side == "左" else "r" if side == "右" else ""
        return prefix + family.casefold()
    if text in _WETYPE_KEY_NAMES:
        return _WETYPE_KEY_NAMES[text]
    normalized = text.casefold()
    if re.fullmatch(r"[a-z0-9]", normalized) or re.fullmatch(
        r"f(?:[1-9]|1\d|2[0-4])", normalized
    ):
        return normalized
    raise ValueError(f"微信按住型快捷键包含不支持的按键：{text}")


def _parse_wetype_settings_shortcut(value: str) -> str:
    text = re.sub(r"(左|右)\s+(?=Ctrl|Shift|Alt|Win)", r"\1", value, flags=re.I)
    tokens = [_wetype_key_text_to_token(part) for part in re.split(r"\s+|\+", text.strip())]
    result = validate_provider_hotkey("wetype", "+".join(tokens))
    if not result.ok:
        raise ValueError(result.message)
    return result.hotkey


def _read_wetype_hotkey(*, cancel_event=None, trigger="hold") -> VoiceHotkeySyncResult:
    provider = voice_program_manager.VOICE_PROGRAM_WETYPE
    from . import wetype_settings_hotkey_windows as settings_reader

    try:
        shortcut = _parse_wetype_settings_shortcut(
            (settings_reader.read_toggle_shortcut if trigger == "toggle" else settings_reader.read_hold_shortcut)(
                cancel_event=cancel_event)
        )
    except settings_reader.SettingsReadError as exc:
        return VoiceHotkeySyncResult(provider, False, exc.code, message=str(exc))
    except Exception as exc:  # noqa: BLE001 - optional provider integration
        return VoiceHotkeySyncResult(
            provider, False, "read_failed",
            message=f"读取微信{'开关型' if trigger == 'toggle' else '按住型'}快捷键失败：{exc}；可以手动录入。",
        )
    return VoiceHotkeySyncResult(
        provider, True, "read", shortcut, "已从微信设置读取" + ("启动语音输入" if trigger == "toggle" else "按住说话") + "快捷键。",
    )


def _provider_tokens_to_hotkey(raw_tokens: object) -> str:
    if not isinstance(raw_tokens, list) or not raw_tokens:
        raise ValueError("快捷键为空")
    tokens = []
    for raw_token in raw_tokens:
        token = str(raw_token).strip()
        if token.casefold() == "numpadenter":
            raise ValueError(
                "无线麦目前不能区分数字键盘 Enter，请在搜狗语音界面换一个快捷键"
            )
        normalized = _SOGOU_TO_REMOTE_TOKEN.get(token.casefold())
        if normalized is None:
            raise ValueError(f"搜狗快捷键包含不支持的按键：{token}")
        tokens.append(normalized)
    spec = hotkey.HotkeySpec.parse("+".join(tokens))
    win32_keys.resolve_vk_codes((*spec.modifiers, spec.key))
    return spec.serialize()


def _hotkey_to_provider_tokens(shortcut: str) -> list[str]:
    spec = hotkey.HotkeySpec.parse(shortcut)
    tokens = (*spec.modifiers, spec.key)
    try:
        return [_REMOTE_TO_SOGOU_TOKEN[token] for token in tokens]
    except KeyError as exc:
        raise ValueError(f"搜狗语音不支持按键：{exc.args[0]}") from exc


def _load_sogou_document_bytes(content: bytes) -> dict:
    document = json.loads(content.decode("utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError("搜狗配置格式无效")
    setting = document.get("setting")
    if not isinstance(setting, dict):
        raise ValueError("搜狗配置缺少 setting")
    return document


def _load_sogou_document(path: Path) -> dict:
    return _load_sogou_document_bytes(path.read_bytes())


def _read_sogou_hotkey(*, appdata: Optional[Path], trigger="hold") -> VoiceHotkeySyncResult:
    provider = voice_program_manager.VOICE_PROGRAM_SOGOU
    path = _sogou_config_path(appdata)
    if not path.is_file():
        return VoiceHotkeySyncResult(
            provider, False, "not_found", message="未找到搜狗语音快捷键配置。"
        )
    try:
        document = _load_sogou_document(path)
        if trigger == "toggle" and document["setting"].get("freespeakEnabled") is False:
            raise ValueError("请先在搜狗设置中启用点按开启语音输入")
        shortcut = _provider_tokens_to_hotkey(
            document["setting"].get("shortcutKeysFree" if trigger == "toggle" else "shortcutKeysPress")
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return VoiceHotkeySyncResult(
            provider, False, "read_failed", message=f"读取搜狗快捷键失败：{exc}"
        )
    return VoiceHotkeySyncResult(
        provider,
        True,
        "read",
        shortcut,
        ("已读取搜狗的点按开启快捷键。" if trigger == "toggle" else
         "已读取搜狗当前的按住说快捷键。如需修改，请在「搜狗语音界面」修改按住型快捷键，改后自动同步。"),
    )


def _sogou_voice_process_running() -> Optional[bool]:
    try:
        status = voice_program_manager.inspect_voice_program(
            {"provider": voice_program_manager.VOICE_PROGRAM_SOGOU}
        )
        return bool(status.running)
    except Exception:
        return None


def _replace_bytes_atomically(
    path: Path,
    content: bytes,
    *,
    cancel_event: Optional[threading.Event] = None,
    expected_current: Optional[bytes] = None,
) -> None:
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_current is not None:
            try:
                current = path.read_bytes()
            except OSError as exc:
                raise _ProviderWriteConflict(
                    "provider content could not be rechecked before replacement"
                ) from exc
            if current != expected_current:
                raise _ProviderWriteConflict(
                    "provider content changed while replacement was staged"
                )
        if cancel_event is not None and cancel_event.is_set():
            raise _ProviderWriteCancelled(
                "provider write was cancelled before replacement"
            )
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _sync_sogou_hotkey(
    shortcut: str,
    *,
    appdata: Optional[Path],
    cancel_event: Optional[threading.Event] = None,
    expected_current: Optional[str] = None,
    expected_current_sha256: Optional[str] = None,
) -> VoiceHotkeySyncResult:
    provider = voice_program_manager.VOICE_PROGRAM_SOGOU
    path = _sogou_config_path(appdata)
    process_running = _sogou_voice_process_running()
    if process_running is None:
        return VoiceHotkeySyncResult(
            provider,
            False,
            "process_check_failed",
            message="无法确认搜狗语音助手是否正在运行；为避免覆盖运行中的配置，本次未写入。",
        )
    if process_running:
        return VoiceHotkeySyncResult(
            provider,
            False,
            "restart_required",
            message="搜狗语音助手正在运行；请先退出它，再保存快捷键。",
        )
    if not path.is_file():
        return VoiceHotkeySyncResult(
            provider, False, "not_found", message="未找到搜狗语音快捷键配置。"
        )

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if cancelled():
        return VoiceHotkeySyncResult(
            provider,
            False,
            "cancelled",
            message="已取消同步搜狗快捷键，本次未写入。",
        )

    try:
        original = path.read_bytes()
    except OSError as exc:
        return VoiceHotkeySyncResult(
            provider, False, "read_failed", message=f"读取搜狗快捷键失败：{exc}"
        )

    replaced = False
    previous_shortcut = ""
    original_sha256 = hashlib.sha256(original).hexdigest()
    try:
        if (
            expected_current_sha256 is not None
            and original_sha256 != expected_current_sha256
        ):
            current = _read_sogou_hotkey(appdata=appdata)
            return VoiceHotkeySyncResult(
                provider,
                False,
                "conflict",
                current.hotkey if current.ok else "",
                "搜狗配置已被其它操作修改，本次未覆盖其新内容。",
                source_sha256=original_sha256,
            )
        document = _load_sogou_document_bytes(original)
        previous_shortcut = _provider_tokens_to_hotkey(
            document["setting"].get("shortcutKeysPress")
        )
        if (
            expected_current is not None
            and previous_shortcut != expected_current
        ):
            return VoiceHotkeySyncResult(
                provider,
                False,
                "conflict",
                previous_shortcut,
                "搜狗快捷键已被其它操作修改，本次未覆盖其新值。",
            )
        document["setting"]["shortcutKeysPress"] = _hotkey_to_provider_tokens(
            shortcut
        )
        document["setting"]["longPressEnabled"] = True
        payload = (
            json.dumps(document, ensure_ascii=False, indent="\t") + "\n"
        ).encode("utf-8")
        if cancelled():
            return VoiceHotkeySyncResult(
                provider,
                False,
                "cancelled",
                previous_shortcut,
                "已取消同步搜狗快捷键，本次未写入。",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        _replace_bytes_atomically(
            path,
            payload,
            cancel_event=cancel_event,
            expected_current=original,
        )
        replaced = True
        verification = _read_sogou_hotkey(appdata=appdata)
        if not verification.ok or verification.hotkey != shortcut:
            raise RuntimeError("写入后读回不一致")
    except _ProviderWriteCancelled:
        return VoiceHotkeySyncResult(
            provider,
            False,
            "cancelled",
            previous_shortcut,
            "已取消同步搜狗快捷键，本次未写入。",
            source_sha256=original_sha256,
        )
    except _ProviderWriteConflict:
        current = _read_sogou_hotkey(appdata=appdata)
        return VoiceHotkeySyncResult(
            provider,
            False,
            "conflict",
            current.hotkey if current.ok else "",
            "搜狗配置已被其它操作修改，本次未覆盖其新内容。",
            source_sha256=original_sha256,
        )
    except Exception as exc:
        if replaced:
            try:
                current_bytes = path.read_bytes()
                if current_bytes != payload:
                    raise OSError("配置已被其它程序再次修改，未覆盖其新内容")
                _replace_bytes_atomically(
                    path,
                    original,
                    expected_current=payload,
                )
                if path.read_bytes() != original:
                    raise OSError("恢复后内容不一致")
            except Exception as rollback_exc:
                current = _read_sogou_hotkey(appdata=appdata)
                return VoiceHotkeySyncResult(
                    provider,
                    False,
                    "rollback_failed",
                    current.hotkey if current.ok else "",
                    message=(
                        f"同步搜狗快捷键失败：{exc}；原快捷键也未能恢复：{rollback_exc}。"
                    ),
                )
        return VoiceHotkeySyncResult(
            provider,
            False,
            "write_failed",
            previous_shortcut,
            message=f"同步搜狗快捷键失败：{exc}",
        )
    return VoiceHotkeySyncResult(
        provider,
        True,
        "synced",
        shortcut,
        "已同步到搜狗的按住说快捷键。",
        source_sha256=original_sha256,
        written_sha256=hashlib.sha256(payload).hexdigest(),
        write_receipt=SogouWriteReceipt(original, payload),
    )


def _restore_sogou_write(
    receipt: SogouWriteReceipt,
    *,
    appdata: Optional[Path],
    cancel_event: Optional[threading.Event],
) -> VoiceHotkeySyncResult:
    provider = voice_program_manager.VOICE_PROGRAM_SOGOU
    path = _sogou_config_path(appdata)
    process_running = _sogou_voice_process_running()
    if process_running is None:
        return VoiceHotkeySyncResult(
            provider,
            False,
            "process_check_failed",
            message="无法确认搜狗语音助手是否正在运行；本次未恢复配置。",
        )
    if process_running:
        return VoiceHotkeySyncResult(
            provider,
            False,
            "restart_required",
            message="搜狗语音助手正在运行；本次未恢复配置。",
        )
    try:
        original_document = _load_sogou_document_bytes(receipt.original_content)
        original_hotkey = _provider_tokens_to_hotkey(
            original_document["setting"].get("shortcutKeysPress")
        )
        _replace_bytes_atomically(
            path,
            receipt.original_content,
            cancel_event=cancel_event,
            expected_current=receipt.written_content,
        )
        if path.read_bytes() != receipt.original_content:
            raise OSError("恢复后内容不一致")
    except _ProviderWriteCancelled:
        return VoiceHotkeySyncResult(
            provider,
            False,
            "cancelled",
            message="已取消恢复搜狗快捷键，本次未写入。",
        )
    except _ProviderWriteConflict:
        current = _read_sogou_hotkey(appdata=appdata)
        return VoiceHotkeySyncResult(
            provider,
            False,
            "conflict",
            current.hotkey if current.ok else "",
            "搜狗配置已被其它操作修改，本次未覆盖其新内容。",
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return VoiceHotkeySyncResult(
            provider,
            False,
            "rollback_failed",
            message=f"恢复搜狗快捷键失败：{exc}",
        )
    return VoiceHotkeySyncResult(
        provider,
        True,
        "restored",
        original_hotkey,
        "已恢复搜狗原有快捷键配置。",
        source_sha256=hashlib.sha256(receipt.written_content).hexdigest(),
        written_sha256=hashlib.sha256(receipt.original_content).hexdigest(),
    )
