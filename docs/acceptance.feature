Feature: GDD 控制台 Mac app
  作为 GDD 使用者
  我希望通过一个 Mac app 直接打开现有的 GDD Web 控制台
  以便不必记忆终端命令，并且 Web 界面更新不需要重新发布 app

  @D1 @P0 @static
  Scenario: D1 使用 Tauri 2 与系统 WKWebView，且不打包第二份前端
    Given 已构建的 "GDD Console.app" 与 src-tauri 配置
    When 检查 Tauri 配置、依赖清单与应用包资源
    Then 应用应使用 Tauri 2 且 macOS 目标使用系统 WKWebView
    And 依赖中不应出现 Electron 或 Chromium
    And 应用包内不应包含 GDD 控制台页面的 HTML/JS/CSS 副本
    And 窗口最终应加载 "http://127.0.0.1:8765/"

  @D2 @P0 @integration
  Scenario: D2 8765 无监听时冷启动控制台
    Given 8765 端口没有监听
    And 配置指向一个可用的 Python（>=3.10）与一个临时 GDD 源码副本
    And 工作台是运行时创建的临时目录
    When 启动 app
    Then app 应以 "--root <工作台> --port 8765 --no-open" 拉起 "python -m gdd.ui"
    And 在 30 秒内 GET "/api/overview" 应返回 200 且包含 ok、templates、tasks
    And 窗口目标地址应为 "http://127.0.0.1:8765/"

  @D3 @P0 @integration
  Scenario: D3 复用已在运行的控制台
    Given 已有一个真实控制台在 8765 运行
    And 该控制台使用临时工作台且其中有一个运行时生成的任务
    When 启动 app
    Then app 不应产生第二个 gdd.ui 进程
    And 原控制台 PID 应保持不变
    And 页面任务列表应来自原工作台

  @D4 @P0 @e2e
  Scenario: D4 窗口展示控制台页面
    Given 已构建的 .app 且配置指向临时 GDD 源码与临时工作台
    And 工作台中有一个运行时生成的任务名
    When 启动 .app
    Then 应出现至少一个窗口
    And 窗口应显示「GDD 控制台」标题或关键 UI 以及预置任务名
    And 窗口目标地址应为 "http://127.0.0.1:8765/"

  @D5 @P0 @integration
  Scenario: D5 Web 更新不需要重新构建 app
    Given app 使用临时 GDD 源码副本 A 启动并成功加载页面
    When 停止该控制台进程
    And 在 A 的 gdd/ui.py 页面中写入运行时生成的随机标记
    And 重新启动 app（不重新构建 app）
    Then 页面应在 30 秒内包含该随机标记
    And app 二进制哈希应与更新前一致

  @D6 @P0 @integration
  Scenario: D6 控制台只监听本机
    Given app 已启动且控制台在 8765 运行
    When 检查 8765 的监听地址并尝试用本机非回环地址连接
    Then 8765 应只监听 127.0.0.1
    And 用本机非回环地址连接 8765 应失败
    And app 主进程不应监听 TCP 端口

  @D7 @P1 @integration
  Scenario: D7 工作台目录决定任务列表
    Given 两个临时工作台 R1 与 R2，分别有运行时生成的任务 T1 与 T2
    When 配置指向 R1 并启动 app，然后查询 "/api/overview"
    Then 结果应只列出 T1，不应列出 T2
    When 停止控制台、将配置指向 R2 并重启 app
    Then 结果应只列出 T2，不应列出 T1

  @D8 @P1 @unit
  Scenario: D8 配置校验返回可定位的错误
    Given 配置校验逻辑
    When 分别输入缺失必填字段、GDD 源码路径不存在、Python 路径不存在、工作台路径为普通文件的配置
    Then 每种情况都应返回包含字段名与路径的错误
    And 不应抛出未捕获异常

  @D9 @P1 @integration
  Scenario: D9 无效配置时不启动控制台并显示错误
    Given 一个无效配置，其中 GDD 源码路径不存在
    When 启动 app
    Then app 不应启动 gdd.ui 子进程
    And 应显示指向具体配置项或路径的可操作错误
    And app 不应崩溃

  @D10 @P1 @unit
  Scenario: D10 Python 版本校验
    Given 一个报告版本低于 3.10 的 fake Python 可执行文件
    When 校验配置
    Then 应拒绝启动并提示最低版本 3.10
    Given 一个报告版本不低于 3.10 的 fake Python 可执行文件
    When 校验配置
    Then 应通过版本校验

  @D11 @P1 @integration
  Scenario: D11 8765 被非 GDD 服务占用
    Given 8765 被一个返回非 GDD 响应的 dummy HTTP 服务占用
    When 启动 app
    Then app 不应启动第二个控制台
    And 不应把该服务当作控制台加载进窗口
    And 应显示端口冲突或未知服务错误

  @D12 @P1 @integration
  Scenario: D12 app 启动的控制台在 app 退出后继续运行并复用
    Given app 已自行启动控制台
    When 关闭窗口并退出 app
    Then 控制台进程应仍存活且 "/api/overview" 可访问
    When 再次启动 app
    Then 应复用该控制台且不产生第二个 gdd.ui 进程

  @D13 @P1 @integration
  Scenario: D13 不终止外部启动的控制台
    Given 用户已在外部启动一个控制台
    When 启动并退出 app
    Then 该外部控制台 PID 应保持不变且 "/api/overview" 可访问

  @D14 @P2 @static
  Scenario: D14 应用包不包含 Python/GDD，文档说明依赖
    Given 已构建的 .app 与使用文档
    When 检查应用包内容与 README/USER_GUIDE
    Then 应用包不应包含 Python 运行时或 framework，也不应包含 gdd 源码或页面副本
    And 文档应明确要求本机已安装 Python >=3.10 与 GDD 源码
