#!/usr/bin/env python3
"""GUI 验收（D4，兼 D6 的 app 主进程监听检查与 D9 错误对话框截图）。

启动真实 `.app`（经 LaunchServices `open --env`，配置/日志/端口均隔离到临时目录），
等待日志出现 `event=window_created`，截屏并检查进程/监听状态；退出 app 后确认
控制台仍存活。

说明：生产端口固定 8765；若 8765 被用户控制台占用，本脚本用测试钩子
GDD_CONSOLE_TEST_PORT 选择空闲端口。窗口 URL 与端口在日志中记录。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APP = ROOT / "src-tauri/target/release/bundle/macos/GDD Console.app"
DEFAULT_EVIDENCE = ROOT / ".gdd/nodes/implement/evidence"


class DummyHandler(BaseHTTPRequestHandler):
    """返回非 GDD 形状 JSON 的占位服务。"""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        body = b'{"hello": "not a gdd console"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_dummy(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), DummyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def find_python() -> str:
    candidates: list[Path] = []
    if os.environ.get("GDD_TEST_PYTHON"):
        candidates.append(Path(os.environ["GDD_TEST_PYTHON"]))
    candidates.append(Path(sys.executable))
    for base in (
        Path.home() / ".local/bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ):
        for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3"):
            candidates.append(base / name)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        out = subprocess.run(
            [str(candidate), "-c", "import sys; print(sys.version_info[0]*100+sys.version_info[1])"],
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and out.stdout.strip().isdigit() and int(out.stdout.strip()) >= 310:
            return str(candidate.resolve())
    raise RuntimeError("需要 Python >= 3.10")


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port_free(port: int, timeout: float = 10.0) -> None:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"端口 {port} 未释放")


def wait_gdd(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=2) as response:
                if json.load(response).get("ok") is True:
                    return
        except Exception:  # noqa: BLE001 - 轮询
            pass
        time.sleep(0.1)
    raise RuntimeError("控制台未就绪")


def create_task(port: int, name: str) -> None:
    body = json.dumps(
        {"name": name, "template": "delivery", "requirement": "GUI 验收任务"}
    ).encode()
    request = Request(
        f"http://127.0.0.1:{port}/api/tasks",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-GDD-Request": "1"},
    )
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if payload.get("ok") is not True:
        raise RuntimeError(f"创建任务失败：{payload}")


def app_pid(bundle: Path) -> int | None:
    binary = str(bundle / "Contents/MacOS/gdd-console")
    out = subprocess.run(["pgrep", "-f", binary], capture_output=True, text=True)
    pids = [int(line) for line in out.stdout.splitlines() if line.strip().isdigit()]
    return pids[0] if pids else None


def quit_app(bundle: Path) -> str:
    bundle_id = "com.gdd.console"
    result = subprocess.run(
        ["osascript", "-e", f'tell application id "{bundle_id}" to quit'],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        return "AppleScript quit"
    pid = app_pid(bundle)
    if pid:
        os.kill(pid, signal.SIGTERM)
        return f"SIGTERM（AppleScript 失败：{result.stderr.strip()}）"
    return "app 已不在运行"


def wait_window(app_log: Path, timeout: float = 45.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app_log.exists():
            text = app_log.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"event=window_created url=(\S+)", text)
            if match:
                return match.group(1)
            if "event=error" in text:
                raise RuntimeError(f"app 报错：{text[-1200:]}")
        time.sleep(0.2)
    raise RuntimeError(f"未在 {timeout:g}s 内创建窗口")


def read_log(log_dir: Path) -> str:
    path = log_dir / "app.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def wait_logged(app_log: Path, pattern: str, timeout: float = 40.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = read_log(app_log.parent)
        match = re.search(pattern, text)
        if match:
            return match.group(1) if match.groups() else match.group(0)
        time.sleep(0.2)
    raise RuntimeError(f"日志未出现 {pattern}：{read_log(app_log.parent)[-1200:]}")


def parse_console_pid(log_text: str) -> int | None:
    matches = re.findall(r"event=console_ready pid=(\d+)", log_text)
    return int(matches[-1]) if matches else None


def listening_on(pid: int) -> list[str]:
    # -a 让 -p 与 -i 取交集（否则 lsof 会列出全机监听套接字）。
    out = subprocess.run(
        ["lsof", "-a", "-nP", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    return [line for line in out.stdout.splitlines()[1:] if line.strip()]


def console_alive(workbench: Path) -> list[int]:
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    needle = f"--root {workbench.resolve()}"
    pids = []
    for line in out.stdout.splitlines():
        if "gdd.ui" in line and needle in line:
            head = line.strip().split(None, 1)[0]
            if head.isdigit():
                pids.append(int(head))
    return sorted(pids)


def capture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["screencapture", "-x", str(path)], check=True)


def try_window_title(bundle: Path) -> str | None:
    script = (
        'tell application "System Events" to tell process "GDD Console" '
        'to get name of every window'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def launch(bundle: Path, env_pairs: list[tuple[str, str]]) -> None:
    command = ["open", "-n", "-F"]
    for key, value in env_pairs:
        command.extend(["--env", f"{key}={value}"])
    command.append(str(bundle))
    subprocess.run(command, check=True)


def write_config(config_dir: Path, gdd: Path, python: str, workbench: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gddHome": str(gdd),
                "python": python,
                "workbenchRoot": str(workbench),
                "path": [],
                "env": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(config_dir / "config.json", 0o600)


def window_helper() -> Path | None:
    """编译窗口可见性探针（CoreGraphics），返回可执行文件路径。"""
    source = ROOT / "scripts/window_list.swift"
    if not source.is_file() or shutil.which("swiftc") is None:
        return None
    binary = Path(tempfile.gettempdir()) / "gdd-window-list-helper"
    if binary.is_file() and binary.stat().st_mtime >= source.stat().st_mtime:
        return binary
    result = subprocess.run(
        ["swiftc", "-O", str(source), "-o", str(binary)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"WARN: 无法编译窗口探针：{result.stderr[-500:]}")
        return None
    return binary


def window_snapshot(pid: int, on_screen_only: bool = True) -> list[dict] | None:
    helper = window_helper()
    if helper is None:
        return None
    command = [str(helper)]
    if on_screen_only:
        command.append("--on-screen")
    command.append(f"--pid={pid}")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None


def wait_visible_windows(pid: int, predicate, timeout: float = 10.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        snapshot = window_snapshot(pid, True)
        if snapshot is not None:
            last = snapshot
            matched = [window for window in snapshot if predicate(window)]
            if matched:
                return matched
        time.sleep(0.2)
    raise RuntimeError(f"未发现符合条件的可见窗口（最近快照：{last}）")


def is_main_window(window: dict) -> bool:
    bounds = window.get("bounds") or {}
    return (
        bool(window.get("onscreen"))
        and window.get("layer") == 0
        and bounds.get("Width", 0) >= 500
        and bounds.get("Height", 0) >= 300
    )


def is_alert_window(window: dict) -> bool:
    # NSAlert 窗口尺寸随文本变化（实测 260~280 x 430 左右），layer 可能为 0 或 8；
    # 错误路径不会创建 1200x800 的主窗口，因此用“可见 + 尺寸区间”识别。
    bounds = window.get("bounds") or {}
    width = bounds.get("Width", 0)
    height = bounds.get("Height", 0)
    return bool(window.get("onscreen")) and 150 <= width <= 1000 and 100 <= height <= 900


def wait_app_pid(bundle: Path, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = app_pid(bundle)
        if pid is not None:
            return pid
        time.sleep(0.2)
    raise RuntimeError("app 未在 20s 内启动")


def kill_app_force(bundle: Path, pid: int | None) -> None:
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and app_pid(bundle) is not None:
            time.sleep(0.2)


def listener_pid(port: int) -> int | None:
    out = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
    )
    pids = [int(line) for line in out.stdout.splitlines() if line.strip().isdigit()]
    return pids[0] if pids else None


def check_8765_reuse(bundle: Path, python: str) -> int:
    """生产端口 8765 只读复用验证：不启动、不终止任何控制台。"""
    try:
        with urlopen("http://127.0.0.1:8765/api/overview", timeout=2) as response:
            overview = json.load(response)
        if overview.get("ok") is not True:
            raise RuntimeError("8765 响应不是 GDD 控制台")
    except HTTPError as exc:
        if exc.code == 401:
            # 控制台默认是鉴权模式；桌面壳复用只适用于本地模式，鉴权模式需登录、不适用。
            print("SKIP: 8765 是鉴权模式控制台（需要登录），本地复用检查不适用")
            return 0
        print(f"SKIP: 8765 当前没有 GDD 控制台（{exc}）")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: 8765 当前没有 GDD 控制台（{exc}）")
        return 0

    original_pid = listener_pid(8765)
    tmp = Path(tempfile.mkdtemp(prefix="gdd-console-8765-")).resolve()
    try:
        workbench = tmp / "workbench"
        workbench.mkdir()
        config_dir = tmp / "config"
        log_dir = tmp / "logs"
        write_config(config_dir, ROOT, python, workbench)
        launch(
            bundle,
            [
                ("GDD_CONSOLE_CONFIG_DIR", str(config_dir)),
                ("GDD_CONSOLE_LOG_DIR", str(log_dir)),
                ("GDD_CONSOLE_TEST_NO_DIALOG", "1"),
                ("GDD_CONSOLE_TEST_DUMP_PAGE", "1"),
            ],
        )
        url = wait_window(log_dir / "app.log")
        dump_url = wait_logged(log_dir / "app.log", r"event=window_url url=(\S+)")
        log_text = read_log(log_dir)
        failures = []
        if url != "http://127.0.0.1:8765/" or dump_url != "http://127.0.0.1:8765/":
            failures.append(f"生产 URL 不符：window_created={url} window_url={dump_url}")
        if "event=probe port=8765 result=reuse" not in log_text:
            failures.append("日志未记录 8765 复用")
        if "event=console_spawn" in log_text:
            failures.append("复用场景不应启动控制台")
        content_raw = wait_logged(log_dir / "app.log", r"event=window_content (.+)")
        content = json.loads(content_raw)
        if "GDD 控制台" not in str(content.get("title", "")):
            failures.append(f"窗口标题异常：{content.get('title')!r}")
        if listener_pid(8765) != original_pid:
            failures.append("原控制台 PID 发生变化")
        quit_app(bundle)
        if failures:
            print("FAIL: 8765 生产端口复用验证")
            for item in failures:
                print(f"  - {item}")
            return 1
        print(
            f"PASS: 生产端口 8765 复用——原控制台 pid={original_pid} 不变，"
            f"窗口 URL={dump_url}"
        )
        return 0
    finally:
        pid = app_pid(bundle)
        if pid:
            os.kill(pid, signal.SIGTERM)
        shutil.rmtree(tmp, ignore_errors=True)


def check_error_dialogs(
    bundle: Path, python: str, evidence: Path, port_override: int | None = None
) -> int:
    """D9/D11 GUI 错误对话框可见性验证（不设置 NO_DIALOG，直接观察窗口）。"""
    if app_pid(bundle) is not None:
        print("FAIL: 已有 GDD Console 在运行，无法做错误对话框隔离验证")
        return 1
    results: list[str] = []
    failures: list[str] = []

    # D9：无效配置 -> 可见错误对话框 + 不启动控制台。
    tmp = Path(tempfile.mkdtemp(prefix="gdd-console-err-d9-")).resolve()
    pid = None
    try:
        workbench = tmp / "workbench"
        workbench.mkdir()
        config_dir = tmp / "config"
        log_dir = tmp / "logs"
        missing = tmp / "missing-gdd"
        write_config(config_dir, missing, python, workbench)
        launch(
            bundle,
            [
                ("GDD_CONSOLE_CONFIG_DIR", str(config_dir)),
                ("GDD_CONSOLE_LOG_DIR", str(log_dir)),
                ("GDD_CONSOLE_TEST_PORT", str(free_port())),
                ("GDD_CONSOLE_TEST_NO_DIALOG", "0"),
            ],
        )
        pid = wait_app_pid(bundle)
        wait_logged(log_dir / "app.log", r"event=dialog_shown", 15)
        windows = wait_visible_windows(pid, is_alert_window, 10)
        log_text = read_log(log_dir)
        if "event=error" not in log_text:
            failures.append("D9 日志缺少 event=error")
        if str(missing) not in log_text:
            failures.append("D9 错误信息未包含具体路径")
        if console_alive(workbench):
            failures.append("D9 不应启动 gdd.ui")
        screenshot = evidence / "d9-error-dialog.png"
        try:
            capture(screenshot)
            results.append(f"D9 对话框截图 {screenshot}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"D9 对话框截图不可用：{exc}")
        results.append(
            f"D9 可见错误对话框：onscreen=true layer={windows[0].get('layer')} "
            f"bounds={windows[0].get('bounds')}；无 gdd.ui 子进程"
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"D9 {type(exc).__name__}: {exc}")
    finally:
        kill_app_force(bundle, pid)
        shutil.rmtree(tmp, ignore_errors=True)

    # D11：非 GDD 服务占用端口 -> 可见错误对话框 + 不启动第二个控制台。
    tmp = Path(tempfile.mkdtemp(prefix="gdd-console-err-d11-")).resolve()
    pid = None
    dummy = None
    try:
        gdd = tmp / "gdd-src"
        shutil.copytree(
            ROOT / "gdd",
            gdd / "gdd",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        workbench = tmp / "workbench"
        workbench.mkdir()
        config_dir = tmp / "config"
        log_dir = tmp / "logs"
        write_config(config_dir, gdd, python, workbench)
        port = port_override or free_port()
        dummy = start_dummy(port)
        error_env = [
            ("GDD_CONSOLE_CONFIG_DIR", str(config_dir)),
            ("GDD_CONSOLE_LOG_DIR", str(log_dir)),
            ("GDD_CONSOLE_TEST_NO_DIALOG", "0"),
        ]
        # 8765 是生产默认端口：不注入测试覆盖，走 app 的生产默认路径。
        if port != 8765:
            error_env.insert(2, ("GDD_CONSOLE_TEST_PORT", str(port)))
        launch(bundle, error_env)
        pid = wait_app_pid(bundle)
        wait_logged(log_dir / "app.log", r"event=dialog_shown", 15)
        windows = wait_visible_windows(pid, is_alert_window, 10)
        log_text = read_log(log_dir)
        if str(port) not in log_text:
            failures.append("D11 错误信息未包含端口")
        if ("非 GDD" not in log_text) and ("占用" not in log_text):
            failures.append("D11 错误信息未说明端口冲突")
        if console_alive(workbench):
            failures.append("D11 不应启动 gdd.ui")
        screenshot = evidence / "d11-error-dialog.png"
        try:
            capture(screenshot)
            results.append(f"D11 对话框截图 {screenshot}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"D11 对话框截图不可用：{exc}")
        results.append(
            f"D11 可见错误对话框：onscreen=true layer={windows[0].get('layer')} "
            f"bounds={windows[0].get('bounds')}；无 gdd.ui 子进程"
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"D11 {type(exc).__name__}: {exc}")
    finally:
        kill_app_force(bundle, pid)
        if dummy is not None:
            dummy.shutdown()
            dummy.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    for item in results:
        print(f"  {item}")
    if failures:
        print("FAIL: D9/D11 GUI 错误对话框验证")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("PASS: D9/D11 均显示可见的原生错误对话框，且未启动 gdd.ui")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", default=str(DEFAULT_APP))
    parser.add_argument("--evidence", default=str(DEFAULT_EVIDENCE))
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--check-8765-reuse",
        action="store_true",
        help="仅验证生产端口 8765 的只读复用（8765 有 GDD 控制台时）",
    )
    parser.add_argument(
        "--check-error-dialogs",
        action="store_true",
        help="验证 D9/D11 的原生错误对话框可见性",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="固定端口（默认随机；8765 空闲时传 8765 做字面生产端口验证，不注入测试端口覆盖）",
    )
    args = parser.parse_args()

    bundle = Path(args.app)
    binary = bundle / "Contents/MacOS/gdd-console"
    if not binary.is_file():
        print(f"FAIL: app 不存在：{bundle}", file=sys.stderr)
        return 1
    python = find_python()
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    if args.check_8765_reuse:
        return check_8765_reuse(bundle, python)
    if args.check_error_dialogs:
        return check_error_dialogs(bundle, python, evidence, args.port)

    tmp = Path(tempfile.mkdtemp(prefix="gdd-console-gui-")).resolve()
    gdd = tmp / "gdd-src"
    shutil.copytree(
        ROOT / "gdd", gdd / "gdd", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    workbench = tmp / "workbench"
    workbench.mkdir()
    config_dir = tmp / "config"
    log_dir = tmp / "logs"
    write_config(config_dir, gdd, python, workbench)
    port = args.port or free_port()
    task = f"task-{uuid.uuid4().hex[:8]}"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(gdd)
    external = subprocess.Popen(
        [
            python,
            "-m",
            "gdd.ui",
            "--root",
            str(workbench),
            "--port",
            str(port),
            "--no-open",
            "--local",
        ],
        cwd=str(workbench),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []
    results: list[str] = []
    try:
        wait_gdd(port)
        create_task(port, task)
        external.terminate()
        external.wait(timeout=5)
        wait_port_free(port)

        gui_env = [
            ("GDD_CONSOLE_CONFIG_DIR", str(config_dir)),
            ("GDD_CONSOLE_LOG_DIR", str(log_dir)),
            ("GDD_CONSOLE_TEST_NO_DIALOG", "1"),
            ("GDD_CONSOLE_TEST_DUMP_PAGE", "1"),
            ("GDD_CONSOLE_TEST_CLOSE_AFTER_READY", "1"),
        ]
        # 8765 是生产默认端口：不注入测试覆盖，走 app 的生产默认路径。
        if port != 8765:
            gui_env.insert(2, ("GDD_CONSOLE_TEST_PORT", str(port)))
        launch(bundle, gui_env)
        url = wait_window(log_dir / "app.log")
        if url != f"http://127.0.0.1:{port}/":
            failures.append(f"窗口 URL 不符：{url}")
        results.append(f"window_created url={url}")

        dump_url = wait_logged(log_dir / "app.log", r"event=window_url url=(\S+)")
        if dump_url != f"http://127.0.0.1:{port}/":
            failures.append(f"窗口实际 URL 不符：{dump_url}")
        results.append(f"window_url（WKWebView 实际加载）={dump_url}")

        content_raw = wait_logged(
            log_dir / "app.log", r"event=window_content (.+)"
        )
        try:
            content = json.loads(content_raw)
        except json.JSONDecodeError:
            # eval_with_callback 可能把结果再序列化为 JSON 字符串。
            content = json.loads(json.loads(content_raw))
        title_text = str(content.get("title", ""))
        body_text = str(content.get("text", ""))
        if "GDD 控制台" not in title_text and "GDD 控制台" not in body_text:
            failures.append(f"窗口内容未见控制台标题：title={title_text!r}")
        if task not in body_text:
            failures.append(f"窗口内容未见预置任务 {task}（前 500 字：{body_text[:500]!r}）")
        results.append(
            f"window_content title={title_text!r}，body 含任务 {task}，"
            f"正文 {len(body_text)} 字"
        )

        pid = app_pid(bundle)
        if pid is None:
            failures.append("未找到 app 主进程")
        else:
            results.append(f"app pid={pid}")
            try:
                main_windows = wait_visible_windows(pid, is_main_window, 10)
                results.append(
                    f"主窗口可见（onscreen=true）{len(main_windows)} 个，"
                    f"bounds={main_windows[0].get('bounds')}"
                )
            except RuntimeError as exc:
                failures.append(str(exc))
            listen = listening_on(pid)
            if listen:
                failures.append(f"app 主进程出现 TCP LISTEN：{listen}")
            else:
                results.append("app 主进程无 TCP LISTEN")

        time.sleep(1.0)
        screenshot = evidence / "d4-window.png"
        try:
            capture(screenshot)
            results.append(f"截图 {screenshot}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"截图不可用（本机缺少屏幕录制权限）：{exc}")

        title = try_window_title(bundle)
        if title:
            results.append(f"窗口标题（System Events）：{title}")
        else:
            results.append("窗口标题：System Events 不可用（需可访问性权限）")

        # 关闭窗口：只隐藏、app 不退出。
        deadline = time.monotonic() + 15
        while (
            "event=test_close_requested" not in read_log(log_dir)
            and time.monotonic() < deadline
        ):
            time.sleep(0.2)
        time.sleep(1.0)
        if app_pid(bundle) is None:
            failures.append("关闭窗口后 app 退出")
        close_log = read_log(log_dir)
        if "event=window_hidden" in close_log:
            results.append("关闭窗口后仅隐藏（window_hidden），app 存活")
        else:
            results.append("关闭窗口后窗口销毁，app 仍存活")

        # Dock/LaunchServices 重新打开：重新探测并显示窗口。
        mark = len(close_log)
        subprocess.run(["open", "-a", str(bundle)], check=True)
        deadline = time.monotonic() + 20
        reopened = False
        while time.monotonic() < deadline:
            tail = read_log(log_dir)[mark:]
            if "event=reopen_ready" in tail and (
                "event=window_shown" in tail or "event=window_created" in tail
            ):
                reopened = True
                break
            time.sleep(0.2)
        if reopened:
            results.append("Dock 重新打开后重新探测并显示窗口")
        else:
            failures.append(f"Dock 重新打开未恢复窗口：{read_log(log_dir)[mark:][-800:]}")

        log_text = read_log(log_dir)
        console_pid = parse_console_pid(log_text)
        if console_pid is None or console_pid not in console_alive(workbench):
            failures.append(f"控制台进程异常：log pid={console_pid} 实际={console_alive(workbench)}")

        quit_mode = quit_app(bundle)
        results.append(f"退出方式：{quit_mode}")
        deadline = time.monotonic() + 10
        while app_pid(bundle) is not None and time.monotonic() < deadline:
            time.sleep(0.2)
        if app_pid(bundle) is not None:
            failures.append("app 退出后仍在运行")
        if console_pid and console_pid not in console_alive(workbench):
            failures.append("app 退出后控制台未存活")
        else:
            results.append(f"app 退出后控制台仍存活 pid={console_pid}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        if external.poll() is None:
            external.terminate()
        pid = app_pid(bundle)
        if pid:
            os.kill(pid, signal.SIGTERM)
        for console_pid in console_alive(workbench):
            try:
                os.kill(console_pid, signal.SIGTERM)
            except OSError:
                pass
        if args.keep:
            print(f"保留临时目录：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    for item in results:
        print(f"  {item}")
    if failures:
        print("FAIL: D4 GUI 验收")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("PASS: D4 窗口展示控制台页面；D6 app 主进程无监听；退出后控制台存活")
    return 0


if __name__ == "__main__":
    sys.exit(main())
