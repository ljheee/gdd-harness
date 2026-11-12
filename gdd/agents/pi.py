"""pi 执行端：设置页、JSONL、--session-id、--skill。"""
from __future__ import annotations

import os
import shutil
from typing import Any, Callable, Dict, List, Optional

from .. import pi_settings
from ..runner import (
    ConfigError,
    SessionResult,
    _SessionState,
    _auth_hint,
    _token_status,
    _tool_summary,
    _usage_int,
)
from .base import AgentAdapter

TOOL_MAP = {
    "read": "read",
    "write": "write",
    "edit": "edit",
    "multiedit": "edit",
    "grep": "grep",
    "glob": "find",
    "find": "find",
    "bash": "bash",
    "ls": "ls",
}


def _settings_hint() -> str:
    return "打开 Web UI 设置页检查端点 / API key / 模型"


def _auth_error(hint: str) -> str:
    return (f"pi 端点凭证或鉴权失败（命中特征 '{hint}'）。"
            f"请{_settings_hint()}，然后 gdd retry 续跑。")


_CONFIG_HINTS = (
    "404", "model not found", "model_not_found", "unknown model",
    "no such model",
)


def _config_hint(*texts: str) -> str:
    for text in texts:
        low = (text or "").lower()
        for hint in _CONFIG_HINTS:
            if hint in low:
                return hint
    return ""


def _config_error(hint: str, detail: str) -> str:
    summary = (detail or "").strip()
    if len(summary) > 300:
        summary = summary[:300] + "…"
    return (f"pi 端点或模型配置错误（命中特征 '{hint}'）。"
            f"请{_settings_hint()}，然后 gdd retry 续跑。"
            f"原始错误：{summary}")


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _observe_message_end(state: _SessionState, message: Any, *,
                         count_usage: bool = True) -> None:
    if not isinstance(message, dict):
        return
    if message.get("role") == "assistant":
        state.saw_model_response = True
        text = _message_text(message)
        if text:
            state.last_text = text
        stop = str(message.get("stopReason") or "")
        if stop in ("error", "aborted"):
            state.aborted = state.aborted or stop == "aborted"
            if not state.error_message:
                state.error_message = str(message.get("errorMessage") or f"stopReason={stop}")
        usage = message.get("usage")
        if isinstance(usage, dict):
            cost = usage.get("cost")
            if isinstance(cost, dict):
                try:
                    state.cost = float(cost.get("total") or 0.0)
                except (TypeError, ValueError):
                    pass
            # agent_end 的回退观察不重复累计 token：message_end 已累计过同一消息。
            if count_usage:
                state.token_input += _usage_int(usage.get("input"))
                state.token_output += _usage_int(usage.get("output"))
                state.token_cache_creation += _usage_int(usage.get("cacheWrite"))
                state.token_cache_read += _usage_int(usage.get("cacheRead"))


def _dispatch_event(obj: dict, state: _SessionState,
                    on_stream: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """把一条 pi JSONL 事件归一为 text/tool 回调并累计会话状态。异常吞掉。"""
    try:
        kind = obj.get("type")
        state.saw_event = True
        if kind == "session":
            sid = obj.get("id")
            if isinstance(sid, str) and sid:
                state.session_id = sid
            return
        if kind == "message_update":
            ev = obj.get("assistantMessageEvent") or {}
            if ev.get("type") == "text_delta":
                delta = str(ev.get("delta") or "")
                if delta:
                    state.text_streamed = True
                    if on_stream:
                        on_stream({"kind": "text", "text": delta})
            return
        if kind == "tool_execution_start":
            name = str(obj.get("toolName") or "?")
            tid = str(obj.get("toolCallId") or "")
            if tid and tid in state.seen_tools:
                return
            if tid:
                state.seen_tools.add(tid)
            # 无 toolCallId 时无法去重：每次事件各计 1（规格第 3 节）。
            state.tool_calls += 1
            summary = _tool_summary(name, obj.get("args"))
            if on_stream:
                on_stream({"kind": "tool", "name": name, "summary": summary})
            return
        if kind == "message_end":
            _observe_message_end(state, obj.get("message"))
            return
        if kind == "turn_end":
            state.turns += 1
            return
        if kind == "agent_end":
            state.saw_agent_end = True
            if not state.last_text:
                for message in reversed(obj.get("messages") or []):
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        _observe_message_end(state, message, count_usage=False)
                        if state.last_text:
                            break
            return
        if kind == "agent_settled":
            if obj.get("aborted"):
                state.aborted = True
            return
    except Exception:
        pass


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve_pi_bin(runner) -> str:
    override = (os.environ.get("GDD_AGENT_BIN") or "").strip()
    if override:
        return override
    candidates = [
        os.path.join(runner.root, "node_modules", ".bin", "pi"),
        os.path.join(_repo_root(), "node_modules", ".bin", "pi"),
    ]
    for local in candidates:
        if os.path.isfile(local) and os.access(local, os.X_OK):
            return local
    for name in ("pi", "pi.cmd"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _map_tools(runner, allowed_tools: List[str], subject: bool) -> List[str]:
    mapped: List[str] = []
    unknown: List[str] = []
    for tool in allowed_tools or []:
        name = TOOL_MAP.get(str(tool).lower())
        if not name:
            if str(tool).lower() not in ("subagent",):
                unknown.append(str(tool))
            continue
        if name not in mapped:
            mapped.append(name)
    if unknown and runner.on_event:
        try:
            runner.on_event("tool_map_unknown", tools=unknown)
        except Exception:
            pass
    if subject and "subagent*" not in mapped:
        mapped.append("subagent*")
    return mapped


def _pi_extensions(runner, subject: bool) -> List[str]:
    if not subject:
        return []
    out: List[str] = []
    from .. import steer as steer_mod
    if steer_mod.enabled():
        ext_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "steer_pi_extension.mjs")
        if os.path.isfile(ext_path):
            out.append(ext_path)
    for base in (runner.root, _repo_root()):
        subagents = os.path.join(base, "node_modules", "pi-subagents",
                                 "bundle", "index.js")
        if os.path.isfile(subagents):
            out.append(subagents)
            break
    return out


def _load_settings():
    try:
        settings = pi_settings.load()
    except pi_settings.SettingsError as e:
        raise ConfigError(str(e)) from e
    ready = pi_settings.ready_error(settings)
    if ready:
        raise ConfigError(ready)
    model = pi_settings.resolve_model(settings)
    if model.startswith("custom/"):
        model = model[len("custom/"):]
    return settings, model


class PiAgent(AgentAdapter):
    name = "pi"

    def prepare(self, runner) -> None:
        settings, model = _load_settings()
        try:
            runner._prepare_agent_dir(str(settings.get("baseUrl") or ""),
                                      str(settings.get("apiKey") or ""), model)
        except OSError as e:
            raise ConfigError(f"pi agent 目录准备失败: {e}") from e

    def build_cmd(self, runner, allowed_tools: List[str], max_turns: int,
                  resume_sid: str = "", *, stream: bool = False,
                  steer: bool = False) -> List[str]:
        import shlex
        pi_bin = _resolve_pi_bin(runner)
        if not pi_bin:
            raise ConfigError(
                "pi 可执行文件不可用：请执行 npm ci，或用 GDD_AGENT_BIN 指定 pi 路径")
        _settings, model = _load_settings()
        subject = bool(steer or getattr(runner, "steer_subject", False))
        cmd = [pi_bin]
        pre = os.environ.get("GDD_AGENT_PRE_ARGS", "")
        if pre:
            cmd += shlex.split(pre)
        cmd += ["--print", "--mode", "json", "--model", f"custom/{model}"]
        tools = _map_tools(runner, allowed_tools, subject)
        if tools:
            cmd += ["--tools", ",".join(tools)]
        if resume_sid and os.environ.get("GDD_SUBJECT_RESUME", "on") != "off":
            cmd += ["--session-id", resume_sid]
        cmd += ["--session-dir", runner._session_dir()]
        ext = getattr(runner, "node_extensions", None)
        if ext is not None:
            for skill_dir in getattr(ext, "skill_dirs", []) or []:
                cmd += ["--skill", skill_dir]
        for extension in _pi_extensions(runner, subject):
            cmd += ["--extension", extension]
        extra = os.environ.get("GDD_AGENT_EXTRA_ARGS", "")
        if extra:
            cmd += shlex.split(extra)
        return cmd

    def extra_env(self, runner, subject: bool) -> dict:
        return {
            "PI_CODING_AGENT_DIR": runner._agent_dir(),
            "PI_CODING_AGENT_SESSION_DIR": runner._session_dir(),
        }

    def on_event(self, obj, state, on_stream) -> None:
        _dispatch_event(obj, state, on_stream)

    def should_stop(self, state, max_turns: int) -> bool:
        return bool(max_turns and max_turns > 0 and state.turns >= max_turns)

    def parse(self, stdout: str, stderr: str, returncode: int, state, *,
              max_turns: int) -> SessionResult:
        import json
        if state is None:
            state = _SessionState()
            for raw in (stdout or "").splitlines():
                raw = raw.strip()
                if not raw.startswith("{"):
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    _dispatch_event(obj, state, None)

        err_tail = (stderr or "").strip()[-1000:]
        if returncode != 0:
            detail = (state.error_message or err_tail or "无错误输出").strip()
            res = SessionResult(
                ok=False,
                text=state.last_text,
                error=f"pi 会话退出码 {returncode}：{detail}",
                session_id=state.session_id,
                turns=state.turns,
                cost_usd=state.cost,
                raw=stdout or "",
            )
        elif state.aborted or state.error_message:
            detail = (state.error_message or err_tail or "会话被中止").strip()
            res = SessionResult(
                ok=False,
                text=state.last_text,
                error=f"pi 会话失败：{detail}",
                session_id=state.session_id,
                turns=state.turns,
                cost_usd=state.cost,
                raw=stdout or "",
            )
        elif not state.saw_event:
            res = SessionResult(
                ok=False,
                error=(f"pi 输出不可解析（returncode={returncode}），未收到 JSON 事件；"
                       f"stderr: {err_tail[:500]}"),
                session_id=state.session_id,
                raw=stdout or "",
            )
        else:
            res = SessionResult(
                ok=True,
                text=state.last_text,
                session_id=state.session_id,
                turns=state.turns,
                cost_usd=state.cost,
                raw=stdout or "",
            )
        total = (state.token_input + state.token_output
                 + state.token_cache_creation + state.token_cache_read)
        res.tool_calls = state.tool_calls
        res.llm_calls = state.turns
        res.token_input = state.token_input
        res.token_output = state.token_output
        res.token_cache_creation = state.token_cache_creation
        res.token_cache_read = state.token_cache_read
        res.token_status = _token_status(
            total, state.saw_model_response or state.turns > 0)
        if not res.ok:
            hint = _auth_hint(res.error, err_tail) if len(res.error) <= 500 else ""
            if hint:
                res.error = _auth_error(hint)
            else:
                config = (_config_hint(res.error, err_tail)
                          if len(res.error) <= 500 else "")
                if config:
                    # D10：错误的 baseUrl/模型同样属于设置页可处置的问题，
                    # 保留原始摘要并给出明确指向，不静默回退其他执行端。
                    res.error = _config_error(config, res.error or err_tail)
                    res.config_error = True
        return res

    def apply_turn_cap(self, res, state, max_turns: int):
        if not self.should_stop(state, max_turns):
            return res
        if state.session_id:
            res.ok = False
            res.turns_exhausted = True
            res.error = (f"pi 会话 turns 达到上限（{max_turns}），"
                         "保留会话可续跑")
        else:
            res.ok = False
            res.error = f"pi 会话 turns 达到上限（{max_turns}），且未取得 session_id"
        return res

    def launch_hint(self) -> str:
        return "请执行 npm ci 或设置 GDD_AGENT_BIN"
