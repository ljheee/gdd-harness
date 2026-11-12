"""T1：会话指标解析。

覆盖：Claude/pi 工具调用去重、LLM 次数口径、token 四项求和与不可得判定、
runner 非流式路径的字段透传、日志新增字段不破坏既有字段。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.runner import AgentRunner, SessionResult  # noqa: E402
from gdd.agents.claude import ClaudeAgent  # noqa: E402
from gdd.agents.pi import PiAgent  # noqa: E402

PI_STUB = os.path.join(ROOT, "tests", "pi_stub.py")


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def _claude_parse(events, stderr="", returncode=0):
    stdout = "\n".join(json.dumps(e) for e in events)
    return ClaudeAgent().parse(stdout, stderr, returncode, None, max_turns=0)


def _pi_parse(events, returncode=0):
    stdout = "\n".join(json.dumps(e) for e in events)
    return PiAgent().parse(stdout, "", returncode, None, max_turns=0)


def _tool_use(tid, name="Read", path="a.md"):
    block = {"type": "tool_use", "name": name, "input": {"file_path": path}}
    if tid:
        block["id"] = tid
    return block


def _pi_message(text="", usage=None):
    message = {"role": "assistant", "stopReason": "stop",
               "content": [{"type": "text", "text": text}] if text else []}
    if usage is not None:
        message["usage"] = usage
    return message


def test_claude_dedupe_and_usage():
    print("[T1-1] Claude 去重计数与 result.usage 求和")
    events = [
        {"type": "assistant",
         "message": {"id": "m1", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant",
         "message": {"id": "m1", "content": [_tool_use("t1")]}},
        # 同一 message id / tool id 的重复流式事件：不得重复计数
        {"type": "assistant",
         "message": {"id": "m1", "content": [_tool_use("t1")]}},
        {"type": "assistant",
         "message": {"id": "m2", "content": [_tool_use("t2"), _tool_use("t3")]}},
        {"type": "assistant",
         "message": {"id": "m2", "content": [_tool_use("t2"), _tool_use("t3")]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "done", "session_id": "s1", "num_turns": 2,
         "usage": {"input_tokens": 10, "output_tokens": 20,
                   "cache_creation_input_tokens": 3,
                   "cache_read_input_tokens": 40}},
    ]
    res = _claude_parse(events)
    check(res.ok and res.turns == 2, "result 仍按 num_turns 解析")
    check(res.llm_calls == 2, "LLM 次数 = 唯一 assistant message id 数")
    check(res.tool_calls == 3, "工具调用 = 唯一 tool_use id 数（重复事件不增加）")
    check((res.token_input, res.token_output, res.token_cache_creation,
           res.token_cache_read) == (10, 20, 3, 40), "token 四分项取 result.usage")
    check(res.token_status == "available", "完整 usage 标记 available")
    print("[T1-1] PASS\n")


def test_claude_usage_fallback():
    print("[T1-2] Claude usage 缺失时汇总 message_delta")
    events = [
        {"type": "assistant",
         "message": {"id": "m1", "content": [_tool_use("t1")]}},
        {"type": "stream_event",
         "event": {"type": "message_delta",
                   "usage": {"input_tokens": 5, "output_tokens": 2,
                             "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 7}}},
        {"type": "stream_event",
         "event": {"type": "message_delta",
                   "usage": {"input_tokens": 3, "output_tokens": 4,
                             "cache_creation_input_tokens": 1,
                             "cache_read_input_tokens": 0}}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "done", "session_id": "s2", "num_turns": 1},
    ]
    res = _claude_parse(events)
    check((res.token_input, res.token_output, res.token_cache_creation,
           res.token_cache_read) == (8, 6, 1, 7),
          "result.usage 缺失时四项来自 message_delta 累加")
    check(res.token_status == "available", "兜底 usage 非 0 标记 available")
    print("[T1-2] PASS\n")


def test_claude_unavailable():
    print("[T1-3] Claude 有响应但 usage 全 0 → 不可得")
    events = [
        {"type": "assistant",
         "message": {"id": "m1", "content": [_tool_use("t1")]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "done", "session_id": "s3", "num_turns": 1,
         "usage": {"input_tokens": 0, "output_tokens": 0,
                   "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 0}},
    ]
    res = _claude_parse(events)
    check(res.llm_calls == 1 and res.tool_calls == 1, "计数不受 usage 影响")
    check(sum((res.token_input, res.token_output, res.token_cache_creation,
               res.token_cache_read)) == 0, "四项均为 0")
    check(res.token_status == "unavailable", "全 0 usage 标记 unavailable")
    print("[T1-3] PASS\n")


def test_claude_no_model_response():
    print("[T1-4] Claude 无模型响应 → token 0 且 available")
    events = [{"type": "result", "subtype": "success", "is_error": False,
               "result": "done", "session_id": "s4", "num_turns": 0}]
    res = _claude_parse(events)
    check(res.llm_calls == 0 and res.tool_calls == 0, "无响应时计数为 0")
    check(res.token_status == "available", "无模型响应不标记不可得")
    print("[T1-4] PASS\n")


def test_claude_no_id_tools():
    print("[T1-5] Claude 无 tool id 时每次事件各计 1")
    events = [
        {"type": "assistant",
         "message": {"id": "m1", "content": [_tool_use(None)]}},
        {"type": "assistant",
         "message": {"id": "m1", "content": [_tool_use(None)]}},
    ]
    res = _claude_parse(events)
    check(res.ok is False, "没有 result 仍按不可解析失败")
    check(res.llm_calls == 1, "message id 去重仍生效")
    check(res.tool_calls == 2, "无 id 的 tool_use 每次各计 1")
    print("[T1-5] PASS\n")


def test_pi_dedupe_and_usage():
    print("[T1-6] pi 去重计数、turn_end 与 usage 累计")
    usage1 = {"input": 10, "output": 5, "cacheRead": 100, "cacheWrite": 7,
              "cost": {"total": 0.1}}
    usage2 = {"input": 1, "output": 2, "cacheRead": 3, "cacheWrite": 4,
              "cost": {"total": 0.2}}
    message1 = _pi_message(usage=usage1)
    events = [
        {"type": "session", "id": "p1"},
        {"type": "message_end", "message": message1},
        {"type": "tool_execution_start", "toolCallId": "a",
         "toolName": "read", "args": {}},
        {"type": "tool_execution_start", "toolCallId": "a",
         "toolName": "read", "args": {}},  # 重复 toolCallId
        {"type": "tool_execution_start", "toolCallId": "b",
         "toolName": "bash", "args": {}},
        {"type": "tool_execution_start", "toolName": "read", "args": {}},
        {"type": "tool_execution_start", "toolName": "read", "args": {}},
        {"type": "message_end", "message": _pi_message(usage=usage2)},
        {"type": "turn_end"}, {"type": "turn_end"}, {"type": "turn_end"},
        # agent_end 回退观察同一消息：不得重复累计 usage
        {"type": "agent_end", "messages": [message1]},
    ]
    res = _pi_parse(events)
    check(res.ok and res.session_id == "p1", "pi 会话成功解析")
    check(res.llm_calls == 3, "LLM 次数 = turn_end 数")
    check(res.tool_calls == 4, "工具按 toolCallId 去重，无 id 每次各计 1")
    check((res.token_input, res.token_output, res.token_cache_creation,
           res.token_cache_read) == (11, 7, 11, 103),
          "token 四项按 message_end.usage 累加（无重复累计）")
    check(res.token_status == "available", "有 usage 标记 available")
    print("[T1-6] PASS\n")


def test_pi_unavailable():
    print("[T1-7] pi 有响应但 usage 全 0 → 不可得")
    events = [
        {"type": "session", "id": "p2"},
        {"type": "message_end",
         "message": _pi_message(usage={"input": 0, "output": 0,
                                       "cacheRead": 0, "cacheWrite": 0})},
        {"type": "turn_end"},
        {"type": "agent_end", "messages": []},
    ]
    res = _pi_parse(events)
    check(res.llm_calls == 1, "turn_end 计数")
    check(res.token_status == "unavailable", "全 0 usage 标记 unavailable")
    print("[T1-7] PASS\n")


def test_pi_no_model_response():
    print("[T1-8] pi 无模型响应 → token 0 且 available")
    events = [
        {"type": "session", "id": "p3"},
        {"type": "agent_end", "messages": []},
    ]
    res = _pi_parse(events)
    check(res.ok and res.llm_calls == 0 and res.tool_calls == 0,
          "无响应时计数为 0")
    check(res.token_status == "available", "无模型响应不标记不可得")
    print("[T1-8] PASS\n")


def test_dump_log_fields():
    print("[T1-9] 日志包含指标字段且保留既有字段")
    d = tempfile.mkdtemp(prefix="gdd-metrics-log-")
    try:
        path = os.path.join(d, "log.json")
        res = SessionResult(ok=True, session_id="s", turns=2, cost_usd=0.1,
                            duration_s=1.5, tool_calls=3, llm_calls=2,
                            token_input=10, token_output=20,
                            token_cache_creation=3, token_cache_read=40,
                            token_status="available")
        AgentRunner._dump_log(path, "prompt", ["cmd"], "stdout", res)
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        for key in ("cmd", "prompt", "stdout", "ok", "error", "session_id",
                    "turns", "turns_exhausted", "cost_usd", "duration_s",
                    "streamed"):
            check(key in payload, f"既有字段 {key} 保留")
        check(payload["tool_calls"] == 3 and payload["llm_calls"] == 2,
              "日志写入工具/LLM 计数")
        check(payload["token_input"] == 10 and payload["token_output"] == 20
              and payload["token_cache_creation"] == 3
              and payload["token_cache_read"] == 40
              and payload["token_status"] == "available",
              "日志写入 token 四分项与状态")
    finally:
        import shutil
        shutil.rmtree(d)
    print("[T1-9] PASS\n")


def test_runner_non_stream_wiring():
    print("[T1-10] runner 非流式路径透传 Claude 指标")
    d = tempfile.mkdtemp(prefix="gdd-metrics-run-")
    saved = {k: os.environ.get(k) for k in (
        "GDD_AGENT", "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS",
        "GDD_AGENT_EXTRA_ARGS", "GDD_AGENT_MODEL", "GDD_STEER",
        "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR", "GDD_PI_SESSION_DIR")}
    try:
        stub = os.path.join(d, "claude_metrics_stub.py")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(
                "import json, sys\n"
                "sys.stdin.read()\n"
                "evs = [\n"
                "  {'type':'assistant','session_id':'sid-m',"
                "'message':{'id':'m1','content':[{'type':'text','text':'hi'}]}},\n"
                "  {'type':'assistant','session_id':'sid-m',"
                "'message':{'id':'m1','content':[{'type':'tool_use','id':'t1',"
                "'name':'Read','input':{'file_path':'a.md'}}]}},\n"
                "  {'type':'assistant','session_id':'sid-m',"
                "'message':{'id':'m1','content':[{'type':'tool_use','id':'t1',"
                "'name':'Read','input':{'file_path':'a.md'}}]}},\n"
                "  {'type':'result','subtype':'success','is_error':False,"
                "'result':'hi','session_id':'sid-m','num_turns':1,"
                "'usage':{'input_tokens':11,'output_tokens':22,"
                "'cache_creation_input_tokens':0,"
                "'cache_read_input_tokens':33}},\n"
                "]\n"
                "for ev in evs:\n"
                "    print(json.dumps(ev))\n"
            )
        os.environ["GDD_AGENT"] = "claude"
        os.environ["GDD_AGENT_BIN"] = sys.executable
        os.environ["GDD_AGENT_PRE_ARGS"] = stub
        os.environ["GDD_STEER"] = "off"
        for key in ("GDD_AGENT_EXTRA_ARGS", "GDD_AGENT_MODEL",
                    "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR",
                    "GDD_PI_SESSION_DIR"):
            os.environ.pop(key, None)
        res = AgentRunner(d).run("hello", allowed_tools=["Read"], max_turns=5)
        check(res.ok and res.streamed is False, "无 on_stream 时仍成功解析")
        check(res.llm_calls == 1 and res.tool_calls == 1,
              "非流式路径也统计去重后的 LLM/工具次数")
        check((res.token_input, res.token_output, res.token_cache_creation,
               res.token_cache_read) == (11, 22, 0, 33),
              "非流式路径透传 token 四项")
        check(res.token_status == "available", "状态透传")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil
        shutil.rmtree(d)
    print("[T1-10] PASS\n")


def test_pi_runner_metrics():
    print("[T1-11] runner pi 会话指标透传")
    d = tempfile.mkdtemp(prefix="gdd-metrics-pi-")
    saved = {k: os.environ.get(k) for k in (
        "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR", "GDD_PI_SESSION_DIR",
        "GDD_AGENT", "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_STEER",
        "GDD_AGENT_EXTRA_ARGS", "GDD_AGENT_MODEL", "GDD_PI_STUB_SCRIPT")}
    try:
        settings = os.path.join(d, "settings.json")
        with open(settings, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "baseUrl": "https://api.example.com/v1",
                       "apiKey": "sk-test", "model": "m-default"}, f)
        script = os.path.join(d, "stub.json")
        with open(script, "w", encoding="utf-8") as f:
            json.dump([{"patterns": ["hello"], "text": "hi",
                        "tools": [{"name": "read", "args": {}},
                                  {"name": "bash", "args": {}}],
                        "turns": 2}], f)
        os.environ["GDD_PI_SETTINGS"] = settings
        os.environ["GDD_PI_AGENT_DIR"] = os.path.join(d, "agent")
        os.environ["GDD_PI_SESSION_DIR"] = os.path.join(d, "sessions")
        os.environ["GDD_AGENT"] = "pi"
        os.environ["GDD_AGENT_BIN"] = sys.executable
        os.environ["GDD_AGENT_PRE_ARGS"] = PI_STUB
        os.environ["GDD_STEER"] = "off"
        os.environ["GDD_PI_STUB_SCRIPT"] = script
        os.environ.pop("GDD_AGENT_EXTRA_ARGS", None)
        os.environ.pop("GDD_AGENT_MODEL", None)
        res = AgentRunner(d).run("hello", allowed_tools=["Read", "Bash"],
                                 max_turns=10)
        check(res.ok and res.streamed is False, "pi 无回调会话成功")
        check(res.llm_calls == 2, "llm_calls 来自 turn_end")
        check(res.tool_calls == 2, "tool_calls 来自 tool_execution_start")
        check((res.token_input, res.token_output) == (1, 1),
              "pi usage input/output 透传")
        check(res.token_status == "available", "pi usage 非 0 标记 available")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil
        shutil.rmtree(d)
    print("[T1-11] PASS\n")


def main():
    test_claude_dedupe_and_usage()
    test_claude_usage_fallback()
    test_claude_unavailable()
    test_claude_no_model_response()
    test_claude_no_id_tools()
    test_pi_dedupe_and_usage()
    test_pi_unavailable()
    test_pi_no_model_response()
    test_dump_log_fields()
    test_runner_non_stream_wiring()
    test_pi_runner_metrics()
    print("ALL SESSION METRICS PASSED")


if __name__ == "__main__":
    main()
