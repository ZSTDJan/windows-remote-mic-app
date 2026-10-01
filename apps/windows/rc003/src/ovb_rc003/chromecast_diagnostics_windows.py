"""Bounded read-only Chromecast environment evidence in the existing app log.

No BLE connection, input, driver/registry writes or event-log enabling. Queries
run outside the receiver, use one hidden time-limited PowerShell child, and never
drive device state. Raw paths, container IDs, event messages and packet contents
are not persisted. No separate artifact or export format is introduced.
"""
from __future__ import annotations

import base64
import atexit
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

from . import remote_selection, raw_input_windows

_lock = threading.Lock()
_pending = {}
_recent = {}
_running = False
_active_exit = None
_active_environment = None
_child_lock = threading.Lock()
_child = None
_closing = False

# Fixed query and projection: never include arbitrary event text, device instance
# paths, serials, addresses or user configuration. GUIDs are hashed before output.
_QUERY_PREFIX = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
function Ref($value) {
 $id = [guid]$value; $hex = $id.ToString('N')
 $prefix = [Text.Encoding]::UTF8.GetBytes("RemoteMic physical remote v1`0")
 $data = New-Object byte[] ($prefix.Length + 16)
 [Array]::Copy($prefix,$data,$prefix.Length)
 for($i=0;$i -lt 16;$i++) { $data[$prefix.Length+$i]=[Convert]::ToByte($hex.Substring($i*2,2),16) }
 $sha=[Security.Cryptography.SHA256]::Create()
 try { return ([BitConverter]::ToString($sha.ComputeHash($data))).Replace('-','').ToLower().Substring(0,12) }
 finally { $sha.Dispose() }
}
$r=@{registry_state='unknown'; registry_kind='unknown'; registry_value=-1; nodes=@(); adapters=@(); drivers=@(); events=@(); errors=@()}
$deviceDetails=@()
function Snapshot($stage) {
 $r.query_stage=$stage
 [Console]::WriteLine(($r | ConvertTo-Json -Depth 7 -Compress))
 [Console]::Out.Flush()
}
function DeviceText($instance,$key) {
 try {
  $p=Get-PnpDeviceProperty -InstanceId $instance -KeyName $key -ErrorAction Stop
  if($p.Type -eq 'Empty' -or $null -eq $p.Data -or [string]$p.Data -eq '') { return @{state='unavailable'} }
  $v=[string]$p.Data
  if($v.Length -gt 96 -or $v -notmatch '^[\p{L}\p{N} ._()+-]+$') { return @{state='omitted'} }
  return @{state='read';value=$v}
 } catch { return @{state='unavailable'} }
}
'''
_REGISTRY_QUERY = r'''
try {
 $key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Services\BthPort\Parameters')
 try {
  if($null -eq $key -or $null -eq $key.GetValue('EtwLogSensitiveData')) { $r.registry_state='missing' }
  else {
   $r.registry_kind=[string]$key.GetValueKind('EtwLogSensitiveData')
   if($r.registry_kind -eq 'DWord') { $r.registry_value=[long]$key.GetValue('EtwLogSensitiveData') }
   $r.registry_state='read'
  }
 } finally { if($null -ne $key) { $key.Dispose() } }
} catch { $r.errors+='registry_unavailable' }
Snapshot 'registry'
'''
_PNP_QUERY = r'''
try {
 $allNodes=@(Get-PnpDevice)
 $r.adapters=@($allNodes | Where-Object { $_.Class -eq 'Bluetooth' -and $_.InstanceId -match '^(USB|PCI|ACPI)\\' } | Select-Object -First 16 | ForEach-Object {
  $props=@(Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_IsPresent,DEVPKEY_Device_ProblemCode)
  @{status=[string]$_.Status; present=($props | Where-Object KeyName -eq DEVPKEY_Device_IsPresent).Data;
    problem_code=($props | Where-Object KeyName -eq DEVPKEY_Device_ProblemCode).Data}
 })
 Snapshot 'adapters'
 $nodes=@($allNodes | Where-Object { $_.FriendlyName -match 'Chromecast' -or $_.InstanceId -match '18d1.*9450' } | Select-Object -First 32)
 foreach($node in $nodes) {
  try {
   $props=@(Get-PnpDeviceProperty -InstanceId $node.InstanceId -KeyName DEVPKEY_Device_ContainerId,DEVPKEY_Device_IsPresent,DEVPKEY_Device_ProblemCode,DEVPKEY_Device_HardwareIds)
   $container=($props | Where-Object KeyName -eq DEVPKEY_Device_ContainerId).Data
   $ids=@(($props | Where-Object KeyName -eq DEVPKEY_Device_HardwareIds).Data)
   $tuples=@([regex]::Matches(($ids -join ' '),'(?i)(?:dev_vid&[0-9a-f]{2}|vid_)[0-9a-f]{4}(?:_pid&|&pid_)[0-9a-f]{4}(?:_rev&|&rev_)[0-9a-f]{4}') | ForEach-Object Value | Select-Object -Unique)
   $row=@{device_ref=(Ref $container); class=[string]$node.Class; status=[string]$node.Status;
    present=($props | Where-Object KeyName -eq DEVPKEY_Device_IsPresent).Data;
    problem_code=($props | Where-Object KeyName -eq DEVPKEY_Device_ProblemCode).Data;
    chromecast_name=[bool]($node.FriendlyName -match 'Chromecast'); pnp_tuples=$tuples;
    hid_child=[bool]($node.InstanceId.StartsWith('HID\'))}
   $r.nodes+= $row
   if($node.Class -eq 'Bluetooth') {
    $deviceDetails+=@{row=$row;instance=$node.InstanceId}
   }
  } catch { $r.errors+='node_query_failed' }
 }
} catch { $r.errors+='pnp_unavailable' }
Snapshot 'nodes'
# Save identities before optional properties. These are Windows-published
# properties, not a claim to have queried live GATT hardware/firmware strings.
foreach($detail in $deviceDetails) {
 $detail.row.model=DeviceText $detail.instance 'DEVPKEY_Device_Model'
 $detail.row.firmware_version=DeviceText $detail.instance 'DEVPKEY_Device_FirmwareVersion'
 $detail.row.firmware_revision=DeviceText $detail.instance 'DEVPKEY_Device_FirmwareRevision'
}
Snapshot 'device_properties'
'''
_DRIVERS_QUERY = r'''
try {
 $r.drivers=@(Get-CimInstance Win32_PnPSignedDriver -Filter "DeviceClass = 'BLUETOOTH'" | Where-Object { $_.DeviceID -match '^(USB|PCI|ACPI)\\' } | Select-Object -First 32 | ForEach-Object {
  @{name=[string]$_.DeviceName; provider=[string]$_.DriverProviderName; version=[string]$_.DriverVersion; date=[string]$_.DriverDate}
 })
} catch { $r.errors+='drivers_unavailable' }
Snapshot 'drivers'
'''
_EVENTS_QUERY = r'''
try {
 # Bounded System sample; empty means no matching entry in this sample, not no fault.
 $r.events=@(Get-WinEvent -FilterHashtable @{LogName='System';StartTime=(Get-Date).AddMinutes(-30);Level=2,3} -MaxEvents 128 -ErrorAction Stop |
  Where-Object { $_.ProviderName -match '^(BTH|Microsoft-Windows-Bluetooth)' } | Select-Object -First 24 | ForEach-Object {
   @{provider=[string]$_.ProviderName; id=[int]$_.Id; level=[int]$_.Level; time=$_.TimeCreated.ToString('o')}
  })
} catch {
 if($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { $r.errors+='no_matching_system_events' }
 else { $r.errors+='events_unavailable' }
}
Snapshot 'complete'
'''

# Each independent source gets its own child/deadline. The combined query is
# retained solely for projection tests, not used by runtime collection.
_QUERY = _QUERY_PREFIX + _REGISTRY_QUERY + _PNP_QUERY + _DRIVERS_QUERY + _EVENTS_QUERY
_QUERY_PARTS = (('registry', _REGISTRY_QUERY, 3), ('events', _EVENTS_QUERY, 5),
                ('drivers', _DRIVERS_QUERY, 5), ('pnp', _PNP_QUERY, 8))


def _raw_snapshot(entity):
    from .chromecast_device_windows import _PNP
    rows = []
    paths = raw_input_windows.enumerate_matching_device_paths(matches=lambda _: True)
    for path in paths[:4096]:
        try:
            key = remote_selection.raw_path_key(path)
            error = False
        except (OSError, remote_selection.SelectionError):
            key, error = "", True
        match = _PNP.search(path)
        if key != entity and "18d1" not in path.lower():
            continue
        rows.append({"device_ref": key[:12], "selected": key == entity,
                     "identity_error": error,
                     "pnp_id": match.group(0).lower() if match else "unrecognized",
                     "pnp_tuple": ":".join(match.groups()).lower() if match else "unrecognized"})
        if len(rows) >= 32:
            break
    return {"total": len(paths), "interfaces": rows}


def _hid_driver_snapshot():
    """Fingerprint the fixed on-disk HID driver before any slow PnP query."""
    from .chromecast_hid_tap_windows import MODULE, _KNOWN_LAYOUTS
    from .frida_hid_tap_runtime import sha256_file
    row = {"scope": "on_disk_not_loaded_module"}
    try:
        row["sha256"] = sha256_file(MODULE)
        row["known_layout"] = row["sha256"] in _KNOWN_LAYOUTS
        import pefile
        image = pefile.PE(str(MODULE), fast_load=True)
        try:
            row["machine"] = int(image.FILE_HEADER.Machine)
            image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
            info = getattr(image, 'VS_FIXEDFILEINFO', [])
            row["version"] = ([info[0].FileVersionMS >> 16, info[0].FileVersionMS & 65535,
                               info[0].FileVersionLS >> 16, info[0].FileVersionLS & 65535] if info else [])
        finally:
            image.close()
    except Exception as error:
        row["state"] = "unavailable"
        row["error_type"] = type(error).__name__
        code = getattr(error, "winerror", None)
        row["winerror"] = code if type(code) is int else -1
    else:
        row["state"] = "read"
    return row


# Only projected Application Error metadata, never message text, paths or dumps.
# Numeric substitutions come from OS-authenticated process identity, not input text.
_EXIT_QUERY = r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$r=@{status='no_matching_event'; events=@()}
$start=[DateTimeOffset]::FromUnixTimeSeconds(__STAMP__).UtcDateTime.AddSeconds(-60)
$end=$start.AddSeconds(90)
try {
 $rows=@(Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000; StartTime=$start; EndTime=$end} -MaxEvents 64)
 foreach($e in $rows) {
  $xml=[xml]$e.ToXml(); $d=@{}
  foreach($item in $xml.Event.EventData.Data) { $d[[string]$item.Name]=[string]$item.'#text' }
  if(-not $d.ContainsKey('ProcessId')) { continue }
  $value=[string]$d.ProcessId
  $eventPid=if($value -match '^0x') { [Convert]::ToInt64($value.Substring(2),16) } else { [long]$value }
  if($eventPid -ne __PID__) { continue }
  $match='pid_and_time'
  if($d.ContainsKey('ProcessCreationTime')) {
   $value=[string]$d.ProcessCreationTime
   $born=if($value -match '^0x') { [Convert]::ToInt64($value.Substring(2),16) } else { [long]$value }
   if($born -ne __BORN__) { continue }
   $match='pid_creation_and_time'
  }
  $module=[IO.Path]::GetFileName([string]$d.ModuleName)
  $r.events+=@{module=$module; module_version=[string]$d.ModuleVersion;
    exception_code=[string]$d.ExceptionCode; fault_offset=[string]$d.FaultingOffset; correlation=$match}
  $r.status='matched'
  if($r.events.Count -ge 4) { break }
 }
} catch {
 if($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { $r.status='query_failed' }
}
$r | ConvertTo-Json -Depth 4 -Compress
'''


def _environment(query=_QUERY, *, timeout=25, snapshots=None):
    global _child
    snapshots = query == _QUERY if snapshots is None else snapshots
    executable = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    encoded = base64.b64encode(query.encode("utf-16-le")).decode("ascii")
    with _child_lock:
        if _closing:
            return {"error": "query_cancelled"}
        process = subprocess.Popen([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        _child = process
    expired = None
    output = b''
    reaped = False
    try:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            expired = error
            process.kill()
            output, _ = process.communicate(timeout=3)
        reaped = True
    finally:
        if not reaped and process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        with _child_lock:
            if _child is process:
                _child = None
    # Environment snapshots are complete JSON lines. Retain the last completed
    # stage even when a later system query hangs. Exit-event queries stay atomic.
    if expired is not None and not snapshots:
        raise expired
    if process.returncode != 0 and expired is None:
        return {"error": "query_failed"}
    if len(output) > (262144 if snapshots else 65536):
        return {"error": "query_size_limit"}
    if snapshots:
        snapshot = None
        for line in output.decode("utf-8-sig", errors="replace").splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    snapshot = value
            except ValueError:
                continue
        if snapshot is not None:
            if expired is not None:
                snapshot.update(error="query_timeout", partial=True)
            return snapshot
        if expired is not None:
            raise expired
        return {"error": "query_failed"}
    return json.loads(output.decode("utf-8-sig"))


def _shutdown():
    global _closing
    with _lock:
        waiting = [key for key in _pending if len(key) == 5 and key[0] == 'exit']
        active = _active_exit
        environments = [key for key in _pending if len(key) == 2]
        if _active_environment is not None:
            environments.insert(0, _active_environment)
    for key in ([active] if active is not None else []) + waiting:
        logging.getLogger('ovb_rc003').warning(
            'Chromecast crash evidence unavailable: run=%s reason=application_closing_before_query_completed',
            key[1][:12])
    for entity, cause in environments:
        logging.getLogger('ovb_rc003').warning(
            'Chromecast environment unavailable: device_ref=%s cause=%s reason=application_closing_before_query_completed',
            entity[:12], cause)
    with _child_lock:
        _closing = True
        if _child is not None and _child.poll() is None:
            try:
                _child.kill()  # Only the diagnostic child owned by this module.
                _child.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


atexit.register(_shutdown)


def _collect(entity, cause, requested_at):
    from .chromecast_etw_windows import capture_settings_snapshot
    logger = logging.getLogger("ovb_rc003")
    common = {"device_ref": entity[:12], "cause": cause, "requested_at": requested_at,
              "collected_at": time.time(), "os_version": list(sys.getwindowsversion()[:3]),
              "scope": "read_only_snapshot_not_proof_of_driver_setting_effect"}
    common["capture_settings"] = capture_settings_snapshot()
    common["hid_driver"] = _hid_driver_snapshot()
    try:
        common["raw_input"] = _raw_snapshot(entity)
    except Exception:
        common["raw_input"] = {"error": "query_failed"}
    # Persist the quick evidence even if the slower system query times out.
    logger.info("Chromecast environment: %s", json.dumps(common, ensure_ascii=True, separators=(",", ":")))
    for name, query, timeout in _QUERY_PARTS:
        started = time.monotonic()
        logger.info('Chromecast system query: device_ref=%s cause=%s part=%s state=begin deadline_ms=%s',
                    entity[:12], cause, name, timeout * 1000)
        try:
            environment = _environment(_QUERY_PREFIX + query, timeout=timeout, snapshots=True)
        except subprocess.TimeoutExpired:
            environment = {"error": "query_timeout"}
        except Exception:
            environment = {"error": "query_failed"}
        logger.info("Chromecast system evidence: %s", json.dumps(
            {"device_ref": entity[:12], "cause": cause, "requested_at": requested_at,
             "part": name, "duration_ms": round((time.monotonic()-started)*1000),
             "completed_at": time.time(), "environment": environment}, ensure_ascii=True, separators=(",", ":")))
        if _closing:
            break


def _drain():
    global _running, _active_exit, _active_environment
    while True:
        with _lock:
            if not _pending:
                _running = False
                return
            key = next(iter(_pending))
            stamp = _pending.pop(key)
            if len(key) == 5 and key[0] == 'exit':
                _active_exit = key
            else:
                _active_environment = key
        try:
            if len(key) == 5 and key[0] == "exit":
                _collect_exit(*key[1:], stamp)
            else:
                _collect(*key, stamp)
        except Exception:
            logging.getLogger("ovb_rc003").info("Chromecast environment: query_failed")
        finally:
            if len(key) == 5 and key[0] == 'exit':
                with _lock:
                    _active_exit = None
            else:
                with _lock:
                    _active_environment = None


def request(entity, cause):
    """Best effort, at most one query and two pending snapshots; throttle repeats."""
    global _running
    try:
        from .chromecast_channel import DIAGNOSTIC_REASONS
        if _closing or not remote_selection.valid_key(entity) or cause not in DIAGNOSTIC_REASONS | {"ready"}:
            return
        with _lock:
            key, now = (entity, cause), time.monotonic()
            throttled = now - _recent.get(key, -1e9) < 60
            if throttled or len(_pending) >= 2:
                logging.getLogger('ovb_rc003').info(
                    'Chromecast environment skipped: device_ref=%s cause=%s reason=%s',
                    entity[:12], cause, 'throttled' if throttled else 'queue_full')
                return
            if len(_recent) >= 64:
                _recent.clear()
            _recent[key] = now
            _pending[key] = time.time()
            logging.getLogger("ovb_rc003").info("Chromecast environment queued: device_ref=%s cause=%s", entity[:12], cause)
            if not _running:
                _running = True
                try:
                    threading.Thread(target=_drain, name="chromecast-evidence", daemon=True).start()
                except Exception:
                    _running = False
                    _pending.clear()
                    logging.getLogger('ovb_rc003').warning(
                        'Chromecast environment unavailable: device_ref=%s cause=%s reason=thread_start_failed', entity[:12], cause)
    except Exception:
        pass  # Evidence must never fail a receiver or delay shutdown.


def _collect_exit(run, pid, born, code, stamp):
    query = _EXIT_QUERY.replace('__PID__', str(pid)).replace('__BORN__', str(born)).replace('__STAMP__', str(int(stamp)))
    try:
        raw = _environment(query, timeout=8)
        status = raw.get('status', raw.get('error', 'query_failed'))
        if status not in {'matched', 'no_matching_event', 'query_failed', 'query_cancelled', 'query_size_limit'}:
            status = 'query_failed'
        events = []
        for row in raw.get('events', [])[:4]:
            if not isinstance(row, dict):
                continue
            projected = {}
            for field, pattern in (("module", r"[A-Za-z0-9_.-]{1,128}"),
                                   ("module_version", r"[0-9.]{1,48}"),
                                   ("exception_code", r"(?:0x)?[0-9a-fA-F]{1,8}"),
                                   ("fault_offset", r"(?:0x)?[0-9a-fA-F]{1,16}")):
                value = row.get(field)
                projected[field] = value if isinstance(value, str) and re.fullmatch(pattern, value) else 'unavailable'
            correlation = row.get('correlation')
            projected['correlation'] = correlation if correlation in {'pid_and_time', 'pid_creation_and_time'} else 'unavailable'
            events.append(projected)
        if status == 'matched' and not events:
            status = 'query_failed'
    except subprocess.TimeoutExpired:
        status, events = 'query_timeout', []
    except Exception:
        status, events = 'query_failed', []
    logging.getLogger('ovb_rc003').warning("Chromecast crash evidence: %s", json.dumps(
        dict(run=run[:12], pid=pid, exit_hex=f'0x{code:08X}', status=status, events=events,
             scope='immediate_system_event_sample_not_native_stack_or_root_cause'), separators=(',', ':')))


def request_process_exit(run, pid, born, code):
    """Use the existing single diagnostic worker; never delay receiver cleanup."""
    global _running
    try:
        if (not remote_selection.valid_key(run) or type(pid) is not int or not 0 < pid <= 0xffffffff
                or type(born) is not int or not 0 < born < 2**63
                or type(code) is not int or not 0 <= code <= 0xffffffff):
            return
        key = ('exit', run, pid, born, code)
        logger = logging.getLogger('ovb_rc003')
        with _lock:
            if _closing or len(_pending) >= 2:
                logger.warning('Chromecast crash evidence unavailable: run=%s reason=%s', run[:12],
                               'application_closing' if _closing else 'queue_full')
                return
            if key in _recent:
                return  # Launcher and authenticated worker can name the same OS process.
            if len(_recent) >= 64:
                _recent.clear()
            _recent[key] = time.monotonic()
            _pending[key] = time.time()
            logger.info('Chromecast crash evidence queued: run=%s pid=%s', run[:12], pid)
            if not _running:
                _running = True
                try:
                    threading.Thread(target=_drain, name='chromecast-evidence', daemon=True).start()
                except Exception:
                    _running = False
                    _pending.pop(key, None)
                    logger.warning('Chromecast crash evidence unavailable: run=%s reason=thread_start_failed', run[:12])
    except Exception:
        pass
