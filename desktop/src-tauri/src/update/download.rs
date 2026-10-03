//! Disk-backed update transport; the plugin remains responsible for native installation.
use base64::Engine;
use futures_util::StreamExt;
use std::time::Duration;
use tokio::io::AsyncWriteExt;

pub(super) async fn download_and_install(
    app: &tauri::AppHandle,
    update: tauri_plugin_updater::Update,
    mut progress: impl FnMut(usize, Option<u64>),
    finished: impl FnOnce() + Send + 'static,
) -> Result<(), String> {
    let config: tauri_plugin_updater::Config = serde_json::from_value(
        app.config()
            .plugins
            .0
            .get("updater")
            .cloned()
            .ok_or("缺少更新器配置")?,
    )
    .map_err(|e| e.to_string())?;
    let mut builder = reqwest::Client::builder()
        .user_agent("tauri-plugin-updater")
        .connect_timeout(Duration::from_secs(10))
        .read_timeout(Duration::from_secs(45))
        .timeout(update.timeout.unwrap_or(Duration::from_secs(1800)))
        .danger_accept_invalid_certs(config.dangerous_accept_invalid_certs)
        .danger_accept_invalid_hostnames(config.dangerous_accept_invalid_hostnames);
    if update.no_proxy {
        builder = builder.no_proxy();
    } else if let Some(proxy) = &update.proxy {
        builder = builder.proxy(reqwest::Proxy::all(proxy.as_str()).map_err(|e| e.to_string())?);
    }
    let mut headers = update.headers.clone();
    headers
        .entry(reqwest::header::ACCEPT)
        .or_insert(reqwest::header::HeaderValue::from_static(
            "application/octet-stream",
        ));
    let response = builder
        .build()
        .map_err(|e| e.to_string())?
        .get(update.download_url.clone())
        .headers(headers)
        .send()
        .await
        .map_err(|e| e.to_string())?
        .error_for_status()
        .map_err(|e| e.to_string())?;
    const MAX_BYTES: u64 = 8 * 1024 * 1024 * 1024;
    let total = response.content_length();
    if total.is_some_and(|n| n > MAX_BYTES) {
        return Err("更新包超过 8 GiB 上限".into());
    }
    // Anonymous, private temporary file: no path can replace the verified data.
    let file = tempfile::tempfile().map_err(|e| e.to_string())?;
    let mut writer = tokio::fs::File::from_std(file);
    let mut stream = response.bytes_stream();
    let mut received = 0u64;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| e.to_string())?;
        received += chunk.len() as u64;
        if received > MAX_BYTES {
            return Err("更新包超过 8 GiB 上限".into());
        }
        writer.write_all(&chunk).await.map_err(|e| e.to_string())?;
        progress(chunk.len(), total);
    }
    writer.flush().await.map_err(|e| e.to_string())?;
    let file = writer.into_std().await;
    let public_key = config.pubkey;
    tauri::async_runtime::spawn_blocking(move || {
        // SAFETY: this private file has no concurrent writers and outlives the mapping.
        // A mapping preserves the plugin's slice API without an installer-sized heap Vec.
        let mapped = unsafe { memmap2::Mmap::map(&file) }.map_err(|e| e.to_string())?;
        verify(&mapped, &update.signature, &public_key)?;
        finished();
        update.install(&mapped[..]).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())??;
    Ok(())
}

fn verify(bytes: &[u8], signature: &str, key: &str) -> Result<(), String> {
    fn decode(value: &str) -> Result<String, String> {
        String::from_utf8(
            base64::engine::general_purpose::STANDARD
                .decode(value)
                .map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())
    }
    let key = minisign_verify::PublicKey::decode(&decode(key)?).map_err(|e| e.to_string())?;
    let signature =
        minisign_verify::Signature::decode(&decode(signature)?).map_err(|e| e.to_string())?;
    key.verify(bytes, &signature, true)
        .map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn signature_rejects_tampered_package() {
        // Public fixture from the minisign-verify test suite (no signing secret).
        let key = "untrusted comment: public key\nRWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3";
        let signature = "untrusted comment: signature from minisign secret key\nRWQf6LRCGA9i59SLOFxz6NxvASXDJeRtuZykwQepbDEGt87ig1BNpWaVWuNrm73YiIiJbq71Wi+dP9eKL8OC351vwIasSSbXxwA=\ntrusted comment: timestamp:1555779966\tfile:test\nQtKMXWyYcwdpZAlPF7tE2ENJkRd1ujvKjlj1m9RtHTBnZPa5WKU5uWRs5GoP5M/VqE81QFuMKI5k/SfNQUaOAA==";
        let key = base64::engine::general_purpose::STANDARD.encode(key);
        let signature = base64::engine::general_purpose::STANDARD.encode(signature);
        assert!(verify(b"test", &signature, &key).is_ok());
        assert!(verify(b"tampered", &signature, &key).is_err());
        assert!(verify(b"test", "invalid", &key).is_err());
    }
}
