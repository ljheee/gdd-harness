# gdd go — 单命令交互模式设计

> 状态：**已实现**（gdd/cli.py `cmd_go`；测试 tests/test_e2e.py G2 七场景 + 7b）。落地与本文差异：
> - 审批交互为 [y]/[f]/[a]/[c]（内联 review 澄清对话：/done 重验、/q 回审批菜单）/[回车跳过]，
>   未识别输入保持挂起回循环；EOF（Ctrl-D）在审批/blocked 窗口与回车同义（退出/保持），
>   但 pending 澄清窗口例外：回车=跳过并启动主体、EOF=退出 go（见 _go_clarify_pending）；不忙循环；
> - 引擎新增 `defer_approval` 开关（go 交互模式启动）：审批一律进挂起态而非内联
>   y/n/a——由 go 循环自己交互（回车跳过/并发裁决友好），§4.2 落地时即为自建循环；
> - pending 窗口 /done 无 recheck 时直接启动主体（窗口不重开）；/q 退出窗口；
> - run 锁忙（外部 gdd 命令在跑）回主循环重读状态，不退出 go；判定不再依赖锁忙文案，
>   引擎提供 `LockBusyError` 专用异常类型（orchestrator.py），cli/ui 均 isinstance 判定。
>   锁忙处退避等待 `_wait_lock`：0.5s 步进探测、每 5s 提示一次、拿到锁即返回（覆盖主循环 run
>   与 _go_approval 的 approve/驳回两分支，消除无提示忙转）；
> - gdd run 内联审批的 EOF/Ctrl-C 不再静默中止流水线：保持挂起（keep 语义，
>   interactive_approval 返回 "keep"，引擎建 pending_approval 快照），交给 gdd approve/go 裁决；
> - §8 心跳未采纳（被 §9 流式回显替代：推进/澄清期有实时输出，静默期可仍 tail `.gdd/logs/`）。
> 对应诉求：像 Claude Code 一样，一个命令启动后，进度通知、人工交互（审批/澄清/blocked
> 处置）全部发生在同一 terminal；澄清与主体会话再加流式可见性。

## 1. 目标与非目标

**目标**
- 单命令 `gdd go` 覆盖完整生命周期：未初始化 → 引导 init → 推进 → 交互 → 终态
- 进度**事件级**实时通知（节点进入/门禁结论/打回/挂起），不需另开终端敲 `gdd status`
- 全部人工交互内联：审批 y/n/f、澄清对话、blocked 处置
- Ctrl-C 随时中断：锁释放、进度落盘、再次 `gdd go` 原地续跑
- **流式回显（P0/P1）**：澄清对话全文流式；主体推进期工具摘要 + 截断文本流式（见 §9）

**非目标（明确边界）**
- **完整 Claude Code TUI**：不做 markdown 渲染面板、不做自由闲聊改代码的第二会话
- **门禁会话流式**：entry/exit 门禁保持安静（只靠 `on_event`），避免验证噪声淹没审批证据
- **自由对话改代码**：GDD 范式是「需求 → 门禁 → 产物」，对话只发生在澄清窗口与打回反馈
- **不新增编排语义**：go 不引入状态机新状态、不绕过任何门禁/审批/指纹校验
- 流式文案**不是**过关证据；权威结论仍在门禁/产物

## 2. 架构：go 是驾驶舱，不是第二引擎

```
┌─────────────────── gdd go 主循环（cli.cmd_go）───────────────────┐
│                                                                    │
│  ① 未初始化？→ 交互引导 init（选模板/输需求）                        │
│                                                                    │
│  ② 循环：load state（以磁盘为准）→ 按 status 分派                    │
│     ┌─ running           → o.run()   ────────┐                     │
│     ├─ awaiting_approval → 终端审批 → 回循环  │                      │
│     ├─ 澄清窗口可用       → 可对话/跳过 → 回循环│  全部调用既有 API    │
│     ├─ blocked           → retry/abort/退出   │  （带 flock+门禁）   │
│     └─ completed/aborted → 终态打印，退出     ┘                     │
└────────────────────────────────────────────────────────────────────┘
```

核心原则：
1. **决策与执行分离**：循环体只做「读状态 → 调入口」。`run / resolve_approval / retry / chat / clarify_done` 全部是 Orchestrator 既有入口，各持 flock 与指纹门禁；打回、bounces、会话组、entry 清下游等编排逻辑 100% 留在引擎——go 是把「多个命令的手工调度」自动化，不复制任何状态判断逻辑
2. **单一权威**：每轮重新 `load()` state.json。终端命令行与 UI 后台线程可同时操作；状态已被他方推进时，入口的前置校验（非 awaiting / 非 pending 窗口等）拒绝执行，go 捕获后刷新状态重入循环——与现有 CLI 交互审批同语义
3. **进度可见零侵入**：Orchestrator 新增可选 `on_event` 回调（构造参数，缺省 None 完全不影响现有行为）。`_record()` 已是统一事件流，在写入后同步调用回调；go 传入 printer 实现实时通知

## 3. 进度通知（on_event）

事件即 `_record` 的事件名（真实全集见 orchestrator.py `_record("...")` 调用点；以下为 go 呈现的子集与映射——**事件名以实现为准，实现时按此表核对**）：

| 事件（真实名） | go 终端呈现 |
|---|---|
| `node_done` / `subject_resume` | `✔ 节点完成 {id}` / `⋯ 主体会话续用` |
| `entry_pass` / `entry_fail` | `✔ 前置门禁通过` / `↩ 打回上游：{reason}` |
| `exit_pass` / `exit_fail` | `✔ 出口门禁通过` / `↩ 打回主体：{feedback 摘要}` |
| `awaiting_approval` | `⏸ 人工准入挂起` → 进入审批交互 |
| `blocked` | `⛔ 阻塞：{reason}` |
| `pipeline_completed` | `✅ 流水线完成` |
| `git_push` / `git_push_fail` / `notify_print` 等交付/周知类 | 原样透出（不静默） |

（其余事件如 `approval_pass`/`bounce_current`/`clarify_msg` 等可按需增补呈现；未映射事件默认不打印。）

实现要点：回调在**持有 flock 的引擎线程内**同步执行，printer 只做格式化打印（无 IO 锁竞争）；`_record` 失败不影响主流程。新增事件若需要（如 `node_start`、`subject_start`），在 `_record` 对应位置补埋点——本表未虚构现有事件集。

## 4. 交互协议（全部为终端 input，非 tty 时跳过）

### 4.1 初始化引导（未初始化时）
```
未初始化。选择模板：
  1) delivery（内置：需求交付流水线，9 节点）
  2) 输入模板文件路径（JSON/YAML）
> 1
输入需求（一行；或路径文件）：
> 给购物车加优惠券叠加规则
```

### 4.2 审批（awaiting_approval）

> 落地差异：引擎 `defer_approval=True`（go 交互模式）使审批一律进挂起态，
> 内联 y/n/a 不触发；以下交互在 go 循环内自建。

```
⏸ 人工准入：plan（方案确认）
  审批要点：……（approval.prompt）
  exit 依据：……（last_exit_evidence 摘要）
  [y] 批准   [f] 驳回（需反馈）   [回车] 跳过（保持挂起，回到提示符可先 chat）
> y
```
驳回时二次输入反馈文本（注入上游重做）。产物快照变更拒绝（`approval_refused_artifact_changed`）原样展示并回到循环（引导 clarify_done 或驳回）。

> 注：现行交互审批（approval.py）为 y/n/a 三选，无「跳过」——go 的 [回车] 跳过属**行为变更**（保持挂起而非必须裁决），实现时需在 cmd_go 自建循环而非复用 interactive_approval，并保留 a 中止选项。

### 4.3 澄清窗口（pending 的 clarify 节点 / 审批挂起 review 模式）
```
◦ 节点 plan 处于澄清窗口：可直接输入消息与 AI 对齐；/done 结束并重验；回车跳过
你> 优惠券叠加是否有上限需求？
AI> ……
你> /done
✅ 澄清结束；结论须已固化产物，将重过 exit 门禁
```
复用 `cmd_chat` 的循环体（20000 字上限、/q、Ctrl-C 语义不变）；回车空行 = 跳过直接 `o.run()`。

### 4.4 blocked
```
⛔ 阻塞：implement 连续 3 次 exit fail
  原因：……（blocked_reason）
  [r] 重试（清零打回计数）   [a] 中止   [回车] 退出 go
> r
```

## 5. 关键设计决策

| 决策 | 依据 |
|---|---|
| 非 tty 自动退化为 `run` | 复用 `is_interactive()`（线程感知：后台/管道恒 False），CI/脚本里 `gdd go` ≡ `gdd run`，零分叉代码 |
| 每轮 load 而非缓存 state | UI 后台线程可并发推进（同进程）；外部 `gdd approve` 可并发裁决（跨进程）。磁盘是唯一权威，与 UI 轮询同语义 |
| go 内不提供 abort 之外的写操作 | reset/init 引导只在未初始化态出现；运行中变更类操作仍走既有子命令，避免 go 变成第二套 CLI |
| 事件通知不落新文件 | 事件流水在 state.json `history`（`_record` 落盘，裁剪至最近 500 条），`gdd status` 可回看；go 只做终端呈现层 |
| Ctrl-C 语义 | 引擎侧已保证（锁 finally 释放、进度落盘）；go 循环捕获 KeyboardInterrupt 打印续跑提示后退出码 130 |

## 6. 竞态与安全（继承自既有机制，无需新代码）

- flock：每个引擎入口互斥，go 调用即受益；`is_interactive` 线程感知防 go 内 input() 与 UI 后台 run 死锁
- 四入口指纹门禁：approve/chat/clarify_done/retry 拒绝时 `last_refusal` 可见，go 原样展示并回循环
- 审批产物快照：approve 前自动核验（glob 展开），变更拒绝保持挂起——go 无旁路

## 7. 实现触点与测试策略

触点（go 本体 + 流式）：
- `gdd/orchestrator.py`：`on_event` / `on_stream(role)`；`_record` 尾部调用；chat/subject 注入流式
- `gdd/cli.py`：`cmd_go` / `cmd_chat`；tty 下挂 `stream_ui.factory_for_tty()`
- `gdd/runner.py`：`on_stream` → `stream-json`；无回调保持 `json`
- `gdd/stream_ui.py`：P0 clarify 全文 / P1 subject 工具+截断文本打印机
- 文档：README 命令表、USER_GUIDE、本文 §9

测试（test_e2e 新增场景 G2，stub agent）：
1. **完整 go 循环**：monkeypatch input 序列（模板选择/需求/y 批准/回车跳过）→ 断言走到 completed；on_event 收到 `entry_pass`/`awaiting_approval`/`pipeline_completed` 序列
2. **非 tty 退化**：FakeTty 关闭 → go 行为收敛为单次 run
3. **并发变更**：go 审批提示前外部改 state → 入口拒绝 → go 刷新重入（不崩）
4. **Ctrl-C**：循环中注入 KeyboardInterrupt → 退出码 130、锁已释放（可再 run）

## 8. 心跳（已关闭）

主体长时段静默曾考虑每 60s 心跳；**不采纳**。P0/P1 流式回显已覆盖「是否在干活」的可见性；更深细节仍可 `tail .gdd/logs/`。

## 9. 流式回显（P0 clarify / P1 subject）

> 状态：**已实现**（`gdd/runner.py` stream-json；`gdd/stream_ui.py` 呈现；orchestrator
> 经 `on_stream` 工厂注入；`gdd go` / `gdd chat` 在 tty 下启用）。

### 9.1 目标分层

| 优先级 | 会话 | 终端呈现 |
|---|---|---|
| **P0** | clarify（pending / review） | `AI> ` 后 **assistant 文本 token 流式**（接近 Claude Code 打字感） |
| **P1** | subject（节点推进） | 每工具一行摘要（`⋯ Write path` / `⋯ Bash: cmd`）+ **截断** assistant 文本 |
| — | gate（entry/exit） | **不流式**（安静；结论走 `on_event`） |

### 9.2 协议（runner）

- 仅当本次 `run(..., on_stream=callback)` 传入回调时切换：
  `--output-format stream-json --verbose --include-partial-messages`
- 无回调时保持既有 `--output-format json`（CI / 门禁 / 单测零侵入）
- 按行消费 NDJSON，归一为内部事件后回调：
  - `{"kind":"text","text":"..."}` ← `stream_event` 的 `text_delta`，或完整 `assistant` 文本块（无 partial 时兜底）
  - `{"kind":"tool","name":"Write|Edit|Bash|...","summary":"..."}` ← `assistant.message.content[]` 的 `tool_use`
- **超时**：stdout 读行在独立线程，主循环 `queue.get(timeout)` 守墙钟——避免
  `readline()` 无换行时永久阻塞（与 buffered `communicate(timeout=)` 对齐）
- 主体每次 `runner.run` 新建打印机（resume→fresh 截断额度不串）
- `gdd chat` / `gdd go` 均以 `is_interactive()`（主线程+双 tty）决定是否开流式
- 末行 `type=result` 仍解析为既有 `SessionResult`（`ok`/`text`/`session_id`/…）；落盘日志含完整 stdout
- stub：检测到 `stream-json` 时输出若干 NDJSON 行 + 末行 result，供单测覆盖流式路径

### 9.3 接线

```
Orchestrator(on_stream=factory)   # factory(role) -> callback|None
  role ∈ {"clarify","subject","gate"}
  chat / _phase_working 调用 factory(role) 得到 on_stream 传入 AgentRunner
  gate 相位不传（或 factory 对 gate 返回 None）

cli.cmd_go / cmd_chat（tty）：
  on_stream = stream_ui.factory_for_tty()
非 tty 的 `gdd run`：on_stream=None
Web 控制台：后台线程挂自己的滚动区回调（工具行全留，助手文本按环形缓冲保留）。
门禁会话仍然不流式，页面只写阶段行。
```

### 9.4 呈现约定

```
# P0 clarify
AI> （流式字符，结束后换行；cli 不再重复打印整段 reply）

# P1 subject
⋯ Write docs/plan.md
⋯ Bash: pytest -q
⋯ （截断文本，默认累计 ≤800 字后省略）
```

### 9.5 不变式

- 编排 / 指纹 / flock / killpg / 超时语义不变；流式只是呈现层
- Ctrl-C 仍杀进程组；半截流式输出可接受，进度已落盘
- 流式失败（解析中断）→ 既有会话失败链，不 invent 成功
