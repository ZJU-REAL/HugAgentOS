//! 顶部菜单：macOS 用系统菜单栏（`build`），Windows/Linux 用 `proxy.rs` 注入的窗口内
//! 标题栏菜单。两边的动作最终都汇到本文件的 `dispatch_for_window`，动作 id 只有一套。
//!
//! 菜单动作由 Rust 侧处理、**不经 WebView IPC**——反代这种远程源下自定义命令会被
//! Tauri 的 ACL 拒绝，所以壳层能力（新建对话 / 设置服务器 / 检查更新）都不依赖 IPC。
//! 编辑、全屏等用系统预定义项（`PredefinedMenuItem`），撤销/复制/粘贴由系统直接作用于
//! 焦点输入框，无需自己接线。

// 菜单构建相关的类型只有 macOS 的 `build` 用得到；其它平台只走下面的动作分发。
#[cfg(target_os = "macos")]
use tauri::menu::{AboutMetadataBuilder, Menu, MenuItem, SubmenuBuilder};
#[cfg(target_os = "macos")]
use tauri::Runtime;
use tauri::menu::MenuEvent;
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

use crate::brand;
use crate::Shared;

/// macOS 专用的系统应用菜单。Windows/Linux 不挂原生菜单——那两个平台显示的是
/// `proxy.rs` 注入的窗口内标题栏菜单（`TB_MENU`），菜单项只在那边定义一份。
#[cfg(target_os = "macos")]
pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let config = crate::config::load(&app.state::<Shared>().config_dir);
    let hybrid =
        brand::HYBRID_ONLY || config.provision_mode() == crate::config::ProvisionMode::Dual;
    let local_capable = config.provision_mode() != crate::config::ProvisionMode::CloudOnly;
    let about = AboutMetadataBuilder::new()
        .name(Some(brand::NAME.to_string()))
        .version(Some(app.package_info().version.to_string()))
        .build();
    let application = SubmenuBuilder::new(app, brand::NAME)
        .about(Some(about))
        .separator()
        .text("server_config", "设置…")
        .text("check_update", "检查更新…")
        .separator()
        .services()
        .separator()
        .hide()
        .hide_others()
        .show_all()
        .separator()
        .quit()
        .build()?;

    let mut file = SubmenuBuilder::new(app, "文件")
        .item(&MenuItem::with_id(app, "new_window", "新建窗口", true, Some("CmdOrCtrl+Shift+N"))?)
        .text("new_chat", "新建对话");
    // 仅交付混合模式的包没有别的形态可切，不摆一个点了也没意义的入口。
    if !hybrid {
        file = file.text("run_mode", "运行模式…");
    }
    if local_capable {
        file = file.text("open_folder", "打开文件夹…");
    }
    if !hybrid {
        file = file.text("local_server", "本机服务…");
    }
    let file = file.separator().quit().build()?;

    let edit = SubmenuBuilder::new(app, "编辑")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .build()?;

    let view = SubmenuBuilder::new(app, "显示")
        .text("reload", "重新加载")
        .separator()
        .item(&MenuItem::with_id(app, "zoom_in", "放大", true, Some("CmdOrCtrl+Plus"))?)
        .item(&MenuItem::with_id(app, "zoom_out", "缩小", true, Some("CmdOrCtrl+-"))?)
        .item(&MenuItem::with_id(app, "zoom_reset", "实际大小", true, Some("CmdOrCtrl+0"))?)
        .separator()
        .fullscreen()
        .build()?;

    let window = SubmenuBuilder::new(app, "窗口")
        .minimize()
        .maximize()
        .build()?;

    let help = SubmenuBuilder::new(app, "帮助")
        .text("website", "访问官网")
        .build()?;

    Menu::with_items(app, &[&application, &file, &edit, &view, &window, &help])
}

/// 菜单事件分发。托盘的同名动作也复用这里（见 `build_tray`）。
pub fn handle(app: &AppHandle, event: MenuEvent) {
    dispatch(app, event.id.as_ref());
}

/// 按菜单项 id 执行动作。抽出来让托盘菜单也能直接调。
pub fn dispatch(app: &AppHandle, id: &str) {
    dispatch_for_window(app, id, &crate::active_desktop_label(app));
}

pub fn dispatch_for_window(app: &AppHandle, id: &str, label: &str) {
    match id {
        "new_window" => {
            let app = app.clone();
            // WebView2 creation must not block the menu/navigation event loop.
            // run_on_main_thread is still synchronous when already on that thread.
            tauri::async_runtime::spawn_blocking(move || crate::new_desktop_window(&app));
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
        "server_config" => crate::open_server_config_in(app, label),
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
        "zoom_in" => crate::adjust_user_zoom(app, 1),
        "zoom_out" => crate::adjust_user_zoom(app, -1),
        "zoom_reset" => crate::adjust_user_zoom(app, 0),
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
