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
    /// 初始化选择页的预填形态：全新安装（尚无 server.json）预填「本机模式」；
    /// 已有配置（显式选过或遗留 deployment_mode）沿用推断值，升级用户预填不变。
    pub init_mode_prefill: ProvisionMode,
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
    Html(inject_after_body(
        &with_theme_boot(&html),
        &platform_titlebar_block(false),
    ))
}

/// 关闭主窗口时的自定义确认页（带「记住我的选择」勾选框）。按钮整页导航到
/// `/__desktop/close-decide?action=..&remember=..`，由确认窗的 Rust 导航守卫执行。
async fn close_confirm_page() -> Html<String> {
    Html(with_theme_boot(
        &CLOSE_CONFIRM_HTML.replace("HugAgentOS", brand::NAME),
    ))
}

/// 「设置服务器地址」页（菜单栏「文件 → 设置服务器地址…」打开）。输入框预填当前后端地址，
/// 保存按钮整页导航到哨兵 `/__desktop/save-server?base=<encoded>`，由主窗口的 Rust 导航守卫
/// 写回 server.json 并重启。同样不走 Tauri IPC。
async fn server_config_page(State(state): State<ProxyState>) -> Html<String> {
    let html = SERVER_CONFIG_HTML
        .replace("__CURRENT_BASE__", &html_escape(&state.server_base))
        .replace("HugAgentOS", brand::NAME);
    Html(inject_after_body(
        &with_theme_boot(&html),
        &platform_titlebar_block(false),
    ))
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
    Html(inject_after_body(
        &with_theme_boot(&html),
        &platform_titlebar_block(false),
    ))
}

/// 初始化「运行模式选择」页（首启时展示）：下拉选本机 / 云端 / 双模式；选到含云端的
/// 形态时展开服务器地址输入。提交整页导航到哨兵 `/__desktop/provision?mode=..&base=..`，
/// 由主窗口的 Rust 导航守卫落盘并重启。`manage=1` 时是「稍后更改运行模式」入口。
async fn init_page(State(state): State<ProxyState>) -> Html<String> {
    let current_mode = match state.init_mode_prefill {
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
    Html(inject_after_body(
        &with_theme_boot(&html),
        &platform_titlebar_block(false),
    ))
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
// 主窗口关闭系统 decorations，避免「系统标题栏 + 原生菜单栏」占两行。Windows/Linux
// 保留一行紧凑菜单、页面前进后退和窗口控制。参考现代桌面应用的层级，最上面是一条全宽、
// 轻量的菜单 / 标签栏：左段延续侧边栏底色但不重复品牌 Logo，右段显示当前页面/对话名称；
// 侧边栏自己的品牌区从标题栏下方开始。中间空白仍承担窗口拖动。壳动作走导航哨兵，由
// lib.rs 拦截执行，不依赖远程源下不稳定的 Tauri IPC。

const TITLEBAR_HEIGHT: u8 = 34;
const TB_OFFSET_SPA: &str =
    ":root{--hugagent-desktop-titlebar-height:34px;--hugagent-desktop-sidebar-width:280px;--hugagent-desktop-sidebar-chrome:color-mix(in srgb, var(--color-bg-gray) 72%, transparent)}:root[data-theme='dark']{--hugagent-desktop-sidebar-chrome:var(--color-bg-layout)}body{box-sizing:border-box!important;padding-top:0!important}.jx-appMainLayout{box-sizing:border-box!important;padding-top:var(--hugagent-desktop-titlebar-height)!important}.jx-brandRow{padding-top:50px!important}.jx-miniRail{padding-top:48px!important}.jx-appLoading{height:100%!important}.jx-appLoading-main{box-sizing:border-box!important;padding-top:calc(40px + var(--hugagent-desktop-titlebar-height))!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";
const TB_OFFSET_PAGE: &str =
    ":root{--hugagent-desktop-titlebar-height:34px;--hugagent-desktop-sidebar-width:280px;--hugagent-desktop-sidebar-chrome:var(--color-bg-layout)}body{box-sizing:border-box!important;padding-top:34px!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";

// The traffic lights start at y=13 and occupy about 14px. A 28px overlay keeps
// their hit area clear without stacking a second, visibly empty toolbar above
// the application's own brand row.
const MAC_TITLEBAR_HEIGHT: u8 = 28;
// 左半幅要在视觉上**接着侧边栏往上长**，所以这里必须逐字复刻 sidebar.css 里 `.jx-sider`
// 的配方：`color-mix(in srgb, var(--color-bg-gray) 72%, transparent)` 压在页面底色上。
//
// 这段历史上写死过 `rgba(203,223,255,.38)` / `#FFFFFF` / `#F5F6F7`，是抄的侧边栏当年那版
// 浅蓝。前端把侧边栏令牌化之后它就双重失真了：浅色下不再和侧边栏同色，深色下更是用
// `!important` 把整个 body 底色摁回浅色 —— 深色模式在 macOS 客户端里直接不成立。
// 改引令牌后两档自动跟随，侧边栏配方再变也只需要改 sidebar.css 一处。
const MAC_OFFSET_SPA: &str =
    ":root{--hugagent-desktop-titlebar-height:28px;--hugagent-desktop-sidebar-width:0px}body{box-sizing:border-box!important;padding-top:28px!important;background:linear-gradient(90deg,color-mix(in srgb, var(--color-bg-gray) 72%, transparent) 0 var(--hugagent-desktop-sidebar-width),var(--color-bg-base) var(--hugagent-desktop-sidebar-width) 100%),var(--color-bg-layout)!important}.jx-brandRow,.jx-miniRail{padding-top:0!important}.jx-appLoading{height:100%!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";
const MAC_OFFSET_PAGE: &str =
    ":root{--hugagent-desktop-titlebar-height:28px}body{box-sizing:border-box!important;padding-top:28px!important}.ant-message{top:calc(var(--hugagent-desktop-titlebar-height) + 8px)!important}.ant-notification-top,.ant-notification-topLeft,.ant-notification-topRight{top:calc(var(--hugagent-desktop-titlebar-height) + 24px)!important}";

// 这条标题栏是**注进 SPA 自己那份文档**的（见 inject_after_body），所以 `<html>` 上的
// data-theme 对它同样生效，直接引用应用令牌即可两档自动跟随 —— 不需要再写一套深色覆盖，
// 也不需要 prefers-color-scheme（那会和手动 light/dark/system 三档打架）。
const TB_CSS: &str = r##"
#hugagent-titlebar{position:fixed;inset:0 0 auto 0;height:34px;z-index:2147483647;display:flex;align-items:stretch;background:var(--color-bg-layout);border:0;box-shadow:none;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--color-text);-webkit-user-select:none;user-select:none}
#hugagent-titlebar *{box-sizing:border-box}
#hugagent-titlebar .tb-sidebarZone{flex:0 0 var(--hugagent-desktop-sidebar-width);min-width:0;height:100%;padding:0 6px 0 10px;display:flex;align-items:center;gap:2px;background:var(--hugagent-desktop-sidebar-chrome,color-mix(in srgb, var(--color-bg-gray) 72%, transparent));transition:flex-basis .16s ease;overflow:visible}
#hugagent-titlebar .tb-mainChrome{flex:1;min-width:0;height:100%;display:flex;align-items:center;background:var(--hugagent-desktop-main-chrome,var(--color-bg-base));border:0}
#hugagent-titlebar .tb-historyNav{display:flex;align-items:center;gap:1px;height:100%;flex:0 0 auto}
#hugagent-titlebar .tb-navButton{width:30px;height:26px;margin:4px 0;padding:0;border:0;border-radius:6px;background:transparent;color:var(--color-text-secondary);display:flex;align-items:center;justify-content:center;cursor:default}
#hugagent-titlebar .tb-navButton:hover{background:var(--color-fill-hover);color:var(--color-text)}
#hugagent-titlebar .tb-navButton:disabled{opacity:.42;background:transparent}
#hugagent-titlebar .tb-spacer{flex:1;height:100%;min-width:48px}
#hugagent-titlebar .tb-menu{display:flex;align-items:stretch;height:100%;flex:0 0 auto}
#hugagent-titlebar .tb-menuGroup{position:relative;height:100%;display:flex;align-items:stretch}
#hugagent-titlebar .tb-menuLabel{height:26px;margin:4px 0;padding:0 7px;border:0;border-radius:6px;background:transparent;color:var(--color-text-secondary);display:flex;align-items:center;justify-content:center;font:12.5px/1 inherit;cursor:default}
#hugagent-titlebar .tb-menuLabel:hover,#hugagent-titlebar .tb-menuGroup.open>.tb-menuLabel{background:var(--color-fill-hover)}
#hugagent-titlebar .tb-navButton:focus-visible,#hugagent-titlebar .tb-menuLabel:focus-visible,#hugagent-titlebar .tb-windowButton:focus-visible{outline:2px solid var(--color-primary);outline-offset:-3px}
#hugagent-titlebar .tb-drop{display:none;position:absolute;top:32px;left:0;min-width:218px;padding:6px;background:var(--color-bg-elevated);border:1px solid var(--color-border);border-radius:8px;box-shadow:0 10px 28px color-mix(in srgb, var(--color-text) 16%, transparent)}
#hugagent-titlebar .tb-menuGroup.open>.tb-drop{display:block}
#hugagent-titlebar .tb-item{display:flex;align-items:center;justify-content:space-between;gap:18px;width:100%;min-height:34px;padding:7px 11px;border:0;border-radius:6px;background:transparent;color:var(--color-text);font:13px/1.3 inherit;text-align:left;white-space:nowrap;cursor:default}
#hugagent-titlebar .tb-item:hover,#hugagent-titlebar .tb-item:focus-visible{background:var(--color-primary-light);color:var(--color-primary);outline:none}
#hugagent-titlebar .tb-shortcut{color:var(--color-text-tertiary);font-size:12px}
#hugagent-titlebar .tb-sep{height:1px;margin:5px 6px;background:var(--color-border)}
#hugagent-titlebar .tb-controls{display:flex;align-items:stretch;height:100%;margin-left:0}
#hugagent-titlebar .tb-windowButton{width:46px;height:100%;padding:0;border:0;background:transparent;color:var(--color-text-secondary);display:flex;align-items:center;justify-content:center;cursor:default}
#hugagent-titlebar .tb-windowButton:hover{background:var(--color-fill-hover)}
/* dark-ok: #E81123 是 Windows 关闭键的平台约定红，两档都得是这个红，不跟主题翻转 */
#hugagent-titlebar .tb-windowButton.close:hover{background:#E81123;color:#fff}
"##;

const TB_NAV: &str = r##"<div class="tb-historyNav" aria-label="页面导航" data-i18n-aria="page_navigation">
<button class="tb-navButton" type="button" data-nav="back" aria-label="后退" title="后退" data-i18n-aria="back"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M9.75 3.5 5.25 8l4.5 4.5M5.5 8h6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
<button class="tb-navButton" type="button" data-nav="forward" aria-label="前进" title="前进" data-i18n-aria="forward"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="m6.25 3.5 4.5 4.5-4.5 4.5M10.5 8h-6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
</div>"##;

const TB_MENU: &str = r##"<nav class="tb-menu" aria-label="应用菜单" data-i18n-aria="app_menu">
<div class="tb-menuGroup" data-menu="file"><button class="tb-menuLabel" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="hugagent-file-menu" data-i18n="file">文件</button><div class="tb-drop" id="hugagent-file-menu" role="menu" aria-label="文件" data-i18n-aria="file">
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="new_chat"><span data-i18n="new_chat">新建对话</span><span class="tb-shortcut" aria-hidden="true">Ctrl+N</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="run_mode"><span data-i18n="run_mode">运行模式…</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="server_config"><span data-i18n="server_config">设置服务器地址…</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="local_server"><span data-i18n="local_server">本机服务…</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-win="quit"><span data-i18n="quit">退出</span></button>
</div></div>
<div class="tb-menuGroup" data-menu="edit"><button class="tb-menuLabel" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="hugagent-edit-menu" data-i18n="edit">编辑</button><div class="tb-drop" id="hugagent-edit-menu" role="menu" aria-label="编辑" data-i18n-aria="edit">
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="undo"><span data-i18n="undo">撤销</span><span class="tb-shortcut" aria-hidden="true">Ctrl+Z</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="redo"><span data-i18n="redo">重做</span><span class="tb-shortcut" aria-hidden="true">Ctrl+Y</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="cut"><span data-i18n="cut">剪切</span><span class="tb-shortcut" aria-hidden="true">Ctrl+X</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="copy"><span data-i18n="copy">复制</span><span class="tb-shortcut" aria-hidden="true">Ctrl+C</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="paste"><span data-i18n="paste">粘贴</span><span class="tb-shortcut" aria-hidden="true">Ctrl+V</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-edit="selectAll"><span data-i18n="select_all">全选</span><span class="tb-shortcut" aria-hidden="true">Ctrl+A</span></button>
</div></div>
<div class="tb-menuGroup" data-menu="view"><button class="tb-menuLabel" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="hugagent-view-menu" data-i18n="view">视图</button><div class="tb-drop" id="hugagent-view-menu" role="menu" aria-label="视图" data-i18n-aria="view">
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="reload"><span data-i18n="reload">重新加载</span><span class="tb-shortcut" aria-hidden="true">Ctrl+R</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-win="fullscreen"><span data-i18n="fullscreen">全屏</span><span class="tb-shortcut" aria-hidden="true">F11</span></button>
</div></div>
<div class="tb-menuGroup" data-menu="help"><button class="tb-menuLabel" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="hugagent-help-menu" data-i18n="help">帮助</button><div class="tb-drop" id="hugagent-help-menu" role="menu" aria-label="帮助" data-i18n-aria="help">
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="check_update"><span data-i18n="check_update">检查更新…</span></button>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="website"><span data-i18n="website">访问官网</span></button>
  <div class="tb-sep" role="separator"></div>
  <button class="tb-item" type="button" role="menuitem" tabindex="-1" data-act="about"><span data-i18n="about">关于</span></button>
</div></div>
</nav>"##;

const TB_CONTROLS: &str = r##"<div class="tb-controls">
<button class="tb-windowButton" type="button" data-win="minimize" aria-label="最小化" title="最小化" data-i18n-aria="minimize"><svg width="11" height="11" viewBox="0 0 12 12"><path d="M2.5 6.5h7" fill="none" stroke="currentColor" stroke-width="1.1"/></svg></button>
<button class="tb-windowButton" type="button" data-win="toggle-maximize" aria-label="最大化或还原" title="最大化 / 还原" data-i18n-aria="maximize_restore"><svg width="10" height="10" viewBox="0 0 12 12"><rect x="2.5" y="2.5" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.1"/></svg></button>
<button class="tb-windowButton close" type="button" data-win="close" aria-label="关闭" title="关闭" data-i18n-aria="close"><svg width="11" height="11" viewBox="0 0 12 12"><path d="m3 3 6 6m0-6L3 9" fill="none" stroke="currentColor" stroke-width="1.2"/></svg></button>
</div>"##;

const TB_JS: &str = r##"(function(){
var bar=document.getElementById('hugagent-titlebar');if(!bar)return;
// 快速问答使用独立原生小窗，不展示主窗口标题栏。
if(new URLSearchParams(location.search).get('quickask')==='1'){
  bar.remove();var style=document.getElementById('hugagent-titlebar-style');if(style)style.remove();return;
}
var desktopCopy={
  'zh-CN':{
    chrome:'桌面菜单栏',page_navigation:'页面导航',back:'后退',forward:'前进',app_menu:'应用菜单',
    file:'文件',edit:'编辑',view:'视图',help:'帮助',new_chat:'新建对话',run_mode:'运行模式…',
    server_config:'设置服务器地址…',local_server:'本机服务…',quit:'退出',undo:'撤销',redo:'重做',
    cut:'剪切',copy:'复制',paste:'粘贴',select_all:'全选',reload:'重新加载',fullscreen:'全屏',
    check_update:'检查更新…',website:'访问官网',about:'关于',minimize:'最小化',
    maximize_restore:'最大化 / 还原',close:'关闭'
  },
  en:{
    chrome:'Desktop menu bar',page_navigation:'Page navigation',back:'Back',forward:'Forward',app_menu:'Application menu',
    file:'File',edit:'Edit',view:'View',help:'Help',new_chat:'New Chat',run_mode:'Run Mode…',
    server_config:'Server Address…',local_server:'Local Service…',quit:'Exit',undo:'Undo',redo:'Redo',
    cut:'Cut',copy:'Copy',paste:'Paste',select_all:'Select All',reload:'Reload',fullscreen:'Full Screen',
    check_update:'Check for Updates…',website:'Visit Website',about:'About',minimize:'Minimize',
    maximize_restore:'Maximize / Restore',close:'Close'
  }
};
function desktopLang(){
  try{var saved=localStorage.getItem('jx_lang');if(saved==='en'||saved==='zh-CN')return saved;}catch(error){}
  var htmlLang=String(document.documentElement.lang||'').toLowerCase();
  if(htmlLang.indexOf('en')===0)return 'en';if(htmlLang.indexOf('zh')===0)return 'zh-CN';
  return String(navigator.language||'').toLowerCase().indexOf('en')===0?'en':'zh-CN';
}
function syncLocale(){
  var copy=desktopCopy[desktopLang()];if(bar.getAttribute('aria-label')!==copy.chrome)bar.setAttribute('aria-label',copy.chrome);
  bar.querySelectorAll('[data-i18n]').forEach(function(node){var value=copy[node.dataset.i18n];if(value&&node.textContent!==value)node.textContent=value;});
  bar.querySelectorAll('[data-i18n-aria]').forEach(function(node){
    var value=copy[node.dataset.i18nAria];if(!value)return;
    if(node.getAttribute('aria-label')!==value)node.setAttribute('aria-label',value);
    if(node.tagName==='BUTTON'&&node.title!==value)node.title=value;
  });
}
var groups=Array.prototype.slice.call(bar.querySelectorAll('.tb-menuGroup'));
var menuItems=Array.prototype.slice.call(bar.querySelectorAll('.tb-item'));
function itemsFor(group){return Array.prototype.slice.call(group.querySelectorAll('.tb-item'));}
function closeMenus(restoreLabel){
  groups.forEach(function(group){
    group.classList.remove('open');
    var label=group.querySelector('.tb-menuLabel');if(label)label.setAttribute('aria-expanded','false');
  });
  menuItems.forEach(function(item){item.tabIndex=-1;});
  if(restoreLabel)restoreLabel.focus();
}
function openMenu(group,focusIndex){
  closeMenus(false);group.classList.add('open');
  var label=group.querySelector('.tb-menuLabel');if(label)label.setAttribute('aria-expanded','true');
  var items=itemsFor(group);
  if(items.length&&focusIndex!=null){
    var index=Math.max(0,Math.min(items.length-1,focusIndex));items[index].tabIndex=0;items[index].focus();
  }
}
function adjacentGroup(group,delta){
  var index=groups.indexOf(group);return groups[(index+delta+groups.length)%groups.length];
}
function sentinel(path){window.location.href=path;}
var lastEditTarget=null;
document.addEventListener('focusin',function(event){if(!bar.contains(event.target))lastEditTarget=event.target;});
document.addEventListener('keydown',function(event){
  var key=String(event.key||'').toLowerCase();
  if(event.ctrlKey&&!event.altKey&&key==='n'){
    event.preventDefault();sentinel('/__desktop/menu?action=new_chat');
  }else if(event.ctrlKey&&!event.altKey&&key==='r'){
    event.preventDefault();sentinel('/__desktop/menu?action=reload');
  }else if(event.key==='F11'){
    event.preventDefault();sentinel('/__desktop/win?action=fullscreen');
  }
});
bar.querySelectorAll('[data-nav]').forEach(function(button){button.addEventListener('click',function(event){
  event.stopPropagation();closeMenus(false);
  if(button.dataset.nav==='back')history.back();else history.forward();
});});
groups.forEach(function(group){
  var label=group.querySelector('.tb-menuLabel');var drop=group.querySelector('.tb-drop');
  label.addEventListener('click',function(event){
    event.stopPropagation();if(group.classList.contains('open'))closeMenus(label);else openMenu(group,0);
  });
  label.addEventListener('keydown',function(event){
    if(event.key==='ArrowDown'||event.key==='Enter'||event.key===' '){event.preventDefault();openMenu(group,0);}
    else if(event.key==='ArrowUp'){event.preventDefault();openMenu(group,itemsFor(group).length-1);}
    else if(event.key==='ArrowLeft'||event.key==='ArrowRight'){
      event.preventDefault();adjacentGroup(group,event.key==='ArrowLeft'?-1:1).querySelector('.tb-menuLabel').focus();
    }else if(event.key==='Escape'){event.preventDefault();closeMenus(label);}
  });
  drop.addEventListener('keydown',function(event){
    var items=itemsFor(group);var current=items.indexOf(document.activeElement);var next=current;
    if(event.key==='ArrowDown')next=(current+1+items.length)%items.length;
    else if(event.key==='ArrowUp')next=(current-1+items.length)%items.length;
    else if(event.key==='Home')next=0;
    else if(event.key==='End')next=items.length-1;
    else if(event.key==='ArrowLeft'||event.key==='ArrowRight'){
      event.preventDefault();openMenu(adjacentGroup(group,event.key==='ArrowLeft'?-1:1),0);return;
    }else if(event.key==='Escape'){event.preventDefault();closeMenus(label);return;}
    else if(event.key==='Tab'){closeMenus(false);return;}
    else return;
    event.preventDefault();items.forEach(function(item){item.tabIndex=-1;});items[next].tabIndex=0;items[next].focus();
  });
});
bar.querySelectorAll('[data-win]').forEach(function(item){item.addEventListener('click',function(event){
  event.stopPropagation();closeMenus(false);sentinel('/__desktop/win?action='+encodeURIComponent(item.dataset.win));
});});
bar.querySelectorAll('[data-act]').forEach(function(item){item.addEventListener('click',function(event){
  event.stopPropagation();closeMenus(false);sentinel('/__desktop/menu?action='+encodeURIComponent(item.dataset.act));
});});
bar.querySelectorAll('[data-edit]').forEach(function(item){item.addEventListener('click',function(event){
  event.stopPropagation();var command=item.dataset.edit;closeMenus(false);
  if(lastEditTarget&&lastEditTarget.isConnected&&typeof lastEditTarget.focus==='function'){
    try{lastEditTarget.focus({preventScroll:true});}catch(error){lastEditTarget.focus();}
  }
  document.execCommand(command,false,null);
});});
document.addEventListener('click',function(event){if(!bar.contains(event.target))closeMenus(false);});
var observedSidebar=null;
var sidebarResizeObserver=typeof ResizeObserver==='function'?new ResizeObserver(syncSidebarWidth):null;
// 标题栏左右两段必须和它正下方的侧边栏 / 主面板同色。两侧底色都是应用令牌算出来的
// （主面板还会随页面在 --color-bg-chat / --color-bg-container 之间切），所以这里不复刻
// 配方，直接取下方元素的计算底色写回令牌——换主题、换页面都自动跟随。
var lastRootVar={};
function sampleBg(selector){
  var node=document.querySelector(selector);if(!node)return '';
  var value=getComputedStyle(node).backgroundColor;
  if(!value||value==='transparent'||/^rgba\(0,\s*0,\s*0,\s*0\)$/.test(value))return '';
  return value;
}
function setRootVar(name,value){
  if(!value||lastRootVar[name]===value)return;
  lastRootVar[name]=value;document.documentElement.style.setProperty(name,value);
}
function syncSurfaceTint(){
  setRootVar('--hugagent-desktop-main-chrome',
    sampleBg('.jx-primaryPane')||sampleBg('.jx-appLoading-main')||sampleBg('.jx-content')||sampleBg('.jx-appMainLayout')||sampleBg('body'));
  setRootVar('--hugagent-desktop-sidebar-chrome',
    sampleBg('.jx-sider')||sampleBg('.jx-appLoading-sidebar'));
}
function syncSidebarWidth(){
  var sidebar=document.querySelector('.jx-sider,.jx-appLoading-sidebar');
  if(sidebarResizeObserver&&sidebar&&sidebar!==observedSidebar){
    if(observedSidebar)sidebarResizeObserver.unobserve(observedSidebar);
    observedSidebar=sidebar;sidebarResizeObserver.observe(sidebar);
  }
  if(!sidebar)return;
  var rect=sidebar.getBoundingClientRect();
  var width=Math.max(0,Math.min(window.innerWidth,Math.round(rect.right)));
  setRootVar('--hugagent-desktop-sidebar-width',width+'px');
}
var chromeSyncQueued=false;
function scheduleChromeSync(){
  if(chromeSyncQueued)return;chromeSyncQueued=true;
  requestAnimationFrame(function(){chromeSyncQueued=false;syncSidebarWidth();syncSurfaceTint();syncLocale();});
}
syncSidebarWidth();syncSurfaceTint();syncLocale();
new MutationObserver(scheduleChromeSync).observe(document.documentElement,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['class','style','data-theme','lang']});
window.addEventListener('resize',scheduleChromeSync);
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
<div class=\"tb-sidebarZone\">{navigation}{menu}</div><div class=\"tb-mainChrome\">\
<div class=\"tb-spacer\"></div>{controls}</div></header><script>{script}</script>",
        css = TB_CSS,
        offset = offset_css,
        height = TITLEBAR_HEIGHT,
        navigation = TB_NAV,
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

/* 壳页面（登录 / 首启 / 部署 / 关闭确认 / 服务器地址）是**各自独立的文档**，
   拿不到 SPA 那份 `<html data-theme>`，所以每张都要自己把主题落一遍。

   规则与 `src/frontend/index.html` 的防闪烁脚本逐字同源：同一个 localStorage key、
   同一套解析（不是 light/dark 的值一律按 system 走系统外观）。壳页面由本地反代提供，
   与 SPA **同源**，因此读得到同一份偏好——用户手动选了深色而系统是浅色时它们也跟着深。
   这正是不能用 `@media (prefers-color-scheme:dark)` 的原因：那只认系统，会和手动三档打架
   （前端门禁把它列为违规也是这个道理）。

   Web 端那条「分享预览锁浅色」的分支是浏览器专有，壳页面没有分享场景，故不带。
   index.html / ce overlay 的 index.html 改了解析规则，这里要一起改。 */
const THEME_BOOT_JS: &str = r##"<script>
;(function(){try{
var mode=localStorage.getItem('hugagent_theme_mode');
var dark=mode==='dark'||(mode!=='light'&&typeof matchMedia==='function'&&matchMedia('(prefers-color-scheme: dark)').matches);
if(dark){document.documentElement.setAttribute('data-theme','dark');document.documentElement.style.colorScheme='dark';}
}catch(e){}})()
</script>"##;

/// 把主题引导脚本插进 `<head>` 最前面——必须**早于任何样式**执行，否则深色用户会先看到
/// 一帧白底再翻黑。
fn with_theme_boot(html: &str) -> String {
    const HEAD: &str = "<head>";
    match html.find(HEAD) {
        Some(index) => {
            let at = index + HEAD.len();
            let mut output = String::with_capacity(html.len() + THEME_BOOT_JS.len());
            output.push_str(&html[..at]);
            output.push_str(THEME_BOOT_JS);
            output.push_str(&html[at..]);
            output
        }
        None => format!("{THEME_BOOT_JS}{html}"),
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
  /* dark-ok-begin: 壳页面是独立文档，取不到 SPA 的令牌，这里就是它自己的调色板真源，
     浅深两套成对定义——取值与 src/frontend/src/styles/variables.css 的同名令牌一致 */
  :root{
    color-scheme:light;--primary:#0A66FF;--primary-hover:#005BE6;--primary-active:#0052CC;
    --text:#1D1D1F;--text-2:#6E6E73;--text-3:#8E8E93;
    --page-top:#FBFBFA;--page-bottom:#F4F4F2;--ring:#E5E5EA;
  }
  :root[data-theme="dark"]{
    color-scheme:dark;--primary:#3E8BFF;--primary-hover:#5FA0FF;--primary-active:#2E7BF0;
    --text:#E8ECF4;--text-2:#B3BDCD;--text-3:#8792A4;
    --page-top:#141A22;--page-bottom:#0F141B;--ring:#2B3442;
  }
  /* dark-ok-end */
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Segoe UI",sans-serif;
    color:var(--text);
    background:linear-gradient(180deg,var(--page-top) 0%,var(--page-bottom) 100%);
    display:flex; align-items:center; justify-content:center;
    -webkit-user-select:none; user-select:none;
  }
  .card{
    width:min(380px,calc(100% - 40px));padding:32px 20px;text-align:center;margin-top:-18px;
  }
  .logo{
    width:64px;height:64px;border-radius:16px;margin:0 auto 20px;display:block;
    box-shadow:0 8px 24px rgba(0,0,0,.1); /* dark-ok: 投影两档都是黑，不随主题翻转 */
  }
  h1{font-size:28px;line-height:1.2;font-weight:650;margin:0;letter-spacing:-.035em}
  .sub{font-size:14px;color:var(--text-2);margin:12px 0 28px;line-height:1.65}
  .btn{
    width:100%;height:46px;margin-top:28px;border:none;border-radius:11px;cursor:pointer;
    /* dark-ok: 白字压在品牌色实心按钮上，两档都是白；投影两档都是黑 */
    background:var(--primary);color:#fff;font-size:14px;font-weight:600;
  /* dark-ok: 白字压在品牌色实心按钮上，两档都是白；投影两档都是黑 */
    transition:background .14s ease,transform .08s ease;box-shadow:0 1px 2px rgba(0,0,0,.08);
  }
  .btn:hover{background:var(--primary-hover)}
  .btn:active{background:var(--primary-active);transform:scale(.99)}
  .links{margin-top:8px;font-size:13px}
  .links a{color:var(--primary);text-decoration:none;cursor:pointer;margin:0 8px}
  .links a:hover{text-decoration:underline}
  .spin{width:32px;height:32px;margin:8px auto 22px;border:3px solid var(--ring);
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
  /* dark-ok-begin: 壳页面是独立文档，取不到 SPA 的令牌，这里就是它自己的调色板真源，
     浅深两套成对定义——取值与 src/frontend/src/styles/variables.css 的同名令牌一致 */
  :root{
    color-scheme:light;
    --accent:#007AFF;--accent-hover:#0071E3;--accent-active:#0068D0;
    --text:#1D1D1F;--secondary:#6E6E73;--tertiary:#8E8E93;
    --line:rgba(60,60,67,.16);--surface:rgba(255,255,255,.72);--danger:#D70015;
    --page:#F5F5F7;--field:#FFFFFF;
  }
  :root[data-theme="dark"]{
    color-scheme:dark;
    --accent:#3E8BFF;--accent-hover:#5FA0FF;--accent-active:#2E7BF0;
    --text:#E8ECF4;--secondary:#B3BDCD;--tertiary:#8792A4;
    --line:#2B3442;--surface:rgba(28,35,48,.72);--danger:#FF6B6B;
    --page:#0F141B;--field:#161C25;
  }
  /* dark-ok-end */
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Segoe UI",sans-serif;
    color:var(--text);background:var(--page);display:flex;align-items:center;justify-content:center;
    min-height:100%;padding:24px;overflow:auto;-webkit-user-select:none;user-select:none}
  .setup{width:min(540px,100%);text-align:center;padding:20px 34px 30px}
  .logo{display:block;width:64px;height:64px;margin:0 auto 16px;border-radius:16px;
    box-shadow:0 1px 2px rgba(0,0,0,.08),0 12px 32px rgba(0,0,0,.09)} /* dark-ok: 投影两档都是黑 */
  .product{margin:0 0 10px;color:var(--secondary);font-size:12px;font-weight:600}
  h1{margin:0;font-size:27px;line-height:1.16;font-weight:650;letter-spacing:-.028em}
  .lead{max-width:430px;margin:10px auto 0;color:var(--secondary);font-size:13.5px;line-height:1.6}
  .form{width:min(400px,100%);margin:24px auto 0;text-align:left}
  label{display:block;font-size:13px;font-weight:600;margin:0 0 7px;color:var(--text)}
  .select-wrap{position:relative}
  select{width:100%;height:46px;padding:0 38px 0 14px;border:1px solid var(--line);border-radius:12px;
    font-size:15px;color:var(--text);background:var(--field);outline:none;appearance:none;-webkit-appearance:none;
    cursor:pointer;transition:border-color .14s ease,box-shadow .14s ease}
  select:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent)}
  .select-wrap::after{content:"";position:absolute;right:16px;top:50%;width:8px;height:8px;
    border-right:1.6px solid var(--tertiary);border-bottom:1.6px solid var(--tertiary);
    transform:translateY(-70%) rotate(45deg);pointer-events:none}
  /* 云端地址栏始终占位、只切换可见性，切换模式时布局高度不变——不产生跳动。 */
  .cloud-field{margin-top:18px;visibility:hidden}
  .cloud-field.show{visibility:visible}
  input[type=text]{width:100%;height:44px;padding:0 14px;border:1px solid var(--line);border-radius:11px;
    font-size:14px;color:var(--text);background:var(--field);outline:none;transition:border-color .14s ease,box-shadow .14s ease}
  input[type=text]:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent)}
  .err{color:var(--danger);font-size:12px;margin:8px 2px 0;min-height:16px}
  .button{width:100%;height:46px;margin-top:22px;border:0;border-radius:12px;padding:0 18px;font:600 15px/1 inherit;
    /* dark-ok: 白字压在品牌色实心按钮上，两档都是白；投影两档都是黑 */
    cursor:pointer;background:var(--accent);color:#fff;box-shadow:0 1px 1px rgba(0,0,0,.08),0 7px 20px color-mix(in srgb, var(--accent) 16%, transparent);
    transition:background 120ms ease-out,transform 100ms ease-out,opacity 120ms ease-out}
  @media(hover:hover){.button:hover{background:var(--accent-hover)}}
  .button:active{background:var(--accent-active);transform:scale(.98)}
  .button:disabled{opacity:.5;cursor:default;transform:none}
  select:focus-visible,input:focus-visible,.button:focus-visible{outline:3px solid color-mix(in srgb, var(--accent) 28%, transparent);outline-offset:3px}
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
  /* dark-ok-begin: 壳页面是独立文档，取不到 SPA 的令牌，这里就是它自己的调色板真源，
     浅深两套成对定义——取值与 src/frontend/src/styles/variables.css 的同名令牌一致 */
  :root{
    color-scheme:light;
    --accent:#007AFF;--accent-hover:#0071E3;--accent-active:#0068D0;
    --text:#1D1D1F;--secondary:#6E6E73;--tertiary:#8E8E93;
    --line:rgba(60,60,67,.14);--surface:rgba(255,255,255,.72);
    --ok:#248A3D;--danger:#D70015;
    --page:#F5F5F7;--solid:#FFFFFF;--danger-bg:#FFF1F0;--log-ink:#48484A;--contrast-ink:#3A3A3C;
  }
  :root[data-theme="dark"]{
    color-scheme:dark;
    --accent:#3E8BFF;--accent-hover:#5FA0FF;--accent-active:#2E7BF0;
    --text:#E8ECF4;--secondary:#B3BDCD;--tertiary:#8792A4;
    --line:#2B3442;--surface:rgba(28,35,48,.72);
    --ok:#22C79D;--danger:#FF6B6B;
    --page:#0F141B;--solid:#161C25;--danger-bg:rgba(255,107,107,.16);--log-ink:#B3BDCD;--contrast-ink:#E8ECF4;
  }
  /* dark-ok-end */
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Segoe UI",sans-serif;
    color:var(--text);background:var(--page);display:flex;align-items:center;justify-content:center;
    min-height:100%;padding:24px;overflow:auto;-webkit-user-select:none;user-select:none}
  .setup{width:min(540px,100%);text-align:center;padding:20px 34px 30px}
  .logo{display:block;width:68px;height:68px;margin:0 auto 17px;border-radius:17px;
    box-shadow:0 1px 2px rgba(0,0,0,.08),0 12px 32px rgba(0,0,0,.09)} /* dark-ok: 投影两档都是黑 */
  .product{margin:0 0 11px;color:var(--secondary);font-size:12px;font-weight:600;letter-spacing:.012em}
  h1{margin:0;font-size:30px;line-height:1.16;font-weight:650;letter-spacing:-.028em;font-optical-sizing:auto}
  .lead{max-width:420px;margin:11px auto 0;color:var(--secondary);font-size:14px;line-height:1.6}
  .actions{width:min(350px,100%);margin:26px auto 0}
  .button{width:100%;height:46px;border:0;border-radius:12px;padding:0 18px;font:600 14px/1 inherit;
    cursor:pointer;transition:background 120ms ease-out,transform 100ms ease-out,opacity 120ms ease-out}
  /* dark-ok: 白字压在品牌色实心按钮上，两档都是白；投影两档都是黑 */
  .button.primary{background:var(--accent);color:#fff;
    box-shadow:0 1px 1px rgba(0,0,0,.08),0 7px 20px color-mix(in srgb, var(--accent) 16%, transparent)} /* dark-ok: 投影两档都是黑 */
  @media(hover:hover){.button.primary:hover{background:var(--accent-hover)}}
  .button.primary:active{background:var(--accent-active);transform:scale(.97)}
  .button:disabled{opacity:.5;cursor:default;transform:none}
  .button:focus-visible,.link-button:focus-visible,.connection button:focus-visible,summary:focus-visible{
    outline:3px solid color-mix(in srgb, var(--accent) 28%, transparent);outline-offset:3px}
  .link-button{margin-top:11px;padding:7px 10px;border:0;background:transparent;color:var(--accent);
    font:500 13px/1.2 inherit;cursor:pointer;border-radius:8px;transition:background 100ms ease-out,transform 100ms ease-out}
  @media(hover:hover){.link-button:hover{background:color-mix(in srgb, var(--accent) 8%, transparent)}}
  .link-button:active{background:color-mix(in srgb, var(--accent) 11%, transparent);transform:scale(.97)}
  .privacy-note{margin:16px 0 0;color:var(--tertiary);font-size:12px;line-height:1.5}
  .progress-wrap{display:none;width:min(410px,100%);margin:24px auto 0;padding:18px;text-align:left;
    border:0;border-radius:16px;background:var(--surface);backdrop-filter:blur(18px) saturate(145%);
    /* dark-ok: 投影两档都是黑 */
    -webkit-backdrop-filter:blur(18px) saturate(145%);box-shadow:0 1px 1px rgba(0,0,0,.05),0 12px 34px rgba(0,0,0,.07);
    animation:materialize 260ms cubic-bezier(.2,.8,.2,1) both}
  @keyframes materialize{from{opacity:0;transform:scale(.985) translateY(4px)}to{opacity:1;transform:scale(1) translateY(0)}}
  .progress-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:10px;font-size:13px}
  .message{color:var(--secondary)}.percent{color:var(--accent);font-variant-numeric:tabular-nums}
  /* dark-ok: 半透明中性灰，压在任一档底色上都成立 */
  .progress{height:5px;border-radius:999px;background:rgba(118,118,128,.16);overflow:hidden}
  .bar{height:100%;width:0;border-radius:inherit;background:var(--accent);transition:width 280ms ease-out}
  .error{display:none;margin-top:12px;padding:10px 12px;border-radius:9px;background:var(--danger-bg);color:var(--danger);
    font-size:12.5px;line-height:1.5}.ready{color:var(--ok);font-weight:600}
  details{margin-top:13px;color:var(--secondary);font-size:12px}summary{width:max-content;cursor:pointer;outline:none}
  .log{height:116px;margin:9px 0 0;padding:11px 12px;overflow:auto;border:0;
    /* dark-ok: 半透明中性灰，压在任一档底色上都成立 */
    border-radius:10px;background:rgba(118,118,128,.09);color:var(--log-ink);font:11px/1.55 "SFMono-Regular",Consolas,monospace;
    white-space:pre-wrap;word-break:break-all;-webkit-user-select:text;user-select:text}
  .ready-actions{display:none;margin-top:16px}
  .connection{margin-top:22px;color:var(--tertiary);font-size:11px;line-height:1.5;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .connection button{padding:3px 5px;border:0;border-radius:6px;background:transparent;color:var(--secondary);font:inherit;cursor:pointer}
  body.platform-macos .setup{margin-top:-10px}
  @media(max-width:620px){body{padding:16px}.setup{padding:16px 10px 24px}h1{font-size:27px}}
  @media(max-height:650px){body{align-items:flex-start}.setup{padding-top:20px}}
  @media(prefers-reduced-motion:reduce){.button,.link-button,.bar{transition:none}.progress-wrap{animation:none}}
  @media(prefers-reduced-transparency:reduce){.progress-wrap{background:var(--solid);backdrop-filter:none;-webkit-backdrop-filter:none}}
  /* dark-ok: 高对比度描边固定用黑，深色档底色已由 --solid 翻过去 */
  @media(prefers-contrast:more){.progress-wrap{background:var(--solid);box-shadow:0 0 0 1px rgba(0,0,0,.55)}.lead,.privacy-note,.connection,.message{color:var(--contrast-ink)}}
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
  /* dark-ok-begin: 壳页面是独立文档，取不到 SPA 的令牌，这里就是它自己的调色板真源，
     浅深两套成对定义——取值与 src/frontend/src/styles/variables.css 的同名令牌一致 */
  :root{
    color-scheme:light;
    --primary:#126DFF; --primary-hover:#3C87FF; --primary-active:#0862F3;
    --text:#262626; --text-2:#6B7280; --border:#E8EBF0;
    --surface:#FFFFFF; --surface-hover:#F5F7FA;
  }
  :root[data-theme="dark"]{
    color-scheme:dark;
    --primary:#3E8BFF; --primary-hover:#5FA0FF; --primary-active:#2E7BF0;
    --text:#E8ECF4; --text-2:#B3BDCD; --border:#2B3442;
    --surface:#161C25; --surface-hover:#252D39;
  }
  /* dark-ok-end */
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    color:var(--text); background:var(--surface);
    display:flex; flex-direction:column; justify-content:center;
    padding:22px 26px; -webkit-user-select:none; user-select:none;
  }
  h1{font-size:16px;font-weight:600;margin:0 0 10px}
  p{font-size:13px;color:var(--text-2);line-height:1.7;margin:0 0 16px}
  .remember{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text);cursor:pointer;margin-bottom:20px}
  .remember input{width:15px;height:15px;cursor:pointer;accent-color:var(--primary)}
  .btns{display:flex;gap:12px;justify-content:flex-end}
  .btn{height:38px;padding:0 20px;border-radius:9px;cursor:pointer;font-size:14px;font-weight:500;border:1px solid var(--border);background:var(--surface);color:var(--text);transition:all .14s ease}
  .btn:hover{background:var(--surface-hover)}
  /* dark-ok: 白字压在品牌色实心按钮上，两档都是白；投影两档都是黑 */
  .btn.primary{border:none;background:var(--primary);color:#fff;box-shadow:0 4px 12px color-mix(in srgb, var(--primary) 26%, transparent)}
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
  /* dark-ok-begin: 壳页面是独立文档，取不到 SPA 的令牌，这里就是它自己的调色板真源，
     浅深两套成对定义——取值与 src/frontend/src/styles/variables.css 的同名令牌一致 */
  :root{
    color-scheme:light;
    --primary:#126DFF; --primary-hover:#3C87FF; --primary-active:#0862F3;
    --text:#262626; --text-2:#6B7280; --border:#E8EBF0;
    --surface:#FFFFFF; --surface-hover:#F5F7FA; --danger:#D4380D;
  }
  :root[data-theme="dark"]{
    color-scheme:dark;
    --primary:#3E8BFF; --primary-hover:#5FA0FF; --primary-active:#2E7BF0;
    --text:#E8ECF4; --text-2:#B3BDCD; --border:#2B3442;
    --surface:#161C25; --surface-hover:#252D39; --danger:#FF6B6B;
  }
  /* dark-ok-end */
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    color:var(--text); background:var(--surface);
    display:flex; justify-content:center; align-items:center;
    padding:24px 28px; -webkit-user-select:none; user-select:none;
  }
  .panel{width:min(520px,100%)}
  h1{font-size:16px;font-weight:600;margin:0 0 8px}
  p{font-size:12.5px;color:var(--text-2);line-height:1.7;margin:0 0 16px}
  label{display:block;font-size:13px;margin:0 0 6px;color:var(--text)}
  input{width:100%;height:40px;padding:0 12px;border:1px solid var(--border);border-radius:9px;
    font-size:14px;color:var(--text);background:var(--surface);outline:none;transition:border-color .14s ease}
  input:focus{border-color:var(--primary)}
  .btns{display:flex;gap:12px;justify-content:flex-end;margin-top:22px}
  .btn{height:38px;padding:0 20px;border-radius:9px;cursor:pointer;font-size:14px;font-weight:500;border:1px solid var(--border);background:var(--surface);color:var(--text);transition:all .14s ease}
  .btn:hover{background:var(--surface-hover)}
  /* dark-ok: 白字压在品牌色实心按钮上，两档都是白；投影两档都是黑 */
  .btn.primary{border:none;background:var(--primary);color:#fff;box-shadow:0 4px 12px color-mix(in srgb, var(--primary) 26%, transparent)}
  .btn.primary:hover{background:var(--primary-hover)}
  .btn.primary:active{background:var(--primary-active)}
  .err{color:var(--danger);font-size:12px;margin-top:8px;min-height:16px}
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
    fn windows_titlebar_has_compact_localized_menus_without_a_context_tab() {
        let block = titlebar_block(TB_OFFSET_SPA);
        assert!(!block.contains("tb-logo"));
        assert!(!block.contains(brand::LOGIN_LOGO_URL));
        assert!(!block.contains("tb-name"));
        assert!(!block.contains(&format!(">{}<", brand::NAME)));
        for label in [">文件<", ">编辑<", ">视图<", ">帮助<"] {
            assert!(block.contains(label));
        }
        for key in ["file", "edit", "view", "help"] {
            assert!(block.contains(&format!("data-i18n=\"{key}\"")));
        }
        assert!(block.contains("localStorage.getItem('jx_lang')"));
        assert!(block.contains("file:'File',edit:'Edit',view:'View',help:'Help'"));
        for action in [
            "new_chat",
            "run_mode",
            "server_config",
            "local_server",
            "reload",
            "check_update",
            "website",
            "about",
        ] {
            assert!(block.contains(&format!("data-act=\"{action}\"")));
        }
        for edit_action in ["undo", "redo", "cut", "copy", "paste", "selectAll"] {
            assert!(block.contains(&format!("data-edit=\"{edit_action}\"")));
        }
        assert_eq!(TB_MENU.matches("class=\"tb-menuGroup\"").count(), 4);
        assert!(block.contains("data-nav=\"back\""));
        assert!(block.contains("data-nav=\"forward\""));
        assert!(block.contains("<svg width=\"16\" height=\"16\""));
        assert!(block.contains("history.back()"));
        assert!(block.contains("history.forward()"));
        assert!(block.contains("aria-haspopup=\"menu\""));
        assert!(block.contains("aria-expanded=\"false\""));
        assert!(block.contains("role=\"menuitem\""));
        assert!(block.contains("ResizeObserver"));
        assert!(block.contains("event.ctrlKey&&!event.altKey&&key==='n'"));
        assert!(block.contains("event.key==='F11'"));
        assert!(block.contains("event.key==='ArrowDown'"));
        assert!(block.contains("inset:0 0 auto 0"));
        assert!(block.contains("background:var(--color-bg-layout)"));
        assert!(block.contains("class=\"tb-sidebarZone\""));
        assert!(block.contains("class=\"tb-mainChrome\""));
        assert!(!block.contains("class=\"tb-currentTab\""));
        assert!(!block.contains("class=\"tb-tabText\""));
        assert!(!block.contains(".jx-historyItem.active .jx-historyTitle"));
        assert!(!block.contains("resolveTabLabel"));
        assert!(block.contains("body{box-sizing:border-box!important;padding-top:0!important}"));
        assert!(block.contains(
            ".jx-appMainLayout{box-sizing:border-box!important;padding-top:var(--hugagent-desktop-titlebar-height)!important}"
        ));
        assert!(block.contains(".jx-brandRow{padding-top:50px!important}"));
        assert!(block.contains(".jx-miniRail{padding-top:48px!important}"));
        assert!(block.contains("sampleBg('.jx-sider')"));
        assert!(block.contains("sampleBg('.jx-primaryPane')"));
        assert!(!block.contains("border-bottom:1px"));
        assert!(block.find("tb-sidebarZone") < block.find("<nav class=\"tb-menu\""));
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
        // 左半幅必须逐字复刻 sidebar.css 里 .jx-sider 的配方，否则安全区和侧边栏会脱色；
        // 底色引令牌而不是写死，深色档才不会被 !important 摁回浅色。
        assert!(block.contains(
            "linear-gradient(90deg,color-mix(in srgb, var(--color-bg-gray) 72%, transparent)"
        ));
        assert!(block.contains("var(--color-bg-layout)!important"));
        assert!(block.contains(".jx-brandRow,.jx-miniRail{padding-top:0!important}"));
        assert!(block.contains("ResizeObserver"));
    }

    /// 每张壳页面都必须成对具备：`<head>` 里的主题引导脚本 + `:root[data-theme="dark"]` 覆盖块。
    /// 少了脚本 → 深色偏好读不到，页面恒亮；少了覆盖块 → 属性打上了也没有对应样式。
    #[test]
    fn every_shell_page_boots_and_defines_both_themes() {
        for (name, html) in [
            ("login", LOGIN_HTML),
            ("init", INIT_HTML),
            ("setup", SETUP_HTML),
            ("close-confirm", CLOSE_CONFIRM_HTML),
            ("server-config", SERVER_CONFIG_HTML),
        ] {
            assert!(
                html.contains(":root[data-theme=\"dark\"]"),
                "{name} 页缺少深色覆盖块"
            );
            let booted = with_theme_boot(html);
            assert!(booted.contains("hugagent_theme_mode"), "{name} 页没注入主题引导");
            // 引导必须早于 <style>，否则深色用户会先闪一帧白底
            assert!(
                booted.find("hugagent_theme_mode") < booted.find("<style>"),
                "{name} 页的主题引导排在样式之后，会闪白"
            );
            // 壳页面不许用媒体查询判深浅：那只认系统外观，会和手动三档打架
            assert!(
                !html.contains("prefers-color-scheme"),
                "{name} 页用了 prefers-color-scheme，应改用 data-theme"
            );
        }
    }

    /// 注进 SPA 文档的这两段样式，任何写死的颜色都会在深色档变成一块亮斑 ——
    /// 关闭键的平台约定红是唯一豁免（标了 dark-ok）。
    #[test]
    fn injected_shell_styles_carry_no_hardcoded_colors() {
        for css in [MAC_OFFSET_SPA, MAC_OFFSET_PAGE, TB_OFFSET_SPA, TB_OFFSET_PAGE] {
            assert!(
                !css.contains('#'),
                "注入 SPA 的偏移样式里出现了写死的颜色：{css}"
            );
        }
        let offenders: Vec<&str> = TB_CSS
            .lines()
            .filter(|line| line.contains('#') && !line.starts_with("#hugagent-titlebar"))
            .filter(|line| !line.contains("dark-ok"))
            .collect();
        // 关闭键那行紧跟在 dark-ok 注释之后，单独放行
        let offenders: Vec<&str> = offenders
            .into_iter()
            .filter(|line| !line.contains(".tb-windowButton.close:hover"))
            .collect();
        assert!(offenders.is_empty(), "标题栏样式里有写死的颜色：{offenders:?}");
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
        assert!(titlebar_block(TB_OFFSET_SPA).contains("--hugagent-desktop-titlebar-height:34px"));
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
