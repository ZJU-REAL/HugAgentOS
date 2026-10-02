use crate::brand;
use tauri::{AppHandle, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
const PROGRESS_HTML: &str = include_str!("../../../shared/update-progress.html");
/// Served only by the desktop's loopback proxy; no Tauri IPC or remote resources required.
pub(crate) async fn progress_page() -> axum::response::Html<&'static str> {
    axum::response::Html(PROGRESS_HTML)
}

/// Called from the updater worker, never a synchronous main-thread callback:
/// WebView2 creation can deadlock in Windows event handlers. Tauri dispatches the native work.
/// Use the existing loopback server: WebView2 does not reliably load data: navigations.
pub(super) fn build_progress_window(app: &AppHandle, port: u16) -> Result<WebviewWindow, String> {
    let parsed = url::Url::parse(&format!(
        "http://127.0.0.1:{port}/__desktop/update-progress"
    ))
    .map_err(|error| error.to_string())?;
    let expected_url = parsed.clone();
    let (ready, loaded) = std::sync::mpsc::channel();
    let window =
        WebviewWindowBuilder::new(app, "hug_updater_progress", WebviewUrl::External(parsed))
            .title(format!("{} 更新", brand::NAME))
            .on_navigation(move |url| {
                crate::navigation::is_shell_origin(url, port)
                    && url.path() == "/__desktop/update-progress"
            })
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
