# GDD 控制台 Mac app — 交付验收证据

> 节点：delivery；日期：2026-11-11。
> 验收基线：`docs/goal.md` 的 DoD（D1–D14）；实现基线 `a85bf18`；测试基线 `7fe3c15`（test 节点 exit 通过）。
> 构建产物：`src-tauri/target/release/bundle/macos/GDD Console.app`
> 二进制 sha256：`87bc0907dea47025d77893f6db832877f4a3eecc6646125b00723b85591bfc19`
> 环境：macOS 26.3（arm64）、Python 3.12.13、Rust 1.99.1、tauri-cli 2.12.4、Tauri 2.12.5、wry 0.57.2。
> 证据来源：`docs/test-report.md`、`docs/verify.md`、`.gdd/nodes/test/evidence/`；本节点独立复验记录见 `.gdd/nodes/delivery/evidence/` 与本文「本节点独立复验」。

## 验收证据

### 结论总览

- **DoD 14/14 通过：P0 6/6（D1–D6），P1 7/7（D7–D13），P2 1/1（D14）。**
- **整体验收：达成。** 无 DoD 未通过项；无 DoD 以「未验证」结项。
- 本节点未重新构建 app，使用 test 节点验证过的同一构建产物（sha256 与台账/test 报告一致）；独立复跑静态检查、Rust 测试、headless 集成（隔离端口）与既有 UI 回归，全部通过。
- 字面 8765 的 D2/D3/D11 由 test 节点在用户授权停掉原控制台（PID 73248）后完成，测试后已恢复控制台（PID 630，`/api/overview` 200）。

### DoD 逐条终态

| ID | 优先级 / 层级 | 终态 | 关键证据（test 报告 / 证据文件 / 复验命令） | 证据口径与备注 |
|---|---|---|---|---|
| D1 | P0 / static | **通过** | `docs/test-report.md` D1 行、`docs/verify.md` §D1；`evidence/D1-D14-static.txt`；本节点复验 `bash scripts/verify_app.sh` → 9 项 PASS、`EXIT=0`（`.gdd/nodes/delivery/evidence/reverify-static.txt`） | `tauri.conf.json`：`frontendDist=http://127.0.0.1:8765`、`app.windows=[]`、`withGlobalTauri=false`；依赖 Tauri 2.12.5，无 Electron/Chromium；`.app/Contents` 仅 `Info.plist`、`MacOS/gdd-console`、`Resources/icon.icns`，无 HTML/JS/CSS；`otool -L` 仅链接系统 WebKit；窗口目标 URL 8765（见 D4） |
| D2 | P0 / integration | **通过** | `docs/test-report.md` D2 行、`docs/verify.md` §D2；`evidence/literal-8765-headless.txt` D2；保留目录 `…/gdd-console-verify-0esfrfh7/d2/logs/app.log`；窗口 URL 见 `evidence/literal-8765-gui.txt`；本节点复验 `python3.12 scripts/verify_headless.py`（隔离端口）D2 PASS | 字面 8765：`event=probe port=8765` → `console_spawn pid=99188 port=8765` → 就绪 0.26s（<30s）；argv 含 `-m gdd.ui`、`--root <tmp>/d2/workbench`、`--port 8765`、`--no-open`；`/api/overview` 200 且含 `ok/templates/tasks`；监听仅 `127.0.0.1:8765`；GUI 冷启动 `window_created/window_url=http://127.0.0.1:8765/`。本节点未占用 8765（用户控制台 PID 630 在跑），复验走隔离端口 |
| D3 | P0 / integration | **通过** | `docs/test-report.md` D3 行、`docs/verify.md` §D3；`evidence/literal-8765-headless.txt` D3；`evidence/D4-8765-reuse.txt`、`evidence/8765-reuse/manual-8765-run.txt`（窗口 URL + 页面内容）；本节点复验 headless D3 PASS | 字面 8765 同一次运行：外部控制台 pid=99195、临时工作台任务 `task-9e0d4324`，app `result=reuse`、`started=false`、原 PID 不变、`console_pids(workbench)==[99195]`（无第二个 `gdd.ui`）、overview 仅列该任务。窗口 URL/页面任务列表由同一二进制的生产 8765 GUI 复用运行取证（`D4-8765-reuse.txt`：PID 73248 不变、窗口 URL 8765、页面内容含原工作台任务） |
| D4 | P0 / e2e | **通过** | `docs/test-report.md` D4 行、`docs/verify.md` §D4；`evidence/literal-8765-gui.txt`；截图 `evidence/literal-8765-d4-window.png`（本节点已视觉复核）；复验 `python3.12 scripts/verify_gui.py --port 8765 --keep`（需 8765 空闲） | app 冷启动控制台 pid=55696；`window_created/window_url=http://127.0.0.1:8765/`；`window_content` 标题「GDD 控制台」、正文含运行时任务 `task-c6c2d13a`；主窗口 onscreen=true 1200×801；截图显示原生「GDD 控制台」窗口与任务名。System Events 标题不可用，按 DoD 允许的「截图 + DOM 日志」口径取证 |
| D5 | P0 / integration | **通过** | `docs/test-report.md` D5 行、`docs/verify.md` §D5；`evidence/literal-8765-headless.txt` D5；保留目录 `…/gdd-console-verify-0esfrfh7/d5/gdd-src/gdd/ui.py`（含 `GDD-MARK-49708a6751`）；本节点复验 headless D5 PASS | 停止控制台 → 向临时源码副本 `gdd/ui.py` 写入随机标记 → **不重建 app** 重启；页面包含 `GDD-MARK-49708a6751`，控制台 pid 99218→99240；app 二进制 sha256 前后一致。标记以 HTTP 页面抓取验证，未在窗口内重复读取；DoD 明确允许该降级，窗口渲染由 D4 覆盖 |
| D6 | P0 / integration | **通过** | `docs/test-report.md` D6 行、`docs/verify.md` §D6；`evidence/literal-8765-headless.txt` D6；`evidence/literal-8765-gui.txt`（app 主进程无监听）；本节点 `lsof -nP -iTCP:8765 -sTCP:LISTEN` 复核当前控制台仅 `127.0.0.1:8765` | 8765 监听集合仅 `127.0.0.1:8765`；本机非回环地址 `11.10.137.3:8765` 连接失败；GUI app 主进程 pid=55626 无 TCP LISTEN。WebKit 系统辅助进程的内部监听未单独取证，未新增应用端口（非 DoD） |
| D7 | P1 / integration | **通过** | `docs/test-report.md` D7 行、`docs/verify.md` §D7；`evidence/literal-8765-headless.txt` D7；保留目录 `…/gdd-console-verify-0esfrfh7/d7/R1/` 与 `…/d7/R2/`；本节点复验 headless D7 PASS | 查询前预置 R1=`task-4361f42d`、R2=`task-6617e18c`；R1 仅列 `task-4361f42d`（不含 R2 任务），R2 仅列 `task-6617e18c`（不含 R1 任务）。直接断言 `/api/overview`；页面由同一接口数据驱动，渲染链路由 D4 覆盖 |
| D8 | P1 / unit | **通过** | `docs/test-report.md` D8 行、`docs/verify.md` §D8；`evidence/cargo-test.txt`（`tests/config.rs` 11 passed）；本节点复验 `cd src-tauri && cargo test` → 11 passed、`EXIT=0`（`reverify-cargo-test.txt`） | 缺必填字段、GDD 路径不存在、缺 `gdd/ui.py`、Python 不存在、工作台为普通文件、非法 JSON/未知字段均返回含字段名与路径的错误；`validate()` 返回 `Result`，无未捕获异常 |
| D9 | P1 / integration | **通过** | `docs/test-report.md` D9 行、`docs/verify.md` §D9；`evidence/literal-8765-headless.txt` D9（退出码 1、无 `gdd.ui`）；`evidence/literal-8765-dialogs.txt` + 截图 `evidence/literal-8765-d9-error-dialog.png`（本节点已视觉复核）；复验 `python3.12 scripts/verify_gui.py --check-error-dialogs --port 8765`（需 8765 空闲） | GUI 可见原生 `NSAlert`（onscreen=true layer=8），截图含标题「GDD 控制台启动失败」、缺失的 `gddHome` 路径、配置文件与应用日志路径；无 `gdd.ui` 子进程 |
| D10 | P1 / unit | **通过** | `docs/test-report.md` D10 行、`docs/verify.md` §D10；`evidence/cargo-test.txt` 中 `python_39_is_rejected_and_310_accepted ... ok`；本节点复验 cargo test 23 passed | fake Python 3.9（309）被拒绝，错误含 `python`、路径与「最低 3.10」；3.10/3.12 通过 |
| D11 | P1 / integration | **通过** | `docs/test-report.md` D11 行、`docs/verify.md` §D11；`evidence/literal-8765-headless.txt` D11；`evidence/literal-8765-dialogs.txt` + 截图 `evidence/literal-8765-d11-error-dialog.png`（本节点已视觉复核）；复验 `verify_gui.py --check-error-dialogs --port 8765`（需 8765 空闲） | 字面 8765 dummy 非 GDD 服务：`probe result=non_gdd` → app 退出码 1、未启动 `gdd.ui`、未加载该服务；可见 `NSAlert` 正文明确「127.0.0.1:8765 已被非 GDD 服务占用（响应 JSON 缺少 ok:true）」 |
| D12 | P1 / integration | **通过** | `docs/test-report.md` D12 行、`docs/verify.md` §D12；`evidence/literal-8765-gui.txt`（pid=55696 退出后存活）；`evidence/literal-8765-headless.txt` D12（pid=99351 存活、重启复用、无第二进程）；本节点复验 headless D12 PASS | GUI：关闭窗口仅隐藏、AppleScript 退出后控制台存活且 API 可访问；headless：app 退出后 pid=99351 存活，再次启动 `result=reuse`/`started=false`，`console_pids(workbench)==[99351]` |
| D13 | P1 / integration | **通过** | `docs/test-report.md` D13 行、`docs/verify.md` §D13；`evidence/literal-8765-headless.txt` D13；本节点复验 headless D13 PASS | 外部控制台 pid=99367（8765、临时工作台）在 app 退出后 PID 不变、`/api/overview` 可访问；app 未终止外部实例 |
| D14 | P2 / static | **通过** | `docs/test-report.md` D14 行、`docs/verify.md` §D14；`evidence/D1-D14-static.txt`；本节点复验 `verify_app.sh` 文档两项 PASS + `reverify-bundle.txt` | `.app/Contents` 无 Python 运行时/framework、无 `gdd` 源码或页面副本；README（`README.md:49-100`）与 USER_GUIDE（`USER_GUIDE.md:322-334`）明确要求本机 Python ≥3.10 与 GDD 源码 |

### 关键证据链（字面 8765）

test 节点保留的临时目录（下称 `T-headless`）：`/private/var/folders/dy/35pzfng13gb8sgb2x1fhys7m0000gn/T/gdd-console-verify-0esfrfh7/`；GUI 目录（下称 `T-gui`）：`/private/var/folders/dy/35pzfng13gb8sgb2x1fhys7m0000gn/T/gdd-console-gui-5_gkd76p/`。

- **D2**：`T-headless/d2/logs/app.log` → `event=probe port=8765`、`event=console_spawn pid=99188 port=8765`、`event=console_ready … url=http://127.0.0.1:8765/`；`T-headless/d2/config/config.json` 指向临时 GDD 源码/工作台与 Python 3.12.13。
- **D3**：`T-headless/d3/logs/app.log` → `event=probe port=8765 result=reuse`、`event=headless_ready pid=- started=false url=http://127.0.0.1:8765/`；`T-headless/d3/workbench/task-9e0d4324/` 存在。
- **D4**：`T-gui/logs/app.log` → `event=console_spawn pid=55696 port=8765`、`event=window_created url=http://127.0.0.1:8765/`、`event=window_url url=http://127.0.0.1:8765/`、`event=window_content`（title「GDD 控制台」，text 含 `task-c6c2d13a`）；`evidence/literal-8765-d4-window.png` 已视觉复核。
- **D5**：`T-headless/d5/gdd-src/gdd/ui.py` 含 `GDD-MARK-49708a6751`；`T-headless/d5/logs/app.log` 首次 pid=99218、`logs-2/app.log` 二次 pid=99240；两次前后 app sha256 一致。
- **D11**：`literal-8765-dialogs.txt` 记录 D11 可见 `NSAlert`（onscreen=true layer=8）；`evidence/literal-8765-d11-error-dialog.png` 正文含「127.0.0.1:8765 已被非 GDD 服务占用（响应 JSON 缺少 ok:true）」。
- **D12/D13**：`literal-8765-headless.txt` 记录 app 退出后 pid=99351（app 拉起）与 pid=99367（外部启动）均存活且可访问。

### 本节点独立复验（2026-11-11）

| # | 命令 | 结果 | 证据文件 |
|---|---|---|---|
| 1 | `git status --short`；`git diff --stat -- gdd/ tests/` | 复验开始时仅 `?? .gdd/`；产出 `docs/delivery.md` 后为 `?? .gdd/`、`?? docs/delivery.md`（待 exit 交付提交）；`gdd/`、`tests/` 无未提交改动 | `.gdd/nodes/delivery/evidence/reverify-git.txt` |
| 2 | `shasum -a 256 "…/GDD Console.app/Contents/MacOS/gdd-console"` | `87bc0907dea47025d77893f6db832877f4a3eecc6646125b00723b85591bfc19`（与 test 报告/台账一致） | `reverify-app-hash.txt` |
| 3 | `bash scripts/verify_app.sh` | 9 项 PASS，`EXIT=0`（D1/D14 静态检查） | `reverify-static.txt` |
| 4 | `cd src-tauri && cargo test` | 7 lib + 11 config + 5 supervisor = **23 passed，0 failed**，`EXIT=0`（D8/D10 等） | `reverify-cargo-test.txt` |
| 5 | `python3.12 scripts/verify_headless.py` | **9/9 PASS**，`EXIT=0`（D2/D3/D5/D6/D7/D9/D11/D12/D13，隔离端口；8765 当前由用户控制台 PID 630 占用，字面 8765 证据引用 test 节点同一次运行） | `reverify-headless.txt` |
| 6 | `python3.12 tests/test_ui.py` | `EXIT=0`，末行「全部通过 ✔」（既有控制台回归） | `reverify-test-ui.txt` |
| 7 | `lsof -nP -iTCP:8765 -sTCP:LISTEN`；`curl /api/overview` | PID 630 仅监听 `127.0.0.1:8765`；overview `ok:true`，templates/tasks 正常（用户控制台已恢复） | `reverify-8765.txt` |
| 8 | `find "…/GDD Console.app/Contents" -maxdepth 2 -mindepth 1`；`otool -L … \| grep WebKit` | 仅 `Info.plist`、`MacOS/gdd-console`、`Resources/icon.icns`；链接系统 `WebKit.framework` | `reverify-bundle.txt` |
| 9 | 视觉复核 `literal-8765-d4-window.png`、`literal-8765-d9-error-dialog.png`、`literal-8765-d11-error-dialog.png` | D4：原生窗口「GDD 控制台」+ `task-c6c2d13a`；D9：错误对话框含 `gddHome` 缺失路径/配置/日志路径；D11：可见错误对话框 + `127.0.0.1:8765 已被非 GDD 服务占用` | `docs/verify.md` 记录 + 截图 |

### 证据口径与已知限制（不影响 DoD 判定）

1. **字面 8765 的取得方式**：implement 节点曾因 8765 被本会话祖先控制台（PID 73248）占用而无法做冷启动；test 节点获得用户授权停掉该控制台后，在字面 8765 上完成 D2/D3/D5/D6/D7/D9/D11/D12/D13 与 D4/D9/D11 的 GUI 验证，测试后用原命令/环境恢复控制台（新 PID 630）。本节点未再次停掉用户控制台；独立复验的 headless 使用隔离端口，字面 8765 结论引用 test 节点证据。
   - 说明：`scripts/verify_headless.py` 的 PASS 文案仍沿用模板措辞「8765（测试端口）」/「8765 等价端口」；实际运行的端口以 app 日志与配置为准。`--port 8765` 时脚本**不注入** `GDD_CONSOLE_TEST_PORT`（`scripts/verify_headless.py:229-232`），字面证据中 `d2/logs/app.log` 为 `event=probe port=8765`、`console_spawn pid=99188 port=8765`。
2. **D4 的标题取证**：System Events 标题不可用，使用 app 日志中的窗口 URL/DOM 与窗口截图；DoD 允许「截图人工核对并保留截图」，本节点已视觉复核。
3. **D5 的窗口内标记**：以 HTTP 页面抓取验证源码更新生效，未在窗口内重复读取标记；DoD 明确允许该降级，窗口渲染机制由 D4 覆盖。
4. **D7 的页面层**：直接断言 `/api/overview` 的任务隔离；页面任务列表由同一接口数据驱动，渲染链路由 D4 覆盖。
5. **D3 的窗口 URL**：复用语义（PID/进程数/任务列表）在字面 8765 headless 同一次运行取证；窗口 URL 与页面内容由同一二进制的生产 8765 GUI 复用运行取证（`D4-8765-reuse.txt`、`8765-reuse/manual-8765-run.txt`）。
6. **非 DoD 未演练项**（不改变 DoD 终态）：首次启动自动生成配置的 GUI 端到端、`Cmd+Q` 菜单项独立事件路径、WebKit 系统辅助进程内部监听取证。均已在 `docs/test-report.md`「未覆盖场景」/`docs/impl-ledger.md` 记录。
7. **构建**：本节点未重新执行 `cargo tauri build`，避免改变被验证产物哈希；构建成功记录见 `.gdd/nodes/implement/evidence/build.log`。

### 交付边界与流程提醒

- **交付边界**（依据 `docs/spec.md` C8/§12）：不含 Developer ID 签名/公证/Mac App Store 分发；无 Windows/Linux 版本；无 app 自动更新；无停止/重启控制台 GUI；不支持非默认端口发现；不打包 Python/GDD。运行前提是本机已安装 Python ≥3.10 与 GDD 源码。
- **仓库状态**：功能代码与既有文档已提交在 `gdd/delivery` 分支；本节点新增的 `docs/delivery.md` 与 `.gdd/nodes/delivery/` 证据/报告待流水线 exit 交付提交；`.gdd/` 为流水线状态目录，不入库。
- **流水线流程提醒（非本功能 DoD）**：`.gdd/state.json` 存在 `stray_delivery` 警示，指向 `dev` 分支两个提交（`ed7f7ef`、`b579097`，内容为 `gdd/orchestrator.py`、`gdd/taskgraph.py`、`tests/*` 的 harness 并行任务改动），与本 Mac app 功能无关。该警示需由人工确认后用 `gdd acknowledge` 清除；本节点未擅自清除，也未将其计入 DoD 结论。
