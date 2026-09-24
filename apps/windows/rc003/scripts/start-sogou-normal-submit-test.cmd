@echo off
rem Source-only, user-confirmed one-shot normal-submit test. Run this script as administrator yourself.
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
".venv\Scripts\python.exe" -B scripts\sogou_normal_submit_test.py --confirm-submit
set "probe_exit=%ERRORLEVEL%"
pause
popd
exit /b %probe_exit%
