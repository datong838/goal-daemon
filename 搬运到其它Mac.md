# 搬运到其它 Mac（给那边的 WorkBuddy 用）

## 0. 这个包是什么

一个**自包含**的常驻调度器：每 10 秒 tick 一次，按 `goal-state/queue.json` 领活、调引擎干活、收包记账。纯 Python 标准库 + bash，**不需要 pip / brew / node / docker**。

已实测：系统自带 Python 3.9.6 可跑；无第三方包；无网络也能跑（`ENGINE: custom` 时）。

## 1. 前置条件（只有这些）

- macOS（launchd 自启；Linux 需换 systemd，Windows 不支持——用了 `fcntl` 文件锁）
- `/usr/bin/python3` 存在（macOS 自带；`python3 -V` 能出版本即可）
- 解压目录**不要含空格**，省得路径转义出岔子

## 2. 三步装好

```bash
tar -xzf goal-daemon-YYYYMMDD.tar.gz
cd goal-daemon-YYYYMMDD

# ① 填端点（唯一需要改的地方）
vi goal-state/goal.md          # 改 ENGINE_BASE_URL / ENGINE_MODEL / ENGINE_API_KEY

# ② 预检：只生成并校验 plist，不注册
DRY_RUN=1 ./install.sh

# ③ 正式安装（注册 launchd，开机自启 + 崩溃自愈）
./install.sh
```

装完看状态：

```bash
./status.sh
launchctl print gui/$(id -u)/local.goal-daemon | head -20
tail -f goal-state/logs/*.log
```

## 3. goal.md 怎么写

```ini
OBJECTIVE: 一句话目标
ENGINE: http                       # 或 custom（不联网，用你自己的脚本）
ENGINE_BASE_URL: http://127.0.0.1:3000/v1
ENGINE_MODEL: gpt-4o-mini
ENGINE_API_KEY:                    # 本地网关可留空
DEPS_REQUIRE: done                 # 严格口径；写 delivered,done 则引擎交付即算过
MAX_FAIL: 3                        # 同包连续失败 3 次熔断
MAX_PKG_SECONDS: 3600
NOTIFY: none
```

**没有可用端点就先写 `ENGINE: custom`**，daemon 会去调包里的 `engine-custom.sh`，你把它换成自己的脚本即可（契约见下）。这样包搬过去立刻能跑，不必先去配模型。

## 4. 自定义引擎契约（三个文件就够）

引擎是一个可执行文件，daemon 这样调它：

```bash
engine.sh <package_id>
# 环境变量：GOAL_STATE_DIR / PKG_WORKDIR / PKG_TOKEN_BUDGET / ENGINE_TIMEOUT_S
# 必须产出：
#   $GOAL_STATE_DIR/pkg/<id>.exit     退出码（0=成功）
#   $GOAL_STATE_DIR/pkg/<id>.last.md  最终汇报
```

**注意**：不要绕过 `engine.sh` 直接调你的脚本——写退出码的是外壳，绕过它会被判"会话失联"并扣失败次数。

## 5. 日常操作

| 想做 | 命令 |
|---|---|
| 加任务 | 编辑 `goal-state/queue.json`（照 `queue.example.json` 抄） |
| 暂停 | `touch goal-state/STOP`（下一秒即停领新活，正在跑的包跑完） |
| 恢复 | `rm goal-state/STOP` |
| 卸载 | `./uninstall.sh` |
| 搬家 | 整个目录拷走 → 在新位置重跑 `./install.sh` |

## 6. 关于"和那边的 WorkBuddy 配合"

这一条必须说清楚，否则会误判：

**WorkBuddy 没有 CLI / 无头接口，daemon 无法直接唤醒一个 WorkBuddy 会话。** 所以那边的配合只有两种形态：

**形态 A：daemon 干活，WorkBuddy 审**
daemon + `engine-http` 自动跑包，产物落在 `pkg/<id>.last.md`。WorkBuddy 会话定期读 `checkpoint.json` 和这些 last.md，做验收、改队列。`delivered` 需要人审后才置 `done`——这是故意的，防止机器自己给自己发合格证。

**形态 B：daemon 出题，WorkBuddy 作答**（本机演示过的那种握手）
把 `engine-custom.sh` 换成"写工单 → 阻塞等答复文件"的版本（参考 `demo-mrpmes/`，但它没打进本包）。WorkBuddy 在对话框里写完答复，引擎就继续。缺点是需要人在会话里推动，不是全自动。

想全自动选 A，想要判断题选 B。

## 7. 三条硬约束（别踩）

1. **只有一个绝对路径锚点 `WorkingDirectory`**。launchd 的 `ProgramArguments[0]` 若写相对路径会按 `/usr/bin:/bin:...` 去找而不是按 cwd（`man 5 launchd.plist` 明说）。脚本名 `goal-daemon.py` 是交给 python 的参数，才可能相对。所以**搬目录后必须重跑 install.sh**。
2. **同一个 `goal-state/` 不能被两台机器的 daemon 同时跑**。锁是本机 `flock`，跨机器不互斥，会双开领同一包。多机要么各用各的 state 目录，要么只在一台装 launchd。
3. **接真实项目时让引擎只读**。若目标仓库有别的会话正在写（很常见），daemon 的写操作会与它抢文件。至少先把 `write_scope` 限死在一个空目录里试。

## 8. 自检（装完想确认没坏）

```bash
./test-regressions.py    # 11 项断言，零成本不联网
./smoke-e2e.py           # 端到端：spawn→busy→收包→STOP→idle
```

两个都通过，说明这套东西在那台机器上是好的。
