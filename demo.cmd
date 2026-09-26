@echo off
setlocal
cd /d "%~dp0"

set "ARGS="
if /I "%~1"=="stop" set "ARGS=-Stop"
if /I "%~1"=="full" set "ARGS=-Full"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\demo.ps1" %ARGS%
