"""GDD — Goal Driven Development harness.

编排可自定义的节点流水线；每个节点有独立的 entry/exit 门禁会话与执行主体会话。
节点完成与否由 harness 侧门禁判定（机器检查 + 独立 AI 验证），不采信 Agent 自评。
"""

__version__ = "0.1.0"
