# 设计记录：working 期间补充信息（steer）

> 状态：已落地。关联：`gdd steer`、`gdd/steer.py`、主体会话的 PostToolUse。

## 1. 问题

`gdd run` / `gdd go` 在主体 working 期间占着 `.gdd/lock`，prompt 写完就关闭 stdin。工具循环在 Claude Code / Codex 进程内部，gdd 只能看见流式事件，不能在中途把一句话塞进当前这次模型请求。

用户看到走向偏了，需要在**下一次工具返回之后、模型决定下一步之前**把补充交出去。澄清通道（`gdd chat`）只在节点启动前和审批挂起时开放，working 不是澄清窗口，也不该改相位、不该消耗打回预算。

## 2. 做法

注册一次，挂住整个主体会话。有没有补充，由 hook 每次工具返回时看队列。

| 环节 | 行为 |
|---|---|
| `gdd steer "…"` | 把原文追加到仓库外的队列。不拿 `.gdd/lock`，所以另一个终端可以在 `gdd run` / `gdd go` 进行中写入 |
| 主体会话启动 | 挂上 `PostToolUse`，matcher `.*`。entry/exit 门禁不挂 |
| 每次工具返回 | hook 读队列。空则 stdout 无输出；有则输出 `hookSpecificOutput.additionalContext` 并消费 |
| 会话成功但队列还有剩余 | 说明本轮没有后续工具调用。用已有 `session_id` `--resume` **补送一次**，把原文写进 prompt |
| 补送失败或被中断 | 原文放回队首。成功主体的 session id 在补送开始前已经落盘，中断不把这一轮主体结果丢掉。相位仍停在 working，下次 `gdd run` 从这里续 |

队列路径：`${GDD_STEER_HOME:-~/.gdd/steer}/<项目真实路径 sha256 前 16 位>/queue.jsonl`。Claude 的 settings 和 Codex 的 `hooks.json` 写在同一目录，都指向：

```text
python3 gdd/steer.py hook <queue.jsonl>
```

hook 必须先读完 stdin（工具结果可能很大），再决定 stdout。退出码保持 0：Codex 的退出码 2 会替换工具结果，那不是补充信息。

Claude 执行端在主体命令上追加 `--settings <claude.settings.json>`（追加一层，只含这条 hook）。Codex 的 `hooks.json` 用同一份 JSON 写好，但 gdd 的进程参数仍是 Claude headless（`-p --output-format`），**不会**改成 `codex exec`，因此 Codex 不会自动加载这份文件。协议已经对齐，接入 Codex 执行端是另一件事。

`GDD_STEER=off` 关闭挂载和补送。`GDD_SUBJECT_RESUME=off` 时不做 `--resume` 补送；Claude 上的 hook 仍会在下一次工具返回时送达。

## 3. 不做什么

- 不改相位、不记打回、不把补充写进 state.json（避免和持锁的 `run` 抢状态文件）。
- 不在看到 `tool_use` 事件时杀掉进程再 resume。那个事件发出时工具还在跑。
- 不把补充注入门禁会话。门禁要保持独立视角。
- 不保证模型服从这段文字。它是建议，主体会话有 Write/Bash，队列放在仓库外只是避免顺手改到 hook 命令。

## 4. 送达边界

- 工具已经发出去的那一次，补充赶不上；挂在**下一次返回**的结果后面。
- 并行工具各自跑完，补充贴在 hook 先执行到的那一次上。
- 模型不再调用工具时，靠会话结束后的那一次 resume。只补送一轮。这一轮里新写的、又没被工具吃掉的补充留在队列里，下一次主体会话继续送（可能是打回重跑，也可能是下一节点）。
- 单条补充上限 8000 字符。hook 发出的 `additionalContext` 还要加上固定前缀，所以一次工具返回最多送出 `8000 - 前缀` 的正文，其余留在队列里，避免超限后只剩预览、原文却已删掉。会话结束时的补送写进 prompt，仍一次取最多 8000 字符正文。
- `gdd steer` 用仓库外的 `subject_active` 标记判断主体进程是否还在跑。exit 门禁期间相位仍是 working，但标记已清，这时入队会提示「下一次主体会话」。`GDD_STEER=off` 直接拒绝入队。
- 流水线已经离开 working（审批挂起、completed）时再 `gdd steer`，命令仍入队并提示当前不是 working。`gdd status` 会显示未送达条数。
- 补送那一轮如果已经改了文件再失败，文件留在工作区，接着走 exit 门禁。门禁不通过会打回，这是判定，不是把成功的主体结果作废。
- Claude 若以 `--bare` / `--safe-mode` 跳过 settings hook，或本机校验静默忽略 settings，hook 不会响，仍靠结束时的补送。
- 补送依赖 `session_id`。主体会话失败、没有 sid、或 `GDD_SUBJECT_RESUME=off` 时不补送，原文留在队列里。

## 5. 用法

```bash
# 终端 A
gdd go

# 终端 B，working 期间
gdd steer "不要引入 Redis，状态写本地文件"
```
