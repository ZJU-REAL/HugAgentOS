use super::logging::pump_output;
use super::logging::tail_file;
use super::logging::RotatingLog;
use super::process::configure_process_group;
#[cfg(target_os = "windows")]
use super::process::stop_recorded_process_tree;
use super::process::stop_recorded_server;
#[cfg(target_os = "windows")]
use super::process::windows_local_server_pids;
use super::LOCAL_SERVER_PORT;
use crate::child_process::hide_console;
use crate::local_payload;
use crate::local_server::LocalServerManager;
use std::path::PathBuf;
use std::process::Command;
use std::process::Stdio;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
impl LocalServerManager {
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

    pub(super) async fn start_server(self: &Arc<Self>) -> Result<(), String> {
        let _start = self.start_lock.lock().await;
        *self.ready_cache.lock().await = None;
        if self.shutting_down.load(Ordering::SeqCst) {
            return Err("桌面端正在退出，不再启动本机服务".to_string());
        }
        if !self.is_installed() {
            return Err("本机服务尚未安装".to_string());
        }
        let release = local_payload::resolved_active(&self.root)?
            .ok_or_else(|| "本机服务版本状态无效，请重新安装".to_string())?;

        let own_child_alive = {
            let mut guard = self.child.lock().map_err(|_| "服务进程锁异常")?;
            match guard.as_mut() {
                Some(child) => child
                    .try_wait()
                    .map_err(|e| format!("检查服务进程失败：{e}"))?
                    .is_none(),
                None => false,
            }
        };
        if self.probe_ready().await {
            if own_child_alive {
                self.update("ready", 100, "本机服务已就绪").await;
                self.monitor_health();
                return Ok(());
            }
            // A listener this process did not spawn is never adopted: it may run
            // an older release, it would outlive us, and on macOS its TCC
            // attribution is already broken. Reclaim it, then start our own.
            // The recorded PID covers every normal case; the full process scan
            // only runs when that record is missing or stale.
            stop_recorded_server(&self.pid_path(), &release.executable, &self.root)?;
            let _ = std::fs::remove_file(self.pid_path());
            #[cfg(target_os = "windows")]
            if self.probe_ready().await {
                for pid in windows_local_server_pids(&self.root, None)? {
                    stop_recorded_process_tree(pid, &self.root)?;
                }
            }
            if self.probe_ready().await {
                return Err(format!(
                    "{LOCAL_SERVER_PORT} 端口被非本客户端管理的本机服务占用，请退出后重试"
                ));
            }
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
                if self.shutting_down.load(Ordering::SeqCst) {
                    return Err("桌面端正在退出，不再启动本机服务".to_string());
                }
                std::fs::create_dir_all(self.root.join("logs"))
                    .map_err(|e| format!("创建日志目录失败：{e}"))?;
                let log = Arc::new(Mutex::new(RotatingLog::open(&self.log_path())?));
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
                    // 能力文件存储根（skills/ plugins/ agents/ mcp.json）：应用本地数据目录
                    // 本身（Windows 即 %LOCALAPPDATA%\<identifier>），不是 local-server 子目录。
                    .env("HUGAGENT_CAPS_ROOT", self.capability_root())
                    .env("PYTHONUTF8", "1")
                    .env("PYTHONIOENCODING", "utf-8")
                    .env("PYTHONDONTWRITEBYTECODE", "1")
                    // sidecar 端口跟着壳的品牌命名空间走，后端不再假定 8900 / 9100 段。
                    .env(
                        "SANDBOX_RUNNER_URL",
                        format!(
                            "http://127.0.0.1:{}",
                            crate::brand::LOCAL_SCRIPT_RUNNER_PORT
                        ),
                    )
                    .env(
                        "HUGAGENT_LOCAL_MCP_PORT_OFFSET",
                        crate::brand::LOCAL_MCP_PORT_OFFSET.to_string(),
                    )
                    .env(
                        "FRONTEND_DIST_DIR",
                        release.source_dir.join("src").join("frontend").join("dist"),
                    )
                    .env("NODE_PATH", self.node_runtime_dir().join("node_modules"))
                    .env(
                        "PLAYWRIGHT_BROWSERS_PATH",
                        release.smoke_test.parent().ok_or("运行时目录无效")?.join("native").join("browser"),
                    )
                    .stdin(Stdio::null())
                    // 走管道而不是直接把文件交给子进程：句柄留在壳这边，日志才能在运行
                    // 中滚存，而不是只在下次启动时才发现已经涨到几百 MB。
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped());
                self.apply_tool_path(&mut command);
                // 混合架构：把桥接秘密注入本机后端（身份桥 + 壳持有的本机控制台令牌）。
                // 工具全部来自云端：本机不引导带 MCP 的默认插件，也不起内置 MCP。
                if let Some(secret) = self.bridge_secret.get() {
                    command.env("HUGAGENT_DESKTOP_BRIDGE_SECRET", secret);
                    command.env("CONFIG_TOKEN", secret);
                } else {
                    command.env("HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS", "1");
                }
                configure_process_group(&mut command);
                hide_console(&mut command);
                let mut child = super::managed_process::ManagedProcess::spawn(&mut command)?;
                if let Some(output) = child.take_stdout() {
                    pump_output(output, log.clone());
                }
                if let Some(errors) = child.take_stderr() {
                    pump_output(errors, log);
                }
                let pid = child.id();
                if let Err(error) = std::fs::write(self.pid_path(), pid.to_string()) {
                    let _ = child.stop();
                    return Err(format!("记录本机服务进程失败：{error}"));
                }
                *child_guard = Some(child);
            }
        }

        self.update("starting", 92, "正在启动本机服务…").await;
        for attempt in 0..90u8 {
            if self.probe_ready().await {
                self.update("ready", 100, "本机服务已就绪").await;
                self.monitor_health();
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
                self.stop_server()?;
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

    pub(super) fn apply_tool_path(&self, command: &mut Command) {
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

    fn monitor_health(self: &Arc<Self>) {
        if self.health_monitor.swap(true, Ordering::SeqCst) {
            return;
        }
        let weak = Arc::downgrade(self);
        tauri::async_runtime::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(5)).await;
                let Some(manager) = weak.upgrade() else {
                    return;
                };
                if manager.shutting_down.load(Ordering::SeqCst) {
                    return;
                }
                if manager.install_running.load(Ordering::SeqCst) {
                    continue;
                }
                let alive = manager
                    .child
                    .lock()
                    .ok()
                    .and_then(|mut c| c.as_mut().and_then(|p| p.try_wait().ok()))
                    .is_some_and(|s| s.is_none());
                let ready = alive && manager.is_ready().await;
                let previous = manager.status.read().await.ready;
                if ready != previous {
                    manager
                        .update(
                            if ready { "ready" } else { "error" },
                            if ready { 100 } else { 0 },
                            if ready {
                                "本机服务已就绪"
                            } else {
                                "本机服务连接已断开，请重试"
                            },
                        )
                        .await;
                }
            }
        });
    }

    pub(super) fn stop_server(&self) -> Result<(), String> {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(child) = guard.as_mut() {
                child.stop()?;
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
