#requires -Version 5.1
<#
.SYNOPSIS
    Creates or updates the current user's product development shortcut.
#>

param(
    [string]$ShortcutPath
)

$ErrorActionPreference = "Stop"
$RC003Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunScript = (Resolve-Path (Join-Path $PSScriptRoot "run-dev.ps1")).Path
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
$UsesDefaultShortcutPath = -not $ShortcutPath
$Desktop = $null

if (-not $ShortcutPath) {
    $Desktop = [Environment]::GetFolderPath("Desktop")
    if (-not $Desktop) {
        throw "Windows did not return a desktop folder for the current user."
    }
    $ShortcutPath = Join-Path $Desktop ($ProductName + " " + $DevLabel + ".lnk")
}

$WindowsPowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (Test-Path -LiteralPath $WindowsPowerShellExe -PathType Leaf) {
    $PowerShellExe = $WindowsPowerShellExe
} else {
    $PowerShellExe = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    if (-not $PowerShellExe -or -not (Test-Path -LiteralPath $PowerShellExe -PathType Leaf)) {
        throw "A usable PowerShell executable could not be found."
    }
}
$ShortcutArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$RunScript"""
$Shell = New-Object -ComObject WScript.Shell

if ($UsesDefaultShortcutPath) {
    $LegacyShortcutPath = Join-Path $Desktop ("Remote Mic " + $DevLabel + ".lnk")
    if (Test-Path -LiteralPath $LegacyShortcutPath -PathType Leaf) {
        try {
            $LegacyShortcut = $Shell.CreateShortcut($LegacyShortcutPath)
            $LegacyMatchesThisCheckout = (
                [string]::Equals(
                    [System.IO.Path]::GetFullPath($LegacyShortcut.TargetPath),
                    [System.IO.Path]::GetFullPath($PowerShellExe),
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -and
                [string]::Equals(
                    [System.IO.Path]::GetFullPath($LegacyShortcut.WorkingDirectory),
                    [System.IO.Path]::GetFullPath($RC003Root),
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -and
                $LegacyShortcut.Arguments -eq $ShortcutArguments
            )
            if ($LegacyMatchesThisCheckout) {
                Remove-Item -LiteralPath $LegacyShortcutPath -Force
            }
        } catch {
            Write-Warning "The previous development shortcut could not be inspected; it was left unchanged."
        }
    }
}

$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PowerShellExe
$Shortcut.Arguments = $ShortcutArguments
$Shortcut.WorkingDirectory = $RC003Root
$Shortcut.Description = "Open the current $ProductName source checkout"

$AppIcon = Join-Path $RC003Root "src\ovb_rc003\assets\icons\remote-mic.ico"
if (Test-Path -LiteralPath $AppIcon -PathType Leaf) {
    $Shortcut.IconLocation = "$AppIcon,0"
}

$Shortcut.Save()
Write-Host "Development shortcut created: $ShortcutPath"
