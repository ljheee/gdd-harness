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
     run / approve / reject / retry / abort / reset 操作；
  4. run 在后台线程执行，前端 2s 轮询刷新。

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
from .models import PipelineSpec
from .orchestrator import Orchestrator

# 任务创建串行化：ThreadingHTTPServer 并发处理双击/双 tab 的同名创建请求时，
# 两个请求都过存在性检查后才建目录，败者的失败清理会删掉胜者初始化好的任务
_create_lock = threading.Lock()

BUILTINS = {"delivery": DELIVERY_PIPELINE}

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,64}$")

_running: dict = {}          # task -> Thread（在跑的 run）
_running_lock = threading.Lock()


# ---------------------------------------------------------------- 工具

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
        import yaml  # 可选依赖
        data = yaml.safe_load(content)
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
        })
    pa = st.get("pending_approval")
    prompt = ""
    if pa:
        # P2-13：同输入优雅降级——缺键/幽灵 node 不 500（load 已有 RuntimeError
        # 契约，这里兜住 load 与读取之间的竞态窗口）
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
    }


def _is_running(name: str) -> bool:
    with _running_lock:
        return name in _running


_chat_err: dict = {}  # 任务名 → 最近一次后台对话失败信息（前端展示用，下次 chat 启动时清除）
_run_err: dict = {}  # 任务名 → 最近一次后台推进失败信息（前端展示用，下次 run 启动时清除）


def _bg_chat(root: str, name: str, node_id: str, message: str) -> None:
    """澄清对话后台轮：与 run 同一互斥域（_running 注册表），锁内执行。"""
    try:
        o = Orchestrator(root)
        if o.load():
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
        return {"ok": True}
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
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tabs button.on{border-color:var(--blue);color:#cfe0ff}
</style></head><body>
<aside>
  <h1>GDD 控制台</h1><div class="sub">Goal Driven Development</div>
  <div class="sec"><div class="t">新建任务</div>
    <button class="pri item" style="text-align:center" onclick="showNew()">＋ 从模板启动任务</button>
  </div>
  <div class="sec"><div class="t">任务</div><div id="navTasks"></div></div>
  <div class="sec"><div class="t">模板</div><div id="navTpl"></div></div>
</aside>
<main id="main"><div class="empty">加载中…</div></main>
<script>
const $=s=>document.querySelector(s);
let view={mode:'home'}, pollTimer=null, lastDetail=null;

async function api(path,opts){
  opts=opts||{};
  opts.headers=Object.assign({'Content-Type':'application/json','X-GDD-Request':'1'},opts.headers||{});
  const r=await fetch(path,opts);
  const j=await r.json().catch(()=>({ok:false,error:r.statusText}));
  if(!r.ok&&!j.error)j.error='HTTP '+r.status; return j;}

async function refreshNav(){
  const [ov]=await Promise.all([api('/api/overview')]);
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
function enc(s){return btoa(unescape(encodeURIComponent(String(s))))}  // onclick 安全传参（避开实体解码突破引号）
function dec(s){return decodeURIComponent(escape(atob(s)))}
function stopPoll(){if(pollTimer){clearInterval(pollTimer);pollTimer=null}}

async function showNew(){
  stopPoll();view={mode:'new'};
  const ov=await api('/api/overview');
  const tpls=(ov.templates||[]).map(t=>`<option value="${esc(t.name)}">${esc(t.name)}${t.builtin?'（内置）':''}</option>`).join('');
  $('#main').innerHTML=`<h2>从模板启动新任务</h2><div class="card">
    <div class="two"><div class="row"><label>任务名（子目录）</label><input id="nName" placeholder="my-task"></div>
    <div class="row"><label>模板</label><select id="nTpl">${tpls}</select></div></div>
    <div class="row"><label>需求（一句话也行）</label><textarea id="nReq" placeholder="实现一个 TODO CLI，支持增删查，带 pytest 测试"></textarea></div>
    <div class="actions"><button class="pri" onclick="createTask()">创建并初始化</button>
    <span class="hint" style="align-self:center">创建后到任务页点「启动」</span></div>
    <div id="nErr"></div></div>`;
}
async function createTask(){
  const name=$('#nName').value.trim();
  const j=await api('/api/tasks',{method:'POST',body:JSON.stringify({name,template:$('#nTpl').value,requirement:$('#nReq').value})});
  if(!j.ok){$('#nErr').innerHTML=`<div class="alert err">${esc(j.error)}</div>`;return}
  showTask(enc(name));
}

async function showTask(name){
  stopPoll();view={mode:'task',name:dec(name)};
  await renderTask(view.name);
  pollTimer=setInterval(()=>renderTask(view.name),2000);
}
async function renderTask(name){
  // 驳回反馈/澄清输入中：跳过本轮重绘，避免 2s 轮询打断/清空输入（两个输入框独立判断）
  const rejEl=document.getElementById('rejFb'), clEl=document.getElementById('clInput');
  if((rejEl&&document.activeElement===rejEl)||(clEl&&document.activeElement===clEl))return;
  const rejVal=rejEl?rejEl.value:'', clVal=clEl?clEl.value:'';
  const j=await api('/api/task/'+encodeURIComponent(name));
  if(!j.ok){$('#main').innerHTML=`<div class="alert err">${esc(j.error)}</div>
    <div class="actions"><button class="warn" onclick="act('reset')">↺ 重置任务（删除 .gdd，不可恢复）</button></div>`;stopPoll();return}
  // await 期间用户可能又在输入框打了字：以请求返回时的现值为准，防旧快照覆盖新输入
  const rejNow=document.getElementById('rejFb'), clNow=document.getElementById('clInput');
  const rejVal2=(rejNow&&document.activeElement!==rejNow&&rejNow.value)?rejNow.value:rejVal;
  const clVal2=(clNow&&document.activeElement!==clNow&&clNow.value)?clNow.value:clVal;
  lastDetail=j; refreshNav();
  const st=j.status, running=j.running;
  const phTxt={pending:'待进入',working:'进行中',exit_retry:'门禁重试',exit_passed:'待审批',done:'已完成'};
  const flow=j.nodes.map((n,i)=>{
    let cls='';let flag='';
    const isCur=n.id===j.current;
    if(isCur){cls=esc(n.phase==='pending'?'cur':n.phase)}
    else if(n.phase==='done')cls='done';
    if(n.needs_approval&&n.phase!=='done')flag='需人工';
    let ph=esc(phTxt[n.phase]||n.phase);
    if(n.phase==='exit_passed'&&j.pending_approval&&j.pending_approval.node===n.id){cls='approval';ph='⏸ 等你审批'}
    const bd=[];
    if(n.bounces)bd.push(`打回${esc(String(n.bounces))}`);
    if(n.entry_bounces)bd.push(`entry打回${esc(String(n.entry_bounces))}`);
    return `${flag?`<div class="flag">${flag}</div>`:''}<div class="node ${cls}"><div class="nm">${esc(n.name)}</div><div class="ph">${ph}</div>${bd.length?`<div class="bd">${bd.join(' · ')}</div>`:''}</div>${i<j.nodes.length-1?'<div class="arrow">→</div>':''}`;
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
  const hist=(j.history_tail||[]).slice().reverse().map(h=>`<tr><td class="ts">${esc(h.ts||'')}</td><td>${esc(h.event)}</td><td>${esc(h.node||'')}</td><td>${esc(typeof h.reason==='string'?h.reason.slice(0,60):(h.reason==null?'':JSON.stringify(h.reason).slice(0,60)))}</td></tr>`).join('');
  $('#main').innerHTML=`<h2>${esc(name)} <span class="tag">${esc(j.pipeline_name||'')}</span><span class="tag">${esc(st)}${running?' · 运行中':''}</span></h2>
    ${alertHtml}
    <div class="card"><div class="flow">${flow}</div>${btns}</div>
    ${clarifyHtml}
    <div class="card"><b style="color:var(--dim);font-size:12px">最近事件</b><table style="margin-top:8px">${hist||'<tr><td class="ts">（无）</td></tr>'}</table></div>`;
  const r2=document.getElementById('rejFb'), c2=document.getElementById('clInput');
  if(r2&&!r2.value&&rejVal2)r2.value=rejVal2;
  if(c2&&!c2.value&&clVal2)c2.value=clVal2;
}
async function act(op){
  if(op==='reset'&&!confirm('确认重置？将删除 .gdd 全部状态，不可恢复。'))return;
  const fb=op==='reject'?(document.getElementById('rejFb')?.value||''):'';
  const j=await api('/api/task/'+encodeURIComponent(view.name)+'/action',{method:'POST',body:JSON.stringify({op,feedback:fb})});
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
    const j=await api('/api/task/'+encodeURIComponent(view.name)+'/action',{method:'POST',body:JSON.stringify({op:'chat',node:nodeId,message:msg})});
    if(!j.ok){alert(j.error||'发送失败（输入内容已保留，请复制后重试）');return}
    if(!j.started){alert('发送未被受理（任务可能已在运行）：'+(j.error||'输入内容已保留，请稍后重试'));return}
    // 仅在后台对话线程确认受理（started）后清空：窗口关闭/完整性拒绝等
    // 不落盘场景下用户输入不丢失（P2-18）
    box.value='';box.blur();
    renderTask(view.name);  // 显示对话中状态（轮询刷新回复）
  }catch(e){alert('网络异常，请重试：'+e)}
  finally{box.dataset.busy='0'}
}
async function clarifyDone(nodeId){
  nodeId=dec(nodeId);
  const isReview=lastDetail&&lastDetail.clarify&&lastDetail.clarify.mode==='review';
  if(!confirm('结束澄清？'+(isReview?'产出将重过 exit 门禁。':'')))return;
  try{
    const j=await api('/api/task/'+encodeURIComponent(view.name)+'/action',{method:'POST',body:JSON.stringify({op:'clarify_done',node:nodeId})});
    if(!j.ok){alert(j.error||'操作失败');return}
    if(j.note)alert(j.note);
    renderTask(view.name);refreshNav();
  }catch(e){alert('网络异常，请重试：'+e)}
}

async function showTplA(b64){showTpl(dec(b64))}
async function showTpl(name){
  stopPoll();view={mode:'tpl',name};
  const j=await api('/api/template/'+encodeURIComponent(name));
  if(!j.ok){$('#main').innerHTML=`<div class="alert err">${esc(j.error)}</div>`;return}
  const editable=!j.builtin;
  const cur=j.content;
  $('#main').innerHTML=`<h2>模板：${esc(name)}${j.builtin?'<span class="tag">内置 · 只读</span>':''}</h2>
  <div class="card">
    <div class="tabs">${j.builtin?'':'<button onclick="tplMode(\'view\')">查看</button><button onclick="tplMode(\'edit\')">编辑</button>'}</div>
    <div id="tplBody"></div>
    <div class="actions" style="margin-top:12px">
      ${editable?'<button class="pri" onclick="saveTpl(\''+esc(name)+'\')">保存</button><button class="danger" onclick="delTpl(\''+esc(name)+'\')">删除</button>':''}
      ${j.builtin?'<button onclick="forkTpl(\''+esc(name)+'\')">复制为新模板</button>':''}
      <button onclick="showNew()">用此模板新建任务 →</button>
    </div><div id="tplErr"></div></div>`;
  window._tpl=cur; window._tplName=name; tplMode(j.builtin?'view':'view');
}
function tplMode(m){
  const cur=window._tpl||'';
  $('#tplBody').innerHTML=m==='edit'?`<textarea id="tplEdit" style="min-height:420px">${esc(cur)}</textarea>`:`<pre>${esc(cur)}</pre>`;
  window._tplMode=m;
}
async function saveTpl(name){
  const content=$('#tplEdit').value;
  const j=await api('/api/template/'+encodeURIComponent(name),{method:'PUT',body:JSON.stringify({content})});
  if(!j.ok){$('#tplErr').innerHTML=`<div class="alert err">${esc(j.error)}</div>`;return}
  window._tpl=content; showTpl(name);
}
async function delTpl(name){
  if(!confirm('删除模板 '+name+'？'))return;
  const j=await api('/api/template/'+encodeURIComponent(name),{method:'DELETE'});
  if(!j.ok){alert(j.error);return}
  view={mode:'home'};refreshNav();showNew();
}
async function forkTpl(name){
  const nn=(prompt('新模板名：','')||'').trim();
  if(!nn)return;
  const j=await api('/api/template/'+encodeURIComponent(name),{method:'PUT',body:JSON.stringify({content:window._tpl,fork:nn})});
  if(!j.ok){$('#tplErr').innerHTML=`<div class="alert err">${esc(j.error)}</div>`;return}
  showTpl(nn);refreshNav();
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

    def _guard_mutating(self) -> bool:
        """防 CSRF/DNS rebinding：变更类请求要求同源 Host + 自定义头
        （跨站 no-cors fetch 无法携带自定义头/JSON Content-Type）。"""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
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
        if host not in ("127.0.0.1", "localhost"):
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
            if p == "/api/tasks":
                name = str(body.get("name", "")).strip()
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
                # 同名创建串行化（双击/双 tab 并发）：锁内重查已存在再初始化；
                # 锁外首个检查仅作快速失败，不作为唯一防线
                with _create_lock:
                    if os.path.exists(os.path.join(d, ".gdd", "state.json")):
                        return self._json({"ok": False, "error": f"任务 {name} 已存在；如需重跑请在任务页重置"}, 400)
                    preexisting = os.path.exists(d)  # 目录已存在（用户同名目录）→ 失败时绝不删除
                    os.makedirs(d, exist_ok=True)
                    try:
                        o = Orchestrator(d)
                        o.init(pipeline, req)
                    except Exception:
                        # init 失败不残留孤儿任务目录；但仅当目录是本请求新建时才清除，
                        # 已存在目录（可能有用户数据）保持原样，避免误删
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
                if op not in ("run", "approve", "reject", "retry", "abort", "reset", "chat", "clarify_done"):
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
            if os.path.exists(_template_path(self.root, fork)):
                return self._json({"ok": False, "error": f"模板 {fork} 已存在，换个名字"}, 400)
            content = str(body.get("content", ""))
            try:
                _parse_pipeline_content(content)
            except Exception as e:
                return self._json({"ok": False, "error": f"模板不合法: {e}"}, 400)
            os.makedirs(_templates_dir(self.root), exist_ok=True)
            with open(_template_path(self.root, fork), "w", encoding="utf-8") as f:
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
    ap.add_argument("--root", default=".", help="任务工作台目录（其下每个子目录是一个任务）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    os.makedirs(root, exist_ok=True)
    Handler.root = root

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
