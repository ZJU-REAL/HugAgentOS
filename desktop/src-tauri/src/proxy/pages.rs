use super::transport::is_same_origin;
use super::window_chrome::platform_titlebar_block;
use super::ProxyState;
use crate::brand;
use crate::config::ProvisionMode;
use axum::extract::Path;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::StatusCode;
use axum::response::Html;
/// 未登录时窗口加载的登录卡片页。默认「初始态」——展示「开始使用」按钮，等用户点击
/// 才经 Tauri 命令 open_login 拉起系统浏览器；带 `?waiting=1` 时（会话过期兜底）直接进
/// 等待态。启动与退出登录都落到这张卡片，避免直接跳外链或白屏。
pub(super) async fn login_page() -> Html<String> {
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
pub(super) async fn close_confirm_page() -> Html<String> {
    Html(with_theme_boot(
        &CLOSE_CONFIRM_HTML.replace("HugAgentOS", brand::NAME),
    ))
}

/// 把标题栏的缩放动作转给壳层。动作 id 与原生菜单共用，这里只做白名单校验，
/// 「哪个 id 对应哪一档」只在 `actions::dispatch_for_window` 定义一处。
pub(super) async fn zoom_action(
    State(state): State<ProxyState>,
    Path(action): Path<String>,
    headers: HeaderMap,
) -> StatusCode {
    if !is_same_origin(
        &headers,
        state.bound_port.load(std::sync::atomic::Ordering::Relaxed),
    ) {
        return StatusCode::FORBIDDEN;
    }
    if !matches!(action.as_str(), "zoom_in" | "zoom_out" | "zoom_reset") {
        return StatusCode::BAD_REQUEST;
    }
    let _ = state.zoom_tx.send(action);
    StatusCode::NO_CONTENT
}

/// 「设置服务器地址」页（菜单栏「文件 → 设置服务器地址…」打开）。输入框预填当前后端地址，
/// 保存按钮整页导航到哨兵 `/__desktop/save-server?base=<encoded>`，由主窗口的 Rust 导航守卫
/// 写回 server.json 并重启。同样不走 Tauri IPC。
pub(super) async fn server_config_page(State(state): State<ProxyState>) -> Html<String> {
    let html = SERVER_CONFIG_HTML
        .replace("__CURRENT_BASE__", &html_escape(&state.server_base))
        .replace("HugAgentOS", brand::NAME);
    Html(inject_after_body(
        &with_theme_boot(&html),
        &platform_titlebar_block(false),
    ))
}

/// 后端不可达或用户在安装器选择本机服务时展示的一体化部署页。
pub(super) async fn setup_page(State(state): State<ProxyState>) -> Html<String> {
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

/// 初始化页（首启时展示）。
///
/// 构建开关 `brand::HYBRID_ONLY` 决定展示哪一张：
/// - 关（默认）：「运行模式选择」页，下拉选本机 / 云端 / 双模式，含云端的形态展开地址输入。
/// - 开：仅交付混合模式，不问模式也不问地址，只留一个「开始初始化」的确认动作。
///
/// 两张页面都整页导航到哨兵 `/__desktop/provision`，由主窗口的 Rust 导航守卫落盘。
/// `manage=1` 时是「稍后更改运行模式」入口（仅混合模式的包没有这个入口）。
pub(super) async fn init_page(State(state): State<ProxyState>) -> Html<String> {
    if brand::HYBRID_ONLY {
        return fixed_init_page();
    }
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

/// 仅交付混合模式的构建用的初始化页：固定「本机 + 云端」，只有一个确认动作。
/// 运行形态已由壳在内存里备好，确认后同一窗口直接进安装进度页，不重启应用。
pub(super) fn fixed_init_page() -> Html<String> {
    let html = INIT_FIXED_HTML
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
        .replace("__TAGLINE__", brand::TAGLINE)
        .replace("HugAgentOS", brand::NAME);
    Html(inject_after_body(
        &with_theme_boot(&html),
        &platform_titlebar_block(false),
    ))
}

/// 极简 HTML 属性/文本转义，防止后端地址里的引号破坏 value。
pub(super) fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
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
pub(super) const THEME_BOOT_JS: &str = include_str!("../../../shared/pages/theme-boot-js.html");

/// 把品牌的主题 key 落进引导脚本模板。
pub(super) fn theme_boot_js() -> String {
    THEME_BOOT_JS.replace("__THEME_STORAGE_KEY__", brand::THEME_STORAGE_KEY)
}

/// 把主题引导脚本插进 `<head>` 最前面——必须**早于任何样式**执行，否则深色用户会先看到
/// 一帧白底再翻黑。
pub(super) fn with_theme_boot(html: &str) -> String {
    pub(super) const HEAD: &str = "<head>";
    let boot = theme_boot_js();
    match html.find(HEAD) {
        Some(index) => {
            let at = index + HEAD.len();
            let mut output = String::with_capacity(html.len() + boot.len());
            output.push_str(&html[..at]);
            output.push_str(&boot);
            output.push_str(&html[at..]);
            output
        }
        None => format!("{boot}{html}"),
    }
}

pub(super) fn inject_after_body(html: &str, block: &str) -> String {
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

pub(super) const LOGIN_HTML: &str = include_str!("../../../shared/pages/login-html.html");

pub(super) const INIT_HTML: &str = include_str!("../../../shared/pages/init-html.html");

/// 仅交付混合模式的构建用的初始化页。与安装进度页共用同一套动画视觉（光晕 + 轨道 +
/// 浮动核心），确认后两页之间只是内容切换，观感上是同一个初始化流程。
pub(super) const INIT_FIXED_HTML: &str = include_str!("../../../shared/pages/init-fixed-html.html");

pub(super) const SETUP_HTML: &str = include_str!("../../../shared/pages/setup-html.html");

pub(super) const CLOSE_CONFIRM_HTML: &str =
    include_str!("../../../shared/pages/close-confirm-html.html");

pub(super) const SERVER_CONFIG_HTML: &str =
    include_str!("../../../shared/pages/server-config-html.html");
