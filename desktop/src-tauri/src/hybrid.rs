//! 混合架构（双模式「云端为主 + 本机执行」）的壳侧胶水，见 desktop/HYBRID_MODE_DESIGN.md。
//!
//! 职责：
//!   1. **桥接秘密**：每个安装目录持久化一份随机秘密（`bridge.secret`），孵化本机后端
//!      时注入 `HUGAGENT_DESKTOP_BRIDGE_SECRET` / `CONFIG_TOKEN`，反代对本机路由的请求
//!      凭它证明「来自本机壳」。
//!   2. **云端身份传递**：登录云端后取 `/api/v1/me`，把 `{user_center_id, username, ...}`
//!      编码为 base64 存进 `ProxyState.bridge_user`，反代随本机路由请求下发
//!      （`X-Desktop-Bridge-User`），本机后端据此 get-or-create 同一身份——本机不再有
//!      独立账号密码。
//!   3. **本机执行能力下发**：云端签发短时 desktop capability token；壳只拉取不含
//!      `base_url` / `api_key` 的模型拓扑，并把模型地址改写为云端 capability gateway
//!      后再导入本机。真实模型凭据与仅云端可达的内网地址绝不落到客户端。
//!
//! 所有步骤只在 provision_mode = Dual 下运行；云端/本机单一形态零行为变化。

use std::path::Path;
use std::sync::Arc;

use crate::auth::SessionEpoch;
use tokio::sync::RwLock;

use crate::local_server::{local_server_base, LocalServerManager};

/// 云端身份 → 本机执行面的同步状态。反代据此决定本机路由能否放行：
/// 身份还没推到本机时，本机后端只会回 401，前端会误判成云端会话过期。
#[derive(Debug, Default, Clone)]
pub struct BridgeSync {
    pub synced: bool,
    pub error: Option<String>,
}

/// 读取或生成桥接秘密（`<config_dir>/bridge.secret`，0600 语义、内容 64 hex）。
pub fn load_or_create_bridge_secret(config_dir: &Path) -> String {
    let path = config_dir.join("bridge.secret");
    if let Ok(existing) = std::fs::read_to_string(&path) {
        let trimmed = existing.trim().to_string();
        if trimmed.len() >= 32 {
            return trimmed;
        }
    }
    let secret = random_hex_64();
    let _ = std::fs::create_dir_all(config_dir);
    if let Err(error) = std::fs::write(&path, &secret) {
        eprintln!("[hybrid] 写入 bridge.secret 失败（继续用内存秘密）: {error}");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    secret
}

/// 64 个 hex 字符的随机串。用 `getrandom`（tauri 传递依赖）取 OS 熵。
fn random_hex_64() -> String {
    let mut bytes = [0u8; 32];
    if getrandom::getrandom(&mut bytes).is_err() {
        // 极端兜底：时间 + 地址熵。仅在 OS 熵源不可用时走到。
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let addr = &bytes as *const _ as usize;
        let seed = now ^ (addr as u128) ^ (std::process::id() as u128) << 64;
        for (i, b) in bytes.iter_mut().enumerate() {
            *b = ((seed >> ((i % 16) * 8)) & 0xff) as u8 ^ (i as u8).wrapping_mul(37);
        }
    }
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Stable non-secret installation identity, separate from the rotating capability.
pub fn load_or_create_device_id(config_dir: &Path) -> Result<String, String> {
    let path = config_dir.join("device-id");
    if let Ok(value) = std::fs::read_to_string(&path) {
        let value = value.trim();
        if value.len() == 64 && value.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Ok(value.to_string());
        }
        return Err("device-id 文件无效，请恢复原设备身份".into());
    }
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).map_err(|_| "OS random source is unavailable")?;
    let id: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    std::fs::create_dir_all(config_dir).map_err(|e| e.to_string())?;
    use std::io::Write;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| format!("无法保存设备身份: {e}"))?;
    file.write_all(id.as_bytes())
        .and_then(|_| file.sync_all())
        .map_err(|e| e.to_string())?;
    Ok(id)
}

async fn current_session(
    epoch: &SessionEpoch,
    expected: u64,
    session: &RwLock<Option<String>>,
    token: &str,
) -> bool {
    epoch.matches(expected)
        && epoch.is_active()
        && session.read().await.as_deref() == Some(token)
        && epoch.matches(expected)
}

/// Each login owns one refresh loop. Epoch invalidation cancels old account work.
pub fn on_cloud_login(
    http: reqwest::Client,
    cloud_base: String,
    cookie_name: String,
    session_token: Arc<RwLock<Option<String>>>,
    bridge_user: Arc<RwLock<Option<String>>>,
    bridge_sync: Arc<RwLock<BridgeSync>>,
    bridge_secret: String,
    local_server: Arc<LocalServerManager>,
    device_id: String,
    session_epoch: Arc<SessionEpoch>,
    expected: u64,
) {
    tauri::async_runtime::spawn(async move {
        let Some(token) = session_token.read().await.clone() else {
            return;
        };
        if !current_session(&session_epoch, expected, &session_token, &token).await {
            return;
        }
        let user_json = loop {
            if !current_session(&session_epoch, expected, &session_token, &token).await {
                return;
            }
            if let Some(user) = fetch_cloud_user(&http, &cloud_base, &cookie_name, &token).await {
                break user;
            }
            eprintln!("[hybrid] 获取云端用户信息失败，30 秒后重试");
            *bridge_sync.write().await = BridgeSync {
                synced: false,
                error: Some("获取云端用户信息失败".to_string()),
            };
            tokio::time::sleep(std::time::Duration::from_secs(30)).await;
        };
        {
            let _write = session_epoch.local_write.lock().await;
            if !current_session(&session_epoch, expected, &session_token, &token).await {
                return;
            }
            *bridge_user.write().await = Some(base64_encode(user_json.as_bytes()));
        }
        let mut ready = false;
        for _ in 0..480 {
            if !current_session(&session_epoch, expected, &session_token, &token).await {
                return;
            }
            if local_server.is_ready().await {
                ready = true;
                break;
            }
            tokio::time::sleep(std::time::Duration::from_secs(5)).await;
        }
        if !ready {
            eprintln!("[hybrid] 本机服务未就绪");
            *bridge_sync.write().await = BridgeSync {
                synced: false,
                error: Some("本机服务未就绪".to_string()),
            };
            return;
        }
        loop {
            if !current_session(&session_epoch, expected, &session_token, &token).await {
                return;
            }
            let result = sync_desktop_runtime_once(
                &http,
                &cloud_base,
                &cookie_name,
                &token,
                &bridge_secret,
                &device_id,
                &session_epoch,
                expected,
                &session_token,
            )
            .await;
            if !current_session(&session_epoch, expected, &session_token, &token).await {
                return;
            }
            let delay = match result {
                Ok(_) => {
                    eprintln!("[hybrid] 本机执行能力已刷新");
                    *bridge_sync.write().await = BridgeSync {
                        synced: true,
                        error: None,
                    };
                    300
                }
                Err(error) => {
                    eprintln!("[hybrid] 本机执行能力刷新失败: {error}");
                    *bridge_sync.write().await = BridgeSync {
                        synced: false,
                        error: Some(error),
                    };
                    30
                }
            };
            tokio::time::sleep(std::time::Duration::from_secs(delay)).await;
        }
    });
}

pub async fn clear_cloud_bridge(http: &reqwest::Client, bridge_secret: &str) -> Result<(), String> {
    let resp = http
        .delete(format!(
            "{}/api/v1/desktop/capability/cloud-bridge",
            local_server_base()
        ))
        .bearer_auth(bridge_secret)
        .send()
        .await
        .map_err(|e| format!("清除本机桥失败: {e}"))?;
    if resp.status().is_success() {
        Ok(())
    } else {
        Err(format!("清除本机桥 HTTP {}", resp.status()))
    }
}

/// `/api/v1/me` → 桥接用户 JSON（含云端 host 前缀的 user_center_id，避免跨云端撞号）。
async fn fetch_cloud_user(
    http: &reqwest::Client,
    cloud_base: &str,
    cookie_name: &str,
    token: &str,
) -> Option<String> {
    let url = format!("{}/api/v1/me", cloud_base.trim_end_matches('/'));
    let resp = http
        .get(&url)
        .header(reqwest::header::COOKIE, format!("{cookie_name}={token}"))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let body: serde_json::Value = resp.json().await.ok()?;
    let data = body.get("data").unwrap_or(&body);
    let ucid = data
        .get("user_center_id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.trim().is_empty())
        .or_else(|| data.get("user_id").and_then(|v| v.as_str()))?;
    let host = reqwest::Url::parse(cloud_base)
        .ok()
        .and_then(|u| {
            u.host_str()
                .map(|h| format!("{h}:{}", u.port_or_known_default().unwrap_or(80)))
        })
        .unwrap_or_else(|| "cloud".to_string());
    let payload = serde_json::json!({
        // 前缀云端地址：同一台机器连不同云端时，本机侧身份天然隔离。
        "user_center_id": format!("cloud:{host}:{ucid}"),
        "username": data.get("username").and_then(|v| v.as_str()).unwrap_or(ucid),
        "email": data.get("email").and_then(|v| v.as_str()),
        "avatar_url": data.get("avatar_url").and_then(|v| v.as_str()),
    });
    Some(payload.to_string())
}

async fn issue_capability_once(
    http: &reqwest::Client,
    cloud_base: &str,
    cookie_name: &str,
    token: &str,
    device_id: &str,
) -> Result<(String, i64), String> {
    let base = cloud_base.trim_end_matches('/');
    let issue_url = format!("{base}/api/v1/desktop/capability/token");
    let resp = http
        .post(&issue_url)
        .json(&serde_json::json!({ "device_id": device_id }))
        .header(reqwest::header::COOKIE, format!("{cookie_name}={token}"))
        .send()
        .await
        .map_err(|e| format!("token 签发请求失败: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("token 签发 HTTP {}", resp.status()));
    }
    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("token 响应解析失败: {e}"))?;
    let data = body.get("data").cloned().unwrap_or(serde_json::json!({}));
    let capability_token = data
        .get("token")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "token 响应缺 token 字段".to_string())?;
    let expires_in = data
        .get("expires_in")
        .and_then(|v| v.as_i64())
        .filter(|ttl| *ttl > 0 && *ttl <= 600)
        .ok_or_else(|| "token 响应有效期无效".to_string())?;
    if data.get("device_id").and_then(|v| v.as_str()) != Some(device_id) {
        return Err("token 响应设备身份不匹配".into());
    }
    if data
        .get("authorization_epoch")
        .and_then(|v| v.as_i64())
        .is_none()
    {
        return Err("token 响应缺授权版本".into());
    }
    Ok((capability_token.to_string(), expires_in))
}

fn model_gateway_url(cloud_base: &str, provider_id: &str) -> Result<String, String> {
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

fn model_import_payload(
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

async fn fetch_model_payload(
    http: &reqwest::Client,
    cloud_base: &str,
    capability_token: &str,
    device_id: &str,
) -> Result<(serde_json::Value, usize), String> {
    let base = cloud_base.trim_end_matches('/');
    let manifest_url = format!("{base}/api/v1/desktop/capability/models");
    let resp = http
        .get(&manifest_url)
        .header("X-Desktop-Device-Id", device_id)
        .bearer_auth(capability_token)
        .send()
        .await
        .map_err(|e| format!("模型能力清单请求失败: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("模型能力清单 HTTP {}", resp.status()));
    }
    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("模型能力清单解析失败: {e}"))?;
    let manifest = body.get("data").cloned().unwrap_or(serde_json::json!({}));
    model_import_payload(manifest, base, capability_token)
}

async fn import_models_once(
    http: &reqwest::Client,
    payload: serde_json::Value,
    providers: usize,
    bridge_secret: &str,
) -> Result<String, String> {
    let import_url = format!("{}/api/v1/models/import", local_server_base());
    let resp = http
        .post(&import_url)
        .header(
            reqwest::header::AUTHORIZATION,
            format!("Bearer {bridge_secret}"),
        )
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("import 请求失败: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("import HTTP {}", resp.status()));
    }
    Ok(format!("providers={providers}"))
}

/// 能力桥下发（双端能力网关）：把已签发的短时 capability token 推送为
/// `{cloud_base, token}` 到本机后端。本机后端据此拉取云端能力 manifest，
/// 把云端授权的 MCP 工具（产业知识中心、知识库、搜索等）合并进本机会话。
async fn push_capability_once(
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
        .send()
        .await
        .map_err(|e| format!("桥配置推送失败: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("桥配置推送 HTTP {}", resp.status()));
    }
    Ok(format!("expires_in={expires_in}s"))
}

async fn sync_desktop_runtime_once(
    http: &reqwest::Client,
    cloud_base: &str,
    cookie_name: &str,
    token: &str,
    bridge_secret: &str,
    device_id: &str,
    epoch: &SessionEpoch,
    expected: u64,
    session: &RwLock<Option<String>>,
) -> Result<String, String> {
    if !current_session(epoch, expected, session, token).await {
        return Err("会话已变更".into());
    }
    let (capability_token, expires_in) =
        issue_capability_once(http, cloud_base, cookie_name, token, device_id).await?;
    if !current_session(epoch, expected, session, token).await {
        return Err("会话已变更".into());
    }
    let (payload, providers) =
        fetch_model_payload(http, cloud_base, &capability_token, device_id).await?;
    // Logout invalidates first, then waits for these writes before clearing. Thus a
    // request already in flight cannot restore bridge state after logout cleanup.
    let _write = epoch.local_write.lock().await;
    if !current_session(epoch, expected, session, token).await {
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
    if !current_session(epoch, expected, session, token).await {
        return Err("会话已变更".into());
    }
    let result = import_models_once(http, payload, providers, bridge_secret).await?;
    if !current_session(epoch, expected, session, token).await {
        return Err("会话已变更".into());
    }
    Ok(result)
}

/// 标准 base64（无换行）。避免为一处编码引第三方 crate。
pub fn base64_encode(input: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(TABLE[(n >> 18 & 63) as usize] as char);
        out.push(TABLE[(n >> 12 & 63) as usize] as char);
        out.push(if chunk.len() > 1 {
            TABLE[(n >> 6 & 63) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[(n & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

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
        let dir =
            std::env::temp_dir().join(format!("hugagent-bridge-secret-{}", std::process::id()));
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
        let epoch = SessionEpoch::default();
        let session = RwLock::new(Some("alice".into()));
        let expected = epoch.current();
        assert!(current_session(&epoch, expected, &session, "alice").await);
        epoch.advance();
        *session.write().await = Some("bob".into());
        epoch.activate(epoch.current());
        assert!(!current_session(&epoch, expected, &session, "alice").await);
        assert!(!current_session(&epoch, expected, &session, "bob").await);
        assert!(current_session(&epoch, epoch.current(), &session, "bob").await);
    }

    #[tokio::test]
    async fn delayed_token_response_is_discarded_after_logout() {
        use axum::{routing::post, Json, Router};
        use std::sync::atomic::{AtomicUsize, Ordering};
        let epoch = Arc::new(SessionEpoch::default());
        let session = Arc::new(RwLock::new(Some("alice-session".into())));
        let epoch_at_issue = epoch.clone();
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
                        epoch.advance(); // User logs out while the cloud response is in flight.
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
        let result = sync_desktop_runtime_once(
            &reqwest::Client::new(),
            &base,
            "session",
            "alice-session",
            "secret",
            "test-device",
            &epoch,
            0,
            &session,
        )
        .await;
        server.abort();
        assert_eq!(issued.load(Ordering::SeqCst), 1);
        assert_eq!(result.unwrap_err(), "会话已变更");
        // No model manifest or local bridge endpoint exists in this fixture:
        // an unguarded continuation would fail with a different error.
    }

    #[tokio::test]
    async fn model_manifest_request_binds_device_and_authorization() {
        use axum::{routing::get, Json, Router};
        let app = Router::new().route(
            "/api/v1/desktop/capability/models",
            get(|headers: axum::http::HeaderMap| async move {
                assert_eq!(headers["x-desktop-device-id"], "test-device");
                assert_eq!(headers["authorization"], "Bearer dcap2.test");
                Json(serde_json::json!({"data": {"providers": [], "role_assignments": []}}))
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let result =
            fetch_model_payload(&reqwest::Client::new(), &base, "dcap2.test", "test-device").await;
        server.abort();
        assert_eq!(result.unwrap().1, 0);
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
}
