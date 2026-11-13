"""pi AgentRunner：命令构造、JSONL 解析、续跑、turn 预算、隔离与错误分支。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.runner import AgentRunner, ConfigError, SessionResult  # noqa: E402

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
    os.environ["GDD_AGENT"] = "pi"
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
        "GDD_AGENT", "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_STEER",
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
        "GDD_AGENT", "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_STEER",
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
        "GDD_AGENT", "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_STEER",
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
        "GDD_AGENT", "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_STEER",
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


def test_claude_agent():
    print("[C3-5] claude 协议")
    d = tempfile.mkdtemp(prefix="gdd-runner-claude-")
    saved = {k: os.environ.get(k) for k in (
        "GDD_AGENT", "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_AGENT_EXTRA_ARGS",
        "GDD_AGENT_MODEL", "GDD_SUBJECT_RESUME", "GDD_STEER", "GDD_STEER_HOME",
        "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR", "GDD_PI_SESSION_DIR")}
    try:
        settings = os.path.join(d, "settings.json")
        with open(settings, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "baseUrl": "https://api.example.com/v1",
                       "apiKey": "sk-super-secret-value", "model": "m-default"}, f)
        os.environ["GDD_PI_SETTINGS"] = settings
        os.environ["GDD_AGENT"] = "claude"
        os.environ["GDD_AGENT_BIN"] = "/tmp/claude"
        os.environ["GDD_STEER"] = "off"
        os.environ.pop("GDD_AGENT_PRE_ARGS", None)
        os.environ.pop("GDD_AGENT_EXTRA_ARGS", None)
        os.environ.pop("GDD_AGENT_MODEL", None)
        os.environ.pop("GDD_SUBJECT_RESUME", None)
        runner = AgentRunner(d)
        cmd = runner._build_cmd(["Read", "Grep", "Bash"], 12, resume_sid="sid-9")
        check(cmd[0] == "/tmp/claude", "用 GDD_AGENT_BIN 指定的 claude")
        check(cmd[cmd.index("--output-format") + 1] == "stream-json",
              "-p 流式 stream-json")
        check("--verbose" in cmd and "--include-partial-messages" in cmd,
              "流式带 verbose 与 partial")
        check(cmd[cmd.index("--max-turns") + 1] == "12", "--max-turns")
        check(cmd[cmd.index("--resume") + 1] == "sid-9", "续跑用 --resume")
        check(cmd[cmd.index("--allowedTools") + 1] == "Read Grep Bash",
              "工具名保持原样")
        denied = set(cmd[cmd.index("--disallowedTools") + 1].split())
        check(denied == {"Write", "Edit", "MultiEdit", "Glob"},
              "未声明的写和未用到的工具被拒绝")
        check("--tools" not in cmd, "不用 pi 的 --tools 来收窄")
        full = runner._build_cmd(
            ["Read", "Write", "Edit", "MultiEdit", "Grep", "Glob", "Bash"], 4)
        check("--disallowedTools" not in full, "声明了全部工具时不追加拒绝名单")
        check(cmd.count("--dangerously-skip-permissions") == 1, "无人应答时跳过权限提示")
        for banned in ("--print", "--mode", "--session-id", "--tools",
                       "--extension", "--skill", "--plugin-dir"):
            check(banned not in cmd, f"claude 命令不含 pi 参数 {banned}")
        check("sk-super-secret-value" not in " ".join(cmd), "不把 pi API key 放进 argv")
        os.environ["GDD_PI_SETTINGS"] = os.path.join(d, "missing-settings.json")
        no_settings = runner._build_cmd(["Read"], 4)
        check("-p" in no_settings and "--print" not in no_settings,
              "缺少 pi 设置文件时仍构造 claude 命令")
        os.environ["GDD_AGENT_BIN"] = os.path.join(d, "no-such-claude")
        missing = AgentRunner(d).run("hi", allowed_tools=["Read"], max_turns=2)
        check(not missing.ok and "no-such-claude" in missing.error
              and "设置页" not in missing.error,
              "找不到 claude 时不指向设置页")
        os.environ["GDD_AGENT_BIN"] = "/tmp/claude"

        os.environ["GDD_SUBJECT_RESUME"] = "off"
        check("--resume" not in runner._build_cmd(["Read"], 4, resume_sid="sid-9"),
              "GDD_SUBJECT_RESUME=off 不传 --resume")
        os.environ.pop("GDD_SUBJECT_RESUME", None)
        os.environ["GDD_AGENT_MODEL"] = "sonnet"
        modeled = runner._build_cmd(["Read"], 4)
        check(modeled[modeled.index("--model") + 1] == "sonnet",
              "claude 的 --model 不加 custom/ 前缀")
        os.environ["GDD_AGENT_EXTRA_ARGS"] = "--dangerously-skip-permissions --verbose"
        extra = runner._build_cmd(["Read"], 4)
        check(extra.count("--dangerously-skip-permissions") == 1, "跳过权限只出现一次")
        check(extra[extra.index("--output-format") + 1] == "stream-json",
              "无界面回调时也走 stream-json")
        os.environ.pop("GDD_AGENT_EXTRA_ARGS", None)
        os.environ.pop("GDD_AGENT_MODEL", None)

        plugin = os.path.join(d, "skills-plugin")
        os.makedirs(os.path.join(plugin, ".claude-plugin"), exist_ok=True)

        class Ext:
            mcp_config = os.path.join(d, "mcp.json")
            skill_dirs = [os.path.join(d, "skills", "one")]
            plugin_dir = plugin
        runner.node_extensions = Ext()
        with_mcp = runner._build_cmd(["Read"], 4)
        check(with_mcp[with_mcp.index("--mcp-config") + 1] == Ext.mcp_config,
              "节点 MCP 用 --mcp-config")
        check(with_mcp[with_mcp.index("--plugin-dir") + 1] == plugin,
              "节点 skill 用 --plugin-dir")
        check("--skill" not in with_mcp and "--strict-mcp-config" not in with_mcp,
              "claude 不使用 --skill，也不收紧 MCP 列表")
        runner.node_extensions = None

        os.environ["GDD_STEER"] = "on"
        os.environ["GDD_STEER_HOME"] = os.path.join(d, "steer-home")
        steered = runner._build_cmd(["Read"], 4, steer=True)
        check("--settings" in steered, "主体会话挂 claude settings")
        check(steered[steered.index("--settings") + 1].endswith("claude.settings.json"),
              "settings 指向仓库外的 claude 配置")
        gate = runner._build_cmd(["Read"], 4, steer=False)
        check("--settings" not in gate, "门禁不挂 steer settings")
        os.environ["GDD_STEER"] = "off"

        os.environ["GDD_AGENT"] = "pi"
        os.environ["GDD_PI_SETTINGS"] = settings
        os.environ["GDD_AGENT_BIN"] = "/tmp/claude"
        pi_cmd = runner._build_cmd(["Read"], 4, resume_sid="sid-9")
        check("--print" in pi_cmd and "--resume" not in pi_cmd
              and pi_cmd[pi_cmd.index("--session-id") + 1] == "sid-9",
              "显式 GDD_AGENT=pi 不因文件名改走 claude")

        os.environ.pop("GDD_AGENT", None)
        os.environ.pop("GDD_PI_SETTINGS", None)
        os.environ["GDD_AGENT_BIN"] = "/opt/bin/claude"
        inferred = AgentRunner(d)._build_cmd(["Read"], 3)
        check("-p" in inferred and "--print" not in inferred, "文件名 claude 推断为 claude")

        os.environ["GDD_AGENT"] = "codex"
        try:
            AgentRunner(d)._build_cmd(["Read"], 3)
            check(False, "非法 GDD_AGENT 应失败")
        except ConfigError as e:
            check("只能是 pi 或 claude" in str(e), "非法 GDD_AGENT 说明可选值")

        ok = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "done", "session_id": "s1", "num_turns": 3,
            "total_cost_usd": 0.2,
        })
        res = AgentRunner._parse_claude_output(ok, "", 0)
        check(res.ok and res.text == "done" and res.session_id == "s1"
              and res.turns == 3 and res.cost_usd == 0.2, "成功 result")
        login = json.dumps({
            "type": "result", "subtype": "success", "is_error": True,
            "result": "Not logged in · Please run /login",
            "session_id": "s-login", "num_turns": 1, "total_cost_usd": 0,
        })
        res = AgentRunner._parse_claude_output(login, "", 1)
        check(not res.ok and "登录" in res.error and "设置页" not in res.error
              and res.session_id == "s-login" and not res.turns_exhausted,
              "未登录指向本机 claude 登录，不指向设置页")
        capped = json.dumps({
            "type": "result", "subtype": "error_max_turns", "is_error": True,
            "result": "stopped", "session_id": "s2", "num_turns": 40,
        })
        res = AgentRunner._parse_claude_output(capped, "", 0)
        check(not res.ok and res.turns_exhausted and res.session_id == "s2"
              and res.turns == 40, "error_max_turns 可续跑")
        real_cap = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({
                "type": "result", "subtype": "error_max_turns", "is_error": True,
                "num_turns": 12, "session_id": "sid-max",
                "errors": ["Reached maximum number of turns (12)"],
            }),
        ])
        res = AgentRunner._parse_claude_output(real_cap, "", 1)
        check(not res.ok and res.turns_exhausted and res.session_id == "sid-max"
              and res.turns == 12 and "maximum number of turns" in res.error,
              "没有 result 字段的 error_max_turns 仍可续跑")
        login_err = json.dumps({
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "session_id": "sid-login-err", "num_turns": 0,
            "errors": ["Not logged in · Please run /login"],
        })
        res = AgentRunner._parse_claude_output(login_err, "", 1)
        check(not res.ok and "登录" in res.error and "设置页" not in res.error
              and res.session_id == "sid-login-err" and not res.turns_exhausted,
              "errors 里的未登录也指向本机登录")
        res = AgentRunner._parse_claude_output('{"foo": 1}\n', "", 1)
        check(not res.ok and "不可解析" in res.error, "非 result 对象不当成成功")
        bare = json.dumps({"result": "bare", "session_id": "sx"})
        res = AgentRunner._parse_claude_output(bare, "", 0)
        check(not res.ok and "不可解析" in res.error,
              "没有 type 的 JSON 不当成结果")
        empty_err = json.dumps({
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "session_id": "s-empty", "num_turns": 1,
        })
        res = AgentRunner._parse_claude_output(empty_err, "", 1)
        check(not res.ok and res.error and "error_during_execution" in res.error
              and res.session_id == "s-empty",
              "无 result/errors 的失败也有可读原因")
        pretty = '{\n  "type": "result",\n  "subtype": "success",\n  "result": "pretty"\n}\n'
        res = AgentRunner._parse_claude_output(pretty, "", 0)
        check(res.ok and res.text == "pretty", "多行 JSON 仍能解析")
        noise = "stream line\n" + ok + "\n"
        res = AgentRunner._parse_claude_output(noise, "", 0)
        check(res.ok and res.text == "done", "噪声后取最后一条 result")
        res = AgentRunner._parse_claude_output("not json", "boom", 1)
        check(not res.ok and "不可解析" in res.error, "无法解析时失败")

        stub = os.path.join(d, "claude_stub.py")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(
                "import json, sys\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'type':'stream_event','session_id':'sid-c',"
                "'event':{'delta':{'type':'text_delta','text':'HelloClaude'}}}))\n"
                "print(json.dumps({'type':'assistant','session_id':'sid-c',"
                "'message':{'content':[{'type':'tool_use','id':'t1','name':'Read',"
                "'input':{'file_path':'a.md'}}]}}))\n"
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
                "'result':'HelloClaude','session_id':'sid-c','num_turns':2,"
                "'total_cost_usd':0.25}))\n"
            )
        os.environ["GDD_AGENT"] = "claude"
        os.environ["GDD_AGENT_BIN"] = sys.executable
        os.environ["GDD_AGENT_PRE_ARGS"] = stub
        os.environ["GDD_STEER"] = "off"
        os.environ.pop("GDD_PI_SETTINGS", None)
        events = []
        ran = AgentRunner(d).run(
            "hello", allowed_tools=["Read"], max_turns=5,
            on_stream=lambda ev: events.append(dict(ev)))
        check(ran.ok and ran.text == "HelloClaude" and ran.session_id == "sid-c"
              and ran.turns == 2, f"stub 会话解析成功（{ran.error}）")
        check(any(ev.get("kind") == "text" and ev.get("text") == "HelloClaude"
                  for ev in events), "流式文本")
        check(any(ev.get("kind") == "tool" and ev.get("name") == "Read"
                  and ev.get("summary") == "a.md" for ev in events), "流式工具")
    finally:
        _restore(saved)
        import shutil
        shutil.rmtree(d)
    print("[C3-5] PASS\n")


def test_agent_registry():
    """新执行端只登记一个类，命令和文件名推断就走它。"""
    print("[C3-6] 执行端登记")
    from gdd.agents import register, resolve_agent, unregister

    class StubAgent:
        name = "stub"

        @classmethod
        def match_bin(cls, base: str) -> bool:
            return base == "stub-bin"

        def prepare(self, runner):
            return None

        def build_cmd(self, runner, allowed_tools, max_turns, resume_sid="", *,
                      stream=False, steer=False):
            return ["stub-bin", "--turns", str(max_turns),
                    "steer" if steer else "gate"]

        def extra_env(self, runner, subject: bool):
            return {"STUB_AGENT": "1"} if subject else {}

        def on_event(self, obj, state, on_stream):
            return None

        def should_stop(self, state, max_turns: int) -> bool:
            return False

        def parse(self, stdout, stderr, returncode, state, *, max_turns: int):
            return SessionResult(ok=True, text="stub")

        def apply_turn_cap(self, res, state, max_turns: int):
            return res

        def launch_hint(self) -> str:
            return "stub hint"

    saved = {k: os.environ.get(k) for k in ("GDD_AGENT", "GDD_AGENT_BIN")}
    register(StubAgent)
    try:
        os.environ["GDD_AGENT"] = "stub"
        os.environ["GDD_AGENT_BIN"] = "/opt/stub-bin"
        cmd = AgentRunner(tempfile.mkdtemp())._build_cmd(["Read"], 3, steer=True)
        check(cmd == ["stub-bin", "--turns", "3", "steer"], "登记的执行端接管命令")
        os.environ.pop("GDD_AGENT", None)
        check(resolve_agent().name == "stub", "未指定 GDD_AGENT 时按 match_bin 推断")
        os.environ["GDD_AGENT"] = "nope"
        try:
            AgentRunner(tempfile.mkdtemp())._build_cmd(["Read"], 1)
            check(False, "未登记的执行端应失败")
        except ConfigError as e:
            check("只能是" in str(e) and "stub" in str(e),
                  "非法值列出已登记的执行端")
    finally:
        unregister("stub")
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("[C3-6] PASS\n")


def main():
    test_command_construction()
    test_jsonl_and_stream()
    test_turn_budget_and_errors()
    test_agent_and_session_dirs()
    test_claude_agent()
    test_agent_registry()
    print("ALL RUNNER PASSED")


if __name__ == "__main__":
    main()
