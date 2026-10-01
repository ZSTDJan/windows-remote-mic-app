"""Read-only evidence around Frida attach, owned by one bounded sampler.

No impersonation, privilege/ACL changes, remote code or device operations. Keep
one process handle across attach so an exited/reused PID cannot look healthy.
Temporary agents are candidates, NOT proven injection paths. AccessCheck reports
the candidate file's DACL decision for the actual target token, not loader success.
Raw paths, SIDs, arbitrary error messages and remote memory are never exported.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import threading
import time

from .chromecast_pipe_windows import _bind, api


BEFORE_WAIT_SECONDS = .25
AFTER_WAIT_SECONDS = .75
_SAMPLER_SLOT = threading.BoundedSemaphore(1)
_HELPER_SLOT = threading.BoundedSemaphore(1)


class _ProcessEntry(C.Structure):
    _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                ('name', W.WCHAR * 260)]


class HelperLifecycle:
    """Observe likely Frida helpers during attach without changing them."""
    def __init__(self, emit):
        self.emit = emit
        self.stop_requested = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.samples = self.query_errors = 0
        self.limited = False
        self.candidates = {}
        self.owner_pid = os.getpid()

    def start(self):
        if not _HELPER_SLOT.acquire(blocking=False):
            self.emit('attach_helper_scan', 'failed',
                      details=dict(sample_count=0, scan_errors=0, scan_limited=1, query_complete=0))
            return
        try:
            self.thread = threading.Thread(target=self._run, name='chromecast-helper-observation', daemon=True)
            self.thread.start()
        except Exception:
            self.thread = None
            _HELPER_SLOT.release()
            raise
        if not self.ready.wait(.05):
            self.limited = True  # A delayed first snapshot could miss a short-lived helper.

    def finish(self):
        if self.thread is None:
            return
        self.stop_requested.set()
        self.thread.join(.25)
        complete = not self.thread.is_alive() and not self.limited
        self.emit('attach_helper_scan', 'success' if complete and not self.query_errors else 'failed',
                  details=dict(sample_count=self.samples, scan_errors=self.query_errors,
                               scan_limited=int(self.limited), query_complete=int(complete and not self.query_errors)))
        if self.thread.is_alive():
            return  # The daemon owns and closes its handles; never inspect a live collection.
        managers = [item for item in self.candidates.values() if item['role'] == 'manager']
        manager_paths = {item['path'] for item in managers}
        services = [item for item in self.candidates.values()
                    if item['role'] == 'service' and item['path'] in manager_paths]
        for role, items in (('manager', managers), ('service', services)):
            details = dict(helper_count=len(items), helper_pid=-1, created_low=-1, created_high=-1,
                           alive=-1, exit_code=-1)
            if len(items) == 1:
                item = items[0]
                details.update(helper_pid=item['pid'], created_low=item['created'] & 0xffffffff,
                               created_high=item['created'] >> 32, alive=item['alive'],
                               exit_code=item['exit_code'])
            self.emit('attach_helper_' + role, 'success' if self.samples and not self.query_errors
                      and not self.limited else 'failed',
                      details=details)

    def _run(self):
        try:
            self.k, _ = api()
            _bind(self.k, 'CreateToolhelp32Snapshot', W.HANDLE, W.DWORD, W.DWORD)
            _bind(self.k, 'Process32FirstW', W.BOOL, W.HANDLE, C.POINTER(_ProcessEntry))
            _bind(self.k, 'Process32NextW', W.BOOL, W.HANDLE, C.POINTER(_ProcessEntry))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    self._sample()
                except Exception:
                    self.query_errors = min(0xffffffff, self.query_errors + 1)
                self.ready.set()
                if self.stop_requested.wait(.05):
                    break
            else:
                self.limited = True
        except Exception:
            self.query_errors = min(0xffffffff, self.query_errors + 1)
            self.ready.set()
        finally:
            try:
                for item in self.candidates.values():
                    item['alive'], item['exit_code'] = -1, -1
                    try:
                        wait = self.k.WaitForSingleObject(item['handle'], 0) if item['sync'] else None
                        code = W.DWORD()
                        if wait not in (None, 0, 258) or not self.k.GetExitCodeProcess(item['handle'], C.byref(code)):
                            self.query_errors = min(0xffffffff, self.query_errors + 1)
                        elif code.value != 259 or wait == 0:
                            item.update(alive=0, exit_code=code.value)
                        elif wait == 258:
                            item['alive'] = 1
                        else:
                            self.query_errors = min(0xffffffff, self.query_errors + 1)
                    except Exception:
                        self.query_errors = min(0xffffffff, self.query_errors + 1)
                    finally:
                        self.k.CloseHandle(item['handle'])
            finally:
                self.ready.set()
                _HELPER_SLOT.release()

    def _sample(self):
        snapshot = self.k.CreateToolhelp32Snapshot(2, 0)
        if snapshot in (None, -1, C.c_void_p(-1).value):
            raise C.WinError(C.get_last_error())
        rows = []
        try:
            entry = _ProcessEntry()
            entry.size = C.sizeof(entry)
            if not self.k.Process32FirstW(snapshot, C.byref(entry)):
                raise C.WinError(C.get_last_error())
            while True:
                rows.append((entry.pid, entry.parent, entry.name.casefold()))
                if len(rows) >= 8192:
                    self.limited = True
                    break
                entry.size = C.sizeof(entry)
                C.set_last_error(0)
                if not self.k.Process32NextW(snapshot, C.byref(entry)):
                    code = C.get_last_error()
                    if code != 18:  # ERROR_NO_MORE_FILES is the sole normal end.
                        raise C.WinError(code)
                    break
        finally:
            self.k.CloseHandle(snapshot)
        self.samples = min(0xffffffff, self.samples + 1)
        service_pids = {pid for pid, _, name in rows if name == 'services.exe'}
        for role in ('manager', 'service'):
            managers = [item for item in self.candidates.values() if item['role'] == 'manager']
            manager_pids = {item['pid'] for item in managers}
            for pid, parent, name in rows:
                if name != 'frida-helper-x86_64.exe' or pid in self.candidates:
                    continue
                if role == 'manager' and parent != self.owner_pid:
                    continue
                # Frida uses a child of the manager normally, or services.exe
                # when it can register an elevated Windows service.
                if role == 'service' and (not managers or
                                          (parent not in manager_pids and parent not in service_pids)):
                    continue
                if len(self.candidates) >= 8:
                    self.limited = True
                    return
                handle = self.k.OpenProcess(0x101000, False, pid)
                sync = bool(handle)
                if not handle:
                    handle = self.k.OpenProcess(0x1000, False, pid)
                if not handle:
                    self.query_errors = min(0xffffffff, self.query_errors + 1)
                    continue
                try:
                    buffer = C.create_unicode_buffer(32768)
                    size = W.DWORD(len(buffer))
                    created, exited, kernel, user = (W.FILETIME() for _ in range(4))
                    if not (self.k.QueryFullProcessImageNameW(handle, 0, buffer, C.byref(size))
                            and self.k.GetProcessTimes(handle, C.byref(created), C.byref(exited),
                                                       C.byref(kernel), C.byref(user))):
                        self.query_errors = min(0xffffffff, self.query_errors + 1)
                        continue
                    path = buffer.value.casefold()
                    born = (created.dwHighDateTime << 32) | created.dwLowDateTime
                    if role == 'service' and not any(item['path'] == path and born >= item['created']
                                                     for item in managers):
                        continue
                    self.candidates[pid] = dict(role=role, pid=pid, path=path,
                                                created=born, handle=handle, sync=sync)
                    handle = None
                finally:
                    if handle:
                        self.k.CloseHandle(handle)

def record_exception(error, emit):
    """Keep the original error even when native evidence sampling times out."""
    seen = set()
    for depth in range(4):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        emit('attach_exception', 'failed', details=dict(exception_details(error), chain_depth=depth), error=error)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)


class BoundedAttachEvidence:
    """One daemon owns all native handles; the attach caller never joins it.

    A blocked Windows call cannot be cancelled in Python. On timeout, suppress
    late records and stop at the next query boundary. Retain the process-wide
    slot until that thread closes its handles, so retries cannot leak threads.
    The HID worker process owns the final lifetime of a permanently stuck call.
    """
    def __init__(self, pid, emit):
        self.pid, self.emit = pid, emit
        self.cancelled = threading.Event()
        self.before_done = threading.Event()
        self.after_requested = threading.Event()
        self.done = threading.Event()
        self.record_lock = threading.Lock()
        self.thread = None
        self.helpers = None
        self.failed_phases = set()

    def _emit(self, *args, **kwargs):
        with self.record_lock:
            if not self.cancelled.is_set():
                self.emit(*args, **kwargs)

    def _status(self, phase, outcome, error=None):
        self.emit('attach_evidence', 'success' if outcome == 'finished' else 'failed', error=error,
                  details=dict(sample_phase=phase, sample_outcome=outcome,
                               query_complete=int(outcome == 'finished')))

    def _wait(self, event, seconds, phase):
        if event.wait(seconds):
            self._status(phase, 'failed' if phase in self.failed_phases else 'finished')
            return
        with self.record_lock:
            self.cancelled.set()
        self.after_requested.set()
        self._status(phase, 'timeout', TimeoutError('attach evidence deadline'))

    def begin(self):
        if not _SAMPLER_SLOT.acquire(blocking=False):
            self._status('before', 'busy')
            return
        try:
            self.thread = threading.Thread(target=self._run, name='chromecast-attach-evidence', daemon=True)
            self.thread.start()
        except Exception:
            self.thread = None
            _SAMPLER_SLOT.release()
            raise
        self._wait(self.before_done, BEFORE_WAIT_SECONDS, 'before')
        try:
            self.helpers = HelperLifecycle(self.emit)
            self.helpers.start()
        except Exception as error:
            self.helpers = None
            self.emit('attach_helper_scan', 'failed', error=error,
                      details=dict(sample_count=0, scan_errors=1, scan_limited=0, query_complete=0))

    def finish(self, error=None):
        try:
            record_exception(error, self.emit)
        finally:
            self.after_requested.set()
            if self.helpers is not None:
                try:
                    self.helpers.finish()
                except Exception as helper_error:
                    self.emit('attach_helper_scan', 'failed', error=helper_error,
                              details=dict(sample_count=0, scan_errors=1, scan_limited=0, query_complete=0))
        if self.thread is not None and not self.cancelled.is_set():
            self._wait(self.done, AFTER_WAIT_SECONDS, 'after')

    def _run(self):
        evidence = None
        phase = 'before'
        try:
            evidence = AttachEvidence(self.pid, self._emit, cancelled=self.cancelled.is_set)
            try:
                evidence.begin()
                if evidence.incomplete:
                    self.failed_phases.add('before')
            except Exception as error:
                self.failed_phases.add('before')
                self._emit('attach_evidence', 'failed', error=error)
            finally:
                self.before_done.set()
            self.after_requested.wait()
            phase = 'after'
            if not self.cancelled.is_set():
                evidence.finish()
                if evidence.incomplete:
                    self.failed_phases.add('after')
        except Exception as error:
            self.failed_phases.add(phase)
            self._emit('attach_evidence', 'failed', error=error, details={'sample_phase': phase})
        finally:
            try:
                if evidence is not None:
                    evidence.close()
            except Exception as error:
                self.failed_phases.add('after')
                self._emit('attach_evidence', 'failed', error=error)
            finally:
                self.before_done.set()
                _SAMPLER_SLOT.release()
                self.done.set()


def exception_details(error):
    """Preserve error identity and known Frida distinctions without raw text."""
    text = str(error)[:4096]
    family = 'unclassified'
    if 'refused to load frida-agent, or terminated during injection' in text:
        family = 'agent_load_or_process_exit'
    elif text.strip().lower() == 'the connection is closed':
        family = 'connection_closed'
    elif 'unable to communicate with remote frida-server' in text.lower():
        family = 'agent_transport'
    elif 'access is denied' in text.lower() or 'access denied' in text.lower():
        family = 'access_denied'
    elif 'timed out' in text.lower() or 'timeout' in text.lower():
        family = 'timeout'
    elif 'process not found' in text.lower():
        family = 'process_missing'
    result = dict(error_family=family, error_sha256=hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest(),
                  error_chars=len(text), error_clipped=int(len(str(error)) > 4096))
    # Only known native error forms. A PID, address or number elsewhere is NOT a code.
    match = re.search(r'\b(OpenProcess|CreateRemoteThread|VirtualAllocEx|WriteProcessMemory|LoadLibraryW)\b.*?\b(?:error|code)\s*[=:]\s*(0x[0-9a-fA-F]+|[0-9]+)\b', text)
    if match:
        number = int(match[2], 16 if match[2].startswith('0x') else 10)
        if number <= 0xffffffff:
            result.update(native_api=match[1], reported_code=number)
    return result


def agent_candidates():
    """Bounded local directory scan; never recurse, follow links or scan drives."""
    roots = {os.environ.get(key, '') for key in ('TEMP', 'TMP', 'TMPDIR')}
    roots.add(tempfile.gettempdir())
    program_files = os.environ.get('ProgramFiles')
    # Frida's elevated helper copies the DLL directly into a SHA1 directory;
    # ordinary extraction uses frida-*/x86_64. These are distinct layouts.
    locations = ([(str(Path(program_files) / 'Frida'), True)] if program_files else [])
    locations.extend((root, False) for root in sorted(roots - {''}))
    result, errors, limited = {}, 0, False
    deadline = time.monotonic() + .15
    for root, elevated in locations:
        path = Path(root)
        if not path.is_absolute() or str(path).startswith('\\\\'):
            errors += 1
            continue
        try:
            if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                errors += 1
                continue
            with os.scandir(path) as entries:
                for index, entry in enumerate(entries):
                    if index >= 2048 or time.monotonic() > deadline or len(result) >= 16:
                        limited = True
                        break
                    matches = (re.fullmatch(r'[0-9a-fA-F]{40}', entry.name) is not None
                               if elevated else entry.name.lower().startswith('frida-'))
                    if not matches or not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    parent = Path(entry.path) if elevated else Path(entry.path) / 'x86_64'
                    candidate = parent / 'frida-agent.dll'
                    try:
                        info = candidate.lstat()
                        if (not stat.S_ISREG(info.st_mode) or info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                                or parent.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                            continue
                        result[candidate] = (info.st_size, info.st_mtime_ns)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        errors += 1
        except FileNotFoundError:
            pass
        except OSError:
            errors += 1
    return result, dict(scan_errors=errors, scan_limited=int(limited), candidates=len(result))


def agent_file(path, *, cancelled=lambda: False):
    """Fingerprint a bounded candidate, including malformed/truncated PE files."""
    with path.open('rb') as source:
        before = os.fstat(source.fileno())
        header = source.read(65536)
        result = dict(size=min(0xffffffff, before.st_size), pe_valid=0, machine=-1, hash_complete=0)
        if len(header) >= 64 and header[:2] == b'MZ':
            offset = struct.unpack_from('<I', header, 60)[0]
            if offset + 24 <= len(header) and header[offset:offset+4] == b'PE\0\0':
                result.update(pe_valid=1, machine=struct.unpack_from('<H', header, offset+4)[0])
        if before.st_size <= 64*1024*1024:
            source.seek(0)
            digest, remaining = hashlib.sha256(), before.st_size
            while remaining:
                if cancelled():
                    break
                block = source.read(min(1024*1024, remaining))
                if not block:
                    break
                digest.update(block)
                remaining -= len(block)
            result.update(hash_complete=int(remaining == 0), agent_sha256=digest.hexdigest())
        after = os.fstat(source.fileno())
        result['file_unchanged'] = int((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns))
        return result


class AttachEvidence:
    def __init__(self, pid, emit, *, cancelled=lambda: False):
        self.pid, self.emit = pid, emit
        self.cancelled = cancelled
        self.k = self.a = self.process = None
        self.token = W.HANDLE()
        self.before = {}
        self.born = None
        self.incomplete = False

    def _record(self, step, details=None, error=None):
        # Query failure is explicitly separate from the attach operation's failure.
        details = dict(details or {})
        if error is not None:
            outcome = 'failed'
        elif details.get('process_handle') == 0 or details.get('token_available') == 0 or details.get('process_changed') == 1:
            outcome = 'unavailable'
        elif (details.get('query_complete') == 0
              or any(value > 0 for key, value in details.items() if key.endswith('_error'))
              or any(details.get(key, 0) > 0 for key in
                     ('scan_errors', 'scan_limited', 'omitted_candidates', 'module_limit', 'module_name_errors'))
              or details.get('hash_complete') == 0 or details.get('file_unchanged') == 0):
            outcome = 'partial'
        else:
            outcome = 'captured'
        complete = outcome == 'captured'
        self.incomplete |= not complete
        details.update(query_outcome=outcome, query_complete=int(complete))
        self.emit(step, 'success' if complete else 'failed', details=details, error=error)

    def _query(self, step, operation):
        if self.cancelled():
            return
        started = time.monotonic()
        try:
            result = operation()
            self._record(step, dict(result, query_ms=min(0xffffffff, round((time.monotonic()-started)*1000))))
        except Exception as error:
            self._record(step, error=error)

    def begin(self):
        self.incomplete = False
        self._query('attach_controller', self._controller)
        self._query('attach_process_before', self._open)
        self._query('attach_policy', self._policy)
        self._query('attach_modules_before', self._modules)
        self._query('attach_assets_before', self._assets_before)

    def _controller(self):
        from .input_environment_diagnostics_windows import process_security
        row = process_security(os.getpid())
        result = {key: int(row[key]) if row.get(key) is not None else -1
                  for key in ('elevated', 'integrity_rid', 'session_id')}
        result['query_complete'] = int(row['status'] == 'captured')
        import frida
        version = getattr(frida, '__version__', '')
        if isinstance(version, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', version):
            result['frida_version'] = version
        return result

    def _open(self):
        self.k, self.a = api()
        self.process = self.k.OpenProcess(0x1000 | 0x100000, False, self.pid)
        if not self.process:
            raise C.WinError(C.get_last_error())
        times = [W.FILETIME() for _ in range(4)]
        if not self.k.GetProcessTimes(self.process, *[C.byref(item) for item in times]):
            raise C.WinError(C.get_last_error())
        self.born = times[0].dwLowDateTime | (times[0].dwHighDateTime << 32)
        result = self._state()
        result.update(created_low=times[0].dwLowDateTime, created_high=times[0].dwHighDateTime)
        name, size = C.create_unicode_buffer(32768), W.DWORD(32768)
        if self.k.QueryFullProcessImageNameW(self.process, 0, name, C.byref(size)):
            expected = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'/'WUDFHost.exe'
            result['system_wudfhost'] = int(os.path.normcase(name.value) == os.path.normcase(str(expected)))
        else:
            result['image_error'] = C.get_last_error()
        self._query('attach_token', self._open_token)
        return result

    def _open_token(self):
        token = W.HANDLE()
        if not self.a.OpenProcessToken(self.process, 8 | 2, C.byref(token)):
            raise C.WinError(C.get_last_error())
        try:
            _bind(self.a, 'DuplicateToken', W.BOOL, W.HANDLE, C.c_int, C.POINTER(W.HANDLE))
            if not self.a.DuplicateToken(token, 2, C.byref(self.token)):
                raise C.WinError(C.get_last_error())
            _bind(self.a, 'IsTokenRestricted', W.BOOL, W.HANDLE)
            result = {'restricted': int(self.a.IsTokenRestricted(token))}
            for field, kind in (('elevated', 20), ('session_id', 12), ('app_container', 29)):
                value, needed = W.DWORD(), W.DWORD()
                if self.a.GetTokenInformation(token, kind, C.byref(value), C.sizeof(value), C.byref(needed)):
                    result[field] = value.value
                else:
                    result[field] = -1
                    result[field + '_error'] = C.get_last_error()
            return result
        finally:
            self.k.CloseHandle(token)

    def _state(self):
        if not self.process:
            return {'process_handle': 0, 'alive': -1}
        code = W.DWORD()
        if not self.k.GetExitCodeProcess(self.process, C.byref(code)):
            raise C.WinError(C.get_last_error())
        wait = self.k.WaitForSingleObject(self.process, 0)
        if wait == 0xffffffff:
            raise C.WinError(C.get_last_error())
        return dict(process_handle=1, alive=int(wait == 258), exit_code=code.value)

    def _policy(self):
        if not self.process:
            return {'process_handle': 0}
        _bind(self.k, 'GetProcessMitigationPolicy', W.BOOL, W.HANDLE, C.c_int, C.c_void_p, C.c_size_t)
        result = {}
        for name, policy in (('dynamic_code', 2), ('extension_points', 6), ('signature_policy', 8)):
            value = W.DWORD()
            if self.k.GetProcessMitigationPolicy(self.process, policy, C.byref(value), C.sizeof(value)):
                result[name] = value.value
            else:
                result[name] = -1
                result[name+'_error'] = C.get_last_error()
        return result

    def _modules(self):
        if self.k is None or self.born is None:
            return {'process_handle': 0}
        handle = self.k.OpenProcess(0x400 | 0x10, False, self.pid)  # QUERY_INFORMATION | VM_READ
        if not handle:
            raise C.WinError(C.get_last_error())
        try:
            times = [W.FILETIME() for _ in range(4)]
            if not self.k.GetProcessTimes(handle, *[C.byref(item) for item in times]):
                raise C.WinError(C.get_last_error())
            if self.born != times[0].dwLowDateTime | (times[0].dwHighDateTime << 32):
                return {'process_changed': 1}
            psapi = C.WinDLL('psapi', use_last_error=True)
            _bind(psapi, 'EnumProcessModulesEx', W.BOOL, W.HANDLE, C.POINTER(W.HMODULE), W.DWORD, C.POINTER(W.DWORD), W.DWORD)
            _bind(psapi, 'GetModuleFileNameExW', W.DWORD, W.HANDLE, W.HMODULE, W.LPWSTR, W.DWORD)
            modules, needed = (W.HMODULE*1024)(), W.DWORD()
            if not psapi.EnumProcessModulesEx(handle, modules, C.sizeof(modules), C.byref(needed), 3):
                raise C.WinError(C.get_last_error())
            result = dict(module_limit=int(needed.value > C.sizeof(modules)), module_name_errors=0,
                          loaded_driver=0, loaded_agent=0)
            for module in modules[:min(1024, needed.value//C.sizeof(W.HMODULE))]:
                name = C.create_unicode_buffer(32768)
                length = psapi.GetModuleFileNameExW(handle, module, name, len(name))
                if not length or length >= len(name):
                    result['module_name_errors'] += 1
                    continue
                basename = Path(name.value).name.lower()
                if basename == 'microsoft.bluetooth.profiles.hidovergatt.dll':
                    result['loaded_driver'] = 1
                elif basename == 'frida-agent.dll':
                    result['loaded_agent'] = 1
            return result
        finally:
            self.k.CloseHandle(handle)

    def _assets_before(self):
        self.before, details = agent_candidates()
        return details

    def finish(self, error=None):
        self.incomplete = False
        record_exception(error, self.emit)
        self._query('attach_process_after', self._state)
        self._query('attach_modules_after', self._modules)
        self._query('attach_assets_after', self._assets_after)

    def _assets_after(self):
        candidates, details = agent_candidates()
        changed = [p for p, signature in candidates.items() if self.before.get(p) != signature]
        details.update(changed_candidates=len(changed), asset_scope='candidates_not_proven_loaded_path')
        # Fresh extraction wins; existing candidates remain explicitly ambiguous.
        selected = changed or list(candidates)
        details['omitted_candidates'] = max(0, len(selected)-2)
        for path in selected[:2]:
            ref = hashlib.sha256(str(path).encode('utf-8')).hexdigest()
            program_files = os.environ.get('ProgramFiles')
            location = ('elevated_helper' if program_files and
                        path.parent.parent == Path(program_files) / 'Frida' else 'temporary')
            self._query('attach_asset_file', lambda path=path,ref=ref,location=location:
                        dict(agent_file(path, cancelled=self.cancelled), asset_ref=ref, asset_location=location))
            def read(path=path, ref=ref):
                return dict(self.file_access(path), asset_ref=ref, fresh_candidate=int(path in changed),
                            size=min(0xffffffff, candidates[path][0]), asset_scope='candidate_file_dacl_only')
            self._query('attach_asset_access', read)
            # File permission alone is insufficient if the containing directory
            # blocks traversal. Preserve each ancestor's DACL decision separately;
            # token traversal privileges may bypass it, so never label it proof.
            for depth, parent in enumerate(path.parents, 1):
                if depth > 8:
                    break
                def read_parent(parent=parent, depth=depth, ref=ref):
                    return dict(self.file_access(parent, desired=0x20), asset_ref=ref, parent_depth=depth,
                                asset_scope='candidate_parent_dacl_only')
                self._query('attach_asset_access', read_parent)
        return details

    def file_access(self, path, *, desired=0x1200a9):
        """Read owner/group/DACL and AccessCheck with the unchanged target token."""
        if not self.token:
            return {'token_available': 0, 'read_execute': -1}
        class Mapping(C.Structure):
            _fields_ = [(name, W.DWORD) for name in ('read', 'write', 'execute', 'all')]
        _bind(self.a, 'GetNamedSecurityInfoW', W.DWORD, W.LPWSTR, C.c_int, W.DWORD,
              C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p))
        _bind(self.a, 'AccessCheck', W.BOOL, C.c_void_p, W.HANDLE, W.DWORD, C.POINTER(Mapping),
              C.c_void_p, C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.POINTER(W.BOOL))
        descriptor = C.c_void_p()
        code = self.a.GetNamedSecurityInfoW(str(path), 1, 7, None, None, None, None, C.byref(descriptor))
        if code:
            raise C.WinError(code)
        try:
            mapping = Mapping(0x120089, 0x120116, 0x1200a0, 0x1f01ff)
            privileges, length = C.create_string_buffer(4096), W.DWORD(4096)
            granted, allowed = W.DWORD(), W.BOOL()
            if not self.a.AccessCheck(descriptor, self.token, desired, C.byref(mapping), privileges,
                                      C.byref(length), C.byref(granted), C.byref(allowed)):
                raise C.WinError(C.get_last_error())
            return dict(token_available=1, read_execute=int(allowed.value), granted_access=granted.value, requested_access=desired)
        finally:
            self.k.LocalFree(descriptor)

    def close(self):
        if self.k is not None:
            if self.token:
                self.k.CloseHandle(self.token)
                self.token = W.HANDLE()
            if self.process:
                self.k.CloseHandle(self.process)
                self.process = None
