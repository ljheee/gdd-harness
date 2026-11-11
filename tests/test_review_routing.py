"""代码评审节点集成：模板结构、独立只读会话、fail 打回 implement、pass 进入 test。

覆盖 D18-D21。运行：python3 tests/test_review_routing.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd import builtins  # noqa: E402
from gdd.models import PipelineSpec  # noqa: E402
from gdd.orchestrator import Orchestrator  # noqa: E402
from gdd.runner import AgentRunner  # noqa: E402

STUB = os.path.join(ROOT, "tests", "pi_stub.py")
PASS_GATE = 'GDD-VERDICT: {"verdict": "pass", "reason": "stub通过", "evidence": ["e"], "feedback": ""}'
PASS_REVIEW = 'GDD-REVIEW: {"verdict":"pass","issues":[]}'
FAIL_REVIEW = ('GDD-REVIEW: {"verdict":"fail","issues":[{"severity":"blocker",'
               '"file":"gdd/x.py","line":1,"clause":"goal G1","problem":"缺少校验"}]}')


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def _write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _git(d, *args):
    return subprocess.run(["git", *args], cwd=d, check=True,
                          capture_output=True, text=True)


PIPELINE = {
    "id": "review-routing",
    "name": "review-routing",
    "git_delivery": {"enabled": False},
    "nodes": [
        {"id": "implement", "name": "开发实现", "baseline_marker": True,
         "subject": {"goal": "实现"}},
        {"id": "review", "name": "代码评审", "review": True,
         "subject": {"goal": "评审本次实现", "max_turns": 10},
         "entry": {"artifacts": [
             {"path": "docs/impl-ledger.md", "must_contain": "## 核销台账"},
             {"path": "docs/plan.md", "must_contain": "## 实现步骤"},
         ]},
         "exit": {
             "goal": "评审报告完整",
             "artifacts": [
                 {"path": "docs/review.md", "must_contain": "## 评审结论"},
                 {"path": "docs/review.md", "must_contain": "## 问题清单"},
             ],
         }},
        {"id": "test", "name": "测试验证", "subject": {"goal": "测试"}},
    ],
}


def test_template_structure():
    print("[R5-1] 模板结构与 review 配置（D18）")
    tpl = builtins.DELIVERY_PIPELINE
    ids = [n["id"] for n in tpl["nodes"]]
    check(ids == ["project_init", "survey", "clarify", "plan", "implement",
                  "review", "test", "delivery", "release_checklist"],
          f"9 节点固定顺序（{ids}）")
    review = next(n for n in tpl["nodes"] if n["id"] == "review")
    check(review.get("review") is True, "review: true")
    check(not (review.get("approval") or {}).get("required"), "review 无人工审批")
    check(not review.get("session_group"), "review 不加入 implement 的会话组")
    check(review.get("clarify") is not True, "review 不开启澄清")
    spec = PipelineSpec.from_dict(tpl)
    check(spec.prev("review").id == "implement", "implement → review")
    check(spec.next("review").id == "test", "review → test")


def _setup_repo():
    d = tempfile.mkdtemp(prefix="gdd-review-route-")
    _git(d, "init", "-q", "-b", "main")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _write(os.path.join(d, "src", "impl.py"), "def add(a, b):\n    return a + b\n")
    _git(d, "add", "src/impl.py")
    _git(d, "commit", "-qm", "baseline")
    o = Orchestrator(d)
    o.init(json.loads(json.dumps(PIPELINE)), "需求")
    head = _git(d, "rev-parse", "HEAD").stdout.strip()
    o.state["baseline_commit:implement"] = head
    o.state["node_states"]["implement"]["sessions"] = {"subject": "impl-session-xyz"}
    # implement 基线之后的实际改动（评审范围）
    _write(os.path.join(d, "src", "impl.py"), "def add(a, b):\n    return a - b\n")
    _write(os.path.join(d, "docs", "plan.md"), "# 方案\n## 实现步骤\n1. 实现 add\n")
    _write(os.path.join(d, "docs", "impl-ledger.md"),
           "# 台账\n## 核销台账\nT1｜完成｜src/impl.py:1｜单测｜无\n")
    o.state["current"] = "review"
    o.state["node_states"]["review"]["phase"] = "working"
    o._save()
    return d, o


def _pi_env(d, rules, name):
    # pi 配置/脚本/记录放在仓库外，避免污染被测工作区
    base = tempfile.mkdtemp(prefix="gdd-review-pi-")
    settings = os.path.join(base, "pi-settings.json")
    _write(settings, json.dumps({"version": 1, "baseUrl": "https://api.example.com/v1",
                                 "apiKey": "sk-test", "model": "m"}))
    script = os.path.join(base, f"script-{name}.json")
    _write(script, json.dumps(rules, ensure_ascii=False))
    record = os.path.join(base, f"record-{name}.jsonl")
    saved = {k: os.environ.get(k) for k in (
        "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR", "GDD_PI_SESSION_DIR",
        "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_PI_STUB_SCRIPT",
        "GDD_PI_STUB_RECORD", "GDD_PI_STUB_COUNTER_DIR", "GDD_STEER")}
    os.environ.update({
        "GDD_PI_SETTINGS": settings,
        "GDD_PI_AGENT_DIR": os.path.join(base, "agent"),
        "GDD_PI_SESSION_DIR": os.path.join(base, "sessions"),
        "GDD_AGENT_BIN": sys.executable,
        "GDD_AGENT_PRE_ARGS": STUB,
        "GDD_PI_STUB_SCRIPT": script,
        "GDD_PI_STUB_RECORD": record,
        "GDD_PI_STUB_COUNTER_DIR": os.path.join(base, "stub-counters"),
        "GDD_STEER": "off",
    })
    return record, saved, base


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _status(d):
    out = _git(d, "status", "--porcelain", "-uall").stdout
    return {ln for ln in out.splitlines() if ln.strip() and ".gdd/" not in ln}


def _records(path):
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def test_review_fail_then_pass():
    print("[R5-2] 评审 fail 打回 implement，重跑 pass 后进入 test（D19-D21）")
    d, o = _setup_repo()
    record = ""
    saved = {}
    pi_dir = ""
    try:
        rules = [
            {"patterns": ["[[GDD-ROLE:review"], "max_hits": 1, "text": FAIL_REVIEW},
            {"patterns": ["[[GDD-ROLE:review"], "text": PASS_REVIEW},
            {"patterns": ["[[GDD-ROLE:gate"], "text": PASS_GATE},
        ]
        record, saved, pi_dir = _pi_env(d, rules, "failpass")
        before_status = _status(d)
        node = o.pipeline.node("review")
        rns = o.state["node_states"]["review"]

        # ---- 第一次：fail ----
        o._phase_review(node, rns, AgentRunner(d))
        st = o.state
        ins = st["node_states"]["implement"]
        check(st["current"] == "implement", "评审 fail 后 current 指回 implement")
        check(ins["bounces"] == 1, "计入 implement 的 bounces")
        check(st["feedback_kinds"].get("implement") == "review",
              "implement 反馈类型为 review")
        check("缺少校验" in st["feedbacks"].get("implement", ""),
              "implement 反馈包含问题清单")
        check(rns["bounces"] == 0, "评审内容失败不消耗 review 自身 bounces")
        check(st["node_states"]["review"]["phase"] == "pending"
              and st["node_states"]["test"]["phase"] == "pending",
              "review 与 test 等下游重置为 pending")
        check(st["pending_approval"] is None, "评审 fail 不进入人工审批")
        events = [e.get("event") for e in st["history"]]
        check("review_result" in events and "review_bounce" in events,
              "记录 review_result / review_bounce 事件")
        result = [e for e in st["history"] if e.get("event") == "review_result"][-1]
        check(result.get("blocking") is True and result.get("verdict") == "fail",
              "review_result 记录 fail/blocking")
        review_md = os.path.join(d, "docs", "review.md")
        check(os.path.isfile(review_md), "评审报告仍落盘供审计")
        with open(review_md, encoding="utf-8") as f:
            body = f.read()
        check("不通过" in body and "缺少校验" in body, "报告含不通过结论与问题")

        # fail 路径不应启动测试主体/门禁
        roles = [r for r in _records(record)]
        check(not any("subject node:test" in r.get("prompt", "") for r in roles),
              "评审 fail 不进入测试")
        check(not any("gate:exit node:review" in r.get("prompt", "") for r in roles),
              "评审 fail 不运行 review exit 门禁")

        # ---- 第二次：pass ----
        o.state["current"] = "review"
        rns["phase"] = "working"
        o.state["feedbacks"].pop("review", None)
        o._save()
        o._phase_review(node, rns, AgentRunner(d))
        check(o.state["current"] == "review"
              and o.state["node_states"]["review"]["phase"] == "exit_passed",
              "评审通过后先过 exit 门禁")
        recs = _records(record)
        review_runs = [r for r in recs if "[[GDD-ROLE:review" in r.get("prompt", "")]
        check(len(review_runs) == 2, f"两次评审各自新建会话（{len(review_runs)}）")
        for run in review_runs:
            argv = run.get("argv") or []
            check("--session-id" not in argv, "评审会话不续跑任何旧 session")
            tools = argv[argv.index("--tools") + 1] if "--tools" in argv else ""
            check(tools == "read,grep,find",
                  f"评审工具白名单仅只读（{tools}）")
            check("impl-session-xyz" not in json.dumps(argv), "不继承 implement 会话")
            check("[[GDD-ROLE:review" in run.get("prompt", ""), "使用独立评审 prompt")
            check("只读" in run.get("prompt", ""), "prompt 声明只读纪律")
            check("src/impl.py" in run.get("prompt", ""),
                  "prompt 附带 implement 基线以来的 diff")
        check(o.state["pending_approval"] is None, "评审通过不进入人工审批")
        o.step()
        check(o.state["current"] == "test" and o.state["node_states"]["test"]["phase"] == "pending",
              "通过后下一节点为 test")
        check(o.state["node_states"]["review"]["phase"] == "done", "review 节点完成")
        with open(os.path.join(d, "docs", "review.md"), encoding="utf-8") as f:
            body = f.read()
        check("## 评审结论" in body and "通过" in body and "## 问题清单" in body,
              "review.md 含规定章节")
        # review 会话不写文件：工作区唯一新增的条目是 harness 落的 review.md
        added = _status(d) - before_status
        check(added == {"?? docs/review.md"},
              f"评审未修改实现文件（工作区新增 {added}）")
    finally:
        _restore_env(saved)
        if pi_dir:
            shutil.rmtree(pi_dir)
        shutil.rmtree(d)
    print("[R5-2] PASS")


def test_unparsable_review_is_gate_error():
    print("[R5-3] 不可解析评审按 gate_error，不按 fail/pass 走")
    d, o = _setup_repo()
    saved = {}
    pi_dir = ""
    try:
        rules = [{"patterns": ["[[GDD-ROLE:review"], "text": "没有哨兵"}]
        _, saved, pi_dir = _pi_env(d, rules, "unparsable")
        node = o.pipeline.node("review")
        rns = o.state["node_states"]["review"]
        o._phase_review(node, rns, AgentRunner(d))
        events = [e.get("event") for e in o.state["history"]]
        check("gate_error_retry" in events, "记录 gate_error_retry")
        check(o.state["current"] == "review", "仍停在 review")
        check(o.state["node_states"]["implement"]["bounces"] == 0,
              "不把实现判成不通过")
        check(o.state["node_states"]["review"]["phase"] == "working",
              "评审主体相位保持，重试")
        check(o.state["gate_error_streak"] == 1, "故障计数 1")
        # 连续第二次故障 → blocked
        o._phase_review(node, rns, AgentRunner(d))
        check(o.state["status"] == "blocked", "连续 2 次故障转 blocked")
        check(o.state["blocked_owner"] == "review", "blocked_owner=review")
    finally:
        _restore_env(saved)
        if pi_dir:
            shutil.rmtree(pi_dir)
        shutil.rmtree(d)
    print("[R5-3] PASS")


def test_missing_baseline_is_gate_error():
    print("[R5-4] 找不到 implement 基线按 gate_error，不判通过")
    d, o = _setup_repo()
    saved = {}
    pi_dir = ""
    try:
        rules = [{"patterns": ["[[GDD-ROLE:review"], "text": PASS_REVIEW},
                 {"patterns": ["[[GDD-ROLE:gate"], "text": PASS_GATE}]
        record, saved, pi_dir = _pi_env(d, rules, "nobase")
        o.state["baseline_commit:implement"] = ""
        node = o.pipeline.node("review")
        rns = o.state["node_states"]["review"]
        o._phase_review(node, rns, AgentRunner(d))
        check(o.state["gate_error_streak"] == 1, "缺基线记 gate_error")
        check(o.state["current"] == "review", "不推进")
        check(not _records(record), "缺基线不启动评审会话")
    finally:
        _restore_env(saved)
        if pi_dir:
            shutil.rmtree(pi_dir)
        shutil.rmtree(d)
    print("[R5-4] PASS")


def main():
    test_template_structure()
    test_review_fail_then_pass()
    test_unparsable_review_is_gate_error()
    test_missing_baseline_is_gate_error()
    print("ALL REVIEW ROUTING TESTS PASSED")


if __name__ == "__main__":
    main()
