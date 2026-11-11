"""pi AgentRunner：命令构造、JSONL 解析、续跑、turn 预算、隔离与错误分支。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.runner import AgentRunner, SessionResult  # noqa: E402

STUB = os.path.join(ROOT, "tests", "pi_stub.py")


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def _env(d, **extra):
    settings = os.path.join(d, "settings.json")
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "baseUrl": "https://api.example.com/v1",
                   "apiKey": "sk-super-secret-value", "model": "m-default"}, f)
    os.environ["GDD_PI_SETTINGS"] = settings
    os.environ["GDD_PI_AGENT_DIR"] = os.path.join(d, "agent")
    os.environ["GDD_PI_SESSION_DIR"] = os.path.join(d, "sessions")
    os.environ["GDD_AGENT_BIN"] = sys.executable
    os.environ["GDD_AGENT_PRE_ARGS"] = STUB
    os.environ["GDD_STEER"] = "off"
    os.environ.pop("GDD_AGENT_EXTRA_ARGS", None)
    os.environ.pop("GDD_AGENT_MODEL", None)
    for k, v in extra.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _script(d, entries):
    path = os.path.join(d, "stub.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    os.environ["GDD_PI_STUB_SCRIPT"] = path
    return path


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_command_construction():
    print("[C3-1] 命令构造")
    d = tempfile.mkdtemp(prefix="gdd-runner-cmd-")
    saved = {k: os.environ.get(k) for k in (
        "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR", "GDD_PI_SESSION_DIR",
        "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_STEER",
        "GDD_AGENT_EXTRA_ARGS", "GDD_AGENT_MODEL", "GDD_SUBJECT_RESUME")}
    try:
        _env(d)
        runner = AgentRunner(d)
        cmd = runner._build_cmd(["Read", "Write", "Edit", "MultiEdit", "Grep",
                                 "Glob", "Bash", "UnknownTool"], 10)
        check("--print" in cmd and cmd[cmd.index("--mode") + 1] == "json", "print/json")
        check("--model" in cmd and cmd[cmd.index("--model") + 1] == "custom/m-default",
              "模型 custom/<id>")
        tools = cmd[cmd.index("--tools") + 1].split(",")
        check(tools == ["read", "write", "edit", "grep", "find", "bash"],
              f"Claude 工具名映射且 MultiEdit 合并 edit、未知工具丢弃（{tools}）")
        for banned in ("--output-format", "--max-turns", "--allowedTools",
                       "--settings", "--mcp-config", "--plugin-dir", "--resume"):
            check(banned not in cmd, f"命令不含 Claude 参数 {banned}")
        check("sk-super-secret-value" not in " ".join(cmd), "API key 不出现在 argv")
        gate_cmd = runner._build_cmd(["Read"], 10)
        check(not any("pi-subagents" in a for a in gate_cmd), "门禁不加载 pi-subagents")
        repo_runner = AgentRunner(ROOT)
        repo_subject = repo_runner._build_cmd(["Read"], 10, steer=True)
        repo_gate = repo_runner._build_cmd(["Read"], 10)
        check(any("pi-subagents" in a for a in repo_subject), "主体加载 pi-subagents")
        check(not any("pi-subagents" in a for a in repo_gate), "门禁不加载 pi-subagents")

        resume = runner._build_cmd(["Read"], 10, resume_sid="sid-123")
        check(resume[resume.index("--session-id") + 1] == "sid-123", "续跑带 session-id")
        os.environ["GDD_SUBJECT_RESUME"] = "off"
        no_resume = runner._build_cmd(["Read"], 10, resume_sid="sid-123")
        check("--session-id" not in no_resume, "GDD_SUBJECT_RESUME=off 不传 session-id")
        os.environ.pop("GDD_SUBJECT_RESUME", None)
        os.environ["GDD_AGENT_MODEL"] = "m-override"
        override = runner._build_cmd(["Read"], 10)
        check(override[override.index("--model") + 1] == "custom/m-override",
              "GDD_AGENT_MODEL 优先")
    finally:
        _restore(saved)
        import shutil
        shutil.rmtree(d)
    print("[C3-1] PASS\n")


def test_jsonl_and_stream():
    print("[C3-2] JSONL 解析与流式回调")
    d = tempfile.mkdtemp(prefix="gdd-runner-jsonl-")
    saved = {k: os.environ.get(k) for k in (
        "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR", "GDD_PI_SESSION_DIR",
        "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_STEER",
        "GDD_AGENT_EXTRA_ARGS", "GDD_AGENT_MODEL", "GDD_PI_STUB_SCRIPT",
        "GDD_SUBJECT_RESUME")}
    try:
        _env(d)
        _script(d, [{
            "patterns": ["hello"],
            "text": "HelloPiStream",
            "tools": [{"name": "read", "args": {"path": "docs/a.md"}},
                      {"name": "bash", "args": {"command": "echo hi"}}],
            "turns": 2,
        }])
        events = []
        res = AgentRunner(d).run(
            "hello", allowed_tools=["Read", "Bash"], max_turns=10,
            on_stream=lambda ev: events.append(dict(ev)))
        check(res.ok and res.text == "HelloPiStream", f"文本与成功（{res.error}）")
        check(res.session_id == "pi-stub-session", "session_id 来自 session header")
        check(res.turns == 2, "turn_end 计数")
        texts = "".join(e["text"] for e in events if e.get("kind") == "text")
        check(texts == "HelloPiStream", "text_delta 拼接完整")
        tools = [e for e in events if e.get("kind") == "tool"]
        check(any(t["name"] == "read" and "docs/a.md" in t["summary"] for t in tools),
              "工具摘要含路径")
        check(any(t["name"] == "bash" and "echo hi" in t["summary"] for t in tools),
              "工具摘要含命令")
        check(res.streamed is True, "有回调时标记 streamed")

        res2 = AgentRunner(d).run("hello", allowed_tools=[], max_turns=10)
        check(res2.ok and res2.streamed is False, "无回调时不标记 streamed")
    finally:
        _restore(saved)
        import shutil
        shutil.rmtree(d)
    print("[C3-2] PASS\n")


def test_turn_budget_and_errors():
    print("[C3-3] turn 预算、错误与凭证提示")
    d = tempfile.mkdtemp(prefix="gdd-runner-budget-")
    saved = {k: os.environ.get(k) for k in (
        "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR", "GDD_PI_SESSION_DIR",
        "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_STEER",
        "GDD_AGENT_EXTRA_ARGS", "GDD_AGENT_MODEL", "GDD_PI_STUB_SCRIPT",
        "GDD_SUBJECT_RESUME")}
    try:
        _env(d)
        _script(d, [{
            "patterns": ["many"], "text": "continue", "turns": 5,
        }])
        res = AgentRunner(d).run("many", allowed_tools=[], max_turns=2)
        check(not res.ok and res.turns_exhausted, "达到 turn 上限记 turns_exhausted")
        check(res.session_id == "pi-stub-session", "保留 session_id 供续跑")

        _script(d, [{
            "patterns": ["auth"], "text": "", "is_error": True,
            "stop_reason": "error", "error": "401 Unauthorized: invalid api key",
        }])
        res = AgentRunner(d).run("auth", allowed_tools=[], max_turns=5)
        check(not res.ok, "401 会话失败")
        check("设置页" in res.error, f"凭证错误提示设置页（{res.error}）")

        _script(d, [{
            "patterns": ["model404"], "text": "", "is_error": True,
            "stop_reason": "error",
            "error": ('404: {"message":"model not found: mock-model",'
                      '"type":"invalid_request_error","code":"model_not_found"}'),
        }])
        res = AgentRunner(d).run("model404", allowed_tools=[], max_turns=5)
        check(not res.ok, "404 会话失败")
        check("设置页" in res.error, f"404 配置错误提示设置页（{res.error}）")
        check("404" in res.error and "model not found" in res.error,
              f"404 错误保留原始摘要（{res.error}）")
        check(res.config_error, "404 标记为配置错误")

        _script(d, [{
            "patterns": ["abort"], "text": "partial", "aborted": True,
        }])
        res = AgentRunner(d).run("abort", allowed_tools=[], max_turns=5)
        check(not res.ok, "aborted 会话失败")

        _script(d, [{"patterns": ["bad"], "text": "x", "exit_code": 3,
                     "is_error": True, "stop_reason": "error", "error": "boom"}])
        res = AgentRunner(d).run("bad", allowed_tools=[], max_turns=5)
        check(not res.ok and "退出码 3" in res.error, "非零退出码转换为失败")

        os.environ["GDD_PI_SETTINGS"] = os.path.join(d, "missing.json")
        res = AgentRunner(d).run("hello", allowed_tools=[], max_turns=5)
        check(not res.ok and "设置页" in res.error, "缺少设置提示设置页")

        _env(d)
        os.environ["GDD_AGENT_BIN"] = os.path.join(d, "no-such-pi")
        res = AgentRunner(d).run("hello", allowed_tools=[], max_turns=5)
        check(not res.ok and "GDD_AGENT_BIN" in res.error, "缺少可执行文件提示覆盖项")
    finally:
        _restore(saved)
        import shutil
        shutil.rmtree(d)
    print("[C3-3] PASS\n")


def test_agent_and_session_dirs():
    print("[C3-4] agent/session 目录隔离")
    d = tempfile.mkdtemp(prefix="gdd-runner-dirs-")
    saved = {k: os.environ.get(k) for k in (
        "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR", "GDD_PI_SESSION_DIR",
        "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_STEER",
        "GDD_AGENT_EXTRA_ARGS", "GDD_AGENT_MODEL", "GDD_PI_STUB_SCRIPT")}
    try:
        _env(d)
        record = os.path.join(d, "record.jsonl")
        _script(d, [{"patterns": ["hi"], "text": "ok"}])
        os.environ["GDD_PI_STUB_RECORD"] = record
        res = AgentRunner(d).run("hi", allowed_tools=[], max_turns=5)
        check(res.ok, "会话成功")
        with open(record, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        check(rec["agent_dir"] == os.path.join(d, "agent"), "PI_CODING_AGENT_DIR 已隔离")
        check(rec["session_dir"] == os.path.join(d, "sessions"),
              "PI_CODING_AGENT_SESSION_DIR 已隔离")
        agent = os.path.join(d, "agent")
        models = json.load(open(os.path.join(agent, "models.json"), encoding="utf-8"))
        check("m-default" in json.dumps(models), "models.json 写入设置模型")
        check(oct(os.stat(os.path.join(agent, "auth.json")).st_mode & 0o777) == "0o600",
              "auth.json 0600")
        os.environ.pop("GDD_PI_STUB_RECORD", None)
    finally:
        _restore(saved)
        import shutil
        shutil.rmtree(d)
    print("[C3-4] PASS\n")


def main():
    test_command_construction()
    test_jsonl_and_stream()
    test_turn_budget_and_errors()
    test_agent_and_session_dirs()
    print("ALL RUNNER PASSED")


if __name__ == "__main__":
    main()
