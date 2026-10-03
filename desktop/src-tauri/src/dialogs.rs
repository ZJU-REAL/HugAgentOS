use crate::{brand, prefs, Shared};
use tauri::{webview::PageLoadEvent, Manager, WebviewUrl, WebviewWindowBuilder};
pub(crate) fn open_close_confirm(app: &tauri::AppHandle, target_label: &str) {
    // 已经开着就聚焦，别重复弹。
    if let Some(w) = app.get_webview_window("close-confirm") {
        let _ = w.center();
        let _ = w.show();
        let _ = w.set_focus();
        return;
    }
    let port = app.state::<Shared>().port;
    let url = format!("http://127.0.0.1:{}/__desktop/close-confirm", port);
    let parsed = match url::Url::parse(&url) {
        Ok(u) => u,
        Err(_) => return,
    };
    let app_for_nav = app.clone();
    let target_label = target_label.to_string();
    let _ = WebviewWindowBuilder::new(app, "close-confirm", WebviewUrl::External(parsed))
        .title(brand::NAME)
        .inner_size(460.0, 250.0)
        .resizable(false)
        .minimizable(false)
        .maximizable(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .center()
        // WebView2 first paints an empty native surface and only then loads the
        // confirmation HTML. Keep it hidden until the final page-load event so
        // Windows never exposes that white frame.
        .visible(false)
        .focused(false)
        .on_page_load(|window, payload| {
            if matches!(payload.event(), PageLoadEvent::Finished) {
                let _ = window.center();
                let _ = window.show();
                let _ = window.set_focus();
            }
        })
        .on_navigation(move |u| {
            if !crate::navigation::is_shell_origin(u, port) {
                return u.as_str() == "about:blank";
            }
            if u.path() != "/__desktop/close-decide" {
                return u.path() == "/__desktop/close-confirm";
            }
            let mut action = String::new();
            let mut remember = false;
            for (k, v) in u.query_pairs() {
                match k.as_ref() {
                    "action" => action = v.into_owned(),
                    "remember" => remember = v == "1",
                    _ => {}
                }
            }
            let app2 = app_for_nav.clone();
            let target_label = target_label.clone();
            tauri::async_runtime::spawn(async move {
                let exit = action == "exit";
                if remember {
                    let dir = app2.state::<Shared>().config_dir.clone();
                    prefs::save_close_action(
                        &dir,
                        if exit {
                            prefs::CloseAction::Exit
                        } else {
                            prefs::CloseAction::Minimize
                        },
                    );
                }
                if let Some(cw) = app2.get_webview_window("close-confirm") {
                    // Destroy the confirmation window after making it invisible.
                    // Keeping the loaded WebView around races its page-load handler:
                    // it can show itself again after the main window was hidden.
                    let _ = cw.hide();
                    let _ = cw.close();
                }
                if exit {
                    app2.exit(0);
                } else if let Some(mw) = app2.get_webview_window(&target_label) {
                    let _ = mw.hide();
                }
            });
            false
        })
        .build();
}
