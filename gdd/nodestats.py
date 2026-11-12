"""节点三段统计（entry / 主体 / exit）的存储与聚合。

纯函数 + 普通 dict，不依赖 orchestrator / runner：编排器在段的起止点调用
``open_segment`` / ``close_segment``，在会话结束后调用 ``add_session``；
展示层用 ``for_ui`` 取只读视图。

存储位置：``state["node_stats"]``，按节点 ID 存::

    {node_id: {"segments": {"entry": S, "subject": S, "exit": S},
               "total_s": float}}

    S = {"duration_s": float,            # 段内全部活动区间之和（秒）
         "open_started_at": float|None,  # 当前活动区间起点（内部字段，展示时剥离）
         "sessions": [session...],       # 按开始时间排序的会话记录
         "tool_calls": int, "llm_calls": int,
         "token_total": int|None,        # 全部不可得时为 None（不能以 0 冒充）
         "token_status": "available"|"partial"|"unavailable"}

时间一律用 epoch 秒（float）表示，累计也按秒；展示层负责换算为 min。

失败隔离：统计是可选观测能力，采集失败不得改变节点推进结果。公开写函数捕获
自身异常，把诊断信息追加到 ``state["node_stats"]["_diagnostics"]``（有上限），
并返回安全默认值；损坏的历史数据在读取时按空值归一化，不抛异常。
"""
from __future__ import annotations

import copy
import time
from typing import Any, Dict, List, Optional

SEGMENTS = ("entry", "subject", "exit")
# 悬浮展示用中文段名：entry / 主体 / exit（与 spec §5 一致）
SEGMENT_LABELS = {"entry": "entry", "subject": "主体", "exit": "exit"}

TOKEN_AVAILABLE = "available"
TOKEN_PARTIAL = "partial"
TOKEN_UNAVAILABLE = "unavailable"
_SESSION_TOKEN_STATUSES = (TOKEN_AVAILABLE, TOKEN_UNAVAILABLE)
# 会话类型（spec §4）；subject_resume/subject_steer、task-<id> 按同一逻辑类型归一
SESSION_KINDS = ("entry", "exit", "subject", "task", "review", "adversarial")

_MAX_DIAGNOSTICS = 50


# ---------------------------------------------------------------- 基础归一化

def _as_float(value: Any, default: float = 0.0) -> float:
    """接受 int/float（拒绝 bool），其余回退默认值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _as_int(value: Any, default: int = 0) -> int:
    """非负整数归一化：bool/负数/非数字回退默认值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return max(0, int(value))
    except (OverflowError, ValueError):
        return default


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else float(now)


def _validate_segment(segment: str) -> str:
    if segment not in SEGMENTS:
        raise ValueError(f"未知统计段 {segment!r}（应为 {', '.join(SEGMENTS)}）")
    return segment


def _normalize_kind(kind: str) -> str:
    """会话类型归一：subject_resume/subject_steer → subject，task-<id> → task。

    未知类型原样保留（审计可诊断），不静默丢弃。
    """
    k = str(kind or "").strip()
    if not k:
        raise ValueError("kind 不能为空")
    if k.startswith("subject"):
        return "subject"
    if k.startswith("task"):
        return "task"
    return k


# ---------------------------------------------------------------- 诊断

def _diagnose(state: Any, message: str) -> None:
    """尽力记录统计诊断；本身绝不抛异常（否则隔离失效）。"""
    try:
        if not isinstance(state, dict):
            return
        root = state.setdefault("node_stats", {})
        if not isinstance(root, dict):
            root = {}
            state["node_stats"] = root
        diag = root.get("_diagnostics")
        if not isinstance(diag, list):
            diag = []
            root["_diagnostics"] = diag
        diag.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(message)[:300],
        })
        del diag[:-_MAX_DIAGNOSTICS]
    except Exception:
        pass


# ---------------------------------------------------------------- 结构归一化

def _normalize_segment(seg: Dict[str, Any]) -> None:
    seg["duration_s"] = _as_float(seg.get("duration_s"))
    opened = seg.get("open_started_at")
    seg["open_started_at"] = None if (
        opened is None or isinstance(opened, bool)
        or not isinstance(opened, (int, float))) else float(opened)
    if not isinstance(seg.get("sessions"), list):
        seg["sessions"] = []
    seg["tool_calls"] = _as_int(seg.get("tool_calls"))
    seg["llm_calls"] = _as_int(seg.get("llm_calls"))
    if seg.get("token_status") not in (TOKEN_AVAILABLE, TOKEN_PARTIAL, TOKEN_UNAVAILABLE):
        seg["token_status"] = TOKEN_AVAILABLE
    total = seg.get("token_total")
    seg["token_total"] = None if (
        seg["token_status"] == TOKEN_UNAVAILABLE or total is None) else _as_int(total)


def _normalize_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    """把节点统计容器归一化为合法结构（原地）；损坏字段按空值处理。"""
    segments = stats.get("segments")
    if not isinstance(segments, dict):
        segments = {}
        stats["segments"] = segments
    for name in SEGMENTS:
        seg = segments.get(name)
        if not isinstance(seg, dict):
            seg = {}
            segments[name] = seg
        _normalize_segment(seg)
    stats["total_s"] = _as_float(stats.get("total_s"))
    stats["seq"] = _as_int(stats.get("seq"))  # 无 session id 会话的稳定标识序号
    return stats


def ensure(state: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    """返回节点统计容器；缺失或被剥空时补成合法结构（原地）。"""
    if not isinstance(state, dict):
        raise TypeError("state 必须是 dict")
    root = state.setdefault("node_stats", {})
    if not isinstance(root, dict):  # 损坏：整块替换，不保留无法解释的形态
        root = {}
        state["node_stats"] = root
    stats = root.get(node_id)
    if not isinstance(stats, dict):
        stats = {}
        root[node_id] = stats
    return _normalize_stats(stats)


# ---------------------------------------------------------------- 会话记录

def _infer_token_status(llm_calls: int, components: List[int]) -> str:
    """调用方未给 token_status 时按口径推断。

    有模型响应（llm_calls > 0）但四项 usage 全 0/缺失 → 不可得；
    无模型响应 → token 为 0，可得；任一分量 > 0 → 可得。
    """
    if any(c > 0 for c in components):
        return TOKEN_AVAILABLE
    return TOKEN_UNAVAILABLE if llm_calls > 0 else TOKEN_AVAILABLE


def _from_result(result: Any, attr: str, default: Any = None) -> Any:
    """从 SessionResult（或测试替身）取字段；T1 未落地时也能安全降级。"""
    if result is None:
        return default
    return getattr(result, attr, default)


def _new_session_record(
    *,
    session_id: str,
    kind: str,
    task_id: str,
    started_at: float,
    ended_at: float,
    duration_s: float,
    tool_calls: int,
    llm_calls: int,
    token_input: int,
    token_output: int,
    token_cache_creation: int,
    token_cache_read: int,
    token_status: str,
) -> Dict[str, Any]:
    total = (token_input + token_output + token_cache_creation + token_cache_read)
    return {
        "session_id": session_id,
        "kind": kind,
        "task_id": task_id,
        "attempts": 1,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": round(duration_s, 6),
        "tool_calls": tool_calls,
        "llm_calls": llm_calls,
        "token_input": token_input,
        "token_output": token_output,
        "token_cache_creation": token_cache_creation,
        "token_cache_read": token_cache_read,
        # 不可得时 token_total 置空：不得以 0 冒充「未上报」
        "token_total": total if token_status == TOKEN_AVAILABLE else None,
        "token_status": token_status,
    }


def _normalize_session(session: Dict[str, Any]) -> Dict[str, Any]:
    """会话记录损坏时按空值归一化；保留已知的可解释字段。"""
    status = session.get("token_status")
    if status not in _SESSION_TOKEN_STATUSES:
        status = TOKEN_UNAVAILABLE
    out = {
        "session_id": str(session.get("session_id") or ""),
        "kind": str(session.get("kind") or "subject"),
        "task_id": str(session.get("task_id") or ""),
        "attempts": max(1, _as_int(session.get("attempts"), 1)),
        "started_at": _as_float(session.get("started_at")),
        "ended_at": _as_float(session.get("ended_at")),
        "duration_s": _as_float(session.get("duration_s")),
        "tool_calls": _as_int(session.get("tool_calls")),
        "llm_calls": _as_int(session.get("llm_calls")),
        "token_input": _as_int(session.get("token_input")),
        "token_output": _as_int(session.get("token_output")),
        "token_cache_creation": _as_int(session.get("token_cache_creation")),
        "token_cache_read": _as_int(session.get("token_cache_read")),
        "token_status": status,
    }
    out["token_total"] = None if status == TOKEN_UNAVAILABLE else _as_int(session.get("token_total"))
    return out


def _find_session(seg: Dict[str, Any], session_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    for item in seg.get("sessions", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("session_id") or "") != session_id:
            continue
        if str(item.get("task_id") or "") != task_id:
            continue
        return item
    return None


def _merge_session(existing: Dict[str, Any], incoming: Dict[str, Any], attempts: int) -> None:
    existing["attempts"] = max(1, _as_int(existing.get("attempts"), 1)) + attempts
    existing["started_at"] = min(_as_float(existing.get("started_at")),
                                 _as_float(incoming.get("started_at")))
    existing["ended_at"] = max(_as_float(existing.get("ended_at")),
                               _as_float(incoming.get("ended_at")))
    existing["duration_s"] = round(
        _as_float(existing.get("duration_s")) + _as_float(incoming.get("duration_s")), 6)
    for key in ("tool_calls", "llm_calls", "token_input", "token_output",
                "token_cache_creation", "token_cache_read"):
        existing[key] = _as_int(existing.get(key)) + _as_int(incoming.get(key))
    known_total = _as_int(existing.get("token_total"))
    incoming_total = _as_int(incoming.get("token_total"))
    if (existing.get("token_status") == TOKEN_AVAILABLE
            and incoming.get("token_status") == TOKEN_AVAILABLE):
        existing["token_total"] = known_total + incoming_total
        existing["token_status"] = TOKEN_AVAILABLE
    else:
        # 同一逻辑会话任一次调用 usage 不可得 → 整体不可得（不把已知部分当全量）
        existing["token_total"] = None
        existing["token_status"] = TOKEN_UNAVAILABLE


# ---------------------------------------------------------------- 段计时

def open_segment(state: Dict[str, Any], node_id: str, segment: str,
                 now: Optional[float] = None) -> bool:
    """开启一个活动区间；已有未关闭区间时丢弃旧起点（不计跨进程停机时间）。"""
    try:
        seg_name = _validate_segment(segment)
        seg = ensure(state, node_id)["segments"][seg_name]
        seg["open_started_at"] = _now(now)
        return True
    except Exception as e:  # 统计失败不得影响节点推进
        _diagnose(state, f"open_segment({node_id},{segment}): {e}")
        return False


def close_segment(state: Dict[str, Any], node_id: str, segment: str,
                  now: Optional[float] = None) -> float:
    """关闭当前活动区间并把耗时累加到段；无未关闭区间时幂等，返回本次累加秒数。"""
    try:
        seg_name = _validate_segment(segment)
        stats = ensure(state, node_id)
        seg = stats["segments"][seg_name]
        started = seg.get("open_started_at")
        if started is None:
            return 0.0
        seg["open_started_at"] = None
        # 时钟回拨时按 0 计，不让负区间吞掉已累计耗时
        delta = max(0.0, _now(now) - _as_float(started))
        seg["duration_s"] = round(_as_float(seg.get("duration_s")) + delta, 6)
        _sync_aggregates(stats)
        return delta
    except Exception as e:
        _diagnose(state, f"close_segment({node_id},{segment}): {e}")
        return 0.0


# ---------------------------------------------------------------- 会话写入

def add_session(
    state: Dict[str, Any],
    node_id: str,
    segment: str,
    *,
    kind: str,
    session_id: str = "",
    task_id: str = "",
    started_at: Optional[float] = None,
    ended_at: Optional[float] = None,
    result: Any = None,
    duration_s: Optional[float] = None,
    tool_calls: Optional[int] = None,
    llm_calls: Optional[int] = None,
    token_input: Optional[int] = None,
    token_output: Optional[int] = None,
    token_cache_creation: Optional[int] = None,
    token_cache_read: Optional[int] = None,
    token_status: Optional[str] = None,
    attempts: int = 1,
) -> Optional[Dict[str, Any]]:
    """记录一次会话调用；同一 ``session_id`` + ``task_id`` 的续跑合并为一条。

    - ``result`` 给定时从 SessionResult 取缺失字段（getattr 降级，字段未落地也不报错）；
    - 无 ``session_id`` 的调用不合并，按 ``kind#序号`` 生成稳定标识；
    - 不同 ``task_id`` 即使 session id 相同也分开记录。
    返回写入/合并后的会话记录（副本），失败返回 None。
    """
    try:
        seg_name = _validate_segment(segment)
        kind = _normalize_kind(kind)
        stats = ensure(state, node_id)
        seg = stats["segments"][seg_name]

        sid = str(session_id or _from_result(result, "session_id", "") or "")
        tid = str(task_id or "")
        dur = duration_s if duration_s is not None else _from_result(result, "duration_s", 0.0)
        dur = max(0.0, _as_float(dur))
        tc = tool_calls if tool_calls is not None else _from_result(result, "tool_calls", 0)
        lc = llm_calls if llm_calls is not None else _from_result(result, "llm_calls", 0)
        tin = token_input if token_input is not None else _from_result(result, "token_input", 0)
        tout = token_output if token_output is not None else _from_result(result, "token_output", 0)
        tcc = (token_cache_creation if token_cache_creation is not None
               else _from_result(result, "token_cache_creation", 0))
        tcr = (token_cache_read if token_cache_read is not None
               else _from_result(result, "token_cache_read", 0))
        tc, lc = _as_int(tc), _as_int(lc)
        tin, tout = _as_int(tin), _as_int(tout)
        tcc, tcr = _as_int(tcc), _as_int(tcr)

        status = token_status or _from_result(result, "token_status", None)
        if status not in _SESSION_TOKEN_STATUSES:
            status = _infer_token_status(lc, [tin, tout, tcc, tcr])

        valid_start = (started_at is not None and not isinstance(started_at, bool)
                       and isinstance(started_at, (int, float)))
        if valid_start:
            start = float(started_at)
            end = start + dur if ended_at is None else _now(ended_at)
        else:
            end = _now(ended_at)
            start = end - dur

        record = _new_session_record(
            session_id=sid, kind=kind, task_id=tid,
            started_at=start, ended_at=max(start, end),
            duration_s=dur, tool_calls=tc, llm_calls=lc,
            token_input=tin, token_output=tout,
            token_cache_creation=tcc, token_cache_read=tcr,
            token_status=status,
        )
        record["attempts"] = max(1, _as_int(attempts, 1))

        existing = _find_session(seg, sid, tid) if sid else None
        if existing is None:
            if not sid:
                stats["seq"] = _as_int(stats.get("seq")) + 1
                record["session_id"] = f"{kind}#{stats['seq']}"
            seg["sessions"].append(record)
            saved = record
        else:
            merged = _normalize_session(existing)
            _merge_session(merged, record, max(1, _as_int(attempts, 1)))
            existing.clear()
            existing.update(merged)
            saved = existing
        _sync_aggregates(stats)
        return copy.deepcopy(saved)
    except Exception as e:
        _diagnose(state, f"add_session({node_id},{segment},{kind!r}): {e}")
        return None


# ---------------------------------------------------------------- 聚合与视图

def _sync_aggregates(stats: Dict[str, Any]) -> None:
    """重算每段工具/LLM/token 汇总与 total_s（原地）；会话按开始时间排序。"""
    segments = stats["segments"]
    total = 0.0
    for name in SEGMENTS:
        seg = segments[name]
        sessions: List[Dict[str, Any]] = []
        for item in seg.get("sessions", []):
            if isinstance(item, dict):
                sessions.append(_normalize_session(item))
        sessions.sort(key=lambda s: _as_float(s.get("started_at")))
        seg["sessions"] = sessions
        seg["tool_calls"] = sum(_as_int(s.get("tool_calls")) for s in sessions)
        seg["llm_calls"] = sum(_as_int(s.get("llm_calls")) for s in sessions)
        available = [s for s in sessions if s.get("token_status") == TOKEN_AVAILABLE]
        unknown = [s for s in sessions if s.get("token_status") != TOKEN_AVAILABLE]
        if not sessions:
            # 段内无 AI 会话：真实无消耗，不是「不可得」
            seg["token_total"] = 0
            seg["token_status"] = TOKEN_AVAILABLE
        elif not unknown:
            seg["token_total"] = sum(_as_int(s.get("token_total")) for s in available)
            seg["token_status"] = TOKEN_AVAILABLE
        elif available:
            seg["token_total"] = sum(_as_int(s.get("token_total")) for s in available)
            seg["token_status"] = TOKEN_PARTIAL
        else:
            seg["token_total"] = None
            seg["token_status"] = TOKEN_UNAVAILABLE
        total += _as_float(seg.get("duration_s"))
    stats["total_s"] = round(total, 6)


def _build_view(stats: Dict[str, Any]) -> Dict[str, Any]:
    """构造展示用只读视图：剥离内部 open 字段，会话已归一化排序。"""
    stats = _normalize_stats(copy.deepcopy(stats))
    _sync_aggregates(stats)
    out_segments: Dict[str, Any] = {}
    for name in SEGMENTS:
        seg = stats["segments"][name]
        out_segments[name] = {
            "duration_s": _as_float(seg.get("duration_s")),
            "tool_calls": _as_int(seg.get("tool_calls")),
            "llm_calls": _as_int(seg.get("llm_calls")),
            "token_total": seg.get("token_total"),
            "token_status": seg.get("token_status"),
            "sessions": copy.deepcopy(seg.get("sessions", [])),
        }
    return {"segments": out_segments, "total_s": _as_float(stats.get("total_s"))}


def refresh(state: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    """重算并落回该节点汇总，返回展示视图。

    节点没有统计条目时返回 None（不凭空创建）：旧任务/未采集节点不得因一次
    刷新就显示「0min」。需要建立统计时先调用 ``ensure`` 或段计时函数。
    """
    try:
        if not isinstance(state, dict):
            return None
        root = state.get("node_stats")
        if not isinstance(root, dict):
            return None
        stats = root.get(node_id)
        if not isinstance(stats, dict):
            return None
        _normalize_stats(stats)
        _sync_aggregates(stats)
        return _build_view(stats)
    except Exception as e:
        _diagnose(state, f"refresh({node_id}): {e}")
        return None


def for_ui(state: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    """只读展示视图；节点没有统计（旧任务/未产生）时返回 None，由展示层隐藏。"""
    try:
        if not isinstance(state, dict):
            return None
        root = state.get("node_stats")
        if not isinstance(root, dict):
            return None
        raw = root.get(node_id)
        if not isinstance(raw, dict) or not isinstance(raw.get("segments"), dict):
            return None
        return _build_view(raw)
    except Exception as e:
        _diagnose(state, f"for_ui({node_id}): {e}")
        return None
