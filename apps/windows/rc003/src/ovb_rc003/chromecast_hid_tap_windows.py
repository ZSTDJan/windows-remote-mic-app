"""Read selected Chromecast HID notifications when BTHPORT omits report bytes.

The callback is private Windows code. A matching module hash or matching public
PDB is required before attaching. An adapter address match is required before
any report is delivered; this module never modifies the Windows report.
"""
from __future__ import annotations

import ctypes
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
import uuid

from . import remote_selection


MODULE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "umdf" / "microsoft.bluetooth.profiles.hidovergatt.dll"
_TESTED_SHA256 = "8a18571ddfa447bd590354ccdf7800b815a098806359ab195d395158f9d07cc5"
_TESTED_RVA = 0x12FCC
_HID_SERVICE = "{00001812-0000-1000-8000-00805f9b34fb}"
_GOOGLE_HARDWARE = "dev_vid&0118d1_pid&9450_rev&0110"
_ADDRESS_SUFFIX = re.compile(r"_([0-9a-f]{12})$", re.I)
_SYMBOL = "Microsoft::Bluetooth::Profiles::HidOverGatt::HogpGattAdapter::OnReportValueChanged"


class TapError(RuntimeError):
    pass


def selected_host_and_address(entity: str) -> tuple[int, str]:
    """Resolve exactly one selected Google HID service from Windows PnP."""
    import winreg

    if not remote_selection.valid_key(entity):
        raise TapError("hid_source_unconfirmed")
    found = set()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice") as root:
            count = winreg.QueryInfoKey(root)[0]
            for index in range(count):
                name = winreg.EnumKey(root, index)
                folded = name.casefold()
                if not folded.startswith(_HID_SERVICE) or _GOOGLE_HARDWARE not in folded:
                    continue
                address = _ADDRESS_SUFFIX.search(name)
                if address is None:
                    continue
                with winreg.OpenKey(root, name) as service:
                    for member in range(winreg.QueryInfoKey(service)[0]):
                        instance = winreg.EnumKey(service, member)
                        with winreg.OpenKey(service, instance) as key:
                            try:
                                container, _ = winreg.QueryValueEx(key, "ContainerID")
                            except OSError:
                                continue
                            if remote_selection.container_key(container) != entity:
                                continue
                            with winreg.OpenKey(key, r"Device Parameters\WUDFDiagnosticInfo") as info:
                                pid, _ = winreg.QueryValueEx(info, "HostPid")
                            if type(pid) is int and pid > 0:
                                found.add((pid, address.group(1).lower()))
    except (OSError, ValueError) as exc:
        raise TapError("hid_source_unconfirmed") from exc
    if len(found) != 1:
        raise TapError("hid_source_unconfirmed")
    return found.pop()


def _pdb_identity(path: Path) -> tuple[str, str]:
    import pefile

    image = pefile.PE(str(path), fast_load=False)
    try:
        entries = [entry for entry in image.DIRECTORY_ENTRY_DEBUG if entry.struct.Type == 2]
        if len(entries) != 1:
            raise TapError("hid_symbol_unavailable")
        entry = entries[0]
        with path.open("rb") as stream:
            stream.seek(entry.struct.PointerToRawData)
            record = stream.read(entry.struct.SizeOfData)
        if record[:4] != b"RSDS" or len(record) < 28:
            raise TapError("hid_symbol_unavailable")
        name = record[24:].split(b"\0", 1)[0].decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.pdb", name, re.I):
            raise TapError("hid_symbol_unavailable")
        index = uuid.UUID(bytes_le=record[4:20]).hex.upper() + f"{struct.unpack_from('<I', record, 20)[0]:X}"
        return name, index
    finally:
        image.close()


def _symbol_cache() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise TapError("hid_symbol_unavailable")
    return Path(root) / "RemoteMic" / "HidSymbols"


def _ensure_pdb(module: Path) -> Path:
    name, index = _pdb_identity(module)
    target = _symbol_cache() / name / index / name
    if target.is_file() and target.stat().st_size <= 2_097_152 and target.read_bytes()[:32].startswith(b"Microsoft C/C++ MSF 7.00"):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{name}.{os.getpid()}.{threading.get_ident()}.tmp")
    url = f"https://msdl.microsoft.com/download/symbols/{name}/{index}/{name}"
    curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    try:
        result = subprocess.run(
            [str(curl), "--fail", "--location", "--silent", "--show-error", "--max-time", "15",
             "--max-filesize", "2097152", "--output", str(temporary), url],
            capture_output=True, timeout=18, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or not temporary.is_file() or not 100_000 <= temporary.stat().st_size <= 2_097_152:
            raise TapError("hid_symbol_unavailable")
        if not temporary.read_bytes()[:32].startswith(b"Microsoft C/C++ MSF 7.00"):
            raise TapError("hid_symbol_unavailable")
        os.replace(temporary, target)
        return target
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TapError("hid_symbol_unavailable") from exc
    finally:
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


def _resolve_pdb_symbol(module: Path) -> int:
    _ensure_pdb(module)
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
    if not dbghelp.SymInitializeW(process, str(_symbol_cache()), False):
        raise TapError("hid_symbol_unavailable")
    try:
        base = dbghelp.SymLoadModuleExW(process, None, str(module), None, 0, 0, None, 0)
        if not base:
            raise TapError("hid_symbol_unavailable")
        matches = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.POINTER(_SymbolInfo), wintypes.ULONG, wintypes.LPVOID)

        def collect(pointer, _size, _context):
            item = pointer.contents
            name = ctypes.wstring_at(ctypes.addressof(item) + _SymbolInfo.Name.offset, item.NameLen).rstrip("\0")
            if name == _SYMBOL:
                matches.append(item.Address - base)
            return True

        callback = callback_type(collect)
        if not dbghelp.SymEnumSymbolsW(process, base, "*", callback, None) or len(matches) != 1:
            raise TapError("hid_symbol_unavailable")
        return matches[0]
    finally:
        dbghelp.SymCleanup(process)


def callback_rva(module: Path = MODULE) -> int:
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    rva = _TESTED_RVA if digest == _TESTED_SHA256 else _resolve_pdb_symbol(module)
    import pefile

    image = pefile.PE(str(module), fast_load=True)
    try:
        executable = [section for section in image.sections if section.Characteristics & 0x20000000
                      and section.VirtualAddress <= rva < section.VirtualAddress + max(section.Misc_VirtualSize, section.SizeOfRawData)]
        if len(executable) != 1:
            raise TapError("hid_symbol_unavailable")
    finally:
        image.close()
    return rva


_SCRIPT = r"""
const expectedPath = __PATH__;
const expectedAddress = __ADDRESS__;
const module = Process.findModuleByName("microsoft.bluetooth.profiles.hidovergatt.dll");
if (module === null || module.path.toLowerCase().replace(/\\/g, "/") !== expectedPath)
  throw new Error("module_mismatch");
const callback = module.base.add(__RVA__);
const iid = Memory.alloc(16);
iid.writeByteArray([0xef,0x0f,0x5a,0x90,0x53,0xbc,0xdf,0x11,0x8c,0x49,0x00,0x1e,0x4f,0xc6,0x86,0xda]);
let addressSeen = false;
function method(object, slot, result, args) {
  return new NativeFunction(object.readPointer().add(slot * Process.pointerSize).readPointer(), result, args);
}
function addressMatches(adapter) {
  const bytes = new Uint8Array(adapter.add(24).readByteArray(6));
  for (let i = 0; i < 6; i++) if (bytes[i] !== expectedAddress[i]) return false;
  return true;
}
function report(buffer) {
  let access = ptr(0);
  try {
    if (buffer.isNull()) return null;
    const sizeOut = Memory.alloc(4);
    if (method(buffer, 7, "int", ["pointer", "pointer"])(buffer, sizeOut) !== 0) return null;
    if (sizeOut.readU32() !== 8) return null;
    const accessOut = Memory.alloc(Process.pointerSize);
    if (method(buffer, 0, "int", ["pointer", "pointer", "pointer"])(buffer, iid, accessOut) !== 0) return null;
    access = accessOut.readPointer();
    const bytesOut = Memory.alloc(Process.pointerSize);
    if (method(access, 3, "int", ["pointer", "pointer"])(access, bytesOut) !== 0) return null;
    return new Uint8Array(bytesOut.readPointer().readByteArray(8));
  } finally {
    if (!access.isNull()) method(access, 2, "uint32", ["pointer"])(access);
  }
}
Interceptor.attach(callback, {onEnter(args) {
  try {
    if (!addressMatches(args[0])) return;
    addressSeen = true;
    const bytes = report(args[2]);
    if (bytes === null) return;
    const valid = bytes.slice(1).every(value => value === 0) &&
                  (bytes[0] === 0 || [1,3,4,5,6,7,8,10,11,12,13,14,15,17].includes(bytes[0]));
    if (!valid) return;
    // The sender interface pointer changes between reports on this driver.
    // Bind by the selected adapter address, never by that transient pointer.
    send({kind:"report", value:Array.from(bytes).map(x => x.toString(16).padStart(2,"0")).join("")});
  } catch (_) {
    if (addressSeen) send({kind:"error", reason:"hid_callback_failed"});
  }
}});
send({kind:"hook_ready"});
"""


class HidTap:
    def __init__(self, entity: str):
        self.entity = entity
        self.events = queue.Queue(maxsize=256)
        self.overflow = threading.Event()
        self.session = self.script = None
        self.pid = self.address = None

    def _put(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            # A dropped release is unsafe. Discard the backlog and stop.
            self.overflow.set()

    def start(self):
        import frida
        pid, address = selected_host_and_address(self.entity)
        rva = callback_rva()
        source = (_SCRIPT.replace("__PATH__", json.dumps(str(MODULE).lower().replace("\\", "/")))
                 .replace("__ADDRESS__", json.dumps(list(bytes.fromhex(address)[::-1])))
                 .replace("__RVA__", str(rva)))
        self.pid, self.address = pid, address
        try:
            self.session = frida.attach(pid)
            self.session.on("detached", lambda *_: self._put(("error", "hid_host_detached", 0.0)))
            self.script = self.session.create_script(source)

            def on_message(message, _data):
                import time
                if message.get("type") != "send":
                    self._put(("error", "hid_callback_failed", 0.0))
                    return
                item = message.get("payload", {})
                if item.get("kind") == "report":
                    self._put(("report", item.get("value"), time.monotonic()))
                elif item.get("kind") == "hook_ready":
                    self._put(("hook_ready", "", time.monotonic()))
                elif item.get("kind") == "error":
                    self._put(("error", item.get("reason", "hid_callback_failed"), 0.0))

            self.script.on("message", on_message)
            self.script.load()
            if selected_host_and_address(self.entity) != (pid, address):
                raise TapError("hid_source_unconfirmed")
        except Exception:
            self.close()
            raise

    def poll(self):
        if self.overflow.is_set():
            return [("error", "hid_queue_overflow", 0.0)]
        result = []
        for _ in range(128):
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                break
        return result

    def source_alive(self):
        return selected_host_and_address(self.entity) == (self.pid, self.address)

    def close(self):
        if self.script is not None:
            try:
                self.script.unload()
            except Exception:
                pass
            self.script = None
        if self.session is not None:
            try:
                self.session.detach()
            except Exception:
                pass
            self.session = None
