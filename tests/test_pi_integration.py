"""真实 pi CLI + 本地 mock OpenAI 兼容端点：最小会话与可处置错误。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.runner import AgentRunner  # noqa: E402

PI_BIN = os.path.join(ROOT, "node_modules", ".bin", "pi")


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def ensure_install():
    if os.path.isfile(PI_BIN) and os.access(PI_BIN, os.X_OK):
        return
    print("  npm ci（缺少 node_modules）…")
    proc = subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=ROOT,
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise SystemExit("npm ci 失败: " + (proc.stderr or proc.stdout)[-1000:])


class MockEndpoint(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "ok"
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        MockEndpoint.requests.append({
            "path": self.path,
            "auth": self.headers.get("authorization", ""),
            "body": body,
        })
        if MockEndpoint.mode == "401":
            data = b'{"error":{"message":"invalid api key","type":"invalid_request_error"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if MockEndpoint.mode == "404":
            data = json.dumps({"error": {
                "message": "model not found: mock-model",
                "type": "invalid_request_error", "code": "model_not_found",
            }}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not body.get("stream"):
            data = json.dumps({
                "id": "chatcmpl-1", "object": "chat.completion",
                "model": body.get("model", "mock"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "integration-ok"}}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if MockEndpoint.mode == "steer":
            # 第一轮要求执行一次 bash 工具；工具结果回传后的第二轮给出最终文本。
            # 用于验证 steer 扩展在真实 pi 的 tool_result 钩子里把补充送入同一会话。
            has_tool_result = any(
                isinstance(m, dict) and m.get("role") == "tool"
                for m in (body.get("messages") or []))
            if not has_tool_result:
                chunks = [
                    {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                     "created": 0, "model": body.get("model", "mock"),
                     "choices": [{"index": 0, "delta": {
                         "role": "assistant",
                         "tool_calls": [{"index": 0, "id": "call_1",
                                         "type": "function",
                                         "function": {"name": "bash",
                                                      "arguments": ""}}]},
                         "finish_reason": None}]},
                    {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                     "created": 0, "model": body.get("model", "mock"),
                     "choices": [{"index": 0, "delta": {
                         "tool_calls": [{"index": 0, "function": {
                             "arguments": "{\"command\":\"echo TOOL_RAN\"}"}}]},
                         "finish_reason": None}]},
                    {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                     "created": 0, "model": body.get("model", "mock"),
                     "choices": [{"index": 0, "delta": {},
                                  "finish_reason": "tool_calls"}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                               "total_tokens": 2}},
                ]
            else:
                chunks = [
                    {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                     "created": 0, "model": body.get("model", "mock"),
                     "choices": [{"index": 0, "delta": {
                         "role": "assistant", "content": "steer-final-ok"},
                         "finish_reason": None}]},
                    {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                     "created": 0, "model": body.get("model", "mock"),
                     "choices": [{"index": 0, "delta": {},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 2, "completion_tokens": 1,
                               "total_tokens": 3}},
                ]
            data = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n"
                            for c in chunks) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        chunks = [
            {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0,
             "model": body.get("model", "mock"),
             "choices": [{"index": 0, "delta": {"role": "assistant",
                                                "content": ""},
                          "finish_reason": None}]},
            {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0,
             "model": body.get("model", "mock"),
             "choices": [{"index": 0, "delta": {"content": "integration-ok"},
                          "finish_reason": None}]},
            {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0,
             "model": body.get("model", "mock"),
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                       "total_tokens": 3}},
        ]
        data = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n"
                        for c in chunks) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    ensure_install()
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockEndpoint)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{mock.server_address[1]}/v1"
    d = tempfile.mkdtemp(prefix="gdd-pi-int-")
    settings = os.path.join(d, "pi-settings.json")
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "baseUrl": base,
                   "apiKey": "sk-int-secret-123456", "model": "mock-model"}, f)
    saved = {k: os.environ.get(k) for k in (
        "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR", "GDD_PI_SESSION_DIR",
        "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS", "GDD_AGENT_EXTRA_ARGS",
        "GDD_AGENT_MODEL", "GDD_STEER", "GDD_STEER_HOME", "GDD_NODE_EXT_HOME",
        "PI_OFFLINE")}
    os.environ.update({
        "GDD_PI_SETTINGS": settings,
        "GDD_PI_AGENT_DIR": os.path.join(d, "agent"),
        "GDD_PI_SESSION_DIR": os.path.join(d, "sessions"),
        "GDD_AGENT_BIN": PI_BIN,
        "GDD_STEER": "off",
        "PI_OFFLINE": "1",
    })
    for key in ("GDD_AGENT_PRE_ARGS", "GDD_AGENT_EXTRA_ARGS", "GDD_AGENT_MODEL"):
        os.environ.pop(key, None)
    try:
        print("[C7-1] 真实 pi 最小会话")
        events = []
        res = AgentRunner(d).run(
            "Reply with exactly: integration-ok",
            allowed_tools=["Read"], max_turns=10,
            on_stream=lambda ev: events.append(dict(ev)))
        check(res.ok, f"exit 0 且解析成功（{res.error}）")
        check("integration-ok" in res.text, f"文本来自 assistant message（{res.text!r}）")
        check(bool(res.session_id), f"取得 session_id（{res.session_id}）")
        check(any(e.get("kind") == "text" for e in events), "流式文本回调")
        check(MockEndpoint.requests, "端点收到请求")
        req = MockEndpoint.requests[-1]
        check(req["auth"] == "Bearer sk-int-secret-123456", "auth.json key 生效")
        check(req["body"].get("model") == "mock-model", "models.json 模型生效")
        check(os.path.isfile(os.path.join(d, "agent", "models.json")),
              "隔离 agent 目录已生成 models.json")

        print("[C7-2] GDD_AGENT_MODEL 覆盖")
        MockEndpoint.requests.clear()
        os.environ["GDD_AGENT_MODEL"] = "override-model"
        res = AgentRunner(d).run("hi", allowed_tools=[], max_turns=10)
        check(res.ok, f"覆盖模型会话成功（{res.error}）")
        check(MockEndpoint.requests[-1]["body"].get("model") == "override-model",
              "请求使用 GDD_AGENT_MODEL")
        os.environ.pop("GDD_AGENT_MODEL", None)

        print("[C7-3] 401 可处置错误")
        MockEndpoint.mode = "401"
        res = AgentRunner(d).run("hi", allowed_tools=[], max_turns=10)
        check(not res.ok, "401 会话失败")
        check("设置页" in res.error, f"错误指向设置页（{res.error[:160]}）")
        MockEndpoint.mode = "ok"

        print("[C7-3b] 404 端点/模型配置错误可处置（D10）")
        MockEndpoint.mode = "404"
        res = AgentRunner(d).run("hi", allowed_tools=[], max_turns=10)
        check(not res.ok, "404 会话失败")
        check("设置页" in res.error, f"404 错误指向设置页（{res.error[:200]}）")
        check("404" in res.error and "model not found" in res.error,
              f"404 错误保留原始摘要（{res.error[:200]}）")
        check(res.config_error, "404 标记为配置错误（不当作会话失效重试）")
        MockEndpoint.mode = "ok"

        print("[C7-4] 缺少可执行文件")
        os.environ["GDD_AGENT_BIN"] = os.path.join(d, "no-such-pi")
        res = AgentRunner(d).run("hi", allowed_tools=[], max_turns=10)
        check(not res.ok and "npm ci" in res.error and "GDD_AGENT_BIN" in res.error,
              f"提示安装或覆盖（{res.error}）")

        print("[C7-5] 空 API key 时用占位凭证")
        os.environ["GDD_AGENT_BIN"] = PI_BIN
        with open(settings, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "baseUrl": base, "apiKey": "", "model": "mock-model"}, f)
        MockEndpoint.requests.clear()
        res = AgentRunner(d).run("hi", allowed_tools=[], max_turns=10)
        check(res.ok, f"空 key 仍可连接无鉴权端点（{res.error}）")
        check(MockEndpoint.requests[-1]["auth"] == "Bearer gdd-no-key",
              "空 key 写占位值，pi 不因缺凭证拒绝启动")

        print("[C7-6] 真实 pi tool_result 后 steer 送入同一会话")
        from gdd import steer as steer_mod
        os.environ["GDD_STEER"] = "on"
        os.environ["GDD_STEER_HOME"] = os.path.join(d, "steer-home")
        seed = "用户在运行中补充：请把答案写成 STEER-SEEN"
        MockEndpoint.mode = "steer"
        MockEndpoint.requests.clear()
        steer_mod.append(d, seed)
        subject = AgentRunner(d)
        subject.steer_subject = True
        res = subject.run("先执行工具再回答", allowed_tools=["Bash"],
                          max_turns=10)
        check(res.ok, f"主体会话完成（{res.error}）")
        check(res.text == "steer-final-ok", f"最终文本（{res.text!r}）")
        check(bool(res.session_id), f"同一会话有 session_id（{res.session_id}）")
        check(len(MockEndpoint.requests) >= 2,
              f"发生了工具调用后的第二轮请求（{len(MockEndpoint.requests)}）")
        msgs = (MockEndpoint.requests[-1]["body"].get("messages") or [])
        blob = json.dumps(msgs, ensure_ascii=False)
        check(any(m.get("role") == "tool" and "TOOL_RAN" in blob
                  for m in msgs), "第二轮消息含 bash 工具结果")
        check(any(m.get("role") == "user" and "STEER-SEEN" in blob
                  for m in msgs), "补充作为 user 消息进入同一会话的下一轮请求")
        check(steer_mod.pending(d) == "", "tool_result 后队列已消费")

        # 门禁会话不加载 steer 扩展：同样触发工具调用，补充不注入且队列保留。
        MockEndpoint.requests.clear()
        steer_mod.append(d, seed)
        gate = AgentRunner(d)  # steer_subject 默认 False
        res_gate = gate.run("先执行工具再回答", allowed_tools=["Bash"],
                            max_turns=10)
        check(res_gate.ok, f"门禁会话完成（{res_gate.error}）")
        gate_blob = json.dumps(
            MockEndpoint.requests[-1]["body"].get("messages") or [],
            ensure_ascii=False)
        check(gate_blob.count("TOOL_RAN") >= 1, "门禁会话也执行了工具")
        check("STEER-SEEN" not in gate_blob, "门禁会话不注入补充")
        check(steer_mod.pending(d) == seed, "门禁会话不消费队列")
        steer_mod.drain(d)
        MockEndpoint.mode = "ok"

        print("[C7-7] 真实 pi 读取 GDD 生成的 mcpServers 并连接 MCP server")
        from gdd.models import PipelineSpec
        from gdd.node_ext import prepare
        proj = os.path.join(d, "mcp-proj")
        os.makedirs(proj)
        server_py = os.path.join(proj, "mcp_server.py")
        with open(server_py, "w", encoding="utf-8") as f:
            f.write(
                'import json, sys\n'
                'def send(o):\n'
                '    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()\n'
                'for line in sys.stdin:\n'
                '    line = line.strip()\n'
                '    if not line: continue\n'
                '    msg = json.loads(line); m = msg.get("method"); mid = msg.get("id")\n'
                '    if m == "initialize":\n'
                '        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2025-06-18",'
                '"capabilities":{"tools":{}},"serverInfo":{"name":"gdd-demo","version":"1.0.0"}}})\n'
                '    elif m == "notifications/initialized": pass\n'
                '    elif m == "tools/list":\n'
                '        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[{"name":"ping",'
                '"description":"Return mcp-pong","inputSchema":{"type":"object","properties":{}}}]}})\n'
                '    elif m == "tools/call":\n'
                '        send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text",'
                '"text":"mcp-pong"}]}})\n'
                '    elif mid is not None:\n'
                '        send({"jsonrpc":"2.0","id":mid,"result":{}})\n')
        with open(os.path.join(proj, "mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"demo": {
                "command": sys.executable, "args": [server_py], "timeout": 10}}}, f)
        os.environ["GDD_NODE_EXT_HOME"] = os.path.join(d, "node-ext")
        node = PipelineSpec.from_dict({
            "id": "p", "name": "p",
            "nodes": [{"id": "work", "name": "work", "mcp": ["mcp.json"]}]}).nodes[0]
        ext = prepare(proj, node)
        check(ext is not None and os.path.isfile(ext.mcp_config),
              "prepare 生成节点 mcp 配置")
        runner = AgentRunner(proj)
        runner.node_extensions = ext
        MockEndpoint.requests.clear()
        res = runner.run("hi", allowed_tools=["Read"], max_turns=10)
        check(res.ok, f"MCP 主体会话完成（{res.error}）")
        agent_mcp = os.path.join(d, "agent", "mcp.json")
        check(os.path.isfile(agent_mcp), "mcp.json 已复制进隔离 agent 目录")
        with open(agent_mcp, encoding="utf-8") as f:
            doc = json.load(f)
        check("demo" in (doc.get("mcpServers") or {}), "agent 目录 mcpServers 含 demo")
        listing = subprocess.run(
            [PI_BIN, "mcp", "list"],
            env=dict(os.environ, PI_CODING_AGENT_DIR=os.path.join(d, "agent"),
                     PI_OFFLINE="1"),
            capture_output=True, text=True, timeout=60)
        out = (listing.stdout or "") + (listing.stderr or "")
        check(listing.returncode == 0,
              f"pi mcp list 读取 agent mcp.json 成功（{out.strip()[:200]}）")
        check("demo: connected" in out and "tools: ping" in out,
              f"pi 连接到 MCP server 并列出 ping 工具（{out.strip()[:200]}）")
        blob = json.dumps(MockEndpoint.requests[0], ensure_ascii=False)
        check("mcp_servers" in blob and "mcp__demo" in blob,
              "真实会话请求的系统提示含 mcp_servers 段与 mcp__demo")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        mock.shutdown()
        mock.server_close()
        import shutil
        shutil.rmtree(d)
    print("ALL PI INTEGRATION PASSED")


if __name__ == "__main__":
    main()
