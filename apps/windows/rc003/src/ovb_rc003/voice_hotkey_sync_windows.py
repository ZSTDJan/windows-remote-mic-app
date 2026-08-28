"""Read and synchronize provider-owned voice shortcuts on Windows.

The provider remains the authority for the shortcut it actually registers.
Remote Mic reads that value when possible, writes through the provider's own
supported surface, and verifies the result before updating its local mirror.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Callable, Iterable, Optional, Sequence

from . import hotkey, voice_program_manager, win32_input, win32_keys


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
_WETYPE_SETTINGS_CLASS = "wetype.flutter.setting"
_WETYPE_SETTINGS_TITLE = "设置"
_WETYPE_SETTINGS_EXE = "wetype_update.exe"
_WETYPE_SHOW_SETTINGS_ARGUMENT = "-showsetting"
_WETYPE_WINDOW_WAIT_SECONDS = 5.0
_WETYPE_CONTROL_WAIT_SECONDS = 2.0
_WM_CLOSE = 0x0010

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


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


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
        voice_program_manager.VOICE_PROGRAM_CUSTOM,
    }:
        return VoiceHotkeySyncResult(
            provider, False, "local_only", message="该程序只使用 Remote Mic 内的快捷键。"
        )
    if current_platform != "win32":
        return VoiceHotkeySyncResult(
            provider, False, "unsupported_platform", message="仅 Windows 支持自动读取。"
        )
    if provider == voice_program_manager.VOICE_PROGRAM_SOGOU:
        return _read_sogou_hotkey(appdata=appdata)
    if provider == voice_program_manager.VOICE_PROGRAM_WETYPE:
        return _read_wetype_hotkey()
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
        voice_program_manager.VOICE_PROGRAM_CUSTOM,
    }:
        return VoiceHotkeySyncResult(
            provider, True, "local_only", normalized, "快捷键已保存到 Remote Mic。"
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
    if provider == voice_program_manager.VOICE_PROGRAM_WETYPE:
        return _sync_wetype_hotkey(normalized, tokens)
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


def _load_uiautomation():
    try:
        import uiautomation as automation
    except ImportError as exc:
        raise RuntimeError("缺少 Windows 界面自动化组件") from exc
    return automation


def _descendants(control) -> Iterable[object]:
    pending = [control]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(reversed(current.GetChildren()))


def _find_wetype_window(automation):
    window = automation.WindowControl(
        searchDepth=1,
        ClassName=_WETYPE_SETTINGS_CLASS,
    )
    return window if window.Exists(0.2) else None


def _find_wetype_settings_executable() -> Optional[Path]:
    resolved = voice_program_manager.resolve_voice_program(
        {"provider": voice_program_manager.VOICE_PROGRAM_WETYPE}
    )
    if resolved.executable is None:
        return None
    candidate = resolved.executable.parent / _WETYPE_SETTINGS_EXE
    return candidate if candidate.is_file() else None


def _show_wetype_settings(executable: Path) -> None:
    voice_program_manager.open_voice_program_settings(
        executable,
        _WETYPE_SHOW_SETTINGS_ARGUMENT,
    )


def _open_wetype_window(automation):
    existing = _find_wetype_window(automation)
    if existing is not None:
        return existing, False
    executable = _find_wetype_settings_executable()
    if executable is None:
        raise FileNotFoundError("未找到微信输入法设置程序")
    _show_wetype_settings(executable)
    deadline = time.monotonic() + _WETYPE_WINDOW_WAIT_SECONDS
    while time.monotonic() < deadline:
        window = _find_wetype_window(automation)
        if window is not None:
            return window, True
        time.sleep(0.1)
    raise TimeoutError("微信输入法设置窗口未出现")


def _close_wetype_window(window) -> None:
    hwnd = int(window.NativeWindowHandle)
    if hwnd:
        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostMessageW.restype = wintypes.BOOL
        user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)


def _find_voice_navigation(window):
    window_rect = window.BoundingRectangle
    split_x = window_rect.left + (window_rect.right - window_rect.left) * 0.4
    candidates = [
        control
        for control in _descendants(window)
        if control.ControlTypeName == "GroupControl"
        and control.Name == "语音输入"
        and control.BoundingRectangle.left < split_x
    ]
    if len(candidates) != 1:
        raise RuntimeError("微信设置的语音输入入口结构不匹配")
    return candidates[0]


def _find_wetype_hotkey_group(window):
    candidates = []
    for control in _descendants(window):
        if control.ControlTypeName != "GroupControl":
            continue
        name = control.Name.replace("\n", " ").strip()
        if not name:
            continue
        if any(key in name for key in ("Ctrl", "Shift", "Alt", "Win", "F")):
            try:
                _wetype_name_to_hotkey(control.Name)
            except ValueError:
                continue
            candidates.append(control)
    if len(candidates) != 1:
        raise RuntimeError("微信设置的按住说快捷键结构不匹配")
    return candidates[0]


def _show_wetype_voice_page(window) -> None:
    try:
        _find_wetype_hotkey_group(window)
        return
    except RuntimeError:
        pass
    _find_voice_navigation(window).Click()
    deadline = time.monotonic() + _WETYPE_CONTROL_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            _find_wetype_hotkey_group(window)
            return
        except RuntimeError:
            time.sleep(0.05)
    raise TimeoutError("微信设置没有进入语音输入页")


def _wetype_name_to_hotkey(name: str) -> str:
    parts = [part.strip() for part in name.splitlines() if part.strip()]
    tokens: list[str] = []
    index = 0
    while index < len(parts):
        side = ""
        if parts[index] in {"左", "右"}:
            side = "l" if parts[index] == "左" else "r"
            index += 1
            if index >= len(parts):
                raise ValueError("快捷键方向缺少按键")
        label = parts[index].casefold()
        mapping = {
            "ctrl": f"{side}ctrl" if side else "ctrl",
            "shift": f"{side}shift" if side else "shift",
            "alt": f"{side}alt" if side else "alt",
            "win": f"{side}win" if side else "win",
            "空格": "space",
            "space": "space",
            "enter": "enter",
        }
        tokens.append(mapping.get(label, label))
        index += 1
    if not tokens:
        raise ValueError("快捷键为空")
    spec = hotkey.HotkeySpec.parse("+".join(tokens))
    win32_keys.resolve_vk_codes((*spec.modifiers, spec.key))
    return spec.serialize()


def _with_wetype_window(callback: Callable[[object], VoiceHotkeySyncResult]):
    automation = _load_uiautomation()
    user32 = ctypes.windll.user32
    previous_foreground = user32.GetForegroundWindow()
    previous_cursor = _POINT()
    cursor_known = bool(user32.GetCursorPos(ctypes.byref(previous_cursor)))
    window, launched = _open_wetype_window(automation)
    try:
        _show_wetype_voice_page(window)
        return callback(window)
    finally:
        if launched:
            _close_wetype_window(window)
            time.sleep(0.1)
        if cursor_known:
            user32.SetCursorPos(previous_cursor.x, previous_cursor.y)
        if previous_foreground:
            user32.SetForegroundWindow(previous_foreground)


def _read_wetype_hotkey() -> VoiceHotkeySyncResult:
    provider = voice_program_manager.VOICE_PROGRAM_WETYPE

    def read(window):
        shortcut = _wetype_name_to_hotkey(_find_wetype_hotkey_group(window).Name)
        return VoiceHotkeySyncResult(
            provider, True, "read", shortcut, "已读取微信输入法当前的按住说快捷键。"
        )

    try:
        return _with_wetype_window(read)
    except Exception as exc:
        return VoiceHotkeySyncResult(
            provider, False, "read_failed", message=f"读取微信快捷键失败：{exc}"
        )


def _wait_for_wetype_hotkey(window, expected: str) -> tuple[bool, str]:
    deadline = time.monotonic() + _WETYPE_CONTROL_WAIT_SECONDS
    current = ""
    while True:
        try:
            current = _wetype_name_to_hotkey(
                _find_wetype_hotkey_group(window).Name
            )
        except (RuntimeError, ValueError):
            current = ""
        if current == expected:
            return True, current
        if time.monotonic() >= deadline:
            return False, current
        time.sleep(0.05)


def _restore_wetype_hotkey(window, previous: str) -> tuple[bool, str, str]:
    try:
        previous_spec = hotkey.HotkeySpec.parse(previous)
        rollback_tokens = (*previous_spec.modifiers, previous_spec.key)
        _find_wetype_hotkey_group(window).Click()
        time.sleep(0.35)
        win32_input.send_wetype_voice_key_combo_tap(rollback_tokens)
        restored, current = _wait_for_wetype_hotkey(window, previous)
        return restored, current, "" if restored else "恢复后读回不一致"
    except Exception as exc:
        try:
            current = _wetype_name_to_hotkey(
                _find_wetype_hotkey_group(window).Name
            )
        except Exception:
            current = ""
        return False, current, str(exc)


def _sync_wetype_hotkey(
    shortcut: str, tokens: Sequence[str]
) -> VoiceHotkeySyncResult:
    provider = voice_program_manager.VOICE_PROGRAM_WETYPE

    def write(window):
        group = _find_wetype_hotkey_group(window)
        previous = _wetype_name_to_hotkey(group.Name)
        if previous == shortcut:
            return VoiceHotkeySyncResult(
                provider, True, "synced", shortcut, "微信输入法快捷键已经一致。"
            )

        write_started = False
        try:
            group.Click()
            write_started = True
            time.sleep(0.35)
            win32_input.send_wetype_voice_key_combo_tap(tokens)
            matched, _ = _wait_for_wetype_hotkey(window, shortcut)
            if matched:
                return VoiceHotkeySyncResult(
                    provider,
                    True,
                    "synced",
                    shortcut,
                    "已同步到微信输入法的按住说快捷键。",
                )
            failure = RuntimeError("微信输入法写入后读回不一致")
        except Exception as exc:
            failure = exc

        if not write_started:
            raise failure
        restored, current, rollback_error = _restore_wetype_hotkey(window, previous)
        if restored:
            return VoiceHotkeySyncResult(
                provider,
                False,
                "write_failed",
                previous,
                f"同步微信快捷键失败：{failure}；已恢复原快捷键。",
            )
        current_text = f" 当前读到 {current}。" if current else ""
        return VoiceHotkeySyncResult(
            provider,
            False,
            "rollback_failed",
            current,
            message=(
                f"同步微信快捷键失败：{failure}；原快捷键也未能恢复："
                f"{rollback_error or '恢复后读回不一致'}。{current_text}"
            ),
        )

    try:
        return _with_wetype_window(write)
    except Exception as exc:
        return VoiceHotkeySyncResult(
            provider, False, "write_failed", message=f"同步微信快捷键失败：{exc}"
        )
