"""Execute Gadget callbacks against an isolated, deterministic kernel-copy model.

No keyboard events are injected. The child exists only to run the actual Frida
JavaScript runtime; its I/O and close interceptors are replaced with callbacks.
"""

import importlib.util
import os
import subprocess
import sys
import time
import unittest

from ovb_rc003.frida_hid_tap_runtime import GADGET_SCRIPT


HARNESS = r"""
const records = [];
emit = payload => records.push(payload);
installHook();
const NEUTRAL = "010000000000000000";
const RIGHT = "0100004f0000000000";
function ensure(condition, detail) { if (!condition) throw Error(detail); }
function invoke(raw, options = {}) {
  const metadata = Memory.alloc(8);
  metadata.writeU32(123);
  metadata.add(4).writeU8(options.operation === undefined ? 2 : options.operation);
  metadata.add(5).writeU8(options.selector === undefined ? 1 : options.selector);
  const buffer = Memory.alloc(9);
  buffer.writeByteArray(raw.match(/../g).map(x => parseInt(x, 16)));
  const args = [ptr(options.handle || 0x123),ptr(0),ptr(0),ptr(0),ptr(0),ptr(0x80018483),
                metadata,ptr(8),buffer,ptr(options.length || 9)];
  const context = {context: null};
  let outer = null;
  if (options.sourceContext) {
    outer = {context: options.sourceContext, returnAddress: options.returnAddress};
    globalThis.deviceCallbacks.onEnter.call(outer, [args[0], args[5], args[6], args[7], args[8], args[9]]);
  }
  const start = records.length;
  globalThis.copyCallbacks.onEnter.call(context, args);
  const kernelCopy = hex(buffer, 9);
  if (options.beforeReturn) options.beforeReturn();
  globalThis.copyCallbacks.onLeave.call(context, ptr(options.status || 0));
  if (outer) globalThis.deviceCallbacks.onLeave.call(outer, ptr(options.status || 0));
  return {kernelCopy, afterReturn: hex(buffer, 9), records: records.slice(start)};
}
function bindNeutral() {
  const initial = invoke(NEUTRAL);
  const candidate = initial.records.find(r => r.kind === "copy_candidate");
  ensure(candidate !== undefined, "missing pre-binding candidate");
  handleControl({...candidate, kind: "intercept_control", action: "bind_copy_handle"});
  ensure(boundCopyHandle === "0x123", "binding rejected");
  return candidate;
}
rpc.exports.exercise = function (scenario) {
  records.length = 0;
  resetCopyOwnership();
  selectedSourceKey = null;
  output = {};
  interceptLeaseDeadline = Date.now() + 10000;
  copyProbeDeadline = 0;
  resetCopyHealth();
  healthDiagnosticsEnabled = true;
  if (scenario === "candidate_failure") {
    const result = invoke(RIGHT, {status: 0xc0000001});
    const failure = result.records.find(r => r.kind === "copy_failure");
    ensure(failure && failure.report_owned === false && failure.restored === null, "candidate failure evidence missing");
    ensure(result.kernelCopy === RIGHT && result.afterReturn === RIGHT, "candidate failure modified native source");
    ensure(!result.records.some(r => r.kind === "copy_candidate" || r.kind === "gatt_read"), "failed candidate was forwarded");
    return true;
  }
  if (scenario === "unbound_hold") {
    const first = invoke(RIGHT);
    ensure(first.kernelCopy === RIGHT, "unverified handle was modified");
    const candidate = first.records.find(r => r.kind === "copy_candidate");
    handleControl({...candidate, kind: "intercept_control", action: "bind_copy_handle"});
    const held = invoke(RIGHT);
    ensure(held.kernelCopy === RIGHT && !held.records.some(r => r.kind === "gatt_read"), "preexisting hold was taken over");
    invoke(NEUTRAL);
    ensure(invoke(RIGHT).kernelCopy === NEUTRAL, "new hold not intercepted");
    return true;
  }
  const candidate = bindNeutral();
  if (scenario === "kernel_copy") {
    const press = invoke(RIGHT);
    const mapped = press.records.filter(r => r.kind === "gatt_read");
    ensure(press.kernelCopy === NEUTRAL, "kernel received native press before late clearing");
    ensure(mapped.length === 1 && mapped[0].raw === RIGHT, "original report not delivered exactly once");
    ensure(copyHealthSnapshot().intercepted_reports === 1, "missing successful-copy count");
    ensure(invoke(NEUTRAL).kernelCopy === NEUTRAL, "release changed");
  } else if (scenario === "other_handle") {
    const result = invoke(RIGHT, {handle: 0x456});
    ensure(result.kernelCopy === RIGHT && result.records.length === 0, "other device handle was intercepted");
  } else if (scenario === "read_direction" || scenario === "other_selector" || scenario === "wrong_length") {
    const options = scenario === "read_direction" ? {operation: 1} : scenario === "other_selector" ? {selector: 0} : {length: 8};
    const result = invoke(RIGHT, options);
    ensure(result.kernelCopy === RIGHT && result.records.length === 0, "unrelated copy was intercepted");
  } else if (scenario === "failed_copy") {
    const result = invoke(RIGHT, {status: 0xc0000001});
    ensure(result.afterReturn === RIGHT, "failed copy did not restore original source");
    ensure(!result.records.some(r => r.kind === "gatt_read"), "failed copy triggered mapping");
    const failure = result.records.find(r => r.kind === "copy_failure");
    ensure(failure && failure.ntstatus === 0xc0000001 && failure.restored === true, "missing failure evidence");
    ensure(!invoke(RIGHT, {status: 0xc0000001}).records.some(r => r.kind === "copy_failure"), "failure log is not bounded");
    ensure(copyHealthSnapshot().copy_failures === 2, "failure count lost");
  } else if (scenario === "legacy_client") {
    healthDiagnosticsEnabled = false;
    const result = invoke(RIGHT, {status: 0xc0000001});
    ensure(!result.records.some(r => r.kind === "copy_failure"), "new message sent to old client");
    ensure(result.afterReturn === RIGHT, "old-client restoration changed");
  } else if (scenario === "entry_failure") {
    const intercept = interceptOutgoingCopy;
    try {
      interceptOutgoingCopy = () => { throw Error("private-content"); };
      const result = invoke(RIGHT);
      ensure(result.records.some(r => r.kind === "error" && r.code === "copy_entry_exception"), "lost entry error code");
      ensure(!JSON.stringify(result.records).includes("private-content"), "exception content leaked");
    } finally { interceptOutgoingCopy = intercept; }
  } else if (scenario === "counter_reset") {
    invoke(RIGHT);
    handleControl({kind: "intercept_control", action: "renew", protocol: 4, lease_ms: 2000});
    ensure(copyHealth.intercepted_reports === 1, "renewal reset health counters");
    handleControl({kind: "intercept_control", action: "enable", protocol: 4, lease_ms: 2000, health_diagnostics: 1});
    ensure(copyHealth.intercepted_reports === 0 && boundCopyHandle === null, "new lease mixed old counters");
  } else if (scenario === "expired_lease") {
    interceptLeaseDeadline = Date.now() - 1;
    const result = invoke(RIGHT);
    ensure(result.kernelCopy === RIGHT && !result.records.some(r => r.kind === "gatt_read"), "expired lease still intercepts");
  } else if (scenario === "closed_handle") {
    globalThis.closeCallbacks.onEnter.call({}, [ptr(0x123)]);
    ensure(boundCopyHandle === null, "closed handle remained owned");
    ensure(records.some(r => r.kind === "error" && r.code === "bound_copy_handle_closed"), "lost close reason");
    handleControl({...candidate, kind: "intercept_control", action: "bind_copy_handle"});
    ensure(boundCopyHandle === null && invoke(RIGHT).kernelCopy === RIGHT, "stale binding consumed reused handle");
  } else throw Error("unknown scenario");
  return true;
};
"""


@unittest.skipUnless(os.name == "nt" and importlib.util.find_spec("frida"), "requires Windows Frida runtime")
class CopyInterceptionRuntimeTests(unittest.TestCase):
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
        source = GADGET_SCRIPT.replace("Interceptor.attach(target, {", "globalThis.copyCallbacks = ({")
        source = source.replace("Interceptor.attach(closeTarget, {", "globalThis.closeCallbacks = ({")
        source = source.replace("Interceptor.attach(deviceTarget, {", "globalThis.deviceCallbacks = ({")
        cls.script = cls.session.create_script(source + "\n" + HARNESS)
        cls.errors = []
        cls.script.on("message", lambda message, _data: cls.errors.append(message) if message.get("type") == "error" else None)
        cls.script.load()
        time.sleep(0.05)
        if cls.errors:
            raise AssertionError(cls.errors)

    @classmethod
    def cleanup(cls):
        if cls.session is not None:
            cls.session.detach()
        cls.child.terminate()
        cls.child.wait(timeout=5)

    def test_actual_callbacks_obey_copy_ownership_and_recovery(self):
        for scenario in (
            "kernel_copy", "other_handle", "read_direction", "other_selector",
            "wrong_length", "failed_copy", "expired_lease", "closed_handle", "unbound_hold",
            "legacy_client", "entry_failure", "counter_reset",
            "candidate_failure",
        ):
            with self.subTest(scenario=scenario):
                self.assertTrue(self.script.exports_sync.exercise(scenario))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
