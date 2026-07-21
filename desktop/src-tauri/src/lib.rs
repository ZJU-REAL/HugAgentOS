//! HugAgentOS桌面客户端（Tauri v2 瘦客户端）。
//!
//! 方案 B —— 系统浏览器跳转登录 + deep-link 唤起：
//!   1. 启动：起本地反代（127.0.0.1:随机端口），加载已存 token；
//!   2. 已登录 → 窗口直接加载 `http://127.0.0.1:<port>/`（前端经反代访问后端）；
//!   3. 未登录 → 窗口加载登录卡片（初始态），用户点「开始使用」再打开**系统浏览器**到 `<server>/?desktop=1`；
//!   4. 浏览器登录成功 → 前端换一次性 handoff 票据 → 跳 `hugagent://auth/callback?ticket=`；
//!   5. OS 唤起 App → `redeem` 票据换回真正 token → 存盘 + 反代注入 cookie → 窗口跳首页；
//!   6. 会话过期：前端要跳外部 SSO 时被导航守卫拦下 → 清 token + 重走系统浏览器登录。

mod auth;
mod brand;
mod config;
mod local_server;
mod menu;
mod notify;
mod prefs;
mod proxy;
mod update;

use std::sync::Arc;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
use tauri_plugin_opener::OpenerExt;
use tokio::sync::RwLock;

/// Keep this value identical for every WebView2 window in the process. Fractional Windows DPI
/// scaling makes WebView2 return inconsistent sub-pixel coordinates to Ant Design's popup
/// positioning code, which can place dropdowns far outside the visible window. Pinning the device
/// scale to an integer fixes the coordinates; `apply_display_zoom` restores the expected visual
/// size afterwards.
pub(crate) const WEBVIEW_BROWSER_ARGS: &str = "--force-device-scale-factor=1";

/// 根据系统 DPI 与当前显示器物理分辨率共同计算 WebView 缩放。
///
/// 远程桌面（尤其 Mac Retina → Windows）可能报告「高 DPI + 较低虚拟分辨率」。若直接把
/// `scale_factor` 当 zoom，会把页面和图标二次放大。这里以 2560×1440 为缩放基准，并把
/// 最大 zoom 限制为 1.5：1080p 远程桌面保持 1.0，4K 本地屏最多使用 1.5。
fn adaptive_display_zoom(scale_factor: f64, width: u32, height: u32) -> f64 {
    let safe_scale = if scale_factor.is_finite() {
        scale_factor.clamp(1.0, 1.5)
    } else {
        1.0
    };
    let resolution_cap = ((width as f64 / 2560.0).min(height as f64 / 1440.0)).clamp(1.0, 1.5);
    safe_scale.min(resolution_cap)
}

/// 主窗口初始尺寸同样按显示器的逻辑分辨率计算，避免远程会话中 1280×860 逻辑像素
/// 被高 DPI 放大后超出可用桌面。
fn adaptive_window_dimensions(
    physical_width: u32,
    physical_height: u32,
    scale_factor: f64,
) -> (f64, f64, f64, f64) {
    let scale = if scale_factor.is_finite() {
        scale_factor.max(1.0)
    } else {
        1.0
    };
    let logical_width = physical_width as f64 / scale;
    let logical_height = physical_height as f64 / scale;
    let available_width = (logical_width - 32.0).max(760.0);
    let available_height = (logical_height - 48.0).max(520.0);
    let width = (logical_width * 0.86)
        .clamp(960.0, 1280.0)
        .min(available_width);
    let height = (logical_height * 0.86)
        .clamp(640.0, 860.0)
        .min(available_height);
    (width, height, width.min(960.0), height.min(640.0))
}

fn main_window_dimensions(app: &tauri::AppHandle) -> (f64, f64, f64, f64) {
    app.primary_monitor()
        .ok()
        .flatten()
        .map(|monitor| {
            let size = monitor.size();
            adaptive_window_dimensions(size.width, size.height, monitor.scale_factor())
        })
        .unwrap_or((1280.0, 860.0, 960.0, 640.0))
}

/// `WEBVIEW_BROWSER_ARGS` 把 WebView2 设备缩放固定为整数 1，消除分数 DPI 下 Ant Design
/// 弹层坐标漂移；这里再施加经过分辨率限幅的视觉缩放。
pub(crate) fn apply_display_zoom(window: &tauri::WebviewWindow) {
    let scale_factor = window.scale_factor().unwrap_or(1.0);
    let monitor_size = window
        .current_monitor()
        .ok()
        .flatten()
        .map(|monitor| *monitor.size())
        .or_else(|| window.inner_size().ok())
        .unwrap_or(tauri::PhysicalSize::new(1920, 1080));
    let zoom = adaptive_display_zoom(scale_factor, monitor_size.width, monitor_size.height);
    let _ = window.set_zoom(zoom);
}

/// 跨组件共享的运行时状态（经 Tauri manage 注入）。
pub(crate) struct Shared {
    pub(crate) server_base: String,
    pub(crate) update_base: String,
    pub(crate) token: Arc<RwLock<Option<String>>>,
    pub(crate) http: reqwest::Client,
    pub(crate) port: u16,
    pub(crate) config_dir: std::path::PathBuf,
}

impl Shared {
    fn login_url(&self) -> String {
        format!("{}/?desktop=1", self.server_base.trim_end_matches('/'))
    }
    fn home_url(&self) -> String {
        format!("http://127.0.0.1:{}/", self.port)
    }
    /// 登录页「等待态」：浏览器已自动拉起，页面显示 spinner（启动 / 会话过期走这里）。
    fn waiting_url(&self) -> String {
        format!("http://127.0.0.1:{}/__desktop/login?waiting=1", self.port)
    }
    /// 登录页「初始态」：显示「登录」按钮，等用户点击再开浏览器（退出登录走这里）。
    fn login_idle_url(&self) -> String {
        format!("http://127.0.0.1:{}/__desktop/login", self.port)
    }
}

/// 登录页按钮 → 打开系统浏览器登录页。
#[tauri::command]
fn open_login(app: tauri::AppHandle) {
    let shared = app.state::<Shared>();
    let _ = app.opener().open_url(shared.login_url(), None::<String>);
}

/// 前端退出登录时由 webview 调用：清掉本地 token（内存 + 落盘），把窗口切回登录页。
/// 解决「退出后前端跳外部 SSO / 内部空路由 → 白屏」的问题。
#[tauri::command]
async fn logout_desktop(app: tauri::AppHandle) {
    {
        let shared = app.state::<Shared>();
        *shared.token.write().await = None;
        auth::save_token(&shared.config_dir, None);
    }
    let idle = app.state::<Shared>().login_idle_url();
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval(format!("window.location.replace('{}')", idle));
        let _ = w.set_focus();
    }
}

pub fn run() {
    tauri::Builder::default()
        // single-instance：第二次被 deep-link 拉起时，把 URL 转交给已运行实例。
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            for arg in argv.iter() {
                if arg.starts_with("hugagent://") {
                    handle_deep_link(app, arg.clone());
                }
            }
            if let Some(w) = app.get_webview_window("main") {
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
        .on_menu_event(menu::handle)
        .invoke_handler(tauri::generate_handler![open_login, logout_desktop])
        // 关闭主窗口时不直接退出：首次弹出**自定义确认窗**（带「记住我的选择」勾选框）
        // 问「最小化到托盘」还是「退出」。只有勾选后才记住，之后关闭直接执行、不再弹。
        // 可在托盘「关闭时重新询问」重置。（自定义窗而非原生对话框，是因为原生对话框
        // 不支持勾选框。）
        .on_window_event(|window, event| {
            // RDP/ToDesk 调整分辨率、或把窗口移到不同 DPI 的显示器时重新计算 zoom。
            // `set_zoom` 不改变外窗尺寸，因此处理 Resized 不会形成窗口 resize 循环。
            if matches!(
                event,
                tauri::WindowEvent::ScaleFactorChanged { .. } | tauri::WindowEvent::Resized(_)
            ) {
                if let Some(webview) = window.app_handle().get_webview_window(window.label()) {
                    apply_display_zoom(&webview);
                }
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() != "main" {
                    return;
                }
                api.prevent_close();
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
                    None => open_close_confirm(&app),
                }
            }
        })
        .setup(|app| {
            let handle = app.handle().clone();

            let config_dir = app
                .path()
                .app_config_dir()
                .unwrap_or_else(|_| std::path::PathBuf::from("."));
            std::fs::create_dir_all(&config_dir).ok();

            let mut cfg = config::load(&config_dir);

            // NSIS 交互安装可选择「同时安装本机服务」。选择通过一次性 pending
            // 文件交给应用消费；自动更新不写 pending，用户日后的菜单切换也不会
            // 被旧安装选择覆盖。该机制同时兼容已有 server.json 的旧版客户端升级。
            if let Some(installer_mode) = config::pending_installer_mode(&config_dir) {
                match installer_mode {
                    config::DeploymentMode::Local => {
                        config::save_local_server(&config_dir, local_server::LOCAL_SERVER_BASE)
                            .map_err(std::io::Error::other)?;
                    }
                    config::DeploymentMode::Remote => {
                        config::save_server_base(&config_dir, cfg.server_base_trimmed())
                            .map_err(std::io::Error::other)?;
                    }
                }
                config::clear_pending_installer_mode(&config_dir).map_err(std::io::Error::other)?;
                cfg = config::load(&config_dir);
            }

            let http = reqwest::Client::builder()
                .danger_accept_invalid_certs(cfg.insecure_tls)
                .no_proxy()
                .build()
                .expect("构建 http client 失败");

            let resource_dir = app
                .path()
                .resource_dir()
                .unwrap_or_else(|_| std::path::PathBuf::from("."));
            let local_data_dir = app
                .path()
                .app_local_data_dir()
                .unwrap_or_else(|_| config_dir.clone());
            let installer_name = if cfg!(target_os = "macos") {
                "install-local-server.sh"
            } else {
                "install-local-server.ps1"
            };
            let local_server = local_server::LocalServerManager::new(
                local_data_dir.join("local-server"),
                resource_dir.join("server-ce"),
                resource_dir.join("server-bootstrap").join(installer_name),
                http.clone(),
            );

            // 本机模式下，安装包版本变化会自动升级服务资源；已安装且同版本则直接
            // 拉起服务。安装/启动都在后台进行，主窗口显示可观察、可重试的进度页。
            if cfg.uses_local_server() {
                if local_server.needs_install() {
                    local_server.install_in_background();
                } else if !tauri::async_runtime::block_on(local_server.is_ready()) {
                    local_server.start_in_background();
                }
            }

            let backend_ready = if cfg.uses_local_server() {
                tauri::async_runtime::block_on(local_server.is_ready())
            } else {
                tauri::async_runtime::block_on(local_server::LocalServerManager::probe_base(
                    &http,
                    cfg.server_base_trimmed(),
                ))
            };

            // 后端可达时校验已存 token：已吊销/过期就清盘。后端暂时不可达时保留
            // token，避免一次断网把有效桌面会话永久注销。
            let mut token0 = auth::load_token(&config_dir);
            if backend_ready {
                if let Some(t) = token0.clone() {
                    let valid = tauri::async_runtime::block_on(auth::validate(
                        &http,
                        cfg.server_base_trimmed(),
                        &cfg.cookie_name,
                        &t,
                    ));
                    if !valid {
                        token0 = None;
                        auth::save_token(&config_dir, None);
                    }
                }
            }
            let token = Arc::new(RwLock::new(token0.clone()));

            let web_dir = resolve_web_dir(app);

            // 同步起反代拿到端口（仅绑定 + 后台 spawn，很快返回）。
            let pstate = proxy::ProxyState {
                http: http.clone(),
                server_base: cfg.server_base_trimmed().to_string(),
                cookie_name: cfg.cookie_name.clone(),
                token: token.clone(),
                local_server: local_server.clone(),
                active_local: cfg.uses_local_server(),
            };
            let port = tauri::async_runtime::block_on(proxy::serve(pstate, web_dir))
                .expect("启动本地反代失败");

            app.manage(Shared {
                server_base: cfg.server_base.clone(),
                update_base: cfg.update_base(),
                token: token.clone(),
                http: http.clone(),
                port,
                config_dir: config_dir.clone(),
            });

            // 运行时 deep-link 回调（macOS / 已运行实例）。
            {
                let h = handle.clone();
                app.deep_link().on_open_url(move |event| {
                    for url in event.urls() {
                        handle_deep_link(&h, url.to_string());
                    }
                });
            }
            // Linux / Windows 开发期运行时注册协议（打包安装时由安装器注册）。
            #[cfg(any(target_os = "linux", target_os = "windows"))]
            {
                let _ = app.deep_link().register("hugagent");
            }

            // 初始窗口：有 token 进首页，没有则进登录卡片「初始态」（不自动开浏览器，
            // 等用户点「开始使用」再拉起）。退出登录同样回到这张卡片，避免白屏。
            let start = if !backend_ready {
                format!("http://127.0.0.1:{}/__desktop/setup", port)
            } else if token0.is_some() {
                format!("http://127.0.0.1:{}/", port)
            } else {
                format!("http://127.0.0.1:{}/__desktop/login", port)
            };
            build_window(&handle, &start)?;

            // 系统托盘：关闭窗口时「最小化到托盘」后，从这里恢复主窗口。
            build_tray(app)?;

            // A1：后台通知轮询——自动化/后台任务跑完发原生系统通知。
            notify::start(handle.clone(), port, token.clone(), http.clone());

            // A2：注册全局快捷键 Ctrl/Cmd+Shift+Space（唤起悬浮快速问答窗）。
            let qa = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::Space);
            if let Err(e) = app.global_shortcut().register(qa) {
                eprintln!("[shortcut] 注册全局快捷键失败: {e}");
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("运行 Tauri 应用失败");
}

/// 显示并聚焦主窗口（从托盘恢复 / 单实例再次拉起 / deep-link 回跳时用）。
fn show_main_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// A2：切换悬浮「快速问答」窗——已可见且聚焦则隐藏，否则显示/创建并聚焦。
/// 复用主前端 `?quickask=1` 紧凑模式（chatStream.ts 全套能力，零重复）。
fn toggle_quickask(app: &tauri::AppHandle) {
    // 未登录时 quickask 前端会白屏，退化为唤起主窗（回登录卡片）。
    let logged_in = app
        .state::<Shared>()
        .token
        .try_read()
        .map(|g| g.is_some())
        .unwrap_or(true);
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
    let _ = WebviewWindowBuilder::new(app, "quickask", WebviewUrl::External(parsed))
        .title(format!("{} · 快速问答", brand::NAME))
        .additional_browser_args(WEBVIEW_BROWSER_ARGS)
        .inner_size(680.0, 540.0)
        .min_inner_size(480.0, 360.0)
        .always_on_top(true)
        .skip_taskbar(true)
        .center()
        .focused(true)
        .build()
        .map(|window| apply_display_zoom(&window));
}

/// 在主窗口打开「设置服务器地址」页。独立 WebView 小窗在部分 Windows/WebView2 环境下
/// 可能只创建出空白窗口；复用已经完成初始化的主 WebView 更稳定，也不依赖 Tauri IPC。
pub(crate) fn open_server_config(app: &tauri::AppHandle) {
    let port = app.state::<Shared>().port;
    if let Some(w) = app.get_webview_window("main") {
        let url = format!("http://127.0.0.1:{port}/__desktop/server-config");
        let _ = w.eval(format!("window.location.assign('{url}')"));
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// 构建系统托盘：左键单击恢复主窗口；右键菜单「显示主窗口 / 退出」。
/// 配合「关闭即最小化到托盘」，让应用关窗后仍在后台运行（自动化任务等）。
fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let show_i = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
    let new_chat_i = MenuItem::with_id(app, "tray_new_chat", "新建对话", true, None::<&str>)?;
    let update_i = MenuItem::with_id(app, "tray_check_update", "检查更新…", true, None::<&str>)?;
    let ask_i = MenuItem::with_id(app, "ask_close", "关闭时重新询问", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_i, &new_chat_i, &update_i, &ask_i, &quit_i])?;

    let mut builder = TrayIconBuilder::new()
        .tooltip(brand::NAME)
        .menu(&menu)
        .show_menu_on_left_click(false)
        // 托盘项用 `tray_*` 前缀 id，避开主菜单全局事件处理器（menu::handle）的动作 id，
        // 防止同一事件被托盘 + 全局两处重复触发；这里再映射回统一的 menu::dispatch。
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main_window(app),
            "tray_new_chat" => menu::dispatch(app, "new_chat"),
            "tray_check_update" => menu::dispatch(app, "check_update"),
            // 清除记住的关闭行为 → 下次点关闭又会弹「最小化 / 退出」框。
            "ask_close" => {
                let dir = app.state::<Shared>().config_dir.clone();
                prefs::clear_close_action(&dir);
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

/// 弹出「关闭确认」自定义窗（带「记住我的选择」勾选框）。按钮不走 Tauri IPC——
/// 整页导航到哨兵路径 `/__desktop/close-decide?action=..&remember=..`，由本窗口的
/// 导航守卫解析并执行（最小化 / 退出 + 是否记住）。远程源下 IPC 不可靠，导航守卫必触发。
fn open_close_confirm(app: &tauri::AppHandle) {
    // 已经开着就聚焦，别重复弹。
    if let Some(w) = app.get_webview_window("close-confirm") {
        let _ = w.set_focus();
        return;
    }
    let port = app.state::<Shared>().port;
    let url = format!("http://127.0.0.1:{}/__desktop/close-confirm", port);
    let parsed = match url::Url::parse(&url) {
        Ok(u) => u,
        Err(_) => return,
    };
    let app_for_nav = app.clone();
    let _ = WebviewWindowBuilder::new(app, "close-confirm", WebviewUrl::External(parsed))
        .title(brand::NAME)
        .additional_browser_args(WEBVIEW_BROWSER_ARGS)
        .inner_size(460.0, 250.0)
        .resizable(false)
        .minimizable(false)
        .maximizable(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .center()
        .focused(true)
        .on_navigation(move |u| {
            // 只拦哨兵；确认页自身的加载 / 其它放行。
            if !(matches!(u.scheme(), "http" | "https")
                && u.host_str() == Some("127.0.0.1")
                && u.path() == "/__desktop/close-decide")
            {
                return true;
            }
            let mut action = String::new();
            let mut remember = false;
            for (k, v) in u.query_pairs() {
                match k.as_ref() {
                    "action" => action = v.into_owned(),
                    "remember" => remember = v == "1",
                    _ => {}
                }
            }
            let app2 = app_for_nav.clone();
            tauri::async_runtime::spawn(async move {
                let exit = action == "exit";
                if remember {
                    let dir = app2.state::<Shared>().config_dir.clone();
                    prefs::save_close_action(
                        &dir,
                        if exit {
                            prefs::CloseAction::Exit
                        } else {
                            prefs::CloseAction::Minimize
                        },
                    );
                }
                if let Some(cw) = app2.get_webview_window("close-confirm") {
                    let _ = cw.close();
                }
                if exit {
                    app2.exit(0);
                } else if let Some(mw) = app2.get_webview_window("main") {
                    let _ = mw.hide();
                }
            });
            false
        })
        .build()
        .map(|window| apply_display_zoom(&window));
}

/// 创建主窗口，并挂导航守卫：只放行本地反代 / Tauri 内部源，其余外部跳转
/// （典型：会话过期后前端要跳外部 SSO 授权页）一律拦下，改走系统浏览器登录。
fn build_window(app: &tauri::AppHandle, url: &str) -> tauri::Result<()> {
    let parsed = url::Url::parse(url).expect("窗口起始 URL 非法");
    let app_for_nav = app.clone();
    let (width, height, min_width, min_height) = main_window_dimensions(app);

    let builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed))
        .title(brand::NAME)
        .additional_browser_args(WEBVIEW_BROWSER_ARGS)
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
        .traffic_light_position(tauri::LogicalPosition::new(14.0, 13.0));
    #[cfg(not(target_os = "macos"))]
    let builder = builder.decorations(false);

    let window = builder
        .on_navigation(move |u| {
            let scheme = u.scheme();
            // Tauri 内部 / 数据类源放行。
            if matches!(scheme, "tauri" | "about" | "data" | "blob") {
                return true;
            }
            // 本地反代（同源）：默认放行——但前端若**整页跳转**到「登录落地页」
            // （后端 logout 返回的 `/login`、会话过期兜底的 `/mock-sso` 等），这些
            // 路由在桌面 SPA 内并不存在，放行必然白屏。把它们识别为「需重新登录」
            // 信号、拦下走原生登录流程。我们自己的原生登录页 `/__desktop/*` 放行。
            // 注：SPA 内部的前端路由切换走 history API，不触发 on_navigation，故不受影响。
            if matches!(scheme, "http" | "https") && u.host_str() == Some("127.0.0.1") {
                let path = u.path();
                // 「开始使用」/「重新打开」按钮：整页导航到这个哨兵路径。不依赖 Tauri IPC
                // ——远程源（本地反代）下 `window.__TAURI__` 不保证注入、invoke 会静默失效，
                // 而 on_navigation 是纯 Rust、一定触发。这里由壳子开系统浏览器 + 切等待态。
                if path == "/__desktop/open-login" {
                    let app2 = app_for_nav.clone();
                    tauri::async_runtime::spawn(async move {
                        let shared = app2.state::<Shared>();
                        let _ = app2.opener().open_url(shared.login_url(), None::<String>);
                        if let Some(w) = app2.get_webview_window("main") {
                            let _ = w.eval(format!(
                                "window.location.replace('{}')",
                                shared.waiting_url()
                            ));
                        }
                    });
                    return false;
                }
                // 服务设置页动作同样走导航哨兵，避免依赖远程源下不稳定的 Tauri IPC。
                if path == "/__desktop/connect-server" {
                    open_server_config(&app_for_nav);
                    return false;
                }
                if path == "/__desktop/save-server" {
                    let base = u
                        .query_pairs()
                        .find_map(|(key, value)| (key == "base").then(|| value.into_owned()))
                        .unwrap_or_default();
                    let app2 = app_for_nav.clone();
                    tauri::async_runtime::spawn(async move {
                        if !base.trim().is_empty() {
                            let dir = app2.state::<Shared>().config_dir.clone();
                            if let Err(error) = config::save_server_base(&dir, &base) {
                                eprintln!("[config] 保存 server.json 失败: {error}");
                                return;
                            }
                        }
                        app2.dialog()
                            .message("服务器地址已保存，点击确定重启客户端生效。")
                            .title(brand::NAME)
                            .blocking_show();
                        app2.restart();
                    });
                    return false;
                }
                // 自定义标题栏的窗口动作走导航哨兵，避免远程源下依赖 Tauri IPC。
                if path == "/__desktop/win" {
                    let action = u
                        .query_pairs()
                        .find_map(|(key, value)| (key == "action").then(|| value.into_owned()))
                        .unwrap_or_default();
                    if let Some(window) = app_for_nav.get_webview_window("main") {
                        match action.as_str() {
                            "minimize" => {
                                let _ = window.minimize();
                            }
                            "toggle-maximize" => {
                                if window.is_maximized().unwrap_or(false) {
                                    let _ = window.unmaximize();
                                } else {
                                    let _ = window.maximize();
                                }
                            }
                            "fullscreen" => {
                                let fullscreen = window.is_fullscreen().unwrap_or(false);
                                let _ = window.set_fullscreen(!fullscreen);
                            }
                            "drag" => {
                                let _ = window.start_dragging();
                            }
                            // 延迟关闭，避免在 on_navigation 中嵌套触发 CloseRequested。
                            "close" => {
                                tauri::async_runtime::spawn(async move {
                                    let _ = window.close();
                                });
                            }
                            "quit" => app_for_nav.exit(0),
                            _ => {}
                        }
                    }
                    return false;
                }
                // 菜单动作复用 menu::dispatch；切到主线程下一拍执行，避免导航回调重入。
                if path == "/__desktop/menu" {
                    let action = u
                        .query_pairs()
                        .find_map(|(key, value)| (key == "action").then(|| value.into_owned()))
                        .unwrap_or_default();
                    let app = app_for_nav.clone();
                    let _ = app_for_nav.run_on_main_thread(move || {
                        menu::dispatch(&app, &action);
                    });
                    return false;
                }
                if path == "/__desktop/retry-server" {
                    app_for_nav.restart();
                }
                if path == "/__desktop/activate-local" {
                    let app2 = app_for_nav.clone();
                    tauri::async_runtime::spawn(async move {
                        let dir = app2.state::<Shared>().config_dir.clone();
                        if let Err(error) =
                            config::save_local_server(&dir, local_server::LOCAL_SERVER_BASE)
                        {
                            eprintln!("[config] 切换本机服务失败: {error}");
                            return;
                        }
                        app2.restart();
                    });
                    return false;
                }
                let is_login_landing = !path.starts_with("/__desktop")
                    && (path == "/login"
                        || path.starts_with("/login/")
                        || path.starts_with("/mock-sso")
                        || path.contains("/sso/"));
                if !is_login_landing {
                    return true;
                }
            }
            // 外部导航 或 同源登录落地页（退出登录 / 会话过期）：拦下 → 清 token →
            // 回到登录卡片「初始态」。**不自动开浏览器**——桌面端退出后应停在「开始使用」
            // 卡片，等用户主动点击再登录，而不是突兀地弹出系统浏览器（也不再白屏）。
            let app2 = app_for_nav.clone();
            tauri::async_runtime::spawn(async move {
                let shared = app2.state::<Shared>();
                *shared.token.write().await = None;
                auth::save_token(&shared.config_dir, None);
                if let Some(w) = app2.get_webview_window("main") {
                    let _ = w.eval(format!(
                        "window.location.replace('{}')",
                        shared.login_idle_url()
                    ));
                }
            });
            false
        })
        .build()?;

    apply_display_zoom(&window);

    // macOS application menus belong in the system menu bar. Windows/Linux use
    // the in-window menu injected by proxy.rs to avoid a second chrome row.
    #[cfg(target_os = "macos")]
    match menu::build(app) {
        Ok(menu) => {
            if let Err(error) = window.set_menu(menu) {
                eprintln!("[menu] 挂载 macOS 原生菜单失败: {error}");
            }
        }
        Err(error) => eprintln!("[menu] 构建 macOS 原生菜单失败: {error}"),
    }

    Ok(())
}

/// 处理 deep-link：解析 ticket → 兑换 token → 存盘 + 注入反代 → 窗口跳首页。
fn handle_deep_link(app: &tauri::AppHandle, raw_url: String) {
    let Some(ticket) = parse_ticket(&raw_url) else {
        return;
    };
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let shared = app.state::<Shared>();
        match auth::redeem(&shared.http, &shared.server_base, &ticket).await {
            Ok(tok) => {
                *shared.token.write().await = Some(tok.clone());
                auth::save_token(&shared.config_dir, Some(&tok));
                let home = shared.home_url();
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.eval(format!("window.location.replace('{}')", home));
                }
                show_main_window(&app);
            }
            Err(e) => {
                eprintln!("[deep-link] 兑换 token 失败: {e}");
            }
        }
    });
}

/// 从 `hugagent://auth/callback?ticket=XXX` 抽取 ticket。
fn parse_ticket(raw_url: &str) -> Option<String> {
    let parsed = url::Url::parse(raw_url).ok()?;
    parsed
        .query_pairs()
        .find(|(k, _)| k == "ticket")
        .map(|(_, v)| v.into_owned())
        .filter(|t| !t.is_empty())
}

/// 解析前端静态资源目录：优先打包资源 `web/`，开发期回落到仓库内的 dist。
fn resolve_web_dir(app: &tauri::App) -> std::path::PathBuf {
    if let Ok(res) = app.path().resource_dir() {
        let p = res.join("web");
        if p.join("index.html").exists() {
            return p;
        }
    }
    for cand in [
        "../src/frontend/dist",
        "../../src/frontend/dist",
        "src/frontend/dist",
    ] {
        let p = std::path::PathBuf::from(cand);
        if p.join("index.html").exists() {
            return p;
        }
    }
    eprintln!("[web] 未找到前端 dist；请先在 src/frontend 执行 npm run build");
    app.path()
        .resource_dir()
        .map(|r| r.join("web"))
        .unwrap_or_else(|_| std::path::PathBuf::from("web"))
}

#[cfg(test)]
mod display_tests {
    use super::*;

    #[test]
    fn remote_retina_dpi_is_capped_by_virtual_resolution() {
        assert_eq!(adaptive_display_zoom(2.0, 1920, 1080), 1.0);
        assert_eq!(adaptive_display_zoom(1.5, 2560, 1440), 1.0);
    }

    #[test]
    fn high_resolution_display_gets_moderate_zoom() {
        assert_eq!(adaptive_display_zoom(2.0, 3840, 2160), 1.5);
        assert_eq!(adaptive_display_zoom(1.25, 3840, 2160), 1.25);
    }

    #[test]
    fn remote_window_stays_inside_logical_desktop() {
        assert_eq!(
            adaptive_window_dimensions(1920, 1080, 2.0),
            (928.0, 520.0, 928.0, 520.0)
        );
        assert_eq!(
            adaptive_window_dimensions(3840, 2160, 2.0),
            (1280.0, 860.0, 960.0, 640.0)
        );
    }
}
