#!/usr/bin/env bash
# The Last Sovereign 汉化收尾脚本：翻完 → 补漏（NVIDIA 兜底）→ 回填 → 报告
#
# 为什么要有这个
# --------------
# 全量跑一遍要十几分钟，跑完还有一批「主模型过不去」的句子（约 3%，R18 露骨
# 描写）需要走 NVIDIA 重跑，然后才能 apply 回 rvdata2。这一串不能靠人盯：
# 挂成定时任务，跑完自动报告状态。
#
# 幂等：任何时候都能重跑。缓存命中会跳过已翻的句子。
set -uo pipefail

TOOL="/home/mrsugarcane/文档/deepseek-harness/default-workspace/galgame-tl"
GAME="/home/mrsugarcane/下载/_galgame_tl/The_Last_Sovereign_1.2.2"
OUT="/home/mrsugarcane/下载/_galgame_tl"
GLOSSARY="$OUT/The_Last_Sovereign_1.2.2.glossary.json"
CACHE="$OUT/The_Last_Sovereign_1.2.2.cache.json"
TEXT="$OUT/The_Last_Sovereign_1.2.2.text.json"
STATUS="/tmp/tls_status.txt"
LOCK="/tmp/tls_pipeline.lock"

cd "$TOOL" || exit 1

# 防止两个实例同时跑（定时任务 + 手动）
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "state=busy" > "$STATUS"
    echo "另一个实例正在跑，本次跳过"
    exit 0
fi

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a /tmp/tls_finish.log; }

# 0) 还有别的 gtl 进程在跑就等着，别抢配额
for _ in $(seq 1 120); do
    if ! pgrep -f "gtl run .*The_Last_Sovereign" >/dev/null; then
        break
    fi
    sleep 10
done

log "开始收尾跑批"
START=$(date +%s)

# 1) 全量跑（缓存命中的直接跳过，所以这一步只补没翻过的）
GTL_REEXEC=1 timeout 3600 .venv/bin/python gtl run "$GAME" \
    --base-url workbuddy: --model deepseek-v4.1-flash \
    --glossary "$GLOSSARY" --batch 20 --concurrency 4 \
    > /tmp/tls_run.log 2>&1
RC1=$?
END=$(date +%s)
ELAPSED=$((END - START))

TOTAL=$(grep -oE "唯一句子 [0-9]+" /tmp/tls_run.log | grep -oE "[0-9]+" | tail -1)
PENDING=$(grep -oE "待翻 [0-9]+" /tmp/tls_run.log | grep -oE "[0-9]+" | tail -1)
TRANSLATED=$(grep -oE "译出 [0-9]+" /tmp/tls_run.log | grep -oE "[0-9]+" | tail -1)
DEFERRED=$(grep -oE "未成功 [0-9]+" /tmp/tls_run.log | grep -oE "[0-9]+" | tail -1)
NVUSED=$(grep -oE "兜底 [0-9]+ 句" /tmp/tls_run.log | grep -oE "[0-9]+" | tail -1)
APPLIED=$(grep -oE "写入 [0-9]+" /tmp/tls_run.log | grep -oE "[0-9]+" | tail -1)

# 缓存里还剩多少没翻的
MISSING=0
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
    echo "rc=$RC1"
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

log "完成 rc=$RC1 耗时=${ELAPSED}s 译出=$TRANSLATED 未成功=$DEFERRED 仍缺=$MISSING"
