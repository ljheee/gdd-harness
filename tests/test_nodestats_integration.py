"""节点三段统计的集成验证（T6 / goal.md D1-D6）。

全部用例使用真实 Orchestrator 推进合成任务，只把执行端替换为可控脚本替身：

- INT-D1 entry 故障重试、exit 失败后主体 resume、exit 门禁故障重试：
  entry/主体/exit 多区间累计，人工审批等待与澄清会话不计入，会话按段归属。
- INT-D4 并行开发任务：每个任务会话独立记录（task_id/session_id/耗时/工具/LLM/token），
  主体段为并行墙钟（不等于会话耗时之和）；usage 缺失会话不可得、段级部分不可得。
- INT-D5 自定义最小模板（非 delivery、无 AI 门禁、模板无任何统计字段）：
  节点仍产出三段统计，空门禁段工具/LLM/token 为 0；模板内容与运行实例指纹不变。
- INT-D6 旧 state.json（无 node_stats）：加载、继续推进、控制台任务详情兼容；
  统计生效前后 pipeline 指纹与 .gdd/pipeline.json 不变，旧节点不回填假统计。
- INT-STATS-FAIL 统计采集全抛异常：节点仍按原门禁结果完成，失败留痕。

运行：python3 tests/test_nodestats_integration.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gdd.orchestrator as orch  # noqa: E402
import gdd.ui as ui  # noqa: E402
from gdd import gates, nodestats  # noqa: E402
from gdd.cli import _load_pipeline_file  # noqa: E402
from gdd.orchestrator import Orchestrator  # noqa: E402
from gdd.runner import SessionResult  # noqa: E402

PASS = 'GDD-VERDICT: {"verdict": "pass", "reason": "ok", "evidence": ["e"], "feedback": ""}'
FAIL = 'GDD-VERDICT: {"verdict": "fail", "reason": "no", "evidence": [], "feedback": "fix"}'
PLAN_WITH_TASKS = ("# P\n## 实现步骤\n## 附录：任务依赖\n"
                   "- T1 | - | 甲\n- T2 | - | 乙\n")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok", msg)


def close(a, b, eps=1e-6):
    return abs(float(a) - float(b)) <= eps


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 可控时钟 / 执行端替身

class Clock:
    """线程安全可控时钟；并行任务用 advance_to 只前进（墙钟取 max）。"""

    def __init__(self, start=1000.0):
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self):
        with self._lock:
            return self._t

    def advance_to(self, value):
        with self._lock:
            self._t = max(self._t, float(value))

    def set(self, value):
        with self._lock:
            self._t = float(value)


CLOCK = Clock()


class ScriptedRunner:
    """按 prompt 角色出队的执行端替身；advance_to 模拟并行区间重叠。"""

    barrier = None
    plan: dict = {}
    calls: list = []
    lock = threading.Lock()

    def __init__(self, root):
        self.root = root

    @staticmethod
    def reset(start=1000.0, barrier=None):
        CLOCK.set(start)
        ScriptedRunner.barrier = barrier
        ScriptedRunner.plan = {}
        ScriptedRunner.calls = []

    @staticmethod
    def push(role, spec):
        ScriptedRunner.plan.setdefault(role, []).append(spec)

    @staticmethod
    def set_answer(role, spec):
        ScriptedRunner.plan[role] = [spec]

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
        role = ScriptedRunner._role(prompt)
        tid = ""
        with ScriptedRunner.lock:
            if role == "task":
                tid = prompt.split("[[GDD-TASK:", 1)[1].split("]", 1)[0]
                spec = (ScriptedRunner.plan.get("task") or {}).get(tid)
                if spec is None:
                    raise AssertionError(f"未预期的任务会话 {tid}")
                ScriptedRunner.calls.append(f"task:{tid}")
            else:
                queue = ScriptedRunner.plan.get(role) or []
                if not queue:
                    raise AssertionError(f"未预期的 {role} 会话调用（脚本已耗尽）")
                ScriptedRunner.calls.append(role)
                spec = queue.pop(0)
        started = CLOCK.now()
        seconds = float(spec.get("seconds", 0.0))
        if role == "task" and ScriptedRunner.barrier is not None:
            ScriptedRunner.barrier.wait(timeout=10)
        if seconds:
            CLOCK.advance_to(started + seconds)
        if role == "task":
            _write(os.path.join(self.root, ".gdd", "task-results", f"{tid}.md"),
                   f"{tid}｜完成｜gdd/x.py:1｜ok｜无\n")
        text = spec.get("text")
        if text is None:
            text = PASS if role in ("entry", "exit") else "subject done"
        return SessionResult(
            ok=spec.get("ok", True),
            text=text,
            error=spec.get("error", ""),
            session_id=spec.get("sid", f"{role}-{len(ScriptedRunner.calls)}"),
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
    root = tempfile.mkdtemp(prefix="gdd-ns-int-")
    o = Orchestrator(root, defer_approval=True)
    o.init(pipe, requirement)
    o.load()
    o._stats_now = CLOCK.now
    return root, o


def run_steps(o, limit=40, until=None):
    for _ in range(limit):
        if until is not None and until(o):
            return
        if not o.step():
            return
    raise AssertionError("step 超过上限仍未停止")


def view(o, node_id):
    return nodestats.for_ui(o.state, node_id)


def _no_gate_pipe(nodes):
    return {
        "id": "int-p", "name": "集成流水线", "git_delivery": {"enabled": False},
        "nodes": nodes,
    }


# ---------------------------------------------------------------- INT-D1 段边界与重试累计

def test_d1_segments_retries_and_waits():
    print("[INT-D1] 段边界/重试累计；审批与澄清不计入")
    ScriptedRunner.reset(start=1000.0)
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
        # 节点 pending 窗口内的澄清会话：不计入任何段
        ScriptedRunner.push("clarify", {"sid": "clarify-1", "seconds": 11.0,
                                        "text": "对齐完成"})
        # entry：门禁自身故障 3s + 重试通过 2s
        ScriptedRunner.push("entry", {"sid": "entry-1", "seconds": 3.0,
                                      "ok": False, "error": "entry gate boom",
                                      "tool_calls": 3, "llm_calls": 1,
                                      "token_input": 100, "token_output": 10,
                                      "token_cache_creation": 5, "token_cache_read": 20})
        ScriptedRunner.push("entry", {"sid": "entry-2", "seconds": 2.0,
                                      "tool_calls": 1, "llm_calls": 1,
                                      "token_input": 200, "token_output": 20})
        # 主体首轮 4s；exit 失败后 resume 3s
        ScriptedRunner.push("subject", {"sid": "subj-1", "seconds": 4.0})
        ScriptedRunner.push("subject", {"sid": "subj-1", "seconds": 3.0,
                                        "tool_calls": 2, "llm_calls": 1,
                                        "token_input": 30})
        # exit：失败 6s（usage 不可得）→ 门禁故障 3s → 重试通过 1s
        ScriptedRunner.push("exit", {"sid": "exit-1", "seconds": 6.0, "text": FAIL,
                                     "tool_calls": 2, "llm_calls": 1,
                                     "token_status": "unavailable"})
        ScriptedRunner.push("exit", {"sid": "exit-2", "seconds": 3.0,
                                     "ok": False, "error": "exit gate boom"})
        ScriptedRunner.push("exit", {"sid": "exit-3", "seconds": 1.0,
                                     "tool_calls": 1, "llm_calls": 1,
                                     "token_input": 50, "token_output": 5})

        res = o.chat("a", "先对齐需求")
        check(res.get("ok"), "pending 窗口澄清会话成功")
        check(close(CLOCK.now(), 1011.0), "澄清会话推进了时钟（但不计入段）")

        run_steps(o, until=lambda x: x.state["node_states"]["a"]["phase"] != "pending")
        CLOCK.advance_to(CLOCK.now() + 7.0)  # 步骤间空档：不计入任何段
        run_steps(o, until=lambda x: x.state["status"] == "awaiting_approval")
        check(o.state["status"] == "awaiting_approval", "exit 通过后进入人工审批挂起")
        CLOCK.advance_to(CLOCK.now() + 300.0)  # 人工审批等待：不计入任何段
        check(o.resolve_approval(True), "审批通过")
        check(o.state["status"] == "completed", "任务完成")

        v = view(o, "a")
        seg = v["segments"]
        check(close(seg["entry"]["duration_s"], 5.0), "entry = 故障 3s + 重试通过 2s")
        check(close(seg["subject"]["duration_s"], 7.0), "主体 = 首轮 4s + exit 失败后 resume 3s")
        check(close(seg["exit"]["duration_s"], 10.0), "exit = 失败 6s + 故障 3s + 通过 1s")
        check(close(v["total_s"], 22.0), "total = 三段之和；澄清 11s / 空档 7s / 审批 300s 均不计入")

        entry_sessions = seg["entry"]["sessions"]
        check(len(entry_sessions) == 2 and all(s["kind"] == "entry" for s in entry_sessions),
              "entry 两次门禁尝试各一条记录")
        check(seg["entry"]["tool_calls"] == 4 and seg["entry"]["llm_calls"] == 2
              and seg["entry"]["token_total"] == 355, "entry 段工具/LLM/token 汇总两次尝试")
        exit_sessions = seg["exit"]["sessions"]
        check(len(exit_sessions) == 3 and all(s["kind"] == "exit" for s in exit_sessions),
              "exit 三次尝试各一条记录（含门禁故障重试）")
        check(exit_sessions[0]["token_status"] == "unavailable"
              and exit_sessions[0]["token_total"] is None,
              "usage 缺失的 exit 会话不可得（不以 0 冒充）")
        check(seg["exit"]["token_status"] == "partial" and seg["exit"]["token_total"] == 55,
              "exit 段部分不可得：已知合计 55 并标 partial")
        subj_sessions = seg["subject"]["sessions"]
        check(len(subj_sessions) == 1 and subj_sessions[0]["attempts"] == 2
              and close(subj_sessions[0]["duration_s"], 7.0),
              "同一 sid 的主体续跑合并为一条（attempts=2, 7s）")

        reloaded = Orchestrator(root, defer_approval=True)
        check(reloaded.load(), "完成后新实例可加载 state")
        check(close(view(reloaded, "a")["total_s"], 22.0), "统计随 state.json 落盘")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- INT-D4 并行开发会话

def test_d4_parallel_task_sessions():
    print("[INT-D4] 并行开发任务逐任务统计；usage 缺失降级为不可得")
    ScriptedRunner.reset(start=2000.0, barrier=threading.Barrier(2))
    pipe = {
        "id": "p", "name": "p", "git_delivery": {"enabled": False},
        "nodes": [{"id": "implement", "name": "I", "baseline_marker": True,
                   "entry": {"ai_check": "入口检查"}, "exit": {"ai_check": "出口检查"},
                   "subject": {"goal": "实现"}}],
    }
    root, o = setup(pipe)
    _write(os.path.join(root, "docs", "plan.md"), PLAN_WITH_TASKS)
    ScriptedRunner.push("entry", {"sid": "impl-entry", "seconds": 2.0})
    ScriptedRunner.push("exit", {"sid": "impl-exit", "seconds": 3.0,
                                 "token_input": 7, "token_output": 3})
    ScriptedRunner.plan["task"] = {
        "T1": {"sid": "sid-T1", "seconds": 10.0, "tool_calls": 3, "llm_calls": 2,
               "token_input": 100, "token_output": 10,
               "token_cache_creation": 5, "token_cache_read": 20},
        "T2": {"sid": "sid-T2", "seconds": 6.0, "tool_calls": 1, "llm_calls": 1,
               "token_status": "unavailable"},
    }
    orig = with_runner()
    try:
        run_steps(o, limit=50)
        check(o.state["status"] == "completed", "并行任务流水线完成")
        v = view(o, "implement")
        subj = v["segments"]["subject"]
        check(close(subj["duration_s"], 10.0),
              "主体段 = 并行墙钟 max(10s,6s)，不是会话耗时之和 16s")
        sessions = subj["sessions"]
        check(len(sessions) == 2 and all(s["kind"] == "task" for s in sessions),
              "两个并行任务各生成一条 kind=task 会话")
        by_task = {s["task_id"]: s for s in sessions}
        check(set(by_task) == {"T1", "T2"}, "会话按任务 ID 可区分")
        t1, t2 = by_task["T1"], by_task["T2"]
        check(t1["session_id"] == "sid-T1" and t2["session_id"] == "sid-T2"
              and close(t1["duration_s"], 10.0) and close(t2["duration_s"], 6.0),
              "各任务独立 session_id 与耗时")
        check(t1["tool_calls"] == 3 and t1["llm_calls"] == 2
              and t1["token_total"] == 135 and t1["token_status"] == "available",
              "T1 指标：工具 3 / LLM 2 / token 135")
        check(t2["token_status"] == "unavailable"
              and t2["token_total"] is None and t2["llm_calls"] == 1,
              "T2 有模型响应但 usage 缺失：会话 token 不可得且不显示 0")
        check(subj["tool_calls"] == 4 and subj["llm_calls"] == 3,
              "主体段工具/LLM 合计 = 各会话之和")
        check(subj["token_status"] == "partial" and subj["token_total"] == 135,
              "主体段部分不可得：已知合计 135 并标 partial")
        segs = v["segments"]
        check(close(segs["entry"]["duration_s"], 2.0) and close(segs["exit"]["duration_s"], 3.0),
              "entry/exit 只含各自门禁窗口")
        check(close(v["total_s"], 15.0), "total = entry 2s + 主体 10s + exit 3s")
        check(segs["exit"]["token_total"] == 10, "exit 门禁 token 四项求和（7+3）")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- INT-D5 自定义最小模板

def test_d5_custom_template():
    print("[INT-D5] 自定义最小模板（无 AI 门禁、无统计字段）自动获得三段统计")
    ScriptedRunner.reset(start=3000.0)
    workdir = tempfile.mkdtemp(prefix="gdd-ns-int-tpl-")
    tpl_path = os.path.join(workdir, "custom-min.json")
    tpl = {
        "id": "custom-min", "name": "自定义最小模板", "git_delivery": {"enabled": False},
        "nodes": [
            {"id": "a", "name": "A", "subject": {"goal": "做 A"}},
            {"id": "b", "name": "B", "entry": {}, "exit": {}, "subject": {"goal": "做 B"}},
        ],
    }
    original_text = json.dumps(tpl, ensure_ascii=False, indent=2)
    _write(tpl_path, original_text)
    loaded = _load_pipeline_file(tpl_path)  # 走 CLI 的自定义模板文件加载路径
    check(loaded == tpl, "自定义模板文件原样加载（无需任何统计字段）")

    task_root = os.path.join(workdir, "task-custom")
    os.makedirs(task_root)
    o = Orchestrator(task_root, defer_approval=True)
    o.init(loaded, "自定义模板集成")
    o.load()
    o._stats_now = CLOCK.now
    fp_before = o._pipeline_fingerprint()
    instance_before = _read(o.pipeline_path)
    ScriptedRunner.set_answer("subject", {"sid": "s-a", "seconds": 5.0,
                                          "tool_calls": 2, "llm_calls": 1,
                                          "token_input": 11, "token_output": 1})
    ScriptedRunner.push("subject", {"sid": "s-b", "seconds": 7.0,
                                    "tool_calls": 1, "llm_calls": 1,
                                    "token_input": 20})
    orig = with_runner()
    orig_machine = gates.run_machine_checks

    def slow_machine(gate, root_path):
        # 机器检查真实耗时也应计入 entry/exit 段：用可控时钟让每次检查花 2s
        CLOCK.advance_to(CLOCK.now() + 2.0)
        return orig_machine(gate, root_path)

    gates.run_machine_checks = slow_machine
    try:
        run_steps(o, limit=30)
        check(o.state["status"] == "completed", "自定义模板流水线完成")
        for node_id, subj_secs in (("a", 5.0), ("b", 7.0)):
            v = view(o, node_id)
            check(v is not None, f"节点 {node_id} 自动获得统计")
            seg = v["segments"]
            check(set(seg) == {"entry", "subject", "exit"}, f"{node_id} 含 entry/主体/exit 三段")
            check(close(seg["entry"]["duration_s"], 2.0)
                  and close(seg["exit"]["duration_s"], 2.0),
                  f"{node_id} 空门禁段耗时为机器检查耗时（各 2s）")
            check(seg["entry"]["tool_calls"] == 0 and seg["entry"]["llm_calls"] == 0
                  and seg["entry"]["token_total"] == 0
                  and seg["entry"]["token_status"] == "available",
                  f"{node_id} 无 AI 门禁：段工具/LLM/token 为真实 0")
            check(close(seg["subject"]["duration_s"], subj_secs)
                  and close(v["total_s"], subj_secs + 4.0),
                  f"{node_id} 主体/总耗时含主体与机器检查活动区间")
            check(len(seg["subject"]["sessions"]) == 1
                  and seg["subject"]["sessions"][0]["kind"] == "subject",
                  f"{node_id} 主体会话已记录")

        check(_read(tpl_path) == original_text, "模板文件运行前后逐字符不变（未写入统计字段）")
        check("stats" not in json.dumps(o.pipeline.to_dict(), ensure_ascii=False),
              "运行实例 NodeSpec 序列化不含统计字段")
        check(o._pipeline_fingerprint() == fp_before
              and o.state["pipeline_fingerprint"] == fp_before,
              "统计生效前后运行实例指纹不变")
        check(_read(o.pipeline_path) == instance_before,
              ".gdd/pipeline.json 内容不变（不触发指纹迁移）")
    finally:
        gates.run_machine_checks = orig_machine
        restore_runner(orig)
        shutil.rmtree(workdir, ignore_errors=True)
    print()


# ---------------------------------------------------------------- INT-D6 旧 state 兼容

def test_d6_old_state_compat():
    print("[INT-D6] 旧 state.json（无 node_stats）加载/推进/控制台兼容，指纹不变")
    ScriptedRunner.reset(start=4000.0)
    pipe = _no_gate_pipe([
        {"id": "a", "name": "A", "subject": {"goal": "做 A"}},
        {"id": "b", "name": "B", "subject": {"goal": "做 B"}},
    ])
    root, o = setup(pipe)
    ScriptedRunner.set_answer("subject", {"sid": "s-a", "seconds": 5.0})
    ScriptedRunner.push("subject", {"sid": "s-b", "seconds": 6.0})
    orig = with_runner()
    try:
        fp_before = o._pipeline_fingerprint()
        instance_before = _read(o.pipeline_path)
        run_steps(o, until=lambda x: (x.state.get("current") == "b"
                                      and x.state["node_states"]["a"]["phase"] == "done"))
        check(o.state["node_states"]["a"]["phase"] == "done"
              and o.state["current"] == "b", "节点 a 已完成，当前推进到 b")
        check(view(o, "a") is not None, "新版本运行节点 a 时已产生统计")

        # 模拟旧版本写下的 state.json：整体删除 node_stats（旧任务没有统计字段）
        raw = json.loads(_read(o.state_path))
        raw.pop("node_stats", None)
        _write(o.state_path, json.dumps(raw, ensure_ascii=False, indent=2))

        old = Orchestrator(root, defer_approval=True)
        check(old.load(), "不含 node_stats 的旧 state.json 加载成功")
        check(nodestats.for_ui(old.state, "a") is None and nodestats.for_ui(old.state, "b") is None,
              "旧任务无统计时不虚报")
        detail = ui._task_detail(os.path.dirname(root), os.path.basename(root))
        json.dumps(detail)  # 控制台任务详情必须可序列化（无统计不崩溃）
        nodes = {n["id"]: n for n in detail["nodes"]}
        check(nodes["a"]["phase"] == "done" and nodes["a"]["stats"] is None,
              "控制台正常展示旧任务已完成节点，stats 为 None")
        check(nodes["b"]["phase"] == "pending" and nodes["b"]["stats"] is None,
              "旧任务未完成节点正常展示，stats 为 None")

        old._stats_now = CLOCK.now
        run_steps(old, limit=20)
        check(old.state["status"] == "completed", "旧 state 继续推进到完成")
        check(nodestats.for_ui(old.state, "a") is None,
              "旧节点不被回填假统计（本次不要求历史回填）")
        vb = nodestats.for_ui(old.state, "b")
        check(vb is not None and close(vb["segments"]["subject"]["duration_s"], 6.0),
              "旧 state 加载后新推进的节点 b 正常产生统计 6s")
        check(old._pipeline_fingerprint() == fp_before
              and old.state["pipeline_fingerprint"] == fp_before,
              "旧 state 推进后运行实例指纹不变")
        check(_read(old.pipeline_path) == instance_before,
              ".gdd/pipeline.json 内容不变")
    finally:
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


# ---------------------------------------------------------------- INT-STATS-FAIL 统计失败隔离

def test_stats_failure_is_isolated():
    print("[INT-STATS-FAIL] 统计采集全抛异常时节点仍按门禁结果推进")
    ScriptedRunner.reset(start=5000.0)
    pipe = _no_gate_pipe([{"id": "a", "name": "A", "subject": {"goal": "做 A"}}])
    root, o = setup(pipe)
    ScriptedRunner.set_answer("subject", {"sid": "s-a", "seconds": 2.0})
    orig = with_runner()
    saved = (nodestats.open_segment, nodestats.close_segment, nodestats.add_session)

    def boom(*_a, **_k):
        raise RuntimeError("stats exploded")

    nodestats.open_segment = boom
    nodestats.close_segment = boom
    nodestats.add_session = boom
    try:
        run_steps(o, limit=20)
        check(o.state["status"] == "completed", "统计全失败时节点仍按门禁结果完成")
        events = [h.get("event") for h in o.state["history"]]
        check("node_stats_error" in events, "统计失败以 node_stats_error 事件留痕")
        check(nodestats.for_ui(o.state, "a") is None, "无有效统计时不虚报节点统计")
        detail = ui._task_detail(os.path.dirname(root), os.path.basename(root))
        check(detail["nodes"][0]["stats"] is None, "统计失败任务在控制台按无统计展示")
    finally:
        nodestats.open_segment, nodestats.close_segment, nodestats.add_session = saved
        restore_runner(orig)
        shutil.rmtree(root, ignore_errors=True)
    print()


def main():
    test_d1_segments_retries_and_waits()
    test_d4_parallel_task_sessions()
    test_d5_custom_template()
    test_d6_old_state_compat()
    test_stats_failure_is_isolated()
    print("ALL NODESTATS INTEGRATION PASSED")


if __name__ == "__main__":
    main()
