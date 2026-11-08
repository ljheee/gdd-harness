# 主体会话复用（session_group）设计

## 1. 背景与动机

每节点独立主体会话的默认策略源于**判断独立性**（验证者角色不继承生产者上下文），
但它同时丢掉了真实的连续性收益：同一思考流的上游节点（澄清→方案）频繁需要
回指对话中形成的否决项、权衡过程与术语约定——这些只能靠纪要文档转述，会失真。

token 成本不是复用的理由（独立会话重发的是一次性固定物料；resume 后历史轮次
全量滞留上下文，长对话反而更贵）。复用的唯一正当动机是**对话记忆连续性**。

## 2. 配置模型（两级）

### 2.1 节点级：`session_group`（显式）

```yaml
nodes:
  - id: clarify
    session_group: design      # 同组 → 主体会话 sid 互通复用
  - id: plan
    session_group: design
  - id: dev                    # 缺省 → 组名 = node.id（独立，即现状）
```

- 组名合法字符：字母数字 `_`（不含 `-`，比节点 id 更严；`gdd_` 前缀为预设展开专用，手写即拒绝；组名不得与节点 id 撞名）。
- 同组语义：后进入的节点 resume 组内最新主体会话；会话失败失效时全组降级
  全新会话（干净失败，不半复用）。
- **缺省 = node.id**：未配置 session_group 的节点永远独立——与现状完全兼容，
  存量模板/自定义模板零影响。

### 2.2 流水线级：`session_preset`（预设）

| 预设 | 语义 | 适用 |
|---|---|---|
| `independent` | 全部独立（等价不写） | 大型项目：验证者独立性最大化 |
| `clarify-design` | 相邻的 clarify 标注节点归入 `gdd_design` 组，其余独立 | **中等常规项目（默认推荐）** |
| `shared` | 全部节点归入 `gdd_all` 组 | 微型改动：一个会话跑完全程 |

规则：**节点级 session_group 显式声明优先于预设**（局部覆盖全局）。
preset 在模板解析时**展开为逐节点 session_group**（from_dict 展开后 to_dict
输出完整字段），指纹始终基于展开后的实例——模板写 preset，磁盘存展开值，
两者指纹一致，不引入双形态。

`clarify-design` 展开规则：相邻节点段（连续的 clarify=True 节点）归一组，
分段各自成组（组名 `gdd_design`、`gdd_design2`…递增），
非相邻的 clarify 段不合并——避免远距离合并放大污染面。

### 2.3 校验规则

- session_preset 非法值 → ValueError（fail-closed，模板加载时）。
- session_group 自定义组名含非法字符（非字母数字 `_`）、`gdd_` 前缀、或与节点 id 撞名 → ValueError（fail-closed）。
- preset=shared/independent 与节点级显式组共存 → 合法（覆盖语义）。

## 3. 编排实现（orchestrator）

会话存取从节点键位改为**组查找**（不改 state 存储格式，缺省组=node.id 时
行为与现状逐位一致）：

```
_node_group(node) -> str           # 节点 → 组名
_subject_group_sid(group) -> str   # 扫描所有 node_states[*].sessions.subject，
                                   # 取组内节点 sid（同组多 sid：取流水线顺序最晚节点；
                                   # 失败会话不存档，正常仅同组唯一 sid）
# 存档写入内联在 _phase_working（本节点 sessions.subject 位，组查找读全流水线）
```

- **bounces 计数保持节点级**：上游打回不消耗下游预算（会话复用 ≠ 打回预算复用）。
  「防上下文膨胀」的 bounces<2 门只看本节点计数——组继承可能 resume 上游多轮
  打回累积的会话，这是有意取舍：组内本来就是同一思考流，上游打回历史对下游
  是有效上下文而非污染；若需严格隔离可拆组。
- **跨节点继承发完整任务书**：resume 的 sid 来自组内上游节点（非本节点存档）且无
  打回反馈时，注入 build_subject_prompt 完整任务书——会话历史里有上游节点的任务书，
  但本节点的目标/产物/上游物料必须重述；只有本节点自存档的中断续跑才发「继续」短指令。
- **entry 打回清下游 sid**：下游产物基于旧上游产出被门禁判无效时，同步清下游
  节点的主体**与澄清** sid 存档（澄清结论同样基于旧上游产出）——重进时不得
  resume 失效上下文（组复用场景下防旧会话污染）。
- resume 失败 sid pop：**组级清空**（review-chat 路径同样走 _pop_subject_group_sid，
  两路径降级语义一致）——会话失效是组级的（同一 sid）。
- 打回隔离：组内打回（如 plan 打回）resume 组会话——**这是预期行为**（同组
  本来就是同一思考流，打回反馈延续在同一对话里处理），文档明示。
- GDD_SUBJECT_RESUME=off 仍然全局短路（逃生舱保留）。

## 4. 内置模板

delivery（ 九节点）套 `clarify-design` 预设：project_init→survey→clarify→plan
四个相邻 clarify 节点归入 gdd_design 组，implement 及以后独立——其中 implement
靠显式 `session_group: impl_standalone` 自述语义（不受预设影响）。内置模板现为
文件化定义（gdd/templates/delivery.json，唯一权威源），无其他内置模板。

## 5. 迁移

`session_group`/`session_preset` 加入 `_migrate_legacy_fingerprint` 的
added_keys（同 clarify 先例）：存量实例磁盘模板剔除新键后重算指纹命中
→ legacy，重写为展开后实例；带 truthy 新字段的按篡改拒绝，fail-closed。

## 6. 明确不做

- 不做运行时动态换组（组关系只在模板解析时确定）。
- 不做组内会话选择性清理（干净失败全组降级）。
- 不做 token 成本核算展示（复用动机是记忆连续性，非省钱）。
