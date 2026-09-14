#requires -Version 5.1
<#
.SYNOPSIS
    Builds an unsigned Remote Mic · RC003 candidate on Windows.

    Steps: create/activate a virtual environment, install
    requirements-dev.txt, run the public-boundary scan, run the test suite
    (gated with ``-W error::ResourceWarning`` plus a complete log scan for
    late resource leaks, matching the CI workflow),
    invoke PyInstaller against RemoteMicRC003.spec, then smoke-check the
    narrow HID helper with ``--self-check`` and the built executable with
    ``--dry-run`` and ``--qt-runtime-check``. The
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
    past a real failure. Console commands use ``$LASTEXITCODE`` via
    ``Assert-LastExitCode``. Frozen GUI-subsystem executables are launched
    with ``Start-Process -Wait -PassThru`` so their real ``ExitCode`` is read
    even when PowerShell leaves ``$LASTEXITCODE`` unset or stale.

.PARAMETER PythonExecutable
    Python 3.12 executable used to create the virtual environment. When this
    is omitted, the script tries "py -3.12" first and then "python". The
    selected interpreter and the resulting virtual environment must both be
    Python 3.12; another version stops the build before PyInstaller runs.
#>

param(
    [string]$PythonExecutable = ""
)

$ErrorActionPreference = "Stop"
$RC003Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot = (Resolve-Path (Join-Path $RC003Root "..\..\..")).Path
. (Join-Path $PSScriptRoot "build-provenance.ps1")
$BuildRoot = Join-Path $RC003Root "dist\RemoteMicRC003"
$BuildProvenancePath = Get-RC003BuildProvenancePath -BuildRoot $BuildRoot

function Assert-LastExitCode {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

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

function Get-PythonMinorVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [string[]]$PrefixArguments = @()
    )

    try {
        $output = & $Command @PrefixArguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        $exitCode = $LASTEXITCODE
    } catch {
        return $null
    }
    if ($exitCode -ne 0) {
        return $null
    }
    return (($output | Out-String).Trim())
}

function Resolve-BuildPython {
    param([string]$RequestedExecutable)

    if (-not [string]::IsNullOrWhiteSpace($RequestedExecutable)) {
        $command = (Get-Command $RequestedExecutable -ErrorAction Stop).Source
        $version = Get-PythonMinorVersion -Command $command
        if ($version -ne "3.12") {
            throw "candidate builds require Python 3.12; '$RequestedExecutable' reported '$version'"
        }
        return [pscustomobject]@{
            Command = $command
            PrefixArguments = @()
            Version = $version
        }
    }

    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        $version = Get-PythonMinorVersion -Command $pyLauncher.Source -PrefixArguments @("-3.12")
        if ($version -eq "3.12") {
            return [pscustomobject]@{
                Command = $pyLauncher.Source
                PrefixArguments = @("-3.12")
                Version = $version
            }
        }
    }

    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $version = Get-PythonMinorVersion -Command $python.Source
        if ($version -eq "3.12") {
            return [pscustomobject]@{
                Command = $python.Source
                PrefixArguments = @()
                Version = $version
            }
        }
    }

    throw "candidate builds require Python 3.12; install it or pass -PythonExecutable with a Python 3.12 executable"
}

Write-Host "== Remote Mic · RC003 candidate build =="

$BuildGate = Enter-RC003BuildGate -RC003Root $RC003Root
try {
    Push-Location $RC003Root
    try {
    # Any failed build attempt must leave the previous dist unable to masquerade
    # as the current source state.
    Remove-Item -LiteralPath $BuildProvenancePath -Force -ErrorAction SilentlyContinue

    # Candidate builds run on a developer's interactive desktop. Tests must
    # never install a real keyboard hook or inject an actual key edge there.
    $env:RC003_DISABLE_LIVE_INPUT = "1"
    $env:RC003_ALLOW_LIVE_INPUT_TESTS = "0"

    $sourceVersionFile = Join-Path $RC003Root "src\ovb_rc003\VERSION"
    $sourceVersion = (Get-Content -LiteralPath $sourceVersionFile -Raw).Trim()
    if ($sourceVersion -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
        throw "invalid application version in ${sourceVersionFile}: $sourceVersion"
    }

    $buildPython = Resolve-BuildPython -RequestedExecutable $PythonExecutable
    $buildPythonCommand = $buildPython.Command
    $buildPythonPrefixArguments = @($buildPython.PrefixArguments)
    Write-Host "-- build interpreter: Python $($buildPython.Version) --"

    Write-Host "-- stop marked source development session --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "stop-dev.ps1")
    Assert-LastExitCode "stop-dev.ps1"

    if (-not (Test-Path ".venv")) {
        & $buildPythonCommand @buildPythonPrefixArguments -m venv .venv
        Assert-LastExitCode "python -m venv"
    }
    $venvPython = Join-Path ".venv" "Scripts\python.exe"
    $venvVersion = Get-PythonMinorVersion -Command $venvPython
    if ($venvVersion -ne "3.12") {
        throw "candidate build virtual environment must use Python 3.12; .venv reported '$venvVersion'"
    }

    & $venvPython -m pip install --upgrade pip
    Assert-LastExitCode "pip install --upgrade pip"
    & $venvPython -m pip install -r requirements-dev.txt
    Assert-LastExitCode "pip install -r requirements-dev.txt"

    Write-Host "-- generate shared Windows application icon --"
    & $venvPython (Join-Path "build" "generate-app-icon.py")
    Assert-LastExitCode "generate-app-icon.py"

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

    $inputStateBeforeBuild = Get-RC003BuildInputState `
        -RC003Root $RC003Root `
        -RepoRoot $RepoRoot

    Write-Host "-- third-party notice and license inventory --"
    & $venvPython (Join-Path "build" "check-third-party-notices.py")
    Assert-LastExitCode "check-third-party-notices.py"

    Write-Host "-- public boundary scan --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "check-public-boundary.ps1")
    Assert-LastExitCode "check-public-boundary.ps1"

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

    Write-Host "-- compile selected permission modules with Cython --"
    & $venvPython (Join-Path "build" "prepare-cython-core.py")
    Assert-LastExitCode "prepare-cython-core.py"

    $cythonSourceRoot = (Resolve-Path (Join-Path "build" "cython-stage\src")).Path
    $env:PYTHONPATH = $cythonSourceRoot
    Write-Host "-- compiled permission module tests --"
    & $venvPython -u -W error::ResourceWarning -m unittest `
        tests.test_hid_elevation_windows `
        tests.test_hid_helper_consumers `
        -v
    Assert-LastExitCode "compiled permission module tests"

    Write-Host "-- compiled HID helper self-check (no UAC/task/HID changes) --"
    & $venvPython (Join-Path $cythonSourceRoot "hid_helper_launcher.py") --self-check
    Assert-LastExitCode "compiled HID helper --self-check"

    $env:RC003_BUILD_SOURCE_ROOT = $cythonSourceRoot
    Write-Host "-- PyInstaller source root: Cython stage --"
    Write-Host "-- PyInstaller build (unsigned candidate) --"
    & $venvPython -m PyInstaller (Join-Path "build" "RemoteMicRC003.spec") --distpath dist --workpath build\pyinstaller-work --noconfirm
    Assert-LastExitCode "PyInstaller"

    Write-Host "-- built-artifact dry-run smoke check (no GUI/BLE/HID/audio) --"
    $builtExe = Join-Path "dist" (Join-Path "RemoteMicRC003" "RemoteMicRC003.exe")
    if (-not (Test-Path $builtExe)) {
        throw "expected built executable not found: $builtExe"
    }
    $builtHidHelper = Join-Path "dist" (Join-Path "RemoteMicRC003" (Join-Path "_internal" "RemoteMicRC003HidHelper.exe"))
    if (-not (Test-Path $builtHidHelper)) {
        throw "expected narrow HID helper not found: $builtHidHelper"
    }
    $builtVersionFile = Join-Path "dist" (Join-Path "RemoteMicRC003" (Join-Path "_internal" (Join-Path "ovb_rc003" "VERSION")))
    if (-not (Test-Path $builtVersionFile)) {
        throw "expected built VERSION file not found: $builtVersionFile"
    }
    $builtPackageRoot = Split-Path $builtVersionFile -Parent
    foreach ($compiledModuleName in @("hid_elevation_windows", "hid_helper_consumers")) {
        $compiledMatches = @(
            Get-ChildItem -LiteralPath $builtPackageRoot -File -Filter "${compiledModuleName}*.pyd"
        )
        if ($compiledMatches.Count -ne 1) {
            throw "expected one compiled $compiledModuleName extension in frozen output"
        }
    }
    $builtVersion = (Get-Content -LiteralPath $builtVersionFile -Raw).Trim()
    if ($builtVersion -ne $sourceVersion) {
        throw "built VERSION mismatch: source=$sourceVersion built=$builtVersion"
    }
    $rootExecutables = @(Get-ChildItem -LiteralPath (Split-Path $builtExe -Parent) -Force -Filter "*.exe" -File)
    if ($rootExecutables.Count -ne 1 -or $rootExecutables[0].Name -ne "RemoteMicRC003.exe") {
        throw "build root must expose only RemoteMicRC003.exe"
    }
    Write-Host "-- built HID helper self-check (no UAC/task/HID changes) --"
    Invoke-FrozenExecutableCheck `
        -FilePath $builtHidHelper `
        -ArgumentList @("--self-check") `
        -Step "$builtHidHelper --self-check"

    Invoke-FrozenExecutableCheck `
        -FilePath $builtExe `
        -ArgumentList @("--dry-run") `
        -Step "$builtExe --dry-run"

    Write-Host "-- built-artifact Qt runtime smoke check (no GUI/BLE/HID/audio) --"
    Invoke-FrozenExecutableCheck `
        -FilePath $builtExe `
        -ArgumentList @("--qt-runtime-check") `
        -Step "$builtExe --qt-runtime-check"

    $inputStateAfterBuild = Get-RC003BuildInputState `
        -RC003Root $RC003Root `
        -RepoRoot $RepoRoot
    if (
        $inputStateAfterBuild.FileCount -ne $inputStateBeforeBuild.FileCount -or
        $inputStateAfterBuild.FingerprintSha256 -ne $inputStateBeforeBuild.FingerprintSha256
    ) {
        throw "build inputs changed while PyInstaller or frozen checks were running"
    }
    Write-RC003BuildProvenance `
        -RC003Root $RC003Root `
        -RepoRoot $RepoRoot `
        -BuildRoot $BuildRoot `
        -InputState $inputStateBeforeBuild | Out-Null
    Assert-RC003BuildProvenance `
        -RC003Root $RC003Root `
        -RepoRoot $RepoRoot `
        -BuildRoot $BuildRoot | Out-Null

    Write-Host "== build complete: dist\RemoteMicRC003\ (unsigned) =="
    } catch {
        Remove-Item -LiteralPath $BuildProvenancePath -Force -ErrorAction SilentlyContinue
        throw
    } finally {
        Pop-Location
    }
} finally {
    Exit-RC003BuildGate -Mutex $BuildGate
}
