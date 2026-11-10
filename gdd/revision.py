"""决策定级、增量重跑的指纹比对，以及对抗测试裁决。

定级契约是全局的：主体会话对照它给出等级。L0/L1 经人工确认后，
从需求澄清起按顺序重跑开发阶段之前的节点。重跑把修订稿写到暂存区，
只有基线指纹没变才复制到正式产物上。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from typing import Dict, List, Optional

FINGERPRINT_FILES = (
    "docs/goal.md",
    "docs/spec.md",
    "docs/plan.md",
    "docs/decisions.md",
)
STAGE_ROOT = ".gdd/revision-stage"
BASELINE_ROOT = ".gdd/revision-baseline"

CONTRACT = """决策定级契约（全局）：
这次选择有没有改变已经确认的决定，以及这个改变要不要人负责。先核实事实，再定一个等级。
同一件事沾到多条时取最高等级。核实后仍不能证明「不改变已确认的决定」，就按 L1 提交，不能当成 L2 继续做。

L0：增加或推翻已确认的决定。必须写入决策记录，必须人工确认。正式文件是 docs/decisions.md；增量重跑时改写 .gdd/revision-stage/docs/decisions.md，不要改正式文件。包括：
- 对外契约：接口、字段、错误语义、事件、权限，以及给其他系统的承诺。
- 持久化形态：表、字段、类型、约束、迁移是否可逆、存量数据怎么处理。
- 复用还是新建。
- 兼容策略：旧调用方、旧数据、旧接口是否继续可用。
- 改写 docs/goal.md、docs/spec.md、docs/plan.md 或已有决策记录里已经确认的决定。

L1：目标仍然能达到，但做法偏离已确认决定，成本或风险有明显缺口。必须写入决策记录，必须人工确认。包括：
- 同步还是异步。
- 实效性：结果要多快被看到，允许的延迟上限。
- 一致性级别：强一致、最终一致，还是读己之写。
- 失败时用户能看到的行为、可靠性或运维负担，和已确认做法相比有明显缺口。

L2：已确认的目标、对外契约、数据形态、兼容策略、同步方式、实效性和一致性都不变。节点内自己决定，不提交定级。函数拆分、命名、目录、等价重构属于 L2。
环境问题和依赖问题先自己处理（例如 mock 掉缺的依赖）。只有缺权限、缺资料，或必须由人做外部动作时，才一次说明：失败点、已经试过什么、排除了什么、推荐路径、最少需要什么帮助。环境缺口不能记成通过。

方案开工前先扫描。只要出现 L0 或 L1 的选择，先追加到决策记录再往下设计。不在重跑时写 docs/decisions.md，重跑时写 .gdd/revision-stage/docs/decisions.md。决策记录只追加：等级、事实、对照的条款、备选、选定、原因。推翻旧决定时追加一条新记录指向旧条目，不改旧行。

若判定为 L0 或 L1，在回复最末一行单独写出（其后不要再写别的内容）：
GDD-GRADE: {"level":"L0 或 L1","clause":"对照的条款","fact":"核实到的事实","basis":"已确认文档里的原决定"}
L2 不要输出这一行。"""

RERUN_BLOCK = """[[GDD-RERUN]]
这是增量重跑，不是第一次执行。
不要清空上一轮产物，在原文件的内容上修订。
不要直接修改这些基线文件：docs/goal.md、docs/spec.md、docs/plan.md、docs/decisions.md。
把修订后的全文写到 .gdd/revision-stage/ 下的同名相对路径（例如 .gdd/revision-stage/docs/goal.md）。
决策记录的修订稿必须保留已有条目，只在末尾追加。
上面契约里写到的 docs/goal.md、docs/spec.md、docs/plan.md、docs/decisions.md，这次一律改到 .gdd/revision-stage/ 下的同名路径。
入口条件和出口条件都会重新验证。需要人工审批的节点，重跑之后仍然要审批。"""

ADVERSARIAL_INTRO = """[[GDD-ROLE:adversarial node:{node_id}]]
你是独立于测试主体的对抗会话。不要复用测试主体的结论，也不要重跑测试。
阅读 docs/acceptance.feature（Gherkin）和 docs/verify.md。
verify.md 里每个用例应能复查：使用的数据、过程日志、落盘数据。
对照 Gherkin 判定每个用例。证据支撑不了结论、或环境缺口被记成通过，都判 fail。
cause 只能是 code、test、spec、env 之一：
- code：实现没有满足用例
- test：用例执行、数据或记录本身有问题
- spec：goal/spec/plan 或已确认的决定写错了
- env：环境或依赖导致跑不起来；能自己解决的应该已经解决（例如 mock），不应被记成通过
在回复最末一行单独写出，不要把说明里的示例原样抄进结果：
GDD-ADVERSARIAL: {"cases":[每条用例的 id、result、cause、reason]}
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(root: str, rel: str) -> str:
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return ""
    with open(path, "rb") as f:
        return _sha(f.read())


def outside_progress(state: Dict, outside_ids: List[str]) -> str:
    """开发阶段及之后的节点相位。重跑自己推进的阶段不在这里。"""
    nodes = state.get("node_states") or {}
    snap = {}
    for nid in outside_ids:
        item = nodes.get(nid) or {}
        snap[nid] = item.get("phase")
    raw = json.dumps(snap, ensure_ascii=False, sort_keys=True)
    return _sha(raw.encode("utf-8"))


def _copy(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def snapshot_baseline(root: str, state: Dict, outside_ids: List[str]) -> Dict:
    """把当前四份产物存成基线，并记下开发阶段之外的进度指纹。"""
    files = {}
    for rel in FINGERPRINT_FILES:
        files[rel] = file_hash(root, rel)
        src = os.path.join(root, rel)
        dst = os.path.join(root, BASELINE_ROOT, rel)
        if os.path.isfile(src):
            _copy(src, dst)
        elif os.path.isfile(dst):
            os.remove(dst)
    return {"files": files, "progress": outside_progress(state, outside_ids)}


def baseline_matches(root: str, state: Dict, baseline: Dict, outside_ids: List[str]) -> bool:
    files = baseline.get("files") or {}
    for rel in FINGERPRINT_FILES:
        if file_hash(root, rel) != files.get(rel, ""):
            return False
    return outside_progress(state, outside_ids) == baseline.get("progress")


def _appended(old: bytes, new: bytes) -> bool:
    """新文本必须原样保留旧文本。旧文本不以换行结束时，不能接着旧的最后一行往下写。"""
    if new == old or not old:
        return True
    if not new.startswith(old):
        return False
    if old.endswith(b"\n"):
        return True
    return new[len(old):].startswith(b"\n")


def staged_decisions_ok(root: str) -> bool:
    """决策记录只允许在已确认文本之后追加。没有修订稿或没有旧记录时不拦。"""
    base = os.path.join(root, BASELINE_ROOT, "docs", "decisions.md")
    stage = os.path.join(root, STAGE_ROOT, "docs", "decisions.md")
    if not os.path.isfile(stage) or not os.path.isfile(base):
        return True
    with open(base, "rb") as f:
        old = f.read()
    with open(stage, "rb") as f:
        new = f.read()
    return _appended(old, new)


def live_decisions_rewritten(root: str) -> bool:
    """正式决策记录相对基线副本改了旧内容，而不是只在后面追加。"""
    base = os.path.join(root, BASELINE_ROOT, "docs", "decisions.md")
    live = os.path.join(root, "docs", "decisions.md")
    if not os.path.isfile(base):
        return False
    with open(base, "rb") as f:
        old = f.read()
    if not old:
        return False
    if not os.path.isfile(live):
        return True
    with open(live, "rb") as f:
        new = f.read()
    return not _appended(old, new)


def restore_decisions(root: str) -> None:
    """把正式决策记录退回基线副本。"""
    rel = "docs/decisions.md"
    src = os.path.join(root, BASELINE_ROOT, rel)
    dst = os.path.join(root, rel)
    if os.path.isfile(src):
        _copy(src, dst)
    elif os.path.isfile(dst):
        os.remove(dst)


def clear_stage(root: str) -> None:
    """丢掉暂存稿。基线已换或新一轮重跑开始时，旧稿不能再盖到正式文件上。"""
    for rel in FINGERPRINT_FILES:
        path = os.path.join(root, STAGE_ROOT, rel)
        if os.path.isfile(path):
            os.remove(path)


def apply_staged(root: str) -> List[str]:
    """基线未变时，把暂存的修订稿复制到正式路径。返回实际复制的相对路径。"""
    copied = []
    for rel in FINGERPRINT_FILES:
        src = os.path.join(root, STAGE_ROOT, rel)
        if not os.path.isfile(src):
            continue
        _copy(src, os.path.join(root, rel))
        copied.append(rel)
    return copied


def restore_baseline_files(root: str, baseline: Dict) -> None:
    """签批拒绝时，把正式产物退回上次确认的基线。"""
    files = baseline.get("files") or {}
    for rel, digest in files.items():
        dst = os.path.join(root, rel)
        src = os.path.join(root, BASELINE_ROOT, rel)
        if not digest:
            if os.path.isfile(dst):
                os.remove(dst)
            continue
        if os.path.isfile(src):
            _copy(src, dst)


def parse_grade(text: str) -> Optional[dict]:
    """取最后一条合法的 GDD-GRADE。没有或等级不是 L0/L1/L2 则返回 None。"""
    found = None
    for match in re.finditer(r"^GDD-GRADE:\s*(\{.*\})\s*$", text or "", re.M):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("level") in ("L0", "L1", "L2"):
            found = data
    return found


def parse_adversarial(text: str) -> Optional[dict]:
    """取最后一条带 cases 列表的 GDD-ADVERSARIAL。"""
    found = None
    for match in re.finditer(r"^GDD-ADVERSARIAL:\s*(\{.*\})\s*$", text or "", re.M):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        cases = data.get("cases") if isinstance(data, dict) else None
        if isinstance(cases, list):
            found = data
    return found


def normalize_cases(cases: List[dict]) -> List[dict]:
    """环境缺口被写成通过时，改记为失败。"""
    out = []
    for c in cases:
        if not isinstance(c, dict):
            continue
        item = dict(c)
        if item.get("cause") == "env" and item.get("result") == "pass":
            item["result"] = "fail"
            reason = str(item.get("reason") or "").strip()
            item["reason"] = (reason + " " if reason else "") + "环境缺口不能记成通过"
        out.append(item)
    return out


def route_cases(cases: List[dict]) -> str:
    """spec 优先，其次 code，其余留在测试环节。没有失败则 pass。"""
    fails = [c for c in cases if isinstance(c, dict) and c.get("result") != "pass"]
    if not fails:
        return "pass"
    if any(c.get("cause") == "spec" for c in fails):
        return "spec"
    if any(c.get("cause") == "code" for c in fails):
        return "code"
    return "test"


def failed_cases(cases: List[dict], causes: Optional[set] = None) -> List[dict]:
    out = []
    for c in cases:
        if not isinstance(c, dict) or c.get("result") == "pass":
            continue
        if causes is not None and c.get("cause") not in causes:
            continue
        out.append(c)
    return out


def format_failed(cases: List[dict]) -> str:
    lines = []
    for c in cases:
        lines.append(f"- [{c.get('cause')}] {c.get('id')}: {c.get('reason')}")
    return "\n".join(lines)


def revision_range(nodes: List) -> List:
    """从需求澄清到开发阶段之前。开发阶段是 baseline_marker 或 id 为 implement 的节点。"""
    start = next((i for i, n in enumerate(nodes) if n.id == "clarify"), None)
    end = next((i for i, n in enumerate(nodes)
                if n.baseline_marker or n.id == "implement"), None)
    if start is None or end is None or start >= end:
        return []
    return list(nodes[start:end])
