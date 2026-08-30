#requires -Version 5.1

param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$templateRoot = Split-Path -Parent $PSScriptRoot
$windowsRoot = Split-Path -Parent $templateRoot
$repositoryRoot = Resolve-Path (Join-Path $windowsRoot "..\..")
$sourceRoot = Resolve-Path (Join-Path $windowsRoot "rc003\scripts")
$testRoot = Resolve-Path (Join-Path $windowsRoot "rc003\tests")
$destinationPath = if ([System.IO.Path]::IsPathRooted($Destination)) {
    [System.IO.Path]::GetFullPath($Destination)
} else {
    [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Destination))
}
$protectedPaths = @(
    [System.IO.Path]::GetFullPath($templateRoot),
    [System.IO.Path]::GetFullPath($repositoryRoot),
    [System.IO.Path]::GetPathRoot($destinationPath)
)
if ($protectedPaths -contains $destinationPath) {
    throw "Destination must be a dedicated export directory: $destinationPath"
}

if (Test-Path -LiteralPath $destinationPath) {
    if (-not $Force) {
        throw "Destination already exists: $destinationPath"
    }
    Remove-Item -LiteralPath $destinationPath -Recurse -Force
}

$sourceFiles = @(
    "element_navigation_command_windows.py",
    "element_navigation_prototype.py",
    "element_navigation_support.py",
    "element_navigation_windows_host.py",
    "element_targeting_core.py",
    "spatial_navigation_core.py"
)
$metadataFiles = @(
    ".gitignore",
    "ATTRIBUTION.md",
    "COPYRIGHT.md",
    "pyproject.toml",
    "README.md",
    "RELEASE-CHECKLIST.md",
    "requirements.txt",
    "requirements-dev.txt",
    "THIRD_PARTY_NOTICES.md"
)

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([System.BitConverter]::ToString(
                $sha256.ComputeHash($stream)
            ) -replace "-", "").ToLowerInvariant()
        } finally {
            $sha256.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

New-Item -ItemType Directory -Path $destinationPath | Out-Null
$destinationScripts = New-Item -ItemType Directory -Path (
    Join-Path $destinationPath "scripts"
)
$destinationTests = New-Item -ItemType Directory -Path (
    Join-Path $destinationPath "tests"
)

foreach ($name in $metadataFiles) {
    Copy-Item -LiteralPath (Join-Path $templateRoot $name) -Destination $destinationPath
}
Copy-Item -LiteralPath (Join-Path $repositoryRoot "LICENSE.md") -Destination (
    Join-Path $destinationPath "LICENSE"
)
Copy-Item -LiteralPath (Join-Path $templateRoot "docs") -Destination $destinationPath -Recurse
foreach ($name in $sourceFiles) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot $name) -Destination $destinationScripts
}
Copy-Item -LiteralPath (
    Join-Path $testRoot "test_element_navigation_prototype.py"
) -Destination $destinationTests

$commit = (& git -C $repositoryRoot rev-parse HEAD 2>$null)
if ($LASTEXITCODE -ne 0) {
    $commit = "unknown"
}
$snapshotFiles = foreach ($name in $sourceFiles) {
    $path = Join-Path $destinationScripts $name
    [ordered]@{
        path = "scripts/$name"
        sha256 = Get-Sha256 -Path $path
    }
}
$snapshot = [ordered]@{
    sourceCommit = [string]$commit
    generatedAtUtc = [DateTime]::UtcNow.ToString("o")
    files = @($snapshotFiles)
}
$snapshot | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (
    Join-Path $destinationPath "SOURCE-SNAPSHOT.json"
) -Encoding UTF8

Write-Host "Exported OrthoFocus source to $destinationPath"
