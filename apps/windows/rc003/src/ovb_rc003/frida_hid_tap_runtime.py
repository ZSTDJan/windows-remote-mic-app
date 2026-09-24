"""Verified x64 Frida Gadget runtime for the RC003 HID-over-GATT tap.

The normal Windows keyboard path drops several RC003 usages.  The upstream
remote-bridge-hub implementation observes the completed HID read inside the
RC003 WUDF host instead.  This module contains only the verified runtime
preparation and the small Gadget script; button policy stays in the app.
"""

from __future__ import annotations

import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import threading

from .device_profile import BUTTON_USAGE_IDS

try:
    import winreg
except ImportError:  # pragma: no cover - import smoke on non-Windows hosts
    winreg = None  # type: ignore[assignment]


GADGET_VERSION = "17.15.3"
GADGET_ARCHIVE_NAME = "frida-gadget-17.15.3-windows-x86_64.dll.xz"
GADGET_ARCHIVE_SHA256 = (
    "b566d70189b6d551ad8f4e0bea24de08a3d4c0f559bb35b2bdb67d45182240c2"
)
GADGET_DLL_NAME = "RemoteMicRC003HidTap.dll"
GADGET_DLL_SHA256 = (
    "6fca4007b2284c765a6c15c967a741f536b5865bf83867326a54029a3b752748"
)
GADGET_CONFIG_NAME = "RemoteMicRC003HidTap.config"
GADGET_SCRIPT_NAME = "rc003_hid_gadget.js"
HID_TAP_PORT = int(os.environ.get("REMOTE_MIC_RC003_HID_TAP_PORT", "30684"))

BTHLE_ENUM_KEY = r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice"
HID_SERVICE_PREFIX = "{00001812-0000-1000-8000-00805f9b34fb}"
RC003_HARDWARE_TOKEN = "dev_vid&012717_pid&32b8_rev&00a4"
WUDF_DIAGNOSTIC_SUFFIX = r"Device Parameters\WUDFDiagnosticInfo"
INTERCEPT_PROTOCOL = 4
HID_DIAGNOSTIC_REVISION = 1
SOURCE_BINDING_REVISION = 1


# Frida Gadget is loaded into the WUDF host, not into this Python process.
# It copies the selected RC003 report to the loopback client and clears the
# usage payload before Windows translates it into a second keyboard event.
GADGET_SCRIPT = r"""
const UMDF_COPY_IOCTL = 0x80018483;
const EXPECTED_OUTPUT_LENGTH = 9;
const INTERCEPT_PROTOCOL = __INTERCEPT_PROTOCOL__;
const HID_DIAGNOSTIC_REVISION = __HID_DIAGNOSTIC_REVISION__;
const SOURCE_BINDING_REVISION = __SOURCE_BINDING_REVISION__;
const SCRIPT_BUILD_ID = "__SCRIPT_BUILD_ID__";
const HEARTBEAT_INTERVAL_MS = 5000;
const RECONNECT_DELAY_MS = 1000;
const MAX_INTERCEPT_LEASE_MS = 5000;

let host = "127.0.0.1";
let port = 30684;
let output = null;
let input = null;
let writeChain = Promise.resolve();
let reconnectTimer = null;
let socketConnection = null;
let connecting = false;
let disposed = false;
let hookInstalled = false;
let interceptLeaseDeadline = 0;
let interceptionReady = false;
let boundCopyHandle = null;
let pendingCopyCandidate = null;
let copyHandleEpoch = 0;
let copyProbeDeadline = 0;
let copyProbeSequence = 0;
let lastHookError = "";
let copyHealth = {};
let healthDiagnosticsEnabled = false;
let selectedSourceKey = null;
let exclusiveSourceVerified = false;
let sourceWarningSent = false;
let sourceApi = null;
const sourceEvidenceKeys = new Set();
let sourceDiagnosticsEnabled = false;
let sourceEntryInfo = {};
let sourceEntryStatus = "not_checked";
const observedOtherSources = new Set();
const sourceFrames = new Map();
const sourceHandles = new Map();
const sourceCallsites = new Map();
const sourceVtables = new Map();
const copyProbeUsages = new Set(__COPY_PROBE_USAGES__);

// DeviceIoControl's caller still owns the UMDF device-stack register. At the
// deeper NtDeviceIoControlFile entry that register is no longer contractual.
// Resolve only a verified caller instruction stream and device-stack interface; never
// guess the device from a report, handle number, register, or process alone.
function insideModule(module, address, size = 1) {
  return address.compare(module.base) >= 0 && address.add(size).compare(module.base.add(module.size)) <= 0;
}

function initializeSourceApi() {
  if (Process.arch !== "x64") return;
  try {
    const kernel = Process.getModuleByName("kernel32.dll");
    const getSystemDirectory = new NativeFunction(kernel.getExportByName("GetSystemDirectoryW"), "uint", ["pointer", "uint"]);
    const directory = Memory.alloc(65536);
    const count = getSystemDirectory(directory, 32768);
    if (!count || count >= 32768) return;
    const prefix = directory.readUtf16String(count) + "\\";
    const registry = Module.load(prefix + "advapi32.dll");
    const crypto = Module.load(prefix + "bcrypt.dll");
    const api = {
      query: new NativeFunction(registry.getExportByName("RegQueryValueExW"), "long", ["pointer", "pointer", "pointer", "pointer", "pointer", "pointer"]),
      lookup: new NativeFunction(Process.getModuleByName("ntdll.dll").getExportByName("RtlLookupFunctionEntry"), "pointer", ["pointer", "pointer", "pointer"]),
      hash: new NativeFunction(crypto.getExportByName("BCryptHash"), "int", ["pointer", "pointer", "uint", "pointer", "uint", "pointer", "uint"]),
      close: new NativeFunction(crypto.getExportByName("BCryptCloseAlgorithmProvider"), "int", ["pointer", "uint"]),
      containerName: Memory.allocUtf16String("ContainerID")
    };
    const open = new NativeFunction(crypto.getExportByName("BCryptOpenAlgorithmProvider"), "int", ["pointer", "pointer", "pointer", "uint"]);
    const algorithm = Memory.alloc(Process.pointerSize);
    if (open(algorithm, Memory.allocUtf16String("SHA256"), ptr(0), 0) !== 0) return;
    api.algorithm = algorithm.readPointer();
    sourceApi = api;
  } catch (_error) { sourceApi = null; }
}

function containerSourceKey(key, diagnostic = {}) {
  diagnostic.reason = "device_registry_unavailable";
  if (sourceApi === null || key.isNull()) return null;
  const size = Memory.alloc(4), type = Memory.alloc(4), value = Memory.alloc(80);
  type.writeU32(0);
  size.writeU32(80);
  const status = sourceApi.query(key, sourceApi.containerName, ptr(0), type, value, size);
  diagnostic.value_type = type.readU32();
  diagnostic.value_bytes = size.readU32();
  if (status !== 0) {
    diagnostic.reason = "device_registry_read_failed"; diagnostic.winerror = status; return null;
  }
  diagnostic.reason = "device_identity_invalid";
  if (type.readU32() !== 1 || (size.readU32() !== 74 && size.readU32() !== 78)) return null;
  const count = size.readU32() / 2;
  if (value.add((count - 1) * 2).readU16() !== 0) return null;
  const text = value.readUtf16String(count - 1);
  if (!/^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\})$/i.test(text)) return null;
  const canonical = text.replace(/[{}-]/g, "");
  if (/^0{31}[01]$/.test(canonical)) return null;
  const bytes = asciiBytes("RemoteMic physical remote v1\0").concat(canonical.match(/../g).map(x => parseInt(x, 16)));
  const data = Memory.alloc(bytes.length), digest = Memory.alloc(32);
  data.writeByteArray(bytes);
  diagnostic.reason = "device_identity_hash_failed";
  return sourceApi.hash(sourceApi.algorithm, ptr(0), 0, data, bytes.length, digest, 32) === 0 ? hex(digest, 32) : null;
}

function instanceSourceKey(key, diagnostic = {}, evidence = {}) {
  // The verified stack getter can return Device Parameters, not the Enum instance.
  // Derive only this live device's exact instance; never search other device keys.
  diagnostic.reason = "device_registry_unavailable";
  if (sourceApi === null || key.isNull()) return null;
  try {
    const query = new NativeFunction(Process.getModuleByName("ntdll.dll").getExportByName("NtQueryKey"),
      "int", ["pointer", "int", "pointer", "uint", "pointer"]);
    const buffer = Memory.alloc(8192), needed = Memory.alloc(4);
    const status = query(key, 3, buffer, 8192, needed);
    evidence.key_name_status = status >>> 0;
    if (status !== 0) return null;
    const length = buffer.readU32();
    if (length === 0 || length > 8188 || length % 2 !== 0) return null;
    const name = buffer.add(4).readUtf16String(length / 2);
    if (name.length * 2 !== length) return null;
    const parts = name.split("\\"), lower = parts.map(value => value.toLowerCase());
    const index = 5;
    evidence.key_scope = "other";
    if (lower.slice(0, 4).join("\\") !== "\\registry\\machine\\system" ||
        !/^(currentcontrolset|controlset[0-9]{3})$/.test(lower[4]) || lower[index] !== "enum" ||
        parts.length < index + 4 || parts.slice(4).some(part => !part || part.includes("\0"))) return null;
    const tail = lower.slice(index + 4);
    evidence.key_scope = tail.length === 0 ? "device_instance" :
      tail[0] === "device parameters" ? "device_parameters" : "enum_descendant";
    evidence.key_depth = tail.length;
    evidence.key_bus = ["bthledevice", "bthenum", "bthle", "hid"].includes(lower[index + 1]) ? lower[index + 1] : "other";
    if (tail.length > 1 || (tail.length === 1 && tail[0] !== "device parameters")) return null;
    if (tail.length === 0) return containerSourceKey(key, diagnostic);
    const registry = Process.getModuleByName("advapi32.dll");
    const open = new NativeFunction(registry.getExportByName("RegOpenKeyExW"), "long",
      ["pointer", "pointer", "uint", "uint", "pointer"]);
    const close = new NativeFunction(registry.getExportByName("RegCloseKey"), "long", ["pointer"]);
    const root = Memory.alloc(Process.pointerSize);
    evidence.instance_open_status = open(ptr("0xffffffff80000002"),
      Memory.allocUtf16String(parts.slice(3, index + 4).join("\\")), 0, 1, root);
    if (evidence.instance_open_status !== 0) {
      diagnostic.reason = "device_registry_read_failed";
      diagnostic.winerror = evidence.instance_open_status;
      return null;
    }
    try {
      const identity = containerSourceKey(root.readPointer(), diagnostic);
      evidence.instance_value_status = diagnostic.winerror === undefined ? 0 : diagnostic.winerror;
      evidence.instance_identity_valid = identity !== null;
      if (identity !== null) {
        evidence.instance_ref = identity.slice(0, 12);
        evidence.instance_matches_selected = identity === selectedSourceKey;
      }
      return identity;
    } finally { close(root.readPointer()); }
  } catch (_error) { evidence.query_incomplete = true; }
  return null;
}

function registrySourceEvidence(key) {
  // Opt-in failure detail uses the same resolver; raw names never leave the host.
  const evidence = {};
  instanceSourceKey(key, {}, evidence);
  return evidence;
}

function recordSourceEvidence(key, diagnostic) {
  if (!sourceDiagnosticsEnabled || sourceEvidenceKeys.size >= 8 || sourceEvidenceKeys.has(key.toString())) return;
  sourceEvidenceKeys.add(key.toString());
  // This only queries a live handle already proven to belong to the observed device object.
  emit({kind: "source_evidence", source_binding_revision: SOURCE_BINDING_REVISION,
        ...diagnostic, ...registrySourceEvidence(key)});
}

function pointerLoad(instruction, register) {
  const ops = instruction.operands;
  if (instruction.mnemonic !== "mov" || ops.length !== 2 || ops[0].type !== "reg" ||
      ops[0].value !== register || ops[1].type !== "mem" || ops[1].size !== 8) return null;
  const mem = ops[1].value;
  if (mem.index || mem.segment || !Number.isInteger(mem.disp) || mem.disp <= 0 || mem.disp > 4096) return null;
  return mem;
}

function deviceStackLoad(module, returnAddress) {
  if (sourceApi === null || !insideModule(module, returnAddress)) return null;
  const cacheKey = returnAddress.toString();
  if (sourceCallsites.has(cacheKey)) return sourceCallsites.get(cacheKey);
  let result = null;
  try {
    const imageBase = Memory.alloc(8);
    const entry = sourceApi.lookup(returnAddress.sub(1), imageBase, ptr(0));
    if (entry.isNull() || !imageBase.readPointer().equals(module.base)) return null;
    let cursor = module.base.add(entry.readU32());
    const end = module.base.add(entry.add(4).readU32());
    if (!insideModule(module, cursor) || returnAddress.compare(end) > 0 ||
        returnAddress.sub(cursor).toUInt32() > 8192) return null;
    let candidate = null;
    while (cursor.compare(returnAddress) < 0) {
      const instruction = Instruction.parse(cursor);
      const next = instruction.next;
      if (next.compare(returnAddress) > 0) break;
      const load = pointerLoad(instruction, "rcx");
      if (load && /^(rbx|rbp|rsi|rdi|r12|r13|r14|r15)$/.test(load.base)) {
        candidate = {base: load.base, disp: load.disp};
      } else if (instruction.mnemonic === "call" && next.equals(returnAddress)) {
        result = candidate;
      } else if (!/^(mov|lea|nop)$/.test(instruction.mnemonic) ||
                 (instruction.operands[0] && instruction.operands[0].type === "reg" &&
                  !/^(rax|eax|r8|r8d|r9|r9d|rdx|edx)$/.test(instruction.operands[0].value))) {
        candidate = null;
      }
      cursor = next;
    }
  } catch (_error) { result = null; }
  if (sourceCallsites.size < 64) sourceCallsites.set(cacheKey, result);
  return result;
}

function deviceStackInterface(module, queryInterface) {
  if (!insideModule(module, queryInterface) || sourceApi === null) return false;
  const imageBase = Memory.alloc(8);
  const entry = sourceApi.lookup(queryInterface, imageBase, ptr(0));
  if (entry.isNull() || !imageBase.readPointer().equals(module.base) ||
      !module.base.add(entry.readU32()).equals(queryInterface)) return false;
  const end = module.base.add(entry.add(4).readU32());
  if (!insideModule(module, end.sub(1)) || end.sub(queryInterface).toUInt32() > 512) return false;
  // Windows builds omit C++ RTTI. Identify the interface by the two halves of
  // its IID checked by QueryInterface (verified with Microsoft's matching PDB).
  // Do not call QueryInterface/AddRef or execute any undocumented host method.
  const iid = "7a2dfa5b66f7d34a959ed8fe0f0e83e1";
  let cursor = queryInterface, previous = null, low = false, high = false;
  while (cursor.compare(end) < 0) {
    const instruction = Instruction.parse(cursor), ops = instruction.operands;
    if (instruction.next.compare(end) > 0) return false;
    if (previous && /^(sub|cmp)$/.test(instruction.mnemonic) && ops.length === 2 &&
        ops[0].type === "reg" && ops[0].value === "rax" && ops[1].type === "mem" &&
        ops[1].size === 8 && ops[1].value.base === "rip") {
      const before = previous.operands;
      if (previous.mnemonic === "mov" && before.length === 2 && before[0].type === "reg" &&
          before[0].value === "rax" && before[1].type === "mem" && before[1].size === 8 &&
          before[1].value.base === "rdx" && !before[1].value.index) {
        const half = before[1].value.disp;
        const address = instruction.next.add(ops[1].value.disp).sub(half);
        if ((half === 0 || half === 8) && insideModule(module, address, 16) && hex(address, 16) === iid) {
          if (half === 0) low = true; else high = true;
        }
      }
    }
    previous = instruction; cursor = instruction.next;
  }
  return low && high;
}

function deviceRegistryKey(module, object) {
  const vtable = object.readPointer();
  if (!insideModule(module, vtable, 32 * 8)) return null;
  let offset = sourceVtables.get(vtable.toString());
  if (offset !== undefined) return object.add(offset).readPointer();
  if (!deviceStackInterface(module, vtable.readPointer())) return null;
  // This is a pure field getter in the verified UMDF ABI. Decode it rather
  // than invoking an undocumented C++ method or hard-coding its field offset.
  const getter = vtable.add(31 * 8).readPointer();
  if (!insideModule(module, getter, 16)) return null;
  const instruction = Instruction.parse(getter), field = pointerLoad(instruction, "rax");
  if (field === null || field.base !== "rcx" || Instruction.parse(instruction.next).mnemonic !== "ret") return null;
  if (sourceVtables.size < 64) sourceVtables.set(vtable.toString(), field.disp);
  return object.add(field.disp).readPointer();
}

function resolveCopyDevice(context, returnAddress, handle, diagnostic = {}) {
  try {
    diagnostic.reason = "source_host_unverified";
    const module = Process.findModuleByAddress(returnAddress);
    diagnostic.caller_module = module && ["wudfhost.exe", "kernel32.dll", "kernelbase.dll", "ntdll.dll", "apphelp.dll"].includes(module.name.toLowerCase()) ? module.name.toLowerCase() : "other";
    if (module) diagnostic.caller_rva = returnAddress.sub(module.base).toUInt32();
    if (module === null || module.name.toLowerCase() !== "wudfhost.exe") return null;
    diagnostic.reason = "copy_callsite_unverified";
    const load = deviceStackLoad(module, returnAddress);
    if (load === null) return null;
    diagnostic.reason = "copy_object_unverified";
    const object = context[load.base];
    if (!object || object.isNull() || !object.add(load.disp).readPointer().equals(handle)) return null;
    diagnostic.reason = "device_interface_unverified";
    const registryKey = deviceRegistryKey(module, object);
    if (registryKey === null || registryKey.isNull()) return null;
    const cached = sourceHandles.get(handle.toString());
    if (cached && cached.object.equals(object) && cached.registryKey.equals(registryKey)) return cached.key;
    const key = instanceSourceKey(registryKey, diagnostic);
    if (key === null) recordSourceEvidence(registryKey, diagnostic);
    if (key !== null && sourceHandles.size < 64) sourceHandles.set(handle.toString(), {object, registryKey, key});
    return key;
  } catch (_error) { return null; }
}

function copySource(args) {
  // The None path is retained only for older, exclusive-host diagnostic clients.
  if (selectedSourceKey === null) return {kind: "exclusive", key: null};
  const frames = sourceFrames.get(Process.getCurrentThreadId());
  const frame = frames && frames[frames.length - 1];
  let diagnostic = {reason: "copy_frame_unverified"};
  if (frame && frame.handle.equals(args[0]) && frame.metadata.equals(args[6]) &&
      frame.buffer.equals(args[8]) && frame.inputLength === args[7].toUInt32() &&
      frame.outputLength === args[9].toUInt32()) {
    if (frame.key !== null) {
      if (frame.key === selectedSourceKey) return {kind: "device", key: frame.key};
      if (sourceDiagnosticsEnabled && observedOtherSources.size < 8 && !observedOtherSources.has(frame.key)) {
        observedOtherSources.add(frame.key);
        emit({kind: "source_observation", result: "not_selected", device_ref: frame.key.slice(0, 12)});
      }
      return null;
    }
    diagnostic = frame.diagnostic || diagnostic;
  }
  if (exclusiveSourceVerified) return {kind: "exclusive", key: selectedSourceKey};
  if (boundCopyHandle === null || boundCopyHandle === args[0].toString()) {
    reportSourceUnavailable(diagnostic);
  }
  return null;
}

function reportSourceUnavailable(diagnostic) {
  if (boundCopyHandle !== null) resetCopyOwnership();
  if (!sourceWarningSent) {
    sourceWarningSent = true;
    emit({kind: "source_status", source_binding_revision: SOURCE_BINDING_REVISION, verified: false,
          ...diagnostic});
  }
}

function resetCopyHealth() {
  copyHealth = {ioctl_calls: 0, layout_matches: 0, candidate_reports: 0,
    intercepted_reports: 0, copy_failures: 0, last_ntstatus: 0};
}
resetCopyHealth();

function reportHookError(code, ntstatus) {
  lastHookError = code;
  emit({kind: "error", code: code, ntstatus: ntstatus, diagnostic_revision: HID_DIAGNOSTIC_REVISION});
}

function copyHealthSnapshot() {
  return {...copyHealth, source_bound: boundCopyHandle !== null,
    waiting_neutral: boundCopyHandle !== null && !interceptionReady};
}

function copyProbeReport(pointer) {
  try {
    const bytes = new Uint8Array(pointer.readByteArray(9));
    if (bytes[0] !== 1 || bytes[1] !== 0 || bytes[2] !== 0) return null;
    for (let i = 3; i < 9; i += 2) {
      const usage = bytes[i] | (bytes[i + 1] << 8);
      if (usage !== 0 && !copyProbeUsages.has(usage)) return null;
    }
    return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
  } catch (_error) { return null; }
}

function startCopyProbe(seconds) {
  copyProbeDeadline = 0;
  if (!Number.isInteger(seconds) || seconds <= 0 || seconds > 300) return;
  copyProbeDeadline = Date.now() + seconds * 1000;
  copyProbeSequence = 0;
  const deadline = copyProbeDeadline;
  emit({kind: "copy_probe", phase: "ready", host_pid: Process.id,
        wall_ms: Date.now(), deadline_ms: deadline});
  setTimeout(() => {
    if (copyProbeDeadline !== deadline) return;
    copyProbeDeadline = 0;
    emit({kind: "copy_probe", phase: "stopped", host_pid: Process.id,
          wall_ms: Date.now(), call_count: copyProbeSequence});
  }, seconds * 1000);
}

function beginCopyObservation(args, context) {
  if (copyProbeDeadline === 0 || Date.now() >= copyProbeDeadline ||
      args[7].toUInt32() !== 8 || args[9].toUInt32() !== 9 || args[6].isNull()) return null;
  try {
    const record = {kind: "copy_probe", phase: "copy", host_pid: Process.id,
      call_id: ++copyProbeSequence, native_thread_id: Process.getCurrentThreadId(),
      handle: args[0].toString(), request_id: args[6].readU32(),
      operation: args[6].add(4).readU8(), selector: args[6].add(5).readU8(),
      entry_ms: Date.now(), entry_hex: copyProbeReport(args[8])};
    if (record.entry_hex !== null && record.call_id <= 8) {
      try {
        record.stack = Thread.backtrace(context, Backtracer.ACCURATE).slice(0, 12).map(address => {
          const module = Process.findModuleByAddress(address);
          return module ? {module: module.name, rva: address.sub(module.base).toString()} : {module: "unknown", rva: ""};
        });
      } catch (_error) { record.stack = []; }
    }
    return record;
  } catch (_error) { return null; }
}

function asciiBytes(text) {
  const result = [];
  for (let index = 0; index < text.length; index++) {
    result.push(text.charCodeAt(index) & 0xff);
  }
  return result;
}

function hex(pointer, length) {
  if (pointer.isNull() || length <= 0) return "";
  const bytes = new Uint8Array(pointer.readByteArray(length));
  let result = "";
  for (let index = 0; index < bytes.length; index++) {
    result += bytes[index].toString(16).padStart(2, "0");
  }
  return result;
}

function textFromBytes(data) {
  const bytes = new Uint8Array(data);
  let result = "";
  for (let index = 0; index < bytes.length; index++) {
    result += String.fromCharCode(bytes[index]);
  }
  return result;
}

function interceptionActive() {
  if (output === null || interceptLeaseDeadline === 0) return false;
  if (Date.now() < interceptLeaseDeadline) return true;
  interceptLeaseDeadline = 0;
  interceptionReady = false;
  emit({ kind: "intercept_expired", protocol: INTERCEPT_PROTOCOL });
  return false;
}

function interceptKeyboardReport(pointer, length) {
  if (!interceptionActive() || pointer.isNull() || length !== EXPECTED_OUTPUT_LENGTH) {
    return null;
  }
  const raw = hex(pointer, length);
  if (!raw.startsWith("010000")) return null;
  if (!interceptionReady) {
    if (raw.slice(6) !== "000000000000") return null;
    // The enable request can race a key that Windows already received. Let
    // that hold reach its neutral report before taking ownership so its key-up
    // can never be intercepted without the matching key-down.
    interceptionReady = true;
  }
  pointer.add(3).writeByteArray([0, 0, 0, 0, 0, 0]);
  return raw;
}

function resetCopyOwnership() {
  boundCopyHandle = null;
  pendingCopyCandidate = null;
  copyHandleEpoch++;
  interceptionReady = false;
  sourceHandles.clear();
  sourceWarningSent = false;
}

function interceptOutgoingCopy(args, source) {
  if (!interceptionActive() || args[7].toUInt32() !== 8 || args[6].isNull() ||
      args[9].toUInt32() !== EXPECTED_OUTPUT_LENGTH || args[8].isNull()) return null;
  if (args[6].add(4).readU8() !== 2 || args[6].add(5).readU8() !== 1) return null;
  copyHealth.layout_matches++;
  const handle = args[0].toString();
  if (boundCopyHandle !== null) {
    return handle === boundCopyHandle ? {raw: interceptKeyboardReport(args[8], 9), candidate: null} : null;
  }
  const raw = copyProbeReport(args[8]);
  if (raw === null) return null;
  copyHealth.candidate_reports++;
  if (pendingCopyCandidate === null) {
    const prebound = selectedSourceKey !== null && source.kind === "device" &&
                     source.key === selectedSourceKey;
    pendingCopyCandidate = {handle: handle, epoch: ++copyHandleEpoch, neutral: false, announced: false,
                            source_kind: source.kind, source_key: source.key, prebound: prebound};
    if (prebound) {
      // Device identity was proven at the native callsite. Own this first
      // report before Windows translates it, while retaining the client bind
      // acknowledgement as an independent validation step.
      boundCopyHandle = handle;
      interceptionReady = true;
      const owned = interceptKeyboardReport(args[8], EXPECTED_OUTPUT_LENGTH);
      if (owned !== null) return {raw: owned, candidate: pendingCopyCandidate};
      boundCopyHandle = null;
      interceptionReady = false;
      pendingCopyCandidate.prebound = false;
    }
  }
  return pendingCopyCandidate.handle === handle ? {raw: null, candidate: pendingCopyCandidate} : null;
}

function scheduleReconnect() {
  if (disposed || reconnectTimer !== null) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectToHub();
  }, RECONNECT_DELAY_MS);
}

function markDisconnected(currentOutput) {
  if (output !== currentOutput) return;
  const connection = socketConnection;
  socketConnection = null;
  output = null;
  input = null;
  interceptLeaseDeadline = 0;
  resetCopyOwnership();
  copyProbeDeadline = 0;
  if (connection !== null) connection.close().catch(() => {});
  scheduleReconnect();
}

function emit(payload) {
  const currentOutput = output;
  if (currentOutput === null) {
    scheduleReconnect();
    return;
  }
  const line = JSON.stringify(payload) + "\n";
  writeChain = writeChain
    .then(() => currentOutput.writeAll(asciiBytes(line)))
    .catch(() => markDisconnected(currentOutput));
}

function acknowledgeControl(action, accepted, state, detail) {
  emit({
    kind: "control_ack",
    action: action,
    accepted: accepted,
    state: state,
    detail: detail || "",
    protocol: INTERCEPT_PROTOCOL
  });
}

function handleControl(message) {
  if (message === null || typeof message !== "object") return;
  if (message.kind !== "intercept_control") return;
  const action = message.action;
  if (message.protocol !== INTERCEPT_PROTOCOL) {
    interceptLeaseDeadline = 0;
    resetCopyOwnership();
    acknowledgeControl(action, false, "disabled", "protocol_mismatch");
    return;
  }
  if (action === "disable") {
    interceptLeaseDeadline = 0;
    resetCopyOwnership();
    copyProbeDeadline = 0;
    acknowledgeControl(action, true, "disabled", "");
    return;
  }
  if (action === "bind_copy_handle" || action === "reject_copy_handle") {
    const candidate = pendingCopyCandidate;
    if (!interceptionActive() || candidate === null || message.handle !== candidate.handle ||
        message.epoch !== candidate.epoch) {
      acknowledgeControl(action, false, "disabled", "stale_copy_candidate");
      return;
    }
    if (action === "reject_copy_handle") {
      resetCopyOwnership();
      exclusiveSourceVerified = false;
      acknowledgeControl(action, true, "unbound", "");
      return;
    }
    boundCopyHandle = candidate.handle;
    interceptionReady = candidate.prebound === true || candidate.neutral;
    pendingCopyCandidate = null;
    acknowledgeControl(action, true, "bound", "");
    return;
  }
  const leaseMs = Number(message.lease_ms);
  if ((action !== "enable" && action !== "renew") ||
      !Number.isInteger(leaseMs) || leaseMs <= 0 || leaseMs > MAX_INTERCEPT_LEASE_MS) {
    interceptLeaseDeadline = 0;
    interceptionReady = false;
    acknowledgeControl(action, false, "disabled", "invalid_control");
    return;
  }
  if (action === "enable") resetCopyOwnership();
  sourceDiagnosticsEnabled = message.source_diagnostics === true;
  if (action === "enable") {
      sourceEvidenceKeys.clear();
    observedOtherSources.clear();
    selectedSourceKey = null;
    exclusiveSourceVerified = false;
    if (message.selected_key !== undefined) {
      if (typeof message.selected_key !== "string" || message.selected_key.length !== 64 ||
          !/^[a-f0-9]{64}$/.test(message.selected_key) ||
          message.source_binding_revision !== SOURCE_BINDING_REVISION) {
        interceptLeaseDeadline = 0;
        acknowledgeControl(action, false, "disabled", "invalid_source_selection");
        return;
      }
      selectedSourceKey = message.selected_key;
      exclusiveSourceVerified = message.exclusive_source_verified === true;
    }
    resetCopyHealth();
    healthDiagnosticsEnabled = message.health_diagnostics === HID_DIAGNOSTIC_REVISION;
  }
  if (action === "enable") startCopyProbe(message.copy_probe_seconds);
  interceptLeaseDeadline = Date.now() + leaseMs;
  acknowledgeControl(action, true, "enabled", "");
}

async function readControls(connection) {
  const currentOutput = connection.output;
  const currentInput = connection.input;
  let buffer = "";
  try {
    while (output === currentOutput && input === currentInput) {
      const chunk = await currentInput.read(4096);
      if (disposed || output !== currentOutput || input !== currentInput) return;
      // Frida InputStream signals EOF with an empty ArrayBuffer.
      if (chunk === null || chunk.byteLength === 0) break;
      buffer += textFromBytes(chunk);
      if (buffer.length > 65536) break;
      let newline = buffer.indexOf("\n");
      while (newline !== -1) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        try {
          handleControl(JSON.parse(line));
        } catch (_error) {
          // Invalid control input never enables interception.
          interceptLeaseDeadline = 0;
          interceptionReady = false;
        }
        newline = buffer.indexOf("\n");
      }
    }
  } catch (_error) {
    // The lease is cleared below before reconnecting.
  }
  markDisconnected(currentOutput);
}

async function connectToHub() {
  if (disposed || connecting || output !== null) return;
  connecting = true;
  try {
    const connection = await Socket.connect({
      family: "ipv4",
      host: host,
      port: port
    });
    if (disposed) {
      await connection.close();
      return;
    }
    socketConnection = connection;
    output = connection.output;
    input = connection.input;
    interceptLeaseDeadline = 0;
    interceptionReady = false;
    emit({
      kind: "ready",
      pid: Process.id,
      hook_installed: hookInstalled,
      protocol: INTERCEPT_PROTOCOL,
      source_binding_revision: SOURCE_BINDING_REVISION,
      diagnostic_revision: HID_DIAGNOSTIC_REVISION,
      script_build_id: SCRIPT_BUILD_ID,
      source_entry: sourceEntryInfo,
      source_entry_status: sourceEntryStatus,
      hook_error_code: lastHookError
    });
    readControls(connection);
  } catch (_error) {
    output = null;
    input = null;
    interceptLeaseDeadline = 0;
    interceptionReady = false;
    scheduleReconnect();
  } finally {
    connecting = false;
  }
}

function deviceIoControlImportSlot(module) {
  // Read the loaded PE's name/index pairing ourselves. Frida has reported
  // the preceding import's slot for DeviceIoControl in a live WUDFHost.
  function span(rva, size) {
    if (!Number.isInteger(rva) || rva < 0 || !Number.isInteger(size) || size < 1 ||
        rva > module.size - size) throw Error("invalid import range");
    return module.base.add(rva);
  }
  function nameAt(rva) {
    let name = "";
    for (let i = 0; i < 256; i++) {
      const ch = span(rva + i, 1).readU8();
      if (ch === 0) return name;
      name += String.fromCharCode(ch);
    }
    throw Error("unterminated import name");
  }
  sourceEntryStatus = "invalid_import_table";
  if (span(0, 64).readU16() !== 0x5a4d) return null;
  const ntRva = module.base.add(0x3c).readU32(), nt = span(ntRva, 24);
  if (nt.readU32() !== 0x4550 || nt.add(4).readU16() !== 0x8664 ||
      nt.add(20).readU16() < 128) return null;
  const optional = span(ntRva + 24, nt.add(20).readU16());
  if (optional.readU16() !== 0x20b || optional.add(56).readU32() !== module.size ||
      optional.add(108).readU32() < 2) return null;
  const directoryRva = optional.add(120).readU32(), directorySize = optional.add(124).readU32();
  if (directoryRva === 0 || directorySize < 20) return null;
  span(directoryRva, directorySize);
  let found = null;
  for (let index = 0; index < Math.min(1024, Math.floor(directorySize / 20)); index++) {
    const descriptor = span(directoryRva + index * 20, 20);
    const lookup = descriptor.readU32(), table = descriptor.add(16).readU32();
    const dllName = descriptor.add(12).readU32();
    if ((lookup | table | dllName | descriptor.add(4).readU32() | descriptor.add(8).readU32()) === 0) {
      sourceEntryStatus = found === null ? "import_missing" : "target_unverified";
      return found;
    }
    if (lookup === 0 || table === 0 || dllName === 0 || nameAt(dllName).length === 0) return null;
    let terminated = false;
    for (let entry = 0; entry < 4096; entry++) {
      const name = span(lookup + entry * 8, 8).readU64();
      if (name.compare(uint64(0)) === 0) { terminated = true; break; }
      const slot = span(table + entry * 8, 8);
      if (name.and(uint64("0x8000000000000000")).compare(uint64(0)) !== 0) continue;
      if (name.compare(uint64(0xffffffff)) > 0) return null;
      const nameRva = name.toNumber();
      span(nameRva, 3);
      if (nameAt(nameRva + 2) !== "DeviceIoControl") continue;
      if (found !== null) { sourceEntryStatus = "import_ambiguous"; return null; }
      found = slot;
    }
    if (!terminated) return null;
  }
  return null;
}

function deviceIoControlEntry(module) {
  try {
    const slot = deviceIoControlImportSlot(module);
    if (slot === null) return null;
    // API-set resolution can report a different address from the live IAT.
    // Read the slot so the source frame is captured before any Win32 wrapper
    // changes its caller's nonvolatile registers (notably the device in rbp).
    const target = slot.readPointer();
    if (target.isNull()) return null;
    for (const name of ["kernel32.dll", "kernelbase.dll"]) {
      const owner = Process.findModuleByName(name);
      if (owner === null) continue;
      // Windows application compatibility may redirect GetProcAddress while
      // an existing import still points at the original PE export.
      const resolved = owner.findExportByName("DeviceIoControl");
      if (resolved !== null && target.equals(resolved)) { sourceEntryStatus = "verified"; return target; }
      const exported = owner.enumerateExports().find(item => item.name === "DeviceIoControl" && item.type === "function");
      if (exported && target.equals(exported.address)) { sourceEntryStatus = "verified"; return target; }
    }
    return null;
  } catch (_error) { return null; }
}

function installHook() {
  if (hookInstalled) return;
  initializeSourceApi();
  const ntdll = Process.findModuleByName("ntdll.dll");
  const sourceHost = Process.findModuleByName("WUDFHost.exe");
  if (sourceHost === null) sourceEntryStatus = "source_host_missing";
  const deviceTarget = sourceHost ? deviceIoControlEntry(sourceHost) : null;
  if (deviceTarget !== null) {
    const module = Process.findModuleByAddress(deviceTarget);
    if (module) sourceEntryInfo = {
      module: ["kernel32.dll", "kernelbase.dll", "apphelp.dll"].includes(module.name.toLowerCase()) ? module.name.toLowerCase() : "other",
      rva: deviceTarget.sub(module.base).toUInt32()
    };
  }
  const target = ntdll ? ntdll.findExportByName("NtDeviceIoControlFile") : null;
  const closeTarget = ntdll ? ntdll.findExportByName("NtClose") : null;
  if (target === null) {
    reportHookError("copy_export_missing");
    return;
  }
  if (closeTarget === null) {
    reportHookError("close_export_missing");
    return;
  }
  if (deviceTarget !== null) Interceptor.attach(deviceTarget, {
    onEnter(args) {
      this.sourceFrame = null;
      if (!interceptionActive() || selectedSourceKey === null) return;
      const frame = {handle: args[0], metadata: args[2], inputLength: args[3].toUInt32(),
                     buffer: args[4], outputLength: args[5].toUInt32(), key: null,
                     diagnostic: {reason: "copy_frame_unverified"}};
      if (args[1].toUInt32() === UMDF_COPY_IOCTL && frame.inputLength === 8 && frame.outputLength === 9) {
        frame.key = resolveCopyDevice(this.context, this.returnAddress, args[0], frame.diagnostic);
      }
      const thread = Process.getCurrentThreadId();
      const frames = sourceFrames.get(thread) || [];
      frames.push(frame);
      sourceFrames.set(thread, frames);
      this.sourceFrame = frame;
      this.sourceThread = thread;
    },
    onLeave(_retval) {
      if (this.sourceFrame === null) return;
      const frames = sourceFrames.get(this.sourceThread);
      if (!frames) return;
      // A mismatched stack loses its evidence; it never borrows an older frame.
      if (frames.pop() !== this.sourceFrame || frames.length === 0) sourceFrames.delete(this.sourceThread);
    }
  });
  Interceptor.attach(target, {
    onEnter(args) {
      this.copyObservation = null;
      this.ownedReport = null;
      this.copyCandidate = null;
      this.captureEpoch = copyHandleEpoch;
      this.capture = args[5].toUInt32() === UMDF_COPY_IOCTL;
      if (this.capture) {
        copyHealth.ioctl_calls++;
        this.output = args[8];
        this.outputLength = args[9].toUInt32();
        try {
          if (!interceptionActive() || args[7].toUInt32() !== 8 || args[9].toUInt32() !== 9 ||
              args[6].isNull() || args[6].add(4).readU8() !== 2 || args[6].add(5).readU8() !== 1) return;
          const source = copySource(args);
          if (source === null) return;
          // Exclusive fallback needs the client's fresh membership check before
          // any report content may enter diagnostics or be suppressed.
          if (selectedSourceKey === null || source.kind === "device" || boundCopyHandle === args[0].toString()) {
            this.copyObservation = beginCopyObservation(args, this.context);
          }
          const decision = interceptOutgoingCopy(args, source);
          this.captureEpoch = copyHandleEpoch;
          if (decision !== null) {
            this.ownedReport = decision.raw;
            this.copyCandidate = decision.candidate;
          }
        } catch (error) {
          reportHookError("copy_entry_exception");
        }
      }
    },
    onLeave(retval) {
      if (this.captureEpoch === copyHandleEpoch && this.copyCandidate !== null && this.copyCandidate === pendingCopyCandidate && retval.toUInt32() === 0) {
        const raw = copyProbeReport(this.output);
        if (raw !== null) {
          if (pendingCopyCandidate.prebound !== true) {
            pendingCopyCandidate.neutral = raw.slice(6) === "000000000000";
          }
          if (!pendingCopyCandidate.announced) {
            pendingCopyCandidate.announced = true;
            emit({kind: "copy_candidate", handle: pendingCopyCandidate.handle,
                  epoch: pendingCopyCandidate.epoch, protocol: INTERCEPT_PROTOCOL,
                  source_kind: pendingCopyCandidate.source_kind, source_key: pendingCopyCandidate.source_key,
                  source_binding_revision: SOURCE_BINDING_REVISION});
          }
        }
      }
      const observation = this.copyObservation;
      if (observation !== null && this.captureEpoch === copyHandleEpoch) {
        observation.return_ms = Date.now();
        observation.status = retval.toUInt32();
        observation.return_hex = copyProbeReport(this.output);
        if (observation.entry_hex !== null || observation.return_hex !== null) emit(observation);
      }
      if (!this.capture || this.output.isNull()) return;
      if (retval.toUInt32() !== 0) {
        if (this.ownedReport !== null || this.copyCandidate !== null) {
          copyHealth.copy_failures++;
          copyHealth.last_ntstatus = retval.toUInt32();
          const reportOwned = this.ownedReport !== null;
          let restored = null;
          if (reportOwned) {
            restored = false;
            try {
              this.output.writeByteArray(this.ownedReport.match(/../g).map(value => parseInt(value, 16)));
              restored = true;
            } catch (error) { reportHookError("copy_restore_exception", retval.toUInt32()); }
          }
          if (healthDiagnosticsEnabled && copyHealth.copy_failures === 1) {
            emit({kind: "copy_failure", ntstatus: retval.toUInt32(), restored: restored,
                  report_owned: reportOwned,
                  diagnostic_revision: HID_DIAGNOSTIC_REVISION});
          }
          if (this.captureEpoch === copyHandleEpoch && this.copyCandidate !== null &&
              this.copyCandidate === pendingCopyCandidate && this.copyCandidate.prebound === true) {
            resetCopyOwnership();
          }
        }
        return;
      }
      try {
        if (this.captureEpoch !== copyHandleEpoch || !interceptionActive()) return;
        const raw = this.ownedReport;
        if (raw !== null) {
          copyHealth.intercepted_reports++;
          emit({
            kind: "gatt_read",
            raw: raw,
            intercepted: true,
            copy_probe_id: observation === null ? 0 : observation.call_id,
            protocol: INTERCEPT_PROTOCOL,
            source_key: selectedSourceKey,
            source_binding_revision: SOURCE_BINDING_REVISION
          });
        }
      } catch (error) {
        reportHookError("copy_delivery_exception");
      }
    }
  });
  Interceptor.attach(closeTarget, {
    onEnter(args) {
      const handle = args[0].toString();
      sourceHandles.delete(handle);
      for (const [sourceHandle, source] of sourceHandles) {
        if (source.registryKey.equals(args[0])) sourceHandles.delete(sourceHandle);
      }
      if (handle !== boundCopyHandle &&
          (pendingCopyCandidate === null || handle !== pendingCopyCandidate.handle)) return;
      resetCopyOwnership();
      interceptLeaseDeadline = 0;
      reportHookError("bound_copy_handle_closed");
    }
  });
  hookInstalled = true;
}

const heartbeatTimer = setInterval(() => {
  if (output === null) {
    scheduleReconnect();
  } else {
    emit({
      kind: "heartbeat",
      pid: Process.id,
      protocol: INTERCEPT_PROTOCOL,
      diagnostic_revision: HID_DIAGNOSTIC_REVISION,
      intercept_enabled: interceptionActive(),
      intercept_ready: interceptionReady,
      copy_health: copyHealthSnapshot()
    });
  }
}, HEARTBEAT_INTERVAL_MS);

rpc.exports = {
  async init(_stage, parameters) {
    host = parameters.host || host;
    port = parameters.port || port;
    try { installHook(); }
    catch (_error) { reportHookError("hook_install_exception"); }
    await connectToHub();
  },
  async dispose() {
    disposed = true;
    interceptLeaseDeadline = 0;
    resetCopyOwnership();
    copyProbeDeadline = 0;
    sourceFrames.clear();
    sourceCallsites.clear();
    sourceVtables.clear();
    clearInterval(heartbeatTimer);
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    const connection = socketConnection;
    socketConnection = null;
    output = null;
    input = null;
    // Do not wait on pending reads/writes before closing the connection.
    // Frida removes this script's interceptors when unloading it.
    if (connection !== null) {
      try { await connection.close(); } catch (_error) {}
    }
    if (sourceApi !== null) {
      sourceApi.close(sourceApi.algorithm, 0);
      sourceApi = null;
    }
  }
};
""".replace("__COPY_PROBE_USAGES__", json.dumps(sorted(BUTTON_USAGE_IDS))).replace(
    "__INTERCEPT_PROTOCOL__", str(INTERCEPT_PROTOCOL)
).replace(
    "__HID_DIAGNOSTIC_REVISION__", str(HID_DIAGNOSTIC_REVISION)
).replace(
    "__SOURCE_BINDING_REVISION__", str(SOURCE_BINDING_REVISION)
).strip() + "\n"
GADGET_SCRIPT_BUILD_ID = hashlib.sha256(GADGET_SCRIPT.encode("utf-8")).hexdigest()
GADGET_SCRIPT = GADGET_SCRIPT.replace("__SCRIPT_BUILD_ID__", GADGET_SCRIPT_BUILD_ID)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gadget_archive_path() -> Path:
    return Path(__file__).resolve().with_name("frida_assets") / GADGET_ARCHIVE_NAME


def secure_runtime_directory(
    *,
    user_sid: str | None = None,
    program_files_root: Path | None = None,
) -> Path:
    from . import hid_elevation_windows

    sid = user_sid or hid_elevation_windows.current_user_sid()
    owner_root = hid_elevation_windows.protected_runtime_owner_root(
        sid,
        program_files_root=program_files_root,
    )
    # A distinct path identifies runtimes whose config was loaded with reload
    # enabled. Rewriting the config of an already-loaded legacy DLL cannot do so.
    return owner_root / f"{GADGET_VERSION}-x64-{GADGET_DLL_SHA256[:12]}-reload"


def gadget_config_text() -> str:
    return (
        json.dumps(
            {
                "interaction": {
                    "type": "script",
                    "path": GADGET_SCRIPT_NAME,
                    "parameters": {"host": "127.0.0.1", "port": HID_TAP_PORT},
                    "on_change": "reload",
                },
                "runtime": "qjs",
                "teardown": "minimal",
            },
            indent=2,
        )
        + "\n"
    )


def _write_verified_text(path: Path, content: str, *, user_sid: str) -> None:
    from . import hid_elevation_windows

    encoded = content.encode("utf-8")
    if path.is_file():
        try:
            if path.read_bytes() == encoded:
                hid_elevation_windows._apply_path_security(
                    path,
                    user_sid=user_sid,
                    directory=False,
                    read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
                )
                return
        except OSError:
            pass
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_bytes(encoded)
        hid_elevation_windows._apply_path_security(
            temporary,
            user_sid=user_sid,
            directory=False,
            read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _runtime_validation_error(reason: str) -> RuntimeError:
    error = RuntimeError(reason)
    error.injection_diagnostic = {"stage": "runtime_prepare", "reason": reason}
    return error


def prepare_secure_runtime() -> Path:
    from . import hid_elevation_windows

    archive = gadget_archive_path()
    if not archive.is_file():
        raise FileNotFoundError(archive)
    archive_hash = sha256_file(archive)
    if archive_hash != GADGET_ARCHIVE_SHA256:
        raise _runtime_validation_error("runtime_archive_hash_mismatch")

    sid = hid_elevation_windows.current_user_sid()
    program_files_root = hid_elevation_windows._program_files_root()
    owner_root = hid_elevation_windows.protected_runtime_owner_root(
        sid, program_files_root=program_files_root
    )
    destination = secure_runtime_directory(
        user_sid=sid,
        program_files_root=program_files_root,
    )
    hid_elevation_windows.ensure_protected_directory(
        destination,
        user_sid=sid,
        trusted_root=program_files_root,
        security_root=owner_root,
        read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
    )
    hid_elevation_windows.assert_no_reparse_points(
        destination, trusted_root=program_files_root
    )
    dll_path = destination / GADGET_DLL_NAME
    if not dll_path.is_file() or sha256_file(dll_path) != GADGET_DLL_SHA256:
        temporary = dll_path.with_suffix(f".dll.{os.getpid()}.tmp")
        try:
            with lzma.open(archive, "rb") as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            dll_hash = sha256_file(temporary)
            if dll_hash != GADGET_DLL_SHA256:
                raise _runtime_validation_error("runtime_dll_hash_mismatch")
            hid_elevation_windows._apply_path_security(
                temporary,
                user_sid=sid,
                directory=False,
                read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
            )
            os.replace(temporary, dll_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    hid_elevation_windows._apply_path_security(
        dll_path,
        user_sid=sid,
        directory=False,
        read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
    )
    _write_verified_text(
        destination / GADGET_CONFIG_NAME,
        gadget_config_text(),
        user_sid=sid,
    )
    _write_verified_text(
        destination / GADGET_SCRIPT_NAME,
        GADGET_SCRIPT,
        user_sid=sid,
    )
    for runtime_directory in (owner_root, destination):
        hid_elevation_windows.assert_no_reparse_points(
            runtime_directory, trusted_root=program_files_root
        )
        if not hid_elevation_windows.validate_path_security_sddl(
            hid_elevation_windows._read_path_security_sddl(runtime_directory),
            user_sid=sid,
            directory=True,
            read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
        ):
            raise _runtime_validation_error("runtime_directory_acl_invalid")
    for runtime_file in (
        dll_path,
        destination / GADGET_CONFIG_NAME,
        destination / GADGET_SCRIPT_NAME,
    ):
        hid_elevation_windows.assert_no_reparse_points(
            runtime_file, trusted_root=program_files_root
        )
        if not hid_elevation_windows.validate_path_security_sddl(
            hid_elevation_windows._read_path_security_sddl(runtime_file),
            user_sid=sid,
            directory=False,
            read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
        ):
            raise _runtime_validation_error("runtime_file_acl_invalid")
    return dll_path


def find_rc003_hidogatt_host_pid(*, diagnostic: dict | None = None,
                               selected_key: str | None = None) -> int | None:
    """Locate the WUDFHost assigned to the paired RC003 HID service."""

    matched_services = 0
    lookup_error = {}
    selected_pids = set()

    def result(pid: int | None = None, reason: str = "host_pid_unavailable") -> int | None:
        if diagnostic is not None:
            diagnostic.update(reason=reason, matched_services=matched_services)
            if pid is None and lookup_error:
                diagnostic.update(lookup_error)
        return pid

    if os.name != "nt" or winreg is None:
        return result(reason="registry_unavailable")
    if selected_key is not None:
        from . import remote_selection
        if not remote_selection.valid_key(selected_key):
            return result(reason="no_selected_device")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BTHLE_ENUM_KEY) as root:
            service_index = 0
            while True:
                try:
                    service_name = winreg.EnumKey(root, service_index)
                except OSError as exc:
                    if getattr(exc, "winerror", None) != 259:
                        lookup_error = _registry_error_details(exc)
                    break
                service_index += 1
                folded = service_name.casefold()
                if not folded.startswith(HID_SERVICE_PREFIX):
                    continue
                if RC003_HARDWARE_TOKEN not in folded:
                    continue
                matched_services += 1
                with winreg.OpenKey(root, service_name) as service_key:
                    instance_index = 0
                    while True:
                        try:
                            instance_name = winreg.EnumKey(service_key, instance_index)
                        except OSError as exc:
                            if getattr(exc, "winerror", None) != 259:
                                lookup_error = _registry_error_details(exc)
                            break
                        instance_index += 1
                        if selected_key is not None:
                            try:
                                with winreg.OpenKey(service_key, instance_name) as instance_key:
                                    container, _ = winreg.QueryValueEx(instance_key, "ContainerID")
                                if remote_selection.container_key(container) != selected_key:
                                    continue
                            except (OSError, ValueError):
                                continue
                        diagnostic_path = (
                            f"{service_name}\\{instance_name}\\{WUDF_DIAGNOSTIC_SUFFIX}"
                        )
                        try:
                            with winreg.OpenKey(root, diagnostic_path) as diagnostic_key:
                                value, _ = winreg.QueryValueEx(diagnostic_key, "HostPid")
                            pid = int(value)
                            if pid > 0:
                                if selected_key is not None:
                                    selected_pids.add(pid)
                                    continue
                                return result(pid, "host_found")
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            lookup_error = _registry_error_details(exc)
                            continue
                        except (TypeError, ValueError):
                            lookup_error = {"reason": "host_pid_invalid"}
                            continue
    except OSError as exc:
        lookup_error = _registry_error_details(exc)
    if selected_key is not None and not lookup_error and len(selected_pids) == 1:
        return result(next(iter(selected_pids)), "selected_host_found")
    return result(reason="host_pid_unavailable" if matched_services else "rc003_service_not_found")


def _registry_error_details(error: OSError) -> dict:
    return {
        "reason": "registry_access_denied" if isinstance(error, PermissionError) or getattr(error, "winerror", None) == 5
        else "registry_read_failed",
        "error_type": type(error).__name__, "winerror": getattr(error, "winerror", None), "errno": error.errno,
    }


def rc003_hidogatt_host_is_exclusive(pid: int, *, diagnostic: dict | None = None,
                                   selected_key: str | None = None) -> bool:
    """Only bind an observed live control handle while its host owns RC003 alone."""

    matches = []

    def result(reason: str, *, complete: bool = False, error: OSError | None = None) -> bool:
        if diagnostic is not None:
            diagnostic.update(
                reason=reason, scan_complete=complete, host_members=len(matches),
                rc003_members=matches.count(True),
            )
            if error is not None:
                diagnostic.update(_registry_error_details(error))
        return reason == "exclusive_rc003_host"

    if winreg is None:
        return result("registry_unavailable")
    lookup_details = {}
    current_pid = find_rc003_hidogatt_host_pid(diagnostic=lookup_details, selected_key=selected_key)
    if current_pid != pid:
        verified = result("host_changed" if current_pid is not None else "host_unavailable_unresolved")
        if diagnostic is not None and lookup_details:
            diagnostic["host_lookup"] = lookup_details
        return verified
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum") as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                enumerator = winreg.EnumKey(root, i)
                with winreg.OpenKey(root, enumerator) as enum_key:
                    for j in range(winreg.QueryInfoKey(enum_key)[0]):
                        device = winreg.EnumKey(enum_key, j)
                        with winreg.OpenKey(enum_key, device) as device_key:
                            for k in range(winreg.QueryInfoKey(device_key)[0]):
                                instance = winreg.EnumKey(device_key, k)
                                try:
                                    with winreg.OpenKey(
                                        device_key, f"{instance}\\{WUDF_DIAGNOSTIC_SUFFIX}"
                                    ) as diagnostic_key:
                                        host_pid, _ = winreg.QueryValueEx(diagnostic_key, "HostPid")
                                except FileNotFoundError:
                                    continue
                                if host_pid == pid:
                                    matches.append(
                                        enumerator.casefold() == "bthledevice"
                                        and device.casefold().startswith(HID_SERVICE_PREFIX)
                                        and RC003_HARDWARE_TOKEN in device.casefold()
                                    )
    except OSError as exc:
        return result("registry_access_denied" if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5
                      else "registry_read_failed", error=exc)
    if matches == [True]:
        return result("exclusive_rc003_host", complete=True)
    if len(matches) > 1:
        return result("shared_host", complete=True)
    return result("rc003_membership_not_confirmed", complete=True)
