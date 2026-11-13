"""控制台登录鉴权的 integration 测试（无浏览器，直连 HTTP）。

覆盖 goal.md / acceptance.feature 的 D1-D12（D13 的 Rust 探活分类由 T4 覆盖）：
  D1  默认启动即鉴权模式（子进程 `python -m gdd.ui`，不传 `--local`）
  D2  本地模式显式启用、仅回环、不可按 Host 自动判定
  D3  未登录读 API 全部拒绝且不泄露数据、磁盘不变
  D4  未登录写 API 全部拒绝且无副作用
  D5  未登录 SSE 被拒绝且不推送事件
  D6  默认账号登录成功、错误凭据失败、会话可建立
  D7  登录后既有读功能可用（设置仍脱敏）
  D8  登录后任务生命周期可用（run→approve→完成；reject→重试→完成）
  D9  登录后模板 CRUD 与设置读写可用，非法输入仍按既有语义拒绝
  D10 登录后 SSE 正常推送
  D11 退出登录与短 TTL 会话过期后重新被拒绝
  D12 凭据不以明文保存或回显

运行：python3.12 tests/test_auth_ui.py
"""
from __future__ import annotations

import hashlib
import http.client
import http.cookiejar
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd import console_auth  # noqa: E402
from gdd import ui as gdd_ui  # noqa: E402
from gdd.console_auth import ConsoleAuth, SessionStore  # noqa: E402

STUB = os.path.join(ROOT, "tests", "pi_stub.py")
PYTHON = sys.executable
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "gdd2026"
UNAUTH_ERROR = "未登录或登录已失效"
BAD_LOGIN_ERROR = "用户名或密码错误"

ENV_KEYS = (
    "GDD_CONSOLE_AUTH", "GDD_UI_LOCAL", "GDD_UI_SESSION_TTL", "GDD_UI_SECURE_COOKIE",
    "GDD_UI_EXTRA_HOSTS", "GDD_AGENT", "GDD_AGENT_BIN", "GDD_AGENT_PRE_ARGS",
    "GDD_PI_STUB_SCRIPT", "GDD_PI_SETTINGS", "GDD_PI_AGENT_DIR", "GDD_PI_SESSION_DIR",
    "GDD_STEER", "PYTHONPATH", "PYTHONUNBUFFERED",
)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def section(title):
    print(f"\n[{title}]")


def tag():
    return uuid.uuid4().hex[:8]


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def save_env():
    return {k: os.environ.get(k) for k in ENV_KEYS}


def restore_env(snapshot):
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def configure_stub_env(tmp, settings_path):
    """与 tests/test_ui.py 一致的 stub 执行端环境。"""
    script = os.path.join(tmp, "stub.json")
    with open(script, "w", encoding="utf-8") as f:
        json.dump([{"patterns": ["GDD-ROLE"], "text": "stub 主体工作完成"}],
                  f, ensure_ascii=False)
    pi_home = os.path.join(tmp, "pi-home")
    os.makedirs(pi_home, exist_ok=True)
    os.environ["GDD_AGENT"] = "pi"
    os.environ["GDD_AGENT_BIN"] = PYTHON
    os.environ["GDD_AGENT_PRE_ARGS"] = STUB
    os.environ["GDD_PI_STUB_SCRIPT"] = script
    os.environ["GDD_PI_SETTINGS"] = settings_path
    os.environ["GDD_PI_AGENT_DIR"] = os.path.join(pi_home, "agent")
    os.environ["GDD_PI_SESSION_DIR"] = os.path.join(pi_home, "sessions")
    os.environ.pop("GDD_STEER", None)


def template_doc(marker, with_approval=True):
    """现场生成的两节点 JSON 模板（不依赖 PyYAML）。"""
    first = {
        "id": "first",
        "name": f"第一节点-{marker}",
        "description": f"主体工作 {marker}",
        "subject": {"goal": f"目标 {marker}"},
    }
    if with_approval:
        first["approval"] = {"required": True, "prompt": f"请确认 {marker}"}
    return {
        "id": f"tpl-{marker}",
        "name": f"鉴权测试模板 {marker}",
        "description": f"fixture {marker}",
        "nodes": [first, {
            "id": "last",
            "name": f"收尾节点-{marker}",
            "description": f"收尾 {marker}",
            "subject": {"goal": f"收尾目标 {marker}"},
        }],
    }


# ---------------------------------------------------------------- HTTP 客户端


class Client:
    """带 CookieJar 的 HTTP 客户端（登录态自动随同源请求发送）。"""

    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, path, method="GET", body=None, headers=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", "X-GDD-Request": "auth-test"}
        h.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data,
                                         method=method, headers=h)
        try:
            with self.opener.open(request, timeout=timeout) as resp:
                return (resp.status,
                        {k.lower(): v for k, v in resp.headers.items()},
                        resp.read())
        except urllib.error.HTTPError as e:
            return (e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())

    def json(self, path, method="GET", body=None, headers=None):
        status, hdrs, raw = self.req(path, method, body, headers)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            obj = None
        return status, obj, hdrs, raw

    def get(self, path):
        return self.json(path)

    def post(self, path, body):
        return self.json(path, "POST", body)

    def put(self, path, body):
        return self.json(path, "PUT", body)

    def delete(self, path):
        return self.json(path, "DELETE")

    def token(self):
        for cookie in self.jar:
            if cookie.name == gdd_ui.SESSION_COOKIE:
                return cookie.value
        return ""


def raw_get(port, path, host):
    """显式 Host 的 GET（用于验证 GDD_UI_EXTRA_HOSTS 与「不按 Host 降级」）。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers={"Host": host})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def parse_sse_events(buf):
    events = []
    for line in buf.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            events.append(json.loads(line[5:].strip()))
        except json.JSONDecodeError:
            continue
    return events


def read_sse(port, path, token=None, seconds=2.0):
    """短读 SSE，返回 (status, content_type, 非流式 body, 事件列表)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Accept": "text/event-stream", "Connection": "keep-alive"}
    if token:
        headers["Cookie"] = f"{gdd_ui.SESSION_COOKIE}={token}"
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        ctype = resp.getheader("Content-Type") or ""
        if "text/event-stream" not in ctype:
            return status, ctype, resp.read(), []
        if conn.sock is not None:
            conn.sock.settimeout(0.3)
        raw = b""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                chunk = resp.fp.read1(8192)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                break
            raw += chunk
        return status, ctype, b"", parse_sse_events(raw)
    finally:
        conn.close()


class SseReader(threading.Thread):
    """后台读取已登录 SSE，直到 stop()。"""

    def __init__(self, port, path, token):
        super().__init__(daemon=True)
        self.port = port
        self.path = path
        self.token = token
        self.status = None
        self.content_type = ""
        self.body = b""
        self.events = []
        self.error = None
        self._stop = threading.Event()

    def run(self):
        conn = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            headers = {"Accept": "text/event-stream", "Connection": "keep-alive",
                       "Cookie": f"{gdd_ui.SESSION_COOKIE}={self.token}"}
            conn.request("GET", self.path, headers=headers)
            resp = conn.getresponse()
            self.status = resp.status
            self.content_type = resp.getheader("Content-Type") or ""
            if "text/event-stream" not in self.content_type:
                self.body = resp.read()
                return
            if conn.sock is not None:
                conn.sock.settimeout(0.2)
            pending = b""
            while not self._stop.is_set():
                try:
                    chunk = resp.fp.read1(8192)
                except (socket.timeout, TimeoutError):
                    continue
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    line = line.strip()
                    if line.startswith(b"data:"):
                        try:
                            self.events.append(json.loads(line[5:].strip()))
                        except json.JSONDecodeError:
                            pass
        except Exception as e:  # 连接被关闭等：留给主线程根据状态判断
            self.error = e
        finally:
            if conn is not None:
                conn.close()

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------- 服务进程


class SubprocessConsole:
    """真实控制台子进程（鉴权模式默认启动，不传 --local）。"""

    def __init__(self, workdir, log_path):
        self.workdir = workdir
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path
        env = os.environ.copy()
        env["PYTHONPATH"] = ROOT + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("GDD_UI_LOCAL", None)
        self._log = open(log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "gdd.ui", "--root", workdir,
             "--port", str(self.port), "--no-open"],
            cwd=ROOT, env=env, stdout=self._log, stderr=subprocess.STDOUT)
        self._wait_ready()

    def _wait_ready(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"控制台进程提前退出（code={self.proc.returncode}）：\n{self.log_tail()}")
            try:
                status, _, raw = Client(self.base).req("/", timeout=2)
                if status == 200 and b'id="loginForm"' in raw:
                    return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.1)
        raise RuntimeError(f"控制台未在 {timeout}s 内就绪：\n{self.log_tail()}")

    def log_tail(self, limit=100):
        self._log.flush()
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-limit:])
        except OSError:
            return ""

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._log.close()


class InProcessConsole:
    """进程内控制台（鉴权/本地模式由 Handler 类属性注入）。"""

    def __init__(self, workdir, creds_path, local_mode=False, ttl=None,
                 iterations=1000):
        gdd_ui.Handler.root = workdir
        gdd_ui.Handler.repos = workdir
        gdd_ui.Handler.local_mode = local_mode
        if local_mode:
            gdd_ui.Handler.auth = None
            gdd_ui.Handler.sessions = None
        else:
            auth = ConsoleAuth(creds_path, iterations=iterations)
            auth.ensure_initialized()
            gdd_ui.Handler.auth = auth
            gdd_ui.Handler.sessions = SessionStore(
                ttl=console_auth.DEFAULT_SESSION_TTL if ttl is None else ttl)
        self.srv = gdd_ui.ThreadingHTTPServer(("127.0.0.1", 0), gdd_ui.Handler)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


@contextmanager
def running(console):
    try:
        yield console
    finally:
        console.stop()


@contextmanager
def subprocess_console(workdir, log_path):
    console = SubprocessConsole(workdir, log_path)
    try:
        yield console
    finally:
        console.stop()


# ---------------------------------------------------------------- 断言辅助


def login(client, username=DEFAULT_USER, password=DEFAULT_PASSWORD):
    return client.json("/api/login", "POST",
                       {"username": username, "password": password})


def assert_unauthenticated(result, what):
    status, obj, hdrs, raw = result
    check(status == 401, f"{what}：401（实际 {status}）")
    check(isinstance(obj, dict) and obj.get("ok") is False,
          f"{what}：统一失败 JSON（{raw[:120]!r}）")
    check(obj.get("error") == UNAUTH_ERROR,
          f"{what}：错误为「{UNAUTH_ERROR}」（实际 {obj.get('error')!r}）")
    check("text/html" not in hdrs.get("content-type", ""),
          f"{what}：不返回业务页面")


def assert_no_secrets(text, secrets, what):
    for secret in secrets:
        if secret:
            check(secret not in text, f"{what}：不含敏感值 {secret[:12]}…")


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(*paths):
    """路径（文件或目录）内容快照：绝对路径 → sha256。"""
    out = {}
    for path in paths:
        path = os.path.abspath(path)
        if os.path.isfile(path):
            out[path] = file_sha256(path)
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                full = os.path.join(dirpath, name)
                out[full] = file_sha256(full)
    return out


def walk_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            yield os.path.join(dirpath, name)


def within(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path),
                                   os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def wait_task(client, name, pred, timeout=60, desc=""):
    quoted = urllib.parse.quote(name, safe="")
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, data, _, raw = client.get(f"/api/task/{quoted}")
        if status == 200 and isinstance(data, dict) and data.get("ok") and pred(data):
            return data
        time.sleep(0.3)
    raise AssertionError(f"等待超时：{desc or name}")


class MockEndpoint(BaseHTTPRequestHandler):
    """记录命中的假模型端点（未登录 POST /api/settings/test 不得命中）。"""

    hits = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        type(self).hits += 1
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({"choices": [{"message": {"role": "assistant",
                                                    "content": "pong"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------- D1 / D6 / D12（子进程默认启动）


def run_subprocess_default_mode(tmp, secrets):
    """D1 默认鉴权模式 + D6 默认账号登录 + D12 凭据文件/日志（子进程）。"""
    work = os.path.join(tmp, "sub-work")
    os.makedirs(work, exist_ok=True)
    creds = os.path.join(tmp, "sub-creds", "console-auth.json")
    log_path = os.path.join(tmp, "sub-console.log")
    os.environ["GDD_CONSOLE_AUTH"] = creds

    section("D1 默认启动即鉴权模式（子进程 `python -m gdd.ui`，不传 --local）")
    with subprocess_console(work, log_path) as console:
        anon = Client(console.base)
        status, hdrs, raw = anon.req("/")
        html = raw.decode("utf-8", "replace")
        check(status == 200, f"未登录 GET / 返回登录页 200（实际 {status}）")
        check('id="loginForm"' in html and 'id="password"' in html,
              "登录页包含用户名/密码表单")
        check('id="navTasks"' not in html and '<main id="main">' not in html,
              "登录页不包含业务控制台骨架/任务列表")
        status, _, raw_idx = anon.req("/index.html")
        check(status == 200 and b'id="loginForm"' in raw_idx,
              "未登录 GET /index.html 同样返回登录页")
        assert_unauthenticated(anon.get("/api/overview"),
                               "未登录 GET /api/overview（默认启动）")

        section("D6 默认账号登录成功、错误凭据失败、会话可建立")
        owner = Client(console.base)
        status, obj, hdrs, raw_login = login(owner)
        check(status == 200 and obj and obj.get("ok"),
              f"admin/gdd2026 登录成功（{status} {raw_login[:120]!r}）")
        set_cookie = hdrs.get("set-cookie", "")
        lowered = set_cookie.lower()
        check(gdd_ui.SESSION_COOKIE + "=" in set_cookie, "登录成功设置会话 Cookie")
        check("httponly" in lowered, "会话 Cookie 带 HttpOnly")
        check("samesite=lax" in lowered, "会话 Cookie 带 SameSite=Lax")
        check("path=/" in lowered, "会话 Cookie Path=/")
        check("max-age=" in lowered, "会话 Cookie 带 Max-Age")
        check(bool(owner.token()), "CookieJar 已保存会话 token")
        status, obj, _, raw_ov = owner.get("/api/overview")
        check(status == 200 and obj and obj.get("ok"),
              "携带登录态 GET /api/overview 返回 200")

        for username, password, label in (
                ("root", DEFAULT_PASSWORD, "错误用户名"),
                (DEFAULT_USER, "wrong-password", "错误密码")):
            fresh = Client(console.base)
            status, obj, _, raw_bad = login(fresh, username, password)
            check(status == 401 and obj and obj.get("ok") is False,
                  f"{label} 登录失败 401（实际 {status}）")
            check(obj.get("error") == BAD_LOGIN_ERROR,
                  f"{label} 返回统一通用错误（{obj.get('error')!r}）")
            check(fresh.token() == "", f"{label} 失败后没有可用会话 Cookie")
            assert_unauthenticated(fresh.get("/api/overview"),
                                   f"{label} 失败后的受保护 API")
            secrets["login_raw"].append(raw_bad.decode("utf-8", "replace"))

        # 已登录页面为业务控制台；登录页仍不泄露现场模板数据。
        marker = f"tpl-body-{tag()}"
        status, obj, _, _ = owner.put(
            "/api/template/" + urllib.parse.quote("sub-tpl", safe=""),
            {"content": json.dumps(template_doc(marker))})
        check(status == 200 and obj and obj.get("ok"), "已登录可保存模板（现场数据）")
        status, _, raw_business = owner.req("/")
        business = raw_business.decode("utf-8", "replace")
        check(status == 200 and 'id="navTasks"' in business
              and "<main id=\"main\">" in business, "已登录 GET / 返回业务控制台")
        status, _, raw_anon_page = Client(console.base).req("/")
        anon_page = raw_anon_page.decode("utf-8", "replace")
        check(marker not in anon_page, "未登录登录页不含现场模板原文")

        secrets["login_raw"].append(raw_login.decode("utf-8", "replace"))
        secrets["html"].extend([html, anon_page, business,
                                raw_idx.decode("utf-8", "replace")])

        # 退出后原登录态被拒绝（D11 的退出路径在子进程也验一次）。
        old_token = owner.token()
        status, obj, _, _ = owner.post("/api/logout", {})
        check(status == 200 and obj and obj.get("ok"), "退出登录返回 ok")
        check(owner.token() == "", "退出后 CookieJar 不再持有会话")
        status, _, _ = Client(console.base).req(
            "/api/overview", headers={"Cookie": f"{gdd_ui.SESSION_COOKIE}={old_token}"})
        check(status == 401, f"退出后复用原 token 被拒绝（实际 {status}）")

    # D12：扫描控制台 stdout/stderr 与工作台运行文件（凭据文件本身除外）。
    log_text = read_text(log_path)
    check("鉴权模式（需要登录）" in log_text,
          f"启动日志声明鉴权模式（{log_text.strip()[:160]!r}）")
    secrets["logs"].append(log_text)
    for path in walk_files(tmp):
        if os.path.abspath(path) == os.path.abspath(creds):
            continue
        try:
            secrets["logs"].append(read_text(path))
        except OSError:
            pass

    section("D12 凭据文件权限与内容")
    doc_text = read_text(creds)
    doc = json.loads(doc_text)
    mode = stat.S_IMODE(os.stat(creds).st_mode)
    dir_mode = stat.S_IMODE(os.stat(os.path.dirname(creds)).st_mode)
    check(mode == 0o600, f"凭据文件权限 0600（实际 {oct(mode)}）")
    check(dir_mode == 0o700, f"凭据目录权限 0700（实际 {oct(dir_mode)}）")
    check(DEFAULT_PASSWORD not in doc_text, "凭据文件不含明文 gdd2026")
    secrets["salt"] = doc["password"]["salt"]
    secrets["hash"] = doc["password"]["hash"]
    secrets["file"] = doc_text
    check(secrets["salt"] in doc_text and secrets["hash"] in doc_text,
          "凭据文件保存加盐哈希（盐/摘要存在）")
    proc = subprocess.run(
        ["git", "-C", ROOT, "ls-files", "--error-unmatch", creds],
        capture_output=True, text=True)
    check(proc.returncode != 0, "凭据文件路径不被 git 索引命中")
    check(not within(creds, ROOT), "凭据文件 realpath 位于仓库根之外")


# ---------------------------------------------------------------- 主流程（进程内鉴权模式）


def run_in_process_mode(tmp, secrets):
    work = os.path.join(tmp, "work")
    os.makedirs(work, exist_ok=True)
    creds = os.path.join(tmp, "creds", "console-auth.json")
    settings = os.path.join(tmp, "pi-settings.json")
    os.environ["GDD_CONSOLE_AUTH"] = creds
    os.environ["GDD_PI_SETTINGS"] = settings
    os.environ.pop("GDD_UI_EXTRA_HOSTS", None)
    os.environ.pop("GDD_UI_SESSION_TTL", None)

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockEndpoint)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_url = f"http://127.0.0.1:{mock.server_address[1]}/v1"
    MockEndpoint.hits = 0

    marker = tag()
    tpl_name = f"auth-tpl-{marker}"
    task_name = f"auth-task-{marker}"
    tpl_body = json.dumps(template_doc(marker), ensure_ascii=False)
    requirement = f"现场需求-{marker}"
    api_key = f"sk-auth-{marker}-secret"
    settings_model = f"model-{marker}"

    try:
        with running(InProcessConsole(work, creds)) as console:
            owner = Client(console.base)
            status, obj, _, _ = login(owner)
            check(status == 200 and obj and obj.get("ok"), "进程内鉴权模式默认账号登录成功")

            section("现场数据准备（已登录）")
            status, obj, _, _ = owner.put(
                "/api/template/" + urllib.parse.quote(tpl_name, safe=""),
                {"content": tpl_body})
            check(status == 200 and obj and obj.get("ok"), "创建现场模板")
            status, obj, _, _ = owner.put("/api/settings", {
                "baseUrl": mock_url, "model": settings_model, "apiKey": api_key})
            check(status == 200 and obj and obj.get("ok")
                  and api_key not in json.dumps(obj), "保存现场设置（响应脱敏）")
            status, obj, _, _ = owner.post("/api/tasks", {
                "name": task_name, "template": tpl_name, "requirement": requirement})
            check(status == 200 and obj and obj.get("ok"),
                  f"创建现场任务（{obj.get('error') if obj else ''}）")

            # ---------------- D3 未登录读拒绝 ----------------
            section("D3 未登录读 API 全部拒绝且不泄露数据")
            before = snapshot(work, settings)
            anon = Client(console.base)
            reads = [
                "/api/settings",
                "/api/overview",
                "/api/template/" + urllib.parse.quote(tpl_name, safe=""),
                "/api/task/" + urllib.parse.quote(task_name, safe=""),
            ]
            for path in reads:
                result = anon.get(path)
                assert_unauthenticated(result, f"未登录 GET {path}")
                body = result[3].decode("utf-8", "replace")
                for leaked in (marker, requirement, api_key, tpl_body):
                    check(leaked not in body, f"未登录 GET {path} 不泄露现场数据")
            after = snapshot(work, settings)
            check(before == after, "未登录读请求前后工作台/设置文件逐字节一致")

            # ---------------- D4 未登录写拒绝 ----------------
            section("D4 未登录写 API 全部拒绝且无副作用")
            before = snapshot(work, settings)
            quoted_task = urllib.parse.quote(task_name, safe="")
            quoted_tpl = urllib.parse.quote(tpl_name, safe="")
            writes = [
                ("POST", "/api/settings/test",
                 {"baseUrl": mock_url, "model": settings_model}),
                ("POST", "/api/tasks",
                 {"name": f"evil-{marker}", "template": tpl_name,
                  "requirement": "unauth"}),
                ("POST", f"/api/task/{quoted_task}/action", {"op": "run"}),
                ("POST", f"/api/task/{quoted_task}/action", {"op": "approve"}),
                ("POST", f"/api/task/{quoted_task}/action", {"op": "abort"}),
                ("POST", f"/api/task/{quoted_task}/action", {"op": "reset"}),
                ("PUT", "/api/settings",
                 {"baseUrl": mock_url, "model": "evil", "apiKey": "sk-evil"}),
                ("PUT", f"/api/template/{quoted_tpl}",
                 {"content": json.dumps(template_doc("evil"))}),
                ("DELETE", f"/api/template/{quoted_tpl}", None),
            ]
            for method, path, body in writes:
                result = anon.json(path, method, body)
                assert_unauthenticated(result, f"未登录 {method} {path}")
            check(MockEndpoint.hits == 0, "未登录 POST /api/settings/test 未触达模型端点")
            status, detail, _, _ = owner.get(f"/api/task/{quoted_task}")
            check(status == 200 and detail["status"] == "running"
                  and detail["nodes"][0]["phase"] == "pending",
                  "未登录写请求后任务状态仍为初始 running/pending")
            after = snapshot(work, settings)
            check(before == after, "未登录写请求前后工作台/设置文件逐字节一致")

            # ---------------- D5 未登录 SSE ----------------
            section("D5 未登录 SSE 被拒绝")
            sse_task = f"auth-sse-{marker}"
            status, obj, _, _ = owner.post("/api/tasks", {
                "name": sse_task, "template": tpl_name,
                "requirement": f"SSE 拒绝路径-{marker}"})
            check(status == 200 and obj.get("ok"), "创建 SSE 拒绝路径任务")
            status, obj, _, _ = owner.post(
                f"/api/task/{urllib.parse.quote(sse_task, safe='')}/action",
                {"op": "run"})
            check(status == 200 and obj.get("ok") and obj.get("started"),
                  "SSE 拒绝路径任务已运行")
            wait_task(owner, sse_task,
                      lambda d: d["status"] == "awaiting_approval"
                      and not d.get("running"),
                      desc="SSE 拒绝路径任务运行到待审批且后台线程退出")
            time.sleep(0.2)  # 等文件收尾，确保快照稳定
            before = snapshot(work)
            status, ctype, body, events = read_sse(
                console.port, f"/api/task/{urllib.parse.quote(sse_task, safe='')}/live",
                token=None, seconds=1.0)
            check(status == 401, f"未登录 SSE 返回 401（实际 {status}）")
            check("text/event-stream" not in ctype,
                  f"未登录 SSE 不建立事件流（Content-Type={ctype!r}）")
            check(not events and b"data:" not in body,
                  "未登录 SSE 观察窗口内收不到任何事件")
            check(UNAUTH_ERROR.encode("utf-8") in body,
                  "未登录 SSE 返回统一鉴权失败语义")
            check(before == snapshot(work), "未登录 SSE 请求不改变任务状态")

            # ---------------- D2 模式边界 ----------------
            section("D2 本地模式显式启用、仅回环、不可按 Host 自动判定")
            os.environ["GDD_UI_EXTRA_HOSTS"] = "console.example.test"
            try:
                for host in ("127.0.0.1", f"127.0.0.1:{console.port}",
                             "console.example.test",
                             f"console.example.test:{console.port}"):
                    status, _, body = raw_get(console.port, "/api/overview", host)
                    obj = json.loads(body.decode("utf-8")) if body else {}
                    check(status == 401 and obj.get("error") == UNAUTH_ERROR,
                          f"鉴权模式 Host={host} 未登录仍被拒绝（不得按 Host 降级）")
            finally:
                os.environ.pop("GDD_UI_EXTRA_HOSTS", None)

            help_text = subprocess.run(
                [PYTHON, "-m", "gdd.ui", "--help"], cwd=ROOT,
                capture_output=True, text=True).stdout
            check("--host" not in help_text,
                  "未引入非回环监听参数（--host 不存在，该子句不适用）")
            check("--local" in help_text, "显式本地模式开关 --local 存在")

            # ---------------- D7 登录后读 ----------------
            section("D7 登录后既有读功能可用")
            status, overview, _, _ = owner.get("/api/overview")
            check(status == 200 and overview.get("ok"), "登录后 overview 200")
            check(any(t["name"] == task_name for t in overview["tasks"]),
                  "overview 包含现场任务")
            check(any(t["name"] == tpl_name for t in overview["templates"]),
                  "overview 包含现场模板")
            status, tpl, _, _ = owner.get(
                "/api/template/" + urllib.parse.quote(tpl_name, safe=""))
            check(status == 200 and tpl.get("content") == tpl_body,
                  "登录后模板原文与现场创建一致")
            status, detail, _, _ = owner.get(f"/api/task/{quoted_task}")
            check(status == 200 and detail.get("ok")
                  and detail.get("name") == task_name
                  and [n["id"] for n in detail["nodes"]] == ["first", "last"],
                  "登录后任务详情与现场创建一致")
            status, view, _, raw_settings = owner.get("/api/settings")
            check(status == 200 and view.get("baseUrl") == mock_url
                  and view.get("model") == settings_model and view.get("hasApiKey"),
                  "登录后设置读取返回现场保存值")
            check(view.get("apiKeyMasked") == f"{api_key[:4]}****{api_key[-4:]}",
                  "设置仍返回脱敏视图")
            check(api_key not in raw_settings.decode("utf-8", "replace"),
                  "设置响应不回显明文 key")

            # ---------------- D9 模板 CRUD / 设置读写 ----------------
            section("D9 登录后模板 CRUD 与设置读写可用")
            tpl2 = f"auth-tpl2-{marker}"
            doc2 = template_doc(f"v1-{marker}")
            status, obj, _, _ = owner.put(
                "/api/template/" + urllib.parse.quote(tpl2, safe=""),
                {"content": json.dumps(doc2)})
            check(status == 200 and obj.get("ok"), "登录后创建模板")
            status, obj, _, _ = owner.get(
                "/api/template/" + urllib.parse.quote(tpl2, safe=""))
            check(status == 200 and obj.get("content") == json.dumps(doc2),
                  "模板创建后可读回")
            doc2["description"] = f"modified-{marker}"
            status, obj, _, _ = owner.put(
                "/api/template/" + urllib.parse.quote(tpl2, safe=""),
                {"content": json.dumps(doc2)})
            check(status == 200 and obj.get("ok"), "登录后修改模板")
            status, obj, _, _ = owner.get(
                "/api/template/" + urllib.parse.quote(tpl2, safe=""))
            check(status == 200 and json.loads(obj["content"])["description"]
                  == f"modified-{marker}", "模板修改后可读回")
            status, obj, _, _ = owner.delete(
                "/api/template/" + urllib.parse.quote(tpl2, safe=""))
            check(status == 200 and obj.get("ok"), "登录后删除模板")
            status, obj, _, _ = owner.get(
                "/api/template/" + urllib.parse.quote(tpl2, safe=""))
            check(status == 404 and obj.get("ok") is False, "删除后模板返回 404")

            bad_tpl = f"auth-bad-{marker}"
            status, obj, _, _ = owner.put(
                "/api/template/" + urllib.parse.quote(bad_tpl, safe=""),
                {"content": "{ bad json"})
            check(status == 400 and obj.get("ok") is False,
                  "非法模板仍按既有语义被拒绝（400）")

            secret2 = f"sk-auth-{marker}-rotated"
            status, obj, _, raw_put = owner.put("/api/settings", {
                "baseUrl": mock_url, "model": f"{settings_model}-v2",
                "apiKey": secret2})
            check(status == 200 and obj.get("ok"), "登录后保存设置")
            check(secret2 not in raw_put.decode("utf-8", "replace"),
                  "保存设置响应不回显明文 key")
            status, view, _, raw_get_settings = owner.get("/api/settings")
            check(status == 200 and view.get("model") == f"{settings_model}-v2"
                  and view.get("hasApiKey"), "设置保存后可读回")
            check("****" in view.get("apiKeyMasked", "")
                  and secret2 not in raw_get_settings.decode("utf-8", "replace"),
                  "读取设置仍为脱敏视图")
            status, obj, _, _ = owner.put("/api/settings", {
                "baseUrl": "ftp://x", "model": "m"})
            check(status == 400 and obj.get("ok") is False,
                  "非法端点仍按既有语义被拒绝（400）")
            status, obj, _, _ = owner.put("/api/settings", {
                "baseUrl": mock_url, "model": ""})
            check(status == 400 and obj.get("ok") is False,
                  "空模型仍按既有语义被拒绝（400）")

            # ---------------- D8 任务生命周期 ----------------
            section("D8 登录后既有任务生命周期可用")
            finish_name = f"auth-finish-{marker}"
            status, obj, _, _ = owner.post("/api/tasks", {
                "name": finish_name, "template": tpl_name,
                "requirement": f"完成路径-{marker}"})
            check(status == 200 and obj.get("ok"), "创建完成路径任务")
            status, obj, _, _ = owner.post(
                f"/api/task/{urllib.parse.quote(finish_name, safe='')}/action",
                {"op": "run"})
            check(status == 200 and obj.get("ok") and obj.get("started"),
                  "run 启动后台推进")
            detail = wait_task(owner, finish_name,
                               lambda d: d["status"] == "awaiting_approval",
                               desc="完成路径等待审批")
            pending = detail.get("pending_approval") or {}
            check(pending.get("node") == "first"
                  and f"请确认 {marker}" in (pending.get("prompt") or ""),
                  "审批挂起节点与 prompt 正确")
            status, obj, _, _ = owner.post(
                f"/api/task/{urllib.parse.quote(finish_name, safe='')}/action",
                {"op": "approve"})
            check(status == 200 and obj.get("ok"), "approve 受理")
            detail = wait_task(owner, finish_name,
                               lambda d: d["status"] == "completed",
                               desc="完成路径批准后完成")
            check(all(n["phase"] == "done" for n in detail["nodes"]),
                  "批准后全部节点 done")
            check(not detail.get("running"), "完成路径运行线程已退出")

            bounce_name = f"auth-bounce-{marker}"
            status, obj, _, _ = owner.post("/api/tasks", {
                "name": bounce_name, "template": tpl_name,
                "requirement": f"打回路径-{marker}"})
            check(status == 200 and obj.get("ok"), "创建打回路径任务")
            owner.post(f"/api/task/{urllib.parse.quote(bounce_name, safe='')}/action",
                       {"op": "run"})
            wait_task(owner, bounce_name,
                      lambda d: d["status"] == "awaiting_approval",
                      desc="打回路径首次审批")
            status, obj, _, _ = owner.post(
                f"/api/task/{urllib.parse.quote(bounce_name, safe='')}/action",
                {"op": "reject", "feedback": f"补充 {marker}"})
            check(status == 200 and obj.get("ok"), "reject 受理")
            detail = wait_task(owner, bounce_name,
                               lambda d: d["status"] == "awaiting_approval"
                               and d["nodes"][0]["bounces"] >= 1,
                               desc="打回后重新跑到审批")
            check(detail["nodes"][0]["bounces"] == 1, "驳回计入 bounces")
            hist = [h["event"] for h in detail.get("history_tail", [])]
            check("approval_reject" in hist, "history 记录 approval_reject")
            status, obj, _, _ = owner.post(
                f"/api/task/{urllib.parse.quote(bounce_name, safe='')}/action",
                {"op": "approve"})
            check(status == 200 and obj.get("ok"), "打回后 approve 受理")
            detail = wait_task(owner, bounce_name,
                               lambda d: d["status"] == "completed",
                               desc="打回路径最终完成")
            check(all(n["phase"] == "done" for n in detail["nodes"]),
                  "打回路径批准后全部节点 done")

            # ---------------- D10 登录后 SSE ----------------
            section("D10 登录后 SSE 正常推送")
            live_name = f"auth-live-{marker}"
            status, obj, _, _ = owner.post("/api/tasks", {
                "name": live_name, "template": tpl_name,
                "requirement": f"SSE-{marker}"})
            check(status == 200 and obj.get("ok"), "创建 SSE 任务")
            token = owner.token()
            check(bool(token), "SSE 使用当前登录会话 token")
            reader = SseReader(console.port,
                               f"/api/task/{urllib.parse.quote(live_name, safe='')}/live",
                               token)
            reader.start()
            time.sleep(0.3)
            owner.post(f"/api/task/{urllib.parse.quote(live_name, safe='')}/action",
                       {"op": "run"})
            wait_task(owner, live_name,
                      lambda d: d["status"] == "awaiting_approval",
                      desc="SSE 任务等待审批")
            owner.post(f"/api/task/{urllib.parse.quote(live_name, safe='')}/action",
                       {"op": "approve"})
            detail = wait_task(owner, live_name,
                               lambda d: d["status"] == "completed",
                               desc="SSE 任务完成")
            time.sleep(0.5)
            reader.stop()
            reader.join(timeout=5)
            check(reader.status == 200,
                  f"登录后 SSE 返回 200（实际 {reader.status}）")
            check("text/event-stream" in reader.content_type,
                  f"登录后 SSE Content-Type 正确（{reader.content_type!r}）")
            check(len(reader.events) >= 1,
                  f"登录后 SSE 收到至少一条事件（实际 {len(reader.events)}）")
            check(any(ev.get("kind") in ("phase", "text", "note", "tool")
                      for ev in reader.events),
                  "SSE 事件包含 stub 运行对应的阶段/文本事件")
            check(detail["status"] == "completed",
                  "连接期间任务正常推进到完成")

            # ---------------- D12 页面/响应不回显 ----------------
            section("D12 页面与响应不回显凭据")
            status, _, raw_anon = Client(console.base).req("/")
            status, _, raw_business = owner.req("/")
            _, _, _, raw_login_ok = login(Client(console.base))
            _, _, _, raw_login_bad = login(Client(console.base), "admin", "bad-password")
            status, _, raw_settings_final = owner.req("/api/settings")
            secrets["html"].extend([
                raw_anon.decode("utf-8", "replace"),
                raw_business.decode("utf-8", "replace"),
            ])
            secrets["login_raw"].extend([
                raw_login_ok.decode("utf-8", "replace"),
                raw_login_bad.decode("utf-8", "replace"),
                raw_settings_final.decode("utf-8", "replace"),
            ])

            # ---------------- D11 退出登录 ----------------
            section("D11 退出登录后重新被拒绝")
            old_token = owner.token()
            status, obj, _, _ = owner.post("/api/logout", {})
            check(status == 200 and obj.get("ok"), "退出登录返回 ok")
            check(owner.token() == "", "退出后 CookieJar 不再持有会话")
            status, _, _ = Client(console.base).req(
                "/api/overview",
                headers={"Cookie": f"{gdd_ui.SESSION_COOKIE}={old_token}"})
            check(status == 401, f"退出后复用原 token 被拒绝（实际 {status}）")
            relogin = Client(console.base)
            status, obj, _, _ = login(relogin)
            check(status == 200 and obj.get("ok"), "退出后重新登录成功")
            status, obj, _, _ = relogin.get("/api/overview")
            check(status == 200 and obj.get("ok"), "重新登录后恢复访问")

        # ---------------- 本地模式对照 ----------------
        section("D2 本地模式显式启用后免登录")
        local_work = os.path.join(tmp, "local-work")
        os.makedirs(local_work, exist_ok=True)
        local_creds = os.path.join(tmp, "local-creds", "console-auth.json")
        with running(InProcessConsole(local_work, local_creds,
                                      local_mode=True)) as local:
            status, obj, _, _ = Client(local.base).get("/api/overview")
            check(status == 200 and obj.get("ok"),
                  "本地模式未登录 GET /api/overview 返回 200")
            check("tasks" in obj and "templates" in obj,
                  "本地模式响应结构与既有行为一致")
            status, _, raw = Client(local.base).req("/")
            html = raw.decode("utf-8", "replace")
            check(status == 200 and 'id="navTasks"' in html
                  and 'id="loginForm"' not in html,
                  "本地模式 GET / 直接返回业务控制台")
            check(not os.path.exists(local_creds),
                  "本地模式不初始化凭据文件")

        # ---------------- D11 短 TTL 会话过期 ----------------
        section("D11 短 TTL 会话过期后重新被拒绝")
        with running(InProcessConsole(work, creds, ttl=1)) as short:
            client = Client(short.base)
            status, obj, _, _ = login(client)
            check(status == 200 and obj.get("ok"), "短 TTL 会话登录成功")
            status, obj, _, _ = client.get("/api/overview")
            check(status == 200 and obj.get("ok"), "TTL 内会话可用")
            time.sleep(1.3)
            assert_unauthenticated(client.get("/api/overview"),
                                   "超过短 TTL 后的受保护 API")
            client2 = Client(short.base)
            status, obj, _, _ = login(client2)
            check(status == 200 and obj.get("ok"), "过期后重新登录成功")
            status, obj, _, _ = client2.get("/api/overview")
            check(status == 200 and obj.get("ok"), "重新登录后恢复访问")
    finally:
        mock.shutdown()
        mock.server_close()


def main():
    env_before = save_env()
    tmp = tempfile.mkdtemp(prefix="gdd-auth-ui-")
    secrets = {"salt": "", "hash": "", "file": "", "login_raw": [], "html": [],
               "logs": []}
    print(f"[setup] 临时目录：{tmp}")
    try:
        configure_stub_env(tmp, os.path.join(tmp, "sub-pi-settings.json"))
        run_subprocess_default_mode(tmp, secrets)
        run_in_process_mode(tmp, secrets)

        section("D12 汇总：登录/页面/日志不含明文、哈希或盐")
        sensitive = [DEFAULT_PASSWORD, secrets["salt"], secrets["hash"],
                     secrets["file"]]
        for text in secrets["login_raw"]:
            assert_no_secrets(text, sensitive, "登录/设置响应")
        for text in secrets["html"]:
            assert_no_secrets(text, sensitive, "登录页/业务页 HTML")
        for text in secrets["logs"]:
            assert_no_secrets(text, sensitive, "控制台/运行日志")

        print("\nALL AUTH UI TESTS PASSED")
        return 0
    except Exception:
        print("\n[auth-ui] 失败", file=sys.stderr)
        traceback.print_exc()
        print(f"[auth-ui] 现场保留在：{tmp}", file=sys.stderr)
        return 1
    finally:
        restore_env(env_before)


if __name__ == "__main__":
    sys.exit(main())
