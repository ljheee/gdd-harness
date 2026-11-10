"""任务依赖附录：解析，以及开发节点按拓扑并行。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.orchestrator import Orchestrator  # noqa: E402
from gdd.runner import SessionResult  # noqa: E402
from gdd.taskgraph import parse  # noqa: E402

PLAN = """# 方案
## 实现步骤
## 附录：任务依赖
- T1 | - | 先做甲
- T2 | - | 先做乙
- T3 | T1, T2 | 等甲和乙都完成
"""


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def test_parse():
    print("[P] 附录解析")
    graph = parse(PLAN)
    check(not graph.error, "三行任务能解析")
    check([t.id for t in graph.ready(set())] == ["T1", "T2"], "没有依赖的任务先就绪")
    check([t.id for t in graph.ready({"T1", "T2"})] == ["T3"], "依赖都完成后 T3 才就绪")
    cycle = parse("## 附录：任务依赖\n- A | B | 甲\n- B | A | 乙\n")
    check("成环" in cycle.error, "成环会被指出来")
    missing = parse("## 附录：任务依赖\n- A | Z | 甲\n")
    check("没有的任务" in missing.error, "依赖必须指向附录里的任务")
    plain = parse("# 方案\n## 实现步骤\n没有附录\n")
    check(plain.error, "没有任务行时不能当成空图放行")
    print("[P] PASS\n")


class ParRunner:
    events = []
    lock = threading.Lock()
    fail = ""

    def __init__(self, root):
        self.root = root

    def run(self, prompt, *, allowed_tools, max_turns, log_path=None, resume_sid="", on_stream=None):
        marker = "[[GDD-TASK:"
        if marker not in prompt:
            return SessionResult(ok=True, text="gate", session_id="g", turns=1)
        tid = prompt.split(marker, 1)[1].split("]", 1)[0]
        start = time.time()
        with ParRunner.lock:
            ParRunner.events.append((tid, "start", start))
        time.sleep(0.3)
        end = time.time()
        with ParRunner.lock:
            ParRunner.events.append((tid, "end", end))
        if tid == ParRunner.fail:
            return SessionResult(ok=False, error="boom", session_id="s-" + tid, turns=1)
        folder = os.path.join(self.root, ".gdd", "task-results")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, tid + ".md"), "w", encoding="utf-8") as f:
            f.write(f"{tid}｜已完成｜demo.py:1｜ok｜无\n")
        return SessionResult(ok=True, text="done " + tid, session_id="s-" + tid, turns=1)


def _orch(plan, fail="", with_plan=False):
    root = tempfile.mkdtemp(prefix="gdd-task-")
    os.makedirs(os.path.join(root, "docs"))
    with open(os.path.join(root, "docs", "plan.md"), "w", encoding="utf-8") as f:
        f.write(plan)
    nodes = []
    if with_plan:
        nodes.append({"id": "plan", "name": "方案设计"})
    nodes.append({"id": "implement", "name": "开发实现", "baseline_marker": True})
    pipe = {
        "id": "p", "name": "p",
        "git_delivery": {"enabled": False},
        "nodes": nodes,
    }
    import gdd.orchestrator as orch
    orig = orch.AgentRunner
    orch.AgentRunner = ParRunner
    ParRunner.events = []
    ParRunner.fail = fail
    o = Orchestrator(root)
    o.init(pipe, "r")
    o.load()
    return o, orig, root


def test_parallel_waves():
    print("[W] 无依赖的任务并行，依赖完成后才开始")
    o, orig, root = _orch(PLAN)
    import gdd.orchestrator as orch
    try:
        check(o.step() is True, "进入开发")
        check(o.state["node_states"]["implement"]["phase"] == "working", "入口过后开始开发")
        ParRunner.events = []
        check(o.step() is True, "第一批跑完")
        names = [item[0] for item in ParRunner.events if item[1] == "start"]
        check(names == ["T1", "T2"] or names == ["T2", "T1"], "第一批只有 T1 和 T2")
        span = {}
        for tid, kind, ts in ParRunner.events:
            span.setdefault(tid, {})[kind] = ts
        check(span["T1"]["start"] < span["T2"]["end"] and span["T2"]["start"] < span["T1"]["end"],
              "T1 和 T2 的执行时间重叠")
        check("T3" not in span, "T3 没有提前开始")
        check(set(o.state["node_states"]["implement"]["tasks_done"]) == {"T1", "T2"},
              "完成的任务记下来")
        before = max(span["T1"]["end"], span["T2"]["end"])
        check(o.step() is True, "依赖完成后跑 T3")
        t3 = [ts for tid, kind, ts in ParRunner.events if tid == "T3" and kind == "start"]
        check(t3 and t3[0] >= before, "T3 在甲乙都结束后才开始")
        ledger = open(os.path.join(root, "docs", "impl-ledger.md"), encoding="utf-8").read()
        check("## 核销台账" in ledger and "T3｜已完成" in ledger, "三行核销合成台账")
    finally:
        orch.AgentRunner = orig
    print("[W] PASS\n")


def test_failed_task_does_not_unblock_dependents():
    print("[F] 失败的任务不会放开它的下游")
    o, orig, root = _orch(PLAN, fail="T2")
    import gdd.orchestrator as orch
    try:
        check(o.step() is True, "先过入口")
        check(o.state["node_states"]["implement"]["phase"] == "working", "停在开发中")
        ParRunner.events = []
        check(o.step() is True, "第一批有失败仍返回继续")
        done = set(o.state["node_states"]["implement"].get("tasks_done") or [])
        check(done == {"T1"}, "只有成功的 T1 记完成")
        check(not any(tid == "T3" for tid, _, _ in ParRunner.events), "T3 不开始")
        check(o.state["status"] == "running", "一次失败先重试，不立刻阻塞")
    finally:
        orch.AgentRunner = orig
    print("[F] PASS\n")


def test_cycle_returns_to_plan():
    print("[C] 成环退回方案")
    bad = "## 附录：任务依赖\n- A | B | 甲\n- B | A | 乙\n"
    o, orig, _root = _orch(bad, with_plan=True)
    import gdd.orchestrator as orch
    try:
        seen = False
        for _ in range(8):
            if any(h.get("event") == "task_graph_invalid" for h in o.state.get("history") or []):
                seen = True
                break
            o.step()
        check(seen, "成环被记下来")
        check(o.state["current"] == "plan", "依赖成环时退回方案节点")
    finally:
        orch.AgentRunner = orig
    print("[C] PASS\n")


if __name__ == "__main__":
    test_parse()
    test_parallel_waves()
    test_failed_task_does_not_unblock_dependents()
    test_cycle_returns_to_plan()
    print("ALL PASS")
