@echo off
chcp 65001 >nul
setlocal
set "ReceiverReportExe="
set /a ReceiverReportCount=0 >nul
for %%F in ("%~dp0无线麦 win版 *.exe" "%~dp0..\无线麦 win版 *.exe") do if exist "%%~fF" (
  set "ReceiverReportExe=%%~fF"
  set /a ReceiverReportCount+=1 >nul
)
if not "%ReceiverReportCount%"=="1" (
  echo 请把本工具放在新版无线麦主程序旁边再运行。
  pause
  exit /b 2
)
echo 正在生成诊断报告，约需半分钟。请保持此窗口打开。
"%ReceiverReportExe%" --receiver-startup-report
if errorlevel 1 (
  echo 报告生成失败。请确认程序已完整解压，且所在目录可以写入。
  pause
  exit /b 1
)
echo 已在无线麦主程序所在目录生成“无线麦接收诊断”ZIP，请将最新生成的报告发给开发者。
pause
