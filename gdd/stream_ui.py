"""终端流式呈现（P0 clarify 全文 / P1 subject 工具摘要+截断文本）。

仅 tty 交互路径（gdd go / gdd chat）注入；非 tty、门禁、UI 后台不启用。
流式文案不是过关证据——权威结论仍在门禁/产物（见 docs/design-go.md §9）。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

# P1 主体文本累计上限：超出后省略，避免长文淹没工具摘要
SUBJECT_TEXT_CAP = 800

OnStream = Callable[[Dict[str, Any]], None]
StreamFactory = Callable[[str], Optional[OnStream]]


def _safe_print(s: str, *, end: str = "\n", flush: bool = True) -> None:
    try:
        print(s, end=end, flush=flush)
    except BrokenPipeError:
        pass


def make_clarify_printer() -> OnStream:
    """P0：AI> 后流式打字；调用方在会话结束后再补换行。"""
    state = {"started": False}

    def on_stream(ev: Dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind == "text":
            text = ev.get("text") or ""
            if not text:
                return
            if not state["started"]:
                _safe_print("AI> ", end="")
                state["started"] = True
            _safe_print(text, end="")
        elif kind == "tool":
            # 澄清会话偶发工具：另起一行摘要，不打断正文观感过多
            name = ev.get("name") or "?"
            summary = ev.get("summary") or ""
            prefix = "\n" if state["started"] else ""
            _safe_print(f"{prefix}⋯ {name}" + (f" {summary}" if summary else ""))
            state["started"] = False  # 下次文本重新打 AI>

    return on_stream


def make_subject_printer(*, text_cap: int = SUBJECT_TEXT_CAP) -> OnStream:
    """P1：工具一行摘要 + 截断 assistant 文本。"""
    state = {"text_n": 0, "omitted": False, "in_text": False}

    def on_stream(ev: Dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind == "tool":
            if state["in_text"]:
                _safe_print("")  # 结束文本行
                state["in_text"] = False
            name = ev.get("name") or "?"
            summary = ev.get("summary") or ""
            _safe_print(f"⋯ {name}" + (f" {summary}" if summary else ""))
            return
        if kind != "text":
            return
        text = ev.get("text") or ""
        if not text or state["omitted"]:
            return
        remain = text_cap - state["text_n"]
        if remain <= 0:
            state["omitted"] = True
            if state["in_text"]:
                _safe_print("…")
                state["in_text"] = False
            else:
                _safe_print("⋯ …")
            return
        chunk = text if len(text) <= remain else text[:remain]
        if not state["in_text"]:
            _safe_print("⋯ ", end="")
            state["in_text"] = True
        _safe_print(chunk, end="")
        state["text_n"] += len(chunk)
        if len(text) > remain:
            state["omitted"] = True
            _safe_print("…")
            state["in_text"] = False

    return on_stream


def finish_stream_line(printer: Optional[OnStream]) -> None:
    """会话结束后补一个换行（若打印机曾输出未换行文本）。"""
    if printer is None:
        return
    # clarify/subject 打印机无统一 flush API：直接打换行最稳
    _safe_print("")


def factory_for_tty() -> StreamFactory:
    """tty 下返回 role→printer；gate 返回 None。每次调用新建打印机（额度不串）。"""

    def factory(role: str) -> Optional[OnStream]:
        if role == "clarify":
            return make_clarify_printer()
        if role == "subject":
            return make_subject_printer()
        return None

    return factory