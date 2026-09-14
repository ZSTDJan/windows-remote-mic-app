"""Settings-window pure logic: button mapping list, voice hotkey, output
endpoint, bridge-launch and log-location status text.

This module is deliberately Tk/Qt-free (XRBM-030 replaced the previous Tk
view with a PySide6-Essentials + Qt Quick/QML one - see
``qt_settings_app.py`` and ``qml/`` - but every piece of validation/save/
launch/log-status logic below stays here so it remains
directly unit-testable without constructing any window at all, matching the
   contract fixed after XRBM-014 review RETRY P1 #7): every piece of
validation/save logic is a plain function (``_action_to_display``,
``_display_to_action``, ``build_save_model``, ``_endpoint_display``,
``_parse_endpoint_display``, ``describe_launch_result``,
``describe_log_open_result``) that tests call directly - see
   tests/test_settings_ui_helpers.py. Legacy voice values render as an
   explicit disabled notice; new saves allow hold-to-talk only on the
   physical microphone button.

``main()`` at the bottom of this module is the only place that touches Qt at
all, and does so via a lazy import inside the function body - importing this
module (e.g. from ``__main__.py``'s ``--dry-run`` smoke check) never
requires PySide6 to be installed, the same optional-dependency convention
this package already uses for ``sounddevice``/``numpy``/``winrt`` (see
``qt_settings_app.py``'s module docstring for the exact error raised when
Qt is missing).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from . import (
    audio_output,
    bridge_launcher,
    device_catalog,
    device_profile,
    hotkey,
    key_mapping,
    logging_setup,
    product_identity,
    win32_keys,
)

_OPEN_APPLICATION_DISPLAY = f"打开{product_identity.DISPLAY_NAME}"

# Reference action names map to semantic values.  The Windows implementation
# is intentionally behind these values; the dropdown must never turn
# ``方向上`` back into a generic ``key_combo`` just because the platform uses
# a key event to deliver it.
_REFERENCE_ACTION_LABELS: Dict[key_mapping.ActionKind, str] = {
    key_mapping.ActionKind.ESCAPE: "Escape",
    key_mapping.ActionKind.RETURN: "回车",
    key_mapping.ActionKind.ARROW_UP: "方向上",
    key_mapping.ActionKind.ARROW_DOWN: "方向下",
    key_mapping.ActionKind.ARROW_LEFT: "方向左",
    key_mapping.ActionKind.ARROW_RIGHT: "方向右",
    key_mapping.ActionKind.DELETE_BACKWARD: "退格",
    key_mapping.ActionKind.SHOW_DESKTOP: "显示桌面",
    key_mapping.ActionKind.CONTEXT_MENU: "右键菜单",
    key_mapping.ActionKind.APP_SWITCHER: "应用切换",
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: "系统音量 +",
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: "系统音量 −",
    key_mapping.ActionKind.SYSTEM_VOLUME_MUTE: "系统静音",
    key_mapping.ActionKind.PLAY_PAUSE: "播放 / 暂停",
    key_mapping.ActionKind.MOUSE_LEFT_CLICK: "鼠标左键单击",
    key_mapping.ActionKind.MOUSE_RIGHT_CLICK: "鼠标右键单击",
    key_mapping.ActionKind.MOUSE_MIDDLE_CLICK: "鼠标中键单击",
    key_mapping.ActionKind.MOUSE_WHEEL_UP: "滚轮向上",
    key_mapping.ActionKind.MOUSE_WHEEL_DOWN: "滚轮向下",
    key_mapping.ActionKind.MOUSE_X1_CLICK: "鼠标 X1 单击",
    key_mapping.ActionKind.MOUSE_X2_CLICK: "鼠标 X2 单击",
    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE: "元素导航开关",
    key_mapping.ActionKind.OPEN_REMOTE_MIC: _OPEN_APPLICATION_DISPLAY,
    key_mapping.ActionKind.OPEN_CODEX: "打开 Codex",
    key_mapping.ActionKind.OPEN_CLAUDE: "打开 Claude",
    key_mapping.ActionKind.OPEN_CMUX: "打开 cmux",
    key_mapping.ActionKind.OPEN_WECHAT: "打开微信",
    key_mapping.ActionKind.OPEN_CURSOR: "打开 Cursor",
    key_mapping.ActionKind.OPEN_SLACK: "打开 Slack",
    key_mapping.ActionKind.OPEN_WECOM: "打开企业微信",
    key_mapping.ActionKind.OPEN_NETEASE_MUSIC: "打开网易云音乐",
    key_mapping.ActionKind.OPEN_CHROME: "打开 Chrome",
    key_mapping.ActionKind.OPEN_EDGE: "打开 Edge",
    key_mapping.ActionKind.OPEN_ZED: "打开 Zed",
}
_REFERENCE_ACTION_KINDS_BY_LABEL: Dict[str, key_mapping.ActionKind] = {
    label: action_kind for action_kind, label in _REFERENCE_ACTION_LABELS.items()
}

# Older display spellings remain accepted for editable fields and tests that
# construct the UI model directly. Persisted bindings store semantic action
# kinds, so rendering always uses the current labels above.
_LEGACY_REFERENCE_ACTION_KINDS_BY_LABEL: Dict[str, key_mapping.ActionKind] = {
    "Return": key_mapping.ActionKind.RETURN,
    "Delete（退格）": key_mapping.ActionKind.DELETE_BACKWARD,
    "上下文菜单": key_mapping.ActionKind.CONTEXT_MENU,
}

# Preset choices shown in the mapping dropdown. Any other
# "mod+mod+key" text is still accepted as a custom shortcut through
# hotkey.HotkeySpec.parse.
ACTION_OPTION_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "按键操作",
        (
            "Escape", "回车", "退格", "方向上", "方向下", "方向左", "方向右",
            "tab", "space", "f5",
        ),
    ),
    (
        "鼠标与导航",
        (
            "鼠标左键单击", "鼠标右键单击", "鼠标中键单击",
            "滚轮向上", "滚轮向下", "鼠标 X1 单击", "鼠标 X2 单击",
            "元素导航开关", "右键菜单",
        ),
    ),
    (
        "系统与媒体",
        (
            "显示桌面", "应用切换", "系统音量 +", "系统音量 −",
            "系统静音", "播放 / 暂停",
        ),
    ),
    (
        "启动应用",
        (
            _OPEN_APPLICATION_DISPLAY, "打开 Codex", "打开 Claude", "打开 cmux", "打开微信",
            "打开 Cursor", "打开 Slack", "打开企业微信", "打开网易云音乐",
            "打开 Chrome", "打开 Edge", "打开 Zed",
        ),
    ),
    ("其他", ("禁用",)),
)

_PRESET_KEY_COMBOS = tuple(
    option
    for _group_title, group_options in ACTION_OPTION_GROUPS
    for option in group_options
)

ACTION_OPTION_GROUP_BY_LABEL = {
    option: group_title
    for group_title, group_options in ACTION_OPTION_GROUPS
    for option in group_options
}

ACTION_OPTION_GROUP_STARTS = frozenset(
    group_options[0]
    for _group_title, group_options in ACTION_OPTION_GROUPS
    if group_options
)

_TRIGGER_MODE_LABELS = {
    key_mapping.VoiceTriggerMode.HOLD: "按住说话",
}

_MAPPING_HOTKEY_TOKEN_ALIASES = {
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
}

_CTRL_HOTKEY_TOKENS = frozenset({"ctrl", "lctrl", "rctrl"})
_ALT_HOTKEY_TOKENS = frozenset({"alt", "lalt", "ralt"})


def voice_hotkey_for_trigger_mode(trigger_mode: key_mapping.VoiceTriggerMode) -> str:
    """Return the physical host shortcut paired with a voice trigger mode."""

    return key_mapping.voice_hotkey_for_trigger_mode(trigger_mode)

# Legacy generic voice text is still accepted so an older in-memory model can
# be saved without being parsed as a keyboard chord. New rows only expose the
# supported hold-to-talk action below.
_VOICE_DISPLAY = "语音（使用专用组合键）"
_VOICE_HOLD_DISPLAY = "按住说话"
_REMOVED_VOICE_DISPLAY = "已停用：旧语音配置（请重新选择）"
_PRIMARY_VOICE_DISPLAYS = (_VOICE_HOLD_DISPLAY,)

# Secondary gestures are optional.  Keep an explicit display value in the
# editable ComboBox so Qt does not fall back to the first real preset (usually
# ``escape``) when an older key_bindings.json has no secondary_bindings map.
SECONDARY_UNCONFIGURED_DISPLAY = "未设置"

_BUTTON_TRIGGER_LABELS = {
    key_mapping.ButtonTrigger.SINGLE_CLICK.value: "单击",
    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: "双击",
    key_mapping.ButtonTrigger.LONG_PRESS.value: "长按",
}

# device_profile.ALL_BUTTON_IDS also carries "volume_mute", a HID usage-table
# entry kept for protocol compatibility (see key_mapping.py's module
# docstring) even though the physical RC003 has no dedicated mute key - only
# Volume + and Volume -. The settings window must not offer a mapping row a
# real remote can never actually trigger (XRBM-019 review round 1 P2), so
# every button list this module builds for display uses this narrowed set
# instead of ALL_BUTTON_IDS directly.
_USER_FACING_BUTTON_IDS = frozenset(device_profile.ALL_BUTTON_IDS - {"volume_mute"})

_ENDPOINT_NAME_HOST_API_SEPARATOR = " — "


class SettingsValidationError(Exception):
    """Raised by build_save_model on invalid input. ``button_id`` is None
    for a hotkey-level error, or the offending button's id for a mapping
    error.
    """

    def __init__(self, button_id: Optional[str], message: str) -> None:
        super().__init__(message)
        self.button_id = button_id
        self.message = message


def _action_to_display(action: key_mapping.ButtonAction) -> str:
    if action.kind == key_mapping.ActionKind.DISABLED:
        return "禁用"
    if action.kind == key_mapping.ActionKind.VOICE:
        return _REMOVED_VOICE_DISPLAY
    if action.kind == key_mapping.ActionKind.VOICE_TOGGLE:
        return _REMOVED_VOICE_DISPLAY
    if action.kind == key_mapping.ActionKind.VOICE_HOLD:
        return _VOICE_HOLD_DISPLAY
    if action.kind == key_mapping.ActionKind.QUICKER_URI:
        return action.uri
    reference_label = _REFERENCE_ACTION_LABELS.get(action.kind)
    if reference_label is not None:
        return reference_label
    # Make old configs readable even before the loader has had a chance to
    # migrate them (e.g. a caller is rendering a raw document in a test).
    legacy_action = key_mapping.semantic_action_for_keys(action.keys)
    if legacy_action is not None:
        return _REFERENCE_ACTION_LABELS[legacy_action.kind]
    return "+".join(action.keys)


def normalize_mapping_hotkey_text(text: str) -> str:
    """Validate manual button-mapping text and return persisted key tokens.

    This is deliberately separate from voice-hotkey parsing. Button mappings
    accept a few user-facing Chinese aliases and treat a lone generic ``win``
    as the physical left Windows key, while voice settings keep their existing
    stricter rules.
    """

    normalized_text = str(text).strip().replace("＋", "+")
    raw_tokens = [token.strip() for token in normalized_text.split("+")]
    if not normalized_text or any(not token for token in raw_tokens):
        raise hotkey.HotkeyParseError("请输入单键或用 + 连接的组合键。")
    tokens = [
        _MAPPING_HOTKEY_TOKEN_ALIASES.get(token.casefold(), token.casefold())
        for token in raw_tokens
    ]
    if len(tokens) == 1 and tokens[0] == "win":
        tokens[0] = "lwin"
    parsed = hotkey.HotkeySpec.parse("+".join(tokens))
    resolved_tokens = tuple(parsed.modifiers) + (parsed.key,)
    if (
        parsed.key == "delete"
        and _CTRL_HOTKEY_TOKENS.intersection(resolved_tokens)
        and _ALT_HOTKEY_TOKENS.intersection(resolved_tokens)
    ):
        raise hotkey.HotkeyParseError(
            "Ctrl+Alt+Delete 是 Windows 安全按键，软件不能模拟。"
        )
    try:
        win32_keys.resolve_vk_codes(resolved_tokens)
    except win32_keys.UnknownKeyTokenError as exc:
        raise hotkey.HotkeyParseError(str(exc)) from exc
    return "+".join(resolved_tokens)


def _display_to_action(text: str) -> key_mapping.ButtonAction:
    text = text.strip()
    if text in ("禁用", "disabled", SECONDARY_UNCONFIGURED_DISPLAY):
        return key_mapping.ButtonAction(key_mapping.ActionKind.DISABLED)
    if text == _VOICE_DISPLAY:
        return key_mapping.ButtonAction(key_mapping.ActionKind.VOICE)
    if text == _VOICE_HOLD_DISPLAY:
        return key_mapping.ButtonAction(key_mapping.ActionKind.VOICE_HOLD)
    if text == "系统音量 -":
        text = "系统音量 −"
    reference_kind = _REFERENCE_ACTION_KINDS_BY_LABEL.get(text)
    if reference_kind is None:
        reference_kind = _LEGACY_REFERENCE_ACTION_KINDS_BY_LABEL.get(text)
    if reference_kind is not None:
        return key_mapping.ButtonAction(reference_kind)
    # Keep the previous spelling accepted for users who copied the macOS
    # reference label into the Windows field.
    if text == "Command-Tab":
        return key_mapping.ButtonAction(key_mapping.ActionKind.APP_SWITCHER)
    if text.casefold().startswith("quicker:"):
        try:
            uri = key_mapping.normalize_quicker_uri(text)
        except (TypeError, ValueError) as exc:
            raise hotkey.HotkeyParseError(
                "Quicker URI 必须使用 quicker:runaction:动作ID或名称，可在末尾附加 ?参数。"
            ) from exc
        return key_mapping.ButtonAction(
            key_mapping.ActionKind.QUICKER_URI,
            uri=uri,
        )
    normalized_hotkey = normalize_mapping_hotkey_text(text)
    return key_mapping.ButtonAction(
        key_mapping.ActionKind.KEY_COMBO, tuple(normalized_hotkey.split("+"))
    )


def _validated_button_action(
    button_id: str,
    trigger: str,
    text: str,
) -> Optional[key_mapping.ButtonAction]:
    """Parse one editor field using the same rules as the final save."""

    trigger_label = _BUTTON_TRIGGER_LABELS.get(trigger)
    if trigger_label is None:
        raise SettingsValidationError(button_id, f"不支持的按键操作：{trigger}")

    clean_text = str(text).strip()
    if not clean_text:
        return None
    if trigger != key_mapping.ButtonTrigger.SINGLE_CLICK.value and clean_text in (
        "禁用",
        "disabled",
        SECONDARY_UNCONFIGURED_DISPLAY,
    ):
        return None
    if clean_text == _REMOVED_VOICE_DISPLAY:
        raise SettingsValidationError(
            button_id,
            f"{trigger_label}：旧语音配置已停用，请重新选择",
        )

    try:
        action = _display_to_action(clean_text)
    except hotkey.HotkeyParseError as exc:
        detail = str(exc).strip()
        if detail.startswith("Quicker URI") or "Windows 安全按键" in detail:
            message = f"{trigger_label}：{detail}"
        else:
            message = f"{trigger_label}：“{clean_text}”不支持映射，请重新录入"
        raise SettingsValidationError(button_id, message) from exc

    if key_mapping.is_voice_action(action):
        if trigger != key_mapping.ButtonTrigger.SINGLE_CLICK.value:
            raise SettingsValidationError(
                button_id,
                f"{trigger_label}：语音动作只能用于主映射，请选择普通动作或快捷键",
            )
        if button_id != "mic":
            raise SettingsValidationError(
                button_id,
                f"{trigger_label}：只有实体话筒键支持“按住说话”",
            )
    return action


def button_action_validation_message(
    button_id: str,
    trigger: str,
    text: str,
) -> str:
    """Return a user-facing editor error, or an empty string when valid."""

    try:
        _validated_button_action(button_id, trigger, text)
    except SettingsValidationError as exc:
        return exc.message
    return ""


def _endpoint_display(endpoint: audio_output.AudioEndpoint) -> str:
    if endpoint.host_api:
        return f"{endpoint.name}{_ENDPOINT_NAME_HOST_API_SEPARATOR}{endpoint.host_api}"
    return endpoint.name


def _parse_endpoint_display(text: str) -> Tuple[str, str]:
    """Inverse of _endpoint_display: returns (name, host_api), where
    host_api is "" if the text has no disambiguating suffix.
    """

    text = text.strip()
    if _ENDPOINT_NAME_HOST_API_SEPARATOR in text:
        name, host_api = text.rsplit(_ENDPOINT_NAME_HOST_API_SEPARATOR, 1)
        return name.strip(), host_api.strip()
    return text, ""


def build_save_model(
    *,
    button_display_map: Dict[str, str],
    secondary_display_map: Optional[Dict[str, Dict[str, str]]] = None,
    display_note_map: Optional[Dict[str, Dict[str, str]]] = None,
    hotkey_text: str,
    trigger_mode: key_mapping.VoiceTriggerMode,
    endpoint_display_text: str,
    base_config: dict,
    base_bindings: dict,
    selected_device_profile: str = device_catalog.RC003_ID,
    voice_hotkeys: Optional[Dict[str, str]] = None,
    validate_voice_hotkeys: bool = True,
) -> Tuple[dict, dict]:
    """Pure validation+build step for "Save"/"Restore defaults", with no Tk
    dependency at all - directly unit tested without constructing any
    window (see tests/test_settings_ui_helpers.py). Raises
    SettingsValidationError on invalid input; never raises a Tk exception.
    """

    trigger_mode = key_mapping.VoiceTriggerMode.HOLD
    mode_hotkeys = {
        "hold": key_mapping.voice_hotkey_for_trigger_mode(trigger_mode)
    }
    if voice_hotkeys is None:
        mode_hotkeys["hold"] = hotkey_text.strip()
    else:
        mode_hotkeys["hold"] = str(voice_hotkeys.get("hold", "")).strip()

    bindings: Dict[str, dict] = {}
    voice_binding: Optional[Tuple[str, key_mapping.VoiceTriggerMode]] = None
    for button_id, text in button_display_map.items():
        action = _validated_button_action(
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK.value,
            text,
        )
        if action is None:
            continue
        voice_mode = key_mapping.voice_trigger_mode_for_action(
            action,
            legacy_mode=trigger_mode,
        )
        if voice_mode is not None:
            voice_binding = (button_id, voice_mode)
            action = key_mapping.voice_action_for_trigger_mode(
                key_mapping.VoiceTriggerMode.HOLD
            )
        bindings[button_id] = action.to_dict()

    active_mode = key_mapping.VoiceTriggerMode.HOLD
    active_hotkey_text = mode_hotkeys["hold"]
    if validate_voice_hotkeys and voice_binding is not None and not active_hotkey_text:
        raise SettingsValidationError(
            voice_binding[0],
            f"请先录入{_TRIGGER_MODE_LABELS[active_mode]}的语音快捷键",
        )

    for mode in (key_mapping.VoiceTriggerMode.HOLD,):
        candidate = mode_hotkeys["hold"]
        if not validate_voice_hotkeys or not candidate:
            continue
        try:
            parsed_hotkey = hotkey.HotkeySpec.parse(candidate)
            win32_keys.resolve_vk_codes(
                tuple(parsed_hotkey.modifiers) + (parsed_hotkey.key,)
            )
        except hotkey.HotkeyParseError as exc:
            raise SettingsValidationError(
                None, f"{_TRIGGER_MODE_LABELS[mode]}快捷键：{exc}"
            ) from exc
        except win32_keys.UnknownKeyTokenError as exc:
            raise SettingsValidationError(
                None, f"{_TRIGGER_MODE_LABELS[mode]}快捷键：{exc}"
            ) from exc

    if secondary_display_map is None:
        raw_secondary = base_bindings.get("secondary_bindings", {})
        secondary_bindings = (
            copy.deepcopy(raw_secondary) if isinstance(raw_secondary, dict) else {}
        )
        for button_id, trigger_map in secondary_bindings.items():
            if not isinstance(trigger_map, dict):
                continue
            for raw_action in trigger_map.values():
                try:
                    action = key_mapping.ButtonAction.from_dict(raw_action)
                except (KeyError, TypeError, ValueError) as exc:
                    raise SettingsValidationError(
                        button_id,
                        "双击或长按动作配置无效，请重新选择后保存。",
                    ) from exc
                if action.kind == key_mapping.ActionKind.DISABLED:
                    continue
                if key_mapping.is_voice_action(action):
                    raise SettingsValidationError(
                        button_id,
                        "语音动作只能用于主映射，不能设置为双击或长按动作。",
                    )
    else:
        secondary_bindings: Dict[str, Dict[str, dict]] = {}
        for button_id, trigger_map in secondary_display_map.items():
            if not isinstance(trigger_map, dict):
                continue
            for trigger_name, text in trigger_map.items():
                action = _validated_button_action(
                    button_id,
                    trigger_name,
                    text,
                )
                if action is None:
                    continue
                if action.kind == key_mapping.ActionKind.DISABLED:
                    continue
                secondary_bindings.setdefault(button_id, {})[trigger_name] = action.to_dict()

    if display_note_map is None:
        raw_display_notes = base_bindings.get("display_notes", {})
        display_notes = (
            copy.deepcopy(raw_display_notes)
            if isinstance(raw_display_notes, dict)
            else {}
        )
    else:
        display_notes: Dict[str, Dict[str, str]] = {}
        valid_note_triggers = {
            key_mapping.ButtonTrigger.SINGLE_CLICK.value,
            key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
            key_mapping.ButtonTrigger.LONG_PRESS.value,
        }
        for button_id, trigger_map in display_note_map.items():
            if button_id not in device_profile.ALL_BUTTON_IDS or not isinstance(
                trigger_map, dict
            ):
                continue
            for trigger_name, note in trigger_map.items():
                if trigger_name not in valid_note_triggers:
                    continue
                if not isinstance(note, str):
                    continue
                clean_note = note.strip()
                if clean_note and clean_note != "未命名":
                    display_notes.setdefault(button_id, {})[
                        trigger_name
                    ] = clean_note

    # RC003 remote-button combinations are retired. Keep the normalized
    # legacy document opaque for downgrade compatibility, but never let it
    # participate in validation of the current visible mappings.
    raw_combo = base_bindings.get("combo_bindings", {})
    combo_bindings = copy.deepcopy(raw_combo) if isinstance(raw_combo, dict) else {}

    endpoint_name, endpoint_host_api = _parse_endpoint_display(endpoint_display_text)

    new_config = dict(base_config)
    new_config["selected_device_profile"] = device_catalog.normalize_device_id(
        selected_device_profile
    )
    new_config["voice_hotkey"] = active_hotkey_text
    new_config["voice_hotkeys"] = mode_hotkeys
    new_config["voice_trigger_mode"] = active_mode.value
    new_config.pop("voice_release_finish_tap_enabled", None)
    new_config["output_endpoint_name"] = endpoint_name
    new_config["output_endpoint_host_api"] = endpoint_host_api

    new_bindings = dict(base_bindings)
    new_bindings["bindings"] = bindings
    new_bindings["secondary_bindings"] = secondary_bindings
    new_bindings["display_notes"] = display_notes
    new_bindings["combo_bindings"] = combo_bindings

    return new_config, new_bindings


@dataclass(frozen=True)
class DefaultDisplayState:
    """What "restore defaults" resets every widget to - a pure snapshot, so
    it can be asserted on directly in tests without touching a StringVar.
    """

    button_display_map: Dict[str, str]
    secondary_display_map: Dict[str, Dict[str, str]]
    hotkey_text: str
    voice_hotkeys: Dict[str, str]
    trigger_mode_label: str


def default_display_state() -> DefaultDisplayState:
    defaults = key_mapping.default_button_actions()
    button_display_map = {
        button_id: _action_to_display(action) for button_id, action in defaults.items()
    }
    for button_id in _USER_FACING_BUTTON_IDS:
        button_display_map.setdefault(button_id, "")
    secondary_display_map = {
        button_id: {
            key_mapping.ButtonTrigger.DOUBLE_CLICK.value: "",
            key_mapping.ButtonTrigger.LONG_PRESS.value: "",
        }
        for button_id in _USER_FACING_BUTTON_IDS
    }
    voice_hotkeys = {
        "hold": key_mapping.voice_hotkey_for_trigger_mode(
            key_mapping.VoiceTriggerMode.HOLD
        )
    }
    return DefaultDisplayState(
        button_display_map=button_display_map,
        secondary_display_map=secondary_display_map,
        hotkey_text=voice_hotkeys["hold"],
        voice_hotkeys=voice_hotkeys,
        trigger_mode_label=_TRIGGER_MODE_LABELS[key_mapping.VoiceTriggerMode.HOLD],
    )


# Bridge-control status text (XRBM-029). Kept as pure, Tk-free functions -
# same testability contract as the save-model helpers above (see
# tests/test_settings_ui_helpers.py) - so every stable state is asserted on
# directly without constructing a window or a real subprocess.
#
# Wording contract: a STARTED result is deliberately never described as
# "RC003 已连接"/"RC003 connected" - only as the process itself still being
# alive. The settings controller continues polling the runtime status and
# promotes the UI to the connected state only after the bridge reports it.
LAUNCH_NOT_STARTED_TEXT = "服务未运行；点击“启动服务”后，按键和语音才会生效"
LAUNCH_ALREADY_RUNNING_TEXT = (
    f"服务已在运行；正在检查{device_catalog.RC003_DISPLAY_NAME}的连接"
)
LAUNCH_STATUS_UNKNOWN_TEXT = "无法确认服务状态；请查看 app.log，勿重复启动"


def describe_launch_result(result: bridge_launcher.LaunchResult) -> str:
    if result.outcome is bridge_launcher.LaunchOutcome.STARTED:
        pid_text = f"（PID {result.pid}）" if result.pid is not None else ""
        return (
            f"服务已启动{pid_text}；正在连接{device_catalog.RC003_DISPLAY_NAME}，"
            "首次可能约 1 分钟"
        )
    if result.outcome is bridge_launcher.LaunchOutcome.ALREADY_RUNNING:
        return f"服务已在运行（代码 {result.exit_code}）；无需重复启动"
    if result.outcome is bridge_launcher.LaunchOutcome.STATUS_UNKNOWN:
        pid_text = f"（PID {result.pid}）" if result.pid is not None else ""
        return f"服务已创建{pid_text}，但状态未知；请查看 app.log，勿重复启动"
    if result.outcome is bridge_launcher.LaunchOutcome.QUICK_EXIT:
        return f"服务启动后立即退出（代码 {result.exit_code}）；请查看 app.log"
    # LAUNCH_FAILED
    return f"服务启动失败：{result.error}；请查看 app.log"


def describe_log_open_result(result: logging_setup.LogOpenResult) -> str:
    if result.outcome is logging_setup.LogOpenOutcome.OPENED:
        note = ""
        if result.location.status is logging_setup.LogLocationStatus.FILE_MISSING:
            note = "（该目录存在，但 app.log 尚不存在——桥接可能还没有运行过一次，这不是错误。）"
        return f"已打开日志目录：{result.location.directory}{note}"
    if result.outcome is logging_setup.LogOpenOutcome.DIRECTORY_MISSING:
        return (
            f"日志目录尚不存在：{result.location.directory}。这通常表示桥接程序在这台"
            "电脑上还没有运行过；本程序不会为了显示而伪造日志。"
        )
    return f"无法打开日志目录（{result.error}）：{result.location.directory}"


def main(*, start_hidden: bool = False, start_bridge: bool = False) -> None:
    """Launches the Qt Quick/QML settings window (XRBM-030). Imports
    ``qt_settings_app`` lazily so importing THIS module (e.g. from
    ``__main__.py``'s ``--dry-run`` smoke check, or from any test that only
    needs the pure functions above) never requires PySide6 to be installed -
    see ``qt_settings_app.py``'s module docstring for the exact, clear error
    raised here if it is missing.
    """

    from . import qt_settings_app

    qt_settings_app.run_settings_window(
        start_hidden=start_hidden,
        start_bridge=start_bridge,
    )


if __name__ == "__main__":
    main()
