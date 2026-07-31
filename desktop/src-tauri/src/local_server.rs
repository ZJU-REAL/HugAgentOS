//! 桌面端托管的无 Docker 本机服务。
//!
//! Windows、macOS 与 Linux 安装包携带同版本 CE 派生树和私有 Python 运行时。
//! 这里负责离线安装、启动服务、轮询健康状态，并在桌面进程退出时回收整个进程组。
//! 运行环境位于应用本地数据目录；macOS/Linux 业务数据统一放在 ``~/.hugagent``。

use crate::local_payload::{self, PayloadPaths};
use serde::{Deserialize, Serialize};
use std::collections::{HashSet, VecDeque};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::RwLock;

pub const LOCAL_SERVER_PORT: u16 = 32101;
pub const LOCAL_SERVER_BASE: &str = "http://127.0.0.1:32101";
const MAX_LOG_LINES: usize = 80;
const MAX_DATA_BACKUPS: usize = 3;
const BACKUP_FILES: &[&str] = &[
    "data.db",
    "data.db-wal",
    "data.db-shm",
    "milvus.db",
    "config.env",
    "secrets.json",
    "catalog.json",
];

#[derive(Debug, Deserialize, Serialize)]
struct DataBackupManifest {
    schema: u32,
    files: Vec<String>,
}

/// Resolve the business-data directory independently from the managed runtime.
///
/// The Tauri application-data root is still the right place for the bundled
/// Python/runtime payload. On macOS it contains ``Application Support`` and on
/// Linux it may live below a desktop-specific data root; neither should become a
/// model-generated workspace path. Keep only the runtime there and use the same
/// ``~/.hugagent`` data root as the standalone local installer. Existing desktop
/// data is moved on first launch when that does not overwrite standalone data.
pub fn resolve_local_server_data_dir(runtime_root: &Path, home_dir: Option<&Path>) -> PathBuf {
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    {
        let legacy = runtime_root.join("data");
        let Some(home_dir) = home_dir else {
            return legacy;
        };
        let preferred = home_dir.join(".hugagent");
        if let Err(error) = migrate_legacy_data_dir(&legacy, &preferred) {
            eprintln!("[local-server] 迁移本机数据目录失败，继续使用旧目录：{error}");
            return legacy;
        }
        preferred
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let _ = home_dir;
        runtime_root.join("data")
    }
}

#[cfg(any(target_os = "macos", target_os = "linux", test))]
fn migrate_legacy_data_dir(legacy: &Path, preferred: &Path) -> Result<(), String> {
    if !legacy.exists() || legacy == preferred {
        return Ok(());
    }
    let parent = preferred
        .parent()
        .ok_or_else(|| format!("目标目录没有父目录：{}", preferred.display()))?;
    std::fs::create_dir_all(parent)
        .map_err(|error| format!("创建 {} 失败：{error}", parent.display()))?;

    if preferred.exists() {
        let is_empty = preferred
            .read_dir()
            .map_err(|error| format!("读取 {} 失败：{error}", preferred.display()))?
            .next()
            .is_none();
        if !is_empty {
            eprintln!(
                "[local-server] {} 已有数据，将其作为统一数据目录；旧目录 {} 保留为备份",
                preferred.display(),
                legacy.display()
            );
            return Ok(());
        }
        std::fs::remove_dir(preferred)
            .map_err(|error| format!("移除空目录 {} 失败：{error}", preferred.display()))?;
    }

    std::fs::rename(legacy, preferred).map_err(|error| {
        format!(
            "无法把 {} 移到 {}：{error}",
            legacy.display(),
            preferred.display()
        )
    })
}

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
            server_base: LOCAL_SERVER_BASE.to_string(),
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
    child: Mutex<Option<Child>>,
    install_running: AtomicBool,
    shutting_down: AtomicBool,
    /// 混合架构（P2 身份桥）：桌面壳生成的桥接秘密。设置后孵化本机后端时注入
    /// `HUGAGENT_DESKTOP_BRIDGE_SECRET`（身份桥）与 `CONFIG_TOKEN`（壳持有本机
    /// 实例的控制台令牌，用于云端模型配置下发 /v1/models/import）。
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
            child: Mutex::new(None),
            install_running: AtomicBool::new(false),
            shutting_down: AtomicBool::new(false),
            bridge_secret: std::sync::OnceLock::new(),
        })
    }

    /// 设置桥接秘密（进程内只设一次；重复设置忽略）。须在首次 start 之前调用。
    pub fn set_bridge_secret(&self, secret: String) {
        let _ = self.bridge_secret.set(secret);
    }

    fn data_dir(&self) -> PathBuf {
        self.data_root.clone()
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
        value.installed = self.is_installed();
        if self.is_ready().await {
            value.phase = "ready".to_string();
            value.progress = 100;
            value.message = "本机服务已就绪".to_string();
            value.ready = true;
        }
        value
    }

    async fn update(&self, phase: &str, progress: u8, message: impl Into<String>) {
        let mut status = self.status.write().await;
        status.phase = phase.to_string();
        status.progress = progress.min(100);
        status.message = message.into();
        status.installed = self.is_installed();
        status.ready = phase == "ready";
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
        let target = format!("{LOCAL_SERVER_BASE}/health");
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

    /// 后台启动已安装的本机服务；重复调用是幂等的。
    pub fn start_in_background(self: &Arc<Self>) {
        if self.shutting_down.load(Ordering::SeqCst) {
            return;
        }
        let manager = self.clone();
        tauri::async_runtime::spawn(async move {
            if let Err(error) = manager.start_server().await {
                manager.update("error", 0, error).await;
            }
        });
    }

    async fn start_server(self: &Arc<Self>) -> Result<(), String> {
        if self.shutting_down.load(Ordering::SeqCst) {
            return Err("桌面端正在退出，不再启动本机服务".to_string());
        }
        if self.is_ready().await {
            self.update("ready", 100, "本机服务已就绪").await;
            return Ok(());
        }
        if !self.is_installed() {
            return Err("本机服务尚未安装".to_string());
        }
        let release = local_payload::resolved_active(&self.root)?
            .ok_or_else(|| "本机服务版本状态无效，请重新安装".to_string())?;

        {
            let mut child_guard = self.child.lock().map_err(|_| "服务进程锁异常")?;
            let already_running = if let Some(child) = child_guard.as_mut() {
                if child
                    .try_wait()
                    .map_err(|e| format!("检查服务进程失败：{e}"))?
                    .is_none()
                {
                    true
                } else {
                    *child_guard = None;
                    false
                }
            } else {
                false
            };

            if !already_running {
                if self.shutting_down.load(Ordering::SeqCst) {
                    return Err("桌面端正在退出，不再启动本机服务".to_string());
                }
                std::fs::create_dir_all(self.root.join("logs"))
                    .map_err(|e| format!("创建日志目录失败：{e}"))?;
                let stdout = open_log(&self.log_path())?;
                let stderr = stdout
                    .try_clone()
                    .map_err(|e| format!("打开服务错误日志失败：{e}"))?;
                let backend_cli = release
                    .source_dir
                    .join("src")
                    .join("backend")
                    .join("cli.py");
                let mut command = Command::new(&release.executable);
                command
                    .arg(&backend_cli)
                    .arg("serve")
                    .args([
                        "--host",
                        "127.0.0.1",
                        "--port",
                        &LOCAL_SERVER_PORT.to_string(),
                    ])
                    .arg("--no-browser")
                    .current_dir(&release.source_dir)
                    .env("HUGAGENT_HOME", self.data_dir())
                    .env("PYTHONUTF8", "1")
                    .env("PYTHONIOENCODING", "utf-8")
                    .env("PYTHONDONTWRITEBYTECODE", "1")
                    .env("HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS", "1")
                    .env(
                        "FRONTEND_DIST_DIR",
                        release.source_dir.join("src").join("frontend").join("dist"),
                    )
                    .env("NODE_PATH", self.node_runtime_dir().join("node_modules"))
                    .env(
                        "PLAYWRIGHT_BROWSERS_PATH",
                        self.node_runtime_dir().join("browsers"),
                    )
                    .stdin(Stdio::null())
                    .stdout(Stdio::from(stdout))
                    .stderr(Stdio::from(stderr));
                self.apply_tool_path(&mut command);
                // 混合架构：把桥接秘密注入本机后端（身份桥 + 壳持有的本机控制台令牌）。
                if let Some(secret) = self.bridge_secret.get() {
                    command.env("HUGAGENT_DESKTOP_BRIDGE_SECRET", secret);
                    command.env("CONFIG_TOKEN", secret);
                }
                configure_process_group(&mut command);
                hide_console(&mut command);
                let child = command
                    .spawn()
                    .map_err(|e| format!("启动本机服务失败：{e}"))?;
                let pid = child.id();
                if let Err(error) = std::fs::write(self.pid_path(), pid.to_string()) {
                    let mut child = child;
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!("记录本机服务进程失败：{error}"));
                }
                *child_guard = Some(child);
            }
        }

        self.update("starting", 92, "正在启动本机服务…").await;
        for attempt in 0..90u8 {
            if self.is_ready().await {
                self.update("ready", 100, "本机服务已就绪").await;
                return Ok(());
            }
            let exited = {
                let mut guard = self.child.lock().map_err(|_| "服务进程锁异常")?;
                match guard.as_mut() {
                    Some(child) => child
                        .try_wait()
                        .map_err(|e| format!("检查服务进程失败：{e}"))?
                        .map(|status| status.to_string()),
                    None => Some("进程不存在".to_string()),
                }
            };
            if let Some(status) = exited {
                let _ = std::fs::remove_file(self.pid_path());
                for line in tail_file(&self.log_path(), 30) {
                    self.append_log(line).await;
                }
                return Err(format!("本机服务提前退出（{status}），请查看安装日志"));
            }
            self.update(
                "starting",
                92 + (attempt / 12).min(7),
                "正在等待本机服务通过健康检查…",
            )
            .await;
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
        for line in tail_file(&self.log_path(), 30) {
            self.append_log(line).await;
        }
        let _ = self.stop_server();
        Err("本机服务启动超时，请查看日志后重试".to_string())
    }

    fn apply_tool_path(&self, command: &mut Command) {
        let mut paths = Vec::new();
        for filename in ["node-executable.txt", "bash-executable.txt"] {
            let Ok(executable) = std::fs::read_to_string(self.root.join("tools").join(filename))
            else {
                continue;
            };
            let executable = PathBuf::from(executable.trim());
            if let Some(parent) = executable.parent() {
                paths.push(parent.to_path_buf());
            }
        }
        if let Some(current) = std::env::var_os("PATH") {
            paths.extend(std::env::split_paths(&current));
        }
        if let Ok(combined) = std::env::join_paths(paths) {
            command.env("PATH", combined);
        }
    }

    /// 从桌面安装包携带的 CE 派生树安装或升级本机服务。
    pub fn install_in_background(self: &Arc<Self>) -> bool {
        if self.shutting_down.load(Ordering::SeqCst) {
            return false;
        }
        if self
            .install_running
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return false;
        }
        let manager = self.clone();
        tauri::async_runtime::spawn(async move {
            let result = manager.run_install().await;
            manager.install_running.store(false, Ordering::SeqCst);
            if let Err(error) = result {
                manager.append_log(format!("安装失败：{error}")).await;
                manager.update("error", 0, error).await;
            }
        });
        true
    }

    /// 用户点击「一键安装并启动」时，已安装同版本则只启动，否则执行安装/升级。
    pub fn prepare_in_background(self: &Arc<Self>) -> bool {
        if self.shutting_down.load(Ordering::SeqCst) {
            return false;
        }
        if self.needs_install() {
            self.install_in_background()
        } else {
            self.start_in_background();
            true
        }
    }

    async fn run_install(self: &Arc<Self>) -> Result<(), String> {
        if self.shutting_down.load(Ordering::SeqCst) {
            return Err("桌面端正在退出，已取消本机服务安装".to_string());
        }
        if local_payload::current_target() == "unsupported" {
            return Err("当前 CPU 架构没有对应的离线本机运行时".to_string());
        }
        if !self.bundle_archive.is_file()
            || !self.bundle_manifest.is_file()
            || !self.runtime_archive.is_file()
            || !self.runtime_manifest.is_file()
        {
            return Err("安装包未携带本机服务资源，请重新下载完整安装包".to_string());
        }

        let upgrading = self.is_installed();
        self.stop_server()?;
        let data_backup = if upgrading {
            self.update("installing", 3, "正在备份本机数据…").await;
            backup_local_data(&self.data_root, &self.root.join("backups"))?
        } else {
            None
        };
        let installer_log_path = self.installer_log_path();
        if let Some(parent) = installer_log_path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| format!("创建安装日志目录失败：{error}"))?;
        }
        File::create(installer_log_path).map_err(|error| format!("重置安装日志失败：{error}"))?;
        self.status.write().await.logs.clear();
        self.update("installing", 2, "正在准备本机服务…").await;
        self.append_log("开始离线安装本机服务；不会下载 Python 或项目依赖。")
            .await;

        let root = self.root.clone();
        let source_archive = self.bundle_archive.clone();
        let source_manifest = self.bundle_manifest.clone();
        let runtime_archive = self.runtime_archive.clone();
        let runtime_manifest = self.runtime_manifest.clone();
        let (progress_tx, mut progress_rx) = tokio::sync::mpsc::unbounded_channel();
        let install_task = tauri::async_runtime::spawn_blocking(move || {
            let paths = PayloadPaths {
                root: &root,
                source_archive: &source_archive,
                source_manifest: &source_manifest,
                runtime_archive: &runtime_archive,
                runtime_manifest: &runtime_manifest,
            };
            local_payload::install_payloads(&paths, |progress, message| {
                let _ = progress_tx.send((progress, message.to_string()));
            })
        });
        while let Some((progress, message)) = progress_rx.recv().await {
            self.update("installing", progress, &message).await;
            self.append_log(format!("HUGAGENT_PROGRESS|{progress}|{message}"))
                .await;
        }
        if let Err(error) = install_task
            .await
            .map_err(|error| format!("本机服务安装任务异常退出：{error}"))?
        {
            if self.is_installed() {
                self.append_log("新版本安装失败，正在恢复原有本机服务…")
                    .await;
                if let Err(restart_error) = self.start_server().await {
                    self.append_log(format!("原有本机服务恢复失败：{restart_error}"))
                        .await;
                }
            }
            return Err(error);
        }

        self.update("starting", 92, "离线运行环境已就绪，正在启动服务…")
            .await;
        match self.start_server().await {
            Ok(()) => {
                local_payload::prune_old_releases(&self.root);
                prune_data_backups(&self.root.join("backups"));
                Ok(())
            }
            Err(start_error) => {
                if local_payload::restore_previous(&self.root)? {
                    if let Some(backup) = data_backup.as_deref() {
                        restore_local_data(&self.data_root, backup)?;
                    }
                    self.append_log(format!("新版本启动失败，已回滚原有版本：{start_error}"))
                        .await;
                    self.start_server().await.map_err(|rollback_error| {
                        format!(
                            "新版本启动失败（{start_error}），回滚后原有版本也无法启动（{rollback_error}）"
                        )
                    })?;
                    return Err(format!("新版本启动失败，已自动恢复原有版本：{start_error}"));
                }
                Err(start_error)
            }
        }
    }

    fn stop_server(&self) -> Result<(), String> {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(child) = guard.as_mut() {
                let running = child
                    .try_wait()
                    .map_err(|error| format!("检查本机服务进程失败：{error}"))?
                    .is_none();
                #[cfg(target_os = "windows")]
                if running {
                    // This PID comes from the live Child handle owned by this
                    // manager, so killing its process tree cannot target a stale
                    // reused PID. Fall back to Child::kill if taskkill fails.
                    if let Err(tree_error) = stop_process_tree(child.id()) {
                        child.kill().map_err(|kill_error| {
                            format!(
                                "结束本机服务进程树失败（{tree_error}），备用回收也失败：{kill_error}"
                            )
                        })?;
                    }
                }
                #[cfg(not(target_os = "windows"))]
                if running {
                    if let Err(group_error) = stop_live_process_group(child) {
                        child.kill().map_err(|kill_error| {
                            format!(
                                "结束本机服务进程组失败（{group_error}），备用回收也失败：{kill_error}"
                            )
                        })?;
                    }
                }
                let _ = child.wait();
                let _ = std::fs::remove_file(self.pid_path());
                *guard = None;
                return Ok(());
            }
        }
        stop_recorded_server(&self.pid_path(), &self.executable(), &self.root)?;
        let _ = std::fs::remove_file(self.pid_path());
        Ok(())
    }

    /// Stop the managed Python service and permanently block respawn in this
    /// desktop process. A newly launched desktop process creates a fresh manager.
    pub fn shutdown(&self) -> Result<(), String> {
        self.shutting_down.store(true, Ordering::SeqCst);
        self.stop_server()
    }
}

impl Drop for LocalServerManager {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}

fn open_log(path: &Path) -> Result<File, String> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| format!("打开服务日志失败：{e}"))
}

fn backup_local_data(data_root: &Path, backups_root: &Path) -> Result<Option<PathBuf>, String> {
    let files: Vec<&str> = BACKUP_FILES
        .iter()
        .copied()
        .filter(|name| data_root.join(name).is_file())
        .collect();
    if files.is_empty() {
        return Ok(None);
    }
    std::fs::create_dir_all(backups_root)
        .map_err(|error| format!("创建数据备份目录失败：{error}"))?;
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_millis())
        .unwrap_or_default();
    let staged = backups_root.join(format!(".stage-{}-{stamp}", std::process::id()));
    let destination = backups_root.join(format!("backup-{stamp}"));
    let _ = std::fs::remove_dir_all(&staged);
    std::fs::create_dir_all(&staged)
        .map_err(|error| format!("创建数据备份暂存目录失败：{error}"))?;
    let result = (|| {
        for name in &files {
            std::fs::copy(data_root.join(name), staged.join(name))
                .map_err(|error| format!("备份 {name} 失败：{error}"))?;
        }
        let manifest = DataBackupManifest {
            schema: 1,
            files: files.iter().map(|value| (*value).to_string()).collect(),
        };
        let bytes = serde_json::to_vec_pretty(&manifest)
            .map_err(|error| format!("序列化数据备份清单失败：{error}"))?;
        std::fs::write(staged.join("backup.json"), bytes)
            .map_err(|error| format!("写入数据备份清单失败：{error}"))?;
        std::fs::rename(&staged, &destination)
            .map_err(|error| format!("提交数据备份失败：{error}"))?;
        Ok::<(), String>(())
    })();
    if let Err(error) = result {
        let _ = std::fs::remove_dir_all(&staged);
        return Err(error);
    }
    Ok(Some(destination))
}

fn restore_local_data(data_root: &Path, backup: &Path) -> Result<(), String> {
    let manifest: DataBackupManifest = serde_json::from_slice(
        &std::fs::read(backup.join("backup.json"))
            .map_err(|error| format!("读取数据备份清单失败：{error}"))?,
    )
    .map_err(|error| format!("解析数据备份清单失败：{error}"))?;
    if manifest.schema != 1
        || manifest
            .files
            .iter()
            .any(|name| !BACKUP_FILES.contains(&name.as_str()))
    {
        return Err("数据备份清单无效".to_string());
    }
    std::fs::create_dir_all(data_root).map_err(|error| format!("创建本机数据目录失败：{error}"))?;
    let restored: HashSet<String> = manifest.files.iter().cloned().collect();
    for name in BACKUP_FILES {
        if !restored.contains(*name) {
            let _ = std::fs::remove_file(data_root.join(name));
        }
    }
    for name in manifest.files {
        std::fs::copy(backup.join(&name), data_root.join(&name))
            .map_err(|error| format!("恢复 {name} 失败：{error}"))?;
    }
    Ok(())
}

fn prune_data_backups(backups_root: &Path) {
    let Ok(entries) = std::fs::read_dir(backups_root) else {
        return;
    };
    let mut backups: Vec<PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.is_dir()
                && path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.starts_with("backup-"))
        })
        .collect();
    backups.sort();
    let remove_count = backups.len().saturating_sub(MAX_DATA_BACKUPS);
    for path in backups.into_iter().take(remove_count) {
        let _ = std::fs::remove_dir_all(path);
    }
}

#[cfg(target_os = "windows")]
fn hide_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(target_os = "windows"))]
fn hide_console(_command: &mut Command) {}

#[cfg(unix)]
fn configure_process_group(command: &mut Command) {
    use std::os::unix::process::CommandExt;
    command.process_group(0);
}

#[cfg(not(unix))]
fn configure_process_group(_command: &mut Command) {}

#[cfg(unix)]
fn signal_process_group(pid: u32, signal: i32) -> Result<(), String> {
    let result = unsafe { libc::kill(-(pid as i32), signal) };
    if result == 0 {
        return Ok(());
    }
    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(())
    } else {
        Err(format!("发送进程组信号失败：{error}"))
    }
}

#[cfg(unix)]
fn stop_live_process_group(child: &mut Child) -> Result<(), String> {
    signal_process_group(child.id(), libc::SIGTERM)?;
    for _ in 0..30 {
        if child
            .try_wait()
            .map_err(|error| format!("等待本机服务退出失败：{error}"))?
            .is_some()
        {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    signal_process_group(child.id(), libc::SIGKILL)
}

#[cfg(target_os = "windows")]
fn stop_recorded_server(
    pid_path: &Path,
    _expected_executable: &Path,
    install_root: &Path,
) -> Result<(), String> {
    let Ok(raw_pid) = std::fs::read_to_string(pid_path) else {
        return Ok(());
    };
    let pid = raw_pid
        .trim()
        .parse::<u32>()
        .map_err(|_| "本机服务 PID 文件已损坏".to_string())?;
    // PID 文件可能来自上次异常退出；若该 PID 已被别的程序复用，只清理陈旧
    // 记录，不结束无关进程，也不阻断本次重新安装/启动。
    stop_recorded_process_tree(pid, install_root)
}

#[cfg(target_os = "windows")]
fn stop_process_tree(pid: u32) -> Result<(), String> {
    let mut command = Command::new("taskkill.exe");
    command.args(["/PID", &pid.to_string(), "/T", "/F"]);
    hide_console(&mut command);
    let status = command
        .status()
        .map_err(|error| format!("无法回收本机服务进程树：{error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!(
            "回收本机服务进程树失败（退出码 {:?}）",
            status.code()
        ))
    }
}

#[cfg(target_os = "windows")]
fn stop_recorded_process_tree(pid: u32, install_root: &Path) -> Result<(), String> {
    let script = format!(
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; \
         if (-not $p) {{ exit 0 }}; \
         $root=[IO.Path]::GetFullPath($env:HUGAGENT_INSTALL_ROOT).TrimEnd('\\'); \
         if (-not $p.ExecutablePath -or -not [IO.Path]::GetFullPath($p.ExecutablePath).StartsWith($root + '\\',[StringComparison]::OrdinalIgnoreCase)) {{ exit 3 }}; \
         & taskkill.exe /PID {pid} /T /F | Out-Null; exit $LASTEXITCODE"
    );
    let mut command = Command::new("powershell.exe");
    command
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            &script,
        ])
        .env("HUGAGENT_INSTALL_ROOT", install_root);
    hide_console(&mut command);
    let status = command
        .status()
        .map_err(|error| format!("无法回收上次本机服务进程：{error}"))?;
    match status.code() {
        Some(0) => Ok(()),
        Some(3) => Ok(()),
        code => Err(format!("回收上次本机服务进程失败（退出码 {code:?}）")),
    }
}

#[cfg(target_os = "macos")]
fn stop_recorded_server(
    pid_path: &Path,
    _expected_executable: &Path,
    install_root: &Path,
) -> Result<(), String> {
    let Ok(raw_pid) = std::fs::read_to_string(pid_path) else {
        return Ok(());
    };
    let Ok(pid) = raw_pid.trim().parse::<u32>() else {
        return Ok(());
    };
    let output = Command::new("/bin/ps")
        .args(["-p", &pid.to_string(), "-o", "command="])
        .output()
        .map_err(|error| format!("无法检查上次本机服务进程：{error}"))?;
    if !output.status.success() {
        return Ok(());
    }
    let command_line = String::from_utf8_lossy(&output.stdout);
    if !mac_server_command_matches(&command_line, install_root) {
        return Ok(());
    }

    let pid_text = pid.to_string();
    let _ = signal_process_group(pid, libc::SIGTERM);
    for _ in 0..20 {
        if !mac_process_exists(&pid_text) {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let _ = signal_process_group(pid, libc::SIGKILL);
    if mac_process_exists(&pid_text) {
        return Err("无法结束上次遗留的本机服务进程".to_string());
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn mac_process_exists(pid: &str) -> bool {
    Command::new("/bin/kill")
        .args(["-0", pid])
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

#[cfg(any(target_os = "macos", test))]
fn mac_server_command_matches(command_line: &str, install_root: &Path) -> bool {
    let root = install_root.to_string_lossy();
    command_line.contains(root.as_ref())
        && command_line.contains("cli.py")
        && command_line.contains(" serve")
        && command_line.contains(&format!("--port {LOCAL_SERVER_PORT}"))
}

#[cfg(target_os = "linux")]
fn stop_recorded_server(
    pid_path: &Path,
    _expected_executable: &Path,
    install_root: &Path,
) -> Result<(), String> {
    let Ok(raw_pid) = std::fs::read_to_string(pid_path) else {
        return Ok(());
    };
    let Ok(pid) = raw_pid.trim().parse::<u32>() else {
        return Ok(());
    };
    let proc_root = PathBuf::from(format!("/proc/{pid}"));
    if !proc_root.exists() {
        return Ok(());
    }
    let executable = match std::fs::read_link(proc_root.join("exe")) {
        Ok(path) => path,
        Err(_) => return Ok(()),
    };
    let command_line = std::fs::read(proc_root.join("cmdline"))
        .map(|bytes| String::from_utf8_lossy(&bytes).replace('\0', " "))
        .unwrap_or_default();
    if !executable.starts_with(install_root)
        || !linux_server_command_matches(&command_line, install_root)
    {
        return Ok(());
    }
    signal_process_group(pid, libc::SIGTERM)?;
    for _ in 0..20 {
        if !proc_root.exists() {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    signal_process_group(pid, libc::SIGKILL)?;
    Ok(())
}

#[cfg(any(target_os = "linux", test))]
fn linux_server_command_matches(command_line: &str, install_root: &Path) -> bool {
    let root = install_root.to_string_lossy();
    command_line.contains(root.as_ref())
        && command_line.contains("cli.py")
        && command_line.contains(" serve")
        && command_line.contains(&format!("--port {LOCAL_SERVER_PORT}"))
}

fn tail_file(path: &Path, max_lines: usize) -> Vec<String> {
    let Ok(file) = File::open(path) else {
        return Vec::new();
    };
    let mut lines: VecDeque<String> = BufReader::new(file).lines().map_while(Result::ok).collect();
    while lines.len() > max_lines {
        lines.pop_front();
    }
    lines.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manager(name: &str) -> Arc<LocalServerManager> {
        let base = std::env::temp_dir().join(format!(
            "hugagent-desktop-local-server-{name}-{}",
            std::process::id()
        ));
        let root = base.join("installed");
        let bundle_archive = base.join("server-ce.zip");
        let bundle_manifest = base.join("server-ce-manifest.json");
        let runtime_archive = base.join("runtime-core.tar.gz");
        let runtime_manifest = base.join("runtime-manifest.json");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        std::fs::write(&bundle_archive, "test archive").unwrap();
        std::fs::write(&runtime_archive, "test runtime").unwrap();
        LocalServerManager::new(
            root,
            base.join("data"),
            bundle_archive,
            bundle_manifest,
            runtime_archive,
            runtime_manifest,
            reqwest::Client::new(),
        )
    }

    #[test]
    fn missing_or_invalid_payload_requires_reinstall() {
        let manager = manager("manifest");
        assert!(!manager.is_installed());
        assert!(manager.needs_install());
    }

    #[test]
    fn shutdown_prevents_the_local_service_from_restarting() {
        let manager = manager("shutdown");

        manager.shutdown().unwrap();

        assert!(!manager.prepare_in_background());
        assert!(manager.shutting_down.load(Ordering::SeqCst));
    }

    #[test]
    fn windows_uninstaller_stops_and_detaches_managed_runtime_cleanup() {
        let hooks = include_str!("../installer-hooks.nsh");

        assert!(hooks.contains("NSIS_HOOK_PREUNINSTALL"));
        assert!(hooks.contains("taskkill.exe /PID"));
        assert!(hooks.contains("HUGAGENT_DELETE_DATA"));
        assert!(hooks.contains("MB_DEFBUTTON2"));
        assert!(hooks.contains("GetTempFileName"));
        assert!(hooks.contains("ExecShell"));
        assert!(!hooks.contains("RMDir /r"));
    }

    #[test]
    fn every_desktop_target_embeds_source_and_offline_runtime() {
        let windows_config = include_str!("../tauri.windows.conf.json");
        let macos_config = include_str!("../tauri.macos.conf.json");
        let linux_config = include_str!("../tauri.linux.conf.json");

        for config in [windows_config, macos_config, linux_config] {
            assert!(config.contains("server-ce.zip"));
            assert!(config.contains("server-ce-manifest.json"));
            assert!(config.contains("runtime-core.tar.gz"));
            assert!(config.contains("runtime-manifest.json"));
            assert!(!config.contains("\"../generated/server-ce\": \"server-ce\""));
        }
    }

    #[test]
    fn log_tail_keeps_only_recent_lines() {
        let manager = manager("logs");
        let log = manager.root.join("tail.log");
        std::fs::create_dir_all(log.parent().unwrap()).unwrap();
        std::fs::write(&log, "one\ntwo\nthree\n").unwrap();

        assert_eq!(tail_file(&log, 2), vec!["two", "three"]);
    }

    #[test]
    fn upgrade_backup_restores_databases_and_removes_new_wal_files() {
        let manager = manager("data-backup");
        std::fs::create_dir_all(&manager.data_root).unwrap();
        std::fs::write(manager.data_root.join("data.db"), "before").unwrap();
        std::fs::write(manager.data_root.join("config.env"), "old=true").unwrap();
        let backup = backup_local_data(&manager.data_root, &manager.root.join("backups"))
            .unwrap()
            .unwrap();

        std::fs::write(manager.data_root.join("data.db"), "after").unwrap();
        std::fs::write(manager.data_root.join("data.db-wal"), "new wal").unwrap();
        restore_local_data(&manager.data_root, &backup).unwrap();

        assert_eq!(
            std::fs::read_to_string(manager.data_root.join("data.db")).unwrap(),
            "before"
        );
        assert_eq!(
            std::fs::read_to_string(manager.data_root.join("config.env")).unwrap(),
            "old=true"
        );
        assert!(!manager.data_root.join("data.db-wal").exists());
    }

    #[test]
    fn only_three_successful_upgrade_backups_are_retained() {
        let manager = manager("backup-prune");
        let backups = manager.root.join("backups");
        for index in 0..5 {
            std::fs::create_dir_all(backups.join(format!("backup-{index}"))).unwrap();
        }
        prune_data_backups(&backups);
        let count = std::fs::read_dir(backups).unwrap().count();
        assert_eq!(count, MAX_DATA_BACKUPS);
    }

    #[test]
    fn legacy_macos_data_moves_to_dot_hugagent_without_copying() {
        let base = std::env::temp_dir().join(format!(
            "hugagent-desktop-data-migration-{}",
            std::process::id()
        ));
        let legacy = base
            .join("Library")
            .join("Application Support")
            .join("data");
        let preferred = base.join("home").join(".hugagent");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(legacy.join("workspace").join("site")).unwrap();
        std::fs::write(legacy.join("data.db"), "desktop data").unwrap();

        migrate_legacy_data_dir(&legacy, &preferred).unwrap();

        assert!(!legacy.exists());
        assert_eq!(
            std::fs::read_to_string(preferred.join("data.db")).unwrap(),
            "desktop data"
        );
        assert!(preferred.join("workspace").join("site").is_dir());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn existing_dot_hugagent_wins_without_overwriting_or_deleting_legacy_data() {
        let base = std::env::temp_dir().join(format!(
            "hugagent-desktop-data-conflict-{}",
            std::process::id()
        ));
        let legacy = base.join("legacy");
        let preferred = base.join("home").join(".hugagent");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&legacy).unwrap();
        std::fs::create_dir_all(&preferred).unwrap();
        std::fs::write(legacy.join("data.db"), "desktop data").unwrap();
        std::fs::write(preferred.join("data.db"), "standalone data").unwrap();

        migrate_legacy_data_dir(&legacy, &preferred).unwrap();
        assert!(legacy.join("data.db").is_file());
        assert_eq!(
            std::fs::read_to_string(preferred.join("data.db")).unwrap(),
            "standalone data"
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn mac_stale_process_match_is_scoped_to_this_install_and_port() {
        let root = Path::new("/Users/test/Library/Application Support/HugAgentOS/local-server");
        assert!(mac_server_command_matches(
            "/Users/test/Library/Application Support/HugAgentOS/local-server/releases/runtimes/abc/python/bin/python3 /Users/test/Library/Application Support/HugAgentOS/local-server/releases/sources/def/src/backend/cli.py serve --host 127.0.0.1 --port 32101",
            root,
        ));
        assert!(!mac_server_command_matches(
            "/tmp/python /tmp/cli.py serve --host 127.0.0.1 --port 32101",
            root,
        ));
        assert!(!mac_server_command_matches(
            "/Users/test/Library/Application Support/HugAgentOS/local-server/releases/runtimes/abc/python/bin/python3 /Users/test/Library/Application Support/HugAgentOS/local-server/releases/sources/def/src/backend/cli.py serve --port 32102",
            root,
        ));
    }

    #[test]
    fn linux_stale_process_match_is_scoped_to_this_install_and_port() {
        let root = Path::new("/home/test/.local/share/hugagent/local-server");
        assert!(linux_server_command_matches(
            "/home/test/.local/share/hugagent/local-server/releases/runtimes/abc/python/bin/python3 /home/test/.local/share/hugagent/local-server/releases/sources/def/src/backend/cli.py serve --host 127.0.0.1 --port 32101",
            root,
        ));
        assert!(!linux_server_command_matches(
            "/tmp/python /tmp/cli.py serve --port 32101",
            root,
        ));
    }
}
