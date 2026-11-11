"""acceptance.feature 的 Scenario 与 @task 关联解析（行级）。"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd import acceptance  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def test_parse_mapping():
    print("[A1-1] 正常解析与去重")
    text = """Feature: 验收
  @smoke @task:T1 @task:T2 @task:T1
  Scenario: 主路径
    Given 就绪
    When 执行
    Then 通过

  @task:T2
  Scenario: 备选路径
    Given 就绪
"""
    result = acceptance.parse_text(text)
    check(not result.errors, "合法文件没有解析错误")
    check([s.id for s in result.scenarios] == ["主路径", "备选路径"], "Scenario 顺序与标题")
    check(result.scenarios[0].tasks == ["T1", "T2"], "重复 task tag 去重且保持顺序")
    check(result.scenarios[1].tasks == ["T2"], "第二个场景的 tag")
    check(acceptance.validate(result, {"T1", "T2"}) == [], "任务 ID 都存在时校验通过")
    print("[A1-1] PASS\n")


def test_missing_and_unknown():
    print("[A1-2] 缺 tag / 未知任务 / 空 ID / 非法 ID")
    result = acceptance.parse_text("""Feature: x
  Scenario: 没有关联
    Given x
  @task:T9
  Scenario: 未知任务
    Given x
  @task:
  Scenario: 空 ID
    Given x
  @task:9bad
  Scenario: 非法 ID
    Given x
""")
    errors = acceptance.validate(result, {"T1"})
    joined = "\n".join(errors)
    check(any("没有关联" in e and "缺少 @task" in e for e in errors), "缺 tag 报场景")
    check(any("未知任务" in e and "T9" in e for e in errors), "未知任务报 ID")
    check("缺少任务 ID" in joined, "空 ID 报错")
    check("任务 ID 非法" in joined, "非法 ID 报错")
    print("[A1-2] PASS\n")


def test_no_scenario_and_duplicate_title():
    print("[A1-3] 没有 Scenario / 标题重复")
    empty = acceptance.parse_text("Feature: x\n  Given 没有场景\n")
    check("没有任何 Scenario" in "\n".join(acceptance.validate(empty, set())), "无场景被拒绝")
    dup = acceptance.parse_text("""Feature: x
  @task:T1
  Scenario: 同名
    Given x
  @task:T1
  Scenario: 同名
    Given y
""")
    check(any("标题重复" in e for e in dup.errors), "重复标题报错")
    print("[A1-3] PASS\n")


def test_file_roundtrip():
    print("[A1-4] 文件读取与缺失文件")
    d = tempfile.mkdtemp(prefix="gdd-accept-")
    try:
        path = os.path.join(d, "acceptance.feature")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Feature: x\n  @task:T1\n  Scenario: A\n    Given x\n")
        check(acceptance.case_tasks_from_file(path) == {"A": ["T1"]}, "文件映射")
        check(acceptance.case_tasks_from_file(os.path.join(d, "none")) is None,
              "缺失文件返回 None")
        check(acceptance.load_and_validate(os.path.join(d, "none"), ["T1"]),
              "缺失文件校验报错")
    finally:
        import shutil
        shutil.rmtree(d)
    print("[A1-4] PASS\n")


def main():
    test_parse_mapping()
    test_missing_and_unknown()
    test_no_scenario_and_duplicate_title()
    test_file_roundtrip()
    print("ALL ACCEPTANCE PASSED")


if __name__ == "__main__":
    main()
