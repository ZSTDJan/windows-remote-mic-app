"""Bounded metadata journal for one Google receiver; never records audio.

Owned by the worker, shipped as ordinary application source. Observations do
not replay packets or change device/recording state. Unproven sources contribute
anonymous counts/structure only, never payloads or a claimed device identity.
"""
from collections import deque
import time

from .chromecast_buttons import STOP_REASONS

COUNTERS = frozenset({"rx_acl", "tx_acl", "other_connection", "preproof_notify",
    "controls", "audio_packets", "audio_bytes", "ordinary", "other_notify",
    "no_handler", "not_available", "accepted_audio", "ignored_audio",
    "control_rows_lost", "send_failed", "hid_edges", "metadata_rejected"})
STATES = frozenset({"idle", "starting", "recording", "stopping", "blocked", "unavailable"})
RESULTS = frozenset({"unknown_control", "invalid_control", "unsupported_voice_stream",
    "accepted_start", "duplicate_start", "awaiting_start", "ignored_mic", "second_press",
    "invalid_stop", "accepted_stop", "device_open_failed", "accepted_sync",
    "detect_ignored", "detect_pressed", "detect_release", "not_available", "no_handler",
    "accepted_caps", "unsupported_caps", "unexpected_caps"})
STAGES = frozenset({"run", "detect", "setup", "source_ready", "source_probe_scheduled",
    "hid_primary_start", "hid_primary_ready",
    "hid_fallback_start", "hid_fallback_ready", "voice_open_begin", "voice_wake_retry",
    "voice_ready", "voice_unavailable", "stop", "voice_caps_request", "voice_caps_ready",
    "voice_caps_write_failed", "voice_caps_timeout", "voice_caps_unsupported", "voice_caps_overflow"})
MAX_COUNT = 2**31 - 1
GATT_NUMBERS = frozenset({"status", "tx", "audio", "control", "tx_handle", "audio_handle",
                          "control_handle", "service_count", "characteristic_count", "mtu"})
CAPTURE_COUNTS = frozenset({"received", "other_provider", "other_event", "other_kind",
    "hci", "rx", "tx", "queued", "queue_overflow", "parse_failed"})
HID_COUNTS = frozenset({"callbacks", "address_match", "address_mismatch", "buffer_null",
    "length_error", "length_rejected", "access_error", "data_error", "tail_rejected",
    "code_rejected", "accepted", "callback_error", "lengths_overflow",
    "copy_hook_error", "copy_calls", "copy_success", "copy_pending", "copy_failed",
    "copy_capacity_three", "copy_read_three", "copy_report_two", "copy_release",
    "copy_up", "copy_down", "copy_left", "copy_right", "copy_ok",
    "copy_known_other", "copy_other_usage", "copy_read_error", "copy_lengths_overflow"})
HID_STEPS = frozenset({"none", "address", "length", "access", "data", "release", "send"})
HID_STARTUP_STEPS = frozenset({"import_frida", "selected_host", "driver_layout", "script_prepare",
    "attach", "gadget_fallback", "create_script", "load_script", "verify_source", "script_message",
    "startup_script_unload", "startup_session_detach", "cleanup_script_unload", "cleanup_session_detach",
    "cleanup_gadget_device_remove",
    "host_scan", "driver_read", "pdb_identity",
    "cache_lookup", "cache_directory", "pdb_download", "download_validate", "cache_commit", "cache_cleanup",
    "dbghelp_load", "symbol_initialize", "symbol_load", "symbol_enumerate", "symbol_match", "symbol_cleanup",
    "driver_pe", "driver_arch", "callback_range", "constructor_range", "constructor_layout",
    "runtime_module", "runtime_hook", "wait_device", "wait_start", "wait_ready", "worker_failure", "evidence_overflow",
    "attach_controller", "attach_process_before", "attach_process_after", "attach_token", "attach_exception",
    "attach_assets_before", "attach_assets_after", "attach_asset_access", "attach_evidence", "session_detached",
    "attach_policy", "attach_modules_before", "attach_modules_after", "attach_asset_file",
    "attach_helper_scan", "attach_helper_manager", "attach_helper_service"})
HID_STARTUP_REASONS = frozenset({"none", "invalid_entity", "registry_read", "no_selected_host",
    "ambiguous_host", "debug_entries", "debug_signature", "pdb_name", "cache_root", "download_exit",
    "download_size", "download_signature", "symbol_initialize_failed", "symbol_load_failed",
    "symbol_enumerate_failed", "symbol_missing_or_ambiguous", "unsupported_architecture",
    "nonexecutable_rva", "constructor_unrecognized", "module_missing", "module_path_mismatch",
    "hook_exception", "device_open_timeout", "startup_timeout", "ready_timeout", "records_dropped", "records_rejected",
    "gadget_config_invalid", "gadget_runtime_missing", "gadget_runtime_acl_invalid",
    "gadget_runtime_hash_invalid", "gadget_port_unavailable", "gadget_elevation_required",
    "gadget_archive_hash_invalid", "gadget_host_unreadable", "gadget_host_changed",
    "gadget_host_invalid", "gadget_modules_unreadable", "gadget_shared_host",
    "gadget_runtime_conflict", "gadget_connect_failed", "gadget_inject_failed",
    "gadget_listener_unreadable", "gadget_listener_conflict", "gadget_identity_mismatch",
    "gadget_inject_uncertain", "gadget_lock_unavailable", "gadget_cancelled",
    "gadget_cleanup_failed", "gadget_runtime_prepare_failed",
    "gadget_host_membership_unreadable", "gadget_rc003_host_conflict"})
HID_DETAIL_NUMBERS = frozenset({"registry_nodes", "hardware_matches", "selected_matches", "invalid_address",
    "missing_container", "host_candidates", "invalid_pid", "size", "cache_size", "cache_present", "cache_valid",
    "return_code", "http_status", "callback_matches", "constructor_matches", "callback_rva", "constructor_rva",
    "address_offset", "machine", "known_layout", "debug_entries", "executable_sections", "dropped",
    "module_present", "module_path_match", "pointer_size", "loaded_size", "native_result", "native_code",
    "query_ms", "query_complete", "process_handle", "alive", "exit_code", "created_low", "created_high",
    "system_wudfhost", "image_error", "elevated", "integrity_rid", "session_id", "restricted", "app_container",
    "elevated_error", "session_id_error", "app_container_error", "scan_errors", "scan_limited", "candidates",
    "changed_candidates", "omitted_candidates", "fresh_candidate", "token_available", "read_execute", "granted_access",
    "error_chars", "error_clipped", "chain_depth", "reported_code", "crash_present", "parent_depth", "requested_access",
    "dynamic_code", "extension_points", "signature_policy", "dynamic_code_error", "extension_points_error",
    "signature_policy_error", "process_changed", "module_limit", "module_name_errors", "loaded_driver", "loaded_agent",
    "pe_valid", "hash_complete", "file_unchanged", "sample_count", "helper_count", "helper_pid"})

HID_DETAIL_CHOICES = {
    'sample_phase': {'before', 'after'},
    'sample_outcome': {'finished', 'timeout', 'busy', 'failed'},
    'query_outcome': {'captured', 'partial', 'unavailable', 'failed'},
    'error_family': {'unclassified', 'agent_load_or_process_exit', 'agent_transport', 'connection_closed', 'access_denied', 'timeout', 'process_missing'},
    'native_api': {'OpenProcess', 'CreateRemoteThread', 'VirtualAllocEx', 'WriteProcessMemory', 'LoadLibraryW'},
    'asset_scope': {'candidates_not_proven_loaded_path', 'candidate_file_dacl_only', 'candidate_parent_dacl_only'},
    'asset_location': {'temporary', 'elevated_helper'},
    'detach_reason': {'application-requested', 'process-replaced', 'process-terminated', 'connection-terminated', 'device-lost', 'unknown'},
}


def _valid_hid_details(info):
    if not isinstance(info, dict) or len(info) > 12:
        return False
    for key, value in info.items():
        if key in HID_DETAIL_NUMBERS:
            if not _integer(value, -1, 0xffffffff):
                return False
        elif key in ("driver_sha256", "pdb_sha256", "error_sha256", "asset_ref", "agent_sha256"):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                return False
        elif key in HID_DETAIL_CHOICES:
            if not isinstance(value, str) or value not in HID_DETAIL_CHOICES[key]:
                return False
        elif key == 'frida_version':
            import re
            if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', value):
                return False
        elif key == "constructor_prefix":
            if not isinstance(value, str) or len(value) > 128 or len(value) % 2 or any(c not in "0123456789abcdef" for c in value):
                return False
        elif key == "pdb_key":
            # Public build identifier, never a device GUID or local path.
            if not isinstance(value, str) or not 33 <= len(value) <= 40 or any(c not in "0123456789ABCDEF" for c in value):
                return False
        elif key == "worker_stage":
            if not isinstance(value, str) or value not in ("start", "selection", "device_open", "capture", "receive"):
                return False
        else:
            return False
    return True


HID_ERROR_TYPES = frozenset({"none", "other", "TapError", "GadgetAttachError", "PipeError", "PermissionError", "FileNotFoundError", "UnicodeDecodeError",
    "OSError", "TimeoutError", "ImportError", "ModuleNotFoundError", "AttributeError", "ValueError",
    "RuntimeError", "TypeError", "PEFormatError", "PermissionDeniedError", "ProcessNotFoundError",
    "ProcessNotRespondingError", "InvalidOperationError", "InvalidArgumentError", "NotSupportedError",
    "TransportError", "ProtocolError", "ServerNotRunningError", "ExecutableNotFoundError", "TimeoutExpired",
    "ExecutableNotSupportedError", "AddressInUseError", "OperationCancelledError", "TimedOutError"})


def _integer(value, low=0, high=MAX_COUNT):
    return type(value) is int and low <= value <= high


def valid_record(row):
    if not isinstance(row, dict):
        return False
    kind = row.get("kind")
    if kind == "hid_startup":
        return (set(row) == {"kind", "elapsed_ms", "step", "state", "duration_ms", "error_type",
                            "cause_type", "native_code", "cause_code", "host_pid", "script_line", "reason", "details"}
                and _integer(row["elapsed_ms"]) and _integer(row["duration_ms"])
                and all(isinstance(row[k], str) for k in ("step", "state", "error_type", "cause_type"))
                and row["step"] in HID_STARTUP_STEPS and row["state"] in ("begin", "success", "failed")
                and row["error_type"] in HID_ERROR_TYPES and row["cause_type"] in HID_ERROR_TYPES
                and _integer(row["native_code"], -1, 0xffffffff)
                and _integer(row["cause_code"], -1, 0xffffffff)
                and _integer(row["host_pid"], -1) and _integer(row["script_line"], -1, 65535)
                and isinstance(row["reason"], str) and row["reason"] in HID_STARTUP_REASONS
                and _valid_hid_details(row["details"]))
    if kind == "voice_link":
        return (set(row) == {"kind", "elapsed_ms", "attempt", "step", "outcome", "duration_ms", "hresult",
                            "connected", "device_access", "service_access", "sharing", "service_handle",
                            "session_status", "maintain", "can_maintain"}
                and _integer(row["elapsed_ms"]) and _integer(row["attempt"], 1, 2)
                and row["step"] in ("session_create", "session_active", "characteristics", "ready", "failed", "closed")
                and row["outcome"] in ("success", "status_failed", "timeout", "cancelled", "os_error", "exception")
                and _integer(row["duration_ms"]) and _integer(row["hresult"], -1, 0xffffffff)
                and all(_integer(row[k], -1, 1) for k in
                        ("connected", "sharing", "session_status", "maintain", "can_maintain"))
                and all(_integer(row[k], -1, 3) for k in ("device_access", "service_access"))
                and _integer(row["service_handle"], -1, 65535))
    if kind == "voice_query":
        items = row.get("items")
        return (set(row) == {"kind", "elapsed_ms", "attempt", "step", "cache", "duration_ms",
                            "connected_before", "connected_after", "outcome", "status", "protocol_error", "hresult",
                            "item_count", "item_offset", "items"}
                and _integer(row["elapsed_ms"]) and _integer(row["attempt"], 1, 2)
                and row["step"] in ("services", "characteristics", "audio_subscription", "control_subscription")
                and row["cache"] in ("cached", "live") and _integer(row["duration_ms"])
                and all(_integer(row[k], -1, 1) for k in ("connected_before", "connected_after"))
                and row["outcome"] in ("success", "status_failed", "timeout", "cancelled", "os_error", "exception")
                and _integer(row["status"], -1, 65535) and _integer(row["hresult"], -1, 0xffffffff)
                and _integer(row["protocol_error"], -1, 255)
                and _integer(row["item_count"], 0, 65535) and isinstance(items, list)
                and _integer(row["item_offset"], 0, 24) and row["item_offset"] % 8 == 0
                and len(items) <= 8 and row["item_offset"] + len(items) <= min(32, row["item_count"])
                and all(isinstance(item, dict) and set(item) == {"uuid", "handle", "properties"}
                        and isinstance(item["uuid"], str) and len(item["uuid"]) == 36
                        and all(c == "-" if i in (8, 13, 18, 23) else c in "0123456789abcdef"
                                for i, c in enumerate(item["uuid"]))
                        and _integer(item["handle"], 0, 65535)
                        and _integer(item["properties"], -1, 65535) for item in items))
    if kind == "hid_flow":
        counts, lengths, copy_lengths = row.get("counts"), row.get("lengths"), row.get("copy_buffer_lengths")
        return (set(row) == {"kind", "elapsed_ms", "sample_ms", "counts", "lengths",
                             "copy_buffer_lengths", "copy_hook_state", "copy_scope", "last_error_step"}
                and _integer(row["elapsed_ms"]) and _integer(row["sample_ms"])
                and isinstance(counts, dict) and set(counts) == HID_COUNTS
                and all(_integer(v) for v in counts.values())
                and all(isinstance(items, list) and len(items) <= 16
                        and all(isinstance(pair, list) and len(pair) == 2
                                and _integer(pair[0], 0, 0xffffffff) and _integer(pair[1], 1)
                                for pair in items)
                        and len({pair[0] for pair in items}) == len(items)
                        for items in (lengths, copy_lengths))
                and isinstance(row["copy_hook_state"], str)
                and row["copy_hook_state"] in {"ready", "unavailable"}
                and row["copy_scope"] == "selected_host_unattributed"
                and isinstance(row["last_error_step"], str) and row["last_error_step"] in HID_STEPS)
    if kind == "source_probe":
        return (set(row) == {"kind", "elapsed_ms", "state", "duration_ms", "status", "services", "hresult"}
                and row["state"] in {"begin", "completed", "failed", "cancelled"}
                and all(_integer(row[k]) for k in ("elapsed_ms", "duration_ms"))
                and _integer(row["status"], -1, 65535) and _integer(row["services"], -1, 65535)
                and _integer(row["hresult"], -1, 0xffffffff))
    if kind == "source_state":
        return (set(row) == {"kind", "elapsed_ms", "phase", "reason", "api_ok", "request_seen",
                            "response_seen", "ready", "input_attribute", "proof_handle", "packet_relation"}
                and _integer(row["elapsed_ms"]) and row["phase"] in {"ready", "stop"}
                and row["reason"] in STOP_REASONS | {"ready"}
                and all(type(row[k]) is bool for k in ("api_ok", "request_seen", "response_seen", "ready"))
                and _integer(row["input_attribute"], -1, 65535)
                and _integer(row["proof_handle"], -1, 4095)
                and row["packet_relation"] in {"unknown", "same_handle", "other_handle"})
    if kind == "capture_flow":
        counts = row.get("counts")
        return (set(row) == {"kind", "counts", "first_other_event", "first_other_version", "first_other_kind",
                            "schema_step", "schema_property", "schema_code"}
                and isinstance(counts, dict) and set(counts) == CAPTURE_COUNTS
                and all(_integer(v) for v in counts.values())
                and _integer(row["first_other_event"], -1, 65535)
                and _integer(row["first_other_version"], -1, 255)
                and _integer(row["first_other_kind"], -1, 255)
                and row["schema_step"] in {"none", "size", "read", "limit", "length"}
                and row["schema_property"] in {"none", "BIP_Type", "BIP_Data", "BIP_DataLen"}
                and _integer(row["schema_code"], -1, 0xffffffff))
    if kind == "capture":
        return (set(row) == {"kind", "operation", "result", "attempt", "owner_ref"}
                and row["operation"] in {"owner_busy", "start", "restart", "recover_query", "recover_stop", "stop", "close_consumer"}
                and _integer(row["result"], 0, 0xffffffff) and _integer(row["attempt"], 1, 2)
                and isinstance(row["owner_ref"], str) and len(row["owner_ref"]) == 32
                and all(c in "0123456789abcdef" for c in row["owner_ref"]))
    if kind == "control":
        fields = row.get("fields")
        opcode = row.get("opcode")
        maximum = {0: 1, 4: 3, 8: 0, 11: 8, 12: 2}.get(opcode, 0)
        return (set(row) == {"kind", "n", "source_ms", "received_ms", "length", "opcode", "fields", "state", "result"}
                and _integer(row["n"], 1) and _integer(row["source_ms"], -60000)
                and _integer(row["received_ms"]) and _integer(row["length"], 0, 4096)
                and _integer(opcode, -1, 255) and isinstance(fields, list)
                and len(fields) <= maximum and all(_integer(v, 0, 255) for v in fields)
                and row["state"] in STATES and row["result"] in RESULTS)
    if kind == "summary":
        counts = row.get("counts")
        return (set(row) == {"kind", "elapsed_ms", "final", "counts", "audio_min", "audio_max"}
                and _integer(row["elapsed_ms"]) and type(row["final"]) is bool
                and isinstance(counts, dict) and set(counts) == COUNTERS
                and all(_integer(v) for v in counts.values())
                and _integer(row["audio_min"], 0, 4096) and _integer(row["audio_max"], 0, 4096))
    if kind == "stage":
        return (set(row) == {"kind", "elapsed_ms", "stage"}
                and _integer(row["elapsed_ms"]) and row["stage"] in STAGES)
    if kind == "gatt":
        return (set(row) == {"kind", "elapsed_ms", "step"} | GATT_NUMBERS
                and _integer(row["elapsed_ms"]) and row["step"] in (
                    "service", "characteristics", "audio_subscription", "control_subscription", "ready")
                and all(_integer(row[k], -1, 65535) for k in GATT_NUMBERS))
    return False


class Observation:
    def __init__(self, send, clock=time.monotonic):
        self.send, self.clock = send, clock
        self.started = clock()
        self.last_summary = self.started
        self.counts = dict.fromkeys(sorted(COUNTERS), 0)
        self.rows = deque()
        self.number = 0
        self.audio_min = self.audio_max = 0

    def count(self, name, amount=1):
        self.counts[name] = min(MAX_COUNT, self.counts[name] + amount)

    def _ms(self, stamp):
        return max(-60000, min(MAX_COUNT, round((stamp - self.started) * 1000)))

    def _send(self, row):
        try:
            self.send(row)
        except Exception:
            self.count("send_failed")

    def stage(self, name):
        self._send(dict(kind="stage", elapsed_ms=max(0, self._ms(self.clock())), stage=name))

    def gatt(self, data):
        self._send(dict(kind="gatt", elapsed_ms=max(0, self._ms(self.clock())), **data))

    def voice_query(self, data, attempt):
        # Keep each evidence message within the existing 2048-byte pipe limit.
        items = data.get("items", [])
        if not isinstance(items, list):
            self.count('metadata_rejected')
            return
        for offset in range(0, max(1, min(32, len(items))), 8):
            row = dict(data, kind="voice_query", elapsed_ms=max(0, self._ms(self.clock())),
                       attempt=attempt, item_offset=offset, items=items[offset:offset + 8])
            if valid_record(row):
                self._send(row)
            else:
                self.count('metadata_rejected')

    def voice_link(self, data, attempt):
        row = dict(data, elapsed_ms=max(0, self._ms(self.clock())), attempt=attempt)
        if valid_record(row):
            self._send(row)
        else:
            self.count('metadata_rejected')

    def hid_flow(self, data):
        row = dict(data, elapsed_ms=max(0, self._ms(self.clock())))
        if valid_record(row):
            self._send(row)
        else:
            self.count('metadata_rejected')

    def hid_startup(self, data):
        if not valid_record(data):
            self.count('metadata_rejected')
            return
        row = dict(data, elapsed_ms=max(0, self._ms(self.clock())))
        if valid_record(row):
            self._send(row)

    def source_probe(self, state, started, *, status=-1, services=-1, hresult=-1):
        now = self.clock()
        self._send(dict(kind="source_probe", elapsed_ms=max(0, self._ms(now)), state=state,
                        duration_ms=max(0, min(MAX_COUNT, round((now - started) * 1000))),
                        status=status, services=services, hresult=hresult))

    def source_state(self, **data):
        self._send(dict(kind="source_state", elapsed_ms=max(0, self._ms(self.clock())), **data))

    def notification(self, attribute, value, ordinary=False):
        if ordinary:
            self.count("ordinary")
        else:
            self.voice_notification({0x3f: "control", 0x3c: "audio"}.get(attribute), value)

    def voice_notification(self, kind, value):
        if kind == "control":
            self.count("controls")
        elif kind == "audio":
            self.count("audio_packets")
            self.count("audio_bytes", len(value))
            self.audio_min = min(self.audio_min, len(value)) if self.counts["audio_packets"] > 1 else len(value)
            self.audio_max = max(self.audio_max, len(value))
        else:
            self.count("other_notify")

    def control(self, value, stamp, state, result):
        self.number = min(MAX_COUNT, self.number + 1)
        opcode = value[0] if value else -1
        # CAPS and fixed command headers only. SYNC contains audio predictor
        # samples; unknown opcodes and SYNC never expose their payload bytes.
        size = {0: 1, 4: 3, 8: 0, 11: 8, 12: 2}.get(opcode, 0)
        row = dict(kind="control", n=self.number, source_ms=self._ms(stamp),
                   received_ms=max(0, self._ms(self.clock())), length=len(value),
                   opcode=opcode, fields=list(value[1:1 + size]), state=state, result=result)
        if len(self.rows) == 256:
            self.rows.popleft()
            self.count("control_rows_lost")
        self.rows.append(row)

    def flush(self, final=False):
        # No audio-rate IPC. At most eight controls per worker iteration;
        # preserve repeats and source times. Summary is cumulative, including zero.
        for _ in range(min(8, len(self.rows))):
            self._send(self.rows.popleft())
        if final and self.rows:
            self.count("control_rows_lost", len(self.rows))
            self.rows.clear()
        now = self.clock()
        if final or now - self.last_summary >= 2:
            self._send(dict(kind="summary", elapsed_ms=max(0, self._ms(now)), final=final,
                            counts=dict(self.counts), audio_min=self.audio_min, audio_max=self.audio_max))
            self.last_summary = now
