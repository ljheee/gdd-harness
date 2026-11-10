//! 配置模型、持久化与校验。
//!
//! 配置是用户可直接编辑的 JSON 文件（默认
//! `~/Library/Application Support/com.gdd.console/config.json`）。
//! 本模块不依赖 Tauri，便于单元测试。

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

pub const SUPPORTED_VERSION: u32 = 1;
/// 最低 Python 版本，编码为 `major * 100 + minor`（3.10 -> 310）。
pub const MIN_PYTHON: u32 = 310;

/// JSON 配置模型。字段名使用 camelCase，与文档/用户编辑保持一致。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, rename_all = "camelCase", default)]
pub struct Config {
    pub version: u32,
    pub gdd_home: String,
    pub python: String,
    pub workbench_root: String,
    pub path: Vec<String>,
    pub env: BTreeMap<String, String>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            version: SUPPORTED_VERSION,
            gdd_home: String::new(),
            python: String::new(),
            workbench_root: String::new(),
            path: Vec::new(),
            env: BTreeMap::new(),
        }
    }
}

/// 校验通过、路径展开/规范化后的配置。
#[derive(Debug, Clone)]
pub struct ValidatedConfig {
    pub gdd_home: PathBuf,
    pub python: PathBuf,
    pub workbench_root: PathBuf,
    pub path: Vec<PathBuf>,
    pub env: BTreeMap<String, String>,
}

impl ValidatedConfig {
    pub fn python_dir(&self) -> Option<&Path> {
        self.python.parent()
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("必填配置项 {field} 未设置（路径为空）。请编辑应用配置文件后重启 app")]
    Missing { field: &'static str },
    #[error("配置项 {field} 必须是绝对路径：{path}。请填写绝对路径")]
    NotAbsolute { field: &'static str, path: String },
    #[error("配置项 gddHome 的路径不存在或不可访问：{path}。请填写 GDD 源码根目录")]
    GddHomeMissing { path: String },
    #[error("配置项 gddHome 无效：{path} 下缺少 gdd/ui.py。请指向包含 gdd 包的 GDD 源码根目录")]
    GddPackageMissing { path: String },
    #[error(
        "配置项 python 无效：{path} 不存在或不可执行（{detail}）。请填写 Python 解释器绝对路径"
    )]
    PythonInvalid { path: String, detail: String },
    #[error("配置项 python 版本过低：{path} 报告版本 {found}，最低要求 3.10。请改用 Python 3.10+ 解释器")]
    PythonTooOld { path: String, found: String },
    #[error("配置项 workbenchRoot 无效：{path}（{detail}）。请填写可写的目录")]
    WorkbenchInvalid { path: String, detail: String },
    #[error("配置版本不受支持：version={version}（仅支持 1）")]
    VersionUnsupported { version: u32 },
    #[error("无法读取配置文件 {path}：{detail}")]
    Read { path: String, detail: String },
    #[error("配置文件 {path} 解析失败：{detail}")]
    Parse { path: String, detail: String },
    #[error("无法写入配置文件 {path}：{detail}")]
    Write { path: String, detail: String },
}

impl Config {
    /// 读取配置；不存在时生成默认配置并以 0600 权限写盘。
    pub fn load_or_create(path: &Path) -> Result<Config, ConfigError> {
        if path.exists() {
            let text = fs::read_to_string(path).map_err(|e| ConfigError::Read {
                path: path.display().to_string(),
                detail: e.to_string(),
            })?;
            return serde_json::from_str(&text).map_err(|e| ConfigError::Parse {
                path: path.display().to_string(),
                detail: e.to_string(),
            });
        }
        let config = Config::generate_default();
        config.save_to(path)?;
        Ok(config)
    }

    /// 以 0600 权限写盘（目录以 0700 创建）。
    pub fn save_to(&self, path: &Path) -> Result<(), ConfigError> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|e| ConfigError::Write {
                path: path.display().to_string(),
                detail: format!("无法创建目录 {}：{e}", parent.display()),
            })?;
            set_dir_private(parent);
        }
        let json = serde_json::to_string_pretty(self).expect("配置可序列化");
        let mut opts = OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut file = opts.open(path).map_err(|e| ConfigError::Write {
            path: path.display().to_string(),
            detail: e.to_string(),
        })?;
        file.write_all(json.as_bytes())
            .and_then(|_| file.write_all(b"\n"))
            .and_then(|_| file.sync_all())
            .map_err(|e| ConfigError::Write {
                path: path.display().to_string(),
                detail: e.to_string(),
            })?;
        set_file_private(path);
        Ok(())
    }

    /// 校验并返回展开后的绝对路径。所有错误带字段名与路径。
    pub fn validate(&self) -> Result<ValidatedConfig, ConfigError> {
        if self.version != SUPPORTED_VERSION {
            return Err(ConfigError::VersionUnsupported {
                version: self.version,
            });
        }

        let gdd_raw = self.gdd_home.trim();
        if gdd_raw.is_empty() {
            return Err(ConfigError::Missing { field: "gddHome" });
        }
        let gdd_path = PathBuf::from(expand_tilde(gdd_raw));
        if !gdd_path.is_absolute() {
            return Err(ConfigError::NotAbsolute {
                field: "gddHome",
                path: gdd_path.display().to_string(),
            });
        }
        let gdd_home = fs::canonicalize(&gdd_path).map_err(|_| ConfigError::GddHomeMissing {
            path: gdd_path.display().to_string(),
        })?;
        if !gdd_home.join("gdd").join("ui.py").is_file() {
            return Err(ConfigError::GddPackageMissing {
                path: gdd_home.display().to_string(),
            });
        }

        let python_raw = self.python.trim();
        if python_raw.is_empty() {
            return Err(ConfigError::Missing { field: "python" });
        }
        let python_path = PathBuf::from(expand_tilde(python_raw));
        if !python_path.is_absolute() {
            return Err(ConfigError::NotAbsolute {
                field: "python",
                path: python_path.display().to_string(),
            });
        }
        let python = fs::canonicalize(&python_path).map_err(|_| ConfigError::PythonInvalid {
            path: python_path.display().to_string(),
            detail: "路径不存在".to_string(),
        })?;
        if !is_executable(&python) {
            return Err(ConfigError::PythonInvalid {
                path: python.display().to_string(),
                detail: "不是可执行文件".to_string(),
            });
        }
        let version =
            query_python_version(&python).map_err(|detail| ConfigError::PythonInvalid {
                path: python.display().to_string(),
                detail,
            })?;
        if version < MIN_PYTHON {
            return Err(ConfigError::PythonTooOld {
                path: python.display().to_string(),
                found: format_version(version),
            });
        }

        let wb_raw = self.workbench_root.trim();
        if wb_raw.is_empty() {
            return Err(ConfigError::Missing {
                field: "workbenchRoot",
            });
        }
        let wb_path = PathBuf::from(expand_tilde(wb_raw));
        if !wb_path.is_absolute() {
            return Err(ConfigError::NotAbsolute {
                field: "workbenchRoot",
                path: wb_path.display().to_string(),
            });
        }
        let workbench_root = prepare_workbench(&wb_path)?;

        let mut path = Vec::new();
        for entry in &self.path {
            let entry = entry.trim();
            if entry.is_empty() {
                continue;
            }
            path.push(PathBuf::from(expand_tilde(entry)));
        }

        Ok(ValidatedConfig {
            gdd_home,
            python,
            workbench_root,
            path,
            env: self.env.clone(),
        })
    }

    /// 首次启动生成默认配置：编译期仓库根 + 可用的 Python + `~/gdd-workbench`。
    pub fn generate_default() -> Config {
        let home = home_dir();
        let gdd_home = compile_time_repo_root()
            .filter(|root| root.join("gdd").join("ui.py").is_file())
            .map(|p| p.display().to_string())
            .unwrap_or_default();
        let python = find_python()
            .map(|p| p.display().to_string())
            .unwrap_or_default();
        let workbench_root = home
            .as_ref()
            .map(|h| h.join("gdd-workbench").display().to_string())
            .unwrap_or_default();
        let mut path = Vec::new();
        if let Some(home) = home.as_ref() {
            for dir in [
                home.join(".local/bin"),
                PathBuf::from("/opt/homebrew/bin"),
                PathBuf::from("/usr/local/bin"),
            ] {
                if dir.is_dir() {
                    path.push(dir.display().to_string());
                }
            }
        }
        Config {
            version: SUPPORTED_VERSION,
            gdd_home,
            python,
            workbench_root,
            path,
            env: BTreeMap::new(),
        }
    }
}

/// 测试与运行期共享的路径集合。环境变量覆盖仅用于测试隔离。
#[derive(Debug, Clone)]
pub struct RuntimePaths {
    pub config_dir: PathBuf,
    pub log_dir: PathBuf,
}

impl RuntimePaths {
    pub fn new(config_dir: PathBuf, log_dir: PathBuf) -> Self {
        RuntimePaths {
            config_dir,
            log_dir,
        }
    }

    /// 测试可用 `GDD_CONSOLE_CONFIG_DIR` / `GDD_CONSOLE_LOG_DIR` 覆盖默认目录。
    pub fn with_env_overrides(config_dir: PathBuf, log_dir: PathBuf) -> Self {
        let config_dir = env_path("GDD_CONSOLE_CONFIG_DIR").unwrap_or(config_dir);
        let log_dir = env_path("GDD_CONSOLE_LOG_DIR").unwrap_or(log_dir);
        RuntimePaths {
            config_dir,
            log_dir,
        }
    }

    pub fn headless_defaults() -> Self {
        let home = home_dir().unwrap_or_else(|| PathBuf::from("."));
        RuntimePaths::with_env_overrides(
            home.join("Library/Application Support/com.gdd.console"),
            home.join("Library/Logs/com.gdd.console"),
        )
    }

    pub fn ensure_dirs(&self) -> io::Result<()> {
        fs::create_dir_all(&self.config_dir)?;
        fs::create_dir_all(&self.log_dir)?;
        set_dir_private(&self.config_dir);
        set_dir_private(&self.log_dir);
        Ok(())
    }

    pub fn config_file(&self) -> PathBuf {
        self.config_dir.join("config.json")
    }

    pub fn app_log(&self) -> PathBuf {
        self.log_dir.join("app.log")
    }

    pub fn console_log(&self) -> PathBuf {
        self.log_dir.join("console.log")
    }
}

fn env_path(key: &str) -> Option<PathBuf> {
    std::env::var_os(key)
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
}

/// `~` / `~/x` 展开；只在有 home 时展开，否则原样返回。
pub fn expand_tilde(value: &str) -> String {
    expand_tilde_with_home(value, home_dir().as_deref())
}

pub fn expand_tilde_with_home(value: &str, home: Option<&Path>) -> String {
    if value == "~" {
        return home
            .map(|h| h.display().to_string())
            .unwrap_or_else(|| value.to_string());
    }
    if let Some(rest) = value.strip_prefix("~/") {
        if let Some(home) = home {
            return home.join(rest).display().to_string();
        }
    }
    value.to_string()
}

pub fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
}

fn compile_time_repo_root() -> Option<PathBuf> {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf)
}

fn prepare_workbench(path: &Path) -> Result<PathBuf, ConfigError> {
    if path.exists() && !path.is_dir() {
        return Err(ConfigError::WorkbenchInvalid {
            path: path.display().to_string(),
            detail: "已存在但不是目录".to_string(),
        });
    }
    if !path.exists() {
        fs::create_dir_all(path).map_err(|e| ConfigError::WorkbenchInvalid {
            path: path.display().to_string(),
            detail: format!("无法创建目录：{e}"),
        })?;
    }
    let probe = path.join(format!(
        ".gdd-console-write-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    match fs::write(&probe, b"") {
        Ok(_) => {
            let _ = fs::remove_file(&probe);
        }
        Err(e) => {
            return Err(ConfigError::WorkbenchInvalid {
                path: path.display().to_string(),
                detail: format!("目录不可写：{e}"),
            });
        }
    }
    fs::canonicalize(path).map_err(|e| ConfigError::WorkbenchInvalid {
        path: path.display().to_string(),
        detail: format!("无法解析路径：{e}"),
    })
}

fn find_python() -> Option<PathBuf> {
    let mut dirs: Vec<PathBuf> = Vec::new();
    if let Some(home) = home_dir() {
        dirs.push(home.join(".local/bin"));
    }
    dirs.push(PathBuf::from("/opt/homebrew/bin"));
    dirs.push(PathBuf::from("/usr/local/bin"));
    if let Some(path) = std::env::var_os("PATH") {
        dirs.extend(std::env::split_paths(&path));
    }
    let mut seen: BTreeSet<PathBuf> = BTreeSet::new();
    for dir in dirs {
        for name in [
            "python3.13",
            "python3.12",
            "python3.11",
            "python3.10",
            "python3",
        ] {
            let candidate = dir.join(name);
            let Ok(canon) = fs::canonicalize(&candidate) else {
                continue;
            };
            if !seen.insert(canon.clone()) || !is_executable(&canon) {
                continue;
            }
            if let Ok(v) = query_python_version(&canon) {
                if v >= MIN_PYTHON {
                    return Some(canon);
                }
            }
        }
    }
    None
}

fn query_python_version(python: &Path) -> Result<u32, String> {
    let output = Command::new(python)
        .arg("-c")
        .arg("import sys; print(sys.version_info[0]*100+sys.version_info[1])")
        .output()
        .map_err(|e| format!("无法执行：{e}"))?;
    if !output.status.success() {
        return Err(format!(
            "版本查询失败（退出码 {:?}）：{}",
            output.status.code(),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    text.trim()
        .parse::<u32>()
        .map_err(|_| format!("无法解析版本输出：{text:?}"))
}

fn format_version(v: u32) -> String {
    format!("{}.{}", v / 100, v % 100)
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.metadata()
        .map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable(path: &Path) -> bool {
    path.is_file()
}

#[cfg(unix)]
fn set_file_private(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn set_file_private(_path: &Path) {}

#[cfg(unix)]
fn set_dir_private(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o700));
}

#[cfg(not(unix))]
fn set_dir_private(_path: &Path) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tilde_expansion() {
        let home = PathBuf::from("/Users/tester");
        assert_eq!(
            expand_tilde_with_home("~/gdd-workbench", Some(&home)),
            "/Users/tester/gdd-workbench"
        );
        assert_eq!(expand_tilde_with_home("~", Some(&home)), "/Users/tester");
        assert_eq!(
            expand_tilde_with_home("/abs/path", Some(&home)),
            "/abs/path"
        );
        assert_eq!(expand_tilde_with_home("rel/path", Some(&home)), "rel/path");
    }
}
