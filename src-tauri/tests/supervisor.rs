mod common;

use gdd_console_lib::config::{RuntimePaths, ValidatedConfig};
use gdd_console_lib::http_probe::{self, ProbeOutcome};
use gdd_console_lib::supervisor::{self, SupervisorError};
use std::io::{Read, Write};
use std::net::TcpListener;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};
use tempfile::TempDir;

struct TestEnv {
    _temp: TempDir,
    config: ValidatedConfig,
    paths: RuntimePaths,
}

fn setup() -> TestEnv {
    let temp = TempDir::new().unwrap();
    let gdd = common::make_gdd_home(temp.path());
    let python = common::find_python();
    let workbench = temp.path().join("workbench");
    let config = common::base_config(&gdd, &python, &workbench)
        .validate()
        .expect("测试配置应有效");
    let paths = common::runtime_paths(temp.path());
    TestEnv {
        _temp: temp,
        config,
        paths,
    }
}

fn wait_ready(port: u16) {
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        if http_probe::probe(port, Duration::from_millis(300)) == ProbeOutcome::Gdd {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    panic!("控制台在 15 秒内未就绪（端口 {port}）");
}

fn wait_port_free(port: u16) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if http_probe::probe(port, Duration::from_millis(200)) == ProbeOutcome::NoListener {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    panic!("端口 {port} 未在 10 秒内释放");
}

fn kill_pid(pid: u32) {
    let _ = Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status();
}

fn start_external_console(env: &TestEnv, port: u16) -> std::process::Child {
    Command::new(&env.config.python)
        .arg("-m")
        .arg("gdd.ui")
        .arg("--root")
        .arg(&env.config.workbench_root)
        .arg("--port")
        .arg(port.to_string())
        .arg("--no-open")
        .arg("--local")
        .current_dir(&env.config.workbench_root)
        .env("PYTHONPATH", &env.config.gdd_home)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("启动外部控制台")
}

#[test]
fn cold_start_spawns_console_with_expected_argv() {
    let env = setup();
    let port = common::free_port();
    let handle = supervisor::ensure_console(&env.config, &env.paths, port).expect("冷启动就绪");
    assert!(handle.started);
    let pid = handle.pid.expect("应返回 pid");
    assert_eq!(handle.url, format!("http://127.0.0.1:{port}/"));
    assert_eq!(
        http_probe::probe(port, Duration::from_secs(2)),
        ProbeOutcome::Gdd
    );

    let output = Command::new("ps")
        .args(["-o", "command=", "-p", &pid.to_string()])
        .output()
        .expect("ps");
    let cmdline = String::from_utf8_lossy(&output.stdout).to_string();
    assert!(cmdline.contains("-m gdd.ui"), "argv: {cmdline}");
    assert!(cmdline.contains("--root"), "argv: {cmdline}");
    assert!(
        cmdline.contains(env.config.workbench_root.to_str().unwrap()),
        "argv: {cmdline}"
    );
    assert!(
        cmdline.contains(&format!("--port {port}")),
        "argv: {cmdline}"
    );
    assert!(cmdline.contains("--no-open"), "argv: {cmdline}");
    assert!(
        cmdline.contains("--local"),
        "桌面壳必须以本地模式启动控制台，argv: {cmdline}"
    );
    assert!(handle.log_path.is_file(), "应创建控制台日志文件");

    kill_pid(pid);
    wait_port_free(port);
}

#[test]
fn running_console_is_reused_without_second_process() {
    let env = setup();
    let port = common::free_port();
    let mut external = start_external_console(&env, port);
    let original_pid = external.id();
    wait_ready(port);

    let handle = supervisor::ensure_console(&env.config, &env.paths, port).expect("应复用");
    assert!(!handle.started);
    assert_eq!(handle.pid, None);
    assert_eq!(
        common::console_process_count(&env.config.workbench_root),
        1,
        "不应产生第二个 gdd.ui 进程"
    );

    let output = Command::new("ps")
        .args(["-o", "pid=", "-p", &original_pid.to_string()])
        .output()
        .expect("ps");
    assert!(
        String::from_utf8_lossy(&output.stdout).trim() == original_pid.to_string(),
        "原控制台 PID 应保持不变"
    );

    kill_pid(original_pid);
    let _ = external.wait();
    wait_port_free(port);
}

/// 起一个固定状态码/响应体的假 HTTP 服务，返回端口、停止信号与线程句柄。
fn spawn_stub_http(
    status_line: &'static str,
    body: &'static [u8],
) -> (u16, mpsc::Sender<()>, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind dummy");
    let port = listener.local_addr().unwrap().port();
    listener.set_nonblocking(true).unwrap();
    let (stop_tx, stop_rx) = mpsc::channel::<()>();
    let server = std::thread::spawn(move || loop {
        match stop_rx.try_recv() {
            Ok(()) | Err(mpsc::TryRecvError::Disconnected) => break,
            Err(mpsc::TryRecvError::Empty) => {}
        }
        match listener.accept() {
            Ok((mut socket, _)) => {
                let mut buf = [0u8; 2048];
                let _ = socket.read(&mut buf);
                let head = format!(
                    "HTTP/1.0 {status_line}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
                    body.len()
                );
                let _ = socket.write_all(head.as_bytes());
                let _ = socket.write_all(body);
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(_) => break,
        }
    });
    (port, stop_tx, server)
}

#[test]
fn non_gdd_service_on_port_is_rejected_without_spawn() {
    let env = setup();
    let (port, stop_tx, server) = spawn_stub_http("200 OK", br#"{"hello":"not gdd"}"#);

    let error = supervisor::ensure_console(&env.config, &env.paths, port).unwrap_err();
    assert!(
        matches!(error, SupervisorError::PortOccupied { .. }),
        "应报端口占用：{error}"
    );
    assert!(error.to_string().contains(&port.to_string()));
    assert_eq!(
        common::console_process_count(&env.config.workbench_root),
        0,
        "端口被占用时不得启动控制台"
    );

    drop(stop_tx);
    server.join().unwrap();
}

#[test]
fn auth_required_console_is_rejected_without_spawn() {
    let env = setup();
    let (port, stop_tx, server) = spawn_stub_http(
        "401 Unauthorized",
        "{\"ok\":false,\"error\":\"未登录或登录已失效\"}".as_bytes(),
    );

    let error = supervisor::ensure_console(&env.config, &env.paths, port).unwrap_err();
    assert!(
        matches!(error, SupervisorError::ConsoleAuthRequired { .. }),
        "鉴权模式控制台应报需要登录而非端口占用：{error}"
    );
    let message = error.to_string();
    assert!(
        message.contains(&port.to_string()),
        "错误应包含端口：{message}"
    );
    assert!(
        message.contains("需要登录"),
        "错误应提示控制台需要登录：{message}"
    );
    assert!(
        message.contains("仅支持本地模式"),
        "错误应提示桌面 app 仅支持本地模式：{message}"
    );
    assert_eq!(
        common::console_process_count(&env.config.workbench_root),
        0,
        "鉴权模式控制台存在时不得启动第二个控制台"
    );

    drop(stop_tx);
    server.join().unwrap();
}

#[test]
fn console_exiting_before_ready_reports_log_path() {
    let temp = TempDir::new().unwrap();
    let gdd = common::make_fake_gdd_home(temp.path());
    let python = common::write_failing_python(temp.path());
    let workbench = temp.path().join("workbench");
    let config = common::base_config(&gdd, &python, &workbench)
        .validate()
        .expect("fake python 版本校验应通过");
    let paths = RuntimePaths::new(temp.path().join("config"), temp.path().join("logs"));
    let port = common::free_port();

    let error = supervisor::ensure_console(&config, &paths, port).unwrap_err();
    assert!(
        matches!(error, SupervisorError::EarlyExit { .. }),
        "应报提前退出：{error}"
    );
    assert!(error.to_string().contains("console.log"), "{error}");
}

#[test]
fn spawned_console_survives_test_process_lifetime_of_handle_logic() {
    // 验证普通 spawn 的 argv 与环境隔离：控制台能读到 PYTHONPATH（gdd 包在临时副本中）。
    let env = setup();
    let port = common::free_port();
    let handle = supervisor::ensure_console(&env.config, &env.paths, port).expect("冷启动就绪");
    assert!(handle.started);
    // 不带 PYTHONPATH 的 python 无法 import gdd，能就绪说明 app 正确注入了临时 GDD 源码路径。
    let pid = handle.pid.expect("pid");
    kill_pid(pid);
    wait_port_free(port);
}
