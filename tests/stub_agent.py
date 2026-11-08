"""Stub agent：按 prompt 关键词路由的假 CLI，用于全流程无模型实测。

行为由环境变量驱动：
  GDD_STUB_SCRIPT  一个 JSON 文件，描述各调用的应答（见 tests/stub_*.json）
每次调用读取 stdin 完整 prompt，匹配 script 中 patterns（按出现顺序首个命中），
输出 claude -p --output-format json 兼容的 result JSON。

若 argv 含 --output-format stream-json：先发若干 NDJSON 事件（text_delta / tool_use），
再发末行 type=result——覆盖 P0/P1 流式路径单测。
"""
from __future__ import annotations

import json
import os
import sys


def _wants_stream() -> bool:
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--output-format" and i + 1 < len(args) and args[i + 1] == "stream-json":
            return True
        if a.startswith("--output-format=") and a.split("=", 1)[1] == "stream-json":
            return True
    return False


def _emit_stream(text: str, is_error: bool, entry: dict, subtype: str) -> None:
    """最小 stream-json：text_delta 分片 + 可选 tool_use + result。"""
    sid = "stub-session"
    # 分片文本模拟 token 流
    chunks = []
    if text:
        step = max(1, len(text) // 3)
        for i in range(0, len(text), step):
            chunks.append(text[i:i + step])
    else:
        chunks = [""]
    for c in chunks:
        print(json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_delta",
                      "delta": {"type": "text_delta", "text": c}},
            "session_id": sid,
        }, ensure_ascii=False), flush=True)
    # 可选：script 条目带 stream_tools → 发 assistant tool_use（测 P1 摘要）
    tools = entry.get("stream_tools") if entry else None
    if isinstance(tools, list):
        blocks = []
        for i, t in enumerate(tools):
            if not isinstance(t, dict):
                continue
            blocks.append({
                "type": "tool_use",
                "id": f"tool_{i}",
                "name": t.get("name", "Write"),
                "input": t.get("input") or {"file_path": t.get("path", "out.md")},
            })
        if blocks:
            print(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": blocks},
                "session_id": sid,
            }, ensure_ascii=False), flush=True)
    print(json.dumps({
        "type": "result",
        "subtype": subtype,
        "result": text,
        "is_error": is_error,
        "session_id": sid,
        "num_turns": 2,
        "total_cost_usd": 0.001,
    }, ensure_ascii=False), flush=True)


def main() -> int:
    prompt = sys.stdin.read()
    script_path = os.environ.get("GDD_STUB_SCRIPT")
    if not script_path:
        print(json.dumps({"result": "stub: no GDD_STUB_SCRIPT", "is_error": True, "session_id": "stub", "num_turns": 1, "total_cost_usd": 0.0}))
        return 0
    with open(script_path, "r", encoding="utf-8") as f:
        script = json.load(f)

    text, is_error = "stub: no pattern matched", True
    entry = {}  # script 为空/无命中时保持定义，防下方引用 NameError
    for entry in script:
        for pat in entry.get("patterns", []):
            if pat in prompt:
                text, is_error = entry.get("text", ""), bool(entry.get("is_error", False))
                break
        else:
            continue
        break

    hit = entry if entry and _matched(entry, prompt) else {}
    subtype = (hit.get("subtype") if hit
               else ("success" if not is_error else "error"))
    if _wants_stream():
        _emit_stream(text, is_error, hit if isinstance(hit, dict) else {}, subtype)
        return 0

    print(json.dumps({
        "type": "result",
        # 条目可显式指定 subtype（如 "error_max_turns" 模拟 turns 打满）；
        # 仅在命中时生效；错误缺省为通用 error——真实 CLI 只有打满才报 error_max_turns
        "subtype": subtype,
        "result": text,
        "is_error": is_error,
        "session_id": "stub-session",
        "num_turns": 2,
        "total_cost_usd": 0.001,
    }, ensure_ascii=False))
    return 0


def _matched(entry, prompt) -> bool:
    for pat in entry.get("patterns", []):
        if pat in prompt:
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
