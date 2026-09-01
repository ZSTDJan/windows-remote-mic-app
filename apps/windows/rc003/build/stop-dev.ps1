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

$DevProcesses = Get-CimInstance Win32_Process | Where-Object {
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

if (-not $DevProcesses) {
    Write-Host "[stop-dev] no marked source development session is running"
    exit 0
}

foreach ($devProcess in $DevProcesses) {
    $process = Get-Process -Id $devProcess.ProcessId -ErrorAction SilentlyContinue
    if (-not $process) {
        continue
    }

    Write-Host "[stop-dev] stopping marked source process PID $($process.Id)"
    $closeRequested = $process.CloseMainWindow()
    if ($closeRequested) {
        [void]$process.WaitForExit($GracefulTimeoutSeconds * 1000)
    }

    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction Stop
        if (-not $process.WaitForExit($GracefulTimeoutSeconds * 1000)) {
            throw "Marked source process PID $($process.Id) did not exit."
        }
    }
}

Write-Host "[stop-dev] marked source development session stopped"
