"""功能 1 集成：方案关联校验、按任务打回、非 code 分流、命名迭代路径、单次审批。"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.iteration import prepare as prepare_iteration  # noqa: E402
from gdd.models import PipelineSpec  # noqa: E402
from gdd.orchestrator import Orchestrator  # noqa: E402
from gdd.runner import SessionResult  # noqa: E402

PASS = 'GDD-VERDICT: {"verdict": "pass", "reason": "ok", "evidence": ["e"], "feedback": ""}'

PIPELINE = {
    "id": "routing",
    "name": "routing",
    "git_delivery": {"enabled": False},
    "nodes": [
        {"id": "plan", "name": "方案", "subject": {"goal": "出方案"},
         "exit": {"goal": "方案产出", "artifacts": [
             {"path": "docs/plan.md", "must_contain": "## 实现步骤"},
             {"path": "docs/acceptance.feature", "must_contain": "Feature:"},
         ]}},
        {"id": "implement", "name": "开发实现", "description": "实现",
         "baseline_marker": True, "subject": {"goal": "实现"}},
        {"id": "test", "name": "测试", "adversarial": True, "subject": {"goal": "测试"}},
    ],
}


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _new_orch(pipeline=None, name=""):
    d = tempfile.mkdtemp(prefix="gdd-routing-")
    pd = json.loads(json.dumps(pipeline or PIPELINE))
    if name:
        pd = prepare_iteration(pd, d, name=name)
    o = Orchestrator(d)
    o.init(pd, "需求")
    o.load()
    return d, o


class FakeRunner:
    """不启动真实 pi 的会话替身；任务会话按 [[GDD-TASK:ID]] 写核销行。"""
    started: list = []
    responses: dict = {}
    write_notes = True
    prompts: list = []

    def __init__(self, root):
        self.root = root

    def run(self, prompt, *, allowed_tools, max_turns, log_path=None,
            resume_sid="", on_stream=None):
        FakeRunner.prompts.append(prompt)
        if "[[GDD-TASK:" in prompt:
            tid = prompt.split("[[GDD-TASK:", 1)[1].split("]", 1)[0]
            FakeRunner.started.append(tid)
            if FakeRunner.write_notes:
                _write(os.path.join(self.root, ".gdd", "task-results", f"{tid}.md"),
                       f"{tid}｜完成｜gdd/x.py:1｜ok｜无\n")
            return SessionResult(ok=True, text="task done", session_id=f"sid-{tid}",
                                 turns=1)
        for key, text in FakeRunner.responses.items():
            if key in prompt:
                return SessionResult(ok=True, text=text, session_id=f"sid-{abs(hash(key))}",
                                     turns=1)
        return SessionResult(ok=True, text=PASS, session_id="sid-gate", turns=1)


def _reset_fake():
    FakeRunner.started = []
    FakeRunner.responses = {}
    FakeRunner.write_notes = True
    FakeRunner.prompts = []


def test_plan_exit_link_validation():
    print("[B2-1] 方案出口关联校验（D2）")
    plan = "# P\n## 实现步骤\n## 附录：任务依赖\n- T1 | - | 甲\n- T2 | - | 乙\n"
    cases = {
        "缺 tag": "Feature: x\n  Scenario: 主路径\n    Given 就绪\n",
        "未知任务": "Feature: x\n  @task:T9\n  Scenario: 主路径\n    Given 就绪\n",
        "没有场景": "Feature: x\n  Given 就绪\n",
    }
    for label, text in cases.items():
        _reset_fake()
        d, o = _new_orch()
        try:
            _write(os.path.join(d, "docs", "plan.md"), plan)
            _write(os.path.join(d, "docs", "acceptance.feature"), text)
            o.state["current"] = "plan"
            node = o.pipeline.node("plan")
            ns = o.state["node_states"]["plan"]
            ns["phase"] = "working"
            before = json.dumps(o.state, sort_keys=True)
            o._run_exit_gate(node, ns, FakeRunner(d))
            check(o.state["current"] == "plan", f"{label}: 节点未推进")
            check(ns["phase"] == "working" and ns.get("bounces") == 1,
                  f"{label}: 打回主体")
            fb = o.state["feedbacks"].get("plan", "")
            check("关联校验未通过" in fb, f"{label}: 反馈说明关联失败")
            check(not FakeRunner.started, f"{label}: 未启动 AI 门禁会话")
            if label == "缺 tag":
                check("主路径" in fb, "反馈列出具体 Scenario")
            if label == "未知任务":
                check("T9" in fb, "反馈列出非法任务 ID")
            check(json.dumps(o.state, sort_keys=True) != before, "状态已记录打回")
        finally:
            import shutil
            shutil.rmtree(d)

    _reset_fake()
    d, o = _new_orch()
    try:
        _write(os.path.join(d, "docs", "plan.md"), plan)
        _write(os.path.join(d, "docs", "acceptance.feature"),
               "Feature: x\n  @task:T1 @task:T2\n  Scenario: 主路径\n    Given 就绪\n")
        o.state["current"] = "plan"
        node = o.pipeline.node("plan")
        ns = o.state["node_states"]["plan"]
        ns["phase"] = "working"
        o._run_exit_gate(node, ns, FakeRunner(d))
        check(ns["phase"] == "exit_passed", "合法方案通过出口")
        check(o.state["feedbacks"].get("plan") is None, "通过后不留下打回反馈")
    finally:
        import shutil
        shutil.rmtree(d)

    _reset_fake()
    d, o = _new_orch()
    try:
        _write(os.path.join(d, "docs", "plan.md"), "# P\n## 实现步骤\n没有附录\n")
        _write(os.path.join(d, "docs", "acceptance.feature"),
               "Feature: x\n  Scenario: 无 tag\n    Given x\n")
        o.state["current"] = "plan"
        node = o.pipeline.node("plan")
        ns = o.state["node_states"]["plan"]
        ns["phase"] = "working"
        o._run_exit_gate(node, ns, FakeRunner(d))
        check(ns["phase"] == "exit_passed", "没有任务附录时跳过关联校验")
    finally:
        import shutil
        shutil.rmtree(d)
    print("[B2-1] PASS\n")


def _seed_impl(o, d, plan_text, accept_text):
    from gdd import taskgraph
    _write(os.path.join(d, "docs", "plan.md"), plan_text)
    _write(os.path.join(d, "docs", "acceptance.feature"), accept_text)
    graph = taskgraph.parse(plan_text)
    ns = o.state["node_states"]["implement"]
    ns["phase"] = "working"
    ns["tasks_done"] = [t.id for t in graph.tasks]
    ns["task_sig"] = {t.id: o._task_signature(t) for t in graph.tasks}
    ns["sessions"] = {f"task:{tid}": f"old-{tid}" for tid in ns["tasks_done"]}
    ns["sessions"]["subject"] = "subject-old"
    for tid in ns["tasks_done"]:
        _write(os.path.join(d, ".gdd", "task-results", f"{tid}.md"),
               f"{tid}｜旧｜x:1｜old｜无\n")
    o.state["current"] = "implement"
    return ns


PLAN4 = """# P
## 实现步骤
## 附录：任务依赖
- T1 | - | 甲
- T2 | - | 乙
- T3 | T1 | 依赖甲
- T4 | - | 无关
"""

ACCEPT4 = """Feature: x
  @task:T1
  Scenario: 用例甲
    Given x
  @task:T2
  Scenario: 用例乙
    Given x
  @task:T4
  Scenario: 用例丁
    Given x
"""


def test_task_routing_and_closure():
    print("[B2-2] code 失败按关联任务打回（D3）")
    _reset_fake()
    d, o = _new_orch()
    try:
        ns = _seed_impl(o, d, PLAN4, ACCEPT4)
        cases = [
            {"id": "用例甲", "result": "fail", "cause": "code", "reason": "甲没实现"},
            {"id": "用例乙", "result": "fail", "cause": "code", "reason": "乙没实现"},
            {"id": "用例丁", "result": "pass", "cause": "code", "reason": "ok"},
        ]
        test_node = o.pipeline.node("test")
        check(o._return_to_implement(test_node, cases) is True, "打回实现成功")
        check(ns["tasks_done"] == ["T4"], f"只保留无关任务（{ns['tasks_done']}）")
        check("task:T4" in ns["sessions"], "无关任务会话保留")
        for tid in ("T1", "T2", "T3"):
            check(f"task:{tid}" not in ns["sessions"], f"{tid} 会话已清除")
            check(not os.path.isfile(os.path.join(d, ".gdd", "task-results",
                                                  f"{tid}.md")), f"{tid} 核销行已清除")
        check(ns.get("task_feedback", {}).get("T1", "").find("甲没实现") >= 0,
              "T1 的任务反馈只含关联用例")
        check("上游任务" in ns.get("task_feedback", {}).get("T3", ""),
              "下游任务收到上游重跑提示")
        events = [e for e in o.state["history"] if e.get("event") == "task_route"]
        check(events and set(events[-1]["tasks"]) == {"T1", "T2", "T3"},
              "history 记录失效任务集合")
        check(ns["bounces"] == 1, "打回计数不变")

        # 重跑：只启动 T1/T2，然后 T3，T4 不启动
        runner = FakeRunner(d)
        o._run_impl_tasks(o.pipeline.node("implement"), ns, runner,
                          o.state["feedbacks"]["implement"], [], "")
        check(FakeRunner.started == ["T1", "T2"], f"第一波只跑 T1/T2（{FakeRunner.started}）")
        o._run_impl_tasks(o.pipeline.node("implement"), ns, runner,
                          o.state["feedbacks"]["implement"], [], "")
        check(FakeRunner.started == ["T1", "T2", "T3"], f"第二波跑 T3（{FakeRunner.started}）")
        check("T4" not in FakeRunner.started, "T4 不重跑")
        check(set(ns["tasks_done"]) == {"T1", "T2", "T3", "T4"}, "最终全部完成")
    finally:
        import shutil
        shutil.rmtree(d)
    print("[B2-2] PASS\n")


PLAN3 = """# P
## 实现步骤
## 附录：任务依赖
- T1 | - | 甲
- T2 | - | 乙
- T3 | T1 | 依赖甲
"""

ACCEPT3 = """Feature: x
  @task:T1
  Scenario: 用例甲
    Given x
  @task:T2
  Scenario: 用例乙
    Given x
  @task:T3
  Scenario: 用例丙
    Given x
"""


def test_single_failing_case_routes_only_linked():
    """Gherkin「失败用例只打回关联任务」的数据：只有一条失败且只关联 T1。"""
    print("[B2-2a] 单条失败只关联 T1 时，T2 保持完成（D3）")
    _reset_fake()
    d, o = _new_orch()
    try:
        ns = _seed_impl(o, d, PLAN3, ACCEPT3)
        check(set(ns["tasks_done"]) == {"T1", "T2", "T3"}, "预置 T1/T2/T3 已完成")
        cases = [{"id": "用例甲", "result": "fail", "cause": "code", "reason": "甲没实现"}]
        test_node = o.pipeline.node("test")
        check(o._return_to_implement(test_node, cases) is True, "打回实现成功")
        check(ns["tasks_done"] == ["T2"],
              f"只失效 T1 及其下游 T3，T2 保持完成（{ns['tasks_done']}）")
        check("task:T2" in ns["sessions"], "T2 会话保留")
        for tid in ("T1", "T3"):
            check(f"task:{tid}" not in ns["sessions"], f"{tid} 会话已清除")
            check(not os.path.isfile(os.path.join(d, ".gdd", "task-results",
                                                  f"{tid}.md")), f"{tid} 核销行已清除")
        check(os.path.isfile(os.path.join(d, ".gdd", "task-results", "T2.md")),
              "T2 核销行保留")
        check("甲没实现" in ns.get("task_feedback", {}).get("T1", ""),
              "T1 反馈含关联失败用例")
        check("上游任务" in ns.get("task_feedback", {}).get("T3", ""),
              "下游 T3 收到上游重跑提示")
        check("T2" not in ns.get("task_feedback", {}), "T2 无重跑反馈")
        events = [e for e in o.state["history"] if e.get("event") == "task_route"]
        check(events and set(events[-1]["tasks"]) == {"T1", "T3"},
              "history 记录失效任务集合 T1/T3")
        check(events and events[-1]["cases"] == ["用例甲"],
              "history 记录本轮唯一失败用例")

        # 重跑：第一波 T1，第二波 T3；T2 不在启动序列
        runner = FakeRunner(d)
        o._run_impl_tasks(o.pipeline.node("implement"), ns, runner,
                          o.state["feedbacks"]["implement"], [], "")
        check(FakeRunner.started == ["T1"], f"第一波只跑 T1（{FakeRunner.started}）")
        o._run_impl_tasks(o.pipeline.node("implement"), ns, runner,
                          o.state["feedbacks"]["implement"], [], "")
        check(FakeRunner.started == ["T1", "T3"], f"第二波跑 T3（{FakeRunner.started}）")
        check("T2" not in FakeRunner.started, "T2 不重跑")
        check(set(ns["tasks_done"]) == {"T1", "T2", "T3"}, "最终全部完成")
    finally:
        import shutil
        shutil.rmtree(d)
    print("[B2-2a] PASS\n")


def test_route_fallback_when_unlinked():
    print("[B2-3] 找不到关联时整体回退（D3 兜底）")
    _reset_fake()
    d, o = _new_orch()
    try:
        ns = _seed_impl(o, d, PLAN4, ACCEPT4)
        cases = [{"id": "不存在的用例", "result": "fail", "cause": "code", "reason": "x"}]
        o._return_to_implement(o.pipeline.node("test"), cases)
        check(ns["tasks_done"] == ["T1", "T2", "T3", "T4"], "tasks_done 不变")
        check("task_feedback" not in ns, "不写按任务反馈")
        events = [e for e in o.state["history"] if e.get("event") == "task_route_fallback"]
        check(events and "找不到" in events[-1]["reason"], "记录 task_route_fallback")
        check(o._run_impl_tasks(o.pipeline.node("implement"), ns, FakeRunner(d),
                                o.state["feedbacks"]["implement"], [], "") is None,
              "整体回退到单会话")
    finally:
        import shutil
        shutil.rmtree(d)
    print("[B2-3] PASS\n")


def test_non_code_routes_keep_tasks():
    print("[B2-4] 非 code 分流不变（D4）")
    variants = {
        "spec": 'GDD-ADVERSARIAL: {"cases":[{"id":"c","result":"fail","cause":"spec","reason":"规格错"}]}',
        "test": 'GDD-ADVERSARIAL: {"cases":[{"id":"c","result":"fail","cause":"test","reason":"用例错"}]}',
        "env": 'GDD-ADVERSARIAL: {"cases":[{"id":"c","result":"fail","cause":"env","reason":"环境缺"}]}',
    }
    for cause, text in variants.items():
        _reset_fake()
        FakeRunner.responses = {"GDD-ROLE:adversarial": text}
        d, o = _new_orch()
        try:
            ns = _seed_impl(o, d, PLAN4, ACCEPT4)
            o.state["current"] = "test"
            tns = o.state["node_states"]["test"]
            tns["phase"] = "working"
            o._run_adversarial(o.pipeline.node("test"), tns, FakeRunner(d))
            check(ns["tasks_done"] == ["T1", "T2", "T3", "T4"], f"{cause}: tasks_done 不变")
            if cause == "spec":
                check(o.state["status"] == "awaiting_approval", "spec: 暂停等待定级")
                check((o.state.get("pending_approval") or {}).get("kind") == "grade",
                      "spec: 等的是定级")
            elif cause == "test":
                check(o.state["current"] == "test" and tns["phase"] == "working",
                      "test: 回到测试环节")
                check(tns.get("bounces") == 1, "test: 打回测试自身")
            else:
                check(tns.get("env_retries") == 1 and tns["phase"] == "working",
                      "env: 留在测试环节重跑")
                check(not tns.get("bounces"), "env: 首次不消耗打回")
        finally:
            import shutil
            shutil.rmtree(d)
    print("[B2-4] PASS\n")


def test_named_iteration_paths():
    print("[B2-5] 命名迭代 plan/ledger 路径（D5）")
    _reset_fake()
    pipeline = json.loads(json.dumps(PIPELINE))
    pipeline["nodes"][0]["exit"]["artifacts"][0]["path"] = "{{output}}/plan.md"
    pipeline["nodes"][0]["exit"]["artifacts"][1]["path"] = "{{output}}/acceptance.feature"
    pipeline["nodes"][1]["subject"]["goal"] = "按 {{output}}/plan.md 实现"
    d, o = _new_orch(pipeline, name="iter-x")
    try:
        out = os.path.join(d, "iterations", "iter-x")
        _write(os.path.join(out, "plan.md"),
               "## 实现步骤\n## 附录：任务依赖\n- P1 | - | 甲\n- P2 | P1 | 乙\n")
        ns = o.state["node_states"]["implement"]
        ns["phase"] = "working"
        o.state["current"] = "implement"
        runner = FakeRunner(d)
        o._run_impl_tasks(o.pipeline.node("implement"), ns, runner, "", [], "")
        check(FakeRunner.started == ["P1"], "读取命名迭代 plan 的第一波")
        o._run_impl_tasks(o.pipeline.node("implement"), ns, runner, "", [], "")
        check(FakeRunner.started == ["P1", "P2"], "第二波")
        ledger = os.path.join(out, "impl-ledger.md")
        check(os.path.isfile(ledger), "核销台账写入命名迭代目录")
        with open(ledger, encoding="utf-8") as f:
            body = f.read()
        check("## 核销台账" in body and "P1" in body and "P2" in body, "台账内容")
        check(not os.path.exists(os.path.join(d, "docs", "impl-ledger.md")),
              "不写 docs/impl-ledger.md")
    finally:
        import shutil
        shutil.rmtree(d)
    print("[B2-5] PASS\n")


def test_plan_batch_single_approval():
    print("[B2-6] 方案与验收用例同批产出、一次审批（D17）")
    _reset_fake()
    pipeline = json.loads(json.dumps(PIPELINE))
    pipeline["nodes"][0]["approval"] = {"required": True, "prompt": "确认方案"}
    pipeline["nodes"] = pipeline["nodes"][:2]
    d = tempfile.mkdtemp(prefix="gdd-routing-d17-")
    try:
        o = Orchestrator(d)
        o.init(pipeline, "需求")
        o.load()

        class PlanRunner:
            def __init__(self, root):
                self.root = root

            def run(self, prompt, *, allowed_tools, max_turns, log_path=None,
                    resume_sid="", on_stream=None):
                if "[[GDD-ROLE:subject node:plan]]" in prompt:
                    _write(os.path.join(self.root, "docs", "plan.md"),
                           "# P\n## 实现步骤\n## ADR\n## 附录：任务依赖\n- T1 | - | 甲\n")
                    _write(os.path.join(self.root, "docs", "acceptance.feature"),
                           "Feature: x\n  @task:T1\n  Scenario: 主路径\n    Given x\n")
                    return SessionResult(ok=True, text="plan done",
                                         session_id="plan-sid", turns=1)
                return SessionResult(ok=True, text=PASS, session_id="gate", turns=1)

        import gdd.orchestrator as orch
        orig = orch.AgentRunner
        orch.AgentRunner = PlanRunner
        try:
            o.run()
        finally:
            orch.AgentRunner = orig
        approvals = [e for e in o.state["history"] if e.get("event") == "awaiting_approval"
                     and e.get("node") == "plan"]
        check(len(approvals) == 1, "方案阶段只出现一次审批")
        check(o.state["current"] == "plan" and o.state["status"] == "awaiting_approval",
              "停在方案审批")
        check(os.path.isfile(os.path.join(d, "docs", "plan.md")), "plan.md 产出")
        check(os.path.isfile(os.path.join(d, "docs", "acceptance.feature")),
              "acceptance.feature 同批产出")
        check(o.state["node_states"]["plan"]["phase"] == "exit_passed", "方案出口已通过")
    finally:
        import shutil
        shutil.rmtree(d)
    print("[B2-6] PASS\n")


def main():
    test_plan_exit_link_validation()
    test_task_routing_and_closure()
    test_single_failing_case_routes_only_linked()
    test_route_fallback_when_unlinked()
    test_non_code_routes_keep_tasks()
    test_named_iteration_paths()
    test_plan_batch_single_approval()
    print("ALL TASK ROUTING PASSED")


if __name__ == "__main__":
    main()
