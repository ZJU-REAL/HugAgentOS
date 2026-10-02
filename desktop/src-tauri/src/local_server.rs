//! 桌面端托管的无 Docker 本机服务。
//!
//! Windows、macOS 与 Linux 安装包携带同版本 CE 派生树和私有 Python 运行时。
//! 这里负责离线安装、启动服务、轮询健康状态，并在桌面进程退出时回收整个进程组。
//! 运行环境位于应用本地数据目录；macOS/Linux 业务数据统一放在 ``~/.hugagent``。

use crate::local_payload::{self, PayloadPaths};
use serde::Serialize;
use std::collections::VecDeque;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::{watch, RwLock};

mod backup;
mod data;
mod installer;
mod logging;
mod managed_process;
mod process;
use managed_process::ManagedProcess;
mod supervisor;
#[cfg(test)]
use data::migrate_legacy_data_dir;
pub use data::resolve_local_server_data_dir;
use logging::*;
#[cfg(test)]
mod tests;
pub const LOCAL_SERVER_PORT: u16 = crate::brand::LOCAL_SERVER_PORT;

/// 本机后端基址。端口是白标可配的（见 `brand.rs`），所以只能在运行时拼，不能再当常量用。
pub fn local_server_base() -> String {
    format!("http://127.0.0.1:{LOCAL_SERVER_PORT}")
}
const MAX_LOG_LINES: usize = 80;
#[derive(Clone, Debug, Serialize)]
pub struct LocalServerStatus {
    pub phase: String,
    pub progress: u8,
    pub message: String,
    pub logs: Vec<String>,
    pub installed: bool,
    pub ready: bool,
    pub supported: bool,
    pub server_base: String,
}

impl Default for LocalServerStatus {
    fn default() -> Self {
        Self {
            phase: "idle".to_string(),
            progress: 0,
            message: "尚未安装本机服务".to_string(),
            logs: Vec::new(),
            installed: false,
            ready: false,
            supported: local_payload::current_target() != "unsupported",
            server_base: local_server_base(),
        }
    }
}

pub struct LocalServerManager {
    root: PathBuf,
    data_root: PathBuf,
    bundle_archive: PathBuf,
    bundle_manifest: PathBuf,
    runtime_archive: PathBuf,
    runtime_manifest: PathBuf,
    http: reqwest::Client,
    status: RwLock<LocalServerStatus>,
    /// Bumped on every status/log change; the shell pages and the SPA subscribe
    /// instead of polling.
    status_version: watch::Sender<u64>,
    child: Mutex<Option<ManagedProcess>>,
    install_running: AtomicBool,
    shutting_down: Arc<AtomicBool>,
    start_lock: tokio::sync::Mutex<()>,
    health_monitor: AtomicBool,
    ready_cache: tokio::sync::Mutex<Option<(std::time::Instant, bool)>>,
    /// 混合架构（P2 身份桥）：桌面壳生成的桥接秘密。设置后孵化本机后端时注入
    /// `HUGAGENT_DESKTOP_BRIDGE_SECRET`（身份桥）与 `CONFIG_TOKEN`（壳持有本机
    /// 实例的控制台令牌，用于安全模型清单 / capability gateway 下发）。
    bridge_secret: std::sync::OnceLock<String>,
}

impl LocalServerManager {
    pub fn new(
        root: PathBuf,
        data_root: PathBuf,
        bundle_archive: PathBuf,
        bundle_manifest: PathBuf,
        runtime_archive: PathBuf,
        runtime_manifest: PathBuf,
        http: reqwest::Client,
    ) -> Arc<Self> {
        let initial_status = LocalServerStatus {
            installed: local_payload::resolved_active(&root)
                .ok()
                .flatten()
                .is_some(),
            logs: tail_file(&root.join("logs").join("installer.log"), MAX_LOG_LINES),
            ..LocalServerStatus::default()
        };
        Arc::new(Self {
            root,
            data_root,
            bundle_archive,
            bundle_manifest,
            runtime_archive,
            runtime_manifest,
            http,
            status: RwLock::new(initial_status),
            status_version: watch::channel(0).0,
            child: Mutex::new(None),
            install_running: AtomicBool::new(false),
            shutting_down: Arc::new(AtomicBool::new(false)),
            start_lock: tokio::sync::Mutex::new(()),
            health_monitor: AtomicBool::new(false),
            ready_cache: tokio::sync::Mutex::new(None),
            bridge_secret: std::sync::OnceLock::new(),
        })
    }

    /// Receiver that wakes whenever the status, logs or bridge readiness change.
    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.status_version.subscribe()
    }

    /// Something observable changed (status, logs, bridge state): wake subscribers.
    pub fn notify_changed(&self) {
        self.status_version.send_modify(|version| *version += 1);
    }

    /// 设置桥接秘密（进程内只设一次；重复设置忽略）。须在首次 start 之前调用。
    pub fn set_bridge_secret(&self, secret: String) {
        let _ = self.bridge_secret.set(secret);
    }

    fn data_dir(&self) -> PathBuf {
        self.data_root.clone()
    }

    /// 能力文件存储根：`local-server` 的父目录，即 Tauri 应用本地数据目录。
    fn capability_root(&self) -> PathBuf {
        self.root
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| self.root.clone())
    }

    fn node_runtime_dir(&self) -> PathBuf {
        self.root.join("tools").join("node")
    }

    fn log_path(&self) -> PathBuf {
        self.root.join("logs").join("server.log")
    }

    fn installer_log_path(&self) -> PathBuf {
        self.root.join("logs").join("installer.log")
    }

    fn pid_path(&self) -> PathBuf {
        self.root.join("server.pid")
    }

    fn executable(&self) -> PathBuf {
        local_payload::resolved_active(&self.root)
            .ok()
            .flatten()
            .map(|release| release.executable)
            .unwrap_or_else(|| self.root.join("missing-python"))
    }

    pub fn is_installed(&self) -> bool {
        local_payload::resolved_active(&self.root)
            .ok()
            .flatten()
            .is_some()
    }

    pub fn needs_install(&self) -> bool {
        local_payload::needs_install(&self.payload_paths())
    }

    fn payload_paths(&self) -> PayloadPaths<'_> {
        PayloadPaths {
            root: &self.root,
            source_archive: &self.bundle_archive,
            source_manifest: &self.bundle_manifest,
            runtime_archive: &self.runtime_archive,
            runtime_manifest: &self.runtime_manifest,
        }
    }

    pub async fn snapshot(&self) -> LocalServerStatus {
        let mut value = self.status.read().await.clone();
        if !self.install_running.load(Ordering::SeqCst) && value.installed && self.is_ready().await
        {
            value.phase = "ready".to_string();
            value.progress = 100;
            value.message = "本机服务已就绪".to_string();
            value.ready = true;
        } else if value.ready {
            value.phase = "error".into();
            value.ready = false;
            value.progress = 0;
            value.message = "本机服务连接已断开，请重试".into();
        }
        value
    }

    async fn update(&self, phase: &str, progress: u8, message: impl Into<String>) {
        let mut status = self.status.write().await;
        status.phase = phase.to_string();
        status.progress = progress.min(100);
        status.message = message.into();
        if phase == "ready" {
            status.installed = true;
        }
        status.ready = phase == "ready";
        drop(status);
        self.notify_changed();
    }

    async fn append_log(&self, line: impl Into<String>) {
        let line = line.into();
        if line.trim().is_empty() {
            return;
        }
        let installer_log_path = self.installer_log_path();
        if let Some(parent) = installer_log_path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(installer_log_path)
        {
            let _ = writeln!(file, "{line}");
        }
        let mut status = self.status.write().await;
        let mut logs: VecDeque<String> = status.logs.drain(..).collect();
        logs.push_back(line);
        while logs.len() > MAX_LOG_LINES {
            logs.pop_front();
        }
        status.logs = logs.into_iter().collect();
        drop(status);
        self.notify_changed();
    }

    pub async fn probe_base(http: &reqwest::Client, base: &str) -> bool {
        let target = format!("{}/health", base.trim_end_matches('/'));
        http.get(target)
            .timeout(Duration::from_secs(3))
            .send()
            .await
            .map(|response| response.status().is_success())
            .unwrap_or(false)
    }

    pub async fn is_ready(&self) -> bool {
        let mut cache = self.ready_cache.lock().await;
        if let Some((at, ready)) = *cache {
            if at.elapsed() < Duration::from_secs(1) {
                return ready;
            }
        }
        let ready = self.probe_ready().await;
        *cache = Some((std::time::Instant::now(), ready));
        ready
    }

    async fn probe_ready(&self) -> bool {
        let target = format!("{}/health", local_server_base());
        let Ok(response) = self
            .http
            .get(target)
            .timeout(Duration::from_secs(3))
            .send()
            .await
        else {
            return false;
        };
        if !response.status().is_success() {
            return false;
        }
        response
            .json::<serde_json::Value>()
            .await
            .ok()
            .and_then(|body| {
                body.get("service")
                    .and_then(|value| value.as_str())
                    .map(str::to_owned)
            })
            .as_deref()
            == Some(crate::brand::LOCAL_SERVICE_NAME)
    }
}
