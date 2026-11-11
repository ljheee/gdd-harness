"""代码评审解析与报告渲染（R2 / D19 单元层）。

运行：python3 tests/test_review.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd.review import (  # noqa: E402
    format_issue_line, has_blocking, parse_review, render_problems,
    render_report, write_report,
)


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def _sentinel(verdict="pass", issues=None):
    return "GDD-REVIEW: " + json.dumps(
        {"verdict": verdict, "issues": issues or []}, ensure_ascii=False)


def test_parse_valid():
    print("[R2-1] 合法哨兵解析")
    text = ("评审说明……\n"
            + _sentinel("pass", [{"severity": "minor", "file": "gdd/x.py",
                                  "line": 12, "clause": "spec §3",
                                  "problem": "命名可读性"}]))
    r = parse_review(text)
    check(r is not None, "解析成功")
    check(r["verdict"] == "pass", "verdict 保留")
    issue = r["issues"][0]
    check(issue["severity"] == "minor" and issue["file"] == "gdd/x.py"
          and issue["line"] == 12 and issue["clause"] == "spec §3",
          "问题字段保留")
    check(not has_blocking(r), "pass + minor 不阻塞")

    r2 = parse_review(_sentinel("fail", [
        {"severity": "blocker", "file": "a.py", "line": 1,
         "clause": "goal G1", "problem": "没实现"}]))
    check(has_blocking(r2), "fail/blocker 阻塞")


def test_parse_missing_and_invalid():
    print("[R2-2] 缺失 / 非法形态按不可解析")
    check(parse_review("") is None, "空文本不可解析")
    check(parse_review("没有哨兵") is None, "缺哨兵不可解析")
    check(parse_review("GDD-REVIEW: {not json}") is None, "非法 JSON 不可解析")
    check(parse_review('GDD-REVIEW: {"verdict":"maybe","issues":[]}') is None,
          "非法 verdict 不可解析")
    check(parse_review('GDD-REVIEW: {"verdict":"pass","issues":{}}') is None,
          "issues 非列表不可解析")
    check(parse_review('GDD-REVIEW: {"verdict":"pass","issues":[1]}') is None,
          "问题项非对象不可解析")
    check(has_blocking(None), "不可解析按阻塞处理（fail-closed）")


def test_last_valid_sentinel():
    print("[R2-3] 多哨兵取最后一条合法")
    text = "\n".join([
        _sentinel("fail", [{"severity": "blocker", "file": "a", "line": 1,
                            "clause": "c", "problem": "旧结论"}]),
        "中间说明",
        "GDD-REVIEW: {broken json}",
        _sentinel("pass", []),
    ])
    r = parse_review(text)
    check(r is not None and r["verdict"] == "pass" and r["issues"] == [],
          "最后一条合法哨兵胜出")
    check(not has_blocking(r), "后续 pass 覆盖旧 fail")


def test_severity_normalization():
    print("[R2-4] severity 规范化")
    r = parse_review(_sentinel("pass", [
        {"severity": "CRITICAL", "file": "a", "line": 2,
         "clause": "c", "problem": "未知等级"},
        {"severity": "minor", "file": "b", "line": None,
         "clause": "c", "problem": "提示"},
    ]))
    check(r["issues"][0]["severity"] == "major", "未知 severity 按 major（不静默放行）")
    check(has_blocking(r), "未知 severity 阻塞")
    check(r["issues"][1]["severity"] == "minor" and r["issues"][1]["line"] is None,
          "minor/空行号保留")

    r2 = parse_review(_sentinel("pass", [
        {"severity": "blocker", "file": "a", "line": "7", "clause": "c", "problem": "x"},
        {"severity": "major", "file": "b", "line": 8, "clause": "c", "problem": "y"},
        {"severity": "minor", "file": "c", "line": 9, "clause": "c", "problem": "z"},
    ]))
    check(has_blocking(r2), "blocker/major 组合阻塞")
    check(parse_review(_sentinel("pass", [
        {"severity": "minor", "file": "c", "line": 9, "clause": "c", "problem": "z"}]))
        is not None and not has_blocking(parse_review(_sentinel("pass", [
            {"severity": "minor", "file": "c", "line": 9,
             "clause": "c", "problem": "z"}]))),
        "仅 minor 不阻塞")
    check(r2["issues"][0]["line"] == 7, "字符串行号转整数")


def test_render_and_write():
    print("[R2-5] review.md 渲染与原子落盘")
    passed = parse_review(_sentinel("pass", []))
    body = render_report(passed)
    check("## 评审结论" in body and "通过" in body, "通过结论")
    check("## 问题清单" in body and "\n无\n" in body, "无问题写「无」")
    check(body.index("## 评审结论") < body.index("## 问题清单"), "章节顺序")

    blocked = parse_review(_sentinel("fail", [
        {"severity": "blocker", "file": "gdd/x.py", "line": 3,
         "clause": "goal G2", "problem": "缺少校验"},
        {"severity": "minor", "file": "gdd/y.py", "line": None,
         "clause": "plan R1", "problem": "命名"},
    ]))
    body = render_report(blocked)
    check("不通过" in body, "不通过结论")
    check("- [blocker] gdd/x.py:3 | goal G2 | 缺少校验" in body,
          "问题行含 severity/文件/行号/条款/描述")
    check("- [minor] gdd/y.py | plan R1 | 命名" in body, "无行号时省略冒号")
    check("缺少校验" in render_problems(blocked), "反馈正文复用问题清单")
    check(format_issue_line(blocked["issues"][0]).startswith("[blocker]"),
          "单行格式化")

    d = tempfile.mkdtemp(prefix="gdd-review-")
    try:
        path = os.path.join(d, "out", "review.md")
        write_report(path, blocked)
        check(os.path.isfile(path), "报告落盘")
        with open(path, encoding="utf-8") as f:
            saved = f.read()
        check(saved == body, "落盘内容与渲染一致")
        leftovers = [n for n in os.listdir(os.path.dirname(path)) if n.startswith(".review-")]
        check(not leftovers, "临时文件不残留")
    finally:
        shutil.rmtree(d)


def main():
    test_parse_valid()
    test_parse_missing_and_invalid()
    test_last_valid_sentinel()
    test_severity_normalization()
    test_render_and_write()
    print("ALL REVIEW TESTS PASSED")


if __name__ == "__main__":
    main()
