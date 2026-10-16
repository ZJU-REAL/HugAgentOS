//! Native operations use a host-only bootstrap secret and never return paths.
use crate::proxy::ProxyState;
use axum::{
    extract::{DefaultBodyLimit, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
    Extension, Json, Router,
};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{Read, Write},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;
const MAX_BYTES: usize = 8 * 1024 * 1024;
#[derive(Clone)]
pub(crate) struct NativePlatform {
    app: tauri::AppHandle,
    pub(crate) key: String,
    grants: Arc<Mutex<HashMap<String, Grant>>>,
    cancelled: Arc<Mutex<HashMap<String, Instant>>>,
    gate: Arc<tokio::sync::Mutex<()>>,
}
struct Grant {
    epoch: u64,
    created: Instant,
    bytes: Vec<u8>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Input {
    op: String,
    #[serde(default, rename = "requestId")]
    request_id: String,
    #[serde(default)]
    url: String,
    #[serde(default)]
    handle: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    content: String,
}
fn random_key() -> String {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).expect("OS entropy unavailable");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
impl NativePlatform {
    pub(crate) fn new(app: tauri::AppHandle) -> Self {
        Self {
            app,
            key: random_key(),
            grants: Default::default(),
            cancelled: Default::default(),
            gate: Arc::new(tokio::sync::Mutex::new(())),
        }
    }
    pub(crate) fn watch_session(&self, session: Arc<crate::session_state::SessionState>) {
        let grants = self.grants.clone();
        tauri::async_runtime::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_millis(100)).await;
                let epoch = session.epoch.current();
                if let Ok(mut items) = grants.lock() {
                    items.retain(|_, g| {
                        session.epoch.is_active()
                            && g.epoch == epoch
                            && g.created.elapsed() < Duration::from_secs(600)
                    });
                }
            }
        });
    }
    pub(crate) fn router(&self) -> Router<ProxyState> {
        Router::new()
            .route("/__desktop/workbench/platform", post(request))
            .layer(DefaultBodyLimit::max(12 * 1024 * 1024))
            .layer(Extension(self.clone()))
    }
}
fn permitted(headers: &HeaderMap, port: u16, key: &str) -> bool {
    let value = |name: &str| headers.get(name).and_then(|x| x.to_str().ok());
    value("origin") == Some(format!("http://127.0.0.1:{port}").as_str())
        && value("sec-fetch-site") == Some("same-origin")
        && value("x-workbench-native") == Some(key)
        && value("content-type") == Some("application/json")
}
fn current(state: &ProxyState, epoch: u64) -> Result<(), &'static str> {
    if state.session.epoch.matches(epoch) && state.session.epoch.is_active() {
        Ok(())
    } else {
        Err("session_changed")
    }
}
async fn request(
    State(state): State<ProxyState>,
    Extension(native): Extension<NativePlatform>,
    headers: HeaderMap,
    Json(input): Json<Input>,
) -> Response {
    let port = state.bound_port.load(std::sync::atomic::Ordering::Relaxed);
    if !permitted(&headers, port, &native.key) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"code":"permission_denied"})),
        )
            .into_response();
    }
    let epoch = state.session.epoch.current();
    let result = operate(&state, &native, epoch, input).await;
    let response = match result {
        Ok(value) => Json(value).into_response(),
        Err(code) => (StatusCode::BAD_REQUEST, Json(json!({"code":code}))).into_response(),
    };
    ([(header::CACHE_CONTROL, "no-store")], response).into_response()
}
async fn operate(
    state: &ProxyState,
    native: &NativePlatform,
    epoch: u64,
    input: Input,
) -> Result<Value, &'static str> {
    current(state, epoch)?;
    if input.op == "logout" {
        crate::session::logout_from_host(native.app.clone());
        return Ok(serde_json::json!({"cleared":true}));
    }
    {
        let mut cancelled = native.cancelled.lock().map_err(|_| "bridge_error")?;
        cancelled.retain(|_, stamp| stamp.elapsed() < Duration::from_secs(120));
        if input.op == "cancel" {
            if input.request_id.len() != 32
                || !input.request_id.bytes().all(|b| b.is_ascii_hexdigit())
            {
                return Err("invalid_params");
            }
            if cancelled.len() >= 128 {
                return Err("resource_limit");
            }
            cancelled.insert(input.request_id, Instant::now());
            return Ok(Value::Null);
        }
    }
    let started = Instant::now();
    let check = || {
        if started.elapsed() > Duration::from_secs(120) {
            return Err("bridge_timeout");
        }
        current(state, epoch)?;
        if native
            .cancelled
            .lock()
            .map_err(|_| "bridge_error")?
            .contains_key(&input.request_id)
        {
            return Err("cancelled");
        }
        Ok(())
    };
    check()?;
    {
        let mut grants = native.grants.lock().map_err(|_| "bridge_error")?;
        grants.retain(|_, g| g.epoch == epoch && g.created.elapsed() < Duration::from_secs(600));
        if input.op == "releaseFile" {
            grants.remove(&input.handle);
            return Ok(Value::Null);
        }
        if input.op == "readFile" {
            let grant = grants.get(&input.handle).ok_or("permission_denied")?;
            check()?;
            return Ok(json!({"content":STANDARD.encode(&grant.bytes)}));
        }
    }
    let _permit = native.gate.try_lock().map_err(|_| "concurrency_limit")?;
    match input.op.as_str() {
        "openExternal" => {
            let url = url::Url::parse(&input.url).map_err(|_| "invalid_url")?;
            if !["https", "http"].contains(&url.scheme())
                || !url.username().is_empty()
                || url.password().is_some()
                || url.as_str().len() > 4096
            {
                return Err("invalid_url");
            }
            let app = native.app.clone();
            let prompt = url.to_string();
            let approved = tokio::task::spawn_blocking(move || {
                app.dialog()
                    .message(prompt)
                    .title("Open external link")
                    .buttons(tauri_plugin_dialog::MessageDialogButtons::OkCancel)
                    .blocking_show()
            })
            .await
            .map_err(|_| "bridge_error")?;
            check()?;
            if !approved {
                return Err("cancelled");
            }
            native
                .app
                .opener()
                .open_url(url.to_string(), None::<String>)
                .map_err(|_| "bridge_error")?;
            Ok(Value::Null)
        }
        "pickFile" => {
            if native.grants.lock().map_err(|_| "bridge_error")?.len() >= 64 {
                return Err("resource_limit");
            }
            let app = native.app.clone();
            let selected = tokio::task::spawn_blocking(move || {
                app.dialog()
                    .file()
                    .set_title("Select a file for this application")
                    .blocking_pick_file()
            })
            .await
            .map_err(|_| "bridge_error")?;
            check()?;
            let Some(selected) = selected else {
                return Ok(Value::Null);
            };
            let path = selected.into_path().map_err(|_| "permission_denied")?;
            let name = path
                .file_name()
                .ok_or("invalid_params")?
                .to_string_lossy()
                .into_owned();
            let bytes = tokio::task::spawn_blocking(move || read_snapshot(&path))
                .await
                .map_err(|_| "bridge_error")??;
            check()?;
            if native
                .grants
                .lock()
                .map_err(|_| "bridge_error")?
                .values()
                .map(|g| g.bytes.len())
                .sum::<usize>()
                + bytes.len()
                > 32 * 1024 * 1024
            {
                return Err("resource_limit");
            }
            let handle = random_key();
            let size = bytes.len();
            native.grants.lock().map_err(|_| "bridge_error")?.insert(
                handle.clone(),
                Grant {
                    epoch,
                    created: Instant::now(),
                    bytes,
                },
            );
            Ok(json!({"handle":handle,"name":name,"size":size}))
        }
        "saveFile" => {
            if input.name.trim().is_empty()
                || input.name.len() > 200
                || input.name == "."
                || input.name == ".."
                || input
                    .name
                    .chars()
                    .any(|c| c == '/' || c == '\\' || c == ':' || c.is_control())
            {
                return Err("invalid_params");
            }
            if input.content.len() > ((MAX_BYTES + 2) / 3) * 4 {
                return Err("file_too_large");
            }
            let bytes = STANDARD
                .decode(input.content)
                .map_err(|_| "invalid_params")?;
            if bytes.len() > MAX_BYTES {
                return Err("file_too_large");
            }
            let app = native.app.clone();
            let name = input.name;
            let selected = tokio::task::spawn_blocking(move || {
                app.dialog()
                    .file()
                    .set_file_name(name)
                    .set_title("Save application file")
                    .blocking_save_file()
            })
            .await
            .map_err(|_| "bridge_error")?;
            check()?;
            let Some(selected) = selected else {
                return Ok(Value::Null);
            };
            let path = selected.into_path().map_err(|_| "permission_denied")?;
            let parent = path.parent().ok_or("invalid_params")?;
            let mut temp = tempfile::NamedTempFile::new_in(parent).map_err(|_| "bridge_error")?;
            temp.write_all(&bytes).map_err(|_| "bridge_error")?;
            temp.as_file().sync_all().map_err(|_| "bridge_error")?;
            check()?;
            temp.persist(path).map_err(|_| "bridge_error")?;
            Ok(json!({"saved":true}))
        }
        _ => Err("unknown_method"),
    }
}
fn read_snapshot(path: &std::path::Path) -> Result<Vec<u8>, &'static str> {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x00200000);
    }
    let file = options.open(path).map_err(|_| "permission_denied")?;
    let info = file.metadata().map_err(|_| "permission_denied")?;
    if !info.is_file() || info.file_type().is_symlink() {
        return Err("permission_denied");
    }
    if info.len() > MAX_BYTES as u64 {
        return Err("file_too_large");
    }
    let mut bytes = Vec::new();
    file.take(MAX_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "bridge_error")?;
    if bytes.len() > MAX_BYTES {
        return Err("file_too_large");
    }
    Ok(bytes)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn null_origin_and_other_listeners_cannot_use_native() {
        let mut headers = HeaderMap::new();
        for (key, value) in [
            ("origin", "http://127.0.0.1:1234"),
            ("sec-fetch-site", "same-origin"),
            ("content-type", "application/json"),
            ("x-workbench-native", "trusted"),
        ] {
            headers.insert(key, value.parse().unwrap());
        }
        assert!(permitted(&headers, 1234, "trusted"));
        assert!(!permitted(&headers, 1235, "trusted"));
        headers.insert("origin", "null".parse().unwrap());
        assert!(!permitted(&headers, 1234, "trusted"));
    }
}
