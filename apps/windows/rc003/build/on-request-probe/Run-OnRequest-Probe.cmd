@echo off
setlocal
"%~dp0RemoteMicRC003.exe" --on-request-probe
exit /b %errorlevel%
