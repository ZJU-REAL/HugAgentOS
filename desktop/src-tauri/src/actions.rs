//! Executes desktop commands shared by native menus, tray and navigation adapters.
use crate::{brand, Shared};
use tauri::menu::MenuEvent;
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

/// 菜单事件分发。托盘的同名动作也复用这里（见 `build_tray`）。
pub fn handle(app: &AppHandle, event: MenuEvent) {
    dispatch(app, event.id.as_ref());
}

/// 按菜单项 id 执行动作。抽出来让托盘菜单也能直接调。
pub fn dispatch(app: &AppHandle, id: &str) {
    dispatch_for_window(app, id, &crate::windows::active_desktop_label(app));
}

pub fn dispatch_for_window(app: &AppHandle, id: &str, label: &str) {
    match id {
        "new_window" => {
            let app = app.clone();
            // WebView2 creation must not block the menu/navigation event loop.
            // run_on_main_thread is still synchronous when already on that thread.
            tauri::async_runtime::spawn_blocking(move || crate::windows::new_desktop_window(&app));
        }
        // 新建对话：主窗口整页导航回首页（= 全新对话就绪态）。
        "new_chat" => {
            if let Some(w) = app.get_webview_window(label) {
                let port = app.state::<Shared>().port;
                let _ = w.eval(format!(
                    "window.location.replace('http://127.0.0.1:{}/')",
                    port
                ));
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }
        "open_folder" => {
            let shared = app.state::<Shared>();
            let cfg = crate::config::load(&shared.config_dir);
            if cfg.provision_mode() == crate::config::ProvisionMode::CloudOnly {
                return;
            }
            let app = app.clone();
            let label = label.to_string();
            app.clone().dialog().file().pick_folder(move |picked| {
                let Some(path) = picked.and_then(|p| p.into_path().ok()) else { return };
                if let Some(window) = app.get_webview_window(&label) {
                    let detail = serde_json::to_string(&path.to_string_lossy()).unwrap();
                    let _ = window.eval(format!(
                        "if(window.dispatchEvent(new CustomEvent('hugagent:open-project-folder',{{detail:{detail},cancelable:true}}))){{sessionStorage.setItem('hugagent:pending-project-folder',{detail});window.location.replace('/');}}"
                    ));
                }
            });
        }
        "server_config" => crate::windows::open_server_config_in(app, label),
        // 运行模式选择页（本机 / 云端 / 双模式）——初始化选型的再次入口。
        "run_mode" => {
            if let Some(w) = app.get_webview_window(label) {
                let port = app.state::<Shared>().port;
                let _ = w.eval(format!(
                    "window.location.replace('http://127.0.0.1:{}/__desktop/init?manage=1')",
                    port
                ));
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }
        "local_server" => {
            if let Some(w) = app.get_webview_window(label) {
                let port = app.state::<Shared>().port;
                let _ = w.eval(format!(
                    "window.location.replace('http://127.0.0.1:{}/__desktop/setup?manage=1')",
                    port
                ));
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }
        "reload" => {
            if let Some(w) = app.get_webview_window(label) {
                let _ = w.eval("window.location.reload()");
            }
        }
        // 页面缩放：只改用户自己的档位，系统 DPI 仍由 WebView 原生处理。
        "zoom_in" => crate::display::adjust_user_zoom(app, 1),
        "zoom_out" => crate::display::adjust_user_zoom(app, -1),
        "zoom_reset" => crate::display::adjust_user_zoom(app, 0),
        "check_update" => {
            let update_base = app.state::<Shared>().update_base.clone();
            crate::update::check_and_install(app.clone(), update_base, false);
        }
        "website" => {
            let shared = app.state::<Shared>();
            let target = if brand::WEBSITE_URL.is_empty() {
                shared.server_base.clone()
            } else {
                brand::WEBSITE_URL.to_string()
            };
            let _ = app.opener().open_url(target, None::<String>);
        }
        _ => {
            // about / 系统预定义项由系统自行处理，这里无需接管；未知 id 兜底提示。
            if id == "about" {
                app.dialog()
                    .message(format!(
                        "{} 桌面客户端\n当前版本：{}",
                        brand::NAME,
                        app.package_info().version
                    ))
                    .title("关于")
                    .blocking_show();
            }
        }
    }
}

#[cfg(all(test, any(target_os = "windows", target_os = "linux")))]
#[path = "menu_native_tests.rs"]
mod native_tests;
