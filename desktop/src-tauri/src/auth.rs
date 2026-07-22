//! 桌面端会话 token 的持久化 + handoff 票据兑换（方案 B 的 App 侧）。
//!
//! - token 落盘在 `<应用配置目录>/auth.json`（OS 用户私有目录），重启后免登录；
//! - deep-link 带回的一次性 handoff 票据，经 `redeem()` 直连后端 HTTPS 换回真正的
//!   session token。长期 token 全程不进 URL。

use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Serialize, Deserialize, Default)]
struct StoredAuth {
    token: Option<String>,
}

/// 读取已保存的 session token（没有则 None）。
pub fn load_token(config_dir: &Path) -> Option<String> {
    let path = config_dir.join("auth.json");
    let text = std::fs::read_to_string(path).ok()?;
    let parsed: StoredAuth = serde_json::from_str(&text).ok()?;
    parsed.token.filter(|t| !t.is_empty())
}

/// 写入 / 清除 session token（传 None 清除）。
pub fn save_token(config_dir: &Path, token: Option<&str>) {
    let _ = std::fs::create_dir_all(config_dir);
    let path = config_dir.join("auth.json");
    let data = StoredAuth {
        token: token.map(|s| s.to_string()),
    };
    if let Ok(text) = serde_json::to_string(&data) {
        if let Err(e) = std::fs::write(&path, text) {
            eprintln!("[auth] 写入 token 失败: {e}");
        }
    }
}

/// 用一次性 handoff 票据换回真正的 session token（直连后端 HTTPS）。
pub async fn redeem(
    http: &reqwest::Client,
    server_base: &str,
    ticket: &str,
) -> Result<String, String> {
    let url = format!(
        "{}/api/v1/auth/desktop/redeem",
        server_base.trim_end_matches('/')
    );
    let resp = http
        .post(&url)
        .json(&serde_json::json!({ "ticket": ticket }))
        .send()
        .await
        .map_err(|e| format!("网络错误: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("换票失败: HTTP {}", resp.status()));
    }

    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("响应解析失败: {e}"))?;
    // 后端统一信封 { code, message, data: { token, cookie_name, expires_at } }
    let token = body
        .get("data")
        .and_then(|d| d.get("token"))
        .and_then(|t| t.as_str());

    match token {
        Some(t) if !t.is_empty() => Ok(t.to_string()),
        _ => Err("响应缺少 token".to_string()),
    }
}

/// 启动时校验已存 token 是否仍有效：带 cookie 直连后端打 `session/check`。
/// 只有明确 2xx 才算有效；401/网络错误均视为失效（宁可回登录页，也不要带废
/// token 进首页导致前端鉴权失败 → 白屏）。
pub async fn validate(
    http: &reqwest::Client,
    server_base: &str,
    cookie_name: &str,
    token: &str,
) -> bool {
    let url = format!(
        "{}/api/v1/auth/session/check",
        server_base.trim_end_matches('/')
    );
    match http
        .get(&url)
        .header(
            reqwest::header::COOKIE,
            format!("{}={}", cookie_name, token),
        )
        .send()
        .await
    {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    }
}
