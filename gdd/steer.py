"""working 期间的补充信息。

队列放在仓库外（默认 ~/.gdd/steer/<项目哈希>/），gdd steer 不拿 .gdd/lock。
主体会话启动时挂上 PostToolUse：每次工具返回都跑 hook，队列空则无输出；
有补充则作为 additionalContext 交给模型并消费。本轮不再调用工具时，
编排器在会话成功后 --resume 补送一次。详见 docs/design-steer.md。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import sys
import time
from typing import List

# Claude Code 对 additionalContext 有长度上限（超出后只给预览）。
# hook 发出的是前缀加正文，两者一起停在 8000 以内；超出的正文留在队列。
# 会话结束时的 --resume 补送写进 prompt，仍按 8000 取正文。
MAX_CHARS = 8000
_DRAIN_LIMIT = 8000
_CONTEXT_PREFIX = (
    "用户在主体执行过程中补充了信息（不是门禁结论，按此调整后续工作）：\n"
)


def steer_home() -> str:
    return os.environ.get("GDD_STEER_HOME") or os.path.join(
        os.path.expanduser("~"), ".gdd", "steer")


def project_key(root: str) -> str:
    real = os.path.realpath(os.path.abspath(root))
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]


def _dir(root: str) -> str:
    return os.path.join(steer_home(), project_key(root))


def queue_path(root: str) -> str:
    return os.path.join(_dir(root), "queue.jsonl")


def claude_settings_path(root: str) -> str:
    return os.path.join(_dir(root), "claude.settings.json")


def codex_hooks_path(root: str) -> str:
    return os.path.join(_dir(root), "hooks.json")


def agent_family(bin_path: str) -> str:
    """执行端家族。只有 Claude 的命令行接受 --settings；Codex 用 hooks.json。"""
    name = os.path.basename(bin_path or "").lower()
    if name.startswith("claude"):
        return "claude"
    if name.startswith("codex"):
        return "codex"
    return "other"


def enabled() -> bool:
    return os.environ.get("GDD_STEER", "on") != "off"


def format_context(body: str) -> str:
    return _CONTEXT_PREFIX + body


def hook_command(root: str) -> str:
    script = os.path.abspath(__file__)
    return " ".join([
        shlex.quote(sys.executable),
        shlex.quote(script),
        "hook",
        shlex.quote(queue_path(root)),
    ])


def _hooks_document(root: str) -> dict:
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command(root),
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
    }


def prepare(root: str) -> None:
    """写出 Claude settings 与 Codex hooks.json。两者指向同一 hook 命令。"""
    os.makedirs(_dir(root), exist_ok=True)
    doc = json.dumps(_hooks_document(root), ensure_ascii=False, indent=2)
    for path in (claude_settings_path(root), codex_hooks_path(root)):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(doc)
        os.replace(tmp, path)


def _lock(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = open(path + ".lock", "a+")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _unlock(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        fd.close()


def _read_items(path: str) -> List[dict]:
    if not os.path.isfile(path):
        return []
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str) and text.strip():
                items.append({"ts": item.get("ts"), "text": text.strip()})
    return items


def _write_items(path: str, items: List[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def restore(root: str, text: str) -> int:
    """把已取出的原文放回队首（补送失败时用，避免排到后写入的补充后面）。"""
    body = (text or "").strip()
    if not body:
        return 0
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS]
    path = queue_path(root)
    fd = _lock(path)
    try:
        items = _read_items(path)
        items.insert(0, {"ts": time.time(), "text": body})
        _write_items(path, items)
        return len(items)
    finally:
        _unlock(fd)


def phase_hint(root: str) -> str:
    """磁盘上的 status/phase。读不到或不在 working 时返回非空提示；working 返回空串。"""
    path = os.path.join(os.path.abspath(root), ".gdd", "state.json")
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeError):
        return ""
    if not isinstance(st, dict):
        return ""
    status = str(st.get("status") or "")
    cur = str(st.get("current") or "")
    ns = (st.get("node_states") or {}).get(cur) or {}
    phase = str(ns.get("phase") or "") if isinstance(ns, dict) else ""
    if status == "running" and phase == "working":
        return ""
    if not status and not phase:
        return ""
    return f"{status}/{phase}" if phase else status


def delivery_hint(root: str) -> str:
    """入队后的送达说明。命令行和 Web 控制台共用这一句。"""
    if subject_active(root):
        tail = "主体会话下一次工具返回时送入模型"
        if os.environ.get("GDD_SUBJECT_RESUME", "on") == "off":
            tail += "。当前 GDD_SUBJECT_RESUME=off，本轮若不再调用工具则不会在会话结束时补送"
        else:
            tail += "；若本轮不再调用工具，会话结束时补送一次"
        return f"已排队。{tail}。"
    hint = phase_hint(root)
    where = f"（当前 {hint}）" if hint else ""
    return f"已排队{where}。主体会话此刻不在运行，将在下一次主体会话送达。"


def append(root: str, text: str) -> int:
    """追加一条补充。返回队列中的条数。不拿 .gdd/lock。"""
    body = (text or "").strip()
    if not body:
        raise ValueError("补充内容为空")
    if len(body) > MAX_CHARS:
        raise ValueError(f"消息过长（≤{MAX_CHARS} 字符）")
    path = queue_path(root)
    fd = _lock(path)
    try:
        items = _read_items(path)
        items.append({"ts": time.time(), "text": body})
        _write_items(path, items)
        return len(items)
    finally:
        _unlock(fd)


def pending(root: str) -> str:
    """未消费补充的原文（多条以换行拼接）。不消费。队列不存在时不创建目录。"""
    path = queue_path(root)
    if not os.path.isfile(path):
        return ""
    fd = _lock(path)
    try:
        return "\n".join(item["text"] for item in _read_items(path))
    finally:
        _unlock(fd)


def queued_count(root: str) -> int:
    path = queue_path(root)
    if not os.path.isfile(path):
        return 0
    fd = _lock(path)
    try:
        return len(_read_items(path))
    finally:
        _unlock(fd)


def mark_subject(root: str, active: bool) -> None:
    """主体进程是否还在跑。exit 门禁期间 phase 仍是 working，靠这个标记区分。"""
    path = os.path.join(_dir(root), "subject_active")
    if not active:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    try:
        os.makedirs(_dir(root), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("1\n")
    except OSError:
        pass


def subject_active(root: str) -> bool:
    return os.path.isfile(os.path.join(_dir(root), "subject_active"))


def drain(root: str, limit: int = _DRAIN_LIMIT) -> str:
    """取出不超过 limit 字符的补充原文并留下超出部分。空串表示没有。"""
    return _drain_path(queue_path(root), limit)


def _drain_path(path: str, limit: int = _DRAIN_LIMIT) -> str:
    fd = _lock(path)
    try:
        items = _read_items(path)
        taken: List[str] = []
        size = 0
        rest: List[dict] = []
        for i, item in enumerate(items):
            text = item["text"]
            extra = len(text) if not taken else len(text) + 1
            if taken and size + extra > limit:
                rest = items[i:]
                break
            if not taken and len(text) > limit:
                taken.append(text[:limit])
                leftover = text[limit:].strip()
                if leftover:
                    rest.append({"ts": item.get("ts"), "text": leftover})
                rest.extend(items[i + 1:])
                break
            taken.append(text)
            size += extra
        if taken:
            _write_items(path, rest)
        return "\n".join(taken)
    finally:
        _unlock(fd)


def hook_main(queue_file: str) -> int:
    """PostToolUse 入口。必须读完 stdin，避免工具结果管道写满阻塞。"""
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        body = _drain_path(queue_file, limit=MAX_CHARS - len(_CONTEXT_PREFIX))
    except Exception:
        return 0
    if not body:
        return 0
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": format_context(body),
        }
    }, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def take_main(root: str) -> int:
    """pi 扩展入口：取出一条补充的格式化上下文；空队列无输出。"""
    try:
        body = drain(root)
    except Exception:
        return 0
    if body:
        sys.stdout.write(format_context(body))
        sys.stdout.write("\n")
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[0] == "hook":
        return hook_main(args[1])
    if len(args) >= 2 and args[0] == "take":
        return take_main(args[1])
    print("usage: steer.py hook <queue> | take <root>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
