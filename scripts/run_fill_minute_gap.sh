#!/bin/bash
# 补齐 kline_minute 2023-03 ~ 2026-08-06 断档。
# 用法:
#   ./run_fill_minute_gap.sh              # 完整跑
#   ./run_fill_minute_gap.sh --failed     # 只跑上次失败的段
#   tail -f /tmp/ts-fill/fill_minute_gap.log  # 另一个窗口看进度
set -e
cd "$(dirname "$0")/.."
exec ./backend/.venv/bin/python -u scripts/fill_minute_gap.py "$@" 2>&1 | tee -a /tmp/ts-fill/fill_minute_gap.log
