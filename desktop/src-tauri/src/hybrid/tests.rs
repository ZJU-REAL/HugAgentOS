use super::models::fetch_model_payload;
use super::*;

#[test]
fn readiness_polling_backs_off_and_is_capped() {
    assert_eq!(readiness_poll_delay_ms(0), READINESS_POLL_BASE_MS);
    assert_eq!(readiness_poll_delay_ms(1), 1_000);
    assert_eq!(readiness_poll_delay_ms(4), 8_000);
    // 卡在未就绪状态时封顶，不会再退化成每秒两次敲本机后端。
    assert_eq!(readiness_poll_delay_ms(5), READINESS_POLL_MAX_MS);
    assert_eq!(readiness_poll_delay_ms(u32::MAX), READINESS_POLL_MAX_MS);
}

#[test]
fn base64_matches_known_vectors() {
    assert_eq!(base64_encode(b""), "");
    assert_eq!(base64_encode(b"f"), "Zg==");
    assert_eq!(base64_encode(b"fo"), "Zm8=");
    assert_eq!(base64_encode(b"foo"), "Zm9v");
    assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
    assert_eq!(
        base64_encode(br#"{"user_center_id":"u1"}"#),
        "eyJ1c2VyX2NlbnRlcl9pZCI6InUxIn0="
    );
}

#[test]
fn bridge_secret_is_persistent_and_hex() {
    let dir = std::env::temp_dir().join(format!("hugagent-bridge-secret-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let first = load_or_create_bridge_secret(&dir);
    let second = load_or_create_bridge_secret(&dir);
    assert_eq!(first, second, "秘密应持久化复用");
    assert_eq!(first.len(), 64);
    assert!(first.chars().all(|c| c.is_ascii_hexdigit()));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn model_manifest_is_rewritten_to_capability_gateway() {
    let manifest = serde_json::json!({
        "providers": [{
            "provider_id": "private/deepseek",
            "display_name": "DeepSeek",
            "base_url": "http://192.0.2.10:1029/v1",
            "api_key": "must-not-survive"
        }],
        "role_assignments": [{"role_key": "main_agent", "provider_id": "private/deepseek"}]
    });
    let (payload, count) =
        model_import_payload(manifest, "https://cloud.example", "dcap2.short-lived")
            .expect("manifest should be valid");
    assert_eq!(count, 1);
    let provider = &payload["providers"][0];
    assert_eq!(provider["api_key"], "dcap2.short-lived");
    assert_eq!(
        provider["base_url"],
        "https://cloud.example/api/v1/desktop/capability/gateway/models/private%2Fdeepseek"
    );
    assert_eq!(payload["overwrite"], true);
    let serialized = payload.to_string();
    assert!(!serialized.contains("192.0.2."));
    assert!(!serialized.contains("must-not-survive"));
}

#[test]
fn device_identity_is_stable_and_separate_from_bridge_secret() {
    let dir = std::env::temp_dir().join(format!("hugagent-device-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let first = load_or_create_device_id(&dir).unwrap();
    assert_eq!(first, load_or_create_device_id(&dir).unwrap());
    assert_eq!(first.len(), 64);
    assert_ne!(first, load_or_create_bridge_secret(&dir));
    std::fs::write(dir.join("device-id"), "broken").unwrap();
    assert!(load_or_create_device_id(&dir).is_err());
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test]
async fn old_login_response_cannot_match_a_new_session() {
    let session = SessionState::new(Some("alice".into()));
    let expected = session.epoch.current();
    assert!(session.is_current(expected, "alice").await);
    let next = session.epoch.advance();
    session.accept(next, "bob".into()).await;
    session.epoch.activate(next);
    assert!(!session.is_current(expected, "alice").await);
    assert!(!session.is_current(expected, "bob").await);
    assert!(session.is_current(next, "bob").await);
}

#[tokio::test]
async fn delayed_token_response_is_discarded_after_logout() {
    use axum::{routing::post, Json, Router};
    use std::sync::atomic::{AtomicUsize, Ordering};
    let session = Arc::new(SessionState::new(Some("alice-session".into())));
    let epoch_at_issue = session.clone();
    let issued = Arc::new(AtomicUsize::new(0));
    let issued_at_server = issued.clone();
    let app = Router::new().route(
        "/api/v1/desktop/capability/token",
        post(
            move |headers: axum::http::HeaderMap, Json(body): Json<serde_json::Value>| {
                let epoch = epoch_at_issue.clone();
                let issued = issued_at_server.clone();
                async move {
                    assert_eq!(body["device_id"], "test-device");
                    assert_eq!(headers["cookie"], "session=alice-session");
                    issued.fetch_add(1, Ordering::SeqCst);
                    epoch.epoch.advance(); // User logs out while the cloud response is in flight.
                    Json(serde_json::json!({"data": {
                        "token": "dcap2.test", "expires_in": 600,
                        "device_id": "test-device", "authorization_epoch": 42
                    }}))
                }
            },
        ),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let scratch = std::env::temp_dir().join(format!("hybrid-sync-test-{}", std::process::id()));
    let local_server = crate::local_server::LocalServerManager::new(
        scratch.join("runtime"),
        scratch.join("data"),
        scratch.join("missing.zip"),
        scratch.join("missing.json"),
        scratch.join("missing.tar.gz"),
        scratch.join("missing-runtime.json"),
        reqwest::Client::new(),
    );
    let context = BridgeContext {
        http: reqwest::Client::new(),
        cloud_base: base,
        cookie_name: "session".into(),
        session,
        bridge_secret: "secret".into(),
        device_id: "test-device".into(),
        local_server,
    };
    let mut revision = None;
    let result = sync_desktop_runtime_once(&context, "alice-session", 0, &mut revision).await;
    server.abort();
    assert_eq!(issued.load(Ordering::SeqCst), 1);
    assert_eq!(result.unwrap_err(), "会话已变更");
    // No model manifest or local bridge endpoint exists in this fixture:
    // an unguarded continuation would fail with a different error.
}

#[tokio::test]
async fn model_manifest_request_binds_device_and_authorization() {
    use axum::{response::IntoResponse, routing::get, Json, Router};
    let app = Router::new().route(
        "/api/v1/desktop/capability/models",
        get(|headers: axum::http::HeaderMap| async move {
            assert_eq!(headers["x-desktop-device-id"], "test-device");
            assert_eq!(headers["authorization"], "Bearer dcap2.test");
            if headers
                .get("if-none-match")
                .map(|v| v == "\"rev-1\"")
                .unwrap_or(false)
            {
                return axum::http::StatusCode::NOT_MODIFIED.into_response();
            }
            Json(serde_json::json!({"data": {
                "providers": [], "role_assignments": [], "revision": "rev-1"
            }}))
            .into_response()
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let client = reqwest::Client::new();
    let fresh = fetch_model_payload(&client, &base, "dcap2.test", "test-device", None).await;
    let unchanged =
        fetch_model_payload(&client, &base, "dcap2.test", "test-device", Some("rev-1")).await;
    server.abort();
    let (_, count, revision) = fresh.unwrap().expect("first fetch returns the topology");
    assert_eq!((count, revision.as_str()), (0, "rev-1"));
    assert!(
        unchanged.unwrap().is_none(),
        "same revision must not re-import"
    );
}

#[test]
fn renewal_is_derived_from_the_token_lifetime() {
    assert_eq!(renewal_delay_secs(600), 540);
    assert_eq!(renewal_delay_secs(90), 30);
    assert_eq!(renewal_delay_secs(60), 30);
}

#[test]
fn model_manifest_requires_complete_topology() {
    let error = model_import_payload(
        serde_json::json!({"providers": []}),
        "https://cloud.example",
        "dcap2.token",
    )
    .expect_err("role assignments are required");
    assert!(error.contains("role_assignments"));
}

#[tokio::test]
async fn stalled_control_response_releases_session_write_budget() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let (entered, ready) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let (_socket, _) = listener.accept().await.unwrap();
        let _ = entered.send(());
        std::future::pending::<()>().await;
    });
    let session = Arc::new(SessionState::new(Some("alice".into())));
    let writer = session.clone();
    let request = tokio::spawn(async move {
        let _write = writer.epoch.local_write.lock().await;
        super::identity::fetch_cloud_user(
            &reqwest::Client::builder().no_proxy().build().unwrap(),
            &base,
            "session",
            "alice",
        )
        .await
    });
    ready.await.unwrap();
    session.epoch.advance();
    let cleanup = tokio::time::timeout(
        std::time::Duration::from_secs(17),
        session.epoch.local_write.lock(),
    )
    .await;
    server.abort();
    assert!(
        cleanup.is_ok(),
        "a connected but stalled control endpoint must not pin logout forever"
    );
    assert!(request.await.unwrap().is_none());
}
