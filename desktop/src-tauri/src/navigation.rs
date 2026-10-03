use crate::actions;
use crate::session::{
    cancel_device_login, clear_desktop_session, reset_login_state, start_device_login,
};
use crate::windows::{navigate_session_windows, open_server_config_in};
use crate::{brand, config, local_server, Shared};
use tauri::Manager;
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;
pub(crate) fn is_shell_origin(u: &url::Url, port: u16) -> bool {
    u.scheme() == "http"
        && u.host_str() == Some("127.0.0.1")
        && u.port_or_known_default() == Some(port)
        && u.username().is_empty()
        && u.password().is_none()
}

pub(crate) fn handle_navigation(app_for_nav: &tauri::AppHandle, label: &str, u: &url::Url) -> bool {
    let window_label = label.to_string();

    let scheme = u.scheme();
    // Tauri 内部 / 数据类源放行。
    if scheme == "tauri" || u.as_str() == "about:blank" {
        return true;
    }
    // 本地反代（同源）：默认放行——但前端若**整页跳转**到「登录落地页」
    // （后端 logout 返回的 `/login`、会话过期兜底的 `/mock-sso` 等），这些
    // 路由在桌面 SPA 内并不存在，放行必然白屏。把它们识别为「需重新登录」
    // 信号、拦下走原生登录流程。我们自己的原生登录页 `/__desktop/*` 放行。
    // 注：SPA 内部的前端路由切换走 history API，不触发 on_navigation，故不受影响。
    if is_shell_origin(u, app_for_nav.state::<Shared>().port) {
        let path = u.path();
        // 「开始使用」/「重新打开」按钮：整页导航到这个哨兵路径。不依赖 Tauri IPC
        // ——远程源（本地反代）下 `window.__TAURI__` 不保证注入、invoke 会静默失效，
        // 而 on_navigation 是纯 Rust、一定触发。这里由壳子开系统浏览器 + 切等待态。
        if path == "/__desktop/open-login" {
            start_device_login(app_for_nav.clone());
            return false;
        }
        if path == "/__desktop/cancel-login" {
            let app2 = app_for_nav.clone();
            cancel_device_login(&app2.state::<Shared>());
            let expected = app2.state::<Shared>().session.epoch.advance();
            tauri::async_runtime::spawn(async move {
                let shared = app2.state::<Shared>();
                if reset_login_state(&shared, expected).await {
                    navigate_session_windows(&app2, &shared.login_idle_url());
                }
            });
            return false;
        }
        // 服务设置页动作同样走导航哨兵，避免依赖远程源下不稳定的 Tauri IPC。
        if path == "/__desktop/connect-server" {
            open_server_config_in(&app_for_nav, &window_label);
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
            if let Some(window) = app_for_nav.get_webview_window(&window_label) {
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
        // 菜单动作复用 actions::dispatch；切到主线程下一拍执行，避免导航回调重入。
        if path == "/__desktop/menu" {
            let action = u
                .query_pairs()
                .find_map(|(key, value)| (key == "action").then(|| value.into_owned()))
                .unwrap_or_default();
            let app = app_for_nav.clone();
            let window_label = window_label.clone();
            let _ = app_for_nav.run_on_main_thread(move || {
                actions::dispatch_for_window(&app, &action, &window_label);
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
                // 双模式恒为云端为主：本机只是执行面，不允许把登录/会话目标
                // 整体切到本机（config::load 也会把这种遗留状态归一化回云端）。
                if config::load(&dir).provision_mode() == config::ProvisionMode::Dual {
                    eprintln!("[config] 双模式下忽略 activate-local（云端为主）");
                    return;
                }
                // 整体切到本机 = LocalOnly 形态：连 provision_mode 一起写全，
                // 重启后不会再被初始化页拦一道（云端地址仍被记住，可切回）。
                if let Err(error) = config::provision(
                    &dir,
                    config::ProvisionMode::LocalOnly,
                    &local_server::local_server_base(),
                    "",
                ) {
                    eprintln!("[config] 切换本机服务失败: {error}");
                    return;
                }
                app2.restart();
            });
            return false;
        }
        // 双模式下切到云端：复用初始化时记住的云端地址，不再弹「选服务器」页。
        if path == "/__desktop/activate-cloud" {
            let app2 = app_for_nav.clone();
            let window_label = window_label.clone();
            tauri::async_runtime::spawn(async move {
                let dir = app2.state::<Shared>().config_dir.clone();
                let base = config::load(&dir).cloud_base();
                if base.trim().is_empty() {
                    // 没有记住的地址（异常态）：退回到「设置服务器地址」页手动填。
                    open_server_config_in(&app2, &window_label);
                    return;
                }
                if let Err(error) = config::save_server_base(&dir, &base) {
                    eprintln!("[config] 切换云端服务失败: {error}");
                    return;
                }
                app2.restart();
            });
            return false;
        }
        // 仅交付混合模式的包：忽略任何查询参数，固定写入「本机 + 云端」+ 构建期
        // 烤进来的云端地址。反代此刻已按双模式准备好，所以保存后让**同一个窗口**
        // 直接切到安装进度页——不重启桌面进程，窗口不会先消失再重开。
        if brand::HYBRID_ONLY && path == "/__desktop/provision" {
            let app2 = app_for_nav.clone();
            let window_label = window_label.clone();
            tauri::async_runtime::spawn(async move {
                let dir = app2.state::<Shared>().config_dir.clone();
                if let Err(error) = config::provision(
                    &dir,
                    config::ProvisionMode::Dual,
                    &local_server::local_server_base(),
                    brand::DEFAULT_SERVER_BASE,
                ) {
                    eprintln!("[config] 保存双模式初始化配置失败: {error}");
                    return;
                }
                let port = app2.state::<Shared>().port;
                let setup_url =
                    match url::Url::parse(&format!("http://127.0.0.1:{port}/__desktop/setup")) {
                        Ok(url) => url,
                        Err(error) => {
                            eprintln!("[config] 生成初始化进度页地址失败: {error}");
                            return;
                        }
                    };
                if let Some(window) = app2.get_webview_window(&window_label) {
                    if let Err(error) = window.navigate(setup_url) {
                        eprintln!("[config] 打开初始化进度页失败: {error}");
                    }
                }
            });
            return false;
        }
        // 初始化选型：写入运行模式 + 云端地址并重启。本机 / 双模式重启后由启动
        // 流程自动安装本机服务；云端模式重启后直接连所填服务器。
        if path == "/__desktop/provision" {
            let mode = u
                .query_pairs()
                .find_map(|(key, value)| (key == "mode").then(|| value.into_owned()))
                .unwrap_or_default();
            let base = u
                .query_pairs()
                .find_map(|(key, value)| (key == "base").then(|| value.into_owned()))
                .unwrap_or_default();
            let provision = match mode.as_str() {
                "local" => Some(config::ProvisionMode::LocalOnly),
                "cloud" => Some(config::ProvisionMode::CloudOnly),
                "dual" => Some(config::ProvisionMode::Dual),
                _ => None,
            };
            let app2 = app_for_nav.clone();
            tauri::async_runtime::spawn(async move {
                let Some(provision) = provision else {
                    eprintln!("[config] 初始化收到未知运行模式: {mode}");
                    return;
                };
                let dir = app2.state::<Shared>().config_dir.clone();
                if let Err(error) =
                    config::provision(&dir, provision, &local_server::local_server_base(), &base)
                {
                    eprintln!("[config] 保存初始化选型失败: {error}");
                    return;
                }
                app2.restart();
            });
            return false;
        }
        // 在访达/资源管理器中打开一个本机路径（本地项目文件夹）。
        if path == "/__desktop/open-path" {
            if let Some(p) = u
                .query_pairs()
                .find_map(|(key, value)| (key == "path").then(|| value.into_owned()))
            {
                if !p.trim().is_empty() {
                    let _ = app_for_nav.opener().open_path(p, None::<String>);
                }
            }
            return false;
        }
        let folder_event = match path {
            "/__desktop/pick-local-folder" => Some("hugagent:local-folder"),
            "/__desktop/pick-grant-folder" => Some("hugagent:grant-folder"),
            _ => None,
        };
        if let Some(event) = folder_event {
            pick_folder(app_for_nav, &window_label, event);
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
        let expected = clear_desktop_session(&shared).await;
        if !shared.session.epoch.matches(expected) {
            return;
        }
        navigate_session_windows(&app2, &shared.login_idle_url());
    });
    false
}

#[cfg(test)]
mod tests {
    #[test]
    fn origin_requires_the_actual_listener_and_no_credentials() {
        assert!(super::is_shell_origin(
            &url::Url::parse("http://127.0.0.1:1234/path").unwrap(),
            1234
        ));
        for value in [
            "http://127.0.0.1:1235/",
            "https://127.0.0.1:1234/",
            "http://user@127.0.0.1:1234/",
            "http://localhost:1234/",
            "data:text/html,hi",
        ] {
            assert!(!super::is_shell_origin(
                &url::Url::parse(value).unwrap(),
                1234
            ));
        }
    }
}

fn pick_folder(app: &tauri::AppHandle, label: &str, event: &'static str) {
    let owner = app.clone();
    let label = label.to_string();
    app.dialog().file().pick_folder(move |picked| {
        let Some(folder) = picked else { return };
        let Ok(path) = folder.into_path() else { return };
        if let Some(window) = owner.get_webview_window(&label) {
            let path = serde_json::to_string(&path.to_string_lossy()).unwrap();
            let _ = window.eval(format!(
                "window.dispatchEvent(new CustomEvent('{event}',{{detail:{path}}}));"
            ));
        }
    });
}
