#requires -Version 5.1
<#
.SYNOPSIS
    Builds an unsigned Remote Mic · RC003 candidate on Windows.

    Steps: create/activate a virtual environment, install
    requirements-dev.txt, run the public-boundary scan, run the test suite
    (gated with ``-W error::ResourceWarning`` plus a complete log scan for
    late resource leaks, matching the CI workflow),
    invoke PyInstaller against RemoteMicRC003.spec, then smoke-check
    the built executable with ``--dry-run`` and ``--qt-runtime-check``. The
    second check loads the real frozen Qt DLL chain and verifies main.qml,
    without constructing a window or touching BLE/HID/audio, so a package
    that cannot open settings is caught here rather than on a real machine.

    Fetches and hash-verifies the pinned Frida Gadget and VB-CABLE helper
    before freezing so a complete RC003 build cannot silently lose the HID
    path required for Back and volume buttons. It does NOT request elevation
    and does NOT sign the resulting binary.

    Exit-code gating (XRBM-014 review RETRY P2 #3): PowerShell's
    ``$ErrorActionPreference = "Stop"`` only turns PowerShell-cmdlet errors
    into terminating errors - a NATIVE command (venv creation, pip, python,
    PyInstaller, the built .exe itself) can exit non-zero without PowerShell
    treating that as an error at all, letting the script silently continue
    past a real failure. Every native invocation below is followed by an
    explicit ``$LASTEXITCODE`` check via ``Assert-LastExitCode``.

.PARAMETER PythonExecutable
    Python interpreter to create the virtual environment with. Defaults to
    "py -3.12" if available, else "python".
#>

param(
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
$RC003Root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Assert-LastExitCode {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

Write-Host "== Remote Mic · RC003 candidate build =="

Push-Location $RC003Root
try {
    # Keep Python and child-process output deterministic on English Windows.
    $env:PYTHONUTF8 = "1"

    # Candidate builds run on a developer's interactive desktop. Tests must
    # never install a real keyboard hook or inject an actual key edge there.
    $env:RC003_DISABLE_LIVE_INPUT = "1"
    $env:RC003_ALLOW_LIVE_INPUT_TESTS = "0"

    Write-Host "-- stop marked source development session --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "stop-dev.ps1")
    Assert-LastExitCode "stop-dev.ps1"

    if (-not (Test-Path ".venv")) {
        & $PythonExecutable -m venv .venv
        Assert-LastExitCode "python -m venv"
    }
    $venvPython = Join-Path ".venv" "Scripts\python.exe"

    & $venvPython -m pip install --upgrade pip
    Assert-LastExitCode "pip install --upgrade pip"
    & $venvPython -m pip install -r requirements-dev.txt
    Assert-LastExitCode "pip install -r requirements-dev.txt"

    Write-Host "-- third-party notice and license inventory --"
    & $venvPython (Join-Path "build" "check-third-party-notices.py")
    Assert-LastExitCode "check-third-party-notices.py"

    Write-Host "-- generate shared Windows application icon --"
    & $venvPython (Join-Path "build" "generate-app-icon.py")
    Assert-LastExitCode "generate-app-icon.py"

    Write-Host "-- public boundary scan --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "check-public-boundary.ps1")
    Assert-LastExitCode "check-public-boundary.ps1"

    # XRBM-031 In-scope item 8: fetch + hash-verify the official VB-CABLE
    # base package BEFORE PyInstaller runs, so the frozen build deterministically
    # bundles it (see build/RemoteMicRC003.spec) and end users of the
    # built candidate never need their own network access to install the
    # optional driver. A download failure or hash mismatch here must fail
    # this whole build closed, not silently produce a candidate with no
    # bundled driver helper.
    Write-Host "-- fetch + verify VB-CABLE driver pack --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "fetch-vb-cable.ps1")
    Assert-LastExitCode "fetch-vb-cable.ps1"

    Write-Host "-- fetch + verify Frida Gadget --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "fetch-frida-gadget.ps1")
    Assert-LastExitCode "fetch-frida-gadget.ps1"

    Write-Host "-- test suite --"
    $env:PYTHONPATH = Join-Path $RC003Root "src"
    $testLogPath = Join-Path ([System.IO.Path]::GetTempPath()) (
        "remote-mic-rc003-tests-{0}.log" -f [guid]::NewGuid().ToString("N")
    )
    try {
        # unittest writes normal progress to stderr. Windows PowerShell turns
        # redirected native stderr into non-terminating ErrorRecord objects;
        # with the script-wide Stop policy those ordinary lines would abort
        # the build before the real process exit code can be checked.
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $venvPython -u -W error::ResourceWarning -m unittest discover -s tests -t . -p "test_*.py" -v 2>&1 |
                Tee-Object -FilePath $testLogPath
            $testExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($testExitCode -ne 0) {
            throw "python -m unittest discover failed with exit code $testExitCode"
        }

        # Resource warnings raised during interpreter shutdown can print
        # after unittest has already decided to exit 0. Scan the complete
        # captured output so the local candidate gate cannot miss them.
        $logContent = Get-Content -LiteralPath $testLogPath -Raw
        $forbiddenPatterns = @(
            "ResourceWarning:",
            "unclosed event loop",
            "unclosed <socket.socket"
        )
        foreach ($pattern in $forbiddenPatterns) {
            if ($logContent -match [regex]::Escape($pattern)) {
                throw "test log contains forbidden resource-leak pattern '$pattern'"
            }
        }
    } finally {
        Remove-Item -LiteralPath $testLogPath -Force -ErrorAction SilentlyContinue
    }

    Write-Host "-- PyInstaller build (unsigned candidate) --"
    & $venvPython -m PyInstaller (Join-Path "build" "RemoteMicRC003.spec") --distpath dist --workpath build\pyinstaller-work --noconfirm
    Assert-LastExitCode "PyInstaller"

    Write-Host "-- built-artifact dry-run smoke check (no GUI/BLE/HID/audio) --"
    $builtExe = Join-Path "dist" (Join-Path "RemoteMicRC003" "RemoteMicRC003.exe")
    if (-not (Test-Path $builtExe)) {
        throw "expected built executable not found: $builtExe"
    }
    & $builtExe --dry-run
    Assert-LastExitCode "$builtExe --dry-run"

    Write-Host "-- built-artifact Qt runtime smoke check (no GUI/BLE/HID/audio) --"
    & $builtExe --qt-runtime-check
    Assert-LastExitCode "$builtExe --qt-runtime-check"

    Write-Host "== build complete: dist\RemoteMicRC003\ (unsigned) =="
} finally {
    Pop-Location
}
