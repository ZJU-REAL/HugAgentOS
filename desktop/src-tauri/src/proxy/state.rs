use super::transport::is_same_origin;
use super::ProxyState;
use crate::config::ProvisionMode;
use crate::local_server::LocalServerStatus;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::StatusCode;
use axum::response::sse::Event;
use axum::response::sse::KeepAlive;
use axum::response::sse::Sse;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::Json;
use futures_util::Stream;
use std::convert::Infallible;
#[derive(serde::Serialize)]
pub(super) struct SetupStatus {
    #[serde(flatten)]
    service: LocalServerStatus,
    active_local: bool,
    current_server_base: String,
    /// 本机后端基址（端口由构建期品牌配置决定）。前端为「本机站点」生成对外链接时用，
    /// 避免把随启动变化的反代随机端口写进可分享的 URL。
    local_server_base: String,
    provision_mode: ProvisionMode,
    /// 双模式：云端身份 / 模型拓扑是否已推到本机执行面。
    bridge: crate::hybrid::BridgeSync,
    update: crate::update::UpdateStatus,
}

pub(super) async fn setup_status_value(state: &ProxyState) -> SetupStatus {
    SetupStatus {
        service: state.local_server.snapshot().await,
        active_local: state.active_local,
        current_server_base: state.server_base.clone(),
        local_server_base: state.local_base.clone(),
        provision_mode: state.provision_mode.clone(),
        bridge: state.session.snapshot().await.bridge,
        update: crate::update::status(),
    }
}

pub(super) async fn setup_status(State(state): State<ProxyState>) -> Json<SetupStatus> {
    Json(setup_status_value(&state).await)
}

/// 状态推送：本机服务安装/启动进度与桥接就绪一有变化就推一帧完整状态。
/// 进度页与 SPA 订阅它，不再定时轮询。
pub(super) async fn desktop_events(
    State(state): State<ProxyState>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let receiver = state.local_server.subscribe();
    let updates = crate::update::subscribe();
    let stream = futures_util::stream::unfold(
        (state, receiver, updates, None::<SetupStatus>),
        |(state, mut receiver, mut updates, cached)| async move {
            let service_changed = if cached.is_some() {
                tokio::select! {
                    result = receiver.changed() => { result.ok()?; true },
                    result = updates.changed() => { result.ok()?; false },
                }
            } else {
                true
            };
            // 更新版本/busy 变化只替换内存中的更新字段，不触发本机健康探测。
            // 保持完整帧格式，兼容安装进度页与 SPA 的已有订阅者。
            let mut status = if service_changed {
                setup_status_value(&state).await
            } else {
                cached?
            };
            status.update = updates.borrow_and_update().clone();
            let event = Event::default().json_data(&status).ok()?;
            Some((
                Ok::<_, Infallible>(event),
                (state, receiver, updates, Some(status)),
            ))
        },
    );
    Sse::new(stream).keep_alive(KeepAlive::default())
}

pub(super) async fn start_local_install(
    State(state): State<ProxyState>,
    headers: HeaderMap,
) -> Response {
    if !is_same_origin(
        &headers,
        state.bound_port.load(std::sync::atomic::Ordering::Relaxed),
    ) {
        return (StatusCode::FORBIDDEN, "Cross-origin request rejected").into_response();
    }
    state.local_server.prepare_in_background();
    setup_status(State(state)).await.into_response()
}
