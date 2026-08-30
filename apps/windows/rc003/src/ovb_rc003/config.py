"""Configuration persistence with an enforced privacy contract.

Config root: ``%LOCALAPPDATA%\\RemoteMic\\RC003`` (falls back to the
user's home directory if ``LOCALAPPDATA`` is unset, e.g. when unit testing on
non-Windows). Two JSON files live there: ``config.json`` (tuning/behavior) and
``key_bindings.json`` (per-button actions plus the voice hotkey).

Hard privacy rule: neither file may ever contain a real Bluetooth address,
HID device interface path/GUID, or device token. ``save_config`` and
``save_key_bindings`` actively refuse to write any of ``FORBIDDEN_KEYS`` -
this is enforced in code, not just by convention, and is covered by
tests/test_config.py and tests/test_privacy_contract.py.

The guard walks the ENTIRE structure recursively (nested dicts, and dicts
nested inside lists at any depth), not just the top-level keys - a field
like ``bindings.menu.metadata.address`` is refused exactly the same as a
top-level ``address`` key (see XRBM-014 review RETRY P1 #6).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Union

APP_ID = "RemoteMic"
PRODUCT_ID = "RC003"

CONFIG_FILENAME = "config.json"
KEY_BINDINGS_FILENAME = "key_bindings.json"

SCHEMA_VERSION = 9

CLOSE_BEHAVIOR_HIDE_TO_TRAY = "hide_to_tray"
CLOSE_BEHAVIOR_QUIT = "quit"
VALID_CLOSE_BEHAVIORS = frozenset(
    {CLOSE_BEHAVIOR_HIDE_TO_TRAY, CLOSE_BEHAVIOR_QUIT}
)

RUNTIME_LEGACY_VOICE_MODE_KEY = "_legacy_voice_trigger_mode"
RUNTIME_REMOVED_VOICE_BINDINGS_KEY = "_removed_voice_bindings"
_RUNTIME_ONLY_KEYS = frozenset(
    {
        RUNTIME_LEGACY_VOICE_MODE_KEY,
        RUNTIME_REMOVED_VOICE_BINDINGS_KEY,
    }
)

# Any config key matching one of these names is refused at save time,
# regardless of which file it would land in.
FORBIDDEN_KEYS = frozenset(
    {
        "address",
        "bluetooth_address",
        "bt_address",
        "mac_address",
        "device_match",
        "device_id",
        "ble_device_id",
        "device_path",
        "hid_device_path",
        "raw_device_path",
        "device_token",
        "interface_id",
        "device_interface_id",
    }
)


class ConfigPrivacyError(Exception):
    """Raised when code attempts to persist a forbidden identity field."""


class ConfigFormatError(ValueError):
    """Raised when a persisted JSON document is not an object."""


class ConfigTransactionError(RuntimeError):
    """Raised when a paired settings save also fails to roll back."""


def config_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home())
    return Path(base) / APP_ID / PRODUCT_ID


def config_path(root: Path = None) -> Path:  # type: ignore[assignment]
    return (root or config_root()) / CONFIG_FILENAME


def key_bindings_path(root: Path = None) -> Path:  # type: ignore[assignment]
    return (root or config_root()) / KEY_BINDINGS_FILENAME


def default_config() -> Dict[str, Any]:
    from . import key_mapping, voice_hotkey_sync_windows, voice_program_manager

    voice_program = voice_program_manager.normalize_voice_program_settings({})
    provider_hotkeys = voice_hotkey_sync_windows.default_hotkeys_by_provider()
    current_hotkey = provider_hotkeys[str(voice_program["provider"])]["hold"]

    return {
        "schema_version": SCHEMA_VERSION,
        # Existing installations predate multi-device selection and must
        # continue to open as RC003 rather than silently switching behavior.
        "selected_device_profile": "xiaomi-rc003",
        # RC003's upstream decoder applies a 10 dB speech gain before the
        # 16 kHz PCM is sent to the virtual microphone.
        "gain_db": 10.0,
        "retry_delay": 5.0,
        "max_retry_delay": 60.0,
        "voice_shortcut_enabled": True,
        "voice_hotkey": current_hotkey,
        # Kept fixed for backwards compatibility with older builds. RC003's
        # supported product path is now hold-to-talk only.
        "voice_trigger_mode": "hold",
        "voice_hotkeys": {"hold": current_hotkey},
        "voice_hotkeys_by_provider": provider_hotkeys,
        "voice_program": voice_program,
        # Empty until the user explicitly picks one in settings; voice fails
        # closed while this is empty (see audio_output.resolve_selected_endpoint).
        # Both fields together disambiguate endpoints that share a display
        # name across host APIs (e.g. the same device exposed via both
        # Windows WASAPI and MME) - name alone is not always unique.
        "output_endpoint_name": "",
        "output_endpoint_host_api": "",
        # Desktop-shell behavior. Windows login startup itself is owned by
        # the user's HKCU Run value and is deliberately not mirrored here.
        "launch_bridge_on_app_start": False,
        "close_behavior": CLOSE_BEHAVIOR_HIDE_TO_TRAY,
    }


def _find_forbidden_key_paths(
    node: Union[Dict[str, Any], List[Any], Any], path: str = ""
) -> List[str]:
    """Recursively collect dotted-path locations of any forbidden key,
    found at any nesting depth inside dicts and lists-of-dicts.
    """

    found: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.casefold() in FORBIDDEN_KEYS:
                found.append(child_path)
            found.extend(_find_forbidden_key_paths(value, child_path))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_find_forbidden_key_paths(item, f"{path}[{index}]"))
    return found


def _assert_no_forbidden_keys(data: Dict[str, Any]) -> None:
    found = _find_forbidden_key_paths(data)
    if found:
        raise ConfigPrivacyError(
            "refusing to persist forbidden identity field(s) at: "
            + ", ".join(sorted(found))
        )


def load_config(path: Path) -> Dict[str, Any]:
    config = default_config()
    if path.is_file():
        with path.open("r", encoding="utf-8-sig") as handle:
            stored = json.load(handle)
        if not isinstance(stored, dict):
            raise ConfigFormatError("config.json root must be a JSON object")
        _assert_no_forbidden_keys(stored)
        # Normalize the persisted voice fields before merging defaults. A
        # shallow merge would otherwise make a newly introduced default look
        # like an explicitly saved top-level/nested shortcut and could hide
        # the old value that is actually present in the file.
        _normalize_voice_program(stored)
        _normalize_voice_hotkey(stored)
        _normalize_desktop_behavior(stored)
        config.update(stored)
    _normalize_voice_program(config)
    _normalize_voice_hotkey(config)
    _normalize_desktop_behavior(config)
    return config


def save_config(path: Path, config: Dict[str, Any]) -> None:
    persisted = _without_runtime_only_keys(config)
    _assert_no_forbidden_keys(persisted)
    _normalize_voice_hotkey(persisted)
    _normalize_voice_program(persisted)
    _normalize_desktop_behavior(persisted)
    persisted = _without_runtime_only_keys(persisted)
    _save_json_atomic(path, persisted)


def save_config_and_load(path: Path, config_data: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically save one config and restore the previous file if verification fails."""

    previous = _read_file_snapshot(path)
    saved = False
    try:
        save_config(path, config_data)
        saved = True
        return load_config(path)
    except BaseException as save_exc:
        if saved:
            try:
                _restore_file_snapshot(path, previous)
                _verify_file_snapshot(path, previous)
            except Exception as rollback_exc:
                raise ConfigTransactionError(
                    "config save verification failed and rollback was incomplete"
                ) from save_exc
        raise


def _normalize_voice_hotkey(config: Dict[str, Any]) -> None:
    """Normalize legacy voice settings into provider-scoped hold shortcuts."""

    current = str(config.get("voice_hotkey", "")).strip().lower()
    from . import key_mapping, voice_hotkey_sync_windows, voice_program_manager

    provider_settings = voice_program_manager.normalize_voice_program_settings(
        config.get("voice_program")
    )
    provider_id = str(provider_settings["provider"])
    config["voice_program"] = provider_settings

    raw_mode = str(config.get("voice_trigger_mode", "hold")).strip().lower()
    raw_mode_hotkeys = config.get("voice_hotkeys")
    if not isinstance(raw_mode_hotkeys, dict):
        raw_mode_hotkeys = {}
    saved_hold_hotkey = str(raw_mode_hotkeys.get("hold", "")).strip()
    explicit_current_override = bool(
        current and saved_hold_hotkey and current != saved_hold_hotkey
    )

    if raw_mode == key_mapping.VoiceTriggerMode.TOGGLE.value:
        config[RUNTIME_LEGACY_VOICE_MODE_KEY] = raw_mode
        # Never reinterpret a toggle shortcut as hold-to-talk. Use the
        # separately stored hold shortcut when available. Otherwise retain
        # the right-Alt fallback shipped with those old files; the new-install
        # default must not silently rewrite a historical configuration.
        current = saved_hold_hotkey or key_mapping.LEGACY_HOLD_VOICE_HOTKEY
    elif saved_hold_hotkey and not current:
        current = saved_hold_hotkey

    if not current:
        current = key_mapping.voice_hotkey_for_trigger_mode(
            key_mapping.VoiceTriggerMode.HOLD
        )

    raw_provider_hotkeys = config.get("voice_hotkeys_by_provider")
    has_provider_hotkeys = isinstance(raw_provider_hotkeys, dict)
    if not has_provider_hotkeys:
        # These two repairs apply only to the old global field. They must not
        # rewrite WeType's real native Ctrl+Win default after schema 7.
        if current in {"lctrl+win", "lctrl+lwin"}:
            current = key_mapping.LEGACY_HOLD_VOICE_HOTKEY
        if current == "lalt":
            current = key_mapping.LEGACY_HOLD_VOICE_HOTKEY
        raw_provider_hotkeys = {}

    provider_hotkeys = voice_hotkey_sync_windows.default_hotkeys_by_provider()
    for candidate_provider in voice_program_manager.VOICE_PROGRAM_PROVIDER_ORDER:
        raw_entry = raw_provider_hotkeys.get(candidate_provider)
        if not isinstance(raw_entry, dict):
            continue
        candidate = str(raw_entry.get("hold", "")).strip().lower()
        if candidate:
            provider_hotkeys[candidate_provider] = {"hold": candidate}

    if (
        explicit_current_override
        or not has_provider_hotkeys
        or provider_id not in raw_provider_hotkeys
    ):
        provider_hotkeys[provider_id] = {"hold": current}

    current = provider_hotkeys[provider_id]["hold"]

    config["schema_version"] = SCHEMA_VERSION
    config["voice_trigger_mode"] = key_mapping.VoiceTriggerMode.HOLD.value
    config["voice_hotkey"] = current
    config["voice_hotkeys"] = {"hold": current}
    config["voice_hotkeys_by_provider"] = provider_hotkeys
    config.pop("voice_release_finish_tap_enabled", None)


def voice_hotkey_for_provider(
    config_data: Dict[str, Any], provider_id: object
) -> str:
    """Return one provider's normalized hold shortcut without changing selection."""

    from . import voice_hotkey_sync_windows

    provider = str(provider_id).strip().lower()
    entries = config_data.get("voice_hotkeys_by_provider")
    if isinstance(entries, dict):
        entry = entries.get(provider)
        if isinstance(entry, dict):
            candidate = str(entry.get("hold", "")).strip().lower()
            if candidate:
                return candidate
    return voice_hotkey_sync_windows.default_hotkey(provider)


def set_voice_hotkey_for_provider(
    config_data: Dict[str, Any], provider_id: object, shortcut: str
) -> None:
    """Update one provider and keep legacy current-provider mirrors coherent."""

    from . import voice_program_manager

    provider = str(provider_id).strip().lower()
    normalized = str(shortcut).strip().lower()
    entries = config_data.get("voice_hotkeys_by_provider")
    next_entries = dict(entries) if isinstance(entries, dict) else {}
    next_entries[provider] = {"hold": normalized}
    config_data["voice_hotkeys_by_provider"] = next_entries
    current_provider = str(
        voice_program_manager.normalize_voice_program_settings(
            config_data.get("voice_program")
        )["provider"]
    )
    if provider == current_provider:
        config_data["voice_hotkey"] = normalized
        config_data["voice_hotkeys"] = {"hold": normalized}


def _normalize_voice_program(config: Dict[str, Any]) -> None:
    from . import voice_program_manager

    config["voice_program"] = voice_program_manager.normalize_voice_program_settings(
        config.get("voice_program")
    )


def _normalize_desktop_behavior(config: Dict[str, Any]) -> None:
    config["launch_bridge_on_app_start"] = bool(
        config.get("launch_bridge_on_app_start", False)
    )
    close_behavior = str(
        config.get("close_behavior", CLOSE_BEHAVIOR_HIDE_TO_TRAY)
    ).strip()
    if close_behavior not in VALID_CLOSE_BEHAVIORS:
        close_behavior = CLOSE_BEHAVIOR_HIDE_TO_TRAY
    config["close_behavior"] = close_behavior
    config["schema_version"] = SCHEMA_VERSION


def default_key_bindings() -> Dict[str, Any]:
    # Imported lazily to avoid a hard import-order dependency between the two
    # modules at package-load time.
    from . import key_mapping

    return {
        "schema_version": SCHEMA_VERSION,
        "bindings": {
            button_id: action.to_dict()
            for button_id, action in key_mapping.default_button_actions().items()
        },
        # Secondary gestures follow the reference project's separate map.
        # Keeping the primary action flat preserves compatibility with all
        # existing Windows config files.
        "secondary_bindings": {},
        # One optional remote-button combination layer.  The modifier is
        # inert until at least one second-key action is configured.
        "combo_bindings": {
            "modifier": key_mapping.COMBO_MODIFIER_BUTTON_IDS[0],
            "bindings": {},
            "display_notes": {},
        },
        # Optional user-facing labels for each gesture. They never participate
        # in action parsing or execution and are safe to omit in older files.
        "display_notes": {},
        # Physical signatures are learned from Raw Input captures. They are
        # deliberately independent of semantic actions and contain no device
        # path or Bluetooth identity.
        "physical_bindings": {},
    }


def load_key_bindings(path: Path) -> Dict[str, Any]:
    bindings = default_key_bindings()
    if path.is_file():
        with path.open("r", encoding="utf-8-sig") as handle:
            stored = json.load(handle)
        if not isinstance(stored, dict):
            raise ConfigFormatError("key_bindings.json root must be a JSON object")
        _assert_no_forbidden_keys(stored)
        for key, value in stored.items():
            if key in {
                "bindings",
                "secondary_bindings",
                "combo_bindings",
                "physical_bindings",
                "display_notes",
            }:
                if isinstance(value, dict):
                    current = bindings.get(key)
                    if not isinstance(current, dict):
                        current = {}
                        bindings[key] = current
                    current.update(value)
                else:
                    bindings[key] = value
            else:
                bindings[key] = value
    if not isinstance(bindings.get("bindings"), dict):
        bindings["bindings"] = {}
    if not isinstance(bindings.get("secondary_bindings"), dict):
        bindings["secondary_bindings"] = {}
    _normalize_physical_bindings(bindings)
    _normalize_semantic_actions(bindings)
    _normalize_secondary_bindings(bindings)
    _normalize_combo_bindings(bindings)
    _normalize_display_notes(bindings)
    bindings["schema_version"] = SCHEMA_VERSION
    return bindings


def normalize_voice_product_boundary(
    config_data: Dict[str, Any], bindings: Dict[str, Any]
) -> Dict[str, str]:
    """Fail closed for voice mappings the RC003 cannot actually support.

    The returned map is runtime-only and names every button that requires an
    explicit user choice. The caller may display it, log it, or suppress the
    whole button gesture. The original file is not overwritten until save.
    """

    from . import key_mapping

    primary = bindings.get("bindings")
    if not isinstance(primary, dict):
        bindings[RUNTIME_REMOVED_VOICE_BINDINGS_KEY] = {}
        return {}
    legacy_mode = str(
        config_data.get(
            RUNTIME_LEGACY_VOICE_MODE_KEY,
            config_data.get("voice_trigger_mode", "hold"),
        )
    ).strip().lower()
    removed: Dict[str, str] = {}
    for button_id, raw_action in list(primary.items()):
        try:
            action = key_mapping.ButtonAction.from_dict(raw_action)
        except (KeyError, TypeError, ValueError):
            continue
        if action.kind == key_mapping.ActionKind.VOICE:
            if button_id == "mic" and legacy_mode == "hold":
                primary[button_id] = key_mapping.ButtonAction(
                    key_mapping.ActionKind.VOICE_HOLD
                ).to_dict()
            else:
                removed[button_id] = action.kind.value
        elif action.kind == key_mapping.ActionKind.VOICE_TOGGLE:
            removed[button_id] = action.kind.value
        elif (
            action.kind == key_mapping.ActionKind.VOICE_HOLD
            and button_id != "mic"
        ):
            removed[button_id] = action.kind.value

    secondary = bindings.get("secondary_bindings")
    if isinstance(secondary, dict):
        for button_id, trigger_map in secondary.items():
            if not isinstance(trigger_map, dict):
                continue
            for raw_action in trigger_map.values():
                try:
                    action = key_mapping.ButtonAction.from_dict(raw_action)
                except (KeyError, TypeError, ValueError):
                    continue
                if key_mapping.is_voice_action(action):
                    removed.setdefault(button_id, action.kind.value)
    bindings[RUNTIME_REMOVED_VOICE_BINDINGS_KEY] = removed
    return dict(removed)


def _normalize_semantic_actions(bindings: Dict[str, Any]) -> None:
    """Migrate old reference-looking key chords to real action kinds.

    Builds before the semantic action layer wrote values such as
    ``{"kind":"key_combo","keys":["up"]}``.  Keeping those values in
    memory would make the UI look correct while the runtime still takes the
    generic shortcut path.  Convert only the exact built-in chords; arbitrary
    user-recorded combinations remain custom ``key_combo`` actions.
    """

    from . import key_mapping

    def normalize(raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        try:
            action = key_mapping.ButtonAction.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            return raw
        migrated = key_mapping.semantic_action_for_keys(action.keys)
        return migrated.to_dict() if migrated is not None else raw

    primary = bindings.get("bindings")
    if isinstance(primary, dict):
        for button_id, raw in list(primary.items()):
            primary[button_id] = normalize(raw)

    secondary = bindings.get("secondary_bindings")
    if isinstance(secondary, dict):
        for button_id, trigger_map in list(secondary.items()):
            if not isinstance(trigger_map, dict):
                continue
            for trigger_name, raw in list(trigger_map.items()):
                trigger_map[trigger_name] = normalize(raw)

    combo = bindings.get("combo_bindings")
    if isinstance(combo, dict):
        combo_actions = combo.get("bindings")
        if isinstance(combo_actions, dict):
            for button_id, raw in list(combo_actions.items()):
                combo_actions[button_id] = normalize(raw)


def _normalize_secondary_bindings(bindings: Dict[str, Any]) -> None:
    """Keep optional double/long mappings structurally safe on load.

    A malformed secondary entry is ignored by the runtime action lookup, but
    the container itself must still be a mapping so a damaged config cannot
    make the settings page or save path crash. Every physical button,
    including the microphone button, may use ordinary secondary gestures.
    """

    secondary = bindings.get("secondary_bindings")
    if not isinstance(secondary, dict):
        bindings["secondary_bindings"] = {}
        return
    for button_id in list(secondary):
        entry = secondary[button_id]
        if not isinstance(entry, dict):
            secondary.pop(button_id, None)
            continue
        for trigger in list(entry):
            if trigger not in {"double_click", "long_press"} or not isinstance(
                entry[trigger], dict
            ):
                entry.pop(trigger, None)
        if not entry:
            secondary.pop(button_id, None)


def _normalize_combo_bindings(bindings: Dict[str, Any]) -> None:
    """Keep the optional one-modifier combination layer fail-closed."""

    from . import key_mapping

    raw_combo = bindings.get("combo_bindings")
    if not isinstance(raw_combo, dict):
        raw_combo = {}
    modifier = raw_combo.get("modifier")
    if modifier not in key_mapping.COMBO_MODIFIER_BUTTON_IDS:
        modifier = key_mapping.COMBO_MODIFIER_BUTTON_IDS[0]

    normalized_actions: Dict[str, dict] = {}
    raw_actions = raw_combo.get("bindings")
    if isinstance(raw_actions, dict):
        for button_id in key_mapping.COMBO_ACTION_BUTTON_IDS:
            raw_action = raw_actions.get(button_id)
            if not isinstance(raw_action, dict):
                continue
            try:
                action = key_mapping.ButtonAction.from_dict(raw_action)
            except (KeyError, TypeError, ValueError):
                continue
            if (
                action.kind != key_mapping.ActionKind.DISABLED
                and not key_mapping.is_voice_action(action)
            ):
                normalized_actions[button_id] = action.to_dict()

    # A hand-edited file must not let the same physical modifier own both
    # the Fn-style combination layer and delayed double/long gestures. Keep
    # the older single-button gestures and fail the newer combination layer
    # closed until the conflict is resolved in Settings.
    if normalized_actions and key_mapping.has_secondary_action(
        bindings, str(modifier)
    ):
        normalized_actions = {}

    normalized_notes: Dict[str, str] = {}
    raw_notes = raw_combo.get("display_notes")
    if isinstance(raw_notes, dict):
        for button_id in normalized_actions:
            note = raw_notes.get(button_id)
            if isinstance(note, str) and note.strip() and note.strip() != "未命名":
                normalized_notes[button_id] = note.strip()

    bindings["combo_bindings"] = {
        "modifier": str(modifier),
        "bindings": normalized_actions,
        "display_notes": normalized_notes,
    }


def _normalize_physical_bindings(bindings: Dict[str, Any]) -> None:
    """Keep learned physical overrides portable and action-safe."""

    from . import device_profile

    physical = bindings.get("physical_bindings")
    if not isinstance(physical, dict):
        bindings["physical_bindings"] = {}
        return
    bindings["physical_bindings"] = {
        str(signature): str(button_id)
        for signature, button_id in physical.items()
        if isinstance(signature, str)
        and signature.strip()
        and isinstance(button_id, str)
        and button_id in device_profile.ALL_BUTTON_IDS
    }


def _normalize_display_notes(bindings: Dict[str, Any]) -> None:
    """Keep optional display-only labels separate from executable actions."""

    from . import device_profile, key_mapping

    raw_notes = bindings.get("display_notes")
    if not isinstance(raw_notes, dict):
        bindings["display_notes"] = {}
        return
    valid_triggers = {
        key_mapping.ButtonTrigger.SINGLE_CLICK.value,
        key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
        key_mapping.ButtonTrigger.LONG_PRESS.value,
    }
    normalized: Dict[str, Dict[str, str]] = {}
    for button_id, trigger_map in raw_notes.items():
        if button_id not in device_profile.ALL_BUTTON_IDS or not isinstance(
            trigger_map, dict
        ):
            continue
        clean_trigger_map = {
            str(trigger): str(note).strip()
            for trigger, note in trigger_map.items()
            if trigger in valid_triggers
            and isinstance(note, str)
            and note.strip()
            and note.strip() != "未命名"
        }
        if clean_trigger_map:
            normalized[str(button_id)] = clean_trigger_map
    bindings["display_notes"] = normalized


def save_key_bindings(path: Path, bindings: Dict[str, Any]) -> None:
    persisted = _without_runtime_only_keys(bindings)
    persisted["schema_version"] = SCHEMA_VERSION
    _assert_no_forbidden_keys(persisted)
    _save_json_atomic(path, persisted)


def save_settings_pair(
    config_file: Path,
    config_data: Dict[str, Any],
    bindings_file: Path,
    bindings_data: Dict[str, Any],
) -> None:
    """Save the two user-facing settings documents as one recoverable action.

    NTFS does not provide a multi-file atomic replace. Validate both
    documents before touching disk, then restore the first file byte-for-byte
    if the second atomic write fails. This prevents ordinary disk-lock,
    permission, serialization, and replace failures from leaving a mixed
    configuration behind.
    """

    if Path(config_file).absolute() == Path(bindings_file).absolute():
        raise ValueError("paired settings paths must be distinct")
    _assert_no_forbidden_keys(config_data)
    _assert_no_forbidden_keys(bindings_data)

    previous_config = _read_file_snapshot(config_file)
    config_saved = False
    try:
        save_config(config_file, config_data)
        config_saved = True
        save_key_bindings(bindings_file, bindings_data)
    except BaseException as save_exc:
        if config_saved:
            try:
                _restore_file_snapshot(config_file, previous_config)
            except Exception as rollback_exc:
                raise ConfigTransactionError(
                    "paired settings save failed and config rollback was incomplete"
                ) from save_exc
        raise


def _save_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """Write one settings file without exposing a half-written JSON file."""

    content = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _save_bytes_atomic(path, content)


def _without_runtime_only_keys(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if key not in _RUNTIME_ONLY_KEYS
    }


def _save_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _restore_file_snapshot(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    _save_bytes_atomic(path, content)


def _read_file_snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _verify_file_snapshot(path: Path, content: bytes | None) -> None:
    if content is None:
        try:
            path.stat()
        except FileNotFoundError:
            return
        raise OSError("restored file should not exist")
    if not path.is_file() or path.read_bytes() != content:
        raise OSError("restored file does not match its previous contents")
