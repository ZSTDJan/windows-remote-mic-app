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
import subprocess
import sys
import threading
import time

from . import remote_selection, raw_input_windows

_lock = threading.Lock()
_pending = {}
_recent = {}
_running = False
_child_lock = threading.Lock()
_child = None
_closing = False

# Fixed query and projection: never include arbitrary event text, device instance
# paths, serials, addresses or user configuration. GUIDs are hashed before output.
_QUERY = r'''
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
try {
 $allNodes=@(Get-PnpDevice)
 $r.adapters=@($allNodes | Where-Object { $_.Class -eq 'Bluetooth' -and $_.InstanceId -match '^(USB|PCI|ACPI)\\' } | Select-Object -First 16 | ForEach-Object {
  $props=@(Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_IsPresent,DEVPKEY_Device_ProblemCode)
  @{status=[string]$_.Status; present=($props | Where-Object KeyName -eq DEVPKEY_Device_IsPresent).Data;
    problem_code=($props | Where-Object KeyName -eq DEVPKEY_Device_ProblemCode).Data}
 })
 $nodes=@($allNodes | Where-Object { $_.FriendlyName -match 'Chromecast' -or $_.InstanceId -match '18d1.*9450' } | Select-Object -First 32)
 foreach($node in $nodes) {
  try {
   $props=@(Get-PnpDeviceProperty -InstanceId $node.InstanceId -KeyName DEVPKEY_Device_ContainerId,DEVPKEY_Device_IsPresent,DEVPKEY_Device_ProblemCode,DEVPKEY_Device_HardwareIds)
   $container=($props | Where-Object KeyName -eq DEVPKEY_Device_ContainerId).Data
   $ids=@(($props | Where-Object KeyName -eq DEVPKEY_Device_HardwareIds).Data)
   $tuples=@([regex]::Matches(($ids -join ' '),'(?i)(?:dev_vid&[0-9a-f]{2}|vid_)[0-9a-f]{4}(?:_pid&|&pid_)[0-9a-f]{4}(?:_rev&|&rev_)[0-9a-f]{4}') | ForEach-Object Value | Select-Object -Unique)
   $r.nodes+=@{device_ref=(Ref $container); class=[string]$node.Class; status=[string]$node.Status;
    present=($props | Where-Object KeyName -eq DEVPKEY_Device_IsPresent).Data;
    problem_code=($props | Where-Object KeyName -eq DEVPKEY_Device_ProblemCode).Data;
    chromecast_name=[bool]($node.FriendlyName -match 'Chromecast'); pnp_tuples=$tuples;
    hid_child=[bool]($node.InstanceId.StartsWith('HID\'))}
  } catch { $r.errors+='node_query_failed' }
 }
} catch { $r.errors+='pnp_unavailable' }
try {
 $r.drivers=@(Get-CimInstance Win32_PnPSignedDriver -Filter "DeviceClass = 'BLUETOOTH'" | Where-Object { $_.DeviceID -match '^(USB|PCI|ACPI)\\' } | Select-Object -First 32 | ForEach-Object {
  @{name=[string]$_.DeviceName; provider=[string]$_.DriverProviderName; version=[string]$_.DriverVersion; date=[string]$_.DriverDate}
 })
} catch { $r.errors+='drivers_unavailable' }
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
$r | ConvertTo-Json -Depth 7 -Compress
'''


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
                     "pnp_tuple": ":".join(match.groups()).lower() if match else "unrecognized"})
        if len(rows) >= 32:
            break
    return {"total": len(paths), "interfaces": rows}


def _environment():
    global _child
    executable = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    encoded = base64.b64encode(_QUERY.encode("utf-16-le")).decode("ascii")
    with _child_lock:
        if _closing:
            return {"error": "query_cancelled"}
        process = subprocess.Popen([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        _child = process
    try:
        output, _ = process.communicate(timeout=25)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        with _child_lock:
            if _child is process:
                _child = None
    if process.returncode != 0:
        return {"error": "query_failed"}
    if len(output) > 65536:
        return {"error": "query_size_limit"}
    return json.loads(output.decode("utf-8-sig"))


def _shutdown():
    global _closing
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
    try:
        common["raw_input"] = _raw_snapshot(entity)
    except Exception:
        common["raw_input"] = {"error": "query_failed"}
    # Persist the quick evidence even if the slower system query times out.
    logger.info("Chromecast environment: %s", json.dumps(common, ensure_ascii=True, separators=(",", ":")))
    try:
        environment = _environment()
    except subprocess.TimeoutExpired:
        environment = {"error": "query_timeout"}
    except Exception:
        environment = {"error": "query_failed"}
    logger.info("Chromecast system evidence: %s", json.dumps(
        {"device_ref": entity[:12], "cause": cause, "requested_at": requested_at,
         "completed_at": time.time(), "environment": environment}, ensure_ascii=True, separators=(",", ":")))


def _drain():
    global _running
    while True:
        with _lock:
            if not _pending:
                _running = False
                return
            key = next(iter(_pending))
            stamp = _pending.pop(key)
        try:
            _collect(*key, stamp)
        except Exception:
            logging.getLogger("ovb_rc003").info("Chromecast environment: query_failed")


def request(entity, cause):
    """Best effort, at most one query and two pending snapshots; throttle repeats."""
    global _running
    try:
        from .chromecast_channel import DIAGNOSTIC_REASONS
        if _closing or not remote_selection.valid_key(entity) or cause not in DIAGNOSTIC_REASONS | {"ready"}:
            return
        with _lock:
            key, now = (entity, cause), time.monotonic()
            if now - _recent.get(key, -1e9) < 60 or len(_pending) >= 2:
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
    except Exception:
        pass  # Evidence must never fail a receiver or delay shutdown.
