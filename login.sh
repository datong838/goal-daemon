#!/bin/bash
# login.sh — 一次性登录 WorkBuddy 内核 CLI（codebuddy 后端需要 ~/.credentials.json）
#
# 为什么需要这个脚本：
#   1. codebuddy 是 `#!/usr/bin/env node` 脚本，普通终端 PATH 里通常没有 node；
#   2. WorkBuddy 只随包发布了 lite-wb 构建，必须设 CODEBUDDY_FORCE_LITE_WB_BUNDLE=1；
#   3. /login 通过 `/usr/bin/open` 唤起浏览器做 OAuth——只有在真正的 GUI 终端
#      （Terminal.app / iTerm）里才会弹出；在 WorkBuddy 自带终端 / 管道里会静默失败。
#
# 用法（务必在 macOS 自带「终端」App 里执行，不要在本对话框的终端里跑）：
#   cd ~/work/projects/aiagent/goal-daemon
#   ./login.sh
#   进去后输入：/login    （浏览器会自动弹出授权页）
#   看不到浏览器？URL 会直接显示在 TUI 里，复制它到浏览器手动打开也行
#   授权成功后按 Ctrl+C 退出
#
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"

PORT="59187"
if [ "${1:-}" = "--port" ] && [ -n "${2:-}" ]; then
  PORT="$2"
fi

# shellcheck source=env.sh
[ -f "$DIR/env.sh" ] && . "$DIR/env.sh"

CB="${CODEBUDDY_BIN:-$(goal_detect_cli 2>/dev/null || true)}"
if [ -z "${CB:-}" ] || [ ! -f "$CB" ]; then
  echo "找不到 codebuddy CLI（已自动探测 WorkBuddy 安装位置）" >&2
  echo "请设置 WORKBUDDY_APP=/Applications/WorkBuddy.app 或 CODEBUDDY_BIN=<cli 路径>" >&2
  exit 1
fi

NODE_BIN="$(goal_detect_node 2>/dev/null || true)"
if [ -z "${NODE_BIN:-}" ]; then
  echo "找不到 node。请设 CODEBUDDY_NODE_BIN 指向 node 可执行文件" >&2
  exit 1
fi

export CODEBUDDY_FORCE_LITE_WB_BUNDLE="${CODEBUDDY_FORCE_LITE_WB_BUNDLE:-1}"
# 从 WorkBuddy 的终端里跑会继承 node shim，导致 axios setProxy 崩（instanceof is not callable）
unset NODE_OPTIONS
export PATH="$(dirname "$NODE_BIN"):$PATH"

echo "node:     $NODE_BIN"
echo "codebuddy:$CB"
echo "port:     $PORT"
echo

# ── 前置自检：避免“白跑一趟还以为成功了” ──────────────────────────────
if [ -f "$HOME/.credentials.json" ]; then
  echo "✅ 已经登录过（~/.credentials.json 存在），无需重复登录。"
  echo "   直接退出即可，daemon 会在下个 tick 自动开始工作。"
  exit 0
fi

# TUI 需要真·GUI 终端。在 WorkBuddy 集成终端/管道里跑会立刻 process.exit。
if [ ! -t 0 ] || [ ! -t 1 ]; then
  echo "❌ 当前不是交互式终端，TUI 起不来（会立刻退出，什么都不会发生）。" >&2
  echo "→ 请打开 macOS 自带的「终端」App（或 iTerm 独立窗口）再运行：" >&2
  echo "    cd $DIR && ./login.sh" >&2
  exit 3
fi

# 检测是不是“能弹浏览器”的 GUI 终端；不是则强烈提示（但不阻断，给用户机会）。
case "${TERM_PROGRAM:-}" in
  Apple_Terminal|iTerm.app|WarpTerminal|Hyper|Alacritty|kitty)
    echo "✅ 检测到 GUI 终端（${TERM_PROGRAM}），/login 应当能正常弹出浏览器。" ;;
  *)
    echo "⚠️  当前终端程序是『${TERM_PROGRAM:-未知}』，/login 可能无法自动弹出浏览器。" >&2
    echo "    如果浏览器没弹出来，TUI 里会直接显示授权 URL，复制它到浏览器打开即可。" >&2
    echo "    若仍失败，请改用 macOS 自带的「终端」App 重跑本脚本。" >&2
    ;;
esac
echo

echo "登录用的是你自己的 WorkBuddy 账号（不是第三方、不是新账号）："
echo "  1) 进去后输入：/login"
echo "  2) 浏览器会打开授权页，确认即可（和 WorkBuddy 桌面端同一个账号）"
echo "     看不到浏览器？URL 会显示在 TUI 里，手动复制打开同样有效"
echo "  3) 看到登录成功后，Ctrl+C 或 /exit 退出"
echo "之后每个 tick 都用 WorkBuddy 的模型额度跑，不需要再登录。"
echo "------------------------------------------------------------"

if [ "${1:-}" = "--dry-run" ]; then
  echo "[dry-run] 将执行：\"$NODE_BIN\" \"$CB\" --port $PORT"
  exit 0
fi

"$NODE_BIN" "$CB" --port "$PORT"
rc=$?

# TUI 退出后清掉“未登录”标记并校验凭据
STATE="$DIR/goal-state"
[ -n "${GOAL_STATE_DIR:-}" ] && STATE="$GOAL_STATE_DIR"
rm -f "$STATE/.need-login" 2>/dev/null

echo "------------------------------------------------------------"
if [ -f "$HOME/.credentials.json" ]; then
  echo "✅ 登录成功（~/.credentials.json 已生成）。daemon 下个 tick 会自动开始工作。"
  echo "   无需再运行任何命令；可运行 ./status.sh 看进度。"
else
  echo "⚠️  TUI 已退出，但 ~/.credentials.json 仍未生成——登录大概率没完成。"
  echo "   常见原因：在 WorkBuddy 自带终端里运行，浏览器无法弹出。"
  echo "   解决办法：打开 macOS 自带「终端」App，重新执行："
  echo "     cd $DIR && ./login.sh"
  echo "   进去后输入 /login，等浏览器弹出并授权；若只看到 URL 就手动打开它。"
fi
exit $rc
