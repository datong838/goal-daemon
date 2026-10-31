#!/usr/bin/env python3
"""followup.py — 把上一包"仍未修"变成下一包。

没有这一层，daemon 只是批处理：queue 里预置几个包，跑完就 idle，剩下的
"未修/未证"永远停在汇报里没人管——上一轮就是这么停的。

有了这一层才是 Loop：
   执行 -> 交付 -> 扫汇报里的遗留项 -> 长出下一包 -> 再执行 -> …
   直到某包汇报里没有遗留项，或撞到深度上限。

设计上的三条自保：
  1) 必须显式开：goal.md 写 FOLLOWUP: on 才生效，默认 off。
  2) 默认要求父包先被人工放行（FOLLOWUP_REQUIRE_DONE: on）——自动追会自动
     绕过审阅，那是失控而不是 Loop。
  3) 有深度上限（FOLLOWUP_MAX_DEPTH，默认 2）与"同父只生一次"去重。

用法：
  ./followup.py --list                 # 看每个包能回流几项
  ./followup.py --dry-run              # 看会生成什么包，不写
  ./followup.py --apply                # 真写进 queue.json
  ./followup.py --from vd-003 --apply  # 指定父包
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# 第一档：明确待办（有这些就只取这些）
HARD_KEYS = ("仍未修", "没做", "未做", "未覆盖", "遗留", "待修", "没修")

# 「机器在写界内做不动」的项。这类不是待办，是**要外部输入**：
#   人   —— 缺真人/人耳（走查、朗读验收、访谈）
#   硬件 —— 缺真机/设备（真机触摸、实机联调）
#   上游 —— 仓库里根本没有（六端只给了一端）
#   凭证 —— 缺账号/身份源/证书，机器拿不到也不该去拿
#   决策 —— 产品/视觉/信息架构要人拍板，不是查证能得出答案的
#
# 为什么要单独分出来：拆掉审批后的 Loop 是无人值守的，这些项每轮都会被
# 汇报原样抄回来一次，再被自动派成下一包 —— 派下去唯一的结果是写一句
# "做不了"，下一包再抽出来派一遍。真事故：vd-003-f4 就是这么来的，
# 7 项里 7 项都要人/真机/身份源，整包 3600s 预算换回一页"做不了"。
# 处理方式不是审批（审批又要人守在旁边，Loop 就白搭了），而是记账：
# 收进 blocked 台账、不进下一包、Loop 继续往前跑。
# 顺序即优先级：一条同时命中多类时取靠前的。硬件排在最前是因为"缺真机"
# 比"缺人耳"更硬——设备不到，人来多少都测不了。
BLOCKED_WORDS = {
    "硬件": ("真机", "实机", "实物", "硬件", "设备", "外设", "样机"),
    "凭证": ("身份源", "凭证", "证书", "账号密码", "密钥", "登录态"),
    # 界限：这条要动的是包外的文件（改 Schema、改父包产物、改上游源）。
    # 单独成类是因为它和"上游缺文件"不同——文件在，但写界不让写。
    # 真事故：vd-004 的「demand_request.kind 要钉住必须改 Schema 加 CHECK，属写界外」
    # 被当成待办派成 vd-004-f1，可 f1 的写界同样不允许改 Schema，必然再写一页"做不了"。
    "界限": ("写界外", "写界之外", "允许写入范围外", "改 Schema", "改schema", "Schema 加",
             "改父包", "不改设计稿"),
    "上游": ("仓库只有", "只有一端", "缺源码", "缺代码", "缺素材", "上游未给", "未提供文件",
             "等构建产物", "等产物", "等上游", "端产物", "等数据", "等段名", "等另外",
             "等界面", "等真要用到", "等触发"),
    "人": ("真人", "人耳", "人工走查", "用户访谈", "受访者", "志愿者", "等人执行", "等人来"),
    "决策": ("信息架构决策", "产品决策", "视觉定调", "定调", "拍板", "审美取向",
             "视觉评审", "待评审", "需评审", "等产品", "等一句话", "等一句", "等决策",
             "一句话定", "未与人确认", "等人确认",
             # 「要一起定」「未定」是同一类：查证查不出答案，只能人定。
             # 真事故：vd-004 的「没有数据时怎么表示没有约定」「退役批次界面显示什么未定」
             # 两条被抽成 f1 的第 2、3 项。
             "一起定", "要定", "未定", "须产品", "需产品", "产品+后端"),
}

# 第三档：作者自己标了"不做也过"的。
# 这类留在汇报里是给读者看的余量说明，不是待办。真事故：f5 的
# 「focus 描边只剩 0–2px，未改也能过检查，按可选优化记」被抽成 f6 的第一项。
NOT_TODO_WORDS = ("可选优化", "不算未完成项", "不改也能过", "未改也能过", "非必须", "可选项")

# 第二档：认知边界
OPEN_KEYS = ("未证", "未核", "未实测")
# 只有上面全空时才退而用这个
NEXT_KEYS = ("建议下一步", "下一步建议", "下一轮最该做")

# 明确已处理完的措辞。注意：含"未/没/待/缺"的一律不判已解决——
# "已登记未修"这种既含"已登记"又含"未修"，必须留下。
CLOSED_WORDS = ("已修", "已出屏", "已定", "已核", "已完成", "已补", "不再算开口")
OPEN_WORDS = ("未", "没", "待", "缺", "只登记", "仍")

MAX_ITEMS = 12
MIN_ITEM_LEN = 6


# ---------------------------------------------------------------- 基础


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_kv(path: Path) -> dict:
    out = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------- 解析汇报


def _strip_md(s: str) -> str:
    s = re.sub(r"\*\*|__|`", "", s)
    s = re.sub(r"\[([ xX])\]\s*", "", s)
    s = re.sub(r"^\s*\[[ xX]\]\s*", "", s)
    return s.strip().strip("。；; ")


def _closed(t: str) -> bool:
    """这条是不是已经处理完了。"""
    # 删除线是作者亲手划掉的：划掉的条目即使还含"未/等/缺"，也是已作废，
    # 不是待办。f5 的「~~390 下按钮右下角点不到~~ —— 已推翻，不是缺陷」
    # 就被当成遗留项派过一次。
    if "~~" in t:
        return True
    if any(w in t for w in OPEN_WORDS):
        return False
    return any(w in t for w in CLOSED_WORDS)


def _not_todo(t: str) -> bool:
    """作者自己写了"不做也能过"的，别派。"""
    return any(w in t for w in NOT_TODO_WORDS)


# 汇报里常见的写法：「原问题写法 → 现状」。箭头后半句才是现状，
# 只看前半句会把已做完的整节当待办，再把节下的证据条目当遗留项派下去。
# 真实事故：vd-003-f1 的「…全未做 → 三项全做了」，被抽出 3 条"遗留"，
# 而那 3 条其实是实测证据（交互回归/无障碍/窄屏分别做了什么）。
DONE_TITLE_WORDS = ("做了", "已做", "已完成", "已收口", "已修", "已出屏", "已定", "已核", "已补")
REMAIN_WORDS = ("未", "没", "待", "缺", "剩", "仍", "部分")


def _status_text(title: str) -> str:
    """取「原问题 → 现状」里的现状那一半；没有箭头就整句。"""
    return title.rsplit("→", 1)[-1] if "→" in title else title


def _closed_title(title: str) -> bool:
    """整节是不是已经做完了（现状那半句既不表剩余、又含完成词）。"""
    st = _status_text(title)
    if any(w in st for w in REMAIN_WORDS):
        return False
    return any(w in st for w in DONE_TITLE_WORDS)


def blocked_kind(text: str):
    """这条是不是「机器在写界内做不动」。是就返回类别，否返回 None。

    只认窄词表，宁可漏判也不要误判：误判成 blocked 会让 Loop 提前收工，
    明明还能干的活被挂在"等人"上；漏判的代价只是多派一包，看得见。
    """
    for kind, words in BLOCKED_WORDS.items():
        if any(w in text for w in words):
            return kind
    return None


def _sections(md: str):
    """按标题切成 [(level, title, [body lines])]。"""
    out, cur = [], None
    for ln in md.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            if cur:
                out.append(cur)
            cur = {"level": len(m.group(1)), "title": m.group(2).strip(), "body": []}
        elif cur is not None:
            cur["body"].append(ln)
    if cur:
        out.append(cur)
    return out


def _norm(t: str) -> str:
    """比对用：去括号内容、去标点、去空白。"""
    t = re.sub(r"（[^）]*）|\([^)]*\)", "", t)
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", t)


def _bigrams(s: str):
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def _similar(a: str, b: str, th: float = 0.55) -> bool:
    A, B = _bigrams(_norm(a)), _bigrams(_norm(b))
    if not A or not B:
        return False
    return len(A & B) / len(A | B) >= th


def _is_branch(t: str) -> bool:
    """「答该 → 按 A 实现 / 答不该 → 本条关闭」是决策分支，不是待办。

    真实事故：vd-003-f4 的汇报在"未证点"里写了这两行，被 `_collect` 当成两条
    待办抽走，v5 包里就出现了"1. 答'该' → 按 A 实现"这种派给自己的怪活。
    分支是给读报告的人选路的，谁也不需要"去执行一个分支"。
    """
    if "→" not in t:
        return False
    head = t.split("→", 1)[0].strip()
    return head.startswith(("答", "若", "如果", "如答", "选", "取决于"))


def _collect(picked):
    """抽遗留项。列表项跨行写（缩进续行）要拼回同一项。

    真实事故：vd-003-f4 的「…那一行高了 26px，」在行末断开，
    「未与人确认是否可接受」在下一行。只取首行等于丢掉关键判断词，
    于是这条被判成"机器能做"，派进了 v5 包。
    """
    items: list = []
    for s in picked:
        buf = None

        def flush():
            nonlocal buf
            if buf is None:
                return
            txt, buf = _strip_md(buf), None
            if len(txt) < MIN_ITEM_LEN or _closed(txt) or _is_branch(txt) or _not_todo(txt):
                return
            # 同一件事常在"未证点"和"仍未修"各写一遍，措辞还不同。
            # 按 bigram 相似度去重，否则下一包会把同一件事派两次。
            if any(_similar(txt, prev) for _, prev in items):
                return
            items.append((s["title"], txt))

        for ln in s["body"]:
            m = re.match(r"^\s*(?:\d+[.)]|[-*+])\s+(.+)$", ln)
            if m:
                flush()
                buf = m.group(1)
            elif buf is not None and ln.strip():
                buf += " " + ln.strip()
            else:
                flush()
        flush()
    return items


def open_items(md: str):
    """抽出遗留项：[(section_title, text)]。

    分三档取，只取命中的最高一档——"仍未修"是明确待办，
    "未证点"是认知边界，"建议下一步"是方向。混在一起会把边界当待办派下去。
    """
    secs = _sections(md)
    for keys in (HARD_KEYS, OPEN_KEYS, NEXT_KEYS):
        picked = [
            s
            for s in secs
            if any(k in s["title"] for k in keys) and not _closed_title(s["title"])
        ]
        if picked:
            items = _collect(picked)
            if items:
                return items[:MAX_ITEMS]
    return []


# ---------------------------------------------------------------- 选父包


def resolved_marks(state_dir: Path, pid: str):
    """读 <id>.recheck.md 里的 RESOLVED 行。

    为什么不直接改 <id>.last.md：那是这一包的交付物，改它等于篡改历史。
    另开一个复审视账文件，机器读最新状态，人看得到改动轨迹。
    """
    p = state_dir / "pkg" / f"{pid}.recheck.md"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*[-*]?\s*RESOLVED\s*[:：]\s*(.+)$", ln, re.I)
        if m:
            out.append(m.group(1).strip())
    return out


# ---------------------------------------------------------------- 台账

BLOCKED_KIND_ORDER = ("人", "硬件", "界限", "上游", "凭证", "决策")


def blocked_store(state_dir: Path) -> dict:
    data = read_json(state_dir / "blocked.json", {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("items", [])
    return data


def _render_blocked(state_dir: Path, store: dict) -> str:
    rows = [it for it in store.get("items", []) if it.get("status") != "closed"]
    lines = [
        "# 机器做不动的项（需要外部输入）",
        "",
        "> 这张表由 `followup.py` 自动记账，**不是待办队列**。",
        "> Loop 不会为这里任何一条派新包 —— 派下去唯一的结果是写一句「做不了」，",
        "> 下一包再把它抽出来派一遍，烧掉一整个预算周期，一行代码都没改。",
        "> 你处理完一条：把 `goal-state/blocked.json` 里该项 `status` 改成 `closed`，",
        "> 或在对应包的 `<pid>.recheck.md` 写一行 `RESOLVED: <原话>`。",
        "",
        f"共 {len(rows)} 项待外部输入。",
        "",
    ]
    for kind in BLOCKED_KIND_ORDER:
        group = [it for it in rows if it.get("kind") == kind]
        if not group:
            continue
        lines.append(f"## {kind}（{len(group)} 项）")
        lines.append("")
        for it in group:
            src = it.get("parent", "")
            seen = (it.get("first_seen") or "")[:10]
            lines.append(f"- {it.get('text', '')}  ·  ← {src} {seen}".rstrip())
        lines.append("")
    other = [it for it in rows if it.get("kind") not in BLOCKED_KIND_ORDER]
    if other:
        lines.append(f"## 其他（{len(other)} 项）")
        lines.append("")
        for it in other:
            lines.append(f"- {it.get('text', '')}  ·  ← {it.get('parent', '')}")
        lines.append("")
    return "\n".join(lines)


def park_blocked(state_dir: Path, parent_id: str, items) -> int:
    """把做不动的项记进台账。返回新增条数（已在表里的不重复记）。

    `items` 是 [(text, kind)]。同一句话换了措辞再次出现也要认出来，
    否则每轮回流都会往表里堆一条同义的废话。
    """
    store = blocked_store(state_dir)
    rows = store["items"]
    added = 0
    for text, kind in items:
        # 去重要连 closed 一起看：人工把一条标成 closed 就是"这条别再提了"。
        # 早先只比 open 项，结果每轮回流都把它重新记成 open —— 实测一次
        # 就多 3 条（9 → 12），status.sh 的"另有 N 项"也跟着虚高。
        dup = any(_similar(text, it.get("text", ""), 0.75) for it in rows)
        if dup:
            continue
        rows.append(
            {
                "text": text,
                "kind": kind,
                "parent": parent_id,
                "first_seen": now_iso(),
                "status": "open",
            }
        )
        added += 1
    if added:
        write_json_atomic(state_dir / "blocked.json", store)
        (state_dir / "blocked.md").write_text(_render_blocked(state_dir, store), encoding="utf-8")
    return added


def _split_id(pid: str):
    """把 vd-003-f2 拆成 ("vd-003", 2)。

    早先版本用 f"{pid}-f{depth}" 拼新 id，结果 vd-003-f1 会生成
    vd-003-f1-f2，名字越滚越长，且 _depth 只看末尾一个 -fN，
    下次仍算成第 2 层 → 生成的 id 与上一次完全相同。这是个真 bug。
    改成"根号 + 层号"：vd-003 → vd-003-f1 → vd-003-f2。
    从哪个父来不丢，followup_of 字段里记着。
    """
    # 层号就是后缀里的那个数字本身，不是"-fN"出现的次数。
    # 早先按出现次数算，t-001-f2 会被算成第 1 层，再追一次还是生成 t-001-f2，id 直接撞车。
    m = re.match(r"^(.*?)-f(\d+)$", pid)
    if not m:
        return pid, 0  # 从没被追过
    return m.group(1), int(m.group(2))


def last_delivered(queue: dict):
    cands = [
        p
        for p in queue.get("packages", [])
        if p.get("status") in ("delivered", "done") and p.get("delivered_at")
    ]
    if not cands:
        return None
    return max(cands, key=lambda p: p.get("delivered_at") or "")


def find_pkg(queue: dict, pid: str):
    return next((p for p in queue.get("packages", []) if p.get("id") == pid), None)


# ---------------------------------------------------------------- 生成包


def build_instruction(new_id, parent, items, state_dir: Path) -> str:
    scope = parent.get("write_scope") or []
    scope_txt = "\n".join(f"- {s}" for s in scope) if scope else "- （父包未声明写界，只能新增汇报文件）"
    item_txt = "\n".join(f"{i}. {t}" for i, (_, t) in enumerate(items, 1))
    from_sec = sorted({s for s, _ in items})

    return f"""# 收口任务 {new_id}（自动回流自 {parent['id']}）

## 由来

`{parent['id']}`（{parent.get('title', '')}）交付后，汇报
`{state_dir / 'pkg' / (parent['id'] + '.last.md')}`
在「{'、'.join(from_sec)}」里留了 {len(items)} 项没有处理完。

这一包**只做这些**，不新增主题、不扩大范围。上一轮已经把能做的做完了，
剩下的是"知道有问题但没动手"的部分——现在动手，或者给出为什么动不了的确定依据。

## 必须逐条处理

{item_txt}

## 处理要求

- 每条给一个**确定结论**，二选一：
  a) 改掉了 —— 写明改了哪个文件、哪一处、改前改后是什么；
  b) 做不了 —— 写明卡在哪一环（缺文件 / 缺权限 / 写界外 / 需要人决策），
     并给出可执行的下一位（谁、在哪做、要什么输入）。
- **不接受把"仍未修"原样留在汇报里**。上一包已经这样留过一次了。
- 如果某一条查证后发现"其实已经是对的"，要写明用什么证据推翻的，不要默默删掉。
- 如果某一条在执行中发现前提不成立（例如上游文件已变），写明前提怎么变的。

## 禁止

- 不改父包已经交付的文件内容（除非某条遗留项本身就是改它——那种要写明理由）。
- 不执行 git 提交、不跑导航生成脚本、不新增依赖安装。
- 不改写界之外的文件。

## 工作目录

{parent.get('workdir', '')}

## 允许写入

{scope_txt}

## 完成协议

1. 使用绝对路径访问所有文件；工作目录以上文为准。
2. 完成后，把最终汇报（改动文件清单 / 已跑验证与结果 / 未证点 / 建议下一步）
   用 UTF-8 写入：
   `{state_dir / 'pkg' / (new_id + '.last.md')}`
3. 只写上述汇报文件和允许范围内的文件，不要改动其它任何文件，不要执行 git 提交。
4. 若某条无法完成，也要在该汇报文件里写明原因，不要空着。
"""


def plan(state_dir: Path, queue: dict, cfg: dict, force_from: str = None, write: bool = True):
    """算出该不该长新包、长什么样。返回 dict，带 skip 表示不长。"""
    if force_from:
        parent = find_pkg(queue, force_from)
        if parent is None:
            return {"skip": "parent_not_found", "parent": force_from}
    else:
        parent = last_delivered(queue)
        if parent is None:
            return {"skip": "no_delivered_pkg"}

    pid = parent["id"]

    # 被撤销的包不算"已回流"，否则撤掉一个误派包之后，父包就永远追不动了。
    if any(
        p.get("followup_of") == pid and p.get("status") != "cancelled"
        for p in queue.get("packages", [])
    ):
        return {"skip": "already_spawned", "parent": pid}

    # 默认不设人工闸门（FOLLOWUP_REQUIRE_DONE: off）：引擎交付即算放行。
    # 要回到"每包必须 ./review.sh <id> done 才推下一步"，把它设成 on。
    require_done = (cfg.get("FOLLOWUP_REQUIRE_DONE", "off") or "off").strip().lower()
    if not force_from and require_done == "on" and parent.get("status") != "done":
        return {"skip": "parent_not_reviewed", "parent": pid}

    md_path = state_dir / "pkg" / f"{pid}.last.md"
    if not md_path.exists():
        return {"skip": "no_last_md", "parent": pid, "path": str(md_path)}

    items = open_items(md_path.read_text(encoding="utf-8"))
    marks = resolved_marks(state_dir, pid)
    dropped = []
    if marks:
        kept = []
        for sec, t in items:
            hit = next((r for r in marks if _similar(t, r, 0.45)), None)
            if hit:
                dropped.append((t, hit))
            else:
                kept.append((sec, t))
        items = kept
    # 拆掉审批之后，唯一还会让 Loop 空转的就是"把做不动的活再派一遍"。
    # 这类项要的是外部输入（真人/真机/身份源/上游文件/人拍板），不是下一轮
    # 执行。所以记账、不派包、Loop 继续往前跑 —— 记的是"人等"，不是"等审批"。
    blocked = []
    if (cfg.get("FOLLOWUP_PARK", "on") or "on").strip().lower() != "off":
        rest = []
        for sec, t in items:
            k = blocked_kind(t)
            if k:
                blocked.append((t, k))
            else:
                rest.append((sec, t))
        items = rest
        if blocked and write:
            park_blocked(state_dir, pid, blocked)

    if not items:
        if blocked:
            return {"skip": "all_blocked", "parent": pid, "blocked": blocked, "dropped": dropped}
        return {"skip": "clean", "parent": pid, "dropped": dropped}

    # 自动模式没有人工闸门，唯一能自己判断的刹车是"这一轮到底有没有新东西"：
    # 若新抽出的遗留项逐条都与父包当初被派去的项高度重合（≥0.9），说明它只是
    # 把同一批"做不了"的话又抄了一遍，再派下去就是空烧额度、同一份稿反复改。
    # 这是收敛判据，不是审批：判的是"有没有进展"，不是"人同不同意"。
    if (cfg.get("FOLLOWUP_NO_PROGRESS", "on") or "on").strip().lower() != "off":
        prev = parent.get("followup_items") or []
        if prev and all(any(_similar(t, o, 0.9) for o in prev) for _, t in items):
            return {"skip": "no_progress", "parent": pid, "items": len(items)}

    root, parent_depth = _split_id(pid)
    depth = parent_depth + 1  # 新包的层号 = 父层号 + 1
    # 0 或负数 = 不限层。无限层是"无人值守长跑"的正当配置：停下来的判据交给
    # no_progress（原地打转）、clean（没遗留了）、STOP（人工刹停），而不是数层数。
    try:
        max_depth = int((cfg.get("FOLLOWUP_MAX_DEPTH", "2") or "2").strip())
    except ValueError:
        max_depth = 2
    if max_depth > 0 and depth > max_depth:
        return {"skip": "max_depth", "parent": pid, "depth": depth, "max": max_depth}

    new_id = f"{root}-f{depth}"
    pkg = {
        "id": new_id,
        "title": f"收口 {pid} 遗留的 {len(items)} 项",
        "instruction": build_instruction(new_id, parent, items, state_dir),
        "workdir": parent.get("workdir", ""),
        "write_scope": parent.get("write_scope") or [],
        "checks": [
            f"上面 {len(items)} 条逐条有结论（改掉了，或写明做不了的原因）",
            "除允许写入范围外没有任何文件改动",
            "未执行 git 提交、未跑导航生成脚本",
        ],
        "deps": [pid],
        "status": "pending",
        "fail_count": 0,
        "token_budget": parent.get("token_budget", "none"),
        "timeout_s": parent.get("timeout_s", 1800),
        "followup_of": pid,
        "followup_depth": depth,
        "followup_items": [t for _, t in items],
        "created_at": now_iso(),
    }
    return {"spawn": pkg, "parent": pid, "items": items, "dropped": dropped, "blocked": blocked}


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=os.environ.get("GOAL_STATE_DIR", str(SCRIPT_DIR / "goal-state")))
    ap.add_argument("--from", dest="force_from", default=None, help="指定父包 id")
    ap.add_argument("--list", action="store_true", help="列出各包的遗留项数")
    ap.add_argument("--dry-run", action="store_true", help="只看，不写")
    ap.add_argument("--apply", action="store_true", help="真写进 queue.json")
    args = ap.parse_args()

    state_dir = Path(args.state_dir).expanduser().resolve()
    queue = read_json(state_dir / "queue.json", {"packages": []})
    cfg = parse_kv(state_dir / "goal.md")

    if args.list:
        for p in queue.get("packages", []):
            md = state_dir / "pkg" / f"{p['id']}.last.md"
            if not md.exists():
                print(f"{p['id']:<14} {p.get('status', ''):<12} 遗留 —（无 last.md）")
                continue
            items = open_items(md.read_text(encoding="utf-8"))
            marks = resolved_marks(state_dir, p["id"])
            kept = [t for _, t in items if not any(_similar(t, m, 0.45) for m in marks)]
            cut = len(items) - len(kept)
            flag = f"{len(kept)} 项" + (f"（已收口 {cut}）" if cut else "")
            kids = [x for x in queue["packages"] if x.get("followup_of") == p["id"]]
            alive = [x for x in kids if x.get("status") != "cancelled"]
            spawned = "已回流" if alive else ("已撤销" if kids else "")
            print(f"{p['id']:<14} {p.get('status', ''):<12} 遗留 {flag:<22} {spawned}")
        n_blocked = len([it for it in blocked_store(state_dir)["items"] if it.get("status") != "closed"])
        if n_blocked:
            print(f"\n  另有 {n_blocked} 项机器做不动（要人/真机/上游/凭证/拍板）→ goal-state/blocked.md")
        return 0

    r = plan(state_dir, queue, cfg, args.force_from, write=args.apply)

    if "skip" in r:
        print(f"[skip] {r['skip']}" + (f"（{r.get('parent')}）" if r.get("parent") else ""))
        if r["skip"] == "parent_not_reviewed":
            print("  父包还没被人工放行。先 ./review.sh " + r["parent"] + " done，再让 daemon 下一拍自动追。")
        if r["skip"] == "clean":
            print("  这一包没有遗留项 —— Loop 到此为止，是正常收敛。")
        if r["skip"] == "all_blocked":
            print(
                f"  剩下的 {len(r['blocked'])} 项全都要外部输入"
                "（人 / 真机 / 写界外 / 上游文件 / 凭证 / 人拍板），"
                "已记进 blocked.md，不派包。"
            )
            print("  Loop 停在这里是收敛，不是卡住：等外部输入到位，删掉 blocked 里那行再开 FOLLOWUP。")
        if r["skip"] == "max_depth":
            print(f"  深度 {r['depth']} 已超上限 {r['max']}。要么手动加包，要么接受收敛。")
        return 0

    pkg, items = r["spawn"], r["items"]
    dropped = r.get("dropped") or []
    print(f"[plan] {pkg['id']}  ←  {r['parent']} 的 {len(items)} 项遗留")
    if dropped:
        print(f"  （另有 {len(dropped)} 项已在 recheck 里标 RESOLVED，不再派）")
        for t, hit in dropped:
            print(f"    - 已收口：{t[:50]}")
    for i, (sec, t) in enumerate(items, 1):
        print(f"  {i}. [{sec}] {t[:70]}{'…' if len(t) > 70 else ''}")
    blocked = r.get("blocked") or []
    if blocked:
        print(f"  （另有 {len(blocked)} 项机器做不动，记账进 blocked.md，不进本包）")
        for t, k in blocked:
            print(f"    - [{k}] {t[:60]}")

    if not args.apply:
        print("\n（未写盘。加 --apply 生效）")
        return 0

    queue.setdefault("packages", []).append(pkg)
    write_json_atomic(state_dir / "queue.json", queue)
    print(f"\n[written] {state_dir / 'queue.json'} 新增 pending 包 {pkg['id']}")
    print("daemon 下一拍会把它升为 ready 并派发（deps 指回父包，父包 delivered 即满足）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
