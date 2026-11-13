#!/bin/bash
# 双击打开 MiniMax H3 工作台；关掉这个终端窗口就停止。
cd "$(dirname "$0")" || exit 1
xattr -dr com.apple.quarantine . 2>/dev/null   # 从网上或隔空投送拿到的包，先去掉系统的隔离标记，里面的程序才能运行
export H3_DATA="$PWD/data"
export PATH="$PWD/runtime/ffmpeg:$PATH"
export PYTHONPATH="$PWD/runtime/lib"
export PYTHONDONTWRITEBYTECODE=1
exec "$PWD/runtime/python/bin/python3" "$PWD/app/hf.py" "$@"
