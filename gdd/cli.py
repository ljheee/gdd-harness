"""gdd 命令行入口。

用法：
  gdd init --name <迭代名> [--pipeline delivery|<file>] [--requirement <file|->] [--branch] [--base]
  gdd run [-v]          推进直到需要人工介入或结束
  gdd status            查看状态与节点进度
  gdd approve --yes | --reject --feedback "..."                    裁决人工准入
  gdd chat --node <id>     节点澄清窗口内对话（/done 结束，审批挂起时触发重验）
  gdd retry             blocked 后人工重试当前节点（清零打回计数）
  gdd abort             中止流水线
  gdd reset             删除 .gdd 重新开始（需确认）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .builtins import DELIVERY_PIPELINE
from .iteration import needs_iteration, prepare as prepare_iteration
from .orchestrator import LockBusyError, Orchestrator

BUILTINS = {"delivery": DELIVERY_PIPELINE}


def _load_pipeline_file(path: str):
    """读取 pipeline 文件：JSON 或 YAML（可选 PyYAML）。"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return json.loads(text)
    # YAML：可选依赖（零依赖不破坏；未安装时给出明确指引）
    try:
        import yaml  # type: ignore
    except ImportError:
        raise RuntimeError(
            "检测到 YAML 格式的 pipeline 文件，但未安装 PyYAML（pip3 install pyyaml）")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        # 与 UI 保存路径一致：语法错误变成 ValueError，HTTP 层才能回 JSON 400。
        raise ValueError(f"YAML 无法解析: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("YAML 顶层必须是 mapping（nodes 列表）")
    return data


def cmd_init(args) -> int:
    o = Orchestrator(args.root)
    if os.path.exists(o.state_path):
        print(f"已初始化：{o.state_path}（gdd reset 可重来）")
        return 1
    if args.pipeline in BUILTINS:
        pipeline = json.loads(json.dumps(BUILTINS[args.pipeline]))  # deep copy
        builtin = True
    else:
        try:
            pipeline = _load_pipeline_file(args.pipeline)
        except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as e:
            print(f"读取 pipeline 文件失败: {e}")
            return 1
        builtin = False

    try:
        pipeline = prepare_iteration(
            pipeline, args.root,
            name=getattr(args, "name", "") or "",
            branch=getattr(args, "branch", "") or "",
            base=getattr(args, "base", "") or "")
    except ValueError as e:
        print(f"初始化失败: {e}")
        return 1

    if args.requirement == "-":
        requirement = sys.stdin.read()
    elif args.requirement:
        try:
            with open(args.requirement, "r", encoding="utf-8") as f:
                requirement = f.read()
        except OSError as e:
            print(f"读取需求文件失败: {e}")
            return 1
    else:
        requirement = ""

    try:
        o.init(pipeline, requirement)
    except (ValueError, TypeError, AttributeError, RuntimeError, KeyError) as e:
        print(f"初始化失败: {e}")
        return 1
    print(f"流水线已初始化：{pipeline.get('name', pipeline.get('id'))}"
          f"{'（内置）' if builtin else ''}")
    print(f"节点数：{len(pipeline['nodes'])}")
    n_approval = sum(1 for n in pipeline["nodes"] if n.get("approval", {}).get("required"))
    print(f"人工准入节点：{n_approval}")
    print(f"状态文件：{o.state_path}")
    it = o.state.get("iteration") or {}
    if it.get("output"):
        print(f"迭代目录：{it['output']}")
        prior = o.state.get("prior_contracts") or []
        if prior:
            print("只读基线：" + "、".join(prior))
    # P1-3：init 时即报 git 交付实况（README:94 承诺「控制台显式警示」——
    # 曾只在 status/_print_status 打印，init/go 两入口零提示，外层仓子目录
    # 用户全程以为在交付）
    if pipeline.get("git_delivery", {}).get("enabled", True):
        if o.state.get("git_delivery_disabled"):
            reason = o.state.get("git_delivery_disabled_reason") or "未知原因"
            print(f"⚠ git 交付已停用：{reason}。")
            print("  如需启用：修复上述原因后 gdd reset 重开。")
        else:
            br = (pipeline.get("git_delivery") or {}).get("branch", "gdd/delivery")
            if o.state.get("git_baseline_failed"):
                # P2-2：基线提交失败（failing hook/gpgsign 等）——不得打印
                # 与事实相反的「已就绪」；git_baseline_fail 详情在 history
                print(f"⚠ git 交付分支 {br} 已建，但基线提交失败（gdd status / "
                      f"GDD_VERBOSE=1 查看 git_baseline_fail 原因）；exit 交付可能不完整")
            else:
                print(f"git 交付：已就绪（交付分支 {br}，exit 通过即提交）")
    print("运行：gdd run")
    return 0


def _load_or_exit(args, quiet: bool = False):
    o = Orchestrator(args.root)
    try:
        if not o.load():
            if not quiet:
                print("未初始化（先 gdd init）")
            return None
    except RuntimeError as e:
        if not quiet:
            print(f"⛔ {e}")
        return None
    return o


def cmd_run(args) -> int:
    o = _load_or_exit(args)
    if o is None:
        return 1
    try:
        status = o.run()
    except RuntimeError as e:
        print(f"⛔ {e}")
        return 1
    except KeyboardInterrupt:
        print("\n已中断（锁已释放，进度已落盘；可随时 gdd run 续跑）")
        return 130
    _print_status(o)
    if status == "awaiting_approval":
        print("\n⏸ 等待人工准入：gdd approve --yes | gdd approve --reject --feedback \"...\"")
        if (o.last_refusal or "").strip():
            # 交互审批中产物被改/完整性拒绝：引擎已设拒因但本路径曾静默（go/approve
            # 都打印），不让用户盲猜审批为何未消费
            print(f"⚠ 上一轮审批未生效：{(o.last_refusal or '').strip()}")
    elif status == "blocked":
        print(f"\n⛔ 已阻塞：{o.state.get('blocked_reason')}")
        print("处理：修复问题后 gdd retry，或 gdd abort")
    elif status == "completed":
        print("\n✅ 流水线完成")
    elif status == "aborted":
        print("\n🛑 流水线已中止")
    return 0


def cmd_status(_args) -> int:
    o = _load_or_exit(_args)
    if o is None:
        return 1
    _print_status(o)
    return 0


def _print_status(o: Orchestrator) -> None:
    st = o.state
    print(f"流水线：{o.pipeline.name}（{o.pipeline.id}）  状态：{st['status']}")
    print("-" * 56)
    for n in o.pipeline.nodes:
        # corrupt-but-parseable state 防御：缺节点条目时优雅降级而非裸 KeyError
        #（load 校验 phase 在场，但 pipeline 有节点而 state 无条目的形态仍可达）
        ns = st["node_states"].get(n.id) or {}
        mark = {"pending": " ", "working": "▶", "exit_retry": "↻", "adversarial_retry": "↻", "exit_passed": "⏸", "done": "✓"}.get(ns.get("phase"), "?")
        approval_mark = " [人工]" if n.approval.required else ""
        bounce = f" 打回{ns.get('bounces', 0)}/{n.max_bounces}" if ns.get("bounces") else ""
        print(f" {mark} {n.name}（{n.id}）{approval_mark}{bounce}")
    if st.get("pending_approval"):
        print("-" * 56)
        print(st["pending_approval"].get("summary", "（无摘要）"))
    if st.get("blocked_reason"):
        print("-" * 56)
        print(f"⛔ {st['blocked_reason']}")
    if st.get("push_failed"):
        print("-" * 56)
        pf = st["push_failed"]
        print(f"⚠ 交付分支推送失败（{pf.get('remote')}）: {pf.get('reason')}")
        print("  下次 gdd run 将自动重试补推；修复网络/权限后可手动 git push。")
    if st.get("stray_delivery"):
        print("-" * 56)
        sd = st["stray_delivery"]
        print(f"⚠ 交付不完整：提交滞留 {sd.get('ref')}（节点 {sd.get('node')}）")
        print("  处置：cherry-pick/merge 该分支后重跑；或确属本人工作/已处置时")
        print("  gdd acknowledge 人工确认清除（快照刷新为当前基线）。")
    if st.get("commit_failed"):
        print("-" * 56)
        cf = st["commit_failed"]
        print(f"⚠ 交付提交失败（节点 {cf.get('node')}）: {cf.get('reason')}")
        print("  产物可能未进入交付分支，请处理后重跑。")
    if st.get("git_delivery_disabled"):
        print("-" * 56)
        reason = st.get("git_delivery_disabled_reason") or "未知原因"
        print(f"⚠ git 交付已停用：{reason}。")
        print("  如需启用：修复上述原因后 gdd reset 重开。")
    from gdd import steer as steer_mod
    try:
        queued = steer_mod.pending(o.root) if steer_mod.enabled() else ""
    except OSError:
        queued = ""
    if queued:
        print("-" * 56)
        n = steer_mod.queued_count(o.root)
        print(f"✉ 有未送达的补充（{n} 条）。"
              "主体下一次工具返回时送入；本轮不再调用工具则会话结束时补送一次。")
    if _verbose():
        print("-" * 56)
        # GDD_VERBOSE 兑现“完整 history”承诺：全量打印（含超过 500 条封顶被截的老事件）
        for h in st["history"]:
            # 单条损坏（非 dict）不砖化整个 status 输出——降级打印原始形态
            if not isinstance(h, dict):
                print(f"  [损坏条目] {json.dumps(h, ensure_ascii=False, default=str)[:160]}")
                continue
            print(f"  {h.get('ts', '')} {h.get('event', '')} {json.dumps({k: v for k, v in h.items() if k not in ('ts', 'event')}, ensure_ascii=False)[:160]}")


def _verbose() -> bool:
    return bool(os.environ.get("GDD_VERBOSE"))


def cmd_approve(args) -> int:
    o = _load_or_exit(args)
    if o is None:
        return 1
    if not (args.yes or args.reject):
        print("需要 --yes 或 --reject")
        return 1
    if args.yes and args.reject:
        # 互斥：argparse 无互斥组时静默采纳 approve 丢弃 reject+feedback，
        # 人工门禁边界 fail-open——必须显式拒绝
        print("--yes 与 --reject 互斥，只能选一个")
        return 1
    try:
        progressed = o.resolve_approval(approved=bool(args.yes), feedback=args.feedback or "")
    except RuntimeError as e:
        if isinstance(e, LockBusyError):
            # 锁忙有两种：审批未生效（状态仍 awaiting_approval，必须报失败 exit 1）
            # 与审批已生效但自动推进遇锁忙（不算失败）。以磁盘状态为准判定，
            # 不再凭异常抛出点猜（曾把未生效误报为「已生效」exit 0，审批静默丢失）。
            # 非法上属场景之外（如被并发 reject 到 blocked / state 被删）：一律报
            # 未生效——「已生效」必须在磁盘状态上能被证实才可宣称
            # P1-11（R6，双证规则）：LockBusyError 只在 acquire 失败时抛出
            # （resolve_approval 未做任何变更），本进程的审批 100% 未生效。
            # 磁盘上的 running/completed 同样是「无可批事项」的稳态——外部
            # 进程推进的结果，不是「你的审批被消费」的证据（曾据此宣称
            # 「ℹ️ 审批已生效」exit 0，违反「已生效必须在磁盘状态上可证实」
            # 的自述不变式）。锁忙一律如实报未生效；磁盘态作为线索附注。
            o2 = _load_or_exit(args, quiet=True)
            st2 = o2.state.get("status") if o2 else "不可读"
            hint = ""
            if o2 is not None:
                if st2 == "awaiting_approval":
                    hint = "（审批门仍在挂起，重试即可）"
                elif st2 in ("running", "completed"):
                    hint = ("（另一进程在推进或已收尾；你的请求未生效——"
                            "若需要审批，等锁释放后重试）")
                elif st2 == "blocked":
                    hint = "（当前 blocked，可能被并发驳回或门禁打回）"
            print(f"⛔ 审批未生效（锁忙）：{e}{hint}")
            if o2 is not None:
                _print_status(o2)
            return 1
        print(f"⛔ {e}")
        return 1
    if not progressed:
        # resolve_approval 返回 False = 本进程未消费审批项（无待批/产物变更
        # 拒批/指纹拦截等）。驳回触达 max_bounces 置 blocked 时引擎返回 True
        # （与 UI 一致），不再落入本分支。磁盘 blocked + False 只可能来自
        # 外部并发（门禁打回/他进程驳回）或拒绝原因旁路——如实报未生效。
        refusal = o.last_refusal or "当前没有待处理的人工准入。"
        if o.state.get("status") == "blocked":
            print(f"⛔ 审批未生效：{refusal}（当前状态 blocked，可能被并发驳回或"
                  f"门禁打回；gdd status 查看后重试）")
            _print_status(o)
            return 1
        print(f"⛔ 审批未生效：{refusal}")
        return 1
    if progressed and o.state["status"] not in ("blocked", "completed"):
        try:
            o.run()
        except KeyboardInterrupt:
            # P2-18：自动续跑同 cmd_run 的中断语义（最长 2h 会话中 Ctrl-C
            # 是常态操作）：提示落盘/续跑，而非 ~20 帧裸 traceback
            print("\n已中断（锁已释放，进度已落盘；可随时 gdd run 续跑）")
            return 130
        except RuntimeError as e:
            if isinstance(e, LockBusyError):
                # 审批已落盘生效（progressed=True 走到这里说明 resolve 成功），
                # 只是本轮自动推进让位给持锁的外部进程——生效动作不能报失败
                # （P2-19）；状态机推进由外部进程完成，不丢。
                print(f"ℹ️ 审批已生效，自动推进遇锁忙（外部进程正在推进）：{e}")
                _print_status(o)
                return 0
            print(f"⛔ {e}")
            return 1
    _print_status(o)
    return 0


def cmd_retry(args) -> int:
    o = _load_or_exit(args)
    if o is None:
        return 1
    try:
        retried = o.retry()
    except RuntimeError as e:
        print(f"⛔ {e}")
        return 1
    if not retried:
        # retry 被拒（非 blocked / 完整性拒绝）：动作无效必须非零退出，
        # 否则脚本视角「重试成功」而状态未变（cmd_approve 拒绝即 exit 1，对称）
        return 1
    if not args.no_run:
        try:
            o.run()
        except KeyboardInterrupt:
            # P2-18：同 cmd_run/approve 的中断语义，不裸 traceback
            print("\n已中断（锁已释放，进度已落盘；可随时 gdd run 续跑）")
            return 130
        except RuntimeError as e:
            print(f"⛔ {e}")
            return 1
    _print_status(o)
    return 0


def cmd_abort(args) -> int:
    o = _load_or_exit(args)
    if o is None:
        return 1
    try:
        aborted = o.abort()
    except RuntimeError as e:
        print(f"⛔ {e}")
        return 1
    if not aborted:
        # 终态（completed/aborted）时动作未发生，不得宣称「已中止」exit 0
        return 1
    print("已中止。")
    return 0


def cmd_acknowledge(args) -> int:
    """人工确认滞留警示（P2-9/P1-2）：git 无法区分「主体会话的提交」与
    「用户本人的提交」，误伤用户与放走主体不可两全；显式人工裁决后清除。"""
    o = _load_or_exit(args)
    if o is None:
        return 1
    try:
        cleared = o.acknowledge()
    except RuntimeError as e:
        print(f"⛔ {e}")
        return 1
    if not cleared:
        print("无滞留警示可确认（stray_delivery 为空）。")
        return 1
    print("已确认：滞留警示已清除（分支快照已刷新为当前基线）。")
    return 0


def cmd_reset(args) -> int:
    o = Orchestrator(args.root)
    if not args.yes:
        print("reset 将删除 .gdd（状态与日志），确认请加 --yes")
        return 1
    import shutil
    if os.path.isdir(o.gdd_dir):
        # 取 flock 再删：防止外部 gdd run 持锁运行中被 reset 删掉 lock 文件，
        # 导致新旧进程互斥失效（rmtree 会连 .gdd/lock 一起删）
        if not o._acquire_lock():
            print("拒止：有 gdd 进程正在运行（.gdd/lock 被占用），请先等待或 abort")
            return 1
        try:
            shutil.rmtree(o.gdd_dir)
        finally:
            o._release_lock()  # 显式释放锁 fd，与 ui.py reset 对称（不依赖进程退出）
    print("已重置。")
    return 0


def _print_chat_reply(r: dict) -> None:
    """澄清回复：已流式则只补换行，避免整段重复打印。"""
    if r.get("streamed"):
        print()
    else:
        print(f"AI> {r.get('reply', '')}")


def cmd_steer(args) -> int:
    """working 期间补充信息。不拿 .gdd/lock，可在另一个终端执行。"""
    from gdd import steer as steer_mod
    text = " ".join(args.message or []).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not steer_mod.enabled():
        print("GDD_STEER=off，未入队。")
        return 1
    if not text:
        print('用法: gdd steer "补充内容"（另开终端，不必停止正在跑的 gdd run / gdd go）')
        return 1
    if len(text) > steer_mod.MAX_CHARS:
        print(f"消息过长（≤{steer_mod.MAX_CHARS} 字符），请拆分发送")
        return 1
    try:
        steer_mod.append(os.path.abspath(args.root), text)
    except ValueError as e:
        print(f"⛔ {e}")
        return 1
    print(steer_mod.delivery_hint(args.root))
    return 0


def cmd_chat(args) -> int:
    """要点澄清终端对话：与节点的澄清会话逐轮交互，/done 结束。"""
    from gdd.approval import is_interactive
    from gdd.stream_ui import factory_for_tty
    o = _load_or_exit(args)
    if o is None:
        return 1
    # 与 cmd_go 同口径：主线程 + stdin/stdout 均为 tty 才开流式
    # （避免仅 stdout tty / 后台线程持锁 print）
    if is_interactive():
        o.on_stream = factory_for_tty()
    node_id = args.node or o.state.get("current", "")
    node = o.pipeline.node(node_id)
    if node is None:
        print(f"节点不存在: {node_id}")
        return 1
    if not node.clarify:
        print(f"节点 {node_id} 未开启要点澄清（模板中 clarify: true 才允许）")
        return 1
    ctx = o._clarify_context()
    if ctx is None or ctx["node"].id != node_id:
        print(f"节点 {node_id} 当前不在澄清窗口（需处于节点启动前或审批挂起）")
        return 1
    print(f"── 要点澄清：{node.name}（{node.id}）──")
    print("输入消息回车发送；/done 结束澄清并触发重验；/q 直接退出。")
    while True:
        try:
            line = input("你> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\n（已退出；对话记录保留在 .gdd/dialogs/）")
            return 130  # 与 cmd_run/cmd_go 中断语义一致
        if not line:
            continue
        if len(line) > 20000:  # 与 UI 限一致（ui.py chat 分支）
            print("消息过长（≤20000 字符），请拆分发送")
            continue
        if line == "/q":
            return 0
        if line == "/done":
            try:
                r = o.clarify_done(node_id)
            except RuntimeError as e:
                print(f"⛔ {e}")
                return 1
            if not r.get("ok"):
                print(f"⛔ {r.get('error')}")
                return 1
            print("✅ 澄清结束" + ("；产出已更新，将重过 exit 门禁（gdd run 继续）" if r.get("recheck") else "。"))
            return 0
        try:
            r = o.chat(node_id, line)
        except KeyboardInterrupt:
            # 与 input() 对称：chat/流式期间 Ctrl-C 也干净退出（runner 内已 killpg）
            print("\n（已退出；对话记录保留在 .gdd/dialogs/）")
            return 130
        except RuntimeError as e:
            print(f"⛔ {e}")
            return 1
        if not r.get("ok"):
            print(f"⛔ {r.get('error')}")
            continue
        _print_chat_reply(r)


# ------------------------------------------------------------ go 会话模式

def _is_lock_busy(e: Exception) -> bool:
    """判定异常是否为锁忙（外部进程短暂持锁，go 应回循环而非退出）。

    按异常类型判定（引擎专用 LockBusyError）——文案改动不再影响判定。
    """
    return isinstance(e, LockBusyError)


def _wait_lock(o: Orchestrator, what: str) -> None:
    """锁忙退避：等待外部 gdd 进程释放锁（0.5s 步进 + 每 5s 提示）。

    防止外部长期持锁时 go 主循环空转（实测无退避可达 ~4800 次/秒 load）。
    Ctrl-C 语义同主循环（130 退出）。
    """
    import time as _time
    print(f"  ⏳ 外部 gdd 进程持锁中，等待其完成后继续{what}……（Ctrl-C 退出 go）")
    waited = 0.0
    while True:
        _time.sleep(0.5)
        waited += 0.5
        if o._acquire_lock():
            o._release_lock()
            return
        if waited >= 5.0:
            print("  ⏳ 仍在等待锁（外部 gdd 命令运行较久）……")
            waited = 0.0


_EVENT_LINES = {
    "entry_pass": "  ✔ 前置门禁通过",
    "entry_fail": "  ↩ 打回上游：{reason}",
    "exit_pass": "  ✔ 出口门禁通过",
    "exit_fail": "  ↩ 打回主体重做",
    "awaiting_approval": "  ⏸ 人工准入挂起",
    "blocked": "  ⛔ 阻塞",
    "node_done": "  ✔ 节点完成：{node}",
    "subject_resume": "  ⋯ 主体会话续用（resume）",
    "subject_done": "  ⋯ 主体会话完成",
    "pipeline_completed": "  ✅ 流水线完成",
    "approval_pass": "  ✔ 审批通过",
    "approval_reject": "  ↩ 审批驳回（反馈注入上游）",
    "bounce_current": "  ↩ exit 打回主体重做",
    "bounce_prev": "  ↩ entry 打回上游重做",
    "approval_refused_artifact_changed": "  ⚠ 审批期间产物已变更，拒绝放行",
    "approval_refused_integrity": "  ⚠ 审批被指纹门禁拒绝",
    "retry_refused_integrity": "  ⚠ 重试被指纹门禁拒绝",
    "git_push_fail": "  ⚠ 交付分支推送失败（下次 run 自动补推）",
    "git_commit_fail": "  ⚠ 交付提交失败（产物可能未入交付分支）",
    "git_commit": "  ✔ 已提交交付分支",
    "git_push": "  ✔ 已推送交付分支",
    "notify_print": "  ✔ done，可以上线",
    "session_resume_fail": "  ⚠ 会话续用失败（已回退全新会话）",
    "steer_fallback": "  ✉ 补充信息未赶上工具返回，已补送一轮",
    "steer_fallback_fail": "  ⚠ 补充信息补送失败（已放回队列）",
}


def _go_event_printer(event: str, **kw) -> None:
    """gdd go 的事件呈现层（on_event 回调；只做格式化打印）。

    仅用零占位符文案——kw 键随事件不同而异，带占位符的 format 在缺键时
    会 KeyError（被引擎静默吞掉，呈现缺失难祭）。需要带参呈现的（如
    node_done.node）由对应事件确保必有该 kw。
    """
    fmt = _EVENT_LINES.get(event)
    if fmt:
        try:
            print(fmt.format(**{k: v for k, v in kw.items()}))
        except (KeyError, IndexError):
            print(fmt)  # 缺键退化为无参呈现


def _go_guide_init(o: Orchestrator) -> bool:
    """未初始化时的交互引导：选模板 → 输需求 → init。"""
    print("未初始化。选择模板：")
    print("  1) delivery（内置：需求交付流水线，8 节点）")
    print("  2) 输入模板文件路径（JSON/YAML）")
    try:
        choice = input("> ").strip()
    except EOFError:
        print("\n（EOF，取消引导）")
        return False
    if choice == "1" or choice == "":
        pipeline_src = "delivery"
    else:
        pipeline_src = choice
    if pipeline_src in BUILTINS:
        pipeline = json.loads(json.dumps(BUILTINS[pipeline_src]))
    else:
        try:
            pipeline = _load_pipeline_file(pipeline_src)
        except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as e:
            print(f"读取 pipeline 文件失败: {e}")
            return False
    print("输入需求（一行文本；或需求文件路径）：")
    try:
        req = input("> ").strip()
    except EOFError:
        print("\n（EOF，取消引导）")
        return False
    requirement = ""
    if req and os.path.isfile(req):
        try:
            with open(req, "r", encoding="utf-8") as f:
                requirement = f.read()
        except OSError as e:
            print(f"读取需求文件失败: {e}")
            return False
    else:
        requirement = req
    name = ""
    if needs_iteration(pipeline):
        print("迭代名（本次产物写入 iterations/<名字>/，分支默认 gdd/<名字>）：")
        try:
            name = input("> ").strip()
        except EOFError:
            print("\n（EOF，取消引导）")
            return False
    try:
        pipeline = prepare_iteration(pipeline, o.root, name=name)
    except ValueError as e:
        print(f"初始化失败: {e}")
        return False
    try:
        o.init(pipeline, requirement)
    except (ValueError, TypeError, AttributeError, RuntimeError, KeyError) as e:
        print(f"初始化失败: {e}")
        return False
    print(f"流水线已初始化：{pipeline.get('name', pipeline.get('id'))}（{len(pipeline['nodes'])} 节点）")
    # P1-3：go 流程与 init 命令同报 git 交付实况（外层仓停用零提示曾全程误导）
    if pipeline.get("git_delivery", {}).get("enabled", True):
        if o.state.get("git_delivery_disabled"):
            reason = o.state.get("git_delivery_disabled_reason") or "未知原因"
            print(f"⚠ git 交付已停用：{reason}；"
                  f"如需启用：修复原因后 gdd reset 重开。")
        elif o.state.get("git_baseline_failed"):
            print("⚠ git 交付分支已建，但基线提交失败（GDD_VERBOSE=1 查看原因）；"
                  "exit 交付可能不完整")
        else:
            br = (pipeline.get("git_delivery") or {}).get("branch", "gdd/delivery")
            print(f"git 交付：已就绪（交付分支 {br}）")
    return True


def _go_approval(o: Orchestrator) -> str:
    """审批交互。返回 'loop'（已裁决/已对话，回主循环）/'exit'（保持挂起退出 go）。

    review 澄清窗口（对产出追问）内联在 [c] 选项：对话若干轮后 /done 触发
    exit 重验（或回车保持挂起回主循环）。单 terminal 目标：不退出 go 即可澄清。
    """
    pa = o.state.get("pending_approval") or {}
    print("⏸ 人工准入：" + (pa.get("summary") or o.state.get("current", "?")))
    print("  [y] 批准   [f] 驳回（需反馈）   [c] 澄清对话（/done 结束并重验）"
          "   [a] 中止   [回车] 保持挂起并退出")
    try:
        choice = input("> ").strip().lower()
    except EOFError:
        return "exit"
    if choice == "c":
        node = o.pipeline.node(o.state.get("current")) if o.state.get("current") else None
        if node is None or not node.clarify:
            print("（当前节点未开启 clarify，无法对话）")
            return "loop"
        print("◦ review 澄清窗口：对产出追问；/done 结束并触发 exit 重验；/q 退出窗口")
        while True:
            try:
                line = input("你> ").strip()
            except EOFError:
                return "exit"
            if not line or line == "/q":
                return "loop"  # 保持挂起回主循环（可再选 y/f/c）
            if len(line) > 20000:
                print("消息过长（≤20000 字符），请拆分发送")
                continue
            if line == "/done":
                try:
                    r = o.clarify_done(node.id)
                except RuntimeError as e:
                    if _is_lock_busy(e):
                        _wait_lock(o, "澄清结束")
                    else:
                        print(f"⛔ {e}")
                    return "loop"  # 退避等锁后回主循环重读状态（防陈旧覆盖）
                if not r.get("ok"):
                    print(f"⛔ {r.get('error')}")
                    return "loop"
                print("✅ 澄清结束；产出已更新，exit 将重验" if r.get("recheck")
                      else "✅ 澄清结束")
                return "loop"
            try:
                r = o.chat(node.id, line)
            except KeyboardInterrupt:
                print("\n（已退出澄清；对话记录保留在 .gdd/dialogs/）")
                return "exit"
            except RuntimeError as e:
                if _is_lock_busy(e):
                    _wait_lock(o, "澄清对话")
                else:
                    print(f"⛔ {e}")
                return "loop"
            if not r.get("ok"):
                print(f"⛔ {r.get('error')}")
                continue
            _print_chat_reply(r)
    if choice in ("", "y"):
        if choice == "":
            print("已保持挂起。继续：gdd chat --node <id>（澄清后 /done）或再次 gdd go。")
            return "exit"
        try:
            progressed = o.resolve_approval(approved=True, feedback="")
        except RuntimeError as e:
            if _is_lock_busy(e):
                _wait_lock(o, "审批")
                return "loop"  # 退避等锁后回循环重读状态再裁决
            print(f"⛔ {e}")
            return "loop"
        if not progressed and (o.last_refusal or "").strip():
            # 引擎拒绝（产物变更/完整性等）：last_refusal 必可见，不让用户盲猜重按
            print(f"⛔ 审批未生效：{(o.last_refusal or '').strip()}")
        return "loop"
    if choice == "f":
        print("驳回反馈（注入上游重做，必填）：")
        try:
            fb = input("> ").strip()
        except EOFError:
            fb = ""
        if not fb:
            print("（反馈为空，取消驳回）")
            return "loop"
        try:
            progressed = o.resolve_approval(approved=False, feedback=fb)
        except RuntimeError as e:
            if _is_lock_busy(e):
                _wait_lock(o, "驳回")
            else:
                print(f"⛔ {e}")
            return "loop"
        if not progressed and (o.last_refusal or "").strip():
            print(f"⛔ 驳回未生效：{(o.last_refusal or '').strip()}")
        return "loop"
    if choice == "a":
        try:
            o.abort()
        except RuntimeError as e:
            if _is_lock_busy(e):
                _wait_lock(o, "中止")
                return "loop"  # 中止未生效：退避后回主循环重读状态再裁决
            print(f"⛔ {e}")
        return "abort"
    print("（未识别，保持挂起）")
    return "loop"


def _go_clarify_pending(o: Orchestrator, ctx) -> str:
    """pending 澄清窗口：输入消息对话；/done 结束；空行跳过（启动主体）。

    返回 'done'（窗口结束，回主循环重读状态）/'skip'（跳过，主循环应直接
    run 启动主体——否则每轮重进窗口死循环）/'exit'（用户要求退出 go）。
    """
    node = ctx["node"]
    print(f"◦ 节点 {node.name}（{node.id}）处于澄清窗口："
          "输入消息与 AI 对齐；/done 结束；回车跳过直接启动主体")
    while True:
        try:
            line = input("你> ").strip()
        except EOFError:
            return "exit"  # 与审批/blocked 的 EOF 语义一致：退出 go（防重进窗口忙循环）
        if not line:
            return "skip"  # 跳过 → 主循环直接 run 启动主体
        if line == "/q":
            print("（退出澄清窗口与 go，节点保持待启动；再次 gdd go 可继续）")
            return "exit"
        if len(line) > 20000:
            print("消息过长（≤20000 字符），请拆分发送")
            continue
        if line == "/done":
            while True:
                try:
                    r = o.clarify_done(node.id)
                    break
                except RuntimeError as e:
                    if _is_lock_busy(e):
                        _wait_lock(o, "澄清结束")
                        continue
                    print(f"⛔ {e}")
                    return "done"
            if not r.get("ok"):
                print(f"⛔ {r.get('error')}")
                return "done"
            if r.get("recheck"):
                print("✅ 澄清结束；产出已更新，将重过 exit 门禁")
                return "done"
            # pending 窗口 /done 仅记事件（phase 仍 pending）——窗口视为已用，
            # 直接启动主体（否则窗口重开，须再回一次回车）
            print("✅ 澄清结束；启动主体（主体携带对话上下文）")
            return "skip"
        try:
            r = o.chat(node.id, line)
        except KeyboardInterrupt:
            print("\n（已退出澄清；对话记录保留在 .gdd/dialogs/）")
            return "exit"
        except RuntimeError as e:
            if _is_lock_busy(e):
                _wait_lock(o, "澄清对话")
            else:
                print(f"⛔ {e}")
            return "done"
        if not r.get("ok"):
            print(f"⛔ {r.get('error')}")
            continue
        _print_chat_reply(r)


def _go_blocked(o: Orchestrator) -> str:
    """blocked 处置：r 重试 / a 中止 / 回车退出。"""
    print(f"⛔ 阻塞：{o.state.get('blocked_reason')}")
    print("  [r] 重试（清零打回计数）   [a] 中止   [回车] 退出 go")
    try:
        choice = input("> ").strip().lower()
    except EOFError:
        return "exit"
    if choice == "r":
        try:
            o.retry()
        except RuntimeError as e:
            if _is_lock_busy(e):
                _wait_lock(o, "重试")
            else:
                print(f"⛔ {e}")
            return "loop"
        if o.state.get("status") == "blocked":
            # retry 未生效（仍在 blocked）：拒因可见，不让用户盲猜
            refusal = (o.last_refusal or "").strip()
            if refusal:
                print(f"⛔ 重试未生效：{refusal}")
        return "loop"
    if choice == "a":
        try:
            o.abort()
        except RuntimeError as e:
            if _is_lock_busy(e):
                _wait_lock(o, "中止")
                return "loop"  # 中止未生效：退避后回主循环重读状态再裁决
            print(f"⛔ {e}")
        return "abort"
    return "exit"


def cmd_go(args) -> int:
    """单命令交互模式：一个 terminal 覆盖 init 引导→推进→审批→澄清→终态。

    非 tty（后台/管道）自动退化为单次 run。设计见 docs/design-go.md。
    """
    from gdd.approval import is_interactive
    from gdd.stream_ui import factory_for_tty
    tty = is_interactive()
    o = Orchestrator(args.root, on_event=_go_event_printer, defer_approval=tty,
                     on_stream=(factory_for_tty() if tty else None))
    try:
        try:
            loaded = o.load()
        except RuntimeError as e:
            print(f"⛔ {e}")
            return 1
        if not loaded:
            if not tty:
                print("未初始化（先 gdd init）")
                return 1
            if not _go_guide_init(o):
                return 1
        if not tty:
            # 退化：与 gdd run 等价（事件仅走回调无终端交互）
            try:
                o.run()
            except RuntimeError as e:
                print(f"⛔ {e}")
                return 1
            _print_status(o)
            return 0
        print(f"GDD go — {o.pipeline.name}（{o.pipeline.id}）")
        while True:
            # 每轮以磁盘为准（UI/外部 gdd 命令可能并发推进）
            try:
                if not o.load():
                    print("state 已消失（外部 reset？），退出。")
                    return 1
            except RuntimeError as e:
                print(f"⛔ {e}")
                return 1
            st = o.state["status"]
            if st == "completed":
                print("✅ 流水线完成。")
                return 0
            if st == "aborted":
                print("🛑 流水线已中止。")
                return 0
            if st == "awaiting_approval":
                act = _go_approval(o)
                if act == "exit":
                    return 0
                continue  # loop/abort 均回主循环（abort 后 load 到 aborted 终态退出）
            if st == "blocked":
                act = _go_blocked(o)
                if act == "exit":
                    return 0
                continue
            # running：pending 澄清窗口优先询问
            ctx = o._clarify_context()
            if ctx and ctx.get("mode") == "pending":
                act = _go_clarify_pending(o, ctx)
                if act == "exit":
                    return 0
                if act == "done":
                    # 窗口结束但节点保持待启动（对话出错/锁忙后退避）：回主循环重读
                    # 状态分派，绝不落入 o.run() 违背「保持待启动」承诺
                    continue
                if act == "skip":
                    # 用户明确跳过：直接 run 启动主体（回循环会重进窗口死循环）。
                    # 锁忙退避后以磁盘为准重试：外部若已改变状态则交回主循环分派，
                    # 仍是本窗口 pending 则保留 skip 意图直接启动（不重开窗口）
                    while True:
                        try:
                            o.run()
                            break
                        except RuntimeError as e:
                            if not _is_lock_busy(e):
                                print(f"⛔ {e}")
                                return 1
                            _wait_lock(o, "主体启动")
                            if not o.load():
                                print("state 已消失（外部 reset？），退出。")
                                return 1
                            ctx2 = o._clarify_context()
                            if o.state["status"] != "running" or not (
                                    ctx2 and ctx2.get("mode") == "pending"):
                                break  # 状态已变：回主循环重新分派
                    continue
            try:
                o.run()
            except RuntimeError as e:
                if _is_lock_busy(e):
                    _wait_lock(o, "推进")
                    continue  # 锁忙：退避后回循环重读状态（与 chat/approval 一致，不退出）
                print(f"⛔ {e}")
                return 1
    except KeyboardInterrupt:
        print("\n已中断（锁已释放，进度已落盘；再次 gdd go 原地续跑）")
        return 130


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="gdd", description="GDD — Goal Driven Development harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化流水线")
    p.add_argument("--pipeline", default="delivery", help="内置名（delivery）或 pipeline JSON 文件路径")
    p.add_argument("--name", default="", help="迭代名。内置 delivery 必填，产物写入 iterations/<名字>/")
    p.add_argument("--branch", default="", help="交付分支，默认 gdd/<名字>")
    p.add_argument("--base", default="", help="从这条已有分支拉出交付分支，默认当前分支")
    p.add_argument("--requirement", default=None, help="需求文本文件路径，- 表示 stdin")
    p.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="推进流水线")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="查看状态")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("approve", help="裁决人工准入")
    p.add_argument("--yes", action="store_true", help="批准进入下一阶段")
    p.add_argument("--reject", action="store_true", help="打回当前节点")
    p.add_argument("--feedback", default=None, help="打回反馈")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("retry", help="blocked 后重试")
    p.add_argument("--no-run", action="store_true", help="仅解除 blocked，不推进")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("abort", help="中止流水线")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_abort)

    p = sub.add_parser("acknowledge", help="人工确认滞留警示已处置（清除 stray_delivery，刷新快照基线）")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_acknowledge)

    p = sub.add_parser("steer", help="working 期间补充信息（不占锁，下一次工具返回后送达）")
    p.add_argument("message", nargs="*", help="补充内容；省略且 stdin 非终端时从 stdin 读取")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_steer)

    p = sub.add_parser("chat", help="要点澄清终端对话（clarify: true 节点）")
    p.add_argument("--node", default=None, help="节点 id（默认当前节点）")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("go", help="单命令交互模式：init 引导→推进→审批→澄清，同一 terminal")
    p.add_argument("--root", default=".", help="项目根目录")
    p.set_defaults(func=cmd_go)

    p = sub.add_parser("reset", help="删除 .gdd 重新开始")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
