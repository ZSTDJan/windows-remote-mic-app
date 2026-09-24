@echo off
rem Source-only read-only probe. User explicitly runs this as administrator.
setlocal
pushd "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo This source tree requires its own Python 3.12 .venv.
    pause
    popd
    exit /b 2
)
set "PYTHONPATH=%CD%\src"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
".venv\Scripts\python.exe" -B scripts\sogou_stop_probe.py
set "probe_exit=%ERRORLEVEL%"
pause
popd
exit /b %probe_exit%
