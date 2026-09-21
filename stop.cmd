@echo off
chcp 65001 >nul
title 智拓商机作战助手 - 停止
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
if errorlevel 1 (
  echo.
  echo 停止失败，请保留本窗口中的错误信息。
  pause
  exit /b 1
)
echo.
echo 智拓服务已安全停止，数据库数据卷已保留。
pause
