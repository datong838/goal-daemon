#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受保护根（PROTECTED_ROOTS）测试。

这条闸的用途：某个仓你说"别再动了"，但队列里还留着它的包。
口头约定守不住——守护进程每拍都会把 ready 的包捡起来，人不在场时没人拦。
所以闸放在引擎里：命中即拒派，置 held，可一键恢复。

用例：
  A. 命中判定 —— workdir 就是根 / 根的子路径 / write_scope 命中
  B. 前缀陷阱 —— /a/proj-old 不能被 /a/proj 误伤，反过来也不能漏
  C. 配置关闭 —— none / off / 空 / 缺省一并不拦
  D. 多根逗号分隔，任一根命中即拦
  E. pick_ready 跳过并把包置 held，且不影响同队列里别的包
  F. release_held：配置清掉后放回 ready，仍命中的不动
  G. 状态流转不会丢 fail_count（held 是暂停，不是失败）

零成本：不联网、不调 LLM、不启子进程。
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
T = ROOT / ".guard"
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {detail}")


spec = importlib.util.spec_from_file_location("gd", ROOT / "goal-daemon.py")
gd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gd)

REPO = "/Users/x/work/mrpmes"
CFG = {"PROTECTED_ROOTS": REPO}


def pkg(pid, workdir, scope=None, status="ready", **kw):
    p = {"id": pid, "workdir": workdir, "write_scope": scope or [], "status": status,
         "deps": [], "fail_count": 0}
    p.update(kw)
    return p


print("A. 命中判定")
check("workdir 恰为根", gd.hits_protected(pkg("p1", REPO), CFG) == REPO)
check("workdir 是根的子路径", gd.hits_protected(pkg("p2", REPO + "/docs/a"), CFG) == REPO)
check("workdir 末尾带斜杠", gd.hits_protected(pkg("p3", REPO + "/"), CFG) == REPO)
check("write_scope 命中（workdir 在外）",
      gd.hits_protected(pkg("p4", "/tmp/other", [REPO + "/out"]), CFG) == REPO)
check("write_scope 里有多个，只要一个命中即拦",
      gd.hits_protected(pkg("p5", "/tmp", ["/tmp/a", REPO + "/x", "/tmp/b"]), CFG) == REPO)

print("\nB. 前缀陷阱（最容易写错的一处）")
check("兄弟目录 proj-old 不误伤", gd.hits_protected(pkg("p6", REPO + "-old"), CFG) == "")
check("同名前缀 a/b 不误伤", gd.hits_protected(pkg("p7", REPO + "2"), CFG) == "")
check("父目录不拦（根是子路径时才拦）", gd.hits_protected(pkg("p8", "/Users/x/work"), CFG) == "")

print("\nC. 配置关闭")
for v in ("", "none", "off", "-", "  ", None):
    check(f"PROTECTED_ROOTS={v!r} 不拦",
          gd.hits_protected(pkg("q", REPO), {"PROTECTED_ROOTS": v}) == "")
check("完全不写这个键也不拦", gd.hits_protected(pkg("q", REPO), {}) == "")

print("\nD. 多根")
multi = {"PROTECTED_ROOTS": "/a/one, /b/two ,/c/three"}
check("第 1 根", gd.hits_protected(pkg("r1", "/a/one/x"), multi) == "/a/one")
check("第 2 根（带空格）", gd.hits_protected(pkg("r2", "/b/two/y"), multi) == "/b/two")
check("第 3 根", gd.hits_protected(pkg("r3", "/c/three"), multi) == "/c/three")
check("都不命中", gd.hits_protected(pkg("r4", "/d/four"), multi) == "")

print("\nE. pick_ready 拒派")
shutil.rmtree(T, ignore_errors=True)
(T / "logs").mkdir(parents=True)
queue = {"packages": [
    pkg("mes-1", REPO + "/docs", status="ready"),
    pkg("free-1", "/tmp/elsewhere", status="ready"),
]}
picked = gd.pick_ready(queue, ("done",), CFG, T)
check("跳过受保护的包，取到后面的", picked is not None and picked["id"] == "free-1")
held = queue["packages"][0]
check("被拒的包置为 held", held["status"] == "held", held["status"])
check("记下命中的根", held.get("held_root") == REPO, held.get("held_root"))
check("held 有时间戳", bool(held.get("held_at")))
log_file = sorted((T / "logs").glob("*.log"))
text = log_file[-1].read_text(encoding="utf-8") if log_file else ""
check("写了 guard_hold 日志", "guard_hold" in text, text[-200:])

print("\nF. release_held 恢复")
gd.release_held(queue, {"PROTECTED_ROOTS": "none"})
check("配置清掉 → 放回 ready", queue["packages"][0]["status"] == "ready",
      queue["packages"][0]["status"])
check("held_root 已清", "held_root" not in queue["packages"][0])
gd.pick_ready(queue, ("done",), CFG, None)
check("再拦一次（重新置 held）", queue["packages"][0]["status"] == "held")
gd.release_held(queue, CFG)
check("配置没变 → 仍 held，不放回", queue["packages"][0]["status"] == "held",
      queue["packages"][0]["status"])
gd.release_held(queue, {"PROTECTED_ROOTS": "/another/repo"})
check("换成别的根 → 原来那个不再命中，放回 ready",
      queue["packages"][0]["status"] == "ready", queue["packages"][0]["status"])

print("\nG. held 是暂停不是失败")
q2 = {"packages": [pkg("mes-2", REPO, status="ready", fail_count=2)]}
gd.pick_ready(q2, ("done",), CFG, None)
check("fail_count 不变（不把拒派算成失败）", q2["packages"][0]["fail_count"] == 2)
check("状态是 held 不是 blocked", q2["packages"][0]["status"] == "held")

print("\nH. 队列里全是受保护包时不取任何包")
q3 = {"packages": [pkg("m1", REPO, status="ready"), pkg("m2", REPO + "/x", status="ready")]}
check("返回 None（正确的空闲）", gd.pick_ready(q3, ("done",), CFG, None) is None)

print("\nI. 引擎源码里不能出现项目名（goal 是通用的）")
src = (ROOT / "goal-daemon.py").read_text(encoding="utf-8")
fu = (ROOT / "followup.py").read_text(encoding="utf-8")
check("goal-daemon.py 无项目名", "mrpmes" not in src.lower())
check("followup.py 无项目名", "mrpmes" not in fu.lower())

shutil.rmtree(T, ignore_errors=True)
print(f"\n结果：{ok} passed, {fail} failed")
sys.exit(0 if fail == 0 else 1)
