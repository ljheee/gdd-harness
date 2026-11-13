"""模板可视化静态检查（D4 / D14，无 HTTP 服务、无浏览器）。

D4 可编辑字段覆盖：
  - 直接 `import gdd.ui` 解析内嵌 HTML/JS 常量；用 Node 作为纯 JS 求值器渲染
    `tplPipelineForm` / `tplFormDetail`（页面自身的纯字符串函数，不触 DOM/网络），
    逐项断言 spec.md 5.2 列出的每个可编辑字段都有稳定的 `data-field` 结构化控件；
  - 断言节点 `id` 只读展示（`data-readonly`，无 `data-field` 编辑控件）、
    `mcp`/`skills` 不在结构化字段清单且无结构化控件，仅保留只读摘要 + 源码模式路径。

D14 文档与依赖就绪：
  - README/USER_GUIDE 模板管理章节含可视化/编辑/新建/内置只读/源码/YAML 说明；
  - `gdd/ui.py` 仍为零依赖内嵌单页：唯一内联 `<script>`，无 `<script src>`/`<link>`/CDN；
  - 根 `package.json` 依赖仍为既有 pi 执行端两项，无前端运行时依赖/构建配置。

Node 仅作静态 JS 求值器使用（仓库既有引擎依赖 `package.json:engines`，非新增前端依赖）；
本脚本不启动 HTTP 服务、不访问网络、不写仓库内任何文件。

运行：python3.12 tests/test_template_ui_static.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gdd.ui  # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  FAIL", msg)
        FAILS.append(msg)


# ---------------- spec.md 5.2 可编辑字段清单（节点 id / mcp / skills 不在其中） ----------------

PIPELINE_FIELDS = [
    "name",
    "description",
    "session_preset",
    "git_delivery.enabled",
    "git_delivery.branch",
    "git_delivery.remote",
]

NODE_FIELDS = [
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
    "entry.artifacts",
    "entry.commands",
    "exit.goal",
    "exit.ai_check",
    "exit.change_aware",
    "exit.artifacts",
    "exit.commands",
]

# spec.md 5.2 规定的控件形态
PIPELINE_CONTROLS = {
    "pipeline.name": "text",
    "pipeline.description": "multiline",
    "pipeline.session_preset": "select",
    "pipeline.git_delivery.enabled": "checkbox",
    "pipeline.git_delivery.branch": "text",
    "pipeline.git_delivery.remote": "text",
}

NODE_CONTROLS = {
    "node.0.name": "text",
    "node.0.description": "multiline",
    "node.0.subject.goal": "multiline",
    "node.0.subject.instructions": "multiline",
    "node.0.subject.max_turns": "number",
    "node.0.approval.required": "checkbox",
    "node.0.approval.prompt": "multiline",
    "node.0.clarify": "checkbox",
    "node.0.max_bounces": "number",
    "node.0.baseline_marker": "checkbox",
    "node.0.adversarial": "checkbox",
    "node.0.review": "checkbox",
    "node.0.session_group": "text",
    "node.0.entry.goal": "multiline",
    "node.0.entry.ai_check": "multiline",
    "node.0.entry.change_aware": "checkbox",
    "node.0.exit.goal": "multiline",
    "node.0.exit.ai_check": "multiline",
    "node.0.exit.change_aware": "checkbox",
}

# 列表编辑：容器 + 每行的可编辑子字段（fixture 每类各 1 行）
LIST_CONTAINERS = [
    "node.0.entry.artifacts",
    "node.0.entry.commands",
    "node.0.exit.artifacts",
    "node.0.exit.commands",
]
LIST_ROW_CONTROLS = {
    "node.0.entry.artifacts.0.path": "text",
    "node.0.entry.artifacts.0.must_contain": "text",
    "node.0.entry.commands.0": "text",
    "node.0.exit.artifacts.0.path": "text",
    "node.0.exit.artifacts.0.must_contain": "text",
    "node.0.exit.commands.0": "text",
}

# 渲染表单用 fixture：覆盖全部字段、包含 mcp/skills 与未知键以验证只读/源码路径
FIXTURE = {
    "id": "tpl-static",
    "name": "静态检查流水线",
    "description": "desc",
    "session_preset": "independent",
    "git_delivery": {"enabled": True, "branch": "gdd/delivery", "remote": "origin"},
    "nodes": [
        {
            "id": "node-1",
            "name": "节点一",
            "description": "节点描述",
            "subject": {"goal": "目标", "instructions": "指令", "max_turns": 12},
            "approval": {"required": True, "prompt": "请审批"},
            "clarify": True,
            "max_bounces": 2,
            "baseline_marker": True,
            "adversarial": True,
            "review": True,
            "session_group": "g1",
            "entry": {
                "goal": "入口判据",
                "ai_check": "入口 AI 判据",
                "change_aware": True,
                "artifacts": [{"path": "a.md", "must_contain": "^# ", "note": "保留我"}],
                "commands": ["echo entry"],
            },
            "exit": {
                "goal": "出口判据",
                "ai_check": "出口 AI 判据",
                "change_aware": True,
                "artifacts": [{"path": "b.md"}],
                "commands": ["echo exit"],
            },
            "mcp": [{"name": "m"}],
            "skills": ["s"],
            "x_unknown": {"k": 1},
        }
    ],
}

CONTROL_RE = re.compile(
    r'<(textarea|input|select)\b([^>]*?)\bdata-field="([^"]+)"([^>]*)>', re.S
)
TOP_LEVEL_LINE_RE = re.compile(
    r"^refreshNav\(\);showNew\(\);setInterval\(refreshNav,\s*\d+\);$"
)


def _inline_scripts(html):
    """返回内嵌 <script ...> 的属性与正文；只解析，不执行页面启动逻辑。"""
    return re.findall(r"<script(\s[^>]*)?>(.*?)</script>", html, re.S)


def _strip_bootstrap(script):
    """去掉页面最后的启动语句（refreshNav/showNew/setInterval），只保留纯函数定义。"""
    lines = script.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not TOP_LEVEL_LINE_RE.match(lines[-1].strip()):
        return None
    lines.pop()
    return "\n".join(lines)


def _render_forms(script):
    """用 Node 求值页面纯函数，返回 (pipeline_html, detail_html)；失败返回 None。"""
    node = shutil.which("node")
    if not node:
        check(False, "D4 未找到 node，无法静态渲染表单控件（D4 未验证，不记为通过）")
        return None
    harness = (
        "\nconst pipelineData = "
        + json.dumps(FIXTURE, ensure_ascii=False)
        + ";\n"
        "const pipeHtml = tplPipelineForm(pipelineData);\n"
        "const detailEl = {innerHTML: ''};\n"
        "tplFormDetail(detailEl, pipelineData.nodes[0], 0);\n"
        "process.stdout.write(JSON.stringify({pipeline: pipeHtml, detail: detailEl.innerHTML}));\n"
    )
    with tempfile.TemporaryDirectory(prefix="gdd-tpl-static-") as td:
        js_path = os.path.join(td, "render_forms.js")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(script + "\n" + harness)
        try:
            proc = subprocess.run(
                [node, js_path], capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            check(False, f"D4 node 渲染表单失败：{exc}")
            return None
    if proc.returncode != 0:
        check(
            False,
            f"D4 node 渲染表单失败（exit {proc.returncode}）：{proc.stderr.strip()[:400]}",
        )
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        check(False, f"D4 node 输出不是合法 JSON：{exc}")
        return None
    return payload["pipeline"], payload["detail"]


def _controls(html):
    """解析渲染结果中的结构化控件：data-field -> 控件形态。"""
    out = {}
    for m in CONTROL_RE.finditer(html):
        tag, pre, field, post = m.groups()
        attrs = pre + " " + post
        if tag == "textarea":
            kind = "multiline"
        elif tag == "select":
            kind = "select"
        elif re.search(r'type="checkbox"', attrs):
            kind = "checkbox"
        elif re.search(r'type="number"', attrs):
            kind = "number"
        else:
            kind = "text"
        out[field] = kind
    return out


def run_d4():
    print("[D4] 可编辑字段覆盖（解析 gdd.ui.HTML，不启动 HTTP 服务）")
    html = gdd.ui.HTML
    scripts = _inline_scripts(html)
    check(len(scripts) == 1 and not (scripts[0][0] or "").strip(),
          "D4 页面恰有一个内联 <script>（无外部脚本属性）")
    if not scripts:
        return
    raw_script = scripts[0][1]

    # 1) TPL_FIELDS 与 spec.md 5.2 逐字段一致
    block = re.search(r"const TPL_FIELDS=\{(.*?)\};", raw_script, re.S)
    check(block is not None, "D4 内嵌 JS 含 const TPL_FIELDS 字段清单")
    if not block:
        return
    p_match = re.search(r"pipeline:\[(.*?)\]", block.group(1), re.S)
    n_match = re.search(r"node:\[(.*?)\]", block.group(1), re.S)
    check(p_match is not None and n_match is not None,
          "D4 TPL_FIELDS 含 pipeline[] 与 node[] 清单")
    if not (p_match and n_match):
        return
    tpl_pipeline = re.findall(r"'([^']+)'", p_match.group(1))
    tpl_node = re.findall(r"'([^']+)'", n_match.group(1))
    check(tpl_pipeline == PIPELINE_FIELDS,
          f"D4 TPL_FIELDS.pipeline 与 spec 5.2 一致（{len(PIPELINE_FIELDS)} 项）")
    check(tpl_node == NODE_FIELDS,
          f"D4 TPL_FIELDS.node 与 spec 5.2 一致（{len(NODE_FIELDS)} 项）")
    check("id" not in tpl_pipeline and "id" not in tpl_node,
          "D4 流水线/节点 id 不在结构化字段清单")
    check("mcp" not in tpl_node and "skills" not in tpl_node,
          "D4 mcp/skills 不在结构化字段清单（仅源码模式）")

    # 2) 生成/收集逻辑确实以 TPL_FIELDS 为唯一字段来源
    for needle in (
        "TPL_FIELDS.pipeline.forEach",
        "TPL_FIELDS.node.filter",
        "TPL_FIELDS.node.forEach",
    ):
        check(needle in raw_script, f"D4 表单生成/收集引用 {needle}")
    check(raw_script.count('data-field="${esc(') >= 6,
          "D4 data-field 标识由统一控件构造函数生成并转义")

    # 3) 用页面自身纯函数渲染表单，逐项检查控件存在
    stripped = _strip_bootstrap(raw_script)
    check(stripped is not None, "D4 可从内嵌脚本剥离页面启动语句（仅静态求值）")
    if stripped is None:
        return
    rendered = _render_forms(stripped)
    if rendered is None:
        return
    pipeline_html, detail_html = rendered
    actual = _controls(pipeline_html + detail_html)
    check(len(actual) == len(_controls(pipeline_html)) + len(_controls(detail_html)),
          "D4 渲染出的 data-field 无重复标识")

    for field, kind in PIPELINE_CONTROLS.items():
        got = actual.get(field)
        check(got == kind, f"D4 流水线控件 {field} 存在且为 {kind}（实际 {got}）")
    for field, kind in NODE_CONTROLS.items():
        got = actual.get(field)
        check(got == kind, f"D4 节点控件 {field} 存在且为 {kind}（实际 {got}）")
    for field, kind in LIST_ROW_CONTROLS.items():
        got = actual.get(field)
        check(got == kind, f"D4 列表行控件 {field} 存在且为 {kind}（实际 {got}）")
    for field in LIST_CONTAINERS:
        gate, name = field.rsplit(".", 1)
        check(
            f'data-field="{field}" data-list="{name}"' in detail_html,
            f"D4 列表编辑器容器 {field} 存在（data-list={name}）",
        )
    check(len(re.findall(r"data-art-row(?![-\w])", detail_html)) == 2,
          "D4 artifacts 行可增删（entry/exit 各 1 行）")
    check(len(re.findall(r"data-cmd-row(?![-\w])", detail_html)) == 2,
          "D4 commands 行可增删（entry/exit 各 1 行）")
    check(detail_html.count("＋ 添加产物") == 2,
          "D4 artifacts 提供「添加产物」操作（entry/exit）")
    check(detail_html.count("＋ 添加命令") == 2,
          "D4 commands 提供「添加命令」操作（entry/exit）")

    # 4) id 只读、mcp/skills 无结构化控件、源码模式路径保留
    check("pipeline.id" not in actual, "D4 流水线 id 无结构化编辑控件")
    check("node.0.id" not in actual, "D4 节点 id 无结构化编辑控件")
    check('data-readonly="pipeline-id"' in pipeline_html,
          "D4 流水线 id 以只读标识展示（data-readonly=pipeline-id）")
    check('data-readonly="node-id"' in detail_html,
          "D4 节点 id 以只读标识展示（data-readonly=node-id）")
    check("node.0.mcp" not in actual and "node.0.skills" not in actual,
          "D4 mcp/skills 无结构化编辑控件")
    check('<div class="tpl-k">mcp</div>' in detail_html
          and "只读摘要；源码模式可修改" in detail_html,
          "D4 mcp 为只读摘要并提示源码模式")
    check('<div class="tpl-k">skills</div>' in detail_html,
          "D4 skills 为只读摘要并提示源码模式")
    check("mcp / skills 仅源码可改" in detail_html,
          "D4 节点详情明示 mcp/skills 走源码模式")
    check("其他字段（只读）" in detail_html,
          "D4 未知节点字段只读展示（保存时原样保留）")
    check("function renderTplSource()" in raw_script
          and 'data-view="template-source"' in raw_script,
          "D4 源码视图保留（renderTplSource / data-view=template-source）")
    check('id="tplEdit"' in raw_script and 'id="tplTabSource"' in raw_script,
          "D4 源码编辑 textarea 与「源码」页签存在")


def run_d14():
    print("[D14] 文档与依赖就绪（零前端运行时依赖/构建链）")
    html = gdd.ui.HTML
    lower = html.lower()

    with open(os.path.join(ROOT, "gdd", "ui.py"), encoding="utf-8") as f:
        ui_source = f.read()
    check('HTML = r"""' in ui_source and html.lstrip().startswith("<!DOCTYPE html>"),
          "D14 gdd/ui.py 仍以内嵌原始字符串提供单页 HTML")

    scripts = _inline_scripts(html)
    check(len(scripts) == 1 and not (scripts[0][0] or "").strip(),
          "D14 恰有一个内联 <script>，无外部脚本属性")
    check(re.search(r"<script\s+[^>]*\bsrc\s*=", html, re.I) is None,
          "D14 无 <script src=...> 外部脚本")
    check("<link" not in lower, "D14 无 <link ...> 外部样式/资源引用")
    check(re.search(r'(?:src|href)\s*=\s*["\']https?://', html, re.I) is None,
          "D14 无 src/href 指向 http(s) 的外部资源")
    for host in ("cdn.", "unpkg.com", "jsdelivr", "cdnjs", "googleapis",
                 "bootstrapcdn", "fontawesome", "staticfile", "bootcdn"):
        check(host not in lower, f"D14 HTML 无 CDN 引用（{host}）")
    check("<style>" in html and "</style>" in html,
          "D14 样式仍内联于页面（<style>）")

    for doc in ("README.md", "USER_GUIDE.md"):
        with open(os.path.join(ROOT, doc), encoding="utf-8") as f:
            text = f.read()
        for kw in ("可视化", "编辑", "新建模板", "内置只读", "源码", "YAML", "Playwright"):
            check(kw in text, f"D14 {doc} 模板管理说明含「{kw}」")
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    check("tests/test_template_ui_static.py" in readme,
          "D14 README 测试命令列出本静态检查脚本")

    with open(os.path.join(ROOT, "package.json"), encoding="utf-8") as f:
        pkg = json.load(f)
    deps = pkg.get("dependencies") or {}
    check(set(deps) == {"@earendil-works/pi-coding-agent", "pi-subagents"},
          f"D14 package.json 依赖仍仅既有 pi 执行端两项（实际 {sorted(deps)}）")
    check(deps.get("@earendil-works/pi-coding-agent") == "1.4.0"
          and deps.get("pi-subagents") == "0.88.0",
          "D14 pi 执行端依赖版本未变（1.4.0 / 0.88.0）")
    check(not pkg.get("devDependencies"),
          "D14 package.json 无前端 devDependencies")

    for name in (
        "vite.config.js", "vite.config.ts", "vite.config.mjs",
        "webpack.config.js", "webpack.config.ts",
        "rollup.config.js", "rollup.config.ts",
        "esbuild.config.js", "esbuild.config.ts",
        "parcel.json", "next.config.js", "nuxt.config.js",
        "svelte.config.js", "angular.json",
        "tailwind.config.js", "postcss.config.js", "babel.config.json",
    ):
        check(not os.path.exists(os.path.join(ROOT, name)),
              f"D14 无新增前端构建配置 {name}")


def main():
    run_d4()
    run_d14()
    if FAILS:
        print(f"\n未通过 {len(FAILS)} 项：")
        for msg in FAILS:
            print(" -", msg)
        return 1
    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
