//! A3 · 一键自动更新。
//!
//! 解决「前端/壳一改就得重编译分发新客户端」的痛点：客户在「帮助 → 检查更新」或托盘触发，
//! 壳去后端拉更新清单（`<server_base>/api/v1/desktop/latest.json`）→ 本地用内置 pubkey 验签
//! → 下载安装包 → 安装 → 重启。**整包替换**，因此前端 dist（打进包里的）也一并更新。
//!
//! 关键设计：updater 的 endpoint **运行时**用当前 `server_base` 拼装，而非写死在
//! `tauri.conf.json`——这样一个 .exe 通吃多套私有化环境（server.json / 环境变量切后端，
//! 更新源自动跟着走），也避开了配置里 `{{...}}` 占位被 URL 编码的坑。
//!
//! 交互：**确认/结果**走原生对话框（`tauri-plugin-dialog`）——因为检查更新是从原生菜单/托盘
//! （Rust 侧）触发的，不经主 WebView，天然不受「远程源下 Tauri IPC 不可靠」影响。
//! **下载进度**走一个独立的原生进度窗（内联 HTML 的 data: URL，本地内容），进度由 Rust 侧
//! `eval` 直接推 DOM——不依赖主窗的远程 IPC，也无需给进度窗配任何 capability。

use tauri::{AppHandle, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;

use crate::{apply_display_zoom, brand, WEBVIEW_BROWSER_ARGS};

/// 进度窗内联页面。定义 `__set/__done/__fail` 三个函数，Rust 侧靠 `eval` 调用它们刷新界面。
/// 页面自带 DOM + 脚本，一加载完就绪，不存在「eval 早于 DOM 就绪」的竞态。
const PROGRESS_HTML: &str = r#"<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 html,body{margin:0;height:100%}
 body{font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;background:#f7f8fa;color:#1f2329;display:flex;align-items:center;justify-content:center}
 @media (prefers-color-scheme:dark){body{background:#1f2023;color:#e6e6e6}.track{background:#3a3c40 !important}}
 .card{width:100%;box-sizing:border-box;padding:22px 26px}
 .title{font-size:14px;font-weight:600;margin-bottom:16px;line-height:1.4}
 .track{height:10px;border-radius:6px;background:#e6e8eb;overflow:hidden}
 .fill{height:100%;width:0;border-radius:6px;background:linear-gradient(90deg,#2f6bff,#5b8dff);transition:width .15s ease}
 .fill.indet{width:40% !important;animation:slide 1.1s ease-in-out infinite}
 @keyframes slide{0%{margin-left:-40%}100%{margin-left:100%}}
 .meta{display:flex;justify-content:space-between;font-size:12px;color:#8a9099;margin-top:12px}
</style></head><body><div class="card">
 <div class="title" id="t">正在准备更新…</div>
 <div class="track"><div class="fill" id="f"></div></div>
 <div class="meta"><span id="p">0%</span><span id="s"></span></div>
</div><script>
 var f=document.getElementById('f'),p=document.getElementById('p'),s=document.getElementById('s'),t=document.getElementById('t');
 window.__set=function(pct,d,tt){
   if(pct<0){f.className='fill indet';p.textContent='';}
   else{f.className='fill';f.style.width=pct+'%';p.textContent=pct+'%';}
   t.textContent='正在下载新版本…';
   s.textContent=tt>0?(d.toFixed(1)+' / '+tt.toFixed(1)+' MB'):(d.toFixed(1)+' MB');
 };
 window.__done=function(msg){f.className='fill';f.style.width='100%';p.textContent='100%';t.textContent=msg||'下载完成，正在安装…';};
 window.__fail=function(msg){f.className='fill';f.style.background='#f5222d';t.textContent=msg||'更新失败';};
</script></body></html>"#;

/// 检查更新，用户确认后下载安装并重启。
///
/// - `server_base`：当前后端根地址，用于拼更新 endpoint。
/// - `silent`：为 true 时「已是最新」「检查失败」都不弹框（预留给启动静默检查）；
///   但「发现新版本」始终弹确认框——绝不静默自动安装。
pub fn check_and_install(app: AppHandle, server_base: String, silent: bool) {
    tauri::async_runtime::spawn(async move {
        let endpoint = format!(
            "{}/api/v1/desktop/latest.json",
            server_base.trim_end_matches('/')
        );
        let url = match url::Url::parse(&endpoint) {
            Ok(u) => u,
            Err(e) => return report_err(&app, silent, &format!("更新地址非法：{e}")),
        };

        let updater = match app.updater_builder().endpoints(vec![url]) {
            Ok(b) => match b.build() {
                Ok(u) => u,
                Err(e) => return report_err(&app, silent, &format!("初始化更新器失败：{e}")),
            },
            Err(e) => return report_err(&app, silent, &format!("初始化更新器失败：{e}")),
        };

        match updater.check().await {
            Ok(Some(update)) => {
                let ver = update.version.clone();
                let notes = update.body.clone().unwrap_or_default();
                let msg = if notes.trim().is_empty() {
                    format!("发现新版本 {ver}。\n\n是否现在下载并更新？更新完成后应用会自动重启。")
                } else {
                    format!("发现新版本 {ver}\n\n{notes}\n\n是否现在下载并更新？更新完成后应用会自动重启。")
                };
                let confirmed = app
                    .dialog()
                    .message(msg)
                    .title(format!("{} 更新", brand::NAME))
                    .kind(MessageDialogKind::Info)
                    .buttons(MessageDialogButtons::OkCancelCustom(
                        "立即更新".into(),
                        "稍后".into(),
                    ))
                    .blocking_show();
                if !confirmed {
                    return;
                }

                // 确认后弹出独立进度窗（本地内容，eval 驱动，无需 capability）。
                let progress_win = build_progress_window(&app);

                // 下载进度回调：累加已下载字节，按百分比变化节流刷新进度窗。
                let win_chunk = progress_win.clone();
                let win_finish = progress_win.clone();
                let mut downloaded: u64 = 0;
                let mut last_pct: i64 = -2;
                let result = update
                    .download_and_install(
                        move |chunk: usize, total: Option<u64>| {
                            downloaded += chunk as u64;
                            let Some(w) = win_chunk.as_ref() else { return };
                            let d_mb = downloaded as f64 / 1_048_576.0;
                            match total {
                                Some(t) if t > 0 => {
                                    let pct = (((downloaded as f64 / t as f64) * 100.0).floor()
                                        as i64)
                                        .clamp(0, 100);
                                    if pct != last_pct {
                                        last_pct = pct;
                                        let t_mb = t as f64 / 1_048_576.0;
                                        let _ = w.eval(format!(
                                            "window.__set&&window.__set({pct},{d_mb:.1},{t_mb:.1})"
                                        ));
                                    }
                                }
                                // 服务器没给 Content-Length → 不确定态，只报已下载量。
                                _ => {
                                    let _ = w.eval(format!(
                                        "window.__set&&window.__set(-1,{d_mb:.1},0)"
                                    ));
                                }
                            }
                        },
                        move || {
                            if let Some(w) = win_finish.as_ref() {
                                let _ = w.eval("window.__done&&window.__done()");
                            }
                        },
                    )
                    .await;

                match result {
                    Ok(_) => {
                        if let Some(w) = progress_win {
                            let _ = w.close();
                        }
                        app.dialog()
                            .message("更新已安装，点击确定重启应用。")
                            .title(format!("{} 更新", brand::NAME))
                            .kind(MessageDialogKind::Info)
                            .blocking_show();
                        app.restart();
                    }
                    Err(e) => {
                        if let Some(w) = progress_win {
                            let _ = w.close();
                        }
                        report_err(&app, false, &format!("下载或安装更新失败：{e}"));
                    }
                }
            }
            Ok(None) => {
                if !silent {
                    app.dialog()
                        .message("当前已是最新版本。")
                        .title(format!("{} 更新", brand::NAME))
                        .kind(MessageDialogKind::Info)
                        .blocking_show();
                }
            }
            Err(e) => report_err(&app, silent, &format!("检查更新失败：{e}")),
        }
    });
}

/// 在**主线程**上创建进度窗并把 handle 取回（窗口创建跨平台要求主线程）。
/// 失败/超时都返回 None——拿不到进度窗不影响更新本身继续跑（只是没进度显示）。
fn build_progress_window(app: &AppHandle) -> Option<WebviewWindow> {
    use percent_encoding::{utf8_percent_encode, NON_ALPHANUMERIC};

    let encoded = utf8_percent_encode(PROGRESS_HTML, NON_ALPHANUMERIC).to_string();
    let data_url = format!("data:text/html;charset=utf-8,{encoded}");
    let parsed = url::Url::parse(&data_url).ok()?;
    let title = format!("{} 更新", brand::NAME);

    let (tx, rx) = std::sync::mpsc::channel::<Option<WebviewWindow>>();
    let app2 = app.clone();
    let res = app.run_on_main_thread(move || {
        let win =
            WebviewWindowBuilder::new(&app2, "hug_updater_progress", WebviewUrl::External(parsed))
                .title(title)
                // WebView2 uses one browser environment per argument set. Keep this identical to the
                // main and auxiliary windows or this progress webview can fail to load after the DPI fix.
                .additional_browser_args(WEBVIEW_BROWSER_ARGS)
                .inner_size(460.0, 168.0)
                .resizable(false)
                .minimizable(false)
                .maximizable(false)
                .always_on_top(true)
                .center()
                .build()
                .map(|window| {
                    apply_display_zoom(&window);
                    window
                })
                .ok();
        let _ = tx.send(win);
    });
    if res.is_err() {
        return None;
    }
    rx.recv_timeout(std::time::Duration::from_secs(5))
        .ok()
        .flatten()
}

/// 弹错误框（silent 时静默）。
fn report_err(app: &AppHandle, silent: bool, msg: &str) {
    if silent {
        eprintln!("[update] {msg}");
        return;
    }
    app.dialog()
        .message(msg)
        .title(format!("{} 更新", brand::NAME))
        .kind(MessageDialogKind::Error)
        .blocking_show();
}
