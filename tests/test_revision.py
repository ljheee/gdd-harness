"""决策定级、增量重跑落盘、对抗分流。

运行：python3 tests/test_revision.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.models import PipelineSpec  # noqa: E402
from gdd.orchestrator import Orchestrator  # noqa: E402
from gdd.revision import (  # noqa: E402
    ADVERSARIAL_INTRO, CONTRACT, apply_staged, baseline_matches, parse_adversarial,
    parse_grade, revision_range, route_cases, snapshot_baseline,
)
from gdd.runner import SessionResult  # noqa: E402

PASS = 'GDD-VERDICT: {"verdict": "pass", "reason": "ok", "evidence": [], "feedback": ""}'
GRADE = ('GDD-GRADE: {"level":"L0","clause":"对外契约","fact":"要删字段",'
         '"basis":"goal 已确认保留"}')
CODE = ('GDD-ADVERSARIAL: {"cases":['
        '{"id":"登录","result":"fail","cause":"code","reason":"少了校验"},'
        '{"id":"列表","result":"fail","cause":"test","reason":"数据不对"}]}')
SPEC = ('GDD-ADVERSARIAL: {"cases":['
        '{"id":"规格","result":"fail","cause":"spec","reason":"规格写错"},'
        '{"id":"登录","result":"fail","cause":"code","reason":"少了校验"}]}')
ENV = ('GDD-ADVERSARIAL: {"cases":['
       '{"id":"库","result":"fail","cause":"env","reason":"没有数据库"}]}')

PIPE = {
    "id": "p", "name": "p",
    "git_delivery": {"enabled": False},
    "nodes": [
        {"id": "clarify", "name": "需求澄清"},
        {"id": "plan", "name": "方案设计"},
        {"id": "implement", "name": "开发实现", "baseline_marker": True, "clarify": True},
        {"id": "test", "name": "测试验证", "adversarial": True},
    ],
}


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


class Scripted:
    def __init__(self, root, subject="ok", adversarial=None):
        self.root = root
        self.subject = subject
        self.adversarial = adversarial
        self.prompts = []
        self.resume_sids = []
        self.subjects = 0
        self.grade_text = GRADE

    def run(self, prompt, *, allowed_tools, max_turns, log_path=None, resume_sid="", on_stream=None):
        self.prompts.append(prompt)
        self.resume_sids.append(resume_sid)
        if "[[GDD-ROLE:adversarial" in prompt:
            text = self.adversarial or "not-a-verdict"
            return SessionResult(ok=True, text=text, session_id="adv", turns=1)
        if "[[GDD-ROLE:subject" in prompt:
            self.subjects += 1
            if self.subject == "grade":
                return SessionResult(ok=True, text=self.grade_text, session_id="s", turns=1)
            return SessionResult(ok=True, text="subject done", session_id="s", turns=1)
        return SessionResult(ok=True, text=PASS, session_id="g", turns=1)


def fresh(subject="ok", adversarial=None):
    root = tempfile.mkdtemp(prefix="gdd-rev-")
    os.makedirs(os.path.join(root, "docs"))
    o = Orchestrator(root)
    o.init(PIPE, "需求")
    runner = Scripted(root, subject, adversarial)
    import gdd.orchestrator as orch
    orch.AgentRunner = lambda r: runner
    return root, o, runner


def restore_runner(orig):
    import gdd.orchestrator as orch
    orch.AgentRunner = orig


def test_parsers():
    print("[P] 解析与分流")
    check(parse_grade(CONTRACT) is None, "契约正文不会被当成定级")
    check(".gdd/revision-stage/docs/decisions.md" in CONTRACT
          and "不要改正式文件" in CONTRACT, "契约在重跑时改写暂存区")
    check(parse_adversarial(ADVERSARIAL_INTRO) is None, "对抗说明不会被当成裁决")
    g = parse_grade("前文\n" + GRADE + "\n")
    check(g["level"] == "L0" and g["fact"] == "要删字段", "取最后一条合法定级")
    check(parse_grade('GDD-GRADE: {"level":"L9","clause":"x","fact":"y","basis":"z"}') is None,
          "未知等级丢弃")
    check(route_cases(parse_adversarial(CODE)["cases"]) == "code", "code 分流")
    check(route_cases(parse_adversarial(SPEC)["cases"]) == "spec", "spec 优先于 code")
    check(route_cases(parse_adversarial(ENV)["cases"]) == "test", "env 留在测试")
    nodes = PipelineSpec.from_dict(PIPE).nodes
    check([n.id for n in revision_range(nodes)] == ["clarify", "plan"], "重跑区间到开发阶段之前")
    cut = [n for n in nodes if n.id != "clarify"]
    check(revision_range(cut) == [], "没有需求澄清就不能重跑")
    print("[P] PASS\n")


def test_cas_files():
    print("[C] 指纹一致才复制暂存稿")
    root = tempfile.mkdtemp(prefix="gdd-cas-")
    os.makedirs(os.path.join(root, "docs"))
    live = os.path.join(root, "docs", "goal.md")
    with open(live, "w") as f:
        f.write("BASE")
    state = {"node_states": {"implement": {"phase": "done"}}}
    base = snapshot_baseline(root, state, ["implement"])
    stage = os.path.join(root, ".gdd", "revision-stage", "docs", "goal.md")
    os.makedirs(os.path.dirname(stage))
    with open(stage, "w") as f:
        f.write("NEW")
    check(baseline_matches(root, state, base, ["implement"]), "未改基线时指纹一致")
    check(apply_staged(root) == ["docs/goal.md"], "一致时复制暂存稿")
    check(open(live).read() == "NEW", "正式产物变成修订稿")

    with open(live, "w") as f:
        f.write("MOVED")
    with open(stage, "w") as f:
        f.write("STAGED")
    check(not baseline_matches(root, state, base, ["implement"]), "基线变了就拒绝")
    check(open(live).read() == "MOVED", "拒绝时不覆盖正式产物")
    from gdd.revision import outside_progress
    same = {"node_states": {"clarify": {"phase": "working"}, "implement": {"phase": "done"}}}
    moved = {"node_states": {"implement": {"phase": "working"}}}
    check(outside_progress(same, ["implement"]) == outside_progress(state, ["implement"]),
          "重跑自己的阶段不进进度指纹")
    check(outside_progress(moved, ["implement"]) != outside_progress(state, ["implement"]),
          "别人推进了开发阶段就能看出来")
    shutil.rmtree(root)
    print("[C] PASS\n")


def test_grade_and_rerun():
    print("[G] 定级确认后从需求澄清重跑，驳回不修订")
    import gdd.orchestrator as orch
    orig = orch.AgentRunner
    root, o, runner = fresh("grade")
    try:
        os.makedirs(os.path.join(root, "docs"), exist_ok=True)
        with open(os.path.join(root, "docs", "goal.md"), "w") as f:
            f.write("ORIGINAL")
        st = o.state
        st["current"] = "implement"
        st["node_states"]["implement"]["phase"] = "working"
        st["node_states"]["implement"]["sessions"] = {"subject": "impl-sid"}
        st["node_states"]["clarify"]["phase"] = "done"
        st["node_states"]["clarify"]["sessions"] = {"subject": "clarify-sid"}
        st["node_states"]["plan"]["phase"] = "done"
        o._save()
        check(o.step() is False, "L0 暂停")
        pa = o.state["pending_approval"]
        check(pa["kind"] == "grade" and "要删字段" in pa["summary"], "待确认里有事实")
        o.state["node_states"]["implement"]["sessions"]["subject"] = "impl-sid"
        o.state["node_states"]["test"]["env_retries"] = 1
        o._save()
        check(o.resolve_approval(True, "请改字段"), "批准定级")
        st = o.state
        check(st["current"] == "clarify" and st["revision"]["active"], "从需求澄清开始重跑")
        check(st["revision"]["nodes"] == ["clarify", "plan"], "重跑不含开发阶段")
        check(st["node_states"]["clarify"]["sessions"]["subject"] == "clarify-sid", "复用澄清会话")
        check(st["node_states"]["implement"]["sessions"]["subject"] == "impl-sid", "实现会话还在")
        check(st["node_states"]["implement"]["phase"] == "pending", "开发阶段重新排队")
        check("env_retries" not in st["node_states"]["test"], "重跑清掉环境说明次数")
        check(open(os.path.join(root, "docs", "goal.md")).read() == "ORIGINAL", "重跑不清空产物")

        st["node_states"]["clarify"]["phase"] = "working"
        st["node_states"]["clarify"]["bounces"] = 2
        check(o.step() is True, "重跑节点继续")
        subjects = [p for p in runner.prompts if "[[GDD-ROLE:subject node:clarify" in p]
        check(any("[[GDD-RERUN]]" in p and "决策定级契约" in p and "要删字段" in p
                  and "请改字段" in p and "上轮会话中断" not in p for p in subjects),
              "重跑任务书带决定、确认意见和重跑标记")
        check("clarify-sid" in runner.resume_sids, "打回已达 2 次仍复用原会话")
        check(st["node_states"]["clarify"]["phase"] == "exit_passed", "重跑节点通过出口门禁")
        check(any(h.get("event") == "exit_pass" and h.get("node") == "clarify"
                  for h in st["history"]), "出口门禁重新跑过")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)

    root, o, runner = fresh("grade")
    try:
        st = o.state
        st["current"] = "implement"
        st["node_states"]["implement"]["phase"] = "working"
        o._save()
        o.step()
        check(o.resolve_approval(False, "不改契约"), "驳回定级")
        check(o.state["status"] == "running", "驳回后继续")
        check("不启动修订" in o.state["feedbacks"]["implement"], "驳回不修订")
        check(o.step() is True, "同一事实不再提起")
        check(o.state["status"] != "awaiting_approval", "没有再次暂停")
        check(o.state["node_states"]["implement"]["phase"] == "exit_passed", "按 L2 通过出口门禁")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)
    print("[G] PASS\n")


def test_rebase():
    print("[B] 基线变化时放弃本次写入")
    import gdd.orchestrator as orch
    orig = orch.AgentRunner
    root, o, runner = fresh()
    try:
        live = os.path.join(root, "docs", "goal.md")
        os.makedirs(os.path.dirname(live), exist_ok=True)
        with open(live, "w") as f:
            f.write("BASE")
        st = o.state
        st["current"] = "clarify"
        st["node_states"]["clarify"]["phase"] = "working"
        st["node_states"]["implement"]["phase"] = "done"
        outside = ["implement", "test"]
        st["revision"] = {
            "active": True,
            "nodes": ["clarify", "plan"],
            "baseline": snapshot_baseline(root, st, outside),
        }
        with open(live, "w") as f:
            f.write("MOVED")
        stage = os.path.join(root, ".gdd", "revision-stage", "docs", "goal.md")
        os.makedirs(os.path.dirname(stage))
        with open(stage, "w") as f:
            f.write("STAGED")
        o._save()
        check(o.step() is False, "指纹不一致则暂停签批")
        check(open(live).read() == "MOVED", "暂存稿没有盖住新基线")
        check(o.state["pending_approval"]["kind"] == "rebase", "签批种类是基线变化")
        check(o.resolve_approval(False), "拒绝签批")
        check(open(live).read() == "BASE", "正式产物退回上次确认的基线")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)

    root, o, runner = fresh()
    try:
        live = os.path.join(root, "docs", "goal.md")
        os.makedirs(os.path.dirname(live), exist_ok=True)
        with open(live, "w") as f:
            f.write("BASE")
        st = o.state
        st["current"] = "clarify"
        st["node_states"]["clarify"]["phase"] = "working"
        st["revision"] = {
            "active": True,
            "nodes": ["clarify", "plan"],
            "baseline": snapshot_baseline(root, st, ["implement", "test"]),
        }
        with open(live, "w") as f:
            f.write("MOVED")
        stage = os.path.join(root, ".gdd", "revision-stage", "docs", "goal.md")
        os.makedirs(os.path.dirname(stage))
        with open(stage, "w") as f:
            f.write("STAGED")
        o._save()
        check(o.step() is False, "批准路径同样先停住")
        check(o.resolve_approval(True), "批准新基线")
        check(not os.path.isfile(stage), "旧暂存稿作废")
        check(open(live).read() == "MOVED", "新基线保留")
        st = o.state
        st["node_states"]["clarify"]["phase"] = "working"
        check(o.step() is True, "下一轮按新基线继续")
        check(open(live).read() == "MOVED", "没有把旧稿盖回去")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)

    root, o, runner = fresh()
    try:
        live = os.path.join(root, "docs", "goal.md")
        os.makedirs(os.path.dirname(live), exist_ok=True)
        with open(live, "w") as f:
            f.write("BASE")
        st = o.state
        st["current"] = "clarify"
        st["node_states"]["clarify"]["phase"] = "working"
        st["revision"] = {
            "active": True,
            "nodes": ["clarify", "plan"],
            "baseline": snapshot_baseline(root, st, ["implement", "test"]),
        }
        stage = os.path.join(root, ".gdd", "revision-stage", "docs", "goal.md")
        os.makedirs(os.path.dirname(stage))
        with open(stage, "w") as f:
            f.write("NEW")
        o._save()
        check(o.step() is True, "指纹一致时落盘")
        check(open(live).read() == "NEW", "暂存稿写到正式产物")

        dec = os.path.join(root, "docs", "decisions.md")
        with open(dec, "w") as f:
            f.write("OLD\n")
        st = o.state
        st["current"] = "plan"
        st["node_states"]["plan"]["phase"] = "working"
        st["revision"]["baseline"] = snapshot_baseline(root, st, ["implement", "test"])
        bad = os.path.join(root, ".gdd", "revision-stage", "docs", "decisions.md")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        with open(bad, "w") as f:
            f.write("REWRITTEN\n")
        o._save()
        check(o.step() is True, "改旧决策记录时不落盘")
        check(open(dec).read() == "OLD\n", "旧决策还在")
        check("不能改旧行" in o.state["feedbacks"]["plan"], "要求只追加")
        check(o.step() is True, "再改旧行则打回")
        check(o.state["node_states"]["plan"]["bounces"] == 1, "计入打回，不再空转")
        check(open(dec).read() == "OLD\n", "打回后旧决策仍在")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)
    print("[B] PASS\n")


def test_adversarial_routes():
    print("[A] 对抗分流")
    import gdd.orchestrator as orch
    orig = orch.AgentRunner

    def arm(text):
        root, o, runner = fresh(adversarial=text)
        st = o.state
        st["current"] = "test"
        st["node_states"]["implement"]["phase"] = "done"
        st["node_states"]["test"]["phase"] = "working"
        o._runner = runner
        o._save()
        return root, o

    root, o = arm(CODE)
    try:
        check(o.step() is True, "code 问题回到实现")
        st = o.state
        check(st["current"] == "implement", "当前节点是实现")
        check(st["node_states"]["test"]["phase"] == "pending", "测试重新排队")
        fb = st["feedbacks"]["implement"]
        check("少了校验" in fb and "数据不对" in fb, "全部失败用例都带回去")
        check(st["node_states"]["implement"]["bounces"] == 1, "计入实现打回")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)

    root, o = arm(SPEC)
    try:
        check(o.step() is False, "spec 问题暂停定级")
        pa = o.state["pending_approval"]
        check(pa["kind"] == "grade" and pa["grade"]["clause"] == "spec", "走修订确认")
        check(not o.state["node_states"]["test"].get("adversarial_done"), "未判通过")
        check(o.state["current"] == "test", "还停在测试")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)

    root, o = arm(ENV)
    try:
        check(o.step() is True, "环境问题留在测试")
        ns = o.state["node_states"]["test"]
        fb = o.state["feedbacks"]["test"]
        check(ns.get("bounces", 0) == 0 and ns.get("env_retries") == 1, "第一次不计入打回")
        check("一次说明" in fb and "不能记成通过" in fb, "一次说明，且不能记成通过")
        check(not ns.get("adversarial_done"), "环境缺口不是通过")
        check(o.step() is True, "再次仍是环境问题")
        check(o.state["node_states"]["test"]["bounces"] == 1, "第二次才打回重跑")
        check(o.state["current"] == "test", "仍在测试环节")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)

    root, o = arm("不是裁决")
    try:
        check(o.step() is True, "裁决不可解析则重试对抗会话")
        check(o.state["node_states"]["test"]["phase"] == "adversarial_retry", "不重跑测试主体")
        check(o._runner.subjects == 1, "主体只跑过一次")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)

    root, o = arm('GDD-ADVERSARIAL: {"cases":[{"id":"库","result":"pass","cause":"env","reason":"没起来"}]}')
    try:
        check(o.step() is True, "环境被写成通过也不放行")
        ns = o.state["node_states"]["test"]
        check(not ns.get("adversarial_done"), "环境通过不是通过")
        check("不能记成通过" in o.state["feedbacks"]["test"], "改记为环境失败")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)

    root, o = arm('GDD-ADVERSARIAL: {"cases":[]}')
    try:
        check(o.step() is True, "空裁决不放行")
        check(o.state["node_states"]["test"]["phase"] == "adversarial_retry", "空列表留在对抗重试")
        check(not o.state["node_states"]["test"].get("adversarial_done"), "空列表不是通过")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)
    print("[A] PASS\n")


def test_grade_does_not_land_or_skip():
    print("[R] 驳回不落盘，确认不能被澄清收尾拆掉")
    import gdd.orchestrator as orch
    orig = orch.AgentRunner
    root, o, runner = fresh("grade")
    try:
        live = os.path.join(root, "docs", "goal.md")
        os.makedirs(os.path.dirname(live), exist_ok=True)
        with open(live, "w") as f:
            f.write("BASE")
        st = o.state
        st["current"] = "clarify"
        st["node_states"]["clarify"]["phase"] = "working"
        st["node_states"]["clarify"]["sessions"] = {"subject": "clarify-sid"}
        st["revision"] = {
            "active": True,
            "nodes": ["clarify", "plan"],
            "confirmed": [],
            "reason": "",
            "baseline": snapshot_baseline(root, st, ["implement", "test"]),
        }
        stage = os.path.join(root, ".gdd", "revision-stage", "docs", "goal.md")
        os.makedirs(os.path.dirname(stage))
        with open(stage, "w") as f:
            f.write("REJECTED-L0")
        o._save()
        check(o.step() is False, "修订中的新定级仍要确认")
        check(o.resolve_approval(False, "不要改"), "驳回这条定级")
        check(open(live).read() == "BASE", "驳回时正式文件不变")
        check(not os.path.isfile(stage), "驳回作废暂存稿")
        check(o.state["current"] == "clarify", "驳回不重开一轮修订")
        runner.subject = "ok"
        check(o.step() is True, "驳回后按 L2 继续")
        check(open(live).read() == "BASE", "继续时不会把驳回稿盖上去")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)

    root, o, runner = fresh("grade")
    try:
        st = o.state
        st["current"] = "implement"
        st["node_states"]["implement"]["phase"] = "working"
        o._save()
        check(o.step() is False, "先停在定级")
        r = o.clarify_done("implement")
        check(not r.get("ok"), "结束澄清不能跳过定级")
        check(o.state["status"] == "awaiting_approval", "定级挂起还在")
        check(o.state["pending_approval"]["kind"] == "grade", "挂起项没被清掉")
        check(o.resolve_approval(True, "请改字段"), "确认后才修订")
        check(o.state["current"] == "clarify", "第一次确认从需求澄清开始")
        st = o.state
        st["node_states"]["clarify"]["phase"] = "working"
        runner.subjects = 0
        runner.subject = "grade"
        runner.grade_text = ('GDD-GRADE: {"level":"L1","clause":"实效性",'
                             '"fact":"要改成异步","basis":"原来是同步"}')
        check(o.step() is False, "修订中的另一条仍要确认")
        check(o.resolve_approval(True, "继续这条"), "确认后留在本轮")
        check(o.state["current"] == "clarify", "不把本轮修订作废重来")
        check(o.state["revision"]["active"], "修订还在")
        check("要删字段" in o.state["revision"]["reason"]
              and "要改成异步" in o.state["revision"]["reason"], "两条已确认的决定都在")
    finally:
        restore_runner(orig)
        shutil.rmtree(root)
    print("[R] PASS\n")


if __name__ == "__main__":
    test_parsers()
    test_cas_files()
    test_grade_and_rerun()
    test_rebase()
    test_adversarial_routes()
    test_grade_does_not_land_or_skip()
    print("ALL PASS")
