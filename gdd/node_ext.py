"""把节点声明的 MCP / skill 变成一次主体会话的 Claude 参数。

配置写在仓库外。门禁会话不调用 prepare，因此不会继承。
空声明返回 None，命令行不追加任何参数。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import McpSpec, NodeSpec, SkillSpec


@dataclass
class NodeExtensions:
    mcp_config: str = ""
    plugin_dirs: List[str] = field(default_factory=list)


def ext_home() -> str:
    return os.environ.get("GDD_NODE_EXT_HOME") or os.path.join(
        os.path.expanduser("~"), ".gdd", "node-ext")


def _project_key(root: str) -> str:
    real = os.path.realpath(os.path.abspath(root))
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]


def _resolve(root: str, raw: str) -> str:
    if os.path.isabs(raw):
        return os.path.realpath(raw)
    return os.path.realpath(os.path.join(root, raw))


def _skill_name(spec: SkillSpec, path: str, used: set) -> str:
    base = spec.name or os.path.basename(path.rstrip(os.sep)) or "skill"
    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in base).strip("-") or "skill"
    if name[0] == "-" or not name[0].isalnum():
        name = "s" + name
    candidate = name[:64]
    n = 2
    while candidate in used:
        suffix = f"-{n}"
        candidate = name[:64 - len(suffix)] + suffix
        n += 1
    used.add(candidate)
    return candidate


def _is_plugin(path: str) -> bool:
    return os.path.isfile(os.path.join(path, ".claude-plugin", "plugin.json"))


def _load_mcp_file(path: str) -> Dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"mcp 文件无法读取: {path}（{e}）") from e
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise ValueError(f"mcp 文件缺少 mcpServers: {path}")
    for name, spec in servers.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            raise ValueError(f"mcp 文件条目非法: {path}")
    return servers


def _inline_server(spec: McpSpec) -> dict:
    if spec.url:
        out = {"type": "http", "url": spec.url}
        if spec.headers:
            out["headers"] = dict(spec.headers)
        return out
    out = {"command": spec.command}
    if spec.args:
        out["args"] = list(spec.args)
    if spec.env:
        out["env"] = dict(spec.env)
    return out


def prepare(root: str, node: NodeSpec) -> Optional[NodeExtensions]:
    """生成该节点主体会话要用的 mcp 配置和 skill 插件目录。没有声明则返回 None。"""
    if not node.mcp and not node.skills:
        return None
    dest = os.path.join(ext_home(), _project_key(root), node.id)
    os.makedirs(dest, exist_ok=True)
    ext = NodeExtensions()
    if node.mcp:
        servers: Dict[str, dict] = {}
        for spec in node.mcp:
            if spec.path:
                path = _resolve(root, spec.path)
                if not os.path.isfile(path):
                    raise ValueError(f"mcp 文件不存在: {spec.path}")
                for name, body in _load_mcp_file(path).items():
                    if name in servers:
                        raise ValueError(f"MCP 名字重复: {name}")
                    servers[name] = body
                continue
            if spec.name in servers:
                raise ValueError(f"MCP 名字重复: {spec.name}")
            servers[spec.name] = _inline_server(spec)
        cfg = os.path.join(dest, "mcp.json")
        tmp = cfg + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, cfg)
        ext.mcp_config = cfg
    if node.skills:
        bundle = os.path.join(dest, "skills-plugin")
        skill_root = os.path.join(bundle, "skills")
        if os.path.isdir(bundle):
            shutil.rmtree(bundle)
        os.makedirs(skill_root, exist_ok=True)
        used = set()
        staged = False
        for spec in node.skills:
            path = _resolve(root, spec.path)
            if not os.path.isdir(path):
                raise ValueError(f"skill 目录不存在: {spec.path}")
            if _is_plugin(path):
                ext.plugin_dirs.append(path)
                continue
            if not os.path.isfile(os.path.join(path, "SKILL.md")):
                raise ValueError(f"skill 目录缺少 SKILL.md: {spec.path}")
            name = _skill_name(spec, path, used)
            shutil.copytree(path, os.path.join(skill_root, name))
            staged = True
        if staged:
            plugin = {
                "name": f"gdd-{node.id}"[:64],
                "version": "0.0.1",
                "description": f"GDD node {node.id} skills",
                "skills": "./skills",
            }
            manifest = os.path.join(bundle, ".claude-plugin")
            os.makedirs(manifest, exist_ok=True)
            with open(os.path.join(manifest, "plugin.json"), "w", encoding="utf-8") as f:
                json.dump(plugin, f, ensure_ascii=False, indent=2)
            ext.plugin_dirs.insert(0, bundle)
        elif os.path.isdir(bundle):
            shutil.rmtree(bundle, ignore_errors=True)
    if not ext.mcp_config and not ext.plugin_dirs:
        return None
    return ext
