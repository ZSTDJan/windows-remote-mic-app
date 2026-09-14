"""Exercise real Win32 wrapper calls in an isolated child with invalid handles.

Only the source observer runs: native report interception is replaced with test
callbacks. No driver handle, physical input, real host or registry is accessed.
"""

import importlib.util
import os
import subprocess
import sys
import unittest
import uuid

from ovb_rc003.frida_hid_tap_runtime import GADGET_SCRIPT
from ovb_rc003.remote_selection import container_key
from tests.test_hid_copy_interception import HARNESS
from tests.test_hid_device_source import SOURCE_HARNESS


NATIVE_ENTRY_HARNESS = r"""
const nativeHostLookup = Process.findModuleByName;
function peEntryFixture(names = ["DeviceIoControl"]) {
  fixtureMemory.writeU16(0x5a4d); fixtureMemory.add(0x3c).writeU32(0x1000);
  fixtureMemory.add(0x1000).writeByteArray(new Uint8Array(0x800));
  const nt = fixtureMemory.add(0x1000), optional = nt.add(24);
  nt.writeU32(0x4550); nt.add(4).writeU16(0x8664); nt.add(20).writeU16(240);
  optional.writeU16(0x20b); optional.add(56).writeU32(8192);
  optional.add(108).writeU32(16); optional.add(120).writeU32(0x1200);
  optional.add(124).writeU32(40);
  const descriptor = fixtureMemory.add(0x1200);
  descriptor.writeU32(0x1400); descriptor.add(12).writeU32(0x1300);
  descriptor.add(16).writeU32(0x180);
  fixtureMemory.add(0x1300).writeUtf8String("api-ms-win-core-io-l1-1-0.dll");
  names.forEach((name,index)=>{
    const rva=0x1500+index*64;
    fixtureMemory.add(0x1400+index*8).writeU64(name===null ? uint64("0x8000000000000001") : rva);
    if(name!==null) fixtureMemory.add(rva+2).writeUtf8String(name);
    fixtureMemory.add(0x180+index*8).writePointer(ptr(0));
  });
  return fixtureMemory.add(0x180);
}
rpc.exports.shiftedimport = function() {
  resetFixture();
  const first=peEntryFixture(["GetOverlappedResult","DeviceIoControl"]);
  const kernel=Process.getModuleByName("kernel32.dll");
  const wanted=kernel.getExportByName("DeviceIoControl");
  first.writePointer(kernel.getExportByName("GetOverlappedResult"));first.add(8).writePointer(wanted);
  fixture.enumerateImports=()=>[{name:"DeviceIoControl",slot:first}];
  const actual=deviceIoControlEntry(fixture);
  ensure(actual!==null && actual.equals(wanted), "shifted Frida slot prevented correct PE import resolution");
  return true;
};
rpc.exports.entryvalidation = function () {
  resetFixture();
  let slot = peEntryFixture();
  const kernel = Process.getModuleByName("kernel32.dll");
  const base = Process.getModuleByName("kernelbase.dll");
  const target = kernel.getExportByName("DeviceIoControl");
  slot.writePointer(target);
  fixture.enumerateImports = () => {throw Error("Frida import enumeration must not be used");};
  ensure(deviceIoControlEntry(fixture).equals(target), "verified PE import rejected");
  ensure(sourceEntryStatus === "verified", "verified entry status absent");
  peEntryFixture(["CloseHandle"]);
  ensure(deviceIoControlEntry(fixture) === null, "missing import guessed");
  ensure(sourceEntryStatus === "import_missing", "missing import status absent");
  peEntryFixture(["DeviceIoControl","DeviceIoControl"]);
  ensure(deviceIoControlEntry(fixture) === null, "ambiguous import guessed");
  ensure(sourceEntryStatus === "import_ambiguous", "ambiguous status absent");
  slot=peEntryFixture([null,"DeviceIoControl"]);
  slot.add(8).writePointer(target);
  ensure(deviceIoControlEntry(fixture).equals(target), "ordinal changed the following named import index");
  for(const corrupt of [
    ()=>fixtureMemory.writeU16(0),
    ()=>fixtureMemory.add(0x3c).writeU32(8190),
    ()=>fixtureMemory.add(0x1004).writeU16(0x14c),
    ()=>fixtureMemory.add(0x1014).writeU16(10),
    ()=>fixtureMemory.add(0x1018).writeU16(0x10b),
    ()=>fixtureMemory.add(0x1050).writeU32(8193),
    ()=>fixtureMemory.add(0x1090).writeU32(8190),
    ()=>fixtureMemory.add(0x1094).writeU32(20),
    ()=>fixtureMemory.add(0x1200).writeU32(0),
    ()=>fixtureMemory.add(0x1200).writeU32(8188),
    ()=>fixtureMemory.add(0x1210).writeU32(8188),
    ()=>fixtureMemory.add(0x1400).writeU64(8191),
    ()=>fixtureMemory.add(0x1400).writeU64(uint64("0x100000000")),
    ()=>fixtureMemory.add(0x1502).writeByteArray(new Uint8Array(256).fill(65))
  ]) {
    peEntryFixture().writePointer(target);corrupt();
    ensure(deviceIoControlEntry(fixture) === null, "malformed import table trusted");
  }
  slot=peEntryFixture();
  for (const bad of [ptr(0), target.add(1), kernel.getExportByName("CloseHandle"), fixtureMemory]) {
    slot.writePointer(bad);
    ensure(deviceIoControlEntry(fixture) === null, "unverified function hooked");
  }
  for (const target of [base.getExportByName("DeviceIoControl"), base.enumerateExports().find(e=>e.name === "DeviceIoControl").address]) {
    slot.writePointer(target);
    ensure(deviceIoControlEntry(fixture).equals(target), "direct or OS-resolved KernelBase import rejected");
  }
  return true;
};
rpc.exports.nativeentry = function (entryName, selected) {
  Interceptor.detachAll();
  hookInstalled = false;
  resetFixture();
  const entryModule = Process.getModuleByName(entryName === "resolved_kernelbase" ? "kernelbase.dll" : entryName);
  const entryTarget = entryName === "resolved_kernelbase" ? entryModule.getExportByName("DeviceIoControl") :
    entryModule.enumerateExports().find(e=>e.name === "DeviceIoControl").address;
  const importSlot = peEntryFixture();
  importSlot.writePointer(entryTarget);
  // Frida's resolved address may differ from the live slot for API-set imports.
  fixture.enumerateImports = () => [{name: "DeviceIoControl", type: "function",
    slot: importSlot, address: Process.getModuleByName("kernelbase.dll").getExportByName("DeviceIoControl")}];
  Process.findModuleByName = name => name.toLowerCase() === "wudfhost.exe" ? fixture : nativeHostLookup(name);
  const observations = [];
  try {
    installHook();
    resetFixture();
    enableSelected(selected);
    const metadata = Memory.alloc(8), buffer = Memory.alloc(9), returned = Memory.alloc(4);
    metadata.writeU32(123); metadata.add(4).writeU8(2); metadata.add(5).writeU8(1);
    buffer.writeByteArray(RIGHT.match(/../g).map(x => parseInt(x,16)));
    const bytes = [];
    function put(values) { bytes.push(...values); }
    function u64(pointer) { const value = Memory.alloc(8); value.writePointer(pointer); put(Array.from(new Uint8Array(value.readByteArray(8)))); }
    function i32(value) { const data=Memory.alloc(4);data.writeS32(value);put(Array.from(new Uint8Array(data.readByteArray(4)))); }
    // Synthetic host call site: preserve the device object in rbp, then call
    // the live import slot. The real kernel32 wrapper overwrites rbp internally.
    put([0x55,0x48,0x83,0xec,0x40,0x48,0x89,0xcd]);
    put([0x48,0xb8]); u64(buffer); put([0x48,0x89,0x44,0x24,0x20]);
    put([0xc7,0x44,0x24,0x28,9,0,0,0]);
    put([0x48,0xb8]); u64(returned); put([0x48,0x89,0x44,0x24,0x30]);
    put([0x48,0xc7,0x44,0x24,0x38,0,0,0,0]);
    put([0x49,0xb8]); u64(metadata);
    put([0x41,0xb9,8,0,0,0,0xba,0x83,0x84,0x01,0x80]);
    put([0x48,0x8b,0x8d,0xc8,0,0,0,0xff,0x15]);
    i32(importSlot.sub(callsite.add(bytes.length+4)).toInt32());
    put([0x48,0x83,0xc4,0x40,0x5d,0xc3]);
    ensure(bytes.length < 0x80, "native call site exceeds fixture");
    ensure(Memory.protect(fixtureMemory,8192,"rwx"), "cannot execute owned fixture");
    callsite.writeByteArray(bytes);
    // Invalid pseudo-handles cannot refer to a real device in the child.
    objects[0].add(0xc8).writePointer(ptr(-1));
    objects[1].add(0xc8).writePointer(ptr(-2));
    const nt = Process.getModuleByName("ntdll.dll").getExportByName("NtDeviceIoControlFile");
    Interceptor.attach(nt, {
      onEnter(args) {
        if (args[5].toUInt32() !== UMDF_COPY_IOCTL || args[7].toUInt32() !== 8 || args[9].toUInt32() !== 9) return;
        observations.push(copySource(args));
      }
    });
    Interceptor.flush();
    const invokeNative = new NativeFunction(callsite,"int",["pointer"]);
    const results=[invokeNative(objects[0]),invokeNative(objects[1])];
    return {observations,results,records:records.slice(),remainingFrames:sourceFrames.size,raw:hex(buffer,9)};
  } finally {
    Interceptor.detachAll();
    Process.findModuleByName = nativeHostLookup;
    hookInstalled=false; output=null;
  }
};
"""


@unittest.skipUnless(os.name == "nt" and importlib.util.find_spec("frida"), "requires Windows Frida runtime")
class NativeSourceEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import frida

        cls.child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        cls.session = None
        cls.addClassCleanup(cls.cleanup)
        cls.session = frida.attach(cls.child.pid)
        source = GADGET_SCRIPT
        for target, callback in (("target", "copyCallbacks"), ("closeTarget", "closeCallbacks")):
            source = source.replace(f"Interceptor.attach({target}, {{", f"globalThis.{callback} = ({{")
        cls.script = cls.session.create_script(source + HARNESS + SOURCE_HARNESS + NATIVE_ENTRY_HARNESS)
        cls.errors = []
        cls.script.on("message", lambda message, _data: cls.errors.append(message) if message.get("type") == "error" else None)
        cls.script.load()

    @classmethod
    def cleanup(cls):
        if cls.session is not None:
            cls.session.detach()
        cls.child.terminate()
        cls.child.wait(timeout=5)

    def test_real_wrapper_preserves_selected_source_at_import_entry(self):
        selected = container_key(uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00"))
        for name in ("kernelbase.dll", "kernel32.dll", "resolved_kernelbase"):
            with self.subTest(entry=name):
                result = self.script.exports_sync.nativeentry(name, selected)
                self.assertEqual(result["observations"], [{"kind": "device", "key": selected}, None])
                self.assertEqual(result["results"], [0, 0])
                self.assertEqual(result["remainingFrames"], 0)
                self.assertEqual(result["raw"], "0100004f0000000000")
                self.assertFalse(any(r.get("kind") == "source_status" for r in result["records"]))
        self.assertEqual(self.errors, [])

    def test_source_entry_requires_a_verified_live_import_slot(self):
        self.assertTrue(self.script.exports_sync.entryvalidation())
        self.assertEqual(self.errors, [])

    def test_shifted_frida_slot_does_not_hide_the_real_import(self):
        self.assertTrue(self.script.exports_sync.shiftedimport())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
