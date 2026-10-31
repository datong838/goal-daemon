# goal-daemon — Goal 常驻循环

一个每 10 秒 tick 一次的常驻进程，把「一个目标 + 一串任务包」持续往前推。
派完活就走，**正在跑的会话绝不被打扰**。不依赖 codex / claude 等厂商 CLI，也不依赖任何第三方调度器。

> **完整操作手册见 [使用说明.md](使用说明.md)**（安装、任务包怎么写、排错、原理全在里面）。
> 本文只做快速入口。

---

## 一分钟上手

```bash
cd /path/to/goal-daemon   # 换成你的安装目录（解压到哪就用哪，路径不写死）

./doctor.sh          # 1) 先看能不能用、差哪一步（只读，7 段自检）
./install.sh         # 2) 装成常驻（必须在 macOS 自带「终端」里跑，见下方注意）
./status.sh          # 3) 看状态
```

装完它就在跑了。队列空了会 `idle`——但**空掉之前它会先看上一包的汇报**：
如果汇报里还留着「仍未修」，就自动长出下一包继续干（见下方「回流」）。

### 人工只发两个字：`继续`

```bash
bash goal-daemon/动手.sh      # 在对话框里发「继续」，等价于跑这一条
```

它做四件事，然后给一句结论（有活 / 没活 + 为什么）：清 `STOP` 刹车片 →
看队列有没有 `ready` / `in_progress` → 没有就试一次自动回流 → 敲一下 launchd，
等一拍，把落地的事件和在跑的包打出来。

**它不是审批**：不点确认、不改任何包状态，只是把机制踢一下。
`继续` 之后仍然没活，说明剩下的全在 `blocked.md` 里等人——那种情况它会把原因说出来，
而不是凭空造一个包。

> 整套东西不绑定任何机器：安装目录由脚本自身位置推导，WorkBuddy 位置与 node 自动探测，
> launchd 服务名默认 `local.goal-daemon`。搬目录后重跑一次 `./install.sh` 即可。

> **注意**：`install.sh` 在 WorkBuddy 的集成终端里会报 `Bootstrap failed: 5`。
> 那不是脚本问题——那种终端不在你的登录会话里，`launchctl` 写不进去。
> 用 `launchctl list | wc -l` 判断：几百条就对，0 条就换终端。
> 日常的 `status.sh` / `review.sh` / `doctor.sh` 在哪个终端都行。

---

## 它怎么干活

```
goal.md（目标+配置） → goal-daemon.py（每 10 秒看一次） → 派一个包 → 引擎去做 → 回来收
```

引擎是哪个，由 `goal.md` 的 `ENGINE` 决定。**本机默认用 `wbrun`**：

通过 WorkBuddy 桌面端的本机会话网关提交任务，**免登录、免 API key**，
直接复用你桌面端已登录的账号和模型额度。

---

## 常用命令

| 我想…… | 命令 |
|---|---|
| 看状态 | `./status.sh` |
| 自检、看差哪一步 | `./doctor.sh` |
| 真发一次最小任务验证引擎 | `./doctor.sh --probe` |
| 看有哪些包跑完了等审 | `./review.sh` |
| 看每包还剩几项遗留 | `./followup.py --list` |
| 手动回流成下一包 | `./followup.py --from <id> --apply` |
| 放行 / 打回 | `./review.sh <id> done`、`./review.sh <id> retry` |
| 停机 / 恢复 | `touch goal-state/STOP`、`rm goal-state/STOP` |
| 卸载 | `./uninstall.sh` |
| 打包搬走 | `./pack.sh` |

---

## 回流：它凭什么算 Loop

只在 `queue.json` 里预置任务、跑完就 idle —— 那是批处理，不是 Loop。
真实情况是：一个包交付时，汇报结尾常常写着「仍未修 3 条」，然后就停在那里了。

**回流层把这三条变成下一包。**

```
vd-003 delivered（默认免审，收包即 done）
   └─ 汇报里「仍未修」3 条
        ├─ 机器能做的 2 条 → daemon 下一拍自动生成 vd-003-f1
        │     （deps 指回 vd-003，写界继承）
        │        └─ 派发 → 执行 → 汇报 → 还有遗留？→ f2 → f3 → …
        │             └─ 没有遗留 → clean，Loop 正常收敛
        └─ 机器做不动的 1 条（真人/真机/身份源/上游文件/要人拍板）
              └─ 记进 goal-state/blocked.md，不派包
```

**默认没有审批环节**：引擎交付即 `done`，下一包直接排，不需要人按任何键。
要回到"每包都必须人放行"的老模式，`FOLLOWUP_REQUIRE_DONE: on`。

四条自保，缺一条就会变成失控或空转：

| 开关 | 默认 | 作用 |
|---|---|---|
| `FOLLOWUP: on` | off | 不开就是纯批处理 |
| `FOLLOWUP_REQUIRE_DONE: off` | off | 关=免审直接追；开=每包都要 `./review.sh <id> done` 才推下一步 |
| `FOLLOWUP_MAX_DEPTH: 0` | 0 | 0 = 不限层，一直 Loop；正整数 = 最多追 N 层 |
| `FOLLOWUP_NO_PROGRESS: on` | on | 新遗留项与父包被派的项几乎同文（≥0.9）→ 判原地打转，停下并记 `no_progress` |
| `FOLLOWUP_PARK: on` | on | 做不动的项（真人/真机/凭证/上游/拍板）记账不派，见 `goal-state/blocked.md` |
| `PROTECTED_ROOTS: none` | 空 | **硬闸**：逗号分隔的绝对路径。包的工作目录或写界落进去就拒派并置 `held` |

### 别再来动这个仓：`PROTECTED_ROOTS`

一句"这个仓别再动了"口头约定是守不住的：队列里只要留着一个 `ready` 的包，
守护进程每拍就会把它捡起来，人不在场时没人拦得住。所以闸放在引擎里：

```yaml
PROTECTED_ROOTS: /path/to/repo,/path/to/another
```

- 派发时检查 `workdir` 与 `write_scope`（任一命中即算），命中就**不派**，包置 `held`
- 回流也检查：父包在被保护的仓里，连子包都不生
- `held` 是暂停不是失败，`fail_count` 不加，不会熔断
- 恢复只需删掉这一行，held 包自动回 `ready`（`release_held()`，每拍都跑）
- `status.sh` 会打一行 `🔒 受保护根`

它不理解业务，只比路径。所以 `PROTECTED_ROOTS` 是**运行时的值**，引擎代码里
没有任何项目名——换目标、换仓，改 `goal.md` 即可，代码一行不动。


最后一条是拆掉审批之后**唯一还会空转的地方**：汇报里那类"缺 5 名真人""缺真机"
"仓库只有一端""需视觉定调"的条目，派下去唯一的结果是写一句「做不了」，
下一包再把它抽出来派一遍 —— 一个预算周期换一页废话。所以它们进台账不派包，
Loop 继续往前跑；剩下的活全做完、只剩这类项时停下（`all_blocked`），等人来。

**调度器不把"作者已经判过的东西"当待办**，这几种写法一律不派：

| 写法 | 为什么不派 |
|---|---|
| `~~被划掉的条目~~` | 作者亲手划掉＝已作废，即使句子里还有「未/等/缺」 |
| `按可选优化记` `不改也能过` `非必须` | 作者自己说了不做也过，派下去是空转 |
| `答"该" → 做 A` `若答"不该" → 关闭` | 决策分支的两支，不是两件待办 |
| 列表项跨行写（缩进续行） | 必须拼回同一条，否则丢掉第二行的判断词 |

最后一条踩过两次：`vd-003-f4` 的「…那一行高了 26px，」在行末断开，
「未与人确认是否可接受」在下一行，只读首行就把"要人确认"读成了"机器能做"。

新包的指令不是把父包重跑一遍，而是**逐条列出遗留项**，并且写死一条要求：

> 每条给确定结论——改掉了（写清位置），或做不了（写清卡在哪、该找谁）。
> **不接受把「仍未修」原样留在汇报里。**

某条已经修好、但不想改历史汇报？在 `goal-state/pkg/<id>.recheck.md` 写一行：

```
RESOLVED: <那条遗留项的原话>
```

回流时自动扣掉，不再派。汇报是交付物，改它等于篡改历史，所以要另开一个账本。

手动看一眼它会长出什么：

```bash
./followup.py --list                      # 每个包还剩几项遗留
./followup.py --from vd-003 --dry-run     # 会生成哪个包、列哪几条
./followup.py --from vd-003 --apply       # 不等 daemon，立刻写盘
```

---

## 两件最容易搞错的事

**1. `delivered` 与 `done` 的差别**

引擎跑成功只代表自验通过，不代表结果对。

- 默认（`FOLLOWUP_REQUIRE_DONE: off`）：收包直接置 `done`，并记 `reviewed_by: "auto"`，
  不留一堆永远等不到人的 `delivered`。
- 设成 `on`：收包置 `delivered`，只有 `./review.sh <id> done` 才变 `done`——人工把关模式。

两种模式下 `DEPS_REQUIRE: delivered,done` 都能让串行流水线跑起来。

**2. 不可用 ≠ 失败**

后端暂时不可用（网关失联、端点没起）时，包退回 `ready` 等下一拍，**失败计数不增加**。
所以你看到 `fail_count=0` 的包卡了几十次，那不是坏了，是在等。

---

## 目录

```
goal-daemon.py    主循环          status.sh   状态面板
engine.sh         引擎分派        review.sh   审阅闸门
engine-wbrun.sh   wbrun 后端      doctor.sh   自检
engine-http.py    自研 agent 循环  install.sh  装 launchd
engine-custom.sh  自定义样板       pack.sh     打包
followup.py       回流：汇报→下一包
goal-state/       运行时状态（唯一事实源）
```

---

## 测试

```bash
/usr/bin/python3 test-regressions.py        # 12 项：状态机与护栏
/usr/bin/python3 test-codebuddy-backend.py  # 18 项：codebuddy 后端
/usr/bin/python3 test-auto-backend.py       # 12 项：auto 选路
/usr/bin/python3 test-followup.py           # 23 项：回流层（解析/闸门/深度/幂等）
/usr/bin/python3 test-engine-fixes.py       # 13 项：超时认产出 / 残留旧产出 / 已交付不重派
/usr/bin/python3 smoke-e2e.py               # 端到端冒烟（mock 端点，零成本）
```

### 失败判定的三条硬规则

改这三条会重新开始"伪造失败"（都踩过，细节见使用说明「失败是怎么判的」）：

1. 超时但产出已落盘 → `exit=0`，不是失败；
2. 只认本次提交之后写的 `last.md`，旧汇报记 `stale` 判失败；
3. `last.md` 已存在就不再派活，直接判 `delivered`。

---

## 更多

- [使用说明.md](使用说明.md) — 完整手册：安装三条路径、任务包六要素、四种引擎、状态机、12 类排错、原理与限制
- [Goal常驻循环方案.md](Goal常驻循环方案.md) — 设计依据
- [搬运到其它Mac.md](搬运到其它Mac.md) — 搬运细节与坑
