#!/usr/bin/env bash
# kick.sh —— 「继续」的落地实现。
#
# 你在对话框里发两个字「继续」，等价于跑这一条：
#     bash goal-daemon/动手.sh
# 它做四件事，然后打印一句结论（有活 / 没活，以及为什么）：
#   1. 清 STOP 文件（它只是刹车，不是开关，清了才能动）
#   2. 看队列：有没有 ready / in_progress
#   3. 没 ready 就试一次自动回流（受 goal.md 里的开关约束），把还能做的遗留项接上
#   4. 敲一下 launchd，等一拍，把落地的日志和状态打出来
#
# 它不是审批：不点确认、不改包状态，只是"把机制踢一下"。
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE="$DIR/goal-state"
LABEL="${GOAL_LABEL:-com.datong.goal-daemon}"
WAIT_S="${KICK_WAIT_S:-14}"

say() { printf '%s\n' "$*"; }
hr() { printf '%s\n' "────────────────────────────────────────────"; }

if [ ! -f "$STATE/queue.json" ]; then
  say "✗ 找不到 $STATE/queue.json，这个目录不是 goal-daemon 的状态目录。"
  exit 2
fi

# ---- 1) 刹车片 ----
if [ -f "$STATE/STOP" ]; then
  rm -f "$STATE/STOP"
  say "· 已清 STOP（它只是刹车，不清就动不了）"
fi

count() {
  python3 - "$STATE/queue.json" <<'PY'
import json, sys
q = json.load(open(sys.argv[1], encoding="utf-8"))
c = {}
for p in q.get("packages", []):
    c[p.get("status", "?")] = c.get(p.get("status", "?"), 0) + 1
print(json.dumps(c, ensure_ascii=False))
PY
}

before="$(count)"
say "· 队列现状：$before"

# ---- 2) 有没有现成的活 ----
has_work() {
  python3 - "$STATE/queue.json" <<'PY'
import json, sys
q = json.load(open(sys.argv[1], encoding="utf-8"))
print(any(p.get("status") in ("ready", "in_progress") for p in q.get("packages", [])))
PY
}

# ---- 3) 没活就试回流 ----
if [ "$(has_work)" != "True" ]; then
  say "· 队列里没有可执行包，试一次自动回流（看上一包汇报里还有没有能做的遗留）"
  if [ -x "$DIR/followup.py" ]; then
    followup_out="$("$DIR/followup.py" --apply 2>&1 | tail -4)"
  else
    followup_out="$(python3 "$DIR/followup.py" --apply 2>&1 | tail -4)"
  fi
  [ -n "$followup_out" ] && printf '%s\n' "$followup_out" | sed 's/^/    /'
  say "· 回流后：$(count)"
fi

# ---- 4) 敲一下 ----
if [ "$(has_work)" = "True" ]; then
  ready_id="$(python3 - "$STATE/queue.json" <<'PY'
import json, sys
q = json.load(open(sys.argv[1], encoding="utf-8"))
for p in q.get("packages", []):
    if p.get("status") == "ready":
        print(p["id"]); break
PY
)"
  say "· 有活：下一个是 ${ready_id:-（没有 ready 的，看下面是不是已经有包在跑）}"
else
  say "· 没有可派的活（等外部输入的那些在 blocked 台账里，派下去只会写一页'做不了'）"
fi

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1
  say "· 已敲守护进程（${LABEL}），等 ${WAIT_S}s 看落地"
else
  say "! 守护进程没注册到 launchd（${LABEL}）。先装：bash ${DIR}/install.sh"
fi

sleep "$WAIT_S"

hr
log_file="$(ls -t "$STATE"/logs/*.log 2>/dev/null | head -1)"
if [ -n "${log_file:-}" ]; then
  say "最近事件："
  tail -5 "$log_file" | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        print("  " + line); continue
    try:
        r = json.loads(line)
    except Exception:
        print("  " + line); continue
    ts = (r.get("ts") or "")[11:19]
    ev = r.get("event", "?")
    rest = {k: v for k, v in r.items() if k not in ("ts", "event")}
    print("  {}  {}  {}".format(ts, ev, rest if rest else ""))
'
fi

hr
lock="$STATE/lock.json"
if [ -f "$lock" ]; then
  python3 - "$lock" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print("在跑：{}  pid={}  开始={}".format(
    d.get("pkg_id") or d.get("pkg") or "?", d.get("pid"), d.get("started_at")))
PY
  say "结论：Loop 已经在干活，这一包交付后会自动接下一包。"
else
  say "结论：这一刻没有在跑的包。上面那行字就是原因——要么等外部输入，要么队列空了。"
fi
