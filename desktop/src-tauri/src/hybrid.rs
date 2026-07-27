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
//!   3. **模型配置下发**：云端 `/v1/models/export` → 本机 `/v1/models/import`（Bearer
//!      桥接秘密走 CONFIG_TOKEN 路径），模型配置以云端为准。
//!
//! 所有步骤只在 provision_mode = Dual 下运行；云端/本机单一形态零行为变化。

use std::path::Path;
use std::sync::Arc;

use tokio::sync::RwLock;

use crate::local_server::{LocalServerManager, LOCAL_SERVER_BASE};

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

/// 登录成功后调用：取云端用户信息 → 存 bridge_user → 触发模型配置下发。
/// 幂等；网络失败只打日志（下次登录/启动会重试）。
pub fn on_cloud_login(
    http: reqwest::Client,
    cloud_base: String,
    cookie_name: String,
    token: String,
    bridge_user: Arc<RwLock<Option<String>>>,
    bridge_secret: String,
    local_server: Arc<LocalServerManager>,
) {
    tauri::async_runtime::spawn(async move {
        let user_json = match fetch_cloud_user(&http, &cloud_base, &cookie_name, &token).await {
            Some(u) => u,
            None => {
                eprintln!("[hybrid] 获取云端用户信息失败，暂不更新桥接身份");
                return;
            }
        };
        let encoded = base64_encode(user_json.as_bytes());
        *bridge_user.write().await = Some(encoded);
        eprintln!("[hybrid] 桥接身份已更新（云端用户 → 本机执行面）");

        sync_models_when_local_ready(http, cloud_base, cookie_name, token, bridge_secret, local_server)
            .await;
    });
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
        .and_then(|u| u.host_str().map(|h| format!("{h}:{}", u.port_or_known_default().unwrap_or(80))))
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

/// 等本机服务就绪后：云端 export → 本机 import。
///
/// 首次安装本机服务要下载 Python 运行时 + 全量依赖，普遍超过 5 分钟——等待窗口
/// 给到 ~40 分钟。就绪后的单次同步失败（云端瞬断、本机刚起还没热）也不能永久
/// 跳过（否则本机会话一直「所选模型不可用」直到下次登录），带退避重试几次。
async fn sync_models_when_local_ready(
    http: reqwest::Client,
    cloud_base: String,
    cookie_name: String,
    token: String,
    bridge_secret: String,
    local_server: Arc<LocalServerManager>,
) {
    let mut failures = 0u32;
    for _ in 0..480 {
        if local_server.is_ready().await {
            match sync_models_once(&http, &cloud_base, &cookie_name, &token, &bridge_secret).await {
                Ok(counts) => {
                    eprintln!("[hybrid] 云端模型配置已下发到本机: {counts}");
                    return;
                }
                Err(error) => {
                    failures += 1;
                    eprintln!("[hybrid] 模型配置下发失败（第 {failures} 次）: {error}");
                    if failures >= 5 {
                        return;
                    }
                    // 失败退避：比就绪轮询更长的间隔，避免打爆刚起的本机服务。
                    tokio::time::sleep(std::time::Duration::from_secs(15 * failures as u64))
                        .await;
                    continue;
                }
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
    eprintln!("[hybrid] 本机服务迟迟未就绪，本次跳过模型配置下发");
}

async fn sync_models_once(
    http: &reqwest::Client,
    cloud_base: &str,
    cookie_name: &str,
    token: &str,
    bridge_secret: &str,
) -> Result<String, String> {
    let export_url = format!("{}/api/v1/models/export", cloud_base.trim_end_matches('/'));
    let resp = http
        .get(&export_url)
        .header(reqwest::header::COOKIE, format!("{cookie_name}={token}"))
        .send()
        .await
        .map_err(|e| format!("export 请求失败: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("export HTTP {}", resp.status()));
    }
    let body: serde_json::Value = resp.json().await.map_err(|e| format!("export 解析失败: {e}"))?;
    let data = body.get("data").cloned().unwrap_or(serde_json::json!({}));
    let providers = data.get("providers").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0);

    let import_url = format!("{LOCAL_SERVER_BASE}/api/v1/models/import");
    let resp = http
        .post(&import_url)
        .header(reqwest::header::AUTHORIZATION, format!("Bearer {bridge_secret}"))
        .json(&data)
        .send()
        .await
        .map_err(|e| format!("import 请求失败: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("import HTTP {}", resp.status()));
    }
    Ok(format!("providers={providers}"))
}

/// 标准 base64（无换行）。避免为一处编码引第三方 crate。
pub fn base64_encode(input: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(TABLE[(n >> 18 & 63) as usize] as char);
        out.push(TABLE[(n >> 12 & 63) as usize] as char);
        out.push(if chunk.len() > 1 { TABLE[(n >> 6 & 63) as usize] as char } else { '=' });
        out.push(if chunk.len() > 2 { TABLE[(n & 63) as usize] as char } else { '=' });
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
    fn bridge_secret_is_persistent_and_hex(){
        let dir = std::env::temp_dir().join(format!("hugagent-bridge-secret-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let first = load_or_create_bridge_secret(&dir);
        let second = load_or_create_bridge_secret(&dir);
        assert_eq!(first, second, "秘密应持久化复用");
        assert_eq!(first.len(), 64);
        assert!(first.chars().all(|c| c.is_ascii_hexdigit()));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
