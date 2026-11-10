//! Tauri 壳：启动线程里完成配置校验与控制台探测/拉起，就绪后再创建窗口。
//!
//! 窗口只加载 `http://127.0.0.1:8765/`；关闭窗口只隐藏，Dock 重新打开时重新
//! 执行探测/复用流程；app 退出时不终止控制台。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use tauri::menu::{MenuBuilder, MenuItemBuilder, SubmenuBuilder};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};

use crate::config::{self, RuntimePaths, ValidatedConfig};
use crate::log;
use crate::supervisor::{self, ConsoleHandle, DEFAULT_PORT};

struct AppState {
    quitting: AtomicBool,
    ensuring: AtomicBool,
    config: Mutex<Option<ValidatedConfig>>,
    paths: Mutex<Option<RuntimePaths>>,
    console: Mutex<Option<ConsoleHandle>>,
}

pub fn run() -> i32 {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState {
            quitting: AtomicBool::new(false),
            ensuring: AtomicBool::new(false),
            config: Mutex::new(None),
            paths: Mutex::new(None),
            console: Mutex::new(None),
        })
        .setup(|app| {
            app.state::<AppState>()
                .ensuring
                .store(true, Ordering::SeqCst);
            let paths = resolve_paths(app.handle());
            if let Err(e) = paths.ensure_dirs() {
                eprintln!("无法创建配置/日志目录：{e}");
            }
            if let Err(e) = log::init(&paths.app_log()) {
                eprintln!("无法初始化日志 {}：{e}", paths.app_log().display());
            }
            log::event(format!(
                "event=startup configDir={} logDir={}",
                paths.config_dir.display(),
                paths.log_dir.display()
            ));
            install_menu(app)?;
            let handle = app.handle().clone();
            std::thread::spawn(move || startup(handle, paths));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<AppState>();
                if state.quitting.load(Ordering::SeqCst) {
                    return;
                }
                api.prevent_close();
                let _ = window.hide();
                log::event(format!("event=window_hidden label={}", window.label()));
            }
        })
        .on_menu_event(|app, event| {
            if event.id().as_ref() == "quit" {
                app.state::<AppState>()
                    .quitting
                    .store(true, Ordering::SeqCst);
                log::event("event=quit_requested");
                app.exit(0);
            }
        });

    match builder.build(tauri::generate_context!()) {
        Ok(app) => {
            app.run(|app_handle, event| {
                if let RunEvent::Reopen {
                    has_visible_windows,
                    ..
                } = event
                {
                    if !has_visible_windows {
                        log::event("event=reopen");
                        reopen(app_handle.clone());
                    }
                }
            });
            0
        }
        Err(e) => {
            eprintln!("无法启动 GDD Console：{e}");
            1
        }
    }
}

fn install_menu(app: &mut tauri::App) -> tauri::Result<()> {
    let quit = MenuItemBuilder::with_id("quit", "退出 GDD Console")
        .accelerator("CmdOrCtrl+Q")
        .build(app)?;
    let app_menu = SubmenuBuilder::new(app, "GDD Console")
        .item(&quit)
        .build()?;
    let menu = MenuBuilder::new(app).item(&app_menu).build()?;
    app.set_menu(menu).map(|_| ())
}

fn resolve_paths(app: &AppHandle) -> RuntimePaths {
    let fallback_config = config::home_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("Library/Application Support/com.gdd.console");
    let fallback_log = config::home_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("Library/Logs/com.gdd.console");
    let config_dir = app.path().app_config_dir().unwrap_or(fallback_config);
    let log_dir = app.path().app_log_dir().unwrap_or(fallback_log);
    RuntimePaths::with_env_overrides(config_dir, log_dir)
}

fn startup(app: AppHandle, paths: RuntimePaths) {
    {
        let state = app.state::<AppState>();
        *state.paths.lock().expect("paths lock") = Some(paths.clone());
    }
    let result = (|| -> Result<(ValidatedConfig, ConsoleHandle), String> {
        let config =
            config::Config::load_or_create(&paths.config_file()).map_err(|e| e.to_string())?;
        let valid = config.validate().map_err(|e| e.to_string())?;
        log::event(format!(
            "event=config path={} gddHome={} python={} workbenchRoot={} envKeys={}",
            paths.config_file().display(),
            valid.gdd_home.display(),
            valid.python.display(),
            valid.workbench_root.display(),
            valid.env.keys().cloned().collect::<Vec<_>>().join(",")
        ));
        let port = supervisor::test_port_override().unwrap_or(DEFAULT_PORT);
        let handle = supervisor::ensure_console(&valid, &paths, port).map_err(|e| e.to_string())?;
        Ok((valid, handle))
    })();

    app.state::<AppState>()
        .ensuring
        .store(false, Ordering::SeqCst);

    match result {
        Ok((valid, handle)) => {
            {
                let state = app.state::<AppState>();
                *state.config.lock().expect("config lock") = Some(valid);
                *state.console.lock().expect("console lock") = Some(handle);
            }
            log::event("event=startup_ready");
            on_ready(&app);
        }
        Err(message) => fail(&app, &paths, &message),
    }
}

/// Dock 重新打开：重新探测/复用控制台，成功后显示窗口并导航。
fn reopen(app: AppHandle) {
    let state = app.state::<AppState>();
    if state.ensuring.swap(true, Ordering::SeqCst) {
        return;
    }
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        let paths = state.paths.lock().expect("paths lock").clone();
        let config = state.config.lock().expect("config lock").clone();
        let port = supervisor::test_port_override().unwrap_or(DEFAULT_PORT);
        let result = match (paths.clone(), config) {
            (Some(paths), Some(config)) => supervisor::ensure_console(&config, &paths, port),
            _ => Err(supervisor::SupervisorError::ProbeFailed {
                port: DEFAULT_PORT,
                detail: "应用尚未完成首次启动".to_string(),
            }),
        };
        state.ensuring.store(false, Ordering::SeqCst);
        match result {
            Ok(handle) => {
                *state.console.lock().expect("console lock") = Some(handle);
                log::event("event=reopen_ready");
                on_ready(&app);
            }
            Err(e) => {
                let paths = paths.unwrap_or_else(RuntimePaths::headless_defaults);
                fail(&app, &paths, &e.to_string());
            }
        }
    });
}

fn on_ready(app: &AppHandle) {
    let url = app
        .state::<AppState>()
        .console
        .lock()
        .expect("console lock")
        .as_ref()
        .map(|handle| handle.url.clone());
    let Some(url) = url else {
        return;
    };
    let app_handle = app.clone();
    if let Err(e) = app.run_on_main_thread(move || {
        create_or_show_window(&app_handle, &url);
        if std::env::var("GDD_CONSOLE_TEST_EXIT_AFTER_READY")
            .ok()
            .as_deref()
            == Some("1")
        {
            log::event("event=test_exit");
            let handle = app_handle.clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(500));
                handle.exit(0);
            });
        }
    }) {
        log::event(format!(
            "event=error error=run_on_main_thread_failed detail={e}"
        ));
    }
}

fn create_or_show_window(app: &AppHandle, url: &str) {
    let parsed: tauri::Url = match url.parse() {
        Ok(parsed) => parsed,
        Err(e) => {
            log::event(format!(
                "event=error error=invalid_url url={url} detail={e}"
            ));
            return;
        }
    };
    let expected_port = parsed.port_or_known_default();
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
        let _ = window.navigate(parsed);
        log::event(format!("event=window_shown url={url}"));
        maybe_dump_page(&window);
        maybe_close_window(&window);
        return;
    }
    match WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed))
        .title("GDD 控制台")
        .inner_size(1200.0, 800.0)
        .min_inner_size(800.0, 600.0)
        .center()
        .on_navigation(move |target| {
            target.host_str() == Some("127.0.0.1")
                && target.port_or_known_default() == expected_port
        })
        .build()
    {
        Ok(window) => {
            log::event(format!("event=window_created url={url}"));
            maybe_dump_page(&window);
            maybe_close_window(&window);
        }
        Err(e) => log::event(format!("event=error error=window_create_failed detail={e}")),
    }
}

/// 仅测试钩子：窗口创建后自动触发一次关闭，验证“关闭窗口不退出 app”。
fn maybe_close_window(window: &tauri::WebviewWindow) {
    if std::env::var("GDD_CONSOLE_TEST_CLOSE_AFTER_READY")
        .ok()
        .as_deref()
        != Some("1")
    {
        return;
    }
    let window = window.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(4000));
        log::event("event=test_close_requested");
        if let Err(e) = window.close() {
            log::event(format!("event=error error=test_close_failed detail={e}"));
        }
    });
}

/// 仅测试钩子：把窗口当前 URL 与页面 DOM 文本写入日志，供无屏幕录制权限的
/// 环境验证“窗口确实加载了控制台页面”。不向页面暴露任何 IPC 能力。
fn maybe_dump_page(window: &tauri::WebviewWindow) {
    if std::env::var("GDD_CONSOLE_TEST_DUMP_PAGE").ok().as_deref() != Some("1") {
        return;
    }
    let window = window.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(2500));
        match window.url() {
            Ok(url) => log::event(format!("event=window_url url={url}")),
            Err(e) => log::event(format!("event=window_url_error detail={e}")),
        }
        let script = "({title: document.title, \
            url: location.href, \
            text: ((document.body && document.body.innerText) || '').slice(0, 4000)})";
        let _ = window.eval_with_callback(script, |result| {
            let one_line = result.replace(['\r', '\n'], " ");
            log::event(format!("event=window_content {one_line}"));
        });
    });
}

fn fail(app: &AppHandle, paths: &RuntimePaths, message: &str) {
    let one_line = message.replace(['\r', '\n'], " ");
    log::event(format!(
        "event=error error={} config={} appLog={}",
        one_line,
        paths.config_file().display(),
        paths.app_log().display()
    ));
    let full = format!(
        "{message}\n\n配置文件：{}\n应用日志：{}",
        paths.config_file().display(),
        paths.app_log().display()
    );
    if std::env::var("GDD_CONSOLE_TEST_NO_DIALOG").ok().as_deref() == Some("1") {
        app.exit(1);
        return;
    }
    app.dialog()
        .message(full)
        .title("GDD 控制台启动失败")
        .kind(MessageDialogKind::Error)
        .blocking_show();
    app.exit(1);
}
