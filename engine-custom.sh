#!/bin/bash
# engine-custom.sh — 演示用自定义引擎：模拟 6 秒工作量（不联网，避开沙箱 HTTP 代理）
set -u
ID="${1:?pkg id required}"
S="${GOAL_STATE_DIR:?}"
WORK="$S/../work"
mkdir -p "$WORK"

echo "$(date '+%H:%M:%S') start $ID" >> "$WORK/ticks.log"
printf '{"pid":%s,"pkg_id":"%s","epoch":%s}\n' "$$" "$ID" "$(date +%s)" > "$S/heartbeat.json"

sleep "${DEMO_ENGINE_SLEEP:-6}"

echo "$(date '+%H:%M:%S') done  $ID" >> "$WORK/ticks.log"
mkdir -p "$S/pkg"
{
  echo "改动：ticks.log 追加两行（${ID}）"
  echo "验证：文件已写入"
  echo "未证点：无（演示用自定义引擎）"
  echo "下一步：待审"
} > "$S/pkg/$ID.last.md"
exit 0
