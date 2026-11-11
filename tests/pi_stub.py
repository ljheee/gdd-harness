#!/usr/bin/env python3
"""pi CLI 测试替身：模拟 `pi --print --mode json` 的 JSONL 事件流。

用法（与真实 pi 相同的接线方式）：
  GDD_AGENT_BIN=python3
  GDD_AGENT_PRE_ARGS=<repo>/tests/pi_stub.py
  GDD_PI_STUB_SCRIPT=<json 文件>

脚本文件是列表，按出现顺序取首个命中 prompt 的条目：
  {"patterns": ["关键词"], "text": "最终文本", "is_error": false,
   "tools": [{"name": "read", "args": {"path": "x"}}],
   "turns": 1, "session_id": "可选", "stop_reason": "stop|error|aborted",
   "error": "错误文本", "exit_code": 0}

设置 GDD_PI_STUB_RECORD 时，每次调用追加一行 JSON（argv/prompt/部分环境），
供测试断言命令参数与扩展加载情况。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys


def _arg_value(args, name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
    return ""


def _load_script():
    path = os.environ.get("GDD_PI_STUB_SCRIPT")
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _consume_limit(script_path, index, max_hits):
    """max_hits 条目：用仓库内计数文件限制命中次数（跨多次进程调用有效）。"""
    base = os.environ.get("GDD_PI_STUB_COUNTER_DIR") or os.path.join(
        os.getcwd(), ".gdd", "pi-stub-counters")
    try:
        os.makedirs(base, exist_ok=True)
        key = hashlib.sha256(
            f"{os.path.abspath(script_path)}#{index}".encode()).hexdigest()[:16]
        path = os.path.join(base, key + ".count")
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read().strip()
                try:
                    n = int(raw or 0)
                except ValueError:
                    n = 0
                if n >= max_hits:
                    return False
                f.seek(0)
                f.truncate()
                f.write(str(n + 1))
                f.flush()
                return True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        return True


def _pick(script, prompt, script_path=""):
    for index, entry in enumerate(script):
        if not isinstance(entry, dict):
            continue
        for pat in entry.get("patterns") or []:
            if pat and pat in prompt:
                limit = entry.get("max_hits")
                if isinstance(limit, int) and limit >= 0:
                    if not _consume_limit(script_path, index, limit):
                        break  # 该条已用尽，继续找下一条匹配
                return entry
    return {}


def _write_files(entry):
    files = entry.get("write_files")
    if not isinstance(files, dict):
        return
    for rel, content in files.items():
        if not isinstance(rel, str) or not rel or os.path.isabs(rel) or ".." in rel.split("/"):
            continue
        path = os.path.join(os.getcwd(), rel)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(content))


def _record(args, prompt):
    path = os.environ.get("GDD_PI_STUB_RECORD")
    if not path:
        return
    item = {
        "argv": list(sys.argv[1:]),
        "prompt": prompt,
        "cwd": os.getcwd(),
        "bin": os.environ.get("GDD_AGENT_BIN", ""),
        "agent_dir": os.environ.get("PI_CODING_AGENT_DIR", ""),
        "session_dir": os.environ.get("PI_CODING_AGENT_SESSION_DIR", ""),
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main() -> int:
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    _record(args, prompt)
    script_path = os.environ.get("GDD_PI_STUB_SCRIPT", "")
    entry = _pick(_load_script(), prompt, script_path)
    _write_files(entry)

    sid = _arg_value(args, "--session-id") or entry.get("session_id") or "pi-stub-session"
    text = str(entry.get("text") or "")
    is_error = bool(entry.get("is_error", False))
    stop_reason = str(entry.get("stop_reason") or ("error" if is_error else "stop"))
    tools = entry.get("tools") if isinstance(entry.get("tools"), list) else []
    turns = entry.get("turns")
    try:
        turns = max(1, int(turns if turns is not None else 1))
    except (TypeError, ValueError):
        turns = 1

    _emit({"type": "session", "version": 3, "id": sid,
           "timestamp": "2026-01-01T00:00:00.000Z", "cwd": os.getcwd()})
    _emit({"type": "agent_start"})
    _emit({"type": "turn_start"})
    _emit({"type": "message_start",
           "message": {"role": "user", "content": prompt, "timestamp": 0}})
    _emit({"type": "message_end",
           "message": {"role": "user", "content": prompt, "timestamp": 0}})
    _emit({"type": "message_start",
           "message": {"role": "assistant", "content": [], "stopReason": "pending",
                       "timestamp": 0}})
    chunks = []
    if text:
        step = max(1, len(text) // 3)
        chunks = [text[i:i + step] for i in range(0, len(text), step)]
    for chunk in chunks:
        _emit({"type": "message_update", "usage": {},
               "assistantMessageEvent": {"type": "text_delta",
                                         "contentIndex": 0, "delta": chunk}})
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        _emit({"type": "tool_execution_start",
               "toolCallId": f"tool-{i}",
               "toolName": tool.get("name", "read"),
               "args": tool.get("args") or {}})
        _emit({"type": "tool_execution_end",
               "toolCallId": f"tool-{i}",
               "toolName": tool.get("name", "read"),
               "isError": False})
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}] if text else [],
        "stopReason": stop_reason,
        "timestamp": 0,
        "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0,
                  "totalTokens": 2,
                  "cost": {"input": 0, "output": 0, "cacheRead": 0,
                           "cacheWrite": 0, "total": 0}},
    }
    if entry.get("error"):
        message["errorMessage"] = str(entry.get("error"))
    if entry.get("aborted"):
        message["stopReason"] = "aborted"
    _emit({"type": "message_end", "message": message})
    for _ in range(turns):
        _emit({"type": "turn_end", "message": message, "toolResults": []})
    _emit({"type": "agent_end", "messages": [message],
           "willRetry": stop_reason == "error"})
    _emit({"type": "agent_settled", "aborted": bool(entry.get("aborted"))})
    return int(entry.get("exit_code", 0) or 0)


if __name__ == "__main__":
    sys.exit(main())
