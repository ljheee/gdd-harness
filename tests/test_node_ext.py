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
    check(boom({**base, "mcp": ["../mcp.json"]}), "mcp 文件路径不能逃出项目根")

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
    check(len(ext.plugin_dirs) == 1, f"两个 skill 打进一个插件目录（{ext.plugin_dirs}）")
    names = set(os.listdir(os.path.join(ext.plugin_dirs[0], "skills")))
    check(names == {"review", "tester"}, f"插件内有两个 skill（{names}）")
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

    print("[N3] 只有 Claude 命令挂 MCP 和 skill")
    os.environ["GDD_AGENT_BIN"] = "claude"
    os.environ.pop("GDD_AGENT_PRE_ARGS", None)
    os.environ.pop("GDD_AGENT_EXTRA_ARGS", None)
    runner = AgentRunner(proj)
    runner.node_extensions = ext
    cmd = runner._build_cmd(["Read"], 5)
    check(cmd.count("--mcp-config") == 1, "一个 --mcp-config")
    check(cmd[cmd.index("--mcp-config") + 1] == ext.mcp_config, "指向生成的配置")
    dirs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--plugin-dir"]
    check(dirs == ext.plugin_dirs, "每个插件目录一个 --plugin-dir")
    runner.node_extensions = None
    check("--mcp-config" not in runner._build_cmd(["Read"], 5), "无扩展时不挂")
    os.environ["GDD_AGENT_BIN"] = "codex"
    runner = AgentRunner(proj)
    runner.node_extensions = ext
    codex_cmd = runner._build_cmd(["Read"], 5)
    check("--mcp-config" not in codex_cmd and "--plugin-dir" not in codex_cmd,
          "codex 不注入 Claude 的 MCP/skill 参数")

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
    check(all(ext.plugin_dirs for ext in subjects), "主体会话带 skill 插件")
    check(gates and all(ext is None for ext in gates), "门禁会话没有节点扩展")

    print("ALL NODE EXT PASSED")


if __name__ == "__main__":
    main()
