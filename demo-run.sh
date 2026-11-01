#!/bin/bash
# demo-run.sh — 真机 10 秒调度演示：mock 端点（6 秒延迟）+ 常驻 daemon loop + 3 个链式包
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
DEMO="$DIR/demo"
STATE="$DEMO/goal-state"
WORK="$DEMO/work"

mkdir -p "$STATE/logs" "$STATE/pkg" "$WORK"
rm -f "$STATE/STOP" "$DEMO"/*.pid
rm -rf "$WORK"/* "$STATE"/logs/* "$STATE/pkg"/*

cat > "$STATE/goal.md" <<'EOF'
OBJECTIVE: 10 秒调度真机演示
ENGINE: http
ENGINE_BASE_URL: http://127.0.0.1:18998/v1
ENGINE_MODEL: mock-model
ENGINE_API_KEY:
ENGINE_MAX_STEPS: 8
MAX_FAIL: 3
MAX_PKG_SECONDS: 3600
BUDGET_DAILY_TOKENS: none
NOTIFY: none
EOF

/usr/bin/python3 - "$STATE/queue.json" "$WORK" <<'PY'
import json, sys
q, work = sys.argv[1], sys.argv[2]
pkgs = []
for i in (1, 2, 3):
    pkgs.append({
        "id": "demo-%02d" % i,
        "title": "第 %d 个链式包" % i,
        "instruction": "在 ticks.log 追加一行当前时间",
        "workdir": work,
        "write_scope": [work],
        "checks": ["ticks.log 有新行"],
        "deps": [] if i == 1 else ["demo-%02d" % (i - 1)],
        "status": "pending",
        "fail_count": 0,
    })
json.dump({"packages": pkgs}, open(q, "w"), ensure_ascii=False, indent=2)
PY

# nohup：这两个进程必须比本脚本活得久。在 WorkBuddy 集成终端、IDE 任务终端这类
# 会被工具托管的地方，脚本一退出，它起的后台子进程常常被整组回收——现象是
# "演示没反应"：mock 和 daemon 一起消失，队列停在 in_progress。
nohup /usr/bin/python3 "$DEMO/mock_openai.py" > "$DEMO/mock.log" 2>&1 &
echo $! > "$DEMO/mock.pid"

# 等 mock 真的能连上再起 daemon。原来固定 sleep 1：python 冷启动慢一点、
# 或者上个演示的端口没退干净，第一拍就 502，看起来像引擎坏了。
ready=0
for _ in $(seq 1 50); do
  if /usr/bin/python3 - <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(0.3)
sys.exit(0 if s.connect_ex(("127.0.0.1", 18998)) == 0 else 1)
PY
  then ready=1; break; fi
  sleep 0.2
done
[ "$ready" = 1 ] || echo "警告：mock 端点 18998 起不来（看 demo/mock.log），第一拍会失败" >&2

# --state-dir 显式传：命令行里带上演示目录，demo-stop.sh 才能只杀演示进程，
# 不会连生产 daemon 一起 pkill 掉。
nohup /usr/bin/python3 "$DIR/goal-daemon.py" --loop --interval 10 --state-dir "$STATE" \
  > "$DEMO/daemon.out" 2>&1 &
echo $! > "$DEMO/daemon.pid"

echo "started: daemon=$(cat "$DEMO/daemon.pid") mock=$(cat "$DEMO/mock.pid")"
echo "state=$STATE work=$WORK"
