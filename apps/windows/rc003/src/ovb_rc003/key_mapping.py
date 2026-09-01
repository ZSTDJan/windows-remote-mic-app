"""RC003 -> Windows *semantic* action mapping.

The reference app stores behavior actions (``arrowUp``, ``showDesktop``,
``openCodex``), not presentation strings such as ``"up"`` or
``"win+d"``.  Windows keeps that same separation: this module is pure action
data, while :mod:`app` and :mod:`win32_input` execute each action through its
own platform operation.  ``KEY_COMBO`` remains available for a genuinely
custom user shortcut and for backward-compatible hand-edited files.

Default table matches the reference app's action choices:

| RC003 按键 | Windows 候选动作 |
| --- | --- |
| 麦克风 | 专用语音生命周期 |
| 电源 | Escape |
| 上 / 下 / 左 / 右 | 对应方向动作 |
| 确定 | 回车 |
| 返回 | 退格 |
| 音量 + / − | 系统音量 + / − |
| 主页 | 显示桌面 |
| 菜单 | 右键菜单 |
| TV | 应用切换 |

The RC003 HID usage table also defines a "volume_mute" usage (see
device_profile.BUTTON_USAGE_IDS), but this Windows client documents that the
physical remote has no dedicated mute key - "系统静音" is only an optional
assignable action, never a default. This module mirrors that: mute is a valid,
bindable logical button but intentionally has no default entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


class ActionKind(str, Enum):
    DISABLED = "disabled"
    KEY_COMBO = "key_combo"
    QUICKER_URI = "quicker_uri"
    ESCAPE = "escape"
    RETURN = "return"
    ARROW_UP = "arrow_up"
    ARROW_DOWN = "arrow_down"
    ARROW_LEFT = "arrow_left"
    ARROW_RIGHT = "arrow_right"
    DELETE_BACKWARD = "delete_backward"
    SHOW_DESKTOP = "show_desktop"
    CONTEXT_MENU = "context_menu"
    APP_SWITCHER = "app_switcher"
    SYSTEM_VOLUME_UP = "system_volume_up"
    SYSTEM_VOLUME_DOWN = "system_volume_down"
    SYSTEM_VOLUME_MUTE = "system_volume_mute"
    PLAY_PAUSE = "play_pause"
    ELEMENT_NAVIGATION_TOGGLE = "element_navigation_toggle"
    # ``VOICE`` and ``VOICE_TOGGLE`` remain parseable only so schema-1 files
    # can be failed closed with an explicit migration notice. They are not
    # selectable or executable product actions anymore.
    VOICE = "voice"
    VOICE_TOGGLE = "voice_toggle"
    VOICE_HOLD = "voice_hold"
    OPEN_REMOTE_MIC = "open_remote_mic"
    OPEN_CODEX = "open_codex"
    OPEN_CLAUDE = "open_claude"
    OPEN_CMUX = "open_cmux"
    OPEN_WECHAT = "open_wechat"
    OPEN_CURSOR = "open_cursor"
    OPEN_SLACK = "open_slack"
    OPEN_WECOM = "open_wecom"
    OPEN_NETEASE_MUSIC = "open_netease_music"
    OPEN_CHROME = "open_chrome"
    OPEN_EDGE = "open_edge"
    OPEN_ZED = "open_zed"


class ButtonTrigger(str, Enum):
    """The three gestures available for an ordinary RC003 button."""

    SINGLE_CLICK = "single_click"
    DOUBLE_CLICK = "double_click"
    LONG_PRESS = "long_press"


class VoiceTriggerMode(str, Enum):
    TOGGLE = "toggle"
    HOLD = "hold"


VOICE_ACTION_KINDS = frozenset(
    {
        ActionKind.VOICE,
        ActionKind.VOICE_TOGGLE,
        ActionKind.VOICE_HOLD,
    }
)

SUPPORTED_VOICE_ACTION_KINDS = frozenset({ActionKind.VOICE_HOLD})


def is_voice_action(action: "ButtonAction") -> bool:
    """Return whether ``action`` is current or legacy RC003 voice data."""

    return action.kind in VOICE_ACTION_KINDS


def is_supported_voice_action(action: "ButtonAction") -> bool:
    """Return whether ``action`` is an executable RC003 voice action."""

    return action.kind in SUPPORTED_VOICE_ACTION_KINDS


def voice_action_for_trigger_mode(trigger_mode: VoiceTriggerMode) -> "ButtonAction":
    """Build the explicit primary mapping for one voice lifecycle."""

    kind = (
        ActionKind.VOICE_TOGGLE
        if trigger_mode == VoiceTriggerMode.TOGGLE
        else ActionKind.VOICE_HOLD
    )
    return ButtonAction(kind)


def voice_trigger_mode_for_action(
    action: "ButtonAction",
    *,
    legacy_mode: Optional[VoiceTriggerMode] = None,
) -> Optional[VoiceTriggerMode]:
    """Resolve an explicit voice action, preserving legacy ``VOICE`` files."""

    if action.kind == ActionKind.VOICE_TOGGLE:
        return VoiceTriggerMode.TOGGLE
    if action.kind == ActionKind.VOICE_HOLD:
        return VoiceTriggerMode.HOLD
    if action.kind == ActionKind.VOICE:
        return legacy_mode
    return None


# Right Alt remains the conservative default for hold-to-talk. A proposed
# Ctrl+Alt+F8 default was rejected after UU remote-control testing showed that
# the forwarded key-up edge does not preserve a stable physical hold duration.
DEFAULT_HOLD_VOICE_HOTKEY = "ralt"
LEGACY_HOLD_VOICE_HOTKEY = "ralt"
LEGACY_TOGGLE_VOICE_HOTKEY = "ralt+space"

# HOLD is the only selectable product mode. TOGGLE remains in this lookup only
# so schema-1 data can still be identified and failed closed during migration.
VOICE_HOTKEY_PRESETS = {
    VoiceTriggerMode.TOGGLE: LEGACY_TOGGLE_VOICE_HOTKEY,
    VoiceTriggerMode.HOLD: DEFAULT_HOLD_VOICE_HOTKEY,
}

# These values were shipped by earlier Windows builds. They remain recognizable
# so old configuration can keep its prior compatibility behavior without being
# mistaken for the default of a fresh installation.
LEGACY_VOICE_HOTKEYS = frozenset(
    {
        LEGACY_HOLD_VOICE_HOTKEY,
        LEGACY_TOGGLE_VOICE_HOTKEY,
        "lctrl+win",
        "lctrl+lwin",
    }
)


# Exact old Windows chords that were previously presented as the reference
# action labels.  These are migrations, not the representation used for new
# saves.  ``alt+esc`` is included because the first Windows build shipped it
# as the TV default before it was corrected to the native app-switch action.
LEGACY_SEMANTIC_ACTIONS = {
    ("escape",): ActionKind.ESCAPE,
    ("enter",): ActionKind.RETURN,
    ("up",): ActionKind.ARROW_UP,
    ("down",): ActionKind.ARROW_DOWN,
    ("left",): ActionKind.ARROW_LEFT,
    ("right",): ActionKind.ARROW_RIGHT,
    ("backspace",): ActionKind.DELETE_BACKWARD,
    ("win", "d"): ActionKind.SHOW_DESKTOP,
    ("shift", "f10"): ActionKind.CONTEXT_MENU,
    ("alt", "tab"): ActionKind.APP_SWITCHER,
    ("alt", "esc"): ActionKind.APP_SWITCHER,
}


APPLICATION_ACTIONS = frozenset(
    {
        ActionKind.OPEN_REMOTE_MIC,
        ActionKind.OPEN_CODEX,
        ActionKind.OPEN_CLAUDE,
        ActionKind.OPEN_CMUX,
        ActionKind.OPEN_WECHAT,
        ActionKind.OPEN_CURSOR,
        ActionKind.OPEN_SLACK,
        ActionKind.OPEN_WECOM,
        ActionKind.OPEN_NETEASE_MUSIC,
        ActionKind.OPEN_CHROME,
        ActionKind.OPEN_EDGE,
        ActionKind.OPEN_ZED,
    }
)

REPEATABLE_ACTIONS = frozenset(
    {
        ActionKind.ARROW_UP,
        ActionKind.ARROW_DOWN,
        ActionKind.ARROW_LEFT,
        ActionKind.ARROW_RIGHT,
        ActionKind.DELETE_BACKWARD,
        ActionKind.SYSTEM_VOLUME_UP,
        ActionKind.SYSTEM_VOLUME_DOWN,
    }
)


QUICKER_URI_PREFIX = "quicker:runaction:"
MAX_QUICKER_URI_LENGTH = 2048

# The first combination implementation deliberately behaves like one Fn
# layer.  Only a non-repeating utility key may be the modifier, while the
# second key stays in the ordinary navigation cluster.  This avoids exposing
# the microphone lifecycle, power behavior, or the Home+Menu pairing chord.
COMBO_MODIFIER_BUTTON_IDS = ("tv", "menu", "home")
COMBO_ACTION_BUTTON_IDS = (
    "up",
    "down",
    "left",
    "right",
    "ok",
    "back",
    "volume_up",
    "volume_down",
)


def normalize_quicker_uri(uri: str) -> str:
    """Validate the narrow Quicker action URI accepted by button mappings."""

    if not isinstance(uri, str):
        raise TypeError("Quicker URI must be text")
    normalized = uri.strip()
    if not normalized:
        raise ValueError("Quicker URI must not be empty")
    if len(normalized) > MAX_QUICKER_URI_LENGTH:
        raise ValueError("Quicker URI is too long")
    if any(character in normalized for character in ("\x00", "\r", "\n")):
        raise ValueError("Quicker URI must stay on one line")
    if not normalized.casefold().startswith(QUICKER_URI_PREFIX):
        raise ValueError("only quicker:runaction: URIs are supported")
    action_identifier = normalized[len(QUICKER_URI_PREFIX) :].partition("?")[0]
    if not action_identifier.strip():
        raise ValueError("Quicker URI must contain an action identifier")
    return QUICKER_URI_PREFIX + normalized[len(QUICKER_URI_PREFIX) :]


def semantic_action_for_keys(keys: Tuple[str, ...]) -> Optional["ButtonAction"]:
    """Return the semantic action represented by one legacy key tuple."""

    action_kind = LEGACY_SEMANTIC_ACTIONS.get(tuple(keys))
    if action_kind is None:
        return None
    return ButtonAction(action_kind)


def action_allows_repeat(action: "ButtonAction") -> bool:
    """Allow hold-repeat only for actions with clear repeat semantics.

    Custom key combinations remain one tap per physical press. Replaying a
    modifier chord such as Shift+3 on a timer is both surprising and more
    likely to expose an incomplete host key release. Navigation, backspace,
    and volume changes keep the expected remote-control repeat behavior.
    """

    return action.kind in REPEATABLE_ACTIONS


def voice_hotkey_for_trigger_mode(trigger_mode: VoiceTriggerMode) -> str:
    """Return the host shortcut paired with a voice trigger mode."""

    return VOICE_HOTKEY_PRESETS[trigger_mode]


@dataclass(frozen=True)
class ButtonAction:
    kind: ActionKind
    keys: Tuple[str, ...] = field(default_factory=tuple)
    uri: str = ""

    def to_dict(self) -> dict:
        data = {"kind": self.kind.value, "keys": list(self.keys)}
        if self.kind == ActionKind.QUICKER_URI:
            data["uri"] = normalize_quicker_uri(self.uri)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ButtonAction":
        if not isinstance(data, dict):
            raise TypeError("button action must be a mapping")
        kind = ActionKind(data["kind"])
        raw_keys = data.get("keys", ())
        if not isinstance(raw_keys, (list, tuple)) or not all(
            isinstance(key, str) and key.strip() for key in raw_keys
        ):
            raise ValueError("button action keys must be non-empty strings")
        keys = tuple(key.strip().lower() for key in raw_keys)
        raw_uri = data.get("uri", "")
        if not isinstance(raw_uri, str):
            raise ValueError("button action URI must be text")
        uri = raw_uri.strip()
        if kind == ActionKind.KEY_COMBO and not keys:
            raise ValueError("key_combo action must contain at least one key")
        if kind != ActionKind.KEY_COMBO and keys:
            raise ValueError("non-key action must not contain keys")
        if kind == ActionKind.QUICKER_URI:
            uri = normalize_quicker_uri(uri)
        elif uri:
            raise ValueError("non-URI action must not contain a URI")
        return cls(kind=kind, keys=keys, uri=uri)


def button_action_for(
    bindings: Dict[str, object], button_id: str, trigger: ButtonTrigger
) -> ButtonAction:
    """Read one gesture action from the versioned bindings document.

    The original Windows build stored the single action directly under each
    button.  Secondary actions use the reference project's separate map so
    every existing ``key_bindings.json`` remains valid and keeps its primary
    mapping unchanged.
    """

    button_bindings = bindings.get("bindings", {})
    if not isinstance(button_bindings, dict):
        return ButtonAction(ActionKind.DISABLED)
    if trigger == ButtonTrigger.SINGLE_CLICK:
        raw = button_bindings.get(button_id)
    else:
        secondary = bindings.get("secondary_bindings", {})
        raw = (
            secondary.get(button_id, {}).get(trigger.value)
            if isinstance(secondary, dict)
            and isinstance(secondary.get(button_id, {}), dict)
            else None
        )
    if not isinstance(raw, dict):
        return ButtonAction(ActionKind.DISABLED)
    try:
        return ButtonAction.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return ButtonAction(ActionKind.DISABLED)


def has_secondary_action(bindings: Dict[str, object], button_id: str) -> bool:
    return any(
        button_action_for(bindings, button_id, trigger).kind != ActionKind.DISABLED
        for trigger in (ButtonTrigger.DOUBLE_CLICK, ButtonTrigger.LONG_PRESS)
    )


def button_combo_modifier(bindings: Dict[str, object]) -> Optional[str]:
    raw = bindings.get("combo_bindings", {})
    if not isinstance(raw, dict):
        return None
    modifier = raw.get("modifier")
    if modifier not in COMBO_MODIFIER_BUTTON_IDS:
        return None
    actions = raw.get("bindings", {})
    if not isinstance(actions, dict):
        return None
    if not any(
        button_combo_action_for(bindings, button_id).kind != ActionKind.DISABLED
        for button_id in COMBO_ACTION_BUTTON_IDS
    ):
        return None
    return str(modifier)


def button_combo_action_for(
    bindings: Dict[str, object], button_id: str
) -> ButtonAction:
    if button_id not in COMBO_ACTION_BUTTON_IDS:
        return ButtonAction(ActionKind.DISABLED)
    raw_combo = bindings.get("combo_bindings", {})
    if not isinstance(raw_combo, dict):
        return ButtonAction(ActionKind.DISABLED)
    raw_actions = raw_combo.get("bindings", {})
    if not isinstance(raw_actions, dict):
        return ButtonAction(ActionKind.DISABLED)
    raw_action = raw_actions.get(button_id)
    if not isinstance(raw_action, dict):
        return ButtonAction(ActionKind.DISABLED)
    try:
        action = ButtonAction.from_dict(raw_action)
    except (KeyError, TypeError, ValueError):
        return ButtonAction(ActionKind.DISABLED)
    if is_voice_action(action):
        return ButtonAction(ActionKind.DISABLED)
    return action


# Buttons that have a defined default action out of the box. "volume_mute" is
# deliberately absent (see module docstring).
DEFAULT_BUTTON_IDS = frozenset(
    {
        "mic",
        "power",
        "up",
        "down",
        "left",
        "right",
        "ok",
        "back",
        "volume_up",
        "volume_down",
        "home",
        "menu",
        "tv",
    }
)


def default_button_actions() -> Dict[str, ButtonAction]:
    return {
        "mic": voice_action_for_trigger_mode(VoiceTriggerMode.HOLD),
        "power": ButtonAction(ActionKind.ESCAPE),
        "up": ButtonAction(ActionKind.ARROW_UP),
        "down": ButtonAction(ActionKind.ARROW_DOWN),
        "left": ButtonAction(ActionKind.ARROW_LEFT),
        "right": ButtonAction(ActionKind.ARROW_RIGHT),
        "ok": ButtonAction(ActionKind.RETURN),
        "back": ButtonAction(ActionKind.DELETE_BACKWARD),
        "volume_up": ButtonAction(ActionKind.SYSTEM_VOLUME_UP),
        "volume_down": ButtonAction(ActionKind.SYSTEM_VOLUME_DOWN),
        "home": ButtonAction(ActionKind.SHOW_DESKTOP),
        "menu": ButtonAction(ActionKind.CONTEXT_MENU),
        "tv": ButtonAction(ActionKind.APP_SWITCHER),
    }
