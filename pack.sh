#!/bin/bash
# pack.sh — 打一个可搬到其它 Mac 的干净包（不含演示与测试残留）
#
# 用法：./pack.sh [输出目录]
# 产物：goal-daemon-<日期>.tar.gz
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$DIR/dist}"
STAMP="$(date +%Y%m%d)"
NAME="goal-daemon-$STAMP"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

PKG="$STAGE/$NAME"
mkdir -p "$PKG/goal-state"

# 只带核心与文档，不带 demo/、.smoke、.regress、.dryhome、logs、活队列
for f in \
  goal-daemon.py engine-http.py engine.sh engine-wbrun.sh engine-custom.sh env.sh \
  install.sh uninstall.sh status.sh review.sh login.sh doctor.sh \
  smoke-e2e.py test-regressions.py test-codebuddy-backend.py test-auto-backend.py \
  goal-daemon.plist.template \
  README.md 使用说明.md Goal常驻循环方案.md 搬运到其它Mac.md
do
  [ -f "$DIR/$f" ] && cp "$DIR/$f" "$PKG/"
done

# 只带模板，不带本机的真实目标：install.sh 会用它在目标机器生成 goal.md
cp "$DIR/goal-state/goal.md.template" "$PKG/goal-state/"
[ -f "$DIR/goal-state/queue.example.json" ] && cp "$DIR/goal-state/queue.example.json" "$PKG/goal-state/"

# 把"本机的真实目标"模板化：包是要给别人用的，不能带着这一台机器的项目路径与用户原话。
/usr/bin/python3 - "$PKG" <<'PY'
import json, pathlib, re, sys
pkg = pathlib.Path(sys.argv[1])

# 1) goal.md：OBJECTIVE / SCOPE_NOTE 换成占位说明
gm = pkg / "goal-state" / "goal.md"
if gm.exists():
    out, done = [], set()
    for line in gm.read_text(encoding="utf-8").splitlines():
        if line.startswith("OBJECTIVE:"):
            out.append("OBJECTIVE: <在这里写你要它持续做成的事，一句话。建议直接把需求原话贴过来>")
            done.add("OBJECTIVE")
        elif line.startswith("SCOPE_NOTE:"):
            out.append("SCOPE_NOTE: <能改哪里、绝不能碰什么。例：工作目录其余部分只读；唯一可写 runs/ ；禁止 git 操作>")
            done.add("SCOPE_NOTE")
        else:
            # 本机特有的版本路径不该出现在分发包里
            line = re.sub(
                r"#\s*CODEBUDDY_NODE_BIN:.*",
                "# CODEBUDDY_NODE_BIN: <留空即可，engine.sh 会自动探测 node>",
                line,
            )
            line = re.sub(
                r"#\s*CODEBUDDY_BIN:.*",
                "# CODEBUDDY_BIN: <留空即可，默认用 WorkBuddy.app 内置 CLI>",
                line,
            )
            out.append(line)
    gm.write_text("\n".join(out) + "\n", encoding="utf-8")

# 2) queue.example.json：把打包机的绝对路径换成通用示例
qe = pkg / "goal-state" / "queue.example.json"
if qe.exists():
    try:
        d = json.loads(qe.read_text(encoding="utf-8"))
    except Exception:
        d = None
    if d:
        example = "/tmp/goal-pilot"
        for p in d.get("packages", []):
            if "workdir" in p:
                p["workdir"] = example
            if p.get("write_scope"):
                p["write_scope"] = [example] * len(p["write_scope"])
        d["note"] = (
            "示例格式，复制进 queue.json 使用。"
            "workdir/write_scope 是占位路径 /tmp/goal-pilot，用前改成你自己的目录。"
            "状态：pending|ready|in_progress|delivered|blocked|done"
        )
        qe.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# 3) 校验：包里不应再残留打包机的绝对路径
leak = []
for f in list(pkg.glob("*.md")) + list((pkg / "goal-state").glob("*")):
    if f.is_file() and f.suffix in (".md", ".json"):
        t = f.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"/Users/[A-Za-z0-9_.-]+/", t):
            leak.append(f"{f.name}: {m.group(0)}")
if leak:
    print("提示：以下文件仍含绝对路径（若为示例/说明可忽略）：")
    for x in sorted(set(leak))[:8]:
        print("  -", x)
PY

# 打进去之前做自检：语法 + 模板占位符完整
/usr/bin/python3 - "$PKG" <<'PY'
import sys, pathlib
pkg = pathlib.Path(sys.argv[1])
bad = []
for f in sorted(pkg.glob("*.py")):
    try:
        compile(f.read_text(encoding="utf-8"), f.name, "exec")
    except SyntaxError as e:
        bad.append(f"{f.name}: {e}")
tpl = pkg / "goal-daemon.plist.template"
if not tpl.exists():
    tpl = pkg / "com.datong.goal-daemon.plist.template"
if not tpl.exists():
    bad.append("缺少 plist 模板")
elif "__GOAL_DIR__" not in tpl.read_text(encoding="utf-8"):
    bad.append("模板缺 __GOAL_DIR__ 占位符")
elif "__LABEL__" not in tpl.read_text(encoding="utf-8"):
    bad.append("模板缺 __LABEL__ 占位符")
if bad:
    sys.exit("自检失败：\n- " + "\n- ".join(bad))
print(f"自检通过：{len(list(pkg.glob('*.py')))} 个 py + plist 模板")
PY

chmod +x "$PKG"/*.sh "$PKG"/*.py
mkdir -p "$OUT"
TARBALL="$OUT/$NAME.tar.gz"
tar -czf "$TARBALL" -C "$STAGE" "$NAME"
echo "已生成：$TARBALL"
echo "大小：$(du -h "$TARBALL" | cut -f1)"
echo "内容："
tar -tzf "$TARBALL" | sed 's|^|  |'
