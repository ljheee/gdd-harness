"""控制台登录鉴权的浏览器 e2e 测试（Playwright 仅测试使用，不是产品依赖）。

覆盖 goal.md D14 / acceptance.feature「浏览器完成登录、进入控制台并退出」：
  未登录看到登录界面；错误密码显示失败且不进入控制台；admin/gdd2026 登录后进入
  业务控制台并能看到现场创建的任务；点击退出后回到登录界面，刷新后仍要求登录。
断言基于 DOM 文本/URL/状态，不依赖固定截图。

运行（需要可导入 playwright 的 Python 解释器）：
  GDD_E2E_PYTHON=python3.12 python3 tests/test_auth_e2e.py
或：
  PYTHONPATH=/path/to/playwright/site-packages python3.12 tests/test_auth_e2e.py

脚本用子进程启动真实鉴权模式控制台（临时工作台 + 临时凭据文件 + 随机端口，不传
--local），再用系统 Chrome 的 headless 模式驱动：`chromium.launch(channel="chrome",
headless=True)`。缺少 Playwright 或浏览器时按 goal.md 降级建议标记 DEGRADED，改用
HTTP + 静态 DOM 断言替代，不得删除断言、不得记为真实浏览器通过。
可用 GDD_E2E_PYTHON 指定启动控制台的解释器（默认 sys.executable）。
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB = os.path.join(ROOT, "tests", "pi_stub.py")
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "gdd2026"

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


def configure_env(tmp, creds_path, settings_path):
    script = os.path.join(tmp, "stub.json")
    with open(script, "w", encoding="utf-8") as f:
        json.dump([{"patterns": ["GDD-ROLE"], "text": "stub 主体工作完成"}],
                  f, ensure_ascii=False)
    pi_home = os.path.join(tmp, "pi-home")
    os.makedirs(pi_home, exist_ok=True)
    os.environ["GDD_CONSOLE_AUTH"] = creds_path
    os.environ["GDD_UI_SESSION_TTL"] = "3600"
    os.environ.pop("GDD_UI_LOCAL", None)
    os.environ["GDD_AGENT"] = "pi"
    os.environ["GDD_AGENT_BIN"] = os.environ.get("GDD_E2E_PYTHON") or sys.executable
    os.environ["GDD_AGENT_PRE_ARGS"] = STUB
    os.environ["GDD_PI_STUB_SCRIPT"] = script
    os.environ["GDD_PI_SETTINGS"] = settings_path
    os.environ["GDD_PI_AGENT_DIR"] = os.path.join(pi_home, "agent")
    os.environ["GDD_PI_SESSION_DIR"] = os.path.join(pi_home, "sessions")
    os.environ.pop("GDD_STEER", None)


class Client:
    """带 CookieJar 的 HTTP 客户端，用于在浏览器之外现场准备数据。"""

    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, path, method="GET", body=None, headers=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", "X-GDD-Request": "auth-e2e"}
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

    def json(self, path, method="GET", body=None):
        status, hdrs, raw = self.req(path, method, body)
        try:
            return status, json.loads(raw.decode("utf-8")), hdrs, raw
        except (UnicodeDecodeError, json.JSONDecodeError):
            return status, None, hdrs, raw

    def token(self):
        for cookie in self.jar:
            if cookie.name == "gdd_session":
                return cookie.value
        return ""


class Console:
    """鉴权模式真实控制台子进程（不传 --local）。"""

    def __init__(self, workdir, log_path):
        self.workdir = workdir
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path
        python = os.environ.get("GDD_E2E_PYTHON") or sys.executable
        env = os.environ.copy()
        env["PYTHONPATH"] = ROOT + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("GDD_UI_LOCAL", None)
        self._log = open(log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [python, "-m", "gdd.ui", "--root", workdir,
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


def build_template(marker):
    return {
        "id": f"tpl-{marker}",
        "name": f"浏览器测试模板 {marker}",
        "description": f"fixture {marker}",
        "nodes": [{
            "id": "first",
            "name": f"节点-{marker}",
            "description": f"主体工作 {marker}",
            "subject": {"goal": f"目标 {marker}"},
        }],
    }


def prepare_data(console, marker, task_name):
    """登录并在现场工作台创建一个模板与一个任务（D14 的现场数据）。"""
    owner = Client(console.base)
    status, obj, _, _ = owner.json("/api/login", "POST", {
        "username": DEFAULT_USER, "password": DEFAULT_PASSWORD})
    check(status == 200 and obj and obj.get("ok"), "现场准备：默认账号登录成功")
    tpl_name = f"e2e-tpl-{marker}"
    status, obj, _, _ = owner.json(
        "/api/template/" + urllib.parse.quote(tpl_name, safe=""), "PUT",
        {"content": json.dumps(build_template(marker))})
    check(status == 200 and obj.get("ok"), "现场准备：模板创建成功")
    status, obj, _, _ = owner.json("/api/tasks", "POST", {
        "name": task_name, "template": tpl_name,
        "requirement": f"浏览器 e2e 现场需求 {marker}"})
    check(status == 200 and obj.get("ok"),
          f"现场准备：任务创建成功（{obj.get('error') if obj else ''}）")
    status, overview, _, _ = owner.json("/api/overview")
    check(status == 200 and any(t["name"] == task_name
                                for t in overview.get("tasks", [])),
          "现场准备：任务出现在 overview")
    return owner


def run_degraded(console, task_name, marker, reason):
    """浏览器不可用时的降级断言（HTTP + 静态 DOM），不得删除 D14 断言。"""
    print(f"\nDEGRADED: 真实浏览器不可用（{reason}）")
    print("DEGRADED: 按 goal.md 降级为 HTTP + 静态 DOM 断言；"
          "不得记为真实浏览器通过")
    section("D14（DEGRADED）未登录看到登录界面")
    anon = Client(console.base)
    status, hdrs, raw = anon.req("/")
    html = raw.decode("utf-8", "replace")
    check(status == 200 and 'id="loginForm"' in html and 'id="password"' in html,
          "GET / 返回登录表单 DOM")
    check('id="navTasks"' not in html and '<main id="main">' not in html,
          "登录页不含业务控制台骨架/任务列表")
    check(task_name not in html, "登录页不泄露现场任务名")

    section("D14（DEGRADED）错误密码显示失败且不进入控制台")
    bad = Client(console.base)
    status, obj, _, raw_bad = bad.json("/api/login", "POST", {
        "username": DEFAULT_USER, "password": "wrong-password"})
    check(status == 401 and obj and obj.get("error"), "错误密码登录被拒绝（401）")
    check(bad.token() == "", "错误密码后无会话 Cookie")
    status, _, raw_home = bad.req("/")
    check('id="loginForm"' in raw_home.decode("utf-8", "replace"),
          "失败后仍停留在登录页")

    section("D14（DEGRADED）默认账号登录后进入业务控制台并看到任务")
    owner = Client(console.base)
    status, obj, _, _ = owner.json("/api/login", "POST", {
        "username": DEFAULT_USER, "password": DEFAULT_PASSWORD})
    check(status == 200 and obj and obj.get("ok"), "默认账号登录成功")
    status, _, raw_business = owner.req("/")
    business = raw_business.decode("utf-8", "replace")
    check('id="navTasks"' in business and 'id="navLogout"' in business,
          "登录后 GET / 返回业务控制台 DOM（含任务列表与退出按钮）")
    status, overview, _, _ = owner.json("/api/overview")
    check(status == 200 and any(t["name"] == task_name
                                for t in overview.get("tasks", [])),
          "业务控制台数据源能看到现场创建的任务列表")

    section("D14（DEGRADED）退出后回到登录界面，刷新后仍要求登录")
    old_token = owner.token()
    status, obj, _, _ = owner.json("/api/logout", "POST", {})
    check(status == 200 and obj and obj.get("ok"), "退出登录成功")
    status, _, _ = Client(console.base).req(
        "/api/overview", headers={"Cookie": f"gdd_session={old_token}"})
    check(status == 401, "退出后原会话被拒绝")
    status, _, raw_after = Client(console.base).req("/")
    check(status == 200 and 'id="loginForm"' in raw_after.decode("utf-8", "replace"),
          "退出/刷新后 GET / 仍返回登录界面")

    print("\nDEGRADED 断言全部通过（未运行真实浏览器）")
    return 0


def run_browser(console, task_name, marker):
    from playwright.sync_api import sync_playwright

    section("D14 浏览器：未登录看到登录界面")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(20000)
            page_errors = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            page.goto(console.base + "/", wait_until="domcontentloaded")
            page.wait_for_selector("#loginForm")
            check(page.locator("#loginForm").count() == 1, "登录表单可见")
            check(page.locator("#navTasks").count() == 0, "未登录页面没有任务列表")
            check(task_name not in page.content(), "未登录页面不泄露现场任务名")

            section("D14 浏览器：错误密码显示失败且不进入控制台")
            page.fill("#username", DEFAULT_USER)
            page.fill("#password", "wrong-password")
            page.click("#submit")
            page.wait_for_function(
                "() => (document.querySelector('#msg')?.textContent || '').length > 0")
            msg = page.locator("#msg").text_content() or ""
            check("用户名或密码错误" in msg or "失败" in msg,
                  f"错误密码显示失败提示（{msg!r}）")
            check(page.locator("#loginForm").count() == 1,
                  "错误密码后仍停留在登录界面")
            check(page.locator("#navTasks").count() == 0,
                  "错误密码后没有进入业务控制台")

            section("D14 浏览器：admin/gdd2026 登录后进入控制台并看到任务列表")
            page.fill("#username", DEFAULT_USER)
            page.fill("#password", DEFAULT_PASSWORD)
            page.click("#submit")
            page.wait_for_selector("#navTasks", state="visible")
            page.wait_for_function(
                "n => (document.querySelector('#navTasks')?.textContent || '').includes(n)",
                arg=task_name)
            check(page.locator("#navTasks").count() == 1, "业务控制台任务列表存在")
            check(task_name in (page.locator("#navTasks").text_content() or ""),
                  f"任务列表显示现场任务 {task_name}")
            check(page.locator("#navLogout").count() == 1, "侧栏退出入口存在")
            check(page.locator("#main").count() == 1, "业务控制台主区域存在")

            section("D14 浏览器：退出后回到登录界面，刷新后仍要求登录")
            page.click("#navLogout")
            page.wait_for_selector("#loginForm", state="visible")
            check(page.locator("#navTasks").count() == 0,
                  "退出后不再展示业务控制台")
            cookies = context.cookies()
            check(all(c.get("name") != "gdd_session" for c in cookies),
                  "退出后浏览器不再持有会话 Cookie")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#loginForm")
            check(page.locator("#navTasks").count() == 0,
                  "刷新后仍要求登录")
            check(not page_errors, f"全程无页面未捕获 JS 异常（实际 {page_errors}）")
            return 0
        finally:
            browser.close()


def main():
    tmp = tempfile.mkdtemp(prefix="gdd-auth-e2e-")
    workdir = os.path.join(tmp, "work")
    os.makedirs(workdir, exist_ok=True)
    creds = os.path.join(tmp, "creds", "console-auth.json")
    settings = os.path.join(tmp, "pi-settings.json")
    log_path = os.path.join(tmp, "console.log")
    configure_env(tmp, creds, settings)

    marker = tag()
    task_name = f"e2e-task-{marker}"
    console = Console(workdir, log_path)
    print(f"[setup] 鉴权模式控制台 {console.base}（pid {console.proc.pid}），"
          f"工作台 {workdir}")
    try:
        prepare_data(console, marker, task_name)
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as e:
            return run_degraded(console, task_name, marker, f"无法导入 Playwright：{e}")

        try:
            result = run_browser(console, task_name, marker)
        except Exception as e:
            # 只有浏览器启动失败才降级；D14 断言失败必须如实失败。
            message = f"{type(e).__name__}: {e}"
            if _browser_unavailable(e):
                return run_degraded(console, task_name, marker, message)
            raise
        print("\n全部通过 ✔")
        return result
    except Exception:
        print("\n[e2e] 失败", file=sys.stderr)
        traceback.print_exc()
        print(f"[e2e] 控制台日志尾部：\n{console.log_tail()}", file=sys.stderr)
        print(f"[e2e] 现场保留在：{tmp}", file=sys.stderr)
        return 1
    finally:
        console.stop()


def _browser_unavailable(exc):
    """区分「浏览器/驱动无法启动」与真实断言失败。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = ("executable doesn't exist", "browserType.launch", "playwright",
               "channel", "chromium", "headless_shell", "host system is missing")
    return any(m in text for m in markers)


if __name__ == "__main__":
    sys.exit(main())
