//! Native event-loop regression. Uses an isolated HTTP page and no user service/data.
use crate::{auth, device_login, hybrid, local_server, Shared};
use std::sync::{atomic::AtomicU64, mpsc, Arc};
use std::time::Duration;
use tauri::Manager;
use tokio::sync::RwLock;

#[test]
#[ignore = "requires a native desktop session; run alone with an external timeout"]
fn native_new_window_loads_from_menu_navigation() {
    let root = std::env::temp_dir().join(format!(
        "desktop-new-window-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir(&root).unwrap();
    let config_dir = root.clone();
    let (rendered_tx, rendered_rx) = mpsc::channel::<String>();
    let (done_tx, done_rx) = mpsc::channel();
    // A WebView2 deadlock also prevents app.exit()/window.url() from completing.
    // This ignored test must run in its own process so a regression fails boundedly.
    std::thread::spawn(move || {
        if done_rx.recv_timeout(Duration::from_secs(35)).is_err() {
            eprintln!("FAIL: new window did not render; native event loop may be blocked");
            std::process::exit(1);
        }
    });
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    listener.set_nonblocking(true).unwrap();
    let server = tauri::async_runtime::spawn(async move {
        let listener = tokio::net::TcpListener::from_std(listener).unwrap();
        let report = rendered_tx.clone();
        let router = axum::Router::new()
            .route(
                "/rendered/:label",
                axum::routing::get(
                    move |axum::extract::Path(label): axum::extract::Path<String>| {
                        let report = report.clone();
                        async move {
                            let _ = report.send(label);
                            "ok"
                        }
                    },
                ),
            )
            .fallback(axum::routing::get(|| async {
                axum::response::Html(
                    "<!doctype html><html><body><h1 id='ready'>Window loaded</h1></body></html>",
                )
            }));
        axum::serve(listener, router).await.unwrap();
    });
    let mut context = tauri::generate_context!();
    context.config_mut().identifier = "test.desktop-new-window.native".into();
    let app = tauri::Builder::default()
        .any_thread()
        .plugin(tauri_plugin_dialog::init())
        .on_page_load(|window, payload| {
            if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) {
                let report = serde_json::to_string(&format!("/rendered/{}", window.label())).unwrap();
                window.eval(format!(
                    "if(document.getElementById('ready')?.textContent==='Window loaded') fetch({report});"
                )).unwrap();
            }
        })
        .setup(move |app| {
            let http = reqwest::Client::new();
            let local_server = local_server::LocalServerManager::new(
                config_dir.join("local-server"), config_dir.join("data"),
                config_dir.join("server.zip"), config_dir.join("server.json"),
                config_dir.join("runtime.tar.gz"), config_dir.join("runtime.json"), http.clone(),
            );
            app.manage(Shared {
                server_base: format!("http://127.0.0.1:{port}"),
                update_base: format!("http://127.0.0.1:{port}"),
                token: Arc::new(RwLock::new(None)),
                http, port, config_dir,
                ui_zoom: AtomicU64::new(1.0_f64.to_bits()),
                local_server, cookie_name: "test_session".into(), hybrid_local: false,
                bridge_secret: String::new(),
                bridge_user: Arc::new(RwLock::new(None)),
                bridge_sync: Arc::new(RwLock::new(hybrid::BridgeSync::default())),
                session_epoch: Arc::new(auth::SessionEpoch::default()),
                device_id: "test-device".into(),
                device_login: Arc::new(device_login::Login::default()),
            });
            // Initial construction is legal in setup; subsequent windows must come
            // through the real navigation -> menu dispatch -> window factory path.
            crate::build_window(app.handle(), "main", &format!("http://127.0.0.1:{port}/"))?;
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let check = (|| -> Result<(), String> {
                    let receive = || rendered_rx.recv_timeout(Duration::from_secs(10))
                        .map_err(|_| "window DOM did not load".to_string());
                    if receive()? != "main" { return Err("initial window missing".into()); }
                    let main = handle.get_webview_window("main").ok_or("main missing")?;
                    for _ in 0..2 {
                        main.eval("window.location.href='/__desktop/menu?action=new_window'")
                            .map_err(|e| e.to_string())?;
                        let label = receive()?;
                        if !label.starts_with("main-") { return Err(format!("unexpected window: {label}")); }
                        let window = handle.get_webview_window(&label).ok_or("new window missing")?;
                        if !window.is_visible().map_err(|e| e.to_string())? {
                            return Err("new window is hidden".into());
                        }
                    }
                    if handle.webview_windows().len() != 3 {
                        return Err("repeated New Window did not keep three windows".into());
                    }
                    // Verify the original page/event loop is still responsive.
                    main.eval("fetch('/rendered/original-responsive')").map_err(|e| e.to_string())?;
                    if receive()? != "original-responsive" { return Err("original window unresponsive".into()); }
                    Ok(())
                })();
                match check {
                    Ok(()) => { println!("PASS: two new windows rendered; original remains responsive"); handle.exit(0); }
                    Err(error) => { eprintln!("FAIL: {error}"); std::process::exit(1); }
                }
            });
            Ok(())
        })
        .build(context).expect("create native test app");
    let code = app.run_return(|_, _| {});
    server.abort();
    done_tx.send(()).unwrap();
    // Only this test's uniquely-created fixture is removed.
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(code, 0);
}
