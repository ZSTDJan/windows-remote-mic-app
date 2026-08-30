#requires -Version 5.1
<#
.SYNOPSIS
    Force-stops any running RemoteMicRC003.exe under the given install
    path, so the installer/uninstaller can safely replace or remove files.
    Generic and product-path-driven; requests no elevation itself.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$AppPath
)

$ErrorActionPreference = "Stop"

$normalizedAppPath = Resolve-Path -LiteralPath $AppPath -ErrorAction SilentlyContinue
if (-not $normalizedAppPath) {
    exit 0
}

$root = [System.IO.Path]::GetFullPath($normalizedAppPath.Path).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
)
$rootPrefix = $root + [System.IO.Path]::DirectorySeparatorChar
$targetExecutableName = "RemoteMicRC003.exe"
$productName = -join @(
    [char]0x65E0,
    [char]0x7EBF,
    [char]0x9EA6
)

$targetProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            if (-not $_.ExecutablePath) {
                return $false
            }
            try {
                $executable = [System.IO.Path]::GetFullPath($_.ExecutablePath)
            } catch {
                return $false
            }
            return (
                [System.IO.Path]::GetFileName($executable).Equals(
                    $targetExecutableName,
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -and $executable.StartsWith(
                $rootPrefix,
                [System.StringComparison]::OrdinalIgnoreCase
                )
            )
        } |
        ForEach-Object {
            [PSCustomObject]@{
                ProcessId = [uint32]$_.ProcessId
                CreationDate = $_.CreationDate
            }
        }
)

function Get-CurrentTargetProcess {
    param(
        [Parameter(Mandatory = $true)]
        [uint32]$ProcessId,

        [Parameter(Mandatory = $true)]
        $CreationDate
    )

    $current = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $ProcessId" `
        -ErrorAction SilentlyContinue
    if (-not $current -or $current.CreationDate -ne $CreationDate) {
        return $null
    }
    if (-not $current.ExecutablePath) {
        return $null
    }
    try {
        $executable = [System.IO.Path]::GetFullPath($current.ExecutablePath)
    } catch {
        return $null
    }
    if (
        -not [System.IO.Path]::GetFileName($executable).Equals(
            $targetExecutableName,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or -not $executable.StartsWith(
            $rootPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $null
    }
    return $current
}

try {
    foreach ($target in $targetProcesses) {
        $current = Get-CurrentTargetProcess `
            -ProcessId $target.ProcessId `
            -CreationDate $target.CreationDate
        if (-not $current) {
            continue
        }
        try {
            Stop-Process -Id $target.ProcessId -Force -ErrorAction Stop
        } catch {
            $stillCurrent = Get-CurrentTargetProcess `
                -ProcessId $target.ProcessId `
                -CreationDate $target.CreationDate
            if ($stillCurrent) {
                throw
            }
        }
    }
} catch {
    Write-Error "$productName process could not be stopped."
    exit 1
}

$deadline = [DateTime]::UtcNow.AddSeconds(5)
do {
    $remaining = @(
        $targetProcesses | Where-Object {
            Get-CurrentTargetProcess `
                -ProcessId $_.ProcessId `
                -CreationDate $_.CreationDate
        }
    )
    if ($remaining.Count -eq 0) {
        exit 0
    }
    Start-Sleep -Milliseconds 100
} while ([DateTime]::UtcNow -lt $deadline)

Write-Error "$productName process did not exit within the bounded timeout."
exit 2
