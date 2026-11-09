# GDD 编排模板格式（templates/*.json）

模板是 GDD 流水线的**唯一配置形态**：JSON 文件、零依赖（不强制 PyYAML；YAML 可选支持）。
内置模板与用户自定义模板完全同源——内置 `delivery` 只是随包分发的一个普通文件。

```
gdd/templates/delivery.json      # 内置模板（唯一权威源；可复制修改自用）
my-delivery.json                  # 用户副本：cp gdd/templates/delivery.json 后自由修改
```

使用：

```bash
gdd init --pipeline delivery              # 内置名（≡ 直接传文件路径）
gdd init --pipeline my-delivery.json      # 自定义副本
gdd init --pipeline my-delivery.yaml      # YAML 亦可（需 pip3 install pyyaml）
```

**实例化语义**：`init` 时模板内容写入任务 `.gdd/pipeline.json` 并记录指纹（canonical JSON 的 sha256 前 16 位）。之后修改/删除模板文件**不影响**已实例化任务；任务期望的 pipeline 与磁盘不一致时 fail-closed 拒绝运行。

---

## 1. 顶层字段

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `id` | string | 否 | `"custom"` | 流水线标识 |
| `name` | string | 否 | `""` | 展示名 |
| `description` | string | 否 | `""` | 描述（给读者/AI 的上下文） |
| `nodes` | array | **是** | — | 节点列表（至少 1 个，**顺序即执行顺序**） |
| `session_preset` | string | 否 | `"independent"` | 会话复用预设（§4.2，三选一） |
| `git_delivery` | object | 否 | 见 §5 | exit 通过后的自动 git 提交推送配置 |

校验规则（`PipelineSpec.from_dict`，违反抛 `ValueError`）：
- `nodes` 为空 → 拒绝
- 节点 `id` 非法（仅允许 `字母数字_-`）或重复 → 拒绝
- `session_preset` 非三值之一 → 拒绝
- `session_group` 非法（仅 `字母数字_`；`gdd_` 前缀为预设展开保留，手写即拒）→ 拒绝

## 2. 节点字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | string | **必填** | 节点标识（`字母数字_-`，唯一） |
| `name` | string | `= id` | 展示名 |
| `description` | string | `""` | 给 AI 主体会话的节点上下文 |
| `subject` | object | — | **主体**：节点真正的工作指令（§3.1） |
| `entry` | object | 空 | 前置门禁：进入主体工作前的机器检查（§3.2） |
| `exit` | object | 空 | 出口门禁：成功的机器判据 + AI 判据（§3.3） |
| `approval` | object | 空 | 人工准入：exit 通过后是否等人（§3.4） |
| `clarify` | bool | `false` | 允许要点澄清：pending 或审批挂起时的人工对话窗口（§4.1） |
| `session_group` | string | `""` | 主体会话复用组（§4.2；空 = 独立） |
| `max_bounces` | int | `3` | 打回上限：exit/审批打回计入本节点，entry 打回计入**前**节点 |
| `baseline_marker` | bool | `false` | 进入主体工作时记录 git 基线，供 `change_aware` exit 使用 |
| `mcp` | list | 空 | 仅主体会话追加的 MCP（多个）。见 §3.7 |
| `skills` | list | 空 | 仅主体会话追加的 skill / 插件目录（多个）。见 §3.7 |

节点推进语义（状态机）：`pending →（entry 门禁）→ working（主体）→（exit 门禁）→（可选审批挂起）→ done → 下一节点`。exit 门禁 fail 打回**主体**重做（不重跑 exit 门禁本身、不耗 gate_error/exit_retry 故障预算，但**计入 bounces**）；连续 fail 达 `max_bounces` → `blocked`（人工 `gdd retry` 清零重试或 `gdd abort`）。`exit_retry` 是门禁自身故障（如执行命令异常）的内部重试态，不在正常推进链路上。

## 3. 各配置块详解

### 3.1 subject（主体）

| 字段 | 默认 | 说明 |
|---|---|---|
| `goal` | `""` | 该节点要完成什么（写给执行 Agent 的任务目标） |
| `instructions` | `""` | 补充约束/方法（如"禁止自行假设"） |
| `max_turns` | `80` | 主体会话轮次预算 |

主体由 headless Agent（默认 `claude -p`）执行，产物写文件、报告写 `.gdd/logs/`。同组会话可跨节点 resume（§4.2）。

### 3.2 entry（前置门禁）

进入主体前由 harness 执行机器检查，fail 则打回**上一节点**（`entry_bounces`，计入前节点，达上限则整条流水线 `blocked`；下游 entry 通过时上游 entry_bounces 归档清零）。

| 字段 | 说明 |
|---|---|
| `goal` | 判据描述（给 AI 门禁与文档） |
| `artifacts` | 产物存在检查列表：`[{path, must_contain}]`，path 支持 glob（如 `"docs/*.md"`）；`must_contain` 为正则（re.search 语义） |
| `commands` | harness 直接执行（`bash -c`）的命令列表，非 LLM 自报 |

### 3.3 exit（出口门禁）

主体工作完成后的成功判据。机器检查（artifacts/commands）先跑；`ai_check` 由**独立 AI 验证会话**执行（不共享主体上下文，防自证）。审批挂起时对产物做指纹快照（glob 与门禁同语义展开），approve 前核验，变更则拒绝放行。

| 字段 | 说明 |
|---|---|
| `goal` | 目标状态描述 |
| `artifacts` | 同 entry；额外支持变更拦截语义（快照核验） |
| `commands` | 同 entry |
| `ai_check` | 机器检查之外的 AI 验证指令（可选） |
| `change_aware` | bool（默认 false）：注入基于 git 改动（自 `baseline_marker` 节点起）的补充验证项并实际执行 |

已知取舍（R5 起语义更新）：artifacts 声明目录（如 `"docs/"`）**不算产物存在**——门禁、快照、交付三层一致只认常规文件（门禁报「产物为目录非文件」）。产物必须声明到具体文件或能展开为文件的 glob。

### 3.4 approval（人工准入）

| 字段 | 默认 | 说明 |
|---|---|---|
| `required` | `false` | exit 通过后是否挂起等待人工 |
| `prompt` | `""` | 展示给确认人的要点 |

挂起时交互终端直接 y/n/a（approval.py 交互协议：y 批准 / n 打回并反馈 / a 中止；驳回需附反馈文本，反馈注入上游重做）；非交互（UI/后台）走 `gdd approve --yes|--reject --feedback`。审批通过后：继续推进；若末节点则完成流水线。

### 3.5 完成周知（内置行为，非模板字段）

流水线**末节点**审批通过后，harness 在 stdout 打印「done，可以上线」与各节点 exit 摘要（记 `notify_print`）。不接外部 IM；模板里的 `notify` 节点仅负责产出周知文案（如 `docs/notify.md`）。

### 3.6 git_delivery（流水线级）

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 每节点 exit 通过后自动 commit 并推送（harness 执行，非 LLM） |
| `branch` | `"gdd/delivery"` | 开发交付分支 |
| `remote` | `""` | 空 = 自动探测首个 remote；无 remote 则仅本地提交 |

### 3.7 mcp / skills（仅主体会话）

节点可以给**主体会话**追加多个 MCP 和 skill。entry/exit 门禁和 `gdd chat` 不继承，避免门禁和澄清会话用上主体的外部工具。

```yaml
mcp:
  - name: fs
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
    env:
      TOKEN: "..."          # 值里不要换行
  - name: remote
    url: https://example.com/mcp
    headers:
      Authorization: "Bearer ..."
  - mcp/extra.json          # 文件内容是 {"mcpServers": { ... }}，相对项目根
skills:
  - skills/review           # 目录里要有 SKILL.md
  - path: skills/test
    name: tester            # 可选；缺省用目录名
  - /abs/path/to/plugin     # 含 .claude-plugin/plugin.json 时按插件目录原样加载
```

- 相对路径的每一段都不能是 `..`。解析符号链接之后也必须留在项目根里。绝对路径可以，用来指向仓库外的共用 skill。
- 名字字母或数字开头，只含字母数字、`_`、`-`。同一节点里 MCP 名字不能重复。mcp 文件里的 server 走同一套规则；`url` 必须带 `type`（`http` 或 `sse`），缺了这一轮主体会话失败。
- 主体进程启动时，gdd 在仓库外生成一份仅所有者可读的 `mcp.json`，并给 Claude 追加 `--mcp-config`。多个 skill 目录打进一个临时插件，追加 `--plugin-dir`。这是**追加**：用户自己已经配的 MCP / skill 仍然在。节点删掉声明后，仓库外的旧配置会清掉。
- 目录不存在或 skill 缺少 `SKILL.md` 时，这一轮主体会话失败，不静默跳过，也不把已保存的主体会话当成失效。
- `skills[].name` 会写进暂存副本的 `SKILL.md`，Claude 按这个名字调用。
- Codex 执行端不使用这两条 Claude 参数。
- 没声明 mcp/skills 的节点不写这两个键，已有流水线的指纹不变。

## 4. 两个横切配置

### 4.1 clarify（要点澄清）

`clarify: true` 的节点在**pending**（主体启动前）或**审批挂起**（review 模式）时开启对话窗口：
- 人工通过 `gdd chat --node <id>`（或 UI）与该节点的澄清会话对话，`/done` 结束
- 两个窗口语义不同：**review 窗口** `/done` 触发 exit 重验（纪要注入反馈回主体）；**pending 窗口** `/done` 仅落事件，主体照常启动时携带对话上下文
- 不消耗打回预算；但**结论必须固化到 artifact** 才影响后续门禁（对话本身不改变判据）
- 审批挂起期间对话若改动了产物，approve 会被快照核验拒绝（引导先 clarify_done 重验或驳回）

### 4.2 会话复用（session_group / session_preset）

主体会话（subject session）是否跨节点复用。**预设**（流水线级，解析时展开为逐节点 `session_group`，单形态指纹）：

| preset | 语义 | 适用 |
|---|---|---|
| `independent` | 全部独立（缺省） | 大型项目：每节点独立会话完整摸底，成本略高 |
| `clarify-design` | **相邻** clarify 节点段归 `gdd_design` 组（分段各 `gdd_design2/3…`，非相邻不合并），其余独立 | 中型常规（内置 delivery 默认）：需求澄清→方案设计同一思考流 |
| `shared` | 全部归 `gdd_all` 组 | 微型改动：一个会话全程 |

**节点级覆盖**：显式 `session_group: "my_group"` 优先于预设（不被覆盖）；空 = 独立。自定义组名仅 `字母数字_`（不含 `-`）；`gdd_` 前缀为预设展开专用（`gdd_design`/`gdd_all` 等），手写即 init 拒绝；组名也不得与任何节点 id 撞名（独立节点的组名即其 node.id，撞名会静默合并）。

复用语义：组内取流水线顺序最晚节点的存档 sid resume；有打回反馈时带反馈续跑；跨节点继承时注入完整任务书；entry 打回清下游全部 sid（主体+澄清）。预算（`bounces`）保持节点级。可用环境变量 `GDD_SUBJECT_RESUME=off` 全局关闭。

## 5. 最小可用模板（可直接复制）

```json
{
  "id": "minimal",
  "name": "最小流水线",
  "nodes": [
    {
      "id": "build",
      "name": "构建产物",
      "subject": {"goal": "生成 docs/out.md，内容包含结论章节"},
      "exit": {"artifacts": [{"path": "docs/out.md", "must_contain": "## 结论"}]}
    }
  ]
}
```

（可选配置全部缺省：无 entry/审批/git 推送/会话复用/澄清。）

## 6. 设计建议（写模板时）

1. **判据尽量机器化**：能用 artifacts/commands 表达的不要写成 `ai_check`——AI 门禁带 Bash 属软约束，语义判据可能被自造证据欺骗
2. **澄清问题前置**：歧义多的需求把早期节点标 `clarify: true` 并配 `clarify-design` 预设，把「问清楚」和「做方案」放进同一思考流
3. **验证者独立**：implement/test 等验证角色节点不要与上游共享 session_group（独立会话判断才可信）
4. **打回预算按节点调**：复杂主体节点可上调 `max_bounces`；机器判据弱的节点不要上调（防 AI 反复空转烧预算）
5. **改模板的正确姿势**：复制为新文件再改——直接改内置文件等于改了所有未来任务的起点，且不影响已实例化任务
