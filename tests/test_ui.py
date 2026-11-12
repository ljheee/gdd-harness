"""UI 端到端测试（无浏览器，直连 HTTP API + stub agent）。

覆盖：
  T1 模板：内置 delivery 列表/只读；自定义模板 保存/读取/校验失败/复制内置/删除
  T2 任务创建：从自定义模板 init；非法任务名/未知模板 拒绝
  T3 全流程：run（后台线程）→ awaiting_approval → 驳回（带反馈）→ 再 awaiting →
     批准 → 完成；期间 detail 的节点 phase / 审批高亮字段 / history 校验
  T4 防护：非法任务名操作 / 未知 op / 重复 run 拒绝

运行：python3 tests/test_ui.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PYTHONPATH", ROOT)
os.environ["GDD_AGENT"] = "pi"

STUB = os.path.join(ROOT, "tests", "pi_stub.py")
OK_TEXT = "stub 主体工作完成"

PIPELINE_YAML = """\
id: ui-e2e
name: UI测试流水线
nodes:
  - id: first
    name: 第一节点
    description: 产出点东西
    approval:
      required: true
      prompt: 请确认第一节点
  - id: last
    name: 收尾节点
    description: 收尾
"""

PASS = 'GDD-VERDICT: {"verdict": "pass", "reason": "stub通过", "evidence": ["e1"], "feedback": ""}'


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class Client:
    def __init__(self, base):
        self.base = base

    def req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json",
                                            "X-GDD-Request": "e2e-test"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read().decode()
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {"ok": True, "_raw": raw}
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())

    def get(self, path):
        return self.req(path)

    def post(self, path, body):
        return self.req(path, "POST", body)

    def put(self, path, body):
        return self.req(path, "PUT", body)

    def delete(self, path):
        return self.req(path, "DELETE")


def wait_for(client, name, pred, timeout=60, desc=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/task/{name}")
        if d.get("ok") and pred(d):
            return d
        time.sleep(0.3)
    raise AssertionError(f"等待超时: {desc}")


def c_req_status(port, path):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    status = conn.getresponse().status
    conn.close()
    return status


def read_sse(port, path, seconds=3):
    """读一小段 SSE，把 data 行解析成事件。连接保持到超时。"""
    import socket
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.settimeout(0.4)
    req = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: keep-alive\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
    sock.close()
    events = []
    for block in buf.decode("utf-8", "replace").split("\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        try:
            events.append(json.loads(line[5:].strip()))
        except json.JSONDecodeError:
            continue
    return events


def main():
    from gdd import ui as gdd_ui

    workdir = tempfile.mkdtemp(prefix="gdd-ui-e2e-")
    # stub agent 环境（server 线程内 run 会继承本进程 env）
    script = os.path.join(workdir, "stub.json")
    with open(script, "w", encoding="utf-8") as f:
        json.dump([{"patterns": ["GDD-ROLE"], "text": PASS}], f, ensure_ascii=False)
    os.environ["GDD_AGENT_BIN"] = sys.executable
    # stub 走 PRE_ARGS：cmd = python3 pi_stub.py --print ...
    os.environ["GDD_AGENT_PRE_ARGS"] = STUB
    os.environ["GDD_PI_STUB_SCRIPT"] = script
    pi_home = tempfile.mkdtemp(prefix="gdd-ui-pi-")
    settings = os.path.join(pi_home, "settings.json")
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "baseUrl": "https://api.example.com/v1",
                   "apiKey": "k", "model": "m"}, f)
    os.environ["GDD_PI_SETTINGS"] = settings
    os.environ["GDD_PI_AGENT_DIR"] = os.path.join(pi_home, "agent")
    os.environ["GDD_PI_SESSION_DIR"] = os.path.join(pi_home, "sessions")
    os.environ.pop("GDD_STEER", None)

    gdd_ui.Handler.root = workdir
    srv = gdd_ui.ThreadingHTTPServer(("127.0.0.1", 0), gdd_ui.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(f"http://127.0.0.1:{port}")
    print(f"[setup] server on {port}, workdir {workdir}")

    # ---------- T1 模板管理 ----------
    print("[T1] 模板管理")
    ov = c.get("/api/overview")
    check(ov["ok"], "overview ok")
    names = [t["name"] for t in ov["templates"]]
    check("delivery" in names, "内置 delivery 在模板列表")
    check(ov["tasks"] == [], "初始无任务")

    t = c.get("/api/template/delivery")
    check(t["ok"] and t["builtin"] and "nodes" in t["content"], "内置模板可读")

    r = c.put("/api/template/delivery", {"content": "id: x\nnodes: []"})
    check(not r["ok"], "内置模板只读")

    r = c.put("/api/template/mine", {"content": "{ bad json"})
    check(not r["ok"] and "模板不合法" in r["error"], "坏内容拒绝保存")
    r = c.put("/api/template/mine", {"content": "foo: [\n"})
    check(not r["ok"] and "模板不合法" in r.get("error", ""), "非法 YAML 拒绝保存")

    r = c.put("/api/template/mine", {"content": PIPELINE_YAML})
    check(r["ok"], "自定义模板保存（YAML 校验通过）")
    t = c.get("/api/template/mine")
    check(t["ok"] and t["content"] == PIPELINE_YAML, "自定义模板可读回")

    r = c.put("/api/template/mine2", {"content": t["content"]})
    check(r["ok"], "保存第二份模板")
    r = c.delete("/api/template/mine2")
    check(r["ok"], "删除模板")
    t = c.get("/api/template/mine2")
    check(not t["ok"], "删除后 404")

    # 复制内置为新模板（fork）：内容必须合法
    d = c.get("/api/template/delivery")
    r = c.put("/api/template/delivery", {"content": d["content"], "fork": "forked"})
    check(r["ok"], "复制内置模板为 forked")
    t = c.get("/api/template/forked")
    check(t["ok"] and not t["builtin"], "forked 可编辑")

    # ---------- T2 任务创建 ----------
    print("[T2] 任务创建")
    r = c.post("/api/tasks", {"name": "../evil", "template": "mine", "requirement": "x"})
    check(not r["ok"], "路径穿越任务名拒绝")
    r = c.post("/api/tasks", {"name": "t1", "template": "nope", "requirement": "x"})
    check(not r["ok"], "未知模板拒绝")
    bad_yaml = os.path.join(workdir, ".gdd-templates", "broken.yaml")
    os.makedirs(os.path.dirname(bad_yaml), exist_ok=True)
    with open(bad_yaml, "w", encoding="utf-8") as f:
        f.write("foo: [\n")
    try:
        r = c.post("/api/tasks", {"name": "from-bad-yaml", "template": "broken", "requirement": "x"})
    except Exception as e:
        raise AssertionError(f"非法 YAML 创建任务不应断开连接: {e}")
    check(not r["ok"] and "无法解析" in r.get("error", ""),
          f"非法 YAML 创建任务返回 JSON（{r.get('error')}）")
    r = c.post("/api/tasks", {"name": "t1", "template": "mine", "requirement": "做个小工具"})
    check(r["ok"], "任务创建成功")
    r = c.post("/api/tasks", {"name": "proj-a", "template": "delivery", "requirement": "x"})
    check(not r["ok"] and "仓库" in r.get("error", ""),
          f"delivery 缺仓库被拒绝（{r.get('error')}）")
    src = os.path.join(workdir, "src-repo")
    os.makedirs(src)
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=src, check=True)
    subprocess.run(["git", "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
                    "commit", "--allow-empty", "-m", "base"], cwd=src, check=True)
    gdd_ui.Handler.repos = os.path.join(workdir, "code")
    r = c.post("/api/tasks", {"name": "round-1", "template": "delivery",
                              "requirement": "做评审",
                              "repos": [{"url": src, "base": "master"}]})
    check(r["ok"], f"填写仓库后可以创建（{r.get('error')}）")
    with open(os.path.join(workdir, "round-1", ".gdd", "pipeline.json"), encoding="utf-8") as f:
        pipe_text = f.read()
    check("goal.md" in pipe_text and "iterations/" not in pipe_text,
          "产物直接在任务目录，不再写 iterations/")
    check(os.path.isdir(os.path.join(workdir, "round-1", "worktrees")), "任务目录里有 worktrees")
    kept = os.path.join(workdir, "round-1", "goal.md")
    with open(kept, "w", encoding="utf-8") as f:
        f.write("上一轮\n")
    r = c.post("/api/task/round-1/action", {"op": "reset"})
    check(r["ok"], "重置后可以再初始化")
    r = c.post("/api/tasks", {"name": "round-1", "template": "delivery",
                              "requirement": "下一轮",
                              "repos": [{"url": src, "base": "master"}]})
    check(not r["ok"] and "已有" in r.get("error", ""),
          f"同名任务目录拒绝覆盖（{r.get('error')}）")
    check(open(kept, encoding="utf-8").read() == "上一轮\n", "已有任务文件保持不动")
    r = c.post("/api/tasks", {"name": "round-2", "template": "delivery",
                              "requirement": "下一轮",
                              "repos": [{"url": src, "base": "master"}]})
    check(r["ok"], f"换任务名可以再初始化（{r.get('error')}）")
    check(open(kept, encoding="utf-8").read() == "上一轮\n", "新任务不改上一轮目录")
    check(os.path.exists(os.path.join(workdir, "t1", ".gdd", "state.json")), "state.json 已生成")
    d = c.get("/api/task/t1")
    check(d["ok"] and d["status"] == "running" and d["nodes"][0]["phase"] == "pending",
          "初始 detail：running + first pending")

    # ---------- T3 全流程（run→驳回→批准→完成）----------
    print("[T3] 全流程")
    r = c.post("/api/task/t1/action", {"op": "run"})
    check(r["ok"] and r.get("started"), "run 启动（后台线程）")
    r = c.post("/api/task/t1/action", {"op": "run"})
    check(not r["ok"], "重复 run 拒绝")

    d = wait_for(c, "t1", lambda x: x["status"] == "awaiting_approval", desc="first 审批挂起")
    pa = d["pending_approval"]
    check(pa and pa["node"] == "first" and "请确认第一节点" in pa["prompt"],
          "审批挂起：节点 + prompt 正确")
    n0 = d["nodes"][0]
    check(n0["phase"] == "exit_passed" and n0["needs_approval"], "first 处于 exit_passed 且需审批")

    r = c.post("/api/task/t1/action", {"op": "reject", "feedback": "再加点细节"})
    check(r["ok"], "驳回成功")
    d = wait_for(c, "t1", lambda x: x["status"] == "awaiting_approval", desc="驳回后再次挂起")
    n0 = d["nodes"][0]
    check(n0["bounces"] == 1, "驳回计入 bounces")
    hist_events = [h["event"] for h in d["history_tail"]]
    check("approval_reject" in hist_events, "history 记录 approval_reject")

    r = c.post("/api/task/t1/action", {"op": "approve"})
    check(r["ok"], "批准成功")
    d = wait_for(c, "t1", lambda x: x["status"] == "completed", desc="批准后完成全流程")
    check(all(n["phase"] == "done" for n in d["nodes"]), "全部节点 done")
    check(not d["running"], "运行线程已退出")
    ov = c.get("/api/overview")
    t1 = [t for t in ov["tasks"] if t["name"] == "t1"][0]
    check(t1["status"] == "completed" and t1["awaiting"] is False, "任务列表显示完成")

    # ---------- T4 防护杂项 ----------
    print("[T4] 防护")
    r = c.get("/api/task/" + urllib.parse.quote("不存在"))
    check(not r["ok"], "不存在任务 404")
    r = c.post("/api/task/t1/action", {"op": "explode"})
    check(not r["ok"], "未知 op 拒绝")
    r = c.get("/api/task/../etc")
    check(not r["ok"], "非法任务名请求拒绝")
    r = c.get("/")
    check(r.get("ok") and len(r.get("_raw", "")) > 1000, "首页 HTML 可达")

    # ---------- T5 CR 回归：中文名 URL / 重复创建 / fork 覆盖 / reject 转 blocked / running 防护 ----------
    print("[T5] CR 回归")
    # 中文名任务：encodeURIComponent 路径能取到详情、能操作
    r = c.post("/api/tasks", {"name": "中文任务", "template": "mine", "requirement": "r"})
    check(r["ok"], "中文任务名创建")
    d = c.get("/api/task/" + urllib.parse.quote("中文任务"))
    check(d["ok"] and d["nodes"][0]["phase"] == "pending", "中文任务名 URL unquote 后可读详情")
    r = c.post("/api/task/中文任务/action".replace("中文任务", urllib.parse.quote("中文任务")), {"op": "explode"})
    check(not r["ok"] and "未知操作" in r["error"], "中文任务名 action 路由 unquote 正常（未知 op 拒绝而非非法名）")
    # 中文名模板：GET/PUT/DELETE
    r = c.put("/api/template/" + urllib.parse.quote("中文模板"), {"content": PIPELINE_YAML})
    check(r["ok"], "中文模板名保存")
    r = c.get("/api/template/" + urllib.parse.quote("中文模板"))
    check(r["ok"] and r["content"] == PIPELINE_YAML, "中文模板名可读回")
    r = c.delete("/api/template/" + urllib.parse.quote("中文模板"))
    check(r["ok"], "中文模板名可删除")
    # 重复创建任务拒绝
    r = c.post("/api/tasks", {"name": "t1", "template": "mine", "requirement": "r"})
    check(not r["ok"] and "已存在" in r["error"], "重复创建任务拒绝（防覆盖）")
    # fork 覆盖已存在模板拒绝
    d = c.get("/api/template/delivery")
    r = c.put("/api/template/delivery", {"content": d["content"], "fork": "mine"})
    check(not r["ok"] and "已存在" in r["error"], "fork 覆盖已存在模板拒绝")
    # 终态任务 run 拒绝
    r = c.post("/api/task/t1/action", {"op": "run"})
    check(not r["ok"] and "终态" in r["error"], "终态任务 run 拒绝")
    # 无待审批时 approve/reject 拒绝
    r = c.post("/api/task/t1/action", {"op": "approve"})
    check(not r["ok"] and "没有待审批" in r["error"], "无待审批项时 approve 拒绝")
    # running 中 reset/abort 防护（注入假运行标记）
    from gdd import ui as gdd_ui
    import threading as _th
    fake = _th.Thread(target=lambda: None, daemon=True)
    gdd_ui._running["中文任务"] = fake
    try:
        r = c.post("/api/task/" + urllib.parse.quote("中文任务") + "/action", {"op": "reset"})
        check(not r["ok"] and "正在运行" in r["error"], "运行中 reset 拒绝")
        r = c.post("/api/task/" + urllib.parse.quote("中文任务") + "/action", {"op": "abort"})
        check(not r["ok"] and "正在运行" in r["error"], "运行中 abort 拒绝")
    finally:
        gdd_ui._running.pop("中文任务", None)
    # reject 打回至 blocked（max_bounces=1）时响应 ok=true 且状态 blocked
    yaml_b1 = PIPELINE_YAML.replace("    approval:\n", "    max_bounces: 1\n    approval:\n")
    r = c.put("/api/template/strict", {"content": yaml_b1})
    check(r["ok"], "strict 模板（max_bounces=1）保存")
    r = c.post("/api/tasks", {"name": "t-strict", "template": "strict", "requirement": "r"})
    check(r["ok"], "strict 任务创建")
    c.post("/api/task/t-strict/action", {"op": "run"})
    wait_for(c, "t-strict", lambda x: x["status"] == "awaiting_approval", desc="strict 审批挂起")
    r = c.post("/api/task/t-strict/action", {"op": "reject", "feedback": "不行"})
    check(r["ok"], "reject 打回至 blocked 时响应仍 ok（动作已受理，不再误报失败）")
    d = wait_for(c, "t-strict", lambda x: x["status"] == "blocked", desc="strict 转 blocked")
    check(d["blocked_reason"], "blocked 原因已展示")

    # ---------- T6 独立 CR 回归：CSRF guard / DELETE 穿越 / awaiting 态 run / 大 body / _bg_run 防毁盘 / 外部锁 ----------
    print("[T6] 独立 CR 回归")
    # 缺 X-GDD-Request 头 → 403（跨站 no-cors fetch 无法带自定义头）
    r = urllib.request.Request(c.base + "/api/tasks", data=b"{}", method="POST",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
    check(not body.get("ok") and "X-GDD-Request" in body.get("error", ""), "缺自定义头 403（CSRF 防护）")
    # 伪造 Host（DNS rebinding）→ 403
    r = urllib.request.Request(c.base + "/api/tasks", data=b"{}", method="POST",
                               headers={"Content-Type": "application/json", "X-GDD-Request": "1", "Host": "evil.com"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
    check(not body.get("ok"), "伪造 Host 403（DNS rebinding 防护）")
    # DELETE 非法名（路径穿越）→ 400
    r = c.delete("/api/template/" + urllib.parse.quote("../../evil"))
    check(not r["ok"], "DELETE 路径穿越名拒绝")
    # text/plain body → 403
    r = urllib.request.Request(c.base + "/api/tasks", data=b'{"name":"x"}', method="POST",
                               headers={"Content-Type": "text/plain", "X-GDD-Request": "1"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
    check(not body.get("ok"), "text/plain 内容类型 403")
    # blocked 态点 run → 引导重试（t-strict 已因 reject 转 blocked）
    r = c.post("/api/task/t-strict/action", {"op": "run"})
    check(not r["ok"] and "重试" in r["error"], "blocked 态 run 拒绝并引导重试")
    # awaiting 态点 run → 拒绝（防空调用挤掉自动续跑）
    c.post("/api/tasks", {"name": "t-await", "template": "mine", "requirement": "r"})
    c.post("/api/task/t-await/action", {"op": "run"})
    wait_for(c, "t-await", lambda x: x["status"] == "awaiting_approval", desc="t-await 审批挂起")
    r = c.post("/api/task/t-await/action", {"op": "run"})
    check(not r["ok"] and "批准" in r["error"], "awaiting 态 run 拒绝")
    # 收尾：批准让 t-await 走完，避免残留后台线程
    c.post("/api/task/t-await/action", {"op": "approve"})
    wait_for(c, "t-await", lambda x: x["status"] == "completed", desc="t-await 完成")
    # 大 body → 400
    r = c.put("/api/template/big", {"content": "x" * 2_000_000})
    check(not r["ok"] and "过大" in r["error"], "超 1MB 请求体拒绝")
    # _bg_run 对损坏 state.json：不写盘、不覆盖为残缺 state
    import gdd.ui as ui_mod
    corrupt = c.post("/api/tasks", {"name": "t-corrupt", "template": "mine", "requirement": "r"})
    check(corrupt["ok"], "t-corrupt 创建")
    sp = os.path.join(workdir, "t-corrupt", ".gdd", "state.json")
    with open(sp, "w", encoding="utf-8") as f:
        f.write("{corrupted!!")
    ui_mod._bg_run(os.path.join(workdir, "t-corrupt"), "t-corrupt")
    with open(sp, encoding="utf-8") as f:
        check(f.read() == "{corrupted!!", "_bg_run 对损坏 state 不覆盖（不产生残缺 blocked state）")
    # 外部进程持锁：run no-op，不置 blocked、不 stale 覆盖
    import fcntl
    lock_fd = os.open(os.path.join(workdir, "中文任务", ".gdd", "lock"), os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        sp2 = os.path.join(workdir, "中文任务", ".gdd", "state.json")
        before = open(sp2, encoding="utf-8").read()
        ui_mod._bg_run(os.path.join(workdir, "中文任务"), "中文任务")
        check(open(sp2, encoding="utf-8").read() == before, "外部持锁时 _bg_run no-op（不 stale 覆盖状态）")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    # 结构性非法模板（nodes 为字符串列表）创建任务：必须返回 JSON 400，而非
    # TypeError 逃出 handler 导致连接断开无响应（第七轮 CR P2-1 回归）
    bad_tpl = os.path.join(workdir, ".gdd-templates", "bad-struct.json")
    with open(bad_tpl, "w", encoding="utf-8") as f:
        json.dump({"name": "bad-struct", "nodes": ["n1", "n2"]}, f)
    r = c.post("/api/tasks", {"name": "t-badstruct", "template": "bad-struct", "requirement": "r"})
    check(not r["ok"] and r.get("error"), "结构性非法模板返回 JSON 错误（不断连）")
    # 损坏 state 任务仍出现在任务列表（status=corrupt），前端有入口自救 reset
    sp3 = os.path.join(workdir, "t-corrupt", ".gdd", "state.json")
    with open(sp3, "w", encoding="utf-8") as f:
        f.write("{corrupted!!")
    ov2 = c.get("/api/overview")
    names2 = {t["name"]: t.get("status") for t in ov2.get("tasks", [])}
    check(names2.get("t-corrupt") == "corrupt", "损坏 state 任务以 corrupt 状态列出（UI 可见可 reset）")

    # ---------- T7 运行中滚动 + working 补充（与 gdd steer 同一队列）----------
    print("[T7] 运行进度 SSE 与 steer")
    page = c.get("/")
    raw_page = page.get("_raw") or ""
    check("EventSource" in raw_page and "sendSteer" in raw_page and "liveLog" in raw_page,
          "页面带滚动区与补充入口")
    import socket
    missing = c_req_status(port, "/api/task/no-such-task/live")
    check(missing == 404, f"不存在的任务不挂 SSE（{missing}）")

    # 环形缓冲丢掉最旧的，总长度有上限
    for i in range(200):
        gdd_ui.live_append("ring-task", "text", "字" * 1000)
    ring = gdd_ui.live_wait("ring-task", 0, 0.01)
    total = sum(len(ev.get("text") or "") for ev in ring)
    check(total <= gdd_ui.LIVE_MAX_CHARS, f"滚动区不超过 {gdd_ui.LIVE_MAX_CHARS} 字（{total}）")
    check(ring and ring[0]["id"] > 1, "最旧的片段已被丢掉")
    gdd_ui.live_clear("ring-task")

    with open(script, "w", encoding="utf-8") as f:
        json.dump([{
            "patterns": ["GDD-ROLE"],
            "text": PASS,
            "tools": [{"name": "write", "args": {"path": "docs/out.md"}}],
        }], f, ensure_ascii=False)
    live_body = json.dumps({
        "id": "live", "name": "滚动",
        "nodes": [{
            "id": "first", "name": "第一节点", "description": "产出",
            "entry": {"goal": "可以开始"},
            "exit": {"goal": "做完了"},
            "approval": {"required": True, "prompt": "确认"},
        }],
    }, ensure_ascii=False)
    saved = c.put("/api/template/live-ui", {"content": live_body})
    check(saved["ok"], f"带 AI 门禁的模板（{saved}）")
    made = c.post("/api/tasks", {"name": "t-live", "template": "live-ui", "requirement": "看进度"})
    check(made["ok"], f"t-live 创建（{made}）")
    c.post("/api/task/t-live/action", {"op": "run"})
    wait_for(c, "t-live", lambda x: x["status"] == "awaiting_approval", desc="t-live 跑到审批")
    live = read_sse(port, "/api/task/t-live/live", seconds=3)
    texts = "".join(ev.get("text") or "" for ev in live if ev.get("kind") == "text")
    phases = " | ".join(ev.get("text") or "" for ev in live if ev.get("kind") == "phase")
    tools = " | ".join(ev.get("text") or "" for ev in live if ev.get("kind") == "tool")
    check("正在跑入口门禁" in phases and "正在跑主体会话" in phases and "正在跑出口门禁" in phases,
          f"阶段行含入口/主体/出口（{phases}）")
    check(texts.count("stub通过") == 1, f"只有主体会话把模型文本推进滚动区（{texts.count('stub通过')}）")
    check("docs/out.md" in tools, f"工具行留在滚动区（{tools}）")

    # 持锁时仍可补充，且不改 state
    steer_task = c.post("/api/tasks", {"name": "t-steer", "template": "mine", "requirement": "补充"})
    check(steer_task["ok"], "t-steer 创建")
    state_path = os.path.join(workdir, "t-steer", ".gdd", "state.json")
    before = open(state_path, encoding="utf-8").read()
    import fcntl
    lock_fd = os.open(os.path.join(workdir, "t-steer", ".gdd", "lock"), os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = c.post("/api/task/t-steer/action", {"op": "steer", "message": "不要用 Redis"})
        check(r.get("ok") and "下一次主体会话" in r.get("hint", ""), f"持锁入队并说明送达时机（{r}）")
        from gdd import steer as steer_mod
        check("不要用 Redis" in steer_mod.pending(os.path.join(workdir, "t-steer")), "补充进了 steer 队列")
        check(open(state_path, encoding="utf-8").read() == before, "补充不改 state.json")
        r = c.post("/api/task/t-steer/action", {"op": "steer", "message": ""})
        check(not r["ok"], "空补充拒绝")
        r = c.post("/api/task/t-steer/action", {"op": "steer", "message": "长" * 8001})
        check(not r["ok"] and "过长" in r["error"], "超长补充拒绝")
        os.environ["GDD_STEER"] = "off"
        try:
            r = c.post("/api/task/t-steer/action", {"op": "steer", "message": "关掉了"})
            check(not r["ok"] and "off" in r["error"], "GDD_STEER=off 拒绝入队")
            check("关掉了" not in steer_mod.pending(os.path.join(workdir, "t-steer")), "关闭时不入队")
        finally:
            os.environ.pop("GDD_STEER", None)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    # 澄清对话也进滚动区（门禁不在这次调用里）
    cl_body = json.dumps({
        "id": "cl", "name": "澄清",
        "nodes": [{"id": "c1", "name": "澄清节点", "clarify": True, "description": "问清楚"}],
    }, ensure_ascii=False)
    saved = c.put("/api/template/clarify-ui", {"content": cl_body})
    check(saved["ok"], f"澄清模板（{saved}）")
    made = c.post("/api/tasks", {"name": "t-cl", "template": "clarify-ui", "requirement": "问"})
    check(made["ok"], f"t-cl 创建（{made}）")
    r = c.post("/api/task/t-cl/action", {"op": "chat", "node": "c1", "message": "先确认范围"})
    check(r.get("started"), f"澄清已受理（{r}）")
    wait_for(c, "t-cl", lambda x: not x["running"] and any(
        d.get("role") == "assistant" for d in x.get("dialog_tail") or []), desc="澄清回复落盘")
    live = read_sse(port, "/api/task/t-cl/live", seconds=2)
    blob = " | ".join(ev.get("text") or "" for ev in live)
    check("先确认范围" in blob and "正在澄清对话" in blob and "stub通过" in blob,
          f"澄清轮的用户句、阶段和模型文本都在滚动区（{blob[:300]}）")

    srv.shutdown()
    print("\n全部通过 ✔")


if __name__ == "__main__":
    main()
