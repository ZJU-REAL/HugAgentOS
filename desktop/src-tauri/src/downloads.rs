//! 页面内触发的下载（`<a download>`、附件响应）由 WebView 原生接管。
//!
//! macOS 的 WKWebView（wry）默认直接把文件落到 `~/Downloads`，用户没法选目录。
//! 这里在 `DownloadEvent::Requested` 时弹系统「存储为」面板：选了路径就改写目标，
//! 取消则放弃这次下载。上次选的目录在本次运行内记住，作为下一次的起点。
//!
//! Windows（WebView2 自带下载提示）与 Linux 保持各自 WebView 的默认行为。
use tauri::{webview::DownloadEvent, Runtime, Webview};

#[cfg(target_os = "macos")]
static LAST_DIR: std::sync::Mutex<Option<std::path::PathBuf>> = std::sync::Mutex::new(None);

pub(crate) fn handle<R: Runtime>(webview: Webview<R>, event: DownloadEvent<'_>) -> bool {
    match event {
        DownloadEvent::Requested { destination, .. } => ask_destination(&webview, destination),
        _ => true,
    }
}

#[cfg(target_os = "macos")]
fn ask_destination<R: Runtime>(webview: &Webview<R>, destination: &mut std::path::PathBuf) -> bool {
    // wry 给的默认路径形如 ~/Downloads/<建议文件名>，重名时会加 " (n)"；取它的文件名作默认值。
    let file_name = destination
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "download".into());
    let start_dir = LAST_DIR
        .lock()
        .ok()
        .and_then(|d| d.clone())
        .filter(|d| d.is_dir())
        .or_else(|| destination.parent().map(|p| p.to_path_buf()));

    let mut dialog = rfd::FileDialog::new().set_file_name(file_name);
    if let Some(dir) = start_dir {
        dialog = dialog.set_directory(dir);
    }
    let window = webview.window();
    dialog = dialog.set_parent(&window);

    // WKDownload 的目标路径回调在主线程同步等待结果，同步模态面板在这里是安全的。
    let Some(chosen) = dialog.save_file() else {
        return false;
    };
    // 面板已让用户确认覆盖；WKDownload 遇到已存在的目标会直接失败，先删掉旧文件。
    if chosen.is_file() && std::fs::remove_file(&chosen).is_err() {
        return false;
    }
    if let (Ok(mut last), Some(parent)) = (LAST_DIR.lock(), chosen.parent()) {
        *last = Some(parent.to_path_buf());
    }
    *destination = chosen;
    true
}

#[cfg(not(target_os = "macos"))]
fn ask_destination<R: Runtime>(
    _webview: &Webview<R>,
    _destination: &mut std::path::PathBuf,
) -> bool {
    true
}
