"""Read and synchronize supported provider-owned voice shortcuts on Windows.

Sogou exposes a stable on-disk shortcut setting that can be read and updated
without opening its UI. Windows dictation has one fixed shortcut. WeType does
not expose a stable silent settings surface, so Remote Mic only remembers its
shortcut locally and lets the user open WeType's own settings when needed.
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
    # WeType 2.1.2's native migration initializes hold-to-talk as Ctrl+Win.
    voice_program_manager.VOICE_PROGRAM_WETYPE: "lctrl+lwin",
    voice_program_manager.VOICE_PROGRAM_WINDOWS_DICTATION: "win+h",
    voice_program_manager.VOICE_PROGRAM_CUSTOM: "ralt",
}

_SOGOU_CONFIG_RELATIVE_PATH = Path("sogou_voice_assistant_pc") / "config.json"
_SOGOU_PROCESS_NAME = "sogou_voice_assistant.exe"
_REMOTE_TO_PROVIDER_TOKEN = {
    "ctrl": "LeftCtrl",
    "lctrl": "LeftCtrl",
    "rctrl": "RightCtrl",
    "shift": "LeftShift",
    "lshift": "LeftShift",
    "rshift": "RightShift",
    "alt": "LeftAlt",
    "lalt": "LeftAlt",
    "ralt": "RightAlt",
    "win": "LeftWin",
    "lwin": "LeftWin",
    "rwin": "RightWin",
}
_PROVIDER_TO_REMOTE_TOKEN = {
    value.casefold(): key for key, value in _REMOTE_TO_PROVIDER_TOKEN.items()
}
_PROVIDER_TO_REMOTE_TOKEN.update(
    {
        "leftctrl": "lctrl",
        "rightctrl": "rctrl",
        "leftshift": "lshift",
        "rightshift": "rshift",
        "leftalt": "lalt",
        "rightalt": "ralt",
        "leftwin": "lwin",
        "rightwin": "rwin",
    }
)


@dataclass(frozen=True)
class VoiceHotkeySyncResult:
    provider_id: str
    ok: bool
    code: str
    hotkey: str = ""
    message: str = ""


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
            f"微信输入法快捷键由{product_identity.DISPLAY_NAME}按程序记忆，"
            "不自动打开或修改微信设置。"
            if provider == voice_program_manager.VOICE_PROGRAM_WETYPE
            else f"该程序只使用{product_identity.DISPLAY_NAME}内的快捷键。"
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
    try:
        spec = hotkey.HotkeySpec.parse(shortcut)
        tokens = tuple(spec.modifiers) + (spec.key,)
        win32_keys.resolve_vk_codes(tokens)
        normalized = spec.serialize()
    except (hotkey.HotkeyParseError, win32_keys.UnknownKeyTokenError) as exc:
        return VoiceHotkeySyncResult(
            provider, False, "invalid_hotkey", message=f"快捷键无效：{exc}"
        )

    current_platform = platform or sys.platform
    if provider in {
        voice_program_manager.VOICE_PROGRAM_NONE,
        voice_program_manager.VOICE_PROGRAM_WETYPE,
        voice_program_manager.VOICE_PROGRAM_CUSTOM,
    }:
        message = (
            f"快捷键已保存到{product_identity.DISPLAY_NAME}；"
            "请在微信输入法设置中保持一致。"
            if provider == voice_program_manager.VOICE_PROGRAM_WETYPE
            else f"快捷键已保存到{product_identity.DISPLAY_NAME}。"
        )
        return VoiceHotkeySyncResult(
            provider, True, "local_only", normalized, message
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
        normalized = _PROVIDER_TO_REMOTE_TOKEN.get(token.casefold(), token.lower())
        tokens.append(normalized)
    spec = hotkey.HotkeySpec.parse("+".join(tokens))
    win32_keys.resolve_vk_codes((*spec.modifiers, spec.key))
    return spec.serialize()


def _hotkey_to_provider_tokens(shortcut: str) -> list[str]:
    spec = hotkey.HotkeySpec.parse(shortcut)
    tokens = (*spec.modifiers, spec.key)
    return [
        _REMOTE_TO_PROVIDER_TOKEN.get(
            token,
            token.upper()
            if token.startswith("f") or (len(token) == 1 and token.isalpha())
            else token,
        )
        for token in tokens
    ]


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
        provider, True, "read", shortcut, "已读取搜狗当前的按住说快捷键。"
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
