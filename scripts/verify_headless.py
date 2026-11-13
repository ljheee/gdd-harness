#!/usr/bin/env python3
"""应用级 headless 验收（D2/D3/D5/D6/D7/D9/D11/D12/D13 的可自动化部分）。

通过 app 二进制的 `GDD_CONSOLE_TEST_HEADLESS=1` 入口执行与 GUI 相同的
配置 / 探测 / 拉起核心；临时 GDD 源码副本、临时工作台、随机端口，
不读写用户现有工作台。

说明：
- 生产端口固定 8765（`supervisor::DEFAULT_PORT`，有单元测试守护）。
- 本机 8765 可能被用户正在使用的控制台占用；脚本使用仅测试用的
  `GDD_CONSOLE_TEST_PORT` 覆盖端口做隔离验证，实际端口记录在输出中。
- D4（窗口画面）由 scripts/verify_gui.sh 单独验证。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APP = (
    ROOT
    / "src-tauri/target/release/bundle/macos/GDD Console.app/Contents/MacOS/gdd-console"
)

RESULTS: list[tuple[str, bool, str]] = []
FIXED_PORT: int | None = None


def scenario(name: str):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            try:
                detail = fn(*args, **kwargs)
                RESULTS.append((name, True, detail))
                print(f"PASS: {name} — {detail}")
            except Exception as exc:  # noqa: BLE001 - 验收脚本需要汇总所有失败
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"FAIL: {name} — {type(exc).__name__}: {exc}")

        wrapper.__name__ = fn.__name__
        return wrapper

    return decorator


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


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
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3"):
                candidates.append(Path(entry) / name)
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if not candidate.is_file():
            continue
        try:
            out = subprocess.run(
                [str(candidate), "-c", "import sys; print(sys.version_info[0]*100+sys.version_info[1])"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except OSError:
            continue
        if out.returncode == 0 and out.stdout.strip().isdigit():
            if int(out.stdout.strip()) >= 310:
                return str(candidate.resolve())
    raise RuntimeError("需要 Python >= 3.10 作为控制台解释器")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pick_port() -> int:
    """默认随机端口；`--port 8765` 用于 8765 空闲时做真实端口验证。"""
    return FIXED_PORT if FIXED_PORT is not None else free_port()


def ensure_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"端口 {port} 已被占用，无法开始隔离测试")


def wait_port_free(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"端口 {port} 未在 {timeout:g} 秒内释放")


def wait_gdd(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if get_json(port, "/api/overview").get("ok") is True:
                return
        except (OSError, URLError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"控制台未在 {timeout:g} 秒内就绪（端口 {port}）")


def get_json(port: int, path: str) -> dict:
    with urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return json.load(response)


def get_text(port: int, path: str) -> str:
    with urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.read().decode("utf-8", "replace")


def create_task(port: int, name: str) -> None:
    body = json.dumps(
        {"name": name, "template": "delivery", "requirement": f"验收测试任务 {name}"}
    ).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{port}/api/tasks",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GDD-Request": "1",
        },
    )
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    check(payload.get("ok") is True, f"创建任务失败：{payload}")


def copy_gdd(destination: Path) -> Path:
    root = destination / "gdd-src"
    shutil.copytree(
        ROOT / "gdd",
        root / "gdd",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return root


def prepare(tmp: Path, tag: str) -> tuple[Path, Path]:
    base = tmp / tag
    base.mkdir(parents=True, exist_ok=True)
    gdd = copy_gdd(base)
    workbench = base / "workbench"
    return gdd, workbench


def write_config(
    config_dir: Path, gdd_home: Path, python: str, workbench: Path
) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    payload = {
        "version": 1,
        "gddHome": str(gdd_home),
        "python": python,
        "workbenchRoot": str(workbench),
        "path": [],
        "env": {},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def run_app(
    app_bin: Path,
    config_dir: Path,
    log_dir: Path,
    port: int,
    timeout: float = 60.0,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        {
            "GDD_CONSOLE_CONFIG_DIR": str(config_dir),
            "GDD_CONSOLE_LOG_DIR": str(log_dir),
            "GDD_CONSOLE_TEST_HEADLESS": "1",
            "GDD_CONSOLE_TEST_NO_DIALOG": "1",
        }
    )
    # 8765 是 app 的生产默认端口：此时不注入测试端口覆盖，
    # 让 app 走“未设置环境变量”的生产路径（D2/D3/D11 字面验证）。
    if port != 8765:
        env["GDD_CONSOLE_TEST_PORT"] = str(port)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(app_bin)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def read_log(log_dir: Path) -> str:
    path = log_dir / "app.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def parse_ready(log_text: str) -> dict:
    match = re.search(
        r"event=headless_ready pid=(\S+) started=(true|false) url=(\S+) log=(.+)",
        log_text,
    )
    check(match is not None, f"日志缺少 headless_ready：{log_text[-1000:]}")
    return {
        "pid": match.group(1),
        "started": match.group(2),
        "url": match.group(3),
        "log": match.group(4).strip(),
    }


def ps_command(pid: int) -> str:
    out = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True
    )
    return out.stdout.strip()


def console_pids(workbench: Path) -> list[int]:
    out = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True
    )
    needle = f"--root {workbench.resolve()}"
    pids = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if "gdd.ui" in line and needle in line:
            head = line.split(None, 1)[0]
            if head.isdigit():
                pids.append(int(head))
    return sorted(pids)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate(pid: int, timeout: float = 5.0) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        # 已退出但仍是僵尸（父进程未回收）时 kill(0) 仍成功，直接返回。
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        )
        if "Z" in out.stdout:
            return
        time.sleep(0.1)


def start_external_console(python: str, gdd_home: Path, workbench: Path, port: int):
    workbench.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(gdd_home)
    proc = subprocess.Popen(
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
    wait_gdd(port)
    return proc


def stop_external(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def listeners_for(port: int) -> set[str]:
    out = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    addresses: set[str] = set()
    for line in out.stdout.splitlines():
        match = re.search(r"\s(\S+):(\d+) \(LISTEN\)$", line)
        if match and int(match.group(2)) == port:
            addresses.add(f"{match.group(1)}:{match.group(2)}")
    return addresses


def non_loopback_ip() -> str | None:
    out = subprocess.run(
        ["ipconfig", "getifaddr", "en0"], capture_output=True, text=True
    )
    candidate = out.stdout.strip()
    if candidate and not candidate.startswith("127."):
        return candidate
    out = subprocess.run(["ifconfig"], capture_output=True, text=True)
    for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", out.stdout):
        address = match.group(1)
        if not address.startswith("127."):
            return address
    return None


class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        body = b'{"hello": "not a gdd console"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D401 - 静默
        pass


def start_dummy(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), DummyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------


@scenario("D2 8765（测试端口）无监听时冷启动控制台")
def d2(tmp: Path, app_bin: Path, python: str) -> str:
    port = pick_port()
    ensure_port_free(port)
    gdd, workbench = prepare(tmp, "d2")
    config_dir = tmp / "d2" / "config"
    log_dir = tmp / "d2" / "logs"
    write_config(config_dir, gdd, python, workbench)

    started_at = time.monotonic()
    proc = run_app(app_bin, config_dir, log_dir, port)
    elapsed = time.monotonic() - started_at
    log_text = read_log(log_dir)
    check(proc.returncode == 0, f"app 退出码 {proc.returncode}；日志：{log_text[-800:]}")
    ready = parse_ready(log_text)
    check(ready["started"] == "true", f"应记录 started=true：{ready}")
    check(
        ready["url"] == f"http://127.0.0.1:{port}/",
        f"URL 不符：{ready['url']}",
    )
    check(elapsed < 30, f"就绪耗时 {elapsed:.1f}s 超过 30s 上限")
    pid = int(ready["pid"])
    try:
        overview = get_json(port, "/api/overview")
        check(overview.get("ok") is True, f"overview 异常：{overview}")
        check(isinstance(overview.get("templates"), list), "templates 不是数组")
        check(isinstance(overview.get("tasks"), list), "tasks 不是数组")

        cmdline = ps_command(pid)
        check("-m gdd.ui" in cmdline, f"子进程不是 gdd.ui：{cmdline}")
        check(
            f"--root {workbench.resolve()}" in cmdline,
            f"argv 缺少工作台：{cmdline}",
        )
        check(f"--port {port}" in cmdline, f"argv 缺少端口：{cmdline}")
        check("--no-open" in cmdline, f"argv 缺少 --no-open：{cmdline}")

        addresses = listeners_for(port)
        check(addresses, "lsof 未看到监听")
        check(
            all(address == f"127.0.0.1:{port}" for address in addresses),
            f"监听地址不是仅 127.0.0.1：{sorted(addresses)}",
        )
        lan_ip = non_loopback_ip()
        if lan_ip:
            try:
                with socket.create_connection((lan_ip, port), timeout=2):
                    raise AssertionError(f"非回环地址 {lan_ip}:{port} 竟可连接")
            except OSError:
                pass
        return (
            f"pid={pid} 就绪 {elapsed:.2f}s，argv 含 --root/--port/--no-open，"
            f"overview 200，监听仅 127.0.0.1"
        )
    finally:
        terminate(pid)
        wait_port_free(port)


@scenario("D3 复用已在运行的控制台")
def d3(tmp: Path, app_bin: Path, python: str) -> str:
    port = pick_port()
    ensure_port_free(port)
    gdd, workbench = prepare(tmp, "d3")
    config_dir = tmp / "d3" / "config"
    log_dir = tmp / "d3" / "logs"
    write_config(config_dir, gdd, python, workbench)

    external = start_external_console(python, gdd, workbench, port)
    original_pid = external.pid
    try:
        task = f"task-{uuid.uuid4().hex[:8]}"
        create_task(port, task)
        before_cmd = ps_command(original_pid)
        proc = run_app(app_bin, config_dir, log_dir, port)
        log_text = read_log(log_dir)
        check(proc.returncode == 0, f"app 退出码 {proc.returncode}；日志：{log_text[-800:]}")
        ready = parse_ready(log_text)
        check(ready["started"] == "false", f"应复用而非启动：{ready}")
        check(ready["pid"] == "-", f"复用不应有子进程 pid：{ready}")
        check(pid_alive(original_pid), "原控制台进程应存活")
        check(ps_command(original_pid) == before_cmd, "原控制台 PID/命令行不应变化")
        check(
            console_pids(workbench) == [original_pid],
            f"应只有一个 gdd.ui 进程：{console_pids(workbench)}",
        )
        overview = get_json(port, "/api/overview")
        names = [item["name"] for item in overview.get("tasks", [])]
        check(names == [task], f"任务列表应来自原工作台：{names}")
        return f"复用原控制台 pid={original_pid}，无第二个进程，任务列表含 {task}"
    finally:
        stop_external(external)
        wait_port_free(port)


@scenario("D5 更新 GDD 源码无需重建 app")
def d5(tmp: Path, app_bin: Path, python: str) -> str:
    port = pick_port()
    ensure_port_free(port)
    gdd, workbench = prepare(tmp, "d5")
    config_dir = tmp / "d5" / "config"
    log_dir = tmp / "d5" / "logs"
    write_config(config_dir, gdd, python, workbench)
    app_hash_before = sha256(app_bin)

    proc = run_app(app_bin, config_dir, log_dir, port)
    log_text = read_log(log_dir)
    check(proc.returncode == 0, f"首次启动失败：{log_text[-800:]}")
    ready = parse_ready(log_text)
    first_pid = int(ready["pid"])
    check(ready["started"] == "true", f"首次应自行拉起：{ready}")
    terminate(first_pid)
    wait_port_free(port)

    marker = f"GDD-MARK-{uuid.uuid4().hex[:10]}"
    ui_py = gdd / "gdd" / "ui.py"
    source = ui_py.read_text(encoding="utf-8")
    check("GDD 控制台</h1>" in source, "ui.py 中未找到页面标题锚点")
    ui_py.write_text(
        source.replace("GDD 控制台</h1>", f"GDD 控制台 {marker}</h1>", 1),
        encoding="utf-8",
    )

    log_dir2 = tmp / "d5" / "logs-2"
    proc2 = run_app(app_bin, config_dir, log_dir2, port)
    log_text2 = read_log(log_dir2)
    check(proc2.returncode == 0, f"二次启动失败：{log_text2[-800:]}")
    ready2 = parse_ready(log_text2)
    second_pid = int(ready2["pid"])
    try:
        page = get_text(port, "/")
        check(marker in page, "页面未包含源码更新标记")
        check(
            sha256(app_bin) == app_hash_before,
            "app 二进制哈希发生变化（不应重建）",
        )
        return (
            f"页面包含标记 {marker}，app 二进制哈希未变 "
            f"({app_hash_before[:12]}…)，控制台 pid={second_pid}"
        )
    finally:
        terminate(second_pid)
        wait_port_free(port)


@scenario("D6 控制台只监听本机（8765 等价端口）")
def d6(tmp: Path, app_bin: Path, python: str) -> str:
    port = pick_port()
    ensure_port_free(port)
    gdd, workbench = prepare(tmp, "d6")
    config_dir = tmp / "d6" / "config"
    log_dir = tmp / "d6" / "logs"
    write_config(config_dir, gdd, python, workbench)
    proc = run_app(app_bin, config_dir, log_dir, port)
    check(proc.returncode == 0, f"启动失败：{read_log(log_dir)[-800:]}")
    ready = parse_ready(read_log(log_dir))
    pid = int(ready["pid"])
    try:
        addresses = listeners_for(port)
        check(
            addresses == {f"127.0.0.1:{port}"},
            f"监听地址异常：{sorted(addresses)}",
        )
        lan_ip = non_loopback_ip()
        if lan_ip:
            try:
                with socket.create_connection((lan_ip, port), timeout=2):
                    raise AssertionError(f"非回环地址 {lan_ip}:{port} 竟可连接")
            except OSError:
                pass
        return f"监听 {sorted(addresses)}；非回环连接失败"
    finally:
        terminate(pid)
        wait_port_free(port)


@scenario("D7 工作台目录决定任务列表")
def d7(tmp: Path, app_bin: Path, python: str) -> str:
    port = pick_port()
    ensure_port_free(port)
    gdd = copy_gdd(tmp / "d7")
    r1 = tmp / "d7" / "R1"
    r2 = tmp / "d7" / "R2"
    config_dir = tmp / "d7" / "config"
    log_dir = tmp / "d7" / "logs"
    task1 = f"task-{uuid.uuid4().hex[:8]}"
    task2 = f"task-{uuid.uuid4().hex[:8]}"

    # Gherkin 要求 R1/T1 与 R2/T2 在查询 R1 之前都已存在。
    # 先后用真实控制台 API 在工作台预置任务（每次创建后立即校验该工作台仅含自己的任务），
    # 然后才启动 app 查询 R1；这样“R1 不列出 T2”才是有意义的断言。
    for workbench, task in ((r1, task1), (r2, task2)):
        pre = start_external_console(python, gdd, workbench, port)
        try:
            create_task(port, task)
            names = [item["name"] for item in get_json(port, "/api/overview")["tasks"]]
            check(names == [task], f"预置 {workbench.name} 任务列表异常：{names}")
        finally:
            stop_external(pre)
            wait_port_free(port)

    write_config(config_dir, gdd, python, r1)
    proc = run_app(app_bin, config_dir, log_dir, port)
    check(proc.returncode == 0, f"R1 启动失败：{read_log(log_dir)[-800:]}")
    ready = parse_ready(read_log(log_dir))
    pid1 = int(ready["pid"])
    try:
        names1 = [item["name"] for item in get_json(port, "/api/overview")["tasks"]]
        check(names1 == [task1], f"R1 任务列表异常（应仅 T1，T2 已存在于 R2）：{names1}")
        check(task2 not in names1, f"R1 不应列出 R2 的任务 {task2}：{names1}")
        # 页面由同一 overview 数据驱动（任务名在前端渲染，不在静态 HTML 中；
        # 页面 JS 使用相对路径 api('api/overview')）。
        check("api/overview" in get_text(port, "/"), "页面应通过 overview 接口加载任务列表")
    finally:
        terminate(pid1)
        wait_port_free(port)

    write_config(config_dir, gdd, python, r2)
    log_dir2 = tmp / "d7" / "logs-2"
    proc2 = run_app(app_bin, config_dir, log_dir2, port)
    check(proc2.returncode == 0, f"R2 启动失败：{read_log(log_dir2)[-800:]}")
    ready2 = parse_ready(read_log(log_dir2))
    pid2 = int(ready2["pid"])
    try:
        names2 = [item["name"] for item in get_json(port, "/api/overview")["tasks"]]
        check(names2 == [task2], f"R2 任务列表异常（应仅 T2，T1 已存在于 R1）：{names2}")
        check(task1 not in names2, f"R2 不应列出 R1 的任务 {task1}：{names2}")
        return (
            f"预置 R1={task1}、R2={task2}（均在查询前创建）；"
            f"R1 仅列 {task1} 不列 {task2}；R2 仅列 {task2} 不列 {task1}"
        )
    finally:
        terminate(pid2)
        wait_port_free(port)


@scenario("D9 无效配置不启动控制台")
def d9(tmp: Path, app_bin: Path, python: str) -> str:
    port = pick_port()
    ensure_port_free(port)
    base = tmp / "d9"
    workbench = base / "workbench"
    missing_gdd = base / "no-such-gdd"
    config_dir = base / "config"
    log_dir = base / "logs"
    write_config(config_dir, missing_gdd, python, workbench)

    proc = run_app(app_bin, config_dir, log_dir, port)
    log_text = read_log(log_dir)
    check(proc.returncode == 1, f"无效配置应以 1 退出，实际 {proc.returncode}")
    check("event=error" in log_text, f"日志缺少 error：{log_text[-800:]}")
    check(str(missing_gdd) in log_text, "错误未包含具体路径")
    check(console_pids(workbench) == [], "无效配置不应启动 gdd.ui")
    return f"退出码 1，错误包含 {missing_gdd}，无 gdd.ui 子进程"


@scenario("D11 8765（测试端口）被非 GDD 服务占用")
def d11(tmp: Path, app_bin: Path, python: str) -> str:
    port = pick_port()
    ensure_port_free(port)
    gdd, workbench = prepare(tmp, "d11")
    config_dir = tmp / "d11" / "config"
    log_dir = tmp / "d11" / "logs"
    write_config(config_dir, gdd, python, workbench)

    dummy = start_dummy(port)
    try:
        proc = run_app(app_bin, config_dir, log_dir, port)
        log_text = read_log(log_dir)
        check(proc.returncode == 1, f"应报错退出，实际 {proc.returncode}")
        check("event=error" in log_text, "日志缺少 error")
        check(str(port) in log_text, "错误未包含端口")
        check(
            ("非 GDD" in log_text) or ("占用" in log_text),
            f"错误未说明端口冲突：{log_text[-800:]}",
        )
        check(console_pids(workbench) == [], "不应启动第二个控制台")
        return f"端口 {port} dummy 服务导致退出码 1，未启动 gdd.ui"
    finally:
        dummy.shutdown()
        dummy.server_close()


@scenario("D12 app 拉起的控制台在 app 退出后继续运行并可复用")
def d12(tmp: Path, app_bin: Path, python: str) -> str:
    port = pick_port()
    ensure_port_free(port)
    gdd, workbench = prepare(tmp, "d12")
    config_dir = tmp / "d12" / "config"
    log_dir = tmp / "d12" / "logs"
    write_config(config_dir, gdd, python, workbench)

    proc = run_app(app_bin, config_dir, log_dir, port)
    check(proc.returncode == 0, f"首次启动失败：{read_log(log_dir)[-800:]}")
    ready = parse_ready(read_log(log_dir))
    pid = int(ready["pid"])
    try:
        check(pid_alive(pid), "app 退出后控制台应存活")
        check(get_json(port, "/api/overview").get("ok") is True, "控制台 API 不可用")
        check(console_pids(workbench) == [pid], "应有且仅有一个控制台进程")

        log_dir2 = tmp / "d12" / "logs-2"
        proc2 = run_app(app_bin, config_dir, log_dir2, port)
        check(proc2.returncode == 0, f"二次启动失败：{read_log(log_dir2)[-800:]}")
        ready2 = parse_ready(read_log(log_dir2))
        check(ready2["started"] == "false", f"二次启动应复用：{ready2}")
        check(ready2["pid"] == "-", "二次启动不应产生子进程")
        check(console_pids(workbench) == [pid], "复用后仍应只有一个控制台进程")
        return f"app 退出后 pid={pid} 存活；重启复用且无第二个进程"
    finally:
        terminate(pid)
        wait_port_free(port)


@scenario("D13 不终止外部启动的控制台")
def d13(tmp: Path, app_bin: Path, python: str) -> str:
    port = pick_port()
    ensure_port_free(port)
    gdd, workbench = prepare(tmp, "d13")
    config_dir = tmp / "d13" / "config"
    log_dir = tmp / "d13" / "logs"
    write_config(config_dir, gdd, python, workbench)

    external = start_external_console(python, gdd, workbench, port)
    original_pid = external.pid
    try:
        proc = run_app(app_bin, config_dir, log_dir, port)
        check(proc.returncode == 0, f"app 启动失败：{read_log(log_dir)[-800:]}")
        ready = parse_ready(read_log(log_dir))
        check(ready["started"] == "false", f"应复用外部控制台：{ready}")
        check(pid_alive(original_pid), "app 退出后外部控制台 PID 应存活")
        check(get_json(port, "/api/overview").get("ok") is True, "外部控制台不可访问")
        return f"外部控制台 pid={original_pid} 在 app 退出后存活且可访问"
    finally:
        stop_external(external)
        wait_port_free(port)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app",
        default=os.environ.get("GDD_APP_BIN", str(DEFAULT_APP)),
        help="gdd-console 可执行文件路径",
    )
    parser.add_argument(
        "--keep", action="store_true", help="保留临时目录（证据排查）"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="固定测试端口（默认随机；8765 空闲时可传 8765 做真实端口验证）",
    )
    args = parser.parse_args()
    global FIXED_PORT
    FIXED_PORT = args.port

    app_bin = Path(args.app)
    if not app_bin.is_file():
        print(f"FAIL: app 二进制不存在：{app_bin}", file=sys.stderr)
        print("先执行：cargo tauri build --bundles app", file=sys.stderr)
        return 1
    python = find_python()
    print(f"app={app_bin}")
    print(f"python={python}")
    print("说明：生产端口为 8765；若本机 8765 被用户控制台占用，本脚本用 GDD_CONSOLE_TEST_PORT 做隔离验证。")

    # resolve() 把 /var -> /private/var，与控制台 argv 中的真实路径保持一致。
    tmp_dir = Path(tempfile.mkdtemp(prefix="gdd-console-verify-")).resolve()
    print(f"tmp={tmp_dir}")
    try:
        for fn in (d2, d3, d5, d6, d7, d9, d11, d12, d13):
            fn(tmp_dir, app_bin, python)
    finally:
        if args.keep:
            print(f"保留临时目录：{tmp_dir}")
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    failures = [item for item in RESULTS if not item[1]]
    print()
    print(f"汇总：{len(RESULTS) - len(failures)} 通过，{len(failures)} 失败")
    if failures:
        for name, _, detail in failures:
            print(f"  FAIL {name} — {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
