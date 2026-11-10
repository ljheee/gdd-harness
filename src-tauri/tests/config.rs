mod common;

use gdd_console_lib::config::Config;
use std::fs;
use tempfile::TempDir;

#[test]
fn valid_config_passes_and_creates_workbench() {
    let temp = TempDir::new().unwrap();
    let gdd = common::make_fake_gdd_home(temp.path());
    let python = common::write_fake_python(temp.path(), 312);
    let workbench = temp.path().join("workbench");
    let config = common::base_config(&gdd, &python, &workbench);

    let valid = config.validate().expect("配置应通过校验");
    assert_eq!(valid.workbench_root, fs::canonicalize(&workbench).unwrap());
    assert!(workbench.is_dir(), "工作台应被创建");
}

#[test]
fn missing_gdd_home_reports_field() {
    let temp = TempDir::new().unwrap();
    let python = common::write_fake_python(temp.path(), 312);
    let config = Config {
        version: 1,
        gdd_home: String::new(),
        python: python.display().to_string(),
        workbench_root: temp.path().join("wb").display().to_string(),
        path: Vec::new(),
        env: Default::default(),
    };
    let error = config.validate().unwrap_err().to_string();
    assert!(error.contains("gddHome"), "{error}");
}

#[test]
fn nonexistent_gdd_home_reports_path() {
    let temp = TempDir::new().unwrap();
    let python = common::write_fake_python(temp.path(), 312);
    let missing = temp.path().join("does-not-exist");
    let config = common::base_config(&missing, &python, &temp.path().join("wb"));
    let error = config.validate().unwrap_err().to_string();
    assert!(error.contains("gddHome"), "{error}");
    assert!(error.contains(&missing.display().to_string()), "{error}");
}

#[test]
fn gdd_home_without_package_reports_expected_structure() {
    let temp = TempDir::new().unwrap();
    let root = temp.path().join("no-package");
    fs::create_dir_all(&root).unwrap();
    let python = common::write_fake_python(temp.path(), 312);
    let config = common::base_config(&root, &python, &temp.path().join("wb"));
    let error = config.validate().unwrap_err().to_string();
    assert!(error.contains("gddHome"), "{error}");
    assert!(error.contains("gdd/ui.py"), "{error}");
}

#[test]
fn nonexistent_python_reports_path() {
    let temp = TempDir::new().unwrap();
    let gdd = common::make_fake_gdd_home(temp.path());
    let missing = temp.path().join("no-python");
    let config = common::base_config(&gdd, &missing, &temp.path().join("wb"));
    let error = config.validate().unwrap_err().to_string();
    assert!(error.contains("python"), "{error}");
    assert!(error.contains(&missing.display().to_string()), "{error}");
}

#[test]
fn python_39_is_rejected_and_310_accepted() {
    let temp = TempDir::new().unwrap();
    let gdd = common::make_fake_gdd_home(temp.path());
    let old = common::write_fake_python(temp.path(), 309);
    let config = common::base_config(&gdd, &old, &temp.path().join("wb"));
    let error = config.validate().unwrap_err().to_string();
    assert!(error.contains("python"), "{error}");
    assert!(error.contains("3.10"), "{error}");
    assert!(error.contains(&old.display().to_string()), "{error}");

    let ok = common::write_fake_python(temp.path(), 310);
    let config = common::base_config(&gdd, &ok, &temp.path().join("wb-310"));
    assert!(config.validate().is_ok());

    let newer = common::write_fake_python(temp.path(), 312);
    let config = common::base_config(&gdd, &newer, &temp.path().join("wb-312"));
    assert!(config.validate().is_ok());
}

#[test]
fn workbench_regular_file_is_rejected() {
    let temp = TempDir::new().unwrap();
    let gdd = common::make_fake_gdd_home(temp.path());
    let python = common::write_fake_python(temp.path(), 312);
    let file = temp.path().join("not-a-dir");
    fs::write(&file, b"x").unwrap();
    let config = common::base_config(&gdd, &python, &file);
    let error = config.validate().unwrap_err().to_string();
    assert!(error.contains("workbenchRoot"), "{error}");
    assert!(error.contains(&file.display().to_string()), "{error}");
}

#[test]
fn unsupported_version_is_rejected() {
    let temp = TempDir::new().unwrap();
    let gdd = common::make_fake_gdd_home(temp.path());
    let python = common::write_fake_python(temp.path(), 312);
    let mut config = common::base_config(&gdd, &python, &temp.path().join("wb"));
    config.version = 2;
    let error = config.validate().unwrap_err().to_string();
    assert!(error.contains("version"), "{error}");
    assert!(error.contains("2"), "{error}");
}

#[test]
fn unknown_field_and_invalid_json_are_parse_errors() {
    let unknown = r#"{
        "version": 1,
        "gddHome": "/tmp/gdd",
        "python": "/usr/bin/python3",
        "workbenchRoot": "/tmp/wb",
        "extraField": true
    }"#;
    let error = serde_json::from_str::<Config>(unknown)
        .unwrap_err()
        .to_string();
    assert!(error.contains("extraField"), "{error}");

    assert!(serde_json::from_str::<Config>("{not-json").is_err());
}

#[test]
fn save_sets_private_permissions() {
    let temp = TempDir::new().unwrap();
    let gdd = common::make_fake_gdd_home(temp.path());
    let python = common::write_fake_python(temp.path(), 312);
    let config = common::base_config(&gdd, &python, &temp.path().join("wb"));
    let path = temp.path().join("nested").join("config.json");
    config.save_to(&path).expect("写配置");
    assert!(path.is_file());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "配置文件应仅当前用户可读写");
    }
}

#[test]
fn load_or_create_writes_default_config() {
    let temp = TempDir::new().unwrap();
    let path = temp.path().join("config.json");
    let config = Config::load_or_create(&path).expect("生成默认配置");
    assert_eq!(config.version, 1);
    assert!(path.is_file());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
    }
    let reloaded = Config::load_or_create(&path).expect("重新读取");
    assert_eq!(reloaded, config);
}
