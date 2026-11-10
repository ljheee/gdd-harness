//! 简单文件日志器：每行 `unix秒 事件`，0600 权限。
//!
//! 不记录配置 `env` 的值（可能含密钥），只记录键名。

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

struct Sink {
    file: File,
}

static SINK: OnceLock<Mutex<Sink>> = OnceLock::new();

pub fn init(path: &Path) -> std::io::Result<()> {
    if SINK.get().is_some() {
        return Ok(());
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
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
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    let _ = SINK.set(Mutex::new(Sink { file }));
    Ok(())
}

pub fn event(message: impl AsRef<str>) {
    let line = format!("{} {}\n", now_secs(), message.as_ref());
    if let Some(sink) = SINK.get() {
        if let Ok(mut sink) = sink.lock() {
            let _ = sink.file.write_all(line.as_bytes());
            let _ = sink.file.flush();
        }
    }
    eprint!("[gdd-console] {line}");
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
