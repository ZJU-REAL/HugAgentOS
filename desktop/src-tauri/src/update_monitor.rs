//! Release notices trigger ordinary signed updater checks, never installation.
use crate::update;
use futures_util::StreamExt;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Notify;

const FALLBACK_SECONDS: u64 = 300;

fn retry_delay(failures: u32, unsupported: bool) -> u64 {
    if unsupported {
        FALLBACK_SECONDS
    } else {
        (3u64 << failures.min(5)).min(60)
    }
}

fn jitter_ms() -> u64 {
    let mut bytes = [0u8; 2];
    let _ = getrandom::getrandom(&mut bytes);
    u16::from_le_bytes(bytes) as u64 % 1500
}

/// Each process has one cloud subscription and one serialized, coalescing check worker.
pub fn start(app: tauri::AppHandle, base: String, client: reqwest::Client) {
    let demand = Arc::new(Notify::new());
    let worker_demand = demand.clone();
    let worker_base = base.clone();
    tauri::async_runtime::spawn(async move {
        loop {
            worker_demand.notified().await;
            // Spread a publication burst across clients without recurring frontend timers.
            tokio::time::sleep(Duration::from_millis(jitter_ms())).await;
            let mut status = update::subscribe();
            while update::status().busy {
                if status.changed().await.is_err() {
                    return;
                }
            }
            update::check_and_install(app.clone(), worker_base.clone(), true);
            while update::status().busy {
                if status.changed().await.is_err() {
                    return;
                }
            }
        }
    });
    tauri::async_runtime::spawn(async move {
        let endpoint = format!("{}/api/v1/desktop/events", base.trim_end_matches('/'));
        let mut failures = 0;
        let mut last_fallback: Option<Instant> = None;
        loop {
            let started = Instant::now();
            let unsupported = listen(&client, &endpoint, &demand).await.unwrap_or(false);
            if last_fallback.map_or(true, |last| last.elapsed().as_secs() >= FALLBACK_SECONDS) {
                demand.notify_one();
                last_fallback = Some(Instant::now());
            }
            if started.elapsed().as_secs() >= 60 {
                failures = 0;
            }
            let seconds = retry_delay(failures, unsupported);
            failures = failures.saturating_add(1);
            tokio::time::sleep(Duration::from_millis(seconds * 1000 + jitter_ms())).await;
        }
    });
}

/// A true return marks a server that has not deployed release notifications yet.
async fn listen(client: &reqwest::Client, endpoint: &str, demand: &Notify) -> Result<bool, ()> {
    let response = tokio::time::timeout(
        crate::net::CLOUD_REQUEST_TIMEOUT,
        client
            .get(endpoint)
            .header("Accept", "text/event-stream")
            .send(),
    )
    .await
    .map_err(|_| ())?
    .map_err(|_| ())?;
    if [404, 405, 501].contains(&response.status().as_u16()) {
        return Ok(true);
    }
    if !response.status().is_success() {
        return Err(());
    }
    if !response
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("text/event-stream"))
    {
        return Ok(true);
    }
    let mut stream = response.bytes_stream();
    let mut decoder = ReleaseDecoder::default();
    let mut previous = None;
    // A low-frequency safety check also covers a failed manifest request after a notice.
    let mut safety = tokio::time::interval_at(
        tokio::time::Instant::now() + Duration::from_secs(1800),
        Duration::from_secs(1800),
    );
    loop {
        tokio::select! {
            _ = safety.tick() => demand.notify_one(),
            chunk = tokio::time::timeout(Duration::from_secs(45), stream.next()) => {
                let bytes = chunk.map_err(|_| ())?.ok_or(())?.map_err(|_| ())?;
                for revision in decoder.push(&bytes)? {
                    if previous.as_ref() != Some(&revision) {
                        previous = Some(revision);
                        demand.notify_one();
                    }
                }
            }
        }
    }
}

#[derive(Default)]
struct ReleaseDecoder {
    buffer: Vec<u8>,
    event: String,
    data: String,
}

impl ReleaseDecoder {
    fn push(&mut self, bytes: &[u8]) -> Result<Vec<String>, ()> {
        if self.buffer.len() + bytes.len() + self.data.len() + self.event.len() > 16384 {
            return Err(());
        }
        self.buffer.extend_from_slice(bytes);
        let mut revisions = Vec::new();
        while let Some(end) = self.buffer.iter().position(|byte| *byte == b'\n') {
            let raw: Vec<_> = self.buffer.drain(..=end).collect();
            let line = std::str::from_utf8(&raw[..raw.len() - 1])
                .map_err(|_| ())?
                .trim_end_matches('\r');
            if line.is_empty() {
                if self.event == "release" {
                    let value: serde_json::Value =
                        serde_json::from_str(&self.data).map_err(|_| ())?;
                    let revision = value.get("revision").and_then(|v| v.as_str()).ok_or(())?;
                    if revision.len() > 128 {
                        return Err(());
                    }
                    revisions.push(revision.to_owned());
                }
                self.event.clear();
                self.data.clear();
            } else if let Some(value) = line.strip_prefix("event:") {
                self.event = value.trim_start().to_owned();
            } else if let Some(value) = line.strip_prefix("data:") {
                if !self.data.is_empty() {
                    self.data.push('\n');
                }
                self.data.push_str(value.strip_prefix(' ').unwrap_or(value));
            }
        }
        Ok(revisions)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn release_frames_survive_chunk_boundaries_and_ignore_heartbeats() {
        let mut decoder = ReleaseDecoder::default();
        assert!(decoder
            .push(b": keepalive\n\nevent: rele")
            .unwrap()
            .is_empty());
        assert!(decoder
            .push(b"ase\r\ndata: {\"revision\":\"abc\"}\r")
            .unwrap()
            .is_empty());
        assert_eq!(decoder.push(b"\n\r\n").unwrap(), vec!["abc"]);
        assert!(decoder
            .push(b"event: other\ndata: {\"revision\":\"evil\"}\n\n")
            .unwrap()
            .is_empty());
        assert!(decoder.push(&vec![b'x'; 16385]).is_err());
    }

    #[test]
    fn reconnect_backoff_is_bounded_and_old_servers_are_low_frequency() {
        assert_eq!(retry_delay(0, false), 3);
        assert_eq!(retry_delay(1, false), 6);
        assert_eq!(retry_delay(20, false), 60);
        assert_eq!(retry_delay(0, true), 300);
    }
}
