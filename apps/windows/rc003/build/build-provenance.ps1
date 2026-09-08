#requires -Version 5.1

$script:RC003_BUILD_PROVENANCE_SCHEMA_VERSION = 1
$script:RC003_BUILD_PROVENANCE_RELATIVE_PATH = "_internal/build-provenance.json"

function Get-RC003BuildGateName {
    param([Parameter(Mandatory = $true)][string]$RC003Root)

    $normalized = [System.IO.Path]::GetFullPath($RC003Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ).ToUpperInvariant()
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($normalized)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = [System.BitConverter]::ToString(
            $sha256.ComputeHash($bytes)
        ).Replace("-", "")
    } finally {
        $sha256.Dispose()
    }
    return "Global\RemoteMicRC003_BuildGate_$($digest.Substring(0, 32))"
}

function Enter-RC003BuildGate {
    param([Parameter(Mandatory = $true)][string]$RC003Root)

    $mutex = [System.Threading.Mutex]::new(
        $false,
        (Get-RC003BuildGateName -RC003Root $RC003Root)
    )
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne(0)
        } catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if (-not $acquired) {
            throw "another RC003 build or package operation is already running"
        }
        return $mutex
    } catch {
        $mutex.Dispose()
        throw
    }
}

function Exit-RC003BuildGate {
    param([Parameter(Mandatory = $true)][System.Threading.Mutex]$Mutex)

    try {
        $Mutex.ReleaseMutex()
    } finally {
        $Mutex.Dispose()
    }
}

function Get-RC003FileState {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $length = $stream.Length
        $hash = [System.BitConverter]::ToString(
            $sha256.ComputeHash($stream)
        ).Replace("-", "")
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
    return [pscustomobject]@{
        Length = $length
        Sha256 = $hash
    }
}

function Get-RC003NormalizedRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$FilePath
    )

    $base = [System.IO.Path]::GetFullPath($BasePath).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $file = [System.IO.Path]::GetFullPath($FilePath)
    $prefix = $base + [System.IO.Path]::DirectorySeparatorChar
    if (-not $file.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "build input is outside the fingerprint root: $file"
    }
    return $file.Substring($prefix.Length).Replace(
        [System.IO.Path]::DirectorySeparatorChar,
        [char]'/'
    )
}

function Get-RC003ContentState {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][System.IO.FileInfo[]]$Files
    )

    $records = @(
        foreach ($file in $Files) {
            if (-not $file.Exists) {
                throw "fingerprinted file is missing: $($file.FullName)"
            }
            $relative = Get-RC003NormalizedRelativePath `
                -BasePath $BasePath `
                -FilePath $file.FullName
            $fileState = Get-RC003FileState -Path $file.FullName
            "$relative`0$($fileState.Length)`0$($fileState.Sha256)"
        }
    )
    [System.Array]::Sort($records, [System.StringComparer]::Ordinal)
    $payload = [System.Text.UTF8Encoding]::new($false).GetBytes(
        ($records -join "`n")
    )
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash($payload)
    } finally {
        $sha256.Dispose()
    }
    return [pscustomobject]@{
        FingerprintSha256 = [System.BitConverter]::ToString($digest).Replace("-", "")
        FileCount = $records.Count
    }
}

function Get-RC003BuildInputFiles {
    param(
        [Parameter(Mandatory = $true)][string]$RC003Root,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    $files = [System.Collections.Generic.List[System.IO.FileInfo]]::new()
    $sourceRoot = Join-Path $RC003Root "src"
    if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container -ErrorAction Stop)) {
        throw "required source directory not found: $sourceRoot"
    }
    foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -Force -File -Recurse -ErrorAction Stop) {
        if ($file.Extension -eq ".pyc") {
            continue
        }
        if ($file.FullName -match '[\\/]__pycache__[\\/]') {
            continue
        }
        $files.Add($file)
    }

    $testsRoot = Join-Path $RC003Root "tests"
    if (-not (Test-Path -LiteralPath $testsRoot -PathType Container -ErrorAction Stop)) {
        throw "required test directory not found: $testsRoot"
    }
    foreach ($file in Get-ChildItem -LiteralPath $testsRoot -Force -File -Recurse -ErrorAction Stop) {
        if ($file.Extension -eq ".pyc") {
            continue
        }
        if ($file.FullName -match '[\\/]__pycache__[\\/]') {
            continue
        }
        $files.Add($file)
    }

    $requiredFiles = @(
        (Join-Path $RC003Root "build\build-candidate.ps1"),
        (Join-Path $RC003Root "build\package-local-test.ps1"),
        (Join-Path $RC003Root "build\build-provenance.ps1"),
        (Join-Path $RC003Root "build\check-public-boundary.ps1"),
        (Join-Path $RC003Root "build\check-release-readiness.py"),
        (Join-Path $RC003Root "build\check-third-party-notices.py"),
        (Join-Path $RC003Root "build\prepare-cython-core.py"),
        (Join-Path $RC003Root "build\fetch-frida-gadget.ps1"),
        (Join-Path $RC003Root "build\fetch-vb-cable.ps1"),
        (Join-Path $RC003Root "build\stop-dev.ps1"),
        (Join-Path $RC003Root "build\RemoteMicRC003.spec"),
        (Join-Path $RC003Root "build\generate-app-icon.py"),
        (Join-Path $RC003Root "build\third_party\VBCABLE_Driver_Pack45.zip"),
        (Join-Path $RC003Root "requirements.txt"),
        (Join-Path $RC003Root "requirements-dev.txt"),
        (Join-Path $RC003Root "pyproject.toml"),
        (Join-Path $RC003Root "scripts\element_navigation_prototype.py"),
        (Join-Path $RC003Root "scripts\element_navigation_command_windows.py"),
        (Join-Path $RC003Root "scripts\element_navigation_support.py"),
        (Join-Path $RC003Root "scripts\element_navigation_windows_host.py"),
        (Join-Path $RC003Root "scripts\element_targeting_core.py"),
        (Join-Path $RC003Root "scripts\spatial_navigation_core.py"),
        (Join-Path $RC003Root "ATTRIBUTION.md"),
        (Join-Path $RC003Root "README.md"),
        (Join-Path $RepoRoot ".github\workflows\windows-rc003-ci.yml"),
        (Join-Path $RepoRoot "README.md"),
        (Join-Path $RepoRoot "Resources\RC003-remote-photo.png"),
        (Join-Path $RepoRoot "ASSET_LICENSES.md"),
        (Join-Path $RepoRoot "COPYRIGHT.md"),
        (Join-Path $RepoRoot "LICENSE.md"),
        (Join-Path $RepoRoot "THIRD_PARTY_NOTICES.md"),
        (Join-Path $RepoRoot "THIRD_PARTY_SOURCE.md")
    )
    foreach ($path in $requiredFiles) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf -ErrorAction Stop)) {
            throw "required build input not found: $path"
        }
        $files.Add((Get-Item -LiteralPath $path -ErrorAction Stop))
    }

    $profilesRoot = Join-Path $RepoRoot "device-profiles"
    if (-not (Test-Path -LiteralPath $profilesRoot -PathType Container -ErrorAction Stop)) {
        throw "required device profile directory not found: $profilesRoot"
    }
    foreach ($file in Get-ChildItem -LiteralPath $profilesRoot -Force -File -Recurse -ErrorAction Stop) {
        $files.Add($file)
    }

    $installerRoot = Join-Path $RC003Root "installer"
    if (-not (Test-Path -LiteralPath $installerRoot -PathType Container -ErrorAction Stop)) {
        throw "required installer directory not found: $installerRoot"
    }
    foreach ($file in Get-ChildItem -LiteralPath $installerRoot -Force -File -Recurse -ErrorAction Stop) {
        $files.Add($file)
    }

    $thirdPartyLicensesRoot = Join-Path $RepoRoot "THIRD_PARTY_LICENSES"
    if (-not (Test-Path -LiteralPath $thirdPartyLicensesRoot -PathType Container -ErrorAction Stop)) {
        throw "required third-party license directory not found: $thirdPartyLicensesRoot"
    }
    foreach ($file in Get-ChildItem -LiteralPath $thirdPartyLicensesRoot -Force -File -Recurse -ErrorAction Stop) {
        $files.Add($file)
    }

    $unique = [System.Collections.Generic.Dictionary[string, System.IO.FileInfo]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($file in $files) {
        $unique[[System.IO.Path]::GetFullPath($file.FullName)] = $file
    }
    return @($unique.Values)
}

function Get-RC003BuildInputState {
    param(
        [Parameter(Mandatory = $true)][string]$RC003Root,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    $files = Get-RC003BuildInputFiles -RC003Root $RC003Root -RepoRoot $RepoRoot
    return Get-RC003ContentState -BasePath $RepoRoot -Files $files
}

function Get-RC003ArtifactState {
    param([Parameter(Mandatory = $true)][string]$BuildRoot)

    if (-not (Test-Path -LiteralPath $BuildRoot -PathType Container -ErrorAction Stop)) {
        throw "expected build root not found: $BuildRoot"
    }
    $files = @(
        Get-ChildItem -LiteralPath $BuildRoot -Force -File -Recurse -ErrorAction Stop |
            Where-Object {
                (Get-RC003NormalizedRelativePath `
                    -BasePath $BuildRoot `
                    -FilePath $_.FullName) -ne $script:RC003_BUILD_PROVENANCE_RELATIVE_PATH
            }
    )
    return Get-RC003ContentState -BasePath $BuildRoot -Files $files
}

function Get-RC003BuildProvenancePath {
    param([Parameter(Mandatory = $true)][string]$BuildRoot)

    return Join-Path $BuildRoot (
        $script:RC003_BUILD_PROVENANCE_RELATIVE_PATH.Replace('/', '\')
    )
}

function Get-RC003SourceVersion {
    param([Parameter(Mandatory = $true)][string]$RC003Root)

    $path = Join-Path $RC003Root "src\ovb_rc003\VERSION"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf -ErrorAction Stop)) {
        throw "required VERSION file not found: $path"
    }
    $version = (Get-Content -LiteralPath $path -Raw -ErrorAction Stop).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
        throw "invalid application version in ${path}: $version"
    }
    return $version
}

function Write-RC003BuildProvenance {
    param(
        [Parameter(Mandatory = $true)][string]$RC003Root,
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$BuildRoot,
        [Parameter(Mandatory = $true)]$InputState
    )

    if (
        [string]$InputState.FingerprintSha256 -notmatch '^[0-9A-Fa-f]{64}$' -or
        [int]$InputState.FileCount -lt 1
    ) {
        throw "invalid build input state"
    }
    $currentInputState = Get-RC003BuildInputState `
        -RC003Root $RC003Root `
        -RepoRoot $RepoRoot
    if (
        $currentInputState.FileCount -ne [int]$InputState.FileCount -or
        $currentInputState.FingerprintSha256 -ne [string]$InputState.FingerprintSha256
    ) {
        throw "build inputs changed before the source fingerprint was written"
    }

    $mainExe = Join-Path $BuildRoot "RemoteMicRC003.exe"
    $hidHelper = Join-Path $BuildRoot "_internal\RemoteMicRC003HidHelper.exe"
    foreach ($path in @($mainExe, $hidHelper)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf -ErrorAction Stop)) {
            throw "required built executable not found: $path"
        }
    }
    $artifactState = Get-RC003ArtifactState -BuildRoot $BuildRoot
    $manifest = [ordered]@{
        schema_version = $script:RC003_BUILD_PROVENANCE_SCHEMA_VERSION
        app_version = Get-RC003SourceVersion -RC003Root $RC003Root
        input_fingerprint_sha256 = $InputState.FingerprintSha256
        input_file_count = $InputState.FileCount
        artifact_fingerprint_sha256 = $artifactState.FingerprintSha256
        artifact_file_count = $artifactState.FileCount
        main_exe_sha256 = (Get-RC003FileState -Path $mainExe).Sha256
        hid_helper_sha256 = (Get-RC003FileState -Path $hidHelper).Sha256
    }
    $path = Get-RC003BuildProvenancePath -BuildRoot $BuildRoot
    $temporary = "$path.$PID.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = $manifest | ConvertTo-Json -Depth 3
        [System.IO.File]::WriteAllText(
            $temporary,
            $json + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $path -Force -ErrorAction Stop
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
    return [pscustomobject]$manifest
}

function Assert-RC003BuildProvenanceMatches {
    param(
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)]$Actual,
        [Parameter(Mandatory = $true)][string]$Context
    )

    $fields = @(
        "schema_version",
        "app_version",
        "input_fingerprint_sha256",
        "input_file_count",
        "artifact_fingerprint_sha256",
        "artifact_file_count",
        "main_exe_sha256",
        "hid_helper_sha256"
    )
    foreach ($field in $fields) {
        if ([string]$Expected.$field -ne [string]$Actual.$field) {
            throw "${Context}: build output changed while the package was being created"
        }
    }
}

function Assert-RC003BuildProvenance {
    param(
        [Parameter(Mandatory = $true)][string]$RC003Root,
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$BuildRoot
    )

    $path = Get-RC003BuildProvenancePath -BuildRoot $BuildRoot
    if (-not (Test-Path -LiteralPath $path -PathType Leaf -ErrorAction Stop)) {
        throw "built output has no verified source fingerprint; run build-candidate.ps1 again"
    }
    try {
        $manifest = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch {
        throw "built output source fingerprint is invalid"
    }
    $required = @(
        "schema_version",
        "app_version",
        "input_fingerprint_sha256",
        "input_file_count",
        "artifact_fingerprint_sha256",
        "artifact_file_count",
        "main_exe_sha256",
        "hid_helper_sha256"
    )
    $propertyNames = @($manifest.PSObject.Properties.Name)
    foreach ($name in $required) {
        if ($propertyNames -notcontains $name) {
            throw "built output source fingerprint is missing field: $name"
        }
    }
    if ([int]$manifest.schema_version -ne $script:RC003_BUILD_PROVENANCE_SCHEMA_VERSION) {
        throw "built output source fingerprint schema is unsupported"
    }
    foreach ($name in @(
        "input_fingerprint_sha256",
        "artifact_fingerprint_sha256",
        "main_exe_sha256",
        "hid_helper_sha256"
    )) {
        if ([string]$manifest.$name -notmatch '^[0-9A-Fa-f]{64}$') {
            throw "built output source fingerprint has invalid field: $name"
        }
    }
    foreach ($name in @("input_file_count", "artifact_file_count")) {
        try {
            $count = [int]$manifest.$name
        } catch {
            throw "built output source fingerprint has invalid field: $name"
        }
        if ($count -lt 1) {
            throw "built output source fingerprint has invalid field: $name"
        }
    }

    $sourceVersion = Get-RC003SourceVersion -RC003Root $RC003Root
    if ([string]$manifest.app_version -ne $sourceVersion) {
        throw "built output is stale: source VERSION=$sourceVersion built VERSION=$($manifest.app_version)"
    }
    $inputState = Get-RC003BuildInputState -RC003Root $RC003Root -RepoRoot $RepoRoot
    if (
        [int]$manifest.input_file_count -ne $inputState.FileCount -or
        [string]$manifest.input_fingerprint_sha256 -ne $inputState.FingerprintSha256
    ) {
        throw "built output is stale: build inputs changed after the last complete build"
    }
    $artifactState = Get-RC003ArtifactState -BuildRoot $BuildRoot
    if (
        [int]$manifest.artifact_file_count -ne $artifactState.FileCount -or
        [string]$manifest.artifact_fingerprint_sha256 -ne $artifactState.FingerprintSha256
    ) {
        throw "built output is invalid: packaged files changed after the last complete build"
    }

    $mainExe = Join-Path $BuildRoot "RemoteMicRC003.exe"
    $hidHelper = Join-Path $BuildRoot "_internal\RemoteMicRC003HidHelper.exe"
    if (
        (Get-RC003FileState -Path $mainExe).Sha256 -ne
        [string]$manifest.main_exe_sha256
    ) {
        throw "built output is invalid: main executable hash changed"
    }
    if (
        (Get-RC003FileState -Path $hidHelper).Sha256 -ne
        [string]$manifest.hid_helper_sha256
    ) {
        throw "built output is invalid: HID helper hash changed"
    }
    return $manifest
}
