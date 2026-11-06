use crate::auth::SessionVerdict;
use crate::device_login::LoginError;
use crate::windows::{navigate_session_windows, show_main_window};
use crate::{auth, device_login, hybrid, net, Shared};
use std::sync::atomic::Ordering;
use tauri::Manager;
use tauri_plugin_opener::OpenerExt;
/// Only the authenticated host proxy calls this; no custom IPC command is registered.
/// Invalidate the identity before async server cleanup and return the HTTP acknowledgement.
pub(crate) fn logout_from_host(app: tauri::AppHandle) {
    let shared = app.state::<Shared>();
    cancel_device_login(&shared);
    let expected = shared.session.epoch.advance();
    drop(shared);
    tauri::async_runtime::spawn(async move {
        let shared = app.state::<Shared>();
        clear_invalidated_session(&shared, expected).await;
        if !shared.session.epoch.matches(expected) {
            return;
        }
        navigate_session_windows(&app, &shared.login_idle_url());
    });
}

/// Invalidate async tasks before waiting for writes; revoke the old cloud session.
pub(crate) async fn clear_desktop_session(shared: &Shared) -> u64 {
    cancel_device_login(shared);
    let expected = shared.session.epoch.advance();
    clear_invalidated_session(shared, expected).await;
    expected
}

async fn clear_invalidated_session(shared: &Shared, expected: u64) {
    let old_token = {
        let _write = shared.session.epoch.local_write.lock().await;
        if !shared.session.epoch.matches(expected) {
            return;
        }
        let old_token = shared.session.clear(expected).await;
        auth::save_token(&shared.config_dir, &shared.server_base, None);
        if shared.hybrid_local {
            if let Err(error) =
                hybrid::clear_cloud_bridge(&shared.http, &shared.bridge_secret).await
            {
                eprintln!("[auth] {error}");
            }
        }
        old_token
    };
    // 桥接状态被清空也是一次状态变化：推一帧给订阅者，前端不会停在退出登录前的
    // 就绪态，上一轮登录里还在等本机服务的协程也能立刻醒来发现会话已换、退出。
    shared.local_server.notify_changed();
    if let Some(token) = old_token {
        let response = shared
            .http
            .post(format!(
                "{}/api/v1/auth/logout",
                shared.server_base.trim_end_matches('/')
            ))
            .header(
                reqwest::header::COOKIE,
                format!("{}={token}", shared.cookie_name),
            )
            .timeout(net::CLOUD_REQUEST_TIMEOUT)
            .send()
            .await;
        match response {
            Ok(response) if response.status().is_success() => {}
            Ok(response) => eprintln!("[auth] 云端退出 HTTP {}", response.status()),
            Err(_) => eprintln!("[auth] 云端退出暂不可达；本机身份已清除"),
        }
    }
}

/// Asks the cloud about the saved session, retrying transient failures. `None`
/// means undecided (or the epoch moved on): the token is kept for the next try.
async fn verify_saved_session(shared: &Shared, epoch: u64, token: &str) -> Option<SessionVerdict> {
    net::retry_transient(
        || shared.session.epoch.matches(epoch),
        || {
            auth::check(
                &shared.http,
                &shared.server_base,
                &shared.cookie_name,
                token,
            )
        },
    )
    .await
}

/// The cloud explicitly rejected the saved session: drop it and show the login card.
async fn drop_rejected_session(app: &tauri::AppHandle, shared: &Shared, epoch: u64) {
    let Some(expected) = shared.session.epoch.advance_if_current(epoch) else {
        return;
    };
    cancel_device_login(shared);
    clear_invalidated_session(shared, expected).await;
    if shared.session.epoch.matches(expected) {
        navigate_session_windows(app, &shared.login_idle_url());
    }
}

/// Cloud backends: the window already opened on what local state suggested, so
/// this only corrects it on a settled answer. An unreachable cloud lands on the
/// setup page; an undecided session check keeps the user where they are.
pub(crate) fn spawn_startup_probe(app: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        let shared = app.state::<Shared>();
        let epoch = shared.session.epoch.current();
        let alive = || shared.session.epoch.matches(epoch);
        let reachable = net::cloud_reachable(&shared.http, &shared.server_base, alive).await;
        if !alive() {
            return;
        }
        if !reachable {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.eval(format!(
                    "window.location.replace('http://127.0.0.1:{}/__desktop/setup')",
                    shared.port
                ));
            }
            return;
        }
        let Some(token) = shared.session.token().await else {
            return;
        };
        if verify_saved_session(&shared, epoch, &token).await == Some(SessionVerdict::Rejected) {
            drop_rejected_session(&app, &shared, epoch).await;
        }
    });
}

/// LocalOnly: the backend is the local server this process is still starting,
/// so the saved session can only be checked once it is up. A valid session goes
/// straight to the app; a rejected one is cleared and lands on the login card.
pub(crate) fn spawn_local_session_restore(app: tauri::AppHandle) {
    /// The setup page waits while this is set; clearing it wakes the page to route itself.
    struct Restoring(tauri::AppHandle);
    impl Drop for Restoring {
        fn drop(&mut self) {
            let shared = self.0.state::<Shared>();
            shared.session.restoring.store(false, Ordering::SeqCst);
            shared.local_server.notify_changed();
        }
    }
    if !app.state::<Shared>().session.has_token() {
        return;
    }
    app.state::<Shared>()
        .session
        .restoring
        .store(true, Ordering::SeqCst);
    tauri::async_runtime::spawn(async move {
        let _restoring = Restoring(app.clone());
        let shared = app.state::<Shared>();
        let epoch = shared.session.epoch.current();
        let Some(token) = shared.session.token().await else {
            return;
        };
        let mut changes = shared.local_server.subscribe();
        while !shared.local_server.reported_ready().await {
            if !shared.session.epoch.matches(epoch) || changes.changed().await.is_err() {
                return;
            }
        }
        let verdict = verify_saved_session(&shared, epoch, &token).await;
        if !shared.session.epoch.matches(epoch) {
            return;
        }
        match verdict {
            Some(SessionVerdict::Valid) => navigate_session_windows(&app, &shared.home_url()),
            // Undecided: keep the token, so the next launch can try again.
            None => navigate_session_windows(&app, &shared.login_idle_url()),
            Some(SessionVerdict::Rejected) => drop_rejected_session(&app, &shared, epoch).await,
        }
    });
}

/// Cancel immediately in memory; server cleanup cannot block logout/navigation.
pub(crate) fn cancel_device_login(shared: &Shared) {
    let cancelled_generation = shared.device_login.generation();
    shared.device_login.invalidate();
    let login = shared.device_login.clone();
    let http = shared.http.clone();
    let base = shared.server_base.clone();
    tauri::async_runtime::spawn(async move {
        let old = {
            let mut slot = login.attempt.write().await;
            if slot
                .as_ref()
                .is_some_and(|a| a.generation <= cancelled_generation)
            {
                slot.take()
            } else {
                None
            }
        };
        if let Some(attempt) = old {
            let _ = device_login::action(&http, &base, &attempt, "cancel").await;
        }
    });
}

/// Starting a new attempt also clears any prior accepted-but-cancelled session.
pub(crate) async fn reset_login_state(shared: &Shared, expected: u64) -> bool {
    let _write = shared.session.epoch.local_write.lock().await;
    if !shared.session.epoch.matches(expected) {
        return false;
    }
    shared.session.clear(expected).await;
    auth::save_token(&shared.config_dir, &shared.server_base, None);
    if shared.hybrid_local {
        let _ = hybrid::clear_cloud_bridge(&shared.http, &shared.bridge_secret).await;
    }
    shared.local_server.notify_changed();
    shared.session.epoch.matches(expected)
}

pub(crate) fn start_device_login(app: tauri::AppHandle) {
    let requested_generation = app.state::<Shared>().device_login.generation();
    tauri::async_runtime::spawn(async move {
        let shared = app.state::<Shared>();
        let login = shared.device_login.clone();
        let _start = login.start_lock.lock().await;
        if login.generation() != requested_generation {
            return;
        }
        let existing = login.attempt.read().await.clone();
        if let Some(attempt) =
            existing.filter(|a| login.matches(a) && shared.session.epoch.matches(a.epoch))
        {
            if let Ok(url) = device_login::browser_url(&shared.server_base, &attempt.grant) {
                let _ = app.opener().open_url(url, None::<String>);
            }
            return;
        }
        login.invalidate();
        let generation = login.generation();
        let epoch = shared.session.epoch.advance();
        *login.view.write().await = device_login::View {
            status: "starting".into(),
            message: "正在创建登录请求…".into(),
            ..Default::default()
        };
        navigate_session_windows(&app, &shared.waiting_url());
        if !reset_login_state(&shared, epoch).await || login.generation() != generation {
            return;
        }
        let secret = match device_login::new_secret() {
            Ok(secret) => secret,
            Err(message) => {
                *login.view.write().await = device_login::View {
                    status: "error".into(),
                    message,
                    ..Default::default()
                };
                return;
            }
        };
        // A timeout may still have created a request server-side; repeating is safe because
        // each POST yields a new request id and the orphan simply expires unclaimed.
        let begun = net::retry_transient(
            || login.generation() == generation,
            || async {
                let started = std::time::Instant::now();
                match device_login::begin(&shared.http, &shared.server_base, &secret).await {
                    Err(error @ LoginError::Unreachable) => {
                        login.view.write().await.message = error.to_string();
                        None
                    }
                    settled => Some(settled.map(|grant| (grant, started))),
                }
            },
        )
        .await;
        let (grant, started) = match begun {
            Some(Ok(begun)) => begun,
            // A stale attempt's success still reaches the cancel below; its failure is moot.
            _ if login.generation() != generation => return,
            failure => {
                *login.view.write().await = device_login::View {
                    status: "error".into(),
                    message: match failure {
                        Some(Err(error)) => error.to_string(),
                        _ => "无法连接登录服务器，请检查网络后重新登录。".into(),
                    },
                    ..Default::default()
                };
                return;
            }
        };
        let attempt = device_login::Attempt {
            deadline: started + std::time::Duration::from_secs(grant.expires_in),
            grant,
            secret,
            generation,
            epoch,
        };
        if login.generation() != generation || !shared.session.epoch.matches(epoch) {
            let _ =
                device_login::action(&shared.http, &shared.server_base, &attempt, "cancel").await;
            return;
        }
        *login.attempt.write().await = Some(attempt.clone());
        *login.view.write().await = device_login::View {
            status: "pending".into(),
            message: "请在浏览器核对账号和核对码，确认登录本桌面端。".into(),
            confirm_code: attempt.grant.confirm_code.clone(),
        };
        if let Ok(url) = device_login::browser_url(&shared.server_base, &attempt.grant) {
            if app.opener().open_url(url, None::<String>).is_err() {
                login.view.write().await.message = "浏览器未能打开，请点击重新打开。".into();
            }
        }
        drop(_start);
        let mut delay = attempt.grant.interval.clamp(2, 10);
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(delay)).await;
            if !login.matches(&attempt) || !shared.session.epoch.matches(epoch) {
                break;
            }
            let result =
                device_login::action(&shared.http, &shared.server_base, &attempt, "poll").await;
            if !login.matches(&attempt) || !shared.session.epoch.matches(epoch) {
                break;
            }
            match result {
                Ok(data) => {
                    delay = attempt.grant.interval.clamp(2, 10);
                    match data["status"].as_str().unwrap_or("") {
                        "pending" => {
                            login.view.write().await.message = "等待浏览器确认登录…".into();
                        }
                        "delivered" => {
                            let Some(token) = data["token"].as_str().filter(|s| !s.is_empty())
                            else {
                                break;
                            };
                            // The server's cookie name must match the configured session namespace.
                            if data["cookie_name"].as_str() != Some(shared.cookie_name.as_str()) {
                                login.view.write().await.message =
                                    "登录会话配置不匹配，请联系管理员。".into();
                                break;
                            }
                            let _write = shared.session.epoch.local_write.lock().await;
                            if !login.matches(&attempt) || !shared.session.epoch.matches(epoch) {
                                break;
                            }
                            if !shared.session.accept(epoch, token.to_string()).await {
                                break;
                            }
                            auth::save_token(&shared.config_dir, &shared.server_base, Some(token));
                            shared.session.epoch.activate(epoch);
                            drop(_write);
                            if shared.hybrid_local {
                                hybrid::on_cloud_login(shared.bridge_context(), epoch);
                            }
                            if login.generation() != generation
                                || !shared.session.epoch.matches(epoch)
                            {
                                return;
                            }
                            *login.view.write().await = device_login::View {
                                status: "completed".into(),
                                message: "登录成功".into(),
                                confirm_code: attempt.grant.confirm_code.clone(),
                            };
                            login.attempt.write().await.take();
                            navigate_session_windows(&app, &shared.home_url());
                            show_main_window(&app);
                            // A lost ack never delays the user entering the app.
                            for _ in 0..3 {
                                if !login.matches(&attempt) || !shared.session.epoch.matches(epoch)
                                {
                                    break;
                                }
                                if device_login::action(
                                    &shared.http,
                                    &shared.server_base,
                                    &attempt,
                                    "ack",
                                )
                                .await
                                .is_ok()
                                {
                                    break;
                                }
                            }
                            return;
                        }
                        "cancelled" | "denied" | "completed" => {
                            login.view.write().await.message =
                                "登录已取消或结束，请重新登录。".into();
                            break;
                        }
                        _ => {
                            login.view.write().await.message = "登录响应无效，请重试。".into();
                            break;
                        }
                    }
                }
                Err(LoginError::Expired) => break,
                Err(error) => {
                    login.view.write().await.message = error.to_string();
                    delay = (delay * 2).min(10);
                }
            }
        }
        if login.generation() == generation {
            let mut view = login.view.write().await;
            view.status = "error".into();
            if std::time::Instant::now() >= attempt.deadline {
                view.message = "登录请求已过期，请重新登录。".into();
            }
            login.attempt.write().await.take();
        }
        let _ = device_login::action(&shared.http, &shared.server_base, &attempt, "cancel").await;
    });
}

/// External protocols only focus the window; credentials arrive over the bound request.
pub(crate) fn handle_deep_link(app: &tauri::AppHandle, raw_url: String) {
    if let Ok(url) = url::Url::parse(&raw_url) {
        if url.scheme() == "hugagent" && url.host_str() == Some("auth") && url.path() == "/focus" {
            show_main_window(app);
        }
    }
}
