# GDD 控制台 Mac app — 实现计划

> 依据：`docs/goal.md`、`docs/spec.md`、`docs/survey.md`。
> 本节点决策扫描结论：未发现改变 `docs/goal.md` / `docs/spec.md` 已确认决定的 L0/L1 选择；本计划内的技术取舍均为 L2，记录在 `docs/decisions.md`。
> 现状基线：控制台是 `gdd/ui.py` 的单文件零依赖 HTTP 服务（`gdd/ui.py:596`、`gdd/ui.py:1250-1269`），页面与 `/api/overview` 已存在；本计划**不修改** `gdd/ui.py`，不新增控制台 API。

## 实现步骤

### 0. 前置条件与边界

- 构建目标：macOS（本机 arm64，macOS 26.3），Tauri 2，系统 WKWebView。
- 运行依赖：本机 Python ≥3.10（`pyproject.toml:9`）与 GDD 源码；app 不打包 Python/GDD。
- 构建依赖：Rust 工具链 + Tauri CLI；当前本机 `rustc`/`cargo` 缺失（`docs/survey.md` 实测），需先安装。
- 代码边界：只新增 `src-tauri/`、`scripts/` 与文档；不改 `gdd/`、`tests/` 的现有行为。
- 不引入前端构建链（无 Node/Vite/React）；Tauri 的 `frontendDist` 使用远程 URL，不嵌入页面资产。
- 任何需要改变已确认决定的做法（例如打包 Python、支持非默认端口、停止控制台 GUI、扩大 ATS 放行范围）都不在本计划内；出现时按决策定级契约提交。

### 1. Tauri 2 可行性 spike（必须先做）

目标：先验证本计划依赖的两个关键前提，再写业务代码：

1. `frontendDist` 为远程 URL、窗口由 Rust 代码创建时，Tauri 2 能加载 `http://127.0.0.1:8765/` 且不嵌入前端资产。
2. macOS WKWebView 加载 `http://127.0.0.1` 通过 ATS；若需要，`NSAllowsLocalNetworking` 是否足够。

步骤与验证：

1. 安装 Rust：
   - `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y`
   - `source "$HOME/.cargo/env"`；`rustc --version`、`cargo --version` 均有输出。
2. 安装 Tauri CLI：`cargo install tauri-cli --version "^2" --locked`；`cargo tauri --version` 输出版本。
3. 创建最小 `src-tauri`：
   - `Cargo.toml` 依赖 `tauri = "2"`、`tauri-build = "2"`；`build.rs` 调 `tauri_build::build()`。
   - `tauri.conf.json`：`build.frontendDist = "http://127.0.0.1:8765"`，`app.windows = []`，`app.withGlobalTauri = false`，`bundle.targets = ["app"]`。
   - 在 `setup` 中用 `WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))` 创建窗口；URL 先硬编码 `http://127.0.0.1:8765/`。
   - 添加 `src-tauri/Info.plist`，先只放 `NSAppTransportSecurity` → `NSAllowsLocalNetworking = true`。
4. 手动启动一个真实控制台：`python3.12 -m gdd.ui --root /tmp/gdd-spike-wb --port 8765 --no-open`。
5. `cargo tauri build --bundles app` 或 `cargo tauri dev`；启动 `.app`，确认窗口显示控制台页面（标题「GDD 控制台」、页面可交互）。
6. 检查 `.app/Contents` 无 `.html`/`.js`/`.css` 资产；检查 `lsof -nP -iTCP:8765 -sTCP:LISTEN` 只有 `127.0.0.1:8765`。
7. 记录证据：窗口截图、`app` 日志、`find` 资源清单、`plutil -p Info.plist`。

失败处理：

- 若窗口无法加载（白屏/ATS 拦截），先查看 Console.app 与 WebKit 日志；只尝试窄口径的 `NSAllowsLocalNetworking`，**不得**直接改成 `NSAllowsArbitraryLoads` 或引入 Electron/Chromium。若窄口径仍失败，停止实现并在报告中写明失败点、已排除项与推荐路径，按决策定级契约提交。
- 若 Tauri 构建强制要求一个本地入口才能构建/加载远程 URL，允许回退到**最小占位页**：只做初始化/跳转到 `http://127.0.0.1:8765/`，不含控制台 UI、不复制 `gdd/ui.py` 的 HTML；记录为 ADR-02 的 L2 回退。若连最小占位页也无法加载控制台，停止并报告，不得用本地页面复制/包装控制台 UI 绕过。

### 2. 工程骨架与静态配置

1. 建立 `src-tauri/` 标准结构：
   - `Cargo.toml`：包名 `gdd-console`，lib 名 `gdd_console_lib`，bin `gdd-console`；依赖 `tauri = "2"`、`tauri-build = "2"`、`serde`（derive）、`serde_json`、`tauri-plugin-dialog = "2"`、`thiserror = "2"`；dev-dependencies `tempfile = "3"`。
   - `build.rs`、`src/main.rs`（调 `gdd_console_lib::run()`）、`src/lib.rs`（模块导出）。
   - `tauri.conf.json`：`productName = "GDD Console"`、`identifier = "com.gdd.console"`、`version = "0.1.0"`、`build.frontendDist = "http://127.0.0.1:8765"`、`app.windows = []`、`app.security.csp = null`、`app.withGlobalTauri = false`、`bundle.active = true`、`bundle.targets = ["app"]`、`bundle.macOS.minimumSystemVersion = "10.15"`、`bundle.macOS.signingIdentity = null`。
   - `src-tauri/Info.plist`（ATS，见步骤 1）；图标用 `cargo tauri icon` 从一张 1024×1024 PNG 生成，或使用 Tauri 默认图标。
2. 更新 `.gitignore`：加入 `src-tauri/target/`、`src-tauri/gen/`；保留现有 `/node_modules`、`package-lock.json` 规则。
3. 提交 `src-tauri/Cargo.lock`（应用工程固定依赖版本）。
4. 验证：
   - `cargo tauri build --bundles app` 成功，产物为 `src-tauri/target/release/bundle/macos/GDD Console.app`。
   - `scripts/verify_app.sh`（步骤 6 创建）通过静态检查。
   - `plutil -p "$APP/Contents/Info.plist"` 含 `NSAllowsLocalNetworking`；`find "$APP/Contents" -name '*.html' -o -name '*.js' -o -name '*.css'` 为空。

### 3. 配置、Python 与工作台校验（核心库）

新增 `src/config.rs`（不依赖 Tauri，便于单测）：

1. 配置模型（JSON，`deny_unknown_fields`）：
   ```json
   {
     "version": 1,
     "gddHome": "/abs/path/to/gdd-harness",
     "python": "/abs/path/to/python3.12",
     "workbenchRoot": "/Users/<user>/gdd-workbench",
     "path": ["/Users/<user>/.local/bin", "/opt/homebrew/bin"],
     "env": {}
   }
   ```
   - `version` 缺省 1；不是 1 报配置错误。
   - `gddHome`、`python`、`workbenchRoot` 必填非空；`path`/`env` 可缺省为空。
   - `path` 中的 `~` 用 `home_dir` 展开；`env` 值按字符串传入子进程。
2. 配置路径：默认 `app.path().app_config_dir()/config.json`（macOS 为 `~/Library/Application Support/com.gdd.console/config.json`）；测试可用 `GDD_CONSOLE_CONFIG_DIR` 覆盖。日志目录默认 `app.path().app_log_dir()`；测试可用 `GDD_CONSOLE_LOG_DIR` 覆盖。
3. 首次启动：配置不存在时生成默认配置并写盘（权限 0600）：
   - `gddHome`：优先取编译期仓库根（`env!("CARGO_MANIFEST_DIR")` 的父目录）且其中存在 `gdd/ui.py`；否则留空。
   - `python`：在 `~/.local/bin`、`/opt/homebrew/bin`、`/usr/local/bin` 与 `PATH` 中依次查找 `python3.13`、`python3.12`、`python3.11`、`python3.10`、`python3`，用 `-c "import sys; print(sys.version_info[0]*100+sys.version_info[1])"` 验证 ≥310，取第一个可用绝对路径；否则留空。
   - `workbenchRoot`：`<home>/gdd-workbench`。
   - `path`：仅写入存在的 `~/.local/bin`、`/opt/homebrew/bin`、`/usr/local/bin`。
   - `env`：空对象。
4. 校验 `Config::validate`：
   - `gddHome` 存在且 `gddHome/gdd/ui.py` 是普通文件；
   - `python` 存在、可执行、版本 ≥3.10；
   - `workbenchRoot` 展开后非空；不存在则 `create_dir_all`；存在但是普通文件或不可写时报错（用临时文件做一次写测试后删除）；
   - 所有错误包含字段名/路径与可操作提示。
5. 单元测试（`src-tauri/tests/config.rs` 或 `#[cfg(test)]`）：
   - 缺 `gddHome`、路径不存在、缺 `gdd/ui.py`；
   - Python 不存在、`3.9`/`3.10`/`3.12` 的 fake 可执行文件；
   - `workbenchRoot` 为普通文件、不可写目录；
   - `version != 1`、未知字段、非法 JSON；
   - `~` 展开；首次创建权限 0600（Unix）。
   - 验证：`cargo test config` 全部通过。

### 4. 控制台探测与进程监管（核心库）

新增 `src/http_probe.rs`、`src/console.rs`、`src/supervisor.rs`：

1. `http_probe.rs`：
   - `probe(port, timeout) -> ProbeOutcome`：`Gdd` / `NoListener` / `NonGdd(detail)` / `Io(detail)`。
   - 用 `std::net::TcpStream::connect_timeout` + 手写 `GET /api/overview HTTP/1.1`（`Host: 127.0.0.1:<port>`、`Connection: close`），读响应；解析状态行与 `Content-Length`/body。
   - 200 且 JSON 对象含 `ok: true`、`templates` 数组、`tasks` 数组 → `Gdd`；连接拒绝 → `NoListener`；其他 HTTP 状态/非法 JSON/连接成功但无有效响应 → `NonGdd`/`Io`。
   - 不发送 `POST/PUT/DELETE`，不修改控制台。
2. `console.rs`：
   - `spawn_console(config, paths, port) -> Child`：argv 为 `<python> -m gdd.ui --root <workbench> --port <port> --no-open`；不经过 shell。
   - 子进程环境：继承当前环境；`PYTHONPATH` = `gddHome` + 继承的 `PYTHONPATH`；`PATH` = 配置 `path` + Python 所在目录 + 继承的 `PATH`；`PYTHONUNBUFFERED=1`；最后应用配置 `env`。
   - `stdin` 为 null；`stdout`/`stderr` 追加写入 `logDir/console.log`（0600），不使用 app 的管道；`current_dir` 为工作台目录。
   - 启动命令与 PID 写 app 日志（不记录 env 值）；子进程句柄交给 reaper 线程 `wait()`，避免僵尸；app 退出时不 kill。
3. `supervisor.rs`：
   - `DEFAULT_PORT: u16 = 8765`；`ensure_console(config, paths, port) -> Result<ConsoleHandle>`。
   - 启动前探测：`Gdd` → 复用（`started=false`，不 spawn）；`NoListener` → 启动；`NonGdd`/`Io` → 端口被非 GDD 服务占用，报错，不启动。
   - 启动后就绪轮询：每 200ms 探测，总上限 30 秒；成功返回 `ConsoleHandle { url, started, pid, log_path }`；超时只终止本次拉起的子进程，报错含 `console.log` 路径。
   - 子进程提前退出：再探测一次；若已是 GDD 控制台则复用（处理并发启动竞态），否则按启动失败报错。
   - 集成测试（`src-tauri/tests/supervisor.rs`，使用临时端口避免冲突）：
     - 冷启动：临时 GDD 副本 + 临时工作台；断言 `--root/--port/--no-open`、`/api/overview` 200、URL；用 `ps -o command= -p <pid>` 核实 argv；
     - 复用：外部先启动真实控制台；`ensure_console` 返回 `started=false`，原 PID 不变，无第二个 `gdd.ui`；
     - 工作台隔离：R1/R2 两个临时工作台，各自经 API 创建任务，`/api/overview` 只列出对应任务；
     - 端口冲突：临时 dummy HTTP 服务返回非 GDD 响应，`ensure_console` 报错且不 spawn；
     - 探测单测：有效/无效 JSON、连接拒绝、超时；
     - 清理：测试结束用 `kill -TERM <pid>` 终止自己启动的控制台。
   - 验证：`cargo test --test supervisor` 全部通过。

### 5. Tauri 壳、窗口与生命周期

新增 `src/app.rs`、`src/log.rs`、`src/headless.rs`：

1. `app.rs`：
   - `run()`：`tauri::Builder::default().plugin(tauri_plugin_dialog::init()).setup(...).on_window_event(...).on_menu_event(...).build(...).run(...)`。
   - `setup` 在主线程计算 `RuntimePaths`（Tauri `app_config_dir`/`app_log_dir`，或 env 覆盖），随后在后台线程执行 `Config::load_or_create` + `ensure_console`；成功后 `run_on_main_thread` 创建窗口；失败后显示原生错误对话框（`tauri-plugin-dialog`）并退出。
   - 窗口：`WebviewWindowBuilder::new(app, "main", WebviewUrl::External(handle.url))`，标题「GDD 控制台」，1200×800，最小 800×600，居中。**app 不创建窗口直到控制台就绪**，因此不会出现空白窗口。
   - 关闭窗口：`WindowEvent::CloseRequested` 时隐藏窗口并 `api.prevent_close()`；app 继续运行。
   - 退出：`on_menu_event` 捕获默认菜单 id `quit`，设置 `quitting=true` 后 `app.exit(0)`；关闭窗口时若 `quitting` 为 true 则放行。若默认菜单事件顺序不可靠，改用自定义 Quit 菜单项（同 id 语义）。
   - Dock 重新打开：`RunEvent::Reopen { has_visible_windows }` 为 false 时，在后台重新 `ensure_console`（先探测，活着则复用），成功后显示/聚焦窗口并 `navigate(url)`；失败则显示错误对话框并退出。
   - 并发保护：`AtomicBool`/`Mutex` 防止启动线程与 Reopen 同时执行 `ensure_console`。
   - 日志：`log_dir/app.log`（0600）记录 `event=config`、`event=probe`、`event=console_ready`、`event=window_created`、`event=error` 等；错误对话框包含配置路径与相关日志路径。
   - 测试钩子（仅测试，不改变默认行为）：
     - `GDD_CONSOLE_CONFIG_DIR` / `GDD_CONSOLE_LOG_DIR`：隔离配置/日志；
     - `GDD_CONSOLE_TEST_HEADLESS=1`：在 `main` 中走无 GUI 的 `headless::run()`，直接执行同一套配置/监管核心，成功/失败写日志后退出；
     - `GDD_CONSOLE_TEST_NO_DIALOG=1`：错误时只写日志并退出 1，不弹对话框（供自动化）；
     - `GDD_CONSOLE_TEST_EXIT_AFTER_READY=1`：正常创建窗口后写 `event=test_exit` 并退出，用于验证控制台继续运行。
2. `headless.rs`：
   - 复用 `config`/`supervisor` 核心；从 env 或默认目录构建 `RuntimePaths`；`ensure_console` 成功后写 `event=headless_ready pid=... started=... url=...` 并退出 0；失败写 `event=error ...` 并退出 1。
   - 不启动 Tauri、不创建窗口，便于无 GUI 自动化验证生命周期。
3. `log.rs`：简单的 `Mutex<File>` + `OnceLock` 日志器；每行带 unix 秒时间戳；`flush`；不写配置 `env` 的值。
4. 验证：
   - `cargo build` 通过；
   - 手动启动 `.app`：窗口显示控制台；关闭窗口后 app/Dock 仍在；点击 Dock 重新显示；`Cmd+Q` 后 app 退出且控制台进程仍在；
   - `GDD_CONSOLE_TEST_HEADLESS=1` + 临时配置：日志出现 `headless_ready`，退出码 0；
   - `GDD_CONSOLE_TEST_NO_DIALOG=1` + 无效配置：日志出现 `error`，退出码 1，无 `gdd.ui` 子进程。

### 6. 静态检查脚本与应用级验证

1. `scripts/verify_app.sh`（D1/D14）：
   - 解析 `src-tauri/tauri.conf.json`：`build.frontendDist` 为 `http://127.0.0.1:8765`（或指向本地最小占位入口），`app.windows` 为空，`withGlobalTauri=false`；
   - `cargo tree --manifest-path src-tauri/Cargo.toml` 无 electron/chromium；
   - 应用包内无控制台页面/前端副本：首选要求 `find "$APP/Contents" -name '*.html' -o -name '*.js' -o -name '*.css'` 为空；若存在本地占位入口，则只允许一个仅做初始化/跳转的占位页，检查其内容包含 `http://127.0.0.1:8765/` 且不含控制台 UI 标记；
   - `find "$APP/Contents" -name 'Python.framework' -o -name 'python3*' -o -path '*gdd/ui.py'` 为空；
   - `plutil -p "$APP/Contents/Info.plist"` 含 `NSAllowsLocalNetworking`；
   - 输出 `PASS`/失败项并以非零退出。
2. 应用级 headless 验证（D2/D3/D5/D7/D9/D11/D12/D13）：
   - 用临时目录复制 `gdd/` 为 GDD 副本、生成临时工作台/配置、设置 `GDD_CONSOLE_CONFIG_DIR`/`GDD_CONSOLE_LOG_DIR`；
   - 启动 `GDD Console.app/Contents/MacOS/gdd-console`，`GDD_CONSOLE_TEST_HEADLESS=1`；
   - 从 `app.log` 读取 `pid`/`started`/`url`，用 `curl`/Python 调 `/api/overview`，用 `ps`/`pgrep` 核实进程数；
   - D5：先启动，停止控制台，修改副本的 `gdd/ui.py` 加入运行时随机标记，重启 app（不重建），页面抓取含标记，`shasum` 确认 app 二进制哈希不变；
   - D7：R1/R2 两轮，每轮通过控制台 API 创建随机任务，确认只列出对应任务；
   - D12/D13：headless app 退出后 `kill -0 <pid>` 确认控制台存活；重启 app 确认 `started=false` 且无第二个进程；外部启动的控制台同理；
   - D9/D11：无效配置/占用 8765 的 dummy 服务，确认 app 退出 1、日志错误、无 `gdd.ui` 子进程；另用正常 app + 无 `NO_DIALOG` 截图错误对话框。
3. 正常 app 的 e2e（D4）：
   - 启动 `.app`（临时配置、工作台预置随机任务），等待 `event=window_created`；
   - 用 `screencapture` 截屏，检查窗口显示「GDD 控制台」与任务名；用 `osascript` 或 app 日志确认窗口 URL；保留截图作为证据；
   - 若 macOS 可访问性权限不足，保留截图 + 日志作为人工核对证据，并在测试报告中注明。
4. D6：在 app/控制台运行时执行 `lsof -nP -iTCP:8765 -sTCP:LISTEN`（只应 `127.0.0.1`）；用 `ifconfig` 取非回环 IP 连接 8765 应失败；`lsof -p <app 主进程>` 无 TCP LISTEN。
5. 验证：`scripts/verify_app.sh` 退出 0；headless 场景全部通过；D4 截图存在且内容正确。

### 7. 文档与最终回归

1. 更新 `README.md`：新增 Mac app 章节（前置依赖、构建命令、配置路径/字段、启动/关闭/退出语义、日志路径、停止控制台的终端方式）。
2. 更新 `USER_GUIDE.md`：新增 Mac app 使用说明（可与第 10 章并列），说明配置文件、默认工作台、8765 限制、非默认端口不支持、Web 更新需重启控制台。
3. 更新 README 的目录树（`src-tauri/` 说明）；确认 `.gitignore` 忽略目标产物。
4. 最终回归：
   - `cargo test`（单元 + 集成）全绿；
   - `cargo tauri build --bundles app` 成功；
   - `scripts/verify_app.sh` 退出 0；
   - headless 场景与应用级 e2e 结果记录到测试证据；
   - 确认 `git diff -- gdd/ tests/` 为空（未改现有控制台/测试）。
5. 交接到 test 节点：在 `docs/impl-ledger.md` 中逐项核销；未实测项标注「未验证」及原因。

### DoD 覆盖矩阵

| DoD | 实现步骤 | 验证方式 |
|---|---|---|
| D1 | 2、6 | `scripts/verify_app.sh` 检查 Tauri 配置、无 Electron/Chromium、无控制台前端副本；占位入口（如有）仅初始化/跳转 |
| D2 | 3、4、6 | supervisor 集成测试 + headless app 冷启动；检查 argv、`/api/overview`、URL |
| D3 | 4、6 | 外部控制台复用测试；PID 不变、无第二个 `gdd.ui` |
| D4 | 5、6 | 正常 `.app` 启动截图 + 窗口 URL/标题证据 |
| D5 | 6 | 停止控制台 → 改 `gdd/ui.py` 标记 → 重启 app → 页面含标记；app 二进制哈希不变 |
| D6 | 4、6 | `lsof` 只监听 127.0.0.1；非回环地址连接失败；app 主进程无 TCP LISTEN |
| D7 | 4、6 | R1/R2 临时工作台 + 随机任务；`/api/overview` 只列对应任务 |
| D8 | 3 | 配置校验单元测试（缺字段/路径不存在/普通文件工作台） |
| D9 | 3、5、6 | 无效配置 headless 退出 1、无子进程；正常 app 错误对话框截图 |
| D10 | 3 | fake Python 版本 3.9/3.10/3.12 的单元测试 |
| D11 | 4、6 | 8765 dummy 非 GDD 服务；app 报错、不 spawn |
| D12 | 6 | headless app 启动后退出；控制台存活；重启复用、无第二个进程 |
| D13 | 6 | 外部启动控制台；app 退出后 PID 仍存活 |
| D14 | 6、7 | `scripts/verify_app.sh` 检查 app 无 Python/GDD；README/USER_GUIDE 依赖说明 |

## ADR

### ADR-01 构建工具链：Rust + `cargo tauri`

- 选项：`cargo install tauri-cli`（Cargo CLI）vs npm `@tauri-apps/cli`。
- 取舍：本机已有 Node/npm，但仓库当前是 Python 工程且 `.gitignore` 忽略了 `package-lock.json`；引入 npm 会新增 Node 工具链与锁文件策略。Rust 工具链是 Tauri 构建的硬前置，无论如何都要装。
- 决定：使用 `cargo install tauri-cli --version "^2" --locked`，构建命令 `cargo tauri build --bundles app`；提交 `Cargo.lock`。
- 原因：依赖最少、版本由 Cargo.lock 固定、不引入前端构建链。

### ADR-02 页面加载：`frontendDist` 远程 URL + Rust 创建窗口

- 选项：本地占位页 + `navigate` vs 直接以远程 URL 作为 `frontendDist` 并在控制台就绪后由 Rust 创建窗口。
- 事实：Tauri 2.12.5 配置 schema 明确 `frontendDist` 接受远程 URL，且“不会嵌入 assets，应用默认加载该 URL”；`WindowConfig.url` 也支持外部 URL（已核对 schema `https://schema.tauri.app/config/2`）。
- 决定：首选 `build.frontendDist = "http://127.0.0.1:8765"`；`app.windows = []`；在 `ensure_console` 成功后用 `WebviewWindowBuilder::new(..., WebviewUrl::External(url))` 创建窗口。
- 回退：若 Tauri 构建强制要求本地入口，使用最小占位页（仅初始化/跳转，不含控制台 UI）；仍满足 `docs/spec.md` §6 与 D1 的占位入口约束。
- 原因：不打包任何前端副本（满足 D1/D14），窗口只在控制台就绪后出现，不产生空白页；比本地占位页更直接。

### ADR-03 配置持久化：`app_config_dir()/config.json`（v1）

- 选项：JSON 文件 vs Tauri Store 插件 vs UserDefaults vs 环境变量。
- 取舍：Tauri Store 引入插件与不透明的存储格式；UserDefaults 不便用户编辑/迁移；环境变量在 GUI 启动时不可靠（从 Finder 启动不继承 shell 环境）。JSON 文件可被用户直接编辑，符合“改 GDD 源码/更新 Web 不需要重发 app”。
- 决定：`~/Library/Application Support/com.gdd.console/config.json`，字段 `version/gddHome/python/workbenchRoot/path/env`；首次启动自动生成，权限 0600；未知字段/版本错误报配置错误；`GDD_CONSOLE_CONFIG_DIR`/`GDD_CONSOLE_LOG_DIR` 仅供测试覆盖。
- 原因：显式、可审计、可测试；无需额外插件；支持可选 PATH/env 以解决 GUI 启动环境问题。

### ADR-04 控制台探测：手写本机 HTTP/1.0 请求 + `serde_json`

- 选项：手写 TCP/HTTP vs `ureq` vs `reqwest`。
- 事实：控制台是本地 `BaseHTTPRequestHandler`，`/api/overview` 返回固定 JSON，HTTP/1.0 + `Content-Length`（`gdd/ui.py:1086-1089`、`gdd/ui.py:1018-1025`）；不需要 TLS/重定向/连接池。
- 决定：用 `TcpStream::connect_timeout` + 短读写超时 + 手写 `GET /api/overview`，`serde_json` 校验 `ok/templates/tasks`；不引入 HTTP 客户端依赖。
- 原因：依赖最少、行为可控、可单测；避免为本地探针引入异步运行时或 TLS 依赖。

### ADR-05 生命周期：控制台独立于 app 存活

- 选项：退出 app 时 kill 子进程 vs 让子进程继续运行。
- 事实：goal/spec 已确认“窗口关闭/app 退出后控制台继续运行”（`docs/spec.md` C5、§7）；GDD 会话可能运行 7200 秒（`gdd/runner.py:5-10`）。
- 决定：子进程 `stdin=null`、stdout/stderr 重定向到日志文件；app 不向子进程发送终止信号；用 reaper 线程 `wait()` 回收退出状态；窗口关闭改为隐藏，`Cmd+Q` 通过 `quitting` 标志放行退出；Dock `Reopen` 时重新 `ensure_console`。
- 原因：不打断长任务；避免孤儿僵尸进程（reaper）；符合 spec 生命周期。

### ADR-06 错误报告：原生对话框（`tauri-plugin-dialog`）

- 选项：原生对话框 vs 本地错误页 vs 仅日志。
- 事实：配置错误/启动超时可能发生在窗口创建之前，没有页面可显示；spec §8 要求用户可见的可操作错误。
- 决定：加入 `tauri-plugin-dialog`，Rust 侧显示原生错误消息（含字段/路径/日志路径）；测试用 `GDD_CONSOLE_TEST_NO_DIALOG` 跳过对话框只写日志。
- 原因：窗口未创建时也能提示；不向远程页面暴露 IPC；错误路径可测试。

### ADR-07 测试策略：核心库测试 + headless app + 单条窗口 e2e

- 选项：`tauri-driver`/AppleScript 全 GUI 自动化 vs 核心库测试 + app 二进制 headless 模式 + 截图 e2e。
- 事实：macOS WKWebView 的 WebDriver 支持有限，AppleScript/可访问性需要额外权限；DoD 允许 D4 在自动化不可用时用截图人工核对（`docs/goal.md` D4）。
- 决定：`config`/`supervisor` 核心用 Rust 单元/集成测试（临时端口）；app 二进制用 `GDD_CONSOLE_TEST_HEADLESS=1` 验证生命周期/复用/端口冲突；D4 用正常 app + `screencapture` + app 日志。
- 原因：大部分 DoD 可在无 GUI 下稳定验证；只保留一条窗口 e2e，符合反模式自查。

### ADR-08 ATS：优先 `NSAllowsLocalNetworking`

- 选项：不加 ATS 例外 vs `NSAllowsLocalNetworking` vs `NSAllowsArbitraryLoads(InWebContent)`。
- 事实：survey 风险 7 指出 WKWebView 加载 `http://127.0.0.1` 的 ATS/远程 URL 配置未验证；spec §9 要求窗口只加载本机控制台。
- 决定：`src-tauri/Info.plist` 只加 `NSAllowsLocalNetworking`；步骤 1 的 spike 验证；若仍失败，停止并报告，不擅自扩大到任意 HTTP 加载。
- 原因：最小放行面；避免为本地页面放宽全局 ATS。

## 风险与待确认

- **Tauri 远程 URL/ATS 尚未实测**：步骤 1 的 spike 是第一道验证；若失败，按步骤 1 的失败处理停止，不把未验证假设当通过。
- **Rust 工具链/网络**：本机缺 `rustc`/`cargo`，安装 rustup 与 `cargo install tauri-cli` 需要网络与磁盘；若权限/网络失败，按环境问题报告，不伪造构建结果。
- **GUI e2e 自动化**：D4 可能需要人工截图；若可访问性权限不足，按 DoD 的降级方式保留截图 + 日志。
- **配置自动探测**：首次启动的 `gddHome` 使用编译期仓库路径；换机器/移动仓库后需用户编辑配置；这是 spec C4（不承诺独立运行）的边界。
- **测试钩子**：`GDD_CONSOLE_CONFIG_DIR`/`LOG_DIR`/`TEST_HEADLESS`/`TEST_NO_DIALOG`/`TEST_EXIT_AFTER_READY` 仅用于测试，不构成产品对外契约；README 只在开发者/测试章节说明。
- **不纳入本计划**：非默认端口发现、独立运行/捆绑 Python、GUI 工作台选择器、停止控制台 GUI、自动重启、签名/公证/Mac App Store、多实例协调（均见 `docs/spec.md` §12）。
