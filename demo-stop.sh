#!/bin/bash
# demo-stop.sh — 停止演示：先落 STOP，再终止 daemon 与 mock，最后清理残留引擎进程
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
DEMO="$DIR/demo"
STATE="$DEMO/goal-state"

touch "$STATE/STOP"

for f in daemon.pid mock.pid; do
  if [ -f "$DEMO/$f" ]; then
    pid=$(cat "$DEMO/$f")
    kill "$pid" 2>/dev/null && echo "已停止 $f ($pid)" || echo "$f 已不在"
    rm -f "$DEMO/$f"
  fi
done

# 兜底：按命令行特征清理（仅本演示目录）
pkill -f "goal-daemon.py --loop" 2>/dev/null && echo "清理残留 daemon" || true
pkill -f "demo/mock_openai.py" 2>/dev/null && echo "清理残留 mock" || true

echo "--- 最终队列状态 ---"
/usr/bin/python3 - "$STATE/queue.json" <<'PY'
import json, sys
q = json.load(open(sys.argv[1]))
for p in q["packages"]:
    print(p["id"], p["status"], "fail=%s" % p.get("fail_count"))
PY
