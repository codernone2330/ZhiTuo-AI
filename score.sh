#!/usr/bin/env bash
# ===========================================================================
#  智拓 · 政企企业评分模型 —— 一键运行入口（macOS / Linux / Git Bash）
#
#  用法：
#    ./score.sh                                   # 用内置样例
#    ./score.sh path/to/企业清单.xlsx --top 100
#    ./score.sh 清单.xlsx --as-of 2026-10-06 --region 深圳·福田区
#
#  首次运行会自动创建隔离虚拟环境 scripts/.venv 并安装 pandas / openpyxl。
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")"

VENV="scripts/.venv"
PYEXE="$VENV/Scripts/python.exe"          # Windows(Git Bash)
[ -x "$PYEXE" ] || PYEXE="$VENV/bin/python" # macOS / Linux

if [ ! -x "$PYEXE" ]; then
  echo "[1/3] 首次运行：创建虚拟环境 $VENV ..."
  if command -v py >/dev/null 2>&1; then py -3 -m venv "$VENV"; else python3 -m venv "$VENV"; fi
  PYEXE="$VENV/Scripts/python.exe"; [ -x "$PYEXE" ] || PYEXE="$VENV/bin/python"
  echo "[2/3] 安装依赖 pandas / openpyxl ..."
  "$PYEXE" -m pip install -q --upgrade pip
  "$PYEXE" -m pip install -q pandas openpyxl
fi

echo "[3/3] 运行评分模型 ..."
"$PYEXE" scripts/score_enterprise.py "$@"

echo
echo "产物目录： output/enterprise_scoring/"
echo "  - scoring_report.html          用浏览器打开看报告"
echo "  - 政企企业评分_三版本.csv      评分名单（含联系方式，可直接分派）"
echo "  - scoring_anchors_lock.json    锚点锁"
echo "  - scoring_metrics.json         指标明细"
