#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回流层（followup.py）回归测试。零成本，不联网、不调 LLM。

用例：
  A. 解析 —— 只认「仍未修」这类小节里的列表项；三级优先只取最高一档
  B. 判定 —— "已登记未修"不能被当成已解决
  C. 去重 —— 同一件事在「未证点」和「仍未修」各写一遍，只算一条
  D. recheck —— RESOLVED 行能扣减，但不动历史汇报
  E. 闸门 —— 父包 delivered 且 REQUIRE_DONE=on 时不追；done 才追
  F. 深度 —— vd-003 → f1 → f2；f2 再追必须被 max_depth 挡住（历史 bug：id 撞车）
  G. 幂等 —— 同父只生一次
  H. 指令 —— 生成的 instruction 里必须逐条列出遗留项、写清汇报路径
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
T = ROOT / ".followup-test"
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {detail}")


spec = importlib.util.spec_from_file_location("fu", ROOT / "followup.py")
fu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fu)


def fresh(cfg=None, pkgs=None):
    shutil.rmtree(T, ignore_errors=True)
    (T / "pkg").mkdir(parents=True)
    base = "FOLLOWUP: on\nFOLLOWUP_MAX_DEPTH: 2\n"
    if cfg:
        base += cfg
    (T / "goal.md").write_text(base, encoding="utf-8")
    queue = {"packages": pkgs or []}
    (T / "queue.json").write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
    return queue


def pkg(pid, status="done", at="2026-10-31T20:00:00+08:00"):
    return {
        "id": pid,
        "title": f"测试 {pid}",
        "instruction": "x",
        "workdir": str(T),
        "write_scope": [str(T)],
        "deps": [],
        "status": status,
        "fail_count": 0,
        "delivered_at": at,
        "timeout_s": 60,
    }


def last(pid, body):
    (T / "pkg" / f"{pid}.last.md").write_text(body, encoding="utf-8")


print("A. 解析：只认小节里的列表项")
md = """# 汇报

## 通过项
1. 这条不该被当成遗留。

### 仍未修（不冒充已解决）

1. 撤权形态只有文字表，屏没出。
2. 6 处枚举原值没回 Schema 核对。

## 建议下一步
1. 也不该被取到。
"""
items = fu.open_items(md)
check("只抓到仍未修那节的 2 条", len(items) == 2, items)
check("没抓到通过项", all("不该被当成遗留" not in t for _, t in items), items)
check("没抓建议下一步", all("也不该被取到" not in t for _, t in items), items)

print("\nB. 判定：'已登记未修'不能被当已解决")
check("含'未'即视为未完成", fu._closed("583 表 184 行无中文显示名只登记未修") is False)
check("纯'已修'算完成", fu._closed("撤权屏已修") is True)
check("'已出屏'算完成", fu._closed("撤权形态已出屏") is True)

print("\nC. 去重：同一件事写两遍只算一条")
md2 = """### 未证点
1. 6 处状态枚举原值仍未回原 Schema。
2. 六端只做了桌面一端。
### 仍未修
1. 6 处状态枚举原值未回 Schema。
2. 交互回归没做。
"""
# 分档后只取第一档（仍未修），这里直接测 _collect 的相似去重
secs = fu._sections(md2)
allsec = [s for s in secs if any(k in s["title"] for k in ("未证", "仍未修"))]
got = fu._collect(allsec)
check("相似项被合并", len(got) == 3, [t for _, t in got])

print("\nD. recheck：RESOLVED 能扣减")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\n", pkgs=[pkg("t-001")])
last("t-001", "### 仍未修\n1. 撤权形态只有文字表，屏没出。\n2. 六端只做了桌面一端，其余五端未出稿。\n")
(T / "pkg" / "t-001.recheck.md").write_text(
    "RESOLVED: 撤权形态只有文字表，屏没出\n", encoding="utf-8"
)
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("扣掉 1 项，剩 1 项", "spawn" in r and len(r["items"]) == 1, r.get("items"))
check("扣减有记录", r.get("dropped") and len(r["dropped"]) == 1, r.get("dropped"))
check("历史汇报未被改动", "撤权形态" in (T / "pkg" / "t-001.last.md").read_text(encoding="utf-8"))

print("\nE. 闸门：默认免审直接追；显式设 on 才要求人放行")
fresh(pkgs=[pkg("t-001", status="delivered")])
last("t-001", "### 仍未修\n1. 还有一件事没有处理完。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("默认（无配置）不就等放行", "spawn" in r, r.get("skip"))

q = fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\n", pkgs=[pkg("t-001", status="delivered")])
last("t-001", "### 仍未修\n1. 还有一件事没有处理完。\n")
r = fu.plan(T, q, fu.parse_kv(T / "goal.md"))
check("显式 off 时 delivered 即追", "spawn" in r, r.get("skip"))

# 反向：真要人看着的场合，显式 on 仍然挡得住
fresh(cfg="FOLLOWUP_REQUIRE_DONE: on\n", pkgs=[pkg("t-001", status="delivered")])
last("t-001", "### 仍未修\n1. 还有一件事没有处理完。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("显式 on 时未放行不追", r.get("skip") == "parent_not_reviewed", r.get("skip"))

print("\nF. 深度：f2 再追必须被挡住")
for pid, expect in (("t-001", "t-001-f1"), ("t-001-f1", "t-001-f2")):
    fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\n", pkgs=[pkg(pid)])
    last(pid, "### 仍未修\n1. 这件事还没有处理完，需要继续。\n")
    r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
    check(f"{pid} → {expect}", "spawn" in r and r["spawn"]["id"] == expect, r.get("spawn", {}).get("id"))

fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\n", pkgs=[pkg("t-001-f2")])
last("t-001-f2", "### 仍未修\n1. 这件事还没有处理完，需要继续。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("f2 再追被 max_depth 挡住", r.get("skip") == "max_depth", r)
check("新 id 不与父包撞车", fu._split_id("t-001-f2") == ("t-001", 2), fu._split_id("t-001-f2"))

print("\nG. 幂等：同父只生一次")
q = fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\n", pkgs=[pkg("t-001")])
last("t-001", "### 仍未修\n1. 还有一件事没有处理完。\n")
r1 = fu.plan(T, q, fu.parse_kv(T / "goal.md"))
q["packages"].append(r1["spawn"])
r2 = fu.plan(T, q, fu.parse_kv(T / "goal.md"))
check("第二次 skip", r2.get("skip") == "already_spawned", r2)

print("\nH. 指令内容")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\n", pkgs=[pkg("t-001")])
last("t-001", "### 仍未修\n1. 撤权形态只有文字表，屏没出。\n2. 六端只做了桌面一端，其余五端未出稿。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
ins = r["spawn"]["instruction"]
check("列出遗留项原文", "撤权形态只有文字表" in ins and "六端只做了桌面一端" in ins)
check("写明汇报路径", "t-001-f1.last.md" in ins)
check("写明写界", str(T) in ins)
check("写死不接受原样留白", "不接受把" in ins)
check("deps 指回父包", r["spawn"]["deps"] == ["t-001"], r["spawn"]["deps"])

print("\nI. 干净汇报不生成")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\n", pkgs=[pkg("t-001")])
last("t-001", "## 通过项\n1. 全部完成。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("skip clean", r.get("skip") == "clean", r)

print("\nJ. 不限层：MAX_DEPTH=0 时第 8 层照追")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\n", pkgs=[pkg("t-001-f7")])
last("t-001-f7", "### 仍未修\n1. 第七层之后还有一件没做完的事。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("f7 → f8（不再数层数）", "spawn" in r and r["spawn"]["id"] == "t-001-f8", r.get("skip") or r["spawn"]["id"])

print("\nK. 原地打转自停：与父包被派的项几乎同文就不再派")
p = pkg("t-002-f1")
p["followup_items"] = ["六端只做了桌面一端，其余五端未出稿", "真实 ACL 拿不到，缺身份源"]
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\n", pkgs=[p])
last("t-002-f1", "### 仍未修\n1. 六端只做了桌面一端，其余五端未出稿。\n2. 真实 ACL 拿不到，缺身份源。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("判 no_progress 并停下", r.get("skip") == "no_progress", r.get("skip"))

# 反向：只要出现一条真正新的待办，就必须继续派
last("t-002-f1", "### 仍未修\n1. 六端只做了桌面一端，其余五端未出稿。\n2. 真实 ACL 拿不到，缺身份源。\n3. 新发现：台账导出按钮点了没反应。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("有新待办就继续", "spawn" in r, r.get("skip"))

print("\nL. 关掉 no_progress 后不再自动停")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\nFOLLOWUP_NO_PROGRESS: off\n", pkgs=[p])
last("t-002-f1", "### 仍未修\n1. 六端只做了桌面一端，其余五端未出稿。\n2. 真实 ACL 拿不到，缺身份源。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("NO_PROGRESS=off 时照派", "spawn" in r, r.get("skip"))

print("\nM. 做不动的项：记账，不派包（拆审批后唯一的空转来源）")
BLOCKED_MD = "### 仍未修\n1. 真人走查（缺 5 名真人）。\n2. VoiceOver 实际朗读（缺真机 + 人耳）。\n3. B 载体鉴权缺身份源。\n"
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\n", pkgs=[pkg("t-003")])
last("t-003", BLOCKED_MD)
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("全 blocked → skip all_blocked", r.get("skip") == "all_blocked", r.get("skip"))
check("记 3 条", len(r.get("blocked") or []) == 3, r.get("blocked"))
check("blocked.md 落盘", (T / "blocked.md").exists())
items = json.loads((T / "blocked.json").read_text(encoding="utf-8"))["items"]
check("blocked.json 3 项", len(items) == 3, len(items))
kinds = {i["kind"] for i in items}
check("分类对（人/硬件/凭证）", kinds == {"人", "硬件", "凭证"}, kinds)

print("\nN. 混合：只派能做的，做不动的记账")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\n", pkgs=[pkg("t-004")])
last("t-004", "### 仍未修\n1. 真人走查（缺 5 名真人）。\n2. 台账导出按钮点了没反应。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("spawn", "spawn" in r, r.get("skip"))
check("包里只剩 1 项", len(r.get("items") or []) == 1, r.get("items"))
check("指令里没有真人走查", "真人走查" not in (r.get("spawn") or {}).get("instruction", ""))
check("blocked 记 1 条", len(json.loads((T / "blocked.json").read_text(encoding="utf-8"))["items"]) == 1)

print("\nO. FOLLOWUP_PARK: off 回到旧行为（照派）")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\nFOLLOWUP_PARK: off\n", pkgs=[pkg("t-003")])
last("t-003", BLOCKED_MD)
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("off 时照派 3 项", "spawn" in r and len(r["items"]) == 3, r.get("skip"))
check("off 时不写台账", not (T / "blocked.json").exists())

print("\nP. 台账去重：同一批话再来一轮不重复堆")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\n", pkgs=[pkg("t-003")])
last("t-003", BLOCKED_MD)
fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
last("t-003", BLOCKED_MD.replace("缺 5 名真人", "缺五名真人"))  # 换措辞，同义
fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
items = json.loads((T / "blocked.json").read_text(encoding="utf-8"))["items"]
check("仍是 3 条", len(items) == 3, len(items))

print("\nQ. dry-run 不写盘")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\n", pkgs=[pkg("t-003")])
last("t-003", BLOCKED_MD)
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"), write=False)
check("仍判 all_blocked", r.get("skip") == "all_blocked", r.get("skip"))
check("但不落 blocked.json", not (T / "blocked.json").exists())

print("\nR. 决策分支不是待办（vd-003-f5 误派的根因）")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\n", pkgs=[pkg("t-005")])
last(
    "t-005",
    "### 未证点\n"
    "1. 答「该」→ 按 A 实现，约半天（折叠控件 + 展开态）。\n"
    "2. 答「不该」→ 本条关闭，不实现。\n"
    "3. 台账导出按钮点了没反应。\n",
)
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
items = [t for _, t in (r.get("items") or [])]
check("两条分支被跳过", len(items) == 1, items)
check("只剩真待办", bool(items) and "台账导出" in items[0], items)

print("\nS. 「等外部输入」的四种写法都要认出来")
false_neg = [
    "六端其余 5 端 —— 等构建产物",
    "56 段取舍 —— 等产品一句话",
    ".btn 39→44 的视觉效果未做视觉评审",
    "那一行高了 26px，未与人确认是否可接受",
]
kinds = [fu.blocked_kind(t) for t in false_neg]
check("四条全判 blocked", all(kinds), kinds)
check("分类：上游/决策/决策/决策", kinds == ["上游", "决策", "决策", "决策"], kinds)

print("\nT. 被撤销的子包不算已回流")
child = pkg("t-006-f1", status="cancelled")
child["followup_of"] = "t-006"
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\n", pkgs=[pkg("t-006"), child])
last("t-006", "### 仍未修\n1. 台账导出按钮点了没反应。\n")
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("撤掉误派包后父包能重新回流", "spawn" in r, r.get("skip"))
check("新包层号仍是 f1", (r.get("spawn") or {}).get("id") == "t-006-f1", (r.get("spawn") or {}).get("id"))

print("\nU. 作者自己划掉/标了可选优化的，都不派")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\n", pkgs=[pkg("t-007")])
last("t-007", (
    "## 九、未证点（本包之后仍在）\n\n"
    "1. ~~390 下按钮右下角 2×2px 点不到自己~~ —— 已推翻，不是缺陷。\n"
    "2. focus 描边只剩 0–2px 余量，未改也能过检查，按可选优化记。\n"
    "3. 六端另外 5 端 —— 等另外 5 端产物。\n"
    "4. 56 段取舍 —— 等一句决策 + 56 段数据。\n"
))
r = fu.plan(T, json.loads((T / "queue.json").read_text(encoding="utf-8")), fu.parse_kv(T / "goal.md"))
check("删除线与可选项都不抽", r.get("skip") == "all_blocked", r.get("skip"))
check("两条待外部输入的记了账", len(r.get("blocked") or []) == 2, r.get("blocked"))
check("一条也不派", not r.get("items"), r.get("items"))

print("\nV. 删除线单独验：含「未」也照样判已闭环")
check("划掉的项算已闭环", fu._closed("~~某项没做完~~ —— 已推翻") is True, fu._closed("~~某项没做完~~ —— 已推翻"))
check("没划掉的含未照旧是开口", fu._closed("某项没做完") is False, fu._closed("某项没做完"))
check("可选项识别", fu._not_todo("按可选优化记") is True, fu._not_todo("按可选优化记"))
check("真待办不算可选项", fu._not_todo("台账导出按钮点了没反应") is False, fu._not_todo("台账导出按钮点了没反应"))

print("\nW. 人工关掉的台账项，回流时不重新记账")
fresh(cfg="FOLLOWUP_REQUIRE_DONE: off\nFOLLOWUP_MAX_DEPTH: 0\n", pkgs=[pkg("t-008")])
n1 = fu.park_blocked(T, "t-008", [("六端其余 5 端 —— 等构建产物", "上游")])
store = fu.blocked_store(T)
for it in store["items"]:
    it["status"] = "closed"
fu.write_json_atomic(T / "blocked.json", store)
n2 = fu.park_blocked(T, "t-008", [("六端其余 5 端 —— 等构建产物", "上游")])
after = fu.blocked_store(T)["items"]
check("第一次记 1 条", n1 == 1, n1)
check("关掉后再出现不重复记", n2 == 0, n2)
check("open 仍是 0 条", sum(1 for i in after if i.get("status") == "open") == 0,
      [(i.get("status"), i["text"][:20]) for i in after])

print(f"\n结果：{ok} passed, {fail} failed")
shutil.rmtree(T, ignore_errors=True)
sys.exit(1 if fail else 0)
