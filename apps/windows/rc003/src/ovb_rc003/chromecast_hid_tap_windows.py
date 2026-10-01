"""Read selected Chromecast HID notifications when BTHPORT omits report bytes.

The callback is private Windows code. A matching module hash or matching public
PDB is required before attaching. An adapter address match is required before
any report is delivered; this module never modifies the Windows report.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from contextvars import ContextVar
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import threading
import time
import uuid

from . import remote_layout, remote_selection
from .chromecast_observation import HID_COUNTS, HID_ERROR_TYPES, valid_record


MODULE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "umdf" / "microsoft.bluetooth.profiles.hidovergatt.dll"
# Exact x64 driver contents, not the (often unchanged) FileVersion string.
# Values are (OnReportValueChanged RVA, Bluetooth address offset in this).
_KNOWN_LAYOUTS = {
    "8a18571ddfa447bd590354ccdf7800b815a098806359ab195d395158f9d07cc5": (0x12FCC, 24),
    "de2b7ff2a61d50473cfe1e00ec012e99bda9361d563249ef93dce59f576e9936": (0x1580C, 8),
    "372c3628e3366c18152199eb8fb554d27bb4f02d1356b1f514f711328df4a8d6": (0x157FC, 8),
}
_HID_SERVICE = "{00001812-0000-1000-8000-00805f9b34fb}"
_GOOGLE_HARDWARE = (
    "dev_vid&0118d1_pid&9450_rev&0110",
    "dev_vid&0218d1_pid&9450_rev&011b",
)
_ADDRESS_SUFFIX = re.compile(r"_([0-9a-f]{12})$", re.I)
_SYMBOL = "Microsoft::Bluetooth::Profiles::HidOverGatt::HogpGattAdapter::OnReportValueChanged"
_CONSTRUCTOR = "Microsoft::Bluetooth::Profiles::HidOverGatt::HogpGattAdapter::HogpGattAdapter"


class TapError(RuntimeError):
    def __init__(self, message, *, detail="none", native_code=-1):
        super().__init__(message)
        self.detail = detail
        if native_code >= 0:
            self.winerror = native_code


_startup_sink = ContextVar("chromecast_hid_startup_sink", default=None)
_STARTUP_LIMIT = 128


def startup_record(step, state, *, started=None, error=None, script_line=-1, host_pid=-1,
                   reason="none", details=None):
    """Fixed metadata only; usable before a HID tap exists as well as in its thread."""
    def error_type(value):
        name = type(value).__name__ if value is not None else "none"
        return name if name in HID_ERROR_TYPES else "other"

    def code(value):
        for name in ("winerror", "hresult", "errno"):
            number = getattr(value, name, None)
            if type(number) is int:
                return number & 0xffffffff
        return -1

    try:
        cause = getattr(error, "__cause__", None) or (None if getattr(error, '__suppress_context__', False)
                                                     else getattr(error, '__context__', None))
        row = dict(kind="hid_startup", elapsed_ms=0, step=step, state=state,
            duration_ms=0 if started is None else min(2147483647, max(0, round((time.monotonic() - started) * 1000))),
            error_type=error_type(error), cause_type=error_type(cause), native_code=code(error), cause_code=code(cause),
            host_pid=host_pid if type(host_pid) is int else -1,
            script_line=script_line if type(script_line) is int and 0 <= script_line <= 65535 else -1,
            reason=getattr(error, "detail", reason), details=dict(details or {}))
        return row if valid_record(row) else None
    except Exception:
        return None


@contextmanager
def _trace_step(name):
    # Context is local to this startup thread. Runtime source polling stays quiet.
    sink = _startup_sink.get()
    started = time.monotonic()
    details = {}
    def emit(state, error=None):
        if sink is not None:
            try:
                sink(name, state, started=started, error=error, details=details)
            except Exception:
                pass
    emit("begin")
    try:
        yield details
    except Exception as error:
        emit("failed", error)
        raise
    else:
        emit("success")


def selected_host_and_address(entity: str) -> tuple[int, str]:
    with _trace_step("host_scan") as details:
        return _selected_host_and_address(entity, details)


def _selected_host_and_address(entity, details):
    """Resolve exactly one selected Google HID service from Windows PnP."""
    import winreg

    if not remote_selection.valid_key(entity):
        raise TapError("hid_source_unconfirmed", detail="invalid_entity")
    found = set()
    details.update(hardware_matches=0, selected_matches=0, invalid_address=0, missing_container=0, invalid_pid=0)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice") as root:
            count = winreg.QueryInfoKey(root)[0]
            details["registry_nodes"] = count
            for index in range(count):
                name = winreg.EnumKey(root, index)
                folded = name.casefold()
                if not folded.startswith(_HID_SERVICE) or not any(hardware in folded for hardware in _GOOGLE_HARDWARE):
                    continue
                details["hardware_matches"] += 1
                address = _ADDRESS_SUFFIX.search(name)
                if address is None:
                    details["invalid_address"] += 1
                    continue
                with winreg.OpenKey(root, name) as service:
                    for member in range(winreg.QueryInfoKey(service)[0]):
                        instance = winreg.EnumKey(service, member)
                        with winreg.OpenKey(service, instance) as key:
                            try:
                                container, _ = winreg.QueryValueEx(key, "ContainerID")
                            except OSError:
                                details["missing_container"] += 1
                                continue
                            if remote_selection.container_key(container) != entity:
                                continue
                            details["selected_matches"] += 1
                            with winreg.OpenKey(key, r"Device Parameters\WUDFDiagnosticInfo") as info:
                                pid, _ = winreg.QueryValueEx(info, "HostPid")
                            if type(pid) is int and pid > 0:
                                found.add((pid, address.group(1).lower()))
                            else:
                                details["invalid_pid"] += 1
    except (OSError, ValueError) as exc:
        raise TapError("hid_source_unconfirmed", detail="registry_read") from exc
    details["host_candidates"] = len(found)
    if len(found) != 1:
        raise TapError("hid_source_unconfirmed", detail="no_selected_host" if not found else "ambiguous_host")
    return found.pop()


def _pdb_identity(path: Path) -> tuple[str, str]:
    with _trace_step("pdb_identity") as details:
        return _read_pdb_identity(path, details)


def _read_pdb_identity(path, details):
    import pefile

    image = pefile.PE(str(path), fast_load=False)
    try:
        entries = [entry for entry in image.DIRECTORY_ENTRY_DEBUG if entry.struct.Type == 2]
        details["debug_entries"] = len(entries)
        if len(entries) != 1:
            raise TapError("hid_symbol_unavailable", detail="debug_entries")
        entry = entries[0]
        with path.open("rb") as stream:
            stream.seek(entry.struct.PointerToRawData)
            record = stream.read(entry.struct.SizeOfData)
        if record[:4] != b"RSDS" or len(record) < 28:
            raise TapError("hid_symbol_unavailable", detail="debug_signature")
        name = record[24:].split(b"\0", 1)[0].decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.pdb", name, re.I):
            raise TapError("hid_symbol_unavailable", detail="pdb_name")
        index = uuid.UUID(bytes_le=record[4:20]).hex.upper() + f"{struct.unpack_from('<I', record, 20)[0]:X}"
        details["pdb_key"] = index
        return name, index
    finally:
        image.close()


def _symbol_cache() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise TapError("hid_symbol_unavailable", detail="cache_root")
    return Path(root) / "RemoteMic" / "HidSymbols"


def _ensure_pdb(module: Path) -> Path:
    name, index = _pdb_identity(module)
    with _trace_step("cache_lookup") as details:
        target = _symbol_cache() / name / index / name
        present = target.is_file()
        details["cache_present"] = int(present)
        details["cache_size"] = target.stat().st_size if present else -1
        cached = target.read_bytes() if present and details["cache_size"] <= 2_097_152 else b""
        valid = present and details["cache_size"] <= 2_097_152 and cached[:32].startswith(b"Microsoft C/C++ MSF 7.00")
        if cached:
            details["pdb_sha256"] = hashlib.sha256(cached).hexdigest()
        details["cache_valid"] = int(valid)
        if valid:
            return target
    with _trace_step("cache_directory"):
        target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{name}.{os.getpid()}.{threading.get_ident()}.tmp")
    url = f"https://msdl.microsoft.com/download/symbols/{name}/{index}/{name}"
    curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    try:
        with _trace_step("pdb_download") as details:
            result = subprocess.run(
                [str(curl), "--fail", "--location", "--silent", "--show-error", "--max-time", "15",
                 "--max-filesize", "2097152", "--write-out", "%{http_code}", "--output", str(temporary), url],
                capture_output=True, timeout=18, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            details["return_code"] = result.returncode & 0xffffffff
            status = result.stdout.strip()
            details["http_status"] = int(status) if re.fullmatch(rb"[0-9]{3}", status) else -1
            if result.returncode:
                raise TapError("hid_symbol_unavailable", detail="download_exit")
        with _trace_step("download_validate") as details:
            details["size"] = temporary.stat().st_size if temporary.is_file() else -1
            if not 100_000 <= details["size"] <= 2_097_152:
                raise TapError("hid_symbol_unavailable", detail="download_size")
            downloaded = temporary.read_bytes()
            details["pdb_sha256"] = hashlib.sha256(downloaded).hexdigest()
            if not downloaded[:32].startswith(b"Microsoft C/C++ MSF 7.00"):
                raise TapError("hid_symbol_unavailable", detail="download_signature")
        with _trace_step("cache_commit"):
            os.replace(temporary, target)
        return target
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TapError("hid_symbol_unavailable") from exc
    finally:
        with _trace_step("cache_cleanup"):
            temporary.unlink(missing_ok=True)


class _SymbolInfo(ctypes.Structure):
    _fields_ = [("SizeOfStruct", wintypes.ULONG), ("TypeIndex", wintypes.ULONG),
                ("Reserved", ctypes.c_ulonglong * 2), ("Index", wintypes.ULONG),
                ("Size", wintypes.ULONG), ("ModBase", ctypes.c_ulonglong),
                ("Flags", wintypes.ULONG), ("Value", ctypes.c_ulonglong),
                ("Address", ctypes.c_ulonglong), ("Register", wintypes.ULONG),
                ("Scope", wintypes.ULONG), ("Tag", wintypes.ULONG),
                ("NameLen", wintypes.ULONG), ("MaxNameLen", wintypes.ULONG),
                ("Name", wintypes.WCHAR * 1)]


def _resolve_pdb_symbols(module: Path) -> tuple[int, int]:
    _ensure_pdb(module)
    with _trace_step("dbghelp_load"):
        dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel32.GetCurrentProcess()
    dbghelp.SymInitializeW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.BOOL)
    dbghelp.SymInitializeW.restype = wintypes.BOOL
    dbghelp.SymLoadModuleExW.argtypes = (wintypes.HANDLE, wintypes.HANDLE, wintypes.LPCWSTR,
        wintypes.LPCWSTR, ctypes.c_ulonglong, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD)
    dbghelp.SymLoadModuleExW.restype = ctypes.c_ulonglong
    dbghelp.SymEnumSymbolsW.argtypes = (wintypes.HANDLE, ctypes.c_ulonglong, wintypes.LPCWSTR,
                                       ctypes.c_void_p, wintypes.LPVOID)
    dbghelp.SymEnumSymbolsW.restype = wintypes.BOOL
    dbghelp.SymCleanup.argtypes = (wintypes.HANDLE,)
    dbghelp.SymCleanup.restype = wintypes.BOOL
    with _trace_step("symbol_initialize"):
        ctypes.set_last_error(0)
        if not dbghelp.SymInitializeW(process, str(_symbol_cache()), False):
            raise TapError("hid_symbol_unavailable", detail="symbol_initialize_failed", native_code=ctypes.get_last_error())
    try:
        with _trace_step("symbol_load"):
            ctypes.set_last_error(0)
            base = dbghelp.SymLoadModuleExW(process, None, str(module), None, 0, 0, None, 0)
            if not base:
                raise TapError("hid_symbol_unavailable", detail="symbol_load_failed", native_code=ctypes.get_last_error())
        matches = {_SYMBOL: [], _CONSTRUCTOR: []}
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.POINTER(_SymbolInfo), wintypes.ULONG, wintypes.LPVOID)

        def collect(pointer, _size, _context):
            item = pointer.contents
            name = ctypes.wstring_at(ctypes.addressof(item) + _SymbolInfo.Name.offset, item.NameLen).rstrip("\0")
            if name in matches:
                matches[name].append(item.Address - base)
            return True

        callback = callback_type(collect)
        with _trace_step("symbol_enumerate") as details:
            ctypes.set_last_error(0)
            success = dbghelp.SymEnumSymbolsW(process, base, "*", callback, None)
            code = ctypes.get_last_error()
            details.update(callback_matches=len(matches[_SYMBOL]), constructor_matches=len(matches[_CONSTRUCTOR]))
            if not success:
                raise TapError("hid_symbol_unavailable", detail="symbol_enumerate_failed", native_code=code)
        with _trace_step("symbol_match") as details:
            details.update(callback_matches=len(matches[_SYMBOL]), constructor_matches=len(matches[_CONSTRUCTOR]))
            if any(len(items) != 1 for items in matches.values()):
                raise TapError("hid_symbol_unavailable", detail="symbol_missing_or_ambiguous")
            details.update(callback_rva=matches[_SYMBOL][0], constructor_rva=matches[_CONSTRUCTOR][0])
        return matches[_SYMBOL][0], matches[_CONSTRUCTOR][0]
    finally:
        # This API's failure was nonfatal before; observe its result without
        # turning a successful layout lookup into a different product behavior.
        with _trace_step("symbol_cleanup") as details:
            ctypes.set_last_error(0)
            details["native_result"] = int(bool(dbghelp.SymCleanup(process)))
            details["native_code"] = ctypes.get_last_error() if not details["native_result"] else -1


def _constructor_address_offset(code: bytes) -> int:
    """Recognize complete verified x64 prologues before RCX/RDX can change.

    The constructor's first argument (RDX) is the Bluetooth address. Only the
    RIP-relative vtable displacement may vary; never scan object memory or
    search arbitrary instructions for a coincidental address/store.
    """
    prefix = bytes.fromhex(
        "4c894c2420 48894c2408 53 55 56 57 4154 4156 4157 4883ec50 "
        "4d8bf1 410fb7e8 488bf9"
    )
    for offset, initialize in ((24, bytes.fromhex("4533e4 4c896108 4c896110")), (8, b"")):
        start = prefix + initialize + bytes.fromhex("488d05")
        end = bytes.fromhex("488901 488951") + bytes([offset])
        if code.startswith(start) and code[len(start) + 4:len(start) + 4 + len(end)] == end:
            return offset
    raise TapError("hid_symbol_unavailable", detail="constructor_unrecognized")


def _driver_layout(module: Path = MODULE) -> tuple[int, int]:
    with _trace_step("driver_read") as details:
        data = module.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        known = _KNOWN_LAYOUTS.get(digest)
        details.update(driver_sha256=digest, size=len(data), known_layout=int(known is not None))
    constructor = None
    if known is None:
        rva, constructor = _resolve_pdb_symbols(module)
    else:
        rva, address_offset = known
    import pefile

    with _trace_step("driver_pe"):
        image = pefile.PE(str(module), fast_load=True)
    try:
        with _trace_step("driver_arch") as details:
            details["machine"] = image.FILE_HEADER.Machine
            if image.FILE_HEADER.Machine != 0x8664:
                raise TapError("hid_symbol_unavailable", detail="unsupported_architecture")
        for location, size in ((rva, 1),) + (((constructor, 64),) if constructor is not None else ()):
            with _trace_step("callback_range" if size == 1 else "constructor_range") as details:
                details["callback_rva" if size == 1 else "constructor_rva"] = location
                executable = [section for section in image.sections if section.Characteristics & 0x20000000
                              and section.VirtualAddress <= location
                              and location + size <= section.VirtualAddress + min(section.Misc_VirtualSize, section.SizeOfRawData)]
                details["executable_sections"] = len(executable)
                if len(executable) != 1:
                    raise TapError("hid_symbol_unavailable", detail="nonexecutable_rva")
        if constructor is not None:
            with _trace_step("constructor_layout") as details:
                code = image.get_data(constructor, 64)
                details["size"] = len(code)
                details["constructor_prefix"] = code.hex()
                address_offset = _constructor_address_offset(code)
                details["address_offset"] = address_offset
    finally:
        image.close()
    return rva, address_offset


_SCRIPT = r"""
const expectedPath = __PATH__;
const expectedAddress = __ADDRESS__;
const consumerCodes = __CONSUMER_CODES__;
const copyIoctl = 0x80018483;
const module = Process.findModuleByName("microsoft.bluetooth.profiles.hidovergatt.dll");
function setupEvidence(step, state, reason, details) {
  try { send({kind:"hid_setup", step, state, reason, details}); } catch (_) {}
}
const pathMatches = module !== null && module.path.toLowerCase().replace(/\\/g, "/") === expectedPath;
setupEvidence("runtime_module", module !== null && pathMatches ? "success" : "failed",
  module === null ? "module_missing" : pathMatches ? "none" : "module_path_mismatch", {
  module_present:module === null ? 0 : 1,
  module_path_match:pathMatches ? 1 : 0,
  pointer_size:Process.pointerSize, loaded_size:module === null ? -1 : module.size
});
if (module === null || module.path.toLowerCase().replace(/\\/g, "/") !== expectedPath)
  throw new Error("module_mismatch");
const callback = module.base.add(__RVA__);
const iid = Memory.alloc(16);
iid.writeByteArray([0xef,0x0f,0x5a,0x90,0x53,0xbc,0xdf,0x11,0x8c,0x49,0x00,0x1e,0x4f,0xc6,0x86,0xda]);
let addressSeen = false;
// Cumulative metadata only; no report content or device addresses in diagnostics.
const counts = Object.fromEntries(__COUNTS__.map(name => [name, 0]));
const lengths = new Map();
const copyLengths = new Map();
let copyHookState = "unavailable";
const started = Date.now();
let errorStep = "none", lastErrorStep = "none", errorDiagnosticsSent = false;
function count(name) { counts[name] = Math.min(2147483647, counts[name] + 1); }
function emitDiagnostics() {
  try {
    send({kind:"hid_flow", elapsed_ms:0,
      sample_ms:Math.max(0, Math.min(2147483647, Date.now() - started)),
      counts:{...counts}, lengths:Array.from(lengths), copy_buffer_lengths:Array.from(copyLengths),
      copy_hook_state:copyHookState, copy_scope:"selected_host_unattributed",
      last_error_step:lastErrorStep});
  } catch (_) { /* Diagnostic delivery must never alter input handling. */ }
}
function method(object, slot, result, args) {
  return new NativeFunction(object.readPointer().add(slot * Process.pointerSize).readPointer(), result, args);
}
function addressMatches(adapter) {
  const bytes = new Uint8Array(adapter.add(__ADDRESS_OFFSET__).readByteArray(6));
  for (let i = 0; i < 6; i++) if (bytes[i] !== expectedAddress[i]) return false;
  return true;
}
function report(buffer) {
  let access = ptr(0);
  try {
    if (buffer.isNull()) { count("buffer_null"); return null; }
    errorStep = "length";
    const sizeOut = Memory.alloc(4);
    if (method(buffer, 7, "int", ["pointer", "pointer"])(buffer, sizeOut) !== 0) {
      count("length_error"); return null;
    }
    const size = sizeOut.readU32();
    if (lengths.has(size)) lengths.set(size, Math.min(2147483647, lengths.get(size) + 1));
    else if (lengths.size < 16) lengths.set(size, 1);
    else count("lengths_overflow");
    if (size !== 8 && size !== 2) { count("length_rejected"); return null; }
    errorStep = "access";
    const accessOut = Memory.alloc(Process.pointerSize);
    if (method(buffer, 0, "int", ["pointer", "pointer", "pointer"])(buffer, iid, accessOut) !== 0) {
      count("access_error"); return null;
    }
    access = accessOut.readPointer();
    errorStep = "data";
    const bytesOut = Memory.alloc(Process.pointerSize);
    if (method(access, 3, "int", ["pointer", "pointer"])(access, bytesOut) !== 0) {
      count("data_error"); return null;
    }
    const bytes = new Uint8Array(bytesOut.readPointer().readByteArray(size));
    if (size === 2) {
      // GATT omits the 0x02 report ID added by the downstream Windows copy.
      // Normalize only known Consumer usages from the selected adapter.
      const usage = bytes[0] | (bytes[1] << 8);
      const code = usage === 0 ? 0 : consumerCodes[usage];
      if (code === undefined) { count("code_rejected"); return null; }
      return new Uint8Array([code, 0, 0, 0, 0, 0, 0, 0]);
    }
    return bytes;
  } finally {
    if (!access.isNull()) {
      const previousStep = errorStep;
      errorStep = "release";
      method(access, 2, "uint32", ["pointer"])(access);
      errorStep = previousStep;
    }
  }
}
// Observe the downstream HID copy used by vibe-mote. This host can also serve
// other devices, so these counts are never treated as selected-device reports.
// This private UMDF copy can consume the output parameter as a source buffer.
// IO_STATUS_BLOCK.Information is not its report length (observed as zero).
// Match vibe-mote's successful three-byte buffer observation; never edit it.
function installCopyObservation() {
  try {
    const ntdll = Process.findModuleByName("ntdll.dll");
    const target = ntdll ? ntdll.findExportByName("NtDeviceIoControlFile") : null;
    if (target === null) { count("copy_hook_error"); return; }
    Interceptor.attach(target, {
      onEnter(args) {
        this.copy = false;
        try {
          if (args[5].toUInt32() !== copyIoctl) return;
          this.output = args[8];
          this.capacity = args[9].toUInt32();
          this.copy = true;
          count("copy_calls");
          if (this.capacity === 3) count("copy_capacity_three");
        } catch (_) { count("copy_read_error"); }
      },
      onLeave(retval) {
        if (!this.copy) return;
        try {
          const result = retval.toUInt32();
          if (result === 0x103) { count("copy_pending"); return; }
          if (result !== 0) { count("copy_failed"); return; }
          count("copy_success");
          const size = this.capacity;
          if (copyLengths.has(size))
            copyLengths.set(size, Math.min(2147483647, copyLengths.get(size) + 1));
          else if (copyLengths.size < 16) copyLengths.set(size, 1);
          else count("copy_lengths_overflow");
          if (size !== 3) return;
          if (this.output.isNull()) { count("copy_read_error"); return; }
          const bytes = new Uint8Array(this.output.readByteArray(3));
          if (bytes.length !== 3) { count("copy_read_error"); return; }
          count("copy_read_three");
          if (bytes[0] !== 0x02) return;
          count("copy_report_two");
          const usage = bytes[1] | (bytes[2] << 8);
          if (usage === 0) count("copy_release");
          else if (usage === 0x42) count("copy_up");
          else if (usage === 0x43) count("copy_down");
          else if (usage === 0x44) count("copy_left");
          else if (usage === 0x45) count("copy_right");
          else if (usage === 0x41) count("copy_ok");
          else if (consumerCodes[usage] !== undefined)
            count("copy_known_other");
          else count("copy_other_usage");
        } catch (_) { count("copy_read_error"); }
      }
    });
    copyHookState = "ready";
  } catch (_) { count("copy_hook_error"); }
}
installCopyObservation();
setupEvidence("runtime_hook", "begin", "none", {});
try {
Interceptor.attach(callback, {onEnter(args) {
  try {
    count("callbacks");
    errorStep = "address";
    if (!addressMatches(args[0])) { count("address_mismatch"); return; }
    count("address_match");
    addressSeen = true;
    const bytes = report(args[2]);
    if (bytes === null) return;
    if (!bytes.slice(1).every(value => value === 0)) { count("tail_rejected"); return; }
    if (!(bytes[0] === 0 || [1,3,4,5,6,7,8,10,11,12,13,14,15,17].includes(bytes[0]))) {
      count("code_rejected"); return;
    }
    // The sender interface pointer changes between reports on this driver.
    // Bind by the selected adapter address, never by that transient pointer.
    errorStep = "send";
    send({kind:"report", value:Array.from(bytes).map(x => x.toString(16).padStart(2,"0")).join("")});
    count("accepted");
  } catch (_) {
    count("callback_error");
    lastErrorStep = errorStep;
    if (!errorDiagnosticsSent) {
      emitDiagnostics();
      errorDiagnosticsSent = true;
    }
    if (addressSeen) send({kind:"error", reason:"hid_callback_failed"});
  }
}});
} catch (error) {
  setupEvidence("runtime_hook", "failed", "hook_exception", {});
  throw error;
}
setupEvidence("runtime_hook", "success", "none", {});
send({kind:"hook_ready"});
emitDiagnostics();
setInterval(emitDiagnostics, 2000);
"""


class HidTap:
    def __init__(self, entity: str):
        self.entity = entity
        self.events = queue.Queue(maxsize=256)
        self.overflow = threading.Event()
        self.session = self.script = None
        self._gadget_device = None
        self.pid = self.address = None
        self._diagnostic_lock = threading.Lock()
        self._diagnostic = None
        self._startup = queue.Queue(maxsize=_STARTUP_LIMIT)
        self._startup_lock = threading.Lock()
        self._startup_dropped = 0
        self._startup_rejected = 0
        self._script_error_recorded = False
        self._startup_cleanup_ok = True
        self._abort_start = threading.Event()

    def abort_start(self):
        self._abort_start.set()

    def _startup_record(self, step, state, **fields):
        try:
            row = startup_record(step, state, host_pid=self.pid, **fields)
            if row is not None:
                with self._startup_lock:
                    if self._startup.full():
                        # Keep the latest failure and explicitly report loss.
                        self._startup.get_nowait()
                        self._startup_dropped = min(2147483647, self._startup_dropped + 1)
                    self._startup.put_nowait(row)
            else:
                with self._startup_lock:
                    self._startup_rejected = min(2147483647, self._startup_rejected + 1)
        except Exception:
            pass  # Evidence cannot alter startup or resource cleanup.

    @contextmanager
    def _step(self, name):
        started = time.monotonic()
        details = {}
        self._startup_record(name, "begin")
        try:
            yield details
        except Exception as error:
            self._startup_record(name, "failed", started=started, error=error, details=details)
            raise
        else:
            self._startup_record(name, "success", started=started, details=details)

    def poll_startup(self):
        rows = []
        with self._startup_lock:
            if self._startup_dropped:
                rows.append(startup_record("evidence_overflow", "success", reason="records_dropped",
                                           details={"dropped": self._startup_dropped}))
                self._startup_dropped = 0
            if self._startup_rejected:
                rows.append(startup_record('evidence_overflow', 'success', reason='records_rejected',
                                           details={'dropped': self._startup_rejected}))
                self._startup_rejected = 0
            for _ in range(_STARTUP_LIMIT):
                try:
                    rows.append(self._startup.get_nowait())
                except queue.Empty:
                    break
        return rows

    def _store_diagnostic(self, item):
        if valid_record(item) and item.get("kind") == "hid_flow":
            # One replaceable slot, independent of the press/release queue.
            with self._diagnostic_lock:
                self._diagnostic = item

    def _put(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            # A dropped release is unsafe. Discard the backlog and stop.
            self.overflow.set()

    def start(self):
        token = _startup_sink.set(self._startup_record)
        try:
            return self._start()
        finally:
            _startup_sink.reset(token)

    def _start(self):
        with self._step("import_frida"):
            import frida
        with self._step("selected_host"):
            pid, address = selected_host_and_address(self.entity)
            self.pid, self.address = pid, address
        with self._step("driver_layout") as details:
            rva, address_offset = _driver_layout()
            details.update(callback_rva=rva, address_offset=address_offset)
        with self._step("script_prepare"):
            source = (_SCRIPT.replace("__PATH__", json.dumps(str(MODULE).lower().replace("\\", "/")))
                 .replace("__ADDRESS__", json.dumps(list(bytes.fromhex(address)[::-1])))
                 .replace("__ADDRESS_OFFSET__", str(address_offset))
                 .replace("__RVA__", str(rva))
                 .replace("__CONSUMER_CODES__", json.dumps({usage: remote_layout.CHROMECAST_REPORT_CODES[key]
                     for key, usage in remote_layout.CHROMECAST_CONSUMER_USAGES.items()}))
                 .replace("__COUNTS__", json.dumps(sorted(HID_COUNTS))))
        try:
            with self._step("attach"):
                self.session = self._attach(frida, pid)
                self.session.on("detached", self._detached)
            with self._step("create_script"):
                self.script = self.session.create_script(source)

            def on_message(message, _data):
                import time
                if message.get("type") != "send":
                    if not self._script_error_recorded:
                        self._script_error_recorded = True
                        from .chromecast_attach_diagnostics_windows import exception_details
                        self._startup_record("script_message", "failed", script_line=message.get("lineNumber", -1),
                            details=exception_details(RuntimeError(str(message.get('description', '')))))
                    self._put(("error", "hid_callback_failed", 0.0))
                    return
                item = message.get("payload", {})
                if item.get("kind") == "hid_setup":
                    if item.get("step") in ("runtime_module", "runtime_hook"):
                        self._startup_record(item["step"], item.get("state"), reason=item.get("reason"),
                                             details=item.get("details"))
                    return
                if item.get("kind") == "hid_flow":
                    self._store_diagnostic(item)
                    return
                if item.get("kind") == "report":
                    self._put(("report", item.get("value"), time.monotonic()))
                elif item.get("kind") == "hook_ready":
                    self._put(("hook_ready", "", time.monotonic()))
                elif item.get("kind") == "error":
                    self._put(("error", item.get("reason", "hid_callback_failed"), 0.0))

            with self._step("load_script"):
                self.script.on("message", on_message)
                self.script.load()
            with self._step("verify_source"):
                if selected_host_and_address(self.entity) != (pid, address):
                    raise TapError("hid_source_unconfirmed")
        except Exception:
            self._startup_cleanup_ok = self.close(startup=True)
            raise

    def _attach(self, frida, pid):
        from . import chromecast_gadget_windows as gadget

        if self._abort_start.is_set():
            raise TapError("hid_source_unconfirmed")
        # Reuse Gadget only when this exact host already loaded it or has an
        # uncertain injection. A new host may still succeed through direct Frida.
        selected_address = isinstance(self.address, str) and re.fullmatch(r"[0-9a-f]{12}", self.address)
        if selected_address and gadget.loaded_or_pending_for_host(pid):
            return self._attach_gadget(frida, gadget, pid)
        evidence = None
        try:
            from .chromecast_attach_diagnostics_windows import BoundedAttachEvidence
            evidence = BoundedAttachEvidence(pid, self._startup_record)
        except Exception as error:
            self._startup_record('attach_evidence', 'failed', error=error)
        def sample(operation):
            if evidence is None:
                return
            try:
                operation()
            except Exception as error:
                self._startup_record('attach_evidence', 'failed', error=error)
        if evidence is not None:
            sample(evidence.begin)
        error = None
        session = None
        try:
            session = frida.attach(pid)
        except Exception as caught:
            error = caught
        finally:
            if evidence is not None:
                sample(lambda: evidence.finish(error))
        if error is None:
            return session
        nonresponding = getattr(frida, "ProcessNotRespondingError", None)
        if selected_address and isinstance(nonresponding, type) and isinstance(error, nonresponding):
            if self._abort_start.is_set():
                raise TapError("hid_source_unconfirmed") from error
            return self._attach_gadget(frida, gadget, pid)
        raise error

    def _attach_gadget(self, frida, gadget, pid):
        with self._step("gadget_fallback"):
            if self._abort_start.is_set():
                raise gadget.GadgetAttachError("gadget_cancelled")
            session, device, device_address = gadget.attach(
                frida, pid, self.address,
                lambda: selected_host_and_address(self.entity),
                cancelled=self._abort_start.is_set,
            )
            self._gadget_device = (device, device_address)
            return session

    def _detached(self, reason=None, crash=None):
        from .chromecast_observation import HID_DETAIL_CHOICES
        reason = reason if reason in HID_DETAIL_CHOICES['detach_reason'] else 'unknown'
        self._startup_record('session_detached', 'success',
                             details={'detach_reason': reason, 'crash_present': int(crash is not None)})
        self._put(("error", "hid_host_detached", 0.0))

    def poll(self):
        result = []
        if self.overflow.is_set():
            result.append(("error", "hid_queue_overflow", 0.0))
        else:
            for _ in range(128):
                try:
                    result.append(self.events.get_nowait())
                except queue.Empty:
                    break
        with self._diagnostic_lock:
            if self._diagnostic is not None:
                # Preserve the failure snapshot before an error ends the worker.
                result.insert(0, ("hid_flow", self._diagnostic, 0.0))
                self._diagnostic = None
        return result

    def source_alive(self):
        return selected_host_and_address(self.entity) == (self.pid, self.address)

    def close(self, *, diagnostic=None, startup=False):
        succeeded = self._startup_cleanup_ok

        def invoke(target, method, *args):
            call = getattr(target, method)
            if self._gadget_device is None:
                return call(*args)
            import frida
            from . import chromecast_gadget_windows as gadget
            return gadget.bounded_remote_cleanup(
                frida, lambda cancel: call(*args, cancellable=cancel))
        def report(stage, phase):
            if diagnostic is not None:
                try:
                    diagnostic(stage, phase)
                except Exception:
                    pass
        if self.script is not None:
            started = time.monotonic()
            report("hid_script_unload", "begin")
            try:
                invoke(self.script, "unload")
            except Exception as error:
                succeeded = False
                report("hid_script_unload", "failed")
                self._startup_record("startup_script_unload" if startup else "cleanup_script_unload",
                                     "failed", started=started, error=error)
            else:
                report("hid_script_unload", "done")
            self.script = None
        if self.session is not None:
            started = time.monotonic()
            report("hid_session_detach", "begin")
            try:
                invoke(self.session, "detach")
            except Exception as error:
                succeeded = False
                report("hid_session_detach", "failed")
                self._startup_record("startup_session_detach" if startup else "cleanup_session_detach",
                                     "failed", started=started, error=error)
            else:
                report("hid_session_detach", "done")
            self.session = None
        if self._gadget_device is not None:
            started = time.monotonic()
            try:
                import frida
                invoke(frida.get_device_manager(), "remove_remote_device", self._gadget_device[1])
            except Exception as error:
                succeeded = False
                self._startup_record("cleanup_gadget_device_remove", "failed",
                                     started=started, error=error)
            self._gadget_device = None
        return succeeded
