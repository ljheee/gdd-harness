"""会话复用（resume）+ 要点澄清（clarify）+ legacy 指纹迁移 测试。

运行：python3 tests/test_clarify.py
覆盖（对应 docs/design-clarify-resume.md）：
  C1 主体 resume：驳回打回后 subject 会话带 --resume，sid 跨尝试一致
  C2 打回≥2 次后不再 resume（防上下文膨胀）
  C3 clarify pending：启动前预对齐 → clarify_done → 正常推进
  C4 clarify review：审批挂起时对话（resume 主体）→ clarify_done 强制重验
     → bounces 不增加 → 批准后完成
  C5 非 clarify 节点 chat 拒绝；非澄清窗口 chat 拒绝
  C6 legacy 指纹迁移：剥 clarify 键 + 旧指纹 → run 自动迁移放行；
     篡改其他字段 → 仍 fail-closed
"""
from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
import urllib.error
import urllib.request

os.environ["GDD_AGENT"] = "pi"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PYTHONPATH", ROOT)

STUB = os.path.join(ROOT, "tests", "pi_stub.py")
PASS = 'GDD-VERDICT: {"verdict": "pass", "reason": "stub通过", "evidence": ["e1"], "feedback": ""}'


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


class Client:
    def __init__(self, base):
        self.base = base

    def req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json",
                                            "X-GDD-Request": "e2e-test"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())

    def get(self, path):
        return self.req(path)

    def post(self, path, body):
        return self.req(path, "POST", body)

    def put(self, path, body):
        return self.req(path, "PUT", body)


def wait_for(client, name, pred, timeout=60, desc=""):
    deadline = __import__("time").time() + timeout
    while __import__("time").time() < deadline:
        d = client.get(f"/api/task/{name}")
        if d.get("ok") and pred(d):
            return d
        __import__("time").sleep(0.3)
    raise AssertionError(f"等待超时: {desc}")


def logs_for(workdir, name, kind):
    """收集 .gdd/logs 下指定 kind 的会话日志（按序）。"""
    d = os.path.join(workdir, name, ".gdd", "logs")
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(f"-{kind}.json"):
                try:
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        out.append(json.load(f))
                except (OSError, json.JSONDecodeError):
                    pass
    return out


PIPELINE = """\
id: clarify-e2e
name: 澄清测试流水线
nodes:
  - id: plan
    name: 方案
    description: 出方案
    clarify: true
    approval:
      required: true
      prompt: 请确认方案
    exit:
      goal: 方案产出
      artifacts:
        - path: docs/plan.md
          must_contain: "## 方案"
  - id: ship
    name: 收尾
    description: 收尾
"""


def mkartifact(workdir, name, path="docs/plan.md", text="## 方案\n"):
    """预置节点产物（stub 主体会话不真正写文件，机器门禁需要）。"""
    p = os.path.join(workdir, name, path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    from gdd import ui as gdd_ui

    workdir = tempfile.mkdtemp(prefix="gdd-clarify-e2e-")
    script = os.path.join(workdir, "stub.json")
    with open(script, "w", encoding="utf-8") as f:
        json.dump([{"patterns": ["GDD-ROLE"], "text": PASS}], f, ensure_ascii=False)
    os.environ["GDD_AGENT_BIN"] = sys.executable
    os.environ["GDD_AGENT_PRE_ARGS"] = STUB  # cmd = python3 pi_stub.py --print ...
    os.environ["GDD_PI_STUB_SCRIPT"] = script
    pi_home = tempfile.mkdtemp(prefix="gdd-clarify-pi-")
    settings = os.path.join(pi_home, "settings.json")
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "baseUrl": "https://api.example.com/v1",
                   "apiKey": "k", "model": "m"}, f)
    os.environ["GDD_PI_SETTINGS"] = settings
    os.environ["GDD_PI_AGENT_DIR"] = os.path.join(pi_home, "agent")
    os.environ["GDD_PI_SESSION_DIR"] = os.path.join(pi_home, "sessions")

    gdd_ui.Handler.root = workdir
    srv = gdd_ui.ThreadingHTTPServer(("127.0.0.1", 0), gdd_ui.Handler)
    threading_port = srv.server_address[1]
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(f"http://127.0.0.1:{threading_port}")
    print(f"[setup] server on {threading_port}, workdir {workdir}")

    r = c.put("/api/template/ce", {"content": PIPELINE})
    check(r["ok"], "模板保存")
    r = c.post("/api/tasks", {"name": "t1", "template": "ce", "requirement": "做个健康检查"})
    check(r["ok"], "任务创建")

    # ---------- C3 clarify pending：启动前预对齐 ----------
    print("[C3] clarify pending")
    r = c.post("/api/task/t1/action", {"op": "chat", "node": "plan", "message": "健康检查要不要鉴权？"})
    check(r["ok"] and r.get("started"), "pending 态 chat 启动（后台线程）")
    wait_for(c, "t1", lambda x: not x["running"], desc="对话轮结束")
    dlg = c.get("/api/task/t1")["dialog_tail"]
    check(len(dlg) == 2 and dlg[0]["role"] == "user" and dlg[1]["role"] == "assistant",
          "对话落盘（user+assistant）")
    st = json.load(open(os.path.join(workdir, "t1", ".gdd", "state.json"), encoding="utf-8"))
    check(st["node_states"]["plan"]["sessions"].get("clarify"), "澄清会话 sid 已存档")
    check(st["status"] == "running" and st["node_states"]["plan"]["phase"] == "pending",
          "状态机未被对话改变（仍 pending）")
    r = c.post("/api/task/t1/action", {"op": "clarify_done", "node": "plan"})
    check(r["ok"] and r["mode"] == "pending" and not r["recheck"], "pending 态 clarify_done 仅记事件")
    dlg2 = c.get("/api/task/t1")["dialog_tail"]

    # 第二轮对话 resume 澄清会话
    r = c.post("/api/task/t1/action", {"op": "chat", "node": "plan", "message": "好，不要鉴权，写进需求"})
    check(r["ok"] and r.get("started"), "第二轮 chat")
    wait_for(c, "t1", lambda x: not x["running"], desc="第二轮对话结束")
    cl_logs = logs_for(workdir, "t1", "clarify_pending")
    check(any("--session-id" in (lg.get("cmd") or []) for lg in cl_logs[1:]),
          "第二轮澄清会话带 --session-id（首轮为全新会话）")
    st = json.load(open(os.path.join(workdir, "t1", ".gdd", "state.json"), encoding="utf-8"))
    check(st["node_states"]["plan"]["sessions"].get("clarify"), "澄清会话 sid 稳定复用")

    # ---------- C1 主体 resume（驳回打回后） ----------
    print("[C1] 主体 resume")
    mkartifact(workdir, "t1")  # 模拟主体会话产出，供 exit 机器门禁核验
    r = c.post("/api/task/t1/action", {"op": "clarify_done", "node": "plan"})
    check(r["ok"], "澄清结束")
    r = c.post("/api/task/t1/action", {"op": "run"})
    check(r["ok"], "run 启动")
    d = wait_for(c, "t1", lambda x: x["status"] == "awaiting_approval", desc="审批挂起")
    st = json.load(open(os.path.join(workdir, "t1", ".gdd", "state.json"), encoding="utf-8"))
    sid1 = st["node_states"]["plan"]["sessions"].get("subject")
    check(bool(sid1), "主体会话 sid 已存档")
    # 驳回 → 主体 resume 重跑
    c.post("/api/task/t1/action", {"op": "reject", "feedback": "补一下回滚方案"})
    d = wait_for(c, "t1", lambda x: x["status"] == "awaiting_approval", desc="驳回后重新挂起")
    st = json.load(open(os.path.join(workdir, "t1", ".gdd", "state.json"), encoding="utf-8"))
    check(st["node_states"]["plan"]["sessions"].get("subject") == sid1, "主体 sid 跨打回一致（复用）")
    sr = logs_for(workdir, "t1", "subject_resume")
    check(any("--session-id" in (lg.get("cmd") or []) for lg in sr), "resume 会话 cmd 带 --session-id")
    check(d["nodes"][0]["bounces"] == 1, "驳回计入 bounces=1")

    # ---------- C4 clarify review：审批挂起时对话 + 强制重验（此时 bounces=1，resume 生效） ----------
    print("[C4] clarify review + 强制重验")
    st = json.load(open(os.path.join(workdir, "t1", ".gdd", "state.json"), encoding="utf-8"))
    sid_before = st["node_states"]["plan"]["sessions"].get("subject")
    r = c.post("/api/task/t1/action", {"op": "chat", "node": "plan", "message": "方案里加监控埋点"})
    check(r["ok"] and r.get("started"), "review 态 chat 启动（resume 主体会话）")
    wait_for(c, "t1", lambda x: not x["running"], desc="对话轮结束")
    d = c.get("/api/task/t1")
    check(d["status"] == "awaiting_approval", "对话后仍停在审批挂起（状态机不变）")
    cl_logs = logs_for(workdir, "t1", "clarify_review")
    check(any("--session-id" in (lg.get("cmd") or []) for lg in cl_logs), "review 对话 resume 主体会话")
    st = json.load(open(os.path.join(workdir, "t1", ".gdd", "state.json"), encoding="utf-8"))
    check(st["node_states"]["plan"]["bounces"] == 1, "澄清不计入打回预算")
    r = c.post("/api/task/t1/action", {"op": "clarify_done", "node": "plan"})
    check(r["ok"] and r["mode"] == "review" and r["recheck"], "clarify_done 触发强制重验")
    n_exit_before = len(logs_for(workdir, "t1", "exit"))
    d = wait_for(c, "t1", lambda x: x["status"] == "awaiting_approval", desc="重验后重新挂起")
    exit_logs = logs_for(workdir, "t1", "exit")
    check(len(exit_logs) > n_exit_before, "重验新增了 exit 门禁会话")
    # 纪要性质走结构化 kind（feedback_kinds=clarify），门禁按非强制判据措辞注入。
    # 断言用 lead 独有子串 + 反向断言：反馈正文本身含“非强制判据”字样，不能作区分依据
    exit_prompts = [(lg.get("prompt") or "") for lg in exit_logs[n_exit_before:]]
    check(any("明显偏离则 fail" in p and "打回修改项：逐项核实" not in p for p in exit_prompts),
          "澄清纪要按 kind 识别为非强制判据 lead（且非打回 lead）")
    check(not any("--session-id" in (lg.get("cmd") or []) for lg in exit_logs[n_exit_before:]),
          "重验的 exit 门禁为全新会话（永不 resume）")
    st = json.load(open(os.path.join(workdir, "t1", ".gdd", "state.json"), encoding="utf-8"))
    check(st["node_states"]["plan"]["bounces"] == 1, "重验路径不消耗打回预算")
    check(st["node_states"]["plan"]["sessions"].get("subject") == sid_before,
          "重验走主体会话 resume 收尾（sid 不变）")

    # ---------- C2 打回≥2 次后不再 resume ----------
    print("[C2] 打回 2 次后停止复用")
    sr = logs_for(workdir, "t1", "subject_resume")
    n_resume_before = len([lg for lg in sr if "--session-id" in (lg.get("cmd") or [])])
    c.post("/api/task/t1/action", {"op": "reject", "feedback": "第二次"})
    d = wait_for(c, "t1", lambda x: x["status"] == "awaiting_approval", desc="第二次驳回后挂起")
    st = json.load(open(os.path.join(workdir, "t1", ".gdd", "state.json"), encoding="utf-8"))
    check(st["node_states"]["plan"]["bounces"] == 2, "bounces=2")
    sr2 = logs_for(workdir, "t1", "subject_resume")
    n_resume_after = len([lg for lg in sr2 if "--session-id" in (lg.get("cmd") or [])])
    check(n_resume_after == n_resume_before, "bounces>=2 后新尝试为全新会话（不再 resume）")

    # 收尾：批准 → 完成
    # 先验证硬规则3机制化：审批期间产物被改 → approve 被拒，必须走重验/驳回
    plan_p = os.path.join(workdir, "t1", "docs", "plan.md")
    with open(plan_p, "a", encoding="utf-8") as f:
        f.write("\n（审批期间被澄清会话补写的内容）\n")
    r = c.post("/api/task/t1/action", {"op": "approve"})
    check(not r["ok"] and "产物已变更" in str(r.get("error", "")),
          "审批期间产物被改 → approve 被拒（硬规则3机制化）")
    d = c.get("/api/task/t1")
    check(d["status"] == "awaiting_approval", "拒绝后审批项保持挂起")
    # 走正路：clarify_done 强制重验 → 重新挂起 → 批准
    r = c.post("/api/task/t1/action", {"op": "clarify_done", "node": "plan"})
    check(r["ok"] and r.get("recheck"), "重验路径仍可用")
    d = wait_for(c, "t1", lambda x: x["status"] == "awaiting_approval", desc="重验后重新挂起")
    c.post("/api/task/t1/action", {"op": "approve"})
    d = wait_for(c, "t1", lambda x: x["status"] == "completed", desc="批准后完成")
    check(all(n["phase"] == "done" for n in d["nodes"]), "全流程完成")

    # ---------- C5 防线 ----------
    print("[C5] clarify 防线")
    from gdd.orchestrator import Orchestrator as _O
    r = c.post("/api/tasks", {"name": "t2", "template": "ce", "requirement": "r"})
    check(r["ok"], "t2 创建")
    # 非 clarify 流水线：验证「当前节点未开 clarify」拒绝路径
    r = c.put("/api/template/nc", {"content": PIPELINE.replace("    clarify: true\n", "")})
    check(r["ok"], "非 clarify 模板保存")
    r = c.post("/api/tasks", {"name": "t5", "template": "nc", "requirement": "r"})
    check(r["ok"], "t5 创建（非 clarify 流水线）")
    o5 = _O(os.path.join(workdir, "t5")); o5.load()
    r = o5.chat("plan", "hi")
    check(not r["ok"], "当前节点未开 clarify 时 chat 拒绝")
    o2 = _O(os.path.join(workdir, "t2")); o2.load()
    r = o2.chat("ship", "hi")
    check(not r["ok"], "非当前节点 chat 拒绝（ship 未到且非 clarify 窗口）")
    o1 = _O(os.path.join(workdir, "t1")); o1.load()
    r = o1.chat("plan", "hi")
    check(not r["ok"], "终态任务 chat 拒绝")
    # chat 后台失败 → 前端可见（chat_error 通路，UI 修复回归）
    r = c.post("/api/tasks", {"name": "t4", "template": "ce", "requirement": "r"})
    check(r["ok"], "t4 创建")
    with open(script, "w", encoding="utf-8") as f:
        json.dump([{"patterns": ["GDD-ROLE"], "text": "", "is_error": True}], f)
    r = c.post("/api/task/t4/action", {"op": "chat", "node": "plan", "message": "hello"})
    check(r["ok"] and r.get("started"), "失败注入 chat 启动")
    wait_for(c, "t4", lambda x: not x["running"], desc="失败对话轮结束")
    d = c.get("/api/task/t4")
    check(bool(d.get("chat_error")), "chat 后台失败时前端可见（chat_error）")
    r = c.post("/api/task/t4/action", {"op": "chat", "node": "plan", "message": "x" * 20001})
    check(not r["ok"] and "过长" in str(r.get("error", "")), "chat message 超 20000 上限被拒")
    with open(script, "w", encoding="utf-8") as f:
        json.dump([{"patterns": ["GDD-ROLE"], "text": PASS}], f)

    # ---------- C5b 既有目录不误删（init 失败 rmtree 安全） ----------
    victim = os.path.join(workdir, "t6")
    os.makedirs(os.path.join(victim, "mydata"), exist_ok=True)
    with open(os.path.join(victim, "mydata", "keep.txt"), "w", encoding="utf-8") as f:
        f.write("用户数据")
    r = c.post("/api/tasks", {"name": "t6", "template": "ce", "requirement": "r"})
    check(r["ok"], "t6（既有目录）创建成功")
    check(os.path.exists(os.path.join(victim, "mydata", "keep.txt")),
          "同名既有目录的用户数据保留")

    # ---------- C6 legacy 指纹迁移 ----------
    print("[C6] legacy 指纹迁移")
    r = c.post("/api/tasks", {"name": "t3", "template": "ce", "requirement": "r"})
    check(r["ok"], "t3 创建")
    mkartifact(workdir, "t3")
    sp = os.path.join(workdir, "t3", ".gdd", "state.json")
    pp = os.path.join(workdir, "t3", ".gdd", "pipeline.json")
    st = json.load(open(sp, encoding="utf-8"))
    pd = json.load(open(pp, encoding="utf-8"))
    # 构造 legacy 实例：剥掉 clarify 键，指纹改为"剔除 clarify 后"的旧算法值
    legacy = json.loads(json.dumps(pd))
    for n in legacy["nodes"]:
        n.pop("clarify", None)
    legacy_fp = hashlib.sha256(
        json.dumps(legacy, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    st["pipeline_fingerprint"] = legacy_fp
    json.dump(st, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(legacy, open(pp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    r = c.post("/api/task/t3/action", {"op": "run"})
    check(r["ok"], "legacy 实例 run 启动（迁移放行）")
    d = wait_for(c, "t3", lambda x: x["status"] == "awaiting_approval", desc="legacy 任务推进")
    pd2 = json.load(open(pp, encoding="utf-8"))
    check(all("clarify" in n for n in pd2["nodes"]), "迁移后实例已重写（补回新字段）")
    # 指纹收敛：迁移后 verify 通过、交互入口不重复迁移
    o3v = _O(os.path.join(workdir, "t3")); o3v.load()
    check(o3v.verify_pipeline_integrity(), "迁移后指纹收敛（verify 通过）")
    n_hist = len(o3v.state["history"])
    r = o3v.chat("plan", "hi")
    check(not any(h.get("event") == "pipeline_fingerprint_migrated"
                  for h in o3v.state["history"][n_hist:]),
          "指纹收敛后交互入口不再重复迁移")
    # 迁移语义：legacy 实例创建于 clarify 字段存在之前，迁移只补默认值 False
    # （在途存量任务不获得澄清能力，需 reset 重新实例化）——chat 应被拒且不报指纹错误
    check(not r.get("ok") and "不可澄清" in str(r.get("error", "")),
          "迁移后 clarify 取默认 False（存量任务不自动获得澄清能力）")
    # 篡改场景：改 pipeline 其他字段 + 剥 clarify → 不得借道迁移
    st = json.load(open(sp, encoding="utf-8"))
    pd3 = json.load(open(pp, encoding="utf-8"))
    pd3["nodes"][0]["description"] = "被篡改的描述"
    for n in pd3["nodes"]:
        n.pop("clarify", None)
    json.dump(pd3, open(pp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    wait_for(c, "t3", lambda x: not x["running"], desc="等待运行结束")
    r = c.post("/api/task/t3/action", {"op": "run"})
    d = c.get("/api/task/t3")
    # t3 已 awaiting，直接 run 会被拒；伪造推进验证：手动触发指纹校验路径
    from gdd.orchestrator import Orchestrator
    o3 = Orchestrator(os.path.join(workdir, "t3"))
    o3.load()
    check(not o3.verify_pipeline_integrity() and not o3._migrate_legacy_fingerprint(),
          "篡改实例迁移被拒（fail-closed）")

    # ---------- C7 GET Host 防护（DNS rebinding 回归） ----------
    print("[C7] GET Host 防护")
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{threading_port}/api/task/t1",
            headers={"Host": "evil.com"}), timeout=10)
        code = r.getcode()
    except urllib.error.HTTPError as e:
        code = e.code
    check(code == 403, "伪造 Host 的 GET 被拒（403）")

    # ---------- C8 打回反馈传递给 exit 门禁核验 ----------
    print("[C8] 打回反馈注入 exit 门禁")
    r = c.post("/api/tasks", {"name": "t7", "template": "ce", "requirement": "r"})
    check(r["ok"], "t7 创建")
    mkartifact(workdir, "t7")
    r = c.post("/api/task/t7/action", {"op": "run"})
    wait_for(c, "t7", lambda x: x["status"] == "awaiting_approval", desc="t7 首轮挂起")
    first_exits = logs_for(workdir, "t7", "exit")
    check(first_exits and not any("上轮反馈" in (lg.get("prompt") or "") for lg in first_exits),
          "首轮 exit 门禁 prompt 不含打回项")
    c.post("/api/task/t7/action", {"op": "reject", "feedback": "补上回滚方案章节"})
    wait_for(c, "t7", lambda x: x["status"] == "awaiting_approval", desc="t7 打回重跑后挂起")
    exits2 = logs_for(workdir, "t7", "exit")
    check(len(exits2) > len(first_exits), "打回重跑产生了新 exit 门禁会话")
    new_exits = exits2[len(first_exits):]
    check(any("上轮反馈" in (lg.get("prompt") or "") and "补上回滚方案" in (lg.get("prompt") or "")
              for lg in new_exits),
          "打回后的 exit 门禁 prompt 携带上轮反馈（逐项核验）")
    # 接线级防线：真实 exit 门禁 prompt 必须按 bounce lead 注入（kind 管道断裂或错标
    # 为 clarify 时，此断言失败——C10 只覆盖 build_gate_prompt 单元级）
    check(any("打回修改项：逐项核实" in (lg.get("prompt") or "") for lg in new_exits),
          "打回反馈按 bounce lead 注入真实 exit 门禁（编排接线级）")
    # resume 会话的 prompt 应携带门禁证据（增强3）
    sr7 = logs_for(workdir, "t7", "subject_resume")
    check(any("门禁核验证据" in (lg.get("prompt") or "") for lg in sr7),
          "resume 反馈段携带门禁证据")
    # 打回项核验通过后才消费（pop 后移）：打回重跑→exit 通过后 feedbacks 应为空
    st7 = json.load(open(os.path.join(workdir, "t7", ".gdd", "state.json"), encoding="utf-8"))
    check(not st7["feedbacks"].get("plan"), "exit 核验通过后打回项已消费（pop 后移）")
    # 证据新鲜性：打回轮 resume 的证据应来自本次失败门禁（stub 证据非空且在 fail 后写入）
    check(bool(st7.get("last_exit_evidence")), "exit 门禁证据已落盘（pass/fail 均写）")

    # ---------- C9 blocked 保留最新反馈（retry 后主体带最新失败原因） ----------
    print("[C9] blocked 反馈保留")
    r = c.post("/api/tasks", {"name": "t8", "template": "ce", "requirement": "r"})
    check(r["ok"], "t8 创建")
    # 无产物 → 机器门禁失败循环打回至 blocked
    r = c.post("/api/task/t8/action", {"op": "run"})
    d = wait_for(c, "t8", lambda x: x["status"] == "blocked", desc="t8 打回至上限")
    st8 = json.load(open(os.path.join(workdir, "t8", ".gdd", "state.json"), encoding="utf-8"))
    check(bool(st8["feedbacks"].get("plan")) and "机器检查未过" in st8["feedbacks"]["plan"],
          "blocked 时最新失败反馈已写入 feedbacks（retry 后主体可见）")

    # ---------- C10 负控：结构化 kind 优先于子串回退 ----------
    # kind=bounce 但反馈文本前 40 字含「澄清对齐纪要」→ 必须按打回项（强制判据）注入，
    # 否则说明 feedback_kinds 管道断裂、子串回退在抢答（P2 负控：反向用例）
    print("[C10] kind 结构化判据优先于子串回退")
    from gdd.gates import build_gate_prompt
    from gdd.models import NodeSpec, GateSpec
    _node = NodeSpec(id="n", name="n", subject={"goal": "g"})
    _gate = GateSpec(goal="g")
    _fb = "澄清对齐纪要（伪装成纪要的打回反馈）：补上回滚方案"
    _p = build_gate_prompt("exit", _node, _gate, [], pending_feedback=_fb, pending_kind="bounce")
    check("打回修改项：逐项核实" in _p and "明显偏离则 fail" not in _p,
          "kind=bounce 时子串不抢答（按打回项强制判据注入）")
    _p2 = build_gate_prompt("exit", _node, _gate, [], pending_feedback=_fb, pending_kind="clarify")
    check("明显偏离则 fail" in _p2 and "打回修改项：逐项核实" not in _p2,
          "kind=clarify 时按非强制判据注入")
    _p3 = build_gate_prompt("exit", _node, _gate, [], pending_feedback=_fb, pending_kind="")
    check("明显偏离则 fail" in _p3,
          "无 kind（旧 state）时子串回退兜底仍有效")

    # ---------- C11 后台线程禁 tty 交互审批 ----------
    # UI 后台线程（_bg_run）即使进程挂在 tty 上也不得走 input() 交互审批，
    # 否则持 flock 期间阻塞等终端输入（P1 复发防线）
    print("[C11] 后台线程禁 tty 交互审批")
    import threading as _th
    from gdd import approval as _ap
    _bg_ret = {}
    _t = _th.Thread(target=lambda: _bg_ret.update(v=_ap.is_interactive()))
    _t.start(); _t.join()
    check(_bg_ret.get("v") is False, "后台线程 is_interactive 恒 False（线程感知）")
    # 确定性核心断言：即使进程挂在 tty 上（模拟 isatty=True），后台线程仍须 False。
    # 去除对测试进程 stdin 是否 tty 的环境依赖（CI 非 tty 环境下也能捕获回归）
    _orig_stdin, _orig_stdout = _ap.sys.stdin, _ap.sys.stdout
    class _FakeTty:
        def isatty(self):
            return True
        def write(self, *_):  # print 回写兼容
            return 0
        def flush(self):
            pass
    try:
        _ap.sys.stdin = _FakeTty()
        _ap.sys.stdout = _FakeTty()
        check(_ap.is_interactive() is True, "主线程 + tty 允许交互审批")
        _bg_ret2 = {}
        _t2 = _th.Thread(target=lambda: _bg_ret2.update(v=_ap.is_interactive()))
        _t2.start(); _t2.join()
        check(_bg_ret2.get("v") is False, "tty 环境下后台线程仍恒 False（P1 核心）")
    finally:
        _ap.sys.stdin, _ap.sys.stdout = _orig_stdin, _orig_stdout

    # ---------- C12 主体会话组复用（design-session-group.md）----------
    print("[C12] session_group 组复用")
    SG_PIPELINE = """\
id: sg-e2e
name: 会话组测试
session_preset: shared
nodes:
  - id: n1
    name: 步一
    clarify: true            # 迁移用例经 chat 通道触发（需合法澄清窗口）
    session_group: pair      # 节点级显式组（覆盖 shared 预设）
    exit:
      goal: 产出
      artifacts:
        - path: docs/a.md
          must_contain: "## A"
  - id: n2
    name: 步二
    session_group: pair      # 与 n1 同组 → 主体会话 resume 复用
    exit:
      goal: 产出
      artifacts:
        - path: docs/b.md
          must_contain: "## B"
  - id: n3
    name: 步三
    # 无显式组 + preset=shared → gdd:all（独立于 pair 组）
    exit:
      goal: 产出
      artifacts:
        - path: docs/c.md
          must_contain: "## C"
"""
    r = c.put("/api/template/sg", {"content": SG_PIPELINE})
    check(r["ok"], "sg 模板保存")
    r = c.post("/api/tasks", {"name": "t-sg", "template": "sg", "requirement": "组复用"})
    check(r["ok"], "t-sg 创建")
    # 产物预置（stub 主体不写文件，机器门禁需要）
    mkartifact(workdir, "t-sg", "docs/a.md", "## A\n")
    mkartifact(workdir, "t-sg", "docs/b.md", "## B\n")
    mkartifact(workdir, "t-sg", "docs/c.md", "## C\n")
    r = c.post("/api/task/t-sg/action", {"op": "run"})
    check(r["ok"], "run 启动")
    wait_for(c, "t-sg", lambda x: x["status"] == "completed", desc="组复用全流程")
    st = json.load(open(os.path.join(workdir, "t-sg", ".gdd", "state.json"), encoding="utf-8"))
    sid1 = st["node_states"]["n1"]["sessions"].get("subject")
    sid2 = st["node_states"]["n2"]["sessions"].get("subject")
    check(bool(sid1) and sid2 == sid1, "同组相邻节点存档同一主体 sid（stub 恒定 sid）")
    # stub 会话 sid 恒定，无法用 sid 值区分新旧——用 cmd 区分：
    # 同组 n2 主体轮应带 --resume；异组 n3 主体轮应全新（不带）
    subj_logs = logs_for(workdir, "t-sg", "subject")
    sg_logs = logs_for(workdir, "t-sg", "subject_resume")
    check(any("--session-id" in (lg.get("cmd") or []) for lg in sg_logs),
          "同组 n2 主体轮 cmd 带 --session-id（复用 n1 会话）")
    # 跨节点继承会话的 resume 轮必须是完整任务书（节点角色/目标/原始需求齐备），
    # 而非「上轮会话中断」空指令——会话里只有上游节点的任务书，本节点目标必须重述
    _n2 = [lg for lg in sg_logs if "[[GDD-ROLE:subject node:n2]]" in (lg.get("prompt") or "")]
    check(len(_n2) == 1 and "原始需求输入" in (_n2[0].get("prompt") or "")
          and "上轮会话中断" not in (_n2[0].get("prompt") or ""),
          "n2 跨节点 resume 收到完整任务书（角色+需求输入齐备，非空指令）")
    # 异组 n3：全新轮（subject 日志存在且 n3 不走 resume）
    n_all = len(subj_logs) + len(sg_logs)
    check(n_all >= 3, f"三节点各有一轮主体会话（共 {n_all} 轮）")
    hist = [h["event"] for h in st["history"]]
    check("subject_resume" in hist, "审计含 subject_resume 事件")
    # 迁移/兼容语义：session_group 在 to_dict 中非空才写，故真 legacy 实例（磁盘无
    # session 字段）的内存 canonical 与 state fp 天然一致——无需迁移、行为 = 全独立
    # （同 clarify:False 语义：存量任务不自动获得组复用能力）。
    r = c.post("/api/tasks", {"name": "t-sg2", "template": "sg", "requirement": "迁移"})
    check(r["ok"], "t-sg2 创建")
    sp2 = os.path.join(workdir, "t-sg2", ".gdd")
    pp2 = os.path.join(sp2, "pipeline.json")
    stp2 = os.path.join(sp2, "state.json")
    pd2 = json.load(open(pp2, encoding="utf-8"))
    st2 = json.load(open(stp2, encoding="utf-8"))
    legacy = json.loads(json.dumps(pd2))
    for n in legacy["nodes"]:
        n.pop("session_group", None)
    legacy.pop("session_preset", None)
    fp_legacy = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, sort_keys=True)
                               .encode("utf-8")).hexdigest()[:16]
    # 磁盘写剥键 legacy 形态 + state 存对应旧指纹 → 指纹天然一致，通道无指纹错误
    with open(pp2, "w", encoding="utf-8") as f:
        json.dump(legacy, f, ensure_ascii=False)
    st2["pipeline_fingerprint"] = fp_legacy
    with open(stp2, "w", encoding="utf-8") as f:
        json.dump(st2, f, ensure_ascii=False)
    r = c.post("/api/task/t-sg2/action", {"op": "chat", "node": "n1", "message": "hi"})
    check(r.get("ok"), "legacy 组配置实例过指纹门禁（天然一致，无指纹错误）")
    wait_for(c, "t-sg2", lambda x: not x["running"], desc="legacy 对话轮结束")
    from gdd.orchestrator import Orchestrator as _O2
    o2 = _O2(os.path.join(workdir, "t-sg2")); o2.load()
    check(o2.verify_pipeline_integrity(), "legacy 实例指纹收敛（verify 通过）")
    check(all(not n.session_group for n in o2.pipeline.nodes),
          "legacy 实例语义 = 全部独立（不自动获得组复用）")
    # 篡改场景：磁盘带 truthy session_group + 剥键旧指纹 → 不得借道迁移，fail-closed
    pd3 = json.load(open(pp2, encoding="utf-8"))
    pd3["nodes"][0]["session_group"] = "evil"
    with open(pp2, "w", encoding="utf-8") as f:
        json.dump(pd3, f, ensure_ascii=False)
    o3 = _O2(os.path.join(workdir, "t-sg2")); o3.load()
    r3 = o3.chat("n1", "hi")
    check(not r3.get("ok") and "指纹" in str(r3.get("error", "")),
          "truthy 组 + 旧指纹被 fail-closed 拒绝（不可借道迁移）")

    # 预设分段展开回归：非相邻 clarify 段不得共享组名（远距离合并会放大污染面）
    from gdd.models import PipelineSpec as _PS
    _seg = _PS.from_dict({"id": "seg", "session_preset": "clarify-design", "nodes": [
        {"id": "a", "clarify": True}, {"id": "b"}, {"id": "c", "clarify": True},
        {"id": "d", "clarify": True}, {"id": "e"}, {"id": "f", "clarify": True}]})
    _groups = {n.id: n.session_group for n in _seg.nodes}
    check(_groups["a"] == "gdd_design" and _groups["c"] == _groups["d"] == "gdd_design2"
          and _groups["f"] == "gdd_design3" and not _groups["b"] and not _groups["e"],
          f"非相邻 clarify 段各自成组（{_groups}）")
    # 展开结果 round-trip：分段组名须过自身校验（展开名属合法字符集，可直接 round-trip）
    _PS.from_dict(_seg.to_dict())

    # ---------- C13 审批产物快照 glob 展开（审批×glob 交互）----------
    print("[C13] 审批快照 glob 展开")
    # 机器门禁支持 glob（docs/*.md），快照/变更核验须同一匹配语义：
    # 否则 glob 节点快照恒空 → 审批期间改产物不被拦（硬规则3对 glob 形同虚设）
    GLOB_PIPELINE = PIPELINE.replace(
        "        - path: docs/plan.md\n", '        - path: "docs/*.md"\n')
    check("docs/*.md" in GLOB_PIPELINE, "glob 模板替换生效")
    r = c.put("/api/template/cg", {"content": GLOB_PIPELINE})
    check(r["ok"], "cg 模板保存")
    r = c.post("/api/tasks", {"name": "t-glob", "template": "cg", "requirement": "glob 快照"})
    check(r["ok"], "t-glob 创建")
    mkartifact(workdir, "t-glob")  # docs/plan.md
    r = c.post("/api/task/t-glob/action", {"op": "run"})
    check(r["ok"], "run 启动")
    wait_for(c, "t-glob", lambda x: x["status"] == "awaiting_approval", desc="glob 节点审批挂起")
    st = json.load(open(os.path.join(workdir, "t-glob", ".gdd", "state.json"), encoding="utf-8"))
    snap = st["pending_approval"].get("artifact_snapshot") or {}
    check(list(snap) == ["docs/plan.md"],
          f"快照按 glob 展开到具体文件（实际 {list(snap)}）")
    # 新增匹配文件 → 变更（原实现快照恒空，此路不拦）
    extra_p = os.path.join(workdir, "t-glob", "docs", "extra.md")
    with open(extra_p, "w", encoding="utf-8") as f:
        f.write("审批期间新增\n")
    r = c.post("/api/task/t-glob/action", {"op": "approve"})
    check(not r["ok"] and "产物已变更" in str(r.get("error", ""))
          and "docs/extra.md" in str(r.get("error", "")),
          "glob 新增匹配文件 → approve 被拒且指认具体文件")
    check(c.get("/api/task/t-glob")["status"] == "awaiting_approval", "拒绝后保持挂起")
    # 快照内文件消失 → 同样计变更
    os.remove(extra_p)
    plan_p = os.path.join(workdir, "t-glob", "docs", "plan.md")
    plan_text = open(plan_p, encoding="utf-8").read()
    os.remove(plan_p)
    r = c.post("/api/task/t-glob/action", {"op": "approve"})
    check(not r["ok"] and "docs/plan.md" in str(r.get("error", "")),
          "快照内文件消失 → approve 被拒")
    # 恢复原状（指纹复原）→ 放行
    with open(plan_p, "w", encoding="utf-8") as f:
        f.write(plan_text)
    r = c.post("/api/task/t-glob/action", {"op": "approve"})
    # plan 为末审批节点（ship 无审批）：UI 路径末节点 approve 必须返回 ok=True
    # （曾因 _complete/_advance 推进返回值被误报为「审批已失效」）
    check(r.get("ok"), "末节点 approve 返回 ok=True（审批成功不误报失败）")
    wait_for(c, "t-glob", lambda x: x["status"] == "completed", desc="恢复后批准完成")

    srv.shutdown()
    print("\n全部通过 ✔")


if __name__ == "__main__":
    main()
