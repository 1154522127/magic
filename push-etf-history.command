#!/usr/bin/env bash
# 双击此文件：采集 515450 估值历史并 git push
cd "$(dirname "$0")" || exit 1
exec ./push-etf-history.sh
