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

# GetFullPath alone does not expand short names or resolve directory junctions.
# Resolve the existing ancestor through Windows before comparing path boundaries.
if (-not ("OrthoFocus.ExportPathNative" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
namespace OrthoFocus {
    public static class ExportPathNative {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(string path, uint access,
            uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle,
            StringBuilder path, uint size, uint flags);
        public static string Resolve(string path) {
            using (var handle = CreateFileW(path, 0, 7, IntPtr.Zero, 3,
                    0x02000000, IntPtr.Zero)) {
                if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                var buffer = new StringBuilder(512);
                uint length = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
                if (length >= buffer.Capacity) {
                    buffer = new StringBuilder(checked((int)length + 1));
                    length = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
                }
                if (length == 0 || length >= buffer.Capacity)
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                string result = buffer.ToString();
                if (result.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase))
                    return @"\\" + result.Substring(8);
                if (result.StartsWith(@"\\?\", StringComparison.Ordinal)) return result.Substring(4);
                return result;
            }
        }
    }
}
'@
}

function Get-CanonicalExportPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $existing = [System.IO.Path]::GetFullPath($Path)
    $suffix = New-Object 'System.Collections.Generic.List[string]'
    while (-not (Test-Path -LiteralPath $existing)) {
        $suffix.Insert(0, [System.IO.Path]::GetFileName($existing))
        $parent = [System.IO.Path]::GetDirectoryName($existing)
        if (-not $parent -or $parent -eq $existing) {
            throw "Cannot resolve export directory: $Path"
        }
        $existing = $parent
    }
    $resolved = [OrthoFocus.ExportPathNative]::Resolve($existing)
    foreach ($name in $suffix) {
        $resolved = [System.IO.Path]::Combine($resolved, $name)
    }
    return $resolved.TrimEnd([char[]]"\/")
}

function Assert-ExportDestination {
    # Reject reparse points in the destination chain instead of deleting through
    # a link whose target can be unrelated to the requested export directory.
    $ancestor = $destinationPath
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor -Force
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "Destination must not use directory links: $destinationPath"
            }
        }
        $ancestor = [System.IO.Path]::GetDirectoryName($ancestor)
    }
    $destination = Get-CanonicalExportPath $destinationPath
    $root = ([System.IO.Path]::GetPathRoot($destination)).TrimEnd([char[]]"\/")
    $comparison = [System.StringComparison]::OrdinalIgnoreCase
    if ($destination.Equals($root, $comparison)) {
        throw "Destination must be outside the repository and its ancestors: $destinationPath"
    }
    foreach ($protected in @($repositoryRoot, $templateRoot, $sourceRoot, $testRoot)) {
        $source = Get-CanonicalExportPath ([string]$protected)
        if ($destination.Equals($source, $comparison) -or
            $destination.StartsWith($source + "\", $comparison) -or
            $source.StartsWith($destination + "\", $comparison)) {
            throw "Destination must be outside the repository and its ancestors: $destinationPath"
        }
    }
    if ($Force -and (Test-Path -LiteralPath $destinationPath -PathType Container)) {
        $pending = New-Object 'System.Collections.Generic.Stack[string]'
        $pending.Push($destinationPath)
        while ($pending.Count -gt 0) {
            foreach ($child in (Get-ChildItem -LiteralPath $pending.Pop() -Force)) {
                if ($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                    throw "Existing export must not contain links: $destinationPath"
                }
                if ($child.PSIsContainer) { $pending.Push($child.FullName) }
            }
        }
    }
}

Assert-ExportDestination
if (Test-Path -LiteralPath $destinationPath) {
    if (-not $Force) {
        throw "Destination already exists: $destinationPath"
    }
    Assert-ExportDestination
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
