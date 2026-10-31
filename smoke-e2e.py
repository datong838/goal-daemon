#!/usr/bin/env python3
"""端到端冒烟测试：mock OpenAI 兼容端点 + 真实 daemon/engine 链路（零 LLM 成本）。"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
T = ROOT / ".smoke"
CALLS = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        CALLS["n"] += 1
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        has_tool = any(m.get("role") == "tool" for m in body["messages"])
        if not has_tool:
            msg = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "hello.md", "content": "# hello\n循环通了\n"}
                            ),
                        },
                    }
                ],
            }
        else:
            msg = {
                "role": "assistant",
                "content": "改动：hello.md\n验证：文件已写\n未证点：无\n下一步：待审",
            }
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main():
    shutil.rmtree(T, ignore_errors=True)
    for d in ("goal-state/logs", "goal-state/pkg", "work"):
        (T / d).mkdir(parents=True, exist_ok=True)
    # 自己写配置，不复制生产 goal.md（否则生产切到 codebuddy 后本测试会跟着走错分支）
    (T / "goal-state/goal.md").write_text(
        "OBJECTIVE: smoke e2e\n"
        "ENGINE: http\n"
        "ENGINE_BASE_URL: http://127.0.0.1:18999/v1\n"
        "ENGINE_MODEL: mock\n"
        "ENGINE_API_KEY:\n"
        "ENGINE_MAX_STEPS: 6\n"
        "MAX_FAIL: 3\n"
        "MAX_PKG_SECONDS: 120\n"
        "NOTIFY: none\n",
        encoding="utf-8",
    )
    (T / "goal-state/queue.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "id": "pkg-e2e",
                        "title": "e2e",
                        "instruction": "写 hello.md",
                        "workdir": str(T / "work"),
                        "write_scope": [str(T / "work")],
                        "checks": ["hello.md 存在"],
                        "deps": [],
                        "status": "ready",
                        "fail_count": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    srv = HTTPServer(("127.0.0.1", 18999), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    env = os.environ.copy()
    env["GOAL_STATE_DIR"] = str(T / "goal-state")
    env["ENGINE_BASE_URL"] = "http://127.0.0.1:18999/v1"

    def tick(label):
        r = subprocess.run(
            ["/usr/bin/python3", str(ROOT / "goal-daemon.py"), "--once"],
            env=env,
            capture_output=True,
            text=True,
        )
        print(f"[{label}] rc={r.returncode} out={r.stdout.strip()!r} err={r.stderr.strip()[:400]!r}")

    tick("tick1 spawn")
    print("lock exists:", (T / "goal-state/lock.json").exists())
    tick("tick2 busy")

    deadline = time.time() + 25
    while time.time() < deadline:
        if (T / "goal-state/pkg/pkg-e2e.exit").exists():
            break
        time.sleep(0.5)

    tick("tick3 collect")
    q = json.loads((T / "goal-state/queue.json").read_text())
    pkg = q["packages"][0]
    print("status:", pkg["status"], "exit:", pkg.get("last_exit"))
    hello = T / "work/hello.md"
    print("hello.md:", hello.read_text().strip().replace("\n", " / ") if hello.exists() else "缺失")
    last = T / "goal-state/pkg/pkg-e2e.last.md"
    print("last.md:", last.read_text().strip()[:80] if last.exists() else "缺失")
    print("mock calls:", CALLS["n"])
    logf = T / "goal-state/logs/engine-pkg-e2e.log"
    if logf.exists():
        print("engine log:", logf.read_text().strip()[:600])

    (T / "goal-state/STOP").touch()
    tick("tick4 STOP")
    (T / "goal-state/STOP").unlink()
    tick("tick5 idle")

    srv.shutdown()


if __name__ == "__main__":
    main()
