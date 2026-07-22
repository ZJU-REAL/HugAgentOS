//! A1 · 原生通知桥。
//!
//! 托盘常驻的意义就是后台跑自动化任务，但跑完若不提醒，用户得自己开窗看。这里起一个
//! 后台轮询：定期拉后端**已有的**通知列表（`/v1/automations/notifications/list`，由
//! `automation_scheduler` 在任务完成/失败时写入 Redis），对**客户端启动之后新增**的通知
//! 发系统原生通知。零后端改动。
//!
//! 「只提醒启动后新增」的判定用通知自带的 `timestamp`（毫秒）与启动时刻比较——这样即便
//! 用户是启动后才登录，历史积压（老 timestamp）也不会一次性刷屏。seen 集合再做一层去重。

use std::collections::HashSet;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use tauri::AppHandle;
use tauri_plugin_notification::NotificationExt;
use tokio::sync::RwLock;

use crate::brand;

/// 轮询间隔。通知不追求实时，25s 足够且不打扰。
const POLL_INTERVAL_SECS: u64 = 25;
/// seen 集合上限，超过就清空（配合 timestamp 过滤，清空不会导致老通知重复弹）。
const SEEN_CAP: usize = 400;

/// 启动后台通知轮询。`http` 复用壳内已配置好的 client（no_proxy + 接受自签证书），
/// 走本地反代 `127.0.0.1:<port>`，反代会自动注入 session cookie。
pub fn start(app: AppHandle, port: u16, token: Arc<RwLock<Option<String>>>, http: reqwest::Client) {
    tauri::async_runtime::spawn(async move {
        let url = format!(
            "http://127.0.0.1:{}/api/v1/automations/notifications/list",
            port
        );
        let start_ms = now_ms();
        let mut seen: HashSet<String> = HashSet::new();

        loop {
            tokio::time::sleep(Duration::from_secs(POLL_INTERVAL_SECS)).await;

            // 未登录时反代不注入 cookie、接口必失败——直接跳过省一次请求。
            if token.read().await.is_none() {
                continue;
            }

            let resp = match http.get(&url).send().await {
                Ok(r) if r.status().is_success() => r,
                _ => continue,
            };
            let body: serde_json::Value = match resp.json().await {
                Ok(v) => v,
                Err(_) => continue,
            };
            let items = match body.get("data").and_then(|d| d.as_array()) {
                Some(a) => a,
                None => continue,
            };

            for it in items {
                let id = it.get("id").and_then(|v| v.as_str()).unwrap_or("");
                if id.is_empty() || seen.contains(id) {
                    continue;
                }
                seen.insert(id.to_string());

                // 只提醒启动后新增的通知。
                let ts = it.get("timestamp").and_then(|v| v.as_i64()).unwrap_or(0);
                if ts <= start_ms {
                    continue;
                }

                let status = it.get("status").and_then(|v| v.as_str()).unwrap_or("");
                let name = it
                    .get("task_name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("任务");
                let summary = it.get("summary").and_then(|v| v.as_str()).unwrap_or("");

                let title = if status == "failed" {
                    format!("{} · 任务失败", brand::NAME)
                } else {
                    format!("{} · 任务完成", brand::NAME)
                };
                let body_text = if summary.is_empty() {
                    name.to_string()
                } else {
                    format!("{}：{}", name, summary)
                };

                let _ = app
                    .notification()
                    .builder()
                    .title(title)
                    .body(body_text)
                    .show();
            }

            if seen.len() > SEEN_CAP {
                seen.clear();
            }
        }
    });
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
