# GDD 用户使用手册

> Goal Driven Development harness —— 判定权在 harness，不在 LLM。
> 从一句需求，到可验证产物、git 交付、上线准备。

---

## 1. GDD 是什么

GDD 是一个**本地节点流水线编排引擎**：你定义节点（做什么、成功判据是什么），harness 驱动本地 Claude Code 会话干活，并用独立门禁验证每个节点是否真正完成。

它解决的核心问题：**LLM 自报"已完成"不可信**。GDD 中：

- 节点**主体**会话只负责干活，无权宣布完成
- **entry/exit 门禁**独立于主体（机器检查不经 LLM + 独立 AI 验证会话，fail-closed）
- 流转、打回、审批、git 交付全部由 harness 代码决定
- 会话篡改 `.gdd/` 状态会被内存权威副本自动恢复

内置一条 8 节点需求交付流水线（ 七阶段+）：一句需求 → 文档 → 实现 → 测试 → 上线检查单。

---

## 2. 安装与环境

**依赖**：Python 3.10+、git、本地 Claude Code CLI（或兼容代理）。

**无第三方 Python 依赖**；YAML 模板可选装 PyYAML（`pip3 install pyyaml`）。


**环境变量**：

| 变量 | 默认 | 说明 |
|---|---|---|
| `GDD_AGENT_BIN` | `claude` | Agent 可执行文件 |
| `GDD_AGENT_PRE_ARGS` | - | 插在 bin 后、`-p` 前的参数（shlex 切分） |
| `GDD_AGENT_MODEL` | - | 传给 `--model` |
| `GDD_AGENT_EXTRA_ARGS` | - | 附加参数（shlex 切分） |
| `GDD_AGENT_TIMEOUT` | 7200 | 单会话超时秒（默认 2h；plan/implement 重节点勿设短） |
| `GDD_SUBJECT_RESUME` | on | 打回重跑/组内后续节点是否复用主体会话（`--resume`）；`off` 强制全新会话（全局逃生舱） |
| `GDD_AGENT_MAX_TURNS_GATE` | 40 | 门禁会话 max-turns（实跑调优：15 不够复核大量 DoD 证据，40 一次通过） |
| `GDD_CHECK_TIMEOUT` | 600 | 机器检查命令超时秒 |
| `GDD_SUBJECT_AUTO_CONTINUES_MAX` | 4 | 主体会话 turns 打满（error_max_turns）后自动 `--resume` 续跑轮数上限；打满即自动续跑接续干活（产物已落盘部分零丢失），超限才 blocked |
| `GDD_VERBOSE` | - | set 后 status 打印完整 history |

### 会话复用配置（session_group，详见 docs/design-session-group.md）

节点级 `session_group: 组名`（仅字母数字_）：同组节点共享主体会话（对话记忆连续，适合澄清→方案这类同一思考流）；缺省独立（验证者角色判断独立）。流水线级预设 `session_preset:`：

| 预设 | 语义 | 适用 |
|---|---|---|
| `independent`（缺省） | 全部独立 | 大型项目 |
| `clarify-design` | 相邻 clarify 节点归 `gdd_design` 组，其余独立 | 中等常规项目（内置 delivery 即此配置） |
| `shared` | 全部归 `gdd_all` 组 | 微型改动 |

节点级显式组优先于预设；bounces 预算仍按节点计；存量任务不自动获得组复用（需 reset 重新实例化）。

---

## 3. 五分钟上手（内置交付流水线）

> 不想敲命令？直接用图形控制台（见第 10 章）。

```bash
# 1. 写需求（一句话也行）
echo "实现一个 TODO CLI，支持增删查，带 pytest 测试" > req.md

# 2. 初始化（delivery = 内置 8 节点流水线）
python3 -m gdd.cli init --root ./myproj --pipeline delivery --requirement req.md

# 3. 开跑（会自动推进，停在人工审批点）
python3 -m gdd.cli run --root ./myproj

# 4. 审批点出现后查看摘要、批准（approve 会自动续跑）
python3 -m gdd.cli status --root ./myproj
python3 -m gdd.cli approve --root ./myproj --yes

# 5. 重复 run/approve 直到 "✅ done，可以上线"
```

全程你只需在 3 个审批点（需求澄清 / 方案 / 测试）按 `--yes` 或 `--reject --feedback "修改意见"`。

**每节点 exit 通过后**：harness 自动把产出 commit 到 `gdd/delivery` 分支（配置了 remote 则 push）。想看交付历史：

```bash
git -C ./myproj log --oneline gdd/delivery
```

**例外**：若项目目录本身是另一个 git 仓库的子目录（外层仓存在），为防外层仓劫持/误提交，git 交付会被自动停用——`gdd status` 与控制台会警示 `git_delivery_disabled`，产物只留在项目目录内、不入任何分支，需自行同步到外层仓。

---

## 4. 自定义流水线（JSON 或 YAML）

流水线是节点列表，每节点 = 主体 + 双门禁 + 可选审批：

```yaml
# pipeline.yaml
id: my_flow
name: 我的流程
git_delivery:
  enabled: true
  branch: gdd/delivery     # 每节点 exit 通过即提交推送
  # remote: origin          # 省略则自动探测；无 remote 仅本地提交
nodes:
  - id: write
    name: 写文档
    subject:
      goal: 产出 docs/note.md，内容包含背景与结论
      max_turns: 30
    entry: {}                # 首节点通常留空
    exit:
      artifacts:
        - path: docs/note.md
          must_contain: "## 结论"   # 正则；path 为 glob 多文件时为 ALL 语义（每个匹配文件都必须命中，垃圾文件只会收紧检查不会绕过）；非常规文件（FIFO 等）拒绝读取、灾难性回溯正则按超时失败（GDD_REGEX_TIMEOUT，默认 10s）
      commands:              # 命令返回 0 即过
        - "grep -q 背景 docs/note.md"
      goal: 文档结构完整        # AI 门禁的语义判据
    approval:                 # 省略 = exit 过即走
      required: true
      prompt: 请确认文档内容

  - id: review
    name: 审阅
    entry:
      artifacts:
        - path: docs/note.md   # 前置条件：上游产物存在
    exit:
      ai_check: 文档结论有证据支撑
```

```bash
python3 -m gdd.cli init --root ./proj --pipeline pipeline.yaml --requirement req.md
```

**JSON 同构**；字段速查：

| 字段 | 位置 | 作用 |
|---|---|---|
| `subject.goal / instructions / max_turns` | 节点 | 主体会话的任务描述 |
| `entry.artifacts / commands` | 节点 | 前置条件（机器检查） |
| `exit.artifacts / commands / goal / ai_check` | 节点 | 成功判据；`change_aware: true` 注入 git 改动供 AI 补充验证 |
| `baseline_marker: true` | 节点 | 进入该节点时记录 git 基线（change_aware 的 diff 基准） |
| `approval.required / prompt` | 节点 | 人工准入 |
| `max_bounces` | 节点 | 打回上限，默认 3 |

---

## 5. 打回与人工介入

**打回语义**（判定失败时自动发生）：

| 失败类型 | 去向 | 计数 |
|---|---|---|
| entry 不通过 | 前一节点主体重跑 | `entry_bounces`（记前节点） |
| exit 不通过 / 审批驳回 | 本节点主体重跑 | `bounces`（记本节点） |
| 会话崩溃/超时（非判定） | 仅重试会话 | 不消耗打回预算；连续 2 次 → blocked |

计数达 `max_bounces`（默认 3）→ **blocked**，带明确原因等待人工。

**blocked 后**：

```bash
python3 -m gdd.cli status --root ./proj        # 看 blocked_reason（含责任节点）
# …修复问题（改代码/装依赖/人工产出缺失文件）…
python3 -m gdd.cli retry --root ./proj         # 重跑责任节点并续跑
python3 -m gdd.cli abort --root ./proj         # 或放弃
```

**驳回审批**（打回主体附你的反馈，主体会话 resume 续改，不重发全量上下文）：

```bash
python3 -m gdd.cli approve --root ./proj --reject --feedback "DoD 缺回滚场景，补充"
```

**要点澄清**（与审批不同：对齐信息、不裁决、不耗打回预算）：

节点声明 `"clarify": true`（内置 delivery 的 project_init/survey/clarify/plan/implement 均已开启）后，两个窗口可对话：

- **启动前预对齐**：节点还没开始跑时，先问清需求口径（独立澄清会话，多轮累积）；
- **审批挂起时追问**：exit 通过等你批准时，对产出追问（resume 主体会话，它记得自己写了什么）。

```bash
python3 -m gdd.cli chat --root ./proj --node plan
输入消息回车发送；/done 结束澄清（审批挂起时触发重验）；/q 直接退出
```

对话全量落盘 `.gdd/dialogs/<node>.jsonl`（审计可查）。`/done` 后：预对齐窗口仅记事件；审批窗口会**强制重验**——主体会话补产 → 全新 exit 门禁重新验证 → 重新挂审批，全程不消耗打回预算。防线不在预算而在重验：聊完后产出必须重新过门禁才算数。

**working 期间补充**（与澄清不同：不对话、不改状态，只在主体已经开工时纠偏）：

`gdd run` / `gdd go` 占着锁，这个终端不能再输入。另开一个终端：

```bash
gdd steer "不要引入 Redis，状态写本地文件"
```

补充写在仓库外的队列里。Claude Code 主体会话挂着 PostToolUse：下一次工具返回后，模型能看到这段话。若本轮不再调用工具，会话结束时用 `--resume` 补送一次。不消耗打回预算，也不注入门禁会话。细节见 `docs/design-steer.md`。

**重来**：`gdd reset` 删除 `.gdd/`（重新实例化模板；已产出的工作区文件保留）。

---

## 6. 审批点看什么

`status` 会打印审批摘要（节点目标 + exit 门禁证据 + 打回次数）。**门禁证据是独立会话用工具实际核实的输出**，不是主体自报。建议关注：

1. 证据是否引用了具体命令/文件/行号（裸结论=可疑）
2. 有无"未验证项"如实标注
3. 与你对节点的预期判据是否一致

驳回时反馈越具体，主体重跑越有方向。

---

## 7. 状态与审计

```
proj/
├── .gdd/
│   ├── pipeline.json     # 运行实例（init 时从模板复制；门禁补充项会固化到这里）
│   ├── state.json        # 状态机（phase/bounces/history/指纹）
│   ├── input.md          # 原始需求
│   ├── logs/             # 每个会话的完整 prompt+输出（序号-节点-类型.json）
│   └── lock              # 并发锁
└── （节点产物……）
```

```bash
python3 -m gdd.cli status --root ./proj           # 相位图：pending/▶working/↻门禁重试/⏸待审批/✓done
GDD_VERBOSE=1 python3 -m gdd.cli status --root ./proj   # 完整事件史
```

**supplements 固化**：exit 门禁基于改动推导的补充验证项会只增不减写入 `pipeline.json`（机器化命令下次重试直接执行、不再经 LLM）。全局模板不受影响。

**防篡改**：会话若篡改 `.gdd/`，harness 以内存权威副本恢复并记 `*_tamper_restored` 事件；崩溃后持久的篡改由实例指纹拦截（blocked）。

---

## 8. 常见问题

**Q: run 报"另一个 gdd 进程正在运行"？**
上一个 run 还活着（或崩溃残留锁）。`ps` 确认后 `rm .gdd/lock`；正常情况优先用 `gdd reset`（会先取锁再清理，避免与存活进程交叉）。

**Q: state.json 损坏（非法 JSON）任务救不回来？**
UI 中该任务会以「损坏」状态列出，点入可一键「重置任务」自救；CLI 侧 `gdd reset` 同效（reset 不读 state，损坏态也可执行）。

**Q: UI 后台运行到审批点会卡住吗？**
不会。后台线程禁用终端交互审批，任务统一挂起为待审批状态，由前端审批卡处置；仅 CLI 主线程 + tty 才走终端交互。

**Q: 主体会话超时？**
默认 2h；重节点可 `GDD_AGENT_TIMEOUT=10800` 再试。超时走故障域，不消耗打回预算。

**Q: 升级后老任务为什么不能 chat（澄清）？**
版本升级自动迁移只恢复指纹兼容，新增字段取默认值（clarify=false）——在途任务不自动获得澄清能力，需要时 `gdd reset` 以新模板重新实例化。

**Q: push 失败了流水线会停吗？**
不会。失败记入 `push_failed`，`status` 显示警示，下次 `run` 自动补推。远程分叉需人工 `git pull --rebase` 后再跑。

**Q: 主体把代码提交到了别的分支？**
harness 会检出滞留提交并显式报 `git_commit_fail`（非静默丢失），按提示 cherry-pick/merge 后 retry。

**Q: 想重跑某个节点？**
当前仅支持 blocked 责任节点重跑（retry）与 reset 全重来。

---

## 9. 已知边界

- 顺序流水线（无 DAG 并行）
- 门禁会话带 Bash（软约束），语义判据可能被自造证据欺骗——关键判据尽量写成机器检查（artifacts/commands）
- 本地单人场景：无签名密钥，同时篡改 pipeline.json 与 state.json 指纹可绕过校验
- 真实远端 push/分叉恢复未实测（本地无 remote 环境为正确 push_skip）

详见 README.md「设计边界」章节；自定义流水线示例可参考内置模板 `gdd/templates/delivery.json`——复制为副本（如 `my-delivery.json`）后修改，`gdd init --pipeline my-delivery.json` 即可使用。内置模板文件本身是唯一权威源（`--pipeline delivery` 与直接传该文件路径等价）。

---

## 10. 图形控制台（`python3 -m gdd.ui`）

不想记命令，用本地 Web 控制台：模板管理、任务列表、流程图、审批高亮与一键操作，全部图形化。

### 10.1 启动

**CLI 单命令（推荐）**：`gdd go` 在同一 terminal 内完成 init 引导→推进→审批（y/f/回车）→澄清对话（/done）→blocked 处置；Ctrl-C 中断后再次 `go` 原地续跑；非 tty 自动退化为 `gdd run`。tty 下澄清对话 **全文流式**、主体推进期 **工具摘要+截断文本流式**（门禁保持安静）；设计见 docs/design-go.md §9。

**图形控制台**：

```bash
# 工作台目录：其下每个子目录是一个任务（含 .gdd/）
mkdir -p ~/gdd-workbench && cd ~/gdd-workbench
PYTHONPATH=/path/to/GDD python3 -m gdd.ui --root .          # 默认 http://127.0.0.1:8765
```

常用参数：`--port 9000` 换端口；`--no-open` 不自动打开浏览器。

Agent 环境变量（`GDD_AGENT_BIN` / `GDD_AGENT_PRE_ARGS` 等）在启动 UI 前设置即可，任务在后台线程沿用同一套环境。

### 10.2 三块能力

| 能力 | 说明 |
|---|---|
| 模板管理 | 内置 `delivery` 只读可复制（fork）为新模板；自定义模板新建/编辑（YAML/JSON，保存即校验）/删除 |
| 新建任务 | 选模板 + 一句话需求 → 创建并初始化；任务页点「启动」即跑 |
| 任务监控 | 侧边栏任务列表实时刷新状态（运行中/待审批/阻塞/完成）；任务页展示节点流程图——当前节点蓝色脉冲、**待审批节点琥珀色高亮 +「需人工」旗标**、门禁重试橙色、完成绿色；blocked 原因与 push 失败置顶警示 |

### 10.3 操作

- **运行进度**：任务页有一块滚动区。入口/出口门禁只写阶段行；主体会话追加工具一行摘要和助手文本；澄清对话按字追加。人在底部时跟着滚，往上翻时不被拽回去
- **working 期间补充**：当前节点处于进行中时，滚动区下方可以写给模型。与 `gdd steer` 同一条队列：不占锁、不改相位、不消耗打回。主体还在跑时，下一次工具返回后送达；本轮若不再调用工具，会话结束补送一次。出口门禁期间改为下一次主体会话送达
- **待审批**：页面直接「✓ 批准并继续」/「✗ 驳回打回」（可填修改指引），操作后自动续跑
- **要点澄清**（clarify 节点）：蓝色 💬 卡片直接对话（启动前预对齐 / 审批挂起时追问），「结束澄清」触发强制重验；对话不改状态机、不耗打回预算
- **阻塞**：「↻ 重试责任节点」
- **随时**：「▶ 启动/继续推进」「中止」「重置（删 .gdd）」
- 页面 2 秒轮询自动刷新，无需手动刷新

### 10.4 安全

仅监听 `127.0.0.1`（不对外）；任务名/模板名白名单校验，杜绝路径穿越；内置模板只读不可删。
