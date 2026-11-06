#!/usr/bin/env bash
# 从项目目录启动，使用已有 Python 环境，不自动安装系统依赖
set -e
cd -- "$(dirname -- "$0")"
exec "${PYTHON:-python3}" main.py "$@"
