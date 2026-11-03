@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "REMOTE_MIC_REPORT_SELF=%~f0"
set "REMOTE_MIC_REPORT_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "REMOTE_MIC_REPORT_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
"%REMOTE_MIC_REPORT_PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop';try{$s=[IO.File]::ReadAllText($env:REMOTE_MIC_REPORT_SELF,[Text.Encoding]::UTF8);$m=[regex]::Match($s,'(?m)^# POWERSHELL_PAYLOAD\r?$');$p=$s.Substring($m.Index+$m.Length);& ([scriptblock]::Create($p))}catch{Write-Host '报告生成失败，请把此窗口内容反馈给开发者。';Write-Host $_.Exception.GetType().Name;exit 1}"
set "REMOTE_MIC_REPORT_EXIT=%errorlevel%"
echo.
pause
exit /b %REMOTE_MIC_REPORT_EXIT%
# POWERSHELL_PAYLOAD
# One-shot, read-only diagnostics for the .102/.103 paired-device-list failure.
# Authoritative source: this file. Send this single CMD, no Python/install/admin.
# Private files belong to this run and are removed after the final TXT is saved.
$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
$script:ReportVersion = 2

function Safe-Error($Exception) {
    $rows = @()
    for ($i = 0; $i -lt 3 -and $null -ne $Exception; $i++) {
        $row = [ordered]@{ type = $Exception.GetType().Name; hresult = $Exception.HResult.ToString('X8') }
        if ($Exception -is [ComponentModel.Win32Exception]) { $row.winerror = $Exception.NativeErrorCode }
        $rows += [pscustomobject]$row
        $Exception = $Exception.InnerException
    }
    return $rows
}
function Save-Step($Value) {
    $json = $Value | ConvertTo-Json -Depth 12 -Compress
    $path = Join-Path $env:REMOTE_MIC_REPORT_WORK ($env:REMOTE_MIC_REPORT_MODE + '.json')
    [IO.File]::WriteAllText($path + '.tmp', $json, (New-Object Text.UTF8Encoding($true)))
    Move-Item -LiteralPath ($path + '.tmp') -Destination $path -Force
}
function Known-Remote($Name) {
    switch -Regex (([string]$Name).Trim()) {
        '^Chromecast Remote$' { return 'chromecast_remote' }
        '^Google TV Remote$' { return 'google_tv_remote' }
        '^RC003(?:\b|_)' { return 'xiaomi_rc003' }
        'Chromecast|RC003|Google TV Remote' { return 'remote_name_not_exact' }
        default { return 'other' }
    }
}
function Run-System {
    $r = [ordered]@{ state='running'; phase='operating_system' }
    Save-Step $r
    $os = Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5
    $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $r.os = [ordered]@{ edition=$os.Caption; build=$os.BuildNumber; revision=$cv.UBR;
        display_version=$cv.DisplayVersion; architecture=$os.OSArchitecture;
        powershell=$PSVersionTable.PSVersion.ToString(); process_64bit=[Environment]::Is64BitProcess }
    $r.phase='bluetooth_adapters'; Save-Step $r
    $drivers = @(Get-CimInstance Win32_PnPSignedDriver -Filter "DeviceClass='BLUETOOTH'" -OperationTimeoutSec 5)
    $entities = @(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='Bluetooth'" -OperationTimeoutSec 5)
    $adapters = @()
    foreach ($driver in $drivers) {
        # Device/serial/address IDs are only matched in memory, never exported.
        if ([string]$driver.DeviceID -notmatch '^(USB|PCI)\\') { continue }
        $entity = $entities | Where-Object DeviceID -eq $driver.DeviceID | Select-Object -First 1
        $hardware = @([regex]::Matches([string]$driver.DeviceID, '(?i)(?:VID|PID|VEN|DEV)_[0-9A-F]{4}') |
            ForEach-Object { $_.Value.ToUpperInvariant() })
        $adapters += [pscustomobject][ordered]@{ driver_description=$driver.DeviceName;
            provider=$driver.DriverProviderName; version=$driver.DriverVersion;
            date=([string]$driver.DriverDate); signed=[bool]$driver.IsSigned;
            hardware=$hardware; status=$entity.Status; problem_code=$entity.ConfigManagerErrorCode }
    }
    $r.adapters=$adapters; $r.phase='bluetooth_services'; Save-Step $r
    $r.services = @(Get-Service -Name 'bthserv','BluetoothUserService*','DeviceAssociationService',
        'DeviceAssociationBrokerSvc*','DevicesFlowUserSvc*' -ErrorAction SilentlyContinue |
        ForEach-Object { [pscustomobject]@{ name=($_.Name -replace '_[0-9a-fA-F]+$','');
            status=$_.Status.ToString(); start_type=$_.StartType.ToString() } })
    $r.pnp_remote_matches = @($entities | Where-Object { (Known-Remote $_.Name) -ne 'other' } |
        ForEach-Object { [pscustomobject]@{ remote=(Known-Remote $_.Name); status=$_.Status;
            problem_code=$_.ConfigManagerErrorCode; present=$_.Present } })
    $r.phase='finished'; $r.state='success'; Save-Step $r
}
function Run-WinRT {
    $watch=[Diagnostics.Stopwatch]::StartNew()
    $r = [ordered]@{ state='running'; phase='winrt_import'; runtime='windows_powershell_dotnet' }
    Save-Step $r
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null=[Windows.Devices.Bluetooth.BluetoothLEDevice,Windows.Devices.Bluetooth,ContentType=WindowsRuntime]
    $null=[Windows.Devices.Enumeration.DeviceInformation,Windows.Devices.Enumeration,ContentType=WindowsRuntime]
    $r.phase='selector'; Save-Step $r
    $selector=[Windows.Devices.Bluetooth.BluetoothLEDevice]::GetDeviceSelectorFromPairingState($true)
    $r.phase='enumerate'; Save-Step $r
    $operation=[Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync(
        $selector, [string[]]@('System.Devices.Aep.ContainerId'))
    $method=[System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and
        $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $task=$method.MakeGenericMethod([Windows.Devices.Enumeration.DeviceInformationCollection]).Invoke($null,@($operation))
    if (-not $task.Wait(8000)) {
        $r.state='timeout'; $r.query_timeout_ms=8000; $r.elapsed_ms=$watch.ElapsedMilliseconds; Save-Step $r; return
    }
    $r.phase='device_rows'; Save-Step $r
    $devices=$task.Result
    $matches=@(); $valid=0; $missing=0; $identities=@{}
    foreach ($device in $devices) {
        $kind=Known-Remote $device.Name
        $identity='missing'
        $property=@($device.Properties | Where-Object { $_.Key -eq 'System.Devices.Aep.ContainerId' })
        if ($property.Count -eq 1) {
            $container=[guid]::Empty
            if ([guid]::TryParse([string]$property[0].Value,[ref]$container) -and
                $container -ne [guid]::Empty -and $container -ne [guid]'00000000-0000-0000-0000-000000000001') {
                $identity='valid'; $valid++
                $key=$container.ToString()
                if (-not $identities.ContainsKey($key)) { $identities[$key]=0 }
                $identities[$key]++
            } else { $identity='invalid'; $missing++ }
        } else { $missing++ }
        if ($kind -ne 'other') {
            $matches += [pscustomobject]@{ remote=$kind; listed_by_paired_selector=$true; container_identity=$identity }
        }
    }
    $r.count=$devices.Count; $r.valid_identities=$valid; $r.missing_or_invalid_identities=$missing
    $r.duplicate_identity_groups=@($identities.Values | Where-Object { $_ -gt 1 }).Count
    $r.remotes=$matches; $r.phase='finished'; $r.state='success'; $r.elapsed_ms=$watch.ElapsedMilliseconds; Save-Step $r
}
function Initialize-ReportNative {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
public static class RemoteMicReportChild {
 [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct SI {
  public int cb; public string reserved,desktop,title; public int x,y,xSize,ySize,xChars,yChars,fill,flags;
  public short show,reservedSize; public IntPtr reservedData,stdin,stdout,stderr;
 }
 [StructLayout(LayoutKind.Sequential)] struct PI { public IntPtr process,thread; public uint pid,tid; }
 [StructLayout(LayoutKind.Sequential)] struct JobBasic {
  public long userTime,jobTime; public uint flags; public UIntPtr minWorking,maxWorking;
  public uint activeLimit; public UIntPtr affinity; public uint priority,scheduling;
 }
 [StructLayout(LayoutKind.Sequential)] struct JobLimits {
  public JobBasic basic; public ulong readOps,writeOps,otherOps,readBytes,writeBytes,otherBytes;
  public UIntPtr processMemory,jobMemory,peakProcessMemory,peakJobMemory;
 }
 delegate bool EnumWindow(IntPtr window,IntPtr data);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateDesktopW(string name,IntPtr dev,IntPtr mode,int flags,uint access,IntPtr attr);
 [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr desktop);
 [DllImport("user32.dll")] static extern bool EnumDesktopWindows(IntPtr desktop,EnumWindow callback,IntPtr data);
 [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindow callback,IntPtr data);
 [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr window,EnumWindow callback,IntPtr data);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window,out uint pid);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowTextW(IntPtr window,StringBuilder text,int max);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr SendMessageTimeoutW(IntPtr window,uint message,UIntPtr count,StringBuilder text,uint flags,uint timeout,out UIntPtr result);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcessW(string app,StringBuilder command,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref SI startup,out PI process);
 [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle,uint ms);
 [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr process,out uint code);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateProcess(IntPtr process,uint code);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
 [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr CreateJobObjectW(IntPtr attr,IntPtr name);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int kind,ref JobLimits limits,int size);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
 [DllImport("kernel32.dll",SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
 [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,uint pid);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool GetTokenInformation(IntPtr token,int kind,out int value,int length,out int returned);
 public class Result {
  public string state; public int winerror; public uint pid,exitCode; public long elapsedMs;
  public bool errorDialog,exitConfirmed; public string dialogBody="";
  public List<string> loadedModules=new List<string>(); public int moduleQueryError;
  public List<Progress> progress=new List<Progress>();
 }
 public class Progress { public long elapsedMs; public string diagnostic,stage; }
 public class LiveContext {
  public string state; public uint pid; public int sessionId,queryError,moduleQueryError;
  public bool elevated,elevationKnown; public int elevationError; public long window;
  public List<string> loadedModules=new List<string>();
 }
 static bool RelevantModule(string name) {
  return name=="python312.dll" || name=="ws2_32.dll" || name=="qt6core.dll" || name=="qt6qml.dll" ||
   name.StartsWith("remote_selection.") || name.StartsWith("_native_main.") || name.StartsWith("dev_session.") ||
   name.StartsWith("_asyncio.") || name.StartsWith("_socket.") || name.StartsWith("_winrt_windows_devices_enumeration.");
 }
 public static LiveContext Inspect(uint pid) {
  LiveContext r=new LiveContext(); r.pid=pid;
  IntPtr handle=IntPtr.Zero,token=IntPtr.Zero;
  try {
   using(Process p=Process.GetProcessById((int)pid)) {
    r.state="running"; r.sessionId=p.SessionId; r.window=p.MainWindowHandle.ToInt64();
    try { foreach(ProcessModule module in p.Modules) {
     string name=module.ModuleName.ToLowerInvariant(); if(RelevantModule(name)) r.loadedModules.Add(name);
    }} catch(System.ComponentModel.Win32Exception e) { r.moduleQueryError=e.NativeErrorCode; }
   }
   handle=OpenProcess(0x1000,false,pid);
   int returned,value;
   if(handle!=IntPtr.Zero && OpenProcessToken(handle,8,out token) && GetTokenInformation(token,20,out value,4,out returned)) {
    r.elevated=value!=0; r.elevationKnown=true;
   } else { r.elevationError=Marshal.GetLastWin32Error(); }
  } catch(System.ComponentModel.Win32Exception e) { r.state="inaccessible"; r.queryError=e.NativeErrorCode; }
    catch(ArgumentException) { r.state="exited"; }
  finally { if(token!=IntPtr.Zero) CloseHandle(token); if(handle!=IntPtr.Zero) CloseHandle(handle); }
  return r;
 }
 static Progress ReadProgress(string path,long elapsed) {
  Progress p=new Progress(); p.elapsedMs=elapsed;
  try {
   if(!File.Exists(path)) { p.diagnostic="missing"; return p; }
   if(new FileInfo(path).Length>65536) { p.diagnostic="too_large"; return p; }
   string text=File.ReadAllText(path);
   var matches=System.Text.RegularExpressions.Regex.Matches(text,"\"stage\"\\s*:\\s*\"([a-z_]{1,32})\"");
   p.diagnostic="no_valid_stage";
   if(matches.Count>0) {
    string stage=matches[matches.Count-1].Groups[1].Value;
    string[] allowed={"starting","runtime_hooks","entrypoint_import","scan_import","loop_init","loop_shutdown","winrt_import","selector","enumerate","device_rows","result_write","finished"};
    if(Array.IndexOf(allowed,stage)>=0) { p.diagnostic="available"; p.stage=stage; }
   }
  } catch { p.diagnostic="read_failed"; }
  return p;
 }
 public static Result Run(string exe,string output,string diagnostic) {
  Result result=new Result(); PI process=new PI(); IntPtr desktop=IntPtr.Zero,job=IntPtr.Zero;
  Stopwatch watch=Stopwatch.StartNew();
  try {
   string name="RemoteMicReport_"+Guid.NewGuid().ToString("N");
   desktop=CreateDesktopW(name,IntPtr.Zero,IntPtr.Zero,0,0x10000000,IntPtr.Zero);
   if(desktop==IntPtr.Zero) { result.state="desktop_failed"; result.winerror=Marshal.GetLastWin32Error(); return result; }
   job=CreateJobObjectW(IntPtr.Zero,IntPtr.Zero); JobLimits limits=new JobLimits(); limits.basic.flags=0x2000;
   if(job==IntPtr.Zero || !SetInformationJobObject(job,9,ref limits,Marshal.SizeOf(limits))) {
    result.state="cleanup_guard_failed"; result.winerror=Marshal.GetLastWin32Error(); return result;
   }
   SI startup=new SI(); startup.cb=Marshal.SizeOf(startup); startup.desktop="winsta0\\"+name; startup.flags=1;
   string args="\""+exe+"\" --list-remote-devices \""+output+"\" \""+diagnostic+"\"";
   if(!CreateProcessW(exe,new StringBuilder(args),IntPtr.Zero,IntPtr.Zero,false,0x08000004,IntPtr.Zero,Path.GetDirectoryName(exe),ref startup,out process)) {
    result.state="start_failed"; result.winerror=Marshal.GetLastWin32Error(); return result;
   }
   if(!AssignProcessToJobObject(job,process.process)) {
    result.state="cleanup_guard_failed"; result.winerror=Marshal.GetLastWin32Error(); return result;
   }
   if(ResumeThread(process.thread)==0xffffffff) { result.state="resume_failed"; result.winerror=Marshal.GetLastWin32Error(); return result; }
   CloseHandle(process.thread); process.thread=IntPtr.Zero; result.pid=process.pid;
   long[] samples={1000,4000,8000,10000}; int sample=0;
   while(WaitForSingleObject(process.process,150)==258 && watch.ElapsedMilliseconds<12000) {
    if(sample<samples.Length && watch.ElapsedMilliseconds>=samples[sample]) {
     result.progress.Add(ReadProgress(diagnostic,watch.ElapsedMilliseconds)); sample++;
    }
    if(result.errorDialog) continue;
    EnumDesktopWindows(desktop,delegate(IntPtr window,IntPtr data) {
     uint pid; GetWindowThreadProcessId(window,out pid); if(pid!=process.pid) return true;
     StringBuilder title=new StringBuilder(256); GetWindowTextW(window,title,title.Capacity);
     if(title.ToString().IndexOf("Unhandled exception",StringComparison.OrdinalIgnoreCase)<0 && title.ToString().IndexOf("Error",StringComparison.OrdinalIgnoreCase)<0 && title.ToString().IndexOf("错误",StringComparison.OrdinalIgnoreCase)<0) return true;
     result.errorDialog=true; StringBuilder body=new StringBuilder(); int queried=0; Stopwatch bodyTimer=Stopwatch.StartNew();
     EnumChildWindows(window,delegate(IntPtr child,IntPtr unused) {
      if(body.Length>=16000 || queried++>=16 || bodyTimer.ElapsedMilliseconds>800) return false;
      StringBuilder text=new StringBuilder(2048); UIntPtr copied;
      SendMessageTimeoutW(child,13,(UIntPtr)2048,text,2,50,out copied);
      body.AppendLine(text.ToString()); return true;
     },IntPtr.Zero); result.dialogBody=body.ToString(); return false;
    },IntPtr.Zero);
   }
   uint code; GetExitCodeProcess(process.process,out code);
   if(WaitForSingleObject(process.process,0)==258) {
    try {
     using(Process p=Process.GetProcessById((int)process.pid)) foreach(ProcessModule module in p.Modules) {
      string n=module.ModuleName.ToLowerInvariant();
      if(RelevantModule(n)) result.loadedModules.Add(n);
     }
    } catch(System.ComponentModel.Win32Exception e) { result.moduleQueryError=e.NativeErrorCode; }
    result.state="timeout"; TerminateProcess(process.process,91); result.exitConfirmed=WaitForSingleObject(process.process,3000)==0;
    GetExitCodeProcess(process.process,out result.exitCode);
   } else { result.state="exited"; result.exitCode=code; result.exitConfirmed=true; }
   return result;
  } finally {
   result.elapsedMs=watch.ElapsedMilliseconds;
   if(process.process!=IntPtr.Zero) {
    if(WaitForSingleObject(process.process,0)==258) { TerminateProcess(process.process,91); WaitForSingleObject(process.process,3000); }
    CloseHandle(process.process);
   }
   if(process.thread!=IntPtr.Zero) CloseHandle(process.thread);
   if(job!=IntPtr.Zero) CloseHandle(job);
   if(desktop!=IntPtr.Zero) CloseDesktop(desktop);
  }
 }
}
'@
}
function Run-Application {
    $r=[ordered]@{ state='running'; phase='locate_application' }; Save-Step $r
    $folder=[IO.Path]::GetDirectoryName($env:REMOTE_MIC_REPORT_SELF)
    $local=@(Get-ChildItem -LiteralPath $folder -Filter '*.exe' -File | Where-Object Name -like '无线麦 win版 *' | ForEach-Object FullName)
    $paths=@($local)
    foreach ($process in @(Get-Process -Name '无线麦 win版 *' -ErrorAction SilentlyContinue)) {
        try { $paths += $process.MainModule.FileName } catch { }
    }
    $valid=@()
    foreach ($path in @($paths | Select-Object -Unique)) {
        try {
            $info=[Diagnostics.FileVersionInfo]::GetVersionInfo($path)
            if ($info.ProductName -eq '无线麦 win版' -and $info.ProductVersion -match '^1\.0\.(102|103)(?:-candidate\.\d+)?$') {
                $valid += [pscustomobject]@{ path=$path; version=$info.ProductVersion }
            }
        } catch { }
    }
    $nextToScript=@($valid | Where-Object { $_.path -in $local })
    if ($nextToScript.Count -eq 1) { $valid=$nextToScript; $r.selection='next_to_script' }
    if ($valid.Count -ne 1) {
        $r.state='skipped'; $r.reason=if ($valid.Count -eq 0) {'application_not_found'} else {'multiple_applications'}
        $r.valid_application_count=$valid.Count; Save-Step $r; return
    }
    $exe=$valid[0].path
    $r.version=$valid[0].version; $r.exe_sha256=(Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
    $r.modules=@()
    $runtime=Join-Path ([IO.Path]::GetDirectoryName($exe)) '程序文件'
    foreach ($pattern in @('python312.dll','ovb_rc003\_native_main*.pyd','ovb_rc003\remote_selection*.pyd','ovb_rc003\dev_session*.pyd')) {
        foreach ($file in @(Get-ChildItem -Path (Join-Path $runtime $pattern) -File -ErrorAction SilentlyContinue)) {
            $r.modules += [pscustomobject]@{ name=$file.Name; bytes=$file.Length;
                sha256=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash }
        }
    }
    $r.phase='native_launcher'; Save-Step $r
    Initialize-ReportNative
    $output=Join-Path $env:REMOTE_MIC_REPORT_WORK 'application-rows.private.json'
    $diagnostic=Join-Path $env:REMOTE_MIC_REPORT_WORK 'application-diagnostic.private.json'
    $capture=[RemoteMicReportChild]::Run($exe,$output,$diagnostic)
    $r.child=[ordered]@{ state=$capture.state; winerror=$capture.winerror; pid=$capture.pid;
        exit_code=$capture.exitCode; elapsed_ms=$capture.elapsedMs; exit_confirmed=$capture.exitConfirmed;
        error_dialog=$capture.errorDialog; loaded_modules=$capture.loadedModules; module_query_error=$capture.moduleQueryError;
        progress=@($capture.progress) }
    # Raw dialog text stays in memory; only fixed type/code/source markers leave.
    $r.dialog=[ordered]@{ error_types=@([regex]::Matches($capture.dialogBody,'(?m)^[ \t]*([A-Za-z_][A-Za-z_0-9]{0,79}(?:Error|Exception))\s*:') |
        ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique);
        native_codes=@([regex]::Matches($capture.dialogBody,'(?i)\b(?:WinError\s+\d{1,10}|0x[0-9a-f]{8})\b') |
        ForEach-Object Value | Select-Object -Unique) }
    $frames=@()
    foreach ($match in [regex]::Matches($capture.dialogBody,'(?m)File "[^"\r\n]*[\\/]ovb_rc003[\\/]([a-z_0-9]+)\.py", line (\d{1,6})')) {
        $frames += [pscustomobject]@{ module=('ovb_rc003.'+$match.Groups[1].Value); line=[int]$match.Groups[2].Value }
    }
    $r.dialog.frames=@($frames | Select-Object -Last 6)
    $r.phase='read_child_evidence'; Save-Step $r
    $r.diagnostic=[ordered]@{ available=$false; reason='missing' }
    if (Test-Path -LiteralPath $diagnostic) {
        $r.diagnostic.reason='invalid_or_empty'
        if ((Get-Item -LiteralPath $diagnostic).Length -gt 65536) { $r.diagnostic.reason='too_large' }
        else {
            foreach ($line in [IO.File]::ReadAllLines($diagnostic)) {
                try {
                    $value=$line | ConvertFrom-Json
                    if ($value.stage -match '^[a-z_]{1,32}$' -and $value.state -in @('running','success','failed')) {
                        $r.diagnostic=[ordered]@{ available=$true; stage=$value.stage; state=$value.state }
                        if ($value.failure_stage -match '^[a-z_]{1,32}$') { $r.diagnostic.failure_stage=$value.failure_stage }
                        $errors=@()
                        foreach ($error in @($value.errors | Select-Object -First 2)) {
                            if ($null -eq $error -or $error.error_type -notmatch '^[A-Za-z_][A-Za-z_0-9]{0,79}$') { continue }
                            $entry=[ordered]@{ type=$error.error_type }
                            foreach ($code in @('winerror','hresult','errno')) {
                                if ($error.$code -is [int] -or $error.$code -is [long]) { $entry[$code]=$error.$code }
                            }
                            $entry.frames=@($error.frames | Where-Object {
                                $_.module -match '^(?:ovb_rc003\.[a-z_0-9.]+|remote_scan_bootstrap|pyi_rth_[a-z_0-9]+)$' -and
                                $_.module.Length -le 120 -and $_.line -is [int] -and $_.line -gt 0 -and $_.line -le 1000000
                            } | Select-Object -Last 6 | ForEach-Object { [pscustomobject]@{ module=$_.module; line=$_.line } })
                            $errors += [pscustomobject]$entry
                        }
                        $r.diagnostic.errors=$errors
                    }
                } catch { $r.diagnostic.parse_partial=$true }
            }
        }
    }
    if ($capture.state -eq 'exited' -and $capture.exitCode -eq 0 -and (Test-Path -LiteralPath $output)) {
        if ((Get-Item -LiteralPath $output).Length -le 65536) {
            $raw=Get-Content -LiteralPath $output -Raw -Encoding UTF8
            $parsed=$raw | ConvertFrom-Json
            $rows=@()
            if ($null -ne $parsed) { $rows=@($parsed) }
            $validRows=($raw -match '^\s*\[' -and $rows.Count -le 128)
            $keys=@{}
            foreach ($row in $rows) {
                if ($row.key -cnotmatch '^[a-f0-9]{64}$' -or $row.profile -notin @('','chromecast-remote','xiaomi-rc003') -or
                    $keys.ContainsKey([string]$row.key)) { $validRows=$false; break }
                $keys[[string]$row.key]=$true
            }
            if ($validRows) {
                $r.result_count=$rows.Count
                $r.supported_count=@($rows | Where-Object { $_.profile -in @('chromecast-remote','xiaomi-rc003') }).Count
                $r.state='success'
            } else { $r.state='invalid_result' }
        } else { $r.state='invalid_result' }
    } else { $r.state=if ($capture.state -eq 'timeout') {'timeout'} else {'failed'} }
    $r.phase='finished'; Save-Step $r
}

function Safe-ScanRecord($Value) {
    $r=[ordered]@{}
    $stages=@('starting','runtime_hooks','entrypoint_import','scan_import','loop_init','loop_shutdown',
        'winrt_import','selector','enumerate','device_rows','result_write','finished','subprocess',
        'temporary_directory','temporary_cleanup','result_read')
    foreach ($key in @('stage','child_stage','failure_stage')) {
        if ($Value.$key -is [string] -and $Value.$key -in $stages) { $r[$key]=$Value.$key }
    }
    foreach ($key in @('state','child_state')) {
        if ($Value.$key -in @('running','success','failed','timeout','cancelled')) { $r[$key]=$Value.$key }
    }
    foreach ($key in @('elapsed_ms','exit_code','child_pid','child_exit_code','enumerated','supported',
        'unsupported','identity_failed','result_count','write_failures','diagnostic_schema')) {
        $number=$Value.$key
        if (($number -is [int] -or $number -is [long]) -and $number -ge -2147483648 -and $number -le 4294967295) { $r[$key]=$number }
    }
    foreach ($key in @('child_started','child_exited','evidence_unavailable','evidence_partial')) {
        if ($Value.$key -is [bool]) { $r[$key]=$Value.$key }
    }
    if ($Value.app_version -is [string] -and $Value.app_version -match '^\d+\.\d+\.\d+(?:-candidate\.\d+)?$') { $r.app_version=$Value.app_version }
    if ($Value.runtime -in @('source','frozen')) { $r.runtime=$Value.runtime }
    if ($Value.evidence_channel -in @('ready','open_failed','inherit_failed')) { $r.evidence_channel=$Value.evidence_channel }
    if ($Value.evidence_reason -in @('missing','empty','invalid_record','read_failed','too_large','collector_failed')) { $r.evidence_reason=$Value.evidence_reason }
    if ($Value.child_steps -is [array]) { $r.child_steps=@($Value.child_steps | Where-Object { $_ -is [string] -and $_ -in $stages } | Select-Object -First 16) }
    foreach ($key in @('errors','child_errors','identity_errors','evidence_errors','evidence_channel_errors','child_status_errors','write_errors','channel_errors')) {
        if ($Value.$key -isnot [array]) { continue }
        $r[$key]=@()
        foreach ($error in @($Value.$key | Select-Object -First 2)) {
            if ($error.error_type -isnot [string] -or $error.error_type -cnotmatch '^[A-Za-z_][A-Za-z_0-9]{0,79}$') { continue }
            $entry=[ordered]@{ type=$error.error_type }
            foreach ($code in @('winerror','errno','hresult')) {
                $number=$error.$code
                if (($number -is [int] -or $number -is [long]) -and $number -ge -2147483648 -and $number -le 4294967295) { $entry[$code]=$number }
            }
            $entry.frames=@($error.frames | Where-Object {
                $_.module -is [string] -and $_.module -cmatch '^(?:ovb_rc003\.[a-z_0-9.]+|remote_scan_bootstrap|pyi_rth_[a-z_0-9]+)$' -and
                $_.module.Length -le 120 -and $_.line -is [int] -and $_.line -gt 0 -and $_.line -le 1000000
            } | Select-Object -Last 6 | ForEach-Object { [pscustomobject]@{ module=$_.module; line=$_.line } })
            $r[$key]+=[pscustomobject]$entry
        }
    }
    return [pscustomobject]$r
}
function Read-RecentScanLog {
    $r=[ordered]@{ state='success'; lookback_hours=24; record_limit=40; sources=@(); records=@(); malformed_scan_rows=0 }
    $base=$env:LOCALAPPDATA
    if (-not $base) { $base=[Environment]::GetFolderPath('UserProfile') }
    $folder=Join-Path $base 'RemoteMic\RC003\logs'
    foreach ($name in @('app.log','app.log.1','app.log.2','app.log.3')) {
        $path=Join-Path $folder $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $meta=[ordered]@{ file=$name; state='running' }; $stream=$null
        try {
            $stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            $length=$stream.Length; $count=[int][Math]::Min($length,524288); $offset=$length-$count
            [void]$stream.Seek($offset,[IO.SeekOrigin]::Begin)
            $buffer=New-Object byte[] $count; $read=0
            while ($read -lt $count) { $part=$stream.Read($buffer,$read,$count-$read); if ($part -eq 0) { break }; $read+=$part }
            $text=[Text.Encoding]::UTF8.GetString($buffer,0,$read)
            if ($offset -gt 0) { $newline=$text.IndexOf("`n"); if ($newline -ge 0) { $text=$text.Substring($newline+1) } else { $text='' } }
            $meta.state='read'; $meta.bytes=$read; $meta.tail_only=($offset -gt 0)
            foreach ($line in ($text -split "`n")) {
                if ($line -notmatch '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) .*Remote device scan: (\{.*\})\s*$') { continue }
                $timestamp=$Matches[1]; $json=$Matches[2]
                try {
                    $when=[DateTime]::ParseExact($timestamp,'yyyy-MM-dd HH:mm:ss,fff',[Globalization.CultureInfo]::InvariantCulture)
                    if ($when -lt [DateTime]::Now.AddHours(-24)) { continue }
                    $raw=$json | ConvertFrom-Json
                    $safe=Safe-ScanRecord $raw
                    if (-not $safe.state) { throw 'Invalid scan record' }
                    $r.records += [pscustomobject]@{ time=$when.ToString('yyyy-MM-ddTHH:mm:ss.fff'); source=$name; scan=$safe;
                        version_recorded=($null -ne $safe.app_version) }
                } catch { $r.malformed_scan_rows++ }
            }
        } catch { $meta.state='failed'; $meta.errors=@(Safe-Error $_.Exception) }
        finally { if ($null -ne $stream) { $stream.Dispose() } }
        $r.sources += [pscustomobject]$meta
    }
    $all=@($r.records | Sort-Object time)
    $r.available_scan_rows=$all.Count; $r.records=@($all | Select-Object -Last 40)
    $r.omitted_older_scan_rows=[Math]::Max(0,$all.Count-$r.records.Count)
    if ($r.sources.Count -eq 0) { $r.state='not_found' }
    return [pscustomobject]$r
}
function Read-WindowMarkers($Handle,$Role) {
    $r=[ordered]@{ state='running'; markers=@(); error_types=@(); frames=@() }
    if (-not $Handle) { $r.state='no_window'; return [pscustomobject]$r }
    try {
        Add-Type -AssemblyName UIAutomationClient
        Add-Type -AssemblyName UIAutomationTypes
        $window=[Windows.Automation.AutomationElement]::FromHandle([intptr]$Handle)
        $nodes=$window.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition)
        $r.nodes_observed=$nodes.Count; $r.nodes_checked=[Math]::Min(600,$nodes.Count)
        $patterns=[ordered]@{ device_list_read_failed='读取设备失败'; no_paired_remote_text='没有可用的已配对遥控器';
            reading_devices_text='正在读取 Windows 已配对'; select_device_text='请选择设备'; select_device_entry_text='选择设备';
            start_service_text='启动服务'; restart_service_text='重启服务'; unselected_device_text='未选择设备' }
        for ($i=0; $i -lt $r.nodes_checked; $i++) {
            $name=[string]$nodes[$i].Current.Name
            foreach ($key in $patterns.Keys) { if ($name.Contains($patterns[$key])) { $r.markers+=$key } }
            $text=$name
            if ($Role -eq 'scan_child' -and $nodes[$i].Current.ControlType -eq [Windows.Automation.ControlType]::Edit) {
                try {
                    $value=$nodes[$i].GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern)
                    if ($value.Current.IsReadOnly) { $text+="`n"+$value.Current.Value }
                } catch { }
            }
            $r.error_types+=@([regex]::Matches($text,'(?m)^[ \t]*([A-Za-z_][A-Za-z_0-9]{0,79}(?:Error|Exception))\s*:') |
                ForEach-Object { $_.Groups[1].Value })
            foreach ($match in [regex]::Matches($text,'(?m)File "[^"\r\n]*[\\/]ovb_rc003[\\/]([a-z_0-9]+)\.py", line (\d{1,6})')) {
                $r.frames+=[pscustomobject]@{ module=('ovb_rc003.'+$match.Groups[1].Value); line=[int]$match.Groups[2].Value }
            }
        }
        $r.markers=@($r.markers | Select-Object -Unique)
        $r.error_types=@($r.error_types | Select-Object -Unique -First 4); $r.frames=@($r.frames | Select-Object -Last 6)
        $r.state=if ($r.nodes_checked -gt 0) {'success'} else {'no_accessible_children'}
    } catch { $r.state='unavailable'; $r.errors=@(Safe-Error $_.Exception) }
    return [pscustomobject]$r
}
function Run-Context {
    $r=[ordered]@{ state='running'; phase='recent_scan_logs'; main_processes=@(); active_scan_children=@() }; Save-Step $r
    $r.recent_logs=Read-RecentScanLog; $r.phase='running_processes'; Save-Step $r
    Initialize-ReportNative
    $session=[Diagnostics.Process]::GetCurrentProcess().SessionId
    $collector=[RemoteMicReportChild]::Inspect([uint32]$PID)
    $r.collector=[ordered]@{ session=$session; elevated=if ($collector.elevationKnown) {$collector.elevated} else {$null}; elevation_error=$collector.elevationError }
    $processes=@(Get-CimInstance Win32_Process -Filter "Name LIKE '无线麦 win版 %.exe'" -OperationTimeoutSec 5 |
        Where-Object SessionId -eq $session)
    $windows=@()
    foreach ($process in $processes) {
        $entry=[ordered]@{ pid=$process.ProcessId; parent_pid=$process.ParentProcessId }
        if ($null -ne $process.CreationDate) { $entry.started_at=$process.CreationDate.ToString('o') }
        $arguments=[string]$process.CommandLine
        $entry.command_line_available=([bool]$arguments)
        $entry.known_flags=@(@('--settings','--background','--bridge','--list-remote-devices','--isolated-test','--remote-mic-dev-session') |
            Where-Object { $arguments -match ('(?:^|\s)'+[regex]::Escape($_)+'(?:\s|$)') })
        $entry.role=if ($entry.known_flags -contains '--list-remote-devices') {'scan_child'} elseif ($arguments -match '\s--(?:diagnose-|preflight-|chromecast-|probe-|request-exit|install-hid|uninstall-hid|capture-)') {'other_worker'} else {'desktop'}
        try {
            $info=[Diagnostics.FileVersionInfo]::GetVersionInfo($process.ExecutablePath)
            if ($info.ProductName -ne '无线麦 win版' -or $info.ProductVersion -notmatch '^1\.0\.(102|103)(?:-candidate\.\d+)?$') { continue }
            $entry.version=$info.ProductVersion
        } catch { $entry.version_state='unavailable'; $entry.version_errors=@(Safe-Error $_.Exception) }
        $context=[RemoteMicReportChild]::Inspect([uint32]$process.ProcessId)
        $entry.native=[ordered]@{ state=$context.state; elevated=if ($context.elevationKnown) {$context.elevated} else {$null}; elevation_error=$context.elevationError;
            query_error=$context.queryError; loaded_modules=$context.loadedModules; module_query_error=$context.moduleQueryError }
        if ($entry.role -eq 'scan_child') {
            $r.active_scan_children += [pscustomobject]$entry
            $windows += [pscustomobject]@{ pid=$process.ProcessId; handle=$context.window; role='scan_child' }
            continue
        }
        if ($entry.role -ne 'desktop') { continue }
        $entry.compatibility=[ordered]@{ state='not_set'; flags=@() }
        foreach ($hive in @('HKCU:','HKLM:')) {
            try {
                $layer=Get-ItemPropertyValue -LiteralPath ($hive+'\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers') -Name $process.ExecutablePath -ErrorAction Stop
                $entry.compatibility.state='present'
                $entry.compatibility.flags+=@(([string]$layer -split '\s+') | Where-Object {
                    $_ -in @('RUNASADMIN','RUNASINVOKER','WIN7RTM','WIN8RTM','VISTARTM','WINXPSP3','HIGHDPIAWARE','DPIUNAWARE','GDIDPISCALING','DISABLEDXMAXIMIZEDWINDOWEDMODE') })
            } catch [System.Management.Automation.ItemNotFoundException] { }
            catch { $entry.compatibility.query_state='unavailable' }
        }
        $r.main_processes += [pscustomobject]$entry
        $windows += [pscustomobject]@{ pid=$process.ProcessId; handle=$context.window; role='desktop' }
    }
    $r.running_desktop_count=$r.main_processes.Count
    $r.phase='application_events'; Save-Step $r
    $r.application_errors=@()
    try {
        $events=@(Get-WinEvent -FilterHashtable @{LogName='Application';Id=1000;StartTime=[DateTime]::Now.AddHours(-2)} -MaxEvents 64 -ErrorAction Stop)
        foreach ($event in $events) {
            $xml=[xml]$event.ToXml(); $fields=@{}
            foreach ($node in $xml.Event.EventData.Data) { $fields[[string]$node.Name]=[string]$node.'#text' }
            if ([IO.Path]::GetFileName([string]$fields.AppName) -notmatch '^无线麦 win版 1\.0\.(102|103).*\.exe$') { continue }
            $row=[ordered]@{ time=$event.TimeCreated.ToString('o'); event=1000 }
            foreach ($key in @('ExceptionCode','FaultingOffset','ProcessId')) { if ($fields[$key] -match '^(?:0x)?[0-9a-fA-F]{1,16}$') { $row[$key]=$fields[$key] } }
            $module=[IO.Path]::GetFileName([string]$fields.ModuleName)
            if ($module -cmatch '^[A-Za-z_0-9.-]{1,120}\.(?:dll|pyd|exe)$') { $row.module=$module }
            $r.application_errors += [pscustomobject]$row
        }
        $r.application_events_state='success'
    } catch { $r.application_events_state='unavailable_or_empty'; $r.application_events_errors=@(Safe-Error $_.Exception) }
    $r.phase='window_state'; Save-Step $r
    $r.windows=@()
    foreach ($window in $windows) {
        $r.windows += [pscustomobject]@{ pid=$window.pid; role=$window.role; ui=(Read-WindowMarkers $window.handle $window.role) }
        Save-Step $r
    }
    # Time membership is not proof of a source version; preserve that distinction.
    $r.log_attribution='shared_user_log_versions_only_when_recorded'
    $r.phase='finished'; $r.state='success'; Save-Step $r
}

if ($env:REMOTE_MIC_REPORT_MODE) {
    try {
        switch ($env:REMOTE_MIC_REPORT_MODE) {
            'system' { Run-System }
            'winrt' { Run-WinRT }
            'application' { Run-Application }
            'context' { Run-Context }
            default { throw 'Invalid report section' }
        }
    } catch {
        $last=[ordered]@{ state='failed'; phase='worker'; errors=@(Safe-Error $_.Exception) }
        $path=Join-Path $env:REMOTE_MIC_REPORT_WORK ($env:REMOTE_MIC_REPORT_MODE+'.json')
        if (Test-Path -LiteralPath $path) {
            try { $previous=Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json; $last.phase=$previous.phase; $last.previous=$previous } catch { }
        }
        Save-Step $last
    }
    return
}

Write-Host '无线麦设备读取诊断：请保持无线麦的报错界面，正在生成报告，通常需要 30～90 秒。'
Write-Host '只读检查，不更改蓝牙、驱动或软件设置，也不会自动上传。'
$self=$env:REMOTE_MIC_REPORT_SELF
$work=Join-Path ([IO.Path]::GetTempPath()) ('RemoteMicDeviceReport_'+[guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($work) | Out-Null
$worker=Join-Path $work 'worker.ps1'
$body=[IO.File]::ReadAllText($self,[Text.Encoding]::UTF8)
$marker=[regex]::Match($body,'(?m)^# POWERSHELL_PAYLOAD\r?$')
[IO.File]::WriteAllText($worker,$body.Substring($marker.Index+$marker.Length),(New-Object Text.UTF8Encoding($true)))
$report=[ordered]@{ schema='remote_mic_device_read_report_2'; collected_at=[DateTimeOffset]::Now.ToString('o');
    script_sha256=(Get-FileHash -LiteralPath $self -Algorithm SHA256).Hash; sections=[ordered]@{} }
try {
    foreach ($step in @('system','context','winrt','application')) {
        Write-Host ('正在检查：'+(@{system='系统和蓝牙驱动';context='主程序现场、同期日志和系统错误';winrt='Windows 独立读取配对设备';application='无线麦现有程序的设备读取'})[$step])
        $start=New-Object Diagnostics.ProcessStartInfo
        $start.FileName=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $start.Arguments='-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$worker+'"'
        $start.UseShellExecute=$false; $start.CreateNoWindow=$true; $start.WindowStyle='Hidden'
        $start.EnvironmentVariables['REMOTE_MIC_REPORT_WORK']=$work
        $start.EnvironmentVariables['REMOTE_MIC_REPORT_MODE']=$step
        $process=$null
        try {
            $process=[Diagnostics.Process]::Start($start)
            $limit=if ($step -eq 'application') {23000} elseif ($step -eq 'context') {25000} else {15000}
            $finished=$process.WaitForExit($limit)
            if (-not $finished) { $process.Kill(); [void]$process.WaitForExit(3000) }
            $path=Join-Path $work ($step+'.json')
            if (Test-Path -LiteralPath $path) { $value=Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json }
            else { $value=[pscustomobject]@{ state='no_result'; phase='worker_start' } }
            if (-not $finished) {
                $value | Add-Member -NotePropertyName watchdog_timeout -NotePropertyValue $true -Force
                $value.state='timeout'
            }
            $report.sections[$step]=$value
        } catch { $report.sections[$step]=[pscustomobject]@{ state='collector_failed'; errors=@(Safe-Error $_.Exception) } }
        finally {
            if ($null -ne $process) {
                try { if (-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(3000) } } catch { }
                $process.Dispose()
            }
        }
    }
    $desktop=[Environment]::GetFolderPath('Desktop')
    $destination=Join-Path $desktop ('无线麦设备读取报告-'+[DateTime]::Now.ToString('yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,6)+'.txt')
    $labels=@{success='成功';failed='失败';timeout='超时';skipped='未检测';collector_failed='采集失败';no_result='没有返回记录';invalid_result='返回结果格式异常';running='采集未完成'}
    $native=$report.sections.winrt; $app=$report.sections.application
    $text=@('无线麦设备读取诊断报告','请把本 TXT 文件原样发送给开发者。',
        '本报告只检查设备列表读取，不代表按键和语音已验证。',
        ('Windows 独立读取：'+$labels[[string]$native.state]),('无线麦程序读取：'+$labels[[string]$app.state]))
    $live=$report.sections.context
    if ($null -ne $live.running_desktop_count) { $text+=('正在运行的无线麦主界面：'+$live.running_desktop_count) }
    if ($null -ne $live.recent_logs) { $text+=('已附带最近的原程序读取记录：'+$live.recent_logs.records.Count+' 条') }
    if ($app.reason -in @('application_not_found','multiple_applications')) {
        $text+='程序项未完成：请仅打开要检查的 .102/.103，或将脚本放在其 EXE 旁重新双击。'
    }
    $text+=@('','以下为详细诊断数据（不包含账号名、电脑名、蓝牙地址、设备原始 ID 或文件路径）：',
        ($report | ConvertTo-Json -Depth 16))
    [IO.File]::WriteAllText($destination,($text -join "`r`n"),(New-Object Text.UTF8Encoding($true)))
    Write-Host ''; Write-Host '报告已生成，请发送桌面上的这个 TXT 文件：' -ForegroundColor Green
    Write-Host $destination
} finally {
    # Only direct files of this run, never enumerate or recursively delete a computed parent.
    $expected=[IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetFileName($work))))
    if ([IO.Path]::GetFullPath($work) -eq $expected -and [IO.Path]::GetFileName($work) -match '^RemoteMicDeviceReport_[a-f0-9]{32}$') {
        Get-ChildItem -LiteralPath $work -File | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $work -ErrorAction SilentlyContinue
    }
}
