"""Read and synchronize supported provider-owned voice shortcuts on Windows.

Sogou exposes a stable on-disk shortcut setting that can be read and updated
without opening its UI. Windows dictation has one fixed shortcut. WeType is
controlled through its own voice button; its stored shortcut is compatibility
data only and is neither edited nor executed by Remote Mic.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Optional

from . import hotkey, product_identity, voice_program_manager, win32_keys


DEFAULT_PROVIDER_HOTKEYS = {
    voice_program_manager.VOICE_PROGRAM_NONE: "ralt",
    voice_program_manager.VOICE_PROGRAM_SOGOU: "rctrl",
    # Kept only so older provider-scoped configuration remains readable.
    voice_program_manager.VOICE_PROGRAM_WETYPE: "lctrl+lwin",
    voice_program_manager.VOICE_PROGRAM_WINDOWS_DICTATION: "win+h",
    voice_program_manager.VOICE_PROGRAM_CUSTOM: "ralt",
}

_SOGOU_CONFIG_RELATIVE_PATH = Path("sogou_voice_assistant_pc") / "config.json"
_SOGOU_PROCESS_NAME = "sogou_voice_assistant.exe"
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


@dataclass(frozen=True)
class VoiceHotkeySyncResult:
    provider_id: str
    ok: bool
    code: str
    hotkey: str = ""
    message: str = ""


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
    if provider == voice_program_manager.VOICE_PROGRAM_WETYPE:
        return VoiceHotkeySyncResult(
            provider,
            False,
            "not_required",
            message="无线麦直接控制微信语音，无需设置快捷键。",
        )
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

    return VoiceHotkeySyncResult(provider, True, "valid", normalized)


def default_hotkey(provider_id: object) -> str:
    return DEFAULT_PROVIDER_HOTKEYS.get(
        str(provider_id).strip().lower(),
        DEFAULT_PROVIDER_HOTKEYS[voice_program_manager.VOICE_PROGRAM_NONE],
    )


def default_hotkeys_by_provider() -> dict[str, dict[str, str]]:
    return {
        provider_id: {"hold": shortcut}
        for provider_id, shortcut in DEFAULT_PROVIDER_HOTKEYS.items()
    }


def read_provider_hotkey(
    provider_id: object,
    *,
    platform: Optional[str] = None,
    appdata: Optional[Path] = None,
) -> VoiceHotkeySyncResult:
    provider = str(provider_id).strip().lower()
    current_platform = platform or sys.platform
    if provider == voice_program_manager.VOICE_PROGRAM_WINDOWS_DICTATION:
        shortcut = default_hotkey(provider)
        return VoiceHotkeySyncResult(
            provider, True, "fixed", shortcut, "Windows 语音输入固定使用 Win+H。"
        )
    if provider in {
        voice_program_manager.VOICE_PROGRAM_NONE,
        voice_program_manager.VOICE_PROGRAM_WETYPE,
        voice_program_manager.VOICE_PROGRAM_CUSTOM,
    }:
        message = (
            "无线麦直接控制微信语音，无需设置快捷键。"
            if provider == voice_program_manager.VOICE_PROGRAM_WETYPE
            else f"该程序只使用{product_identity.DISPLAY_NAME}内记录的按住型快捷键。"
        )
        return VoiceHotkeySyncResult(
            provider, False, "local_only", message=message
        )
    if current_platform != "win32":
        return VoiceHotkeySyncResult(
            provider, False, "unsupported_platform", message="仅 Windows 支持自动读取。"
        )
    if provider == voice_program_manager.VOICE_PROGRAM_SOGOU:
        return _read_sogou_hotkey(appdata=appdata)
    return VoiceHotkeySyncResult(
        provider, False, "unsupported_provider", message="暂不支持读取该程序。"
    )


def sync_provider_hotkey(
    provider_id: object,
    shortcut: str,
    *,
    platform: Optional[str] = None,
    appdata: Optional[Path] = None,
) -> VoiceHotkeySyncResult:
    provider = str(provider_id).strip().lower()
    if provider == voice_program_manager.VOICE_PROGRAM_WETYPE:
        return VoiceHotkeySyncResult(
            provider,
            False,
            "not_required",
            message="无线麦直接控制微信语音，无需设置快捷键。",
        )
    validation = validate_provider_hotkey(provider, shortcut)
    if not validation.ok:
        return validation
    normalized = validation.hotkey

    current_platform = platform or sys.platform
    if provider in {
        voice_program_manager.VOICE_PROGRAM_NONE,
        voice_program_manager.VOICE_PROGRAM_CUSTOM,
    }:
        return VoiceHotkeySyncResult(
            provider,
            True,
            "local_only",
            normalized,
            f"快捷键已保存到{product_identity.DISPLAY_NAME}。",
        )
    if provider == voice_program_manager.VOICE_PROGRAM_WINDOWS_DICTATION:
        fixed = default_hotkey(provider)
        if normalized != fixed:
            return VoiceHotkeySyncResult(
                provider,
                False,
                "fixed_hotkey",
                fixed,
                "Windows 语音输入只能使用 Win+H。",
            )
        return VoiceHotkeySyncResult(
            provider, True, "fixed", fixed, "Windows 语音输入固定使用 Win+H。"
        )
    if current_platform != "win32":
        return VoiceHotkeySyncResult(
            provider, False, "unsupported_platform", message="仅 Windows 支持自动同步。"
        )
    if provider == voice_program_manager.VOICE_PROGRAM_SOGOU:
        return _sync_sogou_hotkey(normalized, appdata=appdata)
    return VoiceHotkeySyncResult(
        provider, False, "unsupported_provider", message="暂不支持同步该程序。"
    )


def _sogou_config_path(appdata: Optional[Path] = None) -> Path:
    root = appdata
    if root is None:
        value = os.environ.get("APPDATA", "")
        root = Path(value) if value else Path.home() / "AppData" / "Roaming"
    return root / _SOGOU_CONFIG_RELATIVE_PATH


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


def _load_sogou_document(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("搜狗配置格式无效")
    setting = document.get("setting")
    if not isinstance(setting, dict):
        raise ValueError("搜狗配置缺少 setting")
    return document


def _read_sogou_hotkey(*, appdata: Optional[Path]) -> VoiceHotkeySyncResult:
    provider = voice_program_manager.VOICE_PROGRAM_SOGOU
    path = _sogou_config_path(appdata)
    if not path.is_file():
        return VoiceHotkeySyncResult(
            provider, False, "not_found", message="未找到搜狗语音快捷键配置。"
        )
    try:
        document = _load_sogou_document(path)
        shortcut = _provider_tokens_to_hotkey(
            document["setting"].get("shortcutKeysPress")
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
        "已读取搜狗当前的按住说快捷键。如需修改，请在"
        "「搜狗语音界面」修改按住型快捷键，改后自动同步。",
    )


def _sogou_voice_process_running() -> Optional[bool]:
    try:
        status = voice_program_manager.inspect_voice_program(
            {"provider": voice_program_manager.VOICE_PROGRAM_SOGOU}
        )
        return bool(status.running)
    except Exception:
        return None


def _replace_bytes_atomically(path: Path, content: bytes) -> None:
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _sync_sogou_hotkey(
    shortcut: str, *, appdata: Optional[Path]
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

    try:
        original = path.read_bytes()
    except OSError as exc:
        return VoiceHotkeySyncResult(
            provider, False, "read_failed", message=f"读取搜狗快捷键失败：{exc}"
        )

    replaced = False
    previous_shortcut = ""
    try:
        document = _load_sogou_document(path)
        previous_shortcut = _provider_tokens_to_hotkey(
            document["setting"].get("shortcutKeysPress")
        )
        document["setting"]["shortcutKeysPress"] = _hotkey_to_provider_tokens(
            shortcut
        )
        document["setting"]["longPressEnabled"] = True
        payload = (
            json.dumps(document, ensure_ascii=False, indent="\t") + "\n"
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        _replace_bytes_atomically(path, payload)
        replaced = True
        verification = _read_sogou_hotkey(appdata=appdata)
        if not verification.ok or verification.hotkey != shortcut:
            raise RuntimeError("写入后读回不一致")
    except Exception as exc:
        if replaced:
            try:
                _replace_bytes_atomically(path, original)
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
        provider, True, "synced", shortcut, "已同步到搜狗的按住说快捷键。"
    )
