//! Device-bound login transport. No device credential is sent to the browser.
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};
use tokio::sync::{Mutex, RwLock};

#[derive(Clone, Deserialize)]
pub struct Grant {
    pub request_id: String,
    pub confirm_code: String,
    pub expires_in: u64,
    pub interval: u64,
}

#[derive(Clone)]
pub struct Attempt {
    pub grant: Grant,
    pub secret: String,
    pub deadline: Instant,
    pub generation: u64,
    pub epoch: u64,
}

#[derive(Clone, Serialize, Default)]
pub struct View {
    pub status: String,
    pub message: String,
    pub confirm_code: String,
}

#[derive(Default)]
pub struct Login {
    generation: AtomicU64,
    pub start_lock: Mutex<()>,
    pub attempt: RwLock<Option<Attempt>>,
    pub view: RwLock<View>,
}

impl Login {
    pub fn generation(&self) -> u64 { self.generation.load(Ordering::SeqCst) }
    pub fn invalidate(&self) { self.generation.fetch_add(1, Ordering::SeqCst); }
    pub fn matches(&self, attempt: &Attempt) -> bool {
        self.generation() == attempt.generation && Instant::now() < attempt.deadline
    }
}

pub fn new_secret() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).map_err(|_| "无法生成登录凭据".to_string())?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

pub fn browser_url(base: &str, grant: &Grant) -> Result<String, String> {
    let mut url = url::Url::parse(&format!("{}/", base.trim_end_matches('/')))
        .map_err(|_| "登录服务器地址无效".to_string())?;
    url.query_pairs_mut().append_pair("desktop_request", &grant.request_id);
    Ok(url.into())
}

async fn request(http: &reqwest::Client, base: &str, path: &str, secret: &str)
    -> Result<serde_json::Value, String>
{
    let response = http.post(format!("{}/api/v1/auth/desktop/requests{path}", base.trim_end_matches('/')))
        .timeout(Duration::from_secs(10))
        .json(&serde_json::json!({"device_secret": secret}))
        .send().await.map_err(|_| "网络暂时不可用，正在重试…".to_string())?;
    let status = response.status();
    if status.as_u16() == 410 { return Err("expired".into()); }
    if status.as_u16() == 404 { return Err("服务器不支持此登录流程，或登录请求已失效".into()); }
    if status.as_u16() == 429 { return Err("登录请求过于频繁，请稍后重试".into()); }
    if !status.is_success() { return Err(format!("登录服务暂时不可用（HTTP {status}）")); }
    let body: serde_json::Value = response.json().await.map_err(|_| "登录响应无效".to_string())?;
    body.get("data").cloned().ok_or_else(|| "登录响应缺少结果".to_string())
}

pub async fn begin(http: &reqwest::Client, base: &str, secret: &str) -> Result<Grant, String> {
    let grant: Grant = serde_json::from_value(request(http, base, "", secret).await?)
        .map_err(|_| "登录请求响应无效".to_string())?;
    if grant.expires_in == 0 || grant.expires_in > 600 || grant.interval == 0
        || grant.request_id.len() != 43 || grant.confirm_code.len() != 8 {
        return Err("登录请求参数无效".into());
    }
    Ok(grant)
}

pub async fn action(http: &reqwest::Client, base: &str, attempt: &Attempt, action: &str)
    -> Result<serde_json::Value, String>
{
    request(http, base, &format!("/{}/{}", attempt.grant.request_id, action), &attempt.secret).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn browser_url_never_carries_device_secret() {
        let grant = Grant { request_id: "r".repeat(43), confirm_code: "ABCD1234".into(), expires_in: 300, interval: 2 };
        let url = browser_url("https://example.test", &grant).unwrap();
        assert_eq!(url, format!("https://example.test/?desktop_request={}", "r".repeat(43)));
        assert_eq!(new_secret().unwrap().len(), 64);
        assert_ne!(new_secret().unwrap(), new_secret().unwrap());
    }

    #[tokio::test]
    async fn cancelled_generation_rejects_late_results() {
        let login = Login::default();
        let attempt = Attempt {
            grant: Grant { request_id: "r".repeat(43), confirm_code: "ABCD1234".into(), expires_in: 300, interval: 2 },
            secret: new_secret().unwrap(), deadline: Instant::now() + Duration::from_secs(300),
            generation: login.generation(), epoch: 0,
        };
        assert!(login.matches(&attempt));
        login.invalidate();
        assert!(!login.matches(&attempt));
    }

    /// Run only against the isolated fixture launched by test-desktop-login-browser.mjs.
    #[tokio::test]
    #[ignore = "requires the browser E2E coordinator and isolated loopback API"]
    async fn browser_e2e() {
        let base = std::env::var("DESKTOP_LOGIN_E2E_BASE").expect("isolated fixture URL");
        assert!(base.starts_with("http://127.0.0.1:"));
        let http = reqwest::Client::builder().no_proxy().build().unwrap();
        let secret = new_secret().unwrap();
        let started = Instant::now();
        let grant = begin(&http, &base, &secret).await.unwrap();
        println!("DEVICE_LOGIN_URL {}", browser_url(&base, &grant).unwrap());
        let attempt = Attempt { grant, secret, deadline: started + Duration::from_secs(60), generation: 0, epoch: 0 };
        loop {
            assert!(Instant::now() < attempt.deadline, "browser never approved login");
            let data = action(&http, &base, &attempt, "poll").await.unwrap();
            if data["status"] == "delivered" {
                let token = data["token"].as_str().expect("session token");
                let response = http.get(format!("{base}/api/v1/auth/session/check"))
                    .header("Cookie", format!("{}={token}", data["cookie_name"].as_str().unwrap()))
                    .send().await.unwrap();
                assert!(response.status().is_success(), "desktop session must authorize a real request");
                let ack = action(&http, &base, &attempt, "ack").await.unwrap();
                assert_eq!(ack["status"], "completed");
                let replay = action(&http, &base, &attempt, "poll").await.unwrap();
                assert!(replay.get("token").is_none());
                println!("DEVICE_LOGIN_COMPLETE");
                break;
            }
            assert_eq!(data["status"], "pending");
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    }
}
