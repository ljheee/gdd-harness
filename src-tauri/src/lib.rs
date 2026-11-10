//! GDD 控制台 macOS 桌面壳。
//!
//! 壳只做三件事：读取/校验配置、探测或拉起 `python -m gdd.ui`、在 WKWebView 里
//! 打开 `http://127.0.0.1:8765/`。不内置第二份前端，不代理控制台流量。

pub mod app;
pub mod config;
pub mod console;
pub mod headless;
pub mod http_probe;
pub mod log;
pub mod supervisor;

/// 进程入口：测试钩子 `GDD_CONSOLE_TEST_HEADLESS=1` 时走无 GUI 路径。
pub fn run() -> i32 {
    if std::env::var("GDD_CONSOLE_TEST_HEADLESS").ok().as_deref() == Some("1") {
        headless::run()
    } else {
        app::run()
    }
}
