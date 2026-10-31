#!/bin/bash
# engine-wbrun.sh — 通过 WorkBuddy 会话网关（Agent Run API）提交任务包
#
# 原理：WorkBuddy 桌面端给每个 CLI 会话进程挂一个本机网关（端口随会话变），
#   POST /api/v1/runs 可向 agent 提交任务，鉴权用 CODEBUDDY_GATEWAY_PASSWORD。
#   该密码由桌面端在启动时生成、下发给所有子进程——复用它即可免登录驱动模型。
#
# 契约同 engine.sh：读 pkg/<id>.task.md，写 pkg/<id>.exit 与 pkg/<id>.last.md
# 退出码：0 成功；92 网关/密码不可用（daemon 记 waiting 不计失败）；3 完成但无产出；1 其它失败
set -u

STATE="${GOAL_STATE_DIR:?GOAL_STATE_DIR required}"
ID="${1:?package id required}"
DIR="$(cd "$(dirname "$0")" && pwd)"

PKG_DIR="$STATE/pkg"
TASK="$PKG_DIR/$ID.task.md"
EXITF="$PKG_DIR/$ID.exit"
LAST="$PKG_DIR/$ID.last.md"
ENGINE_LOG="$STATE/logs/engine-$ID.log"
PW_FILE="$STATE/gateway.token"
PORT_CACHE="$STATE/gateway.port"

mkdir -p "$PKG_DIR" "$STATE/logs"

cfg_get() {
  local key="$1" def="${2:-}" v=""
  if [ -f "$STATE/goal.md" ]; then
    v=$(grep -E "^[[:space:]]*${key}[[:space:]]*:" "$STATE/goal.md" | head -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
  fi
  if [ -n "${v:-}" ]; then printf '%s' "$v"; else printf '%s' "$def"; fi
}

log() { echo "[wbrun] $(date -u +%FT%TZ) $*" >> "$ENGINE_LOG"; }

TIMEOUT_S=$(cfg_get WBRUN_TIMEOUT_S 1800)
POLL_S=$(cfg_get WBRUN_POLL_S 10)

# ---- 1) 解析网关密码：token 文件 → 从 WorkBuddy 进程环境提取（自动跟随轮换）----
resolve_pw() {
  if [ -s "$PW_FILE" ]; then
    printf '%s' "$(head -1 "$PW_FILE" | tr -d '\r\n ')"
    return 0
  fi
  local pid line
  for pid in $(pgrep -f 'WorkBuddy.app/Contents/MacOS' 2>/dev/null); do
    line=$(ps eww -p "$pid" 2>/dev/null | grep -o 'CODEBUDDY_GATEWAY_PASSWORD=[^ ]*' | head -1)
    if [ -n "$line" ]; then
      printf '%s' "${line#CODEBUDDY_GATEWAY_PASSWORD=}"
      return 0
    fi
  done
  return 1
}

PW="$(resolve_pw)" || { log "no gateway password (token 文件缺失且 ps 提取失败)"; echo 92 > "$EXITF"; exit 92; }

# ---- 2) 发现网关端口：扫描本机 LISTEN 端口，认准 /api/v1/status 的 activeSessionId 响应 ----
port_alive() {
  local p="$1"
  curl -s -m 2 --noproxy '*' -H "Authorization: Bearer ${PW}" \
    "http://127.0.0.1:${p}/api/v1/status" 2>/dev/null | grep -q 'activeSessionId'
}

discover_port() {
  local p cached=""
  [ -s "$PORT_CACHE" ] && cached="$(head -1 "$PORT_CACHE" | tr -d '\r\n ')"
  if [ -n "$cached" ] && port_alive "$cached"; then
    printf '%s' "$cached"; return 0
  fi
  for p in $(netstat -an -p tcp 2>/dev/null | awk '/LISTEN/ && /127\.0\.0\.1\./ {print $4}' | sed 's/.*\.//' | sort -un); do
    if port_alive "$p"; then
      printf '%s' "$p" > "$PORT_CACHE"
      log "gateway discovered at port $p"
      printf '%s' "$p"; return 0
    fi
  done
  return 1
}

PORT="$(discover_port)" || { log "gateway not found（桌面端未运行或会话已关闭）"; echo 92 > "$EXITF"; exit 92; }
BASE="http://127.0.0.1:${PORT}"

# 限流退避：上次被 RATE_LIMITED 后 120 秒内不再撞网关（网关按并发排队，急提交无意义）
RL_MARK="$STATE/.wbrun-ratelimited"
if [ -f "$RL_MARK" ]; then
  age=$(( $(date +%s) - $(stat -f %m "$RL_MARK" 2>/dev/null || echo 0) ))
  if [ "$age" -lt 120 ]; then
    log "rate-limit backoff（${age}s < 120s），跳过本次提交"
    echo 92 > "$EXITF"; exit 92
  fi
  rm -f "$RL_MARK"
fi

# ---- 3) 提交 run（task.md 内容 + 写回协议）----
if [ ! -s "$TASK" ]; then
  log "task file missing: $TASK"
  echo 1 > "$EXITF"; exit 1
fi

MSG_JSON="$(/usr/bin/python3 - "$ID" "$TASK" "$STATE" "$TIMEOUT_S" <<'PY'
import json, sys, time
mid, task_path, state, timeout_s = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
task = open(task_path, encoding="utf-8").read()
protocol = (
    "\n\n## 完成协议（必须遵守）\n"
    "1. 使用绝对路径访问所有文件；本任务的工作目录以上文「工作目录」为准。\n"
    "2. 完成后，把最终汇报（改动文件清单 / 验证结果 / 未证点 / 下一步建议）用 UTF-8 写入：\n"
    "   %s/pkg/%s.last.md\n"
    "3. 只写上述这一个汇报文件和任务允许范围内的文件，不要改动其它任何文件，不要执行 git 提交。\n"
    "4. 若任务无法完成，也要写入该汇报文件，说明原因。\n"
) % (state, mid)
body = {
    "id": "goal-%s-%d" % (mid, int(time.time())),
    "type": "text",
    "text": task + protocol,
    "timeoutMs": int(float(timeout_s) * 1000),
}
print(json.dumps(body, ensure_ascii=False))
PY
)"

RESP="$(curl -s -m 15 --noproxy '*' -X POST \
  -H "Authorization: Bearer ${PW}" -H "Content-Type: application/json" \
  -d "$MSG_JSON" "${BASE}/api/v1/runs" 2>/dev/null)"

RID="$(printf '%s' "$RESP" | /usr/bin/python3 -c 'import json,sys
try: print(json.load(sys.stdin)["data"]["runId"])
except Exception: print("")' 2>/dev/null)"

if [ -z "$RID" ]; then
  log "submit failed: resp=${RESP:0:200}"
  # 限流与鉴权/失联都算等待，不熔断；限流额外记退避标记
  printf '%s' "$RESP" | grep -q 'RATE_LIMITED' && touch "$RL_MARK"
  echo 92 > "$EXITF"; exit 92
fi
rm -f "$RL_MARK" 2>/dev/null
log "submitted runId=$RID port=$PORT"

# ---- 4) 轮询至完成 ----
# 产出新鲜度：只认「本次提交之后写的」last.md，避免把上一轮残留的汇报当成本轮成功。
# 这条同时用在超时判定里——曾经因为"超时时已写好的汇报不算数"，已交付的包被记成
# failure 并重派，fail_count 里全是伪造的失败。
START_TS=$(date +%s)
fresh_output() {
  [ -s "$LAST" ] || return 1
  local m
  m=$(stat -f %m "$LAST" 2>/dev/null || echo 0)
  [ "$m" -ge "$START_TS" ]
}

# 产出落盘后给收尾留的余量（会话可能还在跑验证），只延一次，不无限等。
GRACE_S=$(cfg_get WBRUN_OUTPUT_GRACE_S 300)

elapsed=0
deadline="$TIMEOUT_S"
extended=0
while [ "$elapsed" -lt "$deadline" ]; do
  sleep "$POLL_S"
  elapsed=$((elapsed + POLL_S))
  if [ "$extended" -eq 0 ] && fresh_output; then
    extended=1
    deadline=$((elapsed + GRACE_S))
    log "output appeared at t=${elapsed}s -> grace ${GRACE_S}s"
  fi
  ST="$(curl -s -m 3 --noproxy '*' -H "Authorization: Bearer ${PW}" "${BASE}/api/v1/runs/${RID}" 2>/dev/null)"
  if printf '%s' "$ST" | grep -q '"active":false'; then
    log "run finished after ${elapsed}s"
    break
  fi
  if [ -z "$ST" ]; then
    log "gateway lost while polling (t=${elapsed}s)"
    echo 92 > "$EXITF"; exit 92
  fi
done

if [ "$elapsed" -ge "$deadline" ]; then
  # 超时但产出已在：这是成功，不是失败。判失败会重派同一包、两个 run 抢写同一份稿。
  if fresh_output; then
    log "run timeout after ${elapsed}s but output present -> exit 0"
    echo 0 > "$EXITF"; exit 0
  fi
  if [ -s "$LAST" ]; then
    log "run timeout after ${elapsed}s; last.md exists but stale -> exit 3"
  else
    log "run timeout after ${TIMEOUT_S}s"
  fi
  echo 3 > "$EXITF"; exit 3
fi

# ---- 5) 校验产出 ----
sleep 2
if fresh_output; then
  log "output ok: $LAST ($(wc -c < "$LAST") bytes)"
  echo 0 > "$EXITF"; exit 0
fi
if [ -s "$LAST" ]; then
  log "run finished but $LAST is stale (mtime < submit) -> 不算本轮产出"
else
  log "run finished but no output file $LAST"
fi
echo 3 > "$EXITF"; exit 3
