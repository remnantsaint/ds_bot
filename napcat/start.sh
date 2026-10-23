#!/usr/bin/env bash
set -euo pipefail

# 先迁移本项目的网络地址，再使用上游启动器；不修改 QQ/NapCat 程序。
python3 /opt/ds-bot/configure.py /app/napcat/config
exec bash /app/entrypoint.sh
