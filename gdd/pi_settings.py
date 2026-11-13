"""pi 执行端的全局设置（端点 / API key / 模型）。

持久化在用户级 `~/.gdd/pi-settings.json`（目录 0700、文件 0600、原子写入）；
测试可用 `GDD_PI_SETTINGS` 指定完整文件路径。GET 只返回脱敏视图，永不回显明文 key。

优先级（spec 3.7）：`GDD_AGENT_MODEL` > 设置页模型；baseUrl/apiKey 只来自本文件。
"""
from __future__ import annotations

import json
import os
import stat
import time
import urllib.error
import urllib.request
from typing import Dict, Optional

VERSION = 1
SETTINGS_ENV = "GDD_PI_SETTINGS"
DEFAULTS = {"version": VERSION, "baseUrl": "", "apiKey": "", "model": ""}


class SettingsError(ValueError):
    """设置不可读/不合法（消息可直接展示给用户）。"""


class SettingsTestError(RuntimeError):
    """端点连通性测试失败。"""


def settings_path() -> str:
    override = os.environ.get(SETTINGS_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".gdd", "pi-settings.json")


def _private_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _atomic_write(path: str, text: str) -> None:
    _private_dir(os.path.dirname(path))
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
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


def _normalize(data: dict) -> dict:
    out = dict(DEFAULTS)
    out["version"] = VERSION
    for key in ("baseUrl", "apiKey", "model"):
        value = data.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise SettingsError(f"设置字段 {key} 必须是字符串")
        out[key] = value.strip() if key != "apiKey" else value
    return out


def load() -> dict:
    """读取设置。文件不存在返回空默认值；无法解析时抛出可展示的错误。"""
    path = settings_path()
    if not os.path.isfile(path):
        return dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeError) as e:
        raise SettingsError(
            f"pi 设置文件无法读取：{path}（{e}）。请打开 Web UI 设置页重新保存") from e
    if not isinstance(raw, dict):
        raise SettingsError(
            f"pi 设置文件格式非法：{path}。请打开 Web UI 设置页重新保存")
    try:
        return _normalize(raw)
    except SettingsError:
        raise
    except Exception as e:  # pragma: no cover - 防御未知形态
        raise SettingsError(f"pi 设置文件格式非法：{path}（{e}）") from e


def validate(base_url: str, model: str) -> None:
    url = (base_url or "").strip()
    if not url:
        raise SettingsError("端点（Base URL）必填")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise SettingsError("端点仅支持 http/https URL")
    if any(c in url for c in "\n\r\x00"):
        raise SettingsError("端点不能包含换行或控制字符")
    # 结构化校验：拒绝 "http://" 之类没有主机的形态
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SettingsError("端点不是合法 URL")
    if not (model or "").strip():
        raise SettingsError("模型必填")


def save(base_url: str, model: str, api_key: Optional[str] = None) -> dict:
    """保存设置。api_key 为 None/空串表示保留已保存的 key。"""
    validate(base_url, model)
    current = load()
    key = current.get("apiKey", "")
    if api_key:
        key = str(api_key)
    data = {
        "version": VERSION,
        "baseUrl": (base_url or "").strip(),
        "apiKey": key,
        "model": (model or "").strip(),
    }
    _atomic_write(settings_path(),
                  json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


def public_view(data: Optional[dict] = None) -> Dict[str, object]:
    current = load() if data is None else data
    key = str(current.get("apiKey") or "")
    return {
        "version": VERSION,
        "baseUrl": str(current.get("baseUrl") or ""),
        "model": str(current.get("model") or ""),
        "hasApiKey": bool(key),
        "apiKeyMasked": mask_key(key),
    }


def mask_key(key: str) -> str:
    key = key or ""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def resolve_model(data: Optional[dict] = None) -> str:
    override = (os.environ.get("GDD_AGENT_MODEL") or "").strip()
    if override:
        return override
    current = load() if data is None else data
    return str(current.get("model") or "").strip()


def ready_error(data: dict) -> str:
    """缺少必要设置时返回可处置的错误；就绪返回空串。"""
    base = str(data.get("baseUrl") or "").strip()
    model = resolve_model(data)
    if not base or not model:
        return ("pi 执行端尚未配置：请打开 Web UI 设置页配置端点（Base URL）、"
                "API key 和模型后重试")
    return ""


def _redact(text: str, api_key: str) -> str:
    body = text or ""
    if api_key:
        body = body.replace(api_key, "****")
    return body[:500]


def test_endpoint(base_url: str, api_key: str, model: str,
                  timeout: int = 60) -> dict:
    """向 OpenAI 兼容端点发一条最小 chat/completions 请求。"""
    validate(base_url, model)
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(2000).decode("utf-8", "replace")
        except OSError:
            pass
        msg = _redact(f"HTTP {e.code}: {body or e.reason}", api_key)
        raise SettingsTestError(msg) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SettingsTestError(_redact(f"连接失败: {e}", api_key)) from e
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SettingsTestError(_redact(f"响应不是 JSON: {raw[:200]}", api_key)) from e
    sample = ""
    try:
        content = doc["choices"][0]["message"].get("content")
        if isinstance(content, list):
            content = "".join(str(x.get("text", "")) for x in content
                              if isinstance(x, dict))
        if isinstance(content, str):
            sample = content[:200]
    except (KeyError, IndexError, TypeError):
        sample = raw[:200]
    return {"model": model, "sample": sample}


def file_mode(path: Optional[str] = None) -> int:
    p = path or settings_path()
    try:
        return stat.S_IMODE(os.stat(p).st_mode)
    except OSError:
        return 0
