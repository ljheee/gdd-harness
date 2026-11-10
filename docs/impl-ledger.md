# 实现核销台账

> 节点：implement；依据：`docs/plan.md`、`docs/spec.md`、`docs/goal.md`。
> 验证环境：macOS 26.3（arm64）、Python 3.12.13、Rust 1.99.1、tauri-cli 2.12.4、Tauri 2.12.5。
> 证据目录：`.gdd/nodes/implement/evidence/`。
> 重要环境事实：本机 8765 被用户正在使用的 GDD 控制台占用（PID 73248，工作台
> `/Users/lijianhua04/Documents/IdeaProject`）。为不干扰用户，应用级测试用仅测试钩子
> `GDD_CONSOLE_TEST_PORT` 选择空闲端口做隔离验证；生产 8765 路径另做“只读复用”实测，
> 并由 `supervisor::DEFAULT_PORT == 8765` 的单元测试守护。未杀死或重启用户控制台。

## 核销台账

### 计划步骤核销

| ID | 处理结果 | 代码位置（文件+行号） | 证据（命令/输出） | 待办 |
|---|---|---|---|---|
| P0 前置条件 | 已安装 Rust 1.99.1 与 tauri-cli 2.12.4；Python 3.12.13 可用；网络可达 crates.io/static.rust-lang.org | — | `rustup` 安装输出；`cargo tauri --version` → `tauri-cli 2.12.4`；`cargo fetch` 成功 | 无 |
| P1 可行性 spike | 已验证：`frontendDist` 为远程 URL 可构建；WKWebView 可加载 `http://127.0.0.1:<port>/`；`NSAllowsLocalNetworking` 足够，未使用 `NSAllowsArbitraryLoads` | `src-tauri/tauri.conf.json:7`、`src-tauri/Info.plist:8`、`src-tauri/src/app.rs:230` | `cargo tauri build --bundles app` 退出 0（`.gdd/nodes/implement/evidence/build.log`）；`verify_gui.txt` 记录 `window_url=http://127.0.0.1:<port>/` 与页面 DOM；`plutil -p Info.plist` 含 `NSAllowsLocalNetworking` | 无 |
| P2 工程骨架与静态配置 | 已建 `src-tauri/`（Cargo.toml/build.rs/main.rs/lib.rs/tauri.conf.json/Info.plist/icons/Cargo.lock）；`.gitignore` 增加 `src-tauri/target/`、`src-tauri/gen/` | `src-tauri/Cargo.toml`、`src-tauri/tauri.conf.json:1-31`、`.gitignore` | 同上构建成功；`bash scripts/verify_app.sh` 退出 0 | 无 |
| P3 配置、Python 与工作台校验 | 已实现 JSON 配置（deny_unknown_fields、camelCase、version=1）、首启生成（0600）、`~` 展开、GDD/Python/工作台校验；错误含字段名与路径 | `src-tauri/src/config.rs:21`（模型）、`:89`（load_or_create）、`:138`（validate）、`:270`（RuntimePaths）；测试 `src-tauri/tests/config.rs` | `cargo test`：`tests/config.rs` 11 passed；含 3.9 拒绝/3.10 与 3.12 通过、普通文件工作台、未知字段、非法 JSON、0600 权限 | 无 |
| P4 控制台探测与进程监管 | 已实现只读 `GET /api/overview` 探测（Gdd/NoListener/NonGdd/Io）、argv 数组拉起、环境注入、日志重定向、就绪轮询（200ms，上限 30s）、超时只杀本次子进程、reaper 回收 | `src-tauri/src/http_probe.rs:27`、`src-tauri/src/console.rs:17`、`src-tauri/src/supervisor.rs:13`（DEFAULT_PORT）、`:57`（ensure_console）；测试 `src-tauri/tests/supervisor.rs` | `cargo test`：`tests/supervisor.rs` 5 passed（冷启动 argv、复用无第二进程、非 GDD 端口拒绝、提前退出带日志路径）；`verify_headless.txt` D2/D3/D11 | 就绪超时（30s 后 kill）分支未做运行时演练（代码审查通过，见未决项） |
| P5 Tauri 壳、窗口与生命周期 | 已实现就绪后创建窗口、仅允许 `127.0.0.1:<port>` 导航、关闭窗口隐藏、Dock 重开重新探测并显示、退出不杀控制台、原生错误对话框（测试可跳过） | `src-tauri/src/app.rs:26`（run）、`:230`（create_or_show_window）、`:271`（测试关闭钩子）、`:291`（测试 DOM 钩子）、`:312`（fail）；`src-tauri/src/headless.rs:9` | `verify_gui.txt`：窗口 URL、DOM 标题「GDD 控制台」、任务名可见、关闭后 `window_hidden` 且 app 存活、Dock 重开 `window_shown`、AppleScript 退出后控制台存活；`verify_headless.txt` D12/D13 | GUI 错误对话框视觉未验证（无屏幕录制权限，见未决项） |
| P6 静态检查与应级验证 | 已提供三个脚本：静态检查、headless 应用级场景、GUI 场景 | `scripts/verify_app.sh`、`scripts/verify_headless.py`、`scripts/verify_gui.py` | 三个脚本最终均退出 0；输出见证据目录 | 无 |
| P7 文档与最终回归 | README 新增 Mac app 章节与测试命令；USER_GUIDE 新增第 11 章；目录树更新 | `README.md:49`、`README.md:215`、`USER_GUIDE.md:322` | `bash scripts/verify_app.sh` 文档两项 PASS；`git diff --stat -- gdd/ tests/` 为空 | 无 |

### DoD 核销

| ID | 处理结果 | 代码位置 | 证据（命令/输出） | 待办 |
|---|---|---|---|---|
| D1 | **已验证（static）**：Tauri 2 + 系统 WKWebView；依赖树无 Electron/Chromium；`.app` 内无 HTML/JS/CSS；窗口加载控制台 URL | `src-tauri/Cargo.toml`、`src-tauri/tauri.conf.json:7-11` | `scripts/verify_app.sh`：`frontendDist='http://127.0.0.1:8765' windows=[] withGlobalTauri=False`、无 electron/chromium、无前端资源副本，退出 0 | 无 |
| D2 | **已验证（等价端口）+ 生产 8765 冷启动未实测**：隔离端口下 argv 含 `--root/--port/--no-open`、`/api/overview` 200、URL 正确；生产 8765 被用户控制台占用，无法做无监听冷启动 | `src-tauri/src/supervisor.rs:57`、`src-tauri/src/console.rs:17` | `verify_headless.txt` D2：`pid=75808 就绪 0.25s，argv 含 --root/--port/--no-open，overview 200，监听仅 127.0.0.1`；`cargo test` 中 `default_endpoint_is_local_8765` 断言端口 8765 与 URL | 8765 空闲时补一次真实冷启动：`python3.12 scripts/verify_headless.py --port 8765`（脚本已支持固定端口），确认 30s 内 overview 200 且窗口 URL 为 8765 |
| D3 | **已验证**：外部控制台被复用，不产生第二个 `gdd.ui`，原 PID/命令行不变，任务列表来自原工作台；另在真实 8765 上验证复用用户控制台（只读） | `src-tauri/src/supervisor.rs:57` | `verify_headless.txt` D3：`复用原控制台 pid=75877，无第二个进程，任务列表含 task-69a28918`；`verify_8765_reuse.txt`：`原控制台 pid=73248 不变，窗口 URL=http://127.0.0.1:8765/` | 无 |
| D4 | **已验证（窗口 URL + WKWebView DOM）**：窗口实际加载 `http://127.0.0.1:<test-port>/`，`document.title='GDD 控制台'`，任务名出现在 body 文本；窗口创建日志存在；关闭后隐藏、Dock 重开恢复 | `src-tauri/src/app.rs:230`、`:271`、`:291` | `verify_gui.txt`：`window_url=...`、`window_content title='GDD 控制台'，body 含任务 task-2bd3bb2c`、`window_hidden`、`Dock 重新打开后重新探测并显示窗口` | 截图与 System Events 窗口标题未取得（本机屏幕录制/可访问性权限缺失）；建议人工补一张窗口截图 |
| D5 | **已验证（HTTP 页面证据）**：停止控制台 → 修改临时副本 `gdd/ui.py` 写入随机标记 → 重启 app（不重建）→ 页面包含标记；app 二进制 sha256 前后一致 | `src-tauri/src/supervisor.rs:57`、`src-tauri/src/console.rs:17` | `verify_headless.txt` D5：`页面包含标记 GDD-MARK-e591b36348，app 二进制哈希未变 (e85df1b09a1a…)，控制台 pid=75994` | 窗口内看到标记由 D4 的窗口加载机制覆盖；未在窗口内重复验证标记 |
| D6 | **已验证**：控制台仅监听 `127.0.0.1`；本机非回环 IP 连接失败；app 主进程无 TCP LISTEN | `gdd/ui.py:1262`（未改）、`src-tauri/src/app.rs:230` | `verify_headless.txt` D6：`监听 ['127.0.0.1:62628']；非回环连接失败`；`verify_gui.txt`：`app 主进程无 TCP LISTEN` | WebKit 系统辅助进程的监听未单独取证（未新增应用端口） |
| D7 | **已验证**：R1/R2 临时工作台分别只列出 T1/T2；页面通过同一 `/api/overview` 数据驱动 | `src-tauri/src/config.rs:138`、`src-tauri/src/supervisor.rs:57` | `verify_headless.txt` D7：`R1 仅列 task-319391ea；R2 仅列 task-2333d46d` | 无（任务名为前端渲染，页面文本不含静态任务名，已核对数据源） |
| D8 | **已验证（unit）**：缺字段/GDD 路径不存在/缺 `gdd/ui.py`/Python 不存在/工作台为普通文件等均返回含字段名与路径的错误，无未捕获异常 | `src-tauri/src/config.rs:138-227`、`src-tauri/tests/config.rs` | `cargo test`：11 passed | 无 |
| D9 | **已验证（无子进程 + 可操作错误）**：无效配置退出码 1，日志含 `event=error` 与具体路径，无 `gdd.ui`；GUI 错误对话框视觉未验证 | `src-tauri/src/app.rs:312`、`src-tauri/src/headless.rs:9` | `verify_headless.txt` D9：`退出码 1，错误包含 /private/var/.../no-such-gdd，无 gdd.ui 子进程` | 补一次错误对话框截图（需屏幕录制权限） |
| D10 | **已验证（unit）**：fake Python 3.9 拒绝并提示最低 3.10；3.10/3.12 通过 | `src-tauri/src/config.rs:189-204`、`src-tauri/tests/config.rs` | `cargo test`：`python_39_is_rejected_and_310_accepted ... ok` | 无 |
| D11 | **已验证**：dummy 非 GDD 服务占用端口时 app 退出 1、报端口冲突、不 spawn 第二个控制台 | `src-tauri/src/supervisor.rs:57-85` | `verify_headless.txt` D11：`端口 62667 dummy 服务导致退出码 1，未启动 gdd.ui` | 无 |
| D12 | **已验证**：app 退出的控制台继续存活且 API 可访问；再次启动复用且无第二个进程 | `src-tauri/src/console.rs:17`、`src-tauri/src/supervisor.rs:57`、`src-tauri/src/app.rs:26` | `verify_headless.txt` D12：`app 退出后 pid=76300 存活；重启复用且无第二个进程`；`verify_gui.txt`：AppleScript 退出后控制台存活 | 无 |
| D13 | **已验证**：外部启动的控制台在 app 退出后 PID 不变且可访问 | `src-tauri/src/supervisor.rs:57`、`src-tauri/src/app.rs:26` | `verify_headless.txt` D13：`外部控制台 pid=76343 在 app 退出后存活且可访问` | 无 |
| D14 | **已验证（static）**：`.app` 无 Python framework/`python3*`/`gdd` 源码；README/USER_GUIDE 写明本机 Python ≥3.10 与 GDD 源码 | `scripts/verify_app.sh`、`README.md:49-100`、`USER_GUIDE.md:322` | `scripts/verify_app.sh`：`app 包内无 Python 运行时/GDD 源码副本`、`README 说明…`、`USER_GUIDE 说明…`，退出 0 | 无 |

## 与计划的差异 / 台账待办

1. **新增仅测试钩子（L2，产品行为不变）**：`GDD_CONSOLE_TEST_PORT`、`GDD_CONSOLE_TEST_DUMP_PAGE`、
   `GDD_CONSOLE_TEST_CLOSE_AFTER_READY` 未在 `docs/plan.md` 的钩子清单中。原因：8765 被用户控制台占用，
   需要隔离端口做冷启动验证；截图权限缺失，需要从 WKWebView 读取 URL/DOM 作为窗口内容证据；窗口关闭/重开需要自动化触发。
   三者都只在显式环境变量下生效，生产路径仍固定 8765、不注入脚本、不自动关闭窗口。已写入 README 测试钩子清单。
2. **D2 生产 8765 冷启动未实测**：见 D2 待办；不得把等价端口结果当作 8765 冷启动的完整通过。
3. **D4 截图 / D9 错误对话框截图未取得**：本机 `screencapture` 报 `could not create image from display`，
   System Events 亦不可用（屏幕录制/可访问性权限缺失）。D4 已用 WKWebView 实际 URL + DOM 文本取证；
   D9 已用 headless 退出码/日志/无子进程取证。视觉部分未验证。
4. **就绪超时分支未运行时演练**：`READY_TIMEOUT=30s` 的 kill 逻辑为代码审查通过；没有构造“控制台启动后一直未就绪”的 fake 场景。
5. **首启配置自动生成**：`Config::load_or_create` 单测覆盖生成/0600/重读；未在 GUI 首次启动（无配置文件）端到端演练，避免创建用户 `~/gdd-workbench`。
6. **菜单 Cmd+Q 路径**：`quit` 菜单项事件处理为代码审查；实际退出用 AppleScript `quit`（NSApplication terminate）验证，控制台存活结论一致。
7. **`GDD_CONSOLE_TEST_EXIT_AFTER_READY`**：按计划实现但未在脚本中使用（D12/D13 用 headless 路径覆盖同一语义）。
8. **未改现有代码**：`git diff --stat -- gdd/ tests/` 为空；现有 `tests/test_ui.py` 通过（`全部通过 ✔`）。
