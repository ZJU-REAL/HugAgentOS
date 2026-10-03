use crate::local_server::local_server_base;
pub(super) fn model_gateway_url(cloud_base: &str, provider_id: &str) -> Result<String, String> {
    let mut url = reqwest::Url::parse(cloud_base).map_err(|e| format!("云端地址无效: {e}"))?;
    {
        let mut segments = url
            .path_segments_mut()
            .map_err(|_| "云端地址不能作为模型网关基址".to_string())?;
        segments.pop_if_empty();
        segments.extend(["api", "v1", "desktop", "capability", "gateway", "models"]);
        segments.push(provider_id);
    }
    Ok(url.to_string().trim_end_matches('/').to_string())
}

pub(super) fn model_import_payload(
    manifest: serde_json::Value,
    cloud_base: &str,
    capability_token: &str,
) -> Result<(serde_json::Value, usize), String> {
    let providers = manifest
        .get("providers")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "模型能力清单缺少 providers".to_string())?;
    let role_assignments = manifest
        .get("role_assignments")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "模型能力清单缺少 role_assignments".to_string())?;

    let mut rewritten = Vec::with_capacity(providers.len());
    for provider in providers {
        let mut provider = provider.clone();
        let object = provider
            .as_object_mut()
            .ok_or_else(|| "模型能力清单包含非对象 provider".to_string())?;
        let provider_id = object
            .get("provider_id")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|v| !v.is_empty())
            .ok_or_else(|| "模型能力清单包含空 provider_id".to_string())?;
        let gateway = model_gateway_url(cloud_base, provider_id)?;
        object.insert("base_url".to_string(), serde_json::Value::String(gateway));
        object.insert(
            "api_key".to_string(),
            serde_json::Value::String(capability_token.to_string()),
        );
        rewritten.push(provider);
    }
    let count = rewritten.len();
    Ok((
        serde_json::json!({
            "providers": rewritten,
            "role_assignments": role_assignments,
            "overwrite": true,
        }),
        count,
    ))
}

/// Model topology plus its cloud revision; `Ok(None)` means unchanged since
/// `known_revision` (HTTP 304).
pub(super) async fn fetch_model_payload(
    http: &reqwest::Client,
    cloud_base: &str,
    capability_token: &str,
    device_id: &str,
    known_revision: Option<&str>,
) -> Result<Option<(serde_json::Value, usize, String)>, String> {
    let base = cloud_base.trim_end_matches('/');
    let manifest_url = format!("{base}/api/v1/desktop/capability/models");
    let mut request = http
        .get(&manifest_url)
        .header("X-Desktop-Device-Id", device_id)
        .bearer_auth(capability_token);
    if let Some(revision) = known_revision {
        request = request.header(reqwest::header::IF_NONE_MATCH, format!("\"{revision}\""));
    }
    let resp = request
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| format!("模型能力清单请求失败: {e}"))?;
    if resp.status() == reqwest::StatusCode::NOT_MODIFIED {
        return Ok(None);
    }
    if !resp.status().is_success() {
        return Err(format!("模型能力清单 HTTP {}", resp.status()));
    }
    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("模型能力清单解析失败: {e}"))?;
    let manifest = body.get("data").cloned().unwrap_or(serde_json::json!({}));
    let revision = manifest
        .get("revision")
        .and_then(|v| v.as_str())
        .filter(|v| !v.is_empty())
        .ok_or_else(|| "模型能力清单缺少 revision".to_string())?
        .to_string();
    let (payload, count) = model_import_payload(manifest, base, capability_token)?;
    Ok(Some((payload, count, revision)))
}

pub(super) async fn import_models_once(
    http: &reqwest::Client,
    payload: serde_json::Value,
    providers: usize,
    bridge_secret: &str,
) -> Result<(), String> {
    let import_url = format!("{}/api/v1/models/import", local_server_base());
    let resp = http
        .post(&import_url)
        .header(
            reqwest::header::AUTHORIZATION,
            format!("Bearer {bridge_secret}"),
        )
        .json(&payload)
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| format!("import 请求失败: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("import HTTP {}", resp.status()));
    }
    let _ = providers;
    Ok(())
}
