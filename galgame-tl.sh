#!/usr/bin/env bash
# galgame-tl 启动器（桌面图标 / 命令行都走这个）
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PY="$HERE/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "找不到 venv: $PY" >&2
  echo "先跑: cd '$HERE' && uv venv -p 3.12 .venv && uv pip install -r requirements.txt" >&2
  exit 1
fi

# Qt 在 Wayland 下有时需要回退，让用户能用 GALGAME_TL_QT_PLATFORM 覆盖
if [ -n "${GALGAME_TL_QT_PLATFORM:-}" ]; then
  export QT_QPA_PLATFORM="$GALGAME_TL_QT_PLATFORM"
fi

exec "$PY" "$HERE/app.py" "$@"
