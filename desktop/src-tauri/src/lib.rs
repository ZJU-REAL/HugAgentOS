//! Desktop shell: browser-confirmed, device-bound login.
//! Browser approval is received over HTTPS polling, independently of external
//! protocol prompts. The custom protocol only focuses an already running app.

mod actions;
mod auth;
mod brand;
mod child_process;
mod config;
mod credential_store;
mod device_login;
mod hybrid;
mod local_payload;
mod local_server;
mod menu;
mod notify;
mod prefs;
mod proxy;
mod update;
mod update_monitor;
mod window_events;
mod window_labels;

mod app;
mod bootstrap;
#[cfg(not(target_os = "macos"))]
mod dialogs;
mod display;
mod navigation;
mod session;
mod session_state;
mod tray;
mod windows;
pub use app::run;
use std::sync::Arc;
/// 跨组件共享的运行时状态（经 Tauri manage 注入）。
pub(crate) struct Shared {
    pub(crate) session: Arc<session_state::SessionState>,
    pub(crate) server_base: String,
    pub(crate) update_base: String,
    pub(crate) http: reqwest::Client,
    pub(crate) port: u16,
    pub(crate) config_dir: std::path::PathBuf,
    /// 当前缩放档位（`f64` 的位表示）。真值仍是 prefs.json，这里是运行时副本。
    pub(crate) ui_zoom: std::sync::atomic::AtomicU64,
    pub(crate) local_server: Arc<local_server::LocalServerManager>,
    /// 混合架构（Dual）的连接设置；账号及桥接快照由 session 独占管理。
    pub(crate) cookie_name: String,
    pub(crate) hybrid_local: bool,
    pub(crate) bridge_secret: String,
    pub(crate) device_id: String,
    pub(crate) device_login: Arc<device_login::Login>,
}

impl Shared {
    fn bridge_context(&self) -> hybrid::BridgeContext {
        hybrid::BridgeContext {
            http: self.http.clone(),
            cloud_base: self.server_base.trim_end_matches('/').into(),
            cookie_name: self.cookie_name.clone(),
            session: self.session.clone(),
            bridge_secret: self.bridge_secret.clone(),
            local_server: self.local_server.clone(),
            device_id: self.device_id.clone(),
        }
    }

    fn home_url(&self) -> String {
        format!("http://127.0.0.1:{}/", self.port)
    }
    /// 登录页「等待态」：浏览器已自动拉起，页面显示 spinner（启动 / 会话过期走这里）。
    fn waiting_url(&self) -> String {
        format!("http://127.0.0.1:{}/__desktop/login?waiting=1", self.port)
    }
    /// 登录页「初始态」：显示「登录」按钮，等用户点击再开浏览器（退出登录走这里）。
    fn login_idle_url(&self) -> String {
        format!("http://127.0.0.1:{}/__desktop/login", self.port)
    }
}
