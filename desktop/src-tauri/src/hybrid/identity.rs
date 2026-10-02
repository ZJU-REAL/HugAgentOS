use crate::local_server::local_server_base;
use std::io::Write;
use std::path::Path;
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
pub(super) fn random_hex_64() -> String {
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

pub async fn clear_cloud_bridge(http: &reqwest::Client, bridge_secret: &str) -> Result<(), String> {
    let resp = http
        .delete(format!(
            "{}/api/v1/desktop/capability/cloud-bridge",
            local_server_base()
        ))
        .bearer_auth(bridge_secret)
        .timeout(std::time::Duration::from_secs(15))
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
pub(super) async fn fetch_cloud_user(
    http: &reqwest::Client,
    cloud_base: &str,
    cookie_name: &str,
    token: &str,
) -> Option<String> {
    let url = format!("{}/api/v1/me", cloud_base.trim_end_matches('/'));
    let resp = http
        .get(&url)
        .header(reqwest::header::COOKIE, format!("{cookie_name}={token}"))
        .timeout(std::time::Duration::from_secs(15))
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

pub(super) async fn issue_capability_once(
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
        .timeout(std::time::Duration::from_secs(15))
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
