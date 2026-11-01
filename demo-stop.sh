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

# 兜底：按命令行特征清理 —— 必须带上演示自己的 --state-dir。
# 原来写的是 pkill -f "goal-daemon.py --loop"，而 launchd 装的正是
# `goal-daemon.py --loop --interval 10`：在已安装的机器上跑一次本脚本，
# 生产的守护进程会被一起杀掉（虽然 launchd 会拉回来，但那一刻在跑的包就断了）。
pkill -f "goal-daemon.py --loop --interval 10 --state-dir $STATE" 2>/dev/null && echo "清理残留 daemon" || true
pkill -f "demo/mock_openai.py" 2>/dev/null && echo "清理残留 mock" || true

# 复位：演示进程被 kill 掉时，包会停在 in_progress。不还原的话下次 demo-run
# 会因为"有包在跑"而一直不派发，看起来像卡死。
if [ -f "$STATE/queue.json" ]; then
  /usr/bin/python3 - "$STATE/queue.json" <<'PY'
import json, sys
p = sys.argv[1]
q = json.load(open(p))
changed = 0
for pkg in q.get("packages", []):
    if pkg.get("status") == "in_progress":
        pkg["status"] = "pending"
        changed += 1
if changed:
    json.dump(q, open(p, "w"), ensure_ascii=False, indent=2)
    print("已把 %d 个 in_progress 复位为 pending" % changed)
PY
fi

echo "--- 最终队列状态 ---"
/usr/bin/python3 - "$STATE/queue.json" <<'PY'
import json, sys
q = json.load(open(sys.argv[1]))
for p in q["packages"]:
    print(p["id"], p["status"], "fail=%s" % p.get("fail_count"))
PY
