#requires -Version 5.1
<#
.SYNOPSIS
    Opens the current RC003 source checkout as a marked development session.

.DESCRIPTION
    The desktop shortcut points here instead of at a frozen EXE. Restarting
    this entry loads the current Python and QML sources without a PyInstaller
    rebuild. The marker is inherited by source child processes so the local
    candidate build can stop only this development session.
#>

$ErrorActionPreference = "Stop"
$RC003Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPythonw = Join-Path $RC003Root ".venv\Scripts\pythonw.exe"
$ProductName = -join @(
    [char]0x65E0,
    [char]0x7EBF,
    [char]0x9EA6
)
$DevLabel = -join @(
    [char]0x5F00,
    [char]0x53D1,
    [char]0x7248
)

function Show-LaunchError([string]$Message) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $Message,
        "$ProductName $DevLabel",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
}

try {
    if (-not (Test-Path -LiteralPath $VenvPythonw -PathType Leaf)) {
        throw "The local .venv is missing. Install the development requirements first."
    }

    $env:PYTHONPATH = Join-Path $RC003Root "src"
    $env:REMOTE_MIC_DEV_SESSION = "1"
    $StartArguments = @{
        FilePath = $VenvPythonw
        WorkingDirectory = $RC003Root
        ArgumentList = @(
            "-m",
            "ovb_rc003",
            "--settings",
            "--remote-mic-dev-session"
        )
    }
    Start-Process @StartArguments | Out-Null
} catch {
    Show-LaunchError $_.Exception.Message
    exit 1
}
