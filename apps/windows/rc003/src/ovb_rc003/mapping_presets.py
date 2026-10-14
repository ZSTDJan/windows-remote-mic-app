"""Named button snapshots, independent of the current per-device settings.

Only this module owns mapping_presets.json (schema 2, reads schema 1) under config_root.
It is user data, never a build asset. Removing a saved snapshot never writes
config.json or key_bindings.json. Portable exports contain no device identity.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import UUID, uuid4

from . import config, device_catalog, key_mapping, remote_layout, settings_ui, win32_keys

FILENAME = "mapping_presets.json"
FORMAT = "RemoteMic.key-mapping"
MAX_BYTES = 2 * 1024 * 1024
MAX_PRESETS = 100
MAPPING_FIELDS = ("bindings", "secondary_bindings", "display_notes")
PROFILES = (device_catalog.RC003_ID, device_catalog.CHROMECAST_ID)
AUTO_ID = "automatic"
AUTO_NAME = "26-1012 自动保存"


def validate_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ValueError("名称须为 1～64 个字符。")
    if any(ord(char) < 32 for char in value):
        raise ValueError("名称不能包含换行或控制字符。")
    return value.strip()


def validate_mapping(value: object, profile: str) -> dict:
    if profile not in PROFILES:
        raise ValueError("此配置的遥控器型号不受支持。")
    if not isinstance(value, dict) or set(value) != set(MAPPING_FIELDS):
        raise ValueError("按键配置内容不完整或含有不支持的字段。")
    allowed = set(remote_layout.button_order(profile))
    result = copy.deepcopy(value)
    for field in MAPPING_FIELDS:
        if not isinstance(result[field], dict) or set(result[field]) - allowed:
            raise ValueError("配置包含不属于此型号的按键。")
    if set(result["bindings"]) != allowed:
        raise ValueError("配置必须包含此型号的全部按键。")
    for button, raw in result["bindings"].items():
        _validate_action(raw, button, "single_click", profile)
    for button, actions in result["secondary_bindings"].items():
        if not isinstance(actions, dict) or set(actions) - {"double_click", "long_press"}:
            raise ValueError("双击或长按配置无效。")
        primary = key_mapping.ButtonAction.from_dict(result["bindings"][button])
        for trigger, raw in actions.items():
            action = _validate_action(raw, button, trigger, profile)
            if key_mapping.is_voice_action(primary) and action.kind != key_mapping.ActionKind.DISABLED:
                raise ValueError("语音键不能同时配置双击或长按。")
    for notes in result["display_notes"].values():
        if not isinstance(notes, dict) or set(notes) - {trigger.value for trigger in key_mapping.ButtonTrigger}:
            raise ValueError("按键备注格式无效。")
        if any(not isinstance(note, str) or len(note) > 256 for note in notes.values()):
            raise ValueError("每条备注最多 256 个字符。")
    return result


def _validate_action(raw: object, button: str, trigger: str, profile: str) -> key_mapping.ButtonAction:
    if not isinstance(raw, dict) or set(raw) - {"kind", "keys", "uri"}:
        raise ValueError("按键动作格式无效。")
    action = key_mapping.ButtonAction.from_dict(raw)
    if action.kind in {key_mapping.ActionKind.VOICE, key_mapping.ActionKind.VOICE_TOGGLE}:
        raise ValueError("配置含已停用的语音动作，请重新选择后保存。")
    if action.kind == key_mapping.ActionKind.KEY_COMBO:
        win32_keys.resolve_vk_codes(action.keys)
    try:
        settings_ui._validated_button_action(button, trigger, settings_ui._action_to_display(action))
    except settings_ui.SettingsValidationError as exc:
        raise ValueError(exc.message) from exc
    if profile == device_catalog.CHROMECAST_ID and button == "mic":
        if trigger == "single_click" and action.kind != key_mapping.ActionKind.VOICE_HOLD:
            raise ValueError("谷歌话筒键须保留语音动作。")
        if trigger != "single_click" and action.kind != key_mapping.ActionKind.DISABLED:
            raise ValueError("谷歌话筒键不支持双击或长按映射。")
    return action


def snapshot(bindings: dict, profile: str) -> dict:
    allowed = remote_layout.button_order(profile)
    value = {field: {button: copy.deepcopy(data) for button, data in bindings.get(field, {}).items()
                     if button in allowed} for field in MAPPING_FIELDS}
    # Absent primary actions have always meant disabled at runtime.
    value["bindings"] = {button: value["bindings"].get(button, {"kind": "disabled", "keys": []})
                         for button in allowed}
    return validate_mapping(value, profile)


def apply_to(bindings: dict, preset: dict, profile: str) -> dict:
    if preset["profile"] != profile:
        raise ValueError("只能应用同一遥控器型号的配置。")
    result = copy.deepcopy(bindings)
    result.update(validate_mapping(preset["mapping"], profile))
    return result


def _comparable_mapping(mapping: dict, profile: str) -> dict:
    """Match the loader's normalization without rewriting the saved snapshot."""
    result = copy.deepcopy(mapping)
    config._normalize_semantic_actions(result)
    config._normalize_display_notes(result, profile)
    result["bindings"] = {button: key_mapping.ButtonAction.from_dict(raw).to_dict()
                          for button, raw in result["bindings"].items()}
    secondary = {}
    for button, actions in result["secondary_bindings"].items():
        enabled = {}
        for trigger, raw in actions.items():
            action = key_mapping.ButtonAction.from_dict(raw)
            if action.kind != key_mapping.ActionKind.DISABLED:
                enabled[trigger] = action.to_dict()
        if enabled:
            secondary[button] = enabled
    result["secondary_bindings"] = secondary
    return result


def _read(path: Path) -> dict:
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("配置文件过大（最多 2 MB）。")
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("不是有效的按键配置文件。") from exc
    if not isinstance(value, dict):
        raise ValueError("按键配置文件格式无效。")
    return value


def _validate_document(value: dict) -> dict:
    if (set(value) != {"format", "schema_version", "name", "profile", "mapping"}
            or value.get("format") != FORMAT or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1):
        raise ValueError("请选择本软件导出的按键配置文件（版本 1）。")
    return {**value, "name": validate_name(value["name"]),
            "mapping": validate_mapping(value["mapping"], value["profile"])}


class PresetStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / FILENAME

    def _load_document(self) -> dict:
        try:
            value = _read(self.path)
        except FileNotFoundError:
            return {"schema_version": 2, "presets": [], "applied_ids": {}}
        version = value.get("schema_version")
        expected = {"schema_version", "presets"}
        if version == 2:
            expected.add("applied_ids")
        if (set(value) != expected or type(version) is not int
                or version not in (1, 2) or not isinstance(value["presets"], list)
                or len(value["presets"]) > MAX_PRESETS):
            raise ValueError("保存的配置列表损坏或版本不受支持；原文件已保留。")
        result = []
        ids, names = set(), set()
        for entry in value["presets"]:
            if not isinstance(entry, dict) or set(entry) != {"id", "name", "profile", "mapping"}:
                raise ValueError("保存的配置格式无效；原文件已保留。")
            identity = str(UUID(entry["id"]))
            document = _validate_document({"format": FORMAT, "schema_version": 1,
                                          **{key: entry[key] for key in ("name", "profile", "mapping")}})
            name_key = (entry["profile"], document["name"].casefold())
            if identity in ids or name_key in names:
                raise ValueError("保存的配置含重复名称或标识；原文件已保留。")
            ids.add(identity)
            names.add(name_key)
            result.append({"id": identity, **{key: document[key] for key in ("name", "profile", "mapping")}})
        applied_ids = value.get("applied_ids", {})
        if not isinstance(applied_ids, dict) or set(applied_ids) - set(PROFILES):
            raise ValueError("配置列表状态无效；原文件已保留。")
        for profile, identity in applied_ids.items():
            if identity != AUTO_ID and not any(
                    entry["id"] == identity and entry["profile"] == profile for entry in result):
                raise ValueError("配置列表的应用标记无效；原文件已保留。")
        return {"schema_version": 2, "presets": result, "applied_ids": applied_ids}

    def load(self) -> list[dict]:
        return self._load_document()["presets"]

    def view(self, bindings: dict, profile: str, *, applied_override: str | None = None) -> list[dict]:
        """Read list metadata even when the active mapping needs correction.

        The automatic row never owns a second saved mapping. An unavailable
        snapshot is display-only; copying/exporting must validate it separately.
        """
        if profile not in PROFILES:
            raise ValueError("此配置的遥控器型号不受支持。")
        document = self._load_document()
        try:
            current = snapshot(bindings, profile)
        except (KeyError, TypeError, ValueError):
            current = None
        entries = [entry for entry in document["presets"] if entry["profile"] == profile]
        active = applied_override or document["applied_ids"].get(profile, AUTO_ID)
        if current is None or not any(entry["id"] == active and _comparable_mapping(entry["mapping"], profile)
                   == _comparable_mapping(current, profile) for entry in entries):
            active = AUTO_ID
        rows = [{"id": AUTO_ID, "name": AUTO_NAME, "profile": profile, "mapping": current}, *entries]
        return [{**entry, "automatic": entry["id"] == AUTO_ID, "applied": entry["id"] == active}
                for entry in rows]

    def mark_applied(self, profile: str, identity: str) -> None:
        if profile not in PROFILES:
            raise ValueError("此配置的遥控器型号不受支持。")
        document = self._load_document()
        if identity != AUTO_ID and self._find(document["presets"], identity)["profile"] != profile:
            raise ValueError("所选配置与当前遥控器型号不符。")
        document["applied_ids"][profile] = identity
        self._save(document)

    def _save(self, document: dict) -> None:
        if len(document["presets"]) > MAX_PRESETS:
            raise ValueError("最多保存 100 套配置，请先导出或删除不用的配置。")
        if len((json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")) > MAX_BYTES:
            raise ValueError("保存的配置总量超过 2 MB，请先导出或删除不用的配置。")
        config._save_json_atomic(self.path, document)

    @staticmethod
    def _find(entries: list[dict], identity: str) -> dict:
        for entry in entries:
            if entry["id"] == identity:
                return entry
        raise ValueError("所选配置已不存在，请重新选择。")

    def get(self, identity: str) -> dict:
        return self._find(self.load(), identity)

    def create(self, name: str, profile: str, mapping: dict, *, unique: bool = False) -> str:
        document = self._load_document()
        entries = document["presets"]
        name = validate_name(name)
        names = {entry["name"].casefold() for entry in entries if entry["profile"] == profile}
        names.add(AUTO_NAME.casefold())
        if unique:
            base, index = name, 2
            while name.casefold() in names:
                suffix = f" ({index})"
                name = base[:64 - len(suffix)] + suffix
                index += 1
        elif name.casefold() in names:
            raise ValueError("已有同名配置，请换一个名称。")
        identity = str(uuid4())
        entries.append({"id": identity, "name": name, "profile": profile,
                        "mapping": validate_mapping(mapping, profile)})
        self._save(document)
        return identity

    def rename(self, identity: str, name: str) -> None:
        document = self._load_document()
        entries = document["presets"]
        entry = self._find(entries, identity)
        name = validate_name(name)
        if name.casefold() == AUTO_NAME.casefold() or any(
               other["id"] != identity and other["profile"] == entry["profile"]
               and other["name"].casefold() == name.casefold() for other in entries):
            raise ValueError("已有同名配置，请换一个名称。")
        entry["name"] = name
        self._save(document)

    def delete(self, identity: str) -> None:
        document = self._load_document()
        entries = document["presets"]
        self._find(entries, identity)
        document["presets"] = [entry for entry in entries if entry["id"] != identity]
        document["applied_ids"] = {profile: AUTO_ID if active == identity else active
                                   for profile, active in document["applied_ids"].items()}
        self._save(document)

    def import_file(self, path: Path, profile: str) -> str:
        document = _validate_document(_read(Path(path)))
        if document["profile"] != profile:
            raise ValueError("配置的遥控器型号与当前型号不同，请先切换设备。")
        return self.create(document["name"], profile, document["mapping"], unique=True)

    def export_file(self, identity: str, path: Path) -> None:
        self.export_entry(self.get(identity), path)

    def export_entry(self, entry: dict, path: Path) -> None:
        destination = Path(path).resolve()
        if destination.is_relative_to(self.root.resolve()):
            raise ValueError("请导出到桌面等位置，不能覆盖软件的数据目录。")
        document = _validate_document({"format": FORMAT, "schema_version": 1,
                    **{key: entry[key] for key in ("name", "profile", "mapping")}})
        config._save_json_atomic(destination, document)
