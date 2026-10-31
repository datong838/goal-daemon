#!/usr/bin/env python3
"""goal-daemon: 10 秒 tick 的 Goal 常驻循环守护进程（单一快循环）。

对应方案：goal-daemon/Goal常驻循环方案.md v3。

tick 决策顺序：
  0) flock 防 tick 重叠（上一 tick 未结束则直接跳过）
  1) STOP 存在 -> 记录并退出（不领新包；在跑的会话不打断）
  2) lock 存在且 pid 存活 -> busy，纯读退出（零干扰）
  3) lock 存在但 pid 已退出 -> 有 exit 文件=正常收包；无 exit 文件=失联接管
  4) 空闲 -> pending 升 ready -> 领一个 ready 包 -> spawn 引擎 -> 写锁 -> 立即退出

用法：
  goal-daemon.py --once            执行一次 tick
  goal-daemon.py --once --dry-run  只打印决策，不 spawn / 不写状态
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
HEARTBEAT_STALE_DEFAULT = 90
MAX_FAIL_DEFAULT = 3
MAX_PKG_SECONDS_DEFAULT = 7200

# 引擎"当前不可用"的退出码：不算失败，包退回 ready，等后端就绪后自动继续。
# 92=未登录（codebuddy），93=无可用后端（auto 模式下 codebuddy 未登录且 HTTP 端点不通）
WAIT_EXITS = {92: "need_login", 93: "no_backend"}
# 后端不可用时的重试退避：30s → 60 → 120 → 240 → 封顶 300s。
# 为什么不每拍（10s）重试：网关会随桌面端重启短暂消失，实测掉线 14 分钟
# （22:50→23:04）里本循环重派了 35 次、每个包起一个进程 11 秒后退出，
# 日志刷了 70 行全是同一个 need_login。无人值守的意思是"没人在也不停"，
# 不是"每秒敲一次门"。
WAIT_BACKOFF_START_S = 30
WAIT_BACKOFF_MAX_S = 300


# ---------------------------------------------------------------- 基础工具


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_dir(path: Path) -> None:
    """幂等建目录（对某些沙箱环境不认 exist_ok 的情况做兜底）。"""
    if path.is_dir():
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass


def log(state_dir: Path, event: str, **fields) -> None:
    logs = state_dir / "logs"
    ensure_dir(logs)
    path = logs / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    rec = {"ts": now_iso(), "event": event, **fields}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def parse_kv(path: Path) -> dict:
    """解析 goal.md 的 `KEY: value` 行（忽略注释与其他文本）。"""
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


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def heartbeat_fresh(state_dir: Path, stale_seconds: int) -> bool:
    hb = read_json(state_dir / "heartbeat.json", None) or {}
    ts = hb.get("epoch")
    return isinstance(ts, (int, float)) and (time.time() - ts) < stale_seconds


def notify(state_dir: Path, kind: str, message: str) -> None:
    """v1：不推送任何渠道（用户决定：先不通知），只落盘备查。"""
    log(state_dir, "notify", kind=kind, message=message)


def clear_runtime(state_dir: Path, pkg_id: str) -> None:
    for f in (
        state_dir / "lock.json",
        state_dir / "heartbeat.json",
        state_dir / "pkg" / f"{pkg_id}.exit",
    ):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- 队列


def find_pkg(queue: dict, pkg_id: str):
    return next((p for p in queue.get("packages", []) if p.get("id") == pkg_id), None)


def deps_done(pkg: dict, queue: dict, accept=("done",)) -> bool:
    """依赖包的"已完成"口径。

    默认只认 done（人工/根审后置位，严格口径）；无人值守链路可在 goal.md 用
    DEPS_REQUIRE: delivered,done 放宽到"引擎交付即算过"。两者是不同验收强度，
    不是同一件事，故做成显式配置而非偷偷放宽。
    """
    for dep in pkg.get("deps") or []:
        dep_pkg = find_pkg(queue, dep)
        if not dep_pkg or dep_pkg.get("status") not in accept:
            return False
    return True


def hits_protected(pkg: dict, cfg: dict) -> str:
    """包的工作目录或写界是否落进受保护根。命中则返回那个根，否则空串。

    PROTECTED_ROOTS 是"这一仓不许再动"的硬闸，不是提示语。命中即拒派——
    因为口头约定守不住：队列里只要留着一个 ready 的 mes 包，守护进程每拍就会
    把它捡起来，人不在场时没人拦得住。闸放在引擎里，恢复只需删配置一行。
    """
    raw = (cfg.get("PROTECTED_ROOTS") or "").strip()
    if not raw or raw.lower() in ("none", "off", "-"):
        return ""
    roots = [r.strip().rstrip("/") for r in raw.split(",") if r.strip()]
    if not roots:
        return ""
    cands = [pkg.get("workdir") or ""]
    cands += [str(w) for w in (pkg.get("write_scope") or [])]
    for c in cands:
        c = str(c).rstrip("/")
        if not c:
            continue
        for r in roots:
            if r and (c == r or c.startswith(r + "/")):
                return r
    return ""


def promote_pending(queue: dict, accept=("done",)) -> list:
    changed = []
    for pkg in queue.get("packages", []):
        if pkg.get("status") == "pending" and deps_done(pkg, queue, accept):
            pkg["status"] = "ready"
            changed.append(pkg["id"])
    return changed


def pick_ready(queue: dict, accept=("done",), cfg: dict = None, state_dir: Path = None):
    """取第一个可执行的包。deps 口径必须与 promote_pending 一致，
    否则会出现"已标 ready 但取不出来"的假空闲（曾真实发生过）。

    退避中的包（后端刚不可用）先跳过，等到点再取——否则网关一掉线，
    每拍都会重派一次，什么都不干，只刷日志。

    受保护根里的包一律不取，并就地改成 held：它既不该跑，也不该每拍重判。
    配置删掉后再调 release_held() 就能放回来，不需要改队列数据。
    """
    now = time.time()
    for pkg in queue.get("packages", []):
        if pkg.get("status") != "ready" or not deps_done(pkg, queue, accept):
            continue
        root = hits_protected(pkg, cfg or {})
        if root:
            pkg["status"] = "held"
            pkg["held_at"] = now_iso()
            pkg["held_root"] = root
            if state_dir is not None:
                log(state_dir, "guard_hold", pkg_id=pkg.get("id"), root=root,
                    note="该包工作目录在受保护根内，拒派")
            continue
        if float(pkg.get("wait_until") or 0) > now:
            continue
        return pkg
    return None


def release_held(queue: dict, cfg: dict) -> list:
    """PROTECTED_ROOTS 放宽/清空后，把不再命中的 held 包放回 ready。"""
    back = []
    for pkg in queue.get("packages", []):
        if pkg.get("status") != "held":
            continue
        if hits_protected(pkg, cfg or {}):
            continue
        pkg["status"] = "ready"
        pkg.pop("held_at", None)
        pkg.pop("held_root", None)
        back.append(pkg.get("id"))
    return back


def bump_fail(pkg: dict, max_fail: int) -> None:
    pkg["fail_count"] = int(pkg.get("fail_count", 0)) + 1
    pkg["status"] = "blocked" if pkg["fail_count"] >= max_fail else "ready"


def _clear_wait(pkg: dict) -> None:
    """后端通了（或这次是真失败而不是不可用），退避状态作废，下次立刻可派。"""
    for k in ("wait_until", "wait_backoff_s", "wait_count"):
        pkg.pop(k, None)


# ---------------------------------------------------------------- 收包


def collect(state_dir: Path, queue: dict, pkg_id: str, max_fail: int, auto_accept: bool = False) -> None:
    exit_file = state_dir / "pkg" / f"{pkg_id}.exit"
    try:
        code = int(exit_file.read_text(encoding="utf-8").strip() or "-1")
    except (FileNotFoundError, ValueError):
        code = -1

    lock = read_json(state_dir / "lock.json", {}) or {}
    duration = int(time.time() - float(lock.get("started_epoch", time.time())))
    last_md = state_dir / "pkg" / f"{pkg_id}.last.md"

    pkg = find_pkg(queue, pkg_id)
    if pkg is None:
        log(state_dir, "collect_orphan", pkg_id=pkg_id, exit=code)
    else:
        if code == 0:
            # 无人值守模式（FOLLOWUP_REQUIRE_DONE: off）直接落 done：
            # 引擎交付即接受，不再留一个永远等不到人的 delivered。
            # done 的含义是"这一包走完了"，不是"人看过"，故记 reviewed_by=auto。
            pkg["status"] = "done" if auto_accept else "delivered"
            pkg["fail_count"] = 0
            pkg["delivered_at"] = now_iso()
            if auto_accept:
                pkg["reviewed_at"] = pkg["delivered_at"]
                pkg["reviewed_by"] = "auto"
            _clear_wait(pkg)
        elif code in WAIT_EXITS:
            # 后端当前不可用（未登录 / 端点没起）：不是失败，包退回 ready 等下一拍。
            # 不累计失败、不熔断——否则开机时端点没起就会被判死。
            # 但要退避：每拍重派只会刷日志（实测 14 分钟重派 35 次）。
            pkg["status"] = "ready"
            prev = int(pkg.get("wait_backoff_s") or 0)
            backoff = WAIT_BACKOFF_START_S if prev <= 0 else min(prev * 2, WAIT_BACKOFF_MAX_S)
            pkg["wait_backoff_s"] = backoff
            pkg["wait_until"] = time.time() + backoff
            pkg["wait_count"] = int(pkg.get("wait_count") or 0) + 1
            log(
                state_dir,
                "waiting",
                pkg_id=pkg_id,
                exit=code,
                reason=WAIT_EXITS[code],
                retry_in_s=backoff,
                attempt=pkg["wait_count"],
            )
        else:
            _clear_wait(pkg)
            bump_fail(pkg, max_fail)
            if pkg["status"] == "blocked":
                notify(state_dir, "fuse", f"{pkg_id} 连续失败 {pkg['fail_count']} 次，已熔断")
        pkg["last_exit"] = code
        pkg["last_duration_s"] = duration

    cp = read_json(state_dir / "checkpoint.json", {}) or {}
    cp.update(
        {
            "last_pkg": pkg_id,
            "last_exit": code,
            "last_duration_s": duration,
            "last_message_file": str(last_md) if last_md.exists() else None,
            "updated_at": now_iso(),
            "pending_review": (pkg["id"] if (pkg and not auto_accept) else None),
            "note": (
                "自动放行：交付即 done，无需人工审"
                if auto_accept
                else "引擎自验见 pkg/<id>.last.md；delivered 需人工审后置 done"
            ),
        }
    )
    write_json_atomic(state_dir / "checkpoint.json", cp)
    write_json_atomic(state_dir / "queue.json", queue)
    clear_runtime(state_dir, pkg_id)
    log(state_dir, "collected", pkg_id=pkg_id, exit=code, duration_s=duration)


# ---------------------------------------------------------------- 回流（Loop 闭环）


def _load_followup():
    """按路径加载同目录的 followup.py。

    daemon.py 是单文件脚本，不一定在 sys.path 里，所以显式按文件加载；
    加载失败只记日志、不抛——回流是增强能力，坏了不能拖垮主循环。
    """
    import importlib.util

    fp = SCRIPT_DIR / "followup.py"
    if not fp.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("goal_followup", fp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001
        return None


def _throttled_skip_log(state_dir: Path, reason: str, parent, every_s: int = 300) -> None:
    """同一 (reason, parent) 每 every_s 秒最多记一条。

    父包"等人工放行"可能持续几天，每 tick 记一条等于把日志变成噪声，
    真出事时反而找不到。首次必记，之后按窗口合并。
    """
    stamp = state_dir / ".followup-skip.logged"
    key = f"{reason}:{parent}"
    now = time.time()
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if now - float(data.get(key, 0) or 0) < every_s:
        return
    data[key] = now
    try:
        stamp.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    log(state_dir, "followup_skip", reason=reason, parent=parent)


def maybe_followup(state_dir: Path, queue: dict, cfg: dict, dry_run: bool) -> bool:
    """队列空了以后，试着从上一包汇报的"仍未修"里长出下一包。

    这是本 daemon 从"批处理"变成"Loop"的地方：
      批处理 —— queue 里预置什么跑什么，跑完 idle，剩下的问题永远留在汇报里；
      Loop   —— 每包交付后自己长出下一包，直到汇报里没有遗留项。
    返回 True 表示"已经生了新包，本 tick 不要记 idle"。
    """
    flag = (cfg.get("FOLLOWUP", "off") or "off").strip().lower()
    if flag not in ("on", "1", "true", "yes"):
        return False

    mod = _load_followup()
    if mod is None:
        log(state_dir, "followup_unavailable", note="followup.py 加载失败")
        return False

    try:
        r = mod.plan(state_dir, queue, cfg, write=not dry_run)
    except Exception as exc:  # noqa: BLE001
        log(state_dir, "followup_error", error=str(exc))
        return False

    if "skip" in r:
        # clean 是正常收敛，不必记；其余是真跳过原因，但要限流——
        # 父包没放行这种状态会持续几天，每 10 秒一条能把日志刷到上万行。
        if r["skip"] != "clean":
            _throttled_skip_log(state_dir, r["skip"], r.get("parent"))
        return False

    pkg = r["spawn"]
    root = hits_protected(pkg, cfg)
    if root:
        # 父包在被保护的仓里：连子包都不该生。否则队列里会积一堆碰不到 mes
        # 的 held 包，看着像"有活"，实际一笔也派不出去。
        _throttled_skip_log(state_dir, "protected", r.get("parent"))
        return False

    n = len(r["items"])
    if dry_run:
        print(f"[dry-run] 会从 {r['parent']} 回流出新包 {pkg['id']}（{n} 项遗留）")
        return True

    queue.setdefault("packages", []).append(pkg)
    write_json_atomic(state_dir / "queue.json", queue)
    log(state_dir, "followup_spawned", pkg_id=pkg["id"], parent=r["parent"], items=n)
    notify(state_dir, "followup", f"{r['parent']} 遗留 {n} 项，已自动生成 {pkg['id']}")
    return True


# ---------------------------------------------------------------- tick


def tick(state_dir: Path, dry_run: bool, engine: Path) -> None:
    cfg = parse_kv(state_dir / "goal.md")
    max_fail = int(cfg.get("MAX_FAIL", MAX_FAIL_DEFAULT))
    max_secs = int(cfg.get("MAX_PKG_SECONDS", MAX_PKG_SECONDS_DEFAULT))
    deps_accept = tuple(
        s.strip() for s in (cfg.get("DEPS_REQUIRE", "done") or "done").split(",") if s.strip()
    ) or ("done",)

    # 是否免人工放行。默认关掉审批（FOLLOWUP_REQUIRE_DONE: off）：
    # 引擎交付即 done，下一包直接排，不再等 ./review.sh。
    auto_accept = (cfg.get("FOLLOWUP_REQUIRE_DONE", "off") or "off").strip().lower() != "on"

    if (state_dir / "STOP").exists():
        log(state_dir, "stopped")
        return

    queue = read_json(state_dir / "queue.json", {"packages": []})
    lock = read_json(state_dir / "lock.json", None)

    if lock:
        pid = int(lock.get("pid", 0))
        pkg_id = lock.get("pkg_id", "unknown")
        exit_file = state_dir / "pkg" / f"{pkg_id}.exit"

        # 退出码已落盘 = 会话确定结束（优先于 pid 判断，避免僵尸进程误判 busy）
        if exit_file.exists():
            if dry_run:
                print(f"[dry-run] 会收包 {pkg_id}（会话已正常结束）")
            else:
                collect(state_dir, queue, pkg_id, max_fail, auto_accept=auto_accept)
            return

        if pid_alive(pid):
            log(
                state_dir,
                "busy",
                pkg_id=pkg_id,
                pid=pid,
                hb=heartbeat_fresh(state_dir, HEARTBEAT_STALE_DEFAULT),
            )
            return

        # 无 exit 文件 = 会话失联
        elapsed = int(time.time() - float(lock.get("started_epoch", time.time())))
        log(state_dir, "takeover", pkg_id=pkg_id, pid=pid, elapsed_s=elapsed)
        if dry_run:
            print(f"[dry-run] 会话失联，会接管 {pkg_id}")
            return
        pkg = find_pkg(queue, pkg_id)
        if pkg is not None:
            bump_fail(pkg, max_fail)
            write_json_atomic(state_dir / "queue.json", queue)
            if pkg["status"] == "blocked":
                notify(state_dir, "fuse", f"{pkg_id} 会话失联且失败达上限，已熔断")
        clear_runtime(state_dir, pkg_id)
        return

    # 锁丢失但队列里仍有 in_progress：自愈
    orphan = next((p for p in queue.get("packages", []) if p.get("status") == "in_progress"), None)
    if orphan:
        log(state_dir, "orphan_inprogress", pkg_id=orphan["id"])
        if dry_run:
            print(f"[dry-run] 发现无锁的 in_progress 包 {orphan['id']}，会退回重试")
        else:
            bump_fail(orphan, max_fail)
            write_json_atomic(state_dir / "queue.json", queue)
        return

    if dry_run:
        for p in queue.get("packages", []):
            if p.get("status") == "ready":
                root = hits_protected(p, cfg)
                if root:
                    print(f"[dry-run] {p['id']} 会因受保护根拒派（{root}）")

    back = release_held(queue, cfg)
    if back:
        if dry_run:
            print(f"[dry-run] 会放回 held->ready: {back}")
        else:
            write_json_atomic(state_dir / "queue.json", queue)
            log(state_dir, "released", packages=back)

    changed = promote_pending(queue, deps_accept)
    if changed:
        if dry_run:
            print(f"[dry-run] 会提升 pending->ready: {changed}")
        else:
            write_json_atomic(state_dir / "queue.json", queue)
            log(state_dir, "promoted", packages=changed)

    pkg = pick_ready(queue, deps_accept, cfg, state_dir if not dry_run else None)
    if not pkg:
        if not dry_run and any(p.get("status") == "held" for p in queue.get("packages", [])):
            write_json_atomic(state_dir / "queue.json", queue)
        # 有包但都在后端退避窗口里：这不是 idle，也别打 idle —— 打一条限流的
        # wait_backend，带还要等多久，方便事后看"那 14 分钟在干嘛"。
        cooling = next(
            (
                p
                for p in queue.get("packages", [])
                if p.get("status") == "ready" and float(p.get("wait_until") or 0) > time.time()
            ),
            None,
        )
        if cooling:
            _throttled_skip_log(state_dir, "wait_backend", cooling["id"])
            return
        # 队列空了先别急着 idle：看一眼上一包的汇报里还有没有"仍未修"。
        # 有就长出新包，下一拍再派发（同 tick 不双写状态）。
        if maybe_followup(state_dir, queue, cfg, dry_run):
            return
        log(state_dir, "idle")
        return

    pkg_id = pkg["id"]

    # 交付物已在就不再派活。
    # 超时重投会把同一个包再派一次，两个 run 抢写同一份稿（曾真实发生：
    # vd-003-f1 的汇报 21:16 已落盘，21:28 又被派一遍）。
    # 只认非空文件；要真重跑，先删 pkg/<id>.last.md。
    existing = state_dir / "pkg" / f"{pkg_id}.last.md"
    try:
        existing_size = existing.stat().st_size
    except OSError:
        existing_size = 0
    if existing_size > 0:
        if dry_run:
            print(f"[dry-run] 包 {pkg_id} 已有产出 {existing_size} B，会直接判 delivered")
            return
        pkg["status"] = "done" if auto_accept else "delivered"
        pkg["fail_count"] = 0
        pkg["last_exit"] = 0
        pkg["delivered_at"] = now_iso()
        if auto_accept:
            pkg["reviewed_at"] = pkg["delivered_at"]
            pkg["reviewed_by"] = "auto"
        write_json_atomic(state_dir / "queue.json", queue)
        log(state_dir, "delivered_existing_output", pkg_id=pkg_id, bytes=existing_size)
        return

    # 仅在"显式指定 codebuddy"时跳过派活：未登录时不计失败，等人工 /login 后自动恢复。
    # ENGINE: auto 不跳过——引擎会自己判断能否走 HTTP 后端，实在没有后端才回 93（等待）。
    engine_type = (cfg.get("ENGINE", "") or "").strip().lower()
    login_mark = state_dir / ".need-login"
    login_stamp = state_dir / ".need-login.logged"
    # 凭据一出现就自动解除退避：用户可能在 TUI 里 Ctrl+C 退出，没走到 login.sh 的清理逻辑
    if login_mark.exists() and (Path.home() / ".credentials.json").exists():
        login_mark.unlink(missing_ok=True)
        login_stamp.unlink(missing_ok=True)
        log(state_dir, "login_restored")
    if engine_type == "codebuddy" and login_mark.exists():
        if dry_run:
            print(f"[dry-run] 引擎未登录，跳过 {pkg_id}")
        else:
            # 未登录时每 10 秒记一条会把日志刷满：首次必记，之后 5 分钟最多一条。
            # 用独立的时间戳文件，避免动 .need-login 的 mtime（engine.sh 的退避读它）。
            try:
                last = login_stamp.stat().st_mtime
            except OSError:
                last = 0
            if time.time() - last >= 300:
                login_stamp.touch()
                log(state_dir, "need_login", pkg_id=pkg_id)
        return

    workdir = Path(pkg.get("workdir", "")).expanduser()
    if not workdir.is_dir():
        if dry_run:
            print(f"[dry-run] 包 {pkg_id} 工作目录不存在：{workdir}")
        else:
            pkg["status"] = "blocked"
            write_json_atomic(state_dir / "queue.json", queue)
            notify(state_dir, "bad_workdir", f"{pkg_id} 工作目录不存在：{workdir}")
            log(state_dir, "blocked_bad_workdir", pkg_id=pkg_id, workdir=str(workdir))
        return

    if dry_run:
        print(f"[dry-run] 会领包 {pkg_id}（workdir={workdir}）")
        return

    pkg["status"] = "in_progress"
    pkg["started_at"] = now_iso()
    write_json_atomic(state_dir / "queue.json", queue)

    env = os.environ.copy()
    env.update(
        {
            "GOAL_STATE_DIR": str(state_dir),
            "PKG_ID": pkg_id,
            "PKG_WORKDIR": str(workdir),
            "PKG_TOKEN_BUDGET": str(pkg.get("token_budget", "none")),
            "ENGINE_TIMEOUT_S": str(int(pkg.get("timeout_s", max_secs))),
        }
    )
    # 清掉同包上一次尝试残留的退出码文件。收包判定以 exit 文件优先，
    # 若不清，重试时会在引擎刚启动的那一拍就被"立即收包"，误判完成。
    try:
        (state_dir / "pkg" / f"{pkg_id}.exit").unlink()
    except FileNotFoundError:
        pass

    logdir = state_dir / "logs"
    ensure_dir(logdir)
    out = (logdir / f"engine-{pkg_id}.out").open("ab")
    proc = subprocess.Popen(
        [str(engine), pkg_id],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # 脱离 launchd job 的进程组，daemon 退出不影响引擎
    )
    write_json_atomic(
        state_dir / "lock.json",
        {"pid": proc.pid, "pkg_id": pkg_id, "started_at": now_iso(), "started_epoch": time.time()},
    )
    log(state_dir, "spawned", pkg_id=pkg_id, pid=proc.pid, workdir=str(workdir))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--state-dir",
        default=os.environ.get("GOAL_STATE_DIR", str(SCRIPT_DIR / "goal-state")),
    )
    ap.add_argument("--engine", default=str(SCRIPT_DIR / "engine.sh"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true", help="常驻模式：每 --interval 秒 tick 一次")
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state_dir = Path(args.state_dir).expanduser().resolve()
    if not state_dir.is_dir():
        print(f"state dir 不存在：{state_dir}", file=sys.stderr)
        return 2
    ensure_dir(state_dir / "logs")
    ensure_dir(state_dir / "pkg")

    if args.loop:
        import signal

        # 子进程自动回收，避免僵尸进程被误判为"仍在运行"
        try:
            signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        except (ValueError, OSError):
            pass

        stop = {"flag": False}

        def _on_signal(signum, _frame):
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
        log(state_dir, "loop_start", interval=args.interval)
        while not stop["flag"]:
            try:
                with (state_dir / ".tick.lock").open("w") as fh:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        tick(state_dir, args.dry_run, Path(args.engine))
            except Exception as e:  # noqa: BLE001
                log(state_dir, "tick_error", error=f"{type(e).__name__}: {e}")
            for _ in range(args.interval):
                if stop["flag"]:
                    break
                time.sleep(1)
        log(state_dir, "loop_stop")
        return 0

    guard = state_dir / ".tick.lock"
    with guard.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        tick(state_dir, args.dry_run, Path(args.engine))
    return 0


if __name__ == "__main__":
    sys.exit(main())
