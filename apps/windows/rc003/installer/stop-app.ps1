#requires -Version 5.1
<#
.SYNOPSIS
    Safely stops the installed RemoteMicRC003.exe before upgrade, uninstall,
    or an explicit user-requested stop.

.DESCRIPTION
    Current builds receive an atomic full-exit request and own all cleanup.
    Released legacy builds have no application-exit contract, so their bridge
    is asked to exit through its existing tray control window before the
    remaining settings shell is boundedly removed. The script never touches a
    same-named executable outside the exact installation path.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$AppPath,

    [string]$ConfigRoot = "",

    [switch]$ElevatedRetry
)

$ErrorActionPreference = "Stop"

$exitOk = 0
$exitNeedsElevation = 10
$exitUnsafeToContinue = 20
$exitProbeFailed = 21
$currentExitTimeoutSeconds = 45
$legacyRequestGraceSeconds = 2
$legacyBridgeExitTimeoutSeconds = 10
$legacyShellExitTimeoutSeconds = 5
$targetExecutableName = "RemoteMicRC003.exe"
$exitRequestFileName = "application-exit-request.json"
$bridgeStartRequestFileName = "bridge-start-request.json"
$exitCapabilityFileName = "application-exit-contract-v1.json"
$legacyBridgeWindowTitle = "Remote Mic RC003 bridge tray"
$legacyExitCommand = 1002
$wmCommand = 0x0111
$errorAccessDenied = 5
$productName = -join @(
    [char]0x65E0,
    [char]0x7EBF,
    [char]0x9EA6
)

function Write-StopError {
    param([Parameter(Mandatory = $true)][string]$Message)
    [Console]::Error.WriteLine($Message)
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

$isElevated = Test-IsAdministrator
if ($ElevatedRetry -and -not $isElevated) {
    Write-StopError "$productName elevated stop retry did not receive administrator rights."
    exit $exitProbeFailed
}

$normalizedAppPath = Resolve-Path -LiteralPath $AppPath -ErrorAction SilentlyContinue
if (-not $normalizedAppPath) {
    exit $exitOk
}

$root = [System.IO.Path]::GetFullPath($normalizedAppPath.Path).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
)
$targetExecutablePath = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::Combine($root, $targetExecutableName)
)
$capabilityPath = [System.IO.Path]::Combine($root, $exitCapabilityFileName)

if ([string]::IsNullOrWhiteSpace($ConfigRoot)) {
    $localAppData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::LocalApplicationData
    )
    $ConfigRoot = [System.IO.Path]::Combine($localAppData, "RemoteMic", "RC003")
}
$configRootPath = [System.IO.Path]::GetFullPath($ConfigRoot)
$exitRequestPath = [System.IO.Path]::Combine(
    $configRootPath,
    $exitRequestFileName
)
$bridgeStartRequestPath = [System.IO.Path]::Combine(
    $configRootPath,
    $bridgeStartRequestFileName
)

function Exit-ForRestrictedProcess {
    if (-not $script:isElevated) {
        exit $script:exitNeedsElevation
    }
    Write-StopError "$productName process identity could not be verified."
    exit $script:exitProbeFailed
}

function Get-TargetSnapshot {
    $targets = [System.Collections.Generic.List[object]]::new()
    $unknownCount = 0
    try {
        $processes = @(
            Get-CimInstance Win32_Process `
                -Filter "Name = '$targetExecutableName'" `
                -ErrorAction Stop
        )
    } catch {
        Write-StopError "$productName process list could not be read."
        exit $script:exitProbeFailed
    }

    foreach ($process in $processes) {
        if (-not $process.ExecutablePath -or -not $process.CreationDate) {
            $unknownCount += 1
            continue
        }
        try {
            $executable = [System.IO.Path]::GetFullPath($process.ExecutablePath)
        } catch {
            $unknownCount += 1
            continue
        }
        if (-not $executable.Equals(
            $script:targetExecutablePath,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            continue
        }
        $targets.Add([PSCustomObject]@{
            ProcessId = [uint32]$process.ProcessId
            CreationDate = $process.CreationDate
            CommandLine = [string]$process.CommandLine
        })
    }

    return [PSCustomObject]@{
        Targets = @($targets)
        UnknownCount = $unknownCount
    }
}

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
        throw "target process path is unavailable"
    }
    $executable = [System.IO.Path]::GetFullPath($current.ExecutablePath)
    if (-not $executable.Equals(
        $script:targetExecutablePath,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return $null
    }
    return $current
}

function Wait-ForTargetsToExit {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Targets,

        [Parameter(Mandatory = $true)]
        [double]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $remaining = @()
        foreach ($target in $Targets) {
            try {
                $current = Get-CurrentTargetProcess `
                    -ProcessId $target.ProcessId `
                    -CreationDate $target.CreationDate
            } catch {
                Exit-ForRestrictedProcess
            }
            if ($current) {
                $remaining += $target
            }
        }
        if ($remaining.Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Remove-TransientRequests {
    try {
        Remove-Item -LiteralPath $script:exitRequestPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $script:bridgeStartRequestPath -Force -ErrorAction SilentlyContinue
    } catch {
        # A consumed request is already gone; transient cleanup is best-effort only.
    }
}

function Write-ExitRequest {
    [System.IO.Directory]::CreateDirectory($script:configRootPath) | Out-Null
    $temporary = [System.IO.Path]::Combine(
        $script:configRootPath,
        ".$exitRequestFileName.$([Guid]::NewGuid().ToString('N')).tmp"
    )
    $payload = [ordered]@{
        action = "exit_application"
        created_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        schema = 1
    } | ConvertTo-Json -Compress
    try {
        [System.IO.File]::WriteAllText(
            $temporary,
            $payload,
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $script:exitRequestPath -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

$snapshot = Get-TargetSnapshot
if ($snapshot.UnknownCount -gt 0) {
    Exit-ForRestrictedProcess
}
if ($snapshot.Targets.Count -eq 0) {
    Remove-TransientRequests
    exit $exitOk
}

try {
    Write-ExitRequest
} catch {
    Write-StopError "$productName full-exit request could not be written."
    exit $exitUnsafeToContinue
}

$supportsFullExit = Test-Path -LiteralPath $capabilityPath -PathType Leaf
$initialExitTimeoutSeconds = $legacyRequestGraceSeconds
if ($supportsFullExit) {
    $initialExitTimeoutSeconds = $currentExitTimeoutSeconds
}
if (Wait-ForTargetsToExit `
    -Targets $snapshot.Targets `
    -TimeoutSeconds $initialExitTimeoutSeconds
) {
    $completedSnapshot = Get-TargetSnapshot
    if ($completedSnapshot.UnknownCount -gt 0) {
        Exit-ForRestrictedProcess
    }
    if ($completedSnapshot.Targets.Count -eq 0) {
        Remove-TransientRequests
        exit $exitOk
    }
    Write-StopError "$productName restarted while shutdown was being confirmed."
    exit $exitUnsafeToContinue
}

$remainingSnapshot = Get-TargetSnapshot
if ($remainingSnapshot.UnknownCount -gt 0) {
    Exit-ForRestrictedProcess
}
if ($remainingSnapshot.Targets.Count -eq 0) {
    Remove-TransientRequests
    exit $exitOk
}

# A missing marker identifies every published legacy installer. For safety,
# also recognize an unpublished/request-capable build by observing that it
# claimed the request file, then give that build the same full cleanup bound.
if (-not $supportsFullExit -and -not (Test-Path -LiteralPath $exitRequestPath)) {
    $supportsFullExit = $true
    if (Wait-ForTargetsToExit `
        -Targets $remainingSnapshot.Targets `
        -TimeoutSeconds $currentExitTimeoutSeconds
    ) {
        $completedSnapshot = Get-TargetSnapshot
        if ($completedSnapshot.UnknownCount -gt 0) {
            Exit-ForRestrictedProcess
        }
        if ($completedSnapshot.Targets.Count -eq 0) {
            Remove-TransientRequests
            exit $exitOk
        }
        Write-StopError "$productName restarted while shutdown was being confirmed."
        exit $exitUnsafeToContinue
    }
}

# The marker is installed only with builds that support the request above.
# Never force such a build after its own bounded cleanup refused or timed out.
if ($supportsFullExit) {
    Write-StopError "$productName did not complete its normal exit; files were not touched."
    exit $exitUnsafeToContinue
}

$unknownRoles = @(
    $remainingSnapshot.Targets | Where-Object {
        [string]::IsNullOrWhiteSpace($_.CommandLine)
    }
)
if ($unknownRoles.Count -gt 0) {
    Exit-ForRestrictedProcess
}

$legacyBridgeTargets = @(
    $remainingSnapshot.Targets | Where-Object {
        $_.CommandLine -match '(?i)(^|\s|\")--bridge(\s|$|\")'
    }
)

if ($legacyBridgeTargets.Count -gt 0) {
    $nativeType = [System.Management.Automation.PSTypeName]'RemoteMicInstaller.NativeMethods'
    if (-not $nativeType.Type) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

namespace RemoteMicInstaller {
    public static class NativeMethods {
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr FindWindow(string className, string windowName);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool PostMessage(
            IntPtr window,
            uint message,
            UIntPtr wParam,
            IntPtr lParam
        );

        [DllImport("user32.dll", SetLastError = true)]
        public static extern uint GetWindowThreadProcessId(
            IntPtr window,
            out uint processId
        );
    }
}
"@
    }

    $postDeadline = [DateTime]::UtcNow.AddSeconds(2)
    $posted = $false
    $matchingWindowFound = $false
    $postError = 0
    $legacyBridgeProcessIds = @(
        $legacyBridgeTargets | ForEach-Object { [uint32]$_.ProcessId }
    )
    do {
        $window = [RemoteMicInstaller.NativeMethods]::FindWindow(
            $null,
            $legacyBridgeWindowTitle
        )
        if ($window -ne [IntPtr]::Zero) {
            $windowProcessId = [uint32]0
            [void][RemoteMicInstaller.NativeMethods]::GetWindowThreadProcessId(
                $window,
                [ref]$windowProcessId
            )
            if ($legacyBridgeProcessIds -contains $windowProcessId) {
                $matchingWindowFound = $true
                $posted = [RemoteMicInstaller.NativeMethods]::PostMessage(
                    $window,
                    [uint32]$wmCommand,
                    [UIntPtr][uint32]$legacyExitCommand,
                    [IntPtr]::Zero
                )
                if ($posted) {
                    break
                }
                $postError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            }
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $postDeadline)

    if (-not $posted) {
        if (
            $matchingWindowFound -and
            $postError -eq $errorAccessDenied -and
            -not $isElevated
        ) {
            exit $exitNeedsElevation
        }
        if (-not $matchingWindowFound) {
            Write-StopError "$productName legacy bridge control window was not found."
        } else {
            Write-StopError "$productName legacy bridge could not receive its exit command."
        }
        exit $exitUnsafeToContinue
    }

    if (-not (Wait-ForTargetsToExit `
        -Targets $legacyBridgeTargets `
        -TimeoutSeconds $legacyBridgeExitTimeoutSeconds
    )) {
        Write-StopError "$productName legacy bridge did not finish normal cleanup."
        exit $exitUnsafeToContinue
    }
}

# Candidate releases before the full-exit contract kept the settings shell
# separate from the bridge. Once every legacy bridge process has completed
# normal cleanup, the shell owns no BLE/HID/key/audio resources and can be
# boundedly removed so its executable can be replaced.
$shellSnapshot = Get-TargetSnapshot
if ($shellSnapshot.UnknownCount -gt 0) {
    Exit-ForRestrictedProcess
}
$restartedLegacyBridgeTargets = @(
    $shellSnapshot.Targets | Where-Object {
        $_.CommandLine -match '(?i)(^|\s|\")--bridge(\s|$|\")'
    }
)
if ($restartedLegacyBridgeTargets.Count -gt 0) {
    Write-StopError "$productName legacy bridge restarted during shutdown confirmation."
    exit $exitUnsafeToContinue
}
foreach ($target in $shellSnapshot.Targets) {
    try {
        $current = Get-CurrentTargetProcess `
            -ProcessId $target.ProcessId `
            -CreationDate $target.CreationDate
        if (-not $current) {
            continue
        }
        Stop-Process -Id $target.ProcessId -Force -ErrorAction Stop
    } catch {
        try {
            $stillCurrent = Get-CurrentTargetProcess `
                -ProcessId $target.ProcessId `
                -CreationDate $target.CreationDate
        } catch {
            Exit-ForRestrictedProcess
        }
        if ($stillCurrent) {
            if (-not $isElevated) {
                exit $exitNeedsElevation
            }
            Write-StopError "$productName legacy shell could not be stopped."
            exit $exitUnsafeToContinue
        }
    }
}

if (-not (Wait-ForTargetsToExit `
    -Targets $shellSnapshot.Targets `
    -TimeoutSeconds $legacyShellExitTimeoutSeconds
)) {
    Write-StopError "$productName legacy shell did not exit within the bounded timeout."
    exit $exitUnsafeToContinue
}

$finalSnapshot = Get-TargetSnapshot
if ($finalSnapshot.UnknownCount -gt 0) {
    Exit-ForRestrictedProcess
}
if ($finalSnapshot.Targets.Count -ne 0) {
    Write-StopError "$productName is still running; files were not touched."
    exit $exitUnsafeToContinue
}

Remove-TransientRequests
exit $exitOk
