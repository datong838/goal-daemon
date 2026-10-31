#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codebuddy 后端专项测试（零成本：用假 codebuddy，不联网、不耗额度、不需要登录）。

覆盖：
  D. 成功路径 —— JSON result 被提取成 last.md，session.id 生成，首次用 --session-id
  E. 续接 —— 第二次调用必须走 --resume 且复用同一 session id
  F. 未登录 —— codebuddy 退出码是 0（真机实测如此），必须靠 stderr 判出 92 并落 .need-login
  G. 空输出 —— 不得判成功（否则就是"假交付"）
  H. daemon 侧 —— 存在 .need-login 时不派活、不计失败，等人工登录后自愈
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = "/usr/bin/python3"
T = ROOT / ".cbtest-run"
MOCK = T / "mock-codebuddy.sh"
ARGLOG = T / "mock-args.log"
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {detail}")


def setup(pkg_ids):
    shutil.rmtree(T, ignore_errors=True)
    for d in ("goal-state/logs", "goal-state/pkg", "work"):
        (T / d).mkdir(parents=True, exist_ok=True)
    (T / "goal-state" / "goal.md").write_text(
        "OBJECTIVE: codebuddy backend test\nENGINE: codebuddy\n"
        "MAX_FAIL: 3\nMAX_PKG_SECONDS: 3600\nNOTIFY: none\n",
        encoding="utf-8",
    )
    pkgs = [
        {
            "id": i,
            "title": i,
            "instruction": "写 hello",
            "workdir": str(T / "work"),
            "write_scope": [str(T / "work")],
            "checks": [],
            "deps": [],
            "status": "ready",
            "fail_count": 0,
        }
        for i in pkg_ids
    ]
    (T / "goal-state" / "queue.json").write_text(
        json.dumps({"packages": pkgs}, ensure_ascii=False), encoding="utf-8"
    )
    MOCK.write_text(
        "#!/bin/bash\n"
        'echo "$*" >> "' + str(ARGLOG) + '"\n'
        'case "${MOCK_MODE:-ok}" in\n'
        '  ok) echo \'{"result":"mock 完成：已写 hello","session_id":"mock"}\' ;;\n'
        '  nologin) echo "Authentication required. Please use /login command to sign in to your account" >&2 ;;\n'
        '  empty) : ;;\n'
        'esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    MOCK.chmod(0o755)


def run_engine(pkg_id, mode="ok"):
    env = dict(
        os.environ,
        GOAL_STATE_DIR=str(T / "goal-state"),
        PKG_WORKDIR=str(T / "work"),
        CODEBUDDY_BIN=str(MOCK),
        MOCK_MODE=mode,
        MOCK_ARGLOG=str(ARGLOG),
    )
    r = subprocess.run(
        ["/bin/bash", str(ROOT / "engine.sh"), pkg_id],
        env=env, capture_output=True, text=True, timeout=120,
    )
    return r.returncode, r.stdout + r.stderr


def tick():
    env = dict(os.environ, GOAL_STATE_DIR=str(T / "goal-state"), CODEBUDDY_BIN=str(MOCK), MOCK_MODE="ok")
    r = subprocess.run(
        [PY, str(ROOT / "goal-daemon.py"), "--once", "--engine", str(ROOT / "engine.sh")],
        env=env, capture_output=True, text=True, timeout=60,
    )
    return r.returncode, r.stdout + r.stderr


def events():
    out = []
    for f in sorted((T / "goal-state" / "logs").glob("*.log")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    out.append(json.loads(line)["event"])
                except Exception:
                    pass
    return out


def queue():
    return json.loads((T / "goal-state" / "queue.json").read_text(encoding="utf-8"))


print("D. 成功路径：JSON result -> last.md，首次用 --session-id")
setup(["d-01"])
rc, out = run_engine("d-01", "ok")
check("engine.sh 退出码 0", rc == 0, out[-300:])
last = T / "goal-state" / "pkg" / "d-01.last.md"
check("last.md 已生成", last.exists())
if last.exists():
    body = last.read_text(encoding="utf-8")
    check("内容取自 JSON 的 result 字段", "mock 完成" in body, body[:200])
sess_file = T / "goal-state" / "session.id"
check("session.id 已生成", sess_file.exists() and len(sess_file.read_text().strip()) > 10,
      sess_file.read_text() if sess_file.exists() else "缺失")
check("已完成标记存在", (T / "goal-state" / ".session.started").exists())
args1 = ARGLOG.read_text(encoding="utf-8") if ARGLOG.exists() else ""
check("首次调用用 --session-id", "--session-id" in args1, args1[:200])

print("\nE. 续接：第二次必须 --resume 且复用同一 session")
rc, out = run_engine("d-01", "ok")
lines = [l for l in ARGLOG.read_text(encoding="utf-8").splitlines() if l.strip()]
args2 = lines[-1] if lines else ""
check("第二次调用用 --resume", "--resume" in args2, args2[:200])
sess = sess_file.read_text().strip()
check("复用同一 session id", sess in args2, f"sess={sess} args={args2[:160]}")

print("\nF. 未登录：退出码 0 但必须判 92，并落 .need-login")
setup(["f-01"])
rc, out = run_engine("f-01", "nologin")
check("engine.sh 返回 92", rc == 92, f"rc={rc} {out[-200:]}")
check("已落 .need-login 标记", (T / "goal-state" / ".need-login").exists())
last_f = T / "goal-state" / "pkg" / "f-01.last.md"
check("未产出假成功交付", not last_f.exists() or "完成" not in last_f.read_text(encoding="utf-8"))

print("\nG. 空输出：不得判成功")
setup(["g-01"])
rc, out = run_engine("g-01", "empty")
check("空输出返回非 0（判失败）", rc != 0, f"rc={rc}")
check("未标记 .need-login（不是登录问题）", not (T / "goal-state" / ".need-login").exists())

print("\nH. daemon 侧：need-login 时不派活、不计失败")
setup(["h-01"])
(T / "goal-state" / ".need-login").write_text("", encoding="utf-8")
rc, out = tick()
ev = events()
check("记录 need_login 事件", "need_login" in ev, ev)
check("未派活", "spawned" not in ev, ev)
p = queue()["packages"][0]
check("包仍为 ready", p["status"] == "ready", p)
check("失败次数未增加", p.get("fail_count", 0) == 0, p)
check("未进入 in_progress", p["status"] != "in_progress", p)

print(f"\n结果：{ok} passed, {fail} failed")
shutil.rmtree(T, ignore_errors=True)
sys.exit(1 if fail else 0)
