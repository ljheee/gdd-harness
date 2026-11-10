//! 以 argv 数组（不经过 shell）拉起 `python -m gdd.ui`。
//!
//! 子进程与 app 生命周期解耦：stdin 为 null，stdout/stderr 追加写入日志文件，
//! app 退出时不发送终止信号，控制台继续运行并由下次启动复用。

use crate::config::{RuntimePaths, ValidatedConfig};
use std::fs::{self, OpenOptions};
use std::io;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};

pub struct SpawnedConsole {
    pub child: Child,
    pub log_path: PathBuf,
}

pub fn spawn_console(
    config: &ValidatedConfig,
    paths: &RuntimePaths,
    port: u16,
) -> io::Result<SpawnedConsole> {
    fs::create_dir_all(&paths.log_dir)?;
    let log_path = paths.console_log();
    let stdout = open_log(&log_path)?;
    let stderr = stdout.try_clone()?;

    let mut command = Command::new(&config.python);
    command
        .arg("-m")
        .arg("gdd.ui")
        .arg("--root")
        .arg(&config.workbench_root)
        .arg("--port")
        .arg(port.to_string())
        .arg("--no-open")
        .current_dir(&config.workbench_root)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));

    // PYTHONPATH：GDD 源码根目录优先，保留继承值。
    let mut python_paths: Vec<PathBuf> = vec![config.gdd_home.clone()];
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        if !existing.is_empty() {
            python_paths.extend(std::env::split_paths(&existing));
        }
    }
    if let Ok(joined) = std::env::join_paths(&python_paths) {
        command.env("PYTHONPATH", joined);
    }

    // PATH：配置目录 + Python 所在目录 + 继承 PATH（GUI 启动时找回 Agent CLI）。
    let mut path_dirs: Vec<PathBuf> = config.path.clone();
    if let Some(python_dir) = config.python_dir() {
        path_dirs.push(python_dir.to_path_buf());
    }
    if let Some(existing) = std::env::var_os("PATH") {
        path_dirs.extend(std::env::split_paths(&existing));
    }
    if let Ok(joined) = std::env::join_paths(&path_dirs) {
        command.env("PATH", joined);
    }
    command.env("PYTHONUNBUFFERED", "1");

    // 显式配置的环境变量最后应用（例如 GDD_AGENT_BIN）；不写入日志。
    for (key, value) in &config.env {
        command.env(key, value);
    }

    let child = command.spawn()?;
    Ok(SpawnedConsole { child, log_path })
}

fn open_log(path: &PathBuf) -> io::Result<std::fs::File> {
    let mut opts = OpenOptions::new();
    opts.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let file = opts.open(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    }
    Ok(file)
}
