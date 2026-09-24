#requires -Version 5.1

param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputPath,
    [string]$BuildRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RC003Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot = (Resolve-Path (Join-Path $RC003Root "..\..\..")).Path
if ([string]::IsNullOrWhiteSpace($BuildRoot)) {
    $BuildRoot = Join-Path $RC003Root "dist\RemoteMicRC003"
} else {
    $BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
}
. (Join-Path $PSScriptRoot "build-provenance.ps1")
. (Join-Path $PSScriptRoot "portable-layout.ps1")

function Invoke-FrozenExecutableCheck {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,
        [Parameter(Mandatory = $true)]
        [string]$Step
    )

    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -Wait `
        -PassThru `
        -WindowStyle Hidden `
        -ErrorAction Stop
    if ($process.ExitCode -ne 0) {
        throw "$Step failed with exit code $($process.ExitCode)"
    }
}

$BuildGate = Enter-RC003BuildGate -RC003Root $RC003Root
try {
$sourceVersion = Get-RC003SourceVersion -RC003Root $RC003Root
$productPresentation = Get-RC003ProductPresentation -RC003Root $RC003Root
if ([string]$productPresentation.version -cne $sourceVersion) {
    throw "product presentation VERSION mismatch"
}
$builtPaths = Get-RC003BuildProductPaths -RC003Root $RC003Root -BuildRoot $BuildRoot
$builtVersionPath = $builtPaths.VersionFile
$builtVersion = (Get-Content -LiteralPath $builtVersionPath -Raw).Trim()
if ($builtVersion -ne $sourceVersion) {
    throw "built output is stale: source VERSION=$sourceVersion built VERSION=$builtVersion"
}

$mainExe = $builtPaths.MainExecutable
$hidHelper = $builtPaths.HidHelper
if (-not (Test-Path -LiteralPath $mainExe -PathType Leaf)) {
    throw "expected built executable not found: $mainExe"
}
if (-not (Test-Path -LiteralPath $hidHelper -PathType Leaf)) {
    throw "expected internal HID helper not found: $hidHelper"
}
$rootExecutables = @(Get-ChildItem -LiteralPath $BuildRoot -Force -Filter "*.exe" -File)
if (
    $rootExecutables.Count -ne 1 -or
    $rootExecutables[0].Name -cne [string]$productPresentation.main_executable_name
) {
    throw "build root must expose only $($productPresentation.main_executable_name)"
}

$outputFullPath = [System.IO.Path]::GetFullPath($OutputPath)
if ([System.IO.Path]::GetExtension($outputFullPath) -ne ".zip") {
    throw "OutputPath must end in .zip"
}
$buildRootFullPath = [System.IO.Path]::GetFullPath($BuildRoot).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
)
$buildRootPrefix = $buildRootFullPath + [System.IO.Path]::DirectorySeparatorChar
if (
    $outputFullPath.Equals(
        $buildRootFullPath,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -or
    $outputFullPath.StartsWith(
        $buildRootPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "OutputPath must be outside the build output directory"
}
$outputParent = [System.IO.Path]::GetDirectoryName($outputFullPath)
if (-not (Test-Path -LiteralPath $outputParent -PathType Container)) {
    throw "output directory does not exist: $outputParent"
}
if (Test-Path -LiteralPath $outputFullPath) {
    throw "refusing to overwrite existing test package: $outputFullPath"
}

$initialBuildProvenance = Assert-RC003BuildProvenance `
    -RC003Root $RC003Root `
    -RepoRoot $RepoRoot `
    -BuildRoot $BuildRoot

Write-Host "-- verify frozen HID helper --"
Invoke-FrozenExecutableCheck `
    -FilePath $hidHelper `
    -ArgumentList @("--self-check") `
    -Step "$hidHelper --self-check"

Write-Host "-- verify frozen application --"
Invoke-FrozenExecutableCheck `
    -FilePath $mainExe `
    -ArgumentList @("--dry-run") `
    -Step "$mainExe --dry-run"
Invoke-FrozenExecutableCheck `
    -FilePath $mainExe `
    -ArgumentList @("--qt-runtime-check") `
    -Step "$mainExe --qt-runtime-check"

$checkedBuildProvenance = Assert-RC003BuildProvenance `
    -RC003Root $RC003Root `
    -RepoRoot $RepoRoot `
    -BuildRoot $BuildRoot
Assert-RC003BuildProvenanceMatches `
    -Expected $initialBuildProvenance `
    -Actual $checkedBuildProvenance `
    -Context "pre-package verification"

$topLevelName = [string]$productPresentation.portable_folder_name
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "remote-mic-local-test-{0}" -f [guid]::NewGuid().ToString("N")
)
$stagingDir = Join-Path $temporaryRoot $topLevelName
$temporaryOutputPath = "$outputFullPath.$PID.$([guid]::NewGuid().ToString('N')).tmp.zip"

try {
    New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null
    Copy-Item -Path (Join-Path $BuildRoot "*") -Destination $stagingDir -Recurse -Force
    $stagedBuildProvenance = Assert-RC003BuildProvenance `
        -RC003Root $RC003Root `
        -RepoRoot $RepoRoot `
        -BuildRoot $stagingDir
    Assert-RC003BuildProvenanceMatches `
        -Expected $initialBuildProvenance `
        -Actual $stagedBuildProvenance `
        -Context "staged build verification"

    $documentationDir = Join-Path $stagingDir ([string]$productPresentation.documentation_directory_name)
    New-Item -ItemType Directory -Path $documentationDir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $RepoRoot "LICENSE.md") -Destination (Join-Path $documentationDir "LICENSE.txt") -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot "COPYRIGHT.md") -Destination (Join-Path $documentationDir "COPYRIGHT.txt") -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot "THIRD_PARTY_NOTICES.md") -Destination (Join-Path $documentationDir "THIRD_PARTY_NOTICES.md") -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot "THIRD_PARTY_SOURCE.md") -Destination (Join-Path $documentationDir "THIRD_PARTY_SOURCE.md") -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot "ASSET_LICENSES.md") -Destination (Join-Path $documentationDir "ASSET_LICENSES.md") -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot "THIRD_PARTY_LICENSES") -Destination (Join-Path $documentationDir "THIRD_PARTY_LICENSES") -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $RC003Root "ATTRIBUTION.md") -Destination (Join-Path $documentationDir "ATTRIBUTION.md") -Force
    Copy-Item -LiteralPath (Join-Path $RC003Root "installer\readme-portable-rc003.txt") -Destination (Join-Path $documentationDir ([string]$productPresentation.portable_readme_name)) -Force

    Assert-RC003PortableStagingRoot `
        -StagingDirectory $stagingDir `
        -ProductPresentation $productPresentation
    Compress-Archive -Path $stagingDir -DestinationPath $temporaryOutputPath -CompressionLevel Optimal -ErrorAction Stop
    Assert-RC003PortableZip `
        -ZipPath $temporaryOutputPath `
        -TopLevelName $topLevelName `
        -ExpectedVersion $sourceVersion `
        -StagingDirectory $stagingDir `
        -ProductPresentation $productPresentation
    $finalBuildProvenance = Assert-RC003BuildProvenance `
        -RC003Root $RC003Root `
        -RepoRoot $RepoRoot `
        -BuildRoot $BuildRoot
    Assert-RC003BuildProvenanceMatches `
        -Expected $initialBuildProvenance `
        -Actual $finalBuildProvenance `
        -Context "post-package verification"
    Move-Item `
        -LiteralPath $temporaryOutputPath `
        -Destination $outputFullPath `
        -ErrorAction Stop
} catch {
    Remove-Item -LiteralPath $temporaryOutputPath -Force -ErrorAction SilentlyContinue
    throw
} finally {
    Remove-Item -LiteralPath $temporaryOutputPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$zipHash = (Get-FileHash -LiteralPath $outputFullPath -Algorithm SHA256).Hash
$mainHash = (Get-FileHash -LiteralPath $mainExe -Algorithm SHA256).Hash
Write-Host "local test package: $outputFullPath"
Write-Host "ZIP SHA-256: $zipHash"
Write-Host "EXE SHA-256: $mainHash"
} finally {
    Exit-RC003BuildGate -Mutex $BuildGate
}
