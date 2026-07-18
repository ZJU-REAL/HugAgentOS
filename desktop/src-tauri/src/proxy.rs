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
    routing::{any, get},
    Router,
};
use tokio::sync::RwLock;
use tower_http::services::{ServeDir, ServeFile};

use crate::brand;

#[derive(Clone)]
pub struct ProxyState {
    pub http: reqwest::Client,
    /// 后端根地址（已去尾斜杠）。
    pub server_base: String,
    pub cookie_name: String,
    /// 当前 session token（None = 未登录；反代不注入 cookie）。
    pub token: Arc<RwLock<Option<String>>>,
}

/// 在 127.0.0.1 随机端口起反代，返回实际端口。axum serve 在后台 task 常驻。
pub async fn serve(state: ProxyState, web_dir: PathBuf) -> std::io::Result<u16> {
    let index = web_dir.join("index.html");
    // SPA：静态资源命中即返回，未命中回落 index.html（前端路由自行接管）。
    let serve_dir = ServeDir::new(&web_dir).fallback(ServeFile::new(index));

    let app = Router::new()
        .route("/__desktop/login", get(login_page))
        .route("/__desktop/close-confirm", get(close_confirm_page))
        .route("/__desktop/server-config", get(server_config_page))
        .route("/api", any(proxy_handler))
        .route("/api/*rest", any(proxy_handler))
        // 后端提供的上传资产（页面配置 logo/favicon、操作手册 PDF 等）也在后端、不在前端
        // dist 里——必须像 nginx 一样转发到后端，否则桌面端这些图片/文件 404 → 图裂。
        // 前端 dist 下没有 /docs 目录，转发不会抢占前端静态资源。
        .route("/docs/*rest", any(proxy_handler))
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
        Err(e) => {
            return (StatusCode::BAD_REQUEST, format!("读取请求体失败: {e}")).into_response()
        }
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
                Err(e) => {
                    (StatusCode::BAD_GATEWAY, format!("构造响应失败: {e}")).into_response()
                }
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
    Html(
        LOGIN_HTML
            .replace("HugAgentOS", brand::NAME)
            .replace("/icon.png", brand::LOGIN_LOGO_URL),
    )
}

/// 关闭主窗口时的自定义确认页（带「记住我的选择」勾选框）。按钮整页导航到
/// `/__desktop/close-decide?action=..&remember=..`，由确认窗的 Rust 导航守卫执行。
async fn close_confirm_page() -> Html<String> {
    Html(CLOSE_CONFIRM_HTML.replace("HugAgentOS", brand::NAME))
}

/// 「设置服务器地址」页（菜单栏「文件 → 设置服务器地址…」打开）。输入框预填当前后端地址，
/// 保存按钮整页导航到哨兵 `/__desktop/save-server?base=<encoded>`，由该窗的 Rust 导航守卫
/// 写回 server.json 并重启。同样不走 Tauri IPC。
async fn server_config_page(State(state): State<ProxyState>) -> Html<String> {
    Html(
        SERVER_CONFIG_HTML
            .replace("__CURRENT_BASE__", &html_escape(&state.server_base))
            .replace("HugAgentOS", brand::NAME),
    )
}

/// 极简 HTML 属性/文本转义，防止后端地址里的引号破坏 value。
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
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
    display:flex; flex-direction:column; justify-content:center;
    padding:24px 28px; -webkit-user-select:none; user-select:none;
  }
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
  <h1>服务器地址</h1>
  <p>设置本客户端连接的后端地址。保存后需重启客户端生效。</p>
  <label for="base">后端地址</label>
  <input id="base" type="text" placeholder="https://agent.example.gov.cn" value="__CURRENT_BASE__" spellcheck="false" />
  <div class="err" id="err"></div>
  <div class="btns">
    <button class="btn" onclick="window.close()">取消</button>
    <button class="btn primary" onclick="save()">保存并重启</button>
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
