//! 小更新包不携带 Python 运行时：依赖指纹变化时，在停掉旧服务之前从发布源下载。
//!
//! 期望的 sha256 来自安装包内的 `runtime-manifest.json`，它随已验签的更新包一起到达，
//! 因此下载内容无需额外签名；发布源只用来找文件。

use crate::local_payload::{self, RuntimeDownload};
use futures_util::StreamExt;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

const SPACE_MARGIN: u64 = 256 * 1024 * 1024;

/// Unique non-empty base URLs, in order.
pub(super) fn runtime_sources<I, S>(candidates: I) -> Vec<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut sources: Vec<String> = Vec::new();
    for candidate in candidates {
        let base = candidate.as_ref().trim().trim_end_matches('/');
        let usable = base.starts_with("http://") || base.starts_with("https://");
        if usable && !sources.iter().any(|known| known == base) {
            sources.push(base.to_string());
        }
    }
    sources
}

pub(super) fn runtime_url(base: &str, name: &str) -> String {
    format!("{base}/api/v1/desktop/download/{name}")
}

fn check_space(download: &RuntimeDownload) -> Result<(), String> {
    let parent = download
        .destination
        .parent()
        .ok_or_else(|| "运行时下载路径无效".to_string())?;
    fs::create_dir_all(parent).map_err(|error| format!("创建运行时目录失败：{error}"))?;
    let partial = fs::metadata(&download.destination)
        .map(|m| m.len())
        .unwrap_or(0);
    let required = download
        .archive_size
        .saturating_sub(partial)
        .saturating_add(download.unpacked_size)
        .saturating_add(SPACE_MARGIN);
    let available =
        fs2::available_space(parent).map_err(|error| format!("检查本机磁盘空间失败：{error}"))?;
    if available < required {
        return Err(format!(
            "磁盘空间不足：下载本机运行环境至少还需 {:.1} GB，当前可用 {:.1} GB",
            required as f64 / 1_073_741_824.0,
            available as f64 / 1_073_741_824.0
        ));
    }
    Ok(())
}

enum Attempt {
    Done,
    /// This source doesn't have the file; try the next one.
    Missing,
}

async fn fetch_from<F>(
    client: &reqwest::Client,
    url: &str,
    download: &RuntimeDownload,
    progress: &mut F,
    cancelled: &AtomicBool,
) -> Result<Attempt, String>
where
    F: FnMut(u64, u64),
{
    let path = &download.destination;
    let mut offset = fs::metadata(path).map(|m| m.len()).unwrap_or(0);
    if offset > download.archive_size {
        let _ = fs::remove_file(path);
        offset = 0;
    }
    let mut request = client.get(url);
    if offset > 0 {
        request = request.header(reqwest::header::RANGE, format!("bytes={offset}-"));
    }
    let response = request
        .send()
        .await
        .map_err(|error| format!("连接 {url} 失败：{error}"))?;
    let status = response.status();
    if status == reqwest::StatusCode::NOT_FOUND {
        return Ok(Attempt::Missing);
    }
    let resume = status == reqwest::StatusCode::PARTIAL_CONTENT;
    if !status.is_success() && status != reqwest::StatusCode::RANGE_NOT_SATISFIABLE {
        return Err(format!("下载 {url} 返回 HTTP {status}"));
    }
    if status == reqwest::StatusCode::RANGE_NOT_SATISFIABLE {
        // The partial file is already complete (or stale); verification decides.
        return Ok(Attempt::Done);
    }
    if !resume {
        offset = 0;
    }
    let mut file = OpenOptions::new()
        .create(true)
        .write(true)
        .append(resume)
        .truncate(!resume)
        .open(path)
        .map_err(|error| format!("写入运行时下载文件失败：{error}"))?;
    let mut received = offset;
    progress(received, download.archive_size);
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        if cancelled.load(Ordering::SeqCst) {
            return Err("下载已取消".to_string());
        }
        let chunk = chunk.map_err(|error| format!("下载本机运行环境中断：{error}"))?;
        file.write_all(&chunk)
            .map_err(|error| format!("写入运行时下载文件失败：{error}"))?;
        received += chunk.len() as u64;
        progress(received, download.archive_size);
    }
    file.flush()
        .map_err(|error| format!("写入运行时下载文件失败：{error}"))?;
    Ok(Attempt::Done)
}

async fn verify(path: &Path, expected: &str) -> Result<(), String> {
    let path_owned = path.to_path_buf();
    let actual =
        tauri::async_runtime::spawn_blocking(move || local_payload::file_sha256(&path_owned))
            .await
            .map_err(|error| format!("校验运行时下载失败：{error}"))??;
    if !actual.eq_ignore_ascii_case(expected) {
        let _ = fs::remove_file(path);
        return Err("下载的本机运行环境校验失败，请重试".to_string());
    }
    Ok(())
}

/// Download the runtime into `download.destination`, resuming a previous partial
/// download, and verify it against the package manifest's sha256.
pub(super) async fn fetch_runtime<F>(
    sources: &[String],
    download: &RuntimeDownload,
    mut progress: F,
    cancelled: &AtomicBool,
) -> Result<PathBuf, String>
where
    F: FnMut(u64, u64),
{
    if sources.is_empty() {
        return Err("安装包未携带本机运行环境，且没有配置下载地址；请下载完整安装包".to_string());
    }
    check_space(download)?;
    // Only a stalled transfer fails; an 800MB runtime may legitimately take long.
    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(15))
        .read_timeout(Duration::from_secs(60))
        .build()
        .map_err(|error| format!("初始化下载客户端失败：{error}"))?;
    let mut errors = Vec::new();
    for base in sources {
        let url = runtime_url(base, &download.name);
        match fetch_from(&client, &url, download, &mut progress, cancelled).await {
            Ok(Attempt::Done) => {
                verify(&download.destination, &download.sha256).await?;
                return Ok(download.destination.clone());
            }
            Ok(Attempt::Missing) => errors.push(format!("{base} 未发布该运行环境")),
            Err(error) => {
                if cancelled.load(Ordering::SeqCst) {
                    return Err(error);
                }
                errors.push(error);
            }
        }
    }
    Err(format!(
        "下载本机运行环境失败（{}）；可稍后重试，或下载完整安装包",
        errors.join("；")
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sources_keep_order_and_drop_blank_or_duplicate_bases() {
        let sources = runtime_sources([
            "https://cloud.example.com/",
            "",
            "https://cloud.example.com",
            "  http://release.example.com  ",
            "not-a-url",
        ]);
        assert_eq!(
            sources,
            vec!["https://cloud.example.com", "http://release.example.com"]
        );
    }

    use sha2::{Digest, Sha256};
    use std::sync::atomic::AtomicUsize;
    use std::sync::Arc;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// Minimal release server: `/missing/...` is 404, everything else serves
    /// `body`, honouring `Range: bytes=N-`. Returns the base URL.
    async fn serve(body: &'static [u8], ranged: Arc<AtomicUsize>) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            loop {
                let (mut socket, _) = listener.accept().await.unwrap();
                let ranged = ranged.clone();
                tokio::spawn(async move {
                    let mut request = vec![0u8; 4096];
                    let read = socket.read(&mut request).await.unwrap();
                    let request = String::from_utf8_lossy(&request[..read]).to_string();
                    let path = request.split_whitespace().nth(1).unwrap_or("").to_string();
                    let start = request
                        .lines()
                        .find_map(|line| {
                            line.strip_prefix("range: bytes=")
                                .or(line.strip_prefix("Range: bytes="))
                        })
                        .and_then(|value| value.trim_end_matches('-').parse::<usize>().ok());
                    let response = if path.starts_with("/missing/") {
                        b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
                            .to_vec()
                    } else if let Some(start) = start {
                        ranged.fetch_add(1, Ordering::SeqCst);
                        let part = &body[start..];
                        let mut out = format!(
                            "HTTP/1.1 206 Partial Content\r\ncontent-length: {}\r\ncontent-range: bytes {start}-{}/{}\r\nconnection: close\r\n\r\n",
                            part.len(), body.len() - 1, body.len()
                        ).into_bytes();
                        out.extend_from_slice(part);
                        out
                    } else {
                        let mut out = format!(
                            "HTTP/1.1 200 OK\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
                            body.len()
                        )
                        .into_bytes();
                        out.extend_from_slice(body);
                        out
                    };
                    let _ = socket.write_all(&response).await;
                    let _ = socket.shutdown().await;
                });
            }
        });
        format!("http://{address}")
    }

    fn download_into(dir: &Path, body: &[u8]) -> RuntimeDownload {
        RuntimeDownload {
            name: "runtime-test.tar.gz".to_string(),
            sha256: format!("{:x}", Sha256::digest(body)),
            archive_size: body.len() as u64,
            unpacked_size: 0,
            destination: dir.join(".runtime.stage-download"),
        }
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "hugagent-runtime-download-{name}-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[tokio::test]
    async fn downloads_from_the_first_source_that_has_the_runtime_and_resumes() {
        const BODY: &[u8] = b"runtime archive bytes for the slim update package";
        let ranged = Arc::new(AtomicUsize::new(0));
        let base = serve(BODY, ranged.clone()).await;
        let dir = scratch("resume");
        let download = download_into(&dir, BODY);
        // An interrupted earlier attempt left the first bytes behind.
        fs::write(&download.destination, &BODY[..10]).unwrap();
        let sources = vec![format!("{base}/missing"), base];
        let mut seen = 0;
        let path = fetch_runtime(
            &sources,
            &download,
            |received, _| seen = received,
            &AtomicBool::new(false),
        )
        .await
        .unwrap();
        assert_eq!(fs::read(path).unwrap(), BODY);
        assert_eq!(seen, BODY.len() as u64);
        assert_eq!(ranged.load(Ordering::SeqCst), 1);
        let _ = fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn a_download_that_fails_verification_is_discarded() {
        const BODY: &[u8] = b"tampered runtime";
        let base = serve(BODY, Arc::new(AtomicUsize::new(0))).await;
        let dir = scratch("verify");
        let mut download = download_into(&dir, BODY);
        download.sha256 = "0".repeat(64);
        let error = fetch_runtime(&[base], &download, |_, _| {}, &AtomicBool::new(false))
            .await
            .unwrap_err();
        assert!(error.contains("校验失败"));
        assert!(!download.destination.exists());
        let _ = fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn no_source_with_the_runtime_points_to_the_full_installer() {
        let base = serve(b"", Arc::new(AtomicUsize::new(0))).await;
        let dir = scratch("missing");
        let download = download_into(&dir, b"x");
        let error = fetch_runtime(
            &[format!("{base}/missing")],
            &download,
            |_, _| {},
            &AtomicBool::new(false),
        )
        .await
        .unwrap_err();
        assert!(error.contains("完整安装包"));
        assert!(
            fetch_runtime(&[], &download, |_, _| {}, &AtomicBool::new(false))
                .await
                .unwrap_err()
                .contains("完整安装包")
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn runtime_url_uses_the_public_download_route() {
        assert_eq!(
            runtime_url("https://x.example.com", "runtime-a-b.tar.gz"),
            "https://x.example.com/api/v1/desktop/download/runtime-a-b.tar.gz"
        );
    }
}
