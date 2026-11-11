"""迭代目录、分支和只读基线。"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.iteration import prepare
from gdd.orchestrator import Orchestrator
from gdd.revision import apply_staged, contract_for


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


PIPE = {
    "id": "t",
    "nodes": [{
        "id": "n1",
        "name": "节点",
        "subject": {"goal": "写 {{output}}/goal.md"},
        "exit": {"artifacts": [{"path": "{{output}}/goal.md"}]},
    }],
    "git_delivery": {"enabled": False, "branch": "gdd/{{name}}"},
}


def test_bind_and_refuse():
    print("[iteration] 绑定、占用、只读基线")
    root = tempfile.mkdtemp(prefix="gdd-iter-")
    try:
        try:
            prepare(PIPE, root)
            raise AssertionError("缺名字应拒绝")
        except ValueError as e:
            check("--name" in str(e), "缺名字被拒绝")
        os.makedirs(os.path.join(root, "iterations", "old"))
        with open(os.path.join(root, "iterations", "old", "spec.md"), "w") as f:
            f.write("# 旧规格\n")
        with open(os.path.join(root, "iterations", "old", "decisions.md"), "w") as f:
            f.write("# 旧决策\n")
        bound = prepare(PIPE, root, name="new-1")
        check(bound["git_delivery"]["branch"] == "gdd/new-1", "默认分支 gdd/<名字>")
        check(bound["nodes"][0]["exit"]["artifacts"][0]["path"] == "iterations/new-1/goal.md",
              "产物路径落到迭代目录")
        check(bound["iteration"]["prior"] == [
            "iterations/old/spec.md", "iterations/old/decisions.md"], "上一轮契约列入只读基线")
        os.makedirs(os.path.join(root, "iterations", "new-1"), exist_ok=True)
        with open(os.path.join(root, "iterations", "new-1", "goal.md"), "w") as f:
            f.write("x")
        try:
            prepare(PIPE, root, name="new-1")
            raise AssertionError("同名已有文件应拒绝")
        except ValueError as e:
            check("已有文件" in str(e), "同名目录被拒绝")
        custom = {"id": "c", "nodes": [{"id": "n", "subject": {"goal": "写 a.md"},
                                        "exit": {"artifacts": [{"path": "a.md"}]}}]}
        kept = prepare(custom, root, name="other")
        check(kept["nodes"][0]["exit"]["artifacts"][0]["path"] == "a.md",
              "无占位符的模板不改路径")
        overridden = prepare(PIPE, root, name="new-2", branch="feature/new-2", base="dev")
        check(overridden["git_delivery"]["branch"] == "feature/new-2", "分支可覆盖")
        check(overridden["iteration"]["base"] == "dev", "基线分支记下")
        text = contract_for(bound["iteration"] and [
            "iterations/new-1/goal.md", "iterations/new-1/spec.md",
            "iterations/new-1/plan.md", "iterations/new-1/decisions.md"])
        check("iterations/new-1/decisions.md" in text
              and ".gdd/revision-stage/iterations/new-1/decisions.md" in text,
              "定级契约改指向这次迭代")
        check("docs/goal.md" not in text, "定级契约不再写 docs/goal.md")
    finally:
        subprocess.run(["rm", "-rf", root], check=True)
    print("[iteration] PASS")


def test_init_records_baseline_and_branch():
    print("[iteration] 初始化记下契约并从基线分支拉出")
    root = tempfile.mkdtemp(prefix="gdd-iter-git-")
    try:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "base"], cwd=root, check=True)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                               text=True, check=True).stdout.strip()
        subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "side"], cwd=root, check=True)
        os.makedirs(os.path.join(root, "iterations", "prev"))
        with open(os.path.join(root, "iterations", "prev", "spec.md"), "w") as f:
            f.write("spec")
        pipe = {
            "id": "t",
            "nodes": [{
                "id": "n1", "name": "节点",
                "subject": {"goal": "写 {{output}}/goal.md"},
                "exit": {"artifacts": [{"path": "{{output}}/goal.md"}]},
            }],
            "git_delivery": {"enabled": True, "branch": "gdd/{{name}}"},
        }
        bound = prepare(pipe, root, name="feat", base="main")
        o = Orchestrator(root)
        o.init(bound, "需求")
        check(o.state["contract_files"][0] == "iterations/feat/goal.md", "契约路径在 state")
        check(o.state["prior_contracts"] == ["iterations/prev/spec.md"], "只读基线写入 state")
        prompt = o._decorate_subject_prompt(o.pipeline.nodes[0], "开始")
        check("iterations/prev/spec.md" in prompt and "只读" in prompt, "主体提示带上只读基线")
        check("已经写下" in prompt and "已确认的既有" not in prompt,
              "未跑完的迭代不称作已确认")
        check("iterations/feat/goal.md" in prompt, "定级契约使用这次的路径")
        branch = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=root,
                                 capture_output=True, text=True, check=True).stdout.strip()
        check(branch == "gdd/feat", f"交付分支是 gdd/feat（{branch}）")
        onto_main = subprocess.run(["git", "merge-base", "HEAD", "main"], cwd=root,
                                    capture_output=True, text=True, check=True).stdout.strip()
        side_tip = subprocess.run(["git", "rev-parse", "side"], cwd=root,
                                   capture_output=True, text=True, check=True).stdout.strip()
        check(onto_main == base, "交付分支从 main 拉出")
        check(side_tip != onto_main and subprocess.run(
            ["git", "merge-base", "--is-ancestor", "side", "HEAD"], cwd=root).returncode != 0,
              "side 上多出来的提交没有带进交付分支")
    finally:
        subprocess.run(["rm", "-rf", root], check=True)
    print("[iteration] git PASS")


def test_existing_branch_does_not_swallow_base():
    print("[iteration] 已有交付分支不能吞掉基线")
    root = tempfile.mkdtemp(prefix="gdd-iter-old-")
    try:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "base"], cwd=root, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "gdd/feat"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "old"], cwd=root, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=root, check=True)
        pipe = {
            "id": "t",
            "nodes": [{
                "id": "n1", "name": "节点",
                "subject": {"goal": "写 {{output}}/goal.md"},
                "exit": {"artifacts": [{"path": "{{output}}/goal.md"}]},
            }],
            "git_delivery": {"enabled": True, "branch": "gdd/{{name}}"},
        }
        bound = prepare(pipe, root, name="feat", base="main")
        try:
            Orchestrator(root).init(bound, "需求")
            raise AssertionError("旧分支应拒绝")
        except RuntimeError as e:
            check("已存在" in str(e), "旧交付分支被拒绝")
        check(not os.path.exists(os.path.join(root, ".gdd", "state.json")),
              "拒绝时不留下状态")
        branch = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=root,
                                 capture_output=True, text=True, check=True).stdout.strip()
        check(branch == "main", f"仍停在基线分支（{branch}）")
    finally:
        subprocess.run(["rm", "-rf", root], check=True)
    print("[iteration] old branch PASS")


def test_default_branch_with_base_refuses_existing():
    print("[iteration] 省略分支名时 --base 也不能切回 gdd/delivery")
    root = tempfile.mkdtemp(prefix="gdd-iter-default-")
    try:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "base"], cwd=root, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "gdd/delivery"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "old"], cwd=root, check=True)
        old = subprocess.run(["git", "rev-parse", "gdd/delivery"], cwd=root,
                              capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "checkout", "-q", "main"], cwd=root, check=True)
        pipe = {"id": "min", "nodes": [{"id": "n1", "subject": {"goal": "g"}}]}
        bound = prepare(pipe, root, base="main")
        try:
            Orchestrator(root).init(bound, "需求")
            raise AssertionError("默认交付分支已存在时应拒绝")
        except RuntimeError as e:
            check("gdd/delivery" in str(e) and "已存在" in str(e), "默认分支被拒绝")
        check(not os.path.exists(os.path.join(root, ".gdd", "state.json")),
              "拒绝时不留下状态")
        head = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=root,
                               capture_output=True, text=True, check=True).stdout.strip()
        check(head == "main", f"仍停在基线分支（{head}）")
        tip = subprocess.run(["git", "rev-parse", "gdd/delivery"], cwd=root,
                              capture_output=True, text=True, check=True).stdout.strip()
        check(tip == old, "旧交付分支的提交还在")
        disabled = {
            "id": "min",
            "nodes": [{"id": "n1", "subject": {"goal": "g"}}],
            "git_delivery": {"enabled": False, "branch": "gdd/delivery"},
        }
        Orchestrator(root).init(prepare(disabled, root, base="main"), "需求")
        head = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=root,
                               capture_output=True, text=True, check=True).stdout.strip()
        check(head == "main", "交付关闭时不因为旧分支拒绝，也不切走")
    finally:
        subprocess.run(["rm", "-rf", root], check=True)
    print("[iteration] default branch PASS")


def test_name_token_does_not_retarget_contracts():
    print("[iteration] 只有 {{name}} 不改契约目录")
    root = tempfile.mkdtemp(prefix="gdd-iter-name-")
    try:
        os.makedirs(os.path.join(root, "iterations", "feat"))
        with open(os.path.join(root, "iterations", "feat", "goal.md"), "w") as f:
            f.write("occupied")
        pipe = {
            "id": "t",
            "nodes": [{
                "id": "n1", "name": "节点",
                "subject": {"goal": "写 docs/goal.md"},
                "exit": {"artifacts": [{"path": "docs/goal.md"}]},
            }],
            "git_delivery": {"enabled": False, "branch": "feature/{{name}}"},
        }
        bound = prepare(pipe, root, name="feat")
        check(bound["iteration"]["output"] == "", "不绑定产物目录")
        check(bound["nodes"][0]["exit"]["artifacts"][0]["path"] == "docs/goal.md",
              "产物路径保持 docs/goal.md")
        check(bound["git_delivery"]["branch"] == "gdd/feat", "分支仍换成 gdd/<名字>")
        check("{{name}}" not in json.dumps(bound), "名字占位符已替换")
        o = Orchestrator(root)
        o.init(bound, "需求")
        check(not o.state.get("contract_files"), "修订仍用默认的 docs/ 四份")
        prompt = o._decorate_subject_prompt(o.pipeline.nodes[0], "开始")
        check("docs/goal.md" in prompt and "iterations/feat/goal.md" not in prompt,
              "定级契约仍指向 docs/goal.md")
    finally:
        subprocess.run(["rm", "-rf", root], check=True)
    print("[iteration] name-only PASS")


def test_escaping_iteration_path_refused():
    print("[iteration] 越界迭代路径拒绝")
    root = tempfile.mkdtemp(prefix="gdd-iter-escape-")
    outside_name = "gdd-escape-out.md"
    outside = os.path.join(os.path.dirname(root), outside_name)
    try:
        pipe = {
            "id": "min",
            "nodes": [{"id": "n1", "subject": {"goal": "g"}}],
            "iteration": {"output": "../outside", "name": "x"},
        }
        kept = prepare(pipe, root)
        check(kept["iteration"]["output"] == "../outside", "未传名字时保留模板里的路径")
        try:
            Orchestrator(root).init(kept, "需求")
            raise AssertionError("越界路径应拒绝")
        except ValueError as e:
            check("越出" in str(e), "越界路径被拒绝")
        check(not os.path.exists(os.path.join(root, ".gdd", "state.json")),
              "拒绝时不留下状态")
        copied = apply_staged(root, ["../" + outside_name])
        check(copied == [], "修订落盘不写出任务根")
        check(not os.path.exists(outside), "任务根外没有被写入")
    finally:
        subprocess.run(["rm", "-rf", root], check=True)
        if os.path.exists(outside):
            os.remove(outside)
    print("[iteration] escape PASS")


if __name__ == "__main__":
    test_bind_and_refuse()
    test_init_records_baseline_and_branch()
    test_existing_branch_does_not_swallow_base()
    test_default_branch_with_base_refuses_existing()
    test_name_token_does_not_retarget_contracts()
    test_escaping_iteration_path_refused()
    print("ALL PASS")
