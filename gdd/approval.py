"""人工准入（request_approval）：exit 通过后、进入下一阶段前的确认。

交互终端内联确认；非交互环境写入 pending_approval 状态，
由 `gdd approve --yes / --reject --feedback "..."` 异步裁决。
"""
from __future__ import annotations

import sys
import threading
from typing import Optional


def is_interactive() -> bool:
    # 线程感知：UI 后台线程（_bg_run/_bg_chat）即使进程挂在 tty 上也不得走
    # input() 交互审批——那会在持 flock 期间阻塞等终端输入，UI 前端只见
    # "运行中"且 approve/chat/reset 全被自身锁拒。仅主线程 + tty 允许交互。
    if threading.current_thread() is not threading.main_thread():
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def render_approval_summary(node, exit_evidence, bounce_count: int) -> str:
    lines = [
        "=" * 60,
        f"人工准入确认：{node.name}（{node.id}）",
        "=" * 60,
        f"确认要点：{node.approval.prompt or node.exit.goal or '请审查本节点产出'}",
        "",
        "exit 门禁证据：",
    ]
    if exit_evidence:
        lines += [f"  - {e}" for e in exit_evidence[:10]]
    else:
        lines.append("  （无记录）")
    lines.append(f"累计打回次数：{bounce_count}/{node.max_bounces}")
    lines.append("=" * 60)
    return "\n".join(lines)


def interactive_approval(node, exit_evidence, bounce_count: int):
    """返回 True=批准 / False=打回（feedback 由调用方继续交互获取）/
    "keep"=输入中断（EOF/Ctrl-C，保持挂起交后续裁决）/ None=中止。

    仅在 tty 环境下使用；非 tty 由 orchestrator 转入 pending 状态。
    """
    print(render_approval_summary(node, exit_evidence, bounce_count))
    try:
        while True:
            ans = input("[y]批准进入下一阶段 / [n]打回并反馈 / [a]中止流水线: ").strip().lower()
            if ans in ("y", "yes"):
                return True
            if ans in ("n", "no"):
                return False
            if ans in ("a", "abort"):
                return None
            print("请输入 y / n / a")
    except (EOFError, KeyboardInterrupt):
        # EOF/Ctrl-D/Ctrl-C 不静默中止整条流水线：返回 "keep"，
        # 调用方（orchestrator）据此建挂起态交 gdd approve/go 后续裁决
        print("\n（输入中断：审批保持挂起，未裁决）")
        return "keep"


def interactive_feedback() -> str:
    try:
        fb = input("打回反馈（将传递给该节点执行主体）: ").strip()
        return fb or "人工审批打回，未填写具体反馈。"
    except (EOFError, KeyboardInterrupt):
        return "人工审批打回，未填写具体反馈。"
