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

mod cleanup;
mod download;
mod progress;
mod state;
#[cfg(test)]
mod tests;
use crate::brand;
use cleanup::sweep_stale_installers;
use progress::build_progress_window;
pub(crate) use progress::progress_page;
use state::{begin, set_available};
pub use state::{status, subscribe, UpdateStatus};
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;

pub fn version_message(current: &str, available: Option<&str>) -> String {
    match available {
        Some(next) => format!("当前版本：{current}\n发现新版本：{next}"),
        None => format!("当前版本：{current}\n当前平台暂无可用更新。"),
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
            .configure_client(|client| {
                client
                    .connect_timeout(std::time::Duration::from_secs(10))
                    .read_timeout(std::time::Duration::from_secs(45))
            })
            .endpoints(vec![url])
        {
            Ok(b) => match b.build() {
                Ok(u) => u,
                Err(e) => return report_err(&app, silent, &format!("初始化更新器失败：{e}")),
            },
            Err(e) => return report_err(&app, silent, &format!("初始化更新器失败：{e}")),
        };

        match updater.check().await {
            Ok(Some(mut update)) => {
                // The builder timeout also applies to the entire package download.
                // Full offline installers cannot be constrained to the 30s manifest budget.
                update.timeout = Some(std::time::Duration::from_secs(30 * 60));
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
                let mut last_progress =
                    std::time::Instant::now() - std::time::Duration::from_millis(100);
                let result = download::download_and_install(
                    &app,
                    update,
                    move |chunk: usize, total: Option<u64>| {
                        downloaded += chunk as u64;
                        let Some(w) = win_chunk.as_ref() else { return };
                        let d_mb = downloaded as f64 / 1_048_576.0;
                        match total {
                            Some(t) if t > 0 => {
                                let pct = (((downloaded as f64 / t as f64) * 100.0).floor() as i64)
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
                                if last_progress.elapsed() >= std::time::Duration::from_millis(100)
                                {
                                    last_progress = std::time::Instant::now();
                                    let _ = w.eval(format!(
                                        "window.__set&&window.__set(-1,{d_mb:.1},0)"
                                    ));
                                }
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
