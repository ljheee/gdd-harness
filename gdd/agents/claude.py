"""Claude 执行端：-p stream-json、--resume、--mcp-config、临时插件。"""
from __future__ import annotations

import json
import os
import shlex
from typing import Any, Callable, Dict, List, Optional

from ..runner import (
    SessionResult,
    _auth_hint,
    _token_status,
    _tool_summary,
    _usage_int,
)
from .base import AgentAdapter

_CLAUDE_LOGIN_HINTS = (
    "not logged in", "please run /login", "/login",
)
# 门禁、评审、对抗会话只声明其中一部分。跳过权限检查后，未声明的写工具仍可用，
# 所以按这张名单把没交给本次会话的工具放进 --disallowedTools。
_HARNESS_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "Grep", "Glob", "Bash")


def _claude_login_hint(*texts: str) -> str:
    for text in texts:
        low = (text or "").lower()
        for hint in _CLAUDE_LOGIN_HINTS:
            if hint in low:
                return hint
    return ""


def _claude_auth_error(hint: str) -> str:
    return (f"claude 登录态不可用（命中特征 '{hint}'）。"
            f"请先在本机完成 claude 登录，然后 gdd retry 续跑。")


def _is_result(data: dict) -> bool:
    return data.get("type") == "result"


def _result_text(data: dict) -> str:
    """成功结果在 result 字符串；error_max_turns 等只有 errors 数组。"""
    result = data.get("result")
    if isinstance(result, str) and result.strip():
        return result
    errors = data.get("errors")
    if isinstance(errors, list):
        parts = [item for item in errors if isinstance(item, str) and item.strip()]
        if parts:
            return "\n".join(parts)
    if isinstance(result, str):
        return result
    return ""


_USAGE_KEYS = (
    ("input_tokens", "token_input"),
    ("output_tokens", "token_output"),
    ("cache_creation_input_tokens", "token_cache_creation"),
    ("cache_read_input_tokens", "token_cache_read"),
)


def _collect_metrics(events: List[dict]) -> Dict[str, int]:
    """从完整 stream-json 事件流统计工具/LLM 次数与 message_delta usage。

    - LLM 次数：唯一 assistant message id；无 id 时每次 assistant 事件各计 1。
    - 工具调用：tool_use id 去重（重复流式事件只算一次）；无 id 时每次各计 1。
    - usage 兜底：逐条 stream_event.message_delta.usage 四项累加。
    """
    seen_messages: set = set()
    seen_tools: set = set()
    metrics = {"tool_calls": 0, "llm_calls": 0,
               "token_input": 0, "token_output": 0,
               "token_cache_creation": 0, "token_cache_read": 0}
    for parsed in events:
        kind = parsed.get("type")
        if kind == "assistant":
            message = parsed.get("message")
            if not isinstance(message, dict):
                message = {}
            mid = message.get("id")
            if isinstance(mid, str) and mid:
                if mid not in seen_messages:
                    seen_messages.add(mid)
                    metrics["llm_calls"] += 1
            else:
                metrics["llm_calls"] += 1
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tid = block.get("id")
                if isinstance(tid, str) and tid:
                    if tid in seen_tools:
                        continue
                    seen_tools.add(tid)
                metrics["tool_calls"] += 1
        elif kind == "stream_event":
            event = parsed.get("event")
            if not isinstance(event, dict) or event.get("type") != "message_delta":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            for key, attr in _USAGE_KEYS:
                metrics[attr] += _usage_int(usage.get(key))
    return metrics


def _dispatch_claude_stream(obj: dict, state,
                            on_stream: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """把一条 claude stream-json 事件归一为 text/tool 回调。异常吞掉。"""
    try:
        state.saw_event = True
        sid = obj.get("session_id")
        if isinstance(sid, str) and sid:
            state.session_id = sid
        if on_stream is None:
            return
        kind = obj.get("type")
        if kind == "stream_event":
            delta = (obj.get("event") or {}).get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                state.text_streamed = True
                on_stream({"kind": "text", "text": delta["text"]})
            return
        if kind != "assistant":
            return
        content = (obj.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "?")
            tid = str(block.get("id") or name)
            if tid in state.seen_tools:
                continue
            state.seen_tools.add(tid)
            on_stream({"kind": "tool", "name": name,
                       "summary": _tool_summary(name, block.get("input"))})
        if state.text_streamed:
            return
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        text = "".join(parts)
        if text:
            state.text_streamed = True
            on_stream({"kind": "text", "text": text})
    except Exception:
        pass


class ClaudeAgent(AgentAdapter):
    name = "claude"

    @classmethod
    def match_bin(cls, base: str) -> bool:
        return base.startswith("claude")

    def build_cmd(self, runner, allowed_tools: List[str], max_turns: int,
                  resume_sid: str = "", *, stream: bool = False,
                  steer: bool = False) -> List[str]:
        override = (os.environ.get("GDD_AGENT_BIN") or "").strip()
        cmd = [override or "claude"]
        pre = os.environ.get("GDD_AGENT_PRE_ARGS", "")
        if pre:
            cmd += shlex.split(pre)
        # Claude 一律 -p stream-json。stream 与其他执行端共用签名，不退回单份 json。
        cmd += ["-p", "--output-format", "stream-json", "--max-turns", str(max_turns),
                "--verbose", "--include-partial-messages"]
        if resume_sid and os.environ.get("GDD_SUBJECT_RESUME", "on") != "off":
            cmd += ["--resume", resume_sid]
        model = (os.environ.get("GDD_AGENT_MODEL") or "").strip()
        if model:
            cmd += ["--model", model]
        if allowed_tools:
            cmd += ["--allowedTools", " ".join(allowed_tools)]
            allowed = set(allowed_tools)
            denied = [name for name in _HARNESS_TOOLS if name not in allowed]
            if denied:
                cmd += ["--disallowedTools", " ".join(denied)]
        extra = os.environ.get("GDD_AGENT_EXTRA_ARGS", "")
        if extra:
            cmd += shlex.split(extra)
        if "--dangerously-skip-permissions" not in cmd:
            cmd.append("--dangerously-skip-permissions")
        subject = bool(steer or getattr(runner, "steer_subject", False))
        if subject:
            from .. import steer as steer_mod
            if steer_mod.enabled():
                try:
                    steer_mod.prepare(runner.root)
                except OSError:
                    pass
                else:
                    cmd += ["--settings", steer_mod.claude_settings_path(runner.root)]
        ext = getattr(runner, "node_extensions", None)
        if ext is not None and getattr(ext, "mcp_config", ""):
            cmd += ["--mcp-config", ext.mcp_config]
        plugin = getattr(ext, "plugin_dir", "") if ext is not None else ""
        if plugin:
            cmd += ["--plugin-dir", plugin]
        return cmd

    def on_event(self, obj, state, on_stream) -> None:
        _dispatch_claude_stream(obj, state, on_stream)

    def parse(self, stdout: str, stderr: str, returncode: int, state, *,
              max_turns: int) -> SessionResult:
        events: List[dict] = []
        candidates: List[dict] = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            events.append(parsed)
            if _is_result(parsed):
                candidates.append(parsed)
        if not events:
            try:
                whole = json.loads(stdout or "")
            except json.JSONDecodeError:
                whole = None
            if isinstance(whole, dict):
                events.append(whole)
                if _is_result(whole):
                    candidates.append(whole)
        metrics = _collect_metrics(events)

        def _apply(res: SessionResult, data: Optional[dict]) -> SessionResult:
            """写入会话指标。token 优先 result.usage，缺失时用 message_delta 汇总。"""
            usage = data.get("usage") if isinstance(data, dict) else None
            if isinstance(usage, dict):
                tokens = {attr: _usage_int(usage.get(key))
                          for key, attr in _USAGE_KEYS}
            else:
                tokens = {attr: metrics[attr] for _, attr in _USAGE_KEYS}
            res.tool_calls = metrics["tool_calls"]
            res.llm_calls = metrics["llm_calls"]
            res.token_input = tokens["token_input"]
            res.token_output = tokens["token_output"]
            res.token_cache_creation = tokens["token_cache_creation"]
            res.token_cache_read = tokens["token_cache_read"]
            res.token_status = _token_status(
                sum(tokens.values()), metrics["llm_calls"] > 0)
            return res

        err_tail = (stderr or "").strip()
        if candidates:
            data = candidates[-1]
            is_error = bool(data.get("is_error")) or data.get("subtype") not in (
                None, "success")
            turns_exhausted = data.get("subtype") == "error_max_turns"

            def _to_int(value, default=0):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return default

            def _to_float(value, default=0.0):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return default

            text = _result_text(data)
            res = SessionResult(
                ok=not is_error,
                text=text,
                error=("" if not is_error else
                       text[:500] or "claude 会话失败"
                       f"（subtype={data.get('subtype') or '未知'}）"),
                session_id=str(data.get("session_id", "") or ""),
                turns=_to_int(data.get("num_turns", 0)),
                cost_usd=_to_float(data.get("total_cost_usd", 0.0)),
                raw=stdout or "",
                turns_exhausted=turns_exhausted,
            )
            if not res.ok and len(res.text) <= 300:
                hint = _claude_login_hint(res.text, err_tail) or _auth_hint(
                    res.text, err_tail)
                if hint:
                    res.error = _claude_auth_error(hint)
            return _apply(res, data)
        hint = ""
        if len(stdout or "") <= 300:
            hint = _claude_login_hint(stdout, err_tail) or _auth_hint(stdout, err_tail)
        if hint:
            return _apply(SessionResult(
                ok=False, error=_claude_auth_error(hint),
                raw=(stdout or "") + "\n" + (stderr or "")), None)
        return _apply(SessionResult(
            ok=False,
            error=f"claude 输出不可解析（returncode={returncode}）stderr: {err_tail[:500]}",
            raw=(stdout or "") + "\n" + (stderr or ""),
        ), None)

    def launch_hint(self) -> str:
        return "请确认本机 claude 可执行，或设置 GDD_AGENT_BIN"
