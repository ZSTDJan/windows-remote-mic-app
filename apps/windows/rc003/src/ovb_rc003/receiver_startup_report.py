"""On-demand report; never attaches to a receiver, changes config or installs.

The packaged EXE owns this CLI. Its isolated child probes only its own temporary
files and reads recent Windows event metadata. A kill-on-close Job owns that
child and wevtutil; the app/Windows HID host are never assigned or terminated.
Partial captures retain completed sections. Window shutdown cancels its export.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from . import __version__
from .receiver_startup_errors import failure_details, root_reference

FLAG = '--receiver-startup-report'
CHILD_FLAG = '--receiver-report-worker'
TIMEOUT = 40
CHANNELS = ('Microsoft-Windows-CodeIntegrity/Operational',
            'Microsoft-Windows-Windows Defender/Operational',
            'Microsoft-Windows-AppLocker/EXE and DLL', 'Application')
_COMPONENTS = ('frida', 'wudfhost', 'remotemic', '无线麦', 'bthleenum')
_PROVIDERS = frozenset({
    'Microsoft-Windows-CodeIntegrity', 'Microsoft-Windows-Windows Defender',
    'Microsoft-Windows-AppLocker', 'Application Error', 'Application Hang',
    'Windows Error Reporting', 'Microsoft-Windows-DriverFrameworks-UserMode',
})
_EVENT_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z', re.ASCII)
_MAX_EVIDENCE_BYTES = 512 * 1024


def _error(exc):
    return dict(error_type=type(exc).__name__, native_code=getattr(exc, 'winerror', None)
                or getattr(exc, 'errno', None), **failure_details(exc))


def temporary_roots():
    """Report sources separately; Python fallback is not Frida's effective root."""
    roots = {key: os.environ.get(key, '') for key in ('TEMP', 'TMP', 'TMPDIR')}
    buffer = C.create_unicode_buffer(32768)
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.GetTempPathW.argtypes = (W.DWORD, W.LPWSTR)
    kernel.GetTempPathW.restype = W.DWORD
    size = kernel.GetTempPathW(len(buffer), buffer)
    roots['GetTempPathW'] = buffer.value if 0 < size < len(buffer) else ''
    # Do not call tempfile.gettempdir(): it may fall back and hide a bad TMP.
    return roots


def probe_root(label, value):
    row = dict(source=label, configured=bool(value), outcome='not_configured')
    if not value:
        return row
    row['path_ref'] = root_reference(value)
    path = Path(value)
    if not path.is_absolute() or str(path).startswith('\\\\'):
        row['outcome'] = 'unsupported_path'
        return row
    owned = None
    try:
        info = path.lstat()
        row['directory'] = stat.S_ISDIR(info.st_mode)
        row['reparse'] = bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        if not row['directory'] or row['reparse']:
            row['outcome'] = 'not_plain_directory'
            return row
        candidate = path / ('remote-mic-diagnostic-' + uuid.uuid4().hex)
        candidate.mkdir()
        owned = candidate
        row['create_directory'] = True
        payload = b'RemoteMic receiver diagnostic\n'
        item = owned / 'write-read-check.bin'
        with item.open('xb') as stream:
            stream.write(payload)
        row['write_file'] = True
        row['read_file'] = item.read_bytes() == payload
        item.unlink()
        row['delete_file'] = True
        owned.rmdir()
        owned = None
        row.update(outcome='passed' if row['read_file'] else 'failed', cleanup='complete')
    except OSError as exc:
        row.update(outcome='failed', **_error(exc))
    finally:
        if owned is not None:
            try:
                # Only the fresh, owned directory; never recurse into a root.
                (owned / 'write-read-check.bin').unlink(missing_ok=True)
                owned.rmdir()
                row['cleanup'] = 'complete'
            except OSError as exc:
                row['cleanup'] = 'failed'
                row['cleanup_error'] = _error(exc)
    return row


def event_query(channel):
    tool = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/wevtutil.exe')
    query = '*[System[TimeCreated[timediff(@SystemTime) <= 900000]]]'
    try:
        result = subprocess.run([tool, 'qe', channel, '/q:' + query, '/c:32', '/rd:true', '/f:xml', '/uni:true'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        row = dict(channel=channel, return_code=result.returncode)
        if result.returncode:
            return dict(row, outcome='unavailable')
        content = result.stdout.decode('utf-16', errors='replace').lstrip('\ufeff')
        root = ET.fromstring('<Events>' + content + '</Events>')
        ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
        events = []
        for event in list(root)[:32]:
            system = event.find('e:System', ns)
            if system is None:
                continue
            fields = event.findall('e:EventData/e:Data', ns)
            # EventData may contain unrelated command lines or document names.
            # Retain only receiver-related entries and fixed status numbers;
            # filenames are evidence references, not exported local paths.
            combined = ' '.join(item.text or '' for item in fields).lower()
            if not any(word in combined for word in _COMPONENTS):
                continue
            provider = system.find('e:Provider', ns)
            provider_name = provider.get('Name', '') if provider is not None else ''
            stamp = system.find('e:TimeCreated', ns)
            stamp = stamp.get('SystemTime', '') if stamp is not None else ''
            event_id = system.findtext('e:EventID', '', ns)
            data = []
            for item in fields:
                name, value = item.get('Name', ''), item.text or ''
                if name in ('Status', 'StatusCode', 'ErrorCode', 'RequestedSigningLevel', 'ValidatedSigningLevel'):
                    if re.fullmatch(r'(?:0x[0-9a-fA-F]{1,8}|[0-9]{1,10})', value):
                        data.append(dict(name=name, value=value))
                elif name in ('FileName', 'ProcessName', 'AppName', 'ModuleName', 'Path'):
                    component = next((word for word in _COMPONENTS if word in value.lower()), None)
                    if component is not None:
                        data.append(dict(name=name, component=component, path_ref=root_reference(value)))
                if len(data) == 24:
                    break
            events.append(dict(event_id=event_id if re.fullmatch(r'[0-9]{1,5}', event_id) else '',
                               provider=provider_name if provider_name in _PROVIDERS else 'other',
                               time=stamp if _EVENT_TIME.fullmatch(stamp) else '',
                               data=data))
        return dict(row, outcome='captured', limit=32, window_seconds=900, events=events)
    except subprocess.TimeoutExpired:
        return dict(channel=channel, outcome='timeout')
    except Exception as exc:
        return dict(channel=channel, outcome='failed', **_error(exc))


def worker(folder):
    from .hid_elevation_windows import query_process_elevated

    directory = Path(folder)
    request = json.loads((directory / 'request.json').read_text(encoding='utf-8'))
    if request != {'kind': 'receiver_startup_report', 'schema': 1}:
        return 2
    with (directory / 'evidence.jsonl').open('x', encoding='utf-8') as stream:
        def emit(kind, data):
            stream.write(json.dumps(dict(kind=kind, data=data), ensure_ascii=True) + '\n')
            stream.flush()

        environment = dict(version=__version__, windows=list(sys.getwindowsversion()),
                           temp_probe_scope='new_owned_files_only', receiver_attach_performed=False)
        for name, fetch in (
            ('elevated', query_process_elevated),
            ('frida_version', lambda: __import__('frida').__version__),
            ('executable_sha256', lambda: hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()),
        ):
            try:
                environment[name] = fetch()
            except Exception as exc:
                emit('collection_error', dict(section=name, **_error(exc)))
        emit('environment', environment)
        try:
            checked_roots = {}
            for label, value in temporary_roots().items():
                if value not in checked_roots:
                    checked_roots[value] = probe_root(label, value)
                emit('temporary_root', dict(checked_roots[value], source=label))
        except Exception as exc:
            emit('collection_error', dict(section='temporary_roots', **_error(exc)))
        for channel in CHANNELS:
            emit('windows_events', event_query(channel))
        emit('completion', dict(outcome='complete'))
    return 0


class _Job:
    """Own only the report child. Closing also reaps an outstanding wevtutil."""
    def __init__(self):
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = (C.c_void_p, W.LPCWSTR)
        self.kernel.CreateJobObjectW.restype = W.HANDLE
        self.kernel.SetInformationJobObject.argtypes = (W.HANDLE, C.c_int, C.c_void_p, W.DWORD)
        self.kernel.SetInformationJobObject.restype = W.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = (W.HANDLE, W.HANDLE)
        self.kernel.AssignProcessToJobObject.restype = W.BOOL
        self.kernel.CloseHandle.argtypes = (W.HANDLE,)
        self.handle = self.kernel.CreateJobObjectW(None, None)
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION: basic limits begins with two
        # LARGE_INTEGERs, then DWORD LimitFlags. x64 structure size is 144.
        data = C.create_string_buffer(144 if C.sizeof(C.c_void_p) == 8 else 112)
        C.c_uint32.from_buffer(data, 16).value = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, data, len(data)):
            self.close()
            raise C.WinError(C.get_last_error())

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise C.WinError(C.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _child_command(folder):
    if getattr(sys, 'frozen', False):
        return [sys.executable, CHILD_FLAG, str(folder)]
    return [sys.executable, '-B', '-m', 'ovb_rc003', CHILD_FLAG, str(folder)]


def collect(destination, *, root=None, timeout=TIMEOUT, cancel=None, overwrite=False,
            discard_on_cancel=False):
    from . import log_export

    destination = Path(destination)
    if (not destination.is_absolute() or destination.suffix.lower() != '.zip'
            or (not overwrite and destination.exists())):
        return log_export.ExportResult('invalid_destination')
    if discard_on_cancel and cancel is not None and cancel.is_set():
        return log_export.ExportResult('cancelled')
    directory = destination.parent / ('.remote-mic-report-' + uuid.uuid4().hex)
    try:
        directory.mkdir()
    except OSError:
        return log_export.ExportResult('write_failed')
    try:
        outcome, child_code, error = _collect_evidence(directory, timeout, cancel)
        if discard_on_cancel and cancel is not None and cancel.is_set():
            return log_export.ExportResult('cancelled')
        records = []
        evidence = directory / 'evidence.jsonl'
        try:
            with evidence.open('rb') as stream:
                content = stream.read(_MAX_EVIDENCE_BYTES + 1)
        except FileNotFoundError:
            pass
        except OSError as exc:
            records.append(dict(kind='collection_error', data=dict(section='read_report', **_error(exc))))
        else:
            for line in content[:_MAX_EVIDENCE_BYTES].splitlines():
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not isinstance(row.get('data'), dict):
                        raise ValueError('invalid_evidence_row')
                    records.append(row)
                except (ValueError, UnicodeError):
                    records.append(dict(kind='capture_loss', data=dict(outcome='partial_row')))
            if len(content) > _MAX_EVIDENCE_BYTES:
                records.append(dict(kind='capture_loss', data=dict(outcome='truncated')))
        if outcome == 'complete':
            if not any(row.get('kind') == 'completion' for row in records) or any(
                row.get('kind') in {'capture_loss', 'collection_error'}
                or (row.get('kind') == 'windows_events' and row['data'].get('outcome') != 'captured')
                for row in records
            ):
                outcome = 'capture_incomplete'
        report = dict(schema_version=1, version=__version__, collected_at=datetime.now().astimezone().isoformat(),
                      outcome=outcome, sections=records, child_exit_code=child_code,
                      includes_configuration=False, includes_recordings=False,
                      receiver_attach_performed=False,
                      note='Event absence is not proof of no blocking. No drivers, policies, ACLs or pairing changed.')
        if outcome == 'collection_failed':
            report['collection_error'] = error
        return log_export.export_logs(destination, root=root, cancel=cancel if discard_on_cancel else None,
                                      receiver_report=report, overwrite=overwrite)
    finally:
        for name in ('request.json', 'evidence.jsonl'):
            try:
                (directory / name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass


def _collect_evidence(directory, timeout, cancel):
    job = process = None
    error = None
    outcome = 'timeout'
    try:
        (directory / 'request.json').write_text(json.dumps({'kind': 'receiver_startup_report', 'schema': 1}), encoding='utf-8')
        job = _Job()
        process = subprocess.Popen(_child_command(directory), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(process)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                outcome = 'cancelled'
                break
            code = process.poll()
            if code is not None:
                outcome = 'complete' if code == 0 else 'child_failed'
                break
            time.sleep(.05)
    except Exception as exc:
        outcome, error = 'collection_failed', _error(exc)
    finally:
        if job is not None:
            job.close()
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()  # Fallback if assignment itself failed; own child only.
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired) as exc:
                outcome, error = 'collection_failed', _error(exc)
    return outcome, process.returncode if process is not None else None, error


def main(args):
    if len(args) > 1 or (args and not Path(args[0]).is_absolute()):
        return 2
    destination = Path(args[0]) if args else Path(sys.executable).parent / (
        datetime.now().strftime('无线麦接收诊断-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6] + '.zip')
    return 0 if collect(destination).outcome == 'exported' else 1
