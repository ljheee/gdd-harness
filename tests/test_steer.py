"""working 期间补充信息（steer）。

运行：python3 tests/test_steer.py

覆盖（docs/design-steer.md）：
  S1 队列在仓库外；hook 吃掉未消费补充并输出 additionalContext；空队列无输出
  S2 gdd steer 不拿 .gdd/lock
  S3 仅 Claude 形态的主体命令注入 --settings；stub/其他 bin 不注入
  S4 主体成功但本轮没有工具调用时，补送恰好一次（--resume）；失败则把补充放回队列
  S5 门禁命令不挂 steer hook
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PYTHONPATH", ROOT)
os.environ["GDD_AGENT"] = "pi"

STUB = os.path.join(ROOT, "tests", "pi_stub.py")
PASS = 'GDD-VERDICT: {"verdict": "pass", "reason": "stub通过", "evidence": ["e1"], "feedback": ""}'


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def main():
    home = tempfile.mkdtemp(prefix="gdd-steer-home-")
    os.environ["GDD_STEER_HOME"] = home
    os.environ.pop("GDD_STEER", None)
    pi_home = tempfile.mkdtemp(prefix="gdd-steer-pi-")
    settings = os.path.join(pi_home, "settings.json")
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "baseUrl": "https://api.example.com/v1",
                   "apiKey": "k", "model": "m"}, f)
    os.environ["GDD_PI_SETTINGS"] = settings
    os.environ["GDD_PI_AGENT_DIR"] = os.path.join(pi_home, "agent")
    os.environ["GDD_PI_SESSION_DIR"] = os.path.join(pi_home, "sessions")

    from gdd import steer
    from gdd.cli import main as cli_main
    from gdd.orchestrator import Orchestrator
    from gdd.runner import AgentRunner

    print("[S1] 队列与 hook")
    proj = tempfile.mkdtemp(prefix="gdd-steer-proj-")
    check(steer.pending(proj) == "", "空队列")
    n = steer.append(proj, "不要用 Redis")
    check(n == 1, "追加一条")
    qpath = steer.queue_path(proj)
    check(not os.path.commonpath([os.path.realpath(qpath), os.path.realpath(proj)]).startswith(os.path.realpath(proj)),
          "队列不在项目目录内")
    hook = subprocess.run(
        [sys.executable, os.path.join(ROOT, "gdd", "steer.py"), "hook", qpath],
        input='{"tool_name":"Bash"}',
        capture_output=True, text=True, timeout=10,
    )
    check(hook.returncode == 0, "hook 退出码 0")
    payload = json.loads(hook.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    check(payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse", "事件名 PostToolUse")
    check("不要用 Redis" in ctx and "不是门禁" in ctx, "additionalContext 含补充且标明不是门禁")
    check(steer.pending(proj) == "", "hook 已消费")
    hook2 = subprocess.run(
        [sys.executable, os.path.join(ROOT, "gdd", "steer.py"), "hook", qpath],
        input="{}",
        capture_output=True, text=True, timeout=10,
    )
    check(hook2.returncode == 0 and hook2.stdout.strip() == "", "空队列 hook 无输出")
    full = "测" * steer.MAX_CHARS
    steer.append(proj, full)
    hook3 = subprocess.run(
        [sys.executable, os.path.join(ROOT, "gdd", "steer.py"), "hook", qpath],
        input="{}",
        capture_output=True, text=True, timeout=10,
    )
    ctx3 = json.loads(hook3.stdout)["hookSpecificOutput"]["additionalContext"]
    left = steer.pending(proj)
    check(len(ctx3) <= steer.MAX_CHARS, f"含前缀的 additionalContext 不超过 {steer.MAX_CHARS}（{len(ctx3)}）")
    check(left and full == (ctx3[len(steer._CONTEXT_PREFIX):] + left), "超出前缀预算的正文仍留在队列")

    print("[S2] steer 不拿编排锁")
    gdd_dir = os.path.join(proj, ".gdd")
    os.makedirs(gdd_dir, exist_ok=True)
    lockf = open(os.path.join(gdd_dir, "lock"), "w")
    fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = cli_main(["steer", "改成写本地文件", "--root", proj])
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()
    check(rc == 0, "持锁时 gdd steer 仍成功")
    check("改成写本地文件" in steer.pending(proj), "补充已入队")

    print("[S3] pi 主体命令加载 steer 扩展，门禁不加载")
    os.environ["GDD_AGENT_BIN"] = sys.executable
    os.environ.pop("GDD_AGENT_PRE_ARGS", None)
    os.environ.pop("GDD_AGENT_EXTRA_ARGS", None)
    runner = AgentRunner(proj)
    cmd = runner._build_cmd(["Read"], 5, steer=True)
    exts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--extension"]
    check(any(p.endswith("steer_pi_extension.mjs") for p in exts),
          "主体命令加载 steer 扩展")
    check("--settings" not in cmd, "不再使用 Claude --settings")
    plain = AgentRunner(proj)._build_cmd(["Read"], 5, steer=False)
    check(not any(p.endswith("steer_pi_extension.mjs")
                  for p in [plain[i + 1] for i, a in enumerate(plain)
                            if a == "--extension"]),
          "未要求 steer 时不加载扩展")
    env = runner._prepare_env(True)
    check(env.get("GDD_STEER_QUEUE") == qpath, "扩展从环境拿到同一队列路径")
    check(env.get("GDD_STEER_SCRIPT", "").endswith("steer.py"), "扩展用 Python 队列实现")
    check("--settings" not in plain and "--mcp-config" not in plain
          and "--plugin-dir" not in plain, "命令不含 Claude 专用参数")

    print("[S4] 未被工具消费时补送一次")
    work = tempfile.mkdtemp(prefix="gdd-steer-work-")
    script = os.path.join(work, "stub.json")
    with open(script, "w", encoding="utf-8") as f:
        json.dump([
            {"patterns": ["用户在主体执行过程中补充"], "text": "已按补充改完"},
            {"patterns": ["GDD-ROLE"], "text": "主体完成"},
        ], f, ensure_ascii=False)
    os.environ["GDD_AGENT_BIN"] = sys.executable
    os.environ["GDD_AGENT_PRE_ARGS"] = STUB
    os.environ["GDD_PI_STUB_SCRIPT"] = script
    steer.append(work, "不要用 Redis")
    o = Orchestrator(work)
    o.init({
        "id": "steer-pipe",
        "name": "steer",
        "git_delivery": {"enabled": False},
        "nodes": [{"id": "work", "name": "干活", "description": "完成任务"}],
    }, "做一个健康检查")
    status = o.run()
    check(status == "completed", f"流水线完成（{status}）")
    check(steer.pending(work) == "", "补送后队列已空")
    logs = _logs(work)
    steer_logs = [lg for lg in logs if lg["name"].endswith("-subject_steer.json")]
    subject_logs = [lg for lg in logs if lg["name"].endswith("-subject.json")]
    gate_logs = [lg for lg in logs if "-entry" in lg["name"] or "-exit" in lg["name"]]
    check(len(steer_logs) == 1, "恰好补送一轮")
    check("--session-id" in steer_logs[0]["cmd"], "补送带 --session-id")
    check("不要用 Redis" in steer_logs[0]["prompt"], "补送 prompt 含补充")
    check(all("不要用 Redis" not in lg["prompt"] for lg in subject_logs), "首轮主体 prompt 不含补充")
    check(all(not any(str(a).endswith("steer_pi_extension.mjs") for a in (lg.get("cmd") or [])) for lg in gate_logs), "门禁命令不挂 steer 扩展")
    events = [h.get("event") for h in o.state["history"]]
    check(events.count("steer_fallback") == 1, "记一次 steer_fallback")

    print("[S5] 补送失败则放回队列，原主体结果仍有效")
    work2 = tempfile.mkdtemp(prefix="gdd-steer-fail-")
    script2 = os.path.join(work2, "stub.json")
    with open(script2, "w", encoding="utf-8") as f:
        json.dump([
            {"patterns": ["用户在主体执行过程中补充"], "text": "补送崩了", "is_error": True,
             "stop_reason": "error", "error": "fallback failed"},
            {"patterns": ["GDD-ROLE"], "text": "主体完成"},
        ], f, ensure_ascii=False)
    os.environ["GDD_PI_STUB_SCRIPT"] = script2
    steer.append(work2, "保留这条")
    o2 = Orchestrator(work2)
    o2.init({
        "id": "steer-fail",
        "name": "steer-fail",
        "git_delivery": {"enabled": False},
        "nodes": [{"id": "work", "name": "干活", "description": "完成任务"}],
    }, "需求")
    status2 = o2.run()
    check(status2 == "completed", f"补送失败不推翻已成功的主体（{status2}）")
    check("保留这条" in steer.pending(work2), "失败后补充回到队列")
    check(any(h.get("event") == "steer_fallback_fail" for h in o2.state["history"]),
          "记 steer_fallback_fail")

    print("[S6] 补送被中断：原文回到队列，已成功的 sid 已落盘")
    work3 = tempfile.mkdtemp(prefix="gdd-steer-int-")
    script3 = os.path.join(work3, "stub.json")
    with open(script3, "w", encoding="utf-8") as f:
        json.dump([{"patterns": ["GDD-ROLE"], "text": "主体完成"}], f)
    os.environ["GDD_PI_STUB_SCRIPT"] = script3
    steer.append(work3, "中断也要留住")
    orig_run = AgentRunner.run

    def _boom(self, prompt, **kwargs):
        if "用户在主体执行过程中补充" in prompt:
            raise KeyboardInterrupt()
        return orig_run(self, prompt, **kwargs)

    AgentRunner.run = _boom
    o3 = Orchestrator(work3)
    o3.init({
        "id": "steer-int",
        "name": "steer-int",
        "git_delivery": {"enabled": False},
        "nodes": [{"id": "work", "name": "干活", "description": "完成任务"}],
    }, "需求")
    try:
        try:
            o3.run()
            check(False, "补送应被 KeyboardInterrupt 打断")
        except KeyboardInterrupt:
            pass
    finally:
        AgentRunner.run = orig_run
    check("中断也要留住" in steer.pending(work3), "中断后补充回到队列")
    st3 = json.load(open(os.path.join(work3, ".gdd", "state.json"), encoding="utf-8"))
    check(st3["node_states"]["work"]["sessions"].get("subject") == "pi-stub-session",
          "已成功主体的 sid 已落盘")
    check(st3["node_states"]["work"]["phase"] == "working", "中断后相位仍是 working")
    check(not steer.subject_active(work3), "中断后主体标记已清")

    print("ALL STEER PASSED")


def _logs(root):
    d = os.path.join(root, ".gdd", "logs")
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(d, fn), encoding="utf-8") as f:
            item = json.load(f)
        item["name"] = fn
        out.append(item)
    return out


if __name__ == "__main__":
    main()
