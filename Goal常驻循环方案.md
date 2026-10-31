# Goal 常驻循环（goal-daemon）方案 v6

状态：**v6（2026-10-31 10:55）——已注册 launchd 常驻运行；新增 `ENGINE: auto`（开机自选后端）与 `doctor.sh` 自检**。
用户已定：无 WorkBuddy automation；单一快循环；10 秒 tick；全天候 24×7；运行中零干扰；**不依赖 codex 等厂商 CLI**；预算暂不限制；通知先不发；试点在 WorkBuddy 工作区；**不要为登录一个没用的 Web 网关折腾**。

**v6 变更（开机即用）**：实测确认 `codebuddy --serve` 起的是 **Remote Gateway**，`dist/` 里**没有构建 Web UI**（页面只有一句 "Web UI is not built"），且默认 `--auth password`，与"登录 WorkBuddy 账号"无关——所以这条路线被放弃，不再要求用户登录它。改为 `ENGINE: auto`：开机自动在 codebuddy（需已登录）/ http 端点之间选一个，都不满足则返回 **93=等待**——包退回 ready、不计失败、不熔断，后端就绪后 ≤10 秒自动开工。配套 `doctor.sh` 一条命令给出"能不能用、差哪一步"。

**v5 变更（纠正 v4 的错误结论）**：v4 写"WorkBuddy 无 CLI / 无 headless 接口"，该结论**已证伪**。WorkBuddy 自带完整 agent 内核 CLI（`codebuddy` 2.165.0），支持 `-p` 无头、`--resume` 续接会话、`--tools` 工具白名单、`--serve` REST API、以及自带后台会话管理（`ps/attach/respawn`）。因此引擎首选改为 **codebuddy**——它就是 WorkBuddy 本体，模型、工具、技能完全一致，不是替代品。`http`（自研）/`custom` 保留为备选。

实现文件：`goal-daemon/`（daemon、engine.sh、engine-http.py、plist 模板、install/uninstall/status、pack.sh、README、三套测试）

---

## 0. 结论摘要

1. 形态：**一个常驻 Python 进程**（`--loop --interval 10`，全天候），launchd `KeepAlive` 拉起与自愈。
2. tick 决策：STOP → 查锁（pid 存活即**纯读退出，零干扰**）→ 有 exit 文件则收包 → 无 exit 文件判失联接管 → `.need-login` 存在则跳过派活 → 空闲领包 spawn 引擎后**立即退出**。
3. 全局单并发；`flock` 防 tick 重叠；引擎用 `start_new_session` 脱离 job 进程组。
4. 引擎三后端（`goal.md` 的 `ENGINE` 切换）：
   - **codebuddy（首选）**：调 WorkBuddy 内核 CLI，`--session-id` 首次、`--resume` 续接，等于一个持续工作的 WorkBuddy 会话；
   - **http**：自研 agent 循环，任意 OpenAI 兼容端点，纯标准库；
   - **custom**：用户自写脚本，接口不变。
5. 调度器：launchd（拉起/自愈）+ 自带 loop（10 秒）已满足，**不引入 Supercronic / pytocron**（§3.1）。

---

## 1. 目标与非目标

**目标**：G1 跨天推进、≤10 秒接续；G2 运行中零干扰；G3 全程 JSONL 可观测；G4 硬护栏（STOP/熔断/单并发/写界/登录态）。

**非目标**：不做常驻内存 LLM；daemon 不做验收、不碰 Git；不改变《协作机制》五模块纪律（M3 再扩）。

---

## 2. 本机实测（2026-10-31，逐条可复现）

| 事项 | 结果 |
|---|---|
| **WorkBuddy 内核 CLI** | **存在**：`/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy`，`--version` → `2.165.0`（`@genie/agent-cli`） |
| 其 headless 能力 | `-p/--print`、`--output-format json`、`--session-id`、`-r/--resume`、`--resume-create-missing`、`--tools`、`--allowedTools`、`--add-dir`、`--permission-mode`、`--serve`、`--acp` |
| 其后台会话管理 | `ps` / `attach` / `kill` / `respawn`（保留对话重启）/ `agents` / `daemon` 子命令 |
| **运行依赖 node** | shebang 是 `#!/usr/bin/env node`；普通终端与 launchd 的 PATH 均无 node → `env: node: No such file or directory`。已由 `resolve_node()`（`CODEBUDDY_NODE_BIN` → PATH → `~/.workbuddy/binaries/node/versions/*/bin/node`）用绝对路径解决，并把 node 目录注入 plist `PATH` |
| **依赖 lite-bundle 开关** | `dist/` 只发布 `codebuddy-lite-wb.mjs`，无 full/headless 入口；纯净环境报 `Cannot find module '../dist/codebuddy'`。宿主 spawn sidecar 时自设 `CODEBUDDY_FORCE_LITE_WB_BUNDLE=1`，引擎侧现已显式同设 |
| **端口坑** | GUI 已占 `127.0.0.1:59034`，CLI 默认复用该端口 → `EADDRINUSE`。必须显式 `--port <空闲端口>` |
| **登录态** | GUI 登录态**不复用**，`~/.codebuddy` 为空 → `Authentication required`。无 `login` 子命令，需交互式 TUI 里 `/login` |
| **失败信号坑** | 未登录时 `codebuddy -p` **退出码仍为 0**，错误只在 stderr → 只看退出码会把失败当成功 |
| Homebrew / Go | 均未安装 |
| 系统 Python | 3.9.6（`/usr/bin/python3`，daemon 与引擎均兼容此版本） |
| supercronic | v0.2.51 仅发 linux-*，无 darwin → 不可用 |
| pytocron | 存在（2.0.0，AGPL-3.0，需 Python≥3.10，前台运行、无热重载） |
| Codex CLI | 存在但**按用户要求不依赖** |
| OpenAI 兼容端点 | 本沙箱内 3000/3001/8080/8000/11434 均不可达（需用户提供真实地址） |

---

## 3. 选型

### 3.1 调度层：自带 `--loop`（定稿）

| 方案 | 结论 |
|---|---|
| launchd `StartInterval=10` | 可用，但每次都是新进程；"常驻感"弱 |
| **自带 `--loop` + launchd `KeepAlive`** | ✅ 采用：一个常驻进程，10 秒 tick，崩溃由 launchd 拉回；零依赖 |
| Supercronic | ❌ 无 darwin 二进制，无 Go/brew 无法自编 |
| pytocron | ❌ 需 Python≥3.10（系统 3.9.6），AGPL-3.0，不支持热重载，且自身也需外层守护 → 无净收益 |

若日后迁 Linux/容器，`--once` 模式可直接挂到 pytocron/Supercronic 的 7 段 crontab（`*/10 * * * * * *`），代码不变。

### 3.2 引擎层：codebuddy 首选（v5 定稿）

**codebuddy 后端**（`ENGINE: codebuddy`）：

```bash
codebuddy -p "<单包任务>" \
  --session-id <首次> | --resume <之后> --resume-create-missing \
  --add-dir "$PKG_WORKDIR" \
  --allowedTools "Read,Write,Edit,Bash,Glob,Grep" \
  --permission-mode bypassPermissions \
  --port 59187 \
  --output-format json
```

- `session.id` 持久化在 `goal-state/session.id`，首次生成 UUID 并用 `--session-id`，之后一律 `--resume` → **同一会话连续推进**，这正是"断点续派"。
- `--add-dir` + `--allowedTools` 构成**真正的能力边界**（比 prompt 约束硬）。
- 结果从 JSON 的 `result` 字段提取写入 `pkg/<id>.last.md`；**输出为空一律判失败**，防"假交付"。
- 失败语义：`92` = 未登录（落 `.need-login`，daemon 跳过派活且不计失败）；`91` = CLI 不可执行；其他为原始退出码。

**http 后端**（`ENGINE: http`，备选）：自研 `engine-http.py`，只需 OpenAI 兼容 `/chat/completions`，4 工具（run_shell/read_file/write_file/list_dir），写界白名单硬校验，纯标准库。

**custom 后端**（`ENGINE: custom`）：自写 `engine-custom.sh`，用于演示与特殊场景。

### 3.3 引擎适配器契约（冻结，三后端一致）

```
engine.sh <package_id>
  环境：GOAL_STATE_DIR / PKG_WORKDIR / PKG_TOKEN_BUDGET
  产出：pkg/<id>.exit（退出码）、<id>.last.md（汇报）
  约束：单包单会话；不接收全局 goal；不得写白名单外路径；不得 commit
```

---

## 4. 状态协议（已实现）

`goal-state/`：`goal.md`（唯一目标源）、`queue.json`（daemon 独占写）、`checkpoint.json`、`heartbeat.json`（引擎 15 秒刷新）、`lock.json`、`STOP`、`session.id`、`.session.started`、`.need-login`、`pkg/<id>.{task.md,last.md,exit,cb.json,cb.err}`、`logs/`。

判"运行中"的唯一依据：**lock 存在 + pid 存活**（心跳仅辅助）。mtime/猜测不算。

队列状态：`pending → ready → in_progress → delivered`；失败累加 `fail_count`，达 `MAX_FAIL` 转 `blocked`；**`done` 只能由人置**。

---

## 5. 验收

无独立验收者，故：引擎自验 + 分层状态（`delivered` 待审）。`delivered` 不阻塞领其他无依赖包；`done` 需人（或人让 WorkBuddy 审后）确认。daemon 只记录，不裁决。

---

## 6. 护栏（已实现）

STOP 一键停机（在跑会话不打断）、单并发、写界白名单、失联接管（失败+1，不自动杀进程）、熔断（3 次转 blocked）、登录态熔断（`.need-login` 期间不派活、不累计失败）、空输出判失败（防假交付）、Git 隔离（引擎被明令禁止 commit）、耗时/退出码/改动文件全落盘。

---

## 7. 里程碑

| 里程碑 | 状态 |
|---|---|
| M0 协议冻结 | ✅ §3.3、§4 |
| M1 daemon + 引擎 + 常驻 | ✅ 端到端冒烟通过（mock 端点）：spawn → busy → collected(delivered) → STOP → idle |
| M1.5 codebuddy 后端 | ✅ 18 项专项测试通过（成功/续接/未登录/空输出/daemon 侧护栏，全部用假 CLI，零成本） |
| M2 接真实目标 | ⏳ 待用户完成 codebuddy `/login`，或提供 `ENGINE_BASE_URL` + 模型；并选定试点目录 |
| M3 多模块扩展 | 未开始 |

---

## 8. 待用户提供

| # | 事项 | 说明 |
|---|---|---|
| 1 | **codebuddy 登录**（推荐路径） | 跑 `./login.sh` → 在 TUI 里 `/login`，登录一次即可长期复用（脚本已备好 node 绝对路径与 lite-bundle 开关） |
| 2 | 或 `ENGINE_BASE_URL` / `ENGINE_MODEL` / `ENGINE_API_KEY` | 若走 `ENGINE: http` |
| 3 | 试点目标与目录 | 用户称自行找系统试 |
| 4 | 是否立即 `./install.sh` 装成常驻服务 | 当前仅代码就绪，未注册 launchd |

已定不再询问：预算不限制、不通知、全天候、10 秒、零干扰、不依赖 codex。

---

## 9. 当前状态

- 代码：`goal-daemon/` 全部就绪；四套测试全绿（回归 12/12、codebuddy 18/18、auto 12/12、端到端冒烟 delivered）。
- 系统：**已注册 launchd 并常驻运行**（服务名 `local.goal-daemon`，`--loop --interval 10`，`KeepAlive` 自愈）。
- 引擎：`ENGINE: auto`。当前实测 **codebuddy 未登录**（`--probe` 返回 92），HTTP 端点未启动 → 处于 `93 等待`；包保持 ready，不计失败。
- 依赖：无 pip 依赖、无 brew、无厂商 CLI；codebuddy 为 WorkBuddy 自带组件。
- 自检：`./doctor.sh`（只读）/ `--fix`（补齐）/ `--probe`（真实跑一次）。
- 若要搬到其它 Mac：`./pack.sh` 产出的包内已含本方案，装完重跑 `install.sh` 注入新路径。
- 待用户决定：是否 `./login.sh` 登录一次（codebuddy 路线），或把 `ENGINE_BASE_URL` 指向自建网关（零登录）。
