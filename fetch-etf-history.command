#!/usr/bin/env bash
# 双击此文件：采集最新 515450 估值到本地 json（不提交 git）
cd "$(dirname "$0")" || exit 1
exec ./fetch-etf-history.sh
