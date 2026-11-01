#!/usr/bin/env python3
"""engine-http.py — 自研引擎（M1）：OpenAI 兼容接口上的最小 agent 循环。

不依赖任何厂商 CLI（不用 codex / claude）；只需要一个 OpenAI 兼容的
/v1/chat/completions 端点（本地 new-api / sub2api / ollama / 任意云服务均可）。

契约（与 engine.sh 相同）：
  engine-http.py <package_id>
  输入：$GOAL_STATE_DIR/queue.json 的 instruction，或 <id>.task.md
  环境：GOAL_STATE_DIR / PKG_WORKDIR / PKG_TOKEN_BUDGET / ENGINE_TIMEOUT_S
  输出：pkg/<id>.exit（退出码）、pkg/<id>.last.md（最终回复）
  约束：工具执行限定在 PKG_WORKDIR 与 write_scope 内；不接收全局 goal；不 commit Git

配置（goal.md 的 KEY: value 行，或同名环境变量）：
  ENGINE_BASE_URL  默认 http://127.0.0.1:3000/v1
  ENGINE_MODEL     默认 gpt-4o-mini
  ENGINE_API_KEY   默认空（本地网关可留空）
  ENGINE_MAX_STEPS 默认 24
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE_DIR = Path(os.environ.get("GOAL_STATE_DIR", ".")).resolve()
PKG_ID = sys.argv[1] if len(sys.argv) > 1 else ""
WORKDIR = Path(os.environ.get("PKG_WORKDIR", ".")).resolve()
MAX_STEPS_DEFAULT = 24
SHELL_TIMEOUT = 600
OUTPUT_LIMIT = 6000


def parse_kv(path: Path) -> dict:
    out = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        out[k.strip().upper()] = v.strip()
    return out


CFG = parse_kv(STATE_DIR / "goal.md")


def cfg(key: str, default: str) -> str:
    return os.environ.get(key) or CFG.get(key) or default


BASE_URL = cfg("ENGINE_BASE_URL", "http://127.0.0.1:3000/v1").rstrip("/")
MODEL = cfg("ENGINE_MODEL", "gpt-4o-mini")
API_KEY = cfg("ENGINE_API_KEY", "")
MAX_STEPS = int(cfg("ENGINE_MAX_STEPS", str(MAX_STEPS_DEFAULT)))


# ---------------------------------------------------------------- 工具实现


def _safe_path(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = WORKDIR / p
    p = p.resolve()
    roots = [WORKDIR] + [Path(x).resolve() for x in WRITE_SCOPE]
    if not any(str(p) == str(r) or str(p).startswith(str(r) + os.sep) for r in roots):
        raise PermissionError(f"路径越界（不在工作目录或 write_scope 内）：{p}")
    return p


def tool_run_shell(cmd: str) -> str:
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            cwd=str(WORKDIR),
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT,
        )
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return f"exit={r.returncode}\n{out[:OUTPUT_LIMIT]}"
    except subprocess.TimeoutExpired:
        return f"超时（>{SHELL_TIMEOUT}s）"


def tool_read_file(path: str) -> str:
    p = _safe_path(path)
    if not p.is_file():
        return f"文件不存在：{p}"
    text = p.read_text(encoding="utf-8", errors="replace")
    return text[:OUTPUT_LIMIT]


def tool_write_file(path: str, content: str) -> str:
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    WRITTEN.append(str(p))
    return f"已写入 {p}（{len(content)} 字符）"


def tool_list_dir(path: str = ".") -> str:
    p = _safe_path(path)
    if not p.is_dir():
        return f"目录不存在：{p}"
    rows = []
    for child in sorted(p.iterdir())[:200]:
        rows.append(("d " if child.is_dir() else "f ") + child.name)
    return "\n".join(rows) or "(空)"


WRITE_SCOPE = []
WRITTEN = []


def load_pkg() -> dict:
    q = json.loads((STATE_DIR / "queue.json").read_text(encoding="utf-8"))
    pkg = next((p for p in q.get("packages", []) if p.get("id") == PKG_ID), None)
    if pkg is None:
        raise SystemExit(f"queue.json 中找不到包 {PKG_ID}")
    return pkg


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "在工作目录执行 shell 命令（用于读代码、跑测试、构建）。禁止 git commit/push。",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件内容（相对工作目录或绝对路径，须在允许范围内）。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入/覆盖文本文件（须在 write_scope 内）。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
]


def dispatch(name: str, args: dict) -> str:
    try:
        if name == "run_shell":
            return tool_run_shell(str(args.get("cmd", "")))
        if name == "read_file":
            return tool_read_file(str(args.get("path", "")))
        if name == "write_file":
            return tool_write_file(str(args.get("path", "")), str(args.get("content", "")))
        if name == "list_dir":
            return tool_list_dir(str(args.get("path", ".")))
        return f"未知工具：{name}"
    except PermissionError as e:
        return f"拒绝：{e}"
    except Exception as e:  # noqa: BLE001
        return f"工具异常：{type(e).__name__}: {e}"


# ---------------------------------------------------------------- HTTP

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_loopback(url: str) -> bool:
    """判断目标是否在本机。urllib 默认读 HTTP_PROXY —— 而 WorkBuddy 集成终端等
    环境会注入代理变量，请求 127.0.0.1 也被送去代理，结果是 502
    "upstream connect failed: Connection refused"，看起来像端点没起，极难查。
    curl 那边一直带 --noproxy '*'，这里必须对齐。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK or host.startswith("127.") or host.endswith(".local")


_OPENERS: dict = {}


def _opener(url: str):
    direct = _is_loopback(url)
    if direct not in _OPENERS:
        if direct:
            _OPENERS[direct] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            _OPENERS[direct] = urllib.request.build_opener()
    return _OPENERS[direct]


def chat(messages: list) -> dict:
    payload = {"model": MODEL, "messages": messages, "tools": TOOLS, "tool_choice": "auto"}
    data = json.dumps(payload).encode("utf-8")
    url = BASE_URL + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )
    with _opener(url).open(req, timeout=SHELL_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- 主流程


def main() -> int:
    global WRITE_SCOPE
    pkg = load_pkg()
    WRITE_SCOPE = pkg.get("write_scope") or [str(WORKDIR)]

    task_file = STATE_DIR / "pkg" / f"{PKG_ID}.task.md"
    if task_file.is_file():
        task = task_file.read_text(encoding="utf-8")
    else:
        task = (
            f"# 任务包 {PKG_ID}\n\n## 目标\n{pkg.get('instruction','')}\n\n"
            f"## 允许写入\n" + "\n".join("- " + w for w in WRITE_SCOPE) + "\n\n"
            "## 交付\n结尾段落：改动文件清单 / 已跑验证与结果 / 未证点 / 建议下一步。不得 commit。\n"
        )

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个自动化开发执行器，只完成给定任务包，不扩大范围。\n"
                f"工作目录：{WORKDIR}\n允许写入：{', '.join(WRITE_SCOPE)}\n"
                "用工具实际改代码并跑验证；不得 git commit/push；不得写允许范围外的路径。\n"
                "任务完成后用纯文本给出最终汇报（改动文件 / 验证结果 / 未证点 / 下一步）。"
            ),
        },
        {"role": "user", "content": task},
    ]

    steps = 0
    content = ""
    while steps < MAX_STEPS:
        steps += 1
        try:
            resp = chat(messages)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:800]
            return fail(f"HTTP {e.code}: {body}")
        except Exception as e:  # noqa: BLE001
            return fail(f"请求失败：{type(e).__name__}: {e}")

        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        if calls:
            messages.append(
                {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
            )
            for call in calls:
                fn = (call.get("function") or {}).get("name", "")
                try:
                    args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = dispatch(fn, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": result,
                    }
                )
            continue

        content = msg.get("content") or ""
        break

    if not content:
        return fail(f"未在 {MAX_STEPS} 步内完成（模型未给出最终回复）")

    (STATE_DIR / "pkg" / f"{PKG_ID}.last.md").write_text(content, encoding="utf-8")
    summary = {
        "pkg": PKG_ID,
        "model": MODEL,
        "steps": steps,
        "files_written": WRITTEN,
        "epoch": int(time.time()),
    }
    (STATE_DIR / "pkg" / f"{PKG_ID}.result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(content)
    return 0


def fail(reason: str) -> int:
    msg = f"[engine-http] 失败：{reason}"
    print(msg, file=sys.stderr)
    (STATE_DIR / "pkg" / f"{PKG_ID}.last.md").write_text(msg, encoding="utf-8")
    return 1


if __name__ == "__main__":
    sys.exit(main())
