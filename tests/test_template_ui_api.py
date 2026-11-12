"""模板可视化改造的 HTTP 集成测试（无浏览器，直连 API + 临时工作台）。

覆盖 DoD：
  D3  结构化保存与校验：覆盖全部可编辑字段的 JSON 模板 PUT 后 GET，键/值/类型
      逐字段相等；空 nodes、重复节点 id、非法 must_contain 正则、非法
      session_preset、非法 session_group 均被拒绝且模板文件字节不变。
  D6  编辑后的模板可用于建任务：POST /api/tasks 初始化成功，
      .gdd/pipeline.json 含修改后的字段。
  D8  内置模板只读与 fork 不变：直接 PUT/DELETE delivery 被拒；fork 副本可
      保存编辑、可读回并出现在模板列表。
  D9  源码模式与 YAML 兼容：YAML / JSON 原文 PUT 后 GET 逐字节一致，
      content 接口语义不变。
  D13 实例隔离：用模板 init 任务后 PUT 修改模板文件，任务 .gdd/pipeline.json
      字节与 state.json 的 pipeline_fingerprint 均不变，任务仍可正常加载。

运行：python3.12 tests/test_template_ui_api.py
"""
from __future__ import annotations

import copy
import glob
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PYTHONPATH", ROOT)
os.environ["GDD_AGENT"] = "pi"

STUB = os.path.join(ROOT, "tests", "pi_stub.py")

# spec 5.2 的可编辑字段清单（节点 id / mcp / skills 不可结构化编辑，故不在内）。
NODE_EDITABLE_FIELDS = (
    "name",
    "description",
    "subject.goal",
    "subject.instructions",
    "subject.max_turns",
    "approval.required",
    "approval.prompt",
    "clarify",
    "max_bounces",
    "baseline_marker",
    "adversarial",
    "review",
    "session_group",
    "entry.goal",
    "entry.ai_check",
    "entry.change_aware",
    "entry.artifacts.0.path",
    "entry.artifacts.0.must_contain",
    "entry.commands.0",
    "exit.goal",
    "exit.ai_check",
    "exit.change_aware",
    "exit.artifacts.0.path",
    "exit.artifacts.0.must_contain",
    "exit.commands.0",
)
PIPELINE_EDITABLE_FIELDS = (
    "name",
    "description",
    "session_preset",
    "git_delivery.enabled",
    "git_delivery.branch",
    "git_delivery.remote",
)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def section(title):
    print(f"\n[{title}]")


def get_path(obj, path):
    cur = obj
    for part in path.split("."):
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    return cur


def has_path(obj, path):
    try:
        get_path(obj, path)
        return True
    except (KeyError, IndexError, TypeError):
        return False


def assert_json_equal(actual, expected, path="$"):
    """递归比较 JSON 结构：键集合、值、类型（bool 与 int 严格区分）均须相等。"""
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


def build_node(tag, idx):
    """按 spec 5.2 生成一个覆盖全部可编辑字段的节点（程序生成，值随 tag 变化）。"""
    suffix = f"{tag}{idx}"
    return {
        "id": f"n{suffix}",
        "name": f"节点-{suffix}",
        "description": f"节点描述 {suffix}",
        "subject": {
            "goal": f"主体目标 {suffix}",
            "instructions": f"主体指令 {suffix}",
            "max_turns": 30 + idx,
        },
        "approval": {
            "required": idx % 2 == 1,
            "prompt": f"审批提示 {suffix}",
        },
        "clarify": idx % 2 == 1,
        "max_bounces": 3 + idx,
        "baseline_marker": True,
        "adversarial": True,
        "review": idx % 2 == 0,
        "session_group": f"grp{suffix}",
        "entry": {
            "goal": f"入口判据 {suffix}",
            "ai_check": f"入口 AI {suffix}",
            "change_aware": idx % 2 == 1,
            "artifacts": [
                {"path": f"in_{suffix}.md", "must_contain": "^入口"},
                {"path": f"in2_{suffix}.log", "must_contain": "done"},
            ],
            "commands": [f"test -f in_{suffix}.md", f"echo {suffix}"],
        },
        "exit": {
            "goal": f"出口判据 {suffix}",
            "ai_check": f"出口 AI {suffix}",
            "change_aware": idx % 2 == 0,
            "artifacts": [
                {"path": f"out_{suffix}.md", "must_contain": "结论"},
            ],
            "commands": [f"test -f out_{suffix}.md"],
        },
    }


def build_template(tag, node_count=1):
    return {
        "id": f"tpl-{tag}",
        "name": f"模板 {tag}",
        "description": f"模板描述 {tag}",
        "session_preset": "independent",
        "git_delivery": {
            "enabled": False,
            "branch": f"gdd/tpl-{tag}",
            "remote": "origin",
        },
        "nodes": [build_node(tag, i + 1) for i in range(node_count)],
    }


def template_files(workdir, name):
    td = os.path.join(workdir, ".gdd-templates")
    return sorted(
        p for p in glob.glob(os.path.join(td, name + ".*"))
        if p.endswith((".json", ".yaml", ".yml"))
    )


def single_template_file(workdir, name):
    files = template_files(workdir, name)
    if len(files) != 1:
        raise AssertionError(f"模板 {name} 应恰有一个文件，实际 {files}")
    return files[0]


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Client:
    def __init__(self, base):
        self.base = base

    def req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "X-GDD-Request": "template-ui-api-test"})
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


def configure_stub_env(workdir):
    """按 tests/test_ui.py 的约定配置 stub agent。

    本测试只做 init/load，不调用 run，因此不会真正拉起 agent；配置 stub 是为了
    与 D6/D13 的「临时工作台 + stub agent」环境保持一致，不依赖真实模型端点。
    """
    script = os.path.join(workdir, "stub.json")
    with open(script, "w", encoding="utf-8") as f:
        json.dump([{"patterns": ["GDD-ROLE"], "text": "stub 主体工作完成"}],
                  f, ensure_ascii=False)
    os.environ["GDD_AGENT_BIN"] = sys.executable
    os.environ["GDD_AGENT_PRE_ARGS"] = STUB
    os.environ["GDD_PI_STUB_SCRIPT"] = script
    pi_home = tempfile.mkdtemp(prefix="gdd-tpl-api-pi-")
    settings = os.path.join(pi_home, "settings.json")
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "baseUrl": "https://api.example.com/v1",
                   "apiKey": "k", "model": "m"}, f)
    os.environ["GDD_PI_SETTINGS"] = settings
    os.environ["GDD_PI_AGENT_DIR"] = os.path.join(pi_home, "agent")
    os.environ["GDD_PI_SESSION_DIR"] = os.path.join(pi_home, "sessions")
    os.environ.pop("GDD_STEER", None)


# ---------------------------------------------------------------- D3

def run_d3(c, workdir):
    section("D3 结构化保存与校验")
    tag = uuid.uuid4().hex[:8]
    name = f"tpl_api_d3_{tag}"
    data = build_template(tag, node_count=2)

    missing = [p for p in PIPELINE_EDITABLE_FIELDS if not has_path(data, p)]
    check(not missing, f"测试模板覆盖流水线级可编辑字段（缺失 {missing}）")
    for idx in range(2):
        prefix = f"nodes.{idx}."
        missing = [p for p in NODE_EDITABLE_FIELDS if not has_path(data, prefix + p)]
        check(not missing, f"测试模板 node[{idx}] 覆盖节点级可编辑字段（缺失 {missing}）")

    text = json.dumps(data, ensure_ascii=False, indent=2)
    r = c.put(f"/api/template/{name}", {"content": text})
    check(r.get("ok") is True, f"覆盖全部可编辑字段的 JSON 模板保存成功（{r}）")

    got = c.get(f"/api/template/{name}")
    check(got.get("ok") is True and got.get("builtin") is False,
          "自定义模板 GET 成功且 builtin=false")
    check(isinstance(got.get("content"), str), "GET 返回 content 原文文本")
    assert_json_equal(json.loads(got["content"]), data)
    check(True, "GET 读回的 JSON 与提交 JSON 键/值/类型逐字段相等")

    tpl_file = single_template_file(workdir, name)
    before = read_bytes(tpl_file)

    invalid = []
    bad = copy.deepcopy(data)
    bad["nodes"] = []
    invalid.append(("空 nodes", bad))
    bad = copy.deepcopy(data)
    bad["nodes"][1]["id"] = bad["nodes"][0]["id"]
    invalid.append(("重复节点 id", bad))
    bad = copy.deepcopy(data)
    bad["nodes"][0]["exit"]["artifacts"][0]["must_contain"] = "([a-z"
    invalid.append(("非法 must_contain 正则", bad))
    bad = copy.deepcopy(data)
    bad["session_preset"] = "weird-preset"
    invalid.append(("非法 session_preset", bad))
    bad = copy.deepcopy(data)
    bad["nodes"][0]["session_group"] = "bad group"
    invalid.append(("非法 session_group", bad))

    for label, payload in invalid:
        r = c.put(f"/api/template/{name}",
                  {"content": json.dumps(payload, ensure_ascii=False)})
        check(r.get("ok") is False and bool(r.get("error")),
              f"{label} 被拒绝（{r.get('error')}）")
        check(read_bytes(tpl_file) == before, f"{label} 拒绝后模板文件字节不变")

    got = c.get(f"/api/template/{name}")
    check(got.get("ok") is True and got.get("content") == text,
          "全部非法提交被拒后，模板原文仍为首次保存内容")


# ---------------------------------------------------------------- D6

def run_d6(c, workdir):
    section("D6 编辑后的模板可用于建任务")
    tag = uuid.uuid4().hex[:8]
    tpl = f"tpl_api_d6_{tag}"
    task = f"task_api_d6_{tag}"
    data = build_template(tag, node_count=1)
    data["name"] = f"编辑后模板 {tag}"
    data["description"] = f"编辑后描述 {tag}"
    node = data["nodes"][0]
    node["name"] = f"编辑后节点 {tag}"
    node["description"] = f"编辑后节点描述 {tag}"
    node["subject"]["goal"] = f"编辑后目标 {tag}"
    node["subject"]["instructions"] = f"编辑后指令 {tag}"
    node["subject"]["max_turns"] = 42
    node["approval"]["required"] = True
    node["approval"]["prompt"] = f"编辑后审批 {tag}"
    node["clarify"] = True
    node["max_bounces"] = 5
    node["baseline_marker"] = True
    node["adversarial"] = True
    node["review"] = False
    node["session_group"] = f"grp_{tag}"
    node["entry"]["goal"] = f"编辑后入口 {tag}"
    node["entry"]["ai_check"] = f"编辑后入口AI {tag}"
    node["entry"]["change_aware"] = True
    node["entry"]["artifacts"][0]["path"] = f"edited_in_{tag}.md"
    node["entry"]["commands"] = [f"test -f edited_in_{tag}.md"]
    node["exit"]["goal"] = f"编辑后出口 {tag}"
    node["exit"]["ai_check"] = f"编辑后出口AI {tag}"
    node["exit"]["change_aware"] = True
    node["exit"]["artifacts"][0]["path"] = f"edited_out_{tag}.md"
    node["exit"]["commands"] = [f"test -f edited_out_{tag}.md"]

    r = c.put(f"/api/template/{tpl}",
              {"content": json.dumps(data, ensure_ascii=False, indent=2)})
    check(r.get("ok") is True, "含修改字段的模板保存成功")

    r = c.post("/api/tasks", {"name": task, "template": tpl,
                              "requirement": f"需求 {tag}"})
    check(r.get("ok") is True, f"用编辑后的模板创建任务成功（{r}）")

    task_dir = os.path.join(workdir, task)
    pipe_path = os.path.join(task_dir, ".gdd", "pipeline.json")
    state_path = os.path.join(task_dir, ".gdd", "state.json")
    check(os.path.isfile(pipe_path), "运行实例 .gdd/pipeline.json 已生成")
    inst = read_json(pipe_path)
    node_inst = inst["nodes"][0]

    checks = [
        ("pipeline.name", inst.get("name"), data["name"]),
        ("pipeline.description", inst.get("description"), data["description"]),
        ("pipeline.git_delivery.enabled",
         get_path(inst, "git_delivery.enabled"), False),
        ("pipeline.git_delivery.branch",
         get_path(inst, "git_delivery.branch"), data["git_delivery"]["branch"]),
        ("pipeline.git_delivery.remote",
         get_path(inst, "git_delivery.remote"), data["git_delivery"]["remote"]),
        ("node.id", node_inst.get("id"), node["id"]),
        ("node.name", node_inst.get("name"), node["name"]),
        ("node.description", node_inst.get("description"), node["description"]),
        ("node.subject.goal", get_path(node_inst, "subject.goal"),
         node["subject"]["goal"]),
        ("node.subject.instructions", get_path(node_inst, "subject.instructions"),
         node["subject"]["instructions"]),
        ("node.subject.max_turns", get_path(node_inst, "subject.max_turns"), 42),
        ("node.approval.required", get_path(node_inst, "approval.required"), True),
        ("node.approval.prompt", get_path(node_inst, "approval.prompt"),
         node["approval"]["prompt"]),
        ("node.clarify", node_inst.get("clarify"), True),
        ("node.max_bounces", node_inst.get("max_bounces"), 5),
        ("node.baseline_marker", node_inst.get("baseline_marker"), True),
        ("node.adversarial", node_inst.get("adversarial"), True),
        ("node.session_group", node_inst.get("session_group"), node["session_group"]),
        ("node.entry.goal", get_path(node_inst, "entry.goal"), node["entry"]["goal"]),
        ("node.entry.ai_check", get_path(node_inst, "entry.ai_check"),
         node["entry"]["ai_check"]),
        ("node.entry.change_aware", get_path(node_inst, "entry.change_aware"), True),
        ("node.entry.artifacts.0.path",
         get_path(node_inst, "entry.artifacts.0.path"),
         node["entry"]["artifacts"][0]["path"]),
        ("node.entry.commands.0", get_path(node_inst, "entry.commands.0"),
         node["entry"]["commands"][0]),
        ("node.exit.goal", get_path(node_inst, "exit.goal"), node["exit"]["goal"]),
        ("node.exit.ai_check", get_path(node_inst, "exit.ai_check"),
         node["exit"]["ai_check"]),
        ("node.exit.change_aware", get_path(node_inst, "exit.change_aware"), True),
        ("node.exit.artifacts.0.path",
         get_path(node_inst, "exit.artifacts.0.path"),
         node["exit"]["artifacts"][0]["path"]),
        ("node.exit.commands.0", get_path(node_inst, "exit.commands.0"),
         node["exit"]["commands"][0]),
    ]
    for label, actual, expected in checks:
        check(actual == expected,
              f"实例字段 {label} = {expected!r}（实际 {actual!r}）")

    state = read_json(state_path)
    check(state.get("status") == "running" and state.get("current") == node["id"],
          "任务状态正常初始化（running + 首节点 pending）")
    check(state.get("pipeline_id") == data["id"], "实例记录模板 id")

    detail = c.get(f"/api/task/{task}")
    check(detail.get("ok") is True and detail.get("pipeline_name") == data["name"],
          "GET /api/task 可加载编辑后模板实例")
    check(detail["nodes"][0]["id"] == node["id"]
          and detail["nodes"][0]["needs_approval"] is True,
          "任务详情包含修改后的节点与审批标记")


# ---------------------------------------------------------------- D8

def run_d8(c, workdir):
    section("D8 内置模板只读与 fork 不变")
    tag = uuid.uuid4().hex[:8]
    fork = f"tpl_api_d8_{tag}"

    d = c.get("/api/template/delivery")
    check(d.get("ok") is True and d.get("builtin") is True, "内置 delivery 可读")

    r = c.put("/api/template/delivery", {"content": d["content"]})
    check(r.get("ok") is False and "只读" in r.get("error", ""),
          f"直接 PUT delivery 被拒绝（{r.get('error')}）")
    r = c.delete("/api/template/delivery")
    check(r.get("ok") is False and "不可删除" in r.get("error", ""),
          f"DELETE delivery 被拒绝（{r.get('error')}）")
    check(not template_files(workdir, "delivery"), "内置模板未被写成影子文件")

    r = c.put("/api/template/delivery", {"content": d["content"], "fork": fork})
    check(r.get("ok") is True, f"fork 内置模板成功（{r}）")

    got = c.get(f"/api/template/{fork}")
    check(got.get("ok") is True and got.get("builtin") is False,
          "fork 副本可读且 builtin=false")
    check(got.get("content") == d["content"], "fork 副本内容与内置模板逐字节一致")

    overview = c.get("/api/overview")
    entry = [t for t in overview.get("templates", []) if t.get("name") == fork]
    check(len(entry) == 1 and entry[0].get("builtin") is False,
          "fork 副本出现在模板列表且标记为自定义")

    data = json.loads(got["content"])
    data["name"] = f"fork 改名 {tag}"
    data["nodes"][0]["name"] = f"fork 节点改名 {tag}"
    r = c.put(f"/api/template/{fork}",
              {"content": json.dumps(data, ensure_ascii=False, indent=2)})
    check(r.get("ok") is True, "fork 副本可保存编辑")

    got = c.get(f"/api/template/{fork}")
    check(got.get("ok") is True, "fork 副本编辑后可读回")
    saved = json.loads(got["content"])
    check(saved.get("name") == data["name"]
          and saved["nodes"][0].get("name") == data["nodes"][0]["name"],
          "fork 副本保存的修改值生效")


# ---------------------------------------------------------------- 评审回归：新建 create-only

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


def run_create_guard(c, workdir):
    """spec §6：新建必须拒绝已存在名（任意扩展名/大小写变体）且不创建/覆盖文件。

    页面「新建模板」走 PUT + fork 参数（create-only 语义）；本测试直接覆盖该路径：
    同名、大小写变体（大小写不敏感文件系统）与并发同名均恰一个成功，败者不落盘。
    """
    section("评审回归：新建模板 create-only（重名/大小写变体/并发不覆盖）")
    tag = uuid.uuid4().hex[:8]
    base = build_template(tag)

    name = f"tpl_api_new_{tag}"
    payload = json.dumps(base, ensure_ascii=False, indent=2)
    r = c.put(f"/api/template/{name}", {"content": payload, "fork": name})
    check(r.get("ok") is True, f"fork 参数（create-only）可新建模板（{r.get('error')}）")
    path = single_template_file(workdir, name)
    before = read_bytes(path)
    check(before.decode("utf-8") == payload, "新建模板文件内容与提交内容逐字节一致")

    changed = copy.deepcopy(base)
    changed["description"] = f"second-{tag}"
    r = c.put(f"/api/template/{name}",
              {"content": json.dumps(changed, ensure_ascii=False), "fork": name})
    check(r.get("ok") is False and "已存在" in r.get("error", ""),
          f"同名再建被拒绝（{r.get('error')}）")
    check(read_bytes(path) == before, "同名再建后原文件字节不变（未被覆盖）")

    if fs_case_insensitive(workdir):
        variant = name.upper()
        r = c.put(f"/api/template/{variant}",
                  {"content": json.dumps(changed, ensure_ascii=False), "fork": variant})
        check(r.get("ok") is False and "已存在" in r.get("error", ""),
              f"大小写变体再建被拒绝（{r.get('error')}）")
        check(read_bytes(path) == before, "大小写变体未覆盖原文件")
    else:
        print("  skip: 文件系统大小写敏感，大小写变体是合法独立模板名")

    race_name = f"tpl_api_race_{tag}"
    barrier = threading.Barrier(8)

    def create_once(i):
        body = copy.deepcopy(base)
        body["description"] = f"race-{i}-{tag}"
        content = json.dumps(body, ensure_ascii=False, indent=2)
        barrier.wait()
        return content, c.put(f"/api/template/{race_name}",
                              {"content": content, "fork": race_name})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result() for f in [pool.submit(create_once, i) for i in range(8)]]
    winners = [(content, r) for content, r in results if r.get("ok")]
    losers = [r for _, r in results if not r.get("ok")]
    check(len(winners) == 1, f"8 路并发新建同名恰一个成功（实际 {len(winners)}）")
    check(all("已存在" in r.get("error", "") for r in losers),
          "并发败者均返回“已存在”")
    race_path = single_template_file(workdir, race_name)
    check(read_bytes(race_path) == winners[0][0].encode("utf-8"),
          "并发创建后文件内容等于胜者内容（无交错写入）")


# ---------------------------------------------------------------- D9

def run_d9(c, workdir):
    section("D9 源码模式与 YAML 兼容")
    try:
        import yaml
    except ImportError as e:  # D9 判据依赖本机 PyYAML
        raise AssertionError(f"D9 需要 PyYAML（goal.md 验证环境说明本机可用）: {e}")
    print(f"  PyYAML {yaml.__version__}")

    tag = uuid.uuid4().hex[:8]
    data = build_template(tag, node_count=1)

    # YAML：临时工作台直接写入程序生成的原文，GET/PUT 均须逐字节保真。
    name = f"tpl_api_d9_{tag}"
    yaml_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    td = os.path.join(workdir, ".gdd-templates")
    os.makedirs(td, exist_ok=True)
    with open(os.path.join(td, name + ".yaml"), "w", encoding="utf-8") as f:
        f.write(yaml_text)

    got = c.get(f"/api/template/{name}")
    check(got.get("ok") is True and got.get("content") == yaml_text,
          "YAML 原文 GET 逐字节一致")
    check(yaml.safe_load(got["content"]) == data, "YAML 原文解析语义与写入一致")

    data_m = copy.deepcopy(data)
    data_m["nodes"][0]["name"] = f"YAML 改名 {tag}"
    yaml_text2 = yaml.safe_dump(data_m, allow_unicode=True, sort_keys=False)
    r = c.put(f"/api/template/{name}", {"content": yaml_text2})
    check(r.get("ok") is True, "YAML 原文 PUT 保存成功")
    got = c.get(f"/api/template/{name}")
    check(got.get("content") == yaml_text2, "YAML 原文 PUT 后 GET 逐字节一致")
    check(yaml.safe_load(got["content"]) == data_m, "YAML 保存后为修改值")

    # JSON 源码模式：原文保存不得被重新格式化。
    jname = f"tpl_api_d9_json_{tag}"
    pretty = json.dumps(data, ensure_ascii=False, indent=4)
    r = c.put(f"/api/template/{jname}", {"content": pretty})
    check(r.get("ok") is True, "JSON 原文 PUT 保存成功")
    got = c.get(f"/api/template/{jname}")
    check(got.get("content") == pretty,
          "JSON 原文 GET 逐字节一致（content 接口语义不变）")

    compact = json.dumps(data_m, ensure_ascii=False, separators=(",", ":"))
    r = c.put(f"/api/template/{jname}", {"content": compact})
    check(r.get("ok") is True, "JSON 原文再次保存成功")
    got = c.get(f"/api/template/{jname}")
    check(got.get("content") == compact,
          "JSON 原文再次 GET 逐字节一致（服务端未做格式重写）")
    assert_json_equal(json.loads(got["content"]), data_m)
    check(True, "JSON 原文修改后结构逐字段一致")


# ---------------------------------------------------------------- D13

def run_d13(c, workdir, Orchestrator):
    section("D13 实例隔离")
    tag = uuid.uuid4().hex[:8]
    tpl = f"tpl_api_d13_{tag}"
    task = f"task_api_d13_{tag}"
    data = build_template(tag, node_count=1)
    original_name = f"隔离前模板 {tag}"
    original_node_name = f"隔离前节点 {tag}"
    data["name"] = original_name
    data["nodes"][0]["name"] = original_node_name

    r = c.put(f"/api/template/{tpl}",
              {"content": json.dumps(data, ensure_ascii=False, indent=2)})
    check(r.get("ok") is True, "模板保存成功")
    r = c.post("/api/tasks", {"name": task, "template": tpl,
                              "requirement": f"隔离需求 {tag}"})
    check(r.get("ok") is True, "用模板初始化任务成功")

    task_dir = os.path.join(workdir, task)
    pipe_path = os.path.join(task_dir, ".gdd", "pipeline.json")
    state_path = os.path.join(task_dir, ".gdd", "state.json")
    pipe_before = read_bytes(pipe_path)
    fp_before = read_json(state_path).get("pipeline_fingerprint")
    check(isinstance(fp_before, str) and len(fp_before) == 16,
          f"已记录任务 pipeline_fingerprint={fp_before!r}")

    # 修改同一模板文件，确认模板本身确实变了。
    got = c.get(f"/api/template/{tpl}")
    changed = json.loads(got["content"])
    changed["name"] = f"隔离后模板 {tag}"
    changed["description"] = f"模板已修改 {tag}"
    changed["nodes"][0]["name"] = f"隔离后节点 {tag}"
    r = c.put(f"/api/template/{tpl}",
              {"content": json.dumps(changed, ensure_ascii=False, indent=2)})
    check(r.get("ok") is True, "PUT 修改模板成功")
    got = c.get(f"/api/template/{tpl}")
    check(json.loads(got["content"])["name"] == changed["name"],
          "确认模板文件内容已变化")

    # 任务实例不受模板修改影响。
    check(read_bytes(pipe_path) == pipe_before,
          "任务 .gdd/pipeline.json 字节保持不变")
    check(read_json(state_path).get("pipeline_fingerprint") == fp_before,
          "state.json 的 pipeline_fingerprint 保持不变")

    orch = Orchestrator(task_dir)
    check(orch.load() is True, "Orchestrator.load() 正常加载任务实例")
    check(orch.verify_pipeline_integrity() is True, "实例指纹校验通过")
    check(orch.pipeline.name == original_name
          and orch.pipeline.nodes[0].name == original_node_name,
          "实例快照仍是初始化时的模板内容")

    detail = c.get(f"/api/task/{task}")
    check(detail.get("ok") is True and detail.get("status") == "running",
          "GET /api/task 仍可正常加载")
    check(read_bytes(pipe_path) == pipe_before,
          "GET /api/task 后实例字节仍不变")
    check(read_json(state_path).get("pipeline_fingerprint") == fp_before,
          "GET /api/task 后指纹仍不变")


def main():
    from gdd import ui as gdd_ui
    from gdd.orchestrator import Orchestrator

    workdir = tempfile.mkdtemp(prefix="gdd-tpl-api-")
    configure_stub_env(workdir)
    gdd_ui.Handler.root = workdir
    srv = gdd_ui.ThreadingHTTPServer(("127.0.0.1", 0), gdd_ui.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(f"http://127.0.0.1:{port}")
    print(f"[setup] server on {port}, workdir {workdir}")
    try:
        run_d3(c, workdir)
        run_d6(c, workdir)
        run_d8(c, workdir)
        run_create_guard(c, workdir)
        run_d9(c, workdir)
        run_d13(c, workdir, Orchestrator)
    finally:
        srv.shutdown()
    print("\n全部通过 ✔")


if __name__ == "__main__":
    main()
