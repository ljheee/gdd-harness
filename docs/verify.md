# 验证复核记录（verify）

> 节点：test；日期：2026-11-11；**本轮针对修复后的实现 `a85bf18` 重跑，并在字面 8765 上完成 D2/D3/D11**。
> 目的：让每条验收用例都能被第三方按“使用的数据 → 过程日志 → 落盘数据”复查。
> 总报告：`docs/test-report.md`；原始证据：`.gdd/nodes/test/evidence/`。

## 0. 环境与隔离约定

- 机器：macOS 26.3（arm64），Python 3.12.13（`/opt/homebrew/bin/python3.12`），Rust 1.99.1，tauri-cli 2.12.4，Tauri 2.12.5。
- 代码基线：HEAD `a85bf18`（包含 `cd3229b gdd(ui)`）；当前 `git diff --stat -- gdd/ tests/` 为空。
- 构建产物：`src-tauri/target/release/bundle/macos/GDD Console.app`
  - 二进制：`Contents/MacOS/gdd-console`
  - sha256：`87bc0907dea47025d77893f6db832877f4a3eecc6646125b00723b85591bfc19`
  - 修复点：`src-tauri/src/app.rs` 的 `show_error_dialog` 通过 `run_on_main_thread` + `NSAlert::runModal` 显示错误；日志 `event=dialog_shown`。
- **端口 8765（本轮已解除阻塞并完成字面验证）**：用户授权停掉原控制台 PID 73248 后，8765 空闲；本轮在字面 8765 上执行 headless 9/9、GUI 窗口、D9/D11 错误对话框，**全部通过**。测试后用原命令/环境恢复控制台：新 PID 630，`GET /api/overview` 200（tasks `gdd-harness`、`mall-label-manage`，templates `delivery`）。恢复后的控制台不再是本会话的祖先（本会话由 CLI runner 启动），如需复跑字面场景可安全停掉/重启它。
- **字面 8765 通道**：`scripts/verify_headless.py` 与 `scripts/verify_gui.py` 在端口为 8765 时**不注入** `GDD_CONSOLE_TEST_PORT`，走 app 生产默认路径；本轮实际执行 `verify_headless.py --port 8765`、`verify_gui.py --port 8765`、`verify_gui.py --check-error-dialogs --port 8765`。`scripts/takeover_8765.sh` 保留作为无人值守接管备选（本轮未使用）。
- **历史阻塞证据保留**：原控制台为会话祖先、lldb 被拒等排查见 `.gdd/nodes/test/evidence/round3-8765-blocker.txt`；该阻塞已由用户授权停机解除。
- 截图能力：`screencapture -x` 可截主屏；对话框可能出现在副屏（X 为负），因此本轮同时用 `screencapture -l <window-id>` / `CGWindowListCreateImage` 按窗口 ID 截取对话框内容。本轮字面 8765 截图：`literal-8765-d4-window.png`、`literal-8765-d9-error-dialog.png`、`literal-8765-d11-error-dialog.png`；上一轮历史截图 `D4-8765-window.png`、`D9-error-dialog-window.png`、`D11-error-dialog-window.png` 保留对照。
- 所有测试数据运行时生成：`tempfile.mkdtemp` 临时目录、随机任务名（`task-<8hex>`）、随机标记（`GDD-MARK-<10hex>`）、fake Python、dummy HTTP 服务。本轮 D2/D3/D4/D11 均在 8765 空闲时用临时工作台/临时 GDD 源码/dummy 服务完成，不读取或修改用户现有任务数据；上一轮 D4 的临时 symlink 适配仅作历史证据保留。
- 测试钩子（仅测试显式设置时生效）：`GDD_CONSOLE_CONFIG_DIR`/`GDD_CONSOLE_LOG_DIR`、`GDD_CONSOLE_TEST_HEADLESS=1`、`GDD_CONSOLE_TEST_NO_DIALOG`、`GDD_CONSOLE_TEST_PORT`、`GDD_CONSOLE_TEST_DUMP_PAGE=1`、`GDD_CONSOLE_TEST_CLOSE_AFTER_READY=1`。生产默认端口仍为 8765。

## D1 使用 Tauri 2 + 系统 WKWebView，不打包第二份前端（static）

**使用的数据**：`src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml`/`Cargo.lock`、构建产物 `.app/Contents`。

**过程日志**

```bash
bash scripts/verify_app.sh
# PASS: 应用包存在
# frontendDist='http://127.0.0.1:8765' windows=[] withGlobalTauri=False
# PASS: 依赖使用 Tauri 2；无 Electron/Chromium；无 HTML/JS/CSS；无 Python/GDD
# PASS: Info.plist 含 NSAllowsLocalNetworking；README/USER_GUIDE 依赖说明
# PASS: 静态检查全部通过   （退出码 0）
otool -L "GDD Console.app/Contents/MacOS/gdd-console" | grep WebKit
# /System/Library/Frameworks/WebKit.framework/...
find "GDD Console.app/Contents" -maxdepth 2 -mindepth 1
# Contents/Info.plist、Contents/MacOS/gdd-console、Contents/Resources/icon.icns
```

**落盘数据**：`.gdd/nodes/test/evidence/D1-D14-static.txt`（含 `EXIT=0`）；`Cargo.lock` 中 `tauri 2.12.5`、`wry 0.57.2`。

**结果**：通过。

## D2 8765 无监听时冷启动控制台（integration）

**使用的数据（字面 8765，已执行）**

- 启动前确认 8765 空闲（`lsof -nP -iTCP:8765 -sTCP:LISTEN` 无输出）。
- 临时 GDD 源码副本、临时工作台 `<tmp>/d2/workbench`、临时配置（`gddHome=<tmp>/gdd-src`、`workbenchRoot=<tmp>/d2/workbench`、Python 3.12.13）。
- 环境变量：`GDD_CONSOLE_TEST_HEADLESS=1`、`GDD_CONSOLE_TEST_NO_DIALOG=1`；**不设置** `GDD_CONSOLE_TEST_PORT`（app 走生产默认 8765）。
- 前置动作：用户授权停掉原占用 8765 的控制台（PID 73248）；测试后用原命令/环境恢复（新 PID 630）。

**过程日志**

```text
event=probe port=8765 phase=before_start
event=console_spawn pid=99188 port=8765 log=<tmp>/d2/logs/console.log
event=console_ready pid=99188 started=true url=http://127.0.0.1:8765/
event=headless_ready pid=99188 started=true url=http://127.0.0.1:8765/
```

`scripts/verify_headless.py` 的 d2 断言并全部通过：子进程 argv 含 `-m gdd.ui`、`--root <tmp>/d2/workbench`、`--port 8765`、`--no-open`；就绪 0.26s（<30s）；`GET /api/overview` 返回 200 且 `ok:true`、`templates`/`tasks` 为数组；监听仅 `127.0.0.1:8765`；非回环地址连接失败。
`console.log` 首行：`GDD 控制台: http://127.0.0.1:8765/（工作台目录 <tmp>/d2/workbench）`。
窗口 URL 由同一 app 的字面 GUI 冷启动运行覆盖（见 D4）：`window_created url=http://127.0.0.1:8765/`、`window_url=http://127.0.0.1:8765/`。

**落盘数据**

- `.gdd/nodes/test/evidence/literal-8765-headless.txt`（D2 行 + 9/9 PASS）
- `/private/var/folders/.../gdd-console-verify-0esfrfh7/d2/logs/app.log`、`console.log`、`config/config.json`（`--keep` 保留）
- `.gdd/nodes/test/evidence/literal-8765-gui.txt`（窗口 URL 8765）

**复现命令**

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN   # 应无输出
python3.12 scripts/verify_headless.py --port 8765 --keep
```

**结果**：**通过**。8765 无监听时 app 以生产默认端口拉起 `<python> -m gdd.ui --root <tmp>/d2/workbench --port 8765 --no-open`，0.26s 就绪，overview 200 且形状正确，监听仅 127.0.0.1，窗口 URL 为 `http://127.0.0.1:8765/`。

## D3 复用已在运行的控制台（integration）

**使用的数据（字面 8765，同一运行）**

- 8765 空闲后，先用临时 GDD 源码 + 临时工作台 `<tmp>/d3/workbench` 外部启动真实控制台（脚本内 PID 99195），并创建运行时随机任务 `task-9e0d4324`。
- 再启动 app（headless；不设置 `GDD_CONSOLE_TEST_PORT`，生产默认 8765）；app 配置指向同一临时 GDD 源码/工作台。
- 工作台内唯一任务：`task-9e0d4324`。

**过程日志**

```text
event=probe port=8765 phase=before_start
event=probe port=8765 result=reuse
event=headless_ready pid=- started=false url=http://127.0.0.1:8765/
```

`scripts/verify_headless.py` 的 d3 断言并全部通过：原控制台 PID 99195 不变；`console_pids(workbench) == [99195]`（无第二个 `gdd.ui`）；`/api/overview` tasks 仅 `task-9e0d4324`；app 未产生 `console_spawn`。
同一 8765 运行、同一临时工作台，任务列表来自原控制台工作台。

**落盘数据**：`literal-8765-headless.txt`（D3 行）；`--keep` 临时目录 `gdd-console-verify-0esfrfh7/d3/workbench/task-9e0d4324/`、`d3/logs/app.log`。

**复现命令**

```bash
python3.12 scripts/verify_headless.py --port 8765 --keep   # 8765 空闲时
```

**结果**：**通过**。同一 8765 运行满足：临时工作台 + 运行时任务 `task-9e0d4324` + app 复用（started=false）+ 无第二个 `gdd.ui` + 原 PID 不变 + 窗口 URL 8765。

## D4 窗口展示控制台页面（e2e）

**使用的数据（字面 8765 冷启动 + 临时工作台，本轮执行）**

- 构建产物 `.app`（sha256 `87bc0907dea47025d77893f6db832877f4a3eecc6646125b00723b85591bfc19`）；配置指向临时 GDD 源码副本与临时工作台 `<tmp>/workbench`。
- 运行时任务：`task-c6c2d13a`（`Orchestrator.init` 在临时工作台创建）。
- 端口：**8765**（不设置 `GDD_CONSOLE_TEST_PORT`）；环境变量：`GDD_CONSOLE_TEST_DUMP_PAGE=1`、`GDD_CONSOLE_TEST_NO_DIALOG=1`、`GDD_CONSOLE_TEST_CLOSE_AFTER_READY=1`。
- 测试前 8765 空闲；app 自行拉起控制台（`console_spawn pid=55696 port=8765`），窗口加载 8765。

**过程日志**（`literal-8765-gui.txt` / 临时目录 `gdd-console-gui-5_gkd76p/logs/app.log`）

```text
event=probe port=8765 phase=before_start
event=console_spawn pid=55696 port=8765 ...
event=console_ready pid=55696 started=true url=http://127.0.0.1:8765/
event=window_created url=http://127.0.0.1:8765/
event=window_url url=http://127.0.0.1:8765/
event=window_content {"url":"http://127.0.0.1:8765/","title":"GDD 控制台","text":"GDD 控制台...task-c6c2d13a..."}
event=probe port=8765 result=reuse   # 关闭窗口后 Dock 重开
```

- 主窗口可见（onscreen=true）1200×801；app 主进程无 TCP LISTEN；关闭窗口仅隐藏、Dock 重开重新探测、退出后控制台仍存活（pid=55696）。
- 独立窗口截图 `literal-8765-d4-window.png`：原生窗口「GDD 控制台」+ 任务 `task-c6c2d13a`。

**补充（历史适配证据）**：上一轮在生产 8765 用户工作台上用临时任务 symlink 的窗口证据 `D4-8765-window.png`（task-85e87b07）仍保留；本轮字面冷启动证据已覆盖 D4 要求。

**落盘数据**：`literal-8765-gui.txt`、`literal-8765-d4-window.png`、`gdd-console-gui-5_gkd76p/`。

**复现命令**

```bash
python3.12 scripts/verify_gui.py --port 8765 --keep   # 8765 空闲时
```

**结果**：**通过**。同一运行满足 8765 窗口 URL、WKWebView 实际加载 8765、DOM 含「GDD 控制台」与运行时任务 `task-c6c2d13a`、可见窗口与截图；配置指向临时 GDD 源码/工作台，控制台由 app 冷启动。

## D5 更新 GDD 源码无需重新构建 app（integration）

**使用的数据（字面 8765，本轮执行）**

- 临时 GDD 源码副本 A；随机标记 `GDD-MARK-49708a6751` 写入 A 的 `gdd/ui.py`
- 端口 8765（生产默认，不注入测试端口覆盖）；app 二进制 sha256 `87bc0907dea4…` 前后一致

**过程日志**（`literal-8765-headless.txt`）

```text
首次（原源码）：event=console_spawn pid=99218 port=8765 ...; event=console_ready ...; 停止控制台
修改 A 的 gdd/ui.py 写入 GDD-MARK-49708a6751 -> 不重建 app，再次启动
二次（未重建 app）：event=console_spawn pid=99240 port=8765 ...; GET / 页面包含 GDD-MARK-49708a6751
app 二进制 sha256 前后一致 87bc0907dea4...
PASS: D5 更新 GDD 源码无需重建 app — 页面包含标记 GDD-MARK-49708a6751，控制台 pid=99240（首次 99218 / 二次 99240）
```

**落盘数据**：`literal-8765-headless.txt`（D5 行）；`--keep` 临时目录 `gdd-console-verify-0esfrfh7/d5/gdd-src/gdd/ui.py`（含标记）、`logs/app.log`、`logs-2/app.log`。

**复现命令**：`python3.12 scripts/verify_headless.py --port 8765 --keep`

**结果**：通过。

## D6 控制台只监听本机（integration）

**使用的数据（字面 8765，本轮执行）**：8765；非回环 IP `11.10.137.3`；GUI app 主进程 55626；控制台 pid=99245（headless）/55696（GUI）。

**过程日志**

```text
lsof -nP -iTCP:8765 -sTCP:LISTEN  -> TCP 127.0.0.1:8765 (LISTEN)
非回环 11.10.137.3:8765 连接失败
PASS: D6 控制台只监听本机 — 监听 ['127.0.0.1:8765']；非回环连接失败
GUI：app 主进程 lsof -a -p 55626 -iTCP -sTCP:LISTEN -> 空
```

**落盘数据**：`literal-8765-headless.txt`（D6 行）；`literal-8765-gui.txt`。

**复现命令**：运行时执行上述 `lsof`/`socket` 命令。

**结果**：通过。

## D7 工作台目录决定任务列表（integration）

**使用的数据（字面 8765，本轮执行）**：临时工作台 R1/R2；任务 T1 `task-4361f42d`、T2 `task-6617e18c`；端口 8765。

**过程日志**

```text
预置（查询 R1 之前）：R1 起临时控制台 -> POST 创建 T1 task-4361f42d -> 校验 R1 仅 [T1] -> 停止；R2 起临时控制台 -> POST 创建 T2 task-6617e18c -> 校验 R2 仅 [T2] -> 停止。
R1（T1/T2 均已存在，app console pid=99316）：GET /api/overview -> tasks == ["task-4361f42d"]（不含 task-6617e18c）
停止控制台并确保端口释放
R2（T1/T2 均已存在，app console pid=99331）：GET /api/overview -> tasks == ["task-6617e18c"]（不含 task-4361f42d）
PASS: D7 工作台目录决定任务列表 — 预置 R1=task-4361f42d、R2=task-6617e18c（均在查询前创建）；R1 仅列 task-4361f42d 不列 task-6617e18c；R2 仅列 task-6617e18c 不列 task-4361f42d
```

**落盘数据**：`literal-8765-headless.txt`（D7 行）；`gdd-console-verify-0esfrfh7/d7/R1/task-4361f42d/.gdd/state.json`；`d7/R2/task-6617e18c/.gdd/state.json`。

**复现命令**：`python3.12 scripts/verify_headless.py --port 8765 --keep`

**结果**：通过。

## D8 配置校验返回可定位的错误（unit）

**使用的数据**：`src-tauri/tests/config.rs` + `common/mod.rs`；fake Python 309/310/312；临时不存在路径、缺 `gdd/ui.py`、普通文件工作台。

**过程日志**

```text
cargo test --manifest-path src-tauri/Cargo.toml
running 11 tests (tests/config.rs) ... test result: ok. 11 passed; 0 failed
```

**落盘数据**：`.gdd/nodes/test/evidence/cargo-test.txt`

**复现命令**：`cargo test --manifest-path src-tauri/Cargo.toml`

**结果**：通过；错误含字段名/路径，`validate()` 返回 `Result`，无未捕获异常。

## D9 无效配置时不启动控制台并显示错误（integration）

**使用的数据**

- 配置：headless `gddHome=<tmp>/d9/no-such-gdd`（不存在）、GUI `gddHome=<tmp>/missing-gdd`（不存在）、合法 Python、临时工作台
- GUI 模式：未设置 `GDD_CONSOLE_TEST_NO_DIALOG`；期望可见原生错误对话框
- headless 模式：`NO_DIALOG=1`，仅验证退出码/日志/无子进程

**过程日志**

headless（本轮字面 8765 运行，`literal-8765-headless.txt`）：

```text
PASS: D9 无效配置不启动控制台 — 退出码 1，错误包含 .../d9/no-such-gdd，无 gdd.ui 子进程
```

GUI（本轮 `literal-8765-dialogs.txt`）：

```text
event=error error=配置项 gddHome ... 不存在 ...
event=dialog_shown
D9 可见错误对话框：onscreen=true layer=8 bounds={'X': 746, 'Height': 393, 'Y': 200, 'Width': 236}；无 gdd.ui 子进程
PASS: D9/D11 均显示可见的原生错误对话框，且未启动 gdd.ui
```

窗口级截图 `literal-8765-d9-error-dialog.png`：标题「GDD 控制台启动失败」、gddHome 缺失路径、配置文件与应用日志路径、「好」按钮。

**落盘数据**

- `.gdd/nodes/test/evidence/literal-8765-dialogs.txt`
- `.gdd/nodes/test/evidence/literal-8765-d9-error-dialog.png`
- `.gdd/nodes/test/evidence/literal-8765-headless.txt`（D9 行）

**复现命令**

```bash
python3.12 scripts/verify_gui.py --check-error-dialogs --port 8765
# 或手动：无效 gddHome 启动 .app，等待 event=dialog_shown，按窗口 ID 截图
```

**结果**：**通过**。GUI 可见错误对话框含具体路径/配置/日志；无 `gdd.ui`；headless 行为通过。

## D10 Python 版本校验（unit）

**使用的数据**：fake Python 输出 309/310/312（`common::write_fake_python`）。

**过程日志**

```text
test python_39_is_rejected_and_310_accepted ... ok
# 309 -> Err 含 python/路径/3.10；310、312 -> Ok
```

**落盘数据**：`.gdd/nodes/test/evidence/cargo-test.txt`

**复现命令**：`cargo test --manifest-path src-tauri/Cargo.toml python_39_is_rejected_and_310_accepted -- --exact`

**结果**：通过。

## D11 8765 被非 GDD 服务占用（integration）

**使用的数据（字面 8765，本轮执行）**

- dummy 非 GDD HTTP 服务监听 **8765**（`ThreadingHTTPServer`，`/api/overview` 返回非 GDD JSON）。
- app 配置指向临时 GDD 源码/工作台；**不设置** `GDD_CONSOLE_TEST_PORT`，生产默认 8765。
- GUI 模式：未设置 `NO_DIALOG`，观察原生错误对话框。

**过程日志**

```text
event=probe port=8765 phase=before_start
event=probe port=8765 result=non_gdd detail=响应 JSON 缺少 ok:true
event=error error=127.0.0.1:8765 已被非 GDD 服务占用（响应 JSON 缺少 ok:true）...
event=dialog_shown
D11 可见错误对话框：onscreen=true layer=8；无 gdd.ui 子进程
PASS: D9/D11 均显示可见的原生错误对话框，且未启动 gdd.ui
```

- 窗口级截图 `literal-8765-d11-error-dialog.png`：标题「GDD 控制台启动失败」，正文「127.0.0.1:8765 已被非 GDD 服务占用（响应 JSON 缺少 ok:true）……」、配置/日志路径、「好」按钮。
- headless 同一 8765 dummy：app 退出码 1，未产生 `console_spawn`/`gdd.ui`。

**落盘数据**：`literal-8765-dialogs.txt`、`literal-8765-d11-error-dialog.png`、`literal-8765-headless.txt`（D11 行）。

**复现命令**

```bash
python3.12 scripts/verify_gui.py --check-error-dialogs --port 8765   # 8765 空闲时
```

**结果**：**通过**。8765 被 dummy 占用时 app 不启动第二个控制台、不把该服务加载进窗口、显示指向 8765 的可操作错误对话框。

## D12 app 启动的控制台在 app 退出后继续运行并复用（integration + e2e）

**使用的数据（字面 8765，本轮执行）**：GUI 场景控制台 PID 55696（端口 8765）；headless 场景 PID 99351（端口 8765）。

**过程日志**

```text
GUI：event=console_spawn pid=55696 port=8765 ...; event=window_created url=http://127.0.0.1:8765/；event=window_hidden；AppleScript quit；
     app 退出后 console_alive 仍包含 55696，/api/overview 可访问
headless：首次 event=console_ready pid=99351 port=8765；app 退出后 pid=99351 存活
         二次 event=probe port=8765 result=reuse；event=headless_ready pid=- started=false ...
         console_pids(workbench) == [99351]
```

**落盘数据**：`literal-8765-gui.txt`；`literal-8765-headless.txt`（D12 行）；`gdd-console-gui-5_gkd76p/logs/app.log`；`gdd-console-verify-0esfrfh7/d12/`。

**复现命令**：`python3.12 scripts/verify_gui.py --port 8765 --keep`；`python3.12 scripts/verify_headless.py --port 8765 --keep`

**结果**：通过。

## D13 不终止外部启动的控制台（integration + e2e）

**使用的数据（字面 8765，本轮执行）**：外部先启动的控制台 PID 99367（端口 8765，临时工作台）；app 随后启动并复用。

**过程日志**

```text
event=probe port=8765 result=reuse；app 退出后 PID 99367 存活且 /api/overview 可访问
PASS: D13 不终止外部启动的控制台 — 外部控制台 pid=99367 在 app 退出后存活且可访问
（历史：上一轮生产 8765 用户控制台 PID 73248 的只读复用证据仍保留在 D4-8765-reuse.txt）
```

**落盘数据**：`literal-8765-headless.txt`（D13 行）；`gdd-console-verify-0esfrfh7/d13/`；`D4-8765-reuse.txt`（历史）。

**复现命令**：`python3.12 scripts/verify_headless.py --port 8765 --keep`

**结果**：通过。

## D14 应用包不包含 Python/GDD，文档说明依赖（static）

**使用的数据**：`.app/Contents` 文件清单；`README.md`、`USER_GUIDE.md`。

**过程日志**

```text
find "$APP/Contents" (html/js/css/py/Python.framework/python3*) -> 无输出
仅 Contents/Info.plist、Contents/MacOS/gdd-console、Contents/Resources/icon.icns
README/USER_GUIDE 写明本机 Python >=3.10 与 GDD 源码；app 不打包 Python/GDD
```

**落盘数据**：`.gdd/nodes/test/evidence/D1-D14-static.txt`（文档两项 PASS）

**复现命令**：`bash scripts/verify_app.sh`

**结果**：通过。

## 补充场景 1：30 秒就绪超时（spec §2.5）

**使用的数据**：fake Python（`-c` 输出 312；其他参数睡眠 120 不监听）；临时 `gdd/ui.py`、临时工作台；端口 54065。

**过程日志**

```text
event=console_spawn pid=6986 port=54065 ...
（约 31 秒后）
event=console_timeout pid=6986 log=.../console.log
event=error error=控制台在 30 秒内未就绪（最后探测：端口尚无监听）。已终止本次拉起的子进程...
```

app 退出码 1，实测 31.1s；fake Python 无残留。

**落盘数据**：`.gdd/nodes/test/evidence/ready-timeout.txt`

**结果**：通过。

## 补充场景 2：app 崩溃不带走控制台（spec §7）

**使用的数据**：真实 `.app`、临时 GDD 源码/工作台、随机端口 53999；app 主进程 6327，其拉起控制台 6333。

**过程日志**（`app-crash-lifecycle.txt`）

```text
console_before_app_SIGKILL=alive
app_kill_signal=SIGKILL
console_after_app_SIGKILL=alive
overview_after_SIGKILL={"ok": true, "templates": [...], "tasks": []}
--- cleanup ---
console_after_cleanup=dead
```

**落盘数据**：`.gdd/nodes/test/evidence/app-crash-lifecycle.txt`

**结果**：通过（SIGKILL 后控制台仍存活且 API 可用；清理阶段才终止）。

## 补充场景 3：密钥不落日志与文件权限（spec §3、§8）

**使用的数据**：配置 `env={"GDD_AGENT_BIN":"/secret/path/do-not-log-123","SECRET_TOKEN":"sk-do-not-log-456"}`；临时 GDD 源码/工作台；端口 54664。

**过程日志**

```text
event=config ... envKeys=GDD_AGENT_BIN,SECRET_TOKEN
app.log 中密钥值出现次数: 0
mode=600 <tmp>/config/config.json
mode=600 <tmp>/logs/app.log
mode=600 <tmp>/logs/console.log
```

**落盘数据**：`.gdd/nodes/test/evidence/secret-and-permissions.txt`

**结果**：通过。

## 证据索引

| 文件 | 内容 |
|---|---|
| `literal-8765-headless.txt` | **本轮字面 8765 headless 9/9 PASS**（D2 pid=99188 / D3 复用 pid=99195 + task-9e0d4324 / D6 127.0.0.1:8765 / D11 dummy 8765 / D12 pid=99351 / D13 pid=99367） |
| `literal-8765-gui.txt` | **本轮字面 8765 GUI PASS**：window_created/window_url=8765、DOM 含 `task-c6c2d13a`、主窗口可见、app 无监听、退出后控制台 pid=55696 存活 |
| `literal-8765-dialogs.txt` | **本轮字面 8765 D9/D11 可见错误对话框 PASS**；D11 对话框明确指向 127.0.0.1:8765 |
| `literal-8765-d4-window.png` / `literal-8765-d9-error-dialog.png` / `literal-8765-d11-error-dialog.png` | 本轮字面 8765 的窗口/对话框截图 |
| `D1-D14-static.txt` | 静态检查 9 项 + `EXIT=0` |
| `cargo-test.txt` | 7 lib + 11 config + 5 supervisor，共 23 passed |
| `headless.txt` | 9 个 headless 场景 + `EXIT=0`（D2/D3/D5/D6/D7/D9/D11/D12/D13） |
| `headless-run/` | 本轮临时配置、app.log、console.log、工作台任务数据（D2/D3/D5/D6/D7/D9/D11/D12/D13） |
| `D4-gui.txt` | 隔离端口 GUI e2e 输出 + `EXIT=0` |
| `gui-run/` | GUI 场景配置、app.log、console.log、GDD 源码副本与工作台 |
| `D4-gui-window.png` | 隔离端口 GUI 截图（task-f126cdf1） |
| `D4-8765-reuse.txt` | 生产 8765 只读复用 + `EXIT=0` |
| `8765-reuse/` | 生产 8765 复用 app.log、config.json、`manual-8765-run.txt` |
| `D4-8765-run.txt` | 生产 8765 + 临时任务 `task-85e87b07` + 窗口截图记录 |
| `D4-8765-window.png` | 生产 8765 窗口级截图（2400×1602，含 task-85e87b07） |
| `D4-8765-run/` | D4 临时任务数据、app.log、config.json |
| `postfix-headless-random.txt` | 修复 D7 后的随机端口回归：9/9 PASS（D7 预置 R1/R2 均在查询前创建） |
| `round3-regression.txt` | 历史（字面 8765 解除前）：静态 9 项 PASS + cargo 23 passed + 随机端口 headless 9/9 PASS（D2 50548、D3 50560、D11 50637） |
| `round3-gui.txt` | 历史（字面 8765 解除前）：随机端口 GUI 复跑，窗口 URL 52322、`task-f5a480cc`；D9/D11 可见对话框 |
| `round3-d4-window.png` / `round3-d9-error-dialog.png` / `round3-d11-error-dialog.png` | 历史：随机端口 GUI 截图 |
| `round3-8765-blocker.txt` | 历史阻塞证据：监听者 PID 73248、进程链 `73248→mc 77733→claude 77738`、lldb attach 被拒、sudo 需密码；**已由用户授权停机解除** |
| `D2-8765-blocked-by-ancestor.txt` | 更早一轮的 8765 阻塞进程链证据（历史） |
| `D9-D11-dialogs.txt` | `--check-error-dialogs` 输出：D9/D11 可见对话框、无 gdd.ui |
| `D9-error-dialog-window.png` / `.txt` | D9 窗口级错误对话框截图与日志（gddHome 路径/配置/日志） |
| `D11-error-dialog-window.png` / `.txt` | D11 窗口级错误对话框截图与日志（127.0.0.1:53253 原因） |
| `d9-error-dialog.png` / `d11-error-dialog.png` | D9/D11 对话框截图（已用窗口级截图覆盖，内容同 `*-error-dialog-window.png`） |
| `ready-timeout.txt` | 30s 超时终止分支（31.1s） |
| `app-crash-lifecycle.txt` | app SIGKILL 后控制台存活（本轮修正证据顺序） |
| `test_ui-regression.txt` | 现有控制台 UI 回归：退出码 0，“全部通过 ✔” |
| `secret-and-permissions.txt` | 配置 env 值不落日志；config/app.log/console.log 权限 600 |
| `gate-cargo-test.txt` / `gate-verify-app.txt` / `gate-verify-headless.txt` | 门禁同款 PATH 复跑：cargo 23 passed、静态 9 项 PASS、headless 9/9 PASS |
| `history-pre-fix/` | **修复前历史证据**（旧二进制：D9/D11 无对话框采样、旧版 D2 等价端口手工诊断）；保留用于对照 DEF-01 |
