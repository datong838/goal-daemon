#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两个已修 bug 的回归测试（零成本，不联网、不调 LLM）。

用例：
  A. 僵尸进程误判 —— 引擎写完退出码后立即 exit，进程很快消失但可能短暂成僵尸；
     daemon 必须凭 exit 文件收包，不能永久 busy。
  B. 残留退出码 —— 同包重试时，若上一次的 <id>.exit 还在，必须在 spawn 时清掉，
     否则引擎刚起就被"立即收包"误判完成。
  C. 全角括号变量名 —— engine-custom.sh 里中文标点紧邻变量必须用 ${ID}，
     否则 set -u 下 exit=1 连续 3 次熔断。
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DAEMON = ROOT / "goal-daemon.py"
PY = "/usr/bin/python3"
T = ROOT / ".regress"
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
        "OBJECTIVE: regress\nENGINE: custom\nMAX_FAIL: 3\n"
        "MAX_PKG_SECONDS: 3600\nBUDGET_DAILY_TOKENS: none\nNOTIFY: none\n",
        encoding="utf-8",
    )
    pkgs = [
        {
            "id": i,
            "title": i,
            "instruction": "写 ticks.log",
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


def tick(engine, sleep_s=1):
    env = dict(os.environ, GOAL_STATE_DIR=str(T / "goal-state"), DEMO_ENGINE_SLEEP=str(sleep_s))
    r = subprocess.run(
        [PY, str(DAEMON), "--once", "--engine", str(engine)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    return r.returncode, r.stdout + r.stderr


def queue():
    return json.loads((T / "goal-state" / "queue.json").read_text(encoding="utf-8"))


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


print("A. 僵尸/退出码优先判定")
setup(["a-01"])
rc, _ = tick(ROOT / "engine.sh", sleep_s=1)
check("spawn 成功", "spawned" in events(), events())
# 轮询等待退出码落盘（不能用固定 sleep：机器忙时 1s 引擎可能还没退，会误判为 flaky）
deadline = time.time() + 30
while time.time() < deadline and not (T / "goal-state" / "pkg" / "a-01.exit").exists():
    time.sleep(0.5)
check("引擎已写出退出码", (T / "goal-state" / "pkg" / "a-01.exit").exists())
rc, out = tick(ROOT / "engine.sh", sleep_s=1)
ev = events()
check("引擎已退出 -> 立即收包（不误判 busy）", "collected" in ev, ev)
p = queue()["packages"][0]
check("包状态 done（免人工放行，交付即接受）", p["status"] == "done", p)
check("记账写明是自动放行", p.get("reviewed_by") == "auto", p.get("reviewed_by"))
check("退出码文件已清理", not (T / "goal-state" / "pkg" / "a-01.exit").exists())

print("\nB. 残留退出码必须在 spawn 时清掉")
setup(["b-01"])
(T / "goal-state" / "pkg" / "b-01.exit").write_text("0", encoding="utf-8")  # 伪造上次残留
rc, _ = tick(ROOT / "engine.sh", sleep_s=4)  # 引擎跑 4 秒
ev = events()
check("spawn 成功", "spawned" in ev, ev)
check("未在引擎运行中被误收包", "collected" not in ev, ev)
check("残留 exit 文件已删除", not (T / "goal-state" / "pkg" / "b-01.exit").exists())
check("包仍在 in_progress", queue()["packages"][0]["status"] in ("in_progress", "delivered"),
      queue()["packages"][0])

print("\nC. 引擎脚本在 set -u 下不因中文标点报错")
setup(["c-01"])
env = dict(os.environ, GOAL_STATE_DIR=str(T / "goal-state"), DEMO_ENGINE_SLEEP="0")
r = subprocess.run(["/bin/bash", str(ROOT / "engine-custom.sh"), "c-01"],
                   env=env, capture_output=True, text=True, timeout=30)
check("engine-custom.sh 退出码 0", r.returncode == 0, r.stderr.strip()[:300])
last = T / "goal-state" / "pkg" / "c-01.last.md"
check("产出报告已生成", last.exists())
if last.exists():
    body = last.read_text(encoding="utf-8")
    check("全角括号旁变量已正确展开", "（c-01）" in body, body)

print("\nD. 后端不可用：退避，不每拍重派")
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("gd", DAEMON)
gd = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(gd)

_now = time.time()
_q = {"packages": [{"id": "x", "status": "ready", "wait_until": _now + 60}]}
check("退避窗口内 pick_ready 不取", gd.pick_ready(_q) is None)
_q["packages"][0]["wait_until"] = _now - 1
check("过点后照取", (gd.pick_ready(_q) or {}).get("id") == "x")

setup(["c-02"])
st = T / "goal-state"
seq = []
for _ in range(6):
    (st / "pkg" / "c-02.exit").write_text("92", encoding="utf-8")
    qq = queue()
    qq["packages"][0]["status"] = "in_progress"
    gd.collect(st, qq, "c-02", 3, auto_accept=True)
    seq.append(qq["packages"][0].get("wait_backoff_s"))
check("退避序列 30/60/120/240/300/300", seq == [30, 60, 120, 240, 300, 300], seq)
check("后端不可用不累计失败", queue()["packages"][0]["fail_count"] == 0)
check("包保持 ready", queue()["packages"][0]["status"] == "ready")
check("日志带 retry_in_s", "waiting" in events())

(st / "pkg" / "c-02.exit").write_text("0", encoding="utf-8")
qq = queue()
gd.collect(st, qq, "c-02", 3, auto_accept=True)
check("成功后退避状态清空", "wait_until" not in queue()["packages"][0])

print("\nE. engine-http：本机端点不走代理")
# 现象：在 WorkBuddy 集成终端等注入了 HTTP_PROXY 的环境里跑 ENGINE: http 或
# demo-run.sh，urllib 会把 127.0.0.1 的请求也送去代理，返回
# 502 upstream connect failed: Connection refused —— 看着像端点没起，极难查。
_http_src = (ROOT / "engine-http.py").read_text(encoding="utf-8")
check("有回环判定", "_is_loopback" in _http_src)
check("对本机禁用代理", "ProxyHandler({})" in _http_src)
check("chat 走自建 opener", "_opener(url).open(" in _http_src)
check("没有裸 urlopen", "urllib.request.urlopen(req" not in _http_src)

_espec = _ilu.spec_from_file_location("eh", ROOT / "engine-http.py")
eh = _ilu.module_from_spec(_espec)
_espec.loader.exec_module(eh)
check("127.0.0.1 判为本机", eh._is_loopback("http://127.0.0.1:18998/v1/chat/completions"))
check("localhost 判为本机", eh._is_loopback("http://localhost:3000/v1"))
check("127.0.0.5 判为本机", eh._is_loopback("http://127.0.0.5:8080/"))
check("外网不误判", not eh._is_loopback("https://api.openai.com/v1"))
check("本机 opener 无代理",
      not eh._opener("http://127.0.0.1:18998/v1").handlers
      or all(not isinstance(h, __import__("urllib.request", fromlist=["x"]).ProxyHandler)
             for h in eh._opener("http://127.0.0.1:18998/v1").handlers))

print("\nF. demo 脚本不得误伤生产 daemon")
# launchd 装的就是 `goal-daemon.py --loop --interval 10`。demo-stop.sh 早期用
# pkill -f "goal-daemon.py --loop" 兜底，在已安装的机器上跑一次演示清理，
# 生产守护进程就被一起杀掉。演示进程必须靠 --state-dir 带出自己的路径来区分。
_dstop = (ROOT / "demo-stop.sh").read_text(encoding="utf-8")
_drun = (ROOT / "demo-run.sh").read_text(encoding="utf-8")
_dstop_code = "\n".join(
    ln for ln in _dstop.splitlines() if not ln.lstrip().startswith("#")
)
check("demo-stop 不带裸 pkill", 'pkill -f "goal-daemon.py --loop"' not in _dstop_code)
check("demo-stop 用 --state-dir 区分", "--state-dir" in _dstop)
check("demo-run 启动带 --state-dir", "--state-dir" in _drun)
check("demo-stop 复位 in_progress", "in_progress" in _dstop and "pending" in _dstop)
check("demo-run 等 mock 就绪而非固定 sleep",
      "connect_ex" in _drun and "sleep 1\n" not in _drun)

print(f"\n结果：{ok} passed, {fail} failed")
shutil.rmtree(T, ignore_errors=True)
sys.exit(1 if fail else 0)
