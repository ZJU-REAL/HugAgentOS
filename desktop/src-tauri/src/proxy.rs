//! 本地反向代理（方案 B 核心）。
//!
//! WebView 始终访问 `http://127.0.0.1:<随机端口>`，因此：
//!   - 前端打包产物（`/`、`/icons/...` 等静态资源）由本地反代直接提供；
//!   - 前端的 `/api/*` 相对请求命中本地反代 → 注入 `Cookie: <name>=<token>` 后
//!     原样转发到真实后端。
//!
//! 全程**同源**，前端零改动；session 鉴权对后端而言就是普通 cookie 会话，后端
//! CORS / SameSite / 会话校验链路一行不用改。响应（含 SSE 长连）逐帧透传、不缓冲。

use std::path::PathBuf;
use std::sync::Arc;

use axum::{
    extract::State,
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{any, get, post},
    Json, Router,
};
use tower_http::services::{ServeDir, ServeFile};

use crate::brand;
use crate::config::ProvisionMode;
use crate::local_server::LocalServerManager;

#[derive(Clone)]
pub struct ProxyState {
    pub session: Arc<crate::session_state::SessionState>,
    pub device_login: Arc<crate::device_login::Login>,
    pub http: reqwest::Client,
    pub insecure_tls: bool,
    /// 后端根地址（已去尾斜杠）。
    pub server_base: String,
    pub cookie_name: String,
    pub local_server: Arc<LocalServerManager>,
    pub active_local: bool,
    /// 初始化选定的运行形态（本机 / 云端 / 双模式）。前端据此决定是否展示切换。
    pub provision_mode: ProvisionMode,
    /// 初始化选择页的预填形态：全新安装（尚无 server.json）预填「本机模式」；
    /// 已有配置（显式选过或遗留 deployment_mode）沿用推断值，升级用户预填不变。
    pub init_mode_prefill: ProvisionMode,
    /// 记住的云端服务器地址，供初始化选择页预填。
    pub cloud_server_base: String,
    /// 混合架构（Dual）：本机执行面地址（端口由构建期品牌配置决定）。
    pub local_base: String,
    /// 仅 Dual 为 true：启用按请求路由（x-hugagent-target: local → 本机）。
    pub hybrid_local: bool,
    /// 标题栏「视图 → 放大 / 缩小」的动作出口，投递的是与原生菜单同一套动作 id。
    ///
    /// 缩放不能走标题栏其它动作那套导航哨兵：哨兵靠发起一次随即被 `on_navigation` 取消的
    /// 导航来传话，而 WebView2 会在导航生命周期里把 ZoomFactor 重置回 1.0，刚设上的档位
    /// 当场失效。Tauri IPC 也不可用——反代是远程源，实测自定义命令（含既有的
    /// `logout_desktop`）一律被 ACL 拒绝。
    ///
    /// 另一面是作用域：哨兵在 `on_navigation` 闭包里捕获了发起窗口的 label，天然是
    /// 「窗口作用域」动作；缩放是应用全局档位，本就不需要窗口身份，走 fetch 没有损失。
    pub zoom_tx: tokio::sync::mpsc::UnboundedSender<String>,
    /// 桥接秘密：本机路由请求注入 `X-Desktop-Bridge` 证明来自壳。
    pub bridge_secret: String,
    /// 反代实际监听的端口，`serve` 绑定后填入。同源判定要用它拼出自己的 origin：
    /// 端口每次启动随机，不能写死，也没法在建 state 时就知道。
    pub bound_port: Arc<std::sync::atomic::AtomicU16>,
}

/// 前端标记「该请求属于本地项目」的头；反代读取后剥离，不透传给任何后端。
pub const TARGET_HEADER: &str = "x-hugagent-target";
/// 桥接头（注入本机路由请求；来自 WebView 的同名头一律剥离防伪造）。
pub const BRIDGE_SECRET_HEADER: &str = "x-desktop-bridge";
pub const BRIDGE_USER_HEADER: &str = "x-desktop-bridge-user";

/// 在 127.0.0.1 随机端口起反代，返回实际端口。axum serve 在后台 task 常驻。
pub async fn serve(state: ProxyState, web_dir: PathBuf) -> std::io::Result<u16> {
    let bound_port = state.bound_port.clone();
    let index = web_dir.join("index.html");
    // SPA 首页注入平台标题栏；macOS 保留原生菜单与交通灯，只叠加轻量工具栏。
    // Windows/Linux 继续使用一体化自绘标题栏。静态资源仍直接读取原 dist。
    let raw_index = std::fs::read_to_string(&index).unwrap_or_default();
    // The SPA learns the desktop shape from the document itself: no probe, no race
    // between the first API calls and the routing switch.
    let boot = format!(
        "<script>window.__HG_DESKTOP__={};</script>",
        serde_json::json!({
            "provision_mode": state.provision_mode,
            "active_local": state.active_local,
            "server_base": state.server_base,
            "local_base": state.local_base,
        })
    );
    let injected_index = inject_after_body(
        &raw_index,
        &format!("{boot}{}", platform_titlebar_block(true)),
    );
    let injected_path =
        std::env::temp_dir().join(format!("hugagent-shell-index-{}.html", std::process::id()));
    if let Err(error) = std::fs::write(&injected_path, injected_index.as_bytes()) {
        eprintln!("[proxy] 写入桌面标题栏首页失败，回退原始 index: {error}");
    }
    let spa_index = if injected_path.is_file() {
        injected_path
    } else {
        index
    };
    // SPA：静态资源命中即返回，未命中回落注入后的 index.html。
    let serve_dir = ServeDir::new(&web_dir).fallback(ServeFile::new(&spa_index));

    let app = Router::new()
        .route("/__desktop/login", get(login_page))
        .route("/__desktop/login/status", get(login_status))
        .route("/__desktop/close-confirm", get(close_confirm_page))
        .route(
            "/__desktop/update-progress",
            get(crate::update::progress_page),
        )
        .route("/__desktop/server-config", get(server_config_page))
        .route("/__desktop/init", get(init_page))
        .route("/__desktop/setup", get(setup_page))
        .route("/__desktop/setup/status", get(setup_status))
        .route("/__desktop/setup/install", post(start_local_install))
        .route("/__desktop/events", get(desktop_events))
        .route("/__desktop/zoom/:action", post(zoom_action))
        .route(
            "/__desktop/update/status",
            get(|| async { Json(crate::update::status()) }),
        )
        .route("/api/v1/plugin-resources/:resource_id/stream", get(websocket::upgrade))
        .route("/api", any(proxy_handler))
        .route("/api/*rest", any(proxy_handler))
        // nginx-free desktop mode still needs the backend-owned public paths:
        // generated artifacts (/files) and hosted sites (/site).  Without
        // these routes, links returned by the agent fall through to the SPA
        // index instead of reaching FastAPI.
        .route("/files", any(proxy_handler))
        .route("/files/*rest", any(proxy_handler))
        .route("/site", any(proxy_handler))
        .route("/site/*rest", any(proxy_handler))
        .route("/applications-mcp", any(proxy_handler))
        .route("/applications-mcp/*rest", any(proxy_handler))
        // Page-config assets and manuals also live on the backend, not in the
        // frontend dist.  Forward them with the same streaming proxy.
        .route("/docs/*rest", any(proxy_handler))
        .route_service("/", ServeFile::new(&spa_index))
        .fallback_service(serve_dir)
        .with_state(state);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let port = listener.local_addr()?.port();
    bound_port.store(port, std::sync::atomic::Ordering::Relaxed);

    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            eprintln!("[proxy] axum serve 退出: {e}");
        }
    });

    Ok(port)
}

async fn login_status(State(state): State<ProxyState>, headers: HeaderMap) -> Response {
    if !is_same_origin(
        &headers,
        state.bound_port.load(std::sync::atomic::Ordering::Relaxed),
    ) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let view = state.device_login.view.read().await.clone();
    ([(header::CACHE_CONTROL, "no-store")], Json(view)).into_response()
}

mod deadline;
mod pages;
mod state;
/// 这次请求是不是来自本窗口自己的页面。
///
/// 反代常驻在回环口上，并且**代替调用方注入凭据**——云端路由塞会话 cookie，本机路由塞
/// 桥接秘密。也就是说，够得着这个端口的人不需要任何凭据就能以已登录用户的身份调后端。
/// 端口随机只是提高了猜的成本，不是边界。
///
/// 浏览器引擎会如实标注请求的来源，这里就用它来判定：`Origin` 必须正好是本反代自己的
/// 地址；没有 `Origin` 的请求（同源 GET、页面跳转）看 `Sec-Fetch-Site`。两者都拿不到，
/// 说明发起方根本不是这个 WebView（curl、同机的其它程序），拒绝。
mod transport;
mod websocket;
use pages::*;
use state::{desktop_events, setup_status, start_local_install};
#[cfg(test)]
use transport::is_cloud_site_path;
use transport::{is_same_origin, proxy_handler};
#[cfg(test)]
mod tests;

#[path = "window_chrome.rs"]
mod window_chrome;
use window_chrome::platform_titlebar_block;
