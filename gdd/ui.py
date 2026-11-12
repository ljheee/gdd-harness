"""GDD 本地客户端：零依赖 Web UI（python3 -m gdd.ui）。

一个工作目录 = 一个"任务工作台"：
  <workdir>/
    .gdd-templates/   自定义模板（yaml/json）
    <task>/           每个任务一个子目录（内含 .gdd/）
    （内置 delivery 模板常驻可用）

能力：
  1. 模板管理：列表/查看/新建/编辑/删除（内置 delivery 只读）；
  2. 新任务：选模板 + 需求一句话 → init；
  3. 任务列表 + 任务详情：流程节点图（pending/进行中/门禁重试/待审批高亮/完成），
     run / approve / reject / retry / abort / reset 操作，working 期间用与
     `gdd steer` 相同的队列补充信息；
  4. run 在后台线程执行。页面 2s 轮询状态；运行进度用 SSE 滚动显示
     （澄清全文、主体的工具行和助手文本、门禁只显示阶段行）。

安全：仅监听 127.0.0.1；任务名/模板名白名单校验，禁止路径穿越。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from .builtins import DELIVERY_PIPELINE
from .cli import _load_pipeline_file
from .iteration import needs_iteration, prepare as prepare_iteration
from .models import PipelineSpec
from .orchestrator import Orchestrator
from . import nodestats
from . import pi_settings

# 任务创建串行化：ThreadingHTTPServer 并发处理双击/双 tab 的同名创建请求时，
# 两个请求都过存在性检查后才建目录，败者的失败清理会删掉胜者初始化好的任务
_create_lock = threading.Lock()

BUILTINS = {"delivery": DELIVERY_PIPELINE}

_NAME_RE = re.compile(
    r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff]{0,63}$")

_running: dict = {}          # task -> Thread（在跑的 run）
_running_lock = threading.Lock()

# 运行中滚动区：进程内环形缓冲，SSE 推给页面。不落盘，重置任务时清掉。
LIVE_MAX_CHARS = 64 * 1024
_live_lock = threading.Lock()
_live: dict = {}  # task -> {seq, events, chars, cond}


# ---------------------------------------------------------------- 工具

def _parse_repos(raw):
    """仓库文本或列表。每行「地址」或「地址 基线分支」。"""
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        specs = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) > 2:
                raise ValueError(f"仓库行只能是地址和可选基线分支：{line}")
            specs.append({"url": parts[0], "base": parts[1] if len(parts) == 2 else ""})
        return specs
    if isinstance(raw, list):
        specs = []
        for item in raw:
            if isinstance(item, str):
                specs.append({"url": item, "base": ""})
            elif isinstance(item, dict):
                specs.append({"url": str(item.get("url") or ""),
                              "base": str(item.get("base") or "")})
            else:
                raise ValueError("仓库条目不可用")
        return specs
    raise ValueError("仓库必须是文本或列表")


def _safe_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name or ""))


def _templates_dir(workdir: str) -> str:
    return os.path.join(workdir, ".gdd-templates")


def _list_templates(workdir: str) -> list:
    out = [{"name": "delivery", "builtin": True,
            "desc": BUILTINS["delivery"].get("name", "")}]
    td = _templates_dir(workdir)
    if os.path.isdir(td):
        for fn in sorted(os.listdir(td)):
            if fn.endswith((".yaml", ".yml", ".json")):
                out.append({"name": fn.rsplit(".", 1)[0], "builtin": False, "desc": ""})
    return out


def _template_path(workdir: str, name: str) -> str:
    td = _templates_dir(workdir)
    for ext in (".yaml", ".yml", ".json"):
        p = os.path.join(td, name + ext)
        if os.path.exists(p):
            return p
    return os.path.join(td, name + ".yaml")


def _parse_pipeline_content(content: str):
    """JSON 或 YAML → dict；校验结构合法。"""
    stripped = (content or "").lstrip()
    if not stripped:
        raise ValueError("模板内容为空")
    if stripped.startswith("{"):
        data = json.loads(content)
    else:
        try:
            import yaml  # 可选依赖
        except ImportError as e:
            raise ImportError("解析 YAML 模板需要 PyYAML（pip3 install pyyaml）") from e
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            # YAMLError 不是 ValueError。转成 ValueError，创建/保存模板的
            # 请求才能回到 JSON 400，而不是把连接打掉。
            raise ValueError(f"YAML 无法解析: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("顶层必须是 mapping")
    PipelineSpec.from_dict(data)  # 结构校验（非法即抛 ValueError/KeyError）
    return data


def _list_tasks(workdir: str) -> list:
    out = []
    for fn in sorted(os.listdir(workdir)):
        d = os.path.join(workdir, fn)
        sp = os.path.join(d, ".gdd", "state.json")
        if not os.path.isfile(sp):
            continue
        try:
            with open(sp, encoding="utf-8") as f:
                st = json.load(f)
            # P2-16：可解析但 load 深层校验失败（current 非法/缺条目等腐蚀态）
            # 或配套文件缺失（pipeline.json 被删——load 返回 False 不抛错）——
            # 曾贴健康 status 标签，导航谎报运行中，点进详情才 409。
            # 列表即跑完整 load 校验：异常或 False 都按 corrupt 呈现（reset 入口保留）
            if not Orchestrator(d).load():
                raise RuntimeError("load 失败：pipeline.json 缺失或状态不完整")
            out.append({
                "name": fn, "status": st.get("status"),
                "current": st.get("current"),
                "pipeline_id": st.get("pipeline_id"),
                "updated_at": st.get("updated_at"),
                "awaiting": st.get("status") == "awaiting_approval",
                "blocked": st.get("status") == "blocked",
                "running": fn in _running,
            })
        except (OSError, json.JSONDecodeError, ValueError, TypeError,
                AttributeError, RuntimeError):
            # state 损坏（含非对象 JSON：list/字符串——st.get() 抛 AttributeError；
            # 或结构合法但校验失败——load 的 RuntimeError 腐蚀态契约）：
            # 仍列出（status=corrupt），前端可进入并 reset 自救，而非从列表消失导致无入口
            out.append({"name": fn, "status": "corrupt",
                        "current": None, "pipeline_id": None, "updated_at": None,
                        "awaiting": False, "blocked": False,
                        "running": fn in _running})
    return out


def _task_detail(workdir: str, name: str) -> dict:
    d = os.path.join(workdir, name)
    o = Orchestrator(d)
    if not o.load():
        raise FileNotFoundError(f"任务不存在: {name}")
    st = o.state
    nodes = []
    for n in o.pipeline.nodes:
        ns = st.get("node_states", {}).get(n.id, {})  # 旧版 state 缺键时降级展示，不 500
        nodes.append({
            "id": n.id, "name": n.name, "phase": ns.get("phase", "pending"),
            "bounces": ns.get("bounces", 0),
            "entry_bounces": ns.get("entry_bounces", 0),
            "attempts": ns.get("attempts", 0),
            "needs_approval": n.approval.required,
            # 三段统计只读视图；旧任务/未采集/损坏时为 None，前端隐藏不报错
            "stats": nodestats.for_ui(st, n.id),
        })
    pa = st.get("pending_approval")
    prompt = ""
    if pa:
        # 定级和基线签批把给人看的话写在挂起项上；普通节点审批仍用模板里的 prompt。
        stored = pa.get("prompt")
        if isinstance(stored, str) and stored.strip():
            prompt = stored
        else:
            node = o.pipeline.node(pa.get("node")) if isinstance(pa.get("node"), str) else None
            prompt = node.approval.prompt if node else ""
    # 澄清窗口信息（只读，不落盘）
    clarify = None
    try:
        ctx = o._clarify_context()
        if ctx:
            clarify = {"node": ctx["node"].id, "mode": ctx["mode"]}
    except (KeyError, RuntimeError):
        clarify = None
    return {
        "name": name,
        "status": st.get("status", "unknown"),
        "current": st.get("current", ""),
        "pipeline_name": o.pipeline.name, "nodes": nodes,
        "blocked_reason": st.get("blocked_reason"),
        "push_failed": st.get("push_failed"),
        "stray_delivery": st.get("stray_delivery"),
        "commit_failed": st.get("commit_failed"),
        "git_delivery_disabled": st.get("git_delivery_disabled"),
        "git_delivery_disabled_reason": st.get("git_delivery_disabled_reason"),
        "pending_approval": ({"node": pa.get("node"), "summary": pa.get("summary"),
                              "prompt": prompt} if pa else None),
        "history_tail": [
            {k: e.get(k) for k in ("ts", "event", "node", "reason", "bounces", "entry_bounces")}
            for e in st.get("history", [])[-25:]
            if isinstance(e, dict)],
        "clarify": clarify,
        "dialog_tail": o.dialog_tail(clarify["node"]) if clarify else [],
        "chat_error": _chat_err.get(name),
        "run_error": _run_err.get(name),
        "running": name in _running,
        "steer_enabled": _steer_enabled(),
        "steer_queued": _steer_queued(d),
    }


def _is_running(name: str) -> bool:
    with _running_lock:
        return name in _running


_chat_err: dict = {}  # 任务名 → 最近一次后台对话失败信息（前端展示用，下次 chat 启动时清除）
_run_err: dict = {}  # 任务名 → 最近一次后台推进失败信息（前端展示用，下次 run 启动时清除）


def _live_bucket(name: str) -> dict:
    with _live_lock:
        bucket = _live.get(name)
        if bucket is None:
            bucket = {"seq": 0, "events": [], "chars": 0, "cond": threading.Condition()}
            _live[name] = bucket
        return bucket


def live_append(name: str, kind: str, text: str, **extra) -> None:
    """追加一条滚动事件。单条超长时保留末尾，总长度超出环形上限时丢掉最旧的。"""
    body = text or ""
    if kind != "reset" and not body:
        return
    if len(body) > LIVE_MAX_CHARS:
        body = body[-LIVE_MAX_CHARS:]
    bucket = _live_bucket(name)
    with bucket["cond"]:
        bucket["seq"] += 1
        ev = {"id": bucket["seq"], "kind": kind, "text": body}
        for key, value in extra.items():
            if value is not None:
                ev[key] = value
        bucket["events"].append(ev)
        bucket["chars"] += len(body)
        while len(bucket["events"]) > 1 and bucket["chars"] > LIVE_MAX_CHARS:
            old = bucket["events"].pop(0)
            bucket["chars"] -= len(old.get("text") or "")
        bucket["cond"].notify_all()


def live_wait(name: str, after: int, timeout: float = 15) -> list:
    """返回 id 大于 after 的事件。没有新事件时最多等 timeout 秒。"""
    bucket = _live_bucket(name)
    with bucket["cond"]:
        def newer():
            return [dict(ev) for ev in bucket["events"] if ev["id"] > after]
        items = newer()
        if not items:
            bucket["cond"].wait(timeout)
            items = newer()
        return items


def live_clear(name: str) -> None:
    """重置任务时清掉滚动区，并推一条 reset 让已连接的页面跟着清空。"""
    with _live_lock:
        bucket = _live.get(name)
    if bucket is None:
        return
    with bucket["cond"]:
        bucket["events"].clear()
        bucket["chars"] = 0
        bucket["seq"] += 1
        ev = {"id": bucket["seq"], "kind": "reset", "text": ""}
        bucket["events"].append(ev)
        bucket["cond"].notify_all()


_PHASE_TEXT = {
    "entry": "正在跑入口门禁",
    "subject": "正在跑主体会话",
    "exit": "正在跑出口门禁",
    "clarify": "正在澄清对话",
    "adversarial": "正在核对验证记录",
}


def _present_event(name: str, event: str, kw: dict) -> None:
    if event == "ui_phase":
        phase = str(kw.get("phase") or "")
        label = _PHASE_TEXT.get(phase, phase or "进行中")
        node = kw.get("name") or kw.get("node") or ""
        live_append(name, "phase", f"{label} · {node}" if node else label)
        return
    notes = {
        "entry_pass": "入口门禁通过",
        "entry_fail": "入口门禁未通过",
        "exit_pass": "出口门禁通过",
        "exit_fail": "出口门禁未通过",
        "subject_done": "主体会话结束",
        "steer_fallback": "补充未赶上工具返回，已补送一轮",
        "steer_fallback_fail": "补充补送失败，已放回队列",
        "blocked": "流程已阻塞",
    }
    if event not in notes:
        return
    extra = ""
    if event == "subject_done":
        extra = "（成功）" if kw.get("ok") else "（未成功）"
    reason = kw.get("reason")
    if event in ("entry_fail", "exit_fail", "blocked") and reason:
        extra = "：" + str(reason)[:180]
    live_append(name, "phase", notes[event] + extra)


def _stream_factory(name: str):
    """Web 滚动区：工具行全留，助手文本按环形缓冲保留，不套用终端 800 字截断。"""

    def factory(role: str):
        def on_stream(ev: dict) -> None:
            kind = ev.get("kind")
            if kind == "tool":
                tool = ev.get("name") or "?"
                summary = ev.get("summary") or ""
                live_append(name, "tool", f"⋯ {tool}" + (f" {summary}" if summary else ""), role=role)
            elif kind == "text":
                text = ev.get("text") or ""
                if text:
                    live_append(name, "text", text, role=role)
        return on_stream

    return factory


def _bind_present(orch: Orchestrator, name: str) -> None:
    orch.on_stream = _stream_factory(name)
    orch.on_event = lambda event, **kw: _present_event(name, event, kw)


def _steer_enabled() -> bool:
    try:
        from . import steer as steer_mod
        return steer_mod.enabled()
    except OSError:
        return False


def _steer_queued(root: str) -> int:
    try:
        from . import steer as steer_mod
        if not steer_mod.enabled():
            return 0
        return steer_mod.queued_count(root)
    except OSError:
        return 0


def _bg_chat(root: str, name: str, node_id: str, message: str) -> None:
    """澄清对话后台轮：与 run 同一互斥域（_running 注册表），锁内执行。"""
    try:
        o = Orchestrator(root)
        if o.load():
            _bind_present(o, name)
            live_append(name, "note", "你：" + message.strip()[:500])
            r = o.chat(node_id, message)
            if not r.get("ok"):
                with _running_lock:
                    # P2-15：按落盘事实分叉——拒绝路径（指纹不一致/节点不可澄清等）
                    # 发生在落盘之前，「未发出、未落盘」为真；会话故障路径
                    # （persisted=True）user+assistant 行已写入 dialog 文件，
                    # 曾一律谎称「未落盘」诱使操作员重发同消息（重复进 dialog）
                    if r.get("persisted"):
                        _chat_err[name] = (f"澄清会话失败：{r.get('error', '')}"
                                           f"（消息与会话故障回复已落盘对话记录，"
                                           f"请勿重发同一消息，可换措辞继续）")
                    else:
                        # P2-18：消息未发出未落盘——输入框此时已被清空，
                        # 「复制内容后重试」对不存在的内容是假指引。错误里
                        # 携带被拒原文，前端呈现时回填输入框（真·不丢失）
                        _chat_err[name] = {
                            "error": f"澄清对话失败：{r.get('error', '')}"
                                     f"（消息未发出、未落盘，已回填输入框，可修改后重试）",
                            "rejected_message": message,
                        }
                print(f"[gdd-ui] 任务 {name} 澄清对话失败: {r.get('error')}", flush=True)
                live_append(name, "phase", "澄清会话失败")
    except Exception as e:
        with _running_lock:
            _chat_err[name] = f"澄清对话异常: {e}"
        print(f"[gdd-ui] 任务 {name} 澄清对话异常: {e}", flush=True)
    finally:
        with _running_lock:
            _running.pop(name, None)


def _do_action(workdir: str, name: str, op: str, feedback: str = "", extra: dict | None = None) -> dict:
    d = os.path.join(workdir, name)
    # abort/reset 的 _running 防护前置：reset 不 load 状态（见下），须先于此判
    if op in ("abort", "reset") and _is_running(name):
        return {"ok": False, "error": "该任务正在运行中，不能中止/重置；请等待本轮推进结束再操作"}
    # reset 前置：不 load 状态（损坏/被篡改的 state.json 会让 load 抛错，导致 reset 分支不可达、
    # 任务在列表中也不可见，用户无法自救）。reset 只依赖 .gdd 目录本身。
    if op == "reset":
        gdd = os.path.join(d, ".gdd")
        if os.path.isdir(gdd):
            o = Orchestrator(d)
            if not o._acquire_lock():  # 防删 lock 致互斥失效（同 CLI reset）
                return {"ok": False, "error": "有 gdd 进程正在运行（.gdd/lock 被占用），请先等待或中止"}
            try:
                shutil.rmtree(gdd)
            finally:
                o._release_lock()
        # 清进程内错误横幅：否则同名新任务详情页残留上一实例的 chat/run 错误
        with _running_lock:
            _chat_err.pop(name, None)
            _run_err.pop(name, None)
        live_clear(name)
        return {"ok": True}
    if op == "steer":
        # 跑在 load() 之前：主体持锁写 state 时，补充仍然入队。不拿 .gdd/lock。
        if not os.path.isfile(os.path.join(d, ".gdd", "state.json")):
            raise FileNotFoundError(f"任务不存在: {name}")
        from . import steer as steer_mod
        if not steer_mod.enabled():
            return {"ok": False, "error": "GDD_STEER=off，未入队。"}
        text = str((extra or {}).get("message", "")).strip()
        if not text:
            return {"ok": False, "error": "补充内容为空"}
        if len(text) > steer_mod.MAX_CHARS:
            return {"ok": False, "error": f"消息过长（≤{steer_mod.MAX_CHARS} 字符），请拆分发送"}
        try:
            queued = steer_mod.append(d, text)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        hint = steer_mod.delivery_hint(d)
        preview = text if len(text) <= 180 else text[:180] + "…"
        live_append(name, "note", f"✉ {preview} — {hint}")
        return {"ok": True, "queued": queued, "hint": hint}
    o = Orchestrator(d)
    if not o.load():
        raise FileNotFoundError(f"任务不存在: {name}")

    if op == "run":
        st = o.state["status"]
        if st == "awaiting_approval":
            return {"ok": False, "error": "有待审批项，请先批准或驳回（否则自动续跑会被空调用挤掉）"}
        if st == "blocked":
            return {"ok": False, "error": "任务被阻塞，请用「重试责任节点」（会自动续跑）"}
        if st in ("completed", "aborted"):
            return {"ok": False, "error": f"任务已终态（{st}），如需重跑请先重置"}
        with _running_lock:
            if name in _running:
                return {"ok": False, "error": "该任务已在运行中"}
            _run_err.pop(name, None)  # 新一轮推进开始，清除旧错误
            t = threading.Thread(target=_bg_run, args=(d, name), daemon=True)
            _running[name] = t
            try:
                t.start()
            except Exception:
                _running.pop(name, None)  # start 失败回滚注册，避免永久占位
                raise
        return {"ok": True, "started": True}

    if op == "approve":
        if o.state["status"] != "awaiting_approval" or not o.state.get("pending_approval"):
            return {"ok": False, "error": "当前没有待审批项"}
        if not o.resolve_approval(True):
            return {"ok": False, "error": o.last_refusal or "审批已失效（可能已被处理），请刷新后重试"}
        ok = True
    elif op == "reject":
        if o.state["status"] != "awaiting_approval" or not o.state.get("pending_approval"):
            return {"ok": False, "error": "当前没有待审批项"}
        # 驳回（含触达 max_bounces→blocked）引擎返回 True；False 仅真拒绝
        if not o.resolve_approval(False, feedback=feedback or "驳回（未填写反馈）"):
            return {"ok": False, "error": o.last_refusal or "驳回被拒绝，请刷新后重试"}
        ok = True
    elif op == "retry":
        ok = o.retry()
        if not ok:
            return {"ok": False, "error": o.last_refusal or "无法重试：任务不处于可重试状态（仅 blocked 态可重试）"}
    elif op == "chat":
        node_id = str((extra or {}).get("node", ""))
        message = str((extra or {}).get("message", ""))
        if not message.strip():
            return {"ok": False, "error": "消息为空"}
        if len(message) > 20000:
            return {"ok": False, "error": "消息过长（≤20000 字符），请拆分发送"}
        with _running_lock:
            if name in _running:
                return {"ok": False, "error": "任务正在运行中（本轮推进或对话进行中）"}
            _chat_err.pop(name, None)  # 新一轮对话开始，清除旧错误
            t = threading.Thread(target=_bg_chat, args=(d, name, node_id, message), daemon=True)
            _running[name] = t
            try:
                t.start()
            except Exception:
                _running.pop(name, None)  # start 失败回滚注册，避免永久占位
                raise
        return {"ok": True, "started": True}
    elif op == "clarify_done":
        node_id = str((extra or {}).get("node", ""))
        r = o.clarify_done(node_id)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error", "结束澄清失败")}
        if r.get("recheck"):
            # 重验：主体 resume 收尾 + exit 门禁重跑，自动续跑。
            # 对话线程在 flock 释放后到 _running 注销前有微秒级窗口，短暂等待其退出
            # 再启动重验；超时不静默丢弃，如实告知前端（可手动「继续推进」兜底）。
            started = False
            deadline = time.time() + 5
            while time.time() < deadline:
                with _running_lock:
                    if name not in _running:
                        break
                time.sleep(0.1)
            with _running_lock:
                if name not in _running:
                    t = threading.Thread(target=_bg_run, args=(d, name), daemon=True)
                    _running[name] = t
                    try:
                        t.start()
                        started = True
                    except Exception:
                        _running.pop(name, None)
                        raise
            if not started:
                return {"ok": True, **r, "recheck_started": False,
                        "note": "重验未能自动启动（对话线程尚未退出），请稍后点击「继续推进」"}
        return {"ok": True, **r}
    elif op == "abort":
        # P2-17：终态 no-op 的 False 不得报 ok:true（CLI 同调用 exit 1，
        # 双前端对称——「未发生的动作不得宣称已发生」）
        if not o.abort():
            return {"ok": False, "error": "已处于终态，无需中止"}
        ok = True
    else:
        return {"ok": False, "error": f"未知操作: {op}"}
    # approve/reject/retry 后自动续跑（与 CLI 行为一致）
    if op in ("approve", "reject", "retry") and ok:
        with _running_lock:
            if name not in _running and o.state["status"] not in ("completed", "aborted", "blocked"):
                t = threading.Thread(target=_bg_run, args=(d, name), daemon=True)
                _running[name] = t
                try:
                    t.start()
                except Exception:
                    _running.pop(name, None)
                    raise
    return {"ok": bool(ok)}


def _bg_run(root: str, name: str) -> None:
    o = None
    try:
        o = Orchestrator(root)
        if o.load():
            _bind_present(o, name)
            o.run()
        return
    except Exception as e:
        err = str(e)
        print(f"[gdd-ui] 任务 {name} run 线程异常: {err}", flush=True)
        from .orchestrator import LockBusyError
        if isinstance(e, LockBusyError):
            # 外部 CLI 进程持锁：不动状态，避免 stale 覆盖外部进程的推进。
            # 但不得静默死——前端无错误通道会让操作员以为推进已发生（P2-12）。
            # 此前的 approve/reject/retry 已落盘生效，状态机推进未丢失，
            # 只是本轮自动续跑让位给外部进程；如实告知。
            with _running_lock:
                _run_err[name] = ("自动推进未执行：另一 gdd 进程持锁（外部 CLI 正在推进或对话），"
                                  "状态未受影响。若此前执行过审批/驳回/重试，其变更已生效；"
                                  "可待外部进程结束后手动「继续推进」")
            return
        # 中途异常：锁内重读磁盘最新状态，仅当仍处 running 时置 blocked。
        # P2-17：曾「锁前 load、锁后写」——load 与 acquire 之间的窗口里外部
        # 进程写入的 awaiting_approval+pending_approval+history，随后被锁内
        # 基于 stale 副本的 _save 覆写为「blocked/run 线程异常」，人工门禁
        # 武装态被伪装成 blocked。load 必须在锁内做：读到的一定是持锁者
        # 眼中的最新态；锁内写与外部写天然互斥。
        try:
            o2 = Orchestrator(root)
            if o2._acquire_lock():
                try:
                    if o2.load() and o2.state.get("status") == "running":
                        o2.state["status"] = "blocked"
                        o2.state["blocked_reason"] = f"run 线程异常: {e}"
                        o2._save()
                finally:
                    o2._release_lock()
            # 拿不到锁：外部进程正在推进——不动状态（其自身会推进/兜底），
            # 线程内异常仅记日志
        except Exception:
            print(f"[gdd-ui] 任务 {name} 异常状态写入失败，请人工检查 .gdd/: {e}", flush=True)
    finally:
        with _running_lock:
            _running.pop(name, None)


# ---------------------------------------------------------------- HTML

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>GDD 控制台</title>
<style>
:root{--bg:#0f1115;--panel:#161a22;--line:#242a36;--fg:#e6e9f0;--dim:#8b93a5;
--blue:#4f8ef7;--green:#34c77b;--amber:#f5b544;--red:#ef5b5b;--orange:#e8833a;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"PingFang SC",sans-serif;display:flex;height:100vh}
aside{width:230px;background:var(--panel);border-right:1px solid var(--line);padding:16px 12px;overflow-y:auto;flex-shrink:0}
aside h1{font-size:16px;margin-bottom:4px} aside .sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.sec{margin-top:18px}.sec .t{color:var(--dim);font-size:12px;letter-spacing:.1em;margin-bottom:6px;padding-left:8px}
.item{display:block;width:100%;text-align:left;padding:8px 10px;border:0;border-radius:8px;background:none;color:var(--fg);cursor:pointer;font-size:13px;margin-bottom:2px}
.item:hover{background:#1d2330}.item.on{background:#233049;color:#cfe0ff}
.item .st{float:right;font-size:11px}
.st.run{color:var(--blue)} .st.ok{color:var(--green)} .st.wait{color:var(--amber)}
.st.block{color:var(--red)} .st.abort{color:var(--dim)}
main{flex:1;overflow-y:auto;padding:24px 28px}
button{border:1px solid var(--line);background:#1d2330;color:var(--fg);border-radius:8px;padding:7px 14px;cursor:pointer;font-size:13px}
button:hover{border-color:#3a4358} button.pri{background:var(--blue);border-color:var(--blue);color:#fff}
button.ok{background:var(--green);border-color:var(--green);color:#04150b}
button.warn{background:var(--amber);border-color:var(--amber);color:#241a03}
button.danger{color:var(--red);border-color:#5b2a2a}
button:disabled{opacity:.4;cursor:not-allowed}
h2{font-size:18px;margin-bottom:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.flow{display:flex;flex-wrap:wrap;gap:10px;align-items:stretch}
.node{background:#1a1f2a;border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-width:130px;position:relative}
.node .nm{font-size:13px;font-weight:600;margin-bottom:2px}
.node .ph{font-size:11px;color:var(--dim)}
.node .bd{font-size:11px;color:var(--dim)}
.node.cur{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue)}
.node.cur .ph{color:var(--blue)}
.node.working .ph{color:var(--blue)}
.node.working::after{content:"";position:absolute;top:-4px;right:-4px;width:10px;height:10px;border-radius:50%;background:var(--blue);animation:pulse 1.2s infinite}
.node.approval{border-color:var(--amber);box-shadow:0 0 12px rgba(245,181,68,.35);animation:glow 1.6s infinite}
.node.approval .ph{color:var(--amber);font-weight:700}
.node.exit_retry{border-color:var(--orange)} .node.exit_retry .ph{color:var(--orange)}
.node.done{border-color:#2b5c42} .node.done .ph{color:var(--green)}
.node.blocked{border-color:var(--red)} .node.blocked .ph{color:var(--red)}
.node .flag{position:absolute;top:-7px;left:8px;font-size:10px;background:var(--amber);color:#241a03;border-radius:4px;padding:0 5px}
.node.has-total{padding-bottom:24px}
.node .node-total{position:absolute;right:8px;bottom:7px;font-size:11px;font-weight:600;color:var(--green)}
.node:hover{z-index:30}
.node .node-pop{display:none;position:absolute;left:0;top:100%;width:max-content;max-width:min(460px,72vw);max-height:340px;overflow:auto;background:#10141c;border:1px solid var(--line);border-radius:10px;padding:10px 12px;box-shadow:0 12px 32px rgba(0,0,0,.45);z-index:60;font-size:12px;font-weight:400;color:var(--fg);text-align:left;white-space:normal;cursor:default}
.node:hover .node-pop{display:block}
.node-pop .np-t{color:var(--dim);font-size:11px;margin-bottom:2px}
.node-pop .np-seg{padding:6px 0}
.node-pop .np-seg+.np-seg{border-top:1px dashed var(--line)}
.node-pop .np-h{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline}
.node-pop .np-h b{min-width:38px;color:#cfe0ff}
.node-pop .np-s{display:flex;gap:10px;flex-wrap:wrap;color:var(--dim);padding:2px 0 0 12px}
.node-pop .np-s .k{color:#aab4c8}
@keyframes pulse{50%{opacity:.3}} @keyframes glow{50%{box-shadow:0 0 4px rgba(245,181,68,.2)}}
.arrow{align-self:center;color:var(--dim)}
textarea,input,select{background:#10141c;border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px;width:100%;font:13px/1.5 ui-monospace,Menlo,monospace}
textarea{min-height:120px;resize:vertical} .row{margin-bottom:12px} .row label{display:block;color:var(--dim);font-size:12px;margin-bottom:4px}
.two{display:flex;gap:12px}.two>div{flex:1}
.hint{color:var(--dim);font-size:12px}
.alert{border-radius:8px;padding:10px 12px;margin-bottom:12px;font-size:13px}
.alert.err{background:#2a1517;border:1px solid #5b2a2a;color:#f0a0a0}
.alert.warn{background:#2a2415;border:1px solid #5b512a;color:#f0d9a0}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;color:var(--dim)}
td{color:var(--fg)} td.ts{color:var(--dim)}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
.tag{display:inline-block;font-size:11px;border:1px solid var(--line);border-radius:6px;padding:1px 7px;color:var(--dim);margin-left:6px}
pre{white-space:pre-wrap;font:12px/1.5 ui-monospace,Menlo,monospace;color:#c9d3e3}
.empty{color:var(--dim);padding:40px;text-align:center}
#liveLog{max-height:360px;overflow-y:auto;margin-top:8px;padding:8px 10px;background:#10141c;border-radius:8px;font:12px/1.55 ui-monospace,Menlo,monospace}
#liveLog:empty::before{content:"启动后这里滚动显示入口门禁、主体会话和出口门禁。往上翻时不会被拽回底部。";color:var(--dim)}
.live-phase{color:var(--blue);margin-top:8px}
.live-tool{color:var(--amber)}
.live-text{color:#d5dced;white-space:pre-wrap}
.live-note{color:var(--green);margin-top:6px;white-space:pre-wrap}
.tabs{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
.tabs button.on{border-color:var(--blue);color:#cfe0ff}
.node.sel{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue)}
.tpl-summary{border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:12px}
.tpl-detail-h{margin-bottom:6px}
.tpl-kv{display:flex;gap:10px;margin:6px 0;align-items:flex-start}
.tpl-k{width:150px;flex-shrink:0;color:var(--dim);font-size:12px}
.tpl-v{flex:1;min-width:0}
.tpl-v pre{margin:0}
.tpl-list{margin:0 0 0 18px;color:var(--fg);font-size:12px}
.tpl-form .row{margin-bottom:10px}
.tpl-form textarea{min-height:72px}
.row label.tpl-check{display:flex;align-items:center;gap:8px;color:var(--fg);font-size:13px;margin-bottom:0}
.tpl-check input{width:auto;margin:0}
.tpl-list-row{display:flex;gap:8px;margin-bottom:6px;align-items:flex-start}
.tpl-list-row input{flex:1;min-width:0}
.tpl-list-row button{flex-shrink:0;padding:6px 10px}
.tpl-sub{color:var(--dim);font-size:12px;letter-spacing:.08em;margin:14px 0 6px;border-top:1px dashed var(--line);padding-top:10px}
</style></head><body>
<aside>
  <h1>GDD 控制台</h1><div class="sub">Goal Driven Development</div>
  <div class="sec"><div class="t">新建任务</div>
    <button class="pri item" style="text-align:center" onclick="showNew()">＋ 从模板启动任务</button>
  </div>
  <div class="sec"><div class="t">任务</div><div id="navTasks"></div></div>
  <div class="sec"><div class="t">模板</div><div id="navTpl"></div>
    <button class="item" data-action="new-template" onclick="showNewTpl()">＋ 新建模板</button>
  </div>
  <div class="sec"><div class="t">执行端</div>
    <button class="item" id="navSettings" onclick="showSettings()">⚙ 设置</button>
  </div>
</aside>
<main id="main"><div class="empty">加载中…</div></main>
<script>
const $=s=>document.querySelector(s);
let view={mode:'home'}, pollTimer=null, lastDetail=null;
let liveTask='', liveEs=null, liveEvents=[], liveStick=true, liveScrollTop=0;

async function api(path,opts){
  opts=opts||{};
  opts.headers=Object.assign({'Content-Type':'application/json','X-GDD-Request':'1'},opts.headers||{});
  const r=await fetch(path,opts);
  const j=await r.json().catch(()=>({ok:false,error:r.statusText}));
  if(!r.ok&&!j.error)j.error='HTTP '+r.status; return j;}

async function refreshNav(){
  const [ov]=await Promise.all([api('api/overview')]);
  if(!ov.ok)return;
  $('#navTasks').innerHTML=ov.tasks.map(t=>{
    const cls=t.running?'run':t.status==='completed'?'ok':t.awaiting?'wait':t.blocked?'block':t.status==='aborted'?'abort':t.status==='corrupt'?'block':'';
    const txt=t.running?'运行中':t.status==='completed'?'完成':t.awaiting?'待审批':t.blocked?'阻塞':t.status==='aborted'?'中止':t.status==='corrupt'?'损坏':'';
    return `<button class="item ${view.name===t.name&&view.mode==='task'?'on':''}" onclick="showTask('${enc(t.name)}')">${esc(t.name)}<span class="st ${cls}">${txt}</span></button>`;
  }).join('')||'<div class="hint" style="padding:8px">暂无任务</div>';
  $('#navTpl').innerHTML=ov.templates.map(t=>
    `<button class="item ${view.name===t.name&&view.mode==='tpl'?'on':''}" onclick="showTplA('${enc(t.name)}')">${esc(t.name)}${t.builtin?'<span class="st">内置</span>':''}</button>`
  ).join('');
}

function esc(s){return String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
// ---- 节点三段统计展示（spec §5）：方块右下角整数分钟 + 悬浮明细 ----
function statsMin1(s){return (Math.max(0,Number(s)||0)/60).toFixed(1)+'min'}
function statsTotalMin(s){return Math.round(Math.max(0,Number(s)||0)/60)+'min'}
function statsToken(total,status){
  if(status==='unavailable'||(status!=='partial'&&total==null))return '不可得';
  const n=Number(total)||0;
  return status==='partial'?n+'（部分不可得）':String(n);
}
function statsSessionRow(s){
  s=s||{};
  const kind=String(s.kind||'');
  const who=kind==='task'?(s.task_id?'task '+s.task_id:'task'):kind;
  return `<div class="np-s" data-kind="${esc(kind)}" data-task="${esc(s.task_id||'')}"><span class="k">${esc(who||'会话')}</span><span>${statsMin1(s.duration_s)}</span><span>工具 ${esc(String(s.tool_calls||0))}</span><span>LLM ${esc(String(s.llm_calls||0))}</span><span>token ${esc(statsToken(s.token_total,s.token_status))}</span></div>`;
}
function statsSegment(name,label,seg){
  seg=seg||{};
  const rows=(Array.isArray(seg.sessions)?seg.sessions:[]).map(statsSessionRow).join('');
  return `<div class="np-seg" data-seg="${esc(name)}"><div class="np-h"><b>${esc(label)}</b><span>${statsMin1(seg.duration_s)}</span><span>工具 ${esc(String(seg.tool_calls||0))}</span><span>LLM ${esc(String(seg.llm_calls||0))}</span><span>token ${esc(statsToken(seg.token_total,seg.token_status))}</span></div>${rows}</div>`;
}
function nodeStatsHtml(st){
  if(!st||typeof st!=='object'||!st.segments||typeof st.segments!=='object')return '';
  const order=[['entry','entry'],['subject','主体'],['exit','exit']];
  return `<div class="node-pop"><div class="np-t">耗时合计 ${statsMin1(st.total_s)}（entry + 主体 + exit）</div>${order.map(([k,label])=>statsSegment(k,label,st.segments[k])).join('')}</div>`;
}
function nodeHasStats(n){return !!(n&&n.phase==='done'&&n.stats&&typeof n.stats==='object'&&typeof n.stats.total_s==='number'&&n.stats.segments&&typeof n.stats.segments==='object')}
function nodeStatsBlock(n){
  if(!nodeHasStats(n))return '';  // 未完成/旧任务无统计：不显示总耗时
  return `<div class="node-total">${statsTotalMin(n.stats.total_s)}</div>${nodeStatsHtml(n.stats)}`;
}
function enc(s){return btoa(unescape(encodeURIComponent(String(s))))}  // onclick 安全传参（避开实体解码突破引号）
function dec(s){return decodeURIComponent(escape(atob(s)))}
function stopPoll(){if(pollTimer){clearInterval(pollTimer);pollTimer=null} closeLive()}
function closeLive(){if(liveEs){liveEs.close();liveEs=null} liveTask=''; liveEvents=[]; liveScrollTop=0; liveStick=true}
function bindLive(name){
  if(liveTask===name && liveEs) return;
  if(liveEs){liveEs.close();liveEs=null}
  liveTask=name; liveEvents=[]; liveScrollTop=0; liveStick=true;
  const es=new EventSource('api/task/'+encodeURIComponent(name)+'/live');
  liveEs=es;
  es.onmessage=(msg)=>{
    let ev; try{ev=JSON.parse(msg.data)}catch(e){return}
    if(ev.kind==='reset'){liveEvents=[]; liveScrollTop=0; liveStick=true; paintLive(true); return}
    liveEvents.push(ev);
    paintLive(false);
  };
}
function onLiveScroll(){
  const box=document.getElementById('liveLog');
  if(!box || box.dataset.painting==='1') return;
  liveStick = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
  liveScrollTop = box.scrollTop;
}
function paintLive(force){
  const box=document.getElementById('liveLog');
  if(!box) return;
  const rebuild = force || box.dataset.painted!=='1';
  box.dataset.painting='1';
  if(rebuild){
    box.textContent='';
    let last=null;
    for(const ev of liveEvents){
      if(ev.kind==='reset') continue;
      if(ev.kind==='text' && last && last.dataset.kind==='text' && last.dataset.role===(ev.role||'')){
        last.textContent += ev.text; continue;
      }
      const div=document.createElement('div');
      div.dataset.kind=ev.kind; div.dataset.role=ev.role||'';
      div.className='live-'+(ev.kind||'text');
      div.textContent=ev.text||'';
      box.appendChild(div); last=div;
    }
    box.dataset.painted='1';
    if(liveStick) box.scrollTop=box.scrollHeight;
    else box.scrollTop=liveScrollTop;
  } else {
    let last=box.lastElementChild;
    const start = Number(box.dataset.count||0);
    for(let i=start;i<liveEvents.length;i++){
      const ev=liveEvents[i];
      if(ev.kind==='reset') continue;
      if(ev.kind==='text' && last && last.dataset.kind==='text' && last.dataset.role===(ev.role||'')){
        last.textContent += ev.text; continue;
      }
      const div=document.createElement('div');
      div.dataset.kind=ev.kind; div.dataset.role=ev.role||'';
      div.className='live-'+(ev.kind||'text');
      div.textContent=ev.text||'';
      box.appendChild(div); last=div;
    }
    if(liveStick) box.scrollTop=box.scrollHeight;
  }
  box.dataset.count=String(liveEvents.length);
  box.dataset.painting='0';
}

async function showNew(){
  stopPoll();view={mode:'new'};
  const ov=await api('api/overview');
  const tpls=(ov.templates||[]).map(t=>`<option value="${esc(t.name)}">${esc(t.name)}${t.builtin?'（内置）':''}</option>`).join('');
  $('#main').innerHTML=`<h2>从模板启动新任务</h2><div class="card">
    <div class="two"><div class="row"><label>任务名</label><input id="nName" placeholder="visualization-of-layout">
    <span class="hint">一次迭代的简述。产物写在工作台/&lt;任务名&gt;/，代码检出在其中的 worktrees/。</span></div>
    <div class="row"><label>模板</label><select id="nTpl">${tpls}</select></div></div>
    <div class="row"><label>仓库</label><textarea id="nRepos" placeholder="ssh://git@host/gdd-harness.git&#10;ssh://git@host/other.git develop"></textarea>
    <span class="hint">内置 delivery 至少一行。每行一个地址，可选第二段基线分支。不填基线则用 master，没有再用 main。</span></div>
    <div class="row"><label>需求（一句话也行）</label><textarea id="nReq" placeholder="实现一个 TODO CLI，支持增删查，带 pytest 测试"></textarea></div>
    <div class="actions"><button class="pri" onclick="createTask()">创建并初始化</button>
    <span class="hint" style="align-self:center">创建后到任务页点「启动」</span></div>
    <div id="nErr"></div></div>`;
}
async function createTask(){
  const name=$('#nName').value.trim();
  const j=await api('api/tasks',{method:'POST',body:JSON.stringify({name,repos:$('#nRepos').value,template:$('#nTpl').value,requirement:$('#nReq').value})});
  if(!j.ok){$('#nErr').innerHTML=`<div class="alert err">${esc(j.error)}</div>`;return}
  showTask(enc(name));
}

async function showTask(name){
  stopPoll();view={mode:'task',name:dec(name)};
  bindLive(view.name);
  await renderTask(view.name);
  pollTimer=setInterval(()=>renderTask(view.name),2000);
}
async function renderTask(name){
  // 驳回反馈/澄清输入中：跳过本轮重绘，避免 2s 轮询打断/清空输入（两个输入框独立判断）
  const rejEl=document.getElementById('rejFb'), clEl=document.getElementById('clInput'), stEl=document.getElementById('steerInput');
  if((rejEl&&document.activeElement===rejEl)||(clEl&&document.activeElement===clEl)||(stEl&&document.activeElement===stEl))return;
  const rejVal=rejEl?rejEl.value:'', clVal=clEl?clEl.value:'', stVal=stEl?stEl.value:'';
  const j=await api('api/task/'+encodeURIComponent(name));
  if(!j.ok){$('#main').innerHTML=`<div class="alert err">${esc(j.error)}</div>
    <div class="actions"><button class="warn" onclick="act('reset')">↺ 重置任务（删除 .gdd，不可恢复）</button></div>`;stopPoll();return}
  // await 期间用户可能又在输入框打了字：以请求返回时的现值为准，防旧快照覆盖新输入
  const rejNow=document.getElementById('rejFb'), clNow=document.getElementById('clInput'), stNow=document.getElementById('steerInput');
  const rejVal2=(rejNow&&document.activeElement!==rejNow&&rejNow.value)?rejNow.value:rejVal;
  const clVal2=(clNow&&document.activeElement!==clNow&&clNow.value)?clNow.value:clVal;
  const stVal2=(stNow&&document.activeElement!==stNow&&stNow.value)?stNow.value:stVal;
  lastDetail=j; refreshNav();
  const st=j.status, running=j.running;
  const phTxt={pending:'待进入',working:'进行中',exit_retry:'门禁重试',adversarial_retry:'核对重试',exit_passed:'待审批',done:'已完成'};
  const flow=j.nodes.map((n,i)=>{
    let cls='';let flag='';
    const isCur=n.id===j.current;
    if(isCur){cls=esc(n.phase==='pending'?'cur':n.phase)}
    else if(n.phase==='done')cls='done';
    if(n.needs_approval&&n.phase!=='done')flag='需人工';
    let ph=esc(phTxt[n.phase]||n.phase);
    if((n.phase==='exit_passed'||n.phase==='working'||n.phase==='adversarial_retry')&&j.pending_approval&&j.pending_approval.node===n.id){cls='approval';ph='⏸ 等你审批'}
    const bd=[];
    if(n.bounces)bd.push(`打回${esc(String(n.bounces))}`);
    if(n.entry_bounces)bd.push(`entry打回${esc(String(n.entry_bounces))}`);
    const hasStats=nodeHasStats(n);
    return `${flag?`<div class="flag">${flag}</div>`:''}<div class="node ${cls}${hasStats?' has-total':''}"><div class="nm">${esc(n.name)}</div><div class="ph">${ph}</div>${bd.length?`<div class="bd">${bd.join(' · ')}</div>`:''}${nodeStatsBlock(n)}</div>${i<j.nodes.length-1?'<div class="arrow">→</div>':''}`;
  }).join('');
  let alertHtml='';
  if(j.blocked_reason)alertHtml+=`<div class="alert err">⛔ ${esc(j.blocked_reason)}</div>`;
  if(j.chat_error){
    const ce=j.chat_error;
    const ceMsg=typeof ce==='string'?ce:ce.error;
    alertHtml+=`<div class="alert err">💬 ${esc(ceMsg)}</div>`;
    if(ce&&ce.rejected_message){  // P2-18：被拒消息回填输入框，不丢
      setTimeout(()=>{const b=document.getElementById('clInput');
        if(b&&!b.value&&!b.dataset.busy)b.value=ce.rejected_message;},0);
    }
  }
  if(j.run_error)alertHtml+=`<div class="alert warn">⚠ ${esc(j.run_error)}</div>`;
  if(j.push_failed)alertHtml+=`<div class="alert warn">⚠ 交付推送失败：${esc(j.push_failed.reason||'')}（下次 run 自动补推）</div>`;
  if(j.stray_delivery)alertHtml+=`<div class="alert warn">⚠ 交付不完整：提交滞留 ${esc(j.stray_delivery.ref||'')}（节点 ${esc(j.stray_delivery.node||'')}），cherry-pick/merge 后重跑；确属本人工作/已处置时执行 gdd acknowledge 人工确认清除</div>`;
  if(j.commit_failed)alertHtml+=`<div class="alert warn">⚠ 交付提交失败（节点 ${esc(j.commit_failed.node||'')}）：${esc(j.commit_failed.reason||'')}</div>`;
  if(j.git_delivery_disabled)alertHtml+=`<div class="alert warn">⚠ git 交付已停用：${esc(j.git_delivery_disabled_reason||'未知原因')}；如需启用：修复原因后 reset 重开</div>`;
  if(j.pending_approval)alertHtml+=`<div class="card" style="border-color:var(--amber)"><b>⏸ 人工审批：${esc(j.pending_approval.node)}</b>${j.pending_approval.prompt?`<div class="hint" style="margin:4px 0 8px">${esc(j.pending_approval.prompt)}</div>`:''}<pre>${esc(j.pending_approval.summary)}</pre><div class="row" style="margin-top:10px"><textarea id="rejFb" placeholder="驳回时填写修改指引" style="min-height:60px"></textarea></div><div class="actions"><button class="ok" onclick="act('approve')" ${running?'disabled':''}>✓ 批准并继续</button><button class="warn" onclick="act('reject')" ${running?'disabled':''}>✗ 驳回打回</button>${running?'<span class="hint" style="align-self:center">运行中，待本轮结束后可操作</span>':''}</div></div>`;
  const canRun=!running&&st==='running';
  // 澄清对话区（当前节点 clarify 开启且处于可澄清窗口）
  let clarifyHtml='';
  if(j.clarify){
    const dlg=(j.dialog_tail||[]).map(d=>`<div style="margin:2px 0"><b style="color:${d.role==='user'?'var(--blue)':'var(--green)'}">${d.role==='user'?'你':'AI'}</b> <span class="hint">${esc(d.ts||'')}</span><pre style="margin:2px 0 8px">${esc(d.text)}</pre></div>`).join('');
    clarifyHtml=`<div class="card" style="border-color:var(--blue)"><b>💬 要点澄清：${esc(j.clarify.node)}</b>
      <span class="hint">${j.clarify.mode==='review'?'对产出追问；结束后将重过 exit 门禁':'节点启动前预对齐；结论固化到产物文档'}</span>
      <div style="max-height:340px;overflow-y:auto;margin:10px 0">${dlg||'<div class="hint">（暂无对话）</div>'}</div>
      <div class="row"><textarea id="clInput" placeholder="输入要点/追问" style="min-height:60px"></textarea></div>
      <div class="actions">
        <button class="pri" onclick="sendChat('${enc(j.clarify.node)}')" ${running?'disabled':''}>发送</button>
        <button class="warn" onclick="clarifyDone('${enc(j.clarify.node)}')" ${running?'disabled':''}>✓ 完成澄清${j.clarify.mode==='review'?'并重新验证':''}</button>
        ${running?'<span class="hint" style="align-self:center">对话进行中…</span>':''}
      </div></div>`;
  }
  const btns=`<div class="actions">
    <button class="pri" onclick="act('run')" ${!canRun?'disabled':''}>${running?'运行中…':'▶ '+(st==='running'?'继续推进':'启动')}</button>
    ${st==='blocked'&&!running?'<button class="warn" onclick="act(\'retry\')">↻ 重试责任节点</button>':''}
    ${!['completed','aborted'].includes(st)?`<button class="danger" onclick="act('abort')" ${running?'disabled':''}>中止</button>`:''}
    <button class="danger" onclick="if(confirm('重置将删除 .gdd/ 全部进度（git 交付分支保留），确定？'))act('reset')" ${running?'disabled':''}>重置（删 .gdd）</button></div>`;
  const curNode=(j.nodes||[]).find(n=>n.id===j.current);
  const showSteer=!!(curNode&&curNode.phase==='working');
  let steerBox='';
  if(showSteer){
    if(j.steer_enabled===false){
      steerBox='<div class="hint" style="margin-top:8px">GDD_STEER=off，补充已关闭。</div>';
    } else {
      const q=j.steer_queued?`<span class="hint" style="align-self:center">队列中 ${esc(String(j.steer_queued))} 条未送达</span>`:'';
      steerBox=`<div class="row" style="margin-top:10px"><textarea id="steerInput" placeholder="走向偏了就写在这里，与命令行 gdd steer 同一条队列" style="min-height:60px"></textarea></div>
        <div class="actions"><button class="pri" onclick="sendSteer()">补充给模型</button>${q}</div>
        <div class="hint">送达时机与命令行 gdd steer 相同：主体还在跑时，下一次工具返回后送达；出口门禁期间改为下一次主体会话。不改相位、不消耗打回。</div>`;
    }
  }
  const liveHtml=`<div class="card"><b style="color:var(--dim);font-size:12px">运行进度</b><div id="liveLog" onscroll="onLiveScroll()"></div>${steerBox}</div>`;
  const hist=(j.history_tail||[]).slice().reverse().map(h=>`<tr><td class="ts">${esc(h.ts||'')}</td><td>${esc(h.event)}</td><td>${esc(h.node||'')}</td><td>${esc(typeof h.reason==='string'?h.reason.slice(0,60):(h.reason==null?'':JSON.stringify(h.reason).slice(0,60)))}</td></tr>`).join('');
  $('#main').innerHTML=`<h2>${esc(name)} <span class="tag">${esc(j.pipeline_name||'')}</span><span class="tag">${esc(st)}${running?' · 运行中':''}</span></h2>
    ${alertHtml}
    <div class="card"><div class="flow">${flow}</div>${btns}</div>
    ${liveHtml}
    ${clarifyHtml}
    <div class="card"><b style="color:var(--dim);font-size:12px">最近事件</b><table style="margin-top:8px">${hist||'<tr><td class="ts">（无）</td></tr>'}</table></div>`;
  paintLive(true);
  const r2=document.getElementById('rejFb'), c2=document.getElementById('clInput'), s2=document.getElementById('steerInput');
  if(r2&&!r2.value&&rejVal2)r2.value=rejVal2;
  if(c2&&!c2.value&&clVal2)c2.value=clVal2;
  if(s2&&!s2.value&&stVal2)s2.value=stVal2;
}
async function act(op){
  if(op==='reset'&&!confirm('确认重置？将删除 .gdd 全部状态，不可恢复。'))return;
  const fb=op==='reject'?(document.getElementById('rejFb')?.value||''):'';
  const j=await api('api/task/'+encodeURIComponent(view.name)+'/action',{method:'POST',body:JSON.stringify({op,feedback:fb})});
  if(!j.ok){alert(j.error||'操作失败');return}
  if(view.mode==='task')renderTask(view.name);
  refreshNav();
}
async function sendChat(nodeId){
  nodeId=dec(nodeId);
  const box=document.getElementById('clInput');
  if(!box)return;
  const msg=box.value||'';
  if(!msg.trim()){alert('消息为空');return}
  if(box.dataset.busy==='1')return;  // 防连点重复提交
  box.dataset.busy='1';
  try{
    const j=await api('api/task/'+encodeURIComponent(view.name)+'/action',{method:'POST',body:JSON.stringify({op:'chat',node:nodeId,message:msg})});
    if(!j.ok){alert(j.error||'发送失败（输入内容已保留，请复制后重试）');return}
    if(!j.started){alert('发送未被受理（任务可能已在运行）：'+(j.error||'输入内容已保留，请稍后重试'));return}
    // 仅在后台对话线程确认受理（started）后清空：窗口关闭/完整性拒绝等
    // 不落盘场景下用户输入不丢失（P2-18）
    box.value='';box.blur();
    renderTask(view.name);  // 显示对话中状态（轮询刷新回复）
  }catch(e){alert('网络异常，请重试：'+e)}
  finally{box.dataset.busy='0'}
}
async function sendSteer(){
  const box=document.getElementById('steerInput');
  if(!box)return;
  const msg=box.value||'';
  if(!msg.trim()){alert('补充内容为空');return}
  if(box.dataset.busy==='1')return;
  box.dataset.busy='1';
  try{
    const j=await api('api/task/'+encodeURIComponent(view.name)+'/action',{method:'POST',body:JSON.stringify({op:'steer',message:msg})});
    if(!j.ok){alert(j.error||'未入队（输入已保留）');return}
    box.value=''; box.blur();
    renderTask(view.name);
  }catch(e){alert('网络异常，请重试：'+e)}
  finally{box.dataset.busy='0'}
}
async function clarifyDone(nodeId){
  nodeId=dec(nodeId);
  const isReview=lastDetail&&lastDetail.clarify&&lastDetail.clarify.mode==='review';
  if(!confirm('结束澄清？'+(isReview?'产出将重过 exit 门禁。':'')))return;
  try{
    const j=await api('api/task/'+encodeURIComponent(view.name)+'/action',{method:'POST',body:JSON.stringify({op:'clarify_done',node:nodeId})});
    if(!j.ok){alert(j.error||'操作失败');return}
    if(j.note)alert(j.note);
    renderTask(view.name);refreshNav();
  }catch(e){alert('网络异常，请重试：'+e)}
}

async function showSettings(){
  stopPoll();view={mode:'settings'};
  const j=await api('api/settings');
  if(!j.ok){$('#main').innerHTML=`<div class="alert err">${esc(j.error||'读取设置失败')}</div>`;return}
  const keyHint=j.hasApiKey?`已保存（${esc(j.apiKeyMasked)}），留空表示不修改`:'未设置';
  $('#main').innerHTML=`<h2>执行端设置 <span class="tag">pi / OpenAI 兼容端点</span></h2>
    <div class="card">
      <div class="hint" style="margin-bottom:12px">设置全局生效，对保存之后新建的 agent 会话起作用；运行中的会话不热更新。API key 只写入本机设置文件，不会明文回显。</div>
      <div class="row"><label>端点（Base URL）</label><input id="setBase" value="${esc(j.baseUrl)}" placeholder="https://api.example.com/v1"></div>
      <div class="row"><label>API key</label><input id="setKey" type="password" autocomplete="new-password" placeholder="${keyHint}"></div>
      <div class="row"><label>模型</label><input id="setModel" value="${esc(j.model)}" placeholder="gpt-4o-mini"></div>
      <div class="actions">
        <button class="pri" onclick="saveSettings()">保存</button>
        <button onclick="testSettings()">测试连通性</button>
        <span class="hint" style="align-self:center">测试会向端点发一条最小 chat/completions 请求</span>
      </div>
      <div id="setMsg" style="margin-top:10px"></div>
    </div>`;
}
function settingsBody(includeKey){
  const body={baseUrl:($('#setBase')?.value||'').trim(),model:($('#setModel')?.value||'').trim()};
  const key=($('#setKey')?.value||'');
  if(includeKey&&key)body.apiKey=key;
  return body;
}
async function saveSettings(){
  const j=await api('api/settings',{method:'PUT',body:JSON.stringify(settingsBody(true))});
  const box=$('#setMsg');
  if(!j.ok){box.innerHTML=`<div class="alert err">${esc(j.error||'保存失败')}</div>`;return}
  box.innerHTML='<div class="alert warn" style="border-color:#2b5c42;background:#12241a;color:#9fe0b8">已保存。对之后新建的会话生效。</div>';
  const k=$('#setKey');if(k)k.value='';
  setTimeout(showSettings,600);
}
async function testSettings(){
  const box=$('#setMsg');
  box.innerHTML='<div class="hint">测试中…</div>';
  const j=await api('api/settings/test',{method:'POST',body:JSON.stringify(settingsBody(false))});
  if(!j.ok){box.innerHTML=`<div class="alert err">${esc(j.error||'测试失败')}</div>`;return}
  box.innerHTML=`<div class="alert warn" style="border-color:#2b5c42;background:#12241a;color:#9fe0b8">连通成功（模型 ${esc(j.model)}）${j.sample?`：${esc(String(j.sample).slice(0,120))}`:''}</div>`;
}

async function showTplA(b64){showTpl(dec(b64))}

// ---------- 模板可视化（T1）：JSON 客户端解析 + 节点流/详情 + 源码视图 ----------
const TPL_MARKS=[['clarify','澄清'],['review','评审'],['adversarial','对抗'],['baseline_marker','git 基线']];
const TPL_NODE_KEYS=['id','name','description','entry','exit','approval','subject','max_bounces',
  'baseline_marker','clarify','adversarial','review','session_group','mcp','skills'];

// ---------- 结构化表单（T2）：字段清单、控件生成与收集 ----------
// 严格对应 spec.md 5.2；节点 id 只读展示，mcp/skills 不在清单内（只读摘要 + 源码模式）。
const TPL_FIELDS={
  pipeline:['name','description','session_preset','git_delivery.enabled','git_delivery.branch','git_delivery.remote'],
  node:['name','description','subject.goal','subject.instructions','subject.max_turns',
    'approval.required','approval.prompt','clarify','max_bounces','baseline_marker',
    'adversarial','review','session_group',
    'entry.goal','entry.ai_check','entry.change_aware','entry.artifacts','entry.commands',
    'exit.goal','exit.ai_check','exit.change_aware','exit.artifacts','exit.commands']
};
const TPL_PRESETS=['independent','clarify-design','shared'];
const TPL_INT_FIELDS=['subject.max_turns','max_bounces'];

function tplDeep(v){return v==null?v:JSON.parse(JSON.stringify(v))}
function tplGet(o,path){
  let cur=o;
  for(const k of String(path).split('.')){
    if(cur==null||typeof cur!=='object'||Array.isArray(cur))return undefined;
    cur=cur[k];
  }
  return cur;
}
function tplSet(o,path,v){
  const ks=String(path).split('.');let cur=o;
  for(let i=0;i<ks.length-1;i++){
    if(cur[ks[i]]==null||typeof cur[ks[i]]!=='object'||Array.isArray(cur[ks[i]]))cur[ks[i]]={};
    cur=cur[ks[i]];
  }
  cur[ks[ks.length-1]]=v;
}
function tplDel(o,path){
  const ks=String(path).split('.');let cur=o;
  for(let i=0;i<ks.length-1;i++){
    if(cur==null||typeof cur!=='object'||Array.isArray(cur))return;
    cur=cur[ks[i]];
  }
  if(cur!=null&&typeof cur==='object'&&!Array.isArray(cur))delete cur[ks[ks.length-1]];
}
function tplTextControl(field,val,multiline,placeholder){
  const v=val==null?'':String(val),ph=placeholder==null?'':String(placeholder);
  return multiline
    ?`<textarea data-field="${esc(field)}" placeholder="${esc(ph)}">${esc(v)}</textarea>`
    :`<input data-field="${esc(field)}" value="${esc(v)}" placeholder="${esc(ph)}">`;
}
function tplCheckControl(field,on,label){
  return `<label class="tpl-check"><input type="checkbox" data-field="${esc(field)}"${tplBool(on)?' checked':''}> ${esc(label)}</label>`;
}
function tplFieldRow(field,label,control){
  return `<div class="row"><label>${esc(label)}</label>${control}</div>`;
}

// ---- 流水线级表单（控件均由 TPL_FIELDS.pipeline 生成）----
function tplPipelineField(p,data){
  const f='pipeline.'+p,gd=tplObj(data.git_delivery);
  if(p==='session_preset'){
    const cur=tplStr(data.session_preset,'independent');
    const opts=TPL_PRESETS.map(v=>`<option value="${v}"${cur===v?' selected':''}>${v}</option>`).join('')
      +(TPL_PRESETS.includes(cur)?'':`<option value="${esc(cur)}" selected>${esc(cur)}</option>`);
    return tplFieldRow(p,p,`<select data-field="${esc(f)}">${opts}</select>`);
  }
  if(p==='git_delivery.enabled'){
    const on=gd.enabled===undefined?true:tplBool(gd.enabled);
    return `<div class="row">${tplCheckControl(f,on,'启用 git 交付')}</div>`;
  }
  if(p==='description')return tplFieldRow(p,p,tplTextControl(f,tplStr(data.description,''),true));
  if(p==='git_delivery.branch')return tplFieldRow(p,p,tplTextControl(f,tplStr(gd.branch,'gdd/delivery')));
  if(p==='git_delivery.remote')return tplFieldRow(p,p,tplTextControl(f,tplStr(gd.remote,''),false,'空 = 自动探测首个 remote'));
  return tplFieldRow(p,p,tplTextControl(f,tplStr(data[p],'')));
}
function tplPipelineForm(data){
  const fields={};
  TPL_FIELDS.pipeline.forEach(p=>{fields[p]=tplPipelineField(p,data)});
  return `<div class="card tpl-form" data-form="pipeline">
    <div class="tpl-detail-h"><b>流水线</b> <span class="tag" data-readonly="pipeline-id">${esc(tplStr(data.id,'(缺少 id)'))}</span><span class="hint">id 只读（源码模式可改）</span></div>
    <div class="two"><div>${fields['name']||''}${fields['session_preset']||''}</div>
      <div>${fields['git_delivery.enabled']||''}${fields['git_delivery.branch']||''}${fields['git_delivery.remote']||''}</div></div>
    ${fields['description']||''}
  </div>`;
}

// ---- 节点级表单 ----
function tplArtifactsEditor(i,p,raw){
  const f='node.'+i+'.'+p,list=tplArr(raw);
  const rows=list.map((a,j)=>{
    a=tplObj(a);
    return `<div class="tpl-list-row" data-art-row data-art-index="${j}">
      <input data-field="${esc(f+'.'+j+'.path')}" data-art="path" value="${esc(tplStr(a.path,''))}" placeholder="path（支持 glob）">
      <input data-field="${esc(f+'.'+j+'.must_contain')}" data-art="must_contain" value="${esc(tplStr(a.must_contain,''))}" placeholder="must_contain 正则（可选）">
      <button type="button" onclick="tplDelRow(${i},'${p}',${j})">删除</button>
    </div>`;
  }).join('');
  return `<div class="row" data-field="${esc(f)}" data-list="artifacts"><label>${esc(p)}</label>
    <div data-art-rows>${rows||'<div class="hint">（空）</div>'}</div>
    <button type="button" onclick="tplAddArtifact(${i},'${p}')">＋ 添加产物</button></div>`;
}
function tplCommandsEditor(i,p,raw){
  const f='node.'+i+'.'+p,list=tplArr(raw);
  const rows=list.map((c,j)=>`<div class="tpl-list-row" data-cmd-row data-cmd-index="${j}">
    <input data-field="${esc(f+'.'+j)}" data-cmd="1" value="${esc(String(c))}" placeholder="shell 命令">
    <button type="button" onclick="tplDelRow(${i},'${p}',${j})">删除</button></div>`).join('');
  return `<div class="row" data-field="${esc(f)}" data-list="commands"><label>${esc(p)}</label>
    <div data-cmd-rows>${rows||'<div class="hint">（空）</div>'}</div>
    <button type="button" onclick="tplAddCommand(${i},'${p}')">＋ 添加命令</button></div>`;
}
function tplNodeField(i,p,n){
  const f='node.'+i+'.'+p,v=tplGet(n,p);
  if(p.endsWith('.artifacts'))return tplArtifactsEditor(i,p,v);
  if(p.endsWith('.commands'))return tplCommandsEditor(i,p,v);
  if(p==='name'){
    // 与 NodeSpec.from_dict 的 name=d.get("name", d["id"]) 对齐：缺失时预填 id，
    // 避免“打开即保存”把「缺失→运行时回退 id」写成显式空名字。
    const nm=(v===undefined||v===null)?tplStr(n.id,''):String(v);
    return tplFieldRow(p,p,tplTextControl(f,nm));
  }
  if(TPL_INT_FIELDS.includes(p)){
    const def=p==='subject.max_turns'?80:3;
    return tplFieldRow(p,p,`<input type="number" step="1" data-field="${esc(f)}" value="${esc(String(v===undefined||v===null?def:v))}">`);
  }
  if(p==='approval.required'||p==='clarify'||p==='baseline_marker'||p==='adversarial'||p==='review'||p.endsWith('.change_aware')){
    return `<div class="row">${tplCheckControl(f,v,p)}</div>`;
  }
  if(p==='description'||p==='approval.prompt'||p==='subject.goal'||p==='subject.instructions'||p.endsWith('.goal')||p.endsWith('.ai_check')){
    return tplFieldRow(p,p,tplTextControl(f,tplStr(v,''),true));
  }
  return tplFieldRow(p,p,tplTextControl(f,tplStr(v,'')));
}
function tplFormDetail(el,n,i){
  const id=tplStr(n.id,'(缺少 id)'),nm=tplStr(n.name,id);
  const mcp=tplArr(n.mcp),skills=tplArr(n.skills);
  const unknown=Object.keys(n).filter(k=>!TPL_NODE_KEYS.includes(k));
  const marks=[];
  if(tplBool(tplObj(n.approval).required))marks.push('需人工审批');
  TPL_MARKS.forEach(([k,t])=>{if(tplBool(n[k]))marks.push(t)});
  const field=p=>tplNodeField(i,p,n);
  const top=TPL_FIELDS.node.filter(p=>!p.startsWith('entry.')&&!p.startsWith('exit.')).map(field).join('');
  const entry=TPL_FIELDS.node.filter(p=>p.startsWith('entry.')).map(field).join('');
  const exit=TPL_FIELDS.node.filter(p=>p.startsWith('exit.')).map(field).join('');
  const other=unknown.length
    ?`<div class="tpl-kv" style="margin-top:10px"><div class="tpl-k">其他字段（只读）</div><div class="tpl-v"><pre>${esc(JSON.stringify(Object.fromEntries(unknown.map(k=>[k,n[k]])),null,2))}</pre><div class="hint">未知/未展示字段保存时原样保留，可在源码模式查看。</div></div></div>`
    :'';
  el.innerHTML=`<div class="card tpl-detail tpl-form" data-node-detail="${esc(id)}" data-node-index="${i}">
    <div class="tpl-detail-h"><b>${esc(nm)}</b> <span class="tag" data-readonly="node-id">${esc(id)}</span>${marks.map(m=>`<span class="tag">${esc(m)}</span>`).join('')}</div>
    <div class="hint" style="margin-bottom:8px">节点 id 只读（源码模式可改）；mcp / skills 仅源码可改；未展示字段保存时原样保留。</div>
    ${top}
    <div class="tpl-sub">entry 门禁</div>${entry}
    <div class="tpl-sub">exit 门禁</div>${exit}
    ${tplRow('mcp',mcp.length?`<pre>${esc(JSON.stringify(mcp,null,2))}</pre><div class="hint">只读摘要；源码模式可修改</div>`:'<span class="hint">（空）</span>')}
    ${tplRow('skills',skills.length?`<pre>${esc(JSON.stringify(skills,null,2))}</pre><div class="hint">只读摘要；源码模式可修改</div>`:'<span class="hint">（空）</span>')}
    ${other}
  </div>`;
}

// ---- 表单收集：只覆盖表单字段；mcp/skills/未知键保持原对象引用，不重建 ----
function tplFieldEl(field){return document.querySelector('[data-field="'+field+'"]')}
function tplNodeData(i){
  const nodes=tplArr(window._tplData&&window._tplData.nodes),n=nodes[i];
  return (n&&typeof n==='object'&&!Array.isArray(n))?n:null;
}
function collectTplArtifacts(n,p,box){
  if(!box)return;
  const old=tplArr(tplGet(n,p));
  const rows=Array.from(box.querySelectorAll('[data-art-row]'));
  const out=rows.map((row,domIdx)=>{
    let idx=Number(row.dataset.artIndex);
    if(!Number.isInteger(idx)||idx<0||idx>=old.length)idx=domIdx;
    // 以原对象为基底浅克隆：只改 path/must_contain，未知子字段原样保留
    const base=(old[idx]&&typeof old[idx]==='object'&&!Array.isArray(old[idx]))?old[idx]:{};
    const obj=Object.assign({},base);
    const pe=row.querySelector('[data-art="path"]'),me=row.querySelector('[data-art="must_contain"]');
    obj.path=pe?String(pe.value):'';
    const mc=me?String(me.value):'';
    if(mc)obj.must_contain=mc;else delete obj.must_contain;
    return obj;
  });
  tplSet(n,p,out);
}
function collectTplCommands(n,p,box){
  if(!box)return;
  const rows=Array.from(box.querySelectorAll('[data-cmd-row]'));
  tplSet(n,p,rows.map(r=>{const el=r.querySelector('[data-cmd]');return el?String(el.value):''}));
}
function collectTplForm(){
  const errs=[],data=window._tplData;
  if(!data||typeof data!=='object'||Array.isArray(data))return errs;
  TPL_FIELDS.pipeline.forEach(p=>{
    const el=tplFieldEl('pipeline.'+p);
    if(!el)return;
    if(p==='git_delivery.enabled')tplSet(data,p,!!el.checked);
    else tplSet(data,p,String(el.value));
  });
  const nodes=tplArr(data.nodes);
  nodes.forEach((n,i)=>{
    if(!n||typeof n!=='object'||Array.isArray(n))return;
    TPL_FIELDS.node.forEach(p=>{
      const field='node.'+i+'.'+p;
      if(p.endsWith('.artifacts')){collectTplArtifacts(n,p,tplFieldEl(field));return}
      if(p.endsWith('.commands')){collectTplCommands(n,p,tplFieldEl(field));return}
      const el=tplFieldEl(field);
      if(!el)return;
      if(el.type==='checkbox'){tplSet(n,p,!!el.checked);return}
      if(TPL_INT_FIELDS.includes(p)){
        const s=String(el.value).trim();
        if(s===''){tplDel(n,p);return}
        if(!/^-?\d+$/.test(s)){errs.push('节点 '+(n.id||i)+' 的 '+p+' 必须是整数');return}
        tplSet(n,p,parseInt(s,10));return;
      }
      tplSet(n,p,String(el.value));
    });
  });
  return errs;
}
function tplAddArtifact(i,p){
  collectTplForm();
  const n=tplNodeData(i);if(!n)return;
  const ks=p.split('.');
  if(!n[ks[0]]||typeof n[ks[0]]!=='object'||Array.isArray(n[ks[0]]))n[ks[0]]={};
  const arr=tplArr(n[ks[0]][ks[1]]);
  n[ks[0]][ks[1]]=arr;
  arr.push({path:'',must_contain:''});
  renderTplVisual();
}
function tplAddCommand(i,p){
  collectTplForm();
  const n=tplNodeData(i);if(!n)return;
  const ks=p.split('.');
  if(!n[ks[0]]||typeof n[ks[0]]!=='object'||Array.isArray(n[ks[0]]))n[ks[0]]={};
  const arr=tplArr(n[ks[0]][ks[1]]);
  n[ks[0]][ks[1]]=arr;
  arr.push('');
  renderTplVisual();
}
function tplDelRow(i,p,j){
  collectTplForm();
  const n=tplNodeData(i);if(!n)return;
  const arr=tplArr(tplGet(n,p));
  if(j<0||j>=arr.length)return;
  arr.splice(j,1);
  renderTplVisual();
}

// 与服务端 _parse_pipeline_content 一致的格式识别：按内容而非扩展名；
// 去前导空白后以 { 开头按 JSON 解析，失败归 invalid，其余归 yaml（仅源码视图）。
function parseTplContent(content){
  const s=String(content==null?'':content);
  const t=s.replace(/^\s+/,'');
  if(t.startsWith('{')){
    try{return {kind:'json',data:JSON.parse(s)}}
    catch(e){return {kind:'invalid',error:String((e&&e.message)||e)}}
  }
  return {kind:'yaml'};
}
function tplObj(v){return v&&typeof v==='object'&&!Array.isArray(v)?v:{}}
function tplArr(v){return Array.isArray(v)?v:[]}
function tplBool(v){
  if(v===true)return true;
  if(v===false||v==null)return false;
  if(typeof v==='string')return ['true','yes','on','1'].includes(v.trim().toLowerCase());
  return !!v;
}
function tplStr(v,fallback){
  if(v===undefined||v===null||v==='')return String(fallback==null?'':fallback);
  return String(v);
}
function tplTextBlock(v,empty){
  const s=(v===undefined||v===null)?'':String(v);
  return s?`<pre>${esc(s)}</pre>`:`<span class="hint">${esc(empty||'（空）')}</span>`;
}
function tplOnOff(v){return tplBool(v)?'<span class="tag" style="margin-left:0">启用</span>':'<span class="hint">未启用</span>'}
function tplRow(k,v){return `<div class="tpl-kv"><div class="tpl-k">${esc(k)}</div><div class="tpl-v">${v}</div></div>`}
// 空 session_group 的实际语义取决于流水线 session_preset 的展开（gdd/models.py
// _expand_session_preset）：shared 全部归 gdd_all；clarify-design 下 clarify 节点
// 归相邻设计组；independent 才是真正独立。只读详情按此展示，避免误导。
function tplSessionGroupText(n){
  const preset=tplStr((window._tplData||{}).session_preset,'independent');
  if(preset==='shared')return '<span class="hint">（空 → 由 session_preset=shared 展开为共享组 gdd_all）</span>';
  if(preset==='clarify-design'&&tplBool(n.clarify))return '<span class="hint">（空 → 由 session_preset=clarify-design 与相邻 clarify 节点共享设计组）</span>';
  return '<span class="hint">（空 = 独立会话）</span>';
}

function tplGate(g){
  g=tplObj(g);
  const arts=tplArr(g.artifacts).map(a=>{
    a=tplObj(a);
    const mc=(a.must_contain===undefined||a.must_contain===null||a.must_contain==='')?'':String(a.must_contain);
    return `<li><code>${esc(tplStr(a.path,'(缺少 path)'))}</code>${mc?` — 必须匹配 <code>${esc(mc)}</code>`:''}</li>`;
  }).join('');
  const cmds=tplArr(g.commands).map(c=>`<li><code>${esc(String(c))}</code></li>`).join('');
  return tplRow('goal',tplTextBlock(g.goal))
    +tplRow('ai_check',tplTextBlock(g.ai_check))
    +tplRow('change_aware',tplOnOff(g.change_aware))
    +tplRow('artifacts',arts?`<ul class="tpl-list">${arts}</ul>`:'<span class="hint">（空）</span>')
    +tplRow('commands',cmds?`<ul class="tpl-list">${cmds}</ul>`:'<span class="hint">（空）</span>');
}

function tplPipelineSummary(data){
  const gd=tplObj(data.git_delivery);
  // 与 GitDeliverySpec.from_dict（enabled 缺失默认 True）及 tplPipelineField 保持一致
  const gdEnabled=gd.enabled===undefined?true:tplBool(gd.enabled);
  const preset=tplStr(data.session_preset,'independent');
  return `<div class="tpl-summary">
    <div class="tpl-detail-h"><b>${esc(tplStr(data.name,'(未命名模板)'))}</b><span class="tag">${esc(tplStr(data.id,'(缺少 id)'))}</span><span class="tag">${esc(preset)}</span></div>
    <div class="hint">${esc(tplStr(data.description,'（无描述）'))}</div>
    <div class="hint" style="margin-top:4px">git 交付：${gdEnabled?'启用':'停用'} · 分支 <code>${esc(tplStr(gd.branch,'gdd/delivery'))}</code>${gd.remote?` · remote <code>${esc(String(gd.remote))}</code>`:''}</div>
  </div>`;
}

function renderTplDetail(){
  const el=document.getElementById('tplDetail');if(!el)return;
  const nodes=tplArr(window._tplData&&window._tplData.nodes);
  if(!nodes.length){el.innerHTML='<div class="hint">模板没有可展示的节点。</div>';return}
  let i=Number(window._tplSel);if(!Number.isInteger(i)||i<0||i>=nodes.length)i=0;
  window._tplSel=i;
  const n=tplObj(nodes[i]);
  if(window._tplBuiltin)renderTplDetailReadonly(el,i);
  else tplFormDetail(el,n,i);
}
function renderTplDetailReadonly(el,i){
  const nodes=tplArr(window._tplData&&window._tplData.nodes);
  const n=tplObj(nodes[i]),sub=tplObj(n.subject),ap=tplObj(n.approval);
  const id=tplStr(n.id,'(缺少 id)'),nm=tplStr(n.name,id);
  const mcp=tplArr(n.mcp),skills=tplArr(n.skills);
  const unknown=Object.keys(n).filter(k=>!TPL_NODE_KEYS.includes(k));
  const ro=window._tplBuiltin
    ?'<div class="hint" style="margin-bottom:8px">内置模板 · 只读；如需修改请先「复制为新模板」。</div>'
    :'<div class="hint" style="margin-bottom:8px">当前为只读预览；修改模板请切换到「源码」模式。</div>';
  const marks=[];
  if(tplBool(ap.required))marks.push('需人工审批');
  TPL_MARKS.forEach(([k,t])=>{if(tplBool(n[k]))marks.push(t)});
  const other=unknown.length
    ?`<div class="tpl-kv" style="margin-top:10px"><div class="tpl-k">其他字段（只读）</div><div class="tpl-v"><pre>${esc(JSON.stringify(Object.fromEntries(unknown.map(k=>[k,n[k]])),null,2))}</pre><div class="hint">未知/未展示字段保存时原样保留，可在源码模式查看。</div></div></div>`
    :'';
  el.innerHTML=`<div class="card tpl-detail" data-node-detail="${esc(id)}" data-node-index="${i}">
    <div class="tpl-detail-h"><b>${esc(nm)}</b> <span class="tag">${esc(id)}</span>${marks.map(m=>`<span class="tag">${esc(m)}</span>`).join('')}</div>
    ${ro}
    ${tplRow('description',tplTextBlock(n.description))}
    ${tplRow('subject.goal',tplTextBlock(sub.goal))}
    ${tplRow('subject.instructions',tplTextBlock(sub.instructions))}
    ${tplRow('subject.max_turns',`<code>${esc(String(sub.max_turns===undefined?80:sub.max_turns))}</code>`)}
    ${tplRow('approval.required',tplOnOff(ap.required))}
    ${tplRow('approval.prompt',tplTextBlock(ap.prompt))}
    ${tplRow('session_group',n.session_group?`<code>${esc(String(n.session_group))}</code>`:tplSessionGroupText(n))}
    ${tplRow('max_bounces',`<code>${esc(String(n.max_bounces===undefined?3:n.max_bounces))}</code>`)}
    ${tplRow('baseline_marker',tplOnOff(n.baseline_marker))}
    ${tplRow('clarify',tplOnOff(n.clarify))}
    ${tplRow('adversarial',tplOnOff(n.adversarial))}
    ${tplRow('review',tplOnOff(n.review))}
    ${tplRow('entry',tplGate(n.entry))}
    ${tplRow('exit',tplGate(n.exit))}
    ${tplRow('mcp',mcp.length?`<pre>${esc(JSON.stringify(mcp,null,2))}</pre><div class="hint">只读摘要；源码模式可修改</div>`:'<span class="hint">（空）</span>')}
    ${tplRow('skills',skills.length?`<pre>${esc(JSON.stringify(skills,null,2))}</pre><div class="hint">只读摘要；源码模式可修改</div>`:'<span class="hint">（空）</span>')}
    ${other}
  </div>`;
}

function renderTplVisual(){
  const box=$('#tplBody');if(!box)return;
  const kind=window._tplKind,parseErr=window._tplParseError||'';
  if(kind!=='json'){
    box.innerHTML=`<div data-view="template-visual">
      <div class="alert warn">${kind==='yaml'?'该模板为 YAML/非 JSON，当前仅支持源码查看/编辑。':`模板内容无法解析为 JSON：${esc(parseErr||'未知错误')}。请使用源码查看/编辑。`}</div>
      <div class="hint">点击上方「源码」查看模板原文。</div></div>`;
    return;
  }
  const data=window._tplData;
  if(!data||typeof data!=='object'||Array.isArray(data)){
    box.innerHTML='<div data-view="template-visual"><div class="alert err">模板 JSON 顶层必须是对象，无法可视化；请使用源码查看/编辑。</div></div>';
    return;
  }
  const nodes=tplArr(data.nodes);
  const cards=nodes.map((raw,i)=>{
    const n=tplObj(raw);
    const id=tplStr(n.id,'(缺少 id)'),nm=tplStr(n.name,id);
    const marks=[];
    if(tplBool(tplObj(n.approval).required))marks.push('需人工审批');
    TPL_MARKS.forEach(([k,t])=>{if(tplBool(n[k]))marks.push(t)});
    const desc=tplStr(n.description,'');
    return `<div class="node tpl-node${window._tplSel===i?' sel':''}" data-node-id="${esc(id)}" data-node-index="${i}" style="cursor:pointer" onclick="tplSelect(${i})">
      <div class="nm">${esc(nm)}</div>
      <div class="ph">${esc(id)}</div>
      ${marks.length?`<div class="bd" style="margin-top:3px">${marks.map(m=>`<span class="tag" style="margin-left:0;margin-right:4px">${esc(m)}</span>`).join('')}</div>`:''}
      ${desc?`<div class="bd" style="margin-top:3px">${esc(desc.length>60?desc.slice(0,60)+'…':desc)}</div>`:''}
    </div>${i<nodes.length-1?'<div class="arrow">→</div>':''}`;
  }).join('');
  box.innerHTML=`<div data-view="template-visual">
    ${window._tplBuiltin?tplPipelineSummary(data):tplPipelineForm(data)}
    <div class="hint">节点流程（${nodes.length} 个节点，按模板 nodes 顺序）</div>
    <div class="flow" style="margin-top:8px">${cards||'<div class="empty" style="padding:16px">模板未定义 nodes 数组或节点为空。</div>'}</div>
    <div id="tplDetail" style="margin-top:16px"></div>
  </div>`;
  renderTplDetail();
}
function tplSelect(i){
  // 切换节点前先收集，保留未保存的本地编辑（同一页面会话内）
  if(!window._tplBuiltin)collectTplForm();
  window._tplSel=Number(i)||0;
  document.querySelectorAll('#tplBody .tpl-node[data-node-index]').forEach(el=>{
    el.classList.toggle('sel',Number(el.dataset.nodeIndex)===window._tplSel);
  });
  renderTplDetail();
}

function renderTplSource(){
  const builtin=!!window._tplBuiltin;
  const editing=window._tplSrcMode==='edit'&&!builtin;
  const cur=window._tplDraft!=null?window._tplDraft:(window._tpl||'');
  $('#tplBody').innerHTML=editing
    ?`<textarea id="tplEdit" style="min-height:420px">${esc(cur)}</textarea>`
    :`<pre data-view="template-source">${esc(cur)}</pre>`;
  const sb=$('#tplSaveBtn');if(sb)sb.style.display=editing?'':'none';
}
function tplShow(v){
  if(!$('#tplBody'))return;
  if(window._tplView==='visual'&&!window._tplBuiltin)collectTplForm();
  if(window._tplView==='source'){
    const ta=document.getElementById('tplEdit');if(ta)window._tplDraft=ta.value;
  }
  window._tplView=v;
  const vis=document.getElementById('tplTabVisual'),src=document.getElementById('tplTabSource');
  if(vis)vis.classList.toggle('on',v==='visual');
  if(src)src.classList.toggle('on',v==='source');
  const eb=document.getElementById('tplEditBtn');
  if(eb){
    const editOn=v==='source'&&window._tplSrcMode==='edit';
    eb.textContent=editOn?'查看源码':'编辑源码';
    eb.onclick=()=>tplMode(editOn?'view':'edit');
  }
  const sb=document.getElementById('tplSaveBtn');
  if(sb){
    const canSave=!window._tplBuiltin&&(v==='visual'?window._tplKind==='json':window._tplSrcMode==='edit');
    sb.style.display=canSave?'':'none';
  }
  if(v==='visual'){renderTplVisual()}else{renderTplSource()}
}
function tplMode(m){
  const ta=document.getElementById('tplEdit');if(ta)window._tplDraft=ta.value;
  window._tplSrcMode=m;
  tplShow('source');
}

async function showTpl(name){
  stopPoll();view={mode:'tpl',name};
  const j=await api('api/template/'+encodeURIComponent(name));
  if(!j.ok){$('#main').innerHTML=`<div class="alert err">${esc(j.error)}</div>`;return}
  const editable=!j.builtin;
  const cur=j.content;
  const parsed=parseTplContent(cur);
  const notice=window._tplNotice||''; window._tplNotice='';
  window._tpl=cur; window._tplName=name; window._tplBuiltin=!!j.builtin;
  window._tplKind=parsed.kind; window._tplData=parsed.kind==='json'?tplDeep(parsed.data):null;
  window._tplParseError=parsed.error||''; window._tplSel=0; window._tplDraft=null;
  window._tplSrcMode='view'; window._tplView='';
  const kindWarn=parsed.kind==='json'?'':`<div class="alert warn">${parsed.kind==='yaml'?'该模板为 YAML/非 JSON，当前仅支持源码查看/编辑。':`模板内容无法解析为 JSON：${esc(parsed.error||'未知错误')}；请使用源码查看/编辑。`}</div>`;
  $('#main').innerHTML=`<h2>模板：${esc(name)}${j.builtin?'<span class="tag">内置 · 只读</span>':''}</h2>
  <div class="card">
    <div class="tabs">
      <button id="tplTabVisual" onclick="tplShow('visual')">可视化</button>
      <button id="tplTabSource" onclick="tplShow('source')">源码</button>
      <span style="flex:1"></span>
      ${editable?'<button id="tplEditBtn"></button>':''}
    </div>
    ${kindWarn}
    <div id="tplBody"></div>
    <div class="actions" style="margin-top:12px">
      ${editable?'<button class="pri" id="tplSaveBtn" style="display:none" onclick="saveTpl(\''+esc(name)+'\')">保存</button><button class="danger" onclick="delTpl(\''+esc(name)+'\')">删除</button>':''}
      ${j.builtin?'<button onclick="forkTpl(\''+esc(name)+'\')">复制为新模板</button>':''}
      <button onclick="showNew()">用此模板新建任务 →</button>
    </div><div id="tplErr">${notice}</div></div>`;
  tplShow(parsed.kind==='json'?'visual':'source');
}
async function saveTpl(name){
  let content='';
  if(window._tplView==='visual'){
    // 可视化模式：先收集表单（只覆盖表单字段），再把解析后的对象序列化回 JSON 文本，
    // 走现有 PUT content 路径；未展示字段（mcp/skills/未知键）随原对象保留。
    if(window._tplBuiltin){$('#tplErr').innerHTML='<div class="alert err">内置模板只读，请先「复制为新模板」</div>';return}
    if(window._tplKind!=='json'||!window._tplData||typeof window._tplData!=='object'||Array.isArray(window._tplData)){
      $('#tplErr').innerHTML='<div class="alert err">当前模板不是 JSON 对象，请使用源码模式保存</div>';return;
    }
    const errs=collectTplForm();
    if(errs.length){$('#tplErr').innerHTML=`<div class="alert err">${esc(errs.join('；'))}</div>`;return}
    content=JSON.stringify(window._tplData,null,2);
  }else{
    const ta=document.getElementById('tplEdit');
    if(!ta){$('#tplErr').innerHTML='<div class="alert err">请先切换到源码编辑模式</div>';return}
    content=ta.value;
  }
  const j=await api('api/template/'+encodeURIComponent(name),{method:'PUT',body:JSON.stringify({content})});
  if(!j.ok){
    // 保存失败：显示服务端错误，保留本地编辑，不用本地值覆盖磁盘内容
    $('#tplErr').innerHTML=`<div class="alert err">${esc(j.error)}</div>`;return;
  }
  window._tpl=content; window._tplDraft=null;
  window._tplNotice='<div class="alert warn" style="border-color:#2b5c42;background:#12241a;color:#9fe0b8">已保存。对之后新建的任务生效。</div>';
  refreshNav(); showTpl(name);
}
async function delTpl(name){
  if(!confirm('删除模板 '+name+'？'))return;
  const j=await api('api/template/'+encodeURIComponent(name),{method:'DELETE'});
  if(!j.ok){alert(j.error);return}
  view={mode:'home'};refreshNav();showNew();
}
async function forkTpl(name){
  const nn=(prompt('新模板名：','')||'').trim();
  if(!nn)return;
  const j=await api('api/template/'+encodeURIComponent(name),{method:'PUT',body:JSON.stringify({content:window._tpl,fork:nn})});
  if(!j.ok){$('#tplErr').innerHTML=`<div class="alert err">${esc(j.error)}</div>`;return}
  showTpl(nn);refreshNav();
}

// ---------- 新建模板（T3）：名称预检查 + 最小合法模板 ----------
// 与服务端 _NAME_RE 语义一致：中英文/数字/_/-，1–64 字，首字符不能是 -。
const TPL_NAME_RE=/^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff]{0,63}$/;
function tplNewMinimal(name,display){
  // 最小合法模板（spec 第 6 节）：一个默认节点，其余字段用模型默认值。
  return {id:name,name:display||name,description:'',session_preset:'independent',
    git_delivery:{enabled:true,branch:'gdd/delivery'},
    nodes:[{id:'node1',name:'节点1',description:''}]};
}
function showNewTpl(){
  stopPoll();view={mode:'tplnew'};refreshNav();
  $('#main').innerHTML=`<h2>新建模板</h2><div class="card" data-view="template-new">
    <div class="row"><label>模板名（必填）</label>
      <input id="newTplName" data-field="new-template.name" placeholder="my-template" onkeydown="if(event.key==='Enter')createTpl()">
      <span class="hint">仅支持中英文、数字、_、-，1–64 字且不能以 - 开头；不能与已有模板重名，delivery 为内置保留名。</span></div>
    <div class="row"><label>展示名（可选，默认同模板名）</label>
      <input id="newTplDisplay" data-field="new-template.display-name" placeholder="我的模板" onkeydown="if(event.key==='Enter')createTpl()"></div>
    <div class="actions">
      <button class="pri" id="newTplCreate" data-action="create-template" onclick="createTpl()">创建模板</button>
      <button data-action="cancel-template" onclick="refreshNav();showNew()">取消</button>
      <span class="hint" style="align-self:center">创建后默认含一个节点；节点增删/重排请用源码模式。</span></div>
    <div id="newTplErr"></div></div>`;
}
async function createTpl(){
  const name=($('#newTplName')?.value||'').trim();
  const display=($('#newTplDisplay')?.value||'').trim();
  const fail=msg=>{$('#newTplErr').innerHTML=`<div class="alert err">${esc(msg)}</div>`};
  if(!name){fail('请输入模板名');return}
  if(!TPL_NAME_RE.test(name)){fail('模板名不合法：仅支持中英文、数字、_、-，1–64 字且不能以 - 开头');return}
  if(name==='delivery'){fail('delivery 是内置模板保留名，请换个名字');return}
  // 已存在模板（任意扩展名）先给快速提示；真正的 create-only 由服务端 fork 分支兜底
  // （普通 PUT 是保存覆盖语义，大小写变体/并发同名不能只靠这里的精确匹配）。
  const ov=await api('api/overview');
  if(!ov.ok){fail(ov.error||'无法读取模板列表');return}
  if((ov.templates||[]).some(t=>t.name===name)){fail(`模板 ${name} 已存在，换个名字`);return}
  const btn=$('#newTplCreate');if(btn)btn.disabled=true;
  const content=JSON.stringify(tplNewMinimal(name,display),null,2);
  // 走服务端既有 fork 参数（create-only）：普通 PUT 是保存覆盖语义，
  // 大小写变体/并发同名会覆盖已有模板；fork 分支拒绝已存在名并原子创建。
  const j=await api('api/template/'+encodeURIComponent(name),{method:'PUT',body:JSON.stringify({content,fork:name})});
  if(!j.ok){if(btn)btn.disabled=false;fail(j.error||'创建失败');return}
  refreshNav();showTpl(name);
}

refreshNav();showNew();setInterval(refreshNav,5000);
</script></body></html>"""


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    root = "."
    quiet = True

    def log_message(self, fmt, *args):
        if not self.quiet:
            super().log_message(fmt, *args)

    def _stream_live(self, name: str) -> None:
        """SSE：先推环形缓冲里已有的事件，再等新事件。断开时结束。"""
        after = 0
        hdr = self.headers.get("Last-Event-ID") or ""
        if hdr.isdigit():
            after = int(hdr)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                events = live_wait(name, after, 15)
                if not events:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                for ev in events:
                    data = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"id: {ev['id']}\ndata: {data}\n\n".encode("utf-8"))
                    after = ev["id"]
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    # ---- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 1_000_000:
            # 丢弃请求体后再报错，否则连接被提前关闭，客户端写入中断（broken pipe）
            while n > 0:
                chunk = self.rfile.read(min(n, 65536))
                if not chunk:
                    break
                n -= len(chunk)
            raise ValueError("请求体过大（>1MB）")
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _allowed_hosts(self) -> tuple:
        """同源白名单：本机回环 + GDD_UI_EXTRA_HOSTS（逗号分隔，用于 IDE 代理等反向链路，
        默认空 = 仅本机，fail-closed）。"""
        extra = tuple(
            h.strip() for h in os.environ.get("GDD_UI_EXTRA_HOSTS", "").split(",") if h.strip()
        )
        return ("127.0.0.1", "localhost") + extra

    def _guard_mutating(self) -> bool:
        """防 CSRF/DNS rebinding：变更类请求要求同源 Host + 自定义头
        （跨站 no-cors fetch 无法携带自定义头/JSON Content-Type）。"""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in self._allowed_hosts():
            return False
        if not self.headers.get("X-GDD-Request"):
            return False
        if self.command in ("POST", "PUT"):
            try:
                clen = int(self.headers.get("Content-Length") or 0)
            except (ValueError, TypeError):
                return False  # 畸形 Content-Length：拒绝（fail-closed）
            if clen > 0:
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
                if ctype != "application/json":
                    return False
        return True

    # ---- routes
    def do_GET(self):
        # DNS rebinding 防护：GET 也校验同源 Host（rebound 域名的 Host 非本机即拒）
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in self._allowed_hosts():
            return self._json({"ok": False, "error": "拒绝：Host 非法"}, 403)
        try:
            self._route_get()
        except Exception as e:  # 兑底：绝不让请求无响应（前端轮询静默失败）
            try:
                self._json({"ok": False, "error": f"服务器内部错误: {e}"}, 500)
            except OSError:
                pass

    def _route_get(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/api/settings":
            try:
                return self._json({"ok": True, **pi_settings.public_view()})
            except pi_settings.SettingsError as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/overview":
            self._json({"ok": True, "templates": _list_templates(self.root),
                        "tasks": _list_tasks(self.root)})
            return
        if p.startswith("/api/template/"):
            name = unquote(p.split("/", 3)[3])
            if not _safe_name(name):
                return self._json({"ok": False, "error": "非法模板名"}, 400)
            if name == "delivery":
                return self._json({"ok": True, "builtin": True,
                                   "content": json.dumps(BUILTINS["delivery"], ensure_ascii=False, indent=2)})
            tp = _template_path(self.root, name)
            if not os.path.exists(tp):
                return self._json({"ok": False, "error": "模板不存在"}, 404)
            with open(tp, encoding="utf-8") as f:
                return self._json({"ok": True, "builtin": False, "content": f.read()})
        live = re.match(r"^/api/task/([^/]+)/live$", p)
        if live:
            name = unquote(live.group(1))
            if not _safe_name(name):
                return self._json({"ok": False, "error": "非法任务名"}, 400)
            if not os.path.isfile(os.path.join(self.root, name, ".gdd", "state.json")):
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            self._stream_live(name)
            return
        if p.startswith("/api/task/"):
            name = unquote(p.split("/", 3)[3])
            if not _safe_name(name):
                return self._json({"ok": False, "error": "非法任务名"}, 400)
            try:
                self._json({"ok": True, **_task_detail(self.root, name)})
            except FileNotFoundError as e:
                self._json({"ok": False, "error": str(e)}, 404)
            except RuntimeError as e:
                self._json({"ok": False, "error": str(e)}, 409)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        if not self._guard_mutating():
            return self._json({"ok": False, "error": "拒绝：缺少同源校验头（X-GDD-Request）或 Host 非法"}, 403)
        try:
            body = self._body()
        except (ValueError, OSError) as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        try:
            if p == "/api/settings/test":
                try:
                    saved = pi_settings.load()
                except pi_settings.SettingsError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                raw_base = body.get("baseUrl", saved.get("baseUrl"))
                raw_model = body.get("model", saved.get("model"))
                raw_key = body.get("apiKey")
                if raw_base is not None and not isinstance(raw_base, str):
                    return self._json({"ok": False, "error": "端点必须是字符串"}, 400)
                if raw_model is not None and not isinstance(raw_model, str):
                    return self._json({"ok": False, "error": "模型必须是字符串"}, 400)
                if raw_key not in (None, "") and not isinstance(raw_key, str):
                    return self._json({"ok": False, "error": "API key 必须是字符串"}, 400)
                base = (raw_base or "").strip() or str(saved.get("baseUrl") or "")
                model = (raw_model or "").strip() or str(saved.get("model") or "")
                key = raw_key if raw_key not in (None, "") else saved.get("apiKey") or ""
                try:
                    result = pi_settings.test_endpoint(base, str(key), model)
                except pi_settings.SettingsError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                except pi_settings.SettingsTestError as e:
                    return self._json({"ok": False, "error": str(e)}, 502)
                return self._json({"ok": True, **result})
            if p == "/api/tasks":
                name = str(body.get("name", "")).strip()
                iteration = str(body.get("iteration", "")).strip()
                repos = _parse_repos(body.get("repos"))
                tpl = str(body.get("template", "")).strip()
                req = str(body.get("requirement", "")).strip()
                if not _safe_name(name):
                    return self._json({"ok": False, "error": "任务名仅允许中英文/数字/-_/，≤64 字"}, 400)
                if not tpl:
                    return self._json({"ok": False, "error": "必须选择模板"}, 400)
                if tpl == "delivery":
                    pipeline = json.loads(json.dumps(BUILTINS["delivery"]))
                else:
                    if not _safe_name(tpl):
                        return self._json({"ok": False, "error": "非法模板名"}, 400)
                    tp = _template_path(self.root, tpl)
                    if not os.path.exists(tp):
                        return self._json({"ok": False, "error": f"模板不存在: {tpl}"}, 404)
                    pipeline = _load_pipeline_file(tp)
                d = os.path.join(self.root, name)
                bind_name = name
                task_home = bool(repos)
                if needs_iteration(pipeline) and not repos:
                    if not iteration:
                        return self._json({
                            "ok": False,
                            "error": "请至少填写一个仓库地址",
                        }, 400)
                    bind_name = iteration
                with _create_lock:
                    if os.path.exists(os.path.join(d, ".gdd", "state.json")):
                        return self._json({"ok": False, "error": f"任务 {name} 已存在；如需重跑请在任务页重置"}, 400)
                    preexisting = os.path.exists(d)
                    os.makedirs(d, exist_ok=True)
                    records = []
                    try:
                        pipeline = prepare_iteration(
                            pipeline, d, name=bind_name, task_home=task_home)
                        if repos:
                            from .repos import prepare_repos
                            records = prepare_repos(
                                getattr(self, "repos", None) or self.root, d, name, repos)
                        o = Orchestrator(d)
                        o.init(pipeline, req, repos=records)
                    except Exception:
                        if records:
                            from .repos import release_repos
                            release_repos(records)
                        if not preexisting:
                            shutil.rmtree(d, ignore_errors=True)
                        raise
                return self._json({"ok": True, "task": name})
            m = re.match(r"^/api/task/([^/]+)/action$", p)
            if m:
                name = unquote(m.group(1))
                if not _safe_name(name):
                    return self._json({"ok": False, "error": "非法任务名"}, 400)
                op = str(body.get("op", ""))
                fb = str(body.get("feedback", ""))
                if op not in ("run", "approve", "reject", "retry", "abort", "reset", "chat", "clarify_done", "steer"):
                    return self._json({"ok": False, "error": f"未知操作: {op}"}, 400)
                return self._json(_do_action(self.root, name, op, fb, extra=body))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, ImportError, json.JSONDecodeError) as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"ok": False, "error": "not found"}, 404)

    def do_PUT(self):
        if not self._guard_mutating():
            return self._json({"ok": False, "error": "拒绝：缺少同源校验头（X-GDD-Request）或 Host 非法"}, 403)
        try:
            return self._do_put()
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, ImportError, json.JSONDecodeError) as e:
            return self._json({"ok": False, "error": str(e)}, 400)

    def _do_put(self):
        p = urlparse(self.path).path
        if p == "/api/settings":
            body = self._body()
            base = body.get("baseUrl")
            model = body.get("model")
            api_key = body.get("apiKey")
            if not isinstance(base, str):
                return self._json({"ok": False, "error": "端点必须是字符串"}, 400)
            if not isinstance(model, str):
                return self._json({"ok": False, "error": "模型必须是字符串"}, 400)
            if api_key not in (None, "") and not isinstance(api_key, str):
                return self._json({"ok": False, "error": "API key 必须是字符串"}, 400)
            try:
                data = pi_settings.save(base, model,
                                        api_key=api_key if isinstance(api_key, str) else None)
            except pi_settings.SettingsError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json({"ok": True, **pi_settings.public_view(data)})
        m = re.match(r"^/api/template/([^/]+)$", p)
        if not m:
            return self._json({"ok": False, "error": "not found"}, 404)
        name = unquote(m.group(1))
        body = self._body()
        fork = str(body.get("fork", "") or "")
        if fork:
            # 复制内置/既有模板为新模板
            if not _safe_name(fork):
                return self._json({"ok": False, "error": "非法新模板名"}, 400)
            # P2-15：新名撞内置保留名（delivery）会造出影子文件——GET 仍服务
            # 内置、PUT/DELETE 被内置只读规则拒、列表出现重名：永不可编辑删除
            if fork in BUILTINS:
                return self._json({"ok": False, "error": f"{fork} 是内置模板保留名，请换个名字"}, 400)
            target = _template_path(self.root, fork)
            if os.path.exists(target):
                return self._json({"ok": False, "error": f"模板 {fork} 已存在，换个名字"}, 400)
            content = str(body.get("content", ""))
            try:
                _parse_pipeline_content(content)
            except Exception as e:
                return self._json({"ok": False, "error": f"模板不合法: {e}"}, 400)
            os.makedirs(_templates_dir(self.root), exist_ok=True)
            # 新建是 create-only：exists 检查与写入之间存在 TOCTOU 窗口（两个 tab
            # 并发同名、大小写不敏感文件系统的大小写变体）。O_EXCL 原子创建，
            # 保证恰一个请求成功、其余返回“已存在”且不覆盖既有文件。
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                return self._json({"ok": False, "error": f"模板 {fork} 已存在，换个名字"}, 400)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            return self._json({"ok": True, "name": fork})
        if name == "delivery":
            return self._json({"ok": False, "error": "内置模板只读（可复制为新模板后编辑）"}, 400)
        if not _safe_name(name):
            return self._json({"ok": False, "error": "非法模板名"}, 400)
        content = str(body.get("content", ""))
        try:
            _parse_pipeline_content(content)
        except Exception as e:
            return self._json({"ok": False, "error": f"模板不合法: {e}"}, 400)
        os.makedirs(_templates_dir(self.root), exist_ok=True)
        with open(_template_path(self.root, name), "w", encoding="utf-8") as f:
            f.write(content)
        return self._json({"ok": True, "name": name})

    def do_DELETE(self):
        if not self._guard_mutating():
            return self._json({"ok": False, "error": "拒绝：缺少同源校验头（X-GDD-Request）或 Host 非法"}, 403)
        p = urlparse(self.path).path
        m = re.match(r"^/api/template/([^/]+)$", p)
        if not m:
            return self._json({"ok": False, "error": "not found"}, 404)
        name = unquote(m.group(1))
        if not _safe_name(name):
            return self._json({"ok": False, "error": "非法模板名"}, 400)
        if name == "delivery":
            return self._json({"ok": False, "error": "内置模板不可删除"}, 400)
        tp = _template_path(self.root, name)
        if os.path.exists(tp):
            os.remove(tp)
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "模板不存在"}, 404)


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="gdd-ui", description="GDD 本地控制台")
    ap.add_argument("--root", default=".", help="工作台目录（其下每个子目录是一个任务）")
    ap.add_argument("--repos", default="", help="代码仓库目录，默认与工作台相同")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    os.makedirs(root, exist_ok=True)
    Handler.root = root
    Handler.repos = os.path.abspath(args.repos) if args.repos else root

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"GDD 控制台: {url}（工作台目录 {root}）")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
