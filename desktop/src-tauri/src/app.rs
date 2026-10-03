#[cfg(not(target_os = "macos"))]
use crate::dialogs::open_close_confirm;
use crate::session::handle_deep_link;
use crate::window_labels::is_desktop_window;
#[cfg(target_os = "macos")]
use crate::windows::show_main_window;
use crate::windows::{active_desktop_label, toggle_quickask};
use crate::{actions, Shared};
#[cfg(not(target_os = "macos"))]
use crate::prefs;
use tauri::Manager;
use tauri_plugin_global_shortcut::ShortcutState;

pub fn run() {
    let app = tauri::Builder::default()
        // single-instance：第二次被 deep-link 拉起时，把 URL 转交给已运行实例。
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            for arg in argv.iter() {
                if arg.starts_with("hugagent://") {
                    handle_deep_link(app, arg.clone());
                }
            }
            if let Some(w) = app.get_webview_window(&active_desktop_label(app)) {
                // 可能此前被「最小化到托盘」隐藏了，这里要先 show 再 focus。
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        // A1 原生通知 / A3 自动更新。
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        // A2 全局快捷键：唯一注册的热键（Ctrl/Cmd+Shift+Space）按下即切换悬浮快速问答窗。
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        toggle_quickask(app);
                    }
                })
                .build(),
        )
        // 原生菜单栏事件分发（文件/编辑/视图/帮助）。
        .on_menu_event(actions::handle)
        .invoke_handler(tauri::generate_handler![
            crate::session::open_login,
            crate::session::logout_desktop
        ])
        // macOS 遵循平台习惯：红色关闭按钮只隐藏主窗口并继续驻留后台，退出由系统
        // 菜单或托盘显式执行。其他平台首次关闭时仍弹出自定义确认窗，可记住后续行为。
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if !is_desktop_window(window.label()) {
                    return;
                }
                let other_visible = window.app_handle().webview_windows().values().any(|w| {
                    is_desktop_window(w.label())
                        && w.label() != window.label()
                        && w.is_visible().unwrap_or(false)
                });
                if other_visible {
                    // Keep the original window as a tray/reopen anchor; extra windows can be destroyed.
                    if window.label() == "main" {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                    return;
                }
                api.prevent_close();

                #[cfg(target_os = "macos")]
                {
                    let _ = window.hide();
                    return;
                }

                #[cfg(not(target_os = "macos"))]
                {
                    let app = window.app_handle().clone();
                    let config_dir = app.state::<Shared>().config_dir.clone();

                    // 已记住选择 → 直接执行，不弹确认窗。
                    match prefs::load_close_action(&config_dir) {
                        Some(prefs::CloseAction::Minimize) => {
                            let _ = window.hide();
                        }
                        Some(prefs::CloseAction::Exit) => {
                            app.exit(0);
                        }
                        None => open_close_confirm(&app, window.label()),
                    }
                }
            }
        })
        .setup(crate::bootstrap::initialize)
        .build(tauri::generate_context!())
        .expect("运行 Tauri 应用失败");

    app.run(|_app_handle, _event| {
        // Tauri may tear down the async runtime before managed-state Drop runs.
        // Stop the Python process synchronously while the AppHandle and PID
        // metadata are still available; repeated exit events are idempotent.
        if matches!(
            _event,
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
        ) {
            let local_server = _app_handle.state::<Shared>().local_server.clone();
            if let Err(error) = local_server.shutdown() {
                eprintln!("[local-server] 退出时停止本机服务失败: {error}");
            }
        }

        // 主窗口被红色关闭按钮隐藏后，点击 Dock 图标应立即恢复，而不是只激活一个
        // 没有可见窗口的后台进程。
        #[cfg(target_os = "macos")]
        if let tauri::RunEvent::Reopen {
            has_visible_windows,
            ..
        } = _event
        {
            if !has_visible_windows {
                show_main_window(_app_handle);
            }
        }
    });
}
