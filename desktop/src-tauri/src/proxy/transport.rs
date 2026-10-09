use super::ProxyState;
use super::BRIDGE_SECRET_HEADER;
use super::BRIDGE_USER_HEADER;
use super::TARGET_HEADER;
use axum::body::Body;
use axum::extract::State;
use axum::http::header;
use axum::http::HeaderMap;
use axum::http::Method;
use axum::http::Request;
use axum::http::StatusCode;
use axum::http::Uri;
use axum::response::IntoResponse;
use axum::response::Response;
pub(super) fn is_same_origin(headers: &HeaderMap, port: u16) -> bool {
    // WebView 指向的是 127.0.0.1；localhost 是同一个回环地址的另一种写法，
    // 一并认，免得换个写法就整站不可用。
    let allowed = [
        format!("http://127.0.0.1:{port}"),
        format!("http://localhost:{port}"),
    ];
    if let Some(origin) = headers.get(header::ORIGIN).and_then(|v| v.to_str().ok()) {
        return allowed.iter().any(|value| value == origin);
    }
    match headers.get("sec-fetch-site").and_then(|v| v.to_str().ok()) {
        // same-origin = 本页面发起；none = 用户直接导航（地址栏 / 壳的跳转哨兵）。
        Some(site) => site.eq_ignore_ascii_case("same-origin") || site.eq_ignore_ascii_case("none"),
        None => false,
    }
}

pub(super) fn is_module_asset_request(method: &Method, path: &str) -> bool {
    if *method != Method::GET && *method != Method::HEAD { return false; }
    let parts: Vec<_> = path.trim_start_matches('/').split('/').collect();
    parts.len() >= 6 && parts[..3] == ["api", "v1", "plugin-resource-assets"]
        && ["local", "cloud"].contains(&parts[3])
        && parts[4].len() == 43
        && parts[4].bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

/// 正式站点及其管理接口只认云端：本机既不再托管站点，也不接受旧客户端遗留的
/// `local` 路由标记，否则同一个站点会在两个后端各存一半状态。
/// 与 `desktop-uos/src/proxy.mjs` 的同名判定保持一致。
pub(super) fn is_cloud_site_path(path: &str) -> bool {
    path == "/site"
        || path.starts_with("/site/")
        || path == "/api/v1/sites"
        || path.starts_with("/api/v1/sites/")
        || path == "/api/v1/applications"
        || path.starts_with("/api/v1/applications/")
        || path == "/applications-mcp"
        || path.starts_with("/applications-mcp/")
}

/// 反代处理器：把 `/api/*` 透传到后端，注入 session cookie，流式回传。
pub(super) async fn proxy_handler(State(state): State<ProxyState>, req: Request<Body>) -> Response {
    let (parts, body) = req.into_parts();
    let method: Method = parts.method;
    let uri: Uri = parts.uri;
    let headers: HeaderMap = parts.headers;

    let path_q = uri.path_and_query().map(|p| p.as_str()).unwrap_or("/");

    if !is_same_origin(
        &headers,
        state.bound_port.load(std::sync::atomic::Ordering::Relaxed),
    ) && !is_module_asset_request(&method, uri.path()) {
        return (StatusCode::FORBIDDEN, "Cross-origin request rejected").into_response();
    }

    // 混合架构（Dual）：前端给「本地项目」的请求打 x-hugagent-target: local，
    // 反代把它们转到当前品牌的本机执行面，其余一律云端。单一形态不路由。
    // <img>/<iframe> 等 src 场景无法带请求头，等价支持 query 参数 ?hg_target=local。
    let to_local = state.hybrid_local
        && !is_cloud_site_path(uri.path())
        && (headers
            .get(TARGET_HEADER)
            .and_then(|v| v.to_str().ok())
            .map(|v| v.eq_ignore_ascii_case("local"))
            .unwrap_or(false)
            || uri
                .query()
                .map(|q| q.split('&').any(|kv| kv == "hg_target=local"))
                .unwrap_or(false)
            || uri.path().starts_with("/api/v1/plugin-resource-assets/local/")
);
    let expected_epoch = state.session.epoch.current();
    let session = state.session.snapshot().await;
    let bridge_user = session.bridge_user;
    let token = session.token;
    if !state.session.epoch.matches(expected_epoch) || !state.session.epoch.is_active() {
        return (StatusCode::CONFLICT, "Desktop session changed").into_response();
    }
    if to_local {
        // 本机后端只认已同步的云端身份；没同步好时转发过去只会得到 401，
        // 前端会把它当成云端会话过期。这里直接说明真实原因。模型是否已下发
        // 不在这里拦：由本机后端在真正调用模型时裁决。
        let sync = session.bridge;
        if !sync.identity_ready
            || (uri.path().ends_with("/agents/responses") && !sync.capabilities_ready)
        {
            let reason = sync
                .error
                .unwrap_or_else(|| "正在同步本机身份、模型与技能，请同步完成后重试".to_string());
            let body = serde_json::json!({
                "code": 503,
                "message": format!("本机执行面尚未就绪：{reason}"),
                "data": serde_json::Value::Null,
            });
            return (StatusCode::SERVICE_UNAVAILABLE, axum::Json(body)).into_response();
        }
    }

    // 请求体按帧透传：上传不再整体驻留内存。
    let has_body = headers
        .get(header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok())
        .map(|n| n > 0)
        .unwrap_or(false)
        || headers.contains_key(header::TRANSFER_ENCODING);

    let sent = {
        let use_local = to_local;
        let base = if use_local {
            &state.local_base
        } else {
            &state.server_base
        };
        let mut rb = state
            .http
            .request(method.clone(), format!("{}{}", base, path_q));

        // 透传请求头，但剔除 hop-by-hop / 由我们重写的头。
        // http 的 HeaderName 已规范化为小写，直接 match 即可，无需再 to_ascii_lowercase。
        for (name, value) in headers.iter() {
            match name.as_str() {
                // host 让 reqwest 按目标地址重置；cookie 我们重新注入；
                // accept-encoding 去掉以拿 identity（避免转发压缩流时还要解码）；
                // content-length / connection 交给 reqwest / axum 自管。
                "host" | "cookie" | "accept-encoding" | "content-length" | "connection" => continue,
                // 路由标记不透传；桥接头只能由壳注入——WebView 带来的一律剥离（防伪造）。
                TARGET_HEADER | BRIDGE_SECRET_HEADER | BRIDGE_USER_HEADER => continue,
                _ => {
                    rb = rb.header(name, value);
                }
            }
        }

        if use_local {
            // 本机路由：注入桥接秘密 + 云端身份（身份桥，见 hybrid.rs / desktop_bridge.py）。
            // 不注入云端会话 cookie——本机后端不认它，身份完全由桥接头承载。
            rb = rb.header(BRIDGE_SECRET_HEADER, &state.bridge_secret);
            if let Some(user) = bridge_user.clone() {
                rb = rb.header(BRIDGE_USER_HEADER, user);
            }
        } else if let Some(tok) = token.clone() {
            // 云端路由：注入会话 cookie（已登录时）——这是整套桌面鉴权的关键一笔。
            rb = rb.header(
                reqwest::header::COOKIE,
                format!("{}={}", state.cookie_name, tok),
            );
        }

        let (uploaded, upload_progress) = tokio::sync::watch::channel(false);
        if has_body {
            let stream = futures_util::stream::unfold(
                (body.into_data_stream(), uploaded),
                |(mut input, progress)| async move {
                    use futures_util::StreamExt;
                    match input.next().await {
                        Some(chunk) => {
                            progress.send_replace(false);
                            Some((chunk, (input, progress)))
                        }
                        None => {
                            progress.send_replace(true);
                            None
                        }
                    }
                },
            );
            rb = rb.body(reqwest::Body::wrap_stream(stream));
        } else {
            uploaded.send_replace(true);
        }
        match super::deadline::response_headers(
            rb.send(),
            upload_progress,
            std::time::Duration::from_secs(120),
        )
        .await
        {
            Ok(result) => result,
            Err(_) => return (StatusCode::GATEWAY_TIMEOUT, "代理上游响应超时").into_response(),
        }
    };

    if !state.session.epoch.matches(expected_epoch) || !state.session.epoch.is_active() {
        return (StatusCode::CONFLICT, "Desktop session changed").into_response();
    }
    match sent {
        Ok(upstream) => {
            let status = upstream.status();
            let mut builder = Response::builder().status(status);

            for (name, value) in upstream.headers().iter() {
                // 这些头与「逐帧流式 + 已解压」语义冲突，去掉让 axum 自管分块。
                match name.as_str() {
                    "connection" | "transfer-encoding" | "content-encoding" | "content-length" => {
                        continue
                    }
                    _ => {
                        builder = builder.header(name, value);
                    }
                }
            }

            // bytes_stream 逐帧产出，SSE 不被缓冲。
            let stream = upstream.bytes_stream();
            match builder.body(Body::from_stream(stream)) {
                Ok(resp) => resp,
                Err(e) => (StatusCode::BAD_GATEWAY, format!("构造响应失败: {e}")).into_response(),
            }
        }
        Err(e) => (StatusCode::BAD_GATEWAY, format!("代理上游失败: {e}")).into_response(),
    }
}

#[cfg(test)]
mod resource_asset_tests {
    use super::*;
    #[test]
    fn opaque_asset_exception_is_capability_scoped_and_read_only() {
        let path = format!("/api/v1/plugin-resource-assets/local/{}/browser/channel.js", "A".repeat(43));
        assert!(is_module_asset_request(&Method::GET, &path));
        assert!(is_module_asset_request(&Method::HEAD, &path));
        assert!(!is_module_asset_request(&Method::POST, &path));
        assert!(!is_module_asset_request(&Method::GET, "/api/v1/me"));
        assert!(!is_module_asset_request(&Method::GET, &path.replace("local", "unknown")));
        assert!(!is_module_asset_request(&Method::GET, &path.replace(&"A".repeat(43), "short")));
    }
}
