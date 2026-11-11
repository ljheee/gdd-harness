"""交付层 force-add：.gitignore 保留 iterations 忽略项时，exit 声明产物仍入库（R4 / D23）。

运行：python3 tests/test_delivery_force_add.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.orchestrator import Orchestrator  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def _git(d, *args):
    return subprocess.run(["git", *args], cwd=d, check=True,
                          capture_output=True, text=True)


def _write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _init_repo(d):
    _git(d, "init", "-q", "-b", "gdd/delivery")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _write(os.path.join(d, ".gitignore"), ".gdd/\niterations/\n")
    _write(os.path.join(d, "seed.txt"), "seed\n")
    _git(d, "add", ".gitignore", "seed.txt")
    _git(d, "commit", "-qm", "baseline")


def _pipeline(artifacts):
    return {
        "id": "force-add",
        "git_delivery": {"enabled": True, "branch": "gdd/delivery"},
        "nodes": [{
            "id": "n1", "name": "交付节点",
            "subject": {"goal": "产出产物"},
            "exit": {"artifacts": artifacts},
        }],
    }


def test_declared_artifact_force_added():
    print("[R4-1] 声明产物被忽略仍进入提交")
    d = tempfile.mkdtemp(prefix="gdd-force-add-")
    try:
        _init_repo(d)
        o = Orchestrator(d)
        o.init(_pipeline([{"path": "iterations/x/artifact.md"}]), "需求")
        _write(os.path.join(d, "iterations", "x", "artifact.md"), "# 产物\n")
        _write(os.path.join(d, "iterations", "x", "undeclared.txt"), "未声明\n")
        o._deliver_git(o.pipeline.node("n1"))
        committed = _git(d, "show", "--name-only", "--pretty=format:", "HEAD").stdout
        tracked = set(_git(d, "ls-files").stdout.splitlines())
        check("iterations/x/artifact.md" in committed,
              "声明产物进入交付提交")
        check("iterations/x/undeclared.txt" not in tracked,
              "未声明的 ignored 文件不进入索引")
        check(".gdd/state.json" not in tracked, ".gdd 不进入索引")
        events = [e.get("event") for e in o.state["history"]]
        check("git_force_add" in events, "记录 git_force_add 事件")
        check("git_commit" in events and "git_commit_fail" not in events,
              "交付成功且无 commit 失败")
        check(not o.state.get("commit_failed"), "无 commit_failed 警示")
    finally:
        shutil.rmtree(d)


def test_package_lock_style_ignore_kept():
    print("[R4-2] 非声明 ignored 文件不被 force-add")
    d = tempfile.mkdtemp(prefix="gdd-force-add2-")
    try:
        _init_repo(d)
        with open(os.path.join(d, ".gitignore"), "a", encoding="utf-8") as f:
            f.write("package-lock.json\n")
        _git(d, "add", ".gitignore")
        _git(d, "commit", "-qm", "ignore lock")
        o = Orchestrator(d)
        o.init(_pipeline([{"path": "iterations/x/artifact.md"}]), "需求")
        _write(os.path.join(d, "iterations", "x", "artifact.md"), "# 产物\n")
        _write(os.path.join(d, "package-lock.json"), "{}\n")
        o._deliver_git(o.pipeline.node("n1"))
        tracked = set(_git(d, "ls-files").stdout.splitlines())
        check("iterations/x/artifact.md" in tracked, "声明产物被强制加入")
        check("package-lock.json" not in tracked,
              "未声明的 package-lock.json 不被强制加入")
    finally:
        shutil.rmtree(d)


def main():
    test_declared_artifact_force_added()
    test_package_lock_style_ignore_kept()
    print("ALL DELIVERY FORCE-ADD TESTS PASSED")


if __name__ == "__main__":
    main()
