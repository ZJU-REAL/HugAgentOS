//! 混合架构（双模式「云端为主 + 本机执行」）的壳侧胶水，见 desktop/HYBRID_MODE_DESIGN.md。
//!
//! 职责：
//!   1. **桥接秘密**：每个安装目录持久化一份随机秘密（`bridge.secret`），孵化本机后端
//!      时注入 `HUGAGENT_DESKTOP_BRIDGE_SECRET` / `CONFIG_TOKEN`，反代对本机路由的请求
//!      凭它证明「来自本机壳」。
//!   2. **云端身份传递**：登录云端后取 `/api/v1/me`，把 `{user_center_id, username, ...}`
//!      编码为 base64 存进 `SessionState` 的桥接用户快照，反代随本机路由请求下发
//!      （`X-Desktop-Bridge-User`），本机后端据此 get-or-create 同一身份——本机不再有
//!      独立账号密码。
//!   3. **本机执行能力下发**：云端签发短时 desktop capability token；壳只拉取不含
//!      `base_url` / `api_key` 的模型拓扑，并把模型地址改写为云端 capability gateway
//!      后再导入本机。真实模型凭据与仅云端可达的内网地址绝不落到客户端。
//!
//! 所有步骤只在 provision_mode = Dual 下运行；云端/本机单一形态零行为变化。

use std::sync::Arc;

use crate::session_state::SessionState;

use crate::local_server::{local_server_base, LocalServerManager};

/// 云端身份 → 本机执行面的同步状态。
///
/// `identity_ready`：桥配置已推到本机后端，本机能认出当前云端用户——反代据此放行
/// 本机路由（否则本机后端只会回 401，前端会误判成云端会话过期）。
/// `capabilities_ready`：模型拓扑和首次能力包同步均已完成。
/// 模型与组件的实际可用性仍由本机后端在调用时校验。
/// `retrying`：`error` 是壳自己还在自动重试的暂时性故障（云端不可达等），不需要用户
/// 处理。界面据此区分「还在重试」与「停下来等你决定」——否则一个会自愈的网络抖动会
/// 被画成失败卡片，而那张卡片上的按钮此时全都是灰的，看着像彻底卡死。
#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct BridgeSync {
    pub identity_ready: bool,
    pub capabilities_ready: bool,
    pub models_ready: bool,
    pub error: Option<String>,
    pub retrying: bool,
}

mod capabilities;
/// 读取或生成桥接秘密（`<config_dir>/bridge.secret`，0600 语义、内容 64 hex）。
mod identity;
mod models;
use capabilities::sync_desktop_runtime_once;
use identity::fetch_cloud_user;
pub use identity::{
    base64_encode, clear_cloud_bridge, load_or_create_bridge_secret, load_or_create_device_id,
};
#[cfg(test)]
use models::model_import_payload;
#[cfg(test)]
mod tests;
/// Immutable connection settings and one session owner travel together.
#[derive(Clone)]
pub(crate) struct BridgeContext {
    pub http: reqwest::Client,
    pub cloud_base: String,
    pub cookie_name: String,
    pub session: Arc<SessionState>,
    pub bridge_secret: String,
    pub local_server: Arc<LocalServerManager>,
    pub device_id: String,
}

pub(crate) fn on_cloud_login(context: BridgeContext, expected: u64) {
    let BridgeContext {
        http,
        cloud_base,
        cookie_name,
        session,
        bridge_secret,
        local_server,
        ..
    } = context.clone();
    tauri::async_runtime::spawn(async move {
        let Some(token) = session.token().await else {
            return;
        };
        if !session.is_current(expected, &token).await {
            return;
        }
        let user_json = loop {
            if !session.is_current(expected, &token).await {
                return;
            }
            if let Some(user) = fetch_cloud_user(&http, &cloud_base, &cookie_name, &token).await {
                break user;
            }
            eprintln!("[hybrid] 获取云端用户信息失败，30 秒后重试");
            if !session
                .update_bridge(expected, |sync| {
                    *sync = BridgeSync {
                        error: Some("获取云端用户信息失败".to_string()),
                        retrying: true,
                        ..BridgeSync::default()
                    }
                })
                .await
            {
                return;
            }
            local_server.notify_changed();
            tokio::time::sleep(std::time::Duration::from_secs(30)).await;
        };
        {
            let _write = session.epoch.local_write.lock().await;
            if !session.is_current(expected, &token).await {
                return;
            }
            session
                .set_bridge_user(expected, base64_encode(user_json.as_bytes()))
                .await;
        }
        // 等待本机执行面就绪，不设截止时间。本机服务可能还没装（首启停在初始化页，
        // 装机要等用户按下「开始初始化」）、正在解压运行环境，或刚被重启——这些等待
        // 都可能远超任何固定预算。一旦超时就放弃，本轮登录之后再也不会同步能力：
        // 本机服务后来起来了也没人接手，客户端只能卡在能力同步页直到重启。就绪由
        // LocalServerManager 的状态版本推送唤醒，等待期间不轮询。
        let mut readiness = local_server.subscribe();
        loop {
            if !session.is_current(expected, &token).await {
                return;
            }
            if local_server.is_ready().await {
                break;
            }
            if readiness.changed().await.is_err() {
                return;
            }
        }
        // Renewal cadence follows the token lifetime; the model topology is only
        // re-imported when the cloud says it changed (ETag), so a renewal never
        // touches the local model configuration.
        let mut model_revision: Option<String> = None;
        loop {
            if !session.is_current(expected, &token).await {
                return;
            }
            let result =
                sync_desktop_runtime_once(&context, &token, expected, &mut model_revision).await;
            if !session.is_current(expected, &token).await {
                return;
            }
            let delay = match result {
                Ok(expires_in) => renewal_delay_secs(expires_in),
                Err(error) => {
                    eprintln!("[hybrid] 本机执行能力刷新失败: {error}");
                    if !session
                        .update_bridge(expected, |sync| {
                            sync.capabilities_ready = false;
                            sync.models_ready = false;
                            sync.error = Some(error);
                            sync.retrying = true;
                        })
                        .await
                    {
                        return;
                    }
                    local_server.notify_changed();
                    30
                }
            };
            // Capability readiness is observed independently of token renewal.
            // A retry or explicit partial choice takes effect on the next poll,
            // even when the user leaves the failure card open for several minutes.
            // 只在「还没就绪」时轮询：首次同步完成后停下来睡到下一次令牌续期，
            // 之后的能力变动由变更号驱动，不需要有人一直敲门。
            let next_renewal = tokio::time::Instant::now() + std::time::Duration::from_secs(delay);
            let mut attempts = 0u32;
            while tokio::time::Instant::now() < next_renewal {
                if !session.is_current(expected, &token).await {
                    return;
                }
                let (models_ready, capabilities_ready) = {
                    let sync = session.snapshot().await.bridge;
                    (sync.models_ready, sync.capabilities_ready)
                };
                if capabilities_ready {
                    tokio::time::sleep_until(next_renewal).await;
                    break;
                }
                if models_ready {
                    let readiness = read_capability_readiness(&http, &bridge_secret).await;
                    if !session.is_current(expected, &token).await {
                        return;
                    }
                    let (ready, error) = readiness.unwrap_or_else(|error| (false, Some(error)));
                    let sync = session.snapshot().await.bridge;
                    if sync.capabilities_ready != ready || sync.error != error {
                        if !session
                            .update_bridge(expected, |sync| {
                                sync.capabilities_ready = ready;
                                sync.error = error;
                            })
                            .await
                        {
                            return;
                        }
                        local_server.notify_changed();
                        attempts = 0;
                    } else {
                        attempts = attempts.saturating_add(1);
                    }
                }
                tokio::time::sleep(std::time::Duration::from_millis(readiness_poll_delay_ms(
                    attempts,
                )))
                .await;
            }
        }
    });
}

async fn read_capability_readiness(
    http: &reqwest::Client,
    bridge_secret: &str,
) -> Result<(bool, Option<String>), String> {
    let response = http
        .get(format!(
            "{}/api/v1/desktop/capability/cloud-bridge/readiness",
            local_server_base()
        ))
        .bearer_auth(bridge_secret)
        .timeout(std::time::Duration::from_secs(5))
        .send()
        .await
        .map_err(|_| "读取本机能力同步进度失败".to_string())?;
    if !response.status().is_success() {
        return Err(format!("读取本机能力同步进度 HTTP {}", response.status()));
    }
    let body: serde_json::Value = response
        .json()
        .await
        .map_err(|_| "本机能力同步进度格式无效".to_string())?;
    let data = &body["data"];
    Ok((
        data["ready"].as_bool() == Some(true),
        data["error"].as_str().map(str::to_owned),
    ))
}

/// 就绪轮询的起步间隔：用户正盯着同步页，头几次要足够快。
const READINESS_POLL_BASE_MS: u64 = 500;
/// 一直不就绪时的间隔上限。卡住的状态不该一直以两次每秒敲本机后端——实测那会在
/// 本机日志里堆出几十万条请求，日志体积的绝大部分都来自这一个循环。
const READINESS_POLL_MAX_MS: u64 = 15_000;

/// 读取就绪状态的退避间隔：读数没变化就逐步拉长，一有变化立刻回到起步间隔。
fn readiness_poll_delay_ms(unchanged_reads: u32) -> u64 {
    (READINESS_POLL_BASE_MS << unchanged_reads.min(5)).min(READINESS_POLL_MAX_MS)
}

/// Renew one minute before expiry; the cloud caps `expires_in` at 600 s.
fn renewal_delay_secs(expires_in: i64) -> u64 {
    (expires_in - 60).max(30) as u64
}
