@echo off
setlocal
title ZhiTuo AI - Stop
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
set "ZHITUO_EXIT_CODE=%ERRORLEVEL%"
if not "%ZHITUO_EXIT_CODE%"=="0" (
  echo.
  echo Shutdown failed. Keep this window open and review the error above.
  pause
  exit /b %ZHITUO_EXIT_CODE%
)
echo.
echo ZhiTuo services stopped successfully. Database volumes were preserved.
pause
exit /b 0
