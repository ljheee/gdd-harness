"""数据模型：Pipeline / Node / Gate / Approval / Subject。

Pipeline 定义采用零依赖 JSON（不引入 PyYAML），字段见 README。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_LEADING_FLAGS_RE = re.compile(r"^\(\?([a-zA-Z]+)\)(.*)$", re.S)
_INLINE_FLAGS_RE = re.compile(r"\(\?([a-zA-Z]+)\)")


def _normalize_leading_flags(pat: str) -> str:
    """把前导全局 regex flag 规范化为局部作用域形式：`(?i)FOO` → `(?i:FOO)`。

    Python>=3.11 中非开头的全局 flag 是 re.error；3.9/3.10 中作用于
    整个合并模式（静默弱化原判据）。局部化后两种语义一致且嵌套安全。
    仅 ASCII flag 组合处理，其余原样返回（后续编译校验会拦）。
    """
    if not pat:
        return pat
    m = _LEADING_FLAGS_RE.match(pat)
    if not m:
        return pat
    flags, rest = m.group(1), m.group(2)
    if not flags or set(flags) - set("imsxaLu"):
        return pat
    return f"(?{flags}:{rest})"


def _normalize_inline_flags(pat: str) -> str:
    """把非前导的内联全局 flag 提升为 scoped 形式：`tag(?i)name` → `tag(?i:name)`。

    前导的归 _normalize_leading_flags；这里只处理内联（mid-pattern）出现的
    `(?letters)`。提升后 flag 作用到模式尾（补 `)` 闭合），3.9/3.10 与 3.11+
    语义一致，且不再静默弱化原判据（原全局弱化是 bug 不是语义）。
    归一化结果必须可编译，不可编译则原样返回交由编译校验 fail-closed。
    """
    if not pat or "(?" not in pat:
        return pat
    # 先归一化前导（scoped 形式 `(?i:...)` 不会再被 _INLINE_FLAGS_RE 命中：
    # 该 regex 要求字母后紧跟 `)`）
    pat = _normalize_leading_flags(pat)
    out: List[str] = []
    pos = 0
    opens = 0
    for m in _INLINE_FLAGS_RE.finditer(pat):
        start, end = m.span()
        flags = m.group(1)
        if not flags or set(flags) - set("imsxaLu"):
            continue  # 非法/未知 flag：保留原样，交由编译校验拦
        # 字符组内（如 [a(?i)] 字面字符）不处理：检查匹配之前的文本
        # 是否存在未闭合的 '['（转义的 \[ 不计）
        depth = 0
        i = 0
        prefix = pat[:start]
        while i < len(prefix):
            ch = prefix[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "[":
                depth += 1
            elif ch == "]" and depth:
                depth -= 1
            i += 1
        if depth:
            continue
        out.append(pat[pos:start])
        out.append(f"(?{flags}:")
        pos = end
        opens += 1
    if not opens:
        return pat
    out.append(pat[pos:])
    result = "".join(out) + ")" * opens
    try:
        re.compile(result)
    except re.error:
        return pat  # 归一化失败：保留原样，由编译校验 fail-closed
    return result


@dataclass
class ArtifactCheck:
    """产物检查：路径存在（支持 glob），可选 must_contain 正则。"""

    path: str
    must_contain: Optional[str] = None  # re.search 语义

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ArtifactCheck":
        p = d["path"]
        mc = d.get("must_contain")
        if not isinstance(p, str) or not p.strip():
            raise ValueError(f"artifact.path 必须是非空字符串，got: {p!r}")
        if not isinstance(p, str) or "\x00" in p:
            raise ValueError("artifact.path 含 NUL 字节（会在机器检查期裸崩）")
        if mc is not None and not isinstance(mc, str):
            # list 值会在机器检查期 re.compile 裸崩；YAML 双行写法常见，解析期拦下
            raise ValueError(f"artifact.must_contain 必须是字符串（多判据请写正则或拆两项），got: {mc!r}")
        if mc:
            if "\x00" in mc:
                raise ValueError("artifact.must_contain 含 NUL 字节")
            # 内联全局 flag（前导/中段）先归一化为 scoped 形式再校验/存储：
            # 否则 3.9 接受且全局生效弱化判据、3.11+ 直接 re.error——
            # 同一模板跨解释器行为分裂（实测复现过）
            mc = _normalize_inline_flags(mc)
            try:
                re.compile(mc)
            except re.error as e:
                # 与补充路径一致：非法正则 init 拦下（否则门禁时烧满 max_bounces
                # 个完整主体会话才 blocked）
                raise ValueError(f"artifact.must_contain 不是合法正则: {mc!r}（{e}）")
        return ArtifactCheck(path=p, must_contain=mc or None)

    def to_dict(self) -> Dict[str, Any]:
        r: Dict[str, Any] = {"path": self.path}
        if self.must_contain:
            r["must_contain"] = self.must_contain
        return r


@dataclass
class GateSpec:
    """entry：前置条件（基础卡控）；exit：目标状态（成功判据：可验证产物/目标）。"""

    goal: str = ""                       # 判据的自然语言描述（给 AI 门禁与文档）
    artifacts: List[ArtifactCheck] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)  # harness 直接执行（bash -c），非 LLM 自报
    ai_check: str = ""                   # 机器检查之外的 AI 验证指令（可选）
    change_aware: bool = False           # exit 门禁注入基于 git 改动的补充验证

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "GateSpec":
        if not d:
            return GateSpec()
        cmds = d.get("commands", [])
        if not isinstance(cmds, list):
            # YAML 漏写 `- ` 列表标记会把整条命令拆成逐字符命令风暴，解析期拦下
            raise ValueError(
                f"gate.commands 必须是列表（每项一条完整命令），got: {cmds!r}")
        for item in cmds:
            if not isinstance(item, str):
                raise ValueError(
                    f"gate.commands 每项必须是完整命令字符串（YAML 裸 `-` 会产生 null 项），got: {item!r}")
            if "\x00" in item:
                # NUL 穿透会让 subprocess 每次 run 裸抛 ValueError 且固化在 pipeline
                raise ValueError("gate.commands 含 NUL 字节（模板拼写错误或幻觉补充值）")
        # P2-8：goal/ai_check 类型守卫（commands 已严格校验、这两个没有）——
        # `ai_check: ["判据A"]` 进库后首次 ai_check 类补充拼接
        # `gate.ai_check + "\n" + v` 裸 TypeError 崩出 run 循环，且崩溃在
        # _save_pipeline 前形成反复崩溃环
        raw_goal = d.get("goal", "")
        raw_ai = d.get("ai_check", "")
        if not isinstance(raw_goal, str):
            raise ValueError(f"gate.goal 必须是字符串（得到 {type(raw_goal).__name__}）")
        if not isinstance(raw_ai, str):
            raise ValueError(f"gate.ai_check 必须是字符串（得到 {type(raw_ai).__name__}）")
        return GateSpec(
            goal=raw_goal,
            artifacts=[ArtifactCheck.from_dict(a) for a in d.get("artifacts", [])],
            commands=list(cmds),
            ai_check=raw_ai,
            change_aware=_parse_bool(d.get("change_aware", False)),
        )

    def to_dict(self) -> Dict[str, Any]:
        r: Dict[str, Any] = {}
        if self.goal:
            r["goal"] = self.goal
        if self.artifacts:
            r["artifacts"] = [a.to_dict() for a in self.artifacts]
        if self.commands:
            r["commands"] = self.commands
        if self.ai_check:
            r["ai_check"] = self.ai_check
        if self.change_aware:
            r["change_aware"] = True
        return r


def _parse_bool(v) -> bool:
    """YAML/JSON 布尔解析：引号字符串 "false"/"0"/"no" 不得被 bool() 强转为 True
    （作者想禁用的字段会全部反向启用）。仅原生 bool 与有限字符串集合可信。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0", ""):
            return False
    if v is None:
        return False
    # 未识别形态（非 bool/非已知字符串/非 None，如 "否"、1.5、列表）不猜语义：
    # 猜 True 会把作者想禁用的字段反向启用，猜 False 会静默吞掉作者意图。
    # 显式 raise，把配置错误暴露在 init/加载时而非运行时。
    raise ValueError(f"非法布尔值（仅接受 bool 或 true/false/yes/no/on/off/1/0）: {v!r}")


@dataclass
class ApprovalSpec:
    """是否需要人工准入（request_approval）才能进入下一阶段。"""

    required: bool = False
    prompt: str = ""  # 展示给人的确认要点

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "ApprovalSpec":
        if not d:
            return ApprovalSpec()
        return ApprovalSpec(required=_parse_bool(d.get("required", False)),
                             prompt=d.get("prompt", ""))

    def to_dict(self) -> Dict[str, Any]:
        r: Dict[str, Any] = {"required": self.required}
        if self.prompt:
            r["prompt"] = self.prompt
        return r


@dataclass
class SubjectSpec:
    """节点执行主体：负责该节点的真正工作。"""

    goal: str = ""
    instructions: str = ""
    max_turns: int = 80

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "SubjectSpec":
        if not d:
            return SubjectSpec()
        return SubjectSpec(
            goal=d.get("goal", ""),
            instructions=d.get("instructions", ""),
            max_turns=int(d.get("max_turns", 80)),
        )

    def to_dict(self) -> Dict[str, Any]:
        r: Dict[str, Any] = {}
        if self.goal:
            r["goal"] = self.goal
        if self.instructions:
            r["instructions"] = self.instructions
        r["max_turns"] = self.max_turns
        return r


@dataclass
class GitDeliverySpec:
    """exit 通过后的 git 交付：产出提交推送到开发分支（harness 执行，非 LLM）。"""

    enabled: bool = True
    branch: str = "gdd/delivery"   # 开发交付分支
    remote: str = ""               # 空 = 自动探测首个 remote；无 remote 则仅本地提交

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "GitDeliverySpec":
        if not d:
            return GitDeliverySpec()
        raw_branch = str(d.get("branch") or "gdd/delivery")
        # ref 安全校验：拒绝选项/注入形分支名（checkout/push 会把它们当选项执行，
        # --force 类会丢弃工作区/未提交修改）
        if raw_branch.startswith("-") or ".." in raw_branch or any(
                c.isspace() or ord(c) < 0x20 for c in raw_branch) or ":" in raw_branch:
            raise ValueError(
                f"git_delivery.branch 含非法字符（拒绝选项注入类值）: {raw_branch!r}")
        # P2-6：remote 与 branch 同威胁模型（branch 注释自述防选项注入，
        # remote 漏防——`-o` 类值进 `push -u -o gdd/delivery` argv 吞掉
        # refspec）。校验对称：拒绝空白/控制字符/选项注入形态。
        raw_remote = str(d.get("remote") or "")
        if raw_remote and (
                raw_remote.startswith("-") or any(
                    c.isspace() or ord(c) < 0x20 for c in raw_remote)):
            raise ValueError(
                f"git_delivery.remote 含非法字符（拒绝选项注入类值）: {raw_remote!r}")
        return GitDeliverySpec(
            enabled=_parse_bool(d.get("enabled", True)),
            # or 守卫：YAML 空值/JSON null 不得变成字面量 "None"（否则交付到名为 None 的分支）
            branch=raw_branch,
            remote=raw_remote,
        )

    def to_dict(self) -> Dict[str, Any]:
        r: Dict[str, Any] = {"enabled": self.enabled, "branch": self.branch}
        if self.remote:
            r["remote"] = self.remote
        return r


@dataclass
class McpSpec:
    """节点主体会话额外加载的一个 MCP server，或一份 mcpServers JSON 文件。"""

    name: str = ""
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    path: str = ""  # 相对项目根或绝对路径的 mcpServers JSON

    def to_dict(self) -> Any:
        if self.path and not self.name:
            return self.path
        d: Dict[str, Any] = {}
        if self.path:
            d["path"] = self.path
            return d
        d["name"] = self.name
        if self.command:
            d["command"] = self.command
            if self.args:
                d["args"] = list(self.args)
            if self.env:
                d["env"] = dict(self.env)
        if self.url:
            d["url"] = self.url
            if self.headers:
                d["headers"] = dict(self.headers)
        return d


@dataclass
class SkillSpec:
    """节点主体会话额外加载的一个 skill 目录（内含 SKILL.md）或插件目录。"""

    path: str
    name: str = ""

    def to_dict(self) -> Any:
        if not self.name:
            return self.path
        return {"path": self.path, "name": self.name}


_MCP_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _no_inject(value: str, what: str) -> str:
    text = str(value or "")
    if not text or any(c in text for c in "\n\r\x00") or text.startswith("-"):
        raise ValueError(f"非法{what}: {value!r}（不能为空、不能换行、不能以 - 开头）")
    return text


def _under_root_or_abs(raw: Any, what: str) -> str:
    """相对路径每一段都不能是 ..；绝对路径允许（共用 skill/MCP 常在仓库外）。

    只看规范化后的前缀不够：skills/link/../secret 在 normpath 之后落在项目内，
    但 prepare 的 realpath 会先跟着符号链接走出去。
    """
    text = _no_inject(raw, what)
    if os.path.isabs(text):
        return text
    parts = text.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise ValueError(f"{what}不能逃出项目根: {raw!r}")
    return text


def _str_map(raw: Any, what: str) -> Dict[str, str]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"MCP {what} 必须是字符串映射")
    out: Dict[str, str] = {}
    for k, v in raw.items():
        key = _no_inject(k, f"{what} 键")
        if not isinstance(v, str) or any(c in v for c in "\n\r\x00"):
            raise ValueError(f"MCP {what}[{key}] 必须是字符串")
        out[key] = v
    return out


def _mcp_list(raw: Any) -> List[McpSpec]:
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("mcp 必须是列表")
    seen = set()
    out: List[McpSpec] = []
    for item in raw:
        if isinstance(item, str):
            out.append(McpSpec(path=_under_root_or_abs(item, "mcp 文件")))
            continue
        if not isinstance(item, dict):
            raise ValueError(f"非法 mcp 条目: {item!r}")
        path = item.get("path") or ""
        if path and not isinstance(path, str):
            raise ValueError(f"mcp 文件路径必须是字符串: {path!r}")
        command = item.get("command") or ""
        url = item.get("url") or ""
        if command and not isinstance(command, str):
            raise ValueError(f"mcp command 必须是字符串: {command!r}")
        if url and not isinstance(url, str):
            raise ValueError(f"mcp url 必须是字符串: {url!r}")
        if path and (command or url or item.get("name")):
            raise ValueError("mcp 条目写了 path 就不能再写 name/command/url")
        if path:
            out.append(McpSpec(path=_under_root_or_abs(path, "mcp 文件")))
            continue
        name = str(item.get("name") or "")
        if not _MCP_NAME_RE.fullmatch(name):
            raise ValueError(
                f"非法 MCP 名字: {name!r}（字母或数字开头，仅字母数字_-，且不能以 - 开头）")
        if name in seen:
            raise ValueError(f"MCP 名字重复: {name}")
        seen.add(name)
        if bool(command) == bool(url):
            raise ValueError(f"MCP {name} 必须且只能指定 command 或 url")
        if command:
            command = _no_inject(command, "mcp command")
        if url and (any(c in url for c in "\n\r\x00")
                    or not (url.startswith("https://") or url.startswith("http://"))):
            raise ValueError(f"MCP {name} 的 url 只接受不含换行的 http(s): {url!r}")
        args = item.get("args") or []
        if not isinstance(args, list) or any(
                not isinstance(a, str) or "\n" in a or "\r" in a or "\x00" in a for a in args):
            raise ValueError(f"MCP {name} 的 args 必须是不含换行的字符串列表")
        out.append(McpSpec(
            name=name, command=command, args=list(args),
            env=_str_map(item.get("env"), "env"),
            url=url, headers=_str_map(item.get("headers"), "headers"),
        ))
    return out


def _skill_list(raw: Any) -> List[SkillSpec]:
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("skills 必须是列表")
    out: List[SkillSpec] = []
    for item in raw:
        if isinstance(item, str):
            out.append(SkillSpec(path=_under_root_or_abs(item, "skill 路径")))
            continue
        if not isinstance(item, dict) or not item.get("path"):
            raise ValueError(f"非法 skill 条目: {item!r}")
        name = str(item.get("name") or "")
        if name and not _MCP_NAME_RE.fullmatch(name):
            raise ValueError(f"非法 skill 名字: {name!r}")
        out.append(SkillSpec(path=_under_root_or_abs(item.get("path"), "skill 路径"), name=name))
    return out


@dataclass
class NodeSpec:
    id: str
    name: str = ""
    description: str = ""
    entry: GateSpec = field(default_factory=GateSpec)
    exit: GateSpec = field(default_factory=GateSpec)
    approval: ApprovalSpec = field(default_factory=ApprovalSpec)
    subject: SubjectSpec = field(default_factory=SubjectSpec)
    max_bounces: int = 3  # 打回上限（entry 打回计入前节点，exit/审批打回计入本节点）
    baseline_marker: bool = False  # 进入本节点主体工作时记录 git 基线，供 change_aware exit 使用
    clarify: bool = False  # 允许交互式要点澄清（人工对话对齐，不消耗打回预算，结论必须固化 artifact）
    adversarial: bool = False  # 主体成功后另开会话，核对 verify.md 并按 code/test/spec/env 分流
    review: bool = False  # 只读代码评审节点：独立只读会话输出 GDD-REVIEW，harness 落盘 review.md
    session_group: str = ""  # 主体会话复用组（空=独立，组名仅字母数字_；设计见 docs/design-session-group.md）
    preset_group: bool = False  # 该组名由 session_preset 展开产生（非用户手写；不序列化：磁盘始终存 preset 形态）
    mcp: List[McpSpec] = field(default_factory=list)  # 仅主体会话追加的 MCP，门禁不继承
    skills: List[SkillSpec] = field(default_factory=list)  # 仅主体会话追加的 skill/插件目录

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "NodeSpec":
        # 忽略遗留 notify 键（曾接 IM；现仅打印 done，见 _final_notify）
        return NodeSpec(
            id=d["id"],
            name=d.get("name", d["id"]),
            description=d.get("description", ""),
            entry=GateSpec.from_dict(d.get("entry")),
            exit=GateSpec.from_dict(d.get("exit")),
            approval=ApprovalSpec.from_dict(d.get("approval")),
            subject=SubjectSpec.from_dict(d.get("subject")),
            max_bounces=int(d.get("max_bounces", 3)),
            baseline_marker=_parse_bool(d.get("baseline_marker", False)),
            clarify=_parse_bool(d.get("clarify", False)),
            adversarial=_parse_bool(d.get("adversarial", False)),
            review=_parse_bool(d.get("review", False)),
            session_group=str(d.get("session_group", "") or ""),
            mcp=_mcp_list(d.get("mcp")),
            skills=_skill_list(d.get("skills")),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "entry": self.entry.to_dict(),
            "exit": self.exit.to_dict(),
            "approval": self.approval.to_dict(),
            "subject": self.subject.to_dict(),
            "max_bounces": self.max_bounces,
            "baseline_marker": self.baseline_marker,
            "clarify": self.clarify,
        }
        if self.adversarial:
            d["adversarial"] = True
        if self.review:
            d["review"] = True
        if self.session_group and not self.preset_group:
            # 预设展开产物不落盘（磁盘存 session_preset，load 重放重新展开）——
            # 否则磁盘展开值在重放中被当手写保留字拒绝（P1-4 砖化形态）；
            # 手写组照存（手写即拒的校验在 from_dict 内执行，存下来的必然合法）
            d["session_group"] = self.session_group
        if self.mcp:
            d["mcp"] = [m.to_dict() for m in self.mcp]
        if self.skills:
            d["skills"] = [s.to_dict() for s in self.skills]
        return d


def _valid_node_id(node_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]+", node_id))


def _expand_session_preset(nodes: List["NodeSpec"], preset: str) -> None:
    """把预设展开为逐节点 session_group。

    展开在模板解析时完成，磁盘实例存展开后字段——模板写 preset、实例存展开值，
    两者指纹一致（不引入双形态）。

    保留字（gdd_all/gdd_design{N}）是预设的私有产物：手写保留字直接拒绝
    （P1-7 第三轮同族：曾静默放行/静默置空，非相邻节点可冒名预设段合并；
    两份设计文档承诺「手写即拒」——此处如实执行）。
    """
    if preset == "independent":
        for n in nodes:
            if n.session_group and _is_preset_reserved(n.session_group):
                raise ValueError(
                    f"非法 session_group: {n.session_group!r}（预设展开专用保留字，"
                    f"手写即拒；independent 下无自动段，请换名或改用 session_preset）")
        return  # 缺省：不显式声明组 = 组名取 node.id，即完全独立
    if preset == "shared":
        for n in nodes:
            if n.session_group and _is_preset_reserved(n.session_group):
                raise ValueError(
                    f"非法 session_group: {n.session_group!r}（预设展开专用保留字，"
                    f"手写即拒；shared 下自动归 gdd_all，无需手写）")
            if not n.session_group:
                n.session_group = "gdd_all"
                n.preset_group = True
        return
    # clarify-design：相邻的 clarify 节点段归组，其余独立；分段各自成组
    # （组名 gdd_design、gdd_design2…，非相邻段不共享——避免远距离合并放大污染面。
    # 显式声明组的 clarify 节点自成段边界：它不算入预设段，其后无组 clarify 开新段
    in_seg = False
    seg_idx = 0
    for n in nodes:
        if n.clarify:
            if n.session_group and _is_preset_reserved(n.session_group):
                raise ValueError(
                    f"非法 session_group: {n.session_group!r}（预设展开专用保留字，"
                    f"手写即拒；该节点会被自动段重算覆盖，请换名）")
            if n.session_group:
                in_seg = False  # 显式组自成边界
                continue
            if not in_seg:
                seg_idx += 1
                in_seg = True
            n.session_group = "gdd_design" if seg_idx == 1 else f"gdd_design{seg_idx}"
            n.preset_group = True
        else:
            # P2-26：非 clarify 节点手写保留字（gdd_all 等）曾只查 gdd_design*
            # 形态漏 gdd_all——两节点可借此静默并入保留组共享会话。
            # clarify-design 的保留域 = gdd_design{N}；gdd_all 在此预设下
            # 同为保留字（手写一律拒，自动段绝不产生该名）
            if n.session_group and _is_preset_reserved(n.session_group):
                raise ValueError(
                    f"非法 session_group: {n.session_group!r}（预设展开专用保留字，"
                    f"手写即拒；clarify-design 下非 clarify 节点不会被自动归组，"
                    f"请换名或改用 session_preset: shared）")
            in_seg = False


def _is_preset_reserved(group: str) -> bool:
    """预设保留字：gdd_all 或 gdd_design{N}（自动段产物专用）。"""
    return group == "gdd_all" or bool(re.fullmatch(r"gdd_design\d*", group))


@dataclass
class PipelineSpec:
    id: str
    name: str = ""
    description: str = ""
    nodes: List[NodeSpec] = field(default_factory=list)
    git_delivery: GitDeliverySpec = field(default_factory=GitDeliverySpec)
    session_preset: str = "independent"  # 磁盘序列化字段（重放展开的权威源，见 _expand_session_preset）

    # 会话复用预设（实例序列化存 preset、内存存展开产物——两形态单向转换，
    # 不入指纹双形态：指纹基于 to_dict 输出，始终是 preset 形态）：
    #   independent  全部独立（缺省，等价不写）
    #   clarify-design 相邻 clarify 节点归 gdd_design 组，其余独立
    #   shared       全部归 gdd_all 组
    SESSION_PRESETS = ("independent", "clarify-design", "shared")

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PipelineSpec":
        nodes = [NodeSpec.from_dict(n) for n in d.get("nodes", [])]
        if not nodes:
            raise ValueError("pipeline 至少需要一个节点")
        # 展开必须在节点校验**之前**（P1-4：曾先校验后展开——节点 id 恰等生成
        # 组名（如 id=gdd_design 的 clarify 节点）时 init 按展开前形态通过、
        # 写盘存展开后形态，之后每次 load 重放撞名被拒，误指控「被篡改」、
        # reset-only）。init 与 load 走同一顺序：要么都通过、要么 init 即拒。
        preset = str(d.get("session_preset", "independent") or "independent")
        if preset not in PipelineSpec.SESSION_PRESETS:
            raise ValueError(
                f"非法 session_preset: {preset!r}（可选 {'/'.join(PipelineSpec.SESSION_PRESETS)}）")
        _expand_session_preset(nodes, preset)
        seen = set()
        for n in nodes:
            if not _valid_node_id(n.id):
                raise ValueError(f"非法节点 id: {n.id!r}（仅允许字母数字_-）")
            if n.id in seen:
                raise ValueError(f"节点 id 重复: {n.id}")
            seen.add(n.id)
            if n.session_group and not re.fullmatch(r"[A-Za-z0-9_]+", n.session_group):
                # gdd:* 冒号形式历史拒绝；错误文案声称保留但校验曾放行，一并拒绝
                raise ValueError(
                    f"非法 session_group: {n.session_group!r}（仅允许字母数字_）")
            if n.session_group.startswith("gdd_"):
                # P1-7（第三轮同族）：保留字只允许恰好等于预设展开产物。
                # 展开先于校验且手写保留字被展开逻辑回收（置空/重算），
                # 因此此处带保留字的只可能是预设产物本身；手写非保留 gdd_ 前缀
                # 仍拒（不扩大保留域以外的命名空间）。
                if not (
                    n.session_group == "gdd_all"
                    or (n.clarify and re.fullmatch(r"gdd_design\d*", n.session_group))
                ):
                    raise ValueError(
                        f"非法 session_group: {n.session_group!r}（gdd_ 前缀为预设展开专用，"
                        f"手写即拒，请换名）")
            for other in nodes:
                if other is not n and other.id == n.session_group:
                    # 组名撞节点 id：组名与「独立节点组名取 node.id」同一命名空间，
                    # 撞名即静默合并（违反「未配置节点永远独立」）
                    raise ValueError(
                        f"非法 session_group: {n.session_group!r} 与节点 {other.id!r} 的 id 撞名，"
                        f"会静默合并进该节点的独立会话，请换名")
        return PipelineSpec(
            id=d.get("id", "custom"),
            name=d.get("name", ""),
            description=d.get("description", ""),
            nodes=nodes,
            git_delivery=GitDeliverySpec.from_dict(d.get("git_delivery")),
            session_preset=preset,
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
            "git_delivery": self.git_delivery.to_dict(),
        }
        if self.session_preset and self.session_preset != "independent":
            d["session_preset"] = self.session_preset
        return d

    def node(self, node_id: str) -> Optional[NodeSpec]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def node_order(self) -> Dict[str, int]:
        """节点 id -> 管线序（0 起）。基线回退等「按最新节点」语义用它，
        不能按 state 键名字母序（n1/n2/n10 的字典序 ≠ 管线序）。"""
        return {n.id: i for i, n in enumerate(self.nodes)}

    def prev(self, node_id: str) -> Optional[NodeSpec]:
        for i, n in enumerate(self.nodes):
            if n.id == node_id:
                return self.nodes[i - 1] if i > 0 else None
        return None

    def next(self, node_id: str) -> Optional[NodeSpec]:
        for i, n in enumerate(self.nodes):
            if n.id == node_id:
                return self.nodes[i + 1] if i + 1 < len(self.nodes) else None
        return None
