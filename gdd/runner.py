"""本地 agent 适配器。执行端由 GDD_AGENT 选择，默认 pi。

每次调用 = 一个会话（entry 门禁 / exit 门禁 / 节点主体 互相隔离）。
协议在 gdd/agents 里，一种执行端一个类。这里只负责本机进程：
拉起、超时、杀进程组、把提示词写入 stdin。
pi：本机进程，模型走设置页的 OpenAI 兼容端点，续跑用 --session-id。
claude：本机 `claude -p` headless，使用本机登录态，续跑用 --resume。
不在两种协议之间自动回退。

环境变量：
  GDD_AGENT                pi 或 claude。未设置时看 GDD_AGENT_BIN 的文件名：
                           以 claude 开头则是 claude，否则 pi
  GDD_AGENT_BIN            该执行端的可执行文件。pi 未设置时找 node_modules/.bin/pi
                           再找 PATH 上的 pi；claude 未设置时用 PATH 上的 claude
  GDD_AGENT_PRE_ARGS       插在 bin 之后、协议参数之前（shlex 切分）
  GDD_AGENT_MODEL          pi：覆盖设置页模型。claude：传给 --model
  GDD_AGENT_EXTRA_ARGS     附加 CLI 参数（shlex 切分）
  GDD_AGENT_TIMEOUT        单会话超时秒数，默认 7200
  GDD_PI_AGENT_DIR         仅 pi：agent 目录覆盖
  GDD_PI_SESSION_DIR       仅 pi：会话目录覆盖
  GDD_SUBJECT_RESUME=off   不复用会话
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import pi_settings

# 流式读行：stdout.readline 阻塞时墙钟超时仍须生效
_STREAM_EOF = object()
_STREAM_POLL_S = 0.5


def _drain_line_queue(line_q: "queue.Queue", out_lines: List[str]) -> None:
    """把读线程已经放入、主循环还没取走的行并进 out_lines。异常和 EOF 标记跳过。"""
    while True:
        try:
            item = line_q.get_nowait()
        except queue.Empty:
            return
        if item is _STREAM_EOF or isinstance(item, BaseException):
            continue
        out_lines.append(item)


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
    # turn 预算打满：会话本身仍存活、可 --session-id 续跑（区别于崩溃/凭证失效）。
    turns_exhausted: bool = False
    streamed: bool = False  # 本轮是否走流式呈现路径
    config_error: bool = False  # 节点 MCP/skill/设置加载失败：不是会话失效
    # 会话指标：工具调用按调用 id 去重、LLM 次数按模型响应轮次、token 四项分量。
    # token_status=unavailable 表示产生了模型响应但 usage 缺失或全 0（真实响应必有消耗）。
    tool_calls: int = 0
    llm_calls: int = 0
    token_input: int = 0
    token_output: int = 0
    token_cache_creation: int = 0
    token_cache_read: int = 0
    token_status: str = "available"


# 凭证失效特征（小写匹配）。pi/端点错误可能只出现在 stderr 或 assistant 错误消息里。
_AUTH_HINTS = (
    "401", "403", "unauthorized", "invalid api key", "invalid_api_key",
    "authentication", "no valid credentials", "session expired",
)

def _auth_hint(*texts: str) -> str:
    for t in texts:
        low = (t or "").lower()
        for h in _AUTH_HINTS:
            if h in low:
                return h
    return ""


def _as_text(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data


def _merge_captured(already: str, reread) -> str:
    """合并超时快照和再次 communicate 的返回（兼容旧调用方/单测）。"""
    extra = _as_text(reread)
    if not extra:
        return already
    if not already or extra.startswith(already):
        return extra
    return already + extra


def _tool_summary(name: str, inp: Any) -> str:
    """从 pi 工具参数抽一行摘要（路径 / 命令 / 模式）。"""
    if not isinstance(inp, dict):
        return ""
    name_l = (name or "").lower()
    if name_l in ("write", "edit", "read", "multiedit"):
        for k in ("path", "file_path", "file", "filePath"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:200]
    if name_l == "bash":
        for k in ("command", "cmd"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                one = " ".join(v.strip().split())
                return one[:160] + ("…" if len(one) > 160 else "")
    if name_l in ("grep", "find", "glob", "ls"):
        for k in ("pattern", "path", "glob", "query"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:160]
    return ""


def _usage_int(value: Any) -> int:
    """usage 字段转整数；缺失/非法按 0。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _token_status(total: int, saw_model_response: bool) -> str:
    """真实模型响应不可能零消耗：有响应但 usage 全 0/缺失时标不可得。"""
    if saw_model_response and total <= 0:
        return "unavailable"
    return "available"


class _SessionState:
    def __init__(self) -> None:
        self.session_id = ""
        self.turns = 0
        self.cost = 0.0
        self.last_text = ""
        self.error_message = ""
        self.aborted = False
        self.saw_event = False
        self.saw_agent_end = False
        self.text_streamed = False
        self.seen_tools: set = set()
        # T1 会话指标：工具调用、token 四分量、是否产生过模型响应
        self.tool_calls = 0
        self.token_input = 0
        self.token_output = 0
        self.token_cache_creation = 0
        self.token_cache_read = 0
        self.saw_model_response = False


class AgentRunner:
    def __init__(self, root: str):
        self.root = root
        self.bin = os.environ.get("GDD_AGENT_BIN", "")
        try:
            self.timeout = int(os.environ.get("GDD_AGENT_TIMEOUT", "7200"))
        except (TypeError, ValueError):
            self.timeout = 7200
        if self.timeout <= 0:
            self.timeout = 7200
        # 供 orchestrator 上报 tool_map_unknown（可选）
        self.on_event: Optional[Callable[..., None]] = None
        # 兼容旧属性：主体会话标记 / 节点扩展
        self.steer_subject = False
        self.node_extensions = None

    # ------------------------------------------------------------ 路径与命令

    def _agent_dir(self) -> str:
        override = os.environ.get("GDD_PI_AGENT_DIR")
        if override:
            return os.path.abspath(os.path.expanduser(override))
        from .steer import project_key
        return os.path.join(os.path.expanduser("~"), ".gdd", "pi-agent",
                            project_key(self.root))

    def _session_dir(self) -> str:
        override = os.environ.get("GDD_PI_SESSION_DIR")
        if override:
            return os.path.abspath(os.path.expanduser(override))
        from .steer import project_key
        return os.path.join(os.path.expanduser("~"), ".gdd", "pi-sessions",
                            project_key(self.root))

    @staticmethod
    def _private_dir(path: str) -> None:
        os.makedirs(path, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    @staticmethod
    def _write_private(path: str, text: str) -> None:
        # 并行任务会话共享同一 agent 目录：临时名必须唯一，否则并发 rename 竞速
        tmp = (f"{path}.{os.getpid()}.{threading.get_ident()}."
               f"{time.time_ns()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _models_doc(self, base_url: str, model: str) -> dict:
        return {
            "providers": {
                "custom": {
                    "name": "GDD Custom OpenAI-Compatible Endpoint",
                    "baseUrl": base_url,
                    "api": "openai-completions",
                    "models": [{
                        "id": model,
                        "name": model,
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": 128000,
                        "maxTokens": 16384,
                        "cost": {"input": 0, "output": 0,
                                 "cacheRead": 0, "cacheWrite": 0},
                    }],
                }
            }
        }

    def _prepare_agent_dir(self, base_url: str, api_key: str, model: str) -> str:
        agent_dir = self._agent_dir()
        self._private_dir(agent_dir)
        self._write_private(os.path.join(agent_dir, "models.json"),
                            json.dumps(self._models_doc(base_url, model),
                                       ensure_ascii=False, indent=2))
        # pi 对 custom provider 要求非空 key；设置页 API key 可选，
        # 未填时放占位值（无鉴权端点会忽略 Authorization，需鉴权的端点仍返回 401）。
        auth = {"custom": {"type": "api_key", "key": api_key or "gdd-no-key"}}
        self._write_private(os.path.join(agent_dir, "auth.json"),
                            json.dumps(auth, ensure_ascii=False, indent=2))
        mcp_target = os.path.join(agent_dir, "mcp.json")
        ext = getattr(self, "node_extensions", None)
        mcp_source = getattr(ext, "mcp_config", "") if ext is not None else ""
        if mcp_source and os.path.isfile(mcp_source):
            try:
                with open(mcp_source, encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                raise ValueError(f"MCP 配置无法读取: {mcp_source}（{e}）") from e
            self._write_private(mcp_target,
                                json.dumps(doc, ensure_ascii=False, indent=2))
        else:
            try:
                os.remove(mcp_target)
            except OSError:
                pass
        return agent_dir

    def _build_cmd(self, allowed_tools: List[str], max_turns: int,
                   resume_sid: str = "", *, stream: bool = False,
                   steer: bool = False) -> List[str]:
        """按 GDD_AGENT 构造命令。设置或二进制缺失时抛 ConfigError。"""
        from .agents import resolve_agent
        subject = bool(steer or getattr(self, "steer_subject", False))
        return resolve_agent().build_cmd(
            self, allowed_tools, max_turns, resume_sid,
            stream=stream, steer=subject)

    def _prepare_env(self, subject: bool, kind: str = "pi") -> dict:
        from .agents import adapter_for
        env = dict(os.environ)
        env.update(adapter_for(kind).extra_env(self, subject))
        if subject:
            from . import steer as steer_mod
            env["GDD_STEER_ROOT"] = os.path.abspath(self.root)
            env["GDD_STEER_QUEUE"] = steer_mod.queue_path(self.root)
            env["GDD_STEER_PY"] = sys.executable
            env["GDD_STEER_SCRIPT"] = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "steer.py")
        return env

    # ------------------------------------------------------------ 会话执行

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
        subject = bool(steer or getattr(self, "steer_subject", False))
        agent = None
        try:
            from .agents import resolve_agent
            agent = resolve_agent()
            agent.prepare(self)
            cmd = agent.build_cmd(
                self, allowed_tools, max_turns, resume_sid=resume_sid,
                stream=stream, steer=subject)
        except pi_settings.SettingsError as e:
            res = SessionResult(ok=False, error=str(e), config_error=True)
            self._dump_log(log_path, prompt, [], "", res)
            return res
        except ConfigError as e:
            res = SessionResult(ok=False, error=str(e), config_error=True)
            self._dump_log(log_path, prompt, [], "", res)
            return res
        except ValueError as e:
            res = SessionResult(ok=False, error=f"节点扩展配置失败: {e}",
                                config_error=True)
            self._dump_log(log_path, prompt, [], "", res)
            return res
        except OSError as e:
            who = agent.name if agent is not None else "本地"
            res = SessionResult(ok=False, error=f"{who} agent 目录准备失败: {e}",
                                config_error=True)
            self._dump_log(log_path, prompt, [], "", res)
            return res

        started = time.time()
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
                env=self._prepare_env(subject, agent.name),
                start_new_session=True,
            )
        except OSError as e:
            hint = agent.launch_hint()
            res = SessionResult(
                ok=False,
                error=f"执行端无法启动: {cmd[0]}（{e}；{hint}）",
                config_error=True)
            self._dump_log(log_path, prompt, cmd, "", res)
            return res

        res = self._run_process(proc, prompt, cmd, log_path, started, max_turns,
                                on_stream, agent)
        res.streamed = stream
        return res

    def _run_process(self, proc, prompt: str, cmd: List[str], log_path,
                     started: float, max_turns: int,
                     on_stream: Optional[Callable[[Dict[str, Any]], None]],
                     agent) -> SessionResult:
        err_chunks: List[str] = []
        line_q: "queue.Queue" = queue.Queue()
        out_lines: List[str] = []
        state = _SessionState()
        timed_out = False

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
        try:
            assert proc.stdin is not None and proc.stdout is not None
            err_thr.start()
            out_thr.start()
            proc.stdin.write(prompt)
            proc.stdin.close()

            while True:
                remaining = self.timeout - (time.time() - started)
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    item = line_q.get(timeout=min(remaining, _STREAM_POLL_S))
                except queue.Empty:
                    if proc.poll() is not None and line_q.empty():
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
                out_lines.append(item)
                raw = item.strip()
                if not raw.startswith("{"):
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                agent.on_event(obj, state, on_stream)
                if agent.should_stop(state, max_turns):
                    break
        except KeyboardInterrupt:
            self._kill(proc)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            raise
        except OSError as e:
            self._kill(proc)
            out_thr.join(timeout=2)
            err_thr.join(timeout=2)
            _drain_line_queue(line_q, out_lines)
            res = SessionResult(ok=False, error=f"执行端会话 IO 异常: {e}",
                                duration_s=time.time() - started)
            self._dump_log(log_path, prompt, cmd, "".join(out_lines), res)
            return res
        finally:
            self._kill(proc)
            err_thr.join(timeout=2)
            out_thr.join(timeout=2)
            _drain_line_queue(line_q, out_lines)

        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        out = "".join(out_lines)
        err = "".join(err_chunks)
        res = agent.parse(out, err, proc.returncode, state, max_turns=max_turns)
        res.duration_s = time.time() - started
        if on_stream and res.ok and not state.text_streamed and res.text:
            try:
                on_stream({"kind": "text", "text": res.text})
            except Exception:
                pass
        res = agent.apply_turn_cap(res, state, max_turns)
        if timed_out:
            # 超时前已观测到的会话指标仍有效，随结果保留（不编造未观测数据）。
            res = SessionResult(ok=False, error=f"会话超时（>{self.timeout}s）",
                                session_id=state.session_id,
                                duration_s=time.time() - started, raw=out,
                                tool_calls=res.tool_calls,
                                llm_calls=res.llm_calls,
                                token_input=res.token_input,
                                token_output=res.token_output,
                                token_cache_creation=res.token_cache_creation,
                                token_cache_read=res.token_cache_read,
                                token_status=res.token_status)
        self._dump_log(log_path, prompt, cmd, out, res)
        return res

    @staticmethod
    def _kill(proc) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    @staticmethod
    def _parse_output(stdout: str, stderr: str, returncode: int,
                      state: Optional[_SessionState] = None) -> SessionResult:
        """解析已累计的 pi JSONL 事件，给出会话结论。

        state 由流式循环实时累计；直接调用（单测）时从 stdout 重放构建。
        """
        from .agents.pi import PiAgent
        return PiAgent().parse(stdout, stderr, returncode, state, max_turns=0)

    @staticmethod
    def _parse_claude_output(stdout: str, stderr: str, returncode: int) -> SessionResult:
        """解析 claude -p 的 result JSON。stream-json 取最后一条 type=result。"""
        from .agents.claude import ClaudeAgent
        return ClaudeAgent().parse(stdout, stderr, returncode, None, max_turns=0)

    @staticmethod
    def _dump_log(log_path: Optional[str], prompt: str, cmd: List[str],
                  stdout: str, res: SessionResult) -> None:
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
            # T1 附加会话指标；既有字段语义不变。
            "tool_calls": res.tool_calls,
            "llm_calls": res.llm_calls,
            "token_input": res.token_input,
            "token_output": res.token_output,
            "token_cache_creation": res.token_cache_creation,
            "token_cache_read": res.token_cache_read,
            "token_status": res.token_status,
        }
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


class ConfigError(RuntimeError):
    """执行端配置缺失（二进制/设置/扩展），不是会话本身的失败。"""
