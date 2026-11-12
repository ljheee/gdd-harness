"""节点三段统计模块单元测试：段 open/close、会话合并、token 三态、total。"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd import nodestats  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def _empty_values(view):
    return [(view["segments"][name]["duration_s"],
             view["segments"][name]["tool_calls"],
             view["segments"][name]["llm_calls"]
             ) for name in nodestats.SEGMENTS]


def test_segment_intervals():
    print("[NS-1] 段 open/close 与多区间累计")
    state = {}
    check(nodestats.open_segment(state, "n1", "entry", now=100.0), "open entry")
    check(nodestats.close_segment(state, "n1", "entry", now=110.0) == 10.0, "entry 区间 10s")
    check(nodestats.close_segment(state, "n1", "entry", now=120.0) == 0.0,
          "重复 close 幂等")
    nodestats.open_segment(state, "n1", "entry", now=200.0)
    check(nodestats.close_segment(state, "n1", "entry", now=205.0) == 5.0,
          "entry 第二区间 5s（多区间相加不重复计等待）")

    nodestats.open_segment(state, "n1", "subject", now=110.0)
    nodestats.close_segment(state, "n1", "subject", now=199.0)
    nodestats.open_segment(state, "n1", "exit", now=205.0)
    nodestats.close_segment(state, "n1", "exit", now=215.0)

    view = nodestats.refresh(state, "n1")
    check(view["segments"]["entry"]["duration_s"] == 15.0, "entry=15")
    check(view["segments"]["subject"]["duration_s"] == 89.0, "subject=89")
    check(view["segments"]["exit"]["duration_s"] == 10.0, "exit=10")
    check(view["total_s"] == 114.0, "total = 三段之和")
    check(all("open_started_at" not in view["segments"][name]
              for name in nodestats.SEGMENTS), "视图剥离 open_started_at")
    print()


def test_open_twice_discards_stale_start():
    print("[NS-2] 未关闭区间被新 open 覆盖（不计跨进程停机）")
    state = {}
    nodestats.open_segment(state, "n", "subject", now=1000.0)
    nodestats.open_segment(state, "n", "subject", now=1100.0)
    check(nodestats.close_segment(state, "n", "subject", now=1110.0) == 10.0,
          "只计最后一次 open 起的 10s")
    view = nodestats.refresh(state, "n")
    check(view["segments"]["subject"]["duration_s"] == 10.0, "旧起点被丢弃")
    print()


def test_empty_segment():
    print("[NS-3] 空段为真实 0（token 可得 0，不是不可得）")
    check(nodestats.refresh({}, "absent") is None, "无统计条目不凭空创建")
    state = {}
    nodestats.ensure(state, "fresh")
    view = nodestats.refresh(state, "fresh")
    for name in nodestats.SEGMENTS:
        seg = view["segments"][name]
        check(seg["duration_s"] == 0.0 and seg["tool_calls"] == 0 and seg["llm_calls"] == 0,
              f"{name} 段零值")
        check(seg["token_total"] == 0 and seg["token_status"] == nodestats.TOKEN_AVAILABLE,
              f"{name} 段 token 为 0 且 available")
        check(seg["sessions"] == [], f"{name} 段无会话行")
    check(view["total_s"] == 0.0, "空统计 total=0")
    print()


def _add(state, node_id="n", segment="subject", **kw):
    params = dict(kind="subject", session_id="s1", started_at=100.0, ended_at=110.0,
                  duration_s=10.0, tool_calls=0, llm_calls=0)
    params.update(kw)
    return nodestats.add_session(state, node_id, segment, **params)


def test_session_merge():
    print("[NS-4] 同 session_id 的 resume 合并 / 不同 id、task、无 id 分开")
    state = {}
    _add(state, session_id="s1", tool_calls=3, llm_calls=2,
         token_input=100, token_output=50, token_cache_creation=10,
         token_cache_read=40, token_status="available")
    _add(state, session_id="s1", started_at=200.0, ended_at=215.0, duration_s=15.0,
         tool_calls=4, llm_calls=3, token_input=200, token_output=60,
         token_cache_creation=0, token_cache_read=0, token_status="available")
    sessions = nodestats.for_ui(state, "n")["segments"]["subject"]["sessions"]
    check(len(sessions) == 1, "同一 session_id 合并为一条")
    s = sessions[0]
    check(s["attempts"] == 2, "attempts 累加为 2")
    check(s["duration_s"] == 25.0, "会话耗时累加 25s")
    check(s["tool_calls"] == 7 and s["llm_calls"] == 5, "工具/LLM 次数累加")
    check(s["token_total"] == 460, "token 四项按调用累加（200 + 260）")
    check(s["started_at"] == 100.0 and s["ended_at"] == 215.0, "起止取首末")

    _add(state, session_id="s2")
    _add(state, session_id="s1", task_id="T2")  # 同 id 不同 task：独立记录
    _add(state, session_id="")                  # 无 id：每次尝试独立
    _add(state, session_id="", kind="task")
    # resume/steer 与 task-<id> 归一为 spec 的会话类型，续跑仍合并
    _add(state, session_id="s3", kind="subject_resume", started_at=300.0, ended_at=305.0)
    _add(state, session_id="s3", kind="subject_steer", started_at=400.0, ended_at=405.0)
    _add(state, session_id="s4", kind="task-T9", task_id="T9",
         started_at=500.0, ended_at=505.0)
    sessions = nodestats.for_ui(state, "n")["segments"]["subject"]["sessions"]
    check(len(sessions) == 7, "不同 session/task/无 id 分别记录")
    anon = [x for x in sessions if x["session_id"].startswith(("subject#", "task#"))]
    check(len(anon) == 2 and len({x["session_id"] for x in anon}) == 2,
          "无 id 调用生成不同的稳定标识")
    check(all(x["attempts"] == 1 for x in anon), "无 id 调用不跨调用合并")
    task2 = [x for x in sessions if x["task_id"] == "T2"]
    check(len(task2) == 1 and task2[0]["session_id"] == "s1", "同 id 不同 task 分开")
    s3 = [x for x in sessions if x["session_id"] == "s3"][0]
    check(s3["kind"] == "subject" and s3["attempts"] == 2, "subject_resume/steer 归一为 subject 并合并")
    s4 = [x for x in sessions if x["session_id"] == "s4"][0]
    check(s4["kind"] == "task" and s4["task_id"] == "T9", "task-<id> 归一为 task 并保留 task_id")
    print()


def test_token_tristate():
    print("[NS-5] token 三态：available / partial / unavailable")
    # 全部可得
    state = {}
    _add(state, segment="entry", llm_calls=1, token_input=10, token_output=5,
         token_cache_creation=2, token_cache_read=3, token_status="available")
    seg = nodestats.refresh(state, "n")["segments"]["entry"]
    check(seg["token_total"] == 20 and seg["token_status"] == nodestats.TOKEN_AVAILABLE,
          "全部可得：四项求和 available")

    # 部分可得
    _add(state, segment="entry", session_id="s2", llm_calls=1,
         token_status="unavailable")
    seg = nodestats.refresh(state, "n")["segments"]["entry"]
    check(seg["token_total"] == 20 and seg["token_status"] == nodestats.TOKEN_PARTIAL,
          "部分可得：已知合计 + partial")
    s2 = [x for x in seg["sessions"] if x["session_id"] == "s2"][0]
    check(s2["token_total"] is None, "不可得会话 token_total 为空")

    # 全部不可得
    state = {}
    _add(state, segment="exit", llm_calls=2, token_status="unavailable")
    seg = nodestats.refresh(state, "n")["segments"]["exit"]
    check(seg["token_total"] is None and seg["token_status"] == nodestats.TOKEN_UNAVAILABLE,
          "全部不可得：None + unavailable（不以 0 冒充）")

    # 同会话一次可得一次不可得 → 合并后不可得
    state = {}
    _add(state, session_id="mixed", llm_calls=1, token_input=10, token_status="available")
    _add(state, session_id="mixed", llm_calls=1, token_status="unavailable")
    seg = nodestats.refresh(state, "n")["segments"]["subject"]
    s = seg["sessions"][0]
    check(s["token_status"] == nodestats.TOKEN_UNAVAILABLE and s["token_total"] is None,
          "合并会话任一次不可得 → 整体不可得")
    check(seg["token_status"] == nodestats.TOKEN_UNAVAILABLE, "段随之不可得")

    # 未显式给 status：有模型响应但 usage 全 0 → 不可得；无响应 → 0 可得
    state = {}
    _add(state, session_id="zero-usage", llm_calls=1)
    _add(state, session_id="no-llm", llm_calls=0)
    seg = nodestats.refresh(state, "n")["segments"]["subject"]
    by_id = {x["session_id"]: x for x in seg["sessions"]}
    check(by_id["zero-usage"]["token_status"] == nodestats.TOKEN_UNAVAILABLE,
          "有模型响应但 usage 全 0 → 不可得")
    check(by_id["no-llm"]["token_total"] == 0
          and by_id["no-llm"]["token_status"] == nodestats.TOKEN_AVAILABLE,
          "无模型响应 → token 0 可得")
    print()


def test_total_and_aggregates():
    print("[NS-6] 段级汇总与 total")
    state = {}
    nodestats.open_segment(state, "n", "entry", now=0.0)
    nodestats.close_segment(state, "n", "entry", now=20.0)
    nodestats.open_segment(state, "n", "subject", now=20.0)
    nodestats.close_segment(state, "n", "subject", now=120.0)
    nodestats.open_segment(state, "n", "exit", now=120.0)
    nodestats.close_segment(state, "n", "exit", now=140.0)
    _add(state, segment="subject", session_id="a", tool_calls=2, llm_calls=3,
         token_input=7, token_status="available")
    _add(state, segment="subject", session_id="b", tool_calls=5, llm_calls=1,
         token_input=11, token_status="available")
    view = nodestats.refresh(state, "n")
    seg = view["segments"]["subject"]
    check(seg["tool_calls"] == 7 and seg["llm_calls"] == 4, "段工具/LLM = 会话之和")
    check(seg["token_total"] == 18, "段 token = 会话之和")
    check(view["total_s"] == 140.0, "total = 20 + 100 + 20")
    print()


def test_for_ui_compat():
    print("[NS-7] for_ui 兼容：无统计返回 None，损坏数据不崩溃")
    check(nodestats.for_ui({}, "n") is None, "无 node_stats → None")
    check(nodestats.for_ui({"node_stats": []}, "n") is None, "node_stats 非对象 → None")
    check(nodestats.for_ui({"node_stats": {"n": {"segments": "x"}}}, "n") is None,
          "segments 损坏 → None（展示层隐藏）")
    check(nodestats.for_ui({"node_stats": {"n": None}}, "n") is None, "条目损坏 → None")
    bad = {"node_stats": {"n": {"segments": {
        "entry": {"duration_s": "oops", "sessions": "junk",
                  "open_started_at": True, "token_status": "??"},
        "subject": [], "exit": None}}}}
    view = nodestats.for_ui(bad, "n")
    check(view is not None, "可归一化的损坏结构仍能出视图")
    check(view["segments"]["entry"]["duration_s"] == 0.0
          and view["segments"]["entry"]["sessions"] == [], "损坏字段归一化为零值")
    check("open_started_at" not in view["segments"]["entry"], "内部字段不泄漏")

    # state.json 往返（JSON 序列化后仍可读取）
    state = {}
    nodestats.open_segment(state, "n", "entry", now=10.0)
    nodestats.close_segment(state, "n", "entry", now=20.0)
    _add(state, segment="subject", session_id="j1", llm_calls=1,
         token_input=9, token_status="available")
    roundtrip = json.loads(json.dumps(state))
    view = nodestats.for_ui(roundtrip, "n")
    check(view["segments"]["entry"]["duration_s"] == 10.0
          and view["segments"]["subject"]["token_total"] == 9,
          "JSON 往返后可读")
    print()


def test_result_object_adapter():
    print("[NS-9] 直接消费 SessionResult 字段（含 T1 未落地时的旧对象）")

    class FullResult:
        session_id = "sid-1"
        duration_s = 12.5
        tool_calls = 4
        llm_calls = 3
        token_input = 10
        token_output = 20
        token_cache_creation = 30
        token_cache_read = 40
        token_status = "available"

    state = {}
    rec = nodestats.add_session(state, "n", "exit", kind="exit",
                                started_at=100.0, ended_at=112.5, result=FullResult())
    check(rec["session_id"] == "sid-1" and rec["duration_s"] == 12.5, "取 session_id/duration")
    check(rec["tool_calls"] == 4 and rec["llm_calls"] == 3, "取工具/LLM 次数")
    check(rec["token_total"] == 100 and rec["token_status"] == "available",
          "token 四项求和")

    class OldResult:  # T1 之前的 SessionResult：没有统计字段
        session_id = "sid-2"
        duration_s = 5.0

    rec2 = nodestats.add_session(state, "n", "subject", kind="subject",
                                 started_at=200.0, ended_at=205.0, result=OldResult())
    check(rec2["tool_calls"] == 0 and rec2["llm_calls"] == 0, "缺失字段降级为 0")
    check(rec2["token_total"] == 0 and rec2["token_status"] == "available",
          "无模型响应按 token 0 可得（不误报不可得）")
    print()


def test_failure_isolation():
    print("[NS-8] 采集失败隔离：不抛异常、留诊断")
    check(nodestats.open_segment([], "n", "entry") is False, "非 dict state 不抛异常")
    state = {}
    check(nodestats.add_session(state, "n", "bogus", kind="subject") is None,
          "未知段不抛异常")
    diag = state.get("node_stats", {}).get("_diagnostics") or []
    check(bool(diag), "失败写入诊断")
    # 合法调用不受诊断影响
    check(nodestats.open_segment(state, "n", "entry", now=1.0), "后续合法调用仍可用")
    nodestats.close_segment(state, "n", "entry", now=2.0)
    check(nodestats.refresh(state, "n")["segments"]["entry"]["duration_s"] == 1.0,
          "失败后统计仍可继续累计")
    print()


if __name__ == "__main__":
    test_segment_intervals()
    test_open_twice_discards_stale_start()
    test_empty_segment()
    test_session_merge()
    test_token_tristate()
    test_total_and_aggregates()
    test_for_ui_compat()
    test_result_object_adapter()
    test_failure_isolation()
    print("ALL PASS")
