//! 无 GUI 的测试入口：执行与 GUI 相同的配置/探测/拉起核心，写日志后退出。
//!
//! 仅由 `GDD_CONSOLE_TEST_HEADLESS=1` 触发，不构成产品对外契约。

use crate::config::{self, RuntimePaths};
use crate::log;
use crate::supervisor;

pub fn run() -> i32 {
    let paths = RuntimePaths::headless_defaults();
    let _ = paths.ensure_dirs();
    let _ = log::init(&paths.app_log());
    log::event(format!(
        "event=headless_start config={}",
        paths.config_file().display()
    ));

    match run_core(&paths) {
        Ok(handle) => {
            log::event(format!(
                "event=headless_ready pid={} started={} url={} log={}",
                handle
                    .pid
                    .map(|p| p.to_string())
                    .unwrap_or_else(|| "-".to_string()),
                handle.started,
                handle.url,
                handle.log_path.display()
            ));
            0
        }
        Err(message) => {
            log::event(format!(
                "event=error error={} config={} appLog={}",
                one_line(&message),
                paths.config_file().display(),
                paths.app_log().display()
            ));
            1
        }
    }
}

fn run_core(paths: &RuntimePaths) -> Result<supervisor::ConsoleHandle, String> {
    let config = config::Config::load_or_create(&paths.config_file()).map_err(|e| e.to_string())?;
    let valid = config.validate().map_err(|e| e.to_string())?;
    log::event(format!(
        "event=config path={} gddHome={} python={} workbenchRoot={} envKeys={}",
        paths.config_file().display(),
        valid.gdd_home.display(),
        valid.python.display(),
        valid.workbench_root.display(),
        valid.env.keys().cloned().collect::<Vec<_>>().join(",")
    ));
    let port = supervisor::test_port_override().unwrap_or(supervisor::DEFAULT_PORT);
    supervisor::ensure_console(&valid, paths, port).map_err(|e| e.to_string())
}

fn one_line(message: &str) -> String {
    message.replace(['\r', '\n'], " ")
}
