#requires -Version 5.1
<#
.SYNOPSIS
    Creates or updates the current user's Remote Mic development shortcut.
#>

param(
    [string]$ShortcutPath
)

$ErrorActionPreference = "Stop"
$RC003Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunScript = (Resolve-Path (Join-Path $PSScriptRoot "run-dev.ps1")).Path

if (-not $ShortcutPath) {
    $Desktop = [Environment]::GetFolderPath("Desktop")
    if (-not $Desktop) {
        throw "Windows did not return a desktop folder for the current user."
    }
    $DevLabel = -join @(
        [char]0x5F00,
        [char]0x53D1,
        [char]0x7248
    )
    $ShortcutPath = Join-Path $Desktop ("Remote Mic " + $DevLabel + ".lnk")
}

$PowerShellExe = Join-Path $PSHOME "powershell.exe"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PowerShellExe
$Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$RunScript"""
$Shortcut.WorkingDirectory = $RC003Root
$Shortcut.Description = "Open the current Remote Mic source checkout"

$BuiltExe = Join-Path $RC003Root "dist\RemoteMicRC003\RemoteMicRC003.exe"
if (Test-Path -LiteralPath $BuiltExe -PathType Leaf) {
    $Shortcut.IconLocation = "$BuiltExe,0"
}

$Shortcut.Save()
Write-Host "Development shortcut created: $ShortcutPath"
