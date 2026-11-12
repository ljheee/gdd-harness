"""节点三段统计的控制台展示测试（无浏览器、无 HTTP 服务）。

覆盖 plan.md 步骤 5 / goal.md D7-D8 的可静态验证部分：

1. 任务详情接口：每个节点都带 ``stats`` 字段——有统计时为三段只读视图
   （段耗时、工具/LLM/token 汇总、会话明细、token 三态），无统计（旧任务）
   或统计损坏时为 None，且详情整体仍可 JSON 序列化、不抛异常。
2. 内嵌页面静态结构：节点方块右下角总耗时样式、悬浮面板（默认隐藏、hover
   显示、z-index、最大宽度与滚动）、flow 渲染接入 ``nodeStatsBlock``。
3. 内嵌 JS 纯函数用 Node 求值（与 test_template_ui_static.py 相同做法）：
   - 已完成且有统计 → 右下角整数分钟（四舍五入）＋ entry/主体/exit 三段
     1 位小数分钟、工具/LLM/token、会话明细；
   - 并行开发 task 会话逐条列出且带 task_id；
   - token 不可得显示「不可得」、段级部分不可得标注「部分不可得」；
   - 未完成节点、旧任务无统计、损坏统计 → 不渲染、不抛 JS 异常；
   - 无会话段只显示 0 值汇总，不出现空会话行；
   - 会话文本经 HTML 转义。

Node 仅作纯 JS 求值器使用（沿用既有测试的既有做法），不引入前端依赖。
运行：python3 tests/test_nodestats_ui.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gdd.ui as ui  # noqa: E402
from gdd import nodestats  # noqa: E402
from gdd.orchestrator import Orchestrator  # noqa: E402

PIPELINE = {
    "id": "ui-stats",
    "name": "统计展示",
    "nodes": [
        {"id": "dev", "name": "开发节点", "description": "并行任务"},
        {"id": "plain", "name": "旧节点", "description": "无统计"},
        {"id": "bad", "name": "损坏节点", "description": "统计损坏"},
    ],
}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# ---------------------------------------------------------------- 任务详情接口

def _make_task(root: str, name: str, with_stats: bool) -> str:
    d = os.path.join(root, name)
    os.makedirs(d)
    o = Orchestrator(d)
    o.init(PIPELINE, requirement="统计展示测试")
    st = o.state
    for nid in ("dev", "plain", "bad"):
        st["node_states"][nid]["phase"] = "done"
    st["status"] = "completed"
    if with_stats:
        # entry：90s，一次门禁会话，usage 未上报 → 不可得
        nodestats.open_segment(st, "dev", "entry", now=100.0)
        nodestats.close_segment(st, "dev", "entry", now=190.0)
        nodestats.add_session(
            st, "dev", "entry", kind="entry", session_id="s-entry",
            started_at=100.0, ended_at=160.0, duration_s=60.0,
            tool_calls=2, llm_calls=1, token_input=10, token_output=5,
            token_status="unavailable")
        # 主体：200s（墙钟，包含并行等待），两个并行 task 会话（一个部分不可得）
        nodestats.open_segment(st, "dev", "subject", now=200.0)
        nodestats.close_segment(st, "dev", "subject", now=400.0)
        nodestats.add_session(
            st, "dev", "subject", kind="task", task_id="T1", session_id="s-t1",
            started_at=200.0, ended_at=300.0, duration_s=100.0,
            tool_calls=3, llm_calls=2, token_input=100, token_output=20,
            token_cache_read=30)
        nodestats.add_session(
            st, "dev", "subject", kind="task", task_id="T2", session_id="s-t2",
            started_at=205.0, ended_at=265.0, duration_s=60.0,
            tool_calls=1, llm_calls=1, token_status="unavailable")
        # exit：40s，一次门禁会话，usage 完整
        nodestats.open_segment(st, "dev", "exit", now=500.0)
        nodestats.close_segment(st, "dev", "exit", now=540.0)
        nodestats.add_session(
            st, "dev", "exit", kind="exit", session_id="s-exit",
            started_at=500.0, ended_at=520.0, duration_s=20.0,
            tool_calls=0, llm_calls=1, token_input=7, token_output=3)
        # plain 节点：统计条目存在但被"剥空"？按旧任务处理不建；bad 节点：损坏结构
        st["node_stats"]["bad"] = {"segments": "oops"}
    o._save()
    return d


def test_task_detail() -> None:
    print("[1] 任务详情 stats 字段")
    root = tempfile.mkdtemp(prefix="gdd-nodestats-ui-")
    try:
        _make_task(root, "with-stats", with_stats=True)
        detail = ui._task_detail(root, "with-stats")
        json.dumps(detail)  # 必须整体可 JSON 序列化（HTTP 响应契约）
        nodes = {n["id"]: n for n in detail["nodes"]}
        check("stats" in nodes["dev"], "每个节点都带 stats 字段")
        dev = nodes["dev"]["stats"]
        check(dev and set(dev) == {"segments", "total_s"}, "stats 为 segments/total_s 只读视图")
        check(dev["total_s"] == 330.0, "total_s 等于三段之和（90+200+40）")
        seg = dev["segments"]
        check(set(seg) == {"entry", "subject", "exit"}, "stats 含 entry/subject/exit 三段")
        check(seg["entry"]["duration_s"] == 90.0, "entry 段耗时")
        check(seg["subject"]["duration_s"] == 200.0, "主体段耗时（墙钟，不等于会话和）")
        check(seg["exit"]["duration_s"] == 40.0, "exit 段耗时")
        check(seg["entry"]["token_status"] == "unavailable"
              and seg["entry"]["token_total"] is None,
              "entry 全部会话不可得：token_status=unavailable 且总数为空（不以 0 冒充）")
        check(seg["subject"]["token_status"] == "partial"
              and seg["subject"]["token_total"] == 150,
              "主体段部分不可得：保留已知合计 150 并标 partial")
        check(seg["subject"]["tool_calls"] == 4 and seg["subject"]["llm_calls"] == 3,
              "主体段工具/LLM 为各会话之和")
        check(seg["exit"]["token_status"] == "available"
              and seg["exit"]["token_total"] == 10,
              "exit 段 token 四项求和（7+3）")
        subj = seg["subject"]["sessions"]
        check([s.get("task_id") for s in subj] == ["T1", "T2"],
              "并行开发会话逐条记录且带 task_id")
        check(all("duration_s" in s and "tool_calls" in s and "llm_calls" in s
                  and "token_status" in s for s in subj), "会话记录带展示所需指标")
        check(nodes["plain"]["stats"] is None, "无统计节点 stats 为 None（旧任务兼容）")
        check(nodes["bad"]["stats"] is None, "统计损坏时 stats 为 None，详情不抛异常")

        _make_task(root, "legacy", with_stats=False)
        legacy = ui._task_detail(root, "legacy")
        check(all(n["stats"] is None for n in legacy["nodes"]),
              "旧任务（无 node_stats）所有节点 stats 为 None")
        check(all(n["phase"] == "done" for n in legacy["nodes"]),
              "旧任务节点状态展示不受 stats 缺失影响")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 页面静态结构

def test_page_static() -> None:
    print("[2] 页面静态结构")
    raw = ui.HTML
    check(".node .node-total{position:absolute;right:8px;bottom:7px" in raw,
          "CSS：节点方块右下角显示总耗时")
    check(".node .node-pop{display:none;position:absolute" in raw,
          "CSS：悬浮面板默认隐藏且绝对定位")
    check(".node:hover .node-pop{display:block}" in raw, "CSS：hover 显示悬浮面板")
    check("z-index:60" in raw and "max-height:340px;overflow:auto" in raw,
          "CSS：悬浮面板抬高 z-index 且限高可滚动")
    check("width:max-content;max-width:min(460px,72vw)" in raw,
          "CSS：悬浮面板限制最大宽度")
    check("function nodeStatsBlock(n)" in raw and "${nodeStatsBlock(n)}" in raw,
          "flow 渲染接入 nodeStatsBlock")
    check("function nodeHasStats(n){return !!(n&&n.phase==='done'&&n.stats"
          "&&typeof n.stats==='object'&&typeof n.stats.total_s==='number'"
          "&&n.stats.segments&&typeof n.stats.segments==='object')}" in raw,
          "只有已完成且统计结构合法的节点展示")
    check(".map(statsSessionRow)" in raw and "class=\"np-s\"" in raw,
          "悬浮面板渲染逐条会话明细")
    check("不可得" in raw and "部分不可得" in raw,
          "页面含不可得/部分不可得文案")


# ---------------------------------------------------------------- JS 渲染（Node）

STATS = {
    "total_s": 330.0,
    "segments": {
        "entry": {
            "duration_s": 90.0, "tool_calls": 2, "llm_calls": 1,
            "token_total": None, "token_status": "unavailable",
            "sessions": [{
                "session_id": "s-entry", "kind": "entry", "task_id": "",
                "duration_s": 60.0, "tool_calls": 2, "llm_calls": 1,
                "token_total": None, "token_status": "unavailable",
            }],
        },
        "subject": {
            "duration_s": 200.0, "tool_calls": 4, "llm_calls": 3,
            "token_total": 150, "token_status": "partial",
            "sessions": [
                {"session_id": "s-t1", "kind": "task", "task_id": "T1",
                 "duration_s": 100.0, "tool_calls": 3, "llm_calls": 2,
                 "token_total": 150, "token_status": "available"},
                {"session_id": "s-t2", "kind": "task", "task_id": "T2",
                 "duration_s": 60.0, "tool_calls": 1, "llm_calls": 1,
                 "token_total": None, "token_status": "unavailable"},
            ],
        },
        "exit": {
            "duration_s": 40.0, "tool_calls": 0, "llm_calls": 0,
            "token_total": 0, "token_status": "available",
            "sessions": [],
        },
    },
}

EMPTY_STATS = {
    "total_s": 0.0,
    "segments": {
        name: {"duration_s": 0.0, "tool_calls": 0, "llm_calls": 0,
               "token_total": 0, "token_status": "available", "sessions": []}
        for name in ("entry", "subject", "exit")
    },
}

EVIL_STATS = {
    "total_s": 60.0,
    "segments": {
        name: {"duration_s": 20.0, "tool_calls": 0, "llm_calls": 0,
               "token_total": 0, "token_status": "available", "sessions": []}
        for name in ("entry", "subject", "exit")
    },
}
EVIL_STATS["segments"]["subject"]["sessions"] = [{
    "session_id": "s", "kind": "<img src=x onerror=alert(1)>", "task_id": "",
    "duration_s": 20.0, "tool_calls": 0, "llm_calls": 0,
    "token_total": 0, "token_status": "available",
}]


def _inline_script(html: str) -> str:
    blocks = re.findall(r"<script(\s[^>]*)?>(.*?)</script>", html, re.S)
    check(len(blocks) == 1 and not (blocks[0][0] or "").strip(),
          "页面唯一内联 script（零外部依赖）")
    return blocks[0][1]


def _js_eval(script: str, harness: str) -> dict:
    node = shutil.which("node")
    check(bool(node), "找到 node（纯 JS 求值器）")
    body = script.splitlines()
    while body and not body[-1].strip():
        body.pop()
    check(bool(body) and re.match(
        r"^refreshNav\(\);showNew\(\);setInterval\(refreshNav,\s*\d+\);$",
        body[-1].strip()), "页面启动语句可剥离")
    body.pop()
    with tempfile.TemporaryDirectory(prefix="gdd-nodestats-ui-") as td:
        path = os.path.join(td, "render.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(body) + "\n" + harness)
        proc = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    check(proc.returncode == 0, f"node 求值退出码 0（stderr: {proc.stderr.strip()[:300]}）")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"node 输出不是合法 JSON：{exc}: {proc.stdout[:300]}")


def test_js_render() -> None:
    print("[3] 内嵌 JS 渲染（Node 求值）")
    script = _inline_script(ui.HTML)
    fixtures = {
        "done": {"phase": "done", "stats": STATS},
        "pending": {"phase": "pending", "stats": STATS},
        "working": {"phase": "working", "stats": STATS},
        "legacy": {"phase": "done"},
        "broken": {"phase": "done", "stats": {"segments": "oops"}},
        "empty": {"phase": "done", "stats": EMPTY_STATS},
        "evil": {"phase": "done", "stats": EVIL_STATS},
    }
    harness = (
        "\nconst fixtures = " + json.dumps(fixtures, ensure_ascii=False) + ";\n"
        "const out = {};\n"
        "for (const [k, n] of Object.entries(fixtures)) out[k] = nodeStatsBlock(n);\n"
        "out.doneHas = nodeHasStats(fixtures.done);\n"
        "out.pendingHas = nodeHasStats(fixtures.pending);\n"
        "out.tokenNull = statsToken(null, '');\n"
        "out.tokenZero = statsToken(0, 'available');\n"
        "out.tokenPartial = statsToken(5, 'partial');\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    out = _js_eval(script, harness)
    done = out["done"]
    check("<div class=\"node-total\">6min</div>" in done,
          "已完成节点右下角显示整数分钟总耗时（330s → 6min，四舍五入）")
    check(out["doneHas"] is True and out["pendingHas"] is False,
          "nodeHasStats 只对已完成且有统计的节点为真")
    check(out["pending"] == "" and out["working"] == "",
          "未完成节点不显示总耗时/悬浮统计")
    check(out["legacy"] == "", "旧任务无统计不渲染，不抛异常")
    check(out["broken"] == "", "统计损坏不渲染，不抛异常")
    check("耗时合计 5.5min" in done and "class=\"node-pop\"" in done,
          "悬浮面板显示三段合计（1 位小数 min）")
    for name, label in (("entry", "entry"), ("subject", "主体"), ("exit", "exit")):
        check(f'data-seg="{name}"' in done and f"<b>{label}</b>" in done,
              f"悬浮面板含 {label} 段汇总行（data-seg={name}）")
    for value in ("1.5min", "3.3min", "0.7min"):
        check(value in done, f"段耗时保留 1 位小数（{value}）")
    check("工具 4" in done and "LLM 3" in done,
          "主体段展示工具/LLM 合计")
    check("token 150（部分不可得）" in done,
          "段级部分不可得：显示已知合计并标注")
    check("token 不可得" in done, "会话 token 不可得显示「不可得」")
    check(out["tokenNull"] == "不可得" and out["tokenZero"] == "0"
          and out["tokenPartial"] == "5（部分不可得）",
          "token 文案：缺失不可得、真实 0、部分不可得三态")
    check("task T1" in done and "task T2" in done
          and 'data-task="T1"' in done and 'data-task="T2"' in done,
          "并行开发会话逐条列出且可按 task_id 区分")
    check('data-kind="entry"' in done and done.count('data-kind="task"') == 2,
          "会话行带 kind/task_id DOM 属性")
    check(done.count('class="np-s"') == 3,
          "三个会话行（entry 1 + 主体 2），exit 无会话不显示空行")
    empty = out["empty"]
    check("<div class=\"node-total\">0min</div>" in empty
          and "0.0min" in empty and "token 0" in empty,
          "无会话段显示 0 值汇总")
    check('class="np-s"' not in empty, "无会话段不渲染空会话行")
    evil = out["evil"]
    check("<img" not in evil and "&lt;img" in evil,
          "会话文本经 HTML 转义")


def main() -> None:
    test_task_detail()
    test_page_static()
    test_js_render()
    print("\nALL NODESTATS UI PASSED")


if __name__ == "__main__":
    main()
