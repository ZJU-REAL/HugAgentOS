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
    body::Body,
    extract::State,
    http::{HeaderMap, Method, Request, StatusCode, Uri},
    response::{Html, IntoResponse, Response},
    routing::{any, get, post},
    Json, Router,
};
use tokio::sync::RwLock;
use tower_http::services::{ServeDir, ServeFile};

use crate::brand;
use crate::config::ProvisionMode;
use crate::local_server::{LocalServerManager, LocalServerStatus};

#[derive(Clone)]
pub struct ProxyState {
    pub http: reqwest::Client,
    /// 后端根地址（已去尾斜杠）。
    pub server_base: String,
    pub cookie_name: String,
    /// 当前 session token（None = 未登录；反代不注入 cookie）。
    pub token: Arc<RwLock<Option<String>>>,
    pub local_server: Arc<LocalServerManager>,
    pub active_local: bool,
    /// 初始化选定的运行形态（本机 / 云端 / 双模式）。前端据此决定是否展示切换。
    pub provision_mode: ProvisionMode,
    /// 记住的云端服务器地址，供初始化选择页预填。
    pub cloud_server_base: String,
    /// 混合架构（Dual）：本机执行面地址（http://127.0.0.1:32101）。
    pub local_base: String,
    /// 仅 Dual 为 true：启用按请求路由（x-hugagent-target: local → 本机）。
    pub hybrid_local: bool,
    /// 桥接秘密：本机路由请求注入 `X-Desktop-Bridge` 证明来自壳。
    pub bridge_secret: String,
    /// base64 编码的云端用户信息（登录后由 hybrid::on_cloud_login 填充）。
    pub bridge_user: Arc<RwLock<Option<String>>>,
}

/// 前端标记「该请求属于本地项目」的头；反代读取后剥离，不透传给任何后端。
pub const TARGET_HEADER: &str = "x-hugagent-target";
/// 桥接头（注入本机路由请求；来自 WebView 的同名头一律剥离防伪造）。
pub const BRIDGE_SECRET_HEADER: &str = "x-desktop-bridge";
pub const BRIDGE_USER_HEADER: &str = "x-desktop-bridge-user";

/// 在 127.0.0.1 随机端口起反代，返回实际端口。axum serve 在后台 task 常驻。
pub async fn serve(state: ProxyState, web_dir: PathBuf) -> std::io::Result<u16> {
    let index = web_dir.join("index.html");
    // SPA 首页注入平台标题栏；macOS 保留原生菜单与交通灯，只叠加轻量工具栏。
    // Windows/Linux 继续使用一体化自绘标题栏。静态资源仍直接读取原 dist。
    let raw_index = std::fs::read_to_string(&index).unwrap_or_default();
    let injected_index = inject_after_body(&raw_index, &platform_titlebar_block(true));
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
        .route("/__desktop/close-confirm", get(close_confirm_page))
        .route("/__desktop/server-config", get(server_config_page))
        .route("/__desktop/init", get(init_page))
        .route("/__desktop/setup", get(setup_page))
        .route("/__desktop/setup/status", get(setup_status))
        .route("/__desktop/setup/install", post(start_local_install))
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
        // Page-config assets and manuals also live on the backend, not in the
        // frontend dist.  Forward them with the same streaming proxy.
        .route("/docs/*rest", any(proxy_handler))
        .route_service("/", ServeFile::new(&spa_index))
        .fallback_service(serve_dir)
        .with_state(state);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let port = listener.local_addr()?.port();

    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            eprintln!("[proxy] axum serve 退出: {e}");
        }
    });

    Ok(port)
}

/// 反代处理器：把 `/api/*` 透传到后端，注入 session cookie，流式回传。
async fn proxy_handler(State(state): State<ProxyState>, req: Request<Body>) -> Response {
    let (parts, body) = req.into_parts();
    let method: Method = parts.method;
    let uri: Uri = parts.uri;
    let headers: HeaderMap = parts.headers;

    let path_q = uri.path_and_query().map(|p| p.as_str()).unwrap_or("/");

    // 混合架构（Dual）：前端给「本地项目」的请求打 x-hugagent-target: local，
    // 反代把它们转到本机执行面（127.0.0.1:32101），其余一律云端。单一形态不路由。
    // <img>/<iframe> 等 src 场景无法带请求头，等价支持 query 参数 ?hg_target=local。
    let to_local = state.hybrid_local
        && (headers
            .get(TARGET_HEADER)
            .and_then(|v| v.to_str().ok())
            .map(|v| v.eq_ignore_ascii_case("local"))
            .unwrap_or(false)
            || uri
                .query()
                .map(|q| q.split('&').any(|kv| kv == "hg_target=local"))
                .unwrap_or(false));
    let base = if to_local { &state.local_base } else { &state.server_base };
    let target = format!("{}{}", base, path_q);

    // 收齐请求体（上传等）。下游用 reqwest 重发。
    let body_bytes = match axum::body::to_bytes(body, usize::MAX).await {
        Ok(b) => b,
        Err(e) => return (StatusCode::BAD_REQUEST, format!("读取请求体失败: {e}")).into_response(),
    };

    let bridge_user = state.bridge_user.read().await.clone();
    let token = state.token.read().await.clone();

    let build_request = |use_local: bool| {
        let base = if use_local { &state.local_base } else { &state.server_base };
        let mut rb = state.http.request(method.clone(), format!("{}{}", base, path_q));

        // 透传请求头，但剔除 hop-by-hop / 由我们重写的头。
        // http 的 HeaderName 已规范化为小写，直接 match 即可，无需再 to_ascii_lowercase。
        for (name, value) in headers.iter() {
            match name.as_str() {
                // host 让 reqwest 按目标地址重置；cookie 我们重新注入；
                // accept-encoding 去掉以拿 identity（避免转发压缩流时还要解码）；
                // content-length / connection 交给 reqwest / axum 自管。
                "host" | "cookie" | "accept-encoding" | "content-length" | "connection" => continue,
                // 路由标记不透传；桥接头只能由壳注入——WebView 带来的一律剥离（防伪造）。
                TARGET_HEADER | BRIDGE_SECRET_HEADER | BRIDGE_USER_HEADER => continue,
                _ => {
                    rb = rb.header(name, value);
                }
            }
        }

        if use_local {
            // 本机路由：注入桥接秘密 + 云端身份（身份桥，见 hybrid.rs / desktop_bridge.py）。
            // 不注入云端会话 cookie——本机后端不认它，身份完全由桥接头承载。
            rb = rb.header(BRIDGE_SECRET_HEADER, &state.bridge_secret);
            if let Some(user) = bridge_user.clone() {
                rb = rb.header(BRIDGE_USER_HEADER, user);
            }
        } else if let Some(tok) = token.clone() {
            // 云端路由：注入会话 cookie（已登录时）——这是整套桌面鉴权的关键一笔。
            rb = rb.header(
                reqwest::header::COOKIE,
                format!("{}={}", state.cookie_name, tok),
            );
        }

        if !body_bytes.is_empty() {
            // body_bytes 是 Bytes，clone 仅增引用计数，不复制请求体。
            rb = rb.body(body_bytes.clone());
        }
        rb
    };

    let sent = match build_request(to_local).send().await {
        Ok(upstream) => {
            // 站点访问兜底：本机发布的站点只存在于本机库，而站点入口/子资源/表单
            // （<a>/<img>/fetch 相对路径）都带不上路由标记——云端 404 时按同请求
            // 重试本机，本机命中则用本机响应。云端命中/本机也 404 时行为不变。
            let is_site = uri.path() == "/site" || uri.path().starts_with("/site/");
            if state.hybrid_local && !to_local && is_site && upstream.status().as_u16() == 404 {
                match build_request(true).send().await {
                    Ok(local_resp) if local_resp.status().as_u16() != 404 => Ok(local_resp),
                    _ => Ok(upstream),
                }
            } else {
                Ok(upstream)
            }
        }
        Err(e) => Err(e),
    };

    match sent {
        Ok(upstream) => {
            let status = upstream.status();
            let mut builder = Response::builder().status(status);

            for (name, value) in upstream.headers().iter() {
                // 这些头与「逐帧流式 + 已解压」语义冲突，去掉让 axum 自管分块。
                match name.as_str() {
                    "connection" | "transfer-encoding" | "content-encoding" | "content-length" => {
                        continue
                    }
                    _ => {
                        builder = builder.header(name, value);
                    }
                }
            }

            // bytes_stream 逐帧产出，SSE 不被缓冲。
            let stream = upstream.bytes_stream();
            match builder.body(Body::from_stream(stream)) {
                Ok(resp) => resp,
                Err(e) => (StatusCode::BAD_GATEWAY, format!("构造响应失败: {e}")).into_response(),
            }
        }
        Err(e) => (StatusCode::BAD_GATEWAY, format!("代理上游失败: {e}")).into_response(),
    }
}

/// 未登录时窗口加载的登录卡片页。默认「初始态」——展示「开始使用」按钮，等用户点击
/// 才经 Tauri 命令 open_login 拉起系统浏览器；带 `?waiting=1` 时（会话过期兜底）直接进
/// 等待态。启动与退出登录都落到这张卡片，避免直接跳外链或白屏。
async fn login_page() -> Html<String> {
    // 品牌名 / logo 走编译期可配（brand.rs）——默认，构建时环境变量可覆盖。
    let html = LOGIN_HTML
        .replace("HugAgentOS", brand::NAME)
        .replace("/icon.png", brand::LOGIN_LOGO_URL);
    Html(inject_after_body(&html, &platform_titlebar_block(false)))
}

/// 关闭主窗口时的自定义确认页（带「记住我的选择」勾选框）。按钮整页导航到
/// `/__desktop/close-decide?action=..&remember=..`，由确认窗的 Rust 导航守卫执行。
async fn close_confirm_page() -> Html<String> {
    Html(CLOSE_CONFIRM_HTML.replace("HugAgentOS", brand::NAME))
}

/// 「设置服务器地址」页（菜单栏「文件 → 设置服务器地址…」打开）。输入框预填当前后端地址，
/// 保存按钮整页导航到哨兵 `/__desktop/save-server?base=<encoded>`，由主窗口的 Rust 导航守卫
/// 写回 server.json 并重启。同样不走 Tauri IPC。
async fn server_config_page(State(state): State<ProxyState>) -> Html<String> {
    let html = SERVER_CONFIG_HTML
        .replace("__CURRENT_BASE__", &html_escape(&state.server_base))
        .replace("HugAgentOS", brand::NAME);
    Html(inject_after_body(&html, &platform_titlebar_block(false)))
}

/// 后端不可达或用户在安装器选择本机服务时展示的一体化部署页。
async fn setup_page(State(state): State<ProxyState>) -> Html<String> {
    let html = SETUP_HTML
        .replace("__CURRENT_BASE__", &html_escape(&state.server_base))
        .replace(
            "__ACTIVE_LOCAL__",
            if state.active_local { "true" } else { "false" },
        )
        .replace(
            "__HYBRID_DUAL__",
            if state.provision_mode == ProvisionMode::Dual {
                "true"
            } else {
                "false"
            },
        )
        .replace(
            "__LOCAL_SUPPORTED__",
            if crate::local_payload::current_target() != "unsupported" {
                "true"
            } else {
                "false"
            },
        )
        .replace(
            "__PLATFORM__",
            if cfg!(target_os = "macos") {
                "macos"
            } else if cfg!(target_os = "windows") {
                "windows"
            } else {
                "linux"
            },
        )
        .replace("HugAgentOS", brand::NAME);
    Html(inject_after_body(&html, &platform_titlebar_block(false)))
}

/// 初始化「运行模式选择」页（首启时展示）：下拉选本机 / 云端 / 双模式；选到含云端的
/// 形态时展开服务器地址输入。提交整页导航到哨兵 `/__desktop/provision?mode=..&base=..`，
/// 由主窗口的 Rust 导航守卫落盘并重启。`manage=1` 时是「稍后更改运行模式」入口。
async fn init_page(State(state): State<ProxyState>) -> Html<String> {
    let current_mode = match state.provision_mode {
        ProvisionMode::LocalOnly => "local",
        ProvisionMode::CloudOnly => "cloud",
        ProvisionMode::Dual => "dual",
    };
    // 云端地址预填：优先记住的云端地址，其次当前后端地址（本机模式下为本地地址，
    // 那种情况留空更合理——只有非本地地址才预填）。
    let cloud_prefill = if !state.cloud_server_base.trim().is_empty() {
        state.cloud_server_base.clone()
    } else if !state.server_base.contains("127.0.0.1") {
        state.server_base.clone()
    } else {
        String::new()
    };
    let html = INIT_HTML
        .replace("__CURRENT_MODE__", current_mode)
        .replace("__CLOUD_BASE__", &html_escape(cloud_prefill.trim()))
        .replace(
            "__LOCAL_SUPPORTED__",
            if crate::local_payload::current_target() != "unsupported" {
                "true"
            } else {
                "false"
            },
        )
        .replace(
            "__PLATFORM__",
            if cfg!(target_os = "macos") {
                "macos"
            } else if cfg!(target_os = "windows") {
                "windows"
            } else {
                "linux"
            },
        )
        .replace("HugAgentOS", brand::NAME);
    Html(inject_after_body(&html, &platform_titlebar_block(false)))
}

#[derive(serde::Serialize)]
struct SetupStatus {
    #[serde(flatten)]
    service: LocalServerStatus,
    active_local: bool,
    current_server_base: String,
    /// 本机后端基址（固定 127.0.0.1:32101）。前端为「本机站点」生成对外链接时用，
    /// 避免把随启动变化的反代随机端口写进可分享的 URL。
    local_server_base: String,
    provision_mode: ProvisionMode,
}

async fn setup_status(State(state): State<ProxyState>) -> Json<SetupStatus> {
    Json(SetupStatus {
        service: state.local_server.snapshot().await,
        active_local: state.active_local,
        current_server_base: state.server_base.clone(),
        local_server_base: state.local_base.clone(),
        provision_mode: state.provision_mode.clone(),
    })
}

async fn start_local_install(State(state): State<ProxyState>) -> Json<SetupStatus> {
    state.local_server.prepare_in_background();
    setup_status(State(state)).await
}

/// 极简 HTML 属性/文本转义，防止后端地址里的引号破坏 value。
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

// ── 一体化桌面标题栏 ───────────────────────────────────────────────────────
//
// 主窗口关闭系统 decorations，避免「系统标题栏 + 原生菜单栏」占两行。这里把产品图标、
// 文件/编辑/视图/帮助和窗口控制放进同一行。全部动作走导航哨兵，由 lib.rs 拦截执行，
// 不依赖远程源下不稳定的 Tauri IPC。

const TITLEBAR_HEIGHT: u8 = 36;
const TB_OFFSET_SPA: &str =
    ":root{--hugagent-desktop-titlebar-height:36px}body{box-sizing:border-box!important;padding-top:36px!important}.jx-appLoading{height:100%!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";
const TB_OFFSET_PAGE: &str =
    ":root{--hugagent-desktop-titlebar-height:36px}body{box-sizing:border-box!important;padding-top:36px!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";

// The traffic lights start at y=13 and occupy about 14px. A 28px overlay keeps
// their hit area clear without stacking a second, visibly empty toolbar above
// the application's own brand row.
const MAC_TITLEBAR_HEIGHT: u8 = 28;
// The left half of the macOS safe area reuses the sidebar's first translucent
// blue gradient stop over its #F5F6F7 base.  The traffic lights therefore sit
// on a true visual continuation of the sidebar instead of a detached grey bar.
const MAC_OFFSET_SPA: &str =
    ":root{--hugagent-desktop-titlebar-height:28px;--hugagent-desktop-sidebar-width:0px}body{box-sizing:border-box!important;padding-top:28px!important;background:linear-gradient(90deg,rgba(203,223,255,.38) 0 var(--hugagent-desktop-sidebar-width),#FFFFFF var(--hugagent-desktop-sidebar-width) 100%),#F5F6F7!important}.jx-brandRow,.jx-miniRail{padding-top:0!important}.jx-appLoading{height:100%!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";
const MAC_OFFSET_PAGE: &str =
    ":root{--hugagent-desktop-titlebar-height:28px}body{box-sizing:border-box!important;padding-top:28px!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";

const TB_CSS: &str = r##"
#hugagent-titlebar{position:fixed;inset:0 0 auto 0;height:36px;z-index:2147483647;display:flex;align-items:center;background:#F7F8FA;border-bottom:1px solid #E5E9EF;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#30343B}
#hugagent-titlebar *{box-sizing:border-box}
#hugagent-titlebar .tb-left{display:flex;align-items:center;height:100%;min-width:0;padding-left:8px}
#hugagent-titlebar .tb-logo{width:17px;height:17px;border-radius:4px;margin-right:5px;object-fit:cover}
#hugagent-titlebar .tb-spacer{flex:1;height:100%;min-width:30px}
#hugagent-titlebar .tb-menu{display:flex;align-items:stretch;height:100%}
#hugagent-titlebar .tb-menuGroup{position:relative;height:100%;display:flex;align-items:stretch}
#hugagent-titlebar .tb-menuLabel{height:100%;padding:0 10px;border:0;background:transparent;color:#41464C;font:12.5px/1 inherit;cursor:default;-webkit-user-select:none;user-select:none}
#hugagent-titlebar .tb-menuLabel:hover,#hugagent-titlebar .tb-menuGroup.open>.tb-menuLabel{background:#E7EBF1}
#hugagent-titlebar .tb-drop{display:none;position:absolute;top:35px;left:0;min-width:180px;padding:5px;background:#fff;border:1px solid #DEE3EA;border-radius:8px;box-shadow:0 10px 28px rgba(15,23,42,.16)}
#hugagent-titlebar .tb-menuGroup.open>.tb-drop{display:block}
#hugagent-titlebar .tb-item{display:flex;align-items:center;width:100%;min-height:30px;padding:6px 11px;border:0;border-radius:6px;background:transparent;color:#30343B;font:13px/1.3 inherit;text-align:left;white-space:nowrap;cursor:default}
#hugagent-titlebar .tb-item:hover{background:#EEF4FF;color:#126DFF}
#hugagent-titlebar .tb-sep{height:1px;margin:5px 6px;background:#EDF0F4}
#hugagent-titlebar .tb-controls{display:flex;align-items:stretch;height:100%;margin-left:4px}
#hugagent-titlebar .tb-windowButton{width:46px;height:100%;padding:0;border:0;background:transparent;color:#41464C;display:flex;align-items:center;justify-content:center;cursor:default}
#hugagent-titlebar .tb-windowButton:hover{background:#E3E7ED}
#hugagent-titlebar .tb-windowButton.close:hover{background:#E81123;color:#fff}
"##;

const TB_MENU: &str = r##"<nav class="tb-menu" aria-label="应用菜单">
<div class="tb-menuGroup"><button class="tb-menuLabel" type="button">文件</button><div class="tb-drop">
  <button class="tb-item" type="button" data-act="new_chat">新建对话</button>
  <button class="tb-item" type="button" data-act="run_mode">运行模式…</button>
  <button class="tb-item" type="button" data-act="server_config">设置服务器地址…</button>
  <button class="tb-item" type="button" data-act="local_server">本机服务…</button>
  <div class="tb-sep"></div><button class="tb-item" type="button" data-win="quit">退出</button>
</div></div>
<div class="tb-menuGroup"><button class="tb-menuLabel" type="button">编辑</button><div class="tb-drop">
  <button class="tb-item" type="button" data-edit="undo">撤销</button>
  <button class="tb-item" type="button" data-edit="redo">重做</button>
  <div class="tb-sep"></div>
  <button class="tb-item" type="button" data-edit="cut">剪切</button>
  <button class="tb-item" type="button" data-edit="copy">复制</button>
  <button class="tb-item" type="button" data-edit="paste">粘贴</button>
  <button class="tb-item" type="button" data-edit="selectAll">全选</button>
</div></div>
<div class="tb-menuGroup"><button class="tb-menuLabel" type="button">视图</button><div class="tb-drop">
  <button class="tb-item" type="button" data-act="reload">重新加载</button>
  <button class="tb-item" type="button" data-win="fullscreen">全屏</button>
</div></div>
<div class="tb-menuGroup"><button class="tb-menuLabel" type="button">帮助</button><div class="tb-drop">
  <button class="tb-item" type="button" data-act="check_update">检查更新…</button>
  <button class="tb-item" type="button" data-act="website">访问官网</button>
  <div class="tb-sep"></div><button class="tb-item" type="button" data-act="about">关于</button>
</div></div>
</nav>"##;

const TB_CONTROLS: &str = r##"<div class="tb-controls">
<button class="tb-windowButton" type="button" data-win="minimize" aria-label="最小化" title="最小化"><svg width="11" height="11" viewBox="0 0 12 12"><path d="M2.5 6.5h7" fill="none" stroke="currentColor" stroke-width="1.1"/></svg></button>
<button class="tb-windowButton" type="button" data-win="toggle-maximize" aria-label="最大化或还原" title="最大化 / 还原"><svg width="10" height="10" viewBox="0 0 12 12"><rect x="2.5" y="2.5" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.1"/></svg></button>
<button class="tb-windowButton close" type="button" data-win="close" aria-label="关闭" title="关闭"><svg width="11" height="11" viewBox="0 0 12 12"><path d="m3 3 6 6m0-6L3 9" fill="none" stroke="currentColor" stroke-width="1.2"/></svg></button>
</div>"##;

const TB_JS: &str = r##"(function(){
var bar=document.getElementById('hugagent-titlebar');if(!bar)return;
// 快速问答使用独立原生小窗，不展示主窗口标题栏。
if(new URLSearchParams(location.search).get('quickask')==='1'){
  bar.remove();var style=document.getElementById('hugagent-titlebar-style');if(style)style.remove();return;
}
var lastFocus=null;
document.addEventListener('focusin',function(event){if(!bar.contains(event.target))lastFocus=event.target;},true);
function closeMenus(){bar.querySelectorAll('.tb-menuGroup').forEach(function(group){group.classList.remove('open');});}
function sentinel(path){window.location.href=path;}
bar.querySelectorAll('.tb-menuLabel').forEach(function(label){
  label.addEventListener('mousedown',function(event){event.preventDefault();});
  label.addEventListener('click',function(event){event.stopPropagation();var group=label.parentElement;var open=group.classList.contains('open');closeMenus();if(!open)group.classList.add('open');});
});
bar.querySelectorAll('.tb-item,.tb-windowButton').forEach(function(item){item.addEventListener('mousedown',function(event){event.preventDefault();});});
bar.querySelectorAll('[data-win]').forEach(function(item){item.addEventListener('click',function(event){event.stopPropagation();sentinel('/__desktop/win?action='+encodeURIComponent(item.dataset.win));});});
bar.querySelectorAll('[data-act]').forEach(function(item){item.addEventListener('click',function(event){event.stopPropagation();sentinel('/__desktop/menu?action='+encodeURIComponent(item.dataset.act));});});
function pasteText(text){
  if(lastFocus&&typeof lastFocus.focus==='function')lastFocus.focus();
  if(lastFocus&&(lastFocus.tagName==='INPUT'||lastFocus.tagName==='TEXTAREA')&&typeof lastFocus.setRangeText==='function'){
    var start=lastFocus.selectionStart==null?lastFocus.value.length:lastFocus.selectionStart;
    var end=lastFocus.selectionEnd==null?start:lastFocus.selectionEnd;
    lastFocus.setRangeText(text,start,end,'end');lastFocus.dispatchEvent(new Event('input',{bubbles:true}));return;
  }
  document.execCommand('insertText',false,text);
}
bar.querySelectorAll('[data-edit]').forEach(function(item){item.addEventListener('click',function(event){
  event.stopPropagation();closeMenus();if(lastFocus&&typeof lastFocus.focus==='function')lastFocus.focus();
  var action=item.dataset.edit;
  if(action==='paste'&&navigator.clipboard&&navigator.clipboard.readText){navigator.clipboard.readText().then(pasteText).catch(function(){document.execCommand('paste');});return;}
  document.execCommand(action,false,null);
});});
document.addEventListener('click',closeMenus);
function isControl(target){return target instanceof Element&&!!target.closest('.tb-menu,.tb-controls,button');}
bar.addEventListener('mousedown',function(event){if(event.button!==0||isControl(event.target))return;sentinel('/__desktop/win?action=drag');});
bar.addEventListener('dblclick',function(event){if(isControl(event.target))return;sentinel('/__desktop/win?action=toggle-maximize');});
})();"##;

// macOS keeps application actions in the native system menu. Inside the window
// we only reserve a compact draggable title region for the traffic lights; a
// second branded toolbar would duplicate the native chrome and waste space.
const MAC_TB_CSS: &str = r##"
#hugagent-mac-titlebar{position:fixed;inset:0 0 auto 0;height:28px;z-index:2147483647;background:transparent;border:0;box-shadow:none;-webkit-user-select:none;user-select:none}
#hugagent-mac-titlebar *{box-sizing:border-box}
"##;

const MAC_TB_JS: &str = r##"(function(){
var bar=document.getElementById('hugagent-mac-titlebar');if(!bar)return;
if(new URLSearchParams(location.search).get('quickask')==='1'){
  bar.remove();var style=document.getElementById('hugagent-titlebar-style');if(style)style.remove();return;
}
var observedSidebar=null;
var sidebarResizeObserver=typeof ResizeObserver==='function'?new ResizeObserver(syncSidebarWidth):null;
function syncSidebarWidth(){
  var sidebar=document.querySelector('.jx-sider,.jx-appLoading-sidebar');
  if(sidebarResizeObserver&&sidebar&&sidebar!==observedSidebar){
    if(observedSidebar)sidebarResizeObserver.unobserve(observedSidebar);
    observedSidebar=sidebar;sidebarResizeObserver.observe(sidebar);
  }
  var width=sidebar?Math.max(0,Math.round(sidebar.getBoundingClientRect().width)):0;
  document.documentElement.style.setProperty('--hugagent-desktop-sidebar-width',width+'px');
}
syncSidebarWidth();
new MutationObserver(syncSidebarWidth).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['class','style']});
window.addEventListener('resize',syncSidebarWidth);
bar.addEventListener('mousedown',function(event){
  if(event.button!==0)return;
  window.location.href='/__desktop/win?action=drag';
});
bar.addEventListener('dblclick',function(event){
  window.location.href='/__desktop/win?action=toggle-maximize';
});
})();"##;

fn titlebar_block(offset_css: &str) -> String {
    format!(
        "<style id=\"hugagent-titlebar-style\">{css}{offset}</style>\
<header id=\"hugagent-titlebar\" data-height=\"{height}\">\
<div class=\"tb-left\"><img class=\"tb-logo\" src=\"{logo}\" alt=\"\" onerror=\"this.style.display='none'\"/>{menu}</div>\
<div class=\"tb-spacer\"></div>{controls}</header><script>{script}</script>",
        css = TB_CSS,
        offset = offset_css,
        height = TITLEBAR_HEIGHT,
        logo = brand::LOGIN_LOGO_URL,
        menu = TB_MENU,
        controls = TB_CONTROLS,
        script = TB_JS,
    )
}

fn mac_titlebar_block(offset_css: &str) -> String {
    format!(
        "<style id=\"hugagent-titlebar-style\">{css}{offset}</style>\
<header id=\"hugagent-mac-titlebar\" data-height=\"{height}\" aria-hidden=\"true\"></header><script>{script}</script>",
        css = MAC_TB_CSS,
        offset = offset_css,
        height = MAC_TITLEBAR_HEIGHT,
        script = MAC_TB_JS,
    )
}

fn platform_titlebar_block(spa: bool) -> String {
    if cfg!(target_os = "macos") {
        mac_titlebar_block(if spa { MAC_OFFSET_SPA } else { MAC_OFFSET_PAGE })
    } else {
        titlebar_block(if spa { TB_OFFSET_SPA } else { TB_OFFSET_PAGE })
    }
}

fn inject_after_body(html: &str, block: &str) -> String {
    match html.find("<body").and_then(|position| {
        html[position..]
            .find('>')
            .map(|closing| position + closing + 1)
    }) {
        Some(index) => {
            let mut output = String::with_capacity(html.len() + block.len());
            output.push_str(&html[..index]);
            output.push_str(block);
            output.push_str(&html[index..]);
            output
        }
        None => format!("{block}{html}"),
    }
}

const LOGIN_HTML: &str = r##"<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>登录 · HugAgentOS</title>
<style>
  :root{
    color-scheme:light;--primary:#0A66FF;--primary-hover:#005BE6;--primary-active:#0052CC;
    --text:#1D1D1F;--text-2:#6E6E73;--text-3:#8E8E93;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Segoe UI",sans-serif;
    color:var(--text);
    background:linear-gradient(180deg,#FBFBFA 0%,#F4F4F2 100%);
    display:flex; align-items:center; justify-content:center;
    -webkit-user-select:none; user-select:none;
  }
  .card{
    width:min(380px,calc(100% - 40px));padding:32px 20px;text-align:center;margin-top:-18px;
  }
  .logo{
    width:64px;height:64px;border-radius:16px;margin:0 auto 20px;display:block;
    box-shadow:0 8px 24px rgba(0,0,0,.1);
  }
  h1{font-size:28px;line-height:1.2;font-weight:650;margin:0;letter-spacing:-.035em}
  .sub{font-size:14px;color:var(--text-2);margin:12px 0 28px;line-height:1.65}
  .btn{
    width:100%;height:46px;margin-top:28px;border:none;border-radius:11px;cursor:pointer;
    background:var(--primary);color:#fff;font-size:14px;font-weight:600;
    transition:background .14s ease,transform .08s ease;box-shadow:0 1px 2px rgba(0,0,0,.08);
  }
  .btn:hover{background:var(--primary-hover)}
  .btn:active{background:var(--primary-active);transform:scale(.99)}
  .links{margin-top:8px;font-size:13px}
  .links a{color:var(--primary);text-decoration:none;cursor:pointer;margin:0 8px}
  .links a:hover{text-decoration:underline}
  .spin{width:32px;height:32px;margin:8px auto 22px;border:3px solid #E5E5EA;
    border-top-color:var(--primary);border-radius:50%;animation:r .9s linear infinite}
  @keyframes r{to{transform:rotate(360deg)}}
  .hidden{display:none}
</style>
</head>
<body>
  <div class="card">
    <img class="logo" src="/icon.png" alt="HugAgentOS" onerror="this.style.display='none'"/>
    <!-- 初始态：等待用户点击登录 -->
    <div id="idle">
      <h1>HugAgentOS</h1>
      <button class="btn" onclick="startLogin()">登录并继续</button>
    </div>
    <!-- 等待态：浏览器已打开，等待回跳 -->
    <div id="waiting" class="hidden">
      <div class="spin"></div>
      <h1>正在浏览器中登录…</h1>
      <p class="sub">请在打开的浏览器中完成登录，<br/>成功后将自动返回本客户端。</p>
      <div class="links">
        <a onclick="startLogin()">没反应？重新打开</a>
        <a onclick="showIdle()">返回</a>
      </div>
    </div>
  </div>
  <script>
    function openBrowser(){
      // 整页导航到哨兵路径，由 Rust 导航守卫开系统浏览器。不走 Tauri IPC——
      // 远程源（本地反代 127.0.0.1:随机端口）下 window.__TAURI__ 不保证注入，
      // invoke('open_login') 会静默失效（表现为「点了没反应、浏览器不弹」）。
      window.location.href = '/__desktop/open-login';
    }
    function showWaiting(){
      document.getElementById('idle').classList.add('hidden');
      document.getElementById('waiting').classList.remove('hidden');
    }
    function showIdle(){
      document.getElementById('waiting').classList.add('hidden');
      document.getElementById('idle').classList.remove('hidden');
    }
    function startLogin(){ showWaiting(); openBrowser(); }
    // 启动 / 会话过期由壳子自动拉起浏览器，并带 ?waiting=1 → 直接进等待态。
    if(new URLSearchParams(location.search).get('waiting')==='1'){ showWaiting(); }
  </script>
</body>
</html>"##;

const INIT_HTML: &str = r##"<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>选择运行模式 · HugAgentOS</title>
<style>
  :root{
    color-scheme:light;
    --accent:#007AFF;--accent-hover:#0071E3;--accent-active:#0068D0;
    --text:#1D1D1F;--secondary:#6E6E73;--tertiary:#8E8E93;
    --line:rgba(60,60,67,.16);--surface:rgba(255,255,255,.72);--danger:#D70015;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Segoe UI",sans-serif;
    color:var(--text);background:#F5F5F7;display:flex;align-items:center;justify-content:center;
    min-height:100%;padding:24px;overflow:auto;-webkit-user-select:none;user-select:none}
  .setup{width:min(540px,100%);text-align:center;padding:20px 34px 30px}
  .logo{display:block;width:64px;height:64px;margin:0 auto 16px;border-radius:16px;
    box-shadow:0 1px 2px rgba(0,0,0,.08),0 12px 32px rgba(0,0,0,.09)}
  .product{margin:0 0 10px;color:var(--secondary);font-size:12px;font-weight:600}
  h1{margin:0;font-size:27px;line-height:1.16;font-weight:650;letter-spacing:-.028em}
  .lead{max-width:430px;margin:10px auto 0;color:var(--secondary);font-size:13.5px;line-height:1.6}
  .form{width:min(400px,100%);margin:24px auto 0;text-align:left}
  label{display:block;font-size:13px;font-weight:600;margin:0 0 7px;color:var(--text)}
  .select-wrap{position:relative}
  select{width:100%;height:46px;padding:0 38px 0 14px;border:1px solid var(--line);border-radius:12px;
    font-size:15px;color:var(--text);background:#fff;outline:none;appearance:none;-webkit-appearance:none;
    cursor:pointer;transition:border-color .14s ease,box-shadow .14s ease}
  select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(0,122,255,.16)}
  .select-wrap::after{content:"";position:absolute;right:16px;top:50%;width:8px;height:8px;
    border-right:1.6px solid var(--tertiary);border-bottom:1.6px solid var(--tertiary);
    transform:translateY(-70%) rotate(45deg);pointer-events:none}
  /* 云端地址栏始终占位、只切换可见性，切换模式时布局高度不变——不产生跳动。 */
  .cloud-field{margin-top:18px;visibility:hidden}
  .cloud-field.show{visibility:visible}
  input[type=text]{width:100%;height:44px;padding:0 14px;border:1px solid var(--line);border-radius:11px;
    font-size:14px;color:var(--text);background:#fff;outline:none;transition:border-color .14s ease,box-shadow .14s ease}
  input[type=text]:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(0,122,255,.16)}
  .err{color:var(--danger);font-size:12px;margin:8px 2px 0;min-height:16px}
  .button{width:100%;height:46px;margin-top:22px;border:0;border-radius:12px;padding:0 18px;font:600 15px/1 inherit;
    cursor:pointer;background:var(--accent);color:#fff;box-shadow:0 1px 1px rgba(0,0,0,.08),0 7px 20px rgba(0,122,255,.16);
    transition:background 120ms ease-out,transform 100ms ease-out,opacity 120ms ease-out}
  @media(hover:hover){.button:hover{background:var(--accent-hover)}}
  .button:active{background:var(--accent-active);transform:scale(.98)}
  .button:disabled{opacity:.5;cursor:default;transform:none}
  select:focus-visible,input:focus-visible,.button:focus-visible{outline:3px solid rgba(0,122,255,.28);outline-offset:3px}
  body.platform-macos .setup{margin-top:-8px}
  @media(max-width:620px){body{padding:16px}.setup{padding:16px 10px 24px}h1{font-size:25px}}
  @media(prefers-reduced-motion:reduce){.button{transition:none}}
</style>
</head>
<body class="platform-__PLATFORM__">
  <main class="setup">
    <img class="logo" src="/icon.png" alt="HugAgentOS" onerror="this.style.visibility='hidden'" />
    <p class="product">HugAgentOS</p>
    <h1 id="title">选择运行模式</h1>
    <div class="form">
      <label for="mode">运行模式</label>
      <div class="select-wrap">
        <select id="mode">
          <option value="local">本机模式 · 只在本机运行</option>
          <option value="cloud">云端模式 · 连接云端服务器</option>
          <option value="dual">本机 + 云端 · 双模式</option>
        </select>
      </div>
      <div class="cloud-field" id="cloudField">
        <label for="base">云端服务器地址</label>
        <input id="base" type="text" placeholder="https://agent.example.gov.cn" value="__CLOUD_BASE__" spellcheck="false" autocomplete="off" />
      </div>
      <div class="err" id="err"></div>
      <button class="button" id="go" type="button" onclick="start()">开始使用</button>
    </div>
  </main>
<script>
  var currentMode = '__CURRENT_MODE__' || 'local';
  var localSupported = __LOCAL_SUPPORTED__;
  var modeEl = document.getElementById('mode');
  var cloudField = document.getElementById('cloudField');
  var errEl = document.getElementById('err');
  function needsCloud(m){ return m === 'cloud' || m === 'dual'; }
  function needsLocal(m){ return m === 'local' || m === 'dual'; }
  function sync(){
    var m = modeEl.value;
    cloudField.classList.toggle('show', needsCloud(m));
    errEl.textContent = '';
    if(!localSupported && needsLocal(m)){
      errEl.textContent = '当前系统的安装包暂不支持本机服务，请选择「云端模式」。';
      document.getElementById('go').disabled = true;
    } else {
      document.getElementById('go').disabled = false;
    }
  }
  function start(){
    var m = modeEl.value;
    var base = (document.getElementById('base').value || '').trim();
    if(needsCloud(m)){
      if(!/^https?:\/\//i.test(base)){
        errEl.textContent = '请填写以 http:// 或 https:// 开头的云端服务器地址。';
        document.getElementById('base').focus();
        return;
      }
    }
    if(!localSupported && needsLocal(m)){
      errEl.textContent = '当前系统暂不支持本机服务，请选择「云端模式」。';
      return;
    }
    document.getElementById('go').disabled = true;
    // 整页导航到哨兵路径，由 Rust 导航守卫落盘并重启（不依赖 Tauri IPC）。
    var url = '/__desktop/provision?mode=' + encodeURIComponent(m);
    if(needsCloud(m)) url += '&base=' + encodeURIComponent(base);
    window.location.href = url;
  }
  modeEl.value = ['local','cloud','dual'].indexOf(currentMode) >= 0 ? currentMode : 'local';
  modeEl.addEventListener('change', sync);
  sync();
</script>
</body>
</html>"##;

const SETUP_HTML: &str = r##"<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>服务设置 · HugAgentOS</title>
<style>
  :root{
    color-scheme:light;
    --accent:#007AFF;--accent-hover:#0071E3;--accent-active:#0068D0;
    --text:#1D1D1F;--secondary:#6E6E73;--tertiary:#8E8E93;
    --line:rgba(60,60,67,.14);--surface:rgba(255,255,255,.72);
    --ok:#248A3D;--danger:#D70015;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Segoe UI",sans-serif;
    color:var(--text);background:#F5F5F7;display:flex;align-items:center;justify-content:center;
    min-height:100%;padding:24px;overflow:auto;-webkit-user-select:none;user-select:none}
  .setup{width:min(540px,100%);text-align:center;padding:20px 34px 30px}
  .logo{display:block;width:68px;height:68px;margin:0 auto 17px;border-radius:17px;
    box-shadow:0 1px 2px rgba(0,0,0,.08),0 12px 32px rgba(0,0,0,.09)}
  .product{margin:0 0 11px;color:var(--secondary);font-size:12px;font-weight:600;letter-spacing:.012em}
  h1{margin:0;font-size:30px;line-height:1.16;font-weight:650;letter-spacing:-.028em;font-optical-sizing:auto}
  .lead{max-width:420px;margin:11px auto 0;color:var(--secondary);font-size:14px;line-height:1.6}
  .actions{width:min(350px,100%);margin:26px auto 0}
  .button{width:100%;height:46px;border:0;border-radius:12px;padding:0 18px;font:600 14px/1 inherit;
    cursor:pointer;transition:background 120ms ease-out,transform 100ms ease-out,opacity 120ms ease-out}
  .button.primary{background:var(--accent);color:#fff;
    box-shadow:0 1px 1px rgba(0,0,0,.08),0 7px 20px rgba(0,122,255,.16)}
  @media(hover:hover){.button.primary:hover{background:var(--accent-hover)}}
  .button.primary:active{background:var(--accent-active);transform:scale(.97)}
  .button:disabled{opacity:.5;cursor:default;transform:none}
  .button:focus-visible,.link-button:focus-visible,.connection button:focus-visible,summary:focus-visible{
    outline:3px solid rgba(0,122,255,.28);outline-offset:3px}
  .link-button{margin-top:11px;padding:7px 10px;border:0;background:transparent;color:var(--accent);
    font:500 13px/1.2 inherit;cursor:pointer;border-radius:8px;transition:background 100ms ease-out,transform 100ms ease-out}
  @media(hover:hover){.link-button:hover{background:rgba(0,122,255,.075)}}
  .link-button:active{background:rgba(0,122,255,.11);transform:scale(.97)}
  .privacy-note{margin:16px 0 0;color:var(--tertiary);font-size:12px;line-height:1.5}
  .progress-wrap{display:none;width:min(410px,100%);margin:24px auto 0;padding:18px;text-align:left;
    border:0;border-radius:16px;background:var(--surface);backdrop-filter:blur(18px) saturate(145%);
    -webkit-backdrop-filter:blur(18px) saturate(145%);box-shadow:0 1px 1px rgba(0,0,0,.05),0 12px 34px rgba(0,0,0,.07);
    animation:materialize 260ms cubic-bezier(.2,.8,.2,1) both}
  @keyframes materialize{from{opacity:0;transform:scale(.985) translateY(4px)}to{opacity:1;transform:scale(1) translateY(0)}}
  .progress-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:10px;font-size:13px}
  .message{color:var(--secondary)}.percent{color:var(--accent);font-variant-numeric:tabular-nums}
  .progress{height:5px;border-radius:999px;background:rgba(118,118,128,.16);overflow:hidden}
  .bar{height:100%;width:0;border-radius:inherit;background:var(--accent);transition:width 280ms ease-out}
  .error{display:none;margin-top:12px;padding:10px 12px;border-radius:9px;background:#FFF1F0;color:var(--danger);
    font-size:12.5px;line-height:1.5}.ready{color:var(--ok);font-weight:600}
  details{margin-top:13px;color:var(--secondary);font-size:12px}summary{width:max-content;cursor:pointer;outline:none}
  .log{height:116px;margin:9px 0 0;padding:11px 12px;overflow:auto;border:0;
    border-radius:10px;background:rgba(118,118,128,.09);color:#48484A;font:11px/1.55 "SFMono-Regular",Consolas,monospace;
    white-space:pre-wrap;word-break:break-all;-webkit-user-select:text;user-select:text}
  .ready-actions{display:none;margin-top:16px}
  .connection{margin-top:22px;color:var(--tertiary);font-size:11px;line-height:1.5;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .connection button{padding:3px 5px;border:0;border-radius:6px;background:transparent;color:var(--secondary);font:inherit;cursor:pointer}
  body.platform-macos .setup{margin-top:-10px}
  @media(max-width:620px){body{padding:16px}.setup{padding:16px 10px 24px}h1{font-size:27px}}
  @media(max-height:650px){body{align-items:flex-start}.setup{padding-top:20px}}
  @media(prefers-reduced-motion:reduce){.button,.link-button,.bar{transition:none}.progress-wrap{animation:none}}
  @media(prefers-reduced-transparency:reduce){.progress-wrap{background:#FFF;backdrop-filter:none;-webkit-backdrop-filter:none}}
  @media(prefers-contrast:more){.progress-wrap{background:#FFF;box-shadow:0 0 0 1px rgba(0,0,0,.55)}.lead,.privacy-note,.connection,.message{color:#3A3A3C}}
</style>
</head>
<body class="platform-__PLATFORM__">
  <main class="setup">
    <img class="logo" src="/icon.png" alt="HugAgentOS" onerror="this.style.visibility='hidden'" />
    <p class="product">HugAgentOS</p>
    <h1 id="title">在这台电脑上开始使用</h1>
    <section class="actions" aria-label="初始化操作">
      <button class="button primary" id="install" type="button" onclick="installLocal()">从零开始安装</button>
    </section>
    <section class="progress-wrap" id="progressWrap" aria-live="polite">
      <div class="progress-head"><span class="message" id="message">准备安装…</span><span class="percent" id="percent">0%</span></div>
      <div class="progress" role="progressbar" aria-label="安装进度" aria-valuemin="0" aria-valuemax="100"><div class="bar" id="bar"></div></div>
      <div class="error" id="error"></div>
      <details id="details"><summary>查看安装详情</summary><pre class="log" id="log">等待安装日志…</pre></details>
      <div class="ready-actions" id="readyActions">
        <button class="button primary" id="readyButton" type="button" onclick="finishReady()">进入 HugAgentOS</button>
      </div>
    </section>
  </main>
<script>
  var manage = new URLSearchParams(location.search).get('manage') === '1';
  var activeLocal = __ACTIVE_LOCAL__;
  var dual = __HYBRID_DUAL__;
  var localSupported = __LOCAL_SUPPORTED__;
  var installing = false;
  var pollTimer = null;
  if(document.body.classList.contains('platform-macos')){
    document.getElementById('title').textContent='在这台 Mac 上开始使用';
  }
  if(manage){
    document.getElementById('title').textContent='本机服务';
    document.getElementById('install').textContent='安装或修复本机服务';
  }
  function sentinel(path){ window.location.href = path; }
  function activateLocal(){ sentinel('/__desktop/activate-local'); }
  // 双模式恒为云端为主：本机服务装好后回到云端应用，绝不整体切换登录目标。
  function finishReady(){ if(activeLocal||dual){location.replace('/');}else{activateLocal();} }
  async function installLocal(){
    if(!localSupported){showError('当前安装包暂不支持在此系统一键部署本机服务。');return;}
    installing = true;
    var button=document.getElementById('install');
    button.disabled=true;button.textContent='正在开始…';
    document.getElementById('progressWrap').style.display = 'block';
    document.getElementById('message').textContent='正在准备本机服务…';
    document.getElementById('error').style.display='none';
    try{
      var response=await fetch('/__desktop/setup/install',{method:'POST'});
      if(!response.ok)throw new Error('HTTP '+response.status);
      await response.json();
      poll();
    }catch(e){installing=false;showError('无法启动安装：'+e.message);}
  }
  function showError(text){
    var el=document.getElementById('error');el.textContent=text;el.style.display='block';
    document.getElementById('details').open=true;
    var button=document.getElementById('install');button.disabled=false;button.textContent='重新安装';
  }
  async function poll(){
    if(pollTimer){clearTimeout(pollTimer);pollTimer=null;}
    try{
      var response=await fetch('/__desktop/setup/status',{cache:'no-store'});
      var s=await response.json();
      activeLocal=!!s.active_local;
      var active=['installing','starting','ready','error'].includes(s.phase) || installing;
      if(active) document.getElementById('progressWrap').style.display='block';
      var progress=Math.max(0,Math.min(100,s.progress||0));
      document.getElementById('bar').style.width=progress+'%';
      document.querySelector('.progress').setAttribute('aria-valuenow',String(progress));
      document.getElementById('percent').textContent=progress+'%';
      document.getElementById('message').textContent=s.message||'准备安装…';
      document.getElementById('log').textContent=(s.logs&&s.logs.length?s.logs.join('\n'):'等待安装日志…');
      document.getElementById('log').scrollTop=document.getElementById('log').scrollHeight;
      if(!s.supported){
        showError('当前安装包暂不支持在此系统一键部署本机服务。');
        document.getElementById('install').style.display='none';
        return;
      }
      if(s.phase==='error'){showError(s.message||'安装失败，请重试。');installing=false;return;}
      if(s.ready){
        installing=false;
        document.getElementById('message').innerHTML='<span class="ready">本机服务已就绪</span>';
        document.getElementById('install').style.display='none';
        if(s.active_local && !manage){ setTimeout(function(){location.replace('/__desktop/login')},450);return; }
        if(!s.active_local && !manage){
          if(dual){
            document.getElementById('message').textContent='本机服务已就绪，正在返回…';
            setTimeout(function(){location.replace('/')},450);return;
          }
          document.getElementById('message').textContent='安装完成，正在切换到本机服务…';
          setTimeout(activateLocal,350);return;
        }
        document.getElementById('readyButton').textContent=(s.active_local||dual)?'返回应用':'切换到本机服务';
        document.getElementById('readyActions').style.display='block';
        return;
      }
      if(s.phase==='installing'||s.phase==='starting'){
        installing=true;var button=document.getElementById('install');button.disabled=true;
        button.textContent=s.phase==='starting'?'正在启动…':'正在安装…';
      }else if(s.installed&&!manage){
        document.getElementById('install').textContent='启动本机服务';
      }
    }catch(e){ if(installing) showError('读取安装状态失败：'+e.message); }
    pollTimer=setTimeout(poll,900);
  }
  poll();
</script>
</body>
</html>"##;

const CLOSE_CONFIRM_HTML: &str = r##"<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>关闭 · HugAgentOS</title>
<style>
  :root{
    --primary:#126DFF; --primary-hover:#3C87FF; --primary-active:#0862F3;
    --text:#262626; --text-2:#6B7280; --border:#E8EBF0;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    color:var(--text); background:#fff;
    display:flex; flex-direction:column; justify-content:center;
    padding:22px 26px; -webkit-user-select:none; user-select:none;
  }
  h1{font-size:16px;font-weight:600;margin:0 0 10px}
  p{font-size:13px;color:var(--text-2);line-height:1.7;margin:0 0 16px}
  .remember{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text);cursor:pointer;margin-bottom:20px}
  .remember input{width:15px;height:15px;cursor:pointer;accent-color:var(--primary)}
  .btns{display:flex;gap:12px;justify-content:flex-end}
  .btn{height:38px;padding:0 20px;border-radius:9px;cursor:pointer;font-size:14px;font-weight:500;border:1px solid var(--border);background:#fff;color:var(--text);transition:all .14s ease}
  .btn:hover{background:#F5F7FA}
  .btn.primary{border:none;background:var(--primary);color:#fff;box-shadow:0 4px 12px rgba(18,109,255,.26)}
  .btn.primary:hover{background:var(--primary-hover)}
  .btn.primary:active{background:var(--primary-active)}
</style>
</head>
<body>
  <h1>关闭HugAgentOS</h1>
  <p>关闭后可最小化到系统托盘继续在后台运行（自动化任务等），或直接退出程序。</p>
  <label class="remember"><input type="checkbox" id="remember" /> 记住我的选择，下次不再询问</label>
  <div class="btns">
    <button class="btn" onclick="decide('exit')">退出</button>
    <button class="btn primary" onclick="decide('minimize')">最小化到托盘</button>
  </div>
  <script>
    function decide(action){
      var remember = document.getElementById('remember').checked ? 1 : 0;
      // 整页导航到哨兵路径，由 Rust 导航守卫处理（不依赖 Tauri IPC）。
      window.location.href = '/__desktop/close-decide?action=' + action + '&remember=' + remember;
    }
  </script>
</body>
</html>"##;

const SERVER_CONFIG_HTML: &str = r##"<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>服务器地址 · HugAgentOS</title>
<style>
  :root{
    --primary:#126DFF; --primary-hover:#3C87FF; --primary-active:#0862F3;
    --text:#262626; --text-2:#6B7280; --border:#E8EBF0;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    color:var(--text); background:#fff;
    display:flex; justify-content:center; align-items:center;
    padding:24px 28px; -webkit-user-select:none; user-select:none;
  }
  .panel{width:min(520px,100%)}
  h1{font-size:16px;font-weight:600;margin:0 0 8px}
  p{font-size:12.5px;color:var(--text-2);line-height:1.7;margin:0 0 16px}
  label{display:block;font-size:13px;margin:0 0 6px;color:var(--text)}
  input{width:100%;height:40px;padding:0 12px;border:1px solid var(--border);border-radius:9px;
    font-size:14px;color:var(--text);outline:none;transition:border-color .14s ease}
  input:focus{border-color:var(--primary)}
  .btns{display:flex;gap:12px;justify-content:flex-end;margin-top:22px}
  .btn{height:38px;padding:0 20px;border-radius:9px;cursor:pointer;font-size:14px;font-weight:500;border:1px solid var(--border);background:#fff;color:var(--text);transition:all .14s ease}
  .btn:hover{background:#F5F7FA}
  .btn.primary{border:none;background:var(--primary);color:#fff;box-shadow:0 4px 12px rgba(18,109,255,.26)}
  .btn.primary:hover{background:var(--primary-hover)}
  .btn.primary:active{background:var(--primary-active)}
  .err{color:#D4380D;font-size:12px;margin-top:8px;min-height:16px}
</style>
</head>
<body>
  <div class="panel">
    <h1>服务器地址</h1>
    <p>设置本客户端连接的后端地址。保存后需重启客户端生效。</p>
    <label for="base">后端地址</label>
    <input id="base" type="text" placeholder="https://agent.example.gov.cn" value="__CURRENT_BASE__" spellcheck="false" />
    <div class="err" id="err"></div>
    <div class="btns">
      <button class="btn" onclick="history.back()">取消</button>
      <button class="btn primary" onclick="save()">保存并重启</button>
    </div>
  </div>
  <script>
    function save(){
      var v = (document.getElementById('base').value || '').trim();
      if(!/^https?:\/\//i.test(v)){
        document.getElementById('err').textContent = '请填写以 http:// 或 https:// 开头的完整地址';
        return;
      }
      // 整页导航到哨兵路径，由 Rust 导航守卫写回 server.json 并重启（不依赖 Tauri IPC）。
      window.location.href = '/__desktop/save-server?base=' + encodeURIComponent(v);
    }
    document.getElementById('base').focus();
  </script>
</body>
</html>"##;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_titlebar_keeps_icon_and_four_menus_on_one_row() {
        let block = titlebar_block(TB_OFFSET_SPA);
        assert!(block.contains("tb-logo"));
        assert!(!block.contains("tb-name"));
        assert!(!block.contains(&format!(">{}<", brand::NAME)));
        for label in ["文件", "编辑", "视图", "帮助"] {
            assert!(block.contains(label));
        }
        assert!(block.contains("data-win=\"minimize\""));
        assert!(block.contains("data-win=\"close\""));
    }

    #[test]
    fn mac_titlebar_is_a_compact_drag_region_without_duplicate_actions() {
        let block = mac_titlebar_block(MAC_OFFSET_SPA);
        assert!(block.contains("hugagent-mac-titlebar"));
        assert!(block.contains("height:28px"));
        assert!(block.contains("background:transparent"));
        assert!(!block.contains("border-bottom"));
        assert!(!block.contains("backdrop-filter"));
        assert!(!block.contains("data-act="));
        assert!(!block.contains("mac-toolButton"));
        assert!(!block.contains("data-win=\"minimize\""));
        assert!(!block.contains("data-win=\"close\""));
        assert!(!block.contains("tb-menuLabel"));
        assert!(block.contains("--hugagent-desktop-sidebar-width"));
        assert!(block.contains("linear-gradient(90deg,rgba(203,223,255,.38)"));
        assert!(block.contains("#F5F6F7!important"));
        assert!(block.contains(".jx-brandRow,.jx-miniRail{padding-top:0!important}"));
        assert!(block.contains("ResizeObserver"));
    }

    #[test]
    fn desktop_titlebar_offsets_fixed_feedback_overlays() {
        for block in [
            titlebar_block(TB_OFFSET_SPA),
            titlebar_block(TB_OFFSET_PAGE),
            mac_titlebar_block(MAC_OFFSET_SPA),
            mac_titlebar_block(MAC_OFFSET_PAGE),
        ] {
            assert!(block.contains(
                ".ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}"
            ));
            assert!(block.contains(
                ".ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight"
            ));
        }
        assert!(titlebar_block(TB_OFFSET_SPA).contains("--hugagent-desktop-titlebar-height:36px"));
        assert!(
            mac_titlebar_block(MAC_OFFSET_SPA).contains("--hugagent-desktop-titlebar-height:28px")
        );
    }

    #[test]
    fn setup_install_starts_in_place_before_switching_modes() {
        assert!(SETUP_HTML.contains("fetch('/__desktop/setup/install'"));
        assert!(SETUP_HTML.contains("从零开始安装"));
        assert!(!SETUP_HTML.contains("if(!activeLocal){ activateLocal(); return; }"));
    }

    #[test]
    fn setup_dual_mode_never_switches_login_target_to_local() {
        // 双模式云端为主：安装完成后回云端应用，不 activate-local。
        assert!(SETUP_HTML.contains("var dual = __HYBRID_DUAL__;"));
        assert!(SETUP_HTML.contains("if(activeLocal||dual){location.replace('/');}"));
        assert!(SETUP_HTML.contains("本机服务已就绪，正在返回…"));
    }

    #[test]
    fn setup_mac_copy_and_single_primary_action_are_present() {
        assert!(SETUP_HTML.contains("在这台 Mac 上开始使用"));
        assert_eq!(SETUP_HTML.matches("id=\"install\"").count(), 1);
        assert!(!SETUP_HTML.contains("class=\"choices\""));
        assert!(!SETUP_HTML.contains("border-top:1px solid"));
        assert!(SETUP_HTML.contains("transform:scale(.97)"));
        assert!(SETUP_HTML.contains("prefers-reduced-motion:reduce"));
        assert!(SETUP_HTML.contains("prefers-reduced-transparency:reduce"));
        assert!(SETUP_HTML.contains("prefers-contrast:more"));
    }

    #[test]
    fn init_page_offers_three_modes_and_conditional_cloud_field() {
        // 三种运行模式都在下拉里。
        assert!(INIT_HTML.contains("value=\"local\""));
        assert!(INIT_HTML.contains("value=\"cloud\""));
        assert!(INIT_HTML.contains("value=\"dual\""));
        // 云端地址输入 + 仅在含云端形态时展开。
        assert!(INIT_HTML.contains("id=\"base\""));
        assert!(INIT_HTML.contains("needsCloud"));
        // 提交走初始化哨兵。
        assert!(INIT_HTML.contains("/__desktop/provision?mode="));
        // 占位符齐全，渲染时都会被替换。
        assert!(INIT_HTML.contains("__CURRENT_MODE__"));
        assert!(INIT_HTML.contains("__CLOUD_BASE__"));
        assert!(INIT_HTML.contains("__LOCAL_SUPPORTED__"));
    }

    #[test]
    fn titlebar_is_injected_before_page_content() {
        let html = "<html><body><main>content</main></body></html>";
        let output = inject_after_body(html, "<header>titlebar</header>");
        assert_eq!(
            output,
            "<html><body><header>titlebar</header><main>content</main></body></html>"
        );
    }

    #[test]
    fn titlebar_is_injected_inside_body_with_attributes() {
        let html = "<!doctype html><html><body class=\"platform-macos\"><main>content</main></body></html>";
        let output = inject_after_body(html, "<header>titlebar</header>");
        assert_eq!(
            output,
            "<!doctype html><html><body class=\"platform-macos\"><header>titlebar</header><main>content</main></body></html>"
        );
    }
}
