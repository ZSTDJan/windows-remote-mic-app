// Execute the real injected script with synthetic COM buffers, never a driver.
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const messages = [];
let callback, copyHook, timer, reads = 0, releases = 0, copyReads = 0, copyStatusReads = 0;
const nil = {isNull: () => true};
const alloc = () => ({writeByteArray() {}, readU32() {return this.n;},
                      readPointer() {return this.p;}});
const iface = table => ({isNull: () => false, readPointer: () => ({
  add: offset => ({readPointer: () => table[offset / 8]})})});
const context = {
  Process: {pointerSize: 8, findModuleByName: name => name === 'ntdll.dll'
    ? {findExportByName: () => 'copy-target'}
    : input.missingModule ? null : {path: input.modulePath || 'test.dll', size: 65536, base: {add() {return {};}}}},
  Memory: {alloc}, ptr: () => nil,
  NativeFunction: function (fn) {return fn;},
  Interceptor: {attach(target, handlers) {
    if (target === 'copy-target') {
      if (input.failCopyHook) throw new Error('copy hook unavailable');
      copyHook = handlers;
    }
    else {
      if (input.failMainHook) throw new Error('main hook unavailable');
      callback = handlers.onEnter;
    }
  }},
  send(item) {
    if (input.failDiagnostics && ['hid_flow', 'hid_setup'].includes(item.kind)) throw new Error('sink unavailable');
    messages.push(JSON.parse(JSON.stringify(item)));
  },
  setInterval(fn, delay) {if (delay !== 2000) throw new Error('unbounded timer'); timer = fn;},
};
try { vm.runInNewContext(input.script, context, {timeout: 3000}); }
catch (error) {
  if (!input.expectStartupFailure) throw error;
  process.stdout.write(JSON.stringify({messages, startupFailed:true}));
  process.exit(0);
}
for (const test of input.cases) {
  const access = iface({
    3: (_self, out) => {
      if (test.dataError) return -1;
      out.p = {readByteArray(size) {
        reads++;
        if (test.throwData) throw new Error('data unavailable');
        if (size !== (test.length === undefined ? 8 : test.length)) throw new Error('wrong buffer read size');
        return (test.bytes || [3, 0, 0, 0, 0, 0, 0, 0]).slice(0, size);
      }};
      return 0;
    },
    2: () => {releases++; if (test.throwRelease) throw new Error('release unavailable'); return 0;},
  });
  const buffer = test.nullBuffer ? nil : iface({
    7: (_self, out) => {out.n = test.length === undefined ? 8 : test.length; return test.lengthError ? -1 : 0;},
    0: (_self, _iid, out) => {out.p = access; return test.accessError ? -1 : 0;},
  });
  const adapter = {add: offset => ({readByteArray(size) {
    if (test.throwAddress) throw new Error('address unavailable');
    if (size !== 6) throw new Error('unexpected address read length');
    if (offset !== (test.addressOffset === undefined ? 24 : test.addressOffset)) return [0, 0, 0, 0, 0, 0];
    return test.otherAddress ? [9, 9, 9, 9, 9, 9] : [1, 2, 3, 4, 5, 6];
  }})};
  for (let n = 0; n < (test.repeat || 1); n++) callback([adapter, nil, buffer]);
}
for (const test of (input.copyCases || [])) {
  const capacity = test.capacity === undefined ? 3 : test.capacity;
  const statusBlock = test.nullStatus ? nil : {
    isNull: () => false,
    readU32() {copyStatusReads++; return test.status || 0;},
    add: () => ({readU32() {copyStatusReads++; return test.information || 0;},
      add: () => ({readU32() {copyStatusReads++; return test.informationHigh || 0;}})}),
  };
  const output = test.nullOutput ? nil : {
    isNull: () => false,
    readByteArray() {copyReads++; if (test.throwRead) throw new Error('read failed');
                     return test.bytes || [2, 0x42, 0];},
  };
  const args = Array(10).fill(nil);
  args[4] = statusBlock;
  args[5] = {toUInt32: () => test.ioctl === undefined ? 0x80018483 : test.ioctl};
  args[8] = output;
  args[9] = {toUInt32: () => capacity};
  const frame = {};
  copyHook.onEnter.call(frame, args);
  copyHook.onLeave.call(frame, {toUInt32: () => test.result || 0});
}
const beforeTimer = messages.length;
timer();
process.stdout.write(JSON.stringify({messages, reads, releases, copyReads, copyStatusReads, beforeTimer}));
