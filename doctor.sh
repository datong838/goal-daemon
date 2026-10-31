#!/bin/bash
# doctor.sh — 一条命令判断 goal-daemon 是否"开机即用"
#
#   ./doctor.sh          只检查，不改动任何东西
#   ./doctor.sh --fix    顺手补齐能自动补的（目录、队列、可执行位）
#   ./doctor.sh --probe  额外真实跑一次最小任务，确认引擎真能干活（会花一次极小调用）
#
# 输出：每条一行 OK / WARN / FAIL，末尾给结论。退出码 0=可运行，1=有 FAIL。
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=env.sh
[ -f "$DIR/env.sh" ] && . "$DIR/env.sh"
STATE="${GOAL_STATE_DIR:-$DIR/goal-state}"
LABEL="$(goal_active_label 2>/dev/null || printf '%s' "${GOAL_LABEL:-local.goal-daemon}")"
FIX=0
PROBE=0
for a in "$@"; do
  case "$a" in
    --fix) FIX=1 ;;
    --probe) PROBE=1 ;;
  esac
done

OK=0; WARN=0; FAIL=0
ok()   { printf '  OK    %s\n' "$1"; OK=$((OK+1)); }
warn() { printf '  WARN  %s\n' "$1"; WARN=$((WARN+1)); }
fail() { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }
hint() { printf '        修：%s\n' "$1"; }

echo "goal-daemon 自检  ($(date '+%Y-%m-%d %H:%M:%S'))"
echo "目录: $DIR"
echo "状态: $STATE"

# ---------------------------------------------------------------- 1 运行环境
echo
echo "[1] 运行环境"
if [ -x /usr/bin/python3 ]; then
  PYV="$(/usr/bin/python3 -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null)"
  case "$PYV" in
    3.9.*|3.1[0-9].*) ok "python3 ${PYV}（/usr/bin/python3）" ;;
    *) warn "python3 $PYV —— 脚本按 3.9+ 编写，更旧的解释器可能报语法错" ;;
  esac
else
  fail "找不到 /usr/bin/python3"
fi

for f in goal-daemon.py engine.sh; do
  [ -f "$DIR/$f" ] || fail "缺少 $f"
done
[ -x "$DIR/engine.sh" ] || { [ "$FIX" = "1" ] && chmod +x "$DIR/engine.sh" && ok "已补 engine.sh 可执行位" || warn "engine.sh 没有可执行位"; }
[ -f "$DIR/goal-daemon.py" ] && ok "main / engine 脚本就位"

# ---------------------------------------------------------------- 2 状态目录
echo
echo "[2] 状态目录"
for d in "$STATE" "$STATE/logs" "$STATE/pkg"; do
  if [ -d "$d" ]; then :; else
    [ "$FIX" = "1" ] && mkdir -p "$d" && ok "已创建 $d" || fail "缺少目录 $d"
  fi
done
if [ -w "$STATE" ]; then ok "可写：$STATE"; else fail "不可写：$STATE"; fi

# ---------------------------------------------------------------- 3 配置
echo
echo "[3] 配置 goal.md"
CFG="$STATE/goal.md"
if [ -f "$CFG" ]; then
  ENGINE="$(/usr/bin/grep -E '^ENGINE:' "$CFG" 2>/dev/null | head -1 | sed 's/^ENGINE:[[:space:]]*//;s/[[:space:]]*$//')"
  ENGINE="${ENGINE:-http}"
  ok "ENGINE: $ENGINE"
else
  fail "缺少 $CFG"; ENGINE=""
  if [ -f "$STATE/goal.md.template" ]; then
    hint "./install.sh（会从 goal.md.template 生成，再改里面的 OBJECTIVE）；只想先试跑就 cp goal-state/goal.md.template goal-state/goal.md"
  fi
fi

CB="${CODEBUDDY_BIN:-$(goal_detect_cli 2>/dev/null || true)}"
URL="$(/usr/bin/grep -E '^ENGINE_BASE_URL:' "$CFG" 2>/dev/null | head -1 | sed 's/^ENGINE_BASE_URL:[[:space:]]*//;s/[[:space:]]*$//')"
NODE="$(goal_detect_node 2>/dev/null || true)"

# --probe 时会真实跑一次；这里只做只读探测，口径与 engine.sh 的 auto 选择保持一致
cb_ready=0
[ -f "$CB" ] || cb_ready=1
[ -n "${NODE:-}" ] || cb_ready=1
if [ -f "$STATE/.need-login" ] && [ -z "$(find "$STATE/.need-login" -mmin +120 2>/dev/null)" ]; then cb_ready=1; fi

http_ready=0
if [ -n "${URL:-}" ]; then
  /usr/bin/python3 - "$URL" <<'PYEOF' >/dev/null 2>&1 || http_ready=1
import socket, sys
from urllib.parse import urlparse
u = urlparse(sys.argv[1])
s = socket.socket(); s.settimeout(2)
try: s.connect((u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80)))
finally: s.close()
PYEOF
else
  http_ready=1
fi

show_cb() {
  [ -f "$CB" ] && ok "codebuddy: $CB" || fail "codebuddy 不存在：$CB"
  [ -n "${NODE:-}" ] && ok "node: $NODE" || fail "找不到 node（codebuddy 是 node 脚本）"
  # CLI 的真实凭据是 $HOME/.credentials.json（cli 源码 getCredentialsPath）。
  # 注意：$STATE/.need-login 只是"上次探测到未登录"的退避标记，
  # 没有这个标记 ≠ 已登录 —— 别把"无标记"当成登录成功。
  if [ -f "$HOME/.credentials.json" ]; then
    ok "已登录（凭据：~/.credentials.json）"
  else
    warn "未登录 —— daemon 会常驻，但不会执行 goal（引擎返回 92，包保持 ready、不熔断）"
    hint "cd $DIR && ./login.sh  进 TUI 执行 /login（同一个 WorkBuddy 账号，一次即可）"
  fi
  if [ -f "$STATE/.need-login" ]; then
    warn "存在退避标记 .need-login：2 小时内不再尝试启动 CLI（登录后 ./login.sh 会自动清除）"
  fi
  [ -s "$STATE/session.id" ] && ok "会话 id：$(cut -c1-8 < "$STATE/session.id" 2>/dev/null)…（续接同一会话）"
  return 0
}

show_wbrun() {
  local port pw
  # wbrun 免登录，只关心"网关端口 + 密码能不能用"
  if [ ! -s "$STATE/gateway.port" ] || [ ! -s "$STATE/gateway.token" ]; then
    warn "网关未初始化 —— 首次派活时引擎会自动发现端口并提取密码"
    hint "若一直不派活：确认 WorkBuddy 桌面端在运行，再跑 ./doctor.sh --probe"
    return 0
  fi
  port="$(tr -cd '0-9' < "$STATE/gateway.port" 2>/dev/null)"
  pw="$(head -1 "$STATE/gateway.token" 2>/dev/null | tr -d '\r\n ')"
  if [ -n "${port:-}" ] && curl -s -m 2 --noproxy '*' -o /dev/null \
       -H "Authorization: Bearer $pw" "http://127.0.0.1:$port/api/v1/status" 2>/dev/null; then
    ok "网关 127.0.0.1:$port 可达（免登录，复用桌面端账号）"
  else
    warn "网关 127.0.0.1:${port:-?} 不可达 —— 端口每次会话会变，下个 tick 会自动重扫"
    hint "持续不可达：重启 WorkBuddy 桌面端后跑 launchctl kickstart -k gui/$(id -u)/$LABEL"
    hint "仍不行：按 使用说明.md 9.4 重取 gateway.token（密码值以下划线开头，别用 \"\$_xxx\" 语法）"
  fi
  [ -s "$STATE/.need-login" ] && warn "存在历史 .need-login 标记（对 wbrun 无影响，换回 codebuddy 前需先登录）"
  return 0
}

case "$ENGINE" in
  wbrun)
    show_wbrun
    ;;
  auto)
    if [ "$cb_ready" = "0" ]; then
      ok "auto 会走 codebuddy（优先）"
      show_cb
    elif [ "$http_ready" = "0" ]; then
      ok "auto 会走 http（codebuddy 暂不可用）"
      ok "端点：$URL"
      warn "codebuddy 侧不可用；当前靠自建端点干活"
    else
      warn "auto 当前没有可用后端 —— daemon 会常驻等待，包保持 ready，不会熔断"
      hint "二选一：./login.sh 登录；或把 ENGINE_BASE_URL 指向一个已启动的网关"
    fi
    ;;
  codebuddy)
    show_cb
    ;;
  http)
    if [ -n "${URL:-}" ]; then ok "端点：$URL"; else fail "ENGINE_BASE_URL 为空"; hint "填 $CFG 里的 ENGINE_BASE_URL / ENGINE_MODEL / ENGINE_API_KEY"; fi
    if [ "$http_ready" = "0" ]; then ok "端点 TCP 可达"; else warn "端点 TCP 不通（服务没起？）"; hint "先启动你的 new-api/sub2api，或改用 ENGINE: auto"; fi
    ;;
  custom)
    [ -x "$DIR/engine-custom.sh" ] && ok "custom 引擎脚本存在" || fail "ENGINE: custom 但 engine-custom.sh 不存在或不可执行"
    ;;
  *)
    fail "未知 ENGINE: $ENGINE"
    ;;
esac

# ---------------------------------------------------------------- 4 队列
echo
echo "[4] 队列"
Q="$STATE/queue.json"
if [ ! -f "$Q" ]; then
  if [ "$FIX" = "1" ] && [ -f "$STATE/queue.example.json" ]; then
    cp "$STATE/queue.example.json" "$Q" && ok "已从 queue.example.json 初始化"
  else
    fail "缺少 queue.json"; hint "./doctor.sh --fix 或 cp queue.example.json queue.json"
  fi
else
  /usr/bin/python3 - "$Q" <<'PY'
import json,sys
try:
    q=json.load(open(sys.argv[1],encoding="utf-8"))
except Exception as e:
    print("  FAIL  队列无法解析：%s" % e); sys.exit(2)
pk=q.get("packages",[])
from collections import Counter
c=Counter(p.get("status","?") for p in pk)
parts=" ".join("%s=%d"%(k,v) for k,v in sorted(c.items()))
print("  OK    队列可解析：共 %d 包  %s" % (len(pk), parts))
runnable=[p["id"] for p in pk if p.get("status") in ("ready","pending")]
if not runnable:
    print("  WARN  没有可跑的包 —— daemon 会一直 idle（空转不算故障）")
    print("        修：往 queue.json 加包，或 python3 - < 自己写一个")
PY
  [ $? -eq 2 ] && FAIL=$((FAIL+1))
fi

if [ -f "$STATE/STOP" ]; then
  warn "存在 STOP 文件 —— daemon 会立即停手"
  hint "rm $STATE/STOP 恢复"
else
  ok "无 STOP 文件"
fi

# ---------------------------------------------------------------- 5 launchd
echo
echo "[5] 开机自启（launchd）"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ -f "$PLIST" ]; then ok "已安装：$PLIST"; else warn "未安装 —— 重启后不会自动跑"; hint "cd $DIR && ./install.sh"; fi

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  PID="$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | /usr/bin/awk '/^\tpid = /{print $3}' | head -1)"
  if [ -n "${PID:-}" ]; then ok "launchd 正在运行（pid ${PID}）"; else warn "已注册但当前未运行（launchd 会自动拉起，或刚被卸载）"; fi
else
  warn "未注册到 launchd —— 重启后不会自动跑"
  # launchctl list 为 0 条 = 当前终端不在你的 Aqua 登录会话里（WorkBuddy 集成终端就是这样），
  # 这种环境下 bootstrap 必然报 "Input/output error"，换终端才能解决。
  if [ "$(launchctl list 2>/dev/null | wc -l | tr -d ' ')" -lt 5 ]; then
    hint "当前终端看不到你的 launchd 服务：请用 macOS 自带的「终端」App 跑 ./install.sh"
  else
    hint "cd $DIR && ./install.sh"
  fi
fi

# ---------------------------------------------------------------- 6 心跳
echo
echo "[6] tick 新鲜度"
LAST="$(/usr/bin/python3 - "$STATE" <<'PY'
import glob,json,os,sys,datetime
state=sys.argv[1]
best=None
for f in glob.glob(os.path.join(state,"logs","2*.log")):
    try:
        with open(f,encoding="utf-8",errors="replace") as fh:
            for line in fh:
                line=line.strip()
                if not line.startswith("{"): continue
                try: r=json.loads(line)
                except Exception: continue
                ts=r.get("ts")
                if ts and (best is None or ts>best[0]): best=(ts,r.get("event",""),r.get("pkg_id",""))
    except Exception: pass
if best:
    try:
        t=datetime.datetime.fromisoformat(best[0].replace("Z","+00:00"))
        if t.tzinfo: t=t.astimezone().replace(tzinfo=None)
        age=(datetime.datetime.now()-t).total_seconds()
    except Exception:
        age=-1
    print("%d|%s|%s" % (age,best[1],best[2]))
PY
)"
if [ -n "${LAST:-}" ]; then
  AGE="${LAST%%|*}"; REST="${LAST#*|}"; EV="${REST%%|*}"; PKG="${REST#*|}"
  if [ "$AGE" -lt 0 ]; then ok "最近事件：$EV $PKG"
  elif [ "$AGE" -le 25 ]; then ok "最近 ${AGE}s 前：$EV ${PKG}（节拍正常）"
  elif [ "$AGE" -le 120 ]; then warn "最近 ${AGE}s 前：$EV ${PKG}（偏慢，可能刚启动）"
  else warn "最近 ${AGE}s 前：$EV ${PKG}（daemon 可能没跑）"; hint "launchctl kickstart -k gui/$(id -u)/$LABEL"; fi
else
  warn "还没有 tick 日志（全新安装属正常）"
fi

# ---------------------------------------------------------------- 7 真实探测
if [ "$PROBE" = "1" ]; then
  echo
  echo "[7] 真实探测（会花一次极小调用）"
  P="$DIR/.doctor-probe"
  rm -rf "$P"; mkdir -p "$P/goal-state/pkg" "$P/goal-state/logs" "$P/work"
  [ -f "$CFG" ] && cp "$CFG" "$P/goal-state/goal.md"
  /usr/bin/python3 - "$P/goal-state/queue.json" "$P/work" <<'PYEOF'
import json, sys
q, work = sys.argv[1], sys.argv[2]
json.dump({"packages": [{"id": "doctor-probe", "title": "自检探针",
    "instruction": "只回复两个字：收到", "workdir": work, "write_scope": [work],
    "checks": [], "deps": [], "status": "ready", "fail_count": 0}]},
    open(q, "w", encoding="utf-8"), ensure_ascii=False)
PYEOF
  GOAL_STATE_DIR="$P/goal-state" PKG_WORKDIR="$P/work" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    /bin/bash "$DIR/engine.sh" doctor-probe >/dev/null 2>&1
  PRC=$?
  case "$PRC" in
    0)  ok "真实跑通（exit=0），引擎能干活"
        [ -f "$P/goal-state/pkg/doctor-probe.last.md" ] && printf '        产出：%s\n' "$(head -c 60 "$P/goal-state/pkg/doctor-probe.last.md" | tr '\n' ' ')" ;;
    92) warn "未登录（exit=92）—— 去 ./login.sh 登录一次即可"; hint "登录后 ./doctor.sh --probe 复查" ;;
    93) warn "无可用后端（exit=93）—— codebuddy 未登录且 HTTP 端点不通" ;;
    *)  fail "探测失败（exit=${PRC}）"; hint "看 $P/goal-state/logs/engine-doctor-probe.log" ;;
  esac
  if [ "$PROBE" = "1" ]; then
    echo "        引擎日志：$P/goal-state/logs/engine-doctor-probe.log"
    echo "        （保留该目录便于排查，删除：rm -rf ${P}）"
  fi
fi

# ---------------------------------------------------------------- 结论
echo
if [ "$FAIL" -eq 0 ]; then
  if [ "$WARN" -eq 0 ]; then echo "结论：可以开机即用（0 问题）"
  else echo "结论：可运行，但有 $WARN 项待处理（见上面 WARN）"; fi
  echo "启动/重启用：./install.sh      看状态用：./status.sh"
  exit 0
else
  echo "结论：有 $FAIL 项阻塞，暂不能跑（见上面 FAIL）"
  exit 1
fi
