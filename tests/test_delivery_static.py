"""静态交付检查：契约文档、依赖锁定、仓库卫生（D6 / D16）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def main():
    print("[D6] 验收用例契约")
    with open(os.path.join(ROOT, "docs", "covenant", "acceptance-cases.md"),
              encoding="utf-8") as f:
        acceptance_doc = f.read()
    check("@task:" in acceptance_doc, "契约含 @task: 语法")
    check("校验" in acceptance_doc and "无效示例" in acceptance_doc,
          "契约含校验规则与无效示例")
    check("Scenario Outline" in acceptance_doc, "说明只识别 Scenario")
    check("task_route_fallback" in acceptance_doc, "说明找不到关联时整体回退")

    print("[D16] 依赖与文档")
    with open(os.path.join(ROOT, "package.json"), encoding="utf-8") as f:
        pkg = json.load(f)
    check(pkg.get("private") is True, "package.json 私有包")
    check(pkg["dependencies"].get("@earendil-works/pi-coding-agent") == "1.4.0",
          "pi 锁定 1.4.0")
    check(pkg["dependencies"].get("pi-subagents") == "0.88.0", "pi-subagents 锁定 0.88.0")
    check(pkg.get("engines", {}).get("node") == ">=22.19.0", "Node engines >=22.19.0")
    check(os.path.isfile(os.path.join(ROOT, "package-lock.json")), "package-lock 已生成")
    with open(os.path.join(ROOT, "package-lock.json"), encoding="utf-8") as f:
        lock = json.load(f)
    pkgs = lock.get("packages") or {}
    check(pkgs.get("node_modules/@earendil-works/pi-coding-agent", {}).get("version") == "1.4.0",
          "lockfile 含 pi 1.4.0")
    check(pkgs.get("node_modules/pi-subagents", {}).get("version") == "0.88.0",
          "lockfile 含 pi-subagents 0.88.0")
    with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
        ignore = f.read()
    check(".gdd/" in ignore, ".gdd/ 已忽略")
    check("package-lock.json" in ignore, ".gitignore 保留 package-lock.json 忽略项")
    check("iterations/" in ignore, ".gitignore 保留 iterations 忽略项")
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    for needle in ("pi 进程", "设置", "GDD_PI_SETTINGS", "pi-subagents", "@task:"):
        check(needle in readme, f"README 含 {needle}")
    check("顺序流水线（无并行" not in readme, "README 不再写「无并行」")
    with open(os.path.join(ROOT, "USER_GUIDE.md"), encoding="utf-8") as f:
        guide = f.read()
    for needle in ("npm ci", "设置", "pi", "GDD_PI_SETTINGS"):
        check(needle in guide, f"USER_GUIDE 含 {needle}")
    check("顺序流水线（无 DAG 并行）" not in guide, "USER_GUIDE 不再写旧边界")
    with open(os.path.join(ROOT, "docs", "design-steer.md"), encoding="utf-8") as f:
        steer_doc = f.read()
    check("steer_pi_extension" in steer_doc and "tool_result" in steer_doc,
          "steer 设计文档说明 pi 扩展")

    print("[D16] git 卫生")
    r = subprocess.run(["git", "check-ignore", ".gdd"], cwd=ROOT,
                       capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() == ".gdd", "git check-ignore .gdd")
    r = subprocess.run(["git", "check-ignore", "--no-index", "package-lock.json"],
                       cwd=ROOT, capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() == "package-lock.json",
          "package-lock.json 保留忽略项（--no-index）")
    r = subprocess.run(["git", "check-ignore", "--no-index",
                        "iterations/example/plan.md"],
                       cwd=ROOT, capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() == "iterations/example/plan.md",
          "iterations 保留忽略项（--no-index）")

    print("[D18/D22] 9 节点与 review 顺序")
    with open(os.path.join(ROOT, "gdd", "templates", "delivery.json"),
              encoding="utf-8") as f:
        tpl = json.load(f)
    ids = [n["id"] for n in tpl["nodes"]]
    check(len(ids) == 9, f"内置模板 9 节点（{len(ids)}）")
    check(ids.index("review") == ids.index("implement") + 1
          and ids.index("test") == ids.index("review") + 1,
          "review 位于 implement 与 test 之间")
    review = next(n for n in tpl["nodes"] if n["id"] == "review")
    check(review.get("review") is True, "review 节点带 review 标记")
    check(not (review.get("approval") or {}).get("required"),
          "review 不设人工审批")
    check(not review.get("session_group"), "review 不加入 implement 会话组")
    # 模板可被模型解析且顺序由列表决定
    from gdd.models import PipelineSpec
    spec = PipelineSpec.from_dict(tpl)
    check(spec.prev("review").id == "implement" and spec.next("review").id == "test",
          "PipelineSpec 解析后的前后关系")
    docs = {
        "gdd/cli.py": [("9 节点", True)],
        "gdd/builtins.py": [("9 节点", True), ("review", True)],
        "docs/design-go.md": [("9 节点", True)],
        "docs/design-session-group.md": [("review", True)],
        "README.md": [("9 节点", True), ("implement → review → test", True)],
        "USER_GUIDE.md": [("9 节点", True), ("review", True)],
    }
    for rel, needles in docs.items():
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            body = f.read()
        for needle, _ in needles:
            check(needle in body, f"{rel} 含 {needle}")
    print("ALL DELIVERY STATIC PASSED")


if __name__ == "__main__":
    main()
