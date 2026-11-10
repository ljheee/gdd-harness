#![allow(dead_code)]

use gdd_console_lib::config::{Config, RuntimePaths};
use std::fs;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::Command;

pub fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri 应有父目录")
        .to_path_buf()
}

pub fn copy_dir_all(src: &Path, dst: &Path) -> std::io::Result<()> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let name = entry.file_name();
        if name == "__pycache__" {
            continue;
        }
        let from = entry.path();
        let to = dst.join(&name);
        if entry.file_type()?.is_dir() {
            copy_dir_all(&from, &to)?;
        } else {
            fs::copy(&from, &to)?;
        }
    }
    Ok(())
}

/// 复制仓库 `gdd/` 包到临时目录，返回 GDD 源码根目录。
pub fn make_gdd_home(dir: &Path) -> PathBuf {
    let root = dir.join("gdd-src");
    copy_dir_all(&repo_root().join("gdd"), &root.join("gdd")).expect("复制 gdd 包");
    root
}

/// 只创建 `gdd/ui.py` 占位文件，供纯配置校验测试使用。
pub fn make_fake_gdd_home(dir: &Path) -> PathBuf {
    let root = dir.join("gdd-src");
    fs::create_dir_all(root.join("gdd")).expect("创建 gdd 目录");
    fs::write(root.join("gdd").join("ui.py"), b"").expect("写 ui.py");
    root
}

pub fn find_python() -> PathBuf {
    if let Ok(explicit) = std::env::var("GDD_TEST_PYTHON") {
        let path = PathBuf::from(explicit);
        if python_version(&path) >= 310 {
            return path;
        }
    }
    let mut dirs: Vec<PathBuf> = Vec::new();
    if let Some(home) = std::env::var_os("HOME") {
        dirs.push(PathBuf::from(home).join(".local/bin"));
    }
    dirs.push(PathBuf::from("/opt/homebrew/bin"));
    dirs.push(PathBuf::from("/usr/local/bin"));
    if let Some(path) = std::env::var_os("PATH") {
        dirs.extend(std::env::split_paths(&path));
    }
    for dir in dirs {
        for name in [
            "python3.13",
            "python3.12",
            "python3.11",
            "python3.10",
            "python3",
        ] {
            let candidate = dir.join(name);
            if candidate.is_file() && python_version(&candidate) >= 310 {
                return fs::canonicalize(&candidate).unwrap_or(candidate);
            }
        }
    }
    panic!("测试需要 Python >= 3.10（可用 GDD_TEST_PYTHON 指定）");
}

fn python_version(python: &Path) -> u32 {
    Command::new(python)
        .arg("-c")
        .arg("import sys; print(sys.version_info[0]*100+sys.version_info[1])")
        .output()
        .ok()
        .filter(|out| out.status.success())
        .and_then(|out| String::from_utf8_lossy(&out.stdout).trim().parse().ok())
        .unwrap_or(0)
}

/// 生成 fake Python 可执行文件：任何参数都输出指定版本号（3.12 -> 312）。
pub fn write_fake_python(dir: &Path, version: u32) -> PathBuf {
    let path = dir.join(format!("fake-python-{version}"));
    fs::write(&path, format!("#!/bin/sh\necho {version}\n")).expect("写 fake python");
    set_executable(&path);
    path
}

/// fake Python：`-c` 报告可用版本，真正执行 `-m gdd.ui` 时立即退出（模拟启动即失败）。
pub fn write_failing_python(dir: &Path) -> PathBuf {
    let path = dir.join("fake-python-failing");
    fs::write(
        &path,
        "#!/bin/sh\ncase \"$1\" in\n  -c) echo 312 ;;\n  *) echo 'boom: simulated failure' >&2; exit 3 ;;\nesac\n",
    )
    .expect("写 failing python");
    set_executable(&path);
    path
}

fn set_executable(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).expect("chmod");
    }
}

pub fn base_config(gdd_home: &Path, python: &Path, workbench: &Path) -> Config {
    Config {
        version: 1,
        gdd_home: gdd_home.display().to_string(),
        python: python.display().to_string(),
        workbench_root: workbench.display().to_string(),
        path: Vec::new(),
        env: Default::default(),
    }
}

pub fn runtime_paths(dir: &Path) -> RuntimePaths {
    let paths = RuntimePaths::new(dir.join("config"), dir.join("logs"));
    paths.ensure_dirs().expect("创建临时配置/日志目录");
    paths
}

pub fn free_port() -> u16 {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind 临时端口");
    listener.local_addr().expect("local addr").port()
}

/// 统计命令行同时包含 `gdd.ui` 与指定工作台的进程数。
pub fn console_process_count(workbench: &Path) -> usize {
    let output = Command::new("ps")
        .args(["-axo", "pid=,command="])
        .output()
        .expect("ps");
    let needle = format!("--root {}", workbench.display());
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| line.contains("gdd.ui") && line.contains(&needle))
        .count()
}
