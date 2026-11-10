//! 控制台探测、复用与拉起。
//!
//! 启动前只做只读探测：已是 GDD 控制台则复用；端口无监听才拉起；
//! 端口被其他服务占用则报错且绝不启动第二个实例。

use crate::config::{RuntimePaths, ValidatedConfig};
use crate::console::{self, SpawnedConsole};
use crate::http_probe::{self, ProbeOutcome};
use crate::log;
use std::path::PathBuf;
use std::time::{Duration, Instant};

pub const DEFAULT_PORT: u16 = 8765;
pub const READY_TIMEOUT: Duration = Duration::from_secs(30);
const POLL_INTERVAL: Duration = Duration::from_millis(200);
const PROBE_TIMEOUT: Duration = Duration::from_millis(500);

#[derive(Debug, Clone)]
pub struct ConsoleHandle {
    pub url: String,
    pub started: bool,
    pub pid: Option<u32>,
    pub log_path: PathBuf,
}

#[derive(Debug, thiserror::Error)]
pub enum SupervisorError {
    #[error("127.0.0.1:{port} 已被非 GDD 服务占用（{detail}）。为避免启动第二个控制台，已停止；请先停止该服务或确认其为 GDD 控制台")]
    PortOccupied { port: u16, detail: String },
    #[error("无法确认 127.0.0.1:{port} 的端口状态：{detail}。为避免误启第二个控制台，已停止；请检查端口占用情况")]
    ProbeFailed { port: u16, detail: String },
    #[error("启动控制台失败：{detail}（日志：{log_path}）")]
    Spawn { detail: String, log_path: PathBuf },
    #[error("控制台在 {timeout_secs} 秒内未就绪（最后探测：{detail}）。已终止本次拉起的子进程；请查看日志：{log_path}")]
    NotReady {
        timeout_secs: u64,
        detail: String,
        log_path: PathBuf,
    },
    #[error("控制台进程在就绪前退出（{detail}）。请查看日志：{log_path}")]
    EarlyExit { detail: String, log_path: PathBuf },
}

pub fn url_for(port: u16) -> String {
    format!("http://127.0.0.1:{port}/")
}

/// 仅测试使用的端口覆盖（`GDD_CONSOLE_TEST_PORT`）；生产路径始终使用
/// [`DEFAULT_PORT`]。用于在 8765 被用户控制台占用时做隔离验证。
pub fn test_port_override() -> Option<u16> {
    std::env::var("GDD_CONSOLE_TEST_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
}

/// 确保 8765 上有可用控制台；返回窗口应加载的 URL 与本次是否由 app 拉起。
pub fn ensure_console(
    config: &ValidatedConfig,
    paths: &RuntimePaths,
    port: u16,
) -> Result<ConsoleHandle, SupervisorError> {
    log::event(format!("event=probe port={port} phase=before_start"));
    match http_probe::probe(port, PROBE_TIMEOUT) {
        ProbeOutcome::Gdd => {
            log::event(format!("event=probe port={port} result=reuse"));
            return Ok(ConsoleHandle {
                url: url_for(port),
                started: false,
                pid: None,
                log_path: paths.console_log(),
            });
        }
        ProbeOutcome::NoListener => {}
        ProbeOutcome::NonGdd(detail) => {
            log::event(format!(
                "event=probe port={port} result=non_gdd detail={detail}"
            ));
            return Err(SupervisorError::PortOccupied { port, detail });
        }
        ProbeOutcome::Io(detail) => {
            log::event(format!(
                "event=probe port={port} result=io_error detail={detail}"
            ));
            return Err(SupervisorError::ProbeFailed { port, detail });
        }
    }

    let SpawnedConsole {
        mut child,
        log_path,
    } = console::spawn_console(config, paths, port).map_err(|e| SupervisorError::Spawn {
        detail: e.to_string(),
        log_path: paths.console_log(),
    })?;
    let pid = child.id();
    log::event(format!(
        "event=console_spawn pid={pid} port={port} log={}",
        log_path.display()
    ));

    let start = Instant::now();
    let mut last_detail: String;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                // 子进程提前退出：再探测一次，处理“并发启动竞态”。
                if let ProbeOutcome::Gdd = http_probe::probe(port, PROBE_TIMEOUT) {
                    log::event(format!(
                        "event=probe port={port} result=reuse_after_child_exit"
                    ));
                    return Ok(ConsoleHandle {
                        url: url_for(port),
                        started: false,
                        pid: None,
                        log_path,
                    });
                }
                let detail = format!("退出状态 {status}");
                log::event(format!(
                    "event=console_early_exit pid={pid} detail={detail} log={}",
                    log_path.display()
                ));
                return Err(SupervisorError::EarlyExit { detail, log_path });
            }
            Ok(None) => {}
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(SupervisorError::Spawn {
                    detail: format!("无法等待控制台子进程：{e}"),
                    log_path,
                });
            }
        }

        match http_probe::probe(port, PROBE_TIMEOUT) {
            ProbeOutcome::Gdd => {
                let reap_pid = pid;
                // reaper：回收退出状态，避免僵尸进程；app 退出时不 kill 子进程。
                std::thread::spawn(move || {
                    let _ = child.wait();
                    log::event(format!("event=console_reaped pid={reap_pid}"));
                });
                let url = url_for(port);
                log::event(format!(
                    "event=console_ready pid={pid} started=true url={url}"
                ));
                return Ok(ConsoleHandle {
                    url,
                    started: true,
                    pid: Some(pid),
                    log_path,
                });
            }
            ProbeOutcome::NoListener => {
                last_detail = String::from("端口尚无监听");
            }
            ProbeOutcome::NonGdd(detail) => {
                last_detail = format!("非 GDD 响应：{detail}");
            }
            ProbeOutcome::Io(detail) => {
                last_detail = format!("探测失败：{detail}");
            }
        }

        if start.elapsed() >= READY_TIMEOUT {
            let _ = child.kill();
            let _ = child.wait();
            log::event(format!(
                "event=console_timeout pid={pid} log={}",
                log_path.display()
            ));
            return Err(SupervisorError::NotReady {
                timeout_secs: READY_TIMEOUT.as_secs(),
                detail: last_detail,
                log_path,
            });
        }
        std::thread::sleep(POLL_INTERVAL);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_endpoint_is_local_8765() {
        assert_eq!(DEFAULT_PORT, 8765);
        assert_eq!(url_for(DEFAULT_PORT), "http://127.0.0.1:8765/");
    }
}
