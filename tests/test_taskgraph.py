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
    prose = parse("## 附录：任务依赖\n\n以下按依赖拆分：\n\n- T1 | - | 窗口\n1. T2 | T1 | 接入\n")
    check(not prose.error and [t.id for t in prose.tasks] == ["T1", "T2"], "说明和编号不拆掉任务")
    wide = parse("## 附录：任务依赖\n- T1 ｜ - ｜ 窗口\n")
    check(not wide.error and wide.tasks[0].id == "T1", "全角竖线也能解析")
    bad_row = parse("## 附录：任务依赖\n- T1 | ??? | 窗口\n")
    check(bad_row.error, "有竖线但写坏的行仍然拒绝")
    later = parse(
        "## 附录：任务依赖\n- EX | - | 示例\n\n## ADR\n"
        "## 附录：任务依赖\n- T1 | - | 真正的任务\n")
    check(not later.error and [t.id for t in later.tasks] == ["T1"], "只采用最后一节附录")
    print("[P] PASS\n")


class ParRunner:
    events = []
    resumes = []
    lock = threading.Lock()
    fail = ""
    exhaust_left = 0
    skip_note = ""
    grade_on_steer = False

    def __init__(self, root):
        self.root = root

    def run(self, prompt, *, allowed_tools, max_turns, log_path=None, resume_sid="", on_stream=None):
        if ParRunner.grade_on_steer and "用户在主体执行过程中补充了信息" in prompt:
            return SessionResult(
                ok=True, session_id="steer", turns=1,
                text='GDD-GRADE: {"level":"L0","clause":"接口","fact":"改成异步","basis":"已确认"}')
        marker = "[[GDD-TASK:"
        if marker not in prompt:
            return SessionResult(ok=True, text="gate", session_id="g", turns=1)
        tid = prompt.split(marker, 1)[1].split("]", 1)[0]
        if resume_sid:
            with ParRunner.lock:
                ParRunner.resumes.append((tid, resume_sid))
        start = time.time()
        with ParRunner.lock:
            ParRunner.events.append((tid, "start", start))
        time.sleep(0.3)
        end = time.time()
        with ParRunner.lock:
            ParRunner.events.append((tid, "end", end))
        if tid == ParRunner.fail:
            return SessionResult(ok=False, error="boom", session_id="s-" + tid, turns=1)
        if ParRunner.exhaust_left > 0:
            ParRunner.exhaust_left -= 1
            return SessionResult(ok=False, error="turns", turns_exhausted=True,
                                 session_id="s-" + tid, turns=80)
        if tid == ParRunner.skip_note:
            return SessionResult(ok=True, text="no note", session_id="s-" + tid, turns=1)
        folder = os.path.join(self.root, ".gdd", "task-results")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, tid + ".md"), "w", encoding="utf-8") as f:
            f.write(f"{tid}｜已完成｜demo.py:1｜ok｜无\n")
        return SessionResult(ok=True, text="done " + tid, session_id="s-" + tid, turns=1)


def _orch(plan, fail="", with_plan=False, clarify=False):
    root = tempfile.mkdtemp(prefix="gdd-task-")
    os.makedirs(os.path.join(root, "docs"))
    with open(os.path.join(root, "docs", "plan.md"), "w", encoding="utf-8") as f:
        f.write(plan)
    nodes = []
    if clarify:
        nodes.append({"id": "clarify", "name": "需求澄清"})
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
    ParRunner.resumes = []
    ParRunner.fail = fail
    ParRunner.exhaust_left = 0
    ParRunner.skip_note = ""
    ParRunner.grade_on_steer = False
    o = Orchestrator(root)
    o.init(pipe, "r")
    o.load()
    return o, orig, root


def test_downstream_closure():
    print("[C] 下游传递闭包")
    graph = parse("""## 附录：任务依赖
- T1 | - | 甲
- T2 | - | 乙
- T3 | T1 | 依赖甲
- T4 | T3 | 依赖丙
- T5 | T2 | 依赖乙
- T6 | T1, T5 | 菱形汇合
""")
    check(not graph.error, "图可解析")
    check(graph.downstream({"T1"}) == {"T1", "T3", "T4", "T6"}, "链式 + 汇合的下游")
    check(graph.downstream({"T2"}) == {"T2", "T5", "T6"}, "另一条链的下游")
    check(graph.downstream({"T1", "T2"}) == {"T1", "T2", "T3", "T4", "T5", "T6"},
          "多起点并集")
    check(graph.downstream({"T4"}) == {"T4"}, "叶子任务只含自己")
    check(graph.downstream({"NOPE"}) == {"NOPE"}, "未知 ID 不扩散")
    print("[C] PASS\n")


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


def test_success_clears_gate_streak_and_resume_continues():
    print("[S] 成功清掉故障计数，打满后续跑")
    o, orig, _root = _orch(PLAN, fail="T2")
    import gdd.orchestrator as orch
    try:
        o.step()
        check(o.step() is True, "第一批里有失败")
        check(o.state.get("gate_error_streak") == 1, "失败记了一次")
        ParRunner.fail = ""
        check(o.step() is True, "失败的任务重试成功")
        check(not o.state.get("gate_error_streak"), "成功后故障计数清零")
    finally:
        orch.AgentRunner = orig

    one = "## 附录：任务依赖\n- T1 | - | 单独一项\n"
    o, orig, _root = _orch(one)
    try:
        ParRunner.exhaust_left = 1
        o.step()
        check(o.step() is True, "回合打满先续跑")
        ns = o.state["node_states"]["implement"]
        check("T1" not in set(ns.get("tasks_done") or []), "打满不算做完")
        check(not o.state.get("gate_error_streak"), "续跑不计入连续故障")
        check(ns.get("task_continues", {}).get("T1") == 1, "记下这一次续跑")
        check(o.step() is True, "续跑那一轮做完")
        check(ParRunner.resumes and ParRunner.resumes[0][0] == "T1", "续跑复用了任务会话")
        check("T1" in set(o.state["node_states"]["implement"].get("tasks_done") or []),
              "续跑成功才记完成")
    finally:
        orch.AgentRunner = orig
    print("[S] PASS\n")


def test_missing_note_is_not_done():
    print("[N] 没有核销行不算完成")
    one = "## 附录：任务依赖\n- T1 | - | 单独一项\n"
    o, orig, root = _orch(one)
    import gdd.orchestrator as orch
    try:
        ParRunner.skip_note = "T1"
        o.step()
        check(o.step() is True, "会话成功但没有核销行")
        done = set(o.state["node_states"]["implement"].get("tasks_done") or [])
        check("T1" not in done, "没有记成完成")
        ledger = os.path.join(root, "docs", "impl-ledger.md")
        text = open(ledger, encoding="utf-8").read() if os.path.isfile(ledger) else ""
        check("已完成" not in text, "台账没有写成已完成")
    finally:
        orch.AgentRunner = orig
    print("[N] PASS\n")


def test_rewritten_task_runs_again():
    print("[D] 附录改写后同一个 ID 要重做")
    o, orig, root = _orch(PLAN, with_plan=True)
    import gdd.orchestrator as orch
    try:
        for _ in range(6):
            if (o.state.get("current") == "implement"
                    and o.state["node_states"]["implement"]["phase"] == "working"):
                break
            o.step()
        check(o.state["node_states"]["implement"]["phase"] == "working", "开发已开始")
        check(o.step() is True, "先做完第一批")
        check("T1" in set(o.state["node_states"]["implement"]["tasks_done"]), "T1 已完成")
        with open(os.path.join(root, "docs", "plan.md"), "w", encoding="utf-8") as f:
            f.write("## 附录：任务依赖\n- A | B | 甲\n- B | A | 乙\n")
        ParRunner.events = []
        check(o.step() is True, "成环退回方案")
        check(o.state["current"] == "plan", "回到方案")
        check(not o.state["node_states"]["implement"].get("tasks_done"), "旧的完成记录清掉")
    finally:
        orch.AgentRunner = orig
    print("[D] PASS\n")


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


def test_stale_note_does_not_finish_a_rewritten_task():
    print("[R] 旧核销文件不能把改写后的任务标成完成")
    o, orig, root = _orch(PLAN)
    import gdd.orchestrator as orch
    try:
        o.step()
        check(o.step() is True, "先做完第一批")
        note = os.path.join(root, ".gdd", "task-results", "T1.md")
        check(os.path.isfile(note), "T1 有核销文件")
        with open(os.path.join(root, "docs", "plan.md"), "w", encoding="utf-8") as f:
            f.write(PLAN.replace("先做甲", "新标题必须重做"))
        ParRunner.skip_note = "T1"
        ParRunner.events = []
        check(o.step() is True, "改写后的 T1 会再跑")
        done = set(o.state["node_states"]["implement"].get("tasks_done") or [])
        check("T1" not in done, "没有新核销行就不算完成")
        check(not any(tid == "T3" for tid, _, _ in ParRunner.events), "T3 不会被旧核销放开")
    finally:
        orch.AgentRunner = orig
    print("[R] PASS\n")


def test_revision_clears_finished_tasks():
    print("[V] 定级修订后开发任务要重做")
    one = "## 附录：任务依赖\n- T1 | - | 实现接口\n"
    o, orig, root = _orch(one, clarify=True)
    import gdd.orchestrator as orch
    try:
        for _ in range(8):
            if "T1" in set(o.state["node_states"]["implement"].get("tasks_done") or []):
                break
            o.step()
        check("T1" in set(o.state["node_states"]["implement"].get("tasks_done") or []),
              "修订前 T1 已完成")
        note = os.path.join(root, ".gdd", "task-results", "T1.md")
        check(os.path.isfile(note), "修订前核销文件还在")
        check(o._start_revision("implement", reason="L0", confirmed=["L0|接口|改成异步"]) is True,
              "修订能启动")
        ns = o.state["node_states"]["implement"]
        check(not ns.get("tasks_done"), "完成记录清掉")
        check(ns.get("phase") == "pending", "开发阶段回到待进入")
        check(not os.path.isfile(note), "旧核销文件删掉")
    finally:
        orch.AgentRunner = orig
    print("[V] PASS\n")


def test_invalid_appendix_eventually_blocks():
    print("[L] 附录一直不可用时会停住，而不是来回打回")
    bad = "## 附录：任务依赖\n这里只有说明。\n## 后面\n- T1 | - | 真任务\n"
    o, orig, _root = _orch(bad, with_plan=True)
    import gdd.orchestrator as orch
    try:
        blocked = False
        for _ in range(24):
            if o.state["status"] == "blocked":
                blocked = True
                break
            o.step()
        check(blocked, "连续无法排期后停住")
        check("任务依赖" in (o.state.get("blocked_reason") or ""), "停住原因是任务依赖")
        check(o.state["node_states"]["implement"].get("task_graph_rejects", 0) >= 3,
              "计数挂在开发节点上")
    finally:
        orch.AgentRunner = orig
    print("[L] PASS\n")


def test_failed_task_session_is_not_resumed():
    print("[E] 失败的任务会话下一次重新开始")
    one = "## 附录：任务依赖\n- T1 | - | 单独一项\n"
    o, orig, _root = _orch(one, fail="T1")
    import gdd.orchestrator as orch
    try:
        o.step()
        check(o.step() is True, "第一次失败先重试")
        sessions = o.state["node_states"]["implement"].get("sessions") or {}
        check("task:T1" not in sessions, "失败会话不留下可续跑的 id")
        ParRunner.fail = ""
        ParRunner.resumes = []
        check(o.step() is True, "下一次做完")
        check(not ParRunner.resumes, "没有续跑上次失败的会话")
        check("T1" in set(o.state["node_states"]["implement"].get("tasks_done") or []),
              "新会话成功才记完成")
    finally:
        orch.AgentRunner = orig
    print("[E] PASS\n")


def test_ok_without_note_clears_fault_streak():
    print("[K] 会话成功但没写核销，不算一次故障")
    one = "## 附录：任务依赖\n- T1 | - | 单独一项\n"
    o, orig, _root = _orch(one, fail="T1")
    import gdd.orchestrator as orch
    try:
        o.step()
        check(o.step() is True, "先记一次会话故障")
        check(o.state.get("gate_error_streak") == 1, "故障计数是 1")
        ParRunner.fail = ""
        ParRunner.skip_note = "T1"
        check(o.step() is True, "接着一次成功但没有核销")
        check(not o.state.get("gate_error_streak"), "这次把故障计数清掉")
        ParRunner.skip_note = ""
        ParRunner.fail = "T1"
        check(o.step() is True, "再失败一次")
        check(o.state.get("gate_error_streak") == 1, "重新从 1 计数")
        check(o.state["status"] != "blocked", "不会因为中间那次成功被判成连续故障")
    finally:
        orch.AgentRunner = orig
    print("[K] PASS\n")


def test_grade_in_steer_fallback_pauses():
    print("[G] 补充会话里的定级也会停下等人确认")
    one = "## 附录：任务依赖\n- T1 | - | 实现接口\n"
    o, orig, root = _orch(one)
    import gdd.orchestrator as orch
    from gdd import steer
    old_steer = os.environ.get("GDD_STEER")
    old_resume = os.environ.get("GDD_SUBJECT_RESUME")
    os.environ["GDD_STEER"] = "on"
    os.environ["GDD_SUBJECT_RESUME"] = "on"
    try:
        ParRunner.grade_on_steer = True
        steer.append(root, "请确认改成异步")
        check(o.step() is True, "先过入口")
        check(o.step() is False, "补充会话给出 L0 后停下")
        check(o.state["status"] == "awaiting_approval", "等确认")
        check((o.state.get("pending_approval") or {}).get("kind") == "grade", "等的是定级")
    finally:
        orch.AgentRunner = orig
        if old_steer is None:
            os.environ.pop("GDD_STEER", None)
        else:
            os.environ["GDD_STEER"] = old_steer
        if old_resume is None:
            os.environ.pop("GDD_SUBJECT_RESUME", None)
        else:
            os.environ["GDD_SUBJECT_RESUME"] = old_resume
    print("[G] PASS\n")


if __name__ == "__main__":
    test_parse()
    test_downstream_closure()
    test_parallel_waves()
    test_failed_task_does_not_unblock_dependents()
    test_success_clears_gate_streak_and_resume_continues()
    test_missing_note_is_not_done()
    test_rewritten_task_runs_again()
    test_cycle_returns_to_plan()
    test_stale_note_does_not_finish_a_rewritten_task()
    test_revision_clears_finished_tasks()
    test_invalid_appendix_eventually_blocks()
    test_failed_task_session_is_not_resumed()
    test_ok_without_note_clears_fault_streak()
    test_grade_in_steer_fallback_pauses()
    print("ALL PASS")
