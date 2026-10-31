#!/bin/bash
# status.sh — 一眼看清循环当前状态（只读）
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=env.sh
[ -f "$DIR/env.sh" ] && . "$DIR/env.sh"
STATE="${GOAL_STATE_DIR:-$DIR/goal-state}"
LABEL="$(goal_active_label 2>/dev/null || printf '%s' "${GOAL_LABEL:-local.goal-daemon}")"

echo "== launchd =="
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "已注册（10s tick）"
else
  echo "未注册（未安装或已卸载）"
fi

echo
echo "== STOP =="
[ -f "$STATE/STOP" ] && echo "存在 -> 已停机（删除 $STATE/STOP 即恢复）" || echo "无"

echo
echo "== 引擎 =="
ENGINE_TYPE=$(grep -E '^[[:space:]]*ENGINE[[:space:]]*:' "$STATE/goal.md" 2>/dev/null | head -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
echo "ENGINE: ${ENGINE_TYPE:-未设置}"

case "${ENGINE_TYPE:-}" in
  wbrun|auto)
    # wbrun 免登录，只关心网关能不能连上
    if [ -f "$STATE/gateway.port" ] && [ -f "$STATE/gateway.token" ]; then
      GP="$(tr -cd '0-9' < "$STATE/gateway.port" 2>/dev/null)"
      if [ -n "${GP:-}" ] && curl -s -m 2 --noproxy '*' -o /dev/null \
           -H "Authorization: Bearer $(head -1 "$STATE/gateway.token" | tr -d '\r\n ')" \
           "http://127.0.0.1:$GP/api/v1/status" 2>/dev/null; then
        echo "网关: 127.0.0.1:$GP 可达（免登录，复用桌面端账号）"
      else
        echo "网关: 端口 $GP 暂不可达 —— 下个 tick 会自动重扫；持续失败请重取密码（见 使用说明 9.4）"
      fi
    else
      echo "网关: 未初始化 —— 首次派活时引擎会自动发现端口并提取密码"
    fi
    ;;
esac

if [ -f "$STATE/.need-login" ] && [ "${ENGINE_TYPE:-}" != "wbrun" ]; then
  echo "！未登录：codebuddy 需先跑 ./login.sh —— daemon 已暂停派活，且不计失败"
fi
if [ "${ENGINE_TYPE:-}" = "wbrun" ] && [ -f "$STATE/.need-login" ]; then
  echo "（存在历史未登录标记，对 wbrun 无影响；换回 codebuddy 前需先登录）"
fi
if [ -f "$STATE/session.id" ]; then
  echo "session: $(tr -cd '0-9a-zA-Z-' < "$STATE/session.id" 2>/dev/null)"
fi

echo
echo "== 运行锁 =="
if [ -f "$STATE/lock.json" ]; then
  PID=$(/usr/bin/python3 -c "import json;print(json.load(open('$STATE/lock.json'))['pid'])" 2>/dev/null)
  PKG=$(/usr/bin/python3 -c "import json;print(json.load(open('$STATE/lock.json'))['pkg_id'])" 2>/dev/null)
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    echo "运行中：${PKG}（pid ${PID}）"
  else
    echo "锁存在但进程已退出：${PKG}（下一次 tick 会收包）"
  fi
else
  echo "空闲"
fi

echo
echo "== 队列 =="
/usr/bin/python3 - "$STATE/queue.json" "$STATE/goal.md" <<'PY'
import json, sys, collections, time
q = json.load(open(sys.argv[1], encoding="utf-8"))
pk = q.get("packages", [])
c = collections.Counter(p.get("status") for p in pk)
try:
    txt = open(sys.argv[2], encoding="utf-8").read()
except OSError:
    txt = ""
print("包总数:", sum(c.values()), dict(c))
for p in pk:
    if p.get("status") in ("in_progress", "ready", "blocked", "delivered", "held"):
        extra = ""
        if p.get("status") == "held":
            extra = f"  🔒受保护根内，拒派：{p.get('held_root','')}"
        wu = float(p.get("wait_until") or 0)
        if wu > time.time():
            extra = f"  ⏳等后端 剩 {int(wu - time.time())}s 重试（第 {p.get('wait_count',0)} 次）"
        elif p.get("wait_count"):
            extra = f"  后端曾不可用 {p.get('wait_count')} 次，现已恢复"
        print(f"  - {p['id']} [{p['status']}] fail={p.get('fail_count',0)} {p.get('title','')}{extra}")
# 受保护根：配了才显示，没配不刷屏
for l in txt.splitlines():
    if l.strip().startswith("#") or ":" not in l:
        continue
    if l.split(":", 1)[0].strip() == "PROTECTED_ROOTS":
        v = l.split(":", 1)[1].strip()
        if v and v.lower() not in ("none", "off", "-"):
            print(f"\n  🔒 受保护根（这些路径下的包一律拒派）：{v}")
        break
# 只有显式开了人工审批，才有"待审"这回事
need_review = any(
    l.split(":", 1)[0].strip() == "FOLLOWUP_REQUIRE_DONE"
    and l.split(":", 1)[1].strip().lower() == "on"
    for l in txt.splitlines() if ":" in l and not l.strip().startswith("#")
)
if c.get("delivered") and need_review:
    print(f"\n  有 {c['delivered']} 个包已交付待审：./review.sh 查看，./review.sh <id> done 放行")
PY

echo
echo "== 回流（Loop 闭环） =="
FU=$(grep -E '^[[:space:]]*FOLLOWUP[[:space:]]*:' "$STATE/goal.md" 2>/dev/null | head -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
echo "FOLLOWUP: ${FU:-off（默认，纯批处理）}"
if [ -x "$DIR/followup.py" ]; then
  "$DIR/followup.py" --state-dir "$STATE" --list 2>/dev/null | sed 's/^/  /'
fi
if [ "${FU:-off}" = "on" ]; then
  RQ=$(grep -E '^[[:space:]]*FOLLOWUP_REQUIRE_DONE[[:space:]]*:' "$STATE/goal.md" 2>/dev/null | head -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
  MD=$(grep -E '^[[:space:]]*FOLLOWUP_MAX_DEPTH[[:space:]]*:' "$STATE/goal.md" 2>/dev/null | head -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
  if [ "${RQ:-off}" = "on" ]; then
    echo "  放行：需人工审（每包 ./review.sh <id> done 才推下一步）"
  else
    echo "  放行：免审（引擎交付即 done，下一包直接排）"
  fi
  case "${MD:-0}" in
    0|"") echo "  层数：不限（一直 Loop；停下靠 no_progress / clean / STOP / MAX_FAIL）" ;;
    *)    echo "  层数：最多 ${MD} 层" ;;
  esac
  PK=$(grep -E '^[[:space:]]*FOLLOWUP_PARK[[:space:]]*:' "$STATE/goal.md" 2>/dev/null | head -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
  if [ "${PK:-on}" = "off" ]; then
    echo "  做不动的项：照派（会拿一个预算周期换一页「做不了」）"
  else
    echo "  做不动的项：记账不派（要人/真机/上游/凭证/拍板 → goal-state/blocked.md）"
  fi
  echo "  某条已修好但不想改历史汇报：在 pkg/<id>.recheck.md 写一行 RESOLVED: <原话>"
else
  echo "  关闭中：队列跑完就 idle，汇报里的「仍未修」不会自己变成下一包"
fi

echo
echo "== 最近 12 条日志 =="
tail -12 "$(ls -t "$STATE"/logs/2[0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].log 2>/dev/null | head -1)" 2>/dev/null || echo "（暂无日志）"
