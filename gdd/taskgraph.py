"""方案附录里的任务依赖。

附录按拓扑关系拆开任务。开发节点只并行那些没有依赖、或依赖都已完成的任务。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

SECTION = "## 附录：任务依赖"
_ITEM = re.compile(
    r"^[-*]\s+([A-Za-z][A-Za-z0-9_-]{0,31})\s*\|\s*([^|]*)\|\s*(\S.*?)\s*$"
)
_NONE = {"", "-", "无", "无依赖", "none"}


@dataclass
class Task:
    id: str
    deps: List[str]
    title: str


@dataclass
class TaskGraph:
    tasks: List[Task] = field(default_factory=list)
    error: str = ""

    def ready(self, done: Set[str]) -> List[Task]:
        """没有依赖，或依赖都已完成，且自己还没完成的任务。"""
        if self.error:
            return []
        out = []
        for task in self.tasks:
            if task.id in done:
                continue
            if all(dep in done for dep in task.deps):
                out.append(task)
        return out


def load_graph(path: str) -> Optional[TaskGraph]:
    """没有附录时返回 None，调用方按原来的单会话开发。附录不合法时带回 error。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    if SECTION not in text:
        return None
    return parse(text)


def parse(text: str) -> TaskGraph:
    body = _section(text)
    tasks: List[Task] = []
    seen = set()
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        matched = _ITEM.match(line)
        if not matched:
            return TaskGraph(error=f"任务依赖行无法解析：{line[:120]}")
        task_id, dep_raw, title = matched.group(1), matched.group(2).strip(), matched.group(3).strip()
        if task_id in seen:
            return TaskGraph(error=f"任务 {task_id} 重复")
        deps = _deps(dep_raw)
        if deps is None:
            return TaskGraph(error=f"任务 {task_id} 的依赖无法解析：{dep_raw[:80]}")
        if task_id in deps:
            return TaskGraph(error=f"任务 {task_id} 不能依赖自己")
        seen.add(task_id)
        tasks.append(Task(task_id, deps, title))
    if not tasks:
        return TaskGraph(error="附录「任务依赖」里没有任务")
    known = {task.id for task in tasks}
    for task in tasks:
        missing = [dep for dep in task.deps if dep not in known]
        if missing:
            return TaskGraph(error=f"任务 {task.id} 依赖了附录里没有的任务：{', '.join(missing)}")
    cycle = _cycle(tasks)
    if cycle:
        return TaskGraph(error=f"任务依赖成环：{' → '.join(cycle)}")
    return TaskGraph(tasks=tasks)


def _section(text: str) -> str:
    start = text.find(SECTION)
    if start < 0:
        return ""
    rest = text[start + len(SECTION):]
    nxt = re.search(r"\n##\s+", rest)
    if nxt:
        rest = rest[:nxt.start()]
    return rest


def _deps(raw: str) -> Optional[List[str]]:
    if raw.strip() in _NONE:
        return []
    parts = re.split(r"[,，]\s*", raw.strip())
    out = []
    for part in parts:
        part = part.strip()
        if part in _NONE:
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", part):
            return None
        if part not in out:
            out.append(part)
    return out


def _cycle(tasks: List[Task]) -> List[str]:
    """Kahn 拓扑。有环时返回其中一条环上的 id。"""
    pending = {task.id: set(task.deps) for task in tasks}
    ordered = []
    while True:
        ready = sorted(tid for tid, deps in pending.items() if not deps)
        if not ready:
            break
        for tid in ready:
            ordered.append(tid)
            pending.pop(tid)
            for deps in pending.values():
                deps.discard(tid)
    if not pending:
        return []
    start = sorted(pending)[0]
    path = [start]
    seen = {start}
    cursor = start
    while True:
        nxt = sorted(pending[cursor])
        if not nxt:
            return path
        cursor = nxt[0]
        if cursor in seen:
            return path[path.index(cursor):] + [cursor]
        seen.add(cursor)
        path.append(cursor)
