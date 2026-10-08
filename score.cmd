@echo off
rem ===========================================================================
rem  智拓 · 政企企业评分模型 —— 一键运行入口（Windows）
rem
rem  用法：
rem    双击本文件                  -> 用内置样例跑一遍，产物在 output\enterprise_scoring\
rem    拖动企业清单.xlsx 到本文件上 -> 用指定清单评分
rem    命令行： score.cmd path\to\清单.xlsx --as-of 2026-10-06 --top 100
rem
rem  首次运行会自动创建隔离虚拟环境 scripts\.venv 并安装 pandas / openpyxl，
rem  不会污染系统 Python。之后直接复用。
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "VENV=scripts\.venv"
set "PYEXE=%VENV%\Scripts\python.exe"

if not exist "%PYEXE%" (
  echo [1/3] 首次运行：创建虚拟环境 %VENV% ...
  py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%"
  if not exist "%PYEXE%" (
    echo [错误] 创建虚拟环境失败：未找到可用的 Python 3.10+。
    echo        请先安装 Python（勾选 Add to PATH）后重试。
    pause
    exit /b 1
  )
  echo [2/3] 安装依赖 pandas / openpyxl ...
  "%PYEXE%" -m pip install -q --upgrade pip
  "%PYEXE%" -m pip install -q pandas openpyxl
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

echo [3/3] 运行评分模型 ...
"%PYEXE%" "scripts\score_enterprise.py" %*

if errorlevel 1 (
  echo.
  echo [运行失败] 请把上面的报错信息发我。
  pause
  exit /b 1
)

echo.
echo 产物目录： output\enterprise_scoring\
echo   - scoring_report.html          用浏览器打开看报告
echo   - 政企企业评分_三版本.csv      评分名单（含联系方式，可直接分派）
echo   - scoring_anchors_lock.json    锚点锁
echo   - scoring_metrics.json         指标明细
echo.
pause
endlocal
