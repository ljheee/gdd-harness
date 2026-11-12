"""代码仓库的长期克隆，以及每个任务的 worktree。

克隆放在代码仓库目录，检出放在任务目录的 worktrees/<仓库名>/。
长期克隆停在基线分支。任务分支只在 worktree 里检出。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

_NAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff]{0,63}$")


def _run(args, cwd=None):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)


def repo_dirname(url: str) -> str:
    raw = (url or "").strip()
    if not raw or any(c.isspace() or ord(c) < 0x20 for c in raw) or ".." in raw.split("/"):
        raise ValueError(f"仓库地址不可用: {url!r}")
    path = raw.split("://", 1)[-1]
    if "://" not in raw and ":" in path:
        path = path.split(":", 1)[1]
    name = path.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"仓库名不可用: {name!r}（来自 {url!r}）")
    return name


def _is_git(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git"))


def _has_ref(clone: str, ref: str) -> bool:
    return _run(["git", "rev-parse", "--verify", ref], cwd=clone).returncode == 0


def resolve_baseline(clone: str, requested: str = "") -> str:
    """填写的基线优先。不填则用 master，没有 master 再用 main。"""
    asked = (requested or "").strip()
    if asked:
        if asked.startswith("-") or ".." in asked or ":" in asked or any(
                c.isspace() or ord(c) < 0x20 for c in asked):
            raise ValueError(f"基线分支不可用: {asked!r}")
        return asked
    for name in ("master", "main"):
        if _has_ref(clone, f"refs/remotes/origin/{name}") or _has_ref(clone, f"refs/heads/{name}"):
            return name
    raise ValueError("找不到 master 或 main，请填写基线分支")


def _sync_baseline(clone: str, base: str) -> str:
    """把基线 fetch 到最新。克隆已经停在这条分支上才快进。

    停在别的分支时不切走，返回 origin/<基线> 给 worktree 使用。
    """
    fetched = _run(["git", "fetch", "origin", base], cwd=clone)
    if fetched.returncode != 0:
        raise ValueError(f"fetch {base} 失败：{(fetched.stderr or fetched.stdout).strip()[:200]}")
    remote = f"origin/{base}"
    has_remote = _has_ref(clone, f"refs/remotes/{remote}")
    has_local = _has_ref(clone, f"refs/heads/{base}")
    if not has_remote and not has_local:
        raise ValueError(f"远端没有基线分支 {base}")
    current = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd=clone)
    current_name = current.stdout.strip() if current.returncode == 0 else ""
    if current_name == base and has_remote:
        ff = _run(["git", "merge", "--ff-only", remote], cwd=clone)
        if ff.returncode != 0:
            raise ValueError(f"基线 {base} 不能快进到远端：{(ff.stderr or '').strip()[:200]}")
        return base
    if current_name == base:
        return base
    if not has_remote:
        return base
    return remote


def _add_worktree(clone: str, dest: str, branch: str, base: str) -> bool:
    """创建检出。返回值表示分支是这次新建的。"""
    if os.path.exists(dest):
        raise ValueError(f"worktree 已存在：{dest}")
    existed = _has_ref(clone, f"refs/heads/{branch}")
    if existed:
        made = _run(["git", "worktree", "add", dest, branch], cwd=clone)
    else:
        made = _run(["git", "worktree", "add", "-b", branch, dest, base], cwd=clone)
    if made.returncode != 0:
        if not existed and _has_ref(clone, f"refs/heads/{branch}"):
            _run(["git", "branch", "-D", branch], cwd=clone)
        raise ValueError(f"创建 worktree 失败：{(made.stderr or made.stdout).strip()[:300]}")
    return not existed


def release_repos(records: list) -> None:
    """撤掉本次新建的检出、分支和克隆。调用前已经存在的分支保留。"""
    for rec in reversed(records or []):
        clone = rec.get("clone") or ""
        dest = rec.get("worktree") or ""
        if clone and dest:
            _run(["git", "worktree", "remove", "--force", dest], cwd=clone)
        if dest and os.path.isdir(dest) and not os.path.islink(dest):
            shutil.rmtree(dest, ignore_errors=True)
        branch = rec.get("branch") or ""
        if rec.get("created_branch") and clone and branch and _is_git(clone):
            _run(["git", "branch", "-D", branch], cwd=clone)
        prev = rec.get("prev_branch") or ""
        if clone and prev and _is_git(clone):
            _run(["git", "checkout", prev], cwd=clone)
        if rec.get("created_clone") and clone and os.path.isdir(clone):
            shutil.rmtree(clone, ignore_errors=True)


def prepare_repos(repos_dir: str, task_dir: str, task_name: str, specs: list,
                  branch: str = "") -> list:
    """准备每个仓库的克隆和本任务的 worktree。失败时撤掉本次新建的检出。"""
    if not specs:
        raise ValueError("请至少填写一个仓库地址")
    delivery = (branch or f"gdd/{task_name}").strip()
    if (not delivery or delivery.startswith("-") or ".." in delivery or ":" in delivery
            or any(c.isspace() or ord(c) < 0x20 for c in delivery)):
        raise ValueError(f"分支名不可用: {delivery!r}")
    repos_dir = os.path.abspath(repos_dir)
    task_dir = os.path.abspath(task_dir)
    os.makedirs(repos_dir, exist_ok=True)
    os.makedirs(os.path.join(task_dir, "worktrees"), exist_ok=True)
    records = []
    new_clones = []
    seen = set()
    try:
        for spec in specs:
            url = str(spec.get("url") or "").strip()
            name = repo_dirname(url)
            if name in seen:
                raise ValueError(f"仓库名重复：{name}")
            seen.add(name)
            if (os.path.realpath(repos_dir) == os.path.realpath(os.path.dirname(task_dir))
                    and name == os.path.basename(task_dir)):
                raise ValueError(f"任务名不能与仓库目录同名（{name}）")
            clone = os.path.join(repos_dir, name)
            if os.path.isfile(os.path.join(clone, ".gdd", "state.json")):
                raise ValueError(f"{clone} 是任务目录，不能当作长期克隆")
            created_clone = False
            prev_branch = ""
            if not _is_git(clone):
                if os.path.exists(clone):
                    raise ValueError(f"代码目录已存在但不是 git 仓库：{clone}")
                cloned = _run(["git", "clone", url, clone])
                if cloned.returncode != 0:
                    raise ValueError(f"clone 失败：{(cloned.stderr or cloned.stdout).strip()[:300]}")
                new_clones.append(clone)
                created_clone = True
            else:
                cur = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd=clone)
                prev_branch = cur.stdout.strip() if cur.returncode == 0 else ""
            base = resolve_baseline(clone, str(spec.get("base") or ""))
            start = _sync_baseline(clone, base)
            moved = ""
            if prev_branch and start == base and prev_branch != base:
                moved = prev_branch
            dest = os.path.join(task_dir, "worktrees", name)
            wt_root = os.path.join(task_dir, "worktrees")
            if os.path.islink(wt_root) or os.path.islink(dest):
                raise ValueError("worktrees 不能是符号链接")
            clone_real = os.path.realpath(clone)
            dest_real = os.path.realpath(dest)
            task_real = os.path.realpath(task_dir)
            if not dest_real.startswith(task_real + os.sep):
                raise ValueError(f"worktree 必须留在任务目录里：{dest}")
            if dest_real == clone_real or dest_real.startswith(clone_real + os.sep):
                raise ValueError(f"worktree 不能放在克隆里面：{dest}")
            created_branch = _add_worktree(clone, dest, delivery, start)
            records.append({
                "name": name,
                "url": url,
                "base": base,
                "branch": delivery,
                "clone": clone,
                "worktree": dest,
                "created_clone": created_clone,
                "created_branch": created_branch,
                "prev_branch": moved,
            })
        return records
    except Exception:
        release_repos(records)
        for clone in new_clones:
            if os.path.isdir(clone):
                shutil.rmtree(clone, ignore_errors=True)
        raise
