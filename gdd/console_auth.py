"""控制台登录鉴权的凭据存储与进程内会话。

凭据持久化在用户级 `~/.gdd/console-auth.json`（目录 0700、文件 0600、原子写入）；
测试/部署可用 `GDD_CONSOLE_AUTH` 指定完整文件路径。密码只保存为
PBKDF2-HMAC-SHA256 加盐哈希，文件、异常与日志中都不出现明文。

会话保存在进程内字典（带锁），绝对过期；TTL 由 `GDD_UI_SESSION_TTL` 覆盖，
默认 28800 秒（8 小时）。凭据文件损坏/不可读时抛出 `ConsoleAuthError`，
调用方必须给出可处置错误并拒绝提供控制台，不得静默退回无鉴权模式。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Callable, Dict, Optional

VERSION = 1
AUTH_ENV = "GDD_CONSOLE_AUTH"
SESSION_TTL_ENV = "GDD_UI_SESSION_TTL"

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "gdd2026"
DEFAULT_ITERATIONS = 600_000
DEFAULT_SESSION_TTL = 28_800

ALGO = "pbkdf2_sha256"
SALT_BYTES = 16
HASH_BYTES = 32          # sha256
MAX_ITERATIONS = 10_000_000


class ConsoleAuthError(ValueError):
    """凭据/会话配置不可用（消息可直接展示给用户，不含密码、哈希或盐）。"""


def auth_path() -> str:
    """凭据文件完整路径：`GDD_CONSOLE_AUTH` 优先，否则 `~/.gdd/console-auth.json`。"""
    override = os.environ.get(AUTH_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".gdd", "console-auth.json")


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


def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS,
                  salt: Optional[bytes] = None) -> dict:
    """生成密码哈希记录；salt 仅供测试复现，正常使用随机 16 字节。"""
    if not isinstance(password, str):
        raise ConsoleAuthError("密码必须是字符串")
    if iterations < 1 or iterations > MAX_ITERATIONS:
        raise ConsoleAuthError(
            f"PBKDF2 迭代次数必须在 1..{MAX_ITERATIONS} 之间（实际 {iterations}）")
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    elif len(salt) != SALT_BYTES:
        raise ConsoleAuthError(f"盐必须是 {SALT_BYTES} 字节")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {
        "algo": ALGO,
        "iterations": iterations,
        "salt": salt.hex(),
        "hash": digest.hex(),
    }


def _decode_hex(value: object, field: str, length: Optional[int] = None) -> bytes:
    if not isinstance(value, str) or not value:
        raise ConsoleAuthError(f"凭据文件格式非法：password.{field} 必须是非空十六进制字符串")
    try:
        raw = bytes.fromhex(value)
    except ValueError as e:
        raise ConsoleAuthError(
            f"凭据文件格式非法：password.{field} 不是合法十六进制") from e
    if length is not None and len(raw) != length:
        raise ConsoleAuthError(
            f"凭据文件格式非法：password.{field} 长度应为 {length} 字节")
    return raw


def _check_record(record: object) -> dict:
    """校验哈希记录结构，返回规范化的副本；不合法时抛可处置错误。"""
    if not isinstance(record, dict):
        raise ConsoleAuthError("凭据文件格式非法：password 必须是对象")
    algo = record.get("algo")
    if algo != ALGO:
        raise ConsoleAuthError(f"凭据文件使用了不支持的密码算法：{algo!r}（当前仅支持 {ALGO}）")
    iterations = record.get("iterations")
    if not isinstance(iterations, int) or isinstance(iterations, bool):
        raise ConsoleAuthError("凭据文件格式非法：password.iterations 必须是整数")
    if iterations < 1 or iterations > MAX_ITERATIONS:
        raise ConsoleAuthError(
            f"凭据文件格式非法：password.iterations 必须在 1..{MAX_ITERATIONS} 之间")
    salt = _decode_hex(record.get("salt"), "salt", SALT_BYTES)
    digest = _decode_hex(record.get("hash"), "hash", HASH_BYTES)
    return {"algo": ALGO, "iterations": iterations, "salt": salt.hex(), "hash": digest.hex()}


def verify_password(password: str, record: object) -> bool:
    """按记录中的算法/盐/迭代次数校验密码；结构非法时抛可处置错误。"""
    if not isinstance(password, str):
        return False
    checked = _check_record(record)
    try:
        encoded = password.encode("utf-8")
    except UnicodeError:
        # 孤立代理项等无法编码为 UTF-8 的输入不可能匹配任何按 UTF-8 派生的哈希。
        return False
    computed = hashlib.pbkdf2_hmac(
        "sha256", encoded, bytes.fromhex(checked["salt"]), checked["iterations"])
    return hmac.compare_digest(computed.hex(), checked["hash"])


def _check_document(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ConsoleAuthError("凭据文件格式非法：顶层必须是 JSON 对象")
    version = raw.get("version")
    if version != VERSION:
        raise ConsoleAuthError(
            f"凭据文件版本不受支持：{version!r}（当前支持 version={VERSION}）")
    username = raw.get("username")
    if not isinstance(username, str) or not username.strip():
        raise ConsoleAuthError("凭据文件格式非法：username 必须是非空字符串")
    password = _check_record(raw.get("password"))
    return {"version": VERSION, "username": username.strip(), "password": password}


class ConsoleAuth:
    """凭据文件读写与登录校验。"""

    def __init__(self, path: Optional[str] = None,
                 iterations: int = DEFAULT_ITERATIONS) -> None:
        self._path = os.path.abspath(os.path.expanduser(path)) if path else auth_path()
        self.iterations = iterations

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> dict:
        """读取并校验凭据文件；不存在或损坏时抛出可处置错误。"""
        if not os.path.exists(self._path):
            raise ConsoleAuthError(
                f"凭据文件不存在：{self._path}。请以鉴权模式启动控制台以初始化默认账号")
        if not os.path.isfile(self._path):
            raise ConsoleAuthError(
                f"凭据路径不是普通文件：{self._path}。请删除或改为普通文件后重新初始化")
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeError) as e:
            raise ConsoleAuthError(
                f"凭据文件无法读取：{self._path}（{e}）。"
                "请检查文件权限/内容，或删除该文件让控制台重新初始化默认账号") from e
        try:
            return _check_document(raw)
        except ConsoleAuthError as e:
            raise ConsoleAuthError(
                f"{e}（{self._path}）。请删除该文件让控制台重新初始化默认账号") from e

    def ensure_initialized(self) -> dict:
        """凭据文件不存在时用默认账号初始化；已存在时校验并返回。"""
        if os.path.exists(self._path):
            return self.load()
        record = {
            "version": VERSION,
            "username": DEFAULT_USERNAME,
            "password": hash_password(DEFAULT_PASSWORD, iterations=self.iterations),
        }
        document = _check_document(record)
        try:
            _atomic_write(self._path,
                          json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        except OSError as e:
            raise ConsoleAuthError(
                f"凭据文件无法写入：{self._path}（{e}）。请检查目录权限后重试") from e
        return document

    def verify(self, username: str, password: str) -> bool:
        """校验用户名/密码。用户名不存在与密码错误返回同一个 False。"""
        document = self.load()
        expected_user = document["username"]
        # 无论用户名是否匹配都执行一次哈希，避免用响应时间区分「用户不存在」。
        password_ok = verify_password(password, document["password"])
        try:
            given_user = str(username or "").encode("utf-8")
        except UnicodeError:
            return False
        user_ok = hmac.compare_digest(given_user, expected_user.encode("utf-8"))
        return bool(user_ok and password_ok)


def session_ttl() -> int:
    """会话绝对有效期（秒）：`GDD_UI_SESSION_TTL` 覆盖，默认 28800。"""
    raw = (os.environ.get(SESSION_TTL_ENV) or "").strip()
    if not raw:
        return DEFAULT_SESSION_TTL
    try:
        value = int(raw)
    except ValueError as e:
        raise ConsoleAuthError(
            f"{SESSION_TTL_ENV} 必须是正整数秒（实际 {raw!r}）") from e
    if value <= 0:
        raise ConsoleAuthError(f"{SESSION_TTL_ENV} 必须是正整数秒（实际 {value}）")
    return value


class SessionStore:
    """进程内会话表：token -> (username, 绝对过期时间)。线程安全。"""

    def __init__(self, ttl: Optional[int] = None,
                 now: Optional[Callable[[], float]] = None) -> None:
        self.ttl = session_ttl() if ttl is None else int(ttl)
        if self.ttl <= 0:
            raise ConsoleAuthError(f"会话有效期必须是正整数秒（实际 {self.ttl}）")
        self._now = now or time.time
        self._lock = threading.Lock()
        self._sessions: Dict[str, tuple] = {}

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = self._now() + self.ttl
        with self._lock:
            self._sessions[token] = (str(username or ""), expires_at)
        return token

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            item = self._sessions.get(token)
            if item is None:
                return False
            if self._now() >= item[1]:
                del self._sessions[token]
                return False
            return True

    def revoke(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def prune(self) -> int:
        """清理已过期会话，返回清理数量。"""
        now = self._now()
        with self._lock:
            expired = [t for t, (_, exp) in self._sessions.items() if now >= exp]
            for token in expired:
                del self._sessions[token]
            return len(expired)
