"""验收用例与 tasks 的关联解析（行级，不实现完整 Gherkin）。

约定（docs/covenant/acceptance-cases.md）：
- 用例 id 是 `Scenario:` 后的标题（去首尾空白），文件内唯一；
- 每个 Scenario 前至少一行 `@task:<ID>` Gherkin tag，tag 行必须紧邻 Scenario；
- 其他 tag（如 @smoke）允许出现并被忽略；
- 任务 ID 语法与 taskgraph 一致：字母开头，字母数字 _-，最长 32。

解析器只识别行首的 `Scenario:`，不识别 Scenario Outline（与既有契约一致）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

TASK_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_SCENARIO = re.compile(r"^\s*Scenario:\s*(.*)$")
_TAG = re.compile(r"^\s*@")


@dataclass
class Scenario:
    id: str
    tasks: List[str] = field(default_factory=list)
    line: int = 0


@dataclass
class ParseResult:
    scenarios: List[Scenario] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def parse_text(text: str) -> ParseResult:
    """解析 Scenario 与其前的连续 @task tag。

    重复 task tag 去重（保留首次出现顺序）；空 ID 或非法 ID 记为解析错误。
    tag 行必须与 Scenario 行连续：空行或其他语句会切断 pending tag。
    """
    result = ParseResult()
    pending: List[str] = []
    pending_line = 0
    seen_ids = set()
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            pending = []
            continue
        if _TAG.match(line):
            if not pending:
                pending_line = lineno
            for token in line.split():
                if not token.lower().startswith("@task:"):
                    continue
                task_id = token[len("@task:"):].strip()
                if not task_id:
                    result.errors.append(f"第 {lineno} 行 @task: 缺少任务 ID")
                    continue
                if not TASK_ID_RE.fullmatch(task_id):
                    result.errors.append(
                        f"第 {lineno} 行 @task 任务 ID 非法：{task_id!r}"
                        f"（字母开头，仅字母数字_-，最长 32）")
                    continue
                if task_id not in pending:
                    pending.append(task_id)
            continue
        matched = _SCENARIO.match(line)
        if matched:
            title = matched.group(1).strip()
            if not title:
                result.errors.append(f"第 {lineno} 行 Scenario 缺少标题")
                title = f"<第 {lineno} 行>"
            if title in seen_ids:
                result.errors.append(f"Scenario 标题重复：{title}")
            seen_ids.add(title)
            result.scenarios.append(Scenario(title, list(pending), lineno))
            pending = []
            continue
        # 非 tag、非 Scenario 的语句：前面的 tag 不属于任何 Scenario
        pending = []
    return result


def parse_file(path: str) -> ParseResult:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return parse_text(f.read())
    except OSError as e:
        return ParseResult(errors=[f"验收用例文件无法读取：{path}（{e}）"])


def task_map(result: ParseResult) -> dict:
    """用例 id → 任务 ID 列表（保持文件顺序）。"""
    return {scenario.id: list(scenario.tasks) for scenario in result.scenarios}


def validate(result: ParseResult, known_task_ids: Iterable[str]) -> List[str]:
    """对照方案附录任务集合校验关联完整性。返回全部错误（空列表=通过）。"""
    errors = list(result.errors)
    if not result.scenarios and not errors:
        errors.append("验收用例文件没有任何 Scenario")
    known = set(known_task_ids or ())
    for scenario in result.scenarios:
        if not scenario.tasks:
            errors.append(
                f"Scenario「{scenario.id}」（第 {scenario.line} 行）缺少 @task 关联任务")
            continue
        unknown = [tid for tid in scenario.tasks if tid not in known]
        if unknown:
            errors.append(
                f"Scenario「{scenario.id}」引用了附录中不存在的任务 ID：{', '.join(unknown)}")
    return errors


def load_and_validate(acceptance_path: str,
                       known_task_ids: Sequence[str]) -> List[str]:
    if not os.path.isfile(acceptance_path):
        return [f"验收用例文件不存在：{acceptance_path}"]
    return validate(parse_file(acceptance_path), known_task_ids)


def format_errors(errors: List[str]) -> str:
    return "\n".join(f"- {e}" for e in errors)


def case_tasks_from_file(path: str) -> Optional[dict]:
    """只取映射；文件不可读时返回 None（调用方按无法关联处理）。"""
    if not os.path.isfile(path):
        return None
    result = parse_file(path)
    if not result.scenarios:
        return None
    return task_map(result)
