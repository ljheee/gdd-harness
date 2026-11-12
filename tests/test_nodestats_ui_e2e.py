"""节点三段统计的控制台 e2e（T6 / goal.md D7-D8）。

流程：用真实 Orchestrator + 可控脚本执行端跑出一个包含
「已完成节点（普通主体 / 并行开发任务） + 未开始节点」的真实任务，
再启动真实控制台（子进程 + 随机端口），用 Playwright 驱动本机 Chrome：

- D7 已完成节点方块右下角显示 ``<整数>min``，数值 = stats.total_s 四舍五入到整数分钟；
  未开始/未完成节点不显示总耗时；位置与 CSS 断言基于 DOM/计算样式/几何，不依赖截图。
- D8 悬浮已完成节点：显示 entry / 主体 / exit 三段耗时（1 位小数 min）与每段工具/LLM/token；
  并行开发任务会话逐条列出且 data-task 可区分；token 不可得显示「不可得」，
  段级部分不可得显示已知合计并标注。

无浏览器（Playwright/Chrome 不可用）时按 goal.md 降级为 integration：
启动真实控制台 + HTTP 断言 + 用服务端实际返回的节点数据执行页面内嵌渲染函数，
逐条保留 D7/D8 断言，并在输出中明确标注「DEGRADED」，不得当作真实浏览器验证。

运行：python3 tests/test_nodestats_ui_e2e.py
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gdd.orchestrator as orch  # noqa: E402
from gdd import nodestats  # noqa: E402
from gdd.orchestrator import Orchestrator  # noqa: E402
from gdd.runner import SessionResult  # noqa: E402

PASS = 'GDD-VERDICT: {"verdict": "pass", "reason": "ok", "evidence": ["e"], "feedback": ""}'
PLAN_WITH_TASKS = ("# P\n## 实现步骤\n## 附录：任务依赖\n"
                   "- T1 | - | 甲\n- T2 | - | 乙\n")
TASK_NAME = "task-stats-e2e"


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok", msg)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def min1(seconds):
    return f"{max(0.0, float(seconds)) / 60:.1f}min"


def total_min(seconds):
    # 与页面 statsTotalMin 一致（Math.round），不用 Python 的银行家舍入
    return f"{int(math.floor(max(0.0, float(seconds)) / 60 + 0.5))}min"


# ---------------------------------------------------------------- 可控时钟 / 执行端替身

class Clock:
    def __init__(self, start=1000.0):
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self):
        with self._lock:
            return self._t

    def advance_to(self, value):
        with self._lock:
            self._t = max(self._t, float(value))


CLOCK = Clock()


class ScriptedRunner:
    barrier = None
    plan: dict = {}
    calls: list = []
    lock = threading.Lock()

    def __init__(self, root):
        self.root = root

    @staticmethod
    def reset(start=1000.0, barrier=None):
        with CLOCK._lock:
            CLOCK._t = float(start)
        ScriptedRunner.barrier = barrier
        ScriptedRunner.plan = {}
        ScriptedRunner.calls = []

    @staticmethod
    def push(role, spec):
        ScriptedRunner.plan.setdefault(role, []).append(spec)

    @staticmethod
    def _role(prompt: str) -> str:
        if "[[GDD-TASK:" in prompt:
            return "task"
        if "[[GDD-ROLE:gate:entry" in prompt:
            return "entry"
        if "[[GDD-ROLE:gate:exit" in prompt:
            return "exit"
        if "[[GDD-ROLE:clarify" in prompt:
            return "clarify"
        return "subject"

    def run(self, prompt, *, allowed_tools, max_turns, log_path=None,
            resume_sid="", on_stream=None):
        role = ScriptedRunner._role(prompt)
        tid = ""
        with ScriptedRunner.lock:
            if role == "task":
                tid = prompt.split("[[GDD-TASK:", 1)[1].split("]", 1)[0]
                spec = (ScriptedRunner.plan.get("task") or {}).get(tid)
                if spec is None:
                    raise AssertionError(f"未预期的任务会话 {tid}")
                ScriptedRunner.calls.append(f"task:{tid}")
            else:
                queue = ScriptedRunner.plan.get(role) or []
                if not queue:
                    raise AssertionError(f"未预期的 {role} 会话调用（脚本已耗尽）")
                ScriptedRunner.calls.append(role)
                spec = queue.pop(0)
        started = CLOCK.now()
        seconds = float(spec.get("seconds", 0.0))
        if role == "task" and ScriptedRunner.barrier is not None:
            ScriptedRunner.barrier.wait(timeout=10)
        if seconds:
            CLOCK.advance_to(started + seconds)
        if role == "task":
            _write(os.path.join(self.root, ".gdd", "task-results", f"{tid}.md"),
                   f"{tid}｜完成｜gdd/x.py:1｜ok｜无\n")
        text = spec.get("text")
        if text is None:
            text = PASS if role in ("entry", "exit") else "subject done"
        return SessionResult(
            ok=spec.get("ok", True), text=text, error=spec.get("error", ""),
            session_id=spec.get("sid", f"{role}-{len(ScriptedRunner.calls)}"),
            turns=1, duration_s=seconds,
            tool_calls=spec.get("tool_calls", 0), llm_calls=spec.get("llm_calls", 0),
            token_input=spec.get("token_input", 0),
            token_output=spec.get("token_output", 0),
            token_cache_creation=spec.get("token_cache_creation", 0),
            token_cache_read=spec.get("token_cache_read", 0),
            token_status=spec.get("token_status", "available"),
        )


def build_task(workdir: str):
    """跑出一个真实任务：setup 完成、implement 完成（两个并行任务）、next 未开始。"""
    ScriptedRunner.reset(start=1000.0, barrier=threading.Barrier(2))
    task_root = os.path.join(workdir, TASK_NAME)
    os.makedirs(task_root)
    pipe = {
        "id": "e2e-p", "name": "统计 e2e", "git_delivery": {"enabled": False},
        "nodes": [
            {"id": "setup", "name": "准备节点", "subject": {"goal": "准备"}},
            {"id": "implement", "name": "开发节点", "baseline_marker": True,
             "entry": {"ai_check": "入口检查"}, "exit": {"ai_check": "出口检查"},
             "subject": {"goal": "实现"}},
            {"id": "next", "name": "后续节点", "subject": {"goal": "后续"}},
        ],
    }
    o = Orchestrator(task_root, defer_approval=True)
    o.init(pipe, "e2e 统计展示")
    o.load()
    o._stats_now = CLOCK.now
    _write(os.path.join(task_root, "docs", "plan.md"), PLAN_WITH_TASKS)
    # setup：普通主体 30s
    ScriptedRunner.push("subject", {"sid": "sid-setup", "seconds": 30.0,
                                    "tool_calls": 1, "llm_calls": 1,
                                    "token_input": 9, "token_output": 1})
    # implement：entry 60s → 并行 T1 600s / T2 360s（T2 usage 不可得）→ exit 40s
    ScriptedRunner.push("entry", {"sid": "sid-impl-entry", "seconds": 60.0,
                                  "tool_calls": 2, "llm_calls": 1,
                                  "token_input": 100, "token_output": 10,
                                  "token_cache_creation": 5, "token_cache_read": 20})
    ScriptedRunner.push("exit", {"sid": "sid-impl-exit", "seconds": 40.0,
                                 "tool_calls": 0, "llm_calls": 1,
                                 "token_input": 7, "token_output": 3})
    ScriptedRunner.plan["task"] = {
        "T1": {"sid": "sid-T1", "seconds": 600.0, "tool_calls": 3, "llm_calls": 2,
               "token_input": 100, "token_output": 10,
               "token_cache_creation": 5, "token_cache_read": 20},
        "T2": {"sid": "sid-T2", "seconds": 360.0, "tool_calls": 1, "llm_calls": 1,
               "token_status": "unavailable"},
    }
    orig = orch.AgentRunner
    orch.AgentRunner = ScriptedRunner
    try:
        for _ in range(60):
            if (o.state.get("current") == "next"
                    and o.state["node_states"]["implement"]["phase"] == "done"):
                break
            if not o.step():
                break
    finally:
        orch.AgentRunner = orig
    if not (o.state.get("current") == "next"
            and o.state["node_states"]["implement"]["phase"] == "done"):
        raise AssertionError("e2e 任务未推进到目标状态：implement done + next pending")
    if o.state["status"] == "completed":
        raise AssertionError("e2e 任务不应在 next 未开始时完成")
    o._save()
    return task_root, o


# ---------------------------------------------------------------- 控制台子进程

def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Console:
    def __init__(self, workdir):
        self.workdir = workdir
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = os.path.join(workdir, "console.log")
        env = os.environ.copy()
        env["PYTHONPATH"] = ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("GDD_STEER", None)
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "gdd.ui", "--root", workdir,
             "--port", str(self.port), "--no-open"],
            cwd=ROOT, env=env, stdout=self._log, stderr=subprocess.STDOUT)
        self._wait_ready()

    def _wait_ready(self, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"控制台进程提前退出（code={self.proc.returncode}）：\n{self.log_tail()}")
            try:
                with urllib.request.urlopen(self.base + "/api/overview", timeout=2) as resp:
                    if json.loads(resp.read().decode()).get("ok"):
                        return
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass
            time.sleep(0.1)
        raise AssertionError(f"控制台未在 {timeout}s 内就绪：\n{self.log_tail()}")

    def get_json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def get_text(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return resp.read().decode()

    def log_tail(self, limit=80):
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


# ---------------------------------------------------------------- 断言（浏览器与降级共用口径）

def expected_stats(o, node_id):
    v = nodestats.for_ui(o.state, node_id)
    if v is None:
        raise AssertionError(f"{node_id} 缺少统计")
    return v


def assert_total_text(text, stats, who):
    check(text == total_min(stats["total_s"]),
          f"{who} 方块总耗时 {text!r} = total {stats['total_s']}s 四舍五入到整数分钟")


def assert_pop_html(markup, stats, who):
    """对节点悬浮面板的 DOM/HTML 断言（浏览器 innerHTML 与降级渲染共用）。"""
    segs = stats["segments"]
    entry, subject, exit_seg = segs["entry"], segs["subject"], segs["exit"]
    for name, label in (("entry", "entry"), ("subject", "主体"), ("exit", "exit")):
        check(f'data-seg="{name}"' in markup, f"{who} 悬浮面板含 {label} 段（data-seg={name}）")
        check(f"<b>{label}</b>" in markup, f"{who} 悬浮面板显示段名 {label}")
    for name in ("entry", "subject", "exit"):
        seg = segs[name]
        check(min1(seg["duration_s"]) in markup,
              f"{who} 段 {name} 耗时显示 1 位小数分钟（{min1(seg['duration_s'])}）")
        row = re.search(r'<div class="np-seg" data-seg="%s">.*?(?=<div class="np-seg"|</div></div>$|$)'
                        % name, markup, re.S)
        check(row is not None, f"{who} 段 {name} 汇总行存在")
        row_text = row.group(0)
        check(f"工具 {seg['tool_calls']}" in row_text and f"LLM {seg['llm_calls']}" in row_text,
              f"{who} 段 {name} 显示工具/LLM 合计")
        check("token " in row_text, f"{who} 段 {name} 显示 token 合计")
    check(min1(stats["total_s"]) in markup, f"{who} 悬浮面板显示三段耗时合计（1 位小数 min）")

    # token 三态
    check(entry["token_status"] == "available" and entry["token_total"] == 135,
          f"{who} entry 段 token 四项和 135")
    check("token 135" in markup, f"{who} entry 段展示 token 135")
    check(subject["token_status"] == "partial" and subject["token_total"] == 135,
          f"{who} 主体段部分不可得：已知合计 135")
    check("token 135（部分不可得）" in markup, f"{who} 主体段展示「135（部分不可得）」")
    check(exit_seg["token_status"] == "available" and exit_seg["token_total"] == 10,
          f"{who} exit 段 token 10")
    check("token 10" in markup, f"{who} exit 段展示 token 10")

    # 并行任务会话逐条可见、可区分；T2 不可得
    tasks = [s for s in subject["sessions"] if s["kind"] == "task"]
    check([s["task_id"] for s in tasks] == ["T1", "T2"], f"{who} 主体段含两个并行 task 会话")
    for s in tasks:
        check(f'data-task="{s["task_id"]}"' in markup, f"{who} 会话行带 data-task={s['task_id']}")
        check(f"task {s['task_id']}" in markup, f"{who} 显示 task {s['task_id']}")
        check(min1(s["duration_s"]) in markup,
              f"{who} task {s['task_id']} 会话耗时 {min1(s['duration_s'])}")
    t2 = tasks[1]
    check(t2["token_status"] == "unavailable" and t2["token_total"] is None,
          f"{who} T2 usage 缺失：会话不可得")
    t2_row = re.search(r'data-task="T2".*?</div>', markup, re.S)
    check(t2_row is not None and "token 不可得" in t2_row.group(0),
          f"{who} T2 会话行显示「token 不可得」")
    check(markup.count('class="np-s"') == 4,
          f"{who} 会话行数 = entry 1 + 主体 2 + exit 1（空段不显示空行）")


def page_script_harness(nodes):
    return (
        "\nconst nodes = " + json.dumps(nodes, ensure_ascii=False) + ";\n"
        "const out = {};\n"
        "for (const n of nodes) out[n.id] = nodeStatsBlock(n);\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )


def _inline_script(html):
    blocks = re.findall(r"<script(\s[^>]*)?>(.*?)</script>", html, re.S)
    if len(blocks) != 1 or (blocks[0][0] or "").strip():
        raise AssertionError("页面不是唯一内联 script（零外部依赖约定被破坏）")
    return blocks[0][1]


def _js_eval(script, harness):
    node = shutil.which("node")
    if not node:
        raise AssertionError("找不到 node，无法执行页面内嵌渲染函数（降级路径也需要）")
    body = script.splitlines()
    while body and not body[-1].strip():
        body.pop()
    if not body or not re.match(
            r"^refreshNav\(\);showNew\(\);setInterval\(refreshNav,\s*\d+\);$",
            body[-1].strip()):
        raise AssertionError("页面启动语句不符合预期，无法剥离")
    body.pop()
    with tempfile.TemporaryDirectory(prefix="gdd-ns-e2e-") as td:
        path = os.path.join(td, "render.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(body) + "\n" + harness)
        proc = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node 求值失败：{proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout)


# ---------------------------------------------------------------- 真实浏览器路径

def run_browser(console, o):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [skip] 未安装 Playwright，转降级 integration 路径")
        return False
    with sync_playwright() as pw:
        browser = None
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            print("  [env] 浏览器：chromium channel=chrome headless")
        except Exception as first:
            try:
                browser = pw.chromium.launch(headless=True)
                print(f"  [env] chrome channel 不可用（{str(first)[:120]}），改用 Playwright bundled chromium")
            except Exception as second:
                print(f"  [skip] 浏览器不可用：chrome={str(first)[:100]} bundled={str(second)[:100]}")
                print("         转降级 integration 路径")
                return False
        try:
            page = browser.new_page()
            page.goto(console.base, wait_until="domcontentloaded")
            page.wait_for_selector("#navTasks")
            nav = page.locator("#navTasks button.item").filter(has_text=TASK_NAME)
            nav.first.wait_for(state="visible")
            nav.first.click()
            page.wait_for_selector(".flow .node", state="visible")
            page.wait_for_timeout(300)
            # 页面每 2s 轮询重绘；停掉轮询避免悬浮断言期间 DOM 被替换
            page.evaluate("stopPoll()")
            nodes = page.locator(".flow .node")
            check(nodes.count() == 3, "流程渲染 3 个节点方块（setup / implement / next）")

            def node_by_name(name):
                return page.locator(".flow .node").filter(has_text=name).first

            setup_node = node_by_name("准备节点")
            impl_node = node_by_name("开发节点")
            next_node = node_by_name("后续节点")

            # D7：已完成节点右下角总耗时
            setup_stats = expected_stats(o, "setup")
            impl_stats = expected_stats(o, "implement")
            check(setup_node.locator(".node-total").count() == 1, "已完成节点 setup 显示总耗时")
            assert_total_text(setup_node.locator(".node-total").inner_text(), setup_stats, "setup")
            check(impl_node.locator(".node-total").count() == 1, "已完成节点 implement 显示总耗时")
            assert_total_text(impl_node.locator(".node-total").inner_text(), impl_stats, "implement")
            check(next_node.locator(".node-total").count() == 0, "未开始节点 next 不显示总耗时")

            geom = impl_node.evaluate(
                "el => { const t = el.querySelector('.node-total');"
                " if (!t) return null;"
                " const cs = getComputedStyle(t);"
                " const nr = el.getBoundingClientRect(), tr = t.getBoundingClientRect();"
                " return {position: cs.position, right: cs.right, bottom: cs.bottom,"
                "   rightGap: nr.right - tr.right, bottomGap: nr.bottom - tr.bottom,"
                "   inside: tr.left >= nr.left && tr.right <= nr.right"
                "           && tr.top >= nr.top && tr.bottom <= nr.bottom}; }")
            check(geom and geom["position"] == "absolute",
                  "总耗时采用绝对定位（右下角）")
            check(geom and geom["right"] == "8px" and geom["bottom"] == "7px",
                  "总耗时定位 right:8px / bottom:7px")
            check(geom and geom["inside"] and geom["rightGap"] <= 10 and geom["bottomGap"] <= 10,
                  "总耗时几何上位于节点方块内部右下角")

            # D8：悬浮三段与会话明细
            pop = impl_node.locator(".node-pop")
            check(not pop.is_visible(), "悬浮面板默认隐藏")
            impl_node.hover()
            pop.wait_for(state="visible")
            check(pop.is_visible(), "鼠标悬浮后悬浮面板显示")
            markup = impl_node.inner_html()
            assert_pop_html(markup, impl_stats, "implement")
            # 未完成节点不渲染统计悬浮
            check(next_node.locator(".node-pop").count() == 0, "未开始节点无统计悬浮面板")

            # 悬浮只读：不改变任务状态
            detail = console.get_json(f"/api/task/{TASK_NAME}")
            check(detail["ok"] and detail["status"] == "running",
                  "悬浮展示不改变任务运行状态")
            return True
        finally:
            browser.close()


# ---------------------------------------------------------------- 降级 integration 路径

def run_degraded(console, o):
    print("  [DEGRADED] 无浏览器：D7/D8 按 goal.md 降级为 integration（真实控制台 + HTTP + 服务端页面脚本 DOM 求值）")
    detail = console.get_json(f"/api/task/{TASK_NAME}")
    check(detail.get("ok"), "控制台任务详情可读取")
    nodes = {n["id"]: n for n in detail["nodes"]}
    check(nodes["setup"]["stats"] is not None and nodes["implement"]["stats"] is not None,
          "已完成节点通过 HTTP 返回 stats")
    check(nodes["next"]["stats"] is None, "未开始节点 HTTP stats 为 None")
    impl_stats = nodes["implement"]["stats"]
    setup_stats = nodes["setup"]["stats"]
    html = console.get_text("/")
    script = _inline_script(html)
    rendered = _js_eval(script, page_script_harness(detail["nodes"]))
    assert_total_text(re.search(r'<div class="node-total">([^<]*)</div>', rendered["implement"]).group(1),
                      impl_stats, "implement")
    assert_total_text(re.search(r'<div class="node-total">([^<]*)</div>', rendered["setup"]).group(1),
                      setup_stats, "setup")
    check("node-total" not in rendered["next"], "未开始节点不渲染总耗时")
    assert_pop_html(rendered["implement"], impl_stats, "implement")
    check('class="node-pop"' in rendered["implement"], "悬浮面板结构由页面脚本真实渲染")
    check("node-pop" not in rendered["next"], "未开始节点不渲染悬浮面板")
    return False


def main():
    workdir = tempfile.mkdtemp(prefix="gdd-ns-e2e-")
    task_root, o = build_task(workdir)
    console = Console(workdir)
    real_browser = False
    try:
        # 只有「Playwright/浏览器不可用」才降级；页面断言失败或驱动超时必须让测试失败，
        # 否则界面缺陷会被降级路径掩盖。GDD_E2E_FORCE_DEGRADE=1 仅供测试降级分支本身。
        if os.environ.get("GDD_E2E_FORCE_DEGRADE") == "1":
            print("  [env] GDD_E2E_FORCE_DEGRADE=1：强制走降级 integration 路径")
        else:
            real_browser = run_browser(console, o)
        if not real_browser:
            run_degraded(console, o)
    finally:
        console.stop()
        shutil.rmtree(workdir, ignore_errors=True)
    if real_browser:
        print("\nALL NODESTATS UI E2E PASSED（真实浏览器 D7/D8）")
    else:
        print("\nALL NODESTATS UI E2E PASSED（DEGRADED：无浏览器，按 goal.md 降级为 integration）")


if __name__ == "__main__":
    main()
