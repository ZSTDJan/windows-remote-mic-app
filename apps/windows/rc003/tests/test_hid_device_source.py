"""Real Frida instruction/crypto execution; only synthetic devices and copies.

The UMDF fixtures model independent device objects in one host. No system host
is attached, no registry values changed, and no keyboard events are generated.
"""

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

from ovb_rc003.frida_hid_tap_runtime import GADGET_SCRIPT
from tests.test_hid_copy_interception import HARNESS


SOURCE_HARNESS = r"""
const nativeModuleLookup = Process.findModuleByAddress;
const nativeFunctionLookup = sourceApi.lookup;
const nativeQuery = sourceApi.query;
const fixtureMemory = Memory.alloc(8192);
const fixture = {base: fixtureMemory, size: 8192, name: "WUDFHost.exe"};
const entry = fixtureMemory.add(0x40);
const callsite = fixtureMemory.add(0x100);
// A complete straight-line block: handle from device object, parameters, call.
const callCode = [0x48,0x8b,0x8d,0xc8,0,0,0, 0xba,0x83,0x84,0x01,0x80,
                  0x4c,0x8d,0x44,0x24,0x20, 0xff,0x15,0x80,0,0,0];
const returnSite = callsite.add(callCode.length);
const vtable = fixtureMemory.add(0x300), interfaceEntry = fixtureMemory.add(0x200);
const getter = fixtureMemory.add(0x600), interfaceCode = fixtureMemory.add(0x800), iidMemory = fixtureMemory.add(0x900);
const objects = [Memory.alloc(512), Memory.alloc(512)];
const names = ["{11223344-5566-7788-99aa-bbccddeeff00}", "{22334455-6677-8899-aabb-ccddeeff0011}"];
let queryCount = 0;
const fixtureRoot = "\\REGISTRY\\MACHINE\\SYSTEM\\ControlSet001\\Enum\\BTHLEDEVICE\\FixtureHardware\\";
const registryPaths = new Map();
let parentOpenError = 0, parentOpens = 0, parentCloses = 0, keyNameError = 0;
const originalNativeFunction = NativeFunction;
const fixtureNtQueryKey = Process.getModuleByName("ntdll.dll").getExportByName("NtQueryKey");
const fixtureAdvapi = Process.getModuleByName("advapi32.dll");
NativeFunction = function (address, result, args) {
  if (address.equals(fixtureNtQueryKey)) return (key, kind, output, size, needed) => {
    const path = registryPaths.get(key.toString());
    if (path === undefined) return new originalNativeFunction(address, result, args)(key, kind, output, size, needed);
    ensure(kind === 3 && size === 8192, "unexpected key name query");
    if (keyNameError) return keyNameError;
    output.writeU32(path.length * 2); output.add(4).writeUtf16String(path); return 0;
  };
  if (address.equals(fixtureAdvapi.getExportByName("RegOpenKeyExW"))) return (key, name, options, access, output) => {
    const path = "\\REGISTRY\\MACHINE\\" + name.readUtf16String();
    const index = [fixtureRoot + "0", fixtureRoot + "1"].indexOf(path);
    if (index < 0) throw Error("attempted to open an unrelated registry key");
    ensure(key.equals(ptr("0xffffffff80000002")) && options === 0 && access === 1, "parent query gained write access");
    parentOpens++;
    if (parentOpenError) return parentOpenError;
    output.writePointer(ptr(0x900 + index)); return 0;
  };
  if (address.equals(fixtureAdvapi.getExportByName("RegCloseKey"))) return key => {
    ensure(key.equals(ptr(0x900)) || key.equals(ptr(0x901)), "closed the live source handle");
    parentCloses++; return 0;
  };
  return new originalNativeFunction(address, result, args);
};

function resetFixture() {
  registryPaths.clear();
  for (let index = 0; index < 2; index++) {
    registryPaths.set(ptr(0x900 + index).toString(), fixtureRoot + index);
    registryPaths.set(ptr(0x1900 + index).toString(), fixtureRoot + index + "\\Device Parameters");
  }
  parentOpenError = 0; parentOpens = 0; parentCloses = 0; keyNameError = 0;
  callsite.writeByteArray(callCode);
  entry.writeU32(0x100); entry.add(4).writeU32(0x180);
  vtable.writePointer(interfaceCode);
  interfaceEntry.writeU32(0x800); interfaceEntry.add(4).writeU32(0x817);
  iidMemory.writeByteArray("7a2dfa5b66f7d34a959ed8fe0f0e83e1".match(/../g).map(x => parseInt(x, 16)));
  interfaceCode.writeByteArray([0x48,0x8b,0x02,0x48,0x2b,0x05,0xf6,0,0,0,
    0x48,0x8b,0x42,0x08,0x48,0x2b,0x05,0xf3,0,0,0,0xc3,0xcc]);
  vtable.add(31 * 8).writePointer(getter);
  getter.writeByteArray([0x48,0x8b,0x41,0x40,0xc3]);
  objects.forEach((object, index) => {
    object.writePointer(vtable); object.add(0x40).writePointer(ptr(0x900 + index));
    object.add(0xc8).writePointer(ptr(0x123 + index));
  });
  Process.findModuleByAddress = address => insideModule(fixture, address) ? fixture : nativeModuleLookup(address);
  sourceApi.lookup = (address, imageBase, history) => {
    if (!insideModule(fixture, address)) return nativeFunctionLookup(address, imageBase, history);
    imageBase.writePointer(fixtureMemory); return address.equals(interfaceCode) ? interfaceEntry : entry;
  };
  sourceApi.query = (key, name, reserved, type, value, size) => {
    queryCount++;
    const text = names[key.toUInt32() - 0x900];
    if (text === undefined || name.readUtf16String() !== "ContainerID") return 5;
    type.writeU32(1); size.writeU32((text.length + 1) * 2); value.writeUtf16String(text); return 0;
  };
  sourceHandles.clear(); sourceCallsites.clear(); sourceVtables.clear(); sourceFrames.clear();
  records.length = 0; queryCount = 0;
}

function enableSelected(key, exclusive = false) {
  output = {};
  handleControl({kind: "intercept_control", action: "enable", protocol: 4, lease_ms: 5000,
    selected_key: key, source_binding_revision: 1, exclusive_source_verified: exclusive,
    health_diagnostics: 1, copy_probe_seconds: 10});
}

function deviceCopy(index, raw, extra = {}) {
  return invoke(raw, {handle: 0x123 + index, sourceContext: {rbp: objects[index]}, returnAddress: returnSite, ...extra});
}

rpc.exports.sourcekeys = function () {
  resetFixture();
  return [containerSourceKey(ptr(0x900)), containerSourceKey(ptr(0x901))];
};

rpc.exports.sourcescenario = function (scenario, selected) {
  resetFixture();
  enableSelected(selected);
  if (scenario === "two_devices") {
    const other = deviceCopy(1, RIGHT);
    ensure(other.kernelCopy === RIGHT && other.records.length === 0, "unselected source touched or logged");
    ensure(pendingCopyCandidate === null, "other device occupied candidate slot");
    const initial = deviceCopy(0, NEUTRAL);
    const candidate = initial.records.find(r => r.kind === "copy_candidate");
    ensure(candidate && candidate.source_key === selected && candidate.source_kind === "device", "device proof missing");
    handleControl({...candidate, kind: "intercept_control", action: "bind_copy_handle"});
    ensure(deviceCopy(0, RIGHT).kernelCopy === NEUTRAL, "selected source did not intercept");
    const concurrent = deviceCopy(1, RIGHT);
    ensure(concurrent.kernelCopy === RIGHT && concurrent.records.length === 0, "same-key overlap crossed devices");
    ensure(deviceCopy(1, NEUTRAL).records.length === 0, "other release affected selected hold");
    ensure(deviceCopy(0, NEUTRAL).records.some(r => r.kind === "gatt_read"), "selected release lost");
    ensure(queryCount === 2, "identity not cached per live device");
    objects[1].add(0xc8).writePointer(ptr(0x123));
    ensure(deviceCopy(1, RIGHT, {handle: 0x123}).records.length === 0, "device identity was replaced by handle equality");
    ensure(deviceCopy(0, RIGHT).kernelCopy === NEUTRAL, "other object on same transport retired selected device");
  } else if (scenario === "first_selected_press") {
    const first = deviceCopy(0, MIC);
    const candidate = first.records.find(r => r.kind === "copy_candidate");
    const mapped = first.records.filter(r => r.kind === "gatt_read");
    ensure(first.kernelCopy === NEUTRAL, "first selected-device press reached Windows");
    ensure(candidate && candidate.source_kind === "device" && candidate.source_key === selected,
      "first selected-device press lost its independent bind evidence");
    ensure(mapped.length === 1 && mapped[0].raw === MIC,
      "first selected-device press was not mapped exactly once");
    ensure(boundCopyHandle === "0x123" && interceptionReady,
      "verified first report did not establish provisional ownership");
    handleControl({...candidate, kind: "intercept_control", action: "bind_copy_handle"});
    ensure(boundCopyHandle === "0x123" && interceptionReady,
      "client acknowledgement discarded provisional readiness");
    ensure(deviceCopy(1, RIGHT).kernelCopy === RIGHT,
      "first-report ownership crossed to another device");
    ensure(deviceCopy(0, NEUTRAL).records.some(r => r.kind === "gatt_read"),
      "first selected-device release was lost");
  } else if (scenario === "first_selected_press_failure") {
    const failed = deviceCopy(0, MIC, {status: 0xc0000001});
    ensure(failed.afterReturn === MIC, "failed provisional copy did not restore source");
    ensure(boundCopyHandle === null && pendingCopyCandidate === null && !interceptionReady,
      "failed provisional copy retained ownership");
    ensure(!failed.records.some(r => r.kind === "copy_candidate" || r.kind === "gatt_read"),
      "failed provisional copy was forwarded");
  } else if (scenario === "unknown_source") {
    iidMemory.writeU8(0);
    const unknown = deviceCopy(0, RIGHT);
    ensure(unknown.kernelCopy === RIGHT && !unknown.records.some(r => r.raw || r.entry_hex), "unknown source leaked");
    ensure(unknown.records.some(r => r.kind === "source_status"), "unknown source stayed silent");
    ensure(deviceCopy(0, RIGHT).records.length === 0, "source failure floods/restarts receiver");
    ensure(pendingCopyCandidate === null, "unknown source bound");
  } else if (scenario === "exclusive_fallback") {
    enableSelected(selected, true);
    iidMemory.writeU8(0);
    const candidateCopy = deviceCopy(0, NEUTRAL);
    const candidate = candidateCopy.records.find(r => r.kind === "copy_candidate");
    ensure(candidate && candidate.source_kind === "exclusive", "exclusive compatibility lost");
    ensure(!candidateCopy.records.some(r => r.kind === "copy_probe"), "fallback logged raw before client verification");
    handleControl({...candidate, kind: "intercept_control", action: "bind_copy_handle"});
    ensure(deviceCopy(0, RIGHT).kernelCopy === NEUTRAL, "exclusive ownership failed");
  } else if (scenario === "known_other_beats_fallback") {
    enableSelected(selected, true);
    ensure(deviceCopy(1, RIGHT).records.length === 0 && pendingCopyCandidate === null, "exclusive flag overrode contrary device identity");
  } else if (scenario === "rejected_fallback_recovers") {
    enableSelected(selected, true);
    iidMemory.writeU8(0);
    const candidate = deviceCopy(0, NEUTRAL).records.find(r => r.kind === "copy_candidate");
    handleControl({...candidate, kind: "intercept_control", action: "reject_copy_handle"});
    ensure(pendingCopyCandidate === null && !exclusiveSourceVerified && interceptionActive(), "rejected fallback retained ownership or disconnected");
    iidMemory.writeU8(0x7a);
    ensure(deviceCopy(0, NEUTRAL).records.some(r => r.kind === "copy_candidate" && r.source_kind === "device"), "device evidence did not recover after fallback rejected");
  } else if (scenario === "bound_source_lost") {
    const candidate = deviceCopy(0, NEUTRAL).records.find(r => r.kind === "copy_candidate");
    handleControl({...candidate, kind: "intercept_control", action: "bind_copy_handle"});
    deviceCopy(0, RIGHT);
    objects[0].add(0x40).writePointer(ptr(0x999));
    const missing = deviceCopy(0, NEUTRAL);
    ensure(missing.records.some(r => r.kind === "source_status") && boundCopyHandle === null, "lost bound source did not cancel old ownership");
    ensure(!missing.records.some(r => r.kind === "gatt_read"), "unverified release completed a gesture");
    objects[0].add(0x40).writePointer(ptr(0x900));
    ensure(deviceCopy(0, NEUTRAL).records.some(r => r.kind === "copy_candidate"), "source recovery requires process restart");
  } else if (scenario === "closed_reused_handle") {
    const candidate = deviceCopy(0, NEUTRAL).records.find(r => r.kind === "copy_candidate");
    handleControl({...candidate, kind: "intercept_control", action: "bind_copy_handle"});
    globalThis.closeCallbacks.onEnter.call({}, [ptr(0x123)]);
    objects[1].add(0xc8).writePointer(ptr(0x123));
    enableSelected(selected);
    const reused = deviceCopy(1, RIGHT, {handle: 0x123});
    ensure(reused.kernelCopy === RIGHT && reused.records.length === 0 && sourceHandles.get("0x123").key !== selected, "reused handle inherited selection");
  } else if (scenario === "changed_selection_inflight") {
    const candidate = deviceCopy(0, NEUTRAL).records.find(r => r.kind === "copy_candidate");
    handleControl({...candidate, kind: "intercept_control", action: "bind_copy_handle"});
    const result = deviceCopy(0, RIGHT, {beforeReturn: () => enableSelected(containerSourceKey(ptr(0x901)))});
    ensure(!result.records.some(r => r.kind === "gatt_read" || r.kind === "copy_probe" && r.phase === "copy"), "old report crossed selection boundary");
    ensure(deviceCopy(0, RIGHT).records.length === 0, "previous selection still accepted");
  } else if (scenario === "missing_or_bad_selection") {
    for (const key of ["", "a", "G".repeat(64), "a".repeat(64) + "\n", 123]) {
      enableSelected(key);
      ensure(!interceptionActive() && deviceCopy(0, RIGHT).kernelCopy === RIGHT, "invalid selection enabled interception");
    }
    enableSelected(selected);
    handleControl({kind: "intercept_control", action: "enable", protocol: 4, lease_ms: 5000, selected_key: selected});
    ensure(!interceptionActive(), "missing source capability accepted");
  } else throw Error("unknown source scenario");
  return true;
};

rpc.exports.sourcevalidation = function () {
  resetFixture();
  const good = resolveCopyDevice({rbp: objects[0]}, returnSite, ptr(0x123));
  ensure(good !== null, "valid device resolver failed");
  ensure(resolveCopyDevice({rbx: objects[0]}, returnSite, ptr(0x123)) === null, "guessed another register");
  ensure(resolveCopyDevice({rbp: objects[1]}, returnSite, ptr(0x123)) === null, "mismatched handle accepted");
  for (const corrupt of [
    () => interfaceCode.add(3).writeByteArray([0x90,0x90,0x90]),
    () => iidMemory.writeU8(0),
    () => getter.writeByteArray([0x48,0x8b,0x41,0x40,0x90]),
    () => callsite.add(7).writeByteArray([0x31,0xc9,0x90,0x90,0x90]),
    () => objects[0].add(0x40).writePointer(ptr(0x999)),
    () => { sourceApi.query = () => 5; },
  ]) {
    resetFixture(); corrupt();
    ensure(resolveCopyDevice({rbp: objects[0]}, returnSite, ptr(0x123)) === null, "unverified object/callsite/registry accepted");
  }
  resetFixture(); enableSelected(good);
  const metadata = Memory.alloc(8), buffer = Memory.alloc(9);
  const args = [ptr(0x123),ptr(0),ptr(0),ptr(0),ptr(0),ptr(0x80018483),metadata,ptr(8),buffer,ptr(9)];
  const frame = {handle: args[0], metadata, buffer, inputLength: 8, outputLength: 9, key: good};
  sourceFrames.set(Process.getCurrentThreadId(), [frame]);
  ensure(copySource(args).key === good, "matching native frame rejected");
  sourceFrames.get(Process.getCurrentThreadId()).push({...frame, key: containerSourceKey(ptr(0x901))});
  ensure(copySource(args) === null, "nested call borrowed outer selected identity");
  sourceFrames.get(Process.getCurrentThreadId()).pop();
  args[8] = Memory.alloc(9);
  ensure(copySource(args) === null, "mismatched buffer borrowed identity");
  sourceFrames.clear();
  return true;
};

rpc.exports.instanceidentity = function (scenario, selected) {
  resetFixture(); enableSelected(selected);
  objects.forEach((object, index) => object.add(0x40).writePointer(ptr(0x1900 + index)));
  if (scenario === "two_parameters") {
    const other = deviceCopy(1, RIGHT);
    ensure(other.kernelCopy === RIGHT && other.records.length === 0, "other parameter key accepted");
    const candidate = deviceCopy(0, NEUTRAL).records.find(r => r.kind === "copy_candidate");
    ensure(candidate && candidate.source_kind === "device" && candidate.source_key === selected, "instance root not used for selected source");
    handleControl({...candidate, kind: "intercept_control", action: "bind_copy_handle"});
    ensure(deviceCopy(0, RIGHT).kernelCopy === NEUTRAL, "selected device did not intercept");
    ensure(deviceCopy(1, RIGHT).kernelCopy === RIGHT, "overlapping other device intercepted");
    ensure(deviceCopy(0, NEUTRAL).records.some(r => r.kind === "gatt_read"), "selected release lost");
    ensure(parentOpens === 2 && parentCloses === 2 && queryCount === 2, "instance ownership/cache incorrect");
    ensure(!sourceDiagnosticsEnabled, "resolver requires diagnostics");
  } else if (scenario === "wrong_subkey_identity") {
    const query = sourceApi.query;
    sourceApi.query = (key, ...args) => query(key.equals(ptr(0x1901)) ? ptr(0x900) : key, ...args);
    ensure(deviceCopy(1, RIGHT).records.length === 0 && pendingCopyCandidate === null,
      "ContainerID from Device Parameters overrode authoritative instance identity");
    ensure(parentOpens === 1 && parentCloses === 1, "wrong subkey did not read its own root");
  } else if (scenario === "bad_path") {
    for (const path of [
      fixtureRoot + "0\\Device Parameters\\Nested", fixtureRoot + "0\\Other",
      fixtureRoot.replace("\\SYSTEM\\ControlSet001", "\\SOFTWARE\\ControlSet001") + "0\\Device Parameters",
      fixtureRoot.replace("ControlSet001", "Untrusted") + "0\\Device Parameters",
      fixtureRoot.replace("BTHLEDEVICE", "") + "0\\Device Parameters",
      fixtureRoot + "0\0\\Device Parameters",
    ]) {
      registryPaths.set("0x1900", path);
      ensure(resolveCopyDevice({rbp: objects[0]}, returnSite, ptr(0x123)) === null, "untrusted registry path accepted");
    }
    ensure(parentOpens === 0 && queryCount === 0, "invalid path caused identity lookup");
  } else if (scenario === "failures_recover") {
    keyNameError = -1;
    ensure(resolveCopyDevice({rbp: objects[0]}, returnSite, ptr(0x123)) === null && parentOpens === 0, "key-name failure ignored");
    keyNameError = 0; parentOpenError = 5;
    ensure(resolveCopyDevice({rbp: objects[0]}, returnSite, ptr(0x123)) === null && parentCloses === 0, "failed-open handle closed or accepted");
    parentOpenError = 0;
    const query = sourceApi.query;
    for (const failure of [() => 2, (key, name, reserved, type, value, size) => {
      type.writeU32(1); size.writeU32(78); value.writeUtf16String("x".repeat(38)); return 0;
    }, () => { throw Error("fixture query failure"); }]) {
      sourceApi.query = failure;
      ensure(resolveCopyDevice({rbp: objects[0]}, returnSite, ptr(0x123)) === null, "invalid/missing identity accepted");
      ensure(parentCloses === parentOpens - 1, "opened parent leaked on failure");
    }
    sourceApi.query = query;
    ensure(resolveCopyDevice({rbp: objects[0]}, returnSite, ptr(0x123)) === selected, "recovery cached a failed identity");
    ensure(parentCloses === parentOpens - 1, "successful parent leaked");
  } else throw Error("unknown instance scenario");
  return true;
};

rpc.exports.systemimage = function (path) {
  Process.findModuleByAddress = nativeModuleLookup;
  sourceApi.lookup = nativeFunctionLookup;
  sourceApi.query = nativeQuery;
  sourceCallsites.clear(); sourceVtables.clear();
  const load = new NativeFunction(Process.getModuleByName("kernel32.dll").getExportByName("LoadLibraryExW"), "pointer", ["pointer", "pointer", "uint"]);
  const base = load(Memory.allocUtf16String(path), ptr(0), 1); // map only; never run WUDFHost
  ensure(!base.isNull(), "cannot map Windows image in test child");
  const module = {base, size: 0x4b000, name: "WUDFHost.exe"};
  const loadInfo = deviceStackLoad(module, base.add(0x11497));
  ensure(loadInfo && loadInfo.base === "rbp" && loadInfo.disp === 0xc8, "real caller instruction stream not supported");
  const object = Memory.alloc(512);
  object.writePointer(base.add(0x35b68)); object.add(0x40).writePointer(ptr(0x1234));
  const key = deviceRegistryKey(module, object);
  ensure(key && key.equals(ptr(0x1234)), "real UMDF interface/getter not supported");
  return true;
};
"""


@unittest.skipUnless(os.name == "nt" and importlib.util.find_spec("frida"), "requires Windows Frida runtime")
class DeviceSourceRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import frida

        cls.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        cls.session = None
        cls.addClassCleanup(cls.cleanup)
        cls.session = frida.attach(cls.child.pid)
        source = GADGET_SCRIPT
        for target, callback in (("target", "copyCallbacks"), ("closeTarget", "closeCallbacks"), ("deviceTarget", "deviceCallbacks")):
            prefix = "if (deviceTarget !== null) " if target == "deviceTarget" else ""
            source = source.replace(f"{prefix}Interceptor.attach({target}, {{", f"globalThis.{callback} = ({{")
        cls.script = cls.session.create_script(source + "\n" + HARNESS + "\n" + SOURCE_HARNESS)
        cls.errors = []
        cls.script.on("message", lambda message, _data: cls.errors.append(message) if message.get("type") == "error" else None)
        cls.script.load()
        if cls.errors:
            raise AssertionError(cls.errors)

    @classmethod
    def cleanup(cls):
        if cls.session is not None:
            cls.session.detach()
        cls.child.terminate()
        cls.child.wait(timeout=5)

    def test_source_hash_matches_persisted_physical_identity(self):
        from ovb_rc003.remote_selection import container_key

        expected = [container_key(uuid.UUID(value)) for value in (
            "11223344-5566-7788-99aa-bbccddeeff00", "22334455-6677-8899-aabb-ccddeeff0011")]
        self.assertEqual(self.script.exports_sync.sourcekeys(), expected)

    def test_shared_host_copy_routing_and_lifecycle(self):
        selected = hashlib.sha256(b"RemoteMic physical remote v1\0" + uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00").bytes).hexdigest()
        for scenario in ("two_devices", "first_selected_press", "first_selected_press_failure",
                         "unknown_source", "exclusive_fallback", "known_other_beats_fallback",
                         "rejected_fallback_recovers", "bound_source_lost",
                         "closed_reused_handle", "changed_selection_inflight", "missing_or_bad_selection"):
            with self.subTest(scenario=scenario):
                self.assertTrue(self.script.exports_sync.sourcescenario(scenario, selected))
        self.assertEqual(self.errors, [])

    def test_source_proof_rejects_wrong_object_callsite_and_nested_call(self):
        self.assertTrue(self.script.exports_sync.sourcevalidation())

    def test_device_parameters_resolves_only_its_own_instance(self):
        from ovb_rc003.remote_selection import container_key

        selected = container_key(uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00"))
        for scenario in ("two_parameters", "wrong_subkey_identity", "bad_path", "failures_recover"):
            with self.subTest(scenario=scenario):
                self.assertTrue(self.script.exports_sync.instanceidentity(scenario, selected))
        self.assertEqual(self.errors, [])

    def test_verified_windows_image_uses_actual_unwind_interface_and_getter(self):
        path = Path(os.environ["SystemRoot"]) / "System32" / "WUDFHost.exe"
        if hashlib.sha256(path.read_bytes()).hexdigest() != "104e8aa800b22047d9af7479ffc4e26cde7518d3c2cb180193a4a3d2797ad004":
            self.skipTest("system image differs from audited fixture; no RVA assumptions made")
        self.assertTrue(self.script.exports_sync.systemimage(str(path)))


if __name__ == "__main__":
    unittest.main()
