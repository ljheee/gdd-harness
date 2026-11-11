"""节点级多个 MCP 与 skill。

运行：python3 tests/test_node_ext.py

N1 解析多个 MCP/skill，空列表不进 to_dict（指纹不变）
N2 非法名字、.. 逃逸、缺 SKILL.md 拒绝
N3 Claude 命令带一个 --mcp-config 和多个 --plugin-dir；Codex 不带
N4 主体会话挂上扩展，exit 门禁看不到
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = 'GDD-VERDICT: {"verdict": "pass", "reason": "ok", "evidence": ["e"], "feedback": ""}'


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _skill(parent, name, body="做一件具体的事"):
    d = os.path.join(parent, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: {body}\n---\n\n# {name}\n")
    return d


def main():
    home = tempfile.mkdtemp(prefix="gdd-node-ext-home-")
    os.environ["GDD_NODE_EXT_HOME"] = home
    from gdd.models import PipelineSpec
    from gdd.node_ext import prepare
    from gdd.orchestrator import Orchestrator
    from gdd.runner import AgentRunner, SessionResult
    import gdd.orchestrator as orch

    print("[N1] 多个 MCP/skill 往返，空配置不改变指纹")
    bare = {"id": "p", "name": "p", "nodes": [{"id": "n", "name": "n", "description": "d"}]}
    plain = PipelineSpec.from_dict(bare)
    node = plain.nodes[0].to_dict()
    check("mcp" not in node and "skills" not in node, "空 mcp/skills 不落盘")
    again = PipelineSpec.from_dict(plain.to_dict())
    check(again.to_dict() == plain.to_dict(), "无扩展字段时往返稳定")

    spec = {
        "id": "p", "name": "p",
        "nodes": [{
            "id": "work", "name": "干活",
            "mcp": [
                {"name": "fs", "command": "npx", "args": ["-y", "server"], "env": {"TOKEN": "t"}},
                {"name": "remote", "url": "https://example.com/mcp"},
                "mcp/extra.json",
            ],
            "skills": ["skills/review", {"path": "skills/test", "name": "tester"}],
        }],
    }
    loaded = PipelineSpec.from_dict(spec)
    back = loaded.nodes[0].to_dict()
    check(len(back["mcp"]) == 3 and len(back["skills"]) == 2, "三个 MCP、两个 skill")
    check(back["mcp"][0]["command"] == "npx" and back["mcp"][1]["url"].startswith("https://"),
          "stdio 与 http 都保留")
    check(back["skills"][1]["name"] == "tester", "skill 名字保留")
    roundtrip = PipelineSpec.from_dict(loaded.to_dict()).to_dict()
    check(roundtrip["nodes"][0]["mcp"] == back["mcp"], "mcp 往返一致")

    print("[N2] 拒绝非法配置")
    def boom(node_dict):
        try:
            PipelineSpec.from_dict({"id": "p", "name": "p", "nodes": [node_dict]})
        except ValueError:
            return True
        return False

    base = {"id": "n", "name": "n"}
    check(boom({**base, "mcp": [{"name": "-evil", "command": "npx"}]}), "MCP 名字不能以 - 开头")
    check(boom({**base, "mcp": [{"name": "a", "command": "npx"}, {"name": "a", "url": "https://x"}]}),
          "MCP 名字重复")
    check(boom({**base, "skills": ["../outside"]}), "skill 相对路径不能逃出项目根")
    check(boom({**base, "skills": ["skills/foo/../bar"]}), "skill 路径中间的 .. 也拒绝")
    check(boom({**base, "mcp": ["../mcp.json"]}), "mcp 文件路径不能逃出项目根")
    check(boom({**base, "mcp": [{"name": "fs", "command": ["npx"]}]}), "command 必须是字符串")

    proj = tempfile.mkdtemp(prefix="gdd-node-ext-proj-")
    skills = os.path.join(proj, "skills")
    _skill(skills, "review")
    _skill(skills, "tester")
    with open(os.path.join(proj, "mcp-extra.json"), "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"extra": {"command": "echo", "args": ["hi"]}}}, f)
    node = PipelineSpec.from_dict({
        "id": "p", "name": "p",
        "nodes": [{
            "id": "work", "name": "干活",
            "mcp": [
                {"name": "fs", "command": "npx", "args": ["-y", "server"], "env": {"TOKEN": "t"}},
                {"name": "remote", "url": "https://example.com/mcp",
                 "headers": {"Authorization": "Bearer t"}},
                "mcp-extra.json",
            ],
            "skills": ["skills/review", {"path": "skills/tester", "name": "tester"}],
        }],
    }).nodes[0]
    ext = prepare(proj, node)
    check(ext is not None and os.path.isfile(ext.mcp_config), "生成 mcp 配置")
    cfg = os.path.realpath(ext.mcp_config)
    root_real = os.path.realpath(proj)
    check(cfg != root_real and not cfg.startswith(root_real + os.sep), "mcp 配置不在项目目录内")
    with open(ext.mcp_config, encoding="utf-8") as f:
        servers = json.load(f)["mcpServers"]
    check(set(servers) == {"fs", "remote", "extra"}, f"三个 server（{sorted(servers)}）")
    check(servers["fs"]["args"] == ["-y", "server"], "stdio 参数保留")
    check(servers["remote"]["url"] == "https://example.com/mcp", "http url 保留")
    check(len(ext.skill_dirs) == 2, f"两个 skill 各一个目录（{ext.skill_dirs}）")
    names = {os.path.basename(p) for p in ext.skill_dirs}
    check(names == {"review", "tester"}, f"暂存目录有两个 skill（{names}）")
    check(all(os.path.isfile(os.path.join(p, "SKILL.md")) for p in ext.skill_dirs),
          "每个 skill 目录都有 SKILL.md")
    os.makedirs(os.path.join(proj, "skills", "nope"), exist_ok=True)
    missing = PipelineSpec.from_dict({
        "id": "p", "name": "p",
        "nodes": [{"id": "work", "name": "干活", "skills": ["skills/nope"]}],
    }).nodes[0]
    try:
        prepare(proj, missing)
        check(False, "缺 SKILL.md 应拒绝")
    except ValueError as e:
        check("SKILL.md" in str(e), "缺 SKILL.md 说明原因")

    print("[N5] 路径、文件 MCP、权限、残留配置")
    outside = tempfile.mkdtemp(prefix="gdd-node-ext-out-")
    os.symlink(outside, os.path.join(proj, "skills", "linked"))
    linked = PipelineSpec.from_dict({
        "id": "p", "name": "p",
        "nodes": [{"id": "work", "name": "干活", "skills": ["skills/linked"]}],
    }).nodes[0]
    try:
        prepare(proj, linked)
        check(False, "符号链接逃出项目根应拒绝")
    except ValueError as e:
        check("项目根" in str(e), "符号链接逃出项目根要说明原因")

    with open(os.path.join(proj, "mcp-notype.json"), "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"remote": {"url": "https://example.com/mcp"}}}, f)
    notype = PipelineSpec.from_dict({
        "id": "p", "name": "p",
        "nodes": [{"id": "work", "name": "干活", "mcp": ["mcp-notype.json"]}],
    }).nodes[0]
    try:
        prepare(proj, notype)
        check(False, "文件 MCP 缺 type 应拒绝")
    except ValueError as e:
        check("type" in str(e), "文件 MCP 缺 type 要说明原因")

    with open(os.path.join(proj, "mcp-http.json"), "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {
            "remote": {"type": "http", "url": "https://example.com/mcp", "timeout": 5},
        }}, f)
    http_node = PipelineSpec.from_dict({
        "id": "p", "name": "p",
        "nodes": [{"id": "work", "name": "干活", "mcp": ["mcp-http.json"]}],
    }).nodes[0]
    http_ext = prepare(proj, http_node)
    with open(http_ext.mcp_config, encoding="utf-8") as f:
        remote = json.load(f)["mcpServers"]["remote"]
    check(remote.get("type") == "http" and remote.get("timeout") == 5, "文件 MCP 保留 type 和额外字段")
    check(os.stat(http_ext.mcp_config).st_mode & 0o777 == 0o600, "mcp.json 仅所有者可读")
    check(os.stat(os.path.dirname(http_ext.mcp_config)).st_mode & 0o777 == 0o700,
          "扩展目录仅所有者可进入")

    stale = prepare(proj, node)
    stale_cfg = stale.mcp_config
    check(os.path.isfile(stale_cfg), "先有一份带 token 的配置")
    cleared = PipelineSpec.from_dict({
        "id": "p", "name": "p",
        "nodes": [{"id": "work", "name": "干活"}],
    }).nodes[0]
    check(prepare(proj, cleared) is None, "清空声明后不再挂扩展")
    check(not os.path.exists(stale_cfg), "清空声明后删掉旧 mcp.json")

    named = PipelineSpec.from_dict({
        "id": "p", "name": "p",
        "nodes": [{"id": "work", "name": "干活",
                   "skills": [{"path": "skills/review", "name": "tester"}]}],
    }).nodes[0]
    named_ext = prepare(proj, named)
    staged_md = os.path.join(named_ext.skill_dirs[0], "SKILL.md")
    with open(staged_md, encoding="utf-8") as f:
        staged_text = f.read()
    check("name: tester" in staged_text.split("---", 2)[1], "指定的 skill 名字写进 SKILL.md")

    def reject_file(servers, needle):
        with open(os.path.join(proj, "mcp-bad.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f)
        bad = PipelineSpec.from_dict({
            "id": "p", "name": "p",
            "nodes": [{"id": "work", "name": "干活", "mcp": ["mcp-bad.json"]}],
        }).nodes[0]
        try:
            prepare(proj, bad)
        except ValueError as e:
            return needle in str(e)
        return False

    check(reject_file(
        {"fs": {"command": "echo", "headers": {"X": "a\r\nInjected: b"}}}, "headers"),
          "command server 的 headers 不能含换行")
    check(reject_file(
        {"remote": {"type": "http", "url": "https://example.com/mcp", "env": {"TOKEN": "a\nb"}}},
        "env"), "http server 的 env 不能含换行")
    check(reject_file(
        {"remote": {"type": "http", "url": "https://example.com/mcp", "args": ["a\nb"]}},
        "args"), "http server 的 args 不能含换行")

    outside_file = os.path.join(tempfile.mkdtemp(prefix="gdd-node-ext-secret-"), "secret.md")
    with open(outside_file, "w", encoding="utf-8") as f:
        f.write("OUTSIDE-SECRET")
    linked_skill = os.path.join(proj, "skills", "linked-file")
    os.makedirs(linked_skill, exist_ok=True)
    os.symlink(outside_file, os.path.join(linked_skill, "SKILL.md"))
    leaked = PipelineSpec.from_dict({
        "id": "p", "name": "p",
        "nodes": [{"id": "work", "name": "干活", "skills": ["skills/linked-file"]}],
    }).nodes[0]
    try:
        prepare(proj, leaked)
        check(False, "skill 内指向目录外的符号链接应拒绝")
    except ValueError as e:
        check("符号链接" in str(e), "skill 内指向目录外的符号链接要说明原因")

    print("[N3] pi 命令挂 skill，MCP 写入隔离 agent 目录")
    ext = prepare(proj, node)  # 前面的清理可能删掉了旧暂存目录，重新生成
    settings = os.path.join(proj, "pi-settings.json")
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "baseUrl": "https://api.example.com/v1",
                   "apiKey": "k", "model": "m"}, f)
    os.environ["GDD_AGENT_BIN"] = sys.executable
    os.environ["GDD_PI_SETTINGS"] = settings
    os.environ["GDD_PI_AGENT_DIR"] = os.path.join(proj, "agent-dir")
    os.environ["GDD_PI_SESSION_DIR"] = os.path.join(proj, "session-dir")
    os.environ["GDD_STEER"] = "off"
    os.environ.pop("GDD_AGENT_PRE_ARGS", None)
    os.environ.pop("GDD_AGENT_EXTRA_ARGS", None)
    runner = AgentRunner(proj)
    runner.node_extensions = ext
    cmd = runner._build_cmd(["Read"], 5)
    skills = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--skill"]
    check(skills == ext.skill_dirs, "每个 skill 目录一个 --skill")
    check("--mcp-config" not in cmd and "--plugin-dir" not in cmd,
          "不再使用 Claude 专用 MCP/插件参数")
    agent_dir = runner._prepare_agent_dir("https://api.example.com/v1", "k", "m")
    with open(os.path.join(agent_dir, "mcp.json"), encoding="utf-8") as f:
        check("fs" in json.load(f)["mcpServers"], "MCP 写入 agent 目录 mcp.json")
    runner.node_extensions = None
    cmd = runner._build_cmd(["Read"], 5)
    check("--skill" not in cmd, "无扩展时不挂 skill")
    runner._prepare_agent_dir("https://api.example.com/v1", "k", "m")
    check(not os.path.exists(os.path.join(agent_dir, "mcp.json")),
          "无扩展时清掉旧 mcp.json")

    print("[N4] 主体挂扩展，门禁不挂")
    seen = []

    class Rec(AgentRunner):
        def run(self, prompt, *, allowed_tools, max_turns, log_path=None,
                resume_sid="", on_stream=None):
            ext_now = getattr(self, "node_extensions", None)
            seen.append((("gate" if "GDD-VERDICT" in prompt else "subject"), ext_now))
            if "GDD-VERDICT" in prompt:
                return SessionResult(ok=True, text=PASS, session_id="g")
            return SessionResult(ok=True, text="ok", session_id="s")

    orig = orch.AgentRunner
    orch.AgentRunner = Rec
    try:
        work = tempfile.mkdtemp(prefix="gdd-node-ext-work-")
        sk = os.path.join(work, "skills")
        _skill(sk, "review")
        o = Orchestrator(work)
        o.init({
            "id": "ext", "name": "ext",
            "git_delivery": {"enabled": False},
            "nodes": [{
                "id": "work", "name": "干活", "description": "完成",
                "mcp": [{"name": "fs", "command": "npx", "args": ["-y", "server"]}],
                "skills": ["skills/review"],
                "exit": {"goal": "有产出"},
            }],
        }, "需求")
        status = o.run()
    finally:
        orch.AgentRunner = orig
    check(status == "completed", f"跑完（{status}）")
    subjects = [ext for kind, ext in seen if kind == "subject"]
    gates = [ext for kind, ext in seen if kind == "gate"]
    check(subjects and all(ext is not None and ext.mcp_config for ext in subjects),
          "主体会话带 mcp 配置")
    check(all(ext.skill_dirs for ext in subjects), "主体会话带 skill 目录")
    check(gates and all(ext is None for ext in gates), "门禁会话没有节点扩展")

    print("[N6] 加载失败不清掉已保存的主体会话")
    resume_work = tempfile.mkdtemp(prefix="gdd-node-ext-resume-")
    os.makedirs(os.path.join(resume_work, "skills", "nope"), exist_ok=True)
    calls = []

    class ResumeRec(AgentRunner):
        def run(self, prompt, *, allowed_tools, max_turns, log_path=None,
                resume_sid="", on_stream=None):
            calls.append(resume_sid)
            return SessionResult(ok=True, text="ok", session_id="new")

    orch.AgentRunner = ResumeRec
    try:
        o = Orchestrator(resume_work)
        o.init({
            "id": "ext", "name": "ext",
            "git_delivery": {"enabled": False},
            "nodes": [{
                "id": "work", "name": "干活", "description": "完成",
                "skills": ["skills/nope"],
            }],
        }, "需求")
        ns = o.state["node_states"]["work"]
        ns["phase"] = "working"
        ns["sessions"] = {"subject": "keep-me"}
        o._phase_working(o.pipeline.nodes[0], ns, ResumeRec(resume_work))
    finally:
        orch.AgentRunner = orig
    check(calls == [], "加载失败不启动会话")
    check(ns["sessions"].get("subject") == "keep-me", "加载失败保留原会话")
    check(not any(e.get("event") == "session_resume_fail" for e in o.state["history"]),
          "加载失败不记成会话失效")

    print("ALL NODE EXT PASSED")


if __name__ == "__main__":
    main()
