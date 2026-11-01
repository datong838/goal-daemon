#!/bin/bash
# install.sh — 把 goal-daemon 注册为 launchd 常驻任务（内部 10 秒 tick）
#
# 用法：
#   ./install.sh              安装并启动
#   DRY_RUN=1 ./install.sh    只生成并校验 plist，不注册（用于换目录后预检）
#
# 路径处理：仓库里的 plist 是模板（含 __GOAL_DIR__），安装时注入真实目录。
# 因此项目目录可以整体搬移——搬完在新位置重跑一次本脚本即可，无需手改 plist。
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=env.sh
[ -f "$DIR/env.sh" ] && . "$DIR/env.sh"

# 服务名默认中性（local.goal-daemon），不含机器/用户名；可用 GOAL_LABEL 覆盖
LABEL="${GOAL_LABEL:-local.goal-daemon}"

# plist 落地目录。默认写用户 LaunchAgents；在临时目录里跑（例如用 /tmp 副本做
# "干净机器"演练）时必须改道，否则一次演练就会把生产配置改指到 /tmp，
# 之后 launchctl 找不到服务、goal_active_label 还会误判——这真实发生过。
if [ -n "${GOAL_PLIST_DIR:-}" ]; then
  PLIST_DIR="$GOAL_PLIST_DIR"
elif case "$DIR" in /tmp/*|/private/tmp/*) true ;; *) false ;; esac; then
  PLIST_DIR="$DIR/.generated-plist"
  echo "注意：脚本在临时目录（${DIR}）里运行，plist 只写到 ${PLIST_DIR}，不碰 ~/Library/LaunchAgents。" >&2
  echo "      真安装请把整个目录放到固定位置再跑本脚本。" >&2
else
  PLIST_DIR="$HOME/Library/LaunchAgents"
fi
PLIST_DST="$PLIST_DIR/$LABEL.plist"

# 模板：优先新名字，兼容旧包里的旧名字
TPL=""
for _c in "$DIR/goal-daemon.plist.template" "$DIR/com.datong.goal-daemon.plist.template"; do
  [ -f "$_c" ] && { TPL="$_c"; break; }
done
[ -n "$TPL" ] || { echo "缺少 plist 模板：$DIR/goal-daemon.plist.template" >&2; exit 1; }

# 探测 node（codebuddy 后端需要）：PATH → WorkBuddy 自带任意版本 → 常见包管理器位置
NODE_BIN="$(goal_detect_node 2>/dev/null || true)"
NODE_DIR=""
if [ -n "${NODE_BIN:-}" ]; then
  NODE_DIR="$(cd "$(dirname "$NODE_BIN")" && pwd)"
  echo "node: $NODE_BIN"
else
  echo "提示：未找到 node —— 若用 ENGINE: codebuddy 会失败（ENGINE: http/custom 不受影响）" >&2
fi

chmod +x "$DIR/engine.sh" "$DIR/goal-daemon.py"
mkdir -p "$DIR/goal-state/logs" "$DIR/goal-state/pkg"

# 首次安装：没有 goal.md 就从模板生成（模板不含任何本机路径与项目目标）
if [ ! -f "$DIR/goal-state/goal.md" ] && [ -f "$DIR/goal-state/goal.md.template" ]; then
  cp "$DIR/goal-state/goal.md.template" "$DIR/goal-state/goal.md"
  echo "已从 goal.md.template 生成配置 —— 记得改 OBJECTIVE 与 SCOPE_NOTE"
fi

# OBJECTIVE 还是占位就停手。装了之后再改不算错，但那一刻守护进程已经拿示例队列
# 去跑了——用真额度换一页"这个示例包做完了"，没人想要这个结果。
if [ "${FORCE:-0}" != "1" ]; then
  OBJ="$(/usr/bin/grep -E '^OBJECTIVE:' "$DIR/goal-state/goal.md" 2>/dev/null | head -1 | sed 's/^OBJECTIVE:[[:space:]]*//')"
  case "$OBJ" in
    ""|*"<"*">"*)
      echo "OBJECTIVE 还是模板占位，先别装。" >&2
      echo "  编辑 $DIR/goal-state/goal.md，把 OBJECTIVE 写成一句具体目标，" >&2
      echo "  再跑一次 ./install.sh。只想先看它怎么跑：./demo-run.sh（不碰真额度）。" >&2
      echo "  坚持要装：FORCE=1 ./install.sh" >&2
      exit 2
      ;;
  esac
fi
mkdir -p "$(dirname "$PLIST_DST")"

# 首次安装：没有队列就按示例初始化（已有则不覆盖，避免清掉正在跑的任务）
if [ ! -f "$DIR/goal-state/queue.json" ] && [ -f "$DIR/goal-state/queue.example.json" ]; then
  cp "$DIR/goal-state/queue.example.json" "$DIR/goal-state/queue.json"
  echo "已从 queue.example.json 初始化队列（里面是示例包，跑之前请改 workdir）"
  # 示例包用 /tmp/goal-pilot 作占位工作目录：建出来，免得一上来就 blocked_bad_workdir
  if /usr/bin/grep -q '"/tmp/goal-pilot"' "$DIR/goal-state/queue.json" 2>/dev/null; then
    mkdir -p /tmp/goal-pilot
    echo "已创建示例工作目录 /tmp/goal-pilot"
  fi
fi

# 用 python 做替换，避免 sed 对 / 与中文路径转义出错
/usr/bin/python3 - "$TPL" "$PLIST_DST" "$DIR" "${NODE_DIR:-}" "$LABEL" <<'PY'
import sys, pathlib
tpl, dst, d, node_dir, label = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
text = pathlib.Path(tpl).read_text(encoding="utf-8")
if "__GOAL_DIR__" not in text:
    sys.exit("模板里没有 __GOAL_DIR__ 占位符，拒绝生成（防止误用已展开的文件当模板）")
base = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
full = (node_dir + ":" + base) if node_dir else base
text = text.replace("__GOAL_DIR__", d).replace("__PATH__", full).replace("__LABEL__", label)
pathlib.Path(dst).write_text(text, encoding="utf-8")
PY

# 校验：语法合法 + 占位符已全部展开
/usr/bin/plutil -lint "$PLIST_DST" >/dev/null
if /usr/bin/grep -q "__GOAL_DIR__\|__PATH__\|__LABEL__" "$PLIST_DST"; then
  echo "plist 仍含未展开占位符" >&2; exit 1
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[dry-run] 已生成并校验：$PLIST_DST"
  /usr/bin/grep -E "<string>" "$PLIST_DST" | head -8
  exit 0
fi

# 前置检查：这个终端能不能操作你自己的 launchd 域。
# WorkBuddy 的集成终端跑在非 Aqua session 里，`launchctl list` 会是 0 条，
# 此时无论 plist 怎么写，bootstrap 都必然报 "Bootstrap failed: 5: Input/output error"。
svc_count="$(launchctl list 2>/dev/null | wc -l | tr -d ' ')"
if [ "${svc_count:-0}" -lt 5 ]; then
  echo "当前终端看不到你的 launchd 服务（launchctl list = ${svc_count:-0} 条）。" >&2
  echo "这是 WorkBuddy 集成终端的环境限制，不是脚本或 plist 的问题。" >&2
  echo "→ 请打开 macOS 自带的「终端」App（或 iTerm 独立窗口），重新运行：" >&2
  echo "    cd $DIR && ./install.sh" >&2
  exit 2
fi

# 迁移：收掉旧版服务名（历史版本用过带用户名/组织名的 label）。
# 不做这一步的话，换名后会同时跑两份，互相抢队列。
for _old in $LEGACY_LABELS; do
  [ "$_old" = "$LABEL" ] && continue
  if launchctl print "gui/$(id -u)/$_old" >/dev/null 2>&1; then
    echo "发现旧服务名 $_old，先停掉（避免两个实例抢同一个队列）"
    launchctl bootout "gui/$(id -u)/$_old" >/dev/null 2>&1 || true
  fi
  [ -f "$PLIST_DIR/$_old.plist" ] && rm -f "$PLIST_DIR/$_old.plist" && echo "已移除旧 plist：$_old.plist"
done

# 先 enable：这个 label 若曾被 disable（反复 install/uninstall 的常见后果），
# bootstrap 会直接报 "Bootstrap failed: 5: Input/output error"，而且看不出原因。
launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "已存在，先卸载旧任务"
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  # launchd 的清理是异步的：bootout 刚返回就 bootstrap 会撞上 EIO，
  # 这里必须等旧服务真的从 domain 里消失。
  i=0
  while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -gt 25 ] && break
    sleep 0.2
  done
fi

BOOT_ERR="$(mktemp -t goald)"
ok_boot=0
for n in 1 2 3 4 5; do
  if launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>"$BOOT_ERR"; then
    ok_boot=1
    break
  fi
  sleep 0.4
done

if [ "$ok_boot" -ne 1 ]; then
  echo "bootstrap 失败（已重试 5 次）：" >&2
  sed 's/^/  /' "$BOOT_ERR" >&2
  echo "改走 launchctl load -w 兜底 ……" >&2
  if launchctl load -w "$PLIST_DST" 2>/dev/null; then
    ok_boot=1
  fi
fi
rm -f "$BOOT_ERR"

if [ "$ok_boot" -ne 1 ]; then
  echo "注册失败。常见原因：" >&2
  echo "  1) 在 WorkBuddy 的沙箱终端里跑 —— launchctl 写不了 gui domain，请在系统「终端」里跑" >&2
  echo "  2) label 被 disable —— 试：launchctl enable gui/$(id -u)/$LABEL 后再跑一次" >&2
  exit 1
fi

launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true

# 确认真的拉起来了（RunAtLoad 应立刻启动）
sleep 1
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "已注册 ${LABEL}（每 10 秒 tick）"
else
  echo "已注册，但 launchd 未确认运行；查：launchctl print gui/$(id -u)/$LABEL" >&2
fi

echo
echo "接下来："
echo "  1) 改目标     编辑 $DIR/goal-state/goal.md 的 OBJECTIVE 与 SCOPE_NOTE"
echo "  2) 加任务     编辑 $DIR/goal-state/queue.json，正文写进 $DIR/goal-state/pkg/<id>.task.md"
echo "  3) 验证引擎   $DIR/doctor.sh --probe"
echo "  4) 看状态     $DIR/status.sh"
echo "  5) 看汇报     $DIR/review.sh"
echo
echo "常用：停机 touch $DIR/goal-state/STOP ｜ 恢复 rm $DIR/goal-state/STOP"
echo "完整说明：$DIR/使用说明.md"
