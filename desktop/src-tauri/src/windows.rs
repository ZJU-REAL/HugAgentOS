use crate::display::main_window_dimensions;
use crate::display::{apply_user_zoom, restore_zoom_on_load};
#[cfg(target_os = "macos")]
use crate::menu;
use crate::window_events::navigate as handle_navigation;
use crate::window_labels::{is_desktop_window, is_session_window};
use crate::{brand, config, Shared};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::DialogExt;

pub(crate) fn active_desktop_label(app: &tauri::AppHandle) -> String {
    let windows = app.webview_windows();
    windows
        .values()
        .filter(|w| is_desktop_window(w.label()))
        .find(|w| w.is_focused().unwrap_or(false))
        .or_else(|| {
            windows
                .values()
                .filter(|w| is_desktop_window(w.label()))
                .find(|w| w.is_visible().unwrap_or(false))
        })
        .map(|w| w.label().to_string())
        .unwrap_or_else(|| "main".to_string())
}

pub(crate) fn navigate_session_windows(app: &tauri::AppHandle, url: &str) {
    for window in app.webview_windows().values() {
        if is_session_window(window.label()) {
            let destination =
                if window.label() == "quickask" && url == app.state::<Shared>().home_url() {
                    format!("{url}?quickask=1")
                } else {
                    url.to_string()
                };
            let target = serde_json::to_string(&destination).unwrap();
            let _ = window.eval(format!("window.location.replace({target})"));
        }
    }
}

pub(crate) fn new_desktop_window(app: &tauri::AppHandle) {
    static NEXT_WINDOW: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    let shared = app.state::<Shared>();
    let cfg = config::load(&shared.config_dir);
    let path = if !config::is_provisioned(&shared.config_dir) {
        "/__desktop/init"
    } else if cfg.uses_local_server()
        || (shared.hybrid_local && shared.local_server.needs_install())
    {
        "/__desktop/setup"
    } else if shared.session.has_token() {
        "/"
    } else {
        "/__desktop/login"
    };
    let label = format!(
        "main-{}",
        NEXT_WINDOW.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    );
    let url = format!("http://127.0.0.1:{}{path}", shared.port);
    if let Err(error) = build_window(app, &label, &url) {
        app.dialog()
            .message(format!("无法新建窗口：{error}"))
            .title(brand::NAME)
            .show(|_| {});
    }
}

/// 显示并聚焦主窗口（从托盘恢复 / 单实例再次拉起 / deep-link 回跳时用）。
pub(crate) fn show_main_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window(&active_desktop_label(app)) {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// A2：切换悬浮「快速问答」窗——已可见且聚焦则隐藏，否则显示/创建并聚焦。
/// 复用主前端 `?quickask=1` 紧凑模式（chatStream.ts 全套能力，零重复）。
pub(crate) fn toggle_quickask(app: &tauri::AppHandle) {
    // 未登录时 quickask 前端会白屏，退化为唤起主窗（回登录卡片）。
    let logged_in = app.state::<Shared>().session.has_token();
    if !logged_in {
        show_main_window(app);
        return;
    }

    if let Some(w) = app.get_webview_window("quickask") {
        let visible = w.is_visible().unwrap_or(false);
        let focused = w.is_focused().unwrap_or(false);
        if visible && focused {
            let _ = w.hide();
        } else {
            let _ = w.show();
            let _ = w.unminimize();
            let _ = w.set_focus();
        }
        return;
    }

    let port = app.state::<Shared>().port;
    let url = format!("http://127.0.0.1:{}/?quickask=1", port);
    let parsed = match url::Url::parse(&url) {
        Ok(u) => u,
        Err(_) => return,
    };
    let app_for_nav = app.clone();
    let _ = WebviewWindowBuilder::new(app, "quickask", WebviewUrl::External(parsed))
        .title(format!("{} · 快速问答", brand::NAME))
        // 关掉 webview 内建的拖放拦截：它会吞掉 OS 文件拖入，页面收不到带
        // File 对象的 HTML5 drop 事件，输入框的拖拽上传（useFileDropZone）失效
        .disable_drag_drop_handler()
        .inner_size(680.0, 540.0)
        .min_inner_size(480.0, 360.0)
        .always_on_top(true)
        .skip_taskbar(true)
        .center()
        .focused(true)
        .on_page_load(restore_zoom_on_load)
        .on_navigation(move |url| crate::window_events::navigate(&app_for_nav, "quickask", url))
        .build()
        .map(|window| apply_user_zoom(&window));
}

/// 在主窗口打开「设置服务器地址」页。独立 WebView 小窗在部分 Windows/WebView2 环境下
/// 可能只创建出空白窗口；复用已经完成初始化的主 WebView 更稳定，也不依赖 Tauri IPC。
pub(crate) fn open_server_config_in(app: &tauri::AppHandle, label: &str) {
    let port = app.state::<Shared>().port;
    if let Some(w) = app.get_webview_window(label) {
        let url = format!("http://127.0.0.1:{port}/__desktop/server-config");
        let _ = w.eval(format!("window.location.assign('{url}')"));
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

pub(crate) fn build_window(app: &tauri::AppHandle, label: &str, url: &str) -> tauri::Result<()> {
    let parsed = url::Url::parse(url).expect("窗口起始 URL 非法");
    let app_for_nav = app.clone();
    let window_label = label.to_string();
    let (width, height, min_width, min_height) = main_window_dimensions(app);

    let builder = WebviewWindowBuilder::new(app, label, WebviewUrl::External(parsed))
        .title(brand::NAME)
        // 同 quickask：禁用内建拖放拦截，HTML5 drop 事件才能携带文件进到页面
        .disable_drag_drop_handler()
        .inner_size(width, height)
        .min_inner_size(min_width, min_height);

    // Windows/Linux keep the compact custom chrome. macOS uses native window
    // decorations and traffic lights, with content extending into a translucent
    // toolbar in the same visual hierarchy as Codex and other modern Mac apps.
    #[cfg(target_os = "macos")]
    let builder = builder
        .decorations(true)
        .title_bar_style(tauri::TitleBarStyle::Overlay)
        .hidden_title(true)
        // Move the native controls down 8 logical pixels within the existing sidebar inset.
        .traffic_light_position(tauri::LogicalPosition::new(14.0, 21.0));
    #[cfg(not(target_os = "macos"))]
    let builder = builder.decorations(false);

    let window = builder
        .on_page_load(restore_zoom_on_load)
        .on_navigation(move |u| handle_navigation(&app_for_nav, &window_label, u))
        .build()?;

    apply_user_zoom(&window);

    // macOS application menus belong in the system menu bar. Windows/Linux use
    // the in-window menu injected by proxy.rs to avoid a second chrome row.
    #[cfg(target_os = "macos")]
    if label == "main" {
        match menu::build(app) {
            Ok(menu) => {
                // macOS menus are app-wide; per-window set_menu is unsupported.
                if let Err(error) = app.set_menu(menu) {
                    eprintln!("[menu] 挂载 macOS 原生菜单失败: {error}");
                }
            }
            Err(error) => eprintln!("[menu] 构建 macOS 原生菜单失败: {error}"),
        }
    }

    Ok(())
}
