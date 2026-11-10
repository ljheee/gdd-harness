# 验证复核记录（verify）

> 节点：test；日期：2026-11-11；基线：`08a7180`。
> 目的：让每条验收用例都能被第三方按“使用的数据 → 过程日志 → 落盘数据”复查。
> 总报告：`docs/test-report.md`；原始证据：`.gdd/nodes/test/evidence/`。

## 0. 环境与隔离约定

- 机器：macOS 26.3（arm64），Python 3.12.13（`/opt/homebrew/bin/python3.12`），Rust 1.99.1，tauri-cli 2.12.4，Tauri 2.12.5。
- 构建产物：`src-tauri/target/release/bundle/macos/GDD Console.app`
  - 二进制：`Contents/MacOS/gdd-console`
  - sha256：`e85df1b09a1a63f9712251be73260e76f0f14b26a8d806e8f18d514d7de87004`
- **端口 8765 现状**：被用户正在使用的 GDD 控制台占用（PID 73248，`--root /Users/lijianhua04/Documents/IdeaProject --port 8765 --no-open`）。该进程是**当前测试节点 runner 的祖先**：`73248 (gdd.ui) → mc → claude → 当前 shell`。停掉 73248 会中断当前流水线的 UI/输出链路，并可能终止本节点，因此本会话内**无法**制造“8765 无监听”或“8765 被 dummy 占用”；`GDD_CONSOLE_TEST_PORT` 的等价端口结果仅作诊断，不计为 D2/D11 通过。
  - 涉及“8765 已运行”的复用场景（D3/D4/D13）对该真实实例做只读验证。
- 截图能力：`screencapture` CLI 因屏幕录制权限失败，但 `ScreenCaptureKit`（`SCScreenshotManager`）与 `CGWindowListCreateImage`（经 dlsym）可用；D4 的窗口截图是独立于 app 自报 DOM 的 OS 级窗口图像。
- 所有测试数据运行时生成：`tempfile.mkdtemp` 临时目录、随机端口、随机任务名（`task-<8hex>`）、随机标记（`GDD-MARK-<10hex>`）、fake Python 可执行文件、dummy HTTP 服务。除 D4 需要在现有 8765 控制台 root 放一个**临时 symlink**（指向临时工作台任务，测试后立即移除）外，不读取/修改用户现有任务数据。
- 测试钩子（仅测试显式设置时生效，生产默认不启用）：
  - `GDD_CONSOLE_CONFIG_DIR` / `GDD_CONSOLE_LOG_DIR`：隔离配置与日志；
  - `GDD_CONSOLE_TEST_HEADLESS=1`：无 GUI 执行同一配置/探测/拉起核心；
  - `GDD_CONSOLE_TEST_NO_DIALOG=1`：错误时只写日志并以 1 退出；
  - `GDD_CONSOLE_TEST_PORT=<port>`：覆盖探测/拉起端口（生产默认 `DEFAULT_PORT=8765`）；
  - `GDD_CONSOLE_TEST_DUMP_PAGE=1`：窗口创建后把 WKWebView 实际 URL/DOM 写入日志；
  - `GDD_CONSOLE_TEST_CLOSE_AFTER_READY=1`：窗口创建后自动触发一次关闭（验证隐藏而非退出）。
- 证据目录中的 `headless-run/`、`gui-run/`、`D4-8765-run/` 是从本轮临时目录**原样复制**的；日志内的绝对路径仍指向原临时目录，内容不变。

## D1 使用 Tauri 2 + 系统 WKWebView，不打包第二份前端（static）

**使用的数据**

- `src-tauri/tauri.conf.json`
- `src-tauri/Cargo.toml`、`src-tauri/Cargo.lock`
- 构建产物 `GDD Console.app/Contents`

**复现命令与过程日志**

```bash
bash scripts/verify_app.sh
# PASS: 应用包存在：.../GDD Console.app
# frontendDist='http://127.0.0.1:8765' windows=[] withGlobalTauri=False
# PASS: tauri.conf.json：frontendDist 为控制台 URL、app.windows 为空、withGlobalTauri=false
# PASS: 依赖使用 Tauri 2
# PASS: 依赖树无 Electron/Chromium
# PASS: app 包内无 HTML/JS/CSS 资源副本
# PASS: app 包内无 Python 运行时/GDD 源码副本
# PASS: Info.plist 含 NSAllowsLocalNetworking
# PASS: README 说明本机 Python >=3.10 与 GDD 源码依赖
# PASS: USER_GUIDE 说明 Python >=3.10
# PASS: 静态检查全部通过   （退出码 0）
```

补充独立检查：

```bash
find "GDD Console.app/Contents" -maxdepth 2 -mindepth 1
# Contents/Info.plist
# Contents/MacOS/gdd-console
# Contents/Resources/icon.icns
otool -L "GDD Console.app/Contents/MacOS/gdd-console" | grep WebKit
# /System/Library/Frameworks/WebKit.framework/... （系统 WKWebView）
cargo tree --manifest-path src-tauri/Cargo.toml --prefix none | grep -Ei 'electron|chromium'
# （无输出）
```

**落盘数据**

- `.gdd/nodes/test/evidence/D1-D14-static.txt`（含 `EXIT=0`）
- `src-tauri/Cargo.lock`：`tauri 2.12.5`、`wry 0.57.2`（macOS 使用系统 WebKit；`webkit2gtk 2.0.2` 仅为 wry 的 Linux 目标依赖，未进入 `.app`）

**结果**：通过。

## D2 8765 无监听时冷启动控制台（integration）

**使用的数据（字面 8765 部分：本会话内无法执行）**

- 8765 监听者：PID 73248，`python -m gdd.ui --root /Users/lijianhua04/Documents/IdeaProject --port 8765 --no-open`
- 进程链：`73248 (gdd.ui) → mc → claude → 当前 shell`。73248 是当前 test 节点 runner 的祖先；停掉它会中断当前流水线的 UI/输出链路，并可能终止本节点，故无法在本会话内制造“8765 无监听”。
- 直接证据：`.gdd/nodes/test/evidence/D2-8765-blocked-by-ancestor.txt`

**使用的数据（等价端口诊断，不计为 D2 通过）**

- 临时 GDD 源码副本：`<tmp>/d2/gdd-src`（从仓库 `gdd/` 复制，排除 `__pycache__`）
- 临时工作台：`<tmp>/d2/workbench`
- 临时配置 `<tmp>/d2/config/config.json`：
  ```json
  {
    "version": 1,
    "gddHome": "<tmp>/d2/gdd-src",
    "python": "/opt/homebrew/bin/python3.12",
    "workbenchRoot": "<tmp>/d2/workbench",
    "path": [],
    "env": {}
  }
  ```
- 端口：脚本随机空闲端口；本轮最终运行为 **55931**（手工复核为 55019）
- 环境变量：`GDD_CONSOLE_CONFIG_DIR`、`GDD_CONSOLE_LOG_DIR`、`GDD_CONSOLE_TEST_HEADLESS=1`、`GDD_CONSOLE_TEST_NO_DIALOG=1`、`GDD_CONSOLE_TEST_PORT=<port>`

**过程日志（等价端口诊断）**

`app.log`（最终运行）：

```text
event=headless_start config=.../d2/config/config.json
event=config ... gddHome=.../d2/gdd-src python=.../python3.12 workbenchRoot=.../d2/workbench envKeys=
event=probe port=55931 phase=before_start
event=console_spawn pid=52873 port=55931 log=.../d2/logs/console.log
event=console_ready pid=52873 started=true url=http://127.0.0.1:55931/
event=headless_ready pid=52873 started=true url=http://127.0.0.1:55931/ log=.../d2/logs/console.log
```

`ps -o command= -p 52873`：

```text
/opt/homebrew/Cellar/python@3.12/3.12.13_4/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python -m gdd.ui --root /private/.../d2/workbench --port 55931 --no-open
```

`GET /api/overview`：

```json
{"ok": true, "templates": [{"name": "delivery", "builtin": true, "desc": "需求交付流水线（七阶段+）"}], "tasks": []}
```

监听：

```text
Python  52873 lijianhua04  3u  IPv4 ...  TCP 127.0.0.1:55931 (LISTEN)
```

就绪 0.26s（<30s）；窗口 URL 格式由 D4 的 8765 实测覆盖。另一次手工冷启动（`D2-coldstart-manual.txt`）：端口 55019，pid 45407，同样记录到完整 argv、overview 与监听地址。以上均为等价端口诊断，**不是** 8765 字面验证。

**落盘数据**

- `.gdd/nodes/test/evidence/D2-8765-blocked-by-ancestor.txt`（字面 8765 不可执行证据）
- `.gdd/nodes/test/evidence/headless.txt`（D2 行 + `EXIT=0`，诊断）
- `.gdd/nodes/test/evidence/headless-run/d2/logs/app.log`、`console.log`、`config/config.json`
- `.gdd/nodes/test/evidence/D2-coldstart-manual.txt`（诊断）

**复现命令**

```bash
# 本轮无法执行；需外部操作者在 8765 空闲时执行：
python3.12 scripts/verify_headless.py --port 8765 --keep
```

**结果**：**未验证**。等价端口诊断通过（argv/就绪/overview/监听），但 Gherkin 要求 `--port 8765` 与 8765 的 overview/窗口 URL；因 8765 为当前节点 runner 的父控制台，未执行，不能记为 D2 通过。生产端口常量由 `src-tauri/src/supervisor.rs:13`（`DEFAULT_PORT = 8765`）与单测 `default_endpoint_is_local_8765` 守护，但单测不能代替 D2 的运行验证。

## D3 复用已在运行的控制台（integration）

**使用的数据**

- 临时 GDD 源码副本 `<tmp>/d3/gdd-src`、临时工作台 `<tmp>/d3/workbench`
- 外部先启动的控制台：`<python> -m gdd.ui --root <tmp>/d3/workbench --port <port> --no-open`
- 随机任务：`task-dad71cc1`（通过 `POST /api/tasks`，带 `X-GDD-Request: 1`）
- 端口：本轮最终运行的外部控制台使用随机空闲端口；生产复用验证使用真实 8765

**过程日志**

隔离端口 `app.log`：

```text
event=probe port=<port> phase=before_start
event=probe port=<port> result=reuse
event=headless_ready pid=- started=false url=http://127.0.0.1:<port>/
```

脚本断言：原控制台 PID 52903 不变；`console_pids(workbench) == [52903]`（无第二个 `gdd.ui`）；`/api/overview` 的 `tasks == ["task-dad71cc1"]`。

生产 8765 复用（窗口 + 日志，`8765-reuse/app.log`）：

```text
event=probe port=8765 phase=before_start
event=probe port=8765 result=reuse
event=window_created url=http://127.0.0.1:8765/
event=window_content {"title":"GDD 控制台","text":"...任务\ngdd-harness运行中\nmall-label-manage损坏...","url":"http://127.0.0.1:8765/"}
```

无 `console_spawn`；`/api/overview` 返回 `ok:true` 与真实任务列表。

**落盘数据**

- `.gdd/nodes/test/evidence/headless.txt`（D3 行）
- `.gdd/nodes/test/evidence/headless-run/d3/workbench/task-dad71cc1/.gdd/state.json`、`pipeline.json`
- `.gdd/nodes/test/evidence/8765-reuse/app.log`、`manual-8765-run.txt`、`D4-8765-reuse.txt`

**复现命令**

```bash
python3.12 scripts/verify_headless.py --keep
python3.12 scripts/verify_gui.py --check-8765-reuse   # 8765 有控制台时
```

**结果**：通过（隔离端口与生产 8765 均验证）。

## D4 窗口展示控制台页面（e2e）

**使用的数据（生产 8765 + 临时任务，D4 通过证据）**

- 构建产物 `.app`；配置指向临时 GDD 源码副本 `/tmp/gdd-d4c-8765-Y8Cr/gdd-src` 与临时工作台 `/tmp/gdd-d4b-8765-WYdh/wb`
- 运行时任务：`task-15816fd5`，由 `Orchestrator.init` 在临时工作台创建（`.gdd/state.json`、`pipeline.json`）
- 由于 8765 被现有控制台占用且不可停，测试期间把临时任务以 symlink 挂到现有控制台 root（`/Users/lijianhua04/Documents/IdeaProject/task-15816fd5` → 临时工作台任务），使 8765 控制台列出该运行时任务；测试后立即移除 symlink，`/api/overview` 恢复为 `gdd-harness, mall-label-manage`
- 端口：**8765**（未设置 `GDD_CONSOLE_TEST_PORT`）
- 环境变量：`GDD_CONSOLE_TEST_DUMP_PAGE=1`、`GDD_CONSOLE_TEST_NO_DIALOG=1`

**过程日志**（`D4-8765-run.txt` / `D4-8765-run/app.log`）

```text
event=probe port=8765 phase=before_start
event=probe port=8765 result=reuse
event=startup_ready
event=window_created url=http://127.0.0.1:8765/
event=window_url url=http://127.0.0.1:8765/
event=window_content {"title":"GDD 控制台","url":"http://127.0.0.1:8765/","text":"...任务
gdd-harness运行中
mall-label-manage损坏
task-15816fd5
..."}
```

8765 overview（测试期间）：

```json
{"ok": true, "templates": [{"name": "delivery", ...}], "tasks": [
  {"name": "gdd-harness", "status": "running", "current": "test", ...},
  {"name": "mall-label-manage", "status": "corrupt", ...},
  {"name": "task-15816fd5", "status": "running", "current": "project_init", ...}
]}
```

**独立窗口截图**

- 使用 `CGWindowListCreateImage`（经 dlsym 调用，按窗口 ID 捕获，不受遮挡影响）截取 `owner=GDD Console name=GDD 控制台` 窗口：
  `OK: wrote .gdd/nodes/test/evidence/D4-8765-window.png 2400x1602`
- 截图内容：原生标题栏「GDD 控制台」、任务列表包含 `task-15816fd5`、`gdd-harness`（运行中）、`mall-label-manage`（损坏）；不是 app 自报 DOM，也不是浏览器页面。
- `screencapture` CLI 仍报 `could not create image from display`；本截图走 ScreenCaptureKit/CGWindowListCreateImage，权限可用。

**落盘数据**

- `.gdd/nodes/test/evidence/D4-8765-run.txt`（config/app.log/overview/task state）
- `.gdd/nodes/test/evidence/D4-8765-window.png`（2400×1602）
- `.gdd/nodes/test/evidence/D4-8765-run/task-15816fd5/`（临时任务数据）、`app.log`、`config.json`
- `.gdd/nodes/test/evidence/D4-gui.txt`、`gui-run/`（隔离端口 GUI 脚本，补充：窗口 URL/DOM 通过；其截图 CLI 失败不影响本通过证据）
- `.gdd/nodes/test/evidence/D4-8765-reuse.txt`、`8765-reuse/`

**复现命令**

```bash
# 本轮手工复现（8765 被现有控制台占用时）：
# 1) 在临时工作台创建运行时任务；2) 临时 symlink 到现有控制台 root；
# 3) 启动 app：
#    open -n -F \
#      --env GDD_CONSOLE_CONFIG_DIR=... \
#      --env GDD_CONSOLE_LOG_DIR=... \
#      --env GDD_CONSOLE_TEST_DUMP_PAGE=1 \
#      --env GDD_CONSOLE_TEST_NO_DIALOG=1 \
#      "GDD Console.app"
# 4) 用 CGWindowListCreateImage 按窗口 ID 截图；5) 移除 symlink。
python3.12 scripts/verify_gui.py --keep   # 隔离端口补充
```

**结果**：**通过（环境适配）**。同一运行满足 8765 窗口 URL、运行时任务、独立窗口截图；配置指向临时 GDD 源码与临时工作台。唯一适配点：8765 控制台 root 是现有用户工作台，临时任务通过 symlink 注入，测试后已移除；若要求 8765 上全新 temp-root 控制台，受 D2 同一父进程阻塞限制，需外部环境复验。

## D5 更新 GDD 源码无需重新构建 app（integration）

**使用的数据**

- 临时 GDD 源码副本 A：`<tmp>/d5/gdd-src`
- 随机标记：`GDD-MARK-a7770b4a03`，写入 `<tmp>/d5/gdd-src/gdd/ui.py` 的 `GDD 控制台</h1>` 锚点
- 同一构建产物 `.app`（不重建）
- 端口：本轮最终运行 55954

**过程日志**

首次启动（`d5/logs/app.log`）：

```text
event=probe port=55954 phase=before_start
event=console_spawn pid=52944 port=55954 ...
event=console_ready pid=52944 started=true url=http://127.0.0.1:55954/
```

停止该控制台后修改源码，再次启动（`d5/logs-2/app.log`）：

```text
event=probe port=55954 phase=before_start
event=console_spawn pid=52954 port=55954 ...
event=console_ready pid=52954 started=true url=http://127.0.0.1:55954/
```

`GET /` 页面内容包含 `GDD-MARK-a7770b4a03`；app 二进制 sha256 前后均为 `e85df1b09a1a63f9712251be73260e76f0f14b26a8d806e8f18d514d7de87004`。

**落盘数据**

- `.gdd/nodes/test/evidence/headless.txt`（D5 行）
- `.gdd/nodes/test/evidence/headless-run/d5/gdd-src/gdd/ui.py`（含标记）、`logs/app.log`、`logs-2/app.log`

**复现命令**

```bash
python3.12 scripts/verify_headless.py --keep
```

**结果**：通过。

## D6 控制台只监听本机（integration）

**使用的数据**

- 隔离端口：本轮最终运行 D6 为 **55967**；D2 的 55931 同步检查
- 生产 8765：用户控制台 PID 73248
- 非回环 IP：`ipconfig getifaddr en0` → `11.10.137.3`

**过程日志**

```text
lsof -nP -iTCP:55967 -sTCP:LISTEN
# Python  52974 ... TCP 127.0.0.1:55967 (LISTEN)
# 非回环 11.10.137.3:55967 → ConnectionRefused / 失败
# app 主进程 lsof -a -p 53417 -iTCP -sTCP:LISTEN → 空
lsof -nP -iTCP:8765 -sTCP:LISTEN
# Python 73248 ... TCP 127.0.0.1:8765 (LISTEN)   （仅回环）
python3.12 -c 'socket.create_connection(("11.10.137.3", 8765), 2)'
# ConnectionRefusedError
```

**落盘数据**

- `.gdd/nodes/test/evidence/headless.txt`（D6 行）
- `.gdd/nodes/test/evidence/D4-gui.txt`（app 主进程无 TCP LISTEN）
- `.gdd/nodes/test/evidence/8765-reuse/app.log`

**复现命令**：见 `docs/test-report.md` D6；或运行时执行上述 `lsof`/`socket` 命令。

**结果**：通过。WebKit 系统辅助进程的内部连接未单独取证；app 未新增任何应用侧监听端口。

## D7 工作台目录决定任务列表（integration）

**使用的数据**

- 临时工作台 R1 `<tmp>/d7/R1`、R2 `<tmp>/d7/R2`
- 随机任务：T1 `task-3b19f802`（R1）、T2 `task-1aa71c07`（R2）
- 同一临时 GDD 源码副本、同一构建产物；配置依次指向 R1/R2

**过程日志**

```text
R1: event=console_ready ... url=http://127.0.0.1:<port>/ ; POST /api/tasks {"name":"task-3b19f802",...}
    GET /api/overview → tasks == ["task-3b19f802"]
停止控制台并确保端口释放
R2: event=console_ready ... url=http://127.0.0.1:<port>/ ; POST /api/tasks {"name":"task-1aa71c07",...}
    GET /api/overview → tasks == ["task-1aa71c07"]
```

页面通过 `/api/overview` 加载任务列表（页面 HTML 不含静态任务名）；任务名实际渲染进窗口 DOM 由 D4 证据覆盖。

**落盘数据**

- `.gdd/nodes/test/evidence/headless.txt`（D7 行）
- `.gdd/nodes/test/evidence/headless-run/d7/R1/task-3b19f802/.gdd/state.json`
- `.gdd/nodes/test/evidence/headless-run/d7/R2/task-1aa71c07/.gdd/state.json`

**复现命令**：`python3.12 scripts/verify_headless.py --keep`

**结果**：通过。

## D8 配置校验返回可定位的错误（unit）

**使用的数据**

- `src-tauri/tests/config.rs` + `src-tauri/tests/common/mod.rs`
- fake Python：`write_fake_python(dir, 309/310/312)` 生成输出固定版本号的可执行脚本
- 临时路径：不存在目录、缺 `gdd/ui.py` 的目录、普通文件工作台

**过程日志**

```text
cargo test --manifest-path src-tauri/Cargo.toml
running 11 tests (tests/config.rs)
... missing_gdd_home_reports_field ... ok
... nonexistent_gdd_home_reports_path ... ok
... gdd_home_without_package_reports_expected_structure ... ok
... nonexistent_python_reports_path ... ok
... workbench_regular_file_is_rejected ... ok
... unknown_field_and_invalid_json_are_parse_errors ... ok
test result: ok. 11 passed; 0 failed
```

**落盘数据**：`.gdd/nodes/test/evidence/cargo-test.txt`

**复现命令**：`cargo test --manifest-path src-tauri/Cargo.toml`

**结果**：通过。错误文本含字段名（`gddHome`/`python`/`workbenchRoot`/`version`）与具体路径；`validate()` 返回 `Result`，无未捕获异常。

## D9 无效配置时不启动控制台并显示错误（integration）

**使用的数据**

- 配置：`gddHome=/tmp/definitely-no-such-gdd-home`（不存在）、合法 Python、临时工作台
- GUI 模式：未设置 `GDD_CONSOLE_TEST_NO_DIALOG`，期望显示原生对话框
- headless 模式：设置 `NO_DIALOG=1`，仅验证不启动/退出码/日志

**过程日志**

headless（`d9/logs/app.log`）：

```text
event=error error=配置项 gddHome 的路径不存在或不可访问：/private/.../d9/no-such-gdd。请填写 GDD 源码根目录 config=.../d9/config/config.json appLog=.../d9/logs/app.log
```

退出码 1；`console_pids(workbench) == []`（无 `gdd.ui` 子进程）。

GUI（`D9-gui-dialog.txt`，0.1s 采样）：

```text
t=0.1s app_pid=71347 gdd_windows=0 visible_gdd_windows=0
t=0.3s app_pid=71347 gdd_windows=8 visible_gdd_windows=0
...
t=10.3s app_pid=71347 gdd_windows=8 visible_gdd_windows=0
t=10.5s app_pid=None gdd_windows=0 visible_gdd_windows=0
--- app.log ---
event=error error=配置项 gddHome 的路径不存在或不可访问：/tmp/definitely-no-such-gdd-home。...
--- gdd.ui 子进程 --- none
```

**实际结果**：多次运行约 0.4–10.5s 后退出；期间 8 个 GDD 窗口全部 `onscreen=false`，可见窗口 0；没有出现任何错误对话框。`app.rs:312` 的 `fail()` 在 `startup` 后台线程调用 `app.dialog().blocking_show()`；日志写入正常，但对话框未成为可见窗口。

**落盘数据**

- `.gdd/nodes/test/evidence/D9-gui-dialog.txt`（本轮 GUI 采样）
- `.gdd/nodes/test/evidence/D9-window-details.txt`（8 个 GDD 窗口 name 为空、onscreen=false 的逐样本记录）
- `.gdd/nodes/test/evidence/D9-dialog.txt`（上一轮采样，历史；当时只记录了日志与辅助功能权限限制，未做窗口可见性采样）
- `.gdd/nodes/test/evidence/headless.txt`（D9 行）、`headless-run/d9/`

**复现命令**

```bash
python3.12 scripts/verify_headless.py --keep   # headless 行为
# GUI：
open -n -F --env GDD_CONSOLE_CONFIG_DIR=... --env GDD_CONSOLE_LOG_DIR=... "GDD Console.app"
# 采样：每 0.1s 调用 CGWindowListCopyWindowInfo([.optionAll], ...)，
# 统计 owner=GDD Console 的窗口总数与 kCGWindowIsOnscreen=true 的可见数；
# 原始采样输出见 D9-gui-dialog.txt。
```

**结果**：**未通过**。headless 行为判据通过，但“显示指向具体配置项/路径的可操作错误”未满足：GUI 错误路径没有可见对话框。需 implement 修复后重验。

## D10 Python 版本校验（unit）

**使用的数据**

- `src-tauri/tests/common/mod.rs::write_fake_python`
  ```sh
  #!/bin/sh
  echo <version>   # 309 / 310 / 312
  ```
- 校验调用：`Config::validate()` 执行 `<fake-python> -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])'`

**过程日志**

```text
test python_39_is_rejected_and_310_accepted ... ok
# 309 → Err 含 "python"、路径、"3.10"
# 310 → Ok；312 → Ok
```

**落盘数据**：`.gdd/nodes/test/evidence/cargo-test.txt`

**复现命令**：`cargo test --manifest-path src-tauri/Cargo.toml python_39_is_rejected_and_310_accepted -- --exact`

**结果**：通过。

## D11 8765 被非 GDD 服务占用（integration）

**使用的数据**

- dummy 非 GDD HTTP 服务：`python3.12 -m http.server <random> --bind 127.0.0.1`，`GET /api/overview` 返回 404（非 GDD JSON）
- 随机端口 53725；**8765 字面占用未验证**（8765 为当前节点 runner 的父控制台，见 D2）
- GUI 模式：未设置 `GDD_CONSOLE_TEST_NO_DIALOG`；临时 GDD 源码/工作台
- headless 模式补充：`GDD_CONSOLE_TEST_PORT` + `NO_DIALOG=1`

**过程日志**

GUI（`D11-gui-dialog.txt`，0.1s 采样）：

```text
event=probe port=53725 phase=before_start
event=probe port=53725 result=non_gdd detail=HTTP 状态码 404
event=error error=127.0.0.1:53725 已被非 GDD 服务占用（HTTP 状态码 404）。...
t=0.1s app_pid=72442 gdd_windows=0 visible_gdd_windows=0
t=0.3s app_pid=72442 gdd_windows=8 visible_gdd_windows=0
...
t=2.0s app_pid=None gdd_windows=0 visible_gdd_windows=0
--- gdd.ui 子进程 --- none
```

**实际结果**：app 约 2s 后退出；无第二个 `gdd.ui`；没有把 dummy 服务加载为控制台；但同样**没有可见错误对话框**。8765 字面占用未验证。

**落盘数据**

- `.gdd/nodes/test/evidence/D11-gui-dialog.txt`（本轮 GUI 采样）
- `.gdd/nodes/test/evidence/D11-error.txt`（上一轮 GUI 采样）
- `.gdd/nodes/test/evidence/headless.txt`（D11 行）、`headless-run/d11/`

**复现命令**

```bash
# 随机端口行为：
python3.12 -m http.server <port> --bind 127.0.0.1 &
open -n -F --env GDD_CONSOLE_CONFIG_DIR=... --env GDD_CONSOLE_LOG_DIR=...   --env GDD_CONSOLE_TEST_PORT=<port> "GDD Console.app"
# 8765 字面占用需外部操作者在 8765 空闲时执行。
```

**结果**：**未通过 / 字面 8765 未验证**。“不启动第二个控制台/不加载该服务”通过，但“显示端口冲突/未知服务错误”未满足；且 8765 字面占用未验证。

## D12 app 启动的控制台在 app 退出后继续运行并复用（integration + e2e）

**使用的数据**

- GUI 场景：临时 GDD 源码副本、临时工作台、隔离端口 **56055**、app 自行拉起控制台 pid 53419
- headless 场景：临时工作台，首次启动 pid 53123 后 app 退出

**过程日志**

GUI（`gui-run/logs/app.log`）中：

```text
event=console_spawn pid=53419 ...
event=console_ready pid=53419 started=true ...
...
event=test_close_requested
event=window_hidden label=main
（AppleScript quit）
```

脚本断言：app 退出后 `console_alive(workbench)` 仍包含 53419；`/api/overview` 可访问。

headless（`headless.txt` D12 行）：

```text
首次：event=console_ready pid=53123 ...；app 退出后 pid=53123 存活
再次：event=probe ... result=reuse；event=headless_ready pid=- started=false ...
       console_pids(workbench) == [53123]
```

**落盘数据**

- `.gdd/nodes/test/evidence/D4-gui.txt`、`gui-run/logs/app.log`
- `.gdd/nodes/test/evidence/headless.txt`（D12 行）、`headless-run/d12/`

**复现命令**

```bash
python3.12 scripts/verify_gui.py --keep
python3.12 scripts/verify_headless.py --keep
```

**结果**：通过。

## D13 不终止外部启动的控制台（integration + e2e）

**使用的数据**

- 生产 8765：用户外部启动的控制台 PID 73248（未修改）
- 隔离端口：外部启动的控制台 PID 53136

**过程日志**

生产（`8765-reuse/app.log`）：

```text
event=probe port=8765 phase=before_start
event=probe port=8765 result=reuse
event=startup_ready
event=window_created url=http://127.0.0.1:8765/
（app 退出后）
ps -p 73248 -o pid=,command=
# 73248 ... -m gdd.ui --root /Users/lijianhua04/Documents/IdeaProject --port 8765 --no-open
curl http://127.0.0.1:8765/api/overview → ok:true
```

隔离端口：`event=probe ... result=reuse`，app 退出后 PID 53136 存活且可访问。

**落盘数据**

- `.gdd/nodes/test/evidence/D4-8765-reuse.txt`、`8765-reuse/app.log`、`8765-reuse/manual-8765-run.txt`、`8765-reuse/config.json`
- `.gdd/nodes/test/evidence/headless.txt`（D13 行）

**复现命令**：`python3.12 scripts/verify_gui.py --check-8765-reuse`

**结果**：通过。

## D14 应用包不包含 Python/GDD，文档说明依赖（static）

**使用的数据**

- `.app/Contents` 文件清单
- `README.md`、`USER_GUIDE.md`

**过程日志**

```text
find "$APP/Contents" \( -name '*.html' -o -name '*.js' -o -name '*.css' -o -name '*.py' \
  -o -name 'Python.framework' -o -name 'python3*' \) -print
# （无输出）
# 仅：Contents/Info.plist、Contents/MacOS/gdd-console、Contents/Resources/icon.icns
du -sh "$APP" → 10M
README.md:58  - 本机 **Python ≥3.10**
README.md:59  - 本机 **GDD 源码**（本仓库，配置项 `gddHome`）
USER_GUIDE.md:330-334  同样说明 Python ≥3.10 与 GDD 源码，且“app 不打包 Python 和 GDD 源码”
```

**落盘数据**：`.gdd/nodes/test/evidence/D1-D14-static.txt`（含文档两项 PASS）

**复现命令**：`bash scripts/verify_app.sh`

**结果**：通过。

## 补充场景 1：30 秒就绪超时（spec §2.5）

**使用的数据**

- fake Python：`-c` 输出 312；其他参数打印一行后 `sleep 120`（永不监听）
  ```sh
  #!/bin/sh
  case "$1" in
    -c) echo 312 ;;
    *) echo "fake console booting but never listening"; sleep 120 ;;
  esac
  ```
- 临时 `gdd/ui.py` 占位文件（满足配置校验）、临时工作台、随机空闲端口 56413

**过程日志**

```text
event=probe port=56413 phase=before_start
event=console_spawn pid=57133 port=56413 ...
（约 30 秒后）
event=console_timeout pid=57133 log=.../console.log
event=error error=控制台在 30 秒内未就绪（最后探测：端口尚无监听）。已终止本次拉起的子进程；请查看日志：.../console.log
```

app 退出码 1，实测耗时 **30.7s**；`ps` 检查 fake Python 无残留；端口无监听。

**落盘数据**：`.gdd/nodes/test/evidence/ready-timeout.txt`

**结果**：通过（超时上限与“只终止本次拉起的子进程”均符合 spec）。

## 补充场景 2：app 崩溃不带走控制台（spec §7）

**使用的数据**

- 真实 `.app`、临时 GDD 源码副本、临时工作台、随机端口 49363
- app 主进程 PID 36973；其自行拉起控制台 PID 37048

**过程日志**（`app-crash-lifecycle.txt`，本轮修正了原“先 kill 再记录”的顺序错误）

```text
console_before_app_SIGKILL=alive
app_kill_signal=SIGKILL
console_after_app_SIGKILL=alive
overview_after_SIGKILL={"ok": true, "templates": [{"name": "delivery", ...}], "tasks": []}
--- cleanup ---
console_after_cleanup=dead
```

**落盘数据**：`.gdd/nodes/test/evidence/app-crash-lifecycle.txt`

**结果**：通过（SIGKILL 后控制台仍存活且 `/api/overview` 可访问；清理阶段才终止）。

## 补充场景 3：密钥不落日志与文件权限（spec §3、§8）

**使用的数据**

- 临时配置 `env`：
  ```json
  {"GDD_AGENT_BIN": "/secret/path/do-not-log-123", "SECRET_TOKEN": "sk-do-not-log-456"}
  ```
- 临时 GDD 源码副本/工作台，随机端口 59329，headless 模式

**过程日志**

```text
event=config ... envKeys=GDD_AGENT_BIN,SECRET_TOKEN
app.log 中密钥值 "do-not-log-123" / "sk-do-not-log-456" 出现次数: 0（期望 0）
mode=600 <tmp>/config/config.json
mode=600 <tmp>/logs/app.log
mode=600 <tmp>/logs/console.log
```

**落盘数据**：`.gdd/nodes/test/evidence/secret-and-permissions.txt`

**结果**：通过（未记录 env 值；配置与日志仅当前用户可读写）。

## 证据索引

| 文件 | 内容 |
|---|---|
| `D1-D14-static.txt` | 静态检查 9 项 + `EXIT=0` |
| `cargo-test.txt` | 7 unit + 11 config + 5 supervisor，共 23 passed |
| `headless.txt` | 9 个 headless 场景 + `EXIT=0` |
| `headless-run/` | D2/D3/D5/D6/D7/D9/D11/D12/D13 的临时配置、app.log、console.log、工作台任务数据 |
| `D4-gui.txt` | GUI e2e 输出 + `EXIT=0` |
| `gui-run/` | GUI 场景的配置、app.log、console.log、GDD 源码副本与工作台 |
| `D4-8765-reuse.txt` | 生产 8765 只读复用 + `EXIT=0` |
| `8765-reuse/` | 生产 8765 窗口复用的 app.log、config.json，以及退出后控制台存活的 `manual-8765-run.txt` |
| `D4-8765-run.txt` | 生产 8765 + 临时任务 + 窗口截图的本轮运行记录 |
| `D4-8765-window.png` | 生产 8765 窗口的独立截图（2400×1602，含 task-15816fd5） |
| `D4-8765-run/` | D4 临时任务数据、app.log、config.json |
| `D2-8765-blocked-by-ancestor.txt` | 8765 为当前节点 runner 父控制台的进程链证据 |
| `D2-coldstart-manual.txt` | 等价端口手工冷启动诊断（argv/overview/监听/配置/日志） |
| `D9-gui-dialog.txt` | 本轮 GUI 错误路径 0.1s 采样：无可见对话框 |
| `D9-window-details.txt` | 无效配置时 8 个 GDD 窗口的 name/onscreen 逐样本记录：全部 offscreen、name 为空 |
| `D9-dialog.txt` | 上一轮 GUI 错误路径采样（历史） |
| `D11-gui-dialog.txt` | 本轮 dummy 非 GDD 服务的 GUI 采样：无可见对话框 |
| `D11-error.txt` | 上一轮 dummy 非 GDD 服务的 GUI 采样（历史） |
| `ready-timeout.txt` | 30s 超时终止分支 |
| `app-crash-lifecycle.txt` | app SIGKILL 后控制台存活（本轮修正证据顺序） |
| `test_ui-regression.txt` | 现有控制台 UI 回归：`python3.12 tests/test_ui.py`，退出码 0，末行“全部通过 ✔” |
| `secret-and-permissions.txt` | 配置 env 值不落日志；config/app.log/console.log 权限 600 |
