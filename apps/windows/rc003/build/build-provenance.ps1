#requires -Version 5.1

$script:RC003_BUILD_PROVENANCE_SCHEMA_VERSION = 2
$script:RC003_BUILD_LAYOUT_ID = "windows-portable-v2"

function Get-RC003ProductPresentation {
    param(
        [Parameter(Mandatory = $true)][string]$RC003Root,
        [string]$PythonExecutable = ""
    )

    $python = $PythonExecutable
    if ([string]::IsNullOrWhiteSpace($python)) {
        $python = [string]$env:RC003_PYTHON_EXECUTABLE
    }
    if ([string]::IsNullOrWhiteSpace($python)) {
        $projectPython = Join-Path $RC003Root ".venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
            $python = $projectPython
        } else {
            $pythonCommand = Get-Command "python.exe" -ErrorAction Stop
            $python = $pythonCommand.Source
        }
    }

    $adapter = Join-Path $RC003Root "build\product-presentation.py"
    $sourceRoot = Join-Path $RC003Root "src"
    if (-not (Test-Path -LiteralPath $adapter -PathType Leaf -ErrorAction Stop)) {
        throw "product presentation adapter not found: $adapter"
    }
    $raw = & $python $adapter --source-root $sourceRoot
    if ($LASTEXITCODE -ne 0) {
        throw "product presentation adapter failed with exit code $LASTEXITCODE"
    }
    try {
        $presentation = (($raw | Out-String).Trim() | ConvertFrom-Json)
    } catch {
        throw "product presentation adapter returned invalid JSON"
    }
    $required = @(
        "schema_version",
        "version",
        "presentation_label",
        "main_executable_name",
        "main_executable_stem",
        "portable_folder_name",
        "runtime_directory_name",
        "documentation_directory_name",
        "portable_readme_name",
        "hid_helper_executable_name",
        "hid_helper_file_description",
        "main_file_description",
        "product_name",
        "main_original_filename",
        "helper_original_filename"
    )
    $propertyNames = @($presentation.PSObject.Properties.Name)
    foreach ($name in $required) {
        if ($propertyNames -notcontains $name -or [string]::IsNullOrWhiteSpace([string]$presentation.$name)) {
            throw "product presentation is missing field: $name"
        }
    }
    if ([int]$presentation.schema_version -ne 1) {
        throw "product presentation schema is unsupported"
    }
    foreach ($field in @("runtime_directory_name", "documentation_directory_name")) {
        $directoryName = [string]$presentation.$field
        if (
            $directoryName -in @(".", "..") -or
            $directoryName -match '[\\/]' -or
            $directoryName.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0
        ) {
            throw "product presentation contains an unsafe directory name: $field"
        }
    }
    if (
        [string]$presentation.runtime_directory_name -ceq
        [string]$presentation.documentation_directory_name
    ) {
        throw "product presentation directory names must be distinct"
    }
    return $presentation
}

function Get-RC003BuildProductPaths {
    param(
        [Parameter(Mandatory = $true)][string]$RC003Root,
        [Parameter(Mandatory = $true)][string]$BuildRoot,
        [string]$PythonExecutable = ""
    )

    $presentation = Get-RC003ProductPresentation `
        -RC003Root $RC003Root `
        -PythonExecutable $PythonExecutable
    $runtimeRoot = Join-Path $BuildRoot ([string]$presentation.runtime_directory_name)
    return [pscustomobject]@{
        Presentation = $presentation
        MainExecutable = Join-Path $BuildRoot ([string]$presentation.main_executable_name)
        RuntimeRoot = $runtimeRoot
        HidHelper = Join-Path $runtimeRoot ([string]$presentation.hid_helper_executable_name)
        VersionFile = Join-Path $runtimeRoot "ovb_rc003\VERSION"
        ProvenanceFile = Join-Path $runtimeRoot "build-provenance.json"
    }
}

function Clear-RC003BuildProvenance {
    param(
        [Parameter(Mandatory = $true)][string]$RC003Root,
        [Parameter(Mandatory = $true)][string]$BuildRoot,
        [string]$PythonExecutable = ""
    )

    # Resolve the complete current presentation contract before mutating the
    # build output. If naming initialization fails, an existing receipt stays
    # untouched and the caller cannot proceed with a partly resolved layout.
    $paths = Get-RC003BuildProductPaths `
        -RC003Root $RC003Root `
        -BuildRoot $BuildRoot `
        -PythonExecutable $PythonExecutable
    Remove-Item `
        -LiteralPath $paths.ProvenanceFile `
        -Force `
        -ErrorAction SilentlyContinue
    return $paths.ProvenanceFile
}

function Assert-RC003ExecutableMetadata {
    param(
        [Parameter(Mandatory = $true)][string]$RC003Root,
        [Parameter(Mandatory = $true)][string]$BuildRoot,
        [string]$PythonExecutable = ""
    )

    $paths = Get-RC003BuildProductPaths `
        -RC003Root $RC003Root `
        -BuildRoot $BuildRoot `
        -PythonExecutable $PythonExecutable
    $checks = @(
        [pscustomobject]@{
            Path = $paths.MainExecutable
            FileDescription = [string]$paths.Presentation.main_file_description
            OriginalFilename = [string]$paths.Presentation.main_original_filename
        },
        [pscustomobject]@{
            Path = $paths.HidHelper
            FileDescription = [string]$paths.Presentation.hid_helper_file_description
            OriginalFilename = [string]$paths.Presentation.helper_original_filename
        }
    )
    foreach ($check in $checks) {
        if (-not (Test-Path -LiteralPath $check.Path -PathType Leaf -ErrorAction Stop)) {
            throw "required built executable not found: $($check.Path)"
        }
        $info = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($check.Path)
        $expected = [ordered]@{
            FileDescription = $check.FileDescription
            ProductName = [string]$paths.Presentation.product_name
            FileVersion = [string]$paths.Presentation.version
            ProductVersion = [string]$paths.Presentation.version
            OriginalFilename = $check.OriginalFilename
        }
        foreach ($field in $expected.Keys) {
            if ([string]$info.$field -cne [string]$expected[$field]) {
                throw "built executable metadata mismatch for $($check.Path): $field"
            }
        }
    }
}

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
        (Join-Path $RC003Root "build\portable-layout.ps1"),
        (Join-Path $RC003Root "build\product-presentation.py"),
        (Join-Path $RC003Root "build\check-public-boundary.ps1"),
        (Join-Path $RC003Root "build\check-release-readiness.py"),
        (Join-Path $RC003Root "build\check-third-party-notices.py"),
        (Join-Path $RC003Root "build\prepare-cython-core.py"),
        (Join-Path $RC003Root "build\native_inventory.py"),
        (Join-Path $RC003Root "build\check_native.py"),
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
        (Join-Path $RepoRoot "Resources\Chromecast-remote-photo.png"),
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
    param(
        [Parameter(Mandatory = $true)][string]$BuildRoot,
        [string]$ProvenanceFile = ""
    )

    if (-not (Test-Path -LiteralPath $BuildRoot -PathType Container -ErrorAction Stop)) {
        throw "expected build root not found: $BuildRoot"
    }
    $excludedRelativePath = ""
    if (-not [string]::IsNullOrWhiteSpace($ProvenanceFile)) {
        $excludedRelativePath = Get-RC003NormalizedRelativePath `
            -BasePath $BuildRoot `
            -FilePath $ProvenanceFile
    }
    $files = @(Get-ChildItem -LiteralPath $BuildRoot -Force -File -Recurse -ErrorAction Stop)
    if ($excludedRelativePath) {
        $files = @(
            $files | Where-Object {
                (Get-RC003NormalizedRelativePath `
                    -BasePath $BuildRoot `
                    -FilePath $_.FullName) -ne $excludedRelativePath
            }
        )
    }
    return Get-RC003ContentState -BasePath $BuildRoot -Files $files
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
        [Parameter(Mandatory = $true)]$InputState,
        [Parameter(Mandatory = $true)][switch]$ExecutableMetadataVerified,
        [string]$PythonExecutable = ""
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

    if (-not $ExecutableMetadataVerified) {
        throw "executable metadata must be verified before writing provenance"
    }
    $paths = Get-RC003BuildProductPaths `
        -RC003Root $RC003Root `
        -BuildRoot $BuildRoot `
        -PythonExecutable $PythonExecutable
    $mainExe = $paths.MainExecutable
    $hidHelper = $paths.HidHelper
    foreach ($path in @($mainExe, $hidHelper)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf -ErrorAction Stop)) {
            throw "required built executable not found: $path"
        }
    }
    $artifactState = Get-RC003ArtifactState `
        -BuildRoot $BuildRoot `
        -ProvenanceFile $paths.ProvenanceFile
    $manifest = [ordered]@{
        schema_version = $script:RC003_BUILD_PROVENANCE_SCHEMA_VERSION
        build_layout_id = $script:RC003_BUILD_LAYOUT_ID
        app_version = [string]$paths.Presentation.version
        main_executable_name = [string]$paths.Presentation.main_executable_name
        runtime_directory_name = [string]$paths.Presentation.runtime_directory_name
        documentation_directory_name = [string]$paths.Presentation.documentation_directory_name
        hid_helper_relative_path = "$($paths.Presentation.runtime_directory_name)/$($paths.Presentation.hid_helper_executable_name)"
        input_fingerprint_sha256 = $InputState.FingerprintSha256
        input_file_count = $InputState.FileCount
        artifact_fingerprint_sha256 = $artifactState.FingerprintSha256
        artifact_file_count = $artifactState.FileCount
        main_exe_sha256 = (Get-RC003FileState -Path $mainExe).Sha256
        hid_helper_sha256 = (Get-RC003FileState -Path $hidHelper).Sha256
    }
    $path = $paths.ProvenanceFile
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
        "build_layout_id",
        "app_version",
        "main_executable_name",
        "runtime_directory_name",
        "documentation_directory_name",
        "hid_helper_relative_path",
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
        [Parameter(Mandatory = $true)][string]$BuildRoot,
        [string]$PythonExecutable = ""
    )

    $paths = Get-RC003BuildProductPaths `
        -RC003Root $RC003Root `
        -BuildRoot $BuildRoot `
        -PythonExecutable $PythonExecutable
    $path = $paths.ProvenanceFile
    if (-not (Test-Path -LiteralPath $path -PathType Leaf -ErrorAction Stop)) {
        throw "built output has no verified source fingerprint; run build-candidate.ps1 again"
    }
    try {
        $manifest = Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    } catch {
        throw "built output source fingerprint is invalid: $($_.Exception.Message)"
    }
    $required = @(
        "schema_version",
        "build_layout_id",
        "app_version",
        "main_executable_name",
        "runtime_directory_name",
        "documentation_directory_name",
        "hid_helper_relative_path",
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
    $presentation = $paths.Presentation
    $expectedLayout = [ordered]@{
        build_layout_id = $script:RC003_BUILD_LAYOUT_ID
        app_version = [string]$presentation.version
        main_executable_name = [string]$presentation.main_executable_name
        runtime_directory_name = [string]$presentation.runtime_directory_name
        documentation_directory_name = [string]$presentation.documentation_directory_name
        hid_helper_relative_path = "$($presentation.runtime_directory_name)/$($presentation.hid_helper_executable_name)"
    }
    foreach ($field in $expectedLayout.Keys) {
        if ([string]$manifest.$field -cne [string]$expectedLayout[$field]) {
            throw "built output source fingerprint has incompatible layout field: $field"
        }
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

    $sourceVersion = [string]$presentation.version
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
    $artifactState = Get-RC003ArtifactState `
        -BuildRoot $BuildRoot `
        -ProvenanceFile $paths.ProvenanceFile
    if (
        [int]$manifest.artifact_file_count -ne $artifactState.FileCount -or
        [string]$manifest.artifact_fingerprint_sha256 -ne $artifactState.FingerprintSha256
    ) {
        throw "built output is invalid: packaged files changed after the last complete build"
    }

    $mainExe = $paths.MainExecutable
    $hidHelper = $paths.HidHelper
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
