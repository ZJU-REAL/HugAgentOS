//! Resource WebSockets use the same account and local/cloud routing as HTTP.
use super::{is_same_origin, ProxyState, BRIDGE_SECRET_HEADER, BRIDGE_USER_HEADER};
use axum::{
    extract::{ws::{Message, WebSocketUpgrade}, OriginalUri, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::{connect_async_tls_with_config, Connector, tungstenite::{client::IntoClientRequest, Message as UpstreamMessage}};

pub(super) async fn upgrade(
    State(state): State<ProxyState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    ws: WebSocketUpgrade,
) -> Response {
    let port = state.bound_port.load(std::sync::atomic::Ordering::Relaxed);
    if !is_same_origin(&headers, port) {
        return (StatusCode::FORBIDDEN, "Cross-origin request rejected").into_response();
    }
    let expected = state.session.epoch.current();
    let snapshot = state.session.snapshot().await;
    let local = state.hybrid_local && uri.query().map(|q| q.split('&').any(|x| x == "hg_target=local")).unwrap_or(false);
    if local && !snapshot.bridge.identity_ready {
        return (StatusCode::SERVICE_UNAVAILABLE, "Local identity is not ready").into_response();
    }
    let base = if local { &state.local_base } else { &state.server_base };
    let url = format!("{}{}", base, uri.path_and_query().map(|x| x.as_str()).unwrap_or("/"))
        .replacen("https://", "wss://", 1).replacen("http://", "ws://", 1);
    let mut request = match url.into_client_request() {
        Ok(value) => value,
        Err(_) => return (StatusCode::BAD_GATEWAY, "Invalid upstream").into_response(),
    };
    if let Some(origin) = headers.get("origin") {
        request.headers_mut().insert("origin", origin.clone());
    }
    let mut add = |name: &'static str, value: String| -> bool {
        match value.parse() {
            Ok(value) => { request.headers_mut().insert(name, value); true }
            Err(_) => false,
        }
    };
    if local {
        if !add(BRIDGE_SECRET_HEADER, state.bridge_secret.clone()) {
            return StatusCode::BAD_GATEWAY.into_response();
        }
        if let Some(user) = snapshot.bridge_user {
            if !add(BRIDGE_USER_HEADER, user) { return StatusCode::BAD_GATEWAY.into_response(); }
        }
    } else if let Some(token) = snapshot.token {
        if !add("cookie", format!("{}={}", state.cookie_name, token)) {
            return StatusCode::BAD_GATEWAY.into_response();
        }
    }
    let mut tls = native_tls::TlsConnector::builder();
    tls.danger_accept_invalid_certs(state.insecure_tls);
    let connector = match tls.build() {
        Ok(tls) => Connector::NativeTls(tls),
        Err(_) => return StatusCode::BAD_GATEWAY.into_response(),
    };
    let (upstream, _) = match tokio::time::timeout(std::time::Duration::from_secs(15), connect_async_tls_with_config(request, None, false, Some(connector))).await {
        Ok(Ok(value)) => value,
        _ => return (StatusCode::BAD_GATEWAY, "WebSocket upstream unavailable").into_response(),
    };
    if !state.session.epoch.matches(expected) || !state.session.epoch.is_active() {
        return StatusCode::CONFLICT.into_response();
    }
    ws.max_message_size(24 * 1024 * 1024).on_upgrade(move |socket| async move {
        let (mut client_send, mut client_receive) = socket.split();
        let (mut upstream_send, mut upstream_receive) = upstream.split();
        let downstream = async {
            while let Some(Ok(message)) = upstream_receive.next().await {
                let translated = match message {
                    UpstreamMessage::Text(x) => Message::Text(x),
                    UpstreamMessage::Binary(x) => Message::Binary(x),
                    UpstreamMessage::Ping(x) => Message::Ping(x),
                    UpstreamMessage::Pong(x) => Message::Pong(x),
                    UpstreamMessage::Close(frame) => {
                        let frame = frame.map(|f| axum::extract::ws::CloseFrame { code: f.code.into(), reason: f.reason });
                        let _ = client_send.send(Message::Close(frame)).await;
                        break;
                    },
                    _ => continue,
                };
                if client_send.send(translated).await.is_err() { break; }
            }
        };
        let outgoing = async {
            while let Some(Ok(message)) = client_receive.next().await {
                let translated = match message {
                    Message::Text(x) => UpstreamMessage::Text(x),
                    Message::Binary(x) => UpstreamMessage::Binary(x),
                    Message::Ping(x) => UpstreamMessage::Ping(x),
                    Message::Pong(x) => UpstreamMessage::Pong(x),
                    Message::Close(frame) => {
                        let frame = frame.map(|f| tokio_tungstenite::tungstenite::protocol::CloseFrame { code: f.code.into(), reason: f.reason });
                        let _ = upstream_send.send(UpstreamMessage::Close(frame)).await;
                        break;
                    },
                };
                if upstream_send.send(translated).await.is_err() { break; }
            }
        };
        let account = async {
            loop {
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                if !state.session.epoch.matches(expected) || !state.session.epoch.is_active() { break; }
            }
        };
        tokio::select! { _ = downstream => {}, _ = outgoing => {}, _ = account => {} }
    }).into_response()
}
