//! Separate upload inactivity from response-header wait; never time the response body.
use std::{future::Future, time::Duration};
use tokio::{
    sync::watch,
    time::{error::Elapsed, timeout},
};

pub(super) async fn response_headers<F: Future>(
    response: F,
    mut upload: watch::Receiver<bool>,
    budget: Duration,
) -> Result<F::Output, Elapsed> {
    tokio::pin!(response);
    loop {
        if *upload.borrow_and_update() {
            return timeout(budget, response).await;
        }
        let progress = timeout(budget, async {
            tokio::select! {
                result = &mut response => Some(result),
                _ = upload.changed() => None,
            }
        })
        .await?;
        if let Some(result) = progress {
            return Ok(result);
        }
        if upload.has_changed().is_err() {
            return timeout(budget, response).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn active_upload_may_exceed_header_budget() {
        let (tx, rx) = watch::channel(false);
        let upload = tokio::spawn(async move {
            for _ in 0..8 {
                tokio::time::sleep(Duration::from_millis(15)).await;
                tx.send_replace(false);
            }
            tx.send_replace(true);
        });
        let response = async {
            tokio::time::sleep(Duration::from_millis(140)).await;
            42
        };
        assert_eq!(
            response_headers(response, rx, Duration::from_millis(70))
                .await
                .unwrap(),
            42
        );
        upload.await.unwrap();
    }
    #[tokio::test]
    async fn stalled_headers_and_uploads_have_deadlines() {
        for finished in [false, true] {
            let (_tx, rx) = watch::channel(finished);
            assert!(
                response_headers(std::future::pending::<()>(), rx, Duration::from_millis(10))
                    .await
                    .is_err()
            );
        }
    }
}
