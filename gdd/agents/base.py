"""本机命令行执行端。进程循环留在 AgentRunner，协议差异留在子类。"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


class AgentAdapter:
    """一种 GDD_AGENT。子类实现命令和结果解析，其余方法保持默认。"""

    name = ""

    @classmethod
    def match_bin(cls, base: str) -> bool:
        """未设置 GDD_AGENT 时，用可执行文件名认领。默认不认领。"""
        return False

    def prepare(self, runner) -> None:
        """进程启动前的磁盘准备。失败抛 ConfigError。"""

    def build_cmd(self, runner, allowed_tools: List[str], max_turns: int,
                  resume_sid: str = "", *, stream: bool = False,
                  steer: bool = False) -> List[str]:
        raise NotImplementedError

    def extra_env(self, runner, subject: bool) -> dict:
        return {}

    def on_event(self, obj: dict, state, on_stream: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """吃一行 JSON。异常在子类里吞掉，避免打断读循环。"""

    def should_stop(self, state, max_turns: int) -> bool:
        """读完一行后问要不要结束读取。轮数截断的改写放到 apply_turn_cap。"""
        return False

    def parse(self, stdout: str, stderr: str, returncode: int, state, *,
              max_turns: int):
        raise NotImplementedError

    def apply_turn_cap(self, res, state, max_turns: int):
        """全文补推之后调用。默认不改结果。"""
        return res

    def launch_hint(self) -> str:
        return "请确认本机可执行文件，或设置 GDD_AGENT_BIN"
