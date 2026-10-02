use super::identity::issue_capability_once;
use super::models::fetch_model_payload;
use super::models::import_models_once;
use crate::local_server::local_server_base;
pub(super) async fn push_capability_once(
    http: &reqwest::Client,
    cloud_base: &str,
    capability_token: &str,
    expires_in: i64,
    bridge_secret: &str,
    device_id: &str,
) -> Result<String, String> {
    let base = cloud_base.trim_end_matches('/');
    let push_url = format!(
        "{}/api/v1/desktop/capability/cloud-bridge",
        local_server_base()
    );
    let payload = serde_json::json!({
        "cloud_base": base,
        "token": capability_token,
        "expires_in": expires_in,
        "device_id": device_id,
    });
    let resp = http
        .post(&push_url)
        .header(
            reqwest::header::AUTHORIZATION,
            format!("Bearer {bridge_secret}"),
        )
        .json(&payload)
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| format!("桥配置推送失败: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("桥配置推送 HTTP {}", resp.status()));
    }
    Ok(format!("expires_in={expires_in}s"))
}

pub(super) async fn sync_desktop_runtime_once(
    context: &super::BridgeContext,
    token: &str,
    expected: u64,
    model_revision: &mut Option<String>,
) -> Result<i64, String> {
    let super::BridgeContext {
        http,
        cloud_base,
        cookie_name,
        session,
        bridge_secret,
        device_id,
        local_server,
    } = context;
    if !session.is_current(expected, token).await {
        return Err("会话已变更".into());
    }
    let (capability_token, expires_in) =
        issue_capability_once(http, cloud_base, cookie_name, token, device_id).await?;
    if !session.is_current(expected, token).await {
        return Err("会话已变更".into());
    }
    let models = fetch_model_payload(
        http,
        cloud_base,
        &capability_token,
        device_id,
        model_revision.as_deref(),
    )
    .await?;
    // Logout invalidates first, then waits for these writes before clearing. Thus a
    // request already in flight cannot restore bridge state after logout cleanup.
    let _write = session.epoch.local_write.lock().await;
    if !session.is_current(expected, token).await {
        return Err("会话已变更".into());
    }
    push_capability_once(
        http,
        cloud_base,
        &capability_token,
        expires_in,
        bridge_secret,
        device_id,
    )
    .await?;
    if !session.is_current(expected, token).await {
        return Err("会话已变更".into());
    }
    session
        .update_bridge(expected, |sync| {
            sync.identity_ready = true;
            sync.error = None;
            sync.retrying = false;
        })
        .await;
    local_server.notify_changed();
    if let Some((payload, providers, revision)) = models {
        import_models_once(http, payload, providers, bridge_secret).await?;
        if !session.is_current(expected, token).await {
            return Err("会话已变更".into());
        }
        eprintln!(
            "[hybrid] 模型拓扑已导入 providers={providers} revision={}",
            &revision[..12.min(revision.len())]
        );
        *model_revision = Some(revision);
    }
    session
        .update_bridge(expected, |sync| sync.models_ready = true)
        .await;
    local_server.notify_changed();
    Ok(expires_in)
}
