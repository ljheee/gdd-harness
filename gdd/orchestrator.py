"""编排引擎：文件即状态机的节点流水线调度。

状态持久化于 <root>/.gdd/state.json，可随时中断、恢复。
节点相位（phase）：pending → working → exit_passed → done；
打回：entry fail → 前一节点主体重跑；exit fail / 审批驳回 → 当前节点主体重跑；
超过 max_bounces（默认 3）→ blocked（等待人工 retry / abort）。
"""
from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from . import approval as approval_mod
from . import gates
from .models import ArtifactCheck, NodeSpec, PipelineSpec
from .runner import AgentRunner


class LockBusyError(RuntimeError):
    """另一个 gdd 进程持锁（.gdd/lock 占用）。调用方可按异常类型而非文案判定。"""

TERMINAL = ("completed", "aborted")

# git add 排除 .gdd（根级 + 任意深度嵌套及其下文件）。
# `:(exclude)**/.gdd` 只匹配路径段字面 `.gdd`，不匹配 `.gdd/state.json`；
# 必须再加 `**/.gdd/**`（及根级 `.gdd/**`）才排净嵌套会话状态。
_GIT_ADD_PATHSPEC = (
    ".",
    ":(exclude).gdd",
    ":(exclude).gdd/**",
    ":(exclude)**/.gdd",
    ":(exclude)**/.gdd/**",
)


def _fold_legacy_preset(pd):
    """legacy 展开形态折叠为 preset 形态（P1-4 迁移，仅 load 路径调用）。

    早期实例磁盘存「展开后的组值」（gdd_design{N}/gdd_all 直接写在节点上、
    无 session_preset 键）；磁盘新形态是 preset 键（展开值不落盘）。
    判定保守：无 preset 键且所有组值恰为保留字形态 → 反推 preset、剥展开值；
    组值含任何非保留字（用户手写组）→ 原样返回（手写即拒交给 from_dict）。

    指纹兼容：折叠后磁盘重写与否由 _save_pipeline 决定（指纹迁移判定
    _migrate_legacy_fingerprint 已剥 session_preset 与 session_group 键，
    两种磁盘形态剥后 canonical 一致——迁移自然对上，不需特判）。
    """
    if not isinstance(pd, dict) or pd.get("session_preset") is not None:
        return pd
    nodes = pd.get("nodes")
    if not isinstance(nodes, list):
        return pd
    import re as _re

    def reserved(g):
        return g == "gdd_all" or bool(_re.fullmatch(r"gdd_design\d*", g or ""))

    # gdd_design* 历史上只可能展开到 clarify 节点（expand 语义从未变过）；
    # 非 clarify 节点带它 → 用户手写（会被 from_dict 拒）——不代用户猜测折叠
    for n in nodes:
        if isinstance(n, dict):
            g = n.get("session_group")
            if g and g != "gdd_all" and not n.get("clarify"):
                return pd
    groups = [g for g in (n.get("session_group") for n in nodes if isinstance(n, dict)) if g]
    if not groups or not all(reserved(g) for g in groups):
        return pd  # 无组值或含用户手写组：不折叠
    # 反推 preset：全部 gdd_all → shared；出现 gdd_design* → clarify-design
    preset = "shared" if all(g == "gdd_all" for g in groups) else "clarify-design"
    out = json.loads(json.dumps(pd))  # 深拷贝（折叠不改原 dict 供指纹判定）
    for n in out["nodes"]:
        if isinstance(n, dict) and n.get("session_group"):
            n.pop("session_group", None)
    out["session_preset"] = preset
    return out

# 主体会话 turns 打满后的自动续跑上限（实跑教训：implement 80 turns 打满时
# S4+ 尚未落盘，人工 retry+resume 续跑三轮才完成；每次 resume 续 80 turns，
# 默认 4 轮即约 5×80 turns，超过仍走 gate_error → blocked 人工裁决）。
SUBJECT_AUTO_CONTINUES_MAX = gates.env_int("GDD_SUBJECT_AUTO_CONTINUES_MAX", 4)

# regex flag 归一化（前导/内联全局 flag → scoped）：models.py 定义，模板与补充路径共用
from .models import _normalize_leading_flags, _normalize_inline_flags  # noqa: E402





class Orchestrator:
    def __init__(self, root: str, on_event=None, defer_approval: bool = False,
                 on_stream=None):
        """on_event(event, **kw)：可选事件回调（gdd go 终端呈现层）。
        on_stream(role) -> callback|None：流式呈现工厂（clarify/subject；gate 应返回 None）。

        在 _record 写入 history 后、_save 落盘前同步调用（引擎线程内，持有
        flock；回调内 print 到已关闭的管道会阻塞持锁——go 的终端用法安全，
        管道用法走非 tty 退化路径无回调交互）。回调只应做轻量格式化打印；
        缺省 None 时行为与原先完全一致。

        defer_approval：True 时审批一律进入 awaiting_approval 挂起态（不内联
        终端 y/n/a）——gdd go 用：由 go 循环自己交互（回车跳过/并发裁决友好）。
        """
        self.on_event = on_event
        self.defer_approval = defer_approval
        self.on_stream = on_stream  # role -> callback|None；缺省 None = 全路径不流式
        self.root = os.path.abspath(root)
        self.gdd_dir = os.path.join(self.root, ".gdd")
        self.pipeline_path = os.path.join(self.gdd_dir, "pipeline.json")
        self.state_path = os.path.join(self.gdd_dir, "state.json")
        self.logs_dir = os.path.join(self.gdd_dir, "logs")
        self.input_path = os.path.join(self.gdd_dir, "input.md")
        self.pipeline: Optional[PipelineSpec] = None
        self.state: Dict[str, Any] = {}
        self.last_refusal = ""  # 最近一次审批拒绝原因（产物变更拦截等，供 UI/CLI 展示）
        self._lock_fd = None
        self._state_bytes: Optional[str] = None  # 最近一次落盘的 state 字节（篡改比对基准）
        self._input_text: Optional[str] = None  # 需求原文内存权威副本（input.md 防篡改）

    # ------------------------------------------------------------ init / load

    def init(self, pipeline_dict: Dict[str, Any], requirement: str = "") -> None:
        if os.path.exists(self.state_path):
            raise RuntimeError(f"已初始化：{self.state_path}（如需重来请先删除 .gdd 或 gdd reset）")
        # 先解析模板再建目录：模板非法时不残留空 .gdd/logs、.gdd/nodes
        self.pipeline = PipelineSpec.from_dict(pipeline_dict)
        os.makedirs(self.logs_dir, exist_ok=True)
        os.makedirs(os.path.join(self.gdd_dir, "nodes"), exist_ok=True)
        self._save_pipeline()
        if requirement:
            with open(self.input_path, "w", encoding="utf-8") as f:
                f.write(requirement)
        elif not os.path.exists(self.input_path):
            with open(self.input_path, "w", encoding="utf-8") as f:
                f.write("（未提供原始需求，请项目初始化节点主体说明并停止）\n")
        self.state = self._fresh_state(self.pipeline)
        # 需求原文指纹：_check_tamper 比对恢复（会话可改写 input.md 投毒后续全部
        # 节点的需求源，曾有零篡改事件的盲区）。必须在 _fresh_state 之后写入
        with open(self.input_path, "r", encoding="utf-8", errors="replace") as f:
            self._input_text = f.read()
        self.state["input_digest"] = hashlib.sha256(
            self._input_text.encode("utf-8", "replace")).hexdigest()
        self.state["pipeline_fingerprint"] = self._pipeline_fingerprint()
        self._git_delivery_setup()
        self._save()

    @staticmethod
    def _fresh_state(pipeline: PipelineSpec) -> Dict[str, Any]:
        return {
            "status": "running",  # running | awaiting_approval | blocked | completed | aborted
            "pipeline_id": pipeline.id,
            "current": pipeline.nodes[0].id,
            "node_states": {n.id: {"phase": "pending", "bounces": 0, "entry_bounces": 0, "attempts": 0,
                                    "sessions": {}}
                             for n in pipeline.nodes},
            "feedbacks": {},          # node_id -> 打回反馈（主体重跑时消费）
            "feedback_kinds": {},      # node_id -> "bounce"|"clarify"（反馈性质，结构化判据）
            "baseline_commit": None,
            "last_exit_evidence": [],
            "blocked_reason": None,
            "blocked_owner": None,   # blocked 时的责任节点（retry 时清它的计数并把执行点指回它）
            "pending_approval": None,  # {node, summary}
            "gate_error_streak": 0,    # Agent 会话自身故障连续计数（门禁/主体，非节点问题）
            "push_failed": None,        # 交付分支推送失败信息（run 时自动重试补推；非 stray 型）
            "stray_delivery": None,    # 提交滞留他分支警示（独立槽：不被 push 成败覆盖/清除）
            "git_delivery_disabled": False,  # init 检出外层仓→永久停用 git 交付（持久化，_deliver_git 消费）
            "history": [],
            "updated_at": None,
        }

    def load(self) -> bool:
        if not (os.path.exists(self.state_path) and os.path.exists(self.pipeline_path)):
            return False
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except UnicodeDecodeError as e:
            # 单字节非 UTF-8 曾逃出 load 的窄 except（只 catch JSONDecodeError）
            # 裸 traceback 于所有命令——损坏态一律 RuntimeError 契约在此补全
            raise RuntimeError(
                f"state.json 不是 UTF-8 文本（损坏或被篡改：{e}）；"
                f"请人工核验 .gdd/ 后重开（gdd reset 或删除 .gdd）") from e
        try:
            self.state = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"state.json 不是合法 JSON（损坏或被篡改：{e}）；"
                f"请人工核验 .gdd/ 后重开（gdd reset 或删除 .gdd）") from e
        # 标量/列表形态（json 合法但非对象）会让后续 dict 访问裸 TypeError，
        # 逃出「损坏态一律 RuntimeError」契约使所有命令崩——在这里拦下
        if not isinstance(self.state, dict):
            raise RuntimeError(
                f"state.json 内容不是 JSON 对象（损坏或被篡改）；"
                f"请人工核验 .gdd/ 后重开（gdd reset 或删除 .gdd）")
        missing = [k for k in ("status", "current", "node_states", "history",
                                "feedbacks", "input_digest")
                   if k not in self.state]
        if missing:
            raise RuntimeError(
                f"state.json 缺少必备字段 {missing}（损坏或被篡改）："
                f"请人工核验 .gdd/ 后重开（gdd reset 或删除 .gdd）")
        # P2-6 结构类型簇（P0 兜底）：字段存在但形态错乱曾是裸 KeyError/
        # TypeError 从 run/approve 逃出——损坏态一律在此拦下为 RuntimeError，
        # 所有命令得到友好错误并可 reset 自救
        if not isinstance(self.state["history"], list):
            raise RuntimeError(
                "state.json 的 history 不是数组（损坏或被篡改）："
                "请人工核验 .gdd/ 后重开")
        if not isinstance(self.state["feedbacks"], dict):
            raise RuntimeError(
                "state.json 的 feedbacks 不是对象（损坏或被篡改）："
                "请人工核验 .gdd/ 后重开")
        # P2-11：current 可能非字符串（list/dict unhashable 使 `in` 裸
        # TypeError 逃出 load 自身）；node_states 也可能是 list（`in` 静默
        # False 掩盖损坏）——先判型再成员检查
        if not isinstance(self.state["current"], str):
            raise RuntimeError(
                f"state.json 的 current 不是字符串（{self.state['current']!r}，"
                f"损坏或被篡改）：请人工核验 .gdd/ 后重开")
        if not isinstance(self.state["node_states"], dict):
            raise RuntimeError(
                "state.json 的 node_states 不是对象（损坏或被篡改）："
                "请人工核验 .gdd/ 后重开")
        if self.state["current"] not in self.state["node_states"]:
            # run() 取 self.state["node_states"][current] 曾裸 KeyError
            raise RuntimeError(
                f"state.json 当前节点 {self.state['current']!r} 无状态条目"
                f"（损坏或被篡改）：请人工核验 .gdd/ 后重开")
        for slot in ("pending_approval", "push_failed", "stray_delivery",
                     "commit_failed"):
            v = self.state.get(slot)
            if v is not None and not isinstance(v, dict):
                # approve 取 pending_approval.get / status 打印各槽位曾裸
                # TypeError/AttributeError
                raise RuntimeError(
                    f"state.json 的 {slot} 不是对象（{v!r}，损坏或被篡改）："
                    f"请人工核验 .gdd/ 后重开")
        # P2-12（同族扩展）：
        fk = self.state.get("feedback_kinds")
        if fk is not None and not isinstance(fk, dict):
            raise RuntimeError("state.json 的 feedback_kinds 不是对象（损坏或"
                               "被篡改）：请人工核验 .gdd/ 后重开")
        br = self.state.get("blocked_reason")
        if br is not None and not isinstance(br, str):
            raise RuntimeError("state.json 的 blocked_reason 不是字符串（损坏或"
                               "被篡改）：请人工核验 .gdd/ 后重开")
        bo = self.state.get("blocked_owner")
        if bo is not None and not isinstance(bo, str):
            raise RuntimeError("state.json 的 blocked_owner 不是字符串（损坏或"
                               "被篡改）：请人工核验 .gdd/ 后重开")
        for k, v in self.state.items():
            if (isinstance(k, str) and k.startswith("baseline_commit:")
                    and not isinstance(v, str)):
                raise RuntimeError(f"state.json 的 {k} 不是字符串（损坏或被"
                                   "篡改）：请人工核验 .gdd/ 后重开")
        self._state_bytes = raw  # 篡改比对基准
        # input.md 权威基准：load 后**不采纳磁盘内容**（P1-1：曾在 crash 后把被篡改
        # 的 input.md 采纳为内存权威副本，_check_tamper 把篡改字节原样写回，
        # 「检测→恢复」变成「检测→洗白」）。改为：以 state.input_digest 为基准，
        # 磁盘摘要一致才重建内存副本；不一致（篡改）时副本置 None，
        # _check_tamper 不盲写恢复，fail-closed 对齐 pipeline.json 的指纹校验
        try:
            with open(self.input_path, "r", encoding="utf-8", errors="replace") as f:
                disk_text = f.read()
            disk_digest = hashlib.sha256(disk_text.encode("utf-8", "replace")).hexdigest()
        except OSError:
            disk_text, disk_digest = None, ""
        if disk_text is not None and disk_digest == self.state.get("input_digest"):
            self._input_text = disk_text
        else:
            self._input_text = None
        try:
            with open(self.pipeline_path, "r", encoding="utf-8") as f:
                pd = json.load(f)
        except UnicodeDecodeError as e:
            raise RuntimeError(
                f"pipeline.json 不是 UTF-8 文本（损坏或被篡改：{e}）；"
                f"请人工核验 .gdd/ 后重开") from e
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"pipeline.json 不是合法 JSON（损坏或被篡改：{e}）；"
                f"请人工核验 .gdd/ 后重开") from e
        # legacy 展开形态折叠（P1-4 新序迁移）：早期实例磁盘存「展开后的组值」
        # （gdd_design{N}/gdd_all 直接写在节点上、无 session_preset 键）。新校验
        # 手写保留字即拒——折叠反推 preset 后重写磁盘，init/load 永续同形态。
        # 判定保守：无 preset 键 + 组值全部恰为保留字形态（任意其他值视为
        # 用户新编排手写，交由 from_dict 校验拒绝，不代用户猜测意图）
        pd = _fold_legacy_preset(pd)
        try:
            self.pipeline = PipelineSpec.from_dict(pd)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            # AttributeError：pipeline.json 可解析但结构非 dict（如 [1,2,3]）时
            # from_dict 内 d.get 直接抛 AttributeError，曾逃出窄 except 裸 traceback
            raise RuntimeError(
                f"pipeline.json 结构非法（损坏或被篡改：{e}）；"
                f"请人工核验 .gdd/ 后重开") from e
        # P2-12（同族扩展）：pipeline 已解析，节点 id 集合可交叉校验——
        # 只有当前节点查过成员；其余节点条目缺失会在 entry 打回切 current
        # 时裸 KeyError（损坏态一律 RuntimeError 契约）
        for node in self.pipeline.nodes:
            if node.id not in self.state["node_states"]:
                raise RuntimeError(
                    f"state.json 节点 {node.id} 无状态条目（损坏或被篡改）："
                    f"请人工核验 .gdd/ 后重开")
        # P2-13：pending_approval 幽灵 node 三变体（缺 node 键 / node 非 str /
        # node 不在 pipeline）曾使 approve/_clarify_context 裸 KeyError、
        # _resolve_approval_locked 对 None node 的 AttributeError 逃出命令层
        # ——在此拦下为 RuntimeError 契约
        pa = self.state.get("pending_approval")
        if pa is not None:
            for k in ("node", "summary"):
                if not isinstance(pa.get(k), str):
                    raise RuntimeError(
                        f"state.json 的 pending_approval.{k} 缺失或非字符串"
                        f"（{pa.get(k)!r}，损坏或被篡改）："
                        f"请人工核验 .gdd/ 后重开")
            if self.pipeline.node(pa["node"]) is None:
                raise RuntimeError(
                    f"state.json 的 pending_approval.node {pa['node']!r} "
                    f"不在 pipeline 节点集合（幽灵节点，损坏或被篡改）："
                    f"请人工核验 .gdd/ 后重开")
        # node_states 结构校验：可解析但被剥空（{}）时后续取 self.state["node_states"][nid]
        # 是裸 KeyError；损坏态应得到友好错误（与 UI 的优雅降级对齐），可 reset 自救
        if not isinstance(self.state.get("node_states"), dict) or not self.state["node_states"]:
            raise RuntimeError(
                "state.json 的 node_states 为空或非对象（损坏或被篡改）："
                "请人工核验 .gdd/ 后重开（gdd reset 或删除 .gdd）")
        for nid, ns in self.state["node_states"].items():
            if not isinstance(ns, dict) or "phase" not in ns:
                raise RuntimeError(
                    f"state.json 节点 {nid} 缺少 phase（损坏或被篡改）："
                    f"请人工核验 .gdd/ 后重开（gdd reset 或删除 .gdd）")
            for ck in ("bounces", "entry_bounces", "attempts"):
                # 计数器必须是非负 int：非 int（含字符串 payload）除了引擎侧
                # 语义错乱，还会经 UI 直插 DOM（XSS 面）；fail-closed 拒载
                if ck in ns and (not isinstance(ns[ck], int) or isinstance(ns[ck], bool)
                                 or ns[ck] < 0):
                    raise RuntimeError(
                        f"state.json 节点 {nid} 的 {ck} 非法（{ns[ck]!r}，应为非负整数）："
                        f"损坏或被篡改，请人工核验 .gdd/ 后重开（gdd reset 或删除 .gdd）")
            # sessions 值必须是字符串（篡改窗口可达；非字符串会在 runner 子进程构造
            # 处 TypeError 裸崩，逃出设计错误流）
            sess = ns.get("sessions")
            if sess is not None:
                if not isinstance(sess, dict):
                    raise RuntimeError(
                        f"state.json 节点 {nid} 的 sessions 非对象（损坏或被篡改）："
                        f"请人工核验 .gdd/ 后重开")
                for sk, sv in sess.items():
                    if sv is not None and not isinstance(sv, str):
                        raise RuntimeError(
                            f"state.json 节点 {nid} 的 sessions.{sk} 非字符串（{sv!r}）："
                            f"损坏或被篡改，请人工核验 .gdd/ 后重开")
        return True

    def _save(self) -> None:
        self.state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        payload = json.dumps(self.state, ensure_ascii=False, indent=2)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, self.state_path)  # 原子替换，防中断留下半截状态文件
        self._state_bytes = payload  # 更新篡改比对基准

    def _save_pipeline(self) -> None:
        """原子写运行实例（同 state：中断不产生半截 JSON）。合法变更仅此入口。"""
        tmp = self.pipeline_path + ".tmp"
        payload = json.dumps(self.pipeline.to_dict(), ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, self.pipeline_path)
        self.state["pipeline_fingerprint"] = self._pipeline_fingerprint()

    def _pipeline_fingerprint(self) -> str:
        """运行实例指纹：canonical JSON 的 sha256（前 16 位）。"""
        canonical = json.dumps(self.pipeline.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    # ---- 主体会话组（design-session-group.md）：缺省组 = node.id，与既有行为逐位兼容 ----

    def _node_group(self, node) -> str:
        """节点 → 会话组名：显式 session_group 优先，缺省 node.id（独立）。"""
        return getattr(node, "session_group", "") or node.id

    def _subject_group_sid(self, group: str) -> str:
        """组内最新主体会话 sid：扫描全部 node_states，取同组节点 sid 中
        属于流水线顺序最晚节点的那个（同组多 sid 时）；失败会话不存档，
        正常情况下同组仅一个有效 sid。"""
        best_sid = ""
        best_order = -1
        node_order = {n.id: i for i, n in enumerate(self.pipeline.nodes)}
        for nid, st in self.state.get("node_states", {}).items():
            if self._node_group_of_id(nid) != group:
                continue
            sid = (st.get("sessions") or {}).get("subject")
            if sid and node_order.get(nid, -1) > best_order:
                best_sid, best_order = sid, node_order.get(nid, -1)
        return best_sid

    def _pop_subject_group_sid(self, group: str) -> None:
        """组失效：清掉同组所有节点存档的主体 sid（干净失败，全组降级全新会话）。"""
        for nid, st in self.state.get("node_states", {}).items():
            if self._node_group_of_id(nid) == group:
                (st.get("sessions") or {}).pop("subject", None)

    def _node_group_of_id(self, nid: str) -> str:
        n = self.pipeline.node(nid)
        return self._node_group(n) if n else nid

    def verify_pipeline_integrity(self) -> bool:
        fp = self.state.get("pipeline_fingerprint")
        if fp is None:
            # init 必写指纹：缺失即 state.json 被篡改（删除指纹键即可绕过校验——
            # 比伤造哈希容易得多）。fail-closed，交给 run 的 blocked/人工核验路径
            return False
        return self._pipeline_fingerprint() == fp

    def _migrate_legacy_fingerprint(self) -> bool:
        """存量实例兼容：NodeSpec 字段增删会改变指纹。

        路径 1（增字段）：磁盘剔除 clarify/session_group/session_preset 后
        重算 == 存储指纹 → legacy 缺新字段，重写刷新。
        路径 2（删 notify）：内存 to_dict 加回磁盘/默认 notify 后重算 == 存储指纹
        → 仅 IM 周知字段退役，重写刷新（不再序列化 notify）。
        其他不一致 → 拒绝迁移，fail-closed。
        """
        fp = self.state.get("pipeline_fingerprint")
        if not fp:
            return False
        try:
            with open(self.pipeline_path, "r", encoding="utf-8") as f:
                pd = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        added_keys = {"clarify", "session_group"}  # NodeSpec 新增键（含 session 复用组，见 design-session-group.md）
        legacy = json.loads(json.dumps(pd))
        for n in legacy.get("nodes", []):
            for k in added_keys:
                n.pop(k, None)
        legacy.pop("session_preset", None)  # 流水线级新增键
        canonical = json.dumps(legacy, ensure_ascii=False, sort_keys=True)
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16] == fp:
            # legacy 实例不可能带 truthy 新字段（如 clarify=true 是新编排才有的声明）
            for n in pd.get("nodes", []):
                if any(n.get(k) for k in added_keys):
                    return False
            if pd.get("session_preset") and pd.get("session_preset") != "independent":
                return False  # 非缺省预设只可能是新编排（legacy 剥键后本就无此键）
            self._save_pipeline()  # 以内存解析结果（含默认值）重写 + 更新指纹
            return True

        # 路径 2：notify 字段已从模型移除——用磁盘 notify（或默认）填回内存形态对指纹
        rebuilt = json.loads(json.dumps(self.pipeline.to_dict()))
        disk_nodes = pd.get("nodes") or []
        for i, n in enumerate(rebuilt.get("nodes") or []):
            dn = disk_nodes[i] if i < len(disk_nodes) and isinstance(disk_nodes[i], dict) else {}
            ntf = dn.get("notify") if isinstance(dn, dict) else None
            if isinstance(ntf, dict):
                from .models import _parse_bool
                out = {"enabled": _parse_bool(ntf.get("enabled", False))}
                # 与旧 NotifySpec.to_dict 同形：空串字段不落盘
                for k in ("group", "user", "pub", "message_file"):
                    v = ntf.get(k)
                    if v:
                        out[k] = str(v)
                n["notify"] = out
            else:
                n["notify"] = {"enabled": False}
        rebuilt_canon = json.dumps(rebuilt, ensure_ascii=False, sort_keys=True)
        if hashlib.sha256(rebuilt_canon.encode("utf-8")).hexdigest()[:16] != fp:
            return False
        self._save_pipeline()
        return True

    def _check_tamper(self, where: str) -> None:
        """会话执行后校验 .gdd 关键文件未被会话篡改；以内存权威副本恢复。

        会话（主体/门禁）带文件写权限，理论上可改 state.json / pipeline.json 伪造进度
        或弱化门禁；harness 内存态是唯一权威，此处检测并覆盖回去。
        """
        restored = False
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                disk = f.read()
        except OSError:
            disk = ""
        if self._state_bytes is not None and disk != self._state_bytes:
            self._record("state_tamper_restored", where=where)
            restored = True
        # 磁盘实例 vs 内存权威副本（语义级比对，容忍格式差异）
        try:
            with open(self.pipeline_path, "r", encoding="utf-8") as f:
                disk_pipeline = json.load(f)
            disk_canon = json.dumps(disk_pipeline, ensure_ascii=False, sort_keys=True)
        except (OSError, json.JSONDecodeError):
            disk_canon = None
        mem_canon = json.dumps(self.pipeline.to_dict(), ensure_ascii=False, sort_keys=True)
        if disk_canon != mem_canon:
            self._record("pipeline_tamper_restored", where=where)
            tmp = self.pipeline_path + ".tmp"
            payload = json.dumps(self.pipeline.to_dict(), ensure_ascii=False, indent=2)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.pipeline_path)
            restored = True
        # 需求原文（.gdd/input.md）：全部 prompt 的需求源，改写即投毒。以 init 时
        # 落盘的 input_digest 为基准（跨进程仍可校），篡改即恢复并记录
        digest = self.state.get("input_digest")
        if digest:
            try:
                with open(self.input_path, "rb") as f:
                    cur = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                cur = ""
            if cur != digest:
                if self._input_text is not None:
                    # 权威副本在场（init/上轮内存）：恢复原需求源
                    tmp = self.input_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(self._input_text)
                    os.replace(tmp, self.input_path)
                    self._record("input_tamper_restored", where=where)
                    restored = True
                else:
                    # 无权威副本（crash 重启 + input.md 已被篡改）：磁盘不可信且
                    # 无基准可写回——曾在此把篡改字节原样写回记「已恢复」（检测
                    # 变洗白）。fail-closed：只记事件不恢复，run() 会因 digest
                    # 仍不符持续阻断
                    self._record("input_tamper_unrecoverable", where=where)
        if restored:
            self._save()

    def _record(self, event: str, **kw) -> None:
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
        entry.update({k: v for k, v in kw.items() if v is not None})
        self.state["history"].append(entry)
        if len(self.state["history"]) > 500:
            self.state["history"] = self.state["history"][-500:]
        if self.on_event:
            try:
                self.on_event(event, **kw)
            except Exception:
                pass  # 呈现层异常不得影响引擎主流程

    # ------------------------------------------------------------ 工具

    def _log_path(self, node: NodeSpec, kind: str) -> str:
        # 独立递增序号（history 裁剪到 500 后长度恒定，不能用 len(history)，否则日志名冲突覆盖）
        # bump 后立即落盘：崩溃窗口内重启 seq 复用会覆盖中断会话的审计日志
        seq = self.state.get("log_seq", 0) + 1
        self.state["log_seq"] = seq
        self._save()
        return os.path.join(self.logs_dir, f"{seq:04d}-{node.id}-{kind}.json")

    def _git(self, *args: str) -> Optional[str]:
        proc = self._git_proc(*args)
        if proc is not None and proc.returncode == 0:
            return proc.stdout.strip()
        return None

    def _git_proc(self, *args: str, timeout: int = 60):
        """返回原始 CompletedProcess（None = git 不可用/超时）。"""
        try:
            return subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _change_summary(self, node_id: str = "") -> str:
        # change_aware 基线按节点记（baseline_commit:<node_id>）；空 node_id 时
        # 回退「管线序最新」基线或 legacy 全局单槽（存量实例兼容）。
        # P2-8：曾按 state 键名字母序取「最新」（sorted() 对
        # baseline_commit:n1/n2/n10 排出 n1<n10<n2 的字典序≠管线序），
        # 多 marker 自定义模板的 change_aware 门禁对着错误 diff 验证、
        # 把前序节点已交付内容归因当前节点。正确序 = baseline 事件在
        # history 中的记录序 = 管线推进序（事件只在 entry 通过时按序写入）
        baseline = (self.state.get(f"baseline_commit:{node_id}")
                    if node_id else None)
        if not baseline:
            order = self.pipeline.node_order()  # 节点 id -> 管线序
            keyed = sorted(
                ((k, v) for k, v in self.state.items()
                 if isinstance(k, str) and k.startswith("baseline_commit:")
                 and v),
                key=lambda kv: order.get(kv[0].rsplit(":", 1)[1], -1))
            baseline = keyed[-1][1] if keyed else self.state.get("baseline_commit")
        parts: List[str] = []
        if baseline:
            # 对工作区 diff（含未提交改动）：当前节点产出尚未提交（提交发生在 exit 通过后），
            # 若用 baseline..HEAD 会漏掉本轮改动、多计前序节点已提交内容
            stat = self._git("diff", "--stat", baseline)
            if stat is not None:
                parts.append(f"git diff --stat {baseline[:10]}（含未提交工作区改动）:\n{stat}")
        else:
            parts.append("无 git 基线记录（或非 git 仓库），请用 git status / git diff 自行核实改动。")
        status = self._git("status", "--short")
        if status is not None and status:
            parts.append(f"git status --short:\n{status}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------ git 交付

    def _git_delivery_setup(self) -> None:
        """init 时确保 git 仓库存在并切到交付分支（失败仅记录，不阻塞流水线）。"""
        cfg = self.pipeline.git_delivery if self.pipeline else None
        if cfg is None or not cfg.enabled:
            self._record("git_delivery_skip", reason="git_delivery 未启用")
            return
        inside = self._git_proc("rev-parse", "--is-inside-work-tree")
        if inside is None or inside.returncode != 0:
            init = self._git_proc("init")
            if init is None or init.returncode != 0:
                # 与 add/commit 失败对称：呈现层不得打「已就绪」
                self.state["git_baseline_failed"] = True
                self._record("git_delivery_skip", reason="git init 失败")
                self._record("git_baseline_fail", reason=(
                    ((init.stderr if init else "") or "git init 失败").strip()[:200]))
                return
        else:
            # 已在某个 git 仓内：toplevel 必须就是任务根目录，否则是在外层仓的
            # 子目录里 init——绝不动用户的外层仓（切分支/基线提交/推送都会劫持它）
            top = self._git_proc("rev-parse", "--show-toplevel")
            toplevel = top.stdout.strip() if (top and top.returncode == 0) else ""
            if toplevel and os.path.realpath(toplevel) != os.path.realpath(self.root):
                # 持久化停用（仅记事件不落盘时，_deliver_git 仍会在 exit 时切分支劫持外层仓）
                self.state["git_delivery_disabled"] = True
                self.state["git_delivery_disabled_reason"] = (
                    f"任务根位于外层 git 仓的子目录（toplevel {toplevel}）")
                self._record("git_delivery_skip", reason=(
                    f"检测到外层 git 仓（toplevel {toplevel} ≠ 任务根 {self.root}）："
                    f"已永久停用 git 交付；如需启用请在任务根单独 git init 后 gdd reset 重开"))
                return
        cur = self._git_proc("symbolic-ref", "--short", "HEAD")
        cur_branch = cur.stdout.strip() if (cur and cur.returncode == 0) else ""
        if cur_branch != cfg.branch:
            has = self._git_proc("rev-parse", "--verify", f"refs/heads/{cfg.branch}")
            if has is not None and has.returncode == 0:
                co = self._git_proc("checkout", cfg.branch)
            else:
                co = self._git_proc("checkout", "-b", cfg.branch)
            if co is None or co.returncode != 0:
                # 持久化停用（与外层仓对称）：否则每节点 exit 都会半跑交付反复报错
                self.state["git_delivery_disabled"] = True
                self.state["git_delivery_disabled_reason"] = (
                    f"切换交付分支 {cfg.branch} 失败："
                    f"{((co.stderr if co else '') or '不可执行/超时').strip()[:150]}")
                self._record("git_delivery_skip", reason=(
                    f"切换交付分支 {cfg.branch} 失败："
                    f"{((co.stderr if co else '') or '不可执行/超时').strip()[:150]}；"
                    f"已停用 git 交付，如需启用请修复后 gdd reset 重开"))
                return
        # 初始基线提交：把 init 前已存在（或分支切换携入）的未提交内容固化为基线，
        # 避免首个节点 exit 提交扫入历史遗留改动（含已在交付分支的重 init 场景）。
        # 空仓库必须创建提交（否则交付分支是 unborn 引用，后续 rev-list/checkout 失效）
        add0 = self._git_proc("add", "-A", "--", *_GIT_ADD_PATHSPEC)
        if add0 is None or add0.returncode != 0:
            # add 失败必须显式（index.lock 竞争/权限）：静默空索引会让遗留文件
            # 事后被卷入首个节点交付，且基线记“成功”与事实不符
            self._record("git_baseline_fail", reason=(
                ((add0.stderr if add0 else "") or "git add 失败（index.lock/权限？）").strip()[:200]))
            self.state["git_baseline_failed"] = True  # P2-2：init 呈现层可查
            return
        # 只看真正暂存的内容（status --porcelain 会把未跟踪的 .gdd/ 也列出，误判为有暂存）
        staged = self._git_proc("diff", "--cached", "--name-only")
        has_staged = bool(staged is not None and staged.returncode == 0 and staged.stdout.strip())
        head = self._git_proc("rev-parse", "--verify", "HEAD")
        has_head = bool(head is not None and head.returncode == 0)
        if has_staged or not has_head:
            extra = ["--allow-empty"] if not has_staged else []
            c = self._git_proc("-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
                               "commit", *extra, "-m", "gdd(init): baseline")
            if c is not None and c.returncode == 0:
                self._record("git_baseline", branch=cfg.branch)
            else:
                # 基线失败必须显式（failing hook / gpgsign / 超时）：静默会让交付分支
                # unborn，首个 exit 交付行为异常且无迹可查。hook 输出可能走 stdout，
                # 两路都截取进事件，避免后续只报通用「commit 失败」无从排查
                out = ((c.stdout if c else "") or "").strip()
                err = ((c.stderr if c else "") or "基线提交超时/不可执行").strip()
                self._record("git_baseline_fail", reason=(f"{out} | {err}")[:200])
                self.state["git_baseline_failed"] = True  # 与 add 失败路径对称（呈现层可查）
                # 基线失败不记 git_delivery_ready（交付分支可能 unborn，
                # ready 与事实不符）；也不写分支快照基线
                return
        self._record("git_delivery_ready", branch=cfg.branch, from_branch=cur_branch or None)
        # 分支快照（P1-2 散落检测基准）：记录当前所有本地分支的 tip。
        # 交付时对比：分支 tip 变化且未并入交付分支 → 提交滞留警示，
        # 防「side 分支 commit 后切回交付分支再 exit」绕过 cur≠branch 检测
        fer = self._git_proc("for-each-ref", "refs/heads/",
                             "--format=%(refname:short) %(objectname)")
        if fer is not None and fer.returncode == 0:
            self.state["git_branch_snapshot"] = dict(
                ln.strip().split() for ln in fer.stdout.splitlines() if len(ln.strip().split()) == 2)

    def _deliver_git(self, node: NodeSpec) -> None:
        """exit 通过即把产出（.md/代码）提交到交付分支并推送（harness 执行，非 LLM）。

        - .gdd 状态目录不入库；
        - 无改动则跳过提交；
        - 主体会话若在其他分支提交过（git checkout -b feature + commit），
          切回交付分支后工作区无改动但提交滞留他分支——比对
          log <delivery_branch>..HEAD 检出，记失败并提示，绝不静默丢交付；
        - 推送失败仅记录事件，不阻塞、不打回。
        """
        cfg = self.pipeline.git_delivery
        if not cfg.enabled or self.state.get("git_delivery_disabled"):
            return
        commit_failed_before = bool(self.state.get("commit_failed"))
        # P1-4（R6 第二形态）：warned-this-round 旗标取代「槽空守卫」推演。
        # 「本轮交付是否新失败」不再从 before/after 槽状态推断（两形态都
        # 翻过车：永不清——n1 警示+n2 干净路径清不掉；重复犯例误清——
        # 上轮有警示+本轮走 ignored-skip fall-through，槽被 pop 还记假
        # 「commit_failed_cleared 本轮交付成功」）。每个失败分支显式
        # 置位 warned_this_round，成功路径只看旗标——不再有「未登记的
        # 失败形态」被推断成成功。
        warned_this_round = False

        def _fail(node_id: str, reason: str, *, stray: Optional[str] = "") -> None:
            """本轮交付失败的唯一登记口（P1-4：置旗标 + 记事件 + 写警示槽）。"""
            nonlocal warned_this_round
            warned_this_round = True
            self._record("git_commit_fail", node=node_id, reason=reason)
            if stray:
                self.state["stray_delivery"] = {
                    "node": node_id, "ref": stray,
                    "reason": "提交滞留 %s，交付不完整" % stray}
            else:
                self.state["commit_failed"] = {"node": node_id, "reason": reason}
        cur = self._git_proc("symbolic-ref", "--short", "HEAD")
        cur_branch = cur.stdout.strip() if (cur and cur.returncode == 0) else ""
        stray_ref = ""  # 滞留位置标识：分支名或 detached 短 SHA（检测不依赖分支名非空）
        if cur_branch != cfg.branch:
            # 他分支（含 detached HEAD）上可能有未携带的提交（checkout 只带工作区）
            has_commits = self._git_proc("rev-list", "--count", f"{cfg.branch}..HEAD")
            n = has_commits.stdout.strip() if (has_commits and has_commits.returncode == 0) else ""
            if n and n != "0":
                # detached HEAD 拿不到分支名：用短 SHA 标识滞留位置
                # P2-4：rev-parse 可能返回 None（git 超时/OSError 契约）——
                # 同函数 :758 的正确写法是先判再取，此处曾裸 .stdout 崩穿 run()
                rp = self._git_proc("rev-parse", "--short", "HEAD")
                stray_ref = cur_branch or ((rp.stdout.strip()
                                            if (rp and rp.returncode == 0) else "")
                                           or "detached-HEAD")
            back = self._git_proc("checkout", cfg.branch)
            if back is not None and back.returncode == 0 and stray_ref:
                # 切回成功但提交滞留他分支（checkout 只带工作区、不带提交）：
                # 产物文件可能随分支切换被回收（仅存于滞留提交）——显式警示
                _fail(node.id, f"主体会话在 {stray_ref} 提交且滞留未交付（已切回 {cfg.branch}）；"
                               f"请 cherry-pick/merge {stray_ref} 后重跑", stray=stray_ref)
            if back is None or back.returncode != 0:
                if stray_ref:
                    _fail(node.id, f"主体会话在 {stray_ref} 提交且滞留（切回 {cfg.branch} 失败："
                                   f"{((back.stderr if back else '') or '').strip()[:150]}）；"
                                   f"请人工处理 {stray_ref} 后重跑", stray=stray_ref)
                else:
                    # 无滞留提交但切回失败（如工作区冲突）：不是散落，是交付中断
                    _fail(node.id, f"不在交付分支且切换失败（当前 {cur_branch or 'detached'}："
                                   f"{((back.stderr if back else '') or '').strip()[:150]}）")
                return
        elif cur_branch == cfg.branch:
            # 分支名相同但 tip 已变（主体在 side 分支 commit 后切回交付分支再 exit）：
            # cur==branch 使 rev-list 散落检测不运行——对比 init 时分支快照补检
            stray_ref = self._stray_from_branch_snapshot(cfg.branch)
        # P2-5 / P1-3：排除根级与任意深度嵌套 .gdd 及其下文件（见 _GIT_ADD_PATHSPEC）
        add = self._git_proc("add", "-A", "--", *_GIT_ADD_PATHSPEC)
        if add is None or add.returncode != 0:
            # add 失败（index.lock/权限）→ 静默空索引会把交付记成“无改动”丢失
            _fail(node.id, (((add.stderr if add else "") or "git add 失败（index.lock/权限？）").strip())[:200])
            return
        # 只看真正暂存的内容（status --porcelain 会把未跟踪的 .gdd/ 也列出，误判为有暂存）
        staged = self._git_proc("diff", "--cached", "--name-only")
        staged_files = ([ln for ln in staged.stdout.splitlines() if ln.strip()]
                        if (staged is not None and staged.returncode == 0) else None)
        # 声明产物展开到具体文件（glob 是一等语义，与门禁/快照同一匹配语义）：
        # 展开后逐文件核对 ignore，防 glob 声明的产物被 .gitignore 挡住时静默不交付
        # （stray_ref 已警示：产物可能随分支切换被回收，missing 时归因滞留而非误报“被删”）
        declared: List[str] = []
        missing: List[str] = []
        outside: List[str] = []
        for a in (node.exit.artifacts or []):
            if str(a.path).startswith(".gdd"):
                continue
            # 与门禁同源展开（P2-4 root escape / P2-11 越界 / glob 语义一致）
            # 段级 .. 判定（与 gates.path_escapes_root / P2-10 对齐）：
            # `..dot.md` 是根内合法文件名，startswith("..") 会误拒导致门禁过、交付永久失败
            p = str(a.path)
            if gates.path_escapes_root(p):
                outside.append(p)  # 越界声明：门禁层已判缺失，此处单独归因
                continue
            hits = [m for m in gates._artifact_matches(self.root, p)
                    if os.path.isfile(m)]
            if hits:
                declared.extend(os.path.relpath(m, self.root) for m in hits)
            else:
                missing.append(str(a.path))
        if outside:
            # P2-11：声明产物在任务根外过门禁曾被静默不交付（../outside.md
            # 转 ../ relpath 后 add -A 永不可暂存，记假「无改动」+「✅ done
            # 可上线」）。现在门禁层即判缺失；若状态因 legacy 载入走到这里，
            # 交付层再报真实原因，不静默。
            _fail(node.id, f"声明产物在任务根外，不可交付: {', '.join(outside[:5])}；"
                           f"请把产物放到任务根内后重跑")
            return
        if missing and not stray_ref:
            # exit 机器检查刚过但交付时产物已消失（后台进程/会话残留）：
            # 部分交付比不交付更隐蔽，显式失败。有 stray_ref 时 missing 属预期
            #（产物只在滞留提交里），已警示、不再重复归因
            _fail(node.id, f"声明产物在交付时已不存在：{', '.join(missing[:5])}；"
                           f"产物可能被会话残留进程删除，请检查后重跑")
            return
        if staged_files is not None and not staged_files:
            if stray_ref and not self.state.get("stray_delivery"):
                # 工作区无改动但提交滞留他分支/detached：交付丢失风险，显式失败
                #（切回时已记过警示的不再重复记事件——同一事实一条记录；
                # 但旗标仍须置位——本分支无 commit 成功，不得清上轮警示）
                _fail(node.id, f"主体会话在 {stray_ref} 提交了产出但未在交付分支，"
                               f"节点内容未交付；请 cherry-pick/merge {stray_ref} 后重跑",
                      stray=stray_ref)
            else:
                # 无暂存但声明了产物：核对产物是否被 .gitignore 挡住（add -A 跳过
                # ignored 文件 → 记“无改动”与事实不符，git clean 即永久丢失）
                ignored = self._ignored_of(declared)
                if ignored:
                    _fail(node.id, f"产物被 .gitignore 忽略未入交付分支：{', '.join(ignored[:5])}；"
                                   f"请调整 .gitignore 或移动产物路径后重跑")
                else:
                    # P1-4：纯「无改动」不是成功交付——本分支提前 return，
                    # 清除逻辑只在 commit 真实成功后执行，警示不会被误清
                    self._record("git_commit_skip", node=node.id, reason="无改动")
            return
        if staged_files:
            # 有暂存 ≠ 交付完整：主体会话新写的 .gitignore 本身会被暂存提交，
            # 而被它挡住的声明产物仍在暂存外——同样显式警示，防部分丢失
            staged_set = set(staged_files)
            ignored = self._ignored_of([p for p in declared if p not in staged_set])
            if ignored:
                _fail(node.id, f"产物被 .gitignore 忽略未入交付分支：{', '.join(ignored[:5])}；"
                               f"请调整 .gitignore 或移动产物路径后重跑")
        msg = f"gdd({node.id}): {node.name} exit 通过"
        c = self._git_proc(
            "-c", "user.name=gdd", "-c", "user.email=gdd@harness.local",
            "commit", "-m", msg)
        if c is None or c.returncode != 0:
            _fail(node.id, ((c.stderr if c else "") or "").strip()[:200] or "commit 失败")
            return
        if stray_ref:
            # 工作区还有其他改动可提交（已交付），但主体在他分支/detached 的提交仍滞留——
            # 显式警示，绝不静默丢交付；stray_delivery 独立槽：push 成败均不触碰
            self._record("git_commit_fail", node=node.id,
                         reason=f"主体会话在 {stray_ref} 提交且滞留未交付（本轮仅交付了"
                                f"残留工作区改动）；请 cherry-pick/merge {stray_ref} 后重跑，"
                                f"或确属本人工作时 gdd acknowledge 确认")
            self.state["stray_delivery"] = {
                "node": node.id, "ref": stray_ref,
                "reason": f"提交滞留 {stray_ref}，交付不完整"}
        sha = self._git_proc("rev-parse", "--short", "HEAD")
        # commit_failed 生命周期（P1-1：曾是死代码——条件反转只能清空槽，
        # n1 失败+n2 成功交付后警示永不消失）：本轮交付路径全部走通且
        # 声明产物全部入库（change_aware 门禁会拦内容不符，交付层核对
        # 到位）才清除上一轮警示；本轮新失败则保留。守卫目标是
        # 「警示仍属实」而非「槽非空」。
        if not warned_this_round:
            # 本轮 commit 真实成功且零失败分支置位旗标：上轮警示解除
            # （P1-4：曾以 before/after 槽状态推断「本轮成败」，ignored-skip
            # fall-through 无 return 前置位使推断翻车——重复犯例被误清）
            self.state.pop("commit_failed", None)
            if commit_failed_before:
                self._record("commit_failed_cleared", node=node.id,
                             reason="本轮交付成功，上轮警示解除")
        self._record("git_commit", node=node.id, branch=cfg.branch,
                     sha=(sha.stdout.strip() if (sha and sha.returncode == 0) else None))
        remote = cfg.remote
        if not remote:
            rl = self._git_proc("remote")
            remotes = rl.stdout.split() if (rl and rl.returncode == 0) else []
            remote = remotes[0] if remotes else ""
        if not remote:
            self._record("git_push_skip", node=node.id, reason="未配置 git remote，仅本地提交")
            return
        p = self._git_proc("push", "-u", remote, cfg.branch, timeout=120)
        if p is not None and p.returncode == 0:
            self.state.pop("push_failed", None)  # 仅清推送失败槽；stray 警示独立保留
            # push 成功也同步刷新分支快照（交付分支 tip 已前移，防误报滞留）
            self._refresh_branch_snapshot()
            self._record("git_push", node=node.id, remote=remote, branch=cfg.branch)
        else:
            reason = ((p.stderr if p else "") or "").strip()[:200] or "push 失败"
            self.state["push_failed"] = {"node": node.id, "remote": remote, "reason": reason}
            self._record("git_push_fail", node=node.id, remote=remote, reason=reason)

    def _refresh_branch_snapshot(self) -> None:
        """刷新分支快照——**只更新交付分支自己的 tip**（P1-5）。

        快照是 stray 重核的基线（「init 时分支态势」），刷新的唯一合法场景
        是交付分支 tip 前移（push/commit 后防误报滞留）。曾用 for-each-ref
        全量重读：把滞留分支的新 tip 一并纳入基线 → 重核据此静默清除仍
        真实滞留的 stray_delivery 并记假「已并入」。其他分支的 tip 是
        「待并入的滞留」本身，绝不是基线的一部分。
        """
        snap = self.state.get("git_branch_snapshot")
        branch = self.pipeline.git_delivery.branch
        rp = self._git_proc("rev-parse", branch)
        if rp is not None and rp.returncode == 0:
            tip = rp.stdout.strip()
            if tip:
                if snap is None:
                    snap = {}
                snap[branch] = tip
                self.state["git_branch_snapshot"] = snap

    def _stray_from_branch_snapshot(self, delivery_branch: str) -> str:
        """分支快照对比：返回未并入交付分支的滞留分支名（无则空串）。

        防绕过：主体在 side 分支 commit 后 `git checkout <delivery>` 再 exit——
        cur==branch 使 rev-list 散落检测不运行。对比 init/上次交付快照中各分支 tip：
        tip 变化且新 tip 不在交付分支历史内 → 提交滞留。

        误伤防护（P2-9）：快照是 init/上次交付成功时拍下的基线，基线之后的
        新提交**可能来自主体会话也可能来自用户本人**（用户在自己分支继续
        自己的工作）——区分靠证据而非猜测：无快照（init 基线失败路径，P2-10）
        一律不指控；有快照时只指控「相对快照新滞留」的分支。用户基线变更
        的最终裁决者是 change_aware 门禁与人工审批，不是散落检测。
        """
        snap = self.state.get("git_branch_snapshot")
        if not snap:
            # P2-10：init 基线失败提前 return 未写快照 → 此处若照常对比会把
            # 用户 init 后的正常提交全部指控为散落（检测静默变误伤机器）。
            # 无基线不指控（fail-open 限于本检测，交付完整性另由门禁/审批保障）
            return ""
        fer = self._git_proc("for-each-ref", "refs/heads/",
                             "--format=%(refname:short) %(objectname)")
        if fer is None or fer.returncode != 0:
            return ""
        cur = dict(ln.strip().split() for ln in fer.stdout.splitlines()
                   if len(ln.strip().split()) == 2)
        for br, sha in cur.items():
            if br == delivery_branch:
                continue  # 交付分支自身推进属正常（基线/前序节点交付）
            old = snap.get(br)
            if old is None or old != sha:
                # tip 前移或新出现的分支（init 后新建，不在快照内）：
                # 新提交是否已在交付分支？
                rl = self._git_proc("rev-list", "--count", f"{delivery_branch}..{sha}")
                n = rl.stdout.strip() if (rl is not None and rl.returncode == 0) else ""
                if n and n != "0":
                    return br
        return ""

    def _ignored_of(self, paths) -> list:
        """返回被 .gitignore 忽略的路径子集（产物交付前核对，防 add -A 静默跳过）。"""
        if not paths:
            return []
        out = []
        for p in paths:
            ci = self._git_proc("check-ignore", "-q", "--", p)
            if ci is not None and ci.returncode == 0:  # 0=被忽略 1=未忽略
                out.append(p)
        return out

    def _push_pending(self) -> None:
        """补推此前失败的交付分支推送（run 启动时自动重试，避免交付静默丢失）。

        stray_delivery（提交滞留警示）不走此路径：滞留提交不在交付分支，补推无意义。
        """
        pf = self.state.get("push_failed")
        if not pf:
            return
        cfg = self.pipeline.git_delivery
        if not cfg.enabled:
            return
        remote = pf.get("remote") or ""
        if not remote:
            rl = self._git_proc("remote")
            remotes = rl.stdout.split() if (rl and rl.returncode == 0) else []
            remote = remotes[0] if remotes else ""
        if not remote:
            return
        p = self._git_proc("push", "-u", remote, cfg.branch, timeout=120)
        if p is not None and p.returncode == 0:
            self.state.pop("push_failed", None)
            self._record("git_push", node=pf.get("node"), remote=remote,
                         branch=cfg.branch, note="recovered")
        else:
            self._record("git_push_retry_fail", node=pf.get("node"),
                         reason=(((p.stderr if p else "") or "").strip()[:200]) or "push 失败")
        self._save()

    # ------------------------------------------------------------ 补充验证项固化

    def _apply_supplements(self, node: NodeSpec, verdict: gates.Verdict) -> None:
        """把门禁 AI 补充的验证项固化到**运行实例**（.gdd/pipeline.json，不影响全局模板）。

        只增不减（防止门禁自我弱化）：
        - command  → 追加到 exit.commands（下次重试变为机器检查，不经 LLM）；
        - artifact → 追加到 exit.artifacts；
        - ai_check → 追加到 exit.ai_check（不可机器化的语义判据）；
        按内容去重，防止打回循环膨胀。
        """
        if not verdict.supplements:
            return
        gate = node.exit
        added = []
        for s in verdict.supplements:
            t = str(s.get("type", "")).strip().lower()
            if t == "command":
                v = s.get("value")
                if not isinstance(v, str):
                    continue  # 幻觉非字符串值（list/int）：str() 强转会得到逐字符命令风暴
                v = v.strip()
                if (v and v not in gate.commands and len(v) <= 500
                        and "\x00" not in v):  # NUL 会让 subprocess 每次 run 裸崩且固化
                    gate.commands.append(v)
                    added.append({"type": "command", "value": v})
            elif t == "artifact":
                p_raw = s.get("path")
                if not isinstance(p_raw, str):
                    continue  # path 非 str（如 int）会在机器检查期 os.path.join 裸崩
                p = p_raw.strip()
                # P1-8：NUL 防护与 command 同族（幻觉 \u0000 持久化后下进程 load
                # 报「被篡改」误指控 + 活进程 ValueError 穿透 gates except 子句）
                if p and len(p) <= 300 and "\x00" not in p:
                    # 与门禁 _artifact_matches / 交付层同族：绝对路径与段级 .. 逃逸拒收
                    # （否则可把 /tmp/secret.md 固化进实例，下游列为「可信前序产物」）
                    if gates.path_escapes_root(p):
                        continue
                    mc_raw = s.get("must_contain")
                    if mc_raw is not None and not isinstance(mc_raw, str):
                        continue  # list 值 str() 后是合法字符类正则（fail-open），必须拒
                    mc = (mc_raw or "").strip()[:300] or None
                    if mc and "\x00" in mc:
                        continue  # 同上：NUL 正则必崩（re.error 之外的 ValueError 形态）
                    if mc:
                        # 与 merge / ArtifactCheck.from_dict 共用归一化：新路径若存原始
                        # `(?i)TODO`，指纹按原形态算 → 下进程 load 归一化为 `(?i:TODO)`
                        # → to_dict≠指纹 → 合法补充被永久判定「pipeline 被篡改」
                        mc = _normalize_inline_flags(mc)
                        try:
                            re.compile(mc)
                        except re.error:
                            continue  # 幻觉非法正则不入库（否则永久卡死机器检查，仅 reset 可解）
                    existing = next((a for a in gate.artifacts if a.path == p), None)
                    if existing is None:
                        gate.artifacts.append(ArtifactCheck(path=p, must_contain=mc))
                        added.append({"type": "artifact", "path": p})
                    elif mc and mc != (existing.must_contain or ""):
                        # 同路径仅收紧必含内容（AND 语义：旧新两模式都必须出现，只增不减）。
                        # 去重用全等而非子串：`"A" in "A|B"` / `"MARK" in "MARKER"`
                        # 会静默吞掉合法收紧。
                        # 合并用内联 lookahead + [\s\S]（不用 (?s) 前缀）：全局标志不在
                        # 表达式开头在 Python>=3.11 直接 re.error，多轮再包也会触发；
                        # [\s\S] 内联形式任意嵌套都合法。入库/合并前先把前导全局 flag
                        # 规范化为局部作用域 (?i:...) 形式：3.9/3.10 前导 flag 作用于
                        # 整个合并模式会静默弱化原判据；3.11+ 直接 re.error 被静默丢弃
                        old = _normalize_inline_flags(existing.must_contain or "")
                        mc_norm = _normalize_inline_flags(mc)
                        if old.startswith("(?s)"):
                            old = old[4:]  # 旧版本写入的前缀形式（规范化后不应出现，双保险）
                        # P2-3：数字回引（反斜杠+数字）的模式并入外层合并结构后组号重编号
                        # ——「回引本模式第 1 组」变质为「回引合并模式的第 1 组」，
                        # 语义静默变质且 compile 守卫抓不到（重编号后照样合法；
                        # 仅当两侧都带捕获组时炸开）。命名组包裹隔离同样不可行：
                        # 包裹组自身成为第 1 组后，内部回引指向包裹组触发
                        # re.error "cannot refer to an open group"——整条收紧被
                        # compile 守卫静默丢弃。old 非空且任一侧含数字回引即
                        # 放弃合并、记事件留痕（旧判据照常生效，新判据不收紧
                        # ——宁可少一条收紧，不造语义变质）。old 为空（首个
                        # 判据）时 mc_norm 原样入库、无重编号，不受此限。
                        if old and re.search(r"\\[1-9]", old + mc_norm):
                            self._record("gate_supplement_merge_rejected",
                                         node=node.id,
                                         reason="模式含数字回引，并入后组号重编号语义变质，"
                                                "仅保留单判据", path=p)
                            continue
                        # P2-2（结构性根治）：合取合并用锚定双 lookahead
                        # `^(?=[\s\S]*A)(?=[\s\S]*B)` 而非裸
                        # `(?=[\s\S]*A)(?=[\s\S]*B)`——裸形态在任一 conjunct
                        # 缺失的输入上由 re.search 从每个起点重扫（O(n²)，实测
                        # 17KB≈0.6s、47KB≈2.5s，产物稍大即打穿 REGEX_TIMEOUT，
                        # bounce 反馈还会误诊「请修正 must_contain」——主体无法
                        # 修的 harness 字段：harness 自己的 merge 造出了自家
                        # 超时防御防的形状）。前置 ^ 锚定后（无 re.M，^ 只匹配
                        # 串首），search 仅在位置 0 评估一次两个 lookahead，
                        # miss 变 O(n)；语义不变：两判据仍须各自出现在全文
                        # 某处（子模式内部 ^/$ 锚语义在 inline 位置不变）。
                        # P1-7（R6）：顶层 | 分组——模式 `需求|方案` 并入
                        # lookahead 后 `(?=[\s\S]*需求|方案)` 顶层分裂为
                        # 「(?=[\s\S]*需求) | (方案)」（合取降级成 alternation
                        # 外泄），语义被篡改变紧/变松且持久化复验。每个
                        # conjunct 先包非捕获组 (?:...) 再拼接。
                        if old:
                            old = f"(?:{old})"
                            mc_norm = f"(?:{mc_norm})"
                        merged = (f"^(?=[\\s\\S]*{old})(?=[\\s\\S]*{mc_norm})"
                                  if old else mc_norm)
                        try:
                            re.compile(merged)
                        except re.error:
                            continue
                        # 兜底自测（DoD：合并产物须过自家 ReDoS 防御的回归
                        # 守卫）——17KB 全 miss 探针：锚定形态应快速返回 False。
                        # 注意合取前瞻按序短路（前 conjunct 失败即整体失败，
                        # 后 conjunct 不被求值），探针**无法**逐 conjunct 触发
                        # 用户模式自带的危险回溯（如嵌套量词）——那属于
                        # check-time REGEX_TIMEOUT 防御的管辖域（用户初始
                        # must_contain 同样可能带，非 merge 引入）。探针抓的
                        # 是「合取结构本身」造成的回溯形状：锚定改写后应为
                        # O(n) miss，超时返回 None 即拒绝合并（新判据不入库、
                        # 旧判据照常生效，宁可少一条收紧不造 DoS 形状）。
                        from .gates import _timed_regex_search
                        if _timed_regex_search(merged, "x" * 17000) is None:
                            self._record("gate_supplement_merge_rejected", node=node.id,
                                         reason="合并模式回溯复杂度超标（17KB 探针超时），"
                                                "仅保留单判据", path=p)
                            continue
                        existing.must_contain = merged
                        added.append({"type": "artifact", "path": p,
                                      "must_contain": mc})
            elif t == "ai_check":
                v = s.get("value")
                if not isinstance(v, str):
                    continue
                v = v.strip()
                if v and v not in (gate.ai_check or "") and len(v) <= 1000:
                    gate.ai_check = (gate.ai_check + "\n" + v).strip() if gate.ai_check else v
                    added.append({"type": "ai_check", "value": v[:200]})
        if added:
            self._save_pipeline()
            self._record("gate_supplement", node=node.id, items=added)
            self._save()  # 持久化新指纹

    def _current_node(self) -> NodeSpec:
        node = self.pipeline.node(self.state["current"])
        if node is None:
            raise RuntimeError(f"state.current 指向不存在的节点: {self.state['current']}")
        return node

    # ------------------------------------------------------------ 并发锁

    def _acquire_lock(self) -> bool:
        """独占锁（.gdd/lock，flock）：防止两个 run 进程互相覆盖状态。

        拿锁成功后调用方应 reload 状态（load→acquire 之间存在 TOCTOU 窗口）。
        """
        if self._lock_fd is not None:
            return True
        os.makedirs(self.gdd_dir, exist_ok=True)
        fd = open(os.path.join(self.gdd_dir, "lock"), "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            fd.close()
            return False
        self._lock_fd = fd
        return True

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except OSError:
                pass
            self._lock_fd = None

    # ------------------------------------------------------------ 主循环

    def run(self) -> str:
        """推进直到需要人工介入或结束，返回最终 status。"""
        if not self._acquire_lock():
            raise LockBusyError("另一个 gdd 进程正在运行（.gdd/lock 被占用）")
        try:
            self.load()  # 拿锁后重读：load→acquire 之间状态可能已被其他进程推进
            if not self.state:
                raise RuntimeError(
                    "state.json/pipeline.json 缺失（可能被并发 reset 删除）："
                    "请 gdd init 重开或检查 .gdd/ 目录")
            if not self.verify_pipeline_integrity():
                if self._migrate_legacy_fingerprint():
                    self._record("pipeline_fingerprint_migrated")
                    self._save()
                else:
                    # 终态（completed/aborted）不得翻成 blocked：否则 restore+retry
                    # 会整管线重跑并在「✅ done」后再交付第二个不同 commit
                    if self.state.get("status") in TERMINAL:
                        self._record("pipeline_integrity_fail",
                                     note="终态仅告警，不改 status")
                        self._save()
                        return self.state["status"]
                    self.state["status"] = "blocked"
                    self.state["blocked_reason"] = (
                        "运行实例 .gdd/pipeline.json 与指纹不匹配（可能被会话篡改）："
                        "请人工核验后 gdd reset 重开，或手工恢复实例内容")
                    self._record("pipeline_integrity_fail")
                    self._save()
                    return "blocked"
            # input.md 重启窗口阻断（与指纹同语义）：load 时摘要不符/不可读
            # 的实例，_input_text 为 None——此前仅记事件不阻断，篡改输入带毒
            # 跑完；crash+删除则需求段静默消失。run() 入口直接 blocked（P0-2）
            if self._input_text is None:
                self.state["status"] = "blocked"
                self.state["blocked_reason"] = (
                    ".gdd/input.md 与运行实例记录的需求指纹不符（篡改后不可恢复或文件缺失）："
                    "请人工核验后恢复需求原文（或 gdd reset 重开）")
                self._record("input_integrity_fail")
                self._save()
                return "blocked"
            # stray 警示重核（P1-2：曾无任何清除路径）：用户照警示 cherry-pick/
            # merge 滞留分支后重跑——入口对任意 status 生效（completed 实例的
            # 警示也随重跑解除；此前 run() 对终态直接返回、警示永久悬挂）。
            # P2-1（R6）：无快照基线时**只读不动槽**——曾据此把 rev-list 路径
            # 检出的属实 stray pop 掉并记假「已并入」，与本注释段自述
            # 「无快照基线时不指控也不动槽」直接矛盾（init 基线失败不写
            # 快照的情形，警示的检出方是 rev-list，重核无权推翻它）。
            # 快照在场且不再见滞留 diff → 真并入，清槽；快照缺失 → 保留
            # 警示等人工裁决（acknowledge）或快照恢复。确属本人工作用
            # gdd acknowledge。
            if (self.state.get("stray_delivery")
                    and self.pipeline.git_delivery.enabled
                    and not self.state.get("git_delivery_disabled")):
                if self.state.get("git_branch_snapshot"):
                    if not self._stray_from_branch_snapshot(self.pipeline.git_delivery.branch):
                        # 快照扫描为空仍须核槽内具体 ref：detached/已删分支对
                        # for-each-ref 不可见，曾误 pop 并记假「已并入」
                        slot = self.state.get("stray_delivery") or {}
                        ref = (slot.get("ref") or "").strip()
                        still = False
                        if ref:
                            br = self.pipeline.git_delivery.branch
                            rl = self._git_proc("rev-list", "--count", f"{br}..{ref}")
                            n = (rl.stdout.strip()
                                 if (rl is not None and rl.returncode == 0) else "")
                            # rev-list 失败（ref 已删）→ 保守保留警示，交 acknowledge
                            still = (rl is None or rl.returncode != 0
                                     or (n and n != "0"))
                        if not still:
                            self.state.pop("stray_delivery", None)
                            self._record("stray_delivery_cleared",
                                         reason="滞留提交已并入交付分支（重核解除）")
                            self._save()
            self._push_pending()  # 补推此前失败的交付分支推送
            while True:
                status = self.state["status"]
                if status in TERMINAL or status in ("awaiting_approval", "blocked"):
                    break
                if not self.step():
                    break
            # P1-1 缓解链（收尾最终 tamper 扫描）：最后一次 _save 之后仍有
            # 两个无防护窗口——末节点周知（print）与
            # 返回路径上的延迟写（会话残留孙进程 nohup）。此处对
            # state/pipeline/input 三文件做终检：发现偏差即恢复并记事件，
            # 不让「本进程眼里的完成」与「磁盘上的状态」静默分叉。
            # 注意：真正发生在 run() 返回之后的写无法在本进程内观测——
            # 该边界的完整防护是 killpg（已加）+ README 声明的信任模型。
            self._check_tamper("run_final")
            return self.state["status"]
        finally:
            self._release_lock()

    def step(self) -> bool:
        """执行一个原子动作；返回是否有进展。"""
        st = self.state
        if st["status"] in TERMINAL:
            return False
        node = self._current_node()
        ns = st["node_states"][node.id]
        runner = AgentRunner(self.root)

        if ns["phase"] == "pending":
            return self._phase_entry(node, ns, runner)
        if ns["phase"] == "working":
            return self._phase_working(node, ns, runner)
        if ns["phase"] == "exit_retry":  # 门禁自身故障后仅重跑 exit 门禁（不重跑主体）
            return self._phase_exit_retry(node, ns, runner)
        if ns["phase"] == "exit_passed":
            return self._phase_exit_passed(node, ns)
        if ns["phase"] == "done":
            return self._advance(node)
        return False

    # ------------------------------------------------------------ 相位处理

    def _phase_entry(self, node: NodeSpec, ns: Dict, runner: AgentRunner) -> bool:
        verdict = gates.run_gate("entry", node, self.root, runner, log_path=self._log_path(node, "entry"))
        self._check_tamper("entry_gate")  # 会话/机器命令可能篡改 .gdd，校验并恢复
        if verdict.passed:
            self._clear_gate_error_streak()
            ns["phase"] = "working"
            # 下游 entry 通过 = 上游 entry 打回事件闭合：归档并清零上游 entry_bounces
            prev = self.pipeline.prev(node.id)
            if prev is not None:
                pns = self.state["node_states"][prev.id]
                if pns.get("entry_bounces"):
                    pns["entry_bounces_total"] = pns.get("entry_bounces_total", 0) + pns["entry_bounces"]
                    pns["entry_bounces"] = 0
            self._record("entry_pass", node=node.id, reason=verdict.reason)
            if node.baseline_marker:
                # change_aware 基线按节点记（首个 change_aware 节点也要）：
                # 全局单槽只记第一次基线，第二个 change_aware 节点的 diff 会含
                # 前序节点已提交内容（代码自己注释警告过的多计失败模式）
                head = self._git("rev-parse", "HEAD")
                if head:
                    key = f"baseline_commit:{node.id}"
                    if not self.state.get(key):
                        self.state[key] = head
                        self._record("baseline", node=node.id, commit=head[:10])
            self._save()
            return True
        if verdict.gate_error:
            return self._handle_gate_error(node, verdict)  # phase 保持 pending，重跑 entry
        self._clear_gate_error_streak()
        self._record("entry_fail", node=node.id, reason=verdict.reason[:300])
        return self._bounce_to_previous(node, verdict)

    def _phase_working(self, node: NodeSpec, ns: Dict, runner: AgentRunner) -> bool:
        feedback = self.state["feedbacks"].get(node.id, "")
        fb_kind = self.state.get("feedback_kinds", {}).get(node.id, "")
        ns["attempts"] = ns.get("attempts", 0) + 1  # 极老 state 兼容
        self._save()

        upstream = gates.collect_upstream_artifacts(self.pipeline, node, self.root)
        # 本节点澄清结论固化产物：仅 clarify 节点注入 CLARIFY_<node>.md（澄清会话专用固化文档）。
        # 注意：不注入本节点 exit 声明产物——它们尚未通过本节点 exit 门禁，
        # 标为"已验证上游产物"是假标签（打回重跑时会把上一轮未过门禁的残稿当事实）
        if node.clarify:
            _cl = f"CLARIFY_{node.id}.md"
            if os.path.isfile(os.path.join(self.root, _cl)) and _cl not in upstream:
                upstream.append(_cl)
        raw_req = self.input_path if os.path.exists(self.input_path) else ""

        def _subject_stream_cb():
            # 每次 run 新建打印机：resume 失败后再 fresh 时不得继承 text_n/omitted
            if not self.on_stream:
                return None
            try:
                return self.on_stream("subject")
            except Exception:
                return None

        # ---- 主体会话复用（详见 docs/design-clarify-resume.md §3、design-session-group.md）----
        sessions = ns.setdefault("sessions", {})
        group = self._node_group(node)
        resume_sid = ""
        if (os.environ.get("GDD_SUBJECT_RESUME", "on") != "off"
                and (fb_kind == "clarify"
                     or ns.get("bounces", 0) < 2
                     or ns.get("auto_continues", 0) > 0)):
            # bounces<2 是防门禁打回循环的预算；turns 打满自动续跑与打回正交，
            # 打回打满后仍应续会话而非每轮全新重跑（否则 sid 成死档、烧多倍配额）；
            # clarify 反馈例外：review 澄清对话就发生在主体会话上，收尾轮必须
            # resume 该会话（§4 主体 resume 收尾），否则 review 结论对收尾轮不可见
            resume_sid = self._subject_group_sid(group)
        if resume_sid:
            # 续会话：历史上下文已在会话内，只发反馈/继续指令；门禁不受影响（每次全新）
            own_sid = sessions.get("subject")  # 进入本相位时本节点自己的存档（快照于 run 前）
            self._record("subject_resume", node=node.id)  # 审计：history 可查，不必翻日志文件
            if feedback:
                ev = self.state.get("last_exit_evidence") or []
                ev_part = ("\n门禁核验证据（修改时对照）：\n- "
                           + "\n- ".join(str(e)[:300] for e in ev[:8])) if ev else ""
                prompt = (f"[[GDD-ROLE:subject node:{node.id}]]\n"
                          f"上一轮产出被门禁/审批打回或经澄清对齐。上轮反馈（打回项必须逐条处理；「澄清对齐纪要」按用户意图权衡，文档为唯一事实来源）：\n{feedback}{ev_part}")
            elif resume_sid != own_sid:
                # 跨节点组复用：继承组内上游节点的会话（历史上下文在会话内），
                # 但本节点的目标/产物/上游物料必须由完整任务书给出——
                # 不能发"继续中断工作"的空指令（会话里只有上游节点的任务书）
                prompt = gates.build_subject_prompt(node, self.pipeline, upstream, "", raw_req)
            else:
                prompt = (f"[[GDD-ROLE:subject node:{node.id}]]\n"
                          f"上轮会话中断，请继续完成本节点工作（产物与报告路径不变）。")
            stream_cb = _subject_stream_cb()
            skw = {"on_stream": stream_cb} if stream_cb is not None else {}
            session = runner.run(
                prompt, allowed_tools=gates.SUBJECT_TOOLS,
                max_turns=node.subject.max_turns,
                log_path=self._log_path(node, "subject_resume"),
                resume_sid=resume_sid,
                **skw,
            )
            if not session.ok and not session.turns_exhausted:
                # resume 失效（会话过期等，非打满）：清存档回退全新会话重试一次，不计故障。
                # turns 打满时会话仍存活：不 pop——保留 sid，由下方自动续跑块接管
                # （否则打满被误判 resume 失效，弹档后发全新会话重跑，白烧配额）。
                # 组复用：同组所有节点的主体 sid 都指向同一会话，逐一失效。
                self._record("session_resume_fail", node=node.id, error=session.error[:200])
                self._pop_subject_group_sid(group)
                resume_sid = ""
        if not resume_sid:
            prompt = gates.build_subject_prompt(node, self.pipeline, upstream, feedback, raw_req)
            stream_cb = _subject_stream_cb()  # 与 resume 轮隔离：截断额度重新起算
            skw = {"on_stream": stream_cb} if stream_cb is not None else {}
            session = runner.run(
                prompt, allowed_tools=gates.SUBJECT_TOOLS,
                max_turns=node.subject.max_turns,
                log_path=self._log_path(node, "subject"),
                **skw,
            )
        if session.session_id and session.ok:
            # 仅成功会话才存档 sid（同 chat 路径防线）：失败会话的 sid 可能已死，
            # 存档会让组查找/下轮 resume 命中死会话，浪费一次注定失败的 resume
            sessions["subject"] = session.session_id
        self._check_tamper("subject")  # 主体会话可能篡改 .gdd，校验并恢复
        self._record("subject_done", node=node.id, ok=session.ok,
                     turns=session.turns, cost_usd=round(session.cost_usd, 4))
        if session.ok:
            # 主体成功即打断故障连击（P2-6）：非连续的两次故障不得计为「连续故障 2
            # 次」blocked——[主体故障→主体成功→exit 门禁故障] 中间的成功证明会话
            # 可用，只有门禁自己的连续故障才该归入故障预算。同时清零 turns 自动续跑
            # 计数：问题已解决，「打回≥2 改全新会话」防污染门不应被一次历史打满
            # 永久解除（README 承诺、测试 C2 钉住）
            self._clear_gate_error_streak()
            ns["auto_continues"] = 0
        if (not session.ok and session.turns_exhausted and session.session_id
                and os.environ.get("GDD_SUBJECT_RESUME", "on") != "off"
                and ns.get("auto_continues", 0) < SUBJECT_AUTO_CONTINUES_MAX):
            # turns 打满：会话仍存活、上下文在会话内，--resume 续跑即可接着干（产物
            # 逐步落盘，进度零丢失）。自动续跑不消耗打回/故障预算；存档 sid 供
            # 下轮 step 走 subject_resume 分支发「继续完成本节点工作」指令。
            # GDD_SUBJECT_RESUME=off 逃生舱同攸（第三处）：off 时 --resume 坏掉，
            # 烧 MAX+2 次注定无用的全新满编会话后 blocked，提示 retry 但该环境
            # retry 同样 fresh——直接转人工裁决，不进循环
            sessions["subject"] = session.session_id
            ns["auto_continues"] = ns.get("auto_continues", 0) + 1
            self._record("subject_auto_continue", node=node.id,
                         round=ns["auto_continues"], turns=session.turns)
            self._save()
            return True
        if not session.ok:
            # 主体会话自身故障（CLI 崩溃等，非门禁判定）：与门禁故障同一故障域，
            # 不消耗打回预算（连续 2 次 → blocked）
            if session.turns_exhausted and session.session_id:
                # 续跑超上限转人工：会话与已落盘产物仍在，文案必须与下轮实际
                # 行为一致——retry 后走 resume「继续完成」（而非全新重跑）。
                # P2-13：GDD_SUBJECT_RESUME=off 时 retry 全 fresh、续跑反馈被
                # 丢弃（feedbacks 保持 {}），「续跑（进度不丢）」是假承诺——
                # 按环境分叉：off 时如实说全新重跑，只有产物文件留存
                # P2-14：resume 每轮 fork 出新 sid——触顶轮若不存档，state 仍
                # 指向上上轮旧档，retry resume 旧 sid 使最近满编轮的对话上下文
                # 静默孤儿化（「retry 续跑（进度不丢）」在会话记忆维度不成立）。
                # off 时 retry 不走 resume，不存档（存了也是死档）
                if (os.environ.get("GDD_SUBJECT_RESUME", "on") != "off"
                        and session.session_id):
                    sessions["subject"] = session.session_id
                if os.environ.get("GDD_SUBJECT_RESUME", "on") == "off":
                    reason = (f"主体会话 turns 连续打满（已自动续跑 {ns.get('auto_continues', 0)} 轮"
                              f"仍不足）。当前 GDD_SUBJECT_RESUME=off：gdd retry 将全新重跑"
                              f"（仅产物文件留存，会话上下文与续跑反馈不保留）；"
                              f"可拆小节点任务或调大 GDD_SUBJECT_AUTO_CONTINUES_MAX")
                else:
                    reason = (f"主体会话 turns 连续打满（已自动续跑 {ns.get('auto_continues', 0)} 轮"
                              f"仍不足），需人工裁决：继续 gdd retry 续跑（进度不丢）、"
                              f"拆小节点任务或调大 GDD_SUBJECT_AUTO_CONTINUES_MAX")
                return self._handle_gate_error(node, gates.Verdict(
                    passed=False, reason=reason,
                    feedback="会话 turns 连续打满。请基于已落盘进度继续完成本节点剩余工作（不要重做已完成部分）。"))
            return self._handle_gate_error(node, gates.Verdict(
                passed=False, reason=f"主体会话执行失败: {session.error}",
                feedback=f"上一轮会话异常中断（{session.error[:200]}），请重新完成本节点工作。"))
        # 上轮打回项：随 exit 门禁逐项核验；**核验通过才消费**（pop 后移）——
        # 门禁自身故障（exit_retry）或进程崩溃时反馈保留在 state，重试/重启后不丢失核验项
        pending_fb = self.state["feedbacks"].get(node.id, "")
        pending_kind = self.state.get("feedback_kinds", {}).get(node.id, "")

        change_summary = self._change_summary(node.id) if node.exit.change_aware else ""
        # 上轮打回项：交由 exit 门禁逐项核验（首轮跑 exit 时无打回，天然为空）
        verdict = gates.run_gate("exit", node, self.root, runner,
                                 change_summary=change_summary,
                                 log_path=self._log_path(node, "exit"),
                                 pending_feedback=pending_fb,
                                 pending_kind=pending_kind)
        self._check_tamper("exit_gate")
        self._apply_supplements(node, verdict)  # AI 补充验证项固化到运行实例（只增不减）
        if verdict.passed:
            self._clear_gate_error_streak()
            ns["phase"] = "exit_passed"
            self.state["last_exit_evidence"] = verdict.evidence
            self.state["feedbacks"].pop(node.id, None)  # 打回项已核验通过，此刻才消费
            self.state.get("feedback_kinds", {}).pop(node.id, None)
            self._record("exit_pass", node=node.id, reason=verdict.reason)
            self._deliver_git(node)  # exit 通过即提交推送到开发分支
            self._save()
            return True
        if verdict.gate_error:
            ns["phase"] = "exit_retry"  # 仅重跑 exit 门禁，不重跑主体、不消耗 bounces
            return self._handle_gate_error(node, verdict)
        self._clear_gate_error_streak()
        self._record("exit_fail", node=node.id, reason=verdict.reason[:300])
        # 失败证据同样落盘：主体 resume 时附的「门禁核验证据」是本次失败的依据，
        # 而非上一轮通过（甚至上一节点）的陈旧证据
        self.state["last_exit_evidence"] = verdict.evidence
        return self._bounce_current(node, verdict)

    def _phase_exit_retry(self, node: NodeSpec, ns: Dict, runner: AgentRunner) -> bool:
        """门禁自身故障后的重试：直接重跑 exit 门禁（主体产物保持不变）。"""
        change_summary = self._change_summary(node.id) if node.exit.change_aware else ""
        pending_fb = self.state["feedbacks"].get(node.id, "")  # 故障重试同样要核验上轮打回项
        pending_kind = self.state.get("feedback_kinds", {}).get(node.id, "")
        verdict = gates.run_gate("exit", node, self.root, runner,
                                 change_summary=change_summary,
                                 log_path=self._log_path(node, "exit"),
                                 pending_feedback=pending_fb,
                                 pending_kind=pending_kind)
        self._check_tamper("exit_gate_retry")
        self._apply_supplements(node, verdict)
        if verdict.passed:
            self._clear_gate_error_streak()
            ns["phase"] = "exit_passed"
            self.state["last_exit_evidence"] = verdict.evidence
            self.state["feedbacks"].pop(node.id, None)  # 核验通过才消费
            self.state.get("feedback_kinds", {}).pop(node.id, None)
            self._record("exit_pass", node=node.id, reason=verdict.reason, note="after_gate_error_retry")
            self._deliver_git(node)  # 门禁故障重试后通过同样交付
            self._save()
            return True
        if verdict.gate_error:
            return self._handle_gate_error(node, verdict)  # 继续停留在 exit_retry
        self._clear_gate_error_streak()
        self._record("exit_fail", node=node.id, reason=verdict.reason[:300])
        # 失败证据同样落盘：主体 resume 时附的「门禁核验证据」是本次失败的依据，
        # 而非上一轮通过（甚至上一节点）的陈旧证据
        self.state["last_exit_evidence"] = verdict.evidence
        return self._bounce_current(node, verdict)

    def _handle_gate_error(self, node: NodeSpec, verdict: gates.Verdict) -> bool:
        """Agent 会话自身故障（门禁或主体崩溃，非节点问题）：有限重试，连续 2 次转 blocked。"""
        st = self.state
        st["gate_error_streak"] = st.get("gate_error_streak", 0) + 1
        if st["gate_error_streak"] >= 2:
            st["status"] = "blocked"
            st["blocked_reason"] = (
                f"Agent 会话连续故障 {st['gate_error_streak']} 次（门禁/主体会话，非节点问题）：{verdict.reason[:300]}。"
                f"请检查 Agent CLI 可用性后 gdd retry。")
            st["blocked_owner"] = node.id
            self._record("blocked", node=node.id, reason="gate_error_streak")
            self._save()
            return False
        self._record("gate_error_retry", node=node.id, reason=verdict.reason[:200])
        self._save()
        return True  # 重跑当前门禁

    def _clear_gate_error_streak(self) -> None:
        if self.state.get("gate_error_streak"):
            self.state["gate_error_streak"] = 0

    def _artifact_files(self, node: NodeSpec) -> List[str]:
        """展开节点 exit 声明产物为具体文件列表（支持 glob，与门禁同一匹配语义）。

        与 gates._artifact_matches 同源（root 段 glob.escape 防任务根含
        glob 元字符、越界声明判空、目录命中不算文件）——四处展开点
        （门禁存在性/快照/审批比对/交付核对）共用同一助手，语义分裂
        （曾：门禁算目录、交付 isfile、快照各自为政）即收敛。
        快照仍只记普通文件：目录型 artifact 属异常配置，不为其做指纹。
        """
        from .gates import _artifact_matches
        out: List[str] = []
        for a in node.exit.artifacts:
            for m in _artifact_matches(self.root, str(a.path)):
                if os.path.isfile(m):
                    out.append(os.path.relpath(m, self.root))
        return out

    def _artifact_snapshot(self, node: NodeSpec) -> Dict[str, str]:
        """记录节点 exit 声明产物的当前指纹（sha256-16，glob 展开到具体文件）。"""
        snap: Dict[str, str] = {}
        for rel in self._artifact_files(node):
            fp = os.path.join(self.root, rel)
            try:
                with open(fp, "rb") as f:
                    snap[rel] = hashlib.sha256(f.read()).hexdigest()[:16]
            except OSError:
                pass
        return snap

    def _artifact_changes(self, node: NodeSpec, snap: Dict[str, str]) -> List[str]:
        """比对快照与当前产物：指纹变化/新增匹配文件/文件消失均计为变更。"""
        changed: List[str] = []
        current = set(self._artifact_files(node))
        for rel in sorted(current | set(snap)):
            fp = os.path.join(self.root, rel)
            got = None
            if os.path.isfile(fp):
                try:
                    with open(fp, "rb") as f:
                        got = hashlib.sha256(f.read()).hexdigest()[:16]
                except OSError:
                    got = None
            if got != snap.get(rel):
                changed.append(rel)
        return changed

    def _phase_exit_passed(self, node: NodeSpec, ns: Dict) -> bool:
        if not node.approval.required:
            return self._complete(node)
        # 人工准入
        if approval_mod.is_interactive() and not self.defer_approval:
            # 快照先持久化再交互（与异步路径一致）：交互窗口内进程被杀（kill/断终端），
            # 磁盘上已有 pending_approval+快照，重启后比对基准不丢，窗口内改动无法
            # 经「重启重拍快照」洗白
            snap = self._artifact_snapshot(node) if node.exit.artifacts else {}
            self.state["pending_approval"] = {
                "node": node.id,
                "summary": approval_mod.render_approval_summary(
                    node, self.state.get("last_exit_evidence", []), ns["bounces"]),
                "artifact_snapshot": snap,
            }
            self.state["status"] = "awaiting_approval"
            self._save()
            decision = approval_mod.interactive_approval(node, self.state.get("last_exit_evidence", []), ns["bounces"])
            if decision is True:
                changed = self._artifact_changes(node, snap) if snap else []
                if changed:
                    # 审批等待期间产物被改：不消费审批，转挂起等重新裁决（同异步路径）
                    self.last_refusal = ("审批期间产物已变更（" + ", ".join(changed[:5])
                                          + "）。请驳回打回或重过 exit 门禁。")
                    self._record("approval_refused_artifact_changed", node=node.id, files=changed[:5])
                    self._save()
                    return False
                # 裁决前状态仍为 awaiting（交互前已落盘）；批准后转回推进
                self.state["status"] = "running"
                self.state["pending_approval"] = None  # 幽灵审批卡：裁决后必清（对齐异步路径）
                self._record("approval_pass", node=node.id)
                return self._complete(node)
            if decision is False:
                fb = approval_mod.interactive_feedback()
                self.state["status"] = "running"
                self.state["pending_approval"] = None
                self._record("approval_reject", node=node.id, feedback=fb[:300])
                return self._bounce_current(node, gates.Verdict(passed=False, reason="人工审批驳回", feedback=fb))
            if decision == "keep":
                # 输入中断（EOF/Ctrl-C）：已落盘的挂起态保持，交给 gdd approve/go 后续裁决
                self._record("awaiting_approval", node=node.id)
                self._save()
                return True
            self.state["status"] = "aborted"
            self.state["pending_approval"] = None  # abort 同样清（completed/aborted 态不留幽灵卡）
            self._record("aborted", node=node.id)
            self._save()
            return True
        # 非交互：挂起等待 gdd approve
        # 审批快照：记录 exit 产物指纹。approve 时核验——审批期间产物被改
        # （典型：审批挂起时的澄清对话 resume 主体会话写入）则拒绝直接放行，
        # 必须重过 exit 门禁，堵住「对话改产物→直接批准」旁路（design §5 硬规则3 机制化）
        self.state["pending_approval"] = {
            "node": node.id,
            "summary": approval_mod.render_approval_summary(
                node, self.state.get("last_exit_evidence", []), ns["bounces"]),
            "artifact_snapshot": self._artifact_snapshot(node),
        }
        self.state["status"] = "awaiting_approval"
        self._record("awaiting_approval", node=node.id)
        self._save()
        return False

    # ------------------------------------------------------------ 流转与打回

    def _complete(self, node: NodeSpec) -> bool:
        ns = self.state["node_states"][node.id]
        ns["phase"] = "done"
        # 完成即清零打回计数（历史累计归档到 bounces_total，避免跨事件累计误 blocked）
        if ns["bounces"]:
            ns["bounces_total"] = ns.get("bounces_total", 0) + ns["bounces"]
            ns["bounces"] = 0
        self._record("node_done", node=node.id)
        self._save()
        if self.pipeline.next(node.id) is None:
            # P2-7：先推进到 completed 并落盘，再做周知——_final_notify
            # 裸 print 持锁（UI 后台化 stdout 关闭/`gdd run | head` 断管道）
            # 抛 ValueError，曾使 node_done 已存、pipeline_completed 未记，
            # UI _bg_run 异常处理器把已完成的流水线翻成 blocked。
            # 周知失败（含 print 异常）只记事件，不影响状态机。
            try:
                self._advance(node)  # 末节点：置 completed + pipeline_completed 落盘
            finally:
                self._final_notify(node)
            return False  # 末节点完成即终止本轮推进
        return self._advance(node)

    def _final_notify(self, node: NodeSpec) -> None:
        """流水线末节点完成后打印周知（仅 stdout，不接 IM）。

        P2-7：print 不得让异常逃逸——stdout 断开（UI 后台化、`gdd run | head`）
        时 print 抛 ValueError，曾使 node_done 已存、pipeline_completed 未记，
        UI _bg_run 异常处理器把已完成的流水线翻成 blocked。周知失败只记事件。
        """
        try:
            self._final_notify_inner(node)
        except Exception as e:  # noqa: BLE001 周知属呈现层：失败不碰状态机
            try:
                self._record("notify_fail", node=node.id, reason=str(e)[:200])
                self._save()  # 事件须落盘（曾只改内存，重启后失败痕迹消失）
            except Exception:
                pass
        else:
            self._save()  # notify_print 事件落盘（P2-19：曾只在内存）

    def _final_notify_inner(self, node: NodeSpec) -> None:
        ev = [f"[{e['node']}] {e['reason']}" for e in self.state["history"]
              if e["event"] == "exit_pass"]
        summary = "\n".join(ev) or "（无）"
        print("\n" + "=" * 56)
        print("✅ done，可以上线")
        print(f"流水线「{self.pipeline.name}」全部节点完成（末节点 {node.id}）")
        print("各节点 exit 依据：")
        for line in summary.splitlines():
            print(f"  {line}")
        print("=" * 56)
        self._record("notify_print", node=node.id)

    def _advance(self, node: NodeSpec) -> bool:
        nxt = self.pipeline.next(node.id)
        if nxt is None:
            self.state["status"] = "completed"
            self._record("pipeline_completed")
            self._save()
            return False
        self.state["current"] = nxt.id
        self.state["node_states"][nxt.id]["phase"] = "pending"
        self._save()
        return True

    def _bounce_current(self, node: NodeSpec, verdict: gates.Verdict) -> bool:
        ns = self.state["node_states"][node.id]
        ns["bounces"] += 1
        if ns["bounces"] >= node.max_bounces:
            self.state["status"] = "blocked"
            self.state["blocked_reason"] = (
                f"节点 {node.id} 打回 {ns['bounces']} 次达到上限 {node.max_bounces}：{verdict.reason[:300]}。"
                f"最近反馈：{verdict.feedback_text[:300]}")
            # 最新失败反馈仍写入：blocked 后 retry 清零计数重跑时，主体拿到的是
            # 最新的失败原因（retry 只清计数不清反馈）
            self.state["feedbacks"][node.id] = verdict.feedback_text
            self.state.setdefault("feedback_kinds", {})[node.id] = "bounce"
            self.state["blocked_owner"] = node.id
            self._record("blocked", node=node.id, bounces=ns["bounces"])
            self._save()
            return False
        ns["phase"] = "working"
        self.state["feedbacks"][node.id] = verdict.feedback_text
        self.state.setdefault("feedback_kinds", {})[node.id] = "bounce"
        self._record("bounce_current", node=node.id, bounces=ns["bounces"], feedback=verdict.feedback_text[:300])
        self._save()
        return True

    def _bounce_to_previous(self, node: NodeSpec, verdict: gates.Verdict) -> bool:
        prev = self.pipeline.prev(node.id)
        if prev is None:
            self.state["status"] = "blocked"
            self.state["blocked_reason"] = f"首节点 {node.id} 的 entry 前置条件不满足：{verdict.reason[:300]}"
            self.state["blocked_owner"] = node.id
            self._record("blocked", node=node.id, reason="first_node_entry_fail")
            self._save()
            return False
        pns = self.state["node_states"][prev.id]
        pns["entry_bounces"] = pns.get("entry_bounces", 0) + 1
        if pns["entry_bounces"] >= prev.max_bounces:
            self.state["status"] = "blocked"
            self.state["blocked_reason"] = (
                f"节点 {node.id} 的 entry 打回上游 {prev.id} 已达上限 {prev.max_bounces}：{verdict.reason[:300]}"
                f"（修复后 gdd retry，将重跑 {prev.id}）")
            # 同样保留最新修改指引：retry 重跑上游时主体带最新反馈
            self.state["feedbacks"][prev.id] = f"[下游 {node.id} 的 entry 门禁打回] {verdict.reason}\n修改指引：{verdict.feedback_text}"
            self.state["last_exit_evidence"] = [f"[下游 {node.id} 的 entry 门禁打回] {verdict.reason[:300]}"]  # 同上：清矛盾证据
            self.state.setdefault("feedback_kinds", {})[prev.id] = "bounce"
            # 下游产物基于旧上游产出被判无效：清其主体与澄清 sid，重进时不得
            # resume 失效上下文（强制全新/完整任务书；澄清结论同样基于旧上游产出）
            self.state["node_states"][node.id].setdefault("sessions", {}).pop("subject", None)
            self.state["node_states"][node.id].setdefault("sessions", {}).pop("clarify", None)
            # P2-14：sid 清了但 dialog 文件留存——下轮全新 clarify 的提示词把
            # 已失效对话原样嵌入「此前对话纪要」（D2 钉住的不变量被
            # dialog-brief 路径绕过）。sid 与 dialog 必须同生共死
            self._clear_dialog(node.id)
            self.state["blocked_owner"] = prev.id
            self._record("blocked", node=prev.id, entry_bounces=pns["entry_bounces"])
            self._save()
            return False
        pns["phase"] = "working"
        self.state["current"] = prev.id
        # 上游自己的 exit 通过证据已与本次打回矛盾（"产物存在" vs "产物缺失"）：
        # 清空，主体重跑时不再注入误导性「核验证据」，改用本次 entry 失败证据
        self.state["last_exit_evidence"] = [f"[下游 {node.id} 的 entry 门禁打回] {verdict.reason[:300]}"]
        self.state["feedbacks"][prev.id] = f"[下游 {node.id} 的 entry 门禁打回] {verdict.reason}\n修改指引：{verdict.feedback_text}"
        self.state.setdefault("feedback_kinds", {})[prev.id] = "bounce"
        # 同上：下游主体与澄清 sid 存在则清（其产物与澄清结论基于旧上游产出，已判无效）
        self.state["node_states"][node.id].setdefault("sessions", {}).pop("subject", None)
        self.state["node_states"][node.id].setdefault("sessions", {}).pop("clarify", None)
        self._clear_dialog(node.id)  # P2-14：与 sid 同生共死（见上）
        self._record("bounce_prev", frm=node.id, to=prev.id, entry_bounces=pns["entry_bounces"])
        self._save()
        return True

    # ------------------------------------------------------------ 要点澄清（clarify 通道）

    def _dialog_path(self, node_id: str) -> str:
        return os.path.join(self.gdd_dir, "dialogs", f"{node_id}.jsonl")

    def _clear_dialog(self, node_id: str) -> None:
        """删除节点澄清对话文件（与 sessions.clarify sid 同生共死——P2-14）。

        entry-bounce 判下游澄清结论失效时清 sid；dialog 文件若留存，下轮
        全新 clarify 会话把失效对话嵌入「此前对话纪要」，污染新一轮澄清。
        """
        p = self._dialog_path(node_id)
        try:
            os.remove(p)
        except OSError:
            pass  # 不存在/已删：目标态已达成

    def _dialog_append(self, node_id: str, role: str, text: str) -> None:
        p = self._dialog_path(node_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "role": role, "text": text}, ensure_ascii=False) + "\n")

    def dialog_tail(self, node_id: str, n: int = 30) -> List[Dict[str, str]]:
        p = self._dialog_path(node_id)
        if not os.path.exists(p):
            return []
        lines: List[Dict[str, str]] = []
        try:
            with open(p, encoding="utf-8") as f:
                for l in f:
                    l = l.strip()
                    if not l:
                        continue
                    try:
                        lines.append(json.loads(l))
                    except json.JSONDecodeError:
                        continue  # 单行撕裂（并发写入半行）：跳过该行，不丢整个尾部
        except OSError:
            return []
        return lines[-n:]

    def _clarify_context(self) -> Optional[Dict[str, Any]]:
        """校验当前是否处于可澄清窗口，返回 {node, mode, resume_sid}；否则 None。

        两种窗口（docs/design-clarify-resume.md §4）：
        - pending  ：节点启动前预对齐，独立澄清会话；
        - awaiting ：审批挂起时对产出追问，resume 主体会话。
        """
        st = self.state
        if st["status"] == "running" and st.get("current"):
            node = self.pipeline.node(st["current"])
            if node and node.clarify:
                ns = st["node_states"][node.id]
                if ns["phase"] == "pending":
                    return {"node": node, "mode": "pending",
                            "resume_sid": ns.setdefault("sessions", {}).get("clarify", "")}
        if st["status"] == "awaiting_approval" and st.get("pending_approval"):
            node = self.pipeline.node(st["pending_approval"]["node"])
            if node and node.clarify:
                ns = st["node_states"][node.id]
                return {"node": node, "mode": "review",
                        "resume_sid": ns.setdefault("sessions", {}).get("subject", "")}
        return None

    def _ensure_integrity(self, channel: str = "澄清通道") -> Optional[str]:
        """交互入口（chat/clarify_done/approve/retry）的指纹门禁：fail-closed，仅放行合法 legacy 迁移。

        注意：指纹键缺失（state.json 被删键）时 verify_pipeline_integrity 返回 False →
        走 legacy 迁移判定（迁移要求指纹在场，同样拒绝）→ 返回错误。绝不短路放行
        （曾因 `not fp or ...` 短路使删键绕过校验，approve 在被篡改 pipeline 下放行）。
        """
        if self.verify_pipeline_integrity():
            pass  # 指纹过 → 还须过 input 门（见下，两个门都要过）
        elif self._migrate_legacy_fingerprint():
            self._record("pipeline_fingerprint_migrated")
            self._save()
        else:
            return (f"流水线指纹不一致（实例被篡改或版本不兼容），{channel}已拒绝；"
                    "请人工核验 .gdd/pipeline.json")
        # P1-3（R6，run() P0-2 的同族收口）：交互通道同样不喂被投毒的
        # input.md——chat 会把需求原文嵌进 clarify agent 提示词，其结论写入
        # 产物文档后**永久残留**（用户事后恢复 input.md 也无法撤回已落盘的
        # 投毒内容）。approve/retry/clarify_done 不喂文件但消费状态，同门
        # 防御（后续 run() 也会拦，这里是提前拒绝+准确归因）。
        if self._input_text is None:
            return (f".gdd/input.md 与记录的需求指纹不符（篡改或缺失），{channel}已拒绝；"
                    "请人工核验 .gdd/input.md（恢复原文或 gdd reset）")
        return None

    def chat(self, node_id: str, message: str) -> Dict[str, Any]:
        """澄清对话一轮：不动状态机，只 resume/新建澄清会话并落盘对话。"""
        if not message.strip():
            return {"ok": False, "error": "消息为空"}
        if not self._acquire_lock():
            raise LockBusyError("另一个 gdd 进程正在运行（.gdd/lock 被占用）")
        try:
            self.load()
            if not self.state:
                # 与 run() 对齐：load 失败（并发 reset 删 .gdd / 损坏）时不得继续，
                # 否则 _acquire_lock 重建 .gdd + _save 会复活已 reset 的 state
                raise RuntimeError("状态加载失败（.gdd 可能已被 reset/损坏），请刷新后重试")
            err = self._ensure_integrity()
            if err:
                return {"ok": False, "error": err}
            ctx = self._clarify_context()
            if ctx is None or ctx["node"].id != node_id:
                return {"ok": False, "error": f"节点 {node_id} 当前不可澄清（需 clarify: true 且处于启动前/审批挂起窗口）"}
            node, mode = ctx["node"], ctx["mode"]
            runner = AgentRunner(self.root)
            sessions = self.state["node_states"][node.id].setdefault("sessions", {})
            # GDD_SUBJECT_RESUME=off 全局逃生舱：chat 同样遵守（--resume 坏掉的环境里
            # 每条 review 消息都注定失败并触发降级路径）
            effective_resume_sid = ("" if os.environ.get("GDD_SUBJECT_RESUME", "on") == "off"
                                    else ctx["resume_sid"])
            raw_req = self.input_path if os.path.exists(self.input_path) else ""
            self._dialog_append(node.id, "user", message)

            def _run_one(sid: str) -> Any:
                prompt = gates.build_clarify_prompt(
                    node, self.pipeline,
                    raw_requirement_path=raw_req, resumed=bool(sid)) + f"\n\n用户：{message}"
                if not sid:
                    # 降级/首轮全新澄清会话：带上此前对话纪要，避免上下文丢失
                    tail = self.dialog_tail(node.id, 10)
                    if tail:
                        brief = "\n".join(f"{d.get('role', '?')}: {d.get('text', '')[:200]}" for d in tail[:-1])
                        prompt = f"（此前对话纪要，最后一条为最新）\n{brief}\n\n{prompt}"
                stream_cb = None
                if self.on_stream:
                    try:
                        stream_cb = self.on_stream("clarify")
                    except Exception:
                        stream_cb = None
                kw = {}
                if stream_cb is not None:
                    kw["on_stream"] = stream_cb  # 仅真流式时传参，兼容测试双不接该 kw
                return runner.run(
                    prompt, allowed_tools=gates.SUBJECT_TOOLS,
                    max_turns=max(node.subject.max_turns, 20),
                    log_path=self._log_path(node, f"clarify_{mode}"),
                    resume_sid=sid,
                    **kw,
                )

            session = _run_one(effective_resume_sid)
            was_resume = bool(effective_resume_sid)  # 本轮是否真在 resume 既有会话
            resumed_ok = session.ok
            if not session.ok and effective_resume_sid and not session.turns_exhausted:
                # resume 失效：降级全新澄清会话重试一次。turns 打满的会话仍存活
                # （与 _phase_working 同语义）：不 pop、不降级——降级即判死存活会话，
                # 且 pop 的是全组主体 sid（clarify/review 两路都受害者）
                self._record("session_resume_fail", node=node.id, error=session.error[:200])
                if mode == "pending":
                    sessions.pop("clarify", None)
                else:
                    # review：组级清理失效主体 sid（与 _phase_working 降级语义一致）——
                    # 仅清本节点位会让组查找继续命中上游节点的同一死会话，
                    # 后续每条消息都做注定失败的 resume；会话失效是组级的（同一 sid）
                    self._pop_subject_group_sid(self._node_group(node))
                session = _run_one("")
            self._check_tamper(f"clarify_{mode}")
            self._dialog_append(node.id, "assistant", session.text or session.error or "（会话无输出）")
            if session.ok and session.session_id:
                # 仅成功会话才存档 sid（失败会话的 sid 可能已死，存了会反复 resume 失败）
                if mode == "pending":
                    sessions["clarify"] = session.session_id
                elif was_resume and resumed_ok:
                    # review 窗口：仅本轮确实 resume 成功（延续的就是主体会话本身且
                    # 未降级）才回写主体 sid；resume 失败后降级的全新会话是独立澄清
                    # 会话，冒充主体会话会让下轮 _phase_working resume 一个无工作
                    # 上下文的会话（投毒）。was_resume 单独使用同样漏掉降级轮——
                    # resume 失败 → 降级成功 → sid 已 pop，若回写即投毒
                    sessions["subject"] = session.session_id
            self._record("clarify_msg", node=node.id, mode=mode)
            self._save()
            if not session.ok:
                # persisted=True：会话故障路径（user 行 + assistant 行已落盘
                # dialog）——UI 文案按此分叉（P2-15），拒绝路径才是「未落盘」
                return {"ok": False, "persisted": True, "streamed": session.streamed,
                        "error": f"澄清会话失败: {session.error[:300]}"}
            return {"ok": True, "mode": mode, "reply": session.text,
                    "streamed": session.streamed}
        finally:
            self._release_lock()

    def clarify_done(self, node_id: str) -> Dict[str, Any]:
        """结束澄清：pending 态仅记事件；审批挂起态必须强制重验（主体 resume 收尾 + 全新 exit 门禁）。"""
        if not self._acquire_lock():
            raise LockBusyError("另一个 gdd 进程正在运行（.gdd/lock 被占用）")
        try:
            self.load()
            if not self.state:
                # 空态守卫（chat/run/approve/retry/abort 都有，此处曾漏）：
                # 并发 reset 删 .gdd 后不得继续，否则误导性报「指纹不一致」
                # 且 _acquire_lock 重建 .gdd 复活空目录
                raise RuntimeError("状态加载失败（.gdd 可能已被 reset/损坏），请刷新后重试")
            err = self._ensure_integrity()
            if err:
                return {"ok": False, "error": err}
            st = self.state
            ctx = self._clarify_context()
            if ctx is None or ctx["node"].id != node_id:
                return {"ok": False, "error": f"节点 {node_id} 当前不在澄清窗口"}
            node, mode = ctx["node"], ctx["mode"]
            self._record("clarify_done", node=node.id, mode=mode)
            if mode == "pending":
                self._save()
                return {"ok": True, "mode": mode, "recheck": False}
            # review：清审批挂起（不计打回），纪要注入反馈 → working，走主体 resume + exit 门禁重验
            dialog = self.dialog_tail(node.id, n=10)
            brief = "\n".join(f"[{d['role']}] {d['text'][:300]}" for d in dialog if d["role"] == "user") or "（对话无用户要点）"
            st["pending_approval"] = None
            st["status"] = "running"
            ns = st["node_states"][node.id]
            ns["phase"] = "working"  # 主体会话 resume 收尾，随后 exit 门禁全新会话重验
            st["feedbacks"][node.id] = (
                f"（澄清对齐纪要，非打回）用户要点澄清已结束（完整对话见 .gdd/dialogs/{node.id}.jsonl）。"
                f"请结合以下澄清要点核对/完善产出后重新完成本节点（对齐要点非强制判据，按用户意图权衡）：\n{brief}")
            st.setdefault("feedback_kinds", {})[node.id] = "clarify"
            self._save()
            return {"ok": True, "mode": mode, "recheck": True}
        finally:
            self._release_lock()

    # ------------------------------------------------------------ 审批 / 重试 / 复位

    def resolve_approval(self, approved: bool, feedback: str = "") -> bool:
        if not self._acquire_lock():
            # 锁忙且未做任何变更：审批未生效。用专用子类区分「未生效锁忙」与
            # 「审批后推进遇锁忙」，CLI 不能再把前者误报为「已生效」（P1-7）
            raise LockBusyError("另一个 gdd 进程正在运行（.gdd/lock 被占用）——审批尚未生效，请稍后重试")
        try:
            self.load()  # 拿锁后重读，防 TOCTOU
            if not self.state:
                # 与 run() 对齐：load 失败不继续（否则 _save 复活已 reset 的 state；
                # 未 load 过的实例在此处是裸 KeyError 'status'）
                raise RuntimeError("状态加载失败（.gdd 可能已被 reset/损坏），请刷新后重试")
            # 指纹门禁：awaiting_approval 态同样可能遭遇篡改（对称防线，同 chat/clarify_done）
            err = self._ensure_integrity(channel="审批通道")
            if err:
                print(err)
                self.last_refusal = err  # 拒绝原因前端可见（同产物快照拦截路径）
                self._record("approval_refused_integrity")
                self._save()
                return False
            return self._resolve_approval_locked(approved, feedback)
        finally:
            self._release_lock()

    def _resolve_approval_locked(self, approved: bool, feedback: str = "") -> bool:
        st = self.state
        if st["status"] != "awaiting_approval" or not st.get("pending_approval"):
            print("当前没有待处理的人工准入。")
            return False
        node = self.pipeline.node(st["pending_approval"]["node"])
        snap = dict(st["pending_approval"].get("artifact_snapshot") or {})
        # P1-5：守卫按快照是否在场判定（与交互路径一致）——legacy 实例的
        # pending_approval 无 artifact_snapshot（快照与 clarify 同版本引入），
        # 曾按 node.exit.artifacts 判：legacy 声明产物的节点被当「全部产物已变更」
        # 拒绝，永久无法审批。无快照=无基线=无从判变更，放行走审批（产物完整性
        # 由 exit 门禁与交付层把关，legacy 本就经过门禁）
        changed = self._artifact_changes(node, snap) if snap else []
        if approved and changed:
            # 审批期间产物被修改（含 glob 新增匹配文件；典型：澄清会话写入）——不消费审批项，
            # 保持挂起；引导走 clarify_done（强制重验）或驳回打回
            self.last_refusal = ("审批期间产物已变更（疑似澄清会话写入）："
                                 + ", ".join(changed[:5])
                                 + "。请先「结束澄清」触发重验，或驳回打回。")
            self._record("approval_refused_artifact_changed", node=node.id, files=changed[:5])
            self._save()
            return False
        st["pending_approval"] = None
        st["status"] = "running"
        if approved:
            self._record("approval_pass", node=node.id)
            self._save()
            # 审批动作本身已成功即返回 True——不能用 _complete/_advance 的推进返回值
            # 表达（末节点 _advance 返回 False，会把必现的成功审批误报为失败）
            self._complete(node)
            return True
        fb = feedback or "人工审批驳回，未填写具体反馈。"
        self._record("approval_reject", node=node.id, feedback=fb[:300])
        # 驳回动作本身已生效：_bounce_current 在触达 max_bounces 时返回 False
        # （置 blocked），不得把「驳回已受理」误报为失败——CLI/UI 曾因此宣称
        # 「审批未生效」或依赖 blocked 旁路猜测。与上方 approve 路径对称：
        # 动作成功即 True，推进/blocked 是落盘后的状态机结果。
        self._bounce_current(node, gates.Verdict(passed=False, reason="人工审批驳回", feedback=fb))
        return True

    def retry(self) -> bool:
        if not self._acquire_lock():
            raise LockBusyError("另一个 gdd 进程正在运行（.gdd/lock 被占用）")
        try:
            self.load()  # 拿锁后重读，防 TOCTOU
            if not self.state:
                raise RuntimeError("状态加载失败（.gdd 可能已被 reset/损坏），请刷新后重试")
            # 指纹门禁：blocked 态同样可能遭遇篡改（对称防线，同 chat/clarify_done/approve）
            err = self._ensure_integrity(channel="重试通道")
            if err:
                print(err)
                self.last_refusal = err  # 拒绝原因前端可见（同产物快照拦截路径）
                self._record("retry_refused_integrity")
                self._save()
                return False
            return self._retry_locked()
        finally:
            self._release_lock()

    def _retry_locked(self) -> bool:
        st = self.state
        if st["status"] != "blocked":
            print("当前不处于 blocked 状态，无需 retry。")
            return False
        st["status"] = "running"
        st["blocked_reason"] = None
        st["gate_error_streak"] = 0
        # 责任节点：blocked_owner（entry 打回型 blocked 在上游节点）；缺省为当前节点
        owner_id = st.get("blocked_owner") or st["current"]
        owner = self.pipeline.node(owner_id)
        if owner is None:
            owner = self._current_node()
        st["blocked_owner"] = None
        ns = st["node_states"][owner.id]
        ns["bounces"] = 0
        ns["entry_bounces"] = 0
        ns["auto_continues"] = 0  # 人工重试=新的一轮：刷新主体 turns 打满自动续跑预算
        # 门禁故障型重跑门禁而非主体；其余重跑主体。P1-2（R6）：exit_retry
        # 也可能是**真 verdict fail 后的打回**（bounce-blocked 落在 exit_retry
        # 相位：_bounce_current 置 working + feedbacks 在，但 max_bounces 打满
        # 时 phase 停在 exit_retry）——此时只重跑门禁=反复重验同一批产物、
        # 反馈永不送达、retry 永久砖化。区分：feedbacks 里有本节点的打回
        # 反馈 = 主体有债要还，必须重跑主体（working）；干净 exit_retry
        # （纯门禁故障，无 pending 反馈）才只重验门禁。
        if ns["phase"] == "exit_retry":
            has_feedback = bool((st.get("feedbacks") or {}).get(owner.id))
            if has_feedback:
                ns["phase"] = "working"  # 真打回：反馈须送达主体重做
        elif ns["phase"] != "pending":
            ns["phase"] = "working"
        if owner.id != st["current"]:
            # entry 打回型：把执行点从下游节点指回责任节点（下游保持 pending，重新走 entry）
            st["current"] = owner.id
        self._record("retry", node=owner.id)
        self._save()
        return True

    def abort(self) -> bool:
        """返回是否本次真正执行了中止（终态时 False——CLI 不得宣称未发生的动作）。"""
        if not self._acquire_lock():
            raise LockBusyError("另一个 gdd 进程正在运行（.gdd/lock 被占用）")
        try:
            self.load()  # 拿锁后重读，防 TOCTOU
            if not self.state:
                raise RuntimeError("状态加载失败（.gdd 可能已被 reset/损坏），请刷新后重试")
            if self.state["status"] in TERMINAL:
                print(f"已处于终态（{self.state['status']}），无需 abort。")
                return False
            self.state["status"] = "aborted"
            # 终态不留幽灵审批卡：aborted 后 UI 不得再渲染可点的 approve/reject
            #（与 tty 交互 approve/reject 分支对称，abort 路径曾漏）
            self.state["pending_approval"] = None
            self._record("aborted")
            self._save()
            return True
        finally:
            self._release_lock()

    def acknowledge(self) -> bool:
        """人工确认滞留警示已处置（P2-9/P1-2）：清除 stray_delivery 并把
        当前分支状态刷新为新基线。返回是否有警示被清除（无警示时 False）。

        git 无法区分「主体会话的提交」与「用户本人的提交」——检测器在这两者
        间只能二选一（指控误伤用户 / 静默放走主体）。本命令是人工裁决通道：
        用户核实滞留提交确属本人工作或已 merge 后显式确认，警示清除且快照
        刷新，后续交付不再重复误报。
        """
        if not self._acquire_lock():
            raise LockBusyError("另一个 gdd 进程正在运行（.gdd/lock 被占用）")
        try:
            self.load()
            if not self.state:
                raise RuntimeError("状态加载失败（.gdd 可能已被 reset/损坏），请刷新后重试")
            # 与 approve/chat/retry 对称：指纹门禁，防篡改 pipeline 后洗白 stray
            err = self._ensure_integrity(channel="确认滞留通道")
            if err:
                print(err)
                self.last_refusal = err
                self._record("acknowledge_refused_integrity")
                self._save()
                return False
            if not self.state.get("stray_delivery"):
                return False
            self.state.pop("stray_delivery", None)
            # 吸收**全部**本地分支 tip 为新基线（与 init 快照同形）。
            # 不可复用 _refresh_branch_snapshot（只推交付 tip——给 push 用，
            # 防止误清真滞留）；此处人工已裁决滞留属本人/已处置，side tip
            # 必须纳入基线，否则下一交付立刻按旧快照再报同一 stray。
            fer = self._git_proc("for-each-ref", "refs/heads/",
                                 "--format=%(refname:short) %(objectname)")
            if fer is not None and fer.returncode == 0:
                self.state["git_branch_snapshot"] = dict(
                    ln.strip().split() for ln in fer.stdout.splitlines()
                    if len(ln.strip().split()) == 2)
            self._record("stray_delivery_acknowledged",
                         reason="人工确认滞留警示已处置，快照刷新为新基线")
            self._save()
            return True
        finally:
            self._release_lock()
