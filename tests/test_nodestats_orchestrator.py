"""编排器段级计时与门禁会话捕获（T3）。

覆盖：
- Verdict.session：AI 门禁会话结果随 Verdict 返回；纯机器门禁保持 None；门禁故障路径也带回会话。
- entry 重试 / exit 故障重试 / exit 失败后的主体重跑：各段按活动区间累计，total = 三段之和。
- 人工审批等待、澄清会话、步骤间空档不计入任何段。
- entry 打回前一节点：前一节点主体重跑计入其 subject 段，不计入当前节点 entry 段。
- adversarial 重试计入 subject 段。
- 统计采集/写入抛异常不影响节点推进；旧 state.json 缺 node_stats 仍可加载推进。

运行：python3 tests/test_nodestats_orchestrator.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gdd.orchestrator as orch  # noqa: E402
from gdd import gates, nodestats  # noqa: E402
from gdd.models import NodeSpec  # noqa: E402
from gdd.orchestrator import Orchestrator  # noqa: E402
from gdd.runner import SessionResult  # noqa: E402

PASS = 'GDD-VERDICT: {"verdict": "pass", "reason": "ok", "evidence": ["e"], "feedback": ""}'
FAIL = 'GDD-VERDICT: {"verdict": "fail", "reason": "no", "evidence": [], "feedback": "fix"}'
ADV_PASS = 'GDD-ADVERSARIAL: {"cases": [{"id": "x", "result": "pass", "cause": "test", "reason": "ok"}]}'


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def close(a, b, eps=1e-6):
    return abs(float(a) - float(b)) <= eps


# ---------------------------------------------------------------- 可控时钟 / 脚本执行端

class Clock:
    def __init__(self, start=1000.0):
        self.t = float(start)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


class Ctl:
    """按会话角色出队的脚本；每次 run 推进可控时钟。"""

    def __init__(self):
        self.queues = {}
        self.calls = []

    def reset(self):
        self.queues = {}
        self.calls = []

    def push(self, role, **spec):
        self.queues.setdefault(role, []).append(spec)

    def pop(self, role):
        q = self.queues.get(role) or []
        if not q:
            raise AssertionError(f"未预期的 {role} 会话调用（脚本已耗尽）")
        self.calls.append(role)
        return q.pop(0)


CTRL = Ctl()
CLOCK = Clock()


class ScriptedRunner:
    def __init__(self, root):
        self.root = root

    def run(self, prompt, *, allowed_tools, max_turns, log_path=None,
            resume_sid="", on_stream=None):
        role = "subject"
        if "[[GDD-ROLE:gate:entry" in prompt:
            role = "entry"
        elif "[[GDD-ROLE:gate:exit" in prompt:
            role = "exit"
        elif "[[GDD-ROLE:clarify" in prompt:
            role = "clarify"
        elif "[[GDD-ROLE:adversarial" in prompt:
            role = "adversarial"
        spec = CTRL.pop(role)
        seconds = float(spec.get("seconds", 1.0))
        CLOCK.advance(seconds)
        text = spec.get("text")
        if text is None:
            text = PASS if role in ("entry", "exit") else "subject done"
        return SessionResult(
            ok=spec.get("ok", True),
            text=text,
            error=spec.get("error", ""),
            session_id=spec.get("sid", f"{role}-{len(CTRL.calls)}"),
            turns=1,
            duration_s=seconds,
            tool_calls=spec.get("tool_calls", 0),
            llm_calls=spec.get("llm_calls", 0),
            token_input=spec.get("token_input", 0),
            token_output=spec.get("token_output", 0),
            token_cache_creation=spec.get("token_cache_creation", 0),
            token_cache_read=spec.get("token_cache_read", 0),
            token_status=spec.get("token_status", "available"),
        )


def with_runner():
    orig = orch.AgentRunner
    orch.AgentRunner = ScriptedRunner
    return orig


def restore_runner(orig):
    orch.AgentRunner = orig


def setup(pipe, requirement="req"):
    root = tempfile.mkdtemp(prefix="gdd-ns-orch-")
    o = Orchestrator(root, defer_approval=True)
    o.init(pipe, requirement)
    o.load()
    o._stats_now = CLOCK.now
    return root, o


def run_steps(o, limit=30, until=None):
    for _ in range(limit):
        if until is not None and until(o):
            return
        if not o.step():
            return
    raise SystemExit("FAIL: step 超过上限仍未停止")


def view(o, node_id):
    return nodestats.for_ui(o.state, node_id)


# ---------------------------------------------------------------- T3-1 Verdict.session

def test_verdict_session():
    print("[T3-1] Verdict.session：AI 门禁带回会话，纯机器门禁为 None")
    CTRL.reset()
    root = tempfile.mkdtemp(prefix="gdd-ns-gate-")
    orig = with_runner()
    try:
        node = NodeSpec.from_dict({"id": "n", "name": "n", "entry": {"ai_check": "检查"}})
        CTRL.push("entry", sid="gate-1", seconds=2.0, tool_calls=3, llm_calls=1,
                  token_input=100, token_output=10, token_cache_creation=5, token_cache_read=20)
        verdict = gates.run_gate("entry", node, root, ScriptedRunner(root))
        check(verdict.passed, "AI 门禁判定通过")
        check(verdict.session is not None and verdict.session.session_id == "gate-1",
              "Verdict.session 带回门禁会话")
        check(verdict.session.tool_calls == 3 and verdict.session.llm_calls == 1
              and verdict.session.token_input == 100, "门禁会话指标随 Verdict 可见")

        machine_node = NodeSpec.from_dict({"id": "m", "name": "m"})
        machine = gates.run_gate("entry", machine_node, root, ScriptedRunner(root))
        check(machine.passed and machine.session is None, "纯机器门禁 session 为 None，不虚报会话")

        CTRL.push("entry", ok=False, error="gate crash", sid="gate-boom", seconds=1.0)
        boom = gates.run_gate("entry", node, root, ScriptedRunner(root))
        check(boom.gate_error and boom.session is not None
              and boom.session.session_id == "gate-boom", "门禁故障路径同样带回会话")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- T3-2 段边界 + 审批/澄清排除

def test_segments_with_retries_and_waits():
    print("[T3-2] entry/exit 重试、多区间累计；审批与澄清不计入")
    CTRL.reset()
    CLOCK.t = 1000.0
    pipe = {
        "id": "p", "name": "p", "git_delivery": {"enabled": False},
        "nodes": [{
            "id": "a", "name": "A", "clarify": True,
            "entry": {"ai_check": "入口检查"}, "exit": {"ai_check": "出口检查"},
            "approval": {"required": True},
        }],
    }
    root, o = setup(pipe)
    orig = with_runner()
    try:
        CTRL.push("clarify", seconds=11.0, sid="clarify-1", text="对齐完成")
        # entry：第一次门禁自身故障（3s），重试通过（2s）
        CTRL.push("entry", ok=False, error="entry gate boom", seconds=3.0, sid="entry-1",
                  tool_calls=3, llm_calls=1,
                  token_input=100, token_output=10, token_cache_creation=5, token_cache_read=20)
        CTRL.push("entry", seconds=2.0, sid="entry-2", tool_calls=1, llm_calls=1,
                  token_input=200, token_output=20)
        # 主体：首轮 4s；exit 失败后重跑 3s
        CTRL.push("subject", seconds=4.0, sid="subj-1")
        CTRL.push("subject", seconds=3.0, sid="subj-1")
        # exit：失败 6s（usage 不可得），第二轮通过 5s
        CTRL.push("exit", text=FAIL, seconds=6.0, sid="exit-1", tool_calls=2, llm_calls=1,
                  token_status="unavailable")
        CTRL.push("exit", seconds=5.0, sid="exit-2", tool_calls=1, llm_calls=1,
                  token_input=50, token_output=0)

        # 澄清区间（节点 pending，状态机之外）不计入任何段
        res = o.chat("a", "先对齐需求")
        check(res.get("ok"), "pending 窗口澄清会话成功")
        t_after_chat = CLOCK.t
        check(close(t_after_chat, 1011.0), "澄清会话推进了 11s 时钟")

        run_steps(o, until=lambda x: x.state["node_states"]["a"]["phase"] != "pending")
        check(o.state["node_states"]["a"]["phase"] == "working", "entry 故障重试后进入主体")
        CLOCK.advance(7.0)  # 步骤间空档：不计入任何段
        run_steps(o, until=lambda x: x.state["node_states"]["a"]["phase"] == "exit_passed")
        CLOCK.advance(123.0)  # 人工审阅前的空闲等待（未进入审批挂起）
        run_steps(o, until=lambda x: x.state["status"] == "awaiting_approval")
        check(o.state["status"] == "awaiting_approval", "exit 通过后进入人工审批挂起")
        CLOCK.advance(300.0)  # 人工审批等待：不计入任何段
        check(o.resolve_approval(True), "审批通过")
        check(o.state["status"] == "completed", "任务完成")

        v = view(o, "a")
        entry = v["segments"]["entry"]
        subject = v["segments"]["subject"]
        exit_seg = v["segments"]["exit"]
        check(close(entry["duration_s"], 5.0), "entry = 故障重试 3s + 通过 2s（不含后续空闲）")
        check(close(subject["duration_s"], 7.0), "subject = 首轮 4s + exit 失败后重跑 3s")
        check(close(exit_seg["duration_s"], 11.0), "exit = 失败 6s + 重试通过 5s")
        check(close(v["total_s"], 23.0), "total = 三段之和，澄清/审批/空档均不计入")

        entry_sessions = entry["sessions"]
        check(len(entry_sessions) == 2 and all(s["kind"] == "entry" for s in entry_sessions),
              "entry 两次门禁尝试各记一条 entry 会话")
        check(entry["tool_calls"] == 4 and entry["llm_calls"] == 2, "entry 工具/LLM 为两次会话之和")
        check(entry["token_status"] == "available" and entry["token_total"] == 355,
              "entry token = 135 + 220")

        exit_sessions = exit_seg["sessions"]
        check(len(exit_sessions) == 2 and all(s["kind"] == "exit" for s in exit_sessions),
              "exit 两次门禁尝试各记一条 exit 会话")
        check(exit_seg["token_status"] == "partial" and exit_seg["token_total"] == 50,
              "exit 段部分不可得：展示已知合计 50 并标 partial")
        check(exit_sessions[0]["token_status"] == "unavailable"
              and exit_sessions[0]["token_total"] is None,
              "不可得会话 token_total 为空，不以 0 冒充")

        reloaded = Orchestrator(root, defer_approval=True)
        check(reloaded.load(), "完成后新实例可加载 state")
        persisted = nodestats.for_ui(reloaded.state, "a")
        check(persisted is not None and close(persisted["total_s"], 23.0),
              "统计随 state.json 落盘，重启后仍可读取")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- T3-3 entry 打回归属

def test_entry_bounce_attribution():
    print("[T3-3] entry 打回前一节点：重跑计入前节点 subject，不计入当前节点 entry")
    CTRL.reset()
    CLOCK.t = 2000.0
    pipe = {
        "id": "p", "name": "p", "git_delivery": {"enabled": False},
        "nodes": [
            {"id": "a", "name": "A", "entry": {"ai_check": "e"}, "exit": {"ai_check": "x"}},
            {"id": "b", "name": "B", "entry": {"ai_check": "e"}, "exit": {"ai_check": "x"}},
        ],
    }
    root, o = setup(pipe)
    orig = with_runner()
    try:
        CTRL.push("entry", seconds=1.0, sid="a-entry")
        CTRL.push("subject", seconds=2.0, sid="a-subj")
        CTRL.push("exit", seconds=1.0, sid="a-exit")
        CTRL.push("subject", seconds=2.0, sid="a-subj")     # 被 b 的 entry 打回后重跑
        CTRL.push("exit", seconds=1.0, sid="a-exit")
        CTRL.push("entry", text=FAIL, seconds=3.0, sid="b-entry-1")
        CTRL.push("entry", seconds=1.0, sid="b-entry-2")
        CTRL.push("subject", seconds=1.0, sid="b-subj")
        CTRL.push("exit", seconds=1.0, sid="b-exit")

        run_steps(o, limit=40)
        check(o.state["status"] == "completed", "两节点流水线完成")

        va = view(o, "a")["segments"]
        vb = view(o, "b")["segments"]
        check(close(va["entry"]["duration_s"], 1.0), "a.entry = 一次通过 1s")
        check(close(va["subject"]["duration_s"], 4.0), "a.subject = 首轮 2s + 被打回后重跑 2s")
        check(close(va["exit"]["duration_s"], 2.0), "a.exit = 两次 exit 门禁各 1s")
        check(close(vb["entry"]["duration_s"], 4.0), "b.entry = 失败尝试 3s + 重试通过 1s")
        check(close(vb["subject"]["duration_s"], 1.0), "b.subject 只含自己的主体 1s")
        check(close(vb["exit"]["duration_s"], 1.0), "b.exit 1s")
        check(close(vb["entry"]["duration_s"], 4.0),
              "a 的重跑 2s 未混入 b.entry（否则为 6s）")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- T3-4 adversarial 重试计入 subject

def test_adversarial_retry_subject_segment():
    print("[T3-4] adversarial 重试会话计入 subject 段")
    CTRL.reset()
    CLOCK.t = 3000.0
    pipe = {
        "id": "p", "name": "p", "git_delivery": {"enabled": False},
        "nodes": [{
            "id": "t", "name": "T", "adversarial": True,
            "entry": {"ai_check": "e"}, "exit": {"ai_check": "x"},
        }],
    }
    root, o = setup(pipe)
    orig = with_runner()
    try:
        CTRL.push("entry", seconds=1.0, sid="t-entry")
        CTRL.push("subject", seconds=2.0, sid="t-subj")
        CTRL.push("adversarial", text="not-a-verdict", seconds=3.0, sid="adv-1")
        CTRL.push("adversarial", text=ADV_PASS, seconds=2.0, sid="adv-2")
        CTRL.push("exit", seconds=1.0, sid="t-exit")

        run_steps(o, limit=30)
        check(o.state["status"] == "completed", "带对抗重试的节点完成")
        seg = view(o, "t")["segments"]
        check(close(seg["subject"]["duration_s"], 7.0),
              "subject = 主体 2s + 对抗失败 3s + 对抗重试 2s")
        check(close(seg["entry"]["duration_s"], 1.0) and close(seg["exit"]["duration_s"], 1.0),
              "entry/exit 各自只含门禁窗口")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- T3-5 统计失败隔离

def test_stats_failure_is_isolated():
    print("[T3-5] 统计采集抛异常不影响节点推进")
    CTRL.reset()
    CLOCK.t = 4000.0
    pipe = {
        "id": "p", "name": "p", "git_delivery": {"enabled": False},
        "nodes": [{"id": "a", "name": "A", "entry": {"ai_check": "e"}, "exit": {"ai_check": "x"}}],
    }
    root, o = setup(pipe)
    orig = with_runner()
    saved = (nodestats.open_segment, nodestats.close_segment, nodestats.add_session)

    def boom(*_a, **_k):
        raise RuntimeError("stats exploded")

    nodestats.open_segment = boom
    nodestats.close_segment = boom
    nodestats.add_session = boom
    try:
        CTRL.push("entry", seconds=1.0, sid="e1")
        CTRL.push("subject", seconds=1.0, sid="s1")
        CTRL.push("exit", seconds=1.0, sid="x1")
        run_steps(o, limit=20)
        check(o.state["status"] == "completed", "统计全失败时节点仍按门禁结果完成")
        events = [h.get("event") for h in o.state["history"]]
        check("node_stats_error" in events, "统计失败以 node_stats_error 事件留痕")
        check(any(h.get("action") == "open" for h in o.state["history"]
                  if h.get("event") == "node_stats_error"), "诊断包含失败动作")
        check(nodestats.for_ui(o.state, "a") is None, "无有效统计时不虚报节点统计")
    finally:
        nodestats.open_segment, nodestats.close_segment, nodestats.add_session = saved
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- T3-6 旧 state 兼容

def test_old_state_without_node_stats():
    print("[T3-6] 旧 state.json 无 node_stats 时仍可加载推进")
    CTRL.reset()
    CLOCK.t = 5000.0
    pipe = {
        "id": "p", "name": "p", "git_delivery": {"enabled": False},
        "nodes": [{"id": "a", "name": "A"}],
    }
    root, o = setup(pipe)
    orig = with_runner()
    try:
        path = os.path.join(root, ".gdd", "state.json")
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        raw.pop("node_stats", None)  # 模拟旧版本 state
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        check(o.load(), "缺 node_stats 的旧 state 加载成功")
        fp_before = o._pipeline_fingerprint()

        CTRL.push("subject", seconds=4.0, sid="s-old")
        run_steps(o, limit=20)
        check(o.state["status"] == "completed", "旧 state 推进完成")
        check(o._pipeline_fingerprint() == fp_before, "统计生效前后运行实例指纹不变")
        v = view(o, "a")
        check(v is not None and close(v["segments"]["subject"]["duration_s"], 4.0),
              "推进后按需建立统计，subject 4s")
        check(close(v["segments"]["entry"]["duration_s"], 0.0)
              and close(v["segments"]["exit"]["duration_s"], 0.0),
              "空门禁段耗时为机器检查的真实 0")
        check(close(v["total_s"], 4.0), "total 只含真实活动区间")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


def main():
    test_verdict_session()
    test_segments_with_retries_and_waits()
    test_entry_bounce_attribution()
    test_adversarial_retry_subject_segment()
    test_stats_failure_is_isolated()
    test_old_state_without_node_stats()
    print("ALL NODESTATS ORCHESTRATOR PASSED")


if __name__ == "__main__":
    main()
