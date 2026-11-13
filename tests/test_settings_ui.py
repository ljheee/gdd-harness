"""Web UI 设置页与 /api/settings 接口。"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd import ui  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


class Client:
    def __init__(self, base):
        self.base = base

    def req(self, path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", "X-GDD-Request": "test"}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def get(self, path):
        return self.req(path)[1]

    def put(self, path, body):
        return self.req(path, "PUT", body)

    def post(self, path, body):
        return self.req(path, "POST", body)


class MockEndpoint(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_ui(workdir):
    # 控制台默认是鉴权模式；本测试直连 HTTP、不实现登录，显式启用本地模式（免登录）。
    ui.Handler.local_mode = True
    ui.Handler.root = workdir
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def main():
    d = tempfile.mkdtemp(prefix="gdd-settings-ui-")
    settings = os.path.join(d, "pi-settings.json")
    old = os.environ.get("GDD_PI_SETTINGS")
    os.environ["GDD_PI_SETTINGS"] = settings
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockEndpoint)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_url = f"http://127.0.0.1:{mock.server_address[1]}/v1"
    srv, base = start_ui(d)
    c = Client(base)
    try:
        print("[C6-1] 页面结构")
        html = urllib.request.urlopen(base + "/", timeout=10).read().decode()
        for token in ("设置", "setBase", "setKey", "setModel", "saveSettings",
                      "testSettings", "测试连通性"):
            check(token in html, f"页面含 {token}")
        check("sk-" not in html, "页面源码不含明文 key")

        print("[C6-2] 保存/读取/脱敏/权限")
        code, j = c.put("/api/settings", {
            "baseUrl": mock_url, "apiKey": "sk-secret-abcdefgh", "model": "mock-m"})
        check(code == 200 and j["ok"], f"保存成功（{code} {j}）")
        check(j["hasApiKey"] and "sk-secret-abcdefgh" not in json.dumps(j),
              "PUT 响应不含明文 key")
        mode = stat.S_IMODE(os.stat(settings).st_mode)
        check(mode == 0o600, f"设置文件 0600（{oct(mode)}）")
        got = c.get("/api/settings")
        check(got["ok"] and got["baseUrl"] == mock_url and got["model"] == "mock-m",
              "GET 返回保存值")
        check("sk-secret-abcdefgh" not in json.dumps(got), "GET 脱敏")
        check("****" in got["apiKeyMasked"], "masked 字段")

        print("[C6-3] 空 key 保留、非法输入 400")
        code, j = c.put("/api/settings", {"baseUrl": mock_url, "apiKey": "", "model": "m2"})
        check(code == 200 and j["model"] == "m2", "空 key 保留并更新模型")
        code, j = c.put("/api/settings", {"baseUrl": "ftp://x", "model": "m"})
        check(code == 400 and not j["ok"], "非法 URL 400")
        code, j = c.put("/api/settings", {"baseUrl": mock_url, "model": ""})
        check(code == 400 and not j["ok"], "空模型 400")
        code, j = c.req("/api/settings", "PUT", {"baseUrl": mock_url, "model": "m"},
                        headers={"X-GDD-Request": ""})
        check(code == 403, "缺少同源头被拒绝")

        print("[C6-4] 连通性测试")
        code, j = c.post("/api/settings/test", {"baseUrl": mock_url, "model": "mock-m"})
        check(code == 200 and j["ok"] and j["sample"] == "pong", f"测试成功（{j}）")
        code, j = c.post("/api/settings/test", {"baseUrl": "http://127.0.0.1:1/v1",
                                                "model": "x"})
        check(code == 502 and not j["ok"], "连接失败 502")
        check("sk-secret-abcdefgh" not in json.dumps(j), "错误不含明文 key")

        print("[C6-5] 重启后设置保留")
        srv.shutdown()
        srv.server_close()
        srv2, base2 = start_ui(d)
        try:
            got = Client(base2).get("/api/settings")
            check(got["model"] == "m2" and got["hasApiKey"], "重启后读取同一设置")
        finally:
            srv2.shutdown()
            srv2.server_close()
    finally:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        mock.shutdown()
        mock.server_close()
        if old is None:
            os.environ.pop("GDD_PI_SETTINGS", None)
        else:
            os.environ["GDD_PI_SETTINGS"] = old
        import shutil
        shutil.rmtree(d)
    print("ALL SETTINGS UI PASSED")


if __name__ == "__main__":
    main()
