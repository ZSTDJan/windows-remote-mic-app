"""Pure per-entity settings projection, shared by config and mapping persistence.

Disk records are authoritative; flat fields are a runtime view for existing
consumers. Only config.remote_selection owns the active entity. No device I/O.
"""
from __future__ import annotations

from copy import deepcopy

from . import remote_selection

STORE = "remote_settings"
OWNER = "_remote_settings_owner"
CONFIG_FIELDS = frozenset({
    "gain_db", "voice_shortcut_enabled", "voice_hotkey", "voice_trigger_mode",
    "voice_hotkeys", "voice_hotkeys_by_provider", "voice_program",
    "output_endpoint_name", "output_endpoint_host_api", "remote_recording_mode",
    "remote_recording_limit_seconds",
})
BINDING_FIELDS = frozenset({
    "bindings", "secondary_bindings", "combo_bindings", "display_notes", "physical_bindings",
})


class RemoteSettingsError(ValueError):
    """Invalid records or stale owner: refuse a cross-device save."""


def validate(store, fields):
    if (not isinstance(store, dict) or set(store) != {"schema", "records", "unassigned"}
            or type(store["schema"]) is not int or store["schema"] != 1
            or not isinstance(store["records"], dict) or len(store["records"]) > 128):
        raise RemoteSettingsError("逐设备设置格式不受支持。")
    for key, row in store["records"].items():
        if not remote_selection.valid_key(key) or not isinstance(row, dict) or not set(row) <= fields:
            raise RemoteSettingsError("逐设备设置的身份或字段不正确。")
    if not isinstance(store["unassigned"], dict) or not set(store["unassigned"]) <= fields:
        raise RemoteSettingsError("未绑定的旧设置格式不正确。")
    return deepcopy(store)


def capture(document, fields):
    return {key: deepcopy(document[key]) for key in fields if key in document}


def project(document, fields, active):
    """Return the selected view; never fall back to another entity's record."""
    result = deepcopy(document)
    if STORE not in result:
        return result
    store = validate(result[STORE], fields)
    for key in fields:
        result.pop(key, None)
    result.update(deepcopy(store["records"].get(active, {}) if active else store["unassigned"]))
    result[OWNER] = active
    return result


def migrate(document, fields, previous):
    if STORE in document:
        return validate(document[STORE], fields)
    values = capture(document, fields)
    return {"schema": 1, "records": {previous: values} if previous else {},
            "unassigned": {} if previous else values}


def pack(document, fields, active, latest=None):
    """Persist one view, retaining other records and rejecting stale saves."""
    result = deepcopy(document)
    if STORE not in result:
        if latest and STORE in latest:
            raise RemoteSettingsError("设备设置已变化，请重新载入后再保存。")
        result.pop(OWNER, None)
        return result
    if result.get(OWNER) != active:
        raise RemoteSettingsError("当前设备已切换，旧设置不能写入新设备。")
    store = validate((latest or {}).get(STORE, result[STORE]), fields)
    if active:
        store["records"][active] = capture(result, fields)
    else:
        store["unassigned"] = capture(result, fields)
    for key in fields | {OWNER}:
        result.pop(key, None)
    result[STORE] = store
    return result


def switch_views(settings, bindings, selection, *, allow_legacy_binding=True):
    """Build a two-document transaction without assigning unbound legacy data."""
    selection = remote_selection.normalize(selection)
    previous = remote_selection.active_key(settings)
    active = selection["active"]
    next_settings, next_bindings = deepcopy(settings), deepcopy(bindings)
    for document, fields in ((next_settings, CONFIG_FIELDS), (next_bindings, BINDING_FIELDS)):
        store = migrate(document, fields, previous)
        if previous:
            store["records"][previous] = capture(document, fields)
        # Pre-selection versions only supported Xiaomi. The user's explicit
        # choice of the sole registered Xiaomi establishes that legacy owner;
        # never apply it to Chromecast or overwrite an existing entity record.
        if (allow_legacy_binding and not previous and active and active not in store["records"]
                and remote_selection.profile_for_key(selection, active) == remote_selection.RC003_PROFILE
                and sum(row["profile"] == remote_selection.RC003_PROFILE for row in selection["devices"]) == 1):
            store["records"][active] = deepcopy(store["unassigned"])
            store["unassigned"] = {}
        document[STORE] = store
        document[OWNER] = active
        for key in fields:
            document.pop(key, None)
        document.update(deepcopy(store["records"].get(active, {}) if active else store["unassigned"]))
    next_settings[remote_selection.KEY] = selection
    return next_settings, next_bindings
