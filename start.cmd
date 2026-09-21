@echo off
chcp 65001 >nul
title 智拓商机作战助手 - 启动
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
if errorlevel 1 (
  echo.
  echo 启动失败，请保留本窗口中的错误信息。
  pause
  exit /b 1
)
echo.
echo 智拓服务已成功启动。
pause
