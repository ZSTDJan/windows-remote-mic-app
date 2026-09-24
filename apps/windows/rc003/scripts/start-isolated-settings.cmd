@echo off
rem Source-only manual test entry. No build, install, or production config migration.
setlocal
pushd "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo This source tree requires its own Python 3.12 .venv.
    pause
    popd
    exit /b 2
)
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))"
if errorlevel 1 (
    echo This source tree requires Python 3.12.
    pause
    popd
    exit /b 2
)
set "PYTHONPATH=%CD%\src"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
".venv\Scripts\python.exe" -m ovb_rc003 --settings --isolated-test
set "test_exit=%ERRORLEVEL%"
if not "%test_exit%"=="0" pause
popd
exit /b %test_exit%
