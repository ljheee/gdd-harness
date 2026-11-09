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

from .models import McpSpec, NodeSpec, SkillSpec, _MCP_NAME_RE, _no_inject, _str_map


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


def _within_root(root_real: str, resolved: str) -> bool:
    try:
        return os.path.commonpath([root_real, resolved]) == root_real
    except ValueError:
        return False


def _resolve(root: str, raw: str) -> str:
    """绝对路径原样解析。相对路径解析符号链接后必须仍在项目根内。"""
    root_real = os.path.realpath(os.path.abspath(root))
    if os.path.isabs(raw):
        return os.path.realpath(raw)
    resolved = os.path.realpath(os.path.join(root_real, raw))
    if not _within_root(root_real, resolved):
        raise ValueError(f"路径不能逃出项目根: {raw}")
    return resolved


def _mkdir_private(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path: str, text: str) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _remove_file(path: str) -> None:
    if os.path.isfile(path) or os.path.islink(path):
        os.remove(path)


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


def _assert_skill_links(skill_dir: str) -> None:
    """目录本身在项目内不够：copytree 会跟随符号链接，把目录外的文件拷成普通文件。"""
    root_real = os.path.realpath(skill_dir)
    for dirpath, dirnames, filenames in os.walk(skill_dir, followlinks=False):
        for name in list(dirnames) + list(filenames):
            full = os.path.join(dirpath, name)
            if not os.path.islink(full):
                continue
            resolved = os.path.realpath(full)
            if not _within_root(root_real, resolved):
                rel = os.path.relpath(full, skill_dir)
                raise ValueError(f"skill 内的符号链接不能指向目录外: {rel}")


def _load_mcp_file(path: str) -> Dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"mcp 文件无法读取: {path}（{e}）") from e
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise ValueError(f"mcp 文件缺少 mcpServers: {path}")
    out: Dict[str, dict] = {}
    for name, spec in servers.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            raise ValueError(f"mcp 文件条目非法: {path}")
        out[name] = _file_server(name, spec, path)
    return out


def _file_server(name: str, spec: dict, path: str) -> dict:
    """文件里的 server 与内联条目同一套约束。只写出核对过的字段。"""
    if not _MCP_NAME_RE.fullmatch(name):
        raise ValueError(
            f"非法 MCP 名字: {name!r}（{path}；字母或数字开头，仅字母数字_-）")
    unknown = sorted(k for k in spec if k not in {
        "command", "args", "env", "type", "url", "headers", "timeout"})
    if unknown:
        raise ValueError(f"MCP {name} 含未支持的字段: {', '.join(unknown)}（{path}）")
    command = spec.get("command") or ""
    url = spec.get("url") or ""
    if command and not isinstance(command, str):
        raise ValueError(f"MCP {name} 的 command 必须是字符串（{path}）")
    if url and not isinstance(url, str):
        raise ValueError(f"MCP {name} 的 url 必须是字符串（{path}）")
    if bool(command) == bool(url):
        raise ValueError(f"MCP {name} 必须且只能指定 command 或 url（{path}）")
    side = _file_side_fields(name, spec, path)
    if url:
        if any(c in url for c in "\n\r\x00") or not (
                url.startswith("https://") or url.startswith("http://")):
            raise ValueError(f"MCP {name} 的 url 只接受不含换行的 http(s)（{path}）")
        kind = spec.get("type")
        if kind not in ("http", "sse"):
            raise ValueError(f"MCP {name} 的 url 必须带 type（http 或 sse）（{path}）")
        return {"type": kind, "url": url, **side}
    _no_inject(command, "mcp command")
    return {"command": command, **side}


def _file_side_fields(name: str, spec: dict, path: str) -> dict:
    """command 与 url 两条路径都检查 args、env、headers，避免换行从另一侧漏进去。"""
    out: dict = {}
    if spec.get("args") not in (None, []):
        args = spec.get("args")
        if not isinstance(args, list) or any(
                not isinstance(a, str) or any(c in a for c in "\n\r\x00") for a in args):
            raise ValueError(f"MCP {name} 的 args 必须是不含换行的字符串列表（{path}）")
        out["args"] = list(args)
    if spec.get("env"):
        out["env"] = _str_map(spec.get("env"), "env")
    if spec.get("headers"):
        out["headers"] = _str_map(spec.get("headers"), "headers")
    if "timeout" in spec and spec.get("timeout") is not None:
        timeout = spec["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
            raise ValueError(f"MCP {name} 的 timeout 必须是非负数字（{path}）")
        out["timeout"] = timeout
    return out


def _set_frontmatter_name(text: str, name: str) -> str:
    """把 SKILL.md 开头的 name 改成节点指定的名字。没有 frontmatter 则不动。"""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    lines = text[3:end].split("\n")
    replaced = False
    out = []
    for line in lines:
        if not replaced and line.startswith("name:"):
            out.append(f"name: {name}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"name: {name}")
    return "---" + "\n".join(out) + text[end:]


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
    dest = os.path.join(ext_home(), _project_key(root), node.id)
    cfg = os.path.join(dest, "mcp.json")
    bundle = os.path.join(dest, "skills-plugin")
    if not node.mcp and not node.skills:
        _remove_file(cfg)
        _remove_file(cfg + ".tmp")
        if os.path.isdir(bundle):
            shutil.rmtree(bundle)
        return None
    home = ext_home()
    _mkdir_private(home)
    _mkdir_private(os.path.dirname(dest))
    _mkdir_private(dest)
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
        _write_private(cfg, json.dumps(
            {"mcpServers": servers}, ensure_ascii=False, indent=2))
        ext.mcp_config = cfg
    else:
        _remove_file(cfg)
        _remove_file(cfg + ".tmp")
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
            _assert_skill_links(path)
            name = _skill_name(spec, path, used)
            target = os.path.join(skill_root, name)
            shutil.copytree(path, target)
            if spec.name:
                md = os.path.join(target, "SKILL.md")
                with open(md, encoding="utf-8") as f:
                    text = f.read()
                rewritten = _set_frontmatter_name(text, name)
                if rewritten != text:
                    with open(md, "w", encoding="utf-8") as f:
                        f.write(rewritten)
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
    elif os.path.isdir(bundle):
        shutil.rmtree(bundle)
    if not ext.mcp_config and not ext.plugin_dirs:
        return None
    return ext
