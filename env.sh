#!/bin/bash
# env.sh — 环境探测（所有脚本 source 它，保证只有一套规则）
#
# 目标：不绑定任何机器的**路径、用户名、WorkBuddy 安装位置、node 版本目录**。
# 规则：环境变量优先 → 标准位置 → 动态查找 → 找不到就明确报错，绝不猜。
#
# 可覆盖的环境变量：
#   GOAL_DIR       安装目录（默认取本脚本所在目录）
#   GOAL_LABEL     launchd 服务名（默认 local.goal-daemon）
#   WORKBUDDY_APP  WorkBuddy.app 路径（默认自动探测）
#   CODEBUDDY_BIN  codebuddy CLI 路径（默认由 WORKBUDDY_APP 派生）
#   CODEBUDDY_NODE_BIN  node 路径（默认自动探测）

# ---- 安装目录：永远由脚本位置推导，不写死 ----
GOAL_DIR="${GOAL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)}"

# ---- launchd 服务名：中性默认值，不含任何用户名/组织名 ----
# 若你之前装过旧名字，install.sh 会自动把旧服务收掉（见 LEGACY_LABELS）。
GOAL_LABEL="${GOAL_LABEL:-local.goal-daemon}"
LEGACY_LABELS="${LEGACY_LABELS:-com.datong.goal-daemon com.goal-daemon}"

# ---- WorkBuddy 应用位置 ----
goal_detect_app() {
  local c f
  if [ -n "${WORKBUDDY_APP:-}" ]; then
    if [ -d "$WORKBUDDY_APP" ]; then printf '%s' "$WORKBUDDY_APP"; return 0; fi
    echo "[env] WORKBUDDY_APP 指向的目录不存在：$WORKBUDDY_APP" >&2
    return 1
  fi
  for c in "/Applications/WorkBuddy.app" "$HOME/Applications/WorkBuddy.app"; do
    [ -d "$c" ] && { printf '%s' "$c"; return 0; }
  done
  if command -v mdfind >/dev/null 2>&1; then
    f="$(mdfind -name 'WorkBuddy.app' 2>/dev/null | while read -r p; do
          case "$p" in */WorkBuddy.app) printf '%s\n' "$p"; break;; esac
        done)"
    if [ -n "${f:-}" ] && [ -d "$f" ]; then printf '%s' "$f"; return 0; fi
  fi
  return 1
}

# ---- codebuddy CLI：由应用目录派生（WorkBuddy 升级改了布局也能兜住）----
goal_detect_cli() {
  local app c
  if [ -n "${CODEBUDDY_BIN:-}" ]; then
    [ -f "$CODEBUDDY_BIN" ] && { printf '%s' "$CODEBUDDY_BIN"; return 0; }
    echo "[env] CODEBUDDY_BIN 指向的文件不存在：$CODEBUDDY_BIN" >&2
    return 1
  fi
  app="$(goal_detect_app)" || return 1
  for c in \
    "$app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy" \
    "$app/Contents/Resources/cli/bin/codebuddy" \
    "$app/Contents/MacOS/codebuddy"
  do
    [ -f "$c" ] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

# ---- node：codebuddy 是 node 脚本；launchd 的 PATH 里没有 node ----
goal_detect_node() {
  local c v
  if [ -n "${CODEBUDDY_NODE_BIN:-}" ]; then
    [ -x "$CODEBUDDY_NODE_BIN" ] && { printf '%s' "$CODEBUDDY_NODE_BIN"; return 0; }
    echo "[env] CODEBUDDY_NODE_BIN 不可执行：$CODEBUDDY_NODE_BIN" >&2
    return 1
  fi
  # 1) PATH 里现成的
  if c="$(command -v node 2>/dev/null)"; then printf '%s' "$c"; return 0; fi
  # 2) WorkBuddy 自带的 node（版本号不写死，取最新一个）
  for v in "$HOME"/.workbuddy/binaries/node/versions/*/bin/node; do
    [ -x "$v" ] && { printf '%s' "$v"; return 0; }
  done
  # 3) 常见包管理器位置
  for c in /opt/homebrew/bin/node /usr/local/bin/node; do
    [ -x "$c" ] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

# ---- 当前实际在跑的服务名 ----
# 过渡期用：新名字还没装、旧名字还在跑时，status/doctor 仍能认出来，不会误报"未注册"。
# 同时校验 plist 的 WorkingDirectory：同一个 label 可能指向别的安装目录（比如另一份解压包），
# 那种情况不能认领，否则会误报"已注册"。
goal_active_label() {
  local L plist wd real_here real_wd
  real_here="$(cd "$GOAL_DIR" 2>/dev/null && pwd -P || printf '%s' "$GOAL_DIR")"
  for L in "${GOAL_LABEL}" $LEGACY_LABELS; do
    if launchctl print "gui/$(id -u)/$L" >/dev/null 2>&1; then
      plist="$HOME/Library/LaunchAgents/$L.plist"
      if [ -f "$plist" ]; then
        wd="$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "$plist" 2>/dev/null || true)"
        if [ -n "${wd:-}" ]; then
          real_wd="$(cd "$wd" 2>/dev/null && pwd -P || printf '%s' "$wd")"
          [ "$real_wd" = "$real_here" ] || continue
        fi
      fi
      printf '%s' "$L"; return 0
    fi
  done
  printf '%s' "$GOAL_LABEL"
  return 1
}

# ---- 便捷导出：统一走探测函数（显式设置的值也会被校验，不盲信）---- 
goal_app="$(goal_detect_app 2>/dev/null || true)"
goal_cli="$(goal_detect_cli 2>/dev/null || true)"
goal_node="$(goal_detect_node 2>/dev/null || true)"

export GOAL_DIR GOAL_LABEL
