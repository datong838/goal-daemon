#!/bin/bash
# review.sh — 人工审阅"已交付"的包：放行或打回
#
# 为什么需要这一步：
#   引擎跑成功只把包置为 delivered —— 那只代表"引擎自验通过"，不等于你要的结果对。
#   而串行流水线里，下游包要等上游达到 DEPS_REQUIRE 才派发。
#   本脚本就是那道人工闸门：看一眼汇报，决定放行（done）还是打回（ready 重跑）。
#
#   如果你不想人工把关、要全自动串行：把 goal-state/goal.md 里的
#   DEPS_REQUIRE 改成 delivered,done —— 引擎交付即算过，不再需要本脚本。
#
# 用法：
#   ./review.sh                      列出所有包，重点看"待审"
#   ./review.sh <id> show            看这个包的汇报（pkg/<id>.last.md）
#   ./review.sh <id> done            放行 -> done（依赖它的下游才允许派发）
#   ./review.sh <id> retry           打回重跑 -> ready（不计引擎失败次数）
#   ./review.sh <id> retry "原因"    同上，并把原因记进队列
#   ./review.sh <id> block           人工熔断 -> blocked（不再自动重试）
#
# 说明：本脚本只改队列状态，不启动引擎。放行后由下一个 tick（≤10 秒）派下游包。
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE="${GOAL_STATE_DIR:-$DIR/goal-state}"
Q="$STATE/queue.json"

[ -f "$Q" ] || { echo "找不到队列：$Q" >&2; exit 1; }

# 参数两种顺序都认：`./review.sh done vd-001` 与 `./review.sh vd-001 done`
_a1="${1:-list}"; _a2="${2:-}"; _a3="${3:-}"
case "$_a1" in
  list|ls|show|done|retry|block) ACTION="$_a1"; PID_ARG="$_a2"; REASON="$_a3" ;;
  *)
    if [ -n "$_a2" ]; then ACTION="$_a2"; PID_ARG="$_a1"; REASON="$_a3"
    else ACTION="list"; PID_ARG=""; REASON=""; fi ;;
esac

/usr/bin/python3 - "$Q" "$STATE" "$ACTION" "$PID_ARG" "$REASON" <<'PY'
import json, os, sys, tempfile, datetime

qpath, state, action, pid, arg = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

def load():
    with open(qpath, encoding="utf-8") as f:
        return json.load(f)

def save(q):
    # 原子写：daemon 每 10 秒会读写同一个文件，不能让它读到半个 JSON
    d = os.path.dirname(qpath)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".queue-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(q, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, qpath)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def find(q, i):
    for p in q.get("packages", []):
        if p.get("id") == i:
            return p
    return None

def report_path(i):
    return os.path.join(state, "pkg", i + ".last.md")

# ---------- list ----------
if action in ("", "list", "ls"):
    q = load()
    pkgs = q.get("packages", [])
    if not pkgs:
        print("队列为空。往 goal-state/queue.json 的 packages 里加包，或参考 queue.example.json。")
        sys.exit(0)

    order = ["in_progress", "delivered", "ready", "pending", "blocked", "done"]
    buckets = {k: [] for k in order}
    for p in pkgs:
        buckets.setdefault(p.get("status", "?"), []).append(p)

    print("== 队列总览 ==")
    for k in order + [k for k in buckets if k not in order]:
        v = buckets.get(k) or []
        if v:
            print("  %-12s %d" % (k, len(v)))
    print()

    if buckets.get("delivered"):
        print("== 待你审阅（delivered）==")
        for p in buckets["delivered"]:
            rp = report_path(p["id"])
            size = os.path.getsize(rp) if os.path.exists(rp) else 0
            print("  %-10s %s" % (p["id"], p.get("title", "")))
            print("             汇报: %s（%d 字节）" % (rp if size else "缺失", size))
        print("\n  放行：./review.sh <id> done      打回：./review.sh <id> retry")
        print()

    if buckets.get("blocked"):
        print("== 已熔断（blocked）==")
        for p in buckets["blocked"]:
            print("  %-10s fail=%s  最后一次原因: %s"
                  % (p["id"], p.get("fail_count", 0), p.get("last_error") or "-"))
        print("  重新给一次机会：./review.sh <id> retry")
        print()

    runnable = buckets.get("ready") or []
    if runnable:
        print("== 可派发（ready），下一个 tick 就会跑 ==")
        for p in runnable:
            print("  %-10s fail=%s" % (p["id"], p.get("fail_count", 0)))
        print()

    waiting = buckets.get("pending") or []
    if waiting:
        print("== 等上游（pending）==")
        for p in waiting:
            print("  %-10s deps=%s" % (p["id"], ",".join(p.get("deps") or []) or "-"))
    sys.exit(0)

# ---------- show ----------
if action == "show":
    if not pid:
        sys.exit("用法：./review.sh <id> show")
    rp = report_path(pid)
    if not os.path.exists(rp):
        sys.exit("没有汇报文件：%s\n（说明该包还没跑过，或引擎没按协议写汇报）" % rp)
    with open(rp, encoding="utf-8") as f:
        sys.stdout.write(f.read())
    sys.exit(0)

# ---------- 状态变更 ----------
if action not in ("done", "retry", "block"):
    sys.exit("未知动作：%s\n可用：list / show / done / retry / block" % action)
if not pid:
    sys.exit("用法：./review.sh <id> %s" % action)

q = load()
p = find(q, pid)
if p is None:
    sys.exit("队列里没有这个包：%s" % pid)

cur = p.get("status")
if action == "done":
    if cur == "done":
        print("%s 已经是 done，无需重复放行。" % pid)
        sys.exit(0)
    p["status"] = "done"
    p["done_at"] = now()
    p["reviewed_by"] = "human"
    msg = "已放行：%s -> done" % pid
elif action == "retry":
    p["status"] = "ready"
    p["retry_at"] = now()
    p["reviewed_by"] = "human"
    p.pop("done_at", None)
    if arg:
        p.setdefault("review_notes", []).append({"ts": now(), "action": "retry", "note": arg})
    # 清掉上次的退出码，避免下一次 tick 误判为"已结束"
    for ext in (".exit",):
        f = os.path.join(state, "pkg", pid + ext)
        if os.path.exists(f):
            os.unlink(f)
    msg = "已打回：%s -> ready（不计失败次数，下个 tick 重跑）" % pid
else:  # block
    p["status"] = "blocked"
    p["blocked_at"] = now()
    p["reviewed_by"] = "human"
    if arg:
        p.setdefault("review_notes", []).append({"ts": now(), "action": "block", "note": arg})
    msg = "已熔断：%s -> blocked（不再自动重试）" % pid

save(q)
print(msg)

# 放行/打回后，看看有没有下游被解锁
if action in ("done", "retry"):
    q = load()
    accept = {"done"}
    cfg = os.path.join(state, "goal.md")
    if os.path.exists(cfg):
        for line in open(cfg, encoding="utf-8"):
            if line.strip().startswith("DEPS_REQUIRE"):
                vals = line.split(":", 1)[1]
                accept = {s.strip() for s in vals.split(",") if s.strip()}
                break
    unlocked = []
    for x in q.get("packages", []):
        if x.get("status") != "pending":
            continue
        deps = x.get("deps") or []
        if deps and all(
            (find(q, d) or {}).get("status") in accept for d in deps
        ):
            unlocked.append(x["id"])
    if unlocked:
        print("已被解锁（下个 tick 派发）：%s" % ", ".join(unlocked))
PY
