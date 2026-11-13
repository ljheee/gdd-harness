"""控制台凭据存储与会话：哈希、权限、环境变量、损坏处理、会话生命周期。"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gdd import console_auth  # noqa: E402
from gdd.console_auth import ConsoleAuth, ConsoleAuthError, SessionStore  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok", msg)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def capture(fn, *args, **kwargs):
    """执行 fn 并返回 (结果, stdout+stderr 文本)。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        result = fn(*args, **kwargs)
    return result, out.getvalue() + err.getvalue()


def expect_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ConsoleAuthError as e:
        return str(e)
    raise SystemExit("FAIL: 应抛出 ConsoleAuthError")


def git_tracks(path):
    """凭据路径是否被 git 索引命中。"""
    proc = subprocess.run(
        ["git", "-C", ROOT, "ls-files", "--error-unmatch", path],
        capture_output=True, text=True)
    return proc.returncode == 0


def main():
    old_auth_env = os.environ.get(console_auth.AUTH_ENV)
    old_ttl_env = os.environ.get(console_auth.SESSION_TTL_ENV)
    os.environ.pop(console_auth.AUTH_ENV, None)
    os.environ.pop(console_auth.SESSION_TTL_ENV, None)
    d = tempfile.mkdtemp(prefix="gdd-console-auth-")
    try:
        print("[C1-1] 密码哈希不含明文、盐随机、校验正确")
        rec = console_auth.hash_password("gdd2026", iterations=1000)
        check(rec["algo"] == "pbkdf2_sha256", "算法为 PBKDF2-HMAC-SHA256")
        check(rec["iterations"] == 1000, "记录迭代次数")
        check("gdd2026" not in json.dumps(rec), "哈希记录不含明文密码")
        check(rec["hash"] != "gdd2026" and len(bytes.fromhex(rec["hash"])) == 32,
              "哈希为 32 字节摘要")
        check(len(bytes.fromhex(rec["salt"])) == 16, "盐为 16 字节")
        rec2 = console_auth.hash_password("gdd2026", iterations=1000)
        check(rec["salt"] != rec2["salt"], "两次哈希的盐不同")
        check(rec["hash"] != rec2["hash"], "两次哈希的摘要不同")
        check(console_auth.verify_password("gdd2026", rec) is True, "正确密码校验通过")
        check(console_auth.verify_password("gdd2027", rec) is False, "错误密码校验失败")
        check(console_auth.verify_password("", rec) is False, "空密码校验失败")
        unicode_rec = console_auth.hash_password("口令-测试", iterations=1000)
        check(console_auth.verify_password("口令-测试", unicode_rec), "非 ASCII 密码可用")
        check(console_auth.DEFAULT_ITERATIONS == 600_000, "默认迭代次数 600000")

        print("[C1-2] 首次初始化：默认账号、文件内容与权限")
        path = os.path.join(d, "nested", "console-auth.json")
        auth = ConsoleAuth(path, iterations=1000)
        check(auth.path == os.path.abspath(path), "ConsoleAuth.path 为绝对路径")
        data = auth.ensure_initialized()
        check(os.path.isfile(path), "凭据文件已创建")
        raw = read(path)
        check("gdd2026" not in raw, "凭据文件不含明文密码")
        check(json.loads(raw)["version"] == 1, "文件 version=1")
        check(data["username"] == "admin", "默认用户名 admin")
        check(data["password"]["iterations"] == 1000, "测试注入的迭代次数生效")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        check(mode == 0o600, f"文件权限 0600（实际 {oct(mode)}）")
        dirmode = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
        check(dirmode == 0o700, f"目录权限 0700（实际 {oct(dirmode)}）")
        check(sorted(os.listdir(os.path.dirname(path))) == ["console-auth.json"],
              "原子写入未残留临时文件")
        check(auth.verify("admin", "gdd2026") is True, "默认账号可登录")
        check(auth.verify("admin", "wrong") is False, "错误密码不能登录")
        check(auth.verify("root", "gdd2026") is False, "错误用户名不能登录")
        check(auth.verify("管理员", "gdd2026") is False, "非 ASCII 用户名不崩溃且拒绝")
        before = raw
        auth.ensure_initialized()
        check(read(path) == before, "已初始化文件不会被重复覆盖")

        print("[C1-3] GDD_CONSOLE_AUTH 覆盖完整路径")
        override = os.path.join(d, "override", "cred.json")
        os.environ[console_auth.AUTH_ENV] = override
        check(console_auth.auth_path() == os.path.abspath(override), "auth_path 读取环境变量")
        env_auth = ConsoleAuth(iterations=1000)
        check(env_auth.path == os.path.abspath(override), "ConsoleAuth() 使用环境变量路径")
        env_auth.ensure_initialized()
        check(os.path.isfile(override), "环境变量路径下完成初始化")
        check(env_auth.verify("admin", "gdd2026"), "环境变量路径下的凭据可登录")
        os.environ.pop(console_auth.AUTH_ENV, None)
        check(console_auth.auth_path() ==
              os.path.join(os.path.expanduser("~"), ".gdd", "console-auth.json"),
              "未设置环境变量时回到 ~/.gdd/console-auth.json")

        print("[C1-4] 损坏文件返回可处置错误，不退回无鉴权")
        bad = os.path.join(d, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        bad_auth = ConsoleAuth(bad, iterations=1000)
        msg = expect_error(bad_auth.load)
        check(bad in msg and "重新初始化" in msg, "损坏 JSON 提示路径与处置方式")
        expect_error(bad_auth.ensure_initialized)   # 已存在但损坏时不得覆盖/放行
        expect_error(bad_auth.verify, "admin", "gdd2026")
        check(read(bad) == "{not json", "损坏文件未被静默覆盖")

        cases = [
            ("顶层不是对象", []),
            ("版本不支持", {"version": 99, "username": "admin",
                            "password": rec}),
            ("用户名为空", {"version": 1, "username": "",
                            "password": rec}),
            ("算法不支持", {"version": 1, "username": "admin",
                            "password": dict(rec, algo="md5")}),
            ("盐不是十六进制", {"version": 1, "username": "admin",
                                "password": dict(rec, salt="zz")}),
            ("哈希长度不对", {"version": 1, "username": "admin",
                              "password": dict(rec, hash="abcd")}),
            ("迭代次数非法", {"version": 1, "username": "admin",
                              "password": dict(rec, iterations=0)}),
        ]
        for label, doc in cases:
            with open(bad, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            msg = expect_error(bad_auth.load)
            check("凭据文件" in msg, f"{label}：报凭据文件错误（{msg[:28]}…）")
        with open(bad, "wb") as f:
            f.write(b"\xff\xfe\x00")  # 非法 UTF-8
        check("重新初始化" in expect_error(bad_auth.load), "非 UTF-8 也给出处置提示")

        as_dir = os.path.join(d, "as-dir")
        os.makedirs(as_dir)
        msg = expect_error(ConsoleAuth(as_dir, iterations=1000).load)
        check("不是普通文件" in msg, "路径是目录时报可处置错误")
        msg = expect_error(ConsoleAuth(as_dir, iterations=1000).ensure_initialized)
        check("不是普通文件" in msg, "ensure_initialized 也不会覆盖目录")

        print("[C1-5] 会话创建、校验、撤销与过期")
        clock = [1000.0]
        store = SessionStore(ttl=10, now=lambda: clock[0])
        token = store.create("admin")
        check(isinstance(token, str) and len(token) >= 32, "token_urlsafe(32) 生成会话 token")
        other = store.create("admin")
        check(other != token, "每次登录 token 不同")
        store.revoke(other)
        check(store.valid(token) is True, "新会话有效")
        check(store.valid("nope") is False, "未知 token 无效")
        check(store.valid("") is False and store.valid(None) is False, "空 token 无效")
        store.revoke(token)
        check(store.valid(token) is False, "撤销后会话失效")
        store.revoke(token)  # 重复撤销不报错

        clock[0] = 1000.0
        live = store.create("admin")
        clock[0] = 1009.999
        check(store.valid(live) is True, "TTL 内会话有效")
        clock[0] = 1010.001
        check(store.valid(live) is False, "超过 TTL 后会话失效")

        clock[0] = 2000.0
        fresh = store.create("admin")
        clock[0] += 20.0
        check(store.valid(fresh) is False, "过期会话再次校验仍无效（已清理）")
        clock[0] = 3000.0
        store.create("a")
        clock[0] += 11.0
        store.create("b")
        check(store.prune() == 1, "prune 只清理过期会话")
        check(store.valid(store.create("c")), "prune 后新会话仍可用")

        real = SessionStore(ttl=1)
        real_token = real.create("admin")
        check(real.valid(real_token), "真实时钟：TTL 内有效")
        time.sleep(1.05)
        check(real.valid(real_token) is False, "真实时钟：超过 1 秒后失效")

        print("[C1-6] 会话 TTL 默认值与环境变量")
        check(SessionStore(ttl=None).ttl == 28_800, "默认 TTL 28800 秒")
        os.environ[console_auth.SESSION_TTL_ENV] = "2"
        check(SessionStore().ttl == 2, "GDD_UI_SESSION_TTL 覆盖 TTL")
        for bad_ttl in ("abc", "0", "-3", "1.5"):
            os.environ[console_auth.SESSION_TTL_ENV] = bad_ttl
            msg = expect_error(SessionStore)
            check(console_auth.SESSION_TTL_ENV in msg, f"非法 TTL {bad_ttl!r} 报可处置错误")
        os.environ.pop(console_auth.SESSION_TTL_ENV, None)

        print("[C1-7] 哈希/校验/初始化不向 stdout/stderr 输出明文、哈希或盐")
        secret_salt, secret_hash = rec["salt"], rec["hash"]
        _, text = capture(auth.verify, "admin", "gdd2026")
        check("gdd2026" not in text, "verify 不输出明文密码")
        _, text = capture(auth.ensure_initialized)
        check("gdd2026" not in text, "ensure_initialized 不输出明文密码")
        check(secret_hash not in text and secret_salt not in text,
              "ensure_initialized 不输出哈希/盐")
        _, text = capture(console_auth.hash_password, "gdd2026", 1000)
        check("gdd2026" not in text, "hash_password 不输出明文密码")
        corrupt_msg = expect_error(ConsoleAuth(bad, iterations=1000).load)
        check("gdd2026" not in corrupt_msg and secret_hash not in corrupt_msg
              and secret_salt not in corrupt_msg, "错误消息不含明文、哈希或盐")

        print("[C1-8] 默认凭据路径在仓库外且不被 git 跟踪")
        default_path = console_auth.auth_path()
        repo = os.path.realpath(ROOT)
        candidate = os.path.realpath(default_path)
        check(os.path.commonpath([repo, candidate]) != repo,
              f"默认凭据路径位于仓库外（{default_path}）")
        check(git_tracks(default_path) is False, "默认凭据路径不在 git 索引中")
        check(git_tracks(path) is False, "临时凭据路径不在 git 索引中")
        check(git_tracks(os.path.join(ROOT, "gdd", "ui.py")) is True,
              "git 检查本身有效（既有源码受跟踪）")
    finally:
        if old_auth_env is None:
            os.environ.pop(console_auth.AUTH_ENV, None)
        else:
            os.environ[console_auth.AUTH_ENV] = old_auth_env
        if old_ttl_env is None:
            os.environ.pop(console_auth.SESSION_TTL_ENV, None)
        else:
            os.environ[console_auth.SESSION_TTL_ENV] = old_ttl_env
        import shutil
        shutil.rmtree(d)
    print("ALL CONSOLE AUTH PASSED")


if __name__ == "__main__":
    main()
