# 设计记录：主体会话复用（resume）与要点澄清（clarify）通道

> 状态：已评审定稿，随实现落地。关联：README 设计边界、USER_GUIDE。

## 1. 背景与问题

| # | 问题 | 现状证据 |
|---|---|---|
| 1 | 打回后主体冷启动重跑 | `runner.py:117` 已解析出 `session_id` 但 orchestrator 丢弃；每次重跑重新读代码库，输入 token 大头浪费 |
| 2 | 需求澄清无法对话 | 唯一交互点是 `awaiting_approval`（CLI/UI 同一语义）；每轮"沟通"= 驳回 + feedback 一段文字，且每轮成本 = 主体+exit 两次冷启动会话 |
| 3 | 交互语义混淆 | 判定（审批）与对齐（澄清）混在同一反馈框；审批是稀缺卡点，澄清被挤成"昂贵的打回循环" |

## 2. 两种交互的显式区分

| | ① request_approval（强门禁卡点） | ② clarification（交互式信息对齐） |
|---|---|---|
| 本质 | 判定：人是审批者 | 对齐：人是需求方 |
| 回应 | 批准/驳回（二选一） | 自由文本，多轮 |
| 状态机 | 改变（exit_passed→done 或 bounce） | **不变**（phase/status 纹丝不动） |
| 打回预算 | 驳回计入 bounces，可致 blocked | **不消耗**（防线在 exit 门禁） |
| 触发位置 | 仅 `approval.required` 节点 | 仅 `clarify: true` 节点（模板显式声明） |
| 产出 | 判定记录 | 对话记录 + **固化 artifact** |
| UI 形态 | 琥珀色「⏸ 等你审批」 | 蓝色「💬 澄清中」对话区 |
| history | awaiting_approval / approval_pass / approval_reject | clarify_msg / clarify_done |

**依据**：审批是稀缺卡点（人工成本高、消耗预算、有 blocked 风险），必须只出现在判定点；澄清是廉价对齐，若混用审批通道，每轮对话都付"审批 + 打回 + 双冷启动会话"的成本。区分后防线不放松（见 §5）。

## 3. 会话复用：复用谁、不复用谁

| 会话 | 策略 | 依据 |
|---|---|---|
| 主体 subject | **复用**（`claude -p --resume <sid>`） | 打回 = "继续改"：resume 携带它自己做过什么的记忆，修改更准、省去重读代码库 |
| entry / exit 门禁 | **每次全新，绝不复用** | 门禁价值在独立视角。resume 的门禁带"上次怎么判"的上下文：pass 过的倾向继续 pass，fail 的可能被旧论证说服——门禁被自我污染，防篡改体系失效 |

存储：`state.node_states[node]["sessions"] = {"subject": "<sid>", "clarify": "<sid>"}`（state 原子写已有，防篡改比对自动覆盖）。

**边界条件**：
- resume 失败（会话过期/找不到）→ 清 sid 回退全新会话，记 `session_resume_fail` 事件；
- 同节点打回 ≥2 次后不再 resume（防上下文膨胀 + 被驳回思路污染），后续全新会话；
- `GDD_SUBJECT_RESUME=off` 全局关闭复用；

**打回项的验收闭环**（打回原因如何被核验）：
- 打回反馈随 exit 门禁 prompt 注入"上轮反馈"段——门禁逐项核实是否已落实，任一项未落实则 fail。这堵住"反馈超出门禁声明判据（如人工驳回的『补回滚方案』）时无人验收"的缺口；主体修得对不对不再只靠其自觉，由门禁独立验收。
- **消费时点在"exit 核验通过"而非"主体成功"**：反馈保留在 state 直到门禁放行才 pop——门禁自身故障（exit_retry 路径同样携带核验项）或进程崩溃重启，核验项都不丢失；exit 失败打回时新反馈覆写。
- 澄清纪要（clarify_done 注入）与打回项的区分用**结构化判据**：state 新增 `feedback_kinds`（node_id → "bounce"|"clarify"），由 harness 在写入点确定性设置，门禁按 kind 选择核验措辞（打回项→逐项核实任一未落实则 fail；纪要→非强制判据，明显偏离用户意图才 fail）。文本子串"澄清对齐纪要"仅作旧 state（无 kind 字段）的兼容回退，不再承担判定——杜绝 LLM/人工反馈含特定字样导致的误降级。
- 全新门禁会话 = "是否还有新问题"的自然机制：门禁每次全量重验（机器检查 + AI 判据 + supplements 固化项只增不减），新问题天然被发现，无需针对性设计。
- 机器检查失败也结构化为修改指引（缺失产物→创建、未含必含内容→补齐、命令失败→修复；超长按行边界截断不切半条），主体收到的是可执行动作而非裸失败清单；resume 路径的反馈段同时附上门禁证据列表（单条截断 300 字），供主体对照修改。反馈注入门禁 prompt 截断 2000 字。
- resume 时 prompt 换成"反馈 + 修改指令"，不再重发完整主体 prompt。

## 4. clarify 通道设计

**触发时机**（两种，都不改状态机）：

| 时机 | 场景 | 机制 | 结束动作（clarify_done） |
|---|---|---|---|
| 节点启动前（phase=pending） | 需求先聊清楚再干活 | 新建澄清会话（独立 sid），结论写入产出文档 | 记事件，正常启动即可 |
| 审批挂起时（awaiting_approval） | 对已产出方案追问/微调 | resume 主体会话 | 纪要注入 feedbacks → phase 回 working → 主体 resume 收尾 → **强制重过 exit 门禁** → 重新挂审批 |

**对话记录**：`.gdd/dialogs/<node>.jsonl`，逐行 `{"ts","role","text"}`。不进 state（避免膨胀），不纳入防篡改（审计依赖 history 事件，已知边界）。

**产出固化**：澄清结论必须写成 workspace 内 artifact（节点 exit artifacts 路径，或兜底 `CLARIFY_<NODE>.md`）——下游节点与 exit 门禁通过 `collect_upstream_artifacts` 可见。**对话记忆不能作为判定依据**。

## 5. 三条硬约束（体验让位于 harness 判定权）

1. **并发互斥**：run 推进中不可 chat（flock 拦截）；chat 持锁期间 run/approve 会被拒绝。
2. **对话不动状态机**：chat 只 resume 会话 + 追加 jsonl + 记 history；任务停留原状态。
3. **改动必过门禁**：任何经澄清产生的 artifact 变更，最终都要被全新 exit 门禁会话重新判定——澄清不是绕过判定的后门。这也是澄清不消耗打回预算的正当性来源：滥用会被门禁卡住，而不是被预算卡住。

   机制化（非仅约定）：审批挂起时记录 exit 产物指纹快照（pending_approval.artifact_snapshot，glob 声明按门禁同一匹配语义展开到具体文件）；approve 时核验，指纹变化/新增匹配文件/文件消失均计变更，产物变更则拒绝放行（approval_refused_artifact_changed）并保持挂起，引导走 clarify_done 重验或驳回——堵住「对话改产物→直接批准」旁路。

## 6. 兼容性：legacy 指纹迁移

`NodeSpec` 新增 `clarify` 字段会改变 `pipeline_fingerprint`（to_dict 多一个键），存量运行实例下次 run 会误报指纹不匹配。迁移规则（防篡改安全）：

> 指纹校验失败时，把磁盘 pipeline dict 中各节点的 `clarify` 键**剔除后**重算指纹；若与 state 存储指纹一致 → 判定为 legacy 实例（仅缺新字段），刷新指纹放行；否则仍按篡改 fail-closed。

攻击者若篡改其他字段并剥掉 clarify 键，剔除后重算仍不匹配，无法借道迁移。

迁移语义边界（明确）：迁移仅恢复**指纹兼容性**，新增字段取默认值（`clarify: false`）。真实 legacy 实例创建于字段存在之前，模板从未声明过 clarify，在途任务**不因此获得澄清能力**；需要时 `gdd reset` 以新模板重新实例化。交互入口同样先过此门禁（合法 legacy 自动迁移放行，篡改拒绝）——覆盖四个入口对称防线：chat / clarify_done / approve（resolve_approval）/ retry，篡改态均 fail-closed，防止 awaiting_approval/blocked 态按篡改后的节点定义被批准或恢复推进。

补充：迁移后实例以内存 to_dict 重写（含默认新字段），state 指纹同步刷新并收敛——后续交互不再重复迁移。

## 7. 改动清单

| 文件 | 改动 |
|---|---|
| runner.py | `run(resume_sid=…)`；cmd 追加 `--resume` |
| models.py | `NodeSpec.clarify: bool = False`（to_dict/from_dict） |
| orchestrator.py | sessions 存取；`_phase_working` resume 逻辑 + 失败回退；`chat/clarify_done` + 指纹门禁；dialog 落盘；legacy 指纹迁移（run 与交互入口） |
| gates.py | `build_clarify_prompt` |
| gdd/templates/delivery.json | 内置 delivery（唯一权威源，builtins.py 仅加载器）：project_init/survey/clarify/plan/implement 五节点 `clarify: true` |
| cli.py | `gdd chat <task>` 终端对话循环（/done 结束） |
| ui.py | `chat`/`clarify_done` action（后台线程 + 失败可见）；审批卡/澄清卡双窗口 UI |
| tests/test_clarify.py | resume/clarify/迁移/防线回归 |

## 8. 与 Claude Code 体验的差距（诚实声明）

- 每轮为独立进程 resume（`claude -p --resume`），上下文完整但有几秒冷启动延迟；流式观感用 stream-json 逐行读实现（后续可选）。
- 改完不即生效：必须重过 exit 门禁——这是 GDD 判定权的底线，不做绕行。
- **自觉依赖的声明**：「澄清结论必须固化 artifact」靠 clarify prompt 纪律约束（每轮要求写入 CLARIFY_<node>.md / exit 产物），harness 层不校验固化是否发生；防线在硬规则 3——产物一旦变更，approve 被产物快照拦截、必须重过 exit 门禁，未固化的结论不影响判定链。
