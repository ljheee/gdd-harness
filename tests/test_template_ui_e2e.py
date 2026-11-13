"""模板可视化改造的浏览器 e2e 测试（Playwright 仅测试使用，不是产品依赖）。

覆盖 DoD：
  D1  内置 delivery 模板以可视化视图展示：可视化区域存在，节点名称/ID 顺序与
      `gdd/templates/delivery.json` 的 nodes 顺序一致，默认不是仅 <pre> 原文。
  D2  程序生成的多节点 JSON 模板以可视化视图展示，节点名称/顺序与 fixture 一致。
  D5  fork 内置模板后用可视化表单修改节点名称、描述、subject.goal、
      approval.required、entry.artifacts.path，保存后重新打开值保持。
  D7  通过界面新建模板：列表可见、可打开（至少一个节点）、可初始化任务。
  D9  源码模式与 YAML 兼容：在真实页面打开源码视图、编辑 textarea 并通过页面
      保存 JSON/YAML 原文；未修改直接保存后 content 与原文逐字符一致，修改后
      GET 读回落盘原文一致；YAML 默认回落源码视图且不渲染可视化节点。
  D11 组合验证：查看内置模板 → fork → 编辑保存 → 新建模板 → 用新模板建任务。
  D12 含合法 mcp/skills、未知顶层键、未知节点键的模板经可视化表单保存后，
      未展示键值原样保留，且该模板仍可初始化任务。

运行（需要可导入 playwright 的 Python 解释器）：
  python3.12 tests/test_template_ui_e2e.py
或：
  PYTHONPATH=/path/to/playwright/site-packages python3.12 tests/test_template_ui_e2e.py

脚本用子进程启动真实控制台（临时工作台 + 随机端口），再用系统 Chrome 的
headless 模式驱动：`chromium.launch(channel="chrome", headless=True)`（Playwright
自带的 bundled headless shell 在本机可能未安装）。产品运行时不依赖 Playwright。
缺少 Playwright 或浏览器时脚本以非 0 退出并打印明确错误，不得记为通过。
可用 GDD_E2E_PYTHON 指定启动控制台的解释器（默认 sys.executable）。
"""
from __future__ import annotations

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
DELIVERY = os.path.join(ROOT, "gdd", "templates", "delivery.json")

VISUAL = '[data-view="template-visual"]'
NODE_CARD = VISUAL + " .tpl-node[data-node-id]"


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def section(title):
    print(f"\n[{title}]")


def assert_json_equal(actual, expected, path="$"):
    """递归比较 JSON：键集合、值、类型（bool 与 int 严格区分）均须相等。"""
    if type(actual) is not type(expected):
        raise AssertionError(
            f"{path}: 类型不一致 actual={type(actual).__name__} "
            f"expected={type(expected).__name__}（{actual!r} != {expected!r}）")
    if isinstance(expected, dict):
        if set(actual) != set(expected):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise AssertionError(f"{path}: 键集合不一致 missing={missing} extra={extra}")
        for key in expected:
            assert_json_equal(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        if len(actual) != len(expected):
            raise AssertionError(
                f"{path}: 列表长度不一致 actual={len(actual)} expected={len(expected)}")
        for idx, (a, e) in enumerate(zip(actual, expected)):
            assert_json_equal(a, e, f"{path}[{idx}]")
    elif actual != expected:
        raise AssertionError(f"{path}: 值不一致 actual={actual!r} expected={expected!r}")


# ---------------------------------------------------------------- HTTP / 进程


class Client:
    def __init__(self, base):
        self.base = base

    def req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-GDD-Request": "tpl-e2e"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": False, "error": raw}

    def get(self, path):
        return self.req(path)

    def post(self, path, body):
        return self.req(path, "POST", body)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def configure_stub_env(workdir):
    """与 tests/test_ui.py 一致的 stub agent 环境。

    e2e 只做模板 UI 与任务 init，不调用 run；配置 stub 是为了满足 DoD 的
    「临时工作台 + stub agent」环境，且不依赖真实模型端点。
    """
    script = os.path.join(workdir, "stub.json")
    with open(script, "w", encoding="utf-8") as f:
        json.dump([{"patterns": ["GDD-ROLE"], "text": "stub 主体工作完成"}],
                  f, ensure_ascii=False)
    pi_home = os.path.join(workdir, "pi-home")
    os.makedirs(pi_home, exist_ok=True)
    settings = os.path.join(pi_home, "settings.json")
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "baseUrl": "https://api.example.com/v1",
                   "apiKey": "k", "model": "m"}, f)
    os.environ["GDD_AGENT"] = "pi"
    os.environ["GDD_AGENT_BIN"] = sys.executable
    os.environ["GDD_AGENT_PRE_ARGS"] = STUB
    os.environ["GDD_PI_STUB_SCRIPT"] = script
    os.environ["GDD_PI_SETTINGS"] = settings
    os.environ["GDD_PI_AGENT_DIR"] = os.path.join(pi_home, "agent")
    os.environ["GDD_PI_SESSION_DIR"] = os.path.join(pi_home, "sessions")
    os.environ.pop("GDD_STEER", None)


class Console:
    """真实控制台的子进程（随机端口，临时工作台）。"""

    def __init__(self, workdir):
        self.workdir = workdir
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = os.path.join(workdir, "console.log")
        python = os.environ.get("GDD_E2E_PYTHON") or sys.executable
        env = os.environ.copy()
        env["PYTHONPATH"] = ROOT + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("GDD_STEER", None)
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [python, "-m", "gdd.ui", "--root", workdir,
             "--port", str(self.port), "--no-open", "--local"],
            cwd=ROOT, env=env, stdout=self._log, stderr=subprocess.STDOUT)
        self._wait_ready()

    def _wait_ready(self, timeout=25):
        deadline = time.time() + timeout
        client = Client(self.base)
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"控制台进程提前退出（code={self.proc.returncode}）：\n{self.log_tail()}")
            try:
                if client.get("/api/overview").get("ok"):
                    return
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass
            time.sleep(0.1)
        raise RuntimeError(f"控制台未在 {timeout}s 内就绪：\n{self.log_tail()}")

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


# ---------------------------------------------------------------- 页面操作


def open_home(page, base):
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector("#main")


def open_template(page, base, name):
    """刷新到首页后经侧栏点击模板名打开模板页（真实用户路径）。"""
    open_home(page, base)
    btn = page.locator("#navTpl button.item").filter(has_text=name)
    btn.first.wait_for(state="visible")
    btn.first.click()
    page.wait_for_function(
        "n => { const h = document.querySelector('#main h2');"
        " return !!h && h.textContent.indexOf('模板：' + n) >= 0; }",
        arg=name)
    page.wait_for_selector("#tplBody")


def wait_visual(page):
    page.wait_for_selector(VISUAL, state="visible")


def node_ids(page):
    return page.eval_on_selector_all(
        NODE_CARD, "els => els.map(e => e.dataset.nodeId)")


def node_names(page):
    return page.eval_on_selector_all(
        NODE_CARD, "els => els.map(e => (e.querySelector('.nm')?.textContent || '').trim())")


def node_indexes(page):
    return page.eval_on_selector_all(
        NODE_CARD, "els => els.map(e => Number(e.dataset.nodeIndex))")


def assert_visual_default(page):
    wait_visual(page)
    check(page.locator(VISUAL).count() == 1, "可视化视图区域存在")
    check(page.locator('pre[data-view="template-source"]').count() == 0,
          "默认视图不渲染源码 <pre>（原始 JSON 不是默认唯一展示）")
    cls = page.locator("#tplTabVisual").get_attribute("class") or ""
    check("on" in cls.split(), "「可视化」标签为当前激活视图")
    check(page.locator(NODE_CARD).count() > 0, "主区域渲染节点卡片而非仅原文")


def assert_nodes(page, expected_ids, expected_names):
    wait_visual(page)
    ids = node_ids(page)
    names = node_names(page)
    check(ids == expected_ids, f"DOM 节点 ID 顺序与模板 nodes 一致: {ids}")
    check(names == expected_names, f"DOM 节点名称顺序与模板 nodes 一致: {names}")
    check(node_indexes(page) == list(range(len(expected_ids))),
          "节点 data-node-index 按模板顺序连续")
    check(page.locator("#tplDetail .tpl-detail").count() >= 1, "节点详情区域存在")


def select_node(page, index):
    page.locator(f'{VISUAL} .tpl-node[data-node-index="{index}"]').click()
    page.wait_for_selector(f'#tplDetail [data-node-detail][data-node-index="{index}"]')


def field(page, name):
    return page.locator(f'[data-field="{name}"]').first


def fill_field(page, name, value):
    loc = field(page, name)
    loc.wait_for(state="visible")
    loc.fill(value)


def input_value(page, name):
    return field(page, name).input_value()


def set_checkbox(page, name, checked):
    loc = field(page, name)
    loc.wait_for(state="visible")
    if checked:
        loc.check()
    else:
        loc.uncheck()


def is_checked(page, name):
    return field(page, name).is_checked()


def save_visual(page):
    btn = page.locator("#tplSaveBtn")
    btn.wait_for(state="visible")
    check("on" in (page.locator("#tplTabVisual").get_attribute("class") or "").split(),
          "保存时处于可视化视图（非源码模式）")
    btn.click()
    page.wait_for_function(
        "() => { const e = document.querySelector('#tplErr');"
        " return !!e && e.textContent.indexOf('已保存') >= 0; }")
    check(page.locator("#tplErr .alert.err").count() == 0, "保存后页面无错误提示")


def open_source_view(page):
    """切到模板页「源码」视图并等待只读 <pre> 出现。"""
    page.locator("#tplTabSource").click()
    page.wait_for_selector('pre[data-view="template-source"]')


def source_text(page):
    """源码查看视图中的原文（textContent 保留 <pre> 内的空白与换行）。"""
    return page.locator('pre[data-view="template-source"]').text_content()


def enter_source_edit(page):
    """从源码查看模式进入源码编辑模式（textarea）。"""
    page.locator("#tplEditBtn").click()
    page.wait_for_selector("#tplEdit")


def save_source(page):
    """在源码编辑模式点击页面保存按钮并等待成功提示。"""
    btn = page.locator("#tplSaveBtn")
    btn.wait_for(state="visible")
    cls = page.locator("#tplTabSource").get_attribute("class") or ""
    check("on" in cls.split(), "保存时处于源码视图")
    btn.click()
    page.wait_for_function(
        "() => { const e = document.querySelector('#tplErr');"
        " return !!e && e.textContent.indexOf('已保存') >= 0; }")
    check(page.locator("#tplErr .alert.err").count() == 0,
          "源码保存后页面无错误提示")


def fork_template(page, new_name):
    page.once("dialog", lambda dialog: dialog.accept(new_name))
    page.locator("button", has_text="复制为新模板").first.click()
    page.wait_for_function(
        "n => { const h = document.querySelector('#main h2');"
        " return !!h && h.textContent.indexOf('模板：' + n) >= 0; }",
        arg=new_name)
    wait_visual(page)


def create_template_ui(page, name, display=""):
    page.locator('[data-action="new-template"]').click()
    page.wait_for_selector('[data-view="template-new"]')
    page.fill("#newTplName", name)
    if display:
        page.fill("#newTplDisplay", display)
    page.locator('[data-action="create-template"]').click()
    page.wait_for_function(
        "n => { const h = document.querySelector('#main h2');"
        " return !!h && h.textContent.indexOf('模板：' + n) >= 0; }",
        arg=name)
    wait_visual(page)


# ---------------------------------------------------------------- 数据 / 接口


def get_template(client, name):
    r = client.get("/api/template/" + urllib.parse.quote(name))
    check(r.get("ok"), f"GET /api/template/{name} 成功"
          + (f"（{r['error']}）" if r.get("error") else ""))
    return json.loads(r["content"])


def create_task(client, workdir, name, template):
    r = client.post("/api/tasks", {"name": name, "template": template,
                                   "requirement": f"e2e requirement for {name}"})
    check(r.get("ok"), f"POST /api/tasks 用模板 {template} 初始化成功（{r.get('error')}）")
    pipe_path = os.path.join(workdir, name, ".gdd", "pipeline.json")
    check(os.path.isfile(pipe_path), f"任务 {name} 生成 .gdd/pipeline.json")
    with open(pipe_path, encoding="utf-8") as f:
        return json.load(f)


def write_template(workdir, name, data):
    tdir = os.path.join(workdir, ".gdd-templates")
    os.makedirs(tdir, exist_ok=True)
    path = os.path.join(tdir, name + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def fs_case_insensitive(workdir):
    """探测 workdir 所在文件系统是否大小写不敏感（macOS 默认）。"""
    tag = uuid.uuid4().hex[:8]
    upper = os.path.join(workdir, f"CaseProbe_{tag}")
    lower = os.path.join(workdir, f"caseprobe_{tag}")
    with open(upper, "w", encoding="utf-8") as f:
        f.write("x")
    try:
        return os.path.exists(lower)
    finally:
        os.remove(upper)


def build_plain_template(tag, count=4):
    """程序生成的多节点合法 JSON 模板（名称/顺序均随 tag 变化）。"""
    nodes = []
    for i in range(count):
        nodes.append({
            "id": f"n{i}_{tag}",
            "name": f"节点-{tag}-{i}",
            "description": f"描述 {tag} {i}",
            "subject": {"goal": f"目标 {tag} {i}"},
        })
    return {
        "id": f"tpl_{tag}",
        "name": f"模板 {tag}",
        "description": f"fixture {tag}",
        "session_preset": "independent",
        "git_delivery": {"enabled": False, "branch": f"gdd/{tag}"},
        "nodes": nodes,
    }


def build_preserve_template(tag):
    """含合法 mcp/skills、未知顶层键与未知节点键的合法 JSON 模板。"""
    return {
        "id": f"tpl_{tag}",
        "name": f"模板 {tag}",
        "description": f"初始描述 {tag}",
        "session_preset": "independent",
        "git_delivery": {"enabled": False, "branch": f"gdd/{tag}"},
        f"x-e2e-top-{tag}": [f"sentinel-top-{tag}"],
        "nodes": [{
            "id": f"n_{tag}",
            "name": f"节点-{tag}",
            "description": f"节点初始描述 {tag}",
            "mcp": [{"name": f"e2e-mcp-{tag}", "url": "https://example.invalid/mcp"}],
            "skills": [{"path": f"skills/e2e-{tag}", "name": f"e2e_skill_{tag}"}],
            f"x-e2e-node-{tag}": {"sentinel": f"node-{tag}"},
        }],
    }


# ---------------------------------------------------------------- DoD 场景


def run_d1(client, page, base):
    section("D1 内置模板可视化视图")
    with open(DELIVERY, encoding="utf-8") as f:
        delivery = json.load(f)
    open_template(page, base, "delivery")
    assert_visual_default(page)
    expected_ids = [n.get("id") for n in delivery["nodes"]]
    expected_names = [n.get("name", n.get("id")) for n in delivery["nodes"]]
    assert_nodes(page, expected_ids, expected_names)
    check(len(expected_ids) > 0 and node_ids(page) == expected_ids,
          f"内置 delivery 全部 {len(expected_ids)} 个节点已展示")
    check(page.locator("#tplDetail .tpl-detail").count() >= 1,
          "内置模板节点详情以只读摘要展示")


def run_d2(client, page, base, workdir):
    section("D2 自定义 JSON 模板可视化")
    tag = uuid.uuid4().hex[:8]
    name = f"e2e_d2_{tag}"
    data = build_plain_template(tag, count=4)
    path = write_template(workdir, name, data)
    check(os.path.isfile(path), f"程序生成模板落盘 {os.path.basename(path)}")
    open_template(page, base, name)
    assert_visual_default(page)
    assert_nodes(page, [n["id"] for n in data["nodes"]],
                 [n["name"] for n in data["nodes"]])
    check(node_ids(page) != [], "页面节点来自 fixture 而非硬编码（随机名全部命中）")


def run_d9(client, page, base, workdir):
    """D9：真实页面源码视图查看 + textarea 编辑 + 页面保存（JSON/YAML）。"""
    section("D9 源码视图查看与保存（JSON / YAML）")
    tag = uuid.uuid4().hex[:8]

    # ---- YAML：非 JSON 内容默认回落源码视图 ----
    yaml_name = f"e2e_d9_yaml_{tag}"
    yaml_text = (
        f"id: tpl_yaml_{tag}\n"
        f"name: YAML 模板 {tag}\n"
        f"description: 原文描述 {tag}\n"
        "session_preset: independent\n"
        "git_delivery:\n"
        "  enabled: false\n"
        f"  branch: gdd/{tag}\n"
        "nodes:\n"
        f"- id: n_{tag}\n"
        f"  name: YAML 节点 {tag}\n"
        f"  description: 节点原文描述 {tag}\n"
        "  subject:\n"
        f"    goal: YAML 目标 {tag}\n"
    )
    tdir = os.path.join(workdir, ".gdd-templates")
    os.makedirs(tdir, exist_ok=True)
    yaml_path = os.path.join(tdir, yaml_name + ".yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(yaml_text)

    open_template(page, base, yaml_name)
    page.wait_for_selector('pre[data-view="template-source"]')
    check(page.locator(".alert.warn").filter(has_text="YAML").count() >= 1,
          "YAML 模板显示「仅支持源码查看/编辑」提示")
    check(page.locator(NODE_CARD).count() == 0,
          "YAML 模板不渲染可视化节点卡片")
    check(source_text(page) == yaml_text,
          "YAML 源码视图展示的原文与文件内容一致")

    enter_source_edit(page)
    check(page.locator("#tplEdit").input_value() == yaml_text,
          "YAML 源码编辑 textarea 预填原文")
    save_source(page)
    got = client.get("/api/template/" + urllib.parse.quote(yaml_name))
    check(got.get("ok") is True and got.get("builtin") is False,
          "YAML 源码保存后 GET 成功且 builtin=false")
    check(got.get("content") == yaml_text,
          "YAML 未修改直接保存后 content 与原文逐字符一致")
    check(source_text(page) == yaml_text,
          "YAML 保存后源码视图仍展示原文")

    edited_yaml = yaml_text.replace(f"name: YAML 模板 {tag}",
                                    f"name: YAML 改名 {tag}")
    enter_source_edit(page)
    page.fill("#tplEdit", edited_yaml)
    save_source(page)
    got = client.get("/api/template/" + urllib.parse.quote(yaml_name))
    check(got.get("content") == edited_yaml,
          "YAML 经 textarea 修改并页面保存后 GET 原文逐字符一致")
    check(f"name: YAML 改名 {tag}" in got.get("content", ""),
          "YAML 修改值已落盘")
    check(source_text(page) == edited_yaml,
          "YAML 保存后源码视图展示修改后的原文")

    # ---- JSON：默认可视化，切源码视图查看/编辑/保存 ----
    json_name = f"e2e_d9_json_{tag}"
    data = build_plain_template(tag, count=2)
    json_path = write_template(workdir, json_name, data)
    with open(json_path, encoding="utf-8") as f:
        json_text = f.read()

    open_template(page, base, json_name)
    assert_visual_default(page)  # JSON 默认可视化
    open_source_view(page)
    check(source_text(page) == json_text,
          "JSON 源码视图展示的原文与文件内容一致")
    enter_source_edit(page)
    check(page.locator("#tplEdit").input_value() == json_text,
          "JSON 源码编辑 textarea 预填原文")
    save_source(page)
    got = client.get("/api/template/" + urllib.parse.quote(json_name))
    check(got.get("ok") is True and got.get("builtin") is False,
          "JSON 源码保存后 GET 成功且 builtin=false")
    check(got.get("content") == json_text,
          "JSON 未修改直接保存后 content 与原文逐字符一致")

    edited_data = json.loads(json_text)
    edited_data["description"] = f"JSON 源码修改 {tag}"
    edited_data["nodes"][0]["name"] = f"源码改名节点 {tag}"
    edited_text = json.dumps(edited_data, ensure_ascii=False, indent=2)
    enter_source_edit(page)
    page.fill("#tplEdit", edited_text)
    save_source(page)
    got = client.get("/api/template/" + urllib.parse.quote(json_name))
    check(got.get("content") == edited_text,
          "JSON 经 textarea 修改并页面保存后 GET 原文逐字符一致")
    saved = json.loads(got["content"])
    check(saved["description"] == f"JSON 源码修改 {tag}"
          and saved["nodes"][0]["name"] == f"源码改名节点 {tag}",
          "JSON 源码修改值已落盘且结构可解析")
    open_source_view(page)
    check(source_text(page) == edited_text,
          "JSON 保存后再次查看源码与落盘原文一致")


def run_d5(client, page, base):
    section("D5 fork 后可视化表单编辑与保存")
    tag = uuid.uuid4().hex[:8]
    fork_name = f"e2e_d5_{tag}"

    open_template(page, base, "delivery")
    fork_template(page, fork_name)

    select_node(page, 1)  # survey：entry.artifacts 已有一行
    new_name = f"改名节点-{tag}"
    new_desc = f"改名描述-{tag}"
    new_goal = f"改名目标-{tag}"
    new_path = f"artifacts/{tag}/requirement.md"
    fill_field(page, "node.1.name", new_name)
    fill_field(page, "node.1.description", new_desc)
    fill_field(page, "node.1.subject.goal", new_goal)
    old_checked = is_checked(page, "node.1.approval.required")
    target_checked = not old_checked
    set_checkbox(page, "node.1.approval.required", target_checked)
    fill_field(page, "node.1.entry.artifacts.0.path", new_path)
    save_visual(page)

    saved = get_template(client, fork_name)
    node = saved["nodes"][1]
    check(node["name"] == new_name, "GET 读回节点 name 为修改值")
    check(node["description"] == new_desc, "GET 读回节点 description 为修改值")
    check(node["subject"]["goal"] == new_goal, "GET 读回 subject.goal 为修改值")
    check(bool(node.get("approval", {}).get("required")) == target_checked,
          "GET 读回 approval.required 为修改值")
    check(node["entry"]["artifacts"][0]["path"] == new_path,
          "GET 读回 entry.artifacts[0].path 为修改值")

    open_template(page, base, fork_name)
    select_node(page, 1)
    check(input_value(page, "node.1.name") == new_name, "重新打开后表单 name 保持")
    check(input_value(page, "node.1.description") == new_desc, "重新打开后表单 description 保持")
    check(input_value(page, "node.1.subject.goal") == new_goal, "重新打开后表单 subject.goal 保持")
    check(is_checked(page, "node.1.approval.required") == target_checked,
          "重新打开后表单 approval.required 保持")
    check(input_value(page, "node.1.entry.artifacts.0.path") == new_path,
          "重新打开后表单 entry.artifacts.path 保持")
    check(input_value(page, "node.1.name") == node["name"]
          and input_value(page, "node.1.subject.goal") == node["subject"]["goal"]
          and input_value(page, "node.1.entry.artifacts.0.path")
          == node["entry"]["artifacts"][0]["path"],
          "表单值与 GET 模板内容一致")


def run_d7(client, page, base, workdir):
    section("D7 新建模板并用于建任务")
    tag = uuid.uuid4().hex[:8]
    name = f"e2e_d7_{tag}"
    display = f"展示名-{tag}"

    open_home(page, base)
    create_template_ui(page, name, display)
    assert_visual_default(page)
    check(node_ids(page) == ["node1"], "新建模板打开后含默认节点 node1")
    check(node_names(page) == ["节点1"], "默认节点名称为 节点1")
    nav_btn = page.locator("#navTpl button.item").filter(has_text=name)
    nav_btn.first.wait_for(state="visible")
    check(nav_btn.count() >= 1, "新建后模板列表出现该名字")

    open_template(page, base, name)
    check(node_ids(page) == ["node1"], "刷新后重新打开仍有至少一个节点")
    saved = get_template(client, name)
    check(saved["id"] == name, "GET 读回模板 id 为新建名")
    check(saved["name"] == display, "GET 读回模板展示名为输入值")
    check(len(saved["nodes"]) == 1 and saved["nodes"][0]["id"] == "node1",
          "新建模板内容含一个合法默认节点")

    task = f"e2e_task_d7_{tag}"
    pipe = create_task(client, workdir, task, name)
    check(pipe["id"] == name and pipe["name"] == display,
          "任务实例 pipeline id/name 来自新模板")
    check(pipe["nodes"][0]["id"] == "node1", "任务实例包含新模板节点")


def run_d11(client, page, base, workdir):
    section("D11 全链路组合验证")
    tag = uuid.uuid4().hex[:8]

    with open(DELIVERY, encoding="utf-8") as f:
        delivery_ids = [n["id"] for n in json.load(f)["nodes"]]
    open_template(page, base, "delivery")
    assert_visual_default(page)
    check(node_ids(page) == delivery_ids,
          "组合链路第 1 步：内置模板可视化节点顺序正确")

    fork_name = f"e2e_d11_fork_{tag}"
    fork_template(page, fork_name)
    select_node(page, 0)
    renamed = f"组合改名-{tag}"
    fill_field(page, "node.0.name", renamed)
    save_visual(page)
    saved_fork = get_template(client, fork_name)
    check(saved_fork["nodes"][0]["name"] == renamed,
          "组合链路第 2-3 步：fork 后编辑保存生效")

    new_name = f"e2e_d11_new_{tag}"
    display = f"组合新模板-{tag}"
    create_template_ui(page, new_name, display)
    check(node_ids(page) == ["node1"], "组合链路第 4 步：新建模板含默认节点")

    task = f"e2e_task_d11_{tag}"
    pipe = create_task(client, workdir, task, new_name)
    check(pipe["id"] == new_name and pipe["name"] == display,
          "组合链路第 5 步：用新模板初始化任务成功且实例值正确")


def run_d12(client, page, base, workdir):
    section("D12 未展示字段经可视化表单保存后保留")
    tag = uuid.uuid4().hex[:8]
    name = f"e2e_d12_{tag}"
    data = build_preserve_template(tag)
    top_key = f"x-e2e-top-{tag}"
    node_key = f"x-e2e-node-{tag}"
    write_template(workdir, name, data)

    open_template(page, base, name)
    assert_visual_default(page)
    new_desc = f"经可视化表单修改-{tag}"
    new_node_desc = f"节点描述经表单修改-{tag}"
    fill_field(page, "pipeline.description", new_desc)
    fill_field(page, "node.0.description", new_node_desc)
    save_visual(page)

    saved = get_template(client, name)
    check(saved["description"] == new_desc, "普通字段 pipeline.description 已保存")
    check(saved["nodes"][0]["description"] == new_node_desc,
          "普通字段 node.description 已保存")
    check(saved["nodes"][0]["id"] == data["nodes"][0]["id"], "节点 id 未被表单改写")
    assert_json_equal(saved["nodes"][0]["mcp"], data["nodes"][0]["mcp"])
    check(True, "mcp 经可视化保存后逐字段保留")
    assert_json_equal(saved["nodes"][0]["skills"], data["nodes"][0]["skills"])
    check(True, "skills 经可视化保存后逐字段保留")
    assert_json_equal(saved[top_key], data[top_key])
    check(True, "未知顶层键经可视化保存后原样保留")
    assert_json_equal(saved["nodes"][0][node_key], data["nodes"][0][node_key])
    check(True, "未知节点键经可视化保存后原样保留")

    task = f"e2e_task_d12_{tag}"
    pipe = create_task(client, workdir, task, name)
    check(pipe["id"] == data["id"]
          and pipe["nodes"][0]["id"] == data["nodes"][0]["id"],
          "含未展示字段的模板仍可初始化任务（实例 id/节点与模板一致）")


def run_review_fixes(client, page, base, workdir):
    section("评审回归：缺失 name 预填 id / session_group 预设语义 / git 默认值 / 新建重名")
    tag = uuid.uuid4().hex[:8]

    # 1) 节点缺失 name：表单预填 id，仅打开并保存也不会把缺失写成空串
    name = f"e2e_review_{tag}"
    data = build_plain_template(tag, count=2)
    node_id = data["nodes"][0]["id"]
    del data["nodes"][0]["name"]
    write_template(workdir, name, data)
    open_template(page, base, name)
    prefilled = input_value(page, "node.0.name")
    check(prefilled == node_id, f"缺失 name 的节点表单预填 id（实际 {prefilled!r}）")
    save_visual(page)
    saved = get_template(client, name)
    check(saved["nodes"][0].get("name") == node_id,
          "仅打开并保存后节点 name 为 id（不再变成空串）")

    # 2) 内置 delivery（session_preset=clarify-design）空 session_group 的展示语义
    with open(DELIVERY, encoding="utf-8") as f:
        delivery = json.load(f)
    check(delivery.get("session_preset") == "clarify-design",
          "内置模板 session_preset 为 clarify-design")
    clarify_idx = next(i for i, n in enumerate(delivery["nodes"])
                       if n.get("clarify") and not n.get("session_group"))
    plain_idx = next(i for i, n in enumerate(delivery["nodes"])
                     if not n.get("clarify") and not n.get("session_group"))
    open_template(page, base, "delivery")
    select_node(page, clarify_idx)
    session_kv = page.locator("#tplDetail .tpl-kv").filter(
        has_text="session_group").inner_text()
    check("独立会话" not in session_kv and "clarify-design" in session_kv,
          f"clarify 节点空组按预设展开语义展示（{session_kv!r}）")
    select_node(page, plain_idx)
    session_kv = page.locator("#tplDetail .tpl-kv").filter(
        has_text="session_group").inner_text()
    check("独立会话" in session_kv, f"非 clarify 节点空组展示为独立会话（{session_kv!r}）")

    # 3) git_delivery.enabled 缺失时摘要按模型默认（启用）展示
    summary = page.evaluate(
        "() => tplPipelineSummary({id:'x', git_delivery:{branch:'gdd/delivery'}})")
    check("git 交付：启用" in summary,
          "git_delivery 缺 enabled 时摘要显示启用（与模型默认一致）")
    summary = page.evaluate(
        "() => tplPipelineSummary({id:'x', git_delivery:{enabled:false}})")
    check("git 交付：停用" in summary, "git_delivery.enabled=false 时摘要显示停用")

    # 4) 新建重名 / 大小写变体：页面显示已存在错误，原模板不被覆盖
    open_home(page, base)
    page.locator('[data-action="new-template"]').click()
    page.wait_for_selector('[data-view="template-new"]')
    page.fill("#newTplName", name)
    page.locator('[data-action="create-template"]').click()
    page.wait_for_function(
        "() => { const e = document.querySelector('#newTplErr');"
        " return !!e && e.textContent.indexOf('已存在') >= 0; }")
    check(page.locator("#newTplErr .alert.err").count() == 1,
          "新建重名在页面显示错误")
    saved = get_template(client, name)
    check(saved["nodes"][0].get("name") == node_id, "新建重名失败后原模板未被覆盖")

    if fs_case_insensitive(workdir):
        variant = name.upper()
        page.fill("#newTplName", variant)
        page.locator('[data-action="create-template"]').click()
        page.wait_for_function(
            "v => { const e = document.querySelector('#newTplErr');"
            " return !!e && e.textContent.indexOf('已存在') >= 0"
            " && e.textContent.indexOf(v) >= 0; }",
            arg=variant)
        check(page.locator("#newTplErr .alert.err").count() == 1,
              "大小写变体新建在页面显示已存在错误")
        saved = get_template(client, name)
        check(saved["nodes"][0].get("name") == node_id,
              "大小写变体新建失败后原模板未被覆盖")
    else:
        print("  skip: 文件系统大小写敏感，大小写变体是合法独立模板名")


# ---------------------------------------------------------------- 入口


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"ERROR: 无法导入 Playwright：{e}", file=sys.stderr)
        print("e2e 未验证，不得记为通过。请用可导入 playwright 的解释器运行，例如：",
              file=sys.stderr)
        print("  PYTHONPATH=/path/to/playwright/site-packages python3.12 "
              "tests/test_template_ui_e2e.py", file=sys.stderr)
        return 2

    workdir = tempfile.mkdtemp(prefix="gdd-tpl-e2e-")
    configure_stub_env(workdir)
    console = Console(workdir)
    client = Client(console.base)
    print(f"[setup] 控制台 {console.base}（pid {console.proc.pid}），工作台 {workdir}")
    print(f"[setup] stub agent：GDD_AGENT=pi，"
          f"GDD_AGENT_BIN={os.environ.get('GDD_AGENT_BIN')}")

    browser = None
    page = None
    page_errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            print(f"[setup] 浏览器：chromium channel=chrome headless，"
                  f"version {browser.version}")
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(20000)
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            run_d1(client, page, console.base)
            run_d2(client, page, console.base, workdir)
            run_d9(client, page, console.base, workdir)
            run_d5(client, page, console.base)
            run_d7(client, page, console.base, workdir)
            run_d11(client, page, console.base, workdir)
            run_d12(client, page, console.base, workdir)
            run_review_fixes(client, page, console.base, workdir)

            check(not page_errors, f"全程无页面未捕获 JS 异常（实际 {page_errors}）")
            print(f"\n[e2e] 工作台（含临时模板与任务）：{workdir}")
            print("\n全部通过 ✔")
            return 0
    except Exception:
        print("\n[e2e] 失败", file=sys.stderr)
        traceback.print_exc()
        if page is not None:
            try:
                shot = os.path.join(workdir, "failure.png")
                page.screenshot(path=shot, full_page=True)
                print(f"[e2e] 失败截图：{shot}", file=sys.stderr)
            except Exception:
                pass
        print(f"[e2e] 控制台日志尾部：\n{console.log_tail()}", file=sys.stderr)
        return 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        console.stop()


if __name__ == "__main__":
    sys.exit(main())
