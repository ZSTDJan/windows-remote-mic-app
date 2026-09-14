#requires -Version 5.1

param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RC003Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot = (Resolve-Path (Join-Path $RC003Root "..\..\..")).Path
$BuildRoot = Join-Path $RC003Root "dist\RemoteMicRC003"
. (Join-Path $PSScriptRoot "build-provenance.ps1")

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

function Get-StreamSha256 {
    param([Parameter(Mandatory = $true)][System.IO.Stream]$Stream)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha256.ComputeHash($Stream)
    } finally {
        $sha256.Dispose()
    }
    return [System.BitConverter]::ToString($bytes).Replace("-", "")
}

function Assert-PortableZip {
    param(
        [string]$ZipPath,
        [string]$TopLevelName,
        [string]$ExpectedVersion,
        [string]$StagingDirectory
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $archiveFiles = @{}
        foreach ($entry in $archive.Entries) {
            $entryName = $entry.FullName.Replace(
                [System.IO.Path]::DirectorySeparatorChar,
                [char]'/'
            )
            if (-not $entryName -or $entryName.EndsWith('/')) {
                continue
            }
            if ($archiveFiles.ContainsKey($entryName)) {
                throw "portable ZIP contains a duplicate file entry: $entryName"
            }
            $archiveFiles[$entryName] = $entry
        }
        $entryNames = @($archiveFiles.Keys)
        if ($entryNames.Count -eq 0) {
            throw "portable ZIP is empty"
        }
        $topLevels = @(
            $entryNames |
                ForEach-Object { $_.Split('/')[0] } |
                Sort-Object -Unique
        )
        if ($topLevels.Count -ne 1 -or $topLevels[0] -ne $TopLevelName) {
            throw "portable ZIP must contain exactly one top-level directory named $TopLevelName"
        }

        $rootExecutables = @(
            $entryNames | Where-Object {
                $_ -match ('^' + [regex]::Escape($TopLevelName) + '/[^/]+\.exe$')
            }
        )
        $expectedMainEntry = "$TopLevelName/RemoteMicRC003.exe"
        if ($rootExecutables.Count -ne 1 -or $rootExecutables[0] -ne $expectedMainEntry) {
            throw "portable ZIP root must expose only $expectedMainEntry"
        }

        $helperEntryName = "$TopLevelName/_internal/RemoteMicRC003HidHelper.exe"
        if (-not $archiveFiles.ContainsKey($helperEntryName)) {
            throw "portable ZIP is missing the internal HID helper"
        }
        $versionEntryName = "$TopLevelName/_internal/ovb_rc003/VERSION"
        if (-not $archiveFiles.ContainsKey($versionEntryName)) {
            throw "portable ZIP is missing the frozen VERSION file"
        }
        $provenanceEntryName = "$TopLevelName/_internal/build-provenance.json"
        if (-not $archiveFiles.ContainsKey($provenanceEntryName)) {
            throw "portable ZIP is missing the build provenance file"
        }
        $versionEntry = $archiveFiles[$versionEntryName]
        $reader = [System.IO.StreamReader]::new($versionEntry.Open())
        try {
            $zipVersion = $reader.ReadToEnd().Trim()
        } finally {
            $reader.Dispose()
        }
        if ($zipVersion -ne $ExpectedVersion) {
            throw "portable ZIP VERSION mismatch: source=$ExpectedVersion zip=$zipVersion"
        }

        $pathSeparators = [char[]]@(
            [System.IO.Path]::DirectorySeparatorChar,
            [char]'/'
        )
        $stagingRoot = (Resolve-Path -LiteralPath $StagingDirectory).Path.TrimEnd(
            $pathSeparators
        )
        $stagingFiles = @(
            Get-ChildItem -LiteralPath $stagingRoot -Force -File -Recurse | ForEach-Object {
                $relativePath = $_.FullName.Substring($stagingRoot.Length).TrimStart(
                    $pathSeparators
                ).Replace([System.IO.Path]::DirectorySeparatorChar, [char]'/')
                [pscustomobject]@{
                    EntryName = "$TopLevelName/$relativePath"
                    FullName = $_.FullName
                    Length = $_.Length
                }
            }
        )
        if ($archiveFiles.Count -ne $stagingFiles.Count) {
            throw "portable ZIP file count mismatch: staging=$($stagingFiles.Count) zip=$($archiveFiles.Count)"
        }
        foreach ($stagingFile in $stagingFiles) {
            if (-not $archiveFiles.ContainsKey($stagingFile.EntryName)) {
                throw "portable ZIP is missing staging file: $($stagingFile.EntryName)"
            }
            $entry = $archiveFiles[$stagingFile.EntryName]
            if ($entry.Length -ne $stagingFile.Length) {
                throw "portable ZIP file length mismatch: $($stagingFile.EntryName)"
            }
            $stagingHash = (Get-FileHash -LiteralPath $stagingFile.FullName -Algorithm SHA256).Hash
            $stream = $entry.Open()
            try {
                $zipHash = Get-StreamSha256 -Stream $stream
            } finally {
                $stream.Dispose()
            }
            if ($zipHash -ne $stagingHash) {
                throw "portable ZIP file hash mismatch: $($stagingFile.EntryName)"
            }
        }
    } finally {
        $archive.Dispose()
    }
}

$BuildGate = Enter-RC003BuildGate -RC003Root $RC003Root
try {
$sourceVersion = Get-RC003SourceVersion -RC003Root $RC003Root
$builtVersionPath = Join-Path $BuildRoot "_internal\ovb_rc003\VERSION"
$builtVersion = (Get-Content -LiteralPath $builtVersionPath -Raw).Trim()
if ($builtVersion -ne $sourceVersion) {
    throw "built output is stale: source VERSION=$sourceVersion built VERSION=$builtVersion"
}

$mainExe = Join-Path $BuildRoot "RemoteMicRC003.exe"
$hidHelper = Join-Path $BuildRoot "_internal\RemoteMicRC003HidHelper.exe"
if (-not (Test-Path -LiteralPath $mainExe -PathType Leaf)) {
    throw "expected built executable not found: $mainExe"
}
if (-not (Test-Path -LiteralPath $hidHelper -PathType Leaf)) {
    throw "expected internal HID helper not found: $hidHelper"
}
$rootExecutables = @(Get-ChildItem -LiteralPath $BuildRoot -Force -Filter "*.exe" -File)
if ($rootExecutables.Count -ne 1 -or $rootExecutables[0].Name -ne "RemoteMicRC003.exe") {
    throw "build root must expose only RemoteMicRC003.exe"
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

$topLevelName = "RemoteMicRC003-$sourceVersion"
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

    Copy-Item -LiteralPath (Join-Path $RepoRoot "LICENSE.md") -Destination (Join-Path $stagingDir "LICENSE.txt") -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot "COPYRIGHT.md") -Destination (Join-Path $stagingDir "COPYRIGHT.txt") -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot "THIRD_PARTY_NOTICES.md") -Destination (Join-Path $stagingDir "THIRD_PARTY_NOTICES.md") -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot "THIRD_PARTY_SOURCE.md") -Destination (Join-Path $stagingDir "THIRD_PARTY_SOURCE.md") -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot "ASSET_LICENSES.md") -Destination (Join-Path $stagingDir "ASSET_LICENSES.md") -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot "THIRD_PARTY_LICENSES") -Destination (Join-Path $stagingDir "THIRD_PARTY_LICENSES") -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $RC003Root "ATTRIBUTION.md") -Destination (Join-Path $stagingDir "ATTRIBUTION.md") -Force
    Copy-Item -LiteralPath (Join-Path $RC003Root "installer\readme-portable-rc003.txt") -Destination (Join-Path $stagingDir "README.txt") -Force

    Compress-Archive -Path $stagingDir -DestinationPath $temporaryOutputPath -CompressionLevel Optimal -ErrorAction Stop
    Assert-PortableZip -ZipPath $temporaryOutputPath -TopLevelName $topLevelName -ExpectedVersion $sourceVersion -StagingDirectory $stagingDir
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
