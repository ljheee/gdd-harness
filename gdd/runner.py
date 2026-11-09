"""本地 Agent 适配器（MVP：用户本地 Claude Code headless 会话）。

每次调用 = 一个会话（entry 门禁 / exit 门禁 / 节点主体 互相隔离）。
主体会话支持 --resume 复用（打回后继续上下文，见 docs/design-clarify-resume.md）。
可通过环境变量替换为任意兼容 CLI（用于测试的 stub 也走这条路）：

  GDD_AGENT_BIN            默认 claude
  GDD_AGENT_PRE_ARGS       插在 bin 之后、-p 之前的参数（shlex 切分）
  GDD_AGENT_MODEL          传给 --model（可选）
  GDD_AGENT_EXTRA_ARGS     附加 CLI 参数（shlex 切分）
  GDD_AGENT_TIMEOUT        单会话超时秒数，默认 7200（2 小时；重主体节点如 plan/implement 实跑可能超过 900s）

流式（docs/design-go.md §9）：传入 on_stream 时改用 --output-format stream-json
+ --verbose --include-partial-messages，按行回调 text/tool 事件；无回调保持 json。
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# 流式读行：stdout.readline 阻塞时墙钟超时仍须生效（与 buffered communicate 对齐）
_STREAM_EOF = object()
_STREAM_POLL_S = 0.5  # queue.get 上限：既要及时超时，又不要空转过猛


@dataclass
class SessionResult:
    ok: bool
    text: str = ""
    error: str = ""
    session_id: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    raw: str = field(default="", repr=False)
    # max-turns 打满：会话本身仍存活、可 --resume 续跑（区别于崩溃/凭证失效）。
    # 实跑教训：implement 80 turns 打满时 S4+ 尚未写盘，靠人工 retry+resume 续跑三轮
    # 才完成——见 orchestrator 的自动续跑逻辑。
    turns_exhausted: bool = False
    streamed: bool = False  # 本轮是否走了 stream-json 呈现路径


# 登录态/凭证失效特征（小写匹配）。实跑教训：claude CLI 登录态过期时把
# "Not logged in" 写进 stdout 正文而非 stderr，且可能不带 result JSON 包装，
# 引擎只能拿到「输出不可解析（stderr 为空）」这类空错误，定位全靠翻日志。
_AUTH_HINTS = (
    "not logged in", "please log in", "please login", "login required",
    "log in to", "authentication required", "session expired",
    "invalid api key", "no valid credentials", "unauthorized",
)


def _auth_hint(*texts: str) -> str:
    """在失败输出中找登录态失效特征；命中返回特征串，未命中返回空串。"""
    for t in texts:
        low = (t or "").lower()
        for h in _AUTH_HINTS:
            if h in low:
                return h
    return ""


def _auth_error(hint: str, bin_name: str) -> str:
    return (f"Agent CLI 登录态/凭证失效（命中特征 '{hint}'）。"
            f"请先完成 {bin_name} 登录或切换可用执行端，然后 gdd retry 续跑。")


def _agent_bin_name() -> str:
    # _parse_output 为 staticmethod，取不到实例 bin：从环境读同一默认值
    return os.environ.get("GDD_AGENT_BIN", "claude")


def _tool_summary(name: str, inp: Any) -> str:
    """从 tool_use input 抽一行摘要（路径 / 命令）。"""
    if not isinstance(inp, dict):
        return ""
    name_l = (name or "").lower()
    if name_l in ("write", "edit", "multiedit", "read", "notebookedit"):
        for k in ("file_path", "path", "filePath", "file"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:200]
    if name_l == "bash":
        for k in ("command", "cmd"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                one = " ".join(v.strip().split())
                return one[:160] + ("…" if len(one) > 160 else "")
    if name_l in ("grep", "glob"):
        for k in ("pattern", "path", "glob"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:120]
    return ""


def _dispatch_stream_line(obj: dict, on_stream: Callable[[Dict[str, Any]], None],
                          seen_tools: set) -> None:
    """把一条 NDJSON 事件归一为 text/tool 回调。异常吞掉——呈现层不得拖垮会话。"""
    try:
        t = obj.get("type")
        if t == "stream_event":
            ev = obj.get("event") or {}
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text") or ""
                if text:
                    on_stream({"kind": "text", "text": text})
            return
        if t == "assistant":
            msg = obj.get("message") or {}
            content = msg.get("content") or []
            if not isinstance(content, list):
                return
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    text = block.get("text") or ""
                    # 有 partial 时完整 assistant 文本会重复；仅在无 stream_event
                    # 已推送的场景兜底——用空块跳过：调用方若已收 text_delta，
                    # 完整块会显得重复。策略：assistant 文本块也推送，打印机
                    # 对 clarify 会重复；更好：仅当本行无伴随 partial 时推送。
                    # 简化：assistant 的完整 text 不推（靠 text_delta）；无
                    # include-partial 的代理仍可能只有 assistant——若 text 非空
                    # 且看起来像整段，推一次。用 seen 标记「本消息已有 delta」
                    # 太重。折中：assistant text 仅在没有 "partial" 标志时推送。
                    # Claude 的完整 assistant 消息在 partial 模式下仍会出现；
                    # 跳过 assistant text，只信 stream_event——stub 无 partial
                    # 时用 assistant text（见下方 stub 兼容：stub 发 stream_event）。
                    pass
                elif bt == "tool_use":
                    name = str(block.get("name") or "?")
                    tid = str(block.get("id") or name)
                    if tid in seen_tools:
                        continue
                    seen_tools.add(tid)
                    summary = _tool_summary(name, block.get("input"))
                    on_stream({"kind": "tool", "name": name, "summary": summary})
    except Exception:
        pass


def _dispatch_assistant_text_fallback(obj: dict, on_stream: Callable,
                                      text_emitted: list) -> None:
    """无 text_delta 时（或 stub）用完整 assistant 文本块兜底一次。"""
    if text_emitted[0]:
        return
    if obj.get("type") != "assistant":
        return
    try:
        content = (obj.get("message") or {}).get("content") or []
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        text = "".join(parts)
        if text:
            text_emitted[0] = True
            on_stream({"kind": "text", "text": text})
    except Exception:
        pass


class AgentRunner:
    def __init__(self, root: str):
        self.root = root
        self.bin = os.environ.get("GDD_AGENT_BIN", "claude")
        try:
            self.timeout = int(os.environ.get("GDD_AGENT_TIMEOUT", "7200"))
        except (TypeError, ValueError):
            self.timeout = 7200  # 非法配置回退默认，不让配置 typo 崩掉引擎
        if self.timeout <= 0:
            # 0/-5 等非正数原样透传会让每会话瞬间超时且报误导性「会话超时（>0s）」；
            # 与 GDD_CHECK_TIMEOUT 同口径（R3 已修）：非正值视为非法配置回退默认
            self.timeout = 7200

    def _build_cmd(self, allowed_tools: List[str], max_turns: int,
                   resume_sid: str = "", *, stream: bool = False,
                   steer: bool = False) -> List[str]:
        cmd = [self.bin]
        pre = os.environ.get("GDD_AGENT_PRE_ARGS", "")
        if pre:
            cmd += shlex.split(pre)
        fmt = "stream-json" if stream else "json"
        cmd += ["-p", "--output-format", fmt, "--max-turns", str(max_turns)]
        if stream:
            # verbose：工具/assistant 事件；include-partial-messages：text_delta
            cmd += ["--verbose", "--include-partial-messages"]
        if resume_sid:
            cmd += ["--resume", resume_sid]  # 续会话：历史上下文完整保留，本轮仅追加
        model = os.environ.get("GDD_AGENT_MODEL")
        if model:
            cmd += ["--model", model]
        if allowed_tools:
            cmd += ["--allowedTools", " ".join(allowed_tools)]
        extra = os.environ.get("GDD_AGENT_EXTRA_ARGS", "")
        if extra:
            cmd += shlex.split(extra)
        # 主体会话才挂。Claude 用 --settings 追加 hook，不替换用户自己的配置层；
        # Codex 的进程参数不是这套 -p 命令行，只把 hooks.json 写到仓库外供同一脚本使用。
        if steer or getattr(self, "steer_subject", False):
            from . import steer as steer_mod
            if steer_mod.enabled():
                try:
                    steer_mod.prepare(self.root)
                except OSError:
                    pass
                else:
                    if steer_mod.agent_family(self.bin) == "claude":
                        cmd += ["--settings", steer_mod.claude_settings_path(self.root)]
        ext = getattr(self, "node_extensions", None)
        # Codex 不吃 --mcp-config / --plugin-dir。其他 bin（含测试 stub）照挂，
        # 这样节点声明在命令审计日志里可见；真正执行仍以 Claude 为准。
        if ext is not None:
            from .steer import agent_family
            if agent_family(self.bin) != "codex":
                if getattr(ext, "mcp_config", ""):
                    cmd += ["--mcp-config", ext.mcp_config]
                for plugin in getattr(ext, "plugin_dirs", []) or []:
                    cmd += ["--plugin-dir", plugin]
        return cmd

    def run(
        self,
        prompt: str,
        *,
        allowed_tools: List[str],
        max_turns: int,
        log_path: Optional[str] = None,
        resume_sid: str = "",
        on_stream: Optional[Callable[[Dict[str, Any]], None]] = None,
        steer: bool = False,
    ) -> SessionResult:
        stream = on_stream is not None
        try:
            cmd = self._build_cmd(allowed_tools, max_turns, resume_sid=resume_sid,
                                  stream=stream, steer=steer)
        except ValueError as e:
            # GDD_AGENT_PRE_ARGS/EXTRA_ARGS 引号不合法等：会话失败而非引擎崩溃
            res = SessionResult(ok=False, error=f"Agent 参数解析失败: {e}（检查 GDD_AGENT_PRE_ARGS/EXTRA_ARGS）")
            self._dump_log(log_path, prompt, [], "", res)
            return res
        started = time.time()
        # P2-12：孙进程收编。子进程以新会话/进程组启动（start_new_session），
        # 超时 killpg 整组——subprocess.run 的 timeout 只 kill 直接子进程，
        # 孙进程（Agent 起的工具 bash 等）存活，实测 grandchild 在 run 返回
        # 后 1.2s+ 仍写文件，与后续机器检查/篡改恢复/交付提交竞速。
        # TimeoutExpired 在多数版本不带进程句柄，须 Popen 自持句柄。
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.root,
                start_new_session=True,
            )
        except OSError as e:
            # FileNotFoundError / EACCES（不可执行）/ 目录等：走会话失败 →
            # gate_error → retry → blocked 的既有容错链，不裸崩
            res = SessionResult(ok=False, error=f"Agent CLI 无法执行: {self.bin}（{e}；可用 GDD_AGENT_BIN 指定）")
            self._dump_log(log_path, prompt, cmd, "", res)
            return res

        if stream:
            return self._run_streaming(proc, prompt, cmd, log_path, started, on_stream)
        return self._run_buffered(proc, prompt, cmd, log_path, started)

    def _run_buffered(self, proc, prompt, cmd, log_path, started) -> SessionResult:
        try:
            out, err = proc.communicate(input=prompt, timeout=self.timeout)
        except KeyboardInterrupt:
            # start_new_session 使终端 SIGINT 够不着 agent 进程组——父进程
            # 收到 KI 后必须主动 killpg，否则孤儿会话继续写产物，retry 再起
            # 第二个 subject 并发。其余退出路径（超时/正常/OSError）均已 killpg。
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise
        except subprocess.TimeoutExpired as te:
            # P1-9（R6）：超时不必然是 agent 挂死——孙进程（dev server/watcher）
            # 继承 stdout 管道使 communicate 等不到 EOF；agent 本体可能已退出
            # 且 result JSON 已完整打印（te.output 持有已读到的 stdout）。
            # 先查 agent 本体：已退出 → 按已完成解析（不谎报超时、不丢结果、
            # 不白烧 2h）；未退出才是真超时。
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # 组长 pid = PGID（新会话）——先杀组再判活（已退出的组无成员，无害）
            except (ProcessLookupError, PermissionError, OSError):
                pass
            if proc.poll() is not None:
                # agent 已退出：结果已进管道（te.output / te.stderr），
                # 孙进程持管导致的 EOF 等待不应谎报为「会话超时」
                partial_out = te.output or ""
                partial_err = te.stderr or ""
                if isinstance(partial_out, bytes):
                    partial_out = partial_out.decode("utf-8", "replace")
                if isinstance(partial_err, bytes):
                    partial_err = partial_err.decode("utf-8", "replace")
                # 收尸（SIGKILL 后 communicate 快速返回）
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                res = self._parse_output(partial_out, partial_err, proc.returncode)
                if res.ok:
                    res.duration_s = time.time() - started
                    self._dump_log(log_path, prompt, cmd, partial_out, res)
                    return res
                # 输出不完整（agent 死于中途）：按真实形态报错，仍走会话失败链
            try:
                proc.communicate(timeout=5)  # 收尸（SIGKILL 后必然快速返回）
            except Exception:
                pass
            res = SessionResult(ok=False, error=f"会话超时（>{self.timeout}s）", duration_s=time.time() - started)
            self._dump_log(log_path, prompt, cmd, "", res)
            return res
        except OSError as e:
            # communicate 期 IO 异常（管道断裂等）：杀组防残留再走会话失败链
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            res = SessionResult(ok=False, error=f"Agent 会话 IO 异常: {e}")
            self._dump_log(log_path, prompt, cmd, "", res)
            return res

        # P1-1 缓解链：会话正常结束也收编残留孙进程（组长已退、组可能仍在）。
        # R6 端到端复现的攻击链：subject 用 nohup 延迟写 state.json，孙进程在
        # 会话正常结束后存活（此前 killpg 只在超时路径触发）——伪造
        # status=running + phase=exit_passed 被下一进程 run() 采信，节点从未
        # 过 exit 门禁却记 node_done。start_new_session 使孙进程与 agent 同
        # 组（nohup 只忽略 SIGHUP、不离组），正常退出 killpg 即断链。
        # 交付流水线语义下「会话结束后的残留进程」本身就是与机器检查/
        # 交付提交的竞速源（R6 P1-9 同根因），一律收编。
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass  # 组已空（无孙进程）：常态
        res = self._parse_output(out or "", err or "", proc.returncode)
        res.duration_s = time.time() - started
        self._dump_log(log_path, prompt, cmd, out or "", res)
        return res

    def _run_streaming(self, proc, prompt, cmd, log_path, started,
                       on_stream: Callable[[Dict[str, Any]], None]) -> SessionResult:
        """stream-json：写完 stdin 后按行读 stdout，实时回调，末尾解析 result。

        读行在独立线程：主循环用 queue.get(timeout) 守墙钟——stdout.readline
        在无换行时会永久阻塞，不能在其前后检查 time（与 buffered
        communicate(timeout=) 语义对齐，见 design-go §9.5）。
        """
        err_chunks: List[str] = []
        line_q: queue.Queue = queue.Queue()

        def _drain_err() -> None:
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    err_chunks.append(line)
            except Exception:
                pass

        def _read_stdout() -> None:
            try:
                assert proc.stdout is not None
                while True:
                    line = proc.stdout.readline()
                    if line == "":
                        line_q.put(_STREAM_EOF)
                        return
                    line_q.put(line)
            except Exception as e:
                line_q.put(e)

        err_thr = threading.Thread(target=_drain_err, daemon=True)
        out_thr = threading.Thread(target=_read_stdout, daemon=True)
        out_lines: List[str] = []
        seen_tools: set = set()
        text_emitted = [False]  # 是否已有 text_delta；无则 assistant 文本兜底

        try:
            assert proc.stdin is not None and proc.stdout is not None
            # 先起读线程再喂 stdin：readline 在 agent 吐数前阻塞无害；
            # finally 才能安全 join（未 start 的 Thread.join 会 RuntimeError）
            err_thr.start()
            out_thr.start()
            proc.stdin.write(prompt)
            proc.stdin.close()

            while True:
                remaining = self.timeout - (time.time() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, self.timeout)
                try:
                    item = line_q.get(timeout=min(remaining, _STREAM_POLL_S))
                except queue.Empty:
                    # 尚有预算：继续等；预算耗尽由下一轮 remaining<=0 触发
                    if proc.poll() is not None and line_q.empty():
                        # 进程已退且队列空：读线程应很快放 EOF；再等一小会儿
                        try:
                            item = line_q.get(timeout=0.2)
                        except queue.Empty:
                            break
                    else:
                        continue
                if item is _STREAM_EOF:
                    break
                if isinstance(item, BaseException):
                    raise item
                line = item
                out_lines.append(line)
                raw = line.strip()
                if not raw.startswith("{"):
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "stream_event":
                    ev = obj.get("event") or {}
                    delta = (ev.get("delta") or {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        text_emitted[0] = True
                _dispatch_stream_line(obj, on_stream, seen_tools)
                if not text_emitted[0]:
                    _dispatch_assistant_text_fallback(obj, on_stream, text_emitted)

            # 收尸等待
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        except KeyboardInterrupt:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            raise
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                # 尽量再读残片
                if proc.stdout:
                    rest = proc.stdout.read() or ""
                    if rest:
                        out_lines.append(rest)
                proc.wait(timeout=5)
            except Exception:
                pass
            out = "".join(out_lines)
            err = "".join(err_chunks)
            # agent 已退出且 out 可解析 → 当完成（同 buffered 路径）
            if proc.poll() is not None:
                res = self._parse_output(out, err, proc.returncode)
                if res.ok:
                    res.duration_s = time.time() - started
                    res.streamed = True
                    self._dump_log(log_path, prompt, cmd, out, res)
                    return res
            res = SessionResult(ok=False, error=f"会话超时（>{self.timeout}s）",
                                duration_s=time.time() - started, streamed=True)
            self._dump_log(log_path, prompt, cmd, out, res)
            return res
        except OSError as e:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            res = SessionResult(ok=False, error=f"Agent 会话 IO 异常: {e}", streamed=True)
            self._dump_log(log_path, prompt, cmd, "".join(out_lines), res)
            return res
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            err_thr.join(timeout=2)
            out_thr.join(timeout=2)

        out = "".join(out_lines)
        err = "".join(err_chunks)
        res = self._parse_output(out, err, proc.returncode)
        res.duration_s = time.time() - started
        res.streamed = True
        self._dump_log(log_path, prompt, cmd, out, res)
        return res

    @staticmethod
    def _parse_output(stdout: str, stderr: str, returncode: int) -> SessionResult:
        # claude -p --output-format json 输出单个 result JSON；
        # stream-json 末行 type=result 同形。兼容多行/前后噪声。
        candidates: List[dict] = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not (line.startswith("{") and '"result"' in line):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            # stream-json 中间事件也可能含 result 字段；优先 type=result
            if isinstance(d, dict) and d.get("type") == "result":
                candidates.append(d)
            elif isinstance(d, dict) and "result" in d and d.get("type") in (None, "result"):
                candidates.append(d)
        if not candidates:
            try:
                whole = json.loads(stdout or "")
                if isinstance(whole, dict):
                    candidates.append(whole)
            except json.JSONDecodeError:
                pass
        if candidates:
            d = candidates[-1]
            is_error = bool(d.get("is_error")) or d.get("subtype") not in (None, "success")
            turns_exhausted = (d.get("subtype") == "error_max_turns")

            def _to_int(v, default=0):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return default

            def _to_float(v, default=0.0):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return default

            res = SessionResult(
                ok=not is_error,
                text=str(d.get("result", "")),
                error="" if not is_error else str(d.get("result", ""))[:500],
                session_id=str(d.get("session_id", "")),
                turns=_to_int(d.get("num_turns", 0)),
                cost_usd=_to_float(d.get("total_cost_usd", 0.0)),
                raw=stdout or "",
                turns_exhausted=turns_exhausted,
            )
            # 失败会话的 result 正文里也可能藏着登录态失效信息（CLI 不总是给结构化错误）。
            # 长度守卫：正文很长说明是任务输出（可能引用接口 401/unauthorized 字样），
            # 仅短正文（CLI 级凭证错误通常几十字符）才判登录失效，防误报。
            if not res.ok and len(res.text) <= 300:
                hint = _auth_hint(res.text, stderr)
                if hint:
                    res.error = _auth_error(hint, _agent_bin_name())
            return res
        hint = _auth_hint(stdout, stderr) if len(stdout) <= 300 else ""
        if hint:
            # 输出不可解析且含登录失效特征（短正文：CLI 级凭证错误通常几十字符；
            # 长输出是任务输出，可能引用接口 401/unauthorized 字样，防误报）：
            # 给可处置的显式错误而非空 stderr 片段
            return SessionResult(ok=False, error=_auth_error(hint, _agent_bin_name()),
                                 raw=(stdout or "") + "\n" + (stderr or ""))
        return SessionResult(
            ok=False,
            error=f"Agent 输出不可解析（returncode={returncode}）stderr: {stderr[:500]}",
            raw=(stdout or "") + "\n" + (stderr or ""),
        )

    @staticmethod
    def _dump_log(log_path: Optional[str], prompt: str, cmd: List[str], stdout: str, res: SessionResult) -> None:
        if not log_path:
            return
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        payload = {
            "cmd": cmd,
            "prompt": prompt,
            "stdout": stdout,
            "ok": res.ok,
            "error": res.error,
            "session_id": res.session_id,
            "turns": res.turns,
            "turns_exhausted": res.turns_exhausted,
            "cost_usd": res.cost_usd,
            "duration_s": res.duration_s,
            "streamed": res.streamed,
        }
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
