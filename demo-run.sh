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

/usr/bin/python3 "$DEMO/mock_openai.py" > "$DEMO/mock.log" 2>&1 &
echo $! > "$DEMO/mock.pid"
sleep 1

GOAL_STATE_DIR="$STATE" /usr/bin/python3 "$DIR/goal-daemon.py" --loop --interval 10 \
  > "$DEMO/daemon.out" 2>&1 &
echo $! > "$DEMO/daemon.pid"

echo "started: daemon=$(cat "$DEMO/daemon.pid") mock=$(cat "$DEMO/mock.pid")"
echo "state=$STATE work=$WORK"
