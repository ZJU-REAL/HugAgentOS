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
}

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
        .route("/__desktop/setup", get(setup_page))
        .route("/__desktop/setup/status", get(setup_status))
        .route("/__desktop/setup/install", post(start_local_install))
        .route("/api", any(proxy_handler))
        .route("/api/*rest", any(proxy_handler))
        // 后端提供的上传资产（页面配置 logo/favicon、操作手册 PDF 等）也在后端、不在前端
        // dist 里——必须像 nginx 一样转发到后端，否则桌面端这些图片/文件 404 → 图裂。
        // 前端 dist 下没有 /docs 目录，转发不会抢占前端静态资源。
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
    let target = format!("{}{}", state.server_base, path_q);

    // 收齐请求体（上传等）。下游用 reqwest 重发。
    let body_bytes = match axum::body::to_bytes(body, usize::MAX).await {
        Ok(b) => b,
        Err(e) => return (StatusCode::BAD_REQUEST, format!("读取请求体失败: {e}")).into_response(),
    };

    let mut rb = state.http.request(method, &target);

    // 透传请求头，但剔除 hop-by-hop / 由我们重写的头。
    // http 的 HeaderName 已规范化为小写，直接 match 即可，无需再 to_ascii_lowercase。
    for (name, value) in headers.iter() {
        match name.as_str() {
            // host 让 reqwest 按目标地址重置；cookie 我们重新注入；
            // accept-encoding 去掉以拿 identity（避免转发压缩流时还要解码）；
            // content-length / connection 交给 reqwest / axum 自管。
            "host" | "cookie" | "accept-encoding" | "content-length" | "connection" => continue,
            _ => {
                rb = rb.header(name, value);
            }
        }
    }

    // 注入会话 cookie（已登录时）——这是整套桌面鉴权的关键一笔。
    if let Some(tok) = state.token.read().await.clone() {
        rb = rb.header(
            reqwest::header::COOKIE,
            format!("{}={}", state.cookie_name, tok),
        );
    }

    if !body_bytes.is_empty() {
        // body_bytes 已是 Bytes，直接交给 reqwest（避免再 to_vec 复制一份请求体）。
        rb = rb.body(body_bytes);
    }

    match rb.send().await {
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
            "__LOCAL_SUPPORTED__",
            if cfg!(target_os = "windows") {
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
}

async fn setup_status(State(state): State<ProxyState>) -> Json<SetupStatus> {
    Json(SetupStatus {
        service: state.local_server.snapshot().await,
        active_local: state.active_local,
        current_server_base: state.server_base,
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
// 主窗口关闭系统 decorations，避免「系统标题栏 + 原生菜单栏」占两行。这里把产品名、
// 文件/编辑/视图/帮助和窗口控制放进同一行。全部动作走导航哨兵，由 lib.rs 拦截执行，
// 不依赖远程源下不稳定的 Tauri IPC。

const TITLEBAR_HEIGHT: u8 = 36;
const TB_OFFSET_SPA: &str =
    "body{box-sizing:border-box!important;padding-top:36px!important}.jx-appLoading{height:100%!important}";
const TB_OFFSET_PAGE: &str = "body{box-sizing:border-box!important;padding-top:36px!important}";

const MAC_TITLEBAR_HEIGHT: u8 = 52;
const MAC_OFFSET_SPA: &str =
    "body{box-sizing:border-box!important;padding-top:52px!important}.jx-appLoading{height:100%!important}";
const MAC_OFFSET_PAGE: &str = "body{box-sizing:border-box!important;padding-top:52px!important}";

const TB_CSS: &str = r##"
#hugagent-titlebar{position:fixed;inset:0 0 auto 0;height:36px;z-index:2147483647;display:flex;align-items:center;background:#F7F8FA;border-bottom:1px solid #E5E9EF;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#30343B}
#hugagent-titlebar *{box-sizing:border-box}
#hugagent-titlebar .tb-left{display:flex;align-items:center;height:100%;min-width:0;padding-left:10px}
#hugagent-titlebar .tb-logo{width:17px;height:17px;border-radius:4px;margin-right:7px;object-fit:cover}
#hugagent-titlebar .tb-name{font-size:12.5px;font-weight:650;white-space:nowrap;margin-right:5px;-webkit-user-select:none;user-select:none}
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

// macOS keeps the application menu in the system menu bar and the native
// traffic-light controls on the left. The in-window strip only exposes app
// actions, matching the compact icon-toolbar pattern used by modern Mac apps.
const MAC_TB_CSS: &str = r##"
#hugagent-mac-titlebar{position:fixed;inset:0 0 auto 0;height:52px;z-index:2147483647;display:flex;align-items:center;padding:0 12px 0 82px;background:rgba(247,247,246,.86);border-bottom:1px solid rgba(28,28,28,.09);backdrop-filter:saturate(180%) blur(22px);-webkit-backdrop-filter:saturate(180%) blur(22px);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;color:#252522;-webkit-user-select:none;user-select:none}
#hugagent-mac-titlebar *{box-sizing:border-box}
#hugagent-mac-titlebar .mac-brand{display:flex;align-items:center;min-width:0;gap:8px;font-size:13px;font-weight:590;letter-spacing:-.01em;color:#3a3935}
#hugagent-mac-titlebar .mac-logo{width:18px;height:18px;border-radius:5px;object-fit:cover;box-shadow:0 1px 2px rgba(0,0,0,.08)}
#hugagent-mac-titlebar .mac-spacer{flex:1;height:100%;min-width:24px}
#hugagent-mac-titlebar .mac-actions{display:flex;align-items:center;gap:7px}
#hugagent-mac-titlebar .mac-toolButton{width:30px;height:30px;padding:0;border:1px solid rgba(28,28,28,.1);border-radius:8px;background:rgba(255,255,255,.64);color:#45443f;display:flex;align-items:center;justify-content:center;box-shadow:0 1px 1px rgba(0,0,0,.03);cursor:default;transition:background .12s ease,border-color .12s ease,transform .08s ease}
#hugagent-mac-titlebar .mac-toolButton:hover{background:rgba(255,255,255,.96);border-color:rgba(28,28,28,.16)}
#hugagent-mac-titlebar .mac-toolButton:active{background:rgba(232,231,227,.92);transform:scale(.96)}
@media(prefers-color-scheme:dark){#hugagent-mac-titlebar{background:rgba(38,38,36,.88);border-bottom-color:rgba(255,255,255,.09);color:#f2f2ef}#hugagent-mac-titlebar .mac-brand{color:#e7e6e1}#hugagent-mac-titlebar .mac-toolButton{background:rgba(255,255,255,.07);border-color:rgba(255,255,255,.12);color:#e9e8e3}#hugagent-mac-titlebar .mac-toolButton:hover{background:rgba(255,255,255,.13);border-color:rgba(255,255,255,.18)}}
"##;

const MAC_TB_JS: &str = r##"(function(){
var bar=document.getElementById('hugagent-mac-titlebar');if(!bar)return;
if(new URLSearchParams(location.search).get('quickask')==='1'){
  bar.remove();var style=document.getElementById('hugagent-titlebar-style');if(style)style.remove();return;
}
function sentinel(path){window.location.href=path;}
bar.querySelectorAll('[data-act]').forEach(function(item){
  item.addEventListener('mousedown',function(event){event.preventDefault();});
  item.addEventListener('click',function(event){event.stopPropagation();sentinel('/__desktop/menu?action='+encodeURIComponent(item.dataset.act));});
});
bar.addEventListener('mousedown',function(event){
  if(event.button!==0||(event.target instanceof Element&&event.target.closest('button')))return;
  sentinel('/__desktop/win?action=drag');
});
bar.addEventListener('dblclick',function(event){
  if(event.target instanceof Element&&event.target.closest('button'))return;
  sentinel('/__desktop/win?action=toggle-maximize');
});
})();"##;

fn titlebar_block(offset_css: &str) -> String {
    format!(
        "<style id=\"hugagent-titlebar-style\">{css}{offset}</style>\
<header id=\"hugagent-titlebar\" data-height=\"{height}\">\
<div class=\"tb-left\"><img class=\"tb-logo\" src=\"{logo}\" alt=\"\" onerror=\"this.style.display='none'\"/><span class=\"tb-name\">{name}</span>{menu}</div>\
<div class=\"tb-spacer\"></div>{controls}</header><script>{script}</script>",
        css = TB_CSS,
        offset = offset_css,
        height = TITLEBAR_HEIGHT,
        logo = brand::LOGIN_LOGO_URL,
        name = brand::NAME,
        menu = TB_MENU,
        controls = TB_CONTROLS,
        script = TB_JS,
    )
}

fn mac_titlebar_block(offset_css: &str) -> String {
    format!(
        "<style id=\"hugagent-titlebar-style\">{css}{offset}</style>\
<header id=\"hugagent-mac-titlebar\" data-height=\"{height}\">\
<div class=\"mac-brand\"><img class=\"mac-logo\" src=\"{logo}\" alt=\"\" onerror=\"this.style.display='none'\"/><span>{name}</span></div>\
<div class=\"mac-spacer\"></div><div class=\"mac-actions\">\
<button class=\"mac-toolButton\" type=\"button\" data-act=\"new_chat\" aria-label=\"新建对话\" title=\"新建对话\"><svg width=\"15\" height=\"15\" viewBox=\"0 0 16 16\" fill=\"none\"><path d=\"M9.8 2.5H4.2A1.7 1.7 0 0 0 2.5 4.2v7.6a1.7 1.7 0 0 0 1.7 1.7h7.6a1.7 1.7 0 0 0 1.7-1.7V6.2M8 8l5.2-5.2M10.5 2.5h3v3\" stroke=\"currentColor\" stroke-width=\"1.35\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/></svg></button>\
<button class=\"mac-toolButton\" type=\"button\" data-act=\"server_config\" aria-label=\"服务设置\" title=\"服务设置\"><svg width=\"15\" height=\"15\" viewBox=\"0 0 16 16\" fill=\"none\"><path d=\"M3 4h10M5.5 8h5M4 12h8\" stroke=\"currentColor\" stroke-width=\"1.35\" stroke-linecap=\"round\"/><circle cx=\"10.5\" cy=\"4\" r=\"1.25\" fill=\"currentColor\"/><circle cx=\"6.5\" cy=\"8\" r=\"1.25\" fill=\"currentColor\"/><circle cx=\"9.5\" cy=\"12\" r=\"1.25\" fill=\"currentColor\"/></svg></button>\
</div></header><script>{script}</script>",
        css = MAC_TB_CSS,
        offset = offset_css,
        height = MAC_TITLEBAR_HEIGHT,
        logo = brand::LOGIN_LOGO_URL,
        name = brand::NAME,
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
    match html.find("<body>") {
        Some(position) => {
            let index = position + "<body>".len();
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
    --primary:#126DFF; --primary-hover:#3C87FF; --primary-active:#0862F3;
    --text:#262626; --text-2:#808080; --text-3:#B3B3B3; --border:#E8EBF0; --card:#fff;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    color:var(--text);
    background:
      radial-gradient(1200px 560px at 50% -12%, #E5EFFF 0%, rgba(229,239,255,0) 62%),
      linear-gradient(180deg,#FBFCFE 0%, #EEF2F8 100%);
    display:flex; align-items:center; justify-content:center;
    -webkit-user-select:none; user-select:none;
  }
  .card{
    width:392px; padding:46px 42px 30px; text-align:center;
    background:var(--card); border:1px solid var(--border); border-radius:22px;
    box-shadow:0 24px 70px rgba(18,109,255,.12), 0 2px 10px rgba(15,23,42,.05);
  }
  .logo{
    width:74px;height:74px;border-radius:19px;margin:0 auto 20px;display:block;
    box-shadow:0 10px 24px rgba(18,109,255,.24);
  }
  h1{font-size:22px;font-weight:600;margin:0 0 8px;letter-spacing:.4px}
  .sub{font-size:13.5px;color:var(--text-2);margin:0 0 32px;line-height:1.75}
  .btn{
    width:100%;height:48px;border:none;border-radius:13px;cursor:pointer;
    background:var(--primary);color:#fff;font-size:15px;font-weight:500;
    display:inline-flex;align-items:center;justify-content:center;gap:9px;
    transition:background .15s ease, transform .04s ease, box-shadow .15s ease;
    box-shadow:0 6px 16px rgba(18,109,255,.28);
  }
  .btn:hover{background:var(--primary-hover)}
  .btn:active{background:var(--primary-active);transform:translateY(1px)}
  .hint{margin-top:16px;font-size:12.5px;color:var(--text-3)}
  .links{margin-top:8px;font-size:13px}
  .links a{color:var(--primary);text-decoration:none;cursor:pointer;margin:0 8px}
  .links a:hover{text-decoration:underline}
  .spin{width:36px;height:36px;margin:8px auto 20px;border:3px solid #E8EBF0;
    border-top-color:var(--primary);border-radius:50%;animation:r .9s linear infinite}
  @keyframes r{to{transform:rotate(360deg)}}
  .foot{margin-top:30px;padding-top:18px;border-top:1px solid #F0F2F6;
    font-size:12px;color:var(--text-3)}
  .hidden{display:none}
</style>
</head>
<body>
  <div class="card">
    <img class="logo" src="/icon.png" alt="HugAgentOS" onerror="this.style.display='none'"/>
    <!-- 初始态：等待用户点击登录 -->
    <div id="idle">
      <h1>HugAgentOS</h1>
      <p class="sub">在系统浏览器中安全登录后<br/>自动返回桌面客户端继续使用</p>
      <button class="btn" onclick="startLogin()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M14 3h5a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-5M10 17l5-5-5-5M15 12H3"
            stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        开始使用
      </button>
      <div class="hint">点击后将在系统默认浏览器中打开登录页面</div>
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
    <div class="foot">HugAgentOS · 安全登录</div>
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

const SETUP_HTML: &str = r##"<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>服务设置 · HugAgentOS</title>
<style>
  :root{
    --primary:#126DFF;--primary-hover:#3C87FF;--text:#1F2937;--text-2:#64748B;
    --border:#E5EAF1;--bg:#F4F7FB;--ok:#0F9D68;--danger:#D4380D;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    color:var(--text);background:radial-gradient(900px 440px at 50% -15%,#DCE9FF 0,transparent 68%),var(--bg);
    display:flex;align-items:center;justify-content:center;padding:28px}
  .shell{width:min(760px,100%);background:#fff;border:1px solid var(--border);border-radius:22px;
    box-shadow:0 24px 70px rgba(30,64,175,.12);overflow:hidden}
  .head{padding:28px 32px 22px;border-bottom:1px solid #EEF1F5}
  .brand{font-size:13px;color:var(--primary);font-weight:650;margin-bottom:8px}
  h1{font-size:24px;margin:0 0 8px}.lead{margin:0;color:var(--text-2);font-size:14px;line-height:1.7}
  .body{padding:24px 32px 30px}.choices{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .choice{border:1px solid var(--border);border-radius:16px;padding:20px;background:#fff}
  .choice.recommended{border-color:#B8D2FF;background:#F8FBFF}
  .tag{display:inline-flex;padding:3px 8px;border-radius:999px;background:#E8F1FF;color:var(--primary);
    font-size:11px;font-weight:650;margin-bottom:10px}
  h2{font-size:17px;margin:0 0 8px}.desc{font-size:13px;color:var(--text-2);line-height:1.7;min-height:66px}
  .btn{height:42px;border-radius:10px;padding:0 17px;border:1px solid var(--border);background:#fff;
    color:var(--text);font-size:14px;font-weight:550;cursor:pointer;transition:.15s}
  .btn:hover{background:#F8FAFC}.btn.primary{width:100%;border:0;background:var(--primary);color:#fff;
    box-shadow:0 5px 14px rgba(18,109,255,.25)}.btn.primary:hover{background:var(--primary-hover)}
  .btn:disabled{opacity:.55;cursor:not-allowed}.actions{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
  .progress-wrap{display:none;margin-top:22px;border-top:1px solid #EEF1F5;padding-top:20px}
  .progress-head{display:flex;justify-content:space-between;gap:12px;font-size:13px;margin-bottom:9px}
  .progress{height:8px;border-radius:999px;background:#E9EEF5;overflow:hidden}.bar{height:100%;width:0;
    background:linear-gradient(90deg,#126DFF,#5B9BFF);transition:width .3s ease}
  .message{color:var(--text-2)}.percent{font-variant-numeric:tabular-nums;color:var(--primary)}
  .log{margin-top:12px;height:128px;overflow:auto;padding:11px 12px;border-radius:10px;background:#101827;
    color:#D5DEEC;font:11.5px/1.55 Consolas,"SFMono-Regular",monospace;white-space:pre-wrap;word-break:break-all}
  .error{display:none;color:var(--danger);font-size:13px;margin-top:12px}.ready{color:var(--ok)}
  .current{margin-top:17px;color:#94A3B8;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  body.platform-macos{background:#ECECEA;padding-left:22px;padding-right:22px}
  body.platform-macos .shell{max-width:720px;border-color:rgba(28,28,28,.1);border-radius:16px;box-shadow:0 22px 55px rgba(0,0,0,.12)}
  body.platform-macos .head{padding-top:25px;background:rgba(252,252,250,.92)}
  body.platform-macos .body{background:#FAFAF8}
  body.platform-macos .choice{border-color:rgba(28,28,28,.1);border-radius:13px;box-shadow:0 1px 1px rgba(0,0,0,.025)}
  body.platform-macos .choice.recommended{border-color:rgba(18,109,255,.3);background:#F7FAFF}
  body.platform-macos .btn{border-radius:9px;box-shadow:none}
  @media(max-width:640px){.choices{grid-template-columns:1fr}.desc{min-height:0}.head,.body{padding-left:22px;padding-right:22px}}
</style>
</head>
<body class="platform-__PLATFORM__">
  <main class="shell">
    <header class="head">
      <div class="brand">HugAgentOS 桌面端</div>
      <h1>选择服务运行方式</h1>
      <p class="lead" id="lead">当前服务不可用。你可以在本机一键安装单用户服务，也可以连接已经部署的团队服务器。</p>
    </header>
    <section class="body">
      <div class="choices">
        <article class="choice recommended">
          <span class="tag">推荐 · 无需 Docker</span>
          <h2>安装本机服务</h2>
          <p class="desc">自动创建独立 Python 环境并启动内置 CE 单机服务。数据只保存在当前电脑，适合个人使用。</p>
          <button class="btn primary" id="install" onclick="installLocal()">一键安装并启动</button>
          <div class="actions" id="readyActions" style="display:none">
            <button class="btn primary" id="readyButton" onclick="finishReady()">切换到本机服务</button>
          </div>
        </article>
        <article class="choice">
          <span class="tag">团队 / 私有化</span>
          <h2>连接已有服务器</h2>
          <p class="desc">继续使用组织已经部署的 HugAgentOS 服务。填写 HTTP 或 HTTPS 后端地址即可。</p>
          <div class="actions">
            <button class="btn" onclick="connectServer()">设置服务器地址</button>
            <button class="btn" onclick="retry()">重试当前地址</button>
          </div>
        </article>
      </div>
      <div class="progress-wrap" id="progressWrap">
        <div class="progress-head"><span class="message" id="message">准备安装…</span><span class="percent" id="percent">0%</span></div>
        <div class="progress"><div class="bar" id="bar"></div></div>
        <pre class="log" id="log">等待安装日志…</pre>
        <div class="error" id="error"></div>
      </div>
      <div class="current">当前地址：__CURRENT_BASE__</div>
    </section>
  </main>
<script>
  var manage = new URLSearchParams(location.search).get('manage') === '1';
  var activeLocal = __ACTIVE_LOCAL__;
  var localSupported = __LOCAL_SUPPORTED__;
  var installing = false;
  var pollTimer = null;
  if(manage){document.getElementById('lead').textContent='查看或切换本机单用户服务，也可以继续连接已部署的团队服务器。';}
  function sentinel(path){ window.location.href = path; }
  function connectServer(){ sentinel('/__desktop/connect-server'); }
  function retry(){ sentinel('/__desktop/retry-server'); }
  function activateLocal(){ sentinel('/__desktop/activate-local'); }
  function finishReady(){ if(activeLocal){location.replace('/');}else{activateLocal();} }
  async function installLocal(){
    if(!localSupported){showError('当前安装包暂不支持在此系统一键部署本机服务。');return;}
    installing = true;
    var button=document.getElementById('install');
    button.disabled=true;button.textContent='正在启动安装…';
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
    var button=document.getElementById('install');button.disabled=false;button.textContent='重试安装';
  }
  async function poll(){
    if(pollTimer){clearTimeout(pollTimer);pollTimer=null;}
    try{
      var response=await fetch('/__desktop/setup/status',{cache:'no-store'});
      var s=await response.json();
      activeLocal=!!s.active_local;
      var active=['installing','starting','ready','error'].includes(s.phase) || installing;
      if(active) document.getElementById('progressWrap').style.display='block';
      document.getElementById('bar').style.width=(s.progress||0)+'%';
      document.getElementById('percent').textContent=(s.progress||0)+'%';
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
          document.getElementById('message').textContent='安装完成，正在切换到本机服务…';
          setTimeout(activateLocal,350);return;
        }
        document.getElementById('readyButton').textContent=s.active_local?'返回应用':'切换到本机服务';
        document.getElementById('readyActions').style.display='block';
        return;
      }
      if(s.phase==='installing'||s.phase==='starting'){
        installing=true;var button=document.getElementById('install');button.disabled=true;
        button.textContent=s.phase==='starting'?'正在启动服务…':'正在安装…';
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
    fn windows_titlebar_keeps_product_and_four_menus_on_one_row() {
        let block = titlebar_block(TB_OFFSET_SPA);
        assert!(block.contains("tb-name"));
        for label in ["文件", "编辑", "视图", "帮助"] {
            assert!(block.contains(label));
        }
        assert!(block.contains("data-win=\"minimize\""));
        assert!(block.contains("data-win=\"close\""));
    }

    #[test]
    fn mac_titlebar_keeps_native_window_controls_and_uses_app_actions() {
        let block = mac_titlebar_block(MAC_OFFSET_SPA);
        assert!(block.contains("hugagent-mac-titlebar"));
        assert!(block.contains("data-act=\"new_chat\""));
        assert!(block.contains("data-act=\"server_config\""));
        assert!(!block.contains("data-win=\"minimize\""));
        assert!(!block.contains("data-win=\"close\""));
        assert!(!block.contains("tb-menuLabel"));
    }

    #[test]
    fn setup_install_starts_in_place_before_switching_modes() {
        assert!(SETUP_HTML.contains("fetch('/__desktop/setup/install'"));
        assert!(SETUP_HTML.contains("正在启动安装"));
        assert!(!SETUP_HTML.contains("if(!activeLocal){ activateLocal(); return; }"));
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
}
