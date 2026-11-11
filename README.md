# GDD — Goal Driven Development Harness

节点编排 + entry/exit 双门禁 + 人工准入。
可自定义节点流水线的GDD harness。
MVP 执行端：**本地 Claude Code（headless）**。

## 核心设计

| 其他harness的通病               | GDD 的对策                                                                                        |
|----------------------------|------------------------------------------------------------------------------------------------|
| 节点完成判定权在 LLM（自报【节点完成】可被绕过） | **判定权归 harness**：机器检查由 harness 直接执行（不经过 LLM）；AI 验证由独立门禁会话判定，verdict 解析失败按 fail 处理（fail-closed） |
| 单会话消息循环，上下文膨胀              | **每类会话独立**：entry 门禁 / exit 门禁 / 节点主体各起独立会话，prompt 带 `[[GDD-ROLE:...]]` 机器可读标记                  |
| 所有环节都需人参与，颗粒度偏细、易疲劳        | 卡点只在几个关键节点；其余由双门禁自动把关；打回自动带反馈重试（上限可配）                                                          |

## 快速开始

```bash
cd /path/to/your/project
export PYTHONPATH=/path/to/GDD        # 或 pip install -e /path/to/GDD

# 用内置需求交付流水线初始化（七阶段+，8 节点，3 个人工确认点）
gdd init --name my-change --requirement requirement.md

gdd go           # 单命令交互：init→推进（主体流式）→审批→澄清（全文流式）→终态（推荐）
gdd run          # 推进：门禁→主体→门禁→(审批)→下一节点
# 已验证：全链路（主体产出→机器检查→AI 门禁验证→supplements 固化→git 交付）真实跑通
gdd status       # 查看节点进度
gdd approve --yes                        # 批准进入下一阶段
gdd approve --reject --feedback "..."    # 打回当前节点主体
gdd retry        # blocked 后人工重试：指回责任节点并清零计数，重跑其主体（门禁故障型仅重跑门禁）
gdd steer "…"    # working 期间另开终端补充信息（不占锁，下一次工具返回后送达）
gdd abort / gdd reset --yes
```

状态持久化在 `.gdd/state.json`（文件即状态机），可随时中断恢复；
每个会话（门禁/主体）的完整 prompt 与输出落在 `.gdd/logs/`。

### 图形控制台（推荐）

```bash
mkdir -p ~/gdd-workbench && cd ~/gdd-workbench
PYTHONPATH=/path/to/GDD python3 -m gdd.ui --root .   # 打开 http://127.0.0.1:8765
```

本地 Web 控制台：模板管理（内置 delivery + 自定义 YAML/JSON，fork 可编辑）、从模板启动任务、
任务列表、节点流程图（当前节点/待审批高亮/门禁重试/完成态）、审批与重试一键操作、blocked 原因置顶。
仅监听 127.0.0.1；详见 USER_GUIDE.md 第 10 章。

### Mac app（Tauri 2 壳，`src-tauri/`）

`GDD Console.app` 是控制台的 macOS 桌面入口：窗口直接用系统 **WKWebView** 加载
`http://127.0.0.1:8765/`，不重写界面、不打包第二份前端，也不内置 Chromium/Electron。
Web 页面更新只需更新外部 GDD 源码，**不需要重新构建 app**。

前置依赖（app 不打包这些）：

- macOS；
- 本机 **Python ≥3.10**（系统 `/usr/bin/python3` 为 3.9 时不可用）；
- 本机 **GDD 源码**（本仓库，配置项 `gddHome`）；
- 构建 app 需要 Rust 工具链与 Tauri CLI（运行 app 不需要）。

构建：

```bash
cargo install tauri-cli --version "^2" --locked
cargo tauri build --bundles app
# 产物：src-tauri/target/release/bundle/macos/GDD Console.app
```

首次启动自动生成配置（0600）：

`~/Library/Application Support/com.gdd.console/config.json`

```json
{
  "version": 1,
  "gddHome": "/abs/path/to/gdd-harness",
  "python": "/abs/path/to/python3.12",
  "workbenchRoot": "/Users/<you>/gdd-workbench",
  "path": ["/opt/homebrew/bin"],
  "env": {}
}
```

- `gddHome` 必须包含 `gdd/ui.py`；`python` 必须是 ≥3.10 的绝对路径；`workbenchRoot`
  默认 `~/gdd-workbench`（不存在时自动创建），其下每个子目录是一个任务。
- `path`/`env` 可选，用于让 GUI 启动的控制台找到 `claude`/`codex` 等 Agent CLI
  （如 `GDD_AGENT_BIN`）。配置中的 `env` 值不会写入日志。

行为与边界：

- 启动时只读探测 `GET /api/overview`：已有 GDD 控制台则直接复用（不产生第二个进程）；
  8765 无监听才用 `<python> -m gdd.ui --root <workbench> --port 8765 --no-open` 拉起。
- 只支持默认端口 8765；8765 被非 GDD 服务占用时报错，不退让到其他端口。
- 关闭窗口只隐藏，app 继续运行；点 Dock 重新打开会重新探测并显示窗口。
  退出 app（Cmd+Q）不会终止控制台；下次启动通过 8765 复用。
- 日志：`~/Library/Logs/com.gdd.console/app.log`、`console.log`。
- 停止控制台：`pkill -f "gdd.ui"` 或在活动监视器中结束进程（app 不提供停止按钮）。
- 更新 Web：更新 GDD 源码 → 停止旧控制台（确保 8765 无监听）→ 重新启动 app
  （app 无需重新构建/发布）。
- 不承诺在未安装 Python/GDD 的机器上独立运行；本迭代不含签名/公证/Mac App Store、
  自动更新、非默认端口发现。

开发者/测试钩子（不构成产品对外契约）：`GDD_CONSOLE_CONFIG_DIR`、`GDD_CONSOLE_LOG_DIR`、
`GDD_CONSOLE_TEST_HEADLESS`、`GDD_CONSOLE_TEST_NO_DIALOG`、`GDD_CONSOLE_TEST_PORT`、
`GDD_CONSOLE_TEST_EXIT_AFTER_READY`、`GDD_CONSOLE_TEST_DUMP_PAGE`、
`GDD_CONSOLE_TEST_CLOSE_AFTER_READY`。

## 自定义编排（pipeline JSON）

模板与内置模板完全同源（`gdd/templates/delivery.json` 可复制修改自用）。完整字段说明、校验规则与设计建议见 **`docs/covenant/template-format.md`**。决策定级和决策记录见 **`docs/covenant/decision-grade.md`**。验收用例、证据和对抗裁决见 **`docs/covenant/acceptance-cases.md`**。

```json
{
  "id": "my-pipeline",
  "nodes": [
    {
      "id": "n1",
      "name": "节点一",
      "subject":  { "goal": "产出 a.md", "instructions": "...", "max_turns": 80 },
      "entry": {
        "goal": "前置条件（基础卡控）的自然语言判据",
        "artifacts": [{"path": "x.md", "must_contain": "^# "}],
        "commands": ["test -f x.md"]
      },
      "exit": {
        "goal": "目标状态（成功判据）",
        "artifacts": [{"path": "a.md"}],
        "commands": ["python -m pytest tests/"],
        "ai_check": "逐条核实 DoD……",
        "change_aware": true
      },
      "approval":   { "required": true, "prompt": "请确认……" },
      "max_bounces": 3,
      "baseline_marker": true
    }
  ]
}
```

`gdd init --pipeline my-pipeline.json`

### 字段语义

- **entry**：前置条件。不通过 → **打回给前一个节点**（重跑其主体，反馈含原因与修改指引）；首节点 entry 不满足 → blocked。
- **exit**：目标状态（可验证产物/目标）。不通过 → **打回给当前节点的主体**。
- **max_bounces**：打回上限，默认 3；双计数器：`bounces`（exit/审批打回，计入本节点，节点完成时归档清零）、`entry_bounces`（下游 entry 打回上游，计入上游节点，下游 entry 通过时归档清零）；任一达上限 → blocked。
- **blocked / retry**：blocked 时记录责任节点 `blocked_owner`（entry 打回型指向上游节点）。`gdd retry` 把执行点指回责任节点并清零其计数：普通型重跑节点主体；门禁故障型（exit_retry/pending 相位）仅重跑门禁不重跑主体。
- **approval.required**：exit 通过后是否需人工准入才进入下一阶段（request_approval）；默认 false（exit 通过即进入下一阶段）。交互终端内联 y/n/a；非交互（CI）挂起，由 `gdd approve` 异步裁决。
- **clarify**：是否开放「要点澄清」交互（默认 false）。两类交互严格区分：**request_approval**（强门禁卡点，裁决性的，改变状态机、消耗打回预算，仅 approval.required 节点）vs **clarification**（信息对齐性的，不动状态机、不耗预算，防线靠 exit 门禁重验）。两个窗口可用：节点启动前预对齐（pending 相位，独立澄清会话）、审批挂起时对产出追问（resume 主体会话）；`gdd chat` / 控制台 💬 卡片对话，对话落盘 `.gdd/dialogs/<node>.jsonl`；结束澄清（`/done` 或结束按钮）后主体补产 + 全新 exit 门禁强制重验。设计详见 `docs/design-clarify-resume.md`。
- **主体会话复用**：打回重跑时复用上一轮主体会话（`claude -p --resume <sid>`，反馈以「继续修改」注入而非重发全量上下文）；门禁会话永不复用（独立判定防带偏）。打回 ≥2 次后改全新会话（防上下文膨胀）；**两个豁免**：turns 打满自动续跑正交于打回预算（`auto_continues > 0` 时仍续会话）、审批挂起澄清后的收尾轮强制续会话（review 澄清对话发生在主体会话上，新会话不可见）；主体成功后 `auto_continues` 清零（防一次已解决事件永久解除防污染门）。resume 失效自动降级全新重试。`GDD_SUBJECT_RESUME=off` 可关闭。跨节点复用可配置（`session_group`/`session_preset` 预设 independent/clarify-design/shared，内置 delivery 为 clarify-design：澄清设计链共享会话、实现测试独立——设计详见 `docs/design-session-group.md`）。
- **baseline_marker**：进入该节点主体工作时记录 git 基线 commit。
- **exit.change_aware**：exit 门禁注入 `git diff --stat baseline`（含未提交工作区改动）与 `git status`，**AI 门禁据此补充验证项并实际执行**（内置 pipeline 的 test 节点开启）。
- **补充验证项固化（模板/实例分离）**：全局模板只在 `gdd init` 时实例化为 `.gdd/pipeline.json`，绑定本次运行。change_aware 门禁基于本轮改动推导出的补充验证项通过 verdict 的 `supplements` 字段返回，harness **只增不减**地写回运行实例：可命令化项沉淀为机器检查 `commands`（下次重试不经 LLM）、产物检查追加/收紧 `artifacts.must_contain`、不可机器化的语义项追加到 `ai_check`；按内容去重防膨胀。全局模板不受影响。
- **git_delivery**（pipeline 级，默认启用）：每节点 exit 通过后，harness 自动把产出（.md/代码）提交并推送到开发分支。未写 `branch` 时默认 `gdd/delivery`；内置 delivery 在 `gdd init --name <名字>` 时使用 `gdd/<名字>`，可用 `--branch` 改，用 `--base` 指定从哪条已有分支拉出。`.gdd` 状态目录不入库；无 remote 仅本地提交；push 失败仅记录事件不阻塞流水线。commit 信息形如 `gdd(<node_id>): <节点名> exit 通过`。**停用场景**：项目目录位于另一个 git 仓库的子目录（外层仓存在）时，harness 会自动停用 git 交付（防外层仓劫持/误提交），在 `gdd status` 与控制台显式警示 `git_delivery_disabled`，产物仅在项目目录内、不入任何分支——此时需自行把产物同步到外层仓。

### 门禁两层判定（判定权在 harness）

1. **机器检查**：`artifacts`（glob + 必含正则）与 `commands`（bash -c）由 harness 直接执行，不经过任何 LLM——产物缺失/命令非零即 fail，不启动 AI 验证。
2. **AI 验证**（配置了 `goal`/`ai_check` 时）：独立门禁会话用只读工具实际核实，输出 `{"verdict":"pass|fail",...}`；解析失败按 fail（fail-closed）。门禁 prompt 内置纪律：不采信主体自评、证据必须来自工具输出、无法核实一律 fail。

## 内置 pipeline：delivery（需求交付，七阶段+）

`project_init → survey → clarify → plan → implement → test → delivery → release_checklist`

- 人工确认 3 节点：**clarify**（DoD）、**plan**（方案）、**test**（上线前测试结论）。
- 每个节点内置基础 entry（上游产物机器检查 + AI 门禁语义验证——注意：内置模板每个 entry 都带 goal，即每节点最多 3 个 AI 会话：entry 门禁 + 主体 + exit 门禁）与 exit（本节点产物 + AI 判据）。
- implement 为 baseline_marker 节点；test 的 exit 为 change_aware（AI 基于改动补充验证）。
- 各节点产物都在 `iterations/<名字>/`：`requirement.md`、`survey.md`、`goal.md`（DoD）、`spec.md`、`plan.md`（ADR）、`acceptance.feature`、`decisions.md`、`impl-ledger.md`（核销台账）、`test-report.md`、`verify.md`、`delivery.md`、`RELEASE-CHECKLIST.md`。不写项目的 `docs/`，也不写仓库根目录。已有迭代的 `spec.md` 和 `decisions.md` 在下次初始化时列为只读基线。同名目录已有文件则拒绝再初始化。

## Agent 执行端配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GDD_AGENT_BIN` | `claude` | 任一兼容 CLI（headless 读 stdin、输出 result JSON） |
| `GDD_AGENT_PRE_ARGS` | - | 插在 bin 之后、`-p` 之前的参数（shlex 切分） |
| `GDD_AGENT_MODEL` | - | 传给 `--model` |
| `GDD_AGENT_EXTRA_ARGS` | - | 附加参数（shlex） |
| `GDD_AGENT_TIMEOUT` | 7200 | 单会话超时秒数（默认 2 小时；重主体节点如 plan/implement 实跑可能超 900s，勿设太短） |
| `GDD_AGENT_MAX_TURNS_GATE` | 40 | 门禁会话 max-turns（实跑调优：15 不够复核大量 DoD 证据，40 一次通过；门禁复查型会话一般 30-50 足够） |
| `GDD_CHECK_TIMEOUT` | 600 | 机器检查命令超时秒数 |
| `GDD_SUBJECT_RESUME` | on | 打回重跑是否复用主体会话（`--resume`）；`off` 强制全新会话 |
| `GDD_SUBJECT_AUTO_CONTINUES_MAX` | 4 | 主体会话 turns 打满（error_max_turns）后自动 `--resume` 续跑的轮数上限（每轮继续配满 max_turns；超限转 gate_error → blocked 人工裁决） |
| `GDD_STEER` | on | working 期间补充信息。`off` 不挂 PostToolUse、会话结束也不补送 |
| `GDD_STEER_HOME` | `~/.gdd/steer` | 补充队列与 hook 配置目录（在项目仓库外） |

> 存量实例兼容：升级后 NodeSpec 新增字段（如 `clarify`）会改变流水线指纹，`run` 时自动识别 legacy 实例（剔除新增键后指纹匹配）并重写实例刷新指纹；其他任何指纹不一致仍 fail-closed blocked。

## 测试

```bash
python3 tests/test_e2e.py
# A 全流程（exit 首轮打回 + 4 人工批准）
# B 审批驳回 → 打回 → 批准 → 推进
# C 连续 fail → blocked → retry → 再次 blocked
# D entry 门禁打回上一节点并恢复完成
# E 门禁自身故障：不消耗 bounces，重试后恢复
# F 门禁连续故障 2 次 → blocked → retry 恢复
# G entry 打回达上限 → blocked → retry 指回责任节点重跑并完成
# H 每节点 exit 通过即提交推送到开发分支（git_delivery）
# I AI 补充验证项固化到运行实例（只增不减+去重+机器化）
# J/J2 会话篡改 .gdd 被检测恢复 / 落盘篡改被指纹拦截 blocked
# K 主体在他分支提交 → 交付丢失被检出（非静默 skip）
# G2 gdd go 单命令交互模式（七场景：完整循环/非tty退化/并发变更/Ctrl-C 中断/引导 init+澄清驳回/EOF 退出与 /done 与 review 对话/锁忙退避不退出；另有 7b 变体）
# L/L2 登录态失效转显式错误（含长正文不误报）/ turns 打满独立标记
# M/M2 主体 turns 打满自动 resume 续跑（不耗预算）/ 续跑达上限转 blocked → retry 恢复
# N 门禁默认 max-turns 40（环境变量可覆盖）
# T2 内置模板 test 节点集成验证判据（本机启动优先，降级链含测试环境机器）

python3 tests/test_ui.py     # 图形控制台（含 CSRF/穿越/并发写防护回归）
python3 tests/test_clarify.py # 主体会话 resume / 要点澄清通道 / legacy 指纹迁移
python3 tests/test_steer.py   # working 补充：hook 消费 / 不占锁 / 结束补送一次
python3 tests/test_node_ext.py # 节点多个 MCP/skill：只挂主体，门禁不继承

# Mac app（Tauri 2 壳）
cd src-tauri && cargo test          # 配置校验 / 探测 / 进程监管单元与集成测试
bash scripts/verify_app.sh          # D1/D14 静态检查（Tauri 配置、资源、ATS、文档）
python3.12 scripts/verify_headless.py  # D2/D3/D5/D6/D7/D9/D11/D12/D13 应用级隔离验证
python3.12 scripts/verify_headless.py --port 8765  # 8765 空闲时做真实端口验证
python3.12 scripts/verify_gui.py       # D4 真实窗口（URL + WKWebView DOM + 截图）
python3.12 scripts/verify_gui.py --check-error-dialogs  # D9/D11 错误对话框可见性（截图）
python3.12 scripts/verify_gui.py --check-8765-reuse     # 8765 有控制台时做只读复用验证
```

> 8765 被用户正在使用的控制台占用时，应用级脚本用测试钩子 `GDD_CONSOLE_TEST_PORT`
> 选择空闲端口做隔离验证；生产端口固定 8765 由 `supervisor::DEFAULT_PORT` 单元测试守护。
> `verify_gui.py` 在本机缺少「屏幕录制」权限时无法截屏，会以 WKWebView 实际 URL 与
> DOM 文本作为窗口内容证据。

stub agent（`tests/stub_agent.py` + `GDD_STUB_SCRIPT`）可无模型跑全链路。

## 代码结构

```
gdd/
  models.py        # Pipeline/Node/Gate/Approval/Subject 数据模型（零依赖 JSON）
  runner.py        # 本地 Agent 适配器（claude -p headless，会话级隔离）
  node_ext.py      # 节点 MCP/skill：生成仓库外配置，只挂主体会话
  steer.py         # working 补充队列与 PostToolUse hook（仓库外，不占 .gdd/lock）
  gates.py         # entry/exit 门禁：机器检查 + 独立 AI 验证 + verdict 解析（fail-closed）
  approval.py      # request_approval：交互确认 / 非交互挂起
  orchestrator.py  # 编排引擎：相位状态机、打回、审批、澄清、blocked/retry、状态持久化
  stream_ui.py     # tty 流式呈现（clarify 全文 / subject 工具+截断文本）
  templates/      # 内置 pipeline 模板（delivery.json——唯一权威源，可复制修改自用）
  cli.py           # gdd init/run/status/chat/approve/retry/abort/reset
  ui.py            # 本地图形控制台（127.0.0.1 HTTP + 内嵌页面）
src-tauri/         # macOS 桌面壳（Tauri 2 + 系统 WKWebView，加载 127.0.0.1:8765）
  src/config.rs    # 配置模型/校验/首启生成（0600）
  src/http_probe.rs# 只读 GET /api/overview 识别控制台
  src/console.rs   # argv 数组拉起 python -m gdd.ui（不经过 shell）
  src/supervisor.rs# 探测/复用/拉起/就绪轮询/超时回收
  src/app.rs       # Tauri 窗口、Dock 重开、关闭隐藏、原生错误对话框
  src/headless.rs  # 无 GUI 测试入口
scripts/           # verify_app.sh / verify_headless.py / verify_gui.py / window_list.swift
assets/            # app 图标源文件（icons 由 cargo tauri icon 生成）
```

## 边界（MVP 已知限制）

- 顺序流水线（无并行/分支 DAG）；打回只能回退到相邻节点。
- 主体会话上下文不跨节点共享，只通过产物文件传递（设计使然，非缺陷）。
- AI 门禁仍依赖 LLM 输出 verdict——但机器检查层不依赖；关键判据应尽量放进 artifacts/commands。
- **门禁 AI 会话带 Bash 工具**：prompt 约束「禁止修改文件」是软约束，理论上门禁可用 bash 写文件伪造产物再放行；机器检查层（must_contain 正则）可部分拦截，语义判据仍可能被自造证据欺骗。
- **防篡改（严格流程保证的最后一环）**：会话带文件写权限，但 harness 内存态是唯一权威——每个会话（主体/entry/exit 门禁）结束后 `_check_tamper` 比对磁盘与内存副本，被篡改的 state.json/pipeline.json 自动覆盖恢复并记事件（`state_tamper_restored` / `pipeline_tamper_restored`）；崩溃后落盘持久的篡改由 `pipeline_fingerprint`（canonical JSON sha256）在下次 run 启动时拦截 → blocked，要求人工核验。
- **supplements 命令固化链（已知攻击面）**：exit 门禁输出的 `command` 类补充项会被 harness 持久化并在后续重试中直接 bash 执行——不构成提权（门禁会话本就有 Bash，同样命令当场即可执行），且固化过程记录 `gate_supplement` 事件、受类型白名单/长度/去重约束；但 `.gdd/` 不入 git 无版本可回溯，审计依赖 history 事件与 `.gdd/logs/`。
- **state.json 的跨进程信任边界（文档化设计边界）**：pipeline.json（指纹）与 input.md（摘要）有跨重启完整性锚，**state.json 没有**——它既是被保护对象又是保护记录的载体，本地无签名密钥下无法自证。缓解链：① 会话正常结束与超时均 killpg 收编残留孙进程（nohup 延迟写攻击链在此断开）；② run() 收尾对三文件做最终 tamper 扫描（`run_final`）；③ 下一进程 run() 入口重校 pipeline 指纹与 input 摘要（伪造 state 想推进到 completed 必须同时过这两关）。**残留边界**：run() 返回之后落盘的伪造 state（外置定时写、非会话孙进程）在下次 run 仍会被采信——多用户/对抗环境请将 `.gdd/` 纳入外部管控（版本化/只读挂载），单人本地开发场景可接受。
- **指纹校验的两个固有边界**：① `_apply_supplements` 中 pipeline 落盘与 state 指纹落盘之间存在极小崩溃窗口，下次 run 会误报指纹不匹配（fail-closed 方向，人工 reset 即解）；② 本地无签名密钥，攻击者若**同时**篡改 pipeline.json 与 state.json 的指纹字段可绕过校验——对本地单人开发场景可接受，多用户环境需将 .gdd 纳入外部管控。
- **门禁故障与判定分流**：Agent 会话崩溃（门禁或主体）不消耗节点打回预算（有限重试，连续 2 次转 blocked）；仅明确的 fail 判定才打回主体。
- **并发防护**：run/approve/retry/abort 通过 `.gdd/lock`（flock）互斥，重复启动会拒绝而非覆盖状态。
- **push 失败不静默**：推送到远端失败记入 state（`push_failed`），status 输出警示，下次 run 自动补推；远程分叉时需人工介入同步。
- **他分支提交检出**：主体会话若在其他分支（如 `git checkout -b feature` + commit）提交产出，harness 交付前比对 `rev-list <delivery>..HEAD` 检出滞留提交，记 `git_commit_fail` 并在 state 留交付警示（指导 cherry-pick/merge），绝不静默丢交付；切回交付分支失败同样显式失败。
- **交付分支引用保证**：init 时空仓库以 `--allow-empty` 建基线提交，交付分支永非 unborn（否则 rev-list/checkout 失效）；暂存判定用 `git diff --cached`（`status --porcelain` 会把未跟踪的 `.gdd/` 误判为有暂存）。
- **change_aware diff 语义**：对工作区 diff（含未提交改动），因为当前节点提交发生在 exit 通过之后。
- **日志序号独立递增**（`log_seq`），不受 history 裁剪影响；`.gdd/lock` 文件不入库。
- **init 时固化基线**：init 前已存在的未提交内容提交为 `gdd(init): baseline`，避免被首个节点 exit 提交扫入。
- 状态文件原子写入（tmp + rename），进程中断不会损坏 state.json。
- 审批有三种形态：CLI 交互 / CLI 异步（gdd approve）/ Web 控制台一键批准驳回（`gdd.ui`，仅 127.0.0.1）。
