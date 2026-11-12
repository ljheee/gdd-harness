"""主体 / 并行任务 / review / adversarial 会话记录（T4）。

覆盖：
- 并行开发任务：每个 task 会话独立记录（task_id / session_id / 耗时 / 工具 / LLM / token），
  主体段耗时为并行墙钟（max），不等于各会话耗时之和；段合计为各会话之和。
- review 会话、adversarial 会话归属 subject 段，kind 分别记录。
- 同一 subject session id 的续跑（exit 失败后 resume）合并为一条记录（attempts/耗时/指标累加）。
- 主体段多区间累计（首轮 + 打回后 resume）且会话记录不重复。

运行：python3 tests/test_nodestats_sessions.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gdd.orchestrator as orch  # noqa: E402
from gdd import nodestats  # noqa: E402
from gdd.orchestrator import Orchestrator  # noqa: E402
from gdd.runner import SessionResult  # noqa: E402

PASS = 'GDD-VERDICT: {"verdict": "pass", "reason": "ok", "evidence": ["e"], "feedback": ""}'
FAIL = 'GDD-VERDICT: {"verdict": "fail", "reason": "no", "evidence": [], "feedback": "fix"}'
ADV_PASS = 'GDD-ADVERSARIAL: {"cases": [{"id": "x", "result": "pass", "cause": "test", "reason": "ok"}]}'
REVIEW_PASS = 'GDD-REVIEW: {"verdict": "pass", "issues": []}'
PLAN_WITH_TASKS = ("# P\n## 实现步骤\n## 附录：任务依赖\n"
                   "- T1 | - | 甲\n- T2 | - | 乙\n")


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def close(a, b, eps=1e-6):
    return abs(float(a) - float(b)) <= eps


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------- 可控时钟 / 执行端

class Clock:
    """线程安全可控时钟；advance_to 只前进，模拟并行区间重叠（墙钟取 max）。"""

    def __init__(self, start=1000.0):
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self):
        with self._lock:
            return self._t

    def advance_to(self, value):
        with self._lock:
            self._t = max(self._t, float(value))


CLOCK = Clock(start=1000.0)


class FakeRunner:
    """按 prompt 角色出队的会话替身；task 会话写核销行并按可控时钟模拟并行。"""

    clock = CLOCK
    barrier = None
    plan: dict = {}
    calls: list = []
    lock = threading.Lock()

    def __init__(self, root):
        self.root = root

    @staticmethod
    def reset(start=1000.0):
        FakeRunner.clock = CLOCK
        CLOCK._t = float(start)
        FakeRunner.barrier = None
        FakeRunner.plan = {}
        FakeRunner.calls = []

    @staticmethod
    def push(role, spec):
        FakeRunner.plan.setdefault(role, []).append(spec)

    @staticmethod
    def set_answer(role, spec):
        FakeRunner.plan[role] = [spec]

    @staticmethod
    def _role(prompt: str) -> str:
        if "[[GDD-TASK:" in prompt:
            return "task"
        if "[[GDD-ROLE:gate:entry" in prompt:
            return "entry"
        if "[[GDD-ROLE:gate:exit" in prompt:
            return "exit"
        if "[[GDD-ROLE:review" in prompt:
            return "review"
        if "[[GDD-ROLE:adversarial" in prompt:
            return "adversarial"
        if "[[GDD-ROLE:clarify" in prompt:
            return "clarify"
        return "subject"

    def run(self, prompt, *, allowed_tools, max_turns, log_path=None,
            resume_sid="", on_stream=None):
        role = FakeRunner._role(prompt)
        with FakeRunner.lock:
            if role == "task":
                tid = prompt.split("[[GDD-TASK:", 1)[1].split("]", 1)[0]
                spec = (FakeRunner.plan.get("task") or {}).get(tid)
                if spec is None:
                    raise AssertionError(f"未预期的任务会话 {tid}")
                FakeRunner.calls.append(f"task:{tid}")
            else:
                queue = FakeRunner.plan.get(role) or []
                if not queue:
                    raise AssertionError(f"未预期的 {role} 会话调用（脚本已耗尽）")
                FakeRunner.calls.append(role)
                spec = queue.pop(0)
        started = FakeRunner.clock.now()
        seconds = float(spec.get("seconds", 0.0))
        if role == "task" and FakeRunner.barrier is not None:
            FakeRunner.barrier.wait(timeout=10)
        if seconds:
            # 并行任务共用全局时钟：墙钟前进到「本会话逻辑结束」，先结束的不会回拨
            FakeRunner.clock.advance_to(started + seconds)
        if role == "task":
            tid = prompt.split("[[GDD-TASK:", 1)[1].split("]", 1)[0]
            _write(os.path.join(self.root, ".gdd", "task-results", f"{tid}.md"),
                   f"{tid}｜完成｜gdd/x.py:1｜ok｜无\n")
        text = spec.get("text")
        if text is None:
            text = {"entry": PASS, "exit": PASS, "adversarial": ADV_PASS,
                    "review": REVIEW_PASS}.get(role, "subject done")
        return SessionResult(
            ok=spec.get("ok", True),
            text=text,
            error=spec.get("error", ""),
            session_id=spec.get("sid", f"{role}-{len(FakeRunner.calls)}"),
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
    orch.AgentRunner = FakeRunner
    return orig


def restore_runner(orig):
    orch.AgentRunner = orig


def setup(pipe):
    root = tempfile.mkdtemp(prefix="gdd-ns-sess-")
    o = Orchestrator(root, defer_approval=True)
    o.init(pipe, "req")
    o.load()
    o._stats_now = CLOCK.now
    return root, o


def run_steps(o, limit=40):
    for _ in range(limit):
        if not o.step():
            return
    raise SystemExit("FAIL: step 超过上限仍未停止")


def view(o, node_id):
    return nodestats.for_ui(o.state, node_id)


# ---------------------------------------------------------------- T4-1 并行任务会话

def test_parallel_task_sessions():
    print("[T4-1] 并行开发任务：逐任务独立记录，主体段为墙钟而非会话耗时之和")
    FakeRunner.reset(start=1000.0)
    FakeRunner.barrier = threading.Barrier(2)
    pipe = {
        "id": "p", "name": "p", "git_delivery": {"enabled": False},
        "nodes": [{"id": "implement", "name": "I", "baseline_marker": True,
                   "subject": {"goal": "实现"}}],
    }
    root, o = setup(pipe)
    _write(os.path.join(root, "docs", "plan.md"), PLAN_WITH_TASKS)
    FakeRunner.plan["task"] = {
        "T1": {"sid": "sid-T1", "seconds": 10.0, "tool_calls": 3, "llm_calls": 2,
               "token_input": 100, "token_output": 10,
               "token_cache_creation": 5, "token_cache_read": 20},
        "T2": {"sid": "sid-T2", "seconds": 6.0, "tool_calls": 1, "llm_calls": 1,
               "token_input": 50, "token_output": 5},
    }
    orig = with_runner()
    try:
        run_steps(o)
        check(o.state["status"] == "completed", "并行任务流水线完成")
        subj = view(o, "implement")["segments"]["subject"]
        check(close(subj["duration_s"], 10.0),
              "主体段 = 并行墙钟 max(10s,6s)，不是会话耗时之和 16s")
        sessions = subj["sessions"]
        check(len(sessions) == 2 and all(s["kind"] == "task" for s in sessions),
              "两个并行任务各生成一条 kind=task 会话记录")
        by_task = {s["task_id"]: s for s in sessions}
        check(set(by_task) == {"T1", "T2"}, "会话按任务 ID 可区分")
        t1, t2 = by_task["T1"], by_task["T2"]
        check(t1["session_id"] == "sid-T1" and t2["session_id"] == "sid-T2",
              "会话记录保留各自 session id")
        check(close(t1["duration_s"], 10.0) and close(t2["duration_s"], 6.0),
              "会话耗时分别记录（10s / 6s）")
        check(t1["tool_calls"] == 3 and t1["llm_calls"] == 2
              and t1["token_total"] == 135 and t1["token_status"] == "available",
              "T1 指标：工具 3 / LLM 2 / token 100+10+5+20=135")
        check(t2["tool_calls"] == 1 and t2["llm_calls"] == 1 and t2["token_total"] == 55,
              "T2 指标：工具 1 / LLM 1 / token 50+5=55")
        check(subj["tool_calls"] == 4 and subj["llm_calls"] == 3,
              "主体段工具/LLM 合计等于各会话之和")
        check(subj["token_status"] == "available" and subj["token_total"] == 190,
              "主体段 token 合计 135+55=190")
        check(close(view(o, "implement")["total_s"], 10.0),
              "节点 total 只含主体真实墙钟（空门禁为 0）")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- T4-2 review 会话

def test_review_session():
    print("[T4-2] review 会话记录在 subject 段，kind=review")
    FakeRunner.reset(start=2000.0)
    pipe = {
        "id": "p", "name": "p", "git_delivery": {"enabled": False},
        "nodes": [
            {"id": "implement", "name": "I", "baseline_marker": True,
             "subject": {"goal": "实现"}},
            {"id": "review", "name": "R", "review": True, "subject": {"goal": "评审"}},
        ],
    }
    root, o = setup(pipe)
    # 评审 diff 基线：完整流程里由 implement entry 记录；非 git 测试目录手动注入
    o.state["baseline_commit:implement"] = "deadbeef"
    FakeRunner.set_answer("subject", {"sid": "sid-impl", "seconds": 1.0})
    FakeRunner.set_answer("review", {"sid": "sid-review", "seconds": 7.0,
                                     "tool_calls": 2, "llm_calls": 1,
                                     "token_input": 30, "token_output": 3})
    orig = with_runner()
    try:
        run_steps(o)
        check(o.state["status"] == "completed", "含 review 节点的流水线完成")
        subj = view(o, "review")["segments"]["subject"]
        check(close(subj["duration_s"], 7.0), "review 主体段 = 评审会话 7s")
        sessions = subj["sessions"]
        check(len(sessions) == 1 and sessions[0]["kind"] == "review",
              "评审会话以 kind=review 记录在 subject 段")
        rec = sessions[0]
        check(rec["session_id"] == "sid-review", "评审会话保留 session id")
        check(rec["tool_calls"] == 2 and rec["llm_calls"] == 1
              and rec["token_total"] == 33, "评审会话指标 2 工具 / 1 LLM / token 33")
        check(subj["tool_calls"] == 2 and subj["token_total"] == 33,
              "subject 段合计只含评审会话")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- T4-3 adversarial 会话

def test_adversarial_session():
    print("[T4-3] adversarial 会话记录在 subject 段，kind=adversarial")
    FakeRunner.reset(start=3000.0)
    pipe = {
        "id": "p", "name": "p", "git_delivery": {"enabled": False},
        "nodes": [{"id": "test", "name": "T", "adversarial": True,
                   "subject": {"goal": "测试"}}],
    }
    root, o = setup(pipe)
    FakeRunner.set_answer("subject", {"sid": "sid-test", "seconds": 2.0,
                                      "tool_calls": 1, "llm_calls": 1,
                                      "token_input": 10, "token_output": 1})
    FakeRunner.set_answer("adversarial", {"sid": "sid-adv", "seconds": 3.0,
                                          "tool_calls": 4, "llm_calls": 2,
                                          "token_input": 20, "token_output": 2})
    orig = with_runner()
    try:
        run_steps(o)
        check(o.state["status"] == "completed", "对抗流水线完成")
        subj = view(o, "test")["segments"]["subject"]
        check(close(subj["duration_s"], 5.0), "subject = 主体 2s + 对抗 3s")
        kinds = [s["kind"] for s in subj["sessions"]]
        check(kinds == ["subject", "adversarial"],
              "主体与对抗会话都在 subject 段，按开始时间排序")
        adv = subj["sessions"][1]
        check(close(adv["duration_s"], 3.0) and adv["tool_calls"] == 4
              and adv["llm_calls"] == 2 and adv["token_total"] == 22,
              "对抗会话指标 3s / 4 工具 / 2 LLM / token 22")
        check(subj["tool_calls"] == 5 and subj["llm_calls"] == 3
              and subj["token_total"] == 33,
              "subject 段合计 = 主体 + 对抗会话")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- T4-4 resume 合并

def test_subject_resume_merge():
    print("[T4-4] 同一 session id 的 subject 续跑合并为一条会话记录")
    FakeRunner.reset(start=4000.0)
    pipe = {
        "id": "p", "name": "p", "git_delivery": {"enabled": False},
        "nodes": [{"id": "a", "name": "A", "exit": {"ai_check": "出口检查"},
                   "subject": {"goal": "实现"}}],
    }
    root, o = setup(pipe)
    FakeRunner.push("subject", {"sid": "subj-1", "seconds": 4.0,
                                "tool_calls": 2, "llm_calls": 1,
                                "token_input": 10, "token_output": 5})
    FakeRunner.push("subject", {"sid": "subj-1", "seconds": 2.0,
                                "tool_calls": 1, "llm_calls": 1,
                                "token_input": 20})
    FakeRunner.push("exit", {"sid": "exit-1", "seconds": 3.0, "text": FAIL})
    FakeRunner.push("exit", {"sid": "exit-2", "seconds": 1.0, "text": PASS})
    orig = with_runner()
    try:
        run_steps(o)
        check(o.state["status"] == "completed", "exit 打回后重跑完成")
        segs = view(o, "a")["segments"]
        check(close(segs["subject"]["duration_s"], 6.0),
              "subject = 首轮 4s + exit 失败后 resume 2s（多区间累计）")
        sessions = segs["subject"]["sessions"]
        check(len(sessions) == 1, "同一 session id 的续跑不拆成多行")
        rec = sessions[0]
        check(rec["kind"] == "subject" and rec["session_id"] == "subj-1",
              "合并记录保留 subject kind 与 session id")
        check(rec["attempts"] == 2, "attempts 累计为 2")
        check(close(rec["duration_s"], 6.0), "会话耗时为两次调用之和 4s+2s")
        check(rec["tool_calls"] == 3 and rec["llm_calls"] == 2
              and rec["token_total"] == 35, "工具/LLM/token 按两次调用累加")
        check(len(segs["exit"]["sessions"]) == 2
              and all(s["kind"] == "exit" for s in segs["exit"]["sessions"]),
              "两次 exit 门禁尝试各记一条 exit 会话")
        check(close(segs["exit"]["duration_s"], 4.0), "exit = 失败 3s + 通过 1s")
        check(close(view(o, "a")["total_s"], 10.0), "total = subject 6s + exit 4s")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


def main():
    test_parallel_task_sessions()
    test_review_session()
    test_adversarial_session()
    test_subject_resume_merge()
    print("ALL NODESTATS SESSIONS PASSED")


if __name__ == "__main__":
    main()
