//! 桌面端托管的无 Docker 本机服务。
//!
//! Windows 与 macOS 安装包携带同版本 CE 派生树。这里负责调用平台引导脚本创建
//! 独立 Python 环境、启动 `hugagent serve`、轮询健康状态，并在桌面进程退出时
//! 回收子进程。业务数据和 Python 环境都在应用本地数据目录，不写安装目录。

use serde::Serialize;
use std::collections::VecDeque;
#[cfg(target_os = "windows")]
use std::ffi::OsString;
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
            supported: cfg!(any(target_os = "windows", target_os = "macos")),
            server_base: LOCAL_SERVER_BASE.to_string(),
        }
    }
}

pub struct LocalServerManager {
    root: PathBuf,
    bundle_dir: PathBuf,
    installer_script: PathBuf,
    http: reqwest::Client,
    status: RwLock<LocalServerStatus>,
    child: Mutex<Option<Child>>,
    install_running: AtomicBool,
}

impl LocalServerManager {
    pub fn new(
        root: PathBuf,
        bundle_dir: PathBuf,
        installer_script: PathBuf,
        http: reqwest::Client,
    ) -> Arc<Self> {
        let initial_status = LocalServerStatus {
            logs: tail_file(&root.join("logs").join("installer.log"), MAX_LOG_LINES),
            ..LocalServerStatus::default()
        };
        Arc::new(Self {
            root,
            bundle_dir,
            installer_script,
            http,
            status: RwLock::new(initial_status),
            child: Mutex::new(None),
            install_running: AtomicBool::new(false),
        })
    }

    #[cfg(target_os = "macos")]
    fn source_dir(&self) -> PathBuf {
        let current = self.root.join("current");
        if current.is_dir() {
            return current.join("source");
        }
        self.root.join("source")
    }

    #[cfg(not(target_os = "macos"))]
    fn source_dir(&self) -> PathBuf {
        self.root.join("source")
    }

    fn data_dir(&self) -> PathBuf {
        self.root.join("data")
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

    #[cfg(target_os = "windows")]
    fn executable(&self) -> PathBuf {
        self.root.join("venv").join("Scripts").join("hugagent.exe")
    }

    #[cfg(target_os = "macos")]
    fn executable(&self) -> PathBuf {
        let current = self.root.join("current");
        if current.is_dir() {
            return current.join("venv").join("bin").join("hugagent");
        }
        self.root.join("venv").join("bin").join("hugagent")
    }

    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    fn executable(&self) -> PathBuf {
        self.root.join("venv").join("bin").join("hugagent")
    }

    fn installed_manifest_path(&self) -> PathBuf {
        #[cfg(target_os = "macos")]
        {
            let current = self.root.join("current");
            if current.is_dir() {
                return current.join("desktop-bundle.json");
            }
        }
        self.root.join("installed-bundle.json")
    }

    pub fn is_installed(&self) -> bool {
        self.executable().is_file() && self.installed_manifest_path().is_file()
    }

    pub fn needs_install(&self) -> bool {
        if !self.is_installed() {
            return true;
        }
        let bundled = std::fs::read_to_string(self.bundle_dir.join("desktop-bundle.json"));
        let installed = std::fs::read_to_string(self.installed_manifest_path());
        match (bundled, installed) {
            (Ok(a), Ok(b)) => a.trim() != b.trim(),
            _ => true,
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
            == Some("hugagent")
    }

    /// 后台启动已安装的本机服务；重复调用是幂等的。
    pub fn start_in_background(self: &Arc<Self>) {
        let manager = self.clone();
        tauri::async_runtime::spawn(async move {
            if let Err(error) = manager.start_server().await {
                manager.update("error", 0, error).await;
            }
        });
    }

    async fn start_server(self: &Arc<Self>) -> Result<(), String> {
        if self.is_ready().await {
            self.update("ready", 100, "本机服务已就绪").await;
            return Ok(());
        }
        if !self.is_installed() {
            return Err("本机服务尚未安装".to_string());
        }

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
                std::fs::create_dir_all(self.root.join("logs"))
                    .map_err(|e| format!("创建日志目录失败：{e}"))?;
                let stdout = open_log(&self.log_path())?;
                let stderr = stdout
                    .try_clone()
                    .map_err(|e| format!("打开服务错误日志失败：{e}"))?;
                let mut command = Command::new(self.executable());
                command
                    .arg("serve")
                    .args([
                        "--host",
                        "127.0.0.1",
                        "--port",
                        &LOCAL_SERVER_PORT.to_string(),
                    ])
                    .arg("--no-browser")
                    .current_dir(self.source_dir())
                    .env("HUGAGENT_HOME", self.data_dir())
                    .env(
                        "FRONTEND_DIST_DIR",
                        self.source_dir().join("src").join("frontend").join("dist"),
                    )
                    .stdin(Stdio::null())
                    .stdout(Stdio::from(stdout))
                    .stderr(Stdio::from(stderr));
                self.apply_tool_path(&mut command);
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
            let Ok(executable) = std::fs::read_to_string(self.root.join(filename)) else {
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
        if self.needs_install() {
            self.install_in_background()
        } else {
            self.start_in_background();
            true
        }
    }

    async fn run_install(self: &Arc<Self>) -> Result<(), String> {
        if !cfg!(any(target_os = "windows", target_os = "macos")) {
            return Err("当前版本暂不支持在此系统一键部署本机服务".to_string());
        }
        if !self.bundle_dir.join("pyproject.toml").is_file() {
            return Err("安装包未携带本机服务资源，请重新下载完整安装包".to_string());
        }
        if !self.installer_script.is_file() {
            return Err("安装包缺少本机服务引导脚本".to_string());
        }

        self.stop_server()?;
        let installer_log_path = self.installer_log_path();
        if let Some(parent) = installer_log_path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| format!("创建安装日志目录失败：{error}"))?;
        }
        File::create(installer_log_path).map_err(|error| format!("重置安装日志失败：{error}"))?;
        self.status.write().await.logs.clear();
        self.update("installing", 2, "正在准备本机服务…").await;
        self.append_log("开始安装本机服务；首次安装通常需要数分钟。")
            .await;

        #[cfg(any(target_os = "windows", target_os = "macos"))]
        {
            use tokio::io::{AsyncBufReadExt, BufReader as AsyncBufReader};
            use tokio::process::Command as TokioCommand;

            #[cfg(target_os = "windows")]
            let mut command = TokioCommand::new("powershell.exe");
            #[cfg(target_os = "windows")]
            command
                .args([
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                ])
                .arg("-File")
                .arg(powershell_compatible_path(&self.installer_script))
                .arg("-BundleDir")
                .arg(powershell_compatible_path(&self.bundle_dir))
                .arg("-InstallRoot")
                .arg(powershell_compatible_path(&self.root))
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            #[cfg(target_os = "windows")]
            command.kill_on_drop(true);
            #[cfg(target_os = "windows")]
            hide_tokio_console(&mut command);

            #[cfg(target_os = "macos")]
            let mut command = TokioCommand::new("/bin/bash");
            #[cfg(target_os = "macos")]
            command
                .arg(&self.installer_script)
                .arg("--bundle-dir")
                .arg(&self.bundle_dir)
                .arg("--install-root")
                .arg(&self.root)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .kill_on_drop(true);

            let mut child = command
                .spawn()
                .map_err(|e| format!("无法启动本机服务安装器：{e}"))?;
            let stdout = child.stdout.take().ok_or("无法读取安装器输出")?;
            let stderr = child.stderr.take().ok_or("无法读取安装器错误输出")?;

            let manager_out = self.clone();
            let out_task = tauri::async_runtime::spawn(async move {
                let mut lines = AsyncBufReader::new(stdout).lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    manager_out.consume_installer_line(line).await;
                }
            });
            let manager_err = self.clone();
            let err_task = tauri::async_runtime::spawn(async move {
                let mut lines = AsyncBufReader::new(stderr).lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    manager_err.append_log(line).await;
                }
            });

            let exit = child
                .wait()
                .await
                .map_err(|e| format!("等待安装器退出失败：{e}"))?;
            let _ = tokio::join!(out_task, err_task);
            if !exit.success() {
                let error = format!("依赖安装未完成（退出码 {:?}）", exit.code());
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
        }

        self.update("starting", 92, "依赖安装完成，正在启动服务…")
            .await;
        match self.start_server().await {
            Ok(()) => Ok(()),
            Err(start_error) => {
                #[cfg(target_os = "macos")]
                if restore_previous_release(&self.root)? {
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

    #[cfg(any(target_os = "windows", target_os = "macos"))]
    async fn consume_installer_line(&self, line: String) {
        if let Some(rest) = line.strip_prefix("HUGAGENT_PROGRESS|") {
            let mut parts = rest.splitn(2, '|');
            let progress = parts.next().and_then(|value| value.parse::<u8>().ok());
            let message = parts.next().unwrap_or("正在安装本机服务…");
            if let Some(progress) = progress {
                self.update("installing", progress, message).await;
            }
        }
        self.append_log(line).await;
    }

    fn stop_server(&self) -> Result<(), String> {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(child) = guard.as_mut() {
                #[cfg(target_os = "windows")]
                stop_server_process(child.id(), &self.executable(), false)?;
                #[cfg(not(target_os = "windows"))]
                let _ = child.kill();
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
}

/// Windows PowerShell 5.1 的 `-File` 不接受 `\\?\` 长路径前缀。Tauri 的路径 API
/// 在安装目录中可能返回该格式，因此传给 PowerShell 前恢复为普通 DOS/UNC 路径。
#[cfg(target_os = "windows")]
fn powershell_compatible_path(path: &Path) -> OsString {
    let value = path.as_os_str().to_string_lossy();
    if let Some(rest) = value.strip_prefix(r"\\?\UNC\") {
        return OsString::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = value.strip_prefix(r"\\?\") {
        return OsString::from(rest);
    }
    path.as_os_str().to_owned()
}

impl Drop for LocalServerManager {
    fn drop(&mut self) {
        let _ = self.stop_server();
    }
}

fn open_log(path: &Path) -> Result<File, String> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| format!("打开服务日志失败：{e}"))
}

#[cfg(target_os = "windows")]
fn hide_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(target_os = "windows"))]
fn hide_console(_command: &mut Command) {}

#[cfg(target_os = "windows")]
fn stop_recorded_server(
    pid_path: &Path,
    expected_executable: &Path,
    _install_root: &Path,
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
    stop_server_process(pid, expected_executable, true)
}

#[cfg(target_os = "windows")]
fn stop_server_process(
    pid: u32,
    expected_executable: &Path,
    allow_stale_pid: bool,
) -> Result<(), String> {
    let script = format!(
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; \
         if (-not $p) {{ exit 0 }}; \
         if ($p.ExecutablePath -ne $env:HUGAGENT_EXPECTED_EXE) {{ exit 3 }}; \
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
        .env("HUGAGENT_EXPECTED_EXE", expected_executable);
    hide_console(&mut command);
    let status = command
        .status()
        .map_err(|error| format!("无法回收上次本机服务进程：{error}"))?;
    match status.code() {
        Some(0) => Ok(()),
        Some(3) if allow_stale_pid => Ok(()),
        Some(3) => Err("检测到 PID 已被其他程序复用；为避免误杀，未自动结束该进程".to_string()),
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
    let _ = Command::new("/bin/kill")
        .args(["-TERM", &pid_text])
        .status();
    for _ in 0..20 {
        if !mac_process_exists(&pid_text) {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let _ = Command::new("/bin/kill")
        .args(["-KILL", &pid_text])
        .status();
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
        && command_line.contains("hugagent")
        && command_line.contains(" serve")
        && command_line.contains(&format!("--port {LOCAL_SERVER_PORT}"))
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
fn stop_recorded_server(
    pid_path: &Path,
    _expected_executable: &Path,
    _install_root: &Path,
) -> Result<(), String> {
    let _ = std::fs::remove_file(pid_path);
    Ok(())
}

#[cfg(any(target_os = "macos", test))]
fn restore_previous_release(root: &Path) -> Result<bool, String> {
    let previous = root.join("current.previous");
    if !previous.is_symlink() {
        return Ok(false);
    }
    let current = root.join("current");
    let failed_release = std::fs::read_link(&current).ok();
    std::fs::rename(&previous, &current)
        .map_err(|error| format!("恢复原有本机服务版本失败：{error}"))?;
    if let Some(failed_release) = failed_release {
        let releases = root.join("releases");
        if failed_release.starts_with(&releases) {
            let _ = std::fs::remove_dir_all(failed_release);
        }
    }
    Ok(true)
}

#[cfg(target_os = "windows")]
fn hide_tokio_console(command: &mut tokio::process::Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.as_std_mut().creation_flags(CREATE_NO_WINDOW);
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
        let bundle = base.join("bundle");
        let script = base.join("install.ps1");
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&bundle).unwrap();
        std::fs::write(&script, "# test").unwrap();
        LocalServerManager::new(root, bundle, script, reqwest::Client::new())
    }

    #[test]
    fn manifest_change_requires_reinstall() {
        let manager = manager("manifest");
        std::fs::create_dir_all(manager.executable().parent().unwrap()).unwrap();
        std::fs::write(manager.executable(), "test").unwrap();
        std::fs::write(manager.root.join("installed-bundle.json"), "same\n").unwrap();
        std::fs::write(manager.bundle_dir.join("desktop-bundle.json"), "same\n").unwrap();

        assert!(manager.is_installed());
        assert!(!manager.needs_install());

        std::fs::write(manager.bundle_dir.join("desktop-bundle.json"), "new\n").unwrap();
        assert!(manager.needs_install());
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
    fn mac_stale_process_match_is_scoped_to_this_install_and_port() {
        let root = Path::new("/Users/test/Library/Application Support/HugAgentOS/local-server");
        assert!(mac_server_command_matches(
            "/Users/test/Library/Application Support/HugAgentOS/local-server/releases/abc/venv/bin/python /Users/test/Library/Application Support/HugAgentOS/local-server/current/venv/bin/hugagent serve --host 127.0.0.1 --port 32101",
            root,
        ));
        assert!(!mac_server_command_matches(
            "/tmp/hugagent serve --host 127.0.0.1 --port 32101",
            root,
        ));
        assert!(!mac_server_command_matches(
            "/Users/test/Library/Application Support/HugAgentOS/local-server/current/venv/bin/hugagent serve --port 32102",
            root,
        ));
    }

    #[cfg(unix)]
    #[test]
    fn failed_release_can_atomically_restore_previous_pointer() {
        use std::os::unix::fs::symlink;

        let manager = manager("rollback");
        let root = &manager.root;
        let old = root.join("releases").join("old");
        let new = root.join("releases").join("new");
        std::fs::create_dir_all(&old).unwrap();
        std::fs::create_dir_all(&new).unwrap();
        symlink(&new, root.join("current")).unwrap();
        symlink(&old, root.join("current.previous")).unwrap();

        assert!(restore_previous_release(root).unwrap());
        assert_eq!(std::fs::read_link(root.join("current")).unwrap(), old);
        assert!(!root.join("current.previous").exists());
        assert!(!new.exists());
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn powershell_paths_drop_verbatim_prefixes() {
        assert_eq!(
            powershell_compatible_path(Path::new(r"\\?\C:\HugAgentOS\install.ps1")),
            OsString::from(r"C:\HugAgentOS\install.ps1")
        );
        assert_eq!(
            powershell_compatible_path(Path::new(r"\\?\UNC\server\share\install.ps1")),
            OsString::from(r"\\server\share\install.ps1")
        );
    }
}
