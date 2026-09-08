//! 桌面端会话 token 的持久化 + handoff 票据兑换（方案 B 的 App 侧）。
//!
//! Session tokens are held by the OS credential store; auth.json is migration-only.

use crate::credential_store::{account_key, CredentialStore, SystemCredentialStore};
use serde::Deserialize;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::sync::Mutex;

/// Invalidate work immediately; serialize local writes and logout cleanup separately.
#[derive(Default)]
pub struct SessionEpoch {
    value: AtomicU64,
    active: AtomicU64,
    pub local_write: Mutex<()>,
}
impl SessionEpoch {
    pub fn current(&self) -> u64 {
        self.value.load(Ordering::SeqCst)
    }
    pub fn advance(&self) -> u64 {
        self.value.fetch_add(1, Ordering::SeqCst) + 1
    }
    pub fn matches(&self, expected: u64) -> bool {
        self.current() == expected
    }
    pub fn is_active(&self) -> bool {
        self.active.load(Ordering::SeqCst) == self.current()
    }
    pub fn activate(&self, expected: u64) {
        if self.matches(expected) {
            self.active.store(expected, Ordering::SeqCst);
        }
    }
}

#[derive(Deserialize, Default)]
struct StoredAuth {
    token: Option<String>,
}

fn remove_legacy(config_dir: &Path) {
    if let Err(error) = std::fs::remove_file(config_dir.join("auth.json")) {
        if error.kind() != std::io::ErrorKind::NotFound {
            eprintln!("[auth] 无法删除旧会话文件: {error}");
        }
    }
}

fn load_with_store(
    config_dir: &Path,
    server_base: &str,
    store: &dyn CredentialStore,
) -> Option<String> {
    let key = account_key(config_dir, server_base);
    if config_dir.join("auth-cleared").exists() {
        remove_legacy(config_dir);
        return None;
    }
    let stored = match store.read(&key) {
        Ok(token) => token.filter(|t| !t.is_empty()),
        Err(error) => {
            eprintln!("[auth] 系统凭据库不可用: {error}");
            None
        }
    };
    let legacy = std::fs::read_to_string(config_dir.join("auth.json"))
        .ok()
        .and_then(|text| serde_json::from_str::<StoredAuth>(&text).ok())
        .and_then(|auth| auth.token)
        .filter(|t| !t.is_empty());
    let token = stored.or(legacy.clone());
    if legacy.is_some() {
        if let Some(token) = token.as_deref() {
            if let Err(error) = store.write(&key, token) {
                eprintln!("[auth] 凭据迁移未能持久化，本次仅内存会话: {error}");
                let _ = std::fs::write(config_dir.join("auth-cleared"), b"");
            }
        }
    }
    // Never retain the old plaintext file, including malformed/empty legacy records.
    remove_legacy(config_dir);
    token
}

pub fn load_token(config_dir: &Path, server_base: &str) -> Option<String> {
    load_with_store(config_dir, server_base, &SystemCredentialStore)
}

pub fn save_token(config_dir: &Path, server_base: &str, token: Option<&str>) {
    let key = account_key(config_dir, server_base);
    // A non-secret tombstone also prevents an unavailable credential service from
    // resurrecting an older account after logout or failed persistence.
    let _ = std::fs::create_dir_all(config_dir);
    let marker = config_dir.join("auth-cleared");
    let _ = std::fs::write(&marker, b"");
    let result = match token {
        Some(token) => SystemCredentialStore.write(&key, token),
        None => SystemCredentialStore.delete(&key),
    };
    match result {
        Ok(()) if token.is_some() => {
            let _ = std::fs::remove_file(marker);
        }
        Ok(()) => {}
        Err(error) => eprintln!("[auth] 系统凭据保存/清除失败（不会回落明文）: {error}"),
    }
    remove_legacy(config_dir);
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    struct MemoryStore {
        token: RefCell<Option<String>>,
        fail: bool,
    }
    impl CredentialStore for MemoryStore {
        fn read(&self, _: &str) -> Result<Option<String>, String> {
            Ok(self.token.borrow().clone())
        }
        fn write(&self, _: &str, token: &str) -> Result<(), String> {
            if self.fail {
                return Err("locked".into());
            }
            *self.token.borrow_mut() = Some(token.to_string());
            Ok(())
        }
        fn delete(&self, _: &str) -> Result<(), String> {
            *self.token.borrow_mut() = None;
            Ok(())
        }
    }
    #[test]
    fn legacy_token_migrates_without_plaintext_fallback() {
        for fail in [false, true] {
            let dir =
                std::env::temp_dir().join(format!("hugagent-auth-{}-{fail}", std::process::id()));
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("auth.json"), r#"{"token":"legacy-session"}"#).unwrap();
            let store = MemoryStore {
                token: RefCell::new(None),
                fail,
            };
            assert_eq!(
                load_with_store(&dir, "https://cloud", &store).as_deref(),
                Some("legacy-session")
            );
            assert!(!dir.join("auth.json").exists());
            assert_eq!(store.token.borrow().is_some(), !fail);
            std::fs::remove_dir_all(dir).unwrap();
        }
    }
    #[test]
    fn credentials_are_scoped_to_cloud_and_installation() {
        assert_ne!(
            account_key(Path::new("/app-a"), "https://cloud"),
            account_key(Path::new("/app-b"), "https://cloud")
        );
        assert_ne!(
            account_key(Path::new("/app-a"), "https://cloud"),
            account_key(Path::new("/app-a"), "https://other")
        );
        assert_eq!(
            account_key(Path::new("/app-a"), "https://cloud"),
            account_key(Path::new("/app-a"), "https://cloud/")
        );
    }
    #[tokio::test]
    async fn logout_invalidates_old_work_before_waiting_for_local_write() {
        let epoch = SessionEpoch::default();
        let old = epoch.current();
        let writing = epoch.local_write.lock().await;
        epoch.advance();
        assert!(!epoch.matches(old));
        assert!(!epoch.is_active());
        epoch.activate(old);
        assert!(
            !epoch.is_active(),
            "a stale login cannot reactivate the old epoch"
        );
        drop(writing);
        let _cleanup = epoch.local_write.lock().await;
        assert!(!epoch.matches(old));
    }
}
