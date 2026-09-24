#requires -Version 5.1

function Get-RC003PortableStreamSha256 {
    param([Parameter(Mandatory = $true)][System.IO.Stream]$Stream)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha256.ComputeHash($Stream)
    } finally {
        $sha256.Dispose()
    }
    return [System.BitConverter]::ToString($bytes).Replace("-", "")
}

function Get-RC003PortableFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    try {
        return Get-RC003PortableStreamSha256 -Stream $stream
    } finally {
        $stream.Dispose()
    }
}

function Get-RC003PortableArchivePathInfo {
    param([Parameter(Mandatory = $true)][string]$EntryName)

    $normalized = $EntryName.Replace([char]92, [char]47)
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        throw "portable ZIP contains an empty entry path"
    }
    if ($normalized.StartsWith("/") -or $normalized -match '^[A-Za-z]:') {
        throw "portable ZIP contains an absolute entry path: $normalized"
    }

    $isDirectory = $normalized.EndsWith("/")
    $canonical = $normalized.TrimEnd("/")
    if ([string]::IsNullOrWhiteSpace($canonical)) {
        throw "portable ZIP contains an unsafe entry path: $normalized"
    }
    $segments = @($canonical.Split("/"))
    foreach ($segment in $segments) {
        if (
            [string]::IsNullOrWhiteSpace($segment) -or
            $segment -in @(".", "..") -or
            $segment.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0
        ) {
            throw "portable ZIP contains an unsafe entry path: $normalized"
        }
    }

    return [pscustomobject]@{
        Path = [string]::Join("/", $segments)
        IsDirectory = $isDirectory
        Segments = $segments
    }
}

function Add-RC003PortableArchivePathKind {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Kinds,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet("File", "Directory")][string]$Kind
    )

    if ($Kinds.ContainsKey($Path)) {
        $existing = $Kinds[$Path]
        if ($existing.Path -cne $Path -or $existing.Kind -cne $Kind) {
            throw "portable ZIP contains a directory/path collision: $Path"
        }
        return
    }
    $Kinds[$Path] = [pscustomobject]@{
        Path = $Path
        Kind = $Kind
    }
}

function Assert-RC003PortableStagingRoot {
    param(
        [Parameter(Mandatory = $true)][string]$StagingDirectory,
        [Parameter(Mandatory = $true)]$ProductPresentation
    )

    $expected = [ordered]@{
        ([string]$ProductPresentation.main_executable_name) = "File"
        ([string]$ProductPresentation.runtime_directory_name) = "Directory"
        ([string]$ProductPresentation.documentation_directory_name) = "Directory"
    }
    $entries = @(Get-ChildItem -LiteralPath $StagingDirectory -Force -ErrorAction Stop)
    if ($entries.Count -ne $expected.Count) {
        throw "portable staging root must contain exactly the main executable, runtime directory, and documentation directory"
    }
    foreach ($entry in $entries) {
        $matchingName = @($expected.Keys | Where-Object { $_ -ceq $entry.Name })
        if ($matchingName.Count -ne 1) {
            throw "portable staging root contains an unexpected entry: $($entry.Name)"
        }
        $actualKind = "File"
        if ($entry.PSIsContainer) {
            $actualKind = "Directory"
        }
        if ($actualKind -cne [string]$expected[$matchingName[0]]) {
            throw "portable staging root entry has the wrong type: $($entry.Name)"
        }
    }
}

function Assert-RC003PortableZip {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$TopLevelName,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][string]$StagingDirectory,
        [Parameter(Mandatory = $true)]$ProductPresentation
    )

    Assert-RC003PortableStagingRoot `
        -StagingDirectory $StagingDirectory `
        -ProductPresentation $ProductPresentation

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $explicitEntries = @{}
        $archiveKinds = @{}
        $archiveFiles = @{}
        foreach ($entry in $archive.Entries) {
            $pathInfo = Get-RC003PortableArchivePathInfo -EntryName $entry.FullName
            $entryPath = [string]$pathInfo.Path
            $entryKind = "File"
            if ($pathInfo.IsDirectory) {
                $entryKind = "Directory"
            }
            if ($explicitEntries.ContainsKey($entryPath)) {
                throw "portable ZIP contains a duplicate entry: $entryPath"
            }
            $explicitEntries[$entryPath] = $entryKind
            Add-RC003PortableArchivePathKind `
                -Kinds $archiveKinds `
                -Path $entryPath `
                -Kind $entryKind

            $segments = @($pathInfo.Segments)
            for ($index = 1; $index -lt $segments.Count; $index++) {
                $parentPath = [string]::Join("/", $segments[0..($index - 1)])
                Add-RC003PortableArchivePathKind `
                    -Kinds $archiveKinds `
                    -Path $parentPath `
                    -Kind "Directory"
            }
            if ($entryKind -ceq "File") {
                $archiveFiles[$entryPath] = $entry
            }
        }
        if ($archiveKinds.Count -eq 0) {
            throw "portable ZIP is empty"
        }

        $topLevels = @(
            $archiveKinds.Values |
                ForEach-Object { $_.Path.Split("/")[0] } |
                Sort-Object -Unique
        )
        if ($topLevels.Count -ne 1 -or $topLevels[0] -cne $TopLevelName) {
            throw "portable ZIP must contain exactly one top-level directory named $TopLevelName"
        }
        if (
            -not $archiveKinds.ContainsKey($TopLevelName) -or
            $archiveKinds[$TopLevelName].Kind -cne "Directory"
        ) {
            throw "portable ZIP top-level entry must be a directory: $TopLevelName"
        }

        $rootKinds = @{}
        $topLevelPrefix = "$TopLevelName/"
        foreach ($pathState in $archiveKinds.Values) {
            if (-not $pathState.Path.StartsWith($topLevelPrefix, [System.StringComparison]::Ordinal)) {
                continue
            }
            $relativePath = $pathState.Path.Substring($topLevelPrefix.Length)
            if (-not $relativePath) {
                continue
            }
            $rootName = $relativePath.Split("/")[0]
            $rootPath = "$TopLevelName/$rootName"
            $rootKinds[$rootName] = [string]$archiveKinds[$rootPath].Kind
        }
        $expectedRootKinds = [ordered]@{
            ([string]$ProductPresentation.main_executable_name) = "File"
            ([string]$ProductPresentation.runtime_directory_name) = "Directory"
            ([string]$ProductPresentation.documentation_directory_name) = "Directory"
        }
        if ($rootKinds.Count -ne $expectedRootKinds.Count) {
            throw "portable ZIP root must contain exactly the main executable, runtime directory, and documentation directory"
        }
        foreach ($expectedName in $expectedRootKinds.Keys) {
            $matchingName = @($rootKinds.Keys | Where-Object { $_ -ceq $expectedName })
            if (
                $matchingName.Count -ne 1 -or
                [string]$rootKinds[$matchingName[0]] -cne [string]$expectedRootKinds[$expectedName]
            ) {
                throw "portable ZIP root entry is missing or has the wrong type: $expectedName"
            }
        }

        $expectedMainEntry = "$TopLevelName/$($ProductPresentation.main_executable_name)"
        if (-not $archiveFiles.ContainsKey($expectedMainEntry)) {
            throw "portable ZIP root must expose only $expectedMainEntry"
        }
        $helperEntryName = "$TopLevelName/$($ProductPresentation.runtime_directory_name)/$($ProductPresentation.hid_helper_executable_name)"
        if (-not $archiveFiles.ContainsKey($helperEntryName)) {
            throw "portable ZIP is missing the internal HID helper"
        }
        $versionEntryName = "$TopLevelName/$($ProductPresentation.runtime_directory_name)/ovb_rc003/VERSION"
        if (-not $archiveFiles.ContainsKey($versionEntryName)) {
            throw "portable ZIP is missing the frozen VERSION file"
        }
        $provenanceEntryName = "$TopLevelName/$($ProductPresentation.runtime_directory_name)/build-provenance.json"
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
        if ($zipVersion -cne $ExpectedVersion) {
            throw "portable ZIP VERSION mismatch: source=$ExpectedVersion zip=$zipVersion"
        }

        $pathSeparators = [char[]]@(
            [System.IO.Path]::DirectorySeparatorChar,
            [char]47
        )
        $stagingRoot = (Resolve-Path -LiteralPath $StagingDirectory).Path.TrimEnd(
            $pathSeparators
        )
        $stagingFiles = @(
            Get-ChildItem -LiteralPath $stagingRoot -Force -File -Recurse | ForEach-Object {
                $relativePath = $_.FullName.Substring($stagingRoot.Length).TrimStart(
                    $pathSeparators
                ).Replace([System.IO.Path]::DirectorySeparatorChar, [char]47)
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
            $stagingHash = Get-RC003PortableFileSha256 -Path $stagingFile.FullName
            $stream = $entry.Open()
            try {
                $zipHash = Get-RC003PortableStreamSha256 -Stream $stream
            } finally {
                $stream.Dispose()
            }
            if ($zipHash -cne $stagingHash) {
                throw "portable ZIP file hash mismatch: $($stagingFile.EntryName)"
            }
        }
    } finally {
        $archive.Dispose()
    }
}
