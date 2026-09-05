#requires -Version 5.1
<#
.SYNOPSIS
    Stops source processes started by build/run-dev.ps1.

.DESCRIPTION
    Matching requires both the exact interpreter under this checkout's .venv
    and the private development-session command-line marker. Packaged
    RemoteMicRC003.exe processes and unrelated Python programs are untouched.
#>

param(
    [ValidateRange(1, 30)]
    [int]$GracefulTimeoutSeconds = 5
)

$ErrorActionPreference = "Stop"
$RC003Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DevMarker = "--remote-mic-dev-session"
$CurrentSessionId = [uint32][System.Diagnostics.Process]::GetCurrentProcess().SessionId
$InterpreterPaths = @(
    (Join-Path $RC003Root ".venv\Scripts\python.exe"),
    (Join-Path $RC003Root ".venv\Scripts\pythonw.exe")
) | Where-Object {
    Test-Path -LiteralPath $_ -PathType Leaf
} | ForEach-Object {
    [System.IO.Path]::GetFullPath($_)
}

if ($InterpreterPaths.Count -eq 0) {
    Write-Host "[stop-dev] no local virtual environment; nothing to stop"
    exit 0
}

$AllDevProcesses = Get-CimInstance Win32_Process | Where-Object {
    $executablePath = $_.ExecutablePath
    $commandLine = $_.CommandLine
    if (-not $executablePath -or -not $commandLine) {
        return $false
    }
    $exactInterpreter = $InterpreterPaths | Where-Object {
        [string]::Equals(
            $_,
            $executablePath,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    return $exactInterpreter -and $commandLine.Contains($DevMarker)
}
$OtherSessionDevProcesses = @(
    $AllDevProcesses | Where-Object {
        [uint32]$_.SessionId -ne $CurrentSessionId
    }
)
if ($OtherSessionDevProcesses.Count -gt 0) {
    throw "A marked source process is running in another Windows session."
}
$DevProcesses = @(
    $AllDevProcesses | Where-Object {
        [uint32]$_.SessionId -eq $CurrentSessionId
    }
)

if (-not $DevProcesses) {
    Write-Host "[stop-dev] no marked source development session is running"
    exit 0
}

$PackagedProcesses = @(
    Get-CimInstance Win32_Process -Filter "Name = 'RemoteMicRC003.exe'" |
        Where-Object { [uint32]$_.SessionId -eq $CurrentSessionId }
)
if ($PackagedProcesses.Count -gt 0) {
    throw "A packaged RemoteMicRC003 process is running; stop-dev will not request its exit."
}

$windowProbeType = [System.Management.Automation.PSTypeName]'RemoteMicBuild.WindowProbe'
if (-not $windowProbeType.Type) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

namespace RemoteMicBuild {
    public static class WindowProbe {
        private delegate bool EnumWindowsProc(IntPtr window, IntPtr parameter);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr GetProp(IntPtr window, string propertyName);

        public static uint FindOwner(string propertyName) {
            uint found = 0;
            EnumWindows(delegate(IntPtr window, IntPtr parameter) {
                if (GetProp(window, propertyName) == IntPtr.Zero) {
                    return true;
                }
                GetWindowThreadProcessId(window, out found);
                return false;
            }, IntPtr.Zero);
            return found;
        }
    }
}
"@
}
$DesktopOwnerProcessId = [RemoteMicBuild.WindowProbe]::FindOwner(
    "RemoteMicRC003.ApplicationExitRequestV3"
)
$DevProcessIds = @($DevProcesses | ForEach-Object { [uint32]$_.ProcessId })
if (
    $DesktopOwnerProcessId -eq 0 -or
    $DevProcessIds -notcontains [uint32]$DesktopOwnerProcessId
) {
    throw "The marked processes do not own this checkout's V3 desktop window."
}

$Python = Join-Path $RC003Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "The source exit entry point requires .venv\Scripts\python.exe."
}

Write-Host "[stop-dev] requesting normal application exit"
$PreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $RC003Root "src"
    & $Python -m ovb_rc003 --request-exit
    if ($LASTEXITCODE -ne 0) {
        throw "The source application did not complete normal exit (code $LASTEXITCODE)."
    }
} finally {
    $env:PYTHONPATH = $PreviousPythonPath
}

$deadline = [DateTime]::UtcNow.AddSeconds($GracefulTimeoutSeconds)
do {
    $remaining = @(
        Get-CimInstance Win32_Process | Where-Object {
            $executablePath = $_.ExecutablePath
            $commandLine = $_.CommandLine
            if (
                -not $executablePath -or
                -not $commandLine -or
                [uint32]$_.SessionId -ne $CurrentSessionId
            ) {
                return $false
            }
            $exactInterpreter = $InterpreterPaths | Where-Object {
                [string]::Equals(
                    $_,
                    $executablePath,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            }
            return $exactInterpreter -and $commandLine.Contains($DevMarker)
        }
    )
    if ($remaining.Count -eq 0) {
        break
    }
    Start-Sleep -Milliseconds 100
} while ([DateTime]::UtcNow -lt $deadline)

if ($remaining.Count -gt 0) {
    throw "A marked source process is still running; build files were not touched."
}

Write-Host "[stop-dev] marked source development session stopped"
