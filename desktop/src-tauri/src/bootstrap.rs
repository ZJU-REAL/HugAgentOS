//! Assemble configured services and the initial window; runtime event wiring lives in app.
use crate::display::sanitize_zoom;
use crate::session::{handle_deep_link, spawn_startup_probe};
use crate::tray::build_tray;
use crate::windows::build_window;
use crate::{
    actions, auth, brand, config, device_login, hybrid, local_payload, local_server, notify, prefs,
    proxy, update_monitor, Shared,
};
use std::sync::Arc;
use tauri::Manager;
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut};

pub(crate) fn initialize(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    app.manage(crate::window_events::WindowNavigation(
        crate::navigation::handle_navigation,
    ));
    let handle = app.handle().clone();

    let config_dir = app
        .path()
        .app_config_dir()
        .unwrap_or_else(|_| std::path::PathBuf::from("."));
    std::fs::create_dir_all(&config_dir).ok();

    let needs_initialization = !config::is_provisioned(&config_dir);
    let mut cfg = config::load(&config_dir);

    // NSIS 交互安装的「运行模式三选一」通过一次性 pending 文件交给应用消费；
    // 自动更新不写 pending，用户日后的菜单切换也不会被旧安装选择覆盖。
    // 仅本机不需要地址 → 直接完成初始化；云端 / 双模式还差服务器地址 →
    // 只预选形态（is_provisioned 仍为 false），首启初始化页按预选补地址。
    // 仅交付混合模式的包没有模式可选，安装器的遗留选择一律丢弃。
    if brand::HYBRID_ONLY {
        config::clear_pending_installer_mode(&config_dir).map_err(std::io::Error::other)?;
    } else if let Some(installer_mode) = config::pending_installer_mode(&config_dir) {
        match installer_mode {
            config::ProvisionMode::LocalOnly => {
                config::provision(
                    &config_dir,
                    config::ProvisionMode::LocalOnly,
                    &local_server::local_server_base(),
                    "",
                )
                .map_err(std::io::Error::other)?;
            }
            other => {
                config::preselect_provision_mode(&config_dir, other)
                    .map_err(std::io::Error::other)?;
            }
        }
        config::clear_pending_installer_mode(&config_dir).map_err(std::io::Error::other)?;
        cfg = config::load(&config_dir);
    }

    // 仅交付混合模式的包：首启页与安装进度页共用同一个窗口和反代实例。这里先只在
    // 内存里把运行形态固定成「本机 + 云端」，用户确认后无需重启就能直接进进度页；
    // 真正落盘与安装仍由「开始初始化」触发。
    if brand::HYBRID_ONLY && needs_initialization {
        config::apply_fixed_dual_mode(&mut cfg);
    }

    // 没有全局 `.timeout()`：这个 client 同时给反代转发 SSE 长连用。
    // 但连接建立必须有上限，否则一个半死的上游能把调用方永久挂住。
    let http = reqwest::Client::builder()
        .danger_accept_invalid_certs(cfg.insecure_tls)
        .no_proxy()
        .connect_timeout(std::time::Duration::from_secs(3))
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
    let local_server_root = local_data_dir.join("local-server");
    let home_dir = app.path().home_dir().ok();
    let local_server_data_dir =
        local_server::resolve_local_server_data_dir(&local_server_root, home_dir.as_deref());
    let local_server = local_server::LocalServerManager::new(
        local_server_root,
        local_server_data_dir,
        resource_dir.join("server-ce.zip"),
        resource_dir.join("server-ce-manifest.json"),
        resource_dir.join("runtime-core.tar.gz"),
        resource_dir.join("runtime-manifest.json"),
        http.clone(),
    );

    // 混合架构（Dual）：桥接秘密注入本机服务进程；本机作为「本地项目执行面」
    // 在云端为主的同时常驻。云端/本机单一形态不受影响。
    let hybrid_local = cfg.provision_mode() == config::ProvisionMode::Dual;
    let bridge_secret = hybrid::load_or_create_bridge_secret(&config_dir);
    if hybrid_local {
        local_server.set_bridge_secret(bridge_secret.clone());
    }

    // 本机模式下，安装包版本变化会自动升级服务资源；已安装且同版本则直接
    // 拉起服务。安装/启动都在后台跑；启动例程自己负责回收上次遗留的进程，
    // 这里不再探测端口。还没初始化完（首启停在初始化页）时不预装：装机要等
    // 用户按下「开始初始化」。
    let local_needed = cfg.uses_local_server() || hybrid_local;
    if !needs_initialization && local_needed {
        if local_server.needs_install() {
            local_server.install_in_background();
        } else {
            local_server.start_in_background();
        }
    }

    // 已存 token 只从磁盘读；它是否仍有效在窗口出现之后核实（见
    // spawn_startup_probe），不让一次慢网络把建窗拖住。
    let token0 = auth::load_token(&config_dir, cfg.server_base_trimmed());
    let session = Arc::new(crate::session_state::SessionState::new(token0.clone()));
    let device_id = hybrid::load_or_create_device_id(&config_dir).map_err(std::io::Error::other)?;

    // 启动即持有有效云端会话（Dual）：立刻建立桥接身份 + 下发模型配置。
    if hybrid_local {
        if token0.is_some() {
            hybrid::on_cloud_login(
                hybrid::BridgeContext {
                    http: http.clone(),
                    cloud_base: cfg.server_base_trimmed().into(),
                    cookie_name: cfg.cookie_name.clone(),
                    session: session.clone(),
                    bridge_secret: bridge_secret.clone(),
                    local_server: local_server.clone(),
                    device_id: device_id.clone(),
                },
                session.epoch.current(),
            );
        }
    }

    // 初始化页预填形态：全新安装（尚无 server.json）默认「本机模式」——桌面
    // 包自带本机 server payload，本机开箱即用。瘦客户端（thin bundle，无本机
    // 能力）与不支持本机服务的平台仍预填云端；已有配置沿用推断值（升级用户
    // 预填不变）。
    let init_mode_prefill = if !config_dir.join("server.json").is_file()
        && local_payload::current_target() != "unsupported"
    {
        config::ProvisionMode::LocalOnly
    } else {
        cfg.provision_mode()
    };

    let device_login = Arc::new(device_login::Login::default());
    let web_dir = resolve_web_dir(app);

    // 标题栏缩放动作：webview fetch → 反代 → 这里的 channel → 主线程施加。
    let (zoom_tx, mut zoom_rx) = tokio::sync::mpsc::unbounded_channel::<String>();

    // 同步起反代拿到端口（仅绑定 + 后台 spawn，很快返回）。
    let pstate = proxy::ProxyState {
        device_login: device_login.clone(),
        http: http.clone(),
        server_base: cfg.server_base_trimmed().to_string(),
        cookie_name: cfg.cookie_name.clone(),
        session: session.clone(),
        local_server: local_server.clone(),
        active_local: cfg.uses_local_server(),
        provision_mode: cfg.provision_mode(),
        init_mode_prefill,
        cloud_server_base: cfg.cloud_base(),
        local_base: local_server::local_server_base(),
        hybrid_local,
        zoom_tx,
        bridge_secret: bridge_secret.clone(),
        bound_port: std::sync::Arc::new(std::sync::atomic::AtomicU16::new(0)),
    };
    let port =
        tauri::async_runtime::block_on(proxy::serve(pstate, web_dir)).expect("启动本地反代失败");

    // 复用菜单的动作分发：动作 id → 档位变化只在 menu.rs 定义一处。
    let zoom_app = app.handle().clone();
    tauri::async_runtime::spawn(async move {
        while let Some(action) = zoom_rx.recv().await {
            let app = zoom_app.clone();
            let _ = zoom_app.run_on_main_thread(move || actions::dispatch(&app, &action));
        }
    });

    // Grant remote IPC only to the proxy listener created by this process.
    let mut capability: serde_json::Value =
        serde_json::from_str(include_str!("../capabilities/default.json"))?;
    capability["identifier"] = serde_json::json!("session-proxy");
    capability["windows"] = serde_json::json!(["main", "main-*", "quickask"]);
    capability["remote"] = serde_json::json!({"urls": [format!("http://127.0.0.1:{port}")]});
    app.add_capability(serde_json::to_string(&capability)?)?;
    app.manage(Shared {
        device_login,
        server_base: cfg.server_base.clone(),
        update_base: cfg.update_base(),
        session: session.clone(),
        http: http.clone(),
        port,
        ui_zoom: std::sync::atomic::AtomicU64::new(
            sanitize_zoom(prefs::load_ui_zoom(&config_dir)).to_bits(),
        ),
        config_dir: config_dir.clone(),
        local_server: local_server.clone(),
        cookie_name: cfg.cookie_name.clone(),
        hybrid_local,
        bridge_secret,
        device_id,
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

    // 初始窗口按本地事实立刻决定落点：有 token 进首页，没有则进登录卡片「初始态」
    // （不自动开浏览器，等用户点「开始使用」再拉起）。云端可达性与会话有效性在
    // 窗口出现之后核实，结论到了再修正导航。仅本机形态的后端就是本机服务，进程
    // 启动时它一定还没起来，所以直接进进度页，就绪后自动前进；双模式云端为主，
    // 本机执行面已安装时不再让用户等它启动，只有还要安装时才停在进度页。
    let start = if needs_initialization {
        // 首启：默认包让用户选运行模式（本机 / 云端 / 双模式），云端形态在此填地址；
        // 仅交付混合模式的包只展示一个确认动作（见 proxy.rs 的 init_page）。
        format!("http://127.0.0.1:{}/__desktop/init", port)
    } else if cfg.uses_local_server() || (local_needed && local_server.needs_install()) {
        format!("http://127.0.0.1:{}/__desktop/setup", port)
    } else if token0.is_some() {
        format!("http://127.0.0.1:{}/", port)
    } else {
        format!("http://127.0.0.1:{}/__desktop/login", port)
    };
    build_window(&handle, "main", &start)?;
    if !needs_initialization && !cfg.uses_local_server() {
        spawn_startup_probe(
            handle.clone(),
            cfg.server_base_trimmed().to_string(),
            cfg.cookie_name.clone(),
        );
    }

    // 系统托盘：关闭窗口时「最小化到托盘」后，从这里恢复主窗口。
    build_tray(app)?;
    update_monitor::start(handle.clone(), cfg.update_base(), http.clone());

    // A1：后台通知轮询——自动化/后台任务跑完发原生系统通知。
    notify::start(
        handle.clone(),
        port,
        session.clone(),
        http.clone(),
        hybrid_local,
    );

    // A2：注册全局快捷键 Ctrl/Cmd+Shift+Space（唤起悬浮快速问答窗）。
    let qa = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::Space);
    if let Err(e) = app.global_shortcut().register(qa) {
        eprintln!("[shortcut] 注册全局快捷键失败: {e}");
    }

    Ok(())
}

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
