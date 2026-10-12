#[test]
fn update_attempts_preserve_offer_on_cancel_or_error_and_allow_retry() {
    use super::*;
    let mut events = subscribe();
    events.borrow_and_update();
    set_available(Some("2.0.0".into()));
    assert!(
        events.has_changed().unwrap(),
        "new version must wake the event stream"
    );
    events.borrow_and_update();
    set_available(Some("2.0.0".into()));
    assert!(
        !events.has_changed().unwrap(),
        "unchanged state must not wake the UI"
    );
    {
        let _attempt = begin().unwrap();
        assert!(status().busy);
        assert!(
            begin().is_none(),
            "double-click must not launch another update"
        );
    }
    assert!(!status().busy);
    assert_eq!(status().available_version.as_deref(), Some("2.0.0"));
    let retry = begin().expect("cancel/error releases the update gate");
    drop(retry);
    set_available(None);
    assert!(status().available_version.is_none());
    assert_eq!(
        version_message("1.0.2", None),
        "当前版本：1.0.2\n当前平台暂无可用更新。"
    );
    assert_eq!(
        version_message("1.0.2", Some("2.0.0")),
        "当前版本：1.0.2\n发现新版本：2.0.0"
    );
}

/// Run on a Windows build host with a real WebView2 runtime. This exercises the same
/// window factory used after update confirmation without downloading or installing anything.
#[cfg(target_os = "windows")]
#[test]
#[ignore = "requires a native Windows WebView2 session"]
fn native_update_progress_window_loads_and_renders() {
    use super::*;
    use std::sync::{Arc, Mutex};
    // A blocked native event loop can also block window.url() and app.exit().
    // Run this ignored test alone so a broken WebView fails within a budget.
    let (done_tx, done_rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        if done_rx
            .recv_timeout(std::time::Duration::from_secs(60))
            .is_err()
        {
            eprintln!("FAIL: update progress test exceeded its native event-loop budget");
            std::process::exit(1);
        }
    });
    let outcome = Arc::new(Mutex::new(None));
    let result = outcome.clone();
    let mut context = tauri::generate_context!();
    context.config_mut().identifier = "test.updater-progress.native".into();
    let app = tauri::Builder::default()
            .any_thread()
            .setup(move |app| {
                let app = app.handle().clone();
                std::thread::spawn(move || {
                    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
                    let port = listener.local_addr().unwrap().port();
                    let (rendered_tx, rendered_rx) = std::sync::mpsc::channel();
                    listener.set_nonblocking(true).unwrap();
                    let server = tauri::async_runtime::spawn(async move {
                        let listener = tokio::net::TcpListener::from_std(listener).unwrap();
                        let router = axum::Router::new().route(
                            "/__desktop/update-progress",
                            axum::routing::get(progress_page),
                        ).route("/rendered", axum::routing::get(move || {
                            let sender = rendered_tx.clone();
                            async move { let _ = sender.send(()); "ok" }
                        }));
                        axum::serve(listener, router).await.unwrap();
                    });
                    let check = (|| -> Result<(), String> {
                        let window = build_progress_window(&app, port)?;
                        let result = (|| -> Result<(), String> {
                            if window.is_always_on_top().map_err(|e| e.to_string())?
                                || !window.is_minimizable().map_err(|e| e.to_string())?
                            {
                                return Err("Update progress must be minimizable and not always on top".into());
                            }
                            let wait_minimized = |expected| -> Result<(), String> {
                                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                                loop {
                                    if window.is_minimized().map_err(|e| e.to_string())? == expected {
                                        return Ok(());
                                    }
                                    if std::time::Instant::now() >= deadline {
                                        return Err(format!("Window did not reach minimized={expected}"));
                                    }
                                    std::thread::sleep(std::time::Duration::from_millis(20));
                                }
                            };
                            window.minimize().map_err(|e| e.to_string())?;
                            wait_minimized(true)?;
                            window.eval(
                                "window.__set(42,4.2,10); if(document.getElementById('p').textContent==='42%') fetch('/rendered');"
                            ).map_err(|e| e.to_string())?;
                            rendered_rx.recv_timeout(std::time::Duration::from_secs(10))
                                .map_err(|_| "Progress did not update while minimized".to_string())?;
                            if !window.is_minimized().map_err(|e| e.to_string())? {
                                return Err("Progress update restored the window".into());
                            }
                            window.unminimize().map_err(|e| e.to_string())?;
                            wait_minimized(false)?;
                            window.eval(
                                "if(document.getElementById('p').textContent==='42%' && document.getElementById('s').textContent==='4.2 / 10.0 MB' && typeof window.__done==='function' && typeof window.__fail==='function') fetch('/rendered');"
                            ).map_err(|e| e.to_string())?;
                            rendered_rx.recv_timeout(std::time::Duration::from_secs(10))
                                .map_err(|_| "Restored window lost the current progress".to_string())?;
                            Ok(())
                        })();
                        let _ = window.close();
                        result
                    })();
                    server.abort();
                    let code = if check.is_ok() { 0 } else { 1 };
                    *result.lock().unwrap() = Some(check);
                    app.exit(code);
                });
                Ok(())
            })
            .build(context)
            .expect("create native test app");
    app.run_return(|_, _| {});
    let _ = done_tx.send(());
    outcome
        .lock()
        .unwrap()
        .take()
        .expect("native check completed")
        .expect("update progress window must load and render");
}

#[test]
fn windows_update_is_unattended() {
    let config: serde_json::Value =
        serde_json::from_str(include_str!("../../tauri.conf.json")).unwrap();
    assert_eq!(
        config["plugins"]["updater"]["windows"]["installMode"],
        "passive"
    );
}
