#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ENGINE: auto 与"后端不可用=等待"的测试（零成本，不调 LLM、不联网）。

设计意图：系统开机就能常驻，不强迫用户先登录某个网关。
  - 后端不可用时引擎返回 92/93，daemon 必须判"等待"：包退回 ready、fail_count 不变、不熔断。
  - ENGINE: auto 必须自己挑可用后端，一个都没有时返回 93 而不是假装干活。

用例：
  A. auto：codebuddy 不可用（有新鲜 .need-login）+ HTTP 端点不通 → 93
  B. auto：codebuddy 不可用 + HTTP 端点可达 → 选 http（不选 codebuddy）
  C. auto：codebuddy 可用（无标记）→ 选 codebuddy
  D. daemon：收到 exit=93 → 包回 ready、fail_count 不变、记 waiting 事件
  E. daemon：连续 5 次 93 也不熔断（区别于真失败）
"""

import json
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DAEMON = ROOT / "goal-daemon.py"
ENGINE = ROOT / "engine.sh"
PY = "/usr/bin/python3"
T = ROOT / ".auto-test"
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {detail}")


def fresh_state(engine_line, base_url="", need_login=False):
    shutil.rmtree(T, ignore_errors=True)
    for d in ("goal-state/logs", "goal-state/pkg", "work"):
        (T / d).mkdir(parents=True, exist_ok=True)
    cfg = (
        "OBJECTIVE: auto-test\n"
        f"ENGINE: {engine_line}\n"
        f"ENGINE_BASE_URL: {base_url}\n"
        "ENGINE_MODEL: none\n"
        "MAX_FAIL: 3\n"
        "MAX_PKG_SECONDS: 3600\nNOTIFY: none\n"
    )
    (T / "goal-state" / "goal.md").write_text(cfg, encoding="utf-8")
    if need_login:
        (T / "goal-state" / ".need-login").write_text("", encoding="utf-8")
    pkgs = [{
        "id": "auto-01", "title": "auto", "instruction": "写一行",
        "workdir": str(T / "work"), "write_scope": [str(T / "work")],
        "checks": [], "deps": [], "status": "ready", "fail_count": 0,
    }]
    (T / "goal-state" / "queue.json").write_text(
        json.dumps({"packages": pkgs}, ensure_ascii=False), encoding="utf-8")


def run_engine():
    env = {
        "GOAL_STATE_DIR": str(T / "goal-state"),
        "PKG_WORKDIR": str(T / "work"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
    }
    p = subprocess.run(["/bin/bash", str(ENGINE), "auto-01"], env=env,
                       capture_output=True, text=True, timeout=300)
    log = (T / "goal-state" / "logs" / "engine-auto-01.log")
    return p.returncode, log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


def tick_once():
    env = {"GOAL_STATE_DIR": str(T / "goal-state"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    return subprocess.run([PY, str(DAEMON), "--once"], env=env,
                          capture_output=True, text=True, timeout=120)


def events():
    rows = []
    for f in sorted((T / "goal-state" / "logs").glob("2*.log")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def queue():
    return json.loads((T / "goal-state" / "queue.json").read_text(encoding="utf-8"))


class Listener:
    """本地 TCP 监听，模拟"HTTP 端点已启动"（只验证连通性判定，不发请求）。"""

    def __enter__(self):
        self.s = socket.socket()
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(8)
        self.port = self.s.getsockname()[1]
        self.stop = False

        def loop():
            self.s.settimeout(0.3)
            while not self.stop:
                try:
                    c, _ = self.s.accept()
                    c.close()
                except Exception:
                    pass

        self.t = threading.Thread(target=loop, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop = True
        try:
            self.s.close()
        except Exception:
            pass


print("A. auto：codebuddy 不可用 + HTTP 不通 → 93")
fresh_state("auto", "http://127.0.0.1:59998/v1", need_login=True)
rc, log = run_engine()
check("返回 93（等待，不假装干活）", rc == 93, f"rc={rc}")
check("日志写明无可用后端", "无可用后端" in log, log[-200:])
check("未产出假交付", not (T / "goal-state" / "pkg" / "auto-01.last.md").exists())

print("B. auto：codebuddy 不可用 + HTTP 可达 → 选 http")
with Listener() as L:
    fresh_state("auto", f"http://127.0.0.1:{L.port}/v1", need_login=True)
    rc, log = run_engine()
    check("选择 http 后端", "auto -> http" in log, log[-200:])
    check("没有走 codebuddy", "auto -> codebuddy" not in log)

print("C. auto：codebuddy 可用 → 选 codebuddy")
fresh_state("auto", "http://127.0.0.1:59998/v1", need_login=False)
rc, log = run_engine()
check("选择 codebuddy 后端", "auto -> codebuddy" in log, log[-200:])

print("D. daemon：exit=93 → 包回 ready、不计失败")
fresh_state("auto", "http://127.0.0.1:59998/v1")
st = T / "goal-state"
q = queue()
q["packages"][0]["status"] = "in_progress"
(st / "queue.json").write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
(st / "lock.json").write_text(json.dumps({"pid": 1, "pkg_id": "auto-01", "started_epoch": 0}),
                              encoding="utf-8")
(st / "pkg" / "auto-01.exit").write_text("93", encoding="utf-8")
tick_once()
pkg = queue()["packages"][0]
check("包退回 ready", pkg["status"] == "ready", pkg["status"])
check("fail_count 未增加", int(pkg.get("fail_count", 0)) == 0, pkg.get("fail_count"))
check("记录 waiting 事件", any(e.get("event") == "waiting" and e.get("exit") == 93 for e in events()))

print("E. daemon：连续 5 次 93 也不熔断")
for _ in range(5):
    q = queue()
    q["packages"][0]["status"] = "in_progress"
    (st / "queue.json").write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
    (st / "lock.json").write_text(json.dumps({"pid": 1, "pkg_id": "auto-01", "started_epoch": 0}),
                                  encoding="utf-8")
    (st / "pkg" / "auto-01.exit").write_text("93", encoding="utf-8")
    tick_once()
pkg = queue()["packages"][0]
check("仍是 ready 不是 blocked", pkg["status"] == "ready", pkg["status"])
check("fail_count 仍为 0", int(pkg.get("fail_count", 0)) == 0, pkg.get("fail_count"))
check("waiting 事件累计 6 次", sum(1 for e in events() if e.get("event") == "waiting") == 6,
      sum(1 for e in events() if e.get("event") == "waiting"))

print(f"结果：{ok} passed, {fail} failed")
sys.exit(0 if fail == 0 else 1)
