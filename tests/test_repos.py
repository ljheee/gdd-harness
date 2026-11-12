"""多仓库：克隆、基线分支、任务 worktree。"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.cli import cmd_init
from gdd.gates import build_review_prompt
from gdd.iteration import prepare
from gdd.models import NodeSpec
from gdd.orchestrator import Orchestrator
from gdd.repos import prepare_repos, repo_dirname, resolve_baseline
from gdd.builtins import DELIVERY_PIPELINE


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def make_repo(path, branch):
    os.makedirs(path)
    git(path, "init", "-q", "-b", branch)
    git(path, "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
        "commit", "--allow-empty", "-m", "base")


def test_names_and_baseline():
    print("[repos] 仓库名与默认基线")
    check(repo_dirname("ssh://git@host/team/gdd-harness.git") == "gdd-harness",
          "ssh 地址取仓库名")
    check(repo_dirname("git@host:team/other.git") == "other", "scp 地址取仓库名")
    parent = tempfile.mkdtemp(prefix="gdd-repos-")
    try:
        src = os.path.join(parent, "only-main")
        make_repo(src, "main")
        clone = os.path.join(parent, "clone-main")
        subprocess.check_call(["git", "clone", "-q", src, clone])
        check(resolve_baseline(clone, "") == "main", "没有 master 时用 main")
        check(resolve_baseline(clone, "main") == "main", "填写的基线优先")
    finally:
        import shutil
        shutil.rmtree(parent)


def test_clone_and_worktree():
    print("[repos] 克隆、基线保持、任务检出")
    parent = tempfile.mkdtemp(prefix="gdd-repos-wt-")
    try:
        src = os.path.join(parent, "src")
        make_repo(src, "master")
        with open(os.path.join(src, "README"), "w", encoding="utf-8") as f:
            f.write("v1\n")
        git(src, "add", "README")
        git(src, "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
            "commit", "-m", "readme")
        code = os.path.join(parent, "code")
        work = os.path.join(parent, "work")
        task = os.path.join(work, "round-1")
        os.makedirs(task)
        records = prepare_repos(code, task, "round-1", [{"url": src, "base": ""}])
        check(len(records) == 1 and records[0]["base"] == "master", "默认基线是 master")
        check(records[0]["branch"] == "gdd/round-1", "任务分支 gdd/<任务名>")
        check(git(records[0]["clone"], "symbolic-ref", "--short", "HEAD").stdout.strip() == "master",
              "长期克隆仍在 master")
        check(git(records[0]["worktree"], "symbolic-ref", "--short", "HEAD").stdout.strip()
              == "gdd/round-1", "worktree 在任务分支")
        check(os.path.isfile(os.path.join(records[0]["worktree"], "README")), "检出里有源码")
        check(os.path.isfile(os.path.join(records[0]["worktree"], ".git")), "检出只有 .git 文件")
        pipe = json.loads(json.dumps(DELIVERY_PIPELINE))
        bound = prepare(pipe, task, name="round-1", task_home=True)
        check(bound["nodes"][0]["exit"]["artifacts"][0]["path"].endswith("requirement.md")
              and "iterations/" not in bound["nodes"][0]["exit"]["artifacts"][0]["path"],
              "文档路径在任务目录，不在 iterations/")
        o = Orchestrator(task)
        o.init(bound, "需求", repos=records)
        check(o._plan_path().endswith(os.path.join("round-1", "plan.md")),
              f"计划写在任务根（{o._plan_path()}）")
        check("docs" not in o._plan_path().split(os.sep), "不回退到 docs/")
        with open(os.path.join(task, "stray.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        o._deliver_git(o.pipeline.nodes[0])
        check("stray.py" in (o.state.get("commit_failed") or {}).get("reason", ""),
              "任务根上的代码不算交付成功")
        check(o.state["contract_files"] == ["goal.md", "spec.md", "plan.md", "decisions.md"],
              "契约文件在任务根")
        check(o.state["repos"][0]["worktree"] == records[0]["worktree"], "状态记下 worktree")
        check(any(e.get("event") == "git_delivery_ready" for e in o.state["history"]),
              "交付按 worktree 就绪，不在任务目录建仓")
        check(not os.path.exists(os.path.join(task, ".git")), "任务目录本身不是 git 仓库")
        git(src, "checkout", "-q", "-b", "develop")
        git(src, "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
            "commit", "--allow-empty", "-m", "dev")
        git(src, "checkout", "-q", "master")
        task2 = os.path.join(work, "round-2")
        os.makedirs(task2)
        again = prepare_repos(code, task2, "round-2", [{"url": src, "base": "develop"}])
        check(git(again[0]["clone"], "symbolic-ref", "--short", "HEAD").stdout.strip() == "master",
              "另一条基线不切走长期克隆")
        check(git(again[0]["worktree"], "symbolic-ref", "--short", "HEAD").stdout.strip()
              == "gdd/round-2", "新任务仍有自己的检出")
    finally:
        import shutil
        shutil.rmtree(parent)


def _pipe(name, artifacts=None, marker=False, commands=None):
    node = {
        "id": "implement",
        "name": "实现",
        "baseline_marker": marker,
        "subject": {"goal": "改代码"},
        "exit": {"artifacts": [{"path": p} for p in (artifacts or [])]},
    }
    if commands:
        node["entry"] = {"commands": commands}
    return {
        "id": "mini",
        "git_delivery": {"enabled": True, "branch": f"gdd/{name}"},
        "iteration": {
            "name": name, "output": "", "task_home": True, "branch": f"gdd/{name}",
        },
        "nodes": [node],
    }


def _two(parent, task_name):
    srcs = []
    for leaf, branch in (("alpha", "master"), ("beta", "main")):
        src = os.path.join(parent, leaf)
        make_repo(src, branch)
        with open(os.path.join(src, "README"), "w", encoding="utf-8") as f:
            f.write(leaf + "\n")
        git(src, "add", "README")
        git(src, "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
            "commit", "-q", "-m", "readme")
        srcs.append(src)
    code = os.path.join(parent, "code")
    task = os.path.join(parent, "work", task_name)
    os.makedirs(task)
    records = prepare_repos(
        code, task, task_name,
        [{"url": srcs[0], "base": ""}, {"url": srcs[1], "base": ""}])
    o = Orchestrator(task)
    return srcs, records, o


def test_commit_failure_stays_when_another_repo_succeeds():
    print("[repos] 一个仓库提交失败不被另一个仓库清掉")
    parent = tempfile.mkdtemp(prefix="gdd-repos-fail-")
    try:
        _srcs, records, o = _two(parent, "hooks")
        o.init(_pipe("hooks"), "需求", repos=records)
        hook = os.path.join(records[0]["clone"], ".git", "hooks", "pre-commit")
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho hook-refuse >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        for rec in records:
            with open(os.path.join(rec["worktree"], "feature.py"), "w", encoding="utf-8") as f:
                f.write("x = 1\n")
        o._deliver_git(o.pipeline.nodes[0])
        failed = o.state.get("commit_failed_repos") or {}
        check("alpha" in failed, f"alpha 的提交失败还在（{failed}）")
        check(o.state.get("commit_failed"), "总槽仍标着提交失败")
        beta_log = git(records[1]["worktree"], "log", "-1", "--name-only").stdout
        alpha_log = git(records[0]["worktree"], "log", "-1", "--name-only").stdout
        check("feature.py" in beta_log, "beta 的代码已经提交")
        check("feature.py" not in alpha_log, "alpha 的失败提交没有入库")
        cleared = [e for e in o.state["history"] if e.get("event") == "commit_failed_cleared"]
        check(not cleared, f"本轮还有仓库失败，不能记成已解除（{cleared}）")
    finally:
        shutil.rmtree(parent)


def test_each_repo_keeps_its_baseline():
    print("[repos] 每个仓库用自己的基线 diff，且不覆盖第一次基线")
    parent = tempfile.mkdtemp(prefix="gdd-repos-base-")
    try:
        _srcs, records, o = _two(parent, "bases")
        o.init(_pipe("bases", marker=True, commands=["true"]), "需求", repos=records)
        node = o.pipeline.nodes[0]
        ns = o.state["node_states"][node.id]

        class Dummy:
            pass

        check(o._phase_entry(node, ns, Dummy()) is True, "第一次 entry 记下基线")
        first = dict(o.state["baseline_commits:implement"])
        check(set(first) == {"alpha", "beta"}, f"两份仓库都有基线（{first}）")
        git(records[1]["worktree"], "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
            "commit", "-q", "--allow-empty", "-m", "later")
        ns["phase"] = "pending"
        check(o._phase_entry(node, ns, Dummy()) is True, "第二次 entry 仍通过")
        check(o.state["baseline_commits:implement"] == first,
              f"基线停在第一次 entry（{o.state['baseline_commits:implement']}）")
        with open(os.path.join(records[1]["worktree"], "README"), "w", encoding="utf-8") as f:
            f.write("beta changed\n")
        text = o._change_summary("implement")
        beta = text.split("[beta]", 1)[-1]
        check(f"git diff --stat {first['beta'][:10]}" in beta, f"beta 用自己的基线（{beta[:400]}）")
        check("README" in beta.split("git status")[0], "beta 的 diff 能看到 README")
    finally:
        shutil.rmtree(parent)


def test_review_prompt_uses_task_root():
    print("[repos] 评审提示里的文档在任务根")
    node = NodeSpec.from_dict({"id": "review", "name": "评审", "review": True})
    text = build_review_prompt(node, "", "diff")
    check("- ./goal.md" in text, f"空产物目录写成当前目录（{text[text.find('评审依据'):text.find('评审依据')+220]}）")
    check("- /goal.md" not in text, "不能拼到文件系统根")
    nested = build_review_prompt(node, "iterations/feat", "diff")
    check("- iterations/feat/goal.md" in nested, "有产物目录时仍用相对路径")


def test_task_home_without_placeholders():
    print("[repos] 没有占位符的模板也落在任务目录")
    root = tempfile.mkdtemp(prefix="gdd-repos-plain-")
    try:
        custom = {
            "id": "c",
            "nodes": [{
                "id": "n", "name": "节点",
                "subject": {"goal": "写 a.md"},
                "exit": {"artifacts": [{"path": "a.md"}]},
            }],
            "git_delivery": {"enabled": True, "branch": "gdd/delivery"},
        }
        bound = prepare(custom, root, name="mini", task_home=True)
        check(bound["iteration"].get("task_home") is True, "记下 task_home")
        check(bound["git_delivery"]["branch"] == "gdd/mini", "交付分支与 worktree 一致")
        check(bound["nodes"][0]["exit"]["artifacts"][0]["path"] == "a.md", "原有产物路径不动")
        o = Orchestrator(root)
        o.init(bound, "需求")
        check(o._output_dir() == "", "产物目录就是任务根")
        check("docs" not in o._plan_path().split(os.sep), f"计划不进 docs/（{o._plan_path()}）")
    finally:
        shutil.rmtree(root)


def test_missing_requirement_releases_repos():
    print("[repos] 需求文件打不开时撤掉本次检出")
    parent = tempfile.mkdtemp(prefix="gdd-repos-req-")
    try:
        src = os.path.join(parent, "src-cli")
        make_repo(src, "master")
        code = os.path.join(parent, "code")
        task = os.path.join(parent, "work", "cli-miss")
        os.makedirs(task)
        rc = cmd_init(argparse.Namespace(
            root=task, pipeline="delivery", name="cli-miss", branch="", base="",
            repo=[src], repo_base=[], repos_dir=code,
            requirement=os.path.join(parent, "missing.md"),
        ))
        check(rc == 1, "需求文件缺失时初始化失败")
        check(not os.path.isdir(os.path.join(task, "worktrees", "src-cli")), "worktree 已撤")
        check(not os.path.isdir(os.path.join(code, "src-cli")), "本次新建的克隆已撤")
    finally:
        shutil.rmtree(parent)


def test_created_branch_removed_preexisting_kept():
    print("[repos] 只删这次新建的分支，重试用更新后的基线")
    parent = tempfile.mkdtemp(prefix="gdd-repos-br-")
    try:
        src = os.path.join(parent, "src")
        make_repo(src, "master")
        with open(os.path.join(src, "README"), "w", encoding="utf-8") as f:
            f.write("v1\n")
        git(src, "add", "README")
        git(src, "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
            "commit", "-q", "-m", "v1")
        code = os.path.join(parent, "code")
        os.makedirs(code)
        clone = os.path.join(code, "src")
        subprocess.check_call(["git", "clone", "-q", src, clone])
        git(clone, "branch", "gdd/keep")
        task = os.path.join(parent, "work", "keep")
        os.makedirs(task)
        try:
            prepare_repos(code, task, "keep",
                          [{"url": src, "base": ""}, {"url": "bad name", "base": ""}])
            raise AssertionError("坏地址应失败")
        except ValueError:
            pass
        check(git(clone, "rev-parse", "--verify", "refs/heads/gdd/keep").returncode == 0,
              "调用前已有的分支还在")
        fresh = os.path.join(parent, "work", "fresh")
        os.makedirs(fresh)
        try:
            prepare_repos(code, fresh, "fresh",
                          [{"url": src, "base": ""}, {"url": "bad name", "base": ""}])
            raise AssertionError("坏地址应失败")
        except ValueError:
            pass
        check(git(clone, "rev-parse", "--verify", "refs/heads/gdd/fresh").returncode != 0,
              "这次新建的分支随失败一起删掉")
        with open(os.path.join(src, "README"), "w", encoding="utf-8") as f:
            f.write("v2\n")
        git(src, "add", "README")
        git(src, "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
            "commit", "-q", "-m", "v2")
        again_dir = os.path.join(parent, "work", "fresh-2")
        os.makedirs(again_dir)
        again = prepare_repos(code, again_dir, "fresh", [{"url": src, "base": ""}])
        head = git(again[0]["worktree"], "rev-parse", "HEAD").stdout.strip()
        tip = git(src, "rev-parse", "HEAD").stdout.strip()
        check(head == tip, "重试的任务分支从更新后的基线拉出")
    finally:
        shutil.rmtree(parent)


def test_task_docs_are_not_added_into_worktree():
    print("[repos] 任务根文档不进代码仓库")
    parent = tempfile.mkdtemp(prefix="gdd-repos-doc-")
    try:
        src = os.path.join(parent, "src")
        make_repo(src, "master")
        with open(os.path.join(src, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("*.md\n")
        git(src, "add", ".gitignore")
        git(src, "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
            "commit", "-q", "-m", "ignore md")
        code = os.path.join(parent, "code")
        task = os.path.join(parent, "work", "docs")
        os.makedirs(task)
        records = prepare_repos(code, task, "docs", [{"url": src, "base": ""}])
        o = Orchestrator(task)
        o.init(_pipe("docs", artifacts=["goal.md"]), "需求", repos=records)
        with open(os.path.join(task, "goal.md"), "w", encoding="utf-8") as f:
            f.write("# 目标\n")
        with open(os.path.join(records[0]["worktree"], "code.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        o._deliver_git(o.pipeline.nodes[0])
        check(not o.state.get("commit_failed"),
              f"代码提交不受任务根文档影响（{o.state.get('commit_failed')}）")
        shown = git(records[0]["worktree"], "log", "-1", "--name-only").stdout
        check("code.py" in shown, f"代码已提交（{shown}）")
        check("goal.md" not in shown, "goal.md 不进代码仓库")
    finally:
        shutil.rmtree(parent)


def test_acknowledge_refreshes_each_worktree():
    print("[repos] 确认滞留后按仓库刷新快照")
    parent = tempfile.mkdtemp(prefix="gdd-repos-ack-")
    try:
        src = os.path.join(parent, "src")
        make_repo(src, "master")
        code = os.path.join(parent, "code")
        task = os.path.join(parent, "work", "ack")
        os.makedirs(task)
        records = prepare_repos(code, task, "ack", [{"url": src, "base": ""}])
        o = Orchestrator(task)
        o.init(_pipe("ack"), "需求", repos=records)
        wt = records[0]["worktree"]
        git(wt, "checkout", "-q", "-b", "side")
        git(wt, "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
            "commit", "-q", "--allow-empty", "-m", "side")
        git(wt, "checkout", "-q", "gdd/ack")
        o._git_cwd = wt
        check(o._stray_from_branch_snapshot("gdd/ack") == "side", "确认前能看到滞留分支")
        o._git_cwd = None
        o.state["stray_delivery"] = {"node": "implement", "ref": "side", "reason": "滞留"}
        o._save()
        check(o.acknowledge() is True, "确认滞留成功")
        o._git_cwd = wt
        check(o._stray_from_branch_snapshot("gdd/ack") == "", "确认后同一滞留不再报")
    finally:
        shutil.rmtree(parent)


def main():
    test_names_and_baseline()
    test_clone_and_worktree()
    test_commit_failure_stays_when_another_repo_succeeds()
    test_each_repo_keeps_its_baseline()
    test_review_prompt_uses_task_root()
    test_task_home_without_placeholders()
    test_missing_requirement_releases_repos()
    test_created_branch_removed_preexisting_kept()
    test_task_docs_are_not_added_into_worktree()
    test_acknowledge_refreshes_each_worktree()
    print("ALL REPOS PASSED")


if __name__ == "__main__":
    main()
