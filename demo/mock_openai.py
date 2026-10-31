#!/usr/bin/env python3
"""演示用 mock OpenAI 兼容端点：每个请求延迟 6 秒，先给 tool_call，再给最终汇报。"""
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

LATENCY = 6.0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        time.sleep(LATENCY)
        has_tool = any(m.get("role") == "tool" for m in body["messages"])
        if not has_tool:
            msg = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "run_shell",
                            "arguments": json.dumps({"cmd": "date '+%H:%M:%S' >> ticks.log"}),
                        },
                    }
                ],
            }
        else:
            msg = {
                "role": "assistant",
                "content": "改动：ticks.log 追加一行\n验证：已执行\n未证点：无\n下一步：待审",
            }
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 18998), Handler).serve_forever()
