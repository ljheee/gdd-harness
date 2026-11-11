"""内置 pipeline 模板：以 gdd/templates/*.json 文件为唯一权威源。

内置模板即普通模板文件（用户可直接复制、修改、自行设计——
`cp gdd/templates/delivery.json my-delivery.json` 后 `gdd init --pipeline my-delivery.json`）。
init 时实例化：模板内容写入任务 .gdd/pipeline.json 并记录指纹，后续修改
模板文件不影响已实例化任务（fail-closed）。

delivery（id）见 gdd/templates/delivery.json：
 七阶段（survey/clarify/plan/implement(开发验证)/delivery/release_checklist）
+ 项目初始化 + 独立测试节点 = 8 节点；3 个人工准入节点
（clarify/plan/test）；implement 为 baseline_marker；
test 的 exit 为 change_aware；init --name 后产物写入 iterations/<名字>/，
exit 通过后提交到 gdd/<名字>（可用 --branch / --base 改）。
"""
from __future__ import annotations

import json
import os

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _load_builtin(name: str) -> dict:
    """从包内 templates/ 加载内置模板（JSON）。"""
    path = os.path.join(_TEMPLATE_DIR, f"{name}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


try:
    DELIVERY_PIPELINE = _load_builtin("delivery")
except (OSError, json.JSONDecodeError) as e:  # pragma: no cover - 发布打包缺陷兜底
    raise RuntimeError(
        f"内置模板文件缺失或损坏：gdd/templates/delivery.json（{e}）")
