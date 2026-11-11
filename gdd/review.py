"""代码评审：GDD-REVIEW 哨兵解析、severity 规范化与 review.md 渲染。

评审会话只读，报告由 harness 落盘（见 spec 第 8 节）：
  - 取最后一条合法 `GDD-REVIEW: {...}`；verdict 非法、issues 非列表或
    列表项不是对象时视为不可解析（gate_error，不得默认通过）。
  - severity 规范化为 blocker/major/minor；未知 severity 按 major 处理
    （fail-closed：未知等级不能当成 minor 静默放行）。
  - verdict=fail 或存在 blocker/major 即阻塞；minor 可保留不阻塞。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

SEVERITIES = ("blocker", "major", "minor")
BLOCKING = ("blocker", "major")
_VERDICTS = ("pass", "fail")
_SENTINEL_RE = re.compile(r"^GDD-REVIEW:\s*(\{.*\})\s*$", re.M)


def _normalize_issue(item: Dict[str, Any]) -> Dict[str, Any]:
    severity = str(item.get("severity") or "").strip().lower()
    if severity not in SEVERITIES:
        # 未知 severity 不能降级成 minor（那会把问题静默放行），按 major 阻塞
        severity = "major"
    line = item.get("line")
    if isinstance(line, bool):  # bool 是 int 子类，避免 True 变成行号 1
        line = None
    try:
        line_num = int(line) if line is not None and str(line).strip() != "" else None
    except (TypeError, ValueError):
        line_num = None
    return {
        "severity": severity,
        "file": str(item.get("file") or "").strip(),
        "line": line_num,
        "clause": str(item.get("clause") or "").strip(),
        "problem": str(item.get("problem") or "").strip(),
    }


def parse_review(text: str) -> Optional[Dict[str, Any]]:
    """取最后一条合法 GDD-REVIEW；没有则返回 None（调用方按 gate_error 处理）。"""
    found: Optional[Dict[str, Any]] = None
    for match in _SENTINEL_RE.finditer(text or ""):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        verdict = str(data.get("verdict") or "").strip().lower()
        issues = data.get("issues")
        if verdict not in _VERDICTS or not isinstance(issues, list):
            continue
        # 问题项形态错乱时整条哨兵不可信：宁可 gate_error 重试，也不能丢问题放行
        if any(not isinstance(item, dict) for item in issues):
            continue
        found = {
            "verdict": verdict,
            "issues": [_normalize_issue(item) for item in issues],
        }
    return found


def has_blocking(review: Optional[Dict[str, Any]]) -> bool:
    """不可解析按阻塞处理（fail-closed）；verdict=fail 或 blocker/major 阻塞。"""
    if not review:
        return True
    if str(review.get("verdict") or "").strip().lower() == "fail":
        return True
    return any(i.get("severity") in BLOCKING for i in (review.get("issues") or []))


def format_issue_line(issue: Dict[str, Any]) -> str:
    file_part = issue.get("file") or "(未指明文件)"
    line = issue.get("line")
    loc = f"{file_part}:{line}" if line is not None else file_part
    clause = issue.get("clause") or "(未注明条款)"
    problem = issue.get("problem") or "(未描述问题)"
    return f"[{issue.get('severity')}] {loc} | {clause} | {problem}"


def render_report(review: Dict[str, Any]) -> str:
    """渲染 {{output}}/review.md：评审结论 + 问题清单（无问题写「无」）。"""
    conclusion = "不通过" if has_blocking(review) else "通过"
    issues: List[Dict[str, Any]] = list(review.get("issues") or [])
    lines = ["## 评审结论", conclusion, "", "## 问题清单"]
    if not issues:
        lines.append("无")
    else:
        lines.extend(f"- {format_issue_line(issue)}" for issue in issues)
    return "\n".join(lines) + "\n"


def render_problems(review: Dict[str, Any]) -> str:
    """给 implement 的反馈正文：逐条问题清单。"""
    issues: List[Dict[str, Any]] = list(review.get("issues") or [])
    if not issues:
        return "评审判定不通过，但没有给出具体问题；请重新核对本次实现与 goal/spec/plan。"
    return "\n".join(f"- {format_issue_line(issue)}" for issue in issues)


def write_report(path: str, review: Dict[str, Any]) -> None:
    """原子写 review.md：同目录临时文件 + os.replace，中断不会留下半截报告。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    body = render_report(review)
    fd, tmp = tempfile.mkstemp(prefix=".review-", suffix=".md", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
