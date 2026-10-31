#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test-engine-fixes.py —— 针对 2026-10-31 三处修复的回归测试。

背景（都是真实事故，不是假想）：
  A. engine-wbrun.sh 计时从"提交"起算，排队时间也吃预算 → 有包排队 1190s 只剩 600s 干活。
  B. engine-wbrun.sh 超时分支无条件 exit 3，不认已落盘的 last.md
     → vd-003-f1 21:16 已交付，21:27 仍被判失败，fail_count 被伪造到 2。
  C. goal-daemon.py 派活前不检查 last.md 是否已存在 → 同一包被派三次。

本测试不碰真实网关、不碰真实 goal-state：
  - 用假 HTTP 网关逼出 engine-wbrun.sh 的超时分支；
  - 用假 engine 脚本验证 daemon 的"已有产出"短路。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE / "engine-wbrun.sh"
DAEMON = HERE / "goal-daemon.py"

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  " + detail) if detail else ""))


# ---------------------------------------------------------------- 假网关

class FakeGateway:
    """永远不把 run 标记为完成；按需在轮询中途"写出"报告文件，模拟 agent 交付。"""

    def __init__(self, write_last_after=None, last_path=None):
        self.write_last_after = write_last_after  # 第 N 次轮询后写产出
        self.last_path = last_path
        self.polls = 0
        self.runs_posted = 0
        gw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.endswith("/api/v1/status"):
                    return self._send({"data": {"status": "ok", "busy": True,
                                                "activeSessionId": "fake-session",
                                                "runStatus": "tool_executing"}})
                if "/api/v1/runs/" in self.path:
                    gw.polls += 1
                    if gw.write_last_after and gw.polls >= gw.write_last_after:
                        Path(gw.last_path).write_text("# 假产出\n由假网关在轮询中途写入\n", encoding="utf-8")
                    # 永远 active:true —— 逼出超时分支
                    return self._send({"data": {"runId": "fake-run", "active": True}})
                return self._send({"data": {}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                self.rfile.read(n)
                gw.runs_posted += 1
                return self._send({"data": {"runId": "fake-run"}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def make_state(tmp, timeout_s=3, poll_s=1, grace_s=1):
    st = Path(tmp)
    (st / "pkg").mkdir(parents=True, exist_ok=True)
    (st / "logs").mkdir(parents=True, exist_ok=True)
    (st / "goal.md").write_text(
        "ENGINE: wbrun\n"
        f"WBRUN_TIMEOUT_S: {timeout_s}\n"
        f"WBRUN_POLL_S: {poll_s}\n"
        f"WBRUN_OUTPUT_GRACE_S: {grace_s}\n",
        encoding="utf-8",
    )
    (st / "gateway.token").write_text("fake-token\n", encoding="utf-8")
    return st


def run_engine(st, gw, pkg="t-fix"):
    (st / "gateway.port").write_text(str(gw.port), encoding="utf-8")
    (st / "pkg" / f"{pkg}.task.md").write_text("假任务\n", encoding="utf-8")
    env = dict(os.environ, GOAL_STATE_DIR=str(st))
    p = subprocess.run(["bash", str(ENGINE), pkg], env=env,
                       capture_output=True, text=True, timeout=120)
    exitf = st / "pkg" / f"{pkg}.exit"
    code = int(exitf.read_text().strip()) if exitf.exists() else None
    elog = st / "logs" / f"engine-{pkg}.log"
    log = elog.read_text(encoding="utf-8") if elog.exists() else ""
    return p, code, log


def t_engine_timeout_with_output():
    """A/B：超时但产出已落盘 → 必须 exit 0，不能判失败。"""
    tmp = tempfile.mkdtemp(prefix="fix-a-")
    gw = None
    try:
        st = make_state(tmp, timeout_s=3, poll_s=1, grace_s=1)
        gw = FakeGateway(write_last_after=2, last_path=st / "pkg" / "t-fix.last.md")
        p, code, log = run_engine(st, gw)
        check("超时+产出已落盘 → exit 0", code == 0, f"exit={code}")
        check("引擎日志记 output present", "output present" in log,
              log.strip().splitlines()[-1] if log.strip() else "(空)")
    finally:
        if gw:
            gw.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_engine_timeout_without_output():
    """对照：真的没产出 → 仍须 exit 3，不能因为改宽了就放过。"""
    tmp = tempfile.mkdtemp(prefix="fix-b-")
    gw = None
    try:
        st = make_state(tmp, timeout_s=3, poll_s=1, grace_s=1)
        gw = FakeGateway(write_last_after=None, last_path=st / "pkg" / "t-fix.last.md")
        p, code, log = run_engine(st, gw)
        check("超时且无产出 → exit 3", code == 3, f"exit={code}")
        check("引擎日志记 run timeout", "run timeout" in log,
              log.strip().splitlines()[-1] if log.strip() else "(空)")
    finally:
        if gw:
            gw.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_engine_stale_output():
    """残留的旧汇报不能当本轮产出（否则重跑会秒判成功）。"""
    tmp = tempfile.mkdtemp(prefix="fix-b2-")
    gw = None
    try:
        st = make_state(tmp, timeout_s=3, poll_s=1, grace_s=1)
        stale = st / "pkg" / "t-fix.last.md"
        stale.write_text("上一轮的旧汇报\n", encoding="utf-8")
        old = 1_600_000_000  # 2020 年，远早于 START_TS
        os.utime(stale, (old, old))
        gw = FakeGateway(write_last_after=None, last_path=stale)
        p, code, log = run_engine(st, gw)
        check("残留旧产出 → 仍 exit 3", code == 3, f"exit={code}")
        check("引擎日志识别 stale", "stale" in log or "no output" in log,
              log.strip().splitlines()[-1] if log.strip() else "(空)")
    finally:
        if gw:
            gw.close()
        shutil.rmtree(tmp, ignore_errors=True)


def t_daemon_skips_when_output_exists():
    """C：last.md 已存在 → 直接判 delivered，不再派活（不产生 lock、不调引擎）。"""
    tmp = tempfile.mkdtemp(prefix="fix-c-")
    try:
        st = Path(tmp)
        (st / "pkg").mkdir(parents=True, exist_ok=True)
        (st / "logs").mkdir(parents=True, exist_ok=True)
        (st / "goal.md").write_text("ENGINE: custom\nMAX_FAIL: 3\n", encoding="utf-8")
        (st / "pkg" / "t-d.task.md").write_text("假任务\n", encoding="utf-8")
        (st / "pkg" / "t-d.last.md").write_text("已经交付的汇报\n", encoding="utf-8")
        (st / "queue.json").write_text(json.dumps({"packages": [{
            "id": "t-d", "title": "t", "instruction": "t", "workdir": "/tmp",
            "write_scope": [], "checks": [], "deps": [], "status": "ready",
            "fail_count": 0, "token_budget": "none", "timeout_s": 60,
        }]}, ensure_ascii=False), encoding="utf-8")

        fake_engine = st / "fake-engine.sh"
        fake_engine.write_text('#!/bin/bash\nexit 0\n', encoding="utf-8")
        fake_engine.chmod(0o755)

        env = dict(os.environ, GOAL_STATE_DIR=str(st))
        p = subprocess.run([sys.executable, str(DAEMON), "--state-dir", str(st),
                            "--once", "--engine", str(fake_engine)],
                           env=env, capture_output=True, text=True, timeout=60)
        q = json.loads((st / "queue.json").read_text(encoding="utf-8"))
        pkg = q["packages"][0]
        log = ""
        for f in (st / "logs").glob("*.log"):
            log += f.read_text(encoding="utf-8")
        check("已有产出 → status=done（免审直接接受）", pkg["status"] == "done", pkg["status"])
        check("已有产出 → 记 reviewed_by=auto", pkg.get("reviewed_by") == "auto", pkg.get("reviewed_by"))
        check("已有产出 → 不产生 lock（未派活）", not (st / "lock.json").exists())
        check("已有产出 → 记 delivered_existing_output", "delivered_existing_output" in log)
        check("已有产出 → fail_count 归零", int(pkg.get("fail_count", 0)) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_daemon_still_spawns_first_time():
    """对照：没有 last.md 时仍须正常派活，别把短路写成"永远不干活"。"""
    tmp = tempfile.mkdtemp(prefix="fix-c2-")
    try:
        st = Path(tmp)
        (st / "pkg").mkdir(parents=True, exist_ok=True)
        (st / "logs").mkdir(parents=True, exist_ok=True)
        (st / "goal.md").write_text("ENGINE: custom\nMAX_FAIL: 3\n", encoding="utf-8")
        (st / "pkg" / "t-e.task.md").write_text("假任务\n", encoding="utf-8")
        (st / "queue.json").write_text(json.dumps({"packages": [{
            "id": "t-e", "title": "t", "instruction": "t", "workdir": "/tmp",
            "write_scope": [], "checks": [], "deps": [], "status": "ready",
            "fail_count": 0, "token_budget": "none", "timeout_s": 60,
        }]}, ensure_ascii=False), encoding="utf-8")
        fake_engine = st / "fake-engine.sh"
        fake_engine.write_text('#!/bin/bash\nsleep 5\nexit 0\n', encoding="utf-8")
        fake_engine.chmod(0o755)

        env = dict(os.environ, GOAL_STATE_DIR=str(st))
        subprocess.run([sys.executable, str(DAEMON), "--state-dir", str(st),
                        "--once", "--engine", str(fake_engine)],
                       env=env, capture_output=True, text=True, timeout=60)
        q = json.loads((st / "queue.json").read_text(encoding="utf-8"))
        check("无产出 → 正常派活（in_progress + lock）",
              q["packages"][0]["status"] == "in_progress" and (st / "lock.json").exists(),
              q["packages"][0]["status"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_daemon_review_on_keeps_delivered():
    """F：显式开审批（FOLLOWUP_REQUIRE_DONE: on）时必须退回 delivered。

    免审是默认，但不能把开关做成死代码——要人把关的场合得真能挡住并等人放行。
    """
    tmp = tempfile.mkdtemp(prefix="fix-f-")
    try:
        st = Path(tmp)
        (st / "pkg").mkdir(parents=True, exist_ok=True)
        (st / "logs").mkdir(parents=True, exist_ok=True)
        (st / "goal.md").write_text(
            "ENGINE: custom\nMAX_FAIL: 3\nFOLLOWUP_REQUIRE_DONE: on\n", encoding="utf-8"
        )
        (st / "pkg" / "t-r.task.md").write_text("假任务\n", encoding="utf-8")
        (st / "pkg" / "t-r.last.md").write_text("汇报\n", encoding="utf-8")
        (st / "queue.json").write_text(json.dumps({"packages": [{
            "id": "t-r", "title": "t", "instruction": "t", "workdir": "/tmp",
            "write_scope": [], "checks": [], "deps": [], "status": "ready",
            "fail_count": 0, "token_budget": "none", "timeout_s": 60,
        }]}, ensure_ascii=False), encoding="utf-8")
        fake_engine = st / "fake-engine.sh"
        fake_engine.write_text('#!/bin/bash\nexit 0\n', encoding="utf-8")
        fake_engine.chmod(0o755)

        env = dict(os.environ, GOAL_STATE_DIR=str(st))
        subprocess.run([sys.executable, str(DAEMON), "--state-dir", str(st),
                        "--once", "--engine", str(fake_engine)],
                       env=env, capture_output=True, text=True, timeout=60)
        pkg = json.loads((st / "queue.json").read_text(encoding="utf-8"))["packages"][0]
        check("审批模式 → delivered（等人放行）", pkg["status"] == "delivered", pkg["status"])
        check("审批模式 → 不记 reviewed_by", not pkg.get("reviewed_by"), pkg.get("reviewed_by"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_config_values():
    """超时预算与宽限是否真的落在配置里（改代码不改配置等于没改）。

    读 **template** 而不是 goal.md：仓库该保证的是"出厂默认值对"，
    因为 install.sh 在没有 goal.md 时正是从 template 复制。读某台机器上的
    goal.md 会让这个测试在干净 clone 里直接崩（曾真实发生：新机器第一次
    跑测试就 FileNotFoundError），而且本机改过的值不能代表默认行为。
    """
    tpl = HERE / "goal-state" / "goal.md.template"
    md = HERE / "goal-state" / "goal.md"
    src = tpl if tpl.exists() else md
    if not src.exists():
        check("配置文件存在（goal.md.template）", False, f"{tpl} 与 {md} 都没有")
        return
    txt = src.read_text(encoding="utf-8")
    t = re.search(r"^WBRUN_TIMEOUT_S:\s*(\d+)", txt, re.M)
    g = re.search(r"^WBRUN_OUTPUT_GRACE_S:\s*(\d+)", txt, re.M)
    check("WBRUN_TIMEOUT_S >= 3600", bool(t) and int(t.group(1)) >= 3600,
          f"={t.group(1)}" if t else "缺失")
    check("WBRUN_OUTPUT_GRACE_S 已配置", bool(g), f"={g.group(1)}" if g else "缺失")
    # 免审 + 不限层 + 原地打转自停：三个开关都必须真落在配置里
    r = re.search(r"^FOLLOWUP_REQUIRE_DONE:\s*(\S+)", txt, re.M)
    d = re.search(r"^FOLLOWUP_MAX_DEPTH:\s*(\d+)", txt, re.M)
    n = re.search(r"^FOLLOWUP_NO_PROGRESS:\s*(\S+)", txt, re.M)
    check("FOLLOWUP_REQUIRE_DONE=off（免审）", bool(r) and r.group(1).lower() == "off",
          f"={r.group(1)}" if r else "缺失")
    check("FOLLOWUP_MAX_DEPTH=0（不限层）", bool(d) and int(d.group(1)) == 0,
          f"={d.group(1)}" if d else "缺失")
    check("FOLLOWUP_NO_PROGRESS 已开启", bool(n) and n.group(1).lower() != "off",
          f"={n.group(1)}" if n else "缺失")


def t_followup_closed_section():
    """E：汇报写「原问题 → 现状」时，已完成的那节不能被当成待办。

    真实事故：vd-003-f1 的「…全未做 → **三项全做了**」，被抽出 3 条"遗留"，
    而那 3 条其实是实测证据（交互回归/无障碍/窄屏分别做了什么）。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("fu", HERE / "followup.py")
    fu = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fu)

    md_done = """
## 一、结论

### 2. 交互回归 / 无障碍实测 / <1080px 窄屏实测全未做 → **三项全做了**

- 交互回归：12 段逐段点开校验，130+ 次点击失败 0
- 无障碍：查出 4 类缺陷，全部修掉并复测
- 窄屏：五档横向溢出 0 px

### 3. "已办"真实性、真实 ACL 仍无 → **原型层已证**

- 可提交控件 0 / 51 动作项
"""
    got = fu.open_items(md_done)
    check("已完成节不被当待办（0 项）", len(got) == 0, f"抽到 {len(got)} 项")

    md_open = """
## 三、仍未修（不冒充已解决）

- 真实 ACL 仍然没有，需要命令层
- 窄屏段条横滑的取舍还没定
"""
    got = fu.open_items(md_open)
    check("真待办照常抽出（2 项）", len(got) == 2, f"抽到 {len(got)} 项")

    md_part = """
## 三、仍未修

### 分片 A 未做 → 已修 1 项，还剩 2 项

- 剩下两条要命令层配合
"""
    got = fu.open_items(md_part)
    check("部分完成仍要抽（≥1 项）", len(got) >= 1, f"抽到 {len(got)} 项")


def main():
    print("A/B. 超时分支认不认已落盘的产出")
    t_engine_timeout_with_output()
    print("B2. 残留旧产出不能冒充本轮")
    t_engine_stale_output()
    print("对照. 真没产出仍须判失败")
    t_engine_timeout_without_output()
    print("C. 已有产出不再派活")
    t_daemon_skips_when_output_exists()
    print("对照. 首次仍须正常派活")
    t_daemon_still_spawns_first_time()
    print("D. 配置生效")
    t_config_values()
    print("E. 汇报解析：已完成节不当待办")
    t_followup_closed_section()
    print("F. 审批模式仍能退回 delivered")
    t_daemon_review_on_keeps_delivered()
    print(f"\n结果：{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
