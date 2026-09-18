//! A3 · 一键自动更新。
//!
//! 解决「前端/壳一改就得重编译分发新客户端」的痛点：客户在「帮助 → 检查更新」或托盘触发，
//! 壳去发布源拉更新清单（`<update_base>/api/v1/desktop/latest.json`）→ 本地用内置 pubkey 验签
//! → 下载安装包 → 安装 → 重启。**整包替换**，因此前端 dist（打进包里的）也一并更新。
//!
//! 关键设计：updater 的 endpoint **运行时**拼装，而非写死在 `tauri.conf.json`。远程模式
//! 默认跟随当前 `server_base`，本机服务模式则使用编译期发布源，避免向 127.0.0.1 查询安装包。
//!
//! 交互：**确认/结果**走原生对话框（`tauri-plugin-dialog`）——因为检查更新是从原生菜单/托盘
//! （Rust 侧）触发的，不经主 WebView，天然不受「远程源下 Tauri IPC 不可靠」影响。
//! **下载进度**走一个独立的原生进度窗（本机回环服务提供的内嵌 HTML），进度由 Rust 侧
//! `eval` 直接推 DOM——不依赖主窗的远程 IPC，也无需给进度窗配任何 capability。

use std::sync::OnceLock;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;
use tokio::sync::watch;

use crate::brand;

#[derive(Clone, Default, PartialEq, serde::Serialize)]
pub struct UpdateStatus {
    pub current_version: String,
    pub available_version: Option<String>,
    pub busy: bool,
}

static STATUS: OnceLock<watch::Sender<UpdateStatus>> = OnceLock::new();

fn state() -> &'static watch::Sender<UpdateStatus> {
    STATUS.get_or_init(|| {
        watch::channel(UpdateStatus {
            current_version: env!("CARGO_PKG_VERSION").into(),
            ..Default::default()
        })
        .0
    })
}

pub fn status() -> UpdateStatus {
    state().borrow().clone()
}

pub fn subscribe() -> watch::Receiver<UpdateStatus> {
    state().subscribe()
}

struct UpdateGuard;
impl Drop for UpdateGuard {
    fn drop(&mut self) {
        state().send_if_modified(|status| {
            if !status.busy {
                return false;
            }
            status.busy = false;
            true
        });
    }
}

fn begin() -> Option<UpdateGuard> {
    state()
        .send_if_modified(|status| {
            if status.busy {
                return false;
            }
            status.busy = true;
            true
        })
        .then(|| UpdateGuard)
}

fn set_available(version: Option<String>) {
    state().send_if_modified(|status| {
        if status.available_version == version {
            return false;
        }
        status.available_version = version;
        true
    });
}

pub fn version_message(current: &str, available: Option<&str>) -> String {
    match available {
        Some(next) => format!("当前版本：{current}\n发现新版本：{next}"),
        None => format!("当前版本：{current}\n当前平台暂无可用更新。"),
    }
}

/// 进度窗内联页面。定义 `__set/__done/__fail` 三个函数，Rust 侧靠 `eval` 调用它们刷新界面。
/// 创建窗口时等待页面加载完成，再开始下载，避免丢失第一帧或安装完成事件。
const PROGRESS_HTML: &str = r#"<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 /* dark-ok-begin: 独立更新进度页沿用系统深浅外观，无需主界面加载完成。 */
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
 /* dark-ok-end */
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
/// - `update_base`：桌面发布源根地址，用于拼更新 endpoint。
/// - `silent`：为 true 时「已是最新」「检查失败」都不弹框（预留给启动静默检查）；
///   发现新版只保存状态，由用户点击下载入口后确认安装。
/// 下载到的安装包在装完之后就没用了。
///
/// Windows 上装好之后本进程直接 `exit(0)` 把控制权交给安装器，代码走不到任何清理点；
/// updater 那边的临时目录又是显式 `keep()` 的。于是每更新一次，`%TEMP%` 里就永久多一份
/// 完整安装包——本产品的包里带着 Python 运行时，一份就上百 MB，用户永远不会去翻。
/// 下次检查更新时顺手把上次留下的清掉：那时它一定已经装完了。
/// updater 建临时目录时用的名字形状：`<应用名>-<版本>-updater-<随机后缀>`
/// （tauri-plugin-updater 的 `make_temp_dir`）。只认这个形状，别人的临时目录不碰。
const INSTALLER_TEMP_MARKER: &str = "-updater-";
/// 多久之前留下的就算上一轮的残骸。装机是分钟级的事，一小时足够宽。
const STALE_INSTALLER_AGE: std::time::Duration = std::time::Duration::from_secs(60 * 60);

fn sweep_stale_installers(app_name: &str) {
    let Ok(entries) = std::fs::read_dir(std::env::temp_dir()) else {
        return;
    };
    let prefix = format!("{}-", app_name.to_ascii_lowercase());
    let Some(keep_after) = std::time::SystemTime::now().checked_sub(STALE_INSTALLER_AGE) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_ascii_lowercase();
        if !name.starts_with(&prefix) || !name.contains(INSTALLER_TEMP_MARKER) {
            continue;
        }
        let stale = entry
            .metadata()
            .and_then(|meta| meta.modified())
            .map(|modified| modified < keep_after)
            .unwrap_or(false);
        if !stale {
            continue;
        }
        let path = entry.path();
        let _ = if path.is_dir() {
            std::fs::remove_dir_all(&path)
        } else {
            std::fs::remove_file(&path)
        };
    }
}

pub fn check_and_install(app: AppHandle, update_base: String, silent: bool) {
    let Some(guard) = begin() else {
        if !silent {
            app.dialog()
                .message(format!(
                    "当前版本：{}\n正在检查或安装更新，请稍候。",
                    status().current_version
                ))
                .title(format!("{} 更新", brand::NAME))
                .show(|_| {});
        }
        return;
    };
    tauri::async_runtime::spawn(async move {
        let _guard = guard;
        sweep_stale_installers(&app.package_info().name);
        let endpoint = format!(
            "{}/api/v1/desktop/latest.json?target={{{{target}}}}&arch={{{{arch}}}}",
            update_base.trim_end_matches('/')
        );
        let url = match url::Url::parse(&endpoint) {
            Ok(u) => u,
            Err(e) => return report_err(&app, silent, &format!("更新地址非法：{e}")),
        };

        let updater = match app
            .updater_builder()
            .timeout(std::time::Duration::from_secs(30))
            .endpoints(vec![url])
        {
            Ok(b) => match b.build() {
                Ok(u) => u,
                Err(e) => return report_err(&app, silent, &format!("初始化更新器失败：{e}")),
            },
            Err(e) => return report_err(&app, silent, &format!("初始化更新器失败：{e}")),
        };

        match updater.check().await {
            Ok(Some(update)) => {
                let ver = update.version.clone();
                set_available(Some(ver.clone()));
                if silent {
                    return;
                }
                let version_info = version_message(&status().current_version, Some(&ver));
                let notes = update.body.clone().unwrap_or_default();
                let msg = if notes.trim().is_empty() {
                    format!("{version_info}\n\n是否现在下载并更新？更新完成后应用会自动重启。")
                } else {
                    format!("{version_info}\n\n{notes}\n\n是否现在下载并更新？更新完成后应用会自动重启。")
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
                let window = match build_progress_window(&app, app.state::<crate::Shared>().port) {
                    Ok(window) => window,
                    Err(error) => {
                        return report_err(&app, false, &format!("无法打开更新进度卡片：{error}"))
                    }
                };
                let progress_win = Some(window);

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
                        // Windows 到不了这里：`download_and_install` 拉起 NSIS 安装器后
                        // 立即 `exit(0)`，进度窗随进程消失，重启由安装器的 `/R` 完成。
                        #[cfg(not(windows))]
                        {
                            if let Some(w) = progress_win {
                                let _ = w.close();
                            }
                            app.restart();
                        }
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
                set_available(None);
                if !silent {
                    app.dialog()
                        .message(version_message(&status().current_version, None))
                        .title(format!("{} 更新", brand::NAME))
                        .kind(MessageDialogKind::Info)
                        .blocking_show();
                }
            }
            Err(e) => report_err(&app, silent, &format!("检查更新失败：{e}")),
        }
    });
}

/// Served only by the desktop's loopback proxy; no Tauri IPC or remote resources required.
pub(crate) async fn progress_page() -> axum::response::Html<&'static str> {
    axum::response::Html(PROGRESS_HTML)
}

/// Called from the updater worker, never a synchronous main-thread callback:
/// WebView2 creation can deadlock in Windows event handlers. Tauri dispatches the native work.
/// Use the existing loopback server: WebView2 does not reliably load data: navigations.
fn build_progress_window(app: &AppHandle, port: u16) -> Result<WebviewWindow, String> {
    let parsed = url::Url::parse(&format!(
        "http://127.0.0.1:{port}/__desktop/update-progress"
    ))
    .map_err(|error| error.to_string())?;
    let expected_url = parsed.clone();
    let (ready, loaded) = std::sync::mpsc::channel();
    let window =
        WebviewWindowBuilder::new(app, "hug_updater_progress", WebviewUrl::External(parsed))
            .title(format!("{} 更新", brand::NAME))
            .on_page_load(move |_, payload| {
                if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished)
                    && payload.url() == &expected_url
                {
                    let _ = ready.send(());
                }
            })
            .inner_size(460.0, 168.0)
            .resizable(false)
            .minimizable(false)
            .maximizable(false)
            .closable(false)
            .always_on_top(true)
            .center()
            .build()
            .map_err(|error| format!("创建窗口失败：{error}"))?;
    if let Err(error) = loaded.recv_timeout(std::time::Duration::from_secs(15)) {
        let _ = window.close();
        return Err(format!("等待更新页面加载失败：{error}"));
    }
    Ok(window)
}

/// 弹错误框（silent 时静默）。
fn report_err(app: &AppHandle, silent: bool, msg: &str) {
    eprintln!("[update] {msg}");
    if silent {
        return;
    }
    app.dialog()
        .message(format!("当前版本：{}\n\n{msg}", status().current_version))
        .title(format!("{} 更新", brand::NAME))
        .kind(MessageDialogKind::Error)
        .blocking_show();
}

#[cfg(test)]
mod tests {
    #[test]
    fn update_attempts_preserve_offer_on_cancel_or_error_and_allow_retry() {
        use super::*;
        let mut events = subscribe();
        events.borrow_and_update();
        set_available(Some("2.0.0".into()));
        assert!(
            events.has_changed().unwrap(),
            "new version must wake the event stream"
        );
        events.borrow_and_update();
        set_available(Some("2.0.0".into()));
        assert!(
            !events.has_changed().unwrap(),
            "unchanged state must not wake the UI"
        );
        {
            let _attempt = begin().unwrap();
            assert!(status().busy);
            assert!(
                begin().is_none(),
                "double-click must not launch another update"
            );
        }
        assert!(!status().busy);
        assert_eq!(status().available_version.as_deref(), Some("2.0.0"));
        let retry = begin().expect("cancel/error releases the update gate");
        drop(retry);
        set_available(None);
        assert!(status().available_version.is_none());
        assert_eq!(
            version_message("1.0.2", None),
            "当前版本：1.0.2\n当前平台暂无可用更新。"
        );
        assert_eq!(
            version_message("1.0.2", Some("2.0.0")),
            "当前版本：1.0.2\n发现新版本：2.0.0"
        );
    }

    /// Run on a Windows build host with a real WebView2 runtime. This exercises the same
    /// window factory used after update confirmation without downloading or installing anything.
    #[cfg(target_os = "windows")]
    #[test]
    #[ignore = "requires a native Windows WebView2 session"]
    fn native_update_progress_window_loads_and_renders() {
        use super::*;
        use std::sync::{Arc, Mutex};
        let outcome = Arc::new(Mutex::new(None));
        let result = outcome.clone();
        let mut context = tauri::generate_context!();
        context.config_mut().identifier = "test.updater-progress.native".into();
        let app = tauri::Builder::default()
            .any_thread()
            .setup(move |app| {
                let app = app.handle().clone();
                std::thread::spawn(move || {
                    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
                    let port = listener.local_addr().unwrap().port();
                    listener.set_nonblocking(true).unwrap();
                    let server = tauri::async_runtime::spawn(async move {
                        let listener = tokio::net::TcpListener::from_std(listener).unwrap();
                        let router = axum::Router::new().route(
                            "/__desktop/update-progress",
                            axum::routing::get(progress_page),
                        );
                        axum::serve(listener, router).await.unwrap();
                    });
                    let check = (|| -> Result<(), String> {
                        let window = build_progress_window(&app, port)?;
                        window.eval(
                            "window.__set(42,4.2,10);                              if(document.getElementById('p').textContent==='42%' &&                                 typeof window.__done==='function' && typeof window.__fail==='function')                              window.location.replace('about:blank#progress-rendered');"
                        ).map_err(|error| error.to_string())?;
                        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
                        while std::time::Instant::now() < deadline {
                            if window.url().map_err(|error| error.to_string())?.fragment()
                                == Some("progress-rendered") {
                                let _ = window.close();
                                return Ok(());
                            }
                            std::thread::sleep(std::time::Duration::from_millis(50));
                        }
                        let _ = window.close();
                        Err("Progress JavaScript did not render the download percentage".into())
                    })();
                    server.abort();
                    let code = if check.is_ok() { 0 } else { 1 };
                    *result.lock().unwrap() = Some(check);
                    app.exit(code);
                });
                Ok(())
            })
            .build(context)
            .expect("create native test app");
        app.run_return(|_, _| {});
        outcome
            .lock()
            .unwrap()
            .take()
            .expect("native check completed")
            .expect("update progress window must load and render");
    }

    #[test]
    fn windows_update_is_unattended() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert_eq!(
            config["plugins"]["updater"]["windows"]["installMode"],
            "passive"
        );
    }
}
