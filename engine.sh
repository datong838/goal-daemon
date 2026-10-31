#!/bin/bash
# engine.sh — 引擎适配器外壳：准备任务、维护心跳、分派后端、回收退出码
#
# 契约：engine.sh <package_id>
#   环境：GOAL_STATE_DIR / PKG_WORKDIR / PKG_TOKEN_BUDGET
#   输出：pkg/<id>.exit（退出码）、<id>.last.md（最终回复）
#
# 后端由 goal.md 的 ENGINE 决定：
#   ENGINE: codebuddy -> WorkBuddy 自带内核 CLI（同一模型/工具/技能，可 --resume 续同一会话）
#   ENGINE: http      -> engine-http.py（自研 agent 循环，任意 OpenAI 兼容端点）
#   ENGINE: custom    -> engine-custom.sh（你自己实现）
#
# 退出码约定：0 成功；92 未登录（需人工 /login）；91 codebuddy 不可执行；其他为后端原始失败码。
#
# 自定义引擎只需：读 <id>.task.md，干活，把最终汇报写 <id>.last.md，退出码 0/非 0。

set -u

STATE="${GOAL_STATE_DIR:?GOAL_STATE_DIR required}"
ID="${1:?package id required}"
WORKDIR="${PKG_WORKDIR:?PKG_WORKDIR required}"
DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=env.sh
[ -f "$DIR/env.sh" ] && . "$DIR/env.sh"

PKG_DIR="$STATE/pkg"
TASK="$PKG_DIR/$ID.task.md"
EXITF="$PKG_DIR/$ID.exit"
HB="$STATE/heartbeat.json"
ENGINE_LOG="$STATE/logs/engine-$ID.log"

mkdir -p "$PKG_DIR" "$STATE/logs"
rm -f "$EXITF"

# 读取 goal.md 的 "KEY: value"（缺省回落到第二参数）
cfg_get() {
  local key="$1" def="${2:-}" v=""
  if [ -f "$STATE/goal.md" ]; then
    # 只切到第一个冒号：URL 里的 "http://host:port" 若用贪婪匹配会被截成 "port/path"
    v=$(grep -E "^[[:space:]]*${key}[[:space:]]*:" "$STATE/goal.md" | head -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
  fi
  if [ -n "${v:-}" ]; then printf '%s' "$v"; else printf '%s' "$def"; fi
}

# 解析 node 解释器：codebuddy 是 `#!/usr/bin/env node` 脚本，launchd 的 PATH 里没有 node
resolve_node() {
  local n v
  n="${CODEBUDDY_NODE_BIN:-$(cfg_get CODEBUDDY_NODE_BIN '')}"
  if [ -n "${n:-}" ] && [ -x "$n" ]; then printf '%s' "$n"; return 0; fi
  n="$(command -v node 2>/dev/null || true)"
  if [ -n "${n:-}" ] && [ -x "$n" ]; then printf '%s' "$n"; return 0; fi
  for v in "$HOME"/.workbuddy/binaries/node/versions/*/bin/node; do
    if [ -x "$v" ]; then printf '%s' "$v"; return 0; fi
  done
  return 1
}

# 1) 读取引擎类型（goal.md 的 ENGINE: xxx）
ENGINE_TYPE="$(cfg_get ENGINE http)"

# 2) 生成单包任务文件（不含全局 goal）
if [ ! -f "$TASK" ]; then
  /usr/bin/python3 - "$STATE/queue.json" "$ID" "$TASK" "$WORKDIR" <<'PY'
import json, sys
qpath, pid, out, workdir = sys.argv[1:5]
q = json.load(open(qpath, encoding="utf-8"))
pkg = next((p for p in q.get("packages", []) if p.get("id") == pid), None)
if pkg is None:
    sys.stderr.write("package %s not found\n" % pid); sys.exit(2)
body = (pkg.get("instruction") or "").strip() or "(未提供 instruction)"
writes = pkg.get("write_scope") or [workdir]
checks = pkg.get("checks") or []
text = "# 任务包 %s\n\n## 目标\n%s\n\n## 工作目录\n%s\n\n## 允许写入\n%s\n\n## 必须自验\n%s\n\n## 交付格式\n结尾如实汇报：改动文件清单 / 已跑验证与结果 / 未证点 / 建议下一步。\n不得提交 Git；不得修改允许范围外的文件。\n" % (
    pid, body, workdir,
    "\n".join("- " + w for w in writes),
    "\n".join("- " + c for c in checks) if checks else "- 自跑最小验证，并列未证点",
)
open(out, "w", encoding="utf-8").write(text)
PY
  if [ ! -f "$TASK" ]; then
    echo "failed to build task file for $ID" >&2
    echo 90 > "$EXITF"; exit 90
  fi
fi

# 3) 心跳（每 15 秒），供 daemon 判"运行中"
hb_stop=0
(
  while [ "$hb_stop" -eq 0 ]; do
    printf '{"pid":%s,"pkg_id":"%s","epoch":%s}\n' "$$" "$ID" "$(date +%s)" > "$HB.tmp" 2>/dev/null \
      && mv "$HB.tmp" "$HB" 2>/dev/null
    sleep 15
  done
) &
HB_PID=$!

cleanup() {
  hb_stop=1
  kill "$HB_PID" 2>/dev/null
  wait "$HB_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

echo "[engine] pkg=$ID type=$ENGINE_TYPE workdir=$WORKDIR start=$(date -u +%FT%TZ)" >> "$ENGINE_LOG"

# ---- 后端：codebuddy（WorkBuddy 自带内核 CLI）----
run_codebuddy() {
  local cb_bin port pmode tools sess sess_file mark out_json err_log prompt cb_rc node_bin cb_script
  # CLI 位置不写死：CODEBUDDY_BIN → 由 WorkBuddy.app 位置派生 → 找不到就报 91
  cb_script="${CODEBUDDY_BIN:-$(cfg_get CODEBUDDY_BIN '')}"
  if [ -z "${cb_script:-}" ]; then
    cb_script="$(goal_detect_cli 2>/dev/null || true)"
  fi
  if [ -z "${cb_script:-}" ] || [ ! -f "$cb_script" ]; then
    echo "[engine] 找不到 codebuddy CLI：${cb_script:-<未探测到>}" >> "$ENGINE_LOG"
    echo "[engine] 提示：设置 WORKBUDDY_APP 或 CODEBUDDY_BIN，或改用 ENGINE: wbrun（免安装）" >> "$ENGINE_LOG"
    return 91
  fi
  cb_bin="$cb_script"

  # 未登录退避：上次已探测到未登录就在 2 小时内不再 spawn CLI。
  # 否则固定 ENGINE: codebuddy 时，每 10 秒 tick 都会白启一个 node 进程（约 1~2 秒 CPU）。
  login_mark="$STATE/.need-login"
  # 凭据一出现就自动解除退避，不需要用户再手工清标记（TUI 里 Ctrl+C 退出也可能跳过清理）
  if [ -f "$login_mark" ] && [ -f "$HOME/.credentials.json" ]; then
    rm -f "$login_mark"
    echo "[engine] 检测到 ~/.credentials.json，已解除未登录退避" >> "$ENGINE_LOG"
  fi
  if [ -f "$login_mark" ]; then
    if /usr/bin/python3 -c 'import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime(sys.argv[1])<7200 else 1)' "$login_mark" 2>/dev/null; then
      echo "[engine] 跳过：上次探测到未登录（.need-login 未过期），先跑 ./login.sh" >> "$ENGINE_LOG"
      return 92
    fi
    rm -f "$login_mark"
  fi

  # 只有 node 脚本才需要 node 解释器；普通 shell 脚本直接执行（便于替换成自写脚本/测试桩）
  if head -1 "$cb_script" 2>/dev/null | grep -qi "node"; then
    node_bin="$(resolve_node)" || {
      echo "[engine] 找不到 node（codebuddy 是 node 脚本）。设 CODEBUDDY_NODE_BIN 指向 WorkBuddy 自带 node" >> "$ENGINE_LOG"
      return 91
    }
  else
    node_bin=""
  fi

  port="${CODEBUDDY_PORT:-$(cfg_get CODEBUDDY_PORT 59187)}"
  pmode="${CODEBUDDY_PERMISSION_MODE:-$(cfg_get CODEBUDDY_PERMISSION_MODE bypassPermissions)}"
  tools="${CODEBUDDY_TOOLS:-$(cfg_get CODEBUDDY_TOOLS 'Read,Write,Edit,Bash,Glob,Grep')}"
  # 模型：留空 = 跟随 WorkBuddy 账号默认模型；填了就固定该模型
  # 可选值见 `codebuddy --help` 的 --model（deepseek-v4.1-flash / glm-5.3 / kimi-k3.1-pro ...）
  model="${CODEBUDDY_MODEL:-$(cfg_get CODEBUDDY_MODEL '')}"

  sess_file="$STATE/session.id"
  mark="$STATE/.session.started"
  if [ ! -s "$sess_file" ]; then
    /usr/bin/python3 -c 'import uuid;print(uuid.uuid4())' > "$sess_file" 2>/dev/null
  fi
  sess=$(tr -cd '0-9a-zA-Z-' < "$sess_file" 2>/dev/null)
  if [ -z "${sess:-}" ]; then
    echo "[engine] 无法取得 session id（内部错误）" >> "$ENGINE_LOG"
    return 1
  fi

  prompt="$(cat "$TASK")
## 落盘要求
完成后把最终汇报写到这里（覆盖写）：$PKG_DIR/$ID.last.md
若无法完成，也要在该文件写明卡在哪、缺什么。"

  out_json="$PKG_DIR/$ID.cb.json"
  err_log="$PKG_DIR/$ID.cb.err"
  : > "$err_log"

  cargs=(-p "$prompt")
  if [ -f "$mark" ]; then
    cargs+=(--resume "$sess" --resume-create-missing)
    echo "[engine] codebuddy session=$sess port=$port mode=$pmode model=${model:-account-default} resume=yes node=${node_bin:-none}" >> "$ENGINE_LOG"
  else
    cargs+=(--session-id "$sess")
    echo "[engine] codebuddy session=$sess port=$port mode=$pmode model=${model:-account-default} resume=no node=${node_bin:-none}" >> "$ENGINE_LOG"
  fi
  [ -n "${model:-}" ] && cargs+=(--model "$model")
  cargs+=(--add-dir "$WORKDIR" --allowedTools "$tools" --permission-mode "$pmode" --port "$port" --output-format json)

  (
    cd "$WORKDIR" || exit 96
    # WorkBuddy 只随包发布了 lite-wb 构建（dist/ 下没有 full/headless 入口），
    # 宿主 spawn sidecar 时自己设这个变量；launchd 环境没有它，不设就 MODULE_NOT_FOUND。
    export CODEBUDDY_FORCE_LITE_WB_BUNDLE="${CODEBUDDY_FORCE_LITE_WB_BUNDLE:-1}"
    # 从 GUI 终端手工跑时会继承 WorkBuddy 的 node shim（--require node-language-shim.cjs），
    # 它会让 axios 的 setProxy 抛 "instanceof is not callable"。daemon 是独立进程，不需要它。
    unset NODE_OPTIONS
    # WorkBuddy 沙箱还会注入 PAC 代理（127.0.0.1:随机端口），同样会让 setProxy 崩。
    # 只有检测到沙箱标志才清，避免误伤用户自己的 clash 代理。
    if [ -n "${WORKBUDDY_PAC_RPC_TOKEN:-}" ] || [ -n "${SANDBOX_CENTER_IPC_ADDRESS:-}" ]; then
      unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
    fi
    if [ -n "${node_bin:-}" ]; then
      PATH="$(dirname "$node_bin"):$PATH"
      export PATH
      exec "$node_bin" "$cb_bin" "${cargs[@]}"
    fi
    exec "$cb_bin" "${cargs[@]}"
  ) > "$out_json" 2>> "$err_log"
  cb_rc=$?

  # 未登录时 codebuddy 退出码仍为 0，只能靠 stderr 判定
  if grep -q "Authentication required" "$err_log" 2>/dev/null; then
    echo "[engine] codebuddy 未登录：需先交互式执行一次 /login" >> "$ENGINE_LOG"
    : > "$STATE/.need-login"
    return 92
  fi
  rm -f "$STATE/.need-login"

  if [ "$cb_rc" -ne 0 ]; then
    echo "[engine] codebuddy 退出码 $cb_rc" >> "$ENGINE_LOG"
    return "$cb_rc"
  fi

  # 提取最终回复；输出为空一律判失败（防止"假成功"）
  /usr/bin/python3 - "$out_json" "$PKG_DIR/$ID.last.md" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
try:
    raw = open(src, encoding="utf-8", errors="replace").read().strip()
except OSError:
    raw = ""
text = None
if raw:
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            for k in ("result", "text", "response", "message", "content"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    text = v
                    break
        elif isinstance(d, str):
            text = d
    except Exception:
        text = raw
if not text:
    text = "(codebuddy 无有效输出)"
    open(dst, "w", encoding="utf-8").write(text + "\n")
    sys.exit(3)
open(dst, "w", encoding="utf-8").write(text if text.endswith("\n") else text + "\n")
PY
  local out_rc=$?
  if [ "$out_rc" -eq 0 ]; then
    : > "$mark"
  fi
  return "$out_rc"
}

# ---- 后端可用性探测（供 ENGINE: auto 选择；均只读，不产生模型调用）----
# 判定 codebuddy 可用：CLI 存在 + node 可解析 + 没有"未登录"标记（标记超过 2 小时视为过期，允许重试）
backend_ok_codebuddy() {
  local cb="$1" mark="$STATE/.need-login"
  [ -f "$cb" ] || return 1
  if [ -f "$mark" ]; then
    # 过期标记自动作废：否则登录成功后 auto 会永远绕过 codebuddy
    if [ -n "$(find "$mark" -mmin +120 2>/dev/null)" ]; then
      rm -f "$mark"
      echo "[engine] 未登录标记已超过 2 小时，作废并重试 codebuddy" >> "$ENGINE_LOG"
    else
      return 1
    fi
  fi
  if head -1 "$cb" 2>/dev/null | grep -qi "node"; then
    resolve_node >/dev/null || return 1
  fi
  return 0
}

# 判定 HTTP 端点可用：只做 TCP 连通性检查（2 秒超时），不发请求、不计费
backend_ok_http() {
  local url="$1"
  [ -n "${url:-}" ] || return 1
  /usr/bin/python3 - "$url" <<'PY' >/dev/null 2>&1
import socket, sys
from urllib.parse import urlparse
u = urlparse(sys.argv[1])
host = u.hostname or "127.0.0.1"
port = u.port or (443 if u.scheme == "https" else 80)
s = socket.socket()
s.settimeout(2)
try:
    s.connect((host, port))
except Exception:
    sys.exit(1)
finally:
    s.close()
PY
}

# 判定 wbrun 可用：网关密码可解析（token 文件或进程环境）+ 会话网关在线
# 只做本地探测，不产生模型调用；密码/端口解析的完整逻辑在 engine-wbrun.sh
backend_ok_wbrun() {
  local p
  if [ ! -s "$STATE/gateway.token" ]; then
    pgrep -f 'WorkBuddy.app/Contents/MacOS' >/dev/null 2>&1 || return 1
  fi
  for p in $(netstat -an -p tcp 2>/dev/null | awk '/LISTEN/ && /127\.0\.0\.1\./ {print $4}' | sed 's/.*\.//' | sort -un); do
    if curl -s -m 2 --noproxy '*' -H "Authorization: Bearer $(head -1 "$STATE/gateway.token" 2>/dev/null | tr -d '\r\n ')" \
       "http://127.0.0.1:${p}/api/v1/status" 2>/dev/null | grep -q 'activeSessionId'; then
      printf '%s' "$p" > "$STATE/gateway.port"
      return 0
    fi
  done
  return 1
}

# 4) 分派后端
code=0

# ENGINE: auto —— 开机不用人工决定：谁可用就走谁，都不可用则 93（等待，不熔断）。
# 顺序：wbrun（免登录复用桌面登录态，首选）→ codebuddy → http → 等待。
if [ "$ENGINE_TYPE" = "auto" ]; then
  _cb="${CODEBUDDY_BIN:-$(cfg_get CODEBUDDY_BIN '')}"
  [ -n "${_cb:-}" ] || _cb="$(goal_detect_cli 2>/dev/null || true)"
  _url="${ENGINE_BASE_URL:-$(cfg_get ENGINE_BASE_URL '')}"
  if backend_ok_wbrun; then
    ENGINE_TYPE="wbrun"
    echo "[engine] auto -> wbrun（会话网关 $(cat "$STATE/gateway.port" 2>/dev/null)）" >> "$ENGINE_LOG"
  elif backend_ok_codebuddy "$_cb"; then
    ENGINE_TYPE="codebuddy"
    echo "[engine] auto -> codebuddy" >> "$ENGINE_LOG"
  elif backend_ok_http "$_url"; then
    ENGINE_TYPE="http"
    echo "[engine] auto -> http ($_url)" >> "$ENGINE_LOG"
  else
    ENGINE_TYPE="none"
    echo "[engine] auto -> 无可用后端（codebuddy 未登录/缺失，HTTP 端点 ${_url:-未配置} 不通）：本次等待" >> "$ENGINE_LOG"
  fi
fi

case "$ENGINE_TYPE" in
  wbrun)
    "$DIR/engine-wbrun.sh" "$ID" >> "$ENGINE_LOG" 2>&1
    code=$?
    ;;
  codebuddy)
    run_codebuddy
    code=$?
    ;;
  http)
    /usr/bin/python3 "$DIR/engine-http.py" "$ID" >> "$ENGINE_LOG" 2>&1
    code=$?
    ;;
  custom)
    if [ -x "$DIR/engine-custom.sh" ]; then
      "$DIR/engine-custom.sh" "$ID" >> "$ENGINE_LOG" 2>&1
      code=$?
    else
      echo "ENGINE=custom 但 engine-custom.sh 不存在或不可执行" >> "$ENGINE_LOG"
      code=1
    fi
    ;;
  none)
    # 没有可用后端：明确返回 93，让 daemon 判"等待"而不是"失败"
    code=93
    ;;
  *)
    echo "未知 ENGINE: $ENGINE_TYPE" >> "$ENGINE_LOG"
    code=1
    ;;
esac

echo "$code" > "$EXITF"
echo "[engine] pkg=$ID end=$(date -u +%FT%TZ) exit=$code" >> "$ENGINE_LOG"
exit "$code"
