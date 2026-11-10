# 实现核销台账

> 节点：implement；依据：`docs/plan.md`、`docs/spec.md`、`docs/goal.md`。
> 验证环境：macOS 26.3（arm64）、Python 3.12.13、Rust 1.99.1、tauri-cli 2.12.4、Tauri 2.12.5。
> 证据目录：`.gdd/nodes/implement/evidence/`。
> 重要环境事实：本机 8765 被用户正在使用的 GDD 控制台占用（PID 73248），且它是本测试会话的
> 祖先进程（`73248 gdd.ui → 60471 mc → 60476 claude → 测试 shell`，`ps -o pid,ppid,command` 实测）。
> 终止它会同时终止当前流水线，因此无法在节点内制造“8765 无监听”。应用级冷启动测试用仅测试钩子
> `GDD_CONSOLE_TEST_PORT` 选择空闲端口；`--port 8765` 已就绪，待 8765 空闲时可直接补测真实端口。
> 门禁命令也由该控制台进程（PID 73248）以 `bash -c` 执行，继承的 PATH 不含 `~/.cargo/bin`；
> 第三轮已通过 `/opt/homebrew/bin` 符号链接使 `cargo` 在门禁 PATH 中可见（见“第三轮打回修复记录”）。

## 核销台账

### 计划步骤核销

| ID | 处理结果 | 代码位置（文件+行号） | 证据（命令/输出） | 待办 |
|---|---|---|---|---|
| P0 前置条件 | 已安装 Rust 1.99.1 与 tauri-cli 2.12.4；Python 3.12.13 可用；网络可达 crates.io/static.rust-lang.org；第三轮将工具链暴露到门禁 PATH（`/opt/homebrew/bin` 符号链接） | — | `rustup` 安装输出；`cargo tauri --version` → `tauri-cli 2.12.4`；`cargo fetch` 成功；`env -i PATH=<门禁 PATH> bash -c 'cargo --version; rustc --version'` → cargo 1.99.1 / rustc 1.99.1（`evidence/gate-toolchain.txt`） | 无 |
| P1 可行性 spike | 已验证：`frontendDist` 为远程 URL 可构建；WKWebView 可加载 `http://127.0.0.1:<port>/`；`NSAllowsLocalNetworking` 足够，未使用 `NSAllowsArbitraryLoads` | `src-tauri/tauri.conf.json:7`、`src-tauri/Info.plist:8`、`src-tauri/src/app.rs:228` | `cargo tauri build --bundles app` 退出 0（`evidence/build.log`）；`verify_gui.txt` 记录窗口实际 URL、DOM、主窗口 `onscreen=true` 1200x801；`plutil -p Info.plist` 含 `NSAllowsLocalNetworking` | 无 |
| P2 工程骨架与静态配置 | 已建 `src-tauri/`（Cargo.toml/build.rs/main.rs/lib.rs/tauri.conf.json/Info.plist/icons/Cargo.lock）；`.gitignore` 增加 `src-tauri/target/`、`src-tauri/gen/` | `src-tauri/Cargo.toml`、`src-tauri/tauri.conf.json:1-31`、`.gitignore` | 同上构建成功；`bash scripts/verify_app.sh` 退出 0 | 无 |
| P3 配置、Python 与工作台校验 | 已实现 JSON 配置（deny_unknown_fields、camelCase、version=1）、首启生成（0600）、`~` 展开、GDD/Python/工作台校验；错误含字段名与路径 | `src-tauri/src/config.rs:21`（模型）、`:89`（load_or_create）、`:138`（validate）、`:270`（RuntimePaths）；测试 `src-tauri/tests/config.rs` | `cargo test`：`tests/config.rs` 11 passed；含 3.9 拒绝/3.10 与 3.12 通过、普通文件工作台、未知字段、非法 JSON、0600 权限 | 无 |
| P4 控制台探测与进程监管 | 已实现只读 `GET /api/overview` 探测（Gdd/NoListener/NonGdd/Io）、argv 数组拉起、环境注入、日志重定向、就绪轮询（200ms，上限 30s）、超时只杀本次子进程、reaper 回收 | `src-tauri/src/http_probe.rs:27`、`src-tauri/src/console.rs:17`、`src-tauri/src/supervisor.rs:13`（DEFAULT_PORT）、`:57`（ensure_console）、`:166`（超时 kill）；测试 `src-tauri/tests/supervisor.rs` | `cargo test`：`tests/supervisor.rs` 5 passed（冷启动 argv、复用无第二进程、非 GDD 端口拒绝、提前退出带日志路径）；`verify_headless.txt` D2/D3/D11；第三轮补测超时分支：fake Python 30.4s 未就绪 → 退出码 1、`event=console_timeout`、子进程已终止无残留（`evidence/verify_timeout.txt`） | 无 |
| P5 Tauri 壳、窗口与生命周期 | 已实现就绪后创建窗口、仅允许 `127.0.0.1:<port>` 导航、关闭窗口隐藏、Dock 重开重新探测并显示、退出不杀控制台；错误时用 **AppKit NSAlert 应用级 modal** 显示可操作错误（第二轮修复，见下） | `src-tauri/src/app.rs:25`（run）、`:228`（create_or_show_window）、`:269`（测试关闭钩子）、`:289`（测试 DOM 钩子）、`:310`（fail）、`:333`（show_error_dialog）；`src-tauri/src/headless.rs:9` | `verify_gui.txt`：窗口 URL、DOM 标题「GDD 控制台」、任务名可见、主窗口 onscreen=true、关闭后 `window_hidden`、Dock 重开 `window_shown`、退出后控制台存活；`verify_error_dialogs.txt` + 两张对话框截图；`verify_headless.txt` D12/D13 | GUI 错误对话框视觉已验证；菜单 Cmd+Q 事件路径未单独演练（见未决项） |
| P6 静态检查与应级验证 | 已提供：静态检查、headless 应用级场景、GUI 场景、窗口可见性探针、D9/D11 错误对话框场景 | `scripts/verify_app.sh`、`scripts/verify_headless.py`、`scripts/verify_gui.py`、`scripts/window_list.swift` | 全部脚本最终退出 0；输出见证据目录 | 无 |
| P7 文档与最终回归 | README 新增 Mac app 章节与测试命令；USER_GUIDE 新增第 11 章；目录树更新 | `README.md:49`、`README.md:215`、`USER_GUIDE.md:322` | `bash scripts/verify_app.sh` 文档两项 PASS；`git diff --stat -- gdd/ tests/` 为空 | 无 |

### DoD 核销

| ID | 处理结果 | 代码位置 | 证据（命令/输出） | 待办 |
|---|---|---|---|---|
| D1 | **已验证（static）**：Tauri 2 + 系统 WKWebView；依赖树无 Electron/Chromium；`.app` 内无 HTML/JS/CSS；窗口加载控制台 URL | `src-tauri/Cargo.toml`、`src-tauri/tauri.conf.json:7-11` | `scripts/verify_app.sh`：`frontendDist='http://127.0.0.1:8765' windows=[] withGlobalTauri=False`、无 electron/chromium、无前端资源副本，退出 0 | 无 |
| D2 | **环境受阻（等价端口已验证）**：隔离端口下 argv 含 `--root/--port/--no-open`、`/api/overview` 200、URL 正确；生产 8765 被本测试会话的祖先控制台占用（PID 73248），终止它会终止流水线，无法制造 8765 无监听 | `src-tauri/src/supervisor.rs:57`、`src-tauri/src/console.rs:17` | `verify_headless.txt` D2：`pid=9958 就绪 0.24s，argv 含 --root/--port/--no-open，overview 200，监听仅 127.0.0.1`；`cargo test` 的 `default_endpoint_is_local_8765` 断言 8765 与 URL；`verify_8765_reuse.txt` 证明生产 8765 的 URL/探测链路 | 8765 空闲时执行 `python3.12 scripts/verify_headless.py --port 8765` 补真实冷启动（脚本已支持） |
| D3 | **已验证**：外部控制台被复用，不产生第二个 `gdd.ui`，原 PID/命令行不变，任务列表来自原工作台；另在真实 8765 上验证复用用户控制台（只读） | `src-tauri/src/supervisor.rs:57` | `verify_headless.txt` D3：`复用原控制台 pid=9975，无第二个进程，任务列表含 task-06a33265`；`verify_8765_reuse.txt`：`原控制台 pid=73248 不变，窗口 URL=http://127.0.0.1:8765/` | 无 |
| D4 | **已验证（截图 + URL + DOM）**：窗口实际加载控制台 URL，`document.title='GDD 控制台'`，body 含预置任务名；主窗口 `onscreen=true` 1200x801；截图 `evidence/d4-window.png`（画面含侧栏任务名与「GDD 控制台」）；关闭后隐藏、Dock 重开恢复 | `src-tauri/src/app.rs:228`、`:269`、`:289` | `verify_gui.txt`：`window_url=...`、`window_content ... body 含任务 task-b4c5e114`、`主窗口可见（onscreen=true）1 个，bounds=...1200x801`、`截图 .../d4-window.png`、`window_hidden`、`Dock 重新打开后重新探测并显示窗口` | 无（System Events 标题不可用，已用截图/DOM 替代） |
| D5 | **已验证（HTTP 页面证据）**：停止控制台 → 修改临时副本 `gdd/ui.py` 写入随机标记 → 重启 app（不重建）→ 页面包含标记；app 二进制 sha256 前后一致 | `src-tauri/src/supervisor.rs:57`、`src-tauri/src/console.rs:17` | `verify_headless.txt` D5：`页面包含标记 GDD-MARK-6695a299bb，app 二进制哈希未变 (87bc0907dea4…)，控制台 pid=10010` | 窗口内看到标记由 D4 的窗口加载机制覆盖；未在窗口内重复验证标记 |
| D6 | **已验证**：控制台仅监听 `127.0.0.1`；本机非回环 IP 连接失败；app 主进程无 TCP LISTEN | `gdd/ui.py:1262`（未改）、`src-tauri/src/app.rs:228` | `verify_headless.txt` D6：`监听 ['127.0.0.1:57711']；非回环连接失败`；`verify_gui.txt`：`app 主进程无 TCP LISTEN` | WebKit 系统辅助进程的监听未单独取证（未新增应用端口） |
| D7 | **已验证**：R1/R2 临时工作台分别只列出 T1/T2；页面通过同一 overview 数据驱动（JS 相对路径 `api/overview`） | `src-tauri/src/config.rs:138`、`src-tauri/src/supervisor.rs:57` | `verify_headless.txt` D7：`R1 仅列 task-27a155a4；R2 仅列 task-fc8a71c4` | 无（任务名为前端渲染，页面静态文本不含任务名） |
| D8 | **已验证（unit）**：缺字段/GDD 路径不存在/缺 `gdd/ui.py`/Python 不存在/工作台为普通文件等均返回含字段名与路径的错误，无未捕获异常 | `src-tauri/src/config.rs:138-227`、`src-tauri/tests/config.rs` | `cargo test`：11 passed | 无 |
| D9 | **已验证（GUI 可见对话框 + headless 无子进程）**：无效配置时显示可见 NSAlert（标题「GDD 控制台启动失败」，含 gddHome 路径、配置文件与应用日志路径），不启动 `gdd.ui`；headless 退出码 1 | `src-tauri/src/app.rs:310`、`:333`、`src-tauri/src/headless.rs:9` | `verify_error_dialogs.txt`：`D9 可见错误对话框：onscreen=true layer=8 bounds=...；无 gdd.ui 子进程`；截图 `evidence/d9-error-dialog.png`；`verify_headless.txt` D9 退出码 1 | 无 |
| D10 | **已验证（unit）**：fake Python 3.9 拒绝并提示最低 3.10；3.10/3.12 通过 | `src-tauri/src/config.rs:189-204`、`src-tauri/tests/config.rs` | `cargo test`：`python_39_is_rejected_and_310_accepted ... ok` | 无 |
| D11 | **已验证（GUI 可见对话框）**：dummy 非 GDD 服务占用端口时显示可见 NSAlert（含端口、原因、配置文件/日志路径），不启动第二个控制台 | `src-tauri/src/supervisor.rs:57-85`、`src-tauri/src/app.rs:333` | `verify_error_dialogs.txt`：`D11 可见错误对话框：onscreen=true layer=8 bounds=...；无 gdd.ui 子进程`；截图 `evidence/d11-error-dialog.png`；`verify_headless.txt` D11 退出码 1 | 字面 8765 占用仍受 D2 同一环境限制；错误展示已修复并实测 |
| D12 | **已验证**：app 退出的控制台继续存活且 API 可访问；再次启动复用且无第二个进程 | `src-tauri/src/console.rs:17`、`src-tauri/src/supervisor.rs:57`、`src-tauri/src/app.rs:25` | `verify_headless.txt` D12：`app 退出后 pid=10122 存活；重启复用且无第二个进程`；`verify_gui.txt`：AppleScript 退出后控制台存活 | 无 |
| D13 | **已验证**：外部启动的控制台在 app 退出后 PID 不变且可访问 | `src-tauri/src/supervisor.rs:57`、`src-tauri/src/app.rs:25` | `verify_headless.txt` D13：`外部控制台 pid=10147 在 app 退出后存活且可访问` | 无 |
| D14 | **已验证（static）**：`.app` 无 Python framework/`python3*`/`gdd` 源码；README/USER_GUIDE 写明本机 Python ≥3.10 与 GDD 源码 | `scripts/verify_app.sh`、`README.md:49-100`、`USER_GUIDE.md:322` | `scripts/verify_app.sh`：`app 包内无 Python 运行时/GDD 源码副本`、`README 说明…`、`USER_GUIDE 说明…`，退出 0 | 无 |

## 第二轮打回修复记录（2026-11-11）

### 1. D9/D11 GUI 错误对话框不可见（code，已修复）

- **复现**：无效配置启动 GUI，`app.log` 有 `event=error`，但 `CGWindowList` 只有 8 个
  `onscreen=false` 的内部窗口，无可见对话框；app 进程存活但无 UI。
- **根因（读依赖源码 + 实测）**：
  1. `tauri-plugin-dialog 2.8.4` 的 `blocking_show` 走 rfd 异步路径；rfd 的 `ModalFuture`
     在“无 Tauri 窗口”时用 `NSApp.windows().firstObject()` 选中 app 的内部离屏窗口，
     把 NSAlert 作为 **sheet** 挂上去 → 永远不可见，回调不返回。
  2. 改用 rfd 0.17 同步 `MessageDialog::show()` 后，rfd 在无父窗口时退化为
     `CFUserNotificationDisplayAlert`（通知式，3 秒自动返回 `Ok`，不是应用窗口），
     窗口列表仍无可见对话框。stderr 实测：
     `CFUserNotificationDisplayAlert: called from main application thread, will block waiting for a response.`
  3. 最终改为直接 `NSAlert::runModal`（应用级 modal）：主线程执行，不依赖任何窗口；
     `NSApplication.setActivationPolicy(Regular)` + `activate()` 保证无窗口时也能可见。
- **代码**：`src-tauri/src/app.rs:333` `show_error_dialog`；`Cargo.toml` 移除
  `tauri-plugin-dialog`/`rfd`，macOS 目标直接依赖 `objc2`/`objc2-app-kit`/`objc2-foundation`。
- **验证**：`scripts/verify_gui.py --check-error-dialogs` 用 CoreGraphics 窗口探针
  （`scripts/window_list.swift`）断言 `onscreen=true`；D9/D11 均通过并截图
  `evidence/d9-error-dialog.png`、`evidence/d11-error-dialog.png`（截图内容含标题、
  具体路径/端口、配置文件与日志路径、“好”按钮）。
- **定级说明**：这是对已批准方案 ADR-06/PLAN-006“用 tauri-plugin-dialog 显示原生错误对话框”
  的实现级修正。目标、对外错误语义、失败时用户可见行为（原生可操作错误）、无窗口要求均未变；
  仅替换了底层绑定库（仍为系统原生 NSAlert），无新增产品契约、无持久化/兼容性/时效性变化，
  按 L2 记录。未改写 `docs/plan.md` / `docs/decisions.md`。

### 2. D7 页面断言修正（测试脚本缺陷，已修复）

- 原断言查找 `/api/overview`，但控制台页面 JS 使用相对路径 `api('api/overview')`；
  静态 HTML 不含带前导斜杠的字符串。改为断言 `api/overview`，D7 重新通过（9/9）。
- 这是测试脚本的断言错误，不影响产品代码；`gdd/ui.py` 未改。

### 3. D2 字面 8765 冷启动（env，未能在节点内完成）

- 实测进程链：`73248 Python -m gdd.ui --port 8765` ← `60471 /usr/local/bin/mc` ←
  `60476 claude ...` ← 本测试 shell。8765 由本测试会话的祖先占用，终止它会终止当前流水线；
  未擅自终止用户控制台。
- 已提供可执行的补测入口：`python3.12 scripts/verify_headless.py --port 8765`
  （脚本支持固定端口；8765 空闲时逐场景验证 `--port 8765`、8765 overview 与窗口 URL）。
- 生产 8765 复用链路已实测：`verify_8765_reuse.txt`（原控制台 PID 73248 不变、窗口 URL 8765）。

## 第三轮打回修复记录（2026-11-11，门禁环境）

### 打回项

- 机器检查失败：`命令失败(127): cd src-tauri && cargo test :: bash: cargo: command not found`。

### 核实

- `~/.cargo/bin/cargo` 存在（rustup 代理），`cargo --version` → `1.99.1`；`cargo-tauri` 也在 `~/.cargo/bin`。
- 门禁由 GDD 控制台进程（PID 73248，`python -m gdd.ui --root ... --port 8765`）以
  `subprocess.Popen(["bash", "-c", cmd], cwd=root)` 执行（`gdd/gates.py:265-286`），
  继承该进程的 PATH；实测其 PATH 含 `/usr/local/bin`、`/opt/homebrew/bin`，**不含** `~/.cargo/bin`。
  本会话 shell 能看到 `cargo` 是因为 Claude Code 的 shell 快照额外注入了 `~/.cargo/bin`，门禁不经过它。
- `/usr/local/bin` 属 `root:wheel`，当前用户不可写；`/opt/homebrew/bin` 属当前用户可写，且位于门禁 PATH 中。

### 处理（环境级，不改仓库代码、不改门禁定义）

- 在 `/opt/homebrew/bin` 建立符号链接：`cargo`、`rustc`、`rustup`、`rustdoc`、`rustfmt`、
  `cargo-clippy`、`clippy-driver`、`cargo-tauri` → `~/.cargo/bin/*`。
- 该修复是机器级环境配置，不随仓库提交；换机器/换用户时需安装 rustup 并确保 `cargo` 在 PATH
  （README「Mac app」章节已写明构建需要 Rust 工具链与 Tauri CLI）。

### 验证（以门禁执行者的 PATH 复跑：`env -i HOME=... PATH=<门禁 PATH> bash -c ...`）

| 命令 | 退出码 | 关键输出 | 证据 |
|---|---|---|---|
| `cd src-tauri && cargo test` | 0 | 7 lib + 11 config + 5 supervisor，全部 `test result: ok`，共 23 passed | `evidence/gate-cargo-test.txt` |
| `bash scripts/verify_app.sh` | 0 | `PASS: 静态检查全部通过`（9 项 PASS） | `evidence/gate-verify-app.txt` |
| `python3.12 scripts/verify_headless.py` | 0 | `汇总：9 通过，0 失败`（D2/D3/D5/D6/D7/D9/D11/D12/D13） | `evidence/gate-verify-headless.txt` |
| 工具链解析 | 0 | `/opt/homebrew/bin/cargo` → `cargo 1.99.1`；`rustc 1.99.1`；`tauri-cli 2.12.4` | `evidence/gate-toolchain.txt` |
| 就绪超时分支（补测） | 1（预期失败语义） | fake Python 30.4s 未就绪 → `event=console_timeout`，子进程已终止，无残留 | `evidence/verify_timeout.txt` |

- 复跑脚本：`evidence/run_gate_env.sh`（门禁同款环境逐条执行并落盘）；工具链取证脚本
  `evidence/run_gate_toolchain.sh`。
- D2 仍使用等价端口：8765 继续被本会话祖先控制台占用（PID 73248），未终止；环境限制与补测待办不变。

## 与计划的差异 / 台账待办

1. **错误对话框实现替换（L2）**：见上“第二轮打回修复记录 1”；用系统 AppKit NSAlert 直接实现，
   未使用 `tauri-plugin-dialog`。
2. **新增仅测试钩子（L2，产品行为不变）**：`GDD_CONSOLE_TEST_PORT`、
   `GDD_CONSOLE_TEST_DUMP_PAGE`、`GDD_CONSOLE_TEST_CLOSE_AFTER_READY`；均显式 env 才生效，
   生产端口/默认行为不变。另有测试辅助 `scripts/window_list.swift`（CoreGraphics 窗口可见性探针）。
3. **D2 生产 8765 冷启动未实测**：见上；8765 空闲时执行 `--port 8765`。
4. **就绪超时分支（第三轮已补测，闭环）**：用 fake Python（`-c` 返回 3.12，`-m gdd.ui` 永久 sleep）
   实测 30.4s 未就绪 → 退出码 1、日志 `event=console_timeout`、子进程已终止且 `pgrep` 无残留；
   证据 `evidence/verify_timeout.txt`，脚本 `evidence/verify_timeout.py`。
5. **首启配置自动生成未做 GUI 端到端**：`Config::load_or_create` 单测覆盖生成/0600/重读；
   未用空配置目录启动 GUI，避免在用户 home 创建 `~/gdd-workbench`。
6. **菜单 Cmd+Q 事件路径未单独演练**：实际退出用 AppleScript `quit` 验证（控制台存活结论一致）。
7. **`GDD_CONSOLE_TEST_EXIT_AFTER_READY`**：按计划实现但未在脚本中使用（D12/D13 用 headless 路径覆盖同一语义）。
8. **未改现有代码**：`git diff --stat -- gdd/ tests/` 为空；现有 `tests/test_ui.py` 通过（`全部通过 ✔`）。
9. **工作区存在 test 节点产物** `docs/test-report.md`、`docs/verify.md`（11:06 生成，非本节点产生，未修改）。
   其中独立记录：D2 同样受 8765 祖先控制台阻塞；30s 就绪超时分支已由其演练通过。本台账不把该文件当作
   本节点证据，仅作为同一环境事实的交叉引用。
10. **门禁 PATH 缺 `cargo`（环境，已修复）**：见“第三轮打回修复记录”；通过 `/opt/homebrew/bin` 符号链接
    使 rustup 工具链在门禁 PATH 中可见，未改仓库代码/门禁定义。换机器需保证 `cargo` 在 PATH。
