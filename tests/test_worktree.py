"""任务根落在外层 git 仓里时，初始化直接拒绝。"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.orchestrator import Orchestrator


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


PIPE = {
    "id": "wt",
    "name": "nested",
    "nodes": [{
        "id": "n1",
        "name": "节点",
        "subject": {"goal": "写 a.md"},
        "exit": {"artifacts": [{"path": "a.md"}]},
    }],
    "git_delivery": {"enabled": True, "branch": "gdd/iso"},
}


def test_subdir_is_rejected():
    print("[nested] 仓库子目录不能初始化")
    parent = tempfile.mkdtemp(prefix="gdd-nested-")
    try:
        outer = os.path.join(parent, "repo")
        os.makedirs(outer)
        _git(outer, "init", "-q")
        _git(outer, "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
             "commit", "--allow-empty", "-m", "base")
        before = _git(outer, "symbolic-ref", "--short", "HEAD").stdout.strip()
        inner = os.path.join(outer, "task")
        os.makedirs(inner)
        try:
            Orchestrator(inner).init(dict(PIPE), "req")
            raise AssertionError("子目录初始化应拒绝")
        except RuntimeError as e:
            check("不能在这里初始化" in str(e) and "工作台要放在仓库外面" in str(e),
                  f"说明不要把任务放进仓库（{e}）")
        check(_git(outer, "symbolic-ref", "--short", "HEAD").stdout.strip() == before,
              "外层分支不变")
        check(not os.path.exists(os.path.join(inner, ".gdd")), "不留下 .gdd")
        siblings = os.listdir(parent)
        check(siblings == ["repo"], f"不另开目录（{siblings}）")
    finally:
        import shutil
        shutil.rmtree(parent)


def main():
    test_subdir_is_rejected()
    print("ALL NESTED PASSED")


if __name__ == "__main__":
    main()
