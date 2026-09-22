@echo off
setlocal
title ZhiTuo AI - Start
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
set "ZHITUO_EXIT_CODE=%ERRORLEVEL%"
if not "%ZHITUO_EXIT_CODE%"=="0" (
  echo.
  echo Startup failed. Keep this window open and review the error above.
  pause
  exit /b %ZHITUO_EXIT_CODE%
)
echo.
echo ZhiTuo services started successfully.
pause
exit /b 0
