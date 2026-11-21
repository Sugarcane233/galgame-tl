#!/usr/bin/env bash
# 批量收尾脚本模板：跑一轮 → 统计还缺多少 → 把状态写成一个文件
#
# 用法
# ----
#   ./batch_run.sh /path/to/GameDir [/path/to/out_dir]
#
# 适合挂成定时任务（cron / systemd timer），跑完自己写状态文件，
# 不用人盯着几万句翻译。缓存命中的句子会跳过，所以随时能重跑。
#
# 改这三行就够了：
TOOL="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
GAME="${1:?用法: $0 <游戏目录> [输出目录]}"
OUT="${2:-$HOME/下载/_galgame_tl}"

NAME="$(basename "$GAME")"
GLOSSARY="$OUT/$NAME.glossary.json"
CACHE="$OUT/$NAME.cache.json"
TEXT="$OUT/$NAME.text.json"
STATUS="$OUT/$NAME.status.txt"
LOCK="/tmp/galgame-tl_$(echo "$NAME" | tr -cd 'A-Za-z0-9._-').lock"
RUNLOG="/tmp/galgame-tl_$(echo "$NAME" | tr -cd 'A-Za-z0-9._-').run.log"

# 想换模型 / 关掉某道兜底，改这行
GTL_ARGS=(--model deepseek-v4.1-flash --batch 20 --concurrency 4)

cd "$TOOL" || exit 1
mkdir -p "$OUT"

# 防止两个实例同时跑（定时任务 + 手动 撞车会抢配额）
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "state=busy" > "$STATUS"
    echo "另一个实例正在跑，本次跳过"
    exit 0
fi

log() { echo "[$(date +%H:%M:%S)] $*"; }

# 0) 有别的 gtl 进程在跑就等着，别抢配额
for _ in $(seq 1 120); do
    pgrep -f "gtl run .*$(basename "$GAME")" >/dev/null || break
    sleep 10
done

log "开始跑批: $NAME"
START=$(date +%s)

# 1) 全量跑（缓存命中的直接跳过，所以这一步只补没翻过的）
ARGS=("$GAME" --out-dir "$OUT" "${GTL_ARGS[@]}")
[ -f "$GLOSSARY" ] && ARGS+=(--glossary "$GLOSSARY")
GTL_REEXEC=1 timeout 7200 .venv/bin/python gtl run "${ARGS[@]}" > "$RUNLOG" 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START ))

# 2) 从日志里抠统计
grab() { grep -oE "$1 [0-9]+" "$RUNLOG" 2>/dev/null | grep -oE "[0-9]+" | tail -1; }
TOTAL=$(grab "唯一句子")
PENDING=$(grab "待翻")
TRANSLATED=$(grab "译出")
DEFERRED=$(grab "未成功")
NVUSED=$(grab "兜底")
APPLIED=$(grab "写入")

# 3) 缓存里还剩多少没翻的（这是最准的口径）
MISSING=-1
if [ -f "$CACHE" ] && [ -f "$TEXT" ]; then
    MISSING=$("$TOOL/.venv/bin/python" - "$CACHE" "$TEXT" <<'PY'
import json, sys
try:
    cache = json.load(open(sys.argv[1], encoding="utf-8"))
    items = json.load(open(sys.argv[2], encoding="utf-8"))
    items = items["items"] if isinstance(items, dict) else items
    srcs = {it.get("src", "") for it in items if it.get("src")}
    print(len(srcs - set(cache)))
except Exception:
    print(-1)
PY
)
fi

{
    echo "state=done"
    echo "rc=$RC"
    echo "elapsed=$ELAPSED"
    echo "total=$TOTAL"
    echo "pending_before=$PENDING"
    echo "translated=$TRANSLATED"
    echo "deferred=$DEFERRED"
    echo "nv_fallback_calls=$NVUSED"
    echo "applied=$APPLIED"
    echo "still_missing=$MISSING"
    echo "finished_at=$(date '+%Y-%m-%d %H:%M:%S')"
} > "$STATUS"

log "完成 rc=$RC 耗时=${ELAPSED}s 译出=$TRANSLATED 未成功=$DEFERRED 仍缺=$MISSING"
log "状态已写入 $STATUS，完整日志 $RUNLOG"
