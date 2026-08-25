#requires -Version 5.1
<#
.SYNOPSIS
    Fetches the official Frida Gadget release asset and verifies its SHA-256
    before a complete frozen build. This is the only fetch path for Frida
    Gadget. build-candidate.ps1 and Windows CI require it so a distributable
    RC003 package cannot silently lose Back and volume-button support.

    No Frida binary is bundled in source control. Source-only execution may
    still omit the asset and degrade explicitly; frozen build entry points
    fail closed when it is missing or hash-mismatched. VB-CABLE has a separate
    hash-pinned fetch step. Neither script installs a driver or runs when the
    application starts.

.PARAMETER Destination
    Where to place the verified asset. Defaults to the ignored runtime asset
    directory under src/ovb_rc003 (not committed - see .gitignore).
#>

param(
    [string]$Destination = (Join-Path $PSScriptRoot "..\src\ovb_rc003\frida_assets\frida-gadget-17.15.3-windows-x86_64.dll.xz")
)

$ErrorActionPreference = "Stop"

$AssetName = "Frida Gadget 17.15.3 (Windows x86_64)"
$AssetUrl = "https://github.com/frida/frida/releases/download/17.15.3/frida-gadget-17.15.3-windows-x86_64.dll.xz"
$ExpectedSha256 = "B566D70189B6D551AD8F4E0BEA24DE08A3D4C0F559BB35B2BDB67D45182240C2"

function Get-Sha256Upper([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Get-VerifiedAsset {
    param(
        [string]$Name,
        [string]$Url,
        [string]$Destination,
        [string]$ExpectedSha256
    )

    $destinationDir = Split-Path -Parent $Destination
    if (-not (Test-Path $destinationDir)) {
        New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
    }

    if (Test-Path $Destination) {
        $existingHash = Get-Sha256Upper $Destination
        if ($existingHash -eq $ExpectedSha256.ToUpperInvariant()) {
            Write-Host "[fetch-frida-gadget] $Name already verified at $Destination"
            return
        }
        Write-Warning "[fetch-frida-gadget] existing file at $Destination has an unexpected hash; re-downloading"
    }

    $tempFile = "$Destination.download"
    try {
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            $curlArgs = @(
                "--fail", "--location", "--silent", "--show-error",
                "--retry", "5", "--retry-delay", "2",
                "--connect-timeout", "30", "--max-time", "600",
                "--output", $tempFile, $Url
            )
            if (Test-Path $tempFile) {
                $curlArgs = @("--continue-at", "-") + $curlArgs
            }
            & curl.exe @curlArgs
            if ($LASTEXITCODE -ne 0) {
                throw "curl.exe exited with code $LASTEXITCODE while fetching $Name"
            }
        } else {
            Invoke-WebRequest -Uri $Url -OutFile $tempFile -UseBasicParsing -TimeoutSec 600
        }

        $actualHash = Get-Sha256Upper $tempFile
        if ($actualHash -ne $ExpectedSha256.ToUpperInvariant()) {
            throw "SHA-256 mismatch for $Name`: expected $ExpectedSha256, got $actualHash. Refusing to use this file."
        }

        Move-Item -Force -LiteralPath $tempFile -Destination $Destination
        Write-Host "[fetch-frida-gadget] verified and saved $Name to $Destination"
    } finally {
        if (Test-Path $tempFile) {
            Remove-Item -Force -LiteralPath $tempFile -ErrorAction SilentlyContinue
        }
    }
}

Get-VerifiedAsset -Name $AssetName -Url $AssetUrl -Destination $Destination -ExpectedSha256 $ExpectedSha256

Write-Host ""
Write-Host "Frida Gadget license: see https://raw.githubusercontent.com/frida/frida-core/main/COPYING"
Write-Host "Complete frozen builds require this asset. RC003HidReportTap can inject into"
Write-Host "the paired RC003 WUDF host only when the bridge already has administrator"
Write-Host "rights; the application does not request UAC elevation for the tap. It recovers"
Write-Host "back and volume HID usages that Windows Raw Input drops."
