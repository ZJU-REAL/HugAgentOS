//! Network budgets and transient-failure retry shared by every cloud call.
//!
//! Cross-region TLS handshakes to the cloud routinely take 4–6 s, so a budget only
//! fails a request that is genuinely stuck, and a single miss is retried before any
//! caller acts on it.
use std::future::Future;
use std::time::Duration;

/// TCP + TLS establishment on the shared client (it has no overall timeout: it also
/// carries proxied SSE streams).
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// One bounded cloud request/response: connect plus a slow answer.
pub const CLOUD_REQUEST_TIMEOUT: Duration = Duration::from_secs(15);

const ATTEMPTS: u32 = 3;
const RETRY_DELAY: Duration = Duration::from_secs(2);

/// Runs `op` until it settles (`Some`), at most three times. `None` means every
/// attempt hit a transient failure, or `alive` turned false and the caller's result
/// would be discarded anyway.
pub async fn retry_transient<T, F, Fut>(alive: impl Fn() -> bool, op: F) -> Option<T>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Option<T>>,
{
    retry_spaced(RETRY_DELAY, alive, op).await
}

async fn retry_spaced<T, F, Fut>(delay: Duration, alive: impl Fn() -> bool, mut op: F) -> Option<T>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Option<T>>,
{
    for attempt in 0..ATTEMPTS {
        if attempt > 0 {
            tokio::time::sleep(delay).await;
        }
        if !alive() {
            return None;
        }
        if let Some(settled) = op().await {
            return Some(settled);
        }
    }
    None
}

/// Whether the cloud answers its health endpoint, retrying transient misses.
pub async fn cloud_reachable(http: &reqwest::Client, base: &str, alive: impl Fn() -> bool) -> bool {
    let target = format!("{}/health", base.trim_end_matches('/'));
    retry_transient(alive, || async {
        http.get(&target)
            .timeout(CLOUD_REQUEST_TIMEOUT)
            .send()
            .await
            .is_ok_and(|response| response.status().is_success())
            .then_some(())
    })
    .await
    .is_some()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};

    #[tokio::test]
    async fn transient_failures_are_retried_until_settled() {
        let calls = AtomicU32::new(0);
        let result = retry_spaced(
            Duration::ZERO,
            || true,
            || async { (calls.fetch_add(1, Ordering::SeqCst) == 1).then_some("ok") },
        )
        .await;
        assert_eq!(result, Some("ok"));
        assert_eq!(calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn gives_up_after_three_attempts() {
        let calls = AtomicU32::new(0);
        let result: Option<()> = retry_spaced(
            Duration::ZERO,
            || true,
            || async {
                calls.fetch_add(1, Ordering::SeqCst);
                None
            },
        )
        .await;
        assert_eq!(result, None);
        assert_eq!(calls.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn stale_caller_stops_retrying() {
        let calls = AtomicU32::new(0);
        let result: Option<()> = retry_spaced(
            Duration::ZERO,
            || calls.load(Ordering::SeqCst) == 0,
            || async {
                calls.fetch_add(1, Ordering::SeqCst);
                None
            },
        )
        .await;
        assert_eq!(result, None);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }
}
