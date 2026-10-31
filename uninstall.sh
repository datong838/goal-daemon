#!/bin/bash
# uninstall.sh — 卸载 launchd 任务（不删除 goal-state 与日志）
#
# 会把当前服务名与所有历史服务名一起收掉，避免换名后残留两个实例抢同一个队列。
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=env.sh
[ -f "$DIR/env.sh" ] && . "$DIR/env.sh"

LABEL="${GOAL_LABEL:-local.goal-daemon}"

stopped=0
for L in "$LABEL" $LEGACY_LABELS; do
  [ -z "${L:-}" ] && continue
  PLIST_DST="$HOME/Library/LaunchAgents/$L.plist"
  if launchctl print "gui/$(id -u)/$L" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/$L" || true
    echo "已停止 $L"
    stopped=1
  fi
  if [ -f "$PLIST_DST" ]; then
    rm -f "$PLIST_DST"
    echo "已移除 plist：$L.plist"
  fi
done

if [ "$stopped" -eq 0 ]; then
  echo "没有发现正在运行的服务（已检查：$LABEL $LEGACY_LABELS）"
fi

echo "goal-state 与 logs 保留未删（要彻底清掉请手动删除 $DIR/goal-state）"
