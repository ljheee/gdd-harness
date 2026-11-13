"""按名字登记执行端。resolve_agent 每次新建实例，不挂在 AgentRunner 上。"""
from __future__ import annotations

import os
from typing import Dict, Type

from .base import AgentAdapter

_AGENTS: Dict[str, Type[AgentAdapter]] = {}
_loaded = False


def register(cls: Type[AgentAdapter]) -> Type[AgentAdapter]:
    _AGENTS[cls.name] = cls
    return cls


def unregister(name: str) -> None:
    _AGENTS.pop(name, None)


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from .claude import ClaudeAgent
    from .pi import PiAgent
    register(PiAgent)
    register(ClaudeAgent)


def _names() -> str:
    return " 或 ".join(_AGENTS)


def adapter_for(name: str) -> AgentAdapter:
    """按已知名字取执行端。不看环境变量。"""
    _load()
    from ..runner import ConfigError
    cls = _AGENTS.get(name)
    if cls is None:
        raise ConfigError(f"GDD_AGENT 只能是 {_names()}，当前是 {name!r}")
    return cls()


def resolve_agent() -> AgentAdapter:
    """显式 GDD_AGENT 优先。未设置时用文件名推断，都不认领则是 pi。"""
    _load()
    from ..runner import ConfigError
    raw = (os.environ.get("GDD_AGENT") or "").strip().lower()
    if raw:
        cls = _AGENTS.get(raw)
        if cls is None:
            raise ConfigError(f"GDD_AGENT 只能是 {_names()}，当前是 {raw!r}")
        return cls()
    base = os.path.basename((os.environ.get("GDD_AGENT_BIN") or "").strip()).lower()
    for cls in _AGENTS.values():
        if cls.match_bin(base):
            return cls()
    return _AGENTS["pi"]()
