"""一次迭代的产物目录和交付分支。

内置交付流水线用 {{output}}、{{name}} 占位。出现 {{output}} 时，产物目录绑定成
iterations/<名字>/。{{name}} 换成这次的名字，交付分支默认 gdd/<名字>。
只出现 {{name}} 时不改产物路径。
同一次任务中断后靠已有 .gdd 续跑；目录里已经有文件则拒绝同名再初始化。
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List

_NAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff]{0,63}$")


def needs_iteration(pipeline: Dict) -> bool:
    blob = json.dumps(pipeline, ensure_ascii=False)
    return "{{output}}" in blob or "{{name}}" in blob


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            "迭代名须以字母、数字或汉字开头，只含字母、数字、汉字、_、-，最长 64")
    return name


def validate_branch(branch: str) -> str:
    branch = (branch or "").strip()
    if (not branch or branch.startswith("-") or ".." in branch or ":" in branch
            or any(c.isspace() or ord(c) < 0x20 for c in branch)):
        raise ValueError(f"分支名不可用: {branch!r}")
    return branch


def output_dir(name: str) -> str:
    return f"iterations/{name}"


def default_branch(name: str) -> str:
    return f"gdd/{name}"


def directory_occupied(root: str, name: str) -> bool:
    base = os.path.join(root, "iterations", name)
    if not os.path.isdir(base):
        return False
    for _dirpath, _dirnames, filenames in os.walk(base):
        if filenames:
            return True
    return False


def prior_contracts(root: str, name: str) -> List[str]:
    """其他迭代里已经写下的规格和决策，供这次只读对照。"""
    base = os.path.join(root, "iterations")
    found: List[str] = []
    if not os.path.isdir(base):
        return found
    for entry in sorted(os.listdir(base)):
        if entry == name or entry.startswith("."):
            continue
        for leaf in ("spec.md", "decisions.md"):
            rel = f"iterations/{entry}/{leaf}"
            if os.path.isfile(os.path.join(root, rel)):
                found.append(rel)
    return found


def prepare(pipeline: Dict, root: str, name: str = "", branch: str = "",
            base: str = "") -> Dict:
    """绑定占位符。无占位符的自定义流水线保持原路径。"""
    data = json.loads(json.dumps(pipeline))
    name = (name or "").strip()
    branch = (branch or "").strip()
    base = (base or "").strip()
    if name:
        name = validate_name(name)
    if branch:
        branch = validate_branch(branch)
    if base:
        base = validate_branch(base)
    if not needs_iteration(data):
        if branch and isinstance(data.get("git_delivery"), dict):
            data["git_delivery"]["branch"] = branch
        if name or branch or base:
            data["iteration"] = {
                "name": name,
                "output": "",
                "branch": branch,
                "base": base,
                "prior": [],
            }
        return data
    if not name:
        raise ValueError("这条流水线按迭代落产物，初始化必须提供 --name")
    bind_output = "{{output}}" in json.dumps(data, ensure_ascii=False)
    if bind_output and directory_occupied(root, name):
        raise ValueError(
            f"iterations/{name} 已有文件，这个名字属于已有迭代。"
            f"换一个名字，或删掉该目录后再初始化")
    out = output_dir(name) if bind_output else ""
    resolved = branch or default_branch(name)
    validate_branch(resolved)
    text = json.dumps(data, ensure_ascii=False)
    text = text.replace("{{output}}", out).replace("{{name}}", name)
    data = json.loads(text)
    gd = data.get("git_delivery")
    if not isinstance(gd, dict):
        data["git_delivery"] = {"enabled": True, "branch": resolved}
    else:
        data["git_delivery"]["branch"] = resolved
    data["iteration"] = {
        "name": name,
        "output": out,
        "branch": resolved,
        "base": base,
        "prior": prior_contracts(root, name) if bind_output else [],
    }
    return data
